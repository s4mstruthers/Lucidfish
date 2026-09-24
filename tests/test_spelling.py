"""British English everywhere: the coach's text, opening names, and text stored by older versions."""
import json
import sqlite3

import chess

from lucidfish import prompts, store
from lucidfish.llm import clean_output
from lucidfish.opening import OpeningExplorer, local_opening_name
from lucidfish.spelling import british


def test_american_spellings_become_british():
    assert british("Analyze the Sicilian Defense: he realized the center was weak.") \
        == "Analyse the Sicilian Defence: he realised the centre was weak."
    assert british("ANALYZING, Recognized, unorganized, maneuvering toward his favorite gray square") \
        == "ANALYSING, Recognised, unorganised, manoeuvring towards his favourite grey square"
    assert british("capitalize on it, equalized, neutralization, paralyzed") \
        == "capitalise on it, equalised, neutralisation, paralysed"


def test_only_whole_words_from_the_list_change():
    text = "size, prize, seize, citizen, the Colorado Gambit, Brazil, Nf3 Bxe5+ 12...O-O, analysis, practice"
    assert british(text) == text
    assert british("") == "" and british(None) is None


def test_the_coach_writes_british_english():
    for system in (prompts.COACH_RULES, prompts.GAME_REVIEW_SYSTEM, prompts.COACH_CHAT_SYSTEM,
                   prompts.player_summary_system("rapid")):
        assert "Write in British English" in system
    # ...and what it still spells the American way is corrected on the way in
    assert clean_output("<think>hmm</think> You analyzed the center well.") == "You analysed the centre well."


def test_opening_names_are_british():
    assert local_opening_name("e4 c5 Nc3".split()) == "Sicilian Defence: Closed (B23)"
    assert local_opening_name("e4 e5 d4".split()) == "Centre Game (C21)"
    fen = chess.Board().fen()
    store.explorer_put(fen, {"name": "Sicilian Defense", "eco": "B20", "top_moves": []})   # cached by an old version
    assert OpeningExplorer().lookup(fen).name == "Sicilian Defence"


def test_text_stored_by_older_versions_is_made_british_once():
    store.init()
    pid = store.create_profile(name="Sam")
    pgn = '[Opening "Sicilian Defense"]\n[ECOUrl "https://www.chess.com/openings/Sicilian-Defense"]\n\n1. e4 c5 *\n'
    gid = store.save_game(pid, pgn, {}, "white", None, "Sicilian Defense (B20)", "You analyzed the center well.",
                          [{"side": "White", "cls": "best", "cp_loss": 0, "note": "Controls the center."}], "rapid")
    drawings = {"fen": {"arrows": [{"from": "g1", "to": "f3", "color": "red"}], "circles": []}}
    with sqlite3.connect(store.db_path()) as c:          # as an older version left the database
        c.execute("ALTER TABLE games RENAME COLUMN analysed_at TO analyzed_at")
        c.execute("UPDATE games SET annotations_json=? WHERE id=?", (json.dumps(drawings), gid))
        c.execute("UPDATE profiles SET summary='Your favorite defense.' WHERE id=?", (pid,))
        c.execute("DELETE FROM kv WHERE key='british_spelling'")
    store._initialised.clear()
    store.init()
    g = store.get_game(gid)
    assert g["opening"] == "Sicilian Defence (B20)" and g["review"] == "You analysed the centre well."
    assert g["moves"][0]["note"] == "Controls the centre."
    assert g["annotations"]["fen"]["arrows"][0] == {"from": "g1", "to": "f3", "colour": "red"}
    assert g["pgn"] == pgn                                # the game record itself is never changed
    assert store.get_profile(pid)["summary"] == "Your favourite defence."
    assert store.list_games(pid)[0]["analysed_at"]
