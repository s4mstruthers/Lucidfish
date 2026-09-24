"""Plan tracker: what each side has been trying to do, as verified facts.

A commentator explains moves as parts of plans ("Black keeps pushing on the
queenside to open the a-file"). To ground that kind of commentary, this module
summarises, from the moves actually played:

- where each side has been active recently (queenside / centre / kingside),
- which pawns have advanced on which wing (expansion, pawn storms),
- open and half-open files and who has heavy pieces on them,
- pawn breaks each side has available (a pawn push that attacks an enemy pawn),
- a one-line gist for moves shown as history or "what happened next".

Everything is deterministic and cheap (well under a millisecond per position),
so it is computed for every move without slowing the analysis down.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import chess

WINGS = {"queenside": (0, 1, 2), "centre": (3, 4), "kingside": (5, 6, 7)}


def wing(square: chess.Square) -> str:
    f = chess.square_file(square)
    return next(name for name, files in WINGS.items() if f in files)


def color_name(color: chess.Color) -> str:
    return "White" if color == chess.WHITE else "Black"


@dataclass(frozen=True)
class PlayedMove:
    """One move of the game as seen by the tracker."""
    ply: int
    color: chess.Color
    san: str
    move: chess.Move
    board: chess.Board        # position before the move

    @property
    def label(self) -> str:
        n = self.board.fullmove_number
        return f"{n}. {self.san}" if self.color == chess.WHITE else f"{n}... {self.san}"


def labelled(moves: list[PlayedMove]) -> str:
    """'11. White axb4, 11... Black Qc7' — every move carries its side's name."""
    out = []
    for m in moves:
        n = m.board.fullmove_number
        out.append(f"{n}. White {m.san}" if m.color == chess.WHITE else f"{n}... Black {m.san}")
    return ", ".join(out)


def gist(board: chess.Board, move: chess.Move) -> str:
    """Short verified description of the most important thing a move does."""
    piece = board.piece_at(move.from_square)
    after = board.copy(stack=False)
    after.push(move)
    if after.is_checkmate():
        return "checkmate"
    if board.is_castling(move):
        text = "castles " + ("kingside" if chess.square_file(move.to_square) > chess.square_file(move.from_square)
                             else "queenside")
    elif board.is_capture(move):
        victim = board.piece_at(move.to_square)
        name = "pawn" if board.is_en_passant(move) else chess.piece_name(victim.piece_type)
        text = f"captures the {name} on {chess.square_name(move.to_square)}"
    elif piece.piece_type == chess.PAWN:
        hits = [sq for sq in after.attacks(move.to_square)
                if after.piece_at(sq) and after.piece_at(sq).color != piece.color]
        pawn_hits = [sq for sq in hits if after.piece_type_at(sq) == chess.PAWN]
        if pawn_hits:
            text = f"pawn break, attacks the pawn on {chess.square_name(pawn_hits[0])}"
        elif hits:
            text = f"pawn attacks the {chess.piece_name(after.piece_type_at(hits[0]))} on {chess.square_name(hits[0])}"
        else:
            text = f"{wing(move.to_square)} pawn advance"
    else:
        text = f"{chess.piece_name(piece.piece_type)} to the {wing(move.to_square)}"
    if after.is_check():
        text += ", check"
    return text


def with_gists(moves: list[PlayedMove]) -> str:
    """Labelled moves each followed by their gist in brackets."""
    return ", ".join(f"{m.label.split(' ')[0]} {color_name(m.color)} {m.san} [{gist(m.board, m.move)}]"
                     for m in moves)


# ------------------------------------------------------------ position scan

def _files(board: chess.Board) -> list[str]:
    """Open / half-open files and the heavy pieces standing on them."""
    out = []
    for f in range(8):
        white_p = any(chess.square_file(sq) == f for sq in board.pieces(chess.PAWN, chess.WHITE))
        black_p = any(chess.square_file(sq) == f for sq in board.pieces(chess.PAWN, chess.BLACK))
        if white_p and black_p:
            continue
        heavy = []
        for color in (chess.WHITE, chess.BLACK):
            for pt in (chess.ROOK, chess.QUEEN):
                for sq in board.pieces(pt, color):
                    if chess.square_file(sq) == f:
                        heavy.append(f"{color_name(color)}'s {chess.piece_name(pt)} on {chess.square_name(sq)}")
        name = chess.FILE_NAMES[f]
        if not white_p and not black_p:
            kind = f"the {name}-file is open"
        else:
            kind = f"the {name}-file is half-open for {'White' if not white_p else 'Black'}"
        out.append(kind + (f" ({', '.join(heavy)} on it)" if heavy else ""))
    return out


def pawn_breaks(board: chess.Board, color: chess.Color) -> list[str]:
    """Pawn pushes available to `color` that would attack an enemy pawn."""
    out = []
    step = 8 if color == chess.WHITE else -8
    start_rank = 1 if color == chess.WHITE else 6
    for sq in board.pieces(chess.PAWN, color):
        targets = []
        one = sq + step
        if 0 <= one < 64 and board.piece_at(one) is None:
            targets.append(one)
            two = one + step
            if chess.square_rank(sq) == start_rank and board.piece_at(two) is None:
                targets.append(two)
        for t in targets:
            f, r = chess.square_file(t), chess.square_rank(t)
            ahead = r + (1 if color == chess.WHITE else -1)
            if not 0 <= ahead <= 7:
                continue
            hits = [chess.square(ff, ahead) for ff in (f - 1, f + 1) if 0 <= ff <= 7
                    and board.piece_at(chess.square(ff, ahead)) == chess.Piece(chess.PAWN, not color)]
            if hits:
                mark = "" if color == chess.WHITE else "..."
                out.append(f"{mark}{chess.square_name(t)} (attacks the pawn on {chess.square_name(hits[0])})")
    return out


def plan_lines(history: list[PlayedMove], board: chess.Board, window: int = 6) -> list[str]:
    """Verified plan facts for both sides, given the moves so far and the current position."""
    out = []
    for color in (chess.WHITE, chess.BLACK):
        own = [m for m in history if m.color == color][-window:]
        name = color_name(color)
        if own:
            sectors = Counter("kingside" if m.board.is_castling(m.move) and chess.square_file(m.move.to_square) > 4
                              else "queenside" if m.board.is_castling(m.move)
                              else wing(m.move.to_square) for m in own)
            top, count = sectors.most_common(1)[0]
            recent = ", ".join(m.san for m in own)
            if count >= max(3, len(own) // 2 + 1):
                out.append(f"{name}'s last {len(own)} moves ({recent}) were mostly on the {top} "
                           f"({count} of {len(own)}).")
            else:
                out.append(f"{name}'s last {len(own)} moves: {recent} (spread across the board).")
            pawn_moves = [m for m in own if m.board.piece_type_at(m.move.from_square) == chess.PAWN]
            by_wing = Counter(wing(m.move.to_square) for m in pawn_moves)
            for w, n in by_wing.items():
                if n >= 2 and w != "centre":
                    sans = ", ".join(m.san for m in pawn_moves if wing(m.move.to_square) == w)
                    out.append(f"{name} has advanced {w} pawns recently ({sans}).")
        breaks = pawn_breaks(board, color)
        if breaks:
            out.append(f"Pawn breaks available to {name}: {'; '.join(breaks[:4])}.")
    white_k, black_k = board.king(chess.WHITE), board.king(chess.BLACK)
    if white_k is not None and black_k is not None:
        wk, bk = chess.square_file(white_k), chess.square_file(black_k)
        if (wk >= 5 and bk <= 2) or (wk <= 2 and bk >= 5):
            out.append("The kings are on opposite wings, so pawn storms towards the enemy king are typical.")
    files = _files(board)
    if files:
        out.append("Files: " + "; ".join(files) + ".")
    return out
