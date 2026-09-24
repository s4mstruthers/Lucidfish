"""Engine layer: extract *evidence* from Stockfish, not just a best move.

For each position we gather:
  - top-N candidate lines (MultiPV) with evals and principal variations
  - the change in winning chances caused by the move actually played
  - a refutation line: if the played move was bad, WHY — i.e. the opponent's
    punishing continuation

This structured evidence is what grounds the LLM so it can't hallucinate tactics.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Protocol

import chess
import chess.engine

from .config import IS_MACOS, IS_WINDOWS, AnalysisConfig, EngineConfig
from .scoring import MATE_CP, cp_value, move_accuracy, win_percent

PV_PLIES = 8  # plies of each principal variation kept for explanations


@dataclass
class Line:
    """One engine candidate line for a position (scores from the side to move)."""
    move_san: str
    move_uci: str
    score_cp: int | None          # centipawns; None if mate
    mate_in: int | None           # signed: positive = side to move mates
    pv_san: list[str] = field(default_factory=list)
    pv_uci: list[str] = field(default_factory=list)
    pv_text: str = ""             # numbered movetext, e.g. "5. Qxf3 dxe5 6. Bc4"

    @property
    def score_str(self) -> str:
        if self.mate_in is not None:
            return f"mate in {abs(self.mate_in)}" + (" (against)" if self.mate_in < 0 else "")
        return f"{self.score_cp / 100:+.2f}"

    @property
    def value(self) -> int:
        """Clamped centipawns (mate = ±MATE_CP) for comparisons."""
        return cp_value(self.score_cp, self.mate_in)


@dataclass
class MoveAnalysis:
    """Full engine evidence for one played move. Evals are from the mover's view."""
    fen_before: str
    played_san: str
    played_uci: str
    candidates: list[Line]              # top MultiPV lines BEFORE the move
    best_san: str
    best_uci: str
    eval_before_cp: int | None          # best-move eval
    eval_before_mate: int | None
    eval_after_cp: int | None           # eval after the played move
    eval_after_mate: int | None
    cp_loss: int                        # clamped centipawn loss vs the best move
    win_before: float                   # mover's winning chances with best play (0-100)
    win_after: float                    # ... after the move actually played
    accuracy: float                     # Lichess-style move accuracy (0-100)
    classification: str                 # best/good/inaccuracy/mistake/blunder
    mate_event: str = ""                # "", "missed_mate", "allowed_mate", "delivered_mate"
    game_result: str = ""               # "1-0" / "0-1" / "1/2-1/2" if the move ended the game
    refutation_san: list[str] = field(default_factory=list)  # punishing reply line if bad
    refutation_text: str = ""
    suspicious: bool = False            # engine's own top move looks much worse one ply later

    @property
    def win_loss(self) -> float:
        return max(0.0, self.win_before - self.win_after)


class EvalCache(Protocol):
    """Storage for finished searches (implemented by lucidfish.store.EngineCache)."""
    def get(self, key: str, strength: int, multipv: int) -> list | None: ...
    def put(self, key: str, strength: int, multipv: int, data: list) -> None: ...


def install_hint() -> str:
    if IS_WINDOWS:
        return ("Download Stockfish from https://stockfishchess.org/download/, unzip it, then set the "
                "path to stockfish*.exe in Settings (or the LUCIDFISH_STOCKFISH environment variable).")
    if IS_MACOS:
        return "Install it with `brew install stockfish`, or set LUCIDFISH_STOCKFISH=/path/to/stockfish."
    return ("Install it with your package manager (e.g. `sudo apt install stockfish`, `sudo dnf install "
            "stockfish` or `sudo pacman -S stockfish`), or set LUCIDFISH_STOCKFISH=/path/to/stockfish.")


def numbered_line(board: chess.Board, sans: list[str]) -> str:
    """'5. Qxf3 dxe5 6. Bc4' / '5... dxe5 6. Bc4' — unambiguous about who moves."""
    n, white = board.fullmove_number, board.turn == chess.WHITE
    parts = []
    for i, san in enumerate(sans):
        if white:
            parts.append(f"{n}. {san}")
        else:
            parts.append(f"{n}... {san}" if i == 0 else san)
            n += 1
        white = not white
    return " ".join(parts)


class EngineAnalyzer:
    """Thin, resource-safe wrapper around a UCI Stockfish process."""

    def __init__(self, cfg: EngineConfig, cache: EvalCache | None = None):
        self.cfg = cfg
        self.cache = cache if cfg.use_cache else None
        popen_args = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}
        try:
            self._engine = chess.engine.SimpleEngine.popen_uci(cfg.path, **popen_args)
        except (FileNotFoundError, PermissionError, OSError) as e:
            raise RuntimeError(f"Stockfish could not be started from '{cfg.path}'. {install_hint()}") from e
        except chess.engine.EngineError as e:
            raise RuntimeError(f"'{cfg.path}' does not look like a UCI chess engine: {e}") from e
        options = {k: v for k, v in (("Threads", cfg.threads), ("Hash", cfg.hash_mb))
                   if k in self._engine.options}
        if options:
            self._engine.configure(options)
        self.name = self._engine.id.get("name", "engine")

    def close(self) -> None:
        try:
            self._engine.quit()
        except Exception:  # already dead: make sure the process is gone
            try:
                self._engine.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _limit(self, extra_depth: int = 0) -> tuple[chess.engine.Limit, str, int]:
        if self.cfg.movetime_s:
            secs = self.cfg.movetime_s * (1 + extra_depth / 2)
            return chess.engine.Limit(time=secs), "time", int(secs * 1000)
        depth = self.cfg.depth + extra_depth
        return chess.engine.Limit(depth=depth), "depth", depth

    def top_lines(self, board: chess.Board, multipv: int | None = None, extra_depth: int = 0) -> list[Line]:
        """Top-N candidate lines for the side to move ([] if the game is over).

        `extra_depth` searches deeper than configured (used to double-check
        suspicious verdicts)."""
        legal = board.legal_moves.count()
        if legal == 0:
            return []
        # A forced move needs no alternatives, and MultiPV is what makes search slow.
        multipv = min(multipv or self.cfg.multipv, legal)
        limit, kind, strength = self._limit(extra_depth)
        key = f"{self.name}|{kind}|{board.epd()}"

        raw = self.cache.get(key, strength, multipv) if self.cache else None
        if raw is None:
            try:
                infos = self._engine.analyse(board, limit, multipv=multipv)
            except chess.engine.EngineTerminatedError as e:
                raise RuntimeError("Stockfish stopped unexpectedly during analysis.") from e
            raw = []
            for info in infos:
                pv, score = info.get("pv"), info.get("score")
                if not pv or score is None:
                    continue
                s = score.pov(board.turn)
                raw.append([[m.uci() for m in pv[:PV_PLIES]], s.score(), s.mate()])
            if self.cache and raw:
                self.cache.put(key, strength, multipv, raw)
        return [line for entry in raw[:multipv] if (line := _build_line(board, *entry))]


def _build_line(board: chess.Board, pv_uci: list[str], cp: int | None, mate: int | None) -> Line | None:
    tmp = board.copy(stack=False)
    sans: list[str] = []
    try:
        for uci in pv_uci:
            mv = chess.Move.from_uci(uci)
            sans.append(tmp.san(mv))
            tmp.push(mv)
    except (ValueError, AssertionError):
        if not sans:
            return None  # corrupt cache entry: drop the line rather than crash
    return Line(move_san=sans[0], move_uci=pv_uci[0], score_cp=None if mate is not None else cp,
                mate_in=mate, pv_san=sans, pv_uci=pv_uci[:len(sans)],
                pv_text=numbered_line(board, sans))


# --------------------------------------------------------------- pure helpers

def describe_move(board: chess.Board, move: chess.Move) -> str:
    """Plain-English statement of what a move physically does — verified facts an
    LLM must not contradict (its main hallucination mode is inventing captures
    and misidentifying pieces)."""
    piece = board.piece_at(move.from_square)
    if board.is_castling(move):
        parts = ["castles " + ("kingside" if chess.square_file(move.to_square) > chess.square_file(move.from_square)
                               else "queenside")]
    else:
        parts = [f"{chess.piece_name(piece.piece_type)} from "
                 f"{chess.square_name(move.from_square)} to {chess.square_name(move.to_square)}"]
        if board.is_en_passant(move):
            parts.append("captures a pawn en passant")
        elif board.is_capture(move):
            victim = board.piece_at(move.to_square)
            parts.append(f"captures the {chess.piece_name(victim.piece_type)} on "
                         f"{chess.square_name(move.to_square)}")
        else:
            parts.append("no capture")
    if move.promotion:
        parts.append(f"promotes to a {chess.piece_name(move.promotion)}")
    after = board.copy(stack=False)
    after.push(move)
    if after.is_checkmate():
        parts.append("delivers CHECKMATE — the game is over, this move wins")
    elif after.is_check():
        parts.append("gives check")
    return ", ".join(parts)


def _classify(analysis_inputs: dict, th: AnalysisConfig) -> str:
    """Verdict for a move, following Lichess's rules.

    Mate-related events are judged the way Lichess does (missing a forced mate
    is only a small error if you remain completely winning); everything else
    by how many percentage points of winning chances the move gave away.
    """
    a = analysis_inputs
    if a["played_is_best"] or a["mate_event"] == "delivered_mate":
        return "best"
    raw_after = a["after_cp"] if a["after_mate"] is None else (10**6 if a["after_mate"] > 0 else -10**6)
    raw_before = a["before_cp"] if a["before_mate"] is None else (10**6 if a["before_mate"] > 0 else -10**6)
    if a["mate_event"] == "missed_mate":
        return "inaccuracy" if raw_after > 999 else "mistake" if raw_after > 700 else "blunder"
    if a["mate_event"] == "allowed_mate":
        return "inaccuracy" if raw_before < -999 else "mistake" if raw_before < -700 else "blunder"
    loss = a["win_loss"]
    if loss >= th.blunder_win:
        return "blunder"
    if loss >= th.mistake_win:
        return "mistake"
    if loss >= th.inaccuracy_win:
        return "inaccuracy"
    return "best" if a["cp_loss"] <= 10 else "good"


def build_move_analysis(
    board: chess.Board,           # position BEFORE the move
    move: chess.Move,
    candidates: list[Line],       # top_lines() of the position before the move
    lines_after: list[Line],      # top_lines() of the position AFTER ([] if game over)
    thresholds: AnalysisConfig,
) -> MoveAnalysis:
    """Assemble a MoveAnalysis from two already-computed engine searches.

    Key efficiency insight: the position after move n IS the position before
    move n+1, so the pipeline analyses each position exactly once and passes
    both results here. The refutation line also comes free — it's simply the
    opponent's best line in the after-position.

    `lines_after` scores are from the opponent's perspective (they are to move),
    so they are negated to get the mover's point of view.
    """
    best = candidates[0] if candidates else None
    before_cp = best.score_cp if best else 0
    before_mate = best.mate_in if best else None
    after = board.copy(stack=False)
    after.push(move)

    game_result = ""
    if lines_after:
        opp = lines_after[0]
        after_cp = -opp.score_cp if opp.score_cp is not None else None
        after_mate = -opp.mate_in if opp.mate_in is not None else None
    elif after.is_checkmate():
        after_cp, after_mate = None, 1           # the mover just won
        game_result = "1-0" if board.turn == chess.WHITE else "0-1"
    else:
        after_cp, after_mate = 0, None           # stalemate / insufficient material / draw
        game_result = "1/2-1/2"

    mate_event = ""
    if after.is_checkmate():
        mate_event = "delivered_mate"
    elif (before_mate or 0) > 0 and not (after_mate or 0) > 0:
        mate_event = "missed_mate"
    elif (after_mate or 0) < 0 and not (before_mate or 0) < 0:
        mate_event = "allowed_mate"

    best_value, after_value = cp_value(before_cp, before_mate), cp_value(after_cp, after_mate)
    win_before, win_after = win_percent(best_value), win_percent(after_value)
    cp_loss = max(0, min(2 * MATE_CP, best_value - after_value))
    played_is_best = best is not None and best.move_uci == move.uci()
    suspicious = False
    if played_is_best:
        if win_before - win_after < thresholds.mistake_win:
            # The engine's own choice scoring slightly lower one ply deeper is search noise.
            mate_event = "" if mate_event != "delivered_mate" else mate_event
            cp_loss, win_after = 0, max(win_after, win_before)
        else:
            # A big drop means the first search missed something (horizon effect):
            # the caller should re-search this position deeper before trusting it.
            suspicious = True

    classification = _classify({
        "played_is_best": played_is_best, "mate_event": mate_event, "cp_loss": cp_loss,
        "win_loss": max(0.0, win_before - win_after),
        "before_cp": before_cp, "before_mate": before_mate,
        "after_cp": after_cp, "after_mate": after_mate,
    }, thresholds)

    refutation_san: list[str] = []
    refutation_text = ""
    if classification in ("inaccuracy", "mistake", "blunder") and lines_after:
        refutation_san = lines_after[0].pv_san
        refutation_text = lines_after[0].pv_text

    return MoveAnalysis(
        fen_before=board.fen(),
        played_san=board.san(move),
        played_uci=move.uci(),
        candidates=candidates,
        best_san=best.move_san if best else "",
        best_uci=best.move_uci if best else "",
        eval_before_cp=before_cp,
        eval_before_mate=before_mate,
        eval_after_cp=after_cp,
        eval_after_mate=after_mate,
        cp_loss=cp_loss,
        win_before=round(win_before, 1),
        win_after=round(win_after, 1),
        accuracy=round(move_accuracy(win_before, win_after), 1),
        classification=classification,
        mate_event=mate_event,
        game_result=game_result,
        refutation_san=refutation_san,
        refutation_text=refutation_text,
        suspicious=suspicious,
    )
