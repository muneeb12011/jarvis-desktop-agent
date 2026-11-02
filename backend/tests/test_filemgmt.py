"""make_folder / copy_path / move_path / rename_path / organize_folder / download_file."""

import os
import time
from pathlib import Path

import pytest
import requests

from app import autonomy, config, memory, tools, toolsets, undo
from app.agent import _run_tool
from app.autonomy import HardConfirm
from app.tools import filemgmt


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "Downloads").mkdir(parents=True)
    (h / "Documents").mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(undo, "UNDO_DIR", data / "undo")
    monkeypatch.setattr(memory, "DB_PATH", data / "m.db")
    monkeypatch.setattr(config, "CONFIRM_LEVEL", "normal")
    tools.load_all()
    autonomy.revoke()
    yield h
    autonomy.revoke()


def t(name):
    return tools.REGISTRY[name]


def put(path: Path, text="x", age=3600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    old = time.time() - age
    os.utime(path, (old, old))
    return path


def run(name, **args):
    """Run a tool the way the agent does, with the undo journal, and return (result, undo_id)."""
    return _run_tool(t(name), args, 1)


# ---------------------------------------------------------------- make_folder


def test_make_folder_nested_and_idempotent(home):
    r = filemgmt.make_folder("documents/Projects/2026/Q4")
    assert r.ok and (home / "Documents/Projects/2026/Q4").is_dir()
    assert "already exists" in filemgmt.make_folder("documents/Projects").text
    put(home / "Documents/afile")
    assert not filemgmt.make_folder("documents/afile").ok


def test_make_folder_undo_removes_only_what_it_made(home):
    (home / "Documents/Projects").mkdir()
    _, uid = run("make_folder", path="documents/Projects/new/deeper")
    put(home / "Documents/Projects/new/deeper/keep.txt")
    msg = undo.undo_entry(uid)
    assert (home / "Documents/Projects").is_dir()
    assert (home / "Documents/Projects/new/deeper/keep.txt").exists()  # not empty, so left alone
    assert "Nothing needed" in msg or "Removed" in msg


def test_nothing_is_created_in_protected_places(home, tmp_path, monkeypatch):
    assert not filemgmt.make_folder("/").ok
    assert not filemgmt.make_folder(str(home)).ok
    assert not filemgmt.make_folder(str(config.DATA_DIR / "inside")).ok
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    assert not filemgmt.make_folder(str(tmp_path / "Program Files" / "x")).ok


# ---------------------------------------------------------------- copy / move / rename


def test_copy_into_folder_keeps_name_and_leaves_source(home):
    src = put(home / "Downloads/report.pdf", "data")
    r = filemgmt.copy_path("downloads/report.pdf", "documents/Reports/")
    assert r.ok and (home / "Documents/Reports/report.pdf").read_text() == "data" and src.exists()


def test_copy_to_a_new_name(home):
    put(home / "Downloads/a.txt", "one")
    assert filemgmt.copy_path("downloads/a.txt", "documents/b.txt").ok
    assert (home / "Documents/b.txt").read_text() == "one"


def test_a_clash_never_overwrites(home):
    put(home / "Downloads/a.txt", "new")
    put(home / "Documents/a.txt", "old")
    r = filemgmt.copy_path("downloads/a.txt", "documents")
    assert r.ok and "a (1).txt" in r.text
    assert (home / "Documents/a.txt").read_text() == "old"
    assert (home / "Documents/a (1).txt").read_text() == "new"


def test_overwrite_asks_replaces_and_undo_restores(home):
    put(home / "Downloads/a.txt", "new")
    put(home / "Documents/a.txt", "old")
    args = {"source": "downloads/a.txt", "destination": "documents", "overwrite": True}
    assert t("copy_path").confirmation_reason(args)  # replacing a file asks
    assert t("copy_path").confirmation_reason({**args, "overwrite": False}) is None
    _, uid = run("copy_path", **args)
    assert (home / "Documents/a.txt").read_text() == "new"
    undo.undo_entry(uid)
    assert (home / "Documents/a.txt").read_text() == "old"


def test_overwrite_never_replaces_a_whole_folder(home):
    put(home / "Documents/proj/old.txt", "keep me")
    put(home / "Downloads/proj/new.txt", "new")
    r = filemgmt.copy_path("downloads/proj", "documents", overwrite=True)  # a folder called proj is already there
    assert not r.ok and "folder" in r.text and (home / "Documents/proj/old.txt").read_text() == "keep me"
    put(home / "Downloads/data", "a file")
    (home / "Documents/data").mkdir()  # and a file can't replace a folder either
    assert not filemgmt.copy_path("downloads/data", "documents", overwrite=True).ok


def test_copying_into_an_existing_folder_goes_inside_it(home):
    (home / "Documents/stuff").mkdir()
    put(home / "Downloads/stuff", "file")
    assert filemgmt.copy_path("downloads/stuff", "documents/stuff").ok
    assert (home / "Documents/stuff/stuff").read_text() == "file"


def test_copy_a_folder_recursively(home):
    put(home / "Documents/proj/src/a.py", "1")
    put(home / "Documents/proj/b.py", "2")
    r = filemgmt.copy_path("documents/proj", "downloads")
    assert r.ok and "2 files" in r.text and (home / "Downloads/proj/src/a.py").read_text() == "1"


def test_cannot_copy_or_move_a_folder_into_itself(home):
    put(home / "Documents/proj/a.py")
    assert not filemgmt.copy_path("documents/proj", "documents/proj/sub").ok
    assert not filemgmt.move_path("documents/proj", "documents/proj/sub").ok


def test_missing_source_and_same_place(home):
    assert not filemgmt.copy_path("documents/nope.txt", "downloads").ok
    put(home / "Documents/a.txt")
    r = filemgmt.move_path("documents/a.txt", "documents")
    assert not r.ok and "already there" in r.text


def test_move_then_undo_puts_everything_back(home):
    put(home / "Downloads/invoice.pdf", "inv")
    r, uid = run("move_path", source="downloads/invoice.pdf", destination="documents/Invoices/")
    assert r.ok and not (home / "Downloads/invoice.pdf").exists()
    assert (home / "Documents/Invoices/invoice.pdf").read_text() == "inv"
    assert uid  # recorded in the journal
    undo.undo_entry(uid)
    assert (home / "Downloads/invoice.pdf").read_text() == "inv"
    assert not (home / "Documents/Invoices").exists()  # the folder the move made is gone again


def test_move_overwrite_keeps_the_old_file_for_undo(home):
    put(home / "Downloads/a.txt", "new")
    put(home / "Documents/a.txt", "old")
    _, uid = run("move_path", source="downloads/a.txt", destination="documents", overwrite=True)
    assert (home / "Documents/a.txt").read_text() == "new"
    undo.undo_entry(uid)
    assert (home / "Documents/a.txt").read_text() == "old" and (home / "Downloads/a.txt").read_text() == "new"


def test_undo_does_not_trample_something_new(home):
    put(home / "Downloads/a.txt", "orig")
    _, uid = run("move_path", source="downloads/a.txt", destination="documents")
    put(home / "Downloads/a.txt", "someone made a new one")
    msg = undo.undo_entry(uid)
    assert (home / "Downloads/a.txt").read_text() == "someone made a new one"
    assert (home / "Documents/a.txt").read_text() == "orig" and "couldn't" in msg


def test_moves_out_of_protected_places_are_refused(home, monkeypatch, tmp_path):
    assert not filemgmt.move_path(str(home), "documents").ok  # your whole user folder
    secret = put(config.DATA_DIR / "jarvis.db")
    r = filemgmt.move_path(str(secret), "documents")
    assert not r.ok and secret.exists()
    assert not filemgmt.rename_path(str(config.DATA_DIR / "jarvis.db"), "x.db").ok
    put(home / "Downloads/a.txt")
    assert not filemgmt.move_path("downloads/a.txt", str(config.DATA_DIR)).ok  # nor into Jarvis's own data


def test_rename(home):
    put(home / "Documents/old name.txt", "z")
    r = filemgmt.rename_path("documents/old name.txt", "new name.txt")
    assert r.ok and (home / "Documents/new name.txt").read_text() == "z"
    put(home / "Documents/other.txt")
    assert not filemgmt.rename_path("documents/new name.txt", "other.txt").ok  # won't replace
    for bad in ("a/b.txt", "..\\x", "..", "con", "bad?.txt", ""):
        assert not filemgmt.rename_path("documents/new name.txt", bad).ok, bad
    assert "already has" in filemgmt.rename_path("documents/new name.txt", "new name.txt").text


def test_rename_can_be_undone(home):
    put(home / "Documents/a.txt")
    _, uid = run("rename_path", path="documents/a.txt", new_name="b.txt")
    undo.undo_entry(uid)
    assert (home / "Documents/a.txt").exists() and not (home / "Documents/b.txt").exists()


# ---------------------------------------------------------------- organize_folder


def make_mess(home):
    d = home / "Downloads"
    for name in ("photo.JPG", "notes.pdf", "song.mp3", "setup.exe", "script.py", "weird.xyz", "pack.zip", "clip.mp4"):
        put(d / name)
    put(d / "still-downloading.crdownload")
    put(d / ".hidden")
    put(d / "shortcut.lnk")
    put(d / "just-now.pdf", age=5)  # touched seconds ago: maybe still being written
    (d / "a folder").mkdir()
    return d


def test_dry_run_changes_nothing_and_explains(home):
    d = make_mess(home)
    before = sorted(p.name for p in d.iterdir())
    r = filemgmt.organize_folder("downloads")
    assert r.ok and "nothing moved yet" in r.text and "dry_run=false" in r.text
    assert "Images: 1" in r.text and "Documents: 1" in r.text and "Other: 1" in r.text
    assert sorted(p.name for p in d.iterdir()) == before


def test_organize_sorts_skips_the_unsafe_and_undoes(home):
    d = make_mess(home)
    r, uid = run("organize_folder", path="downloads", dry_run=False)
    assert r.ok and "Sorted 8 files" in r.text
    assert (d / "Images/photo.JPG").exists() and (d / "Documents/notes.pdf").exists()
    assert (
        (d / "Programs/setup.exe").exists() and (d / "Archives/pack.zip").exists() and (d / "Other/weird.xyz").exists()
    )
    for stays in ("still-downloading.crdownload", ".hidden", "shortcut.lnk", "just-now.pdf", "a folder"):
        assert (d / stays).exists(), stays
    msg = undo.undo_entry(uid)
    assert "Moved 8 items back" in msg
    assert (d / "photo.JPG").exists() and (d / "notes.pdf").exists() and not (d / "Images").exists()


def test_organize_by_date_and_name_clashes(home):
    d = home / "Downloads"
    old = time.mktime((2025, 3, 14, 12, 0, 0, 0, 0, -1))
    put(d / "a.txt", age=time.time() - old)
    put(d / "Documents/a.txt")  # already a file there with the same name
    r = filemgmt.organize_folder("downloads", by="date", dry_run=False)
    assert r.ok and (d / "2025-03/a.txt").exists()
    r = filemgmt.organize_folder("downloads", by="type", dry_run=False)
    assert (d / "Documents/a.txt").exists()  # untouched original


def test_organize_asks_only_for_the_real_run(home):
    make_mess(home)
    assert t("organize_folder").confirmation_reason({"path": "downloads"}) is None
    assert t("organize_folder").confirmation_reason({"path": "downloads", "dry_run": True}) is None
    assert "Sort 8 files" in t("organize_folder").confirmation_reason({"path": "downloads", "dry_run": False})
    autonomy.grant()
    assert t("organize_folder").confirmation_reason({"path": "downloads", "dry_run": False}) is None


def test_organize_refuses_protected_or_empty(home):
    assert not filemgmt.organize_folder(str(home)).ok
    assert not filemgmt.organize_folder("documents/missing").ok
    assert "Nothing to sort" in filemgmt.organize_folder("documents").text


# ---------------------------------------------------------------- download_file


class FakeResp:
    def __init__(self, body=b"hello world", status=200, headers=None, url=None):
        self.body, self.status_code, self.url, self.fixed_url = body, status, url or "", bool(url)
        self.headers = {"Content-Length": str(len(body)), "Content-Type": "text/plain", **(headers or {})}
        self.ok = status < 400

    def iter_content(self, n):
        for i in range(0, len(self.body), 7):
            yield self.body[i : i + 7]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def serve(monkeypatch):
    calls = []

    def install(resp):
        def get(url, **kw):
            calls.append((url, kw))
            if isinstance(resp, Exception):
                raise resp
            if not resp.fixed_url:
                resp.url = url  # like requests: the final address after redirects
            return resp

        monkeypatch.setattr(filemgmt.requests, "get", get)
        return calls

    return install


def test_download_saves_to_downloads_with_checksum(home, serve):
    calls = serve(FakeResp(b"hello world"))
    r = filemgmt.download_file("https://files.example/doc.txt")
    assert r.ok and (home / "Downloads/doc.txt").read_bytes() == b"hello world"
    assert "11.0 B".replace(".0", "") in r.text or "11 B" in r.text
    assert "b94d27b9934d3e08" in r.text  # sha256("hello world")
    assert calls[0][1]["stream"] is True and calls[0][1]["timeout"] == (15, 30)
    assert not list((home / "Downloads").glob("*.part"))


def test_download_never_overwrites(home, serve):
    serve(FakeResp(b"two"))
    put(home / "Downloads/doc.txt", "one")
    r = filemgmt.download_file("https://files.example/doc.txt")
    assert (home / "Downloads/doc.txt").read_text() == "one" and (home / "Downloads/doc (1).txt").read_text() == "two"
    assert r.ok


def test_download_uses_server_name_and_cleans_it(home, serve):
    serve(FakeResp(b"data", headers={"Content-Disposition": 'attachment; filename="../../evil:name?.txt"'}))
    r = filemgmt.download_file("https://files.example/get?id=1")
    assert r.ok
    (saved,) = [p for p in (home / "Downloads").iterdir()]
    assert saved.parent == home / "Downloads"  # it could not climb out of the folder
    assert not any(c in saved.name for c in "/\\:?") and saved.name.endswith("evil_name_.txt")
    assert not (home / "evil_name_.txt").exists() and not (home.parent / "evil_name_.txt").exists()


def test_download_to_a_named_file_or_folder(home, serve):
    serve(FakeResp(b"abc"))
    assert filemgmt.download_file("https://x.example/a.txt", "documents/Projects/").ok
    assert (home / "Documents/Projects/a.txt").exists()
    assert filemgmt.download_file("https://x.example/a.txt", "documents/renamed.txt").ok
    assert (home / "Documents/renamed.txt").exists()
    assert filemgmt.download_file("https://x.example/a.txt", "downloads", filename="mine.txt").ok
    assert (home / "Downloads/mine.txt").exists()


def test_download_guesses_a_missing_extension(home, serve):
    serve(FakeResp(b"%PDF", headers={"Content-Type": "application/pdf"}))
    assert filemgmt.download_file("https://x.example/get?id=7", filename="invoice").ok
    assert (home / "Downloads/invoice.pdf").exists()


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://x.example/a", "javascript:alert(1)", "not a url", "https://"]
)
def test_download_only_speaks_http(home, serve, url):
    serve(AssertionError("must not touch the network"))
    assert not filemgmt.download_file(url).ok


def test_download_failures_leave_nothing_behind(home, serve):
    serve(FakeResp(b"", status=404))
    r = filemgmt.download_file("https://x.example/a.txt")
    assert not r.ok and "404" in r.text
    serve(requests.ConnectionError("down"))
    assert not filemgmt.download_file("https://x.example/a.txt").ok
    assert list((home / "Downloads").iterdir()) == []


def test_download_size_limits(home, serve):
    serve(FakeResp(b"x" * 10, headers={"Content-Length": str(50 * 1024 * 1024)}))
    r = filemgmt.download_file("https://x.example/big.bin", max_mb=10)
    assert not r.ok and "limit" in r.text
    # a server that lies about Content-Length is stopped mid-stream and its partial file removed
    serve(FakeResp(b"x" * (6 * 1024 * 1024), headers={"Content-Length": "10"}))
    r = filemgmt.download_file("https://x.example/big.bin", max_mb=5)
    assert not r.ok and "limit" in r.text
    assert list((home / "Downloads").iterdir()) == []


def test_a_login_page_is_not_a_download(home, serve):
    serve(FakeResp(b"<html>please log in</html>", headers={"Content-Type": "text/html; charset=utf-8"}))
    r = filemgmt.download_file("https://x.example/report.pdf")
    assert not r.ok and "web page" in r.text and list((home / "Downloads").iterdir()) == []
    assert filemgmt.download_file("https://x.example/page.html").ok  # but an actual .html file is fine


def test_programs_always_ask_even_when_work_without_asking_is_on(home, serve):
    autonomy.grant()
    reason = t("download_file").confirmation_reason({"url": "https://x.example/setup.exe"})
    assert isinstance(reason, HardConfirm) and "setup.exe" in reason and "will not run it" in reason
    assert isinstance(
        t("download_file").confirmation_reason({"url": "https://x.example/get", "filename": "tool.ps1"}), HardConfirm
    )
    assert t("download_file").confirmation_reason({"url": "https://x.example/photo.png"}) is None


def test_a_server_cannot_sneak_a_program_past_the_approval(home, serve):
    serve(FakeResp(b"MZ", headers={"Content-Disposition": 'attachment; filename="update.exe"'}))
    r = filemgmt.download_file("https://x.example/download?id=3")  # innocent-looking URL: nothing was approved
    assert not r.ok and "program or script" in r.text and list((home / "Downloads").iterdir()) == []
    # the user saw and approved the exact name, so this one goes through, and is never run
    r = filemgmt.download_file("https://x.example/download?id=3", filename="update.exe")
    assert r.ok and "haven't opened or run it" in r.text and (home / "Downloads/update.exe").exists()


def test_windows_reserved_and_hostile_names():
    assert filemgmt.safe_filename("CON.txt").startswith("_")
    assert filemgmt.safe_filename("../../etc/passwd") == ".._.._etc_passwd" or "/" not in filemgmt.safe_filename(
        "../../etc/passwd"
    )
    assert (
        filemgmt.safe_filename("a" * 400 + ".pdf").endswith(".pdf")
        and len(filemgmt.safe_filename("a" * 400 + ".pdf")) <= 150
    )
    assert filemgmt.safe_filename("   ...  ") == "download"
    assert filemgmt.safe_filename("") == "download"


def test_download_is_undoable_through_the_journal(home, serve):
    serve(FakeResp(b"hello"))
    r, uid = run("download_file", url="https://files.example/doc.txt")
    assert r.ok and (home / "Downloads/doc.txt").exists() and uid
    undo.undo_entry(uid)
    assert not (home / "Downloads/doc.txt").exists()
    assert list((config.DATA_DIR / "undo" / "trash").iterdir())  # moved to trash, not destroyed


# ---------------------------------------------------------------- registry and routing


def test_tools_are_registered_in_the_files_group_and_found_by_keywords():
    group = toolsets.BUILTIN_GROUPS["files"]
    for name in ("make_folder", "copy_path", "move_path", "rename_path", "organize_folder", "download_file"):
        assert name in group and name in tools.REGISTRY
    import re

    pattern = next(p for p, g in toolsets._KEYWORDS if g == "files")
    for phrase in ("organize my downloads", "move the invoice to documents", "rename that file", "tidy my desktop"):
        assert re.search(pattern, phrase, re.I), phrase


def test_routine_file_work_needs_no_prompt_in_normal_mode(home):
    for name, args in [
        ("make_folder", {"path": "documents/x"}),
        ("copy_path", {"source": "a", "destination": "b"}),
        ("move_path", {"source": "a", "destination": "b"}),
        ("rename_path", {"path": "a", "new_name": "b"}),
    ]:
        assert t(name).confirmation_reason(args) is None, name
