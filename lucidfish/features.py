"""Feature layer: turn a raw position into named chess concepts.

This is what separates "eval dropped 1.5" from "you traded your active bishop
while your king was still uncastled". Every feature is a concrete, verifiable
fact computed with python-chess — the LLM narrates these, it never invents them.

All features are computed for BOTH colours so the prompt can talk about
differences (development lead, safer king, better structure).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import chess

PIECE_VALUES = {
    chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
    chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0,
}

CENTER = [chess.D4, chess.E4, chess.D5, chess.E5]


@dataclass
class SideFeatures:
    material: int = 0
    developed_minors: int = 0          # knights/bishops off their home squares
    undeveloped_minors: list[str] = field(default_factory=list)
    castled: bool = False
    can_castle: bool = False
    king_square: str = ""
    king_pawn_shield: int = 0          # pawns on the 3 files around the king, near it
    king_open_files: list[str] = field(default_factory=list)  # open/semi-open files at the king
    hanging_pieces: list[str] = field(default_factory=list)   # attacked, not defended
    doubled_pawn_files: list[str] = field(default_factory=list)
    isolated_pawns: list[str] = field(default_factory=list)
    passed_pawns: list[str] = field(default_factory=list)
    center_pawns_and_attacks: int = 0  # occupancy + attacks on d4/e4/d5/e5
    mobility: int = 0                  # legal-move count (proxy for activity)


@dataclass
class PositionFeatures:
    white: SideFeatures
    black: SideFeatures
    phase: str  # opening / middlegame / endgame

    def summary_lines(self) -> list[str]:
        """Human-readable diffs, ready to drop into an LLM prompt."""
        out = []
        w, b = self.white, self.black
        out.append(f"Game phase: {self.phase}.")
        if w.material != b.material:
            lead = "White" if w.material > b.material else "Black"
            out.append(f"Material: {lead} is up {abs(w.material - b.material)} point(s).")
        else:
            out.append("Material: equal.")
        out.append(
            f"Development: White {w.developed_minors}/4 minors out, Black {b.developed_minors}/4."
        )
        for name, s in (("White", w), ("Black", b)):
            if s.castled:
                out.append(f"{name} has castled (king on {s.king_square}).")
            elif s.can_castle:
                out.append(f"{name} has not castled yet (king on {s.king_square}, rights intact).")
            else:
                out.append(f"{name} can no longer castle (king on {s.king_square}).")
            if s.hanging_pieces:
                out.append(f"{name} has hanging (attacked, undefended) material: {', '.join(s.hanging_pieces)}.")
            if s.king_open_files:
                out.append(f"Files near {name}'s king that are open/semi-open: {', '.join(s.king_open_files)}.")
            if s.doubled_pawn_files:
                out.append(f"{name} has doubled pawns on: {', '.join(s.doubled_pawn_files)}.")
            if s.isolated_pawns:
                out.append(f"{name} has isolated pawns: {', '.join(s.isolated_pawns)}.")
            if s.passed_pawns:
                out.append(f"{name} has passed pawns: {', '.join(s.passed_pawns)}.")
        out.append(
            f"Center presence (pawns+attacks on d4/e4/d5/e5): White {w.center_pawns_and_attacks}, "
            f"Black {b.center_pawns_and_attacks}."
        )
        out.append(f"Mobility (legal moves): White {w.mobility}, Black {b.mobility}.")
        return out


# ------------------------------------------------------------------ internals

_HOME_MINORS = {
    chess.WHITE: [chess.B1, chess.G1, chess.C1, chess.F1],
    chess.BLACK: [chess.B8, chess.G8, chess.C8, chess.F8],
}


def _material(board: chess.Board, color: chess.Color) -> int:
    return sum(
        PIECE_VALUES[pt] * len(board.pieces(pt, color))
        for pt in PIECE_VALUES
    )


def _development(board: chess.Board, color: chess.Color) -> tuple[int, list[str]]:
    developed, home = 0, []
    for sq in _HOME_MINORS[color]:
        piece = board.piece_at(sq)
        if piece and piece.color == color and piece.piece_type in (chess.KNIGHT, chess.BISHOP):
            home.append(f"{piece.symbol().upper()}{chess.square_name(sq)}")
        else:
            developed += 1
    return developed, home


def _castled(board: chess.Board, color: chess.Color) -> bool:
    """Heuristic: king off e-file on g/c file's typical castled squares and no rights left."""
    king = board.king(color)
    if king is None:
        return False
    castled_squares = (
        {chess.G1, chess.C1} if color == chess.WHITE else {chess.G8, chess.C8}
    )
    return king in castled_squares and not board.has_castling_rights(color)


def _king_safety(board: chess.Board, color: chess.Color) -> tuple[int, list[str]]:
    king = board.king(color)
    if king is None:
        return 0, []
    kf, kr = chess.square_file(king), chess.square_rank(king)
    shield = 0
    open_files = []
    forward = 1 if color == chess.WHITE else -1
    for f in (kf - 1, kf, kf + 1):
        if not 0 <= f <= 7:
            continue
        # pawn shield: own pawn within two ranks in front of the king on this file
        has_own_pawn_near = any(
            board.piece_at(chess.square(f, r)) == chess.Piece(chess.PAWN, color)
            for r in (kr + forward, kr + 2 * forward)
            if 0 <= r <= 7
        )
        shield += has_own_pawn_near
        # open/semi-open: no own pawn anywhere on the file
        own_pawn_on_file = any(
            chess.square_file(sq) == f for sq in board.pieces(chess.PAWN, color)
        )
        if not own_pawn_on_file:
            open_files.append(chess.FILE_NAMES[f])
    return shield, open_files


def _hanging(board: chess.Board, color: chess.Color) -> list[str]:
    """Pieces of `color` attacked by the opponent and defended by nobody."""
    out = []
    for sq, piece in board.piece_map().items():
        if piece.color != color or piece.piece_type == chess.KING:
            continue
        if board.is_attacked_by(not color, sq) and not board.is_attacked_by(color, sq):
            out.append(f"{piece.symbol().upper()}{chess.square_name(sq)}")
    return out


def _pawn_structure(board: chess.Board, color: chess.Color) -> tuple[list[str], list[str], list[str]]:
    pawns = board.pieces(chess.PAWN, color)
    their_pawns = board.pieces(chess.PAWN, not color)
    files = [chess.square_file(sq) for sq in pawns]

    doubled = sorted({chess.FILE_NAMES[f] for f in files if files.count(f) > 1})

    isolated = []
    for sq in pawns:
        f = chess.square_file(sq)
        if not any(abs(chess.square_file(p) - f) == 1 for p in pawns):
            isolated.append(chess.square_name(sq))

    passed = []
    for sq in pawns:
        f, r = chess.square_file(sq), chess.square_rank(sq)
        blockers = [
            p for p in their_pawns
            if abs(chess.square_file(p) - f) <= 1
            and ((chess.square_rank(p) > r) if color == chess.WHITE else (chess.square_rank(p) < r))
        ]
        if not blockers:
            passed.append(chess.square_name(sq))

    return doubled, sorted(isolated), sorted(passed)


def _center(board: chess.Board, color: chess.Color) -> int:
    score = 0
    for sq in CENTER:
        piece = board.piece_at(sq)
        if piece and piece.color == color and piece.piece_type == chess.PAWN:
            score += 2
        score += len(board.attackers(color, sq))
    return score


def _mobility(board: chess.Board, color: chess.Color) -> int:
    if board.turn == color:
        return board.legal_moves.count()
    b = board.copy(stack=False)
    b.turn = color
    # If flipping the turn leaves the mover's king "in check" illegally, count 0 rather than crash.
    try:
        return b.legal_moves.count()
    except Exception:
        return 0


def _phase(board: chess.Board) -> str:
    minors_majors = sum(
        len(board.pieces(pt, c))
        for pt in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)
        for c in (chess.WHITE, chess.BLACK)
    )
    if board.fullmove_number <= 10 and minors_majors >= 12:
        return "opening"
    if minors_majors <= 6:
        return "endgame"
    return "middlegame"


# --------------------------------------------------------------------- public

def extract(board: chess.Board) -> PositionFeatures:
    sides = {}
    for color in (chess.WHITE, chess.BLACK):
        dev, home = _development(board, color)
        shield, open_files = _king_safety(board, color)
        doubled, isolated, passed = _pawn_structure(board, color)
        king = board.king(color)
        sides[color] = SideFeatures(
            material=_material(board, color),
            developed_minors=dev,
            undeveloped_minors=home,
            castled=_castled(board, color),
            can_castle=board.has_castling_rights(color),
            king_square=chess.square_name(king) if king is not None else "?",
            king_pawn_shield=shield,
            king_open_files=open_files,
            hanging_pieces=_hanging(board, color),
            doubled_pawn_files=doubled,
            isolated_pawns=isolated,
            passed_pawns=passed,
            center_pawns_and_attacks=_center(board, color),
            mobility=_mobility(board, color),
        )
    return PositionFeatures(white=sides[chess.WHITE], black=sides[chess.BLACK], phase=_phase(board))
