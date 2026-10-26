"""
Thin ctypes wrappers over the Win32 calls Jarvis needs for activity tracking,
workspaces and text actions. No pywin32 dependency; every function degrades
to a harmless default on non-Windows so tests run anywhere.
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass

from .platform_utils import IS_WINDOWS

if IS_WINDOWS:
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    dwmapi = ctypes.windll.dwmapi


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    exe: str  # full path ("" if access denied)
    name: str  # process name, e.g. Code.exe
    rect: tuple[int, int, int, int]
    maximized: bool
    minimized: bool


def _title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _proc(pid: int) -> tuple[str, str]:
    try:
        import psutil

        p = psutil.Process(pid)
        name = p.name()
        try:
            exe = p.exe()
        except Exception:
            exe = ""
        return exe, name
    except Exception:
        return "", ""


def _cloaked(hwnd: int) -> bool:
    val = ctypes.c_int(0)
    try:
        dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(val), ctypes.sizeof(val))  # DWMWA_CLOAKED
    except Exception:
        return False
    return val.value != 0


def window_info(hwnd: int) -> WindowInfo:
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    pid = _pid(hwnd)
    exe, name = _proc(pid)
    return WindowInfo(
        hwnd,
        _title(hwnd),
        pid,
        exe,
        name,
        (rect.left, rect.top, rect.right, rect.bottom),
        bool(user32.IsZoomed(hwnd)),
        bool(user32.IsIconic(hwnd)),
    )


def foreground() -> WindowInfo | None:
    if not IS_WINDOWS:
        return None
    hwnd = user32.GetForegroundWindow()
    return window_info(hwnd) if hwnd else None


_SKIP_PROCS = {
    "textinputhost.exe",
    "searchhost.exe",
    "startmenuexperiencehost.exe",
    "shellexperiencehost.exe",
    "lockapp.exe",
    "applicationframehost.exe",
}


def top_windows() -> list[WindowInfo]:
    """Visible, real application windows (no tool windows, cloaked UWP shells, or Jarvis itself)."""
    if not IS_WINDOWS:
        return []
    out: list[WindowInfo] = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # GW_OWNER: skip owned popups
            return True
        if user32.GetWindowTextLengthW(hwnd) == 0 or _cloaked(hwnd):
            return True
        ex_style = user32.GetWindowLongW(hwnd, -20)
        if ex_style & 0x80:  # WS_EX_TOOLWINDOW
            return True
        info = window_info(hwnd)
        low = info.name.lower()
        if low in _SKIP_PROCS or info.title == "Program Manager":
            return True
        if low == "electron.exe" and "screen-agent" in info.exe.lower():
            return True
        out.append(info)
        return True

    user32.EnumWindows(proto(cb), 0)
    return out


def idle_seconds() -> float:
    if not IS_WINDOWS:
        return 0.0

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    return max(0.0, (kernel32.GetTickCount() - lii.dwTime) / 1000.0)


def focus(hwnd: int) -> bool:
    """Bring a window to the front despite Windows' focus-stealing rules."""
    if not IS_WINDOWS:
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # Pressing and releasing Alt marks this process as having had input, which unlocks SetForegroundWindow.
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    ok = bool(user32.SetForegroundWindow(hwnd))
    time.sleep(0.12)
    return ok


def place(hwnd: int, rect: tuple[int, int, int, int], maximized: bool) -> None:
    if not IS_WINDOWS:
        return
    user32.ShowWindow(hwnd, 9)  # restore first so the move applies
    left, top, right, bottom = rect
    user32.MoveWindow(hwnd, left, top, right - left, bottom - top, True)
    if maximized:
        user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
