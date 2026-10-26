"""
Self-check: after a task that changed something, a second, skeptical pass
looks at the evidence (what was actually done, the tool results, and a fresh
screenshot for screen tasks) and decides whether the request was really
accomplished. If not, Jarvis gets one more go with the checker's feedback;
if it still isn't done, Jarvis says so instead of claiming success.

This catches the classic agent failure: "Done!" when the click missed, the
file went to the wrong folder, or the form never submitted.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

PROMPT = """You are the quality checker for a desktop assistant. Decide whether the user's request was \
actually accomplished, using ONLY the evidence: the action log (tool calls and their results), the assistant's \
draft reply, and the screenshot if one is attached (taken just now, after the actions).

Be skeptical but fair:
- "done": the evidence clearly shows the request was fulfilled.
- "not_done": the evidence shows it failed, is incomplete, went to the wrong place, or the reply claims \
something the evidence contradicts.
- "unsure": the evidence can't tell either way.

Reply with JSON only, no prose:
{"verdict": "done" | "not_done" | "unsure", "reason": "one short sentence", "fix": "the next concrete step if not_done, else empty"}"""

# Tools whose effects are worth double-checking (in addition to any GUI action and any integration call).
CHECK_TOOLS = {
    "write_file",
    "run_powershell",
    "restore_workspace",
    "open_path",
    "create_routine",
    "save_workspace",
    "close_app",
    "window_action",
}


def needs_check(used: list) -> bool:
    for t in used:
        if t.ui_thread and t.mutates:
            return True
        if t.name in CHECK_TOOLS:
            return True
        if t.source != "builtin" and t.mutates:
            return True
    return False


def evidence(goal: str, history: list[dict], draft: str) -> str:
    """The action log since the user's request, compact enough for a cheap model call."""
    lines = [f"USER REQUEST: {goal}", "", "ACTION LOG:"]
    start = max(
        (i for i, m in enumerate(history) if m["role"] == "user" and m.get("text", "").endswith(goal)), default=0
    )
    for m in history[start:]:
        if m["role"] == "assistant":
            for tc in m.get("tool_calls", []):
                lines.append(f"- called {tc.name}({json.dumps(tc.args, default=str)[:300]})")
        elif m["role"] == "tool":
            lines.append(f"  -> {m['content'][:600]}")
    lines += ["", f"ASSISTANT'S DRAFT REPLY: {draft}"]
    return "\n".join(lines)[:12000]


def parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            verdict = str(data.get("verdict", "unsure")).lower()
            if verdict not in ("done", "not_done", "unsure"):
                verdict = "unsure"
            return {
                "verdict": verdict,
                "reason": str(data.get("reason", ""))[:300],
                "fix": str(data.get("fix", ""))[:300],
            }
        except json.JSONDecodeError:
            pass
    return {"verdict": "unsure", "reason": "checker gave no clear answer", "fix": ""}


def check(brain, goal: str, history: list[dict], draft: str, image_b64: str | None) -> dict:
    msg: dict = {"role": "user", "text": evidence(goal, history, draft)}
    if image_b64:
        msg["image_b64"] = image_b64
        msg["text"] += "\n\n(Screenshot attached: the screen right now.)"
    try:
        turn = brain.complete(PROMPT, [msg], [])
    except Exception as e:  # the check must never break the actual task
        logger.info("Self-check skipped: %s", e)
        return {"verdict": "unsure", "reason": "checker unavailable", "fix": ""}
    return parse(turn.text)
