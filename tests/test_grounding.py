# Copyright (C) 2026 Sam Struthers
# SPDX-License-Identifier: AGPL-3.0-or-later
# This file is part of Lucidfish <https://github.com/s4mstruthers/Lucidfish>; see LICENSE for the terms.

"""The new grounding layers: line consequences, JSON answer shapes, assessment checks, the engine tool,
what players at the user's level do, and the eval set's automatic grading."""
import json

import chess
import pytest

from conftest import SAMPLE_PGN, fast_config, needs_engine
from lucidfish import evalset, lines, pipeline, schemas
from lucidfish.llm import Reply, ToolCall
from lucidfish.opening import OpeningExplorer, OpeningInfo
from lucidfish.prompts import AssessmentIndex, eval_tier, parse_chapter, parse_commentary, parse_note

ITALIAN = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"


# ------------------------------------------------------------------ what a line leads to

def test_line_consequences_report_material_mate_and_illegal_moves():
    board = chess.Board(ITALIAN)
    text = lines.line_consequences(board, ["Nxe5", "Nxe5", "d4"])
    assert "White is down 2 points of material by the end" in text
    assert "White gives up the knight on e5" in text and "Black loses the pawn on e5" in text
    mate = chess.Board("r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4")
    assert lines.line_consequences(mate, ["Qxf7#"]).startswith("the line ends in CHECKMATE: White wins")
    stopped = lines.play_line(board, ["Bb5", "a6", "Bxc6", "dxc6", "Qh5"])
    assert stopped.illegal == "Qh5" and stopped.sans == ["Bb5", "a6", "Bxc6", "dxc6"]
    assert "an even trade" in " ".join(stopped.clauses())
    assert lines.line_consequences(chess.Board("8/5k2/8/3P4/8/8/5K2/8 w - - 0 1"), ["Kf3"]) == ""   # nothing to say
    castle = lines.play_line(chess.Board(ITALIAN), ["Bc4", "Nf6", "O-O"])
    assert "White castles kingside" in castle.clauses()


# ------------------------------------------------------------------ JSON shapes

def test_answers_are_parsed_from_json_first_and_plain_text_second():
    assert parse_note('{"explanation": "Fine.", "line_ideas": [{"move": "...Nf6", "idea": "develops"}]}') == \
        ("Fine.", {"Nf6": "develops"})
    assert parse_note("EXPLANATION:\nLegacy.\nLINE IDEAS:\nNf3: develops.") == ("Legacy.", {"Nf3": "develops."})
    assert parse_chapter('{"title": "\\"Black\'s storm\\"", "summary": "Black  pushes.\\nMore."}') == \
        ("Black's storm", "Black pushes. More.")
    assert parse_chapter("TITLE: A\nSUMMARY: B") == ("A", "B")
    from lucidfish.prompts import CommentaryMove
    moves = [CommentaryMove(label="11. axb4", side="White", san="axb4", what="", verdict="best", eval_after=""),
             CommentaryMove(label="11... Qc7", side="Black", san="Qc7", what="", verdict="good", eval_after="")]
    as_json = json.dumps({"lines": [{"move": "11. axb4", "comment": "White opens the a-file."},
                                    {"move": "11... Qc7", "comment": "Black lines up."}]})
    assert parse_commentary(as_json, moves) == {"11. axb4": "White opens the a-file.", "11... Qc7": "Black lines up."}


def test_review_and_profile_json_become_the_texts_the_checkers_expect():
    review = schemas.review_markdown(json.dumps({"summary": "Good game.", "story": ["Moves 1-5: both develop."],
                                                 "went_well": ["Move 10 (Nf3): fine"],
                                                 "takeaways": ["- Move 12 (Qh5): check threats"]}))
    assert review.split("\n") == ["## Summary", "Good game.", "", "## How the game unfolded",
                                  "- Moves 1-5: both develop.", "", "## What went well", "- Move 10 (Nf3): fine", "",
                                  "## Key takeaways", "- Move 12 (Qh5): check threats"]
    assert schemas.review_markdown("## Summary\nplain") == "## Summary\nplain"      # plain text passes through
    profile = schemas.profile_markdown(json.dumps({"intro": "You stand well.", "time_controls": ["Blitz: ok"],
                                                   "weaknesses": ["**Hanging pieces** — twice"], "strengths": [],
                                                   "openings": ["Italian: 3 games"], "train": ["Threat check"]}))
    assert profile.startswith("You stand well.\n\n### ⏱ By time control\n- Blitz: ok")
    assert "- **Hanging pieces** — twice" in profile and "### ✓ Working well" not in profile
    assert "### ⏱" not in schemas.profile_markdown(json.dumps({"intro": "x", "weaknesses": ["y"]}), "rapid")
    assert schemas.profile_schema("blitz")["required"] == ["intro", "weaknesses", "strengths", "progress", "openings",
                                                           "train"]
    for schema in (schemas.NOTE, schemas.WINDOW, schemas.CHAPTER, schemas.REVIEW, schemas.POSITION):
        assert schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])


# ------------------------------------------------------------------ assessments

def test_assessment_claims_must_match_an_evaluation_shown():
    assert eval_tier(10, None) == (None, 0) and eval_tier(-120, None) == (chess.BLACK, 2)
    assert eval_tier(None, 3) == (chess.WHITE, 6) and eval_tier(900, None) == (chess.WHITE, 5)
    ix = (AssessmentIndex(coached=chess.WHITE).add("with best play", 10, None)
          .add("after the move", -120, None).add("after Nf3", 40, None))
    fine = ["Black is clearly better after the move.", "The position is equal with best play.",
            "White is slightly better after Nf3.", "Black is better.", "You are now worse.",
            "White is not winning here.", "White's winning chances dropped.", "White is winning a pawn with Nxe5.",
            "Black is better placed.", "an edge for White", "The position is level.", "White has the upper hand.",
            "This leaves Black much better.", "White ends up slightly worse after the trade."]
    for text in fine:
        assert ix.problems(text) == [], text
    wrong = ["White is winning after this.", "Black is winning.", "You are lost.", "a winning position for Black",
             "White stands clearly better here.", "White is completely winning.", "White has mate in 3."]
    for text in wrong:
        problems = ix.problems(text)
        assert problems and "contradicts the engine's evaluation" in problems[0], text
    cleaned, removed = ix.clean("Black is better. White is winning after this. The a-file opens.")
    assert cleaned == "Black is better. The a-file opens." and removed == 1
    over = AssessmentIndex().add_result("after the move", chess.WHITE)
    assert over.problems("White is winning and delivers mate.") == [] and over.problems("Black is better.")


# ------------------------------------------------------------------ the engine tool

@needs_engine
def test_check_line_tool_reports_legality_facts_and_evaluation():
    from lucidfish.tools import CHECK_LINE, LineChecker
    cfg = fast_config()
    board = chess.Board(ITALIAN)
    with LineChecker(board, cfg) as checker:
        out = checker.check({"fen": None, "moves": ["3.", "Nxe5", "Nxe5", "d4"]})
        assert out["ok"] and out["line"] == "3. White Nxe5, 3... Black Nxe5, 4. White d4"
        assert any("down 2 points" in w for w in out["what_happens"])
        assert out["evaluation_at_end"].startswith("Black is") and "worse for White" in out["verdict"]
        bad = json.loads(checker(ToolCall("1", "check_line", {"fen": None, "moves": ["Qh5"]})))
        assert not bad["ok"] and bad["illegal_move"] == "Qh5" and "legal moves here include" in bad["note"]
        assert json.loads(checker(ToolCall("2", "check_line", {"fen": "nonsense", "moves": ["e4"]})))["ok"] is False
        assert len(checker.lines) == 1 and checker.evals and len(checker.calls) == 2
        for _ in range(5):
            checker.check({"fen": board.fen(), "moves": ["Bc4"]})
        assert "No more engine checks" in checker.check({"fen": board.fen(), "moves": ["Bc4"]})["error"]
    assert CHECK_LINE.input_schema["required"] == ["fen", "moves"]


@needs_engine
def test_hard_notes_may_call_the_engine_and_the_checked_line_is_admitted(monkeypatch):
    """A model that checks a line not in the evidence may cite it; without the check it could not."""
    class Checker:
        concurrency = 1
        spec = type("S", (), {"local": True})()

        def __init__(self):
            self.tool_results = []

        def describe(self):
            return "Checker · test"

        def complete(self, system, messages, *, max_tokens=None, schema=None, tools=None, effort=None):
            if tools and not self.tool_results:
                fen = next(m for m in messages[0]["content"].split("\n") if m.startswith("Position before the move"))
                fen = fen.split(": ", 1)[1]
                call = ToolCall("t1", "check_line", {"fen": fen, "moves": ["a4", "bxa4"]})
                return Reply(text="", tool_calls=[call], assistant={"role": "assistant", "content": ""})
            note = {"explanation": CLAIM + " White's Bxb5+ keeps the attack going.", "line_ideas": []}
            return Reply(text=json.dumps(note))

        def tool_turn(self, reply, results):
            self.tool_results += [json.loads(out) for _, out in results]
            return [reply.assistant, {"role": "tool", "content": results[0][1], "tool_name": "check_line"}]

        def chat(self, system, messages, *, max_tokens=None):
            return self.complete(system, messages, max_tokens=max_tokens).text

    # A line no engine candidate contains (it just gives a pawn away): only an engine check makes it citable,
    # because ...bxa4 is possible in no position the coach was shown.
    CLAIM = "White could also have tried a4, but Black simply answers ...bxa4 and keeps the extra piece."
    cfg = fast_config(enabled=True)
    report = pipeline.analyse_game(SAMPLE_PGN, fast_config(), side_filter="white")
    game, _ = pipeline.parse_game(SAMPLE_PGN)
    played = pipeline.played_moves(game)
    # 11. Bxb5+ is a critical moment for White in the Opera game (the only move that keeps the attack going).
    idx = next(i for i, m in enumerate(report.moves) if m.san == "Bxb5+")
    ev = pipeline.evidence_for(report, idx, played, "white")
    assert ev.note == "full"
    ev.critical = True
    model = Checker()
    trace = {}
    explanation, _ideas, checks = pipeline._write_note(model, "sys", ev, "white", chess.WHITE, played, cfg, None,
                                                       None, trace)
    assert model.tool_results and model.tool_results[0]["ok"]
    verdict = model.tool_results[0]["evaluation_at_end"]
    assert checks == [{"line": "11. White a4, 11... Black bxa4", "evaluation": verdict, "legal": True}]
    assert verdict.startswith(("Black is", "White is", "the position"))
    assert explanation.startswith(CLAIM) and trace["evidence_problems"] == []   # the checked line is evidence now
    assert trace["corrected"] is False and trace["removed"] == 0
    # Without the engine check the same sentence is unsupported: it is sent back once, then removed.
    cfg.llm.verify_lines = False
    plain = Checker()
    plain.tool_results = [{"ok": True}]          # answer straight away, with the same claim
    trace = {}
    explanation, _ideas, checks = pipeline._write_note(plain, "sys", ev, "white", chess.WHITE, played, cfg, None,
                                                       None, trace)
    assert checks == [] and any("bxa4" in p for p in trace["evidence_problems"])
    assert "bxa4" not in explanation and "keeps the attack going" in explanation


# ------------------------------------------------------------------ players at the user's level

def test_player_database_lines_name_the_typical_mistake():
    assert OpeningExplorer.rating_bands(1450) == [1200, 1400, 1600]
    assert OpeningExplorer.rating_bands(900) == [0, 1000] and OpeningExplorer.rating_bands(2600) == [2200, 2500]
    assert OpeningExplorer.speeds("daily") == "correspondence" and OpeningExplorer.speeds("") == "blitz,rapid,classical"
    info = OpeningInfo(top_moves=[{"san": "Nf3", "games": 500, "white_pct": 35, "draw_pct": 5, "black_pct": 60},
                                  {"san": "d4", "games": 200, "white_pct": 55, "draw_pct": 6, "black_pct": 39},
                                  {"san": "h4", "games": 5, "white_pct": 50, "draw_pct": 0, "black_pct": 50}])
    out = OpeningExplorer.player_lines(info, 1450, "blitz", chess.WHITE)
    assert out[0].startswith("Players rated around 1450 (700 blitz games in the Lichess database) play here: Nf3 (71%")
    assert "h4" not in out[0]                                                # too few games to mean anything
    assert out[1] == ("At this level Nf3 is the most common choice but scores only 38% for White: the typical "
                      "mistake here; d4 scores 58%.")
    assert OpeningExplorer.player_lines(OpeningInfo(), 1450) == []
    assert len(OpeningExplorer.player_lines(info, 1450, "blitz")) == 1       # no side to move: no verdict


def test_player_database_lookups_are_cached_per_rating_and_speed(monkeypatch):
    explorer = OpeningExplorer(persistent=False)
    monkeypatch.setattr(OpeningExplorer, "_disabled_until", 0.0)
    calls = []

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"opening": {"name": "Italian Game", "eco": "C50"},
                    "moves": [{"san": "Bc5", "white": 40, "draws": 10, "black": 50}]}

    monkeypatch.setattr(explorer._session, "get", lambda url, params, timeout: calls.append((url, params)) or R())
    info = explorer.lookup_players(ITALIAN, 1450, "blitz")
    assert info.name == "Italian Game" and info.top_moves[0]["games"] == 100
    explorer.lookup_players(ITALIAN, 1450, "blitz")
    assert len(calls) == 1 and calls[0][1]["ratings"] == "1200,1400,1600" and calls[0][1]["speeds"] == "blitz"
    explorer.lookup_players(ITALIAN, 1450, "rapid")
    assert len(calls) == 2
    OpeningExplorer._disabled_until = float("inf")


# ------------------------------------------------------------------ the eval set

def test_eval_grading_is_automatic():
    case = evalset.Case(id="c-001", source="x.pgn", ply=17, side="Black", san="b5", cls="blunder", critical=False,
                       fen_before="", pgn="", expect={"best": "Qc7", "refutation": "Nxb5", "motifs": ["wins material"]})
    good = evalset.grade(case, "Black's b5 loses the b-pawn: after Nxb5 White wins material. Qc7 held everything.",
                         {"raw": '{"explanation": "...", "line_ideas": []}', "evidence_problems": [],
                          "assessment_problems": [], "missing": [], "corrected": False, "removed": 0}, 2.0, [])
    assert good.score == 100.0 and good.json_answer and good.mentions_best and good.mentions_refutation
    poor = evalset.grade(case, "A fine move by Black.",
                         {"raw": "A fine move", "evidence_problems": ["'Kxa1' is not a move"],
                          "assessment_problems": ["\"Black is winning\" contradicts"], "corrected": True,
                          "removed": 1}, 3.0, [])
    assert poor.score == 0.0 and not poor.grounded and not poor.assessment_ok and not poor.kept
    summary = evalset.summarise([good, poor])
    assert summary["score"] == 50.0 and summary["grounded"] == 50.0 and summary["best_move"] == 50.0
    no_motif = evalset.Case(id="c", source="", ply=0, side="White", san="e4", cls="mistake", critical=False,
                            fen_before="", pgn="", expect={"best": "", "refutation": "", "motifs": []})
    assert evalset.grade(no_motif, "Text.", {"raw": "Text."}, 1.0, []).mentions_motif is None


@needs_engine
def test_eval_cases_are_built_from_the_engine_and_run_with_a_fake_coach(tmp_path, monkeypatch):
    from test_pipeline import FakeLLM
    cfg = fast_config()
    pgn = tmp_path / "opera.pgn"
    pgn.write_text(SAMPLE_PGN, encoding="utf-8")
    cases = evalset.build_cases([str(pgn)], cfg)
    assert cases and all(c.cls in ("mistake", "blunder") or c.critical for c in cases)
    assert all(c.expect["best"] or c.critical for c in cases)
    path = tmp_path / "cases.jsonl"
    assert evalset.save_cases(cases, path) == len(cases) and evalset.save_cases(cases, path) == 0
    loaded = evalset.load_cases(path)
    assert [c.id for c in loaded] == [c.id for c in cases]

    monkeypatch.setattr(pipeline, "make_provider", lambda c: FakeLLM())
    cfg = fast_config(enabled=True)
    doc = evalset.run(loaded[:2], cfg)
    assert doc["summary"]["cases"] == 2 and doc["summary"]["errors"] == 0
    assert doc["cases"][0]["explanation"] == "A grounded note." and doc["cases"][0]["grounded"]
    assert doc["summary"]["json_answer"] == 0.0            # the fake answers in the old plain format
    out = evalset.save_results(doc, tmp_path / "results")
    assert json.loads(out.read_text())["summary"]["score"] is not None
    assert evalset.previous_result(doc, tmp_path / "results") is None     # the only run so far is this one


@pytest.mark.parametrize("text,san,expected", [
    ("Qc7 was better.", "Qc7", True), ("...Qc7 was better.", "Qc7", True), ("Qc7+ wins.", "Qc7", True),
    ("The queen to c7.", "Qc7", False), ("Nxb5! wins.", "Nxb5", True), ("Rd8# ends it.", "Rd8#", True),
])
def test_move_mentions(text, san, expected):
    assert evalset._mentions_move(text, san) is expected
