"""People & project memory: profiles, notes, commitments, undo."""

import pytest

from app import config, db, memory, people, undo
from app.agent import _run_tool
from app.tools import REGISTRY, load_all


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "a.db")
    monkeypatch.setattr(undo, "UNDO_DIR", tmp_path / "undo")
    monkeypatch.setattr(config, "CONFIRM_LEVEL", "normal")
    load_all()


def call(tool_name, **args):
    return _run_tool(REGISTRY[tool_name], args, run_id=1)


def test_profiles_notes_and_matching():
    call(
        "remember_about",
        name="Hassan Ali",
        note="Backend lead, prefers Slack over email",
        summary="backend lead",
        aliases=["H"],
    )
    call("remember_about", name="Ember", kind="project", note="My compiler project, written in Rust")
    assert people.find("hassan")["name"] == "Hassan Ali"  # first name matches the full name
    assert people.find("h")["name"] == "Hassan Ali"  # alias
    assert people.find("embr")["name"] == "Ember"  # close spelling
    text = REGISTRY["recall_about"].run({"name": "Hassan"}).text
    assert "prefers Slack" in text and "backend lead" in text
    assert "Ember (project)" in people.prompt_block()


def test_commitments_both_ways_with_reminder():
    r, _ = call("add_commitment", name="Hassan", text="send the API docs", minutes_from_now=60)
    assert r.ok and "reminder" in r.text.lower()
    call("add_commitment", name="Shahzain", text="feedback on the pitch deck", direction="they_owe")
    out = REGISTRY["list_commitments"].run({}).text
    assert "You owe:" in out and "API docs" in out and "Owed to you:" in out and "pitch deck" in out
    assert len(memory.pending_reminders()) == 1

    cid = people.commitments()[0]["id"]
    call("complete_commitment", id=cid)
    assert memory.pending_reminders() == []  # finishing it cancels the reminder
    assert "API docs" not in REGISTRY["list_commitments"].run({}).text


def test_people_changes_are_undoable():
    _, n_id = call("remember_about", name="Mahnoor", note="handles onboarding")
    _, c_id = call("add_commitment", name="Mahnoor", text="share the onboarding doc")
    undo.undo_entry(c_id)
    undo.undo_entry(n_id)
    e = people.find("Mahnoor")
    assert people.notes(e["id"]) == [] and people.commitments(e["id"]) == []


def test_forget_about_removes_everything():
    call("remember_about", name="Zara", note="designer")
    call("add_commitment", name="Zara", text="send mockups", minutes_from_now=30)
    r, _ = call("forget_about", name="Zara")
    assert r.ok and "cancelled 1 reminder" in r.text
    assert people.find("Zara") is None and people.commitments() == [] and memory.pending_reminders() == []
