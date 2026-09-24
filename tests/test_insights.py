from lucidfish import insights, store


def _move(side, phase, acc, cls="best", **extra):
    return {"side": side, "phase": phase, "acc": acc, "cls": cls, "cp_loss": 0, "tags": [], "uci": "e2e4", **extra}


GAME = [
    _move("White", "opening", 100), _move("Black", "opening", 50),
    _move("White", "middlegame", 40, "blunder", tags=["hangs material"], think_s=2.0, clock_s=20.0),
    # Missed Rd8# (the best move delivers mate): recognised from the position itself.
    _move("White", "endgame", 60, "mistake", fen_before="6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1",
          best_uci="d1d8", uci="h2h3"),
]


def test_game_insights_counts_only_the_users_moves():
    data = insights.game_insights(GAME, "white")
    assert data["phases"] == {"opening": [100, 1, 0], "middlegame": [40, 1, 1], "endgame": [60, 1, 1]}
    assert data["patterns"] == {"hanging": 1, "rushed": 1, "time_trouble": 1, "missed_mate": 1}


def test_aggregate_ranks_patterns_and_finds_weakest_phase():
    per_game = [{"phases": {"opening": [900.0, 10, 0], "middlegame": [600.0, 10, 3]},
                 "patterns": {"hanging": 2, "missed_tactic": 1}},
                {"phases": {"opening": [800.0, 10, 1]}, "patterns": {"hanging": 1}}]
    agg = insights.aggregate(per_game)
    assert agg["phase_accuracy"] == {"opening": 85.0, "middlegame": 60.0, "endgame": None}
    assert agg["weakest_phase"] == "middlegame"
    assert agg["phase_errors"]["middlegame"] == 1.5
    assert agg["patterns"][0] == {"id": "hanging", "label": insights.PATTERNS["hanging"], "count": 3, "games": 2}


def test_stats_include_insights_and_backfill_old_games():
    store.init()
    pid = store.create_profile(name="Me")
    store.save_game(pid, "1. e4 *", {"White": "Me", "Black": "X", "Result": "1-0"}, "white", None, "", "",
                    GAME, accuracy={"white": 70})
    with store._db(write=True) as c:          # a game stored by an older version: no insights yet
        c.execute("UPDATE games SET insights=NULL")
    stats = store.aggregate_stats(pid)
    assert stats["phase_accuracy"]["opening"] == 100
    assert {p["id"] for p in stats["patterns"]} == {"hanging", "rushed", "time_trouble", "missed_mate"}
    with store._db() as c:
        assert c.execute("SELECT insights FROM games").fetchone()[0]    # saved for next time
