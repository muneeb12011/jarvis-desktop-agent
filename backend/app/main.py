"""
FastAPI server - Jarvis's nervous system.

    WS   /ws/agent?token=...   live channel to the HUD
    POST /stt                   WAV audio in -> {"text": ...}
    POST /tts                   {"text": ...} -> audio/mpeg
    GET  /status                brains, voice engines, tools
    GET  /health

CLIENT -> SERVER (WebSocket)
  {"type": "chat", "text": "...", "voice": bool}
  {"type": "start", "goal": "..."}                  (v2 compatibility = chat)
  {"type": "stop"}
  {"type": "confirm", "id": "...", "approved": bool}
  {"type": "ping"}

SERVER -> CLIENT
  hello, state, brain, narration, tool_call, tool_result, screenshot, plan,
  confirm_request, confirm_closed, reply, reminder, notice, rejected, pong
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from .platform_utils import enable_dpi_awareness

enable_dpi_awareness()  # before anything touches the screen

from . import (  # noqa: E402
    activity,
    config,
    headsup,
    inbox,
    integrations,
    jobs,
    meeting,
    memory,
    onboarding,
    routines,
    scheduler,
    settings,
    textactions,
    voice,
)
from .agent import Agent  # noqa: E402
from .logging_config import configure_logging  # noqa: E402
from .schemas import (  # noqa: E402
    ActionMessage,
    CancelReminderMessage,
    ChatMessage,
    ConfirmMessage,
    MessageValidationError,
    NewSessionMessage,
    PingMessage,
    ReloadIntegrationsMessage,
    SettingsMessage,
    StartMessage,
    StopMessage,
    parse_client_message,
)
from .security import AUTH_TOKEN, RateLimiter, is_token_valid  # noqa: E402

configure_logging()
logger = logging.getLogger("jarvis")


class Hub:
    """All connected HUD windows. Sending never raises - a dead socket is
    dropped instead of crashing the agent run (the v2 'Task exception was
    never retrieved' crash in your logs)."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def broadcast(self, payload: dict) -> None:
        data = json.dumps(payload, default=str)
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                self.clients.discard(ws)

    async def send(self, ws: WebSocket, payload: dict) -> None:
        with contextlib.suppress(Exception):
            await ws.send_text(json.dumps(payload, default=str))


hub = Hub()
agent: Agent | None = None
job_manager: jobs.JobManager | None = None
engine: routines.Engine | None = None


def _job_finished(job: jobs.Job) -> None:
    """Feed finished jobs into the main conversation so follow-up questions work."""
    if agent is not None:
        agent.pending_notes.append(f"[Background job '{job.title}' {job.status}. Result: {job.result[:3000]}]")


async def _hook_start_job(ui: dict) -> str | None:
    assert job_manager is not None
    job = await job_manager.start(ui["task"], ui.get("title", ""), source="you")
    return f"Job id {job.id} is running in the background; the user will be told when it finishes."


async def _hook_cancel_job(ui: dict) -> str | None:
    assert job_manager is not None
    ok = await job_manager.cancel(str(ui.get("id", "")))
    return "Cancelled." if ok else "No running job with that id."


async def _fire_routine(r: dict, context: str = "") -> None:
    assert job_manager is not None
    task = r["action"] + (f"\n\nTrigger details: {context}" if context else "")
    task += f"\n\n(This is the user's routine '{r['name']}', fired {routines.describe_trigger(r)}.)"
    await job_manager.start(task, r["name"], source="routine", announce=r["announce"], allow_gui=True)
    await hub.broadcast(_routines_payload())


async def _hook_run_routine(ui: dict) -> str | None:
    r = routines.find(ui.get("id"))
    if not r:
        return "Routine not found."
    routines.mark_run(r["id"])
    await _fire_routine(r, "Run on request.")
    return "It's running in the background."


async def _hook_routines_changed(_ui: dict) -> str | None:
    await hub.broadcast(_routines_payload())
    return None


def _headsup_payload() -> dict:
    return {"type": "headsup_state", "enabled": headsup.enabled(), "muted": sorted(headsup.muted())}


async def _hook_headsup_changed(_ui: dict) -> str | None:
    await hub.broadcast(_headsup_payload())
    return None


async def _emit_headsup(s: headsup.Suggestion) -> None:
    await hub.broadcast(s.payload())


def _jarvis_busy() -> bool:
    """While Jarvis drives the screen itself, errors it causes are its own business."""
    return agent is not None and agent.busy


async def _finish_meeting(m: meeting.Meeting) -> str:
    """Summary, decisions and action items -> saved notes file + a card in the HUD."""
    assert agent is not None
    s = await asyncio.to_thread(meeting.summarize, agent.brain, m)
    mid, path = await asyncio.to_thread(meeting.save, m, s)
    await hub.broadcast({"type": "meeting", "state": "idle"})
    await hub.broadcast(
        {
            "type": "meeting_summary",
            "id": mid,
            "title": s["title"],
            "summary": s["summary"],
            "decisions": s["decisions"],
            "actions": s["action_items"],
            "path": str(path),
        }
    )
    actions = "\n".join(f"- {a['owner']}: {a['task']}" for a in s["action_items"]) or "(none)"
    return (
        f"Saved meeting #{mid} '{s['title']}' to {path}.\nSummary: {s['summary']}\nAction items:\n{actions}\n"
        "Tell the user briefly, and offer to track the action items (track_meeting_actions); don't do it unasked."
    )


async def _hook_meeting_stop(_ui: dict) -> str | None:
    assert meeting.COPILOT is not None
    m = await asyncio.to_thread(meeting.COPILOT.stop)
    if not m:
        return "No meeting was being recorded."
    return await _finish_meeting(m)


def _routines_payload() -> dict:
    return {
        "type": "routines",
        "items": [{**r, "describe": routines.describe_trigger(r)} for r in routines.all_routines()],
    }


def _workspaces_payload() -> dict:
    from .tools.automation import _all_workspaces

    return {
        "type": "workspaces",
        "items": [
            {"name": n, "windows": len(v["windows"]), "saved": v.get("saved")} for n, v in _all_workspaces().items()
        ],
    }


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global agent
    if not AUTH_TOKEN:
        logger.warning("JARVIS_AUTH_TOKEN is not set - auth is DISABLED. Run `python generate_token.py`.")
    settings.load()
    agent = Agent(hub.broadcast)
    global job_manager, engine
    job_manager = jobs.JobManager(hub.broadcast, agent.brain, on_finished=_job_finished)
    jobs.MANAGER = job_manager
    agent.hooks.update(
        {
            "start_job": _hook_start_job,
            "cancel_job": _hook_cancel_job,
            "run_routine": _hook_run_routine,
            "routines_changed": _hook_routines_changed,
            "headsup_changed": _hook_headsup_changed,
            "meeting_stop": _hook_meeting_stop,
        }
    )
    engine = routines.Engine(_fire_routine)
    loop = asyncio.get_running_loop()
    meeting.COPILOT = meeting.Copilot(lambda ev: loop.call_soon_threadsafe(asyncio.create_task, hub.broadcast(ev)))
    meeting.COPILOT.on_finished = lambda m: asyncio.run_coroutine_threadsafe(_finish_meeting(m), loop)
    headsup.WATCHER = headsup.Watcher(_emit_headsup, busy=_jarvis_busy, explain=headsup.make_explainer(agent.brain))
    logger.info(
        "Jarvis online. Brains: %s | tools: %d | STT: %s | TTS: %s",
        ", ".join(f"{p['name']}({p['model']})" for p in agent.brain.status() if p["configured"]),
        len(agent.tools),
        voice.stt_engine_name(),
        config.TTS_VOICE if config.TTS_ENABLED else "off",
    )
    voice.preload()
    from . import undo

    asyncio.get_running_loop().run_in_executor(None, undo.prune)
    sched = asyncio.create_task(scheduler.run_scheduler(hub.broadcast, lambda: bool(hub.clients)))
    asyncio.create_task(_load_integrations())
    background = [
        asyncio.create_task(inbox.run_inbox(agent, hub.broadcast)),
        asyncio.create_task(engine.run()),
        asyncio.create_task(activity.run_tracker()),
        asyncio.create_task(headsup.WATCHER.run()),
    ]
    yield
    for t in background:
        t.cancel()
    sched.cancel()
    integrations.shutdown()
    if meeting.COPILOT and meeting.COPILOT.recording:
        meeting.COPILOT.stop()


async def _load_integrations() -> None:
    try:
        status = await asyncio.to_thread(integrations.load)
    except Exception:
        logger.exception("Integration loading failed")
        return
    extra = sum(s.get("tools", 0) for s in status if s["status"] == "connected")
    logger.info("Integrations: %d loaded, %d extra tools", len(status), extra)
    await hub.broadcast({"type": "integrations", "items": status})


app = FastAPI(title="Jarvis", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "http://127.0.0.1", "null"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _check_http_token(token: str | None) -> None:
    if not is_token_valid(token):
        raise HTTPException(status_code=401, detail="Invalid token")


@app.websocket("/ws/agent")
async def agent_ws(websocket: WebSocket, token: str = ""):
    if not is_token_valid(token):
        logger.warning("Rejected WebSocket: invalid or missing token.")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    hub.clients.add(websocket)
    assert agent is not None
    limiter = RateLimiter(capacity=20, refill_seconds=5.0)

    greeting = await asyncio.to_thread(scheduler.build_greeting)
    await hub.send(
        websocket,
        {
            "type": "hello",
            "greeting": greeting,
            "busy": agent.busy,
            "brains": agent.brain.status(),
            "stt": voice.stt_engine_name(),
            "tts": config.TTS_VOICE if config.TTS_ENABLED else None,
            "tools": len(agent.tools),
            "user": config.USER_NAME,
            "settings": settings.current(),
            "integrations": integrations.current_status(),
            "activity": activity.enabled(),
            "headsup": headsup.enabled(),
            "setup": onboarding.state(),
            "meeting": (
                {"title": meeting.COPILOT.meeting.title, "started": meeting.COPILOT.meeting.started}
                if meeting.COPILOT and meeting.COPILOT.meeting
                else None
            ),
        },
    )
    await hub.send(websocket, scheduler.reminders_payload())
    await hub.send(websocket, _routines_payload())
    await hub.send(websocket, _workspaces_payload())
    await hub.send(websocket, _headsup_payload())
    if job_manager:
        for j in job_manager.list()[:6]:
            await hub.send(websocket, {"type": "job", "job": j})

    try:
        while True:
            raw_text = await websocket.receive_text()
            if not limiter.allow():
                await hub.send(websocket, {"type": "rejected", "reason": "Rate limit exceeded - slow down."})
                continue
            try:
                msg = parse_client_message(json.loads(raw_text))
            except (json.JSONDecodeError, MessageValidationError) as e:
                detail = str(e) if isinstance(e, MessageValidationError) else "Malformed JSON."
                await hub.send(websocket, {"type": "rejected", "reason": detail})
                continue

            if isinstance(msg, ChatMessage):
                await agent.submit(msg.text, voice=msg.voice)
            elif isinstance(msg, StartMessage):
                await agent.submit(msg.goal, voice=False)
            elif isinstance(msg, StopMessage):
                await agent.stop()
            elif isinstance(msg, ConfirmMessage):
                agent.resolve_confirm(msg.id, msg.approved)
                if job_manager:
                    job_manager.resolve_confirm(msg.id, msg.approved)
            elif isinstance(msg, ActionMessage):
                asyncio.create_task(_hud_action(websocket, msg.name, msg.args))
            elif isinstance(msg, SettingsMessage):
                cur = await asyncio.to_thread(settings.update, msg.changes)
                await hub.broadcast({"type": "settings", "settings": cur})
            elif isinstance(msg, CancelReminderMessage):
                memory.cancel_reminder(msg.id)
                await hub.broadcast(scheduler.reminders_payload())
            elif isinstance(msg, ReloadIntegrationsMessage):
                await hub.send(websocket, {"type": "notice", "level": "info", "text": "Reloading integrations..."})
                await _load_integrations()
            elif isinstance(msg, NewSessionMessage):
                await agent.stop(silent=True)
                agent.clear_session()
                await hub.send(websocket, {"type": "notice", "level": "info", "text": "Fresh conversation."})
            elif isinstance(msg, PingMessage):
                await hub.send(websocket, {"type": "pong", "busy": agent.busy})
    except WebSocketDisconnect:
        logger.info("HUD disconnected.")
    finally:
        hub.clients.discard(websocket)


@app.post("/stt")
async def stt(request: Request, x_jarvis_token: str | None = Header(default=None)):
    _check_http_token(x_jarvis_token)
    audio = await request.body()
    if len(audio) > 15 * 1024 * 1024:
        raise HTTPException(413, "Audio too large")
    try:
        text = await asyncio.to_thread(voice.transcribe, audio)
    except Exception as e:
        logger.warning("STT failed: %s", e)
        raise HTTPException(503, str(e)) from e
    return {"text": text}


@app.post("/tts")
async def tts(request: Request, x_jarvis_token: str | None = Header(default=None)):
    _check_http_token(x_jarvis_token)
    if not config.TTS_ENABLED:
        raise HTTPException(503, "TTS disabled")
    body = await request.json()
    text = str(body.get("text", ""))[:2000]
    preview_voice = body.get("voice") if body.get("voice") in {v["id"] for v in settings.VOICES} else None
    try:
        audio = await asyncio.wait_for(voice.synthesize(text, preview_voice, body.get("rate")), timeout=20)
    except Exception as e:
        logger.info("TTS unavailable (%s) - HUD will use the system voice.", e)
        raise HTTPException(503, "TTS unavailable") from e
    return Response(content=audio, media_type="audio/mpeg")


@app.get("/status")
def status_endpoint(x_jarvis_token: str | None = Header(default=None)):
    _check_http_token(x_jarvis_token)
    assert agent is not None
    return {
        "brains": agent.brain.status(),
        "busy": agent.busy,
        "stt": voice.stt_engine_name(),
        "tts": config.TTS_VOICE,
        "tools": sorted(agent.tools),
    }


async def _hud_action(ws: WebSocket, name: str, args: dict) -> None:
    """Buttons and toggles in the HUD that act directly, without a model call."""
    from .agent import IO_POOL, UI_POOL

    loop = asyncio.get_running_loop()
    try:
        if name == "cancel_job" and job_manager:
            await job_manager.cancel(str(args.get("id", "")))
        elif name == "run_routine":
            await _hook_run_routine({"id": args.get("id")})
        elif name == "toggle_routine":
            routines.set_enabled(args.get("id"), bool(args.get("enabled")))
            await hub.broadcast(_routines_payload())
        elif name == "delete_routine":
            routines.delete(args.get("id"))
            await hub.broadcast(_routines_payload())
        elif name == "set_activity":
            activity.set_enabled(bool(args.get("enabled")))
            await hub.broadcast({"type": "activity", "enabled": activity.enabled()})
        elif name == "setup_test_key":
            ok, text = await asyncio.to_thread(onboarding.test_gemini_key, str(args.get("key", "")))
            await hub.send(ws, {"type": "setup_result", "step": "key", "ok": ok, "message": text})
        elif name == "setup_save":
            assert agent is not None
            res = await asyncio.to_thread(onboarding.save, dict(args.get("values") or {}), agent.brain)
            await hub.send(ws, {"type": "setup_result", "step": args.get("step", ""), **res})
            if res["ok"]:
                await hub.broadcast({"type": "settings", "settings": settings.current()})
        elif name == "meeting_start":
            assert meeting.COPILOT is not None
            text = await asyncio.to_thread(meeting.COPILOT.start, str(args.get("title", "")))
            await hub.send(ws, {"type": "notice", "level": "info", "text": text})
        elif name == "meeting_stop":
            await hub.send(ws, {"type": "notice", "level": "info", "text": "Writing the meeting notes..."})
            text = await _hook_meeting_stop({})
            if text and text.startswith("No meeting"):
                await hub.send(ws, {"type": "notice", "level": "info", "text": text})
        elif name == "meeting_track":
            text = await asyncio.to_thread(meeting.track_actions, int(args.get("id") or 0) or None)
            await hub.send(ws, {"type": "notice", "level": "info", "text": text})
            await hub.broadcast(scheduler.reminders_payload())
        elif name == "open_meeting_notes":
            rec = meeting.get(int(args.get("id") or 0) or None)
            if rec and rec.get("path"):
                import os

                await asyncio.to_thread(os.startfile, rec["path"])  # type: ignore[attr-defined]
        elif name == "set_headsup":
            headsup.set_enabled(bool(args.get("enabled")))
            await hub.broadcast(_headsup_payload())
        elif name == "headsup_mute":
            headsup.set_muted(str(args.get("app", "")), bool(args.get("mute", True)))
            await hub.broadcast(_headsup_payload())
        elif name == "clear_activity":
            n = await asyncio.to_thread(activity.clear)
            await hub.send(ws, {"type": "notice", "level": "info", "text": f"Activity history cleared ({n} entries)."})
        elif name == "restore_workspace":
            from .tools.automation import restore_workspace

            out = await loop.run_in_executor(UI_POOL, restore_workspace, str(args.get("name", "")))
            await hub.send(ws, {"type": "notice", "level": "info", "text": out})
        elif name == "delete_workspace":
            from .tools.automation import delete_workspace

            delete_workspace(str(args.get("name", "")))
            await hub.broadcast(_workspaces_payload())
        elif name == "grab_selection":
            sel = await loop.run_in_executor(UI_POOL, textactions.grab_selection)
            await hub.send(ws, {"type": "selection", **sel})
        elif name == "text_action":
            assert agent is not None
            text = await loop.run_in_executor(
                IO_POOL,
                textactions.run_action,
                agent.brain,
                str(args.get("action", "explain")),
                str(args.get("text", ""))[:20000],
                str(args.get("lang", "")),
                str(args.get("instruction", "")),
            )
            await hub.send(
                ws,
                {
                    "type": "text_action_result",
                    "action": args.get("action"),
                    "text": text,
                    "replaceable": args.get("action") in textactions.REPLACEABLE,
                },
            )
        elif name == "paste_back":
            await loop.run_in_executor(
                UI_POOL, textactions.paste_back, int(args.get("hwnd") or 0), str(args.get("text", ""))
            )
        elif name == "undo":
            from . import undo

            msg = await loop.run_in_executor(UI_POOL, undo.undo_entry, int(args.get("id") or 0))
            await hub.send(ws, {"type": "notice", "level": "info", "text": msg})
            await hub.send(ws, {"type": "undone", "id": args.get("id")})
        elif name == "refresh":
            await hub.send(ws, _routines_payload())
            await hub.send(ws, _workspaces_payload())
    except Exception as e:
        logger.exception("HUD action %s failed", name)
        await hub.send(ws, {"type": "notice", "level": "warn", "text": f"{name} failed: {e}"})


# ------------------------------------------------------------------ inbound API
# Lets other tools drive Jarvis: phone shortcuts, Stream Deck, n8n, scripts, cron.
#   curl -X POST http://127.0.0.1:8000/api/command -H "X-Jarvis-Token: ..." \
#        -H "Content-Type: application/json" -d '{"text": "lock my pc", "wait": true}'


@app.post("/api/command")
async def api_command(request: Request, x_jarvis_token: str | None = Header(default=None)):
    _check_http_token(x_jarvis_token)
    assert agent is not None
    body = await request.json()
    text = str(body.get("text", "")).strip()[:4000]
    if not text:
        raise HTTPException(400, "Missing 'text'")
    await hub.broadcast({"type": "external", "text": text, "source": str(body.get("source", "API"))[:40]})
    await agent.submit(text, voice=bool(body.get("speak", False)))
    if body.get("wait"):
        try:
            if agent.task:
                await asyncio.wait_for(asyncio.shield(agent.task), timeout=float(body.get("timeout", 120)))
        except TimeoutError:
            return {"accepted": True, "done": False}
        return {"accepted": True, "done": True, "reply": agent.last_reply}
    return {"accepted": True}


@app.post("/api/notify")
async def api_notify(request: Request, x_jarvis_token: str | None = Header(default=None)):
    """Make Jarvis announce something (e.g. 'Build finished', 'Someone is at the door')."""
    _check_http_token(x_jarvis_token)
    body = await request.json()
    text = str(body.get("text", "")).strip()[:1000]
    if not text:
        raise HTTPException(400, "Missing 'text'")
    await hub.broadcast({"type": "reminder", "kind": "notify", "text": text, "speak": bool(body.get("speak", True))})
    return {"delivered": len(hub.clients)}


@app.get("/api/integrations")
def api_integrations(x_jarvis_token: str | None = Header(default=None)):
    _check_http_token(x_jarvis_token)
    return {"integrations": integrations.current_status()}


@app.get("/health")
def health():
    return {"status": "ok"}
