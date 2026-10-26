"""
Everyday extras: timers, the daily briefing, news, saving screenshots, and
changing Jarvis's own settings by voice ("Jarvis, speak a bit faster").
"""

from __future__ import annotations

import datetime as dt
import html
import re
import time
import urllib.parse

import requests

from .. import config, memory, settings
from . import ToolResult, tool


@tool(
    "set_timer",
    "Start a countdown timer (shown live in the HUD, announced when it ends).",
    {
        "minutes": {"type": "number"},
        "seconds": {"type": "number"},
        "label": {"type": "string", "description": "e.g. 'tea', 'pomodoro'"},
    },
    category="reminders",
)
def set_timer(minutes: float = 0, seconds: float = 0, label: str = "") -> ToolResult:
    total = float(minutes or 0) * 60 + float(seconds or 0)
    if total < 1:
        return ToolResult("Give the timer a length, e.g. minutes=5.", ok=False)
    name = label.strip() or "timer"
    rid = memory.add_reminder(f"Your {name} timer is done.", time.time() + total, kind="timer", label=name)
    mins, secs = divmod(int(total), 60)
    length = f"{mins} min {secs} s" if mins and secs else (f"{mins} min" if mins else f"{secs} s")
    return ToolResult(f"Timer #{rid} ({name}) started for {length}.", ui={"type": "reminders_changed"})


def news_items(topic: str = "", limit: int = 6) -> list[dict]:
    if topic:
        url = f"https://news.google.com/rss/search?q={urllib.parse.quote(topic)}&hl=en&gl=US&ceid=US:en"
    else:
        url = "https://news.google.com/rss?hl=en&gl=US&ceid=US:en"
    xml = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"}).text
    items = []
    for block in re.findall(r"<item>(.*?)</item>", xml, re.DOTALL)[:limit]:
        title = re.search(r"<title>(.*?)</title>", block, re.DOTALL)
        when = re.search(r"<pubDate>(.*?)</pubDate>", block)
        src = re.search(r"<source[^>]*>(.*?)</source>", block)
        link = re.search(r"<link>(.*?)</link>", block)
        items.append(
            {
                "title": html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", title.group(1))).strip() if title else "",
                "source": html.unescape(src.group(1)) if src else "",
                "when": when.group(1)[:22] if when else "",
                "link": link.group(1) if link else "",
            }
        )
    return items


@tool(
    "news_headlines",
    "Latest news headlines, optionally about a topic (e.g. 'Pakistan', 'AI', 'cricket').",
    {"topic": {"type": "string"}, "limit": {"type": "integer"}},
    category="web",
)
def news_headlines(topic: str = "", limit: int = 6) -> str:
    items = news_items(topic, max(1, min(int(limit or 6), 12)))
    if not items:
        return "No headlines found."
    return "\n".join(f"- {i['title']} ({i['when']})" for i in items)


@tool(
    "daily_briefing",
    "A morning-style briefing: date/time, weather, reminders and timers, recent notes and "
    "top headlines. Use for 'brief me', 'what's my day like', 'good morning'.",
    category="assistant",
)
def daily_briefing() -> str:
    now = dt.datetime.now()
    out = [f"Now: {now.strftime('%A %d %B, %I:%M %p')}"]
    if config.HOME_CITY:
        try:
            from .web import weather_summary

            out.append("Weather: " + weather_summary(config.HOME_CITY).replace("\n", " | "))
        except Exception as e:
            out.append(f"Weather unavailable ({type(e).__name__}).")
    pending = memory.pending_reminders()
    if pending:
        out.append(
            "Reminders/timers: "
            + "; ".join(
                f"{dt.datetime.fromtimestamp(r['due_at']).strftime('%a %I:%M %p')} {r['text']}" for r in pending[:6]
            )
        )
    else:
        out.append("No reminders pending.")
    notes = memory.list_notes(3)
    if notes:
        out.append("Recent notes: " + "; ".join(n["content"][:80] for n in notes))
    try:
        out.append("Headlines: " + " | ".join(i["title"] for i in news_items(limit=4)))
    except Exception:
        pass
    return "\n".join(out)


@tool(
    "save_screenshot",
    "Save a screenshot of the screen to Pictures/Jarvis and return its path.",
    category="screen",
    ui_thread=True,
    mutates=True,
)
def save_screenshot() -> str:
    import mss
    from PIL import Image

    from ..platform_utils import resolve_path

    folder = resolve_path("pictures/Jarvis")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"screenshot-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    with mss.mss() as sct:
        raw = sct.grab(sct.monitors[1])
        Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX").save(path)
    return f"Saved {path}"


@tool(
    "jarvis_settings",
    "Change Jarvis's own settings: voice (one of the listed ids), speaking speed "
    "(slower/normal/brisk/fast), confirmation level (strict/normal/off), how to address the user, home city, "
    "or whether to start with Windows. Call with no arguments to read the current settings.",
    {
        "voice": {"type": "string", "enum": [v["id"] for v in settings.VOICES]},
        "speed": {"type": "string", "enum": list(settings.RATES)},
        "confirm_level": {"type": "string", "enum": list(settings.CONFIRM_LEVELS)},
        "address_as": {"type": "string"},
        "city": {"type": "string"},
        "start_with_windows": {"type": "boolean"},
    },
    category="assistant",
)
def jarvis_settings(
    voice: str | None = None,
    speed: str | None = None,
    confirm_level: str | None = None,
    address_as: str | None = None,
    city: str | None = None,
    start_with_windows: bool | None = None,
) -> ToolResult:
    changes: dict = {}
    if voice:
        changes["tts_voice"] = voice
    if speed in settings.RATES:
        changes["tts_rate"] = settings.RATES[speed]
    if confirm_level:
        changes["confirm_level"] = confirm_level
    if address_as:
        changes["user_title"] = address_as
    if city:
        changes["city"] = city
    if start_with_windows is not None:
        changes["startup"] = start_with_windows
    cur = settings.update(changes) if changes else settings.current()
    view = {k: v for k, v in cur.items() if k not in ("voices", "rates")}
    return ToolResult(f"Settings: {view}", ui={"type": "settings", "settings": cur})


@tool(
    "integrations_status",
    "List connected integrations (MCP servers, HTTP actions, plugins) and their tools.",
    category="assistant",
)
def integrations_status() -> str:
    from .. import integrations
    from . import REGISTRY

    rows = integrations.current_status()
    if not rows:
        return "No integrations configured. Add them in backend/integrations.json or backend/plugins/."
    lines = []
    for r in rows:
        line = f"- {r['name']} ({r['kind']}): {r['status']}, {r.get('tools', 0)} tools"
        if r.get("error"):
            line += f" - {r['error']}"
        lines.append(line)
    extra = [t.name for t in REGISTRY.values() if t.source != "builtin"]
    if extra:
        lines.append("Integration tools: " + ", ".join(sorted(extra))[:2000])
    return "\n".join(lines)


@tool(
    "reload_integrations",
    "Reload integrations.json and the plugins folder after the user changed them.",
    category="assistant",
)
def reload_integrations() -> ToolResult:
    from .. import integrations

    status = integrations.load()
    ok = sum(1 for s in status if s["status"] == "connected")
    bad = [s["name"] for s in status if s["status"] == "error"]
    msg = f"Reloaded: {ok} connected" + (f", failed: {', '.join(bad)}" if bad else "")
    return ToolResult(msg, ui={"type": "integrations", "items": status})
