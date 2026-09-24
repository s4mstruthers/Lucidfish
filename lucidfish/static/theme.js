// Applies the saved light/dark choice before first paint (avoids a flash of the wrong theme).
try {
  var saved = localStorage.getItem("lucidfish-theme");
  if (saved === "light" || saved === "dark") document.documentElement.setAttribute("data-theme", saved);
} catch (e) { /* storage unavailable: follow the system theme */ }
