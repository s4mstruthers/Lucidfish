import io
import json
from unittest import mock

import chess.pgn
import pytest

from lucidfish import fetch
from lucidfish.export import annotated_pgn, markdown_report

PGN = '[White "A"]\n[Black "B"]\n[Result "*"]\n\n1. e4 e5 2. Qh5 Nc6 *'


def _moves():
    return [
        {"ply": 0, "n": 1, "side": "White", "san": "e4", "uci": "e2e4", "cls": "best", "eval": "+0.30", "expl": ""},
        {"ply": 1, "n": 1, "side": "Black", "san": "e5", "uci": "e7e5", "cls": "best", "eval": "+0.35", "expl": ""},
        {"ply": 2, "n": 2, "side": "White", "san": "Qh5", "uci": "d1h5", "cls": "inaccuracy", "eval": "+0.10",
         "best": "Nf3", "expl": "Early queen sortie.",
         "candidates": [{"san": "Nf3", "idea": "Develops.", "steps": [{"san": "Nf3"}, {"san": "Nc6"}]}]},
        {"ply": 3, "n": 2, "side": "Black", "san": "Nc6", "uci": "b8c6", "cls": "best", "eval": "#-2", "expl": ""},
    ]


def test_annotated_pgn_round_trips():
    text = annotated_pgn(PGN, _moves(), review="Nice game.", accuracy={"white": 90.0, "black": 95.5})
    game = chess.pgn.read_game(io.StringIO(text))
    assert game.headers["Annotator"].startswith("Lucidfish")
    assert game.headers["BlackAccuracy"] == "95.5"
    qh5 = list(game.mainline())[2]
    assert chess.pgn.NAG_DUBIOUS_MOVE in qh5.nags
    assert "[%eval 0.10]" in text and "Early queen sortie." in text
    assert "( 2. Nf3" in text                          # best line added as a variation
    assert "[%eval #-2]" in text


def test_markdown_report_lists_key_moments():
    text = markdown_report({"White": "A", "Black": "B", "Result": "*"}, _moves(), "Review.", "Opening X",
                           {"white": 90.0, "black": 95.0}, coached_side="white")
    assert "## Key moments" in text and "2. Qh5?!" in text
    assert "Opening X" in text and "## Post-game review" in text


def test_lichess_ndjson_parsing_filters_variants():
    games = [{"variant": "standard", "pgn": "1. e4 *"}, {"variant": "crazyhouse", "pgn": "1. e4 *"},
             {"variant": "chess960", "pgn": "1. g3 *"}]
    resp = mock.Mock(text="\n".join(json.dumps(g) for g in games))
    with mock.patch.object(fetch, "_get", return_value=resp):
        assert fetch.lichess_recent_pgns("someone", n=5) == ["1. e4 *", "1. g3 *"]


def test_chesscom_skips_variants_and_keeps_order():
    archives = mock.Mock()
    archives.json.return_value = {"archives": ["m1", "m2"]}
    month = mock.Mock()
    month.json.return_value = {"games": [{"pgn": "old", "rules": "chess"}, {"pgn": "bughouse", "rules": "bughouse"},
                                         {"pgn": "new", "rules": "chess"}]}
    with mock.patch.object(fetch, "_get", side_effect=[archives, month]):
        assert fetch.chesscom_recent_pgns("someone", n=2) == ["new", "old"]


def test_fetch_rejects_bad_usernames_and_does_not_retry_404():
    with pytest.raises(fetch.FetchError):
        fetch.chesscom_recent_pgns("bad name!")
    with mock.patch("requests.get", return_value=mock.Mock(status_code=404)) as get:
        with pytest.raises(fetch.FetchError, match="not found"):
            fetch._get("https://example.invalid")
    assert get.call_count == 1


def test_drawings_are_exported_as_pgn_arrows_and_circles():
    from lucidfish.export import annotated_pgn
    pgn = '[White "A"]\n[Black "B"]\n\n1. e4 e5 *\n'
    after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR"
    moves = [{"ply": 0, "uci": "e2e4", "cls": "best", "eval": "+0.3",
              "fen_after": after_e4 + " b KQkq - 0 1"}]
    out = annotated_pgn(pgn, moves, annotations={after_e4: {
        "arrows": [{"from": "g1", "to": "f3", "color": "green"}, {"from": "d7", "to": "d5", "color": "red"}],
        "circles": [{"sq": "e4", "color": "yellow"}]}})
    assert "[%cal Gg1f3,Rd7d5]" in out and "[%csl Ye4]" in out
