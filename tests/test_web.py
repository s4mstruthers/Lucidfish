import time

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE_PGN, STOCKFISH, needs_engine
from lucidfish import web

H = {"X-Lucidfish": "1"}


@pytest.fixture
def client(monkeypatch):
    if STOCKFISH:
        monkeypatch.setenv("LUCIDFISH_STOCKFISH", STOCKFISH)
    monkeypatch.setenv("LUCIDFISH_DEPTH", "8")
    monkeypatch.setenv("LUCIDFISH_THREADS", "1")
    with TestClient(web.app, base_url="http://127.0.0.1:8420") as c:
        c.post("/api/settings", json={"llm_enabled": False}, headers=H)
        yield c


def test_security_guards(client):
    assert client.get("/api/profiles", headers={"host": "attacker.example"}).status_code == 403
    assert client.post("/api/profiles", json={"name": "x"}).status_code == 403            # no CSRF header
    assert client.post("/api/profiles", json={"name": "x"},
                       headers={**H, "origin": "https://attacker.example"}).status_code == 403
    page = client.get("/")
    assert page.status_code == 200 and "script-src 'self'" in page.headers["content-security-policy"]


def test_pieces_are_served_locally(client):
    r = client.get("/pieces/bN.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert client.get("/pieces/zz.svg").status_code == 404


def test_settings_validation_and_keys(client):
    assert client.post("/api/settings", json={"depth": 99}, headers=H).status_code == 400
    assert client.post("/api/settings", json={"provider": "nope"}, headers=H).status_code == 400
    s = client.post("/api/settings", json={"provider": "openai", "model": "gpt-4.1-mini", "detail": "key"},
                    headers=H).json()
    assert s["provider"] == "openai" and s["model"] == "gpt-4.1-mini" and s["detail"] == "key"
    r = client.post("/api/settings/key", json={"provider": "openai", "key": "sk-test-abcdef123456"}, headers=H)
    assert r.json()["stored"] == "session"
    everything = client.get("/api/settings").text + client.get("/api/health").text
    assert "sk-test-abcdef123456" not in everything
    assert client.delete("/api/settings/key/openai", headers=H).json()["key"]["present"] is False


def test_bad_inputs_get_readable_errors(client):
    assert "error" in client.post("/api/analyze", json={"pgn": "hello"}, headers=H).json()
    r = client.post("/api/position", json={"fen": "8/8/8/8/8/8/8/8 w - - 0 1"}, headers=H)
    assert r.status_code == 400 and "needs a king" in r.json()["error"]


@needs_engine
def test_analysis_job_streams_moves_and_saves(client):
    client.post("/api/profiles", json={"name": "Web"}, headers=H)
    job = client.post("/api/analyze", json={"pgn": SAMPLE_PGN, "side": "white"}, headers=H).json()["job_id"]
    moves, deadline = [], time.time() + 120
    while time.time() < deadline:
        snap = client.get(f"/api/job/{job}", params={"since": len(moves)}).json()
        moves += snap["moves"]
        if snap["status"] in ("done", "error", "stopped"):
            break
        time.sleep(0.2)
    assert snap["status"] == "done", snap["error"]
    assert [m["ply"] for m in moves] == list(range(33))
    assert snap["game_id"]
    export = client.post("/api/export", json={"format": "pgn", "pgn": SAMPLE_PGN, "headers": snap["headers"],
                                              "moves": moves}, headers=H)
    assert export.status_code == 200 and "Annotator" in export.text
    games = client.get("/api/profile").json()["games"]
    assert len(games) == 1 and games[0]["white"] == "Paul Morphy"
