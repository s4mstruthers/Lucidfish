# Copyright (C) 2026 Sam Struthers
# SPDX-License-Identifier: AGPL-3.0-or-later
# This file is part of Lucidfish <https://github.com/s4mstruthers/Lucidfish>; see LICENSE for the terms.

"""Download and install Stockfish from inside the app.

The desktop app ships with Stockfish, and most Linux distributions package it,
but someone running Lucidfish from a `pip install` (or whose bundled engine was
built for a newer CPU than theirs) needs a way to get the engine without a
terminal. This module fetches an official build from the Stockfish GitHub
releases, unpacks it into the data folder, checks that it actually starts, and
saves its path in the settings.

Builds are tried from fastest to most compatible: an AVX2 build first (every
x86-64 processor since about 2013), then an SSE4.1 build if the first one
cannot run on this processor. The download runs in a background thread; the
web page polls :func:`status` for progress.
"""

from __future__ import annotations

import io
import os
import platform
import stat
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path

import chess.engine
import requests

from .config import IS_WINDOWS, engine_dir

STOCKFISH_RELEASE = "sf_17.1"
RELEASE_URL = f"https://github.com/official-stockfish/Stockfish/releases/download/{STOCKFISH_RELEASE}/"

# Per platform: the official release assets to try, in order of preference.
ASSETS: dict[tuple[str, str], list[str]] = {
    ("darwin", "arm64"): ["stockfish-macos-m1-apple-silicon.tar"],
    ("darwin", "x86_64"): ["stockfish-macos-x86-64-avx2.tar", "stockfish-macos-x86-64-sse41-popcnt.tar"],
    ("win32", "x86_64"): ["stockfish-windows-x86-64-avx2.zip", "stockfish-windows-x86-64-sse41-popcnt.zip"],
    ("linux", "x86_64"): ["stockfish-ubuntu-x86-64-avx2.tar", "stockfish-ubuntu-x86-64-sse41-popcnt.tar"],
}

_lock = threading.Lock()
_state: dict = {"status": "idle", "progress": 0.0, "message": "", "path": "", "error": ""}
_thread: threading.Thread | None = None


def _arch() -> str:
    m = platform.machine().lower()
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "x86_64"
    return m


def platform_key() -> tuple[str, str]:
    system = "win32" if IS_WINDOWS else "darwin" if sys.platform == "darwin" else "linux"
    return system, _arch()


def available() -> bool:
    """Is there an official build for this computer?"""
    return platform_key() in ASSETS


def status() -> dict:
    with _lock:
        return {**_state, "available": available(), "release": STOCKFISH_RELEASE}


def _set(**fields) -> None:
    with _lock:
        _state.update(fields)


def _download(url: str, on_progress) -> bytes:
    with requests.get(url, stream=True, timeout=60) as r:
        if r.status_code == 404:
            raise RuntimeError("That build is no longer offered for download.")
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        buf, got, last = io.BytesIO(), 0, 0.0
        for chunk in r.iter_content(chunk_size=256 * 1024):
            buf.write(chunk)
            got += len(chunk)
            if total and time.monotonic() - last > 0.1:
                on_progress(got / total)
                last = time.monotonic()
        return buf.getvalue()


def _member_bytes(archive: bytes, name: str) -> bytes:
    """The engine binary inside an official .tar or .zip (the archive holds a `stockfish/` folder)."""
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            for info in z.infolist():
                base = Path(info.filename).name
                if base.startswith("stockfish") and base.endswith(".exe") and not info.is_dir():
                    return z.read(info)
    else:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as t:
            for info in t.getmembers():
                base = Path(info.name).name
                if info.isfile() and base.startswith("stockfish") and "." not in base and "/" not in base:
                    f = t.extractfile(info)
                    if f is not None:
                        return f.read()
    raise RuntimeError("The downloaded archive did not contain a Stockfish program.")


def _install(binary: bytes) -> Path:
    folder = engine_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ("stockfish.exe" if IS_WINDOWS else "stockfish")
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(binary)
    if not IS_WINDOWS:
        tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(tmp, target)
    return target


def verify(path: str | os.PathLike) -> str:
    """Start the engine once and return its name. Raises RuntimeError if it cannot run here."""
    try:
        engine = chess.engine.SimpleEngine.popen_uci(str(path), timeout=20)
    except (OSError, chess.engine.EngineError, chess.engine.EngineTerminatedError) as e:
        raise RuntimeError(f"The engine could not be started: {e}") from e
    try:
        return engine.id.get("name", "Stockfish")
    finally:
        try:
            engine.quit()
        except Exception:   # pragma: no cover - the process may already be gone
            pass


def _run() -> None:
    from . import settings
    key = platform_key()
    assets = ASSETS.get(key, [])
    if not assets:
        _set(status="error", error=f"There is no official Stockfish download for {key[0]} / {key[1]}. "
             "Install it with your package manager and set its path in Settings.")
        return
    last_error = ""
    for n, asset in enumerate(assets):
        label = "fastest build" if n == 0 else "more compatible build"
        try:
            _set(status="downloading", progress=0.0, message=f"Downloading Stockfish ({label})…", error="")
            archive = _download(RELEASE_URL + asset, lambda p: _set(progress=round(p, 3)))
            _set(status="installing", progress=1.0, message="Unpacking…")
            target = _install(_member_bytes(archive, asset))
            _set(message="Checking that it runs on this computer…")
            name = verify(target)
        except (requests.RequestException, OSError) as e:
            last_error = f"Download failed: {e}"
            break
        except RuntimeError as e:
            last_error = str(e)
            continue   # try the next (more compatible) build
        settings.save({"stockfish_path": str(target)})
        _set(status="done", progress=1.0, path=str(target), message=f"{name} is installed and selected.")
        return
    _set(status="error", error=last_error or "No build of Stockfish could run on this computer.")


def start() -> dict:
    """Begin the download in the background (a no-op while one is already running)."""
    global _thread
    with _lock:
        running = _thread is not None and _thread.is_alive()
    if not running:
        _set(status="starting", progress=0.0, message="Contacting GitHub…", error="", path="")
        _thread = threading.Thread(target=_run, name="lucidfish-engine-download", daemon=True)
        _thread.start()
    return status()
