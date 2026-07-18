"""Local multi-profile store (SQLite, no server, no login).

Several people can share the app on one machine: each has a profile row
(chess.com username, ability level, per-mode ELOs, an LLM-written coach
summary) and every analyzed game is tied to a profile. The active profile is
a single pointer in the kv table — switching profiles switches whose games,
stats, and coaching context the whole app uses.

Analyses are stored document-style (full move list as JSON per game): written
once, read whole, so reopening an old game is instant and the dashboard is one
cheap query. A PGN fingerprint dedupes re-imports.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "lucidfish.db"
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT UNIQUE,
    chesscom_user TEXT DEFAULT '',
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
    blunders     INTEGER, mistakes INTEGER, inaccuracies INTEGER, best_moves INTEGER,
    analyzed_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(profile_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""

PROFILE_FIELDS = ("name", "chesscom_user", "level", "elo_bullet", "elo_blitz", "elo_rapid")


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with _lock, _conn() as c:
        c.executescript(_SCHEMA)
        try:   # migrate a DB created by the earlier single-profile version
            c.execute("ALTER TABLE games ADD COLUMN profile_id INTEGER")
        except sqlite3.OperationalError:
            pass  # column already exists


# ---------------------------------------------------------------- profiles

def create_profile(**fields) -> int:
    vals = {k: fields.get(k) for k in PROFILE_FIELDS}
    with _lock, _conn() as c:
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
    with _lock, _conn() as c:
        c.execute(f"UPDATE profiles SET {','.join(k + '=?' for k in vals)} WHERE id=?",
                  (*vals.values(), pid))


def list_profiles() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, name, chesscom_user, level, elo_bullet, elo_blitz, elo_rapid "
            "FROM profiles ORDER BY id").fetchall()]


def get_profile(pid: int | None) -> dict | None:
    if pid is None:
        return None
    with _conn() as c:
        r = c.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


def active_id() -> int | None:
    v = _get_kv("active_profile")
    return int(v) if v else None


def set_active(pid: int) -> None:
    _set_kv("active_profile", str(pid))


def set_summary(pid: int, text: str) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE profiles SET summary=? WHERE id=?", (text, pid))


# ---------------------------------------------------------------- games

def _fingerprint(pgn: str) -> str:
    return hashlib.sha1(pgn.strip().encode()).hexdigest()


def has_game(profile_id: int, pgn: str) -> bool:
    with _conn() as c:
        r = c.execute("SELECT 1 FROM games WHERE profile_id=? AND fingerprint=?",
                      (profile_id, _fingerprint(pgn))).fetchone()
    return r is not None


def save_game(profile_id: int, pgn: str, headers: dict, user_side: str | None,
              user_elo: int | None, opening: str, review: str, moves: list[dict],
              time_class: str = "", replace: bool = False) -> int | None:
    mine = [m for m in moves if not user_side or m["side"].lower() == user_side.lower()]
    losses = [min(m["cp_loss"], 1000) for m in mine]   # cap mate scores out of ACPL
    acpl = round(sum(losses) / len(losses), 1) if losses else None
    n = lambda cls: sum(1 for m in mine if m["cls"] == cls)

    verb = "INSERT OR REPLACE" if replace else "INSERT"
    with _lock, _conn() as c:
        try:
            cur = c.execute(
                f"""{verb} INTO games (profile_id, fingerprint, pgn, white, black, result,
                                      date, time_class, user_side, user_elo, opening, review,
                                      moves_json, acpl, blunders, mistakes, inaccuracies, best_moves)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (profile_id, _fingerprint(pgn), pgn,
                 headers.get("White", "?"), headers.get("Black", "?"),
                 headers.get("Result", ""), headers.get("Date", ""),
                 time_class, user_side, user_elo, opening, review,
                 json.dumps(moves), acpl, n("blunder"), n("mistake"), n("inaccuracy"), n("best")))
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def list_games(profile_id: int) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            """SELECT id, fingerprint, white, black, result, date, time_class, user_side,
                      user_elo, opening, acpl, blunders, mistakes, inaccuracies, analyzed_at
               FROM games WHERE profile_id=? ORDER BY id DESC""", (profile_id,)).fetchall()
    return [dict(r) for r in rows]


def get_game(game_id: int) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d["moves"] = json.loads(d.pop("moves_json") or "[]")
    return d


def _wld(row) -> str:
    if not row["user_side"] or row["result"] not in ("1-0", "0-1", "1/2-1/2"):
        return ""
    if row["result"] == "1/2-1/2":
        return "D"
    return "W" if (row["result"] == "1-0") == (row["user_side"].lower() == "white") else "L"


def aggregate_stats(profile_id: int) -> dict:
    games = list_games(profile_id)
    if not games:
        return {"games": 0}
    wld = [_wld(g) for g in games]
    acpls = [g["acpl"] for g in games if g["acpl"] is not None]
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
        "games": len(games),
        "wins": wld.count("W"), "losses": wld.count("L"), "draws": wld.count("D"),
        "avg_acpl": round(sum(acpls) / len(acpls), 1) if acpls else None,
        "blunders_per_game": round(sum(g["blunders"] for g in games) / len(games), 2),
        "mistakes_per_game": round(sum(g["mistakes"] for g in games) / len(games), 2),
        "openings": sorted(
            ({"name": k, **v} for k, v in openings.items()),
            key=lambda x: -x["games"])[:12],
    }


def recent_reviews(profile_id: int, n: int = 8) -> list[dict]:
    """Recent reviews WITH game context, so the summary can cite games by opponent."""
    with _conn() as c:
        rows = c.execute(
            "SELECT review, white, black, result, date, user_side, opening "
            "FROM games WHERE profile_id=? AND review != '' "
            "ORDER BY id DESC LIMIT ?", (profile_id, n)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- kv

def _get_kv(key: str, default: str = "") -> str:
    with _conn() as c:
        r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def _set_kv(key: str, value: str) -> None:
    with _lock, _conn() as c:
        c.execute("INSERT INTO kv (key, value) VALUES (?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
