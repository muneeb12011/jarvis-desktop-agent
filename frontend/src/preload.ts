/**
 * The only bridge between the sandboxed HUD page and Electron.
 */

import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("hud", {
  config: (): Promise<{ token: string; backend: string }> => ipcRenderer.invoke("hud:config"),
  setLayout: (l: "panel" | "mini") => ipcRenderer.invoke("hud:setLayout", l),
  hide: () => ipcRenderer.send("hud:hide"),
  show: () => ipcRenderer.send("hud:show"),
  notify: (title: string, body: string) => ipcRenderer.send("hud:notify", title, body),
  openConfig: (which: "integrations" | "plugins") => ipcRenderer.send("hud:openConfig", which),
  openExternal: (url: string) => ipcRenderer.send("hud:openExternal", url),
  onLayout: (cb: (l: "panel" | "mini") => void) => ipcRenderer.on("hud:layout", (_e, l) => cb(l)),
  onListen: (cb: () => void) => ipcRenderer.on("hud:listen", () => cb()),
  onStop: (cb: () => void) => ipcRenderer.on("hud:stop", () => cb()),
  onTextAction: (cb: () => void) => ipcRenderer.on("hud:textAction", () => cb()),
});
