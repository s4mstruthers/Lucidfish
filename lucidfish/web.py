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
from .config import Config
from .engine import EngineAnalyzer
from .llm import COACH_CHAT_SYSTEM, OllamaProvider
from .pipeline import analyze_game

app = FastAPI(title="Lucidfish")
STATIC = Path(__file__).parent / "static"
JOBS: dict[str, dict] = {}


class AnalyzeReq(BaseModel):
    pgn: str
    side: str | None = None
    elo: int | None = None


class PositionReq(BaseModel):
    fen: str


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
        "candidates": [
            {"san": l.move_san, "score": _white_pov_score(l, m.side == "White"), "line": l.pv_text,
             "idea": m.line_ideas.get(l.move_san, ""),
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
            )
            job["review"] = report.review
            job["status"] = "stopped" if job.get("stop") else "done"
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
    prompt = "\n".join([
        f"Position (FEN): {req.fen}. {side} to move.",
        "Engine candidate moves (best first):",
        *(f"  {i}. {l.move_san}  eval {l.score_str}  line: {l.pv_text}"
          for i, l in enumerate(lines, 1)),
        "Position facts:",
        *(f"  {x}" for x in features),
        "\nGive a short assessment of this position and the plans for both sides, "
        "then explain why the engine's top move makes sense.",
    ])
    from .llm import SYSTEM_PROMPT
    commentary = OllamaProvider(cfg.llm).generate(SYSTEM_PROMPT, prompt)

    return {
        "lines": [{"san": l.move_san, "score": _white_pov_score(l, board.turn == chess.WHITE),
                   "line": l.pv_text} for l in lines],
        "features": features,
        "commentary": commentary,
        "turn": side,
    }


@app.post("/api/chat")
def coach_chat(req: ChatReq):
    cfg = Config()
    system = COACH_CHAT_SYSTEM
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
