"""
Routines: automations you describe in plain English.

    "Every weekday at 9, brief me and open my standup notes."
    "When I open VS Code, play my focus playlist and set volume to 25."
    "Whenever a PDF lands in Downloads, tell me what it is and move invoices to Documents/Invoices."
    "Check the PS5 page on daraz every 30 minutes and tell me when it says in stock."
    "Every hour, remind me to drink water."

A routine = a trigger + an instruction. When the trigger fires, Jarvis runs the
instruction as a background job (with the trigger details attached, e.g. which
file arrived) and speaks the result if you asked it to.

Triggers: time, interval, app_open, file_added, web_change, startup, manual.
Manual routines run when you say their name ("Jarvis, start my day").
"""

from __future__ import annotations

import asyncio
import datetime as dt
import fnmatch
import hashlib
import json
import logging
import re
import time
from pathlib import Path

from . import memory, winapi
from .platform_utils import resolve_path

logger = logging.getLogger(__name__)

TRIGGERS = ("time", "interval", "app_open", "file_added", "web_change", "startup", "manual")
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
PARTIAL_DOWNLOADS = (".crdownload", ".part", ".tmp", ".download", ".partial")

_SCHEMA = """CREATE TABLE IF NOT EXISTS routines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    trigger TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',
    action TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    announce INTEGER NOT NULL DEFAULT 1,
    last_run REAL,
    runs INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL
)"""


def _conn():
    conn = memory.get_conn()
    conn.execute(_SCHEMA)
    return conn


def _row(r) -> dict:
    return {
        "id": r[0],
        "name": r[1],
        "trigger": r[2],
        "params": json.loads(r[3] or "{}"),
        "action": r[4],
        "enabled": bool(r[5]),
        "announce": bool(r[6]),
        "last_run": r[7],
        "runs": r[8],
    }


def all_routines() -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, name, trigger, params, action, enabled, announce, last_run, runs FROM routines ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [_row(r) for r in rows]


def find(name_or_id: str | int) -> dict | None:
    key = str(name_or_id).strip().lower()
    items = all_routines()
    for r in items:
        if str(r["id"]) == key or r["name"].lower() == key:
            return r
    matches = [r for r in items if key in r["name"].lower()]
    return matches[0] if len(matches) == 1 else None


def describe_trigger(r: dict) -> str:
    p, t = r["params"], r["trigger"]
    if t == "time":
        return f"at {p.get('at')} ({p.get('days', 'daily')})"
    if t == "interval":
        return f"every {p.get('minutes')} min"
    if t == "app_open":
        return f"when {p.get('app')} opens"
    if t == "file_added":
        return f"when {p.get('pattern', '*')} appears in {p.get('folder')}"
    if t == "web_change":
        extra = f" mentions '{p['contains']}'" if p.get("contains") else " changes"
        return f"when {p.get('url')}{extra} (every {p.get('minutes', 30)} min)"
    if t == "startup":
        return "when Jarvis starts"
    return "when you ask"


def validate(trigger: str, params: dict) -> str | None:
    if trigger not in TRIGGERS:
        return f"Unknown trigger '{trigger}'. Use one of: {', '.join(TRIGGERS)}."
    if trigger == "time":
        if not re.fullmatch(r"\d{1,2}:\d{2}", str(params.get("at", ""))):
            return "Time routines need at='HH:MM' (24-hour)."
        days = str(params.get("days", "daily")).lower()
        if days not in ("daily", "weekdays", "weekends") and not all(
            d.strip()[:3] in DAY_NAMES for d in days.split(",")
        ):
            return "days must be daily, weekdays, weekends or a list like 'mon,wed,fri'."
    if trigger == "interval" and float(params.get("minutes", 0) or 0) < 1:
        return "Interval routines need minutes >= 1."
    if trigger == "app_open" and not params.get("app"):
        return "app_open routines need app (e.g. 'code', 'spotify', 'chrome')."
    if trigger == "file_added" and not params.get("folder"):
        return "file_added routines need folder (e.g. 'downloads')."
    if trigger == "web_change" and not str(params.get("url", "")).startswith("http"):
        return "web_change routines need a full url starting with http."
    return None


def create(name: str, trigger: str, params: dict, action: str, announce: bool = True) -> dict:
    err = validate(trigger, params)
    if err:
        raise ValueError(err)
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO routines (name, trigger, params, action, announce, created) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET trigger=excluded.trigger, params=excluded.params, "
            "action=excluded.action, announce=excluded.announce, enabled=1",
            (name.strip(), trigger, json.dumps(params), action.strip(), int(announce), time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    r = find(name)
    assert r
    return r


def delete(name_or_id: str | int) -> bool:
    r = find(name_or_id)
    if not r:
        return False
    conn = _conn()
    try:
        conn.execute("DELETE FROM routines WHERE id = ?", (r["id"],))
        conn.commit()
    finally:
        conn.close()
    return True


def set_enabled(name_or_id: str | int, on: bool) -> bool:
    r = find(name_or_id)
    if not r:
        return False
    conn = _conn()
    try:
        conn.execute("UPDATE routines SET enabled = ? WHERE id = ?", (int(on), r["id"]))
        conn.commit()
    finally:
        conn.close()
    return True


def mark_run(routine_id: int, when: float | None = None) -> None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE routines SET last_run = ?, runs = runs + 1 WHERE id = ?", (when or time.time(), routine_id)
        )
        conn.commit()
    finally:
        conn.close()


def prompt_block() -> str:
    """Listed in the system prompt so 'Jarvis, start my day' maps to run_routine."""
    items = [r for r in all_routines() if r["enabled"]]
    if not items:
        return ""
    lines = [f"- {r['name']} ({describe_trigger(r)}): {r['action'][:120]}" for r in items[:25]]
    return "ROUTINES (the user can run any by name; use run_routine):\n" + "\n".join(lines)


# ------------------------------------------------------------------ trigger evaluation (pure, testable)


def time_due(r: dict, now: dt.datetime) -> bool:
    p = r["params"]
    hh, mm = (int(x) for x in str(p.get("at", "0:0")).split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    days = str(p.get("days", "daily")).lower()
    wd = now.weekday()
    if days == "weekdays" and wd >= 5 or days == "weekends" and wd < 5:
        return False
    if days not in ("daily", "weekdays", "weekends") and DAY_NAMES[wd] not in [d.strip()[:3] for d in days.split(",")]:
        return False
    if not (target <= now <= target + dt.timedelta(minutes=60)):  # fire late if Jarvis started late, max 1h
        return False
    last = r.get("last_run")
    return not last or dt.datetime.fromtimestamp(last) < target


def interval_due(r: dict, now: float, started: float) -> bool:
    period = float(r["params"].get("minutes", 60)) * 60
    return now - (r.get("last_run") or started) >= period


class Engine:
    def __init__(self, fire) -> None:
        self.fire = fire  # async (routine, context) -> None
        self.started = time.time()
        self._windows: set[str] | None = None
        self._files: dict[int, set[str]] = {}
        self._web_next: dict[int, float] = {}
        self._app_debounce: dict[int, float] = {}
        self._startup_done = False

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("Routine tick failed")
            await asyncio.sleep(3)

    async def tick(self) -> None:
        items = [r for r in await asyncio.to_thread(all_routines) if r["enabled"]]
        now = time.time()
        now_dt = dt.datetime.now()
        need_windows = any(r["trigger"] == "app_open" for r in items)
        current_windows: set[str] = set()
        if need_windows:
            wins = await asyncio.to_thread(winapi.top_windows)
            current_windows = {f"{w.name.lower()}|{w.title.lower()}" for w in wins}

        for r in items:
            t = r["trigger"]
            context = ""
            due = False
            if t == "time":
                due = time_due(r, now_dt)
            elif t == "interval":
                due = interval_due(r, now, self.started)
            elif t == "startup":
                due = not self._startup_done
            elif t == "app_open" and self._windows is not None:
                needle = str(r["params"].get("app", "")).lower().replace(".exe", "")
                new = [w for w in current_windows - self._windows if needle in w]
                if new and now - self._app_debounce.get(r["id"], 0) > 60:
                    self._app_debounce[r["id"]] = now
                    due, context = True, f"Window that opened: {new[0].split('|', 1)[1][:120]}"
            elif t == "file_added":
                due, context = await asyncio.to_thread(self._check_files, r)
            elif t == "web_change" and now >= self._web_next.get(r["id"], 0):
                self._web_next[r["id"]] = now + max(5, float(r["params"].get("minutes", 30))) * 60
                due, context = await asyncio.to_thread(self._check_web, r)
            if due:
                mark_run(r["id"])
                logger.info("Routine '%s' fired (%s)", r["name"], describe_trigger(r))
                await self.fire(r, context)

        self._startup_done = True
        if need_windows:
            self._windows = current_windows

    def _check_files(self, r: dict) -> tuple[bool, str]:
        folder = resolve_path(str(r["params"].get("folder", "downloads")))
        pattern = str(r["params"].get("pattern", "*")).lower()
        try:
            names = {p.name for p in folder.iterdir() if p.is_file()}
        except OSError:
            return False, ""
        prev = self._files.get(r["id"])
        self._files[r["id"]] = names
        if prev is None:
            return False, ""
        new = [
            n for n in names - prev if fnmatch.fnmatch(n.lower(), pattern) and not n.lower().endswith(PARTIAL_DOWNLOADS)
        ]
        if not new:
            return False, ""
        return True, "New file(s): " + ", ".join(str(Path(folder) / n) for n in sorted(new)[:10])

    def _check_web(self, r: dict) -> tuple[bool, str]:
        from .tools.web import read_webpage

        url = r["params"]["url"]
        try:
            text = read_webpage(url, max_chars=20000)
        except Exception as e:
            logger.info("web_change fetch failed for %s: %s", url, e)
            return False, ""
        body = text.split("\n\n", 1)[-1]
        digest = hashlib.sha256(re.sub(r"\s+", " ", body).encode()).hexdigest()
        key = f"routine_web_{r['id']}"
        state = json.loads(memory.kv_get(key, "{}") or "{}")
        contains = str(r["params"].get("contains", "")).lower()
        has = contains in body.lower() if contains else None
        memory.kv_set(key, json.dumps({"hash": digest, "has": has}))
        if not state:
            return False, ""  # first look is the baseline
        if contains:
            if has and not state.get("has"):
                return True, f"The page {url} now mentions '{contains}'."
            return False, ""
        if digest != state.get("hash"):
            return True, f"The page {url} changed. Current text (truncated):\n{body[:3000]}"
        return False, ""
