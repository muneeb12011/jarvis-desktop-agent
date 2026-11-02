"""
Audit log: every request Jarvis handles and every tool it calls is written to
agent_log.db, so any run can be replayed or debugged afterwards.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import config

DB_PATH: Path = config.LOG_DB_PATH


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal TEXT NOT NULL,
            started_at REAL NOT NULL,
            ended_at REAL,
            status TEXT DEFAULT 'running'
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            step INTEGER NOT NULL,
            reasoning TEXT,
            action_type TEXT,
            action_json TEXT,
            result TEXT,
            created_at REAL NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            task TEXT NOT NULL,
            source TEXT,
            status TEXT NOT NULL,
            steps INTEGER DEFAULT 0,
            result TEXT DEFAULT '',
            started REAL NOT NULL,
            ended REAL,
            activity TEXT DEFAULT '[]'
        )"""
    )
    conn.commit()
    return conn


def start_run(goal: str) -> int:
    conn = get_conn()
    try:
        cur = conn.execute("INSERT INTO runs (goal, started_at) VALUES (?, ?)", (goal, time.time()))
        conn.commit()
        run_id = cur.lastrowid
    finally:
        conn.close()
    if run_id is None:
        raise RuntimeError("Failed to insert run - no row id returned.")
    return run_id


def log_action(run_id: int, step: int, reasoning: str, action_type: str, action_json: str, result: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO actions (run_id, step, reasoning, action_type, action_json, result, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, step, reasoning, action_type, action_json, result[:4000], time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def end_run(run_id: int, status: str = "completed") -> None:
    conn = get_conn()
    try:
        conn.execute("UPDATE runs SET ended_at = ?, status = ? WHERE id = ?", (time.time(), status, run_id))
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------ background jobs (survive a restart)

MAX_SAVED_JOBS = 200


def save_job(rec: dict) -> None:
    """Insert or update one background job's record."""
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO jobs (id, title, task, source, status, steps, result, started, ended, activity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rec["id"],
                rec["title"],
                rec["task"],
                rec.get("source", ""),
                rec["status"],
                rec.get("steps", 0),
                rec.get("result", ""),
                rec["started"],
                rec.get("ended"),
                json.dumps(rec.get("activity", []), default=str),
            ),
        )
        conn.execute(
            "DELETE FROM jobs WHERE id NOT IN (SELECT id FROM jobs ORDER BY started DESC LIMIT ?)", (MAX_SAVED_JOBS,)
        )
        conn.commit()
    finally:
        conn.close()


def _job_row(r: tuple) -> dict:
    try:
        activity = json.loads(r[9] or "[]")
    except ValueError:
        activity = []
    return {
        "id": r[0],
        "title": r[1],
        "task": r[2],
        "source": r[3],
        "status": r[4],
        "steps": r[5],
        "result": r[6] or "",
        "started": r[7],
        "ended": r[8],
        "activity": activity,
    }


_JOB_COLS = "id, title, task, source, status, steps, result, started, ended, activity"


def recent_jobs(limit: int = 20) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(f"SELECT {_JOB_COLS} FROM jobs ORDER BY started DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return [_job_row(r) for r in rows]


def get_job(job_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(f"SELECT {_JOB_COLS} FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    return _job_row(row) if row else None


def mark_interrupted_jobs() -> int:
    """Jobs that were queued or running when Jarvis last stopped did not finish; say so."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE jobs SET status = 'interrupted', ended = ?, "
            "result = CASE WHEN result = '' THEN 'Jarvis restarted before this finished.' ELSE result END "
            "WHERE status IN ('queued', 'running')",
            (time.time(),),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
