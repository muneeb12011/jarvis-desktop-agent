"""
Universal text actions: select text in ANY app, press Ctrl+Alt+Space, pick
Explain / Summarise / Fix / Rewrite / Translate / Reply, and paste the result
straight back over your selection. Works in WhatsApp Desktop, Word, Gmail in
Chrome, VS Code, Notepad, Slack... anything that supports copy and paste.
"""

from __future__ import annotations

import logging
import time

from . import winapi

logger = logging.getLogger(__name__)

ACTIONS = {
    "explain": "Explain this clearly and briefly, as if to a smart friend. Plain text, no preamble.",
    "summarize": "Summarise this in 2-5 short bullet points. Plain text bullets starting with '- '.",
    "fix": "Fix spelling, grammar and punctuation. Keep the meaning, tone, language and formatting. "
    "Return ONLY the corrected text.",
    "rewrite": "Rewrite this to be clear, confident and professional, keeping it roughly the same length. "
    "Return ONLY the rewritten text.",
    "shorter": "Make this about half as long without losing anything important. Return ONLY the new text.",
    "casual": "Rewrite this in a relaxed, friendly, conversational tone. Return ONLY the rewritten text.",
    "translate": "Translate this. If it's in English, translate to {lang}; otherwise translate to English. "
    "Return ONLY the translation.",
    "reply": "This is a message someone sent me. Draft a short, natural reply I could send back. "
    "Return ONLY the reply text.",
    "custom": "{instruction}\nReturn ONLY the resulting text unless the instruction asks a question.",
}

# Actions whose output is meant to replace the selection.
REPLACEABLE = {"fix", "rewrite", "shorter", "casual", "translate", "reply", "custom"}


def grab_selection() -> dict:
    """Copy the current selection in the foreground app without losing the user's clipboard."""
    import pyautogui
    import pyperclip

    fg = winapi.foreground()
    try:
        previous = pyperclip.paste()
    except Exception:
        previous = ""
    sentinel = f"__jarvis_{time.time()}__"
    try:
        pyperclip.copy(sentinel)
    except Exception:
        pass
    # The hotkey's modifiers may still be physically held; release them so Ctrl+C is clean.
    for key in ("alt", "space", "shift"):
        pyautogui.keyUp(key)
    pyautogui.hotkey("ctrl", "c")
    text = ""
    for _ in range(12):
        time.sleep(0.05)
        try:
            current = pyperclip.paste()
        except Exception:
            current = sentinel
        if current != sentinel:
            text = current
            break
    try:
        pyperclip.copy(previous)
    except Exception:
        pass
    return {
        "text": text[:20000],
        "app": fg.title if fg else "",
        "process": fg.name if fg else "",
        "hwnd": fg.hwnd if fg else 0,
    }


def build_prompt(action: str, lang: str = "Urdu", instruction: str = "") -> str:
    template = ACTIONS.get(action, ACTIONS["explain"])
    return template.format(lang=lang or "Urdu", instruction=instruction or "Improve this text.")


def run_action(brain, action: str, text: str, lang: str = "", instruction: str = "") -> str:
    system = "You transform text the user selected in another app. " + build_prompt(action, lang, instruction)
    turn = brain.complete(system, [{"role": "user", "text": text}], [])
    return (turn.text or "").strip()


def paste_back(hwnd: int, text: str) -> bool:
    import pyautogui
    import pyperclip

    try:
        previous = pyperclip.paste()
    except Exception:
        previous = ""
    pyperclip.copy(text)
    if hwnd:
        winapi.focus(hwnd)
    time.sleep(0.15)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.4)
    try:
        pyperclip.copy(previous)
    except Exception:
        pass
    return True
