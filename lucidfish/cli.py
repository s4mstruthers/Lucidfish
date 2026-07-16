"""CLI: `python -m lucidfish path/to/game.pgn`"""

from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.panel import Panel

from .config import Config
from .pipeline import analyze_game

BADGES = {
    "best": "[green]✓ best[/green]",
    "good": "[green]good[/green]",
    "inaccuracy": "[yellow]?! inaccuracy[/yellow]",
    "mistake": "[dark_orange]? mistake[/dark_orange]",
    "blunder": "[red]?? blunder[/red]",
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lucidfish", description="Stockfish tells you the best move; Lucidfish tells you why.")
    p.add_argument("pgn", nargs="?", default=None, help="path to a PGN file (or '-' for stdin)")
    p.add_argument("--chesscom", metavar="USERNAME", default=None,
                   help="fetch your most recent chess.com game instead of a PGN file")
    p.add_argument("--lichess", metavar="USERNAME", default=None,
                   help="fetch your most recent Lichess game instead of a PGN file")
    p.add_argument("--recent", type=int, default=1,
                   help="with --chesscom/--lichess: which recent game (1 = latest, 2 = one before, ...)")
    p.add_argument("--side", choices=["white", "black"], default=None,
                   help="only generate explanations for this side's moves")
    p.add_argument("--depth", type=int, default=None, help="engine search depth (default 18)")
    p.add_argument("--elo", type=int, default=None,
                   help="your rating — tailors explanation depth and lessons to your level")
    p.add_argument("--fast", action="store_true",
                   help="cap engine at 0.3s/position instead of fixed depth (~3x faster, slightly less precise)")
    p.add_argument("--model", default=None, help="Ollama model name (default qwen2.5:14b)")
    p.add_argument("--explain-all", action="store_true", help="explain every move, not just critical ones")
    p.add_argument("--out", default=None, help="also write a markdown report to this path")
    args = p.parse_args(argv)

    cfg = Config()
    if args.depth:
        cfg.engine.depth = args.depth
    if args.fast:
        cfg.engine.movetime_s = 0.3
        cfg.engine.multipv = 3
    if args.model:
        cfg.llm.model = args.model
    cfg.analysis.explain_all = args.explain_all
    cfg.user_elo = args.elo

    console = Console()

    # No PGN and no explicit fetch flag → fall back to the configured chess.com account.
    if not args.pgn and not args.chesscom and not args.lichess and cfg.chesscom_user:
        args.chesscom = cfg.chesscom_user

    username = args.chesscom or args.lichess
    if args.chesscom or args.lichess:
        from .fetch import chesscom_recent_pgns, lichess_recent_pgns
        site = "chess.com" if args.chesscom else "Lichess"
        console.print(f"Fetching game {args.recent} back from {site} for [bold]{username}[/bold]…")
        fetch = chesscom_recent_pgns if args.chesscom else lichess_recent_pgns
        pgn_text = fetch(username, n=args.recent)[args.recent - 1]
    elif args.pgn:
        pgn_text = sys.stdin.read() if args.pgn == "-" else open(args.pgn).read()
    else:
        p.error("provide a PGN file, or --chesscom/--lichess USERNAME")

    # Auto-detect which colour the user played, so explanations address them.
    if args.side is None and username:
        import io as _io
        import chess.pgn as _pgn
        headers = _pgn.read_headers(_io.StringIO(pgn_text)) or {}
        if headers.get("White", "").lower() == username.lower():
            args.side = "white"
        elif headers.get("Black", "").lower() == username.lower():
            args.side = "black"
        if args.side:
            console.print(f"You played [bold]{args.side.capitalize()}[/bold] — coaching that side.")

    status = console.status("starting engine…")
    status.start()

    def progress(move_no, side, san):
        status.update(f"analysing move {move_no} ({side}): {san}")

    try:
        report = analyze_game(pgn_text, cfg, side_filter=args.side, progress=progress)
    finally:
        status.stop()

    h = report.headers
    console.print(Panel(f"[bold]{h.get('White','?')} vs {h.get('Black','?')}[/bold]  "
                        f"{h.get('Result','')}  {h.get('Date','')}", title="Lucidfish"))

    md_lines = [f"# {h.get('White','?')} vs {h.get('Black','?')} — {h.get('Result','')}", ""]
    for m in report.moves:
        prefix = f"{m.move_number}." if m.side == "White" else f"{m.move_number}…"
        badge = BADGES.get(m.classification, m.classification)
        console.print(f"[bold]{prefix} {m.san}[/bold]  {badge}  eval {m.eval_str}"
                      + (f"  (best: {m.best_san})" if m.san != m.best_san else ""))
        md_lines.append(f"## {prefix} {m.san} — {m.classification} (eval {m.eval_str})")
        if m.explanation:
            console.print(f"   [dim]{m.explanation}[/dim]\n")
            md_lines.append(m.explanation)
        md_lines.append("")

    if report.review:
        console.print(Panel(report.review, title="Post-game review", border_style="cyan"))
        md_lines += ["", "# Post-game review", "", report.review, ""]

    if args.out:
        with open(args.out, "w") as f:
            f.write("\n".join(md_lines))
        console.print(f"\nMarkdown report written to {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
