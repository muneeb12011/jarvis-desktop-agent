"""
First-run setup: what the HUD's welcome wizard reads and writes.

Keys and identity live in the .env file in the data folder (so they survive
updates); the wizard writes them there and applies them immediately, without
a restart.
"""

from __future__ import annotations

import logging
import os
import re

import requests

from . import config, memory, providers

logger = logging.getLogger(__name__)

_ENV_KEYS = {
    "gemini_key": ("GEMINI_API_KEY", "GEMINI_API_KEY"),
    "user_name": ("JARVIS_USER_NAME", "USER_NAME"),
    "user_title": ("JARVIS_USER_TITLE", "USER_TITLE"),
    "city": ("JARVIS_CITY", "HOME_CITY"),
}

# Every brain Jarvis can think with. `key_attr` is None for providers that need no key (local Ollama).
# `base_editable` lets the user point the provider at their own endpoint (LM Studio, vLLM, a remote Ollama).
PROVIDERS: list[dict] = [
    {
        "id": "gemini",
        "label": "Google Gemini",
        "free": True,
        "key_attr": "GEMINI_API_KEY",
        "key_env": "GEMINI_API_KEY",
        "model_attr": "GEMINI_MODEL",
        "model_env": "GEMINI_MODEL",
        "default_model": "gemini-2.5-flash",
        "base_attr": None,
        "base_env": None,
        "base_editable": False,
        "key_url": "https://aistudio.google.com/apikey",
        "note": "Free tier with daily limits. Jarvis rotates between Flash models when one runs out.",
    },
    {
        "id": "groq",
        "label": "Groq",
        "free": True,
        "key_attr": "GROQ_API_KEY",
        "key_env": "GROQ_API_KEY",
        "model_attr": "GROQ_MODEL",
        "model_env": "GROQ_MODEL",
        "default_model": "llama-3.3-70b-versatile",
        "base_attr": "GROQ_BASE_URL",
        "base_env": "GROQ_BASE_URL",
        "base_editable": False,
        "key_url": "https://console.groq.com/keys",
        "note": "Free tier and very fast. Choose a model that supports tool calling.",
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "free": True,
        "key_attr": "OPENROUTER_API_KEY",
        "key_env": "OPENROUTER_API_KEY",
        "model_attr": "OPENROUTER_MODEL",
        "model_env": "OPENROUTER_MODEL",
        "default_model": "openai/gpt-4o-mini",
        "base_attr": "OPENROUTER_BASE_URL",
        "base_env": "OPENROUTER_BASE_URL",
        "base_editable": False,
        "key_url": "https://openrouter.ai/keys",
        "note": "One key, hundreds of models, some of them free. Paste any model id.",
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "free": False,
        "key_attr": "DEEPSEEK_API_KEY",
        "key_env": "DEEPSEEK_API_KEY",
        "model_attr": "DEEPSEEK_MODEL",
        "model_env": "DEEPSEEK_MODEL",
        "default_model": "deepseek-chat",
        "base_attr": "DEEPSEEK_BASE_URL",
        "base_env": "DEEPSEEK_BASE_URL",
        "base_editable": False,
        "key_url": "https://platform.deepseek.com/api_keys",
        "note": "Cheap and strong. Use deepseek-chat: the reasoning-only model can't drive tools.",
    },
    {
        "id": "anthropic",
        "label": "Claude",
        "free": False,
        "key_attr": "ANTHROPIC_API_KEY",
        "key_env": "ANTHROPIC_API_KEY",
        "model_attr": "ANTHROPIC_MODEL",
        "model_env": "ANTHROPIC_MODEL",
        "default_model": "claude-sonnet-5-5",
        "base_attr": None,
        "base_env": None,
        "base_editable": False,
        "key_url": "https://console.anthropic.com/settings/keys",
        "note": "Anthropic's Claude. Excellent at multi-step tasks. Needs a paid API key.",
    },
    {
        "id": "openai",
        "label": "OpenAI or compatible",
        "free": False,
        "key_attr": "OPENAI_API_KEY",
        "key_env": "OPENAI_API_KEY",
        "model_attr": "OPENAI_MODEL",
        "model_env": "OPENAI_MODEL",
        "default_model": "gpt-4o-mini",
        "base_attr": "OPENAI_BASE_URL",
        "base_env": "OPENAI_BASE_URL",
        "base_editable": True,
        "key_url": "https://platform.openai.com/api-keys",
        "note": "GPT models, or any OpenAI-compatible endpoint such as LM Studio (change the address).",
    },
    {
        "id": "ollama",
        "label": "Ollama (on this PC)",
        "free": True,
        "key_attr": None,
        "key_env": None,
        "model_attr": "OLLAMA_TEXT_MODEL",
        "model_env": "OLLAMA_TEXT_MODEL",
        "default_model": "qwen2.5:7b",
        "base_attr": "OLLAMA_HOST",
        "base_env": "OLLAMA_HOST",
        "base_editable": True,
        "key_url": "https://ollama.com/download",
        "note": "Fully private, no key and no quota. Needs a model with tool calling and, ideally, a GPU.",
    },
]
_BY_ID = {p["id"]: p for p in PROVIDERS}

_KEY_RE = re.compile(r"[A-Za-z0-9._\-]{8,400}")
_MODEL_RE = re.compile(r"[A-Za-z0-9._:/\-]{1,100}")
_BASE_RE = re.compile(r"https?://[^\s\"']{3,200}")


def is_done() -> bool:
    flag = memory.kv_get("setup_done")
    if flag is not None:
        return flag == "1"
    # Installs from before the wizard existed: a working key means they're set up already.
    return any(getattr(config, spec["key_attr"], "") for spec in PROVIDERS if spec["key_attr"])


def state() -> dict:
    key = config.GEMINI_API_KEY
    return {
        "done": is_done(),
        "has_key": bool(key),
        "key_hint": f"...{key[-4:]}" if len(key) > 8 else "",
        "user_name": config.USER_NAME,
        "user_title": config.USER_TITLE,
        "city": config.HOME_CITY,
    }


def write_env(updates: dict[str, str], path=None) -> None:
    """Set KEY=value lines in the .env file, keeping every other line and comment as it was."""
    path = path or config.ENV_FILE
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out = []
    for line in lines:
        m = re.match(r"\s*([A-Z0-9_]+)\s*=", line)
        if m and m.group(1) in remaining:
            out.append(f"{m.group(1)}={remaining.pop(m.group(1))}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in remaining.items()]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def test_gemini_key(key: str) -> tuple[bool, str]:
    key = key.strip()
    if not re.fullmatch(r"[\w.\-]{20,200}", key):
        return False, "That doesn't look like a Gemini API key. Copy it again from AI Studio."
    try:
        r = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=5",
            headers={"x-goog-api-key": key},
            timeout=12,
        )
    except requests.RequestException as e:
        return False, f"Couldn't reach Google to check the key ({type(e).__name__}). Check your internet."
    if r.status_code == 200:
        return True, "Key works."
    if r.status_code in (400, 401, 403):
        return False, "Google rejected that key. Copy it again from AI Studio."
    return False, f"Google answered {r.status_code}; try again in a minute."


def save(values: dict, brain=None) -> dict:
    """Apply wizard answers. Returns {"ok", "message", "state"}."""
    env_updates: dict[str, str] = {}
    if values.get("gemini_key"):
        ok, msg = test_gemini_key(str(values["gemini_key"]))
        if not ok:
            return {"ok": False, "message": msg, "state": state()}
    for field, (env_name, attr) in _ENV_KEYS.items():
        if field in values and values[field] is not None:
            v = str(values[field]).strip().replace("\n", " ")
            if field == "gemini_key" and not v:
                continue
            env_updates[env_name] = v
            setattr(config, attr, v)
            os.environ[env_name] = v
    if env_updates:
        write_env(env_updates)
        if "user_title" in values or "city" in values or "user_name" in values:
            from . import settings

            settings.update({k: str(values[k]) for k in ("user_title", "city", "user_name") if k in values})
    if values.get("gemini_key") and brain is not None:
        brain.reset()
    if values.get("done"):
        memory.kv_set("setup_done", "1")
    elif values.get("restart"):
        memory.kv_set("setup_done", "0")
    return {"ok": True, "message": "Saved.", "state": state()}


# ---------------------------------------------------------------- choosing a brain


def _canon(name: str) -> str:
    n = str(name).strip().lower()
    return providers._ALIASES.get(n, n)


def _active_order() -> list[str]:
    """The providers currently switched on, in failover order."""
    order: list[str] = []
    for raw in config.LLM_PROVIDERS:
        pid = _canon(raw)
        if pid in _BY_ID and pid not in order:
            order.append(pid)
    return order


def providers_state() -> dict:
    """What the Settings sheet shows. Never includes a full key, only the last four characters."""
    order = _active_order()
    rows = []
    for spec in PROVIDERS:
        key = getattr(config, spec["key_attr"], "") if spec["key_attr"] else ""
        row = {
            "id": spec["id"],
            "label": spec["label"],
            "free": spec["free"],
            "needs_key": bool(spec["key_attr"]),
            "has_key": bool(key),
            "key_hint": f"...{key[-4:]}" if len(key) > 8 else "",
            "key_url": spec["key_url"],
            "note": spec["note"],
            "model": getattr(config, spec["model_attr"], "") or spec["default_model"],
            "default_model": spec["default_model"],
            "base": getattr(config, spec["base_attr"], "") if spec["base_attr"] else "",
            "base_editable": spec["base_editable"],
            "enabled": spec["id"] in order,
        }
        rows.append(row)
    rows.sort(key=lambda r: (0, order.index(r["id"])) if r["id"] in order else (1, 0))
    return {"providers": rows, "order": order}


def _get(url: str, headers: dict | None = None):
    try:
        return requests.get(url, headers=headers or {}, timeout=12), ""
    except requests.RequestException as e:
        return None, f"Couldn't reach {url.split('/')[2]} ({type(e).__name__}). Check your internet."


def _test_ollama(host: str, model: str) -> tuple[bool, str, list[str]]:
    host = host.rstrip("/")
    resp, err = _get(f"{host}/api/tags")
    if resp is None:
        return False, f"Ollama isn't answering at {host}. Install it from ollama.com and start it.", []
    if resp.status_code != 200:
        return False, f"Ollama answered {resp.status_code} at {host}.", []
    try:
        names = [m.get("name", "") for m in resp.json().get("models", []) if m.get("name")]
    except ValueError:
        return False, f"That address answered, but it doesn't look like Ollama ({host}).", []
    if not names:
        return False, f"Ollama is running but has no models yet. Run: ollama pull {model or 'qwen2.5:7b'}", []
    if model and model not in names and f"{model}:latest" not in names:
        return True, f"Ollama is running, but '{model}' isn't installed yet. Run: ollama pull {model}", names
    return True, f"Ollama is running ({len(names)} model{'s' if len(names) != 1 else ''} installed).", names


def test_provider(pid: str, key: str = "", model: str = "", base: str = "") -> tuple[bool, str, list[str]]:
    """Check a key (or a local server) against the service it belongs to.

    Returns (ok, message, models). `models` is only filled for Ollama, to fill the model dropdown.
    An empty key tests the key that is already saved.
    """
    spec = _BY_ID.get(pid)
    if not spec:
        return False, "I don't know that provider.", []
    label = spec["label"]
    if pid == "ollama":
        return _test_ollama(base or config.OLLAMA_HOST, model or config.OLLAMA_TEXT_MODEL)

    key = (key or "").strip() or getattr(config, spec["key_attr"], "")
    if pid == "gemini":
        return (*test_gemini_key(key), [])

    base = (base or (getattr(config, spec["base_attr"], "") if spec["base_attr"] else "")).rstrip("/")
    local = pid == "openai" and ("localhost" in base or "127.0.0.1" in base)
    if not key and not local:
        return False, f"Paste your {label} key first.", []
    if key and not _KEY_RE.fullmatch(key):
        return False, f"That doesn't look like a {label} key. Copy it again from the provider's page.", []
    if base and not _BASE_RE.fullmatch(base):
        return False, "That address doesn't look right. It should start with http:// or https://.", []

    headers: dict[str, str] = {}
    if pid == "anthropic":
        url = f"{providers._ANTHROPIC_API}/models?limit=1"
        headers = {"x-api-key": key, "anthropic-version": providers._ANTHROPIC_VERSION}
    else:
        # OpenRouter's /models is public, so it can't tell a bad key from a good one; /auth/key can.
        url = f"{base}/auth/key" if pid == "openrouter" else f"{base}/models"
        if key:
            headers = {"Authorization": f"Bearer {key}"}
    resp, err = _get(url, headers)
    if resp is None:
        return False, err, []
    if resp.status_code == 200:
        return True, "Key works.", []
    if resp.status_code in (401, 403):
        return False, f"{label} rejected that key. Copy it again from the provider's page.", []
    if resp.status_code == 429:
        return True, f"{label} accepted the key but is rate-limiting right now.", []
    return False, f"{label} answered {resp.status_code}; try again in a minute.", []


def save_providers(values: dict, brain=None) -> dict:
    """Apply the Settings sheet's brain choices, live, without a restart.

    values: {"keys": {id: key}, "models": {id: model}, "bases": {id: url},
             "clear_keys": [id], "order": [id, ...]}
    Every new key is checked against its service first; if anything fails, nothing is saved.
    A blank key means "leave the saved key alone". `order` is the failover order and the on/off switch:
    a provider that is not listed is off. Without `order`, a provider that just got a key is added.
    """
    keys = dict(values.get("keys") or {})
    models = dict(values.get("models") or {})
    bases = dict(values.get("bases") or {})
    clear = [str(p) for p in (values.get("clear_keys") or [])]

    def fail(message: str, pid: str = "") -> dict:
        return {"ok": False, "message": message, "provider": pid, **providers_state()}

    for group in (keys, models, bases):
        for pid in group:
            if pid not in _BY_ID:
                return fail("I don't know that provider.", str(pid))
    for pid in clear:
        if pid not in _BY_ID:
            return fail("I don't know that provider.", pid)

    order_in = values.get("order")
    if order_in is not None:
        order: list[str] = []
        for raw in order_in:
            pid = _canon(raw)
            if pid not in _BY_ID:
                return fail(f"I don't know a provider called '{raw}'.")
            if pid not in order:
                order.append(pid)
        if not order:
            return fail("Keep at least one brain switched on.")
    else:
        order = _active_order()

    for pid, model in models.items():
        if model and not _MODEL_RE.fullmatch(str(model).strip()):
            return fail("That model name has characters I can't use.", pid)
    for pid, base in bases.items():
        if base and not _BASE_RE.fullmatch(str(base).strip()):
            return fail("That address doesn't look right. It should start with http:// or https://.", pid)

    # Check every new key against its provider before touching anything.
    for pid, key in keys.items():
        key = str(key).strip()
        if not key or not _BY_ID[pid]["key_attr"]:
            continue
        ok, msg, _ = test_provider(pid, key, str(models.get(pid, "")).strip(), str(bases.get(pid, "")).strip())
        if not ok:
            return fail(msg, pid)

    env_updates: dict[str, str] = {}

    def apply(env_name: str, attr: str, value: str) -> None:
        env_updates[env_name] = value
        setattr(config, attr, value)
        os.environ[env_name] = value

    for pid, key in keys.items():
        spec = _BY_ID[pid]
        key = str(key).strip()
        if key and spec["key_attr"]:
            apply(spec["key_env"], spec["key_attr"], key)
            if order_in is None and pid not in order:
                order.append(pid)
    for pid in clear:
        spec = _BY_ID[pid]
        if spec["key_attr"]:
            apply(spec["key_env"], spec["key_attr"], "")
    for pid, model in models.items():
        model = str(model).strip()
        if model:
            apply(_BY_ID[pid]["model_env"], _BY_ID[pid]["model_attr"], model)
    for pid, base in bases.items():
        spec = _BY_ID[pid]
        base = str(base).strip().rstrip("/")
        if base and spec["base_attr"] and spec["base_editable"]:
            apply(spec["base_env"], spec["base_attr"], base)

    config.LLM_PROVIDERS = list(order)
    env_updates["LLM_PROVIDERS"] = ",".join(order)
    os.environ["LLM_PROVIDERS"] = env_updates["LLM_PROVIDERS"]
    write_env(env_updates)

    if brain is not None:
        brain.providers = providers._build(order) or brain.providers
        brain.reset()

    message = "Saved."
    keyless = [
        _BY_ID[p]["label"] for p in order if _BY_ID[p]["key_attr"] and not getattr(config, _BY_ID[p]["key_attr"], "")
    ]
    if keyless:
        message += f" {', '.join(keyless)} still needs a key, so I'll skip it until you add one."
    return {"ok": True, "message": message, **providers_state()}
