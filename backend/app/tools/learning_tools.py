"""Tools to look at, and take back, what Jarvis has learned about how the user likes things done."""

from __future__ import annotations

from .. import learning
from . import tool


@tool(
    "list_lessons",
    "Show what you have learned about how the user likes things done (their standing instructions and "
    "corrections). Use for 'what have you learned about me', 'what are my rules', 'why did you do it that way'.",
    category="memory",
)
def list_lessons() -> str:
    items = learning.all_lessons()
    if not items:
        return "I haven't learned any preferences yet. Tell me things like 'from now on...' and I'll keep them."
    return "\n".join(f"- [{le.id}] {le.text}" + (f" (used {le.uses}x)" if le.uses else "") for le in items[:40])


@tool(
    "forget_lesson",
    "Delete something you learned, by its id from list_lessons or by words it contains. Use when the user says a "
    "rule is wrong, outdated, or asks you to forget it.",
    {"which": {"type": "string", "description": "Lesson id, or words from it"}},
    ["which"],
    category="memory",
)
def forget_lesson(which: str) -> str:
    removed = learning.forget(which)
    return ("Forgot: " + "; ".join(removed)) if removed else f"I haven't learned anything matching '{which}'."
