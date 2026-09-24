"""Local storage (SQLite, no server, no login).

Several people can share the app on one machine: each has a profile row
(chess.com / Lichess usernames, ability level, per-mode ratings, an
LLM-written coach summary) and every analysed game is tied to a profile. The
active profile is a single pointer in the kv table.

Analyses are stored document-style (full move list as JSON per game): written
once, read whole, so reopening an old game is instant and the dashboard is one
cheap query. A PGN fingerprint dedupes re-imports.

The same database also caches engine searches and opening-explorer lookups,
so re-analysing a game (or any position seen before, which is common in
openings) costs nothing. API keys are NEVER stored here — see credentials.py.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import data_dir
from .insights import VERSION as INSIGHTS_VERSION
from .insights import aggregate as aggregate_insights
from .insights import game_insights

_lock = threading.Lock()
_initialised: set[Path] = set()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT UNIQUE,
    chesscom_user TEXT DEFAULT '',
    lichess_user  TEXT DEFAULT '',
    level         TEXT DEFAULT '',
    elo_bullet    INTEGER, elo_blitz INTEGER, elo_rapid INTEGER,
    summary       TEXT DEFAULT '',
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS games (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id   INTEGER,
    fingerprint  TEXT,
    pgn          TEXT,
    white        TEXT, black TEXT, result TEXT, date TEXT,
    time_class   TEXT, user_side TEXT, user_elo INTEGER, opening TEXT,
    review       TEXT,
    moves_json   TEXT,
    acpl         REAL,
    accuracy     REAL,
    blunders     INTEGER, mistakes INTEGER, inaccuracies INTEGER, best_moves INTEGER,
    analyzed_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(profile_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS engine_cache (
    key      TEXT PRIMARY KEY,
    strength INTEGER,
    multipv  INTEGER,
    data     TEXT
);
CREATE TABLE IF NOT EXISTS explorer_cache (
    fen      TEXT PRIMARY KEY,
    data     TEXT,
    fetched  TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

# Columns added after the first release: (table, column, declaration).
_MIGRATIONS = [
    ("games", "profile_id", "INTEGER"),
    ("games", "accuracy", "REAL"),
    ("profiles", "lichess_user", "TEXT DEFAULT ''"),
    ("games", "insights", "TEXT"),
    ("games", "chapters_json", "TEXT"),
]

PROFILE_FIELDS = ("name", "chesscom_user", "lichess_user", "level", "elo_bullet", "elo_blitz", "elo_rapid")


def db_path() -> Path:
    return data_dir() / "lucidfish.db"


@contextmanager
def _db(write: bool = False) -> Iterator[sqlite3.Connection]:
    """Short-lived connection: commits on success, always closes.

    Writes are serialised with a process-wide lock; WAL mode lets readers
    proceed while a background analysis is writing.
    """
    path = db_path()
    if path not in _initialised:
        init()
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        if write:
            with _lock, conn:
                yield conn
        else:
            yield conn
    finally:
        conn.close()


def init() -> None:
    """Create or migrate the database. Idempotent and cheap."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    try:
        with _lock, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            for table, column, decl in _MIGRATIONS:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            # after the migrations, so databases from older versions have the column
            conn.execute("CREATE INDEX IF NOT EXISTS games_profile ON games(profile_id, id)")
    finally:
        conn.close()
    _initialised.add(path)


# ---------------------------------------------------------------- profiles

def create_profile(**fields) -> int:
    vals = {k: fields.get(k) for k in PROFILE_FIELDS}
    with _db(write=True) as c:
        cur = c.execute(
            f"INSERT INTO profiles ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})",
            tuple(vals.values()))
        pid = cur.lastrowid
    set_active(pid)
    return pid


def update_profile(pid: int, **fields) -> None:
    vals = {k: v for k, v in fields.items() if k in PROFILE_FIELDS}
    if not vals:
        return
    with _db(write=True) as c:
        c.execute(f"UPDATE profiles SET {','.join(k + '=?' for k in vals)} WHERE id=?",
                  (*vals.values(), pid))


def delete_profile(pid: int) -> None:
    """Remove a profile and every game analysed for it."""
    with _db(write=True) as c:
        c.execute("DELETE FROM games WHERE profile_id=?", (pid,))
        c.execute("DELETE FROM profiles WHERE id=?", (pid,))
    if active_id() == pid:
        remaining = list_profiles()
        _set_kv("active_profile", str(remaining[0]["id"]) if remaining else "")


def list_profiles() -> list[dict]:
    with _db() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, name, chesscom_user, lichess_user, level, elo_bullet, elo_blitz, elo_rapid "
            "FROM profiles ORDER BY id").fetchall()]


def get_profile(pid: int | None) -> dict | None:
    if pid is None:
        return None
    with _db() as c:
        r = c.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


def active_id() -> int | None:
    v = _get_kv("active_profile")
    return int(v) if v else None


def set_active(pid: int) -> None:
    _set_kv("active_profile", str(pid))


def set_summary(pid: int, text: str) -> None:
    with _db(write=True) as c:
        c.execute("UPDATE profiles SET summary=? WHERE id=?", (text, pid))


# ---------------------------------------------------------------- games

def fingerprint(pgn: str) -> str:
    return hashlib.sha1(pgn.strip().encode()).hexdigest()


def has_game(profile_id: int, pgn: str) -> bool:
    with _db() as c:
        r = c.execute("SELECT 1 FROM games WHERE profile_id=? AND fingerprint=?",
                      (profile_id, fingerprint(pgn))).fetchone()
    return r is not None


def save_game(profile_id: int, pgn: str, headers: dict, user_side: str | None,
              user_elo: int | None, opening: str, review: str, moves: list[dict],
              time_class: str = "", accuracy: dict | None = None, replace: bool = False,
              chapters: list[dict] | None = None) -> int | None:
    """Store an analysed game. Aggregates are computed for the user's side only."""
    mine = [m for m in moves if not user_side or m["side"].lower() == user_side.lower()]
    losses = [min(m["cp_loss"], 1000) for m in mine]   # cap mate scores out of ACPL
    acpl = round(sum(losses) / len(losses), 1) if losses else None
    acc = None
    if accuracy:
        acc = accuracy.get(user_side.lower()) if user_side else None
        if acc is None and not user_side:
            vals = [v for v in accuracy.values() if v is not None]
            acc = round(sum(vals) / len(vals), 1) if vals else None

    def n(cls: str) -> int:
        return sum(1 for m in mine if m["cls"] == cls)

    verb = "INSERT OR REPLACE" if replace else "INSERT"
    with _db(write=True) as c:
        try:
            cur = c.execute(
                f"""{verb} INTO games (profile_id, fingerprint, pgn, white, black, result,
                                      date, time_class, user_side, user_elo, opening, review,
                                      moves_json, acpl, accuracy, blunders, mistakes,
                                      inaccuracies, best_moves, insights, chapters_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (profile_id, fingerprint(pgn), pgn,
                 headers.get("White", "?"), headers.get("Black", "?"),
                 headers.get("Result", ""), headers.get("Date", ""),
                 time_class, user_side, user_elo, opening, review,
                 json.dumps(moves, separators=(",", ":")), acpl, acc,
                 n("blunder"), n("mistake"), n("inaccuracy"), n("best"),
                 json.dumps(game_insights(moves, user_side, headers.get("Result", ""), time_class),
                            separators=(",", ":")),
                 json.dumps(chapters or [], separators=(",", ":"))))
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def list_games(profile_id: int) -> list[dict]:
    with _db() as c:
        rows = c.execute(
            """SELECT id, fingerprint, white, black, result, date, time_class, user_side,
                      user_elo, opening, acpl, accuracy, blunders, mistakes, inaccuracies, analyzed_at
               FROM games WHERE profile_id=? ORDER BY id DESC""", (profile_id,)).fetchall()
    return [dict(r) for r in rows]


def get_game(game_id: int) -> dict | None:
    with _db() as c:
        r = c.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d["moves"] = json.loads(d.pop("moves_json") or "[]")
    d["chapters"] = json.loads(d.pop("chapters_json", None) or "[]")
    return d


def delete_game(game_id: int) -> None:
    with _db(write=True) as c:
        c.execute("DELETE FROM games WHERE id=?", (game_id,))


def _wld(row: dict) -> str:
    if not row["user_side"] or row["result"] not in ("1-0", "0-1", "1/2-1/2"):
        return ""
    if row["result"] == "1/2-1/2":
        return "D"
    return "W" if (row["result"] == "1-0") == (row["user_side"].lower() == "white") else "L"


def aggregate_stats(profile_id: int, games: list[dict] | None = None) -> dict:
    games = list_games(profile_id) if games is None else games
    if not games:
        return {"games": 0}
    wld = [_wld(g) for g in games]
    acpls = [g["acpl"] for g in games if g["acpl"] is not None]
    accs = [g["accuracy"] for g in games if g.get("accuracy") is not None]
    openings: dict[str, dict] = {}
    for g in games:
        name = (g["opening"] or "Unknown").split("(")[0].strip() or "Unknown"
        o = openings.setdefault(name, {"games": 0, "w": 0, "l": 0, "d": 0, "acpl": []})
        o["games"] += 1
        r = _wld(g)
        if r:
            o[r.lower()] += 1
        if g["acpl"] is not None:
            o["acpl"].append(g["acpl"])
    for o in openings.values():
        o["acpl"] = round(sum(o["acpl"]) / len(o["acpl"]), 1) if o["acpl"] else None
    return {
        **aggregate_insights(_insights(profile_id)),
        "games": len(games),
        "wins": wld.count("W"), "losses": wld.count("L"), "draws": wld.count("D"),
        "avg_acpl": round(sum(acpls) / len(acpls), 1) if acpls else None,
        "avg_accuracy": round(sum(accs) / len(accs), 1) if accs else None,
        "blunders_per_game": round(sum(g["blunders"] or 0 for g in games) / len(games), 2),
        "mistakes_per_game": round(sum(g["mistakes"] or 0 for g in games) / len(games), 2),
        "openings": sorted(
            ({"name": k, **v} for k, v in openings.items()),
            key=lambda x: -x["games"])[:12],
    }


def _insights(profile_id: int) -> list[dict]:
    """Every game's insights; (re)computed and saved for games stored by older versions."""
    with _db() as c:
        rows = c.execute("SELECT id, insights, user_side, result, time_class FROM games WHERE profile_id=?",
                         (profile_id,)).fetchall()
    out, backfill = [], []
    for r in rows:
        stored = json.loads(r["insights"]) if r["insights"] else None
        if stored and stored.get("v", 1) >= INSIGHTS_VERSION:
            out.append(stored)
            continue
        game = get_game(r["id"]) or {}
        data = game_insights(game.get("moves", []), r["user_side"], r["result"] or "", r["time_class"] or "")
        out.append(data)
        backfill.append((json.dumps(data, separators=(",", ":")), r["id"]))
    if backfill:
        with _db(write=True) as c:
            c.executemany("UPDATE games SET insights=? WHERE id=?", backfill)
    return out


def recent_reviews(profile_id: int, n: int = 8) -> list[dict]:
    """Recent reviews WITH game context, so the summary can cite games by opponent."""
    with _db() as c:
        rows = c.execute(
            "SELECT review, white, black, result, date, user_side, opening "
            "FROM games WHERE profile_id=? AND review != '' "
            "ORDER BY id DESC LIMIT ?", (profile_id, n)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- settings

def get_settings() -> dict:
    """Non-secret user settings saved from the web UI (provider, model, depth, ...)."""
    try:
        return json.loads(_get_kv("settings") or "{}")
    except json.JSONDecodeError:
        return {}


def save_settings(values: dict) -> dict:
    merged = {**get_settings(), **values}
    merged = {k: v for k, v in merged.items() if v is not None}
    _set_kv("settings", json.dumps(merged))
    return merged


def get_json(key: str, default=None):
    """Small JSON documents kept in the kv table (queue state, learned timings)."""
    try:
        raw = _get_kv(key)
        return json.loads(raw) if raw else default
    except (json.JSONDecodeError, sqlite3.Error):
        return default


def set_json(key: str, value) -> None:
    _set_kv(key, json.dumps(value, separators=(",", ":")))


# ---------------------------------------------------------------- caches

class EngineCache:
    """Persistent store of finished engine searches (see engine.EvalCache).

    Keyed by engine name + search kind + position; an entry satisfies any
    request of equal or lower depth and MultiPV.
    """

    def get(self, key: str, strength: int, multipv: int) -> list | None:
        try:
            with _db() as c:
                r = c.execute("SELECT strength, multipv, data FROM engine_cache WHERE key=?",
                              (key,)).fetchone()
        except sqlite3.Error:
            return None
        if r is None or r["strength"] < strength or r["multipv"] < multipv:
            return None
        try:
            return json.loads(r["data"])
        except json.JSONDecodeError:
            return None

    def put(self, key: str, strength: int, multipv: int, data: list) -> None:
        try:
            with _db(write=True) as c:
                c.execute(
                    "INSERT INTO engine_cache (key, strength, multipv, data) VALUES (?,?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET strength=excluded.strength, "
                    "multipv=excluded.multipv, data=excluded.data "
                    "WHERE excluded.strength >= engine_cache.strength "
                    "AND excluded.multipv >= engine_cache.multipv",
                    (key, strength, multipv, json.dumps(data, separators=(",", ":"))))
        except sqlite3.Error:
            pass  # a cache must never break an analysis


def explorer_get(fen: str) -> dict | None:
    try:
        with _db() as c:
            r = c.execute("SELECT data FROM explorer_cache WHERE fen=?", (fen,)).fetchone()
        return json.loads(r["data"]) if r else None
    except (sqlite3.Error, json.JSONDecodeError):
        return None


def explorer_put(fen: str, data: dict) -> None:
    try:
        with _db(write=True) as c:
            c.execute("INSERT OR REPLACE INTO explorer_cache (fen, data) VALUES (?,?)",
                      (fen, json.dumps(data, separators=(",", ":"))))
    except sqlite3.Error:
        pass


def cache_stats() -> dict:
    with _db() as c:
        return {
            "engine_positions": c.execute("SELECT COUNT(*) FROM engine_cache").fetchone()[0],
            "opening_positions": c.execute("SELECT COUNT(*) FROM explorer_cache").fetchone()[0],
        }


def clear_caches() -> None:
    with _db(write=True) as c:
        c.execute("DELETE FROM engine_cache")
        c.execute("DELETE FROM explorer_cache")
    conn = sqlite3.connect(db_path())
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()


# ---------------------------------------------------------------- kv

def _get_kv(key: str, default: str = "") -> str:
    with _db() as c:
        r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def _set_kv(key: str, value: str) -> None:
    with _db(write=True) as c:
        c.execute("INSERT INTO kv (key, value) VALUES (?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
