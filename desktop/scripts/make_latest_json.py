#!/usr/bin/env python3
# Copyright (C) 2026 Sam Struthers
# SPDX-License-Identifier: AGPL-3.0-or-later
# This file is part of Lucidfish <https://github.com/s4mstruthers/Lucidfish>; see LICENSE for the terms.
"""Write the updater manifest (`latest.json`) for a release from the files in `dist/`.

    python desktop/scripts/make_latest_json.py v0.4.0 --notes notes.md --out dist/latest.json

The installed app fetches `releases/latest/download/latest.json` and installs the file named for
its platform when the version is newer. Each entry carries the minisign signature that `tauri
build` wrote next to the artifact (the `.sig` files collect_bundles.py copied), so an app only
installs updates signed with the key whose public half is built into it.

Platforms without a signature are left out: the manifest then simply offers them nothing. When no
platform is signed at all (the signing secret is not set), no manifest is written and the release
has to be installed by hand.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from collect_bundles import DIST, TARGETS

REPO = "s4mstruthers/Lucidfish"
# Updater platform key → the suffix of the file the updater downloads for it.
UPDATER_FILES = {
    "darwin-aarch64": "macOS-arm64.app.tar.gz",
    "darwin-x86_64": "macOS-x64.app.tar.gz",
    "windows-x86_64": "Windows-x64-setup.exe",
    "linux-x86_64": "Linux-x64.AppImage",
}


def manifest(tag: str, dist: Path, notes: str = "", repo: str = REPO) -> dict | None:
    version = tag.lstrip("vV")
    platforms = {}
    for key, suffix in UPDATER_FILES.items():
        artifact = dist / f"Lucidfish-{version}-{suffix}"
        sig = artifact.with_name(artifact.name + ".sig")
        if artifact.is_file() and sig.is_file():
            platforms[key] = {
                "signature": sig.read_text(encoding="utf-8").strip(),
                "url": f"https://github.com/{repo}/releases/download/{tag}/{artifact.name}",
            }
    if not platforms:
        return None
    return {
        "version": version,
        "notes": notes.strip(),
        "pub_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platforms": platforms,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tag", help="the release tag, e.g. v0.4.0")
    parser.add_argument("--dist", default=str(DIST))
    parser.add_argument("--notes", help="file with the release notes (shown by the app before updating)")
    parser.add_argument("--out", help="where to write the manifest (default: <dist>/latest.json)")
    args = parser.parse_args(argv)
    dist = Path(args.dist)
    notes = Path(args.notes).read_text(encoding="utf-8") if args.notes else ""
    data = manifest(args.tag, dist, notes)
    if data is None:
        print("No signed artifacts in dist/: no latest.json written (set TAURI_SIGNING_PRIVATE_KEY and "
              "TAURI_SIGNING_PRIVATE_KEY_PASSWORD so installed apps can update to future releases).",
              file=sys.stderr)
        return 0
    out = Path(args.out) if args.out else dist / "latest.json"
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    missing = [label for triple, (label, key) in TARGETS.items() if key not in data["platforms"]]
    print(f"Wrote {out} for {', '.join(data['platforms'])}" + (f" (unsigned: {', '.join(missing)})" if missing else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
