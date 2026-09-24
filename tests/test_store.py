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
