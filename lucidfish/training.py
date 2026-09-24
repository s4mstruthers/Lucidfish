"""Training: the coached player's own mistakes, across all games, as puzzles.

Every inaccuracy, mistake and blunder the player made in an analysed game
becomes a puzzle: the position before the move, where they must find a better
move. The web page schedules the puzzles with spaced repetition (a Leitner
system: puzzles you solve come back less and less often, puzzles you miss come
back soon) and chooses which to show (by default the key and costly ones, not
bullet games and not moves made in time trouble, a few new ones a day); this
module lists them with what that choice needs:

- importance: 3 "key" (the error changed the game: a win thrown away, a playable
  game turned into a lost one, a forced mate missed or allowed), 2 "costly" (a
  clear mistake or blunder), 1 "minor" (an inaccuracy, or an error when the game
  was already decided);
- themes: why the error happened, as the analysis verified it (hanging material,
  a missed tactic, a missed threat, mates, king safety), plus the game phase.
"""

from __future__ import annotations

import re

from . import ratings, store
from .insights import mistake_patterns
from .scoring import win_percent

ERRORS = ("inaccuracy", "mistake", "blunder")
THEMES = ("missed_threat", "hanging", "missed_tactic", "missed_mate", "allowed_mate", "king_attack")


def importance(m: dict, before: float, after: float) -> tuple[int, str]:
    """How much an error mattered (3 key, 2 costly, 1 minor) and why, from the mover's winning chances."""
    loss = max(0.0, before - after)
    event = m.get("mate_event")
    if event == "missed_mate":
        return (3, "You missed a forced checkmate.") if after < 85 else \
            (2, "You missed a forced checkmate (and were still winning).")
    if event == "allowed_mate" and before > 15:
        return 3, "It allowed a forced checkmate."
    if after >= 85:
        return 1, "You were still clearly winning afterwards."
    if before <= 15:
        return 1, "The game was already lost."
    if before >= 70 and after <= 50:
        return 3, "It threw away a winning position."
    if before >= 35 and after <= 25:
        return 3, "It turned a playable game into a losing one."
    if loss >= 25:
        return 3, f"It cost {loss:.0f}% of your winning chances."
    if m.get("cls") in ("mistake", "blunder"):
        return 2, f"It cost {loss:.0f}% of your winning chances."
    return 1, "A small inaccuracy."


def train_items(profile_id: int) -> list[dict]:
    """The puzzles for a profile, newest games first.

    Only games where Lucidfish knows which side the player had are used: in the
    others, the opponent's errors would turn up as the player's puzzles.
    """
    items = []
    for row in store.list_games(profile_id):
        side = (row.get("user_side") or "").lower()
        if side not in ("white", "black"):
            continue
        g = store.get_game(row["id"])
        if g is None:
            continue
        opponent = g["black"] if side == "white" else g["white"]
        # "Time trouble": under a tenth of the starting clock (at least 10 s) left after the move.
        base = re.match(r"(\d+)", ratings.pgn_headers(g["pgn"] or "").get("TimeControl", ""))
        low_clock = max(10.0, 0.1 * int(base[1])) if base else 10.0
        for i, m in enumerate(g["moves"]):
            if (m.get("cls") not in ERRORS or m.get("side", "").lower() != side
                    or not m.get("fen_before") or not m.get("best_uci")):
                continue
            cands = m.get("candidates") or []
            mover_after = m["win"] if side == "white" else 100 - m["win"]
            mover_before = win_percent(cands[0]["cp"]) if cands and "cp" in cands[0] else mover_after
            loss = max(0.0, mover_before - mover_after)
            level, why = importance(m, mover_before, mover_after)
            items.append({
                # Stable across re-analyses and new shared copies (the progress is stored under it).
                "id": f"{row.get('fingerprint') or row['id']}:{m['ply']}",
                "game_id": row["id"], "i": i, "fen": m["fen_before"], "side": side,
                "cls": m["cls"], "played": m["san"], "best": m.get("best", ""),
                "label": f"{m['n']}{'.' if m['side'] == 'White' else '…'}",
                "loss": round(loss, 1), "phase": m.get("phase", ""), "tags": m.get("tags", []),
                "opponent": opponent, "date": row.get("date") or "", "opening": row.get("opening") or "",
                "time_class": row.get("time_class") or "",
                "hurried": m.get("clock_s") is not None and m["clock_s"] < low_clock,
                "importance": level, "why": why,
                "themes": [t for t in THEMES if t in mistake_patterns(m)],
            })
    return items
