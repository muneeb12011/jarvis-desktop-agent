"""
The brain: one interface over several LLM backends, all using NATIVE tool
calling (no "please answer in JSON" prompting, which is what made v2 brittle).

    Gemini            - free tier, fast, vision. Default first choice.
    OpenAI-compatible - OpenAI, Groq, OpenRouter, LM Studio, vLLM ...
    Ollama            - fully local fallback (needs a tool-capable model).

`Brain` tries providers in the order given by LLM_PROVIDERS. When one is
rate-limited (the 429s in your old logs) or unreachable, it is benched for
a cooldown and the next one answers instead, mid-conversation.

Conversation history is stored in a provider-neutral format:

    {"role": "user", "text": str, "image_b64": str | None}
    {"role": "assistant", "text": str, "tool_calls": [ToolCall], "raw": {...}, "provider": str}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import requests

from . import config

logger = logging.getLogger(__name__)


# ============================================================ types


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class AssistantTurn:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None
    provider: str = ""
    model: str = ""


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


class ProviderError(RuntimeError):
    """Non-recoverable for this request (bad key, bad request...)."""


class ProviderUnavailable(ProviderError):
    """Rate-limited, overloaded or unreachable - try the next provider."""

    def __init__(self, message: str, cooldown: float = 60.0):
        super().__init__(message)
        self.cooldown = cooldown


def _new_id() -> str:
    return "call_" + uuid.uuid4().hex[:12]


def _post(url: str, payload: dict, headers: dict | None = None, timeout: int | None = None) -> requests.Response:
    try:
        return requests.post(url, json=payload, headers=headers or {}, timeout=timeout or config.LLM_TIMEOUT_SECONDS)
    except requests.exceptions.ConnectionError as e:
        raise ProviderUnavailable(f"Can't reach {url.split('/')[2]}", cooldown=30) from e
    except requests.exceptions.Timeout as e:
        raise ProviderUnavailable(f"{url.split('/')[2]} timed out", cooldown=20) from e


def _retry_after_seconds(resp: requests.Response, default: float) -> float:
    header = resp.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', resp.text)
    if m:
        return float(m.group(1))
    return default


# ============================================================ Gemini

_GEMINI_SCHEMA_KEYS = {
    "type",
    "description",
    "properties",
    "required",
    "items",
    "enum",
    "nullable",
    "minimum",
    "maximum",
    "format",
}


def _gemini_schema(schema: Any) -> Any:
    """Gemini accepts an OpenAPI subset - strip keys it rejects."""
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for k, v in schema.items():
            if k not in _GEMINI_SCHEMA_KEYS:
                continue
            if k == "properties":
                out[k] = {pk: _gemini_schema(pv) for pk, pv in v.items()}
            elif k == "items":
                out[k] = _gemini_schema(v)
            elif k == "format" and v not in ("enum", "date-time"):
                continue
            else:
                out[k] = v
        return out
    return schema


_GEMINI_SKIP = ("image", "tts", "live", "audio", "embedding", "aqa", "learnlm", "robotics", "computer-use")
_GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider:
    """Gemini with automatic model rotation.

    The free tier has a separate *daily* quota per model. When the configured
    model runs out (429 with a retry delay of hours), Jarvis moves to the next
    flash model the key can use instead of falling back to a slow local model.
    The candidate list is discovered from the API once per session.
    """

    name = "gemini"
    supports_vision = True

    def __init__(self) -> None:
        self.model = config.GEMINI_MODEL
        self._candidates: list[str] | None = None
        self._benched: dict[str, float] = {}
        self._dead: set[str] = set()
        self._no_thinking_cfg: set[str] = set()
        self._good: str | None = None

    @property
    def configured(self) -> bool:
        return bool(config.GEMINI_API_KEY)

    def candidates(self) -> list[str]:
        if self._candidates is None:
            found: list[str] = []
            try:
                resp = requests.get(
                    f"{_GEMINI_API}/models?pageSize=200", headers={"x-goog-api-key": config.GEMINI_API_KEY}, timeout=10
                )
                for m in resp.json().get("models", []):
                    mid = m.get("name", "").split("/")[-1]
                    if (
                        "generateContent" in m.get("supportedGenerationMethods", [])
                        and "flash" in mid
                        and not any(k in mid for k in _GEMINI_SKIP)
                    ):
                        found.append(mid)
            except Exception as e:  # discovery is best effort
                logger.info("Gemini model discovery failed: %s", e)

            def rank(mid: str) -> tuple:
                nums = [int(n) for n in re.findall(r"\d+", mid)[:2]] + [0, 0]
                return (-nums[0], -nums[1], "lite" in mid, "preview" in mid or "exp" in mid, len(mid))

            extra = [m for m in config.GEMINI_FALLBACK_MODELS if m]
            ordered = [config.GEMINI_MODEL] + extra + sorted(found, key=rank)
            seen: set[str] = set()
            self._candidates = [m for m in ordered if not (m in seen or seen.add(m))][:8]
            logger.info("Gemini models in rotation: %s", ", ".join(self._candidates))
        return self._candidates

    def _usable(self) -> list[str]:
        now = time.time()
        order = list(self.candidates())
        good = self._last_good()
        if good in order:  # sticky: start with the model that answered last time
            order.remove(good)
            order.insert(0, good)
        return [m for m in order if m not in self._dead and self._benched.get(m, 0) <= now]

    def _last_good(self) -> str | None:
        if self._good is None:
            try:
                from . import memory

                self._good = memory.kv_get("gemini_last_good", "") or ""
            except Exception:
                self._good = ""
        return self._good or None

    def _remember_good(self, model: str) -> None:
        if model != self._good:
            self._good = model
            try:
                from . import memory

                memory.kv_set("gemini_last_good", model)
            except Exception:
                pass

    def _contents(self, messages: list[dict], model: str | None = None) -> list[dict]:
        contents: list[dict] = []
        model = model or self.model

        def push(role: str, parts: list[dict]) -> None:
            if not parts:
                return
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": role, "parts": parts})

        for m in messages:
            if m["role"] == "user":
                parts: list[dict] = []
                if m.get("text"):
                    parts.append({"text": m["text"]})
                if m.get("image_b64"):
                    parts.append({"inline_data": {"mime_type": "image/jpeg", "data": m["image_b64"]}})
                push("user", parts)
            elif m["role"] == "assistant":
                same_model = m.get("model", model) == model
                if m.get("provider") == "gemini" and m.get("raw") and same_model:
                    # Replay verbatim - Gemini 3 requires its own thought signatures back.
                    push("model", list(m["raw"].get("parts", [])))
                else:
                    parts = []
                    if m.get("text"):
                        parts.append({"text": m["text"]})
                    for i, tc in enumerate(m.get("tool_calls", [])):
                        part: dict[str, Any] = {"functionCall": {"name": tc.name, "args": tc.args}}
                        if i == 0:
                            part["thoughtSignature"] = "skip_thought_signature_validator"
                        parts.append(part)
                    push("model", parts)
            elif m["role"] == "tool":
                push("user", [{"functionResponse": {"name": m["name"], "response": {"result": m["content"]}}}])
        return contents

    def complete(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> AssistantTurn:
        if not self.configured:
            raise ProviderUnavailable("GEMINI_API_KEY not set", cooldown=3600)
        decls = [
            {"name": t.name, "description": t.description, "parameters": _gemini_schema(t.parameters)} for t in tools
        ]
        headers = {"x-goog-api-key": config.GEMINI_API_KEY}
        last_error = "no Gemini model available"
        for model in self._usable():
            payload: dict[str, Any] = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": self._contents(messages, model),
            }
            if decls:
                payload["tools"] = [{"functionDeclarations": decls}]
            if model.startswith("gemini-3") and model not in self._no_thinking_cfg:
                # A voice assistant needs answers in seconds; low thinking keeps Gemini 3 snappy.
                payload["generationConfig"] = {"thinkingConfig": {"thinkingLevel": "low"}}
            url = f"{_GEMINI_API}/models/{model}:generateContent"
            resp = None
            for attempt in range(3):
                try:
                    resp = _post(url, payload, headers)
                except ProviderUnavailable as e:
                    if "timed out" not in str(e):
                        raise  # offline: no point trying other models
                    self._benched[model] = time.time() + 1800
                    last_error = f"{model} timed out"
                    logger.warning("Gemini %s timed out, rotating", model)
                    resp = None
                    break
                if resp.status_code == 400 and "thinking" in resp.text.lower() and "generationConfig" in payload:
                    self._no_thinking_cfg.add(model)
                    payload.pop("generationConfig", None)
                    continue
                if resp.status_code in (429, 500, 503):
                    wait = _retry_after_seconds(resp, 2.0 * (attempt + 1))
                    if wait > 35 or attempt == 2:
                        # Three quick 429s in a row usually means this model has no free quota for this key.
                        self._benched[model] = time.time() + (max(wait, 30) if wait > 35 else 900)
                        last_error = f"{model} busy ({resp.status_code}, retry in {int(wait)}s)"
                        logger.warning("Gemini %s unavailable for %.0fs, rotating", model, wait)
                        resp = None
                        break
                    time.sleep(wait)
                    continue
                break
            if resp is None:
                continue
            if resp.status_code == 404:
                self._dead.add(model)
                last_error = f"{model} not found"
                continue
            if resp.status_code in (401, 403):
                raise ProviderUnavailable(f"Gemini rejected the API key: {resp.text[:200]}", cooldown=3600)
            if not resp.ok:
                raise ProviderError(f"Gemini error {resp.status_code}: {resp.text[:400]}")
            self.model = model
            self._remember_good(model)
            return self._parse(resp.json(), model)
        soonest = min((t for m, t in self._benched.items() if m not in self._dead), default=time.time() + 600)
        raise ProviderUnavailable(last_error, cooldown=max(30, soonest - time.time()))

    def _parse(self, data: dict, model: str) -> AssistantTurn:
        candidates = data.get("candidates") or []
        if not candidates:
            reason = data.get("promptFeedback", {}).get("blockReason", "no candidates")
            raise ProviderError(f"Gemini returned nothing ({reason})")
        content = candidates[0].get("content") or {"role": "model", "parts": []}
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for part in content.get("parts", []):
            if "functionCall" in part:
                fc = part["functionCall"]
                calls.append(ToolCall(id=fc.get("id") or _new_id(), name=fc["name"], args=fc.get("args") or {}))
            elif "text" in part and not part.get("thought"):
                text_parts.append(part["text"])
        content.setdefault("role", "model")
        return AssistantTurn(
            text="".join(text_parts).strip(), tool_calls=calls, raw=content, provider=self.name, model=model
        )


# ============================================================ OpenAI-compatible


class OpenAIProvider:
    name = "openai"

    def __init__(self) -> None:
        self.base = config.OPENAI_BASE_URL
        self.model = config.OPENAI_MODEL
        self.supports_vision = config._get_bool("OPENAI_VISION", True)

    @property
    def configured(self) -> bool:
        local = "localhost" in self.base or "127.0.0.1" in self.base
        return bool(config.OPENAI_API_KEY) or local

    def _messages(self, system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                if m.get("image_b64") and self.supports_vision:
                    out.append(
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": m.get("text") or "Screenshot:"},
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{m['image_b64']}"}},
                            ],
                        }
                    )
                else:
                    out.append({"role": "user", "content": m.get("text") or ""})
            elif m["role"] == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.get("text") or None}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.args)},
                        }
                        for tc in m["tool_calls"]
                    ]
                out.append(msg)
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        return out

    def complete(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> AssistantTurn:
        payload: dict[str, Any] = {"model": self.model, "messages": self._messages(system, messages)}
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in tools
            ]
        headers = {"Authorization": f"Bearer {config.OPENAI_API_KEY}"} if config.OPENAI_API_KEY else {}
        resp = _post(f"{self.base}/chat/completions", payload, headers)
        if resp.status_code in (429, 500, 502, 503):
            raise ProviderUnavailable(
                f"{self.base} busy ({resp.status_code})", cooldown=max(_retry_after_seconds(resp, 30), 15)
            )
        if resp.status_code in (401, 403):
            raise ProviderUnavailable(f"OpenAI-compatible endpoint rejected key: {resp.text[:200]}", cooldown=3600)
        if not resp.ok:
            raise ProviderError(f"OpenAI-compatible error {resp.status_code}: {resp.text[:400]}")
        msg = resp.json()["choices"][0]["message"]
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(id=tc.get("id") or _new_id(), name=fn.get("name", ""), args=args))
        return AssistantTurn(text=(msg.get("content") or "").strip(), tool_calls=calls, raw=msg, provider=self.name)


# ============================================================ Ollama


def _extract_inline_tool_call(text: str, tool_names: set[str]) -> ToolCall | None:
    """Small local models sometimes write the tool call as JSON in the reply
    text instead of using the tool_calls field. Recover it."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("function") or obj.get("tool")
    args = obj.get("arguments") or obj.get("parameters") or obj.get("args") or {}
    if isinstance(name, str) and name in tool_names and isinstance(args, dict):
        return ToolCall(id=_new_id(), name=name, args=args)
    return None


# A CPU-only local model can't chew through 70+ tool schemas per request in reasonable
# time, so the local fallback gets the essentials (plus any plugin/integration tools
# the conversation is clearly about would need the cloud brain anyway).
OLLAMA_CORE_TOOLS = {
    "open_app",
    "close_app",
    "open_url",
    "play_youtube",
    "web_search",
    "weather",
    "get_datetime",
    "system_status",
    "media_control",
    "set_volume",
    "set_reminder",
    "set_timer",
    "list_reminders",
    "remember",
    "recall",
    "take_note",
    "run_powershell",
    "find_files",
    "open_path",
    "read_file",
    "screenshot",
    "click_element",
    "click",
    "type_text",
    "press_keys",
    "focus_window",
    "daily_briefing",
    "news_headlines",
}


def _ollama_tool_subset(tools: list[ToolSpec]) -> list[ToolSpec]:
    if len(tools) <= 30:
        return tools
    return [t for t in tools if t.name in OLLAMA_CORE_TOOLS]


class OllamaProvider:
    name = "ollama"
    supports_vision = False  # the text model handles tools; vision goes through describe_image

    def __init__(self) -> None:
        self.model = config.OLLAMA_TEXT_MODEL

    @property
    def configured(self) -> bool:
        return True

    def _messages(self, system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m.get("text") or ""})
            elif m["role"] == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.get("text") or ""}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [
                        {"function": {"name": tc.name, "arguments": tc.args}} for tc in m["tool_calls"]
                    ]
                out.append(msg)
            elif m["role"] == "tool":
                out.append({"role": "tool", "content": m["content"], "tool_name": m["name"]})
        return out

    def complete(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> AssistantTurn:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(system, messages),
            "stream": False,
            "keep_alive": "15m",
            "options": {"temperature": 0.3, "num_ctx": 8192},
        }
        tools = _ollama_tool_subset(tools)
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in tools
            ]
        resp = _post(f"{config.OLLAMA_HOST}/api/chat", payload, timeout=max(config.LLM_TIMEOUT_SECONDS, 180))
        if resp.status_code == 404:
            raise ProviderUnavailable(
                f"Ollama model '{self.model}' not pulled. Run: ollama pull {self.model}", cooldown=300
            )
        if not resp.ok:
            raise ProviderError(f"Ollama error {resp.status_code}: {resp.text[:300]}")
        msg = resp.json().get("message", {})
        calls = [
            ToolCall(id=_new_id(), name=tc["function"]["name"], args=tc["function"].get("arguments") or {})
            for tc in msg.get("tool_calls") or []
        ]
        text = (msg.get("content") or "").strip()
        if not calls and text and tools:
            inline = _extract_inline_tool_call(text, {t.name for t in tools})
            if inline:
                calls, text = [inline], ""
        return AssistantTurn(text=text, tool_calls=calls, raw=msg, provider=self.name)


# ============================================================ router

_REGISTRY = {"gemini": GeminiProvider, "openai": OpenAIProvider, "ollama": OllamaProvider}


class Brain:
    def __init__(self, order: list[str] | None = None) -> None:
        names = order or config.LLM_PROVIDERS
        self.providers = [_REGISTRY[n]() for n in names if n in _REGISTRY]
        if not self.providers:
            self.providers = [OllamaProvider()]
        self._benched_until: dict[str, float] = {}
        self.last_provider = ""

    def reset(self) -> None:
        """Rebuild providers after keys change (the setup wizard), forgetting old failures."""
        names = [p.name for p in self.providers]
        self.providers = [_REGISTRY[n]() for n in names if n in _REGISTRY] or self.providers
        self._benched_until.clear()

    def available(self) -> list:
        now = time.time()
        return [p for p in self.providers if p.configured and self._benched_until.get(p.name, 0) <= now]

    @property
    def current(self):
        avail = self.available()
        return avail[0] if avail else self.providers[0]

    def complete(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> AssistantTurn:
        candidates = self.available() or [p for p in self.providers if p.configured] or self.providers
        errors: list[str] = []
        for provider in candidates:
            try:
                turn = provider.complete(system, messages, tools)
                self.last_provider = provider.name
                return turn
            except ProviderUnavailable as e:
                logger.warning("%s unavailable: %s (benched %.0fs)", provider.name, e, e.cooldown)
                self._benched_until[provider.name] = time.time() + e.cooldown
                errors.append(f"{provider.name}: {e}")
        raise ProviderError("No AI provider is available right now. " + " | ".join(errors))

    def status(self) -> list[dict]:
        now = time.time()
        return [
            {
                "name": p.name,
                "model": getattr(p, "model", ""),
                "configured": p.configured,
                "benched_for": max(0, int(self._benched_until.get(p.name, 0) - now)),
            }
            for p in self.providers
        ]


# ============================================================ vision helper


def describe_image(image_b64: str, question: str) -> str:
    """For brains without vision: get a text description of a screenshot from
    whichever vision model is reachable (Gemini first, then local Ollama)."""
    prompt = (
        "You are the eyes of a desktop assistant. Describe this screenshot precisely and literally: "
        "which app/window is in front, key visible text, buttons, fields, dialogs, and where they are "
        "(approximate x,y in image pixels). Only describe what is actually visible. "
        f"Focus on what's relevant to: {question}"
    )
    if config.GEMINI_API_KEY:
        try:
            g = GeminiProvider()
            turn = g.complete(
                "You describe screenshots.", [{"role": "user", "text": prompt, "image_b64": image_b64}], []
            )
            if turn.text:
                return turn.text
        except ProviderError as e:
            logger.info("Gemini vision fallback failed: %s", e)
    try:
        resp = _post(
            f"{config.OLLAMA_HOST}/api/chat",
            {
                "model": config.OLLAMA_VISION_MODEL,
                "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
                "stream": False,
                "options": {"temperature": 0.1},
            },
            timeout=240,
        )
        if resp.ok:
            return resp.json().get("message", {}).get("content", "").strip() or "(empty description)"
        return f"(vision model error {resp.status_code})"
    except ProviderUnavailable as e:
        return f"(no vision model reachable: {e})"
