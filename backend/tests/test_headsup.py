"""Heads-up: error detection, de-duplication, frozen apps, downloads."""

import asyncio

import pytest

from app import headsup, memory
from app.winapi import WindowInfo

PY_TRACE = """PS D:\\work> python app.py
Traceback (most recent call last):
  File "D:\\work\\app.py", line 1, in <module>
    import requests
ModuleNotFoundError: No module named 'requests'
PS D:\\work> """

CODE_IN_EDITOR = """def load(path):
    if not path:
        raise ValueError("path is required")  # Error: handled by caller
    return open(path).read()
"""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.db")
    headsup.set_enabled(True)


def win(title="Windows PowerShell", name="WindowsTerminal.exe", hwnd=1):
    return WindowInfo(hwnd, title, 1, "", name, (0, 0, 10, 10), False, False)


@pytest.mark.parametrize(
    ("text", "kind", "sig"),
    [
        (PY_TRACE, "python", "ModuleNotFoundError: No module named 'requests'"),
        (
            "> build\nsrc/a.ts(3,7): error TS2322: Type 'string' is not assignable to type 'number'.\n",
            "typescript",
            None,
        ),
        ("npm ERR! code ENOENT\nnpm ERR! syscall open\nnpm ERR! path D:\\x\\package.json\n", "npm", "npm ERR! path"),
        ("C:\\> pnpx foo\n'pnpx' is not recognized as an internal or external command,\n", "shell", None),
        ("$ cargo build\nerror[E0425]: cannot find value `x` in this scope\n", "rust", "error[E0425]"),
        ("$ git push\nfatal: not a git repository (or any of the parent directories): .git\n", "git", "fatal:"),
    ],
)
def test_detects_common_errors(text, kind, sig):
    d = headsup.detect_error(text)
    assert d is not None and d.kind == kind
    if sig:
        assert d.signature.startswith(sig)


def test_ignores_ordinary_code_and_clean_output():
    assert headsup.detect_error(CODE_IN_EDITOR) is None
    assert headsup.detect_error("PS D:\\work> npm run build\n\n> vite build\n✓ built in 2.1s\n") is None


def test_only_the_tail_counts():
    old = PY_TRACE + "\n" + "\n".join(f"line {i} ok" for i in range(200))
    assert headsup.detect_error(old) is None


def _watcher(text_ref, fg_ref):
    out = []

    async def emit(s):
        out.append(s)

    w = headsup.Watcher(emit, reader=lambda _h: text_ref[0], foreground=lambda: fg_ref[0], downloads_dir=None)
    w.downloads = headsup.Path("/nonexistent")
    return w


def test_new_error_offered_once_and_old_errors_ignored():
    text = ["PS D:\\work> python app.py\n" + PY_TRACE]  # already on screen when first seen
    fg = [win()]
    w = _watcher(text, fg)
    assert w.scan_foreground(now=1000) is None  # baseline
    text[0] += "\npython other.py\nTraceback (most recent call last):\n  File x\nKeyError: 'name'\n"
    s = w.scan_foreground(now=1010)
    assert s and s.kind == "error" and "KeyError" in s.text and "Help me fix" in s.actions[0]["label"]
    assert "KeyError: 'name'" in s.actions[0]["prompt"]
    assert w.scan_foreground(now=1100) is None  # same error, already offered


def test_cooldown_mute_and_disabled_app():
    text = ["clean\n"]
    fg = [win()]
    w = _watcher(text, fg)
    w.scan_foreground(now=0)
    text[0] = "x\nfatal: bad revision 'HEAD~9'\n"
    assert w.scan_foreground(now=100)
    text[0] = "x\nerror TS2304: Cannot find name 'foo'.\n"
    assert w.scan_foreground(now=110) is None  # cooldown
    headsup.set_muted("Terminal", True)
    assert w.scan_foreground(now=1000) is None
    headsup.set_muted("Terminal", False)
    assert w.scan_foreground(now=1000)
    fg[0] = win(title="Spotify", name="Spotify.exe", hwnd=2)
    text[0] = "x\nfatal: something else\n"
    assert w.scan_foreground(now=5000) is None  # not a developer app: never read


def test_frozen_app_after_grace_period():
    fg = [win(title="Untitled - Paint (Not Responding)", name="mspaint.exe", hwnd=9)]
    w = _watcher([""], fg)
    assert w.scan_foreground(now=0) is None
    s = w.scan_foreground(now=10)
    assert s and s.kind == "frozen" and "Paint isn't responding" in s.text.replace("Untitled - ", "")
    assert "Force close" in s.actions[1]["label"]


def test_download_announced_when_finished(tmp_path):
    w = _watcher([""], [None])
    w.downloads = tmp_path
    assert w.scan_downloads() is None  # baseline
    (tmp_path / "report.pdf.crdownload").write_bytes(b"x" * 10)
    assert w.scan_downloads() is None  # still downloading
    (tmp_path / "report.pdf.crdownload").rename(tmp_path / "report.pdf")
    assert w.scan_downloads() is None  # size must hold still for one scan
    s = w.scan_downloads()
    assert s and s.kind == "download" and "report.pdf" in s.text
    assert w.scan_downloads() is None


def test_explanation_replaces_raw_line_and_failure_falls_back():
    text = ["ok\n"]
    w = _watcher(text, [win()])
    w.scan_foreground(now=0)
    text[0] = PY_TRACE
    s = w.scan_foreground(now=500)
    w.explain = lambda d: "requests isn't installed in this environment; run pip install requests."
    assert "pip install requests" in w.enrich(s).text

    def boom(_d):
        raise RuntimeError("no model")

    text[0] += "\nTraceback (most recent call last):\nZeroDivisionError: division by zero\n"
    s2 = w.scan_foreground(now=1000)
    w.explain = boom
    assert w.enrich(s2).text.startswith("ZeroDivisionError")


def test_tool_toggles():
    from app.tools import REGISTRY, load_all

    load_all()
    out = REGISTRY["headsup"].run({"enabled": False, "mute": "VS Code"}).text
    assert "off" in out and "code.exe" in out
    assert not headsup.enabled()
    asyncio.run(asyncio.sleep(0))
