"""Pipeline: PGN in → commentary, grounded explanations and a game review out.

Architecture (producer / narrators):
  producer   — Stockfish + features + tactics + opening lookup, running ahead
  narrators  — LLM calls: one worker for local models (a second request would
               only compete for the same GPU), several for cloud APIs
Engine (CPU) and LLM (GPU or network) are independent, so overlapping them
makes the total time ≈ max(engine, llm) instead of engine + llm. Results are
always delivered in move order.

What the coach writes depends on the detail level:
  key       — detailed notes for mistakes and critical moments only (fastest)
  standard  — commentary on every move of both players, written in windows of
              consecutive moves so the flow of the game comes through, plus a
              detailed note for each mistake and critical moment (default)
  full      — a detailed note for every move (slowest)

Two models can share the work. The main model (often local) writes the running
commentary; an optional second, stronger model (often a cloud model) writes the
detailed notes on mistakes and critical moments, the review, and rewrites any
commentary that failed the fact-check. It only sees a handful of requests per
game, so a cloud model here costs very little.

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
from . import plans, ratings, review_check, tactics
from .config import Config
from .engine import EngineAnalyzer, Line, MoveAnalysis, build_move_analysis, describe_move
from .llm import LLMError, LLMProvider, make_provider
from .opening import OpeningExplorer, local_book_depth, local_opening_name
from .prompts import (
    GAME_REVIEW_SYSTEM,
    ChapterContext,
    CommentaryContext,
    CommentaryMove,
    EvidenceIndex,
    build_chapter_prompt,
    build_commentary_prompt,
    build_game_review_prompt,
    build_ideas_prompt,
    build_move_prompt,
    build_system_prompt,
    correction_prompt,
    eval_after_words,
    move_label,
    norm_san,
    parse_chapter,
    parse_commentary,
    side_line,
    split_sections,
    time_note,
)
from .scoring import describe_eval, eval_text, game_accuracy, win_percent

ERRORS = ("inaccuracy", "mistake", "blunder")
VERIFY_EXTRA_DEPTH = 4   # extra plies when double-checking a verdict before reporting it
DETAIL_LEVELS = ("key", "standard", "full")
WINDOW = 8               # plies per commentary window (4 moves by each side)
RECENT = 6               # plies of history shown with each note
HINDSIGHT = 6            # plies of "what actually happened next"
# Output caps for local models (a safety net against rambling, not a target).
_MAX_TOKENS = {"full": 900, "brief": 300, "opponent": 250, "ideas": 300, "window": 1000, "chapter": 300}

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
    explanation: str = ""           # detailed coach note (mistakes, critical moments, "every move")
    commentary: str = ""            # commentator's line about the flow of the game
    line_ideas: dict = field(default_factory=dict)   # candidate SAN -> one-sentence idea
    opening: list[str] = field(default_factory=list)  # book stats shown without LLM cost
    critical: bool = False          # game-deciding moment (only-move found)
    book: bool = False              # known opening theory
    phase: str = ""                 # opening / middlegame / endgame
    tags: list[str] = field(default_factory=list)     # tactical motif labels
    threat: str = ""                # error that ignored a threat already on the board, e.g. "Nxe4 (wins ...)"
    left_book: str = ""             # this move left known opening theory (+ what masters play)
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
            "best_uci": a.best_uci if a else "",
            "best_from": best_sq[0], "best_to": best_sq[1],
            "eval": self.eval_str, "win": round(self.win_white, 1), "acc": self.accuracy,
            "cp_loss": self.cp_loss, "expl": self.explanation, "flow": self.commentary,
            "fen_before": self.fen_before, "fen_after": self.fen_after,
            "refutation": a.refutation_text if a else "",
            "refutation_steps": _line_steps(after, a.refutation_san) if a and a.refutation_san else [],
            "opening": self.opening, "critical": self.critical, "book": self.book, "phase": self.phase,
            "tags": self.tags, "threat": self.threat, "left_book": self.left_book,
            "mate_event": self.mate_event, "think_s": self.think_s, "clock_s": self.clock_s,
            "candidates": [
                {"san": ln.move_san, "uci": ln.move_uci, "score": white_pov_score(ln, mover_white),
                 "line": ln.pv_text, "idea": self.line_ideas.get(ln.move_san, ""),
                 "steps": _line_steps(board, ln.pv_san),
                 "cp": ln.value}      # mover's perspective, clamped (for ranking alternatives)
                for ln in (a.candidates if a else [])
            ],
        }


@dataclass
class GameReport:
    headers: dict
    moves: list[AnnotatedMove] = field(default_factory=list)
    review: str = ""                # whole-game narrative: summary, story, lessons
    opening: str = ""               # most specific opening identified
    time_class: str = ""            # bullet/blitz/rapid/classical/daily
    accuracy: dict = field(default_factory=lambda: {"white": None, "black": None})
    warnings: list[str] = field(default_factory=list)
    coach: str = ""                 # "Ollama (local) · llama3.1:8b", or "" for engine-only
    engine: str = ""
    chapters: list[dict] = field(default_factory=list)   # {start, end, range, title, summary}


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


def plan_note(detail: str, coached_side: str | None, side: str, classification: str, critical: bool) -> str | None:
    """Which kind of detailed note (if any) a move gets at a given detail level.

    Mistakes and critical moments of the coached side always get a full note;
    at the "standard" level every other move is covered by the commentary windows.
    """
    coached = coached_side is None or side.lower() == coached_side.lower()
    if coached and (classification in ERRORS or critical):
        return "full"
    if detail == "full":
        return "full" if coached else "opponent"
    return None


class _Coach:
    """The AI models for one analysis, with graceful failure handling.

    - ``llm`` (main model) writes the running commentary and ordinary notes.
    - ``expert`` (optional, stronger) writes the hard parts: notes on mistakes and
      critical moments, the review, and rewrites of text that failed the fact-check.
      If it stops working, the main model takes over its jobs.
    Repeated or fatal failures of the main model switch the analysis to engine-only.
    """

    def __init__(self, llm: LLMProvider | None, error: str = "", expert: LLMProvider | None = None):
        self.llm = llm
        self.error = error
        self.expert = expert
        self.expert_error = ""
        self.escalations = 0
        self._fails = 0
        self._expert_fails = 0
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

    def smart(self) -> LLMProvider | None:
        """The best model currently working: the expert if it is healthy, else the main one."""
        with self._lock:
            return self.expert if self.expert is not None and not self.expert_error else self.llm

    def hard(self, job: Callable[[LLMProvider], object]):
        """Run a job on the expert model, falling back to the main model if it fails."""
        expert = self.smart()
        if expert is not None and expert is not self.llm:
            try:
                result = job(expert)
                with self._lock:
                    self._expert_fails = 0
                return result
            except LLMError as e:
                with self._lock:
                    self._expert_fails += 1
                    if e.fatal or self._expert_fails >= 2:
                        self.expert_error = self.expert_error or str(e)
        return job(self.llm)

    def describe(self) -> str:
        if self.llm is None:
            return ""
        if self.expert is None:
            return self.llm.describe()
        return f"{self.llm.describe()} + {self.expert.describe()} for key moments"


def build_coach(cfg: Config) -> tuple[_Coach, list[str]]:
    if not cfg.llm.enabled:
        return _Coach(None), []
    try:
        main = make_provider(cfg.llm)
    except LLMError as e:
        return _Coach(None, str(e)), [f"The AI coach is unavailable ({e}). Showing engine analysis only."]
    expert, warnings = None, []
    if cfg.expert is not None:
        try:
            expert = make_provider(cfg.expert)
        except LLMError as e:
            warnings.append(f"The second model is unavailable ({e}), so {main.describe()} wrote everything.")
    return _Coach(main, expert=expert), warnings


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
    book: bool
    note: str | None
    want_ideas: bool
    motifs_best: list[str]
    motifs_reply: list[str]
    reply_line: Line | None
    opening_lines: list[str]
    opening_name: str
    previous: dict | None
    think_s: float | None
    clock_s: float | None
    time_class: str
    recent: str = ""
    hindsight: str = ""
    plan_facts: list[str] = field(default_factory=list)
    threat: str = ""
    left_book: str = ""


@dataclass
class _Unit:
    """Moves emitted together: one move, or a commentary window of several."""
    evs: list[_Evidence]
    notes: dict[int, Future | None]
    window: Future | None = None

    def futures(self) -> list[Future]:
        return [f for f in [self.window, *self.notes.values()] if f is not None]

    def done(self) -> bool:
        return all(f.done() for f in self.futures())


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
    coached_color = None if side_filter is None else side_filter == "white"
    base_s, inc_s, _ = parse_time_control(game.headers.get("TimeControl", ""))
    report.time_class = time_class = ratings.time_class(dict(game.headers))   # the site's own classes

    # The whole game is known up front: history and "what happened next" for any move.
    played: list[plans.PlayedMove] = []
    b = game.board()
    for ply, move in enumerate(game.mainline_moves()):
        played.append(plans.PlayedMove(ply, b.turn, b.san(move), move, b.copy(stack=False)))
        b.push(move)
    total = len(played)
    book_depth = local_book_depth([m.san for m in played])

    coach, coach_warnings = build_coach(cfg)
    report.warnings += coach_warnings
    report.coach = coach.describe()
    system = build_system_prompt(side_filter, cfg.user_elo, level, player_context,
                                 players={"White": game.headers.get("White", "White"),
                                          "Black": game.headers.get("Black", "Black")},
                                 rating_label=cfg.user_elo_label)
    commentary_mode = detail == "standard" and coach.available

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
                candidates = engine.top_lines(board)
                f_before = feat.extract(board)
                previous: dict | None = None
                in_book = True
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

                    critical = (
                        len(candidates) >= 2 and analysis.classification in ("best", "good")
                        and win_percent(candidates[0].value) - win_percent(candidates[1].value)
                        >= cfg.analysis.critical_gap_win
                    )
                    note = plan_note(detail, side_filter, side, analysis.classification, critical)
                    note = note if coach.available else None
                    want_ideas = note == "full" and (analysis.classification in ERRORS or analysis.cp_loss > 20)
                    motifs_best: list[str] = []
                    motifs_reply: list[str] = []
                    if note == "full":
                        if analysis.best_uci and analysis.best_uci != move.uci():
                            motifs_best = tactics.move_motifs(board, chess.Move.from_uci(analysis.best_uci))
                        if lines_after and analysis.refutation_san:
                            motifs_reply = tactics.move_motifs(after, chess.Move.from_uci(lines_after[0].move_uci))

                    # Specialist: did this error ignore a threat that was already on the board?
                    threat = ""
                    if analysis.classification in ERRORS and analysis.refutation_san and lines_after:
                        threat = tactics.existing_threat(board, chess.Move.from_uci(lines_after[0].move_uci))

                    opening_lines: list[str] = []
                    in_theory = ply < book_depth
                    masters: list[dict] = []
                    if ply < cfg.analysis.opening_book_plies:
                        info = opener.lookup(board.fen())
                        masters = info.top_moves
                        opening_lines = opener.summary_lines(info)
                        if any(m["san"] == analysis.played_san and m["games"] >= 20 for m in info.top_moves):
                            in_theory = True
                        if info.name:
                            opening_state.update(name=f"{info.name} ({info.eco})", from_book=False)
                        elif not opening_state["name"] or opening_state["from_book"]:
                            local = local_opening_name([m.san for m in played[:ply]])  # offline fallback
                            if local:
                                opening_state.update(name=local, from_book=True)

                    # Specialist: the move that leaves known theory, and what masters play instead
                    # (only claimed when the masters database was reachable for this position).
                    left_book = ""
                    if in_book and not in_theory:
                        in_book = False
                        others = [m for m in masters if m["san"] != analysis.played_san][:3]
                        if others:
                            left_book = ("left known opening theory; in this position masters usually play "
                                         + ", ".join(f"{m['san']} ({m['games']} games)" for m in others))

                    item = _Evidence(
                        ply=ply, board=board, move=move, analysis=analysis,
                        f_before=f_before, f_after=f_after, motifs=motifs, critical=critical, book=in_theory,
                        note=note, want_ideas=want_ideas, motifs_best=motifs_best,
                        motifs_reply=motifs_reply, reply_line=lines_after[0] if lines_after else None,
                        opening_lines=opening_lines, opening_name=opening_state["name"],
                        previous=previous, think_s=think_s, clock_s=clock, time_class=time_class,
                        threat=threat, left_book=left_book,
                    )
                    if note:
                        item.recent = plans.with_gists(played[max(0, ply - RECENT):ply])
                        item.hindsight = plans.with_gists(played[ply + 1:ply + 1 + HINDSIGHT])
                        item.plan_facts = plans.plan_lines(played[:ply], board)
                    if progress:
                        progress("engine", ply + 1, total, f"{side} {analysis.played_san}")
                    if not put(item):
                        return
                    previous = {"san": analysis.played_san, "cls": analysis.classification, "side": side}
                    board, candidates, f_before = after, lines_after, f_after
            put(None)
        except Exception as e:  # surface engine failures to the caller
            put(e)

    # ---------------- narrators ---------------------------------------------------
    def note_task(ev: _Evidence) -> tuple[str, dict]:
        if not coach.available:
            return "", {}
        try:
            if _is_hard(ev):
                result = coach.hard(lambda llm: _write_note(llm, system, ev, side_filter, coached_color, played,
                                                            cfg, coach))
            else:
                result = _write_note(coach.llm, system, ev, side_filter, coached_color, played, cfg, coach)
            coach.ok()
            return result
        except LLMError as e:
            coach.failed(e)
        except Exception as e:  # never let one note sink the whole analysis
            coach.failed(LLMError(str(e)))
        return "", {}

    def window_task(evs: list[_Evidence]) -> dict[int, str]:
        if not coach.available:
            return {}
        try:
            result = _write_window(coach.llm, system, evs, played, coached_color, opening_state["name"], cfg, coach)
            coach.ok()
            return result
        except LLMError as e:
            coach.failed(e)
        except Exception as e:
            coach.failed(LLMError(str(e)))
        return {}

    def annotate(ev: _Evidence, note: tuple[str, dict], flow: str) -> AnnotatedMove:
        a = ev.analysis
        mover_white = ev.board.turn == chess.WHITE
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
            eval_str=a.game_result or eval_text(*_white_after(a, mover_white)),
            win_white=a.win_after if mover_white else 100 - a.win_after,
            accuracy=a.accuracy,
            fen_before=a.fen_before,
            fen_after=after_board.fen(),
            explanation=note[0],
            commentary=flow,
            line_ideas=note[1],
            opening=ev.opening_lines,
            critical=ev.critical,
            book=ev.book,
            phase=ev.f_before.phase,
            tags=_tags(ev.motifs, a.classification) + (["missed threat"] if ev.threat else [])
                 + (["left book"] if ev.left_book else []),
            threat=ev.threat,
            left_book=ev.left_book,
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
    # A cloud expert works in parallel with a local main model (different hardware); two
    # local models would only compete for the same GPU and memory, so they share one queue.
    expert_executor = executor
    if coach.available and coach.expert is not None and not (coach.expert.spec.local and coach.llm.spec.local):
        expert_executor = ThreadPoolExecutor(max_workers=coach.expert.concurrency,
                                             thread_name_prefix="lucidfish-expert")
    pending: deque[_Unit] = deque()
    window: list[_Evidence] = []
    window_notes: dict[int, Future | None] = {}
    producer_done = False

    def flush_window() -> None:
        if window:
            fut = executor.submit(window_task, list(window)) if coach.available else None
            pending.append(_Unit(list(window), dict(window_notes), fut))
            window.clear()
            window_notes.clear()

    try:
        while not stopped():
            while pending and pending[0].done():
                unit = pending.popleft()
                flows = unit.window.result() if unit.window else {}
                for ev in unit.evs:
                    fut = unit.notes.get(ev.ply)
                    move = annotate(ev, fut.result() if fut else ("", {}), flows.get(ev.ply, ""))
                    report.moves.append(move)
                    if on_move:
                        on_move(move)
                    if progress:
                        progress("coach", len(report.moves), total, f"{move.side} {move.san}")
            if producer_done:
                if not pending:
                    break
                wait_futures(pending[0].futures(), timeout=0.2)
                continue
            try:
                item = work.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                producer_done = True
                flush_window()
            elif isinstance(item, BaseException):
                raise item
            else:
                pool = expert_executor if _is_hard(item) else executor
                fut = pool.submit(note_task, item) if item.note and coach.available else None
                if commentary_mode:
                    window.append(item)
                    window_notes[item.ply] = fut
                    if len(window) >= WINDOW:
                        flush_window()
                else:
                    pending.append(_Unit([item], {item.ply: fut}))
    finally:
        # Always release the engine, even if narration raised: no orphaned Stockfish.
        stop_event.set()
        executor.shutdown(wait=False, cancel_futures=True)
        expert_executor.shutdown(wait=False, cancel_futures=True)
        producer.join(timeout=30)

    report.engine = engine_name[0] if engine_name else ""
    report.opening = opening_state["name"]
    if report.moves:
        first = report.moves[0].analysis
        start = first.win_before if report.moves[0].side == "White" else 100 - first.win_before
        report.accuracy = game_accuracy([start] + [m.win_white for m in report.moves],
                                        white_to_move_first=report.moves[0].side == "White")
    if coach.error and cfg.llm.enabled and coach.llm is not None:
        report.warnings.append(f"The AI coach stopped responding ({coach.error}). Moves after that point "
                               "show engine analysis only.")
    if coach.expert_error:
        report.warnings.append(f"The second model stopped working ({coach.expert_error}), so "
                               f"{coach.llm.describe()} took over its notes.")
    if should_stop and should_stop():
        return report   # partial results, no review — the user asked to stop

    user_stopped = lambda: bool(should_stop and should_stop())  # noqa: E731  (stop_event is set by now)
    if coach.available and report.moves and detail != "key":
        spans = split_chapters(report.moves)
        if len(spans) >= 2:
            if progress:
                progress("review", total, total, "Summarising the game in chapters…")
            report.chapters = _write_chapters(coach, system, spans, report, played, coached_color, user_stopped)
    if coach.available and report.moves and not user_stopped():
        if progress:
            progress("review", total, total, "Writing the post-game review…")
        facts = review_check.game_facts(report.moves, report.headers,
                                        side_filter.capitalize() if side_filter else None, report.opening, base_s,
                                        report.accuracy)
        prompt = _review_prompt(report, side_filter, cfg, player_context, facts.lines)

        def write_review(llm: LLMProvider) -> str:
            # One rewrite if the review gets facts wrong (the result, whose move, a verdict), then clean up.
            messages = [{"role": "user", "content": prompt}]
            text = llm.chat(GAME_REVIEW_SYSTEM, messages)
            return _verified_text(llm, GAME_REVIEW_SYSTEM, messages, text,
                                  lambda t: review_check.problems(t, facts), max_tokens=None)
        try:
            text = coach.hard(write_review)
            report.review, _removed = review_check.check_review(text, facts)
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


def _after(ev: _Evidence) -> chess.Board:
    b = ev.board.copy(stack=False)
    b.push(ev.move)
    return b


def _is_hard(ev: _Evidence) -> bool:
    """Jobs worth the stronger model: detailed notes on mistakes and critical moments."""
    return ev.note == "full" and (ev.analysis.classification in ERRORS or ev.critical)


def _verified_text(llm: LLMProvider, system: str, messages: list[dict], text: str, check,
                   max_tokens: int, coach: _Coach | None = None, escalate: bool = False) -> str:
    """Fact-check `text` with `check(text) -> problems`; ask once for a correction,
    keep whichever version has fewer problems. The caller cleans what remains.

    With `escalate`, the correction goes to the expert model (when there is one):
    it sees the original request, the weaker model's answer and what was wrong
    with it, so one request from a stronger model repairs it.
    """
    problems = check(text)
    if not problems:
        return text
    convo = messages + [{"role": "assistant", "content": text},
                        {"role": "user", "content": correction_prompt(problems)}]
    fixer = coach.smart() if coach is not None and escalate else llm
    if coach is not None and fixer is not llm:
        with coach._lock:
            coach.escalations += 1
        retry = coach.hard(lambda m: m.chat(system, convo, max_tokens=max_tokens))
    else:
        retry = llm.chat(system, convo, max_tokens=max_tokens)
    return retry if retry.strip() and len(check(retry)) < len(problems) else text


def _write_note(llm: LLMProvider, system: str, ev: _Evidence, side_filter: str | None,
                coached: chess.Color | None, played: list[plans.PlayedMove], cfg: Config,
                coach: _Coach | None = None) -> tuple[str, dict]:
    """One grounded note: prompt → parse → fact-check (one retry) → clean → line ideas."""
    a = ev.analysis
    prompt = build_move_prompt(
        board=ev.board, move=ev.move, analysis=a, note_type=ev.note,
        f_before=ev.f_before, f_after=ev.f_after, coached_side=side_filter, critical=ev.critical,
        motifs_played=ev.motifs, motifs_best=ev.motifs_best, motifs_reply=ev.motifs_reply,
        reply_line=ev.reply_line, opening_name=ev.opening_name, opening_lines=ev.opening_lines,
        recent=ev.recent, hindsight=ev.hindsight, plans=ev.plan_facts, threat=ev.threat,
        left_book=ev.left_book,
        time_note=time_note("White" if ev.board.turn == chess.WHITE else "Black",
                            ev.think_s, ev.clock_s, ev.time_class),
        previous=ev.previous, want_ideas=ev.want_ideas,
    )
    max_tokens = _MAX_TOKENS[ev.note]
    messages = [{"role": "user", "content": prompt}]
    text = llm.chat(system, messages, max_tokens=max_tokens)

    if cfg.llm.factcheck:
        after = _after(ev)
        lines = [(ev.board, ln.pv_san) for ln in a.candidates]
        if ev.reply_line is not None:
            lines.append((after, ev.reply_line.pv_san))
        lines.append((after, [m.san for m in played[ev.ply + 1:ev.ply + 1 + HINDSIGHT]]))
        index = EvidenceIndex.for_move(ev.board, ev.move, lines, coached)

        def check(t: str) -> list[str]:
            e, i = split_sections(t)
            missing = [] if e.strip() else ["the EXPLANATION section is missing"]
            return missing + index.problems(e + "\n" + "\n".join(i.values()))

        text = _verified_text(llm, system, messages, text, check, max_tokens, coach, cfg.llm.escalate)
        explanation, ideas = split_sections(text)
        explanation = index.clean(explanation)[0]
        ideas = {k: v for k, v in ideas.items() if not index.problems(v)}
    else:
        explanation, ideas = split_sections(text)

    if ev.want_ideas:
        ideas = _ensure_line_ideas(llm, system, ev, ideas)
    return explanation, ideas


def _write_window(llm: LLMProvider, system: str, evs: list[_Evidence], played: list[plans.PlayedMove],
                  coached: chess.Color | None, opening: str, cfg: Config | None = None,
                  coach: _Coach | None = None) -> dict[int, str]:
    """Commentator lines for a window of consecutive moves (one request)."""
    first, last = evs[0], evs[-1]
    moves = []
    for ev in evs:
        a = ev.analysis
        mover_white = ev.board.turn == chess.WHITE
        after = _after(ev)
        moves.append(CommentaryMove(
            label=move_label(ev.board, a.played_san),
            side="White" if mover_white else "Black",
            san=a.played_san,
            what=describe_move(ev.board, ev.move),
            verdict=a.classification + (" — a critical moment" if ev.critical else ""),
            eval_after=eval_after_words(a, mover_white),
            motifs=ev.motifs,
            changes=feat.diff_lines(ev.f_before, ev.f_after),
            engine_next=side_line(after, ev.reply_line.pv_san, 4) if ev.reply_line else "",
            best=a.best_san if a.classification in ERRORS else "",
            book=ev.book,
            threat=ev.threat,
            left_book=ev.left_book,
        ))
    fa = first.analysis
    before_white = (fa.eval_before_cp, fa.eval_before_mate) if first.board.turn == chess.WHITE else (
        -fa.eval_before_cp if fa.eval_before_cp is not None else None,
        -fa.eval_before_mate if fa.eval_before_mate is not None else None)
    ctx = CommentaryContext(
        start_label=moves[0].label, end_label=moves[-1].label,
        eval_before=describe_eval(*before_white),
        pieces=feat.piece_placement(first.board),
        opening=opening,
        recent=plans.with_gists(played[max(0, first.ply - RECENT):first.ply]),
        hindsight=plans.with_gists(played[last.ply + 1:last.ply + 1 + HINDSIGHT]),
        plans=plans.plan_lines(played[:first.ply], first.board),
    )
    messages = [{"role": "user", "content": build_commentary_prompt(moves, ctx)}]
    text = llm.chat(system, messages, max_tokens=_MAX_TOKENS["window"])

    lines: list[tuple[chess.Board, list[str]]] = []
    for ev in evs:
        lines += [(ev.board, ln.pv_san[:4]) for ln in ev.analysis.candidates]
        if ev.reply_line is not None:
            lines.append((_after(ev), ev.reply_line.pv_san[:4]))
    lines.append((_after(last), [m.san for m in played[last.ply + 1:last.ply + 1 + HINDSIGHT]]))
    start = max(0, first.ply - RECENT)
    if start < first.ply:
        lines.append((played[start].board, [m.san for m in played[start:first.ply]]))
    index = EvidenceIndex([(ev.board, ev.move) for ev in evs], lines, coached)

    def check(t: str) -> list[str]:
        parsed = parse_commentary(t, moves)
        # A missing line counts as a problem too, so an answer that lost lines never "wins".
        missing = [f"there is no line for {m.label}; write one line for every move listed"
                   for m in moves if m.label not in parsed]
        return missing + [f"{label}: {p}" for label, comment in parsed.items() for p in index.problems(comment)]

    escalate = cfg.llm.escalate if cfg is not None else False
    text = _verified_text(llm, system, messages, text, check, _MAX_TOKENS["window"], coach, escalate)
    parsed = parse_commentary(text, moves)
    return {ev.ply: index.clean(parsed.get(m.label, ""))[0] for ev, m in zip(evs, moves, strict=True)}


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
            resp = llm.generate(system, build_ideas_prompt(missing, ev.board, ev.recent),
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


def _review_prompt(report: GameReport, side_filter: str | None, cfg: Config, player_context: str,
                   game_facts: list[str] | None = None) -> str:
    records, swings, flow = [], [], []
    for m in report.moves:
        a = m.analysis
        prefix = f"{m.move_number}. White" if m.side == "White" else f"{m.move_number}... Black"
        cp, mate = _white_after(a, m.side == "White")
        state = "game over" if a.game_result else describe_eval(cp, mate)
        rec = f"{prefix} {m.san} — {m.classification}"
        if m.classification not in ("best", "good") and m.best_san:
            rec += f" (best was {m.best_san})"
        records.append(f"{rec}; after it {state}")
        if m.commentary:
            flow.append(f"{prefix} {m.san}: {m.commentary}")
        if m.classification in ("mistake", "blunder"):
            spent = f" Played in {m.think_s:.0f}s." if m.think_s is not None else ""
            event = {"missed_mate": " It missed a forced mate.",
                     "allowed_mate": " It allowed a forced mate."}.get(m.mate_event, "")
            swings.append((a.win_loss, f"{prefix} {m.san} dropped {m.side}'s winning chances "
                                       f"from {a.win_before:.0f}% to {a.win_after:.0f}%; {m.best_san} was "
                                       f"better.{event}{spent}"))
    swings.sort(key=lambda s: -s[0])
    facts = list(game_facts or [])
    coached = side_filter.capitalize() if side_filter else None
    for i, m in enumerate(report.moves):
        prefix = f"{m.move_number}. White" if m.side == "White" else f"{m.move_number}... Black"
        if m.left_book:
            later = report.moves[min(len(report.moves) - 1, i + 10)]
            before = report.moves[i - 1].win_white if i else 50.0
            facts.append(f"{prefix} {m.san} {m.left_book}. White's winning chances were {before:.0f}% before "
                         f"it and {later.win_white:.0f}% five moves later.")
        if m.threat and (coached is None or m.side == coached):
            facts.append(f"{prefix} {m.san} ignored a threat that was already on the board: {m.threat}.")
    return build_game_review_prompt(
        report.headers, records, report.opening, [t for _, t in swings[:5]], side_filter,
        elo=cfg.user_elo, rating_label=cfg.user_elo_label, time_class=report.time_class,
        player_context=player_context,
        accuracy=report.accuracy, commentary=flow, chapters=report.chapters, facts=facts)


# ------------------------------------------------------------------ chapters

MIN_CHAPTER = 6       # plies
MAX_CHAPTERS = 6
SWING = 15.0          # percentage points of winning chances that make a turning point


def split_chapters(moves: list[AnnotatedMove]) -> list[tuple[int, int]]:
    """Split a game into chapters: (first index, last index) pairs.

    Chapters start where the phase changes (opening → middlegame → endgame) and
    after turning points (big swings in winning chances, critical moments), the
    strongest first, with every chapter at least MIN_CHAPTER plies long.
    """
    n = len(moves)
    if n < 2 * MIN_CHAPTER:
        return [(0, n - 1)] if n else []
    cuts: dict[int, float] = {}          # index where a new chapter starts -> strength
    for i in range(1, n):
        if moves[i].phase and moves[i].phase != moves[i - 1].phase:
            cuts[i] = max(cuts.get(i, 0.0), 50.0)
        swing = abs(moves[i].win_white - moves[i - 1].win_white)
        if swing >= SWING or moves[i].critical:
            cuts[i + 1] = max(cuts.get(i + 1, 0.0), swing)      # the turning point ends its chapter
    chosen: list[int] = []
    for idx, _ in sorted(cuts.items(), key=lambda kv: -kv[1]):
        if MIN_CHAPTER <= idx <= n - MIN_CHAPTER and all(abs(idx - c) >= MIN_CHAPTER for c in chosen):
            chosen.append(idx)
        if len(chosen) >= MAX_CHAPTERS - 1:
            break
    bounds = [0, *sorted(chosen), n]
    return [(bounds[k], bounds[k + 1] - 1) for k in range(len(bounds) - 1)]


def _write_chapters(coach: _Coach, system: str, spans: list[tuple[int, int]], report: GameReport,
                    played: list[plans.PlayedMove], coached: chess.Color | None,
                    stopped: Callable[[], bool]) -> list[dict]:
    """Title + summary for each chapter, written by the best available model.

    Chapters are independent, so a cloud model writes them in parallel.
    """
    def one(k: int) -> dict:
        start, end = spans[k]
        moves = report.moves[start:end + 1]
        first, last = moves[0], moves[-1]
        label = lambda m: f"{m.move_number}. {m.san}" if m.side == "White" else f"{m.move_number}... {m.san}"  # noqa: E731
        side_label = lambda m: (f"{m.move_number}. White {m.san}" if m.side == "White"  # noqa: E731
                                else f"{m.move_number}... Black {m.san}")
        moments = []
        for m in moves:
            if m.classification in ("mistake", "blunder"):
                moments.append(f"{side_label(m)} was a {m.classification} (better was {m.best_san})"
                               + (f"; it ignored the existing threat {m.threat}" if m.threat else ""))
            elif m.critical:
                moments.append(f"{side_label(m)} was a critical moment: the only good move, and it was found")
            if m.left_book:
                moments.append(f"{side_label(m)} {m.left_book}")
        phases = list(dict.fromkeys(m.phase for m in moves if m.phase))
        before = report.moves[start - 1] if start else None
        eval_start = describe_eval(*_white_after(before.analysis, before.side == "White")) if before else \
            "the position was equal"
        ctx = ChapterContext(
            number=k + 1, count=len(spans), start_label=label(first), end_label=label(last),
            phases=" and ".join(phases) or "game", eval_start=eval_start,
            eval_end=("the game was over" if last.analysis.game_result else
                      describe_eval(*_white_after(last.analysis, last.side == "White"))),
            moves=plans.with_gists(played[start:end + 1]),
            commentary=[f"{side_label(m)}: {m.commentary}" for m in moves if m.commentary],
            moments=moments, plans=plans.plan_lines(played[:start], played[start].board) if start else [],
            opening=report.opening if k == 0 else "",
        )
        messages = [{"role": "user", "content": build_chapter_prompt(ctx)}]
        lines = [(played[start].board, [m.san for m in played[start:end + 1]])]
        index = EvidenceIndex([(pm.board, pm.move) for pm in played[start:end + 1]], lines, coached)

        def check(t: str) -> list[str]:
            title, summary = parse_chapter(t)
            missing = [] if summary else ["the SUMMARY line is missing"]
            return missing + index.problems(title + ". " + summary)

        def write(llm: LLMProvider) -> str:
            text = llm.chat(system, messages, max_tokens=_MAX_TOKENS["chapter"])
            return _verified_text(llm, system, messages, text, check, _MAX_TOKENS["chapter"])

        title, summary = parse_chapter(coach.hard(write))
        title = title if title and not index.problems(title) else ""
        rng = f"{first.move_number}–{last.move_number}"
        return {"start": start, "end": end, "range": rng, "title": title or f"Moves {rng}",
                "summary": index.clean(summary)[0]}

    expert = coach.smart()
    parallel = expert is not None and not expert.spec.local
    out: list[dict | None] = [None] * len(spans)
    with ThreadPoolExecutor(max_workers=expert.concurrency if parallel else 1,
                            thread_name_prefix="lucidfish-chapters") as pool:
        futures = {pool.submit(one, k): k for k in range(len(spans))}
        for fut, k in futures.items():
            if stopped():
                break
            try:
                out[k] = fut.result()
            except LLMError as e:
                coach.failed(e)
    return [c for c in out if c is not None and c["summary"]]


# ------------------------------------------------------------------ prefetch

def prefetch_engine(pgn_text: str, cfg: Config, engine_cache, should_stop: Callable[[], bool],
                    on_position: Callable[[int, int], None] | None = None) -> int:
    """Search every position of a game ahead of time, filling the engine cache.

    Used by the analysis queue while the AI coach is still writing up the
    previous game: when this game's real analysis starts, its engine work is
    already done. Identical depth and settings, so results are unchanged —
    only the waiting disappears. Returns the number of positions searched.
    """
    game, _ = parse_game(pgn_text)
    moves = list(game.mainline_moves())
    total = len(moves) + 1
    done = 0
    with EngineAnalyzer(cfg.engine, cache=engine_cache) as engine:
        board = game.board()
        for i in range(total):
            if should_stop():
                break
            if not board.is_game_over():
                engine.top_lines(board)
            done += 1
            if on_position:
                on_position(done, total)
            if i < len(moves):
                board.push(moves[i])
    return done


# ------------------------------------------------------------------ practice

def check_move(fen: str, uci: str, cfg: Config, engine_cache=None) -> dict:
    """Judge a move tried in "practise your mistakes" mode, with the same verdict
    thresholds as the game analysis (the position's lines usually come from the
    engine cache, so only the reply position needs a fresh search)."""
    board = chess.Board(fen)
    move = chess.Move.from_uci(uci)
    if move not in board.legal_moves:
        raise ValueError("That move is not legal in this position.")
    after = board.copy(stack=False)
    after.push(move)
    with EngineAnalyzer(cfg.engine, cache=engine_cache) as engine:
        candidates = engine.top_lines(board)
        lines_after = [] if after.is_game_over() else engine.top_lines(after)
    a = build_move_analysis(board, move, candidates, lines_after, cfg.analysis)
    mover_white = board.turn == chess.WHITE
    solved = a.classification in ("best", "good") or a.mate_event == "delivered_mate"
    return {
        "san": a.played_san, "uci": uci, "cls": a.classification, "solved": solved,
        "best": a.best_san, "best_uci": a.best_uci, "cp_loss": a.cp_loss,
        "eval": a.game_result or eval_text(*_white_after(a, mover_white)),
        "fen_after": after.fen(),
        "refutation": "" if solved else a.refutation_text,
        "refutation_steps": [] if solved else _line_steps(after, a.refutation_san),
        "best_steps": _line_steps(board, a.candidates[0].pv_san) if a.candidates else [],
    }


# ------------------------------------------------------------------ positions

def analyze_position(fen: str, cfg: Config, perspective: str | None = None, level: str | None = None,
                     engine_cache=None) -> dict:
    """One-shot analysis of a set-up position (board editor mode)."""
    from .prompts import build_position_prompt
    board = chess.Board(fen)
    with EngineAnalyzer(cfg.engine, cache=engine_cache) as engine:
        lines = engine.top_lines(board)
    f = feat.extract(board)
    facts = f.summary_lines() + plans.plan_lines([], board)
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
        system = build_system_prompt(persp.lower(), cfg.user_elo, level, rating_label=cfg.user_elo_label)
        prompt = build_position_prompt(board, lines, facts, persp)
        try:
            text = coach.hard(lambda llm: llm.generate(system, prompt, max_tokens=_MAX_TOKENS["full"]))
            commentary, ideas = split_sections(text)
            index = EvidenceIndex([], [(board, ln.pv_san) for ln in lines],
                                  chess.WHITE if persp == "White" else chess.BLACK)
            by_norm = {norm_san(k): v for k, v in ideas.items()}
            result["commentary"] = index.clean(commentary)[0]
            for entry in result["lines"]:
                idea = ideas.get(entry["san"]) or by_norm.get(norm_san(entry["san"]), "")
                entry["idea"] = "" if index.problems(idea) else idea
        except LLMError as e:
            result["warnings"].append(f"The AI coach is unavailable ({e}).")
    return result
