"""Central configuration. Everything overridable via env vars or CLI flags."""

import os
import shutil
from dataclasses import dataclass, field


def _find_stockfish() -> str:
    """Locate a Stockfish binary: env var first, then PATH, then common Homebrew paths."""
    candidates = [
        os.environ.get("LUCIDFISH_STOCKFISH"),
        shutil.which("stockfish"),
        "/opt/homebrew/bin/stockfish",   # Apple Silicon Homebrew
        "/usr/local/bin/stockfish",      # Intel Homebrew
        "/usr/bin/stockfish",            # Linux apt
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return "stockfish"  # hope it's on PATH; engine layer raises a clear error if not


@dataclass
class EngineConfig:
    path: str = field(default_factory=_find_stockfish)
    depth: int = 18            # per-position search depth; 18 is strong + fast on a laptop
    movetime_s: float | None = None  # if set, fixed seconds per position instead of depth
    multipv: int = 4           # how many candidate lines to extract
    threads: int = 6
    hash_mb: int = 256


@dataclass
class LLMConfig:
    # Local Ollama. Start with a small model; swap for a bigger one if prose is weak.
    base_url: str = field(default_factory=lambda: os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    model: str = field(default_factory=lambda: os.environ.get("LUCIDFISH_MODEL", "llama3.1:8b"))
    temperature: float = 0.2   # low: we want faithful narration, not creativity
    timeout_s: int = 120


@dataclass
class AnalysisConfig:
    # Eval-swing thresholds in centipawns for classifying a played move
    # (relative to the engine's best move in that position).
    inaccuracy_cp: int = 50
    mistake_cp: int = 120
    blunder_cp: int = 300
    # Only send "interesting" moves to the LLM (critical moves + every N quiet moves).
    explain_all: bool = False
    # A position is "critical" when the best move is this much better than the
    # 2nd-best — finding it changed the game, so explain it even if it was "best".
    critical_gap_cp: int = 150
    opening_book_plies: int = 20  # consult Lichess explorer for the first N plies


@dataclass
class Config:
    engine: EngineConfig = field(default_factory=EngineConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    # Default account: `python -m lucidfish` with no args analyzes this user's latest game.
    chesscom_user: str = field(default_factory=lambda: os.environ.get("LUCIDFISH_CHESSCOM_USER", "scrubb3rs"))
    # Player rating — tailors explanation depth and lesson targeting (None = generic).
    user_elo: int | None = None
