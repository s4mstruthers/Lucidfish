# Lucidfish

<p align="center">
  <a href="https://github.com/s4mstruthers/Lucidfish/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/s4mstruthers/Lucidfish?label=download&color=2563EB"></a>
  <a href="https://github.com/s4mstruthers/Lucidfish/actions/workflows/ci.yml"><img alt="Tests" src="https://img.shields.io/github/actions/workflow/status/s4mstruthers/Lucidfish/ci.yml?label=tests"></a>
  <a href="https://github.com/s4mstruthers/Lucidfish/actions/workflows/build-apps.yml"><img alt="App builds" src="https://img.shields.io/github/actions/workflow/status/s4mstruthers/Lucidfish/build-apps.yml?label=app%20builds"></a>
  <a href="LICENSE"><img alt="Licence: AGPL-3.0-or-later" src="https://img.shields.io/badge/licence-AGPL--3.0--or--later-2563EB"></a>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-3776AB">
</p>

<p align="center">
  <a href="https://github.com/s4mstruthers/Lucidfish/releases/latest"><b>⬇ Download for macOS, Windows or Linux</b></a>
  · nothing else to install · or <code>pip install -e .</code> from source · <a href="#download">install guide</a>
</p>

**Stockfish tells you the best move — Lucidfish tells you _why_.**

Lucidfish reviews your chess games like a coach and a commentator. The Stockfish engine
finds what happened; an AI language model (running on your own computer, or a cloud model
if you prefer) explains it in plain chess concepts. It follows the flow of the game: what
each player was trying to do, which plans and pawn breaks they were working towards, and
why a move was played ("Black keeps pushing on the queenside to open the a-file"). So when
you look back on a game, you can see the intention behind every move, not just its verdict.
The feedback is pitched at your level and linked to the mistakes you keep making across games.

![Lucidfish game view](docs/screenshot.png)
<sub>Game view, shown here in engine-only mode. With an AI coach enabled, each move also gets a written explanation.</sub>

## Why Lucidfish exists

An engine is the strongest analyst in chess and the worst teacher. It gives you a number and a
line and leaves the understanding to you, and at club level the understanding is the whole
problem. Lucidfish starts from a different picture of what a game is: a story. Each side has
plans, the plans meet, at some moment one of them breaks, and everything afterwards follows from
that. You improve when you can tell that story about your own games, and most of us cannot,
because nobody tells it to us.

A language model can tell the story. Left to itself it cannot be trusted to, because models
misplace pieces, describe captures that never happened and praise moves that lose. Lucidfish's answer is a strict
division of labour. **The engine and the code decide what is true; the model only puts it into
words; and every sentence is checked against the board before you read it.** The model never looks
at a position by itself. It is handed verified evidence (the engine's lines and what each one leads
to, the tactics on the board, the plans each side has been following, the point where the game left
theory) and, when it wants to claim something the evidence does not cover, it has to ask Stockfish
first.

Two further choices follow from wanting this to be something anyone can run:

- **Local first.** The coach runs on your own computer with a free, open-weight model by default.
  Your games stay on your machine unless you choose a cloud model with your own key. There is no
  account, no server and no subscription.
- **Open and measured.** The whole pipeline is free software under the AGPL, and `lucidfish eval`
  scores any model on how faithfully it explains engine-verified positions, so a claim about
  accuracy can be checked rather than taken on trust.

Where it is going: a coach that any club player can run for nothing, that is right far more often
than it is wrong, that shows its evidence, and that improves in the open, with more detectors for
plans and ideas, a larger verified eval set, a "would a player at your level find this?" judgement,
and in time small local models trained on explanations that have survived the fact-checker. The
[roadmap](#roadmap) has the order.

**Status.** Lucidfish is at 0.5: the pipeline is complete and tested on three operating systems,
the desktop apps work but are not yet signed with a paid developer certificate, and the eval set is small. It is ready to use and
ready for contributors; see [Contributing](#contributing).

## Download

Lucidfish runs on **macOS, Windows and Linux**. Take the file for your system from the
[latest release](https://github.com/s4mstruthers/Lucidfish/releases/latest); it brings its own copy of
Stockfish, so there is nothing else to install.

| System | File | Then |
|---|---|---|
| macOS | `Lucidfish-…-macOS-arm64.dmg` (Apple silicon, 2020 or later) or `…-macOS-x64.dmg` (Intel) | Open it and drag **Lucidfish** onto **Applications**. |
| Windows | `Lucidfish-…-Windows-x64-setup.exe` | Run it; Lucidfish appears in the Start menu. |
| Linux | `Lucidfish-…-Linux-x64.AppImage` | `chmod +x` it and run it (works on any distribution). |

The apps are not signed with a paid developer certificate, so the system asks once, the first time
you start one:

- **macOS** says Apple could not verify it. Click *Done*, open *System Settings → Privacy & Security*
  and click *Open Anyway* next to Lucidfish. Versions before 0.5.2 may instead say *"Lucidfish" is
  damaged*. It is not damaged: click *Cancel* (never *Move to Bin*), then run this once in Terminal:
  `xattr -dr com.apple.quarantine /Applications/Lucidfish.app`.
- **Windows** shows *Windows protected your PC*. Click *More info*, then *Run anyway*.

The `.app.tar.gz`, `.sig` and `latest.json` files on the release page are what installed apps use
to update themselves; you never need them. Later versions install themselves: a banner appears
when a new version is out, and *Update and restart* installs it in the background (Settings → About &
updates checks on demand). Versions before 0.5.2 cannot check for updates: if you have one, download
the latest release by hand once. Your games and settings stay where they are. What changed in each version is in [CHANGELOG.md](CHANGELOG.md).

The optional AI coach is set up inside the app (Settings → AI coach): free and private on your own
computer with [Ollama](https://ollama.com), or with a cloud provider and an API key.

Prefer to run from source, or want the command line? See [Installation](#installation).

---

## Contents

- [Why Lucidfish exists](#why-lucidfish-exists)
- [Download](#download)
- [Features](#features)
- [How it works](#how-it-works)
- [Before you start: hardware and performance](#before-you-start-hardware-and-performance)
- [Installation](#installation) — [macOS](#macos) · [Windows](#windows) · [Linux](#linux)
- [The desktop app](#the-desktop-app)
- [Choosing an AI coach](#choosing-an-ai-coach)
- [Using API keys safely](#using-api-keys-safely)
- [Using the web app](#using-the-web-app)
- [Sharing a profile](#sharing-a-profile)
- [Command line](#command-line)
- [Configuration](#configuration)
- [How accurate is the feedback?](#how-accurate-is-the-feedback)
- [Measuring the coach](#measuring-the-coach)
- [Troubleshooting](#troubleshooting)
- [Privacy and your data](#privacy-and-your-data)
- [Development](#development)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [Acknowledgements and licences](#acknowledgements-and-licences)

---

## Features

- **Commentary on the flow of the game.** Every move by both players gets a commentator's
  line about its purpose: the plan it belongs to, the wing it works on, the break it prepares,
  and how it answers the opponent. Read the whole game as a story in the *Story* tab, split
  into chapters ("Black's queenside pawn storm", "The decisive attack") with a short summary
  of each.
- **Explained, not just evaluated.** Every mistake gets a written explanation of *why* it
  was a mistake, what the better move achieves, and the line that punishes it.
- **Grounded in verified facts.** The AI never analyses the position itself. It receives
  engine lines plus facts checked directly on the board (hanging pieces, forks, pins,
  skewers, mate threats, king safety, pawn structure), **what every line actually leads to**
  (material won or lost, checkmate, promotions, the tactics left at the end), and its notes
  are fact-checked against the position afterwards: the moves it names, the pieces it places,
  the captures it describes and the assessments it gives ("Black is winning") must all match
  the evidence. When it writes about your mistakes it can **run the engine itself**: it asks
  Stockfish to test a variation before stating what it leads to, and the lines it checked are
  shown under the note. Specialist checks add the lessons coaches care about
  most: **missed threats** (the opponent was already threatening the move that punished
  you), **endgame technique** (active kings, passed pawns, whether a king can catch a pawn,
  the opposition) and **the move where you left opening theory**, with what masters play
  there instead and what players at your own rating do (and how it goes for them).
- **Accuracy you can compare.** Moves are judged by lost *winning chances*, and game
  accuracy uses Lichess's published formula, so the numbers line up with what you see there.
- **Learn: understand why, not just what.** Every mistake the analysis finds points at a concept
  (a missed threat, hanging material, a fork you did not see, a king left in the open, leaving opening
  theory, the clock). The *Learn* page explains each one in plain language: why it decides games, how to
  recognise it at the board, what to do about it, and the one question to ask yourself before every
  move, with **your own games as the examples** (each example opens as a puzzle) and a *Train* button
  for exactly those positions. Home shows the two or three concepts to work on next, with the evidence.
- **Personal coaching.** Profiles remember your games, ratings and level. After a couple of
  analyses, the coach writes a progress review of your recurring weaknesses and refers
  back to them in later games. The home page shows your accuracy in the opening,
  middlegame and endgame, the kinds of mistakes you make most (hanging material,
  missed threats, errors in time trouble, …) and **patterns across your games**: whether
  you convert winning positions, how you score when you castle late, with White and with
  Black, in each time control, and how your games go after they leave opening theory.
- **See yourself improve.** The *Stats* page charts your accuracy, results, blunders and mistakes,
  and missed threats game by game, per time control, and compares your most recent games with the
  ones before them. A change is only reported when it is big enough to mean something, and the
  numbers are computed, never written by the AI; the coach's review builds its *Progress* section
  from them.
- **Practise your mistakes.** Jump back to any position where you went wrong, play a better
  move on the board, and the engine tells you straight away whether it works and, if not,
  shows the line that refutes it. The answer to your own mistakes stays hidden until you
  have tried (or choose to see it), so you actually get to think.
- **Train on your own mistakes.** The *Train* page turns the positions where you went wrong,
  across all your games, into puzzles, ranked by importance (★★★ the error changed the game,
  ★★ a clear mistake, ★ minor) and grouped by theme (missed threats, hanging
  material, missed tactics, mates, king safety, game phase). Solve one and it comes back less
  and less often (after 1, 3, 7, 16 and 35 days); miss it and it returns at the end of the
  session and again the next day. This is spaced repetition, the way flashcard apps make
  things stick. By default it's kept manageable: key and costly errors only, no bullet games or
  moves made in time trouble, and at most 10 new puzzles a day.
- **Everything per time control.** Pick *Bullet*, *Blitz*, *Rapid*, … at the top of Home,
  Games, Train or *Analyse new games*, and every page follows: the statistics, patterns and
  openings, the games list, the puzzles, and the recent games fetched from chess.com or
  Lichess (so "analyse my last 5 rapid games" is two clicks). Each time control gets its own
  coach review, and the overall review compares them. Time controls follow each site's own
  rules: a 30-minute game is rapid on chess.com and classical on Lichess.
- **Explore any position.** Press *Explore* (<kbd>E</kbd>) and move the pieces yourself:
  Stockfish runs inside the browser and shows the evaluation and its three best lines as you
  go. Take moves back and forth with the arrow keys.
- **Share a profile with someone who doesn't run Lucidfish.** Save a profile as a single web
  page (or keep one up to date in a shared iCloud Drive, Google Drive or Dropbox folder). It
  opens in any browser with nothing to install and no API key, and it stays interactive:
  practise, train, explore with the built-in engine, and draw. See
  [Sharing a profile](#sharing-a-profile).
- **Draw on the board.** Right-click to circle a square, right-drag to draw an arrow (Shift
  for red, Alt for blue), or use the ✎ pen on a tablet. Drawings are saved with the game
  and exported in the annotated PGN.
- **Local or cloud AI.** By default the coach is a free, open-weight model running privately
  on your computer with [Ollama](https://ollama.com) (`qwen3:8b`) or LM Studio. Or plug in
  OpenAI, Anthropic (Claude), Google Gemini, OpenRouter or Groq with an API key, or switch
  the AI off for fast, engine-only analysis. Every answer is requested as structured JSON
  that the provider enforces where it can, so nothing is lost to formatting drift.
- **Your games, one click away.** Pick recent games from chess.com or Lichess by username,
  paste or drop a PGN, or set up any position in the board editor.
- **All your analysed games in one place.** The *Games* page lists every game you've
  analysed, with result, accuracy and mistakes at a glance. Search by opponent or opening,
  filter by result, colour and time control, sort by date or accuracy, and re-analyse a game
  with new settings.
- **An analysis queue with live progress.** Queue one game or twenty. A progress bar shows
  how far each game and the whole batch are, how much time is left, and roughly when it
  will be done. Reorder, pause, resume or remove games at any time; the queue survives a
  restart. Time estimates learn how fast your computer is.
- **A clean, fast workspace.** A sidebar takes you to Home, Learn, Games, Train, Stats and the Board;
  the design is minimal and quick, with light and dark themes, and it works on a phone or tablet
  too. Moves appear while the game is analysed, with an interactive board, arrows and an
  evaluation bar, a clickable evaluation graph, buttons that jump between the key moments,
  variation previews and coach chat. Engine hints (best-move arrow, evaluation bar and graph,
  move highlights) can each be switched off. Several people can share one computer, each with
  their own profile.
- **Export.** Annotated PGN (verdict symbols, evaluations, notes and best lines, ready for a
  Lichess study or ChessBase) or a Markdown report.
- **Efficient without cutting corners.** Commentary is written for 8 moves per AI request,
  and the engine searches the next queued game while the AI is still writing. Engine
  results and opening lookups are cached, so re-analysing a game is nearly instant.
  See [Why it's fast](#why-its-fast).
- **Runs on macOS, Windows and Linux**, as a desktop app that updates itself or from source.

## How it works

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/pipeline-dark.svg">
  <img alt="How Lucidfish analyses a game: Stockfish and board checks in Python establish the facts, the coach model writes from that evidence, and a fact-checker reads every sentence back against the board." src="docs/pipeline-light.svg" width="100%">
</picture>

The engine and the AI run at the same time: while the coach writes about move 10, Stockfish
is already analysing move 14. Results appear in the web app as soon as each move is ready.

### Following the flow of the game

A move only makes sense in context: *why* did White play a4? To explain intentions without
the AI having to "read" the board (which language models do badly), Lucidfish gives it
verified context for every stretch of moves:

- **A plan tracker** (`plans.py`) summarises, from the moves actually played, where each side
  has been active (queenside, centre, kingside), which pawns have advanced on which wing,
  which pawn breaks are available, which files are open and whose rooks are on them, and
  whether the kings are on opposite wings.
- **The recent moves and what happened next.** Because the whole game is known, the coach
  can say "this prepares …a5, which Black played three moves later". Every move shown to
  the AI carries its side ("13… Black a5"), so it cannot give one side's move to the other.
- **Every claim is fact-checked.** A move mentioned in a note must be playable by the side
  the sentence attributes it to, a piece must stand where the note says, and a capture must
  have happened the way it is described. Sentences that fail are corrected or removed.
- **Chapters.** Once the moves are done, the game is split where the phase changes and at
  its turning points (big swings in winning chances). The coach summarises each chapter
  from its commentary, and the post-game review is written from the chapter summaries:
  a short, structured input instead of the whole game at once.

The layers build on each other, like a team of specialists reporting to a head coach:
code checks the board (engine, tactics, threats, endgame, opening theory, plans), the
commentary combines those facts eight moves at a time, the chapters combine the commentary,
the review combines the chapters, and your coach profile combines your reviews and the
patterns found across your games. Only the combining is done by the AI; everything it
combines has been verified.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/layers-dark.svg">
  <img alt="The layers of a review: facts from code at the bottom, then commentary, chapters, the post-game review and the coach profile, each written by the model from the verified layer below." src="docs/layers-light.svg" width="100%">
</picture>

### Why it's fast

Writing one AI request per move would be slow, especially with a local model, and would lose
the thread between moves. Instead, the default **Commentary** level uses three ideas:

1. **Commentary windows.** One request covers 8 consecutive moves (4 by each side), so the AI
   sees them as one stretch of play and writes connected commentary. That is about 8 times
   fewer requests than one per move. Mistakes and critical moments still get their own full,
   detailed note.
2. **Cheap context instead of long history.** The plan tracker compresses the game so far
   into a few verified lines, so prompts stay short however long the game is.
3. **No idle hardware.** Stockfish (CPU) and the AI (GPU or network) run at the same time.
   In the queue, once the engine has finished a game, it immediately starts on the next one
   while the AI finishes writing, so the engine work for the next game is usually done
   before it starts. Accuracy is unchanged: the same depth and settings are used.

| Coaching detail | What you get | AI requests for a 40-move game |
|---|---|---|
| **Key moments** | Notes for your mistakes and the critical moments only | ~5–10 |
| **Commentary** (default) | A line on every move by both players, plus full notes for your mistakes and critical moments | ~15–20 |
| **Every move** | A full note on every move by both players | ~80 |

With *check lines with the engine* on (the default), each note on a mistake may add one to
three short follow-up requests while the coach tests variations with Stockfish.

## Before you start: hardware and performance

Lucidfish needs two programs besides itself: **Stockfish** (the engine) and, optionally,
an **AI model** for the written explanations.

> **Running an AI model locally is processor-intensive.** Local models use a lot of memory
> and compute. They run best on a Mac with Apple silicon or a PC with a recent graphics card.
> On a laptop without a GPU, expect each explanation to take several seconds, and your
> fans to spin up. Stockfish is also CPU-heavy while it analyses.
> If your computer struggles, choose a smaller model, set the coaching detail to
> **Key moments**, use a cloud provider, or turn the AI coach off.

| Your computer | Suggested local model (Ollama) | Download size | Notes |
|---|---|---|---|
| 8 GB RAM | `qwen3:4b` | ~2.5 GB | Shorter, simpler explanations |
| 16 GB RAM | `qwen3:8b` (default) or `qwen2.5:7b` | ~5 GB | Good balance; follows the JSON format and can call the engine |
| 32 GB RAM or more | `qwen3:14b` or `gemma3:12b` | 8–9 GB | Better prose, slower (gemma3 cannot call the engine) |

Approximate speed for a 40-move game at the default settings: the engine takes 1–3 minutes
on a modern laptop. The AI's time depends on your hardware and model; a cloud model is
usually fastest.

## Installation

The easiest way is the [desktop app](#download). The rest of this section is for running Lucidfish
from its source code (for development, the command line, or a server).

You need **Python 3.10 or newer** ([python.org/downloads](https://www.python.org/downloads/)),
**Stockfish** (or let Lucidfish download it: Settings → Analysis → *Download Stockfish*), and
optionally **Ollama** for a local AI.

The quickest way to start is the launcher script. It creates a private Python environment
in `.venv` the first time, installs everything, and opens the app in your browser:

| System | Start Lucidfish |
|---|---|
| macOS / Linux | `./scripts/start.sh` |
| Windows | double-click `scripts\start.bat` |

Prefer to do it by hand? `pip install -e .` inside a virtual environment, then run
`lucidfish web` (details for each system below).

### macOS

```bash
# 1. Engine
brew install stockfish

# 2. Local AI (optional — skip if you'll use a cloud provider or engine-only mode)
brew install ollama          # or download the app from https://ollama.com/download
ollama serve                 # leave running (the Ollama app does this for you)
ollama pull qwen3:8b         # ~5 GB, one-off download

# 3. Lucidfish
git clone https://github.com/s4mstruthers/Lucidfish.git
cd Lucidfish
./scripts/start.sh
```

### Windows

1. **Python:** install it from [python.org](https://www.python.org/downloads/windows/) and tick
   **"Add python.exe to PATH"** during setup.
2. **Stockfish:** download the Windows build from
   [stockfishchess.org/download](https://stockfishchess.org/download/) and unzip it (for example
   to `C:\Stockfish`). Lucidfish looks in common places — `C:\`, your home, Downloads and Desktop
   folders, Program Files, and the Chocolatey, Scoop and winget folders. If it can't find it, set the path in
   **Settings → Analysis → Stockfish**.
3. **Local AI (optional):** install [Ollama for Windows](https://ollama.com/download), then in a
   terminal run `ollama pull qwen3:8b`.
4. **Lucidfish:** download this repository (Code → Download ZIP, or `git clone`), open the folder
   and double-click `scripts\start.bat`.

Manual install in PowerShell:

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\lucidfish.exe web
```

### Linux

```bash
# 1. Engine (Debian/Ubuntu shown; Fedora: sudo dnf install stockfish; Arch: sudo pacman -S stockfish)
sudo apt install stockfish python3-venv

# 2. Local AI (optional)
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3:8b

# 3. Lucidfish
git clone https://github.com/s4mstruthers/Lucidfish.git
cd Lucidfish
./scripts/start.sh
```

On Debian and Ubuntu, Stockfish installs to `/usr/games/stockfish`, which Lucidfish finds
automatically even if it isn't on your `PATH`.

**Check your setup** at any time with `lucidfish --check` (it tests the engine and the AI
coach), or look at the status pill at the bottom of the sidebar.

## The desktop app

The downloads on the [releases page](https://github.com/s4mstruthers/Lucidfish/releases) are the same
program in a native window: a small shell (built with [Tauri](https://tauri.app)) starts the Lucidfish
backend, which is packed into the app together with the web app and an official Stockfish build, and
shows it. Everything in this README applies to it; the differences:

- **Nothing to install first.** Python, Stockfish and the web app are inside. Ollama or an API key are
  still needed for the AI coach, as before.
- **It updates itself.** On start the app looks at the newest release on GitHub. If there is one, a
  banner offers *Update and restart*; Settings → About & updates has *Check for updates*. Updates are
  signed and verified before they are installed, and your data folder is untouched.
- **Same data folder** as the source version, so you can switch between the two.
- `desktop.log` in the data folder says what the shell did if something goes wrong.

Building it yourself, and how releases are made, is described in [`desktop/README.md`](desktop/README.md).

## Choosing an AI coach

Open **Settings (⚙) → AI coach** and pick where the coach runs. Use **Test connection** to
make sure it works before analysing.

| Provider | Runs | Needs | Default model | Good to know |
|---|---|---|---|---|
| **Ollama** | on your computer | [Ollama](https://ollama.com) + a pulled model | `qwen3:8b` | Private and free; uses your CPU/GPU |
| **Custom OpenAI-compatible** | on your computer (or any server) | a server URL, e.g. LM Studio `http://localhost:1234/v1` | — | Also works with llama.cpp, vLLM, LocalAI |
| **OpenAI** | cloud | `OPENAI_API_KEY` | `gpt-4.1-mini` | |
| **Anthropic (Claude)** | cloud | `ANTHROPIC_API_KEY` | `claude-opus-5-5` | `claude-sonnet-5-5` and `claude-haiku-5-5` are cheaper; Haiku is ideal as the main model with Opus or Sonnet as the second. A key created outside a workspace also needs `ANTHROPIC_WORKSPACE_ID` |
| **Google Gemini** | cloud | `GEMINI_API_KEY` | `gemini-2.5-flash` | |
| **OpenRouter** | cloud | `OPENROUTER_API_KEY` | `openai/gpt-4.1-mini` | One key, hundreds of models |
| **Groq** | cloud | `GROQ_API_KEY` | `llama-3.3-70b-versatile` | Very fast open-weight models |

You can type any model name the provider offers; the list only shows suggestions. Model
catalogues change often, so if a default stops working, choose a current model.

**Cost:** cloud providers charge per use. A 40-move game at the *Commentary* detail level makes
roughly 15–20 requests of about 2,500 words each, with short answers. As a rough guide at the
list prices of October 2026 (check your provider's pricing page):

| Model | About per 40-move game | Notes |
|---|---|---|
| `claude-haiku-5-5` | around 1 cent | Fast; good for the running commentary |
| `claude-sonnet-5-5` | 10–20 cents | |
| `claude-opus-5-5` | 20–40 cents | Best as the second model: a few requests per game |
| `gpt-4.1-mini`, `gemini-2.5-flash` | a few cents | |

Use *Key moments* or a smaller model to spend less, or use a cloud model only as the second
model (below): then it handles only the handful of notes on mistakes, the review and chat.
Lucidfish asks Claude 5.5 models for low reasoning effort on the running commentary and more
on the notes and review, which keeps the cost down without losing accuracy where it matters.

**Coaching detail** (Settings → Analysis) controls how much the coach writes, and therefore
how long an analysis takes:

| Level | What gets written | Speed |
|---|---|---|
| **Key moments** | Notes on your mistakes and the game's critical moments | Fastest |
| **Commentary** (default) | A commentator's line on every move by both players, plus full notes on your mistakes and critical moments | Balanced |
| **Every move** | A detailed note on every move by both players | Slowest |

### Which local model? (Ollama)

Pick by how much memory your computer has. Good starting points:

| Memory | Model | Why |
|---|---|---|
| 8 GB | `qwen3:4b` | Small enough to leave room for everything else |
| 16 GB | `qwen3:8b` (default) or `qwen2.5:7b` | Follow the JSON format well and can call the engine (tool use); about 5 GB |
| 24 GB or more | `qwen3:14b` or `gemma3:12b` | Noticeably better explanations, about half the speed (gemma3 has no tool use) |

Ollama enforces the answer format for every model (its `format` option), so even small models
return well-formed JSON. Checking lines with the engine needs a model with *tool use*
(`qwen3`, `qwen2.5`, `llama3.1` and most recent models; not `gemma3`); if a model cannot, the
coach simply writes from the evidence it was given.

*Thinking* models such as `qwen3` normally write hidden reasoning before answering, which is
slow and eats into the answer's length limit. Lucidfish switches that off for Ollama, so they
answer directly. (If a model can't switch it off, Lucidfish notices and asks it normally.)

### Two models: a fast one for commentary, a smarter one for the hard parts

Under **Settings → AI coach → Second model for the hard parts** you can add a second model:

- The **main model** writes the move-by-move commentary. It handles most of the requests, so
  a fast local model is ideal.
- The **second model** writes the detailed notes on your mistakes and critical moments, the
  post-game review, your coach profile, chat answers and board-editor assessments. These are
  the jobs where a smarter model makes the biggest difference, and there are only a handful
  per game.
- **Escalation:** if a stretch of commentary fails the fact-check, the second model rewrites
  it, instead of the main model trying again.
- The second model is also the one that **checks lines with the engine** while writing about
  your mistakes, so a strong cloud model here makes the notes on your errors both deeper and
  better verified.
- If the second model stops working (e.g. no internet or an expired key), the main model
  takes over its jobs and the game page shows a warning.

A good setup for a laptop is a local main model (e.g. `qwen2.5:7b`) with a small cloud model
(e.g. `claude-haiku-5-5`, `gpt-4.1-mini` or `gemini-2.5-flash`) as the second model. The
cloud model only sees the evidence for your key moments and the review, so the cost stays
small. It also works in parallel with your computer, so games finish sooner. From the
command line: `--expert-provider anthropic --expert-model claude-haiku-5-5`, or set
`LUCIDFISH_EXPERT_PROVIDER` / `LUCIDFISH_EXPERT_MODEL`.

## Using API keys safely

Cloud providers need an API key. A key works like a password that can spend money, so
Lucidfish treats it that way. There are three ways to provide one, from most to least convenient:

1. **The Settings panel (recommended).** Paste the key and click *Save key*. It is stored in
   your operating system's credential store: **macOS Keychain**, **Windows Credential
   Manager**, or **Secret Service / KWallet** on Linux. If your system has no secure store
   (for example a headless Linux server), the key is kept in memory only until Lucidfish
   stops, and the app tells you so.
2. **An environment variable**, e.g. `export ANTHROPIC_API_KEY=...` (macOS/Linux) or
   `setx ANTHROPIC_API_KEY "..."` (Windows, then open a new terminal).
3. **A `.env` file.** Copy [`.env.example`](.env.example) to `.env` in the Lucidfish folder
   (or your data folder) and fill in the keys you use. `.env` is in `.gitignore`, so it is
   never committed.

What Lucidfish guarantees:

- Keys are **never written to its database**, never logged, and **never sent back to the
  browser**. The Settings panel only shows a masked hint such as `sk-…a1b2` and where the
  key came from.
- If a provider's error message echoes your key, Lucidfish removes it before showing the error.
- Keys are only sent to the provider you chose (cloud providers are always reached over HTTPS).
- The web server listens on `127.0.0.1` (this computer only). It rejects requests from other
  websites (cross-site request forgery protection) and from unexpected host names
  (DNS-rebinding protection), and it sets a strict Content Security Policy.

To remove a key, click *Remove* in Settings, or delete the "lucidfish" entry in your system's
keychain app.

## Using the web app

Start it with `./scripts/start.sh`, `scripts\start.bat`, or `lucidfish web`. It opens
`http://127.0.0.1:8420`; if that port is busy, it uses the next free one and prints the address.

The sidebar has six sections: **Home** (your progress and what to work on next), **Learn** (the
concepts behind your mistakes, explained with your own games), **Games** (your analysed games and new
ones to analyse), **Train** (puzzles from your mistakes), **Stats** (your numbers over time) and
**Board** (set up any position). Your
profile, the theme switch and ⚙ **Settings** are at the bottom of the sidebar.

1. **Create a profile** (first launch): your name, level, and chess.com / Lichess usernames.
   Each game is coached at *your rating in that game* (chess.com and Lichess put it in every
   game file), and the coach is told which site it's from, since Lichess ratings run a few
   hundred points higher. The profile keeps your newest rating per site and time control by
   itself, from the games you analyse; *Look up* fetches your current ones. Those are used for
   games without a rating (e.g. over the board) and the board editor. To add another person or
   switch between profiles, click your name at the bottom of the sidebar.
2. **Analyse games** (Games → *Analyse new games*): choose chess.com or Lichess to list your
   recent games, and a time control to list only those (for chess.com, Lucidfish looks back up
   to a year to find them). Click games to select them (<kbd>Shift</kbd>-click selects a range,
   or use *Select all*), then *Analyse selected*, or *Analyse batch* for your last 5–20 games. The
   *Analyse* button on a row analyses just that game, and *Open* shows one you've already
   analysed. You can choose a lighter or heavier coaching level for a batch, or paste or drop
   a PGN file instead.
3. **Watch the queue:** the pill at the top shows the current game and the time left. Click it
   to open the queue: an overall progress bar with the estimated finish time, a bar for the
   game in progress, and each waiting game with when it will start. Move games up or down,
   remove them, or pause the whole queue. A game you open yourself jumps to the front.
   If you close Lucidfish with games still waiting, they come back paused next time.
4. **Review the game:** moves appear while the analysis runs. Click any move, or use
   <kbd>←</kbd>/<kbd>→</kbd> (<kbd>Home</kbd>/<kbd>End</kbd> to jump, <kbd>F</kbd> to flip).
   <kbd>N</kbd> and <kbd>P</kbd> (or the ⚑ buttons) jump to the next or previous key moment.
   Each move shows the commentary, its verdict, tactical tags, verified facts (a missed
   threat, the move that left opening theory), the coach's note, better alternatives, and
   the line that punishes a mistake. Click a line to play it out on the board, and press
   <kbd>Esc</kbd> to return. Switch the move list to **Story** to read the whole game as a
   commentary, chapter by chapter. *← Games* (or the browser's Back button) takes you back.
5. **Practise your mistakes:** on your own mistakes the answer is hidden at first. Click
   *Find a better move* and drag a piece on the board: the engine judges your move with the
   same standards as the analysis. *Show the answer* reveals the note and the better move.
   *Practise my mistakes* in the game header steps through all of them. (To always see the
   answers straight away, switch this off in Settings → Analysis.)
6. **Draw and choose what the board shows:** right-click a square to circle it, right-drag
   for an arrow (<kbd>Shift</kbd> red, <kbd>Alt</kbd> blue, both yellow), or press
   *Draw* (<kbd>D</kbd>) to draw with a normal click or a finger. A plain click on the
   board clears the drawing. *Board ▾* switches the best-move arrow (<kbd>A</kbd>), the
   last-move highlight, the evaluation bar and the graph on or off.
7. **Games:** every analysed game is saved to your profile. Search and filter them by time
   control, result, colour or text (e.g. all your losses as Black in blitz, or every
   Sicilian), see your record and average accuracy
   for that selection, and click a game to review it. Each game has its own address, so a
   reload keeps you on it. *↻ Re-analyse* runs a game again with your current settings.
8. **Ask the coach** about the position you're looking at, or about your overall progress on
   the Home page.
9. **Export** an annotated PGN (with the commentary, chapters and your drawings) or a
   Markdown report from the game header.
10. **Board editor:** set up any position (or paste a FEN) and ask for an assessment from
    either side's point of view.
11. **Explore:** press *Explore* under the board (or <kbd>E</kbd>) to leave the game and move
    the pieces yourself. The engine (Stockfish, running in your browser) shows the evaluation,
    an arrow for its best move and its three best lines; click a line to play its first move.
    <kbd>←</kbd>/<kbd>→</kbd> take moves back and replay them, *⟲ Reset* returns to where you
    started, and <kbd>Esc</kbd> or *✕ Done* goes back to the game. During practice, Explore
    unlocks once you've tried the position.
12. **Train:** the *Train* page shows how many puzzles are to do today, not started, being
    learned and mastered. Choose a time control, a theme (e.g. *Missed threats*), how important
    the puzzles must be (★★★ key only, ★★ key and costly, or everything), how many new ones a
    day, and whether to include bullet games or moves made in time trouble. *Start training*
    gives you a session of 5, 10 or 20: first the ones due for review, then new ones, the most
    important first. Each puzzle opens in its game, so after solving it you can read the coach's
    note, play out the lines, or explore. Solve a puzzle on the first try and it moves up a
    level; miss it (or ask for the answer) and it starts again. Progress is saved in your browser.

The **Home** page shows your record, average accuracy, patterns across your games, your
accuracy in each phase of the game (with your weakest phase highlighted), your most common
mistake types, your openings and how you score in them, and the coach's written review of
your progress. Choose a time control at the top to see all of it for, say, your rapid games
only, with the coach's review of those games; each game is also coached with the review for
its time control in mind. The review updates by itself after new analyses (Settings → AI coach →
*Update my coach review automatically*, on by default): it's rewritten once the analysis queue
has finished, so it never slows an analysis down, and the Home page shows when it was last
updated, or that an update is on its way. *⟳ Update review* rewrites it on demand.

Every review has a fixed layout, so they're quick to read and easy to compare. The coach's review
starts with a sentence or two on where you stand, then *⏱ By time control* (in the overall review),
*⚠ Holding you back*, *✓ Working well*, *♟ Openings* and *🎯 Train next*, as short bullets. The
post-game review has *Summary*, *How the game unfolded*, *What went well* and *Key takeaways*. If
the AI drifts from the layout, Lucidfish puts it back, and fills a missing section from the verified
statistics (for example, *Train next* points you at the matching puzzles on the Train page).

## Sharing a profile

Lucidfish needs a reasonably fast computer for the analysis, but the *results* can go
anywhere. Someone without Lucidfish (a partner, a student, a friend on a Chromebook) can get
a profile as **one web page**:

- Click your name at the bottom of the sidebar → **Share this profile**.
- **Download the page** and send it by email, AirDrop or a chat app. Or
- **keep it up to date in a shared folder**: pick a folder that syncs to their computer (a
  shared iCloud Drive, Google Drive, Dropbox or OneDrive folder; Lucidfish suggests the ones it
  finds). The page is rewritten after every analysis of that profile, so they always open the
  latest version. The file is replaced in one step, so a sync app never picks up half a file.

The page is the normal app with the profile's games built in. It opens from disk in any modern
browser (Chrome, Edge, Firefox, Safari) on a laptop or desktop, works offline, needs no AI or
API key, and never contacts a server. They can:

- browse the games, the commentary, the chapters and the review, and see the Home page's
  statistics and patterns;
- **practise their mistakes** and **train** with spaced repetition. Progress is saved in their
  browser and carries over when a newer copy replaces the file, as the shared folder does.
  *Save my progress* on the Train page downloads it as a small file (a backup, or to carry
  it to another computer or browser) and *Load progress…* merges it back in; for each puzzle
  the most recently practised version wins, so an old backup never undoes newer practice;
- **explore** any position with the built-in engine;
- draw on the board (also saved in their browser).

The built-in engine is Stockfish compiled to WebAssembly, and it makes the page about 10 MB.
Untick *Include the chess engine* (or use `lucidfish share --no-engine`) for a page under
1 MB. Without the engine, practice moves are judged only against the engine's top moves
stored with each game, and Explore can move the pieces but shows no evaluation.

Things that need Lucidfish itself are hidden in the shared page: analysing new games, the AI
coach chat and the board editor.

On a phone the page opens in the browser too, but some phones preview downloaded HTML files
without running them (the iPhone's Files and Mail previews, for example): use *Open in
Safari/Chrome*, or open it on a laptop.

## Command line

Everything the web app does can also be done in a terminal:

```bash
lucidfish examples/sample.pgn                      # analyse a PGN file
lucidfish --chesscom YOUR_NAME                     # your latest chess.com game
lucidfish --lichess YOUR_NAME --recent 2           # your second-latest Lichess game
lucidfish game.pgn --side white --elo 1400         # coach White, pitched at ~1400
lucidfish game.pgn --no-llm                        # engine-only, fast
lucidfish game.pgn --detail key                    # notes only for mistakes and key moments
lucidfish game.pgn --detail standard               # commentary on every move (default)
lucidfish game.pgn --provider anthropic --model claude-sonnet-5
lucidfish game.pgn --pgn-out annotated.pgn --out report.md --json analysis.json
lucidfish --check                                  # diagnose Stockfish and the AI coach
lucidfish eval                                     # measure the coach's accuracy on the built-in test set
lucidfish eval --build my_games/*.pgn              # add cases from your own games (engine-derived, automatic)
lucidfish web                                      # start the web app
lucidfish share --profile Ann --out ~/Dropbox      # save a profile as one web page (see above)
```

(Without installing the package, use `python -m lucidfish` instead of `lucidfish`.)

| Option | Meaning |
|---|---|
| `--chesscom USER`, `--lichess USER`, `--recent N` | Fetch a recent game instead of reading a file |
| `--side white\|black` | Coach this side (detected automatically for fetched games) |
| `--elo N` | Your rating, to pitch explanations at your level (default: your rating in the game, else your profile's) |
| `--detail key\|standard\|full`, `--explain-all` | How much the coach writes: key moments, commentary on every move, or a full note on every move (`--explain-all` = `full`) |
| `--provider`, `--model` | AI provider and model (defaults to what you saved in the web app) |
| `--expert-provider`, `--expert-model` | Second model for notes on mistakes, the review and fact-check rewrites (`none` = off) |
| `--no-llm` | Engine analysis only |
| `eval [--build PGN...] [--cases FILE] [--limit N] [--judge PROVIDER:MODEL]` | Measure the coach's accuracy (see [Measuring the coach](#measuring-the-coach)) |
| `--preset fast\|balanced\|deep`, `--depth N`, `--fast` | Engine strength (depth 14 / 18 / 22, or 0.3 s per position) |
| `--threads N`, `--no-cache` | CPU threads for Stockfish; ignore stored engine results |
| `--out FILE.md`, `--pgn-out FILE.pgn`, `--json FILE.json` | Write a report, annotated PGN, or JSON |

## Configuration

Settings are applied in this order (later wins): built-in defaults → environment variables
and `.env` → settings saved in the web app → command-line flags.

| Variable | Default | Purpose |
|---|---|---|
| `LUCIDFISH_STOCKFISH` | auto-detected | Path to the Stockfish program |
| `LUCIDFISH_DEPTH` | `18` | Engine search depth per position |
| `LUCIDFISH_MAX_SECONDS` | `30` | Longest the engine may spend on one position, whatever the depth (`0` = no limit) |
| `LUCIDFISH_THREADS` | most of your cores, leaving some free (max 8) | Engine CPU threads |
| `LUCIDFISH_HASH_MB` | `256` | Engine hash table size |
| `LUCIDFISH_PROVIDER` | `ollama` | `ollama`, `openai`, `anthropic`, `gemini`, `openrouter`, `groq`, `custom` |
| `LUCIDFISH_MODEL` | provider default | Model name |
| `LUCIDFISH_BASE_URL` / `OLLAMA_HOST` | provider default | Server URL for local providers |
| `LUCIDFISH_EXPERT_PROVIDER`, `LUCIDFISH_EXPERT_MODEL` | — | Optional second model for the hard parts (see [Choosing an AI coach](#choosing-an-ai-coach)) |
| `LUCIDFISH_DETAIL` | `standard` | `key` (key moments), `standard` (commentary) or `full` (every move) |
| `LUCIDFISH_NUM_CTX` | `8192` | Context window for local models (prevents silent prompt truncation) |
| `LUCIDFISH_DATA_DIR` | per-user folder (see below) | Where the database is stored |
| `LUCIDFISH_PORT`, `LUCIDFISH_HOST` | `8420`, `127.0.0.1` | Web server address |
| `LUCIDFISH_ALLOWED_HOSTS` | — | Extra host names allowed when serving on a network (comma-separated) |
| `LUCIDFISH_CHESSCOM_USER`, `LUCIDFISH_LICHESS_USER` | — | Default account for `lucidfish` with no arguments |
| `LICHESS_TOKEN` | — | Only if the Lichess opening explorer requires authentication |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `LUCIDFISH_CUSTOM_API_KEY` | — | API keys (see [Using API keys safely](#using-api-keys-safely)) |

## How accurate is the feedback?

Lucidfish is built so the explanation can only be as wrong as the evidence behind it, and
the evidence is checked in several ways:

- **Verdicts use winning chances, not raw centipawns** (the Lichess method). Going from +8 to
  +5 is still completely winning and is not called a blunder, while going from 0.0 to −2.0 is.
  A move is an *inaccuracy*, *mistake* or *blunder* when it costs at least 5, 10 or 15
  percentage points of winning chances.
- **Mates are handled explicitly.** Missing a forced mate, allowing one, and delivering one are
  each recognised and described correctly, using Lichess's rules.
- **Suspicious verdicts are double-checked.** Before a move is called a mistake or blunder,
  and when the engine's own top choice seems to collapse one move later, the position is
  searched deeper. This stops brilliant sacrifices being labelled blunders because of the
  engine's search horizon.
- **Tactics are verified on the board**, not guessed: static exchange evaluation for pieces
  that can be won, plus pins, skewers, forks and mate threats. The AI receives these as facts.
- **Notes are fact-checked, per side.** Each sentence is checked for the side it talks about
  ("White", "Black", "you", "your opponent", "…a5", "12. c4"). A move must be playable by
  that side in a position or line the coach was shown; a piece must stand on the square the
  note says; a capture must have happened the way it is described. The AI is asked once to
  correct any problem, and sentences that are still wrong are removed rather than shown.
  (For example, after White's a-pawn has captured on b4, "your opponent's next move may be
  a5" is rejected: White has no a-pawn left.)
- **The post-game review is checked too.** The coach is given verified facts about the game
  as a whole: the result from your side, how it ended (for example, lost on time with a forced
  mate on the board), which kinds of endgame it reached, if any, the clock, and verified
  highlights of what you did well. The written review is then checked against the game:
  - the result must be right ("mistakes that cost you the game" after a win is an error);
  - every move must be credited to the side that played it ("your Ne4" when White played it
    is an error), and a move called a mistake or blunder must have that verdict;
  - endgames the game never reached ("review king and pawn endings" with queens and rooks on
    the board) and time trouble the clocks don't show are errors too.

  Errors go back to the coach for one rewrite; anything still wrong is removed. Each key
  takeaway, and each point under *What went well*, must name a move or a verified fact, so
  stock advice and empty praise are dropped. Empty sections are written from the facts: the
  costliest mistakes and what was better, or the verified highlights.
- **Every line comes with its consequences.** Candidate moves, refutations and expected
  continuations are played out on the board, and the coach is told what each one actually
  leads to: material won or lost and which pieces came off, checkmate, promotions, castling,
  new passed pawns, and the tactics left at the end. That is what lets it explain *why* a
  line works instead of inventing a reason.
- **The coach can run the engine.** When it writes about a mistake or a critical moment, the
  model may call `check_line`: it hands Stockfish a position and a sequence of moves and gets
  back whether they are legal, what happens, the evaluation at the end and the engine's
  continuation. Every line it checked is admitted by the fact-checker, and shown under the
  note as *Checked with the engine*. (Settings → AI coach → Advanced; on by default.)
- **Assessments are checked too.** "White is winning", "Black has a clear edge", "the position
  is equal": each such claim is compared with the evaluations the coach was given (before and
  after the move, each candidate, each checked line). A claim none of them supports is sent
  back once for correction and otherwise removed.
- **Structured answers.** Every request asks for JSON of a fixed shape, and Ollama, OpenAI-
  compatible servers and Claude enforce it while the model writes. Notes, commentary and
  reviews are parsed, not scraped out of free text, so a model drifting from the format can no
  longer lose a line of commentary or a whole section of the review.
- **Accuracy percentages** use the formula Lichess publishes, so they are directly comparable.
- **It is measured.** `lucidfish eval` runs the coach on a built-in set of positions whose
  expectations come from the engine (the best move, the refutation, the tactical motif, the
  evaluation), grades every note automatically — grounded in the evidence, assessment
  consistent, best move and refutation mentioned, nothing removed by the fact-checker — and
  reports a score per model, so you can compare models and settings without judging the chess
  yourself. See [Measuring the coach](#measuring-the-coach).

Limits: at low depth (the *Fast* preset), very deep combinations can still be misjudged, so use
*Balanced* or *Deep* for important games. Small local models occasionally phrase things
clumsily even when the facts are right; a larger model or a cloud model writes better prose.

## Measuring the coach

How good is a given model at explaining chess faithfully? You don't need to be able to judge
the chess yourself: `lucidfish eval` grades every answer against the engine.

```bash
lucidfish eval                                   # the configured coach on the built-in cases
lucidfish eval --provider ollama --model qwen3:14b
lucidfish eval --provider anthropic --model claude-haiku-5-5 --limit 10
lucidfish eval --build evals/games/*.pgn         # add cases from more games (needs Stockfish only)
lucidfish eval --judge anthropic:claude-opus-5-5 # also have a strong model rate each note 1-5
```

Cases are mistakes, blunders and critical moments found by the engine in real games
(`evals/cases.jsonl`, built from the sample game; add your own with `--build`). For each case
the coach writes its note exactly as it would in the app, and the run reports, per case and
overall:

| Measure | What it means |
|---|---|
| grounded | the first answer named no move, piece or capture that the evidence does not support |
| assessment | its claims about who is better matched the engine's evaluations |
| best move | it named the engine's best move (when the move played wasn't it) |
| refutation | it named the move that punishes the mistake |
| motif | it mentioned the verified tactical motif (fork, pin, hanging piece, mate threat, …) |
| kept | nothing had to be removed by the fact-checker |
| engine checks | how many variations it tested with the engine, and how many were legal |

Results are written to `evals/results/` as JSON, so two runs (two models, or the same model
before and after a prompt change) can be compared. The optional `--judge` adds a 1–5 rating of
faithfulness and helpfulness from a strong model that sees the same evidence.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Stockfish isn't set up yet" | Press *Download Stockfish* in the banner (or Settings → Analysis): Lucidfish fetches the official build for your computer, about 40 MB. Or install it yourself (see [Installation](#installation)) and set its path in Settings → Analysis. |
| macOS says Apple could not verify the app | The app is not notarised yet. Open *System Settings → Privacy & Security* and click *Open Anyway* next to Lucidfish, once (see [Download](#download)). |
| macOS says *"Lucidfish" is damaged and can't be opened* | Versions before 0.5.2 were unsigned, and macOS blocks those downloads this way. It is not damaged: click *Cancel* (not *Move to Bin*), then run `xattr -dr com.apple.quarantine /Applications/Lucidfish.app` in Terminal, once. Or download the latest version. |
| The macOS app says "Lucidfish's backend could not start" with *Failed to load Python shared library* | A bug in 0.5.2 on macOS. Download the [latest release](https://github.com/s4mstruthers/Lucidfish/releases/latest) by hand. |
| *Check for updates* says "Command check_update not allowed by ACL" | A bug in versions before 0.5.2. Download the [latest release](https://github.com/s4mstruthers/Lucidfish/releases/latest) by hand once; updates work from inside the app after that. |
| "Cannot reach Ollama" | Start the Ollama app (or run `ollama serve`). Check the server URL in Settings if Ollama runs on another port or machine. |
| "Model … is not installed in Ollama" | Run `ollama pull <model>`, or pick one of the installed models shown in Settings. |
| Analysis is slow | Use the *Key moments* detail level, a smaller model, a cloud provider, or the *Fast* engine preset. Close other heavy apps. |
| "rejected the API key" | Re-paste the key in Settings. Check it hasn't expired and that billing is enabled on your provider account. |
| Keys don't persist on Linux | Your desktop has no Secret Service running (common on servers). Use a `.env` file or an environment variable instead. |
| chess.com / Lichess games don't load | Check the username in your profile. Some networks block these sites; paste the PGN instead. |
| Opening names show without master statistics | The Lichess opening explorer couldn't be reached, so a built-in opening book is used. Analysis is unaffected. |
| Port 8420 already in use | Lucidfish picks the next free port automatically and prints the address. Or run `lucidfish web --port 9000`. |
| "Lucidfish needs a restart to finish updating" | The files were updated (e.g. with `git pull`) while Lucidfish was running, so the page is new but the running program is old. Close Lucidfish (<kbd>Ctrl</kbd>+<kbd>C</kbd> in its window) and start it again. |
| Queued games didn't start after a restart | Games left in the queue come back **paused**, so nothing starts by surprise. Open the queue and press *Resume*. |
| Time estimates look wrong | The first estimate is a rough guess. After one game with the same settings has finished, estimates use your computer's measured speed. |
| `ModuleNotFoundError` | Run from the Lucidfish folder with the virtual environment active, or use the launcher script. |

## Privacy and your data

Lucidfish is local-first. Your profiles, analysed games and caches are stored in one SQLite
file on your computer:

| System | Data folder |
|---|---|
| macOS | `~/Library/Application Support/Lucidfish` |
| Windows | `%APPDATA%\Lucidfish` |
| Linux | `~/.local/share/lucidfish` |

(Older versions stored it in `data/` inside the project folder; that location keeps working.)
Delete that folder to remove everything, or delete individual games and profiles in the app.

What leaves your computer: game lists are fetched from chess.com and Lichess by your browser;
opening statistics come from the Lichess opening explorer. With a cloud AI provider, the
evidence for each move (moves, engine lines and position facts) is sent to that provider.
With a local model, nothing about your games leaves your machine.

## Development

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest                     # unit + integration tests (engine tests skip if Stockfish is missing)
ruff check lucidfish tests
```

The test suite never touches your real data or keychain: every test uses a temporary data
folder and the network is disabled. CI runs the suite on Linux, macOS and Windows.

| File | Role |
|---|---|
| `lucidfish/config.py` | Defaults, per-OS paths, Stockfish auto-detection, `.env` loading |
| `lucidfish/settings.py` | Merges defaults, environment and settings saved in the web app |
| `lucidfish/engine.py` | Stockfish wrapper, candidate lines, verdicts, evaluation cache hook |
| `lucidfish/scoring.py` | Winning chances, accuracy, evaluation wording |
| `lucidfish/tactics.py` | Static exchange evaluation, pins, skewers, forks, mate threats |
| `lucidfish/features.py` | Positional facts and "what changed" diffs |
| `lucidfish/endgame.py` | Endgame specialist: king activity, passed pawns, rule of the square, opposition |
| `lucidfish/opening.py` | Lichess opening explorer client and offline opening book |
| `lucidfish/prompts.py` | Prompt construction, output parsing, fact-checking (moves, pieces, captures, assessments) |
| `lucidfish/schemas.py` | The JSON shapes of the coach's answers and how they become text |
| `lucidfish/lines.py` | What a line leads to: material, mate, promotions, the tactics left at the end |
| `lucidfish/tools.py` | `check_line`: the engine as a tool the coach can call while writing |
| `lucidfish/llm.py` | AI providers (Ollama, OpenAI-compatible, Anthropic): structured output, tool use, effort |
| `lucidfish/evalset.py` | `lucidfish eval`: engine-graded accuracy measurement of the coach |
| `lucidfish/spelling.py` | British spelling for the coach's text and opening names (Lucidfish is written in British English) |
| `lucidfish/credentials.py` | API key storage (environment, OS keychain, session) |
| `lucidfish/plans.py` | Plan tracker: wing activity, pawn advances, breaks, open files |
| `lucidfish/pipeline.py` | Orchestrates engine and coach; commentary windows; game review; position mode; practice moves |
| `lucidfish/jobs.py` | Analysis queue: ordering, live progress, learned time estimates, engine prefetch |
| `lucidfish/insights.py` | Accuracy by game phase, recurring mistake types, patterns across games |
| `lucidfish/store.py` | SQLite: profiles, games, settings, caches |
| `lucidfish/export.py` | Annotated PGN and Markdown reports |
| `lucidfish/fetch.py` | chess.com / Lichess game download (CLI) |
| `lucidfish/cli.py` | Command-line interface |
| `lucidfish/engine_setup.py` | Downloads an official Stockfish build from inside the app |
| `lucidfish/concepts.py` | The Learn section: concepts, explained and tied to the player's own mistakes |
| `lucidfish/web.py` + `static/` | FastAPI backend and the browser app (plain JS, no build step) |
| `desktop/` | The desktop app: Tauri shell, PyInstaller recipe, release scripts (see its README) |
| `docs/make_diagrams.py` | Draws the README's workflow diagrams (`docs/*.svg`); edit the text there, not the SVGs |
| `demo/record_demo.py` | Records the captioned demo video with a scripted browser (see its docstring) |

## Roadmap

In rough order. Open an issue to argue for a different one.

**Now**

- An accuracy table in this README: `lucidfish eval` results for the common local and cloud
  models, so you can choose a coach on evidence rather than reputation.
- A first run that works without downloading a 5 GB model: a free cloud tier as the one-click
  option, a local model as the private one.
- Signed macOS and Windows builds, once the certificates are funded.

**Next**

- "Would a player at your rating find this?": human-likelihood (in the style of Maia) so the coach
  can tell an objectively best move from a findable one.
- More plan and concept detectors (minority attacks, outposts, bad bishops, rook lifts), each with
  its own fact-check and eval cases.
- A larger eval set from club-level games, and a comparison against published benchmarks for
  chess explanation.

**Later**

- A small local model fine-tuned on Lucidfish notes that survived the fact-checker: faster,
  cheaper, and grounded by construction.
- Real-time coaching from a connected board (for example ChessUp 2 over Bluetooth).

## Contributing

Issues and pull requests are welcome, from a wrong coach note (that is a bug) to a new plan
detector. [CONTRIBUTING.md](CONTRIBUTING.md) explains the setup, the conventions, and the one rule
every change to the coach follows: it may not assert what the code has not verified. Issues
labelled [good first issue](https://github.com/s4mstruthers/Lucidfish/labels/good%20first%20issue)
are a place to start. The [Code of Conduct](CODE_OF_CONDUCT.md) applies everywhere in the project,
and [SECURITY.md](SECURITY.md) says how to report a security problem privately. If Lucidfish is
useful in research or writing, [CITATION.cff](CITATION.cff) has the reference.

## Acknowledgements and licences

- [Stockfish](https://stockfishchess.org) — the engine (GPL-3.0). The engine that analyses your
  games is installed separately. For Explore mode and shared pages, Lucidfish bundles
  [Stockfish.js](https://github.com/nmrugg/stockfish.js) 18 (the "lite, single-threaded"
  WebAssembly build, GPL-3.0; source code at that link) in `lucidfish/static/vendor/`, and a
  shared page carries a copy of it with its licence notice.
- [chess.js](https://github.com/jhlywa/chess.js) (BSD-2-Clause) — legal moves in the browser.
- [python-chess](https://github.com/niklasf/python-chess) (GPL-3.0+) — move generation, PGN,
  UCI, and the classic "Cburnett" piece images shown on the board.
- [chessboard.js](https://chessboardjs.com) and [jQuery](https://jquery.com) (both MIT) —
  bundled in `lucidfish/static/vendor/`; see `LICENSES.txt` there.
- [Lichess](https://lichess.org) — opening explorer and the accuracy/win-probability model.

The desktop app bundles an official Stockfish binary (GPL-3.0); the notices are in
[`desktop/THIRD_PARTY.md`](desktop/THIRD_PARTY.md), shipped inside the app.

Lucidfish itself is free software under the [GNU Affero General Public License, version 3 or later](LICENSE) (AGPL-3.0-or-later): you may use, study, change and share it, and anyone who distributes a changed version, or runs one as a service for others, must make their source available under the same terms. The same family of licence as Stockfish and python-chess, which Lucidfish depends on.
