#!/usr/bin/env python3
# Copyright (C) 2026 Sam Struthers
# SPDX-License-Identifier: AGPL-3.0-or-later
# This file is part of Lucidfish <https://github.com/s4mstruthers/Lucidfish>; see LICENSE for the terms.
"""Record a short, captioned demo video of Lucidfish for a post or a README.

    python demo/record_demo.py --app http://127.0.0.1:8420 --rename "s4mstruthers=You"
                                                    # record your running app, games already analysed
    python demo/record_demo.py                      # the configured coach (Ollama by default)
    python demo/record_demo.py --no-coach           # engine-only, for a quick draft
    python demo/record_demo.py --provider gemini    # a cloud coach (its key must be set up already)

With ``--app`` it records the Lucidfish you already have open (desktop or ``lucidfish web``): no
analysis, no waiting. It only looks: the recording browser has its own storage, so your Train
progress is untouched. ``--rename OLD=NEW`` (repeatable, case-insensitive) swaps names on screen while
it records, for usernames and opponents you would rather not publish.

Without ``--app``, what it does, in order:

1. Starts ``lucidfish web`` on its own port with a throw-away data folder, so your real profile and
   games are never touched. If that folder has no Stockfish and none is on the PATH, it downloads one
   the same way Settings → Analysis does.
2. Creates a profile and analyses the demo games (``--pgn``, default: the Opera game from the player's
   side that loses, plus one club game), waiting until the analysis, and the coach, are done. With a
   local coach this takes 5 to 15 minutes; the slow part comes after the last move (mistake notes,
   chapters, review), and the printed stage says what is happening. ``--detail key`` is much faster.
3. Opens the app in a scripted Chromium window (Playwright) and records a tour: title card, Home, the
   game as a story, a mistake and its explanation, Learn, Train, closing card. Captions are drawn
   into the page, so they are part of the recording.
4. Converts the recording to an MP4 (H.264, 1920×1080, 30 fps) with ffmpeg and saves a poster frame.

Requirements: ``pip install playwright`` and ``playwright install chromium`` once; ``ffmpeg`` on the
PATH (``brew install ffmpeg``). Everything else is Lucidfish's own.
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"x-lucidfish": "1"}                     # the server's cross-site guard wants this on every POST

# The games the demo analyses: (file, the side the profile "played", rating for the opening peers).
DEFAULT_GAMES = [
    (ROOT / "examples" / "sample.pgn", "black", 1200),
    (ROOT / "evals" / "games" / "legal_trap_1750.pgn", "black", 1750),
]

# ------------------------------------------------------------------ the server ----

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(data_dir: Path, port: int) -> subprocess.Popen:
    """Run ``lucidfish web`` as a child process and wait until /api/health answers."""
    env = {**os.environ, "LUCIDFISH_DATA_DIR": str(data_dir), "LUCIDFISH_PORT": str(port)}
    log = open(data_dir / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "lucidfish.web", "--no-browser", "--port", str(port)],
                            env=env, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        time.sleep(0.5)
        try:
            if requests.get(f"{base}/api/health", timeout=2).ok:
                return proc
        except requests.RequestException:
            pass
        if proc.poll() is not None:
            break
    proc.kill()
    raise SystemExit(f"The server did not start; see {data_dir / 'server.log'}")


def ensure_engine(base: str) -> None:
    """Make sure Stockfish is available: on the PATH, or downloaded into the demo's data folder."""
    health = requests.get(f"{base}/api/health?refresh=true", timeout=10).json()
    if health["engine"]["ok"]:
        print(f"engine: {health['engine']['name']}")
        return
    print("engine: none found, downloading Stockfish (one-off, about 40 MB)…")
    requests.post(f"{base}/api/engine/download", headers=HEADERS, timeout=10)
    while True:
        st = requests.get(f"{base}/api/engine/download", timeout=10).json()
        if st["status"] == "done":
            print(f"engine: {st['message']}")
            return
        if st["status"] == "error":
            raise SystemExit(f"Stockfish download failed: {st['error']}")
        time.sleep(1)


def configure(base: str, args: argparse.Namespace) -> bool:
    """Apply the coach settings for the demo. Returns whether a coach will write."""
    settings = {"llm_enabled": not args.no_coach, "detail": args.detail}
    if args.provider:
        settings["provider"] = args.provider
    if args.model:
        settings["model"] = args.model
    r = requests.post(f"{base}/api/settings", json=settings, headers=HEADERS, timeout=10)
    if not r.ok:
        raise SystemExit(f"settings rejected: {r.text}")
    health = requests.get(f"{base}/api/health?refresh=true", timeout=30).json()
    coach = health["coach"]
    if args.no_coach:
        print("coach: off (engine-only draft)")
        return False
    if not coach["ok"]:
        raise SystemExit(f"coach: {coach['message']}\nStart it, or run with --no-coach for an engine-only draft.")
    print(f"coach: {coach['label']} · {coach['model']}")
    return True


def analyse_games(base: str, games: list[tuple[Path, str | None, int]], name: str) -> list[int]:
    """Create the demo profile, queue the games and wait; returns the stored game ids in order."""
    requests.post(f"{base}/api/profiles", json={"name": name, "level": "club"}, headers=HEADERS, timeout=10)
    jobs = []
    for path, side, elo in games:
        pgn = path.read_text(encoding="utf-8")
        r = requests.post(f"{base}/api/analyse", json={"pgn": pgn, "side": side, "elo": elo},
                          headers=HEADERS, timeout=10).json()
        jobs.append((path.name, r["job_id"]))
    ids = []
    t0 = time.time()
    for label, jid in jobs:
        last, last_print = "", 0.0
        while True:
            j = requests.get(f"{base}/api/job/{jid}?since=0", timeout=10).json()
            total = j.get("total") or "?"
            # What the pipeline is doing right now: the engine runs ahead of the coach, and after the last
            # move the coach still writes the mistake notes, the chapters and the review, so the move count
            # can stand still for minutes while real work happens. The job's own label says which part.
            stage = j.get("label") or j["status"]
            eta = j.get("eta_s")
            msg = (f"{label}: engine {j.get('engine_done', 0)}/{total} · coach {j.get('done', 0)}/{total} · "
                   f"{stage}" + (f" · about {eta / 60:.0f} min left" if eta and eta > 90 else ""))
            now = time.time()
            if msg != last or now - last_print > 20:
                print(f"  {msg} ({now - t0:.0f}s)", flush=True)
                last, last_print = msg, now
            if j["status"] not in ("queued", "running"):
                break
            time.sleep(3)
        if j["status"] != "done":
            raise SystemExit(f"{label}: analysis ended with status {j['status']}: {j.get('error', '')}")
        ids.append(j["game_id"])
    return ids


def pick_game(base: str) -> int:
    """The analysed game with the most to show: most mistakes and blunders, then the most recent."""
    games = requests.get(f"{base}/api/profile", timeout=15).json().get("games") or []
    if not games:
        raise SystemExit("The running app's active profile has no analysed games yet.")
    best = max(games, key=lambda g: ((g.get("mistakes") or 0) + (g.get("blunders") or 0), g.get("analysed_at") or ""))
    print(f"game: #{best['id']} {best.get('white')} vs {best.get('black')} ({best.get('date')}); "
          "use --game N for another")
    return best["id"]


# ------------------------------------------------------------------ the tour ----

CAPTION_CSS = """
#demo-cap { position: fixed; left: 50%; bottom: 36px; transform: translateX(-50%); max-width: 72%;
  padding: 14px 22px; border-radius: 14px; background: rgba(17, 24, 39, 0.88); color: #fff;
  font: 500 22px/1.35 -apple-system, "Segoe UI", Inter, Roboto, sans-serif; letter-spacing: 0.1px;
  text-align: center; opacity: 0; transition: opacity 0.35s ease; z-index: 99999; pointer-events: none;
  box-shadow: 0 10px 30px rgba(0,0,0,0.25); }
#demo-cap.on { opacity: 1; }
"""

CARD_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%%;background:#0f172a;color:#fff;
  font-family:-apple-system,"Segoe UI",Inter,Roboto,sans-serif}
.wrap{height:100%%;display:flex;flex-direction:column;align-items:center;justify-content:center;
  text-align:center;padding:0 10%%}
.logo{display:flex;align-items:center;gap:18px;margin-bottom:28px}
.mark{width:72px;height:72px;border-radius:20px;background:#4f46e5;display:flex;align-items:center;justify-content:center;font-size:40px}
.name{font-size:56px;font-weight:700;letter-spacing:-1px}.name b{color:#a5b4fc;font-weight:700}
h1{font-size:40px;font-weight:600;margin:0 0 18px;line-height:1.2}
p{font-size:24px;color:#cbd5e1;margin:0;line-height:1.45}
.foot{position:absolute;bottom:40px;font-size:20px;color:#94a3b8}
</style></head><body><div class="wrap">
<div class="logo"><div class="mark">♞</div><div class="name">Lucid<b>fish</b></div></div>
<h1>%(title)s</h1><p>%(sub)s</p></div><div class="foot">%(foot)s</div></body></html>"""


RENAME_JS = """(pairs) => {
    // Swap names in the visible text, now and whenever the app redraws (it re-renders on every page).
    const rules = pairs.map(([a, b]) => [new RegExp(a.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&'), 'gi'), b]);
    const fix = (root) => {
        const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        for (let n = walk.nextNode(); n; n = walk.nextNode()) {
            let t = n.nodeValue, u = t;
            for (const [re, b] of rules) u = u.replace(re, b);
            if (u !== t) n.nodeValue = u;
        }
        // The profile avatar is the name's first letter, so it has to follow the renamed name.
        const av = document.getElementById('accountAvatar'), nm = document.getElementById('accountName');
        const first = nm && nm.textContent.trim().charAt(0).toUpperCase();
        if (av && first && av.textContent !== first) av.textContent = first;
    };
    const start = () => {
        fix(document.body);
        new MutationObserver((ms) => ms.forEach((m) => m.addedNodes.forEach((x) =>
            fix(x.nodeType === 3 ? x.parentNode || document.body : x)))).observe(document.body,
            {childList: true, subtree: true, characterData: true});
        document.title = rules.reduce((t, [re, b]) => t.replace(re, b), document.title);
    };
    if (document.body) start(); else document.addEventListener('DOMContentLoaded', start);
}"""


def card(page, title: str, sub: str, foot: str, seconds: float) -> None:
    global _needs_reload
    page.set_content(CARD_HTML % {"title": title, "sub": sub, "foot": foot})
    _needs_reload = True                 # the app is gone from this tab until open_route() loads it again
    page.wait_for_timeout(int(seconds * 1000))


def caption(page, text: str) -> None:
    """Show a caption bar over the page (injected once; survives hash navigation)."""
    page.evaluate("""([css, text]) => {
        if (!document.getElementById('demo-cap-style')) {
            const s = document.createElement('style'); s.id = 'demo-cap-style'; s.textContent = css;
            document.head.appendChild(s);
        }
        let el = document.getElementById('demo-cap');
        if (!el) { el = document.createElement('div'); el.id = 'demo-cap'; document.body.appendChild(el); }
        el.classList.remove('on'); el.textContent = text;
        requestAnimationFrame(() => el.classList.add('on'));
    }""", [CAPTION_CSS, text])


def uncaption(page) -> None:
    page.evaluate("document.getElementById('demo-cap')?.remove()")


_needs_reload = True


def open_route(page, base: str, route: str) -> None:
    """Go to a page of the app (``#/learn``). After a title card the document is not the app any more,
    and a hash-only change would not reload it, so a fresh load with a unique query string is forced."""
    global _needs_reload
    if not _needs_reload:
        page.goto(f"{base}/{route}")
    else:
        page.goto(f"{base}/?load={int(time.time() * 1000)}{route}")
        page.wait_for_timeout(900)
        _needs_reload = False


def scroll(page, y: int, seconds: float = 1.6) -> None:
    page.evaluate("y => window.scrollTo({top: y, behavior: 'smooth'})", y)
    page.wait_for_timeout(int(seconds * 1000))


def into_view(page, selector: str, seconds: float = 1.2) -> None:
    page.evaluate("s => document.querySelector(s)?.scrollIntoView({behavior: 'smooth', block: 'center'})", selector)
    page.wait_for_timeout(int(seconds * 1000))


def tour(page, base: str, game_id: int, with_coach: bool) -> None:
    """The demo itself. Timings add up to roughly 70 seconds."""
    # 1. Title card.
    card(page, "Stockfish tells you the best move.<br>Lucidfish tells you <em>why</em>.",
         "Your chess games, reviewed like a coach and told like a story. Free, open source, on your own computer.",
         "github.com/s4mstruthers/Lucidfish", 4.5)

    # 2. Home.
    open_route(page, base, "#/dashboard")
    page.wait_for_selector("#stats .stat, #stats div", timeout=15000)
    page.wait_for_timeout(1200)
    caption(page, "Home: accuracy by phase, the mistakes you keep making, and what to work on next.")
    page.wait_for_timeout(2600)
    scroll(page, 520, 2.2)
    scroll(page, 0, 1.4)

    # 3. The game, move by move.
    uncaption(page)
    open_route(page, base, f"#/game/{game_id}")
    page.wait_for_selector("#moveList .mv, #moveList button, #moveList div", timeout=15000)
    page.wait_for_timeout(1200)
    caption(page, "Every move of both players gets a verdict, measured in winning chances, the way Lichess does it.")
    for _ in range(6):
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(550)
    page.wait_for_timeout(600)

    # 4. The Story tab.
    page.click("#moveViewSeg [data-view=story]")
    page.wait_for_timeout(700)
    caption(page, "Read the game as a story: chapters, and what each move was for."
            if with_coach else "Read the game as a story, split into chapters at its turning points.")
    into_view(page, "#storyList", 1.0)
    page.evaluate("""() => { const el = document.getElementById('storyList');
        if (el) el.scrollTo({top: el.scrollHeight * 0.45, behavior: 'smooth'}); }""")
    page.wait_for_timeout(2600)

    # 5. A mistake and its explanation.
    page.click("#moveViewSeg [data-view=moves]")
    page.wait_for_timeout(400)
    page.click("#btnNextKey")
    page.wait_for_timeout(500)
    page.click("#btnNextKey")
    page.wait_for_timeout(700)
    caption(page, "Every mistake is explained: why, what was better, and the line that punishes it. "
                  "Every sentence is checked against the board." if with_coach else
            "Every mistake comes with the better move and the line that punishes it, verified by the engine.")
    into_view(page, "#explain", 1.4)
    if page.query_selector("#explain [data-reveal]"):      # "hide answers until I try" is on by default
        page.click("#explain [data-reveal]")
        page.wait_for_timeout(600)
        into_view(page, "#explain", 0.8)
    page.wait_for_timeout(4200)

    # 6. Learn.
    uncaption(page)
    open_route(page, base, "#/learn")
    page.wait_for_selector("#learnCategories", timeout=15000)
    page.wait_for_timeout(1000)
    caption(page, "Learn: each concept explained in plain language, with your own games as the examples.")
    page.wait_for_timeout(2200)
    scroll(page, 420, 2.0)

    # 7. Train.
    uncaption(page)
    open_route(page, base, "#/train")
    page.wait_for_selector("#trainStart", timeout=15000)
    page.wait_for_timeout(1000)
    caption(page, "Train: your mistakes become puzzles, and spaced repetition brings them back until they stick.")
    page.wait_for_timeout(2000)
    if page.is_enabled("#trainStart"):
        page.click("#trainStart")
        page.wait_for_timeout(3200)
    else:
        page.wait_for_timeout(1500)

    # 8. Closing card.
    card(page, "Free. Open source. Runs on your computer.",
         "Stockfish for the truth, a local or cloud model for the words, a fact-checker in between.<br>"
         "macOS · Windows · Linux",
         "github.com/s4mstruthers/Lucidfish", 5.0)


def record(base: str, game_id: int, with_coach: bool, out_dir: Path, width: int, height: int,
           rename: list[tuple[str, str]] | None = None) -> Path:
    from playwright.sync_api import sync_playwright
    out_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        warm = browser.new_context(viewport={"width": width, "height": height})
        wp = warm.new_page()
        wp.goto(f"{base}/")                 # fonts, scripts and the WASM engine are cached after this
        wp.wait_for_timeout(1500)
        warm.close()
        size = {"width": width, "height": height}
        context = browser.new_context(viewport=size, record_video_dir=str(out_dir), record_video_size=size,
                                      color_scheme="light")
        if rename:
            import json
            context.add_init_script(f"({RENAME_JS})({json.dumps(rename)})")
        page = context.new_page()
        tour(page, base, game_id, with_coach)
        video = page.video
        context.close()
        browser.close()
        return Path(video.path())


def encode(webm: Path, out: Path, poster: Path) -> None:
    """Scale to 1080p, re-encode to H.264 (what every site and player accepts), and grab a poster frame."""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(webm),
                    "-vf", "scale=1920:1080:flags=lanczos,format=yuv420p", "-r", "30",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-movflags", "+faststart", str(out)],
                   check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1.5", "-i", str(out), "-frames:v", "1", str(poster)],
                   check=True)


# ------------------------------------------------------------------ main ----

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app", help="record the Lucidfish already running at this address, e.g. http://127.0.0.1:8420")
    ap.add_argument("--game", type=int, help="with --app: the game to feature (default: the one with most mistakes)")
    ap.add_argument("--rename", action="append", default=[], metavar="OLD=NEW",
                    help="replace a name on screen while recording (repeatable)")
    ap.add_argument("--pgn", action="append", type=Path, help="game(s) to analyse (default: two built-in games)")
    ap.add_argument("--side", default="black", help="which side the profile played in --pgn games")
    ap.add_argument("--no-coach", action="store_true", help="engine-only: no written explanations")
    ap.add_argument("--provider", help="coach provider id (ollama, openai, anthropic, gemini, …)")
    ap.add_argument("--model", help="coach model name")
    ap.add_argument("--detail", default="standard", choices=["key", "standard", "full"], help="coaching detail")
    ap.add_argument("--name", default="Sam", help="profile name shown on Home")
    ap.add_argument("--out", type=Path, default=ROOT / "demo" / "out" / "lucidfish-demo.mp4")
    ap.add_argument("--size", default="1600x900", help="recording viewport; the MP4 is always 1920x1080")
    ap.add_argument("--data-dir", type=Path, help="reuse this data folder instead of a temporary one")
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed (brew install ffmpeg / apt install ffmpeg).")
    width, height = (int(v) for v in args.size.lower().split("x"))
    games = [(p, args.side, 1200) for p in args.pgn] if args.pgn else DEFAULT_GAMES

    rename = []
    for pair in args.rename:
        if "=" not in pair:
            raise SystemExit(f"--rename wants OLD=NEW, got {pair!r}")
        old, new = pair.split("=", 1)
        rename.append((old.strip(), new.strip()))

    if args.app:
        base = args.app.rstrip("/")
        try:
            health = requests.get(f"{base}/api/health", timeout=5).json()
        except requests.RequestException:
            raise SystemExit(f"No Lucidfish answers at {base}. "
                             "Copy the address from the app's window or terminal.") from None
        game_id = args.game or pick_game(base)
        with_coach = bool(health.get("coach", {}).get("enabled"))
        print("recording…")
        webm = record(base, game_id, with_coach, args.out.parent, width, height, rename)
        poster = args.out.with_suffix(".png")
        encode(webm, args.out, poster)
        webm.unlink(missing_ok=True)
        print(f"done: {args.out} (poster {poster.name})")
        return 0

    temp = None
    if args.data_dir:
        data_dir = args.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
    else:
        temp = tempfile.TemporaryDirectory(prefix="lucidfish-demo-")
        data_dir = Path(temp.name)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    print(f"server: {base} · data: {data_dir}")
    proc = start_server(data_dir, port)
    try:
        ensure_engine(base)
        with_coach = configure(base, args)
        print("analysing the demo games…")
        ids = analyse_games(base, games, args.name)
        print("recording…")
        webm = record(base, ids[0], with_coach, args.out.parent, width, height, rename)
        poster = args.out.with_suffix(".png")
        encode(webm, args.out, poster)
        webm.unlink(missing_ok=True)
        print(f"done: {args.out} (poster {poster.name})")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if temp:
            temp.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
