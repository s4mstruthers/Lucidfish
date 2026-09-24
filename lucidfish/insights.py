"""Per-game insights for the progress dashboard: accuracy by game phase and
the kinds of mistakes a player keeps making.

Computed once when a game is saved (from the analysed moves, no engine or AI
needed) and aggregated across games for the dashboard and the coach profile.
"""

from __future__ import annotations

from collections import Counter

import chess

from . import tactics

PHASES = ("opening", "middlegame", "endgame")
ERRORS = ("inaccuracy", "mistake", "blunder")

# Pattern id → how it is shown to the player.
PATTERNS = {
    "hanging": "Left material hanging",
    "missed_tactic": "Missed a tactic that wins material",
    "missed_mate": "Missed a checkmate",
    "allowed_mate": "Allowed a forced mate",
    "king_attack": "Allowed an attack on the king",
    "rushed": "Rushed a critical move (under 5 seconds)",
    "time_trouble": "Errors in time trouble (under 30 seconds left)",
}
_WINNING_MOTIFS = ("wins material", "creates a fork", "creates a pin")


def _patterns(m: dict) -> set[str]:
    """Why an error happened, as far as the verified evidence shows."""
    out: set[str] = set()
    tags = m.get("tags") or []
    if "hangs material" in tags:
        out.add("hanging")
    if "allows mate threat" in tags:
        out.add("king_attack")
    if m.get("mate_event") == "missed_mate":
        out.add("missed_mate")
    elif m.get("mate_event") == "allowed_mate":
        out.add("allowed_mate")
    best_uci = m.get("best_uci")
    if best_uci and best_uci != m.get("uci") and m.get("fen_before") and "missed_mate" not in out:
        try:
            board = chess.Board(m["fen_before"])
            motifs = tactics.move_motifs(board, chess.Move.from_uci(best_uci))
        except (ValueError, AssertionError):
            motifs = []
        if any(x.startswith("delivers checkmate") for x in motifs):
            out.add("missed_mate")
        elif any(x.startswith(_WINNING_MOTIFS) for x in motifs):
            out.add("missed_tactic")
    if m.get("cls") in ("mistake", "blunder"):
        if m.get("think_s") is not None and m["think_s"] < 5:
            out.add("rushed")
        if m.get("clock_s") is not None and m["clock_s"] < 30:
            out.add("time_trouble")
    return out


def game_insights(moves: list[dict], user_side: str | None) -> dict:
    """{'phases': {phase: [accuracy sum, moves, errors]}, 'patterns': {id: count}} for the user's moves."""
    mine = [m for m in moves if not user_side or m.get("side", "").lower() == user_side.lower()]
    phases: dict[str, list[float]] = {}
    patterns: Counter = Counter()
    for m in mine:
        phase = m.get("phase") or ""
        if phase in PHASES and m.get("acc") is not None:
            row = phases.setdefault(phase, [0.0, 0, 0])
            row[0] += m["acc"]
            row[1] += 1
            row[2] += m.get("cls") in ("mistake", "blunder")
        if m.get("cls") in ERRORS:
            patterns.update(_patterns(m))
    return {"phases": {p: [round(v[0], 1), v[1], v[2]] for p, v in phases.items()},
            "patterns": dict(patterns)}


def aggregate(per_game: list[dict]) -> dict:
    """Combine game_insights() of many games into dashboard statistics."""
    totals = {p: [0.0, 0, 0] for p in PHASES}
    patterns: Counter = Counter()
    games_with: Counter = Counter()
    for g in per_game:
        for phase, (acc_sum, n, errors) in (g.get("phases") or {}).items():
            if phase in totals:
                totals[phase][0] += acc_sum
                totals[phase][1] += n
                totals[phase][2] += errors
        for pid, count in (g.get("patterns") or {}).items():
            patterns[pid] += count
            games_with[pid] += 1
    games = max(1, len(per_game))
    measured = [p for p in PHASES if totals[p][1] >= 10]   # enough moves to compare phases fairly
    phase_accuracy = {p: round(v[0] / v[1], 1) if v[1] else None for p, v in totals.items()}
    return {
        "phase_accuracy": phase_accuracy,
        "phase_errors": {p: round(v[2] / games, 2) for p, v in totals.items()},
        "weakest_phase": min(measured, key=lambda p: phase_accuracy[p]) if len(measured) >= 2 else None,
        "patterns": [{"id": pid, "label": PATTERNS.get(pid, pid), "count": n, "games": games_with[pid]}
                     for pid, n in patterns.most_common(6)],
    }
