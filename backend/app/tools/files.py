"""
File tools: browse, read, write, find and open files. Paths accept shortcuts
like 'desktop', 'downloads/report.pdf' or '~/projects'.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import os
import time
from pathlib import Path

from ..platform_utils import resolve_path, startfile
from . import ToolResult, tool

_SKIP_DIRS = {
    "node_modules",
    ".git",
    "venv",
    ".venv",
    "__pycache__",
    "AppData",
    "$Recycle.Bin",
    "Windows",
    "Program Files",
    "Program Files (x86)",
    ".cache",
}


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


@tool(
    "list_dir",
    "List a folder's contents (newest first).",
    {
        "path": {"type": "string", "description": "Folder, e.g. 'downloads' or 'D:/projects'"},
        "limit": {"type": "integer"},
    },
    ["path"],
    category="files",
)
def list_dir(path: str, limit: int = 50) -> str:
    p = resolve_path(path)
    if not p.is_dir():
        return f"Not a folder: {p}"
    entries = []
    for child in p.iterdir():
        try:
            st = child.stat()
            entries.append((st.st_mtime, child, st.st_size))
        except OSError:
            continue
    entries.sort(reverse=True)
    lines = [f"{p} ({len(entries)} items)"]
    for mtime, child, size in entries[: max(1, min(limit, 200))]:
        kind = "[dir] " if child.is_dir() else ""
        when = dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        lines.append(f"- {kind}{child.name}  {'' if child.is_dir() else _size(size)}  {when}")
    return "\n".join(lines)


@tool(
    "read_file",
    "Read a text file (code, notes, csv, logs...). Large files are truncated.",
    {"path": {"type": "string"}, "max_chars": {"type": "integer"}},
    ["path"],
    category="files",
)
def read_file(path: str, max_chars: int = 8000) -> str:
    p = resolve_path(path)
    if not p.is_file():
        return f"No such file: {p}"
    if p.suffix.lower() in (".pdf", ".docx", ".xlsx", ".png", ".jpg", ".exe", ".zip"):
        return f"{p.name} is a binary file; open it with open_path instead."
    text = p.read_text(encoding="utf-8", errors="replace")
    cap = max(500, min(max_chars, 20000))
    more = f"\n... ({len(text) - cap} more characters)" if len(text) > cap else ""
    return text[:cap] + more


def _confirm_write(args: dict) -> str | None:
    p = resolve_path(str(args.get("path", "")))
    if p.exists() and not args.get("append"):
        return f"Overwrite the existing file {p}?"
    return None


@tool(
    "write_file",
    "Create or overwrite a text file (set append=true to add to the end).",
    {"path": {"type": "string"}, "content": {"type": "string"}, "append": {"type": "boolean"}},
    ["path", "content"],
    category="files",
    confirm=_confirm_write,
    mutates=True,
)
def write_file(path: str, content: str, append: bool = False) -> str:
    p = resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a" if append else "w", encoding="utf-8") as f:
        f.write(content)
    return f"{'Appended to' if append else 'Wrote'} {p} ({len(content)} chars)."


@tool(
    "find_files",
    "Find files by name pattern under a folder, e.g. pattern '*.pdf' or '*invoice*'.",
    {
        "pattern": {"type": "string"},
        "root": {"type": "string", "description": "Default: home folder"},
        "limit": {"type": "integer"},
    },
    ["pattern"],
    category="files",
)
def find_files(pattern: str, root: str = "~", limit: int = 25) -> str:
    base = resolve_path(root)
    pat = pattern if any(c in pattern for c in "*?[") else f"*{pattern}*"
    hits: list[tuple[float, Path]] = []
    deadline = time.monotonic() + 8
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if fnmatch.fnmatch(name.lower(), pat.lower()):
                fp = Path(dirpath) / name
                try:
                    hits.append((fp.stat().st_mtime, fp))
                except OSError:
                    pass
        if time.monotonic() > deadline:
            break
    hits.sort(reverse=True)
    if not hits:
        return f"No files matching '{pattern}' under {base}."
    return "\n".join(str(p) for _, p in hits[: max(1, min(limit, 100))])


@tool(
    "open_path",
    "Open a file or folder with its default app (PDFs, images, folders, documents...).",
    {"path": {"type": "string"}},
    ["path"],
    category="files",
    mutates=True,
)
def open_path(path: str) -> ToolResult:
    p = resolve_path(path)
    if not p.exists():
        return ToolResult(f"Doesn't exist: {p}", ok=False)
    startfile(str(p))
    return ToolResult(f"Opened {p}.")
