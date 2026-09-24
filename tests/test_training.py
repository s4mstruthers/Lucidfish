"""Puzzles from your own mistakes: how important each one is, and what it's about."""
import chess

from lucidfish import store, training
from lucidfish.training import importance


def test_importance_levels():
    assert importance({"cls": "blunder"}, 80, 45) == (3, "It threw away a winning position.")
    assert importance({"cls": "mistake"}, 50, 22) == (3, "It turned a playable game into a losing one.")
    assert importance({"cls": "blunder", "mate_event": "allowed_mate"}, 55, 2)[0] == 3
    assert importance({"cls": "inaccuracy", "mate_event": "missed_mate"}, 99, 95)[0] == 2   # still winning
    assert importance({"cls": "mistake"}, 60, 48) == (2, "It cost 12% of your winning chances.")
    assert importance({"cls": "blunder"}, 99, 88) == (1, "You were still clearly winning afterwards.")
    assert importance({"cls": "blunder"}, 8, 1) == (1, "The game was already lost.")
    assert importance({"cls": "inaccuracy"}, 55, 49) == (1, "A small inaccuracy.")


def test_puzzles_know_their_time_control_theme_and_clock():
    store.init()
    pid = store.create_profile(name="Sam")
    fen = chess.STARTING_FEN
    moves = [
        {"ply": 0, "n": 1, "side": "White", "san": "f3", "uci": "f2f3", "cls": "blunder", "cp_loss": 300,
         "fen_before": fen, "fen_after": fen, "win": 20.0, "best": "e4", "best_uci": "e2e4", "phase": "opening",
         "threat": "Qh4 (wins)", "tags": ["hangs material"], "clock_s": 170.0,
         "candidates": [{"san": "e4", "uci": "e2e4", "cp": 30}]},
        {"ply": 2, "n": 2, "side": "White", "san": "g4", "uci": "g2g4", "cls": "mistake", "cp_loss": 150,
         "fen_before": fen, "fen_after": fen, "win": 30.0, "best": "d4", "best_uci": "d2d4", "phase": "opening",
         "clock_s": 12.0, "candidates": [{"san": "d4", "uci": "d2d4", "cp": 0}]},
    ]
    pgn = '[Site "Chess.com"]\n[TimeControl "180"]\n\n1. f3 e5 2. g4 *\n'
    store.save_game(pid, pgn, {"Site": "Chess.com", "TimeControl": "180"}, "white", None, "", "", moves, "blitz")
    first, second = training.train_items(pid)
    assert first["time_class"] == "blitz" and not first["hurried"] and second["hurried"]   # 12 s left of 3 min
    assert first["importance"] == 3 and first["why"] == "It turned a playable game into a losing one."
    assert first["themes"] == ["missed_threat", "hanging"] and first["phase"] == "opening"
