"""Undo journal and the self-check pass."""

import asyncio

import pytest

from app import config, db, memory, routines, undo
from app.agent import _run_tool
from app.providers import AssistantTurn, ToolCall
from app.tools import REGISTRY, load_all


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "a.db")
    monkeypatch.setattr(undo, "UNDO_DIR", tmp_path / "undo")
    monkeypatch.setattr(config, "CONFIRM_LEVEL", "normal")
    load_all()


def run(tool_name, **args):
    return _run_tool(REGISTRY[tool_name], args, run_id=1)


def test_undo_file_overwrite_restores_previous_version(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("original")
    result, uid = run("write_file", path=str(f), content="changed")
    assert result.ok and f.read_text() == "changed" and uid
    assert "Restored" in undo.undo_entry(uid)
    assert f.read_text() == "original"
    assert "Already undone" in undo.undo_entry(uid)


def test_undo_created_file_moves_it_to_trash_not_delete(tmp_path):
    f = tmp_path / "new.txt"
    _, uid = run("write_file", path=str(f), content="hello")
    undo.undo_entry(uid)
    assert not f.exists()
    assert list((tmp_path / "undo" / "trash").iterdir())  # recoverable


def test_undo_memory_reminders_routines_and_settings():
    run("remember", fact="likes green tea")
    run("set_reminder", text="call Hassan", minutes_from_now=30)
    run("create_routine", name="water", trigger="interval", action="remind me to drink water", minutes=60)
    before_voice = config.TTS_VOICE
    run("jarvis_settings", voice="en-US-GuyNeural")
    assert config.TTS_VOICE == "en-US-GuyNeural"

    msgs = undo.undo_run(1)  # everything request #1 did, newest first
    assert len(msgs) == 4
    assert before_voice == config.TTS_VOICE
    assert routines.find("water") is None
    assert memory.pending_reminders() == []
    assert "likes green tea" not in memory.get_facts()


def test_undo_forget_brings_facts_back():
    memory.add_fact("works at a startup in Lahore")
    _, uid = run("forget", text="startup")
    assert memory.get_facts() == []
    undo.undo_entry(uid)
    assert "works at a startup in Lahore" in memory.get_facts()


def test_non_undoable_actions_are_journalled_honestly(monkeypatch):
    from app.tools import ToolResult

    monkeypatch.setattr(REGISTRY["run_powershell"], "func", lambda **a: ToolResult("ok"))
    _, uid = run("run_powershell", command="New-Item x.txt")
    assert uid
    assert "Can't undo" in undo.undo_entry(uid)
    assert undo.undo_last() == ["There's nothing I can undo."]
    _, read_only = run("run_powershell", command="Get-Date")
    assert read_only is None  # read-only commands aren't journalled


class ScriptedBrain:
    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = 0
        self.current = type("P", (), {"supports_vision": False})()

    def complete(self, system, messages, specs):
        self.calls += 1
        return self.turns.pop(0)

    def status(self):
        return []


def test_self_check_catches_unfinished_work_and_retries(tmp_path, monkeypatch):
    from app.agent import Agent

    target = tmp_path / "report.md"
    brain = ScriptedBrain([
        AssistantTurn("", [ToolCall("c1", "write_file", {"path": str(tmp_path / "wrong.md"), "content": "x"})]),
        AssistantTurn("Saved your report to report.md."),
        AssistantTurn('{"verdict": "not_done", "reason": "File went to wrong.md", "fix": "write report.md"}'),
        AssistantTurn("", [ToolCall("c2", "write_file", {"path": str(target), "content": "x"})]),
        AssistantTurn("Saved to report.md."),
        AssistantTurn('{"verdict": "done", "reason": "report.md written", "fix": ""}'),
    ])
    events = []

    async def emit(e):
        events.append(e)

    async def go():
        a = Agent(emit, brain=brain)
        await a.submit("save my report as report.md")
        await a.task
        return a

    a = asyncio.run(go())
    assert target.exists()
    assert a.last_reply == "Saved to report.md."
    checks = [e for e in events if e["type"] == "tool_result" and e["name"] == "self_check"]
    assert [c["ok"] for c in checks] == [False, True]
    assert any("Self-check" in m.get("text", "") for m in a.history if m["role"] == "user")


def test_self_check_reports_honestly_after_second_failure(tmp_path):
    from app.agent import Agent

    brain = ScriptedBrain([
        AssistantTurn("", [ToolCall("c1", "write_file", {"path": str(tmp_path / "a.md"), "content": "x"})]),
        AssistantTurn("Done!"),
        AssistantTurn('{"verdict": "not_done", "reason": "wrong file"}'),
        AssistantTurn("", [ToolCall("c2", "write_file", {"path": str(tmp_path / "b.md"), "content": "x"})]),
        AssistantTurn("Done now!"),
        AssistantTurn('{"verdict": "not_done", "reason": "still wrong"}'),
    ])

    async def emit(e):
        pass

    async def go():
        a = Agent(emit, brain=brain)
        await a.submit("write c.md")
        await a.task
        return a

    a = asyncio.run(go())
    assert "not confident" in a.last_reply and "still wrong" in a.last_reply


def test_no_self_check_for_simple_answers():
    from app.agent import Agent

    brain = ScriptedBrain([
        AssistantTurn("", [ToolCall("c1", "get_datetime", {})]),
        AssistantTurn("It's noon."),
    ])

    async def emit(e):
        pass

    async def go():
        a = Agent(emit, brain=brain)
        await a.submit("what time is it")
        await a.task

    asyncio.run(go())
    assert brain.calls == 2
