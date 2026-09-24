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
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import ratings
from .config import data_dir
from .insights import VERSION as INSIGHTS_VERSION
from .insights import aggregate as aggregate_insights
from .insights import game_insights
from .spelling import british

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
    analysed_at  TEXT DEFAULT CURRENT_TIMESTAMP,
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
    ("games", "annotations_json", "TEXT"),
    ("profiles", "ratings_json", "TEXT"),
    ("games", "good_moves", "INTEGER"),
    ("games", "great_moves", "INTEGER"),
    ("profiles", "summaries_json", "TEXT"),
    ("profiles", "summary_times_json", "TEXT"),
]

TIME_CLASSES = ("bullet", "blitz", "rapid", "classical", "daily")

# (The elo_* columns hold ratings typed into older versions; they are migrated into ratings_json.)
PROFILE_FIELDS = ("name", "chesscom_user", "lichess_user", "level")


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
            if "analyzed_at" in {r[1] for r in conn.execute("PRAGMA table_info(games)")}:
                conn.execute("ALTER TABLE games RENAME COLUMN analyzed_at TO analysed_at")   # British spelling
            for table, column, decl in _MIGRATIONS:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            # after the migrations, so databases from older versions have the column
            conn.execute("CREATE INDEX IF NOT EXISTS games_profile ON games(profile_id, id)")
            _reclassify_time_controls(conn)
            _backfill_ratings(conn)
            _backfill_move_counts(conn)
            _british_spelling(conn)
    finally:
        conn.close()
    _initialised.add(path)


def _backfill_ratings(conn: sqlite3.Connection) -> None:
    """Profiles from before ratings were kept per site: their typed-in ratings, then every analysed game."""
    conn.row_factory = sqlite3.Row
    for p in conn.execute("SELECT * FROM profiles WHERE ratings_json IS NULL").fetchall():
        found = ratings.from_legacy(dict(p))
        for g in conn.execute("SELECT pgn, user_side, time_class FROM games WHERE profile_id=?", (p["id"],)):
            ratings.from_game(found, ratings.pgn_headers(g["pgn"] or ""), g["user_side"], g["time_class"] or "")
        conn.execute("UPDATE profiles SET ratings_json=? WHERE id=?", (json.dumps(found), p["id"]))


def _reclassify_time_controls(conn: sqlite3.Connection) -> None:
    """Once: games from before time controls followed each site's own rules (a 30-minute chess.com game is
    rapid there, not classical). Their insights are recomputed, and ratings taken from games re-filed."""
    conn.row_factory = sqlite3.Row
    if conn.execute("SELECT 1 FROM kv WHERE key='time_classes_by_site'").fetchone():
        return
    changed: set[int] = set()
    for g in conn.execute("SELECT id, profile_id, pgn, time_class FROM games").fetchall():
        cls = ratings.time_class(ratings.pgn_headers(g["pgn"] or ""))
        if cls != (g["time_class"] or ""):
            conn.execute("UPDATE games SET time_class=?, insights=NULL WHERE id=?", (cls, g["id"]))
            changed.add(g["profile_id"])
    for pid in changed:
        r = conn.execute("SELECT ratings_json FROM profiles WHERE id=?", (pid,)).fetchone()
        if r is None or r["ratings_json"] is None:
            continue      # not migrated yet: _backfill_ratings uses the corrected classes
        kept = {site: {k: e for k, e in entries.items() if e.get("source") != "game"}
                for site, entries in json.loads(r["ratings_json"] or "{}").items()}
        for g in conn.execute("SELECT pgn, user_side, time_class FROM games WHERE profile_id=?", (pid,)):
            ratings.from_game(kept, ratings.pgn_headers(g["pgn"] or ""), g["user_side"], g["time_class"] or "")
        conn.execute("UPDATE profiles SET ratings_json=? WHERE id=?",
                     (json.dumps({s: e for s, e in kept.items() if e}), pid))
    conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('time_classes_by_site', '1')")


# Stored text written before Lucidfish used British spelling throughout: the coach's reviews and notes, opening
# names ("Sicilian Defense"), and drawings (saved with a "color" key, now "colour"). Never the PGN itself.
_BRITISH_COLUMNS = {"games": ("opening", "review", "moves_json", "chapters_json", "annotations_json", "insights"),
                    "profiles": ("summary", "summaries_json")}


def _british_spelling(conn: sqlite3.Connection) -> None:
    """Once: make stored text British (see _BRITISH_COLUMNS)."""
    if conn.execute("SELECT 1 FROM kv WHERE key='british_spelling'").fetchone():
        return
    for table, columns in _BRITISH_COLUMNS.items():
        rows = conn.execute(f"SELECT id, {', '.join(columns)} FROM {table}").fetchall()
        for row_id, *old in map(tuple, rows):
            new = [british(v) if isinstance(v, str) else v for v in old]
            if new != old:
                conn.execute(f"UPDATE {table} SET {', '.join(f'{c}=?' for c in columns)} WHERE id=?", (*new, row_id))
    conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('british_spelling', '1')")


def _move_counts(moves: list[dict], user_side: str | None) -> dict[str, int]:
    """How many of the player's moves were best, good, and "great" (the only good move at a critical moment)."""
    mine = [m for m in moves if not user_side or m.get("side", "").lower() == user_side.lower()]
    return {"best": sum(m.get("cls") == "best" for m in mine), "good": sum(m.get("cls") == "good" for m in mine),
            "great": sum(bool(m.get("critical")) and m.get("cls") in ("best", "good") for m in mine)}


def _backfill_move_counts(conn: sqlite3.Connection) -> None:
    """Games analysed before the good-move counts were kept."""
    conn.row_factory = sqlite3.Row
    for g in conn.execute("SELECT id, user_side, moves_json FROM games WHERE good_moves IS NULL").fetchall():
        n = _move_counts(json.loads(g["moves_json"] or "[]"), g["user_side"])
        conn.execute("UPDATE games SET best_moves=?, good_moves=?, great_moves=? WHERE id=?",
                     (n["best"], n["good"], n["great"], g["id"]))


def _profile_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["ratings"] = json.loads(d.pop("ratings_json", None) or "{}")
    d["summaries"] = json.loads(d.pop("summaries_json", None) or "{}")   # coach review per time control
    d["summary_times"] = json.loads(d.pop("summary_times_json", None) or "{}")   # when each was written ("" = all)
    for k in ("elo_bullet", "elo_blitz", "elo_rapid"):
        d.pop(k, None)
    return d


# ---------------------------------------------------------------- profiles

def create_profile(**fields) -> int:
    vals = {k: fields.get(k) for k in PROFILE_FIELDS}
    vals["ratings_json"] = json.dumps(fields.get("ratings") or {})
    with _db(write=True) as c:
        cur = c.execute(
            f"INSERT INTO profiles ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})",
            tuple(vals.values()))
        pid = cur.lastrowid
    set_active(pid)
    return pid


def add_ratings(pid: int, found: dict) -> bool:
    """Merge ratings (e.g. from Look up) into a profile's; the newest per site and time control wins."""
    with _db(write=True) as c:
        r = c.execute("SELECT ratings_json FROM profiles WHERE id=?", (pid,)).fetchone()
        if r is None:
            return False
        current = json.loads(r["ratings_json"] or "{}")
        if not ratings.merge(current, found):
            return False
        c.execute("UPDATE profiles SET ratings_json=? WHERE id=?", (json.dumps(current), pid))
    return True


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
        return [_profile_row(r) for r in c.execute(
            "SELECT id, name, chesscom_user, lichess_user, level, ratings_json, "
            "(SELECT COUNT(*) FROM games WHERE games.profile_id = profiles.id) AS games "
            "FROM profiles ORDER BY id").fetchall()]


def get_profile(pid: int | None) -> dict | None:
    if pid is None:
        return None
    with _db() as c:
        r = c.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
    return _profile_row(r) if r else None


def active_id() -> int | None:
    v = _get_kv("active_profile")
    return int(v) if v else None


def set_active(pid: int) -> None:
    _set_kv("active_profile", str(pid))


def set_summary(pid: int, text: str, time_class: str | None = None) -> None:
    """The coach review of all games, or (with `time_class`) of the games in one time control."""
    with _db(write=True) as c:
        r = c.execute("SELECT summaries_json, summary_times_json FROM profiles WHERE id=?", (pid,)).fetchone()
        if r is None:
            return
        times = json.loads(r["summary_times_json"] or "{}")
        times[time_class or ""] = round(time.time())
        c.execute("UPDATE profiles SET summary_times_json=? WHERE id=?", (json.dumps(times), pid))
        if not time_class:
            c.execute("UPDATE profiles SET summary=? WHERE id=?", (text, pid))
            return
        summaries = json.loads(r["summaries_json"] or "{}")
        summaries[time_class] = text
        c.execute("UPDATE profiles SET summaries_json=? WHERE id=?", (json.dumps(summaries), pid))


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

    counts = _move_counts(moves, user_side)

    verb = "INSERT OR REPLACE" if replace else "INSERT"
    with _db(write=True) as c:
        try:
            cur = c.execute(
                f"""{verb} INTO games (profile_id, fingerprint, pgn, white, black, result,
                                      date, time_class, user_side, user_elo, opening, review,
                                      moves_json, acpl, accuracy, blunders, mistakes,
                                      inaccuracies, best_moves, good_moves, great_moves, insights,
                                      chapters_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (profile_id, fingerprint(pgn), pgn,
                 headers.get("White", "?"), headers.get("Black", "?"),
                 headers.get("Result", ""), headers.get("Date", ""),
                 time_class, user_side, user_elo, opening, review,
                 json.dumps(moves, separators=(",", ":")), acpl, acc,
                 n("blunder"), n("mistake"), n("inaccuracy"), counts["best"], counts["good"], counts["great"],
                 json.dumps(game_insights(moves, user_side, headers.get("Result", ""), time_class),
                            separators=(",", ":")),
                 json.dumps(chapters or [], separators=(",", ":"))))
        except sqlite3.IntegrityError:
            return None
        # The player's newest rating on that site, if the game carries one.
        r = c.execute("SELECT ratings_json FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if r is not None:
            current = json.loads(r["ratings_json"] or "{}")
            if ratings.from_game(current, headers, user_side, time_class):
                c.execute("UPDATE profiles SET ratings_json=? WHERE id=?", (json.dumps(current), profile_id))
        return cur.lastrowid


def list_games(profile_id: int) -> list[dict]:
    with _db() as c:
        rows = c.execute(
            """SELECT id, fingerprint, white, black, result, date, time_class, user_side,
                      user_elo, opening, acpl, accuracy, blunders, mistakes, inaccuracies,
                      best_moves, good_moves, great_moves, analysed_at
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
    d["annotations"] = json.loads(d.pop("annotations_json", None) or "{}")
    return d


def set_annotations(game_id: int, annotations: dict) -> bool:
    """Your own arrows and circles on the board, keyed by position (board part of the FEN)."""
    with _db(write=True) as c:
        cur = c.execute("UPDATE games SET annotations_json=? WHERE id=?",
                        (json.dumps(annotations, separators=(",", ":")), game_id))
    return cur.rowcount > 0


def delete_game(game_id: int) -> None:
    with _db(write=True) as c:
        c.execute("DELETE FROM games WHERE id=?", (game_id,))


def _wld(row: dict) -> str:
    if not row["user_side"] or row["result"] not in ("1-0", "0-1", "1/2-1/2"):
        return ""
    if row["result"] == "1/2-1/2":
        return "D"
    return "W" if (row["result"] == "1-0") == (row["user_side"].lower() == "white") else "L"


def _record(games: list[dict]) -> dict:
    wld = [_wld(g) for g in games]
    accs = [g["accuracy"] for g in games if g.get("accuracy") is not None]
    return {"games": len(games), "wins": wld.count("W"), "losses": wld.count("L"), "draws": wld.count("D"),
            "avg_accuracy": round(sum(accs) / len(accs), 1) if accs else None,
            "blunders_per_game": round(sum(g["blunders"] or 0 for g in games) / len(games), 2),
            "mistakes_per_game": round(sum(g["mistakes"] or 0 for g in games) / len(games), 2)}


def time_class_counts(games: list[dict]) -> dict[str, int]:
    counts = {tc: sum(1 for g in games if g.get("time_class") == tc) for tc in TIME_CLASSES}
    return {tc: n for tc, n in counts.items() if n}


def aggregate_stats(profile_id: int, games: list[dict] | None = None, time_class: str | None = None) -> dict:
    """Statistics over a profile's games, or only those of one time control."""
    games = list_games(profile_id) if games is None else games
    if time_class:
        games = [g for g in games if g.get("time_class") == time_class]
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
    by_class = {} if time_class else {
        tc: _record([g for g in games if g.get("time_class") == tc]) for tc in time_class_counts(games)}
    return {
        **aggregate_insights(_insights(profile_id, time_class)),
        "time_class": time_class or "",
        "by_time_class": by_class,
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


def _insights(profile_id: int, time_class: str | None = None) -> list[dict]:
    """Every game's insights; (re)computed and saved for games stored by older versions."""
    with _db() as c:
        rows = c.execute("SELECT id, insights, user_side, result, time_class FROM games WHERE profile_id=?"
                         + (" AND time_class=?" if time_class else ""),
                         (profile_id, time_class) if time_class else (profile_id,)).fetchall()
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


def recent_reviews(profile_id: int, n: int = 8, time_class: str | None = None) -> list[dict]:
    """Recent reviews WITH game context, so the summary can cite games by opponent."""
    with _db() as c:
        rows = c.execute(
            "SELECT review, white, black, result, date, user_side, opening, time_class "
            "FROM games WHERE profile_id=? AND review != ''" + (" AND time_class=?" if time_class else "")
            + " ORDER BY id DESC LIMIT ?",
            (profile_id, time_class, n) if time_class else (profile_id, n)).fetchall()
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
