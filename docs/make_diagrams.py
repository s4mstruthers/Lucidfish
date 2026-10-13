#!/usr/bin/env python3
# Copyright (C) 2026 Sam Struthers
# SPDX-License-Identifier: AGPL-3.0-or-later
# This file is part of Lucidfish <https://github.com/s4mstruthers/Lucidfish>; see LICENSE for the terms.
"""Draw the README's workflow diagrams as SVG, in a light and a dark variant.

    python docs/make_diagrams.py        # writes docs/pipeline-{light,dark}.svg and docs/layers-{light,dark}.svg

The README embeds each pair in a <picture> element, so GitHub shows the variant that matches the
reader's theme. Edit the text here, not in the SVG files: they are generated.
"""
from __future__ import annotations

from pathlib import Path

DOCS = Path(__file__).resolve().parent
FONT = "-apple-system, 'Segoe UI', Inter, Roboto, Helvetica, Arial, sans-serif"

THEMES = {
    "light": {"ink": "#0f172a", "muted": "#64748b", "edge": "#cbd5e1", "accent": "#4f46e5",
              "accent_fill": "#eef2ff", "tint": "#f8fafc", "ok": "#15803d"},
    "dark": {"ink": "#e5e7eb", "muted": "#94a3b8", "edge": "#475569", "accent": "#818cf8",
             "accent_fill": "#1e1b4b", "tint": "#1e293b", "ok": "#4ade80"},
}


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Canvas:
    """A tiny SVG builder: boxes with a title and body lines, arrows, labels."""

    def __init__(self, width: int, height: int, theme: dict, title: str):
        self.w, self.h, self.t = width, height, theme
        self.parts = [f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {width} {height}' width='{width}' "
                      f"height='{height}' role='img' aria-label='{esc(title)}' font-family=\"{FONT}\" font-size='14'>",
                      "<defs>" + self._marker("arrow", theme["edge"]) + self._marker("arrow-accent", theme["accent"])
                      + "</defs>"]

    @staticmethod
    def _marker(mid: str, colour: str) -> str:
        return (f"<marker id='{mid}' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7' "
                f"orient='auto-start-reverse'><path d='M0 0L10 5L0 10z' fill='{colour}'/></marker>")

    def text(self, x, y, s, size=14, weight=400, fill=None, anchor="start", italic=False):
        fill = fill or self.t["ink"]
        style = " font-style='italic'" if italic else ""
        self.parts.append(f"<text x='{x}' y='{y}' font-size='{size}' font-weight='{weight}' fill='{fill}' "
                          f"text-anchor='{anchor}'{style}>{esc(s)}</text>")

    def box(self, x, y, w, h, title, lines, accent=False, tint=False, tag=None):
        t = self.t
        fill = t["accent_fill"] if accent else (t["tint"] if tint else "none")
        stroke = t["accent"] if accent else t["edge"]
        self.parts.append(f"<rect x='{x}' y='{y}' width='{w}' height='{h}' rx='10' fill='{fill}' stroke='{stroke}' "
                          f"stroke-width='{2 if accent else 1.5}'/>")
        self.text(x + 16, y + 26, title, 15, 600)
        if tag:
            self.text(x + w - 14, y + 26, tag, 11.5, 500, t["muted"], anchor="end")
        for i, line in enumerate(lines):
            self.text(x + 16, y + 48 + 18 * i, line, 12.5, 400, t["muted"] if line.startswith("·") else t["ink"])

    def arrow(self, d, accent=False, dashed=False):
        c = self.t["accent"] if accent else self.t["edge"]
        dash = " stroke-dasharray='5 4'" if dashed else ""
        self.parts.append(f"<path d='{d}' fill='none' stroke='{c}' stroke-width='1.6'{dash} "
                          f"marker-end='url(#{'arrow-accent' if accent else 'arrow'})'/>")

    def line(self, x1, y1, x2, y2):
        self.parts.append(f"<line x1='{x1}' y1='{y1}' x2='{x2}' y2='{y2}' stroke='{self.t['edge']}' "
                          "stroke-width='1.2'/>")

    def save(self, path: Path):
        self.parts.append("</svg>")
        path.write_text("\n".join(self.parts) + "\n", encoding="utf-8")


def pipeline(theme: dict) -> Canvas:
    """One game through Lucidfish: the engine and the code establish the facts, the model writes, the
    fact-checker reads it back against the board."""
    W, H = 1180, 440
    c = Canvas(W, H, theme, "How Lucidfish analyses a game")
    m = theme["muted"]
    c.text(0, 22, "How Lucidfish analyses a game", 18, 700)
    c.text(0, 44, "The engine and the code decide what is true; the model only puts it into words; every sentence is "
                  "checked before you read it.", 13, 400, m)

    bw, gap, bh = 250, 60, 108
    xs = [i * (bw + gap) for i in range(4)]
    r1, r2 = 68, 270
    # Row 1: establishing the facts, left to right.
    c.box(xs[0], r1, bw, bh, "Your game",
          ["A PGN, or games fetched from", "chess.com or Lichess", "· every move by both sides"])
    c.box(xs[1], r1, bw, bh, "Stockfish", ["Best lines and winning chances", "for every move; deeper search",
                                           "when a verdict looks suspicious"], tag="separate process, UCI")
    c.box(xs[2], r1, bw, bh, "Board checks", ["Hanging pieces, forks, pins, mate", "threats, king safety, structure",
                                              "· what every line actually leads to"], tag="Python")
    c.box(xs[3], r1, bw, bh, "Specialists", ["Missed threats, endgame technique,", "where you left opening theory",
                                             "· what masters and your peers play"], tag="Python")
    for i in range(3):
        c.arrow(f"M{xs[i] + bw} {r1 + bh / 2}H{xs[i + 1]}")
    # Row 2: writing and checking, right to left under row 1.
    c.box(xs[3], r2, bw, bh, "Coach model", ["Ollama on your computer, or a", "cloud model with your own key",
                                             "· never analyses the position itself"], accent=True, tag="local or cloud")
    c.box(xs[2], r2, bw, bh, "Fact-checker", ["Every move, piece, capture and", "'who is better' claim is checked;",
                                               "wrong sentences fixed or removed"], tag="Python")
    c.box(xs[1], r2, bw, bh, "What you read", ["The story of the game, a note on", "every move, the post-game review,",
                                               "Learn concepts and Train puzzles"], tint=True)
    # Evidence pack down into the coach, then leftwards.
    cx = xs[3] + bw / 2
    c.arrow(f"M{cx + 40} {r1 + bh}V{r2}", accent=True)
    c.text(cx + 30, r1 + bh + 40, "verified evidence", 12, 500, m, anchor="end")
    c.text(cx + 30, r1 + bh + 57, "as structured JSON", 12, 500, m, anchor="end")
    c.arrow(f"M{xs[3]} {r2 + bh / 2}H{xs[2] + bw}", accent=True)
    c.text(xs[2] + bw + gap / 2, r2 + bh / 2 - 22, "draft", 12, 500, m, anchor="middle")
    c.text(xs[2] + bw + gap / 2, r2 + bh / 2 - 8, "note", 12, 500, m, anchor="middle")
    c.arrow(f"M{xs[2]} {r2 + bh / 2}H{xs[1] + bw}")
    c.text(xs[1] + bw + gap / 2, r2 + bh / 2 - 8, "verified", 12, 500, m, anchor="middle")
    # check_line: the coach may ask Stockfish to test a variation.
    mid = r1 + bh + (r2 - r1 - bh) / 2
    c.arrow(f"M{cx - 40} {r2}V{mid}H{xs[1] + bw / 2}V{r1 + bh}", dashed=True)
    c.text(xs[1] + bw / 2 + 14, mid - 8, "check_line: the coach can ask Stockfish to test a variation", 12, 500, m)
    # Footer: the eval harness.
    c.text(0, r2 + bh + 44, "lucidfish eval runs this pipeline on engine-verified positions and scores any model on "
                            "how faithfully it explains them.", 12.5, 400, m)
    return c


def layers(theme: dict) -> Canvas:
    """The narrators build on each other: code checks the board, each written layer combines the one below."""
    W, H = 1180, 450
    c = Canvas(W, H, theme, "The layers of a review")
    m = theme["muted"]
    c.text(0, 22, "Each written layer combines the verified layer below it", 18, 700)
    c.text(0, 44, "Only the combining is done by the model; everything it combines has been checked, and the result "
                  "is checked again.", 13, 400, m)
    rows = [
        ("Coach profile", "Your recurring weaknesses, patterns across games, and the progress review; referred to in "
                          "later games.", "from the reviews and the findings across games"),
        ("Post-game review", "What decided the game, the costliest mistakes and what was better, what went well.",
         "from the chapter summaries and verified game facts"),
        ("Chapters", "The game split where the phase changes and at its turning points, each with a summary.",
         "from the commentary, split by the engine's evaluation"),
        ("Commentary and notes", "A line on the purpose of every move, eight moves at a time; a full explanation of "
                                 "every mistake.", "from the evidence for each stretch of moves"),
        ("Facts from code", "Engine lines and what they lead to, tactics on the board, plans, threats, endgame "
                            "technique, opening theory.", "Stockfish + Python, no model involved"),
    ]
    top, rh, gap = 64, 56, 20
    left, width = 0, 800
    for i, (name, what, src) in enumerate(rows):
        y = top + i * (rh + gap)
        base = i == len(rows) - 1
        fill = theme["tint"] if base else "none"
        stroke = theme["ok"] if base else theme["edge"]
        c.parts.append(f"<rect x='{left}' y='{y}' width='{width}' height='{rh}' rx='10' fill='{fill}' "
                       f"stroke='{stroke}' stroke-width='{2 if base else 1.5}'/>")
        c.text(left + 16, y + 24, name, 15, 600)
        c.text(left + 16, y + 44, what, 12.5, 400)
        c.text(left + width + 16, y + 24, "verified by code" if base else "written by the model", 12, 600,
               theme["ok"] if base else theme["accent"])
        c.text(left + width + 16, y + 44, src, 11.5, 400, m)
        if not base:
            # The arrow from the layer below into this one.
            c.arrow(f"M{left + width / 2} {y + rh + gap}V{y + rh}")
    c.text(0, top + 5 * (rh + gap) + 16, "The fact-checker reads every layer back against the board and the game: "
                                         "a move must be playable by the side named, a piece must stand where the "
                                         "text says, a result must be right.", 12.5, 400, m)
    return c


def main() -> None:
    for name, theme in THEMES.items():
        pipeline(theme).save(DOCS / f"pipeline-{name}.svg")
        layers(theme).save(DOCS / f"layers-{name}.svg")
    print("wrote", ", ".join(f"{d}-{t}.svg" for d in ("pipeline", "layers") for t in THEMES))


if __name__ == "__main__":
    main()
