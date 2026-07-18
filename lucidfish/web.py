"""Web UI backend: FastAPI server wrapping the analysis pipeline.

Run with:  python -m lucidfish.web   →  http://127.0.0.1:8420

Design notes:
- Analysis runs in a background thread per job; the frontend polls /api/job/<id>
  and renders moves live as they complete.
- Game listing from chess.com/Lichess happens IN THE BROWSER (their APIs support
  CORS, and a real browser passes Cloudflare's bot checks that block Python/curl).
  The backend only ever receives a PGN.
"""

from __future__ import annotations

import io
import threading
import uuid
from pathlib import Path

import chess
import chess.pgn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import features as feat
from . import store
from .config import Config
from .engine import EngineAnalyzer
from .llm import (COACH_CHAT_SYSTEM, SYSTEM_PROMPT, PLAYER_SUMMARY_SYSTEM,
                  OllamaProvider, _candidate_desc, build_player_summary_prompt)
from .pipeline import analyze_game, _split_line_ideas

store.init()


def _active() -> dict | None:
    return store.get_profile(store.active_id())


def _active_summary() -> str:
    p = _active()
    return (p or {}).get("summary") or ""


def _refresh_player_summary(pid: int) -> None:
    """Re-write the LLM coach profile from current stats + recent reviews."""
    stats = store.aggregate_stats(pid)
    if stats.get("games", 0) < 2:
        return
    try:
        summary = OllamaProvider(Config().llm).generate(
            PLAYER_SUMMARY_SYSTEM,
            build_player_summary_prompt(stats, store.recent_reviews(pid),
                                        profile=store.get_profile(pid)))
        store.set_summary(pid, summary)
    except Exception:
        pass


def _line_steps(fen: str, sans: list[str]) -> list[dict]:
    """Turn a SAN line into playable steps: [{san, from, to, fen-after}] so the
    frontend can walk a variation on the board without a chess library."""
    board = chess.Board(fen)
    steps = []
    try:
        for san in sans:
            mv = board.parse_san(san)
            step = {"san": san, "from": chess.square_name(mv.from_square),
                    "to": chess.square_name(mv.to_square)}
            board.push(mv)
            step["fen"] = board.fen()
            steps.append(step)
    except Exception:
        pass  # a malformed tail just shortens the preview
    return steps


def _san_squares(fen: str, san: str) -> tuple[str, str]:
    try:
        b = chess.Board(fen)
        mv = b.parse_san(san)
        return chess.square_name(mv.from_square), chess.square_name(mv.to_square)
    except Exception:
        return "", ""

app = FastAPI(title="Lucidfish")
STATIC = Path(__file__).parent / "static"
JOBS: dict[str, dict] = {}


class AnalyzeReq(BaseModel):
    pgn: str
    side: str | None = None
    elo: int | None = None


class PositionReq(BaseModel):
    fen: str
    perspective: str | None = None   # "white" / "black" — whose plans to coach
    level: str | None = None         # beginner / casual / club / advanced


class ChatReq(BaseModel):
    messages: list[dict]   # [{role: user|assistant, content: str}, ...]
    context: str = ""      # frontend-built summary of what's on screen


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


def _white_pov_score(line, mover_is_white: bool) -> str:
    """Engine lines are scored from the side-to-move's perspective; the UI shows
    everything from White's perspective (+ = White better), like every chess site."""
    if line.mate_in is not None:
        n = line.mate_in if mover_is_white else -line.mate_in
        return f"#{n}" if n > 0 else f"#{n}"
    cp = line.score_cp if mover_is_white else -line.score_cp
    return f"{cp / 100:+.2f}"


def _serialize_move(m) -> dict:
    """AnnotatedMove → JSON-safe dict, including before/after FENs and move squares."""
    b = chess.Board(m.analysis.fen_before)
    mv = b.parse_san(m.san)
    uci = mv.uci()
    b.push(mv)
    return {
        "n": m.move_number,
        "side": m.side,
        "san": m.san,
        "cls": m.classification,
        "best": m.best_san,
        "eval": m.eval_str,
        "cp_loss": m.cp_loss,
        "expl": m.explanation,
        "fen_before": m.analysis.fen_before,
        "fen_after": b.fen(),
        "from": uci[:2],
        "to": uci[2:4],
        "refutation": m.analysis.refutation_text,
        "opening": m.opening,
        "critical": m.critical,
        "think_s": m.think_s,
        "clock_s": m.clock_s,
        "best_from": _san_squares(m.analysis.fen_before, m.best_san)[0],
        "best_to": _san_squares(m.analysis.fen_before, m.best_san)[1],
        "refutation_steps": _line_steps(
            # refutation starts from the position AFTER the played move
            b.fen(), m.analysis.refutation_san) if m.analysis.refutation_san else [],
        "candidates": [
            {"san": l.move_san, "score": _white_pov_score(l, m.side == "White"), "line": l.pv_text,
             "idea": m.line_ideas.get(l.move_san, ""),
             "steps": _line_steps(m.analysis.fen_before, l.pv_san[:8]),
             # numeric, from the MOVER's perspective (higher = better for whoever moved)
             "cp": l.score_cp if l.score_cp is not None
                   else (100000 if (l.mate_in or 0) > 0 else -100000)}
            for l in m.analysis.candidates[:4]
        ],
    }


@app.post("/api/analyze")
def analyze(req: AnalyzeReq):
    game = chess.pgn.read_game(io.StringIO(req.pgn))
    total = sum(1 for _ in game.mainline_moves()) if game else 0
    if game is None or total == 0:
        return JSONResponse({"error": "Could not parse any moves from that PGN."}, status_code=400)

    job_id = uuid.uuid4().hex[:12]
    job = {
        "status": "running", "progress": "starting engine…",
        "total": total, "side": req.side,
        "headers": dict(game.headers), "moves": [], "review": "", "error": "",
    }
    JOBS[job_id] = job

    def run():
        try:
            cfg = Config()
            cfg.user_elo = req.elo
            report = analyze_game(
                req.pgn, cfg, side_filter=req.side,
                progress=lambda n, s, san: job.update(progress=f"move {n} ({s}): {san}"),
                on_move=lambda m: job["moves"].append(_serialize_move(m)),
                should_stop=lambda: job.get("stop", False),
                player_context=_active_summary(),
            )
            job["review"] = report.review
            job["status"] = "stopped" if job.get("stop") else "done"
            pid = store.active_id()
            if job["status"] == "done" and pid:   # grow the profile with every analysis
                store.save_game(pid, req.pgn, job["headers"], req.side, req.elo,
                                report.opening, job["review"], job["moves"],
                                report.time_class, replace=True)   # re-analysis updates the stored copy
                threading.Thread(target=_refresh_player_summary, args=(pid,), daemon=True).start()
        except Exception as e:  # surface any failure to the UI instead of dying silently
            job["status"] = "error"
            job["error"] = str(e)

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/job/{job_id}/stop")
def job_stop(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    job["stop"] = True
    return {"ok": True}


@app.get("/api/job/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return job


@app.post("/api/position")
def analyze_position(req: PositionReq):
    """One-shot analysis of a manually set-up position (board editor mode)."""
    try:
        board = chess.Board(req.fen)
        if not board.is_valid():
            raise ValueError(board.status())
    except Exception as e:
        return JSONResponse({"error": f"Invalid position: {e}"}, status_code=400)

    cfg = Config()
    with EngineAnalyzer(cfg.engine) as engine:
        lines = engine.top_lines(board)
    f = feat.extract(board)
    features = f.summary_lines()

    side = "White" if board.turn == chess.WHITE else "Black"
    persp = (req.perspective or side.lower()).capitalize()
    level_note = {
        "beginner": "a beginner — explain fundamentals plainly, avoid long lines",
        "casual": "a casual player — fundamentals plus simple plans, short lines",
        "club": "a club player — concrete plans, tactics up to 2-3 moves deep",
        "advanced": "an advanced player — be concrete and positional, deeper lines are fine",
    }.get((req.level or (_active() or {}).get("level") or "").lower(), "")

    prompt = "\n".join([
        f"Position (FEN): {req.fen}. {side} to move.",
        f"You are coaching the {persp} player: assess the position from {persp}'s "
        f"perspective — their plans, their problems, what they should aim for.",
        (f"The player is {level_note}." if level_note else ""),
        "Engine candidate moves (best first). The [brackets] state what each move "
        "physically is — never contradict them:",
        *(f"  {i}. {l.move_san} [{_candidate_desc(req.fen, l.move_san)}]  "
          f"eval {l.score_str}  line: {l.pv_text}"
          for i, l in enumerate(lines, 1)),
        "Position facts:",
        *(f"  {x}" for x in features),
        "\nReply in EXACTLY this format, both sections mandatory:",
        "EXPLANATION:",
        f"<assessment of the position from {persp}'s perspective and the main plans, "
        "then why the engine's top move makes sense>",
        "LINE IDEAS:",
        f"<candidate SAN>: <one short sentence on what that line achieves for {side}>",
        "(one such line per candidate above)",
    ])
    commentary, ideas = _split_line_ideas(OllamaProvider(cfg.llm).generate(SYSTEM_PROMPT, prompt))

    return {
        "lines": [{"san": l.move_san, "score": _white_pov_score(l, board.turn == chess.WHITE),
                   "line": l.pv_text, "idea": ideas.get(l.move_san, ""),
                   "steps": _line_steps(req.fen, l.pv_san[:8])} for l in lines],
        "features": features,
        "commentary": commentary,
        "turn": side,
        "perspective": persp,
    }


class ImportReq(BaseModel):
    games: list[dict]   # [{pgn, side, elo}]
    force: bool = False   # re-analyze games that are already in the profile


IMPORT_JOB: dict = {"status": "idle", "done": 0, "total": 0, "current": "", "errors": 0, "skipped": 0}


@app.post("/api/profile/import")
def profile_import(req: ImportReq):
    if IMPORT_JOB["status"] == "running":
        return JSONResponse({"error": "an import is already running"}, status_code=409)
    pid = store.active_id()
    if not pid:
        return JSONResponse({"error": "create a profile first"}, status_code=400)
    IMPORT_JOB.update(status="running", done=0, total=len(req.games), current="", errors=0, skipped=0)

    def run():
        summary = _active_summary()
        for g in req.games:
            try:
                if not req.force and store.has_game(pid, g["pgn"]):
                    IMPORT_JOB["skipped"] += 1
                    IMPORT_JOB["done"] += 1
                    continue
                IMPORT_JOB["current"] = "analysing game %d/%d" % (IMPORT_JOB["done"] + 1, IMPORT_JOB["total"])
                cfg = Config()
                cfg.user_elo = g.get("elo")
                moves_acc: list[dict] = []
                report = analyze_game(
                    g["pgn"], cfg, side_filter=g.get("side"),
                    on_move=lambda m: moves_acc.append(_serialize_move(m)),
                    player_context=summary)
                store.save_game(pid, g["pgn"], dict(report.headers), g.get("side"), g.get("elo"),
                                report.opening, report.review, moves_acc, report.time_class,
                                replace=req.force)
            except Exception:
                IMPORT_JOB["errors"] += 1
            IMPORT_JOB["done"] += 1
        IMPORT_JOB["current"] = "writing player profile…"
        _refresh_player_summary(pid)
        IMPORT_JOB["status"] = "done"

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


@app.get("/api/profile/import_status")
def profile_import_status():
    return IMPORT_JOB


class ProfileReq(BaseModel):
    name: str
    chesscom_user: str = ""
    level: str = ""
    elo_bullet: int | None = None
    elo_blitz: int | None = None
    elo_rapid: int | None = None


@app.get("/api/profiles")
def profiles():
    return {"profiles": store.list_profiles(), "active": store.active_id()}


@app.post("/api/profiles")
def create_profile(req: ProfileReq):
    try:
        pid = store.create_profile(**req.dict())
    except Exception:
        return JSONResponse({"error": "a profile with that name already exists"}, status_code=400)
    return {"id": pid}


@app.post("/api/profiles/{pid}/activate")
def activate_profile(pid: int):
    if store.get_profile(pid) is None:
        return JSONResponse({"error": "unknown profile"}, status_code=404)
    store.set_active(pid)
    return {"ok": True}


@app.post("/api/profiles/{pid}")
def update_profile(pid: int, req: ProfileReq):
    if store.get_profile(pid) is None:
        return JSONResponse({"error": "unknown profile"}, status_code=404)
    store.update_profile(pid, **req.dict())
    return {"ok": True}


@app.post("/api/profile/refresh_summary")
def refresh_summary():
    pid = store.active_id()
    if not pid:
        return JSONResponse({"error": "no active profile"}, status_code=400)
    _refresh_player_summary(pid)
    return {"summary": (store.get_profile(pid) or {}).get("summary", "")}


@app.get("/api/profile")
def profile():
    pid = store.active_id()
    p = store.get_profile(pid)
    if p is None:
        return {"profile": None}
    return {
        "profile": p,
        "stats": store.aggregate_stats(pid),
        "games": store.list_games(pid),
    }


@app.get("/api/profile/game/{game_id}")
def profile_game(game_id: int):
    g = store.get_game(game_id)
    if g is None:
        return JSONResponse({"error": "unknown game"}, status_code=404)
    return {
        "headers": {"White": g["white"], "Black": g["black"], "Result": g["result"]},
        "moves": g["moves"], "review": g["review"], "side": g["user_side"],
        "opening": g["opening"],
    }


@app.post("/api/chat")
def coach_chat(req: ChatReq):
    cfg = Config()
    system = COACH_CHAT_SYSTEM
    summary = _active_summary()
    if summary:
        system += "\n\n--- Coach profile of this player from their previous games ---\n" + summary
    if req.context:
        system += "\n\n--- Verified context for what the player is looking at ---\n" + req.context
    try:
        reply = OllamaProvider(cfg.llm).chat(system, req.messages[-12:])
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"reply": reply}


def main():
    import uvicorn
    print("Lucidfish web UI → http://127.0.0.1:8420")
    uvicorn.run(app, host="127.0.0.1", port=8420, log_level="warning")


if __name__ == "__main__":
    main()
