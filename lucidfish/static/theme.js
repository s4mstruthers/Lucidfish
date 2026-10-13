// Copyright (C) 2026 Sam Struthers
// SPDX-License-Identifier: AGPL-3.0-or-later
// This file is part of Lucidfish <https://github.com/s4mstruthers/Lucidfish>; see LICENSE for the terms.

// Applies the saved light/dark choice before first paint (avoids a flash of the wrong theme).
try {
  var saved = localStorage.getItem("lucidfish-theme");
  if (saved === "light" || saved === "dark") document.documentElement.setAttribute("data-theme", saved);
} catch (e) { /* storage unavailable: follow the system theme */ }
