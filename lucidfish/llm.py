# Copyright (C) 2026 Sam Struthers
# SPDX-License-Identifier: AGPL-3.0-or-later
# This file is part of Lucidfish <https://github.com/s4mstruthers/Lucidfish>; see LICENSE for the terms.

"""LLM providers: one small interface over local and cloud models.

Supported out of the box:

- **Ollama** (local, default) — private and free, but uses your CPU/GPU.
- **Any OpenAI-compatible server** — LM Studio, llama.cpp, vLLM, ... (local).
- **OpenAI**, **Google Gemini**, **OpenRouter**, **Groq** — cloud, via the
  OpenAI-compatible Chat Completions API.
- **Anthropic (Claude)** — cloud, via the official ``anthropic`` SDK.

Design rule: the LLM NEVER analyses the position itself. It receives verified
evidence (see prompts.py) and its only job is to narrate that evidence.

Every provider offers the same three things through :meth:`LLMProvider.complete`:

- plain text,
- **structured output**: a JSON schema the answer must follow (Ollama ``format``,
  OpenAI ``response_format``, Anthropic ``output_config.format``), so notes,
  commentary and reviews are parsed instead of scraped with regular expressions,
- **tools**: the model may call a function (Lucidfish gives it ``check_line``,
  which runs Stockfish on any variation) before it answers; :func:`run_tools`
  drives that loop the same way for every provider.

A provider that rejects one of these (an Ollama model without tool support, a
server without JSON-schema mode, a Claude model that rejects a parameter) is
asked again without it and the rejection is remembered, so one failed request
never becomes one per move.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests

from . import __version__
from .config import LLMConfig
from .credentials import get_key, redact
from .schemas import parse_json
from .spelling import british


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    kind: str                    # "ollama" | "openai" | "anthropic"
    base_url: str
    default_model: str
    key_env: str | None          # environment variable holding the API key
    needs_key: bool
    local: bool                  # runs on this machine (private, uses local compute)
    concurrency: int             # parallel requests that help rather than hurt
    models: tuple[str, ...] = ()
    key_url: str = ""            # where users create an API key
    note: str = ""


PROVIDERS: dict[str, ProviderSpec] = {p.id: p for p in (
    ProviderSpec("ollama", "Ollama (local)", "ollama", "http://localhost:11434", "qwen3:8b",
                 None, False, True, 1,
                 ("qwen3:8b", "qwen3:4b", "qwen3:14b", "qwen2.5:7b", "qwen2.5:14b", "llama3.1:8b",
                  "gemma3:12b"),
                 "https://ollama.com/download",
                 "Runs on your computer: private and free, but CPU/GPU intensive. qwen3 models can also "
                 "check lines with the engine (tool use); gemma3 cannot."),
    ProviderSpec("openai", "OpenAI", "openai", "https://api.openai.com/v1", "gpt-4.1-mini",
                 "OPENAI_API_KEY", True, False, 4,
                 ("gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini"),
                 "https://platform.openai.com/api-keys"),
    ProviderSpec("anthropic", "Anthropic (Claude)", "anthropic", "https://api.anthropic.com",
                 "claude-opus-5-5", "ANTHROPIC_API_KEY", True, False, 4,
                 ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5",
                  "claude-sonnet-5", "claude-haiku-4-5"),
                 "https://console.anthropic.com/settings/keys",
                 "claude-haiku-5-5 is the cheap choice for running commentary; claude-opus-5-5 or "
                 "claude-sonnet-5-5 as the second model for the hard parts."),
    ProviderSpec("gemini", "Google Gemini", "openai",
                 "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash",
                 "GEMINI_API_KEY", True, False, 4,
                 ("gemini-2.5-flash", "gemini-2.5-pro"),
                 "https://aistudio.google.com/apikey"),
    ProviderSpec("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1",
                 "openai/gpt-4.1-mini", "OPENROUTER_API_KEY", True, False, 4,
                 ("openai/gpt-4.1-mini", "anthropic/claude-sonnet-5", "google/gemini-2.5-flash",
                  "meta-llama/llama-3.3-70b-instruct"),
                 "https://openrouter.ai/keys", "One key for hundreds of models."),
    ProviderSpec("groq", "Groq", "openai", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile",
                 "GROQ_API_KEY", True, False, 4,
                 ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"),
                 "https://console.groq.com/keys", "Very fast open-weight models."),
    ProviderSpec("custom", "Custom OpenAI-compatible", "openai", "http://localhost:1234/v1", "",
                 "LUCIDFISH_CUSTOM_API_KEY", False, True, 1, (),
                 "", "LM Studio, llama.cpp server, vLLM, LocalAI, ... (key optional)."),
)}


class LLMError(RuntimeError):
    """A user-presentable LLM failure. `fatal` errors (bad key, unknown model,
    server unreachable) will not fix themselves by retrying the next move."""

    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


class ToolsUnsupported(LLMError):
    """The model or server cannot take tools; the caller asks again without them."""


_THINK_RE = re.compile(r"<think>.*?</think>|^.*?</think>", re.S)


def clean_output(text: str | None) -> str:
    """Strip reasoning blocks some local models (qwen3, deepseek-r1) emit inline, and make the spelling
    British (models trained mostly on American text slip even when asked)."""
    return british(_THINK_RE.sub("", text or "").strip())


def _normalise_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url.replace("://0.0.0.0", "://127.0.0.1")


# ---------------------------------------------------------------- structured output + tools

@dataclass(frozen=True)
class ToolSpec:
    """A function the model may call. `input_schema` is a JSON schema for its arguments."""
    name: str
    description: str
    input_schema: dict


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Reply:
    """One model answer: its text and the tools it asked to run (if any)."""
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    assistant: Any = None          # the provider-native assistant message, to replay on tool rounds


def _tool_json(call_input: Any) -> dict:
    if isinstance(call_input, dict):
        return call_input
    if isinstance(call_input, str):
        try:
            data = json.loads(call_input)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}
    return {}


# ---------------------------------------------------------------- base class

class LLMProvider(ABC):
    """Common interface. Instances are cheap and safe to share between threads."""

    def __init__(self, spec: ProviderSpec, cfg: LLMConfig):
        self.spec = spec
        self.cfg = cfg
        self.model = cfg.model or spec.default_model
        env_url = os.environ.get("OLLAMA_HOST", "") if spec.id == "ollama" else ""
        self.base_url = _normalise_url(cfg.base_url or env_url or spec.base_url)
        self.api_key = cfg.api_key or get_key(spec.id, spec.key_env).key
        if spec.needs_key and not self.api_key:
            raise LLMError(f"No API key set for {spec.label}. Add one in Settings → AI coach, or set "
                           f"the {spec.key_env} environment variable.", fatal=True)
        if not self.model:
            raise LLMError(f"Choose a model for {spec.label} in Settings → AI coach.", fatal=True)

    @property
    def concurrency(self) -> int:
        return max(1, self.cfg.concurrency or self.spec.concurrency)

    def describe(self) -> str:
        return f"{self.spec.label} · {self.model}"

    @abstractmethod
    def complete(self, system: str, messages: list[dict], *, max_tokens: int | None = None,
                 schema: dict | None = None, tools: list[ToolSpec] | None = None,
                 effort: str | None = None) -> Reply:
        """Send a conversation. `schema` asks for JSON of that shape; `tools` lets the model call
        functions (the reply then carries `tool_calls` to run); `effort` is how hard to think
        ('low' / 'medium' / 'high', honoured where the model supports it)."""

    def tool_turn(self, reply: Reply, results: list[tuple[ToolCall, str]]) -> list[dict]:
        """The messages that put a tool round into the conversation: the assistant's call(s) and
        the results, in this provider's own format."""
        raise ToolsUnsupported(f"{self.spec.label} cannot run tools.")

    def chat(self, system: str, messages: list[dict], *, max_tokens: int | None = None) -> str:
        """Send a conversation; returns the assistant's text."""
        return self.complete(system, messages, max_tokens=max_tokens).text

    def generate(self, system: str, prompt: str, *, max_tokens: int | None = None) -> str:
        return self.chat(system, [{"role": "user", "content": prompt}], max_tokens=max_tokens)

    def list_models(self) -> list[str]:
        return []

    def check(self) -> dict:
        """Verify the provider works end to end with one tiny request."""
        models: list[str] = []
        try:
            models = self.list_models()
        except LLMError:
            raise
        except Exception:
            pass
        reply = self.generate("You are a connection test.", "Reply with the single word: ready",
                              max_tokens=16)
        return {"ok": True, "message": f"Connected to {self.describe()} — replied “{reply[:40]}”.",
                "models": models}

    def _fail(self, message: str, fatal: bool = False) -> LLMError:
        return LLMError(redact(message, self.api_key), fatal=fatal)


def ask(llm, system: str, messages: list[dict], *, max_tokens: int | None = None, schema: dict | None = None,
        effort: str | None = None) -> str:
    """Text answer from any provider-like object.

    Real providers get the schema and effort; a minimal stand-in that only implements ``chat``
    (as the tests' fakes do) is asked the plain way, which is why every parser in the pipeline
    accepts the plain text format as well as the JSON one.
    """
    complete = getattr(llm, "complete", None)
    if complete is not None:
        return complete(system, messages, max_tokens=max_tokens, schema=schema, effort=effort).text
    chat = getattr(llm, "chat", None)
    if chat is not None:
        return chat(system, messages, max_tokens=max_tokens)
    return llm.generate(system, messages[-1]["content"])    # the simplest stand-in: one prompt in, text out


ToolHandler = Callable[[ToolCall], str]


def run_tools(llm, system: str, messages: list[dict], tools: list[ToolSpec], handler: ToolHandler, *,
              max_rounds: int = 4, max_tokens: int | None = None, schema: dict | None = None,
              effort: str | None = None, on_call: Callable[[ToolCall, str], None] | None = None) -> str:
    """The model may call `tools` before it answers; `handler` runs each call and returns its result
    as text. Returns the final answer (JSON when `schema` is given).

    Works for every provider: tool rounds are replayed in the provider's own message format
    (`tool_turn`). The answer's shape is enforced only on the final request, after the tool calls,
    because that is the one request every provider lets us constrain. A provider that cannot run
    tools at all is asked once, without them.
    """
    complete = getattr(llm, "complete", None)
    if complete is None or not tools:
        return ask(llm, system, messages, max_tokens=max_tokens, schema=schema, effort=effort)
    convo = list(messages)
    reply: Reply | None = None
    for _ in range(max_rounds):
        try:
            reply = complete(system, convo, max_tokens=max_tokens, tools=tools, effort=effort)
        except ToolsUnsupported:
            return ask(llm, system, messages, max_tokens=max_tokens, schema=schema, effort=effort)
        if not reply.tool_calls:
            break
        results = []
        for call in reply.tool_calls:
            try:
                out = handler(call)
            except Exception as e:  # a bad argument is the model's mistake to read, not ours to raise
                out = json.dumps({"ok": False, "error": str(e)})
            results.append((call, out))
            if on_call:
                on_call(call, out)
        convo += llm.tool_turn(reply, results)
    else:
        # Out of rounds: ask for the answer with no tools on offer.
        reply = complete(system, convo, max_tokens=max_tokens, effort=effort)
    text = reply.text if reply else ""
    if schema is None:
        return text
    if parse_json(text) is not None:
        return text
    if text.strip():
        convo += [{"role": "assistant", "content": text}]
    convo += [{"role": "user", "content": "Now give your final answer in the required JSON format, "
                                          "using what the tool results showed."}]
    return complete(system, convo, max_tokens=max_tokens, schema=schema, effort=effort).text


# --------------------------------------------------------------------- HTTP

class _HTTPProvider(LLMProvider):
    """Shared plumbing: a pooled session per thread and friendly error mapping."""

    _local = threading.local()
    # (base_url, model) → options this server rejected ("think", "format", "tools", "temperature").
    _rejected: dict[tuple[str, str], set[str]] = {}
    _rejected_lock = threading.Lock()

    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = f"lucidfish/{__version__}"
            self._local.session = s
        return s

    def _rejects(self, option: str) -> bool:
        return option in self._rejected.get((self.base_url, self.model), set())

    def _remember_rejection(self, option: str) -> None:
        with self._rejected_lock:
            self._rejected.setdefault((self.base_url, self.model), set()).add(option)

    def _post(self, url: str, payload: dict, headers: dict | None = None) -> dict:
        """POST with retries for rate limits / server hiccups; returns parsed JSON."""
        attempts = 1 if self.spec.local else 3
        for attempt in range(attempts):
            try:
                r = self._session().post(url, json=payload, headers=headers or {},
                                         timeout=self.cfg.timeout_s)
            except requests.ConnectionError as e:
                # A local server that isn't running won't start by itself; a cloud
                # hiccup might clear, so the pipeline decides after repeated failures.
                raise self._fail(self._unreachable(), fatal=self.spec.local) from e
            except requests.Timeout as e:
                raise self._fail(f"{self.spec.label} took longer than {self.cfg.timeout_s}s to answer.") from e
            if r.status_code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                wait = r.headers.get("retry-after", "")
                time.sleep(min(20.0, float(wait)) if wait.replace(".", "", 1).isdigit() else 2.0 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise self._http_error(r)
            try:
                return r.json()
            except ValueError as e:
                raise self._fail(f"{self.spec.label} returned an unreadable response.") from e
        raise self._fail(f"{self.spec.label} is busy; try again shortly.")  # pragma: no cover

    def _post_adapting(self, url: str, payload: dict, headers: dict | None,
                       options: dict[str, tuple[str, ...]]) -> dict:
        """POST; when the server rejects one of `options` (payload key → words its error would contain),
        drop that option, remember it for this model, and try again. Tools are the exception: the
        caller decides what to do without them."""
        for _ in range(len(options) + 1):
            try:
                return self._post(url, payload, headers)
            except LLMError as e:
                if e.fatal:
                    raise
                msg = str(e).lower()
                culprit = next((key for key, words in options.items()
                                if key in payload and any(w in msg for w in words)), None)
                if culprit is None:
                    raise
                self._remember_rejection(culprit)
                if culprit == "tools":
                    raise ToolsUnsupported(f"{self.describe()} cannot use tools: {e}") from e
                payload.pop(culprit)
                if culprit == "think":
                    pass
        raise self._fail(f"{self.spec.label} rejected the request repeatedly.")  # pragma: no cover

    def _unreachable(self) -> str:
        return f"Cannot reach {self.spec.label} at {self.base_url}. Check your internet connection."

    def _error_text(self, r: requests.Response) -> str:
        try:
            body = r.json()
            err = body.get("error", body)
            if isinstance(err, dict):
                err = err.get("message") or err
            return str(err)[:300]
        except ValueError:
            return r.text[:300]

    def _http_error(self, r: requests.Response) -> LLMError:
        detail = self._error_text(r)
        if r.status_code in (401, 403):
            return self._fail(f"{self.spec.label} rejected the API key (HTTP {r.status_code}). "
                              "Check it in Settings → AI coach.", fatal=True)
        if r.status_code == 404:
            return self._fail(f"Model '{self.model}' was not found at {self.spec.label}: {detail}", fatal=True)
        if r.status_code == 429:
            return self._fail(f"{self.spec.label} rate limit reached: {detail}")
        return self._fail(f"{self.spec.label} error (HTTP {r.status_code}): {detail}")


def _function_tools(tools: list[ToolSpec]) -> list[dict]:
    """Tool definitions in the OpenAI shape (Ollama speaks it too)."""
    return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                              "parameters": t.input_schema}} for t in tools]


class OllamaProvider(_HTTPProvider):
    """Local models via Ollama's native /api/chat endpoint."""

    # Kept for older imports: models (per server) that rejected the "think" switch.
    _no_think_switch: set[tuple[str, str]] = set()
    _switch_lock = threading.Lock()

    def _unreachable(self) -> str:
        return (f"Cannot reach Ollama at {self.base_url}. Start the Ollama app (or run `ollama serve`), "
                f"then pull a model with `ollama pull {self.model}`.")

    def _http_error(self, r: requests.Response) -> LLMError:
        detail = self._error_text(r)
        if r.status_code == 404 or ("not found" in detail.lower() and "tool" not in detail.lower()):
            return self._fail(f"The model '{self.model}' is not installed in Ollama. "
                              f"Run `ollama pull {self.model}` or pick an installed model in Settings.",
                              fatal=True)
        return super()._http_error(r)

    def complete(self, system: str, messages: list[dict], *, max_tokens: int | None = None,
                 schema: dict | None = None, tools: list[ToolSpec] | None = None,
                 effort: str | None = None) -> Reply:
        options = {"temperature": self.cfg.temperature, "num_ctx": self.cfg.num_ctx}
        if max_tokens:
            options["num_predict"] = max_tokens
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": False,
            "keep_alive": "30m",   # stay loaded between moves instead of reloading from disk
            "options": options,
        }
        key = (self.base_url, self.model)
        if key not in self._no_think_switch and not self._rejects("think"):
            # Thinking models (qwen3, deepseek-r1, ...) otherwise write hundreds of hidden
            # reasoning tokens first: slower, and counted against num_predict, which can cut
            # the actual answer off. The notes are narration of verified facts; no thinking needed.
            payload["think"] = False
        if schema and not self._rejects("format"):
            payload["format"] = schema
        if tools and not self._rejects("tools"):
            payload["tools"] = _function_tools(tools)
        elif tools:
            raise ToolsUnsupported(f"{self.describe()} cannot use tools.")
        data = self._post_adapting(f"{self.base_url}/api/chat", payload, None, {
            "think": ("think",), "tools": ("tool", "function"), "format": ("format", "schema", "grammar", "json"),
        })
        if self._rejects("think"):
            with self._switch_lock:
                self._no_think_switch.add(key)
        message = data.get("message") or {}
        calls = [ToolCall(id=f"call_{i}", name=(c.get("function") or {}).get("name", ""),
                          input=_tool_json((c.get("function") or {}).get("arguments")))
                 for i, c in enumerate(message.get("tool_calls") or [], 1)]
        assistant = {"role": "assistant", "content": message.get("content") or ""}
        if message.get("tool_calls"):
            assistant["tool_calls"] = message["tool_calls"]
        return Reply(text=clean_output(message.get("content")), tool_calls=calls, assistant=assistant)

    def tool_turn(self, reply: Reply, results: list[tuple[ToolCall, str]]) -> list[dict]:
        return [reply.assistant, *({"role": "tool", "content": out, "tool_name": call.name} for call, out in results)]

    def list_models(self) -> list[str]:
        try:
            r = self._session().get(f"{self.base_url}/api/tags", timeout=5)
            r.raise_for_status()
        except requests.RequestException as e:
            raise self._fail(self._unreachable(), fatal=True) from e
        return sorted(m["name"] for m in r.json().get("models", []))

    def check(self) -> dict:
        models = self.list_models()
        wanted = {self.model, f"{self.model}:latest"}
        if models and not wanted & set(models):
            raise self._fail(f"'{self.model}' is not installed. Installed models: {', '.join(models)}. "
                             f"Run `ollama pull {self.model}` or choose one of those.", fatal=True)
        result = super().check()
        result["models"] = models
        return result


class OpenAICompatibleProvider(_HTTPProvider):
    """OpenAI's Chat Completions API — also spoken by Gemini, OpenRouter, Groq,
    LM Studio, llama.cpp and vLLM."""

    def _headers(self) -> dict:
        h = {}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if self.spec.id == "openrouter":
            h["X-Title"] = "Lucidfish"
        return h

    def complete(self, system: str, messages: list[dict], *, max_tokens: int | None = None,
                 schema: dict | None = None, tools: list[ToolSpec] | None = None,
                 effort: str | None = None) -> Reply:
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        if not self._rejects("temperature"):
            payload["temperature"] = self.cfg.temperature
        # Output caps only for local servers: cloud reasoning models count hidden
        # thinking against the cap and can return nothing if it is too small.
        if max_tokens and self.spec.local:
            payload["max_tokens"] = max_tokens
        if schema and not self._rejects("response_format"):
            spec: dict = {"name": schema.get("title", "answer"), "schema": schema}
            if self.spec.id == "openai":
                spec["strict"] = True
            payload["response_format"] = {"type": "json_schema", "json_schema": spec}
        if tools and not self._rejects("tools"):
            payload["tools"] = _function_tools(tools)
        elif tools:
            raise ToolsUnsupported(f"{self.describe()} cannot use tools.")
        data = self._post_adapting(f"{self.base_url}/chat/completions", payload, self._headers(), {
            # Some models only accept their default temperature (e.g. reasoning models).
            "temperature": ("temperature",), "tools": ("tool", "function"),
            "response_format": ("response_format", "json_schema", "schema", "structured"),
        })
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        if not message.get("content") and message.get("refusal"):
            raise self._fail(f"The model declined: {message['refusal']}")
        calls = [ToolCall(id=c.get("id") or f"call_{i}", name=(c.get("function") or {}).get("name", ""),
                          input=_tool_json((c.get("function") or {}).get("arguments")))
                 for i, c in enumerate(message.get("tool_calls") or [], 1)]
        assistant = {"role": "assistant", "content": message.get("content") or None}
        if message.get("tool_calls"):
            assistant["tool_calls"] = message["tool_calls"]
        return Reply(text=clean_output(message.get("content")), tool_calls=calls, assistant=assistant)

    def tool_turn(self, reply: Reply, results: list[tuple[ToolCall, str]]) -> list[dict]:
        return [reply.assistant, *({"role": "tool", "tool_call_id": call.id, "content": out} for call, out in results)]

    def list_models(self) -> list[str]:
        r = self._session().get(f"{self.base_url}/models", headers=self._headers(), timeout=8)
        if r.status_code in (401, 403):
            raise self._http_error(r)
        r.raise_for_status()
        return sorted(m.get("id", "") for m in r.json().get("data", []) if m.get("id"))[:200]


# ---------------------------------------------------------------- Anthropic

def _claude_version(model: str) -> tuple[str, float] | None:
    """('opus', 4.6) from 'claude-opus-4-6', ('haiku', 4.5) from 'claude-haiku-4-5-20251001'."""
    m = re.match(r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$", model)
    if m:
        return m.group(1), int(m.group(2)) + int(m.group(3) or 0) / 10
    m = re.match(r"claude-(\d)(?:-(\d))?-(opus|sonnet|haiku)", model)
    if m:
        return m.group(3), int(m.group(1)) + int(m.group(2) or 0) / 10
    return None


class AnthropicProvider(LLMProvider):
    """Claude via the official SDK.

    Request shape follows the current API: no sampling parameters on models that reject
    them, `effort` per job on models that support it (low for narration, higher for the
    review), structured outputs through `output_config.format`, a cached system prompt (it
    is identical for every move of a game), and the server-side refusal fallback on the
    models that offer it. A parameter the model rejects is dropped and remembered.
    """

    MAX_TOKENS = 16000  # a ceiling, not a target: notes are a few hundred tokens
    _rejected: dict[str, set[str]] = {}   # model → request options it rejected
    _lock = threading.Lock()

    def __init__(self, spec: ProviderSpec, cfg: LLMConfig):
        super().__init__(spec, cfg)
        try:
            import anthropic
        except ImportError as e:
            raise LLMError("The Anthropic SDK is not installed. Run `pip install anthropic`.", fatal=True) from e
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=self.api_key, timeout=cfg.timeout_s, max_retries=2)
        version = _claude_version(self.model)
        family, ver = version if version else ("", 0.0)
        # Sampling parameters: accepted up to Opus/Sonnet 4.6 and Haiku 4.5; rejected by every 5.x model.
        self._sampling = (family == "haiku" and 0 < ver < 5) or (family in ("opus", "sonnet") and 0 < ver < 4.7)
        self._effort = family in ("fable", "mythos") or (family == "opus" and ver >= 4.5) or (
            family == "sonnet" and ver >= 4.6) or (family == "haiku" and ver >= 5)
        self._fallbacks = (family, ver) in {("opus", 5.0), ("opus", 5.5), ("sonnet", 5.5), ("fable", 5.1),
                                            ("mythos", 5.1)}

    def _rejects(self, option: str) -> bool:
        return option in self._rejected.get(self.model, set())

    def _remember(self, option: str) -> None:
        with self._lock:
            self._rejected.setdefault(self.model, set()).add(option)

    def complete(self, system: str, messages: list[dict], *, max_tokens: int | None = None,
                 schema: dict | None = None, tools: list[ToolSpec] | None = None,
                 effort: str | None = None) -> Reply:
        a = self._anthropic
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.MAX_TOKENS,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        }
        output_config: dict = {}
        if self._effort and not self._rejects("effort"):
            output_config["effort"] = effort or "low"
        if schema and not self._rejects("format"):
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if output_config:
            kwargs["output_config"] = output_config
        if self._sampling and not self._rejects("temperature"):
            kwargs["extra_body"] = {"temperature": self.cfg.temperature}
        if tools and not self._rejects("tools"):
            kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                               for t in tools]
        elif tools:
            raise ToolsUnsupported(f"{self.describe()} cannot use tools.")
        try:
            resp = self._send(kwargs)
        except (a.AuthenticationError, a.PermissionDeniedError) as e:
            raise self._fail("Anthropic rejected the API key. Check it in Settings → AI coach.", fatal=True) from e
        except a.NotFoundError as e:
            raise self._fail(f"Model '{self.model}' was not found at Anthropic.", fatal=True) from e
        except a.RateLimitError as e:
            raise self._fail("Anthropic rate limit reached; try again shortly.") from e
        except a.APIConnectionError as e:
            raise self._fail(f"Cannot reach Anthropic: {e}") from e
        except a.APIStatusError as e:
            raise self._fail(f"Anthropic error (HTTP {e.status_code}): {e.message}") from e
        if resp.stop_reason == "refusal":
            raise self._fail("The model declined to answer this request.")
        text = clean_output("".join(b.text for b in resp.content if b.type == "text"))
        calls = [ToolCall(id=b.id, name=b.name, input=_tool_json(b.input))
                 for b in resp.content if b.type == "tool_use"]
        return Reply(text=text, tool_calls=calls, assistant=resp.content)

    def _send(self, kwargs: dict):
        """One request; a parameter the model rejects is dropped, remembered, and the request repeated."""
        a = self._anthropic
        for _ in range(4):
            try:
                if self._fallbacks and not self._rejects("fallbacks"):
                    return self._client.beta.messages.create(
                        betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
                return self._client.messages.create(**kwargs)
            except a.BadRequestError as e:
                msg = str(e).lower()
                if "tool" in msg and "tools" in kwargs:
                    self._remember("tools")
                    raise ToolsUnsupported(f"{self.describe()} cannot use tools: {e.message}") from e
                dropped = False
                for option, words in (("format", ("format", "schema", "json_schema")),
                                      ("effort", ("effort",)),
                                      ("temperature", ("temperature", "sampling")),
                                      ("fallbacks", ("fallback", "beta"))):
                    if any(w in msg for w in words) and not self._rejects(option):
                        self._remember(option)
                        dropped = True
                        if option in ("format", "effort"):
                            kwargs.get("output_config", {}).pop(option, None)
                            if not kwargs.get("output_config"):
                                kwargs.pop("output_config", None)
                        elif option == "temperature":
                            kwargs.pop("extra_body", None)
                        break
                if not dropped:
                    raise
        return self._client.messages.create(**kwargs)

    def tool_turn(self, reply: Reply, results: list[tuple[ToolCall, str]]) -> list[dict]:
        return [{"role": "assistant", "content": reply.assistant},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call.id, "content": out}
                                             for call, out in results]}]

    def list_models(self) -> list[str]:
        try:
            return [m.id for m in self._client.models.list(limit=100)]
        except self._anthropic.AuthenticationError as e:
            raise self._fail("Anthropic rejected the API key.", fatal=True) from e


_KINDS = {"ollama": OllamaProvider, "openai": OpenAICompatibleProvider, "anthropic": AnthropicProvider}


def make_provider(cfg: LLMConfig) -> LLMProvider:
    """Build the configured provider (raises LLMError with a helpful message)."""
    spec = PROVIDERS.get(cfg.provider)
    if spec is None:
        raise LLMError(f"Unknown LLM provider '{cfg.provider}'. Choose one of: {', '.join(PROVIDERS)}.",
                       fatal=True)
    return _KINDS[spec.kind](spec, cfg)
