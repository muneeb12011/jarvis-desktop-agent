"""
Tools for the other AI tools on this PC: what are they doing, which one needs me, and (with approval)
nudge one of them.

Reading is free. Acting on another agent is not: approving its prompt or typing into it are exactly the
things a hostile web page would love Jarvis to do on its behalf, so both always ask, even in
work-without-asking mode, and both refuse unless the right window really is in front and really is asking.
These tools need the screen, so they run in the foreground assistant (background jobs can't use them).
"""

from __future__ import annotations

import re
import time

from .. import actuator, ai_agents, jobs
from ..autonomy import HardConfirm
from . import ToolResult, tool

_ACTIONS = ("focus", "interrupt", "approve", "deny", "send")
MAX_READ = 6  # windows read per overview; reading each takes up to a second
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _job_lines() -> list[str]:
    mgr = jobs.MANAGER
    if not mgr:
        return []
    out = []
    for j in mgr.list():
        if j["status"] in ("queued", "running"):
            now = f" now: {j['step']}" if j.get("step") else ""
            state = (
                "paused" if j.get("paused") else "needs you (waiting for approval)" if j.get("waiting") else j["status"]
            )
            out.append(f'[job:{j["id"]}] Jarvis background task "{j["title"]}" | {state} | {j["steps"]} steps{now}')
    return out


@tool(
    "ai_agents_overview",
    "List the AI tools running on this computer (Claude Code, Codex, Gemini CLI, Aider, Cursor, Windsurf, "
    "ChatGPT/Claude/LM Studio apps, AI chats in a browser) plus Jarvis's own background tasks, and for each "
    "say what it looks like it's doing: working, needs the user (stopped on a question or approval), error, "
    "or idle. Use for 'what are my AI agents doing', 'is Claude Code stuck', 'does anything need me'. "
    "Then explain each one to the user in plain words.",
    category="agents",
    ui_thread=True,
)
def ai_agents_overview() -> ToolResult:
    agents = ai_agents.scan()
    lines: list[str] = []
    needs = errors = 0
    seen: dict[int, ai_agents.Reading] = {}  # a window is read once, however many tools share it
    for a in agents:
        if a.hwnd and (a.hwnd in seen or len(seen) < MAX_READ):
            fresh = a.hwnd not in seen
            reading = seen[a.hwnd] if not fresh else seen.setdefault(a.hwnd, ai_agents.read_agent(a)[0])
            if fresh:
                needs += reading.state == "needs you"
                errors += reading.state == "error"
            lines.append(ai_agents.one_line(a, reading))
        else:
            lines.append(
                f"[{a.id}] {a.label} | running {a.age()} | not read" + (f' | "{a.title[:50]}"' if a.title else "")
            )
    lines += _job_lines()
    if not lines:
        return ToolResult("I don't see any AI tools or background tasks running.")
    head = f"{len(agents)} AI tool{'s' if len(agents) != 1 else ''} running"
    head += f"; {needs} need{'s' if needs == 1 else ''} you" if needs else ""
    head += f"; {errors} showing an error" if errors else ""
    return ToolResult(head + ".\n" + "\n".join(lines))


@tool(
    "ai_agent_detail",
    "Look closely at ONE AI tool: what it's working on, its latest output, what it's asking if it's stopped "
    "on a prompt, and any error. 'agent' is an id from ai_agents_overview, a name like 'claude code' or "
    "'cursor', or part of its window title. Use for 'what is Claude Code doing', 'why did Cursor stop', "
    "'explain that agent'.",
    {"agent": {"type": "string"}, "lines": {"type": "integer", "description": "How many recent lines (default 30)"}},
    ["agent"],
    category="agents",
    ui_thread=True,
)
def ai_agent_detail(agent: str, lines: int = 30) -> ToolResult:
    if agent.strip().lower().startswith("job:") and jobs.MANAGER:
        j = jobs.MANAGER.get(agent.split(":", 1)[1].strip())
        if not j:
            return ToolResult(f"No background task '{agent}'.", ok=False)
        state = "paused" if j.get("paused") else "waiting for the user's approval" if j.get("waiting") else j["status"]
        out = [f'Jarvis background task "{j["title"]}": {state}, {j["steps"]} steps.']
        if j.get("step"):
            out.append(f"Doing now: {j['step']}.")
        if j["activity"]:
            out.append("Recent activity:\n" + "\n".join(f"- {x['kind']}: {x['text']}" for x in j["activity"][-10:]))
        if j["result"]:
            out.append(f"Result: {j['result'][:1500]}")
        return ToolResult("\n".join(out))
    a, problem = ai_agents.find(agent)
    if not a:
        return ToolResult(problem, ok=False)
    reading, text = ai_agents.read_agent(a, max_chars=12000)
    if reading.state == "unknown":
        why = "it has no window I can read" if not a.hwnd else "I couldn't read its window (try a screenshot)"
        return ToolResult(f"{a.label} [{a.id}]: running {a.age()}, but {why}.")
    n = max(5, min(int(lines or 30), 80))
    tail = ai_agents._clean_lines(text)[-n:]
    out = [f'{a.label} [{a.id}] in "{a.title[:60]}": running {a.age()}, looks like it is {reading.state}.']
    if a.shared:
        out.append(
            f"Note: it shares this window with {', '.join(a.shared)}. I can only read the tab on screen, so this may be them."
        )
    if reading.prompt:
        out.append("It is asking:\n" + reading.prompt)
    if reading.problem:
        out.append("Error: " + reading.problem)
    out.append("Latest output:\n" + "\n".join(tail)[-3500:])
    return ToolResult("\n".join(out))


def _confirm_control(args: dict) -> str | None:
    action = str(args.get("action", ""))
    a, _ = ai_agents.find(str(args.get("agent", "")))
    label = a.label if a else str(args.get("agent", "that tool"))
    if action == "approve":
        snippet = ""
        if a and a.hwnd:
            snippet = ai_agents.read_agent(a)[0].prompt[-400:]
        return HardConfirm(f"Approve this in {label}?\n{snippet or '(I could not read what it is asking.)'}")
    if action == "send":
        return HardConfirm(f"Type this into {label} and press Enter?\n{str(args.get('text', ''))[:400]}")
    if action == "interrupt":
        return f"Interrupt {label}? It will stop what it is doing."
    return None  # focus and deny change nothing important


def _bring_to_front(hwnd: int) -> bool:
    from .. import winapi

    winapi.focus(hwnd)
    for _ in range(10):
        if ai_agents._foreground_hwnd() == hwnd:
            return True
        time.sleep(0.1)
    return False


@tool(
    "ai_agent_control",
    "Act on another AI tool: focus its window, interrupt it, approve or deny the prompt it is stopped on, or "
    "send it a line of text. Approving and sending ALWAYS ask the user first. It refuses unless that window is "
    "really in front and (for approve/deny) really asking. Never use type_text to type into another AI "
    "tool; use this.",
    {
        "agent": {"type": "string", "description": "id from ai_agents_overview, a name, or part of its window title"},
        "action": {"type": "string", "enum": list(_ACTIONS)},
        "text": {"type": "string", "description": "For action=send: the one line to type"},
    },
    ["agent", "action"],
    category="agents",
    ui_thread=True,
    confirm=_confirm_control,
    mutates=True,
)
def ai_agent_control(agent: str, action: str, text: str = "") -> ToolResult:
    if action not in _ACTIONS:
        return ToolResult(f"Unknown action '{action}'. Use one of: {', '.join(_ACTIONS)}.", ok=False)
    a, problem = ai_agents.find(agent)
    if not a:
        return ToolResult(problem, ok=False)
    if not a.hwnd:
        return ToolResult(f"{a.label} has no window I can reach, so I can't do that.", ok=False)
    if a.shared and action != "focus":
        return ToolResult(
            f"{a.label} shares one window with {', '.join(a.shared)}, so I can't tell which one I'd be {('typing into' if action in ('send', 'approve', 'deny') else 'stopping')}. "
            "Switch to the tab you mean and ask me again.",
            ok=False,
        )
    line = _CONTROL.sub(" ", text).strip()[:500]
    if action == "send" and not line:
        return ToolResult("There's nothing to send.", ok=False)

    if not _bring_to_front(a.hwnd):
        return ToolResult(f"I couldn't bring {a.label} to the front, so I sent nothing.", ok=False)
    time.sleep(0.15)
    if action == "focus":
        return ToolResult(f"{a.label} is in front.")

    if action in ("approve", "deny"):
        reading, _ = ai_agents.read_agent(a)
        if reading.state != "needs you":
            return ToolResult(
                f"{a.label} isn't asking anything right now (it looks {reading.state}), so I sent nothing.", ok=False
            )
        if action == "approve":
            if reading.approve_with == "enter":
                actuator.hotkey(["enter"])
            elif reading.approve_with == "y":
                actuator.type_text("y")
                actuator.hotkey(["enter"])
            else:
                return ToolResult(
                    f"{a.label} is asking something I can't safely answer for you. Here it is:\n{reading.prompt}",
                    ok=False,
                )
        elif reading.approve_with == "y":
            actuator.type_text("n")
            actuator.hotkey(["enter"])
        else:
            actuator.hotkey(["esc"])
        verb = "Approved" if action == "approve" else "Declined"
    elif action == "interrupt":
        actuator.hotkey(["esc"] if a.kind.tui else ["ctrl", "c"])
        verb = "Interrupted"
    else:  # send
        actuator.type_text(line)
        actuator.hotkey(["enter"])
        verb = "Sent your line to"

    time.sleep(0.5)
    after, _ = ai_agents.read_agent(a)
    return ToolResult(f"{verb} {a.label}. It now looks {after.state}.")
