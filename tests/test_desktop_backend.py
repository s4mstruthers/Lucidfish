"""The desktop app's backend contract: desktop mode, the in-app Stockfish download, Learn and focus."""
import io
import json
import re
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

import chess
import pytest
from fastapi.testclient import TestClient

from conftest import STOCKFISH, needs_engine
from lucidfish import __version__, concepts, config, engine_setup, store, web

H = {"X-Lucidfish": "1"}
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client(monkeypatch):
    if STOCKFISH:
        monkeypatch.setenv("LUCIDFISH_STOCKFISH", STOCKFISH)
    monkeypatch.setenv("LUCIDFISH_DEPTH", "8")
    monkeypatch.setenv("LUCIDFISH_THREADS", "1")
    with TestClient(web.app, base_url="http://127.0.0.1:8420") as c:
        c.post("/api/settings", json={"llm_enabled": False}, headers=H)
        yield c


def test_app_info_and_desktop_flag(client, monkeypatch):
    info = client.get("/api/app").json()
    assert info["version"] == __version__ and info["desktop"] is False
    assert info["platform"] in ("linux", "darwin", "win32") and info["arch"]
    assert client.get("/api/health").json()["desktop"] is False
    monkeypatch.setenv("LUCIDFISH_DESKTOP", "1")
    assert client.get("/api/health").json()["desktop"] is True
    assert client.get("/api/app").json()["desktop"] is True


def test_bundled_engine_is_preferred_over_path_but_not_over_the_setting(tmp_path, monkeypatch):
    monkeypatch.delenv("LUCIDFISH_STOCKFISH", raising=False)
    bundled = tmp_path / "stockfish-bundled"
    bundled.write_text("")
    monkeypatch.setenv("LUCIDFISH_BUNDLED_STOCKFISH", str(bundled))
    assert config.find_stockfish() == str(bundled)
    monkeypatch.setenv("LUCIDFISH_STOCKFISH", "/explicit/stockfish")
    assert config.find_stockfish() == "/explicit/stockfish"          # the explicit variable still wins
    monkeypatch.setenv("LUCIDFISH_BUNDLED_STOCKFISH", str(tmp_path / "missing"))
    monkeypatch.delenv("LUCIDFISH_STOCKFISH", raising=False)
    assert config.find_stockfish() != str(tmp_path / "missing")      # a missing bundled file is ignored
    assert config.engine_dir() == config.data_dir() / "engine"


def test_desktop_mode_prints_the_address_and_stops_with_its_parent(tmp_path):
    env = {"LUCIDFISH_DATA_DIR": str(tmp_path / "data"), "PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT),
           "SYSTEMROOT": "C:\\Windows"}
    proc = subprocess.Popen([sys.executable, "-m", "lucidfish", "web", "--desktop", "--port", "8477"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env={k: v for k, v in env.items() if v}, cwd=str(ROOT))
    try:
        url, deadline = None, time.time() + 60
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if line.startswith("LUCIDFISH_URL "):
                url = line.split(" ", 1)[1].strip()
                break
        assert url and re.match(r"^http://127\.0\.0\.1:\d+$", url), url
        proc.stdin.close()            # the launcher went away: the server must stop by itself
        proc.wait(timeout=30)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()


# ---------------------------------------------------------------- Stockfish download

def _fake_archive(name: str, binary: bytes, member: str) -> bytes:
    buf = io.BytesIO()
    if name.endswith(".zip"):
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(f"stockfish/{member}", binary)
    else:
        with tarfile.open(fileobj=buf, mode="w") as t:
            info = tarfile.TarInfo(f"stockfish/{member}")
            info.size = len(binary)
            info.mode = 0o755
            t.addfile(info, io.BytesIO(binary))
    return buf.getvalue()


def test_release_assets_are_named_like_the_official_ones():
    for (system, _arch), assets in engine_setup.ASSETS.items():
        for a in assets:
            assert a.startswith("stockfish-") and a.endswith(".zip" if system == "win32" else ".tar")
            assert engine_setup.RELEASE_URL.startswith("https://github.com/official-stockfish/Stockfish/releases/")
    assert engine_setup.platform_key()[1] in ("x86_64", "arm64") or not engine_setup.available()


def test_archive_members_are_found_and_unsafe_names_ignored():
    tar = _fake_archive("x.tar", b"ENGINE", "stockfish-ubuntu-x86-64-avx2")
    assert engine_setup._member_bytes(tar, "x.tar") == b"ENGINE"
    zipped = _fake_archive("x.zip", b"EXE", "stockfish-windows-x86-64-avx2.exe")
    assert engine_setup._member_bytes(zipped, "x.zip") == b"EXE"
    with pytest.raises(RuntimeError, match="did not contain"):
        engine_setup._member_bytes(_fake_archive("x.tar", b"README", "README.md"), "x.tar")


@needs_engine
def test_download_installs_verifies_and_selects_the_engine(monkeypatch):
    """The whole flow, offline: the 'download' is the locally installed Stockfish packed like a release."""
    monkeypatch.setattr(engine_setup, "platform_key", lambda: ("linux", "x86_64"))
    monkeypatch.setitem(engine_setup.ASSETS, ("linux", "x86_64"),
                        ["stockfish-ubuntu-x86-64-avx512.tar", "stockfish-ubuntu-x86-64-avx2.tar"])
    real = Path(STOCKFISH).read_bytes()
    calls = []

    def fake_download(url, on_progress):
        calls.append(url)
        on_progress(0.5)
        if "avx512" in url:                      # a build this computer can't run: a fallback must follow
            return _fake_archive("x.tar", b"#!/bin/sh\nexit 3\n", "stockfish-ubuntu-x86-64-avx512")
        return _fake_archive("x.tar", real, "stockfish-ubuntu-x86-64-avx2")

    monkeypatch.setattr(engine_setup, "_download", fake_download)
    engine_setup._set(status="idle", progress=0.0, message="", path="", error="")
    engine_setup._run()
    st = engine_setup.status()
    assert st["status"] == "done", st
    assert len(calls) == 2 and "avx512" in calls[0] and "avx2" in calls[1]
    target = Path(st["path"])
    assert target == config.engine_dir() / "stockfish" and target.stat().st_size == len(real)
    assert "Stockfish" in st["message"]
    assert store.get_settings()["stockfish_path"] == str(target)   # selected for the next analysis


def test_download_endpoint(client, monkeypatch):
    assert client.get("/api/engine/download").json()["status"] in ("idle", "done", "error")
    monkeypatch.setattr(engine_setup, "available", lambda: False)
    r = client.post("/api/engine/download", headers=H)
    assert r.status_code == 400 and "no official Stockfish download" in r.json()["error"]
    monkeypatch.setattr(engine_setup, "available", lambda: True)
    started = {}
    monkeypatch.setattr(engine_setup, "start", lambda: started.setdefault("ok", {"status": "starting"}))
    assert client.post("/api/engine/download", headers=H).json() == {"status": "starting"}


# ---------------------------------------------------------------- Learn

def _move(ply, side, san, uci, cls, fen_before, **extra):
    return {"ply": ply, "n": ply // 2 + 1, "side": side, "san": san, "uci": uci, "cls": cls, "cp_loss": 0,
            "fen_before": fen_before, "fen_after": fen_before, "acc": 60, "win": 30, "tags": [], **extra}


def test_concept_library_is_consistent():
    ids = [c.id for c in concepts.CONCEPTS]
    assert len(ids) == len(set(ids)) >= 15
    cats = {c[0] for c in concepts.CATEGORIES}
    for c in concepts.CONCEPTS:
        assert c.category in cats and c.question.endswith("?") and c.summary and c.why and c.spot and c.do
        for r in c.related:
            assert r in ids, f"{c.id} relates to unknown {r}"
    # Every training theme is explained by at least one concept, so a puzzle can always link to Learn.
    from lucidfish.training import THEMES
    for theme in THEMES:
        assert any(theme in c.themes for c in concepts.CONCEPTS), theme


def test_learn_overview_links_the_players_mistakes_to_concepts():
    store.init()
    pid = store.create_profile(name="Ann")
    assert concepts.overview(pid)["focus"] == []                         # nothing before the first game
    start = chess.STARTING_FEN
    moves = [
        _move(0, "White", "a3", "a2a3", "blunder", start, best="e4", best_uci="e2e4", threat="…Qh4#",
              tags=["missed threat", "hangs material"],
              candidates=[{"san": "e4", "uci": "e2e4", "cp": 30, "score": "+0.30", "line": "1. e4", "steps": []}]),
        _move(1, "Black", "e5", "e7e5", "good", "rnbqkbnr/pppppppp/8/8/8/P7/1PPPPPPP/RNBQKBNR b KQkq - 0 1"),
    ]
    pgn = '[White "Ann"]\n[Black "Bob"]\n[Result "0-1"]\n\n1. a3 e5 0-1\n'
    store.save_game(pid, pgn, {"White": "Ann", "Black": "Bob", "Result": "0-1"}, "white", None, "", "", moves)
    data = concepts.overview(pid)
    by_id = {c["id"]: c for c in data["concepts"]}
    assert by_id["what-does-it-threaten"]["puzzles"] == 1 and by_id["hanging-pieces"]["puzzles"] == 1
    assert by_id["what-does-it-threaten"]["examples"][0]["played"] == "a3"
    assert by_id["forks"]["puzzles"] == 0
    assert data["focus"] and data["focus"][0]["id"] in ("what-does-it-threaten", "hanging-pieces",
                                                        "checks-captures-threats")
    assert "1 of your puzzles" in data["focus"][0]["reason"]
    assert json.dumps(data)                                              # serialisable for the API
    assert concepts.overview(pid, "bullet")["concepts"][0]["puzzles"] == 0   # the game has no time control


def test_learn_endpoint(client):
    d = client.get("/api/learn").json()
    assert d["games"] == 0 and d["focus"] == [] and len(d["concepts"]) == len(concepts.CONCEPTS)
    assert client.get("/api/learn?tc=blitz").status_code == 200
    assert client.get("/api/learn?tc=nope").status_code == 422


# ---------------------------------------------------------------- the desktop app's configuration

def test_desktop_versions_agree_with_the_package():
    sys.path.insert(0, str(ROOT / "desktop" / "scripts"))
    import check_version
    assert set(check_version.versions().values()) == {__version__}
    assert check_version.main([f"v{__version__}"]) == 0
    assert check_version.main(["v0.0.0"]) == 1


def test_desktop_configuration_points_at_the_backend_and_the_releases():
    conf = json.loads((ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    assert conf["bundle"]["externalBin"] == ["binaries/lucidfish-server"]
    assert conf["bundle"]["createUpdaterArtifacts"] is True
    updater = conf["plugins"]["updater"]
    assert updater["endpoints"] == ["https://github.com/s4mstruthers/Lucidfish/releases/latest/download/latest.json"]
    assert updater["pubkey"] and "__" not in updater["pubkey"]          # a real minisign public key
    assert conf["app"]["withGlobalTauri"] is True                       # the page calls the shell
    caps = json.loads((ROOT / "desktop" / "src-tauri" / "capabilities" / "default.json").read_text(encoding="utf-8"))
    assert "http://127.0.0.1:*" in caps["remote"]["urls"] and "updater:default" in caps["permissions"]
    # The page served to the desktop shell lets its IPC through the content security policy.
    assert "ipc:" in web._CSP.format(ipc=" ipc: http://ipc.localhost")
