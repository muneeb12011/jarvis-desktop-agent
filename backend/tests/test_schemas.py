"""
Tests for schemas.py — confirms malformed/oversized/unknown input is
rejected rather than silently accepted or crashing the WebSocket loop.
"""

import pytest

from app.schemas import ChatMessage, MessageValidationError, StartMessage, StopMessage, parse_client_message


def test_valid_start_message():
    msg = parse_client_message({"type": "start", "goal": "open notepad"})
    assert isinstance(msg, StartMessage)
    assert msg.goal == "open notepad"


def test_valid_stop_message():
    msg = parse_client_message({"type": "stop"})
    assert isinstance(msg, StopMessage)


def test_valid_chat_message():
    msg = parse_client_message({"type": "chat", "text": "hey jarvis"})
    assert isinstance(msg, ChatMessage)


def test_rejects_unknown_type():
    with pytest.raises(MessageValidationError):
        parse_client_message({"type": "delete_everything"})


def test_rejects_missing_goal():
    with pytest.raises(MessageValidationError):
        parse_client_message({"type": "start"})


def test_rejects_empty_goal():
    with pytest.raises(MessageValidationError):
        parse_client_message({"type": "start", "goal": ""})


def test_rejects_oversized_goal():
    with pytest.raises(MessageValidationError):
        parse_client_message({"type": "start", "goal": "x" * 5000})


def test_rejects_non_dict_type_field():
    with pytest.raises(MessageValidationError):
        parse_client_message({"type": 12345})
