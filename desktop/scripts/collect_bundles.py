#!/usr/bin/env python3
"""Gather what `tauri build` produced into `dist/`, named for people and for the updater.

    python desktop/scripts/collect_bundles.py --target aarch64-apple-darwin

Tauri writes installers under `desktop/src-tauri/target[/<triple>]/release/bundle/<kind>/`, with
names such as `Lucidfish_0.4.0_aarch64.dmg` or `Lucidfish.app.tar.gz` (the same on both Macs). This
copies them to `dist/` as

    Lucidfish-<version>-macOS-arm64.dmg            Lucidfish-<version>-macOS-arm64.app.tar.gz (+ .sig)
    Lucidfish-<version>-macOS-x64.dmg              Lucidfish-<version>-macOS-x64.app.tar.gz   (+ .sig)
    Lucidfish-<version>-Windows-x64-setup.exe (+ .sig)   Lucidfish-<version>-Windows-x64.msi
    Lucidfish-<version>-Linux-x64.AppImage (+ .sig)      Lucidfish-<version>-Linux-x64.deb / .rpm

The `.sig` files exist only when the build was signed (TAURI_SIGNING_PRIVATE_KEY); they are what
`make_latest_json.py` turns into the updater manifest.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TAURI = ROOT / "desktop" / "src-tauri"
DIST = ROOT / "dist"

# Rust target triple → (platform label for file names, updater platform key).
TARGETS = {
    "aarch64-apple-darwin": ("macOS-arm64", "darwin-aarch64"),
    "x86_64-apple-darwin": ("macOS-x64", "darwin-x86_64"),
    "x86_64-pc-windows-msvc": ("Windows-x64", "windows-x86_64"),
    "x86_64-unknown-linux-gnu": ("Linux-x64", "linux-x86_64"),
}

# (bundle folder, glob, suffix of the file in dist/). The updater artifact of each platform is
# listed first; its `.sig` is copied alongside when present.
KINDS = [
    ("macos", "*.app.tar.gz", ".app.tar.gz"),
    ("dmg", "*.dmg", ".dmg"),
    ("nsis", "*-setup.exe", "-setup.exe"),
    ("msi", "*.msi", ".msi"),
    ("appimage", "*.AppImage", ".AppImage"),
    ("deb", "*.deb", ".deb"),
    ("rpm", "*.rpm", ".rpm"),
]


def version() -> str:
    return json.loads((TAURI / "tauri.conf.json").read_text(encoding="utf-8"))["version"]


def bundle_dirs(triple: str) -> list[Path]:
    return [p for p in (TAURI / "target" / triple / "release" / "bundle", TAURI / "target" / "release" / "bundle")
            if p.is_dir()]


def collect(triple: str, out: Path = DIST) -> list[Path]:
    label, _ = TARGETS[triple]
    out.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for folder, pattern, suffix in KINDS:
        for bundles in bundle_dirs(triple):
            found = sorted((bundles / folder).glob(pattern))
            if not found:
                continue
            for src in found:
                target = out / f"Lucidfish-{version()}-{label}{suffix}"
                shutil.copy2(src, target)
                copied.append(target)
                sig = src.with_name(src.name + ".sig")
                if sig.is_file():
                    shutil.copy2(sig, target.with_name(target.name + ".sig"))
                    copied.append(target.with_name(target.name + ".sig"))
            break          # the first bundle folder that has this kind wins (per-target before generic)
    return copied


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", required=True, choices=sorted(TARGETS))
    parser.add_argument("--out", default=str(DIST))
    args = parser.parse_args(argv)
    copied = collect(args.target, Path(args.out))
    if not copied:
        print(f"No bundles found under {TAURI / 'target'} for {args.target}: did `tauri build` run?", file=sys.stderr)
        return 1
    for path in copied:
        print(f"{path.stat().st_size / 1e6:8.1f} MB  {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
