"""`lucidfish eval`: how accurately does a given model explain chess?

The question matters because every provider and model narrates the same
evidence differently — and the only honest way to compare them is to measure.
Nobody needs to judge the chess themselves: the *expectations* for every case
come from the engine, and the *grading* is done by the same fact-checkers the
app uses, so the whole thing runs unattended.

Building cases (``--build``): the engine analyses games (any PGN), and every
mistake, blunder and critical moment becomes a case that records the position,
the move, the engine's best move, the refutation, the verified tactical motifs
and the evaluations. The repository ships a set built from a few famous games
(``evals/cases.jsonl``); ``--build`` adds cases from your own.

Running: for each case the coach writes its note exactly as it would in the
app (same prompt, same fact-check, same optional engine checks), and the run
records what the checks found before anything was corrected or removed:

- **grounded** — the first answer named no move, piece or capture the evidence
  does not support;
- **assessment** — its claims about who is better matched the evaluations;
- **best move** / **refutation** — it named them when there was one to name;
- **motif** — it mentioned the verified tactical motif (fork, pin, hanging
  piece, mate threat, missed threat);
- **kept** — the fact-checker did not have to remove anything.

An optional judge (``--judge provider:model``) adds a 1–5 rating of faithfulness
and helpfulness from a strong model that sees the same evidence. Results are
saved as JSON under ``evals/results/`` so two runs can be compared.
"""

from __future__ import annotations

import io
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from statistics import mean

import chess
import chess.pgn

from . import tactics
from .config import Config, LLMConfig
from .llm import LLMError, ask, make_provider
from .prompts import build_move_prompt, build_system_prompt, norm_san, time_note
from .schemas import parse_json

CASES_PATH = Path("evals/cases.jsonl")
RESULTS_DIR = Path("evals/results")
MAX_CASES_PER_GAME = 10

# Words a note may use for each verified motif tag (any one counts as mentioning it).
_MOTIF_WORDS = {
    "fork": ("fork", "double attack", "two pieces at once", "attacks both"),
    "pin": ("pin",),
    "skewer": ("skewer", "x-ray"),
    "checkmate": ("mate",),
    "mate threat": ("mate",),
    "allows mate threat": ("mate",),
    "wins material": ("material", "wins the", "win the", "winning the", "picks up", "pick up", "captur", "takes",
                      "free", "hanging", "en prise", "undefended", "loses the", "lose the", "drops"),
    "hangs material": ("material", "hanging", "hangs", "en prise", "undefended", "unprotected", "loses the",
                       "lose the", "drops", "gives up", "give up", "free", "captur", "takes"),
    "sacrifice": ("sacrific", "gives up", "give up", "material"),
    "missed threat": ("threat", "already", "ignor", "overlook", "allow"),
}

_JUDGE_SYSTEM = """You grade a chess coach's note against the verified evidence it was written from. \
You never analyse the position yourself: the evidence is ground truth.
Reply with a JSON object only: {"faithful": <1-5>, "helpful": <1-5>, "comment": "<one sentence>"}.
faithful — 5: every claim is supported by the evidence and the sides, pieces and moves are right; \
3: one minor unsupported or vague claim; 1: invents tactics, misattributes moves or contradicts the \
evaluations.
helpful — 5: a player at the stated level would understand why the move was wrong and what to do \
instead; 3: correct but generic; 1: unhelpful or confusing."""

_JUDGE_SCHEMA = {"title": "grade", "type": "object",
                 "properties": {"faithful": {"type": "integer"}, "helpful": {"type": "integer"},
                                "comment": {"type": "string"}},
                 "required": ["faithful", "helpful", "comment"], "additionalProperties": False}


# ------------------------------------------------------------------ cases

@dataclass
class Case:
    id: str
    source: str
    ply: int
    side: str
    san: str
    cls: str
    critical: bool
    fen_before: str
    pgn: str
    expect: dict = field(default_factory=dict)

    @property
    def coached(self) -> str:
        return self.side.lower()


def _slug(path: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", Path(path).stem.lower()).strip("-") or "game"


def _games(path: Path) -> list[tuple[str, str]]:
    """(label, pgn text) for every game in a PGN file."""
    out = []
    text = path.read_text(encoding="utf-8", errors="replace")
    stream = io.StringIO(text)
    n = 0
    while True:
        game = chess.pgn.read_game(stream)
        if game is None:
            break
        n += 1
        pgn = str(game)
        if list(game.mainline_moves()):
            out.append((f"{_slug(str(path))}" + (f"-g{n}" if n > 1 else ""), pgn))
    return out


def build_cases(pgn_paths: list[str], cfg: Config, engine_cache=None, max_per_game: int = MAX_CASES_PER_GAME,
                progress=None) -> list[Case]:
    """Engine-only analysis of each game → one case per mistake, blunder or critical moment."""
    from .pipeline import analyse_game
    engine_cfg = Config(engine=cfg.engine, analysis=cfg.analysis)
    engine_cfg.llm.enabled = False
    cases: list[Case] = []
    for path in pgn_paths:
        for label, pgn in _games(Path(path)):
            report = analyse_game(pgn, engine_cfg, engine_cache=engine_cache)
            picked = [m for m in report.moves if m.classification in ("mistake", "blunder") or m.critical]
            weight = {"blunder": 3, "mistake": 2}
            picked.sort(key=lambda m: -(weight.get(m.classification, 0) + (2 if m.critical else 0)))
            for m in sorted(picked[:max_per_game], key=lambda m: m.ply):
                a = m.analysis
                board = chess.Board(m.fen_before, chess960=m.chess960)
                after = board.copy(stack=False)
                after.push(chess.Move.from_uci(m.uci))
                motifs = []
                if a.best_uci and a.best_uci != m.uci:
                    motifs += tactics.move_motifs(board, chess.Move.from_uci(a.best_uci))
                if a.refutation_san:
                    try:
                        motifs += tactics.move_motifs(after, after.parse_san(a.refutation_san[0]))
                    except ValueError:
                        pass
                tags = tactics.tags_from_motifs(motifs) + (["missed threat"] if m.threat else [])
                cases.append(Case(
                    id=f"{label}-{m.ply:03d}", source=str(path), ply=m.ply, side=m.side, san=m.san,
                    cls=m.classification, critical=m.critical, fen_before=m.fen_before, pgn=pgn,
                    expect={"best": a.best_san if a.best_uci != m.uci else "",
                            "refutation": a.refutation_san[0] if a.refutation_san else "",
                            "motifs": list(dict.fromkeys(tags)), "threat": m.threat, "eval_after": m.eval_str,
                            "win_before": round(a.win_before), "win_after": round(a.win_after)},
                ))
            if progress:
                progress(label, len(cases))
    return cases


def save_cases(cases: list[Case], path: Path = CASES_PATH) -> int:
    """Append cases not yet in the file; returns how many were added."""
    existing = {c.id for c in load_cases(path)} if path.exists() else set()
    new = [c for c in cases if c.id not in existing]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for c in new:
            f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
    return len(new)


def load_cases(path: Path = CASES_PATH) -> list[Case]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            cases.append(Case(**json.loads(line)))
    return cases


# ------------------------------------------------------------------ grading

def _mentions_move(text: str, san: str) -> bool:
    if not san:
        return False
    plain = re.sub(r"[+#!?]", "", san)
    forms = {san, plain, norm_san(san)}
    return any(re.search(rf"(?<![\w/]){re.escape(f)}(?![\w=])", text) for f in forms if f)


def _mentions_motif(text: str, tags: list[str]) -> bool | None:
    words = [w for t in tags for w in _MOTIF_WORDS.get(t, ())]
    if not words:
        return None
    low = text.lower()
    return any(w in low for w in words)


@dataclass
class Outcome:
    case: str
    cls: str
    side: str
    san: str
    seconds: float
    json_answer: bool
    explanation: str
    raw: str
    evidence_problems: list[str]
    assessment_problems: list[str]
    corrected: bool
    removed: int
    checks: list[dict]
    grounded: bool
    assessment_ok: bool
    mentions_best: bool | None
    mentions_refutation: bool | None
    mentions_motif: bool | None
    kept: bool
    error: str = ""
    judge: dict | None = None

    @property
    def score(self) -> float | None:
        marks = [self.grounded, self.assessment_ok, self.mentions_best, self.mentions_refutation,
                 self.mentions_motif, self.kept]
        marks = [m for m in marks if m is not None]
        return 100 * mean(marks) if marks and not self.error else (0.0 if self.error else None)


def grade(case: Case, explanation: str, trace: dict, seconds: float, checks: list[dict]) -> Outcome:
    raw = trace.get("raw", "")
    data = parse_json(raw)
    expect = case.expect
    text = explanation or ""
    best, refutation = expect.get("best", ""), expect.get("refutation", "")
    return Outcome(
        case=case.id, cls=case.cls, side=case.side, san=case.san, seconds=round(seconds, 1),
        json_answer=bool(data) and isinstance(data, dict) and bool(data.get("explanation")),
        explanation=text, raw=raw,
        evidence_problems=list(trace.get("evidence_problems", [])),
        assessment_problems=list(trace.get("assessment_problems", [])),
        corrected=bool(trace.get("corrected")), removed=int(trace.get("removed", 0)), checks=checks,
        grounded=not trace.get("evidence_problems") and not trace.get("missing"),
        assessment_ok=not trace.get("assessment_problems"),
        mentions_best=_mentions_move(text, best) if best else None,
        mentions_refutation=_mentions_move(text, refutation) if refutation else None,
        mentions_motif=_mentions_motif(text, expect.get("motifs", [])),
        kept=int(trace.get("removed", 0)) == 0 and bool(text.strip()),
    )


# ------------------------------------------------------------------ running

def run(cases: list[Case], cfg: Config, *, engine_cache=None, judge: str = "", progress=None) -> dict:
    """Write a note for every case with the configured coach and grade it. Returns the results document."""
    from .pipeline import _write_note, analyse_game, build_coach, evidence_for, parse_game, played_moves
    coach, warnings = build_coach(cfg)
    if not coach.available:
        raise LLMError(warnings[0] if warnings else "The AI coach is turned off.", fatal=True)
    judge_llm = None
    if judge:
        provider, _, model = judge.partition(":")
        judge_llm = make_provider(LLMConfig(provider=provider, model=model))
    engine_cfg = Config(engine=cfg.engine, analysis=cfg.analysis)
    engine_cfg.llm.enabled = False

    reports: dict[str, tuple] = {}
    outcomes: list[Outcome] = []
    for n, case in enumerate(cases, 1):
        if case.pgn not in reports:
            report = analyse_game(case.pgn, engine_cfg, engine_cache=engine_cache)
            game, _ = parse_game(case.pgn)
            reports[case.pgn] = (report, played_moves(game))
        report, played = reports[case.pgn]
        idx = next((i for i, m in enumerate(report.moves) if m.ply == case.ply), None)
        if idx is None:
            continue
        coached = case.coached
        colour = chess.WHITE if coached == "white" else chess.BLACK
        system = build_system_prompt(coached, cfg.user_elo, None, "",
                                     players={"White": report.headers.get("White", "White"),
                                              "Black": report.headers.get("Black", "Black")},
                                     rating_label=cfg.user_elo_label)
        ev = evidence_for(report, idx, played, coached)
        trace: dict = {}
        t0 = time.monotonic()
        write = partial(_write_note, system=system, ev=ev, side_filter=coached, coached=colour, played=played,
                        cfg=cfg, coach=coach, engine_cache=engine_cache, trace=trace)
        try:
            explanation, _ideas, checks = coach.hard(write)
            outcome = grade(case, explanation, trace, time.monotonic() - t0, checks)
        except LLMError as e:
            outcome = grade(case, "", trace, time.monotonic() - t0, [])
            outcome.error = str(e)
        if judge_llm is not None and not outcome.error:
            outcome.judge = _judge(judge_llm, ev, system, coached, outcome.explanation)
        outcomes.append(outcome)
        if progress:
            progress(n, len(cases), outcome)
    return {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "coach": coach.describe(),
        "settings": {"provider": cfg.llm.provider, "model": cfg.llm.model,
                     "expert": cfg.expert.model if cfg.expert else "", "verify_lines": cfg.llm.verify_lines,
                     "factcheck": cfg.llm.factcheck, "depth": cfg.engine.depth, "judge": judge},
        "summary": summarise(outcomes),
        "cases": [asdict(o) | {"score": o.score} for o in outcomes],
    }


def _judge(llm, ev, system: str, coached: str, explanation: str) -> dict | None:
    """A strong model rates the note against the same evidence (1-5 faithful, 1-5 helpful)."""
    evidence = build_move_prompt(
        board=ev.board, move=ev.move, analysis=ev.analysis, note_type="full", f_before=ev.f_before,
        f_after=ev.f_after, coached_side=coached, critical=ev.critical, motifs_played=ev.motifs,
        motifs_best=ev.motifs_best, motifs_reply=ev.motifs_reply, reply_line=ev.reply_line,
        opening_name=ev.opening_name, opening_lines=ev.opening_lines, recent=ev.recent, hindsight=ev.hindsight,
        plans=ev.plan_facts, threat=ev.threat, left_book=ev.left_book,
        time_note=time_note(ev.board.turn and "White" or "Black", ev.think_s, ev.clock_s, ev.time_class),
        previous=ev.previous, want_ideas=False)
    prompt = (f"The coach's instructions were:\n{system}\n\n--- EVIDENCE GIVEN TO THE COACH ---\n{evidence}\n\n"
              f"--- THE COACH'S NOTE ---\n{explanation or '(empty)'}\n\nGrade the note.")
    try:
        data = parse_json(ask(llm, _JUDGE_SYSTEM, [{"role": "user", "content": prompt}], schema=_JUDGE_SCHEMA,
                              effort="medium"))
    except LLMError:
        return None
    if not data:
        return None
    try:
        return {"faithful": max(1, min(5, int(data.get("faithful", 0)))),
                "helpful": max(1, min(5, int(data.get("helpful", 0)))), "comment": str(data.get("comment", ""))[:300]}
    except (TypeError, ValueError):
        return None


def _rate(values: list) -> float | None:
    values = [v for v in values if v is not None]
    return round(100 * mean(values), 1) if values else None


def summarise(outcomes: list[Outcome]) -> dict:
    done = [o for o in outcomes if not o.error]
    checks = [c for o in done for c in o.checks]
    judged = [o.judge for o in done if o.judge]
    return {
        "cases": len(outcomes), "errors": len(outcomes) - len(done),
        "score": round(mean(s for o in outcomes if (s := o.score) is not None), 1) if outcomes else None,
        "json_answer": _rate([o.json_answer for o in done]),
        "grounded": _rate([o.grounded for o in done]),
        "assessment": _rate([o.assessment_ok for o in done]),
        "best_move": _rate([o.mentions_best for o in done]),
        "refutation": _rate([o.mentions_refutation for o in done]),
        "motif": _rate([o.mentions_motif for o in done]),
        "kept": _rate([o.kept for o in done]),
        "corrected": _rate([o.corrected for o in done]),
        "engine_checks": len(checks), "engine_checks_legal": sum(1 for c in checks if c.get("legal")),
        "seconds_per_note": round(mean(o.seconds for o in done), 1) if done else None,
        "judge_faithful": round(mean(j["faithful"] for j in judged), 2) if judged else None,
        "judge_helpful": round(mean(j["helpful"] for j in judged), 2) if judged else None,
    }


def save_results(doc: dict, out_dir: Path = RESULTS_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    model = re.sub(r"[^A-Za-z0-9.-]+", "_", f"{doc['settings']['provider']}-{doc['settings']['model'] or 'default'}")
    path = out_dir / f"{stamp}-{model}.json"
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    return path


def previous_result(doc: dict, out_dir: Path = RESULTS_DIR) -> dict | None:
    """The latest earlier run with the same provider and model, for a before/after comparison."""
    if not out_dir.exists():
        return None
    for path in sorted(out_dir.glob("*.json"), reverse=True):
        try:
            other = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        same = other.get("settings", {}).get("provider") == doc["settings"]["provider"] and \
            other.get("settings", {}).get("model") == doc["settings"]["model"]
        if same and other.get("run_at") != doc.get("run_at"):
            return other
    return None


# ------------------------------------------------------------------ command line

def main(argv: list[str]) -> int:
    import argparse

    from rich.console import Console
    from rich.table import Table

    parser = argparse.ArgumentParser(
        prog="lucidfish eval",
        description="Measure how accurately the AI coach explains chess. Expectations come from the engine and "
                    "grading is automatic, so no chess judgement is needed.")
    parser.add_argument("--build", nargs="+", metavar="PGN", help="analyse these games and add their mistakes, "
                        "blunders and critical moments to the case file (engine only, no AI needed)")
    parser.add_argument("--cases", default=str(CASES_PATH), help=f"case file (default {CASES_PATH})")
    parser.add_argument("--limit", type=int, help="run only the first N cases")
    parser.add_argument("--provider", help="AI provider (default: the saved settings)")
    parser.add_argument("--model", help="model name")
    parser.add_argument("--expert-provider", help="second model's provider ('none' = off)")
    parser.add_argument("--expert-model", help="second model")
    parser.add_argument("--no-tools", action="store_true", help="do not let the coach check lines with the engine")
    parser.add_argument("--judge", metavar="PROVIDER:MODEL", default="",
                        help="also have this model rate each note 1-5 for faithfulness and helpfulness")
    parser.add_argument("--depth", type=int, help="engine depth for the analysis (default: the saved setting)")
    parser.add_argument("--out", help="write the results JSON here (default: evals/results/<time>-<model>.json)")
    args = parser.parse_args(argv)
    console = Console()

    from . import store
    from .settings import load_config
    cfg = load_config(provider=args.provider, model=args.model, expert_provider=args.expert_provider,
                      expert_model=args.expert_model)
    if args.depth:
        cfg.engine.depth = args.depth
    if args.no_tools:
        cfg.llm.verify_lines = False
    cfg.llm.enabled = True
    cache = store.EngineCache() if cfg.engine.use_cache else None
    cases_path = Path(args.cases)

    if args.build:
        console.print(f"Analysing {len(args.build)} file(s) with {cfg.engine.path} at depth {cfg.engine.depth}…")
        try:
            cases = build_cases(args.build, cfg, engine_cache=cache,
                                progress=lambda label, n: console.print(f"  {label}: {n} cases so far"))
        except (RuntimeError, ValueError) as e:
            console.print(f"[red]Error:[/red] {e}")
            return 1
        added = save_cases(cases, cases_path)
        console.print(f"Added {added} new case(s) to {cases_path} ({len(load_cases(cases_path))} in total).")
        return 0

    if not cases_path.exists():
        console.print(f"No case file at {cases_path}. Build one with: lucidfish eval --build GAME.pgn")
        return 1
    cases = load_cases(cases_path)[: args.limit or None]
    second = f" (+ {cfg.expert.provider} · {cfg.expert.model or 'default'} for the hard parts)" if cfg.expert else ""
    console.print(f"Running {len(cases)} case(s) with {cfg.llm.provider} · {cfg.llm.model or 'default model'}{second}"
                  + (", engine checks off" if not cfg.llm.verify_lines else "") + "…")
    table = Table(show_lines=False)
    for col in ("case", "move", "verdict", "grounded", "assessment", "best", "refutation", "motif", "kept",
                "checks", "s"):
        table.add_column(col)

    def mark(v):
        return "[green]✔[/green]" if v else "[red]✘[/red]" if v is not None else "[dim]–[/dim]"

    def show(n, total, o: Outcome):
        if o.error:
            console.print(f"[red]✘[/red] {o.case}: {o.error}")
            return
        table.add_row(o.case, f"{o.side} {o.san}", o.cls + (" ★" if "critical" in o.cls else ""),
                      mark(o.grounded), mark(o.assessment_ok), mark(o.mentions_best), mark(o.mentions_refutation),
                      mark(o.mentions_motif), mark(o.kept), str(len(o.checks)), f"{o.seconds:.0f}")
        console.print(f"[{n}/{total}] {o.case}: {'ok' if o.score and o.score >= 80 else 'issues'} "
                      f"({o.score:.0f}%)" if o.score is not None else f"[{n}/{total}] {o.case}")

    try:
        doc = run(cases, cfg, engine_cache=cache, judge=args.judge, progress=show)
    except (LLMError, RuntimeError, ValueError) as e:
        console.print(f"[red]Error:[/red] {e}")
        return 1
    console.print(table)
    s = doc["summary"]
    console.print(f"\n[bold]Score {s['score']}%[/bold] over {s['cases']} cases ({s['errors']} errors) — "
                  f"grounded {s['grounded']}%, assessment {s['assessment']}%, best move {s['best_move']}%, "
                  f"refutation {s['refutation']}%, motif {s['motif']}%, kept {s['kept']}%, JSON {s['json_answer']}%; "
                  f"{s['engine_checks']} engine checks ({s['engine_checks_legal']} legal); "
                  f"{s['seconds_per_note']} s per note.")
    if s.get("judge_faithful") is not None:
        console.print(f"Judge: faithful {s['judge_faithful']}/5, helpful {s['judge_helpful']}/5.")
    before = previous_result(doc, Path(args.out).parent if args.out else RESULTS_DIR)
    if before and before.get("summary", {}).get("score") is not None and s["score"] is not None:
        delta = s["score"] - before["summary"]["score"]
        console.print(f"Previous run of this model ({before['run_at']}): {before['summary']['score']}% → "
                      f"{'+' if delta >= 0 else ''}{delta:.1f} points.")
    path = Path(args.out) if args.out else save_results(doc)
    if args.out:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    console.print(f"Results written to {path}", soft_wrap=True)
    return 0


__all__ = ["Case", "Outcome", "build_cases", "grade", "load_cases", "run", "save_cases", "summarise"]
