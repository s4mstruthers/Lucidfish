"""Command-line interface.

    lucidfish game.pgn                      analyse a PGN file
    lucidfish --chesscom USER               your latest chess.com game
    lucidfish --lichess USER --recent 2     your second-latest Lichess game
    lucidfish --check                       verify Stockfish and the AI coach are set up
    lucidfish web                           start the web app
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

from . import __version__

BADGES = {
    "best": "[green]best[/green]",
    "good": "[green]good[/green]",
    "inaccuracy": "[yellow]?! inaccuracy[/yellow]",
    "mistake": "[dark_orange]? mistake[/dark_orange]",
    "blunder": "[red]?? blunder[/red]",
}


def build_parser() -> argparse.ArgumentParser:
    from .config import DEPTH_PRESETS
    from .llm import PROVIDERS
    from .pipeline import DETAIL_LEVELS

    p = argparse.ArgumentParser(
        prog="lucidfish",
        description="Stockfish tells you the best move; Lucidfish tells you why.",
        epilog="Run `lucidfish web` for the browser app. Settings saved in the web app "
               "(AI provider, model, depth) are used here too; flags override them.")
    p.add_argument("pgn", nargs="?", default=None, help="PGN file to analyse ('-' reads stdin)")
    src = p.add_argument_group("game source")
    src.add_argument("--chesscom", metavar="USER", help="fetch a recent chess.com game")
    src.add_argument("--lichess", metavar="USER", help="fetch a recent Lichess game")
    src.add_argument("--recent", type=int, default=1, metavar="N",
                     help="with --chesscom/--lichess: 1 = latest game, 2 = the one before, ...")
    coach = p.add_argument_group("coaching")
    coach.add_argument("--side", choices=["white", "black"], help="coach this side (auto-detected for fetched games)")
    coach.add_argument("--elo", type=int, help="your rating — tailors explanations to your level")
    coach.add_argument("--detail", choices=DETAIL_LEVELS,
                       help="key = mistakes and critical moments only; standard = commentary on every move "
                            "plus notes on mistakes (default); full = a detailed note on every move")
    coach.add_argument("--explain-all", action="store_true", help="same as --detail full")
    coach.add_argument("--provider", choices=list(PROVIDERS), help="AI provider (default: ollama)")
    coach.add_argument("--model", help="model name, e.g. llama3.1:8b or gpt-4.1-mini")
    coach.add_argument("--no-llm", action="store_true", help="engine analysis only (fast, no AI coach)")
    coach.add_argument("--expert-provider", choices=[*PROVIDERS, "none"],
                       help="second, stronger model for notes on mistakes, the review and escalations "
                            "(e.g. anthropic); 'none' = main model does everything")
    coach.add_argument("--expert-model", help="model for --expert-provider (default: that provider's default)")
    eng = p.add_argument_group("engine")
    eng.add_argument("--preset", choices=list(DEPTH_PRESETS), help="engine strength preset")
    eng.add_argument("--depth", type=int, help="search depth per position (default 18)")
    eng.add_argument("--fast", action="store_true", help="0.3 s per position instead of a fixed depth")
    eng.add_argument("--threads", type=int, help="CPU threads for Stockfish")
    eng.add_argument("--no-cache", action="store_true", help="ignore stored engine results")
    out = p.add_argument_group("output")
    out.add_argument("--out", metavar="FILE.md", help="write a Markdown report")
    out.add_argument("--pgn-out", metavar="FILE.pgn",
                     help="write an annotated PGN (for Lichess studies, ChessBase, ...)")
    out.add_argument("--json", metavar="FILE.json", help="write the full analysis as JSON")
    p.add_argument("--check", action="store_true", help="check that Stockfish and the AI coach work, then exit")
    p.add_argument("--version", action="version", version=f"lucidfish {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["share"]:
        return _share_command(argv[1:])
    if argv[:1] == ["web"]:
        from .web import main as web_main
        return web_main(argv[1:])

    args = build_parser().parse_args(argv)
    console = Console()
    from .config import DEPTH_PRESETS
    from .settings import load_config

    cfg = load_config(provider=args.provider, model=args.model,
                      expert_provider=args.expert_provider, expert_model=args.expert_model)
    if args.preset:
        cfg.engine.depth = DEPTH_PRESETS[args.preset]
    if args.depth:
        cfg.engine.depth = args.depth
    if args.fast:
        cfg.engine.movetime_s = 0.3
    if args.threads:
        cfg.engine.threads = args.threads
    if args.no_cache:
        cfg.engine.use_cache = False
    if args.no_llm:
        cfg.llm.enabled = False
    if args.explain_all:
        cfg.analysis.detail = "full"
    elif args.detail:
        cfg.analysis.detail = args.detail
    cfg.user_elo = args.elo

    if args.check:
        return _check(cfg, console)

    try:
        pgn_text, username = _load_pgn(args, cfg, console)
    except (OSError, RuntimeError, ValueError) as e:
        console.print(f"[red]Error:[/red] {e}")
        return 1
    if args.side is None and username:
        args.side = _detect_side(pgn_text, username)
        if args.side:
            console.print(f"You played [bold]{args.side.capitalize()}[/bold] — coaching that side.")

    from . import store
    from .jobs import rating_for_game
    from .pipeline import analyze_game
    if cfg.user_elo is None:   # no --elo: the game's own rating, else the active profile's
        cfg.user_elo, cfg.user_elo_label = rating_for_game(pgn_text, args.side, None,
                                                           store.get_profile(store.active_id()))
    columns = (TextColumn("[bold]{task.description:<8}"), BarColumn(), MofNCompleteColumn(),
               TextColumn("{task.fields[label]}"), TimeElapsedColumn())
    try:
        with Progress(*columns, console=console, transient=True) as bar:
            tasks = {"engine": bar.add_task("engine", total=None, label=""),
                     "coach": bar.add_task("coach" if cfg.llm.enabled else "moves", total=None, label="")}

            def progress(stage: str, done: int, total: int, label: str) -> None:
                if stage in tasks:
                    bar.update(tasks[stage], completed=done, total=total, label=label)
                elif stage == "review":
                    bar.update(tasks["coach"], label=label[:1].lower() + label[1:])

            report = analyze_game(pgn_text, cfg, side_filter=args.side, progress=progress,
                                  engine_cache=store.EngineCache() if cfg.engine.use_cache else None)
    except (RuntimeError, ValueError) as e:
        console.print(f"[red]Error:[/red] {e}")
        return 1
    except KeyboardInterrupt:
        console.print("[yellow]Stopped.[/yellow]")
        return 130

    _print_report(report, console)
    moves = [m.to_dict() for m in report.moves]
    from .export import annotated_pgn, markdown_report
    for path, render in ((args.out, lambda: markdown_report(report.headers, moves, report.review, report.opening,
                                                            report.accuracy, args.side, report.chapters)),
                         (args.pgn_out, lambda: annotated_pgn(pgn_text, moves, report.review, report.accuracy,
                                                              report.chapters)),
                         (args.json, lambda: json.dumps({"headers": report.headers, "opening": report.opening,
                                                         "accuracy": report.accuracy, "review": report.review,
                                                         "chapters": report.chapters,
                                                         "warnings": report.warnings, "moves": moves}, indent=2))):
        if path:
            Path(path).write_text(render(), encoding="utf-8")
            console.print(f"Wrote {path}", soft_wrap=True)
    return 0


def _load_pgn(args, cfg, console: Console) -> tuple[str, str | None]:
    if not args.pgn and not args.chesscom and not args.lichess:
        if cfg.chesscom_user:
            args.chesscom = cfg.chesscom_user
        elif cfg.lichess_user:
            args.lichess = cfg.lichess_user
        else:
            raise ValueError("Give a PGN file, or --chesscom/--lichess USERNAME. See `lucidfish --help`.")
    username = args.chesscom or args.lichess
    if username:
        from .fetch import FetchError, chesscom_recent_pgns, lichess_recent_pgns
        site = "chess.com" if args.chesscom else "Lichess"
        fetch = chesscom_recent_pgns if args.chesscom else lichess_recent_pgns
        n = max(1, args.recent)
        console.print(f"Fetching game {n} back from {site} for [bold]{username}[/bold]…")
        try:
            games = fetch(username, n=n)
        except FetchError as e:
            raise RuntimeError(str(e)) from e
        if len(games) < n:
            raise ValueError(f"Only {len(games)} game(s) found for {username}.")
        return games[n - 1], username
    if args.pgn == "-":
        return sys.stdin.read(), None
    return Path(args.pgn).read_text(encoding="utf-8", errors="replace"), None


def _detect_side(pgn_text: str, username: str) -> str | None:
    import chess.pgn
    headers = chess.pgn.read_headers(io.StringIO(pgn_text)) or {}
    if headers.get("White", "").lower() == username.lower():
        return "white"
    if headers.get("Black", "").lower() == username.lower():
        return "black"
    return None


def _print_report(report, console: Console) -> None:
    h = report.headers
    acc = report.accuracy or {}
    sub = []
    if report.opening:
        sub.append(report.opening)
    if acc.get("white") is not None:
        sub.append(f"accuracy — White {acc['white']}% · Black {acc['black']}%")
    sub.append(f"{report.engine or 'engine'} · coach: {report.coach or 'off (engine only)'}")
    console.print(Panel(f"[bold]{h.get('White', '?')} vs {h.get('Black', '?')}[/bold]  "
                        f"{h.get('Result', '')}  {h.get('Date', '')}\n[dim]" + "\n".join(sub) + "[/dim]",
                        title="Lucidfish", border_style="cyan"))
    for w in report.warnings:
        console.print(f"[yellow]⚠ {w}[/yellow]")
    for m in report.moves:
        prefix = f"{m.move_number}." if m.side == "White" else f"{m.move_number}..."
        badge = BADGES.get(m.classification, m.classification)
        extra = f"  (best: {m.best_san})" if m.classification != "best" and m.best_san else ""
        tags = f"  [magenta]{', '.join(m.tags)}[/magenta]" if m.tags else ""
        crit = "  [cyan]critical[/cyan]" if m.critical else ""
        console.print(f"[bold]{prefix} {m.san}[/bold]  {badge}  eval {m.eval_str}{extra}{crit}{tags}")
        chapter = next((c for c in report.chapters if c["start"] == m.ply), None)
        if chapter:
            console.print(Panel(chapter["summary"], title=chapter["title"], border_style="magenta"))
        if m.commentary:
            console.print(f"   [italic]{m.commentary}[/italic]")
        if m.explanation:
            console.print(f"   [dim]{m.explanation}[/dim]\n")
    if report.review:
        console.print(Panel(report.review, title="Post-game review", border_style="cyan"))


def _share_command(argv: list[str]) -> int:
    """`lucidfish share`: write a profile as a single web page anyone can open."""
    parser = argparse.ArgumentParser(prog="lucidfish share",
                                     description="Save a profile's analysed games as one web page that opens in any "
                                                 "browser, with nothing to install.")
    parser.add_argument("--profile", help="profile name (default: the active profile)")
    parser.add_argument("--out", help="file or folder to write to (default: the current folder)")
    parser.add_argument("--no-engine", action="store_true",
                        help="leave out the in-browser Stockfish (about 10 MB smaller; practice moves are then "
                             "judged only against the engine's stored top moves, and Explore shows no evaluation)")
    args = parser.parse_args(argv)
    from . import settings, share, store
    settings.load_config()
    profiles = store.list_profiles()
    if args.profile:
        match = [p for p in profiles if p["name"].lower() == args.profile.lower()]
        if not match:
            print(f"No profile called {args.profile!r}. Profiles: {', '.join(p['name'] for p in profiles) or 'none'}")
            return 1
        pid = match[0]["id"]
    else:
        pid = store.active_id()
        if not pid:
            print("There are no profiles yet. Create one in the web app first.")
            return 1
    profile = store.get_profile(pid)
    out = Path(args.out).expanduser() if args.out else Path.cwd()
    target = out / share.file_name(profile) if out.is_dir() else out
    target.write_text(share.build_html(pid, engine=not args.no_engine), encoding="utf-8")
    print(f"Wrote {target} ({target.stat().st_size / 1e6:.1f} MB) — open it in any web browser.")
    return 0


def _check(cfg, console: Console) -> int:
    """Diagnose the setup: engine, AI coach, storage."""
    from .config import data_dir
    from .engine import EngineAnalyzer
    from .llm import LLMError, make_provider
    ok = True
    try:
        with EngineAnalyzer(cfg.engine) as engine:
            console.print(f"[green]✔[/green] Engine: {engine.name} ({cfg.engine.path}), "
                          f"{cfg.engine.threads} threads, depth {cfg.engine.depth}", soft_wrap=True)
    except RuntimeError as e:
        ok = False
        console.print(f"[red]✘ Engine:[/red] {e}")
    if cfg.llm.enabled:
        try:
            result = make_provider(cfg.llm).check()
            console.print(f"[green]✔[/green] AI coach: {result['message']}")
        except LLMError as e:
            ok = False
            console.print(f"[red]✘ AI coach:[/red] {e}")
        if cfg.expert:
            try:
                result = make_provider(cfg.expert).check()
                console.print(f"[green]✔[/green] Second model: {result['message']}")
            except LLMError as e:
                ok = False
                console.print(f"[red]✘ Second model:[/red] {e} (the main model will do its work instead)")
    else:
        console.print("[yellow]•[/yellow] AI coach: disabled (engine-only mode)")
    console.print(f"[green]✔[/green] Data folder: {data_dir()}", soft_wrap=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
