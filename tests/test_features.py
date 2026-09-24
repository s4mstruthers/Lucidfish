import chess

from lucidfish import features


def test_start_position():
    f = features.extract(chess.Board())
    assert f.phase == "opening"
    assert f.white.material == f.black.material == 39
    assert f.white.developed_minors == 0 and f.white.minors == 4
    assert f.white.can_castle and f.black.can_castle
    assert f.white.king_pawn_shield == 3
    assert not f.white.hanging_pieces and not f.black.hanging_pieces


def test_hanging_piece_detected():
    # Black knight on e5 attacked by the d4 pawn and undefended.
    f = features.extract(chess.Board("4k3/8/8/4n3/3P4/8/8/4K3 w - - 0 1"))
    assert "Ne5" in f.black.hanging_pieces


def test_captured_pieces_are_not_counted_as_developed():
    # White has only one minor piece left and it is still at home.
    f = features.extract(chess.Board("4k3/8/8/8/8/8/8/1N2K3 w - - 0 1"))
    assert f.white.minors == 1 and f.white.developed_minors == 0


def test_doubled_isolated_passed():
    f = features.extract(chess.Board("4k3/8/8/8/2P4P/2P5/8/4K3 w - - 0 1"))
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


def test_diff_reports_what_changed():
    board = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3")
    after = board.copy()
    after.push_san("Nxe5")
    changed = features.diff_lines(features.extract(board), features.extract(after))
    assert any("Material: White is up 1 point" in line for line in changed)
    assert any("knight on e5 can be won" in line for line in changed)


def test_piece_placement_is_readable():
    text = features.piece_placement(chess.Board())
    assert text.startswith("White: Ke1, Qd1, Ra1, Rh1")
    assert "Black:" in text and "pawns a7 b7" in text


def test_summary_lines_readable():
    lines = features.extract(chess.Board()).summary_lines()
    assert any("Material: equal" in line for line in lines)
    assert any("Development" in line for line in lines)
