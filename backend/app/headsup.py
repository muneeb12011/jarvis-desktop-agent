"""
Heads-up: Jarvis notices things on your screen and offers one line of help,
without being asked.

    Terminal / VS Code shows a Python traceback, npm ERR!, a TypeScript error,
    a failed build, "is not recognized as a command"...
        -> "ModuleNotFoundError: requests isn't installed in this venv. Want me to fix it?"
           [Help me fix] [Dismiss] [Mute Terminal]

    A download finishes        -> "Downloaded report.pdf (2.1 MB)."   [Open] [Show in folder]
    An app freezes             -> "Chrome isn't responding."           [Force close it]

How it stays cheap and private:
- Opt-in (off until you turn it on). Nothing is stored.
- Text only: it reads the foreground window's text through UI Automation, no screenshots.
- Only developer apps are read (terminals, VS Code, Cursor, JetBrains, Visual Studio).
- Nothing leaves the PC unless an error is detected; then only the last lines of
  that error go to the model for the one-line explanation.
- An error already on screen when a window is first seen is ignored, each distinct
  error is offered once, there's a cooldown between offers, apps can be muted,
  and it stays quiet while Jarvis itself is working.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import memory, winapi
from .platform_utils import IS_WINDOWS

logger = logging.getLogger(__name__)

SCAN_SECONDS = 4
COOLDOWN_SECONDS = 60
IDLE_SKIP_SECONDS = 180
NOT_RESPONDING_SECONDS = 8

DEV_APPS = {
    "windowsterminal.exe": "Terminal",
    "cmd.exe": "Command Prompt",
    "powershell.exe": "PowerShell",
    "pwsh.exe": "PowerShell",
    "conhost.exe": "Console",
    "openconsole.exe": "Terminal",
    "code.exe": "VS Code",
    "cursor.exe": "Cursor",
    "windsurf.exe": "Windsurf",
    "devenv.exe": "Visual Studio",
    "idea64.exe": "IntelliJ IDEA",
    "pycharm64.exe": "PyCharm",
    "webstorm64.exe": "WebStorm",
    "rider64.exe": "Rider",
    "mintty.exe": "Git Bash",
}

PARTIAL_DOWNLOAD = (".crdownload", ".part", ".partial", ".download", ".tmp", ".opdownload")

# ------------------------------------------------------------------ settings


def enabled() -> bool:
    return memory.kv_get("headsup_enabled", "0") == "1"


def set_enabled(on: bool) -> None:
    memory.kv_set("headsup_enabled", "1" if on else "0")


def muted() -> set[str]:
    try:
        return set(json.loads(memory.kv_get("headsup_muted", "[]") or "[]"))
    except json.JSONDecodeError:
        return set()


def set_muted(app: str, mute: bool) -> set[str]:
    key = _app_key(app)
    items = muted()
    if mute:
        items.add(key)
    else:
        items.discard(key)
    memory.kv_set("headsup_muted", json.dumps(sorted(items)))
    return items


def _app_key(app: str) -> str:
    """'VS Code', 'code', 'Code.exe' -> 'code.exe'."""
    low = app.strip().lower()
    for exe, friendly in DEV_APPS.items():
        if low in (exe, exe.removesuffix(".exe"), friendly.lower()):
            return exe
    if low in ("downloads", "download", "downloads folder"):
        return "downloads"
    return low if low.endswith(".exe") or not low else low + ".exe"


def status() -> str:
    m = sorted(muted())
    return (
        f"Heads-up is {'on' if enabled() else 'off'}."
        + (f" Muted: {', '.join(m)}." if m else "")
        + " It watches terminals and code editors for errors, finished downloads, and frozen apps."
    )


# ------------------------------------------------------------------ error detection

# Strong patterns only: they must not fire on ordinary source code sitting in an editor.
_ERROR_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("python", re.compile(r"^Traceback \(most recent call last\):", re.M)),
    ("npm", re.compile(r"^npm (ERR!|error) ", re.M)),
    ("typescript", re.compile(r"error TS\d{4}:")),
    ("rust", re.compile(r"^error(\[E\d{4}\])?: .+", re.M)),
    ("go", re.compile(r"^panic: .+", re.M)),
    ("java", re.compile(r"^Exception in thread \".+\" [\w.$]+", re.M)),
    ("node", re.compile(r"^(Uncaught )?\w*(Error|Exception) \[?\w*\]?:? .+\n\s+at .+", re.M)),
    ("node", re.compile(r"^node:internal/.+\n", re.M)),
    ("shell", re.compile(r"is not recognized as (an internal or external command|the name of a cmdlet)", re.I)),
    ("shell", re.compile(r"^.{0,80}: command not found$", re.M)),
    ("build", re.compile(r"^(BUILD FAILED|FAILURE: Build failed|Build FAILED\.|error: build failed)", re.M | re.I)),
    ("build", re.compile(r"^\s*(ELIFECYCLE|ERR_PNPM_\w+)", re.M)),
    ("test", re.compile(r"^(=+ .*\b\d+ failed\b.* =+|FAILED \S+::\S+|Tests?:\s+\d+ failed)", re.M)),
    ("git", re.compile(r"^(fatal|error): .+", re.M)),
    ("process", re.compile(r"exited with (exit )?code [1-9]\d*", re.I)),
    ("python", re.compile(r"^\w+(Error|Exception): .+", re.M)),
]

_FINAL_LINE = re.compile(
    r"^(?:\w+(?:Error|Exception|Exit)|error(?:\[E\d+\])?|fatal|panic|npm (?:ERR!|error)|FAILED|ERR_\w+)(?!\w).*$",
    re.M,
)
_NOISE = re.compile(r"(0x[0-9a-f]+|\d+(\.\d+)?(ms|s)\b|line \d+|:\d+:\d+|\b\d{3,}\b)", re.I)


@dataclass
class Detected:
    kind: str
    signature: str  # the one line that names the error
    context: str  # the last lines leading to it, for the model


def detect_error(text: str, tail_lines: int = 60) -> Detected | None:
    """Look only at the end of the window's text: that's what just happened."""
    if not text:
        return None
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    tail = "\n".join(lines[-tail_lines:])
    for kind, pat in _ERROR_PATTERNS:
        m = None
        for m in pat.finditer(tail):  # noqa: B007 - keep the last match
            pass
        if not m:
            continue
        after = tail[m.start() :]
        sig_match = None
        for sig_match in _FINAL_LINE.finditer(after):  # noqa: B007
            pass
        signature = (sig_match.group(0) if sig_match else m.group(0)).strip()
        start = max(0, tail.rfind("\n", 0, max(0, m.start() - 1500)))
        context = tail[start:].strip()[-3000:]
        return Detected(kind, signature[:240], context)
    return None


def fingerprint(app: str, signature: str) -> str:
    norm = _NOISE.sub("#", signature.lower())
    norm = re.sub(r"[a-z]:\\[^\s'\"]+|/[\w./-]+", "<path>", norm)
    return hashlib.sha1(f"{app}|{norm}".encode()).hexdigest()[:16]


# ------------------------------------------------------------------ reading the screen


def read_window_text(hwnd: int, budget: float = 1.2, max_chars: int = 8000) -> str:
    """Text of a window via UI Automation: terminal buffers and documents via the Text pattern,
    plus short error-looking labels (editor notifications, problem lists)."""
    if not IS_WINDOWS or not hwnd:
        return ""
    try:
        from pywinauto.uia_defines import get_elem_interface
        from pywinauto.uia_element_info import UIAElementInfo
    except Exception:
        return ""
    deadline = time.monotonic() + budget
    chunks: list[str] = []
    labels: list[str] = []
    try:
        root = UIAElementInfo(hwnd)  # positional: the kwarg is handle_or_elem
        walker = getattr(root, "iter_descendants", None)
        nodes = walker() if walker else root.descendants()
        for info in nodes:
            if time.monotonic() > deadline:
                break
            try:
                ctype = info.control_type
            except Exception:
                continue
            if ctype in ("Document", "Edit", "Text", "Pane", "Custom", "List"):
                try:
                    iface = get_elem_interface(info.element, "Text")
                    txt = iface.DocumentRange.GetText(-1) if iface else ""
                except Exception:
                    txt = ""
                if txt and len(txt.strip()) > 10:
                    chunks.append(txt[-max_chars:])
                    continue
            if ctype in ("Text", "ListItem", "Group") and len(labels) < 40:
                name = (info.name or "").strip()
                if 8 < len(name) < 400 and re.search(r"error|exception|failed|not recognized", name, re.I):
                    labels.append(name)
    except Exception:
        logger.debug("heads-up read failed", exc_info=True)
    # The longest buffer is the terminal/editor; labels go last so they count as "latest".
    chunks.sort(key=len)
    text = "\n".join(chunks[-2:] + labels)
    return text[-max_chars * 2 :]


# ------------------------------------------------------------------ the watcher


@dataclass
class Suggestion:
    id: str
    kind: str  # error | download | frozen
    app: str
    title: str
    text: str
    actions: list[dict] = field(default_factory=list)  # [{"label": ..., "prompt": ...}]
    detail: str = ""

    def payload(self) -> dict:
        return {
            "type": "headsup",
            "id": self.id,
            "kind": self.kind,
            "app": self.app,
            "app_label": DEV_APPS.get(self.app, self.app.removesuffix(".exe")),
            "title": self.title,
            "text": self.text,
            "actions": self.actions,
            "mute_key": _app_key(self.app) if self.kind != "download" else "downloads",
        }


EXPLAIN_PROMPT = """A developer's terminal or editor just showed this error. In ONE sentence of at most 25 words, \
say what went wrong and the most likely fix, plainly. No preamble, no markdown, no code blocks."""


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n} B"


class Watcher:
    def __init__(
        self,
        emit: Callable[[Suggestion], Awaitable[None]],
        busy: Callable[[], bool] = lambda: False,
        explain: Callable[[Detected], str | None] | None = None,
        downloads_dir: Path | None = None,
        reader: Callable[[int], str] = read_window_text,
        foreground: Callable[[], winapi.WindowInfo | None] = winapi.foreground,
        idle: Callable[[], float] = winapi.idle_seconds,
    ) -> None:
        self.emit = emit
        self.busy = busy
        self.explain = explain
        self.downloads = downloads_dir if downloads_dir is not None else Path.home() / "Downloads"
        self.reader = reader
        self.foreground = foreground
        self.idle = idle
        self.seen: dict[str, float] = {}  # fingerprint -> when
        self.baselined: set[int] = set()  # hwnds already looked at once
        self.last_offer = float("-inf")
        self.frozen_since: dict[int, float] = {}
        self.dl_known: dict[str, int] | None = None
        self.dl_pending: dict[str, int] = {}
        self.recent: list[Suggestion] = []

    # ---- error / frozen: one foreground scan
    def scan_foreground(self, now: float | None = None) -> Suggestion | None:
        now = time.time() if now is None else now
        fg = self.foreground()
        if not fg or not fg.name:
            return None
        exe = fg.name.lower()
        if exe in muted():
            return None

        if fg.title.endswith("(Not Responding)"):
            since = self.frozen_since.setdefault(fg.hwnd, now)
            if now - since >= NOT_RESPONDING_SECONDS:
                fp = fingerprint(exe, "not responding")
                if self._fresh(fp, now):
                    name = fg.title.removesuffix("(Not Responding)").strip(" -")
                    return Suggestion(
                        fp,
                        "frozen",
                        exe,
                        fg.title,
                        f"{name or fg.name} isn't responding.",
                        [
                            {"label": "Wait", "prompt": ""},
                            {"label": "Force close it", "prompt": f"Force close {fg.name} (it is not responding)."},
                        ],
                    )
            return None
        self.frozen_since.pop(fg.hwnd, None)

        if exe not in DEV_APPS:
            return None
        text = self.reader(fg.hwnd)
        found = detect_error(text)
        first_look = fg.hwnd not in self.baselined
        self.baselined.add(fg.hwnd)
        if not found:
            return None
        fp = fingerprint(exe, found.signature)
        if first_look:  # errors already on screen when we first looked are old news
            self.seen[fp] = now
            return None
        if not self._fresh(fp, now):
            return None
        app = DEV_APPS[exe]
        prompt = (
            f"Help me fix this error that just appeared in {app} (window: {fg.title[:120]}).\n"
            f"```\n{found.context[-3000:]}\n```"
        )
        return Suggestion(
            fp,
            "error",
            exe,
            fg.title,
            found.signature,
            [{"label": "Help me fix", "prompt": prompt}],
            detail=found.context,
        )

    def enrich(self, s: Suggestion) -> Suggestion:
        """Swap the raw error line for a one-sentence explanation (a model call, so off the GUI thread)."""
        if s.kind != "error" or not self.explain:
            return s
        try:
            line = self.explain(Detected("error", s.text, s.detail))
        except Exception:
            logger.debug("heads-up explain failed", exc_info=True)
            line = None
        if line:
            s.text = line.strip().split("\n")[0][:240]
        return s

    def _fresh(self, fp: str, now: float) -> bool:
        if fp in self.seen and now - self.seen[fp] < 6 * 3600:
            return False
        if now - self.last_offer < COOLDOWN_SECONDS:
            return False
        self.seen[fp] = now
        self.last_offer = now
        return True

    # ---- downloads: a new, finished file whose size held still for one scan
    def scan_downloads(self, now: float | None = None) -> Suggestion | None:
        now = time.time() if now is None else now
        if "downloads" in muted() or not self.downloads.is_dir():
            return None
        current: dict[str, int] = {}
        try:
            for p in self.downloads.iterdir():
                if p.is_file() and not p.name.lower().endswith(PARTIAL_DOWNLOAD) and not p.name.startswith("~$"):
                    current[p.name] = p.stat().st_size
        except OSError:
            return None
        if self.dl_known is None:
            self.dl_known = current
            return None
        out = None
        for name, size in current.items():
            if name in self.dl_known:
                continue
            if self.dl_pending.get(name) == size and size > 0:
                self.dl_known[name] = size
                self.dl_pending.pop(name, None)
                if out is None:
                    path = self.downloads / name
                    out = Suggestion(
                        fingerprint("downloads", name + str(now)),
                        "download",
                        "Downloads",
                        name,
                        f"Downloaded {name} ({_fmt_size(size)}).",
                        [
                            {"label": "Open", "prompt": f'Open the file "{path}".'},
                            {"label": "Show in folder", "prompt": f'Open the Downloads folder and select "{name}".'},
                        ],
                    )
            else:
                self.dl_pending[name] = size
        for gone in set(self.dl_known) - set(current):
            self.dl_known.pop(gone, None)
        return out

    async def run(self) -> None:
        from .agent import UI_POOL

        loop = asyncio.get_running_loop()
        tick = 0
        while True:
            await asyncio.sleep(SCAN_SECONDS)
            tick += 1
            try:
                if not enabled():
                    self.baselined.clear()
                    self.dl_known = None
                    continue
                found: Suggestion | None = None
                if not self.busy() and self.idle() < IDLE_SKIP_SECONDS:
                    # UI Automation shares the single GUI thread with Jarvis's own clicks.
                    found = await loop.run_in_executor(UI_POOL, self.scan_foreground)
                if found is None and tick % 2 == 0:
                    found = await asyncio.to_thread(self.scan_downloads)
                if found:
                    found = await asyncio.to_thread(self.enrich, found)
                    self.recent = (self.recent + [found])[-10:]
                    logger.info("Heads-up (%s, %s): %s", found.kind, found.app, found.text)
                    await self.emit(found)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("heads-up tick failed", exc_info=True)


WATCHER: Watcher | None = None


def make_explainer(brain) -> Callable[[Detected], str | None]:
    def explain(d: Detected) -> str | None:
        turn = brain.complete(EXPLAIN_PROMPT, [{"role": "user", "text": d.context[-2500:]}], [])
        return (turn.text or "").strip() or None

    return explain
