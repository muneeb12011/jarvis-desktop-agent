"""
Jarvis doctor: checks the parts that depend on this PC (screen reading, audio
devices, speech model, AI key) and prints a plain report.

    venv\\Scripts\\python.exe -m app.selftest        (or double-click check-jarvis.cmd)
"""

from __future__ import annotations

import sys
import time


def _line(ok: bool | None, name: str, detail: str) -> None:
    mark = {True: "OK  ", False: "FAIL", None: "WARN"}[ok]
    print(f"[{mark}] {name}: {detail}", flush=True)


def check_screen() -> None:
    from . import capture, pixel_eyes

    t = time.monotonic()
    f = capture.grab()
    _line(True, "Screen capture", f"{f.original_size[0]}x{f.original_size[1]} in {time.monotonic() - t:.2f}s")
    if f.full is None:
        return
    t = time.monotonic()
    boxes = pixel_eyes.ocr(f.full)
    took = time.monotonic() - t
    words = [b.text for b in boxes]
    _line(
        bool(boxes),
        f"Pixel eyes OCR ({pixel_eyes.engine_name()})",
        f"{len(boxes)} text targets in {took:.2f}s. Sample: {', '.join(words[:12])}",
    )
    t = time.monotonic()
    icons = pixel_eyes.find_icons(f.full, [b.rect for b in boxes])
    _line(
        None if not icons else True,
        "Pixel eyes icons",
        f"{len(icons)} icon-shaped targets in {time.monotonic() - t:.2f}s",
    )


def check_audio(seconds: float = 6.0) -> None:
    import numpy as np

    from . import meeting

    sources, problems = meeting.open_sources()
    for p in problems:
        _line(None, "Meeting audio", p)
    if not sources:
        return
    print(f"      Listening for {seconds:.0f}s (speak, and play something with sound)...", flush=True)
    time.sleep(seconds)
    for s in sources:
        audio = s.drain()
        s.close()
        rms = float(np.sqrt(np.mean(audio**2))) if len(audio) else 0.0
        dev = getattr(s, "device", "?")
        _line(
            len(audio) > 0 or s.name == "Them",
            f"Audio '{s.name}'",
            f"{dev}: {len(audio) / meeting.RATE:.1f}s captured, level {rms:.4f}"
            + ("" if len(audio) or s.name != "Them" else " (nothing was playing, that's fine)"),
        )


def check_whisper() -> None:
    from . import voice

    t = time.monotonic()
    model = voice._load_whisper()
    _line(model is not None, "Speech model", f"loaded in {time.monotonic() - t:.1f}s" if model else "not available")


def check_brain() -> None:
    from . import config, onboarding

    if not config.GEMINI_API_KEY:
        _line(None, "Gemini key", "not set (run the setup wizard)")
        return
    ok, msg = onboarding.test_gemini_key(config.GEMINI_API_KEY)
    _line(ok, "Gemini key", msg)


CHECKS = {"screen": check_screen, "whisper": check_whisper, "brain": check_brain, "audio": check_audio}


def main(argv: list[str]) -> int:
    import faulthandler

    print(f"Jarvis self-test, Python {sys.version.split()[0]}", flush=True)
    for name in argv or list(CHECKS):
        fn = CHECKS.get(name)
        if not fn:
            continue
        # If a check hangs (a driver that never answers), print where it's stuck and move on.
        faulthandler.dump_traceback_later(90, exit=False, file=sys.stdout)
        try:
            fn()
        except Exception as e:
            _line(False, name, f"{type(e).__name__}: {e}")
        finally:
            faulthandler.cancel_dump_traceback_later()
    print("Done.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
