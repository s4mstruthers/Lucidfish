"""Time controls: each site's own classes, statistics and coach reviews per time control."""
import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient

from lucidfish import jobs, ratings, share, store, web
from lucidfish.prompts import build_player_summary_prompt

H = {"X-Lucidfish": "1"}


def _pgn(site: str, tc: str, white="Sam", black="Rival", result="1-0", elo="") -> str:
    tags = {"Site": site, "White": white, "Black": black, "Result": result, "TimeControl": tc, "Date": "2026.09.20"}
    if elo:
        tags["WhiteElo"] = elo
    return "".join(f'[{k} "{v}"]\n' for k, v in tags.items()) + f"\n1. e4 e5 {result}\n"


def _save(pid, pgn, time_class, blunders=0, review="A review."):
    moves = [{"side": "White", "cls": "blunder" if i < blunders else "best", "cp_loss": 300 if i < blunders else 0}
             for i in range(3)]
    headers = ratings.pgn_headers(pgn)
    return store.save_game(pid, pgn, headers, "white", None, "", review, moves, time_class)


def test_each_site_decides_what_rapid_means():
    t = ratings.time_class
    assert t({"Site": "Chess.com", "TimeControl": "1800"}) == "rapid"            # 30 minutes: rapid on chess.com
    assert t({"Site": "https://lichess.org/abc", "TimeControl": "1800"}) == "classical"   # ...classical on Lichess
    assert t({"Site": "https://lichess.org/abc", "TimeControl": "480"}) == "rapid"        # 8 minutes on Lichess
    assert t({"Site": "Chess.com", "TimeControl": "480"}) == "blitz"
    assert t({"Site": "Chess.com", "TimeControl": "1/86400"}) == "daily"
    assert t({"Site": "https://lichess.org/abc", "TimeControl": "-"}) == "daily"
    assert t({"TimeControl": "5400+30"}) == "classical" and t({"TimeControl": "?"}) == ""


def test_statistics_per_time_control():
    store.init()
    pid = store.create_profile(name="Sam")
    _save(pid, _pgn("Chess.com", "600"), "rapid", blunders=1)
    _save(pid, _pgn("Chess.com", "900", result="0-1"), "rapid")
    _save(pid, _pgn("Chess.com", "180"), "blitz", blunders=2)
    everything = store.aggregate_stats(pid)
    assert everything["games"] == 3 and set(everything["by_time_class"]) == {"blitz", "rapid"}
    assert everything["by_time_class"]["rapid"] == {"games": 2, "wins": 1, "losses": 1, "draws": 0,
                                                    "avg_accuracy": None, "blunders_per_game": 0.5,
                                                    "mistakes_per_game": 0.0}
    rapid = store.aggregate_stats(pid, time_class="rapid")
    assert rapid["games"] == 2 and rapid["time_class"] == "rapid" and rapid["by_time_class"] == {}
    assert store.aggregate_stats(pid, time_class="bullet") == {"games": 0}
    assert store.time_class_counts(store.list_games(pid)) == {"blitz": 1, "rapid": 2}
    assert len(store.recent_reviews(pid, time_class="blitz")) == 1


def test_a_review_per_time_control():
    store.init()
    pid = store.create_profile(name="Sam")
    store.set_summary(pid, "Overall review.")
    store.set_summary(pid, "Rapid review.", "rapid")
    p = store.get_profile(pid)
    assert p["summary"] == "Overall review." and p["summaries"] == {"rapid": "Rapid review."}
    # Coaching a rapid game uses the rapid review; a blitz game (no review yet) the overall one.
    assert jobs.coach_context(p, _pgn("Chess.com", "900")) == "Rapid review."
    assert jobs.coach_context(p, _pgn("Chess.com", "180")) == "Overall review."


def test_the_review_prompt_knows_its_time_control():
    stats = {"games": 4, "by_time_class": {}}
    prompt = build_player_summary_prompt(stats, [], {"name": "Sam"}, time_class="bullet")
    assert "ONLY the player's bullet games (4 of them)" in prompt and "In bullet, speed" in prompt
    overall = build_player_summary_prompt({"games": 5, "by_time_class": {
        "blitz": {"games": 3, "wins": 1, "losses": 2, "draws": 0, "avg_accuracy": 71.0, "blunders_per_game": 1.3,
                  "mistakes_per_game": 1.0},
        "rapid": {"games": 2, "wins": 2, "losses": 0, "draws": 0, "avg_accuracy": 85.5, "blunders_per_game": 0.5,
                  "mistakes_per_game": 0.5}}}, [])
    assert "By time control (verified)" in overall and "rapid: 2 games, 2W/0L/0D, accuracy 85.5%" in overall


def test_refreshing_one_time_controls_review(monkeypatch):
    store.init()
    pid = store.create_profile(name="Sam")
    _save(pid, _pgn("Chess.com", "600"), "rapid")
    _save(pid, _pgn("Chess.com", "900"), "rapid")
    _save(pid, _pgn("Chess.com", "180"), "blitz")
    prompts = []
    fake = SimpleNamespace(available=True, hard=lambda f: f(SimpleNamespace(
        generate=lambda system, prompt: prompts.append(prompt) or "Your rapid games: fine.")))
    monkeypatch.setattr(jobs, "build_coach", lambda cfg: (fake, []))
    assert jobs.refresh_player_summary(pid, "rapid") == ("Your rapid games: fine.", "")
    assert store.get_profile(pid)["summaries"]["rapid"] == "Your rapid games: fine."
    assert store.get_profile(pid)["summary"] in ("", None)                    # the overall one is untouched
    assert "ONLY the player's rapid games (2 of them)" in prompts[0]
    assert jobs.refresh_player_summary(pid, "blitz")[1] == "Analyse at least two blitz games first."


def test_games_from_before_are_reclassified_once():
    store.init()
    pid = store.create_profile(name="Sam")
    gid = _save(pid, _pgn("Chess.com", "1800", elo="1450"), "classical")   # the old, site-blind class
    store.add_ratings(pid, {"chesscom": {"classical": {"rating": 1450, "date": "2026-09-20", "source": "game"}}})
    with sqlite3.connect(store.db_path()) as c:
        c.execute("DELETE FROM kv WHERE key='time_classes_by_site'")
    store._initialised.clear()
    store.init()
    assert store.get_game(gid)["time_class"] == "rapid"
    r = store.get_profile(pid)["ratings"]["chesscom"]
    assert "classical" not in r and r["rapid"]["rating"] == 1450


def test_profile_api_and_shared_page_per_time_control():
    with TestClient(web.app, base_url="http://127.0.0.1:8420") as client:
        client.post("/api/profiles", json={"name": "Sam"}, headers=H)
        pid = store.active_id()
        _save(pid, _pgn("Chess.com", "600"), "rapid")
        _save(pid, _pgn("Chess.com", "180"), "blitz")
        d = client.get("/api/profile?tc=rapid").json()
        assert d["stats"]["games"] == 1 and len(d["games"]) == 2 and d["time_classes"] == {"blitz": 1, "rapid": 1}
        assert client.get("/api/profile?tc=hyperbullet").status_code == 422
        assert client.post("/api/profile/refresh_summary", json={"time_class": "rapid"}, headers=H).status_code == 400
    data = share.export_data(pid)
    assert set(data["stats_by_class"]) == {"blitz", "rapid"} and data["stats_by_class"]["rapid"]["games"] == 1
    assert data["time_classes"] == {"blitz": 1, "rapid": 1} and "summaries" in data["profile"]


def test_the_queue_shows_each_games_time_control():
    job = jobs.Job(_pgn("Chess.com", "600"), side="white")
    assert job.summary(0)["time_class"] == "rapid"
    assert jobs.Job(_pgn("https://lichess.org/abc", "60+0")).summary(0)["time_class"] == "bullet"
