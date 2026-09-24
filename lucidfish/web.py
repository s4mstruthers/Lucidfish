"""Web UI backend: a FastAPI server wrapping the analysis pipeline.

Run with ``lucidfish web`` (or ``python -m lucidfish.web``) → http://127.0.0.1:8420

Design notes:

- Analyses run in background threads, one at a time: a second Stockfish would
  only halve the speed of the first. The frontend polls ``/api/job/<id>?since=N``
  and receives only the moves it has not seen yet, rendering them live.
- Game lists from chess.com / Lichess are fetched IN THE BROWSER (their APIs
  allow it, and a real browser passes the bot checks that block scripts).
  The backend only ever receives PGN text.
- Security: the server listens on localhost, rejects foreign ``Host`` headers
  (DNS rebinding) and requires a custom header on every state-changing request
  (cross-site request forgery). API keys go to the operating system's
  credential store and are never sent back to the browser.
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import threading
import time
import uuid
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import chess
import chess.svg
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, credentials, settings, store
from .engine import EngineAnalyzer
from .export import annotated_pgn, markdown_report
from .llm import PROVIDERS, LLMError, make_provider
from .pipeline import analyze_game, analyze_position, build_coach, parse_game
from .prompts import COACH_CHAT_SYSTEM, PLAYER_SUMMARY_SYSTEM, build_player_summary_prompt

STATIC = Path(__file__).parent / "static"
DEFAULT_PORT = 8420
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _allowed_hosts() -> set[str]:
    extra = {h.strip().lower() for h in os.environ.get("LUCIDFISH_ALLOWED_HOSTS", "").split(",") if h.strip()}
    return _LOOPBACK | extra


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    settings.load_config()   # loads .env once and initialises the database
    store.init()
    yield


app = FastAPI(title="Lucidfish", version=__version__, docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "connect-src 'self' https://api.chess.com https://lichess.org; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'")


@app.middleware("http")
async def _security(request: Request, call_next):
    host = (request.headers.get("host") or "").lower()
    hostname = host.rsplit(":", 1)[0] if not host.endswith("]") else host
    if hostname not in _allowed_hosts():
        return _error("Host not allowed. Open Lucidfish via http://127.0.0.1 (or add this host to "
                      "LUCIDFISH_ALLOWED_HOSTS).", 403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if request.headers.get("x-lucidfish") != "1" or (
                origin and (urlparse(origin).hostname or "") not in _allowed_hosts()):
            return _error("Cross-site request blocked.", 403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Content-Security-Policy", _CSP)
    return response


# ================================================================ pages

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


_PIECE_NAME = re.compile(r"^[wb][KQRBNP]$")


@app.get("/pieces/{name}.svg", include_in_schema=False)
def piece(name: str):
    """Piece images for the board, drawn by python-chess (no external image host)."""
    if not _PIECE_NAME.match(name):
        return _error("unknown piece", 404)
    symbol = name[1] if name[0] == "w" else name[1].lower()
    return Response(chess.svg.piece(chess.Piece.from_symbol(symbol)), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=604800"})


# ================================================================ jobs

class Job:
    """State of one background analysis, safe to read while it is written."""

    def __init__(self, total: int, headers: dict, side: str | None):
        self.id = uuid.uuid4().hex[:12]
        self.lock = threading.Lock()
        self.status = "queued"            # queued → running → done | stopped | error
        self.total = total
        self.headers = headers
        self.side = side
        self.engine_done = 0
        self.label = "Waiting for the engine…"
        self.moves: list[dict] = []
        self.review = ""
        self.error = ""
        self.warnings: list[str] = []
        self.accuracy: dict = {}
        self.opening = ""
        self.coach = ""
        self.game_id: int | None = None
        self.stop = False
        self.created = time.time()
        self.finished: float | None = None

    def progress(self, stage: str, done: int, total: int, label: str) -> None:
        with self.lock:
            if stage == "engine":
                self.engine_done = done
            elif stage == "review":
                self.label = "Writing the post-game review…"
                return
            self.label = label

    def add_move(self, move) -> None:
        data = move.to_dict()
        with self.lock:
            self.moves.append(data)

    def snapshot(self, since: int = 0) -> dict:
        with self.lock:
            return {
                "id": self.id, "status": self.status, "total": self.total, "headers": self.headers,
                "side": self.side, "engine_done": self.engine_done, "done": len(self.moves),
                "label": self.label, "moves": self.moves[since:], "since": since,
                "review": self.review, "error": self.error, "warnings": self.warnings,
                "accuracy": self.accuracy, "opening": self.opening, "coach": self.coach,
                "game_id": self.game_id,
            }


JOBS: dict[str, Job] = {}
_JOBS_LOCK = threading.Lock()
_ENGINE_SLOT = threading.Semaphore(1)   # one analysis at a time gets the full CPU
_ENGINE_BUSY = threading.Event()


def _register(job: Job) -> None:
    with _JOBS_LOCK:
        cutoff = time.time() - 3600
        for jid in [j for j, v in JOBS.items() if v.finished and v.finished < cutoff]:
            del JOBS[jid]
        while len(JOBS) >= 30:
            JOBS.pop(next(iter(JOBS)))
        JOBS[job.id] = job


def _acquire_engine(should_stop) -> bool:
    """Wait for the engine slot; gives up if the caller is stopped meanwhile."""
    while not should_stop():
        if _ENGINE_SLOT.acquire(timeout=0.5):
            _ENGINE_BUSY.set()
            return True
    return False


def _release_engine() -> None:
    _ENGINE_BUSY.clear()
    _ENGINE_SLOT.release()


def _active_profile() -> dict | None:
    return store.get_profile(store.active_id())


def _refresh_player_summary(pid: int) -> tuple[str, str]:
    """Re-write the coach profile from stats + recent reviews. Returns (summary, error)."""
    stats = store.aggregate_stats(pid)
    if stats.get("games", 0) < 2:
        return "", "Analyse at least two games first."
    coach, warnings = build_coach(settings.load_config())
    if not coach.available:
        return "", warnings[0] if warnings else "The AI coach is turned off in Settings."
    try:
        summary = coach.llm.generate(
            PLAYER_SUMMARY_SYSTEM,
            build_player_summary_prompt(stats, store.recent_reviews(pid), profile=store.get_profile(pid)))
    except LLMError as e:
        return "", str(e)
    store.set_summary(pid, summary)
    return summary, ""


def _run_analysis(job: Job, pgn: str, side: str | None, elo: int | None) -> None:
    should_stop = lambda: job.stop  # noqa: E731
    if not _acquire_engine(should_stop):
        with job.lock:
            job.status, job.finished = "stopped", time.time()
        return
    try:
        with job.lock:
            job.status, job.label = "running", "Starting the engine…"
        cfg = settings.load_config()
        cfg.user_elo = elo
        profile = _active_profile() or {}
        report = analyze_game(pgn, cfg, side_filter=side, progress=job.progress, on_move=job.add_move,
                              should_stop=should_stop, player_context=profile.get("summary") or "",
                              level=profile.get("level") or None, engine_cache=store.EngineCache())
        with job.lock:
            job.review, job.warnings = report.review, report.warnings
            job.accuracy, job.opening, job.coach = report.accuracy, report.opening, report.coach
            job.status = "stopped" if job.stop else "done"
            moves = list(job.moves)
        pid = profile.get("id")
        if job.status == "done" and pid:
            job.game_id = store.save_game(pid, pgn, report.headers, side, elo, report.opening, report.review,
                                          moves, report.time_class, report.accuracy, replace=True)
            threading.Thread(target=_refresh_player_summary, args=(pid,), daemon=True).start()
    except Exception as e:  # surface any failure to the UI instead of dying silently
        with job.lock:
            job.status, job.error = "error", str(e)
    finally:
        _release_engine()
        with job.lock:
            job.finished = time.time()


class AnalyzeReq(BaseModel):
    pgn: str = Field(max_length=2_000_000)
    side: Literal["white", "black"] | None = None
    elo: int | None = Field(default=None, ge=100, le=3500)


@app.post("/api/analyze")
def analyze(req: AnalyzeReq):
    try:
        game, warnings = parse_game(req.pgn)
    except ValueError as e:
        return _error(str(e))
    job = Job(sum(1 for _ in game.mainline_moves()), dict(game.headers), req.side)
    job.warnings = warnings
    _register(job)
    threading.Thread(target=_run_analysis, args=(job, req.pgn, req.side, req.elo),
                     name=f"lucidfish-job-{job.id}", daemon=True).start()
    return {"job_id": job.id}


@app.get("/api/job/{job_id}")
def job_status(job_id: str, since: int = 0):
    job = JOBS.get(job_id)
    if job is None:
        return _error("This analysis no longer exists (the server may have restarted).", 404)
    return job.snapshot(max(0, since))


@app.post("/api/job/{job_id}/stop")
def job_stop(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        return _error("unknown job", 404)
    job.stop = True
    return {"ok": True}


# ================================================================ positions

_STATUS_MESSAGES = {
    chess.STATUS_NO_WHITE_KING: "White needs a king.",
    chess.STATUS_NO_BLACK_KING: "Black needs a king.",
    chess.STATUS_TOO_MANY_KINGS: "Each side must have exactly one king.",
    chess.STATUS_TOO_MANY_WHITE_PAWNS: "White has more than 8 pawns.",
    chess.STATUS_TOO_MANY_BLACK_PAWNS: "Black has more than 8 pawns.",
    chess.STATUS_PAWNS_ON_BACKRANK: "Pawns can't stand on the first or last rank.",
    chess.STATUS_TOO_MANY_WHITE_PIECES: "White has too many pieces.",
    chess.STATUS_TOO_MANY_BLACK_PIECES: "Black has too many pieces.",
    chess.STATUS_OPPOSITE_CHECK: "The side NOT to move is in check — switch whose turn it is.",
    chess.STATUS_TOO_MANY_CHECKERS: "The king is attacked by too many pieces to be a legal position.",
    chess.STATUS_IMPOSSIBLE_CHECK: "That check could not arise in a real game.",
}


class PositionReq(BaseModel):
    fen: str = Field(max_length=100)
    perspective: Literal["white", "black"] | None = None
    level: str | None = Field(default=None, max_length=20)


@app.post("/api/position")
def position(req: PositionReq):
    """One-shot analysis of a set-up position (board editor mode)."""
    try:
        board = chess.Board(req.fen.strip())
    except ValueError as e:
        return _error(f"That FEN is not valid: {e}")
    board.castling_rights = board.clean_castling_rights()   # drop impossible castling rights
    if not board.is_valid():
        status = board.status()
        reasons = [msg for flag, msg in _STATUS_MESSAGES.items() if status & flag]
        return _error("Illegal position: " + (" ".join(reasons) or "check the pieces."))
    if board.is_checkmate():
        return _error("This position is already checkmate.")
    if board.is_stalemate():
        return _error("This position is stalemate.")
    profile = _active_profile() or {}
    try:
        result = analyze_position(board.fen(), settings.load_config(), req.perspective,
                                  req.level or profile.get("level"), engine_cache=store.EngineCache())
    except RuntimeError as e:
        return _error(str(e), 500)
    result["fen"] = board.fen()
    return result


# ================================================================ settings

@app.get("/api/settings")
def get_settings():
    return settings.public_settings()


@app.post("/api/settings")
def save_settings(values: dict):
    try:
        settings.save(values)
    except ValueError as e:
        return _error(str(e))
    _HEALTH.clear()
    return settings.public_settings()


class KeyReq(BaseModel):
    provider: str
    key: str = Field(max_length=600)


@app.post("/api/settings/key")
def save_key(req: KeyReq):
    if req.provider not in PROVIDERS:
        return _error("Unknown provider.")
    try:
        where = credentials.set_key(req.provider, req.key)
    except ValueError as e:
        return _error(str(e))
    _HEALTH.clear()
    store_name = credentials.secure_store_name() or "your system keychain"
    message = (f"Saved securely in {store_name}." if where == "keychain" else
               "No secure credential store is available on this system, so the key is kept in memory "
               "until Lucidfish stops. To keep it permanently, set it as an environment variable or in "
               "a .env file (see the README).")
    return {"stored": where, "message": message,
            "key": credentials.get_key(req.provider, PROVIDERS[req.provider].key_env).public()}


@app.delete("/api/settings/key/{provider}")
def delete_key(provider: str):
    if provider not in PROVIDERS:
        return _error("Unknown provider.")
    credentials.delete_key(provider)
    _HEALTH.clear()
    return {"key": credentials.get_key(provider, PROVIDERS[provider].key_env).public()}


class TestReq(BaseModel):
    provider: str
    model: str | None = Field(default=None, max_length=200)
    base_url: str | None = Field(default=None, max_length=300)
    key: str | None = Field(default=None, max_length=600)


def _llm_config(provider: str, model: str | None = None, base_url: str | None = None, key: str | None = None):
    cfg = settings.load_config(provider=provider).llm
    if model:
        cfg.model = model
    if base_url is not None:
        cfg.base_url = base_url
    if key:
        cfg.api_key = credentials.validate_key(key)
    return cfg


@app.post("/api/settings/test")
def test_llm(req: TestReq):
    """Try the given (possibly unsaved) provider settings with one tiny request."""
    if req.provider not in PROVIDERS:
        return _error("Unknown provider.")
    try:
        return make_provider(_llm_config(req.provider, req.model, req.base_url, req.key)).check()
    except (LLMError, ValueError) as e:
        return _error(str(e))


@app.get("/api/settings/models")
def list_models(provider: str, base_url: str | None = None):
    if provider not in PROVIDERS:
        return _error("Unknown provider.")
    try:
        return {"models": make_provider(_llm_config(provider, base_url=base_url)).list_models()}
    except (LLMError, ValueError) as e:
        return _error(str(e))
    except Exception:
        return {"models": []}


@app.get("/api/cache")
def cache_info():
    return store.cache_stats()


@app.post("/api/cache/clear")
def cache_clear():
    store.clear_caches()
    return store.cache_stats()


# ---------------------------------------------------------------- health

_HEALTH: dict[str, tuple[float, dict]] = {}


def _cached(key: str, ttl: float, fn) -> dict:
    hit = _HEALTH.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fn()
    _HEALTH[key] = (time.time(), value)
    return value


def _engine_health() -> dict:
    cfg = settings.load_config().engine
    try:
        with EngineAnalyzer(cfg) as engine:
            return {"ok": True, "name": engine.name, "path": cfg.path, "depth": cfg.depth,
                    "threads": cfg.threads}
    except RuntimeError as e:
        return {"ok": False, "error": str(e), "path": cfg.path}


def _coach_health() -> dict:
    cfg = settings.load_config().llm
    spec = PROVIDERS.get(cfg.provider)
    base = {"enabled": cfg.enabled, "provider": cfg.provider, "label": spec.label if spec else cfg.provider,
            "model": cfg.model or (spec.default_model if spec else ""), "local": bool(spec and spec.local)}
    if not cfg.enabled:
        return {**base, "ok": True, "message": "AI coach off — engine analysis only."}
    try:
        provider = make_provider(cfg)
        if spec and spec.id == "ollama":
            installed = provider.list_models()
            if installed and not {provider.model, f"{provider.model}:latest"} & set(installed):
                return {**base, "ok": False, "message": f"'{provider.model}' is not installed in Ollama. "
                                                       f"Run `ollama pull {provider.model}`."}
        return {**base, "ok": True, "message": f"Ready: {provider.describe()}"}
    except LLMError as e:
        return {**base, "ok": False, "message": str(e)}


@app.get("/api/health")
def health(refresh: bool = False):
    if refresh:
        _HEALTH.clear()
    return {
        "version": __version__,
        "engine": _cached("engine", 120, _engine_health),
        "coach": _cached("coach", 20, _coach_health),
        "busy": _ENGINE_BUSY.is_set(),
    }


# ================================================================ profiles

class ProfileReq(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    chesscom_user: str = Field(default="", max_length=40)
    lichess_user: str = Field(default="", max_length=40)
    level: Literal["", "beginner", "casual", "club", "advanced"] = ""
    elo_bullet: int | None = Field(default=None, ge=100, le=3500)
    elo_blitz: int | None = Field(default=None, ge=100, le=3500)
    elo_rapid: int | None = Field(default=None, ge=100, le=3500)


@app.get("/api/profiles")
def profiles():
    return {"profiles": store.list_profiles(), "active": store.active_id()}


@app.post("/api/profiles")
def create_profile(req: ProfileReq):
    try:
        pid = store.create_profile(**req.model_dump())
    except Exception:
        return _error("A profile with that name already exists.")
    return {"id": pid}


@app.post("/api/profiles/{pid}/activate")
def activate_profile(pid: int):
    if store.get_profile(pid) is None:
        return _error("unknown profile", 404)
    store.set_active(pid)
    return {"ok": True}


@app.post("/api/profiles/{pid}")
def update_profile(pid: int, req: ProfileReq):
    if store.get_profile(pid) is None:
        return _error("unknown profile", 404)
    try:
        store.update_profile(pid, **req.model_dump())
    except Exception:
        return _error("A profile with that name already exists.")
    return {"ok": True}


@app.delete("/api/profiles/{pid}")
def delete_profile(pid: int):
    if store.get_profile(pid) is None:
        return _error("unknown profile", 404)
    store.delete_profile(pid)
    return {"ok": True, "active": store.active_id()}


@app.get("/api/profile")
def profile():
    pid = store.active_id()
    p = store.get_profile(pid)
    if p is None:
        return {"profile": None}
    games = store.list_games(pid)
    return {"profile": p, "stats": store.aggregate_stats(pid, games), "games": games}


@app.post("/api/profile/refresh_summary")
def refresh_summary():
    pid = store.active_id()
    if not pid:
        return _error("No active profile.")
    summary, error = _refresh_player_summary(pid)
    if error:
        return _error(error)
    return {"summary": summary}


@app.get("/api/profile/game/{game_id}")
def profile_game(game_id: int):
    g = store.get_game(game_id)
    if g is None:
        return _error("unknown game", 404)
    return {
        "id": g["id"], "pgn": g["pgn"],
        "headers": {"White": g["white"], "Black": g["black"], "Result": g["result"], "Date": g["date"]},
        "moves": g["moves"], "review": g["review"], "side": g["user_side"], "opening": g["opening"],
        "accuracy": {g["user_side"]: g["accuracy"]} if g.get("user_side") and g.get("accuracy") else {},
    }


@app.delete("/api/profile/game/{game_id}")
def delete_game(game_id: int):
    store.delete_game(game_id)
    return {"ok": True}


# ---------------------------------------------------------------- batch import

class ImportGame(BaseModel):
    pgn: str = Field(max_length=2_000_000)
    side: Literal["white", "black"] | None = None
    elo: int | None = Field(default=None, ge=100, le=3500)


class ImportReq(BaseModel):
    games: list[ImportGame] = Field(max_length=50)
    force: bool = False   # re-analyse games already in the profile


_IMPORT: dict = {"status": "idle", "done": 0, "total": 0, "current": "", "errors": 0, "skipped": 0,
                 "last_error": "", "stop": False}
_IMPORT_LOCK = threading.Lock()


@app.post("/api/profile/import")
def profile_import(req: ImportReq):
    pid = store.active_id()
    if not pid:
        return _error("Create a profile first.")
    with _IMPORT_LOCK:
        if _IMPORT["status"] == "running":
            return _error("A batch analysis is already running.", 409)
        _IMPORT.update(status="running", done=0, total=len(req.games), current="Queued…", errors=0,
                       skipped=0, last_error="", stop=False)

    def run():
        profile_row = store.get_profile(pid) or {}
        for g in req.games:
            if _IMPORT["stop"]:
                break
            try:
                if not req.force and store.has_game(pid, g.pgn):
                    _IMPORT["skipped"] += 1
                    continue
                if not _acquire_engine(lambda: _IMPORT["stop"]):
                    break
                try:
                    _IMPORT["current"] = f"Analysing game {_IMPORT['done'] + 1} of {_IMPORT['total']}"
                    cfg = settings.load_config()
                    cfg.user_elo = g.elo
                    moves: list[dict] = []
                    report = analyze_game(g.pgn, cfg, side_filter=g.side,
                                          on_move=lambda m, acc=moves: acc.append(m.to_dict()),
                                          should_stop=lambda: _IMPORT["stop"],
                                          player_context=profile_row.get("summary") or "",
                                          level=profile_row.get("level") or None,
                                          engine_cache=store.EngineCache())
                finally:
                    _release_engine()
                if not _IMPORT["stop"]:
                    store.save_game(pid, g.pgn, report.headers, g.side, g.elo, report.opening, report.review,
                                    moves, report.time_class, report.accuracy, replace=req.force)
            except Exception as e:
                _IMPORT["errors"] += 1
                _IMPORT["last_error"] = str(e)
            finally:
                _IMPORT["done"] += 1
        if not _IMPORT["stop"]:
            _IMPORT["current"] = "Updating your coach review…"
            _refresh_player_summary(pid)
        _IMPORT["status"] = "stopped" if _IMPORT["stop"] else "done"
        _IMPORT["current"] = ""

    threading.Thread(target=run, name="lucidfish-import", daemon=True).start()
    return {"ok": True}


@app.get("/api/profile/import_status")
def profile_import_status():
    return {k: v for k, v in _IMPORT.items() if k != "stop"}


@app.post("/api/profile/import/stop")
def profile_import_stop():
    _IMPORT["stop"] = True
    return {"ok": True}


# ================================================================ export

class ExportReq(BaseModel):
    format: Literal["pgn", "md"]
    pgn: str = Field(default="", max_length=2_000_000)
    headers: dict = Field(default_factory=dict)
    moves: list[dict] = Field(default_factory=list, max_length=1200)
    review: str = Field(default="", max_length=50_000)
    opening: str = Field(default="", max_length=200)
    accuracy: dict = Field(default_factory=dict)
    side: Literal["white", "black"] | None = None


def _filename(headers: dict, ext: str) -> str:
    raw = f"{headers.get('White', 'White')}-vs-{headers.get('Black', 'Black')}"
    return (re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("_") or "game")[:80] + ext


@app.post("/api/export")
def export(req: ExportReq):
    try:
        if req.format == "pgn":
            body = annotated_pgn(req.pgn, req.moves, req.review, req.accuracy)
            media, ext = "application/x-chess-pgn", ".pgn"
        else:
            body = markdown_report(req.headers, req.moves, req.review, req.opening, req.accuracy, req.side)
            media, ext = "text/markdown", ".md"
    except (ValueError, KeyError) as e:
        return _error(f"Could not export this game: {e}")
    return Response(body, media_type=f"{media}; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{_filename(req.headers, ext)}"'})


# ================================================================ chat

class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=6000)


class ChatReq(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=60)
    context: str = Field(default="", max_length=30_000)   # frontend summary of what's on screen


@app.post("/api/chat")
def coach_chat(req: ChatReq):
    coach, warnings = build_coach(settings.load_config())
    if not coach.available:
        return _error(warnings[0] if warnings else "The AI coach is turned off in Settings.", 503)
    system = COACH_CHAT_SYSTEM
    summary = (_active_profile() or {}).get("summary") or ""
    if summary:
        system += "\n\n--- Coach profile of this player from their previous games ---\n" + summary
    if req.context:
        system += "\n\n--- Verified context for what the player is looking at ---\n" + req.context
    history = [m.model_dump() for m in req.messages[-12:]]
    while history and history[0]["role"] != "user":   # conversations must start with the user
        history.pop(0)
    try:
        return {"reply": coach.llm.chat(system, history, max_tokens=700)}
    except LLMError as e:
        return _error(str(e), 502)


# ================================================================ server

def _free_port(host: str, start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"No free port found between {start} and {start + 19}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lucidfish web", description="Start the Lucidfish web app.")
    parser.add_argument("--host", default=os.environ.get("LUCIDFISH_HOST", "127.0.0.1"),
                        help="interface to listen on (default 127.0.0.1 = this computer only)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("LUCIDFISH_PORT", DEFAULT_PORT)))
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = parser.parse_args(argv)

    import uvicorn
    port = _free_port(args.host, args.port)
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{shown}:{port}"
    print(f"Lucidfish {__version__} is running at {url}  (press Ctrl+C to stop)")
    if args.host not in _LOOPBACK:
        print("Warning: listening on a network interface. Other devices can reach Lucidfish; add the "
              "host name they use to LUCIDFISH_ALLOWED_HOSTS. Only do this on a network you trust.")
    if not args.no_browser:
        threading.Timer(1.2, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=args.host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
