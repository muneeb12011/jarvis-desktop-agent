"""
Validation for every message arriving over the WebSocket. It's untrusted
input like any HTTP body, so it gets a schema instead of hopeful dict indexing.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError

__all__ = [
    "ActionMessage",
    "ChatMessage",
    "ConfirmMessage",
    "MessageValidationError",
    "CancelReminderMessage",
    "NewSessionMessage",
    "PingMessage",
    "ReloadIntegrationsMessage",
    "SettingsMessage",
    "StartMessage",
    "StopMessage",
    "parse_client_message",
]


class MessageValidationError(Exception):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class StartMessage(BaseModel):
    """Legacy v2 message - treated exactly like a chat message now."""

    type: Literal["start"]
    goal: str = Field(min_length=1, max_length=4000)


class StopMessage(BaseModel):
    type: Literal["stop"]


class ChatMessage(BaseModel):
    type: Literal["chat"]
    text: str = Field(min_length=1, max_length=4000)
    voice: bool = False  # came from the microphone (reply will be spoken)


class ConfirmMessage(BaseModel):
    type: Literal["confirm"]
    id: str = Field(min_length=1, max_length=64)
    approved: bool


class PingMessage(BaseModel):
    type: Literal["ping"]


class SettingsMessage(BaseModel):
    type: Literal["settings"]
    changes: dict[str, str | bool] = Field(default_factory=dict)


class CancelReminderMessage(BaseModel):
    type: Literal["cancel_reminder"]
    id: int


class NewSessionMessage(BaseModel):
    type: Literal["new_session"]


class ReloadIntegrationsMessage(BaseModel):
    type: Literal["reload_integrations"]


HUD_ACTIONS = Literal[
    "cancel_job",
    "run_routine",
    "toggle_routine",
    "delete_routine",
    "set_activity",
    "clear_activity",
    "restore_workspace",
    "delete_workspace",
    "grab_selection",
    "text_action",
    "paste_back",
    "refresh",
    "undo",
    "set_headsup",
    "headsup_mute",
    "meeting_start",
    "meeting_stop",
    "meeting_track",
    "open_meeting_notes",
    "setup_test_key",
    "setup_save",
]


class ActionMessage(BaseModel):
    """Direct HUD actions that don't need the language model (buttons, toggles, text actions)."""

    type: Literal["action"]
    name: HUD_ACTIONS
    args: dict = Field(default_factory=dict)


ClientMessage = (
    StartMessage
    | StopMessage
    | ChatMessage
    | ConfirmMessage
    | PingMessage
    | SettingsMessage
    | CancelReminderMessage
    | NewSessionMessage
    | ReloadIntegrationsMessage
    | ActionMessage
)

_SCHEMAS: dict[str, type[BaseModel]] = {
    "start": StartMessage,
    "stop": StopMessage,
    "chat": ChatMessage,
    "confirm": ConfirmMessage,
    "ping": PingMessage,
    "settings": SettingsMessage,
    "cancel_reminder": CancelReminderMessage,
    "new_session": NewSessionMessage,
    "reload_integrations": ReloadIntegrationsMessage,
    "action": ActionMessage,
}


def parse_client_message(raw: dict) -> ClientMessage:
    if not isinstance(raw, dict):
        raise MessageValidationError("Message must be a JSON object.")
    msg_type = raw.get("type")
    schema = _SCHEMAS.get(msg_type) if isinstance(msg_type, str) else None
    if schema is None:
        raise MessageValidationError(f"Unknown message type: {msg_type!r}")
    try:
        return schema.model_validate(raw)  # type: ignore[return-value]
    except ValidationError as e:
        raise MessageValidationError(f"Invalid {msg_type} message: {e.errors()[0]['msg']}") from e
