"""Fetch layer: pull your games straight from chess.com or Lichess.

Both use free public APIs, no auth token needed:
  - chess.com published-data API: https://www.chess.com/news/view/published-data-api
    (archives are monthly; games include full PGN)
  - Lichess export API: https://lichess.org/api#tag/Games/operation/apiGamesUser

The web UI fetches game lists in the browser instead (a real browser passes
Cloudflare checks that sometimes block scripts); this module serves the CLI.
"""

from __future__ import annotations

import json
import re
import time

import requests

from . import __version__

HEADERS = {
    # chess.com sits behind Cloudflare, which can hang non-browser-looking clients;
    # a browser-style UA with tool info appended (as their API docs request) avoids that.
    "User-Agent": f"Mozilla/5.0 (compatible) lucidfish/{__version__} (personal chess study tool)",
    "Accept": "application/json",
}
TIMEOUT = 30
RETRIES = 3
_USERNAME = re.compile(r"^[A-Za-z0-9_-]{2,30}$")
_SUPPORTED_RULES = {"chess", "chess960"}


class FetchError(RuntimeError):
    pass


def _check_username(username: str) -> str:
    username = (username or "").strip()
    if not _USERNAME.match(username):
        raise FetchError(f"'{username}' is not a valid username.")
    return username


def _get(url: str, *, headers: dict | None = None, params: dict | None = None) -> requests.Response:
    """GET with retries and exponential backoff for rate limits and server errors."""
    last_error: Exception | None = None
    for attempt in range(RETRIES):
        try:
            r = requests.get(url, headers=headers or HEADERS, params=params, timeout=TIMEOUT)
        except requests.RequestException as e:
            last_error = e
        else:
            if r.status_code == 404:
                raise FetchError("User not found — check the spelling of the username.")
            if r.status_code == 429 or r.status_code >= 500:
                last_error = FetchError(f"HTTP {r.status_code}")
            elif r.status_code >= 400:
                raise FetchError(f"Request refused (HTTP {r.status_code}).")
            else:
                return r
        if attempt < RETRIES - 1:
            time.sleep(2 ** attempt)  # 1s, 2s
    raise FetchError(f"Request failed after {RETRIES} attempts: {last_error}")


def chesscom_recent_pgns(username: str, n: int = 1) -> list[str]:
    """Most recent `n` standard/Chess960 games from chess.com, newest first."""
    username = _check_username(username).lower()
    archives = _get(f"https://api.chess.com/pub/player/{username}/games/archives").json().get("archives", [])
    if not archives:
        raise FetchError(f"No games found for chess.com user '{username}'.")
    pgns: list[str] = []
    for archive_url in reversed(archives):          # newest month first
        games = _get(archive_url).json().get("games", [])
        for g in reversed(games):                   # newest last within a month
            if "pgn" in g and g.get("rules", "chess") in _SUPPORTED_RULES:
                pgns.append(g["pgn"])
                if len(pgns) >= n:
                    return pgns
    if not pgns:
        raise FetchError(f"No standard chess games found for '{username}'.")
    return pgns


def lichess_recent_pgns(username: str, n: int = 1) -> list[str]:
    """Most recent `n` standard/Chess960 games from Lichess, newest first."""
    username = _check_username(username)
    r = _get(
        f"https://lichess.org/api/games/user/{username}",
        params={"max": max(n * 2, 5), "pgnInJson": "true", "clocks": "true", "opening": "true"},
        headers={**HEADERS, "Accept": "application/x-ndjson"},
    )
    pgns = []
    for line in r.text.splitlines():    # NDJSON: one game object per line
        if not line.strip():
            continue
        try:
            g = json.loads(line)
        except json.JSONDecodeError:
            continue
        if g.get("variant", "standard") in ("standard", "chess960", "fromPosition") and g.get("pgn"):
            pgns.append(g["pgn"])
        if len(pgns) >= n:
            break
    if not pgns:
        raise FetchError(f"No games found for Lichess user '{username}'.")
    return pgns
