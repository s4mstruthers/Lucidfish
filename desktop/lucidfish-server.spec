# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the desktop app's backend: one self-contained executable.

Build it with `python desktop/scripts/build_sidecar.py` (which renames the result the way
Tauri expects, lucidfish-server-<target triple>), or directly:

    pyinstaller desktop/lucidfish-server.spec --distpath desktop/build/dist --workpath desktop/build/work
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent

datas = collect_data_files("lucidfish", includes=["static/*", "static/vendor/*"])
hiddenimports = (
    collect_submodules("uvicorn")             # servers, loops and protocols are imported by name
    + collect_submodules("keyring.backends")  # the OS credential store for API keys
    + ["lucidfish.cli", "lucidfish.web", "lucidfish.share", "lucidfish.engine_setup", "lucidfish.concepts"]
)

a = Analysis(
    [str(ROOT / "desktop" / "sidecar_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PIL", "IPython", "pytest", "playwright"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    [],
    name="lucidfish-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,            # stdout carries the "LUCIDFISH_URL …" line the shell waits for
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
