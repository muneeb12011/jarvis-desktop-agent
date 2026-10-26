"""
Undo journal: "Jarvis, undo that."

Before a tool that changes something runs, Jarvis snapshots what it's about
to change (the old file, the old setting, the old window position...). After
the tool succeeds, the snapshot is recorded. Undo replays the inverse.

Nothing is ever permanently deleted by an undo: files Jarvis created are
moved to backend/undo/trash instead. Backups older than 7 days are pruned.

Tools without a safe inverse (shell commands, typing into apps, integration
calls) are still journalled as "can't undo automatically", so Jarvis can tell
you exactly what it did and what you'd have to reverse yourself.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config, memory

logger = logging.getLogger(__name__)

UNDO_DIR = config.DATA_DIR / "undo"
RETENTION_DAYS = 7

_SCHEMA = """CREATE TABLE IF NOT EXISTS undo_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    ts REAL NOT NULL,
    tool TEXT NOT NULL,
    label TEXT NOT NULL,
    data TEXT NOT NULL,
    undoable INTEGER NOT NULL,
    undone INTEGER NOT NULL DEFAULT 0
)"""


def _conn():
    conn = memory.get_conn()
    conn.execute(_SCHEMA)
    return conn


# ------------------------------------------------------------------ snapshot / restore per tool
# snapshot(args) -> data (or None to skip journalling); finalize(data, args, result_text) -> data
# restore(data) -> human message


def _backup_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    dest = UNDO_DIR / "files" / uuid.uuid4().hex[:12] / path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    return str(dest)


def _snap_write_file(args: dict) -> dict:
    from .platform_utils import resolve_path

    p = resolve_path(str(args.get("path", "")))
    return {"path": str(p), "backup": _backup_file(p), "existed": p.exists()}


def _undo_write_file(d: dict) -> str:
    path = Path(d["path"])
    if d.get("backup"):
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(d["backup"], path)
        return f"Restored the previous version of {path.name}."
    if path.exists():
        trash = UNDO_DIR / "trash" / f"{int(time.time())}-{path.name}"
        trash.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), trash)
        return f"Removed {path.name} (it's in backend/undo/trash if you want it back)."
    return f"{path.name} is already gone."


def _snap_settings(_args: dict) -> dict:
    from . import settings

    cur = settings.current()
    return {k: cur[k] for k in ("tts_voice", "tts_rate", "confirm_level", "user_title", "city", "startup")}


def _undo_settings(d: dict) -> str:
    from . import settings

    settings.update(d)
    return "Settings restored."


def _final_id(pattern: str) -> Callable[[dict, dict, str], dict]:
    def fin(data: dict, _args: dict, result: str) -> dict:
        m = re.search(pattern, result)
        return {**data, "id": int(m.group(1))} if m else data

    return fin


def _undo_reminder(d: dict) -> str:
    if "id" not in d:
        return "Couldn't find which reminder to cancel."
    return "Cancelled it." if memory.cancel_reminder(d["id"]) else "It had already fired or been cancelled."


def _snap_cancel_reminder(args: dict) -> dict | None:
    rid = int(args.get("id", 0))
    match = next((r for r in memory.pending_reminders() if r["id"] == rid), None)
    return {"reminder": match} if match else None


def _undo_cancel_reminder(d: dict) -> str:
    r = d["reminder"]
    if r["due_at"] < time.time():
        return "That reminder's time has already passed, so I didn't bring it back."
    memory.add_reminder(r["text"], r["due_at"], r.get("kind", "reminder"), r.get("label"))
    return "Reminder restored."


def _undo_remember(d: dict) -> str:
    removed = memory.forget_fact(d["fact"])
    return "Forgotten again." if removed else "That fact was already gone."


def _snap_forget(args: dict) -> dict:
    q = str(args.get("text", "")).lower()
    return {"facts": [f for f in memory.get_facts(500) if q and q in f.lower()]}


def _undo_forget(d: dict) -> str:
    for f in d["facts"]:
        memory.add_fact(f, source="undo")
    return f"Remembered {len(d['facts'])} fact(s) again."


def _undo_note(d: dict) -> str:
    if "id" not in d:
        return "Couldn't find the note."
    conn = memory.get_conn()
    try:
        conn.execute("DELETE FROM notes WHERE id = ?", (d["id"],))
        conn.commit()
    finally:
        conn.close()
    return "Note removed."


def _snap_routine(args: dict) -> dict:
    from . import routines

    r = routines.find(args.get("name", ""))
    return {"name": args.get("name", ""), "before": r}


def _undo_routine(d: dict) -> str:
    from . import routines

    before = d.get("before")
    if before is None:
        routines.delete(d["name"])
        return f"Removed the routine '{d['name']}'."
    routines.create(before["name"], before["trigger"], before["params"], before["action"], before["announce"])
    routines.set_enabled(before["name"], before["enabled"])
    return f"Routine '{before['name']}' is back to how it was."


def _snap_workspace(args: dict) -> dict:
    key = "workspace:" + str(args.get("name", "")).strip().lower()
    return {"key": key, "before": memory.kv_get(key)}


def _undo_workspace(d: dict) -> str:
    conn = memory.get_conn()
    try:
        if d["before"] is None:
            conn.execute("DELETE FROM kv WHERE key = ?", (d["key"],))
        else:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
                (d["key"], d["before"], d["before"]),
            )
        conn.commit()
    finally:
        conn.close()
    return "Workspace list restored."


def _snap_layout(_args: dict) -> dict:
    from . import winapi

    return {
        "windows": [
            {"hwnd": w.hwnd, "rect": list(w.rect), "maximized": w.maximized, "minimized": w.minimized}
            for w in winapi.top_windows()
        ]
    }


def _undo_layout(d: dict) -> str:
    from . import winapi
    from .platform_utils import IS_WINDOWS

    if not IS_WINDOWS:
        return "Window layouts need Windows."
    alive = {w.hwnd for w in winapi.top_windows()}
    n = 0
    for w in d["windows"]:
        if w["hwnd"] in alive:
            if w["minimized"]:
                winapi.user32.ShowWindow(w["hwnd"], 6)  # SW_MINIMIZE
            else:
                winapi.place(w["hwnd"], tuple(w["rect"]), w["maximized"])
            n += 1
    return f"Put {n} windows back where they were."


def _snap_clipboard(_args: dict) -> dict:
    import pyperclip

    try:
        return {"text": pyperclip.paste()}
    except Exception:
        return {"text": ""}


def _undo_clipboard(d: dict) -> str:
    import pyperclip

    pyperclip.copy(d["text"])
    return "Clipboard restored."


def _snap_volume(_args: dict) -> dict | None:
    try:
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL  # type: ignore
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume  # type: ignore

        iface = AudioUtilities.GetSpeakers().Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        vol = cast(iface, POINTER(IAudioEndpointVolume))
        return {"level": round(vol.GetMasterVolumeLevelScalar() * 100)}
    except Exception:
        return None


def _undo_volume(d: dict) -> str:
    from .tools.system import set_volume

    return set_volume(d["level"])


def _undo_entity_note(d: dict) -> str:
    from . import people

    return "Removed that note." if "id" in d and people.delete_note(d["id"]) else "That note was already gone."


def _undo_commitment(d: dict) -> str:
    from . import people

    return "Removed that commitment." if "id" in d and people.delete_commitment(d["id"]) else "Already gone."


def _undo_complete(d: dict) -> str:
    from . import people

    people.set_done(int(d["id"]), False)
    return "Marked it as open again."


UndoSpec = tuple[Callable[[dict], Any], Callable[[dict], str], Callable[[dict, dict, str], dict] | None]

SPECS: dict[str, UndoSpec] = {
    "write_file": (_snap_write_file, _undo_write_file, None),
    "jarvis_settings": (_snap_settings, _undo_settings, None),
    "set_reminder": (lambda a: {}, _undo_reminder, _final_id(r"Reminder #(\d+)")),
    "set_timer": (lambda a: {}, _undo_reminder, _final_id(r"Timer #(\d+)")),
    "cancel_reminder": (_snap_cancel_reminder, _undo_cancel_reminder, None),
    "remember": (lambda a: {"fact": str(a.get("fact", ""))}, _undo_remember, None),
    "forget": (_snap_forget, _undo_forget, None),
    "take_note": (lambda a: {}, _undo_note, _final_id(r"Note #(\d+)")),
    "create_routine": (_snap_routine, _undo_routine, None),
    "delete_routine": (_snap_routine, _undo_routine, None),
    "toggle_routine": (_snap_routine, _undo_routine, None),
    "save_workspace": (_snap_workspace, _undo_workspace, None),
    "delete_workspace": (_snap_workspace, _undo_workspace, None),
    "restore_workspace": (_snap_layout, _undo_layout, None),
    "window_action": (_snap_layout, _undo_layout, None),
    "clipboard_write": (_snap_clipboard, _undo_clipboard, None),
    "set_volume": (_snap_volume, _undo_volume, None),
    "remember_about": (lambda a: {}, _undo_entity_note, _final_id(r"note #(\d+) about")),
    "add_commitment": (lambda a: {}, _undo_commitment, _final_id(r"commitment #(\d+)")),
    "complete_commitment": (lambda a: {"id": int(a.get("id", 0))}, _undo_complete, None),
}

# Changes something but has no automatic inverse: journalled so Jarvis can be honest about it.
NOT_UNDOABLE_HINT = {
    "run_powershell": "shell commands can't be reversed automatically",
    "type_text": "press Ctrl+Z in that app to undo typing",
    "press_keys": "key presses can't be reversed automatically",
    "close_app": "closed apps can be reopened, but unsaved work can't be recovered",
    "power": "power actions can't be undone (shutdown/restart can be cancelled with 'cancel')",
    "open_app": "close the app to undo",
}


class Pending:
    def __init__(self, tool: str, data: Any, spec: UndoSpec | None) -> None:
        self.tool, self.data, self.spec = tool, data, spec


def before(tool_name: str, source: str, args: dict, mutates: bool) -> Pending | None:
    """Called right before a tool runs, on the same thread."""
    spec = SPECS.get(tool_name)
    if spec:
        try:
            data = spec[0](args)
        except Exception:
            logger.debug("undo snapshot failed for %s", tool_name, exc_info=True)
            return Pending(tool_name, None, None)
        return Pending(tool_name, data, spec) if data is not None else None
    if tool_name == "run_powershell":
        from .tools.system import _confirm_shell

        if _confirm_shell(args) is None:
            return None  # read-only command: nothing changed, nothing to journal
    if tool_name in NOT_UNDOABLE_HINT or (mutates and source != "builtin"):
        return Pending(tool_name, None, None)
    return None


def commit(p: Pending | None, run_id: int | None, args: dict, ok: bool, result_text: str) -> int | None:
    """Called after the tool ran. Returns the journal id, or None if nothing was recorded."""
    if p is None or not ok:
        return None
    data = p.data
    if p.spec and p.spec[2]:
        data = p.spec[2](data, args, result_text)
    label = _label(p.tool, args)
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO undo_log (run_id, ts, tool, label, data, undoable) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, time.time(), p.tool, label, json.dumps(data, default=str), int(p.spec is not None)),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _label(tool: str, args: dict) -> str:
    detail = args.get("path") or args.get("name") or args.get("fact") or args.get("text") or args.get("command") or ""
    detail = str(detail)
    return f"{tool.replace('_', ' ')}{': ' + detail[:80] if detail else ''}"


def undo_entry(entry_id: int) -> str:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT tool, label, data, undoable, undone FROM undo_log WHERE id = ?", (entry_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return "Nothing to undo with that id."
    tool, label, data, undoable, undone = row
    if undone:
        return f"Already undone: {label}."
    if not undoable:
        return f"Can't undo '{label}' automatically: {NOT_UNDOABLE_HINT.get(tool, 'no safe inverse')}."
    spec = SPECS[tool]
    try:
        msg = spec[1](json.loads(data))
    except Exception as e:
        logger.exception("Undo failed for %s", tool)
        return f"Undo failed for '{label}': {type(e).__name__}: {e}"
    conn = _conn()
    try:
        conn.execute("UPDATE undo_log SET undone = 1 WHERE id = ?", (entry_id,))
        conn.commit()
    finally:
        conn.close()
    return msg


def recent(limit: int = 15) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, run_id, ts, tool, label, undoable, undone FROM undo_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r[0],
            "run_id": r[1],
            "ts": r[2],
            "tool": r[3],
            "label": r[4],
            "undoable": bool(r[5]),
            "undone": bool(r[6]),
        }
        for r in rows
    ]


def undo_last(count: int = 1) -> list[str]:
    out = []
    for e in [e for e in recent(50) if not e["undone"] and e["undoable"]][: max(1, count)]:
        out.append(undo_entry(e["id"]))
    return out or ["There's nothing I can undo."]


def undo_run(run_id: int | None = None) -> list[str]:
    """Undo everything one request did (the most recent one with changes, by default), newest first."""
    entries = [e for e in recent(200) if not e["undone"]]
    if run_id is None:
        run_id = next((e["run_id"] for e in entries if e["undoable"]), None)
    if run_id is None:
        return ["There's nothing I can undo."]
    msgs = []
    for e in [e for e in entries if e["run_id"] == run_id]:
        msgs.append(undo_entry(e["id"]))
    return msgs


def prune() -> None:
    cutoff = time.time() - RETENTION_DAYS * 86400
    conn = _conn()
    try:
        conn.execute("DELETE FROM undo_log WHERE ts < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()
    for sub in ("files", "trash"):
        base = UNDO_DIR / sub
        if not base.exists():
            continue
        for p in base.iterdir():
            try:
                if p.stat().st_mtime < cutoff:
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
            except OSError:
                pass


def describe(e: dict) -> str:
    when = dt.datetime.fromtimestamp(e["ts"]).strftime("%I:%M %p")
    state = "undone" if e["undone"] else ("can undo" if e["undoable"] else "manual only")
    return f"#{e['id']} [{when}] {e['label']} ({state})"
