"""Finding other AI tools, reading what they are doing, and the guard rails on acting on them."""

import time
from types import SimpleNamespace

import pytest

from app import actuator, ai_agents, autonomy, config, jobs, tools, winapi
from app.ai_agents import Proc
from app.autonomy import HardConfirm
from app.tools import ai_agents_tools as agent_tools

CLAUDE_PROMPT = """\
● Bash(npm test)
╭──────────────────────────────────────────────╮
│ Bash command                                 │
│   npm test -- --coverage                     │
│   Run the unit tests with coverage           │
╰──────────────────────────────────────────────╯
Do you want to proceed?
❯ 1. Yes
  2. Yes, and don't ask again for npm commands in /home/me/app
  3. No, and tell Claude what to do differently (esc)
"""

WORKING = """\
● Update(src/auth.ts)
  ⎿  Updated src/auth.ts with 12 additions
✻ Thinking… (34s · 1.2k tokens · esc to interrupt)
"""

IDLE = """\
● Done. I added the retry logic and the tests pass.

╭──────────────────────────────────────────────╮
│ >                                            │
╰──────────────────────────────────────────────╯
  ? for shortcuts
"""


# ---------------------------------------------------------------- reading the screen


def test_claude_code_permission_prompt():
    r = ai_agents.analyze(CLAUDE_PROMPT)
    assert r.state == "needs you" and r.approve_with == "enter"
    assert "npm test -- --coverage" in r.prompt and "Do you want to proceed?" in r.prompt


def test_yes_no_prompt():
    r = ai_agents.analyze("Writing config.yml\nconfig.yml already exists. Overwrite? (y/n) ")
    assert r.state == "needs you" and r.approve_with == "y" and "Overwrite" in r.prompt


def test_a_prompt_we_cannot_answer_safely():
    r = ai_agents.analyze("Which branch should I use?\nDo you want to continue with main or release?")
    assert r.state == "needs you" and r.approve_with == ""  # nothing a plain Enter or y would mean


def test_working_idle_and_unknown():
    assert ai_agents.analyze(WORKING).state == "working"
    assert ai_agents.analyze(IDLE).state == "idle"
    assert ai_agents.analyze("PS C:\\Users\\me\\app>").state == "idle"
    assert ai_agents.analyze("").state == "unknown"
    assert ai_agents.analyze("\n\n   \n").state == "unknown"


def test_errors_are_called_out_with_the_line():
    r = ai_agents.analyze("Calling the API...\nError: 429 rate limit exceeded, retrying in 20s\n> ")
    assert r.state == "error" and "429" in r.problem
    assert ai_agents.analyze("Traceback (most recent call last):\n  File x\nValueError: boom").state == "error"
    assert ai_agents.analyze("Error: invalid API key\n❯ ").state == "error"


def test_an_answered_prompt_further_up_does_not_count():
    lines = ["Overwrite? (y/n) y"] + [f"building step {i}" for i in range(12)] + ["> "]
    assert ai_agents.analyze("\n".join(lines)).state == "idle"


def test_working_beats_an_old_error_but_a_prompt_beats_working():
    assert ai_agents.analyze("Error: failed to fetch\n" + WORKING).state == "working"
    assert ai_agents.analyze(WORKING + "Do you want to proceed?\n❯ 1. Yes\n  2. No\n").state == "needs you"


def test_terminal_colour_codes_are_ignored():
    r = ai_agents.analyze("\x1b[1m\x1b[32mDo you want to proceed?\x1b[0m\n\x1b[36m❯ 1. Yes\x1b[0m\n  2. No")
    assert r.state == "needs you" and r.approve_with == "enter"


def test_option_one_must_be_a_plain_yes_to_approve_with_enter():
    r = ai_agents.analyze("Do you want to proceed?\n❯ 1. Yes, and don't ask again\n  2. No")
    assert r.state == "needs you" and r.approve_with == ""  # a broader grant than the user was asked about


# ---------------------------------------------------------------- finding them


def win(hwnd, pid, title, name):
    return winapi.WindowInfo(hwnd, title, pid, f"C:\\x\\{name}", name, (0, 0, 10, 10), False, False)


@pytest.fixture
def desktop():
    now = time.time()
    procs = [
        Proc(100, 1, "windowsterminal.exe", "WindowsTerminal.exe", now - 7200),
        Proc(101, 100, "pwsh.exe", "pwsh.exe", now - 7000),
        Proc(
            102, 101, "claude.exe", "claude", now - 600, cpu=12.0
        ),  # Claude Code in the terminal: no window of its own
        Proc(120, 1, "wezterm-gui.exe", "wezterm-gui.exe", now - 800),
        Proc(110, 120, "pwsh.exe", "pwsh.exe", now - 500),
        Proc(111, 110, "node.exe", r"node C:\Users\me\AppData\npm\node_modules\@openai\codex\bin\codex.js", now - 90),
        Proc(
            200, 1, "claude.exe", r"C:\Users\me\AppData\Local\AnthropicClaude\claude.exe", now - 5000
        ),  # the desktop app
        Proc(201, 200, "claude.exe", r"claude.exe --type=renderer", now - 5000),
        Proc(202, 200, "claude.exe", r"claude.exe --type=gpu", now - 5000),
        Proc(300, 1, "cursor.exe", "Cursor.exe", now - 3000),
        Proc(301, 300, "cursor.exe", "Cursor.exe --type=renderer", now - 3000),
        Proc(302, 300, "cursor.exe", "Cursor.exe --type=utility", now - 3000),
        Proc(400, 1, "chrome.exe", "chrome.exe", now - 9000),
        Proc(500, 1, "ollama.exe", "ollama serve", now - 20000),
        Proc(600, 1, "notepad.exe", "notepad.exe", now - 20),
    ]
    windows = [
        win(1, 100, "claude: fix the login bug", "WindowsTerminal.exe"),
        win(6, 120, "codex: add tests", "wezterm-gui.exe"),
        win(2, 200, "Claude", "claude.exe"),
        win(3, 300, "app - Cursor", "Cursor.exe"),
        win(4, 400, "Fixing a bug - ChatGPT - Google Chrome", "chrome.exe"),
        win(5, 600, "Untitled - Notepad", "notepad.exe"),
    ]
    return procs, windows


def test_scan_finds_each_tool_once(desktop):
    found = {f.kind.id: f for f in ai_agents.scan(*desktop)}
    assert set(found) == {"claude-code", "codex", "claude-desktop", "cursor", "browser-chat", "ollama"}
    assert (
        found["claude-code"].id == "claude-code:102" and found["claude-code"].hwnd == 1
    )  # shown in the terminal window
    assert found["codex"].id == "codex:111" and found["codex"].hwnd == 6
    assert found["claude-desktop"].pid == 200 and found["claude-desktop"].hwnd == 2
    assert found["cursor"].pid == 300 and found["cursor"].hwnd == 3  # helper processes are not listed
    assert found["browser-chat"].hwnd == 4 and found["ollama"].hwnd == 0
    assert len([f for f in ai_agents.scan(*desktop) if f.kind.id == "claude-desktop"]) == 1


def test_the_claude_desktop_app_is_not_mistaken_for_claude_code(desktop):
    procs, windows = desktop
    ids = [f.id for f in ai_agents.scan(procs, windows)]
    assert "claude-code:200" not in ids and "claude-desktop:102" not in ids


def test_agents_come_before_chats(desktop):
    kinds = [f.kind for f in ai_agents.scan(*desktop)]
    assert [k.chat for k in kinds] == sorted(k.chat for k in kinds)


def test_nothing_running():
    assert ai_agents.scan([Proc(1, 0, "notepad.exe", "notepad.exe", time.time())], []) == []


def test_find_by_id_name_title_and_ambiguity(desktop):
    agents = ai_agents.scan(*desktop)
    assert ai_agents.find("claude-code:102", agents)[0].pid == 102
    assert ai_agents.find("cursor", agents)[0].pid == 300
    assert ai_agents.find("codex cli", agents)[0].kind.id == "codex"
    assert ai_agents.find("fix the login", agents)[0].kind.id == "claude-code"  # by window title
    a, problem = ai_agents.find("claude", agents)
    assert a is None and "More than one" in problem
    a, problem = ai_agents.find("nonexistent", agents)
    assert a is None and "Running:" in problem
    assert ai_agents.find("", agents)[0] is None  # several are running: it asks which
    assert ai_agents.find("x", [])[1] == "I don't see any AI tools running."
    only = agents[:1]
    assert ai_agents.find("", only)[0] is only[0]


# ---------------------------------------------------------------- the tools


@pytest.fixture
def world(desktop, monkeypatch):
    procs, windows = desktop
    screens = {1: CLAUDE_PROMPT, 2: IDLE, 3: WORKING, 4: "", 6: "PS C:\\app>"}
    state = SimpleNamespace(front=1, keys=[], typed=[], screens=screens, focus_calls=[])
    monkeypatch.setattr(ai_agents, "_processes", lambda: procs)
    monkeypatch.setattr(ai_agents, "_windows", lambda: windows)
    monkeypatch.setattr(ai_agents, "_read_window", lambda hwnd, max_chars=8000: state.screens.get(hwnd, ""))
    monkeypatch.setattr(ai_agents, "_foreground_hwnd", lambda: state.front)
    monkeypatch.setattr(winapi, "focus", lambda h: state.focus_calls.append(h) or True, raising=False)
    monkeypatch.setattr(actuator, "hotkey", lambda keys: state.keys.append(list(keys)))
    monkeypatch.setattr(actuator, "type_text", lambda text: state.typed.append(text))
    monkeypatch.setattr(agent_tools.time, "sleep", lambda s: None)
    monkeypatch.setattr(jobs, "MANAGER", None)
    monkeypatch.setattr(config, "CONFIRM_LEVEL", "normal")
    tools.load_all()
    autonomy.revoke()
    yield state
    autonomy.revoke()


def test_overview_says_who_needs_you(world):
    r = agent_tools.ai_agents_overview()
    assert r.ok
    head = r.text.splitlines()[0]
    assert "6 AI tools running" in head and "1 needs you" in head
    assert "needs you" in r.text and "npm test" not in head
    assert "asking: " in r.text and "working" in r.text and "idle" in r.text
    assert "[claude-code:102]" in r.text and "[cursor:300]" in r.text


def test_overview_includes_jarvis_background_tasks(world, monkeypatch):
    mgr = SimpleNamespace(
        list=lambda: [
            {"id": "ab12", "title": "Sort my downloads", "status": "running", "steps": 4, "step": "move_path"},
            {"id": "cd34", "title": "Old one", "status": "done", "steps": 2, "step": ""},
        ]
    )
    monkeypatch.setattr(jobs, "MANAGER", mgr)
    out = agent_tools.ai_agents_overview().text
    assert '[job:ab12] Jarvis background task "Sort my downloads" | running | 4 steps now: move_path' in out
    assert "cd34" not in out  # finished ones aren't "running"


def test_overview_when_nothing_is_running(monkeypatch):
    monkeypatch.setattr(ai_agents, "_processes", lambda: [])
    monkeypatch.setattr(ai_agents, "_windows", lambda: [])
    monkeypatch.setattr(jobs, "MANAGER", None)
    assert "don't see any AI tools" in agent_tools.ai_agents_overview().text


def test_detail_shows_the_question_it_is_stopped_on(world):
    out = agent_tools.ai_agent_detail("claude code").text
    assert "needs you" in out and "It is asking:" in out and "npm test -- --coverage" in out
    assert "Latest output:" in out


def test_detail_on_an_unreadable_one_and_unknown_names(world):
    assert (
        "no window I can reach"
        in agent_tools.ai_agent_detail("ollama").text.replace("window I can read", "window I can reach")
        or "no window" in agent_tools.ai_agent_detail("ollama").text
    )
    assert not agent_tools.ai_agent_detail("skynet").ok
    assert "couldn't read" in agent_tools.ai_agent_detail("browser-chat").text


def test_detail_for_a_jarvis_job(world, monkeypatch):
    rec = {
        "id": "ab12",
        "title": "Sort downloads",
        "status": "running",
        "steps": 3,
        "step": "organize folder: downloads",
        "result": "",
        "paused": False,
        "waiting": "",
        "activity": [
            {"t": 1.0, "kind": "note", "text": "Looking at what is in Downloads"},
            {"t": 2.0, "kind": "doing", "text": "list dir: downloads"},
        ],
    }
    monkeypatch.setattr(jobs, "MANAGER", SimpleNamespace(get=lambda i: rec if i == "ab12" else None))
    out = agent_tools.ai_agent_detail("job:ab12").text
    assert "Sort downloads" in out and "Doing now: organize folder: downloads" in out
    assert "note: Looking at what is in Downloads" in out and "doing: list dir: downloads" in out
    assert not agent_tools.ai_agent_detail("job:zzzz").ok


# ---------------------------------------------------------------- acting on another tool


def reason(**args):
    return tools.REGISTRY["ai_agent_control"].confirmation_reason(args)


def test_approving_and_sending_always_ask_even_in_autonomy_mode(world):
    autonomy.grant()
    r = reason(agent="claude code", action="approve")
    assert isinstance(r, HardConfirm) and "npm test -- --coverage" in r  # you see exactly what you'd be approving
    s = reason(agent="cursor", action="send", text="delete the tests")
    assert isinstance(s, HardConfirm) and "delete the tests" in s
    assert reason(agent="cursor", action="interrupt") is None  # routine, so skipped in this mode...
    autonomy.revoke()
    assert reason(agent="cursor", action="interrupt")  # ...but asked about normally
    assert reason(agent="cursor", action="focus") is None and reason(agent="cursor", action="deny") is None


def test_approve_presses_enter_for_a_numbered_prompt(world):
    r = agent_tools.ai_agent_control("claude code", "approve")
    assert r.ok and world.keys == [["enter"]] and world.typed == [] and world.focus_calls == [1]
    assert "Approved Claude Code" in r.text


def test_approve_types_y_for_a_yes_no_prompt(world):
    world.screens[1] = "Overwrite config.yml? (y/n) "
    assert agent_tools.ai_agent_control("claude code", "approve").ok
    assert world.typed == ["y"] and world.keys == [["enter"]]


def test_deny(world):
    assert agent_tools.ai_agent_control("claude code", "deny").ok and world.keys == [["esc"]]
    world.keys.clear()
    world.screens[1] = "Overwrite? (y/n) "
    assert agent_tools.ai_agent_control("claude code", "deny").ok
    assert world.typed == ["n"] and world.keys == [["enter"]]


def test_it_refuses_to_approve_something_that_is_not_asking(world):
    world.screens[1] = WORKING
    r = agent_tools.ai_agent_control("claude code", "approve")
    assert not r.ok and "isn't asking" in r.text and world.keys == [] and world.typed == []


def test_it_refuses_a_prompt_it_cannot_answer_safely(world):
    world.screens[1] = "Which branch should I use?\nDo you want to continue with main or release?"
    r = agent_tools.ai_agent_control("claude code", "approve")
    assert not r.ok and "can't safely answer" in r.text and world.keys == [] and world.typed == []


def test_it_never_types_into_the_wrong_window(world):
    world.front = 99  # focus didn't land on the agent
    r = agent_tools.ai_agent_control("claude code", "send", "hello")
    assert not r.ok and "to the front" in r.text and world.keys == [] and world.typed == []


def test_send_types_one_clean_line(world):
    r = agent_tools.ai_agent_control("claude code", "send", "run the tests\nthen rm -rf /\x07")
    assert r.ok and world.typed == ["run the tests then rm -rf /"] and world.keys == [["enter"]]
    assert not agent_tools.ai_agent_control("claude code", "send", "  \n ").ok


def test_interrupt_uses_the_right_key(world):
    agent_tools.ai_agent_control("claude code", "interrupt")  # full-screen terminal agent: Esc
    assert world.keys == [["esc"]]
    world.keys.clear()
    world.front = 3
    agent_tools.ai_agent_control("cursor", "interrupt")
    assert world.keys == [["ctrl", "c"]]


def test_focus_only_brings_it_forward(world):
    r = agent_tools.ai_agent_control("claude code", "focus")
    assert r.ok and world.keys == [] and world.typed == []


def test_bad_requests(world):
    assert not agent_tools.ai_agent_control("claude code", "format_disk").ok
    assert not agent_tools.ai_agent_control("skynet", "approve").ok
    assert not agent_tools.ai_agent_control("ollama", "send", "hi").ok  # no window to type into
    assert world.keys == [] and world.typed == []


def test_toolset_routing_finds_the_agent_tools():
    import re

    from app import toolsets

    pattern = next(p for p, g in toolsets._KEYWORDS if g == "agents")
    for phrase in (
        "what are my ai agents doing",
        "is claude code stuck",
        "does anything need me",
        "what is cursor doing",
        "check codex",
    ):
        assert re.search(pattern, phrase, re.I), phrase
    assert set(toolsets.BUILTIN_GROUPS["agents"]) <= set(tools.REGISTRY)
    assert all(
        tools.REGISTRY[n].ui_thread for n in toolsets.BUILTIN_GROUPS["agents"]
    )  # screen tools: never in background jobs


# ---------------------------------------------------------------- two agents in one terminal window


@pytest.fixture
def tabs(monkeypatch):
    """Claude Code and Codex in two tabs of the same Windows Terminal window: only one tab is readable."""
    now = time.time()
    procs = [
        Proc(100, 1, "windowsterminal.exe", "WindowsTerminal.exe", now - 7200),
        Proc(101, 100, "pwsh.exe", "pwsh.exe", now - 7000),
        Proc(102, 101, "claude.exe", "claude", now - 600),
        Proc(110, 100, "pwsh.exe", "pwsh.exe", now - 500),
        Proc(111, 110, "node.exe", r"node C:\x\node_modules\@openai\codex\bin\codex.js", now - 90),
    ]
    windows = [win(1, 100, "claude: fix the login bug", "WindowsTerminal.exe")]
    state = SimpleNamespace(reads=[], keys=[], typed=[])

    def read(hwnd, max_chars=8000):
        state.reads.append(hwnd)
        return CLAUDE_PROMPT

    monkeypatch.setattr(ai_agents, "_processes", lambda: procs)
    monkeypatch.setattr(ai_agents, "_windows", lambda: windows)
    monkeypatch.setattr(ai_agents, "_read_window", read)
    monkeypatch.setattr(ai_agents, "_foreground_hwnd", lambda: 1)
    monkeypatch.setattr(winapi, "focus", lambda h: True, raising=False)
    monkeypatch.setattr(actuator, "hotkey", lambda keys: state.keys.append(keys))
    monkeypatch.setattr(actuator, "type_text", lambda text: state.typed.append(text))
    monkeypatch.setattr(agent_tools.time, "sleep", lambda s: None)
    monkeypatch.setattr(jobs, "MANAGER", None)
    tools.load_all()
    return state


def test_agents_in_one_window_are_flagged(tabs):
    agents = {a.kind.id: a for a in ai_agents.scan()}
    assert agents["claude-code"].shared == ["Codex CLI"] and agents["codex"].shared == ["Claude Code"]


def test_a_shared_window_is_read_once_and_counted_once(tabs):
    r = agent_tools.ai_agents_overview().text
    assert tabs.reads == [1]  # not once per agent
    assert r.splitlines()[0] == "2 AI tools running; 1 needs you."  # one prompt on screen, not two
    assert r.count("shares its window with") == 2 and "the tab on screen looks needs you" in r


def test_it_will_not_guess_which_tab_to_act_on(tabs):
    for action in ("approve", "deny", "send", "interrupt"):
        res = agent_tools.ai_agent_control("claude code", action, "hi")
        assert not res.ok and "shares one window with Codex CLI" in res.text, action
    assert tabs.keys == [] and tabs.typed == []
    assert agent_tools.ai_agent_control("claude code", "focus").ok  # bringing it forward is harmless


def test_detail_warns_that_the_reading_may_be_the_other_tab(tabs):
    assert "shares this window with Claude Code" in agent_tools.ai_agent_detail("codex").text
    assert "shares this window with Codex CLI" in agent_tools.ai_agent_detail("claude code").text
