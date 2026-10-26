"""
Tool registry. Every capability Jarvis has is a plain Python function
registered with @tool. The registry turns them into native function-calling
specs for whichever LLM is active, and tells the agent which calls need your
approval before they run.

Adding a capability = write one function in one of the modules below.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import config
from ..actuator import UserAbort
from ..providers import ToolSpec

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    text: str
    ok: bool = True
    image_b64: str | None = None  # screenshot to show the model (and the HUD)
    ui: dict[str, Any] | None = None  # extra event for the HUD (e.g. a plan)


ConfirmRule = Callable[[dict[str, Any]], str | None]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., ToolResult | str]
    category: str = "general"
    ui_thread: bool = False  # touches mouse/keyboard/UI Automation
    confirm: ConfirmRule | None = None  # returns a reason when approval is needed
    mutates: bool = False  # changes something (strict mode asks)
    source: str = "builtin"  # builtin | plugin:<file> | mcp:<server> | http:<name>

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)

    def confirmation_reason(self, args: dict[str, Any]) -> str | None:
        level = config.CONFIRM_LEVEL
        if level == "off":
            return None
        if self.confirm:
            reason = self.confirm(args)
            if reason:
                return reason
        if level == "strict" and self.mutates:
            return f"{self.name} will change something on your computer."
        return None

    def run(self, args: dict[str, Any]) -> ToolResult:
        try:
            out = self.func(**args)
        except UserAbort:
            raise
        except TypeError as e:
            return ToolResult(f"Bad arguments for {self.name}: {e}", ok=False)
        except Exception as e:  # tools must never crash the agent loop
            logger.exception("Tool %s failed", self.name)
            return ToolResult(f"{self.name} failed: {type(e).__name__}: {e}", ok=False)
        if isinstance(out, ToolResult):
            return out
        return ToolResult(str(out))


REGISTRY: dict[str, Tool] = {}
_current_source = "builtin"


def set_source(source: str) -> None:
    """Plugins are imported under their own source label so a reload can remove them."""
    global _current_source
    _current_source = source


def tool(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
    *,
    category: str = "general",
    ui_thread: bool = False,
    confirm: ConfirmRule | None = None,
    mutates: bool = False,
):
    def decorator(func: Callable[..., ToolResult | str]):
        params: dict[str, Any] = {"type": "object", "properties": properties or {}}
        if required:
            params["required"] = required
        REGISTRY[name] = Tool(name, description, params, func, category, ui_thread, confirm, mutates, _current_source)
        return func

    return decorator


def always(reason: str) -> ConfirmRule:
    return lambda _args: reason


def load_all() -> dict[str, Tool]:
    """Import every tool module so their @tool decorators run."""
    from . import (  # noqa: F401
        automation,
        extras,
        files,
        headsup_tools,
        knowledge,
        meeting_tools,
        people_tools,
        screen,
        system,
        web,
    )

    return REGISTRY


def specs() -> list[ToolSpec]:
    return [t.spec() for t in load_all().values()]


def get(name: str) -> Tool | None:
    return load_all().get(name)
