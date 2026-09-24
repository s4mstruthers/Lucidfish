import chess

from lucidfish import features
from lucidfish.config import AnalysisConfig
from lucidfish.engine import Line, build_move_analysis
from lucidfish.prompts import (
    EvidenceIndex,
    build_move_prompt,
    build_system_prompt,
    norm_san,
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
    assert norm_san("exd5") == "d5"
    assert norm_san("O-O-O#") == "O-O-O"
    assert norm_san("e8=Q+") == "e8Q"


def test_evidence_index_flags_invented_moves_and_pieces():
    board = chess.Board()
    move = board.parse_san("e4")
    index = EvidenceIndex(board, move, [(board, ["e4", "e5", "Nf3", "Nc6"])])
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
