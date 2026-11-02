"""
Other AI tools on this PC: find them, read what each one is doing, and say whether it needs you.

Claude Code, Codex, Gemini CLI and Aider in a terminal; Cursor, Windsurf, Antigravity; the Claude, ChatGPT
and local-model desktop apps; AI chats open in a browser. Jarvis finds them from running processes and
open windows (nothing is installed in them or sent anywhere), reads the visible text of their window with
UI Automation, and turns the last lines into a plain state:

    needs you   it is stopped on a question or an approval prompt
    error       it printed an error, hit a rate limit or lost its key
    working     it is mid-task (spinner, "esc to interrupt", streaming)
    idle        it is sitting at its prompt, finished or waiting for the next instruction
    unknown     its window couldn't be read

The state is a best guess from text, so every answer says "looks like". Reading is always safe. ACTING on
another agent (approve, send it text, interrupt it) lives in tools/ai_agents_tools.py and always asks.

Everything that touches the operating system goes through four small seams (`_processes`, `_windows`,
`_read_window`, `_foreground`) so the logic is tested without Windows.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from . import winapi
from .platform_utils import IS_WINDOWS

logger = logging.getLogger(__name__)

TAIL_LINES = 14  # how much of the end of the screen decides the state


# ------------------------------------------------------------------ what we look for


@dataclass(frozen=True)
class Kind:
    id: str
    label: str
    exe: tuple[str, ...] = ()  # lower-case process names
    cmd: tuple[str, ...] = ()  # regexes on the full command line
    title: tuple[str, ...] = ()  # regexes on a window title (for things that only exist as a tab or window)
    own_window: bool | None = None  # True: must have its own window; False: must NOT (a CLI); None: either
    tui: bool = False  # full-screen terminal agent: Esc interrupts it
    chat: bool = False  # a chat, not something that carries out tasks


KINDS: tuple[Kind, ...] = (
    Kind(
        "claude-code",
        "Claude Code",
        exe=("claude.exe", "claude"),
        cmd=(r"@anthropic-ai[\\/]claude-code", r"claude-code[\\/]", r"[\\/]claude(?:\.exe|\.cmd)?\"?(?:\s|$)"),
        own_window=False,
        tui=True,
    ),
    Kind("claude-desktop", "Claude (desktop app)", exe=("claude.exe",), own_window=True, chat=True),
    Kind("codex", "Codex CLI", exe=("codex.exe", "codex"), cmd=(r"@openai[\\/]codex", r"[\\/]codex(?:\.exe|\.cmd)?\"?(?:\s|$)"), tui=True),
    Kind("gemini-cli", "Gemini CLI", exe=("gemini.exe",), cmd=(r"@google[\\/]gemini-cli", r"[\\/]gemini(?:\.exe|\.cmd)?\"?(?:\s|$)"), tui=True),
    Kind("opencode", "OpenCode", exe=("opencode.exe", "opencode"), cmd=(r"[\\/]opencode(?:\.exe|\.cmd)?\"?(?:\s|$)",), tui=True),
    Kind("aider", "Aider", cmd=(r"[\\/]aider(?:\.exe)?\"?(?:\s|$)", r"\baider\b.*(?:--model|--message|\.py)", r"-m\s+aider\b")),
    Kind("goose", "Goose", exe=("goose.exe", "goose")),
    Kind("cursor", "Cursor", exe=("cursor.exe",), own_window=True),
    Kind("windsurf", "Windsurf", exe=("windsurf.exe",), own_window=True),
    Kind("antigravity", "Antigravity", exe=("antigravity.exe",), own_window=True),
    Kind("chatgpt", "ChatGPT (desktop app)", exe=("chatgpt.exe",), own_window=True, chat=True),
    Kind("lmstudio", "LM Studio", exe=("lm studio.exe", "lm-studio.exe"), own_window=True, chat=True),
    Kind("jan", "Jan", exe=("jan.exe",), own_window=True, chat=True),
    Kind("gpt4all", "GPT4All", exe=("gpt4all.exe", "chat.exe"), own_window=True, chat=True),
    Kind("ollama", "Ollama (local models)", exe=("ollama.exe", "ollama app.exe", "ollama"), chat=True),
    Kind(
        "browser-chat",
        "AI chat in a browser",
        title=(r"\b(ChatGPT|Claude|Gemini|Copilot|Perplexity|Grok|DeepSeek|Mistral|Le Chat)\b.*\b(Chrome|Edge|Firefox|Brave|Opera|Vivaldi)\b",),
        chat=True,
    ),
)  # fmt: skip

_TERMINAL_HOSTS = {"windowsterminal.exe", "conhost.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "wezterm-gui.exe", "alacritty.exe", "mintty.exe", "code.exe", "cursor.exe", "windsurf.exe"}  # fmt: skip


# ------------------------------------------------------------------ seams (replaced in tests)


@dataclass
class Proc:
    pid: int
    ppid: int
    name: str  # lower-case
    cmdline: str
    started: float
    cpu: float = 0.0
    mem_mb: float = 0.0


def _processes() -> list[Proc]:
    """Every running process, with CPU measured over a short sample."""
    try:
        import psutil
    except Exception:
        return []
    procs = []
    handles = []
    for p in psutil.process_iter(["pid", "ppid", "name", "cmdline", "create_time"]):
        try:
            handles.append(p)
            p.cpu_percent(None)  # primes the counter
        except Exception:
            continue
    time.sleep(0.25)
    for p in handles:
        try:
            info = p.info
            procs.append(
                Proc(
                    pid=info["pid"],
                    ppid=info.get("ppid") or 0,
                    name=(info.get("name") or "").lower(),
                    cmdline=" ".join(info.get("cmdline") or []),
                    started=info.get("create_time") or 0.0,
                    cpu=p.cpu_percent(None),
                    mem_mb=p.memory_info().rss / (1024 * 1024),
                )
            )
        except Exception:
            continue
    return procs


def _windows() -> list[winapi.WindowInfo]:
    return winapi.top_windows() if IS_WINDOWS else []


def _read_window(hwnd: int, max_chars: int = 8000) -> str:
    from . import headsup

    return headsup.read_window_text(hwnd, max_chars=max_chars)


def _foreground_hwnd() -> int:
    fg = winapi.foreground() if IS_WINDOWS else None
    return fg.hwnd if fg else 0


# ------------------------------------------------------------------ finding them


@dataclass
class Found:
    id: str  # stable for this session: "<kind>:<pid>"
    kind: Kind
    pid: int
    started: float
    cpu: float = 0.0
    mem_mb: float = 0.0
    hwnd: int = 0
    title: str = ""
    shared: list[str] = field(default_factory=list)  # labels of other tools in the SAME window (terminal tabs)

    @property
    def label(self) -> str:
        return self.kind.label

    def age(self, now: float | None = None) -> str:
        secs = max(0, (now or time.time()) - self.started)
        if secs < 90:
            return f"{int(secs)}s"
        if secs < 5400:
            return f"{int(secs / 60)}m"
        return f"{secs / 3600:.1f}h"


def _ancestor_window(
    proc: Proc, by_pid: dict[int, Proc], windows_by_pid: dict[int, winapi.WindowInfo]
) -> winapi.WindowInfo | None:
    """The window a command-line tool is running in: the nearest ancestor process that owns one."""
    seen = set()
    cur: Proc | None = proc
    while cur and cur.pid not in seen:
        seen.add(cur.pid)
        if cur.pid in windows_by_pid:
            return windows_by_pid[cur.pid]
        cur = by_pid.get(cur.ppid)
    return None


def scan(procs: list[Proc] | None = None, windows: list[winapi.WindowInfo] | None = None) -> list[Found]:
    """Every AI tool running right now."""
    procs = _processes() if procs is None else procs
    windows = _windows() if windows is None else windows
    by_pid = {p.pid: p for p in procs}
    win_by_pid: dict[int, winapi.WindowInfo] = {}
    for w in windows:
        win_by_pid.setdefault(w.pid, w)
    found: dict[tuple[str, int], Found] = {}

    for kind in KINDS:
        if not (kind.exe or kind.cmd):
            continue
        cmd_res = [re.compile(c, re.I) for c in kind.cmd]
        matches = [p for p in procs if p.name in kind.exe or any(r.search(p.cmdline) for r in cmd_res)]
        same = {p.pid for p in matches}
        for p in matches:
            if p.ppid in same:  # Electron and friends run many helper processes; list the main one only
                continue
            has_window = p.pid in win_by_pid
            if kind.own_window is True and not has_window:
                continue
            if kind.own_window is False and has_window:
                continue
            win = win_by_pid.get(p.pid) or _ancestor_window(p, by_pid, win_by_pid)
            # The terminal that hosts a CLI agent is itself named like one of the hosts; never treat the host as the agent.
            if kind.own_window is not True and win and win.pid == p.pid and p.name in _TERMINAL_HOSTS:
                continue
            found[(kind.id, p.pid)] = Found(
                id=f"{kind.id}:{p.pid}",
                kind=kind,
                pid=p.pid,
                started=p.started,
                cpu=p.cpu,
                mem_mb=p.mem_mb,
                hwnd=win.hwnd if win else 0,
                title=win.title if win else "",
            )

    for kind in KINDS:
        for pattern in kind.title:
            rx = re.compile(pattern, re.I)
            for w in windows:
                if (
                    rx.search(w.title)
                    and (kind.id, w.pid) not in found
                    and not any(f.hwnd == w.hwnd for f in found.values())
                ):
                    p = by_pid.get(w.pid)
                    found[(kind.id, w.hwnd)] = Found(
                        id=f"{kind.id}:{w.hwnd}",
                        kind=kind,
                        pid=w.pid,
                        started=p.started if p else time.time(),
                        cpu=p.cpu if p else 0.0,
                        mem_mb=p.mem_mb if p else 0.0,
                        hwnd=w.hwnd,
                        title=w.title,
                    )
    # Two agents in tabs of one terminal window can't be told apart: only the tab on screen is readable.
    by_window: dict[int, list[Found]] = {}
    for f in found.values():
        if f.hwnd:
            by_window.setdefault(f.hwnd, []).append(f)
    for group in by_window.values():
        if len(group) > 1:
            for f in group:
                f.shared = [g.label for g in group if g is not f]
    return sorted(found.values(), key=lambda f: (f.kind.chat, f.kind.label, f.pid))


def find(query: str, agents: list[Found] | None = None) -> tuple[Found | None, str]:
    """Pick one agent by id, kind, name or part of its window title. Returns (agent, problem)."""
    agents = scan() if agents is None else agents
    q = (query or "").strip().lower()
    if not agents:
        return None, "I don't see any AI tools running."
    if not q:
        return (
            (agents[0], "")
            if len(agents) == 1
            else (None, "Which one? " + ", ".join(f"{a.label} ({a.id})" for a in agents))
        )
    for test in (
        lambda a: a.id.lower() == q,
        lambda a: a.kind.id == q,
        lambda a: a.label.lower() == q,
        lambda a: q in a.label.lower() or q in a.kind.id,
        lambda a: bool(a.title) and q in a.title.lower(),
    ):
        hits = [a for a in agents if test(a)]
        if len(hits) == 1:
            return hits[0], ""
        if len(hits) > 1:
            return None, "More than one matches: " + ", ".join(f"{a.label} ({a.id}, '{a.title[:40]}')" for a in hits)
    return None, f"No AI tool matches '{query}'. Running: " + ", ".join(f"{a.label} ({a.id})" for a in agents)


# ------------------------------------------------------------------ reading what it is doing


@dataclass
class Reading:
    state: str  # needs you | error | working | idle | unknown
    last_lines: list[str]
    prompt: str = ""  # what it is asking, when state is "needs you"
    problem: str = ""  # the error line, when state is "error"
    approve_with: str = ""  # "enter" | "y" | "" (can't tell): how a plain "yes" would be given


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ASKING = [
    re.compile(r"do you want to (?:proceed|continue|allow|make this edit|create|run|apply|overwrite)", re.I),
    re.compile(r"\((?:y/n|yes/no)\)|\[(?:y/n|Y/n|y/N|yes/no)\]", re.I),
    re.compile(r"\b(?:approve|allow) (?:this|the) (?:command|action|edit|tool|request|change)", re.I),
    re.compile(r"\bwaiting for (?:your )?(?:input|approval|confirmation|response|permission)", re.I),
    re.compile(r"press enter to (?:continue|confirm|proceed)", re.I),
    re.compile(r"^\s*[❯>›]?\s*1\.\s*yes\b", re.I),
    re.compile(r"\bgrant (?:permission|access)\b", re.I),
]
_ERROR = re.compile(
    r"\b(?:traceback \(most recent|unhandled exception|fatal error|rate.?limit(?:ed)?|quota exceeded|"
    r"(?:invalid|missing|expired) api key|unauthori[sz]ed|authentication failed|overloaded|"
    r"error:|exception:|failed to|connection (?:refused|reset|error)|http 4\d\d|http 5\d\d|\b429\b)",
    re.I,
)
_WORKING = [
    re.compile(r"esc to interrupt|ctrl\+c to (?:interrupt|stop|cancel)|press esc to (?:cancel|stop)", re.I),
    re.compile(
        r"\b(?:thinking|working|running|generating|analy[sz]ing|reading|searching|editing|writing|building|installing|compiling|planning)\b[^.\n]{0,25}(?:\.{2,}|…|\(\d+s)",
        re.I,
    ),
    re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏✻✽✶✳·∗]\s*\w+(?:ing|…)", re.I),
    re.compile(r"\b\d+(?:\.\d+)?k? tokens\b.*\b(?:esc|interrupt)", re.I),
]
_IDLE = re.compile(
    r"^\s*(?:[>❯›$]|PS [A-Z]:\\[^>]*>|[A-Z]:\\[^>]*>)\s*$|\bwhat would you like|\bhow can i help|\bask me anything|\btype your (?:message|prompt)|\? for shortcuts",
    re.I,
)


def _clean_lines(text: str) -> list[str]:
    text = _ANSI.sub("", text or "")
    return [ln.rstrip() for ln in text.splitlines() if ln.strip()]


def analyze(text: str) -> Reading:
    """Turn the visible text of an agent's window into a state. Pure function; the heart of the feature."""
    lines = _clean_lines(text)
    if not lines:
        return Reading("unknown", [])
    tail = lines[-TAIL_LINES:]
    block = "\n".join(tail)

    # 1. Stopped on a question for you. Look from the bottom up so an old, answered prompt doesn't count.
    asking = [i for i, ln in enumerate(tail) if any(p.search(ln) for p in _ASKING)]
    if asking:
        # A prompt that is still waiting is at the bottom: nothing but its own option lines may follow it.
        start = asking[0] if len(tail) - asking[-1] <= 8 else None
        if start is not None:
            window = tail[max(0, start - 3) :]
            prompt = "\n".join(window[-8:])
            has_numbered_yes = any(re.match(r"^\s*[❯>›]?\s*1\.\s*yes\s*$", ln, re.I) for ln in window)
            yn = bool(re.search(r"\((?:y/n|yes/no)\)|\[(?:y/n|Y/n|y/N|yes/no)\]", prompt, re.I))
            return Reading(
                "needs you", tail[-6:], prompt=prompt, approve_with="enter" if has_numbered_yes else "y" if yn else ""
            )

    # 2. Mid-task.
    if any(p.search(block) for p in _WORKING):
        return Reading("working", tail[-5:])

    # 3. Resting at its prompt: the last line is a prompt, or it says so.
    if _IDLE.search(tail[-1]) or any(_IDLE.search(ln) for ln in tail[-3:]):
        err = _last_error(tail)
        return Reading("error" if err else "idle", tail[-5:], problem=err)

    # 4. An error and nothing else going on.
    err = _last_error(tail)
    if err:
        return Reading("error", tail[-5:], problem=err)

    return Reading("idle", tail[-5:])


def _last_error(lines: list[str]) -> str:
    for ln in reversed(lines):
        if _ERROR.search(ln):
            return ln.strip()[:240]
    return ""


def read_agent(a: Found, max_chars: int = 8000) -> tuple[Reading, str]:
    """(state, the raw text that decided it). Agents with no window can't be read."""
    if not a.hwnd:
        return Reading("unknown", []), ""
    try:
        text = _read_window(a.hwnd, max_chars)
    except Exception:
        logger.debug("read failed for %s", a.id, exc_info=True)
        text = ""
    return analyze(text), text


def one_line(a: Found, r: Reading) -> str:
    where = f' in "{a.title[:50]}"' if a.title else " (no window I can read)"
    if a.shared:
        state = f"shares its window with {', '.join(a.shared)}, and the tab on screen looks {r.state}"
    else:
        state = r.state
    bits = [f"[{a.id}] {a.label}{where}", f"running {a.age()}", state]
    if a.cpu >= 5:
        bits.append(f"{a.cpu:.0f}% CPU")
    line = " | ".join(bits)
    if r.state == "needs you" and r.prompt:
        line += "\n    asking: " + r.prompt.splitlines()[-1].strip()[:160]
    elif r.state == "error" and r.problem:
        line += "\n    error: " + r.problem[:160]
    elif r.last_lines and r.state in ("working", "idle"):
        line += "\n    last: " + r.last_lines[-1].strip()[:160]
    return line
