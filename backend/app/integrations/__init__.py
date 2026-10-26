"""
Integration system - three ways to plug anything into Jarvis, no core edits:

1. MCP servers   (integrations.json -> "mcp_servers")
   Any Model Context Protocol server: GitHub, Notion, Slack, Google Drive,
   Spotify, Home Assistant, Postgres, Playwright... local (command) or
   remote (url). Every tool the server exposes becomes a Jarvis tool.

2. HTTP actions  (integrations.json -> "http_actions")
   Any REST API or webhook, declared in JSON: Home Assistant, n8n, Zapier,
   IFTTT, your own backend. {param} placeholders are filled by Jarvis.

3. Python plugins (backend/plugins/*.py)
   Write a function, decorate it with @tool, drop the file in. Full Python.

Secrets: write ${NAME} in integrations.json and put NAME=... in backend/.env,
so tokens never live in the JSON file.

Reload without restarting: Settings > Integrations > Reload, or say
"Jarvis, reload integrations".
"""

from __future__ import annotations

import concurrent.futures as cf
import importlib.util
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import requests

from .. import config
from .. import tools as tools_pkg
from ..tools import REGISTRY, Tool, ToolResult
from .mcp_client import HttpMCP, MCPError, StdioMCP, _Base

logger = logging.getLogger(__name__)

INTEGRATIONS_FILE = config.DATA_DIR / "integrations.json"
PLUGINS_DIR = config.DATA_DIR / "plugins"

_clients: dict[str, _Base] = {}
_status: list[dict] = []

_MUTATING = re.compile(
    r"(create|delete|remove|send|post|update|write|edit|push|merge|move|rename|pay|"
    r"transfer|archive|close|cancel|invite|publish|comment|reply|upload|set_|add_)",
    re.I,
)


# ------------------------------------------------------------------ helpers


def _expand_env(value: Any) -> Any:
    """Replace ${NAME} with environment values (backend/.env is already loaded)."""
    if isinstance(value, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _fill(value: Any, args: dict) -> Any:
    """Fill {param} placeholders. A string that is exactly "{param}" keeps the arg's type."""
    if isinstance(value, str):
        whole = re.fullmatch(r"\{(\w+)\}", value)
        if whole and whole.group(1) in args:
            return args[whole.group(1)]
        return re.sub(r"\{(\w+)\}", lambda m: str(args.get(m.group(1), m.group(0))), value)
    if isinstance(value, dict):
        return {k: _fill(v, args) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, args) for v in value]
    return value


def _safe_name(*parts: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]", "_", "_".join(p for p in parts if p))
    if not re.match(r"[a-zA-Z_]", name):
        name = "x_" + name
    return name[:64]


def _object_schema(schema: Any) -> dict:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return {"type": "object", "properties": {}}
    out = {"type": "object", "properties": schema.get("properties", {}) or {}}
    if schema.get("required"):
        out["required"] = [r for r in schema["required"] if r in out["properties"]]
    return out


def _seed_data_dir() -> None:
    """Installed builds: give a fresh data folder the example integrations file and plugins once."""
    if config.DATA_DIR == config.BACKEND_DIR:
        return
    src = config.BACKEND_DIR / "integrations.json"
    if not INTEGRATIONS_FILE.exists() and src.exists():
        INTEGRATIONS_FILE.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    if not PLUGINS_DIR.exists() and (config.BACKEND_DIR / "plugins").is_dir():
        PLUGINS_DIR.mkdir(parents=True)
        for f in (config.BACKEND_DIR / "plugins").glob("*.py"):
            (PLUGINS_DIR / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")


def read_config() -> dict:
    _seed_data_dir()
    if not INTEGRATIONS_FILE.exists():
        return {"mcp_servers": {}, "http_actions": []}
    try:
        return json.loads(INTEGRATIONS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"integrations.json is not valid JSON: {e}") from e


# ------------------------------------------------------------------ MCP


def _mcp_confirm_rule(server: str, tool: dict, mode: str):
    if mode == "never":
        return None
    if mode == "always":
        return lambda a: f"Let {server} run '{tool['name']}'?\n{json.dumps(a)[:300]}"
    ann = tool.get("annotations") or {}
    risky = ann.get("destructiveHint") is True or (
        ann.get("readOnlyHint") is not True and bool(_MUTATING.search(tool["name"]))
    )
    if not risky:
        return None
    return lambda a: f"Let {server} run '{tool['name']}'?\n{json.dumps(a)[:300]}"


def _load_mcp(name: str, spec: dict) -> dict:
    spec = _expand_env(spec)
    timeout = float(spec.get("timeout", 60))
    if spec.get("url"):
        client: _Base = HttpMCP(name, spec["url"], spec.get("headers"), timeout)
    elif spec.get("command"):
        client = StdioMCP(name, spec["command"], spec.get("args", []), spec.get("env"), spec.get("cwd"), timeout)
    else:
        raise ValueError("needs either 'command' or 'url'")
    try:
        client.start()
        tools = client.list_tools()
    except Exception:
        client.close()
        raise
    _clients[name] = client
    allow = set(spec.get("only_tools") or [])
    mode = spec.get("confirm", "auto")
    count = 0
    for t in tools:
        if allow and t["name"] not in allow:
            continue
        tool_name = _safe_name(name, t["name"])

        def run(_client=client, _tool=t["name"], **kwargs) -> ToolResult:
            try:
                text, ok = _client.call_tool(_tool, kwargs)
            except MCPError as e:
                return ToolResult(str(e), ok=False)
            return ToolResult(text[:12000], ok=ok)

        REGISTRY[tool_name] = Tool(
            name=tool_name,
            description=f"[{name}] {(t.get('description') or t['name'])[:900]}",
            parameters=_object_schema(t.get("inputSchema")),
            func=run,
            category=f"mcp:{name}",
            confirm=_mcp_confirm_rule(name, t, mode),
            mutates=True,
            source=f"mcp:{name}",
        )
        count += 1
    return {"tools": count, "detail": client.server_info.get("name", "")}


# ------------------------------------------------------------------ HTTP actions


def _load_http_action(action: dict) -> int:
    name = _safe_name(action["name"])
    params = action.get("params", {})
    required = [k for k, v in params.items() if not v.get("optional")]
    props = {k: {kk: vv for kk, vv in v.items() if kk != "optional"} for k, v in params.items()}

    def run(**kwargs) -> ToolResult:
        spec = _expand_env(action)
        method = spec.get("method", "POST").upper()
        url = _fill(spec["url"], kwargs)
        headers = _fill(spec.get("headers", {}), kwargs)
        body = _fill(spec.get("body"), kwargs) if spec.get("body") is not None else None
        query = _fill(spec.get("query"), kwargs) if spec.get("query") else None
        body_text = _fill(spec.get("body_text"), kwargs) if spec.get("body_text") is not None else None
        resp = requests.request(
            method,
            url,
            headers=headers,
            params=query,
            json=body if body_text is None and method not in ("GET", "DELETE") else None,
            data=body_text.encode("utf-8") if isinstance(body_text, str) else None,
            timeout=float(spec.get("timeout", 20)),
        )
        text = resp.text[:6000] or "(empty response)"
        return ToolResult(f"HTTP {resp.status_code}\n{text}", ok=resp.ok)

    confirm = action.get("confirm", False)
    REGISTRY[name] = Tool(
        name=name,
        description=action.get("description", f"Call {action['url']}"),
        parameters={"type": "object", "properties": props, **({"required": required} if required else {})},
        func=run,
        category="http",
        confirm=(lambda a, n=name: f"Run {n} with {json.dumps(a)[:200]}?") if confirm else None,
        mutates=True,
        source=f"http:{name}",
    )
    return 1


# ------------------------------------------------------------------ plugins


def _load_plugin(path: Path) -> int:
    before = set(REGISTRY)
    mod_name = f"jarvis_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if not spec or not spec.loader:
        raise ImportError("can't load")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    tools_pkg.set_source(f"plugin:{path.stem}")
    try:
        spec.loader.exec_module(module)
    finally:
        tools_pkg.set_source("builtin")
    return len(set(REGISTRY) - before)


# ------------------------------------------------------------------ orchestration


def shutdown() -> None:
    for c in _clients.values():
        try:
            c.close()
        except Exception:
            pass
    _clients.clear()


def load() -> list[dict]:
    """(Re)load every integration. Blocking - call from a worker thread."""
    global _status
    tools_pkg.load_all()
    shutdown()
    for key in [k for k, t in REGISTRY.items() if t.source != "builtin"]:
        REGISTRY.pop(key, None)

    status: list[dict] = []
    if str(config.BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(config.BACKEND_DIR))

    _seed_data_dir()
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(PLUGINS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            n = _load_plugin(path)
            status.append({"name": path.stem, "kind": "plugin", "status": "connected", "tools": n})
        except Exception as e:
            logger.exception("Plugin %s failed", path.name)
            status.append(
                {
                    "name": path.stem,
                    "kind": "plugin",
                    "status": "error",
                    "tools": 0,
                    "error": f"{type(e).__name__}: {e}",
                }
            )

    try:
        cfg = read_config()
    except ValueError as e:
        status.append({"name": "integrations.json", "kind": "config", "status": "error", "tools": 0, "error": str(e)})
        _status = status
        return status

    for action in cfg.get("http_actions", []):
        label = action.get("name", "?")
        if not action.get("enabled", True):
            status.append({"name": label, "kind": "http", "status": "disabled", "tools": 0})
            continue
        try:
            _load_http_action(action)
            status.append({"name": label, "kind": "http", "status": "connected", "tools": 1})
        except Exception as e:
            status.append({"name": label, "kind": "http", "status": "error", "tools": 0, "error": str(e)})

    servers = {n: s for n, s in cfg.get("mcp_servers", {}).items() if not n.startswith("_")}
    for name, spec in servers.items():
        if not spec.get("enabled", True):
            status.append({"name": name, "kind": "mcp", "status": "disabled", "tools": 0})
    enabled = {n: s for n, s in servers.items() if s.get("enabled", True)}
    if enabled:
        with cf.ThreadPoolExecutor(max_workers=min(8, len(enabled))) as pool:
            futures = {pool.submit(_load_mcp, n, s): n for n, s in enabled.items()}
            for fut in cf.as_completed(futures):
                name = futures[fut]
                try:
                    info = fut.result()
                    status.append({"name": name, "kind": "mcp", "status": "connected", **info})
                    logger.info("MCP %s connected: %d tools", name, info["tools"])
                except Exception as e:
                    logger.warning("MCP %s failed: %s", name, e)
                    status.append(
                        {
                            "name": name,
                            "kind": "mcp",
                            "status": "error",
                            "tools": 0,
                            "error": f"{type(e).__name__}: {e}"[:300],
                        }
                    )
    _status = status
    return status


def current_status() -> list[dict]:
    return list(_status)
