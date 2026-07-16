# Lucidfish

Stockfish tells you the best move; Lucidfish tells you **why**. It grounds an LLM in real engine analysis — evals, refutation lines, and extracted positional features like tempo, king safety, and pawn structure — so you get explanations in chess concepts instead of centipawns. Opening-aware suggestions via the Lichess Opening Explorer keep early-game advice consistent with your chosen opening's plans rather than raw engine output.

## How it works

```
PGN game
   │
   ▼
┌─────────────┐   candidates, eval swings,     ┌──────────────┐
│ engine.py   │──  refutation lines          ──▶              │
│ (Stockfish) │                                │              │
└─────────────┘                                │              │
┌─────────────┐   king safety, development,    │  llm.py      │──▶ explanation
│ features.py │──  pawn structure, hanging   ──▶  (Ollama)    │    per move
│ (py-chess)  │    pieces, center, mobility    │              │
└─────────────┘                                │              │
┌─────────────┐   opening name + what          │              │
│ opening.py  │──  masters actually play     ──▶              │
│ (Lichess)   │                                └──────────────┘
└─────────────┘
```

The LLM never analyses the position itself — it only narrates verified evidence. That's what stops it hallucinating tactics.

## Setup (macOS)

```bash
# 1. Engine
brew install stockfish

# 2. Local LLM
brew install ollama
ollama serve            # leave running in a separate terminal (or run the Ollama app)
ollama pull llama3.1:8b # ~5 GB; fits comfortably in 16 GB RAM
                        # qwen2.5:14b (~9 GB) gives better prose but is tight on 16 GB —
                        # close other heavy apps if you try it

# 3. Python deps (3.10+) — conda env "lucidfish"
cd Lucidfish
conda activate lucidfish
pip install -r requirements.txt
```

## Web UI

```bash
python -m lucidfish.web     # then open http://127.0.0.1:8420
```

Three input modes: paste/upload a PGN, pick a recent game by username (chess.com or
Lichess — fetched by your browser, which passes Cloudflare checks that block Python),
or set up a custom position on the board editor. You get an interactive board with
eval bar and move-square highlights, a clickable annotated move list, per-move
explanations with engine lines, the whole-game review, and a coach chat grounded in
whatever you're currently looking at.

## CLI usage

```bash
# Analyze a game, explanations for critical moves + opening phase
python -m lucidfish examples/sample.pgn

# Or pull your latest game straight from chess.com / Lichess (no token needed)
python -m lucidfish --chesscom YOUR_USERNAME
python -m lucidfish --lichess YOUR_USERNAME --recent 2   # second-most-recent game

# Only explain your own (White) moves, write a markdown report
python -m lucidfish mygame.pgn --side white --out report.md

# Explain every single move (slow), custom model/depth
python -m lucidfish mygame.pgn --explain-all --model qwen2.5:14b --depth 20
```

Getting your ChessUp 2 games: connect the board to Lichess, play, then download the PGN from your Lichess games page (or `https://lichess.org/api/games/user/<username>`).

## Tests

```bash
python tests/test_features.py     # no engine or LLM needed
```

## Layout

| File | Role |
|---|---|
| `lucidfish/config.py` | all knobs: engine path/depth, model, blunder thresholds |
| `lucidfish/engine.py` | Stockfish wrapper — MultiPV candidates, eval swing, refutations |
| `lucidfish/features.py` | positional facts computed with python-chess |
| `lucidfish/opening.py` | Lichess masters-database explorer client |
| `lucidfish/fetch.py` | pull recent games from chess.com / Lichess by username |
| `lucidfish/llm.py` | Ollama provider + the grounded prompt builder |
| `lucidfish/pipeline.py` | orchestrates the layers per move |
| `lucidfish/cli.py` | terminal output + markdown report |
| `lucidfish/web.py` + `static/` | FastAPI backend + single-page web UI |

## Roadmap

- v1.1: cache engine analysis (re-running a game shouldn't recompute)
- ~~v1.2: pull games straight from the Lichess/chess.com API by username~~ ✅ done
- v2: real-time coaching over the ChessUp 2 BLE board API
- ideas: Maia integration ("what would a human play?"), eval graphs, tactic tagging (fork/pin/skewer detection)
