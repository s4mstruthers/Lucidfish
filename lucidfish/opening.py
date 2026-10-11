"""Opening layer: Lichess Opening Explorer client.

In the opening, "engine best" and "theory best" can differ — the explorer tells
us what strong humans actually play and what the opening is called, so early
advice stays consistent with real plans instead of raw engine output.

API docs: https://lichess.org/api#tag/Opening-Explorer (be polite; set LICHESS_TOKEN if
the explorer asks for authentication).
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict, dataclass, field

import requests

from . import __version__
from .spelling import british

EXPLORER_URL = "https://explorer.lichess.ovh/masters"
PLAYERS_URL = "https://explorer.lichess.ovh/lichess"      # games of ordinary players, by rating and speed
# The explorer's rating groups (each value is the lower bound of a band).
RATING_BANDS = (0, 1000, 1200, 1400, 1600, 1800, 2000, 2200, 2500)
_SPEEDS = {"bullet": "bullet", "blitz": "blitz", "rapid": "rapid", "classical": "classical",
           "daily": "correspondence"}
# After a network failure, stop asking for a while instead of paying a timeout
# on every early move of every game (some networks silently drop these requests).
_BACKOFF_S = 600

# Offline fallback book: longest SAN-prefix match. Used when the Lichess explorer
# is unreachable (some networks block Python clients), so opening identification
# never silently fails — an unverified LLM guess is worse than a coarse local name.
_LOCAL_BOOK: list[tuple[str, str, str]] = [
    # --- 1.e4 e5 ---
    ("e4 e5 Nf3 Nc6 Bc4 Bc5 b4", "Evans Gambit", "C51"),
    ("e4 e5 Nf3 Nc6 Bc4 Bc5", "Italian Game: Giuoco Piano", "C53"),
    ("e4 e5 Nf3 Nc6 Bc4 Nf6", "Italian Game: Two Knights Defence", "C55"),
    ("e4 e5 Nf3 Nc6 Bc4", "Italian Game", "C50"),
    ("e4 e5 Nf3 Nc6 Bb5 a6", "Ruy Lopez: Morphy Defence", "C70"),
    ("e4 e5 Nf3 Nc6 Bb5 Nf6", "Ruy Lopez: Berlin Defence", "C65"),
    ("e4 e5 Nf3 Nc6 Bb5", "Ruy Lopez", "C60"),
    ("e4 e5 Nf3 Nc6 d4", "Scotch Game", "C45"),
    ("e4 e5 Nf3 Nc6 Nc3 Nf6", "Four Knights Game", "C47"),
    ("e4 e5 Nf3 Nf6", "Petrov's Defence", "C42"),
    ("e4 e5 Nf3 d6", "Philidor Defence", "C41"),
    ("e4 e5 Nf3 Nc6", "King's Knight Opening", "C44"),
    ("e4 e5 Nc3", "Vienna Game", "C25"),
    ("e4 e5 f4 exf4", "King's Gambit Accepted", "C33"),
    ("e4 e5 f4", "King's Gambit", "C30"),
    ("e4 e5 Bc4", "Bishop's Opening", "C23"),
    ("e4 e5 d4", "Centre Game", "C21"),
    ("e4 e5", "King's Pawn Game", "C20"),
    # --- Sicilian ---
    ("e4 c5 c3", "Sicilian Defence: Alapin Variation", "B22"),
    ("e4 c5 Nc3", "Sicilian Defence: Closed", "B23"),
    ("e4 c5 d4", "Sicilian Defence: Smith-Morra Gambit", "B21"),
    ("e4 c5", "Sicilian Defence", "B20"),
    # --- French ---
    ("e4 e6 d4 d5 e5", "French Defence: Advance Variation", "C02"),
    ("e4 e6 d4 d5 exd5", "French Defence: Exchange Variation", "C01"),
    ("e4 e6 d4 d5 Nd2", "French Defence: Tarrasch Variation", "C03"),
    ("e4 e6 d4 d5 Nc3 Bb4", "French Defence: Winawer Variation", "C15"),
    ("e4 e6 d4 d5 Nc3 Nf6", "French Defence: Classical Variation", "C11"),
    ("e4 e6", "French Defence", "C00"),
    # --- Caro-Kann ---
    ("e4 c6 d4 d5 e5", "Caro-Kann Defence: Advance Variation", "B12"),
    ("e4 c6 d4 d5 exd5", "Caro-Kann Defence: Exchange Variation", "B13"),
    ("e4 c6 d4 d5 Nc3 dxe4 Nxe4 Bf5", "Caro-Kann Defence: Classical Variation", "B18"),
    ("e4 c6 d4 d5 Nc3", "Caro-Kann Defence: Main Line", "B15"),
    ("e4 c6 Nc3 d5 Nf3", "Caro-Kann Defence: Two Knights Attack", "B11"),
    ("e4 c6", "Caro-Kann Defence", "B10"),
    # --- other e4 defences ---
    ("e4 d5 exd5 Qxd5", "Scandinavian Defence: Mieses-Kotroc", "B01"),
    ("e4 d5 exd5 Nf6", "Scandinavian Defence: Modern Variation", "B01"),
    ("e4 d5", "Scandinavian Defence", "B01"),
    ("e4 d6 d4 Nf6", "Pirc Defence", "B07"),
    ("e4 d6", "Pirc Defence", "B07"),
    ("e4 g6", "Modern Defence", "B06"),
    ("e4 Nf6", "Alekhine's Defence", "B02"),
    ("e4", "King's Pawn Opening", "B00"),
    # --- 1.d4 ---
    ("d4 d5 c4 dxc4", "Queen's Gambit Accepted", "D20"),
    ("d4 d5 c4 e6", "Queen's Gambit Declined", "D30"),
    ("d4 d5 c4 c6", "Slav Defence", "D10"),
    ("d4 d5 c4", "Queen's Gambit", "D06"),
    ("d4 d5 Bf4", "London System", "D02"),
    ("d4 d5 Nf3 Nf6 Bf4", "London System", "D02"),
    ("d4 Nf6 Bg5", "Trompowsky Attack", "A45"),
    ("d4 Nf6 c4 g6 Nc3 d5", "Grünfeld Defence", "D80"),
    ("d4 Nf6 c4 g6", "King's Indian Defence", "E60"),
    ("d4 Nf6 c4 e6 Nc3 Bb4", "Nimzo-Indian Defence", "E20"),
    ("d4 Nf6 c4 e6 Nf3 b6", "Queen's Indian Defence", "E12"),
    ("d4 Nf6 c4 e6 g3", "Catalan Opening", "E01"),
    ("d4 Nf6 c4 c5", "Benoni Defence", "A56"),
    ("d4 f5", "Dutch Defence", "A80"),
    ("d4 d5", "Queen's Pawn Game", "D00"),
    ("d4", "Queen's Pawn Opening", "A40"),
    # --- flank ---
    ("c4 e5", "English Opening: King's English", "A20"),
    ("c4 c5", "English Opening: Symmetrical", "A30"),
    ("c4", "English Opening", "A10"),
    ("Nf3 d5 c4", "Réti Opening", "A09"),
    ("Nf3 d5 g3", "King's Indian Attack", "A07"),
    ("Nf3", "Zukertort Opening", "A04"),
    ("f4", "Bird's Opening", "A02"),
    ("b3", "Nimzo-Larsen Attack", "A01"),
    ("g3", "King's Fianchetto Opening", "A00"),
    ("b4", "Polish Opening", "A00"),
]


def local_book_depth(sans: list[str]) -> int:
    """How many opening plies of `sans` follow a line in the built-in book."""
    best = 0
    for seq, _, _ in _LOCAL_BOOK:
        line = seq.split()
        if len(line) > best and sans[:len(line)] == line:
            best = len(line)
    return best


def local_opening_name(sans: list[str]) -> str:
    """Longest-prefix match against the built-in book. Returns 'Name (ECO)' or ''."""
    played = " ".join(sans)
    best = ""
    best_len = -1
    for seq, name, eco in _LOCAL_BOOK:
        if (played == seq or played.startswith(seq + " ")) and len(seq) > best_len:
            best, best_len = f"{name} ({eco})", len(seq)
    return best


@dataclass
class OpeningInfo:
    name: str = ""
    eco: str = ""
    top_moves: list[dict] = field(default_factory=list)  # [{san, games, white_pct, draw_pct, black_pct}]

    @property
    def known(self) -> bool:
        return bool(self.top_moves)


class OpeningExplorer:
    """Lichess masters-database client with a persistent cache and a circuit breaker.

    Results are cached in memory and (optionally) in the local database, so
    positions from your usual openings are looked up once, ever. Set
    LICHESS_TOKEN to send an API token if the explorer requires authentication.
    """

    _disabled_until = 0.0            # shared by all instances: one outage, one timeout
    _state_lock = threading.Lock()

    def __init__(self, timeout_s: float = 4.0, persistent: bool = True):
        self.timeout_s = timeout_s
        self.persistent = persistent
        self._session = requests.Session()
        self._session.headers["User-Agent"] = f"lucidfish/{__version__} (personal chess study tool)"
        token = os.environ.get("LICHESS_TOKEN")
        if token:
            self._session.headers["Authorization"] = f"Bearer {token}"
        self._cache: dict[str, OpeningInfo] = {}

    @classmethod
    def available(cls) -> bool:
        return time.monotonic() >= cls._disabled_until

    def lookup(self, fen: str) -> OpeningInfo:
        """Masters-database stats for a position. Fails soft: network errors → empty info."""
        if fen in self._cache:
            return self._cache[fen]
        if self.persistent:
            from . import store
            cached = store.explorer_get(fen)
            if cached is not None:
                info = OpeningInfo(**cached)
                info.name = british(info.name)   # cached before names were made British
                self._cache[fen] = info
                return info
        if not self.available():
            return OpeningInfo()

        info = OpeningInfo()
        try:
            r = self._session.get(EXPLORER_URL, params={"fen": fen, "moves": 5, "topGames": 0},
                                  timeout=self.timeout_s)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            with self._state_lock:   # offline, blocked, rate-limited or auth required
                OpeningExplorer._disabled_until = time.monotonic() + _BACKOFF_S
            return info
        opening = data.get("opening") or {}
        info.name = british(opening.get("name", ""))   # Lichess spells them the American way
        info.eco = opening.get("eco", "")
        for m in data.get("moves", []):
            total = m.get("white", 0) + m.get("draws", 0) + m.get("black", 0)
            if total == 0:
                continue
            info.top_moves.append({
                "san": m["san"],
                "games": total,
                "white_pct": round(100 * m["white"] / total),
                "draw_pct": round(100 * m["draws"] / total),
                "black_pct": round(100 * m["black"] / total),
            })
        self._cache[fen] = info
        if self.persistent:
            from . import store
            store.explorer_put(fen, asdict(info))
        return info

    # ---------------------------------------------------------------- players at the user's level

    @staticmethod
    def rating_bands(elo: int) -> list[int]:
        """The explorer's rating band containing `elo` and its neighbours: "players around your level"."""
        idx = max(i for i, lo in enumerate(RATING_BANDS) if elo >= lo) if elo >= RATING_BANDS[0] else 0
        return [RATING_BANDS[i] for i in (idx - 1, idx, idx + 1) if 0 <= i < len(RATING_BANDS)]

    @staticmethod
    def speeds(time_class: str) -> str:
        return _SPEEDS.get(time_class, "") or "blitz,rapid,classical"

    def lookup_players(self, fen: str, elo: int, time_class: str = "") -> OpeningInfo:
        """What ordinary players around `elo` play here (the Lichess games database), with results.

        Masters show the right plan; peers show what actually goes wrong at this level — the natural
        mistake, the trap that keeps working — which is what a coach wants to warn about. Same cache
        and circuit breaker as the masters lookup; results are cached per rating range and speed.
        """
        bands, speeds = self.rating_bands(elo), self.speeds(time_class)
        key = f"players|{','.join(map(str, bands))}|{speeds}|{fen}"
        if key in self._cache:
            return self._cache[key]
        if self.persistent:
            from . import store
            cached = store.explorer_get(key)
            if cached is not None:
                info = OpeningInfo(**cached)
                self._cache[key] = info
                return info
        if not self.available():
            return OpeningInfo()
        info = OpeningInfo()
        try:
            r = self._session.get(PLAYERS_URL, params={"variant": "standard", "fen": fen, "moves": 5,
                                                       "topGames": 0, "recentGames": 0,
                                                       "ratings": ",".join(map(str, bands)), "speeds": speeds},
                                  timeout=self.timeout_s)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            with self._state_lock:
                OpeningExplorer._disabled_until = time.monotonic() + _BACKOFF_S
            return info
        opening = data.get("opening") or {}
        info.name = british(opening.get("name", ""))
        info.eco = opening.get("eco", "")
        for m in data.get("moves", []):
            total = m.get("white", 0) + m.get("draws", 0) + m.get("black", 0)
            if total == 0:
                continue
            info.top_moves.append({
                "san": m["san"], "games": total,
                "white_pct": round(100 * m["white"] / total),
                "draw_pct": round(100 * m["draws"] / total),
                "black_pct": round(100 * m["black"] / total),
            })
        self._cache[key] = info
        if self.persistent:
            from . import store
            store.explorer_put(key, asdict(info))
        return info

    @staticmethod
    def player_lines(info: OpeningInfo, elo: int, time_class: str = "", turn: bool | None = None) -> list[str]:
        """Prompt-ready facts about what players around `elo` do here and how it goes for them.

        `turn` (chess.WHITE / chess.BLACK) is the side to move; with it, a popular move that scores badly
        for its own side is named as the typical mistake of this level, and the best-scoring one as the
        better choice.
        """
        moves = [m for m in info.top_moves if m["games"] >= 10]
        if not moves:
            return []
        total = sum(m["games"] for m in moves)
        where = f"{time_class} games" if time_class in _SPEEDS else "games"
        out = [f"Players rated around {elo} ({total} {where} in the Lichess database) play here: "
               + ", ".join(f"{m['san']} ({round(100 * m['games'] / total)}% of games, "
                           f"{m['white_pct']}% W / {m['draw_pct']}% D / {m['black_pct']}% B)"
                           for m in moves[:4]) + "."]
        if turn is None:
            return out
        side, key = ("White", "white_pct") if turn else ("Black", "black_pct")
        score = lambda m: m[key] + m["draw_pct"] / 2  # noqa: E731  (points per game, in %)
        popular = moves[0]
        common = [m for m in moves if m["games"] >= 0.2 * total and m["games"] >= 30]
        best = max(common or moves, key=score)
        if popular["games"] >= 30 and score(popular) <= 42:
            text = (f"At this level {popular['san']} is the most common choice but scores only "
                    f"{round(score(popular))}% for {side}: the typical mistake here")
            if best is not popular and score(best) - score(popular) >= 8:
                text += f"; {best['san']} scores {round(score(best))}%"
            out.append(text + ".")
        elif best is not popular and score(best) - score(popular) >= 10:
            out.append(f"{best['san']} scores best for {side} at this level ({round(score(best))}% against "
                       f"{round(score(popular))}% for the more popular {popular['san']}).")
        return out

    @staticmethod
    def summary_lines(info: OpeningInfo) -> list[str]:
        if not info.known:
            return []
        out = []
        if info.name:
            out.append(f"Opening: {info.name} ({info.eco}).")
        moves = ", ".join(
            f"{m['san']} ({m['games']} master games, {m['white_pct']}% W / {m['draw_pct']}% D / {m['black_pct']}% B)"
            for m in info.top_moves[:4]
        )
        out.append(f"Master-game moves here: {moves}.")
        return out
