# What's new in Lucidfish

Newest first. Each [release on GitHub](https://github.com/s4mstruthers/Lucidfish/releases) uses its
version's section here as its description, and the desktop app shows it when it offers the update.
Write for the people using the app: what they will notice, not how it was done.

## Unreleased

- **Check for updates works again in the desktop app.** Settings → About & updates said "Command check_update not allowed by ACL" instead of checking. Versions before this one cannot update themselves to it: download this release once by hand, and later updates install from inside the app again.
- **Analysis no longer stalls on forced mates.** A depth-limited search could run for an hour in a position with a forced mate (the demo's Opera game sat on move 14 for that long). Every search now also has a time cap, 30 seconds per position by default (`LUCIDFISH_MAX_SECONDS`).

## 0.5.0

- **See yourself improve.** A new *Stats* page charts your accuracy, results, blunders and mistakes, and missed threats game by game, with a then-and-now comparison of your most recent games against the ones before them. Home shows the changes that matter, and the coach's review gains a *Progress* section that says what has improved and what has not, from those verified numbers only.
- **Coach's notes in paragraphs.** The coach now writes each note as two or three short paragraphs (verdict, what the line shows, the lesson) instead of one block of text, and notes from earlier analyses are split at sentence boundaries when shown.
- **A saved API key no longer looks unsaved.** Once a key is stored, Settings shows where it lives and offers *Replace key* and *Remove* instead of an empty paste box.
- **Anthropic keys that belong to an organisation rather than a workspace now work.** Set `ANTHROPIC_WORKSPACE_ID` (in the environment or `.env`) to the workspace the coach should use. Without it, the error now says what to do instead of showing Anthropic's raw reply.
- **Lucidfish now has a licence: AGPL-3.0-or-later.** The `LICENSE` file is in the repository; you are free to use, change and share Lucidfish under its terms.
- **A clearer front page and a way in for contributors.** The README now opens with why Lucidfish exists and where it is going; `CONTRIBUTING.md`, a code of conduct, a security policy, issue and pull-request templates and a citation file are in the repository.
- **Diagrams of how it works.** The README now shows the analysis pipeline and the layers of a review as drawings, in light and dark.
- **Install notes match the defaults.** The quick-start snippets pull `qwen3:8b`, the model the coach uses by default, and the macOS first-start tip matches current macOS.

## 0.4.2

- **A tidier coach review on Home.** Section headings no longer show up as stray `###` text at
  the start of every bullet.

## 0.4.1

- **A shorter download page.** One file per system: the `.dmg` for each kind of Mac, the Windows
  installer and the Linux AppImage. Every release now starts with a "which file do I need?" table.

## 0.4.0

The first downloadable desktop app, and a coach that is held to its evidence.

- **A desktop app for macOS, Windows and Linux.** One installer with Stockfish inside; nothing
  else to set up. The app tells you when a new version is out and installs it with one click.
- **A new workspace.** Home, Learn, Games, Train and Board in a sidebar; a bottom bar on phones.
  Light and dark themes, and a cleaner, faster game view.
- **Learn: understand why, not just what.** Twenty concepts, from "what does the move threaten?"
  to passed pawns and time trouble, each explained with your own games as the examples, with a
  Train button for exactly those positions. Home shows what to work on next and why.
- **Free, local AI coach by default.** The coach runs on your computer with Ollama's `qwen3:8b`;
  cloud models (Claude 5.5, OpenAI, Gemini, OpenRouter, Groq) remain one setting away.
- **The coach checks its lines with the engine.** When it writes about your mistakes it can test a
  variation with Stockfish before stating what it leads to, and the lines it checked are shown
  under the note. In the chat and the board editor too.
- **Every line comes with what it actually leads to**: material won or lost, checkmate, promotions,
  the tactics left at the end. Claims about who is better are checked against the engine's
  evaluations, and dotted pawn captures ("...bxa4") are now fact-checked like every other move.
- **What players at your rating do** in the opening, with the typical mistake at that level named
  next to what masters play.
- **Answers that cannot lose their shape.** The coach replies in a fixed structure the AI
  provider enforces, so a line of commentary or a section of the review is never lost to
  formatting drift.
- **Measure your coach**: `lucidfish eval` grades a model on engine-derived cases, so you can
  compare models and settings without judging the chess yourself.
- **Download Stockfish from inside the app** (Settings → Engine) if your system has none.
