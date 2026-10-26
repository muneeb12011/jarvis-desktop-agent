/**
 * Electron main process.
 *
 * - One frameless, always-on-top HUD docked bottom-right. Two layouts:
 *   "panel" (full HUD) and "mini" (a slim strip while Jarvis drives the
 *   mouse, so it stays out of the way of what it's clicking).
 * - Content protection keeps the HUD OUT of screen captures, so Jarvis's own
 *   screenshots show your desktop, not itself (v2 had to go fullscreen/compact
 *   to dodge this).
 * - Global hotkeys: Ctrl+Alt+J = summon + listen, Ctrl+Alt+X = stop.
 * - Tray icon; closing the window hides it instead of quitting.
 */

import { app, BrowserWindow, globalShortcut, ipcMain, Menu, nativeImage, Notification, screen, session, shell, Tray } from "electron";
import * as dotenv from "dotenv";
import * as path from "path";
import { ensureRuntime, Runtime, startBackend, stopBackend } from "./runtime";

// Source checkout: frontend/.env holds the token and start-jarvis.bat runs the backend.
// Installed app: everything lives under %APPDATA%\Jarvis and the app runs its own backend (see runtime.ts).
dotenv.config({ path: path.join(__dirname, "..", ".env") });

let AUTH_TOKEN = process.env.JARVIS_AUTH_TOKEN || "";
const BACKEND_URL = (process.env.JARVIS_BACKEND_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
let runtime: Runtime | null = null;

type Layout = "panel" | "mini";
const SIZES: Record<Layout, { width: number; height: number }> = {
  panel: { width: 430, height: 780 },
  mini: { width: 400, height: 96 },
};

const ICON =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAA8ElEQVR4nO1XyRHCMAxMGKrgQzk8aIHyaIEH5fChjfBjbEda78rKDDNBz0je3SjWkWnau83qgdvrvSD//XySMOngHnFUSDdIJVaFQKdH7oGq8VBAC6Z+W/b8YQty64yXnZWADHJFhJmBDHIWoxJQKswgt7DaLByjoMvjUgHN12dI8DcDSr235N4zz0ou8w6g9CMi5JPKMEKgxIQFbGF/AZIAptTUcqRmAUuAfN1ZoHQ+i0h585Ir3Amjna+16hOgnj1iaMbAS5ghooexEsAuEhFy655RsyAigl1sfncp7YGyNrSWjwhJ+zFRhWSucvuwD1tllTplblkWAAAAAElFTkSuQmCC";

let win: BrowserWindow | null = null;
let tray: Tray | null = null;
let quitting = false;
let layout: Layout = "panel";

function boundsFor(l: Layout) {
  const area = screen.getPrimaryDisplay().workArea;
  const width = SIZES[l].width;
  const height = Math.min(SIZES[l].height, area.height - 32);
  return { x: area.x + area.width - width - 16, y: area.y + area.height - height - 16, width, height };
}

function setLayout(l: Layout) {
  if (!win) return;
  layout = l;
  win.setBounds(boundsFor(l), true);
  win.webContents.send("hud:layout", l);
}

function showHud() {
  if (!win) return;
  console.log("[hud] summon");
  if (win.isMinimized()) win.restore();
  win.showInactive();
  win.moveTop();
}

function createWindow() {
  win = new BrowserWindow({
    ...boundsFor("panel"),
    frame: false,
    transparent: true,
    resizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    show: false,
    hasShadow: false,
    backgroundColor: "#00000000",
    icon: nativeImage.createFromDataURL(ICON),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false,
    },
  });
  win.setAlwaysOnTop(true, "floating");
  // JARVIS_HUD_CAPTURABLE=1 lets screenshot tools see the HUD (for sharing or debugging).
  if (process.env.JARVIS_HUD_CAPTURABLE !== "1") win.setContentProtection(true);
  win.setVisibleOnAllWorkspaces(true);

  Promise.all([win.webContents.session.clearCache(), win.webContents.session.clearCodeCaches({})]).then(() =>
    win!.loadFile(path.join(__dirname, "index.html")),
  );
  win.once("ready-to-show", () => win?.show());
  // Window lifecycle goes to backend/logs/hud.log, which makes "where did the HUD go?" debuggable.
  for (const ev of ["show", "hide", "minimize", "restore"] as const) {
    win.on(ev as any, () => console.log(`[hud] ${ev} ${JSON.stringify(win?.getBounds())}`));
  }
  win.webContents.on("render-process-gone", (_e, d) => console.log(`[hud] renderer gone: ${d.reason}`));
  win.webContents.on("console-message", (_e, level, message) => {
    if (level >= 2) console.log(`[hud:renderer] ${message}`);
  });
  win.on("close", (e) => {
    if (!quitting) {
      e.preventDefault();
      win?.hide();
    }
  });
}

function createTray() {
  tray = new Tray(nativeImage.createFromDataURL(ICON).resize({ width: 16, height: 16 }));
  tray.setToolTip("Jarvis (Ctrl+Alt+J to talk)");
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "Show Jarvis", click: showHud },
      { label: "Talk  (Ctrl+Alt+J)", click: () => { showHud(); win?.webContents.send("hud:listen"); } },
      { label: "Hide", click: () => win?.hide() },
      { type: "separator" },
      { label: "Quit Jarvis", click: () => { quitting = true; app.quit(); } },
    ]),
  );
  tray.on("click", () => (win?.isVisible() ? win.hide() : showHud()));
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", showHud);

  app.whenReady().then(async () => {
    // Microphone access for the voice loop.
    session.defaultSession.setPermissionRequestHandler((_wc, permission, cb) => cb(permission === "media"));
    session.defaultSession.setPermissionCheckHandler((_wc, permission) => permission === "media");

    if (app.isPackaged) {
      runtime = await ensureRuntime();
      if (!runtime) { quitting = true; app.quit(); return; }
      AUTH_TOKEN = runtime.token;
      startBackend(runtime);
      checkForUpdates(runtime);
    }

    createWindow();
    createTray();

    globalShortcut.register("Control+Alt+J", () => {
      showHud();
      win?.webContents.send("hud:listen");
    });
    globalShortcut.register("Control+Alt+X", () => win?.webContents.send("hud:stop"));
    // Text actions: the renderer asks the backend to copy the selection while the user's app still has
    // focus, and only then shows the panel.
    globalShortcut.register("Control+Alt+Space", () => win?.webContents.send("hud:textAction"));
  });

  app.on("will-quit", () => { globalShortcut.unregisterAll(); stopBackend(); });
  app.on("before-quit", () => (quitting = true));
  app.on("window-all-closed", () => { /* tray keeps Jarvis alive */ });
}

ipcMain.handle("hud:config", () => ({ token: AUTH_TOKEN, backend: BACKEND_URL }));
ipcMain.handle("hud:setLayout", (_e, l: Layout) => { setLayout(l); return layout; });
ipcMain.on("hud:hide", () => win?.hide());
ipcMain.on("hud:show", showHud);
ipcMain.on("hud:notify", (_e, title: string, body: string) => {
  if (Notification.isSupported()) new Notification({ title, body, silent: true }).show();
});

ipcMain.on("hud:openConfig", (_e, which: "integrations" | "plugins") => {
  const data = runtime ? runtime.dataDir : path.join(__dirname, "..", "..", "backend");
  void shell.openPath(which === "plugins" ? path.join(data, "plugins") : path.join(data, "integrations.json"));
});

ipcMain.on("hud:openExternal", (_e, url: string) => {
  if (/^https:\/\/[\w.-]+\//.test(url)) void shell.openExternal(url);
});

/** Auto-update for installed builds: set JARVIS_UPDATE_URL in %APPDATA%\Jarvis\.env to a folder holding
 * the latest.yml and installer that `npm run dist` produces (any static host works). */
function checkForUpdates(rt: Runtime) {
  if (!rt.updateUrl) return;
  try {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const { autoUpdater } = require("electron-updater");
    autoUpdater.setFeedURL({ provider: "generic", url: rt.updateUrl });
    autoUpdater.autoInstallOnAppQuit = true;
    autoUpdater.on("update-downloaded", (info: { version: string }) => {
      new Notification({ title: "Jarvis update ready", body: `Version ${info.version} installs when Jarvis restarts.` }).show();
    });
    autoUpdater.on("error", (err: Error) => console.log(`[update] ${err.message}`));
    void autoUpdater.checkForUpdates();
  } catch (err) {
    console.log(`[update] unavailable: ${(err as Error).message}`);
  }
}
