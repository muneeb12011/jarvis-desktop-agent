"""
Long-term memory. db.py logs WHAT Jarvis did (for replay); this stores WHAT
JARVIS KNOWS: facts about you, every conversation turn (full-text searchable),
reminders, notes, and a small key/value store for things like "last briefing".

Plain SQLite with FTS5 - no vector DB, no extra dependency. At personal scale
keyword search over your own history is fast and good enough, and the
interface is small enough to swap for embeddings later.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from . import config

DB_PATH: Path = config.MEMORY_DB_PATH

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        source TEXT DEFAULT 'inferred',
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS conversation (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        due_at REAL NOT NULL,
        created_at REAL NOT NULL,
        fired INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
]


def _fts_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS conversation_fts USING fts5(content, role UNINDEXED, "
            "created_at UNINDEXED)"
        )
    except sqlite3.OperationalError:
        return False
    # One-time backfill: conversations recorded before v3 had no FTS index.
    if conn.execute("SELECT count(*) FROM conversation_fts").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO conversation_fts (content, role, created_at) "
            "SELECT content, role, created_at FROM conversation"
        )
    return True


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    for stmt in _SCHEMA:
        conn.execute(stmt)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(reminders)")}
    if "kind" not in cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN kind TEXT NOT NULL DEFAULT 'reminder'")
        conn.execute("ALTER TABLE reminders ADD COLUMN label TEXT")
    _fts_available(conn)
    conn.commit()
    return conn


# ---------------- facts ----------------


def add_fact(content: str, source: str = "inferred") -> int:
    content = content.strip()
    conn = get_conn()
    try:
        existing = conn.execute("SELECT id FROM facts WHERE lower(content) = lower(?)", (content,)).fetchone()
        if existing:
            return int(existing[0])
        cur = conn.execute(
            "INSERT INTO facts (content, source, created_at) VALUES (?, ?, ?)",
            (content, source, time.time()),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def get_facts(limit: int = 40) -> list[str]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT content FROM facts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return [r[0] for r in reversed(rows)]


def list_facts_with_ids(limit: int = 100) -> list[tuple[int, str]]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT id, content FROM facts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return [(int(r[0]), r[1]) for r in reversed(rows)]


def forget_fact(query: str) -> list[str]:
    """Delete facts whose text contains `query` (case-insensitive). Returns
    what was removed."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, content FROM facts WHERE lower(content) LIKE ?", (f"%{query.lower()}%",)
        ).fetchall()
        conn.executemany("DELETE FROM facts WHERE id = ?", [(r[0],) for r in rows])
        conn.commit()
    finally:
        conn.close()
    return [r[1] for r in rows]


# ---------------- conversation ----------------


def add_turn(role: str, content: str) -> None:
    now = time.time()
    conn = get_conn()
    try:
        conn.execute("INSERT INTO conversation (role, content, created_at) VALUES (?, ?, ?)", (role, content, now))
        try:
            conn.execute(
                "INSERT INTO conversation_fts (content, role, created_at) VALUES (?, ?, ?)",
                (content, role, now),
            )
        except sqlite3.OperationalError:
            pass
        conn.commit()
    finally:
        conn.close()


def get_recent_conversation(limit: int = 20) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT role, content FROM conversation ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def search_conversation(query: str, limit: int = 8) -> list[dict]:
    """Full-text search over everything you've ever said to Jarvis."""
    words = re.findall(r"\w+", query.lower())
    if not words:
        return []
    conn = get_conn()
    try:
        try:
            fts_query = " OR ".join(f'"{w}"' for w in words)
            rows = conn.execute(
                "SELECT role, content, created_at FROM conversation_fts WHERE conversation_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (fts_query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            like = f"%{words[0]}%"
            rows = conn.execute(
                "SELECT role, content, created_at FROM conversation WHERE lower(content) LIKE ? "
                "ORDER BY id DESC LIMIT ?",
                (like, limit),
            ).fetchall()
    finally:
        conn.close()
    return [
        {"role": r[0], "content": r[1], "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(float(r[2])))}
        for r in rows
    ]


def build_context_block() -> str:
    """Text block of what Jarvis remembers, for injection into prompts."""
    facts = get_facts()
    convo = get_recent_conversation(limit=10)
    lines: list[str] = []
    if facts:
        lines.append("KNOWN FACTS ABOUT THE USER:")
        lines.extend(f"- {f}" for f in facts)
    if convo:
        lines.append("\nRECENT CONVERSATION (previous sessions):")
        lines.extend(f"- {c['role']}: {c['content'][:300]}" for c in convo)
    return "\n".join(lines) if lines else "(no memory yet)"


# ---------------- reminders ----------------


def add_reminder(text: str, due_at: float, kind: str = "reminder", label: str | None = None) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO reminders (text, due_at, created_at, kind, label) VALUES (?, ?, ?, ?, ?)",
            (text, due_at, time.time(), kind, label),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _rows_to_reminders(rows) -> list[dict]:
    return [{"id": r[0], "text": r[1], "due_at": r[2], "kind": r[3], "label": r[4] or r[1]} for r in rows]


def pending_reminders() -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, text, due_at, kind, label FROM reminders WHERE fired = 0 ORDER BY due_at"
        ).fetchall()
    finally:
        conn.close()
    return _rows_to_reminders(rows)


def pop_due_reminders(now: float | None = None) -> list[dict]:
    now = now or time.time()
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, text, due_at, kind, label FROM reminders WHERE fired = 0 AND due_at <= ? ORDER BY due_at",
            (now,),
        ).fetchall()
        conn.executemany("UPDATE reminders SET fired = 1 WHERE id = ?", [(r[0],) for r in rows])
        conn.commit()
    finally:
        conn.close()
    return _rows_to_reminders(rows)


def cancel_reminder(reminder_id: int) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("UPDATE reminders SET fired = 1 WHERE id = ? AND fired = 0", (reminder_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------------- notes ----------------


def add_note(content: str) -> int:
    conn = get_conn()
    try:
        cur = conn.execute("INSERT INTO notes (content, created_at) VALUES (?, ?)", (content, time.time()))
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def list_notes(limit: int = 20) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT id, content, created_at FROM notes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return [
        {"id": r[0], "content": r[1], "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(float(r[2])))}
        for r in rows
    ]


# ---------------- key/value ----------------


def kv_get(key: str, default: str | None = None) -> str | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else default


def kv_set(key: str, value: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?", (key, value, value)
        )
        conn.commit()
    finally:
        conn.close()
