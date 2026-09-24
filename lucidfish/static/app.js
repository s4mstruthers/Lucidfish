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

function showPage(name) {
  state.page = name;
  document.querySelectorAll("[data-page-section]").forEach((s) => s.classList.toggle("hidden", s.id !== `page-${name}`));
  document.querySelectorAll("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.page === name));
  if (name === "dashboard") loadDashboard();
  if (name === "analyze" && !state.recent.length) loadRecentGames();
  if (name === "editor") ensureEditor();
  if (name === "game") { ensureBoard(); setTimeout(() => { board.resize(); refreshBoard(); renderGraph(); }, 0); }
  window.scrollTo({ top: 0 });
}
document.querySelectorAll("#nav button").forEach((b) => b.addEventListener("click", () => showPage(b.dataset.page)));
$("goAnalyze").addEventListener("click", () => showPage("analyze"));

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
  loadDashboard();
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
  $("summary").innerHTML = d.profile.summary
    ? md(d.profile.summary)
    : `<div class="empty small">${s.games ? "Analyse one more game and your coach will write a review of your play." : "Analyse a couple of your games and your coach will write a review of your play here."}</div>`;
  renderSavedGames();
  $("openings").innerHTML = (s.openings || []).length
    ? s.openings.map((o) => `<div class="item static"><span class="title">${esc(o.name)}</span>
        <span class="meta">${o.games} game${o.games === 1 ? "" : "s"} · ${o.w}W ${o.l}L ${o.d}D${o.acpl != null ? ` · ${o.acpl} ACPL` : ""}</span></div>`).join("")
    : `<div class="empty small">Appears after your first analysis.</div>`;
}

function resultFor(g) {
  if (!g.user_side || !["1-0", "0-1", "1/2-1/2"].includes(g.result)) return "";
  if (g.result === "1/2-1/2") return "D";
  return (g.result === "1-0") === (g.user_side === "white") ? "W" : "L";
}
function resultChip(r) {
  return r === "W" ? `<span class="chip win">WIN</span>` : r === "L" ? `<span class="chip loss">LOSS</span>` : r === "D" ? `<span class="chip">DRAW</span>` : "";
}

function renderSavedGames() {
  const box = $("savedGames");
  if (!state.savedGames.length) { box.innerHTML = `<div class="empty small">No analysed games yet.</div>`; return; }
  box.innerHTML = "";
  for (const g of state.savedGames) {
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
    g.storedId = analysed.get(await sha1(g.pgn.trim()));
    const el = document.createElement("div");
    el.className = "item";
    el.innerHTML = `<input type="checkbox" aria-label="Select game">
      <span class="title">${esc(g.white)} vs ${esc(g.black)}</span>${resultChip(g.result)}
      ${g.storedId ? `<span class="chip accent">ANALYSED</span>` : ""}
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

function updateSelection() {
  const n = state.selected.size;
  $("analyzeSelected").disabled = !n;
  $("analyzeSelected").textContent = n ? `Analyze selected (${n})` : "Analyze selected";
}

/* ================================================================ batch import */

async function startImport(games) {
  if (!state.profile) { toast("Create a profile first — batch analyses are saved to it.", true); openProfile(null); return; }
  const payload = games.map((g) => ({ pgn: g.pgn, side: g.side, elo: g.rating }));
  try { await api("/api/profile/import", { method: "POST", body: { games: payload, force: $("redo").checked } }); }
  catch (e) { toast(e.message, true); return; }
  pollImport();
}
$("analyzeSelected").addEventListener("click", () => startImport([...state.selected].sort((a, b) => a - b).map((i) => state.recent[i])));
$("analyzeBatch").addEventListener("click", () => {
  if (!state.recent.length) { toast("No games loaded to analyse.", true); return; }
  startImport(state.recent.slice(0, parseInt($("batchCount").value, 10)));
});

async function pollImport() {
  let j;
  try { j = await api("/api/profile/import_status"); } catch { return; }
  const box = $("importStatus");
  const extra = `${j.skipped ? ` · ${j.skipped} already analysed` : ""}${j.errors ? ` · ${j.errors} failed` : ""}`;
  if (j.status === "running") {
    box.innerHTML = `<span class="dot busy"></span> ${esc(j.current)} · ${j.done}/${j.total} done${esc(extra)}
      <button class="ghost sm" id="stopImport">Stop</button>`;
    $("stopImport").addEventListener("click", () => api("/api/profile/import/stop", { method: "POST" }));
    setTimeout(pollImport, 2500);
  } else if (j.status === "done" || j.status === "stopped") {
    box.textContent = `${j.status === "done" ? "✓ Batch complete" : "Batch stopped"}: ${j.done - j.skipped - j.errors} analysed${extra}`
      + (j.last_error ? ` — last error: ${j.last_error}` : "");
    await loadDashboard();
    if (state.page === "analyze") loadRecentGames();
  }
}

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
  clearTimeout(pollTimer);
  state.game = { mode: "game", pgn, jobId: d.job_id, status: "queued", headers: {}, side, moves: [], review: "",
    opening: "", accuracy: {}, warnings: [], total: 0, started: Date.now(), coach: "" };
  resetGameView();
  $("navGame").classList.remove("hidden");
  showPage("game");
  $("progressCard").classList.remove("hidden");
  // Engine-only mode has nothing for a "Coach" bar to show.
  $("barCoach").closest(".bar-row").classList.toggle("hidden", state.health?.coach?.enabled === false);
  $("stopBtn").disabled = false; $("stopBtn").textContent = "■ Stop";
  poll();
}

async function poll() {
  const g = state.game;
  if (!g || !g.jobId) return;
  let j;
  try { j = await api(`/api/job/${g.jobId}?since=${g.moves.length}`); }
  catch (e) { toast(e.message, true); $("progressCard").classList.add("hidden"); return; }
  if (state.game !== g) return;   // a different game was opened meanwhile
  Object.assign(g, { status: j.status, headers: j.headers, side: j.side, total: j.total, warnings: j.warnings });
  if (j.moves.length) {
    g.moves.push(...j.moves);
    if (state.cur < 0) goTo(0); else renderMoveList();
    renderGraph();
  }
  renderHeader();
  updateProgress(j);
  if (j.status === "queued" || j.status === "running") {
    pollTimer = setTimeout(poll, j.status === "queued" ? 1500 : 700);
    return;
  }
  Object.assign(g, { review: j.review, opening: j.opening, accuracy: j.accuracy, coach: j.coach, jobId: null });
  $("progressCard").classList.add("hidden");
  renderHeader(); renderReview(); renderGraph();
  if (state.cur >= 0) goTo(state.cur);
  if (j.status === "error") toast(j.error || "The analysis failed.", true);
  else if (j.status === "stopped") toast("Analysis stopped — showing the moves completed so far.");
  else toast(j.game_id ? "Analysis complete and saved to your profile." : "Analysis complete.");
  loadHealth(); loadDashboard();
}

function updateProgress(j) {
  const total = Math.max(1, j.total);
  $("progressLabel").textContent = j.status === "queued" ? "Waiting for the current analysis to finish…" : j.label;
  $("barEngine").style.width = `${(100 * j.engine_done) / total}%`;
  $("barCoach").style.width = `${(100 * j.done) / total}%`;
  $("barEngineTxt").textContent = `${j.engine_done}/${j.total}`;
  $("barCoachTxt").textContent = `${j.done}/${j.total}`;
  const elapsed = (Date.now() - state.game.started) / 1000;
  const done = j.done;
  if (done >= 3 && done < j.total) {
    const eta = Math.round((elapsed / done) * (j.total - done));
    $("progressEta").textContent = `about ${eta >= 90 ? Math.ceil(eta / 60) + " min" : eta + " s"} left`;
  } else $("progressEta").textContent = "";
}

$("stopBtn").addEventListener("click", async () => {
  const g = state.game;
  if (!g?.jobId) return;
  $("stopBtn").disabled = true; $("stopBtn").textContent = "Stopping…";
  try { await api(`/api/job/${g.jobId}/stop`, { method: "POST" }); } catch (e) { toast(e.message, true); }
});

async function openStoredGame(id) {
  let d;
  try { d = await api(`/api/profile/game/${id}`); } catch (e) { toast(e.message, true); return; }
  clearTimeout(pollTimer);
  state.game = { mode: "game", pgn: d.pgn, headers: d.headers, side: d.side, moves: d.moves, review: d.review,
    opening: d.opening, accuracy: d.accuracy || {}, warnings: [], total: d.moves.length, status: "done" };
  resetGameView();
  $("navGame").classList.remove("hidden");
  showPage("game");
  renderHeader(); renderReview();
  goTo(d.moves.length ? 0 : -1);
}

function resetGameView() {
  state.cur = -1; state.preview = null; state.gameChat.length = 0;
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
    $("exportRow").querySelectorAll("#exportPgn,#exportMd").forEach((b) => b.classList.remove("hidden"));
  }
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
  board = Chessboard("board", { position: "start", pieceTheme: PIECES, showNotation: true, moveSpeed: 120, snapSpeed: 60 });
}

function orientation() { return board ? board.orientation() : "white"; }

function refreshBoard() {
  if (!board || !state.game) return;
  if (state.preview) { stepPreview(0); return; }
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

function flip() {
  if (!board) return;
  board.flip();
  refreshBoard();
  if (state.game?.mode === "game" && state.cur >= 0) setEvalBar(winOf(state.game.moves[state.cur]), evalWords(state.game.moves[state.cur].eval));
}

/* ================================================================ moves */

function goTo(i, rerender = true) {
  const g = state.game;
  if (!g || g.mode !== "game" || !g.moves.length) return;
  ensureBoard();
  state.preview = null; $("previewBar").classList.add("hidden");
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
  const g = state.game, box = $("moveList");
  if (!g || g.mode !== "game") return;
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
    + (opponent ? `<span class="chip">opponent</span>` : "") + `</div>`;
  if (m.tags && m.tags.length) html += `<div class="row" style="margin-bottom:10px">${m.tags.map((t) => `<span class="chip tag">${esc(t)}</span>`).join("")}</div>`;
  if (m.expl) {
    html += (opponent ? `<span class="opp-label">What your opponent is up to</span>` : "") + `<div class="md">${md(m.expl)}</div>`;
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
  if (el) startPreview(el.dataset.prev);
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
  if (state.game?.mode === "position") refreshBoard(); else goTo(state.cur, false);
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
  key: "Notes only for your mistakes and the game's critical moments. Fastest — good for slower computers.",
  standard: "A short note on each of your moves, full explanations of mistakes, and warnings about your opponent's threats. Recommended.",
  full: "Detailed notes on every move by both players. Slowest — best with a fast GPU or a cloud model.",
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
    factcheck: $("setFactcheck").checked, detail: draft.detail,
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
    await loadDashboard();
    if (!d.profiles.length) openProfile(null);
  } catch (e) { toast(e.message, true); }
  // Resume the progress display of a batch analysis started before a page reload.
  api("/api/profile/import_status").then((j) => { if (j.status === "running") pollImport(); }).catch(() => {});
})();
