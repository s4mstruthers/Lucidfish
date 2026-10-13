#!/usr/bin/env python3
# Copyright (C) 2026 Sam Struthers
# SPDX-License-Identifier: AGPL-3.0-or-later
# This file is part of Lucidfish <https://github.com/s4mstruthers/Lucidfish>; see LICENSE for the terms.
"""Check that every file carrying Lucidfish's version agrees (and matches a release tag, if given).

    python desktop/scripts/check_version.py          # the files agree with each other
    python desktop/scripts/check_version.py v0.4.0   # ... and with this tag

The version lives in lucidfish/__init__.py; desktop/src-tauri/tauri.conf.json, desktop/src-tauri/Cargo.toml
and desktop/package.json must repeat it (Tauri names the installers and the updater manifest after it), and
desktop/src-tauri/Cargo.lock must record it for the shell's own crate, or `cargo check --locked` refuses to
build (run `cargo update -w --offline` in desktop/src-tauri after bumping the version).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def versions() -> dict[str, str]:
    init = (ROOT / "lucidfish" / "__init__.py").read_text(encoding="utf-8")
    conf = json.loads((ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    pkg = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
    cargo = (ROOT / "desktop" / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
    lock = (ROOT / "desktop" / "src-tauri" / "Cargo.lock").read_text(encoding="utf-8")
    crate = re.search(r'^name\s*=\s*"([^"]+)"', cargo, re.M).group(1)
    locked = re.search(rf'\[\[package\]\]\s*\nname = "{re.escape(crate)}"\nversion = "([^"]+)"', lock)
    return {
        "lucidfish/__init__.py": re.search(r'__version__\s*=\s*"([^"]+)"', init).group(1),
        "desktop/src-tauri/tauri.conf.json": conf["version"],
        "desktop/src-tauri/Cargo.toml": re.search(r'^version\s*=\s*"([^"]+)"', cargo, re.M).group(1),
        "desktop/src-tauri/Cargo.lock": locked.group(1) if locked else "(missing)",
        "desktop/package.json": pkg["version"],
    }


def main(argv: list[str]) -> int:
    found = versions()
    distinct = set(found.values())
    for name, v in found.items():
        print(f"{v:10} {name}")
    if len(distinct) != 1:
        print("Versions differ: make them the same before releasing.")
        return 1
    tag = argv[0].strip() if argv else ""
    if tag and tag.lstrip("v") != next(iter(distinct)):
        print(f"The tag {tag} does not match version {next(iter(distinct))}.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
