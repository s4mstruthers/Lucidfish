"""Move classification from synthetic engine lines (no engine needed), plus an
integration test against a real Stockfish when one is installed."""

import chess

from conftest import fast_config, needs_engine
from lucidfish.config import AnalysisConfig
from lucidfish.engine import EngineAnalyzer, Line, build_move_analysis, describe_move, numbered_line

TH = AnalysisConfig()


def line(board: chess.Board, san: str, cp=None, mate=None) -> Line:
    mv = board.parse_san(san)
    return Line(move_san=san, move_uci=mv.uci(), score_cp=cp, mate_in=mate, pv_san=[san], pv_uci=[mv.uci()])


def analyse(fen, played, best, best_cp=None, best_mate=None, reply=None, reply_cp=None, reply_mate=None):
    board = chess.Board(fen)
    after = board.copy()
    after.push_san(played)
    cands = [line(board, best, best_cp, best_mate)]
    lines_after = [line(after, reply, reply_cp, reply_mate)] if reply else []
    return build_move_analysis(board, board.parse_san(played), cands, lines_after, TH)


BACK_RANK = "6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1"


def test_missed_mate_is_not_described_as_allowing_mate():
    a = analyse(BACK_RANK, "h3", "Rd8#", best_mate=1, reply="h6", reply_cp=-540)
    assert a.mate_event == "missed_mate"
    assert a.classification == "blunder"      # Lichess: missed mate and below +7
    a = analyse(BACK_RANK, "h3", "Rd8#", best_mate=1, reply="h6", reply_cp=-1200)
    assert a.classification == "inaccuracy"   # still completely winning


def test_delivered_mate_is_best():
    a = analyse(BACK_RANK, "Rd8#", "Rd8#", best_mate=1)
    assert a.classification == "best" and a.mate_event == "delivered_mate" and a.game_result == "1-0"


def test_allowed_mate():
    fen = "6k1/5ppp/8/8/8/8/5PPP/3R2K1 b - - 0 1"
    a = analyse(fen, "Kf8", "h6", best_cp=-450, reply="Rd8#", reply_mate=1)
    assert a.mate_event == "allowed_mate"
    assert a.classification == "blunder"


def test_win_probability_thresholds():
    start = chess.STARTING_FEN
    # Equal position, move drops two pawns: blunder.
    assert analyse(start, "a3", "e4", best_cp=30, reply="e5", reply_cp=170).classification == "blunder"
    # Already completely winning: losing two pawns of a +9 advantage is only an inaccuracy or less.
    assert analyse(start, "a3", "e4", best_cp=900, reply="e5", reply_cp=-700).classification in ("good", "inaccuracy")


def test_engine_top_move_with_small_noise_is_best():
    a = analyse(chess.STARTING_FEN, "e4", "e4", best_cp=35, reply="e5", reply_cp=-20)
    assert a.classification == "best" and a.cp_loss == 0 and a.accuracy == 100 and not a.suspicious


def test_engine_top_move_that_collapses_is_flagged_for_verification():
    a = analyse(chess.STARTING_FEN, "e4", "e4", best_cp=30, reply="e5", reply_cp=400)
    assert a.suspicious


def test_describe_move_and_numbering():
    board = chess.Board()
    assert describe_move(board, board.parse_san("Nf3")) == "knight from g1 to f3, no capture"
    assert numbered_line(board, ["e4", "e5", "Nf3"]) == "1. e4 e5 2. Nf3"
    board.push_san("e4")
    assert numbered_line(board, ["e5", "Nf3"]) == "1... e5 2. Nf3"


class DictCache:
    def __init__(self):
        self.data = {}

    def get(self, key, strength, multipv):
        hit = self.data.get(key)
        return hit[2] if hit and hit[0] >= strength and hit[1] >= multipv else None

    def put(self, key, strength, multipv, data):
        self.data[key] = (strength, multipv, data)


@needs_engine
def test_engine_lines_and_cache():
    cfg = fast_config()
    cache = DictCache()
    board = chess.Board(BACK_RANK)
    with EngineAnalyzer(cfg.engine, cache=cache) as engine:
        lines = engine.top_lines(board)
        assert lines[0].move_san == "Rd8#" and lines[0].mate_in == 1
        assert len(cache.data) == 1
        again = engine.top_lines(board)            # served from the cache
        assert [ln.move_san for ln in again] == [ln.move_san for ln in lines]
        # A forced reply gets a single line.
        forced = chess.Board("7k/8/8/8/8/8/8/R5RK b - - 0 1")
        assert len(engine.top_lines(forced)) == forced.legal_moves.count()
