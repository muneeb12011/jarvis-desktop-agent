"""
System tools: launching and closing apps, windows, media, volume, power,
PowerShell, clipboard, and machine status. These are the "direct" routes -
far faster and more reliable than finding and clicking things on screen.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path

from ..platform_utils import IS_WINDOWS, powershell, run, startfile
from . import ToolResult, tool

# ------------------------------------------------------------------ time / status


@tool("get_datetime", "Current local date, time, weekday and timezone.", category="system")
def get_datetime() -> str:
    now = dt.datetime.now().astimezone()
    return now.strftime("%A, %d %B %Y, %I:%M %p (%Z, UTC%z)")


@tool("system_status", "CPU, memory, disk, battery, uptime and the heaviest running processes.", category="system")
def system_status() -> str:
    try:
        import psutil
    except ImportError:
        return "psutil is not installed (pip install psutil)."
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(os.path.abspath(os.sep))
    lines = [
        f"OS: {platform.system()} {platform.release()} ({platform.machine()})",
        f"CPU: {psutil.cpu_percent(interval=0.5):.0f}% across {psutil.cpu_count()} threads",
        f"Memory: {vm.percent:.0f}% used ({vm.used / 2**30:.1f} of {vm.total / 2**30:.1f} GB)",
        f"Disk {os.path.abspath(os.sep)}: {disk.percent:.0f}% used, {disk.free / 2**30:.0f} GB free",
    ]
    battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if battery:
        state = "charging" if battery.power_plugged else "on battery"
        lines.append(f"Battery: {battery.percent:.0f}% ({state})")
    uptime = time.time() - psutil.boot_time()
    lines.append(f"Uptime: {int(uptime // 3600)}h {int(uptime % 3600 // 60)}m")
    procs = []
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            procs.append((p.info["memory_info"].rss, p.info["name"]))
        except Exception:
            continue
    procs.sort(reverse=True)
    lines.append("Top memory: " + ", ".join(f"{n} ({r / 2**20:.0f} MB)" for r, n in procs[:5]))
    return "\n".join(lines)


# ------------------------------------------------------------------ apps

_ALIASES = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "paint": "mspaint.exe",
    "cmd": "cmd.exe",
    "command prompt": "cmd.exe",
    "powershell": "powershell.exe",
    "terminal": "wt.exe",
    "windows terminal": "wt.exe",
    "file explorer": "explorer.exe",
    "explorer": "explorer.exe",
    "files": "explorer.exe",
    "task manager": "taskmgr.exe",
    "settings": "ms-settings:",
    "control panel": "control.exe",
    "chrome": "chrome",
    "google chrome": "chrome",
    "edge": "msedge",
    "microsoft edge": "msedge",
    "firefox": "firefox",
    "vs code": "code",
    "vscode": "code",
    "visual studio code": "code",
    "code": "code",
    "word": "winword",
    "excel": "excel",
    "powerpoint": "powerpnt",
    "outlook": "outlook",
    "spotify": "spotify:",
    "whatsapp": "whatsapp:",
    "camera": "microsoft.windows.camera:",
    "microsoft store": "ms-windows-store:",
    "store": "ms-windows-store:",
    "clock": "ms-clock:",
    "alarms": "ms-clock:",
    "snipping tool": "ms-screenclip:",
    "calendar": "outlookcal:",
    "mail": "outlookmail:",
    "photos": "ms-photos:",
    "bluetooth": "ms-settings:bluetooth",
    "wifi": "ms-settings:network-wifi",
    "display settings": "ms-settings:display",
    "sound settings": "ms-settings:sound",
}

_start_apps_cache: list[dict] | None = None


def _start_menu_shortcuts() -> dict[str, Path]:
    roots = [
        Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path.home() / "Desktop",
    ]
    found: dict[str, Path] = {}
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.suffix.lower() in (".lnk", ".url", ".appref-ms"):
                stem = p.stem.lower()
                if "uninstall" not in stem:
                    found.setdefault(stem, p)
    return found


def _start_apps() -> list[dict]:
    """Packaged (Store/UWP) apps, which have no .lnk file."""
    global _start_apps_cache
    if _start_apps_cache is None:
        try:
            out = powershell("Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress", timeout=20)
            data = json.loads(out.stdout or "[]")
            _start_apps_cache = data if isinstance(data, list) else [data]
        except Exception:
            _start_apps_cache = []
    return _start_apps_cache


def _best_match(query: str, names: list[str]) -> str | None:
    q = query.lower().strip()
    if q in names:
        return q
    starts = [n for n in names if n.startswith(q)]
    if starts:
        return min(starts, key=len)
    contains = [n for n in names if q in n]
    if contains:
        return min(contains, key=len)
    close = difflib.get_close_matches(q, names, n=1, cutoff=0.72)
    return close[0] if close else None


@tool(
    "open_app",
    "Launch an application by name (e.g. 'notepad', 'chrome', 'spotify', 'vs code', 'settings', "
    "'whatsapp'). Searches aliases, Start Menu shortcuts and installed Store apps. ALWAYS prefer this "
    "over clicking through the Start menu.",
    {"name": {"type": "string", "description": "App name as the user said it"}},
    ["name"],
    category="apps",
    mutates=True,
)
def open_app(name: str) -> str:
    if not IS_WINDOWS:
        return "open_app currently supports Windows only."
    key = name.lower().strip()
    target = _ALIASES.get(key)
    if target:
        try:
            if ":" in target:  # URI scheme (ms-settings:, spotify:, ...)
                startfile(target)
            elif shutil.which(target):
                subprocess.Popen([shutil.which(target) or target], creationflags=0)
            else:
                startfile(target)  # ShellExecute resolves "App Paths" (chrome, winword...)
            return f"Launched {name}."
        except OSError:
            pass  # fall through to the searches below

    shortcuts = _start_menu_shortcuts()
    match = _best_match(key, list(shortcuts))
    if match:
        startfile(str(shortcuts[match]))
        return f"Launched '{shortcuts[match].stem}' from the Start Menu."

    apps = _start_apps()
    by_name = {a.get("Name", "").lower(): a for a in apps if a.get("Name")}
    match = _best_match(key, list(by_name))
    if match:
        app_id = by_name[match]["AppID"]
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"])
        return f"Launched '{by_name[match]['Name']}'."

    exe = shutil.which(key) or shutil.which(key + ".exe")
    if exe:
        subprocess.Popen([exe])
        return f"Launched {exe}."
    return (
        f"Couldn't find an app called '{name}'. It may not be installed. You can try the Start menu "
        f"via the screen tools (press_keys ['win'], type_text the name, press Enter)."
    )


def _windows():
    import pygetwindow as gw  # Windows-only dependency, imported lazily

    return [w for w in gw.getAllWindows() if w.title and w.title.strip() and w.width > 50]


def _find_window(title: str):
    t = title.lower()
    wins = _windows()
    exact = [w for w in wins if w.title.lower() == t]
    partial = [w for w in wins if t in w.title.lower()]
    return (exact or partial or [None])[0]


@tool("list_windows", "List open windows (title, size, which one is focused).", category="windows")
def list_windows() -> str:
    if not IS_WINDOWS:
        return "Window listing supports Windows only."
    import pygetwindow as gw

    active = gw.getActiveWindow()
    lines = []
    for w in _windows():
        flag = " [FOCUSED]" if active and w._hWnd == active._hWnd else ""
        state = " (minimized)" if w.isMinimized else ""
        lines.append(f"- {w.title}{state}{flag}")
    return "\n".join(lines) or "No windows open."


@tool(
    "focus_window",
    "Bring a window to the front by (partial) title.",
    {"title": {"type": "string"}},
    ["title"],
    category="windows",
    ui_thread=True,
)
def focus_window(title: str) -> str:
    if not IS_WINDOWS:
        return "Supports Windows only."
    win = _find_window(title)
    if not win:
        return f"No window matching '{title}'. Use list_windows to see what's open."
    if win.isMinimized:
        win.restore()
    try:
        win.activate()
    except Exception:
        # Windows blocks focus-stealing unless the process just got input; an
        # Alt tap satisfies that rule.
        import pyautogui

        pyautogui.press("alt")
        try:
            win.activate()
        except Exception:
            pass
    time.sleep(0.3)
    return f"Focused '{win.title}'."


def _confirm_close(args: dict) -> str | None:
    if args.get("action") == "close":
        return f"Close '{args.get('title')}'? Unsaved work in it could be lost."
    return None


@tool(
    "window_action",
    "Minimize, maximize, restore or close a window by (partial) title.",
    {"title": {"type": "string"}, "action": {"type": "string", "enum": ["minimize", "maximize", "restore", "close"]}},
    ["title", "action"],
    category="windows",
    ui_thread=True,
    confirm=_confirm_close,
    mutates=True,
)
def window_action(title: str, action: str) -> str:
    if not IS_WINDOWS:
        return "Supports Windows only."
    win = _find_window(title)
    if not win:
        return f"No window matching '{title}'."
    getattr(win, action)()
    return f"{action.capitalize()}d '{win.title}'."


@tool(
    "close_app",
    "Close an application gracefully (like clicking X) by window title or process name.",
    {"name": {"type": "string"}},
    ["name"],
    category="apps",
    confirm=lambda a: f"Close {a.get('name')}? Unsaved work could be lost.",
    mutates=True,
)
def close_app(name: str) -> str:
    if not IS_WINDOWS:
        return "Supports Windows only."
    closed = []
    for w in _windows():
        if name.lower() in w.title.lower():
            try:
                w.close()
                closed.append(w.title)
            except Exception:
                pass
    if closed:
        return "Closed: " + "; ".join(closed)
    exe = name if name.lower().endswith(".exe") else name.replace(" ", "") + ".exe"
    out = run(["taskkill", "/IM", exe], timeout=10)
    return (out.stdout or out.stderr).strip() or f"Asked {exe} to close."


# ------------------------------------------------------------------ media / volume / display

_MEDIA_KEYS = {
    "play_pause": "playpause",
    "next": "nexttrack",
    "previous": "prevtrack",
    "stop": "stop",
    "volume_up": "volumeup",
    "volume_down": "volumedown",
    "mute": "volumemute",
}


@tool(
    "media_control",
    "Media keys: play/pause, next/previous track, volume up/down, mute.",
    {
        "action": {"type": "string", "enum": list(_MEDIA_KEYS)},
        "steps": {"type": "integer", "description": "Repeat count for volume up/down (each ~2%)"},
    },
    ["action"],
    category="media",
    ui_thread=True,
)
def media_control(action: str, steps: int = 1) -> str:
    import pyautogui

    key = _MEDIA_KEYS[action]
    n = max(1, min(int(steps or 1), 50)) if action.startswith("volume_") else 1
    pyautogui.press(key, presses=n, interval=0.02)
    return f"Pressed {action} x{n}."


@tool(
    "set_volume",
    "Set the master volume to a percentage (0-100).",
    {"percent": {"type": "integer"}},
    ["percent"],
    category="media",
    ui_thread=True,
)
def set_volume(percent: int) -> str:
    percent = max(0, min(100, int(percent)))
    try:  # precise path if pycaw is installed
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL  # type: ignore
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume  # type: ignore

        speakers = AudioUtilities.GetSpeakers()
        iface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        vol = cast(iface, POINTER(IAudioEndpointVolume))
        vol.SetMasterVolumeLevelScalar(percent / 100, None)
        return f"Volume set to {percent}%."
    except Exception:
        import pyautogui

        pyautogui.press("volumedown", presses=50, interval=0.01)
        pyautogui.press("volumeup", presses=round(percent / 2), interval=0.01)
        return f"Volume set to about {percent}%."


@tool(
    "set_brightness",
    "Set screen brightness (laptop displays) to a percentage.",
    {"percent": {"type": "integer"}},
    ["percent"],
    category="display",
)
def set_brightness(percent: int) -> str:
    percent = max(0, min(100, int(percent)))
    out = powershell(
        f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,{percent})"
    )
    if out.returncode != 0:
        return "This display doesn't support software brightness control (external monitors usually don't)."
    return f"Brightness set to {percent}%."


def _confirm_power(args: dict) -> str | None:
    action = args.get("action")
    if action in ("shutdown", "restart", "sleep", "sign_out"):
        return f"{action.replace('_', ' ').capitalize()} the computer?"
    return None


@tool(
    "power",
    "Lock, sleep, restart, shut down or sign out. Shutdown/restart wait 30s so they can be "
    "cancelled with action 'cancel'.",
    {"action": {"type": "string", "enum": ["lock", "sleep", "restart", "shutdown", "sign_out", "cancel"]}},
    ["action"],
    category="system",
    confirm=_confirm_power,
    mutates=True,
)
def power(action: str) -> str:
    if not IS_WINDOWS:
        return "Supports Windows only."
    cmds = {
        "lock": ["rundll32.exe", "user32.dll,LockWorkStation"],
        "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
        "restart": ["shutdown", "/r", "/t", "30"],
        "shutdown": ["shutdown", "/s", "/t", "30"],
        "sign_out": ["shutdown", "/l"],
        "cancel": ["shutdown", "/a"],
    }
    subprocess.Popen(cmds[action])
    return {"restart": "Restarting in 30 seconds.", "shutdown": "Shutting down in 30 seconds."}.get(
        action, f"Done: {action}."
    )


# ------------------------------------------------------------------ shell

_READ_ONLY = re.compile(
    r"^\s*(get-|test-|measure-|select-|where-|sort-|format-|out-string|resolve-|find-|search-|"
    r"dir\b|ls\b|echo\b|write-output|whoami|hostname|ipconfig|systeminfo|tasklist|ping|nslookup|"
    r"\$psversiontable|\[environment\]|python --version|node --version|git status|git log)",
    re.IGNORECASE,
)
_DANGER_HINTS = re.compile(
    r"(remove-|del\b|rm\b|rmdir|format\b|stop-|kill|set-|new-|>|reg\b|"
    r"invoke-webrequest|iwr\b|curl\b|start-process|-force|install|uninstall)",
    re.IGNORECASE,
)


def _confirm_shell(args: dict) -> str | None:
    cmd = str(args.get("command", ""))
    segments = re.split(r"[;|&]", cmd)
    if all(_READ_ONLY.match(s) for s in segments if s.strip()) and not _DANGER_HINTS.search(cmd):
        return None
    return f"Run this PowerShell command?\n{cmd}"


@tool(
    "run_powershell",
    "Run a PowerShell command and return its output. Use for anything the other tools can't do "
    "(file management, network info, installed programs, git, system settings). Read-only commands run "
    "immediately; anything that changes the system asks the user first.",
    {"command": {"type": "string"}, "timeout_seconds": {"type": "integer"}},
    ["command"],
    category="shell",
    confirm=_confirm_shell,
    mutates=True,
)
def run_powershell(command: str, timeout_seconds: int = 60) -> ToolResult:
    timeout = max(5, min(int(timeout_seconds or 60), 300))
    try:
        out = powershell(command, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ToolResult(f"Command timed out after {timeout}s.", ok=False)
    except FileNotFoundError:
        return ToolResult("PowerShell isn't available on this system.", ok=False)
    text = (out.stdout or "").strip()
    err = (out.stderr or "").strip()
    body = text[:6000] or "(no output)"
    if err:
        body += f"\n[stderr]\n{err[:2000]}"
    return ToolResult(f"exit code {out.returncode}\n{body}", ok=out.returncode == 0)


# ------------------------------------------------------------------ clipboard


@tool("clipboard_read", "Read the text currently on the clipboard.", category="clipboard")
def clipboard_read() -> str:
    import pyperclip

    text = pyperclip.paste() or ""
    return text[:5000] if text else "(clipboard is empty)"


@tool(
    "clipboard_write",
    "Put text on the clipboard.",
    {"text": {"type": "string"}},
    ["text"],
    category="clipboard",
    mutates=True,
)
def clipboard_write(text: str) -> str:
    import pyperclip

    pyperclip.copy(text)
    return f"Copied {len(text)} characters to the clipboard."
