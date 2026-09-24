"""Evaluation maths: winning chances, move accuracy, and human-readable evals.

Centipawns are a poor measure of how bad a move was: dropping from +8 to +5
changes nothing, while dropping from 0 to -2 usually loses the game. Like
Lichess, Lucidfish therefore judges moves by the change in *winning chances*
(an expected-score percentage derived from the engine eval) and computes game
accuracy with Lichess's published formula, so the numbers are comparable to
what players see there.

Every function here is pure (no engine, no I/O) and fully unit-tested.
"""

from __future__ import annotations

import math
from statistics import pstdev

# Evals are clamped to ±MATE_CP before converting to winning chances; a forced
# mate counts as the ceiling. Matches Lichess's `Cp.CEILING`.
MATE_CP = 1000
_WIN_K = 0.00368208  # Lichess logistic slope, fitted on real games


def cp_value(cp: int | None, mate: int | None) -> int:
    """Collapse an engine score (cp or signed mate distance) into clamped centipawns."""
    if mate is not None:
        return MATE_CP if mate > 0 else -MATE_CP
    if cp is None:
        return 0
    return max(-MATE_CP, min(MATE_CP, cp))


def win_percent(cp: int | None, mate: int | None = None) -> float:
    """Winning chances (0-100) for the side whose perspective the score is in."""
    x = cp_value(cp, mate)
    return 50 + 50 * (2 / (1 + math.exp(-_WIN_K * x)) - 1)


def move_accuracy(win_before: float, win_after: float) -> float:
    """Lichess per-move accuracy (0-100) from the mover's winning chances."""
    if win_after >= win_before:
        return 100.0
    raw = 103.1668100711649 * math.exp(-0.04354415386753951 * (win_before - win_after)) - 3.166924740191411
    return max(0.0, min(100.0, raw + 1))  # +1 uncertainty bonus, as Lichess does


def game_accuracy(white_win_percents: list[float], white_to_move_first: bool = True) -> dict[str, float | None]:
    """Per-side game accuracy (Lichess formula).

    ``white_win_percents`` is White's winning chances for the starting position
    followed by the position after every move. Each side's accuracy blends a
    volatility-weighted mean (calm positions matter less) with a harmonic mean
    (one blunder drags the score down), exactly like Lichess.
    """
    n_moves = len(white_win_percents) - 1
    if n_moves < 1:
        return {"white": None, "black": None}
    window = max(2, min(8, n_moves // 10))
    windows = [white_win_percents[:window]] * max(0, window - 2) + [
        white_win_percents[i:i + window] for i in range(len(white_win_percents) - window + 1)
    ]
    weights = [max(0.5, min(12.0, pstdev(w))) for w in windows]

    per_side: dict[str, list[tuple[float, float]]] = {"white": [], "black": []}
    for i in range(n_moves):
        prev, nxt = white_win_percents[i], white_win_percents[i + 1]
        white_moved = (i % 2 == 0) == white_to_move_first
        if white_moved:
            acc = move_accuracy(prev, nxt)
        else:
            acc = move_accuracy(100 - prev, 100 - nxt)
        weight = weights[i] if i < len(weights) else weights[-1]
        per_side["white" if white_moved else "black"].append((acc, weight))

    out: dict[str, float | None] = {}
    for side, items in per_side.items():
        if not items:
            out[side] = None
            continue
        total_w = sum(w for _, w in items)
        weighted = sum(a * w for a, w in items) / total_w
        harmonic = 0.0 if any(a <= 0 for a, _ in items) else len(items) / sum(1 / a for a, _ in items)
        out[side] = round((weighted + harmonic) / 2, 1)
    return out


# ------------------------------------------------------------ presentation

def eval_text(cp: int | None, mate: int | None) -> str:
    """Compact eval in the convention of every chess site: '+1.25', '#3', '#-2'."""
    if mate is not None:
        return f"#{mate}"
    if cp is None:
        return "0.00"
    return f"{cp / 100:+.2f}"


def describe_eval(cp: int | None, mate: int | None) -> str:
    """Plain-English assessment of a White-perspective score, for LLM prompts.

    Giving the model words instead of numbers stops it from quoting centipawns
    and keeps its assessment consistent with the engine's.
    """
    if mate is not None:
        side = "White" if mate > 0 else "Black"
        n = abs(mate)
        return f"{side} has a forced checkmate (mate in {n})" if n else f"{side} has delivered checkmate"
    cp = cp or 0
    side = "White" if cp > 0 else "Black"
    a = abs(cp)
    if a < 25:
        return "the position is equal"
    if a < 70:
        return f"{side} has a slight edge"
    if a < 150:
        return f"{side} is clearly better"
    if a < 300:
        return f"{side} is much better, close to winning"
    if a < 800:
        return f"{side} is winning"
    return f"{side} is completely winning"
