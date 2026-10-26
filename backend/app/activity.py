"""
Activity timeline: a private, local record of which app and window you had
in front, sampled every few seconds. Window titles carry a lot of meaning
(file names in VS Code, page titles in Chrome, chat names in WhatsApp), so
Jarvis can answer:

    "How long was I in VS Code today?"
    "What was I doing around 3pm yesterday?"
    "Find that YouTube video I watched on Monday."
    "Was I productive this week?"

Nothing leaves your PC unless you ask a question about it (then only the
matching rows go to the model). Idle time isn't recorded, password managers
are stored without titles, history older than 30 days is deleted, and the
whole thing can be switched off or wiped from Settings.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time
from collections import defaultdict

from . import memory, winapi

logger = logging.getLogger(__name__)

SAMPLE_SECONDS = 5
IDLE_AFTER_SECONDS = 300
RETENTION_DAYS = 30
MERGE_GAP_SECONDS = 20

PRIVATE_APPS = {
    a.strip().lower()
    for a in os.environ.get("JARVIS_ACTIVITY_PRIVATE", "keepass.exe,1password.exe,bitwarden.exe,keepassxc.exe").split(
        ","
    )
    if a.strip()
}

FRIENDLY = {
    "code.exe": "VS Code",
    "chrome.exe": "Chrome",
    "msedge.exe": "Edge",
    "firefox.exe": "Firefox",
    "explorer.exe": "File Explorer",
    "windowsterminal.exe": "Terminal",
    "cmd.exe": "Command Prompt",
    "powershell.exe": "PowerShell",
    "notepad.exe": "Notepad",
    "winword.exe": "Word",
    "excel.exe": "Excel",
    "powerpnt.exe": "PowerPoint",
    "outlook.exe": "Outlook",
    "spotify.exe": "Spotify",
    "discord.exe": "Discord",
    "slack.exe": "Slack",
    "teams.exe": "Teams",
    "ms-teams.exe": "Teams",
    "whatsapp.exe": "WhatsApp",
    "figma.exe": "Figma",
    "obs64.exe": "OBS",
    "vlc.exe": "VLC",
    "claude.exe": "Claude",
    "cursor.exe": "Cursor",
    "devenv.exe": "Visual Studio",
    "idea64.exe": "IntelliJ IDEA",
    "pycharm64.exe": "PyCharm",
}

_SCHEMA = """CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start REAL NOT NULL,
    end REAL NOT NULL,
    app TEXT NOT NULL,
    title TEXT NOT NULL
)"""


def _conn():
    conn = memory.get_conn()
    conn.execute(_SCHEMA)
    conn.execute("CREATE INDEX IF NOT EXISTS activity_start ON activity(start)")
    return conn


def enabled() -> bool:
    return memory.kv_get("activity_enabled", "1") == "1"


def set_enabled(on: bool) -> None:
    memory.kv_set("activity_enabled", "1" if on else "0")


def clear() -> int:
    conn = _conn()
    try:
        n = conn.execute("DELETE FROM activity").rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def friendly(app: str) -> str:
    return FRIENDLY.get(app.lower(), app.rsplit(".", 1)[0] if app else "Unknown")


_last: dict = {"id": None, "app": None, "title": None, "end": 0.0}


def record(app: str, title: str, now: float | None = None) -> None:
    """Extend the current span if nothing changed, otherwise start a new one."""
    now = now or time.time()
    if app.lower() in PRIVATE_APPS:
        title = "(private)"
    title = title[:300]
    conn = _conn()
    try:
        if _last["id"] and _last["app"] == app and _last["title"] == title and now - _last["end"] <= MERGE_GAP_SECONDS:
            conn.execute("UPDATE activity SET end = ? WHERE id = ?", (now, _last["id"]))
        else:
            cur = conn.execute(
                "INSERT INTO activity (start, end, app, title) VALUES (?, ?, ?, ?)", (now, now + 1, app, title)
            )
            _last["id"] = cur.lastrowid
        conn.commit()
        _last.update(app=app, title=title, end=now)
    finally:
        conn.close()


def prune() -> None:
    cutoff = time.time() - RETENTION_DAYS * 86400
    conn = _conn()
    try:
        conn.execute("DELETE FROM activity WHERE end < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()


def sample_once() -> None:
    if not enabled():
        return
    if winapi.idle_seconds() > IDLE_AFTER_SECONDS:
        _last["id"] = None  # next activity starts a fresh span
        return
    fg = winapi.foreground()
    if not fg or not fg.name or fg.name.lower() in ("lockapp.exe",):
        return
    if fg.name.lower() == "electron.exe" and "screen-agent" in fg.exe.lower():
        return  # Jarvis itself
    record(fg.name, fg.title)


async def run_tracker() -> None:
    last_prune = 0.0
    while True:
        try:
            await asyncio.to_thread(sample_once)
            if time.time() - last_prune > 3600:
                last_prune = time.time()
                await asyncio.to_thread(prune)
        except Exception:
            logger.debug("activity sample failed", exc_info=True)
        await asyncio.sleep(SAMPLE_SECONDS)


# ------------------------------------------------------------------ queries


def _day_bounds(day: str) -> tuple[float, float, str]:
    today = dt.date.today()
    key = (day or "today").strip().lower()
    if key == "today":
        d = today
    elif key == "yesterday":
        d = today - dt.timedelta(days=1)
    elif key in ("week", "this week", "last 7 days"):
        start = dt.datetime.combine(today - dt.timedelta(days=6), dt.time())
        return start.timestamp(), time.time(), "the last 7 days"
    else:
        try:
            d = dt.date.fromisoformat(key)
        except ValueError:
            names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            if key in names:
                delta = (today.weekday() - names.index(key)) % 7 or 7
                d = today - dt.timedelta(days=delta)
            else:
                d = today
    start = dt.datetime.combine(d, dt.time())
    return start.timestamp(), (start + dt.timedelta(days=1)).timestamp(), d.strftime("%A %d %B")


def _rows(start: float, end: float) -> list[tuple]:
    conn = _conn()
    try:
        return conn.execute(
            "SELECT start, end, app, title FROM activity WHERE end > ? AND start < ? ORDER BY start", (start, end)
        ).fetchall()
    finally:
        conn.close()


def _fmt(sec: float) -> str:
    sec = int(sec)
    h, m = divmod(sec // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m" if m else f"{sec}s"


def report(day: str = "today", top: int = 8) -> str:
    start, end, label = _day_bounds(day)
    rows = _rows(start, end)
    if not rows:
        return f"No activity recorded for {label}." + ("" if enabled() else " (Activity history is switched off.)")
    per_app: dict[str, float] = defaultdict(float)
    per_title: dict[tuple[str, str], float] = defaultdict(float)
    for s, e, app, title in rows:
        dur = max(0.0, min(e, end) - max(s, start))
        per_app[app] += dur
        per_title[(app, title)] += dur
    total = sum(per_app.values())
    first = dt.datetime.fromtimestamp(rows[0][0]).strftime("%I:%M %p")
    last = dt.datetime.fromtimestamp(rows[-1][1]).strftime("%I:%M %p")
    lines = [f"Active time {label}: {_fmt(total)} (first activity {first}, last {last})."]
    for app, dur in sorted(per_app.items(), key=lambda x: -x[1])[:top]:
        titles = sorted(((t, d) for (a, t), d in per_title.items() if a == app), key=lambda x: -x[1])[:3]
        detail = "; ".join(f"{t[:70]} ({_fmt(d)})" for t, d in titles if t)
        lines.append(f"- {friendly(app)}: {_fmt(dur)} ({dur / total:.0%}). Top: {detail}")
    return "\n".join(lines)


def search(query: str, days: int = 7, limit: int = 15) -> str:
    since = time.time() - max(1, min(days, RETENTION_DAYS)) * 86400
    words = [w for w in query.lower().split() if len(w) > 1]
    if not words:
        return "Give me something to search for."
    conn = _conn()
    try:
        clause = " AND ".join("lower(title) LIKE ?" for _ in words)
        rows = conn.execute(
            f"SELECT app, title, MIN(start), MAX(end), SUM(end - start) FROM activity "  # noqa: S608 - static clause
            f"WHERE end > ? AND {clause} GROUP BY app, title ORDER BY MAX(end) DESC LIMIT ?",
            (since, *[f"%{w}%" for w in words], limit),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return f"Nothing in the last {days} days matching '{query}'."
    out = []
    for app, title, _first, last, total in rows:
        when = dt.datetime.fromtimestamp(last).strftime("%a %d %b %I:%M %p")
        out.append(f"- [{friendly(app)}] {title[:120]} (last seen {when}, {_fmt(total)} total)")
    return "\n".join(out)


def around(time_str: str, day: str = "today", window_minutes: int = 20) -> str:
    start, end, label = _day_bounds(day)
    base = dt.datetime.fromtimestamp(start)
    t = None
    for fmt in ("%H:%M", "%I:%M %p", "%I %p", "%I:%M%p", "%I%p"):
        try:
            t = dt.datetime.strptime(time_str.strip().upper(), fmt).time()
            break
        except ValueError:
            continue
    if t is None:
        return "Give a time like '15:00' or '3 PM'."
    center = dt.datetime.combine(base.date(), t).timestamp()
    rows = _rows(center - window_minutes * 60, center + window_minutes * 60)
    if not rows:
        return f"Nothing recorded around {time_str} on {label}."
    out = [f"Around {time_str} on {label}:"]
    for s, e, app, title in rows:
        if e - s < 15:
            continue
        out.append(
            f"- {dt.datetime.fromtimestamp(s).strftime('%I:%M %p')}-"
            f"{dt.datetime.fromtimestamp(e).strftime('%I:%M %p')} {friendly(app)}: {title[:120]}"
        )
    return "\n".join(out[:40])
