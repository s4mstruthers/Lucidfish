"""The post-game review keeps only what the game supports."""
from types import SimpleNamespace

import chess

from lucidfish.review_check import check_review, endgame_kind, game_facts, problems

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


# ---------------------------------------------------------------- a won game reviewed as a loss (reported)

MATED = "6k1/5ppp/8/8/8/8/5PPP/1r4K1 w - - 0 53"   # Black's rook mates on the back rank


def _won_as_black():
    """mushygo_1 (Black) beat MCprinceseville with checkmate; White blundered on move 6 (Ne4)."""
    moves = [_move(1, "White", "d4", MIDDLE), _move(1, "Black", "e5", MIDDLE, cls="inaccuracy", loss=6, best="Nf6"),
             _move(2, "White", "c4", MIDDLE, cls="good"), _move(2, "Black", "exd4", MIDDLE),
             _move(6, "White", "Ne4", MIDDLE, cls="blunder", loss=30, best="Nf3"),
             _move(6, "Black", "Bb4+", MIDDLE, cls="mistake", loss=11, best="O-O"),
             _move(8, "White", "Bd2", MIDDLE, cls="inaccuracy", loss=5),
             _move(8, "Black", "Nxd2+", MIDDLE, win=10.0),
             _move(52, "White", "Kh1", MATED, phase="endgame", win=3.0),
             _move(52, "Black", "Rb1#", MATED, phase="endgame", win=0.0)]
    for m in moves[:6]:
        m.win_white = 50.0
    moves[5].critical = False
    headers = {"White": "MCprinceseville", "Black": "mushygo_1", "Result": "0-1",
               "Termination": "mushygo_1 won by checkmate"}
    return game_facts(moves, headers, "Black", "Queen's Pawn Opening", accuracy={"black": 94.2})


WON_REVIEW = """## Summary
In this game, you employed the Queen's Pawn Opening against White, but unfortunately, you made several mistakes that \
cost you the game. The turning point came on move 8, when you captured on d2.

## How the game unfolded
- Moves 1-2: You responded to White's d4 with e5, which was a mistake. White's c4 was also a mistake, and exd4 was \
the right reply.
- Moves 22-52: You created a strong passed pawn and your rook delivered checkmate.

## Key takeaways
- Move 6 (Ne4): Be more careful with your pieces and avoid blunders that can cost you a significant advantage.
- Move 8 (Nxd2+): Take advantage of White's mistakes and seize opportunities to gain material.
- Move 6 (Bb4+): Be more aware of the threats on the board and avoid unnecessary checks."""


def test_a_won_game_is_not_reviewed_as_a_loss():
    facts = _won_as_black()
    assert facts.lines[0].startswith("RESULT: Black, the player you are coaching, WON this game by checkmate")
    found = "\n".join(problems(WON_REVIEW, facts))
    assert "cost you the game" in found and "Black won this game (you are Black, so you won)" in found
    assert "c4 (White, move 2) was rated 'good' by the engine, not a mistake" in found     # e5 was an inaccuracy: fine
    assert "Move 6 (Ne4) was White's move, not yours" in found
    review, _ = check_review(WON_REVIEW, facts)
    assert "cost you the game" not in review and "The turning point came on move 8" in review
    assert "You responded to White's d4 with e5, which was a mistake." in review and "c4 was also" not in review
    assert "Move 6 (Ne4)" not in review and "Move 6 (Bb4+)" in review and "Move 8 (Nxd2+)" in review


def test_what_went_well_comes_from_verified_highlights():
    facts = _won_as_black()
    assert "8... Nxd2+ punished White's blunder" not in " ".join(facts.highlights)     # Ne4 wasn't the move before
    assert any("winning from 8... Nxd2+ on and converted it into a win by checkmate" in h for h in facts.highlights)
    assert any("accuracy was 94.2%" in h for h in facts.highlights)
    review, _ = check_review(WON_REVIEW, facts)
    went_well = review.split("## What went well\n")[1].split("\n\n")[0].splitlines()
    assert 1 <= len(went_well) <= 2 and review.index("## What went well") < review.index("## Key takeaways")
    # The model's own points are kept when grounded; flattery and extras are not.
    review, _ = check_review("## Summary\nWon.\n## What went well\n- Great game, well done!\n"
                             "- 8... Nxd2+ won material after White's inaccuracy.\n- Your accuracy was excellent.\n"
                             "- Move 52 (Rb1#) finished it.\n## Key takeaways\n- Move 6 (Bb4+): check threats.", facts)
    section = review.split("## What went well\n")[1].split("## Key takeaways")[0]
    assert "Great game" not in section and section.count("\n- ") + section.startswith("- ") == 2


def test_punished_errors_and_critical_finds_are_highlights():
    moves = [_move(10, "White", "Qd2", MIDDLE, cls="blunder", loss=25),
             _move(10, "Black", "Nf3+", MIDDLE, cls="best", win=20.0),
             _move(20, "White", "Kh1", MIDDLE),
             _move(20, "Black", "Rxe1", MIDDLE, cls="best", win=15.0)]
    moves[3].critical = True
    facts = game_facts(moves, {"Result": "*"}, "Black")
    assert facts.highlights[0] == "20... Rxe1: Black found the only good move at a critical moment."
    assert "10... Nf3+ punished White's blunder 10. Qd2 straight away." in facts.highlights


def test_the_post_game_review_keeps_its_fixed_order():
    from lucidfish.review_check import order_game_review
    messy = ("A sharp game.\n**Key takeaways:**\n- Move 9 (Qxb7): the queen was trapped.\n"
             "### What went well\n- 4... Nf6 developed with tempo.\n# Story of the game\n- Moves 1-8: quiet opening.")
    assert order_game_review(messy) == (
        "## Summary\nA sharp game.\n\n## How the game unfolded\n- Moves 1-8: quiet opening.\n\n"
        "## What went well\n- 4... Nf6 developed with tempo.\n\n"
        "## Key takeaways\n- Move 9 (Qxb7): the queen was trapped.")
