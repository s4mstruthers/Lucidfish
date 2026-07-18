"""Opening layer: Lichess Opening Explorer client.

In the opening, "engine best" and "theory best" can differ — the explorer tells
us what strong humans actually play and what the opening is called, so early
advice stays consistent with real plans instead of raw engine output.

API docs: https://lichess.org/api#tag/Opening-Explorer (no auth needed, be polite).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import requests

EXPLORER_URL = "https://explorer.lichess.ovh/masters"

# Offline fallback book: longest SAN-prefix match. Used when the Lichess explorer
# is unreachable (some networks block Python clients), so opening identification
# never silently fails — an unverified LLM guess is worse than a coarse local name.
_LOCAL_BOOK: list[tuple[str, str, str]] = [
    # --- 1.e4 e5 ---
    ("e4 e5 Nf3 Nc6 Bc4 Bc5 b4", "Evans Gambit", "C51"),
    ("e4 e5 Nf3 Nc6 Bc4 Bc5", "Italian Game: Giuoco Piano", "C53"),
    ("e4 e5 Nf3 Nc6 Bc4 Nf6", "Italian Game: Two Knights Defense", "C55"),
    ("e4 e5 Nf3 Nc6 Bc4", "Italian Game", "C50"),
    ("e4 e5 Nf3 Nc6 Bb5 a6", "Ruy Lopez: Morphy Defense", "C70"),
    ("e4 e5 Nf3 Nc6 Bb5 Nf6", "Ruy Lopez: Berlin Defense", "C65"),
    ("e4 e5 Nf3 Nc6 Bb5", "Ruy Lopez", "C60"),
    ("e4 e5 Nf3 Nc6 d4", "Scotch Game", "C45"),
    ("e4 e5 Nf3 Nc6 Nc3 Nf6", "Four Knights Game", "C47"),
    ("e4 e5 Nf3 Nf6", "Petrov's Defense", "C42"),
    ("e4 e5 Nf3 d6", "Philidor Defense", "C41"),
    ("e4 e5 Nf3 Nc6", "King's Knight Opening", "C44"),
    ("e4 e5 Nc3", "Vienna Game", "C25"),
    ("e4 e5 f4 exf4", "King's Gambit Accepted", "C33"),
    ("e4 e5 f4", "King's Gambit", "C30"),
    ("e4 e5 Bc4", "Bishop's Opening", "C23"),
    ("e4 e5 d4", "Center Game", "C21"),
    ("e4 e5", "King's Pawn Game", "C20"),
    # --- Sicilian ---
    ("e4 c5 c3", "Sicilian Defense: Alapin Variation", "B22"),
    ("e4 c5 Nc3", "Sicilian Defense: Closed", "B23"),
    ("e4 c5 d4", "Sicilian Defense: Smith-Morra Gambit", "B21"),
    ("e4 c5", "Sicilian Defense", "B20"),
    # --- French ---
    ("e4 e6 d4 d5 e5", "French Defense: Advance Variation", "C02"),
    ("e4 e6 d4 d5 exd5", "French Defense: Exchange Variation", "C01"),
    ("e4 e6 d4 d5 Nd2", "French Defense: Tarrasch Variation", "C03"),
    ("e4 e6 d4 d5 Nc3 Bb4", "French Defense: Winawer Variation", "C15"),
    ("e4 e6 d4 d5 Nc3 Nf6", "French Defense: Classical Variation", "C11"),
    ("e4 e6", "French Defense", "C00"),
    # --- Caro-Kann ---
    ("e4 c6 d4 d5 e5", "Caro-Kann Defense: Advance Variation", "B12"),
    ("e4 c6 d4 d5 exd5", "Caro-Kann Defense: Exchange Variation", "B13"),
    ("e4 c6 d4 d5 Nc3 dxe4 Nxe4 Bf5", "Caro-Kann Defense: Classical Variation", "B18"),
    ("e4 c6 d4 d5 Nc3", "Caro-Kann Defense: Main Line", "B15"),
    ("e4 c6 Nc3 d5 Nf3", "Caro-Kann Defense: Two Knights Attack", "B11"),
    ("e4 c6", "Caro-Kann Defense", "B10"),
    # --- other e4 defences ---
    ("e4 d5 exd5 Qxd5", "Scandinavian Defense: Mieses-Kotroc", "B01"),
    ("e4 d5 exd5 Nf6", "Scandinavian Defense: Modern Variation", "B01"),
    ("e4 d5", "Scandinavian Defense", "B01"),
    ("e4 d6 d4 Nf6", "Pirc Defense", "B07"),
    ("e4 d6", "Pirc Defense", "B07"),
    ("e4 g6", "Modern Defense", "B06"),
    ("e4 Nf6", "Alekhine's Defense", "B02"),
    ("e4", "King's Pawn Opening", "B00"),
    # --- 1.d4 ---
    ("d4 d5 c4 dxc4", "Queen's Gambit Accepted", "D20"),
    ("d4 d5 c4 e6", "Queen's Gambit Declined", "D30"),
    ("d4 d5 c4 c6", "Slav Defense", "D10"),
    ("d4 d5 c4", "Queen's Gambit", "D06"),
    ("d4 d5 Bf4", "London System", "D02"),
    ("d4 d5 Nf3 Nf6 Bf4", "London System", "D02"),
    ("d4 Nf6 Bg5", "Trompowsky Attack", "A45"),
    ("d4 Nf6 c4 g6 Nc3 d5", "Grünfeld Defense", "D80"),
    ("d4 Nf6 c4 g6", "King's Indian Defense", "E60"),
    ("d4 Nf6 c4 e6 Nc3 Bb4", "Nimzo-Indian Defense", "E20"),
    ("d4 Nf6 c4 e6 Nf3 b6", "Queen's Indian Defense", "E12"),
    ("d4 Nf6 c4 e6 g3", "Catalan Opening", "E01"),
    ("d4 Nf6 c4 c5", "Benoni Defense", "A56"),
    ("d4 f5", "Dutch Defense", "A80"),
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
    top_moves: list[dict] = field(default_factory=list)  # [{san, games, white%, draw%, black%}]

    @property
    def known(self) -> bool:
        return bool(self.top_moves)


class OpeningExplorer:
    def __init__(self, timeout_s: int = 10):
        self.timeout_s = timeout_s
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "lucidfish/0.1 (personal chess study tool)"
        self._cache: dict[str, OpeningInfo] = {}

    def lookup(self, fen: str) -> OpeningInfo:
        """Masters-database stats for a position. Fails soft: network errors → empty info."""
        if fen in self._cache:
            return self._cache[fen]
        info = OpeningInfo()
        try:
            r = self._session.get(
                EXPLORER_URL, params={"fen": fen, "moves": 5, "topGames": 0},
                timeout=self.timeout_s,
            )
            r.raise_for_status()
            data = r.json()
            opening = data.get("opening") or {}
            info.name = opening.get("name", "")
            info.eco = opening.get("eco", "")
            for m in data.get("moves", []):
                total = m["white"] + m["draws"] + m["black"]
                if total == 0:
                    continue
                info.top_moves.append({
                    "san": m["san"],
                    "games": total,
                    "white_pct": round(100 * m["white"] / total),
                    "draw_pct": round(100 * m["draws"] / total),
                    "black_pct": round(100 * m["black"] / total),
                })
        except requests.RequestException:
            pass  # offline or rate-limited → just skip opening context
        self._cache[fen] = info
        return info

    def summary_lines(self, info: OpeningInfo) -> list[str]:
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
