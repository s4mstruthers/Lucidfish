"""British English: Lucidfish writes British spelling everywhere (analyse, colour, centre, defence).

The AI coach is asked to write British English, but models trained mostly on American text still slip,
and opening names from the Lichess database are American ("Sicilian Defense"). ``british`` turns the
common American spellings into British ones, keeping the capitalisation. It changes whole words from a
fixed list only (never "size", "prize" or "Colorado"), so chess notation and names are untouched.
"""

from __future__ import annotations

import re

# Whole words: American → British.
_WORDS = {
    "color": "colour", "colors": "colours", "colored": "coloured", "coloring": "colouring",
    "colorful": "colourful", "colorless": "colourless",
    "center": "centre", "centers": "centres", "centered": "centred", "centering": "centring",
    "defense": "defence", "defenses": "defences", "defenseless": "defenceless",
    "offense": "offence", "offenses": "offences",
    "behavior": "behaviour", "behaviors": "behaviours",
    "favor": "favour", "favors": "favours", "favored": "favoured", "favoring": "favouring",
    "favorable": "favourable", "favorably": "favourably", "unfavorable": "unfavourable",
    "favorite": "favourite", "favorites": "favourites",
    "honor": "honour", "honors": "honours", "honored": "honoured",
    "neighbor": "neighbour", "neighbors": "neighbours", "neighboring": "neighbouring",
    "humor": "humour", "labor": "labour", "rigor": "rigour", "vigor": "vigour", "armor": "armour",
    "maneuver": "manoeuvre", "maneuvers": "manoeuvres", "maneuvered": "manoeuvred",
    "maneuvering": "manoeuvring",
    "judgment": "judgement", "judgments": "judgements",
    "toward": "towards", "gray": "grey",
    "labeled": "labelled", "labeling": "labelling", "canceled": "cancelled", "canceling": "cancelling",
    "traveled": "travelled", "traveling": "travelling", "modeled": "modelled", "modeling": "modelling",
    "leveled": "levelled", "signaled": "signalled", "signaling": "signalling", "fueled": "fuelled",
    "fulfill": "fulfil", "fulfills": "fulfils", "fulfillment": "fulfilment", "skillful": "skilful",
    "skillfully": "skilfully", "catalog": "catalogue",
}

# Stems whose -ize / -yze becomes -ise / -yse in every form (realize, realized, realization, ...),
# also after a prefix (unrecognized, reorganize).
_STEMS = (
    "anal", "paral", "catal",
    "real", "recogn", "organ", "priorit", "summar", "emphas", "minim", "maxim", "optim", "critic",
    "apolog", "memor", "util", "visual", "neutral", "stabil", "central", "jeopard", "mobil", "immobil",
    "special", "character", "categor", "familiar", "final", "harmon", "capital", "normal", "general",
    "standard", "personal", "symbol", "author", "scrutin", "sympath", "agon", "patron", "energ",
    "revital", "material", "initial", "synchron", "custom", "local", "penal", "rational", "theor",
    "econom", "dramat", "trivial", "equal", "polar", "fantas", "hypothes", "internal",
)
_STEM_RE = re.compile(r"\b((?:un|re|de|over|under|mis)?(?:" + "|".join(sorted(_STEMS, key=len, reverse=True))
                      + r"))([iy])([zZ])"
                      r"(e|es|ed|er|ers|ing|ation|ations|able)\b", re.I)
_WORD_RE = re.compile(r"\b(" + "|".join(sorted(_WORDS, key=len, reverse=True)) + r")\b", re.I)


def _like(original: str, new: str) -> str:
    """`new` with the capitalisation of `original`."""
    if original.isupper() and len(original) > 1:
        return new.upper()
    if original[0].isupper():
        return new[0].upper() + new[1:]
    return new


def british(text: str) -> str:
    """`text` with American spellings made British."""
    if not text:
        return text
    text = _STEM_RE.sub(lambda m: m[1] + m[2] + ("S" if m[3] == "Z" else "s") + m[4], text)
    return _WORD_RE.sub(lambda m: _like(m[0], _WORDS[m[0].lower()]), text)
