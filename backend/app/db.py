"""
Audit log: every request Jarvis handles and every tool it calls is written to
agent_log.db, so any run can be replayed or debugged afterwards.
"""

from __future__ import annotations

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
