"""First-run wizard: .env editing, key checks, applying answers live."""

import os

import pytest

from app import config, memory, onboarding, providers


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.db")
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    for attr in (
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
        "USER_NAME",
        "USER_TITLE",
        "HOME_CITY",
    ):
        monkeypatch.setattr(config, attr, "")
    monkeypatch.setattr(config, "LLM_PROVIDERS", ["gemini", "ollama"])
    monkeypatch.setattr(os, "environ", os.environ.copy())  # save() writes env vars; keep them out of other tests


def test_write_env_keeps_other_lines(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# keys\nJARVIS_AUTH_TOKEN=abc\nGEMINI_API_KEY=old\n", encoding="utf-8")
    onboarding.write_env({"GEMINI_API_KEY": "new", "JARVIS_CITY": "Lahore"}, f)
    assert f.read_text(encoding="utf-8") == "# keys\nJARVIS_AUTH_TOKEN=abc\nGEMINI_API_KEY=new\nJARVIS_CITY=Lahore\n"


def test_done_flag_and_legacy_installs(monkeypatch):
    assert not onboarding.is_done()
    monkeypatch.setattr(config, "GEMINI_API_KEY", "AIza" + "x" * 35)
    assert onboarding.is_done()  # existing install with a key: don't nag
    memory.kv_set("setup_done", "0")
    assert not onboarding.is_done()  # "run setup again"


def test_bad_key_is_rejected_without_network():
    ok, msg = onboarding.test_gemini_key("hello")
    assert not ok and "doesn't look like" in msg
    res = onboarding.save({"gemini_key": "nope", "done": True})
    assert not res["ok"] and not onboarding.is_done()


def test_save_applies_live(monkeypatch):
    monkeypatch.setattr(onboarding, "test_gemini_key", lambda k: (True, "Key works."))
    reset = []

    class FakeBrain:
        def reset(self):
            reset.append(True)

    res = onboarding.save(
        {
            "gemini_key": "AIza" + "k" * 35,
            "user_name": "Muneeb",
            "user_title": "Muneeb",
            "city": "Lahore",
            "done": True,
        },
        FakeBrain(),
    )
    assert res["ok"] and reset and onboarding.is_done()
    assert config.GEMINI_API_KEY.startswith("AIza") and config.HOME_CITY == "Lahore"
    text = config.ENV_FILE.read_text(encoding="utf-8")
    assert "JARVIS_USER_NAME=Muneeb" in text and "GEMINI_API_KEY=AIza" in text
    assert res["state"]["key_hint"].startswith("...")


# ---------------------------------------------------------------- choosing a brain


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body


def _fake_get(monkeypatch, resp, seen=None):
    def get(url, headers=None, timeout=None):
        if seen is not None:
            seen.append((url, headers or {}))
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(onboarding.requests, "get", get)


KEY = "k" * 30


def test_state_never_exposes_a_full_key_and_lists_enabled_first(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_" + "s" * 30)
    monkeypatch.setattr(config, "LLM_PROVIDERS", ["groq", "ollama"])
    state = onboarding.providers_state()
    assert state["order"] == ["groq", "ollama"]
    assert [r["id"] for r in state["providers"][:2]] == ["groq", "ollama"]
    assert all(not r["enabled"] for r in state["providers"][2:])
    groq = state["providers"][0]
    assert groq["has_key"] and groq["key_hint"] == "...ssss" and "gsk_" not in str(state)
    assert {r["id"] for r in state["providers"]} == {
        "gemini",
        "groq",
        "openrouter",
        "deepseek",
        "anthropic",
        "openai",
        "ollama",
    }


def test_state_understands_aliases_and_ignores_unknown(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDERS", ["claude", "nope", "claude", "local"])
    assert onboarding.providers_state()["order"] == ["anthropic", "ollama"]


def test_test_provider_checks_each_service_the_right_way(monkeypatch):
    seen: list = []
    _fake_get(monkeypatch, _Resp(200), seen)
    assert onboarding.test_provider("anthropic", KEY)[0]
    assert seen[-1][0].startswith("https://api.anthropic.com/v1/models") and seen[-1][1]["x-api-key"] == KEY
    assert onboarding.test_provider("groq", KEY)[0]
    assert seen[-1][0] == f"{config.GROQ_BASE_URL}/models" and seen[-1][1]["Authorization"] == f"Bearer {KEY}"
    assert onboarding.test_provider("deepseek", KEY)[0]
    assert seen[-1][0] == f"{config.DEEPSEEK_BASE_URL}/models"
    assert onboarding.test_provider("openrouter", KEY)[0]
    assert seen[-1][0] == f"{config.OPENROUTER_BASE_URL}/auth/key"  # /models is public and can't reject a bad key


def test_test_provider_messages(monkeypatch):
    _fake_get(monkeypatch, _Resp(401))
    ok, msg, _ = onboarding.test_provider("anthropic", KEY)
    assert not ok and "rejected" in msg and "Claude" in msg
    _fake_get(monkeypatch, _Resp(429))
    assert onboarding.test_provider("groq", KEY)[0]  # rate-limited means the key is valid
    _fake_get(monkeypatch, _Resp(500))
    assert not onboarding.test_provider("groq", KEY)[0]
    _fake_get(monkeypatch, onboarding.requests.ConnectionError("down"))
    ok, msg, _ = onboarding.test_provider("groq", KEY)
    assert not ok and "internet" in msg


def test_test_provider_rejects_junk_without_network(monkeypatch):
    _fake_get(monkeypatch, AssertionError("must not touch the network"))
    assert "Paste" in onboarding.test_provider("anthropic", "")[1]
    assert "doesn't look like" in onboarding.test_provider("anthropic", "has spaces in it")[1]
    assert not onboarding.test_provider("nope", KEY)[0]
    assert not onboarding.test_provider("openai", KEY, base="ftp://x")[0]


def test_test_provider_uses_the_saved_key_when_blank(monkeypatch):
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "saved" + "x" * 20)
    seen: list = []
    _fake_get(monkeypatch, _Resp(200), seen)
    assert onboarding.test_provider("deepseek", "")[0]
    assert seen[-1][1]["Authorization"].endswith("x" * 20)


def test_openai_compatible_local_server_needs_no_key(monkeypatch):
    seen: list = []
    _fake_get(monkeypatch, _Resp(200), seen)
    assert onboarding.test_provider("openai", "", base="http://localhost:1234/v1")[0]
    assert seen[-1] == ("http://localhost:1234/v1/models", {})
    assert not onboarding.test_provider("openai", "", base="https://api.openai.com/v1")[0]


def test_ollama_checks(monkeypatch):
    models = {"models": [{"name": "qwen2.5:7b"}, {"name": "llama3.1:latest"}]}
    _fake_get(monkeypatch, _Resp(200, models))
    ok, msg, names = onboarding.test_provider("ollama", model="qwen2.5:7b")
    assert ok and names == ["qwen2.5:7b", "llama3.1:latest"] and "2 models" in msg
    assert "isn't installed" not in onboarding.test_provider("ollama", model="llama3.1")[1]  # matches llama3.1:latest
    ok, msg, _ = onboarding.test_provider("ollama", model="mistral")
    assert ok and "isn't installed" in msg and "ollama pull mistral" in msg
    _fake_get(monkeypatch, _Resp(200, {"models": []}))
    ok, msg, _ = onboarding.test_provider("ollama", model="qwen2.5:7b")
    assert not ok and "ollama pull" in msg
    _fake_get(monkeypatch, onboarding.requests.ConnectionError("refused"))
    ok, msg, _ = onboarding.test_provider("ollama")
    assert not ok and "isn't answering" in msg


def test_bad_key_saves_nothing(monkeypatch):
    _fake_get(monkeypatch, _Resp(401))
    res = onboarding.save_providers({"keys": {"anthropic": KEY}, "models": {"anthropic": "claude-haiku-5-5"}})
    assert not res["ok"] and res["provider"] == "anthropic" and "rejected" in res["message"]
    assert config.ANTHROPIC_API_KEY == "" and not config.ENV_FILE.exists()
    assert os.environ.get("ANTHROPIC_API_KEY") is None and config.LLM_PROVIDERS == ["gemini", "ollama"]


def test_good_key_is_saved_applied_and_enabled(monkeypatch):
    _fake_get(monkeypatch, _Resp(200))
    res = onboarding.save_providers({"keys": {"anthropic": KEY}, "models": {"anthropic": "claude-haiku-5-5"}})
    assert res["ok"] and res["order"] == ["gemini", "ollama", "anthropic"]  # a new key switches the provider on
    assert config.ANTHROPIC_API_KEY == KEY and config.ANTHROPIC_MODEL == "claude-haiku-5-5"
    assert os.environ["ANTHROPIC_API_KEY"] == KEY
    text = config.ENV_FILE.read_text(encoding="utf-8")
    assert f"ANTHROPIC_API_KEY={KEY}" in text and "ANTHROPIC_MODEL=claude-haiku-5-5" in text
    assert "LLM_PROVIDERS=gemini,ollama,anthropic" in text
    row = next(r for r in res["providers"] if r["id"] == "anthropic")
    assert row["enabled"] and row["has_key"] and row["model"] == "claude-haiku-5-5" and KEY not in str(res)


def test_save_rebuilds_the_live_brain_in_the_new_order(monkeypatch):
    _fake_get(monkeypatch, _Resp(200))
    brain = providers.Brain(["gemini", "ollama"])
    brain._benched_until["gemini"] = 9e12
    res = onboarding.save_providers(
        {"keys": {"groq": KEY, "deepseek": KEY}, "order": ["groq", "deepseek", "ollama"]}, brain
    )
    assert res["ok"]
    assert [p.name for p in brain.providers] == ["groq", "deepseek", "ollama"]
    assert brain._benched_until == {}
    assert [p.name for p in brain.available()] == ["groq", "deepseek", "ollama"]
    assert brain.providers[0].api_key == KEY  # the rebuilt provider already sees the new key


def test_order_is_the_on_off_switch(monkeypatch):
    res = onboarding.save_providers({"order": ["ollama"]})
    assert res["ok"] and res["order"] == ["ollama"] and config.LLM_PROVIDERS == ["ollama"]
    assert not next(r for r in res["providers"] if r["id"] == "gemini")["enabled"]
    assert "LLM_PROVIDERS=ollama" in config.ENV_FILE.read_text(encoding="utf-8")


def test_cannot_switch_every_brain_off_or_use_unknown_ones():
    assert not onboarding.save_providers({"order": []})["ok"]
    assert not onboarding.save_providers({"order": ["gemini", "skynet"]})["ok"]
    assert not onboarding.save_providers({"keys": {"skynet": KEY}})["ok"]
    assert not onboarding.save_providers({"clear_keys": ["skynet"]})["ok"]
    assert config.LLM_PROVIDERS == ["gemini", "ollama"] and not config.ENV_FILE.exists()


def test_rejects_unsafe_model_and_address():
    assert not onboarding.save_providers({"models": {"groq": "bad model; rm -rf"}})["ok"]
    assert not onboarding.save_providers({"models": {"groq": "x\nINJECTED=1"}})["ok"]
    assert not onboarding.save_providers({"bases": {"openai": "javascript:alert(1)"}})["ok"]
    assert not config.ENV_FILE.exists()


def test_blank_key_keeps_the_saved_one_and_clear_removes_it(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_" + "o" * 30)
    res = onboarding.save_providers({"keys": {"groq": ""}, "models": {"groq": "llama-3.1-8b-instant"}})
    assert res["ok"] and config.GROQ_API_KEY.startswith("gsk_") and config.GROQ_MODEL == "llama-3.1-8b-instant"
    res = onboarding.save_providers({"clear_keys": ["groq"], "order": ["gemini", "ollama"]})
    assert res["ok"] and config.GROQ_API_KEY == ""
    assert not next(r for r in res["providers"] if r["id"] == "groq")["has_key"]


def test_only_openai_and_ollama_addresses_are_editable():
    res = onboarding.save_providers(
        {
            "bases": {
                "openai": "http://localhost:1234/v1/",
                "ollama": "http://192.168.1.5:11434",
                "groq": "https://evil.example/v1",
            }
        }
    )
    assert res["ok"]
    assert config.OPENAI_BASE_URL == "http://localhost:1234/v1" and config.OLLAMA_HOST == "http://192.168.1.5:11434"
    assert "evil" not in config.GROQ_BASE_URL


def test_warns_about_enabled_providers_without_a_key():
    res = onboarding.save_providers({"order": ["gemini", "anthropic", "ollama"]})
    assert res["ok"] and "Gemini" in res["message"] and "Claude" in res["message"] and "Ollama" not in res["message"]


def test_any_brain_key_counts_as_set_up_for_old_installs(monkeypatch):
    assert not onboarding.is_done()
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", KEY)
    assert onboarding.is_done()
