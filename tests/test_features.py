"""Smoke tests for the feature layer — run with: python -m pytest tests/ (or python tests/test_features.py)."""

import chess

from lucidfish import features


def test_start_position():
    f = features.extract(chess.Board())
    assert f.phase == "opening"
    assert f.white.material == f.black.material == 39
    assert f.white.developed_minors == 0
    assert f.white.can_castle and f.black.can_castle
    assert not f.white.hanging_pieces and not f.black.hanging_pieces


def test_hanging_piece_detected():
    # White queen on h5 attacked by g6 pawn... build a simple hang instead:
    # Black knight on e5 attacked by white pawn d4, undefended.
    board = chess.Board("4k3/8/8/4n3/3P4/8/8/4K3 w - - 0 1")
    f = features.extract(board)
    assert "Ne5" in f.black.hanging_pieces


def test_doubled_isolated_passed():
    # White: doubled c-pawns (isolated), and a passed h-pawn.
    board = chess.Board("4k3/8/8/8/2P4P/2P5/8/4K3 w - - 0 1")
    f = features.extract(board)
    assert f.white.doubled_pawn_files == ["c"]
    assert set(f.white.isolated_pawns) == {"c3", "c4", "h4"}
    assert set(f.white.passed_pawns) == {"c3", "c4", "h4"}


def test_castled_detection():
    board = chess.Board()
    for san in ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "O-O"]:
        board.push_san(san)
    f = features.extract(board)
    assert f.white.castled
    assert not f.black.castled and f.black.can_castle


def test_summary_lines_readable():
    lines = features.extract(chess.Board()).summary_lines()
    assert any("Material: equal" in l for l in lines)
    assert any("Development" in l for l in lines)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all feature tests passed")
