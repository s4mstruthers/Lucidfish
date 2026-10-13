#!/usr/bin/env python3
# Copyright (C) 2026 Sam Struthers
# SPDX-License-Identifier: AGPL-3.0-or-later
# This file is part of Lucidfish <https://github.com/s4mstruthers/Lucidfish>; see LICENSE for the terms.
"""Download the official Stockfish build for a target and bundle it with the desktop app.

    python desktop/scripts/fetch_stockfish.py                       # for this computer
    python desktop/scripts/fetch_stockfish.py --target x86_64-pc-windows-msvc

The engine lands in `desktop/src-tauri/resources/engine/` and is copied into the app
bundle (see `bundle.resources` in tauri.conf.json). At first launch the app copies it
into the data folder and tells the backend where it is. The release and asset names
come from `lucidfish.engine_setup`, the same module that powers the in-app download.
"""
from __future__ import annotations

import argparse
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lucidfish import engine_setup  # noqa: E402

OUT = ROOT / "desktop" / "src-tauri" / "resources" / "engine"

# Rust target triple → (system, arch) as engine_setup.ASSETS keys them.
TRIPLES = {
    "aarch64-apple-darwin": ("darwin", "arm64"),
    "x86_64-apple-darwin": ("darwin", "x86_64"),
    "x86_64-pc-windows-msvc": ("win32", "x86_64"),
    "x86_64-unknown-linux-gnu": ("linux", "x86_64"),
}


def host_triple() -> str:
    out = subprocess.run(["rustc", "-vV"], check=True, capture_output=True, text=True).stdout
    return next(line.split(":", 1)[1].strip() for line in out.splitlines() if line.startswith("host:"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", help="Rust target triple (default: this computer's)")
    parser.add_argument("--asset", type=int, default=0,
                        help="which build to take from the preference list (0 = fastest, 1 = most compatible)")
    args = parser.parse_args()
    triple = args.target or host_triple()
    key = TRIPLES.get(triple)
    if key is None or key not in engine_setup.ASSETS:
        raise SystemExit(f"No official Stockfish build is known for {triple}.")
    asset = engine_setup.ASSETS[key][min(args.asset, len(engine_setup.ASSETS[key]) - 1)]
    url = engine_setup.RELEASE_URL + asset
    print(f"Downloading {url}", flush=True)
    archive = engine_setup._download(url, lambda p: None)
    binary = engine_setup._member_bytes(archive, asset)
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("stockfish*"):
        old.unlink()
    target = OUT / ("stockfish.exe" if key[0] == "win32" else "stockfish")
    target.write_bytes(binary)
    if key[0] != "win32":
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (OUT / "VERSION.txt").write_text(f"{engine_setup.STOCKFISH_RELEASE} {asset}\n", encoding="utf-8")
    print(f"Bundled {target} ({len(binary) / 1e6:.1f} MB) from {asset}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
