"""Runtime settings: defaults + environment + choices saved from the web UI.

The web Settings panel and the CLI share this, so a provider or model chosen
in the browser is also what `lucidfish game.pgn` uses. Only non-secret values
are stored (in the local database); API keys go through credentials.py.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from . import credentials, store
from .config import DEPTH_PRESETS, Config, data_dir, find_stockfish, load_dotenv
from .llm import PROVIDERS
from .pipeline import DETAIL_LEVELS

_dotenv_loaded = False


def _ensure_env() -> None:
    global _dotenv_loaded
    if not _dotenv_loaded:
        load_dotenv()
        _dotenv_loaded = True


def load_config(**overrides) -> Config:
    """Build the effective Config. Keyword overrides (e.g. from CLI flags) win."""
    _ensure_env()
    cfg = Config()
    saved = store.get_settings()
    values = {**saved, **{k: v for k, v in overrides.items() if v is not None}}

    provider = values.get("provider") or cfg.llm.provider
    if provider in PROVIDERS:
        cfg.llm.provider = provider
    models = saved.get("models", {})
    urls = saved.get("base_urls", {})
    cfg.llm.model = overrides.get("model") or models.get(cfg.llm.provider) or cfg.llm.model
    cfg.llm.base_url = overrides.get("base_url") or urls.get(cfg.llm.provider) or cfg.llm.base_url
    if values.get("llm_enabled") is not None:
        cfg.llm.enabled = bool(values["llm_enabled"])
    if values.get("concurrency"):
        cfg.llm.concurrency = int(values["concurrency"])
    if values.get("factcheck") is not None:
        cfg.llm.factcheck = bool(values["factcheck"])

    if values.get("stockfish_path"):
        cfg.engine.path = values["stockfish_path"]
    if values.get("depth"):
        cfg.engine.depth = int(values["depth"])
    if values.get("threads"):
        cfg.engine.threads = int(values["threads"])
    if values.get("hash_mb"):
        cfg.engine.hash_mb = int(values["hash_mb"])
    if values.get("detail") in DETAIL_LEVELS:
        cfg.analysis.detail = values["detail"]
    return cfg


def validate(values: dict) -> dict:
    """Check user-submitted settings; returns the cleaned subset. Raises ValueError."""
    clean: dict = {}
    saved = store.get_settings()
    if "provider" in values:
        if values["provider"] not in PROVIDERS:
            raise ValueError("Unknown AI provider.")
        clean["provider"] = values["provider"]
    provider = clean.get("provider") or saved.get("provider") or Config().llm.provider
    if "model" in values:
        model = str(values["model"] or "").strip()
        if len(model) > 200:
            raise ValueError("Model name is too long.")
        clean["models"] = {**saved.get("models", {}), provider: model}
    if "base_url" in values:
        url = str(values["base_url"] or "").strip()
        if url:
            parsed = urlparse(url if "://" in url else "http://" + url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError("The server URL must look like http://host:port.")
        clean["base_urls"] = {**saved.get("base_urls", {}), provider: url}
    for key in ("llm_enabled", "factcheck", "prefetch"):
        if key in values:
            clean[key] = bool(values[key])
    ranges = {"depth": (6, 30), "threads": (1, max(1, os.cpu_count() or 1)), "hash_mb": (16, 4096),
              "concurrency": (1, 16)}
    for key, (lo, hi) in ranges.items():
        if key in values and values[key] not in (None, ""):
            try:
                v = int(values[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a whole number.") from None
            if not lo <= v <= hi:
                raise ValueError(f"{key} must be between {lo} and {hi}.")
            clean[key] = v
    if "detail" in values:
        if values["detail"] not in DETAIL_LEVELS:
            raise ValueError("Unknown coaching detail level.")
        clean["detail"] = values["detail"]
    if "stockfish_path" in values:
        path = str(values["stockfish_path"] or "").strip().strip('"')
        if path and not Path(path).expanduser().is_file():
            raise ValueError(f"No file found at {path}.")
        clean["stockfish_path"] = str(Path(path).expanduser()) if path else ""
    return clean


def save(values: dict) -> dict:
    return store.save_settings(validate(values))


def public_settings() -> dict:
    """Everything the Settings panel needs — never includes an API key."""
    cfg = load_config()
    saved = store.get_settings()
    providers = []
    for spec in PROVIDERS.values():
        key = credentials.get_key(spec.id, spec.key_env)
        providers.append({
            "id": spec.id, "label": spec.label, "local": spec.local, "needs_key": spec.needs_key,
            "key_env": spec.key_env, "key_url": spec.key_url, "note": spec.note,
            "default_model": spec.default_model, "default_base_url": spec.base_url,
            "models": list(spec.models),
            "model": saved.get("models", {}).get(spec.id) or "",
            "base_url": saved.get("base_urls", {}).get(spec.id) or "",
            "key": key.public(),
        })
    return {
        "provider": cfg.llm.provider,
        "model": cfg.llm.model or PROVIDERS[cfg.llm.provider].default_model,
        "llm_enabled": cfg.llm.enabled,
        "factcheck": cfg.llm.factcheck,
        "prefetch": saved.get("prefetch", True),
        "concurrency": saved.get("concurrency"),
        "detail": cfg.analysis.detail,
        "depth": cfg.engine.depth,
        "depth_presets": DEPTH_PRESETS,
        "threads": cfg.engine.threads,
        "max_threads": os.cpu_count() or 1,
        "hash_mb": cfg.engine.hash_mb,
        "stockfish_path": saved.get("stockfish_path", ""),
        "stockfish_detected": find_stockfish(),
        "providers": providers,
        "secure_store": credentials.secure_store_name(),
        "data_dir": str(data_dir()),
    }
