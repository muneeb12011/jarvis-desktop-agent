"""Provider message conversion and failover, with HTTP mocked out."""

import pytest

from app import providers
from app.providers import (
    AssistantTurn,
    Brain,
    GeminiProvider,
    OpenAIProvider,
    ProviderUnavailable,
    ToolCall,
    _extract_inline_tool_call,
    _gemini_schema,
)

HISTORY = [
    {"role": "user", "text": "open notepad"},
    {
        "role": "assistant",
        "text": "",
        "tool_calls": [ToolCall("c1", "open_app", {"name": "notepad"})],
        "raw": None,
        "provider": "ollama",
    },
    {"role": "tool", "tool_call_id": "c1", "name": "open_app", "content": "Launched notepad."},
    {"role": "user", "text": "[Screenshot after open_app]", "image_b64": "QUJD"},
]


def test_gemini_contents_merge_tool_results_and_images():
    contents = GeminiProvider()._contents(HISTORY)
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    call = contents[1]["parts"][0]
    assert call["functionCall"]["name"] == "open_app"
    assert "thoughtSignature" in call  # required by Gemini 3 when not replaying raw
    last = contents[2]["parts"]
    assert "functionResponse" in last[0] and "inline_data" in last[-1]


def test_gemini_replays_raw_parts_verbatim():
    raw = {"role": "model", "parts": [{"functionCall": {"name": "x", "args": {}}, "thoughtSignature": "SIG"}]}
    msgs = [
        {"role": "user", "text": "hi"},
        {"role": "assistant", "text": "", "tool_calls": [], "raw": raw, "provider": "gemini"},
    ]
    assert GeminiProvider()._contents(msgs)[1]["parts"][0]["thoughtSignature"] == "SIG"


def test_gemini_schema_strips_unsupported_keys():
    out = _gemini_schema(
        {"type": "object", "additionalProperties": False, "properties": {"a": {"type": "string", "default": "x"}}}
    )
    assert out == {"type": "object", "properties": {"a": {"type": "string"}}}


def test_openai_messages_shape():
    msgs = OpenAIProvider()._messages("sys", HISTORY)
    assert msgs[0]["role"] == "system"
    assert msgs[2]["tool_calls"][0]["function"]["arguments"] == '{"name": "notepad"}'
    assert msgs[3] == {"role": "tool", "tool_call_id": "c1", "content": "Launched notepad."}
    assert msgs[4]["content"][1]["type"] == "image_url"


def test_inline_tool_call_recovery():
    tc = _extract_inline_tool_call('Sure! {"name": "open_app", "parameters": {"name": "calc"}}', {"open_app"})
    assert tc and tc.name == "open_app" and tc.args == {"name": "calc"}
    assert _extract_inline_tool_call('{"name": "rm_rf"}', {"open_app"}) is None


class _Fake:
    def __init__(self, name, behaviour):
        self.name, self.behaviour, self.configured, self.supports_vision, self.model = name, behaviour, True, True, "m"

    def complete(self, system, messages, tools):
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        return AssistantTurn(self.behaviour, provider=self.name)


def test_brain_fails_over_and_benches():
    brain = Brain(["ollama"])
    brain.providers = [_Fake("gemini", ProviderUnavailable("429", cooldown=60)), _Fake("ollama", "hello")]
    assert brain.complete("s", [], []).text == "hello"
    assert [p.name for p in brain.available()] == ["ollama"]  # gemini benched


def test_brain_raises_when_all_down():
    brain = Brain(["ollama"])
    brain.providers = [_Fake("a", ProviderUnavailable("down"))]
    with pytest.raises(providers.ProviderError):
        brain.complete("s", [], [])
