"""Central configuration.

Every knob has a sensible default, can be overridden with an environment
variable, and (for the web UI) can be changed from the Settings panel, which
persists its choices via :mod:`lucidfish.settings`. Precedence, lowest first:

    built-in defaults  <  environment / .env file  <  saved settings  <  CLI flags

Nothing here is platform specific beyond the lookup tables below, so the same
code runs on macOS, Linux and Windows.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "Lucidfish"
IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"


# --------------------------------------------------------------------- .env

def load_dotenv(path: str | os.PathLike | None = None) -> None:
    """Load ``KEY=value`` lines from a .env file into ``os.environ``.

    Existing environment variables always win, so a .env file can never silently
    override something the user exported. Only the tiny subset of the format we
    need is supported (comments, optional ``export``, optional quotes), which
    avoids a dependency on python-dotenv.
    """
    # Lazily yielded so a LUCIDFISH_DATA_DIR set in ./.env is honoured for the second file.
    candidates = (lambda: Path(path),) if path else (lambda: Path.cwd() / ".env",
                                                     lambda: data_dir() / ".env")
    for candidate in candidates:
        try:
            text = candidate().read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.removeprefix("export ").partition("=")
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


def _env_int(name: str, default: int | None) -> int | None:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


# -------------------------------------------------------------------- paths

def data_dir() -> Path:
    """Per-user directory for the database and optional .env file.

    - Windows: ``%APPDATA%\\Lucidfish``
    - macOS:   ``~/Library/Application Support/Lucidfish``
    - Linux:   ``$XDG_DATA_HOME/lucidfish`` (default ``~/.local/share/lucidfish``)

    ``LUCIDFISH_DATA_DIR`` overrides it. A database created by earlier versions
    inside the source checkout (``<repo>/data``) keeps being used so nobody
    loses their history on upgrade.
    """
    override = os.environ.get("LUCIDFISH_DATA_DIR")
    if override:
        return Path(override).expanduser()
    legacy = Path(__file__).resolve().parent.parent / "data"
    if (legacy / "lucidfish.db").exists():
        return legacy
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    if IS_MACOS:
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "lucidfish"


# ---------------------------------------------------------------- stockfish

def _stockfish_candidates() -> list[Path]:
    """Well-known install locations, checked after PATH."""
    if IS_WINDOWS:
        roots = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
            Path(os.environ.get("ChocolateyInstall", r"C:\ProgramData\chocolatey")) / "bin",
            Path.home() / "scoop" / "shims",
            Path.home() / "Downloads",
            Path.home() / "Desktop",
            Path.home(),
            Path(os.environ.get("SystemDrive", "C:") + "\\"),   # e.g. C:\Stockfish\stockfish*.exe
            data_dir(),
        ]
        found: list[Path] = []
        for root in roots:
            if not root.is_dir():
                continue
            # Official downloads unpack to e.g. stockfish\stockfish-windows-x86-64-avx2.exe
            found += sorted(root.glob("stockfish*.exe"))
            found += sorted(root.glob("stockfish*/stockfish*.exe"))
        return found
    paths = [
        "/opt/homebrew/bin/stockfish",   # macOS, Apple Silicon Homebrew
        "/usr/local/bin/stockfish",      # macOS Intel Homebrew / manual Linux installs
        "/usr/games/stockfish",          # Debian / Ubuntu apt (not always on PATH)
        "/usr/bin/stockfish",            # Fedora / Arch
        "/snap/bin/stockfish",
        str(data_dir() / "stockfish"),
    ]
    return [Path(p) for p in paths]


def find_stockfish() -> str:
    """Locate a Stockfish binary: env var, then PATH, then common install paths.

    Returns ``"stockfish"`` when nothing is found, so the engine layer can raise
    a clear, platform-specific installation hint.
    """
    override = os.environ.get("LUCIDFISH_STOCKFISH")
    if override:
        return override
    for name in ("stockfish", "stockfish.exe") if IS_WINDOWS else ("stockfish",):
        hit = shutil.which(name)
        if hit:
            return hit
    for candidate in _stockfish_candidates():
        if candidate.is_file():
            return str(candidate)
    return "stockfish"


def default_threads() -> int:
    """Leave headroom for the OS and a local LLM: all but two cores, max 8."""
    cores = os.cpu_count() or 2
    return max(1, min(8, cores - 2 if cores > 4 else cores // 2))


# ------------------------------------------------------------------ configs

# Named engine strength presets exposed in the UI and CLI (--depth overrides).
DEPTH_PRESETS = {"fast": 14, "balanced": 18, "deep": 22}


@dataclass
class EngineConfig:
    path: str = field(default_factory=find_stockfish)
    depth: int = field(default_factory=lambda: _env_int("LUCIDFISH_DEPTH", 18))
    movetime_s: float | None = None   # if set, fixed seconds per position instead of depth
    multipv: int = 3                  # candidate lines per position (3 covers every UI view)
    threads: int = field(default_factory=lambda: _env_int("LUCIDFISH_THREADS", default_threads()))
    hash_mb: int = field(default_factory=lambda: _env_int("LUCIDFISH_HASH_MB", 256))
    use_cache: bool = True            # reuse stored evaluations for positions seen before


@dataclass
class LLMConfig:
    provider: str = field(default_factory=lambda: os.environ.get("LUCIDFISH_PROVIDER", "ollama"))
    model: str = field(default_factory=lambda: os.environ.get("LUCIDFISH_MODEL", ""))
    base_url: str = field(default_factory=lambda: os.environ.get("LUCIDFISH_BASE_URL", ""))
    api_key: str | None = None        # resolved at runtime by lucidfish.credentials; never persisted here
    temperature: float = 0.2          # low: we want faithful narration, not creativity
    timeout_s: int = 180
    num_ctx: int = field(default_factory=lambda: _env_int("LUCIDFISH_NUM_CTX", 8192))
    concurrency: int | None = None    # parallel requests; None = provider default (1 local, 4 cloud)
    enabled: bool = True              # False = engine-only analysis (no LLM at all)
    factcheck: bool = True            # re-ask once when a note names moves/pieces not on the board
    escalate: bool = True             # let the expert model (if any) rewrite text that failed the fact-check


@dataclass
class AnalysisConfig:
    # Verdict thresholds, in percentage points of *winning chances* lost
    # (the Lichess method). Win% makes "+8 → +5" a non-event and "0 → -2" a
    # blunder, which matches how humans feel about those moves.
    inaccuracy_win: float = 5.0
    mistake_win: float = 10.0
    blunder_win: float = 15.0
    # A position is "critical" when the best move keeps this many more percentage
    # points of winning chances than the 2nd-best — finding it changed the game.
    critical_gap_win: float = 15.0
    # How much the coach writes:
    #   key      — only mistakes and critical moments (fastest)
    #   standard — every move you played + opponent moves that matter
    #   full     — detailed notes on every move of both sides (slowest)
    detail: str = field(default_factory=lambda: os.environ.get("LUCIDFISH_DETAIL", "standard"))
    opening_book_plies: int = 20      # consult the opening explorer for the first N plies


@dataclass
class Config:
    engine: EngineConfig = field(default_factory=EngineConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    # Optional second, stronger model for the hard parts: notes on mistakes and critical
    # moments, the post-game review, the coach profile and chat. None = main model does all.
    expert: LLMConfig | None = None
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    # Optional default account for `lucidfish` with no arguments.
    chesscom_user: str = field(default_factory=lambda: os.environ.get("LUCIDFISH_CHESSCOM_USER", ""))
    lichess_user: str = field(default_factory=lambda: os.environ.get("LUCIDFISH_LICHESS_USER", ""))
    # Player rating — tailors explanation depth and lesson targeting (None = generic).
    user_elo: int | None = None
