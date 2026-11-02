"""
File management: make folders, copy, move, rename, tidy a folder, and download files.

Rules these tools keep, so they are safe to use freely (and in work-without-asking mode):

- They never overwrite silently. A name clash gets "name (1)" unless the caller asks to replace one FILE,
  which asks you first and keeps the old file in the undo journal.
- They refuse system locations: a drive's top level, your user folder itself, Windows, Program Files,
  ProgramData and Jarvis's own data folder.
- Every change is recorded in the undo journal ("undo that" puts it back).
- Downloads go to a temporary .part file first, stop at a size limit, are never opened or run, and a
  program or script (.exe, .msi, .bat, .ps1 ...) always asks first, even in work-without-asking mode.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
import shutil
import time
import urllib.parse
from pathlib import Path

import requests

from .. import config, undo
from ..autonomy import HardConfirm
from ..platform_utils import resolve_path
from . import ToolResult, tool

logger = logging.getLogger(__name__)

# A program or script: running one is how malware arrives, so downloading one always asks.
DANGEROUS_EXTENSIONS = frozenset(
    {
        ".exe", ".msi", ".msix", ".msp", ".appx", ".appxbundle", ".appinstaller", ".bat", ".cmd", ".com", ".scr",
        ".pif", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".cpl", ".reg", ".lnk",
        ".jar", ".dll", ".sys", ".apk", ".dmg", ".pkg", ".sh",
    }
)  # fmt: skip
_PARTIAL = (".crdownload", ".part", ".tmp", ".download", ".opdownload", ".partial")
_WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_DOWNLOAD_MB = 4096


# ------------------------------------------------------------------ helpers


def safe_filename(name: str, fallback: str = "download") -> str:
    """A name that is valid on Windows, can't climb out of a folder, and isn't absurdly long."""
    name = urllib.parse.unquote(name or "")
    name = _BAD_CHARS.sub("_", name).strip(" .")
    if len(name) > 150:
        stem, ext = os.path.splitext(name)
        name = stem[: 150 - len(ext)] + ext
    if not name:
        name = fallback
    if name.split(".")[0].lower() in _WIN_RESERVED:
        name = "_" + name
    return name


def _protected_roots() -> list[Path]:
    roots = [
        Path(v)
        for v in (
            os.environ.get(k)
            for k in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData")
        )
        if v
    ]
    if os.name != "nt":
        roots += [
            Path(p)
            for p in ("/etc", "/usr", "/bin", "/sbin", "/boot", "/lib", "/lib64", "/sys", "/proc", "/dev", "/System")
        ]
    roots.append(config.DATA_DIR)
    return roots


def _resolved(p: Path) -> Path:
    try:
        return p.resolve()
    except (OSError, RuntimeError):
        return p


def _blocked(p: Path) -> str | None:
    """Why these tools won't change `p`, or None if it's fine."""
    rp = _resolved(p)
    if rp == Path(rp.anchor):
        return "the top level of a drive"
    if rp == _resolved(Path.home()):
        return "your user folder itself"
    for root in _protected_roots():
        rr = _resolved(root)
        if rp == rr or rr in rp.parents:
            return f"a system location ({root})"
    return None


def _inside(child: Path, parent: Path) -> bool:
    c, p = _resolved(child), _resolved(parent)
    return c == p or p in c.parents


def _unique(p: Path) -> Path:
    if not p.exists():
        return p
    for i in range(1, 1000):
        cand = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not cand.exists():
            return cand
    raise FileExistsError(f"Too many files called {p.name}")


def _target_for(src_name: str, destination: str) -> Path:
    """'downloads' or 'D:/x/' -> inside that folder; 'D:/x/new name.pdf' -> exactly that path."""
    raw = destination.strip().strip('"')
    dest = resolve_path(raw)
    if dest.is_dir() or raw.endswith(("/", "\\")):
        return dest / src_name
    return dest


def _missing_parents(p: Path) -> list[str]:
    made, cur = [], p
    while not cur.exists() and cur != cur.parent:
        made.append(str(cur))
        cur = cur.parent
    return made


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _backup_for_replace(target: Path) -> dict | None:
    """Keep the file about to be replaced, so undo can put it back."""
    if target.is_file():
        saved = undo._backup_file(target)
        return {"path": str(target), "file": saved} if saved else None
    return None


# ------------------------------------------------------------------ make_folder


@tool(
    "make_folder",
    "Create a folder, and any missing parent folders. Paths accept shortcuts like 'documents/Projects'.",
    {"path": {"type": "string"}},
    ["path"],
    category="files",
    mutates=True,
)
def make_folder(path: str) -> ToolResult:
    p = resolve_path(path)
    why = _blocked(p)
    if why:
        return ToolResult(f"I won't create folders in {why}.", ok=False)
    if p.is_dir():
        return ToolResult(f'The folder "{p}" already exists.')
    if p.exists():
        return ToolResult(f'"{p}" is a file, so I can\'t make a folder with that name.', ok=False)
    made = _missing_parents(p)
    p.mkdir(parents=True, exist_ok=True)
    return ToolResult(f'Created the folder "{p}".', undo={"created_dirs": made})


# ------------------------------------------------------------------ copy / move / rename


def _confirm_replace(args: dict) -> str | None:
    """Replacing an existing file asks. Anything else here is harmless, so it doesn't."""
    if not args.get("overwrite"):
        return None
    src = resolve_path(str(args.get("source", "")))
    target = _target_for(src.name, str(args.get("destination", "")))
    if target.is_file() and src.is_file():
        return f'Replace "{target}" with "{src.name}"? The old file is kept in the undo journal.'
    return None


def _plan_transfer(source: str, destination: str, *, move: bool) -> tuple[Path, Path, str | None]:
    src = resolve_path(source)
    if not src.exists():
        return src, src, f'"{src}" doesn\'t exist.'
    if move:
        why = _blocked(src)
        if why:
            return src, src, f"I won't move anything out of {why}."
    target = _target_for(src.name, destination)
    why = _blocked(target)
    if why:
        return src, target, f"I won't put files in {why}."
    if src.is_dir() and _inside(target, src):
        return src, target, "A folder can't be copied or moved into itself."
    if _resolved(target) == _resolved(src):
        return src, target, "It's already there."
    return src, target, None


def _land(src: Path, target: Path, overwrite: bool) -> tuple[Path, dict | None, str | None]:
    """Decide the final path. Returns (path, backup-of-replaced-file, problem)."""
    if not target.exists():
        return target, None, None
    if overwrite and src.is_file() and target.is_file():
        backup = _backup_for_replace(target)
        if backup is None:
            return target, None, "I couldn't keep a copy of the file being replaced, so I stopped."
        target.unlink()
        return target, backup, None
    if overwrite and target.is_dir():
        return target, None, "I won't replace a whole folder. Choose another name."
    return _unique(target), None, None


@tool(
    "copy_path",
    "Copy a file or folder. 'destination' can be a folder (it goes inside, keeping its name) or a full new "
    "path (to rename on copy). Never overwrites: a name clash becomes 'name (1)' unless overwrite=true, "
    "which replaces an existing FILE after asking.",
    {
        "source": {"type": "string"},
        "destination": {"type": "string", "description": "Folder or full target path, e.g. 'documents/Projects'"},
        "overwrite": {"type": "boolean"},
    },
    ["source", "destination"],
    category="files",
    confirm=_confirm_replace,
    mutates=True,
)
def copy_path(source: str, destination: str, overwrite: bool = False) -> ToolResult:
    src, target, problem = _plan_transfer(source, destination, move=False)
    if problem:
        return ToolResult(problem, ok=False)
    made = _missing_parents(target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    final, backup, problem = _land(src, target, bool(overwrite))
    if problem:
        return ToolResult(problem, ok=False)
    if src.is_dir():
        shutil.copytree(src, final)
        count = sum(1 for f in final.rglob("*") if f.is_file())
        what = f"folder ({count} files)"
    else:
        shutil.copy2(src, final)
        what = _human(final.stat().st_size)
    note = "" if final == target else f' (a file with that name was already there, so I used "{final.name}")'
    return ToolResult(
        f'Copied "{src.name}" ({what}) to "{final}".{note}',
        undo={"copied": [str(final)], "created_dirs": made, "replaced": backup},
    )


@tool(
    "move_path",
    "Move a file or folder. 'destination' can be a folder (it goes inside, keeping its name) or a full new "
    "path (to move and rename). Never overwrites: a name clash becomes 'name (1)' unless overwrite=true, "
    "which replaces an existing FILE after asking. Can be undone.",
    {
        "source": {"type": "string"},
        "destination": {"type": "string", "description": "Folder or full target path, e.g. 'D:/Projects/'"},
        "overwrite": {"type": "boolean"},
    },
    ["source", "destination"],
    category="files",
    confirm=_confirm_replace,
    mutates=True,
)
def move_path(source: str, destination: str, overwrite: bool = False) -> ToolResult:
    src, target, problem = _plan_transfer(source, destination, move=True)
    if problem:
        return ToolResult(problem, ok=False)
    made = _missing_parents(target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    final, backup, problem = _land(src, target, bool(overwrite))
    if problem:
        return ToolResult(problem, ok=False)
    shutil.move(str(src), str(final))
    note = "" if final == target else f' (a file with that name was already there, so I used "{final.name}")'
    return ToolResult(
        f'Moved "{src.name}" to "{final}".{note}',
        undo={"moves": [{"from": str(src), "to": str(final)}], "created_dirs": made, "replaced": backup},
    )


@tool(
    "rename_path",
    "Rename a file or folder in place. 'new_name' is just the name, not a path. Won't replace an existing one.",
    {"path": {"type": "string"}, "new_name": {"type": "string"}},
    ["path", "new_name"],
    category="files",
    mutates=True,
)
def rename_path(path: str, new_name: str) -> ToolResult:
    src = resolve_path(path)
    if not src.exists():
        return ToolResult(f'"{src}" doesn\'t exist.', ok=False)
    why = _blocked(src)
    if why:
        return ToolResult(f"I won't rename anything in {why}.", ok=False)
    name = new_name.strip()
    if not name or name != safe_filename(name, fallback="") or name in (".", ".."):
        return ToolResult("The new name can't contain slashes or characters Windows doesn't allow.", ok=False)
    target = src.with_name(name)
    if target.exists() and str(target).lower() != str(src).lower():
        return ToolResult(f'"{name}" already exists in that folder.', ok=False)
    if target == src:
        return ToolResult("It already has that name.")
    src.rename(target)
    return ToolResult(f'Renamed "{src.name}" to "{name}".', undo={"moves": [{"from": str(src), "to": str(target)}]})


# ------------------------------------------------------------------ organize_folder

_CATEGORIES: dict[str, tuple[str, ...]] = {
    "Images": ("jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "heic", "tif", "tiff", "ico", "raw"),
    "Documents": ("pdf", "doc", "docx", "txt", "rtf", "odt", "md", "ppt", "pptx", "xls", "xlsx", "csv", "epub"),
    "Videos": ("mp4", "mkv", "mov", "avi", "wmv", "flv", "webm", "m4v"),
    "Audio": ("mp3", "wav", "flac", "aac", "ogg", "m4a", "wma"),
    "Archives": ("zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso"),
    "Programs": ("exe", "msi", "msix", "appx", "dmg", "apk"),
    "Code": (
        "py",
        "js",
        "ts",
        "tsx",
        "jsx",
        "json",
        "html",
        "css",
        "c",
        "cpp",
        "h",
        "java",
        "rs",
        "go",
        "ipynb",
        "sh",
        "ps1",
        "bat",
        "sql",
        "yml",
        "yaml",
        "toml",
    ),  # fmt: skip
}
_EXT_TO_CATEGORY = {ext: cat for cat, exts in _CATEGORIES.items() for ext in exts}
_LEAVE_ALONE = {"desktop.ini", "thumbs.db", ".ds_store"}
_LEAVE_EXTS = {".lnk", ".url"}
_FRESH_SECONDS = 120  # a file touched this recently may still be downloading
MAX_ORGANIZE = 500


def _organize_plan(folder: Path, by: str) -> list[tuple[Path, str]]:
    plan: list[tuple[Path, str]] = []
    now = time.time()
    for child in sorted(folder.iterdir(), key=lambda c: c.name.lower()):
        try:
            if not child.is_file() or child.name.startswith(".") or child.name.lower() in _LEAVE_ALONE:
                continue
            ext = child.suffix.lower()
            if ext in _LEAVE_EXTS or ext in _PARTIAL or now - child.stat().st_mtime < _FRESH_SECONDS:
                continue
            if by == "date":
                group = time.strftime("%Y-%m", time.localtime(child.stat().st_mtime))
            else:
                group = _EXT_TO_CATEGORY.get(ext.lstrip("."), "Other")
        except OSError:
            continue
        plan.append((child, group))
        if len(plan) >= MAX_ORGANIZE:
            break
    return plan


def _confirm_organize(args: dict) -> str | None:
    if args.get("dry_run", True):
        return None
    folder = resolve_path(str(args.get("path", "")))
    if not folder.is_dir():
        return None
    n = len(_organize_plan(folder, str(args.get("by", "type"))))
    return f'Sort {n} files in "{folder}" into subfolders? You can undo it.' if n else None


@tool(
    "organize_folder",
    "Tidy a folder by sorting the files directly inside it into subfolders: by='type' (Images, Documents, "
    "Videos, Audio, Archives, Programs, Code, Other) or by='date' (YYYY-MM). Leaves subfolders, shortcuts and "
    "files modified in the last two minutes (possibly still downloading) alone. dry_run defaults to true and only "
    "shows the plan; call again with dry_run=false to do it. Can be undone.",
    {
        "path": {"type": "string", "description": "Folder to tidy, e.g. 'downloads'"},
        "by": {"type": "string", "enum": ["type", "date"]},
        "dry_run": {"type": "boolean"},
    },
    ["path"],
    category="files",
    confirm=_confirm_organize,
    mutates=True,
)
def organize_folder(path: str, by: str = "type", dry_run: bool = True) -> ToolResult:
    folder = resolve_path(path)
    if not folder.is_dir():
        return ToolResult(f'"{folder}" isn\'t a folder.', ok=False)
    why = _blocked(folder)
    if why:
        return ToolResult(f"I won't reorganise {why}.", ok=False)
    by = "date" if by == "date" else "type"
    plan = _organize_plan(folder, by)
    if not plan:
        return ToolResult(f'Nothing to sort in "{folder}" (it has no loose files that are safe to move).')
    groups: dict[str, list[Path]] = {}
    for f, g in plan:
        groups.setdefault(g, []).append(f)
    summary = "\n".join(
        f"- {g}: {len(fs)} file{'s' if len(fs) != 1 else ''} (e.g. {', '.join(f.name for f in fs[:3])})"
        for g, fs in sorted(groups.items())
    )
    if dry_run:
        return ToolResult(
            f'Plan for "{folder}" (nothing moved yet):\n{summary}\nCall again with dry_run=false to do it.'
        )

    moves: list[dict] = []
    made: list[str] = []
    for f, g in plan:
        dest_dir = folder / g
        if not dest_dir.exists():
            made.append(str(dest_dir))
            dest_dir.mkdir()
        final = _unique(dest_dir / f.name)
        try:
            shutil.move(str(f), str(final))
        except OSError as e:
            logger.warning("organize: couldn't move %s: %s", f, e)
            continue
        moves.append({"from": str(f), "to": str(final)})
    skipped = len(plan) - len(moves)
    tail = f" ({skipped} couldn't be moved)" if skipped else ""
    return ToolResult(
        f'Sorted {len(moves)} files in "{folder}":{tail}\n{summary}\nSay "undo that" to put everything back.',
        ok=bool(moves),
        undo={"moves": moves, "created_dirs": made},
    )


# ------------------------------------------------------------------ download_file


def _ext(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def _name_from_url(url: str) -> str:
    return os.path.basename(urllib.parse.urlparse(url).path)


def _confirm_download(args: dict) -> str | None:
    url = str(args.get("url", ""))
    name = str(args.get("filename") or "") or _name_from_url(url)
    if _ext(name) in DANGEROUS_EXTENSIONS:
        host = urllib.parse.urlparse(url).netloc or "the internet"
        return HardConfirm(
            f'Download the program or script file "{safe_filename(name)}" from {host}? Jarvis will not run it.'
        )
    return None


def _header_filename(disposition: str) -> str:
    m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", disposition, re.I) or re.search(
        r'filename\s*=\s*"?([^";]+)"?', disposition, re.I
    )
    return urllib.parse.unquote(m.group(1).strip()) if m else ""


@tool(
    "download_file",
    "Download a file from a web address (http/https) into a folder on this computer. Default folder is "
    "'downloads'; 'destination' can be a folder or a full file path. Saves under a safe name, never overwrites, "
    "stops at max_mb, and never opens or runs the file. Reports the size and a SHA-256 checksum. "
    "Programs and scripts (.exe, .msi, .bat, .ps1 ...) always ask the user first.",
    {
        "url": {"type": "string"},
        "destination": {"type": "string", "description": "Folder or full path. Default 'downloads'."},
        "filename": {"type": "string", "description": "Save as this name instead of the server's."},
        "max_mb": {"type": "integer", "description": "Size limit in MB (default 500)."},
    },
    ["url"],
    category="files",
    confirm=_confirm_download,
    mutates=True,
)
def download_file(url: str, destination: str = "downloads", filename: str = "", max_mb: int = 500) -> ToolResult:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ToolResult("I can only download from http or https addresses.", ok=False)
    max_bytes = max(5, min(int(max_mb or 500), MAX_DOWNLOAD_MB)) * 1024 * 1024
    approved_program = _ext(filename or _name_from_url(url)) in DANGEROUS_EXTENSIONS  # the user saw this name

    raw = destination.strip().strip('"') or "downloads"
    dest = resolve_path(raw)
    explicit_file = (not dest.is_dir()) and bool(dest.suffix) and not raw.endswith(("/", "\\"))
    folder = dest.parent if explicit_file else dest
    why = _blocked(folder)
    if why:
        return ToolResult(f"I won't save files in {why}.", ok=False)

    tmp: Path | None = None
    try:
        with requests.get(
            parsed.geturl(),
            stream=True,
            timeout=(15, 30),
            headers={"User-Agent": "Mozilla/5.0 (compatible; Jarvis-desktop-assistant)"},
        ) as resp:
            if not resp.ok:
                return ToolResult(f"The server answered {resp.status_code}, so nothing was downloaded.", ok=False)
            total = int(resp.headers.get("Content-Length") or 0)
            if total > max_bytes:
                return ToolResult(
                    f"That file is {_human(total)}, over the {_human(max_bytes)} limit. Nothing was downloaded.",
                    ok=False,
                )
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            name = safe_filename(
                dest.name
                if explicit_file
                else filename
                or _header_filename(resp.headers.get("Content-Disposition", ""))
                or _name_from_url(resp.url)
            )
            if not _ext(name):
                name += mimetypes.guess_extension(ctype) or ""
            if _ext(name) in DANGEROUS_EXTENSIONS and not approved_program:
                return ToolResult(
                    f'The server is sending a program or script ("{name}"). I stopped. If you really want it, ask me to '
                    "download it with that file name so you can approve it.",
                    ok=False,
                )
            made = _missing_parents(folder)
            folder.mkdir(parents=True, exist_ok=True)
            if total and shutil.disk_usage(folder).free < total * 1.1:
                return ToolResult(f"There isn't enough free space for {_human(total)}.", ok=False)
            target = _unique(folder / name)
            tmp = target.with_name(target.name + ".part")
            digest, size, started = hashlib.sha256(), 0, time.time()
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(1 << 16):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        return ToolResult(f"It grew past the {_human(max_bytes)} limit, so I stopped.", ok=False)
                    if time.time() - started > 1800:
                        return ToolResult("The download was taking over 30 minutes, so I stopped.", ok=False)
                    fh.write(chunk)
                    digest.update(chunk)
            looks_like_page = ctype in ("text/html", "application/xhtml+xml") and _ext(name) not in (".html", ".htm")
            if looks_like_page and size < 512 * 1024:
                return ToolResult(
                    "The link gave me a web page, not a file (it may need a login or has moved). Nothing was saved.",
                    ok=False,
                )
            os.replace(tmp, target)
            tmp = None
    except requests.RequestException as e:
        return ToolResult(f"The download failed: {type(e).__name__}.", ok=False)
    except OSError as e:
        return ToolResult(f"I couldn't save the file: {e.strerror or type(e).__name__}.", ok=False)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)

    note = " I haven't opened or run it." if _ext(target.name) in DANGEROUS_EXTENSIONS else ""
    return ToolResult(
        f'Downloaded "{target.name}" ({_human(size)}) to "{target}". SHA-256 {digest.hexdigest()[:16]}...{note}',
        undo={"copied": [str(target)], "created_dirs": made},
    )
