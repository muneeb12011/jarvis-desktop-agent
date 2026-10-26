"""
Central configuration. Every knob lives here and is read from backend/.env,
so no other module calls os.environ directly.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
# Installed builds keep the database, logs, keys and plugins in %APPDATA%\Jarvis (JARVIS_DATA_DIR) so an
# update or reinstall never touches them. A source checkout keeps everything inside backend/ as before.
DATA_DIR = Path(os.environ["JARVIS_DATA_DIR"]) if os.environ.get("JARVIS_DATA_DIR") else BACKEND_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
ENV_FILE = DATA_DIR / ".env"
load_dotenv(ENV_FILE)
if DATA_DIR != BACKEND_DIR:
    load_dotenv(BACKEND_DIR / ".env")  # defaults shipped with the app; the user's file wins


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name, "")
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _get_int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


# ---------- Identity ----------
USER_NAME = _get("JARVIS_USER_NAME", "")  # e.g. "Muneeb"
USER_TITLE = _get("JARVIS_USER_TITLE", "sir")  # how Jarvis addresses you
HOME_CITY = _get("JARVIS_CITY", "")  # default city for weather

# ---------- Brain ----------
# Comma-separated, tried in order. If one is rate-limited or down, the next
# one takes over mid-conversation. Options: gemini, openai, ollama
LLM_PROVIDERS = [p.strip() for p in _get("LLM_PROVIDERS", "gemini,ollama").split(",") if p.strip()]

GEMINI_API_KEY = _get("GEMINI_API_KEY")
GEMINI_MODEL = _get("GEMINI_MODEL", "gemini-2.5-flash")
# Extra models to rotate through when one hits its daily free quota (others are auto-discovered).
GEMINI_FALLBACK_MODELS = [m.strip() for m in _get("GEMINI_FALLBACK_MODELS", "").split(",") if m.strip()]

# Any OpenAI-compatible endpoint: OpenAI, Groq, OpenRouter, LM Studio, etc.
OPENAI_API_KEY = _get("OPENAI_API_KEY")
OPENAI_BASE_URL = _get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_MODEL = _get("OPENAI_MODEL", "gpt-4o-mini")

OLLAMA_HOST = _get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
# Must be a model with tool-calling support (llama3.1, qwen2.5, qwen3...).
OLLAMA_TEXT_MODEL = _get("OLLAMA_TEXT_MODEL", "qwen2.5:7b")
# Optional vision model used only to describe screenshots when Ollama is the
# active brain (qwen2.5vl, llava, moondream...).
OLLAMA_VISION_MODEL = _get("OLLAMA_VISION_MODEL", "qwen2.5vl:3b")

LLM_TIMEOUT_SECONDS = _get_int("LLM_TIMEOUT_SECONDS", 60)

# ---------- Agent ----------
MAX_AGENT_STEPS = _get_int("JARVIS_MAX_STEPS", 30)
# strict = confirm every action that changes something
# normal = confirm risky things (shell writes, closing apps, overwriting files)
# off    = never ask (not recommended)
CONFIRM_LEVEL = _get("JARVIS_CONFIRM_LEVEL", "normal").lower()
# auto = after tasks that changed something, a second pass checks it really worked; off = never
VERIFY = _get("JARVIS_VERIFY", "auto").lower()
CONFIRM_TIMEOUT_SECONDS = _get_int("JARVIS_CONFIRM_TIMEOUT", 60)

# ---------- Voice ----------
# STT: "whisper" (local faster-whisper, needs requirements-voice.txt),
#      "gemini" (sends audio to Gemini), or "auto" (whisper if installed).
STT_ENGINE = _get("JARVIS_STT", "auto").lower()
WHISPER_MODEL = _get("JARVIS_WHISPER_MODEL", "base.en")
# TTS: edge-tts neural voices. en-GB-RyanNeural is the closest to the films.
TTS_VOICE = _get("JARVIS_TTS_VOICE", "en-GB-RyanNeural")
TTS_RATE = _get("JARVIS_TTS_RATE", "+8%")
TTS_ENABLED = _get_bool("JARVIS_TTS", True)

# ---------- Security ----------
AUTH_TOKEN = _get("JARVIS_AUTH_TOKEN")

# ---------- Storage ----------
MEMORY_DB_PATH = DATA_DIR / "memory.db"
LOG_DB_PATH = DATA_DIR / "agent_log.db"
LOG_LEVEL = _get("LOG_LEVEL", "INFO").upper()


def active_assistant_name() -> str:
    return "Jarvis"


def address() -> str:
    """How Jarvis addresses the user in speech."""
    return USER_TITLE or USER_NAME or "sir"
