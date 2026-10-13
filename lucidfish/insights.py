# Copyright (C) 2026 Sam Struthers
# SPDX-License-Identifier: AGPL-3.0-or-later
# This file is part of Lucidfish <https://github.com/s4mstruthers/Lucidfish>; see LICENSE for the terms.

"""Per-game insights for the progress dashboard: accuracy by game phase, the
kinds of mistakes a player keeps making, and patterns across their games
(converting winning positions, castling late, results by colour, ...).

Computed once when a game is saved (from the analysed moves, no engine or AI
needed) and aggregated across games for the dashboard and the coach profile.
The cross-game findings are plain statistics, stated only when enough games
support them; the AI coach turns them into advice, it never invents them.
"""

from __future__ import annotations

import re
from collections import Counter

import chess

from . import features, tactics

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
    "missed_threat": "Missed the opponent's threat",
}
VERSION = 2          # bump when game_insights() learns something new: stored games are recomputed
_WINNING_MOTIFS = ("wins material", "creates a fork", "creates a pin")


def _phase(fen: str | None) -> str:
    """Phase for moves stored before the analysis recorded it."""
    try:
        return features.game_phase(chess.Board(fen)) if fen else ""
    except ValueError:
        return ""


def _patterns(m: dict) -> set[str]:
    """Why an error happened, as far as the verified evidence shows."""
    out: set[str] = set()
    tags = m.get("tags") or []
    if "hangs material" in tags:
        out.add("hanging")
    if "allows mate threat" in tags:
        out.add("king_attack")
    if m.get("threat") or "missed threat" in tags:
        out.add("missed_threat")
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


def mistake_patterns(m: dict) -> set[str]:
    """Public: why an error happened (pattern ids of PATTERNS)."""
    return _patterns(m)


def _user_result(result: str, side: str | None) -> str:
    if not side or result not in ("1-0", "0-1", "1/2-1/2"):
        return ""
    if result == "1/2-1/2":
        return "D"
    return "W" if (result == "1-0") == (side.lower() == "white") else "L"


def game_facts(moves: list[dict], user_side: str | None, result: str = "", time_class: str = "") -> dict:
    """Facts about one game, from the user's point of view, for cross-game patterns."""
    side = (user_side or "").lower()
    if not side or not moves:
        return {}
    pov = [(m.get("win", 50.0) if side == "white" else 100 - m.get("win", 50.0)) for m in moves]
    castle = next((m.get("n") for m in moves if m.get("side", "").lower() == side
                   and str(m.get("san", "")).startswith("O-O")), None)
    facts = {"side": side, "result": _user_result(result, side), "time_class": time_class,
             "plies": len(moves), "castle_move": castle, "max_win": round(max(pov), 1),
             "min_win": round(min(pov), 1),
             "missed_threats": sum(1 for m in moves if m.get("side", "").lower() == side and m.get("threat"))}
    exit_i = next((i for i, m in enumerate(moves) if m.get("left_book")), None)
    if exit_i is not None:
        before = pov[exit_i - 1] if exit_i else 50.0
        facts["book_exit"] = {"move": moves[exit_i].get("n"), "by_user": moves[exit_i].get("side", "").lower() == side,
                              "delta": round(pov[min(len(pov) - 1, exit_i + 10)] - before, 1)}
    return facts


def game_insights(moves: list[dict], user_side: str | None, result: str = "", time_class: str = "") -> dict:
    """Phase accuracy, mistake patterns and game facts for the user's side of one game."""
    mine = [m for m in moves if not user_side or m.get("side", "").lower() == user_side.lower()]
    phases: dict[str, list[float]] = {}
    patterns: Counter = Counter()
    for m in mine:
        phase = m.get("phase") or _phase(m.get("fen_before"))
        if phase in PHASES and m.get("acc") is not None:
            row = phases.setdefault(phase, [0.0, 0, 0])
            row[0] += m["acc"]
            row[1] += 1
            row[2] += m.get("cls") in ("mistake", "blunder")
        if m.get("cls") in ERRORS:
            patterns.update(_patterns(m))
    return {"v": VERSION, "phases": {p: [round(v[0], 1), v[1], v[2]] for p, v in phases.items()},
            "patterns": dict(patterns), "facts": game_facts(moves, user_side, result, time_class)}


# ------------------------------------------------------------------ across games

MIN_GROUP = 3         # games needed on each side of a comparison
MIN_GAP = 20.0        # percentage points of score difference worth mentioning


def _score(games: list[dict]) -> float:
    return 100.0 * sum(1.0 if g["result"] == "W" else 0.5 if g["result"] == "D" else 0.0 for g in games) / len(games)


def _pct(x: float) -> str:
    return f"{x:.0f}%"


def findings(per_game: list[dict]) -> list[dict]:
    """Verified patterns across the user's games: [{id, kind, text}], weaknesses first.

    kind is "weakness", "strength" or "info". A finding is only stated when enough
    games support it (MIN_GROUP per group, MIN_GAP difference for comparisons).
    """
    games = [g["facts"] for g in per_game if (g.get("facts") or {}).get("result")]
    out: list[dict] = []
    if len(games) < MIN_GROUP:
        return out

    winning = [g for g in games if g["max_win"] >= 80]
    if len(winning) >= MIN_GROUP:
        won = sum(g["result"] == "W" for g in winning)
        rate = won / len(winning)
        out.append({"id": "conversion", "kind": "weakness" if rate < 0.7 else "strength" if rate >= 0.85 else "info",
                    "text": f"You won {won} of the {len(winning)} games in which you reached a winning position "
                            f"(80%+ winning chances)."})
    losing = [g for g in games if g["min_win"] <= 20]
    if len(losing) >= MIN_GROUP:
        saved = sum(g["result"] != "L" for g in losing)
        out.append({"id": "defence", "kind": "strength" if saved / len(losing) >= 0.35 else "info",
                    "text": f"You saved (drew or won) {saved} of the {len(losing)} games in which you were "
                            "clearly losing."})

    early = [g for g in games if g["castle_move"] is not None and g["castle_move"] <= 10]
    late = [g for g in games if g["castle_move"] is None or g["castle_move"] > 10]
    if len(early) >= MIN_GROUP and len(late) >= MIN_GROUP and abs(_score(early) - _score(late)) >= MIN_GAP:
        worse_late = _score(late) < _score(early)
        out.append({"id": "castling", "kind": "weakness" if worse_late else "info",
                    "text": f"You score {_pct(_score(early))} when you castle by move 10 and "
                            f"{_pct(_score(late))} when you castle later or not at all."})

    white = [g for g in games if g["side"] == "white"]
    black = [g for g in games if g["side"] == "black"]
    if len(white) >= MIN_GROUP and len(black) >= MIN_GROUP and abs(_score(white) - _score(black)) >= MIN_GAP:
        weaker = "Black" if _score(black) < _score(white) else "White"
        out.append({"id": "colour", "kind": "weakness",
                    "text": f"You score {_pct(_score(white))} with White and {_pct(_score(black))} with Black, "
                            f"so your games as {weaker} need the most work."})

    by_tc: dict[str, list[dict]] = {}
    for g in games:
        if g.get("time_class"):
            by_tc.setdefault(g["time_class"], []).append(g)
    tcs = {tc: _score(gs) for tc, gs in by_tc.items() if len(gs) >= MIN_GROUP}
    if len(tcs) >= 2:
        best, worst = max(tcs, key=tcs.get), min(tcs, key=tcs.get)
        if tcs[best] - tcs[worst] >= MIN_GAP:
            out.append({"id": "time_class", "kind": "info",
                        "text": f"Your best results are in {best} ({_pct(tcs[best])}) and your weakest in "
                                f"{worst} ({_pct(tcs[worst])})."})

    exits = [g["book_exit"] for g in games if g.get("book_exit")]
    if len(exits) >= MIN_GROUP:
        avg = sum(e["delta"] for e in exits) / len(exits)
        move = sum(e["move"] or 0 for e in exits) / len(exits)
        kind = "weakness" if avg <= -5 else "strength" if avg >= 5 else "info"
        out.append({"id": "after_book", "kind": kind,
                    "text": f"Your games leave opening theory around move {move:.0f}; over the next five moves "
                            f"your winning chances change by {avg:+.0f} points on average."})

    long = [g for g in games if g["plies"] > 80]
    short = [g for g in games if g["plies"] <= 80]
    if len(long) >= MIN_GROUP and len(short) >= MIN_GROUP and abs(_score(long) - _score(short)) >= MIN_GAP + 5:
        out.append({"id": "length", "kind": "weakness" if _score(long) < _score(short) else "strength",
                    "text": f"You score {_pct(_score(long))} in games longer than 40 moves and "
                            f"{_pct(_score(short))} in shorter ones."})

    threat_games = [g for g in games if g.get("missed_threats")]
    if len(threat_games) >= 2:
        total = sum(g["missed_threats"] for g in threat_games)
        out.append({"id": "threats", "kind": "weakness",
                    "text": f"In {len(threat_games)} of your {len(games)} games you missed a threat that was "
                            f"already on the board ({total} time{'s' if total != 1 else ''} in all). Before each "
                            "move, ask: what does my opponent threaten?"})
    order = {"weakness": 0, "strength": 1, "info": 2}
    return sorted(out, key=lambda f: order[f["kind"]])


# ------------------------------------------------------------------ over time

RECENT_MAX = 10       # "recent" is the last games, at most this many
_DATE = re.compile(r"^\d{4}\.\d{2}\.\d{2}$")

# (key, label, higher is better, smallest change worth calling a change)
PROGRESS_METRICS = (
    ("accuracy", "Accuracy", True, 3.0),
    ("score", "Score", True, 10.0),
    ("blunders", "Blunders per game", False, 0.3),
    ("mistakes", "Mistakes per game", False, 0.3),
    ("missed_threats", "Missed threats per game", False, 0.3),
)


def timeline(games: list[dict], per_game: list[dict]) -> list[dict]:
    """One row per game in the order they were played (undated games first, in analysis order)."""
    facts = {g.get("game_id"): (g.get("facts") or {}) for g in per_game}
    patterns = {g.get("game_id"): (g.get("patterns") or {}) for g in per_game}
    rows = []
    for g in games:
        date = g.get("date") or ""
        rows.append({"id": g["id"], "date": date if _DATE.match(date) else "", "accuracy": g.get("accuracy"),
                     "acpl": g.get("acpl"), "blunders": g.get("blunders") or 0, "mistakes": g.get("mistakes") or 0,
                     "result": _user_result(g.get("result") or "", (g.get("user_side") or "").lower()),
                     "time_class": g.get("time_class") or "", "opening": g.get("opening") or "",
                     "opponent": g.get("black") if (g.get("user_side") or "").lower() == "white" else g.get("white"),
                     "missed_threats": facts.get(g["id"], {}).get("missed_threats", 0),
                     "patterns": patterns.get(g["id"], {})})
    rows.sort(key=lambda r: (r["date"], r["id"]))
    return rows


def _measures(rows: list[dict]) -> dict:
    accs = [r["accuracy"] for r in rows if r["accuracy"] is not None]
    scored = [r for r in rows if r["result"]]
    n = max(1, len(rows))
    return {"games": len(rows),
            "accuracy": round(sum(accs) / len(accs), 1) if accs else None,
            "score": round(_score(scored)) if scored else None,
            "blunders": round(sum(r["blunders"] for r in rows) / n, 2),
            "mistakes": round(sum(r["mistakes"] for r in rows) / n, 2),
            "missed_threats": round(sum(r["missed_threats"] for r in rows) / n, 2),
            "patterns": {pid: round(sum(r["patterns"].get(pid, 0) for r in rows) / n, 2)
                         for pid in {k for r in rows for k in r["patterns"]}}}


def progress(games: list[dict], per_game: list[dict]) -> dict:
    """How the most recent games compare with the ones before them: the per-game timeline, both groups'
    measures and the differences that are big enough to mean something (`changes`, verified). Needs at
    least MIN_GROUP games in each group; until then only the timeline is filled in."""
    rows = timeline(games, per_game)
    out: dict = {"series": rows, "recent": None, "earlier": None, "changes": []}
    if len(rows) < 2 * MIN_GROUP:
        return out
    n_recent = min(RECENT_MAX, len(rows) // 2)
    earlier, recent = rows[:-n_recent], rows[-n_recent:]
    out["earlier"], out["recent"] = _measures(earlier), _measures(recent)
    for key, label, higher_better, step in PROGRESS_METRICS:
        a, b = out["earlier"][key], out["recent"][key]
        if a is None or b is None:
            continue
        delta = round(b - a, 2)
        unit = "%" if key in ("accuracy", "score") else ""
        verdict = "same" if abs(delta) < step else "better" if (delta > 0) == higher_better else "worse"
        out["changes"].append({"id": key, "label": label, "earlier": a, "recent": b, "delta": delta,
                               "unit": unit, "verdict": verdict})
    for pid in sorted({*out["earlier"]["patterns"], *out["recent"]["patterns"]} - {"missed_threat"}):  # counted above
        a, b = out["earlier"]["patterns"].get(pid, 0.0), out["recent"]["patterns"].get(pid, 0.0)
        delta = round(b - a, 2)
        if abs(delta) >= 0.3 and max(a, b) >= 0.5:
            out["changes"].append({"id": f"pattern:{pid}", "label": f"{PATTERNS.get(pid, pid)} per game",
                                   "earlier": a, "recent": b, "delta": delta, "unit": "",
                                   "verdict": "better" if delta < 0 else "worse"})
    return out


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
        "findings": findings(per_game),
    }
