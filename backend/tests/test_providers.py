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


# ---------------------------------------------------------------- Claude (Anthropic)


def _anthropic():
    from app.providers import AnthropicProvider

    return AnthropicProvider()


def test_anthropic_messages_shape_and_tool_result_order():
    msgs = _anthropic()._messages(HISTORY)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"][0] == {"type": "tool_use", "id": "c1", "name": "open_app", "input": {"name": "notepad"}}
    last = msgs[2]["content"]
    assert last[0]["type"] == "tool_result" and last[0]["tool_use_id"] == "c1"
    assert last[1]["type"] == "image" and last[2]["type"] == "text"


def test_anthropic_answers_dangling_tool_use():
    history = [
        {"role": "user", "text": "do it"},
        {"role": "assistant", "text": "", "tool_calls": [ToolCall("c9", "click", {})], "provider": "gemini"},
        {"role": "user", "text": "never mind"},
    ]
    msgs = _anthropic()._messages(history)
    blocks = msgs[2]["content"]
    assert blocks[0]["type"] == "tool_result" and blocks[0]["tool_use_id"] == "c9"
    assert blocks[-1] == {"type": "text", "text": "never mind"}


def test_anthropic_drops_orphan_tool_result_and_starts_with_user():
    history = [
        {"role": "tool", "tool_call_id": "gone", "name": "x", "content": "old"},
        {"role": "assistant", "text": "hello", "tool_calls": []},
        {"role": "user", "text": "hi"},
    ]
    msgs = _anthropic()._messages(history)
    assert msgs[0]["role"] == "user"
    assert all(b["type"] != "tool_result" for m in msgs for b in m["content"])
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]


def test_anthropic_sanitises_foreign_tool_ids():
    history = [
        {"role": "user", "text": "go"},
        {"role": "assistant", "text": "", "tool_calls": [ToolCall("weird id!:1", "open_app", {})]},
        {"role": "tool", "tool_call_id": "weird id!:1", "name": "open_app", "content": "ok"},
    ]
    msgs = _anthropic()._messages(history)
    use_id = msgs[1]["content"][0]["id"]
    assert use_id == msgs[2]["content"][0]["tool_use_id"] == "weird_id__1"


def test_anthropic_complete_parses_tool_use(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test")
    seen = {}

    class Resp:
        status_code, ok, headers, text = 200, True, {}, ""

        def json(self):
            return {
                "model": "claude-test",
                "content": [
                    {"type": "text", "text": "Opening it."},
                    {"type": "tool_use", "id": "toolu_1", "name": "open_app", "input": {"name": "calc"}},
                ],
            }

    def fake_post(url, payload, headers=None, timeout=None):
        seen.update(url=url, payload=payload, headers=headers)
        return Resp()

    monkeypatch.setattr(providers, "_post", fake_post)
    spec = providers.ToolSpec("open_app", "Open an app", {"type": "object", "properties": {"name": {"type": "string"}}})
    turn = _anthropic().complete("sys", [{"role": "user", "text": "open calc"}], [spec])
    assert turn.text == "Opening it." and turn.provider == "anthropic" and turn.model == "claude-test"
    assert turn.tool_calls == [ToolCall("toolu_1", "open_app", {"name": "calc"})]
    assert seen["url"].endswith("/v1/messages")
    assert seen["headers"]["x-api-key"] == "sk-ant-test" and "anthropic-version" in seen["headers"]
    assert seen["payload"]["system"] == "sys" and seen["payload"]["tools"][0]["input_schema"]["type"] == "object"


@pytest.mark.parametrize("status", [429, 529])
def test_anthropic_rate_limit_fails_over(monkeypatch, status):
    from app import config

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test")

    class Resp:
        status_code, ok, headers, text = status, False, {"retry-after": "42"}, "slow down"

    monkeypatch.setattr(providers, "_post", lambda *a, **k: Resp())
    with pytest.raises(ProviderUnavailable) as err:
        _anthropic().complete("s", [{"role": "user", "text": "hi"}], [])
    assert err.value.cooldown == 42


def test_anthropic_bad_key_is_benched_not_fatal(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "bad")

    class Resp:
        status_code, ok, headers, text = 401, False, {}, "invalid x-api-key"

    monkeypatch.setattr(providers, "_post", lambda *a, **k: Resp())
    with pytest.raises(ProviderUnavailable):
        _anthropic().complete("s", [{"role": "user", "text": "hi"}], [])


# ---------------------------------------------------------------- OpenAI-compatible presets


def test_presets_read_their_own_settings(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.setattr(config, "GROQ_API_KEY", "")
    ds, groq = providers.DeepSeekProvider(), providers.GroqProvider()
    assert (ds.name, ds.model, ds.configured, ds.supports_vision) == ("deepseek", config.DEEPSEEK_MODEL, True, False)
    assert ds.base == config.DEEPSEEK_BASE_URL
    assert groq.configured is False  # no key -> skipped by Brain instead of erroring


def test_unconfigured_preset_raises_unavailable():
    from app import config

    assert not config.OPENROUTER_API_KEY  # test environment has no keys
    with pytest.raises(ProviderUnavailable):
        providers.OpenRouterProvider().complete("s", [], [])


def test_openai_compat_sends_bearer_to_its_own_endpoint(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_test")
    seen = {}

    class Resp:
        status_code, ok, headers, text = 200, True, {}, ""

        def json(self):
            return {"choices": [{"message": {"content": "hi"}}]}

    def fake_post(url, payload, headers=None, timeout=None):
        seen.update(url=url, headers=headers, model=payload["model"])
        return Resp()

    monkeypatch.setattr(providers, "_post", fake_post)
    turn = providers.GroqProvider().complete("s", [{"role": "user", "text": "hi"}], [])
    assert turn.text == "hi" and turn.provider == "groq"
    assert seen["url"] == f"{config.GROQ_BASE_URL}/chat/completions"
    assert seen["headers"] == {"Authorization": "Bearer gsk_test"}


# ---------------------------------------------------------------- registry and failover across providers


def test_brain_builds_every_provider_and_accepts_aliases():
    brain = Brain(["claude", "deepseek", "groq", "openrouter", "gpt", "gemini", "ollama", "claude", "nope"])
    assert [p.name for p in brain.providers] == [
        "anthropic",
        "deepseek",
        "groq",
        "openrouter",
        "openai",
        "gemini",
        "ollama",
    ]
    assert all("label" in s for s in brain.status())


def test_brain_skips_unconfigured_and_fails_over_between_new_providers():
    brain = Brain(["ollama"])
    claude, deepseek, local = (
        _Fake("anthropic", ProviderUnavailable("529", cooldown=30)),
        _Fake("deepseek", "from deepseek"),
        _Fake("ollama", "from local"),
    )
    deepseek.configured = True
    brain.providers = [claude, deepseek, local]
    assert brain.complete("s", [], []).text == "from deepseek"
    assert brain.last_provider == "deepseek"
