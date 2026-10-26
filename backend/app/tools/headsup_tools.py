"""
Tools for heads-up (screen-aware help) and for reading a window's text.
"""

from __future__ import annotations

from .. import headsup, winapi
from . import ToolResult, tool


@tool(
    "headsup",
    "Turn heads-up on or off, mute or unmute it for one app, or show its status. Heads-up watches terminals and "
    "code editors for errors, notices finished downloads and frozen apps, and offers one line of help. "
    "Use for 'watch my terminal for errors', 'stop pointing out errors in VS Code', 'turn off heads-up'.",
    {
        "enabled": {"type": "boolean", "description": "Turn the whole feature on (true) or off (false)"},
        "mute": {"type": "string", "description": "App to stop watching, e.g. 'VS Code', 'Terminal', 'downloads'"},
        "unmute": {"type": "string", "description": "App to watch again"},
    },
    category="headsup",
)
def headsup_tool(enabled: bool | None = None, mute: str = "", unmute: str = "") -> ToolResult:
    if enabled is not None:
        headsup.set_enabled(bool(enabled))
    if mute:
        headsup.set_muted(mute, True)
    if unmute:
        headsup.set_muted(unmute, False)
    return ToolResult(headsup.status(), ui={"type": "headsup_changed"})


@tool(
    "recent_headsups",
    "What heads-up recently pointed out (errors with their full text, downloads, frozen apps). Use for "
    "'what was that error?', 'explain that last error'.",
    category="headsup",
)
def recent_headsups() -> str:
    w = headsup.WATCHER
    if not w or not w.recent:
        return "Nothing pointed out yet this session." + ("" if headsup.enabled() else " (Heads-up is off.)")
    out = []
    for s in reversed(w.recent):
        out.append(f"- [{s.kind}] {s.app}: {s.text}" + (f"\n  Raw:\n{s.detail[-1500:]}" if s.kind == "error" else ""))
    return "\n".join(out)


@tool(
    "read_window_text",
    "Read the text inside a window (terminal output, document, editor) without a screenshot. Defaults to the "
    "window in front; pass part of a window title to read another one. Returns the last part of the text.",
    {
        "title": {"type": "string", "description": "Part of the window title; empty = foreground window"},
        "max_chars": {"type": "integer", "description": "How much of the end to return (default 4000)"},
    },
    category="screen",
    ui_thread=True,
)
def read_window_text(title: str = "", max_chars: int = 4000) -> ToolResult:
    if title:
        key = title.lower()
        matches = [w for w in winapi.top_windows() if key in w.title.lower()]
        if not matches:
            return ToolResult(f"No open window with '{title}' in its title.", ok=False)
        win = matches[0]
    else:
        win = winapi.foreground()
        if not win:
            return ToolResult("No window in front.", ok=False)
    text = headsup.read_window_text(win.hwnd, budget=2.5, max_chars=max(500, min(int(max_chars), 20000)))
    if not text.strip():
        return ToolResult(f"'{win.title}' exposes no readable text (take a screenshot instead).", ok=False)
    return ToolResult(f"Text of '{win.title}' (last {min(len(text), max_chars)} chars):\n{text[-int(max_chars) :]}")
