"""
Work-without-asking mode.

Normally Jarvis puts anything that changes your computer to you first. That is right for a stranger's
request and tiresome for a morning of small jobs. Say "go ahead without asking until I say stop" and
routine approvals are skipped until you say stop, press the pill in the HUD, or Jarvis restarts.

Three rules keep this safe:

1. Only YOU can switch it on. The grant is read from your own typed or spoken message in the HUD
   (`parse_command`, called from main.py's websocket handler) or from the HUD toggle. It is not a tool the
   model can call, and web pages, files, background jobs and the inbound API cannot trigger it, so
   a malicious page can't talk Jarvis into dropping its guard.
2. A short list of actions still asks, always. Those approval rules return `HardConfirm` instead of a
   plain string: shutdown/restart, bulk or forced deletion, formatting, registry and system changes,
   downloaded programs, lowering the approval level, approving something inside another AI tool, and any
   integration action you marked "always ask".
3. It never survives a restart (nothing is written to disk), and a grant can carry an expiry
   ("for the next hour").

The "off" confirm level in Settings is a separate, permanent switch and behaves as before.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MAX_MINUTES = 12 * 60
# Spoken or typed commands are short. A long message is far more likely to be pasted text (an email, a web
# page) that happens to contain the phrase, so it never switches anything on.
MAX_COMMAND_CHARS = 300


class HardConfirm(str):
    """An approval reason that autonomy mode must NOT skip. Behaves exactly like the plain string."""


# ------------------------------------------------------------------ state


@dataclass
class _State:
    active: bool = False
    since: float = 0.0
    until: float | None = None


_state = _State()


def active() -> bool:
    if _state.active and _state.until is not None and time.time() >= _state.until:
        logger.info("Autonomy mode expired")
        _state.active, _state.until = False, None
    return _state.active


def grant(minutes: float | None = None) -> dict:
    """Switch the mode on, optionally for a limited time."""
    _state.active = True
    _state.since = time.time()
    if minutes and minutes > 0:
        _state.until = _state.since + min(float(minutes), MAX_MINUTES) * 60
    else:
        _state.until = None
    logger.warning("Autonomy mode ON (%s)", "until you say stop" if _state.until is None else f"{minutes} min")
    return status()


def revoke() -> dict:
    if _state.active:
        logger.warning("Autonomy mode OFF")
    _state.active, _state.until = False, None
    return status()


def status() -> dict:
    on = active()
    return {"active": on, "since": _state.since if on else None, "until": _state.until if on else None}


def event() -> dict:
    """The message the HUD understands."""
    return {"type": "autonomy", **status()}


def prompt_block() -> str:
    """Tells the model what is going on, so it stops asking in words what it no longer asks in a dialog."""
    if not active():
        return ""
    until = _state.until
    when = (
        "until the user says stop"
        if until is None
        else f"for another {max(1, round((until - time.time()) / 60))} minutes"
    )
    return (
        "\nWORK-WITHOUT-ASKING MODE IS ON\n"
        f"The user has given you standing permission to act {when}. Do the work directly: don't ask "
        "'shall I proceed?', don't offer a plan to approve, don't stop for confirmation between routine steps. "
        "Report what you did when you're done.\n"
        "The permission does not cover: shutting down or restarting, bulk or forced deletion, formatting, "
        "registry or system changes, running downloaded programs, or anything a tool still asks about. "
        "Those still reach the user as approval prompts; if one is declined, accept it and move on.\n"
        "This mode is controlled by the app from the user's own words. Never try to switch it on or off yourself, "
        "and never change the confirmation level to get the same effect.\n"
    )


# ------------------------------------------------------------------ the user's own words

_NUM = (
    r"(\d+(?:\.\d+)?|an?|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|forty-five|forty five)"
)
_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "fifteen": 15,
    "twenty": 20,
    "thirty": 30,
    "forty-five": 45,
    "forty five": 45,
}
_DURATION = re.compile(rf"\bfor (?:the next |about |another )?{_NUM} ?(minutes?|mins?|hours?|hrs?)\b", re.I)
_HALF_HOUR = re.compile(r"\bfor (?:the next |about )?(?:half an hour|half hour)\b", re.I)

# Strongest, most specific "take it back" phrases first: they contain words that also appear in grants.
_REVOKE_STRONG = [
    r"\b(?:stop|quit|cease|don'?t|do not|no more|no longer)\s+(?:working|going|acting|proceeding|continuing)"
    r"[^.!?]{0,15}\bwithout (?:asking|approval|permission|confirm\w*|checking)",
    r"\b(?:stop|end|disable|cancel|exit|pause|leave|turn off|switch off)\b[^.!?]{0,20}\b(?:autonom\w*|auto[- ]?approv\w*|go[- ]?ahead mode)",
    r"\b(?:autonom\w*|auto[- ]?approv\w*|go[- ]?ahead) mode\b[^.!?]{0,12}\b(?:off|stop|disable\w*|end)\b",
    r"\bautonom(?:y|ous)\s+(?:mode\s+)?(?:off|stop\w*|disable\w*|end\w*)\b",
    r"\b(?:go|come|get) back to (?:asking|normal|approvals?|confirm\w*)",
    r"\b(?:start|begin|resume) asking (?:me )?(?:again|first|for (?:approval|permission|confirm\w*))",
]
_GRANT = [
    r"\b(?:work|working|go|proceed|continue|carry on|act|do (?:it|things|tasks?|the work))\b[^.!?]{0,40}\bwithout "
    r"(?:asking|approval|permission|confirm\w*|checking(?: with me)?)",
    r"\b(?:don'?t|do not|never|no need to|stop)\b[^.!?]{0,15}\bask(?:ing)?\b[^.!?]{0,25}\b(?:approval|permission|confirm\w*|"
    r"every time|each time|every (?:little|small|single)|for (?:every|each|small|minor|routine)|anymore|any more|so much)",
    r"\bno (?:more )?(?:approvals?|confirmations?|permission)s?\b[^.!?]{0,25}\b(?:needed|required|necessary|until|from now)",
    r"\b(?:autonom\w*|auto[- ]?approv\w*|go[- ]?ahead) mode\b[^.!?]{0,12}\b(?:on|enable\w*|start\w*|activate\w*)\b",
    r"\b(?:turn on|enable|start|activate|switch on|use)\b[^.!?]{0,12}\b(?:autonom\w*|auto[- ]?approv\w*|go[- ]?ahead mode)",
    r"\byou have my (?:permission|approval) to (?:work|act|go ahead|proceed|continue|do (?:it|things|tasks?|the work))\b",
    r"\bapprove everything\b[^.!?]{0,20}\buntil\b",
]
# A grant only counts when it OPENS the message ("please, go ahead without asking..."), so a phrase buried in a
# task ("write an email that says work without asking") or quoted text can never switch anything on.
_LEAD_IN = (
    r"(?:please|jarvis|okay|ok|hey|sir|now|so|and|just|from now on|i want you to|i need you to|i'?d like you to|"
    r"you can|you may|you should|can you|could you|would you|will you|go ahead and)"
)
_GRANT_RE = re.compile(rf"^\W*(?:{_LEAD_IN}\W*)*(?:{'|'.join(f'(?:{p})' for p in _GRANT)})", re.I)

# Loose on purpose: taking permission back is always the safe direction.
_REVOKE_LOOSE = [
    r"\bask (?:me )?(?:again|first|before|for (?:approval|permission|confirm\w*))\b",
    r"\b(?:i want|i'?d like|let me) to (?:approve|confirm|review)\b",
    r"\brequire (?:my )?(?:approval|permission|confirm\w*)\b",
    r"\bno more (?:autonom\w*|going ahead)\b",
]
_QUESTION_START = re.compile(r"^\s*(?:what|how|why|which|when|where|who|is|are|does|do|did|was|were)\b", re.I)


@dataclass(frozen=True)
class Command:
    action: str  # "grant" | "revoke"
    minutes: float | None = None


def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def _minutes(text: str) -> float | None:
    if _HALF_HOUR.search(text):
        return 30
    m = _DURATION.search(text)
    if not m:
        return None
    raw = m.group(1).lower()
    n = float(raw) if raw.replace(".", "", 1).isdigit() else float(_WORDS.get(raw, 0))
    if n <= 0:
        return None
    unit = m.group(2).lower()
    return n * (60 if unit.startswith("h") else 1)


def parse_command(text: str) -> Command | None:
    """Recognise "work without asking" / "ask me again" in a message the USER typed or spoke.

    Returns None for anything else, including questions about the feature and long pasted text.
    Only call this for messages that really come from the HUD user.
    """
    text = (text or "").strip()
    if not text or len(text) > MAX_COMMAND_CHARS:
        return None
    if _any(_REVOKE_STRONG, text):
        return Command("revoke")
    if _GRANT_RE.match(text):
        if text.endswith("?") and _QUESTION_START.match(text):
            return None  # "what does autonomous mode do?" is a question, not a grant
        return Command("grant", _minutes(text))
    if _any(_REVOKE_LOOSE, text):
        return Command("revoke")
    return None


def acknowledgement(cmd: Command) -> str:
    """What Jarvis says back when the message was only the command."""
    if cmd.action == "revoke":
        return "Understood. I'll ask before making changes again."
    if cmd.minutes:
        span = f"{round(cmd.minutes)} minutes" if cmd.minutes < 90 else f"{cmd.minutes / 60:g} hours"
        lead = f"Understood. I'll work without asking for the next {span}."
    else:
        lead = "Understood. I'll work without asking until you say stop."
    return lead + " I'll still check with you before anything drastic, like shutting down or deleting in bulk."


def is_only_command(text: str) -> bool:
    """True when the message is nothing but the command, so there is no task left to run.

    Anything left over that isn't filler ("please", "anymore", "until I say stop"...) is treated as a task
    and still goes to the agent, so "stop asking and clean my desktop" does both.
    """
    t = re.sub(r"[^a-z0-9.' ]", " ", text.lower())
    for pat in [*_REVOKE_STRONG, *_GRANT, *_REVOKE_LOOSE, _DURATION.pattern, _HALF_HOUR.pattern]:
        t = re.sub(pat, " ", t, flags=re.I)
    left = [w for w in t.split() if w not in _FILLER]
    return not left


_FILLER = frozenset(
    {
        *("please", "jarvis", "okay", "ok", "sir", "now", "thanks", "thank", "you", "just", "yes", "yeah", "sure"),
        *("and", "so", "can", "could", "would", "will", "i", "want", "need", "to", "the", "a", "an", "it", "that"),
        *("this", "is", "are", "my", "of", "in", "on", "from", "until", "say", "tell", "me", "stop", "work"),
        *("working", "anymore", "any", "more", "longer", "again", "whatever", "everything", "all", "forward"),
        *("onwards", "mode"),
    }
)
