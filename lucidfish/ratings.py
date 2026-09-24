"""Ratings: the player's rating in each game, and their latest rating on each site.

chess.com and Lichess ratings are not on the same scale (Lichess numbers run a few
hundred points higher for the same strength, most of all below 2000), so ratings
are kept per site and per time control, never mixed.

- A game's own rating comes from its PGN (``WhiteElo`` / ``BlackElo`` for the side
  the player had); chess.com and Lichess put it in every game they export.
- The profile keeps the newest rating seen for each site and time control. It is
  updated from every analysed game that carries one, and from "Look up" (the
  site's current rating). It is only a fallback for games without a rating
  (over-the-board games, some pasted PGNs) and for the board editor.

Stored shape: ``{"chesscom": {"blitz": {"rating": 1410, "date": "2026-09-24",
"source": "game"}}, "lichess": {...}}``.
"""

from __future__ import annotations

import re
from datetime import date as _date

SITES = {"chesscom": "chess.com", "lichess": "Lichess", "other": "other games"}
CLASSES = ("bullet", "blitz", "rapid", "classical", "daily")
MIN_RATING, MAX_RATING = 100, 3500
_HEADER = re.compile(r'^\[(\w+)\s+"((?:[^"\\]|\\.)*)"\]', re.M)


def pgn_headers(pgn: str) -> dict[str, str]:
    """The PGN's tag pairs (cheap: no move parsing)."""
    head = pgn.split("\n\n", 1)[0] if pgn.lstrip().startswith("[") else ""
    return {k: v for k, v in _HEADER.findall(head)}


def game_site(headers: dict) -> str:
    """'chesscom', 'lichess' or 'other' from the Site / Link headers."""
    where = f"{headers.get('Site', '')} {headers.get('Link', '')}".lower()
    if "chess.com" in where:
        return "chesscom"
    if "lichess.org" in where:
        return "lichess"
    return "other"


# Estimated duration (base + 40 × increment, in seconds) below which a game is in each class, per site.
_CLASS_LIMITS = {
    "chesscom": ((180, "bullet"), (600, "blitz"), (10**9, "rapid")),          # chess.com has no classical
    "lichess": ((180, "bullet"), (480, "blitz"), (1500, "rapid"), (10**9, "classical")),
    "other": ((180, "bullet"), (600, "blitz"), (1800, "rapid"), (10**9, "classical")),
}


def time_class(headers: dict) -> str:
    """The game's time control class by the rules of the site it was played on, so "rapid" means what
    chess.com or Lichess call rapid (a 30-minute game is rapid on chess.com, classical on Lichess)."""
    tc = headers.get("TimeControl", "").strip()
    site = game_site(headers)
    if "/" in tc:                                   # chess.com daily: "1/259200"
        return "daily"
    if tc == "-" and site == "lichess":             # Lichess correspondence
        return "daily"
    m = re.fullmatch(r"(\d+)(?:\+(\d+))?", tc)
    if not m:
        return ""
    estimated = int(m[1]) + 40 * int(m[2] or 0)
    return next(cls for limit, cls in _CLASS_LIMITS[site] if estimated < limit)


def game_rating(headers: dict, side: str | None) -> int | None:
    """The coached side's rating in this game, from WhiteElo / BlackElo."""
    if not side or side.lower() not in ("white", "black"):
        return None
    raw = headers.get("WhiteElo" if side.lower() == "white" else "BlackElo", "")
    return int(raw) if raw.isdigit() and MIN_RATING <= int(raw) <= MAX_RATING else None


def game_date(headers: dict) -> str:
    """'2026-09-24' from the game's date headers ('' if unknown)."""
    for key in ("UTCDate", "Date", "EndDate"):
        m = re.fullmatch(r"(\d{4})\.(\d{2})\.(\d{2})", headers.get(key, ""))
        if m:
            return "-".join(m.groups())
    return ""


def record(ratings: dict, site: str, cls: str, rating: int, when: str, source: str) -> bool:
    """Keep `rating` if it's at least as new as the one stored. Returns whether anything changed."""
    if site not in SITES or cls not in CLASSES or not (MIN_RATING <= rating <= MAX_RATING):
        return False
    current = ratings.get(site, {}).get(cls)
    if current and when < current.get("date", ""):
        return False
    new = {"rating": int(rating), "date": when, "source": source}
    if current == new:
        return False
    ratings.setdefault(site, {})[cls] = new
    return True


def from_game(ratings: dict, headers: dict, side: str | None, time_class: str) -> bool:
    """Update `ratings` from an analysed game that carries the player's rating."""
    rating = game_rating(headers, side)
    if rating is None or time_class not in CLASSES:
        return False
    return record(ratings, game_site(headers), time_class, rating, game_date(headers), "game")


def label(site: str, cls: str) -> str:
    """'chess.com blitz', 'Lichess rapid', 'blitz'."""
    name = SITES.get(site, "") if site != "other" else ""
    return " ".join(x for x in (name, cls) if x)


def for_game(ratings: dict, site: str, cls: str) -> tuple[int, str] | None:
    """The best stand-in for a game without its own rating: same site and time control, then the nearest
    time control on that site, then the same time control elsewhere, then the newest rating anywhere."""
    def nearest(entries: dict) -> str | None:
        if not entries:
            return None
        if cls in entries:
            return cls
        at = CLASSES.index(cls) if cls in CLASSES else CLASSES.index("rapid")
        return min(entries, key=lambda k: (abs(CLASSES.index(k) - at), -CLASSES.index(k)))

    order = [site] + [s for s in SITES if s != site]
    for s in order:                                  # same time control, this site first
        if cls in ratings.get(s, {}):
            return ratings[s][cls]["rating"], label(s, cls)
    for s in order:                                  # nearest time control
        k = nearest(ratings.get(s, {}))
        if k:
            return ratings[s][k]["rating"], label(s, k)
    return None


def newest(ratings: dict) -> tuple[int, str] | None:
    """The most recently seen rating on any site (for positions that aren't from a game)."""
    best = None
    for s, entries in ratings.items():
        for k, e in entries.items():
            if best is None or e.get("date", "") > best[0]:
                best = (e.get("date", ""), e["rating"], label(s, k))
    return (best[1], best[2]) if best else None


def summary(ratings: dict) -> str:
    """'chess.com: blitz 1410, rapid 1450; Lichess: rapid 1720' (for prompts)."""
    parts = []
    for s, name in SITES.items():
        entries = ratings.get(s, {})
        if entries:
            parts.append(f"{name}: " + ", ".join(f"{k} {entries[k]['rating']}" for k in CLASSES if k in entries))
    return "; ".join(parts)


def from_legacy(profile: dict, today: str = "") -> dict:
    """Ratings typed into older versions (one set, no site): filed under the site whose username the profile
    has (chess.com if both), else under 'other'. Dated the day they were migrated, as the best we know."""
    site = "chesscom" if profile.get("chesscom_user") else "lichess" if profile.get("lichess_user") else "other"
    out: dict = {}
    for cls in ("bullet", "blitz", "rapid"):
        v = profile.get(f"elo_{cls}")
        if isinstance(v, int):
            record(out, site, cls, v, "", "entered")
    return out


def clean_lookup(data: dict | None, today: str | None = None) -> dict:
    """Validate ratings fetched by the page from chess.com / Lichess: {site: {class: rating}} → stored shape."""
    out: dict = {}
    when = today or _date.today().isoformat()
    for site in ("chesscom", "lichess"):
        for cls, v in ((data or {}).get(site) or {}).items():
            if isinstance(v, int) and not isinstance(v, bool):
                record(out, site, cls, v, when, "lookup")
    return out


def merge(ratings: dict, other: dict) -> bool:
    """Merge `other` into `ratings`, newest wins. Returns whether anything changed."""
    changed = False
    for s, entries in other.items():
        for k, e in entries.items():
            changed |= record(ratings, s, k, e["rating"], e.get("date", ""), e.get("source", ""))
    return changed
