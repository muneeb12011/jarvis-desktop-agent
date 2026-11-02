"""Settings sheet -> backend: choosing a brain over the HUD websocket."""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from app import config, main, memory, onboarding, providers
from app.schemas import ActionMessage, MessageValidationError, parse_client_message

KEY = "k" * 30


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.db")
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    for attr in ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "GROQ_API_KEY"):
        monkeypatch.setattr(config, attr, "")
    monkeypatch.setattr(config, "LLM_PROVIDERS", ["gemini", "ollama"])
    monkeypatch.setattr(os, "environ", os.environ.copy())


@pytest.fixture
def hud(monkeypatch):
    """Run HUD actions against a real Brain, recording what the HUD would receive."""
    sent: list[dict] = []
    broadcast: list[dict] = []

    async def send(ws, payload):
        sent.append(payload)

    async def bcast(payload):
        broadcast.append(payload)

    monkeypatch.setattr(main.hub, "send", send)
    monkeypatch.setattr(main.hub, "broadcast", bcast)
    brain = providers.Brain(["gemini", "ollama"])
    monkeypatch.setattr(main, "agent", SimpleNamespace(brain=brain))

    def run(name, args):
        asyncio.run(main._hud_action(object(), name, args))

    return SimpleNamespace(run=run, sent=sent, broadcast=broadcast, brain=brain)


def test_new_actions_pass_validation_and_junk_does_not():
    for name in ("provider_test", "provider_save"):
        msg = parse_client_message({"type": "action", "name": name, "args": {"provider": "groq"}})
        assert isinstance(msg, ActionMessage) and msg.name == name
    with pytest.raises(MessageValidationError):
        parse_client_message({"type": "action", "name": "provider_delete_everything", "args": {}})


def test_state_sent_to_the_hud_is_json_safe():
    json.dumps(onboarding.providers_state())  # the websocket serialises this on every connect


def test_provider_test_reports_back_to_the_asking_hud(hud, monkeypatch):
    monkeypatch.setattr(onboarding.requests, "get", lambda *a, **k: _Resp(401))
    hud.run("provider_test", {"provider": "anthropic", "key": KEY})
    assert hud.sent == [
        {
            "type": "provider_result",
            "kind": "test",
            "provider": "anthropic",
            "ok": False,
            "message": "Claude rejected that key. Copy it again from the provider's page.",
            "models": [],
        }
    ]
    assert hud.broadcast == []


def test_provider_test_returns_installed_ollama_models(hud, monkeypatch):
    body = {"models": [{"name": "qwen2.5:7b"}, {"name": "llama3.1:8b"}]}
    monkeypatch.setattr(onboarding.requests, "get", lambda *a, **k: _Resp(200, body))
    hud.run("provider_test", {"provider": "ollama", "model": "qwen2.5:7b"})
    assert hud.sent[0]["ok"] and hud.sent[0]["models"] == ["qwen2.5:7b", "llama3.1:8b"]


def test_provider_save_updates_the_live_brain_and_tells_every_window(hud, monkeypatch):
    monkeypatch.setattr(onboarding.requests, "get", lambda *a, **k: _Resp(200))
    values = {"keys": {"groq": KEY}, "models": {"groq": "llama-3.3-70b-versatile"}, "order": ["groq", "ollama"]}
    hud.run("provider_save", {"values": values})

    result = hud.sent[0]
    assert result["type"] == "provider_result" and result["kind"] == "save" and result["ok"]
    assert [p.name for p in hud.brain.providers] == ["groq", "ollama"]
    assert hud.brain.current.name == "groq"

    (update,) = hud.broadcast
    assert update["type"] == "brain_setup" and update["order"] == ["groq", "ollama"]
    assert [b["name"] for b in update["brains"]] == ["groq", "ollama"]
    assert KEY not in json.dumps([hud.sent, hud.broadcast])  # keys never travel back to the HUD


def test_failed_save_is_reported_and_changes_nothing(hud, monkeypatch):
    monkeypatch.setattr(onboarding.requests, "get", lambda *a, **k: _Resp(403))
    hud.run("provider_save", {"values": {"keys": {"deepseek": KEY}}})
    assert hud.sent[0]["ok"] is False and hud.sent[0]["provider"] == "deepseek"
    assert hud.broadcast == []
    assert [p.name for p in hud.brain.providers] == ["gemini", "ollama"]
    assert config.DEEPSEEK_API_KEY == "" and not config.ENV_FILE.exists()


def test_malformed_payloads_are_handled_not_crashed(hud):
    hud.run("provider_save", {"values": "not a dict"})
    hud.run("provider_save", {"values": {"order": "gemini"}})
    hud.run("provider_test", {})
    assert all(m.get("ok") is False or m.get("type") == "notice" for m in hud.sent)
    assert hud.broadcast == []
