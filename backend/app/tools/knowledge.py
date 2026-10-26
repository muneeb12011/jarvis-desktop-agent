"""
Knowledge tools: long-term memory about the user, searchable conversation
history, reminders, quick notes, and the visible task plan.
"""

from __future__ import annotations

import datetime as dt
import re

from .. import memory
from . import ToolResult, tool


@tool(
    "remember",
    "Save a durable fact about the user (preferences, people, projects, routines). "
    "Use whenever the user tells you something worth knowing next time.",
    {"fact": {"type": "string", "description": "One self-contained sentence"}},
    ["fact"],
    category="memory",
)
def remember(fact: str) -> str:
    memory.add_fact(fact, source="conversation")
    return f"Remembered: {fact}"


@tool(
    "forget",
    "Delete remembered facts that contain the given text.",
    {"text": {"type": "string"}},
    ["text"],
    category="memory",
)
def forget(text: str) -> str:
    removed = memory.forget_fact(text)
    return ("Forgot: " + "; ".join(removed)) if removed else f"Nothing remembered matches '{text}'."


@tool(
    "recall",
    "Search everything remembered and every past conversation for a topic.",
    {"query": {"type": "string"}},
    ["query"],
    category="memory",
)
def recall(query: str) -> str:
    q = query.lower()
    facts = [f for f in memory.get_facts(200) if any(w in f.lower() for w in re.findall(r"\w+", q))]
    convo = memory.search_conversation(query)
    out = []
    if facts:
        out.append("FACTS:\n" + "\n".join(f"- {f}" for f in facts[:15]))
    if convo:
        out.append(
            "PAST CONVERSATION:\n" + "\n".join(f"- [{c['when']}] {c['role']}: {c['content'][:240]}" for c in convo)
        )
    return "\n\n".join(out) or f"Nothing found about '{query}'."


def _parse_due(minutes_from_now: float | None, at: str | None) -> dt.datetime:
    now = dt.datetime.now()
    if minutes_from_now is not None and float(minutes_from_now) > 0:
        return now + dt.timedelta(minutes=float(minutes_from_now))
    if at:
        at = at.strip().upper().replace(".", "")
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %I:%M %p", "%Y-%m-%dT%H:%M:%S"):
            try:
                return dt.datetime.strptime(at, fmt)
            except ValueError:
                pass
        for fmt in ("%H:%M", "%I:%M %p", "%I %p", "%I:%M%p", "%I%p"):
            try:
                t = dt.datetime.strptime(at, fmt).time()
                due = dt.datetime.combine(now.date(), t)
                return due if due > now else due + dt.timedelta(days=1)
            except ValueError:
                pass
    raise ValueError("Give minutes_from_now or at (e.g. '17:30', '5:30 PM', '2026-10-21 09:00').")


@tool(
    "set_reminder",
    "Set a reminder. Jarvis will speak it at the right time (even if the window is hidden).",
    {
        "text": {"type": "string", "description": "What to remind about"},
        "minutes_from_now": {"type": "number"},
        "at": {"type": "string", "description": "Local time like '17:30', '5:30 PM' or '2026-10-21 09:00'"},
    },
    ["text"],
    category="reminders",
)
def set_reminder(text: str, minutes_from_now: float | None = None, at: str | None = None) -> ToolResult:
    try:
        due = _parse_due(minutes_from_now, at)
    except ValueError as e:
        return ToolResult(str(e), ok=False)
    rid = memory.add_reminder(text, due.timestamp())
    return ToolResult(
        f"Reminder #{rid} set for {due.strftime('%a %d %b, %I:%M %p')}: {text}", ui={"type": "reminders_changed"}
    )


@tool("list_reminders", "List pending reminders.", category="reminders")
def list_reminders() -> str:
    items = memory.pending_reminders()
    if not items:
        return "No pending reminders."
    return "\n".join(
        f"#{r['id']} {dt.datetime.fromtimestamp(r['due_at']).strftime('%a %d %b %I:%M %p')}: {r['text']}" for r in items
    )


@tool("cancel_reminder", "Cancel a reminder by its number.", {"id": {"type": "integer"}}, ["id"], category="reminders")
def cancel_reminder(id: int) -> ToolResult:  # noqa: A002 - matches the tool schema
    ok = memory.cancel_reminder(int(id))
    return ToolResult(
        f"Cancelled reminder #{id}." if ok else f"No pending reminder #{id}.", ok=ok, ui={"type": "reminders_changed"}
    )


@tool(
    "take_note",
    "Save a quick note (ideas, things to buy, snippets).",
    {"content": {"type": "string"}},
    ["content"],
    category="notes",
)
def take_note(content: str) -> str:
    nid = memory.add_note(content)
    return f"Note #{nid} saved."


@tool("list_notes", "Show recent notes.", {"limit": {"type": "integer"}}, category="notes")
def list_notes(limit: int = 15) -> str:
    notes = memory.list_notes(limit)
    return "\n".join(f"#{n['id']} [{n['when']}] {n['content']}" for n in notes) or "No notes yet."


@tool(
    "update_plan",
    "Show the user your step-by-step plan for a multi-step task, and update it as "
    "steps complete. Call this first for anything that needs 3+ actions.",
    {
        "steps": {"type": "array", "items": {"type": "string"}},
        "current": {"type": "integer", "description": "0-based index of the step in progress"},
    },
    ["steps"],
    category="agent",
)
def update_plan(steps: list[str], current: int = 0) -> ToolResult:
    return ToolResult("Plan shown to the user.", ui={"type": "plan", "steps": steps, "current": current})
