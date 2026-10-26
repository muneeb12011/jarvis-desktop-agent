"""Registry, confirmation rules and pure helpers - no display needed."""

import datetime as dt

from app import config, tools
from app.platform_utils import resolve_path
from app.tools.knowledge import _parse_due
from app.tools.system import _best_match, _confirm_shell


def test_registry_loads_and_specs_are_valid():
    reg = tools.load_all()
    assert {"open_app", "screenshot", "click_element", "run_powershell", "set_reminder", "web_search"} <= set(reg)
    for spec in tools.specs():
        assert spec.parameters["type"] == "object"
        for req in spec.parameters.get("required", []):
            assert req in spec.parameters["properties"], (spec.name, req)


def test_readonly_powershell_runs_without_confirmation():
    assert _confirm_shell({"command": "Get-Process | Select-Object -First 5"}) is None
    assert _confirm_shell({"command": "ipconfig"}) is None


def test_mutating_powershell_requires_confirmation():
    assert _confirm_shell({"command": "Remove-Item C:/temp -Recurse"})
    assert _confirm_shell({"command": "Get-ChildItem; Stop-Process -Name notepad"})
    assert _confirm_shell({"command": "Get-Content a.txt > b.txt"})


def test_confirm_level_off_skips_everything(monkeypatch):
    monkeypatch.setattr(config, "CONFIRM_LEVEL", "off")
    assert tools.get("close_app").confirmation_reason({"name": "chrome"}) is None


def test_strict_mode_confirms_mutating_tools(monkeypatch):
    monkeypatch.setattr(config, "CONFIRM_LEVEL", "strict")
    assert tools.get("click").confirmation_reason({"x": 1, "y": 2})
    assert tools.get("get_datetime").confirmation_reason({}) is None


def test_tool_errors_become_results_not_crashes():
    t = tools.get("read_file")
    res = t.run({"wrong_arg": 1})
    assert not res.ok and "Bad arguments" in res.text


def test_app_name_matching():
    names = ["google chrome", "visual studio code", "spotify", "microsoft teams"]
    assert _best_match("chrome", names) == "google chrome"
    assert _best_match("spotfy", names) == "spotify"
    assert _best_match("zzz", names) is None


def test_reminder_time_parsing():
    now = dt.datetime.now()
    due = _parse_due(10, None)
    assert 9 * 60 < (due - now).total_seconds() < 11 * 60
    due = _parse_due(None, "2030-01-02 09:30")
    assert due == dt.datetime(2030, 1, 2, 9, 30)
    due = _parse_due(None, "5:30 pm")
    assert due.hour == 17 and due.minute == 30 and due > now


def test_resolve_known_folders():
    assert resolve_path("downloads/x.txt").name == "x.txt"
    assert "Downloads" in str(resolve_path("downloads"))


def test_timer_and_settings_roundtrip(tmp_path, monkeypatch):
    from app import memory, settings
    from app.tools.extras import jarvis_settings, set_timer

    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.db")
    res = set_timer(minutes=0, seconds=90, label="tea")
    assert res.ok and res.ui == {"type": "reminders_changed"}
    items = memory.pending_reminders()
    assert items[0]["kind"] == "timer" and items[0]["label"] == "tea"

    before = config.TTS_VOICE
    out = jarvis_settings(voice="en-US-GuyNeural", speed="fast")
    assert config.TTS_VOICE == "en-US-GuyNeural" and config.TTS_RATE == "+18%"
    assert out.ui["type"] == "settings"
    settings.update({"tts_voice": "not-a-voice"})
    assert config.TTS_VOICE == "en-US-GuyNeural"
    settings.update({"tts_voice": before, "tts_rate": "+8%"})
