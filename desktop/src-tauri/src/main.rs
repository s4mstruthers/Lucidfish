// Copyright (C) 2026 Sam Struthers
// SPDX-License-Identifier: AGPL-3.0-or-later
// This file is part of Lucidfish <https://github.com/s4mstruthers/Lucidfish>; see LICENSE for the terms.

// Prevents an extra console window on Windows in release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    lucidfish_desktop_lib::run()
}
