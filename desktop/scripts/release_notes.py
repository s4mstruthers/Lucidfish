#!/usr/bin/env python3
"""Print a version's section of CHANGELOG.md: the release's description.

    python desktop/scripts/release_notes.py 0.4.0 > notes.md
    python desktop/scripts/release_notes.py 0.4.0 --with-downloads > release.md

The build workflow uses this as the text of the GitHub release (with the download table on top, so
the four files people need stand out from the updater's files below), and the app shows the plain
section when it offers the update. It exits with an error when the changelog has no section for the
version, so a release is never published without saying what is new.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[2] / "CHANGELOG.md"


def section(version: str, text: str | None = None) -> str | None:
    """The text under ``## <version>`` (without the heading), or None when there is none."""
    text = CHANGELOG.read_text(encoding="utf-8") if text is None else text
    version = version.lstrip("vV")
    # The heading may carry a title after the version ("## 0.4.0 — the first app"); the suffix must stay
    # on the heading line, or it would eat a first bullet that starts with "-".
    match = re.search(rf"^##[ \t]+v?{re.escape(version)}[ \t]*(?:[-—(][^\n]*)?$(.*?)(?=^##\s|\Z)", text,
                      re.M | re.S)
    if match is None or not match.group(1).strip():
        return None
    return match.group(1).strip() + "\n"


DOWNLOADS = (("macOS, Apple silicon (M1 and later)", "macOS-arm64.dmg"),
             ("macOS, Intel", "macOS-x64.dmg"),
             ("Windows", "Windows-x64-setup.exe"),
             ("Linux", "Linux-x64.AppImage"))


def downloads_table(version: str, repo: str = "s4mstruthers/Lucidfish") -> str:
    """'Which file do I need?' for the top of a release page: one link per system."""
    version = version.lstrip("vV")
    rows = "\n".join(f"| {system} | [`Lucidfish-{version}-{suffix}`](https://github.com/{repo}/releases/download/"
                      f"v{version}/Lucidfish-{version}-{suffix}) |" for system, suffix in DOWNLOADS)
    return ("## Which file do I need?\n\n| Your computer | Download |\n|---|---|\n" + rows +
            "\n\nThe other files below (`.app.tar.gz`, `.sig`, `latest.json`) are what installed apps use to "
            "update themselves; there is no need to download them. First start on macOS or Windows: see "
            f"[the install notes](https://github.com/{repo}#download).\n\n## What's new\n\n")


def main(argv: list[str]) -> int:
    with_downloads = "--with-downloads" in argv
    argv = [a for a in argv if a != "--with-downloads"]
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    notes = section(argv[0])
    if notes is None:
        print(f"CHANGELOG.md has no section '## {argv[0].lstrip('vV')}': say what is new in this version "
              "before releasing it.", file=sys.stderr)
        return 1
    if with_downloads:
        notes = downloads_table(argv[0]) + notes
    sys.stdout.buffer.write(notes.encode("utf-8"))   # as UTF-8 bytes: Windows consoles choke on "→"
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
