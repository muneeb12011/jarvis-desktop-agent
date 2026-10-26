"""
Actuator: the only module allowed to touch the real mouse and keyboard.
Every input goes through here so it's easy to audit or gate.

pyautogui is imported lazily because merely importing it needs a display,
which would break headless CI.
"""

from __future__ import annotations

import time


class UserAbort(Exception):
    """The user slammed the mouse into a screen corner (pyautogui failsafe)."""


def rescale_point(x: int, y: int, scaled_size, original_size) -> tuple[int, int]:
    """Map a coordinate in scaled-image space back to real screen pixels."""
    sx, sy = scaled_size
    ox, oy = original_size
    return int(x * ox / sx), int(y * oy / sy)


_pyautogui = None


def pg():
    global _pyautogui
    if _pyautogui is None:
        import pyautogui  # noqa: PLC0415

        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.04
        _pyautogui = pyautogui
    return _pyautogui


KEY_ALIASES = {
    "windows": "win",
    "super": "win",
    "meta": "win",
    "cmd": "win",
    "command": "win",
    "start": "win",
    "control": "ctrl",
    "return": "enter",
    "escape": "esc",
    "del": "delete",
    "ins": "insert",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "page_up": "pageup",
    "page_down": "pagedown",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "option": "alt",
    "spacebar": "space",
    "bksp": "backspace",
}


def normalize_keys(keys: list[str] | str) -> list[str]:
    if isinstance(keys, str):
        keys = [k for k in keys.replace("+", " ").split() if k]
    return [KEY_ALIASES.get(k.lower().strip(), k.lower().strip()) for k in keys]


def _guard(fn, *args, **kwargs):
    gui = pg()
    try:
        return fn(*args, **kwargs)
    except gui.FailSafeException as e:
        raise UserAbort("Failsafe triggered (mouse moved to a screen corner).") from e


def click(x: int, y: int, button: str = "left", clicks: int = 1) -> None:
    gui = pg()
    _guard(gui.moveTo, x, y, duration=0.12)
    _guard(gui.click, x, y, clicks=clicks, interval=0.08, button=button)


def drag(x1: int, y1: int, x2: int, y2: int) -> None:
    gui = pg()
    _guard(gui.moveTo, x1, y1, duration=0.1)
    _guard(gui.dragTo, x2, y2, duration=0.5, button="left")


def type_text(text: str) -> None:
    """pyautogui.write drops non-ASCII characters and is slow, so anything
    long or non-ASCII is pasted through the clipboard (then restored)."""
    gui = pg()
    if text.isascii() and len(text) <= 40 and "\n" not in text:
        _guard(gui.write, text, interval=0.015)
        return
    import pyperclip

    try:
        previous = pyperclip.paste()
    except Exception:
        previous = None
    pyperclip.copy(text)
    _guard(gui.hotkey, "ctrl", "v")
    time.sleep(0.25)
    if previous is not None:
        try:
            pyperclip.copy(previous)
        except Exception:
            pass


def hotkey(keys: list[str]) -> None:
    gui = pg()
    keys = normalize_keys(keys)
    if len(keys) == 1:
        _guard(gui.press, keys[0])
    else:
        _guard(gui.hotkey, *keys, interval=0.05)


def scroll(amount: int, x: int | None = None, y: int | None = None) -> None:
    gui = pg()
    if x is not None and y is not None:
        _guard(gui.moveTo, x, y, duration=0.1)
    _guard(gui.scroll, amount)
