"""Share a profile as a single web page that needs nothing installed.

The page is the normal Lucidfish app (same HTML, CSS and JavaScript) with the
profile's analysed games, review and statistics built into it. It opens in any
web browser, works offline, and needs nothing installed, no AI and no API key:
the analysis was done on the computer that made it.

What the viewer can do: browse the games, read the commentary, chapters, notes
and review, see the dashboard, draw on the board (kept in their browser),
practise their mistakes, train on puzzles from all their games with spaced
repetition, and explore any position by moving the pieces.

By default the page also carries Stockfish compiled to WebAssembly (about 10 MB),
which runs inside the browser: it judges any practice move and analyses the
positions explored. Without it (engine=False) the page is much smaller, and
practice moves are judged against the engine's top moves stored with the games.

Sync: a profile can keep an up-to-date copy in a folder (for example one shared
through iCloud Drive, Google Drive or Dropbox). The file is rewritten after each
analysis of that profile, atomically, so a sync client never sees half a file.
"""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import chess
import chess.svg

from . import __version__, store, training

STATIC = Path(__file__).parent / "static"
_SHARE_KEY = "share"
_lock = threading.Lock()


# ------------------------------------------------------------------ data

def _pieces() -> dict[str, str]:
    out = {}
    for color in "wb":
        for piece in "KQRBNP":
            svg = chess.svg.piece(chess.Piece.from_symbol(piece if color == "w" else piece.lower()))
            out[color + piece] = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
    return out


def _game_detail(g: dict) -> dict:
    return {
        "id": g["id"], "pgn": g["pgn"],
        "headers": {"White": g["white"], "Black": g["black"], "Result": g["result"], "Date": g["date"]},
        "moves": g["moves"], "review": g["review"], "side": g["user_side"], "opening": g["opening"],
        "accuracy": {g["user_side"]: g["accuracy"]} if g.get("user_side") and g.get("accuracy") else {},
        "chapters": g.get("chapters", []), "annotations": g.get("annotations", {}),
    }


def export_data(profile_id: int) -> dict:
    """Everything the shared page shows, as one JSON-serialisable document."""
    profile = store.get_profile(profile_id)
    if profile is None:
        raise ValueError("unknown profile")
    games = store.list_games(profile_id)
    details = {}
    for row in games:
        g = store.get_game(row["id"])
        if g is not None:
            details[str(row["id"])] = _game_detail(g)
    public = {k: profile.get(k) for k in ("id", "name", "level", "chesscom_user", "lichess_user",
                                           "elo_bullet", "elo_blitz", "elo_rapid", "summary")}
    public["games"] = len(games)
    return {
        "version": __version__,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "profile": public,
        "stats": store.aggregate_stats(profile_id, games),
        "games": games,
        "details": details,
        "train": training.train_items(profile_id),
        "pieces": _pieces(),
    }


# ------------------------------------------------------------------ page

def _inline_script(code: str) -> str:
    return "<script>\n" + code.replace("</script", "<\\/script") + "\n</script>"


def _json_for_script(data: dict) -> str:
    # ensure_ascii escapes U+2028/2029 too; "</" can't close the script element early.
    return json.dumps(data, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")


ENGINE_JS = "vendor/stockfish-18-lite-single.js"
ENGINE_WASM = "vendor/stockfish-18-lite-single.wasm"


@lru_cache(maxsize=1)
def _engine_blocks() -> str:
    """Stockfish (JavaScript loader + WebAssembly as base64) as inert text blocks.

    The page starts the engine from them in a Web Worker when it's first needed
    (a browser can't load a worker or a .wasm file from a page opened from disk).
    """
    js = (STATIC / ENGINE_JS).read_text(encoding="utf-8")
    wasm = base64.b64encode((STATIC / ENGINE_WASM).read_bytes()).decode("ascii")
    return ('<script type="text/plain" id="lfEngineJs">' + js.replace("</script", "<\\/script") + "</script>\n"
            '<script type="text/plain" id="lfEngineWasm">' + wasm + "</script>\n")


def build_html(profile_id: int, engine: bool = True) -> str:
    """The shared page: index.html with every asset and the profile's data inlined."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    read = lambda name: (STATIC / name).read_text(encoding="utf-8")  # noqa: E731
    replacements = {
        '<link rel="stylesheet" href="/static/vendor/chessboard-1.0.0.min.css">':
            "<style>\n" + read("vendor/chessboard-1.0.0.min.css") + "\n</style>",
        '<link rel="stylesheet" href="/static/app.css">': "<style>\n" + read("app.css") + "\n</style>",
        '<script src="/static/theme.js"></script>': _inline_script(read("theme.js")),
        '<script src="/static/vendor/jquery-3.7.1.min.js"></script>':
            _inline_script(read("vendor/jquery-3.7.1.min.js")),
        '<script src="/static/vendor/chessboard-1.0.0.min.js"></script>':
            _inline_script(read("vendor/chessboard-1.0.0.min.js")),
        '<script src="/static/vendor/chess-1.4.0.js"></script>': _inline_script(read("vendor/chess-1.4.0.js")),
        '<script src="/static/app.js"></script>':
            (_engine_blocks() if engine else "")
            + "<script>window.LUCIDFISH_EXPORT = " + _json_for_script(export_data(profile_id)) + ";</script>\n"
            + _inline_script(read("app.js")),
    }
    for old, new in replacements.items():
        if old not in html:
            raise RuntimeError(f"index.html changed: cannot find {old!r} to inline")
        html = html.replace(old, new)
    name = (store.get_profile(profile_id) or {}).get("name", "")
    return html.replace("<title>Lucidfish — explainable chess coaching</title>",
                        f"<title>{_escape(name)}'s chess games — Lucidfish</title>", 1)


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def file_name(profile: dict) -> str:
    safe = re.sub(r"[^\w .-]+", "", profile.get("name") or "profile", flags=re.UNICODE).strip() or "profile"
    return f"Lucidfish - {safe[:60]}.html"


# ------------------------------------------------------------------ folder sync

def _all() -> dict:
    return store.get_json(_SHARE_KEY, {}) or {}


def get_config(profile_id: int) -> dict:
    cfg = _all().get(str(profile_id), {})
    return {"folder": cfg.get("folder", ""), "auto": bool(cfg.get("auto")), "engine": cfg.get("engine", True),
            "file": cfg.get("file", ""), "last_synced": cfg.get("last_synced"),
            "last_error": cfg.get("last_error", "")}


def _update(profile_id: int, **fields) -> dict:
    with _lock:
        data = _all()
        entry = {**data.get(str(profile_id), {}), **fields}
        data[str(profile_id)] = entry
        store.set_json(_SHARE_KEY, data)
    return get_config(profile_id)


def check_folder(folder: str, create: bool = False) -> Path:
    """Validate a sync folder (optionally creating its last level). Raises ValueError with a readable reason."""
    path = Path(folder.strip().strip('"')).expanduser()
    if not folder.strip():
        raise ValueError("Enter a folder.")
    if not path.is_absolute():
        raise ValueError("Use the folder's full path, e.g. /Users/you/Dropbox/Lucidfish.")
    if not path.exists() and create and path.parent.is_dir():
        try:
            path.mkdir()
        except OSError as e:
            raise ValueError(f"Couldn't create {path}: {e.strerror or e}.") from None
    if not path.is_dir():
        raise ValueError(f"There is no folder at {path}. Create it first.")
    if not os.access(path, os.W_OK):
        raise ValueError(f"Lucidfish can't write to {path}.")
    return path


def configure(profile_id: int, folder: str, auto: bool, engine: bool = True) -> dict:
    """Save the sync folder (validated; '' switches syncing off) and whether copies carry the engine."""
    if not folder.strip():
        return _update(profile_id, folder="", auto=False, engine=bool(engine), last_error="")
    path = check_folder(folder, create=True)
    return _update(profile_id, folder=str(path), auto=bool(auto), engine=bool(engine), last_error="")


def write_file(profile_id: int, folder: Path, engine: bool = True) -> Path:
    """Write the shared page into `folder`, atomically (temp file + rename)."""
    profile = store.get_profile(profile_id)
    if profile is None:
        raise ValueError("unknown profile")
    target = folder / file_name(profile)
    html = build_html(profile_id, engine=engine)
    fd, tmp = tempfile.mkstemp(prefix=".lucidfish-", suffix=".html.tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(html)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def sync_now(profile_id: int) -> dict:
    """Write the up-to-date page into the profile's sync folder. Returns the status."""
    cfg = get_config(profile_id)
    if not cfg["folder"]:
        raise ValueError("Choose a folder first.")
    try:
        target = write_file(profile_id, check_folder(cfg["folder"]), engine=cfg["engine"])
    except (OSError, ValueError, RuntimeError) as e:
        return _update(profile_id, last_error=str(e))
    return _update(profile_id, file=str(target), last_synced=time.time(), last_error="")


def sync_profile(profile_id: int | None) -> None:
    """Called after anything that changes a profile's games: refresh its shared copy if enabled.

    Never raises: sharing must not break an analysis. Problems are recorded in the status.
    """
    if not profile_id:
        return
    try:
        if get_config(profile_id)["auto"]:
            sync_now(profile_id)
    except Exception as e:   # pragma: no cover - defensive
        _update(profile_id, last_error=str(e))


def forget(profile_id: int) -> None:
    with _lock:
        data = _all()
        data.pop(str(profile_id), None)
        store.set_json(_SHARE_KEY, data)


def folder_suggestions() -> list[dict]:
    """Folders that sync to other devices, if they exist on this computer."""
    home = Path.home()
    candidates = [("iCloud Drive", home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"),
                  ("Dropbox", home / "Dropbox"), ("Google Drive", home / "Google Drive"),
                  ("OneDrive", home / "OneDrive")]
    cloud = home / "Library" / "CloudStorage"
    if cloud.is_dir():
        for p in sorted(cloud.iterdir()):
            if p.name.startswith("GoogleDrive-") and (p / "My Drive").is_dir():
                candidates.append(("Google Drive", p / "My Drive"))
            elif p.name.startswith(("OneDrive", "Dropbox")):
                candidates.append((p.name.split("-")[0], p))
    out, seen = [], set()
    for label, path in candidates:
        if path.is_dir() and str(path) not in seen:
            seen.add(str(path))
            out.append({"label": label, "path": str(path)})
    return out
