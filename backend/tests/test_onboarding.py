"""First-run wizard: .env editing, key checks, applying answers live."""

import pytest

from app import config, memory, onboarding


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.db")
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    for attr in ("GEMINI_API_KEY", "OPENAI_API_KEY", "USER_NAME", "USER_TITLE", "HOME_CITY"):
        monkeypatch.setattr(config, attr, "")


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
