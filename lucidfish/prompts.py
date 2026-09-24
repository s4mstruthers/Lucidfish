"""Prompt construction, output parsing, and fact-checking of LLM notes.

Design rule: the LLM NEVER analyses the position itself. It receives verified
evidence — engine lines, winning-chance changes, board-verified tactics,
positional facts, opening statistics — and narrates it in chess concepts.

Prompts are split so that everything constant for a game (coaching rules,
player level, coach profile) sits in the system prompt. Local servers such as
Ollama and cloud APIs with prompt caching can then reuse that prefix instead
of re-reading it for every move, which is a large speed-up on local models.
"""

from __future__ import annotations

import re

import chess

from .engine import Line, MoveAnalysis, describe_move
from .features import PositionFeatures, diff_lines, piece_placement
from .scoring import describe_eval

LEVEL_NOTES = {
    "beginner": "a beginner — explain fundamentals plainly (hanging pieces, one-move threats, "
                "development, king safety) and avoid long lines",
    "casual": "a casual player — fundamentals plus simple plans, lines of 2-3 moves",
    "club": "a club player — concrete plans and tactics up to 2-3 moves deep",
    "advanced": "an advanced player — be concrete and positional; deeper lines are fine",
}

COACH_RULES = """You are a patient, precise chess coach. Each request gives you VERIFIED \
evidence about one move: engine lines and evaluations, tactics computed from the board, \
and positional facts. Your job is to explain that evidence in chess concepts — you never \
analyse the position yourself.

Hard rules:
- Use only claims present in the evidence. Never invent tactics, threats, moves or lines.
- The evidence states which piece moved, what it captured, and where every piece stands. \
Never contradict it, and never mention a piece on a square where the evidence has none.
- Any move you name must appear in the evidence (the move played, a candidate, or a line).
- If you are unsure whether something is true, leave it out.
- Explain with concepts (development, tempo, king safety, pawn structure, piece activity, \
weak squares, forks, pins) and translate evaluations into words.
- Never mention centipawns or numbers like +1.3; say "slightly better", "winning", \
"about a pawn's worth".
- Be concise: 2-4 sentences for normal moves, up to 7 for mistakes and critical moments.
- Always answer in exactly the format requested."""


def elo_guidance(elo: int | None) -> str:
    if not elo:
        return ""
    return (f"The player is rated about {elo}. At lower ratings favour fundamentals (hanging pieces, "
            "one-move threats, development, king safety) and keep lines to 2-3 moves; at higher "
            "ratings be more concrete and positional. Aim each lesson at what would take this "
            "player to the next level.")


def build_system_prompt(coached_side: str | None = None, elo: int | None = None,
                        level: str | None = None, player_context: str = "") -> str:
    """Per-game system prompt: identical for every move, so it can be prefix-cached."""
    parts = [COACH_RULES, "\nCoaching context for this game:"]
    if coached_side:
        side = coached_side.capitalize()
        parts.append(f"- You are coaching {side}; 'you' always means {side}. In numbered lines, "
                     "moves after 'N.' are White's and moves after 'N...' are Black's.")
    else:
        parts.append("- You are coaching both players; refer to them as White and Black. In numbered "
                     "lines, moves after 'N.' are White's and moves after 'N...' are Black's.")
    if level and level.lower() in LEVEL_NOTES:
        parts.append(f"- The player is {LEVEL_NOTES[level.lower()]}.")
    if elo:
        parts.append("- " + elo_guidance(elo))
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
    game_so_far: str = "",
    time_note: str = "",
    previous: dict | None = None,       # {"san","cls","side"} of the previous (opponent) move
    want_ideas: bool = False,
) -> str:
    """Assemble the grounded evidence block the LLM narrates from."""
    mover_is_white = board.turn == chess.WHITE
    side = "White" if mover_is_white else "Black"
    opp = "Black" if mover_is_white else "White"
    a = analysis
    after = board.copy(stack=False)
    after.push(move)
    p: list[str] = []

    verdict = a.classification + (" — a CRITICAL MOMENT" if critical else "")
    p.append(f"MOVE: {move_label(board, a.played_san)} by {side}. Engine verdict: {verdict}.")
    p.append(f"What physically happened: {describe_move(board, move)}.")
    p.append("Tactics of this move (verified from the board): "
             + ("; ".join(motifs_played) if motifs_played else "nothing tactical — no captures won, "
                "no pieces left en prise, no new forks, pins or mate threats") + ".")

    before_w = _white_pov(a.eval_before_cp, a.eval_before_mate, mover_is_white)
    after_w = _white_pov(a.eval_after_cp, a.eval_after_mate, mover_is_white)
    if a.game_result:
        after_text = "the game is over (" + ("checkmate" if a.mate_event == "delivered_mate" else "a draw") + ")"
    else:
        after_text = describe_eval(*after_w)
    p.append(f"Evaluation: with best play {describe_eval(*before_w)}; after the move played, "
             f"{after_text}. {side}'s winning chances went from {a.win_before:.0f}% to {a.win_after:.0f}%.")

    if a.played_uci != a.best_uci and a.best_san:
        best_desc = candidate_desc(board, a.best_san)
        p.append(f"The engine's best move was {a.best_san} [{best_desc}]"
                 + (f" — tactically it {'; '.join(motifs_best)}" if motifs_best else "") + ".")
        if a.mate_event == "missed_mate":
            p.append(f"{side} had a forced checkmate and missed it; "
                     + ("they are still winning." if a.win_after >= 80 else "the advantage is now much smaller."))
        elif a.mate_event == "allowed_mate":
            p.append(f"This move allows {opp} a forced checkmate.")
        elif a.cp_loss >= 30:
            p.append(f"The move concedes roughly {a.cp_loss / 100:.1f} pawns' worth of evaluation.")
    else:
        p.append(f"{a.played_san} was the engine's top choice.")

    gives_up = any(m.startswith(("puts the", "leaves the", "loses material")) for m in motifs_played or [])
    if gives_up and a.classification in ("best", "good"):
        p.append("The engine rates this move highly even though it gives up material: treat it as a "
                 "deliberate sacrifice and explain what it gains, using the engine lines.")
    if critical:
        p.append("This was a CRITICAL MOMENT: the best move was far stronger than any alternative, "
                 "and the player found it. Explain what it achieves that the alternatives don't, and "
                 "give credit.")
    if previous and previous.get("cls") in ("mistake", "blunder"):
        p.append(f"{previous['side']}'s previous move ({previous['san']}) was a {previous['cls']}; "
                 f"the best move here ({a.best_san}) is how to exploit it — say whether {side} did.")
    if time_note:
        p.append(f"Time context: {time_note} If a serious error was played very quickly or in time "
                 "trouble, recommend a habit (a blunder check of checks, captures and threats) rather "
                 "than deeper calculation.")

    if opening_name:
        p.append(f"Opening: {opening_name}. Use exactly this name and never guess a different "
                 "variation. Where relevant, relate the move to this opening's usual plans (development "
                 "scheme, pawn breaks, where each side castles).")
    else:
        p.append("The opening has not been identified: do not name or guess any opening.")
    if game_so_far:
        p.append(f"Game so far: {game_so_far}")
    p.append(f"Pieces before the move — {piece_placement(board)}.")

    p.append("\nEngine candidate moves before this move, best first ([brackets] state what each move "
             "physically is — never contradict them):")
    for i, line in enumerate(a.candidates, 1):
        p.append(f"  {i}. {line.move_san} [{candidate_desc(board, line.move_san)}] — "
                 f"{line_eval_words(line, board)} — line: {line.pv_text}")

    if a.refutation_text:
        p.append(f"\nRefutation — {opp}'s punishing reply: {a.refutation_text}")
        if motifs_reply:
            p.append(f"Tactics of {a.refutation_san[0]} (verified): {'; '.join(motifs_reply)}.")
    elif note_type == "opponent" and reply_line is not None:
        p.append(f"\nEngine's best reply for {opp}: {reply_line.move_san} "
                 f"[{candidate_desc(after, reply_line.move_san)}] — line: {reply_line.pv_text}")

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

    p.append("")
    if note_type == "opponent":
        coached = coached_side.capitalize() if coached_side else opp
        p.append(f"You are coaching {coached}; this move was played by their OPPONENT. Do not critique "
                 f"it as if it were {coached}'s move. In 1-2 sentences tell {coached} what it "
                 "accomplished (it has already happened — use the past tense for any capture) and what "
                 f"the opponent threatens or plans next, based only on the tactics and lines above.")
        p.append("Reply in EXACTLY this format:\nEXPLANATION:\n<your note>")
    elif note_type == "brief":
        p.append("This move was fine — do not critique it. In 1-3 sentences give the big picture: what "
                 "plan it advances, what it prepares, and what opponent idea it prevents.")
        p.append("Reply in EXACTLY this format:\nEXPLANATION:\n<your note>")
    else:
        p.append("Reply in EXACTLY this format:\nEXPLANATION:\n<flowing prose: lead with the verdict and "
                 "the single most important reason; if a refutation is given, walk through why it "
                 "punishes the move>")
        ideas_for = [ln.move_san for ln in a.candidates[:3] if ln.move_uci != a.played_uci]
        if want_ideas and ideas_for:
            p.append("LINE IDEAS:\n<SAN>: <one short sentence on what that move achieves for "
                     f"{side}>\nWrite one line for each of: {', '.join(ideas_for)}. Describe the purpose "
                     f"of the candidate move itself, from {side}'s point of view (a pawn move develops no "
                     "piece); use its continuation only as supporting evidence.")
    return "\n".join(p)


def build_ideas_prompt(candidates: list[Line], board: chess.Board, game_so_far: str) -> str:
    """Minimal follow-up used when the main call failed to produce line ideas."""
    side = "White" if board.turn == chess.WHITE else "Black"
    parts = [
        f"Game so far: {game_so_far}",
        f"Pieces — {piece_placement(board)}. {side} to move.",
        f"The lines below are candidate moves FOR {side}. For EACH one, state in one short sentence "
        f"what the candidate move ITSELF achieves for {side}. The [brackets] state exactly what the "
        "move physically is — never contradict them. Reply with EXACTLY one line per candidate in the "
        "format '<SAN>: <sentence>' and nothing else.",
    ]
    for ln in candidates:
        parts.append(f"  {ln.move_san} [{candidate_desc(board, ln.move_san)}] — "
                     f"{line_eval_words(ln, board)} — line: {ln.pv_text}")
    return "\n".join(parts)


# --------------------------------------------------------------- position

def build_position_prompt(board: chess.Board, lines: list[Line], features: list[str],
                          perspective: str) -> str:
    """Board-editor mode: assess a set-up position for one side."""
    side = "White" if board.turn == chess.WHITE else "Black"
    persp = perspective.capitalize()
    return "\n".join([
        f"Position: {side} to move. Pieces — {piece_placement(board)}.",
        f"You are coaching the {persp} player: assess the position from {persp}'s perspective — "
        "their plans, their problems, what they should aim for.",
        "Engine candidate moves for the side to move, best first ([brackets] state what each move "
        "physically is — never contradict them):",
        *(f"  {i}. {ln.move_san} [{candidate_desc(board, ln.move_san)}] — {line_eval_words(ln, board)} "
          f"— line: {ln.pv_text}" for i, ln in enumerate(lines, 1)),
        "Position facts (verified):",
        *(f"  {x}" for x in features),
        "\nReply in EXACTLY this format, both sections mandatory:",
        "EXPLANATION:",
        f"<assessment from {persp}'s perspective and the main plans, then why the engine's top move makes sense>",
        "LINE IDEAS:",
        f"<candidate SAN>: <one short sentence on what that line achieves for {side}>",
        "(one such line per candidate above)",
    ])


# --------------------------------------------------------------- review

GAME_REVIEW_SYSTEM = """You are a patient chess coach giving a post-game review. You \
receive a VERIFIED move-by-move record: each move with its engine verdict, the \
evaluation trajectory, accuracy scores, the opening identification, and the biggest swings.

Write a SHORT review with exactly these two sections (markdown headers). Detailed \
per-move commentary lives elsewhere in the app — do NOT walk through the game move \
by move here.
## Summary — 3-5 sentences: the opening (name it and say how well it was handled) \
and the overall shape of the game — who stood better when and why, citing at most \
the 2-3 decisive move numbers.
## Key takeaways — 2-4 concrete, transferable lessons drawn from the mistakes in \
THIS game, as bullet points each starting with '- ' (e.g. "- you traded a developed \
piece for tempo twice; ask what the capture concedes before taking"). These are the \
things to practise before the next game.

Hard rules:
- Cite only moves and verdicts present in the record. Do not invent tactics or lines.
- Explain in chess concepts; never mention centipawns (say "slightly worse", "winning").
- If coaching one side, address them as "you" and focus lessons on their moves."""


def build_game_review_prompt(
    headers: dict,
    move_records: list[str],   # "9... b5 — mistake (best Na6); after it White is clearly better"
    opening_name: str,
    turning_points: list[str],
    side_filter: str | None,
    elo: int | None = None,
    time_class: str = "",
    player_context: str = "",
    accuracy: dict | None = None,
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
        parts.append(f"You are coaching {side_filter.capitalize()}. Focus the review and lessons on their play.")
    if time_class:
        parts.append(f"Time control: {time_class}. Judge decisions accordingly — fast games reward "
                     "practical choices and time management, not perfect play.")
    if elo:
        parts.append(f"The coached player is rated about {elo}; aim the takeaways at what would take "
                     "them to the next level. If the record shows big errors played in very few "
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
    parts.append("\nWrite the post-game review.")
    return "\n".join(parts)


# --------------------------------------------------------------- chat / profile

COACH_CHAT_SYSTEM = """You are a friendly chess coach chatting with an improving player \
about a game or position they are reviewing. Verified analysis context (engine lines, \
verdicts, positional facts) is provided below — treat it as ground truth.

Rules:
- Ground every tactical claim in the provided context; if the context doesn't cover the \
question, say so honestly and answer with general chess principles instead, clearly \
labelled as general advice.
- Never invent concrete moves or variations that are not in the context.
- Be concise and conversational. Explain in chess concepts, never centipawns.
- It's fine to answer general chess questions (openings, plans, rules of thumb)."""


PLAYER_SUMMARY_SYSTEM = """You are a chess coach writing a progress review FOR your \
student, addressed directly TO them. Always say "you" and "your" — never their name in \
third person, never "the player". Write a compact review (150-250 words) covering: your \
typical openings and how you score in them, your 2-4 most persistent weaknesses (be \
specific: piece-hanging, time trouble, bad trades, king safety, specific openings), any \
clear strengths, and what to train next. Ground every claim in the statistics and \
reviews given — do not invent patterns. This text is also fed to future coaching \
sessions as context, so make every sentence carry information."""


def build_player_summary_prompt(stats: dict, reviews: list, profile: dict | None = None) -> str:
    parts = []
    if profile:
        elos = ", ".join(f"{k.replace('elo_', '')} {v}" for k, v in profile.items()
                         if k.startswith("elo_") and v)
        parts.append(
            f"Player: {profile.get('name', 'the player')}. "
            + (f"Actual ratings: {elos}. " if elos else "")
            + (f"Self-described level: {profile.get('level')}. " if profile.get("level") else "")
            + "Use ONLY these ratings when referring to their strength — never estimate, invent, or "
              "upgrade a rating class. Opening names may come ONLY from the statistics below, never "
              "from memory of the reviews.")
    parts.append("Verified statistics across analysed games:")
    parts.append(f"  Games: {stats.get('games', 0)}, W-L-D: {stats.get('wins', 0)}-"
                 f"{stats.get('losses', 0)}-{stats.get('draws', 0)}")
    if stats.get("avg_accuracy") is not None:
        parts.append(f"  Average accuracy: {stats['avg_accuracy']}% (Lichess method; higher is better)")
    if stats.get("avg_acpl") is not None:
        parts.append(f"  Average centipawn loss: {stats['avg_acpl']} "
                     "(lower is better; ~20 strong club, ~80 beginner)")
    parts.append(f"  Blunders per game: {stats.get('blunders_per_game', '?')}, "
                 f"mistakes per game: {stats.get('mistakes_per_game', '?')}")
    for o in stats.get("openings", []):
        parts.append(f"  Opening: {o['name']} — {o['games']} games, "
                     f"{o['w']}W/{o['l']}L/{o['d']}D, avg cp loss {o['acpl']}")
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


def norm_san(san: str) -> str:
    """Loose SAN form for matching model output ('Nbxd7+' → 'Nd7', 'exd5' → 'd5')."""
    s = re.sub(r"[+#!?]", "", san).replace("=", "")
    if s.startswith("O-O"):
        return s
    m = re.match(r"([KQRBN])?[a-h]?[1-8]?x?([a-h][1-8])([QRBN])?$", s)
    if not m:
        return s
    return (m.group(1) or "") + m.group(2) + (m.group(3) or "")


# --------------------------------------------------------------- fact-check

_SAN_TOKEN = re.compile(r"(?<![\w-])(O-O-O|O-O|[KQRBN][a-h]?[1-8]?x?[a-h][1-8]|[a-h]x[a-h][1-8](?:=?[QRBN])?)(?![\w-])")
# Only definite references ("the/your/White's knight on d5") are claims about the board;
# "a knight on d5 would be strong" is a legitimate hypothetical and is not checked.
_PIECE_ON = re.compile(r"\b(the|your|their|his|her|white's|black's|white|black)\s+"
                       r"(king|queen|rook|bishop|knight|pawn)\s+(?:on|at)\s+([a-h][1-8])\b", re.I)
_PIECE_TYPES = {name: chess.PIECE_NAMES.index(name) for name in chess.PIECE_NAMES if name}


class EvidenceIndex:
    """Everything a note may legitimately mention: moves and piece placements
    from the positions before/after the move and along every line shown."""

    def __init__(self, board: chess.Board, move: chess.Move, lines: list[tuple[chess.Board, list[str]]]):
        self.moves: set[str] = set()
        self.pieces: set[tuple[bool, int, int]] = set()
        after = board.copy(stack=False)
        after.push(move)
        self._add_position(board)
        self._add_position(after)
        for start, sans in lines:
            b = start.copy(stack=False)
            for san in sans:
                try:
                    mv = b.parse_san(san)
                except ValueError:
                    break
                self.moves.add(norm_san(san))
                b.push(mv)
                self._add_position(b)

    def _add_position(self, b: chess.Board) -> None:
        for sq, piece in b.piece_map().items():
            self.pieces.add((piece.color, piece.piece_type, sq))
        if not b.is_game_over():
            self.moves.update(norm_san(b.san(mv)) for mv in b.legal_moves)

    def problems(self, text: str) -> list[str]:
        """Claims in `text` that the evidence does not support."""
        issues = []
        for tok in dict.fromkeys(_SAN_TOKEN.findall(text)):
            if norm_san(tok) not in self.moves:
                issues.append(f"'{tok}' is not a legal move here and appears in none of the given lines")
        for owner, name, sq in _PIECE_ON.findall(text):
            ptype, square = _PIECE_TYPES[name.lower()], chess.parse_square(sq.lower())
            owner = owner.lower().removesuffix("'s")
            colors = (owner == "white",) if owner in ("white", "black") else (True, False)
            if not any((c, ptype, square) in self.pieces for c in colors):
                who = f"{owner.capitalize()} " if owner in ("white", "black") else ""
                issues.append(f"there is no {who}{name.lower()} on {sq.lower()} in any position shown")
        return issues


def correction_prompt(problems: list[str]) -> str:
    return ("Your answer contains claims that do not match the verified evidence:\n"
            + "\n".join(f"- {p}" for p in problems)
            + "\nRewrite your answer in the same format, using only moves, pieces and squares from the "
              "evidence.")


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


def game_so_far_text(root: chess.Board, sans: list[str]) -> str:
    """Numbered movetext of the game up to (not including) the current move."""
    from .engine import numbered_line
    return numbered_line(root, sans) if sans else "(game start)"


__all__ = [
    "COACH_CHAT_SYSTEM", "COACH_RULES", "GAME_REVIEW_SYSTEM", "LEVEL_NOTES", "PLAYER_SUMMARY_SYSTEM",
    "EvidenceIndex", "build_game_review_prompt", "build_ideas_prompt", "build_move_prompt",
    "build_player_summary_prompt", "build_position_prompt", "build_system_prompt", "candidate_desc",
    "correction_prompt", "game_so_far_text", "norm_san", "split_sections", "time_note",
]
