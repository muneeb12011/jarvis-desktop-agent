/**
 * Jarvis HUD renderer.
 *
 * Loaded with a plain <script> tag, so this file must not contain top-level
 * import/export (tsc would emit CommonJS `exports` and crash the page).
 *
 *   1. Reactor     animated core; reacts to your mic and Jarvis's voice
 *   2. Link        WebSocket to the backend with auto-reconnect
 *   3. Stream      conversation, live tool trace, screenshots, plan
 *   4. Telemetry   clock, weather, CPU/RAM/battery, timers and reminders
 *   5. Confirm     approve / deny risky actions
 *   6. Voice out   neural TTS, sentence by sentence; OS voice fallback
 *   7. Voice in    mic + voice-activity detection, wake word, follow-up
 *                  listening and barge-in
 *   8. Settings    voice, speed, listening, approvals, startup
 */

type HudState = "offline" | "idle" | "listening" | "thinking" | "speaking" | "executing" | "error";
type Layout = "panel" | "mini";

interface HudBridge {
  config(): Promise<{ token: string; backend: string }>;
  setLayout(l: Layout): Promise<Layout>;
  hide(): void;
  show(): void;
  notify(title: string, body: string): void;
  openConfig(which: "integrations" | "plugins"): void;
  openExternal(url: string): void;
  onLayout(cb: (l: Layout) => void): void;
  onListen(cb: () => void): void;
  onStop(cb: () => void): void;
  onTextAction(cb: () => void): void;
}

// Named `bridge`, not `hud`: preload exposes a non-configurable global `hud`, and a
// top-level `const hud` would throw "Identifier has already been declared".
const bridge = (window as unknown as { hud: HudBridge }).hud;

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const el = {
  stateWord: $<HTMLSpanElement>("stateWord"),
  brainName: $<HTMLSpanElement>("brainName"),
  caption: $<HTMLParagraphElement>("caption"),
  heard: $<HTMLParagraphElement>("heard"),
  stream: $<HTMLElement>("stream"),
  timers: $<HTMLDivElement>("timers"),
  clock: $<HTMLSpanElement>("clock"),
  weather: $<HTMLSpanElement>("weather"),
  cpu: $<HTMLSpanElement>("cpuMeter"),
  ram: $<HTMLSpanElement>("ramMeter"),
  bat: $<HTMLSpanElement>("batMeter"),
  confirm: $<HTMLElement>("confirm"),
  confirmReason: $<HTMLParagraphElement>("confirmReason"),
  confirmTimer: $<HTMLDivElement>("confirmTimer"),
  allowBtn: $<HTMLButtonElement>("allowBtn"),
  denyBtn: $<HTMLButtonElement>("denyBtn"),
  composer: $<HTMLFormElement>("composer"),
  input: $<HTMLInputElement>("input"),
  micBtn: $<HTMLButtonElement>("micBtn"),
  stopBtn: $<HTMLButtonElement>("stopBtn"),
  wakeBtn: $<HTMLButtonElement>("wakeBtn"),
  voiceBtn: $<HTMLButtonElement>("voiceBtn"),
  settingsBtn: $<HTMLButtonElement>("settingsBtn"),
  hideBtn: $<HTMLButtonElement>("hideBtn"),
  settings: $<HTMLElement>("settings"),
  settingsClose: $<HTMLButtonElement>("settingsClose"),
  voiceSelect: $<HTMLSelectElement>("voiceSelect"),
  previewBtn: $<HTMLButtonElement>("previewBtn"),
  rateGroup: $<HTMLDivElement>("rateGroup"),
  confirmGroup: $<HTMLDivElement>("confirmGroup"),
  optWake: $<HTMLInputElement>("optWake"),
  optFollow: $<HTMLInputElement>("optFollow"),
  optBarge: $<HTMLInputElement>("optBarge"),
  optStartup: $<HTMLInputElement>("optStartup"),
  optTitle: $<HTMLInputElement>("optTitle"),
  optCity: $<HTMLInputElement>("optCity"),
  micLevel: $<HTMLElement>("micLevel"),
  micState: $<HTMLElement>("micState"),
  newSessionBtn: $<HTMLButtonElement>("newSessionBtn"),
  integrationList: $<HTMLUListElement>("integrationList"),
  reloadIntegrationsBtn: $<HTMLButtonElement>("reloadIntegrationsBtn"),
  openIntegrationsBtn: $<HTMLButtonElement>("openIntegrationsBtn"),
  openPluginsBtn: $<HTMLButtonElement>("openPluginsBtn"),
  routineList: $<HTMLUListElement>("routineList"),
  workspaceList: $<HTMLUListElement>("workspaceList"),
  optActivity: $<HTMLInputElement>("optActivity"),
  optHeadsup: $<HTMLInputElement>("optHeadsup"),
  meetBtn: $<HTMLButtonElement>("meetBtn"),
  meetingBar: $<HTMLElement>("meetingBar"),
  meetingTitle: $<HTMLElement>("meetingTitle"),
  meetingClock: $<HTMLElement>("meetingClock"),
  meetingStop: $<HTMLButtonElement>("meetingStop"),
  meetingLast: $<HTMLElement>("meetingLast"),
  headsupMuted: $<HTMLElement>("headsupMuted"),
  clearActivityBtn: $<HTMLButtonElement>("clearActivityBtn"),
  textSheet: $<HTMLElement>("textSheet"),
  textSheetTitle: $<HTMLElement>("textSheetTitle"),
  textSheetClose: $<HTMLButtonElement>("textSheetClose"),
  selectionPreview: $<HTMLElement>("selectionPreview"),
  textActions: $<HTMLDivElement>("textActions"),
  customActionForm: $<HTMLFormElement>("customActionForm"),
  customAction: $<HTMLInputElement>("customAction"),
  textResult: $<HTMLDivElement>("textResult"),
  textResultText: $<HTMLElement>("textResultText"),
  copyResultBtn: $<HTMLButtonElement>("copyResultBtn"),
  replaceBtn: $<HTMLButtonElement>("replaceBtn"),
  miniStep: $<HTMLSpanElement>("miniStep"),
  miniTitle: $<HTMLElement>("miniTitle"),
  miniStop: $<HTMLButtonElement>("miniStop"),
  miniExpand: $<HTMLButtonElement>("miniExpand"),
  reactor: $<HTMLCanvasElement>("reactor"),
  miniReactor: $<HTMLCanvasElement>("miniReactor"),
};

// ------------------------------------------------------------------ local prefs

const prefs = {
  get(key: string, fallback: boolean): boolean {
    try {
      const v = localStorage.getItem(`jarvis.${key}`);
      return v === null ? fallback : v === "1";
    } catch {
      return fallback;
    }
  },
  set(key: string, value: boolean) {
    try { localStorage.setItem(`jarvis.${key}`, value ? "1" : "0"); } catch { /* storage unavailable */ }
  },
};

let speakReplies = prefs.get("speak", true);
let followUp = prefs.get("follow", true);
let bargeIn = prefs.get("barge", true);
let wakeEnabled = false;
let sttEngine = "none";

// ------------------------------------------------------------------ state

let state: HudState = "offline";
let busy = false;
let layout: Layout = "panel";
let lastRequestWasVoice = false;

const STATE_WORDS: Record<HudState, string> = {
  offline: "Offline",
  idle: "Standing by",
  listening: "Listening",
  thinking: "Thinking",
  speaking: "Speaking",
  executing: "Working",
  error: "Problem",
};

function setState(next: HudState, caption?: string) {
  state = next;
  document.body.className = document.body.className.replace(/\bstate-\w+/g, "").trim() + ` state-${next}`;
  el.stateWord.textContent = wakeEnabled && next === "idle" ? "Say “Jarvis”" : STATE_WORDS[next];
  if (caption !== undefined) el.caption.textContent = caption;
}

function setBusy(b: boolean) {
  busy = b;
  el.stopBtn.hidden = !b;
  el.micBtn.hidden = b;
}

async function setLayout(l: Layout) {
  if (l === layout) return;
  layout = l;
  await bridge.setLayout(l);
}

bridge.onLayout((l) => {
  layout = l;
  document.body.classList.toggle("layout-mini", l === "mini");
  document.body.classList.toggle("layout-panel", l === "panel");
});

// ================================================================== 1. Reactor

const levels = { mic: 0, voice: 0 };
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const COLORS: Record<HudState, [number, number, number]> = {
  offline: [111, 138, 150],
  idle: [92, 225, 230],
  listening: [92, 225, 230],
  thinking: [255, 181, 71],
  speaking: [255, 181, 71],
  executing: [255, 181, 71],
  error: [255, 93, 93],
};
const tint = { r: 92, g: 225, b: 230, energy: 0 };

function drawReactor(canvas: HTMLCanvasElement, t: number, compact: boolean) {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const w = canvas.width;
  const c = w / 2;
  ctx.clearRect(0, 0, w, w);
  const rgb = (a: number) => `rgba(${tint.r | 0},${tint.g | 0},${tint.b | 0},${a})`;
  const speed = { offline: 0.02, idle: 0.1, listening: 0.18, thinking: 0.9, speaking: 0.25, executing: 0.6, error: 0.05 }[state];
  const spin = reduceMotion ? 0 : t * speed;
  const e = tint.energy;

  const ticks = compact ? 36 : 72;
  const rOuter = c * 0.92;
  ctx.lineCap = "round";
  for (let i = 0; i < ticks; i++) {
    const a = (i / ticks) * Math.PI * 2 + spin * 0.25;
    const wobble = Math.sin(i * 1.7 + t * 6) * 0.5 + 0.5;
    const len = c * (0.035 + e * 0.13 * wobble);
    ctx.strokeStyle = rgb(0.25 + 0.55 * (i % 6 === 0 ? 1 : wobble * e + 0.2));
    ctx.lineWidth = compact ? 2 : 2.4;
    ctx.beginPath();
    ctx.moveTo(c + Math.cos(a) * (rOuter - len), c + Math.sin(a) * (rOuter - len));
    ctx.lineTo(c + Math.cos(a) * rOuter, c + Math.sin(a) * rOuter);
    ctx.stroke();
  }

  const rMid = c * 0.7;
  ctx.lineWidth = compact ? 4 : 6;
  for (let k = 0; k < 3; k++) {
    const start = -spin + (k * Math.PI * 2) / 3;
    ctx.strokeStyle = rgb(0.8);
    ctx.beginPath();
    ctx.arc(c, c, rMid, start, start + Math.PI * (0.38 + (state === "thinking" ? 0.2 * Math.sin(t * 3) : 0)));
    ctx.stroke();
  }

  const rIn = c * 0.52;
  ctx.lineWidth = 1.5;
  ctx.strokeStyle = rgb(0.35);
  ctx.beginPath();
  ctx.arc(c, c, rIn, 0, Math.PI * 2);
  ctx.stroke();
  if (state === "executing" && !reduceMotion) {
    const a = t * 2.4;
    ctx.strokeStyle = rgb(0.9);
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.arc(c, c, rIn, a, a + 0.6);
    ctx.stroke();
  }

  const coreR = c * (0.28 + e * 0.1 + (state === "idle" && !reduceMotion ? Math.sin(t * 1.4) * 0.01 : 0));
  const g = ctx.createRadialGradient(c, c, 0, c, c, coreR * 1.8);
  g.addColorStop(0, "rgba(255,255,255,0.95)");
  g.addColorStop(0.25, rgb(0.9));
  g.addColorStop(0.6, rgb(0.25));
  g.addColorStop(1, rgb(0));
  ctx.fillStyle = g;
  ctx.beginPath();
  ctx.arc(c, c, coreR * 1.8, 0, Math.PI * 2);
  ctx.fill();
}

function frame(ms: number) {
  const t = ms / 1000;
  const target = COLORS[state];
  tint.r += (target[0] - tint.r) * 0.08;
  tint.g += (target[1] - tint.g) * 0.08;
  tint.b += (target[2] - tint.b) * 0.08;
  const live = state === "listening" ? levels.mic : state === "speaking" ? levels.voice : state === "thinking" ? 0.25 : 0.05;
  tint.energy += (Math.min(1, live) - tint.energy) * 0.25;
  if (layout === "panel") {
    if (!document.body.classList.contains("settings-open")) drawReactor(el.reactor, t, false);
    el.micLevel.style.width = `${Math.round(Math.min(1, levels.mic) * 100)}%`;
  } else {
    drawReactor(el.miniReactor, t, true);
  }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// ================================================================== 2. Link

let ws: WebSocket | null = null;
let cfg = { token: "", backend: "http://127.0.0.1:8000" };
let retryMs = 1000;

function send(payload: object): boolean {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(payload));
    return true;
  }
  addMsg("error", "I'm not connected to my backend yet. If this persists, run start-jarvis.bat.");
  return false;
}

function connect() {
  const url = cfg.backend.replace(/^http/, "ws") + `/ws/agent?token=${encodeURIComponent(cfg.token)}`;
  const sock = new WebSocket(url);
  ws = sock;
  sock.onopen = () => { retryMs = 1000; setState("idle", "Ready"); };
  sock.onmessage = (ev) => handle(JSON.parse(ev.data));
  sock.onclose = (ev) => {
    setBusy(false);
    if (ev.code === 1008) {
      setState("error", "Token rejected. JARVIS_AUTH_TOKEN must match in backend/.env and frontend/.env.");
      return;
    }
    setState("offline", "Waiting for my backend");
    setTimeout(connect, retryMs);
    retryMs = Math.min(retryMs * 2, 8000);
  };
}

setInterval(() => { if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping" })); }, 20000);

// ================================================================== 3. Stream

const traces = new Map<string, HTMLElement>();
let planEl: HTMLOListElement | null = null;

const TOOL_LABELS: Record<string, string> = {
  open_app: "Opening", close_app: "Closing", screenshot: "Looking at the screen", click_element: "Clicking",
  click: "Clicking", type_text: "Typing", press_keys: "Pressing", scroll: "Scrolling", wait: "Waiting",
  web_search: "Searching the web", read_webpage: "Reading", open_url: "Opening", play_youtube: "Finding on YouTube",
  run_powershell: "Running", weather: "Checking the weather", remember: "Remembering", recall: "Recalling",
  set_reminder: "Setting a reminder", set_timer: "Starting a timer", system_status: "Checking the system",
  update_plan: "Planning", find_files: "Searching files", list_dir: "Looking in", read_file: "Reading",
  write_file: "Writing", media_control: "Media", set_volume: "Volume", focus_window: "Switching to",
  window_action: "Window", daily_briefing: "Preparing your briefing", news_headlines: "Fetching headlines",
  save_screenshot: "Saving a screenshot", jarvis_settings: "Adjusting my settings", take_note: "Noting",
  get_datetime: "Checking the time", clipboard_read: "Reading the clipboard", set_brightness: "Brightness",
  self_check: "Double-checking the result", enable_tools: "Loading abilities", undo_last: "Undoing",
  undo_last_request: "Undoing the last request", start_background_job: "Starting a background job",
  create_routine: "Saving a routine", save_workspace: "Saving workspace", restore_workspace: "Restoring workspace",
  activity_report: "Checking your activity", activity_search: "Searching your history",
};

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]!);
}

function lightMarkdown(s: string): string {
  return escapeHtml(s)
    .replace(/```(?:\w+)?\n?([\s\S]*?)```/g, "<code>$1</code>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

function captionOf(text: string): string {
  const line = text.replace(/```[\s\S]*?(```|$)/g, " ").split("\n").map((l) => l.trim()).find(Boolean) ?? "";
  return line.replace(/[*_`#>]/g, "").slice(0, 140);
}

function scrollDown() {
  el.stream.scrollTop = el.stream.scrollHeight;
}

function addMsg(kind: "you" | "jarvis" | "reminder" | "error" | "notice", text: string, faded = false) {
  const div = document.createElement("div");
  div.className = `msg ${kind}`;
  if (faded) div.style.opacity = "0.7";
  div.innerHTML = lightMarkdown(text);
  el.stream.appendChild(div);
  while (el.stream.childElementCount > 160) el.stream.firstElementChild?.remove();
  scrollDown();
}

function describeArgs(name: string, args: Record<string, unknown>): string {
  if (name === "click_element") return `control [${args.id}]`;
  if (name === "set_timer") return `${args.label ?? ""} ${args.minutes ? args.minutes + " min" : ""}${args.seconds ? " " + args.seconds + " s" : ""}`.trim();
  const pick = args.name ?? args.query ?? args.topic ?? args.url ?? args.title ?? args.text ?? args.command ??
    args.path ?? args.fact ?? args.city ?? (args.keys as unknown) ?? args.id ?? args.action ?? "";
  const s = Array.isArray(pick) ? pick.join("+") : String(pick);
  return s.length > 90 ? s.slice(0, 90) + "..." : s;
}

function addTrace(id: string, name: string, args: Record<string, unknown>) {
  const row = document.createElement("div");
  row.className = "trace";
  const label = TOOL_LABELS[name] ?? name.replace(/_/g, " ");
  row.innerHTML = `<span class="dot"></span><span><span class="name">${escapeHtml(label)}</span> ` +
    `${escapeHtml(describeArgs(name, args))}<span class="detail"></span></span>`;
  el.stream.appendChild(row);
  traces.set(id, row);
  el.miniStep.textContent = `${label} ${describeArgs(name, args)}`.trim();
  scrollDown();
}

function finishTrace(id: string, ok: boolean, summary: string, undoId?: number | null) {
  const row = traces.get(id);
  if (!row) return;
  row.classList.add(ok ? "ok" : "fail");
  const detail = row.querySelector(".detail") as HTMLElement;
  detail.textContent = summary.split("\n")[0];
  detail.title = summary;
  if (undoId) {
    const btn = document.createElement("button");
    btn.className = "undo";
    btn.textContent = "Undo";
    btn.title = "Reverse this change";
    btn.dataset.undo = String(undoId);
    row.appendChild(btn);
  }
  traces.delete(id);
}

el.stream.addEventListener("click", (e) => {
  const btn = (e.target as HTMLElement).closest("button.undo") as HTMLButtonElement | null;
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  btn.textContent = "Undoing";
  send({ type: "action", name: "undo", args: { id: Number(btn.dataset.undo) } });
});

function addSight(b64: string) {
  const prev = el.stream.querySelectorAll(".sight");
  if (prev.length > 2) prev[0].remove();
  const fig = document.createElement("figure");
  fig.className = "sight";
  const img = new Image();
  img.src = `data:image/jpeg;base64,${b64}`;
  img.alt = "What Jarvis saw";
  fig.appendChild(img);
  el.stream.appendChild(fig);
  scrollDown();
}

function showPlan(steps: string[], current: number) {
  if (!planEl || !planEl.isConnected) {
    planEl = document.createElement("ol");
    planEl.className = "plan";
    el.stream.appendChild(planEl);
  }
  planEl.innerHTML = steps
    .map((s, i) => `<li class="${i < current ? "done" : i === current ? "now" : ""}">${escapeHtml(s)}</li>`)
    .join("");
  scrollDown();
}

// ================================================================== 4. Telemetry

interface JobView { id: string; title: string; status: string; step: string; steps: number; ended: number | null }
const jobs = new Map<string, JobView>();

interface Reminder { id: number; text: string; due_at: number; kind: string; label: string }
let reminders: Reminder[] = [];

function tickClock() {
  const now = new Date();
  el.clock.textContent = now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  renderTimers();
}
setInterval(tickClock, 1000);
tickClock();

function setMeter(node: HTMLElement, pct: number, hot: boolean, crit: boolean) {
  const bar = node.querySelector("s") as HTMLElement;
  const label = node.querySelector("em") as HTMLElement;
  bar.style.width = `${Math.max(2, Math.min(100, pct))}%`;
  label.textContent = `${Math.round(pct)}%`;
  node.classList.toggle("hot", hot && !crit);
  node.classList.toggle("crit", crit);
}

function applyStats(m: any) {
  if (typeof m.cpu === "number") setMeter(el.cpu, m.cpu, m.cpu >= 70, m.cpu >= 90);
  if (typeof m.ram === "number") setMeter(el.ram, m.ram, m.ram >= 75, m.ram >= 90);
  if (typeof m.battery === "number") {
    el.bat.hidden = false;
    setMeter(el.bat, m.battery, !m.plugged && m.battery <= 30, !m.plugged && m.battery <= 15);
    el.bat.title = m.plugged ? "Battery, charging" : "Battery";
  }
  if (m.weather) {
    el.weather.hidden = false;
    const match = /^(-?\d+) degrees and (.*)$/.exec(m.weather);
    el.weather.textContent = match ? `${match[1]}°` : m.weather;
    el.weather.title = `${m.weather} in ${m.city ?? ""}`;
  }
}

function fmtCountdown(sec: number): string {
  if (sec >= 3600) return `${Math.floor(sec / 3600)}h ${String(Math.floor((sec % 3600) / 60)).padStart(2, "0")}m`;
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
}

function visibleJobs(): JobView[] {
  const now = Date.now() / 1000;
  return [...jobs.values()].filter((j) => j.status === "running" || j.status === "queued" || (j.ended && now - j.ended < 45));
}

function renderTimers() {
  const now = Date.now() / 1000;
  const soon = reminders.filter((r) => r.due_at - now < 36 * 3600).slice(0, 4);
  const vj = visibleJobs();
  const key = soon.map((r) => r.id).join(",") + "|" + vj.map((j) => `${j.id}:${j.status}:${j.steps}`).join(",");
  if (el.timers.dataset.key !== key) {
    el.timers.dataset.key = key;
    el.timers.innerHTML = vj.map((j) =>
      `<span class="chip job ${escapeHtml(j.status)}" data-job="${escapeHtml(j.id)}" title="Background job">` +
      `<span class="label">${escapeHtml(j.title.slice(0, 26))}</span>` +
      `<span class="when">${escapeHtml(j.status === "running" ? (j.step || "starting") : j.status)}</span>` +
      (j.status === "running" || j.status === "queued" ? `<button title="Cancel job" aria-label="Cancel job">×</button>` : "") +
      `</span>`).join("") + soon.map((r) =>
      `<span class="chip ${r.kind === "timer" ? "timer" : "reminder"}" data-id="${r.id}">` +
      `<span class="label">${escapeHtml(r.kind === "timer" ? r.label : r.text).slice(0, 28)}</span>` +
      `<span class="when"></span><button title="Cancel" aria-label="Cancel">×</button></span>`).join("");
  }
  for (const r of soon) {
    const chip = el.timers.querySelector(`[data-id="${r.id}"]`) as HTMLElement | null;
    if (!chip) continue;
    const left = Math.max(0, Math.round(r.due_at - now));
    const when = chip.querySelector(".when") as HTMLElement;
    when.textContent = r.kind === "timer" || left < 3600
      ? fmtCountdown(left)
      : new Date(r.due_at * 1000).toLocaleTimeString([], { weekday: left > 18 * 3600 ? "short" : undefined, hour: "numeric", minute: "2-digit" });
    chip.classList.toggle("ending", left <= 10);
  }
}

el.timers.addEventListener("click", (e) => {
  const btn = (e.target as HTMLElement).closest("button");
  const chip = btn?.closest(".chip") as HTMLElement | null;
  if (!chip) return;
  if (chip.dataset.job) send({ type: "action", name: "cancel_job", args: { id: chip.dataset.job } });
  else send({ type: "cancel_reminder", id: Number(chip.dataset.id) });
});

// ================================================================== 5. Confirm

let confirmId: string | null = null;
let confirmAnim: Animation | null = null;

function openConfirm(id: string, reason: string, timeout: number) {
  confirmId = id;
  el.confirmReason.textContent = reason;
  el.confirm.hidden = false;
  closeSettings();
  void setLayout("panel");
  bridge.show();
  confirmAnim?.cancel();
  confirmAnim = el.confirmTimer.animate([{ transform: "scaleX(1)" }, { transform: "scaleX(0)" }],
    { duration: timeout * 1000, easing: "linear", fill: "forwards" });
  el.allowBtn.focus();
  speak(reason.split("\n")[0]);
}

function closeConfirm(id?: string) {
  if (id && id !== confirmId) return;
  confirmId = null;
  el.confirm.hidden = true;
  confirmAnim?.cancel();
}

function answerConfirm(approved: boolean) {
  if (!confirmId) return;
  send({ type: "confirm", id: confirmId, approved });
  addMsg("notice", approved ? "Allowed." : "Declined.");
  closeConfirm();
}

el.allowBtn.addEventListener("click", () => answerConfirm(true));
el.denyBtn.addEventListener("click", () => answerConfirm(false));

// ================================================================== 6. Voice out

const player = new Audio();
let playCtx: AudioContext | null = null;
let playAnalyser: AnalyserNode | null = null;
let speaking = false;
let speechGen = 0; // bumps on every stop so stale audio never plays
let quietUntil = 0;
const sentenceQueue: string[] = [];
let afterSpeech: (() => void) | null = null;

function ensurePlaybackGraph() {
  if (playCtx) return;
  playCtx = new AudioContext();
  const srcNode = playCtx.createMediaElementSource(player);
  playAnalyser = playCtx.createAnalyser();
  playAnalyser.fftSize = 512;
  srcNode.connect(playAnalyser);
  playAnalyser.connect(playCtx.destination);
  const buf = new Uint8Array(playAnalyser.frequencyBinCount);
  const tick = () => {
    if (playAnalyser && !player.paused) {
      playAnalyser.getByteTimeDomainData(buf);
      let sum = 0;
      for (const v of buf) sum += ((v - 128) / 128) ** 2;
      levels.voice = Math.min(1, Math.sqrt(sum / buf.length) * 4);
    } else {
      levels.voice *= 0.85;
    }
    requestAnimationFrame(tick);
  };
  tick();
}

function splitSentences(text: string): string[] {
  const clean = text.replace(/```[\s\S]*?```/g, " I've put the code on screen. ").replace(/\s+/g, " ").trim();
  const parts = clean.match(/[^.!?]+(?:[.!?]+["')\]]*|$)/g) ?? [clean];
  // Merge tiny fragments so we don't make a network round trip per "Sir."
  const out: string[] = [];
  for (const p of parts.map((s) => s.trim()).filter(Boolean)) {
    if (out.length && (out[out.length - 1].length < 40 || p.length < 12)) out[out.length - 1] += " " + p;
    else out.push(p);
  }
  return out;
}

/** Speak `text`. `then` runs once Jarvis has finished talking (used for follow-up listening). */
function speak(text: string, then?: () => void) {
  if (!speakReplies || !text.trim()) {
    then?.();
    return;
  }
  sentenceQueue.push(...splitSentences(text));
  if (then) afterSpeech = then;
  if (!speaking) void drainSpeech();
}

function stopSpeaking() {
  speechGen++;
  sentenceQueue.length = 0;
  afterSpeech = null;
  player.pause();
  window.speechSynthesis?.cancel();
}

async function fetchTts(text: string, voice?: string, rate?: string): Promise<Blob | null> {
  try {
    const resp = await fetch(`${cfg.backend}/tts`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Jarvis-Token": cfg.token },
      body: JSON.stringify({ text, voice, rate }),
    });
    if (!resp.ok) return null;
    const blob = await resp.blob();
    return blob.size ? blob : null;
  } catch {
    return null;
  }
}

async function drainSpeech() {
  speaking = true;
  const gen = speechGen;
  if (state !== "executing") setState("speaking");
  // Pipeline: synthesise the next sentence while the current one plays.
  let next: Promise<Blob | null> | null = sentenceQueue.length ? fetchTts(sentenceQueue[0]) : null;
  while (sentenceQueue.length && gen === speechGen) {
    const sentence = sentenceQueue.shift()!;
    const blob = await next;
    next = sentenceQueue.length ? fetchTts(sentenceQueue[0]) : null;
    if (gen !== speechGen) break;
    if (blob) await playBlob(blob);
    else await speakSystem(sentence);
  }
  speaking = false;
  quietUntil = performance.now() + 350;
  if (state === "speaking" || (!busy && state === "thinking")) setState(busy ? "thinking" : "idle");
  const then = afterSpeech;
  afterSpeech = null;
  if (gen === speechGen && then) then();
}

async function playBlob(blob: Blob): Promise<void> {
  ensurePlaybackGraph();
  await playCtx?.resume();
  const url = URL.createObjectURL(blob);
  player.src = url;
  await new Promise<void>((resolve) => {
    player.onended = player.onerror = player.onpause = () => resolve();
    player.play().catch(() => resolve());
  });
  URL.revokeObjectURL(url);
}

function speakSystem(text: string): Promise<void> {
  return new Promise((resolve) => {
    if (!("speechSynthesis" in window)) return resolve();
    const u = new SpeechSynthesisUtterance(text.replace(/[*_`#]/g, ""));
    const voices = speechSynthesis.getVoices();
    u.voice = voices.find((v) => /en-GB/i.test(v.lang) && /male|george|ryan|daniel/i.test(v.name)) ??
      voices.find((v) => /en-GB/i.test(v.lang)) ?? null;
    u.rate = 1.03;
    let fake = 0;
    const pulse = setInterval(() => { fake += 0.35; levels.voice = 0.35 + Math.abs(Math.sin(fake)) * 0.4; }, 60);
    u.onend = u.onerror = () => { clearInterval(pulse); levels.voice = 0; resolve(); };
    speechSynthesis.speak(u);
  });
}

// ================================================================== 7. Voice in

const WAKE = /^\W*(?:(?:hey|hi|ok|okay|yo)\W+)?(?:jarvis|jarvas|jervis|javis|jarviss|jarvi)\b\W*/i;
const STOP_WORDS = /^\W*(?:(?:jarvis\W+)?(?:stop|wait|hold on|enough|cancel|quiet|shut up|never ?mind|that'?s enough))\b/i;

type Ear = "off" | "wake" | "command";
let ear: Ear = "off";
let commandDeadline = 0;
let micCtx: AudioContext | null = null;
let micStream: MediaStream | null = null;
let micNode: ScriptProcessorNode | null = null;

const vad = {
  floor: 0,
  inSpeech: false,
  loudFrames: 0,
  silenceMs: 0,
  speechMs: 0,
  barge: false,
  preroll: [] as Float32Array[],
  chunks: [] as Float32Array[],
};

async function startMic(): Promise<boolean> {
  if (micCtx) return true;
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (err) {
    addMsg("error", `I can't use the microphone: ${(err as Error).message}. Check Windows Settings > Privacy > Microphone.`);
    el.micState.textContent = "blocked";
    return false;
  }
  micCtx = new AudioContext({ sampleRate: 16000 });
  const src = micCtx.createMediaStreamSource(micStream);
  micNode = micCtx.createScriptProcessor(1024, 1, 1);
  micNode.onaudioprocess = (e) => onAudio(new Float32Array(e.inputBuffer.getChannelData(0)));
  src.connect(micNode);
  micNode.connect(micCtx.destination);
  el.micState.textContent = "on";
  return true;
}

function stopMic() {
  micNode?.disconnect();
  micStream?.getTracks().forEach((t) => t.stop());
  void micCtx?.close();
  micCtx = null;
  micStream = null;
  micNode = null;
  levels.mic = 0;
  el.micState.textContent = "off";
  resetVad();
}

function resetVad() {
  vad.inSpeech = false;
  vad.loudFrames = 0;
  vad.silenceMs = 0;
  vad.speechMs = 0;
  vad.barge = false;
  vad.chunks = [];
}

function onAudio(buf: Float32Array) {
  if (!micCtx) return;
  const frameMs = (buf.length / micCtx.sampleRate) * 1000;
  let sum = 0;
  for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
  const rms = Math.sqrt(sum / buf.length);

  // While Jarvis talks, only a clearly louder voice counts (barge-in), so his own
  // voice leaking from the speakers isn't mistaken for you.
  const talking = speaking || performance.now() < quietUntil;
  if (talking && !(bargeIn && wakeEnabled)) {
    levels.mic *= 0.8;
    resetVad();
    return;
  }
  if (ear === "command" && !vad.inSpeech && !talking && performance.now() > commandDeadline) endCommandWindow();

  if (!talking) {
    vad.floor = vad.floor === 0 ? rms : rms < vad.floor ? vad.floor * 0.9 + rms * 0.1 : vad.floor * 0.997 + rms * 0.003;
  }
  const threshold = talking ? Math.max(0.05, vad.floor * 6) : Math.max(0.012, vad.floor * 3.2);
  levels.mic = ear === "command" || vad.inSpeech ? Math.min(1, rms * 9) : Math.min(0.5, rms * 6);

  if (!vad.inSpeech) {
    vad.preroll.push(buf);
    if (vad.preroll.length > 6) vad.preroll.shift();
    vad.loudFrames = rms > threshold ? vad.loudFrames + 1 : 0;
    if (vad.loudFrames >= (talking ? 3 : 2)) {
      vad.inSpeech = true;
      vad.barge = talking;
      vad.chunks = [...vad.preroll];
      vad.preroll = [];
      vad.speechMs = 0;
      vad.silenceMs = 0;
      if (ear === "command" && !talking) setState("listening");
    }
    return;
  }

  vad.chunks.push(buf);
  vad.speechMs += frameMs;
  vad.silenceMs = rms < threshold * 0.75 ? vad.silenceMs + frameMs : 0;
  const maxMs = ear === "command" ? 20000 : 8000;
  if (vad.silenceMs > (ear === "command" ? 900 : 700) || vad.speechMs > maxMs) {
    const chunks = vad.chunks;
    const spoken = vad.speechMs - vad.silenceMs;
    const barge = vad.barge;
    resetVad();
    if (spoken > 350) void transcribe(chunks, micCtx.sampleRate, barge ? "barge" : ear);
  }
}

function encodeWav(chunks: Float32Array[], rate: number): Blob {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const buffer = new ArrayBuffer(44 + total * 2);
  const v = new DataView(buffer);
  const str = (o: number, s: string) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, "RIFF"); v.setUint32(4, 36 + total * 2, true); str(8, "WAVE"); str(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, "data"); v.setUint32(40, total * 2, true);
  let o = 44;
  for (const c of chunks) {
    for (let i = 0; i < c.length; i++, o += 2) {
      const s = Math.max(-1, Math.min(1, c[i]));
      v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
  }
  return new Blob([buffer], { type: "audio/wav" });
}

let sttInFlight = 0;

async function transcribe(chunks: Float32Array[], rate: number, mode: Ear | "barge") {
  if (mode === "command") setState("thinking", "Transcribing");
  sttInFlight++;
  let text = "";
  try {
    const resp = await fetch(`${cfg.backend}/stt`, {
      method: "POST",
      headers: { "Content-Type": "audio/wav", "X-Jarvis-Token": cfg.token },
      body: encodeWav(chunks, rate),
    });
    if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail ?? `HTTP ${resp.status}`);
    text = ((await resp.json()).text ?? "").trim();
  } catch (err) {
    if (mode === "command") addMsg("error", `Speech recognition failed: ${(err as Error).message}`);
  } finally {
    sttInFlight--;
  }

  if (mode === "barge") {
    // Only act if you actually addressed Jarvis or told him to stop.
    if (!text) return;
    if (STOP_WORDS.test(text)) {
      stopSpeaking();
      if (busy) send({ type: "stop" });
      setState("idle", "Stopped.");
      return;
    }
    if (WAKE.test(text)) {
      stopSpeaking();
      const command = text.replace(WAKE, "").trim();
      if (command.length > 2) submitText(command, true);
      else void openCommandWindow();
    }
    return;
  }

  if (mode === "command") {
    endCommandWindow();
    if (text && STOP_WORDS.test(text) && text.split(/\s+/).length <= 3) {
      setState("idle", "Okay.");
      return;
    }
    if (text) submitText(text, true);
    else setState(busy ? "thinking" : "idle", "I didn't catch that.");
    return;
  }

  if (!text || !WAKE.test(text)) return;
  const command = text.replace(WAKE, "").trim();
  if (command.length > 2) {
    chime(880);
    submitText(command, true);
  } else {
    void openCommandWindow();
  }
}

function chime(freq: number) {
  try {
    const c = new AudioContext();
    const o = c.createOscillator();
    const g = c.createGain();
    o.frequency.value = freq;
    o.type = "sine";
    g.gain.setValueAtTime(0.0001, c.currentTime);
    g.gain.exponentialRampToValueAtTime(0.1, c.currentTime + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, c.currentTime + 0.2);
    o.connect(g).connect(c.destination);
    o.start();
    o.stop(c.currentTime + 0.22);
    setTimeout(() => void c.close(), 400);
  } catch { /* audio unavailable */ }
}

async function openCommandWindow(opts: { quiet?: boolean; seconds?: number } = {}) {
  if (sttEngine === "none") {
    addMsg("error", "No speech recognition engine. Run setup-jarvis.bat (installs Whisper) or set GEMINI_API_KEY.");
    return;
  }
  stopSpeaking();
  if (!(await startMic())) return;
  ear = "command";
  commandDeadline = performance.now() + (opts.seconds ?? 7) * 1000;
  el.micBtn.classList.add("live");
  if (!opts.quiet) chime(660);
  setState("listening", opts.quiet ? "Go on" : "Listening");
}

function endCommandWindow() {
  if (ear !== "command") return;
  ear = wakeEnabled ? "wake" : "off";
  el.micBtn.classList.remove("live");
  if (ear === "off" && sttInFlight === 0) stopMic();
  if (state === "listening") setState(busy ? "thinking" : "idle", "");
}

async function applyWake(on: boolean) {
  wakeEnabled = on;
  prefs.set("wake", on);
  el.wakeBtn.setAttribute("aria-pressed", String(on));
  el.optWake.checked = on;
  if (on) {
    if (await startMic()) ear = ear === "command" ? "command" : "wake";
    else void applyWake(false);
  } else if (ear !== "command") {
    ear = "off";
    stopMic();
  }
  if (state === "idle") setState("idle");
}

// ================================================================== 8. Settings

let serverSettings: any = null;

const CONFIRM_LABELS: [string, string][] = [["strict", "Every change"], ["normal", "Risky actions"], ["off", "Never"]];
const RATE_LABELS: Record<string, string> = { slower: "Slower", normal: "Normal", brisk: "Brisk", fast: "Fast" };

function segmented(group: HTMLElement, options: [string, string][], current: string, onPick: (v: string) => void) {
  group.innerHTML = options.map(([v, label]) =>
    `<button type="button" role="radio" data-v="${escapeHtml(v)}" aria-checked="${v === current}">${escapeHtml(label)}</button>`).join("");
  group.onclick = (e) => {
    const b = (e.target as HTMLElement).closest("button") as HTMLButtonElement | null;
    if (!b) return;
    group.querySelectorAll("button").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
    onPick(b.dataset.v!);
  };
}

function renderSettings(s: any) {
  serverSettings = s;
  el.voiceSelect.innerHTML = (s.voices as { id: string; label: string }[])
    .map((v) => `<option value="${v.id}" ${v.id === s.tts_voice ? "selected" : ""}>${escapeHtml(v.label)}</option>`).join("");
  const rates = Object.entries(s.rates as Record<string, string>).map(([k, v]) => [v, RATE_LABELS[k] ?? k] as [string, string]);
  segmented(el.rateGroup, rates, s.tts_rate, (v) => send({ type: "settings", changes: { tts_rate: v } }));
  segmented(el.confirmGroup, CONFIRM_LABELS, s.confirm_level, (v) => send({ type: "settings", changes: { confirm_level: v } }));
  el.optStartup.checked = Boolean(s.startup);
  if (document.activeElement !== el.optTitle) el.optTitle.value = s.user_title ?? "";
  if (document.activeElement !== el.optCity) el.optCity.value = s.city ?? "";
}

function openSettings() {
  closeTextSheet();
  document.body.classList.add("settings-open");
  el.settings.hidden = false;
  el.settingsBtn.setAttribute("aria-expanded", "true");
  el.optFollow.checked = followUp;
  el.optBarge.checked = bargeIn;
  el.optWake.checked = wakeEnabled;
}

function closeSettings() {
  document.body.classList.remove("settings-open");
  el.settings.hidden = true;
  el.settingsBtn.setAttribute("aria-expanded", "false");
}

el.settingsBtn.addEventListener("click", () => (el.settings.hidden ? openSettings() : closeSettings()));
el.settingsClose.addEventListener("click", closeSettings);
el.voiceSelect.addEventListener("change", () => send({ type: "settings", changes: { tts_voice: el.voiceSelect.value } }));
el.previewBtn.addEventListener("click", async () => {
  stopSpeaking();
  const name = cfg && serverSettings?.user_title ? serverSettings.user_title : "sir";
  const blob = await fetchTts(`Good evening, ${name}. This is how I'll sound.`, el.voiceSelect.value, serverSettings?.tts_rate);
  if (blob) await playBlob(blob);
  else addMsg("notice", "Voice preview needs an internet connection.");
});
el.optWake.addEventListener("change", () => void applyWake(el.optWake.checked));
el.optFollow.addEventListener("change", () => { followUp = el.optFollow.checked; prefs.set("follow", followUp); });
el.optBarge.addEventListener("change", () => { bargeIn = el.optBarge.checked; prefs.set("barge", bargeIn); });
el.optStartup.addEventListener("change", () => send({ type: "settings", changes: { startup: el.optStartup.checked } }));
el.optTitle.addEventListener("change", () => send({ type: "settings", changes: { user_title: el.optTitle.value } }));
el.optCity.addEventListener("change", () => send({ type: "settings", changes: { city: el.optCity.value } }));
el.newSessionBtn.addEventListener("click", () => {
  send({ type: "new_session" });
  el.stream.innerHTML = "";
  closeSettings();
});

// ---------------------------------------------------------------- integrations

const KIND_LABEL: Record<string, string> = { mcp: "MCP server", http: "HTTP action", plugin: "Plugin", config: "Config" };

function renderIntegrations(items: any[]) {
  if (!items.length) {
    el.integrationList.innerHTML = `<li class="empty">Nothing connected yet. Edit integrations to add one.</li>`;
    return;
  }
  el.integrationList.innerHTML = items.map((i) =>
    `<li class="${escapeHtml(i.status)}"><span class="led"></span>` +
    `<span>${escapeHtml(i.name)} <span class="kind">${escapeHtml(KIND_LABEL[i.kind] ?? i.kind)}</span></span>` +
    `<span class="kind">${i.status === "connected" ? `${i.tools} tool${i.tools === 1 ? "" : "s"}` : escapeHtml(i.status)}</span>` +
    (i.error ? `<span class="err">${escapeHtml(i.error)}</span>` : "") + `</li>`).join("");
}

el.reloadIntegrationsBtn.addEventListener("click", () => {
  el.integrationList.innerHTML = `<li class="empty">Reloading...</li>`;
  send({ type: "reload_integrations" });
});
el.openIntegrationsBtn.addEventListener("click", () => bridge.openConfig("integrations"));
el.openPluginsBtn.addEventListener("click", () => bridge.openConfig("plugins"));

// ---------------------------------------------------------------- routines, workspaces, activity

function hudAction(name: string, args: Record<string, unknown> = {}) {
  send({ type: "action", name, args });
}

function renderRoutines(items: any[]) {
  if (!items.length) {
    el.routineList.innerHTML = `<li class="empty">None yet. Try “Jarvis, every weekday at 9 brief me”.</li>`;
    return;
  }
  el.routineList.innerHTML = items.map((r) =>
    `<li class="${r.enabled ? "" : "paused"}" data-id="${r.id}"><span><span class="name">${escapeHtml(r.name)}</span>` +
    `<span class="meta">${escapeHtml(r.describe)}, ran ${r.runs}×</span></span><span class="acts">` +
    `<button data-do="run" title="Run now">Run</button>` +
    `<button data-do="toggle" title="${r.enabled ? "Pause" : "Resume"}">${r.enabled ? "Pause" : "Resume"}</button>` +
    `<button data-do="delete" title="Delete">×</button></span></li>`).join("");
}

el.routineList.addEventListener("click", (e) => {
  const btn = (e.target as HTMLElement).closest("button") as HTMLButtonElement | null;
  const li = btn?.closest("li") as HTMLElement | null;
  if (!btn || !li) return;
  const id = Number(li.dataset.id);
  if (btn.dataset.do === "run") hudAction("run_routine", { id });
  if (btn.dataset.do === "toggle") hudAction("toggle_routine", { id, enabled: li.classList.contains("paused") });
  if (btn.dataset.do === "delete") hudAction("delete_routine", { id });
});

function renderWorkspaces(items: any[]) {
  if (!items.length) {
    el.workspaceList.innerHTML = `<li class="empty">None yet. Arrange your windows and say “save this as my coding workspace”.</li>`;
    return;
  }
  el.workspaceList.innerHTML = items.map((w) =>
    `<li data-name="${escapeHtml(w.name)}"><span><span class="name">${escapeHtml(w.name)}</span>` +
    `<span class="meta">${w.windows} windows</span></span><span class="acts">` +
    `<button data-do="restore">Restore</button><button data-do="delete" title="Delete">×</button></span></li>`).join("");
}

el.workspaceList.addEventListener("click", (e) => {
  const btn = (e.target as HTMLElement).closest("button") as HTMLButtonElement | null;
  const li = btn?.closest("li") as HTMLElement | null;
  if (!btn || !li) return;
  if (btn.dataset.do === "restore") { closeSettings(); hudAction("restore_workspace", { name: li.dataset.name }); }
  if (btn.dataset.do === "delete") hudAction("delete_workspace", { name: li.dataset.name });
});

el.optActivity.addEventListener("change", () => hudAction("set_activity", { enabled: el.optActivity.checked }));
el.clearActivityBtn.addEventListener("click", () => hudAction("clear_activity"));


// ---------------------------------------------------------------- first-run setup wizard

const ob = {
  root: $<HTMLElement>("onboard"),
  steps: Array.from(document.querySelectorAll<HTMLElement>("#onboard .obStep")),
  dots: Array.from(document.querySelectorAll<HTMLElement>("#onboard .obSteps li")),
  name: $<HTMLInputElement>("obName"),
  city: $<HTMLInputElement>("obCity"),
  title: $<HTMLElement>("obTitle"),
  key: $<HTMLInputElement>("obKey"),
  keyState: $<HTMLElement>("obKeyState"),
  micLevel: $<HTMLElement>("obMicLevel"),
  micState: $<HTMLElement>("obMicState"),
  micTest: $<HTMLButtonElement>("obMicTest"),
  heard: $<HTMLElement>("obHeard"),
  voice: $<HTMLSelectElement>("obVoice"),
  activity: $<HTMLInputElement>("obActivity"),
  headsup: $<HTMLInputElement>("obHeadsup"),
  startup: $<HTMLInputElement>("obStartup"),
  back: $<HTMLButtonElement>("obBack"),
  skip: $<HTMLButtonElement>("obSkip"),
  next: $<HTMLButtonElement>("obNext"),
};
let obStep = 1;
let obTitleChoice = "sir";
let obHasKey = false;
let obKeyChecked = false;

function obTitleOptions(): [string, string][] {
  const n = ob.name.value.trim().split(/\s+/)[0] || "";
  return [["sir", "Sir"], ["ma'am", "Ma'am"], ...(n ? [[n, n] as [string, string]] : []), ["", "Nothing"]];
}

function obRenderTitle() {
  const opts = obTitleOptions();
  if (!opts.some(([v]) => v === obTitleChoice)) obTitleChoice = "sir";
  segmented(ob.title, opts, obTitleChoice, (v) => { obTitleChoice = v; });
}

function openOnboarding(state: any) {
  closeSettings();
  closeTextSheet();
  ob.name.value = state?.user_name ?? "";
  ob.city.value = state?.city ?? "";
  obTitleChoice = state?.user_title ?? "sir";
  obHasKey = Boolean(state?.has_key);
  obKeyChecked = obHasKey;
  ob.key.value = "";
  ob.key.placeholder = obHasKey ? `saved (${state.key_hint}); paste a new one to replace it` : "paste it here";
  ob.keyState.textContent = obHasKey ? "A key is already saved." : "";
  ob.keyState.className = "obState" + (obHasKey ? " good" : "");
  ob.activity.checked = el.optActivity.checked || !state?.done;
  ob.headsup.checked = el.optHeadsup.checked;
  ob.startup.checked = serverSettings?.startup ?? true;
  if (serverSettings?.voices) {
    ob.voice.innerHTML = (serverSettings.voices as { id: string; label: string }[])
      .map((v) => `<option value="${v.id}" ${v.id === serverSettings.tts_voice ? "selected" : ""}>${escapeHtml(v.label)}</option>`).join("");
  }
  obRenderTitle();
  document.body.classList.add("onboard-open");
  ob.root.hidden = false;
  void setLayout("panel");
  bridge.show();
  obGo(1);
}

function closeOnboarding() {
  document.body.classList.remove("onboard-open");
  ob.root.hidden = true;
}

function obGo(step: number) {
  obStep = Math.max(1, Math.min(4, step));
  ob.steps.forEach((s) => (s.hidden = Number(s.dataset.step) !== obStep));
  ob.dots.forEach((d, i) => d.classList.toggle("on", i < obStep));
  ob.back.style.visibility = obStep === 1 ? "hidden" : "visible";
  ob.skip.hidden = obStep !== 2;
  ob.next.textContent = obStep === 4 ? "Finish" : obStep === 2 && ob.key.value.trim() && !obKeyChecked ? "Check key" : "Next";
  ob.next.disabled = false;
  if (obStep === 1) ob.name.focus();
  if (obStep === 2) ob.key.focus();
}

function obSave(values: Record<string, unknown>, step: string) {
  hudAction("setup_save", { values, step });
}

ob.name.addEventListener("input", obRenderTitle);
ob.key.addEventListener("input", () => {
  obKeyChecked = false;
  ob.keyState.textContent = "";
  ob.keyState.className = "obState";
  obGo(2);
});
$<HTMLButtonElement>("obGetKey").addEventListener("click", () => bridge.openExternal("https://aistudio.google.com/apikey"));
$<HTMLButtonElement>("rerunSetupBtn").addEventListener("click", () => {
  hudAction("setup_save", { values: { restart: true }, step: "restart" });
});
ob.back.addEventListener("click", () => obGo(obStep - 1));
ob.skip.addEventListener("click", () => obGo(3));
ob.next.addEventListener("click", () => {
  if (obStep === 1) {
    obSave({ user_name: ob.name.value.trim(), city: ob.city.value.trim(), user_title: obTitleChoice }, "who");
    obGo(2);
  } else if (obStep === 2) {
    const key = ob.key.value.trim();
    if (key && !obKeyChecked) {
      ob.next.disabled = true;
      ob.keyState.className = "obState";
      ob.keyState.textContent = "Checking with Google...";
      obSave({ gemini_key: key }, "key");  // validated server-side before it's saved
      return;
    }
    if (!obHasKey && !key) {
      ob.keyState.className = "obState bad";
      ob.keyState.textContent = "Without a key I can't think. Paste one, or skip and add it later in Setup.";
      return;
    }
    obGo(3);
  } else if (obStep === 3) {
    if (ob.voice.value) send({ type: "settings", changes: { tts_voice: ob.voice.value } });
    obGo(4);
  } else {
    hudAction("set_activity", { enabled: ob.activity.checked });
    hudAction("set_headsup", { enabled: ob.headsup.checked });
    send({ type: "settings", changes: { startup: ob.startup.checked } });
    obSave({ done: true }, "done");
    closeOnboarding();
    const who = obTitleChoice || ob.name.value.trim().split(/\s+/)[0] || "";
    const hello = `All set${who ? ", " + who : ""}. Say “Jarvis” or press Ctrl+Alt+J whenever you need me.`;
    addMsg("jarvis", hello);
    speak(hello);
  }
});

function onSetupResult(m: any) {
  if (m.step === "key") {
    ob.next.disabled = false;
    if (m.ok) {
      obHasKey = true;
      obKeyChecked = true;
      ob.key.value = "";
      ob.key.placeholder = `saved (${m.state?.key_hint ?? ""})`;
      ob.keyState.className = "obState good";
      ob.keyState.textContent = "Key works. I'm thinking with Gemini now.";
      obGo(2);
    } else {
      ob.keyState.className = "obState bad";
      ob.keyState.textContent = m.message;
      ob.next.textContent = "Check key";
    }
  } else if (m.step === "restart" && m.ok) {
    openOnboarding(m.state);
  }
}

// Mic test: record ~4 s, show the level, then show what Whisper heard.
let obRecording = false;
ob.micTest.addEventListener("click", async () => {
  if (obRecording) return;
  obRecording = true;
  ob.micTest.disabled = true;
  ob.heard.className = "obState";
  ob.heard.textContent = "";
  let stream: MediaStream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
  } catch (err) {
    ob.heard.className = "obState bad";
    ob.heard.textContent = `Windows blocked the microphone (${(err as Error).message}). Settings > Privacy > Microphone.`;
    ob.micTest.disabled = false;
    obRecording = false;
    return;
  }
  const ctx = new AudioContext({ sampleRate: 16000 });
  const node = ctx.createScriptProcessor(1024, 1, 1);
  const chunks: Float32Array[] = [];
  let peak = 0;
  node.onaudioprocess = (e) => {
    const buf = new Float32Array(e.inputBuffer.getChannelData(0));
    chunks.push(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
    const level = Math.min(1, Math.sqrt(sum / buf.length) * 9);
    peak = Math.max(peak, level);
    ob.micLevel.style.width = `${Math.round(level * 100)}%`;
  };
  ctx.createMediaStreamSource(stream).connect(node);
  node.connect(ctx.destination);
  for (let s = 4; s > 0; s--) {
    ob.micState.textContent = `listening ${s}`;
    await new Promise((r) => setTimeout(r, 1000));
  }
  node.disconnect();
  stream.getTracks().forEach((t) => t.stop());
  void ctx.close();
  ob.micLevel.style.width = "0%";
  ob.micState.textContent = "checking";
  try {
    const resp = await fetch(`${cfg.backend}/stt`, {
      method: "POST",
      headers: { "Content-Type": "audio/wav", "X-Jarvis-Token": cfg.token },
      body: encodeWav(chunks, 16000),
    });
    const text = resp.ok ? String((await resp.json()).text ?? "").trim() : "";
    if (text) {
      ob.heard.className = "obState good";
      ob.heard.textContent = `I heard: “${text}”`;
    } else {
      ob.heard.className = "obState bad";
      ob.heard.textContent = peak < 0.08
        ? "I didn't hear anything. Is the right microphone selected in Windows sound settings?"
        : "I heard sound but no words. Try again a little closer to the mic.";
    }
  } catch {
    ob.heard.className = "obState bad";
    ob.heard.textContent = "Speech recognition isn't reachable right now.";
  }
  ob.micState.textContent = "ready";
  ob.micTest.disabled = false;
  obRecording = false;
});
$<HTMLButtonElement>("obVoicePreview").addEventListener("click", async () => {
  stopSpeaking();
  const who = obTitleChoice || ob.name.value.trim().split(/\s+/)[0] || "";
  const blob = await fetchTts(`Good evening${who ? ", " + who : ""}. This is how I'll sound.`, ob.voice.value, serverSettings?.tts_rate);
  if (blob) await playBlob(blob);
});

// ---------------------------------------------------------------- meeting copilot

let meetingStarted: number | null = null;

function setMeeting(state: string, title = "", started: number | null = null, last = "") {
  const on = state === "recording" || state === "processing";
  el.meetingBar.hidden = !on;
  el.meetingBar.classList.toggle("processing", state === "processing");
  el.meetBtn.setAttribute("aria-pressed", String(state === "recording"));
  el.meetBtn.classList.toggle("live", state === "recording");
  meetingStarted = state === "recording" ? started : null;
  if (title) el.meetingTitle.textContent = title;
  if (state === "processing") { el.meetingLast.textContent = "Writing the notes..."; el.meetingStop.disabled = true; }
  else { el.meetingStop.disabled = false; if (last) el.meetingLast.textContent = last; else if (!on) el.meetingLast.textContent = ""; }
  tickMeeting();
}

function tickMeeting() {
  if (meetingStarted === null) return;
  el.meetingClock.textContent = fmtCountdown(Math.max(0, Math.round(Date.now() / 1000 - meetingStarted)));
}
setInterval(tickMeeting, 1000);

el.meetBtn.addEventListener("click", () => {
  if (el.meetBtn.getAttribute("aria-pressed") === "true") hudAction("meeting_stop");
  else hudAction("meeting_start");
});
el.meetingStop.addEventListener("click", () => { el.meetingStop.disabled = true; hudAction("meeting_stop"); });

function showMeetingSummary(m: any) {
  const card = document.createElement("div");
  card.className = "meetcard";
  const actions: any[] = m.actions ?? [];
  const li = (xs: string[]) => xs.map((x) => `<li>${escapeHtml(x)}</li>`).join("");
  card.innerHTML =
    `<div class="mc-head"><span class="mc-kind">Meeting notes</span><span class="mc-title">${escapeHtml(m.title)}</span></div>` +
    `<p class="mc-sum">${escapeHtml(m.summary)}</p>` +
    ((m.decisions ?? []).length ? `<h4>Decisions</h4><ul>${li(m.decisions)}</ul>` : "") +
    (actions.length ? `<h4>Action items</h4><ul>${li(actions.map((a) =>
      `${/^(me|you|i)$/i.test(a.owner) ? "You" : a.owner}: ${a.task}${a.due ? ` (due ${a.due})` : ""}`))}</ul>` : "") +
    `<div class="mc-actions"><button data-do="open">Open notes</button>` +
    (actions.length ? `<button data-do="track" class="primary">Track action items</button>` : "") + `</div>`;
  card.dataset.id = String(m.id);
  el.stream.appendChild(card);
  scrollDown();
  bridge.notify("Meeting notes ready", m.title);
}

el.stream.addEventListener("click", (e) => {
  const btn = (e.target as HTMLElement).closest(".meetcard button") as HTMLButtonElement | null;
  const card = btn?.closest(".meetcard") as HTMLElement | null;
  if (!btn || !card) return;
  if (btn.dataset.do === "open") hudAction("open_meeting_notes", { id: Number(card.dataset.id) });
  if (btn.dataset.do === "track") { btn.disabled = true; btn.textContent = "Tracked"; hudAction("meeting_track", { id: Number(card.dataset.id) }); }
});

// ---------------------------------------------------------------- heads-up (screen-aware help)

const HEADSUP_LABEL: Record<string, string> = { error: "Error spotted", download: "Download finished", frozen: "Not responding" };
const headsupPrompts = new Map<string, string[]>();

function showHeadsup(m: any) {
  const card = document.createElement("div");
  card.className = `headsup ${m.kind}`;
  card.dataset.id = m.id;
  card.dataset.mute = m.mute_key ?? "";
  const actions: { label: string; prompt: string }[] = m.actions ?? [];
  headsupPrompts.set(m.id, actions.map((a) => a.prompt));
  const muteName = m.kind === "download" ? "downloads" : m.app_label ?? (m.mute_key ?? "").replace(/\.exe$/, "");
  card.innerHTML =
    `<div class="hu-head"><span class="hu-kind">${escapeHtml(HEADSUP_LABEL[m.kind] ?? "Heads-up")}</span>` +
    `<span class="hu-where">${escapeHtml(m.kind === "download" ? "" : m.title ?? "")}</span>` +
    `<button class="hu-x" data-do="dismiss" title="Dismiss">×</button></div>` +
    `<p class="hu-text">${escapeHtml(m.text)}</p>` +
    `<div class="hu-actions">` +
    actions.map((a, i) => `<button data-do="act" data-i="${i}" class="${i === 0 && m.kind === "error" ? "primary" : ""}">${escapeHtml(a.label)}</button>`).join("") +
    `<button data-do="mute" class="quiet">Mute ${escapeHtml(muteName)}</button></div>`;
  el.stream.appendChild(card);
  const all = el.stream.querySelectorAll(".headsup");
  if (all.length > 4) all[0].remove();
  scrollDown();
  if (m.kind === "error") chime(523);
  bridge.notify(HEADSUP_LABEL[m.kind] ?? "Jarvis", m.text);
}

el.stream.addEventListener("click", (e) => {
  const btn = (e.target as HTMLElement).closest(".headsup button") as HTMLButtonElement | null;
  const card = btn?.closest(".headsup") as HTMLElement | null;
  if (!btn || !card) return;
  if (btn.dataset.do === "act") {
    const prompt = headsupPrompts.get(card.dataset.id ?? "")?.[Number(btn.dataset.i)] ?? "";
    if (prompt) submitText(prompt, false);
  } else if (btn.dataset.do === "mute") {
    hudAction("headsup_mute", { app: card.dataset.mute, mute: true });
  }
  headsupPrompts.delete(card.dataset.id ?? "");
  card.classList.add("gone");
  setTimeout(() => card.remove(), 180);
});

function renderHeadsupState(enabled: boolean, muted: string[]) {
  el.optHeadsup.checked = enabled;
  el.headsupMuted.hidden = muted.length === 0;
  el.headsupMuted.innerHTML = muted.length
    ? "Muted: " + muted.map((a) => `<button class="link" data-unmute="${escapeHtml(a)}">${escapeHtml(a.replace(/\.exe$/, ""))} ×</button>`).join(" ")
    : "";
}

el.optHeadsup.addEventListener("change", () => hudAction("set_headsup", { enabled: el.optHeadsup.checked }));
el.headsupMuted.addEventListener("click", (e) => {
  const b = (e.target as HTMLElement).closest("button[data-unmute]") as HTMLButtonElement | null;
  if (b) hudAction("headsup_mute", { app: b.dataset.unmute, mute: false });
});

// ---------------------------------------------------------------- text actions (Ctrl+Alt+Space)

let selection: { text: string; app: string; hwnd: number } | null = null;
let textResultValue = "";

function openTextSheet() {
  closeSettings();
  document.body.classList.add("text-open");
  el.textSheet.hidden = false;
  el.textResult.hidden = true;
  void setLayout("panel");
  bridge.show();
}

function closeTextSheet() {
  document.body.classList.remove("text-open");
  el.textSheet.hidden = true;
}

function showSelection(m: any) {
  if (!m.text || !String(m.text).trim()) {
    addMsg("notice", "Select some text in any app first, then press Ctrl+Alt+Space.");
    bridge.show();
    return;
  }
  selection = { text: m.text, app: m.app, hwnd: m.hwnd };
  const app = (m.app || "").split(" - ").slice(-1)[0] || m.process || "your app";
  el.textSheetTitle.textContent = `From ${app}`;
  el.selectionPreview.textContent = m.text.length > 600 ? m.text.slice(0, 600) + "…" : m.text;
  el.customAction.value = "";
  openTextSheet();
}

function runTextAction(action: string, instruction = "") {
  if (!selection) return;
  el.textActions.querySelectorAll("button").forEach((b) => b.classList.toggle("busy", b.dataset.action === action));
  el.textResult.hidden = false;
  el.textResultText.textContent = "Working on it…";
  el.replaceBtn.hidden = true;
  hudAction("text_action", { action, text: selection.text, instruction, lang: "Urdu" });
}

el.textActions.addEventListener("click", (e) => {
  const b = (e.target as HTMLElement).closest("button") as HTMLButtonElement | null;
  if (b?.dataset.action) runTextAction(b.dataset.action);
});
el.customActionForm.addEventListener("submit", (e) => {
  e.preventDefault();
  if (el.customAction.value.trim()) runTextAction("custom", el.customAction.value.trim());
});
el.textSheetClose.addEventListener("click", closeTextSheet);
el.copyResultBtn.addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(textResultValue); el.copyResultBtn.textContent = "Copied"; } catch { /* ignore */ }
  setTimeout(() => (el.copyResultBtn.textContent = "Copy"), 1200);
});
el.replaceBtn.addEventListener("click", () => {
  if (!selection) return;
  hudAction("paste_back", { hwnd: selection.hwnd, text: textResultValue });
  closeTextSheet();
});

// ================================================================== messages

const sentHistory: string[] = [];
let historyIdx = -1;

function submitText(text: string, voice: boolean) {
  const clean = text.trim();
  if (!clean) return;
  if (!send({ type: "chat", text: clean, voice })) return;
  sentHistory.push(clean);
  historyIdx = -1;
  lastRequestWasVoice = voice;
  closeSettings();
  addMsg("you", clean);
  el.heard.textContent = voice ? `“${clean}”` : "";
  planEl = null;
  setBusy(true);
  setState("thinking", "Thinking");
}

function handle(m: any) {
  switch (m.type) {
    case "hello": {
      sttEngine = m.stt;
      const live = (m.brains as any[]).find((b) => b.configured && !b.benched_for);
      el.brainName.textContent = live ? live.model : "no model configured";
      if (m.settings) renderSettings(m.settings);
      if (m.integrations) renderIntegrations(m.integrations);
      el.optActivity.checked = Boolean(m.activity);
      setMeeting(m.meeting ? "recording" : "idle", m.meeting?.title ?? "", m.meeting?.started ?? null);
      if (m.setup && !m.setup.done) openOnboarding(m.setup);
      if (m.greeting && (!m.setup || m.setup.done)) {
        addMsg("jarvis", m.greeting);
        speak(m.greeting);
      }
      const defaultWake = String(m.stt).startsWith("whisper");
      void applyWake(prefs.get("wake", defaultWake) && m.stt !== "none");
      setBusy(Boolean(m.busy));
      setState(m.busy ? "thinking" : "idle", m.busy ? "Still working on your last request" : "Ready");
      break;
    }
    case "state":
      if (m.state === "thinking") { setBusy(true); if (!speaking) setState("thinking", "Thinking"); }
      else if (m.state === "executing") { setState("executing"); el.miniTitle.textContent = "Working"; void setLayout("mini"); }
      else if (m.state === "idle") {
        setBusy(false);
        void setLayout("panel");
        if (!speaking && state !== "listening") setState("idle");
      }
      break;
    case "brain":
      el.brainName.textContent = (m.status as any[])?.find((b) => b.name === m.provider)?.model ?? m.provider;
      break;
    case "narration":
      addMsg("jarvis", m.text, true);
      el.caption.textContent = m.text.split("\n")[0].slice(0, 120);
      break;
    case "tool_call":
      addTrace(m.id, m.name, m.args ?? {});
      el.caption.textContent = `${TOOL_LABELS[m.name] ?? m.name.replace(/_/g, " ")} ${describeArgs(m.name, m.args ?? {})}`;
      break;
    case "tool_result":
      finishTrace(m.id, m.ok, m.summary ?? "", m.undo_id);
      break;
    case "screenshot":
      addSight(m.b64);
      break;
    case "plan":
      showPlan(m.steps ?? [], m.current ?? 0);
      break;
    case "confirm_request":
      openConfirm(m.id, m.reason, m.timeout ?? 60);
      break;
    case "confirm_closed":
      closeConfirm(m.id);
      break;
    case "reply": {
      addMsg(m.status === "error" ? "error" : "jarvis", m.text);
      el.caption.textContent = captionOf(m.text);
      el.heard.textContent = "";
      void setLayout("panel");
      const keepTalking = lastRequestWasVoice && followUp && sttEngine !== "none" && m.status !== "error";
      const then = keepTalking ? () => void openCommandWindow({ quiet: true, seconds: 6 }) : undefined;
      if (m.speak || speakReplies) speak(m.text, then);
      else then?.();
      break;
    }
    case "reminder":
      addMsg("reminder", m.text);
      bridge.show();
      bridge.notify("Jarvis", m.text);
      if (m.kind === "timer") { chime(988); setTimeout(() => chime(1318), 220); }
      speak(m.text);
      break;
    case "reminders":
      reminders = m.items ?? [];
      el.timers.dataset.key = "";
      renderTimers();
      break;
    case "integrations":
      renderIntegrations(m.items ?? []);
      break;
    case "job":
      jobs.set(m.job.id, m.job);
      el.timers.dataset.key = "";
      renderTimers();
      break;
    case "routines":
      renderRoutines(m.items ?? []);
      break;
    case "workspaces":
      renderWorkspaces(m.items ?? []);
      break;
    case "activity":
      el.optActivity.checked = Boolean(m.enabled);
      break;
    case "selection":
      showSelection(m);
      break;
    case "text_action_result":
      textResultValue = m.text || "";
      el.textResult.hidden = false;
      el.textResultText.textContent = textResultValue || "(no result)";
      el.replaceBtn.hidden = !m.replaceable || !textResultValue;
      el.textActions.querySelectorAll("button").forEach((b) => b.classList.remove("busy"));
      break;
    case "undone": {
      const b = el.stream.querySelector(`button.undo[data-undo="${Number(m.id)}"]`) as HTMLButtonElement | null;
      if (b) b.textContent = "Undone";
      break;
    }
    case "external":
      addMsg("you", `${m.text}  (via ${m.source})`);
      break;
    case "stats":
      applyStats(m);
      break;
    case "settings":
      renderSettings(m.settings);
      break;
    case "notice":
      addMsg("notice", m.text);
      break;
    case "headsup":
      showHeadsup(m);
      break;
    case "meeting":
      setMeeting(m.state, m.title ?? "", m.started ?? null, m.last ?? "");
      break;
    case "meeting_summary":
      showMeetingSummary(m);
      break;
    case "setup_result":
      onSetupResult(m);
      break;
    case "headsup_state":
      renderHeadsupState(Boolean(m.enabled), m.muted ?? []);
      break;
    case "rejected":
      addMsg("error", m.reason);
      setBusy(false);
      setState("idle");
      break;
  }
}

// ================================================================== controls

el.composer.addEventListener("submit", (e) => {
  e.preventDefault();
  stopSpeaking();
  submitText(el.input.value, false);
  el.input.value = "";
});

el.input.addEventListener("keydown", (e) => {
  if (!sentHistory.length) return;
  if (e.key === "ArrowUp") {
    historyIdx = historyIdx < 0 ? sentHistory.length - 1 : Math.max(0, historyIdx - 1);
    el.input.value = sentHistory[historyIdx];
    e.preventDefault();
  } else if (e.key === "ArrowDown" && historyIdx >= 0) {
    historyIdx = historyIdx + 1 >= sentHistory.length ? -1 : historyIdx + 1;
    el.input.value = historyIdx < 0 ? "" : sentHistory[historyIdx];
    e.preventDefault();
  }
});

function stopEverything() {
  stopSpeaking();
  endCommandWindow();
  if (busy) send({ type: "stop" });
  closeConfirm();
}

el.stopBtn.addEventListener("click", stopEverything);
el.miniStop.addEventListener("click", stopEverything);
el.miniExpand.addEventListener("click", () => void setLayout("panel"));
el.micBtn.addEventListener("click", () => (ear === "command" ? endCommandWindow() : void openCommandWindow()));
el.hideBtn.addEventListener("click", () => bridge.hide());
el.wakeBtn.addEventListener("click", () => void applyWake(!wakeEnabled));
el.voiceBtn.setAttribute("aria-pressed", String(speakReplies));
el.voiceBtn.addEventListener("click", () => {
  speakReplies = !speakReplies;
  prefs.set("speak", speakReplies);
  el.voiceBtn.setAttribute("aria-pressed", String(speakReplies));
  if (!speakReplies) stopSpeaking();
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (!el.textSheet.hidden) closeTextSheet();
    else if (!el.settings.hidden) closeSettings();
    else stopEverything();
  }
  if (confirmId && e.key === "Enter" && document.activeElement !== el.input) {
    e.preventDefault();
    answerConfirm(document.activeElement !== el.denyBtn);
  }
});

bridge.onListen(() => void openCommandWindow());
bridge.onTextAction(() => hudAction("grab_selection"));
bridge.onStop(stopEverything);

// ================================================================== boot

(async () => {
  cfg = await bridge.config();
  setState("offline", "Connecting");
  connect();
})();
