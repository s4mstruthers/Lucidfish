import chess

from lucidfish import features
from lucidfish.config import AnalysisConfig
from lucidfish.engine import Line, build_move_analysis
from lucidfish.prompts import (
    CommentaryContext,
    CommentaryMove,
    EvidenceIndex,
    build_commentary_prompt,
    build_move_prompt,
    build_system_prompt,
    norm_san,
    parse_commentary,
    side_line,
    split_sections,
)


def test_split_sections_variants():
    text = "EXPLANATION:\nGood move.\nLINE IDEAS:\nNf3: develops.\n- **Bc4**: eyes f7."
    expl, ideas = split_sections(text)
    assert expl == "Good move."
    assert ideas == {"Nf3": "develops.", "Bc4": "eyes f7."}
    expl, ideas = split_sections("**Explanation:** Solid.")
    assert expl == "Solid." and ideas == {}
    assert split_sections("Just prose, no headers.") == ("Just prose, no headers.", {})


def test_norm_san():
    assert norm_san("Nbxd7+") == "Nd7"
    assert norm_san("exd5") == "exd5"          # a pawn capture keeps its file ...
    assert norm_san("bxa5") != norm_san("a5")  # ... so it is never confused with a push
    assert norm_san("O-O-O#") == "O-O-O"
    assert norm_san("e8=Q+") == "e8Q"


def test_evidence_index_flags_invented_moves_and_pieces():
    board = chess.Board()
    move = board.parse_san("e4")
    index = EvidenceIndex.for_move(board, move, [(board, ["e4", "e5", "Nf3", "Nc6"])])
    assert index.problems("After Nf3 the knight on f3 eyes e5.") == []
    problems = index.problems("This prepares Qxh7 and the bishop on a5 is strong.")
    assert any("Qxh7" in p for p in problems)
    assert any("bishop on a5" in p for p in problems)
    # Hypotheticals ("a knight on d5 would ...") are not claims about the board.
    assert index.problems("A knight on d5 would be a monster.") == []


def test_move_prompt_contains_grounded_evidence():
    board = chess.Board("6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1")
    move = board.parse_san("h3")
    after = board.copy()
    after.push(move)
    cands = [Line("Rd8#", "d1d8", None, 1, ["Rd8#"], ["d1d8"], "1. Rd8#")]
    reply = [Line("h6", "h7h6", -540, None, ["h6"], ["h7h6"], "1... h6")]
    a = build_move_analysis(board, move, cands, reply, AnalysisConfig())
    prompt = build_move_prompt(board=board, move=move, analysis=a, note_type="full",
                               f_before=features.extract(board), f_after=features.extract(after),
                               motifs_played=[], want_ideas=True)
    assert "had a forced checkmate and missed it" in prompt
    assert "allows" not in prompt.split("Engine verdict")[1].split("\n")[0]
    assert "Pieces before the move — White: Kg1, Rd1" in prompt
    assert "EXPLANATION:" in prompt


def test_system_prompt_is_per_game_and_mentions_profile():
    system = build_system_prompt("black", 1500, "club", "You hang pieces in time trouble.")
    assert "'you' always means Black" in system
    assert "rated about 1500" in system
    assert "You hang pieces in time trouble." in system


# The position from a real report, coaching Black: after 11. axb4 White has no a-pawn
# left, yet an early version said "the opponent's next move may be to play a5" (a5 was
# Black's move three moves later) and described the capture the wrong way round.
AXB4_FEN = "r2qk1nr/pp1n2pp/1b2bp2/3pp3/1p4P1/P2PP2P/1BP2PB1/RN1QK1NR w KQkq - 0 11"
AXB4_NEXT = ["Qc7", "c4", "d4", "e4", "a5", "h4", "axb4"]   # what actually followed


def _axb4_index():
    board = chess.Board(AXB4_FEN)
    move = board.parse_san("axb4")
    after = board.copy()
    after.push(move)
    return EvidenceIndex.for_move(board, move, [(after, AXB4_NEXT)], coached=chess.BLACK)


def test_fact_check_regression_axb4_side_and_capture():
    index = _axb4_index()
    reported = ("White's pawn on a3 has been captured, and the opponent now has a pawn on b4 that can be "
                "used to launch a pawn storm on the queenside. The opponent's next move may be to play a5, "
                "attacking the pawn on b4 and preparing to develop their queenside pieces.")
    problems = index.problems(reported)
    assert any("pawn on a3 is not captured" in p for p in problems)
    assert any("'a5' is not a move White can play" in p for p in problems)
    assert index.clean(reported)[0] == "" or "a5" not in index.clean(reported)[0]
    good = ("White recaptured on b4 with the a-pawn, opening the a-file. Black later answered with ...a5, "
            "attacking the pawn on b4. You could also consider 11...Qc7 to add pressure.")
    assert index.problems(good) == []
    assert index.clean(good) == (good, 0)


def test_fact_check_attributes_moves_to_the_right_side():
    index = _axb4_index()
    assert index.problems("Black answers ...Qc7 and White later pushes c4.") == []
    assert index.problems("White answers with Qc7.") != []            # a Black move given to White
    assert index.problems("Next comes 12. Qc7.") != []                # a move number marks a White move
    assert index.problems("White's next move may be a5.") != []       # White's a-pawn is gone
    assert index.problems("The a5 push is coming.") == []             # no side named: Black can play it
    assert index.problems("White captured the knight on b4.") != []  # only pawns were taken on b4


def test_side_line_labels_every_move():
    board = chess.Board(AXB4_FEN)
    assert side_line(board, ["axb4", "Qc7", "c4"]) == "11. White axb4, 11... Black Qc7, 12. White c4"


def _commentary_moves():
    return [
        CommentaryMove(label="11. axb4", side="White", san="axb4", what="pawn a3 takes pawn on b4",
                       verdict="best", eval_after="White is slightly better", book=False),
        CommentaryMove(label="11... Qc7", side="Black", san="Qc7", what="queen d8 to c7",
                       verdict="good", eval_after="White is slightly better"),
    ]


def test_commentary_prompt_and_parser():
    moves = _commentary_moves()
    ctx = CommentaryContext(start_label="11. axb4", end_label="11... Qc7", eval_before="equal",
                            pieces="White: Ke1; Black: Ke8", hindsight="12. White c4 [pawn break]")
    prompt = build_commentary_prompt(moves, ctx)
    assert prompt.startswith("COMMENTARY WINDOW: moves 11. axb4 to 11... Qc7.")
    assert "- 11. axb4 (White) [" in prompt and "- 11... Qc7 (Black) [" in prompt
    assert "What actually happened after this stretch: 12. White c4" in prompt
    reply = ("Here is the commentary:\n"
             "**11. axb4**: White recaptures and opens the a-file.\n"
             "- 11...Qc7 (Black) — Black lines up on the long diagonal.\n"
             "12. c4: not part of this window")
    parsed = parse_commentary(reply, moves)
    assert parsed == {"11. axb4": "White recaptures and opens the a-file.",
                      "11... Qc7": "Black lines up on the long diagonal."}
