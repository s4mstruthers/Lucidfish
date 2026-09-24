"""Keep the post-game review honest.

Small local models pad reviews with stock advice ("review king and pawn endings",
"you made several mistakes under time pressure") whether or not the game had
anything to do with it. Two defences, both deterministic:

1. ``game_facts`` gives the model verified facts it would otherwise guess: how
   the game ended (and what the position was worth then), which kinds of
   endgame the game reached (or that it never reached one), and the clock.
2. ``check_review`` runs over the written review. A sentence about a kind of
   endgame the game never reached, or about time trouble that neither the
   clocks nor the result show, is removed. Each key takeaway must also point at
   something in this game (a move or a verified fact); stock advice that
   doesn't is dropped. If no takeaway survives, grounded ones are written from
   the facts instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import chess

# ------------------------------------------------------------------ facts

_KIND_NAMES = {"pawn": "a king-and-pawn endgame", "rook": "a rook endgame", "queen": "a queen endgame",
               "minor": "a minor-piece endgame (bishops/knights)", "mixed": "an endgame with mixed pieces"}


def endgame_kind(board: chess.Board) -> str:
    """'pawn', 'rook', 'queen', 'minor' or 'mixed' by the pieces left (call only for endgame positions)."""
    types = {p.piece_type for p in board.piece_map().values()} - {chess.KING, chess.PAWN}
    if not types:
        return "pawn"
    if types == {chess.ROOK}:
        return "rook"
    if types == {chess.QUEEN}:
        return "queen"
    if types <= {chess.BISHOP, chess.KNIGHT}:
        return "minor"
    return "mixed"


def _pieces_left(board: chess.Board) -> str:
    names = []
    for color, side in ((chess.WHITE, "White"), (chess.BLACK, "Black")):
        counts = [(len(board.pieces(pt, color)), chess.piece_name(pt))
                  for pt in (chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT, chess.PAWN)]
        parts = [f"{n} {name}{'s' if n > 1 else ''}" for n, name in counts if n]
        names.append(f"{side}: king" + (", " + ", ".join(parts) if parts else " only"))
    return "; ".join(names)


def _clock(s: float) -> str:
    s = max(0, int(round(s)))
    return f"{s // 60}:{s % 60:02d}"


@dataclass
class GameFacts:
    lines: list[str] = field(default_factory=list)      # for the prompt
    endgame_kinds: set[str] = field(default_factory=set)
    reached_endgame: bool = False
    time_support: bool = False     # the clocks or the result make time a fair lesson
    hurried_errors: bool = False   # errors were made with little time left or in a few seconds
    flag_lesson: str = ""          # the coached side lost on time: a takeaway the review must not miss
    fallback: list[str] = field(default_factory=list)   # grounded takeaways, if the model's all fail
    sans: set[str] = field(default_factory=set)
    opening_words: set[str] = field(default_factory=set)


def game_facts(moves: list, headers: dict, coached: str | None, opening: str = "",
               base_s: int | None = None) -> GameFacts:
    """Verified facts about the game as a whole. `moves` are AnnotatedMove-like objects (side, move_number,
    san, classification, phase, fen_after, clock_s, think_s, win_white, best_san, analysis)."""
    f = GameFacts()
    if not moves:
        return f
    f.sans = {m.san.rstrip("+#") for m in moves}
    f.opening_words = {w.lower() for w in re.findall(r"[A-Za-z]{5,}", opening)} - {"defense", "defence",
                                                                                "opening", "variation", "attack"}
    label = lambda m: f"{m.move_number}{'.' if m.side == 'White' else '...'} {m.san}"  # noqa: E731
    sides = [coached] if coached else ["White", "Black"]

    # How it ended.
    last = moves[-1]
    result = headers.get("Result", "*")
    termination = headers.get("Termination", "")
    board = chess.Board(last.fen_after)
    on_time = bool(re.search(r"on time|time forfeit|timeout", termination, re.I))
    how = ("by checkmate" if board.is_checkmate() else "on time" if on_time
           else "by resignation" if re.search(r"resign", termination, re.I)
           else "by agreement" if re.search(r"agree", termination, re.I) else "")
    winner = {"1-0": "White", "0-1": "Black"}.get(result)
    if winner:
        f.lines.append(f"The game ended after {label(last)}: {winner} won{' ' + how if how else ''}"
                       f"{f' ({termination})' if termination and not how else ''}.")
        loser = "Black" if winner == "White" else "White"
        loser_win = last.win_white if loser == "White" else 100 - last.win_white
        if not board.is_checkmate() and loser_win >= 70:
            state = "had a forced mate" if (last.analysis and last.analysis.eval_after_mate is not None
                                            and loser_win >= 95) else "was winning" if loser_win >= 85 else "was better"
            f.lines.append(f"When the game ended, {loser} {state} (winning chances {loser_win:.0f}%), "
                           f"but lost{' on time' if on_time else ''}.")
            if on_time and loser in sides:
                f.time_support = True
                f.flag_lesson = (f"- **Keep time to finish won games**: you lost on time after {label(last)} although "
                                 f"you {state}. Save time on moves you know, so there is some left for the "
                                 "position that decides the game.")
                f.fallback.append(f.flag_lesson)
    elif result == "1/2-1/2":
        f.lines.append(f"The game ended in a draw after {label(last)}{f' ({termination})' if termination else ''}.")

    # Which endgames, if any.
    first_eg = next((m for m in moves if m.phase == "endgame"), None)
    if first_eg is None:
        f.lines.append(f"The game never reached an endgame: it ended in the {last.phase or 'middlegame'} with "
                       f"{_pieces_left(board)}. Do not give endgame advice.")
    else:
        f.reached_endgame = True
        kinds = []
        for m in moves:
            if m.phase == "endgame":
                k = endgame_kind(chess.Board(m.fen_after))
                if k not in kinds:
                    kinds.append(k)
        f.endgame_kinds = set(kinds)
        f.lines.append(f"The endgame began after {label(first_eg)}. Kinds of endgame reached: "
                       + ", then ".join(_KIND_NAMES[k] for k in kinds)
                       + f". Final material: {_pieces_left(board)}. Only these kinds of endgame may be discussed.")

    # The clock.
    clocked = [m for m in moves if m.clock_s is not None]
    if clocked:
        for side in sides:
            mine = [m for m in clocked if m.side == side]
            if not mine:
                continue
            low = min(mine, key=lambda m: m.clock_s)
            start = base_s or max(m.clock_s for m in mine)
            threshold = max(10.0, 0.1 * start)
            hurried = [m for m in mine if m.clock_s < threshold and m.classification in ("inaccuracy", "mistake",
                                                                                          "blunder")]
            f.lines.append(f"{side}'s lowest clock: {_clock(low.clock_s)} after {label(low)}. "
                           + (f"Errors {side} made with under {_clock(threshold)} left: "
                              + ", ".join(f"{label(m)} ({m.classification})" for m in hurried) + "."
                              if hurried else f"{side} made no errors with under {_clock(threshold)} on the clock."))
            if hurried or low.clock_s < threshold:
                f.time_support = True
            f.hurried_errors = f.hurried_errors or bool(hurried)
    else:
        f.lines.append("The PGN has no clock times, so nothing is known about time trouble.")
    hasty = [m for m in moves if m.side in sides and m.classification in ("mistake", "blunder")
             and m.think_s is not None and m.think_s <= 3]
    if hasty:
        f.time_support = f.hurried_errors = True

    # Grounded takeaways from the costliest errors (used only if the model's don't survive).
    errors = [m for m in moves if m.side in sides and m.classification in ("mistake", "blunder", "inaccuracy")
              and m.analysis is not None]
    errors.sort(key=lambda m: -m.analysis.win_loss)
    for m in errors[:2]:
        a = m.analysis
        f.fallback.append(f"- **{label(m)}** was {'an' if m.classification == 'inaccuracy' else 'a'} "
                          f"{m.classification}: {m.side}'s winning chances went from {a.win_before:.0f}% to "
                          f"{a.win_after:.0f}%. {m.best_san} was better. Replay this position in "
                          "\"Practise my mistakes\".")
    if not errors:
        f.fallback.append("- No mistakes or blunders from you in this game: the engine found only small "
                          "improvements. Keep playing this way.")
    return f


# ------------------------------------------------------------------ checking

# What a sentence talks about → the kind of endgame it needs ("any" = any endgame at all).
_ENDGAME_CLAIMS = [
    (re.compile(r"king[- ]?(and|&|\+)[- ]?pawns?|pawn endings?|pawn endgames?|\bK\+?P\b|\bopposition\b|"
                r"rule of the square|square of the pawn|triangulation|outside passed pawn", re.I), "pawn"),
    (re.compile(r"rook (endgames?|endings?)|rook[- ]and[- ]pawn|lucena|philidor|cut(ting)? off the king",
                re.I), "rook"),
    (re.compile(r"queen (endgames?|endings?)", re.I), "queen"),
    (re.compile(r"(bishop|knight|minor[- ]piece)s? (endgames?|endings?)|opposite[- ]colou?red bishops? "
                r"(endgames?|endings?)", re.I), "minor"),
    (re.compile(r"\bendgames?\b|\bendings?\b|\bend game\b", re.I), "any"),
]
_TIME_CLAIM = re.compile(r"time (pressure|trouble|scramble|management|control)|\b(on|out of|short of|low on) time\b|"
                         r"\bclock\b|\bflag(ged)?\b|\btoo (fast|quickly)\b|\bhast(y|ily)\b|\brush(ed|ing)?\b", re.I)
_MOVE_REF = re.compile(r"\bmoves? \d{1,3}\b|\b\d{1,3}\s*(\.{1,3}|…)\s*[KQRBNO]?[a-h1-8x-]", re.I)
_SAN = re.compile(r"\b(?:[KQRBN][a-h]?[1-8]?x?[a-h][1-8]|[a-h]x[a-h][1-8]|[a-h][1-8]|O-O(?:-O)?)\b")


def _endgame_ok(sentence: str, facts: GameFacts) -> bool:
    for pattern, kind in _ENDGAME_CLAIMS:
        if pattern.search(sentence):
            if kind == "any":
                return facts.reached_endgame
            return kind in facts.endgame_kinds
    return True


_ERRORS_UNDER_PRESSURE = re.compile(
    r"(mistakes?|errors?|blunders?|inaccurac\w+)\b[^.]{0,50}\b(time (pressure|trouble|scramble)|low on time|"
    r"short of time|running out of time|the clock)|(time (pressure|trouble|scramble)|low on time|short of time)"
    r"[^.]{0,50}\b(mistakes?|errors?|blunders?|inaccurac\w+)", re.I)


def _supported(sentence: str, facts: GameFacts) -> bool:
    if not _endgame_ok(sentence, facts):
        return False
    if not facts.hurried_errors and _ERRORS_UNDER_PRESSURE.search(sentence):
        return False      # e.g. "several mistakes under time pressure" when the clocks show none
    return facts.time_support or not _TIME_CLAIM.search(sentence)


def _grounded(bullet: str, facts: GameFacts) -> bool:
    """Does a takeaway point at this game: a move, or a verified fact it is allowed to talk about?"""
    if _MOVE_REF.search(bullet) or any(m.rstrip("+#") in facts.sans for m in _SAN.findall(bullet)):
        return True
    if facts.time_support and _TIME_CLAIM.search(bullet):
        return True
    if facts.reached_endgame and re.search(r"\bendgames?\b|\bendings?\b", bullet, re.I):
        return True
    words = {w.lower() for w in re.findall(r"[A-Za-z]{5,}", bullet)}
    return bool(facts.opening_words & words)


_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z*])")


def _clean_text(text: str, facts: GameFacts) -> str:
    """Drop unsupported sentences from a paragraph or bullet (keeping a bullet's '- ' and bold lead)."""
    lead = re.match(r"^(\s*(?:[-*•]|\d+[.)])\s+(?:\*\*[^*]+\*\*:?\s*)?)", text)
    head, body = (lead.group(1), text[lead.end():]) if lead else ("", text)
    if head and not _supported(head, facts):
        return ""
    kept = [s for s in _SENTENCE.split(body) if _supported(s, facts)]
    return head + " ".join(kept) if kept else ""


def _only_lead(text: str) -> bool:
    """A bullet with nothing left but its marker and bold title."""
    return bool(re.fullmatch(r"\s*(?:[-*•]|\d+[.)])\s*(\*\*[^*]+\*\*:?)?\s*", text))


def _missing(facts: GameFacts, kept: int, mentions_time: bool) -> list[str]:
    """Takeaways to add after the model's: all of the grounded ones if none survived, or the time forfeit."""
    if not kept:
        return list(facts.fallback)
    return [facts.flag_lesson] if facts.flag_lesson and not mentions_time else []


def check_review(review: str, facts: GameFacts) -> tuple[str, list[str]]:
    """The review with unsupported claims removed, and what was removed (for the log/tests)."""
    if not review.strip():
        return review, []
    removed: list[str] = []
    out: list[str] = []
    in_takeaways, takeaway_count, mentions_time = False, 0, False
    for line in review.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if in_takeaways:
                out.extend(_missing(facts, takeaway_count, mentions_time))
            in_takeaways = "takeaway" in stripped.lower()
            takeaway_count, mentions_time = 0, False
            out.append(line)
            continue
        if not stripped:
            out.append(line)
            continue
        is_bullet = bool(re.match(r"^\s*(?:[-*•]|\d+[.)])\s+", line))
        if in_takeaways and is_bullet:
            cleaned = _clean_text(line, facts)
            if cleaned.strip() and _grounded(cleaned, facts) and not _only_lead(cleaned):
                out.append(cleaned)
                takeaway_count += 1
                mentions_time = mentions_time or bool(_TIME_CLAIM.search(cleaned))
                if cleaned.strip() != stripped:
                    removed.append(stripped)
            else:
                removed.append(stripped)
            continue
        cleaned = _clean_text(line, facts)
        if cleaned.strip() != stripped:
            removed.append(stripped)
        if cleaned.strip() and not _only_lead(cleaned):
            out.append(cleaned)
    if in_takeaways:
        out.extend(_missing(facts, takeaway_count, mentions_time))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip(), removed
