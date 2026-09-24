"""The coach's progress review always has the same structure."""
from lucidfish.prompts import player_summary_system
from lucidfish.review_check import tidy_profile_review

STATS = {
    "patterns": [{"id": "missed_threat", "label": "Missed the opponent's threat", "count": 5, "games": 2},
                 {"id": "hanging", "label": "Left material hanging", "count": 4, "games": 3}],
    "openings": [{"name": "Italian Game", "games": 3, "w": 3, "l": 0, "d": 0},
                 {"name": "Scandinavian Defense", "games": 2, "w": 0, "l": 2, "d": 0},
                 {"name": "Queen's Pawn Game", "games": 1, "w": 1, "l": 0, "d": 0}],
    "phase_accuracy": {"opening": 77.7, "middlegame": 86.2, "endgame": 96.9}, "findings": [],
    "by_time_class": {"blitz": {"games": 3, "wins": 1, "losses": 2, "draws": 0, "avg_accuracy": 70.1,
                                "blunders_per_game": 2.0, "mistakes_per_game": 1.0},
                      "rapid": {"games": 3, "wins": 2, "losses": 1, "draws": 0, "avg_accuracy": 62.2,
                                "blunders_per_game": 3.67, "mistakes_per_game": 1.33}},
}


def _headings(text):
    return [line[4:] for line in text.splitlines() if line.startswith("### ")]


def test_each_review_type_asks_for_its_fixed_structure():
    overall, rapid = player_summary_system(), player_summary_system("rapid")
    assert "### ⏱ By time control" in overall and "### ⏱ By time control" not in rapid
    for title in ("### ⚠ Holding you back", "### ✓ Working well", "### ♟ Openings", "### 🎯 Train next"):
        assert title in overall and title in rapid
    assert "in your rapid games" in rapid and "Never mention centipawns" in rapid


def test_a_review_in_the_format_keeps_its_content_in_order():
    raw = """## Your progress review
You convert well once ahead, but threats slip past you.

**Train next:**
- Missed-threat puzzles every day.

### Weaknesses
- **Missed threats**: 5 times, e.g. against Shark.
- **Hanging pieces**: 4 times.
- One.
- Two too many.

### Working well
- You won all three Italians. Your centipawn loss is 87.1.
"""
    out = tidy_profile_review(raw, STATS, "rapid")
    assert _headings(out) == ["⚠ Holding you back", "✓ Working well", "♟ Openings", "🎯 Train next"]
    assert out.startswith("You convert well once ahead, but threats slip past you.")      # the title is gone
    assert "- **Missed threats**: 5 times, e.g. against Shark." in out and "Two too many" not in out  # max 3
    assert "- You won all three Italians." in out and "centipawn" not in out
    assert "- Missed-threat puzzles every day." in out          # the model's own "Train next" is kept
    assert "- **Italian Game**: your best opening (3W 0L 0D in 3 games)." in out         # left out: from stats
    assert "Queen's Pawn" not in out                               # one game is too few to judge


def test_prose_is_rebuilt_into_the_structure():
    raw = ("You play rapid at a rating of 701, which is a beginner-level rating. Your endgames are strong. "
           "You rush critical moves. And more.\n\nYour weaknesses are many.")
    out = tidy_profile_review(raw, STATS)
    assert out.startswith("Your endgames are strong. You rush critical moves.")         # two sentences, no label
    assert _headings(out) == ["⏱ By time control", "⚠ Holding you back", "✓ Working well", "♟ Openings",
                              "🎯 Train next"]
    assert "- **Rapid**: 3 games, 2W 1L 0D, accuracy 62.2%, 3.67 blunders per game." in out
    assert "- **Missed the opponent's threat**: 5 times in 2 games." in out
    assert "Your weaknesses are many" not in out                    # the opening line is the first paragraph
    assert "- Your strongest phase is the endgame (96.9% accuracy)." in out
    assert "*Missed threats* puzzles on the Train page" in out


def test_one_opening_is_not_called_the_best():
    stats = {**STATS, "openings": [{"name": "Scandinavian Defense", "games": 2, "w": 1, "l": 1, "d": 0}]}
    out = tidy_profile_review("Fine.", stats, "rapid")
    assert "- **Scandinavian Defense**: 1W 1L 0D in 2 games." in out and "best opening" not in out


def test_counts_read_naturally():
    stats = {"patterns": [{"id": "hanging", "label": "Left material hanging", "count": 1, "games": 1},
                          {"id": "missed_threat", "label": "Missed the opponent's threat", "count": 2, "games": 1}]}
    out = tidy_profile_review("Fine.", stats, "blitz")
    assert "- **Left material hanging**: once." in out
    assert "- **Missed the opponent's threat**: twice in one game." in out
