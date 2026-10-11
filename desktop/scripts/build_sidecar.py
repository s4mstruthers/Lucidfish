#!/usr/bin/env python3
"""Build the backend into one executable and put it where Tauri expects a sidecar.

    python desktop/scripts/build_sidecar.py            # for this computer
    python desktop/scripts/build_sidecar.py --target aarch64-apple-darwin

Tauri looks for `desktop/src-tauri/binaries/lucidfish-server-<target triple>` (with `.exe`
on Windows). The target triple defaults to the host's, as reported by `rustc -vV`.
Requires PyInstaller in the current Python environment (`pip install pyinstaller`).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "desktop"
BIN = DESKTOP / "src-tauri" / "binaries"


def host_triple() -> str:
    env = os.environ.get("TAURI_ENV_TARGET_TRIPLE") or os.environ.get("CARGO_BUILD_TARGET")
    if env:
        return env
    out = subprocess.run(["rustc", "-vV"], check=True, capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise SystemExit("Could not determine the target triple: pass --target.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", help="Rust target triple (default: this computer's)")
    parser.add_argument("--python", default=sys.executable, help="Python with PyInstaller and Lucidfish installed")
    args = parser.parse_args()
    triple = args.target or host_triple()
    work = DESKTOP / "build"
    dist = work / "dist"
    shutil.rmtree(dist, ignore_errors=True)
    cmd = [args.python, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--distpath", str(dist), "--workpath", str(work / "work"), str(DESKTOP / "lucidfish-server.spec")]
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)
    ext = ".exe" if sys.platform == "win32" else ""
    built = dist / f"lucidfish-server{ext}"
    if not built.is_file():
        raise SystemExit(f"PyInstaller did not produce {built}")
    BIN.mkdir(parents=True, exist_ok=True)
    target = BIN / f"lucidfish-server-{triple}{ext}"
    shutil.copy2(built, target)
    if ext == "":
        target.chmod(0o755)
    print(f"Sidecar ready: {target} ({target.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
