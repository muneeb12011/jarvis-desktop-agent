/**
 * Settings > Brain: pick which AI Jarvis thinks with, paste and test keys, set the failover order.
 *
 * Loaded with a plain <script> tag BEFORE renderer.js, so like renderer.ts it must not contain top-level
 * import/export, and every top-level name starts with `brain` so nothing collides with the renderer.
 * It only touches renderer.ts's helpers (send, hudAction, escapeHtml, bridge) inside functions that run
 * after the page has loaded.
 *
 * The backend does the real work (onboarding.py): it checks every key against its own service before
 * saving anything and applies the new order to the running brain, so no restart is needed. Full keys never
 * come back from the backend, only the last four characters.
 */

interface BrainRow {
  id: string;
  label: string;
  free: boolean;
  needs_key: boolean;
  has_key: boolean;
  key_hint: string;
  key_url: string;
  note: string;
  model: string;
  default_model: string;
  base: string;
  base_editable: boolean;
  enabled: boolean;
}

interface BrainSetup {
  providers: BrainRow[];
  order: string[];
}

interface BrainStatus {
  name: string;
  label?: string;
  model: string;
  configured: boolean;
  benched_for: number;
}

interface BrainNote {
  state: "busy" | "good" | "bad";
  text: string;
}

const brainUi = {
  setup: null as BrainSetup | null,
  status: [] as BrainStatus[],
  order: [] as string[], // the failover order being edited; a provider that isn't listed is off
  keys: {} as Record<string, string>, // typed but not yet applied
  models: {} as Record<string, string>,
  bases: {} as Record<string, string>,
  notes: {} as Record<string, BrainNote>,
  ollamaModels: [] as string[],
  open: "",
  removing: "",
  saving: false,
};

function brainEl<T extends HTMLElement>(id: string): T | null {
  return document.getElementById(id) as T | null;
}

function brainNode<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  cls = "",
  text = "",
): HTMLElementTagNameMap[K] {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text) n.textContent = text;
  return n;
}

function brainButton(label: string, act: string, cls = "", aria = ""): HTMLButtonElement {
  const b = brainNode("button", cls, label);
  b.type = "button";
  b.dataset.act = act;
  if (aria) b.setAttribute("aria-label", aria);
  return b;
}

function brainDuration(seconds: number): string {
  if (seconds < 90) return `${Math.max(1, Math.round(seconds))}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}

/** The provider answering right now: the first switched-on one that has a key and isn't resting. */
function brainLiveName(): string {
  return brainUi.status.find((s) => s.configured && !s.benched_for)?.name ?? "";
}

function brainTag(row: BrainRow): string {
  if (row.id === "ollama") return "on this PC";
  if (row.id === "openrouter") return "free models";
  return row.free ? "free tier" : "paid key";
}

function brainStatusOf(row: BrainRow, enabled: boolean): { cls: string; text: string } {
  const st = brainUi.status.find((s) => s.name === row.id);
  if (enabled && row.id === brainLiveName()) return { cls: "live", text: "answering now" };
  if (enabled && st && st.benched_for > 0) return { cls: "warn", text: `resting ${brainDuration(st.benched_for)}` };
  if (!row.needs_key) return { cls: "good", text: "no key needed" };
  if ((brainUi.keys[row.id] ?? "").trim()) return { cls: "warn", text: "key not applied yet" };
  return row.has_key ? { cls: "good", text: `key ${row.key_hint}` } : { cls: "warn", text: "needs a key" };
}

function brainDirty(): boolean {
  const s = brainUi.setup;
  if (!s) return false;
  if (brainUi.order.join(",") !== s.order.join(",")) return true;
  return s.providers.some((r) => {
    const model = (brainUi.models[r.id] ?? "").trim();
    const base = (brainUi.bases[r.id] ?? "").trim();
    return Boolean((brainUi.keys[r.id] ?? "").trim()) || (model !== "" && model !== r.model) || (base !== "" && base !== r.base);
  });
}

function brainSetMessage(state: "good" | "bad" | "busy" | "", text: string) {
  const m = brainEl<HTMLElement>("brainMsg");
  if (!m) return;
  m.className = "brainMsg" + (state ? ` ${state}` : "");
  m.textContent = text;
}

function brainUpdateApply() {
  const b = brainEl<HTMLButtonElement>("brainApply");
  if (!b) return;
  b.disabled = brainUi.saving || !brainDirty();
  b.textContent = brainUi.saving ? "Checking..." : "Apply";
}

function brainRenderLive() {
  const line = brainEl<HTMLElement>("brainLive");
  if (!line) return;
  const live = brainUi.status.find((s) => s.name === brainLiveName());
  line.replaceChildren();
  if (!live) {
    line.append(brainNode("span", "brainLiveNone", "No brain is answering. Switch one on and give it a key."));
    return;
  }
  line.append(brainNode("span", "brainLiveLead", "Thinking with "), brainNode("strong", "", live.label || live.name));
  if (live.model) line.append(brainNode("span", "brainLiveModel", ` · ${live.model}`));
  const resting = brainUi.status.filter((s) => s.benched_for > 0 && brainUi.order.includes(s.name));
  if (resting.length) {
    line.append(
      brainNode("span", "brainLiveModel", `  (resting: ${resting.map((s) => `${s.label || s.name} ${brainDuration(s.benched_for)}`).join(", ")})`),
    );
  }
}

function brainField(row: BrainRow, field: "key" | "model" | "base", label: string): HTMLLabelElement {
  const wrap = brainNode("label", "field brainField");
  wrap.append(brainNode("span", "", label));
  const input = brainNode("input");
  input.dataset.field = field;
  input.autocomplete = "off";
  input.spellcheck = false;
  if (field === "key") {
    input.type = "password";
    input.value = brainUi.keys[row.id] ?? "";
    input.placeholder = row.has_key ? `saved (${row.key_hint}); paste a new one to replace it` : "paste it here";
  } else if (field === "model") {
    input.type = "text";
    input.value = brainUi.models[row.id] ?? row.model;
    input.placeholder = row.default_model;
    input.maxLength = 100;
    if (row.id === "ollama") input.setAttribute("list", "brainOllamaModels");
  } else {
    input.type = "text";
    input.value = brainUi.bases[row.id] ?? row.base;
    input.maxLength = 200;
  }
  wrap.append(input);
  return wrap;
}

function brainRowNode(row: BrainRow): HTMLLIElement {
  const enabled = brainUi.order.includes(row.id);
  const isOpen = brainUi.open === row.id;
  const li = brainNode("li", `brain ${enabled ? "on" : "off"}${isOpen ? " open" : ""}`);
  li.dataset.id = row.id;

  const head = brainNode("div", "brainHead");
  const sw = brainNode("label", "switch brainSwitch");
  const toggle = brainNode("input");
  toggle.type = "checkbox";
  toggle.checked = enabled;
  toggle.dataset.act = "toggle";
  toggle.setAttribute("aria-label", `Use ${row.label}`);
  sw.append(toggle, brainNode("span", "brainName", row.label));
  head.append(sw);

  const status = brainStatusOf(row, enabled);
  const meta = brainNode("div", "brainMeta");
  meta.append(brainNode("span", "brainTag", brainTag(row)), brainNode("span", `brainLed ${status.cls}`, status.text));
  head.append(meta);

  const acts = brainNode("div", "brainActs");
  if (enabled) {
    const at = brainUi.order.indexOf(row.id);
    const up = brainButton("↑", "up", "brainMove", `Move ${row.label} earlier`);
    const down = brainButton("↓", "down", "brainMove", `Move ${row.label} later`);
    up.disabled = at === 0;
    down.disabled = at === brainUi.order.length - 1;
    acts.append(up, down);
  }
  acts.append(brainButton(isOpen ? "Done" : "Set up", "edit", "brainEdit"));
  head.append(acts);
  li.append(head);

  if (isOpen) {
    const body = brainNode("div", "brainBody");
    body.append(brainNode("p", "hint", row.note));
    if (row.needs_key) body.append(brainField(row, "key", "API key"));
    body.append(brainField(row, "model", "Model"));
    if (row.base_editable) body.append(brainField(row, "base", "Address"));

    const buttons = brainNode("div", "row");
    buttons.append(brainButton("Test", "test", "ghost"));
    buttons.append(brainButton(row.id === "ollama" ? "Get Ollama" : "Get a key", "getkey", "ghost"));
    if (row.needs_key && row.has_key) {
      buttons.append(brainButton(brainUi.removing === row.id ? "Remove it?" : "Remove key", "remove", "ghost"));
    }
    body.append(buttons);

    const note = brainUi.notes[row.id];
    const noteEl = brainNode("p", `brainNote${note ? ` ${note.state}` : ""}`, note?.text ?? "");
    noteEl.dataset.note = row.id;
    body.append(noteEl);
    li.append(body);
  }
  return li;
}

function brainRender() {
  const list = brainEl<HTMLUListElement>("brainList");
  const setup = brainUi.setup;
  if (!list || !setup) return;
  const byId = new Map(setup.providers.map((r) => [r.id, r] as [string, BrainRow]));
  const on = brainUi.order.map((id) => byId.get(id)).filter((r): r is BrainRow => Boolean(r));
  const off = setup.providers.filter((r) => !brainUi.order.includes(r.id));
  list.replaceChildren(...[...on, ...off].map(brainRowNode));

  const models = brainEl<HTMLDataListElement>("brainOllamaModels");
  if (models) {
    models.replaceChildren(
      ...brainUi.ollamaModels.map((name) => {
        const o = document.createElement("option");
        o.value = name;
        return o;
      }),
    );
  }
  brainUpdateApply();
}

/** Called by the renderer with the backend's provider list (on connect, and after every save). */
function renderBrainSetup(setup: BrainSetup, status?: BrainStatus[]) {
  brainUi.setup = setup;
  if (status) brainUi.status = status;
  brainUi.order = [...setup.order];
  brainRender();
  brainRenderLive();
}

/** Called by the renderer when the brain that answered, or a provider's cooldown, changes. */
function renderBrainLive(status: BrainStatus[]) {
  brainUi.status = status;
  brainRenderLive();
  // Only the status lights change; don't rebuild rows while someone is typing a key.
  document.querySelectorAll<HTMLLIElement>("#brainList li.brain").forEach((li) => {
    const row = brainUi.setup?.providers.find((r) => r.id === li.dataset.id);
    const led = li.querySelector<HTMLElement>(".brainLed");
    if (!row || !led) return;
    const s = brainStatusOf(row, brainUi.order.includes(row.id));
    led.className = `brainLed ${s.cls}`;
    led.textContent = s.text;
  });
}

function brainSetNote(id: string, state: BrainNote["state"], text: string) {
  brainUi.notes[id] = { state, text };
  const n = document.querySelector<HTMLElement>(`#brainList [data-note="${id}"]`);
  if (n) {
    n.className = `brainNote ${state}`;
    n.textContent = text;
  }
}

/** Called by the renderer with the backend's answer to a Test or an Apply. */
function onProviderResult(m: {
  kind: string;
  ok: boolean;
  message: string;
  provider?: string;
  models?: string[];
  providers?: BrainRow[];
  order?: string[];
}) {
  if (m.kind === "test") {
    const id = m.provider ?? "";
    if (m.models && m.models.length) {
      brainUi.ollamaModels = m.models;
      brainRender();
    }
    if (id) brainSetNote(id, m.ok ? "good" : "bad", m.message);
    return;
  }
  brainUi.saving = false;
  if (m.ok) {
    brainUi.keys = {};
    brainUi.models = {};
    brainUi.bases = {};
    brainUi.notes = {};
    brainUi.removing = "";
    if (m.providers && m.order) renderBrainSetup({ providers: m.providers, order: m.order });
    brainSetMessage("good", m.message);
  } else {
    if (m.provider) {
      brainUi.open = m.provider;
      brainUi.notes[m.provider] = { state: "bad", text: m.message };
      brainRender();
    }
    brainSetMessage("bad", m.message);
  }
  brainUpdateApply();
}

function brainValues() {
  const s = brainUi.setup!;
  const keys: Record<string, string> = {};
  const models: Record<string, string> = {};
  const bases: Record<string, string> = {};
  for (const r of s.providers) {
    const key = (brainUi.keys[r.id] ?? "").trim();
    const model = (brainUi.models[r.id] ?? "").trim();
    const base = (brainUi.bases[r.id] ?? "").trim();
    if (key) keys[r.id] = key;
    if (model && model !== r.model) models[r.id] = model;
    if (base && base !== r.base) bases[r.id] = base;
  }
  return { keys, models, bases, order: [...brainUi.order] };
}

function brainMove(id: string, by: -1 | 1) {
  const at = brainUi.order.indexOf(id);
  const to = at + by;
  if (at < 0 || to < 0 || to >= brainUi.order.length) return;
  [brainUi.order[at], brainUi.order[to]] = [brainUi.order[to], brainUi.order[at]];
  brainRender();
}

function brainToggle(id: string, on: boolean) {
  if (on && !brainUi.order.includes(id)) brainUi.order.push(id);
  if (!on) {
    if (brainUi.order.length <= 1) {
      brainSetMessage("bad", "Keep at least one brain switched on.");
    } else {
      brainUi.order = brainUi.order.filter((x) => x !== id);
      brainSetMessage("", "");
    }
  }
  brainRender();
  brainRenderLive();
}

function brainClick(act: string, id: string) {
  const row = brainUi.setup?.providers.find((r) => r.id === id);
  if (!row) return;
  if (act !== "remove") brainUi.removing = "";
  if (act === "edit") {
    brainUi.open = brainUi.open === id ? "" : id;
    brainRender();
  } else if (act === "up" || act === "down") {
    brainMove(id, act === "up" ? -1 : 1);
  } else if (act === "getkey") {
    bridge.openExternal(row.key_url);
  } else if (act === "test") {
    brainSetNote(id, "busy", "Checking...");
    hudAction("provider_test", {
      provider: id,
      key: (brainUi.keys[id] ?? "").trim(),
      model: (brainUi.models[id] ?? "").trim() || row.model,
      base: (brainUi.bases[id] ?? "").trim() || row.base,
    });
  } else if (act === "remove") {
    if (brainUi.removing !== id) {
      brainUi.removing = id;
      brainRender();
      window.setTimeout(() => {
        if (brainUi.removing === id) {
          brainUi.removing = "";
          brainRender();
        }
      }, 4000);
      return;
    }
    brainUi.removing = "";
    brainSetMessage("busy", "Removing the key...");
    hudAction("provider_save", { values: { clear_keys: [id] } });
  }
}

function brainApply() {
  if (!brainUi.setup || brainUi.saving || !brainDirty()) return;
  brainUi.saving = true;
  brainSetMessage("busy", "Checking your keys with each provider...");
  brainUpdateApply();
  hudAction("provider_save", { values: brainValues() });
}

const brainListEl = brainEl<HTMLUListElement>("brainList");
brainListEl?.addEventListener("click", (e) => {
  const b = (e.target as HTMLElement).closest("button[data-act]") as HTMLButtonElement | null;
  const li = (e.target as HTMLElement).closest("li.brain") as HTMLLIElement | null;
  if (b && li && !b.disabled) brainClick(b.dataset.act!, li.dataset.id!);
});
brainListEl?.addEventListener("change", (e) => {
  const t = e.target as HTMLInputElement;
  const li = t.closest("li.brain") as HTMLLIElement | null;
  if (li && t.dataset.act === "toggle") brainToggle(li.dataset.id!, t.checked);
});
brainListEl?.addEventListener("input", (e) => {
  const t = e.target as HTMLInputElement;
  const li = t.closest("li.brain") as HTMLLIElement | null;
  const field = t.dataset.field;
  if (!li || !field) return;
  const id = li.dataset.id!;
  if (field === "key") brainUi.keys[id] = t.value;
  else if (field === "model") brainUi.models[id] = t.value;
  else if (field === "base") brainUi.bases[id] = t.value;
  const row = brainUi.setup?.providers.find((r) => r.id === id);
  const led = li.querySelector<HTMLElement>(".brainLed");
  if (row && led) {
    const s = brainStatusOf(row, brainUi.order.includes(id));
    led.className = `brainLed ${s.cls}`;
    led.textContent = s.text;
  }
  brainUpdateApply();
});
brainEl<HTMLButtonElement>("brainApply")?.addEventListener("click", brainApply);
