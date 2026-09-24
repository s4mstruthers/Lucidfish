from lucidfish import scoring


def test_win_percent_is_symmetric_and_bounded():
    assert scoring.win_percent(0) == 50
    assert abs(scoring.win_percent(300) + scoring.win_percent(-300) - 100) < 1e-9
    assert scoring.win_percent(None, 3) == scoring.win_percent(scoring.MATE_CP)
    assert scoring.win_percent(10_000) == scoring.win_percent(scoring.MATE_CP)  # clamped


def test_win_percent_matches_lichess_scale():
    # A two-pawn advantage is roughly 67-68% winning chances on Lichess.
    assert 67 < scoring.win_percent(200) < 68


def test_move_accuracy():
    assert scoring.move_accuracy(60, 60) == 100
    assert scoring.move_accuracy(60, 70) == 100          # improving never costs accuracy
    assert 0 <= scoring.move_accuracy(95, 5) < 5
    assert scoring.move_accuracy(55, 50) > scoring.move_accuracy(55, 40)


def test_game_accuracy_perfect_and_blundered_games():
    flat = [50.0] * 21
    acc = scoring.game_accuracy(flat)
    assert acc["white"] == acc["black"] == 100
    # Black blunders on move 10 (White's chances jump from 50% to 90%).
    swing = [50.0] * 10 + [90.0] * 11
    acc = scoring.game_accuracy(swing)
    assert acc["white"] > acc["black"]
    assert scoring.game_accuracy([50.0]) == {"white": None, "black": None}


def test_eval_text_and_words():
    assert scoring.eval_text(125, None) == "+1.25"
    assert scoring.eval_text(None, -2) == "#-2"
    assert scoring.describe_eval(0, None) == "the position is equal"
    assert "White" in scoring.describe_eval(400, None) and "winning" in scoring.describe_eval(400, None)
    assert scoring.describe_eval(None, -3) == "Black has a forced checkmate (mate in 3)"
