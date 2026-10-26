"""
Dynamic toolsets. Jarvis has 70+ tools once integrations are loaded; sending
every schema on every request wastes tokens, burns free-tier quota faster,
slows small local models to a crawl and makes tool choice worse.

So each request carries the CORE tools plus whichever groups are switched
on. Groups switch on automatically from keywords in your request, or the
model asks for them with the `enable_tools` meta-tool. Integration tools
(MCP servers, HTTP actions, plugins) are grouped by their source.
"""

from __future__ import annotations

import re

from .providers import ToolSpec
from .tools import Tool

CORE = {
    "get_datetime",
    "system_status",
    "open_app",
    "open_url",
    "web_search",
    "read_webpage",
    "weather",
    "play_youtube",
    "news_headlines",
    "daily_briefing",
    "remember",
    "recall",
    "set_reminder",
    "set_timer",
    "list_reminders",
    "media_control",
    "set_volume",
    "run_powershell",
    "screenshot",
    "click_element",
    "click",
    "type_text",
    "press_keys",
    "update_plan",
    "take_note",
    "start_background_job",
    "run_routine",
}

CORE |= {"undo_last", "undo_last_request", "remember_about", "recall_about", "add_commitment", "list_commitments"}

BUILTIN_GROUPS = {
    "files": ["list_dir", "read_file", "write_file", "find_files", "open_path"],
    "windows": ["list_windows", "focus_window", "window_action", "close_app", "power", "set_brightness"],
    "screen_extra": ["scroll", "drag", "wait", "save_screenshot"],
    "clipboard": ["clipboard_read", "clipboard_write"],
    "memory_extra": ["forget", "cancel_reminder", "list_notes"],
    "assistant": [
        "jarvis_settings",
        "integrations_status",
        "reload_integrations",
        "list_jobs",
        "cancel_job",
        "undo_history",
    ],
    "routines": ["create_routine", "list_routines", "delete_routine", "toggle_routine"],
    "activity": ["activity_report", "activity_search", "what_was_i_doing"],
    "workspaces": ["save_workspace", "restore_workspace", "list_workspaces", "delete_workspace"],
    "people": ["complete_commitment", "list_people", "forget_about"],
    "headsup": ["headsup", "recent_headsups", "read_window_text"],
    "meeting": [
        "start_meeting",
        "stop_meeting",
        "meeting_status",
        "meeting_notes",
        "list_meetings",
        "track_meeting_actions",
    ],
}
_NAME_TO_GROUP = {name: g for g, names in BUILTIN_GROUPS.items() for name in names}

GROUP_HELP = {
    "files": "browse, read, write, find and open files and folders",
    "windows": "list/focus/minimize/close windows and apps, brightness, lock/sleep/shutdown",
    "screen_extra": "scroll, drag, wait for UI, save a screenshot to disk",
    "clipboard": "read or set the clipboard",
    "memory_extra": "forget facts, cancel reminders, list notes",
    "assistant": "Jarvis's own settings, integrations status/reload, background job list/cancel",
    "routines": "create/list/delete automations that run on a schedule or when something happens",
    "activity": "the user's computer activity history: time spent per app, what they were doing when",
    "workspaces": "save and restore window layouts (e.g. a 'coding' setup)",
    "people": "mark promises done, list or forget known people and projects",
    "headsup": "screen-aware help on/off/mute, what it last pointed out, read a window's text (terminal output)",
    "meeting": "record/transcribe a call or meeting, its summary and action items, past meeting notes",
}

# Cheap keyword routing so common requests don't need an extra round trip.
_KEYWORDS: list[tuple[str, str]] = [
    (r"\b(file|folder|pdf|docx?|download|desktop|document|read the|save (it|this) (to|as)|csv)\b", "files"),
    (r"\b(window|minimi[sz]e|maximi[sz]e|close|lock|shut ?down|restart|sleep|brightness|switch to)\b", "windows"),
    (r"\b(clipboard|copied|paste)\b", "clipboard"),
    (r"\b(forget|cancel|notes?)\b", "memory_extra"),
    (r"\b(setting|voice|speak (faster|slower)|integration|plugin|background|jobs?)\b", "assistant"),
    (
        r"\b(routine|automat|every (day|morning|evening|week|hour|monday|weekday)|whenever|when i open|"
        r"each time|schedule)\b",
        "routines",
    ),
    (
        r"\b(what was i|how long|time spent|spent .* (on|in)|yesterday|earlier today|this morning|"
        r"productiv|activity|history)\b",
        "activity",
    ),
    (r"\b(workspace|layout|setup|arrange)\b", "workspaces"),
    (r"\b(done with|finished|completed|delivered|who do i know|people|projects|forget|don.t know)\b", "people"),
    (
        r"\b(heads.?up|errors?|terminal|traceback|what was that|that last|watch my|point(ing)? out|"
        r"read (the |my )?(window|terminal|output))\b",
        "headsup",
    ),
    (r"\b(meeting|call|standup|stand-up|minutes|transcri\w*|take notes|action items)\b", "meeting"),
    (r"\b(scroll|drag|wait for)\b", "screen_extra"),
    (
        r"(https?://|www\.|\.com\b|\.org\b|\.io\b|website|web ?page|browser|tab|log ?in|sign ?in|form|"
        r"checkout|github|gmail|youtube\.com)",
        "mcp:browser",
    ),
]


def group_of(tool: Tool) -> str:
    if tool.name in CORE:
        return "core"
    if tool.source != "builtin":
        return tool.source
    return _NAME_TO_GROUP.get(tool.name, tool.category)


def available_groups(tools: dict[str, Tool], allow_gui: bool = True) -> dict[str, list[Tool]]:
    groups: dict[str, list[Tool]] = {}
    for t in tools.values():
        if not allow_gui and t.ui_thread:
            continue
        g = group_of(t)
        if g != "core":
            groups.setdefault(g, []).append(t)
    return groups


def keyword_groups(text: str, known: set[str]) -> set[str]:
    low = text.lower()
    hits = {g for pattern, g in _KEYWORDS if re.search(pattern, low)}
    # Mentioning an integration by name switches it on ("ask github...", "home assistant").
    for g in known:
        if ":" in g:
            name = g.split(":", 1)[1].replace("_", " ")
            if name and name in low:
                hits.add(g)
    return hits & known


def meta_spec(groups: dict[str, list[Tool]], enabled: set[str]) -> ToolSpec | None:
    off = {g: ts for g, ts in groups.items() if g not in enabled}
    if not off:
        return None
    lines = []
    for g, ts in sorted(off.items()):
        if g in GROUP_HELP:
            desc = GROUP_HELP[g]
        else:
            first = ts[0].description.split("] ", 1)[-1].split(".")[0][:90]
            desc = f"{len(ts)} tools from the {g} integration, e.g. {first}"
        lines.append(f"- {g}: {desc}")
    return ToolSpec(
        "enable_tools",
        "Load more abilities before using them. Call this whenever the task needs a capability you don't "
        "currently have a tool for. Available groups:\n" + "\n".join(lines),
        {
            "type": "object",
            "properties": {"groups": {"type": "array", "items": {"type": "string", "enum": sorted(off)}}},
            "required": ["groups"],
        },
    )


def select(tools: dict[str, Tool], enabled: set[str], allow_gui: bool = True, threshold: int = 36) -> list[ToolSpec]:
    usable = {n: t for n, t in tools.items() if allow_gui or not t.ui_thread}
    if len(usable) <= threshold:
        return [t.spec() for t in usable.values()]
    specs = [t.spec() for t in usable.values() if group_of(t) == "core" or group_of(t) in enabled]
    meta = meta_spec(available_groups(usable, allow_gui), enabled)
    if meta:
        specs.append(meta)
    return specs
