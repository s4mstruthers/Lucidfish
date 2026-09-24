import time

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE_PGN, STOCKFISH, fast_config, needs_engine
from lucidfish import jobs, store, web

H = {"X-Lucidfish": "1"}
SHORT_PGN = '[White "A"]\n[Black "B"]\n[Result "1-0"]\n\n1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0\n'
MINIATURE = '[White "C"]\n[Black "D"]\n[Result "0-1"]\n\n1. f3 e5 2. g4 Qh4# 0-1\n'


def _wait(pred, timeout=120.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_timings_learn_from_real_runs():
    store.init()
    t = jobs.Timings()
    cfg = fast_config()
    per, review, learned = t.estimate(cfg)
    assert per > 0 and review == 0 and not learned      # engine only: no review to wait for
    t.record(cfg, plies=40, move_seconds=20.0, review_seconds=0.0)
    assert t.estimate(cfg) == (0.5, 0.0, True)
    t.record(cfg, plies=40, move_seconds=40.0, review_seconds=0.0)
    assert 0.5 < t.estimate(cfg)[0] < 1.0               # running average, not the last value
    assert jobs.Timings().estimate(cfg)[2]              # persisted across instances


def test_job_time_left_blends_estimate_with_live_speed():
    job = jobs.Job(SAMPLE_PGN)
    job.per_ply, job.review_s, job.coach_on = 1.0, 10.0, True
    assert job.remaining(time.time()) == pytest.approx(33 + 10)
    job.status, job.started = "running", 1000.0
    job.moves = [{}] * 12                                # 12 plies in 6 s: twice as fast as guessed
    assert job.remaining(1006.0) == pytest.approx((33 - 12) * 0.5 + 10)


def test_queue_order_and_management_without_running(monkeypatch):
    store.init()
    q = jobs.AnalysisQueue()
    q.paused = True                                      # keep everything waiting
    a, b, c = jobs.Job(SAMPLE_PGN), jobs.Job(SHORT_PGN), jobs.Job(MINIATURE)
    q.add([a, b, c])
    assert [j["id"] for j in q.snapshot()["queued"]] == [a.id, b.id, c.id]
    q.move(c.id, "top")
    q.move(a.id, "down")
    snap = q.snapshot()
    assert [j["id"] for j in snap["queued"]] == [c.id, b.id, a.id]
    starts = [j["start_in_s"] for j in snap["queued"]]
    assert starts == sorted(starts) and starts[0] == 0  # each game starts when the one before ends
    assert snap["totals"]["games_left"] == 3 and snap["totals"]["seconds_left"] > 0
    assert q.find_pending(SHORT_PGN) is b
    assert q.cancel(b.id) and q.snapshot()["finished"][0]["status"] == "cancelled"
    assert q.find_pending(SHORT_PGN) is None
    view = q.job_view(a.id)
    assert view["position"] == 2 and view["start_in_s"] > 0
    # The queue survives a restart, paused so nothing starts by surprise.
    q2 = jobs.AnalysisQueue()
    assert q2.restore() == 2 and q2.paused and q2.restored == 2
    q2.stop_all()
    assert q2.snapshot()["totals"]["games_left"] == 0
    assert jobs.AnalysisQueue().restore() == 0


@pytest.fixture
def client(monkeypatch):
    if STOCKFISH:
        monkeypatch.setenv("LUCIDFISH_STOCKFISH", STOCKFISH)
    monkeypatch.setenv("LUCIDFISH_DEPTH", "8")
    monkeypatch.setenv("LUCIDFISH_THREADS", "1")
    with TestClient(web.app, base_url="http://127.0.0.1:8420") as c:
        c.post("/api/settings", json={"llm_enabled": False}, headers=H)
        c.post("/api/profiles", json={"name": "Queue"}, headers=H)
        yield c


@needs_engine
def test_batch_queue_runs_in_order_skips_duplicates_and_saves(client):
    r = client.post("/api/queue", json={"games": [{"pgn": SHORT_PGN, "side": "white"},
                                                  {"pgn": MINIATURE, "side": "black"},
                                                  {"pgn": SHORT_PGN, "side": "white"}]}, headers=H).json()
    assert r == {"added": 2, "skipped": 1, "errors": []}
    seen_progress = []

    def finished():
        snap = client.get("/api/queue").json()
        seen_progress.append(snap["totals"]["progress"])
        return snap["totals"]["games_left"] == 0

    assert _wait(finished)
    snap = client.get("/api/queue").json()
    assert [j["status"] for j in snap["finished"]] == ["done", "done"]
    assert snap["totals"]["progress"] == 1 and seen_progress == sorted(seen_progress)
    assert len(client.get("/api/profile").json()["games"]) == 2
    # Already analysed: skipped unless forced.
    again = client.post("/api/queue", json={"games": [{"pgn": SHORT_PGN}]}, headers=H).json()
    assert again["added"] == 0 and again["skipped"] == 1
    assert client.post("/api/queue/clear_finished", headers=H).json()["finished"] == []


@needs_engine
def test_single_analysis_jumps_the_queue(client):
    client.post("/api/queue/pause", headers=H)
    client.post("/api/queue", json={"games": [{"pgn": SHORT_PGN}, {"pgn": MINIATURE}]}, headers=H)
    job = client.post("/api/analyse", json={"pgn": SAMPLE_PGN, "side": "white"}, headers=H).json()["job_id"]
    view = client.get(f"/api/job/{job}").json()
    assert view["status"] == "queued" and view["position"] == 1 and view["paused"]
    # Opening a game that is already queued brings that job forward instead of adding a copy.
    dup = client.post("/api/analyse", json={"pgn": MINIATURE}, headers=H).json()["job_id"]
    assert client.get(f"/api/job/{dup}").json()["position"] == 1
    assert len(client.get("/api/queue").json()["queued"]) == 3
    client.post("/api/queue/resume", headers=H)
    assert _wait(lambda: client.get(f"/api/job/{job}").json()["status"] == "done")
    client.post("/api/queue/stop_all", headers=H)


@needs_engine
def test_reanalyse_a_stored_game_replaces_it(client):
    job = client.post("/api/analyse", json={"pgn": SHORT_PGN, "side": "white"}, headers=H).json()["job_id"]
    assert _wait(lambda: client.get(f"/api/job/{job}").json()["status"] == "done")
    old_id = client.get("/api/profile").json()["games"][0]["id"]
    again = client.post(f"/api/profile/game/{old_id}/reanalyse", json={"detail": "key"}, headers=H).json()["job_id"]
    assert _wait(lambda: client.get(f"/api/job/{again}").json()["status"] == "done")
    games = client.get("/api/profile").json()["games"]
    assert len(games) == 1 and games[0]["white"] == "A"          # replaced, not duplicated
    assert client.post("/api/profile/game/999999/reanalyse", headers=H).status_code == 404


@needs_engine
def test_done_is_reported_only_after_the_game_is_saved(client, monkeypatch):
    real_save = store.save_game

    def slow_save(*args, **kwargs):
        time.sleep(0.6)            # widen the window a page poll could fall into
        return real_save(*args, **kwargs)

    monkeypatch.setattr(store, "save_game", slow_save)
    job = client.post("/api/analyse", json={"pgn": SHORT_PGN, "side": "white"}, headers=H).json()["job_id"]
    deadline = time.time() + 120
    while time.time() < deadline:
        view = client.get(f"/api/job/{job}").json()
        if view["status"] == "done":
            break
        time.sleep(0.05)
    assert view["status"] == "done"
    assert view["game_id"], "reported done before the game was saved"
    assert client.get("/api/profile").json()["games"]
