"""Training: the coached player's own mistakes, across all games, as puzzles.

Every inaccuracy, mistake and blunder the player made in an analysed game
becomes a puzzle: the position before the move, where they must find a better
move. The web page schedules the puzzles with spaced repetition (a Leitner
system: puzzles you solve come back less and less often, puzzles you miss come
back soon); this module only lists them.
"""

from __future__ import annotations

from . import store
from .scoring import win_percent

ERRORS = ("inaccuracy", "mistake", "blunder")


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
        for i, m in enumerate(g["moves"]):
            if (m.get("cls") not in ERRORS or m.get("side", "").lower() != side
                    or not m.get("fen_before") or not m.get("best_uci")):
                continue
            cands = m.get("candidates") or []
            mover_after = m["win"] if side == "white" else 100 - m["win"]
            loss = max(0.0, win_percent(cands[0]["cp"]) - mover_after) if cands and "cp" in cands[0] else 0.0
            items.append({
                # Stable across re-analyses and new shared copies (the progress is stored under it).
                "id": f"{row.get('fingerprint') or row['id']}:{m['ply']}",
                "game_id": row["id"], "i": i, "fen": m["fen_before"], "side": side,
                "cls": m["cls"], "played": m["san"], "best": m.get("best", ""),
                "label": f"{m['n']}{'.' if m['side'] == 'White' else '…'}",
                "loss": round(loss, 1), "phase": m.get("phase", ""), "tags": m.get("tags", []),
                "opponent": opponent, "date": row.get("date") or "", "opening": row.get("opening") or "",
            })
    return items
