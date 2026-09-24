"""Keep the post-game review honest, and balanced.

Small local models pad reviews with stock advice ("review king and pawn endings",
"you made several mistakes under time pressure"), sometimes get the result
backwards ("mistakes that cost you the game" after a win), blame the player for
the opponent's moves, and only ever list what went wrong. The defences are
deterministic:

1. ``game_facts`` gives the model verified facts it would otherwise guess: the
   result from the coached player's side, how the game ended (and what the
   position was worth then), which kinds of endgame it reached, the clock, and
   verified highlights (only-moves found, errors punished, advantages
   converted, clean phases) for a short "What went well" section.
2. ``problems`` lists the factual errors in a written review: the wrong result,
   a move credited to the wrong side, a verdict the engine didn't give ("e5 was
   a mistake" when it wasn't), an endgame the game never reached, or time
   trouble the clocks don't show. The pipeline sends these back once for a
   rewrite.
3. ``check_review`` then removes whatever is still wrong. Each key takeaway and
   each "what went well" point must name a move or a verified fact; stock
   advice and empty praise are dropped. Sections left empty are written from
   the facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import chess

ERRORS = ("inaccuracy", "mistake", "blunder")
MAX_STRENGTHS = 2      # acknowledge what went well, briefly: the review is for improving

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
    coached: str | None = None     # "White", "Black" or None (both)
    winner: str = ""               # "White", "Black", "draw" or "" (unknown)
    endgame_kinds: set[str] = field(default_factory=set)
    reached_endgame: bool = False
    time_support: bool = False     # the clocks or the result make time a fair lesson
    hurried_errors: bool = False   # errors were made with little time left or in a few seconds
    flag_lesson: str = ""          # the coached side lost on time: a takeaway the review must not miss
    fallback: list[str] = field(default_factory=list)    # grounded takeaways, if the model's all fail
    strengths: list[str] = field(default_factory=list)   # grounded "what went well" points, likewise
    highlights: list[str] = field(default_factory=list)
    played: dict[str, list[tuple[str, int, str]]] = field(default_factory=dict)   # SAN -> [(side, move no., cls)]
    opening_words: set[str] = field(default_factory=set)


def _norm(san: str) -> str:
    return re.sub(r"[+#!?]|=[QRBN]", "", san)


def _other(side: str) -> str:
    return "Black" if side == "White" else "White"


def game_facts(moves: list, headers: dict, coached: str | None, opening: str = "",
               base_s: int | None = None, accuracy: dict | None = None) -> GameFacts:
    """Verified facts about the game as a whole. `moves` are AnnotatedMove-like objects (side, move_number,
    san, classification, phase, fen_after, clock_s, think_s, win_white, best_san, critical, analysis)."""
    f = GameFacts(coached=coached)
    if not moves:
        return f
    for m in moves:
        f.played.setdefault(_norm(m.san), []).append((m.side, m.move_number, m.classification))
    f.opening_words = {w.lower() for w in re.findall(r"[A-Za-z]{5,}", opening)} - {"defense", "defence",
                                                                                "opening", "variation", "attack"}
    label = lambda m: f"{m.move_number}{'.' if m.side == 'White' else '...'} {m.san}"  # noqa: E731
    sides = [coached] if coached else ["White", "Black"]

    # How it ended, and who won.
    last = moves[-1]
    result = headers.get("Result", "*")
    termination = headers.get("Termination", "")
    board = chess.Board(last.fen_after)
    on_time = bool(re.search(r"on time|time forfeit|timeout", termination, re.I))
    how = ("by checkmate" if board.is_checkmate() else "on time" if on_time
           else "by resignation" if re.search(r"resign", termination, re.I)
           else "by agreement" if re.search(r"agree", termination, re.I) else "")
    winner = {"1-0": "White", "0-1": "Black"}.get(result)
    f.winner = winner or ("draw" if result == "1/2-1/2" else "")
    if coached and f.winner:
        outcome = "drew" if f.winner == "draw" else "WON" if f.winner == coached else "LOST"
        f.lines.append(f"RESULT: {coached}, the player you are coaching, {outcome} this game"
                       f"{' ' + how if how and f.winner != 'draw' else ''}. The review must get this right.")
    if winner:
        f.lines.append(f"The game ended after {label(last)}: {winner} won{' ' + how if how else ''}"
                       f"{f' ({termination})' if termination and not how else ''}.")
        loser = _other(winner)
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
            hurried = [m for m in mine if m.clock_s < threshold and m.classification in ERRORS]
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

    # What went well (verified), for the coached side.
    if coached:
        f.highlights = _highlights(moves, coached, f.winner, board, how, accuracy or {}, label)
        f.lines += [f"Highlight (verified, for 'What went well'): {h}" for h in f.highlights]
        f.strengths = [f"- {h}" for h in f.highlights[:MAX_STRENGTHS]]

    # Grounded takeaways from the costliest errors (used only if the model's don't survive).
    errors = [m for m in moves if m.side in sides and m.classification in ERRORS and m.analysis is not None]
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


def _highlights(moves: list, me: str, winner: str, final: chess.Board, how: str, accuracy: dict, label) -> list[str]:
    """The best verified things the coached side did, most telling first."""
    out: list[tuple[int, str]] = []
    opp = _other(me)
    mine = [m for m in moves if m.side == me]
    for m in mine:
        if getattr(m, "critical", False) and m.classification in ("best", "good"):
            out.append((10, f"{label(m)}: {me} found the only good move at a critical moment."))
    for prev, m in zip(moves, moves[1:], strict=False):
        if prev.side == opp and prev.classification in ("mistake", "blunder") and m.side == me \
                and m.classification in ("best", "good"):
            out.append((8, f"{label(m)} punished {opp}'s {prev.classification} {label(prev)} straight away."))
    chances = [(m, m.win_white if me == "White" else 100 - m.win_white) for m in moves]
    if winner == me:
        low = min(chances, key=lambda x: x[1])
        if low[1] <= 30:
            out.append((9, f"{me} came back from a worse position (winning chances down to {low[1]:.0f}% after "
                           f"{label(low[0])}) to win."))
        ahead = next((m for m, w in chances if w >= 85), None)
        if ahead is not None:
            out.append((7, f"{me} was winning from {label(ahead)} on and converted it into a win"
                           f"{' ' + how if how else ''}."))
        elif final.is_checkmate():
            out.append((6, f"{me} finished the game with checkmate: {label(moves[-1])}."))
    for phase in ("opening", "middlegame", "endgame"):
        in_phase = [m for m in mine if m.phase == phase]
        if len(in_phase) >= 4 and not any(m.classification in ("mistake", "blunder") for m in in_phase):
            out.append((5, f"No mistakes or blunders by {me} in the {phase} (moves {in_phase[0].move_number}–"
                           f"{in_phase[-1].move_number})."))
    acc = accuracy.get(me.lower())
    if acc is not None and acc >= 85:
        out.append((4, f"{me}'s accuracy was {acc}% (Lichess method)."))
    seen, texts = set(), []
    for _, t in sorted(out, key=lambda x: -x[0]):
        if t not in seen:
            seen.add(t)
            texts.append(t)
    return texts[:5]


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
_ERRORS_UNDER_PRESSURE = re.compile(
    r"(mistakes?|errors?|blunders?|inaccurac\w+)\b[^.]{0,50}\b(time (pressure|trouble|scramble)|low on time|"
    r"short of time|running out of time|the clock)|(time (pressure|trouble|scramble)|low on time|short of time)"
    r"[^.]{0,50}\b(mistakes?|errors?|blunders?|inaccurac\w+)", re.I)
_MOVE_REF = re.compile(r"\bmoves? \d{1,3}\b|\b\d{1,3}\s*(\.{1,3}|…)\s*[KQRBNO]?[a-h1-8x-]", re.I)
# A move in SAN, optionally with who it belongs to ("your Bb4+", "White's Ne4").
_SAN_TOKEN = re.compile(r"(?:\b(your|white's|black's|(?:your )?opponent's)\s+)?"
                        r"(?<![\w/-])((?:[KQRBN][a-h]?[1-8]?x?[a-h][1-8]|[a-h]x[a-h][1-8]|[a-h][1-8]|O-O(?:-O)?)"
                        r"(?:=[QRBN])?[+#]?)(?![\w-])", re.I)
_SQUARE_WORDS = {"on", "to", "from", "at", "the", "square", "squares", "towards", "of", "into", "onto", "via", "and"}
_CITED = re.compile(r"\bmove\s+(\d{1,3})\s*\(\s*(?:\.{0,3}|…)?\s*([^)\s]+)\s*\)", re.I)
_VERDICT_WORD = re.compile(r"\b(blunder|mistake|inaccuracy|error)s?\b", re.I)
# "... was (also) a" right before the verdict word ("not"/"never" break the link, so negations don't count).
_LINK = re.compile(r"\b(?:was|is|were)\b(?:\s+(?:also|again|clearly|another|then|a|an|big|serious|costly|major|"
                   r"real|small|minor|slight|huge|critical))*\s*$", re.I)


def _moves_in(text: str, facts: GameFacts) -> list[tuple[str | None, str]]:
    """(owner, SAN) for each move of this game the text names; owner is the side it credits the move to."""
    out = []
    for m in _SAN_TOKEN.finditer(text):
        owner_word, san = m.group(1), m.group(2)
        norm = _norm(san)
        if norm not in facts.played:
            continue
        if re.fullmatch(r"[a-h][1-8]", norm) and not owner_word:   # a bare square: "the knight on e4"
            before = text[:m.start()].split()
            if before and before[-1].lower().strip(",") in _SQUARE_WORDS:
                continue
        owner = None
        if owner_word:
            w = owner_word.lower()
            owner = ("White" if w.startswith("white") else "Black" if w.startswith("black")
                     else _other(facts.coached) if "opponent" in w and facts.coached
                     else facts.coached if w == "your" else None)
        out.append((owner, norm))
    return out


def _result_problem(s: str, facts: GameFacts) -> str:
    w, me = facts.winner, facts.coached
    if not w:
        return ""
    if w == "draw":
        bad = re.search(r"\b(you|white|black)\s+(won|lost)\b|cost (you|white|black) the game|\b(won|lost) the game",
                        s, re.I)
        return "The game was a draw." if bad else ""
    loser = _other(w)
    pats = [rf"\b{loser}\s+(won|went on to win)\b", rf"\b{w}\s+lost\b", rf"cost {w} the game"]
    if me == w:
        pats += [r"\byou lost\b", r"cost you the game", r"\byour (loss|defeat)\b", rf"(?<!{loser} )lost the game",
                 r"\blosing the game\b", r"not enough to compensate"]
    elif me == loser:
        pats += [r"\byou won\b", r"\byour (win|victory)\b", r"you went on to win", rf"(?<!{w} )won the game"]
    if any(re.search(p, s, re.I) for p in pats):
        return f"{w} won this game" + (f" (you are {me}, so you {'won' if me == w else 'lost'})" if me else "") + "."
    return ""


def _verdict_problem(s: str, facts: GameFacts) -> str:
    """ "c4 was also a mistake" / "e5, which was a mistake": the move named must have that verdict."""
    for vm in _VERDICT_WORD.finditer(s):
        before = s[:vm.start()]
        link = _LINK.search(before)
        if not link:
            continue                      # e.g. "take advantage of White's mistakes": no move is judged
        named = _moves_in(before[max(0, link.start() - 60):link.start()], facts)
        if not named:
            continue
        owner, san = named[-1]
        allowed = {"mistake", "blunder"} if vm.group(1).lower() == "blunder" else set(ERRORS)
        if any((owner is None or owner == side) and cls in allowed for side, _, cls in facts.played[san]):
            continue
        side, n, cls = next(((sd, n, c) for sd, n, c in facts.played[san] if owner in (None, sd)),
                            facts.played[san][0])
        word = "blunder" if vm.group(1).lower() == "blunder" else vm.group(1).lower()
        return f"{san} ({side}, move {n}) was rated '{cls}' by the engine, not a {word}."
    return ""


def _side_problem(s: str, facts: GameFacts) -> str:
    for owner, san in _moves_in(s, facts):
        sides = {side for side, _, _ in facts.played[san]}
        if owner and owner not in sides:
            actual = sides.pop()
            who = "yours" if owner == facts.coached else f"{owner}'s"
            return f"{san} was {actual}'s move, not {who}."
    return ""


def _problem(sentence: str, facts: GameFacts) -> str:
    """Why a sentence isn't supported by the game ('' if it is)."""
    for pattern, kind in _ENDGAME_CLAIMS:
        if pattern.search(sentence):
            if (kind == "any" and not facts.reached_endgame) or (kind != "any" and kind not in facts.endgame_kinds):
                return "This game never reached that kind of endgame."
            break
    if not facts.hurried_errors and _ERRORS_UNDER_PRESSURE.search(sentence):
        return "The clocks show no errors made with little time left."
    if not facts.time_support and _TIME_CLAIM.search(sentence):
        return "Nothing in the clocks or the result shows time trouble."
    return _result_problem(sentence, facts) or _verdict_problem(sentence, facts) or _side_problem(sentence, facts)


def _cited_problem(bullet: str, facts: GameFacts) -> str:
    """A takeaway citing "Move 6 (Ne4)" must be about the player's own move, or say it was the opponent's."""
    me = facts.coached
    if not me:
        return ""
    opp = _other(me)
    for n, san in _CITED.findall(bullet):
        by = {side for side, num, _ in facts.played.get(_norm(san), []) if num == int(n)}
        if by == {opp} and not re.search(rf"\b{opp}\b|opponent|\btheir\b", bullet, re.I):
            return f"Move {n} ({san}) was {opp}'s move, not yours."
    return ""


def _grounded(bullet: str, facts: GameFacts) -> bool:
    """Does a bullet point at this game: a move, or a verified fact it is allowed to talk about?"""
    if _MOVE_REF.search(bullet) or _moves_in(bullet, facts):
        return True
    if facts.time_support and _TIME_CLAIM.search(bullet):
        return True
    if facts.reached_endgame and re.search(r"\bendgames?\b|\bendings?\b", bullet, re.I):
        return True
    if re.search(r"\baccuracy\b|\bcheckmate\b|\bcame back\b|\bconverted\b", bullet, re.I) and facts.highlights:
        return True
    words = {w.lower() for w in re.findall(r"[A-Za-z]{5,}", bullet)}
    return bool(facts.opening_words & words)


_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z*])")
_LEAD = re.compile(r"^(\s*(?:[-*•]|\d+[.)])\s+(?:\*\*[^*]+\*\*:?\s*)?)")


def _split(text: str) -> tuple[str, list[str]]:
    lead = _LEAD.match(text)
    head, body = (lead.group(1), text[lead.end():]) if lead else ("", text)
    return head, [x for x in _SENTENCE.split(body) if x.strip()]


def _clean_text(text: str, facts: GameFacts) -> str:
    """Drop unsupported sentences from a paragraph or bullet (keeping a bullet's '- ' and bold lead)."""
    head, sentences = _split(text)
    if head and _problem(head, facts):
        return ""
    kept = [s for s in sentences if not _problem(s, facts)]
    return head + " ".join(kept) if kept else ""


def _only_lead(text: str) -> bool:
    """A bullet with nothing left but its marker and bold title."""
    return bool(re.fullmatch(r"\s*(?:[-*•]|\d+[.)])\s*(\*\*[^*]+\*\*:?)?\s*", text))


def _is_bullet(line: str) -> bool:
    return bool(re.match(r"^\s*(?:[-*•]|\d+[.)])\s+", line))


def _section(header: str) -> str:
    h = header.lower()
    return "takeaways" if "takeaway" in h else "strengths" if "went well" in h or "strength" in h else "other"


def problems(review: str, facts: GameFacts) -> list[str]:
    """Factual errors in a review, worded for the model to fix (generic advice isn't listed: it's replaced)."""
    out: list[str] = []
    section = "other"
    for line in review.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            section = _section(stripped)
            continue
        if not stripped:
            continue
        head, sentences = _split(stripped)
        for s in ([head] if head else []) + sentences:
            why = _problem(s, facts)
            if why:
                out.append(f'"{s.strip()[:140]}": {why}')
        if section in ("takeaways", "strengths") and _is_bullet(line):
            why = _cited_problem(stripped, facts)
            if why:
                out.append(f'"{stripped[:140]}": {why}')
    return out


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
    section, kept, mentions_time, has_strengths = "other", 0, False, False

    def close() -> None:
        if section == "takeaways":
            out.extend(_missing(facts, kept, mentions_time))
        elif section == "strengths" and not kept:
            out.extend(facts.strengths)

    for line in review.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            close()
            section, kept, mentions_time = _section(stripped), 0, False
            if section == "takeaways" and not has_strengths and facts.strengths:
                out.extend(["## What went well", *facts.strengths, ""])   # the model left it out
                has_strengths = True
            has_strengths = has_strengths or section == "strengths"
            out.append(line)
            continue
        if not stripped:
            out.append(line)
            continue
        if section in ("takeaways", "strengths") and _is_bullet(line):
            cleaned = _clean_text(line, facts)
            ok = (cleaned.strip() and not _only_lead(cleaned) and _grounded(cleaned, facts)
                  and not _cited_problem(cleaned, facts)
                  and not (section == "strengths" and kept >= MAX_STRENGTHS))
            if ok:
                out.append(cleaned)
                kept += 1
                mentions_time = mentions_time or bool(_TIME_CLAIM.search(cleaned))
            if not ok or cleaned.strip() != stripped:
                removed.append(stripped)
            continue
        cleaned = _clean_text(line, facts)
        if cleaned.strip() != stripped:
            removed.append(stripped)
        if cleaned.strip() and not _only_lead(cleaned):
            out.append(cleaned)
    close()
    if not has_strengths and facts.strengths and section != "takeaways":
        out.extend(["", "## What went well", *facts.strengths])
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip(), removed
