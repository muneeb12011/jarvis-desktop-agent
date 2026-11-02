"""
Learning: how Jarvis gets better at working for YOU.

Two things, both visible to you and both easy to undo:

LESSONS: short rules about how you like things done.
  - Standing instructions you give ("from now on put screenshots in D:\\Shots", "never open Chrome, use Edge")
    are kept as they are.
  - A correction ("no, I meant the other folder") is distilled by the model into one lasting rule, or
    dropped if it was only about that one request.
  Lessons are ranked against each new request and the few that matter are shown to the model, so it doesn't
  carry everything everywhere. Repeating a lesson makes it stronger; the weakest are pruned past a limit.

HABITS: a task you keep asking for ("brief me on my calendar") on several different days is noticed,
  and Jarvis offers ONCE to turn it into a routine.

Safety, because a learning system is also an attack surface:
  - Lessons come only from what you typed or said in the HUD (main.py calls these functions for HUD messages
    only). Web pages, files, tool output, background jobs and the inbound API can never teach it anything.
  - Anything that looks like a key, a password or a card number is refused.
  - Every lesson is one short sentence, shown in Settings, and deletable ("forget that").
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

from . import db, memory, routines

logger = logging.getLogger(__name__)

MAX_LESSONS = 150
MAX_TEXT = 240
MIN_TEXT = 8
MAX_MESSAGE = 300  # a longer message is more likely pasted text than an instruction
SHOWN_TOPICAL = 5
SHOWN_STANDING = 3
HABIT_MIN_DAYS = 3
HABIT_WINDOW_DAYS = 21

_SECRET = re.compile(
    r"(sk-[A-Za-z0-9_\-]{12,}|AIza[0-9A-Za-z_\-]{16,}|gsk_[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9\-]{8,}|"
    r"\b(?:password|passcode|passphrase|pin|secret|token|api[ _-]?key)\b\s*(?:is|are|:|=)|\b\d{13,19}\b|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY)",
    re.IGNORECASE,
)
_STANDING = re.compile(
    r"\b(always|never|from now on|going forward|in (?:the )?future|every time|whenever|next time)\b", re.I
)
_STOP = frozenset(
    [
        "the",
        "and",
        "for",
        "that",
        "this",
        "with",
        "you",
        "your",
        "from",
        "have",
        "has",
        "had",
        "are",
        "was",
        "were",
        "will",
        "would",
        "should",
        "could",
        "can",
        "not",
        "but",
        "all",
        "any",
        "out",
        "into",
        "onto",
        "than",
        "then",
        "them",
        "they",
        "their",
        "there",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "how",
        "its",
        "our",
        "his",
        "her",
        "she",
        "him",
        "one",
        "two",
        "use",
        "used",
        "using",
        "make",
        "made",
        "get",
        "got",
        "let",
        "just",
        "only",
        "also",
        "very",
        "more",
        "most",
        "some",
        "such",
        "own",
        "same",
        "too",
    ]
)

_CAPTURE = re.compile(
    r"^\W*(?:(?:ok(?:ay)?|jarvis|please|and|so|hey)\W+)*"
    r"(?P<core>(?:from now on|going forward|in (?:the )?future|next time|always|never|whenever i|every time i)\b.*)$",
    re.I | re.S,
)
_NEVER_MIND = re.compile(r"^\W*never\s*mind\b", re.I)
_CORRECTION = re.compile(
    r"^\W*(?:no\b|nope\b|not that\b|wrong\b|that'?s (?:not|wrong)\b|i said\b|i meant\b|i didn'?t (?:ask|say|mean|want)\b|"
    r"don'?t do that\b|stop (?:doing|using|opening|putting)\b|use .{2,40} instead\b|instead\b)",
    re.I,
)


@dataclass
class Lesson:
    id: int
    text: str
    kind: str
    source: str
    created: float
    last_used: float
    uses: int
    strength: float
    standing: bool


# ------------------------------------------------------------------ helpers


def _clean(text: str) -> str:
    """One short sentence with no secrets, or ''."""
    t = " ".join((text or "").split()).strip(" \"'`")
    t = re.sub(r"^(?:remember(?: that)?|note(?: that)?)\s+", "", t, flags=re.I)
    if len(t) < MIN_TEXT or _SECRET.search(t):
        return ""
    if len(t) > MAX_TEXT:
        t = t[: MAX_TEXT - 1].rstrip() + "…"
    return t


def _tokens(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if len(w) < 3 or w in _STOP:
            continue
        out.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _row(r: tuple) -> Lesson:
    return Lesson(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], bool(r[8]))


_COLS = "id, text, kind, source, created, last_used, uses, strength, standing"


# ------------------------------------------------------------------ storing


def add(text: str, kind: str = "preference", source: str = "said") -> int | None:
    """Keep a lesson. Returns its id, or None if it was empty or looked like a secret."""
    text = _clean(text)
    if not text:
        return None
    toks = _tokens(text)
    now = time.time()
    conn = memory.get_conn()
    try:
        for lid, existing in conn.execute("SELECT id, text FROM lessons").fetchall():
            if _jaccard(toks, _tokens(existing)) >= 0.75:  # already known: it just got more certain
                conn.execute(
                    "UPDATE lessons SET strength = MIN(strength + 0.5, 5.0), last_used = ? WHERE id = ?", (now, lid)
                )
                conn.commit()
                return int(lid)
        cur = conn.execute(
            "INSERT INTO lessons (text, kind, source, created, last_used, uses, strength, standing) "
            "VALUES (?, ?, ?, ?, ?, 0, 1.0, ?)",
            (text, kind, source, now, now, 1 if _STANDING.search(text) else 0),
        )
        conn.execute(
            "DELETE FROM lessons WHERE id IN (SELECT id FROM lessons ORDER BY strength ASC, last_used ASC "
            "LIMIT max(0, (SELECT count(*) FROM lessons) - ?))",
            (MAX_LESSONS,),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def all_lessons() -> list[Lesson]:
    conn = memory.get_conn()
    try:
        return [_row(r) for r in conn.execute(f"SELECT {_COLS} FROM lessons ORDER BY created DESC").fetchall()]
    finally:
        conn.close()


def public_list(limit: int = 60) -> list[dict]:
    """What the Settings sheet shows."""
    return [
        {"id": le.id, "text": le.text, "kind": le.kind, "uses": le.uses, "standing": le.standing}
        for le in all_lessons()[:limit]
    ]


def delete(lesson_id: int) -> bool:
    conn = memory.get_conn()
    try:
        cur = conn.execute("DELETE FROM lessons WHERE id = ?", (int(lesson_id),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def forget(query: str) -> list[str]:
    """Delete lessons by id ('12') or by words they contain. Returns what was removed."""
    q = (query or "").strip()
    if not q:
        return []
    removed: list[str] = []
    for le in all_lessons():
        matches = le.id == int(q) if q.isdigit() else q.lower() in le.text.lower()
        if matches and delete(le.id):
            removed.append(le.text)
    return removed


def _mark_used(ids: list[int]) -> None:
    if not ids:
        return
    conn = memory.get_conn()
    try:
        conn.executemany(
            "UPDATE lessons SET uses = uses + 1, last_used = ? WHERE id = ?", [(time.time(), i) for i in ids]
        )
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------ using lessons


def relevant(request: str) -> list[Lesson]:
    """The few lessons worth showing for this request: ones about the same things, plus the standing 'always/never' ones."""
    lessons = all_lessons()
    if not lessons:
        return []
    req = _tokens(request)
    now = time.time()
    topical, standing = [], []
    for le in lessons:
        overlap = len(req & _tokens(le.text))
        recency = 1.0 / (1.0 + max(0.0, now - le.last_used) / (30 * 86400))
        score = overlap * 3 + le.strength * 0.5 + recency
        if overlap:
            topical.append((score, le))
        elif le.standing:
            standing.append((le.strength + recency, le))
    top = [le for _, le in sorted(topical, key=lambda x: -x[0])[:SHOWN_TOPICAL]]
    gen = [le for _, le in sorted(standing, key=lambda x: -x[0])[:SHOWN_STANDING]]
    seen, out = set(), []
    for le in gen + top:
        if le.id not in seen:
            seen.add(le.id)
            out.append(le)
    return out


def prompt_block(request: str = "") -> str:
    """Added to the system prompt: what the user has taught Jarvis that matters for this request."""
    parts = []
    shown = relevant(request)
    if shown:
        _mark_used([le.id for le in shown])
        parts.append(
            "WHAT THE USER HAS TAUGHT YOU (their own instructions and corrections; follow them unless they conflict "
            "with the current request or with safety):\n" + "\n".join(f"- {le.text}" for le in shown)
        )
    habit = habit_block(request)
    if habit:
        parts.append(habit)
    return "\n".join(parts)


# ------------------------------------------------------------------ learning from what the user says


def capture_standing(text: str) -> int | None:
    """'From now on put screenshots in D:\\Shots' -> a lesson. Questions, 'never mind' and long pastes are ignored."""
    t = (text or "").strip()
    if not t or len(t) > MAX_MESSAGE or t.endswith("?") or _NEVER_MIND.match(t):
        return None
    m = _CAPTURE.match(t)
    if not m:
        return None
    return add(m.group("core"), kind="preference", source="said")


def looks_like_correction(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and len(t) <= 200 and not t.endswith("?") and bool(_CORRECTION.match(t)) and not _CAPTURE.match(t)


_DISTILL_SYSTEM = (
    "You turn a user's correction of an assistant into one lasting rule about how to work for that user. "
    "Reply with ONE short imperative sentence (at most 25 words) that stands on its own and applies to future "
    "requests, or reply exactly NONE if the correction only concerned that one request."
)


def distill_correction(brain, previous_request: str, previous_reply: str, correction: str) -> str | None:
    """Ask the model for the lasting rule inside a correction. Blocking: call it off the event loop."""
    ask = (
        f"Earlier request: {previous_request[:300]}\nWhat the assistant did or said: {previous_reply[:400]}\n"
        f"The user's correction: {correction[:300]}\n\nThe lasting rule (or NONE):"
    )
    try:
        turn = brain.complete(_DISTILL_SYSTEM, [{"role": "user", "text": ask}], [])
    except Exception:
        logger.debug("couldn't distill a correction", exc_info=True)
        return None
    rule = (turn.text or "").strip().splitlines()[0] if (turn.text or "").strip() else ""
    rule = _clean(rule.lstrip("-* ").strip())
    if not rule or rule.upper().startswith("NONE") or len(rule.split()) > 40:
        return None
    return rule


# ------------------------------------------------------------------ noticing habits

_NOT_A_HABIT = re.compile(r"^(?:undo|stop|cancel|never ?mind|thank|thanks|yes|no|okay|ok|sorry|hello|hi|hey)\b", re.I)
_SPAM = ("this is the user's routine", "background job mode", "trigger details")
_offered_cache: dict[str, tuple[float, str]] = {}


def _norm_goal(goal: str) -> str:
    g = re.sub(r"[^a-z0-9 ]+", " ", (goal or "").lower())
    g = re.sub(r"\b(?:jarvis|please|hey|ok|okay)\b", " ", g)
    return " ".join(g.split())


def noticed_habit(now: float | None = None) -> tuple[str, int] | None:
    """A request made on at least HABIT_MIN_DAYS different days recently, that isn't a routine yet."""
    now = now or time.time()
    since = now - HABIT_WINDOW_DAYS * 86400
    conn = db.get_conn()
    try:
        rows = conn.execute("SELECT goal, started_at FROM runs WHERE started_at >= ?", (since,)).fetchall()
    finally:
        conn.close()
    days: dict[str, set[str]] = {}
    original: dict[str, str] = {}
    for goal, started in rows:
        if any(s in goal.lower() for s in _SPAM):
            continue
        key = _norm_goal(goal)
        if not 8 <= len(key) <= 80 or _NOT_A_HABIT.match(key):
            continue
        days.setdefault(key, set()).add(time.strftime("%Y-%m-%d", time.localtime(started)))
        original.setdefault(key, " ".join(goal.split())[:80])
    declined = set(json.loads(memory.kv_get("habits_offered", "[]") or "[]"))
    existing = [_tokens(r["action"]) for r in routines.all_routines()]
    for key, d in sorted(days.items(), key=lambda kv: -len(kv[1])):
        if len(d) < HABIT_MIN_DAYS or key in declined:
            continue
        toks = _tokens(key)
        if any(_jaccard(toks, e) >= 0.5 or (toks and len(toks & e) / len(toks) >= 0.8) for e in existing):
            continue  # already a routine for it
        return original[key], len(d)
    return None


def habit_block(request: str = "") -> str:
    """One gentle nudge, once per habit: the model is told to offer a routine, and we never mention it again."""
    cached = _offered_cache.get(request)
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    try:
        found = noticed_habit()
    except Exception:
        logger.debug("habit scan failed", exc_info=True)
        return ""
    if not found:
        return ""
    goal, n = found
    offered = set(json.loads(memory.kv_get("habits_offered", "[]") or "[]"))
    offered.add(_norm_goal(goal))
    memory.kv_set("habits_offered", json.dumps(sorted(offered)[-100:]))
    block = (
        f'HABIT NOTICED: the user has asked for "{goal}" on {n} different days recently. Once, at a natural pause '
        "after finishing what they asked now, offer in ONE sentence to make it a routine (create_routine). If they say "
        "no, drop it. Don't bring it up again."
    )
    _offered_cache[request] = (time.time(), block)
    return block
