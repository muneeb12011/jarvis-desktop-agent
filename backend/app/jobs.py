"""
Background jobs: hand Jarvis something long ("research the best laptops under
$800 and write me a comparison", "go through my Downloads and tell me what's
taking space") and keep talking to it while the job runs.

Each job is a separate agent with its own conversation, sharing the brain and
tools. Jobs don't get the mouse/keyboard (that would fight with you and the
main agent); risky actions still ask for your approval in the HUD. When a job
finishes Jarvis tells you, and the result is fed into the main conversation so
you can ask follow-up questions about it.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from . import config

logger = logging.getLogger(__name__)

Broadcast = Callable[[dict], Awaitable[None]]

JOB_PROMPT = """

BACKGROUND JOB MODE
You are running as a background job the user delegated, while they do other things.
- Work autonomously from start to finish. Don't ask questions; make sensible assumptions and state them.
- You cannot use the mouse/keyboard tools. Use web, file, shell and integration tools.
- If the result is longer than a few paragraphs, save it as a Markdown file under the user's
  Documents\\Jarvis folder (write_file, path like "documents/Jarvis/<short-name>.md") and say where it is.
- Finish with a 1-3 sentence summary of what you found or did.
"""

MAX_PARALLEL = 3
MANAGER: JobManager | None = None  # set by main at startup


@dataclass
class Job:
    id: str
    title: str
    task: str
    source: str
    status: str = "queued"  # queued | running | done | failed | cancelled
    step: str = ""
    steps: int = 0
    result: str = ""
    started: float = field(default_factory=time.time)
    ended: float | None = None
    announce: bool = True
    agent: object | None = None
    runner: asyncio.Task | None = None

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
        }


class JobManager:
    def __init__(self, broadcast: Broadcast, brain, on_finished: Callable[[Job], None] | None = None) -> None:
        self.broadcast = broadcast
        self.brain = brain
        self.on_finished = on_finished
        self.jobs: dict[str, Job] = {}
        self._slots = asyncio.Semaphore(MAX_PARALLEL)

    def list(self) -> list[dict]:
        return [j.public() for j in sorted(self.jobs.values(), key=lambda j: j.started, reverse=True)][:20]

    def active(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.status in ("queued", "running")]

    async def start(
        self, task: str, title: str = "", source: str = "you", announce: bool = True, allow_gui: bool = False
    ) -> Job:
        from .agent import Agent  # local import: agent imports tools which import jobs lazily

        job = Job(id=uuid.uuid4().hex[:8], title=(title or task)[:60], task=task, source=source, announce=announce)

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
        self.jobs[job.id] = job
        job.runner = asyncio.create_task(self._run(job))
        await self._publish(job)
        logger.info("Job %s started (%s): %s", job.id, source, task[:120])
        return job

    async def _run(self, job: Job) -> None:
        agent = job.agent
        async with self._slots:
            if job.status == "cancelled":
                return
            job.status = "running"
            await self._publish(job)
            try:
                await agent.submit(job.task)  # type: ignore[attr-defined]
                if agent.task:  # type: ignore[attr-defined]
                    await agent.task  # type: ignore[attr-defined]
                if job.status != "cancelled":
                    job.result = agent.last_reply or "(no result)"  # type: ignore[attr-defined]
                    job.status = "failed" if agent.last_status == "error" else "done"  # type: ignore[attr-defined]
            except Exception as e:
                logger.exception("Job %s crashed", job.id)
                job.status, job.result = "failed", f"{type(e).__name__}: {e}"
        job.ended = time.time()
        job.step = ""
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

    async def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job or job.status not in ("queued", "running"):
            return False
        job.status = "cancelled"
        if job.agent:
            await job.agent.stop(silent=True)  # type: ignore[attr-defined]
        await self._publish(job)
        return True

    def resolve_confirm(self, confirm_id: str, approved: bool) -> None:
        for job in self.active():
            if job.agent:
                job.agent.resolve_confirm(confirm_id, approved)  # type: ignore[attr-defined]

    async def _publish(self, job: Job) -> None:
        await self.broadcast({"type": "job", "job": job.public()})

    async def _on_event(self, job: Job, event: dict) -> None:
        kind = event.get("type")
        if kind == "tool_call" and event.get("name") != "enable_tools":
            job.steps += 1
            job.step = f"{event['name'].replace('_', ' ')}"
            await self._publish(job)
        elif kind in ("confirm_request", "confirm_closed"):
            if kind == "confirm_request":
                event = {**event, "reason": f"[Background: {job.title}] {event.get('reason', '')}"}
            await self.broadcast(event)
