import threading

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
    assert plan("key", "white", "White", "blunder", False, False) == "full"
    assert plan("key", "white", "White", "good", False, False) is None
    assert plan("standard", "white", "White", "good", False, False) == "brief"
    assert plan("standard", "white", "Black", "good", False, False) is None
    assert plan("standard", "white", "Black", "good", False, True) == "opponent"
    assert plan("full", "white", "Black", "best", False, False) == "opponent"
    assert plan("full", None, "Black", "best", False, False) == "full"


class FakeLLM:
    """Stands in for a provider; records calls and can simulate failures."""

    def __init__(self, fail: Exception | None = None):
        self.calls = 0
        self.fail = fail
        self.lock = threading.Lock()

    concurrency = 2

    def describe(self):
        return "Fake · test"

    def chat(self, system, messages, *, max_tokens=None):
        with self.lock:
            self.calls += 1
        if self.fail:
            raise self.fail
        prompt = messages[-1]["content"]
        if "Write the post-game review" in prompt:
            return "## Summary\nGood game.\n## Key takeaways\n- Develop."
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
