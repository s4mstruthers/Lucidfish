"""Endgame specialist: the facts that decide endgames, checked on the board.

In the endgame, plans are about the kings and the passed pawns. This module
reports, for positions in the endgame phase:

- how active each king is (distance from the centre),
- each passed pawn: how far it is from promoting, whether it is protected by a
  pawn, and whether a piece blocks it,
- pawn races in pure king-and-pawn endings: whether the defending king is
  inside the "square" of the pawn, i.e. can still catch it,
- the opposition, when the kings face each other with one square between them.

Everything is deterministic and cheap; the facts go to the AI coach as
verified evidence (never guessed by the model).
"""

from __future__ import annotations

import chess

_CENTRE = (chess.D4, chess.E4, chess.D5, chess.E5)


def _name(color: chess.Color) -> str:
    return "White" if color == chess.WHITE else "Black"


def king_activity(board: chess.Board, color: chess.Color) -> tuple[int, str]:
    """(distance to the nearest centre square, description)."""
    king = board.king(color)
    if king is None:
        return 9, ""
    d = min(chess.square_distance(king, c) for c in _CENTRE)
    where = chess.square_name(king)
    if d == 0:
        return d, f"{_name(color)}'s king on {where} is fully centralised"
    if d == 1:
        return d, f"{_name(color)}'s king on {where} is active, next to the centre"
    return d, f"{_name(color)}'s king on {where} is passive, {d} squares from the centre"


def passed_pawns(board: chess.Board, color: chess.Color) -> list[chess.Square]:
    out = []
    enemy = board.pieces(chess.PAWN, not color)
    for sq in board.pieces(chess.PAWN, color):
        f, r = chess.square_file(sq), chess.square_rank(sq)
        ahead = range(r + 1, 8) if color == chess.WHITE else range(r - 1, -1, -1)
        if not any(chess.square(ff, rr) in enemy for ff in (f - 1, f, f + 1) if 0 <= ff < 8 for rr in ahead):
            out.append(sq)
    return out


def _steps_to_queen(sq: chess.Square, color: chess.Color) -> int:
    r = chess.square_rank(sq)
    steps = 7 - r if color == chess.WHITE else r
    start = 1 if color == chess.WHITE else 6
    return steps - 1 if r == start else steps   # the double step from the starting rank


def _pawn_only(board: chess.Board) -> bool:
    return all(not board.pieces(pt, c) for pt in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)
               for c in (chess.WHITE, chess.BLACK))


def _catches(board: chess.Board, pawn: chess.Square, color: chess.Color) -> bool:
    """Rule of the square: can the defending king reach the promotion square in time?"""
    defender = board.king(not color)
    if defender is None:
        return False
    queen_sq = chess.square(chess.square_file(pawn), 7 if color == chess.WHITE else 0)
    pawn_moves = _steps_to_queen(pawn, color)
    king_moves = chess.square_distance(defender, queen_sq)
    # If the pawn's side is to move, the pawn is a tempo ahead and the king needs one move less.
    return king_moves <= pawn_moves - (1 if board.turn == color else 0)


def passed_pawn_lines(board: chess.Board, color: chess.Color) -> list[tuple[str, str]]:
    out = []
    for sq in passed_pawns(board, color):
        name = chess.square_name(sq)
        steps = _steps_to_queen(sq, color)
        bits = [f"{steps} move{'s' if steps != 1 else ''} from promoting"]
        if board.attackers(color, sq) & board.pieces(chess.PAWN, color):
            bits.append("protected by a pawn")
        front = sq + (8 if color == chess.WHITE else -8)
        blocker = board.piece_at(front) if 0 <= front < 64 else None
        if blocker is not None and blocker.color != color:
            bits.append(f"blocked by the {chess.piece_name(blocker.piece_type)} on {chess.square_name(front)}")
        if _pawn_only(board):
            catch = _catches(board, sq, color)
            bits.append(f"{_name(not color)}'s king {'can' if catch else 'cannot'} catch it (rule of the square)")
        out.append((f"eg:passed:{name}", f"{_name(color)}'s passed pawn on {name}: {', '.join(bits)}."))
    return out


def opposition_line(board: chess.Board) -> tuple[str, str] | None:
    """Direct opposition in king-and-pawn endings: kings face each other with one square between."""
    wk, bk = board.king(chess.WHITE), board.king(chess.BLACK)
    if wk is None or bk is None or not _pawn_only(board):
        return None
    same_file = chess.square_file(wk) == chess.square_file(bk)
    same_rank = chess.square_rank(wk) == chess.square_rank(bk)
    if (same_file or same_rank) and chess.square_distance(wk, bk) == 2:
        holder = _name(not board.turn)   # the side that just moved into it (not to move) holds it
        return ("eg:opposition", f"The kings are in direct opposition; {holder} has the opposition "
                                 f"({_name(board.turn)} to move must give way).")
    return None


def endgame_lines(board: chess.Board) -> list[tuple[str, str]]:
    """Keyed endgame facts (keys let callers diff two positions)."""
    out: list[tuple[str, str]] = []
    for color in (chess.WHITE, chess.BLACK):
        _, text = king_activity(board, color)
        if text:
            out.append((f"eg:king:{_name(color)}", text + "."))
    for color in (chess.WHITE, chess.BLACK):
        out += passed_pawn_lines(board, color)
    opp = opposition_line(board)
    if opp:
        out.append(opp)
    return out
