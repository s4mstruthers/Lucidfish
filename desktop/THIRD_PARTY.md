# Third-party software in the Lucidfish desktop app

- **Stockfish** (https://stockfishchess.org), the chess engine, bundled as an unmodified official
  binary (`engine/stockfish`) from https://github.com/official-stockfish/Stockfish/releases
  (release and build recorded in `engine/VERSION.txt`). Copyright T. Romstad, M. Costalba,
  J. Kiiski, G. Linscott and the Stockfish developers. Licensed under the GNU General Public
  License, version 3: https://www.gnu.org/licenses/gpl-3.0.html. Source code: the repository above.
- **Stockfish.js** 18 lite (https://github.com/nmrugg/stockfish.js), the WebAssembly engine used by
  Explore mode and shared pages, GPL-3.0; see `lucidfish/static/vendor/LICENSES.txt`.
- **python-chess** (https://github.com/niklasf/python-chess), GPL-3.0+: move generation, PGN, UCI
  and the piece images.
- **chess.js** (BSD-2-Clause), **chessboard.js** and **jQuery** (MIT): see
  `lucidfish/static/vendor/LICENSES.txt`.
- **Python** and its standard library (PSF licence), frozen with **PyInstaller** (GPL-2.0 with an
  exception that allows distributing the bundled program under its own terms), plus the Python
  packages Lucidfish depends on (`pyproject.toml`): FastAPI, Starlette, uvicorn, pydantic, requests,
  rich, keyring and the Anthropic SDK, all under permissive licences (MIT, BSD or Apache-2.0).
- **Tauri** (https://tauri.app) and its plugins (MIT or Apache-2.0): the native shell and updater.
