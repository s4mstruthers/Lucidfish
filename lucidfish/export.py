"""Export an analysed game as an annotated PGN or a Markdown report.

Both work from the move dictionaries produced by ``AnnotatedMove.to_dict()``,
so freshly analysed games and games reopened from the database export the
same way. The annotated PGN imports cleanly into Lichess studies, ChessBase
and other GUIs: verdict symbols (?! ? ??), evaluations in the standard
``[%eval]`` format, the coach's notes as comments, and the engine's best line
as a variation after every error.
"""

from __future__ import annotations

import io
from datetime import date

import chess
import chess.pgn

from . import __version__

_NAGS = {
    "inaccuracy": chess.pgn.NAG_DUBIOUS_MOVE,   # ?!
    "mistake": chess.pgn.NAG_MISTAKE,           # ?
    "blunder": chess.pgn.NAG_BLUNDER,           # ??
}
_SYMBOLS = {"inaccuracy": "?!", "mistake": "?", "blunder": "??"}


def _eval_tag(ev: str) -> str:
    """UI eval string → PGN [%eval] value ('+0.35' → '0.35', '#-2' stays)."""
    if not ev or ev in ("1-0", "0-1", "1/2-1/2"):
        return ""
    return ev.lstrip("+") if not ev.startswith("#") else ev


def annotated_pgn(pgn_text: str, moves: list[dict], review: str = "",
                  accuracy: dict | None = None) -> str:
    """The original game with Lucidfish's verdicts, evals, notes and best lines."""
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        raise ValueError("Could not read the game's PGN.")
    game.headers["Annotator"] = f"Lucidfish {__version__}"
    if accuracy and accuracy.get("white") is not None:
        game.headers["WhiteAccuracy"] = f"{accuracy['white']}"
    if accuracy and accuracy.get("black") is not None:
        game.headers["BlackAccuracy"] = f"{accuracy['black']}"
    if review:
        game.comment = (game.comment + "\n" if game.comment else "") + review.strip()

    by_ply = {m.get("ply", i): m for i, m in enumerate(moves)}
    node = game
    ply = 0
    while node.variations:
        child = node.variations[0]
        m = by_ply.get(ply)
        if m and m.get("uci", child.move.uci()) == child.move.uci():
            parts = []
            tag = _eval_tag(m.get("eval", ""))
            if tag:
                parts.append(f"[%eval {tag}]")
            if m.get("expl"):
                parts.append(m["expl"].strip())
            if parts:
                child.comment = " ".join(filter(None, [child.comment.strip(), *parts]))
            cls = m.get("cls")
            if cls in _NAGS:
                child.nags.add(_NAGS[cls])
                best = next((c for c in m.get("candidates", []) if c.get("san") == m.get("best")), None)
                if best:
                    _add_line(node, [s["san"] for s in best.get("steps", [])],
                              f"Best was {m['best']}" + (f": {best['idea']}" if best.get("idea") else ""))
            elif m.get("critical"):
                child.nags.add(chess.pgn.NAG_GOOD_MOVE)   # !
        node = child
        ply += 1

    exporter = chess.pgn.StringExporter(headers=True, variations=True, comments=True)
    return game.accept(exporter) + "\n"


def _add_line(parent: chess.pgn.GameNode, sans: list[str], comment: str) -> None:
    board = parent.board()
    node = parent
    for i, san in enumerate(sans):
        try:
            move = board.parse_san(san)
        except ValueError:
            break
        node = node.add_variation(move)
        if i == 0:
            node.comment = comment
        board.push(move)


def markdown_report(headers: dict, moves: list[dict], review: str = "", opening: str = "",
                    accuracy: dict | None = None, coached_side: str | None = None) -> str:
    """A readable report: summary table, key moments, every coach note, review."""
    w, b = headers.get("White", "White"), headers.get("Black", "Black")
    out = [f"# {w} vs {b} — {headers.get('Result', '*')}", ""]
    meta = [("Date", headers.get("Date", "")), ("Event", headers.get("Event", "")), ("Opening", opening)]
    if accuracy:
        meta.append(("Accuracy", f"White {accuracy.get('white', '—')}% · Black {accuracy.get('black', '—')}%"))
    out += ["| | |", "|---|---|"] + [f"| {k} | {v} |" for k, v in meta if v] + [""]

    def label(m: dict) -> str:
        return f"{m['n']}{'.' if m['side'] == 'White' else '...'} {m['san']}{_SYMBOLS.get(m['cls'], '')}"

    errors = [m for m in moves if m["cls"] in _SYMBOLS
              and (not coached_side or m["side"].lower() == coached_side.lower())]
    if errors:
        out += ["## Key moments", ""]
        for m in errors:
            out.append(f"- **{label(m)}** — {m['cls']} (best: {m.get('best', '?')}, eval after: {m.get('eval', '')})")
        out.append("")
    out += ["## Moves", ""]
    for m in moves:
        line = f"**{label(m)}** · {m['cls']} · {m.get('eval', '')}"
        if m.get("best") and m["best"] != m["san"]:
            line += f" · best {m['best']}"
        out.append(line + "  ")
        if m.get("expl"):
            out.append(m["expl"].strip())
        out.append("")
    if review:
        out += ["## Post-game review", "", review.strip(), ""]
    out.append(f"*Generated by Lucidfish {__version__} on {date.today().isoformat()}.*")
    return "\n".join(out) + "\n"
