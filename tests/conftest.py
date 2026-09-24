"""Shared test fixtures.

Every test runs against a throw-away data folder and with the OS keychain
disabled, so running the suite never touches your real games or API keys.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lucidfish import credentials
from lucidfish.config import find_stockfish
from lucidfish.opening import OpeningExplorer


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LUCIDFISH_DATA_DIR", str(tmp_path / "data"))
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY",
                "GROQ_API_KEY", "LUCIDFISH_PROVIDER", "LUCIDFISH_MODEL", "LUCIDFISH_BASE_URL", "LUCIDFISH_DETAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(credentials, "_keyring", lambda: None)
    monkeypatch.setattr(OpeningExplorer, "_disabled_until", float("inf"))   # no network in tests
    credentials._session_keys.clear()
    credentials._cache.clear()
    yield


def _stockfish() -> str | None:
    path = find_stockfish()
    return path if os.path.isfile(path) else None


STOCKFISH = _stockfish()
needs_engine = pytest.mark.skipif(STOCKFISH is None, reason="Stockfish is not installed")

SAMPLE_PGN = (Path(__file__).resolve().parent.parent / "examples" / "sample.pgn").read_text(encoding="utf-8")


def fast_config(**llm):
    """A Config tuned for quick, deterministic tests (shallow single-thread engine)."""
    from lucidfish.config import Config
    cfg = Config()
    cfg.engine.path = STOCKFISH or "stockfish"
    cfg.engine.depth = 10
    cfg.engine.threads = 1
    cfg.engine.hash_mb = 32
    cfg.llm.enabled = False
    for k, v in llm.items():
        setattr(cfg.llm, k, v)
    return cfg
