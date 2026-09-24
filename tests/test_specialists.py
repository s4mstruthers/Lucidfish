"""The code 'specialists': missed threats, endgame facts, leaving the book, chapters, cross-game patterns."""
import chess
import pytest

from conftest import SAMPLE_PGN, fast_config, needs_engine
from lucidfish import endgame, features, insights, pipeline, tactics
from lucidfish.opening import OpeningExplorer, OpeningInfo

# ------------------------------------------------------------------ missed threats


def test_existing_threat_separates_ignored_threats_from_new_problems():
    board = chess.Board()
    for san in "e4 e5 Bc4 Nc6 Qh5".split():
        board.push_san(san)
    after = board.copy()
    after.push_san("Nf6")                               # ignores the threat of Qxf7#
    assert tactics.existing_threat(board, after.parse_san("Qxf7#")).startswith("Qxf7# (delivers checkmate")
    quiet = chess.Board()
    for san in "e4 e5 Nf3 Nc6".split():
        quiet.push_san(san)
    blunder = quiet.copy()
    blunder.push_san("Nd4")                             # hands White the e5 pawn? no: the knight itself is new
    reply = blunder.parse_san("Nxd4")
    assert tactics.existing_threat(quiet, reply) == ""  # Nxd4 wasn't possible before Nd4: the move created it


@needs_engine
def test_analysis_flags_a_missed_threat():
    pgn = '[White "A"]\n[Black "B"]\n[Result "1-0"]\n\n1. e4 e5 2. Bc4 Nc6 3. Qh5 Nf6 4. Qxf7# 1-0\n'
    report = pipeline.analyze_game(pgn, fast_config(), side_filter="black")
    nf6 = report.moves[5]
    assert nf6.san == "Nf6" and nf6.classification == "blunder"
    assert nf6.threat.startswith("Qxf7#") and "missed threat" in nf6.tags
    assert nf6.to_dict()["threat"] == nf6.threat
    assert all(not m.threat for m in report.moves if m is not nf6)


# ------------------------------------------------------------------ endgame


@pytest.mark.parametrize("fen, can_catch", [
    ("8/8/8/8/P3k3/8/8/6K1 b - - 0 1", True),     # defender to move: inside the square
    ("8/8/8/8/P3k3/8/8/6K1 w - - 0 1", False),    # pawn to move: one tempo too slow
    ("8/8/8/P3k3/8/8/8/6K1 b - - 0 1", False),
    ("4k3/8/8/8/8/8/P7/6K1 b - - 0 1", True),     # double step from the starting rank counts
])
def test_rule_of_the_square(fen, can_catch):
    line = next(t for k, t in endgame.endgame_lines(chess.Board(fen)) if k.startswith("eg:passed"))
    assert ("can catch it" in line) == can_catch


def test_endgame_facts_appear_only_in_the_endgame():
    eg = features.extract(chess.Board("8/8/4k3/8/4K3/4P3/8/8 w - - 0 1"))
    keys = dict(eg.keyed_lines())
    assert "White's king on e4 is fully centralised." == keys["eg:king:White"]
    assert "Black has the opposition" in keys["eg:opposition"]
    assert "protected" not in keys["eg:passed:e3"]
    assert not features.extract(chess.Board()).endgame


# ------------------------------------------------------------------ leaving the book


class MastersBook(OpeningExplorer):
    """Pretends masters played the game's own moves, except 3...Bg4 (they prefer exd4 and Nf6)."""

    def __init__(self, pgn):
        super().__init__()
        self.stats = {}
        game = pipeline.parse_game(pgn)[0]
        board = game.board()
        for ply, move in enumerate(game.mainline_moves()):
            san = board.san(move)
            stat = {"white_pct": 40, "draw_pct": 30, "black_pct": 30}
            top = [{"san": "exd4", "games": 5000, **stat}, {"san": "Nf6", "games": 2100, **stat}] if ply == 5 else \
                [{"san": san, "games": 900, **stat}]
            self.stats[board.fen()] = top
            board.push(move)

    def lookup(self, fen):
        return OpeningInfo(name="Philidor Defense", eco="C41", top_moves=self.stats.get(fen, []))


@needs_engine
def test_the_move_that_leaves_theory_is_marked_with_what_masters_play():
    report = pipeline.analyze_game(SAMPLE_PGN, fast_config(), side_filter="white", explorer=MastersBook(SAMPLE_PGN))
    left = [m for m in report.moves if m.left_book]
    assert [m.san for m in left] == ["Bg4"]
    assert "masters usually play exd4 (5000 games), Nf6 (2100 games)" in left[0].left_book
    assert "left book" in left[0].tags and all(m.book for m in report.moves[:5])


# ------------------------------------------------------------------ chapters


def _moves(wins, phases=None):
    out = []
    for i, w in enumerate(wins):
        out.append(pipeline.AnnotatedMove(
            ply=i, move_number=i // 2 + 1, side="White" if i % 2 == 0 else "Black", san="a3", uci="a2a3",
            classification="best", cp_loss=0, best_san="a3", eval_str="0.00", win_white=w, accuracy=100,
            fen_before=chess.STARTING_FEN, fen_after=chess.STARTING_FEN,
            phase=(phases[i] if phases else "middlegame")))
    return out


def test_split_chapters_cuts_at_phases_and_turning_points():
    assert pipeline.split_chapters(_moves([50] * 10)) == [(0, 9)]            # too short to split
    phases = ["opening"] * 12 + ["middlegame"] * 20 + ["endgame"] * 12
    wins = [50] * 20 + [85] * 24                                              # White's winning jump at 20
    spans = pipeline.split_chapters(_moves(wins, phases))
    starts = [s for s, _ in spans]
    assert starts == [0, 12, 21, 32]            # phase change, after the turning point, endgame
    assert spans[-1][1] == 43 and all(e - s + 1 >= pipeline.MIN_CHAPTER for s, e in spans)
    many = pipeline.split_chapters(_moves([50 if (i // 7) % 2 else 90 for i in range(100)]))
    assert len(many) <= pipeline.MAX_CHAPTERS


# ------------------------------------------------------------------ cross-game patterns


def _game(result, side="white", max_win=60, min_win=40, castle=8, plies=60, tc="blitz", exit_delta=None,
          threats=0):
    facts = {"side": side, "result": result, "time_class": tc, "plies": plies, "castle_move": castle,
             "max_win": max_win, "min_win": min_win, "missed_threats": threats}
    if exit_delta is not None:
        facts["book_exit"] = {"move": 6, "by_user": True, "delta": exit_delta}
    return {"facts": facts}


def test_findings_need_enough_games_and_rank_weaknesses_first():
    assert insights.findings([_game("W"), _game("L")]) == []                  # too few games
    games = ([_game("L", max_win=90, castle=None, exit_delta=-12, threats=1) for _ in range(3)]
             + [_game("W", max_win=85, castle=6, exit_delta=-6) for _ in range(3)]
             + [_game("W", side="black", castle=7) for _ in range(3)])
    found = {f["id"]: f for f in insights.findings(games)}
    assert found["conversion"]["kind"] == "weakness" and "won 3 of the 6 games" in found["conversion"]["text"]
    assert found["castling"]["text"].startswith("You score 100% when you castle by move 10 and 0%")
    assert found["after_book"]["kind"] == "weakness" and "-9 points" in found["after_book"]["text"]
    assert found["threats"]["kind"] == "weakness"
    kinds = [f["kind"] for f in insights.findings(games)]
    assert kinds == sorted(kinds, key={"weakness": 0, "strength": 1, "info": 2}.get)


def test_game_facts_from_moves():
    moves = [{"side": "White", "n": 1, "san": "e4", "win": 55}, {"side": "Black", "n": 1, "san": "e5", "win": 50,
             "left_book": "left known opening theory"}, {"side": "White", "n": 2, "san": "O-O", "win": 30},
             {"side": "Black", "n": 2, "san": "Qh4", "win": 10, "threat": "Qxf2#"}]
    facts = insights.game_facts(moves, "black", "0-1", "rapid")
    assert facts["result"] == "W" and facts["castle_move"] is None and facts["missed_threats"] == 1
    assert facts["max_win"] == 90 and facts["min_win"] == 45
    # Black's winning chances: 45% before leaving the book, 90% at the end (fewer than 10 plies later).
    assert facts["book_exit"] == {"move": 1, "by_user": True, "delta": 45.0}
