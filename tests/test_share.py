"""Sharing a profile as a single web page, and keeping a synced copy up to date."""
import base64
import json
import re
import time

import chess
import pytest
from fastapi.testclient import TestClient

from conftest import STOCKFISH, needs_engine
from lucidfish import cli, share, store, training, web

H = {"X-Lucidfish": "1"}
SHORT_PGN = '[White "Ann"]\n[Black "Bob"]\n[Result "1-0"]\n\n1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0\n'


def _move(ply, side, san, uci, cls, fen_before, **extra):
    return {"ply": ply, "n": ply // 2 + 1, "side": side, "san": san, "uci": uci, "cls": cls, "cp_loss": 0,
            "fen_before": fen_before, "fen_after": fen_before, "acc": 90, "win": 50, "tags": [], **extra}


def _profile_with_game(name="Ann"):
    store.init()
    pid = store.create_profile(name=name)
    start = chess.STARTING_FEN
    moves = [
        _move(0, "White", "a3", "a2a3", "mistake", start, expl="A note with </script><script>alert(1)</script>.",
              best="e4", best_uci="e2e4", win=40.0,
              candidates=[{"san": "e4", "uci": "e2e4", "cp": 30, "score": "+0.30", "line": "1. e4", "steps": []}]),
        _move(1, "Black", "e5", "e7e5", "blunder", "rnbqkbnr/pppppppp/8/8/8/P7/1PPPPPPP/RNBQKBNR b KQkq - 0 1"),
    ]
    gid = store.save_game(pid, SHORT_PGN, {"White": "Ann", "Black": "Bob", "Result": "1-0"}, "white", None, "",
                          "## Summary\nGood.", moves, chapters=[{"start": 0, "end": 1, "range": "1–1",
                                                                  "title": "Start", "summary": "It began."}])
    store.set_summary(pid, "Ann plays actively.")
    return pid, gid


def _page_data(html: str) -> dict:
    raw = re.search(r"window\.LUCIDFISH_EXPORT = (.*?);</script>", html, re.S).group(1)
    return json.loads(raw)


def test_page_is_self_contained_and_carries_the_profile():
    pid, gid = _profile_with_game()
    html = share.build_html(pid)
    assert "<title>Ann's chess games — Lucidfish</title>" in html
    assert 'src="/static/' not in html and 'href="/static/' not in html     # every asset inlined
    assert "</script><script>alert(1)" not in html                          # notes can't break out of the data
    data = _page_data(html)
    assert data["profile"]["name"] == "Ann" and data["profile"]["summary"] == "Ann plays actively."
    assert [g["id"] for g in data["games"]] == [gid]
    assert data["stats"]["games"] == 1 and set(data["pieces"]) == {c + p for c in "wb" for p in "KQRBNP"}
    game = data["details"][str(gid)]
    assert game["chapters"][0]["title"] == "Start" and game["review"].startswith("## Summary")
    assert game["moves"][0]["expl"].startswith("A note with </script>")     # decoded back intact
    assert "window.Chess = module.exports.Chess" in html                      # chess.js: legal moves, SAN
    # The player's own mistakes are the Train page's puzzles (the opponent's blunder is not).
    assert [(t["played"], t["best"], t["label"]) for t in data["train"]] == [("a3", "e4", "1.")]


def _engine_wasm(html: str) -> bytes:
    raw = re.search(r'<script type="text/plain" id="lfEngineWasm">(.*?)</script>', html, re.S).group(1)
    return base64.b64decode(raw)


def test_page_carries_the_engine_unless_left_out():
    pid, _ = _profile_with_game()
    full, small = share.build_html(pid), share.build_html(pid, engine=False)
    wasm = (share.STATIC / share.ENGINE_WASM).read_bytes()
    assert _engine_wasm(full) == wasm                                         # the WebAssembly survives intact
    loader = re.search(r'<script type="text/plain" id="lfEngineJs">(.*?)</script>', full, re.S).group(1)
    assert loader == (share.STATIC / share.ENGINE_JS).read_text(encoding="utf-8")
    assert full.index('id="lfEngineWasm"') < full.index("window.LUCIDFISH_EXPORT")   # there before the app starts
    assert 'id="lfEngine' not in small and len(full) - len(small) > 9_000_000
    assert _page_data(small)["train"] == _page_data(full)["train"]


def test_training_puzzles():
    pid, gid = _profile_with_game()
    items = training.train_items(pid)
    assert len(items) == 1
    item = items[0]
    fingerprint = store.list_games(pid)[0]["fingerprint"]
    assert item["id"] == f"{fingerprint}:0" and item["game_id"] == gid and item["i"] == 0
    assert item["fen"] == chess.STARTING_FEN and item["side"] == "white" and item["opponent"] == "Bob"
    assert item["cls"] == "mistake" and item["loss"] > 10                    # 30 cp ≈ 53% before, 40% after
    # A game without a known side gives no puzzles (its errors could be the opponent's).
    store.save_game(pid, SHORT_PGN.replace("Ann", "Cy"), {"White": "Cy", "Black": "Bob"}, None, None, "", "",
                    [_move(0, "White", "a3", "a2a3", "blunder", chess.STARTING_FEN, best_uci="e2e4")])
    assert len(training.train_items(pid)) == 1


def test_folder_sync_is_atomic_and_follows_the_setting(tmp_path):
    pid, _ = _profile_with_game()
    target_dir = tmp_path / "Shared" / "Lucidfish"
    (tmp_path / "Shared").mkdir()
    cfg = share.configure(pid, str(target_dir), auto=True)                   # the last level is created
    assert cfg["folder"] == str(target_dir) and target_dir.is_dir()
    share.sync_profile(pid)
    page = target_dir / "Lucidfish - Ann.html"
    assert page.exists() and "LUCIDFISH_EXPORT" in page.read_text(encoding="utf-8")
    assert [p.name for p in target_dir.iterdir()] == [page.name]             # no temporary files left behind
    status = share.get_config(pid)
    assert status["file"] == str(page) and status["last_synced"] and not status["last_error"]
    # Automatic updates off: nothing is written.
    share.configure(pid, str(target_dir), auto=False)
    page.unlink()
    share.sync_profile(pid)
    assert not page.exists()
    # A folder that disappeared is reported, not raised.
    share.configure(pid, str(target_dir), auto=True)
    target_dir.rmdir()
    share.sync_profile(pid)
    assert "no folder" in share.get_config(pid)["last_error"]


def test_folder_validation(tmp_path):
    pid, _ = _profile_with_game()
    with pytest.raises(ValueError, match="full path"):
        share.configure(pid, "relative/folder", True)
    with pytest.raises(ValueError, match="no folder"):
        share.configure(pid, str(tmp_path / "missing" / "deeper"), True)
    assert share.configure(pid, "", True) == {**share.get_config(pid), "folder": "", "auto": False}


def test_share_command_writes_the_page(tmp_path):
    _profile_with_game(name="Ann Marie")
    assert cli.main(["share", "--profile", "ann marie", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "Lucidfish - Ann Marie.html").exists()
    assert cli.main(["share", "--profile", "nobody", "--out", str(tmp_path)]) == 1


@pytest.fixture
def client(monkeypatch):
    if STOCKFISH:
        monkeypatch.setenv("LUCIDFISH_STOCKFISH", STOCKFISH)
    monkeypatch.setenv("LUCIDFISH_DEPTH", "8")
    monkeypatch.setenv("LUCIDFISH_THREADS", "1")
    with TestClient(web.app, base_url="http://127.0.0.1:8420") as c:
        c.post("/api/settings", json={"llm_enabled": False}, headers=H)
        yield c


def test_share_api(client, tmp_path):
    assert client.get("/api/share").status_code == 400                       # no profile yet
    client.post("/api/profiles", json={"name": "Ann"}, headers=H)
    status = client.get("/api/share").json()
    assert status["file_name"] == "Lucidfish - Ann.html" and status["folder"] == ""
    assert isinstance(status["suggestions"], list)
    assert client.post("/api/share", json={"folder": str(tmp_path)}).status_code == 403    # CSRF guard
    written = client.post("/api/share", json={"folder": str(tmp_path), "auto": True}, headers=H).json()
    assert written["last_synced"] and (tmp_path / "Lucidfish - Ann.html").exists()
    assert client.post("/api/share", json={"folder": "nope"}, headers=H).status_code == 400
    assert client.post("/api/share/sync", headers=H).json()["last_error"] == ""
    download = client.get("/api/share/download")
    assert download.status_code == 200 and "LUCIDFISH_EXPORT" in download.text and 'id="lfEngineWasm"' in download.text
    assert "Lucidfish%20-%20Ann.html" in download.headers["content-disposition"]
    assert 'id="lfEngineWasm"' not in client.get("/api/share/download?engine=false").text
    # The folder copy follows the engine setting too.
    small = client.post("/api/share", json={"folder": str(tmp_path), "engine": False}, headers=H).json()
    assert small["engine"] is False
    assert 'id="lfEngineWasm"' not in (tmp_path / "Lucidfish - Ann.html").read_text(encoding="utf-8")
    assert client.get("/api/share").json()["engine"] is False


def test_the_app_can_run_the_engine_in_the_browser(client):
    csp = client.get("/").headers["content-security-policy"]
    assert "'wasm-unsafe-eval'" in csp and "'unsafe-eval'" not in csp.replace("'wasm-unsafe-eval'", "")
    wasm = client.get("/static/vendor/stockfish-18-lite-single.wasm")
    assert wasm.status_code == 200 and wasm.headers["content-type"] == "application/wasm"
    worker = client.get("/static/vendor/stockfish-18-lite-single.js")
    assert "'wasm-unsafe-eval'" in worker.headers["content-security-policy"]   # a worker gets its script's CSP
    assert client.get("/static/vendor/chess-1.4.0.js").status_code == 200
    assert client.get("/api/train").json() == {"items": []}                   # no profile yet


@needs_engine
def test_a_finished_analysis_updates_the_shared_copy(client, tmp_path):
    client.post("/api/profiles", json={"name": "Ann"}, headers=H)
    client.post("/api/share", json={"folder": str(tmp_path), "auto": True}, headers=H)
    page = tmp_path / "Lucidfish - Ann.html"
    assert _page_data(page.read_text(encoding="utf-8"))["games"] == []
    job = client.post("/api/analyse", json={"pgn": SHORT_PGN, "side": "white"}, headers=H).json()["job_id"]
    deadline = time.time() + 120
    while time.time() < deadline and client.get(f"/api/job/{job}").json()["status"] != "done":
        time.sleep(0.2)
    data = _page_data(page.read_text(encoding="utf-8"))
    assert len(data["games"]) == 1 and data["stats"]["games"] == 1
    # Deleting the game updates the copy too.
    client.delete(f"/api/profile/game/{data['games'][0]['id']}", headers=H)
    assert _page_data(page.read_text(encoding="utf-8"))["games"] == []
