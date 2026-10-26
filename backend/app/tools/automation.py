"""
Tools for the bigger capabilities: background jobs, routines, the activity
timeline and workspaces.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import os
import subprocess
import time

from .. import activity, memory, routines, undo, winapi
from ..platform_utils import IS_WINDOWS
from . import ToolResult, tool

# ------------------------------------------------------------------ background jobs


@tool(
    "start_background_job",
    "Delegate a long task to run in the background while the user keeps talking to you: research, "
    "comparisons, summarising many pages/files, writing reports, organising folders. Use when the user says "
    "'in the background', 'while I...', 'take your time', or the task clearly needs many steps without the "
    "screen. Background jobs can't use the mouse/keyboard.",
    {
        "task": {"type": "string", "description": "Complete, self-contained instructions"},
        "title": {"type": "string", "description": "3-6 word label shown in the HUD"},
    },
    ["task"],
    category="jobs",
)
def start_background_job(task: str, title: str = "") -> ToolResult:
    return ToolResult("Starting background job.", ui={"type": "start_job", "task": task, "title": title})


@tool("list_jobs", "List background jobs and their status/results.", category="jobs")
def list_jobs() -> str:
    from .. import jobs

    mgr = jobs.MANAGER
    if not mgr or not mgr.jobs:
        return "No background jobs."
    lines = []
    for j in mgr.list():
        line = f"- [{j['id']}] {j['title']}: {j['status']}"
        if j["status"] == "running" and j["step"]:
            line += f" (now: {j['step']}, {j['steps']} steps)"
        if j["result"]:
            line += f"\n  result: {j['result'][:400]}"
        lines.append(line)
    return "\n".join(lines)


@tool("cancel_job", "Cancel a running background job by id.", {"id": {"type": "string"}}, ["id"], category="jobs")
def cancel_job(id: str) -> ToolResult:  # noqa: A002
    return ToolResult(f"Cancelling job {id}.", ui={"type": "cancel_job", "id": id})


# ------------------------------------------------------------------ routines


@tool(
    "create_routine",
    "Create (or replace) an automation. trigger: time (needs at='HH:MM' 24h, days=daily|weekdays|weekends|"
    "'mon,wed'), interval (minutes), app_open (app, e.g. 'code', 'spotify'), file_added (folder, optional "
    "pattern like '*.pdf'), web_change (url, optional contains text, minutes between checks), startup, manual "
    "(runs when the user says its name). action is a plain-English instruction you'll carry out when it fires.",
    {
        "name": {"type": "string", "description": "Short name the user can say, e.g. 'start my day'"},
        "trigger": {"type": "string", "enum": list(routines.TRIGGERS)},
        "action": {"type": "string"},
        "at": {"type": "string"},
        "days": {"type": "string"},
        "minutes": {"type": "number"},
        "app": {"type": "string"},
        "folder": {"type": "string"},
        "pattern": {"type": "string"},
        "url": {"type": "string"},
        "contains": {"type": "string"},
        "announce": {"type": "boolean", "description": "Speak the result when it runs (default true)"},
    },
    ["name", "trigger", "action"],
    category="routines",
)
def create_routine(name: str, trigger: str, action: str, announce: bool = True, **params) -> ToolResult:
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    try:
        r = routines.create(name, trigger, clean, action, announce)
    except ValueError as e:
        return ToolResult(str(e), ok=False)
    return ToolResult(
        f"Routine '{r['name']}' saved: {routines.describe_trigger(r)} -> {r['action']}", ui={"type": "routines_changed"}
    )


@tool("list_routines", "List the user's routines (automations).", category="routines")
def list_routines() -> str:
    items = routines.all_routines()
    if not items:
        return "No routines yet."
    out = []
    for r in items:
        last = dt.datetime.fromtimestamp(r["last_run"]).strftime("%a %I:%M %p") if r["last_run"] else "never"
        state = "" if r["enabled"] else " [paused]"
        out.append(
            f"- {r['name']}{state}: {routines.describe_trigger(r)} -> {r['action'][:140]} "
            f"(ran {r['runs']}x, last {last})"
        )
    return "\n".join(out)


@tool(
    "delete_routine",
    "Delete a routine by name or id.",
    {"name": {"type": "string"}},
    ["name"],
    category="routines",
    confirm=lambda a: f"Delete the routine '{a.get('name')}'?",
)
def delete_routine(name: str) -> ToolResult:
    ok = routines.delete(name)
    return ToolResult("Deleted." if ok else f"No routine called '{name}'.", ok=ok, ui={"type": "routines_changed"})


@tool(
    "toggle_routine",
    "Pause or resume a routine.",
    {"name": {"type": "string"}, "enabled": {"type": "boolean"}},
    ["name", "enabled"],
    category="routines",
)
def toggle_routine(name: str, enabled: bool) -> ToolResult:
    ok = routines.set_enabled(name, enabled)
    return ToolResult(
        ("Resumed." if enabled else "Paused.") if ok else f"No routine called '{name}'.",
        ok=ok,
        ui={"type": "routines_changed"},
    )


@tool(
    "run_routine",
    "Run one of the user's routines now (e.g. when they say its name).",
    {"name": {"type": "string"}},
    ["name"],
    category="routines",
)
def run_routine(name: str) -> ToolResult:
    r = routines.find(name)
    if not r:
        names = ", ".join(x["name"] for x in routines.all_routines()) or "none"
        return ToolResult(f"No routine called '{name}'. Routines: {names}", ok=False)
    return ToolResult(f"Running routine '{r['name']}'.", ui={"type": "run_routine", "id": r["id"]})


# ------------------------------------------------------------------ activity timeline


@tool(
    "activity_report",
    "How the user spent time on the computer: active time, time per app, top windows/files/"
    "pages. day: today, yesterday, a weekday name, YYYY-MM-DD, or 'week'.",
    {"day": {"type": "string"}},
    category="activity",
)
def activity_report(day: str = "today") -> str:
    return activity.report(day)


@tool(
    "activity_search",
    "Search the user's window history (page titles, file names, chats, videos) for the last N "
    "days. Use for 'that article/video/file I had open...'.",
    {"query": {"type": "string"}, "days": {"type": "integer"}},
    ["query"],
    category="activity",
)
def activity_search(query: str, days: int = 7) -> str:
    return activity.search(query, days)


@tool(
    "what_was_i_doing",
    "What the user had on screen around a given time.",
    {"time": {"type": "string", "description": "e.g. '15:30' or '3 PM'"}, "day": {"type": "string"}},
    ["time"],
    category="activity",
)
def what_was_i_doing(time: str, day: str = "today") -> str:  # noqa: A002 - schema name
    return activity.around(time, day)


# ------------------------------------------------------------------ workspaces


def _ws_key(name: str) -> str:
    return "workspace:" + name.strip().lower()


def _all_workspaces() -> dict[str, dict]:
    conn = memory.get_conn()
    try:
        rows = conn.execute("SELECT key, value FROM kv WHERE key LIKE 'workspace:%'").fetchall()
    finally:
        conn.close()
    return {k.split(":", 1)[1]: json.loads(v) for k, v in rows}


@tool(
    "save_workspace",
    "Remember the current set of open app windows and their positions under a name "
    "(e.g. 'coding', 'study', 'editing'), so it can be restored later.",
    {"name": {"type": "string"}},
    ["name"],
    category="workspaces",
)
def save_workspace(name: str) -> str:
    if not IS_WINDOWS:
        return "Workspaces need Windows."
    wins = [w for w in winapi.top_windows() if w.exe and not w.minimized and w.name.lower() != "explorer.exe"]
    if not wins:
        return "No app windows to save."
    items = [
        {"exe": w.exe, "name": w.name, "title": w.title, "rect": list(w.rect), "maximized": w.maximized} for w in wins
    ]
    memory.kv_set(_ws_key(name), json.dumps({"saved": time.time(), "windows": items}))
    apps = ", ".join(sorted({w.name.rsplit(".", 1)[0] for w in wins}))
    return f"Saved workspace '{name}' with {len(items)} windows: {apps}."


def _wait_for_window(exe: str, exclude: set[int], timeout: float = 12.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for w in winapi.top_windows():
            if w.exe.lower() == exe.lower() and w.hwnd not in exclude:
                return w
        time.sleep(0.5)
    return None


@tool(
    "restore_workspace",
    "Open the apps of a saved workspace and put their windows back where they were.",
    {"name": {"type": "string"}},
    ["name"],
    category="workspaces",
    mutates=True,
)
def restore_workspace(name: str) -> str:
    if not IS_WINDOWS:
        return "Workspaces need Windows."
    saved = _all_workspaces()
    key = name.strip().lower()
    if key not in saved:
        close = difflib.get_close_matches(key, list(saved), n=1)
        if not close:
            return f"No workspace called '{name}'. Saved: {', '.join(saved) or 'none'}."
        key = close[0]
    items = saved[key]["windows"]
    used: set[int] = set()
    launched, placed, failed = 0, 0, []
    for item in items:
        current = [w for w in winapi.top_windows() if w.exe.lower() == item["exe"].lower() and w.hwnd not in used]
        # Prefer the window whose title best matches what was saved.
        current.sort(key=lambda w: -difflib.SequenceMatcher(None, w.title, item["title"]).ratio())
        target = current[0] if current else None
        if target is None:
            if not os.path.exists(item["exe"]):
                failed.append(item["name"])
                continue
            subprocess.Popen([item["exe"]], cwd=os.path.dirname(item["exe"]))
            launched += 1
            target = _wait_for_window(item["exe"], used)
            if target is None:
                failed.append(item["name"])
                continue
        used.add(target.hwnd)
        winapi.place(target.hwnd, tuple(item["rect"]), item["maximized"])
        placed += 1
    msg = f"Restored '{key}': {placed} windows placed, {launched} apps launched."
    if failed:
        msg += f" Couldn't open: {', '.join(failed)}."
    return msg


@tool("list_workspaces", "List saved workspaces.", category="workspaces")
def list_workspaces() -> str:
    saved = _all_workspaces()
    if not saved:
        return "No saved workspaces."
    return "\n".join(
        f"- {n}: {len(v['windows'])} windows ({', '.join(sorted({w['name'].rsplit('.', 1)[0] for w in v['windows']}))})"
        for n, v in saved.items()
    )


@tool("delete_workspace", "Delete a saved workspace.", {"name": {"type": "string"}}, ["name"], category="workspaces")
def delete_workspace(name: str) -> str:
    conn = memory.get_conn()
    try:
        n = conn.execute("DELETE FROM kv WHERE key = ?", (_ws_key(name),)).rowcount
        conn.commit()
    finally:
        conn.close()
    return "Deleted." if n else f"No workspace called '{name}'."


# ------------------------------------------------------------------ undo


@tool(
    "undo_last",
    "Undo the most recent change(s) Jarvis made: file writes, settings, reminders, notes, routines, "
    "workspaces, window layouts, clipboard, volume, remembered facts. Use for 'undo that', 'put it back'.",
    {"count": {"type": "integer", "description": "How many changes to undo (default 1)"}},
    category="undo",
)
def undo_last(count: int = 1) -> str:
    return " ".join(undo.undo_last(int(count or 1)))


@tool(
    "undo_last_request",
    "Undo everything Jarvis changed while handling one request (the latest by default). "
    "Use for 'undo all of that', 'revert what you just did'.",
    category="undo",
)
def undo_last_request() -> str:
    return " ".join(undo.undo_run())


@tool("undo_history", "List recent changes Jarvis made and whether each can be undone.", category="undo")
def undo_history() -> str:
    items = undo.recent(15)
    return "\n".join(undo.describe(e) for e in items) or "No changes recorded yet."
