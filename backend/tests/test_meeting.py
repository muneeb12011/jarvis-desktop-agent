"""Meeting copilot: chunked transcription, echo removal, summaries, notes, action items."""

import json
import time

import numpy as np
import pytest

from app import meeting, memory, people
from app.meeting import Line, Source


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.db")


def tone(seconds, amp=0.1):
    t = np.arange(int(seconds * meeting.RATE)) / meeting.RATE
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


class FakeSource(Source):
    def __init__(self, name):
        super().__init__()
        self.name = name
        self.closed = False

    def close(self):
        self.closed = True


def make(script):
    """script: source name -> list of (start, end, text) the fake Whisper 'hears' per chunk."""
    events = []
    calls = {"n": 0}
    srcs = {"You": FakeSource("You"), "Them": FakeSource("Them")}

    def transcriber(audio):
        calls["n"] += 1
        return script.pop(0) if script else []

    c = meeting.Copilot(events.append, transcriber=transcriber, opener=lambda: (list(srcs.values()), []))
    return c, srcs, events, calls


def test_waits_for_a_full_chunk_then_transcribes():
    c, srcs, events, calls = make([[(0.5, 3.0, "Let's ship on Friday.")]])
    c.start("Sprint sync")
    c._stop.set()  # don't let the background thread interfere; drive steps by hand
    c._thread.join()
    srcs["Them"].push(tone(5))
    assert c.step(final=False) == [] and calls["n"] == 0  # not enough audio yet
    srcs["Them"].push(tone(12))
    got = c.step(final=False)
    assert [(ln.speaker, ln.text) for ln in got] == [("Them", "Let's ship on Friday.")]
    assert events[0]["state"] == "recording" and events[0]["title"] == "Sprint sync"


def test_silence_is_skipped_without_calling_whisper():
    c, srcs, _events, calls = make([])
    c.start()
    c._stop.set()
    c._thread.join()
    srcs["You"].push(np.zeros(20 * meeting.RATE, dtype=np.float32))
    assert c.step(final=True) == [] and calls["n"] == 0


def test_timestamps_continue_across_chunks():
    c, srcs, _e, _calls = make([[(1.0, 2.0, "first")], [(1.0, 2.0, "second")]])
    c.start()
    c._stop.set()
    c._thread.join()
    srcs["You"].push(tone(15))
    c.step(final=False)
    srcs["You"].push(tone(15))
    c.step(final=True)
    t = [ln.t for ln in c.meeting.lines]
    assert t[0] == pytest.approx(1.0) and t[1] > 10


def test_echo_of_the_call_on_my_mic_is_dropped():
    them = [Line(10.0, "Them", "Can you send the invoice by Monday?")]
    assert meeting.is_echo(Line(11.0, "You", "can you send the invoice by monday"), them)
    assert not meeting.is_echo(Line(12.0, "You", "Sure, I'll send it tonight."), them)
    assert not meeting.is_echo(Line(90.0, "You", "can you send the invoice by monday"), them)


def test_cut_point_lands_in_a_pause():
    audio = np.concatenate([tone(11), np.zeros(meeting.RATE // 2, dtype=np.float32), tone(2.5)])
    cut = meeting._cut_point(audio, min_keep=10 * meeting.RATE)
    assert 11 * meeting.RATE <= cut <= 11.5 * meeting.RATE


def test_parse_summary_is_forgiving():
    raw = (
        'Sure!\n```json\n{"title": "Pricing call", "summary": "We agreed.", "decisions": ["Price $20"], '
        '"action_items": [{"owner": "me", "task": "send deck", "for": "Ali", "due": "2026-10-23 17:00"}, '
        '{"owner": "Sara", "task": ""}]}\n```'
    )
    s = meeting.parse_summary(raw, "x")
    assert s["title"] == "Pricing call" and len(s["action_items"]) == 1
    assert meeting.parse_summary("not json at all", "Fallback")["title"] == "Fallback"


def test_save_search_and_track(tmp_path):
    m = meeting.Meeting("Client call", started=time.time() - 600)
    m.lines = [Line(5, "Them", "We need the landing page by Thursday."), Line(9, "You", "I'll have it ready.")]
    s = meeting.parse_summary(
        json.dumps(
            {
                "title": "Landing page call",
                "summary": "Client wants the landing page.",
                "decisions": ["Use the blue theme"],
                "action_items": [
                    {"owner": "me", "task": "deliver the landing page", "for": "Bilal", "due": "2030-01-02 17:00"},
                    {"owner": "Bilal", "task": "send the logo files", "due": None},
                    {"owner": "me", "task": "update the invoice", "due": None},
                ],
            }
        ),
        m.title,
    )
    mid, path = meeting.save(m, s, folder=tmp_path)
    md = path.read_text(encoding="utf-8")
    assert "# Landing page call" in md and "- [ ] **You**: deliver the landing page (for Bilal)" in md
    assert "Them: We need the landing page by Thursday." in md
    assert meeting.search("landing thursday")[0]["id"] == mid
    out = meeting.track_actions(mid)
    assert "Tracked 3" in out
    bilal = people.find("Bilal")
    owed = {c["direction"]: c["text"] for c in people.commitments(bilal["id"])}
    assert owed == {"i_owe": "deliver the landing page", "they_owe": "send the logo files"}
    texts = [r["text"] for r in memory.pending_reminders()]
    assert any("deliver the landing page" in t for t in texts) and "update the invoice" in texts


def test_start_twice_and_stop_closes_devices():
    c, srcs, events, _ = make([])
    assert "Recording" in c.start("Standup")
    assert "Already recording" in c.start("Again")
    m = c.stop()
    assert m.title == "Standup" and srcs["You"].closed and srcs["Them"].closed
    assert events[-1]["state"] == "processing" and not c.recording
    assert c.stop() is None


def test_no_devices_gives_a_clear_reason():
    c = meeting.Copilot(lambda _e: None, opener=lambda: ([], ["Couldn't open the microphone (busy)."]))
    assert c.start().startswith("Couldn't start: Couldn't open the microphone")
