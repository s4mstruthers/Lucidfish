import chess

from lucidfish import tactics


def lines(fen: str) -> list[str]:
    return [text for _, text in tactics.position_lines(chess.Board(fen))]


def test_knight_fork_of_king_and_rook():
    facts = lines("r3k3/2N5/8/8/8/8/8/4K3 b - - 0 1")
    assert any("forks" in f and "rook on a8" in f and "king on e8" in f for f in facts)


def test_absolute_and_relative_pins():
    assert any("pinned to its king" in f for f in lines("4k3/3n4/8/1B6/8/8/8/4K3 b - - 0 1"))
    rel = lines("4k3/3q4/2n5/1B6/8/8/8/4K3 b - - 0 1")
    assert any("pinned by White's bishop on b5" in f and "queen on d7" in f for f in rel)


def test_static_exchange_evaluation():
    # Knight attacked by a pawn is lost even though it is defended.
    board = chess.Board("4k3/8/3p4/4N3/3P4/8/8/4K3 b - - 0 1")
    threats = tactics.analyse(board).en_prise[chess.WHITE]
    assert [chess.square_name(t.square) for t in threats] == ["e5"]
    assert threats[0].gain == 200            # knight (300) for pawn (100)
    # A defended pawn attacked once by a pawn is not en prise (even trade).
    board = chess.Board("4k3/8/3p4/4P3/5P2/8/8/4K3 b - - 0 1")
    assert not tactics.analyse(board).en_prise[chess.WHITE]


def test_pinned_attacker_cannot_capture():
    # The black knight on d7 attacks e5, but is pinned to its king by Bb5.
    board = chess.Board("4k3/3n4/8/1B2P3/8/8/8/4K3 b - - 0 1")
    assert not tactics.analyse(board).en_prise[chess.WHITE]


def test_mate_threat_detection():
    assert any("threatens checkmate with Rd8#" in f for f in lines("6k1/5ppp/8/8/8/8/5PPP/3R2K1 b - - 0 1"))
    assert any("can deliver checkmate now with Rd8#" in f for f in lines("6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1"))


def test_move_motifs():
    board = chess.Board("r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4")
    assert tactics.move_motifs(board, board.parse_san("Qxf7#")) == ["delivers checkmate"]
    board = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/8/PPPP1PPP/RNBQK1NR w KQkq - 2 3")
    motifs = tactics.move_motifs(board, board.parse_san("Qh5"))
    assert any(m.startswith("threatens checkmate with Qxf7#") for m in motifs)
    board = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 3 3")
    assert any("stops White's threat of checkmate" in m for m in tactics.move_motifs(board, board.parse_san("g6")))
    board = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3")
    assert tactics.move_motifs(board, board.parse_san("Nxe5"))[0].startswith("loses material")


def test_tags_from_motifs():
    assert tactics.tags_from_motifs(["delivers checkmate"]) == ["checkmate"]
    assert tactics.tags_from_motifs(["creates a fork: x", "threatens checkmate with Qh7#"]) == ["fork", "mate threat"]
