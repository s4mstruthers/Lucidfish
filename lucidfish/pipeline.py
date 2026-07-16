"""Pipeline: PGN in → per-move grounded explanations + whole-game review out.

Architecture (two threads, producer/consumer):
  producer  — Stockfish + feature extraction + opening lookup, one position ahead
  consumer  — LLM narration of the producer's evidence
Engine (CPU) and LLM (GPU) are independent, so overlapping them makes total time
≈ max(engine, llm) instead of engine + llm.
"""

from __future__ import annotations

import io
import queue
import re
import threading
from dataclasses import dataclass, field

import chess
import chess.pgn

from .config import Config
from .engine import EngineAnalyzer, MoveAnalysis, build_move_analysis, describe_move
from . import features as feat
from .opening import OpeningExplorer
from .llm import (OllamaProvider, SYSTEM_PROMPT, GAME_REVIEW_SYSTEM,
                  build_prompt, build_game_review_prompt, build_ideas_prompt)


@dataclass
class AnnotatedMove:
    move_number: int
    side: str                       # "White" / "Black"
    san: str
    classification: str
    cp_loss: int
    best_san: str
    eval_str: str                   # eval after the move, White's perspective
    explanation: str = ""           # empty for quiet moves unless explain_all
    line_ideas: dict = field(default_factory=dict)  # candidate SAN -> one-sentence idea
    opening: list[str] = field(default_factory=list)  # book stats shown without LLM cost
    critical: bool = False          # game-deciding moment (only-move found)
    think_s: float | None = None    # seconds spent on this move (from [%clk] annotations)
    clock_s: float | None = None    # clock remaining after the move
    analysis: MoveAnalysis | None = None


@dataclass
class GameReport:
    headers: dict
    moves: list[AnnotatedMove] = field(default_factory=list)
    review: str = ""            # whole-game narrative: opening, flow, lessons


def _white_pov_eval(a: MoveAnalysis, mover_is_white: bool) -> str:
    cp = a.eval_after_cp
    if cp is None:
        return "mate on the board"
    if not mover_is_white:
        cp = -cp
    return f"{cp / 100:+.2f}"


def _split_line_ideas(text: str) -> tuple[str, dict]:
    """Split LLM output into (explanation, {candidate_san: idea}).

    Expected format has labelled EXPLANATION / LINE IDEAS sections, but models
    drift — so fall back gracefully and never lose content.
    """
    body = re.sub(r"^\s*\**\s*EXPLANATION:?\s*\**\s*\n?", "", text, flags=re.I)
    parts = re.split(r"(?:^|\n)\s*\**\s*LINE IDEAS:?\s*\**\s*\n?", body, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return body.strip(), {}
    ideas = {}
    for ln in parts[1].splitlines():
        m = re.match(r"\s*(?:[-*\d.]+\s*)?\**([A-Za-z0-9+#=x-]+?)\**\s*:\s*(.+)", ln)
        if m:
            ideas[m.group(1).strip()] = m.group(2).strip()
    explanation = parts[0].strip()
    if not explanation and ideas:
        # Model skipped the prose — synthesise something rather than showing nothing.
        explanation = "See the engine lines below for the key ideas in this position."
    return explanation, ideas


def _norm_san(s: str) -> str:
    """Normalise SAN for fuzzy idea matching (models drop/add check symbols)."""
    return re.sub(r"[+#x=]", "", s).strip()


def _ensure_line_ideas(llm, analysis, game_so_far: str, line_ideas: dict, side: str = "the player") -> dict:
    """Guarantee an idea sentence for each top-3 candidate.

    First fuzzy-match keys the model DID produce (Bb5 vs Bb5+ etc.); if any are
    still missing, make one minimal follow-up call asking only for those.
    """
    # the played move gets its own full explanation — no idea sentence needed for it
    top = [l for l in analysis.candidates[:3] if l.move_san != analysis.played_san]
    nmap = {_norm_san(k): v for k, v in line_ideas.items()}
    for l in top:
        if l.move_san not in line_ideas and _norm_san(l.move_san) in nmap:
            line_ideas[l.move_san] = nmap[_norm_san(l.move_san)]

    missing = [l for l in top if l.move_san not in line_ideas]
    if missing:
        try:
            resp = llm.generate(SYSTEM_PROMPT,
                                build_ideas_prompt(missing, game_so_far, analysis.fen_before, side))
            _, extra = _split_line_ideas("LINE IDEAS:\n" + resp)
            emap = {_norm_san(k): v for k, v in extra.items()}
            for l in missing:
                idea = extra.get(l.move_san) or emap.get(_norm_san(l.move_san))
                if idea:
                    line_ideas[l.move_san] = idea
        except Exception:
            pass  # ideas are decoration — never fail the move over them
    return line_ideas


def _parse_time_control(tc: str) -> tuple[int | None, int, str]:
    """'600+5' -> (600, 5, 'rapid'). Returns (base_s, increment_s, class)."""
    if not tc or "/" in tc:   # daily/correspondence or unknown
        return None, 0, "daily" if tc and "/" in tc else ""
    try:
        parts = tc.split("+")
        base = int(parts[0])
        inc = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return None, 0, ""
    cls = ("bullet" if base < 180 else "blitz" if base < 600
           else "rapid" if base < 1800 else "classical")
    return base, inc, cls


def _fmt_clock(s: float | None) -> str:
    if s is None:
        return ""
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


def analyze_game(
    pgn_text: str,
    cfg: Config,
    side_filter: str | None = None,   # "white", "black", or None for both
    progress=None,                    # optional callback(move_number, side, san)
    on_move=None,                     # optional callback(AnnotatedMove) after each move completes
    should_stop=None,                 # optional callable() -> bool; True aborts cleanly with partial results
) -> GameReport:
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        raise ValueError("Could not parse a game from the PGN input.")

    report = GameReport(headers=dict(game.headers))
    llm = OllamaProvider(cfg.llm)
    work: queue.Queue = queue.Queue(maxsize=4)   # producer stays a few plies ahead
    opening_name_holder = [""]
    stop = should_stop or (lambda: False)

    def _put(item) -> bool:
        """put that never deadlocks: gives up when a stop is requested."""
        while True:
            if stop():
                return False
            try:
                work.put(item, timeout=0.5)
                return True
            except queue.Full:
                continue

    # ---------------- producer: engine + features + opening book -------------
    def produce():
        explorer = OpeningExplorer()
        base_s, inc_s, time_class = _parse_time_control(game.headers.get("TimeControl", ""))
        prev_clock = {True: float(base_s) if base_s else None,
                      False: float(base_s) if base_s else None}
        try:
            with EngineAnalyzer(cfg.engine) as engine:
                board = game.board()
                root = game.board()
                moves_so_far: list[chess.Move] = []
                candidates = engine.top_lines(board)
                for ply, node in enumerate(game.mainline()):
                    if stop():
                        break
                    move = node.move
                    # per-move thinking time from [%clk] PGN annotations (chess.com/Lichess)
                    mover_is_white_now = board.turn == chess.WHITE
                    clock = node.clock()
                    think_s = None
                    if clock is not None and prev_clock[mover_is_white_now] is not None:
                        think_s = max(0.0, prev_clock[mover_is_white_now] - clock + inc_s)
                    if clock is not None:
                        prev_clock[mover_is_white_now] = clock
                    f_before = feat.extract(board)

                    after = board.copy(stack=False)
                    after.push(move)
                    lines_after = [] if after.is_game_over() else engine.top_lines(after)
                    analysis = build_move_analysis(board, move, candidates, lines_after, cfg.analysis)

                    opening_lines: list[str] = []
                    if ply < cfg.analysis.opening_book_plies:
                        info = explorer.lookup(board.fen())
                        opening_lines = explorer.summary_lines(info)
                        if info.name:
                            opening_name_holder[0] = f"{info.name} ({info.eco})"

                    game_so_far = root.variation_san(moves_so_far) if moves_so_far else "(game start)"
                    mover_is_white = board.turn == chess.WHITE

                    board.push(move)
                    f_after = feat.extract(board)
                    candidates = lines_after
                    moves_so_far.append(move)

                    if not _put({
                        "move": move,
                        "mover_is_white": mover_is_white,
                        "analysis": analysis, "f_before": f_before, "f_after": f_after,
                        "opening_lines": opening_lines, "game_so_far": game_so_far,
                        "opening_name": opening_name_holder[0],
                        "think_s": think_s, "clock_s": clock, "time_class": time_class,
                    }):
                        break
            _put(None)                            # done (or stopped)
        except Exception as e:
            _put(("error", e))

    producer = threading.Thread(target=produce, daemon=True)
    producer.start()

    # ---------------- consumer: LLM narration --------------------------------
    while True:
        if stop():
            break
        try:
            item = work.get(timeout=0.5)
        except queue.Empty:
            continue
        if item is None:
            break
        if isinstance(item, tuple) and item[0] == "error":
            raise item[1]

        analysis: MoveAnalysis = item["analysis"]
        mover_is_white = item["mover_is_white"]
        side = "White" if mover_is_white else "Black"
        pre = chess.Board(analysis.fen_before)
        move_number = pre.fullmove_number

        if progress:
            progress(move_number, side, analysis.played_san)

        skip_llm_side = (side_filter is not None and side.lower() != side_filter.lower())

        # "Critical moment": the best move was far stronger than the alternatives,
        # and the player found it (or nearly) — these decided the game and deserve
        # an explanation even though nothing went wrong.
        cands = analysis.candidates
        critical = (
            len(cands) >= 2
            and cands[0].score_cp is not None and cands[1].score_cp is not None
            and (cands[0].score_cp - cands[1].score_cp) >= cfg.analysis.critical_gap_cp
            and analysis.classification in ("best", "good")
        )

        # Errors and critical moments get full forensic notes; quiet good moves get
        # a brief big-picture note (plan / preparation / prevention).
        full_note = (
            cfg.analysis.explain_all
            or analysis.classification in ("inaccuracy", "mistake", "blunder")
            or critical
        )

        # Time context: was this an instant move, or played in time trouble?
        time_note = ""
        if item["think_s"] is not None:
            time_note = (f"{side} spent {item['think_s']:.0f} seconds on this move"
                         + (f" ({_fmt_clock(item['clock_s'])} remaining)" if item["clock_s"] is not None else "")
                         + (f", in a {item['time_class']} game" if item["time_class"] else "") + ".")

        explanation, line_ideas = "", {}
        prompt = build_prompt(move_number, side, analysis,
                              item["f_before"], item["f_after"],
                              item["opening_lines"],
                              move_desc=describe_move(pre, item["move"]) + ".",
                              game_so_far=item["game_so_far"],
                              critical=critical,
                              brief=not full_note,
                              # opponent's move → short "what are they up to" note
                              opponent_of=side_filter.capitalize() if skip_llm_side else None,
                              opening_name=item["opening_name"],
                              time_note=time_note,
                              elo=cfg.user_elo)
        explanation, line_ideas = _split_line_ideas(llm.generate(SYSTEM_PROMPT, prompt))
        if not skip_llm_side:   # opponent panels don't show engine lines, skip the cost
            line_ideas = _ensure_line_ideas(llm, analysis, item["game_so_far"], line_ideas, side)

        report.moves.append(AnnotatedMove(
            move_number=move_number,
            side=side,
            san=analysis.played_san,
            classification=analysis.classification,
            cp_loss=analysis.cp_loss,
            best_san=analysis.best_san,
            eval_str=_white_pov_eval(analysis, mover_is_white),
            explanation=explanation,
            line_ideas=line_ideas,
            opening=item["opening_lines"],
            critical=critical,
            think_s=item["think_s"],
            clock_s=item["clock_s"],
            analysis=analysis,
        ))
        if on_move:
            on_move(report.moves[-1])

    producer.join()

    if stop():
        return report   # partial results, no review — the user asked to stop

    # ---------------- whole-game review --------------------------------------
    if progress:
        progress("—", "review", "writing post-game review")
    records, swings = [], []
    for m in report.moves:
        prefix = f"{m.move_number}." if m.side == "White" else f"{m.move_number}..."
        rec = f"{prefix} {m.san} — {m.classification}, eval {m.eval_str}"
        if m.san != m.best_san and m.best_san:
            rec += f" (best was {m.best_san})"
        records.append(rec)
        if m.cp_loss >= cfg.analysis.mistake_cp:
            spent = f" (played in {m.think_s:.0f}s)" if m.think_s is not None else ""
            swings.append((m.cp_loss,
                           f"{prefix} {m.san} ({m.side}) gave up ~{m.cp_loss / 100:.1f} pawns "
                           f"of evaluation; {m.best_san} was better.{spent}"))
    swings = [text for _, text in sorted(swings, reverse=True)[:5]]
    _, _, review_time_class = _parse_time_control(report.headers.get("TimeControl", ""))
    review_prompt = build_game_review_prompt(
        report.headers, records, opening_name_holder[0], swings, side_filter,
        elo=cfg.user_elo, time_class=review_time_class)
    report.review = llm.generate(GAME_REVIEW_SYSTEM, review_prompt)

    return report
