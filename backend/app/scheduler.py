"""
Proactive behaviour - things Jarvis does without being asked:

- Fires reminders on time (spoken, and the HUD pops up even if hidden).
- Warns once when the battery is low and not charging.
- Greets you with a briefing (time, weather, pending reminders) the first
  time you connect after a few hours away.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Awaitable, Callable

from . import config, memory

logger = logging.getLogger(__name__)

Broadcast = Callable[[dict], Awaitable[None]]

TICK_SECONDS = 2
STATS_SECONDS = 4
WEATHER_SECONDS = 15 * 60
BATTERY_WARN_SECONDS = 120
GREETING_GAP_HOURS = 4

_weather_cache: dict = {"text": None, "at": 0.0}


def reminders_payload() -> dict:
    return {"type": "reminders", "items": memory.pending_reminders()}


def _stats() -> dict:
    try:
        import psutil
    except ImportError:
        return {}
    out: dict = {
        "cpu": psutil.cpu_percent(interval=None),
        "ram": psutil.virtual_memory().percent,
    }
    b = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if b:
        out["battery"] = round(b.percent)
        out["plugged"] = bool(b.power_plugged)
    if config.HOME_CITY and time.time() - _weather_cache["at"] > WEATHER_SECONDS:
        _weather_cache["at"] = time.time()
        try:
            from .tools.web import weather_brief

            _weather_cache["text"] = weather_brief(config.HOME_CITY, timeout=6)
        except Exception:
            pass
    if _weather_cache["text"]:
        out["weather"] = _weather_cache["text"]
        out["city"] = config.HOME_CITY
    return out


async def run_scheduler(broadcast: Broadcast, has_clients: Callable[[], bool] = lambda: True) -> None:
    last_stats = 0.0
    last_battery_check = 0.0
    battery_warned = False
    while True:
        try:
            due = memory.pop_due_reminders()
            for r in due:
                text = r["text"] if r["kind"] == "timer" else f"Reminder, {config.address()}: {r['text']}"
                logger.info("%s fired: %s", r["kind"], r["text"])
                await broadcast({"type": "reminder", "id": r["id"], "kind": r["kind"], "text": text, "speak": True})
            if due:
                await broadcast(reminders_payload())

            now = time.time()
            if has_clients() and now - last_stats > STATS_SECONDS:
                last_stats = now
                stats = await asyncio.to_thread(_stats)
                if stats:
                    await broadcast({"type": "stats", **stats})

            if now - last_battery_check > BATTERY_WARN_SECONDS:
                last_battery_check = now
                level = await asyncio.to_thread(_battery)
                if level is not None:
                    pct, plugged = level
                    if pct <= 15 and not plugged and not battery_warned:
                        battery_warned = True
                        await broadcast(
                            {
                                "type": "reminder",
                                "kind": "system",
                                "text": f"Battery's at {pct:.0f} percent, {config.address()}. You may want to plug in.",
                                "speak": True,
                            }
                        )
                    elif plugged or pct > 25:
                        battery_warned = False
        except Exception:
            logger.exception("Scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)


def _battery() -> tuple[float, bool] | None:
    try:
        import psutil

        b = psutil.sensors_battery()
        return (b.percent, bool(b.power_plugged)) if b else None
    except Exception:
        return None


def _part_of_day(hour: int) -> str:
    if hour < 5:
        return "Burning the midnight oil"
    if hour < 12:
        return "Good morning"
    if hour < 17:
        return "Good afternoon"
    return "Good evening"


def build_greeting(force: bool = False) -> str | None:
    """A short spoken briefing, at most once per few hours. No LLM call."""
    now = dt.datetime.now()
    last = memory.kv_get("last_greeting")
    if not force and last and (time.time() - float(last)) < GREETING_GAP_HOURS * 3600:
        return None
    memory.kv_set("last_greeting", str(time.time()))

    who = config.USER_NAME or config.address()
    parts = [f"{_part_of_day(now.hour)}, {who}. It's {now.strftime('%I:%M %p').lstrip('0')}."]
    if config.HOME_CITY:
        try:
            from .tools.web import weather_brief

            brief = weather_brief(config.HOME_CITY)
            if brief:
                parts.append(f"{brief[0].upper()}{brief[1:]} in {config.HOME_CITY}.")
        except Exception:
            pass
    pending = memory.pending_reminders()
    today_end = dt.datetime.combine(now.date(), dt.time(23, 59)).timestamp()
    today = [r for r in pending if r["due_at"] <= today_end]
    if today:
        nxt = today[0]
        when = dt.datetime.fromtimestamp(nxt["due_at"]).strftime("%I:%M %p").lstrip("0")
        more = f", and {len(today) - 1} more after that" if len(today) > 1 else ""
        parts.append(f"Next reminder at {when}: {nxt['text']}{more}.")
    return " ".join(parts)
