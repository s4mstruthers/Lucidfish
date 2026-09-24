"""Feature layer: turn a raw position into named chess concepts.

This is what separates "eval dropped 1.5" from "you traded your active bishop
while your king was still uncastled". Every feature is a concrete, verifiable
fact computed with python-chess — the LLM narrates these, it never invents them.

Features are computed for BOTH colours so the prompt can talk about
differences (development lead, safer king, better structure), and each fact
carries a stable key so two positions can be diffed ("what did this move
change?") instead of dumping both in full.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import chess

from . import endgame, tactics

PIECE_VALUES = {
    chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
    chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0,
}

CENTER = [chess.D4, chess.E4, chess.D5, chess.E5]

_HOME_MINORS = {
    chess.WHITE: {chess.B1, chess.G1, chess.C1, chess.F1},
    chess.BLACK: {chess.B8, chess.G8, chess.C8, chess.F8},
}


@dataclass
class SideFeatures:
    material: int = 0
    minors: int = 0                    # knights + bishops on the board
    developed_minors: int = 0          # of those, how many have left their home squares
    undeveloped_minors: list[str] = field(default_factory=list)
    castled: bool = False
    can_castle: bool = False
    king_square: str = ""
    king_pawn_shield: int = 0          # files next to the king with an own pawn close in front
    king_open_files: list[str] = field(default_factory=list)  # open/semi-open files at the king
    hanging_pieces: list[str] = field(default_factory=list)   # can be won by capture (SEE > 0)
    doubled_pawn_files: list[str] = field(default_factory=list)
    isolated_pawns: list[str] = field(default_factory=list)
    passed_pawns: list[str] = field(default_factory=list)
    center_pawns_and_attacks: int = 0  # occupancy + attacks on d4/e4/d5/e5
    mobility: int = 0                  # legal-move count (proxy for activity)


@dataclass
class PositionFeatures:
    white: SideFeatures
    black: SideFeatures
    phase: str                         # opening / middlegame / endgame
    tactical: list[tuple[str, str]] = field(default_factory=list)  # (key, text) from tactics
    endgame: list[tuple[str, str]] = field(default_factory=list)   # (key, text) from endgame.py

    def keyed_lines(self) -> list[tuple[str, str]]:
        """Every fact as (stable key, sentence). Keys allow diffing two positions."""
        out: list[tuple[str, str]] = [("phase", f"Game phase: {self.phase}.")]
        w, b = self.white, self.black
        if w.material != b.material:
            lead = "White" if w.material > b.material else "Black"
            n = abs(w.material - b.material)
            out.append(("material", f"Material: {lead} is up {n} point{'s' if n != 1 else ''}."))
        else:
            out.append(("material", "Material: equal."))
        if self.phase != "endgame":
            out.append(("development",
                        f"Development: White has {w.developed_minors} of {w.minors} minor pieces "
                        f"developed, Black {b.developed_minors} of {b.minors}."))
        for name, s in (("White", w), ("Black", b)):
            if s.castled:
                king = f"{name} has castled (king on {s.king_square})"
            elif s.can_castle:
                king = f"{name} has not castled yet (king on {s.king_square}, castling still possible)"
            else:
                king = f"{name} can no longer castle (king on {s.king_square})"
            if self.phase != "endgame":
                king += f"; {s.king_pawn_shield}/3 shield pawns in front of the king"
            out.append((f"king:{name}", king + "."))
            if s.king_open_files and self.phase != "endgame":
                out.append((f"kingfiles:{name}",
                            f"Open/semi-open files next to {name}'s king: {', '.join(s.king_open_files)}."))
            if s.doubled_pawn_files:
                files = ", ".join(s.doubled_pawn_files)
                out.append((f"doubled:{name}", f"{name} has doubled pawns on the {files}-file(s)."))
            if s.isolated_pawns:
                out.append((f"isolated:{name}", f"{name} has isolated pawns on {', '.join(s.isolated_pawns)}."))
            if s.passed_pawns:
                out.append((f"passed:{name}", f"{name} has passed pawns on {', '.join(s.passed_pawns)}."))
        out.extend(self.tactical)
        out.extend(self.endgame)
        out.append(("center", f"Center control (pawns + attacks on d4/e4/d5/e5): "
                              f"White {w.center_pawns_and_attacks}, Black {b.center_pawns_and_attacks}."))
        out.append(("mobility", f"Mobility (legal moves): White {w.mobility}, Black {b.mobility}."))
        return out

    def summary_lines(self) -> list[str]:
        """Human-readable facts, ready to drop into an LLM prompt."""
        return [text for _, text in self.keyed_lines()]


def diff_lines(before: PositionFeatures, after: PositionFeatures) -> list[str]:
    """What changed between two positions, as prompt-ready sentences.

    New or changed facts are stated as they now are; facts that disappeared are
    reported as no longer true (e.g. a hanging piece that was rescued).
    """
    old = dict(before.keyed_lines())
    new = dict(after.keyed_lines())
    out = [text for key, text in new.items() if old.get(key) != text and key != "mobility"]
    out += [f"No longer the case: {text[0].lower() + text[1:]}" for key, text in old.items()
            if key not in new and not key.startswith(("development", "kingfiles"))]
    return out


# ------------------------------------------------------------------ internals

def _material(board: chess.Board, color: chess.Color) -> int:
    return sum(PIECE_VALUES[pt] * len(board.pieces(pt, color)) for pt in PIECE_VALUES)


def _development(board: chess.Board, color: chess.Color) -> tuple[int, int, list[str]]:
    """(minors on board, minors developed, labels of minors still at home).

    Counting actual pieces (rather than empty home squares) means captured
    pieces are never reported as "developed".
    """
    minors = board.pieces(chess.KNIGHT, color) | board.pieces(chess.BISHOP, color)
    home = sorted(f"{board.piece_at(sq).symbol().upper()}{chess.square_name(sq)}"
                  for sq in minors if sq in _HOME_MINORS[color])
    return len(minors), len(minors) - len(home), home


def _castled(board: chess.Board, color: chess.Color) -> bool:
    """Heuristic: king on a castled square with no castling rights left."""
    king = board.king(color)
    castled_squares = {chess.G1, chess.C1} if color == chess.WHITE else {chess.G8, chess.C8}
    return king in castled_squares and not board.has_castling_rights(color)


def _king_safety(board: chess.Board, color: chess.Color) -> tuple[int, list[str]]:
    king = board.king(color)
    if king is None:
        return 0, []
    kf, kr = chess.square_file(king), chess.square_rank(king)
    forward = 1 if color == chess.WHITE else -1
    own_pawns = board.pieces(chess.PAWN, color)
    own_pawn_files = {chess.square_file(sq) for sq in own_pawns}
    shield, open_files = 0, []
    for f in (kf - 1, kf, kf + 1):
        if not 0 <= f <= 7:
            continue
        # shield pawn: own pawn one or two ranks in front of the king on this file
        shield += any(chess.square(f, r) in own_pawns
                      for r in (kr + forward, kr + 2 * forward) if 0 <= r <= 7)
        if f not in own_pawn_files:
            open_files.append(chess.FILE_NAMES[f])
    return shield, open_files


def _pawn_structure(board: chess.Board, color: chess.Color) -> tuple[list[str], list[str], list[str]]:
    pawns = board.pieces(chess.PAWN, color)
    their_pawns = board.pieces(chess.PAWN, not color)
    files = [chess.square_file(sq) for sq in pawns]
    doubled = sorted({chess.FILE_NAMES[f] for f in files if files.count(f) > 1})
    isolated, passed = [], []
    for sq in pawns:
        f, r = chess.square_file(sq), chess.square_rank(sq)
        if not any(abs(chess.square_file(p) - f) == 1 for p in pawns):
            isolated.append(chess.square_name(sq))
        direction = 1 if color == chess.WHITE else -1
        if not any(abs(chess.square_file(p) - f) <= 1 and (chess.square_rank(p) - r) * direction > 0
                   for p in their_pawns):
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
    if board.is_check():
        return 0  # flipping the turn would leave a king in check; not meaningful
    b = board.copy(stack=False)
    b.turn = color
    b.ep_square = None
    return b.legal_moves.count()


def game_phase(board: chess.Board) -> str:
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
    pt = tactics.analyse(board)
    sides = {}
    for color in (chess.WHITE, chess.BLACK):
        minors, developed, home = _development(board, color)
        shield, open_files = _king_safety(board, color)
        doubled, isolated, passed = _pawn_structure(board, color)
        king = board.king(color)
        sides[color] = SideFeatures(
            material=_material(board, color),
            minors=minors,
            developed_minors=developed,
            undeveloped_minors=home,
            castled=_castled(board, color),
            can_castle=board.has_castling_rights(color),
            king_square=chess.square_name(king) if king is not None else "?",
            king_pawn_shield=shield,
            king_open_files=open_files,
            hanging_pieces=[f"{board.piece_at(t.square).symbol().upper()}{chess.square_name(t.square)}"
                            for t in pt.en_prise[color]],
            doubled_pawn_files=doubled,
            isolated_pawns=isolated,
            passed_pawns=passed,
            center_pawns_and_attacks=_center(board, color),
            mobility=_mobility(board, color),
        )
    phase = game_phase(board)
    return PositionFeatures(white=sides[chess.WHITE], black=sides[chess.BLACK],
                            phase=phase, tactical=tactics.position_lines(board),
                            endgame=endgame.endgame_lines(board) if phase == "endgame" else [])


def piece_placement(board: chess.Board) -> str:
    """'White: Kg1, Qd1, Ra1, ..., pawns a2 b2 ...; Black: ...'.

    LLMs read piece lists far more reliably than FEN strings, which cuts down
    on invented piece locations.
    """
    parts = []
    order = (chess.KING, chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT)
    for color in (chess.WHITE, chess.BLACK):
        pieces = [f"{chess.piece_symbol(pt).upper()}{chess.square_name(sq)}"
                  for pt in order for sq in board.pieces(pt, color)]
        pawns = " ".join(chess.square_name(sq) for sq in board.pieces(chess.PAWN, color))
        side = ", ".join(pieces) + (f", pawns {pawns}" if pawns else "")
        parts.append(f"{'White' if color == chess.WHITE else 'Black'}: {side}")
    return "; ".join(parts)
