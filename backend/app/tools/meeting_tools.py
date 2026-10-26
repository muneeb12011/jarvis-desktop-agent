"""
Tools for the meeting copilot: start/stop recording a call, recall past meetings, track action items.
"""

from __future__ import annotations

import datetime as dt

from .. import meeting
from . import ToolResult, tool


def _copilot() -> meeting.Copilot:
    if meeting.COPILOT is None:
        raise RuntimeError("Meeting copilot isn't running.")
    return meeting.COPILOT


@tool(
    "start_meeting",
    "Start taking notes on a meeting or call: transcribes the user's mic ('You') and the PC's audio ('Them') "
    "locally until stopped. Use for 'take notes on this call', 'record this meeting', 'start meeting notes'.",
    {"title": {"type": "string", "description": "Optional meeting name, e.g. 'Sprint planning'"}},
    category="meeting",
    mutates=True,
)
def start_meeting(title: str = "") -> str:
    return _copilot().start(title)


@tool(
    "stop_meeting",
    "Stop the meeting recording, then write the summary, decisions and action items and save the notes.",
    category="meeting",
    mutates=True,
)
def stop_meeting() -> ToolResult:
    if not _copilot().recording:
        return ToolResult("No meeting is being recorded.", ok=False)
    return ToolResult("Stopping the recording and writing the notes...", ui={"type": "meeting_stop"})


@tool("meeting_status", "Whether a meeting is being recorded, for how long, and the latest lines.", category="meeting")
def meeting_status() -> str:
    c = _copilot()
    m = c.meeting
    if not m:
        return "Not recording a meeting."
    tail = "\n".join(f"{ln.speaker}: {ln.text}" for ln in m.lines[-6:]) or "(nothing transcribed yet)"
    return f"Recording '{m.title}' for {meeting._mmss(m.elapsed())}. Latest:\n{tail}"


@tool(
    "meeting_notes",
    "Notes from a past meeting: summary, decisions, action items, and (if asked) transcript lines. Give a meeting "
    "id, or words to search for ('the pricing call'), or nothing for the most recent one.",
    {
        "id": {"type": "integer"},
        "query": {"type": "string"},
        "include_transcript": {"type": "boolean"},
    },
    category="meeting",
)
def meeting_notes(id: int | None = None, query: str = "", include_transcript: bool = False) -> str:  # noqa: A002
    if query and not id:
        hits = meeting.search(query)
        if not hits:
            return f"No meeting mentions '{query}'."
        rec = hits[0]
        others = ", ".join(f"#{h['id']} {h['title']}" for h in hits[1:])
    else:
        rec = meeting.get(id)
        others = ""
        if not rec:
            return "No saved meetings yet."
    when = dt.datetime.fromtimestamp(rec["started"]).strftime("%a %d %b %I:%M %p")
    out = [f"Meeting #{rec['id']} '{rec['title']}' ({when}). Notes file: {rec['path']}", rec.get("summary", "")]
    if rec.get("decisions"):
        out.append("Decisions:\n" + "\n".join(f"- {d}" for d in rec["decisions"]))
    if rec["action_items"]:
        out.append(
            "Action items:\n"
            + "\n".join(
                f"- {a['owner']}: {a['task']}"
                + (f" (for {a['for']})" if a.get("for") else "")
                + (f", due {a['due']}" if a.get("due") else "")
                for a in rec["action_items"]
            )
        )
    if include_transcript or query:
        t = rec["transcript"]
        if query:
            words = [w for w in query.lower().split() if len(w) > 2]
            lines = [ln for ln in t.split("\n") if any(w in ln.lower() for w in words)]
            t = "\n".join(lines[:40]) or t[-3000:]
        out.append("Transcript:\n" + t[-6000:])
    if others:
        out.append(f"Other matches: {others}")
    return "\n\n".join(x for x in out if x)


@tool("list_meetings", "Recent meetings Jarvis took notes on.", category="meeting")
def list_meetings() -> str:
    items = meeting.recent()
    if not items:
        return "No saved meetings yet."
    return "\n".join(
        f"#{m['id']} {m['title']} ({dt.datetime.fromtimestamp(m['started']).strftime('%a %d %b %I:%M %p')})"
        for m in items
    )


@tool(
    "track_meeting_actions",
    "Turn a meeting's action items into reminders and promises (yours become reminders/promises, other people's "
    "become 'owed to you'). Only when the user asks for it. Defaults to the latest meeting.",
    {"id": {"type": "integer"}},
    category="meeting",
    mutates=True,
)
def track_meeting_actions(id: int | None = None) -> ToolResult:  # noqa: A002
    return ToolResult(meeting.track_actions(id), ui={"type": "reminders_changed"})
