"""Entry point of the packaged backend (built by PyInstaller into one executable).

The desktop app starts it as `lucidfish-server web --desktop`; it is the same
program as the `lucidfish` command, so `lucidfish-server game.pgn` works too.
"""
import multiprocessing
import sys

from lucidfish.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
