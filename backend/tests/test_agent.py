"""End-to-end agent loop with a scripted fake brain and no real tools touching the OS."""

import asyncio

import pytest

from app import agent as agent_mod
from app import config, db, memory
from app.providers import AssistantTurn, ToolCall


@pytest.fixture(autouse=True)
def isolated_dbs(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "a.db")
    monkeypatch.setattr(config, "CONFIRM_LEVEL", "normal")
    monkeypatch.setattr(config, "CONFIRM_TIMEOUT_SECONDS", 2)


class ScriptedBrain:
    def __init__(self, turns):
        self.turns = list(turns)
        self.seen = []
        self.current = type("P", (), {"supports_vision": True})()

    def complete(self, system, messages, tools):
        self.seen.append([dict(m) for m in messages])
        return self.turns.pop(0)

    def status(self):
        return []


def make_agent(turns, events):
    async def emit(e):
        events.append(e)

    return agent_mod.Agent(emit, brain=ScriptedBrain(turns))


def test_plain_chat_reply():
    events = []

    async def go():
        a = make_agent([AssistantTurn("Good evening.", provider="fake")], events)
        await a.submit("hello")
        await a.task
        return a

    asyncio.run(go())
    replies = [e for e in events if e["type"] == "reply"]
    assert replies and replies[0]["text"] == "Good evening."
    assert memory.get_recent_conversation()[-1]["content"] == "Good evening."


def test_tool_call_then_answer():
    events = []

    async def go():
        a = make_agent(
            [
                AssistantTurn("", [ToolCall("c1", "remember", {"fact": "likes tea"})], provider="fake"),
                AssistantTurn("Noted.", provider="fake"),
            ],
            events,
        )
        await a.submit("I like tea")
        await a.task
        return a

    a = asyncio.run(go())
    assert "likes tea" in memory.get_facts()
    kinds = [e["type"] for e in events]
    assert "tool_call" in kinds and "tool_result" in kinds and kinds[-1] == "state"
    second_call_msgs = a.brain.seen[1]
    assert second_call_msgs[-1]["role"] == "tool" and "likes tea" in second_call_msgs[-1]["content"]


def test_denied_confirmation_is_reported_to_model():
    events = []

    async def go():
        a = make_agent(
            [
                AssistantTurn("", [ToolCall("c1", "close_app", {"name": "chrome"})], provider="fake"),
                AssistantTurn("Understood, leaving it open.", provider="fake"),
            ],
            events,
        )
        await a.submit("close chrome")
        for _ in range(100):
            req = next((e for e in events if e["type"] == "confirm_request"), None)
            if req:
                break
            await asyncio.sleep(0.01)
        a.resolve_confirm(req["id"], False)
        await a.task
        return a

    a = asyncio.run(go())
    tool_msg = a.brain.seen[1][-1]
    assert tool_msg["role"] == "tool" and "declined" in tool_msg["content"]


def test_stop_repairs_dangling_tool_calls():
    events = []

    async def go():
        a = make_agent(
            [
                AssistantTurn("", [ToolCall("c1", "close_app", {"name": "x"})], provider="fake"),
                AssistantTurn("ok", provider="fake"),
            ],
            events,
        )
        await a.submit("close x")
        await asyncio.sleep(0.05)
        await a.stop()
        await a.task
        a._repair_history()
        ids = {m.get("tool_call_id") for m in a.history if m["role"] == "tool"}
        return ids

    assert "c1" in asyncio.run(go())
