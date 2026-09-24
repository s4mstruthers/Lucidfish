"""The post-game review keeps only what the game supports."""
from types import SimpleNamespace

import chess

from lucidfish.review_check import check_review, endgame_kind, game_facts

# The end of a real game (scrubb3rs vs i_hate_chess679): White had mate in one after 35...Kg7 but lost on time.
FINAL = "5r2/5bk1/p4p2/5Q1R/1p3P2/6PB/PPP4P/2K4R w - - 2 36"
MIDDLE = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"
ROOK_END = "8/5pk1/6p1/8/3R4/6P1/r4PK1/8 w - - 0 40"
PAWN_END = "8/5pk1/6p1/8/8/6P1/5PK1/8 w - - 0 45"


def _move(n, side, san, fen, cls="best", phase="middlegame", clock=None, think=None, win=50.0, loss=0.0,
          best="", mate=None):
    analysis = SimpleNamespace(win_loss=loss, win_before=60.0, win_after=60.0 - loss, eval_after_mate=mate)
    return SimpleNamespace(move_number=n, side=side, san=san, fen_after=fen, classification=cls, phase=phase,
                           clock_s=clock, think_s=think, win_white=win, best_san=best, analysis=analysis)


def _lost_on_time_game():
    moves = [_move(31, "White", "Qf5", MIDDLE, cls="inaccuracy", clock=40, loss=6, best="Qe4"),
             _move(31, "Black", "Kg7", MIDDLE, clock=100),
             _move(35, "White", "Qf5+", FINAL, phase="endgame", clock=1.5, win=90),
             _move(35, "Black", "Kg7", FINAL, phase="endgame", clock=80, win=97.5, mate=1)]
    headers = {"White": "scrubb3rs", "Black": "i_hate_chess679", "Result": "0-1",
               "Termination": "i_hate_chess679 won on time"}
    return moves, headers


SCREENSHOT_REVIEW = """## Summary
White outplayed Black in the Scandinavian Defense but lost on time.

## Key takeaways
- **Time management is crucial**: You made several critical mistakes under time pressure, which cost you the game.
- **Material trades are essential**: You often struggle with material trades, which can snowball into lost positions.
- **Pawn structure is vital**: You need to work on developing your pawn structure skills.
- **Review basic techniques**: You should review basic techniques like king and pawn versus king and pawn \
to improve your endgame play."""


def test_the_screenshot_review_loses_its_unsupported_takeaways():
    moves, headers = _lost_on_time_game()
    facts = game_facts(moves, headers, "White", "Scandinavian Defense: Mieses-Kotroc")
    text = "\n".join(facts.lines)
    assert "Black won on time" in text and "White had a forced mate" in text and "but lost on time" in text
    assert "an endgame with mixed pieces" in text                    # queens, rooks and bishops: not a pawn ending
    assert "White's lowest clock: 0:02 after 35. Qf5+" in text
    review, removed = check_review(SCREENSHOT_REVIEW, facts)
    # White lost on time, but made no errors with little time left: "mistakes under time pressure" is false.
    for gone in ("mistakes under time pressure", "king and pawn", "Material trades", "Pawn structure"):
        assert gone not in review
    assert len(removed) == 4
    assert review.startswith("## Summary\nWhite outplayed Black")
    # Nothing survived, so the takeaways come from the facts: the time forfeit, then the costliest error.
    takeaways = review.split("## Key takeaways\n")[1].splitlines()
    assert takeaways[0].startswith("- **Keep time to finish won games**: you lost on time after 35... Kg7")
    assert takeaways[1].startswith("- **31. Qf5** was an inaccuracy") and "Qe4 was better" in takeaways[1]


def test_a_missed_time_forfeit_is_added_to_good_takeaways():
    moves, headers = _lost_on_time_game()
    facts = game_facts(moves, headers, "White")
    review, _ = check_review("## Summary\nClose game.\n## Key takeaways\n- Move 31 (Qf5): Qe4 kept control.", facts)
    assert review.endswith("- Move 31 (Qf5): Qe4 kept control.\n" + facts.flag_lesson)


def test_endgame_claims_must_match_the_endgame_reached():
    moves = [_move(20, "White", "Rd1", MIDDLE, cls="mistake", loss=12, best="Rb1"),
             _move(40, "White", "Rd4", ROOK_END, phase="endgame")]
    facts = game_facts(moves, {"Result": "1/2-1/2"}, "White")
    assert facts.endgame_kinds == {"rook"} and not facts.time_support
    review = ("## Summary\nA long game. The king and pawn endgame was a draw. White held the rook ending.\n"
              "## Key takeaways\n- Move 20 (Rd1): the rook belonged on b1.\n"
              "- Rook endgame technique: activate the rook before pushing pawns.\n"
              "- You were in time trouble, so manage the clock better.\n"
              "- Study the opposition in king and pawn endings.")
    out, removed = check_review(review, facts)
    assert "A long game. White held the rook ending." in out          # only the false sentence went
    assert "Move 20 (Rd1)" in out and "Rook endgame technique" in out
    assert "time trouble" not in out and "opposition" not in out     # no clocks in the PGN; no pawn ending
    assert len(removed) == 3


def test_takeaways_are_rebuilt_from_facts_when_none_survive():
    moves = [_move(12, "White", "Nxe5", MIDDLE, cls="blunder", loss=30, best="d4"),
             _move(40, "White", "Kg2", PAWN_END, phase="endgame")]
    facts = game_facts(moves, {"Result": "0-1"}, "White")
    assert facts.endgame_kinds == {"pawn"}
    out, _ = check_review("## Summary\nOk.\n## Key takeaways\n- Practise trades.\n- Improve your pawn structure.",
                          facts)
    assert "Practise trades" not in out
    assert "**12. Nxe5** was a blunder" in out and "d4 was better" in out


def test_a_game_that_never_reached_an_endgame_gets_no_endgame_advice():
    moves = [_move(9, "Black", "Qxf2#", MIDDLE, cls="best")]
    facts = game_facts(moves, {"Result": "0-1"}, "Black")
    assert "never reached an endgame" in facts.lines[-2] and not facts.reached_endgame
    out, _ = check_review("## Summary\nBlack attacked.\n## Key takeaways\n- 9... Qxf2# shows the power of the "
                          "queen. Endgame technique would also help.", facts)
    assert out.endswith("- 9... Qxf2# shows the power of the queen.")  # only the endgame sentence goes
    out, _ = check_review("## Summary\nBlack attacked.\n## Key takeaways\n- Improve your endgame technique.", facts)
    assert out.endswith("- No mistakes or blunders from you in this game: the engine found only small "
                        "improvements. Keep playing this way.")      # nothing left: grounded takeaway instead


def test_endgame_kinds():
    assert endgame_kind(chess.Board(PAWN_END)) == "pawn"
    assert endgame_kind(chess.Board(ROOK_END)) == "rook"
    assert endgame_kind(chess.Board("8/5pk1/6p1/8/3B4/6P1/5PK1/3n4 w - - 0 45")) == "minor"
    assert endgame_kind(chess.Board(FINAL)) == "mixed"
