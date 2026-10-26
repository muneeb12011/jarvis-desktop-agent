"""
Tools for people & project memory: profiles, dated notes, and commitments
("I promised Hassan the docs by Friday", "Shahzain owes me feedback").
"""

from __future__ import annotations

from .. import activity, memory, people
from . import ToolResult, tool
from .knowledge import _parse_due


@tool(
    "remember_about",
    "Save something about a specific person or project (who they are, preferences, decisions, what happened). "
    "Use whenever the user mentions a named person/project with a detail worth keeping. Creates the profile if "
    "it's new.",
    {
        "name": {"type": "string", "description": "Person or project name as the user calls it"},
        "note": {"type": "string", "description": "One self-contained sentence"},
        "kind": {"type": "string", "enum": list(people.KINDS)},
        "summary": {
            "type": "string",
            "description": "Optional one-line 'who/what this is' (e.g. 'backend lead at work')",
        },
        "aliases": {"type": "array", "items": {"type": "string"}},
    },
    ["name", "note"],
    category="people",
)
def remember_about(
    name: str, note: str, kind: str = "person", summary: str = "", aliases: list[str] | None = None
) -> ToolResult:
    e = people.upsert(name, kind, summary, aliases)
    nid = people.add_note(e["id"], note)
    return ToolResult(f"Saved note #{nid} about {e['name']}.")


@tool(
    "recall_about",
    "Everything known about a person or project: profile, notes, open commitments both ways, and (for projects "
    "and people) recent computer activity mentioning them. Use for 'catch me up on X', 'what do I know about X'.",
    {"name": {"type": "string"}},
    ["name"],
    category="people",
)
def recall_about(name: str) -> str:
    e = people.find(name)
    if not e:
        known = ", ".join(x["name"] for x in people.all_entities()[:20]) or "nobody yet"
        return f"I have no profile for '{name}'. Known: {known}."
    text = people.profile(e)
    seen = activity.search(e["name"], days=7, limit=5)
    if not seen.startswith("Nothing"):
        text += "\nRecent activity mentioning them (last 7 days):\n" + seen
    return text


@tool(
    "add_commitment",
    "Track a promise. direction 'i_owe' = the user promised something to them; 'they_owe' = they promised the "
    "user. Optional due as minutes_from_now or at ('Friday 17:00' not supported; use 'YYYY-MM-DD HH:MM' or "
    "'17:30'). For things the user owes with a due time, a reminder is set automatically.",
    {
        "name": {"type": "string"},
        "text": {"type": "string", "description": "What was promised"},
        "direction": {"type": "string", "enum": ["i_owe", "they_owe"]},
        "minutes_from_now": {"type": "number"},
        "at": {"type": "string"},
        "kind": {"type": "string", "enum": list(people.KINDS)},
    },
    ["name", "text"],
    category="people",
)
def add_commitment(
    name: str,
    text: str,
    direction: str = "i_owe",
    minutes_from_now: float | None = None,
    at: str | None = None,
    kind: str = "person",
) -> ToolResult:
    e = people.find(name) or people.upsert(name, kind)
    due = None
    reminder_id = None
    if minutes_from_now is not None or at:
        try:
            due = _parse_due(minutes_from_now, at).timestamp()
        except ValueError as err:
            return ToolResult(str(err), ok=False)
        if direction != "they_owe":
            reminder_id = memory.add_reminder(f"You promised {e['name']}: {text}", due)
    cid = people.add_commitment(e["id"], text, direction, due, reminder_id)
    c = people.commitments(e["id"])
    line = next((people.fmt_commitment(x) for x in c if x["id"] == cid), text)
    extra = " A reminder is set for the due time." if reminder_id else ""
    return ToolResult(
        f"Tracked commitment #{cid}: {line}.{extra}", ui={"type": "reminders_changed"} if reminder_id else None
    )


@tool(
    "list_commitments",
    "Open promises, optionally for one person/project. Use for 'what do I owe X', 'who's waiting on me', "
    "'what am I waiting for'.",
    {"name": {"type": "string"}, "include_done": {"type": "boolean"}},
    category="people",
)
def list_commitments(name: str = "", include_done: bool = False) -> str:
    eid = None
    if name:
        e = people.find(name)
        if not e:
            return f"No profile for '{name}'."
        eid = e["id"]
    items = people.commitments(eid, open_only=not include_done)
    if not items:
        return "No open commitments." if not include_done else "No commitments recorded."
    owe = [people.fmt_commitment(c) for c in items if c["direction"] == "i_owe"]
    owed = [people.fmt_commitment(c) for c in items if c["direction"] == "they_owe"]
    out = []
    if owe:
        out.append("You owe:\n" + "\n".join(f"- {x}" for x in owe))
    if owed:
        out.append("Owed to you:\n" + "\n".join(f"- {x}" for x in owed))
    return "\n\n".join(out)


@tool(
    "complete_commitment",
    "Mark a commitment as done (by its number).",
    {"id": {"type": "integer"}},
    ["id"],
    category="people",
)
def complete_commitment(id: int) -> ToolResult:  # noqa: A002
    r = people.set_done(int(id), True)
    if not r:
        return ToolResult(f"No commitment #{id}.", ok=False)
    return ToolResult(f"Marked commitment #{id} done: {r['text']}", ui={"type": "reminders_changed"})


@tool(
    "list_people",
    "List the people and projects Jarvis has profiles for.",
    {"kind": {"type": "string", "enum": list(people.KINDS)}},
    category="people",
)
def list_people(kind: str = "") -> str:
    items = people.all_entities(kind or None)
    if not items:
        return "No profiles yet."
    return "\n".join(f"- {e['name']} ({e['kind']})" + (f": {e['summary']}" if e["summary"] else "") for e in items)


@tool(
    "forget_about",
    "Delete everything saved about a person or project: profile, notes and commitments (their reminders are "
    "cancelled). Use when the user says the profile is wrong, they don't know this person, or asks to forget them.",
    {"name": {"type": "string"}},
    ["name"],
    category="people",
)
def forget_about(name: str) -> ToolResult:
    e = people.find(name)
    if not e:
        return ToolResult(f"I have no profile for '{name}'.", ok=False)
    r = people.forget(e["id"])
    return ToolResult(
        f"Forgot {e['name']}: removed {r['notes']} notes and {r['commitments']} commitments"
        + (f", cancelled {r['reminders']} reminder(s)." if r["reminders"] else "."),
        ui={"type": "reminders_changed"} if r["reminders"] else None,
    )
