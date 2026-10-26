"""
Runtime settings you can change from the HUD's settings sheet (or by asking
Jarvis). Stored in memory.db so they survive restarts; .env stays the place
for keys and defaults.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from . import config, memory
from .platform_utils import IS_WINDOWS

logger = logging.getLogger(__name__)

VOICES = [
    {"id": "en-GB-RyanNeural", "label": "Ryan, British"},
    {"id": "en-GB-ThomasNeural", "label": "Thomas, British"},
    {"id": "en-US-GuyNeural", "label": "Guy, American"},
    {"id": "en-US-AndrewNeural", "label": "Andrew, American"},
    {"id": "en-AU-WilliamNeural", "label": "William, Australian"},
    {"id": "en-IN-PrabhatNeural", "label": "Prabhat, Indian"},
    {"id": "en-GB-SoniaNeural", "label": "Sonia, British"},
    {"id": "en-US-AriaNeural", "label": "Aria, American"},
]
RATES = {"slower": "-10%", "normal": "+0%", "brisk": "+8%", "fast": "+18%"}
CONFIRM_LEVELS = ("strict", "normal", "off")

# setting key -> config attribute
_MAP = {
    "tts_voice": "TTS_VOICE",
    "tts_rate": "TTS_RATE",
    "confirm_level": "CONFIRM_LEVEL",
    "user_title": "USER_TITLE",
    "city": "HOME_CITY",
    "user_name": "USER_NAME",
}


def _stored() -> dict:
    try:
        return json.loads(memory.kv_get("settings", "{}") or "{}")
    except json.JSONDecodeError:
        return {}


def load() -> None:
    """Apply saved settings over the .env defaults. Called at startup."""
    for key, value in _stored().items():
        attr = _MAP.get(key)
        if attr and isinstance(value, str):
            setattr(config, attr, value)


def current() -> dict:
    return {
        "tts_voice": config.TTS_VOICE,
        "tts_rate": config.TTS_RATE,
        "confirm_level": config.CONFIRM_LEVEL,
        "user_title": config.USER_TITLE,
        "city": config.HOME_CITY,
        "user_name": config.USER_NAME,
        "startup": startup_enabled(),
        "voices": VOICES,
        "rates": RATES,
    }


def update(changes: dict) -> dict:
    stored = _stored()
    for key, value in changes.items():
        if key == "startup":
            set_startup(bool(value))
            continue
        if key not in _MAP or not isinstance(value, str):
            continue
        value = value.strip()
        if key == "tts_voice" and value not in {v["id"] for v in VOICES}:
            continue
        if key == "tts_rate" and value not in RATES.values():
            continue
        if key == "confirm_level" and value not in CONFIRM_LEVELS:
            continue
        setattr(config, _MAP[key], value)
        stored[key] = value
    memory.kv_set("settings", json.dumps(stored))
    return current()


# ---------------------------------------------------------------- start with Windows


def _startup_file() -> Path | None:
    if not IS_WINDOWS:
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup/Jarvis.vbs"


def startup_enabled() -> bool:
    f = _startup_file()
    return bool(f and f.exists())


def set_startup(enabled: bool) -> bool:
    f = _startup_file()
    if not f:
        return False
    if enabled:
        app_exe = os.environ.get("JARVIS_APP_EXE", "")
        if app_exe:  # installed build: the app starts its own backend
            f.write_text(
                f'Set sh = CreateObject("WScript.Shell")\r\nsh.Run """{app_exe}""", 1, False\r\n', encoding="utf-8"
            )
            logger.info("Start with Windows: %s", enabled)
            return startup_enabled()
        bat = config.BACKEND_DIR.parent / "start-jarvis.bat"
        # Run the launcher with its console hidden; the HUD window still appears.
        f.write_text(
            'Set sh = CreateObject("WScript.Shell")\r\n'
            f'sh.CurrentDirectory = "{bat.parent}"\r\n'
            f'sh.Run """{bat}"" silent", 0, False\r\n',
            encoding="utf-8",
        )
    elif f.exists():
        f.unlink()
    logger.info("Start with Windows: %s", enabled)
    return startup_enabled()
