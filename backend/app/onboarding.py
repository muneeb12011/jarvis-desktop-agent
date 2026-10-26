"""
First-run setup: what the HUD's welcome wizard reads and writes.

Keys and identity live in the .env file in the data folder (so they survive
updates); the wizard writes them there and applies them immediately, without
a restart.
"""

from __future__ import annotations

import logging
import os
import re

import requests

from . import config, memory

logger = logging.getLogger(__name__)

_ENV_KEYS = {
    "gemini_key": ("GEMINI_API_KEY", "GEMINI_API_KEY"),
    "user_name": ("JARVIS_USER_NAME", "USER_NAME"),
    "user_title": ("JARVIS_USER_TITLE", "USER_TITLE"),
    "city": ("JARVIS_CITY", "HOME_CITY"),
}


def is_done() -> bool:
    flag = memory.kv_get("setup_done")
    if flag is not None:
        return flag == "1"
    # Installs from before the wizard existed: a working key means they're set up already.
    return bool(config.GEMINI_API_KEY or config.OPENAI_API_KEY)


def state() -> dict:
    key = config.GEMINI_API_KEY
    return {
        "done": is_done(),
        "has_key": bool(key),
        "key_hint": f"...{key[-4:]}" if len(key) > 8 else "",
        "user_name": config.USER_NAME,
        "user_title": config.USER_TITLE,
        "city": config.HOME_CITY,
    }


def write_env(updates: dict[str, str], path=None) -> None:
    """Set KEY=value lines in the .env file, keeping every other line and comment as it was."""
    path = path or config.ENV_FILE
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out = []
    for line in lines:
        m = re.match(r"\s*([A-Z0-9_]+)\s*=", line)
        if m and m.group(1) in remaining:
            out.append(f"{m.group(1)}={remaining.pop(m.group(1))}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in remaining.items()]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def test_gemini_key(key: str) -> tuple[bool, str]:
    key = key.strip()
    if not re.fullmatch(r"[\w.\-]{20,200}", key):
        return False, "That doesn't look like a Gemini API key. Copy it again from AI Studio."
    try:
        r = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=5",
            headers={"x-goog-api-key": key},
            timeout=12,
        )
    except requests.RequestException as e:
        return False, f"Couldn't reach Google to check the key ({type(e).__name__}). Check your internet."
    if r.status_code == 200:
        return True, "Key works."
    if r.status_code in (400, 401, 403):
        return False, "Google rejected that key. Copy it again from AI Studio."
    return False, f"Google answered {r.status_code}; try again in a minute."


def save(values: dict, brain=None) -> dict:
    """Apply wizard answers. Returns {"ok", "message", "state"}."""
    env_updates: dict[str, str] = {}
    if values.get("gemini_key"):
        ok, msg = test_gemini_key(str(values["gemini_key"]))
        if not ok:
            return {"ok": False, "message": msg, "state": state()}
    for field, (env_name, attr) in _ENV_KEYS.items():
        if field in values and values[field] is not None:
            v = str(values[field]).strip().replace("\n", " ")
            if field == "gemini_key" and not v:
                continue
            env_updates[env_name] = v
            setattr(config, attr, v)
            os.environ[env_name] = v
    if env_updates:
        write_env(env_updates)
        if "user_title" in values or "city" in values or "user_name" in values:
            from . import settings

            settings.update({k: str(values[k]) for k in ("user_title", "city", "user_name") if k in values})
    if values.get("gemini_key") and brain is not None:
        brain.reset()
    if values.get("done"):
        memory.kv_set("setup_done", "1")
    elif values.get("restart"):
        memory.kv_set("setup_done", "0")
    return {"ok": True, "message": "Saved.", "state": state()}
