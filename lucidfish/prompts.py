# Copyright (C) 2026 Sam Struthers
# SPDX-License-Identifier: AGPL-3.0-or-later
# This file is part of Lucidfish <https://github.com/s4mstruthers/Lucidfish>; see LICENSE for the terms.

"""Prompt construction, output parsing, and fact-checking of LLM notes.

Design rule: the LLM NEVER analyses the position itself. It receives verified
evidence — engine lines, winning-chance changes, board-verified tactics,
positional facts, plans tracked from the moves played, what actually happened
next — and narrates it in chess concepts.

Two ways of writing about a game:

- **Per-move notes** (``build_move_prompt``): a detailed explanation of one
  move. Used for mistakes and critical moments, and for every move at the
  "every move" detail level.
- **Commentary windows** (``build_commentary_prompt``): one request covers a
  stretch of consecutive moves and returns a short commentator-style line for
  each. Seeing the moves together is what lets the model describe the *flow*
  ("b4 grabs space… …c5 challenges it… axb4 opens the a-file"), and sharing
  the context across the window roughly halves the text the model must read.

Every move in every line is written with its side's name ("12. White c4,
12... Black d4"). Small models misread bare numbered notation and attribute
moves to the wrong player; explicit names prevent that, and the fact-checker
verifies it afterwards.

Prompts are split so that everything constant for a game (rules, player level,
coach profile) sits in the system prompt, which local servers and cloud APIs
can reuse between requests instead of re-reading it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import chess

from . import ratings
from .engine import Line, MoveAnalysis, describe_move
from .features import PositionFeatures, diff_lines, piece_placement
from .lines import line_consequences
from .schemas import chapter_from, ideas_from, note_from, parse_json, window_lines
from .scoring import describe_eval

LEVEL_NOTES = {
    "beginner": "a beginner — explain fundamentals plainly (hanging pieces, one-move threats, "
                "development, king safety) and avoid long lines",
    "casual": "a casual player — fundamentals plus simple plans, lines of 2-3 moves",
    "club": "a club player — concrete plans and tactics up to 2-3 moves deep",
    "advanced": "an advanced player — be concrete and positional; deeper lines are fine",
}

# Every text the coach writes is in British English (as the rest of Lucidfish); llm.clean_output also
# converts the American spellings a model still uses (spelling.british).
BRITISH_ENGLISH = ('Write in British English: analyse, colour, centre, defence, favourite, realise, '
                   'manoeuvre; "practise" is the verb and "practice" the noun.')

COACH_RULES = f"""You are a chess commentator and coach. You explain what each player is \
trying to do — their plans, intentions and strategies — and why moves work or fail. \
Each request gives you VERIFIED evidence: engine lines and evaluations, tactics computed \
from the board, positional facts, plans tracked from the moves played, and what actually \
happened next in the game. You narrate that evidence; you never analyse the position yourself.

Hard rules:
- Use only claims present in the evidence. Never invent tactics, threats, moves or lines.
- Always name the side: say "White" or "Black" (you may add "you" for the player you coach). \
Never write "the opponent" without naming the colour.
- A move you name must be one the side you attribute it to can actually play: it must \
appear in the evidence for that side (the moves played, a candidate, or a line where \
every move is labelled with its side).
- The evidence states which piece moved and what it captured ("pawn from a3 to b4, \
captures the pawn on b4" means the a3 pawn did the capturing). Never reverse this, and \
never mention a piece on a square where the evidence has none.
- You may use what actually happened next to explain intentions ("this prepares ...a5, \
which came two moves later"), but a move being played later does not make it good.
- If you are unsure whether something is true, leave it out.
- Explain with concepts (space, development, pawn breaks, open files, king safety, piece \
activity, weak squares, forks, pins) and translate evaluations into words.
- Never mention centipawns or numbers like +1.3; say "slightly better", "winning", \
"about a pawn's worth".
- {BRITISH_ENGLISH}
- Answer in exactly the format requested. When a JSON object is requested, reply with that JSON \
object alone: no text before or after it and no markdown fences."""


def rating_text(elo: int, label: str = "") -> str:
    """'about 1410 (chess.com blitz)', with a reminder that the sites' scales differ."""
    text = f"about {elo}" + (f" ({label})" if label else "")
    if "chess.com" in label or "Lichess" in label:
        text += "; the same player is usually rated a few hundred points higher on Lichess than on chess.com"
    return text


def elo_guidance(elo: int | None, label: str = "") -> str:
    if not elo:
        return ""
    return (f"The player is rated {rating_text(elo, label)}. At lower ratings favour fundamentals (hanging pieces, "
            "one-move threats, development, king safety) and keep lines to 2-3 moves; at higher "
            "ratings be more concrete and positional. Aim each lesson at what would take this "
            "player to the next level.")


def build_system_prompt(coached_side: str | None = None, elo: int | None = None,
                        level: str | None = None, player_context: str = "",
                        players: dict | None = None, rating_label: str = "") -> str:
    """Per-game system prompt: identical for every request of a game, so it can be cached."""
    parts = [COACH_RULES, "\nContext for this game:"]
    if players:
        parts.append(f"- White is {players.get('White', 'White')}; Black is {players.get('Black', 'Black')}.")
    if coached_side:
        side = coached_side.capitalize()
        other = "Black" if side == "White" else "White"
        parts.append(f"- You are coaching {side}: 'you' always means {side}, and {other} is their opponent.")
    else:
        parts.append("- You are coaching both players; refer to them as White and Black.")
    if level and level.lower() in LEVEL_NOTES:
        parts.append(f"- The player is {LEVEL_NOTES[level.lower()]}.")
    if elo:
        parts.append("- " + elo_guidance(elo, rating_label))
    if player_context:
        parts.append("- Coach profile of this player from previous games:\n" + player_context.strip()
                     + "\n  When a move repeats one of their recurring issues, point out the pattern "
                     "explicitly — that connection is more valuable than the move itself.")
    return "\n".join(parts)


# --------------------------------------------------------------- helpers

def _white_pov(cp: int | None, mate: int | None, mover_is_white: bool) -> tuple[int | None, int | None]:
    if mover_is_white:
        return cp, mate
    return (-cp if cp is not None else None), (-mate if mate is not None else None)


def line_eval_words(line: Line, board: chess.Board) -> str:
    """Engine lines are scored for the side to move; describe them from White's view."""
    cp, mate = _white_pov(line.score_cp, line.mate_in, board.turn == chess.WHITE)
    return describe_eval(cp, mate)


def candidate_desc(board: chess.Board, san: str) -> str:
    """Verified physical description of a candidate move ('' if it can't be parsed)."""
    try:
        return describe_move(board, board.parse_san(san))
    except ValueError:
        return ""


def move_label(board: chess.Board, san: str) -> str:
    n = board.fullmove_number
    return f"{n}. {san}" if board.turn == chess.WHITE else f"{n}... {san}"


def side_line(board: chess.Board, sans: list[str], limit: int | None = None) -> str:
    """'11. White axb4, 11... Black Qc7, 12. White c4' — every move carries its side."""
    b = board.copy(stack=False)
    out = []
    for san in sans[:limit]:
        n = b.fullmove_number
        out.append(f"{n}. White {san}" if b.turn == chess.WHITE else f"{n}... Black {san}")
        try:
            b.push_san(san)
        except ValueError:
            break
    return ", ".join(out)


def eval_after_words(a: MoveAnalysis, mover_is_white: bool) -> str:
    if a.game_result:
        return "the game is over (" + ("checkmate" if a.mate_event == "delivered_mate" else "a draw") + ")"
    return describe_eval(*_white_pov(a.eval_after_cp, a.eval_after_mate, mover_is_white))


# --------------------------------------------------------------- per move

def build_move_prompt(
    *,
    board: chess.Board,                 # position before the move
    move: chess.Move,
    analysis: MoveAnalysis,
    note_type: str,                     # "full" | "brief" | "opponent"
    f_before: PositionFeatures,
    f_after: PositionFeatures,
    coached_side: str | None = None,
    critical: bool = False,
    motifs_played: list[str] | None = None,
    motifs_best: list[str] | None = None,
    motifs_reply: list[str] | None = None,
    reply_line: Line | None = None,     # best line for the side to move AFTER the move
    opening_name: str = "",
    opening_lines: list[str] | None = None,
    recent: str = "",                   # labelled previous moves with gists
    hindsight: str = "",                # labelled moves actually played next
    plans: list[str] | None = None,     # verified plan facts (plans.plan_lines)
    threat: str = "",                   # the error ignored this existing threat (tactics.existing_threat)
    left_book: str = "",                # the move left opening theory (+ what masters play)
    time_note: str = "",
    previous: dict | None = None,       # {"san","cls","side"} of the previous (opponent) move
    want_ideas: bool = False,
    players: list[str] | None = None,   # what players at this rating do here (opening.player_lines)
    tools: bool = False,                # the model may call check_line: give it the FEN and say so
) -> str:
    """Assemble the grounded evidence block for one move."""
    mover_is_white = board.turn == chess.WHITE
    side = "White" if mover_is_white else "Black"
    opp = "Black" if mover_is_white else "White"
    a = analysis
    after = board.copy(stack=False)
    after.push(move)
    p: list[str] = []

    verdict = a.classification + (" — a CRITICAL MOMENT" if critical else "")
    p.append(f"MOVE: {move_label(board, a.played_san)}, played by {side}. Engine verdict: {verdict}.")
    p.append(f"What physically happened: {side}'s {describe_move(board, move)}.")
    p.append("Tactics of this move (verified from the board): "
             + ("; ".join(motifs_played) if motifs_played else "nothing tactical — no captures won, "
                "no pieces left en prise, no new forks, pins or mate threats") + ".")

    before_w = _white_pov(a.eval_before_cp, a.eval_before_mate, mover_is_white)
    p.append(f"Evaluation: with best play {describe_eval(*before_w)}; after the move played, "
             f"{eval_after_words(a, mover_is_white)}. {side}'s winning chances went from "
             f"{a.win_before:.0f}% to {a.win_after:.0f}%.")

    if a.played_uci != a.best_uci and a.best_san:
        p.append(f"{side}'s best move according to the engine was {a.best_san} "
                 f"[{candidate_desc(board, a.best_san)}]"
                 + (f" — tactically it {'; '.join(motifs_best)}" if motifs_best else "") + ".")
        if a.mate_event == "missed_mate":
            p.append(f"{side} had a forced checkmate and missed it; "
                     + ("they are still winning." if a.win_after >= 80 else "the advantage is now much smaller."))
        elif a.mate_event == "allowed_mate":
            p.append(f"This move allows {opp} a forced checkmate.")
        elif a.cp_loss >= 30:
            p.append(f"The move concedes roughly {a.cp_loss / 100:.1f} pawns' worth of evaluation.")
    else:
        p.append(f"{a.played_san} was the engine's top choice for {side}.")

    if threat:
        p.append(f"MISSED THREAT (verified): before this move, {opp} was ALREADY threatening {threat}. "
                 f"{side}'s move did not deal with it, and {opp}'s punishing reply is exactly that threat. "
                 f"Make this the centre of the explanation: {side} needed to ask 'what does my opponent "
                 "threaten?' before moving.")
    if left_book:
        p.append(f"Opening theory (verified): this move {left_book}. Say what the deviation changes "
                 "compared with the usual moves.")
    gives_up = any(m.startswith(("puts the", "leaves the", "loses material")) for m in motifs_played or [])
    if gives_up and a.classification in ("best", "good"):
        p.append("The engine rates this move highly even though it gives up material: treat it as a "
                 "deliberate sacrifice and explain what it gains, using the engine lines.")
    if critical:
        p.append("This was a CRITICAL MOMENT: the best move was far stronger than any alternative, "
                 f"and {side} found it. Explain what it achieves that the alternatives don't, and give credit.")
    if previous and previous.get("cls") in ("mistake", "blunder"):
        p.append(f"{previous['side']}'s previous move ({previous['san']}) was a {previous['cls']}; "
                 f"the best move here ({a.best_san}) is how {side} could exploit it — say whether {side} did.")
    if time_note:
        p.append(f"Time context: {time_note} If a serious error was played very quickly or in time "
                 "trouble, recommend a habit (a blunder check of checks, captures and threats) rather "
                 "than deeper calculation.")

    if opening_name:
        p.append(f"Opening: {opening_name}. Use exactly this name and never guess a different "
                 "variation. Where relevant, relate the move to this opening's usual plans.")
    else:
        p.append("The opening has not been identified: do not name or guess any opening.")
    if recent:
        p.append(f"Moves just before this one: {recent}.")
    if plans:
        p.append("Plans so far (verified from the moves played):")
        p.extend("  " + ln for ln in plans)
    p.append(f"Pieces before the move — {piece_placement(board)}.")

    p.append(f"\n{side}'s engine candidate moves before this move, best first ([brackets] state what "
             "each move physically is — never contradict them):")
    for i, line in enumerate(a.candidates, 1):
        p.append(f"  {i}. {line.move_san} [{candidate_desc(board, line.move_san)}] — "
                 f"{line_eval_words(line, board)} — line: {side_line(board, line.pv_san)}"
                 + leads_to(board, line.pv_san))

    if a.refutation_san:
        p.append(f"\nRefutation — {opp}'s punishing reply: {side_line(after, a.refutation_san)}"
                 + leads_to(after, a.refutation_san))
        if motifs_reply:
            p.append(f"Tactics of {opp}'s {a.refutation_san[0]} (verified): {'; '.join(motifs_reply)}.")
    elif reply_line is not None:
        p.append(f"\nEngine's expected continuation: {side_line(after, reply_line.pv_san, 6)}"
                 + leads_to(after, reply_line.pv_san[:6]))
    if hindsight:
        p.append(f"What actually happened next in the game: {hindsight}.")

    p.append("\nPosition facts before the move:")
    p.extend("  " + ln for ln in f_before.summary_lines())
    changed = diff_lines(f_before, f_after)
    p.append("What the move changed:")
    p.extend("  " + ln for ln in (changed or ["nothing significant"]))

    if opening_lines:
        p.append("\nOpening book for this exact position (Lichess masters database):")
        p.extend("  " + ln for ln in opening_lines)
        p.append("If the move played differs from what masters play here, name the main book move and "
                 "explain in opening terms what the deviation gives up or invites.")
    if players:
        p.append("\nWhat players around this player's rating do in this exact position (Lichess database):")
        p.extend("  " + ln for ln in players)
        p.append("Use it for what typically goes wrong at this level here, and to compare the move played "
                 "with what peers and masters choose.")
    if tools:
        p.append(f"\nPosition before the move as FEN (only for the check_line tool): {board.fen()}")
        p.append(TOOL_HINT)

    p.append("")
    if note_type == "opponent":
        coached = coached_side.capitalize() if coached_side else opp
        p.append(f"This move was played by {side}, the opponent of {coached} (the player you coach). "
                 f"In 1-2 sentences tell {coached} what {side} was trying to do with it and what {side} "
                 "is aiming for next. It has already happened — use the past tense for any capture. "
                 "Name the colours explicitly.")
        p.append(SHORT_NOTE_FORMAT)
    elif note_type == "brief":
        p.append(f"This move was fine — do not critique it. In 1-3 sentences explain {side}'s intention: "
                 "what plan it advances, what it prepares, and what idea of the other side it prevents.")
        p.append(SHORT_NOTE_FORMAT)
    else:
        ideas_for = [ln.move_san for ln in a.candidates[:3] if ln.move_uci != a.played_uci]
        if want_ideas and ideas_for:
            ideas = ('[{"move": "<SAN>", "idea": "<one short sentence on what that move achieves for '
                     f'{side}>"}}]')
        else:
            ideas = "[]"
        p.append('Reply with a JSON object only, of this shape:\n{"explanation": "<2-7 sentences of flowing '
                 f"prose: lead with the verdict and the single most important reason, explain {side}'s "
                 'intention and, if a refutation is given, walk through why it punishes the move>", '
                 f'"line_ideas": {ideas}}}')
        if ideas != "[]":
            p.append(f"Write one line_ideas entry for each of: {', '.join(ideas_for)}. Describe the purpose "
                     f"of the candidate move itself, from {side}'s point of view (a pawn move develops no "
                     "piece); use its continuation only as supporting evidence.")
    return "\n".join(p)


SHORT_NOTE_FORMAT = 'Reply with a JSON object only: {"explanation": "<your note>", "line_ideas": []}'
TOOL_HINT = ("You can call check_line to have the engine test any variation from this position before you "
             "write. Do this for every concrete line you want to mention that is not already given above, and "
             "for any move you want to recommend instead of the engine's; never state what a line leads to "
             "without evidence.")


def leads_to(board: chess.Board, sans: list[str]) -> str:
    """' — leads to: <verified consequences>' for a line, or '' when nothing verifiable happens."""
    facts = line_consequences(board, sans)
    return f" — leads to: {facts}" if facts else ""


def build_ideas_prompt(candidates: list[Line], board: chess.Board, recent: str) -> str:
    """Minimal follow-up used when the main call failed to produce line ideas."""
    side = "White" if board.turn == chess.WHITE else "Black"
    parts = [
        f"Moves just played: {recent or '(game start)'}",
        f"Pieces — {piece_placement(board)}. {side} to move.",
        f"The lines below are candidate moves FOR {side}. For EACH one, state in one short sentence "
        f"what the candidate move ITSELF achieves for {side}. The [brackets] state exactly what the "
        'move physically is — never contradict them. Reply with a JSON object only: {"line_ideas": '
        '[{"move": "<SAN>", "idea": "<sentence>"}]}, one entry per candidate, nothing else.',
    ]
    for ln in candidates:
        parts.append(f"  {ln.move_san} [{candidate_desc(board, ln.move_san)}] — "
                     f"{line_eval_words(ln, board)} — line: {side_line(board, ln.pv_san)}"
                     + leads_to(board, ln.pv_san))
    return "\n".join(parts)


# --------------------------------------------------------------- commentary windows

@dataclass
class CommentaryMove:
    """Verified evidence for one move inside a commentary window."""
    label: str                     # "11. axb4" / "11... Qc7"
    side: str                      # "White" / "Black"
    san: str
    what: str                      # describe_move()
    verdict: str
    eval_after: str
    motifs: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    engine_next: str = ""          # labelled expected continuation
    best: str = ""                 # engine's preferred move when the played one was an error
    book: bool = False             # known opening theory
    threat: str = ""               # the error ignored this existing threat
    left_book: str = ""            # the move left opening theory


@dataclass
class CommentaryContext:
    start_label: str
    end_label: str
    eval_before: str
    pieces: str
    opening: str = ""
    recent: str = ""
    hindsight: str = ""
    plans: list[str] = field(default_factory=list)


def build_commentary_prompt(moves: list[CommentaryMove], ctx: CommentaryContext) -> str:
    """One request for a whole stretch of moves: commentator-style lines for each."""
    p = [f"COMMENTARY WINDOW: moves {ctx.start_label} to {ctx.end_label}."]
    if ctx.opening:
        p.append(f"Opening: {ctx.opening} (use exactly this name).")
    p.append(f"Before this stretch: {ctx.eval_before}. Pieces — {ctx.pieces}.")
    if ctx.recent:
        p.append(f"Moves just before this stretch: {ctx.recent}.")
    if ctx.plans:
        p.append("Plans so far (verified from the moves played):")
        p.extend("  " + ln for ln in ctx.plans)
    p.append("\nTHE MOVES (verified facts; [brackets] are exact descriptions — never contradict them):")
    for m in moves:
        bits = [f"{m.side}'s {m.what}", f"verdict: {m.verdict}" + (f" (engine preferred {m.best})" if m.best else "")]
        if m.book:
            bits.append("known opening theory")
        if m.left_book:
            bits.append(m.left_book)
        if m.threat:
            bits.append(f"ignored the threat {m.threat} that was already on the board")
        if m.motifs:
            bits.append("tactics: " + "; ".join(m.motifs))
        if m.changes:
            bits.append("changes: " + " ".join(m.changes[:3]))
        bits.append(f"afterwards {m.eval_after}")
        if m.engine_next:
            bits.append(f"engine expected: {m.engine_next}")
        p.append(f"- {m.label} ({m.side}) [{' | '.join(bits)}]")
    if ctx.hindsight:
        p.append(f"\nWhat actually happened after this stretch: {ctx.hindsight}.")
    p.append(
        "\nWrite a commentator's line for EACH move above, in order, and reply with a JSON object only:\n"
        '{"lines": [{"move": "<move label exactly as given, e.g. 11. axb4>", "comment": "<1-2 sentences>"}]}\n'
        "- Say what the move is trying to do and why now: the plan it belongs to (space, a pawn break, "
        "opening a file, bringing a piece to a wing, attacking the king, prophylaxis) and how it answers "
        "or continues the moves around it. Use the plans, the engine's expected continuation and what "
        "actually happened next as your evidence.\n"
        "- Name the side in every comment (White or Black).\n"
        "- For inaccuracies, mistakes and blunders, say briefly what the move allows.\n"
        "- Moves marked 'known opening theory': one short clause.\n"
        "One entry per move listed, in order, and nothing else.")
    return "\n".join(p)


_COMMENT_LINE = re.compile(
    r"^\s*(?:[-*•]\s*)?[*_]*(\d+)\s*(\.\.\.|…|\.\s*\.\.\.|\.)\s*([KQRBNa-hO][A-Za-z0-9+#=x-]*)[*_]*"
    r"\s*(?:\((?:white|black)\))?\s*[:—–-]\s*(.+)$", re.I)


def parse_commentary(text: str, moves: list[CommentaryMove]) -> dict[str, str]:
    """{move label: comment} from a commentary answer: the JSON shape ({"lines": [{move, comment}]}) or
    plain '11. axb4: ...' lines. Matches by number, side and SAN, tolerating format drift."""
    data = parse_json(text)
    if data is not None and isinstance(data.get("lines"), list):
        text = "\n".join(window_lines(data))
    by_key = {}
    for m in moves:
        n, dots = m.label.split(" ")[0].rstrip("."), "..." in m.label
        by_key[(n, dots, norm_san(m.san))] = m.label
    out: dict[str, str] = {}
    unmatched: list[str] = []
    for raw in text.splitlines():
        hit = _COMMENT_LINE.match(raw.strip())
        if not hit:
            continue
        num, dots, san, comment = hit.groups()
        key = (num, "." != dots.replace(" ", ""), norm_san(san))
        label = by_key.get(key) or next((lbl for (k_n, _, k_san), lbl in by_key.items()
                                         if k_n == num and k_san == norm_san(san)), None)
        if label and label not in out:
            out[label] = comment.strip()
        else:
            unmatched.append(comment.strip())
    return out


# --------------------------------------------------------------- chapters

@dataclass
class ChapterContext:
    number: int
    count: int
    start_label: str                 # "12. Nf3"
    end_label: str                   # "18... Qxb2"
    phases: str                      # "middlegame" / "opening and middlegame"
    eval_start: str                  # "White is slightly better"
    eval_end: str
    moves: str                       # labelled moves with gists (plans.with_gists)
    commentary: list[str] = field(default_factory=list)   # "12. White Nf3: ..." lines
    moments: list[str] = field(default_factory=list)      # errors, critical moments, missed threats
    plans: list[str] = field(default_factory=list)        # plan facts at the start
    opening: str = ""


def build_chapter_prompt(ctx: ChapterContext) -> str:
    """One chapter of the game (a phase or the stretch up to a turning point) → title + summary."""
    p = [f"CHAPTER {ctx.number} of {ctx.count}: moves {ctx.start_label} to {ctx.end_label} ({ctx.phases})."]
    if ctx.opening:
        p.append(f"Opening: {ctx.opening} (use exactly this name).")
    p.append(f"At the start of the chapter {ctx.eval_start}; at the end {ctx.eval_end}.")
    if ctx.plans:
        p.append("Plans at the start of the chapter (verified from the moves played):")
        p.extend("  " + ln for ln in ctx.plans)
    p.append(f"The moves, with what each did (verified): {ctx.moves}.")
    if ctx.moments:
        p.append("Key moments in this chapter (verified):")
        p.extend("  - " + m for m in ctx.moments)
    if ctx.commentary:
        p.append("Commentary already written for these moves (verified):")
        p.extend("  " + c for c in ctx.commentary)
    p.append(
        "\nSummarise this chapter for the player. Reply with a JSON object only:\n"
        '{"title": "<3-7 words naming what this chapter was about, e.g. Black\'s queenside pawn storm>", '
        '"summary": "<2-3 sentences: what each side was trying to do, how the balance changed, and the '
        'moment that decided the chapter>"}\n'
        "Name the side (White/Black) for every move you mention, mention only moves listed above, and "
        "write nothing else.")
    return "\n".join(p)


_TITLE = re.compile(r"^\s*[*_#\s]*TITLE\s*:?[*_]*\s*(.+)$", re.I | re.M)
_SUMMARY = re.compile(r"^\s*[*_#\s]*SUMMARY\s*:?[*_]*\s*(.+)", re.I | re.M | re.S)


def parse_chapter(text: str) -> tuple[str, str]:
    """(title, summary) from the JSON shape or the old TITLE:/SUMMARY: text; tolerant of markdown."""
    data = parse_json(text)
    if data is not None and ("title" in data or "summary" in data):
        title_text, summary_text = chapter_from(data)
        return title_text, " ".join(summary_text.split())
    title = _TITLE.search(text or "")
    summary = _SUMMARY.search(text or "")
    t = title.group(1).strip().strip("*_\"'") if title else ""
    s = summary.group(1).strip() if summary else ""
    if not s and text and not title:
        s = text.strip()
    return t[:80], " ".join(s.split())


# --------------------------------------------------------------- position

def build_position_prompt(board: chess.Board, lines: list[Line], features: list[str],
                          perspective: str, tools: bool = False) -> str:
    """Board-editor mode: assess a set-up position for one side."""
    side = "White" if board.turn == chess.WHITE else "Black"
    persp = perspective.capitalize()
    return "\n".join([
        f"Position: {side} to move. Pieces — {piece_placement(board)}.",
        f"You are coaching the {persp} player: assess the position from {persp}'s perspective — "
        "their plans, their problems, what they should aim for.",
        f"Engine candidate moves for {side} (to move), best first ([brackets] state what each move "
        "physically is — never contradict them):",
        *(f"  {i}. {ln.move_san} [{candidate_desc(board, ln.move_san)}] — {line_eval_words(ln, board)} "
          f"— line: {side_line(board, ln.pv_san)}" + leads_to(board, ln.pv_san) for i, ln in enumerate(lines, 1)),
        "Position facts (verified):",
        *(f"  {x}" for x in features),
        *([f"\nPosition as FEN (only for the check_line tool): {board.fen()}", TOOL_HINT] if tools else []),
        "\nReply with a JSON object only, of this shape:",
        '{"assessment": "<assessment from ' + persp + "'s perspective and the main plans, then why the engine's "
        'top move makes sense>", "line_ideas": [{"move": "<candidate SAN>", "idea": "<one short sentence on '
        f'what that line achieves for {side}>"}}]}}',
        "One line_ideas entry per candidate above.",
    ])


# --------------------------------------------------------------- review

GAME_REVIEW_SYSTEM = f"""You are a patient, honest chess coach giving a post-game review. \
You receive a VERIFIED record: each move with its engine verdict, the evaluation \
trajectory, accuracy scores, the opening, the biggest swings, verified facts (starting with \
the RESULT) and, when available, the commentary written for each stretch of the game.

Reply with a JSON object only, with exactly these fields (they become the review's sections):
"summary" — 2-4 sentences: the opening (name it), the result (exactly as the RESULT fact \
says), and who stood better when and why.
"story" — a list of 3-6 strings, one per phase or turning point, each starting with the move \
range (e.g. "Moves 8-12: White expands on the queenside with b4 and a3 while Black ..."). \
Tell the story of the plans each side pursued, how they collided, and where the balance shifted.
"went_well" — a list of 1-2 strings on what the player did well in THIS game, taken from \
the verified highlights (a strong move found at a critical moment, an opponent's error \
punished, an advantage converted), each naming the move or fact. Specific and brief: no \
flattery or empty praise. If the highlights give nothing, write one honest point.
"takeaways" — a list of 2-4 concrete, transferable lessons drawn from THIS game. Each one \
names the player's own move it comes from (e.g. "Move 31 (Qf5): ...") or the verified fact \
it rests on. These are the things to practise before the next game, and the most important \
part of the review.

Hard rules:
- Get the result right: say who won exactly as the RESULT fact states. Never say the \
player lost a game they won, or the reverse.
- Cite only moves and verdicts present in the record, and credit every move to the side \
that played it: "your" moves are the coached side's, never the opponent's. Call a move a \
mistake, blunder or inaccuracy only if the record says so.
- Do not invent tactics or lines.
- No generic advice that this game doesn't show (e.g. "study pawn structures", "practise trades").
- Mention endgames only of the kinds the verified facts say this game reached. If it never \
reached an endgame, give no endgame advice.
- Mention time trouble or time management only if the clock facts or the way the game ended \
show it.
- Always name the side (White/Black); if coaching one side you may also say "you".
- Explain in chess concepts; never mention centipawns (say "slightly worse", "winning").
- {BRITISH_ENGLISH}"""


def build_game_review_prompt(
    headers: dict,
    move_records: list[str],   # "9... Black b5 — mistake (best Na6); after it White is clearly better"
    opening_name: str,
    turning_points: list[str],
    side_filter: str | None,
    elo: int | None = None,
    time_class: str = "",
    player_context: str = "",
    accuracy: dict | None = None,
    commentary: list[str] | None = None,
    chapters: list[dict] | None = None,
    facts: list[str] | None = None,
    rating_label: str = "",
) -> str:
    parts: list[str] = []
    w, b = headers.get("White", "White"), headers.get("Black", "Black")
    parts.append(f"Game: {w} (White) vs {b} (Black), result {headers.get('Result', '?')}.")
    if opening_name:
        parts.append(f"Opening (identified from the opening database): {opening_name}. "
                     "Use EXACTLY this name; never substitute a different opening.")
    else:
        parts.append("The opening was NOT identified. Do not name or guess any opening — "
                     "refer to it only as 'the opening'.")
    if accuracy and any(v is not None for v in accuracy.values()):
        parts.append("Accuracy (0-100, Lichess method): " + ", ".join(
            f"{k.capitalize()} {v}%" for k, v in accuracy.items() if v is not None) + ".")
    if side_filter:
        parts.append(f"You are coaching {side_filter.capitalize()}. Focus the lessons on their play.")
    if time_class:
        parts.append(f"Time control: {time_class}. Judge decisions accordingly — fast games reward "
                     "practical choices and time management, not perfect play.")
    if elo:
        parts.append(f"The coached player is rated {rating_text(elo, rating_label)}; aim the takeaways at "
                     "what would take them to the next level. If the record shows big errors played in very few "
                     "seconds, make time discipline one of the takeaways.")
    if player_context:
        parts.append("Coach profile from their previous games: " + player_context +
                     "\nIf this game repeats known patterns, say so; if it shows improvement on a "
                     "known weakness, acknowledge it.")
    parts.append("\nMove-by-move record:")
    parts.extend("  " + r for r in move_records)
    if turning_points:
        parts.append("\nBiggest swings in winning chances (the turning points):")
        parts.extend("  " + t for t in turning_points)
    if facts:
        parts.append("\nOther verified facts:")
        parts.extend("  " + f for f in facts)
    if chapters:
        parts.append("\nThe game in chapters (verified summaries; base 'How the game unfolded' on these):")
        parts.extend(f"  Moves {c['range']} — {c['title']}: {c['summary']}" for c in chapters)
    elif commentary:
        parts.append("\nCommentary written during the analysis (verified against the board):")
        parts.extend("  " + c for c in commentary)
    parts.append("\nWrite the post-game review.")
    return "\n".join(parts)


# --------------------------------------------------------------- chat / profile

COACH_CHAT_SYSTEM = f"""You are a friendly chess coach chatting with an improving player \
about a game or position they are reviewing. Verified analysis context (engine lines, \
verdicts, positional facts) is provided below — treat it as ground truth.

Rules:
- Ground every tactical claim in the provided context; if the context doesn't cover the \
question, say so honestly and answer with general chess principles instead, clearly \
labelled as general advice.
- Never invent concrete moves or variations that are not in the context, and always say \
which side (White or Black) a move belongs to.
- Be concise and conversational. Explain in chess concepts, never centipawns.
- It's fine to answer general chess questions (openings, plans, rules of thumb).
- {BRITISH_ENGLISH}"""


# The coach's progress review has a fixed structure: an opening line, then these sections in this order
# (the overall review adds "By time control"). review_check.tidy_profile_review() enforces it.
PROFILE_SECTIONS = (
    ("time_controls", "⏱ By time control",
     "One bullet per time control in the statistics: how you do there in a few words, and how it compares."),
    ("weaknesses", "⚠ Holding you back",
     "2-3 bullets, the most persistent first: **the weakness in a few words** — the evidence (how often, "
     "in which games)."),
    ("strengths", "✓ Working well", "1-2 bullets, only what the statistics or reviews show."),
    ("openings", "♟ Openings",
     "1-2 bullets: the opening that goes best and the one that goes worst, with how many games."),
    ("train", "🎯 Train next", "2-3 concrete things to practise, one line each."),
)


def profile_sections(time_class: str | None = None) -> list[tuple[str, str, str]]:
    return [sec for sec in PROFILE_SECTIONS if not (time_class and sec[0] == "time_controls")]


def player_summary_system(time_class: str | None = None) -> str:
    """The coach's progress review: overall, or of one time control. Same fixed structure for both."""
    scope = f"in your {time_class} games" if time_class else "across your games"
    layout = "\n".join(f'"{key}" (shown under the heading "{title}"): a list of bullets — {what}'
                       for key, title, what in profile_sections(time_class))
    return f"""You are a chess coach writing a progress review FOR your student, addressed directly TO \
them: always "you" and "your", never their name or "the player".

Reply with a JSON object only. Its fields become the review's sections, in this order:
"intro": one or two sentences on where you stand {scope}. Don't repeat the numbers the player sees right \
above the review (games, record, accuracy, centipawn loss, blunders and mistakes per game).
{layout}

Rules:
- Bullets are one sentence, two at most. No filler, no repeating yourself.
- The app adds the headings: never put a heading, "###" or the section's name in a bullet.
- Never mention centipawns. Don't label the player's level ("beginner-level"); quote a rating only \
exactly as listed, if at all.
- Percentages from one or two games mean nothing: say "your one game in the Italian", not "won 100%".
- Ground every claim in the statistics and reviews given; never invent patterns or openings.
- {BRITISH_ENGLISH}
This text is also given to the coach in future analyses, so make every line carry information."""


PLAYER_SUMMARY_SYSTEM = player_summary_system()   # the overall review (kept for existing imports)


_TC_FOCUS = {
    "bullet": "In bullet, speed and pre-planned, safe moves matter most; deep calculation rarely fits.",
    "blitz": "In blitz, practical choices and time use matter as much as accuracy.",
    "rapid": "In rapid there is time to calculate: blunder checks, plans and technique count most.",
    "classical": "In classical there is time for full calculation, planning and endgame technique.",
    "daily": "In daily games there is time to analyse every move: accuracy and planning count most.",
}


def build_player_summary_prompt(stats: dict, reviews: list, profile: dict | None = None,
                                time_class: str | None = None) -> str:
    parts = []
    if time_class:
        parts.append(f"This review covers ONLY the player's {time_class} games ({stats.get('games', 0)} of them). "
                     f"{_TC_FOCUS.get(time_class, '')} Base every point on the {time_class} statistics and "
                     "reviews below, and say what to train for this time control.")
    if profile:
        elos = ratings.summary(profile.get("ratings") or {})
        parts.append(
            f"Player: {profile.get('name', 'the player')}. "
            + (f"Actual ratings: {elos}. " if elos else "")
            + (f"Self-described level: {profile.get('level')}. " if profile.get("level") else "")
            + "Use ONLY these ratings when referring to their strength — never estimate, invent, or "
              "upgrade a rating class. Opening names may come ONLY from the statistics below, never "
              "from memory of the reviews.")
    parts.append(f"Verified statistics across their analysed {time_class + ' ' if time_class else ''}games:")
    parts.append(f"  Games: {stats.get('games', 0)}, W-L-D: {stats.get('wins', 0)}-"
                 f"{stats.get('losses', 0)}-{stats.get('draws', 0)}")
    if stats.get("avg_accuracy") is not None:
        parts.append(f"  Average accuracy: {stats['avg_accuracy']}% (Lichess method; higher is better)")
    if stats.get("avg_acpl") is not None:
        parts.append(f"  Average centipawn loss: {stats['avg_acpl']} "
                     "(lower is better; ~20 strong club, ~80 beginner)")
    parts.append(f"  Blunders per game: {stats.get('blunders_per_game', '?')}, "
                 f"mistakes per game: {stats.get('mistakes_per_game', '?')}")
    for phase, acc in (stats.get("phase_accuracy") or {}).items():
        if acc is not None:
            parts.append(f"  Accuracy in the {phase}: {acc}%")
    for pattern in stats.get("patterns", []):
        parts.append(f"  Recurring mistake type: {pattern['label']} — {pattern['count']} times")
    for finding in stats.get("findings", []):
        parts.append(f"  Pattern across games ({finding['kind']}): {finding['text']}")
    for o in stats.get("openings", []):
        parts.append(f"  Opening: {o['name']} — {o['games']} games, "
                     f"{o['w']}W/{o['l']}L/{o['d']}D, avg cp loss {o['acpl']}")
    by_class = stats.get("by_time_class") or {}
    if len(by_class) >= 2:
        parts.append("By time control (verified). If they clearly differ, say how, in one or two sentences: "
                     "each time control also gets its own review.")
        for tc, r in by_class.items():
            parts.append(f"  {tc}: {r['games']} games, {r['wins']}W/{r['losses']}L/{r['draws']}D, "
                         + (f"accuracy {r['avg_accuracy']}%, " if r.get("avg_accuracy") is not None else "")
                         + f"{r['blunders_per_game']} blunders and {r['mistakes_per_game']} mistakes per game")
    parts.append(
        "\nRecent post-game reviews (verified, newest first). When you cite evidence, refer to the "
        "GAME it came from — e.g. \"your game against zztobias\" or \"as Black vs Infant001\" — "
        "NEVER by review number; the player cannot see these numbers.")
    for r in reviews:
        if isinstance(r, dict):
            side = (r.get("user_side") or "").capitalize()
            opp = r.get("black") if side == "White" else r.get("white")
            head = (f"game vs {opp}" + (f" (as {side}" if side else " (")
                    + f", {r.get('result', '')}"
                    + (f", {r.get('opening')}" if r.get("opening") else "") + ")")
            parts.append(f"--- {head} ---\n{r.get('review', '')}")
        else:
            parts.append(f"---\n{r}")
    parts.append("\nWrite the player profile.")
    return "\n".join(parts)


# --------------------------------------------------------------- parsing

_EXPL_HEAD = re.compile(r"^\s*[#*_\s]*EXPLANATION\s*:?[*_]*[ \t]*\n?", re.I)
_IDEAS_HEAD = re.compile(r"(?:^|\n)\s*[#*_\s]*LINE IDEAS\s*:?[*_]*[ \t]*\n?", re.I)
_IDEA_LINE = re.compile(r"\s*(?:[-*•]|\d+[.)])?\s*[*_`]*([KQRBNa-hO][A-Za-z0-9+#=x-]*?)[*_`]*\s*[:—–-]\s+(.+)")


def split_sections(text: str) -> tuple[str, dict[str, str]]:
    """Split LLM output into (explanation, {candidate_san: idea}).

    Models drift from the requested format, so this falls back gracefully and
    never loses content.
    """
    body = _EXPL_HEAD.sub("", text.strip(), count=1)
    parts = _IDEAS_HEAD.split(body, maxsplit=1)
    explanation = parts[0].strip()
    ideas: dict[str, str] = {}
    if len(parts) == 2:
        for ln in parts[1].splitlines():
            m = _IDEA_LINE.match(ln)
            if m:
                ideas[m.group(1).strip()] = m.group(2).strip()
    if not explanation and ideas:
        explanation = "See the engine lines below for the key ideas in this position."
    return explanation, ideas


def parse_note(text: str) -> tuple[str, dict[str, str]]:
    """(explanation, {move: idea}) from a note or position answer: the JSON shape first, the old
    EXPLANATION / LINE IDEAS text second."""
    data = parse_json(text)
    if data is not None and any(k in data for k in ("explanation", "assessment", "note", "line_ideas")):
        return note_from(data)
    return split_sections(text)


def parse_ideas(text: str) -> dict[str, str]:
    """{move: idea} from a line-ideas answer (JSON or '<SAN>: <idea>' lines)."""
    data = parse_json(text)
    if data is not None and "line_ideas" in data:
        return ideas_from(data)
    return split_sections("LINE IDEAS:\n" + text)[1]


def norm_san(san: str) -> str:
    """Loose SAN form for matching model output.

    Piece moves drop disambiguation and capture marks ('Nbxd7+' → 'Nd7'), but a
    pawn capture keeps its file ('exd5'), because 'bxa5' and the push 'a5' are
    different moves (possibly by different sides).
    """
    s = re.sub(r"[+#!?]", "", san).replace("=", "")
    if s.startswith("O-O"):
        return s
    m = re.match(r"([KQRBN])?([a-h])?[1-8]?(x)?([a-h][1-8])([QRBN])?$", s)
    if not m:
        return s
    piece, origin, capture, dest, promo = m.groups()
    if piece:
        return piece + dest + (promo or "")
    return (f"{origin}x" if capture and origin else "") + dest + (promo or "")


# --------------------------------------------------------------- fact-check

# Piece moves, pawn captures and castling are unambiguous move notation.
_SAN_TOKEN = re.compile(r"(?<![\w.-])(O-O-O|O-O|[KQRBN][a-h]?[1-8]?x?[a-h][1-8](?:=?[QRBN])?"
                        r"|[a-h]x[a-h][1-8](?:=?[QRBN])?)(?![\w-])")
# A bare square is a pawn move only in move context: "...a5", "12. c4", "play a5", "the a5 break".
_MOVE_VERB = (r"(?:play|plays|played|playing|push|pushes|pushed|pushing|prepar(?:e|es|ed|ing)(?:\s+to\s+play)?|"
              r"threaten(?:s|ed|ing)?(?:\s+to\s+play)?|answer(?:s|ed)?\s+with|repl(?:y|ies|ied)\s+with|"
              r"follow(?:s|ed)?\s+up\s+with|continu(?:e|es|ed)\s+with|"
              r"move\s+(?:is|was|(?:may|might|could|would|will)\s+be)(?:\s+to\s+play)?)")
_PAWN_MOVE = re.compile(
    rf"(?P<dots>\.\.\.|…)\s?(?P<b>[a-h][1-8](?:=?[QRBN])?)(?![\w-])"
    rf"|(?<![\w.])(?P<num>\d+)\.\s?(?P<w>[a-h][1-8](?:=?[QRBN])?)(?![\w-])"
    rf"|\b{_MOVE_VERB}\s+(?:the\s+)?(?P<v>[a-h][1-8](?:=?[QRBN])?)(?![\w-])"
    rf"|(?<![\w.-])(?P<s>[a-h][1-8])\s+(?:push|break|advance|thrust|lever)\b", re.I)
_DOTTED_SAN = re.compile(r"(\.\.\.|…|(?<![\w.])\d+\.)\s?(O-O-O|O-O|[KQRBN][a-h]?[1-8]?x?[a-h][1-8]|[a-h]x[a-h][1-8])")
_SIDE_WORD = re.compile(r"\b(white|black|you|your|yours|opponent)\b", re.I)
# Only definite references ("the/your/White's knight on d5") are claims about the board;
# "a knight on d5 would be strong" is a legitimate hypothetical and is not checked.
_PIECE_ON = re.compile(r"\b(the|your|their|his|her|white's|black's|white|black)\s+"
                       r"(king|queen|rook|bishop|knight|pawn)\s+(?:on|at)\s+([a-h][1-8])\b", re.I)
_CAPTURED = re.compile(
    r"\b(?P<owner>white's|black's|white|black|your|the|their|his|her)\s+(?P<piece>king|queen|rook|bishop|knight|pawn)"
    r"\s+(?:on|at|from)\s+(?P<sq>[a-h][1-8])\s+(?:has\s+been|had\s+been|was|is|gets|got)\s+"
    r"(?:captured|taken|won|lost|exchanged|traded)"
    r"|\b(?:captures|captured|takes|took|wins|won)\s+(?P<owner2>white's|black's|the|your|a)?\s*"
    r"(?P<piece2>king|queen|rook|bishop|knight|pawn)\s+(?:on|at)\s+(?P<sq2>[a-h][1-8])", re.I)
# Sentence breaks, but never inside move notation such as "12. Qc7" or "11... Qc7".
_SENTENCE = re.compile(r"(?<=[.!?])(?<!\d\.)(?<!\.\.)\s+(?=[A-Z0-9\"'(“])")
_PIECE_TYPES = {name: chess.PIECE_NAMES.index(name) for name in chess.PIECE_NAMES if name}


class EvidenceIndex:
    """Everything a note may legitimately mention, per side.

    Built from the positions before/after the move(s) being described and every
    line shown to the model (candidates, refutations, what happened next). A
    move token in the note must be playable by the side the sentence attributes
    it to; piece locations and captures must match some position or move shown.
    """

    def __init__(self, anchors: list[tuple[chess.Board, chess.Move]],
                 lines: list[tuple[chess.Board, list[str]]] = (), coached: chess.Color | None = None):
        self.coached = coached
        self.moves: dict[bool, set[str]] = {chess.WHITE: set(), chess.BLACK: set()}
        self.pieces: set[tuple[bool, int, int]] = set()
        self.captures: set[tuple[bool, int, int]] = set()
        self._seen: set[str] = set()
        for board, move in anchors:
            self._add_position(board)
            self._add_capture(board, move)
            after = board.copy(stack=False)
            after.push(move)
            self._add_position(after)
        for start, sans in lines:
            self._add_position(start)
            b = start.copy(stack=False)
            for san in sans:
                try:
                    mv = b.parse_san(san)
                except ValueError:
                    break
                self.moves[b.turn].add(norm_san(san))
                self._add_capture(b, mv)
                b.push(mv)
                self._add_position(b)

    @classmethod
    def for_move(cls, board: chess.Board, move: chess.Move, lines: list[tuple[chess.Board, list[str]]] = (),
                 coached: chess.Color | None = None) -> EvidenceIndex:
        return cls([(board, move)], lines, coached)

    def _add_position(self, b: chess.Board) -> None:
        key = b.fen()
        if key in self._seen:
            return
        self._seen.add(key)
        for sq, piece in b.piece_map().items():
            self.pieces.add((piece.color, piece.piece_type, sq))
        if not b.is_game_over():
            self.moves[b.turn].update(norm_san(b.san(mv)) for mv in b.legal_moves)

    def _add_capture(self, b: chess.Board, mv: chess.Move) -> None:
        if b.is_en_passant(mv):
            sq = mv.to_square + (-8 if b.turn == chess.WHITE else 8)
            self.captures.add((not b.turn, chess.PAWN, sq))
        elif b.is_capture(mv):
            self.captures.add((not b.turn, b.piece_type_at(mv.to_square), mv.to_square))

    def _side_of(self, word: str) -> chess.Color | None:
        w = word.lower()
        if w == "white":
            return chess.WHITE
        if w == "black":
            return chess.BLACK
        if self.coached is None:
            return None
        return self.coached if w in ("you", "your", "yours") else not self.coached

    def _sentence_problems(self, sentence: str) -> list[str]:
        issues: list[str] = []
        mentions = [(m.start(), self._side_of(m.group(1))) for m in _SIDE_WORD.finditer(sentence)]

        def side_near(pos: int) -> chess.Color | None:
            before = [s for p, s in mentions if p < pos]
            if before:
                return before[-1]
            after = [s for p, s in mentions if p > pos]
            return after[0] if after else None

        def check(token: str, side: chess.Color | None) -> None:
            key = norm_san(token)
            sides = (side,) if side is not None else (chess.WHITE, chess.BLACK)
            if not any(key in self.moves[s] for s in sides):
                who = f"{'White' if side == chess.WHITE else 'Black'} can play" if side is not None else "can be played"
                issues.append(f"'{token}' is not a move {who} in any position or line shown")

        dotted = {}
        for m in _DOTTED_SAN.finditer(sentence):
            dotted[m.start(2)] = chess.BLACK if m.group(1) in ("...", "…") else chess.WHITE
        checked = set()
        for m in _SAN_TOKEN.finditer(sentence):
            checked.add(m.start(1))
            check(m.group(1), dotted.get(m.start(1), side_near(m.start(1))))
        for m in _DOTTED_SAN.finditer(sentence):      # "...bxa4", "12. exd5": the dots hide the token above
            if m.start(2) not in checked:
                check(m.group(2), dotted[m.start(2)])
        for m in _PAWN_MOVE.finditer(sentence):
            if m.group("b"):
                check(m.group("b"), chess.BLACK)
            elif m.group("w"):
                check(m.group("w"), chess.WHITE)
            else:
                token = m.group("v") or m.group("s")
                check(token, side_near(m.start()))
        for owner, name, sq in _PIECE_ON.findall(sentence):
            ptype, square = _PIECE_TYPES[name.lower()], chess.parse_square(sq.lower())
            side = self._side_of(owner.removesuffix("'s")) if owner.lower() not in ("the", "their", "his", "her") \
                else None
            sides = (side,) if side is not None else (chess.WHITE, chess.BLACK)
            if not any((c, ptype, square) in self.pieces for c in sides):
                who = f"{'White' if side == chess.WHITE else 'Black'}'s " if side is not None else ""
                issues.append(f"there is no {who}{name.lower()} on {sq.lower()} in any position shown")
        for m in _CAPTURED.finditer(sentence):
            owner = (m.group("owner") or m.group("owner2") or "").lower().removesuffix("'s")
            name = (m.group("piece") or m.group("piece2")).lower()
            square = chess.parse_square((m.group("sq") or m.group("sq2")).lower())
            side = self._side_of(owner) if owner in ("white", "black", "your") else None
            sides = (side,) if side is not None else (chess.WHITE, chess.BLACK)
            if not any((c, _PIECE_TYPES[name], square) in self.captures for c in sides):
                who = f"{'White' if side == chess.WHITE else 'Black'}'s " if side is not None else "the "
                issues.append(f"{who}{name} on {chess.square_name(square)} is not captured in any move shown "
                              "(check which piece captured and which was captured)")
        return issues

    def problems(self, text: str) -> list[str]:
        """Claims in `text` that the evidence does not support."""
        out: list[str] = []
        for sentence in _SENTENCE.split(text):
            for issue in self._sentence_problems(sentence):
                if issue not in out:
                    out.append(issue)
        return out

    def clean(self, text: str) -> tuple[str, int]:
        """Drop sentences that still contain unsupported claims. Returns (text, sentences removed)."""
        kept, removed = [], 0
        for sentence in _SENTENCE.split(text.strip()):
            if self._sentence_problems(sentence):
                removed += 1
            else:
                kept.append(sentence)
        return " ".join(kept).strip(), removed


# --------------------------------------------------------------- assessment check

# Evaluation tiers, matching scoring.describe_eval: 0 equal, 1 a slight edge, 2 clearly better, 3 much
# better, 4 winning, 5 completely winning, 6 a forced mate — for the side that is better (None when equal).
def eval_tier(cp: int | None, mate: int | None) -> tuple[chess.Color | None, int]:
    if mate is not None:
        return (chess.WHITE if mate > 0 else chess.BLACK), 6
    cp = cp or 0
    a = abs(cp)
    tier = 0 if a < 25 else 1 if a < 70 else 2 if a < 150 else 3 if a < 300 else 4 if a < 800 else 5
    return (None if tier == 0 else chess.WHITE if cp > 0 else chess.BLACK), tier


_SIDE = r"(?:white|black|you)"
# A side word, then (without another side word in between) the assessment it is given.
_ASSESSED = re.compile(
    rf"\b(?P<side>{_SIDE})(?:'s)?\b(?:(?!\b{_SIDE}\b)[^.;,!?]){{0,45}}?"
    r"\b(?P<claim>completely winning|totally winning|crushing|overwhelming(?:ly better)?|"
    r"winning(?!\s+(?:chances|a|an|the|back|material|more|some|his|her|their|its|your|white's|black's|"
    r"pawn|piece|rook|knight|bishop|queen|exchange|position\s+for|attempt|try)\b)|"
    r"(?:in\s+a\s+)?(?:lost|hopeless)(?!\s+(?:a|an|the|on|material|time|his|her|their|its|your|some|all|"
    r"control|track|pawn|piece|rook|knight|bishop|queen|exchange|tempo|tempi|ground|momentum)\b)|"
    r"losing(?!\s+(?:a|an|the|material|on|time|his|her|their|its|your|some|all|control|ground|pawn|piece|"
    r"rook|knight|bishop|queen|exchange|tempo|tempi|battle|fight|track|momentum)\b)|"
    r"decisive advantage|decisively better|"
    r"clear(?:ly)?\s+(?:better|ahead|on\s+top)|(?:a\s+)?clear\s+(?:advantage|edge|plus)|much better|"
    r"(?:a\s+)?(?:big|large|significant|considerable|substantial|serious|strong|healthy)\s+(?:advantage|edge)|"
    r"comfortably better|"
    r"slight(?:ly)?\s+(?:better|ahead|worse)|(?:a\s+)?(?:slight|small|tiny|modest|minimal|nagging|minor)\s+"
    r"(?:advantage|edge|plus|pull)|a\s+(?:bit|little|touch|shade)\s+(?:better|worse)|somewhat\s+(?:better|worse)|"
    r"marginally\s+(?:better|worse)|an?\s+edge|"
    r"better(?!\s+(?:placed|developed|coordinated|off|than|at|in|to|for|prepared|positioned|protected|"
    r"defended|centralised|centralized|supported|organised|organized|mobilised|mobilized|chances|version|"
    r"squares?|bishop|knight|rook|queen|king|pawns?|structure|pieces?|move|moves|option|options|idea|plan|"
    r"choice|alternative|try|way|here\s+was|would\s+have\s+been)\b)|"
    r"(?:much|clearly|far)\s+worse|worse(?!\s+(?:off|than|placed|version|bishop|knight|rook|queen|king|pawns?|"
    r"structure|piece|pieces|squares?)\b)|"
    r"(?:roughly|about|approximately|dynamically|objectively|essentially|basically|still|fully)?\s*"
    r"(?:equal(?!\s+(?:material|trade|trades|exchange|chances|terms|footing|pieces|number|share|value)\b)|"
    r"level(?!\s+(?:of|material|pieces)\b)|balanced(?!\s+(?:approach|position\s+for|development)\b))|"
    r"(?:is|are|was|were|has|have|had|stands?|stood)\s+(?:now\s+|still\s+|clearly\s+)?(?:ahead|on\s+top|"
    r"in\s+the\s+driving\s+seat|in\s+(?:serious|deep|big|real)\s+trouble|"
    r"(?:the\s+)?upper\s+hand|the\s+advantage|a\s+decisive\s+advantage|a\s+winning\s+(?:position|advantage|"
    r"attack|endgame|ending)|a\s+lost\s+(?:position|game|endgame|ending)|a\s+forced\s+(?:check)?mate|"
    r"(?:check)?mate\s+in\s+\d+))\b", re.I)
# "a winning position for White", "an edge for Black", "a clear advantage to White".
_FOR_SIDE = re.compile(
    r"\b(?P<claim>(?:completely\s+)?winning|lost|decisive|clear(?:ly)?|slight|small|big|large|significant|"
    r"an?\s+edge|equal|level|balanced|better|worse|much better|comfortable)"
    r"\s*(?:position|advantage|edge|game|ending|endgame|chances)?\s+(?:for|to)\s+(?P<side>" + _SIDE + r")\b", re.I)
_NEGATED = re.compile(r"\b(?:not|never|no\s+longer|hardly|far\s+from|isn't|aren't|wasn't|weren't|nowhere\s+near)"
                      r"\s+(?:\w+\s+){0,2}$", re.I)
_POSITION_EQUAL = re.compile(
    r"\b(?:the\s+)?(?:position|game|struggle|fight|battle|ending|endgame|middlegame)\s+(?:is|was|remains?|remained|"
    r"stays?|stayed|looks?|seems?)\s+(?:now\s+|still\s+|roughly\s+|about\s+|dynamically\s+|objectively\s+|"
    r"basically\s+|essentially\s+)?(?:equal|level|balanced|even|unclear|a\s+draw|drawn|drawish|dead\s+equal)\b",
    re.I)


class AssessmentIndex:
    """The evaluations a note may claim: who is better, and by how much.

    Built from every evaluation the model was shown (with best play, after the move, each candidate,
    the lines a tool checked). A sentence such as "White is winning" is a problem when none of them has
    White at least "much better". The check is deliberately tolerant: "better" matches any edge,
    "roughly equal" matches a slight edge, and anything negated ("not winning") or hypothetical in a
    way the evidence covers is left alone. It catches the costly error — telling a player they stood
    well when they were lost, or the reverse — not shades of wording.
    """

    def __init__(self, coached: chess.Color | None = None):
        self.coached = coached
        self.evals: list[tuple[str, chess.Color | None, int]] = []     # (label, side, tier)
        self.words: list[str] = []

    def add(self, label: str, cp: int | None, mate: int | None) -> AssessmentIndex:
        """Record an evaluation from WHITE's point of view (the convention of describe_eval)."""
        side, tier = eval_tier(cp, mate)
        self.evals.append((label, side, tier))
        words = describe_eval(cp, mate)
        if f"{label}: {words}" not in self.words:
            self.words.append(f"{label}: {words}")
        return self

    def add_result(self, label: str, winner: chess.Color | None) -> AssessmentIndex:
        """The game ended: `winner` has 'won', None is a draw (treated as equal)."""
        if winner is None:
            self.evals.append((label, None, 0))
            self.words.append(f"{label}: the game ended in a draw")
        else:
            self.evals.append((label, winner, 6))
            self.words.append(f"{label}: {'White' if winner == chess.WHITE else 'Black'} won")
        return self

    def _side(self, word: str) -> chess.Color | None:
        w = word.lower().removesuffix("'s")
        if w == "white":
            return chess.WHITE
        if w == "black":
            return chess.BLACK
        return self.coached

    def _ok(self, side: chess.Color | None, claim: str) -> bool:
        c = " ".join(claim.lower().split())
        tiers = [t for _, s, t in self.evals if s == side]
        other = [t for _, s, t in self.evals if s is not None and s != side]
        equalish = any(t <= 1 for _, _, t in self.evals)
        if re.search(r"completely|totally|crushing|overwhelming|mate", c):
            return any(t >= 4 for t in tiers)
        if re.search(r"lost|hopeless|losing|worse|trouble", c):
            if re.search(r"slight|bit|little|touch|shade|somewhat|marginally", c):
                return any(1 <= t <= 2 for t in other)
            if re.search(r"much|clearly|far|lost|hopeless|losing|trouble", c):
                return any(t >= 2 for t in other) if "worse" in c or "trouble" in c else any(t >= 3 for t in other)
            return any(t >= 1 for t in other)
        if re.search(r"winning|decisive", c):
            return any(t >= 3 for t in tiers)
        if re.search(r"clear|much|big|large|significant|considerable|substantial|serious|strong|healthy|"
                     r"comfortabl|driving seat", c):
            return any(t >= 2 for t in tiers)
        if re.search(r"slight|small|tiny|modest|minimal|nagging|minor|bit|little|touch|shade|somewhat|"
                     r"marginally|edge|pull", c):
            return any(1 <= t <= 2 for t in tiers)
        if re.search(r"equal|level|balanced", c):
            return equalish
        return any(t >= 1 for t in tiers)          # better / ahead / on top / the advantage / upper hand

    def _sentence_problems(self, sentence: str) -> list[str]:
        out: list[str] = []
        if not self.evals:
            return out

        def flag(claim: str) -> None:
            if self.words and claim not in out:
                out.append(f"\"{claim.strip()}\" contradicts the engine's evaluation "
                           f"(the evidence says — {'; '.join(self.words[:4])})")

        for m in _ASSESSED.finditer(sentence):
            if _NEGATED.search(sentence[:m.start("claim")]):
                continue
            side = self._side(m.group("side"))
            if side is None and not re.search(r"equal|level|balanced", m.group("claim"), re.I):
                continue                        # "you" with nobody coached: can't tell whom it means
            if not self._ok(side, m.group("claim")):
                flag(sentence[m.start():m.end()])
        for m in _FOR_SIDE.finditer(sentence):
            if _NEGATED.search(sentence[:m.start()]):
                continue
            side = self._side(m.group("side"))
            if side is not None and not self._ok(side, m.group("claim")):
                flag(sentence[m.start():m.end()])
        for m in _POSITION_EQUAL.finditer(sentence):
            if not _NEGATED.search(sentence[:m.start()]) and not self._ok(None, "equal"):
                flag(sentence[m.start():m.end()])
        return out

    def problems(self, text: str) -> list[str]:
        out: list[str] = []
        for sentence in _SENTENCE.split(text or ""):
            for issue in self._sentence_problems(sentence):
                if issue not in out:
                    out.append(issue)
        return out

    def clean(self, text: str) -> tuple[str, int]:
        """Drop sentences whose assessment contradicts the evaluations. Returns (text, sentences removed)."""
        kept, removed = [], 0
        for sentence in _SENTENCE.split((text or "").strip()):
            if self._sentence_problems(sentence):
                removed += 1
            else:
                kept.append(sentence)
        return " ".join(kept).strip(), removed


def correction_prompt(problems: list[str]) -> str:
    return ("Your answer contains claims that do not match the verified evidence:\n"
            + "\n".join(f"- {p}" for p in problems)
            + "\nRewrite your answer in the same format. Check which side each move belongs to, and use "
              "only moves, pieces and squares from the evidence.")


def time_note(side: str, think_s: float | None, clock_s: float | None, time_class: str) -> str:
    if think_s is None:
        return ""
    note = f"{side} spent {think_s:.0f} seconds on this move"
    if clock_s is not None:
        s = int(clock_s)
        note += f" ({s // 60}:{s % 60:02d} remaining)"
    if time_class:
        note += f", in a {time_class} game"
    return note + "."


__all__ = [
    "COACH_CHAT_SYSTEM", "COACH_RULES", "GAME_REVIEW_SYSTEM", "LEVEL_NOTES", "PLAYER_SUMMARY_SYSTEM",
    "PROFILE_SECTIONS", "TOOL_HINT", "player_summary_system", "profile_sections",
    "AssessmentIndex", "CommentaryContext", "CommentaryMove", "EvidenceIndex", "build_commentary_prompt",
    "build_game_review_prompt", "build_ideas_prompt", "build_move_prompt", "build_player_summary_prompt",
    "build_position_prompt", "build_system_prompt", "candidate_desc", "correction_prompt", "eval_after_words",
    "eval_tier", "leads_to", "norm_san", "parse_chapter", "parse_commentary", "parse_ideas", "parse_note",
    "side_line", "split_sections", "time_note",
]
