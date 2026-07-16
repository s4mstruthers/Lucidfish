"""Fetch layer: pull your games straight from chess.com or Lichess.

Both use free public APIs, no auth token needed:
  - chess.com published-data API: https://www.chess.com/news/view/published-data-api
    (archives are monthly; games include full PGN)
  - Lichess export API: https://lichess.org/api#tag/Games/operation/apiGamesUser
"""

from __future__ import annotations

import time

import requests

# chess.com sits behind Cloudflare, which can hang non-browser-looking clients.
# A browser-style UA (with tool info appended, as their API docs request) avoids that.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "lucidfish/0.1 (personal chess study tool)"),
    "Accept": "application/json",
}
TIMEOUT = 30
RETRIES = 3


class FetchError(RuntimeError):
    pass


def _get(url: str, **kwargs) -> requests.Response:
    """GET with retries and exponential backoff for flaky/slow API responses."""
    last_error: Exception | None = None
    for attempt in range(RETRIES):
        try:
            r = requests.get(url, headers=kwargs.pop("headers", HEADERS),
                             timeout=TIMEOUT, **kwargs)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_error = e
            if attempt < RETRIES - 1:
                time.sleep(2 ** attempt)  # 1s, 2s
    raise FetchError(f"Request failed after {RETRIES} attempts: {last_error}") from last_error


def chesscom_recent_pgns(username: str, n: int = 1) -> list[str]:
    """Most recent `n` games from chess.com, newest first."""
    r = _get(f"https://api.chess.com/pub/player/{username.lower()}/games/archives")
    archives = r.json().get("archives", [])
    if not archives:
        raise FetchError(f"No game archives found for chess.com user '{username}'.")

    pgns: list[str] = []
    # Walk months newest-first until we have n games.
    for archive_url in reversed(archives):
        r = _get(archive_url)
        games = r.json().get("games", [])
        for g in reversed(games):  # newest last within a month
            if "pgn" in g:
                pgns.append(g["pgn"])
            if len(pgns) >= n:
                return pgns
    if not pgns:
        raise FetchError(f"No games with PGNs found for '{username}'.")
    return pgns


def lichess_recent_pgns(username: str, n: int = 1) -> list[str]:
    """Most recent `n` games from Lichess, newest first."""
    r = _get(
        f"https://lichess.org/api/games/user/{username}",
        params={"max": n, "pgnInJson": "false", "clocks": "true"},
        headers={**HEADERS, "Accept": "application/x-chess-pgn"},
    )
    # Games are separated by blank lines between [Event ...] headers.
    chunks = [c.strip() for c in r.text.split("\n\n\n") if c.strip()]
    if not chunks:
        raise FetchError(f"No games found for Lichess user '{username}'.")
    return chunks[:n]
