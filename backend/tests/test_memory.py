"""
Tests for memory.py, isolated from the real memory.db by pointing DB_PATH
at a temp file for the duration of each test.
"""

import importlib

import pytest


@pytest.fixture
def memory_module(tmp_path, monkeypatch):
    import app.memory as memory

    importlib.reload(memory)
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test_memory.db")
    return memory


def test_add_and_get_facts(memory_module):
    memory_module.add_fact("likes dark mode")
    memory_module.add_fact("uses VS Code")
    facts = memory_module.get_facts()
    assert "likes dark mode" in facts
    assert "uses VS Code" in facts


def test_facts_respect_limit(memory_module):
    for i in range(5):
        memory_module.add_fact(f"fact {i}")
    facts = memory_module.get_facts(limit=2)
    assert len(facts) == 2


def test_conversation_round_trip(memory_module):
    memory_module.add_turn("user", "hello")
    memory_module.add_turn("assistant", "hi there")
    convo = memory_module.get_recent_conversation()
    assert convo[-2] == {"role": "user", "content": "hello"}
    assert convo[-1] == {"role": "assistant", "content": "hi there"}


def test_context_block_empty_when_no_memory(memory_module):
    assert memory_module.build_context_block() == "(no memory yet)"


def test_context_block_includes_facts_and_convo(memory_module):
    memory_module.add_fact("works as an engineer")
    memory_module.add_turn("user", "what's my job again?")
    block = memory_module.build_context_block()
    assert "works as an engineer" in block
    assert "what's my job again?" in block
