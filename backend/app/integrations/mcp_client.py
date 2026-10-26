"""
Minimal Model Context Protocol (MCP) client - stdio and streamable HTTP.

MCP is the open standard most services now ship "AI connectors" for: GitHub,
Notion, Slack, Google Drive, Spotify, Home Assistant, databases, browsers...
Point Jarvis at any MCP server in integrations.json and every tool that
server offers becomes something Jarvis can do.

Only the parts Jarvis needs are implemented: initialize, tools/list and
tools/call. Requests are synchronous (each call blocks its worker thread),
which matches how the agent runs tools.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import shutil
import subprocess
import threading
from typing import Any

import requests

from ..platform_utils import NO_WINDOW

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "jarvis", "version": "3.1"}


class MCPError(RuntimeError):
    pass


class _Base:
    def __init__(self, name: str, timeout: float = 60) -> None:
        self.name = name
        self.timeout = timeout
        self._ids = itertools.count(1)
        self.server_info: dict = {}

    def request(self, method: str, params: dict | None = None) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def notify(self, method: str, params: dict | None = None) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def start(self) -> None:
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        )
        self.server_info = result.get("serverInfo", {}) if isinstance(result, dict) else {}
        self.notify("notifications/initialized")

    def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        for _ in range(20):
            res = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(res.get("tools", []))
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, tool: str, arguments: dict) -> tuple[str, bool]:
        res = self.request("tools/call", {"name": tool, "arguments": arguments})
        parts = []
        for item in res.get("content", []):
            kind = item.get("type")
            if kind == "text":
                parts.append(item.get("text", ""))
            elif kind == "resource":
                parts.append(json.dumps(item.get("resource", {}))[:4000])
            elif kind in ("image", "audio"):
                parts.append(f"[{kind} returned]")
        if not parts and "structuredContent" in res:
            parts.append(json.dumps(res["structuredContent"])[:8000])
        return "\n".join(parts) or "(no output)", not res.get("isError", False)

    def close(self) -> None:
        pass


class StdioMCP(_Base):
    """Server launched as a local process (e.g. `npx -y @modelcontextprotocol/server-github`)."""

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float = 60,
    ) -> None:
        super().__init__(name, timeout)
        exe = shutil.which(command) or command  # resolves npx -> npx.cmd on Windows
        full_env = {**os.environ, **(env or {})}
        self.proc = subprocess.Popen(
            [exe, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
            cwd=cwd,
            creationflags=NO_WINDOW,
            bufsize=0,
        )
        self._pending: dict[int, dict] = {}
        self._events: dict[int, threading.Event] = {}
        self._lock = threading.Lock()
        self._stderr_tail: list[str] = []
        threading.Thread(target=self._read_stdout, daemon=True, name=f"mcp-{name}-out").start()
        threading.Thread(target=self._read_stderr, daemon=True, name=f"mcp-{name}-err").start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid = msg.get("id")
            if mid is not None and ("result" in msg or "error" in msg):
                with self._lock:
                    self._pending[mid] = msg
                    ev = self._events.get(mid)
                if ev:
                    ev.set()
            elif mid is not None and "method" in msg:
                # Server -> client request (e.g. ping). Answer with an empty result.
                self._write({"jsonrpc": "2.0", "id": mid, "result": {}})

    def _read_stderr(self) -> None:
        assert self.proc.stderr
        for raw in self.proc.stderr:
            self._stderr_tail.append(raw.decode("utf-8", "replace").rstrip())
            self._stderr_tail = self._stderr_tail[-20:]

    def _write(self, msg: dict) -> None:
        if self.proc.poll() is not None:
            raise MCPError(f"{self.name} exited. Last output: {' | '.join(self._stderr_tail[-5:])}")
        assert self.proc.stdin
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> Any:
        mid = next(self._ids)
        ev = threading.Event()
        with self._lock:
            self._events[mid] = ev
        self._write({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        if not ev.wait(self.timeout):
            raise MCPError(f"{self.name}: '{method}' timed out after {self.timeout:.0f}s")
        with self._lock:
            msg = self._pending.pop(mid)
            self._events.pop(mid, None)
        if "error" in msg:
            raise MCPError(f"{self.name}: {msg['error'].get('message', msg['error'])}")
        return msg.get("result", {})

    def notify(self, method: str, params: dict | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()


class HttpMCP(_Base):
    """Remote server over streamable HTTP (a single URL that accepts JSON-RPC POSTs)."""

    def __init__(self, name: str, url: str, headers: dict[str, str] | None = None, timeout: float = 60) -> None:
        super().__init__(name, timeout)
        self.url = url
        self.headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **(headers or {}),
        }
        self.session_id: str | None = None

    def _post(self, payload: dict) -> requests.Response:
        headers = dict(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        resp = requests.post(self.url, json=payload, headers=headers, timeout=self.timeout, stream=True)
        if resp.headers.get("Mcp-Session-Id"):
            self.session_id = resp.headers["Mcp-Session-Id"]
        if resp.status_code >= 400:
            raise MCPError(f"{self.name}: HTTP {resp.status_code} {resp.text[:200]}")
        return resp

    def request(self, method: str, params: dict | None = None) -> Any:
        mid = next(self._ids)
        resp = self._post({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        msg: dict | None = None
        if "text/event-stream" in resp.headers.get("Content-Type", ""):
            for line in resp.iter_lines(decode_unicode=True):
                if line and line.startswith("data:"):
                    try:
                        data = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if data.get("id") == mid:
                        msg = data
                        break
        else:
            msg = resp.json()
        resp.close()
        if not msg:
            raise MCPError(f"{self.name}: no response to {method}")
        if "error" in msg:
            raise MCPError(f"{self.name}: {msg['error'].get('message', msg['error'])}")
        return msg.get("result", {})

    def notify(self, method: str, params: dict | None = None) -> None:
        try:
            self._post({"jsonrpc": "2.0", "method": method, "params": params or {}}).close()
        except MCPError:
            pass
