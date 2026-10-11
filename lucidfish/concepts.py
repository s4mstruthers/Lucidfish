"""The Learn section: chess concepts, explained, and tied to the player's own games.

Lucidfish's analysis already *names* what went wrong in a move (a missed
threat, hanging material, a fork the player did not see, a king left in the
open, poor endgame technique, leaving opening theory). This module turns those
names into something to learn from: a short, plain explanation of each concept,
why it matters, how to recognise it at the board, and the question to ask
yourself so you stop making the same mistake, with your own puzzles and games
as the examples.

Everything here is written by hand and verified by the analysis that links to
it; the AI coach is only used when the player asks a question about a concept.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import store, training

CATEGORIES = [
    ("thinking", "Thinking habits", "The questions to ask before every move."),
    ("tactics", "Tactics", "Short sequences that win material or mate."),
    ("safety", "Threats and safety", "Seeing what the opponent wants before it happens."),
    ("strategy", "Strategy and plans", "What to aim for when there is nothing to calculate."),
    ("phases", "The three phases", "What matters in the opening, the middlegame and the endgame."),
    ("practical", "Practical play", "Clocks, nerves and converting what you have."),
]


@dataclass(frozen=True)
class Concept:
    id: str
    title: str
    category: str
    summary: str          # one sentence: what it is
    why: str              # why it decides games
    spot: str             # how to recognise it at the board
    do: str               # what to do about it / the habit to build
    question: str         # the self-check question, short enough to remember
    themes: tuple[str, ...] = ()     # training themes (training.THEMES) that are this concept
    tags: tuple[str, ...] = ()       # move tags from the analysis
    patterns: tuple[str, ...] = ()   # insights.PATTERNS ids
    findings: tuple[str, ...] = ()   # insights.findings ids
    phase: str = ""                  # opening / middlegame / endgame
    related: tuple[str, ...] = field(default=())


CONCEPTS: list[Concept] = [
    # ---------------------------------------------------------------- thinking habits
    Concept(
        "checks-captures-threats", "Checks, captures, threats", "thinking",
        "Before every move, look at every check, every capture and every threat, for both sides.",
        "Most lost games below master level are decided by a tactic that was there to be seen. The player did not "
        "calculate badly; they never looked. A fixed scan is what makes you look, every move, even when tired.",
        "There is nothing to spot: this is the habit that finds the other concepts. The signs you need it are blunders "
        "that look obvious a second later, and moves played in a few seconds in a sharp position.",
        "Run the scan in that order, for the opponent first (what can they do to me?) and then for yourself. Only then "
        "think about plans. It takes ten seconds once it is a habit and it removes most one-move blunders.",
        "What are all the checks, captures and threats, for them and for me?",
        themes=("hanging", "missed_tactic", "missed_threat"), patterns=("rushed",),
        related=("what-does-it-threaten", "hanging-pieces"),
    ),
    Concept(
        "what-does-it-threaten", "What does their last move threaten?", "safety",
        "Every opponent move changes something. Find out what it now attacks, uncovers or prepares before you reply.",
        "A threat you did not see is the most common reason a reasonable-looking move turns out to be a blunder. The "
        "analysis marks these as *missed threats*: the opponent was already threatening the very move that "
        "punished you.",
        "Look at the piece that moved: what does it attack now? Then look at the line it left: which of their pieces "
        "now sees further? Pawn moves that open a diagonal for a bishop or a file for a rook are the easiest to miss.",
        "Answer the threat first, or make sure your move answers it as a side effect. If the threat is real and your "
        "planned move ignores it, your planned move is wrong, however good it looks.",
        "What does their last move attack, uncover or prepare?",
        themes=("missed_threat",), tags=("missed threat",), patterns=("missed_threat",), findings=("threats",),
        related=("checks-captures-threats", "hanging-pieces"),
    ),
    Concept(
        "candidate-moves", "Candidate moves", "thinking",
        "Pick two or three moves worth considering before you calculate any of them deeply.",
        "Calculating the first move you see, finding it works, and playing it is how good moves get overlooked. A "
        "position where only one move holds (the analysis marks these *critical moments*) is where candidates "
        "matter most.",
        "You notice you are checking one line again and again, or that you played a move you had not compared with "
        "anything. In critical positions, the engine shows a big gap between the best move and the second best.",
        "Write the short list first (mentally: 'Nf5, Rd1 or h4'), then compare them. Ask what the opponent's best "
        "reply is to each. Only then choose. When the position is quiet, two candidates are enough.",
        "Which two or three moves deserve a look here?",
        related=("checks-captures-threats", "critical-moments"),
    ),
    Concept(
        "critical-moments", "Critical moments", "thinking",
        "A few positions in every game decide it: the ones where only one move keeps the balance or the advantage.",
        "Playing 35 decent moves and one careless one at the critical moment loses the game. Learning to feel when a "
        "position is critical tells you when to slow down and spend your time.",
        "Tension that can be resolved (pieces that can be exchanged, pawns that can be pushed or captured), a king "
        "under pressure, or a chance to grab or sacrifice material. The analysis marks them with a lightning bolt.",
        "Slow down. Use the checks-captures-threats scan and list candidates. Spend the time here, not on the move "
        "order of a quiet manoeuvre, and make sure the move you choose works against their best reply.",
        "Is this one of the moments that decides the game?",
        related=("candidate-moves", "clock-management"),
    ),
    # ---------------------------------------------------------------- tactics
    Concept(
        "hanging-pieces", "Hanging pieces", "tactics",
        "A piece is hanging when it can be taken for free, or for less than it is worth, after all the recaptures.",
        "Giving away a piece is the single most common cause of a lost game. The analysis checks every move with a "
        "static exchange evaluation (who captures what, in what order) and marks the result *hangs material*.",
        "Count attackers and defenders of every piece that just moved or was just attacked. A piece defended once but "
        "attacked twice is as good as undefended. Watch for pieces that stop defending something when they move.",
        "Before you move a piece, check what it was protecting. After the opponent's move, check what they now attack. "
        "If you cannot count the exchange in your head, take a moment: it is nearly always worth it.",
        "Is anything of mine undefended or defended less than it is attacked?",
        themes=("hanging",), tags=("hangs material",), patterns=("hanging",),
        related=("checks-captures-threats", "counting-exchanges"),
    ),
    Concept(
        "counting-exchanges", "Counting exchanges", "tactics",
        "When several pieces can capture on one square, work out the sequence of captures and who comes out ahead.",
        "Many 'hanging piece' blunders are really miscounted exchanges: the piece looked defended, but the defender "
        "was worth more than the attacker, or a pin stopped it from recapturing.",
        "A square attacked and defended by several pieces, especially when the attackers are worth less than the "
        "defenders (a pawn attacking a knight is a threat even if the knight is defended).",
        "List attackers and defenders by value, cheapest first. Play the captures out in order and stop whenever the "
        "side to move would lose by continuing. The piece values (1, 3, 3, 5, 9) are enough for this.",
        "If they capture here, and I recapture, and they recapture... who is ahead at the end?",
        themes=("hanging",), tags=("hangs material", "wins material"),
        related=("hanging-pieces", "forks"),
    ),
    Concept(
        "forks", "Forks", "tactics",
        "One piece attacks two or more targets at once, so only one can be saved.",
        "Forks win material from nothing: no sacrifice, no long calculation. Knights are the usual culprits because "
        "their attacks are hard to see, but pawns, queens and even kings fork.",
        "Two of your pieces on squares a knight could reach in one hop, or on the same diagonal, rank or file with "
        "nothing between them. A loose piece next to your king is a fork waiting to happen (king plus piece).",
        "After each opponent move, ask which of your pieces are *loose* (undefended). Two loose pieces are a target. "
        "Keep pieces defended or out of a knight's range, and look for forks of your own when their pieces are loose.",
        "Can one of their pieces attack two of mine at once? Can one of mine attack two of theirs?",
        themes=("missed_tactic",), tags=("fork",),
        related=("hanging-pieces", "pins-and-skewers"),
    ),
    Concept(
        "pins-and-skewers", "Pins and skewers", "tactics",
        "A pin stops a piece from moving because something more valuable stands behind it; a skewer attacks the "
        "valuable piece first and wins what is behind it.",
        "A pinned piece is a weak piece: it cannot defend, and anything attacking it attacks it in effect twice. Many "
        "'inexplicable' lost pawns and pieces trace back to a pin the player forgot about.",
        "Three pieces on one line: attacker, piece in front, piece behind. Bishops on long diagonals and rooks on "
        "open files create them. Your own king behind a knight on the same diagonal as their bishop is the classic.",
        "Count a pinned piece as not defending anything. Break pins early (move the king, block, or chase the pinning "
        "piece) before the opponent adds a second attacker. Look for lines through their king and queen.",
        "Is any piece of mine pinned, so that it is really not defending what it seems to defend?",
        themes=("missed_tactic",), tags=("pin", "skewer"),
        related=("forks", "hanging-pieces"),
    ),
    Concept(
        "mating-patterns", "Mating patterns", "tactics",
        "Checkmates come in families: back-rank mates, smothered mates, the queen-and-knight battery, the two-rook "
        "ladder. Knowing the shapes lets you see a mate in two in one glance.",
        "The analysis records every forced mate that was missed or allowed. Missing one costs a win; allowing one "
        "loses on the spot. Both usually come from not knowing the pattern rather than from miscalculation.",
        "A king with few escape squares (its own pawns in front, the edge of the board, pieces blocking it). Heavy "
        "pieces that could reach the back rank. A knight within reach of the squares next to the king.",
        "Learn ten patterns by name. When their king has few free squares, look for a check that takes the rest away. "
        "When yours does, make a flight square (a pawn move, or a rook off the back rank) before the attack arrives.",
        "How many free squares does each king have, and which check would take them away?",
        themes=("missed_mate", "allowed_mate"), tags=("checkmate", "mate threat"),
        patterns=("missed_mate", "allowed_mate"),
        related=("king-safety", "checks-captures-threats"),
    ),
    # ---------------------------------------------------------------- safety
    Concept(
        "king-safety", "King safety", "safety",
        "Your king needs a shelter (pawns in front), defenders nearby, and no open lines leading to it.",
        "Attacks on the king end games quickly, and the analysis finds that attacks are usually *allowed* several "
        "moves before they succeed: a pawn pushed in front of the king, a defender sent to the other wing, a file "
        "opened by an exchange. The pattern across your games shows how often this happens to you.",
        "Count the defenders near your king and the attackers near it. Which pawns in front of your king have moved? "
        "Which files or diagonals point at it? Have you castled yet (the castling statistic on Home tells you how "
        "much castling late costs you)?",
        "Castle early unless there is a concrete reason not to. Keep at least one minor piece near the king. Avoid "
        "pushing the pawns in front of your king unless you are the one attacking. When they bring pieces towards "
        "your king, bring defenders back before the attack lands.",
        "If they bring every piece towards my king, who is defending it?",
        themes=("king_attack",), tags=("allows mate threat",), patterns=("king_attack",), findings=("castling",),
        related=("mating-patterns", "what-does-it-threaten"),
    ),
    Concept(
        "prophylaxis", "Prophylaxis", "safety",
        "Preventing the opponent's best idea before it happens, instead of answering it afterwards.",
        "Strong players spend as much time on the opponent's plans as on their own. A move that stops their only good "
        "idea is often stronger than any move that pursues yours, and it is the hardest kind of move to find.",
        "Ask what the opponent would play if it were their move twice in a row. If the answer is a good move, that is "
        "the idea to prevent. Pawn breaks, piece manoeuvres to a strong square and rook lifts are typical.",
        "Before pursuing your plan, make sure their best plan does not work. A quiet move that takes away a square, "
        "overprotects a weak point or closes a file can be worth a tempo.",
        "If they could move twice, what would they do, and can I stop it now?",
        related=("what-does-it-threaten", "plans-and-pawn-breaks"),
    ),
    # ---------------------------------------------------------------- strategy
    Concept(
        "plans-and-pawn-breaks", "Plans and pawn breaks", "strategy",
        "A plan is where you will attack and how; the pawn structure tells you where. A pawn break opens the position "
        "on the wing where you are stronger.",
        "Without a plan, moves are random and the position slowly gets worse. The commentary in every game names each "
        "side's plan (the wing, the break, the open file) so you can see what each move was for.",
        "Which way do your pawn chains point? Where do you have more space? Which pawn can advance to challenge "
        "their centre? Which file would open if pawns were exchanged, and whose rooks would use it?",
        "Attack where your pawns point and where you have more space. Prepare a pawn break with pieces first, then "
        "play it when their pieces are far away. Read the Story tab of your games to see the plans spelt out.",
        "Where am I stronger, and which pawn move opens the game there?",
        related=("open-files-and-outposts", "prophylaxis"),
    ),
    Concept(
        "open-files-and-outposts", "Open files and outposts", "strategy",
        "Rooks belong on open or half-open files; knights belong on outposts, squares in the enemy camp that no pawn "
        "can ever attack.",
        "Piece activity decides most middlegames. A rook on a closed file and a knight on the rim do nothing; the same "
        "pieces on an open file and an outpost dominate. The commentary points out open files and whose rooks "
        "use them.",
        "A file with no pawns on it (open) or only enemy pawns (half-open). A square in front of an enemy pawn that "
        "their pawns can never attack, especially one supported by your own pawn.",
        "Put rooks on open files before they are needed. Route a knight to an outpost even if it takes three moves. "
        "Deny the opponent the same by keeping pawns able to challenge their outposts.",
        "Which of my pieces is doing the least, and where would it do the most?",
        related=("plans-and-pawn-breaks", "piece-activity"),
    ),
    Concept(
        "piece-activity", "Piece activity", "strategy",
        "A piece is worth what it does: a bishop behind its own pawns is a tall pawn, a rook on the seventh rank "
        "is a monster.",
        "Material counts are even in most positions; what differs is how much each piece does. Positions are won by "
        "improving the worst-placed piece, one move at a time, before anything dramatic happens.",
        "Count the squares each piece controls. Pieces on their starting squares after move 15, bishops blocked by "
        "their own pawns, knights on the edge, rooks behind closed pawns are all underused.",
        "When there is no tactic and no immediate threat, find your worst piece and give it a better square. Trade "
        "your bad pieces for their good ones, never the other way round.",
        "Which of my pieces is worst, and how do I improve it?",
        related=("open-files-and-outposts", "plans-and-pawn-breaks"),
    ),
    # ---------------------------------------------------------------- phases
    Concept(
        "opening-principles", "Opening principles", "phases",
        "Control the centre, develop every piece once, castle, connect the rooks, and only then start a plan.",
        "Losing in the opening is rarely about not knowing theory; it is about breaking a principle: moving the same "
        "piece twice, grabbing a pawn with the queen, delaying castling. The analysis shows where you left theory and "
        "what masters play there, and how your games go after that point.",
        "Pieces still at home while the opponent's are all out. Your queen out early and being chased. A king still in "
        "the centre at move 12. Pawn grabs that cost two developing moves.",
        "Develop with a purpose (towards the centre, with a threat if possible), castle by move 10, and when you reach "
        "a position you do not know, fall back on the principles rather than memory. Review the *left book* moment in "
        "each game: that is where learning a little theory pays most.",
        "Have I developed everything and castled? If not, why am I doing something else?",
        tags=("left book",), findings=("after_book",), phase="opening",
        related=("king-safety", "piece-activity"),
    ),
    Concept(
        "middlegame-method", "Middlegame method", "phases",
        "When the pieces are out and nothing is hanging, the middlegame is about plans, piece placement and timing.",
        "The middlegame is where most of the moves are played and most of the winning chances are lost. The phase "
        "statistics on Home show your accuracy here against the opening and endgame.",
        "The pieces are developed, the kings are castled and there is no immediate tactic. If you feel there is "
        "'nothing to do', you are in the middlegame and need a plan.",
        "Use the sequence: safety first (checks, captures, threats), then the worst piece, then the pawn break. "
        "Trade when it helps your structure or removes their best piece; otherwise keep the tension.",
        "What is my plan for the next five moves, and does it survive their best reply?",
        phase="middlegame",
        related=("plans-and-pawn-breaks", "piece-activity", "prophylaxis"),
    ),
    Concept(
        "endgame-technique", "Endgame technique", "phases",
        "With few pieces left, the king becomes a fighting piece, passed pawns decide, and exact knowledge replaces "
        "general ideas.",
        "Endgames are where a won game is actually won, or thrown away. The endgame specialist in the analysis checks "
        "king activity, passed pawns, whether a king can catch a pawn and the opposition, and tells you when you got "
        "them wrong.",
        "Queens off and few pieces left. Passed pawns (no enemy pawn can stop them). Kings far from the action. "
        "Pawn endings, where a single tempo decides, deserve special care.",
        "Activate the king as soon as the queens are gone. Push passed pawns, or create one. In king and pawn endings, "
        "count: can the king catch the pawn (the rule of the square)? Who has the opposition? Learn the handful of "
        "theoretical positions (Lucena, Philidor, king and pawn against king) by heart.",
        "Is my king active, and which pawn can become a passed pawn?",
        phase="endgame",
        related=("converting-advantages", "counting-exchanges"),
    ),
    # ---------------------------------------------------------------- practical play
    Concept(
        "converting-advantages", "Converting a winning position", "practical",
        "Being winning is not the same as winning. Converting means trading down, keeping the king safe and giving the "
        "opponent nothing.",
        "The pattern across your games shows how often you win the games in which you reached a winning position. "
        "Every one of those not won is a result thrown away after the hard part was done.",
        "The evaluation bar is well in your favour and you start playing fast, or looking for a brilliant finish, or "
        "allowing complications for no reason.",
        "Simplify: trade pieces (not pawns) when ahead in material. Take away the opponent's counterplay before you "
        "push your own advantage. Keep asking what they threaten; a winning position has to be kept, every move.",
        "What is their only hope here, and can I remove it?",
        findings=("conversion",),
        related=("endgame-technique", "what-does-it-threaten"),
    ),
    Concept(
        "clock-management", "Using the clock", "practical",
        "Time is a resource: spend it at the critical moments, not on routine moves, and never let it run out.",
        "The analysis separates mistakes made in time trouble and moves made in under five seconds from the rest. "
        "Those are not gaps in knowledge; they are gaps in time use, and they are fixed by habit, not study.",
        "Blunders in the last minute. Long thinks on moves that did not matter, followed by instant moves that did. "
        "The Train page leaves these puzzles out by default, because they are a clock problem, not a chess one.",
        "In the opening, play moves you know quickly. At the first real decision, spend time. Keep a reserve for the "
        "endgame. If you keep losing on time, play a slower time control for a while: it is the fastest way to "
        "improve.",
        "Is this a moment to think, or a moment to move?",
        patterns=("rushed", "time_trouble"),
        related=("critical-moments", "checks-captures-threats"),
    ),
    Concept(
        "defending-worse-positions", "Defending a worse position", "practical",
        "A bad position is not a lost one. Defence means making the win as hard as possible: activity, counterplay and "
        "setting problems.",
        "The pattern across your games shows how many of your clearly lost positions you saved. Every half point "
        "rescued counts as much as one won from the opening.",
        "The evaluation bar is well against you. The temptation is either to give up or to lash out; both lose "
        "faster.",
        "Find the opponent's clearest plan and make it awkward. Keep pieces active rather than passive. Look for "
        "fortresses and perpetual checks. Make them find the win: most players below master do not.",
        "What is the most difficult problem I can set them right now?",
        findings=("defence",),
        related=("prophylaxis", "endgame-technique"),
    ),
]

_BY_ID = {c.id: c for c in CONCEPTS}


def concept(cid: str) -> Concept | None:
    return _BY_ID.get(cid)


def _public(c: Concept) -> dict:
    return {"id": c.id, "title": c.title, "category": c.category, "summary": c.summary, "why": c.why,
            "spot": c.spot, "do": c.do, "question": c.question, "themes": list(c.themes), "tags": list(c.tags),
            "phase": c.phase, "related": [{"id": r, "title": _BY_ID[r].title} for r in c.related if r in _BY_ID]}


def _matches(c: Concept, item: dict) -> bool:
    themes = set(item.get("themes") or [])
    tags = set(item.get("tags") or [])
    if themes & set(c.themes) or tags & set(c.tags):
        return True
    return bool(c.phase) and item.get("phase") == c.phase and not (c.themes or c.tags)


def overview(profile_id: int | None, time_class: str | None = None) -> dict:
    """Every concept with the player's own evidence: how many of their puzzles it explains, which
    cross-game findings point at it, and a few of those puzzles as worked examples."""
    items = training.train_items(profile_id) if profile_id else []
    if time_class:
        items = [it for it in items if it.get("time_class") == time_class]
    stats = store.aggregate_stats(profile_id, time_class=time_class) if profile_id else {"games": 0}
    findings = {f["id"]: f for f in stats.get("findings") or []}
    patterns = {p["id"]: p for p in stats.get("patterns") or []}
    weakest = stats.get("weakest_phase")
    out = []
    for c in CONCEPTS:
        mine = [it for it in items if _matches(c, it)]
        mine.sort(key=lambda it: (-(it.get("importance") or 0), -(it.get("loss") or 0)))
        games = {it["game_id"] for it in mine}
        hits = [findings[f] for f in c.findings if f in findings]
        pat = [patterns[p] for p in c.patterns if p in patterns]
        weight = (sum((it.get("importance") or 1) for it in mine)
                  + 4 * sum(1 for f in hits if f["kind"] == "weakness")
                  + 2 * sum(p["count"] for p in pat if p["id"] in ("rushed", "time_trouble"))
                  + (6 if c.phase and c.phase == weakest else 0))
        examples = [{"game_id": it["game_id"], "i": it["i"], "label": it["label"], "played": it["played"],
                     "best": it["best"], "opponent": it["opponent"], "cls": it["cls"],
                     "importance": it.get("importance"), "date": it.get("date", ""), "why": it.get("why", "")}
                    for it in mine[:3]]
        out.append({**_public(c), "puzzles": len(mine), "games": len(games), "findings": hits, "patterns": pat,
                    "weakest_phase": bool(c.phase and c.phase == weakest), "weight": weight, "examples": examples})
    return {
        "categories": [{"id": i, "title": t, "blurb": b} for i, t, b in CATEGORIES],
        "concepts": out,
        "focus": focus(out, stats),
        "games": stats.get("games", 0),
    }


def focus(concepts: list[dict], stats: dict) -> list[dict]:
    """The two or three concepts to work on next, with the evidence in one sentence each.

    Only concepts with real evidence qualify: puzzles from the player's games, a weakness found
    across games, or the weakest phase. Nothing is said before the first analysed game.
    """
    if not stats.get("games"):
        return []
    ranked = sorted((c for c in concepts if c["weight"] > 0), key=lambda c: -c["weight"])
    out = []
    for c in ranked[:3]:
        reasons = []
        if c["puzzles"]:
            reasons.append(f"{c['puzzles']} of your puzzles come from this, in {c['games']} game"
                           f"{'s' if c['games'] != 1 else ''}")
        for f in c["findings"]:
            if f["kind"] == "weakness":
                reasons.append(f["text"].rstrip("."))
        for p in c["patterns"]:
            if p["id"] in ("rushed", "time_trouble"):
                reasons.append(f"{p['label'].lower()}: {p['count']} time{'s' if p['count'] != 1 else ''}")
        if c["weakest_phase"]:
            acc = (stats.get("phase_accuracy") or {}).get(c["phase"])
            reasons.append(f"the {c['phase']} is your least accurate phase"
                           + (f" ({acc}% accuracy)" if acc is not None else ""))
        out.append({"id": c["id"], "title": c["title"], "question": c["question"], "summary": c["summary"],
                    "reason": "; ".join(reasons[:2]).capitalize() + ".", "puzzles": c["puzzles"],
                    "theme": c["themes"][0] if c["themes"] else "", "phase": c["phase"]})
    return out
