"""Tactical motif detection: verified facts about threats on the board.

Small language models are poor at reading tactics out of engine lines, and
they will happily invent a "fork" that does not exist. This module finds the
common motifs directly from the position so the prompt can state them as
facts:

- material that can be won by capture (static exchange evaluation, not just
  "attacked and undefended"),
- absolute and relative pins, skewers,
- forks / double attacks,
- one-move checkmate threats,

plus a per-move summary of what a move does tactically (wins material, hangs
a piece, creates a fork, stops a mate threat, ...).

Everything is pure python-chess and deterministic. Results are memoised per
position because the pipeline asks about the same position several times.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import chess

VALUE = {
    chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 300,
    chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 20_000,
}
_DIAGONALS = ((1, 1), (1, -1), (-1, 1), (-1, -1))
_ORTHOGONALS = ((1, 0), (-1, 0), (0, 1), (0, -1))
_MATERIAL_EPS = 50  # ignore exchanges worth less than half a pawn


def color_name(color: chess.Color) -> str:
    return "White" if color == chess.WHITE else "Black"


def piece_label(board: chess.Board, square: chess.Square, with_color: bool = True) -> str:
    """'White's knight on f3' — the phrasing used throughout the prompts."""
    piece = board.piece_at(square)
    if piece is None:
        return chess.square_name(square)
    name = f"{chess.piece_name(piece.piece_type)} on {chess.square_name(square)}"
    return f"{color_name(piece.color)}'s {name}" if with_color else name


def _points(centipawns: int) -> str:
    pts = max(1, round(centipawns / 100))
    return f"{pts} point" + ("s" if pts != 1 else "")


# ------------------------------------------------------ static exchange eval

def _capturers(board: chess.Board, square: chess.Square, color: chess.Color) -> list[tuple[int, chess.Square]]:
    """Pieces of `color` that can legally capture on `square` (absolute pins respected)."""
    out = []
    for sq in board.attackers(color, square):
        if board.is_pinned(color, sq) and square not in board.pin(color, sq):
            continue
        out.append((VALUE[board.piece_type_at(sq)], sq))
    return out


def _swap(board: chess.Board, square: chess.Square, color: chess.Color, depth: int = 0) -> int:
    """Best material `color` can win by starting a capture sequence on `square`.

    Classic swap algorithm: capture with the least valuable piece, let the
    opponent do the same, and allow either side to stop when continuing loses.
    Never negative, because declining to capture is always an option.
    """
    target = board.piece_type_at(square)
    if target is None or depth > 12:
        return 0
    capturers = _capturers(board, square, color)
    if not capturers:
        return 0
    _, frm = min(capturers)
    piece = board.piece_at(frm)
    nxt = board.copy(stack=False)
    nxt.remove_piece_at(frm)
    nxt.set_piece_at(square, piece)
    if piece.piece_type == chess.KING and nxt.is_attacked_by(not color, square):
        return 0  # the king may not capture into a defended square
    return max(0, VALUE[target] - _swap(nxt, square, not color, depth + 1))


def exchange_value(board: chess.Board, move: chess.Move) -> int:
    """Net material (centipawns) the mover gains by playing `move`, after the
    opponent's best recapture sequence on the destination square. Negative
    means the move loses material."""
    captured = VALUE[chess.PAWN] if board.is_en_passant(move) else (
        VALUE[board.piece_type_at(move.to_square)] if board.is_capture(move) else 0)
    after = board.copy(stack=False)
    after.push(move)
    return captured - _swap(after, move.to_square, after.turn)


# ----------------------------------------------------------- position scan

@dataclass(frozen=True)
class Threat:
    square: chess.Square
    gain: int                    # material the opponent wins by capturing (centipawns)
    text: str


@dataclass
class PositionTactics:
    en_prise: dict[bool, list[Threat]] = field(default_factory=lambda: {True: [], False: []})
    lines: list[tuple[str, bool, str]] = field(default_factory=list)   # (key, attacker color, text)
    forks: list[tuple[str, bool, str]] = field(default_factory=list)
    mate_threat: dict[bool, str] = field(default_factory=dict)          # color -> mating SAN

    def en_prise_squares(self, color: chess.Color) -> set[chess.Square]:
        return {t.square for t in self.en_prise[color]}


def _en_prise(board: chess.Board, color: chess.Color) -> list[Threat]:
    """Pieces of `color` the opponent can win material from by capturing."""
    out = []
    for sq in chess.SquareSet(board.occupied_co[color]):
        piece = board.piece_at(sq)
        if piece.piece_type == chess.KING or not board.is_attacked_by(not color, sq):
            continue
        gain = _swap(board, sq, not color)
        if gain > _MATERIAL_EPS:
            out.append(Threat(sq, gain, f"{piece_label(board, sq)} can be won by capture "
                                        f"(the capturer gains about {_points(gain)})"))
    return out


def _is_defended(board: chess.Board, square: chess.Square) -> bool:
    piece = board.piece_at(square)
    return piece is not None and board.is_attacked_by(piece.color, square)


def _line_tactics(board: chess.Board, color: chess.Color) -> list[tuple[str, bool, str]]:
    """Pins and skewers created by `color`'s bishops, rooks and queens."""
    out = []
    for sq in chess.SquareSet(board.occupied_co[color]):
        slider = board.piece_type_at(sq)
        if slider not in (chess.BISHOP, chess.ROOK, chess.QUEEN):
            continue
        dirs = (_DIAGONALS if slider == chess.BISHOP else _ORTHOGONALS if slider == chess.ROOK
                else _DIAGONALS + _ORTHOGONALS)
        slider_safe = _swap(board, sq, not color) == 0
        for df, dr in dirs:
            hits: list[chess.Square] = []
            f, r = chess.square_file(sq) + df, chess.square_rank(sq) + dr
            while 0 <= f < 8 and 0 <= r < 8 and len(hits) < 2:
                s = chess.square(f, r)
                if board.piece_at(s):
                    hits.append(s)
                f, r = f + df, r + dr
            if len(hits) < 2:
                continue
            front, back = hits
            p1, p2 = board.piece_at(front), board.piece_at(back)
            if p1.color == color or p2.color == color:
                continue
            v1, v2, vs = VALUE[p1.piece_type], VALUE[p2.piece_type], VALUE[slider]
            back_wins = v2 > vs or not _is_defended(board, back)
            key = f"{chess.square_name(sq)}-{chess.square_name(front)}-{chess.square_name(back)}"
            attacker = piece_label(board, sq)
            if p2.piece_type == chess.KING:
                out.append((key, color, f"{piece_label(board, front)} is pinned to its king by "
                                        f"{attacker} and cannot legally move off that line"))
            elif not slider_safe:
                continue
            elif v2 > v1 and back_wins and p1.piece_type != chess.KING:
                out.append((key, color, f"{piece_label(board, front)} is pinned by {attacker}: "
                                        f"moving it would expose the {piece_label(board, back, False)}"))
            elif (p1.piece_type == chess.KING or v1 > v2) and back_wins:
                out.append((key, color, f"{attacker} skewers the {piece_label(board, front, False)}: "
                                        f"once it moves, the {piece_label(board, back, False)} behind it falls"))
    return out


def _forks(board: chess.Board, color: chess.Color) -> list[tuple[str, bool, str]]:
    """Safe pieces of `color` attacking two or more worthwhile targets at once."""
    out = []
    for sq in chess.SquareSet(board.occupied_co[color]):
        forker = board.piece_type_at(sq)
        targets = []
        for t in board.attacks(sq):
            victim = board.piece_at(t)
            if victim is None or victim.color == color:
                continue
            if (victim.piece_type == chess.KING or VALUE[victim.piece_type] > VALUE[forker]
                    or not _is_defended(board, t)):
                targets.append(t)
        if len(targets) < 2 or _swap(board, sq, not color) > 0:
            continue
        names = " and the ".join(piece_label(board, t, False) for t in targets)
        key = f"{chess.square_name(sq)}>" + ",".join(chess.square_name(t) for t in targets)
        out.append((key, color, f"{piece_label(board, sq)} forks the {names}"))
    return out


def _mate_in_one(board: chess.Board, color: chess.Color) -> str:
    """SAN of a mate-in-1 `color` would have if it were their move ('' if none)."""
    b = board.copy(stack=False)
    if b.turn != color:
        if b.is_check():
            return ""  # a null move would leave the king in check
        b.push(chess.Move.null())
    for mv in list(b.legal_moves):
        san = b.san(mv)
        if san.endswith("#"):
            return san
    return ""


@lru_cache(maxsize=1024)
def _analyse_fen(fen: str) -> PositionTactics:
    board = chess.Board(fen)
    pt = PositionTactics()
    for color in (chess.WHITE, chess.BLACK):
        pt.en_prise[color] = _en_prise(board, color)
        pt.lines += _line_tactics(board, color)
        pt.forks += _forks(board, color)
        mate = _mate_in_one(board, color)
        if mate:
            pt.mate_threat[color] = mate
    return pt


def analyse(board: chess.Board) -> PositionTactics:
    """All tactical facts for a position (memoised)."""
    return _analyse_fen(board.fen())


def position_lines(board: chess.Board) -> list[tuple[str, str]]:
    """(key, text) facts for the position summary; keys let callers diff positions."""
    pt = analyse(board)
    out: list[tuple[str, str]] = []
    for color in (chess.WHITE, chess.BLACK):
        for t in pt.en_prise[color]:
            out.append((f"enprise:{chess.square_name(t.square)}", t.text + "."))
    for key, _, text in pt.lines:
        out.append((f"line:{key}", text[0].upper() + text[1:] + "."))
    for key, _, text in pt.forks:
        out.append((f"fork:{key}", text + "."))
    for color, san in pt.mate_threat.items():
        who = color_name(color)
        verb = "can deliver checkmate now with" if board.turn == color else "threatens checkmate with"
        out.append((f"mate:{who}", f"{who} {verb} {san}."))
    return out


# ------------------------------------------------------------- move motifs

def move_motifs(board: chess.Board, move: chess.Move) -> list[str]:
    """What `move` does tactically, as short verified clauses.

    `board` is the position before the move. Returns phrases such as
    "wins material: nets about 3 points after all recaptures" or
    "leaves the bishop on c4 en prise".
    """
    mover, opp = board.turn, not board.turn
    after = board.copy(stack=False)
    after.push(move)
    if after.is_checkmate():
        return ["delivers checkmate"]
    before_t, after_t = analyse(board), analyse(after)
    moved = piece_label(after, move.to_square, False)
    out: list[str] = []

    xv = exchange_value(board, move)
    if board.is_capture(move):
        if xv > _MATERIAL_EPS:
            out.append(f"wins material: nets about {_points(xv)} after all recaptures")
        elif xv < -_MATERIAL_EPS:
            out.append(f"loses material: after the recaptures it costs about {_points(-xv)}")
        else:
            out.append("is an even trade")
    elif xv < -_MATERIAL_EPS:
        out.append(f"puts the {moved} en prise: it can be captured, losing about {_points(-xv)}")

    was_en_prise = before_t.en_prise_squares(mover)
    now_en_prise = after_t.en_prise_squares(mover)
    if move.from_square in was_en_prise and move.to_square not in now_en_prise:
        out.append(f"moves the attacked {chess.piece_name(board.piece_type_at(move.from_square))} to safety")
    for sq in was_en_prise - {move.from_square}:
        if sq not in now_en_prise and after.piece_at(sq) is not None:
            out.append(f"protects the {piece_label(after, sq, False)}, which was en prise")
    for t in after_t.en_prise[mover]:
        if t.square not in was_en_prise and t.square != move.to_square:
            out.append(f"leaves the {piece_label(after, t.square, False)} en prise "
                       f"(the opponent can win about {_points(t.gain)})")
    before_opp = before_t.en_prise_squares(opp)
    for t in after_t.en_prise[opp]:
        if t.square not in before_opp:
            out.append(f"attacks {piece_label(after, t.square)}, which can now be won "
                       f"unless {color_name(opp)} responds")

    before_forks = {k for k, _, _ in before_t.forks}
    for key, color, text in after_t.forks:
        if color == mover and key not in before_forks:
            out.append("creates a fork: " + text)
    before_lines = {k for k, _, _ in before_t.lines}
    for key, color, text in after_t.lines:
        if color == mover and key not in before_lines:
            out.append("creates a pin/skewer: " + text)
    for key, color, text in before_t.lines:
        if color == opp and key not in {k for k, _, _ in after_t.lines}:
            out.append("breaks the opponent's pin/skewer (" + text + ")")

    if mover in after_t.mate_threat and not after.is_check():
        out.append(f"threatens checkmate with {after_t.mate_threat[mover]}")
    if opp in before_t.mate_threat and opp not in after_t.mate_threat:
        out.append(f"stops {color_name(opp)}'s threat of checkmate ({before_t.mate_threat[opp]})")
    elif opp in after_t.mate_threat and opp not in before_t.mate_threat:
        out.append(f"allows {color_name(opp)} to threaten checkmate with {after_t.mate_threat[opp]}")
    return out


_WINNING = ("delivers checkmate", "wins material", "creates a fork", "creates a pin", "threatens checkmate")


def existing_threat(board: chess.Board, reply: chess.Move) -> str:
    """Was `reply` already a threat before the side to move played?

    `board` is the position before a move; `reply` is the opponent's punishing
    answer to it. If the opponent could already have played `reply` with the
    same effect had it been their turn (checked with a "null move"), the move
    ignored an existing threat. Returns e.g. "Nxe4 (wins material: nets about
    1 point after all recaptures)", or "" when the move itself created the problem.
    """
    if board.is_check():
        return ""   # a null move is impossible in check
    b = board.copy(stack=False)
    b.push(chess.Move.null())
    if reply not in b.legal_moves:
        return ""
    for motif in move_motifs(b, reply):
        if motif.startswith(_WINNING):
            return f"{b.san(reply)} ({motif})"
    return ""


def motif_tags(board: chess.Board, move: chess.Move) -> list[str]:
    """Short labels for UI chips (e.g. 'fork', 'hangs material', 'wins material')."""
    return tags_from_motifs(move_motifs(board, move))


def tags_from_motifs(motifs: list[str]) -> list[str]:
    """Map move_motifs() clauses to short UI labels."""
    tags = []
    for m in motifs:
        if m.startswith("delivers checkmate"):
            tags.append("checkmate")
        elif m.startswith("wins material"):
            tags.append("wins material")
        elif m.startswith(("loses material", "puts the", "leaves the")):
            tags.append("hangs material")
        elif m.startswith("creates a fork"):
            tags.append("fork")
        elif m.startswith("creates a pin"):
            tags.append("pin" if "pinned" in m else "skewer")
        elif m.startswith("threatens checkmate"):
            tags.append("mate threat")
        elif m.startswith("allows"):
            tags.append("allows mate threat")
    return list(dict.fromkeys(tags))
