"""v4 capabilities: toolsets, Gemini rotation, background jobs, routines, activity timeline."""

import asyncio
import datetime as dt
import json
import time

import pytest

from app import activity, config, db, jobs, memory, routines, tools, toolsets
from app.providers import AssistantTurn, GeminiProvider, ProviderUnavailable, ToolCall


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "a.db")
    monkeypatch.setattr(config, "CONFIRM_LEVEL", "normal")
    activity._last.update(id=None, app=None, title=None, end=0.0)


class ScriptedBrain:
    def __init__(self, turns):
        self.turns = list(turns)
        self.seen_tools = []
        self.current = type("P", (), {"supports_vision": True})()

    def complete(self, system, messages, specs):
        self.seen_tools.append([s.name for s in specs])
        return self.turns.pop(0)

    def status(self):
        return []


# ------------------------------------------------------------------ toolsets


def test_toolsets_send_core_plus_meta_and_route_keywords():
    reg = tools.load_all()
    specs = toolsets.select(reg, set())
    names = {s.name for s in specs}
    assert "open_app" in names and "enable_tools" in names
    assert "list_dir" not in names and "activity_report" not in names
    assert len(specs) < len(reg)
    known = set(toolsets.available_groups(reg))
    assert "files" in toolsets.keyword_groups("open the pdf in my downloads folder", known)
    assert "routines" in toolsets.keyword_groups("every weekday at 9 brief me", known)
    assert "activity" in toolsets.keyword_groups("how long was I in vs code today", known)
    # Jobs can't see GUI tools at all.
    assert "click" not in {s.name for s in toolsets.select(reg, set(), allow_gui=False)}


def test_enable_tools_adds_group_for_next_call():
    from app.agent import Agent

    async def go():
        events = []

        async def emit(e):
            events.append(e)

        brain = ScriptedBrain(
            [
                AssistantTurn("", [ToolCall("c1", "enable_tools", {"groups": ["workspaces"]})], provider="t"),
                AssistantTurn("Done.", provider="t"),
            ]
        )
        a = Agent(emit, brain=brain)
        await a.submit("hello there")
        await a.task
        return brain

    brain = asyncio.run(go())
    assert "save_workspace" not in brain.seen_tools[0]
    assert "save_workspace" in brain.seen_tools[1]


# ------------------------------------------------------------------ gemini rotation


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)
        self.headers = {}
        self.ok = status < 400

    def json(self):
        return self._body


def test_gemini_rotates_to_next_model_on_daily_quota(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    g = GeminiProvider()
    g._candidates = ["model-a", "model-b"]
    g._good = ""
    calls = []

    def fake_post(url, payload, headers=None, timeout=None):
        calls.append(url)
        if "model-a" in url:
            return _Resp(429, {"error": {"details": [{"retryDelay": "70000s"}]}})
        return _Resp(200, {"candidates": [{"content": {"role": "model", "parts": [{"text": "hi"}]}}]})

    monkeypatch.setattr("app.providers._post", fake_post)
    turn = g.complete("sys", [{"role": "user", "text": "x"}], [])
    assert turn.text == "hi" and turn.model == "model-b"
    # model-a is now benched; the next call goes straight to model-b, and model-b is remembered.
    calls.clear()
    g.complete("sys", [{"role": "user", "text": "x"}], [])
    assert all("model-b" in c for c in calls)
    fresh = GeminiProvider()
    fresh._candidates = ["model-a", "model-b"]
    assert fresh._usable()[0] == "model-b"


def test_gemini_all_models_exhausted_raises_unavailable(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    g = GeminiProvider()
    g._candidates = ["model-a"]
    g._good = ""
    monkeypatch.setattr(
        "app.providers._post", lambda *a, **k: _Resp(429, {"error": {"details": [{"retryDelay": "500s"}]}})
    )
    with pytest.raises(ProviderUnavailable):
        g.complete("sys", [{"role": "user", "text": "x"}], [])


# ------------------------------------------------------------------ jobs


def test_background_job_runs_and_reports():
    async def go():
        sent = []

        async def broadcast(e):
            sent.append(e)

        finished = []
        brain = ScriptedBrain(
            [
                AssistantTurn("", [ToolCall("c1", "get_datetime", {})], provider="t"),
                AssistantTurn("Report saved. Laptops compared.", provider="t"),
            ]
        )
        mgr = jobs.JobManager(broadcast, brain, on_finished=finished.append)
        job = await mgr.start("compare laptops", "Laptop research")
        await job.runner
        return job, sent, finished, brain

    job, sent, finished, brain = asyncio.run(go())
    assert job.status == "done" and "compared" in job.result
    assert finished and finished[0].id == job.id
    assert any(e["type"] == "reminder" and e.get("kind") == "job" for e in sent)
    assert "click" not in brain.seen_tools[0]  # no mouse/keyboard for jobs
    assert memory.get_recent_conversation() == []  # jobs don't pollute the main conversation log


# ------------------------------------------------------------------ routines


def test_routine_validation_and_crud():
    with pytest.raises(ValueError):
        routines.create("bad", "time", {"at": "9am"}, "x")
    r = routines.create("start my day", "time", {"at": "09:00", "days": "weekdays"}, "brief me")
    assert routines.find("Start My Day")["id"] == r["id"]
    routines.create("start my day", "manual", {}, "brief me and open notes")  # upsert by name
    assert routines.find("start my day")["trigger"] == "manual"
    assert "start my day" in routines.prompt_block()
    assert routines.set_enabled("start my day", False) and not routines.find("start my day")["enabled"]
    assert routines.delete("start my day") and routines.find("start my day") is None


def test_time_trigger_logic():
    r = {"params": {"at": "09:00", "days": "weekdays"}, "last_run": None}
    monday_905 = dt.datetime(2026, 10, 19, 9, 5)
    assert routines.time_due(r, monday_905)
    assert not routines.time_due(r, dt.datetime(2026, 10, 19, 8, 59))
    assert not routines.time_due(r, dt.datetime(2026, 10, 18, 9, 5))  # Sunday
    assert not routines.time_due(r, dt.datetime(2026, 10, 19, 11, 0))  # more than an hour late
    r["last_run"] = monday_905.timestamp()
    assert not routines.time_due(r, dt.datetime(2026, 10, 19, 9, 6))  # already ran today


def test_file_added_trigger_fires_once(tmp_path):
    fired = []

    async def fire(r, ctx):
        fired.append(ctx)

    folder = tmp_path / "dl"
    folder.mkdir()
    routines.create("pdf watcher", "file_added", {"folder": str(folder), "pattern": "*.pdf"}, "summarise it")
    eng = routines.Engine(fire)

    async def go():
        await eng.tick()  # baseline
        (folder / "notes.txt").write_text("x")
        (folder / "big.pdf.crdownload").write_text("x")
        await eng.tick()
        assert not fired
        (folder / "invoice.pdf").write_text("x")
        await eng.tick()
        await eng.tick()

    asyncio.run(go())
    assert len(fired) == 1 and "invoice.pdf" in fired[0]


def test_startup_and_interval_triggers():
    fired = []

    async def fire(r, ctx):
        fired.append(r["name"])

    routines.create("hello", "startup", {}, "greet")
    routines.create("water", "interval", {"minutes": 1}, "remind water")
    eng = routines.Engine(fire)
    eng.started = time.time() - 120

    async def go():
        await eng.tick()
        await eng.tick()

    asyncio.run(go())
    assert fired.count("hello") == 1 and fired.count("water") == 1


# ------------------------------------------------------------------ activity


def test_activity_record_report_search():
    base = dt.datetime.combine(dt.date.today(), dt.time(10, 0)).timestamp()
    for i in range(0, 600, 5):
        activity.record("Code.exe", "main.py - screen-agent - Visual Studio Code", base + i)
    for i in range(600, 900, 5):
        activity.record("chrome.exe", "Interstellar soundtrack - YouTube - Google Chrome", base + i)
    activity.record("KeePass.exe", "Database - secrets", base + 905)

    rep = activity.report("today")
    assert "VS Code: 9m" in rep and "Chrome: 4m" in rep
    assert "Interstellar" in activity.search("interstellar youtube")
    assert "secrets" not in rep and "(private)" in rep  # password managers never store titles
    near = activity.around("10:03")
    assert "main.py" in near
    assert activity.clear() >= 2
