"""
Voice I/O.

Speech-to-text:
  - faster-whisper running locally (pip install -r requirements-voice.txt).
    Private, works offline, ~0.5-1.5s per sentence on a normal CPU with base.en.
  - Gemini audio transcription as a fallback when whisper isn't installed.
  Chromium's webkitSpeechRecognition (what v2 used) does NOT work inside
  Electron - it needs Google API keys only Chrome ships with - which is why
  the mic in v2 never really worked.

Text-to-speech:
  - edge-tts neural voices (free, needs internet). en-GB-RyanNeural is the
    closest thing to the films. The HUD falls back to the OS voice if this fails.
"""

from __future__ import annotations

import base64
import io
import logging
import re
import threading
import wave

import requests

from . import config

logger = logging.getLogger(__name__)

_whisper_model = None
_whisper_lock = threading.Lock()
_whisper_failed = False


def whisper_available() -> bool:
    if config.STT_ENGINE == "gemini" or _whisper_failed:
        return False
    try:
        import faster_whisper  # noqa: F401

        return True
    except ImportError:
        return False


def _load_whisper():
    global _whisper_model, _whisper_failed
    with _whisper_lock:
        if _whisper_model is None and not _whisper_failed:
            try:
                from faster_whisper import WhisperModel

                logger.info("Loading Whisper model '%s' (first run downloads it)...", config.WHISPER_MODEL)
                _whisper_model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
                logger.info("Whisper ready.")
            except Exception:
                logger.exception("Couldn't load faster-whisper; falling back to Gemini STT")
                _whisper_failed = True
    return _whisper_model


def preload() -> None:
    if whisper_available():
        threading.Thread(target=_load_whisper, daemon=True).start()


def stt_engine_name() -> str:
    if whisper_available():
        return f"whisper:{config.WHISPER_MODEL}"
    if config.GEMINI_API_KEY:
        return "gemini"
    return "none"


def _wav_duration(wav_bytes: bytes) -> float:
    try:
        with wave.open(io.BytesIO(wav_bytes)) as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 0.0


# Whisper hallucinates these on silence/noise; never treat them as speech.
_HALLUCINATIONS = {
    "",
    "you",
    "thank you",
    "thanks for watching",
    "thank you for watching",
    "bye",
    "subtitles by the amara.org community",
    ".",
    "okay",
}


def transcribe(wav_bytes: bytes) -> str:
    if _wav_duration(wav_bytes) < 0.3:
        return ""
    text = ""
    if whisper_available():
        model = _load_whisper()
        if model is not None:
            segments, _info = model.transcribe(
                io.BytesIO(wav_bytes),
                language="en",
                beam_size=1,
                vad_filter=True,
                initial_prompt="Jarvis, open Chrome. Hey Jarvis, what's the weather?",
                condition_on_previous_text=False,
            )
            text = " ".join(s.text for s in segments).strip()
    elif config.GEMINI_API_KEY:
        text = _gemini_transcribe(wav_bytes)
    else:
        raise RuntimeError("No speech-to-text engine: install requirements-voice.txt or set GEMINI_API_KEY.")
    if re.sub(r"[^\w ]", "", text.lower()).strip() in _HALLUCINATIONS:
        return ""
    return text


def _gemini_transcribe(wav_bytes: bytes) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": "Transcribe this audio exactly as spoken, in English. Output only the words. "
                        "If there is no clear speech, output nothing."
                    },
                    {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav_bytes).decode()}},
                ],
            }
        ],
    }
    resp = requests.post(url, json=payload, headers={"x-goog-api-key": config.GEMINI_API_KEY}, timeout=30)
    if not resp.ok:
        raise RuntimeError(f"Gemini STT error {resp.status_code}")
    parts = resp.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()


# ------------------------------------------------------------------ TTS


def clean_for_speech(text: str) -> str:
    text = re.sub(r"```.*?```", " I've put the code on screen. ", text, flags=re.DOTALL)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_#`>|]", "", text)
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)
    text = re.sub(r"^\s*[-•]\s*", "", text, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", text).strip()[:1200]


async def synthesize(text: str, voice: str | None = None, rate: str | None = None) -> bytes:
    import edge_tts

    spoken = clean_for_speech(text)
    if not spoken:
        return b""
    communicate = edge_tts.Communicate(spoken, voice or config.TTS_VOICE, rate=rate or config.TTS_RATE)
    buf = bytearray()
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio":
            buf.extend(chunk["data"])
    return bytes(buf)
