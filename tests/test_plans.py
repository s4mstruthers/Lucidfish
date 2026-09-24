import chess

from lucidfish import plans


def _played(sans: list[str], fen: str = chess.STARTING_FEN) -> tuple[list[plans.PlayedMove], chess.Board]:
    board = chess.Board(fen)
    out = []
    for ply, san in enumerate(sans):
        move = board.parse_san(san)
        out.append(plans.PlayedMove(ply, board.turn, san, move, board.copy(stack=False)))
        board.push(move)
    return out, board


def test_labelled_and_gists():
    played, _ = _played(["e4", "d5", "exd5", "Qxd5", "Nc3"])
    assert plans.labelled(played[:2]) == "1. White e4, 1... Black d5"
    text = plans.with_gists(played)
    assert "2. White exd5 [captures the pawn on d5]" in text
    assert "1... Black d5 [pawn break, attacks the pawn on e4]" in text
    assert "3. White Nc3 [knight to the queenside]" in text


def test_gist_castling_and_check():
    board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    assert plans.gist(board, board.parse_san("O-O")) == "castles kingside"
    board = chess.Board("4k3/8/8/8/8/8/8/R3K3 w - - 0 1")
    assert plans.gist(board, board.parse_san("Ra8+")) == "rook to the queenside, check"


def test_plan_lines_detect_wing_play_and_breaks():
    # White pushes the kingside pawns while Black has castled there.
    played, board = _played(["e4", "e5", "Nf3", "Nc6", "Bc4", "Nf6", "d3", "Be7", "h3", "O-O", "g4", "d6", "h4"])
    lines = plans.plan_lines(played, board)
    assert any(ln.startswith("White has advanced kingside pawns") and "g4" in ln and "h4" in ln for ln in lines)
    assert any("Pawn breaks available to Black" in ln and "...d5" in ln for ln in lines)


def test_pawn_breaks_only_list_real_breaks():
    board = chess.Board("4k3/8/8/1p6/8/P7/8/4K3 w - - 0 1")
    assert plans.pawn_breaks(board, chess.WHITE) == ["a4 (attacks the pawn on b5)"]
    assert plans.pawn_breaks(board, chess.BLACK) == ["...b4 (attacks the pawn on a3)"]


def test_open_files_and_opposite_castling():
    board = chess.Board("2kr3r/ppp5/8/8/8/8/5PPP/3R2K1 w - - 0 1")
    lines = plans.plan_lines([], board)
    assert any("opposite wings" in ln for ln in lines)
    files = next(ln for ln in lines if ln.startswith("Files:"))
    assert "the d-file is open (White's rook on d1, Black's rook on d8 on it)" in files
