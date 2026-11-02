/**
 * "Work without asking" control: a switch in Settings and a pill in the HUD header that is visible
 * whenever the mode is on, so it can never be active without you seeing it.
 *
 * The backend decides everything (autonomy.py). This file only shows the state it is sent and sends
 * `autonomy_set`. Like brains.ts it is a plain script: no top-level import/export, and every top-level
 * name starts with `auto` so nothing collides with renderer.ts.
 */

interface AutoState {
  active: boolean;
  since: number | null;
  until: number | null; // epoch seconds, null = until you say stop
}

const autoUi = { state: { active: false, since: null, until: null } as AutoState, timer: 0 };

function autoClock(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** Called by the renderer with the backend's state (on connect, and whenever it changes). */
function renderAutonomy(state?: AutoState) {
  if (state) autoUi.state = { active: Boolean(state.active), since: state.since ?? null, until: state.until ?? null };
  const s = autoUi.state;
  window.clearTimeout(autoUi.timer);
  // The backend lapses a timed grant by itself; hide the pill at the same moment.
  if (s.active && s.until) {
    autoUi.timer = window.setTimeout(() => renderAutonomy({ active: false, since: null, until: null }), Math.max(0, s.until * 1000 - Date.now()) + 500);
  }

  const pill = document.getElementById("autoPill") as HTMLButtonElement | null;
  if (pill) {
    pill.hidden = !s.active;
    pill.textContent = "acting freely";
    pill.title = `Jarvis is working without asking${s.until ? ` until ${autoClock(s.until)}` : ""}. Click to make it ask again.`;
  }

  const toggle = document.getElementById("autoToggle") as HTMLButtonElement | null;
  const note = document.getElementById("autoState");
  if (toggle) {
    toggle.setAttribute("aria-pressed", String(s.active));
    toggle.textContent = s.active ? "Make Jarvis ask again" : "Work without asking";
    toggle.classList.toggle("on", s.active);
  }
  if (note) {
    note.textContent = s.active
      ? s.until
        ? `On until ${autoClock(s.until)}.`
        : "On until you stop it."
      : "Off. Jarvis asks before making changes.";
  }
}

function autoSend(on: boolean) {
  const minutes = Number((document.getElementById("autoFor") as HTMLSelectElement | null)?.value ?? 0);
  hudAction("autonomy_set", on ? { on: true, ...(minutes > 0 ? { minutes } : {}) } : { on: false });
}

document.getElementById("autoPill")?.addEventListener("click", () => autoSend(false));
document.getElementById("autoToggle")?.addEventListener("click", () => autoSend(!autoUi.state.active));
