"""
People & project memory.

Flat facts ("likes tea") aren't enough for real life. This keeps a small
profile per person and per project, a dated log of what you told Jarvis
about each, and commitments in both directions:

    "Hassan is our backend lead, he prefers Slack over email."
    "I promised Hassan the API docs by Friday."
    "Shahzain owes me feedback on the pitch deck."
    "Ember is my compiler project, written in Rust."

    "What do I owe Hassan?"          -> open commitments
    "Catch me up on Ember."           -> profile, notes, and this week's
                                         activity (windows mentioning it)
    "Who's waiting on me?"            -> everything you owe, by due date
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import time

from . import memory

KINDS = ("person", "project", "organization", "place", "other")

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS entities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        aliases TEXT NOT NULL DEFAULT '[]',
        summary TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL,
        updated REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS entity_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER NOT NULL,
        ts REAL NOT NULL,
        text TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS commitments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER NOT NULL,
        direction TEXT NOT NULL,
        text TEXT NOT NULL,
        due REAL,
        done INTEGER NOT NULL DEFAULT 0,
        created REAL NOT NULL,
        reminder_id INTEGER
    )""",
]


def _conn():
    conn = memory.get_conn()
    for stmt in _SCHEMA:
        conn.execute(stmt)
    return conn


def _row(r) -> dict:
    return {
        "id": r[0],
        "kind": r[1],
        "name": r[2],
        "aliases": json.loads(r[3] or "[]"),
        "summary": r[4],
        "created": r[5],
        "updated": r[6],
    }


def all_entities(kind: str | None = None) -> list[dict]:
    conn = _conn()
    try:
        q = "SELECT id, kind, name, aliases, summary, created, updated FROM entities"
        rows = conn.execute(
            q + (" WHERE kind = ?" if kind else "") + " ORDER BY updated DESC", (kind,) if kind else ()
        ).fetchall()
    finally:
        conn.close()
    return [_row(r) for r in rows]


def find(name: str) -> dict | None:
    key = name.strip().lower()
    if not key:
        return None
    items = all_entities()
    for e in items:
        if e["name"].lower() == key or key in [a.lower() for a in e["aliases"]]:
            return e
    # "Hassan" should match "Hassan Ali", but "Ali" alone shouldn't match five people.
    starts = [e for e in items if e["name"].lower().split()[0] == key]
    if len(starts) == 1:
        return starts[0]
    names = {e["name"].lower(): e for e in items}
    close = difflib.get_close_matches(key, list(names), n=1, cutoff=0.85)
    return names[close[0]] if close else None


def upsert(name: str, kind: str = "person", summary: str = "", aliases: list[str] | None = None) -> dict:
    kind = kind if kind in KINDS else "other"
    now = time.time()
    existing = find(name)
    conn = _conn()
    try:
        if existing:
            merged = sorted({*existing["aliases"], *(aliases or [])} - {existing["name"]})
            conn.execute(
                "UPDATE entities SET summary = CASE WHEN ? = '' THEN summary ELSE ? END, aliases = ?, updated = ? "
                "WHERE id = ?",
                (summary, summary, json.dumps(merged), now, existing["id"]),
            )
            eid = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO entities (kind, name, aliases, summary, created, updated) VALUES (?, ?, ?, ?, ?, ?)",
                (kind, name.strip(), json.dumps(sorted(set(aliases or []))), summary, now, now),
            )
            eid = int(cur.lastrowid or 0)
        conn.commit()
    finally:
        conn.close()
    e = find(name) or next(x for x in all_entities() if x["id"] == eid)
    return e


def add_note(entity_id: int, text: str) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO entity_notes (entity_id, ts, text) VALUES (?, ?, ?)", (entity_id, time.time(), text.strip())
        )
        conn.execute("UPDATE entities SET updated = ? WHERE id = ?", (time.time(), entity_id))
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def delete_note(note_id: int) -> bool:
    conn = _conn()
    try:
        n = conn.execute("DELETE FROM entity_notes WHERE id = ?", (note_id,)).rowcount
        conn.commit()
        return n > 0
    finally:
        conn.close()


def notes(entity_id: int, limit: int = 15) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, ts, text FROM entity_notes WHERE entity_id = ? ORDER BY id DESC LIMIT ?", (entity_id, limit)
        ).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "ts": r[1], "text": r[2]} for r in rows]


def add_commitment(
    entity_id: int, text: str, direction: str = "i_owe", due: float | None = None, reminder_id: int | None = None
) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO commitments (entity_id, direction, text, due, created, reminder_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                entity_id,
                "they_owe" if direction == "they_owe" else "i_owe",
                text.strip(),
                due,
                time.time(),
                reminder_id,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def set_done(commitment_id: int, done: bool = True) -> dict | None:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id, entity_id, text, reminder_id, done FROM commitments WHERE id = ?", (commitment_id,)
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE commitments SET done = ? WHERE id = ?", (int(done), commitment_id))
        conn.commit()
    finally:
        conn.close()
    if done and row[3]:
        memory.cancel_reminder(row[3])
    return {"id": row[0], "text": row[2], "was_done": bool(row[4])}


def delete_commitment(commitment_id: int) -> bool:
    conn = _conn()
    try:
        row = conn.execute("SELECT reminder_id FROM commitments WHERE id = ?", (commitment_id,)).fetchone()
        n = conn.execute("DELETE FROM commitments WHERE id = ?", (commitment_id,)).rowcount
        conn.commit()
    finally:
        conn.close()
    if row and row[0]:
        memory.cancel_reminder(row[0])
    return n > 0


def commitments(entity_id: int | None = None, open_only: bool = True) -> list[dict]:
    conn = _conn()
    try:
        q = (
            "SELECT c.id, c.entity_id, e.name, c.direction, c.text, c.due, c.done, c.created FROM commitments c "
            "JOIN entities e ON e.id = c.entity_id WHERE 1=1"
        )
        args: list = []
        if entity_id is not None:
            q += " AND c.entity_id = ?"
            args.append(entity_id)
        if open_only:
            q += " AND c.done = 0"
        q += " ORDER BY c.due IS NULL, c.due, c.id"
        rows = conn.execute(q, args).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r[0],
            "entity_id": r[1],
            "name": r[2],
            "direction": r[3],
            "text": r[4],
            "due": r[5],
            "done": bool(r[6]),
            "created": r[7],
        }
        for r in rows
    ]


def fmt_commitment(c: dict) -> str:
    who = f"you owe {c['name']}" if c["direction"] == "i_owe" else f"{c['name']} owes you"
    due = ""
    if c["due"]:
        d = dt.datetime.fromtimestamp(c["due"])
        overdue = " OVERDUE" if c["due"] < time.time() and not c["done"] else ""
        due = f" (due {d.strftime('%a %d %b %I:%M %p')}{overdue})"
    state = " [done]" if c["done"] else ""
    return f"#{c['id']} {who}: {c['text']}{due}{state}"


def profile(e: dict) -> str:
    lines = [f"{e['name']} ({e['kind']})" + (f", also called {', '.join(e['aliases'])}" if e["aliases"] else "")]
    if e["summary"]:
        lines.append(e["summary"])
    ns = notes(e["id"])
    if ns:
        lines.append("Notes (newest first):")
        lines += [f"- [{dt.datetime.fromtimestamp(n['ts']).strftime('%d %b')}] {n['text']}" for n in ns]
    cs = commitments(e["id"])
    if cs:
        lines.append("Open commitments:")
        lines += [f"- {fmt_commitment(c)}" for c in cs]
    return "\n".join(lines)


def prompt_block(limit: int = 25) -> str:
    items = all_entities()[:limit]
    if not items:
        return ""
    owed = commitments()
    overdue = [c for c in owed if c["due"] and c["due"] < time.time() and c["direction"] == "i_owe"]
    names = ", ".join(f"{e['name']} ({e['kind']})" for e in items)
    text = f"PEOPLE & PROJECTS YOU KNOW (use recall_about for details): {names}"
    if overdue:
        text += "\nOVERDUE (the user owes): " + "; ".join(f"{c['text']} to {c['name']}" for c in overdue[:5])
    return text


def forget(entity_id: int) -> dict:
    """Delete a profile with its notes and commitments; pending reminders for those commitments are cancelled."""
    conn = _conn()
    try:
        reminders = [
            r[0]
            for r in conn.execute(
                "SELECT reminder_id FROM commitments WHERE entity_id = ? AND reminder_id IS NOT NULL AND done = 0",
                (entity_id,),
            )
        ]
        n_notes = conn.execute("DELETE FROM entity_notes WHERE entity_id = ?", (entity_id,)).rowcount
        n_comm = conn.execute("DELETE FROM commitments WHERE entity_id = ?", (entity_id,)).rowcount
        conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
        conn.commit()
    finally:
        conn.close()
    for rid in reminders:
        memory.cancel_reminder(rid)
    return {"notes": n_notes, "commitments": n_comm, "reminders": len(reminders)}
