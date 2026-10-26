# Jarvis

A desktop assistant that lives on your Windows PC. Talk to it or type to it;
it answers out loud and actually does things: opens apps, plays music,
searches the web, manages files, sets timers and reminders, runs PowerShell,
drives web pages, and operates any app's UI with the mouse and keyboard when
there's no direct route. It remembers you between sessions and asks before
doing anything risky.

Beyond "do this now", it does the things desktop assistants usually only
promise:

- **Background jobs**: "research X and write me a report" runs in parallel
  while you keep talking; Jarvis tells you when it's done.
- **Routines**: automations in plain English, triggered by time, by an app
  opening, by a file landing in a folder, or by a web page changing.
- **Activity timeline**: a private record of what you worked on, so you can
  ask "how long was I in VS Code today?" or "find that video I watched Monday".
- **Workspaces**: "save this as my coding setup" / "set up coding" reopens
  your apps and puts every window back where it was.
- **Text actions anywhere**: select text in any app, press Ctrl+Alt+Space,
  then fix, rewrite, translate or draft a reply and paste it straight back.

Everything you can plug in (GitHub, Home Assistant, Notion, n8n, your own
scripts...) is covered under **Integrations** below. Where this is heading:
[ROADMAP.md](ROADMAP.md).

---

## Start it

Double-click **`start-jarvis.bat`**. It also works as a restart: it stops any
running Jarvis first. The panel appears bottom-right; the backend runs
minimized.

First time on a new machine, run **`setup-jarvis.bat`** once (installs
everything, details go to `setup.log`).

## Talk to it

| How | What happens |
|---|---|
| Say **"Jarvis, ..."** | Wake word (sun icon on). Works any time, panel hidden or not. |
| Say **"Jarvis"**, wait for the chime, then speak | Same, in two steps. |
| **Ctrl+Alt+J** anywhere | Opens the panel and listens straight away. |
| Click the mic | Same as Ctrl+Alt+J. |
| Type in the box | Text in, text (and voice, if the speaker icon is on) out. |
| After Jarvis answers a spoken request | It keeps listening ~6 s for your follow-up. No wake word needed. |
| Talk over Jarvis | Say "stop" or "Jarvis, ..." to cut it off mid-sentence. |
| **Ctrl+Alt+X** / **Esc** / Stop | Stops whatever it's doing. Slamming the mouse into a screen corner also aborts. |
| **↑** in the text box | Brings back your last message. |
| **Ctrl+Alt+Space** with text selected anywhere | Text actions: fix, rewrite, translate, reply, explain. |

Risky actions (closing apps, overwriting files, shell commands that change
things, shutdown, anything an integration marks destructive) show
**Allow / Don't** in the panel. Ignore it for 60 s and nothing happens.

## Things worth trying

**Everyday**
- "Brief me" / "Good morning" - time, weather, reminders, headlines
- "What's the weather this week?" / "Will it rain tomorrow?"
- "Set a 25 minute focus timer" / "Remind me at 6 to call Hassan"
- "What's in the news about AI?"
- "Define ephemeral"

**Your PC**
- "Open Spotify and play something chill" / "Pause the music" / "Volume 30"
- "What's eating my RAM?" / "How's my battery?"
- "Find the PDFs I downloaded this week and open the newest"
- "Close Chrome" / "Minimize everything except VS Code"
- "Lock the PC" / "Take a screenshot and save it"
- "Run `git status` in D:\transfer\screen-agent"

**On screen**
- "What's on my screen?" / "Summarise this page"
- "Open Notepad and draft a message to the team about tomorrow's standup"
- "Go to github.com and open my notifications" (uses the browser integration)

**Memory**
- "Remember I prefer VS Code over everything else"
- "What do you know about me?" / "What did we talk about yesterday?"
- "Forget that I like tea"

**Jarvis itself**
- "Speak a bit faster" / "Use the American voice" / "Call me boss"
- "Start with Windows"
- "What integrations do you have?" / "Reload integrations"

## The big five

### Background jobs
Say "in the background" (or give it anything long) and Jarvis hands it to a
separate worker and stays free for you. Up to 3 jobs run at once; each shows
as a chip with its current step and a × to cancel. When one finishes Jarvis
says so, long results are saved under `Documents\Jarvis\`, and you can ask
follow-ups ("what was the cheapest one?"). Jobs can't touch your mouse or
keyboard, so they never fight with you.

- "In the background, compare the top 5 laptops under $800 for programming."
- "Go through my Downloads, tell me what's taking the most space and what's safe to delete."
- "Research the companies hiring React devs in Lahore and make me a list."

### Routines
| Trigger | Example |
|---|---|
| Time | "Every weekday at 9, brief me and open my standup notes." |
| Interval | "Every hour, remind me to stretch." |
| App opens | "When I open VS Code, play my focus playlist and set volume to 25." |
| File arrives | "Whenever a PDF lands in Downloads, tell me what it is." |
| Web page changes | "Check the PS5 page every 30 minutes and tell me when it says in stock." |
| Startup | "When Jarvis starts, tell me today's reminders." |
| By name | "Make a routine called 'wrap up' that saves my workspace and summarises my day." Then just say "Jarvis, wrap up." |

Manage them in Settings > Routines (run, pause, delete) or by voice.

### Activity timeline
Every few seconds Jarvis notes which app and window title is in front (VS
Code file names, browser page titles, chat names). Idle time is skipped,
password managers are stored without titles, everything stays in
`backend\memory.db`, and entries older than 30 days are deleted. Switch it
off or wipe it in Settings > Activity history.

- "How long was I in VS Code today?" / "Was I productive this week?"
- "What was I doing around 3pm yesterday?"
- "Find that YouTube video about compilers I watched on Monday."

### Workspaces
Arrange your windows, then "save this as my coding workspace". Later,
"set up coding" relaunches anything that's closed and puts every window back
in place. Restore from Settings > Workspaces too.

### Text actions (Ctrl+Alt+Space)
Select text in any app (WhatsApp, Word, Gmail, VS Code, Slack...), press
**Ctrl+Alt+Space**: Explain, Summarise, Fix, Rewrite, Shorter, Casual,
Translate (English <-> Urdu), Draft reply, or type your own instruction.
**Replace selection** pastes the result over the original text. Your
clipboard is restored afterwards.

## Undo and self-check

**Undo.** Before Jarvis changes something it saves what was there. Say
"undo that" (last change), "undo all of that" (everything from your last
request), or click **Undo** on any action line in the panel. Covered: file
writes (old version restored; files Jarvis created go to
`backend\undo\trash`, never deleted), settings, reminders and timers,
notes, remembered facts, routines, workspaces, window layouts, clipboard,
volume. Shell commands, typing and integration calls can't be reversed
automatically, and Jarvis tells you so instead of pretending. "What did you
change today?" lists the journal. Backups are kept 7 days.

**Self-check.** After a task that changed something (screen actions, file
writes, shell commands, integration calls), a second skeptical pass looks
at what actually happened, including a fresh screenshot for screen tasks.
If it doesn't look done, Jarvis gets one more attempt with that feedback;
if it still fails, the reply says so plainly. Simple questions skip the
check. Turn it off with `JARVIS_VERIFY=off`.

## People and projects

Tell Jarvis about the people and projects in your life and it keeps a
profile for each: "remember Ali is my teammate on the FarmShift backend",
"I promised Ali the API docs by 5pm" (sets a reminder), "Sara owes me the
designs". Ask "what do I owe Ali?", "who's waiting on me?", "catch me up on
FarmShift" (profile, notes, open promises and this week's activity). "Forget
Ali" deletes everything about him.

## Heads-up (screen-aware help)

Off until you turn it on: say "turn on heads-up" or use the switch in
Settings. Then, while you work:

- An error shows up in a terminal or code editor (Python traceback, npm
  ERR!, TypeScript, Rust, Go, Java, git, failed build or tests, "not
  recognized as a command") and a card explains it in one sentence with
  **Help me fix**, which hands Jarvis the error text.
- A download finishes: **Open** / **Show in folder**.
- An app stops responding: **Force close it**.

It reads text from developer apps only, never screenshots; nothing leaves
the PC until an error appears (then only its last lines go to the model).
Errors already on screen are ignored, each one is offered once, there's a
one-minute cooldown, and it stays quiet while Jarvis is working. "Mute VS
Code", "what was that error?", "turn off heads-up". Run `try-headsup.cmd`
to see it in action.

## Meeting notes

Say "take notes on this call" (or press the waveform button in the panel's
top bar) during a Meet, Zoom, Teams or Discord call. Jarvis transcribes your
mic as "You" and what the PC plays as "Them", locally with Whisper; a red REC
bar shows the time and the latest line. "Stop the meeting" writes a summary,
decisions and action items, saves them to `Documents\Jarvis Meetings`, and
shows a card with **Track action items** (yours become reminders or promises,
other people's become "owed to you"). Audio is never saved. Tell people
they're being transcribed. "What did we decide in the pricing call?" searches
past meetings. For Urdu or mixed calls set `JARVIS_WHISPER_MODEL=small` and
`JARVIS_MEETING_LANGUAGE=auto`.

## Pixel eyes

When an app exposes few accessible controls (games, canvas tools, remote
desktops, some Electron apps), screenshots also number the text and icons
read from the pixels (Windows' built-in OCR, RapidOCR as fallback), so
Jarvis clicks "Export" by its exact position instead of guessing.
`JARVIS_PIXEL_EYES=always|auto|off`.

## Installer

`build-installer.cmd` builds `frontend\release\Jarvis-Setup-<version>.exe`, a
one-click installer for people without Python or Node. On first launch it
downloads a private Python and Jarvis's packages (about 600 MB, a few
minutes), then the setup wizard asks for name, Gemini key, mic test, voice
and privacy choices. Data lives in `%APPDATA%\Jarvis`, so updates never touch
it. Auto-update: put the `latest.yml` and installer from `frontend\release`
on any static host and set `JARVIS_UPDATE_URL` in `%APPDATA%\Jarvis\.env`.
`check-jarvis.cmd` tests screen reading, audio devices, the speech model and
the key.

## The panel

- **Top row**: state, active model, wake word, speak replies, settings, hide.
- **Telemetry**: clock, Lahore weather, CPU, RAM, battery (amber/red when high/low).
- **Core**: the reactor reacts to your voice (cyan) and Jarvis's voice (amber).
- **Chips**: background jobs (with their current step), timers and upcoming reminders; × cancels.
- **Stream**: conversation plus a live trace of every action and what Jarvis saw.
- **Settings** (sliders icon): voice + preview, speed, wake word, follow-up
  listening, interrupting, approval level, your name/city, routines,
  workspaces, activity history, integrations, start with Windows, fresh
  conversation.

While Jarvis drives your mouse the panel shrinks to a strip so it isn't in
the way. It's also invisible to screenshots (so Jarvis doesn't look at
itself); set `JARVIS_HUD_CAPTURABLE=1` in `frontend/.env` if you want to
screenshot it.

---

## Integrations

Three ways to plug anything in, none of which touch Jarvis's core code.
After any change: **Settings > Integrations > Reload**, or say
"Jarvis, reload integrations".

Secrets never go in the JSON: write `${NAME}` there and add `NAME=...` to
`backend/.env`.

### 1. MCP servers (most services)

[Model Context Protocol](https://modelcontextprotocol.io) is the standard
"AI connector" format; GitHub, Notion, Slack, Google Drive, Home Assistant,
Postgres, Playwright, Spotify and hundreds more ship one. Add it to
`backend/integrations.json`:

```jsonc
"mcp_servers": {
  "github": {
    "enabled": true,
    "url": "https://api.githubcopilot.com/mcp/",           // remote server
    "headers": { "Authorization": "Bearer ${GITHUB_TOKEN}" }
  },
  "files": {
    "enabled": true,
    "command": "npx",                                        // local server
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:\\transfer"]
  }
}
```

Options: `confirm` = `"auto"` (default: asks for anything that looks
destructive), `"always"` or `"never"`; `only_tools` = list to expose a
subset; `env`, `cwd`, `timeout`.

Already on: **browser** (Playwright) lets Jarvis use websites by their
structure: fill forms, click links, read pages, far more reliably than
clicking pixels.

### 2. HTTP actions (any API or webhook)

```jsonc
"http_actions": [{
  "enabled": true,
  "name": "lights",
  "description": "Turn a Home Assistant light on or off.",   // the AI reads this
  "method": "POST",
  "url": "http://homeassistant.local:8123/api/services/light/{action}",
  "headers": { "Authorization": "Bearer ${HA_TOKEN}" },
  "body": { "entity_id": "{entity_id}" },
  "params": {
    "action": { "type": "string", "enum": ["turn_on", "turn_off"] },
    "entity_id": { "type": "string" }
  },
  "confirm": false
}]
```

`{param}` placeholders are filled by Jarvis. Use `body_text` instead of
`body` for plain-text APIs (ntfy). Examples for phone notifications, n8n and
Home Assistant are in `integrations.json`, switched off.

### 3. Python plugins (anything you can code)

Drop a `.py` file in `backend/plugins/`. Copy `_template.py` to start;
`dictionary.py` is a working example.

```python
from app.tools import tool

@tool("crypto_price", "Current price of a cryptocurrency. Use for 'what's bitcoin at'.",
      {"coin": {"type": "string"}}, ["coin"])
def crypto_price(coin: str) -> str:
    ...
```

### Drive Jarvis from outside

Other tools can send Jarvis commands (phone shortcuts, Stream Deck, n8n,
scripts, CI):

```powershell
$h = @{ "X-Jarvis-Token" = "<JARVIS_AUTH_TOKEN from backend/.env>" }
# Run a command and get the answer back
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/command -Headers $h `
  -ContentType "application/json" -Body '{"text": "lock my pc", "wait": true}'
# Make Jarvis announce something
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/notify -Headers $h `
  -ContentType "application/json" -Body '{"text": "Build finished"}'
```

Or with no code at all: drop a `.txt` file into `backend/inbox/`. Jarvis
runs it as a command and appends the answer to `backend/inbox/replies.log`.
Point a OneDrive/Dropbox folder there and you can command your PC from your
phone.

---

## Brains (models)

Set in `backend/.env`. `LLM_PROVIDERS` is tried in order; a rate-limited
one is skipped automatically.

- **gemini**: free key from aistudio.google.com. The free tier has a
  *daily* request cap **per model**, so Jarvis discovers every Flash model
  your key can use and rotates through them when one runs out. It also
  remembers which model answered last, so it doesn't waste time retrying
  dead ones after a restart.

To keep requests small and fast, Jarvis only sends the tools a request
needs: a core set, plus groups (files, windows, routines, activity,
workspaces, each integration...) switched on by keywords in what you said
or by the model asking for them.
- **openai**: any OpenAI-compatible API. Recommended free backup:
  **Groq** (console.groq.com, free key):
  ```ini
  LLM_PROVIDERS=gemini,openai,ollama
  OPENAI_BASE_URL=https://api.groq.com/openai/v1
  OPENAI_API_KEY=gsk_...
  OPENAI_MODEL=<a tool-capable model from console.groq.com/docs/models>
  OPENAI_VISION=false
  ```
- **ollama**: fully local and private, but on a CPU-only machine each step
  can take minutes and the model holds several GB of RAM, which can freeze
  everything else. Only add it to `LLM_PROVIDERS` if you have a GPU.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "I can't reach my language model" | Check your internet first (the log says "Can't reach"). Otherwise every Gemini model is out of quota: add a Groq key (above). |
| Everything is slow / PC freezes | Ollama is loaded in RAM. Remove `ollama` from `LLM_PROVIDERS`. |
| Panel says Offline | Backend not running: run `start-jarvis.bat`. Check `backend/logs/console.log`. |
| Mic does nothing | Windows Settings > Privacy > Microphone > allow desktop apps. Settings sheet shows a live mic meter. |
| Voice sounds robotic | The neural voice needs internet; the OS voice is the offline fallback. |
| An integration shows red | Its error is shown under it in Settings; usually a missing token in `.env`. |

Logs: `backend/logs/jarvis.log` (everything), `console.log` (backend
startup), `hud.log` (panel). Every action is recorded in
`backend/agent_log.db`.

## Tests

```powershell
cd backend
venv\Scripts\python -m pip install -r requirements-dev.txt
venv\Scripts\python -m pytest
```

64 tests: providers, failover and Gemini model rotation, the agent loop
(approvals, Stop, dynamic toolsets, self-check retries), undo for every
supported change, background jobs, routine triggers,
the activity timeline, tools and their safety rules, integrations (a real
MCP server over stdio, HTTP actions, plugins), memory. None of them touch
your mouse, keyboard or screen.
