# Copyright (C) 2026 Sam Struthers
# SPDX-License-Identifier: AGPL-3.0-or-later
# This file is part of Lucidfish <https://github.com/s4mstruthers/Lucidfish>; see LICENSE for the terms.

"""What a line actually leads to, as verified facts.

An engine line is a list of moves and a number. The number says *how good* the
line is; nothing in it says *why* — and "why" is what the coach has to write.
Left to itself, a language model fills that gap with plausible chess talk
("...and White's attack crashes through"), which is the main way notes go wrong.

This module plays a line out on the board and reports what can be verified at
the end of it: material won or lost (with the pieces that came off), checkmate
or check, promotion, castling, new passed pawns, and the tactical facts of the
final position (pieces that can be won, forks, pins, mate threats). The coach
gets these facts next to every candidate, refutation and expected continuation,
and the ``check_line`` tool returns them for any line the model wants to test.

Everything here is pure python-chess, deterministic, and cheap enough to run
for every line of every move.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import chess

from . import tactics
from .features import PIECE_VALUES

MAX_PLIES = 8


def colour_name(colour: chess.Color) -> str:
    return "White" if colour == chess.WHITE else "Black"


def material_balance(board: chess.Board) -> int:
    """White's material minus Black's, in points (pawn 1, knight/bishop 3, rook 5, queen 9)."""
    return sum(PIECE_VALUES[pt] * (len(board.pieces(pt, chess.WHITE)) - len(board.pieces(pt, chess.BLACK)))
               for pt in PIECE_VALUES)


def balance_words(balance: int) -> str:
    if balance == 0:
        return "material is level"
    side = "White" if balance > 0 else "Black"
    n = abs(balance)
    return f"{side} is up {n} point{'s' if n != 1 else ''}"


@dataclass
class LineOutcome:
    """Verified facts about a line played from a position."""
    start: chess.Board
    sans: list[str] = field(default_factory=list)         # the moves that could be played (SAN)
    illegal: str = ""                                     # the first move that could not be played
    end: chess.Board | None = None
    captured: dict[bool, list[str]] = field(default_factory=lambda: {chess.WHITE: [], chess.BLACK: []})
    promotions: list[str] = field(default_factory=list)   # "White's pawn promotes to a queen"
    castled: list[str] = field(default_factory=list)      # "White castles kingside"
    checks: int = 0

    @property
    def mover(self) -> chess.Color:
        return self.start.turn

    @property
    def plies(self) -> int:
        return len(self.sans)

    @property
    def material_change(self) -> int:
        """Change in material from the point of view of the side that starts the line."""
        if self.end is None:
            return 0
        delta = material_balance(self.end) - material_balance(self.start)
        return delta if self.mover == chess.WHITE else -delta

    def clauses(self, tactics_at_end: int = 2) -> list[str]:
        """Short verified statements, ready for a prompt."""
        out: list[str] = []
        if self.illegal:
            out.append(f"'{self.illegal}' is not a legal move at that point, so the line stops before it")
        if self.end is None:
            return out
        mover, other = colour_name(self.mover), colour_name(not self.mover)
        end = self.end
        if end.is_checkmate():
            out.append(f"the line ends in CHECKMATE: {colour_name(not end.turn)} wins")
            return out + self._material_clause(mover, other)
        if end.is_stalemate():
            out.append("the line ends in stalemate (a draw)")
        elif end.is_insufficient_material():
            out.append("the line ends with too little material to mate (a draw)")
        out += self._material_clause(mover, other)
        out += self.promotions + self.castled
        if end.is_check():
            out.append(f"it ends with {colour_name(end.turn)}'s king in check")
        passed = self._new_passed_pawns()
        if passed:
            out.append("new passed pawn" + ("s" if len(passed) > 1 else "") + ": " + ", ".join(passed))
        if tactics_at_end:
            facts = [text.rstrip(".") for _, text in tactics.position_lines(end)[:tactics_at_end]]
            if facts:
                out.append(f"at the end ({colour_name(end.turn)} to move): " + "; ".join(facts))
        return out

    def _material_clause(self, mover: str, other: str) -> list[str]:
        change = self.material_change
        pieces_lost = {side: ", ".join(names) for side, names in self.captured.items() if names}
        if not change and not pieces_lost:
            return []
        traded = []
        if pieces_lost.get(self.mover):
            traded.append(f"{mover} gives up {pieces_lost[self.mover]}")
        if pieces_lost.get(not self.mover):
            traded.append(f"{other} loses {pieces_lost[not self.mover]}")
        balance = material_balance(self.end)
        if change > 0:
            verdict = f"{mover} wins {change} point{'s' if change != 1 else ''} of material"
        elif change < 0:
            verdict = f"{mover} is down {-change} point{'s' if change != -1 else ''} of material by the end"
        else:
            verdict = "an even trade"
        text = verdict + (f" ({'; '.join(traded)})" if traded else "")
        if change or balance:
            text += f"; at the end {balance_words(balance)}"
        return [text]

    def _new_passed_pawns(self) -> list[str]:
        out = []
        for colour in (chess.WHITE, chess.BLACK):
            before = _passed(self.start, colour)
            for sq in _passed(self.end, colour) - before:
                out.append(f"{colour_name(colour)}'s pawn on {chess.square_name(sq)}")
        return out


def _passed(board: chess.Board, colour: chess.Color) -> set[chess.Square]:
    out = set()
    their = board.pieces(chess.PAWN, not colour)
    direction = 1 if colour == chess.WHITE else -1
    for sq in board.pieces(chess.PAWN, colour):
        f, r = chess.square_file(sq), chess.square_rank(sq)
        if not any(abs(chess.square_file(p) - f) <= 1 and (chess.square_rank(p) - r) * direction > 0 for p in their):
            out.add(sq)
    return out


def play_line(board: chess.Board, moves: list[str], max_plies: int = MAX_PLIES) -> LineOutcome:
    """Play `moves` (SAN or UCI) from `board`; stops at the first illegal move."""
    outcome = LineOutcome(start=board.copy(stack=False))
    b = board.copy(stack=False)
    for raw in moves[:max_plies]:
        token = str(raw).strip().rstrip(".").split(" ")[-1]   # tolerate "12.", "12...", "12. Nf3"
        token = token.replace("0-0-0", "O-O-O").replace("0-0", "O-O")
        if not token:
            continue
        try:
            mv = b.parse_san(token)
        except ValueError:
            try:
                mv = chess.Move.from_uci(token)
            except ValueError:
                mv = None
            if mv is None or mv not in b.legal_moves:
                outcome.illegal = token
                break
        san = b.san(mv)
        who = colour_name(b.turn)
        if b.is_castling(mv):
            side = "kingside" if chess.square_file(mv.to_square) > chess.square_file(mv.from_square) else "queenside"
            outcome.castled.append(f"{who} castles {side}")
        if b.is_en_passant(mv):
            outcome.captured[not b.turn].append("a pawn")
        elif b.is_capture(mv):
            victim = b.piece_at(mv.to_square)
            outcome.captured[not b.turn].append(
                f"the {chess.piece_name(victim.piece_type)} on {chess.square_name(mv.to_square)}")
        if mv.promotion:
            outcome.promotions.append(f"{who}'s pawn promotes to a {chess.piece_name(mv.promotion)}")
        b.push(mv)
        outcome.sans.append(san)
        if b.is_check():
            outcome.checks += 1
    outcome.end = b
    return outcome


def line_consequences(board: chess.Board, sans: list[str], max_plies: int = MAX_PLIES) -> str:
    """One prompt-ready clause list for a line ('' when nothing verifiable happens)."""
    return "; ".join(play_line(board, sans, max_plies).clauses())


__all__ = ["LineOutcome", "balance_words", "line_consequences", "material_balance", "play_line"]
