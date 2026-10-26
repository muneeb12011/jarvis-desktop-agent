"""
OS helpers. Jarvis targets Windows, but every module stays importable on
Linux/macOS so tests and CI run anywhere.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW: no console flash


def enable_dpi_awareness() -> None:
    """Without this, on a 125%/150% scaled display, screenshots, UI
    Automation rectangles and mouse coordinates disagree and every click
    lands off-target. Must run before mss/pyautogui touch the screen."""
    if not IS_WINDOWS:
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def run(cmd: list[str] | str, timeout: int = 30, shell: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=shell,
        creationflags=NO_WINDOW,
        encoding="utf-8",
        errors="replace",
    )


def powershell(command: str, timeout: int = 60) -> subprocess.CompletedProcess:
    exe = "powershell" if IS_WINDOWS else "pwsh"
    return run(
        [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command], timeout=timeout
    )


def startfile(target: str) -> None:
    if IS_WINDOWS:
        os.startfile(target)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target])


_KNOWN_FOLDERS = {
    "desktop": "Desktop",
    "downloads": "Downloads",
    "documents": "Documents",
    "docs": "Documents",
    "pictures": "Pictures",
    "photos": "Pictures",
    "music": "Music",
    "videos": "Videos",
    "home": "",
}


def resolve_path(raw: str) -> Path:
    """Expand ~, %ENV% vars and bare names like 'downloads' or 'desktop/notes.txt'."""
    text = os.path.expandvars(os.path.expanduser(raw.strip().strip('"')))
    first, _, rest = text.replace("\\", "/").partition("/")
    key = first.lower()
    if key in _KNOWN_FOLDERS and not Path(text).is_absolute():
        base = Path.home() / _KNOWN_FOLDERS[key]
        # OneDrive often redirects Desktop/Documents on Windows.
        onedrive = os.environ.get("OneDrive")
        if onedrive and not base.exists() and (Path(onedrive) / _KNOWN_FOLDERS[key]).exists():
            base = Path(onedrive) / _KNOWN_FOLDERS[key]
        return base / rest if rest else base
    p = Path(text)
    return p if p.is_absolute() else Path.home() / p
