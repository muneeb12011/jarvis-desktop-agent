"""
Background jobs: hand Jarvis something long ("research the best laptops under
$800 and write me a comparison", "go through my Downloads and tell me what's
taking space") and keep talking to it while the job runs.

Each job is a separate agent with its own conversation, sharing the brain and
tools. Jobs don't get the mouse/keyboard (that would fight with you and the
main agent); risky actions still ask for your approval in the HUD. When a job
finishes Jarvis tells you, and the result is fed into the main conversation so
you can ask follow-up questions about it.

Every job keeps a plain-language activity log (what it did, what came of it, what it said it was doing),
so "what is that job doing and why?" has a real answer. You can pause and resume a job, a job that works
too long is stopped and reports what it got done, and finished jobs are saved so they survive a restart.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from . import config, db

logger = logging.getLogger(__name__)

Broadcast = Callable[[dict], Awaitable[None]]

JOB_PROMPT = """

BACKGROUND JOB MODE
You are running as a background job the user delegated, while they do other things.
- Work autonomously from start to finish. Don't ask questions; make sensible assumptions and state them.
- You cannot use the mouse/keyboard tools. Use web, file, shell and integration tools.
- Before each group of tool calls, write ONE short plain sentence saying what you're about to do and why. \
The user can read these while you work.
- If the result is longer than a few paragraphs, save it as a Markdown file under the user's
  Documents\\Jarvis folder (write_file, path like "documents/Jarvis/<short-name>.md") and say where it is.
- Finish with a 1-3 sentence summary of what you found or did.
"""

MAX_PARALLEL = config.MAX_JOBS  # kept under its old name
ACTIVITY_KEPT = 40  # lines of activity remembered per job
ACTIVITY_SHOWN = 6  # lines sent with every update
TICK = 5.0  # watchdog resolution, seconds
_KEY_ARGS = ("path", "folder", "url", "query", "command", "title", "name", "app", "text")


def describe_call(name: str, args: dict) -> str:
    """'move path: a.pdf -> Documents' instead of a bare tool name."""
    label = name.replace("_", " ")
    if args.get("source") and args.get("destination"):
        return f"{label}: {str(args['source'])[:50]} -> {str(args['destination'])[:50]}"
    for key in _KEY_ARGS:
        value = args.get(key)
        if value:
            return f"{label}: {' '.join(str(value).split())[:70]}"
    return label


MANAGER: JobManager | None = None  # set by main at startup


@dataclass
class Job:
    id: str
    title: str
    task: str
    source: str
    status: str = "queued"  # queued | running | done | failed | cancelled | interrupted
    step: str = ""
    steps: int = 0
    result: str = ""
    started: float = field(default_factory=time.time)
    ended: float | None = None
    announce: bool = True
    agent: object | None = None
    runner: asyncio.Task | None = None
    watchdog: asyncio.Task | None = None
    activity: list[dict] = field(default_factory=list)
    waiting: str = ""  # the approval it is waiting on, if any
    paused: bool = False
    errors: int = 0
    active_s: float = 0.0  # working time: not counting pauses or waiting for you
    gate: asyncio.Event = field(default_factory=asyncio.Event)  # set = running, clear = paused

    def note(self, kind: str, text: str) -> None:
        self.activity.append({"t": round(time.time(), 1), "kind": kind, "text": " ".join(text.split())[:200]})
        del self.activity[:-ACTIVITY_KEPT]

    def public(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "step": self.step,
            "steps": self.steps,
            "result": self.result[:2000],
            "started": self.started,
            "ended": self.ended,
            "source": self.source,
            "paused": self.paused,
            "waiting": self.waiting,
            "errors": self.errors,
            "activity": self.activity[-ACTIVITY_SHOWN:],
        }

    def record(self) -> dict:
        """Everything worth keeping after a restart."""
        return {
            "id": self.id,
            "title": self.title,
            "task": self.task,
            "source": self.source,
            "status": self.status,
            "steps": self.steps,
            "result": self.result[:20000],
            "started": self.started,
            "ended": self.ended,
            "activity": self.activity,
        }


def _public_from_record(r: dict) -> dict:
    return {
        "id": r["id"],
        "title": r["title"],
        "status": r["status"],
        "step": "",
        "steps": r["steps"],
        "result": r["result"][:2000],
        "started": r["started"],
        "ended": r["ended"],
        "source": r["source"],
        "paused": False,
        "waiting": "",
        "errors": 0,
        "activity": r["activity"][-ACTIVITY_SHOWN:],
    }


class JobManager:
    def __init__(self, broadcast: Broadcast, brain, on_finished: Callable[[Job], None] | None = None) -> None:
        self.broadcast = broadcast
        self.brain = brain
        self.on_finished = on_finished
        self.jobs: dict[str, Job] = {}
        self._slots = asyncio.Semaphore(config.MAX_JOBS)
        try:
            n = db.mark_interrupted_jobs()
            if n:
                logger.info("%d background job(s) were interrupted by the last shutdown", n)
        except Exception:
            logger.debug("couldn't read saved jobs", exc_info=True)

    # ------------------------------------------------------------ looking

    def list(self) -> list[dict]:
        """Jobs from this session first, then the ones saved from earlier sessions."""
        live = [j.public() for j in sorted(self.jobs.values(), key=lambda j: j.started, reverse=True)]
        try:
            saved = [_public_from_record(r) for r in db.recent_jobs(20) if r["id"] not in self.jobs]
        except Exception:
            saved = []
        return (live + saved)[:20]

    def get(self, job_id: str) -> dict | None:
        """One job in full (live or saved): its task, complete result and whole activity log."""
        job = self.jobs.get(job_id)
        if job:
            return job.record() | {"paused": job.paused, "waiting": job.waiting, "step": job.step, "errors": job.errors}
        try:
            return db.get_job(job_id)
        except Exception:
            return None

    def active(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.status in ("queued", "running")]

    # ------------------------------------------------------------ running

    async def start(
        self, task: str, title: str = "", source: str = "you", announce: bool = True, allow_gui: bool = False
    ) -> Job:
        from .agent import Agent  # local import: agent imports tools which import jobs lazily

        job = Job(id=uuid.uuid4().hex[:8], title=(title or task)[:60], task=task, source=source, announce=announce)
        job.gate.set()

        async def emit(event: dict) -> None:
            await self._on_event(job, event)

        extra = (
            JOB_PROMPT
            if not allow_gui
            else JOB_PROMPT.replace(
                "- You cannot use the mouse/keyboard tools. Use web, file, shell and integration tools.\n", ""
            )
        )
        job.agent = Agent(emit, brain=self.brain, persist=False, allow_gui=allow_gui, prompt_extra=extra)
        job.agent.gate = job.gate  # type: ignore[attr-defined]
        self.jobs[job.id] = job
        self._save(job)
        job.runner = asyncio.create_task(self._run(job))
        job.watchdog = asyncio.create_task(self._watch(job))
        await self._publish(job)
        logger.info("Job %s started (%s): %s", job.id, source, task[:120])
        return job

    def _save(self, job: Job) -> None:
        try:
            db.save_job(job.record())
        except Exception:
            logger.debug("couldn't save job %s", job.id, exc_info=True)

    async def _watch(self, job: Job) -> None:
        """Stops a job that has worked too long. Time spent paused or waiting for the user doesn't count."""
        limit = config.JOB_TIMEOUT_MIN * 60
        while job.status in ("queued", "running"):
            await asyncio.sleep(TICK)
            if job.status == "running" and not job.paused and not job.waiting:
                job.active_s += TICK
                if job.active_s >= limit:
                    await self._time_out(job)
                    return

    async def _time_out(self, job: Job) -> None:
        minutes = config.JOB_TIMEOUT_MIN
        notes = [a["text"] for a in job.activity if a["kind"] == "note"][-3:]
        done = f" Last things it said: {' | '.join(notes)}" if notes else ""
        job.note("note", f"Stopped after {minutes} minutes of work.")
        job.result = f"Stopped after {minutes} minutes without finishing ({job.steps} steps taken).{done}"
        job.status = "failed"
        job.gate.set()
        if job.agent:
            await job.agent.stop(silent=True)  # type: ignore[attr-defined]

    async def _run(self, job: Job) -> None:
        agent = job.agent
        async with self._slots:
            if job.status == "cancelled":
                return
            job.status = "running"
            self._save(job)
            await self._publish(job)
            try:
                await agent.submit(job.task)  # type: ignore[attr-defined]
                if agent.task:  # type: ignore[attr-defined]
                    await agent.task  # type: ignore[attr-defined]
                if job.status == "running":  # not cancelled, not timed out
                    job.result = agent.last_reply or "(no result)"  # type: ignore[attr-defined]
                    job.status = "failed" if agent.last_status == "error" else "done"  # type: ignore[attr-defined]
            except Exception as e:
                logger.exception("Job %s crashed", job.id)
                job.status, job.result = "failed", f"{type(e).__name__}: {e}"
        job.ended = time.time()
        job.step = ""
        job.waiting = ""
        if job.watchdog:
            job.watchdog.cancel()
        self._save(job)
        await self._publish(job)
        if job.status in ("done", "failed"):
            if self.on_finished:
                self.on_finished(job)
            if job.announce:
                verb = "is done" if job.status == "done" else "ran into a problem"
                summary = job.result.strip().splitlines()[-1] if job.result.strip() else ""
                await self.broadcast(
                    {
                        "type": "reminder",
                        "kind": "job",
                        "speak": True,
                        "text": f"{config.address().capitalize()}, your background task {verb}: {job.title}. {summary}"[
                            :600
                        ],
                    }
                )

    # ------------------------------------------------------------ controlling

    async def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job or job.status not in ("queued", "running"):
            return False
        job.status = "cancelled"
        job.paused = False
        job.gate.set()  # let a paused job see the stop
        job.note("note", "Cancelled by you.")
        if job.agent:
            await job.agent.stop(silent=True)  # type: ignore[attr-defined]
        self._save(job)
        await self._publish(job)
        return True

    async def pause(self, job_id: str) -> bool:
        """Hold a job at its next step (a tool already running finishes first)."""
        job = self.jobs.get(job_id)
        if not job or job.status not in ("queued", "running") or job.paused:
            return False
        job.paused = True
        job.gate.clear()
        job.note("note", "Paused by you.")
        await self._publish(job)
        return True

    async def resume(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job or not job.paused:
            return False
        job.paused = False
        job.gate.set()
        job.note("note", "Resumed.")
        await self._publish(job)
        return True

    def resolve_confirm(self, confirm_id: str, approved: bool) -> None:
        for job in self.active():
            if job.agent:
                job.agent.resolve_confirm(confirm_id, approved)  # type: ignore[attr-defined]

    # ------------------------------------------------------------ events

    async def _publish(self, job: Job) -> None:
        await self.broadcast({"type": "job", "job": job.public()})

    async def _on_event(self, job: Job, event: dict) -> None:
        kind = event.get("type")
        if kind == "tool_call" and event.get("name") != "enable_tools":
            job.steps += 1
            job.step = describe_call(event["name"], event.get("args") or {})
            job.note("doing", job.step)
            await self._publish(job)
        elif kind == "tool_result":
            if not event.get("ok", True):
                job.errors += 1
            job.note("result", f"{'ok' if event.get('ok', True) else 'failed'}: {event.get('summary', '')}")
        elif kind == "narration" and event.get("text"):
            job.note("note", str(event["text"]))
            await self._publish(job)
        elif kind in ("confirm_request", "confirm_closed"):
            if kind == "confirm_request":
                event = {**event, "reason": f"[Background: {job.title}] {event.get('reason', '')}"}
                job.waiting = str(event.get("reason", ""))[:200]
                job.note("asked", f"Waiting for your approval: {event.get('reason', '')}")
            else:
                job.waiting = ""
            await self._publish(job)
            await self.broadcast(event)
