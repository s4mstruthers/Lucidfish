/* Lucidfish web UI — plain JavaScript, no build step.
 *
 * Talks to the local FastAPI backend (every state-changing request carries the
 * X-Lucidfish header the server requires) and fetches game lists straight from
 * chess.com / Lichess in the browser. The board is chessboard.js; arrows, square
 * highlights and the evaluation graph are drawn with SVG.
 */
"use strict";

/* ================================================================ utilities */

const $ = (id) => document.getElementById(id);
const PIECES = "/pieces/{piece}.svg";
const SYMBOLS = { best: "✓", good: "○", inaccuracy: "?!", mistake: "?", blunder: "??" };
const LABELS = { best: "Best", good: "Good", inaccuracy: "Inaccuracy", mistake: "Mistake", blunder: "Blunder" };
const ERRORS = ["inaccuracy", "mistake", "blunder"];

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** Markdown-lite for coach text: headings, bullet lists, bold, italics, paragraphs. */
function md(text) {
  const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/(^|[\s(])\*(?!\s)(.+?)\*(?=[\s).,!?:;]|$)/g, "$1<i>$2</i>");
  let out = "", list = false, para = [];
  const flush = () => { if (para.length) { out += `<p>${para.join("<br>")}</p>`; para = []; } };
  for (const raw of String(text || "").split("\n")) {
    const line = raw.trimEnd();
    const bullet = line.match(/^\s*(?:[-•*]|\d+[.)])\s+(.*)$/);
    if (bullet) {
      flush();
      if (!list) { out += "<ul>"; list = true; }
      out += `<li>${inline(bullet[1])}</li>`;
      continue;
    }
    if (list) { out += "</ul>"; list = false; }
    const head = line.match(/^#{1,4}\s+(.*)$/);
    if (head) { flush(); out += `<h3>${inline(head[1])}</h3>`; }
    else if (!line.trim()) flush();
    else para.push(inline(line));
  }
  flush();
  if (list) out += "</ul>";
  return out;
}

function toast(message, bad = false) {
  const el = document.createElement("div");
  el.className = "toast" + (bad ? " bad" : "");
  el.textContent = message;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), bad ? 7000 : 3500);
}

/** fetch() wrapper: JSON in/out, the CSRF header, and readable errors. */
async function api(path, { method = "GET", body, raw = false } = {}) {
  const opts = { method, headers: { "X-Lucidfish": "1" } };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(path, opts); }
  catch { throw new Error("Can't reach the Lucidfish server — is it still running?"); }
  if (!res.ok) {
    let msg = `Request failed (HTTP ${res.status})`;
    try {
      const data = await res.json();
      if (data.error) msg = data.error;
      else if (Array.isArray(data.detail)) msg = data.detail.map((d) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join("; ");
    } catch { /* not JSON */ }
    throw new Error(msg);
  }
  return raw ? res : res.json();
}

function badge(cls) {
  return `<span class="badge b-${esc(cls)}" title="${esc(LABELS[cls] || cls)}">${esc(SYMBOLS[cls] || "")}</span>`;
}

/** White's winning chances (0-100) from an eval string: '+1.2', '#3', '#-2', '1-0'. */
function winFromEval(ev) {
  if (!ev) return 50;
  if (ev === "1-0") return 100;
  if (ev === "0-1") return 0;
  if (ev.startsWith("1/2")) return 50;
  if (ev[0] === "#") return parseInt(ev.slice(1), 10) >= 0 ? 97.5 : 2.5;
  const cp = Math.max(-1000, Math.min(1000, parseFloat(ev) * 100));
  if (Number.isNaN(cp)) return 50;
  return 50 + 50 * (2 / (1 + Math.exp(-0.00368208 * cp)) - 1);
}
const winOf = (m) => (typeof m.win === "number" ? m.win : winFromEval(m.eval));

function evalWords(ev) {
  if (!ev) return "";
  if (ev === "1-0") return "White won";
  if (ev === "0-1") return "Black won";
  if (ev.startsWith("1/2")) return "Draw";
  if (ev[0] === "#") {
    const n = parseInt(ev.slice(1), 10);
    return `${n >= 0 ? "White" : "Black"} mates in ${Math.abs(n)}`;
  }
  const v = parseFloat(ev);
  if (Number.isNaN(v)) return ev;
  if (Math.abs(v) < 0.1) return "Equal";
  return `${v > 0 ? "White" : "Black"} +${Math.abs(v).toFixed(2)}`;
}

async function sha1(s) {
  const buf = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function fmtDate(d) {
  try { return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }); }
  catch { return ""; }
}

/** "45 s", "12 min", "1 h 20 min" — rounded, because estimates are estimates. */
function fmtDuration(s) {
  if (s == null || !Number.isFinite(s)) return "";
  s = Math.max(0, Math.round(s));
  if (s < 55) return `${Math.max(5, Math.round(s / 5) * 5)} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return m ? `${h} h ${m} min` : `${h} h`;
}
function fmtClock(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : "";
}
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : "");
const DETAIL_NAMES = { key: "Key moments", standard: "Commentary", full: "Every move" };

/** chess.com-style time class from a PGN TimeControl header (matches the backend). */
function timeClass(pgn) {
  const m = pgn.match(/\[TimeControl "(\d+)(?:\+(\d+))?"\]/);
  if (!m) return "";
  const est = parseInt(m[1], 10) + 40 * parseInt(m[2] || "0", 10);
  return est < 180 ? "bullet" : est < 600 ? "blitz" : est < 1800 ? "rapid" : "classical";
}

/* ================================================================ state */

const state = {
  page: "dashboard",
  profile: null,
  profiles: [],
  savedGames: [],
  health: null,
  settings: null,
  source: "chesscom",
  recent: [],            // recent games from chess.com / Lichess
  selected: new Set(),   // indices into state.recent
  game: null,            // the game/position on the Game page
  cur: -1,               // selected move index
  preview: null,         // variation being previewed on the board
  previewMap: {},
  gameChat: [],
  reviewChat: [],
  dismissed: new Set(),
  moveView: "moves",     // "moves" grid or "story" transcript
  practice: null,        // "practise your mistakes" attempt in progress
};

let board = null;
let editorBoard = null;

/* ================================================================ theme */

function currentTheme() {
  const explicit = document.documentElement.getAttribute("data-theme");
  if (explicit) return explicit;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function renderThemeButton() {
  const dark = currentTheme() === "dark";
  $("themeBtn").textContent = dark ? "☀" : "☾";
  $("themeBtn").title = dark ? "Switch to light theme" : "Switch to dark theme";
}
$("themeBtn").addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("lucidfish-theme", next); } catch { /* ignore */ }
  renderThemeButton();
  renderGraph();
});
renderThemeButton();

/* ================================================================ navigation */

function showPage(name, fromRoute = false) {
  state.page = name;
  document.querySelectorAll("[data-page-section]").forEach((s) => s.classList.toggle("hidden", s.id !== `page-${name}`));
  document.querySelectorAll("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.page === name));
  if (name === "dashboard") loadDashboard();
  if (name === "games") loadDashboard().then(renderGames);
  if (name === "analyze" && !state.recent.length) loadRecentGames();
  if (name === "editor") ensureEditor();
  if (name === "game") { ensureBoard(); setTimeout(() => { board.resize(); refreshBoard(); renderGraph(); }, 0); }
  if (!fromRoute) syncRoute();
  window.scrollTo({ top: 0 });
}
document.querySelectorAll("#nav button").forEach((b) => b.addEventListener("click", () => showPage(b.dataset.page)));
$("goAnalyze").addEventListener("click", () => showPage("analyze"));

/* Pages and games have addresses (#/games, #/game/12, …), so the browser's Back button
 * returns to the list you came from and a reload reopens the same game. */
function routeFor() {
  if (state.page !== "game") return `#/${state.page}`;
  const g = state.game;
  if (g?.gameId) return `#/game/${g.gameId}`;
  if (g?.jobId) return `#/job/${g.jobId}`;
  return "#/game";
}
function syncRoute(replace = false) {
  const r = routeFor();
  if (location.hash === r) return;
  history[replace ? "replaceState" : "pushState"](null, "", r);
}
async function route() {
  const [, page, id] = location.hash.match(/^#\/(\w+)(?:\/([\w-]+))?$/) || [];
  if (page === "game" && id) {
    if (state.game?.gameId === Number(id)) showPage("game", true);
    else await openStoredGame(Number(id), true);
  } else if (page === "job" && id) {
    if (state.game?.jobId === id) showPage("game", true); else openJob(id, "", true);
  } else if (page === "game" && state.game) {
    showPage("game", true);
  } else {
    showPage(["dashboard", "games", "analyze", "editor"].includes(page) ? page : "dashboard", true);
  }
}
window.addEventListener("popstate", route);

/* ================================================================ health + banners */

async function loadHealth(refresh = false) {
  try { state.health = await api(`/api/health${refresh ? "?refresh=true" : ""}`); }
  catch (e) { state.health = null; }
  renderHealth();
}

function renderHealth() {
  const h = state.health;
  const dot = $("statusDot"), text = $("statusText");
  if (!h) { dot.className = "dot bad"; text.textContent = "Server unreachable"; renderBanners(); return; }
  const coach = h.coach || {}, engine = h.engine || {};
  const coachText = coach.enabled ? `${coach.model}${coach.local ? " (local)" : ""}` : "engine only";
  text.textContent = engine.ok ? `${engine.name} · ${coachText}` : "Stockfish not found";
  dot.className = "dot " + (h.busy ? "busy" : !engine.ok ? "bad" : coach.ok ? "ok" : "warn");
  $("aboutVersion").textContent = h.version || "";
  renderBanners();
}

function renderBanners() {
  const h = state.health, out = [];
  if (h && !h.engine.ok && !state.dismissed.has("engine")) {
    out.push({ id: "engine", cls: "bad", title: "Stockfish isn't set up yet",
      body: esc(h.engine.error || ""), actions: [["Open settings", () => openSettings("analysis")]] });
  }
  if (h && h.coach.enabled && !h.coach.ok && !state.dismissed.has("coach")) {
    out.push({ id: "coach", cls: "", title: "The AI coach isn't ready",
      body: `${esc(h.coach.message)} Analyses still work with engine verdicts only.`,
      actions: [["Set up the coach", () => openSettings("coach")], ["Use engine only", () => setEngineOnly()]] });
  }
  const box = $("banners");
  box.innerHTML = "";
  for (const b of out) {
    const el = document.createElement("div");
    el.className = `banner ${b.cls}`;
    el.innerHTML = `<div class="grow"><b>${esc(b.title)}</b><span class="small">${b.body}</span></div>`;
    for (const [label, fn] of b.actions) {
      const btn = document.createElement("button");
      btn.className = "secondary sm"; btn.textContent = label; btn.addEventListener("click", fn);
      el.appendChild(btn);
    }
    const close = document.createElement("button");
    close.className = "ghost icon"; close.textContent = "✕"; close.setAttribute("aria-label", "Dismiss");
    close.addEventListener("click", () => { state.dismissed.add(b.id); renderBanners(); });
    el.appendChild(close);
    box.appendChild(el);
  }
}

async function setEngineOnly() {
  try { await api("/api/settings", { method: "POST", body: { llm_enabled: false } }); toast("AI coach turned off — engine-only analysis."); }
  catch (e) { toast(e.message, true); }
  loadHealth(true);
}
$("statusPill").addEventListener("click", () => openSettings("coach"));

/* ================================================================ profiles */

async function loadProfiles() {
  const d = await api("/api/profiles");
  state.profiles = d.profiles;
  const sel = $("profileSel");
  sel.innerHTML = d.profiles.length
    ? d.profiles.map((p) => `<option value="${p.id}" ${p.id === d.active ? "selected" : ""}>${esc(p.name)}</option>`).join("")
    : `<option value="">No profile</option>`;
  sel.disabled = !d.profiles.length;
  state.profile = d.profiles.find((p) => p.id === d.active) || null;
  return d;
}

$("profileSel").addEventListener("change", async (e) => {
  try { await api(`/api/profiles/${e.target.value}/activate`, { method: "POST" }); }
  catch (err) { toast(err.message, true); return; }
  await loadProfiles();
  state.reviewChat.length = 0; $("reviewChatLog").innerHTML = "";
  state.recent = []; state.selected.clear();
  loadDashboard().then(() => { if (state.page === "games") renderGames(); });
  if (state.page === "analyze") loadRecentGames();
});

let editingProfile = null;
function openProfile(existing) {
  editingProfile = existing;
  $("profileTitle").textContent = existing ? "Edit profile" : "Create your profile";
  $("pfName").value = existing?.name || "";
  $("pfLevel").value = existing?.level || "";
  $("pfChesscom").value = existing?.chesscom_user || "";
  $("pfLichess").value = existing?.lichess_user || "";
  $("pfBullet").value = existing?.elo_bullet || "";
  $("pfBlitz").value = existing?.elo_blitz || "";
  $("pfRapid").value = existing?.elo_rapid || "";
  $("pfDelete").classList.toggle("hidden", !existing);
  $("pfError").textContent = "";
  openModal("profileModal");
  $("pfName").focus();
}
$("profileBtn").addEventListener("click", () => openProfile(state.profile));

$("pfSave").addEventListener("click", async () => {
  const num = (id) => { const v = parseInt($(id).value, 10); return Number.isFinite(v) ? v : null; };
  const body = {
    name: $("pfName").value.trim(), level: $("pfLevel").value,
    chesscom_user: $("pfChesscom").value.trim(), lichess_user: $("pfLichess").value.trim(),
    elo_bullet: num("pfBullet"), elo_blitz: num("pfBlitz"), elo_rapid: num("pfRapid"),
  };
  if (!body.name) { $("pfError").textContent = "Please enter a name."; return; }
  try {
    if (editingProfile) await api(`/api/profiles/${editingProfile.id}`, { method: "POST", body });
    else await api("/api/profiles", { method: "POST", body });
  } catch (e) { $("pfError").textContent = e.message; return; }
  closeModal("profileModal");
  await loadProfiles();
  state.recent = [];
  loadDashboard();
  toast(editingProfile ? "Profile updated." : `Welcome, ${body.name}!`);
});

$("pfDelete").addEventListener("click", async () => {
  if (!editingProfile) return;
  if (!confirm(`Delete the profile "${editingProfile.name}" and all of its analysed games? This can't be undone.`)) return;
  try { await api(`/api/profiles/${editingProfile.id}`, { method: "DELETE" }); }
  catch (e) { $("pfError").textContent = e.message; return; }
  closeModal("profileModal");
  await loadProfiles();
  loadDashboard();
  toast("Profile deleted.");
});

/* ================================================================ modals */

function openModal(id) { $(id).classList.remove("hidden"); }
function closeModal(id) { $(id).classList.add("hidden"); }
document.querySelectorAll(".modal-back").forEach((m) => {
  m.addEventListener("click", (e) => { if (e.target === m) closeModal(m.id); });
  m.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => closeModal(m.id)));
});
const modalOpen = () => [...document.querySelectorAll(".modal-back")].some((m) => !m.classList.contains("hidden"));

/* ================================================================ dashboard */

async function loadDashboard() {
  let d;
  try { d = await api("/api/profile"); } catch (e) { toast(e.message, true); return; }
  if (!d.profile) {
    $("dashTitle").textContent = "Welcome to Lucidfish";
    $("stats").innerHTML = "";
    $("summary").innerHTML = `<div class="empty"><div class="big">♞</div>Create a profile so the coach can learn your style,
      or jump straight into <a href="#" id="emptyAnalyze">analysing a game</a>.<br><br>
      <button id="emptyProfile">Create profile</button></div>`;
    $("emptyProfile").addEventListener("click", () => openProfile(null));
    $("emptyAnalyze").addEventListener("click", (e) => { e.preventDefault(); showPage("analyze"); });
    $("savedGames").innerHTML = `<div class="empty small">No games yet.</div>`;
    $("openings").innerHTML = `<div class="empty small">Appears after your first analysis.</div>`;
    state.savedGames = [];
    return;
  }
  state.profile = d.profile;
  state.savedGames = d.games || [];
  const s = d.stats || {};
  $("dashTitle").textContent = `${d.profile.name}'s coach review`;
  const stat = (label, value) => `<div class="stat"><b>${value ?? "—"}</b><span>${label}</span></div>`;
  $("stats").innerHTML = s.games
    ? stat("games analysed", s.games) + stat("record (W-L-D)", `${s.wins}-${s.losses}-${s.draws}`)
      + stat("average accuracy", s.avg_accuracy != null ? `${s.avg_accuracy}%` : "—")
      + stat("avg centipawn loss", s.avg_acpl) + stat("blunders / game", s.blunders_per_game)
      + stat("mistakes / game", s.mistakes_per_game)
    : "";
  renderInsights(s);
  $("summary").innerHTML = d.profile.summary
    ? md(d.profile.summary)
    : `<div class="empty small">${s.games ? "Analyse one more game and your coach will write a review of your play." : "Analyse a couple of your games and your coach will write a review of your play here."}</div>`;
  renderSavedGames();
  $("openings").innerHTML = (s.openings || []).length
    ? s.openings.map((o) => `<div class="item static"><span class="title">${esc(o.name)}</span>
        <span class="meta">${o.games} game${o.games === 1 ? "" : "s"} · ${o.w}W ${o.l}L ${o.d}D${o.acpl != null ? ` · ${o.acpl} ACPL` : ""}</span></div>`).join("")
    : `<div class="empty small">Appears after your first analysis.</div>`;
}

const perGame = (n) => `${n} mistake${n === 1 ? "" : "s"} per game`;

function renderInsights(s) {
  $("insightsRow").classList.toggle("hidden", !s.games);
  if (!s.games) return;
  const phases = ["opening", "middlegame", "endgame"], pa = s.phase_accuracy || {};
  $("phaseBars").innerHTML = phases.some((p) => pa[p] != null)
    ? phases.map((p) => {
      const v = pa[p];
      return `<div class="phase-row${s.weakest_phase === p ? " weak" : ""}"><span>${cap(p)}</span>`
        + `<div class="bar"><div style="width:${v ?? 0}%"></div></div><b>${v != null ? `${v}%` : "—"}</b>`
        + `<span class="small">${v != null ? perGame(s.phase_errors?.[p] ?? 0) : "not reached yet"}</span></div>`;
    }).join("")
      + `<p class="small" style="margin:10px 0 0">Average accuracy of your moves in each phase${s.weakest_phase
        ? ` — the <b>${esc(s.weakest_phase)}</b> is where you lose the most, so it's the best place to focus.` : "."}</p>`
    : `<div class="empty small">Appears after your first analysis.</div>`;
  $("patterns").innerHTML = (s.patterns || []).length
    ? s.patterns.map((p) => `<div class="item static"><span class="title">${esc(p.label)}</span>`
      + `<span class="meta">${p.count}× in ${p.games} game${p.games === 1 ? "" : "s"}</span></div>`).join("")
      + `<p class="small" style="margin:8px 0 0">Found in the moves the engine marked as inaccuracies, mistakes or blunders.</p>`
    : `<div class="empty small">No recurring mistake types yet.</div>`;
}

function resultFor(g) {
  if (!g.user_side || !["1-0", "0-1", "1/2-1/2"].includes(g.result)) return "";
  if (g.result === "1/2-1/2") return "D";
  return (g.result === "1-0") === (g.user_side === "white") ? "W" : "L";
}
function resultChip(r) {
  return r === "W" ? `<span class="chip win">WIN</span>` : r === "L" ? `<span class="chip loss">LOSS</span>` : r === "D" ? `<span class="chip">DRAW</span>` : "";
}

const RECENT_ON_DASHBOARD = 6;

function renderSavedGames() {
  const box = $("savedGames"), n = state.savedGames.length;
  $("seeAllGames").classList.toggle("hidden", !n);
  $("seeAllGames").textContent = n > RECENT_ON_DASHBOARD ? `See all ${n} games →` : "Open My games →";
  if (!n) { box.innerHTML = `<div class="empty small">No analysed games yet.</div>`; return; }
  box.innerHTML = "";
  for (const g of state.savedGames.slice(0, RECENT_ON_DASHBOARD)) {
    const el = document.createElement("div");
    el.className = "item";
    el.innerHTML = `<span class="title">${esc(g.white)} vs ${esc(g.black)}</span>${resultChip(resultFor(g))}
      <span class="meta">${esc([g.accuracy != null ? `${g.accuracy}%` : "", g.time_class, g.date && !g.date.includes("?") ? g.date : ""].filter(Boolean).join(" · "))}</span>
      <button class="ghost icon" title="Delete this analysis" aria-label="Delete">🗑</button>`;
    el.addEventListener("click", () => openStoredGame(g.id));
    el.querySelector("button").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm("Delete this analysis?")) return;
      try { await api(`/api/profile/game/${g.id}`, { method: "DELETE" }); } catch (err) { toast(err.message, true); return; }
      loadDashboard();
    });
    box.appendChild(el);
  }
}

$("seeAllGames").addEventListener("click", () => showPage("games"));

$("refreshSummary").addEventListener("click", async () => {
  const btn = $("refreshSummary");
  btn.disabled = true; $("summaryStatus").textContent = "Your coach is writing…";
  try {
    const d = await api("/api/profile/refresh_summary", { method: "POST" });
    $("summary").innerHTML = md(d.summary);
    $("summaryStatus").textContent = "Updated";
  } catch (e) { $("summaryStatus").textContent = ""; toast(e.message, true); }
  btn.disabled = false;
});

/* ================================================================ my games */

const gamesView = { limit: 50 };

/** PGN date "2024.05.01" → Date (null when unknown). */
function playedDate(g) {
  const m = (g.date || "").match(/^(\d{4})\.(\d{2})\.(\d{2})$/);
  return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
}
/** SQLite UTC timestamp "2026-09-24 12:34:56" → Date. */
function analysedDate(g) {
  return g.analyzed_at ? new Date(`${g.analyzed_at.replace(" ", "T")}Z`) : null;
}
function opponentOf(g) {
  return g.user_side === "white" ? g.black : g.user_side === "black" ? g.white : null;
}
const accClass = (v) => (v >= 90 ? "a-hi" : v >= 75 ? "a-mid" : v >= 60 ? "a-low" : "a-bad");

function filteredGames() {
  const words = $("gSearch").value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const res = $("gResult").value, color = $("gColor").value, tc = $("gTime").value;
  const list = state.savedGames.filter((g) => {
    if (res && resultFor(g) !== res) return false;
    if (color && g.user_side !== color) return false;
    if (tc && (g.time_class || "") !== tc) return false;
    if (words.length) {
      const hay = [g.white, g.black, g.opening, g.date, g.time_class, g.result].join(" ").toLowerCase();
      if (!words.every((w) => hay.includes(w))) return false;
    }
    return true;
  });
  const played = (g) => (/^\d{4}\./.test(g.date || "") ? g.date : "");
  const sorts = {
    analysed: (a, b) => b.id - a.id,
    played: (a, b) => played(b).localeCompare(played(a)) || b.id - a.id,
    "acc-desc": (a, b) => (b.accuracy ?? -1) - (a.accuracy ?? -1),
    "acc-asc": (a, b) => (a.accuracy ?? 101) - (b.accuracy ?? 101),
    errors: (a, b) => ((b.blunders || 0) - (a.blunders || 0)) || ((b.mistakes || 0) - (a.mistakes || 0))
      || ((b.inaccuracies || 0) - (a.inaccuracies || 0)),
  };
  return list.sort(sorts[$("gSort").value] || sorts.analysed);
}

function gameRow(g) {
  const opp = opponentOf(g), played = playedDate(g), analysed = analysedDate(g);
  const title = opp ? `<span class="g-vs">vs</span> ${esc(opp)}`
    : `${esc(g.white)} <span class="g-vs">vs</span> ${esc(g.black)}`;
  const colour = g.user_side ? `<span class="g-colour ${g.user_side}" title="You played ${g.user_side}"></span>` : "";
  const meta = [g.opening, cap(g.time_class), played ? fmtDate(played) : ""].filter(Boolean).join(" · ");
  const errs = [["blunder", g.blunders], ["mistake", g.mistakes], ["inaccuracy", g.inaccuracies]]
    .map(([c, n]) => `<span class="g-err${n ? "" : " zero"}" title="${n || 0} ${LABELS[c].toLowerCase()}${n === 1 ? "" : "s"}">`
      + `${badge(c)}<span>${n || 0}</span></span>`).join("");
  return `<div class="g-row" data-gid="${g.id}" tabindex="0" role="button"
      title="${esc(analysed ? `Analysed ${fmtDate(analysed)}` : "")}">
    <div class="g-result">${resultChip(resultFor(g)) || `<span class="chip">${esc(g.result || "*")}</span>`}</div>
    <div class="g-main"><div class="g-title">${colour}${title}</div><div class="g-meta">${esc(meta)}</div></div>
    <div class="g-acc">${g.accuracy != null ? `<b class="${accClass(g.accuracy)}">${g.accuracy}%</b>` : "<b>—</b>"}<span>accuracy</span></div>
    <div class="g-errs">${errs}</div>
    <div class="g-actions">
      <button class="ghost icon" data-ga="reanalyse" title="Analyse again with your current settings" aria-label="Re-analyse">↻</button>
      <button class="ghost icon" data-ga="delete" title="Delete this analysis" aria-label="Delete">🗑</button>
    </div>
  </div>`;
}

function renderGames() {
  const box = $("gList"), all = state.savedGames;
  const tcs = [...new Set(all.map((g) => g.time_class).filter(Boolean))];
  const sel = $("gTime"), chosen = sel.value;
  sel.innerHTML = `<option value="">All time controls</option>` + tcs.map((t) => `<option value="${esc(t)}">${esc(cap(t))}</option>`).join("");
  sel.value = tcs.includes(chosen) ? chosen : "";
  $("gMore").classList.add("hidden");
  if (!state.profile) {
    $("gSummary").innerHTML = "";
    box.innerHTML = `<div class="empty"><div class="big">♞</div>Create a profile so your analysed games are saved here.<br><br>
      <button data-gempty="profile">Create profile</button></div>`;
    return;
  }
  if (!all.length) {
    $("gSummary").innerHTML = "";
    box.innerHTML = `<div class="empty"><div class="big">♞</div>No analysed games yet. Games you analyse are saved here automatically.<br><br>
      <button data-gempty="analyze">Analyze a game</button></div>`;
    return;
  }
  const list = filteredGames();
  const count = (r) => list.filter((g) => resultFor(g) === r).length;
  const accs = list.map((g) => g.accuracy).filter((v) => v != null);
  $("gSummary").innerHTML = `<span><b>${list.length}</b> ${list.length === all.length ? "" : `of ${all.length} `}game${all.length === 1 ? "" : "s"}</span>`
    + `<span>${count("W")}W · ${count("L")}L · ${count("D")}D</span>`
    + (accs.length ? `<span>average accuracy <b>${(accs.reduce((a, b) => a + b, 0) / accs.length).toFixed(1)}%</b></span>` : "");
  if (!list.length) {
    box.innerHTML = `<div class="empty small">No games match these filters. <a href="#" data-gempty="clear">Clear filters</a></div>`;
    return;
  }
  box.innerHTML = list.slice(0, gamesView.limit).map(gameRow).join("");
  if (list.length > gamesView.limit) {
    $("gMore").classList.remove("hidden");
    $("gMore").textContent = `Show more (${list.length - gamesView.limit} more)`;
  }
}

const rerenderGames = () => { gamesView.limit = 50; renderGames(); };
$("gSearch").addEventListener("input", debounce(rerenderGames, 120));
["gResult", "gColor", "gTime", "gSort"].forEach((id) => $(id).addEventListener("change", rerenderGames));
$("gMore").addEventListener("click", () => { gamesView.limit += 50; renderGames(); });
$("gamesAnalyze").addEventListener("click", () => showPage("analyze"));

async function reanalyse(id) {
  const d = await api(`/api/profile/game/${id}/reanalyse`, { method: "POST", body: {} });
  pollQueue();
  return d.job_id;
}

$("gList").addEventListener("click", async (e) => {
  const empty = e.target.closest("[data-gempty]");
  if (empty) {
    e.preventDefault();
    const what = empty.dataset.gempty;
    if (what === "profile") openProfile(null);
    else if (what === "analyze") showPage("analyze");
    else { $("gSearch").value = ""; ["gResult", "gColor", "gTime"].forEach((id) => { $(id).value = ""; }); rerenderGames(); }
    return;
  }
  const row = e.target.closest(".g-row");
  if (!row) return;
  const id = Number(row.dataset.gid);
  const action = e.target.closest("[data-ga]")?.dataset.ga;
  if (!action) { openStoredGame(id); return; }
  try {
    if (action === "reanalyse") {
      await reanalyse(id);
      toast("Added to the front of the queue — the new analysis replaces this one when it's done.");
    } else if (action === "delete") {
      if (!confirm("Delete this analysis? The game itself stays on chess.com / Lichess.")) return;
      await api(`/api/profile/game/${id}`, { method: "DELETE" });
      state.savedGames = state.savedGames.filter((g) => g.id !== id);
      renderGames();
      loadDashboard();
    }
  } catch (err) { toast(err.message, true); }
});
$("gList").addEventListener("keydown", (e) => {
  const row = e.target.closest?.(".g-row");
  if (row && e.target === row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openStoredGame(Number(row.dataset.gid)); }
});

/* ================================================================ recent games */

document.querySelectorAll("#sourceSeg button").forEach((b) => b.addEventListener("click", () => {
  state.source = b.dataset.source;
  document.querySelectorAll("#sourceSeg button").forEach((x) => x.classList.toggle("active", x === b));
  loadRecentGames();
}));
$("reloadGames").addEventListener("click", () => loadRecentGames());

async function fetchChesscom(user) {
  const r = await fetch(`https://api.chess.com/pub/player/${encodeURIComponent(user.toLowerCase())}/games/archives`);
  if (r.status === 404) throw new Error(`chess.com user "${user}" not found`);
  if (!r.ok) throw new Error(`chess.com answered HTTP ${r.status}`);
  const archives = (await r.json()).archives || [];
  const games = [];
  for (const url of archives.slice(-3).reverse()) {
    const month = await (await fetch(url)).json();
    for (const g of (month.games || []).reverse()) {
      if (!g.pgn || !["chess", "chess960"].includes(g.rules || "chess")) continue;
      const isWhite = g.white.username.toLowerCase() === user.toLowerCase();
      const me = isWhite ? g.white : g.black, them = isWhite ? g.black : g.white;
      games.push({
        pgn: g.pgn, white: g.white.username, black: g.black.username, side: isWhite ? "white" : "black",
        rating: me.rating || null, result: me.result === "win" ? "W" : them.result === "win" ? "L" : "D",
        date: new Date(g.end_time * 1000), timeClass: g.time_class || "",
        accuracy: g.accuracies ? g.accuracies[isWhite ? "white" : "black"] : null,
      });
    }
    if (games.length >= 25) break;
  }
  return games.slice(0, 25);
}

async function fetchLichess(user) {
  const url = `https://lichess.org/api/games/user/${encodeURIComponent(user)}?max=25&pgnInJson=true&clocks=true&opening=true&accuracy=true`;
  const r = await fetch(url, { headers: { Accept: "application/x-ndjson" } });
  if (r.status === 404) throw new Error(`Lichess user "${user}" not found`);
  if (!r.ok) throw new Error(`Lichess answered HTTP ${r.status}`);
  const games = [];
  for (const line of (await r.text()).split("\n")) {
    if (!line.trim()) continue;
    let g;
    try { g = JSON.parse(line); } catch { continue; }
    if (!g.pgn || !["standard", "chess960", "fromPosition"].includes(g.variant)) continue;
    const w = g.players.white, b = g.players.black;
    const wName = w.user?.name || "Anonymous", bName = b.user?.name || "Anonymous";
    const isWhite = wName.toLowerCase() === user.toLowerCase();
    const me = isWhite ? w : b;
    games.push({
      pgn: g.pgn, white: wName, black: bName, side: isWhite ? "white" : "black", rating: me.rating || null,
      result: !g.winner ? "D" : (g.winner === (isWhite ? "white" : "black") ? "W" : "L"),
      date: new Date(g.createdAt), timeClass: g.speed || "", accuracy: me.analysis?.accuracy ?? null,
    });
  }
  return games;
}

async function loadRecentGames() {
  const box = $("gameList");
  const user = state.source === "chesscom" ? state.profile?.chesscom_user : state.profile?.lichess_user;
  const site = state.source === "chesscom" ? "chess.com" : "Lichess";
  state.selected.clear(); updateSelection();
  if (!user) {
    box.innerHTML = `<div class="empty small">Add your ${site} username to your profile to see your recent games here.<br><br>
      <button class="secondary sm" id="addUser">${state.profile ? "Edit profile" : "Create profile"}</button></div>`;
    $("addUser").addEventListener("click", () => openProfile(state.profile));
    state.recent = [];
    return;
  }
  box.innerHTML = `<div class="empty small">Loading ${esc(site)} games for ${esc(user)}…</div>`;
  try {
    state.recent = state.source === "chesscom" ? await fetchChesscom(user) : await fetchLichess(user);
  } catch (e) {
    state.recent = [];
    box.innerHTML = `<div class="empty small">Couldn't load games from ${esc(site)}: ${esc(e.message)}.<br>You can paste a PGN instead.</div>`;
    return;
  }
  const analysed = new Map(state.savedGames.map((g) => [g.fingerprint, g.id]));
  box.innerHTML = state.recent.length ? "" : `<div class="empty small">No recent games found.</div>`;
  for (const [i, g] of state.recent.entries()) {
    g.fingerprint = await sha1(g.pgn.trim());
    g.storedId = analysed.get(g.fingerprint);
    const el = document.createElement("div");
    g.el = el;
    el.className = "item";
    el.innerHTML = `<input type="checkbox" aria-label="Select game">
      <span class="title">${esc(g.white)} vs ${esc(g.black)}</span>${resultChip(g.result)}
      <span class="chip accent${g.storedId ? "" : " hidden"}">ANALYSED</span>
      <span class="meta">${g.accuracy != null ? `${Number(g.accuracy).toFixed(1)}% · ` : ""}${esc(g.timeClass)}${g.rating ? ` · ${g.rating}` : ""} · ${fmtDate(g.date)}</span>`;
    const cb = el.querySelector("input");
    cb.addEventListener("click", (e) => {
      e.stopPropagation();
      if (cb.checked) state.selected.add(i); else state.selected.delete(i);
      updateSelection();
    });
    el.addEventListener("click", () => {
      if (g.storedId && !$("redo").checked) openStoredGame(g.storedId);
      else startAnalysis(g.pgn, g.side, g.rating);
    });
    box.appendChild(el);
  }
}

/** Refresh the ANALYSED marks after queued games finish (no refetch from the site). */
function markAnalysed() {
  const analysed = new Map(state.savedGames.map((g) => [g.fingerprint, g.id]));
  for (const g of state.recent) {
    if (!g.fingerprint || !g.el) continue;
    g.storedId = analysed.get(g.fingerprint);
    g.el.querySelector(".chip.accent").classList.toggle("hidden", !g.storedId);
  }
}

function updateSelection() {
  const n = state.selected.size;
  $("analyzeSelected").disabled = !n;
  $("analyzeSelected").textContent = n ? `Analyze selected (${n})` : "Analyze selected";
}

/* ================================================================ batch → queue */

async function startImport(games) {
  if (!state.profile) { toast("Create a profile first — batch analyses are saved to it.", true); openProfile(null); return; }
  const payload = games.map((g) => ({ pgn: g.pgn, side: g.side, elo: g.rating }));
  let d;
  try {
    d = await api("/api/queue", { method: "POST", body: { games: payload, force: $("redo").checked,
      detail: $("batchDetail").value || null } });
  } catch (e) { toast(e.message, true); return; }
  const skipped = d.skipped ? ` (${d.skipped} already analysed or queued)` : "";
  toast(d.added ? `Added ${d.added} game${d.added === 1 ? "" : "s"} to the queue${skipped}.` : `Nothing to add${skipped}.`);
  if (d.errors.length) toast(d.errors[0], true);
  state.selected.clear(); updateSelection();
  document.querySelectorAll("#gameList input[type=checkbox]").forEach((cb) => { cb.checked = false; });
  pollQueue();
}
$("analyzeSelected").addEventListener("click", () => startImport([...state.selected].sort((a, b) => a - b).map((i) => state.recent[i])));
$("analyzeBatch").addEventListener("click", () => {
  if (!state.recent.length) { toast("No games loaded to analyse.", true); return; }
  startImport(state.recent.slice(0, parseInt($("batchCount").value, 10)));
});

/* ================================================================ analysis queue */

const queue = { data: null, timer: null, seq: 0, seen: new Set() };

/** Refresh the queue now and keep polling: every second while a game runs, rarely when idle. */
async function pollQueue() {
  clearTimeout(queue.timer);
  const seq = ++queue.seq;   // only the newest call keeps the polling loop alive
  let d;
  try { d = await api("/api/queue"); }
  catch { if (seq === queue.seq) queue.timer = setTimeout(pollQueue, 5000); return; }
  if (seq !== queue.seq) return;
  const first = queue.data === null;
  queue.data = d;
  let saved = false;
  for (const j of d.finished) {
    if (queue.seen.has(j.id)) continue;
    queue.seen.add(j.id);
    if (!first && j.game_id) saved = true;
  }
  if (saved) {
    loadDashboard().then(() => {
      if (state.page === "analyze") markAnalysed();
      if (state.page === "games") renderGames();
    });
  }
  renderQueuePill();
  if (!$("queueModal").classList.contains("hidden")) renderQueue();
  const active = d.running || d.queued.length;
  const open = !$("queueModal").classList.contains("hidden");
  queue.timer = setTimeout(pollQueue, d.running ? 1000 : active || open ? 3000 : 20000);
}

function renderQueuePill() {
  const d = queue.data, pill = $("queuePill");
  const active = d && (d.running || d.queued.length);
  pill.classList.toggle("hidden", !active);
  const status = $("importStatus");
  if (!active) { status.innerHTML = ""; $("gamesQueueStatus").innerHTML = ""; return; }
  const t = d.totals;
  const which = t.games_total > 1 ? `game ${Math.min(t.games_total, t.games_done + 1)} of ${t.games_total}` : "1 game";
  $("queueText").textContent = d.paused ? `Queue paused · ${t.games_left} waiting`
    : `${cap(which)} · ${fmtDuration(t.seconds_left)} left`;
  $("queueDot").className = `dot ${d.paused ? "warn" : "busy"}`;
  $("queueMiniBar").style.width = `${Math.round(100 * t.progress)}%`;
  status.innerHTML = `<span class="dot ${d.paused ? "warn" : "busy"}"></span> ${t.games_left} game${t.games_left === 1 ? "" : "s"} in the queue`
    + (d.paused ? " (paused)" : ` · about ${fmtDuration(t.seconds_left)} left`)
    + ` <button class="ghost sm" data-open-queue>Manage queue</button>`;
  $("gamesQueueStatus").innerHTML = status.innerHTML.replace(" in the queue", " still being analysed");
}
$("queuePill").addEventListener("click", () => openQueue());
["importStatus", "gamesQueueStatus"].forEach((id) => $(id).addEventListener("click", (e) => {
  if (e.target.closest("[data-open-queue]")) openQueue();
}));

function openQueue() {
  openModal("queueModal");
  renderQueue();
  pollQueue();
}

const STATUS_LABELS = { done: "Done", stopped: "Stopped", error: "Failed", cancelled: "Removed" };

function detailChip(detail) {
  return detail ? `<span class="chip" title="Coaching detail for this game">${esc(DETAIL_NAMES[detail] || detail)}</span>` : "";
}

function renderQueue() {
  const d = queue.data;
  if (!d) return;
  const t = d.totals, active = d.running || d.queued.length;
  $("queueRestored").innerHTML = d.restored && d.paused
    ? `<div class="banner info"><div class="grow"><b>${d.restored} game${d.restored === 1 ? "" : "s"} from your last session ${d.restored === 1 ? "is" : "are"} waiting</b>`
      + `<span class="small">The queue was paused when Lucidfish restarted, so nothing starts by surprise.</span></div>`
      + `<button class="sm" data-qa="resume">▶ Resume</button></div>` : "";
  $("queueOverall").innerHTML = active
    ? `<div class="row"><b>${t.games_done} of ${t.games_total} game${t.games_total === 1 ? "" : "s"} done</b><div class="spacer"></div>`
      + `<span class="small">${d.paused ? "Paused" : `about <b>${fmtDuration(t.seconds_left)}</b> left · done around ${fmtClock(t.finish_at)}`}</span></div>`
      + `<div class="bar big"><div style="width:${(100 * t.progress).toFixed(1)}%"></div></div>`
      + (t.learned ? "" : `<p class="small" style="margin:6px 0 0">Rough estimate for now — it gets accurate once a game with these settings has finished on this computer.</p>`)
    : `<div class="empty small">The queue is empty. Add games from <a href="#" data-qa="analyze">Analyze games</a> — pick several and press “Analyze selected”, or queue your last 5–20 games at once.</div>`;
  $("queuePause").textContent = d.paused ? "▶ Resume" : "Pause";
  $("queuePause").disabled = !active && !d.paused;
  $("queueStopAll").disabled = !active;
  $("queueClear").disabled = !d.finished.length;

  const r = d.running;
  $("queueRunning").innerHTML = r ? `<div class="section-label">Now analysing</div>`
    + `<div class="qjob" data-id="${r.id}"><div class="row"><span class="dot busy"></span><b class="grow ellipsis">${esc(r.title)}</b>${detailChip(r.detail)}`
    + `<span class="small">${r.reviewing ? "writing the review…" : `about ${fmtDuration(r.eta_s)} left`}</span>`
    + `<button class="secondary sm" data-qa="open">Open</button><button class="danger sm" data-qa="cancel">Stop</button></div>`
    + `<div class="bar dual"><div class="eng" style="width:${(100 * r.engine_done / Math.max(1, r.total)).toFixed(1)}%"></div>`
    + `<div class="done" style="width:${(100 * r.done / Math.max(1, r.total)).toFixed(1)}%"></div></div>`
    + `<div class="small">${esc(r.label)} · ${r.done}/${r.total} moves ready${r.engine_done > r.done ? ` · engine at move ${r.engine_done}` : ""}</div></div>` : "";

  $("queueUpcoming").innerHTML = d.queued.length ? `<div class="section-label">Up next</div><div class="list">`
    + d.queued.map((j, i) => `<div class="item qrow" data-id="${j.id}" title="Open this game">`
      + `<span class="qpos">${i + 1}</span><span class="title">${esc(j.title)}</span>${detailChip(j.detail)}`
      + `<span class="meta">${d.paused ? "" : `starts in ${fmtDuration(j.start_in_s)} · `}takes ~${fmtDuration(j.eta_s)}`
      + `${j.prefetched ? ` · engine ${Math.min(100, Math.round(100 * j.prefetched / (j.total + 1)))}% ahead` : ""}</span>`
      + `<button class="ghost icon" data-qa="top" title="Analyse next" aria-label="Move to the front"${i === 0 ? " disabled" : ""}>⤒</button>`
      + `<button class="ghost icon" data-qa="up" title="Move up" aria-label="Move up"${i === 0 ? " disabled" : ""}>↑</button>`
      + `<button class="ghost icon" data-qa="down" title="Move down" aria-label="Move down"${i === d.queued.length - 1 ? " disabled" : ""}>↓</button>`
      + `<button class="ghost icon" data-qa="cancel" title="Remove from the queue" aria-label="Remove">✕</button></div>`).join("")
    + `</div>` : "";

  $("queueDone").innerHTML = d.finished.length ? `<div class="section-label">Finished</div><div class="list">`
    + d.finished.map((j) => `<div class="item qrow ${j.game_id || j.status === "done" ? "" : "static"}" data-id="${j.id}">`
      + `<span class="chip ${j.status === "done" ? "win" : j.status === "error" ? "loss" : ""}">${esc(STATUS_LABELS[j.status] || j.status)}</span>`
      + `<span class="title">${esc(j.title)}</span>`
      + `<span class="meta">${[j.accuracy != null ? `${j.accuracy}% accuracy` : "", j.duration_s ? `took ${fmtDuration(j.duration_s)}` : "",
        j.error ? esc(j.error) : ""].filter(Boolean).join(" · ")}</span></div>`).join("")
    + `</div>` : "";
}

$("queueBody").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-qa]");
  const row = e.target.closest("[data-id]");
  const id = row?.dataset.id;
  try {
    if (btn) {
      e.preventDefault();
      const qa = btn.dataset.qa;
      if (qa === "analyze") { closeModal("queueModal"); showPage("analyze"); return; }
      if (qa === "open") { closeModal("queueModal"); openJob(id); return; }
      if (qa === "resume") queue.data = await api("/api/queue/resume", { method: "POST" });
      else if (qa === "cancel") queue.data = await api(`/api/queue/${id}/cancel`, { method: "POST" });
      else queue.data = await api(`/api/queue/${id}/move`, { method: "POST", body: { where: qa } });
      renderQueue(); renderQueuePill();
      return;
    }
    if (!row || row.classList.contains("static")) return;
    const j = [...queue.data.queued, ...queue.data.finished].find((x) => x.id === id);
    closeModal("queueModal");
    if (j?.game_id) openStoredGame(j.game_id); else openJob(id);
  } catch (err) { toast(err.message, true); }
});
$("queuePause").addEventListener("click", async () => {
  try { queue.data = await api(`/api/queue/${queue.data?.paused ? "resume" : "pause"}`, { method: "POST" }); }
  catch (e) { toast(e.message, true); return; }
  renderQueue(); renderQueuePill(); pollQueue();
});
$("queueStopAll").addEventListener("click", async () => {
  if (!confirm("Stop the current analysis and remove every waiting game from the queue?")) return;
  try { queue.data = await api("/api/queue/stop_all", { method: "POST" }); } catch (e) { toast(e.message, true); return; }
  renderQueue(); renderQueuePill();
});
$("queueClear").addEventListener("click", async () => {
  try { queue.data = await api("/api/queue/clear_finished", { method: "POST" }); } catch (e) { toast(e.message, true); return; }
  renderQueue();
});

/* ================================================================ PGN input */

$("pgnFile").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (f) f.text().then((t) => { $("pgnText").value = t; });
});
const dz = $("dropzone");
["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => {
  const f = e.dataTransfer.files[0];
  if (f) f.text().then((t) => { $("pgnText").value = t; });
});
$("analyzePgn").addEventListener("click", () => {
  const side = $("sideSel").value;
  startAnalysis($("pgnText").value, side === "both" ? "both" : side || null);
});

function detectSide(pgn) {
  const w = (pgn.match(/\[White "(.*?)"\]/) || [])[1]?.toLowerCase() || "";
  const b = (pgn.match(/\[Black "(.*?)"\]/) || [])[1]?.toLowerCase() || "";
  for (const u of [state.profile?.chesscom_user, state.profile?.lichess_user]) {
    const name = (u || "").toLowerCase();
    if (name && w === name) return "white";
    if (name && b === name) return "black";
  }
  return null;
}

function ratingFor(pgn) {
  const p = state.profile;
  if (!p) return null;
  const cls = timeClass(pgn) || "rapid";
  return p[`elo_${cls}`] || p.elo_rapid || p.elo_blitz || p.elo_bullet || null;
}

/* ================================================================ analysis jobs */

let pollTimer = null;

async function startAnalysis(pgn, side, rating) {
  if (!pgn || !pgn.trim()) { toast("Paste a PGN first.", true); return; }
  if (side === "both") side = null;
  else if (!side) side = detectSide(pgn);
  let d;
  try { d = await api("/api/analyze", { method: "POST", body: { pgn, side, elo: rating || ratingFor(pgn) } }); }
  catch (e) { toast(e.message, true); return; }
  openJob(d.job_id, pgn);
  pollQueue();
}

/** Show a queued or running analysis on the Game page; moves stream in as they're ready. */
function openJob(jobId, pgn = "", fromRoute = false) {
  clearTimeout(pollTimer);
  state.game = { mode: "game", pgn, jobId, status: "queued", headers: {}, side: null, moves: [], review: "",
    opening: "", accuracy: {}, warnings: [], total: 0, coach: "" };
  resetGameView();
  $("navGame").classList.remove("hidden");
  showPage("game", fromRoute);
  $("progressCard").classList.remove("hidden");
  // Engine-only mode has nothing for a "Coach" bar to show.
  $("barCoach").closest(".bar-row").classList.toggle("hidden", state.health?.coach?.enabled === false);
  $("stopBtn").disabled = false;
  renderReview();
  poll();
}

async function poll() {
  const g = state.game;
  if (!g || !g.jobId) return;
  let j;
  try { j = await api(`/api/job/${g.jobId}?since=${g.moves.length}`); }
  catch (e) {
    toast(e.message, true); $("progressCard").classList.add("hidden"); g.jobId = null;
    if (!g.moves.length && state.game === g) { history.replaceState(null, "", "#/games"); showPage("games", true); }
    return;
  }
  if (state.game !== g) return;   // a different game was opened meanwhile
  const firstMoves = !g.moves.length && j.moves.length;
  Object.assign(g, { status: j.status, headers: j.headers, side: j.side, total: j.total, warnings: j.warnings });
  if (j.moves.length) {
    g.moves.push(...j.moves);
    if (state.cur < 0 && !state.practice) goTo(0); else renderMoveList();
    renderGraph();
  }
  if (firstMoves || !g.moves.length) renderHeader();
  if (!g.moves.length) {
    renderMoveList();
    $("explain").innerHTML = `<p class="small">The engine verdicts, commentary and coach's notes for each move will appear here.</p>`;
  }
  updateProgress(j);
  if (j.status === "queued" || j.status === "running") {
    pollTimer = setTimeout(poll, j.status === "queued" ? 2000 : 700);
    return;
  }
  Object.assign(g, { review: j.review, opening: j.opening, accuracy: j.accuracy, coach: j.coach, jobId: null,
    pgn: g.pgn || "", gameId: j.game_id || null });
  if (state.page === "game") syncRoute(true);   // #/job/… → #/game/12 (the job itself is gone after a restart)
  if (!g.pgn && j.game_id) {
    try { g.pgn = (await api(`/api/profile/game/${j.game_id}`)).pgn; } catch { /* export needs it; not critical */ }
  }
  $("progressCard").classList.add("hidden");
  renderHeader(); renderReview(); renderGraph();
  if (state.cur >= 0 && !state.practice) goTo(state.cur);
  if (j.status === "error") toast(j.error || "The analysis failed.", true);
  else if (j.status === "stopped") toast("Analysis stopped — showing the moves completed so far.");
  else if (j.status === "cancelled") toast("Removed from the queue.");
  else toast(j.game_id ? "Analysis complete and saved to your profile." : "Analysis complete.");
  loadHealth(); pollQueue();
}

function updateProgress(j) {
  const queued = j.status === "queued";
  $("progressBars").classList.toggle("hidden", queued);
  $("progressDot").className = `dot ${queued && j.paused ? "warn" : "busy"}`;
  $("stopBtn").textContent = queued ? "✕ Remove" : "■ Stop";
  const jump = $("jumpBtn");
  jump.classList.toggle("hidden", !queued || (!j.paused && j.position === 1));
  jump.textContent = j.paused ? "▶ Resume the queue" : "⤒ Analyse this next";
  if (queued) {
    $("progressLabel").textContent = j.paused ? "Waiting in the queue — the queue is paused"
      : j.ahead ? `Waiting in the queue — ${j.ahead} game${j.ahead === 1 ? "" : "s"} ahead` : "Starting…";
    $("progressEta").textContent = j.paused || j.start_in_s == null ? "" : `starts in about ${fmtDuration(j.start_in_s)}`;
    $("progressHint").textContent = j.prefetched
      ? `The engine is already working ahead on this game (${Math.min(100, Math.round((100 * j.prefetched) / (j.total + 1)))}% of positions searched).`
      : "It starts automatically — you can keep using Lucidfish meanwhile.";
    return;
  }
  const total = Math.max(1, j.total);
  $("progressLabel").textContent = j.label;
  $("barEngine").style.width = `${(100 * j.engine_done) / total}%`;
  $("barCoach").style.width = `${(100 * j.done) / total}%`;
  $("barEngineTxt").textContent = `${j.engine_done}/${j.total}`;
  $("barCoachTxt").textContent = `${j.done}/${j.total}`;
  $("progressEta").textContent = j.eta_s == null ? ""
    : `about ${fmtDuration(j.eta_s)} left · done around ${fmtClock(Date.now() / 1000 + j.eta_s)}`;
  $("progressHint").textContent = "Moves appear below as soon as they're ready — you can start reviewing now.";
}

$("stopBtn").addEventListener("click", async () => {
  const g = state.game;
  if (!g?.jobId) return;
  $("stopBtn").disabled = true;
  try { await api(`/api/job/${g.jobId}/stop`, { method: "POST" }); } catch (e) { toast(e.message, true); }
});
$("jumpBtn").addEventListener("click", async () => {
  const g = state.game;
  if (!g?.jobId) return;
  try {
    if (queue.data?.paused) await api("/api/queue/resume", { method: "POST" });
    await api(`/api/queue/${g.jobId}/move`, { method: "POST", body: { where: "top" } });
  } catch (e) { toast(e.message, true); }
  pollQueue();
});

async function openStoredGame(id, fromRoute = false) {
  let d;
  try { d = await api(`/api/profile/game/${id}`); }
  catch (e) {
    toast(e.message === "unknown game" ? "That game is no longer saved (it may have been deleted or re-analysed)." : e.message, true);
    if (fromRoute) { history.replaceState(null, "", "#/games"); showPage("games", true); }
    return;
  }
  clearTimeout(pollTimer);
  state.game = { mode: "game", gameId: d.id, pgn: d.pgn, headers: d.headers, side: d.side, moves: d.moves,
    review: d.review, opening: d.opening, accuracy: d.accuracy || {}, warnings: [], total: d.moves.length, status: "done" };
  resetGameView();
  $("navGame").classList.remove("hidden");
  showPage("game", fromRoute);
  renderHeader(); renderReview();
  goTo(d.moves.length ? 0 : -1);
}

function resetGameView() {
  state.cur = -1; state.preview = null; state.practice = null; state.gameChat.length = 0;
  $("gameChatLog").innerHTML = ""; $("moveList").innerHTML = ""; $("explain").innerHTML = "";
  $("review").innerHTML = ""; $("previewBar").classList.add("hidden");
  $("progressCard").classList.add("hidden");
}

/* ================================================================ header / review */

function renderHeader() {
  const g = state.game;
  if (!g) return;
  if (g.mode === "position") {
    $("players").textContent = "Custom position";
    $("gameMeta").textContent = `${g.position.turn} to move · coaching ${g.position.perspective}`;
    $("accPills").innerHTML = "";
    $("exportRow").querySelectorAll("#exportPgn,#exportMd").forEach((b) => b.classList.add("hidden"));
  } else {
    const h = g.headers || {};
    $("players").innerHTML = `${esc(h.White || "White")}<span class="vs">vs</span>${esc(h.Black || "Black")}`
      + (h.Result ? ` <span class="chip">${esc(h.Result)}</span>` : "");
    $("gameMeta").textContent = [g.opening, h.Date && h.Date !== "????.??.??" ? h.Date : "", g.coach ? `coach: ${g.coach}` : ""]
      .filter(Boolean).join(" · ");
    const acc = g.accuracy || {};
    $("accPills").innerHTML = ["white", "black"].filter((s) => acc[s] != null)
      .map((s) => `<div class="acc"><b>${acc[s]}%</b><span>${s} accuracy</span></div>`).join("");
    $("exportRow").querySelectorAll("#exportPgn,#exportMd").forEach((b) => b.classList.toggle("hidden", !g.moves.length));
  }
  $("practiceAll").classList.toggle("hidden", !(g.mode === "game" && !g.jobId && myMistakes().length));
  $("reanalyseBtn").classList.toggle("hidden", !(g.mode === "game" && g.gameId && !g.jobId));
  $("gameWarnings").innerHTML = (g.warnings || []).map((w) =>
    `<div class="banner" style="margin:12px 0 0"><div class="grow small">${esc(w)}</div></div>`).join("");
}

function renderReview() {
  const g = state.game;
  $("reviewCard").classList.toggle("hidden", g.mode === "position");
  $("review").innerHTML = g.review ? md(g.review)
    : `<div class="small">${g.jobId ? "Appears when the analysis finishes." : "No review for this game (the AI coach was off or unavailable)."}</div>`;
}

/* ================================================================ board */

function ensureBoard() {
  if (board) return;
  board = Chessboard("board", {
    position: "start", pieceTheme: PIECES, showNotation: true, moveSpeed: 120, snapSpeed: 60,
    draggable: true,   // pieces only move in practice mode (see onDragStart)
    onDragStart: (source, piece) => {
      const p = state.practice;
      if (!p || p.busy || p.result || state.preview) return false;
      return piece[0] === p.fen.split(" ")[1];   // only the side to move
    },
    onDrop: (source, target, piece) => {
      if (!state.practice || target === "offboard" || source === target) return "snapback";
      const promotes = piece[1] === "P" && (target[1] === "8" || target[1] === "1");
      tryPracticeMove(source + target + (promotes ? "q" : ""));
      return undefined;
    },
  });
}

function orientation() { return board ? board.orientation() : "white"; }

function refreshBoard() {
  if (!board || !state.game) return;
  if (state.preview) { stepPreview(0); return; }
  if (state.practice) { practiceBoard(); return; }
  if (state.game.mode === "position") {
    board.position(state.game.position.fen, false);
    positionOverlay();
    return;
  }
  if (state.cur >= 0) goTo(state.cur, false);
}

/** Square centre in board units (each square is 1×1; the board is 8×8). */
function sqXY(sq) {
  const f = sq.charCodeAt(0) - 97, r = parseInt(sq[1], 10) - 1;
  const white = orientation() === "white";
  return [(white ? f : 7 - f) + 0.5, (white ? 7 - r : r) + 0.5];
}

function drawOverlay({ arrows = [], squares = [] }) {
  const svg = $("overlay"), el = document.querySelector("#board .board-b72b1");
  if (!el) return;
  const holder = $("boardHolder").getBoundingClientRect(), rect = el.getBoundingClientRect();
  Object.assign(svg.style, { left: `${rect.left - holder.left}px`, top: `${rect.top - holder.top}px`,
    width: `${rect.width}px`, height: `${rect.height}px` });
  $("evalbar").style.height = `${rect.height}px`;   // keep the eval bar exactly as tall as the board
  svg.setAttribute("viewBox", "0 0 8 8");
  let out = "";
  for (const s of squares) {
    if (!s.sq) continue;
    const [x, y] = sqXY(s.sq);
    out += `<rect x="${x - 0.5}" y="${y - 0.5}" width="1" height="1" fill="${s.color}"/>`;
  }
  for (const a of arrows) {
    if (!a.from || !a.to || a.from === a.to) continue;
    const [x1, y1] = sqXY(a.from), [x2, y2] = sqXY(a.to);
    const len = Math.hypot(x2 - x1, y2 - y1), ux = (x2 - x1) / len, uy = (y2 - y1) / len;
    const hx = x2 - ux * 0.38, hy = y2 - uy * 0.38;
    out += `<line x1="${x1}" y1="${y1}" x2="${hx}" y2="${hy}" stroke="${a.color}" stroke-width="0.16" stroke-linecap="round" opacity="0.85"/>`
      + `<polygon points="${x2 - ux * 0.08},${y2 - uy * 0.08} ${hx - uy * 0.22},${hy + ux * 0.22} ${hx + uy * 0.22},${hy - ux * 0.22}" fill="${a.color}" opacity="0.85"/>`;
  }
  svg.innerHTML = out;
}

function setEvalBar(win, label) {
  $("evalWhite").style.height = `${Math.max(2, Math.min(98, win))}%`;
  $("evalbar").classList.toggle("flipped", orientation() === "black");
  $("evalLabel").textContent = label || "";
}

$("btnFirst").addEventListener("click", () => goTo(0));
$("btnPrev").addEventListener("click", () => (state.preview ? stepPreview(-1) : goTo(state.cur - 1)));
$("btnNext").addEventListener("click", () => (state.preview ? stepPreview(1) : goTo(state.cur + 1)));
$("btnLast").addEventListener("click", () => goTo((state.game?.moves.length || 0) - 1));
$("btnFlip").addEventListener("click", flip);
$("btnPrevKey").addEventListener("click", () => jumpKeyMoment(-1));
$("btnNextKey").addEventListener("click", () => jumpKeyMoment(1));

/** Critical moments, every mistake or blunder, and your own inaccuracies. */
function isKeyMoment(m, g) {
  const mine = !g.side || m.side.toLowerCase() === g.side;
  return m.critical || m.cls === "mistake" || m.cls === "blunder" || (mine && m.cls === "inaccuracy");
}

function jumpKeyMoment(dir) {
  const g = state.game;
  if (!g || g.mode !== "game" || !g.moves.length) return;
  for (let i = state.cur + dir; i >= 0 && i < g.moves.length; i += dir) {
    if (isKeyMoment(g.moves[i], g)) { goTo(i); return; }
  }
  toast(dir > 0 ? "No more key moments after this move." : "No key moments before this move.");
}

function flip() {
  if (!board) return;
  board.flip();
  refreshBoard();
  if (state.game?.mode === "game" && state.cur >= 0 && !state.practice) setEvalBar(winOf(state.game.moves[state.cur]), evalWords(state.game.moves[state.cur].eval));
}

/* ================================================================ moves */

function goTo(i, rerender = true) {
  const g = state.game;
  if (!g || g.mode !== "game" || !g.moves.length) return;
  ensureBoard();
  state.preview = null; $("previewBar").classList.add("hidden");
  state.practice = null;
  const prev = state.cur;
  state.cur = Math.max(0, Math.min(g.moves.length - 1, i));
  const m = g.moves[state.cur];
  if (prev === -1 && board.orientation() !== ((g.side || "white") === "black" ? "black" : "white")) {
    board.orientation(g.side === "black" ? "black" : "white");
  }
  board.position(m.fen_after, rerender && Math.abs(state.cur - prev) === 1);
  const arrows = [];
  if (m.best_from && m.cls !== "best" && m.best !== m.san) arrows.push({ from: m.best_from, to: m.best_to, color: "#1f9d6b" });
  drawOverlay({ arrows, squares: [{ sq: m.from, color: "rgba(255, 214, 10, .38)" }, { sq: m.to, color: "rgba(255, 214, 10, .55)" }] });
  setEvalBar(winOf(m), evalWords(m.eval));
  renderMoveList();
  renderExplain(m);
  renderGraph();
}

function renderMoveList() {
  const story = state.moveView === "story";
  $("moveList").classList.toggle("hidden", story);
  $("storyList").classList.toggle("hidden", !story);
  if (story) renderStory(); else renderMoveGrid();
}

function renderMoveGrid() {
  const g = state.game, box = $("moveList");
  if (!g || g.mode !== "game") return;
  if (!g.moves.length) {
    box.innerHTML = `<div class="small" style="grid-column: span 3">${g.jobId ? "Moves appear here as soon as the analysis starts." : "No moves."}</div>`;
    return;
  }
  const h = g.headers || {};
  let html = `<div class="moves-head"></div><div class="moves-head" title="${esc(h.White || "")}">♔ ${esc(h.White || "White")}</div>`
    + `<div class="moves-head" title="${esc(h.Black || "")}">♚ ${esc(h.Black || "Black")}</div>`;
  const cell = (m, i) => m
    ? `<div class="mv${i === state.cur ? " sel" : ""}" data-i="${i}"><span>${m.critical ? `<span class="crit" title="Critical moment">⚡</span>` : ""}<span class="san">${esc(m.san)}</span></span>${badge(m.cls)}</div>`
    : `<div class="mv empty"></div>`;
  for (let i = 0; i < g.moves.length;) {
    const m = g.moves[i];
    html += `<div class="mv-num">${m.n}.</div>`;
    if (m.side === "White") {
      const next = g.moves[i + 1];
      html += cell(m, i) + (next && next.side === "Black" ? cell(next, i + 1) : cell(null));
      i += next && next.side === "Black" ? 2 : 1;
    } else { html += cell(null) + cell(m, i); i += 1; }
  }
  if (g.jobId && g.moves.length < g.total) {
    html += `<div class="mv-num"></div><div class="mv pending" style="grid-column: span 2">analysing… ${g.moves.length}/${g.total}</div>`;
  }
  box.innerHTML = html;
  const sel = box.querySelector(".mv.sel");
  if (sel) sel.scrollIntoView({ block: "nearest" });
}
$("moveList").addEventListener("click", (e) => {
  const cell = e.target.closest(".mv[data-i]");
  if (cell) goTo(parseInt(cell.dataset.i, 10));
});

/** The game as a running commentary: every move with the commentator's line. */
function renderStory() {
  const g = state.game, box = $("storyList");
  if (!g || g.mode !== "game") return;
  const hasFlow = g.moves.some((m) => m.flow);
  let html = hasFlow || !g.moves.length ? "" : `<p class="small story-note">This game was analysed without running commentary, `
    + `so the story shows the coach's notes only. Choose <b>Commentary</b> in Settings → Analysis for a line on every move.</p>`;
  let phase = "";
  g.moves.forEach((m, i) => {
    if (m.phase && m.phase !== phase) { phase = m.phase; html += `<div class="story-phase">${esc(cap(phase))}</div>`; }
    const text = m.flow || (m.expl ? m.expl.split(/(?<=[.!?])\s/)[0] : "");
    const mark = ERRORS.includes(m.cls) ? badge(m.cls) : m.critical ? `<span class="crit" title="Critical moment">⚡</span>` : "";
    html += `<div class="story-row${i === state.cur ? " sel" : ""}${m.side === "White" ? " w" : " b"}" data-i="${i}">`
      + `<span class="story-move">${m.side === "White" ? `${m.n}.` : `${m.n}…`} ${esc(m.san)} ${mark}</span>`
      + `<span class="story-text">${text ? esc(text) : `<span class="muted">${m.book ? "Opening theory." : "—"}</span>`}</span></div>`;
  });
  if (g.jobId && g.moves.length < g.total) html += `<div class="story-row pending">analysing… ${g.moves.length}/${g.total}</div>`;
  box.innerHTML = html;
  const sel = box.querySelector(".story-row.sel");
  if (sel) sel.scrollIntoView({ block: "nearest" });
}
$("storyList").addEventListener("click", (e) => {
  const row = e.target.closest(".story-row[data-i]");
  if (row) goTo(parseInt(row.dataset.i, 10));
});
document.querySelectorAll("#moveViewSeg button").forEach((b) => b.addEventListener("click", () => {
  state.moveView = b.dataset.view;
  document.querySelectorAll("#moveViewSeg button").forEach((x) => x.classList.toggle("active", x === b));
  try { localStorage.setItem("lucidfish-move-view", state.moveView); } catch { /* ignore */ }
  renderMoveList();
}));
try {
  const saved = localStorage.getItem("lucidfish-move-view");
  if (saved === "story" || saved === "moves") {
    state.moveView = saved;
    document.querySelectorAll("#moveViewSeg button").forEach((x) => x.classList.toggle("active", x.dataset.view === saved));
  }
} catch { /* ignore */ }

/** Alternatives worth showing: only when the move wasn't (close to) the best. */
function pickAlternatives(m) {
  const cands = m.candidates || [];
  const played = cands.find((c) => c.san === m.san);
  const alts = cands.filter((c) => c.san !== m.san).sort((a, b) => b.cp - a.cp);
  if (!alts.length || m.cls === "best") return [];
  const top = Math.max(alts[0].cp, played ? played.cp : -Infinity);
  if (played && top - played.cp <= 20) return [];
  if (alts.length > 1 && alts[0].cp - alts[1].cp >= 150) return [alts[0]];
  const floor = played ? played.cp + 20 : alts[0].cp - 80;
  return alts.filter((c) => c.cp >= floor && alts[0].cp - c.cp <= 80).slice(0, 3);
}

function lineHtml(key, title, score, pv, idea, extraClass = "") {
  return `<div class="line clickable ${extraClass}" data-prev="${key}" title="Click to play this line on the board">`
    + `${title ? `<b>${esc(title)}</b>` : ""}${score ? `<span class="ev">${esc(score)}</span>` : ""}`
    + `<span class="pv">${esc(pv)}</span>${idea ? `<span class="idea">↳ ${esc(idea)}</span>` : ""}</div>`;
}

function renderExplain(m) {
  const g = state.game;
  const opponent = g.side && m.side.toLowerCase() !== g.side;
  const think = m.think_s != null ? ` · ⏱ ${m.think_s < 10 ? m.think_s.toFixed(1) : Math.round(m.think_s)}s` : "";
  let html = `<div class="expl-title"><span class="san">${m.side === "White" ? m.n + "." : m.n + "…"} ${esc(m.san)}</span>`
    + `<span class="verdict v-${esc(m.cls)}">${badge(m.cls)} ${esc(LABELS[m.cls] || m.cls)}</span>`
    + `<span class="small">${esc(evalWords(m.eval))}${m.acc != null ? ` · accuracy ${Math.round(m.acc)}%` : ""}${think}</span>`
    + (m.critical ? `<span class="chip tag">⚡ critical moment</span>` : "")
    + (m.book ? `<span class="chip" title="A known opening move">📖 book</span>` : "")
    + (opponent ? `<span class="chip">opponent</span>` : "") + `</div>`;
  if (m.tags && m.tags.length) html += `<div class="row" style="margin-bottom:10px">${m.tags.map((t) => `<span class="chip tag">${esc(t)}</span>`).join("")}</div>`;
  if (m.flow) html += `<div class="flow"><span class="flow-label">🎙 Commentary</span>${esc(m.flow)}</div>`;
  if (!opponent && ERRORS.includes(m.cls) && !g.jobId) {
    html += `<div class="row" style="margin:4px 0 10px"><button class="sm" data-practice="${state.cur}">🎯 Find a better move yourself</button></div>`;
  }
  if (m.expl) {
    html += (opponent ? `<span class="opp-label">What your opponent is up to</span>` : m.flow ? `<div class="section-label">Coach's note</div>` : "")
      + `<div class="md">${md(m.expl)}</div>`;
  } else if (m.flow) {
    /* the commentary line says it all */
  } else if (g.jobId) {
    html += `<p class="small">${m.cls === "best" || m.cls === "good" ? "A solid move — no note needed." : "No note for this move."}</p>`;
  } else if (ERRORS.includes(m.cls)) {
    html += `<p class="small">No written note for this move${m.best ? ` — the engine preferred <b>${esc(m.best)}</b>` : ""}. Ask the coach below for details.</p>`;
  } else {
    html += `<p class="small">A solid move — the engine has no complaint. Ask the coach below if you're curious about it.</p>`;
  }
  state.previewMap = {};
  if (!opponent) {
    const alts = pickAlternatives(m);
    if (alts.length) {
      html += `<div class="section-label">${ERRORS.includes(m.cls) ? "Better was" : "Alternatives"} · click to play it out</div>`;
      alts.forEach((c, k) => {
        state.previewMap[`alt${k}`] = { steps: c.steps || [], base: m.fen_before, label: `${c.san} line` };
        html += lineHtml(`alt${k}`, c.san, c.score, c.line, c.idea);
      });
    }
    if (m.refutation) {
      state.previewMap.ref = { steps: m.refutation_steps || [], base: m.fen_after, label: `refutation of ${m.san}` };
      html += `<div class="section-label">Why it's bad · click to watch the punishment</div>` + lineHtml("ref", "", "", m.refutation, "");
    }
  }
  if (m.opening && m.opening.length) {
    html += `<div class="section-label">Opening book (masters)</div><div class="line"><span class="pv">${m.opening.map(esc).join("<br>")}</span></div>`;
  }
  $("explain").innerHTML = html;
}
$("explain").addEventListener("click", (e) => {
  const el = e.target.closest("[data-prev]");
  if (el) { startPreview(el.dataset.prev); return; }
  const pr = e.target.closest("[data-practice]");
  if (pr) { startPractice(parseInt(pr.dataset.practice, 10)); return; }
  const act = e.target.closest("[data-pa]");
  if (act) practiceAction(act.dataset.pa);
});

/* ================================================================ practise your mistakes */

/** Indices of the coached player's inaccuracies, mistakes and blunders. */
function myMistakes() {
  const g = state.game;
  if (!g || g.mode !== "game") return [];
  return g.moves.map((m, i) => [m, i])
    .filter(([m]) => ERRORS.includes(m.cls) && (!g.side || m.side.toLowerCase() === g.side) && m.fen_before)
    .map(([, i]) => i);
}

function startPractice(i) {
  const g = state.game, m = g?.moves[i];
  if (!m) return;
  goTo(i, false);   // select the move (and leave any preview)
  state.practice = { i, fen: m.fen_before, busy: false, result: null, tries: 0 };
  board.orientation(m.side.toLowerCase());
  practiceBoard();
  renderPractice();
  $("board").scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function practiceBoard() {
  const p = state.practice, g = state.game;
  board.position(p.result ? p.result.fen_after : p.fen, false);
  if (p.result) {
    const u = p.result.uci, color = p.result.solved ? "rgba(31, 157, 107, .5)" : "rgba(224, 70, 75, .45)";
    drawOverlay({ arrows: [], squares: [{ sq: u.slice(0, 2), color }, { sq: u.slice(2, 4), color }] });
    setEvalBar(winFromEval(p.result.eval), evalWords(p.result.eval));
  } else {
    drawOverlay({ arrows: [], squares: [] });
    const before = p.i > 0 ? g.moves[p.i - 1] : null;
    setEvalBar(before ? winOf(before) : 50, "Your move");
  }
}

function renderPractice() {
  const p = state.practice, g = state.game, m = g.moves[p.i];
  const label = `${m.side === "White" ? `${m.n}.` : `${m.n}…`}`;
  const others = myMistakes().filter((i) => i !== p.i);
  let html = `<div class="expl-title"><span class="san">🎯 Practice · ${esc(label)}</span>`
    + `<span class="small">In the game you played <b>${esc(m.san)}</b> ${badge(m.cls)} — can you find something better?</span></div>`;
  state.previewMap = {};
  if (p.busy) html += `<div class="callout">Checking your move with the engine…</div>`;
  else if (!p.result) {
    html += `<div class="callout">Drag a ${esc(m.side)} piece to play your move. Take your time: look for checks, captures and threats first. `
      + `(Pawns promote to a queen.)</div>`;
  } else if (p.result.solved) {
    const r = p.result;
    html += `<div class="callout good"><b>✓ ${esc(r.san)} — ${esc(LABELS[r.cls] || r.cls)}!</b> `
      + (r.cls === "best" ? "That's the engine's top choice." : `Good enough — the engine's first choice was <b>${esc(r.best)}</b>.`)
      + ` ${esc(evalWords(r.eval))}.</div>`;
  } else {
    const r = p.result;
    html += `<div class="callout bad"><b>✗ ${esc(r.san)} is ${r.cls === "inaccuracy" ? "an" : "a"} ${esc(r.cls)} too.</b> ${esc(evalWords(r.eval))}.</div>`;
    if (r.refutation_steps?.length) {
      state.previewMap.pref = { steps: r.refutation_steps, base: r.fen_after, label: `why ${r.san} fails` };
      html += `<div class="section-label">Why it doesn't work · click to watch</div>` + lineHtml("pref", "", "", r.refutation, "");
    }
  }
  if (p.result && !p.result.solved) html += `<div class="row" style="margin-top:12px"><button class="sm" data-pa="retry">↺ Try again</button>`;
  else html += `<div class="row" style="margin-top:12px">`;
  if (!p.result?.solved) html += `<button class="secondary sm" data-pa="answer">Show the answer</button>`;
  if (others.length) html += `<button class="secondary sm" data-pa="next">Next mistake →</button>`;
  html += `<div class="spacer"></div><button class="ghost sm" data-pa="exit">Exit practice</button></div>`;
  if (p.showAnswer) {
    const best = m.candidates?.[0];
    state.previewMap.pbest = { steps: best?.steps || [], base: p.fen, label: "the engine's line" };
    html += `<div class="section-label">The answer · click to play it out</div>`
      + lineHtml("pbest", m.best, best?.score || "", best?.line || m.best, best?.idea || "");
  }
  $("explain").innerHTML = html;
}

async function tryPracticeMove(uci) {
  const p = state.practice;
  p.busy = true; p.tries += 1;
  renderPractice();
  let r;
  try { r = await api("/api/check_move", { method: "POST", body: { fen: p.fen, uci } }); }
  catch (e) {
    if (state.practice !== p) return;
    p.busy = false; toast(e.message, true); practiceBoard(); renderPractice();
    return;
  }
  if (state.practice !== p) return;   // left practice while the engine was thinking
  p.busy = false; p.result = r;
  if (r.solved) p.showAnswer = false;
  practiceBoard();
  renderPractice();
}

function practiceAction(action) {
  const p = state.practice;
  if (!p) return;
  if (action === "retry") { p.result = null; practiceBoard(); renderPractice(); }
  else if (action === "answer") { p.showAnswer = true; renderPractice(); }
  else if (action === "exit") { goTo(p.i); }
  else if (action === "next") {
    const list = myMistakes();
    const next = list.find((i) => i > p.i) ?? list[0];
    if (next != null) startPractice(next);
  }
}
$("reanalyseBtn").addEventListener("click", async () => {
  const g = state.game;
  if (!g?.gameId) return;
  try { openJob(await reanalyse(g.gameId), g.pgn); } catch (e) { toast(e.message, true); }
});
$("practiceAll").addEventListener("click", () => {
  const list = myMistakes();
  if (!list.length) return;
  startPractice(list.find((i) => i >= state.cur) ?? list[0]);
});

/* ================================================================ eval graph */

function renderGraph() {
  const svg = $("graph"), g = state.game;
  const show = g && g.mode === "game" && g.moves.length;
  $("graphWrap").classList.toggle("hidden", !show);
  if (!show) return;
  const W = Math.max(200, svg.clientWidth || 400), H = 110;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const n = Math.max(g.total || 0, g.moves.length);
  const x = (i) => (i / n) * W, y = (w) => H - (w / 100) * H;
  let pts = `0,${y(50)}`;
  g.moves.forEach((m, i) => { pts += ` ${x(i + 1)},${y(winOf(m))}`; });
  const last = x(g.moves.length);
  const colors = { inaccuracy: "var(--c-inaccuracy)", mistake: "var(--c-mistake)", blunder: "var(--c-blunder)" };
  let marks = "";
  g.moves.forEach((m, i) => {
    const c = colors[m.cls] || (m.critical ? "var(--c-critical)" : null);
    if (c) marks += `<circle cx="${x(i + 1)}" cy="${y(winOf(m))}" r="3.5" fill="${c}" stroke="#fff" stroke-width="1"/>`;
  });
  const cur = state.cur >= 0 ? `<line class="g-cursor" x1="${x(state.cur + 1)}" x2="${x(state.cur + 1)}" y1="0" y2="${H}"/>` : "";
  svg.innerHTML = `<rect class="g-bg" x="0" y="0" width="${W}" height="${H}"/>`
    + `<polygon class="g-white" points="0,${H} ${pts} ${last},${H}"/>`
    + `<line class="g-mid" x1="0" x2="${W}" y1="${H / 2}" y2="${H / 2}"/>${cur}${marks}`;
}

function graphIndex(e) {
  const g = state.game, rect = $("graph").getBoundingClientRect();
  const n = Math.max(g.total || 0, g.moves.length);
  return Math.min(g.moves.length - 1, Math.max(0, Math.round(((e.clientX - rect.left) / rect.width) * n) - 1));
}
$("graph").addEventListener("click", (e) => { if (state.game?.moves.length) goTo(graphIndex(e)); });
$("graph").addEventListener("mousemove", (e) => {
  if (!state.game?.moves.length) return;
  const m = state.game.moves[graphIndex(e)];
  $("graphTip").textContent = `${m.n}${m.side === "White" ? "." : "…"} ${m.san} — ${LABELS[m.cls] || m.cls} · ${evalWords(m.eval)}`;
});
$("graph").addEventListener("mouseleave", () => { $("graphTip").textContent = ""; });

/* ================================================================ variation preview */

function startPreview(key) {
  const p = state.previewMap[key];
  if (!p || !p.steps.length) return;
  state.preview = { ...p, idx: -1 };
  $("previewBar").classList.remove("hidden");
  $("previewLabel").textContent = p.label;
  stepPreview(1);
}

function stepPreview(d) {
  const p = state.preview;
  if (!p) return;
  p.idx = Math.max(-1, Math.min(p.steps.length - 1, p.idx + d));
  const s = p.idx >= 0 ? p.steps[p.idx] : null;
  board.position(s ? s.fen : p.base, true);
  drawOverlay({ arrows: s ? [{ from: s.from, to: s.to, color: "#e2a400" }] : [], squares: [] });
  $("previewPos").textContent = ` · move ${p.idx + 1} of ${p.steps.length} (◀ ▶ to step)`;
}

function exitPreview() {
  state.preview = null;
  $("previewBar").classList.add("hidden");
  if (state.practice) practiceBoard();
  else if (state.game?.mode === "position") refreshBoard();
  else goTo(state.cur, false);
}
$("previewExit").addEventListener("click", exitPreview);

/* ================================================================ board editor */

function ensureEditor() {
  if (editorBoard) { setTimeout(() => editorBoard.resize(), 0); return; }
  setTimeout(() => {
    editorBoard = Chessboard("editorBoard", {
      position: "start", pieceTheme: PIECES, draggable: true, dropOffBoard: "trash", sparePieces: true,
      onChange: () => setTimeout(() => { $("edFen").value = editorFen(); }, 0),
    });
    $("edFen").value = editorFen();
  }, 0);
}

/** Full FEN for the editor position, inferring castling rights from king/rook placement. */
function editorFen() {
  const pos = editorBoard.position();
  const has = (sq, piece) => pos[sq] === piece;
  let castle = "";
  if (has("e1", "wK")) { if (has("h1", "wR")) castle += "K"; if (has("a1", "wR")) castle += "Q"; }
  if (has("e8", "bK")) { if (has("h8", "bR")) castle += "k"; if (has("a8", "bR")) castle += "q"; }
  return `${editorBoard.fen()} ${$("edTurn").value} ${castle || "-"} - 0 1`;
}

$("edTurn").addEventListener("change", () => { $("edFen").value = editorFen(); });
$("edStart").addEventListener("click", () => { editorBoard.start(); $("edTurn").value = "w"; });
$("edClear").addEventListener("click", () => editorBoard.clear());
$("edFlip").addEventListener("click", () => editorBoard.flip());
$("edLoad").addEventListener("click", () => {
  const fen = $("edFen").value.trim();
  const parts = fen.split(/\s+/);
  if (!/^([pnbrqkPNBRQK1-8]{1,8}\/){7}[pnbrqkPNBRQK1-8]{1,8}$/.test(parts[0] || "")) { toast("That doesn't look like a FEN.", true); return; }
  editorBoard.position(parts[0], false);
  if (parts[1] === "b" || parts[1] === "w") $("edTurn").value = parts[1];
  setTimeout(() => { $("edFen").value = fen; }, 0);
});

$("edAnalyze").addEventListener("click", async () => {
  const typed = $("edFen").value.trim();
  const fen = typed && typed.split(/\s+/)[0] === editorBoard.fen() ? typed : editorFen();
  const btn = $("edAnalyze");
  btn.disabled = true; btn.textContent = "Analysing…";
  let d;
  try { d = await api("/api/position", { method: "POST", body: { fen, perspective: $("edPersp").value, level: state.profile?.level || null } }); }
  catch (e) { toast(e.message, true); return; }
  finally { btn.disabled = false; btn.textContent = "Analyze position"; }
  clearTimeout(pollTimer);
  state.game = { mode: "position", position: d, moves: [], warnings: d.warnings || [], headers: {} };
  resetGameView();
  $("navGame").classList.remove("hidden");
  showPage("game");
  board.orientation($("edPersp").value);
  renderHeader(); renderReview(); renderPosition();
});

/** Board-editor mode: arrow for the engine's top move. */
function positionOverlay() {
  const first = state.game.position.lines[0]?.steps?.[0];
  drawOverlay({ arrows: first ? [{ from: first.from, to: first.to, color: "#1f9d6b" }] : [], squares: [] });
}

function renderPosition() {
  const d = state.game.position;
  board.position(d.fen, false);
  positionOverlay();
  const best = d.lines[0];
  setEvalBar(best ? best.win : 50, best ? evalWords(best.score) : "");
  $("moveList").innerHTML = `<div class="small" style="grid-column: span 3">Position mode — there is no move list.</div>`;
  state.previewMap = {};
  let html = `<div class="expl-title"><span class="san">Position assessment</span><span class="small">${esc(d.perspective)}'s perspective · ${esc(d.turn)} to move</span></div>`;
  html += d.commentary ? `<div class="md">${md(d.commentary)}</div>` : `<p class="small">No written assessment (the AI coach is off or unavailable). The engine lines are below.</p>`;
  html += `<div class="section-label">Engine lines · click to play one out</div>`;
  d.lines.forEach((c, k) => {
    state.previewMap[`pos${k}`] = { steps: c.steps || [], base: d.fen, label: `${c.san} line` };
    html += lineHtml(`pos${k}`, c.san, c.score, c.line, c.idea);
  });
  html += `<div class="section-label">Position facts (verified from the board)</div><ul class="facts">${d.features.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>`;
  $("explain").innerHTML = html;
  renderGraph();
}

/* ================================================================ export */

async function exportGame(format) {
  const g = state.game;
  if (!g || g.mode !== "game" || !g.moves.length) return;
  try {
    const res = await api("/api/export", { method: "POST", raw: true, body: {
      format, pgn: g.pgn, headers: g.headers, moves: g.moves, review: g.review || "", opening: g.opening || "",
      accuracy: g.accuracy || {}, side: g.side || null } });
    const blob = await res.blob();
    const name = (res.headers.get("content-disposition") || "").match(/filename="(.+?)"/)?.[1] || `game.${format}`;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  } catch (e) { toast(e.message, true); }
}
$("exportPgn").addEventListener("click", () => exportGame("pgn"));
$("exportMd").addEventListener("click", () => exportGame("md"));
$("copyFen").addEventListener("click", async () => {
  const g = state.game;
  if (!g) return;
  const fen = g.mode === "position" ? g.position.fen : g.moves[state.cur]?.fen_after;
  if (!fen) return;
  try { await navigator.clipboard.writeText(fen); toast("FEN copied."); } catch { toast(fen); }
});

/* ================================================================ chat */

function gameContext() {
  const g = state.game;
  if (!g) return "";
  if (g.mode === "position") {
    const d = g.position;
    return `Custom position FEN: ${d.fen} (${d.turn} to move; coaching ${d.perspective}).\nEngine lines:\n`
      + d.lines.map((c) => `${c.san} (${c.score}): ${c.line}`).join("\n") + `\nPosition facts:\n${d.features.join("\n")}`
      + (d.commentary ? `\nCoach assessment: ${d.commentary}` : "");
  }
  const m = g.moves[state.cur];
  if (!m) return "";
  const h = g.headers || {};
  let ctx = `Game: ${h.White} vs ${h.Black}, ${h.Result || ""}. ${g.opening ? `Opening: ${g.opening}.` : ""}\n`
    + (g.side ? `The player is ${g.side}.\n` : "")
    + `Currently viewing move ${m.n} (${m.side}): ${m.san} — verdict ${m.cls}, evaluation after it: ${evalWords(m.eval)}.\n`
    + (m.tags?.length ? `Tactical tags: ${m.tags.join(", ")}.\n` : "")
    + `FEN after the move: ${m.fen_after}\nEngine candidates before it:\n`
    + (m.candidates || []).map((c) => `${c.san} (${c.score}): ${c.line}`).join("\n");
  if (m.refutation) ctx += `\nRefutation of the played move: ${m.refutation}`;
  if (m.flow) ctx += `\nCommentary on this move: ${m.flow}`;
  if (m.expl) ctx += `\nCoach note for this move: ${m.expl}`;
  if (g.review) ctx += `\nWhole-game review:\n${g.review}`;
  return ctx;
}

function reviewContext() {
  let ctx = "The player is discussing their overall coach review (not a specific game).";
  if (state.profile?.summary) ctx += `\nCurrent coach review:\n${state.profile.summary}`;
  return ctx;
}

function bindChat(logId, inputId, sendId, history, contextFn) {
  const log = $(logId), input = $(inputId), send = $(sendId);
  const add = (cls, text) => {
    const div = document.createElement("div");
    div.className = `msg ${cls}`;
    if (cls === "coach") div.innerHTML = md(text); else div.textContent = text;
    log.appendChild(div); log.scrollTop = log.scrollHeight;
    return div;
  };
  async function go() {
    const text = input.value.trim();
    if (!text || send.disabled) return;
    input.value = "";
    history.push({ role: "user", content: text });
    add("user", text);
    const thinking = add("think", "Coach is thinking…");
    send.disabled = true;
    try {
      const d = await api("/api/chat", { method: "POST", body: { messages: history.slice(-12), context: contextFn() } });
      history.push({ role: "assistant", content: d.reply });
      add("coach", d.reply);
    } catch (e) {
      history.pop();
      add("coach", `⚠ ${e.message}`);
    } finally { thinking.remove(); send.disabled = false; input.focus(); }
  }
  send.addEventListener("click", go);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
}
bindChat("gameChatLog", "gameChatInput", "gameChatSend", state.gameChat, gameContext);
bindChat("reviewChatLog", "reviewChatInput", "reviewChatSend", state.reviewChat, reviewContext);

/* ================================================================ settings */

const DETAIL_NOTES = {
  key: "Notes only for your mistakes and the game's critical moments, no running commentary. Fastest — good for slower computers.",
  standard: "A commentator's line on every move by both players — the plans and intentions behind them — plus a full explanation of each of your mistakes and the critical moments. Written 8 moves at a time, so it stays fast. Recommended.",
  full: "A detailed note on every move by both players. Slowest — best with a fast GPU or a cloud model.",
};
let draft = null;

async function openSettings(tab = "coach") {
  let s;
  try { s = await api("/api/settings"); } catch (e) { toast(e.message, true); return; }
  state.settings = s;
  draft = { provider: s.provider, detail: s.detail, models: {}, urls: {} };
  for (const p of s.providers) { draft.models[p.id] = p.model || p.default_model; draft.urls[p.id] = p.base_url; }
  draft.models[s.provider] = s.model;
  $("setLlmEnabled").checked = s.llm_enabled;
  $("setFactcheck").checked = s.factcheck;
  $("setPrefetch").checked = s.prefetch !== false;
  $("setConcurrency").value = s.concurrency || "";
  $("setDepth").value = s.depth;
  $("setThreads").value = s.threads; $("setThreads").max = s.max_threads;
  $("setHash").value = s.hash_mb;
  $("setStockfish").value = s.stockfish_path || "";
  $("stockfishDetected").textContent = s.stockfish_path ? "" : `Auto-detected: ${s.stockfish_detected}`;
  $("aboutData").textContent = s.data_dir;
  $("settingsError").textContent = "";
  $("testResult").textContent = "";
  $("depthSeg").innerHTML = Object.entries(s.depth_presets)
    .map(([name, d]) => `<button data-depth="${d}">${name[0].toUpperCase() + name.slice(1)} · ${d}</button>`).join("");
  renderProviders(); renderDetail(); syncDepth(); renderCoachEnabled();
  api("/api/cache").then((c) => { $("cacheInfo").textContent = `${c.engine_positions.toLocaleString()} engine positions · ${c.opening_positions.toLocaleString()} opening positions stored`; }).catch(() => {});
  selectTab(tab);
  openModal("settingsModal");
}
$("settingsBtn").addEventListener("click", () => openSettings());

function selectTab(tab) {
  document.querySelectorAll("#settingsTabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll("[data-tab-panel]").forEach((p) => p.classList.toggle("hidden", p.dataset.tabPanel !== tab));
}
document.querySelectorAll("#settingsTabs button").forEach((b) => b.addEventListener("click", () => selectTab(b.dataset.tab)));

function spec(id = draft.provider) { return state.settings.providers.find((p) => p.id === id); }

function renderCoachEnabled() {
  $("coachFields").style.opacity = $("setLlmEnabled").checked ? "1" : ".45";
}
$("setLlmEnabled").addEventListener("change", renderCoachEnabled);

function renderProviders() {
  const grid = $("providerGrid");
  grid.innerHTML = state.settings.providers.map((p) =>
    `<button class="provider${p.id === draft.provider ? " active" : ""}" data-provider="${p.id}">${esc(p.label)}`
    + `<small>${p.local ? "Local · private · uses your computer" : "Cloud · needs an API key"}</small></button>`).join("");
  grid.querySelectorAll("[data-provider]").forEach((b) => b.addEventListener("click", () => {
    draft.models[draft.provider] = $("setModel").value.trim();
    draft.urls[draft.provider] = $("setBaseUrl").value.trim();
    draft.provider = b.dataset.provider;
    $("testResult").textContent = "";
    renderProviders();
  }));
  const p = spec();
  $("providerNote").innerHTML = esc(p.note || (p.local ? "Runs on this computer." : "Runs in the provider's cloud; moves and engine facts are sent to them."))
    + (p.key_url ? ` <a href="${esc(p.key_url)}" target="_blank" rel="noopener noreferrer">${p.needs_key ? "Get an API key ↗" : "Download ↗"}</a>` : "");
  $("setModel").value = draft.models[p.id] || "";
  $("setModel").placeholder = p.default_model || "model name";
  $("modelList").innerHTML = p.models.map((m) => `<option value="${esc(m)}">`).join("");
  $("baseUrlField").classList.toggle("hidden", !p.local);
  $("setBaseUrl").value = draft.urls[p.id] || "";
  $("setBaseUrl").placeholder = p.default_base_url;
  $("keyBlock").classList.toggle("hidden", !p.key_env);
  $("setKey").value = "";
  renderKeyStatus(p.key);
  if (p.id === "ollama") {
    api(`/api/settings/models?provider=ollama`).then((d) => {
      if (draft.provider !== "ollama" || !d.models?.length) return;
      $("modelList").innerHTML = [...new Set([...d.models, ...p.models])].map((m) => `<option value="${esc(m)}">`).join("");
      $("providerNote").innerHTML += `<br>Installed: ${d.models.map(esc).join(", ")}`;
    }).catch(() => {});
  }
}

function renderKeyStatus(key) {
  const p = spec();
  const where = { env: `from the ${p.key_env} environment variable`, keychain: `stored in ${state.settings.secure_store || "your system keychain"}`,
    session: "kept in memory for this session only" }[key.source];
  $("keyStatus").innerHTML = key.present
    ? `✓ Key ${esc(where)} <span class="mono">(${esc(key.hint)})</span>`
    : (p.needs_key ? "No key saved yet." : "Optional — only needed if your server requires one.")
      + (state.settings.secure_store ? ` Keys are saved in ${esc(state.settings.secure_store)}.`
        : ` No secure credential store was found, so a saved key lasts until Lucidfish stops — or set ${esc(p.key_env)} in a .env file.`);
  $("removeKey").classList.toggle("hidden", !(key.present && key.source !== "env"));
}

$("saveKey").addEventListener("click", async () => {
  const key = $("setKey").value.trim();
  if (!key) { toast("Paste a key first.", true); return; }
  try {
    const d = await api("/api/settings/key", { method: "POST", body: { provider: draft.provider, key } });
    spec().key = d.key; $("setKey").value = ""; renderKeyStatus(d.key); toast(d.message);
  } catch (e) { toast(e.message, true); }
});
$("removeKey").addEventListener("click", async () => {
  try {
    const d = await api(`/api/settings/key/${draft.provider}`, { method: "DELETE" });
    spec().key = d.key; renderKeyStatus(d.key); toast("Key removed.");
  } catch (e) { toast(e.message, true); }
});
$("testLlm").addEventListener("click", async () => {
  const out = $("testResult"), btn = $("testLlm");
  out.className = "small"; out.textContent = "Testing… (a local model may need a minute to load)";
  btn.disabled = true;
  try {
    const d = await api("/api/settings/test", { method: "POST", body: {
      provider: draft.provider, model: $("setModel").value.trim() || null,
      base_url: spec().local ? $("setBaseUrl").value.trim() : null, key: $("setKey").value.trim() || null } });
    out.className = "result-ok"; out.textContent = `✓ ${d.message}`;
  } catch (e) { out.className = "result-bad"; out.textContent = `✗ ${e.message}`; }
  btn.disabled = false;
});

function renderDetail() {
  document.querySelectorAll("#detailSeg button").forEach((b) => b.classList.toggle("active", b.dataset.detail === draft.detail));
  $("detailNote").textContent = DETAIL_NOTES[draft.detail] || "";
}
document.querySelectorAll("#detailSeg button").forEach((b) => b.addEventListener("click", () => { draft.detail = b.dataset.detail; renderDetail(); }));

function syncDepth() {
  const d = parseInt($("setDepth").value, 10);
  document.querySelectorAll("#depthSeg button").forEach((b) => b.classList.toggle("active", parseInt(b.dataset.depth, 10) === d));
}
$("depthSeg").addEventListener("click", (e) => {
  const b = e.target.closest("[data-depth]");
  if (b) { $("setDepth").value = b.dataset.depth; syncDepth(); }
});
$("setDepth").addEventListener("input", syncDepth);

$("clearCache").addEventListener("click", async () => {
  if (!confirm("Clear stored engine results and opening lookups? Analyses will be slower until they're rebuilt.")) return;
  try {
    const c = await api("/api/cache/clear", { method: "POST" });
    $("cacheInfo").textContent = `${c.engine_positions} engine positions · ${c.opening_positions} opening positions stored`;
    toast("Cache cleared.");
  } catch (e) { toast(e.message, true); }
});

$("saveSettings").addEventListener("click", async () => {
  const p = spec();
  const body = {
    provider: draft.provider, model: $("setModel").value.trim(), llm_enabled: $("setLlmEnabled").checked,
    factcheck: $("setFactcheck").checked, prefetch: $("setPrefetch").checked, detail: draft.detail,
    depth: $("setDepth").value, threads: $("setThreads").value, hash_mb: $("setHash").value,
    stockfish_path: $("setStockfish").value.trim(), concurrency: $("setConcurrency").value || null,
  };
  if (p.local) body.base_url = $("setBaseUrl").value.trim();
  try { await api("/api/settings", { method: "POST", body }); }
  catch (e) { $("settingsError").textContent = e.message; return; }
  closeModal("settingsModal");
  state.dismissed.clear();
  toast("Settings saved.");
  loadHealth(true);
});

/* ================================================================ keyboard + resize */

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && modalOpen()) { document.querySelectorAll(".modal-back").forEach((m) => closeModal(m.id)); return; }
  if (modalOpen() || state.page !== "game" || ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
  if (e.key === "Escape" && state.preview) { exitPreview(); return; }
  if (e.key === "ArrowLeft") { e.preventDefault(); state.preview ? stepPreview(-1) : goTo(state.cur - 1); }
  else if (e.key === "ArrowRight") { e.preventDefault(); state.preview ? stepPreview(1) : goTo(state.cur + 1); }
  else if (e.key === "Home") { e.preventDefault(); goTo(0); }
  else if (e.key === "End") { e.preventDefault(); goTo((state.game?.moves.length || 0) - 1); }
  else if (e.key.toLowerCase() === "f") flip();
  else if (e.key.toLowerCase() === "n") jumpKeyMoment(1);
  else if (e.key.toLowerCase() === "p") jumpKeyMoment(-1);
});

window.addEventListener("resize", debounce(() => {
  if (board && state.page === "game") { board.resize(); refreshBoard(); renderGraph(); }
  if (editorBoard && state.page === "editor") editorBoard.resize();
}, 150));

/* ================================================================ boot */

(async function boot() {
  loadHealth();
  try {
    const d = await loadProfiles();
    await route();
    if (!d.profiles.length) openProfile(null);
  } catch (e) { toast(e.message, true); }
  // Live queue progress (also picks up games restored from the last session).
  await pollQueue();
  if (queue.data?.restored && queue.data.paused) openQueue();
})();
