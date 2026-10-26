/**
 * Installed-app runtime: everything start-jarvis.bat and setup-jarvis.bat do for a source checkout.
 *
 *   %LOCALAPPDATA%\Jarvis\        Python, the virtual environment, uv (rebuildable, never backed up)
 *   %APPDATA%\Jarvis\             memory, logs, keys (.env), plugins, integrations (survives updates)
 *   <install dir>\resources\backend   the backend code shipped in the installer
 *
 * First launch shows a small window while bootstrap.ps1 downloads a private Python and Jarvis's
 * packages; after an update it re-runs only if the requirements changed.
 */

import { BrowserWindow, dialog } from "electron";
import { ChildProcess, spawn } from "child_process";
import * as crypto from "crypto";
import * as dotenv from "dotenv";
import * as fs from "fs";
import * as path from "path";

export interface Runtime {
  backendDir: string;
  dataDir: string;
  runtimeDir: string;
  python: string;
  token: string;
  updateUrl: string;
}

let backend: ChildProcess | null = null;

function paths() {
  const appData = process.env.APPDATA || path.join(process.env.USERPROFILE || "", "AppData", "Roaming");
  const local = process.env.LOCALAPPDATA || path.join(process.env.USERPROFILE || "", "AppData", "Local");
  const backendDir = path.join(process.resourcesPath, "backend");
  const runtimeDir = path.join(local, "Jarvis");
  return {
    backendDir,
    dataDir: path.join(appData, "Jarvis"),
    runtimeDir,
    python: path.join(runtimeDir, "venv", "Scripts", "python.exe"),
    bootstrap: path.join(process.resourcesPath, "bootstrap.ps1"),
  };
}

function requirementsStamp(backendDir: string): string {
  const h = crypto.createHash("sha1");
  for (const f of ["requirements.txt", "requirements-voice.txt"]) {
    const p = path.join(backendDir, f);
    if (fs.existsSync(p)) h.update(fs.readFileSync(p));
  }
  return h.digest("hex").slice(0, 16);
}

/** A per-install secret shared by the HUD and the backend; created once in the data folder's .env. */
function ensureToken(dataDir: string): string {
  const envFile = path.join(dataDir, ".env");
  const existing = fs.existsSync(envFile) ? dotenv.parse(fs.readFileSync(envFile)) : {};
  if (existing.JARVIS_AUTH_TOKEN) return existing.JARVIS_AUTH_TOKEN;
  const token = crypto.randomBytes(24).toString("hex");
  fs.appendFileSync(envFile, `${fs.existsSync(envFile) ? "\n" : ""}JARVIS_AUTH_TOKEN=${token}\n`);
  return token;
}

const PAGE = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  html,body{margin:0;height:100%;background:#08141d;color:#cfe3ea;font:14px "Segoe UI",sans-serif;overflow:hidden}
  main{padding:26px 28px;display:flex;flex-direction:column;height:100%;box-sizing:border-box;-webkit-app-region:drag}
  h1{margin:0 0 4px;font:600 20px "Bahnschrift","Segoe UI",sans-serif;letter-spacing:.04em;color:#5ce1e6}
  p{margin:0;color:#6f8a96;line-height:1.45}
  .bar{height:3px;margin:22px 0 10px;background:rgba(111,138,150,.25)}
  .bar i{display:block;height:100%;width:4%;background:#5ce1e6;transition:width .4s}
  #step{color:#cfe3ea}
  #log{margin-top:auto;font:11px Consolas,monospace;color:#6f8a96;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .fail #step{color:#ff9d9d}.fail .bar i{background:#ff5d5d}
</style></head><body><main>
  <h1>JARVIS</h1><p>Getting ready for the first time. This downloads about 600 MB and takes a few minutes.</p>
  <div class="bar"><i id="bar"></i></div><div id="step">Starting...</div><div id="log"></div>
</main><script>
  window.update=(s)=>{if(s.step!==undefined){document.getElementById('step').textContent=s.step;
    document.getElementById('bar').style.width=Math.max(4,Math.round(100*s.n/s.total))+'%'}
    if(s.log)document.getElementById('log').textContent=s.log;if(s.fail)document.body.className='fail'};
</script></body></html>`;

function runBootstrap(p: ReturnType<typeof paths>, stamp: string): Promise<boolean> {
  const win = new BrowserWindow({
    width: 460, height: 230, resizable: false, frame: false, center: true, show: true,
    backgroundColor: "#08141d", alwaysOnTop: true, webPreferences: { contextIsolation: true },
  });
  void win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(PAGE));
  const push = (s: object) => { if (!win.isDestroyed()) void win.webContents.executeJavaScript(`update(${JSON.stringify(s)})`); };

  fs.mkdirSync(path.join(p.dataDir, "logs"), { recursive: true });
  const log = fs.createWriteStream(path.join(p.dataDir, "logs", "install.log"), { flags: "a" });
  log.write(`\n=== bootstrap ${new Date().toISOString()} ===\n`);

  return new Promise((resolve) => {
    const ps = spawn(
      "powershell.exe",
      ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", p.bootstrap,
        "-Backend", p.backendDir, "-Runtime", p.runtimeDir, "-Stamp", stamp],
      { windowsHide: true },
    );
    let failed = "";
    let buf = "";
    const onData = (chunk: Buffer) => {
      buf += chunk.toString();
      const lines = buf.split(/\r?\n/);
      buf = lines.pop() ?? "";
      for (const line of lines) {
        log.write(line + "\n");
        const m = line.match(/^@@STEP (\d+) (\d+) (.*)$/);
        if (m) push({ n: Number(m[1]), total: Number(m[2]), step: m[3] });
        else if (line.startsWith("@@FAIL")) { failed = line.slice(7); push({ step: failed, fail: true }); }
        else if (line.trim() && !line.startsWith("@@")) push({ log: line.trim().slice(0, 120) });
      }
    };
    ps.stdout.on("data", onData);
    ps.stderr.on("data", onData);
    ps.on("close", async (code) => {
      log.end();
      if (code === 0 && !failed) {
        push({ n: 1, total: 1, step: "Ready." });
        setTimeout(() => { if (!win.isDestroyed()) win.close(); }, 600);
        resolve(true);
        return;
      }
      await dialog.showMessageBox({
        type: "error",
        title: "Jarvis couldn't finish setting up",
        message: failed || `Setup stopped (code ${code}).`,
        detail: `Check your internet connection and start Jarvis again.\nDetails: ${path.join(p.dataDir, "logs", "install.log")}`,
      });
      if (!win.isDestroyed()) win.close();
      resolve(false);
    });
  });
}

export async function ensureRuntime(): Promise<Runtime | null> {
  const p = paths();
  fs.mkdirSync(p.dataDir, { recursive: true });
  const stamp = requirementsStamp(p.backendDir);
  const stampFile = path.join(p.runtimeDir, "venv", "jarvis-requirements.stamp");
  const current = fs.existsSync(stampFile) ? fs.readFileSync(stampFile, "utf8").trim() : "";
  if (!fs.existsSync(p.python) || current !== stamp) {
    if (!(await runBootstrap(p, stamp))) return null;
  }
  const token = ensureToken(p.dataDir);
  const env = dotenv.parse(fs.readFileSync(path.join(p.dataDir, ".env")));
  for (const [k, v] of Object.entries(env)) if (process.env[k] === undefined) process.env[k] = v;
  return {
    backendDir: p.backendDir, dataDir: p.dataDir, runtimeDir: p.runtimeDir, python: p.python, token,
    updateUrl: env.JARVIS_UPDATE_URL || "",
  };
}

export function startBackend(rt: Runtime): void {
  fs.mkdirSync(path.join(rt.dataDir, "logs"), { recursive: true });
  const out = fs.openSync(path.join(rt.dataDir, "logs", "console.log"), "w");
  backend = spawn(
    rt.python,
    ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000", "--no-access-log", "--log-level", "warning"],
    {
      cwd: rt.backendDir,
      windowsHide: true,
      stdio: ["ignore", out, out],
      env: { ...process.env, JARVIS_DATA_DIR: rt.dataDir, JARVIS_APP_EXE: process.execPath, PYTHONUTF8: "1" },
    },
  );
  backend.on("exit", (code) => {
    console.log(`[backend] exited with ${code}`);
    backend = null;
  });
}

export function stopBackend(): void {
  if (backend && !backend.killed) backend.kill();
  backend = null;
}
