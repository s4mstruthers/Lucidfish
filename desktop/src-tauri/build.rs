// Copyright (C) 2026 Sam Struthers
// SPDX-License-Identifier: AGPL-3.0-or-later
// This file is part of Lucidfish <https://github.com/s4mstruthers/Lucidfish>; see LICENSE for the terms.

fn main() {
    // The app is served from the backend on localhost, which Tauri treats as a remote origin, and
    // remote pages may only call the app's own commands when a capability grants them. Declaring
    // the commands here generates an `allow-<command>` permission for each one.
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "backend_url",
            "restart_backend",
            "app_version",
            "check_update",
            "install_update",
        ]),
    ))
    .expect("failed to run the Tauri build script");
}
