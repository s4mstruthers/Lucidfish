"""Ratings per site: each game's own rating, and the profile's newest rating on each site."""
import sqlite3

import chess
from fastapi.testclient import TestClient

from lucidfish import ratings, store, web
from lucidfish.jobs import rating_for_game
from lucidfish.prompts import build_player_summary_prompt, elo_guidance

H = {"X-Lucidfish": "1"}
CHESSCOM = {"Site": "Chess.com", "Date": "2026.09.20", "WhiteElo": "1432", "BlackElo": "1418",
            "TimeControl": "180+2", "Link": "https://www.chess.com/game/live/123"}
LICHESS = {"Site": "https://lichess.org/AbCdEfGh", "UTCDate": "2026.09.22", "WhiteElo": "1705", "BlackElo": "1690",
           "TimeControl": "600+0"}


def _pgn(headers: dict, moves: str = "1. e4 e5 *") -> str:
    return "".join(f'[{k} "{v}"]\n' for k, v in headers.items()) + "\n" + moves + "\n"


def _move():
    return {"ply": 0, "n": 1, "side": "White", "san": "e4", "uci": "e2e4", "cls": "best", "cp_loss": 0,
            "fen_before": chess.STARTING_FEN, "fen_after": chess.STARTING_FEN, "acc": 100, "win": 50, "tags": []}


def test_a_games_own_rating_and_site():
    assert ratings.game_site(CHESSCOM) == "chesscom" and ratings.game_site(LICHESS) == "lichess"
    assert ratings.game_site({"Site": "Wijk aan Zee NED"}) == "other"
    assert ratings.game_rating(CHESSCOM, "white") == 1432 and ratings.game_rating(CHESSCOM, "Black") == 1418
    assert ratings.game_rating(CHESSCOM, None) is None                       # unknown side: nobody's rating
    assert ratings.game_rating({"WhiteElo": "?"}, "white") is None
    assert ratings.game_date(LICHESS) == "2026-09-22" and ratings.game_date({"Date": "????.??.??"}) == ""
    assert ratings.pgn_headers(_pgn(CHESSCOM))["BlackElo"] == "1418"


def test_the_newest_rating_per_site_and_time_control_wins():
    r: dict = {}
    assert ratings.from_game(r, CHESSCOM, "white", "blitz")
    assert not ratings.from_game(r, {**CHESSCOM, "Date": "2026.08.01", "WhiteElo": "1300"}, "white", "blitz")
    assert ratings.from_game(r, {**CHESSCOM, "Date": "2026.09.23", "WhiteElo": "1450"}, "white", "blitz")
    assert ratings.from_game(r, LICHESS, "black", "rapid")
    assert r["chesscom"]["blitz"]["rating"] == 1450 and r["lichess"]["rapid"]["rating"] == 1690   # never mixed
    assert ratings.summary(r) == "chess.com: blitz 1450; Lichess: rapid 1690"
    assert ratings.newest(r) == (1450, "chess.com blitz")


def test_stand_in_for_a_game_without_a_rating():
    r: dict = {}
    ratings.record(r, "chesscom", "blitz", 1400, "2026-09-01", "game")
    ratings.record(r, "chesscom", "rapid", 1500, "2026-09-01", "game")
    ratings.record(r, "lichess", "bullet", 1650, "2026-09-01", "game")
    assert ratings.for_game(r, "chesscom", "rapid") == (1500, "chess.com rapid")
    assert ratings.for_game(r, "lichess", "blitz") == (1400, "chess.com blitz")      # same time control elsewhere
    assert ratings.for_game(r, "lichess", "classical") == (1650, "Lichess bullet")   # nearest on the same site
    assert ratings.for_game({}, "chesscom", "blitz") is None


def test_ratings_typed_into_older_versions_are_kept():
    old = {"chesscom_user": "sam", "elo_blitz": 1320, "elo_rapid": 1450, "elo_bullet": None}
    r = ratings.from_legacy(old)
    assert r == {"chesscom": {"blitz": {"rating": 1320, "date": "", "source": "entered"},
                              "rapid": {"rating": 1450, "date": "", "source": "entered"}}}
    assert ratings.from_game(r, CHESSCOM, "white", "blitz")                  # any dated game rating replaces it
    assert "other" in ratings.from_legacy({"elo_rapid": 1500})


def test_looked_up_ratings_are_validated():
    found = ratings.clean_lookup({"chesscom": {"blitz": 1410, "rapid": True, "daily": 50},
                                  "lichess": {"rapid": 1700, "puzzle": 2000}, "fide": {"rapid": 1800}},
                                 today="2026-09-24")
    assert found == {"chesscom": {"blitz": {"rating": 1410, "date": "2026-09-24", "source": "lookup"}},
                     "lichess": {"rapid": {"rating": 1700, "date": "2026-09-24", "source": "lookup"}}}


def test_saving_a_game_updates_the_profile():
    store.init()
    pid = store.create_profile(name="Sam", chesscom_user="sam")
    store.save_game(pid, _pgn(CHESSCOM), CHESSCOM, "white", 1432, "", "", [_move()], time_class="blitz")
    assert store.get_profile(pid)["ratings"]["chesscom"]["blitz"]["rating"] == 1432
    older = {**CHESSCOM, "Date": "2026.01.01", "WhiteElo": "1200"}
    store.save_game(pid, _pgn(older, "1. d4 *"), older, "white", 1200, "", "", [_move()], time_class="blitz")
    assert store.get_profile(pid)["ratings"]["chesscom"]["blitz"]["rating"] == 1432   # an older game doesn't win
    assert store.list_profiles()[0]["ratings"]["chesscom"]["blitz"]["rating"] == 1432
    assert "elo_blitz" not in store.get_profile(pid)


def test_existing_profiles_are_backfilled_once():
    store.init()
    pid = store.create_profile(name="Sam", chesscom_user="sam")
    store.save_game(pid, _pgn(LICHESS), LICHESS, "white", None, "", "", [_move()], time_class="rapid")
    with sqlite3.connect(store.db_path()) as c:   # as an older version left it
        c.execute("UPDATE profiles SET ratings_json=NULL, elo_blitz=1320 WHERE id=?", (pid,))
    store._initialised.clear()
    store.init()
    r = store.get_profile(pid)["ratings"]
    assert r["chesscom"]["blitz"]["rating"] == 1320 and r["lichess"]["rapid"]["rating"] == 1705


def test_which_rating_a_game_is_coached_at():
    profile = {"ratings": {"chesscom": {"rapid": {"rating": 1500, "date": "2026-09-01", "source": "game"}}}}
    assert rating_for_game(_pgn(CHESSCOM), "black", 999, profile) == (1418, "chess.com blitz")   # its own first
    assert rating_for_game(_pgn({"Site": "Chess.com", "TimeControl": "600"}), "white", 1480, profile) \
        == (1480, "chess.com rapid")                                           # then the one the list gave
    assert rating_for_game(_pgn({"Event": "Club night", "TimeControl": "5400+30"}), "white", None, profile) \
        == (1500, "chess.com rapid")                                           # then the profile's closest
    assert rating_for_game(_pgn({"Event": "Club night"}), "white", None, None) == (None, "")


def test_the_coach_is_told_which_site_a_rating_is_from():
    text = elo_guidance(1705, "Lichess rapid")
    assert "about 1705 (Lichess rapid)" in text and "higher on Lichess" in text
    assert "(" not in elo_guidance(1500).split(".")[0]
    prompt = build_player_summary_prompt({"games": 1}, [], {"name": "Sam", "ratings": {
        "chesscom": {"blitz": {"rating": 1410, "date": "", "source": "game"}}}})
    assert "Actual ratings: chess.com: blitz 1410." in prompt


def test_profile_api_saves_looked_up_ratings():
    with TestClient(web.app, base_url="http://127.0.0.1:8420") as client:
        client.post("/api/profiles", json={"name": "Sam", "lookup": {"chesscom": {"blitz": 1410}}}, headers=H)
        p = client.get("/api/profiles").json()["profiles"][0]
        assert p["ratings"]["chesscom"]["blitz"]["rating"] == 1410 and p["ratings"]["chesscom"]["blitz"]["source"] \
            == "lookup"
        client.post(f"/api/profiles/{p['id']}", json={"name": "Sam", "lookup": {"lichess": {"rapid": 1720}}}, headers=H)
        r = client.get("/api/profile").json()["profile"]["ratings"]
        assert r["chesscom"]["blitz"]["rating"] == 1410 and r["lichess"]["rapid"]["rating"] == 1720
