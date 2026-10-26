"""
The agent loop: one loop for everything. Small talk, questions and
multi-step computer tasks all go through the same cycle:

    think (LLM with tools) -> act (run tool calls) -> observe (results +
    fresh screenshot) -> think again ... -> final answer

This replaces v2's chat-classifier + planner + one-pixel-click-per-turn
pipeline. Key properties:

- Never blocks the event loop: LLM calls and tools run in worker threads,
  so the WebSocket, heartbeats and Stop stay responsive (v2 froze here).
- Stop works instantly, even mid-LLM-call.
- Risky tool calls pause for your approval in the HUD.
- GUI tools run on one dedicated thread (UI Automation/COM need that).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import capture, config, db, memory, prompts, tools, toolsets, undo, verify
from .actuator import UserAbort
from .providers import Brain, ProviderError, ToolCall, describe_image
from .tools import ToolResult

logger = logging.getLogger(__name__)

Emit = Callable[[dict], Awaitable[None]]

MAX_HISTORY_MESSAGES = 60
MAX_TOOL_TEXT_IN_HISTORY = 2500


def _com_init() -> None:
    try:
        import comtypes  # type: ignore

        comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
    except Exception:
        pass


# One mouse, one keyboard: every GUI action from any agent (main or background
# job) goes through this single thread. Non-GUI work shares a wider pool.
UI_POOL = ThreadPoolExecutor(1, thread_name_prefix="jarvis-ui", initializer=_com_init)
IO_POOL = ThreadPoolExecutor(8, thread_name_prefix="jarvis-io")

Hook = Callable[[dict], Awaitable[str | None]]


class Agent:
    def __init__(
        self,
        emit: Emit,
        brain: Brain | None = None,
        *,
        persist: bool = True,
        allow_gui: bool = True,
        prompt_extra: str = "",
    ) -> None:
        self.emit = emit
        self.brain = brain or Brain()
        self.tools = tools.load_all()
        self.specs = [t.spec() for t in self.tools.values()]
        self.history: list[dict[str, Any]] = []
        self.last_reply = ""
        self.last_status = ""
        self._used_tools: list = []
        self._checks = 0
        self.task: asyncio.Task | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.persist = persist  # write turns to long-term conversation memory
        self.allow_gui = allow_gui  # background jobs don't get the mouse/keyboard
        self.prompt_extra = prompt_extra
        self.enabled_groups: set[str] = set()
        self.hooks: dict[str, Hook] = {}  # ToolResult.ui["type"] -> async handler (e.g. start a job)
        self.pending_notes: list[str] = []  # e.g. finished background jobs, injected on the next turn
        self._stop_event: asyncio.Event | None = None
        self._stopped = False
        self._ui_pool = UI_POOL
        self._io_pool = IO_POOL
        self._last_provider = ""

    # ------------------------------------------------------------ public API

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    async def submit(self, text: str, voice: bool = False) -> None:
        """Handle a new request. A new request while busy interrupts the old one."""
        if self.busy:
            await self.stop(silent=True)
            if self.task:
                try:
                    await asyncio.wait_for(asyncio.shield(self.task), timeout=5)
                except (TimeoutError, Exception):
                    pass
        self.task = asyncio.create_task(self._run(text, voice))

    async def stop(self, silent: bool = False) -> None:
        self._stopped = True
        if self._stop_event:
            self._stop_event.set()
        for fut in self.pending.values():
            if not fut.done():
                fut.set_result(False)
        if not silent and self.busy:
            await self.emit({"type": "notice", "level": "info", "text": "Stopping."})

    def resolve_confirm(self, confirm_id: str, approved: bool) -> None:
        fut = self.pending.get(confirm_id)
        if fut and not fut.done():
            fut.set_result(approved)

    def clear_session(self) -> None:
        self.history.clear()
        self.enabled_groups.clear()

    def _current_specs(self) -> list:
        return toolsets.select(self.tools, self.enabled_groups, self.allow_gui)

    # ------------------------------------------------------------ run

    async def _run(self, text: str, voice: bool) -> None:
        self._used_tools: list = []
        self._checks = 0
        self._stopped = False
        self._stop_event = asyncio.Event()
        self._repair_history()
        notes = ""
        if self.pending_notes:
            notes = "\n".join(self.pending_notes) + "\n\n"
            self.pending_notes.clear()
        self.history.append({"role": "user", "text": notes + text})
        known = set(toolsets.available_groups(self.tools, self.allow_gui))
        self.enabled_groups |= toolsets.keyword_groups(text, known)
        if self.persist:
            memory.add_turn("user", text)
        run_id = db.start_run(text)
        await self.emit({"type": "state", "state": "thinking"})

        status = "completed"
        final = ""
        answered_in_history = False
        try:
            final, answered_in_history = await self._loop(run_id, voice, text)
            if self._stopped:
                status = "stopped"
        except UserAbort:
            status, final = "aborted", "Failsafe triggered. I've let go of the mouse and keyboard."
        except ProviderError as e:
            status, final = "error", f"I can't reach my language model right now. {e}"
            logger.warning("Provider error: %s", e)
        except Exception as e:
            logger.exception("Agent run %s crashed", run_id)
            status, final = "error", f"Something went wrong on my side: {type(e).__name__}: {e}"

        if final:
            if not answered_in_history:
                self.history.append({"role": "assistant", "text": final, "tool_calls": []})
            if self.persist:
                memory.add_turn("assistant", final)
            self.last_reply = final
            await self.emit({"type": "reply", "text": final, "speak": voice, "status": status})
        self.last_status = status
        db.end_run(run_id, status)
        self._compact_history()
        await self.emit({"type": "state", "state": "idle"})

    async def _loop(self, run_id: int, voice: bool, goal: str) -> tuple[str, bool]:
        loop = asyncio.get_running_loop()
        for step in range(1, config.MAX_AGENT_STEPS + 1):
            if self._stopped:
                return f"Stopped, {config.address()}.", False

            self.specs = self._current_specs()  # integrations and enabled groups change at runtime
            system = prompts.system_prompt(voice) + self.prompt_extra
            msgs = self._prepared_history()
            turn = await self._race(loop.run_in_executor(self._io_pool, self.brain.complete, system, msgs, self.specs))
            if turn is None:
                return f"Stopped, {config.address()}.", False

            if turn.provider != self._last_provider:
                self._last_provider = turn.provider
                await self.emit({"type": "brain", "provider": turn.provider, "status": self.brain.status()})

            self.history.append(
                {
                    "role": "assistant",
                    "text": turn.text,
                    "tool_calls": turn.tool_calls,
                    "raw": turn.raw,
                    "provider": turn.provider,
                    "model": turn.model,
                }
            )

            if not turn.tool_calls:
                draft = turn.text or "Done."
                if config.VERIFY != "off" and self._checks < 2 and verify.needs_check(self._used_tools):
                    self._checks += 1
                    verdict = await self._self_check(goal, draft, run_id, step)
                    if verdict is None:
                        return f"Stopped, {config.address()}.", False
                    if verdict["verdict"] == "not_done":
                        if self._checks == 1:
                            self.history.append(
                                {
                                    "role": "user",
                                    "text": (
                                        f"[Self-check: this doesn't look finished. {verdict['reason']} "
                                        f"Suggested fix: {verdict['fix'] or 'try a different approach'}. Fix it, then reply "
                                        "again. If it truly can't be done, say so plainly.]"
                                    ),
                                }
                            )
                            self._used_tools = []  # the next round's actions get checked on their own
                            continue
                        return f"{draft}\n\n(Self-check: I'm not confident this worked. {verdict['reason']})", True
                return draft, True

            if turn.text:
                await self.emit({"type": "narration", "text": turn.text})

            last_image: tuple[str, str] | None = None
            for call in turn.tool_calls:
                result = await self._execute(call, run_id, step)
                content = result.text if result.ok else f"ERROR: {result.text}"
                self.history.append(
                    {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content[:8000]}
                )
                if result.image_b64:
                    last_image = (call.name, result.image_b64)
                if self._stopped:
                    break

            if last_image and not self._stopped:
                name, img = last_image
                await self.emit({"type": "screenshot", "b64": img})
                if getattr(self.brain.current, "supports_vision", False):
                    self.history.append({"role": "user", "text": f"[Screenshot after {name}]", "image_b64": img})
                else:
                    desc = await self._race(loop.run_in_executor(self._io_pool, describe_image, img, goal))
                    self.history.append(
                        {"role": "user", "text": f"[Screen description after {name}]\n{desc or '(cancelled)'}"}
                    )

        return (
            f"I've taken {config.MAX_AGENT_STEPS} steps without finishing, so I've paused here. "
            "Tell me how you'd like to proceed."
        ), False

    async def _execute(self, call: ToolCall, run_id: int, step: int) -> ToolResult:
        if call.name == "enable_tools":
            return await self._enable_tools(call)
        tool = self.tools.get(call.name)
        await self.emit(
            {
                "type": "tool_call",
                "id": call.id,
                "name": call.name,
                "args": _safe_args(call.args),
                "category": tool.category if tool else "unknown",
            }
        )
        result: ToolResult | None = None
        undo_id: int | None = None
        if tool is None:
            result = ToolResult(f"Unknown tool '{call.name}'.", ok=False)
        elif tool.ui_thread and not self.allow_gui:
            result = ToolResult("Background jobs can't use the mouse/keyboard. Use direct tools instead.", ok=False)
        else:
            reason = tool.confirmation_reason(call.args)
            if reason and not await self._confirm(call, reason):
                result = ToolResult("The user declined this action. Don't retry it; ask what they'd prefer.", ok=False)
            if result is None:
                if self._stopped:
                    result = ToolResult("Cancelled by the user.", ok=False)
                else:
                    pool = self._ui_pool if tool.ui_thread else self._io_pool
                    if tool.ui_thread:
                        await self.emit({"type": "state", "state": "executing"})
                    loop = asyncio.get_running_loop()
                    outcome = await self._race(loop.run_in_executor(pool, _run_tool, tool, call.args, run_id))
                    if outcome is None:
                        result = ToolResult("Cancelled by the user.", ok=False)
                    else:
                        result, undo_id = outcome
                    self._used_tools.append(tool)

        await self.emit(
            {
                "type": "tool_result",
                "id": call.id,
                "name": call.name,
                "ok": result.ok,
                "summary": result.text[:400],
                "undo_id": undo_id,
            }
        )
        if result.ui:
            kind = result.ui.get("type")
            if kind in self.hooks:
                extra = await self.hooks[kind](result.ui)
                if extra:
                    result = ToolResult(f"{result.text}\n{extra}", ok=result.ok, image_b64=result.image_b64)
            elif kind == "reminders_changed":
                await self.emit({"type": "reminders", "items": memory.pending_reminders()})
            else:
                await self.emit(result.ui)
        try:
            db.log_action(run_id, step, "", call.name, json.dumps(call.args, default=str), result.text)
        except Exception:
            logger.debug("log_action failed", exc_info=True)
        return result

    async def _self_check(self, goal: str, draft: str, run_id: int, step: int) -> dict | None:
        """Independent check that the task really happened. Returns None if Stop was pressed."""
        loop = asyncio.get_running_loop()
        cid = f"check_{run_id}_{step}"
        await self.emit({"type": "tool_call", "id": cid, "name": "self_check", "args": {}, "category": "agent"})
        image = None
        if (
            self.allow_gui
            and any(t.ui_thread for t in self._used_tools)
            and getattr(self.brain.current, "supports_vision", False)
        ):
            try:
                frame = await loop.run_in_executor(self._ui_pool, capture.grab)
                image = capture.to_b64_jpeg(frame.image, quality=70)
            except Exception:
                image = None
        verdict = await self._race(
            loop.run_in_executor(self._io_pool, verify.check, self.brain, goal, list(self.history), draft, image)
        )
        if verdict is None:
            return None
        ok = verdict["verdict"] != "not_done"
        await self.emit(
            {
                "type": "tool_result",
                "id": cid,
                "name": "self_check",
                "ok": ok,
                "summary": f"{verdict['verdict'].replace('_', ' ')}: {verdict['reason']}",
            }
        )
        return verdict

    async def _enable_tools(self, call: ToolCall) -> ToolResult:
        groups = call.args.get("groups") or []
        if isinstance(groups, str):
            groups = [groups]
        known = toolsets.available_groups(self.tools, self.allow_gui)
        added = [g for g in groups if g in known]
        self.enabled_groups.update(added)
        names = sorted(t.name for g in added for t in known[g])
        unknown = [g for g in groups if g not in known]
        await self.emit(
            {
                "type": "tool_call",
                "id": call.id,
                "name": "enable_tools",
                "args": {"groups": groups},
                "category": "agent",
            }
        )
        text = f"Enabled {', '.join(added) or 'nothing'}. New tools: {', '.join(names)}."
        if unknown:
            text += f" Unknown groups: {', '.join(unknown)}."
        await self.emit(
            {"type": "tool_result", "id": call.id, "name": "enable_tools", "ok": bool(added), "summary": text[:400]}
        )
        return ToolResult(text, ok=bool(added))

    async def _confirm(self, call: ToolCall, reason: str) -> bool:
        cid = uuid.uuid4().hex[:10]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[cid] = fut
        await self.emit(
            {
                "type": "confirm_request",
                "id": cid,
                "name": call.name,
                "args": _safe_args(call.args),
                "reason": reason,
                "timeout": config.CONFIRM_TIMEOUT_SECONDS,
            }
        )
        try:
            return bool(await asyncio.wait_for(fut, timeout=config.CONFIRM_TIMEOUT_SECONDS))
        except TimeoutError:
            await self.emit({"type": "notice", "level": "warn", "text": "No answer - I skipped that action."})
            return False
        finally:
            self.pending.pop(cid, None)
            await self.emit({"type": "confirm_closed", "id": cid})

    async def _race(self, fut: Awaitable[Any]) -> Any:
        """Await `fut`, but return None immediately if Stop is pressed."""
        assert self._stop_event is not None
        work = asyncio.ensure_future(fut)
        stopper = asyncio.ensure_future(self._stop_event.wait())
        done, _ = await asyncio.wait({work, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            stopper.cancel()
            return work.result()
        work.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        return None

    # ------------------------------------------------------------ history hygiene

    def _repair_history(self) -> None:
        """After a Stop, the last assistant turn may have tool calls with no
        results. Every provider rejects that, so fill in the gaps."""
        for idx in range(len(self.history) - 1, -1, -1):
            m = self.history[idx]
            if m["role"] == "assistant" and m.get("tool_calls"):
                answered = {h.get("tool_call_id") for h in self.history[idx + 1 :] if h["role"] == "tool"}
                for tc in m["tool_calls"]:
                    if tc.id not in answered:
                        self.history.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "name": tc.name,
                                "content": "Cancelled by the user.",
                            }
                        )
                break

    def _prepared_history(self) -> list[dict]:
        msgs = self.history[-MAX_HISTORY_MESSAGES:]
        # Never start mid tool-exchange: begin at a real user message.
        start = next(
            (
                i
                for i, m in enumerate(msgs)
                if m["role"] == "user" and not m.get("image_b64") and not str(m.get("text", "")).startswith("[")
            ),
            0,
        )
        msgs = msgs[start:]
        last_img = max((i for i, m in enumerate(msgs) if m.get("image_b64")), default=-1)
        out = []
        for i, m in enumerate(msgs):
            if m.get("image_b64") and i != last_img:
                m = {**m, "image_b64": None, "text": m["text"] + " (older screenshot removed)"}
            out.append(m)
        return out

    def _compact_history(self) -> None:
        for m in self.history:
            m.pop("image_b64", None)
            if m["role"] == "tool" and len(m["content"]) > MAX_TOOL_TEXT_IN_HISTORY:
                m["content"] = m["content"][:MAX_TOOL_TEXT_IN_HISTORY] + " ...(truncated)"
        if len(self.history) > MAX_HISTORY_MESSAGES * 2:
            self.history = self.history[-MAX_HISTORY_MESSAGES:]


def _run_tool(tool, args: dict, run_id: int) -> tuple[ToolResult, int | None]:
    """Run a tool with an undo snapshot taken first, on the tool's own thread."""
    pending = undo.before(tool.name, tool.source, args, tool.mutates)
    result = tool.run(args)
    try:
        undo_id = undo.commit(pending, run_id, args, result.ok, result.text)
    except Exception:
        logger.debug("undo journal failed", exc_info=True)
        undo_id = None
    return result, undo_id


def _safe_args(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        s = v if isinstance(v, (int, float, bool)) or v is None else str(v)
        if isinstance(s, str) and len(s) > 300:
            s = s[:300] + "..."
        out[k] = s
    return out
