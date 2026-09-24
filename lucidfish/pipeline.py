"""Pipeline: PGN in → per-move grounded explanations + whole-game review out.

Architecture (producer / narrators):
  producer   — Stockfish + features + tactics + opening lookup, running ahead
  narrators  — LLM calls: one worker for local models (a second request would
               only compete for the same GPU), several for cloud APIs
Engine (CPU) and LLM (GPU or network) are independent, so overlapping them
makes the total time ≈ max(engine, llm) instead of engine + llm. Results are
always delivered in move order.

The LLM is optional and failure-tolerant: without one (or if it stops
responding) the analysis completes with engine verdicts only, plus a warning.
"""

from __future__ import annotations

import io
import queue
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from dataclasses import dataclass, field

import chess
import chess.pgn

from . import features as feat
from . import tactics
from .config import Config
from .engine import EngineAnalyzer, Line, MoveAnalysis, build_move_analysis
from .llm import LLMError, LLMProvider, make_provider
from .opening import OpeningExplorer, local_opening_name
from .prompts import (
    GAME_REVIEW_SYSTEM,
    EvidenceIndex,
    build_game_review_prompt,
    build_ideas_prompt,
    build_move_prompt,
    build_system_prompt,
    correction_prompt,
    game_so_far_text,
    norm_san,
    split_sections,
    time_note,
)
from .scoring import eval_text, game_accuracy, win_percent

ERRORS = ("inaccuracy", "mistake", "blunder")
VERIFY_EXTRA_DEPTH = 4   # extra plies when double-checking a verdict before reporting it
DETAIL_LEVELS = ("key", "standard", "full")
# Output caps for local models (a safety net against rambling, not a target).
_MAX_TOKENS = {"full": 900, "brief": 300, "opponent": 250, "ideas": 300}

Progress = Callable[[str, int, int, str], None]   # (stage, done, total, label)


@dataclass
class AnnotatedMove:
    ply: int                        # 0-based index in the game
    move_number: int
    side: str                       # "White" / "Black"
    san: str
    uci: str
    classification: str
    cp_loss: int
    best_san: str
    eval_str: str                   # after the move, White's perspective ('+0.35', '#3', '1-0')
    win_white: float                # White's winning chances after the move (0-100)
    accuracy: float                 # this move's accuracy (0-100)
    fen_before: str
    fen_after: str
    explanation: str = ""
    line_ideas: dict = field(default_factory=dict)   # candidate SAN -> one-sentence idea
    opening: list[str] = field(default_factory=list)  # book stats shown without LLM cost
    critical: bool = False          # game-deciding moment (only-move found)
    tags: list[str] = field(default_factory=list)     # tactical motif labels
    mate_event: str = ""
    think_s: float | None = None    # seconds spent on this move (from [%clk] annotations)
    clock_s: float | None = None    # clock remaining after the move
    chess960: bool = False
    analysis: MoveAnalysis | None = None

    def to_dict(self) -> dict:
        """JSON-safe form used by the web UI, the database and exports."""
        a = self.analysis
        board = chess.Board(self.fen_before, chess960=self.chess960)
        mover_white = self.side == "White"
        best_sq = _squares(a.best_uci) if a else ("", "")
        after = chess.Board(self.fen_after, chess960=self.chess960)
        return {
            "ply": self.ply, "n": self.move_number, "side": self.side,
            "san": self.san, "uci": self.uci,
            "from": self.uci[:2], "to": self.uci[2:4],
            "cls": self.classification, "best": self.best_san,
            "best_from": best_sq[0], "best_to": best_sq[1],
            "eval": self.eval_str, "win": round(self.win_white, 1), "acc": self.accuracy,
            "cp_loss": self.cp_loss, "expl": self.explanation,
            "fen_before": self.fen_before, "fen_after": self.fen_after,
            "refutation": a.refutation_text if a else "",
            "refutation_steps": _line_steps(after, a.refutation_san) if a and a.refutation_san else [],
            "opening": self.opening, "critical": self.critical, "tags": self.tags,
            "mate_event": self.mate_event, "think_s": self.think_s, "clock_s": self.clock_s,
            "candidates": [
                {"san": ln.move_san, "score": white_pov_score(ln, mover_white), "line": ln.pv_text,
                 "idea": self.line_ideas.get(ln.move_san, ""),
                 "steps": _line_steps(board, ln.pv_san),
                 "cp": ln.value}      # mover's perspective, clamped (for ranking alternatives)
                for ln in (a.candidates if a else [])
            ],
        }


@dataclass
class GameReport:
    headers: dict
    moves: list[AnnotatedMove] = field(default_factory=list)
    review: str = ""                # whole-game narrative: opening, flow, lessons
    opening: str = ""               # most specific opening identified
    time_class: str = ""            # bullet/blitz/rapid/classical/daily
    accuracy: dict = field(default_factory=lambda: {"white": None, "black": None})
    warnings: list[str] = field(default_factory=list)
    coach: str = ""                 # "Ollama (local) · llama3.1:8b", or "" for engine-only
    engine: str = ""


# ------------------------------------------------------------------ helpers

def white_pov_score(line: Line, mover_is_white: bool) -> str:
    """Engine lines are scored for the side to move; the UI shows everything from
    White's perspective (+ = White better), like every chess site."""
    sign = 1 if mover_is_white else -1
    if line.mate_in is not None:
        return eval_text(None, sign * line.mate_in)
    return eval_text(sign * line.score_cp, None)


def _squares(uci: str) -> tuple[str, str]:
    return (uci[:2], uci[2:4]) if uci else ("", "")


def _line_steps(board: chess.Board, sans: list[str]) -> list[dict]:
    """A SAN line as playable steps [{san, from, to, fen}] so the frontend can walk
    a variation on the board without a chess library of its own."""
    b = board.copy(stack=False)
    steps = []
    for san in sans:
        try:
            mv = b.parse_san(san)
        except ValueError:
            break  # a malformed tail just shortens the preview
        b.push(mv)
        steps.append({"san": san, "from": chess.square_name(mv.from_square),
                      "to": chess.square_name(mv.to_square), "fen": b.board_fen()})
    return steps


def parse_time_control(tc: str) -> tuple[int | None, int, str]:
    """'600+5' -> (600, 5, 'rapid'). Returns (base_s, increment_s, class)."""
    if not tc or tc in ("-", "?"):
        return None, 0, ""
    if "/" in tc:                      # chess.com daily: "1/259200"
        return None, 0, "daily"
    try:
        base_str, _, inc_str = tc.partition("+")
        base, inc = int(base_str), int(inc_str or 0)
    except ValueError:
        return None, 0, ""
    # Estimated game duration, bucketed like chess.com (whose ratings profiles store).
    estimated = base + 40 * inc
    cls = ("bullet" if estimated < 180 else "blitz" if estimated < 600
           else "rapid" if estimated < 1800 else "classical")
    return base, inc, cls


def parse_game(pgn_text: str) -> tuple[chess.pgn.Game, list[str]]:
    """Read the first game from PGN text; returns (game, warnings). Raises ValueError."""
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        raise ValueError("Could not find a chess game in that text. Paste a PGN (the moves, "
                         "optionally with [Header \"...\"] lines).")
    variant = game.headers.get("Variant", "Standard").lower()
    if variant not in ("standard", "chess960", "from position", "fromposition", ""):
        raise ValueError(f"The '{game.headers['Variant']}' variant is not supported — only standard "
                         "chess and Chess960.")
    moves = list(game.mainline_moves())
    warnings = []
    if game.errors:
        if not moves:
            raise ValueError(f"The PGN could not be read: {game.errors[0]}")
        warnings.append(f"The PGN has an error after move {(len(moves) + 1) // 2} "
                        f"({game.errors[0]}); only the moves before it were analysed.")
    if not moves:
        raise ValueError("That game has no moves to analyse.")
    return game, warnings


def plan_note(detail: str, coached_side: str | None, side: str, classification: str,
              critical: bool, has_threat: bool) -> str | None:
    """Which kind of coach note (if any) a move gets at a given detail level.

    Mistakes and critical moments of the coached side always get a full note.
    """
    coached = coached_side is None or side.lower() == coached_side.lower()
    if coached:
        if classification in ERRORS or critical or detail == "full":
            return "full"
        return "brief" if detail == "standard" else None
    if detail == "full" or (detail == "standard" and (classification in ERRORS or has_threat)):
        return "opponent"
    return None


class _Coach:
    """Thread-safe wrapper that turns repeated or fatal LLM failures into a
    graceful switch to engine-only analysis."""

    def __init__(self, llm: LLMProvider | None, error: str = ""):
        self.llm = llm
        self.error = error
        self._fails = 0
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self.llm is not None and not self.error

    def ok(self) -> None:
        with self._lock:
            self._fails = 0

    def failed(self, e: Exception) -> None:
        with self._lock:
            self._fails += 1
            if getattr(e, "fatal", False) or self._fails >= 3:
                self.error = self.error or str(e)


def build_coach(cfg: Config) -> tuple[_Coach, list[str]]:
    if not cfg.llm.enabled:
        return _Coach(None), []
    try:
        return _Coach(make_provider(cfg.llm)), []
    except LLMError as e:
        return _Coach(None, str(e)), [f"The AI coach is unavailable ({e}). Showing engine analysis only."]


@dataclass
class _Evidence:
    ply: int
    board: chess.Board              # position before the move
    move: chess.Move
    analysis: MoveAnalysis
    f_before: feat.PositionFeatures
    f_after: feat.PositionFeatures
    motifs: list[str]
    critical: bool
    note: str | None
    want_ideas: bool
    motifs_best: list[str]
    motifs_reply: list[str]
    reply_line: Line | None
    opening_lines: list[str]
    opening_name: str
    game_so_far: str
    previous: dict | None
    think_s: float | None
    clock_s: float | None
    time_class: str


# ------------------------------------------------------------------ public

def analyze_game(
    pgn_text: str,
    cfg: Config,
    side_filter: str | None = None,   # "white", "black", or None for both
    progress: Progress | None = None,
    on_move: Callable[[AnnotatedMove], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    player_context: str = "",         # coach profile of this player from previous games
    level: str | None = None,         # beginner / casual / club / advanced
    engine_cache=None,                # engine.EvalCache (e.g. store.EngineCache())
    explorer: OpeningExplorer | None = None,
) -> GameReport:
    game, warnings = parse_game(pgn_text)
    report = GameReport(headers=dict(game.headers), warnings=warnings)
    detail = cfg.analysis.detail if cfg.analysis.detail in DETAIL_LEVELS else "standard"
    side_filter = side_filter.lower() if side_filter else None
    total = sum(1 for _ in game.mainline_moves())
    base_s, inc_s, time_class = parse_time_control(game.headers.get("TimeControl", ""))
    report.time_class = time_class

    coach, coach_warnings = build_coach(cfg)
    report.warnings += coach_warnings
    if coach.llm:
        report.coach = coach.llm.describe()
    system = build_system_prompt(side_filter, cfg.user_elo, level, player_context)

    stop_event = threading.Event()

    def stopped() -> bool:
        return stop_event.is_set() or bool(should_stop and should_stop())

    work: queue.Queue = queue.Queue(maxsize=8)
    opening_state = {"name": "", "from_book": False}
    engine_name: list[str] = []

    def put(item) -> bool:
        """Blocking put that gives up when a stop is requested (never deadlocks)."""
        while not stopped():
            try:
                work.put(item, timeout=0.3)
                return True
            except queue.Full:
                continue
        return False

    # ---------------- producer: engine + features + tactics + opening book ----------
    def produce() -> None:
        opener = explorer or OpeningExplorer()
        prev_clock = {chess.WHITE: float(base_s) if base_s else None,
                      chess.BLACK: float(base_s) if base_s else None}
        try:
            with EngineAnalyzer(cfg.engine, cache=engine_cache) as engine:
                engine_name.append(engine.name)
                board = game.board()
                root = board.copy(stack=False)
                sans_so_far: list[str] = []
                candidates = engine.top_lines(board)
                f_before = feat.extract(board)
                previous: dict | None = None
                for ply, node in enumerate(game.mainline()):
                    if stopped():
                        return
                    move, mover = node.move, board.turn
                    side = "White" if mover == chess.WHITE else "Black"
                    clock, think_s = node.clock(), None
                    if clock is not None and prev_clock[mover] is not None:
                        think_s = max(0.0, prev_clock[mover] - clock + inc_s)
                    if clock is not None:
                        prev_clock[mover] = clock

                    after = board.copy(stack=False)
                    after.push(move)
                    lines_after = [] if after.is_game_over() else engine.top_lines(after)
                    analysis, candidates, lines_after = _verified_analysis(
                        engine, board, move, after, candidates, lines_after, cfg)
                    f_after = feat.extract(after)
                    motifs = tactics.move_motifs(board, move)
                    threat = bool(tactics.tags_from_motifs(motifs)) or any(
                        m.startswith("attacks ") for m in motifs)

                    critical = (
                        len(candidates) >= 2 and analysis.classification in ("best", "good")
                        and win_percent(candidates[0].value) - win_percent(candidates[1].value)
                        >= cfg.analysis.critical_gap_win
                    )
                    note = plan_note(detail, side_filter, side, analysis.classification, critical, threat)
                    note = note if coach.available else None
                    want_ideas = note == "full" and (analysis.classification in ERRORS or analysis.cp_loss > 20)
                    motifs_best: list[str] = []
                    motifs_reply: list[str] = []
                    if note == "full":
                        if analysis.best_uci and analysis.best_uci != move.uci():
                            motifs_best = tactics.move_motifs(board, chess.Move.from_uci(analysis.best_uci))
                        if lines_after and analysis.refutation_san:
                            motifs_reply = tactics.move_motifs(after, chess.Move.from_uci(lines_after[0].move_uci))

                    opening_lines: list[str] = []
                    if ply < cfg.analysis.opening_book_plies:
                        info = opener.lookup(board.fen())
                        opening_lines = opener.summary_lines(info)
                        if info.name:
                            opening_state.update(name=f"{info.name} ({info.eco})", from_book=False)
                        elif not opening_state["name"] or opening_state["from_book"]:
                            local = local_opening_name(sans_so_far)  # offline fallback book
                            if local:
                                opening_state.update(name=local, from_book=True)

                    item = _Evidence(
                        ply=ply, board=board, move=move, analysis=analysis,
                        f_before=f_before, f_after=f_after, motifs=motifs, critical=critical,
                        note=note, want_ideas=want_ideas, motifs_best=motifs_best,
                        motifs_reply=motifs_reply, reply_line=lines_after[0] if lines_after else None,
                        opening_lines=opening_lines, opening_name=opening_state["name"],
                        game_so_far=game_so_far_text(root, sans_so_far) if note else "",
                        previous=previous, think_s=think_s, clock_s=clock, time_class=time_class,
                    )
                    if progress:
                        progress("engine", ply + 1, total, f"{side} {analysis.played_san}")
                    if not put(item):
                        return
                    previous = {"san": analysis.played_san, "cls": analysis.classification, "side": side}
                    sans_so_far.append(analysis.played_san)
                    board, candidates, f_before = after, lines_after, f_after
            put(None)
        except Exception as e:  # surface engine failures to the caller
            put(e)

    # ---------------- narrator: one LLM note per move ---------------------------
    def narrate(ev: _Evidence) -> AnnotatedMove:
        a = ev.analysis
        explanation, ideas = "", {}
        if ev.note and coach.available:
            try:
                explanation, ideas = _write_note(coach.llm, system, ev, side_filter, cfg)
                coach.ok()
            except LLMError as e:
                coach.failed(e)
        mover_white = ev.board.turn == chess.WHITE
        after_w = _white_after(a, mover_white)
        after_board = ev.board.copy(stack=False)
        after_board.push(ev.move)
        return AnnotatedMove(
            ply=ev.ply,
            move_number=ev.board.fullmove_number,
            side="White" if mover_white else "Black",
            san=a.played_san,
            uci=a.played_uci,
            classification=a.classification,
            cp_loss=a.cp_loss,
            best_san=a.best_san,
            eval_str=a.game_result or eval_text(*after_w),
            win_white=a.win_after if mover_white else 100 - a.win_after,
            accuracy=a.accuracy,
            fen_before=a.fen_before,
            fen_after=after_board.fen(),
            explanation=explanation,
            line_ideas=ideas,
            opening=ev.opening_lines,
            critical=ev.critical,
            tags=_tags(ev.motifs, a.classification),
            mate_event=a.mate_event,
            think_s=ev.think_s,
            clock_s=ev.clock_s,
            chess960=ev.board.chess960,
            analysis=a,
        )

    producer = threading.Thread(target=produce, name="lucidfish-engine", daemon=True)
    producer.start()
    workers = coach.llm.concurrency if coach.available else 1
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lucidfish-coach")
    pending: deque[Future] = deque()
    producer_done = False
    try:
        while not stopped():
            while pending and pending[0].done():
                move = pending.popleft().result()
                report.moves.append(move)
                if on_move:
                    on_move(move)
                if progress:
                    progress("coach", len(report.moves), total, f"{move.side} {move.san}")
            if producer_done:
                if not pending:
                    break
                wait_futures([pending[0]], timeout=0.2)
                continue
            try:
                item = work.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                producer_done = True
            elif isinstance(item, BaseException):
                raise item
            else:
                pending.append(executor.submit(narrate, item))
    finally:
        # Always release the engine, even if narration raised: no orphaned Stockfish.
        stop_event.set()
        executor.shutdown(wait=False, cancel_futures=True)
        producer.join(timeout=30)

    report.engine = engine_name[0] if engine_name else ""
    report.opening = opening_state["name"]
    if report.moves:
        first = report.moves[0].analysis
        start = first.win_before if report.moves[0].side == "White" else 100 - first.win_before
        report.accuracy = game_accuracy([start] + [m.win_white for m in report.moves],
                                        white_to_move_first=report.moves[0].side == "White")
    if coach.error and cfg.llm.enabled and not coach_warnings:
        report.warnings.append(f"The AI coach stopped responding ({coach.error}). Moves after that point "
                               "show engine analysis only.")
    if should_stop and should_stop():
        return report   # partial results, no review — the user asked to stop

    if coach.available and report.moves:
        if progress:
            progress("review", total, total, "writing the post-game review")
        try:
            report.review = coach.llm.generate(GAME_REVIEW_SYSTEM, _review_prompt(report, side_filter, cfg,
                                                                                  player_context))
        except LLMError as e:
            report.warnings.append(f"The post-game review could not be written: {e}")
    return report


# ------------------------------------------------------------------ verification

def _verified_analysis(engine: EngineAnalyzer, board: chess.Board, move: chess.Move, after: chess.Board,
                       candidates: list[Line], lines_after: list[Line], cfg: Config
                       ) -> tuple[MoveAnalysis, list[Line], list[Line]]:
    """Build the move's analysis, double-checking the verdicts that matter most.

    Fixed-depth search has a horizon: sacrifices can look like blunders, and
    the engine's own top choice can turn out to lose one ply later. Before
    calling a move a mistake/blunder, or "best" despite a collapse, search the
    relevant position a few plies deeper. Costs a few seconds per game and
    prevents the worst kind of feedback: a brilliant move labelled a blunder.
    """
    analysis = build_move_analysis(board, move, candidates, lines_after, cfg.analysis)
    if (analysis.classification in ("mistake", "blunder") and analysis.eval_after_mate is None
            and lines_after):
        deeper = engine.top_lines(after, extra_depth=VERIFY_EXTRA_DEPTH)
        if deeper:
            lines_after = deeper
            analysis = build_move_analysis(board, move, candidates, lines_after, cfg.analysis)
    if analysis.suspicious:
        deeper = engine.top_lines(board, extra_depth=VERIFY_EXTRA_DEPTH)
        if deeper:
            candidates = deeper
            analysis = build_move_analysis(board, move, candidates, lines_after, cfg.analysis)
    return analysis, candidates, lines_after


# ------------------------------------------------------------------ narration

def _tags(motifs: list[str], classification: str) -> list[str]:
    """UI labels; material deliberately given up by a good move is a sacrifice."""
    tags = tactics.tags_from_motifs(motifs)
    if classification in ("best", "good") and "hangs material" in tags:
        tags = ["sacrifice" if t == "hangs material" else t for t in tags]
    return tags


def _white_after(a: MoveAnalysis, mover_white: bool) -> tuple[int | None, int | None]:
    cp, mate = a.eval_after_cp, a.eval_after_mate
    if mover_white:
        return cp, mate
    return (-cp if cp is not None else None), (-mate if mate is not None else None)


def _write_note(llm: LLMProvider, system: str, ev: _Evidence, side_filter: str | None,
                cfg: Config) -> tuple[str, dict]:
    """One grounded note: prompt → parse → fact-check (one retry) → line ideas."""
    a = ev.analysis
    prompt = build_move_prompt(
        board=ev.board, move=ev.move, analysis=a, note_type=ev.note,
        f_before=ev.f_before, f_after=ev.f_after, coached_side=side_filter, critical=ev.critical,
        motifs_played=ev.motifs, motifs_best=ev.motifs_best, motifs_reply=ev.motifs_reply,
        reply_line=ev.reply_line, opening_name=ev.opening_name, opening_lines=ev.opening_lines,
        game_so_far=ev.game_so_far,
        time_note=time_note("White" if ev.board.turn == chess.WHITE else "Black",
                            ev.think_s, ev.clock_s, ev.time_class),
        previous=ev.previous, want_ideas=ev.want_ideas,
    )
    max_tokens = _MAX_TOKENS[ev.note]
    messages = [{"role": "user", "content": prompt}]
    text = llm.chat(system, messages, max_tokens=max_tokens)
    explanation, ideas = split_sections(text)

    if cfg.llm.factcheck and explanation:
        after = ev.board.copy(stack=False)
        after.push(ev.move)
        lines = [(ev.board, ln.pv_san) for ln in a.candidates]
        if ev.reply_line is not None:
            lines.append((after, ev.reply_line.pv_san))
        index = EvidenceIndex(ev.board, ev.move, lines)
        problems = index.problems(explanation + "\n" + "\n".join(ideas.values()))
        if problems:
            retry = llm.chat(system, messages + [{"role": "assistant", "content": text},
                                                 {"role": "user", "content": correction_prompt(problems)}],
                             max_tokens=max_tokens)
            e2, i2 = split_sections(retry)
            if e2 and len(index.problems(e2 + "\n" + "\n".join(i2.values()))) < len(problems):
                explanation, ideas = e2, i2 or ideas

    if ev.want_ideas:
        ideas = _ensure_line_ideas(llm, system, ev, ideas)
    return explanation, ideas


def _ensure_line_ideas(llm: LLMProvider, system: str, ev: _Evidence, ideas: dict) -> dict:
    """Guarantee an idea sentence for each top-3 alternative candidate.

    First fuzzy-match keys the model did produce (Bb5 vs Bb5+ etc.); if any
    are still missing, make one minimal follow-up call asking only for those.
    """
    a = ev.analysis
    wanted = [ln for ln in a.candidates[:3] if ln.move_uci != a.played_uci]
    by_norm = {norm_san(k): v for k, v in ideas.items()}
    out = {}
    for ln in wanted:
        idea = ideas.get(ln.move_san) or by_norm.get(norm_san(ln.move_san))
        if idea:
            out[ln.move_san] = idea
    missing = [ln for ln in wanted if ln.move_san not in out]
    if missing:
        try:
            resp = llm.generate(system, build_ideas_prompt(missing, ev.board, ev.game_so_far),
                                max_tokens=_MAX_TOKENS["ideas"])
            _, extra = split_sections("LINE IDEAS:\n" + resp)
            extra_norm = {norm_san(k): v for k, v in extra.items()}
            for ln in missing:
                idea = extra.get(ln.move_san) or extra_norm.get(norm_san(ln.move_san))
                if idea:
                    out[ln.move_san] = idea
        except LLMError:
            pass  # ideas are decoration — never fail the move over them
    return out


def _review_prompt(report: GameReport, side_filter: str | None, cfg: Config, player_context: str) -> str:
    from .scoring import describe_eval
    records, swings = [], []
    for m in report.moves:
        a = m.analysis
        prefix = f"{m.move_number}." if m.side == "White" else f"{m.move_number}..."
        cp, mate = _white_after(a, m.side == "White")
        state = "game over" if a.game_result else describe_eval(cp, mate)
        rec = f"{prefix} {m.san} — {m.classification}"
        if m.san != m.best_san and m.best_san:
            rec += f" (best was {m.best_san})"
        records.append(f"{rec}; after it {state}")
        if m.classification in ("mistake", "blunder"):
            spent = f" Played in {m.think_s:.0f}s." if m.think_s is not None else ""
            event = {"missed_mate": " It missed a forced mate.",
                     "allowed_mate": " It allowed a forced mate."}.get(m.mate_event, "")
            swings.append((a.win_loss, f"{prefix} {m.san} ({m.side}) dropped {m.side}'s winning chances "
                                       f"from {a.win_before:.0f}% to {a.win_after:.0f}%; {m.best_san} was "
                                       f"better.{event}{spent}"))
    swings.sort(key=lambda s: -s[0])
    return build_game_review_prompt(
        report.headers, records, report.opening, [t for _, t in swings[:5]], side_filter,
        elo=cfg.user_elo, time_class=report.time_class, player_context=player_context,
        accuracy=report.accuracy)


# ------------------------------------------------------------------ positions

def analyze_position(fen: str, cfg: Config, perspective: str | None = None, level: str | None = None,
                     engine_cache=None) -> dict:
    """One-shot analysis of a set-up position (board editor mode)."""
    from .prompts import build_position_prompt
    board = chess.Board(fen)
    with EngineAnalyzer(cfg.engine, cache=engine_cache) as engine:
        lines = engine.top_lines(board)
    f = feat.extract(board)
    facts = f.summary_lines()
    side = "White" if board.turn == chess.WHITE else "Black"
    persp = (perspective or side).capitalize()
    result = {
        "turn": side, "perspective": persp, "features": facts, "commentary": "", "warnings": [],
        "lines": [{"san": ln.move_san, "score": white_pov_score(ln, board.turn == chess.WHITE),
                   "line": ln.pv_text, "idea": "", "steps": _line_steps(board, ln.pv_san),
                   "win": round(win_percent(ln.value) if board.turn == chess.WHITE
                                else 100 - win_percent(ln.value), 1)}
                  for ln in lines],
    }
    coach, warnings = build_coach(cfg)
    result["warnings"] += warnings
    if coach.available and lines:
        system = build_system_prompt(persp.lower(), cfg.user_elo, level)
        try:
            text = coach.llm.generate(system, build_position_prompt(board, lines, facts, persp),
                                      max_tokens=_MAX_TOKENS["full"])
            commentary, ideas = split_sections(text)
            by_norm = {norm_san(k): v for k, v in ideas.items()}
            result["commentary"] = commentary
            for entry in result["lines"]:
                entry["idea"] = ideas.get(entry["san"]) or by_norm.get(norm_san(entry["san"]), "")
        except LLMError as e:
            result["warnings"].append(f"The AI coach is unavailable ({e}).")
    return result
