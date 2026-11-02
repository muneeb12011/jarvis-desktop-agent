"""Lessons (what the user taught Jarvis) and habits (what they keep asking for)."""

import asyncio
import json
import pathlib
import re
import time
from types import SimpleNamespace

import pytest

from app import db, learning, main, memory, prompts, routines, tools
from app.providers import AssistantTurn
from app.tools import learning_tools


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "a.db")
    learning._offered_cache.clear()
    tools.load_all()


def texts():
    return [le.text for le in learning.all_lessons()]


# ---------------------------------------------------------------- storing lessons


def test_add_cleans_and_keeps_a_lesson():
    lid = learning.add("  remember that   I like my reports   in Documents/Jarvis  ")
    assert lid and texts() == ["I like my reports in Documents/Jarvis"]
    assert learning.all_lessons()[0].kind == "preference" and not learning.all_lessons()[0].standing


@pytest.mark.parametrize(
    "text",
    [
        "",
        "ok",
        "my api key is sk-abcdefghijklmnop1234",
        "the password is hunter2",
        "my token: abc",
        "use card 4111111111111111 for this",
        "key AIzaSyA1234567890abcdefghij",
        "ghp_abcdefghijklmnopqrstuvwxyz1234",
        "-----BEGIN RSA PRIVATE KEY-----",
    ],
)
def test_secrets_and_junk_are_never_kept(text):
    assert learning.add(text) is None and texts() == []


def test_long_lessons_are_shortened():
    learning.add("always " + "word " * 100)
    assert len(texts()[0]) <= learning.MAX_TEXT


def test_repeating_a_lesson_strengthens_it_instead_of_duplicating():
    a = learning.add("always save screenshots to D:\\Shots")
    b = learning.add("Always save screenshots in D:\\Shots")  # same idea, slightly different words
    assert a == b and len(texts()) == 1
    assert learning.all_lessons()[0].strength == 1.5
    for _ in range(20):
        learning.add("always save screenshots to D:\\Shots")
    assert learning.all_lessons()[0].strength == 5.0  # capped
    assert learning.add("use Edge instead of Chrome for browsing") != a and len(texts()) == 2


def test_the_weakest_lessons_are_pruned_past_the_limit(monkeypatch):
    monkeypatch.setattr(learning, "MAX_LESSONS", 4)
    keep = learning.add("always put invoices in the Finance folder")
    learning.add("always put invoices in the Finance folder")  # reinforced
    for topic in (
        "alpha beta gamma delta",
        "epsilon zeta theta iota",
        "kappa lambda sigma omega",
        "tango foxtrot whiskey yankee",
    ):
        learning.add(f"remember to handle {topic}")
    assert len(texts()) == 4 and keep in {le.id for le in learning.all_lessons()}


def test_delete_and_forget():
    a = learning.add("always put invoices in the Finance folder")
    learning.add("never open Spotify at startup")
    assert learning.delete(a) and not learning.delete(a)
    assert learning.forget("spotify") == ["never open Spotify at startup"] and texts() == []
    learning.add("never open Spotify at startup")
    lid = learning.all_lessons()[0].id
    assert learning.forget(str(lid)) and texts() == [] and learning.forget("") == []


# ---------------------------------------------------------------- picking them up from what the user says


@pytest.mark.parametrize(
    "text",
    [
        "From now on put screenshots in D:\\Shots",
        "Always use Edge instead of Chrome",
        "ok jarvis, never open Spotify on startup",
        "Next time ask me before closing apps",
        "whenever I say lights, turn on the desk lamp",
        "Going forward, name my reports with the date first",
        "In the future keep answers shorter",
    ],
)
def test_standing_instructions_are_kept(text):
    assert learning.capture_standing(text)
    assert len(texts()) == 1


@pytest.mark.parametrize(
    "text",
    [
        "Never mind",
        "never mind that",
        "Always?",
        "Do you always do that?",
        "I always forget my keys",
        "remember that my wife's name is Sara",  # a fact: the remember tool's job
        "open spotify",
        "",
        "From now on " + "x" * 400,  # a paste, not an instruction
        "From now on, my api key is sk-abcdefghijklmnop1234",
    ],
)
def test_other_messages_teach_nothing(text):
    assert learning.capture_standing(text) is None and texts() == []


def test_standing_lessons_are_flagged():
    learning.capture_standing("From now on put screenshots in D:\\Shots")
    assert learning.all_lessons()[0].standing


@pytest.mark.parametrize(
    "text",
    [
        "No, I meant the Documents folder",
        "not that one",
        "I said Edge",
        "Use Edge instead",
        "stop opening Chrome",
        "Wrong, try again",
        "that's not what I asked",
        "no, the other one",
    ],
)
def test_corrections_are_recognised(text):
    assert learning.looks_like_correction(text), text


@pytest.mark.parametrize(
    "text",
    [
        "now open spotify",
        "No problem?",
        "nobody called",
        "Not sure what to pick?",
        "open notepad",
        "from now on don't use Chrome",
        "no " + "x" * 300,
        "",
    ],
)
def test_ordinary_messages_are_not_corrections(text):
    assert not learning.looks_like_correction(text), text


class Brain:
    def __init__(self, answer="Save screenshots in D:\\Shots.", boom=False):
        self.answer, self.boom, self.calls = answer, boom, []

    def complete(self, system, messages, specs):
        self.calls.append((system, messages, specs))
        if self.boom:
            raise RuntimeError("offline")
        return AssistantTurn(self.answer, provider="t")


def test_a_correction_becomes_one_lasting_rule():
    brain = Brain("- Save screenshots in D:\\Shots, not on the Desktop.\nBecause the user said so.")
    rule = learning.distill_correction(
        brain, "take a screenshot", "Saved it to the Desktop.", "no, put them in D:\\Shots"
    )
    assert rule == "Save screenshots in D:\\Shots, not on the Desktop."
    system, messages, specs = brain.calls[0]
    assert "lasting rule" in system and specs == []
    body = messages[0]["text"]
    assert "take a screenshot" in body and "Saved it to the Desktop." in body and "no, put them in D:\\Shots" in body


@pytest.mark.parametrize(
    "answer", ["NONE", "none.", "None - only about that request", "", "   ", "word " * 60, "my password is hunter2 ok"]
)
def test_one_off_corrections_and_junk_teach_nothing(answer):
    assert learning.distill_correction(Brain(answer), "a", "b", "no") is None


def test_a_broken_model_teaches_nothing():
    assert learning.distill_correction(Brain(boom=True), "a", "b", "no, not that") is None


# ---------------------------------------------------------------- using lessons


def teach(*lines):
    for line in lines:
        learning.add(line)


def test_relevant_lessons_match_the_request_and_standing_ones_always_apply():
    teach(
        "always save screenshots in D:\\Shots",
        "use Edge instead of Chrome for browsing",
        "my invoices go in the Finance folder",
        "never delete anything without telling me first",
    )
    got = [le.text for le in learning.relevant("take a screenshot of this window")]
    assert "always save screenshots in D:\\Shots" in got
    assert "never delete anything without telling me first" in got  # standing: applies to everything
    assert "use Edge instead of Chrome for browsing" not in got and "my invoices go in the Finance folder" not in got
    assert [le.text for le in learning.relevant("open the browser")] == [
        "never delete anything without telling me first",
        "use Edge instead of Chrome for browsing",
    ] or "use Edge instead of Chrome for browsing" in [le.text for le in learning.relevant("open chrome")]


def test_relevance_is_limited():
    for i in range(12):
        learning.add(f"when I say report number{i} use the quarterly template for report{i} please")
    assert (
        len(learning.relevant("write the quarterly report template"))
        <= learning.SHOWN_TOPICAL + learning.SHOWN_STANDING
    )


def test_standing_lessons_are_capped_too():
    for i in range(10):
        learning.add(f"always handle project{i} with special{i} care and attention please")
    assert len(learning.relevant("tell me a joke")) == learning.SHOWN_STANDING


def test_using_a_lesson_is_recorded():
    teach("always save screenshots in D:\\Shots")
    assert learning.all_lessons()[0].uses == 0
    block = learning.prompt_block("take a screenshot")
    assert "WHAT THE USER HAS TAUGHT YOU" in block and "- always save screenshots in D:\\Shots" in block
    assert learning.all_lessons()[0].uses == 1


def test_no_lessons_no_block():
    assert learning.prompt_block("anything at all") == ""


def test_lessons_reach_the_system_prompt():
    teach("my screenshots go in D:\\Shots")  # not a standing rule: only shown when it matters
    assert "my screenshots go in D:\\Shots" in prompts.system_prompt(False, "take a screenshot")
    assert "WHAT THE USER HAS TAUGHT YOU" not in prompts.system_prompt(False, "what is the weather")
    teach("always keep answers short")  # a standing rule applies to everything
    assert "always keep answers short" in prompts.system_prompt(False, "what is the weather")


# ---------------------------------------------------------------- noticing habits


def log(goal, days_ago):
    conn = db.get_conn()
    conn.execute("INSERT INTO runs (goal, started_at) VALUES (?, ?)", (goal, time.time() - days_ago * 86400 - 60))
    conn.commit()
    conn.close()


def test_a_task_asked_on_three_different_days_is_noticed():
    for d in (1, 2, 3):
        log("brief me on my calendar", d)
    found = learning.noticed_habit()
    assert found == ("brief me on my calendar", 3)


def test_not_noticed_when_it_is_the_same_day_or_too_few_or_too_old():
    for _ in range(5):
        log("brief me on my calendar", 1)  # five times, but all the same day
    for d in (1, 2):
        log("open my project folder please", d)
    for d in (30, 31, 32):
        log("check the server status now", d)  # outside the 21-day window
    assert learning.noticed_habit() is None


def test_noise_and_automatic_runs_are_not_habits():
    for d in (1, 2, 3, 4):
        log("thanks", d)
        log("undo that", d)
        log("ok", d)
        log("morning brief (This is the user's routine 'morning', fired daily.)", d)
        log("BACKGROUND JOB MODE research x", d)
    assert learning.noticed_habit() is None


def test_something_that_is_already_a_routine_is_not_suggested():
    routines.create("calendar brief", "time", {"at": "09:00", "days": "daily"}, "brief me on my calendar")
    for d in (1, 2, 3):
        log("brief me on my calendar", d)
    assert learning.noticed_habit() is None


def test_the_offer_is_made_once():
    for d in (1, 2, 3):
        log("brief me on my calendar", d)
    first = learning.habit_block("what's the weather")
    assert "HABIT NOTICED" in first and "brief me on my calendar" in first and "3 different days" in first
    assert "create_routine" in first and "Don't bring it up again" in first
    assert (
        learning.habit_block("what's the weather") == first
    )  # the same request keeps the same prompt for all its steps
    assert learning.habit_block("a different request") == ""  # but it is never offered for a new one
    assert "brief me on my calendar" not in prompts.system_prompt(False, "open notepad")


def test_the_habit_nudge_reaches_the_prompt():
    for d in (1, 2, 3):
        log("clean up my downloads folder", d)
    assert "HABIT NOTICED" in prompts.system_prompt(False, "hello there")


# ---------------------------------------------------------------- the tools


def test_list_and_forget_tools():
    assert "haven't learned" in learning_tools.list_lessons()
    teach("always save screenshots in D:\\Shots", "never open Spotify at startup")
    listing = learning_tools.list_lessons()
    assert "always save screenshots in D:\\Shots" in listing and "never open Spotify" in listing
    lid = learning.all_lessons()[-1].id
    assert learning_tools.forget_lesson(str(lid)).startswith("Forgot: always save screenshots")
    assert learning_tools.forget_lesson("spotify").startswith("Forgot")
    assert "haven't learned anything matching" in learning_tools.forget_lesson("spotify")


def test_the_tools_are_registered_and_routed():
    from app import toolsets

    for name in ("list_lessons", "forget_lesson"):
        assert name in tools.REGISTRY and name in toolsets.BUILTIN_GROUPS["memory_extra"]
    pattern = next(p for p, g in toolsets._KEYWORDS if g == "memory_extra")
    for phrase in ("what have you learned about me", "forget that rule", "what are my rules"):
        assert re.search(pattern, phrase, re.I), phrase


# ---------------------------------------------------------------- wiring: only the user's own HUD messages teach


@pytest.fixture
def hud(monkeypatch):
    sent, broadcast, submitted = [], [], []

    async def send(ws, payload):
        sent.append(payload)

    async def bcast(payload):
        broadcast.append(payload)

    async def submit(text, voice=False):
        submitted.append(text)

    brain = Brain("Put screenshots in D:\\Shots, not on the Desktop.")
    agent = SimpleNamespace(
        submit=submit,
        last_reply="Saved it on the Desktop.",
        history=[{"role": "user", "text": "take a screenshot"}],
        brain=brain,
    )
    monkeypatch.setattr(main.hub, "send", send)
    monkeypatch.setattr(main.hub, "broadcast", bcast)
    monkeypatch.setattr(main, "agent", agent)
    return SimpleNamespace(sent=sent, broadcast=broadcast, submitted=submitted, brain=brain, agent=agent)


def say(text):
    async def go():
        await main._handle_user_text(object(), text, False)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)

    asyncio.run(go())


def test_a_standing_instruction_is_learned_and_the_request_still_runs(hud):
    say("From now on put screenshots in D:\\Shots")
    assert texts() == ["From now on put screenshots in D:\\Shots"]
    assert hud.submitted == ["From now on put screenshots in D:\\Shots"]
    assert hud.broadcast == [{"type": "lessons", "items": learning.public_list()}]
    assert hud.broadcast[0]["items"][0]["standing"] is True


def test_a_correction_is_distilled_in_the_background(hud):
    say("no, put them in D:\\Shots")
    assert texts() == ["Put screenshots in D:\\Shots, not on the Desktop."]
    body = hud.brain.calls[0][1][0]["text"]
    assert "take a screenshot" in body and "Saved it on the Desktop." in body
    assert hud.submitted == ["no, put them in D:\\Shots"]  # and Jarvis still acts on it
    assert hud.broadcast and hud.broadcast[-1]["type"] == "lessons"


def test_a_correction_with_no_lasting_rule_is_forgotten(hud):
    hud.brain.answer = "NONE"
    say("no, the other one")
    assert texts() == [] and hud.broadcast == []


def test_no_correction_learning_without_something_to_correct(hud):
    hud.agent.last_reply = ""
    say("no, put them in D:\\Shots")
    assert hud.brain.calls == [] and texts() == []


def test_ordinary_messages_leave_no_trace(hud):
    say("open notepad")
    say("what's the weather?")
    assert texts() == [] and hud.brain.calls == [] and hud.broadcast == []


def test_switching_on_work_without_asking_does_not_become_a_lesson(hud):
    from app import autonomy

    say("from now on work without asking until I say stop")
    autonomy.revoke()
    assert texts() == []  # that is a mode switch, handled before any learning


def test_only_the_hud_handler_ever_teaches():
    app_dir = pathlib.Path(main.__file__).parent
    teachers = sorted(
        str(p.relative_to(app_dir))
        for p in app_dir.rglob("*.py")
        if re.search(r"capture_standing\(|distill_correction\(|learning\.add\(", p.read_text(encoding="utf-8"))
    )
    # learning.py defines them; main.py's HUD handler is the only caller. Not jobs, inbox, the inbound API,
    # integrations, tools or anything a web page can influence.
    assert teachers == ["learning.py", "main.py"]


def test_deleting_a_lesson_from_the_hud(hud):
    a = learning.add("always save screenshots in D:\\Shots")
    asyncio.run(main._hud_action(object(), "lesson_delete", {"id": a}))
    assert texts() == [] and hud.broadcast[-1] == {"type": "lessons", "items": []}
    keep = learning.add("never open Spotify at startup")
    for junk in ("1", True, None, [1], {"x": 1}):
        asyncio.run(main._hud_action(object(), "lesson_delete", {"id": junk}))
    assert texts() == ["never open Spotify at startup"] and keep


def test_the_hud_action_is_allowed():
    from app.schemas import ActionMessage, parse_client_message

    assert isinstance(
        parse_client_message({"type": "action", "name": "lesson_delete", "args": {"id": 3}}), ActionMessage
    )


def test_public_list_is_json_safe_and_newest_first():
    learning.add("always save screenshots in D:\\Shots")
    time.sleep(0.01)
    learning.add("never open Spotify at startup")
    items = learning.public_list()
    json.dumps(items)
    assert [i["text"] for i in items] == ["never open Spotify at startup", "always save screenshots in D:\\Shots"]
    assert set(items[0]) == {"id", "text", "kind", "uses", "standing"}
