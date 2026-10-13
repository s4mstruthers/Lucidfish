# Lucidfish desktop app

A native window around Lucidfish for macOS, Windows and Linux, with in-app updates. Users download
one installer from the [releases page](https://github.com/s4mstruthers/Lucidfish/releases); from
then on the app tells them when a new version is out and installs it with one click.

## How it is put together

```
 Lucidfish.app / Lucidfish.exe / Lucidfish.AppImage
 ├── the shell (Rust, Tauri 2)            src-tauri/        a native window + the updater
 ├── lucidfish-server (sidecar)           binaries/         the Python backend, frozen by PyInstaller
 ├── engine/stockfish                     resources/        an official Stockfish build
 └── ui/index.html                        ui/               the splash shown while the backend starts
```

1. The shell starts `lucidfish-server web --desktop` as a child process. That is the normal Lucidfish
   backend (`lucidfish/web.py`) packed into one executable, with the web app's files inside it.
2. The backend prints `LUCIDFISH_URL http://127.0.0.1:<port>` once it is listening. The shell points
   the window at that address: the page served by the backend *is* the app, exactly as in a browser.
3. The bundled Stockfish is copied into Lucidfish's data folder on first launch (an AppImage is
   mounted read-only) and handed to the backend through `LUCIDFISH_BUNDLED_STOCKFISH`. A Stockfish
   chosen in Settings still wins; `LUCIDFISH_STOCKFISH` wins over both.
4. The shell holds the backend's standard input open. When the window closes, or the shell dies,
   the backend notices and stops, so nothing keeps running invisibly.
5. Updates: a few seconds after start, and on *Check for updates* in Settings → About, the shell
   fetches `latest.json` from the newest GitHub release. If it names a newer version, the page shows
   a banner; *Update and restart* downloads the installer, verifies its signature, installs it and
   restarts the app. Data (profiles, games, settings) lives in the data folder and is untouched.

The data folder is the same as for the pip-installed version: `~/Library/Application Support/Lucidfish`,
`%APPDATA%\Lucidfish` or `~/.local/share/lucidfish`. The shell writes `desktop.log` there.

## Building it yourself

Requirements: Python 3.10+, Node 18+, Rust (stable) and the
[Tauri prerequisites](https://tauri.app/start/prerequisites/) for your system (on Linux:
`libwebkit2gtk-4.1-dev libgtk-3-dev libayatana-appindicator3-dev librsvg2-dev patchelf`).

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e . pyinstaller
python desktop/scripts/build_sidecar.py               # → desktop/src-tauri/binaries/lucidfish-server-<triple>
python desktop/scripts/fetch_stockfish.py             # → desktop/src-tauri/resources/engine/stockfish
cd desktop && npm ci
npm run dev          # a development window (the sidecar is used as built above)
npm run build        # installers in desktop/src-tauri/target/release/bundle/
```

`tauri build` needs `TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` in the
environment because the configuration asks for signed updater artifacts; for a purely local build you
can generate a throwaway key pair (below) or set `bundle.createUpdaterArtifacts` to `false` temporarily.

## Releasing

Releases are built by `.github/workflows/build-apps.yml`, which mirrors a simple rule: every pull
request and every "Run workflow" builds the apps and keeps them as downloadable artifacts on the run's
page; a version tag publishes them as a GitHub release.

1. Set the version in `lucidfish/__init__.py`, `desktop/src-tauri/tauri.conf.json`,
   `desktop/src-tauri/Cargo.toml`, `desktop/package.json` (and its lock file) and `CITATION.cff`, then `cargo update -w --offline` in
   `desktop/src-tauri` so `Cargo.lock` follows (`python desktop/scripts/check_version.py` checks that
   all of them agree; the test suite does too).
2. Add a `## <version>` section to `CHANGELOG.md`, written for the people using the app. It becomes
   the release's description and the text the app shows before it installs the update
   (`python desktop/scripts/release_notes.py <version>` prints it; the workflow refuses to release
   without it).
3. Commit, then `git tag v0.5.2 && git push origin v0.5.2` (with your version).
4. The workflow runs the tests, builds on macOS (Apple silicon and Intel), Windows and Linux, bundles
   Stockfish for each, smoke-tests the frozen backend, signs the updater artifacts and publishes the
   release with the installers and `latest.json`. Installed apps pick the update up from
   `https://github.com/s4mstruthers/Lucidfish/releases/latest/download/latest.json` the next time
   they start.

To try a build without releasing, open the Actions tab → *Build apps* → *Run workflow* on any branch:
the installers are attached to the run as artifacts.

Without the signing secrets (below) the installers are still built and released, but no `latest.json`
is published, so that release has to be installed by hand and installed apps cannot update to it.

### One-time setup: the update signing key

Updates are only installed when their signature matches the public key built into the app
(`plugins.updater.pubkey` in `tauri.conf.json`). The private key must be a repository secret:

```bash
cd desktop && npx tauri signer generate -w ~/.tauri/lucidfish.key
```

Put the public key (`~/.tauri/lucidfish.key.pub`) in `tauri.conf.json`, and add two Actions secrets:
`TAURI_SIGNING_PRIVATE_KEY` (the contents of `lucidfish.key`) and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`.
Keep the key somewhere safe: if it is lost, a new pair means one release that users must install by hand.

### Optional: signed and notarised macOS builds

Without an Apple Developer ID the workflow signs the macOS app ad hoc. macOS then says it could not
verify the app, and the user clicks *Open Anyway* in *System Settings → Privacy & Security* once. (A
completely unsigned bundle is worse: macOS calls the download "damaged" and offers no way to open it.) To sign and notarise in CI, add the
secrets `APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`, `APPLE_SIGNING_IDENTITY`, `APPLE_ID`,
`APPLE_PASSWORD` and `APPLE_TEAM_ID` as described in the
[Tauri documentation](https://tauri.app/distribute/sign/macos/). Windows builds can be signed the same
way (`tauri.conf.json` → `bundle.windows.certificateThumbprint`).

## Files

| Path | Role |
|---|---|
| `src-tauri/src/lib.rs` | The shell: starts and stops the backend, checks and installs updates |
| `src-tauri/tauri.conf.json` | Window, bundle (sidecar, Stockfish resource, installers) and updater settings |
| `src-tauri/capabilities/default.json` | What the page may ask the shell to do (updater, restart, open links, and the app's own commands) |
| `src-tauri/build.rs` | Declares the app's own commands, so the capability can grant them to the page served from localhost |
| `ui/index.html` | The splash page shown until the backend is up |
| `lucidfish-server.spec` | PyInstaller recipe for the backend |
| `scripts/build_sidecar.py` | Builds the backend and names it for Tauri |
| `scripts/fetch_stockfish.py` | Downloads the official Stockfish build for a target |
| `scripts/check_version.py` | Makes sure every version number agrees |
| `THIRD_PARTY.md` | Licence notices for what the app bundles (shipped inside the app) |
