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
