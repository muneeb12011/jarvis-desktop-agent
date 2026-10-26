# Jarvis roadmap

The goal: the desktop assistant people keep running all day, because it does
real work on *their* machine, privately, without them babysitting it.
Most assistants today are a chat box that can call a few APIs, or a
computer-use demo that clicks slowly through one task while you watch.
Jarvis's bet is the layer in between: always on, voice first, owns the
desktop, runs work in parallel, and is extensible by anyone.

## Shipped

| Area | What works today |
|---|---|
| Voice | Wake word, push-to-talk hotkey, local Whisper speech-to-text, neural voice, follow-up listening, interrupting |
| Brain | Native tool calling on Gemini / OpenAI-compatible / Ollama, automatic failover, Gemini model rotation, dynamic toolsets |
| Desktop control | 60+ tools (apps, windows, files, shell, media, power, clipboard, web), UI Automation numbered controls, vision fallback |
| Safety | Approval gate for risky actions, failsafe corner, full action log, rate limits, token auth |
| Proactive | Reminders, timers, battery warning, spoken greeting with weather |
| Background jobs | Parallel delegated tasks with live status, completion announcements, results fed back into chat |
| Routines | Plain-English automations on time, interval, app open, file arrival, web page change, startup, or by name |
| Activity timeline | Private window history: time per app, "what was I doing at 3", search past pages/files/videos |
| Workspaces | Save and restore full window layouts by voice |
| Text actions | Ctrl+Alt+Space on selected text in any app: fix, rewrite, translate, reply, paste back |
| Integrations | MCP servers (local + remote), HTTP actions, Python plugins, inbound API, file inbox |
| Undo | Journal with snapshots for every reversible change; "undo that", "undo all of that", Undo buttons in the panel |
| Self-check | Independent verification pass after changing tasks, one guided retry, honest failure reports |
| People & projects | Profiles, dated notes, promises both ways with automatic reminders, "catch me up on X" linked to the activity timeline |
| Heads-up | Opt-in: spots errors in terminals and editors, finished downloads and frozen apps, offers one line of help and a fix button |
| Pixel eyes | Numbered click targets from OCR text and icon shapes for apps without accessibility data |
| Meeting copilot | Local transcript of mic + call audio, summary, decisions, action items into reminders and promises |
| Installer | One-click Windows installer with private Python bootstrap, setup wizard, data kept across updates, self-test |

## Next

- Code signing for the installer (Windows SmartScreen warns on unsigned apps).
- Speaker names in meetings (diarization) instead of just You / Them.
- A community gallery for routines and plugins.

## Known gaps to fix first
- UI Automation numbered controls need verification on more apps (it
  currently falls back to vision on some windows).
- The text-action hotkey depends on the app supporting Ctrl+C/Ctrl+V.
- Background jobs can't use the GUI by design; GUI-heavy routines run
  through the same single mouse/keyboard thread as you, one step at a time.
