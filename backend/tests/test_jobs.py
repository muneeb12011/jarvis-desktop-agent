"""Background jobs: activity log, saved across restarts, pause/resume, time limit, parallel limit."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from app import config, db, jobs, main, memory, tools
from app.providers import AssistantTurn, ToolCall
from app.schemas import ActionMessage, parse_client_message
from app.tools import automation


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "a.db")
    monkeypatch.setattr(config, "CONFIRM_LEVEL", "normal")
    monkeypatch.setattr(jobs, "MANAGER", None)
    tools.load_all()


class ScriptedBrain:
    def __init__(self, turns):
        self.turns = list(turns)
        self.current = type("P", (), {"supports_vision": True})()

    def complete(self, system, messages, specs):
        return self.turns.pop(0)

    def status(self):
        return []


class HangingBrain(ScriptedBrain):
    """Blocks inside the model call until released, like a provider that has stopped answering."""

    def __init__(self, turns=()):
        super().__init__(turns)
        self.release = threading.Event()

    def complete(self, system, messages, specs):
        self.release.wait(10)
        return self.turns.pop(0) if self.turns else AssistantTurn("released", provider="t")


def two_step_turns():
    return [
        AssistantTurn("First I'll check the time.", [ToolCall("c1", "get_datetime", {})], provider="t"),
        AssistantTurn("Now the date again.", [ToolCall("c2", "get_datetime", {})], provider="t"),
        AssistantTurn("All done. Two checks made.", provider="t"),
    ]


def manager(brain, sent=None, finished=None):
    async def broadcast(e):
        if sent is not None:
            sent.append(e)

    return jobs.JobManager(broadcast, brain, on_finished=(finished.append if finished is not None else None))


async def settle(n=0.05):
    await asyncio.sleep(n)


# ---------------------------------------------------------------- readable steps


@pytest.mark.parametrize(
    "name,args,expected",
    [
        (
            "move_path",
            {"source": "downloads/a.pdf", "destination": "documents/"},
            "move path: downloads/a.pdf -> documents/",
        ),
        ("web_search", {"query": "best   laptops\nunder 800"}, "web search: best laptops under 800"),
        ("read_file", {"path": "documents/notes.md"}, "read file: documents/notes.md"),
        ("run_powershell", {"command": "x" * 200}, "run powershell: " + "x" * 70),
        ("get_datetime", {}, "get datetime"),
    ],
)
def test_steps_read_like_sentences(name, args, expected):
    assert jobs.describe_call(name, args) == expected


# ---------------------------------------------------------------- activity log and saved history


def test_a_job_keeps_a_log_of_what_it_did_and_said():
    async def go():
        mgr = manager(ScriptedBrain(two_step_turns()))
        job = await mgr.start("check the time twice", "Time check")
        await job.runner
        return job

    job = asyncio.run(go())
    assert job.status == "done" and job.steps == 2 and job.errors == 0
    kinds = [a["kind"] for a in job.activity]
    assert kinds[:3] == ["note", "doing", "result"]
    texts = " | ".join(a["text"] for a in job.activity)
    assert "First I'll check the time." in texts and "get datetime" in texts and "ok:" in texts
    pub = job.public()
    assert len(pub["activity"]) <= jobs.ACTIVITY_SHOWN and pub["paused"] is False and pub["waiting"] == ""


def test_failed_steps_are_counted():
    async def go():
        brain = ScriptedBrain(
            [
                AssistantTurn("", [ToolCall("c1", "no_such_tool", {})], provider="t"),
                AssistantTurn("gave up", provider="t"),
            ]
        )
        mgr = manager(brain)
        job = await mgr.start("x", "Broken")
        await job.runner
        return job

    job = asyncio.run(go())
    assert job.errors == 1 and any(a["text"].startswith("failed:") for a in job.activity)


def test_the_activity_log_is_capped():
    job = jobs.Job(id="x", title="t", task="t", source="you")
    for i in range(jobs.ACTIVITY_KEPT + 25):
        job.note("doing", f"step {i}")
    assert len(job.activity) == jobs.ACTIVITY_KEPT and job.activity[-1]["text"] == f"step {jobs.ACTIVITY_KEPT + 24}"
    job.note("note", "a" * 1000)
    assert len(job.activity[-1]["text"]) <= 200


def test_finished_jobs_survive_a_restart():
    async def first():
        mgr = manager(ScriptedBrain(two_step_turns()))
        job = await mgr.start("check the time twice", "Time check")
        await job.runner
        return job.id

    job_id = asyncio.run(first())
    saved = db.get_job(job_id)
    assert saved["status"] == "done" and "Two checks" in saved["result"] and saved["task"] == "check the time twice"
    assert saved["activity"] and saved["steps"] == 2

    mgr2 = manager(ScriptedBrain([]))  # a fresh process
    listed = {j["id"]: j for j in mgr2.list()}
    assert listed[job_id]["status"] == "done" and "Two checks" in listed[job_id]["result"]
    full = mgr2.get(job_id)
    assert full["task"] == "check the time twice" and len(full["activity"]) >= 4
    assert mgr2.get("nope") is None


def test_a_job_cut_off_by_a_restart_says_so():
    db.save_job(
        {
            "id": "dead1",
            "title": "Big research",
            "task": "t",
            "source": "you",
            "status": "running",
            "steps": 5,
            "started": 1.0,
        }
    )
    db.save_job(
        {"id": "dead2", "title": "Queued one", "task": "t", "source": "you", "status": "queued", "started": 2.0}
    )
    db.save_job(
        {
            "id": "ok1",
            "title": "Finished",
            "task": "t",
            "source": "you",
            "status": "done",
            "result": "fine",
            "started": 3.0,
            "ended": 4.0,
        }
    )
    mgr = manager(ScriptedBrain([]))
    by_id = {j["id"]: j for j in mgr.list()}
    assert by_id["dead1"]["status"] == "interrupted" and "restarted" in by_id["dead1"]["result"]
    assert by_id["dead2"]["status"] == "interrupted" and by_id["ok1"]["status"] == "done"


def test_history_is_trimmed(monkeypatch):
    monkeypatch.setattr(db, "MAX_SAVED_JOBS", 3)
    for i in range(6):
        db.save_job({"id": f"j{i}", "title": "t", "task": "t", "source": "you", "status": "done", "started": float(i)})
    assert [j["id"] for j in db.recent_jobs(10)] == ["j5", "j4", "j3"]


# ---------------------------------------------------------------- pause, resume, cancel


def test_pause_holds_the_job_and_resume_finishes_it():
    async def go():
        brain = ScriptedBrain(two_step_turns())
        sent = []
        mgr = manager(brain, sent)
        job = await mgr.start("check the time twice", "Time check")
        assert await mgr.pause(job.id)  # before it has taken a single step
        await settle()
        held = (job.status, job.paused, job.steps, len(brain.turns))
        assert not await mgr.pause(job.id)  # already paused
        assert await mgr.resume(job.id)
        await asyncio.wait_for(job.runner, 5)
        return held, job, sent

    held, job, sent = asyncio.run(go())
    assert held == ("running", True, 0, 3)  # nothing consumed while paused
    assert job.status == "done" and job.steps == 2 and not job.paused
    assert any(e["type"] == "job" and e["job"]["paused"] for e in sent)
    assert [a["text"] for a in job.activity if a["text"] in ("Paused by you.", "Resumed.")] == [
        "Paused by you.",
        "Resumed.",
    ]


def test_a_paused_job_can_still_be_cancelled():
    async def go():
        brain = ScriptedBrain(two_step_turns())
        mgr = manager(brain)
        job = await mgr.start("x", "Time check")
        await mgr.pause(job.id)
        await settle()
        assert await mgr.cancel(job.id)
        await asyncio.wait_for(job.runner, 5)
        return job, brain

    job, brain = asyncio.run(go())
    assert job.status == "cancelled" and not job.paused and len(brain.turns) == 3
    assert db.get_job(job.id)["status"] == "cancelled"


def test_pause_and_resume_only_apply_to_the_right_jobs():
    async def go():
        mgr = manager(ScriptedBrain(two_step_turns()))
        assert not await mgr.pause("nope") and not await mgr.resume("nope")
        job = await mgr.start("x", "t")
        assert not await mgr.resume(job.id)  # not paused
        await job.runner
        assert not await mgr.pause(job.id) and not await mgr.cancel(job.id)  # already finished

    asyncio.run(go())


# ---------------------------------------------------------------- the time limit


def test_a_stuck_job_is_stopped_and_reports_progress(monkeypatch):
    monkeypatch.setattr(jobs, "TICK", 0.01)
    monkeypatch.setattr(config, "JOB_TIMEOUT_MIN", 0.0005)  # 30 ms of work

    async def go():
        brain = HangingBrain([AssistantTurn("Checking.", [ToolCall("c1", "get_datetime", {})], provider="t")])
        sent, finished = [], []
        mgr = manager(brain, sent, finished)
        job = await mgr.start("x", "Stuck research")
        try:
            await asyncio.wait_for(job.runner, 5)
        finally:
            brain.release.set()
        return job, sent, finished

    job, sent, finished = asyncio.run(go())
    assert job.status == "failed" and job.result.startswith("Stopped after")
    assert finished and any(e.get("kind") == "job" and "ran into a problem" in e["text"] for e in sent)
    assert db.get_job(job.id)["status"] == "failed"


def test_time_paused_or_waiting_for_you_does_not_count(monkeypatch):
    monkeypatch.setattr(jobs, "TICK", 0.01)
    monkeypatch.setattr(config, "JOB_TIMEOUT_MIN", 0.0005)

    async def go():
        brain = HangingBrain()
        mgr = manager(brain)
        job = await mgr.start("x", "Patient job")
        await mgr.pause(job.id)
        await settle(0.3)  # ten times the limit, but paused
        paused_ok = job.status == "running"
        await mgr.resume(job.id)
        job.waiting = "Delete these files?"
        await settle(0.3)
        waiting_ok = job.status == "running"
        await mgr.cancel(job.id)
        brain.release.set()
        await asyncio.wait_for(job.runner, 5)
        return paused_ok, waiting_ok

    assert asyncio.run(go()) == (True, True)


def test_the_limits_come_from_settings(monkeypatch):
    assert config.MAX_JOBS == 3 and config.JOB_TIMEOUT_MIN == 45


# ---------------------------------------------------------------- parallel limit and approvals


def test_only_max_jobs_run_at_once(monkeypatch):
    monkeypatch.setattr(config, "MAX_JOBS", 1)

    async def go():
        brain = HangingBrain()
        mgr = manager(brain)
        a = await mgr.start("first", "First")
        b = await mgr.start("second", "Second")
        await settle()
        before = (a.status, b.status)
        brain.release.set()
        await asyncio.wait_for(asyncio.gather(a.runner, b.runner), 5)
        return before, a.status, b.status

    assert asyncio.run(go()) == (("running", "queued"), "done", "done")


def test_an_approval_request_is_shown_on_the_job_and_cleared():
    async def go():
        sent = []
        mgr = manager(ScriptedBrain([]), sent)
        job = jobs.Job(id="j1", title="Tidy downloads", task="t", source="you", status="running")
        await mgr._on_event(job, {"type": "confirm_request", "id": "c9", "reason": "Move 40 files?"})
        mid = job.public()
        await mgr._on_event(job, {"type": "confirm_closed", "id": "c9"})
        return mid, job.public(), sent

    mid, after, sent = asyncio.run(go())
    assert mid["waiting"] == "[Background: Tidy downloads] Move 40 files?" and after["waiting"] == ""
    assert [e["type"] for e in sent if e["type"].startswith("confirm")] == ["confirm_request", "confirm_closed"]
    assert sent[1]["reason"].startswith("[Background: Tidy downloads]")  # the HUD says whose question it is


# ---------------------------------------------------------------- tools and HUD


def test_tools_describe_jobs_in_plain_terms():
    async def go():
        brain = HangingBrain()
        mgr = manager(brain)
        jobs.MANAGER = mgr
        job = await mgr.start("check the time twice", "Time check")
        await settle()
        mgr.jobs[job.id].note("note", "Looking at the clock")
        listing = automation.list_jobs()
        await mgr.pause(job.id)
        paused_listing = automation.list_jobs()
        brain.release.set()
        await mgr.cancel(job.id)
        await asyncio.wait_for(job.runner, 5)
        return job, listing, paused_listing

    job, listing, paused_listing = asyncio.run(go())
    assert f"[{job.id}] Time check: running" in listing and "it said: Looking at the clock" in listing
    assert f"[{job.id}] Time check: paused" in paused_listing


def test_job_result_gives_the_full_story_even_after_a_restart():
    async def first():
        mgr = manager(ScriptedBrain(two_step_turns()))
        jobs.MANAGER = mgr
        job = await mgr.start("check the time twice", "Time check")
        await job.runner
        return job.id

    job_id = asyncio.run(first())
    jobs.MANAGER = manager(ScriptedBrain([]))  # new process
    out = automation.job_result(job_id)
    assert "Time check" in out and "Asked to: check the time twice" in out
    assert "What it did:" in out and "doing: get datetime" in out and "Two checks made" in out
    assert "don't have a background job" in automation.job_result("zzzz")
    jobs.MANAGER = None
    assert "don't have a background job" in automation.job_result(job_id)
    assert automation.list_jobs() == "No background jobs."


def test_pause_and_resume_tools_hand_off_to_the_app():
    assert automation.pause_job("ab12").ui == {"type": "pause_job", "id": "ab12"}
    assert automation.resume_job("ab12").ui == {"type": "resume_job", "id": "ab12"}


def test_the_hud_can_pause_and_resume(monkeypatch):
    async def go():
        sent = []
        mgr = manager(HangingBrain(), sent)
        monkeypatch.setattr(main, "job_manager", mgr)
        job = await mgr.start("x", "t")
        await main._hud_action(object(), "pause_job", {"id": job.id})
        paused = job.paused
        await main._hud_action(object(), "resume_job", {"id": job.id})
        resumed = not job.paused
        await mgr.cancel(job.id)
        mgr.jobs[job.id].agent.gate = None
        return paused, resumed

    assert asyncio.run(go()) == (True, True)
    for name in ("pause_job", "resume_job"):
        assert isinstance(parse_client_message({"type": "action", "name": name, "args": {"id": "ab12"}}), ActionMessage)


def test_hooks_answer_in_words(monkeypatch):
    async def go():
        mgr = manager(HangingBrain())
        monkeypatch.setattr(main, "job_manager", mgr)
        job = await mgr.start("x", "t")
        a = await main._hook_pause_job({"id": job.id})
        b = await main._hook_pause_job({"id": job.id})
        c = await main._hook_resume_job({"id": job.id})
        d = await main._hook_resume_job({"id": job.id})
        await mgr.cancel(job.id)
        return a, b, c, d

    a, b, c, d = asyncio.run(go())
    assert a.startswith("Paused") and "already paused" in b and c == "Resumed." and "isn't paused" in d


def test_new_job_tools_are_reachable():
    from app import toolsets

    for name in ("job_result", "pause_job", "resume_job", "list_jobs", "cancel_job"):
        assert name in tools.REGISTRY and name in toolsets.BUILTIN_GROUPS["assistant"], name
    assert SimpleNamespace  # keep the import used
