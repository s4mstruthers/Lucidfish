from lucidfish import store


def _moves(side="White", cls="best", loss=0):
    return [{"side": side, "cls": cls, "cp_loss": loss}]


def test_profiles_crud_and_active_pointer():
    pid = store.create_profile(name="Ann", level="club")
    assert store.active_id() == pid
    store.update_profile(pid, lichess_user="ann_l", bogus="ignored")
    assert store.get_profile(pid)["lichess_user"] == "ann_l"
    other = store.create_profile(name="Bob")
    store.delete_profile(other)
    assert store.active_id() == pid
    assert [p["name"] for p in store.list_profiles()] == ["Ann"]


def test_games_stats_and_dedupe():
    pid = store.create_profile(name="Cat")
    headers = {"White": "Cat", "Black": "X", "Result": "1-0"}
    gid = store.save_game(pid, "1. e4 *", headers, "white", 1500, "Italian Game (C50)", "review",
                          _moves("White", "blunder", 400) + _moves("Black", "best"), "rapid",
                          accuracy={"white": 80.0, "black": 90.0})
    assert gid and store.has_game(pid, "1. e4 *")
    assert store.save_game(pid, "1. e4 *", headers, "white", None, "", "", [], "") is None  # duplicate
    stats = store.aggregate_stats(pid)
    assert stats["games"] == 1 and stats["wins"] == 1
    assert stats["avg_accuracy"] == 80.0 and stats["blunders_per_game"] == 1
    assert stats["openings"][0]["name"] == "Italian Game"
    store.delete_game(gid)
    assert store.aggregate_stats(pid) == {"games": 0}


def test_good_moves_are_counted_too():
    pid = store.create_profile(name="Dee")
    moves = [m for side, cls, n in (("White", "best", 3), ("White", "good", 2), ("White", "mistake", 1),
                                    ("Black", "best", 5)) for _ in range(n) for m in _moves(side, cls)]
    moves[0]["critical"] = True                        # the only good move at a critical moment
    moves[5]["critical"] = True                        # critical, but missed: not a "great" move
    store.save_game(pid, "1. d4 *", {"Result": "1-0"}, "white", None, "", "", moves, "blitz")
    g = store.list_games(pid)[0]
    assert (g["best_moves"], g["good_moves"], g["great_moves"], g["mistakes"]) == (3, 2, 1, 1)   # White's only


def test_games_from_older_versions_get_the_good_move_counts():
    import sqlite3
    pid = store.create_profile(name="Eve")
    moves = _moves("Black", "good") * 4 + _moves("White", "good")
    store.save_game(pid, "1. e4 *", {"Result": "0-1"}, "black", None, "", "", moves, "rapid")
    with sqlite3.connect(store.db_path()) as c:          # as an older version left it
        c.execute("UPDATE games SET good_moves=NULL, great_moves=NULL")
    store._initialised.clear()
    store.init()
    assert store.list_games(pid)[0]["good_moves"] == 4


def test_settings_merge():
    store.save_settings({"provider": "openai", "depth": 20})
    store.save_settings({"depth": 16})
    assert store.get_settings() == {"provider": "openai", "depth": 16}


def test_engine_cache_respects_strength_and_multipv():
    cache = store.EngineCache()
    cache.put("k", 18, 3, [[["e2e4"], 20, None]])
    assert cache.get("k", 18, 3) == [[["e2e4"], 20, None]]
    assert cache.get("k", 16, 1) is not None       # deeper result satisfies a shallower request
    assert cache.get("k", 20, 3) is None
    assert cache.get("k", 18, 4) is None
    cache.put("k", 12, 1, [[["d2d4"], 0, None]])  # shallower result never overwrites a deeper one
    assert cache.get("k", 18, 3)[0][0] == ["e2e4"]
    store.clear_caches()
    assert store.cache_stats()["engine_positions"] == 0
