# Contributing to Lucidfish

Thank you for considering it. Lucidfish is one person's project so far, and the parts that would
benefit most from more hands are listed first below. Small, focused pull requests are the easiest
to review and the most likely to be merged quickly.

## Where help is most useful

1. **Plan and concept detectors** (`lucidfish/plans.py`, `lucidfish/concepts.py`, `lucidfish/features.py`).
   The coach can only narrate what the code has verified, so every new detector (a minority attack, a
   bad bishop, an outpost, a rook lift, …) is a new thing the coach can explain truthfully. A detector
   comes with tests and, ideally, an eval case.
2. **Eval games and cases** (`evals/`). The built-in set is small. Annotated games with clear
   turning points, especially from club-level play (500–1800), make `lucidfish eval` more
   representative. Build cases with `lucidfish eval --build path/to/game.pgn`.
3. **Model reports.** Run `lucidfish eval` with a model we have no numbers for and post the
   results JSON from `evals/results/` in an issue, with your hardware and settings.
4. **A second language** for the coach's text (`lucidfish/prompts.py`, `lucidfish/spelling.py`).
5. **Bug reports** through the issue template, with the game (PGN) and the model that produced
   the faulty note where possible.

## Setting up

```bash
git clone https://github.com/s4mstruthers/Lucidfish.git
cd Lucidfish
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

You need **Stockfish** on your PATH (`brew install stockfish`, `apt install stockfish`, or
Settings → Engine → *Download Stockfish* inside the app) and, for a local coach, **Ollama** with a
model pulled (`ollama pull qwen3:8b`). Cloud providers work with an API key in `.env` (see
`.env.example`). Then:

```bash
lucidfish web          # the app at http://127.0.0.1:8420
pytest -q              # tests; engine tests skip themselves when Stockfish is missing
ruff check lucidfish tests
node --check lucidfish/static/app.js
lucidfish eval --limit 5   # a quick accuracy check of the configured coach
```

The desktop shell has its own build notes in [`desktop/README.md`](desktop/README.md); you do not
need it for most changes, since the page served by `lucidfish web` *is* the app.

## The one rule: the coach never asserts what the code has not verified

Lucidfish's whole value is that its explanations can be trusted. Every feature that touches the
coach follows the same pattern:

- The **facts** come from the engine or from code that inspects the board (`tactics.py`,
  `features.py`, `endgame.py`, `plans.py`, `lines.py`).
- The **model** receives those facts as structured evidence (`prompts.py`) and answers in a fixed
  JSON shape (`schemas.py`).
- The **output** is checked against the evidence (`review_check.py`, the fact-checker in
  `pipeline.py`); sentences that fail are corrected once, then removed.

So a change that lets the model say something new must also add the check that would catch it
being wrong, and an eval case that exercises it. A pull request that makes the coach more
fluent but less verifiable will be asked to add the verification first.

## Conventions

- **British English** in text shown to users, in comments and in the changelog (`spelling.py`
  keeps the coach's output consistent too).
- **Plain Python, plain JavaScript.** The front end has no build step on purpose; keep it that way.
- **Tests with every change** in `tests/`; `pytest` and `ruff` must pass on all three operating
  systems in CI.
- **Changelog entry** under `## Unreleased` in `CHANGELOG.md`, written for the people using the
  app (what they will notice), not for developers.
- **Commit messages** say what changed and why, in the imperative ("Detect rook lifts in the plan
  tracker").
- Every source file starts with the short licence notice you will find at the top of any module;
  copy it into new files.

## Pull requests

1. Open an issue first for anything larger than a bug fix, so the design can be agreed before the
   work is done.
2. Branch from `main`, keep the PR to one change, and fill in the PR template (what, why, how
   tested).
3. CI must be green. The `Desktop shell compiles` job catches version-number mismatches between
   the files that carry the version (`python desktop/scripts/check_version.py` lists them).

By contributing, you agree that your contribution is licensed under the same terms as Lucidfish,
the [GNU AGPL v3 or later](LICENSE), and that you have the right to license it that way. There is
no contributor licence agreement and no copyright assignment: you keep the copyright on your work.

## Releasing (maintainers)

Bump the version everywhere it appears (the steps, including `Cargo.lock` and `CITATION.cff`, are in
[`desktop/README.md`](desktop/README.md#releasing)), move the `Unreleased` section of `CHANGELOG.md`
under the new version heading, tag `vX.Y.Z` and push the tag; the `build-apps` workflow builds the
installers and publishes the release with that changelog section as its description.
