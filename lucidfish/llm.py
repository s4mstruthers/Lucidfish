"""LLM providers: one small interface over local and cloud models.

Supported out of the box:

- **Ollama** (local, default) — private and free, but uses your CPU/GPU.
- **Any OpenAI-compatible server** — LM Studio, llama.cpp, vLLM, ... (local).
- **OpenAI**, **Google Gemini**, **OpenRouter**, **Groq** — cloud, via the
  OpenAI-compatible Chat Completions API.
- **Anthropic (Claude)** — cloud, via the official ``anthropic`` SDK.

Design rule: the LLM NEVER analyses the position itself. It receives verified
evidence (see prompts.py) and its only job is to narrate that evidence.
"""

from __future__ import annotations

import os
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import requests

from . import __version__
from .config import LLMConfig
from .credentials import get_key, redact


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
    ProviderSpec("ollama", "Ollama (local)", "ollama", "http://localhost:11434", "llama3.1:8b",
                 None, False, True, 1,
                 ("qwen2.5:7b", "qwen3:8b", "llama3.1:8b", "llama3.2:3b", "qwen2.5:14b", "qwen3:14b",
                  "gemma3:12b"),
                 "https://ollama.com/download",
                 "Runs on your computer: private and free, but CPU/GPU intensive."),
    ProviderSpec("openai", "OpenAI", "openai", "https://api.openai.com/v1", "gpt-4.1-mini",
                 "OPENAI_API_KEY", True, False, 4,
                 ("gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini"),
                 "https://platform.openai.com/api-keys"),
    ProviderSpec("anthropic", "Anthropic (Claude)", "anthropic", "https://api.anthropic.com",
                 "claude-opus-5", "ANTHROPIC_API_KEY", True, False, 4,
                 ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "claude-fable-5-1"),
                 "https://console.anthropic.com/settings/keys"),
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


_THINK_RE = re.compile(r"<think>.*?</think>|^.*?</think>", re.S)


def clean_output(text: str | None) -> str:
    """Strip reasoning blocks some local models (qwen3, deepseek-r1) emit inline."""
    return _THINK_RE.sub("", text or "").strip()


def _normalise_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url.replace("://0.0.0.0", "://127.0.0.1")


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
    def chat(self, system: str, messages: list[dict], *, max_tokens: int | None = None) -> str:
        """Send a conversation; returns the assistant's text."""

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


# --------------------------------------------------------------------- HTTP

class _HTTPProvider(LLMProvider):
    """Shared plumbing: a pooled session per thread and friendly error mapping."""

    _local = threading.local()

    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = f"lucidfish/{__version__}"
            self._local.session = s
        return s

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


class OllamaProvider(_HTTPProvider):
    """Local models via Ollama's native /api/chat endpoint."""

    def _unreachable(self) -> str:
        return (f"Cannot reach Ollama at {self.base_url}. Start the Ollama app (or run `ollama serve`), "
                f"then pull a model with `ollama pull {self.model}`.")

    def _http_error(self, r: requests.Response) -> LLMError:
        detail = self._error_text(r)
        if r.status_code == 404 or "not found" in detail.lower():
            return self._fail(f"The model '{self.model}' is not installed in Ollama. "
                              f"Run `ollama pull {self.model}` or pick an installed model in Settings.",
                              fatal=True)
        return super()._http_error(r)

    # Models (per server) that rejected the "think" switch; they are asked without it.
    _no_think_switch: set[tuple[str, str]] = set()
    _switch_lock = threading.Lock()

    def chat(self, system: str, messages: list[dict], *, max_tokens: int | None = None) -> str:
        options = {"temperature": self.cfg.temperature, "num_ctx": self.cfg.num_ctx}
        if max_tokens:
            options["num_predict"] = max_tokens
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": False,
            "keep_alive": "30m",   # stay loaded between moves instead of reloading from disk
            "options": options,
        }
        key = (self.base_url, self.model)
        if key not in self._no_think_switch:
            # Thinking models (qwen3, deepseek-r1, ...) otherwise write hundreds of hidden
            # reasoning tokens first: slower, and counted against num_predict, which can cut
            # the actual answer off. The notes are narration of verified facts; no thinking needed.
            payload["think"] = False
        try:
            data = self._post(f"{self.base_url}/api/chat", payload)
        except LLMError as e:
            if "think" not in payload or "think" not in str(e).lower():
                raise
            with self._switch_lock:
                self._no_think_switch.add(key)   # e.g. a model that can only think: ask without the switch
            payload.pop("think")
            data = self._post(f"{self.base_url}/api/chat", payload)
        return clean_output((data.get("message") or {}).get("content"))

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

    def chat(self, system: str, messages: list[dict], *, max_tokens: int | None = None) -> str:
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": self.cfg.temperature,
        }
        # Output caps only for local servers: cloud reasoning models count hidden
        # thinking against the cap and can return nothing if it is too small.
        if max_tokens and self.spec.local:
            payload["max_tokens"] = max_tokens
        url = f"{self.base_url}/chat/completions"
        try:
            data = self._post(url, payload, self._headers())
        except LLMError as e:
            # Some models only accept their default temperature (e.g. reasoning models).
            if "temperature" not in str(e).lower() or "temperature" not in payload:
                raise
            payload.pop("temperature")
            data = self._post(url, payload, self._headers())
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        if not message.get("content") and message.get("refusal"):
            raise self._fail(f"The model declined: {message['refusal']}")
        return clean_output(message.get("content"))

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

    Request shape follows the current API: no sampling parameters on models
    that reject them, low `effort` for these short narration tasks, a cached
    system prompt (it is identical for every move of a game), and the
    server-side refusal fallback on models that support it.
    """

    MAX_TOKENS = 16000  # a ceiling, not a target: notes are a few hundred tokens

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
        # Opus 4.7+, Sonnet 5 and Fable reject temperature; older models accept it.
        self._sampling = family == "haiku" or (family in ("opus", "sonnet") and 0 < ver < 4.7)
        self._effort = family in ("fable", "mythos") or (family == "opus" and ver >= 4.5) or (
            family == "sonnet" and ver >= 4.6)
        self._fallbacks = (family, ver) in (("opus", 5.0), ("fable", 5.1))

    def chat(self, system: str, messages: list[dict], *, max_tokens: int | None = None) -> str:
        a = self._anthropic
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.MAX_TOKENS,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        }
        if self._effort:
            kwargs["output_config"] = {"effort": "low"}
        if self._sampling:
            kwargs["extra_body"] = {"temperature": self.cfg.temperature}
        try:
            try:
                if self._fallbacks:
                    resp = self._client.beta.messages.create(
                        betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
                else:
                    resp = self._client.messages.create(**kwargs)
            except a.BadRequestError as e:
                # A model/parameter mismatch we did not anticipate: retry once with the bare request.
                if not re.search(r"temperature|effort|fallback|output_config", str(e), re.I):
                    raise
                for opt in ("output_config", "extra_body"):
                    kwargs.pop(opt, None)
                resp = self._client.messages.create(**kwargs)
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
        return clean_output("".join(b.text for b in resp.content if b.type == "text"))

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
