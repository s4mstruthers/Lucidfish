# Copyright (C) 2026 Sam Struthers
# SPDX-License-Identifier: AGPL-3.0-or-later
# This file is part of Lucidfish <https://github.com/s4mstruthers/Lucidfish>; see LICENSE for the terms.

"""The engine as a tool the coach can call while writing.

The evidence in a prompt covers the engine's own lines. The moment the coach
wants to say anything else — "Black could have tried ...Nd4", "the knight
sacrifice fails to ...Qxf2" — it is guessing, and language models guess
confidently. ``check_line`` closes that gap: the model hands over a position
and a sequence of moves, Stockfish plays them out, and the model gets back
whether the moves are legal, what the line captures and leaves behind, the
evaluation at the end in words and as winning chances, and the engine's own
continuation from there. Every line it checked is then also admitted by the
fact-checker, so the note may cite it.

One :class:`LineChecker` lives for one note (or one chat answer). It opens its
own Stockfish process lazily, at a reduced depth and with fewer threads than
the main analysis, and closes it afterwards.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import replace

import chess

from .config import Config
from .engine import EngineAnalyser, Line
from .lines import play_line
from .llm import ToolCall, ToolSpec
from .prompts import side_line
from .scoring import cp_value, describe_eval, win_percent

CHECK_LINE = ToolSpec(
    name="check_line",
    description=(
        "Play a sequence of moves from a position and have the Stockfish engine evaluate the result. Use it "
        "before you state what a variation leads to, to test a tactical idea, or to check a move you want "
        "to recommend that is not among the engine lines you were given. Returns whether every move is "
        "legal, what the line captures and leaves behind (verified), the evaluation at its end in words and "
        "as White's winning chances, and the engine's best continuation from there."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "fen": {"type": ["string", "null"],
                    "description": "The position to start from, as FEN (copy it from the evidence). null "
                                   "means the position before the move under discussion."},
            "moves": {"type": "array", "items": {"type": "string"},
                      "description": "The moves to play from that position, in order, in standard algebraic "
                                     "notation, e.g. [\"Nf3\", \"Nc6\", \"Bb5\"] (UCI such as \"g1f3\" also "
                                     "works)."},
        },
        "required": ["fen", "moves"],
        "additionalProperties": False,
    },
)

DEPTH_DROP = 4        # the tool searches a little shallower than the main analysis
MIN_DEPTH = 10
MAX_CALLS = 4         # per note: enough to test a few ideas, not enough to stall the analysis
MAX_PLIES = 12

_MOVE_NUMBER = re.compile(r"^\d+\.+\s*")


def _white_pov(line: Line, board: chess.Board) -> tuple[int | None, int | None]:
    """Engine lines are scored for the side to move; the evidence speaks from White's view."""
    if board.turn == chess.WHITE:
        return line.score_cp, line.mate_in
    cp = -line.score_cp if line.score_cp is not None else None
    return cp, (-line.mate_in if line.mate_in is not None else None)


class LineChecker:
    """Runs ``check_line`` calls for one note; remembers what it verified for the fact-checkers."""

    def __init__(self, board: chess.Board | None, cfg: Config, cache=None, *, threads: int | None = None,
                 max_calls: int = MAX_CALLS):
        self.board = board
        self.cfg = cfg
        self.cache = cache
        self.max_calls = max_calls
        self._threads = threads
        self._engine: EngineAnalyser | None = None
        self._lock = threading.Lock()
        self.calls: list[dict] = []                               # summaries, shown in the app
        self.lines: list[tuple[chess.Board, list[str]]] = []      # verified lines → EvidenceIndex
        self.evals: list[tuple[str, int | None, int | None]] = []  # (label, cp, mate), White's view

    # ---------------------------------------------------------------- engine

    def engine(self) -> EngineAnalyser:
        with self._lock:
            if self._engine is None:
                e = self.cfg.engine
                threads = self._threads or max(1, e.threads // 2)
                depth = max(MIN_DEPTH, e.depth - DEPTH_DROP)
                movetime = e.movetime_s * 0.5 if e.movetime_s else None
                reduced = replace(e, depth=depth, threads=threads, hash_mb=min(e.hash_mb, 64), multipv=2,
                                  movetime_s=movetime)
                self._engine = EngineAnalyser(reduced, cache=self.cache)
            return self._engine

    def close(self) -> None:
        with self._lock:
            if self._engine is not None:
                self._engine.close()
                self._engine = None

    def __enter__(self) -> LineChecker:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- the tool

    def __call__(self, call: ToolCall) -> str:
        return json.dumps(self.check(call.input), ensure_ascii=False)

    def check(self, args: dict) -> dict:
        if len(self.calls) >= self.max_calls:
            return {"ok": False, "error": "No more engine checks are available for this note; answer with "
                                          "the evidence you already have."}
        fen = args.get("fen") if isinstance(args, dict) else None
        if not fen or not isinstance(fen, str):
            if self.board is None:
                return {"ok": False, "error": "Give the FEN of the position to start from."}
            start = self.board.copy(stack=False)
        else:
            try:
                start = chess.Board(fen.strip())
            except ValueError:
                return {"ok": False, "error": "That FEN is not valid; copy it exactly from the evidence."}
            if not start.is_valid():
                return {"ok": False, "error": "That FEN is not a legal position; copy it exactly from the evidence."}
        raw = args.get("moves") if isinstance(args, dict) else None
        if isinstance(raw, str):
            raw = raw.replace(",", " ").split()
        moves = [_MOVE_NUMBER.sub("", str(m)).strip() for m in (raw or []) if str(m).strip()]
        moves = [m for m in moves if m and not re.fullmatch(r"\d+\.*", m)]
        if not moves:
            return {"ok": False, "error": "Give at least one move to play."}

        outcome = play_line(start, moves, max_plies=MAX_PLIES)
        result: dict = {"ok": True, "line": side_line(start, outcome.sans) or "(no moves played)",
                        "moves_played": len(outcome.sans)}
        if outcome.illegal:
            result["illegal_move"] = outcome.illegal
            legal = [start.san(m) for m in list(start.legal_moves)[:12]] if not outcome.sans else []
            result["note"] = (f"'{outcome.illegal}' is not a legal move at that point"
                              + (f"; legal moves here include {', '.join(legal)}" if legal else "")
                              + (". The facts below cover the moves before it." if outcome.sans else "."))
            if not outcome.sans:
                result["ok"] = False
                self.calls.append({"line": outcome.illegal, "evaluation": "", "legal": False})
                return result
        end = outcome.end
        result["what_happens"] = outcome.clauses()

        engine = self.engine()
        start_best = engine.top_lines(start, multipv=1)
        end_lines = [] if end.is_game_over() else engine.top_lines(end, multipv=2)
        mover = start.turn
        eval_words = ""
        if end.is_game_over():
            if end.is_checkmate():
                winner = not end.turn
                eval_words = f"checkmate — {'White' if winner == chess.WHITE else 'Black'} wins"
                self.evals.append((f"after {result['line']}", None, 1 if winner == chess.WHITE else -1))
            else:
                eval_words = "the game is drawn"
                self.evals.append((f"after {result['line']}", 0, None))
            result["evaluation_at_end"] = eval_words
        elif end_lines:
            cp, mate = _white_pov(end_lines[0], end)
            eval_words = describe_eval(cp, mate)
            result["evaluation_at_end"] = eval_words
            result["white_winning_chances_at_end"] = round(win_percent(cp_value(cp, mate)))
            result["engine_continues"] = side_line(end, end_lines[0].pv_san, 6)
            if len(end_lines) > 1:
                cp2, mate2 = _white_pov(end_lines[1], end)
                result["second_choice_at_end"] = f"{end_lines[1].move_san} ({describe_eval(cp2, mate2)})"
            self.evals.append((f"after {result['line']}", cp, mate))
            if start_best:
                cp0, mate0 = _white_pov(start_best[0], start)
                result["evaluation_with_best_play_at_start"] = describe_eval(cp0, mate0)
                result["engine_best_move_at_start"] = start_best[0].move_san
                self.evals.append(("with best play at the start of the checked line", cp0, mate0))
                sign = 1 if mover == chess.WHITE else -1
                w0 = win_percent(sign * cp_value(cp0, mate0))
                w1 = win_percent(sign * cp_value(cp, mate))
                who = "White" if mover == chess.WHITE else "Black"
                diff = round(w0 - w1)
                if outcome.sans and outcome.sans[0] == start_best[0].move_san:
                    result["first_move"] = f"{outcome.sans[0]} is the engine's best move for {who} here"
                elif diff >= 5:
                    result["verdict"] = (f"compared with the engine's best move at the start, this line ends "
                                         f"{diff} percentage points of winning chances worse for {who} (it "
                                         "plays the replies you gave, which need not be the best ones)")
                else:
                    result["verdict"] = (f"this line holds up: it ends within {max(diff, 0)} percentage "
                                         f"points of the engine's best move for {who}")
        self.lines.append((start, outcome.sans))
        self.calls.append({"line": result["line"], "evaluation": eval_words, "legal": not outcome.illegal})
        return result


__all__ = ["CHECK_LINE", "LineChecker"]
