"""Engine layer: extract *evidence* from Stockfish, not just a best move.

For each position we gather:
  - top-N candidate lines (MultiPV) with evals and principal variations
  - the eval swing caused by the move actually played
  - a refutation line: if the played move was bad, WHY — i.e. the opponent's
    punishing continuation

This structured evidence is what grounds the LLM so it can't hallucinate tactics.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import chess
import chess.engine

from .config import EngineConfig


@dataclass
class Line:
    """One engine candidate line for a position."""
    move_san: str
    move_uci: str
    score_cp: int | None          # centipawns from side-to-move's perspective; None if mate
    mate_in: int | None           # signed: positive = side to move mates
    pv_san: list[str] = field(default_factory=list)
    pv_text: str = ""             # numbered movetext, e.g. "5. Qxf3 dxe5 6. Bc4" — unambiguous about sides

    @property
    def score_str(self) -> str:
        if self.mate_in is not None:
            return f"mate in {abs(self.mate_in)}" + (" (against)" if self.mate_in < 0 else "")
        return f"{self.score_cp / 100:+.2f}"


@dataclass
class MoveAnalysis:
    """Full engine evidence for one played move."""
    fen_before: str
    played_san: str
    played_uci: str
    candidates: list[Line]              # top MultiPV lines BEFORE the move
    eval_before_cp: int | None          # best-move eval (side to move)
    eval_after_cp: int | None           # eval after the played move (same perspective)
    cp_loss: int                        # how much the played move gave up vs best
    classification: str                 # best/good/inaccuracy/mistake/blunder
    refutation_san: list[str] = field(default_factory=list)  # punishing reply line if bad
    refutation_text: str = ""           # same line as numbered movetext
    best_san: str = ""


class EngineAnalyzer:
    """Thin, resource-safe wrapper around a UCI Stockfish process."""

    def __init__(self, cfg: EngineConfig):
        self.cfg = cfg
        try:
            self._engine = chess.engine.SimpleEngine.popen_uci(cfg.path)
        except FileNotFoundError as e:
            raise RuntimeError(
                f"Stockfish not found at '{cfg.path}'. Install it (brew install stockfish) "
                "or set LUCIDFISH_STOCKFISH=/path/to/stockfish."
            ) from e
        self._engine.configure({"Threads": cfg.threads, "Hash": cfg.hash_mb})

    def close(self):
        self._engine.quit()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- helpers

    def _score_parts(self, score: chess.engine.PovScore, pov: chess.Color) -> tuple[int | None, int | None]:
        s = score.pov(pov)
        if s.is_mate():
            return None, s.mate()
        return s.score(), None

    def top_lines(self, board: chess.Board, multipv: int | None = None) -> list[Line]:
        """Top-N candidate lines for the side to move."""
        multipv = multipv or self.cfg.multipv
        limit = (chess.engine.Limit(time=self.cfg.movetime_s)
                 if self.cfg.movetime_s else chess.engine.Limit(depth=self.cfg.depth))
        infos = self._engine.analyse(board, limit, multipv=multipv)
        lines: list[Line] = []
        for info in infos:
            pv = info.get("pv")
            if not pv:
                continue
            cp, mate = self._score_parts(info["score"], board.turn)
            tmp = board.copy(stack=False)
            pv_san = []
            for mv in pv[:8]:  # 8 plies of PV is plenty for an explanation
                pv_san.append(tmp.san(mv))
                tmp.push(mv)
            lines.append(Line(
                move_san=board.san(pv[0]),
                move_uci=pv[0].uci(),
                score_cp=cp,
                mate_in=mate,
                pv_san=pv_san,
                pv_text=board.variation_san(pv[:8]),
            ))
        return lines


# --------------------------------------------------------------- pure helpers

def describe_move(board: chess.Board, move: chess.Move) -> str:
    """Plain-English statement of what a move physically does — verified facts an
    LLM must not contradict (its main hallucination mode is inventing captures
    and misidentifying pieces)."""
    piece = board.piece_at(move.from_square)
    parts = [f"{chess.piece_name(piece.piece_type)} from "
             f"{chess.square_name(move.from_square)} to {chess.square_name(move.to_square)}"]
    if board.is_castling(move):
        parts = ["castles " + ("kingside" if chess.square_file(move.to_square) == 6 else "queenside")]
    elif board.is_en_passant(move):
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

def _classify(cp_loss: int, thresholds) -> str:
    if cp_loss <= 10:
        return "best"
    if cp_loss < thresholds.inaccuracy_cp:
        return "good"
    if cp_loss < thresholds.mistake_cp:
        return "inaccuracy"
    if cp_loss < thresholds.blunder_cp:
        return "mistake"
    return "blunder"


def build_move_analysis(
    board: chess.Board,           # position BEFORE the move
    move: chess.Move,
    candidates: list[Line],       # top_lines() of the position before the move
    lines_after: list[Line],      # top_lines() of the position AFTER ([] if game over)
    thresholds,                   # AnalysisConfig
) -> MoveAnalysis:
    """Assemble a MoveAnalysis from two already-computed engine searches.

    Key efficiency insight: the position after move n IS the position before
    move n+1, so the pipeline analyses each position exactly once and passes
    both results here. The refutation line also comes free — it's simply the
    opponent's best line in the after-position.

    `lines_after` scores are from the opponent's perspective (they are to move),
    so we negate them to get the mover's point of view.
    """
    eval_before_cp = candidates[0].score_cp if candidates else None
    best_san = candidates[0].move_san if candidates else ""
    played_san = board.san(move)

    if lines_after:
        opp = lines_after[0]
        eval_after_cp = -opp.score_cp if opp.score_cp is not None else None
        mate_after = -opp.mate_in if opp.mate_in is not None else None
    else:
        # No reply exists: game over after this move.
        after = board.copy(stack=False)
        after.push(move)
        if after.is_checkmate():
            eval_after_cp, mate_after = None, 1   # mover delivered mate
        else:
            eval_after_cp, mate_after = 0, None   # stalemate / draw

    # cp loss = best eval - achieved eval (both from mover's POV).
    # Mate scores map to huge cp values so classification still works.
    def as_cp(cp, mate):
        if cp is not None:
            return cp
        return 100_000 if (mate or 0) > 0 else -100_000

    best_cp = as_cp(eval_before_cp, candidates[0].mate_in if candidates else None)
    got_cp = as_cp(eval_after_cp, mate_after)
    cp_loss = max(0, best_cp - got_cp)
    classification = _classify(cp_loss, thresholds)

    refutation_san: list[str] = []
    refutation_text = ""
    if classification in ("inaccuracy", "mistake", "blunder") and lines_after:
        refutation_san = lines_after[0].pv_san
        refutation_text = lines_after[0].pv_text

    return MoveAnalysis(
        fen_before=board.fen(),
        played_san=played_san,
        played_uci=move.uci(),
        candidates=candidates,
        eval_before_cp=eval_before_cp,
        eval_after_cp=eval_after_cp,
        cp_loss=cp_loss,
        classification=classification,
        refutation_san=refutation_san,
        refutation_text=refutation_text,
        best_san=best_san,
    )
