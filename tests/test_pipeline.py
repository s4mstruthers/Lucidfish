import re
import threading
from types import SimpleNamespace

import pytest

from conftest import SAMPLE_PGN, fast_config, needs_engine
from lucidfish import pipeline
from lucidfish.llm import LLMError


def test_parse_time_control():
    assert pipeline.parse_time_control("600+5") == (600, 5, "rapid")
    assert pipeline.parse_time_control("180+2") == (180, 2, "blitz")
    assert pipeline.parse_time_control("60") == (60, 0, "bullet")
    assert pipeline.parse_time_control("1/259200") == (None, 0, "daily")
    assert pipeline.parse_time_control("-") == (None, 0, "")


def test_parse_game_errors_and_warnings():
    with pytest.raises(ValueError):
        pipeline.parse_game("")
    with pytest.raises(ValueError):
        pipeline.parse_game('[Variant "Crazyhouse"]\n\n1. e4 e5 *')
    game, warnings = pipeline.parse_game("1. e4 e5 2. Nf3 Qxz9 3. Bc4 *")
    assert len(list(game.mainline_moves())) == 3 and warnings


def test_plan_note_matrix():
    plan = pipeline.plan_note
    assert plan("key", "white", "White", "blunder", False) == "full"
    assert plan("key", "white", "White", "good", False) is None
    assert plan("key", "white", "White", "best", True) == "full"          # critical moment
    assert plan("key", "white", "Black", "blunder", False) is None        # opponent's errors: no note
    assert plan("standard", "white", "White", "good", False) is None      # covered by commentary windows
    assert plan("standard", "white", "White", "mistake", False) == "full"
    assert plan("full", "white", "Black", "best", False) == "opponent"
    assert plan("full", None, "Black", "best", False) == "full"


class FakeLLM:
    """Stands in for a provider; records every request and can simulate failures.

    `slip` adds an impossible move to every commentary line (the fact-checker must catch it).
    """

    def __init__(self, fail: Exception | None = None, *, name="Fake", local=True, slip=False):
        self.calls = 0
        self.windows = 0
        self.prompts: list[str] = []          # first user message of each request
        self.corrections = 0
        self.fail = fail
        self.name = name
        self.slip = slip
        self.spec = SimpleNamespace(local=local)
        self.lock = threading.Lock()

    concurrency = 2

    def describe(self):
        return f"{self.name} · test"

    def chat(self, system, messages, *, max_tokens=None):
        first, last = messages[0]["content"], messages[-1]["content"]
        fixing = len(messages) > 1 and "Your answer contains claims" in last
        with self.lock:
            self.calls += 1
            self.prompts.append(first)
            self.corrections += fixing
        if self.fail:
            raise self.fail
        if "Write the post-game review" in first:
            return "## Summary\nGood game.\n## Key takeaways\n- Develop."
        if first.startswith("COMMENTARY WINDOW"):
            with self.lock:
                self.windows += not fixing
            labels = re.findall(r"^- (\S+ \S+) \((White|Black)\)", first, re.M)
            bad = " Black then plays Kxa1." if self.slip and not fixing else ""
            return "\n".join(f"{label}: {side} continues the plan.{bad}" for label, side in labels)
        return "EXPLANATION:\nA grounded note."

    def generate(self, system, prompt, *, max_tokens=None):
        return self.chat(system, [{"role": "user", "content": prompt}], max_tokens=max_tokens)


def _patch_llm(monkeypatch, llm):
    monkeypatch.setattr(pipeline, "make_provider", lambda cfg: llm)


@needs_engine
def test_engine_only_analysis():
    report = pipeline.analyze_game(SAMPLE_PGN, fast_config(), side_filter="white")
    assert len(report.moves) == 33
    assert report.moves[-1].eval_str == "1-0"
    assert report.moves[-1].tags == ["checkmate"]
    assert report.accuracy["white"] > report.accuracy["black"]
    assert report.coach == "" and report.review == ""
    assert all(not m.explanation for m in report.moves)
    d = report.moves[0].to_dict()
    assert {"ply", "san", "cls", "eval", "win", "acc", "candidates", "fen_after"} <= d.keys()


@needs_engine
def test_notes_follow_detail_level(monkeypatch):
    llm = FakeLLM()
    _patch_llm(monkeypatch, llm)
    cfg = fast_config(enabled=True)
    cfg.analysis.detail = "key"
    report = pipeline.analyze_game(SAMPLE_PGN, cfg, side_filter="white")
    noted = [m for m in report.moves if m.explanation]
    assert noted and len(noted) < 17                     # only key moments
    assert all(m.side == "White" for m in noted)
    assert report.review.startswith("## Summary")
    assert [m.ply for m in report.moves] == list(range(33))   # in order despite parallel notes


@needs_engine
def test_standard_detail_comments_on_every_move_in_windows(monkeypatch):
    llm = FakeLLM()
    _patch_llm(monkeypatch, llm)
    cfg = fast_config(enabled=True)
    cfg.analysis.detail = "standard"
    report = pipeline.analyze_game(SAMPLE_PGN, cfg, side_filter="white")
    assert all(m.commentary.startswith(m.side) for m in report.moves)   # both players, every move
    assert llm.windows == -(-len(report.moves) // pipeline.WINDOW)     # 8 plies per request
    assert llm.calls < len(report.moves)                                # cheaper than one call per move
    assert all(m.explanation for m in report.moves
               if m.side == "White" and m.classification in pipeline.ERRORS)
    assert report.moves[0].book and report.moves[0].phase == "opening"
    assert report.moves[0].to_dict()["flow"] == report.moves[0].commentary


@needs_engine
def test_llm_failure_falls_back_to_engine_only_and_releases_engine(monkeypatch):
    llm = FakeLLM(fail=LLMError("Cannot reach Ollama", fatal=True))
    _patch_llm(monkeypatch, llm)
    closed = []

    class TrackedEngine(pipeline.EngineAnalyzer):
        def close(self):
            closed.append(True)
            super().close()

    monkeypatch.setattr(pipeline, "EngineAnalyzer", TrackedEngine)
    report = pipeline.analyze_game(SAMPLE_PGN, fast_config(enabled=True), side_filter="white")
    assert len(report.moves) == 33
    assert any("stopped responding" in w for w in report.warnings)
    assert llm.calls <= 3                                 # gave up quickly instead of retrying every move
    assert closed == [True]                               # Stockfish was shut down, not leaked


@needs_engine
def test_stop_returns_partial_results():
    seen = []
    report = pipeline.analyze_game(SAMPLE_PGN, fast_config(), on_move=seen.append,
                                   should_stop=lambda: len(seen) >= 5)
    assert 5 <= len(report.moves) < 33 and report.review == ""


@needs_engine
def test_analyze_position():
    result = pipeline.analyze_position("6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1", fast_config())
    assert result["lines"][0]["san"] == "Rd8#" and result["lines"][0]["score"] == "#1"
    assert result["turn"] == "White" and result["features"]


@needs_engine
def test_check_move_for_practice_mode():
    fen = "6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1"
    good = pipeline.check_move(fen, "d1d8", fast_config())
    assert good["solved"] and good["san"] == "Rd8#" and good["eval"] == "1-0"
    bad = pipeline.check_move(fen, "h2h3", fast_config())
    assert not bad["solved"] and bad["best"] == "Rd8#" and bad["best_steps"][0]["san"] == "Rd8#"
    with pytest.raises(ValueError):
        pipeline.check_move(fen, "a1a8", fast_config())            # no piece there: illegal


# ------------------------------------------------------------------ two models


def _two_models(monkeypatch, main, expert, escalate=True):
    from lucidfish.config import LLMConfig
    monkeypatch.setattr(pipeline, "make_provider", lambda c: expert if c.provider == "anthropic" else main)
    cfg = fast_config(enabled=True, escalate=escalate)
    cfg.analysis.detail = "standard"
    cfg.expert = LLMConfig(provider="anthropic", model="claude-haiku-4-5")
    return cfg


@needs_engine
def test_expert_model_writes_the_hard_parts(monkeypatch):
    main, expert = FakeLLM(name="Local"), FakeLLM(name="Cloud", local=False)
    report = pipeline.analyze_game(SAMPLE_PGN, _two_models(monkeypatch, main, expert), side_filter="white")
    assert all(p.startswith("COMMENTARY WINDOW") for p in main.prompts)      # main: running commentary only
    assert main.windows == -(-len(report.moves) // pipeline.WINDOW)
    assert any("Write the post-game review" in p for p in expert.prompts)
    hard = [m for m in report.moves if m.side == "White" and (m.classification in pipeline.ERRORS or m.critical)]
    assert hard and all(m.explanation for m in hard)
    notes = [p for p in expert.prompts if not p.startswith("COMMENTARY WINDOW") and "post-game review" not in p]
    assert len(notes) >= len(hard)
    assert report.coach == "Local · test + Cloud · test for key moments"


@needs_engine
def test_commentary_that_fails_the_fact_check_is_rewritten_by_the_expert(monkeypatch):
    main, expert = FakeLLM(name="Local", slip=True), FakeLLM(name="Cloud", local=False)
    report = pipeline.analyze_game(SAMPLE_PGN, _two_models(monkeypatch, main, expert), side_filter="white")
    assert expert.corrections == main.windows and main.corrections == 0
    assert all(m.commentary and "Kxa1" not in m.commentary for m in report.moves)
    # Without escalation the main model corrects itself.
    main, expert = FakeLLM(name="Local", slip=True), FakeLLM(name="Cloud", local=False)
    pipeline.analyze_game(SAMPLE_PGN, _two_models(monkeypatch, main, expert, escalate=False), side_filter="white")
    assert main.corrections == main.windows and expert.corrections == 0


@needs_engine
def test_main_model_takes_over_when_the_expert_fails(monkeypatch):
    main = FakeLLM(name="Local")
    expert = FakeLLM(LLMError("Anthropic rejected the API key", fatal=True), name="Cloud", local=False)
    report = pipeline.analyze_game(SAMPLE_PGN, _two_models(monkeypatch, main, expert), side_filter="white")
    assert expert.calls == 1                                   # gave up on it after the fatal error
    assert report.review.startswith("## Summary")              # written by the main model instead
    assert any("second model stopped working" in w for w in report.warnings)
    assert all(m.commentary for m in report.moves)


def test_a_garbled_correction_never_replaces_the_original():
    class Garbled(FakeLLM):
        def chat(self, system, messages, *, max_tokens=None):
            return "Sorry, I can't help with that."
    text = "11. axb4: fine.\n11... Qc7: Black then plays Kxa1."
    check = lambda t: ["bad"] * (2 if "Kxa1" in t else 0) + ([] if "11. axb4" in t else ["missing"] * 2)  # noqa: E731
    assert pipeline._verified_text(Garbled(), "sys", [{"role": "user", "content": "x"}], text, check, 100) == text
