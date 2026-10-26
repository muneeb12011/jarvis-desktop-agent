"""
Meeting copilot.

"Jarvis, take notes on this call" -> records your microphone and what your
PC is playing (the other people on Meet/Zoom/Teams/Discord), transcribes it
locally with Whisper as it goes, labelled "You" and "Them". When you say
"stop the meeting", Jarvis writes a summary with decisions and action items,
saves the notes to Documents\\Jarvis Meetings, and offers to track your action
items as reminders and promises.

Privacy and consent:
- Audio is never saved; only the text transcript is kept.
- Transcription runs on this PC. Only the finished transcript goes to the
  model, once, for the summary.
- A red REC indicator stays on screen the whole time, and Jarvis reminds
  you to tell the others they're being transcribed (the law requires it in
  many places).
- Stops on its own after 3 hours, or after 10 minutes with no speech.
"""

from __future__ import annotations

import ctypes
import datetime as dt
import difflib
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import config, memory
from .platform_utils import IS_WINDOWS

logger = logging.getLogger(__name__)

RATE = 16000
CHUNK_SECONDS = 14  # transcribe after this much audio has built up
MAX_HOURS = 3
SILENCE_STOP_MINUTES = 10
LANGUAGE = (config._get("JARVIS_MEETING_LANGUAGE", "en") or "en").lower()  # "auto" for multilingual models

_SCHEMA = """CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    started REAL NOT NULL,
    ended REAL,
    path TEXT,
    summary TEXT NOT NULL DEFAULT '',
    actions TEXT NOT NULL DEFAULT '[]',
    transcript TEXT NOT NULL DEFAULT ''
)"""


def _conn():
    conn = memory.get_conn()
    conn.execute(_SCHEMA)
    return conn


# ------------------------------------------------------------------ audio sources


class Source:
    """A stream of 16 kHz mono float32 audio. Subclasses fill `_frames` from a callback."""

    name = "?"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._chunks: list = []

    def push(self, samples) -> None:
        with self._lock:
            self._chunks.append(samples)

    def drain(self):
        import numpy as np

        with self._lock:
            chunks, self._chunks = self._chunks, []
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    def close(self) -> None:
        pass


def to_mono_16k(raw: bytes, channels: int, rate: int):
    import numpy as np

    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        pcm = pcm[: len(pcm) - len(pcm) % channels].reshape(-1, channels).mean(axis=1)
    if rate != RATE and len(pcm):
        n = int(len(pcm) * RATE / rate)
        pcm = np.interp(np.linspace(0, len(pcm) - 1, n), np.arange(len(pcm)), pcm).astype(np.float32)
    return pcm


class PaSource(Source):
    """A WASAPI device through PyAudioWPatch: the microphone, or the speakers' loopback ('what you hear')."""

    def __init__(self, pa, info: dict, name: str) -> None:
        super().__init__()
        import pyaudiowpatch as pyaudio

        self.name = name
        self.device = info["name"]
        self.rate = int(info["defaultSampleRate"])
        self.channels = max(1, min(2, int(info["maxInputChannels"])))

        def callback(data, _frames, _time, _status):
            self.push(to_mono_16k(data, self.channels, self.rate))
            return (None, pyaudio.paContinue)

        self.stream = pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=info["index"],
            frames_per_buffer=int(self.rate * 0.1),
            stream_callback=callback,
        )
        self.stream.start_stream()

    def close(self) -> None:
        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception:
            pass


def open_sources() -> tuple[list[Source], list[str]]:
    """Mic ('You') and system audio loopback ('Them'). Returns the sources and any problems."""
    problems: list[str] = []
    try:
        import pyaudiowpatch as pyaudio
    except Exception:
        return [], ["Meeting capture needs PyAudioWPatch (run setup-jarvis.bat again)."]
    pa = pyaudio.PyAudio()
    sources: list[Source] = []
    try:
        mic = pa.get_default_input_device_info()
        sources.append(PaSource(pa, mic, "You"))
    except Exception as e:
        problems.append(f"Couldn't open the microphone ({e}).")
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        speakers = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        loop = speakers if speakers.get("isLoopbackDevice") else None
        if loop is None:
            for lb in pa.get_loopback_device_info_generator():
                if speakers["name"] in lb["name"]:
                    loop = lb
                    break
        if loop is None:
            raise RuntimeError("no loopback device for the default speakers")
        sources.append(PaSource(pa, loop, "Them"))
    except Exception as e:
        problems.append(f"Couldn't capture the PC's audio ({e}); only your mic will be transcribed.")
    return sources, problems


# ------------------------------------------------------------------ transcription


def _cut_point(audio, min_keep: int) -> int:
    """Index to cut at: the quietest 250 ms in the last 4 s, so words aren't split between chunks."""
    import numpy as np

    win = RATE // 4
    start = max(min_keep, len(audio) - 4 * RATE)
    best, best_e = len(audio), None
    for i in range(start, len(audio) - win, win // 2):
        e = float(np.mean(audio[i : i + win] ** 2))
        if best_e is None or e < best_e:
            best, best_e = i + win // 2, e
    return best


def has_speech(audio) -> bool:
    import numpy as np

    return len(audio) > RATE // 2 and float(np.sqrt(np.mean(audio**2))) > 0.004


@dataclass
class Line:
    t: float  # seconds since the meeting started
    speaker: str
    text: str


def is_echo(line: Line, others: list[Line], window: float = 25.0) -> bool:
    """Your mic hears the call through your speakers: drop 'You' lines that repeat what 'Them' said."""
    a = re.sub(r"[^\w ]", "", line.text.lower())
    if len(a) < 6:
        return False
    for o in others:
        if abs(o.t - line.t) <= window:
            b = re.sub(r"[^\w ]", "", o.text.lower())
            if difflib.SequenceMatcher(None, a, b).ratio() > 0.72 or (len(a) > 15 and a in b):
                return True
    return False


_HALLUCINATIONS = re.compile(
    r"^(thank you\.?|thanks for watching!?|you|bye\.?|\.+|subtitles by .*|please subscribe.*)$", re.I
)


def default_transcriber(audio) -> list[tuple[float, float, str]]:
    from . import voice

    model = voice._load_whisper()
    if model is None:
        raise RuntimeError("Whisper isn't available, so meetings can't be transcribed.")
    lang = None if LANGUAGE == "auto" else LANGUAGE
    segments, _ = model.transcribe(audio, language=lang, beam_size=1, vad_filter=True, condition_on_previous_text=False)
    return [(s.start, s.end, s.text.strip()) for s in segments]


# ------------------------------------------------------------------ the meeting


@dataclass
class Meeting:
    title: str
    started: float = field(default_factory=time.time)
    lines: list[Line] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    last_speech: float = field(default_factory=time.time)

    def elapsed(self) -> float:
        return time.time() - self.started

    def transcript(self) -> str:
        return "\n".join(f"[{_mmss(ln.t)}] {ln.speaker}: {ln.text}" for ln in self.lines)


def _mmss(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class Copilot:
    """One meeting at a time. `emit` is called from the worker thread with HUD events."""

    def __init__(
        self,
        emit: Callable[[dict], None],
        transcriber: Callable = default_transcriber,
        opener: Callable[[], tuple[list[Source], list[str]]] = open_sources,
    ) -> None:
        self.emit = emit
        self.transcriber = transcriber
        self.opener = opener
        self.meeting: Meeting | None = None
        self.sources: list[Source] = []
        self._buffers: dict[str, object] = {}
        self._offsets: dict[str, float] = {}  # seconds of audio already consumed per source
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._whisper_lock = threading.Lock()
        self.on_finished: Callable[[Meeting], None] | None = None

    @property
    def recording(self) -> bool:
        return self.meeting is not None

    def start(self, title: str = "") -> str:
        if self.meeting:
            return f"Already recording '{self.meeting.title}' ({_mmss(self.meeting.elapsed())})."
        sources, problems = self.opener()
        if not sources:
            return "Couldn't start: " + " ".join(problems)
        import numpy as np

        title = title.strip() or f"Meeting {dt.datetime.now().strftime('%a %d %b %I:%M %p')}"
        self.meeting = Meeting(title, problems=problems)
        self.sources = sources
        self._buffers = {s.name: np.zeros(0, dtype=np.float32) for s in sources}
        self._offsets = {s.name: 0.0 for s in sources}
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="jarvis-meeting", daemon=True)
        self._thread.start()
        self._state("recording")
        heard = " and ".join("your mic" if s.name == "You" else "the call audio" for s in sources)
        return f"Recording '{title}' ({heard}). Let everyone know they're being transcribed." + (
            " " + " ".join(problems) if problems else ""
        )

    def stop(self) -> Meeting | None:
        if not self.meeting:
            return None
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=120)
        m = self.meeting
        self._close_sources()
        self.meeting = None
        self._state("processing", m)
        return m

    def _close_sources(self) -> None:
        for s in self.sources:
            s.close()
        self.sources = []

    def _state(self, state: str, m: Meeting | None = None) -> None:
        m = m or self.meeting
        self.emit(
            {
                "type": "meeting",
                "state": state,
                "title": m.title if m else "",
                "started": m.started if m else None,
                "last": (f"{m.lines[-1].speaker}: {m.lines[-1].text}" if m and m.lines else ""),
            }
        )

    def _run(self) -> None:
        last_ui = 0.0
        while not self._stop.wait(2.0):
            try:
                self.step(final=False)
            except Exception:
                logger.exception("Meeting transcription step failed")
            m = self.meeting
            if not m:
                break
            if m.elapsed() > MAX_HOURS * 3600 or time.time() - m.last_speech > SILENCE_STOP_MINUTES * 60:
                logger.info("Meeting auto-stop (%s)", "max length" if m.elapsed() > MAX_HOURS * 3600 else "silence")
                try:
                    self.step(final=True)
                except Exception:
                    logger.exception("Final meeting transcription failed")
                threading.Thread(target=self._auto_finish, daemon=True).start()
                return
            if time.time() - last_ui > 5:
                last_ui = time.time()
                self._state("recording")
        try:
            self.step(final=True)
        except Exception:
            logger.exception("Final meeting transcription failed")

    def _auto_finish(self) -> None:
        m = self.stop()
        if m and self.on_finished:
            self.on_finished(m)

    def step(self, final: bool) -> list[Line]:
        """Pull new audio from every source; transcribe whatever has built up past CHUNK_SECONDS."""
        import numpy as np

        m = self.meeting
        if not m:
            return []
        added: list[Line] = []
        for s in self.sources:
            buf = np.concatenate([self._buffers[s.name], s.drain()])
            if len(buf) < CHUNK_SECONDS * RATE and not final:
                self._buffers[s.name] = buf
                continue
            cut = len(buf) if final else _cut_point(buf, min_keep=(CHUNK_SECONDS - 4) * RATE)
            chunk, rest = buf[:cut], buf[cut:]
            t0 = self._offsets[s.name]
            self._offsets[s.name] += len(chunk) / RATE
            self._buffers[s.name] = rest
            if not has_speech(chunk):
                continue
            with self._whisper_lock:
                segs = self.transcriber(chunk)
            for start, _end, text in segs:
                if not text or _HALLUCINATIONS.match(text):
                    continue
                added.append(Line(t0 + start, s.name, text))
        if added:
            them = [ln for ln in m.lines + added if ln.speaker == "Them"]
            kept = [ln for ln in added if not (ln.speaker == "You" and is_echo(ln, them))]
            m.lines = sorted(m.lines + kept, key=lambda ln: ln.t)
            if kept:
                m.last_speech = time.time()
                self._state("recording")
            return kept
        return []


# ------------------------------------------------------------------ after the meeting

SUMMARY_PROMPT = """You turn a meeting transcript into notes. "You" is the user (the person running this \
assistant); "Them" is everyone else on the call (use names if they're said aloud). Today is {today}.

Reply with JSON only:
{{"title": "short specific title (max 8 words)",
  "summary": "3-6 sentences: what it was about and where things landed",
  "decisions": ["each decision made, one line each"],
  "action_items": [{{"owner": "me" or the person's name, "task": "what", "for": "who it's for, if said",
                     "due": "YYYY-MM-DD HH:MM if a deadline was said, else null"}}],
  "open_questions": ["unresolved things"]}}
Only include action items someone actually agreed to. If the transcript is too short or empty, still return \
valid JSON with empty lists."""


def summarize(brain, m: Meeting) -> dict:
    text = m.transcript()
    if not text.strip():
        return parse_summary('{"summary": "Nothing was transcribed."}', m.title)
    msg = {"role": "user", "text": f"MEETING: {m.title}\n\nTRANSCRIPT:\n{text[-180000:]}"}
    try:
        turn = brain.complete(SUMMARY_PROMPT.format(today=dt.datetime.now().strftime("%A %Y-%m-%d %H:%M")), [msg], [])
    except Exception as e:
        logger.warning("Meeting summary failed: %s", e)
        return parse_summary(
            json.dumps({"summary": f"The summary couldn't be written ({e}); the full transcript is below."}), m.title
        )
    return parse_summary(turn.text, m.title)


def parse_summary(raw: str, fallback_title: str) -> dict:
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    data: dict = {}
    if match:
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            data = {}
    actions = []
    for a in data.get("action_items") or []:
        if isinstance(a, dict) and str(a.get("task", "")).strip():
            actions.append(
                {
                    "owner": str(a.get("owner") or "me").strip(),
                    "task": str(a["task"]).strip(),
                    "for": str(a.get("for") or "").strip(),
                    "due": a.get("due") or None,
                }
            )
    return {
        "title": str(data.get("title") or fallback_title)[:80],
        "summary": str(data.get("summary") or (raw or "").strip()[:1500] or "No summary."),
        "decisions": [str(x) for x in data.get("decisions") or []],
        "action_items": actions,
        "open_questions": [str(x) for x in data.get("open_questions") or []],
    }


def notes_dir() -> Path:
    """The real Documents folder (it's often redirected to OneDrive)."""
    base = Path.home() / "Documents"
    if IS_WINDOWS:
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:  # CSIDL_PERSONAL
            base = Path(buf.value)
    return base / "Jarvis Meetings"


def _is_me(owner: str) -> bool:
    return owner.strip().lower() in ("me", "you", "i", "user", config.USER_NAME.lower() or "me")


def render_markdown(m: Meeting, s: dict) -> str:
    when = dt.datetime.fromtimestamp(m.started)
    out = [f"# {s['title']}", "", f"{when.strftime('%A %d %B %Y, %I:%M %p')} · {_mmss(m.elapsed())}", ""]
    out += ["## Summary", "", s["summary"], ""]
    if s["decisions"]:
        out += ["## Decisions", ""] + [f"- {d}" for d in s["decisions"]] + [""]
    if s["action_items"]:
        out += ["## Action items", ""]
        for a in s["action_items"]:
            who = "You" if _is_me(a["owner"]) else a["owner"]
            extra = (f" (for {a['for']})" if a["for"] else "") + (f", due {a['due']}" if a["due"] else "")
            out.append(f"- [ ] **{who}**: {a['task']}{extra}")
        out.append("")
    if s.get("open_questions"):
        out += ["## Open questions", ""] + [f"- {q}" for q in s["open_questions"]] + [""]
    out += ["## Transcript", "", m.transcript() or "(empty)", ""]
    return "\n".join(out)


def save(m: Meeting, s: dict, folder: Path | None = None) -> tuple[int, Path]:
    folder = folder or notes_dir()
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*]+', "", s["title"]).strip()[:60] or "Meeting"
    path = folder / f"{dt.datetime.fromtimestamp(m.started).strftime('%Y-%m-%d %H%M')} {safe}.md"
    path.write_text(render_markdown(m, s), encoding="utf-8")
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO meetings (title, started, ended, path, summary, actions, transcript) VALUES (?,?,?,?,?,?,?)",
            (
                s["title"],
                m.started,
                time.time(),
                str(path),
                json.dumps({k: s[k] for k in ("summary", "decisions", "open_questions") if k in s}),
                json.dumps(s["action_items"]),
                m.transcript(),
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0), path
    finally:
        conn.close()


def get(meeting_id: int | None = None) -> dict | None:
    conn = _conn()
    try:
        q = "SELECT id, title, started, ended, path, summary, actions, transcript FROM meetings"
        row = (
            conn.execute(q + " WHERE id = ?", (meeting_id,)).fetchone()
            if meeting_id
            else conn.execute(q + " ORDER BY id DESC LIMIT 1").fetchone()
        )
    finally:
        conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "title": row[1],
        "started": row[2],
        "ended": row[3],
        "path": row[4],
        **json.loads(row[5] or "{}"),
        "action_items": json.loads(row[6] or "[]"),
        "transcript": row[7],
    }


def recent(limit: int = 10) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, title, started, ended FROM meetings ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "title": r[1], "started": r[2], "ended": r[3]} for r in rows]


def search(query: str, limit: int = 5) -> list[dict]:
    words = [w for w in query.lower().split() if len(w) > 2]
    if not words:
        return []
    conn = _conn()
    try:
        clause = " AND ".join(
            "(lower(transcript) LIKE ? OR lower(summary) LIKE ? OR lower(title) LIKE ?)" for _ in words
        )
        args: list = []
        for w in words:
            args += [f"%{w}%"] * 3
        rows = conn.execute(
            f"SELECT id FROM meetings WHERE {clause} ORDER BY id DESC LIMIT ?",  # noqa: S608 - static clause
            (*args, limit),
        ).fetchall()
    finally:
        conn.close()
    return [x for x in (get(r[0]) for r in rows) if x]


def track_actions(meeting_id: int | None = None) -> str:
    """Your action items -> reminders (or promises to a person); others' -> 'owed to you'."""
    from . import people

    rec = get(meeting_id)
    if not rec:
        return "No saved meeting found."
    done: list[str] = []
    for a in rec["action_items"]:
        due = None
        if a.get("due"):
            try:
                due = dt.datetime.fromisoformat(str(a["due"]).replace(" ", "T")).timestamp()
            except ValueError:
                due = None
        if _is_me(a["owner"]):
            if a.get("for"):
                e = people.find(a["for"]) or people.upsert(a["for"])
                rid = memory.add_reminder(f"You promised {e['name']}: {a['task']}", due) if due else None
                people.add_commitment(e["id"], a["task"], "i_owe", due, rid)
                done.append(f"promise to {e['name']}: {a['task']}")
            elif due:
                memory.add_reminder(a["task"], due)
                done.append(f"reminder: {a['task']}")
            else:
                memory.add_reminder(a["task"], time.time() + 24 * 3600)
                done.append(f"reminder tomorrow: {a['task']}")
        else:
            e = people.find(a["owner"]) or people.upsert(a["owner"])
            people.add_commitment(e["id"], a["task"], "they_owe", due)
            done.append(f"{e['name']} owes you: {a['task']}")
    if not done:
        return "That meeting had no action items."
    return f"Tracked {len(done)} action item(s) from '{rec['title']}':\n" + "\n".join(f"- {d}" for d in done)


COPILOT: Copilot | None = None
