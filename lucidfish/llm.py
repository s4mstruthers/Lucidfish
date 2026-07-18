"""LLM layer: a provider interface + local Ollama implementation.

Design rule: the LLM NEVER analyses the position itself. It receives verified
evidence (engine lines, eval swings, refutations, extracted features, opening
stats) and its only job is to narrate that evidence in chess concepts.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod

import chess
import requests

from .config import LLMConfig


def _candidate_desc(fen: str, san: str) -> str:
    """Verified physical description of a candidate move in the given position."""
    from .engine import describe_move  # local import to avoid a cycle at module load
    try:
        board = chess.Board(fen)
        return describe_move(board, board.parse_san(san))
    except Exception:
        return ""

SYSTEM_PROMPT = """You are a patient chess coach. You will receive VERIFIED engine \
analysis and positional facts about one move in a game. Explain the move to an \
improving club player.

Hard rules:
- Only use the tactical/positional claims given in the evidence. Do not invent \
tactics, threats, or lines that are not listed.
- The evidence states exactly which piece moved and what (if anything) it captured. \
Never contradict this or invent captures, hanging pieces, or piece locations.
- If you are unsure whether something is true of the position, leave it out.
- Explain in chess CONCEPTS (development, tempo, king safety, pawn structure, \
piece activity, weak squares), translating evals into plain language.
- If a refutation line is given, walk through WHY it punishes the move.
- If master-game statistics are given, relate the move to known opening plans.
- Be concise: 2-5 sentences for normal moves, up to 8 for blunders/critical moments.
- Never mention centipawns; say things like "slightly better", "winning", "losing a pawn's worth of position"."""


GAME_REVIEW_SYSTEM = """You are a patient chess coach giving a post-game review. You \
receive a VERIFIED move-by-move record: each move with its engine verdict, the \
evaluation trajectory, the opening identification, and the biggest evaluation swings.

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


COACH_CHAT_SYSTEM = """You are a friendly chess coach chatting with an improving club \
player about a game or position they are reviewing. Verified analysis context (engine \
lines, verdicts, positional facts) is provided below — treat it as ground truth.

Rules:
- Ground every tactical claim in the provided context; if the context doesn't cover \
the question, say so honestly and answer with general chess principles instead, \
clearly labelled as general advice.
- Be concise and conversational. Explain in chess concepts, never centipawns.
- It's fine to answer general chess questions (openings, plans, rules of thumb)."""


class LLMProvider(ABC):
    @abstractmethod
    def generate(self, system: str, prompt: str) -> str: ...

    @abstractmethod
    def chat(self, system: str, messages: list[dict]) -> str: ...


class OllamaProvider(LLMProvider):
    """Local models via Ollama's /api/chat endpoint."""

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg

    def chat(self, system: str, messages: list[dict]) -> str:
        try:
            r = requests.post(
                f"{self.cfg.base_url}/api/chat",
                json={
                    "model": self.cfg.model,
                    "messages": [{"role": "system", "content": system}, *messages],
                    "stream": False,
                    "options": {"temperature": self.cfg.temperature},
                },
                timeout=self.cfg.timeout_s,
            )
            r.raise_for_status()
            return r.json()["message"]["content"].strip()
        except requests.ConnectionError as e:
            raise RuntimeError(
                f"Cannot reach Ollama at {self.cfg.base_url}. Is it running? "
                "Start it with `ollama serve` and pull a model with `ollama pull qwen2.5:14b`."
            ) from e

    def generate(self, system: str, prompt: str) -> str:
        return self.chat(system, [{"role": "user", "content": prompt}])


def build_prompt(
    move_number: int,
    side: str,
    analysis,          # engine.MoveAnalysis
    features_before,   # features.PositionFeatures
    features_after,    # features.PositionFeatures
    opening_lines: list[str],
    move_desc: str = "",
    game_so_far: str = "",
    critical: bool = False,
    brief: bool = False,        # quiet good move: short big-picture note, not forensics
    opponent_of: str | None = None,  # set when this is the OPPONENT's move: intent note only
    opening_name: str = "",     # identified opening — carried through the whole game
    time_note: str = "",        # "Black spent 2 seconds on this move (0:41 remaining)..."
    elo: int | None = None,     # player rating — tailors depth and lesson targeting
    player_context: str = "",   # profile summary from previous games
) -> str:
    """Assemble the grounded evidence block the LLM narrates from."""
    parts: list[str] = []
    parts.append(f"Move {move_number} by {side}: {analysis.played_san} "
                 f"(engine verdict: {analysis.classification}).")
    if critical:
        parts.append(
            "This was a CRITICAL MOMENT: the best move here was far stronger than any "
            "alternative, and the player found it (or came close). Explain what this move "
            "achieves that the alternatives don't, how it changes the course of the game "
            "given the play so far, and give credit where due."
        )
    if move_desc:
        parts.append(f"What physically happened: {move_desc}")
    if time_note:
        parts.append(
            f"Time context: {time_note} If a serious error was played very quickly or in "
            "deep time trouble, factor that into your coaching — recommend a habit (like a "
            "pre-move blunder check on checks, captures, and threats) rather than expecting "
            "long calculation."
        )
    if player_context:
        parts.append(
            "What you know about this player from their previous games (their coach "
            "profile): " + player_context + "\nWhen this move repeats one of their known "
            "recurring issues, point out the pattern explicitly — that connection is more "
            "valuable than the move itself."
        )
    if elo:
        parts.append(
            f"The player is rated about {elo}. Pitch your explanation for that level: at "
            "lower ratings favour fundamentals (hanging pieces, one-move threats, development, "
            "king safety) and keep lines to 2-3 moves; at higher ratings be more concrete and "
            "positional. Aim the lesson at what would take this player to the next level."
        )
    if game_so_far:
        parts.append(f"Game so far: {game_so_far}")
    if opening_name:
        parts.append(
            f"Opening as of this move: {opening_name} (identified per-position from the "
            "Lichess masters database; the name refines as the variation develops). Use "
            "EXACTLY this name — never invent, rename, or guess a different variation. Where "
            "relevant, relate your explanation to this opening's typical plans — its usual "
            "development scheme, pawn breaks, and where each side castles — and say whether "
            "this move fits or abandons those plans."
        )
    else:
        parts.append(
            "The opening has NOT been identified. Do not name, guess, or refer to any "
            "specific opening — discuss only what is on the board."
        )
    parts.append(f"Position before (FEN): {analysis.fen_before}")

    parts.append(f"\nPerspective: you are coaching {side}. 'You' always means {side}. "
                 f"In the numbered lines below, moves after 'N.' are White's and moves "
                 f"after 'N...' are Black's.")

    parts.append("\nEngine candidate moves before this move (best first). The [brackets] "
                 "state what each move physically is — never contradict them:")
    for i, line in enumerate(analysis.candidates, 1):
        desc = _candidate_desc(analysis.fen_before, line.move_san)
        parts.append(f"  {i}. {line.move_san} [{desc}]  eval {line.score_str}  line: {line.pv_text}")

    if analysis.played_san != analysis.best_san:
        loss = ("this allows a forced mate" if analysis.cp_loss >= 90000
                else f"this concedes roughly {analysis.cp_loss / 100:.1f} pawns of evaluation")
        parts.append(f"\nPlayed {analysis.played_san} instead of best move {analysis.best_san}; {loss}.")
    else:
        parts.append(f"\n{analysis.played_san} was the engine's top choice.")

    if analysis.refutation_text:
        parts.append("Refutation (opponent's punishing line): " + analysis.refutation_text)

    parts.append("\nPosition facts BEFORE the move:")
    parts.extend("  " + l for l in features_before.summary_lines())
    parts.append("Position facts AFTER the move:")
    parts.extend("  " + l for l in features_after.summary_lines())

    if opening_lines:
        parts.append("\nOpening book for this exact position (Lichess masters database):")
        parts.extend("  " + l for l in opening_lines)
        parts.append(
            "If the played move differs from what masters play here, name the main book "
            "move and explain in opening terms what the deviation gives up or invites."
        )

    if opponent_of:
        parts.append(
            f"\nYou are coaching {opponent_of}, and this move was played by their OPPONENT. "
            f"Do not coach or critique the opponent. In 1-2 sentences, tell {opponent_of} "
            "what this move accomplished and what the opponent likely intends NEXT — the plan "
            "it advances (development, castling preparation, pressure on a square/piece) and "
            f"any concrete threat {opponent_of} should be alert to.\n"
            "The 'What physically happened' line above is ground truth and has ALREADY "
            "happened: if it says a piece was captured, the capture is complete — describe it "
            "in past tense, never as an attempt or plan. Base the 'what's next' part only on "
            "the engine lines given.\n"
            "Reply in EXACTLY this format:\n"
            "EXPLANATION:\n<your 1-2 sentence note>"
        )
        return "\n".join(parts)
    if brief:
        parts.append(
            "\nThis move was FINE — do not critique it. In 1-3 sentences, give the "
            "big picture: what plan does this move advance, what is it preparing, and "
            "what opponent idea does it prevent or discourage, given the game so far."
        )
    parts.append(
        "\nReply in EXACTLY this format, both sections mandatory:\n"
        "EXPLANATION:\n"
        "<your explanation as flowing prose, per the length rules — lead with the verdict "
        "and the single most important reason>\n"
        "LINE IDEAS:\n"
        "<candidate SAN>: <one short sentence on the plan behind that line>\n"
        "(one such line for each of the top 3 candidate moves listed above, EXCLUDING "
        "the move that was actually played — that one is covered by your explanation)\n"
        f"Each idea must be written from {side}'s point of view — these are {side}'s "
        f"candidate moves, so say what the line achieves FOR {side} (development, defence, "
        f"counterplay, trades that help them), never what the opponent intends. "
        "The idea sentence must describe the purpose of the candidate move ITSELF — the "
        "bracketed description states exactly what it is (a pawn move develops no piece!). "
        "Use the continuation only as supporting evidence for that purpose. Only mention "
        "pieces and squares that actually appear on the board or in that line's moves."
    )
    return "\n".join(parts)


PLAYER_SUMMARY_SYSTEM = """You are a chess coach writing a progress review FOR your \
student, addressed directly TO them. Always say "you" and "your" — never their name in \
third person, never "the player". Write a compact review (150-250 words) covering: your \
typical openings and how you score in them, your 2-4 most persistent weaknesses (be \
specific: piece-hanging, time trouble, bad trades, king safety, specific openings), any \
clear strengths, and what to train next. Ground every claim in the statistics and \
reviews given — do not invent patterns. This text is also fed to future coaching \
sessions as context, so make every sentence carry information."""


def build_player_summary_prompt(stats: dict, reviews: list[str], profile: dict | None = None) -> str:
    parts = []
    if profile:
        elos = ", ".join(f"{k.replace('elo_', '')} {v}" for k, v in profile.items()
                         if k.startswith("elo_") and v)
        parts.append(
            f"Player: {profile.get('name', 'the player')}. "
            + (f"Actual chess.com ratings: {elos}. " if elos else "")
            + (f"Self-described level: {profile.get('level')}. " if profile.get("level") else "")
            + "Use ONLY these ratings when referring to their strength — never estimate, "
              "invent, or upgrade a rating class. Opening names may come ONLY from the "
              "statistics below, never from memory of the reviews."
        )
    parts.append("Verified statistics across analyzed games:")
    parts.append(f"  Games: {stats.get('games', 0)}, W-L-D: {stats.get('wins', 0)}-"
                 f"{stats.get('losses', 0)}-{stats.get('draws', 0)}")
    if stats.get("avg_acpl") is not None:
        parts.append(f"  Average centipawn loss: {stats['avg_acpl']} "
                     f"(lower is better; ~20 strong club, ~80 beginner)")
    parts.append(f"  Blunders per game: {stats.get('blunders_per_game', '?')}, "
                 f"mistakes per game: {stats.get('mistakes_per_game', '?')}")
    for o in stats.get("openings", []):
        parts.append(f"  Opening: {o['name']} — {o['games']} games, "
                     f"{o['w']}W/{o['l']}L/{o['d']}D, avg cp loss {o['acpl']}")
    parts.append(
        "\nRecent post-game reviews (verified, newest first). When you cite evidence, "
        "refer to the GAME it came from — e.g. \"your game against zztobias\" or \"as "
        "Black vs Infant001\" — NEVER by review number; the player cannot see these "
        "numbers.")
    for r in reviews:
        if isinstance(r, dict):
            side = (r.get("user_side") or "").capitalize()
            opp = r.get("black") if side == "White" else r.get("white")
            head = (f"game vs {opp}" + (f" (as {side}" if side else "(")
                    + f", {r.get('result', '')}"
                    + (f", {r.get('opening')}" if r.get("opening") else "") + ")")
            parts.append(f"--- {head} ---\n{r.get('review', '')}")
        else:
            parts.append(f"---\n{r}")
    parts.append("\nWrite the player profile.")
    return "\n".join(parts)


def build_ideas_prompt(candidates, game_so_far: str, fen: str, side: str) -> str:
    """Minimal follow-up used when the main call failed to produce line ideas."""
    parts = [
        f"Game so far: {game_so_far}",
        f"Current position (FEN): {fen}",
        f"The lines below are candidate moves FOR {side}. For EACH one, state in one short "
        f"sentence what the candidate move ITSELF achieves for {side}, from {side}'s point "
        "of view, never the opponent's plans. The [brackets] state exactly what the move "
        "physically is — never contradict them (a pawn move develops no piece). Use the "
        "continuation only as supporting evidence. Only mention pieces and squares present "
        "on the board or in that line. Reply with EXACTLY one line per candidate in the "
        "format '<SAN>: <sentence>' and nothing else.",
    ]
    for l in candidates:
        desc = _candidate_desc(fen, l.move_san)
        parts.append(f"  {l.move_san} [{desc}]  eval {l.score_str}  line: {l.pv_text}")
    return "\n".join(parts)


def build_game_review_prompt(
    headers: dict,
    move_records: list[str],   # one compact line per move: "9... b5 — mistake (best Na6), eval +3.53"
    opening_name: str,
    turning_points: list[str],
    side_filter: str | None,
    elo: int | None = None,
    time_class: str = "",
    player_context: str = "",
) -> str:
    parts: list[str] = []
    w, b = headers.get("White", "White"), headers.get("Black", "Black")
    parts.append(f"Game: {w} vs {b}, result {headers.get('Result', '?')}.")
    if opening_name:
        parts.append(f"Opening (identified from the opening database): {opening_name}. "
                     "Use EXACTLY this name; never substitute a different opening.")
    else:
        parts.append("The opening was NOT identified. Do not name or guess any opening — "
                     "refer to it only as 'the opening'.")
    if side_filter:
        parts.append(f"You are coaching {side_filter.capitalize()}. Focus the review and lessons on their play.")
    if time_class:
        parts.append(f"Time control: {time_class}. Judge decisions accordingly — fast games "
                     "reward practical choices and time management, not perfect play.")
    if elo:
        parts.append(f"The coached player is rated about {elo}; aim the takeaways at what "
                     "would take them to the next level. If the record shows big errors "
                     "played in very few seconds, make time discipline one of the takeaways.")
    if player_context:
        parts.append("Coach profile from their previous games: " + player_context +
                     "\nIf this game repeats known patterns, say so; if it shows improvement "
                     "on a known weakness, acknowledge it.")

    parts.append("\nMove-by-move record (evals are from White's perspective):")
    parts.extend("  " + r for r in move_records)

    if turning_points:
        parts.append("\nBiggest evaluation swings (the turning points):")
        parts.extend("  " + t for t in turning_points)

    parts.append("\nWrite the post-game review.")
    return "\n".join(parts)
