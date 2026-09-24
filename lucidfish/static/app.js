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
/* A shared copy (see share.py) carries its data inside the page and has no server behind it. */
const EXPORT = window.LUCIDFISH_EXPORT || null;
const PIECES = EXPORT ? (piece) => EXPORT.pieces[piece] : "/pieces/{piece}.svg";
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

/* An unknown API route means this page is newer than the server process running it:
 * the files were updated (e.g. `git pull`) while Lucidfish was still running. */
const RESTART_MESSAGE = "Lucidfish needs a restart to finish updating: this page is new, but the program serving it "
  + "is still the old version. Close Lucidfish (Ctrl+C in its window) and start it again, then reload this page.";

/** fetch() wrapper: JSON in/out, the CSRF header, and readable errors. */
async function api(path, { method = "GET", body, raw = false } = {}) {
  if (EXPORT) return exportApi(path, { method, body });
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
      else if (res.status === 404 && data.detail === "Not Found") msg = RESTART_MESSAGE;
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

const LOCALE = "en-GB";   // Lucidfish is written in British English: "24 Sept 2026", 24-hour clock

function fmtDate(d) {
  try { return d.toLocaleDateString(LOCALE, { day: "numeric", month: "short", year: "numeric" }); }
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
  return ts ? new Date(ts * 1000).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" }) : "";
}
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : "");
const DETAIL_NAMES = { key: "Key moments", standard: "Commentary", full: "Every move" };

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
  revealed: new Set(),   // your mistakes whose answer you chose to see (practice-first mode)
  draw: { mode: false, colour: "green" },   // board drawing: pen mode for touch / left mouse, colour
  view: { bestArrow: true, highlight: true, evalBar: true, graph: true },   // what the board shows
};
try { Object.assign(state.view, JSON.parse(localStorage.getItem("lucidfish-view") || "{}")); } catch { /* defaults */ }

/** Practice first: hide the answer to your own mistakes until you've tried (Settings → Analysis). */
function hideAnswers() {
  try { return localStorage.getItem("lucidfish-hide-answers") !== "0"; } catch { return true; }
}
function isSpoiler(i) {
  const g = state.game, m = g?.moves?.[i];
  if (!m || g.mode !== "game" || !ERRORS.includes(m.cls) || !hideAnswers() || state.revealed.has(i)) return false;
  return !g.side || m.side.toLowerCase() === g.side;
}

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
  const section = name === "game" && train.session ? "train" : name;   // a puzzle opens in its game
  document.querySelectorAll("#nav button").forEach((b) => b.classList.toggle("active",
    b.dataset.page === section || (b.dataset.also || "").split(" ").includes(section)));
  document.querySelectorAll(".page-tabs [data-page]").forEach((b) => b.classList.toggle("active", b.dataset.page === name));
  if (name === "games" || name === "analyse") renderResume();
  if (name === "dashboard") loadDashboard();
  if (name === "games") loadDashboard().then(renderGames);
  if (name === "analyse" && (!state.recent.length || state.recentTc !== state.tc)) loadRecentGames();
  if (name === "analyse") renderTcBars();
  if (name === "editor") ensureEditor();
  if (name === "train") showTrainPage();
  if (name !== "game" && state.explore) Engine.stop();   // don't keep analysing a board nobody sees
  if (name === "game") {
    ensureBoard();
    setTimeout(() => { board.resize(); refreshBoard(); renderGraph(); if (state.explore) exploreUpdate(false); }, 0);
  }
  if (!fromRoute) syncRoute();
  window.scrollTo({ top: 0 });
}
document.querySelectorAll("#nav button, .page-tabs [data-page]").forEach((b) =>
  b.addEventListener("click", () => showPage(b.dataset.page)));
$("backBtn").addEventListener("click", () => showPage(state.game?.mode === "position" ? "editor" : "games"));

/** "Continue reviewing …" on the Games pages, for the game you were looking at. */
function renderResume() {
  const g = state.game;
  const title = g?.mode === "position" ? "your board-editor position"
    : g ? `${g.headers?.White || "White"} vs ${g.headers?.Black || "Black"}` : "";
  document.querySelectorAll("[data-resume]").forEach((el) => {
    el.innerHTML = g ? `<button class="resume" data-resume-open>▶ Continue reviewing <b>${esc(title)}</b>`
      + `${g.jobId ? " (still analysing)" : ""}</button>` : "";
  });
}
document.addEventListener("click", (e) => { if (e.target.closest("[data-resume-open]")) showPage("game"); });
$("goAnalyse").addEventListener("click", () => showPage("analyse"));

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
  try { history[replace ? "replaceState" : "pushState"](null, "", r); }
  catch { location.hash = r; }   // some browsers restrict history on pages opened from a file
}
async function route() {
  let [, page, id] = location.hash.match(/^#\/(\w+)(?:\/([\w-]+))?$/) || [];
  if (EXPORT && (page === "analyse" || page === "editor" || page === "job")) page = "games";   // need the server
  if (page === "game" && id) {
    if (state.game?.gameId === Number(id)) showPage("game", true);
    else await openStoredGame(Number(id), true);
  } else if (page === "job" && id) {
    if (state.game?.jobId === id) showPage("game", true); else openJob(id, "", true);
  } else if (page === "game" && state.game) {
    showPage("game", true);
  } else {
    showPage(["dashboard", "games", "train", "analyse", "editor"].includes(page) ? page : "dashboard", true);
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
  const coachText = coach.enabled
    ? `${coach.model}${coach.local ? " (local)" : ""}${coach.expert ? ` + ${coach.expert}` : ""}` : "engine only";
  text.textContent = engine.ok ? `${engine.name} · ${coachText}` : "Stockfish not found";
  dot.className = "dot " + (h.busy ? "busy" : !engine.ok ? "bad" : coach.ok ? "ok" : "warn");
  $("aboutVersion").textContent = h.version || "";
  renderBanners();
}

function renderBanners() {
  const h = state.health, out = [];
  if (h?.stale) {
    out.push({ id: "stale", cls: "bad", title: "Lucidfish was updated while it was running",
      body: "Restart it to use the new version: close Lucidfish (Ctrl+C in its window) and start it again, then reload this page.",
      actions: [] });
  }
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

const LEVEL_NAMES = { beginner: "Beginner", casual: "Casual", club: "Club player", advanced: "Advanced" };

/** Initials on a colour derived from the name: a recognisable avatar with no image upload. */
function initials(name) {
  const parts = String(name || "?").trim().split(/\s+/).filter(Boolean);
  return ((parts[0]?.[0] || "?") + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase();
}
function avatarStyle(name) {
  let h = 0;
  for (const ch of String(name || "")) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return `background: hsl(${h}, 55%, 46%)`;
}
function avatar(name, cls = "") {
  return `<span class="avatar ${cls}" style="${avatarStyle(name)}" aria-hidden="true">${esc(initials(name))}</span>`;
}
const RATING_SITES = { chesscom: "chess.com", lichess: "Lichess", other: "Other" };
const RATING_CLASSES = ["bullet", "blitz", "rapid", "classical", "daily"];

/** "chess.com rapid 1450 · Lichess blitz 1720": each site's most recently updated rating. */
function ratingsText(p) {
  return Object.entries(RATING_SITES).map(([site, name]) => {
    const entries = Object.entries(p.ratings?.[site] || {});
    if (!entries.length) return "";
    const [cls, e] = entries.sort((a, b) => (b[1].date || "").localeCompare(a[1].date || ""))[0];
    return `${site === "other" ? "" : `${name} `}${cls} ${e.rating}`;
  }).filter(Boolean).join(" · ");
}

let activeProfileId;   // undefined until the first load
async function loadProfiles() {
  const d = await api("/api/profiles");
  state.profiles = d.profiles;
  state.profile = d.profiles.find((p) => p.id === d.active) || null;
  const id = state.profile?.id ?? null;
  if (activeProfileId !== undefined && id !== activeProfileId) leaveProfile();
  activeProfileId = id;
  renderAccount();
  return d;
}

/** The active profile changed (switched, created or deleted): what's on screen belonged to the previous one. */
function leaveProfile() {
  exitExplore(false);
  clearTimeout(pollTimer);
  state.game = null;
  resetGameView();
  state.recent = []; state.selected.clear();
  state.reviewChat.length = 0; $("reviewChatLog").innerHTML = "";
  train.items = null; train.session = null; train.summary = null;
  renderTrainBar();
  renderResume();
  if (state.page === "game") showPage("games");
  else if (state.page === "train") showTrainPage();
}

function renderAccount() {
  const p = state.profile;
  $("accountAvatar").textContent = p ? initials(p.name) : "+";
  $("accountAvatar").setAttribute("style", p ? avatarStyle(p.name) : "");
  $("accountName").textContent = p ? p.name : "Create profile";
  if (EXPORT) {
    $("accountMenu").innerHTML = `<div class="am-current">${avatar(p.name, "lg")}<div class="grow"><b>${esc(p.name)}</b>`
      + `<div class="small">${[LEVEL_NAMES[p.level], ratingsText(p)].filter(Boolean).map(esc).join(" · ")}</div></div></div>`
      + `<p class="small" style="margin:8px 10px">A shared copy made with Lucidfish on ${esc(sharedDate())}. New games appear `
      + `when the person who shared it analyses them and sends or syncs a new copy.</p>`;
    return;
  }
  const others = state.profiles.filter((x) => x.id !== p?.id);
  const accounts = p ? [p.chesscom_user && `chess.com · ${esc(p.chesscom_user)}`, p.lichess_user && `Lichess · ${esc(p.lichess_user)}`]
    .filter(Boolean).join("<br>") : "";
  $("accountMenu").innerHTML = (p
    ? `<div class="am-current">${avatar(p.name, "lg")}<div class="grow"><b>${esc(p.name)}</b>`
      + `<div class="small">${[LEVEL_NAMES[p.level], ratingsText(p)].filter(Boolean).map(esc).join(" · ") || "No level or ratings yet"}</div>`
      + (accounts ? `<div class="small">${accounts}</div>` : "")
      + `</div></div><button class="am-item" data-am="edit">✎ Edit profile</button>`
      + `<button class="am-item" data-am="share">⇪ Share this profile…</button>`
    : `<div class="am-current"><div class="grow small">Create a profile so the coach can remember your games.</div></div>`)
    + (others.length ? `<div class="am-label">Switch profile</div>` + others.map((o) =>
      `<button class="am-item" data-am="switch" data-id="${o.id}">${avatar(o.name, "sm")}<span class="grow">${esc(o.name)}</span>`
      + `<span class="small">${o.games} game${o.games === 1 ? "" : "s"}</span></button>`).join("") : "")
    + `<button class="am-item" data-am="new">＋ Add a profile</button>`;
}

function toggleAccountMenu(open) {
  const menu = $("accountMenu"), show = open ?? menu.classList.contains("hidden");
  menu.classList.toggle("hidden", !show);
  $("accountBtn").setAttribute("aria-expanded", show);
}
$("accountBtn").addEventListener("click", (e) => {
  e.stopPropagation();
  if (!state.profile && !state.profiles.length) { openProfile(null); return; }
  toggleAccountMenu();
});
document.addEventListener("click", (e) => { if (!e.target.closest("#account")) toggleAccountMenu(false); });
$("accountMenu").addEventListener("click", async (e) => {
  const item = e.target.closest("[data-am]");
  if (!item) return;
  toggleAccountMenu(false);
  if (item.dataset.am === "edit") openProfile(state.profile);
  else if (item.dataset.am === "share") openShare();
  else if (item.dataset.am === "new") openProfile(null);
  else if (item.dataset.am === "switch") switchProfile(Number(item.dataset.id));
});

async function switchProfile(id) {
  try { await api(`/api/profiles/${id}/activate`, { method: "POST" }); }
  catch (err) { toast(err.message, true); return; }
  await loadProfiles();   // clears the previous profile's game, puzzles and chat
  loadDashboard().then(() => { if (state.page === "games") renderGames(); });
  if (state.page === "analyse") loadRecentGames();
  toast(`Switched to ${state.profile?.name}.`);
}

let editingProfile = null;
let pfLevel = "";
let pfLookup = {};   // ratings looked up in the open form, saved with it: {site: {class: rating}}

/** The profile's ratings per site and time control, with what "Look up" just found. */
function renderPfRatings() {
  const today = new Date().toISOString().slice(0, 10);
  const rows = {};
  for (const site of Object.keys(RATING_SITES)) {
    rows[site] = { ...(editingProfile?.ratings?.[site] || {}) };
    for (const [cls, rating] of Object.entries(pfLookup[site] || {})) rows[site][cls] = { rating, date: today, source: "new" };
  }
  const typed = { chesscom: $("pfChesscom").value.trim(), lichess: $("pfLichess").value.trim() };
  const sites = Object.keys(RATING_SITES).filter((s) => Object.keys(rows[s]).length || typed[s]);
  let classes = RATING_CLASSES.filter((c) => sites.some((s) => rows[s][c]));
  if (!classes.length) classes = ["bullet", "blitz", "rapid"];
  const when = (d) => (d ? fmtDate(new Date(`${d}T12:00:00`)) : "");
  const tip = (e, site) => (e.source === "new" ? "Just looked up (saved with the profile)"
    : e.source === "game" ? `From your analysed game of ${when(e.date)}`
      : e.source === "lookup" ? `Looked up on ${RATING_SITES[site]} on ${when(e.date)}` : "Typed in earlier");
  $("pfRatings").innerHTML = sites.length
    ? `<table class="ratings-table"><thead><tr><th></th>${classes.map((c) => `<th>${cap(c)}</th>`).join("")}</tr></thead><tbody>`
      + sites.map((s) => `<tr><th>${RATING_SITES[s]}</th>${classes.map((c) => {
        const e = rows[s][c];
        return e ? `<td class="${e.source === "new" ? "fresh" : ""}" title="${esc(tip(e, s))}">${e.rating}</td>` : `<td class="none">—</td>`;
      }).join("")}</tr>`).join("") + `</tbody></table>`
    : `<p class="small" style="margin:0">No ratings yet. They appear after your first analysed game, or press <b>Look up</b>.</p>`;
}

function openProfile(existing) {
  editingProfile = existing;
  $("profileTitle").textContent = existing ? "Edit profile" : state.profiles.length ? "Add a profile" : "Welcome to Lucidfish";
  $("pfIntro").classList.toggle("hidden", !!existing);
  $("pfName").value = existing?.name || "";
  pfLevel = existing?.level || "";
  $("pfChesscom").value = existing?.chesscom_user || "";
  $("pfLichess").value = existing?.lichess_user || "";
  pfLookup = {};
  renderPfRatings();
  $("pfChesscomStatus").textContent = ""; $("pfLichessStatus").textContent = "";
  $("pfDanger").classList.toggle("hidden", !existing);
  $("pfError").textContent = "";
  renderProfileForm();
  openModal("profileModal");
  $("pfName").focus();
}

function renderProfileForm() {
  const name = $("pfName").value.trim();
  $("pfAvatar").textContent = name ? initials(name) : "+";
  $("pfAvatar").setAttribute("style", name ? avatarStyle(name) : "");
  document.querySelectorAll("#pfLevel [data-level]").forEach((b) => {
    b.classList.toggle("active", b.dataset.level === pfLevel);
    b.setAttribute("aria-checked", b.dataset.level === pfLevel);
  });
}
$("pfName").addEventListener("input", renderProfileForm);
["pfChesscom", "pfLichess"].forEach((id) => $(id).addEventListener("input", renderPfRatings));
$("pfLevel").addEventListener("click", (e) => {
  const b = e.target.closest("[data-level]");
  if (!b) return;
  pfLevel = pfLevel === b.dataset.level ? "" : b.dataset.level;   // click again to clear
  renderProfileForm();
});

/** Fill the ratings from the site's public profile (the browser talks to chess.com / Lichess directly). */
async function lookupRatings(site) {
  const user = $(site === "chesscom" ? "pfChesscom" : "pfLichess").value.trim();
  const out = $(site === "chesscom" ? "pfChesscomStatus" : "pfLichessStatus");
  if (!user) { out.textContent = "Enter a username first."; return; }
  out.className = "small lookup"; out.textContent = "Looking up…";
  try {
    let ratings;
    if (site === "chesscom") {
      const r = await fetch(`https://api.chess.com/pub/player/${encodeURIComponent(user.toLowerCase())}/stats`);
      if (r.status === 404) throw new Error(`No chess.com player called "${user}".`);
      if (!r.ok) throw new Error(`chess.com answered HTTP ${r.status}.`);
      const d = await r.json();
      ratings = { bullet: d.chess_bullet?.last?.rating, blitz: d.chess_blitz?.last?.rating,
        rapid: d.chess_rapid?.last?.rating, daily: d.chess_daily?.last?.rating };
    } else {
      const r = await fetch(`https://lichess.org/api/user/${encodeURIComponent(user)}`);
      if (r.status === 404) throw new Error(`No Lichess player called "${user}".`);
      if (!r.ok) throw new Error(`Lichess answered HTTP ${r.status}.`);
      const d = await r.json();
      const perf = (k) => (d.perfs?.[k] && !d.perfs[k].prov && d.perfs[k].games ? d.perfs[k].rating : null);   // not provisional
      ratings = { bullet: perf("bullet"), blitz: perf("blitz"), rapid: perf("rapid"), classical: perf("classical"),
        daily: perf("correspondence") };
    }
    const found = Object.entries(ratings).filter(([, v]) => Number.isInteger(v));
    pfLookup[site] = Object.fromEntries(found);
    renderPfRatings();
    out.className = "small lookup ok";
    out.textContent = found.length ? `✓ Found: ${found.map(([k, v]) => `${k} ${v}`).join(", ")}. Saved with the profile.`
      : "✓ Found the player, but they have no established ratings there yet.";
  } catch (e) {
    out.className = "small lookup bad";
    out.textContent = e.message.startsWith("No ") || e.message.includes("HTTP") ? e.message
      : "Couldn't reach the site — check your connection and try again.";
  }
}
document.querySelectorAll("[data-lookup]").forEach((b) => b.addEventListener("click", () => lookupRatings(b.dataset.lookup)));

$("pfSave").addEventListener("click", async () => {
  const body = {
    name: $("pfName").value.trim(), level: pfLevel,
    chesscom_user: $("pfChesscom").value.trim(), lichess_user: $("pfLichess").value.trim(), lookup: pfLookup,
  };
  if (!body.name) { $("pfError").textContent = "Please enter a name."; $("pfName").focus(); return; }
  try {
    if (editingProfile) await api(`/api/profiles/${editingProfile.id}`, { method: "POST", body });
    else await api("/api/profiles", { method: "POST", body });   // a new profile becomes the active one
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

/* ================================================================ share this profile */

async function openShare() {
  let d;
  try { d = await api("/api/share"); } catch (e) { toast(e.message, true); return; }
  $("shareTitle").textContent = `Share ${state.profile?.name || "this profile"}'s games`;
  $("shareFolder").value = d.folder || "";
  $("shareAuto").checked = d.folder ? d.auto : true;
  $("shareEngine").checked = d.engine !== false;
  $("shareSuggestions").innerHTML = d.suggestions.length
    ? `<span class="small">Found on this computer:</span>` + d.suggestions.map((s) =>
      `<button class="secondary sm" data-folder="${esc(s.path)}">${esc(s.label)}</button>`).join("") : "";
  renderShareStatus(d);
  openModal("shareModal");
}

let shareCfg = null;
function renderShareStatus(d) {
  shareCfg = d;
  const out = $("shareStatus");
  const when = d.last_synced ? new Date(d.last_synced * 1000).toLocaleString(LOCALE,
    { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
  out.className = "small" + (d.last_error ? " bad" : d.last_synced ? " ok" : "");
  out.textContent = d.last_error ? `✗ ${d.last_error}`
    : d.last_synced ? `✓ Written ${when}: ${d.file}${d.auto ? " — updated automatically after every analysis." : ""}`
      : d.folder ? "Not written yet." : "Not syncing to a folder.";
  $("shareSyncNow").disabled = !d.folder;
  $("shareStop").classList.toggle("hidden", !d.folder);
}

$("shareSuggestions").addEventListener("click", (e) => {
  const b = e.target.closest("[data-folder]");
  if (b) $("shareFolder").value = `${b.dataset.folder}/Lucidfish`;
});
$("shareSave").addEventListener("click", async () => {
  try {
    const d = await api("/api/share", { method: "POST", body: {
      folder: $("shareFolder").value.trim(), auto: $("shareAuto").checked, engine: $("shareEngine").checked } });
    renderShareStatus(d);
    if (d.last_synced && !d.last_error) toast("Saved. The page is in the folder.");
  } catch (e) { $("shareStatus").className = "small bad"; $("shareStatus").textContent = `✗ ${e.message}`; }
});
$("shareEngine").addEventListener("change", async (e) => {
  if (!shareCfg?.folder) return;   // only the downloads use it
  try {
    renderShareStatus(await api("/api/share", { method: "POST", body: {
      folder: shareCfg.folder, auto: shareCfg.auto, engine: e.target.checked } }));
    toast(e.target.checked ? "The copy in the folder now includes the engine." : "The copy in the folder no longer includes the engine.");
  } catch (err) { toast(err.message, true); }
});
$("shareSyncNow").addEventListener("click", async () => {
  try { renderShareStatus(await api("/api/share/sync", { method: "POST" })); } catch (e) { toast(e.message, true); }
});
$("shareStop").addEventListener("click", async () => {
  try {
    renderShareStatus(await api("/api/share", { method: "POST", body: { folder: "", auto: false, engine: $("shareEngine").checked } }));
    $("shareFolder").value = "";
    toast("Stopped syncing. The last copy stays in the folder.");
  } catch (e) { toast(e.message, true); }
});
$("shareDownload").addEventListener("click", async () => {
  try {
    const res = await api(`/api/share/download?engine=${$("shareEngine").checked}`, { raw: true });
    const blob = await res.blob();
    const cd = res.headers.get("content-disposition") || "";
    const name = decodeURIComponent(cd.match(/filename\*=UTF-8''(.+)$/)?.[1] || "Lucidfish.html");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  } catch (e) { toast(e.message, true); }
});

/* ================================================================ engine in the browser */

/* Stockfish compiled to WebAssembly, running in a Web Worker: Explore mode uses it in the app and in shared
 * copies, and a shared copy (which has no server) also uses it to judge practice moves. In the app the worker
 * loads /static/vendor/stockfish-…; a shared copy carries the engine as text blocks, because a page opened
 * from disk can't load a worker or a .wasm file. */
const Engine = (() => {
  let worker = null, booting = null, job = null, error = "";
  const waiting = [];
  const supported = typeof Worker === "function" && typeof WebAssembly === "object";

  function available() {
    if (!supported || error) return false;
    return EXPORT ? !!document.getElementById("lfEngineWasm") : true;
  }

  function makeWorker() {
    if (!EXPORT) return new Worker("/static/vendor/stockfish-18-lite-single.js");
    const bin = atob(document.getElementById("lfEngineWasm").textContent.trim());
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    // The engine downloads its .wasm with fetch(): answer that with the embedded bytes, then start it.
    const prelude = `let lfWasm = null; const lfFetch = self.fetch.bind(self);
self.fetch = (url, opts) => String(url).includes("lucidfish-engine.wasm")
  ? Promise.resolve(new Response(lfWasm, { headers: { "Content-Type": "application/wasm" } })) : lfFetch(url, opts);
self.onmessage = (e) => { lfWasm = e.data; self.onmessage = null; lfStart(); };
function lfStart() {
`;
    const src = prelude + document.getElementById("lfEngineJs").textContent + "\n}";
    const url = URL.createObjectURL(new Blob([src], { type: "text/javascript" }));
    // The part after # names the .wasm file for the engine's loader (",worker" there would mean a helper thread).
    const w = new Worker(`${url}#lucidfish-engine.wasm`);
    w.postMessage(bytes, [bytes.buffer]);
    return w;
  }

  function boot() {
    if (booting) return booting;
    booting = new Promise((resolve, reject) => {
      const fail = (msg) => { error = msg; clearTimeout(timer); worker?.terminate(); worker = null; reject(new Error(msg)); };
      const timer = setTimeout(() => fail("The engine didn't start in this browser."), 30000);
      let ready = false;
      try { worker = makeWorker(); } catch (e) { fail(`The engine can't run here (${e.message}).`); return; }
      worker.onmessage = (e) => {
        const line = String(e.data);
        if (!ready) {
          if (line === "readyok") { ready = true; clearTimeout(timer); resolve(); }
          return;
        }
        onLine(line);
      };
      worker.onerror = (e) => {
        e.preventDefault?.();
        if (!ready) { fail(`The engine can't run here (${e.message || "unknown error"}).`); return; }
        error = "The engine stopped working. Reload the page to restart it.";
        if (job) { job.reject(new Error(error)); job = null; }
        waiting.splice(0).forEach((j) => j.reject(new Error(error)));
      };
      worker.postMessage("uci");
      worker.postMessage("isready");
    });
    return booting;
  }

  function parseInfo(line) {
    const t = line.split(" ");
    if (t.includes("lowerbound") || t.includes("upperbound")) return null;
    const at = (k) => t.indexOf(k);
    const si = at("score"), pi = at("pv");
    if (si < 0 || pi < 0) return null;
    return {
      depth: Number(t[at("depth") + 1]) || 0, multipv: Number(at("multipv") >= 0 ? t[at("multipv") + 1] : 1) || 1,
      cp: t[si + 1] === "cp" ? Number(t[si + 2]) : null, mate: t[si + 1] === "mate" ? Number(t[si + 2]) : null,
      pv: t.slice(pi + 1),
    };
  }

  function onLine(line) {
    if (!job) return;
    if (line.startsWith("info ") && line.includes(" pv ")) {
      const info = parseInfo(line);
      if (!info) return;
      job.lines[info.multipv - 1] = info;
      job.onInfo?.(job.lines.filter(Boolean));
    } else if (line.startsWith("bestmove")) {
      const j = job;
      job = null;
      j.resolve({ lines: j.lines.filter(Boolean), bestmove: line.split(" ")[1] });
      next();
    }
  }

  function next() {
    while (!job && waiting.length) {
      const j = waiting.shift();
      if (j.stale?.()) { j.resolve(null); continue; }
      job = j;
      worker.postMessage(`setoption name MultiPV value ${j.multipv}`);
      worker.postMessage(`position fen ${j.fen}`);
      worker.postMessage(`go depth ${j.depth}${j.movetime ? ` movetime ${j.movetime}` : ""}`
        + `${j.searchmoves ? ` searchmoves ${j.searchmoves.join(" ")}` : ""}`);
    }
  }

  /** Search a position. Resolves {lines: [{depth, multipv, cp, mate, pv}], bestmove} (scores for the side to
   * move), or null if `stale()` said it was no longer wanted before it started. `interrupt` stops the search
   * in progress first; `onInfo(lines)` sees every improvement. */
  async function search(fen, opts = {}) {
    await boot();
    return new Promise((resolve, reject) => {
      waiting.push({ multipv: 1, depth: 16, ...opts, fen, lines: [], resolve, reject });
      if (!job) next();
      else if (opts.interrupt) worker.postMessage("stop");
    });
  }

  function stop() { if (job) worker.postMessage("stop"); }

  return { available, boot, search, stop, error: () => error };
})();

/* ---------------------------------------------------------------- chess helpers (chess.js) */

const hasChess = typeof window.Chess === "function";
const sideToMove = (fen) => (fen.split(" ")[1] === "b" ? "b" : "w");

/** Engine score (side to move) as a White-perspective eval string: '+0.35', '#3', '#-2'. */
function engineEval(line, fen) {
  const sign = sideToMove(fen) === "w" ? 1 : -1;
  if (!line) return "";
  if (line.mate != null) return `#${sign * line.mate}`;
  const v = (sign * line.cp) / 100;
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}`;
}
/** UCI moves as playable steps [{san, from, to, fen}] (the same shape the analysis stores). */
function uciSteps(fen, ucis, max = 14) {
  const c = new Chess(fen), steps = [];
  for (const u of ucis.slice(0, max)) {
    let mv;
    try { mv = c.move({ from: u.slice(0, 2), to: u.slice(2, 4), promotion: u[4] }); } catch { break; }
    steps.push({ san: mv.san, from: mv.from, to: mv.to, fen: c.fen() });
  }
  return steps;
}
/** "14…Nf6 15.e5 Nd5" for steps starting from `fen`. */
function stepsText(fen, steps) {
  const f = fen.split(" ");
  let n = Number(f[5]) || 1, white = f[1] !== "b";
  return steps.map((s, k) => {
    const label = white ? `${n}.` : k === 0 ? `${n}…` : "";
    if (!white) n += 1;
    white = !white;
    return label + s.san;
  }).join(" ");
}

/** A move's verdict from the best move's score and its own ({cp, mate}, both for the side that moved), with the
 * rules of the analysis (lucidfish/engine.py): mates are judged the Lichess way (allowing a forced mate is a
 * blunder unless you were lost anyway), everything else by the winning chances given away. */
function classifyMove(best, mine, playedIsBest) {
  if (playedIsBest) return "best";
  const clamp = (x) => (x.mate != null ? (x.mate > 0 ? 1000 : -1000) : Math.max(-1000, Math.min(1000, x.cp)));
  const raw = (x) => (x.mate != null ? (x.mate > 0 ? 1e6 : -1e6) : x.cp);
  if ((best.mate ?? 0) > 0 && !((mine.mate ?? 0) > 0)) {                  // missed a forced mate
    const a = raw(mine);
    return a > 999 ? "inaccuracy" : a > 700 ? "mistake" : "blunder";
  }
  if ((mine.mate ?? 0) < 0 && !((best.mate ?? 0) < 0)) {                  // allowed a forced mate
    const b = raw(best);
    return b < -999 ? "inaccuracy" : b < -700 ? "mistake" : "blunder";
  }
  const loss = Math.max(0, winPct(clamp(best)) - winPct(clamp(mine)));
  if (loss >= 15) return "blunder";
  if (loss >= 10) return "mistake";
  if (loss >= 5) return "inaccuracy";
  return clamp(best) - clamp(mine) <= 10 ? "best" : "good";
}

/** A stored candidate's score for the side to move: {cp, mate} (the stored mate is in the White-view score). */
function candidateScore(c, fen) {
  const mate = c.score?.[0] === "#" ? parseInt(c.score.slice(1), 10) * (sideToMove(fen) === "w" ? 1 : -1) : null;
  return { cp: c.cp, mate };
}

/** Judge a practice move with the in-browser engine: its best move and the tried move, searched alike. */
async function engineVerdict(fen, uci, fenAfter) {
  const opts = { depth: 16, movetime: 5000 };
  const best = await Engine.search(fen, opts);
  const mine = await Engine.search(fen, { ...opts, searchmoves: [uci] });
  const b = best.lines[0], m = mine.lines[0];
  const cls = !b || !m ? "unknown" : classifyMove({ cp: b.cp, mate: b.mate }, { cp: m.cp, mate: m.mate }, best.bestmove === uci);
  const solved = cls === "best" || cls === "good";
  const refutation = solved ? [] : uciSteps(fenAfter, (m?.pv || []).slice(1));
  const bestSteps = uciSteps(fen, b?.pv || []);
  return {
    cls, solved, eval: engineEval(m, fen),
    best: bestSteps[0]?.san || "", best_uci: best.bestmove, best_steps: bestSteps,
    refutation: stepsText(fenAfter, refutation), refutation_steps: refutation,
  };
}

/* ================================================================ shared copy (no server) */

function sharedDate() {
  return EXPORT ? fmtDate(new Date(EXPORT.generated)) : "";
}

/** Your own drawings in a shared copy stay in this browser, keyed by the game (stable across new copies). */
function drawingsKey(id) {
  const row = EXPORT.games.find((g) => String(g.id) === String(id));
  return `lucidfish-shared-drawings-${row?.fingerprint || id}`;
}

/** Drawings kept by older versions (in this browser or a progress file) have a "color" key, now "colour". */
function britishDrawings(annotations) {
  for (const d of Object.values(annotations || {})) {
    for (const x of [...(d.arrows || []), ...(d.circles || [])]) {
      if (x.colour === undefined && "color" in x) { x.colour = x.color; delete x.color; }
    }
  }
  return annotations;
}

function winPct(cp) { return 50 + 50 * (2 / (1 + Math.exp(-0.00368208 * cp)) - 1); }

/** Every analysed position of the shared copy, to find the stored engine lines of a practice position. */
let storedByFen = null;
function storedMove(fen) {
  if (!storedByFen) {
    storedByFen = new Map();
    for (const g of Object.values(EXPORT.details)) {
      for (const m of g.moves) if (m.fen_before && !storedByFen.has(m.fen_before)) storedByFen.set(m.fen_before, m);
    }
  }
  return storedByFen.get(fen);
}

/** Practice without the server. A move among the engine's top moves stored with the game is judged from them
 * (same verdicts as the analysis); any other move by the in-browser engine, if this copy includes it. */
async function offlineCheck({ fen, uci }) {
  const c = new Chess(fen);
  let mv;
  try { mv = c.move({ from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci[4] }); }
  catch { throw new Error("That move is not legal in this position."); }
  const fenAfter = c.fen(), whiteMoved = mv.color === "w";
  const m = storedMove(fen), cands = m?.candidates || [];
  const best = cands[0], hit = cands.find((x) => x.uci === uci);
  const base = { san: mv.san, uci, fen_after: fenAfter, best: m?.best || best?.san || "",
    best_uci: m?.best_uci || best?.uci || "", best_steps: best?.steps || [] };
  const none = { refutation: "", refutation_steps: [] };
  if (c.isCheckmate()) return { ...base, ...none, cls: "best", solved: true, eval: whiteMoved ? "1-0" : "0-1" };
  if (hit && best) {
    const cls = classifyMove(candidateScore(best, fen), candidateScore(hit, fen), hit === best);
    const solved = cls === "best" || cls === "good";
    return { ...base, cls, solved, eval: hit.score,
      refutation: solved ? "" : stepsText(fenAfter, (hit.steps || []).slice(1)),
      refutation_steps: solved ? [] : (hit.steps || []).slice(1) };
  }
  if (Engine.available()) {
    const v = await engineVerdict(fen, uci, fenAfter);
    return { ...base, ...v, best: base.best || v.best, best_uci: base.best_uci || v.best_uci,
      best_steps: base.best_steps.length ? base.best_steps : v.best_steps };
  }
  return { ...base, ...none, cls: "unknown", solved: false, eval: "" };
}

function exportApi(path, { method = "GET", body } = {}) {
  const E = EXPORT, p = path.split("?")[0];
  let m;
  try {
    if (method === "GET") {
      if (p === "/api/health") {
        return Promise.resolve({ version: E.version, busy: false, engine: { ok: true, name: "Saved analysis" },
          coach: { enabled: false, ok: true } });
      }
      if (p === "/api/profiles") return Promise.resolve({ profiles: [E.profile], active: E.profile.id });
      if (p === "/api/profile") {
        const tc = new URLSearchParams(path.split("?")[1] || "").get("tc");
        return Promise.resolve({ profile: E.profile, games: E.games, time_classes: E.time_classes || {},
          stats: tc ? (E.stats_by_class?.[tc] || { games: 0 }) : E.stats });
      }
      if ((m = p.match(/^\/api\/profile\/game\/(\d+)$/))) {
        const g = E.details[m[1]];
        if (!g) return Promise.reject(new Error("unknown game"));
        const copy = JSON.parse(JSON.stringify(g));   // the page changes games (drawings); keep the original
        try {
          const mine = localStorage.getItem(drawingsKey(g.id));
          if (mine) copy.annotations = britishDrawings(JSON.parse(mine));
        } catch { /* storage unavailable: show the shared drawings */ }
        return Promise.resolve(copy);
      }
    }
    if (method === "GET" && p === "/api/train") return Promise.resolve({ items: E.train || [] });
    if (method === "POST" && p === "/api/check_move") return offlineCheck(body);
    if (method === "POST" && (m = p.match(/^\/api\/profile\/game\/(\d+)\/annotations$/))) {
      try { localStorage.setItem(drawingsKey(m[1]), JSON.stringify(body.annotations || {})); }
      catch { /* private mode: drawings last until the page is closed */ }
      return Promise.resolve({ ok: true });
    }
  } catch (e) { return Promise.reject(e); }
  return Promise.reject(new Error("This shared copy can't do that: it needs Lucidfish running on the computer that made it."));
}

if (EXPORT) {
  document.body.classList.add("export-mode");
  $("shareChip").textContent = `📤 Shared copy · ${sharedDate()}`;
}

/* ================================================================ modals */

function openModal(id) { $(id).classList.remove("hidden"); }
function closeModal(id) { $(id).classList.add("hidden"); }
document.querySelectorAll(".modal-back").forEach((m) => {
  m.addEventListener("click", (e) => { if (e.target === m) closeModal(m.id); });
  m.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => closeModal(m.id)));
});
const modalOpen = () => [...document.querySelectorAll(".modal-back")].some((m) => !m.classList.contains("hidden"));

/* ================================================================ time control filter (Home, Games, Analyse) */

const TIME_CLASSES = ["bullet", "blitz", "rapid", "classical", "daily"];
state.tc = (() => { try { return localStorage.getItem("lucidfish-tc") || ""; } catch { return ""; } })();
state.timeClasses = {};   // analysed games per time control
state.gamesTotal = 0;     // all analysed games (some have no time control, e.g. over the board)

/** Chips: "All" plus the time controls you have games in (Home, Games), or every one (Analyse). */
function renderTcBars() {
  const total = state.gamesTotal;
  document.querySelectorAll("[data-tc-bar]").forEach((bar) => {
    const analysed = bar.dataset.tcBar === "analysed";
    const classes = analysed ? TIME_CLASSES.filter((c) => state.timeClasses[c] || c === state.tc) : TIME_CLASSES;
    if (analysed && !total) { bar.innerHTML = ""; return; }
    const chip = (tc, label, n) => `<button data-tc="${tc}" class="${state.tc === tc ? "active" : ""}" aria-pressed="${state.tc === tc}">`
      + `${label}${n != null ? ` <span class="n">${n}</span>` : ""}</button>`;
    bar.innerHTML = chip("", "All", analysed ? total : null)
      + classes.map((c) => chip(c, cap(c), analysed ? state.timeClasses[c] || 0 : null)).join("");
  });
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-tc-bar] [data-tc]");
  if (b) setTc(b.dataset.tc);
});

function setTc(tc) {
  if (tc === state.tc) return;
  state.tc = tc;
  try { localStorage.setItem("lucidfish-tc", tc); } catch { /* remembered for this visit only */ }
  renderTcBars();
  if (state.page === "dashboard") loadDashboard();
  else if (state.page === "games") rerenderGames();
  else if (state.page === "analyse") loadRecentGames();
  else if (state.page === "train") renderTrainPage();
}

/* ================================================================ dashboard */

/** "3 min ago", "yesterday", "5 days ago" for a Unix time. */
function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  const days = Math.round(s / 86400);
  return days === 1 ? "yesterday" : `${days} days ago`;
}

/** Next to the coach review: when it was written, or that it's waiting / being rewritten. */
function renderReviewStatus(d) {
  if (EXPORT) { $("summaryStatus").textContent = ""; return; }
  const r = d.review || {}, when = d.profile?.summary_times?.[state.tc || ""];
  $("summaryStatus").textContent = r.running
    ? ((r.time_class || "") === (state.tc || "") ? "✍ Your coach is updating this review…" : "✍ Your coach is updating your reviews…")
    : r.pending ? "Will update when the analysis queue has finished"
      : when ? `Updated ${ago(when)}` : "";
  $("summaryStatus").classList.toggle("busy-text", !!r.running);
}

async function loadDashboard() {
  let d;
  try { d = await api(`/api/profile${state.tc ? `?tc=${state.tc}` : ""}`); } catch (e) { toast(e.message, true); return; }
  state.timeClasses = d.time_classes || {};
  state.gamesTotal = (d.games || []).length;
  renderTcBars();
  if (!d.profile) {
    $("dashTitle").textContent = "Welcome to Lucidfish";
    $("stats").innerHTML = "";
    $("summary").innerHTML = `<div class="empty"><div class="big">♞</div>Create a profile so the coach can learn your style,
      or jump straight into <a href="#" id="emptyAnalyse">analysing a game</a>.<br><br>
      <button id="emptyProfile">Create profile</button></div>`;
    $("emptyProfile").addEventListener("click", () => openProfile(null));
    $("emptyAnalyse").addEventListener("click", (e) => { e.preventDefault(); showPage("analyse"); });
    $("savedGames").innerHTML = `<div class="empty small">No games yet.</div>`;
    $("openings").innerHTML = `<div class="empty small">Appears after your first analysis.</div>`;
    state.savedGames = [];
    return;
  }
  state.profile = d.profile;
  state.savedGames = d.games || [];
  const s = d.stats || {};
  $("dashTitle").textContent = `${d.profile.name}'s coach review`;
  const tcName = state.tc ? cap(state.tc) : "";
  $("summaryTitle").textContent = tcName ? `Coach review · ${tcName}` : "Coach review";
  const stat = (label, value) => `<div class="stat"><b>${value ?? "—"}</b><span>${label}</span></div>`;
  $("stats").innerHTML = s.games
    ? stat("games analysed", s.games) + stat("record (W-L-D)", `${s.wins}-${s.losses}-${s.draws}`)
      + stat("average accuracy", s.avg_accuracy != null ? `${s.avg_accuracy}%` : "—")
      + stat("avg centipawn loss", s.avg_acpl) + stat("blunders / game", s.blunders_per_game)
      + stat("mistakes / game", s.mistakes_per_game)
    : state.tc ? `<div class="empty small">No ${esc(state.tc)} games analysed yet.</div>` : "";
  renderInsights(s);
  const review = state.tc ? d.profile.summaries?.[state.tc] : d.profile.summary;
  const waiting = !state.tc
    ? (s.games ? "Analyse one more game and your coach will write a review of your play." : "Analyse a couple of your games and your coach will write a review of your play here.")
    : s.games >= 2 ? `No ${state.tc} review yet.${EXPORT ? "" : " Press ⟳ Update review to write one."}`
      : `Your ${state.tc} review appears after two analysed ${state.tc} games.`;
  $("summary").innerHTML = review ? md(review) : `<div class="empty small">${esc(waiting)}</div>`;
  renderReviewStatus(d);
  renderSavedGames();
  loadTrainItems().then(() => renderGoTrain());
  $("openings").innerHTML = (s.openings || []).length
    ? s.openings.map((o) => `<div class="item static"><span class="title">${esc(o.name)}</span>
        <span class="meta">${o.games} game${o.games === 1 ? "" : "s"} · ${o.w}W ${o.l}L ${o.d}D${o.acpl != null ? ` · ${o.acpl} ACPL` : ""}</span></div>`).join("")
    : `<div class="empty small">Appears after your first analysis.</div>`;
}

const perGame = (n) => `${n} mistake${n === 1 ? "" : "s"} per game`;

const FINDING_ICONS = { weakness: "⚠", strength: "✓", info: "•" };

function renderInsights(s) {
  $("findingsCard").classList.toggle("hidden", !s.games);
  $("findings").innerHTML = (s.findings || []).length
    ? s.findings.map((f) => `<div class="finding ${esc(f.kind)}"><span class="f-icon">${FINDING_ICONS[f.kind] || "•"}</span>`
      + `<span>${esc(f.text)}</span></div>`).join("")
    : `<div class="empty small">Patterns across your games appear after about 3 analysed games where Lucidfish knows which side
        you played (it detects this from your chess.com / Lichess username).</div>`;
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
  const box = $("savedGames"), mine = state.savedGames.filter((g) => !state.tc || g.time_class === state.tc);
  const n = mine.length;
  $("seeAllGames").classList.toggle("hidden", !n);
  $("seeAllGames").textContent = n > RECENT_ON_DASHBOARD ? `See all ${n} games →` : "All games →";
  if (!n) { box.innerHTML = `<div class="empty small">No analysed games yet.</div>`; return; }
  box.innerHTML = "";
  for (const g of mine.slice(0, RECENT_ON_DASHBOARD)) {
    const el = document.createElement("div");
    el.className = "item";
    el.innerHTML = `<span class="title">${esc(g.white)} vs ${esc(g.black)}</span>${resultChip(resultFor(g))}
      <span class="meta">${esc([g.accuracy != null ? `${g.accuracy}%` : "", g.time_class, g.date && !g.date.includes("?") ? g.date : ""].filter(Boolean).join(" · "))}</span>
      <button class="ghost icon server-only" title="Delete this analysis" aria-label="Delete">🗑</button>`;
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
    const d = await api("/api/profile/refresh_summary", { method: "POST", body: { time_class: state.tc } });
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
  return g.analysed_at ? new Date(`${g.analysed_at.replace(" ", "T")}Z`) : null;
}
function opponentOf(g) {
  return g.user_side === "white" ? g.black : g.user_side === "black" ? g.white : null;
}
const accClass = (v) => (v >= 90 ? "a-hi" : v >= 75 ? "a-mid" : v >= 60 ? "a-low" : "a-bad");

function filteredGames() {
  const words = $("gSearch").value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const res = $("gResult").value, colour = $("gColour").value, tc = state.tc;
  const list = state.savedGames.filter((g) => {
    if (res && resultFor(g) !== res) return false;
    if (colour && g.user_side !== colour) return false;
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
  const count = (b, n, what) => `<span class="g-err${n ? "" : " zero"}" title="${n || 0} ${what}">${b}<span>${n || 0}</span></span>`;
  const plural = (n, word) => (n === 1 ? word : word.endsWith("y") ? `${word.slice(0, -1)}ies` : `${word}s`);
  const good = g.best_moves == null ? "" : count(`<span class="badge b-great">⚡</span>`, g.great_moves,
    `${plural(g.great_moves, "great move")}: the only good move at a critical moment`)
    + count(badge("best"), g.best_moves, `${plural(g.best_moves, "best move")} (the engine's top choice)`)
    + count(badge("good"), g.good_moves, `${plural(g.good_moves, "good move")} (close to the best)`)
    + `<span class="g-sep" aria-hidden="true"></span>`;
  const errs = good + [["blunder", g.blunders], ["mistake", g.mistakes], ["inaccuracy", g.inaccuracies]]
    .map(([c, n]) => count(badge(c), n, plural(n, LABELS[c].toLowerCase()))).join("");
  return `<div class="g-row" data-gid="${g.id}" tabindex="0" role="button"
      title="${esc(analysed ? `Analysed ${fmtDate(analysed)}` : "")}">
    <div class="g-result">${resultChip(resultFor(g)) || `<span class="chip">${esc(g.result || "*")}</span>`}</div>
    <div class="g-main"><div class="g-title">${colour}${title}</div><div class="g-meta">${esc(meta)}</div></div>
    <div class="g-acc">${g.accuracy != null ? `<b class="${accClass(g.accuracy)}">${g.accuracy}%</b>` : "<b>—</b>"}<span>accuracy</span></div>
    <div class="g-errs">${errs}</div>
    <div class="g-actions server-only">
      <button class="ghost icon" data-ga="reanalyse" title="Analyse again with your current settings" aria-label="Re-analyse">↻</button>
      <button class="ghost icon" data-ga="delete" title="Delete this analysis" aria-label="Delete">🗑</button>
    </div>
  </div>`;
}

function renderGames() {
  const box = $("gList"), all = state.savedGames;
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
      <button data-gempty="analyse">Analyse a game</button></div>`;
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
["gResult", "gColour", "gSort"].forEach((id) => $(id).addEventListener("change", rerenderGames));
$("gMore").addEventListener("click", () => { gamesView.limit += 50; renderGames(); });


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
    else if (what === "analyse") showPage("analyse");
    else { $("gSearch").value = ""; ["gResult", "gColour"].forEach((id) => { $(id).value = ""; }); setTc(""); rerenderGames(); }
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

/** Your latest 25 chess.com games (of one time control, looking back up to a year for them). */
async function fetchChesscom(user, tc = "") {
  if (tc === "classical") return [];   // chess.com calls every game of 10 minutes or more "rapid"
  const r = await fetch(`https://api.chess.com/pub/player/${encodeURIComponent(user.toLowerCase())}/games/archives`);
  if (r.status === 404) throw new Error(`chess.com user "${user}" not found`);
  if (!r.ok) throw new Error(`chess.com answered HTTP ${r.status}`);
  const archives = (await r.json()).archives || [];
  const games = [];
  for (const url of archives.slice(tc ? -12 : -3).reverse()) {
    const month = await (await fetch(url)).json();
    for (const g of (month.games || []).reverse()) {
      if (!g.pgn || !["chess", "chess960"].includes(g.rules || "chess")) continue;
      if (tc && g.time_class !== tc) continue;
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

const LICHESS_PERF = { bullet: "ultraBullet,bullet", blitz: "blitz", rapid: "rapid", classical: "classical", daily: "correspondence" };
const LICHESS_CLASS = { ultraBullet: "bullet", correspondence: "daily" };

/** Your latest 25 Lichess games (of one time control). */
async function fetchLichess(user, tc = "") {
  const url = `https://lichess.org/api/games/user/${encodeURIComponent(user)}?max=25&pgnInJson=true&clocks=true&opening=true&accuracy=true`
    + (tc ? `&perfType=${LICHESS_PERF[tc]}` : "");
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
      date: new Date(g.createdAt), timeClass: LICHESS_CLASS[g.speed] || g.speed || "", accuracy: me.analysis?.accuracy ?? null,
    });
  }
  return games;
}

async function loadRecentGames() {
  const box = $("gameList");
  const user = state.source === "chesscom" ? state.profile?.chesscom_user : state.profile?.lichess_user;
  const site = state.source === "chesscom" ? "chess.com" : "Lichess";
  state.selected.clear(); lastPicked = null; updateSelection();
  $("selectBar").classList.add("hidden");
  if (!user) {
    box.innerHTML = `<div class="empty small">Add your ${site} username to your profile to see your recent games here.<br><br>
      <button class="secondary sm" id="addUser">${state.profile ? "Edit profile" : "Create profile"}</button></div>`;
    $("addUser").addEventListener("click", () => openProfile(state.profile));
    state.recent = [];
    return;
  }
  const tc = state.tc;
  state.recentTc = tc;
  renderTcBars();
  box.innerHTML = `<div class="empty small">Loading ${esc(site)} ${esc(tc)} games for ${esc(user)}…</div>`;
  try {
    state.recent = state.source === "chesscom" ? await fetchChesscom(user, tc) : await fetchLichess(user, tc);
    if (state.recentTc !== tc) return;   // the filter changed meanwhile: a newer load is on its way
  } catch (e) {
    state.recent = [];
    box.innerHTML = `<div class="empty small">Couldn't load games from ${esc(site)}: ${esc(e.message)}.<br>You can paste a PGN instead.</div>`;
    return;
  }
  const analysed = new Map(state.savedGames.map((g) => [g.fingerprint, g.id]));
  for (const g of state.recent) {
    g.fingerprint = await sha1(g.pgn.trim());
    g.storedId = analysed.get(g.fingerprint);
  }
  renderRecentGames();
}

/* Clicking a game selects it (for "Analyse selected"); only the button on the right
 * starts an analysis or opens a saved one, so nothing expensive happens by accident. */
function recentRow(g, i) {
  const sel = state.selected.has(i);
  const action = g.storedId
    ? `<button class="secondary sm" data-ra="open" title="Open the saved analysis">Open</button>`
    : `<button class="secondary sm" data-ra="analyse" title="Analyse just this game now">Analyse</button>`;
  return `<div class="item recent${sel ? " selected" : ""}" data-i="${i}" role="option" aria-selected="${sel}" tabindex="0">
    <input type="checkbox" tabindex="-1" aria-hidden="true"${sel ? " checked" : ""}>
    <span class="title">${esc(g.white)} vs ${esc(g.black)}</span>${resultChip(g.result)}
    ${g.storedId ? `<span class="chip accent">ANALYSED</span>` : ""}
    <span class="meta">${g.accuracy != null ? `${Number(g.accuracy).toFixed(1)}% · ` : ""}${esc(g.timeClass)}${g.rating ? ` · ${g.rating}` : ""} · ${fmtDate(g.date)}</span>
    ${action}</div>`;
}

function renderRecentGames() {
  const box = $("gameList");
  $("selectBar").classList.toggle("hidden", !state.recent.length);
  const tc = state.recentTc;
  box.innerHTML = state.recent.length ? state.recent.map(recentRow).join("")
    : `<div class="empty small">${tc === "classical" && state.source === "chesscom"
      ? "chess.com has no classical games: it calls every game of 10 minutes or more rapid."
      : `No ${tc ? `${esc(tc)} ` : ""}games found${tc && state.source === "chesscom" ? " in the last 12 months" : ""}.`}</div>`;
  $("analyseBatch").textContent = tc ? `Analyse ${tc} batch` : "Analyse batch";
  updateSelection();
}

let lastPicked = null;
function pickRecent(i, shift) {
  const on = !state.selected.has(i);
  const [from, to] = shift && lastPicked != null ? [Math.min(lastPicked, i), Math.max(lastPicked, i)] : [i, i];
  for (let k = from; k <= to; k++) { if (on) state.selected.add(k); else state.selected.delete(k); }
  lastPicked = i;
  document.querySelectorAll("#gameList .item.recent").forEach((row) => {
    const sel = state.selected.has(Number(row.dataset.i));
    row.classList.toggle("selected", sel);
    row.setAttribute("aria-selected", sel);
    row.querySelector("input").checked = sel;
  });
  updateSelection();
}
$("gameList").addEventListener("mousedown", (e) => { if (e.shiftKey) e.preventDefault(); });   // no text selection
$("gameList").addEventListener("click", (e) => {
  const row = e.target.closest(".item.recent");
  if (!row) return;
  const i = Number(row.dataset.i), g = state.recent[i];
  const action = e.target.closest("[data-ra]")?.dataset.ra;
  if (action === "open") openStoredGame(g.storedId);
  else if (action === "analyse") startAnalysis(g.pgn, g.side, g.rating);
  else pickRecent(i, e.shiftKey);
});
$("gameList").addEventListener("keydown", (e) => {
  const row = e.target.closest?.(".item.recent");
  if (row && e.target === row && (e.key === " " || e.key === "Enter")) {
    e.preventDefault();
    pickRecent(Number(row.dataset.i), e.shiftKey);
  }
});
$("selectAll").addEventListener("change", (e) => {
  state.selected = new Set(e.target.checked ? state.recent.map((_, i) => i) : []);
  lastPicked = null;
  renderRecentGames();
});

/** Refresh the ANALYSED marks after queued games finish (no refetch from the site). */
function markAnalysed() {
  const analysed = new Map(state.savedGames.map((g) => [g.fingerprint, g.id]));
  for (const g of state.recent) if (g.fingerprint) g.storedId = analysed.get(g.fingerprint);
  renderRecentGames();
}

function updateSelection() {
  const n = state.selected.size, all = $("selectAll");
  $("analyseSelected").disabled = !n;
  $("analyseSelected").textContent = n ? `Analyse selected (${n})` : "Analyse selected";
  all.checked = n > 0 && n === state.recent.length;
  all.indeterminate = n > 0 && n < state.recent.length;
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
  state.selected.clear(); lastPicked = null;
  renderRecentGames();
  pollQueue();
}
$("analyseSelected").addEventListener("click", () => startImport([...state.selected].sort((a, b) => a - b).map((i) => state.recent[i])));
$("analyseBatch").addEventListener("click", () => {
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
  const wasReviewing = queue.reviewing;
  queue.reviewing = !!d.reviewing;
  if (wasReviewing && !queue.reviewing && state.page === "dashboard" && !saved) loadDashboard();   // the new review
  if (saved) {
    loadDashboard().then(() => {
      if (state.page === "analyse") markAnalysed();
      if (state.page === "games") renderGames();
    });
  }
  renderQueuePill();
  if (!$("queueModal").classList.contains("hidden")) renderQueue();
  const active = d.running || d.queued.length;
  const open = !$("queueModal").classList.contains("hidden");
  queue.timer = setTimeout(pollQueue, d.running ? 1000 : active || open || d.reviewing ? 3000 : 20000);
}

function renderQueuePill() {
  const d = queue.data, pill = $("queuePill");
  const active = d && (d.running || d.queued.length);
  pill.classList.toggle("hidden", !active && !d?.reviewing);
  pill.classList.toggle("reviewing", !active && !!d?.reviewing);
  const status = $("importStatus");
  if (!active) {
    status.innerHTML = ""; $("gamesQueueStatus").innerHTML = "";
    if (d?.reviewing) {
      $("queueText").textContent = "Updating your coach review…";
      $("queueDot").className = "dot busy";
      $("queueMiniBar").style.width = "0%";
    }
    return;
  }
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
$("queuePill").addEventListener("click", () => ($("queuePill").classList.contains("reviewing") ? showPage("dashboard") : openQueue()));
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
function tcChip(tc) {
  return tc ? `<span class="chip tc-chip" title="Time control">${esc(cap(tc))}</span>` : "";
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
    : `<div class="empty small">The queue is empty. Add games from <a href="#" data-qa="analyse">Analyse games</a> — pick several and press “Analyse selected”, or queue your last 5–20 games at once.</div>`;
  $("queuePause").textContent = d.paused ? "▶ Resume" : "Pause";
  $("queuePause").disabled = !active && !d.paused;
  $("queueStopAll").disabled = !active;
  $("queueClear").disabled = !d.finished.length;

  const r = d.running;
  $("queueRunning").innerHTML = r ? `<div class="section-label">Now analysing</div>`
    + `<div class="qjob" data-id="${r.id}"><div class="row"><span class="dot busy"></span><b class="grow ellipsis">${esc(r.title)}</b>${tcChip(r.time_class)}${detailChip(r.detail)}`
    + `<span class="small">${r.reviewing ? "writing the review…" : `about ${fmtDuration(r.eta_s)} left`}</span>`
    + `<button class="secondary sm" data-qa="open">Open</button><button class="danger sm" data-qa="cancel">Stop</button></div>`
    + `<div class="bar dual"><div class="eng" style="width:${(100 * r.engine_done / Math.max(1, r.total)).toFixed(1)}%"></div>`
    + `<div class="done" style="width:${(100 * r.done / Math.max(1, r.total)).toFixed(1)}%"></div></div>`
    + `<div class="small">${esc(r.label)} · ${r.done}/${r.total} moves ready${r.engine_done > r.done ? ` · engine at move ${r.engine_done}` : ""}</div></div>` : "";

  $("queueUpcoming").innerHTML = d.queued.length ? `<div class="section-label">Up next</div><div class="list">`
    + d.queued.map((j, i) => `<div class="item qrow" data-id="${j.id}" title="Open this game">`
      + `<span class="qpos">${i + 1}</span><span class="title">${esc(j.title)}</span>${tcChip(j.time_class)}${detailChip(j.detail)}`
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
      + `<span class="title">${esc(j.title)}</span>${tcChip(j.time_class)}`
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
      if (qa === "analyse") { closeModal("queueModal"); showPage("analyse"); return; }
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
$("analysePgn").addEventListener("click", () => {
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

/* ================================================================ analysis jobs */

let pollTimer = null;

async function startAnalysis(pgn, side, rating) {
  if (!pgn || !pgn.trim()) { toast("Paste a PGN first.", true); return; }
  if (side === "both") side = null;
  else if (!side) side = detectSide(pgn);
  let d;
  try { d = await api("/api/analyse", { method: "POST", body: { pgn, side, elo: rating || null } }); }
  catch (e) { toast(e.message, true); return; }
  openJob(d.job_id, pgn);
  pollQueue();
}

/** Show a queued or running analysis on the Game page; moves stream in as they're ready. */
function openJob(jobId, pgn = "", fromRoute = false) {
  clearTimeout(pollTimer);
  state.game = { mode: "game", pgn, jobId, status: "queued", headers: {}, side: null, moves: [], review: "",
    opening: "", accuracy: {}, warnings: [], total: 0, coach: "", chapters: [], annotations: {} };
  resetGameView();
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
    pgn: g.pgn || "", gameId: j.game_id || null, chapters: j.chapters || [] });
  if (g.gameId && Object.keys(g.annotations || {}).length) saveDrawings();   // drawn while it was analysing
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
    review: d.review, opening: d.opening, accuracy: d.accuracy || {}, warnings: [], total: d.moves.length, status: "done",
    chapters: d.chapters || [], annotations: d.annotations || {} };
  resetGameView();
  showPage("game", fromRoute);
  renderHeader(); renderReview();
  goTo(d.moves.length ? 0 : -1);
}

function resetGameView() {
  exitExplore(false);
  state.cur = -1; state.preview = null; state.practice = null; state.gameChat.length = 0;
  state.revealed = new Set();
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
  $("practiseAll").classList.toggle("hidden", !(g.mode === "game" && !g.jobId && myMistakes().length));
  $("reanalyseBtn").classList.toggle("hidden", !(g.mode === "game" && g.gameId && !g.jobId));
  $("crumbTitle").textContent = g.mode === "position" ? "Board editor position"
    : `${(g.headers || {}).White || "White"} vs ${(g.headers || {}).Black || "Black"}`;
  $("backBtn").textContent = g.mode === "position" ? "← Board editor" : "← Games";
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
    draggable: true,   // pieces only move in practice and Explore mode (see onDragStart)
    onDragStart: (source, piece) => {
      const x = state.explore;
      if (x) return !x.chess.isGameOver() && piece[0] === x.chess.turn();
      const p = state.practice;
      if (!p || p.busy || p.result || state.preview) return false;
      return piece[0] === p.fen.split(" ")[1];   // only the side to move
    },
    onDrop: (source, target, piece) => {
      if (state.explore) {
        const promotes = piece[1] === "P" && (target[1] === "8" || target[1] === "1");
        if (target === "offboard" || source === target || !exploreMove(source + target + (promotes ? "q" : ""))) return "snapback";
        exploreUpdate(false, false);   // the board catches up (castling, en passant, promotion) in onSnapEnd
        return undefined;
      }
      if (!state.practice || target === "offboard" || source === target) return "snapback";
      const promotes = piece[1] === "P" && (target[1] === "8" || target[1] === "1");
      tryPracticeMove(source + target + (promotes ? "q" : ""));
      return undefined;
    },
    onSnapEnd: () => { if (state.explore) board.position(state.explore.chess.fen(), false); },
  });
}

function orientation() { return board ? board.orientation() : "white"; }

function refreshBoard() {
  if (!board || !state.game) return;
  if (state.explore) { board.position(state.explore.chess.fen(), false); renderExplore(); return; }
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

let lastOverlay = { arrows: [], squares: [] };

function drawOverlay({ arrows = [], squares = [] }) {
  lastOverlay = { arrows, squares };
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
    out += `<rect x="${x - 0.5}" y="${y - 0.5}" width="1" height="1" fill="${s.colour}"/>`;
  }
  for (const a of arrows) {
    if (!a.from || !a.to || a.from === a.to) continue;
    const [x1, y1] = sqXY(a.from), [x2, y2] = sqXY(a.to);
    const len = Math.hypot(x2 - x1, y2 - y1), ux = (x2 - x1) / len, uy = (y2 - y1) / len;
    const hx = x2 - ux * 0.38, hy = y2 - uy * 0.38;
    out += `<line x1="${x1}" y1="${y1}" x2="${hx}" y2="${hy}" stroke="${a.colour}" stroke-width="0.16" stroke-linecap="round" opacity="0.85"/>`
      + `<polygon points="${x2 - ux * 0.08},${y2 - uy * 0.08} ${hx - uy * 0.22},${hy + ux * 0.22} ${hx + uy * 0.22},${hy - ux * 0.22}" fill="${a.colour}" opacity="0.85"/>`;
  }
  svg.innerHTML = out + drawingsSvg();
  renderDrawBar();
}
const redrawOverlay = () => drawOverlay(lastOverlay);

/* ================================================================ your drawings */

const PEN = { green: "#15781b", red: "#b3261e", blue: "#1f5fbf", yellow: "#e08e00" };
let drawDrag = null;   // {from, to, colour} while drawing

/** Drawings belong to the position on the board (placement part of the FEN), so they
 * come back whenever that position is shown, in the game, a variation or practice. */
function drawingKey() { return board ? board.fen() : ""; }
function currentDrawing() {
  const g = state.game;
  return g?.annotations?.[drawingKey()] || { arrows: [], circles: [] };
}

function arrowSvg(a, width, opacity) {
  const [x1, y1] = sqXY(a.from), [x2, y2] = sqXY(a.to);
  const len = Math.hypot(x2 - x1, y2 - y1), ux = (x2 - x1) / len, uy = (y2 - y1) / len;
  const hx = x2 - ux * 0.42, hy = y2 - uy * 0.42, c = PEN[a.colour] || PEN.green;
  return `<line x1="${x1}" y1="${y1}" x2="${hx}" y2="${hy}" stroke="${c}" stroke-width="${width}" stroke-linecap="round" opacity="${opacity}"/>`
    + `<polygon points="${x2 - ux * 0.06},${y2 - uy * 0.06} ${hx - uy * 0.26},${hy + ux * 0.26} ${hx + uy * 0.26},${hy - ux * 0.26}" fill="${c}" opacity="${opacity}"/>`;
}

function drawingsSvg() {
  if (!state.game || !board) return "";
  const d = currentDrawing();
  let out = "";
  for (const c of d.circles) {
    const [x, y] = sqXY(c.sq);
    out += `<circle cx="${x}" cy="${y}" r="0.45" fill="none" stroke="${PEN[c.colour] || PEN.green}" stroke-width="0.07" opacity="0.85"/>`;
  }
  for (const a of d.arrows) out += arrowSvg(a, 0.2, 0.8);
  if (drawDrag && drawDrag.from !== drawDrag.to) out += arrowSvg(drawDrag, 0.2, 0.5);
  else if (drawDrag) {
    const [x, y] = sqXY(drawDrag.from);
    out += `<circle cx="${x}" cy="${y}" r="0.45" fill="none" stroke="${PEN[drawDrag.colour]}" stroke-width="0.07" opacity="0.5"/>`;
  }
  return out;
}

function squareAt(clientX, clientY) {
  const el = document.querySelector("#board .board-b72b1");
  if (!el) return null;
  const r = el.getBoundingClientRect();
  const x = Math.floor(((clientX - r.left) / r.width) * 8), y = Math.floor(((clientY - r.top) / r.height) * 8);
  if (x < 0 || x > 7 || y < 0 || y > 7) return null;
  const white = orientation() === "white";
  return "abcdefgh"[white ? x : 7 - x] + ((white ? 7 - y : y) + 1);
}

function penFor(e) {
  if (e.shiftKey && e.altKey) return "yellow";
  if (e.shiftKey) return "red";
  if (e.altKey || e.ctrlKey || e.metaKey) return "blue";
  return state.draw.colour;
}

function finishDrawing() {
  const g = state.game, d = drawDrag;
  drawDrag = null;
  document.body.classList.remove("no-select");
  document.getSelection()?.removeAllRanges();
  if (!g || !d) return;
  g.annotations = g.annotations || {};
  const key = drawingKey();
  const cur = g.annotations[key] || { arrows: [], circles: [] };
  if (d.from === d.to) {
    const old = cur.circles.find((c) => c.sq === d.from);
    cur.circles = cur.circles.filter((c) => c.sq !== d.from);
    if (!old || old.colour !== d.colour) cur.circles.push({ sq: d.from, colour: d.colour });
  } else {
    const old = cur.arrows.find((a) => a.from === d.from && a.to === d.to);
    cur.arrows = cur.arrows.filter((a) => !(a.from === d.from && a.to === d.to));
    if (!old || old.colour !== d.colour) cur.arrows.push({ from: d.from, to: d.to, colour: d.colour });
  }
  if (cur.arrows.length || cur.circles.length) g.annotations[key] = cur; else delete g.annotations[key];
  redrawOverlay();
  saveDrawingsSoon();
}

function clearDrawing() {
  const g = state.game;
  if (!g?.annotations?.[drawingKey()]) return;
  delete g.annotations[drawingKey()];
  redrawOverlay();
  saveDrawingsSoon();
}

async function saveDrawings() {
  const g = state.game;
  if (!g?.gameId) return;   // not saved yet: kept in memory and saved once the analysis is stored
  try { await api(`/api/profile/game/${g.gameId}/annotations`, { method: "POST", body: { annotations: g.annotations } }); }
  catch (e) { toast(`Couldn't save your drawing: ${e.message}`, true); }
}
const saveDrawingsSoon = debounce(saveDrawings, 700);

function renderDrawBar() {
  const has = !!(state.game && currentDrawing() && (currentDrawing().arrows.length || currentDrawing().circles.length));
  $("btnClearDrawing").classList.toggle("hidden", !has);
  $("btnDraw").classList.toggle("active", state.draw.mode);
  $("btnDraw").setAttribute("aria-pressed", state.draw.mode);
  $("drawSwatches").classList.toggle("hidden", !state.draw.mode);
  $("boardHolder").classList.toggle("drawing", state.draw.mode);
}

function startDrawing(e, point) {
  const sq = squareAt(point.clientX, point.clientY);
  if (!sq || !state.game) return false;
  // Shift+click would otherwise extend a text selection (and scroll the page) mid-drag.
  document.getSelection()?.removeAllRanges();
  document.body.classList.add("no-select");
  drawDrag = { from: sq, to: sq, colour: penFor(e) };
  redrawOverlay();
  return true;
}
document.addEventListener("selectstart", (e) => { if (drawDrag) e.preventDefault(); });
function moveDrawing(point) {
  if (!drawDrag) return;
  const sq = squareAt(point.clientX, point.clientY);
  if (sq && sq !== drawDrag.to) { drawDrag.to = sq; redrawOverlay(); }
}

// Capture phase, so chessboard.js never sees a drawing gesture as a piece drag.
$("boardHolder").addEventListener("mousedown", (e) => {
  const drawing = e.button === 2 || (e.button === 0 && state.draw.mode);
  if (!drawing) {
    if (e.button === 0 && !state.practice) clearDrawingOnClick = true;
    return;
  }
  e.preventDefault(); e.stopPropagation();
  startDrawing(e, e);
}, true);
let clearDrawingOnClick = false;
$("boardHolder").addEventListener("click", () => {
  // A plain left click on the board clears the drawing on this position (as on Lichess).
  if (clearDrawingOnClick && !state.draw.mode) clearDrawing();
  clearDrawingOnClick = false;
});
window.addEventListener("mousemove", (e) => moveDrawing(e));
window.addEventListener("mouseup", () => { if (drawDrag) finishDrawing(); });
$("boardHolder").addEventListener("contextmenu", (e) => e.preventDefault());
$("boardHolder").addEventListener("touchstart", (e) => {
  if (!state.draw.mode || e.touches.length !== 1) return;
  e.preventDefault(); e.stopPropagation();
  startDrawing(e, e.touches[0]);
}, { capture: true, passive: false });
$("boardHolder").addEventListener("touchmove", (e) => {
  if (!drawDrag) return;
  e.preventDefault();
  moveDrawing(e.touches[0]);
}, { passive: false });
$("boardHolder").addEventListener("touchend", () => { if (drawDrag) finishDrawing(); });

$("btnDraw").addEventListener("click", () => { state.draw.mode = !state.draw.mode; renderDrawBar(); });
$("btnClearDrawing").addEventListener("click", clearDrawing);
$("drawSwatches").addEventListener("click", (e) => {
  const b = e.target.closest("[data-colour]");
  if (!b) return;
  state.draw.colour = b.dataset.colour;
  document.querySelectorAll("#drawSwatches [data-colour]").forEach((x) => {
    x.classList.toggle("active", x === b);
    x.setAttribute("aria-checked", x === b);
  });
});

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
  if (state.game?.mode === "game" && state.cur >= 0 && !state.practice && !state.explore) setEvalBar(winOf(state.game.moves[state.cur]), evalWords(state.game.moves[state.cur].eval));
}

/* ================================================================ moves */

function goTo(i, rerender = true) {
  const g = state.game;
  if (!g || g.mode !== "game" || !g.moves.length) return;
  ensureBoard();
  exitExplore(false);
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
  if (state.view.bestArrow && m.best_from && m.cls !== "best" && m.best !== m.san && !isSpoiler(state.cur)) {
    arrows.push({ from: m.best_from, to: m.best_to, colour: "#1f9d6b" });
  }
  const squares = state.view.highlight
    ? [{ sq: m.from, colour: "rgba(255, 214, 10, .38)" }, { sq: m.to, colour: "rgba(255, 214, 10, .55)" }] : [];
  drawOverlay({ arrows, squares });
  setEvalBar(winOf(m), evalWords(m.eval));
  renderMoveList();
  renderExplain(m);
  renderGraph();
  renderTrainBar();
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

/** The game as a running commentary: every move with the commentator's line, grouped into
 * chapters when the coach wrote them (otherwise by game phase). Every row has the same columns
 * (number · side · move · verdict · text), so White's and Black's moves line up. */
function renderStory() {
  const g = state.game, box = $("storyList");
  if (!g || g.mode !== "game") return;
  const hasFlow = g.moves.some((m) => m.flow);
  let html = hasFlow || !g.moves.length ? "" : `<p class="small story-note">This game was analysed without running commentary, `
    + `so the story shows the coach's notes only. Choose <b>Commentary</b> in Settings → Analysis for a line on every move.</p>`;
  const chapters = new Map((g.chapters || []).map((c, k) => [c.start, { ...c, k }]));
  let phase = "";
  g.moves.forEach((m, i) => {
    const ch = chapters.get(i);
    if (ch) {
      html += `<div class="story-chapter" data-i="${i}" title="Go to the start of this chapter">`
        + `<div class="ch-head"><span>Chapter ${ch.k + 1}</span><span>moves ${esc(ch.range)}</span></div>`
        + `<div class="ch-title">${esc(ch.title)}</div><p>${esc(ch.summary)}</p></div>`;
    } else if (!chapters.size && m.phase && m.phase !== phase) {
      phase = m.phase;
      html += `<div class="story-phase">${esc(cap(phase))}</div>`;
    }
    const white = m.side === "White";
    const hidden = isSpoiler(i);
    const text = hidden ? "" : m.flow || (m.expl ? m.expl.split(/(?<=[.!?])\s/)[0] : "");
    const mark = ERRORS.includes(m.cls) ? badge(m.cls) : m.critical ? `<span class="crit" title="Critical moment">⚡</span>` : "";
    html += `<div class="story-row${i === state.cur ? " sel" : ""}" data-i="${i}">`
      + `<span class="s-num">${white ? `${m.n}.` : `${m.n}…`}</span>`
      + `<span class="s-side ${white ? "w" : "b"}" title="${m.side}"></span>`
      + `<span class="s-san">${esc(m.san)}</span><span class="s-mark">${mark}</span>`
      + `<span class="story-text">${hidden ? `<span class="muted">🎯 Your ${esc(m.cls)} — try to find the better move first.</span>`
        : text ? esc(text) : `<span class="muted">${m.book ? "Opening theory." : "—"}</span>`}</span></div>`;
  });
  if (g.jobId && g.moves.length < g.total) html += `<div class="story-row pending">analysing… ${g.moves.length}/${g.total}</div>`;
  box.innerHTML = html;
  const sel = box.querySelector(".story-row.sel");
  if (sel) sel.scrollIntoView({ block: "nearest" });
}
$("storyList").addEventListener("click", (e) => {
  const row = e.target.closest(".story-row[data-i], .story-chapter[data-i]");
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
  const hidden = isSpoiler(state.cur);
  const tags = (m.tags || []).filter((t) => !hidden || !["missed threat", "left book"].includes(t));
  if (tags.length) html += `<div class="row" style="margin-bottom:10px">${tags.map((t) => `<span class="chip tag">${esc(t)}</span>`).join("")}</div>`;
  if (hidden) {
    state.previewMap = {};
    $("explain").innerHTML = html + `<div class="spoiler">
      <b>🎯 Can you find a better move?</b>
      <p class="small">The coach's note and the better move are hidden so you can try first. Play your move on the board and the engine will judge it.</p>
      <div class="row"><button data-practice="${state.cur}">Find a better move</button>
        <button class="secondary" data-reveal="${state.cur}">Show the answer</button></div>
      <p class="small" style="margin:8px 0 0">You can switch this off under 👁 Board, below the board.</p></div>`;
    return;
  }
  const opp = m.side === "White" ? "Black" : "White";
  if (m.threat) {
    html += `<div class="fact warn">⚠ <b>Missed threat.</b> Before this move, ${opp} was already threatening ${esc(m.threat)}, `
      + `and that is exactly how ${m.side}'s move gets punished.</div>`;
  }
  if (m.left_book) html += `<div class="fact book">📖 This move ${esc(m.left_book)}.</div>`;
  if (m.flow) html += `<div class="flow"><span class="flow-label">🎙 Commentary</span>${esc(m.flow)}</div>`;
  if (!opponent && ERRORS.includes(m.cls) && !g.jobId) {
    html += `<div class="row" style="margin:4px 0 10px"><button class="secondary sm" data-practice="${state.cur}">🎯 Practise this position</button></div>`;
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
  const xm = e.target.closest("[data-xmove]");
  if (xm && state.explore) { if (exploreMove(xm.dataset.xmove)) exploreUpdate(true); return; }
  const el = e.target.closest("[data-prev]");
  if (el) { startPreview(el.dataset.prev); return; }
  const pr = e.target.closest("[data-practice]");
  if (pr) { startPractice(parseInt(pr.dataset.practice, 10)); return; }
  const rv = e.target.closest("[data-reveal]");
  if (rv) { state.revealed.add(parseInt(rv.dataset.reveal, 10)); goTo(state.cur, false); return; }
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
  renderTrainBar();
  $("board").scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function practiceBoard() {
  const p = state.practice, g = state.game;
  board.position(p.result ? p.result.fen_after : p.fen, false);
  if (p.result) {
    const u = p.result.uci, colour = p.result.solved ? "rgba(31, 157, 107, .5)" : "rgba(224, 70, 75, .45)";
    drawOverlay({ arrows: [], squares: [{ sq: u.slice(0, 2), colour }, { sq: u.slice(2, 4), colour }] });
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
  const puzzle = currentPuzzle();
  const others = puzzle ? [] : myMistakes().filter((i) => i !== p.i);
  let html;
  if (puzzle) {
    const t = train.session;
    html = `<div class="expl-title"><span class="san">🎯 Puzzle ${t.pos + 1} of ${t.queue.length}</span>`
      + `<span class="small">From your game against <b>${esc(puzzle.opponent)}</b>${playedDate(puzzle) ? ` (${esc(fmtDate(playedDate(puzzle)))})` : ""}, `
      + `move ${m.n}. You played <b>${esc(label)} ${esc(m.san)}</b> ${badge(m.cls)} here. Find a better move.</span></div>`
      + `<div class="row puzzle-tags"><span class="chip imp-${importanceOf(puzzle)}" title="${esc(puzzle.why || "")}">`
      + `${IMPORTANCE[importanceOf(puzzle)].join(" ")}</span>`
      + themesOf(puzzle).map((th) => `<span class="chip">${esc(THEME_LABELS[th] || th)}</span>`).join("")
      + (p.result || p.showAnswer ? `<span class="small">${esc(puzzle.why || "")}</span>` : "") + `</div>`
      + (t.again.has(puzzle.id) && t.queue.indexOf(puzzle) !== t.pos ? `<div class="callout">🔁 Again: you missed this one earlier in the session.</div>` : "");
  } else {
    html = `<div class="expl-title"><span class="san">🎯 Practice · ${esc(label)}</span>`
      + `<span class="small">In the game you played <b>${esc(m.san)}</b> ${badge(m.cls)} — can you find something better?</span></div>`;
  }
  state.previewMap = {};
  if (p.busy) html += `<div class="callout">Checking your move with the engine…</div>`;
  else if (!p.result) {
    html += `<div class="callout">Drag a ${esc(m.side)} piece to play your move. Take your time: look for checks, captures and threats first. `
      + `(Pawns promote to a queen.)</div>`;
  } else if (p.result.cls === "unknown") {
    html += `<div class="callout bad"><b>✗ ${esc(p.result.san)} isn't one of the engine's top moves here,</b> so it's unlikely `
      + `to be the answer. (This shared copy was made without the engine, so it can only judge the engine's top choices.)</div>`;
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
  if (puzzle) {
    const last = train.session.pos + 1 >= train.session.queue.length;
    html += `<button class="${p.result?.solved || p.showAnswer ? "" : "secondary "}sm" data-pa="puzzle">${last ? "Finish ✓" : "Next puzzle →"}</button>`;
  }
  if (p.result || p.showAnswer) html += `<button class="secondary sm" data-pa="explore" title="Move the pieces yourself; the engine analyses (E)">🔍 Explore</button>`;
  html += `<div class="spacer"></div><button class="ghost sm" data-pa="exit">${puzzle ? "See the game" : "Exit practice"}</button></div>`;
  if (puzzle && (p.result?.solved || p.showAnswer) && m.expl) {
    html += `<div class="section-label">Coach's note on ${esc(m.san)}</div><div class="md">${md(m.expl)}</div>`;
  }
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
  gradePuzzle(r.solved);
  if (r.solved) { p.showAnswer = false; state.revealed.add(p.i); }
  practiceBoard();
  renderPractice();
}

function practiceAction(action) {
  const p = state.practice;
  if (!p) return;
  if (action === "retry") { p.result = null; practiceBoard(); renderPractice(); }
  else if (action === "answer") { gradePuzzle(false); p.showAnswer = true; state.revealed.add(p.i); renderPractice(); }
  else if (action === "puzzle") nextPuzzle();
  else if (action === "explore") startExplore();
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
$("practiseAll").addEventListener("click", () => {
  const list = myMistakes();
  if (!list.length) return;
  startPractice(list.find((i) => i >= state.cur) ?? list[0]);
});

/* ================================================================ explore: move the pieces, the engine follows */

/** Full FEN of what the board shows now: a game move, a previewed line, practice, or a board-editor position. */
function boardFen() {
  const g = state.game;
  if (!g) return "";
  if (state.preview) { const p = state.preview; return p.idx >= 0 ? p.steps[p.idx].fen : p.base; }
  if (state.practice) { const p = state.practice; return p.result ? p.result.fen_after : p.fen; }
  if (g.mode === "position") return g.position.fen;
  if (state.cur >= 0) return g.moves[state.cur].fen_after;
  return g.moves[0]?.fen_before || "";
}

function startExplore() {
  if (!state.game || state.page !== "game") return;
  if (!hasChess) { toast("Explore isn't available in this page.", true); return; }
  const p = state.practice;
  if (p && !p.result && !p.showAnswer) { toast("Try the position first: Explore shows the engine's best move."); return; }
  const fen = boardFen();
  let chess;
  try { chess = new Chess(fen); } catch { toast("Explore can't set up this position.", true); return; }
  state.preview = null;
  $("previewBar").classList.add("hidden");
  state.explore = { start: fen, chess, redo: [], lines: [], token: 0, error: "" };
  $("exploreBar").classList.remove("hidden");
  $("btnExplore").classList.add("active");
  $("btnExplore").setAttribute("aria-pressed", "true");
  exploreUpdate(false);
}

function exitExplore(render = true) {
  if (!state.explore) return;
  state.explore = null;
  Engine.stop();
  $("exploreBar").classList.add("hidden");
  $("btnExplore").classList.remove("active");
  $("btnExplore").setAttribute("aria-pressed", "false");
  if (!render) return;
  if (state.practice) { practiceBoard(); renderPractice(); }
  else if (state.game?.mode === "position") renderPosition();
  else goTo(state.cur, false);
}

/** The position changed (a move, undo, redo, reset): show it and let the engine analyse it. */
function exploreUpdate(animate = true, moveBoard = true) {
  const x = state.explore;
  const fen = x.chess.fen();
  if (moveBoard) board.position(fen, animate);
  x.lines = []; x.error = ""; x.done = false;
  const token = ++x.token;
  const current = () => state.explore === x && x.token === token;
  renderExplore();
  if (x.chess.isGameOver() || !Engine.available()) return;
  Engine.search(fen, {
    multipv: 3, depth: 20, interrupt: true, stale: () => !current(),
    onInfo: (lines) => {
      if (!current()) return;
      x.lines = lines;
      if (!x.renderTimer) x.renderTimer = setTimeout(() => { x.renderTimer = null; if (current()) renderExplore(); }, 200);
    },
  }).then((res) => {
    if (!res || !current()) return;
    x.lines = res.lines; x.done = true;
    renderExplore();
  }).catch((e) => { if (current()) { x.error = e.message; renderExplore(); } });
}

function exploreMove(uci) {
  const x = state.explore;
  try { x.chess.move({ from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci[4] }); } catch { return false; }
  x.redo = [];
  return true;
}

function exploreStep(dir) {
  const x = state.explore;
  if (!x) return;
  if (dir < 0) { const mv = x.chess.undo(); if (!mv) return; x.redo.push(mv.lan || mv.from + mv.to + (mv.promotion || "")); }
  else { const u = x.redo.pop(); if (!u) return; x.chess.move({ from: u.slice(0, 2), to: u.slice(2, 4), promotion: u[4] }); }
  exploreUpdate(true);
}

function exploreResult(c) {
  if (c.isCheckmate()) return c.turn() === "w" ? "0-1" : "1-0";
  if (c.isGameOver()) return "1/2-1/2";
  return "";
}

function renderExplore() {
  const x = state.explore;
  if (!x) return;
  const c = x.chess, fen = c.fen();
  const hist = c.history({ verbose: true });
  const over = exploreResult(c), top = x.lines[0];
  const ev = over || (top ? engineEval(top, fen) : "");
  setEvalBar(ev ? winFromEval(ev) : winFromEval(state.practice?.result?.eval || ""), ev ? evalWords(ev) : "");
  const arrows = [], squares = [];
  if (state.view.bestArrow && top?.pv?.[0] && !over) arrows.push({ from: top.pv[0].slice(0, 2), to: top.pv[0].slice(2, 4), colour: "#1f9d6b" });
  const last = hist[hist.length - 1];
  if (last && state.view.highlight) squares.push({ sq: last.from, colour: "rgba(255, 214, 10, .38)" }, { sq: last.to, colour: "rgba(255, 214, 10, .55)" });
  drawOverlay({ arrows, squares });
  $("explorePos").textContent = hist.length ? `· ${hist.length} move${hist.length === 1 ? "" : "s"} from where you started` : "";
  $("exploreUndo").disabled = !hist.length;
  $("exploreRedo").disabled = !x.redo.length;
  $("exploreReset").disabled = !hist.length;

  const turn = c.turn() === "w" ? "White" : "Black";
  let html = `<div class="expl-title"><span class="san">🔍 Explore</span>`
    + `<span class="small">${over ? "Game over" : `${turn} to move`}</span></div>`;
  html += `<div class="explore-path">${hist.length
    ? `<span class="small">Your line:</span> <b>${esc(stepsText(x.start, hist))}</b>`
    : `<span class="small">Drag a piece to try a move. Take moves back with ← and replay them with →.</span>`}</div>`;
  if (over) {
    html += `<div class="callout">${c.isCheckmate() ? `Checkmate: ${turn === "White" ? "Black" : "White"} wins.`
      : c.isStalemate() ? "Stalemate: a draw." : "A draw."}</div>`;
  } else if (!Engine.available()) {
    html += `<div class="callout">${Engine.error() ? esc(Engine.error())
      : "This copy was made without the engine, so there's no evaluation. You can still move the pieces to try your ideas."}</div>`;
  } else if (x.error) {
    html += `<div class="callout bad">${esc(x.error)}</div>`;
  } else if (!x.lines.length) {
    html += `<div class="callout">⏳ The engine is thinking…</div>`;
  } else {
    html += `<div class="section-label">Engine · depth ${top.depth}${x.done ? "" : " …"} · click a line to play its first move</div>`;
    x.lines.forEach((ln, k) => {
      const steps = uciSteps(fen, ln.pv, 10);
      if (!steps.length) return;
      html += `<div class="line clickable" data-xmove="${esc(ln.pv[0])}" title="Play ${esc(steps[0].san)}">`
        + `<b>${esc(steps[0].san)}</b><span class="ev">${esc(engineEval(ln, fen))}</span>`
        + `<span class="pv">${esc(stepsText(fen, steps))}</span></div>`;
    });
  }
  $("explain").innerHTML = html;
}

$("btnExplore").addEventListener("click", () => (state.explore ? exitExplore() : startExplore()));
$("exploreExit").addEventListener("click", () => exitExplore());
$("exploreUndo").addEventListener("click", () => exploreStep(-1));
$("exploreRedo").addEventListener("click", () => exploreStep(1));
$("exploreReset").addEventListener("click", () => {
  const x = state.explore;
  if (!x) return;
  x.chess = new Chess(x.start); x.redo = [];
  exploreUpdate(true);
});

/* ================================================================ train: your mistakes, with spaced repetition */

/* Leitner system: a solved puzzle moves up a level and comes back after TRAIN_DAYS[level] days; a missed one
 * drops to level 1, comes back at the end of the session (for practice) and again the next day. */
const TRAIN_DAYS = [0, 1, 3, 7, 16, 35];
const DAY_MS = 86400000;
const train = { items: null, session: null, summary: null };

function trainKey() {
  return EXPORT ? `lucidfish-train-shared-${EXPORT.profile.id}-${EXPORT.profile.name}` : `lucidfish-train-${state.profile?.id || 0}`;
}
function trainRecords() {
  try { return JSON.parse(localStorage.getItem(trainKey()) || "{}") || {}; } catch { return train.memory || {}; }
}
function saveTrainRecords(rec) {
  train.memory = rec;   // private browsing: progress lasts until the page closes
  try { localStorage.setItem(trainKey(), JSON.stringify(rec)); } catch { /* see above */ }
}

/* ---------------------------------------------------------------- back up / move your progress */

/* Progress lives in this browser. "Save my progress" downloads it as a small file (puzzle levels and due dates,
 * today's count, Train settings, and in a shared copy your drawings); "Load progress" merges such a file back,
 * e.g. on another computer or browser. For each puzzle the most recently practised version wins, so an old
 * backup never undoes newer practice. */
const DRAWINGS_PREFIX = "lucidfish-shared-drawings-";

function progressFile() {
  const read = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key) || "null") ?? fallback; } catch { return fallback; } };
  const drawings = {};
  if (EXPORT) {
    for (const g of EXPORT.games) {
      const d = read(`${DRAWINGS_PREFIX}${g.fingerprint || g.id}`, null);
      if (d && Object.keys(d).length) drawings[g.fingerprint || g.id] = d;
    }
  }
  const p = EXPORT ? EXPORT.profile : state.profile;
  return { lucidfish: "progress", version: 1, saved: new Date().toISOString(), profile: { id: p?.id, name: p?.name || "" },
    records: trainRecords(), newToday: read(`${trainKey()}-new`, {}), settings: read("lucidfish-train-settings", null),
    drawings };
}

function saveProgress() {
  const data = progressFile();
  const n = Object.keys(data.records).length;
  const name = (data.profile.name || "profile").replace(/[^\w .-]+/g, "").trim() || "profile";
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 1)], { type: "application/json" }));
  a.download = `Lucidfish progress - ${name} - ${new Date().toISOString().slice(0, 10)}.json`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  toast(`Saved your progress on ${n} puzzle${n === 1 ? "" : "s"}.`);
}

const validRecord = (r) => r && Number.isInteger(r.box) && r.box >= 0 && r.box <= 5 && Number.isFinite(r.due)
  && Number.isInteger(r.seen) && Number.isInteger(r.right);

/** Merge a saved progress file into this browser's. Returns what happened, for the message. */
function mergeProgress(data) {
  if (!data || data.lucidfish !== "progress" || typeof data.records !== "object") throw new Error("That isn't a Lucidfish progress file.");
  const rec = trainRecords();
  let added = 0, updated = 0, kept = 0;
  for (const [id, r] of Object.entries(data.records)) {
    if (!validRecord(r)) continue;
    const mine = rec[id];
    if (!mine) { rec[id] = r; added += 1; }
    else if ((r.last || 0) > (mine.last || 0)) { rec[id] = r; updated += 1; }
    else kept += 1;
  }
  saveTrainRecords(rec);
  try {
    const today = new Date().toDateString(), theirs = data.newToday || {};
    if (theirs.date === today && Number.isInteger(theirs.count) && theirs.count > newToday()) {
      localStorage.setItem(`${trainKey()}-new`, JSON.stringify({ date: today, count: theirs.count }));
    }
    if (data.settings && !localStorage.getItem("lucidfish-train-settings")) saveTrainSettings(data.settings);
    if (EXPORT) {
      for (const [game, positions] of Object.entries(data.drawings || {})) {
        const key = `${DRAWINGS_PREFIX}${game}`;
        const mine = JSON.parse(localStorage.getItem(key) || "{}");
        localStorage.setItem(key, JSON.stringify({ ...positions, ...mine }));   // yours win where both drew
      }
    }
  } catch { /* storage unavailable: the puzzle progress above still applies for this visit */ }
  return { added, updated, kept };
}

async function loadProgress(file) {
  let data;
  try { data = JSON.parse(await file.text()); } catch { toast("That file couldn't be read as a progress file.", true); return; }
  const here = (EXPORT ? EXPORT.profile : state.profile)?.name || "";
  if (data?.profile?.name && here && data.profile.name !== here
      && !confirm(`This progress was saved for "${data.profile.name}". Load it into "${here}" anyway?`)) return;
  let r;
  try { r = mergeProgress(data); } catch (e) { toast(e.message, true); return; }
  toast(`Loaded: ${r.added} new puzzle${r.added === 1 ? "" : "s"}, ${r.updated} updated`
    + (r.kept ? `, ${r.kept} kept as they were (practised more recently here)` : "") + ".");
  renderTrainPage();
}

$("trainSaveProgress").addEventListener("click", saveProgress);
$("trainLoadFile").addEventListener("change", (e) => {
  const f = e.target.files?.[0];
  e.target.value = "";
  if (f) loadProgress(f);
});

async function loadTrainItems() {
  try { train.items = (await api("/api/train")).items || []; } catch { train.items = train.items || []; }
  return train.items;
}

/* What to train on. Defaults keep it manageable: key and costly errors only, no bullet games, nothing played in
 * time trouble (that's the clock, not a gap in knowledge), and at most 10 new puzzles a day. */
const TRAIN_DEFAULTS = { level: 2, perDay: 10, hurried: false, bullet: false, theme: "" };
const IMPORTANCE = { 3: ["★★★", "Key"], 2: ["★★", "Costly"], 1: ["★", "Minor"] };
const THEME_LABELS = {
  missed_threat: "Missed threats", hanging: "Hanging material", missed_tactic: "Missed tactics",
  missed_mate: "Missed mates", allowed_mate: "Allowed mates", king_attack: "King safety",
  "phase:opening": "Opening", "phase:middlegame": "Middlegame", "phase:endgame": "Endgame",
};
const importanceOf = (it) => it.importance ?? (it.cls === "inaccuracy" ? 1 : 2);   // copies made before levels
const themesOf = (it) => [...(it.themes || []), ...(it.phase ? [`phase:${it.phase}`] : [])];

function trainSettings() {
  try { return { ...TRAIN_DEFAULTS, ...JSON.parse(localStorage.getItem("lucidfish-train-settings") || "{}") }; }
  catch { return { ...TRAIN_DEFAULTS, ...(train.settings || {}) }; }
}
function saveTrainSettings(changes) {
  train.settings = { ...trainSettings(), ...changes };
  try { localStorage.setItem("lucidfish-train-settings", JSON.stringify(train.settings)); } catch { /* this visit */ }
}

/** New puzzles started today (the daily limit), kept per profile like the progress. */
function newToday() {
  const today = new Date().toDateString();
  try {
    const m = JSON.parse(localStorage.getItem(`${trainKey()}-new`) || "{}");
    return m.date === today ? m.count : 0;
  } catch { return train.newToday || 0; }
}
function countNewToday() {
  const today = new Date().toDateString(), n = newToday() + 1;
  train.newToday = n;
  try { localStorage.setItem(`${trainKey()}-new`, JSON.stringify({ date: today, count: n })); } catch { /* this visit */ }
}
function newLeftToday() {
  const per = trainSettings().perDay;
  return per > 0 ? Math.max(0, per - newToday()) : Infinity;
}

/** The puzzles that pass every filter except the theme (the theme chips count within this). */
function trainBase() {
  const s = trainSettings();
  return (train.items || []).filter((it) => importanceOf(it) >= s.level
    && (s.hurried || !it.hurried)
    && (state.tc ? it.time_class === state.tc : s.bullet || it.time_class !== "bullet"));
}
function trainPool() {
  const theme = trainSettings().theme;
  return trainBase().filter((it) => !theme || themesOf(it).includes(theme));
}

function trainCounts(pool, rec, now = Date.now()) {
  const c = { total: pool.length, due: 0, fresh: 0, learning: 0, reviewing: 0, mastered: 0, nextDue: Infinity };
  for (const it of pool) {
    const r = rec[it.id];
    if (!r) { c.fresh += 1; continue; }
    if (r.due <= now) c.due += 1; else c.nextDue = Math.min(c.nextDue, r.due);
    if (r.box >= 5) c.mastered += 1; else if (r.box >= 3) c.reviewing += 1; else c.learning += 1;
  }
  c.newToday = Math.min(c.fresh, newLeftToday());
  c.todo = c.due + c.newToday;
  return c;
}

function whenText(ts) {
  const days = Math.round((ts - Date.now()) / DAY_MS);
  return days <= 0 ? "later today" : days === 1 ? "tomorrow" : `in ${days} days`;
}

async function showTrainPage() {
  if (!train.items) $("trainLevels").innerHTML = `<div class="small">Loading your puzzles…</div>`;
  else renderTrainPage();   // show what we have while the list refreshes
  await loadTrainItems();
  renderTrainPage();
}

function renderTrainControls() {
  const s = trainSettings();
  $("trainFilter").value = String(s.level);
  $("trainPerDay").value = String(s.perDay);
  $("trainHurried").checked = s.hurried;
  $("trainBullet").checked = s.bullet;
  $("trainBulletWrap").classList.toggle("hidden", !!state.tc || !(train.items || []).some((it) => it.time_class === "bullet"));
  const base = trainBase(), counts = {};
  for (const it of base) for (const t of themesOf(it)) counts[t] = (counts[t] || 0) + 1;
  const chip = (t, label, n) => `<button data-theme="${t}" class="${s.theme === t ? "active" : ""}" aria-pressed="${s.theme === t}">`
    + `${esc(label)} <span class="n">${n}</span></button>`;
  const themes = Object.keys(THEME_LABELS).filter((t) => counts[t] || t === s.theme);
  $("trainThemes").innerHTML = base.length && themes.length
    ? chip("", "All themes", base.length) + themes.map((t) => chip(t, THEME_LABELS[t], counts[t] || 0)).join("") : "";
}

function renderTrainPage() {
  renderTrainControls();
  const pool = trainPool(), rec = trainRecords(), c = trainCounts(pool, rec);
  const tile = (cls, n, label, hint) => `<div class="stat tl-${cls}" title="${esc(hint)}"><b>${n}</b><span>${label}</span></div>`;
  $("trainLevels").innerHTML = pool.length
    ? `<div class="stats train-stats">${tile("due", c.todo, "to do today", "Puzzles due for review, plus today's new ones")}`
      + tile("new", c.fresh, "not started", "Never tried; a few are added each day")
      + tile("learning", c.learning, "learning", "Levels 1–2: missed recently or solved once or twice")
      + tile("reviewing", c.reviewing, "reviewing", "Levels 3–4: solved several times")
      + tile("mastered", c.mastered, "mastered", "Level 5: solved every time; comes back every 35 days") + `</div>`
      + `<div class="tl-bar" aria-hidden="true">${["new", "learning", "reviewing", "mastered"].map((k) =>
        `<div class="tl-${k}" style="flex:${c[k === "new" ? "fresh" : k]}"></div>`).join("")}</div>`
    : `<div class="empty small">${(train.items || []).length
      ? "No puzzles match these settings. Try “Everything”, another time control or theme, or include time trouble."
      : "No puzzles yet. They come from analysed games in which Lucidfish knows which side you played."}</div>`;
  $("trainStart").disabled = !pool.length;
  $("trainStart").textContent = c.todo || !pool.length ? "▶ Start training" : "▶ Practise anyway";
  const waiting = c.fresh - c.newToday;
  $("trainNote").textContent = !pool.length ? ""
    : c.todo ? `${pool.length} puzzle${pool.length === 1 ? "" : "s"} · ${c.due} due for review · ${c.newToday} new today`
      + (waiting > 0 ? ` (${waiting} more new ones wait for the next days).` : ".")
      : c.fresh ? `Done for today: ${c.fresh} new puzzle${c.fresh === 1 ? "" : "s"} wait for tomorrow (or raise “New per day”). `
        + "You can still practise the ones you know least."
        : `All caught up! The next puzzle is due ${whenText(c.nextDue)}. You can still practise the ones you know least.`;
  const s = train.summary;
  $("trainDone").classList.toggle("hidden", !s);
  if (s) {
    $("trainDone").innerHTML = `<div class="row"><b>${s.early ? "Session ended" : "Session complete"}</b>`
      + `<span class="chip good">✓ ${s.solved} solved first try</span>`
      + (s.missed ? `<span class="chip bad">✗ ${s.missed} missed</span>` : "")
      + `<div class="spacer"></div><button class="ghost sm" id="trainDoneClose" aria-label="Dismiss">✕</button></div>`
      + `<p class="small" style="margin:8px 0 0">${s.missed ? "The ones you missed come back tomorrow. " : ""}`
      + `${s.solved ? "The ones you solved come back later, a little further apart each time." : ""}</p>`;
    $("trainDoneClose").addEventListener("click", () => { train.summary = null; renderTrainPage(); });
  }
  renderGoTrain(c);
}

/** The dashboard button, with how many puzzles are waiting today. */
function renderGoTrain(c) {
  const all = train.items || [];
  if (!c) c = trainCounts(trainPool(), trainRecords());
  $("goTrain").classList.toggle("hidden", !all.length);
  $("goTrain").textContent = c.todo ? `🎯 Train · ${c.todo} to do` : "🎯 Train";
}

function startTraining() {
  const pool = trainPool(), rec = trainRecords(), now = Date.now();
  const size = Number($("trainSize").value) || 10;
  const byImportance = (a, b) => importanceOf(b) - importanceOf(a);
  const due = pool.filter((it) => rec[it.id] && rec[it.id].due <= now)
    .sort((a, b) => rec[a.id].box - rec[b.id].box || byImportance(a, b) || rec[a.id].due - rec[b.id].due);
  const fresh = pool.filter((it) => !rec[it.id]).sort((a, b) => byImportance(a, b) || b.loss - a.loss)
    .slice(0, newLeftToday());                      // the most important first, a few a day
  let queue = [...due, ...fresh].slice(0, size);
  const extra = !queue.length;   // nothing due: practise the least-known puzzles, without moving them up
  if (extra) {
    queue = pool.filter((it) => rec[it.id]).sort((a, b) => (rec[a.id].box - rec[b.id].box) || byImportance(a, b))
      .slice(0, size);
  }
  if (!queue.length) return;
  train.summary = null;
  train.session = { queue, pos: 0, graded: {}, again: new Set(), solved: 0, missed: 0, extra };
  openPuzzle();
}

$("trainFilter").addEventListener("change", (e) => { saveTrainSettings({ level: Number(e.target.value) }); renderTrainPage(); });
$("trainPerDay").addEventListener("change", (e) => { saveTrainSettings({ perDay: Number(e.target.value) }); renderTrainPage(); });
$("trainHurried").addEventListener("change", (e) => { saveTrainSettings({ hurried: e.target.checked }); renderTrainPage(); });
$("trainBullet").addEventListener("change", (e) => { saveTrainSettings({ bullet: e.target.checked }); renderTrainPage(); });
$("trainThemes").addEventListener("click", (e) => {
  const b = e.target.closest("[data-theme]");
  if (b) { saveTrainSettings({ theme: b.dataset.theme }); renderTrainPage(); }
});

function currentPuzzle() {
  const t = train.session, it = t?.queue[t.pos], p = state.practice;
  return it && p && state.game?.gameId === it.game_id && p.i === it.i ? it : null;
}

async function openPuzzle() {
  const t = train.session, it = t.queue[t.pos];
  if (state.game?.gameId !== it.game_id) await openStoredGame(it.game_id);
  else if (state.page !== "game") showPage("game");
  if (train.session !== t) return;
  const m = state.game?.gameId === it.game_id ? state.game.moves[it.i] : null;
  if (!m || m.fen_before !== it.fen) {   // the game was deleted or re-analysed since the list was made
    toast("That puzzle's game has changed; skipping it.");
    nextPuzzle();
    return;
  }
  startPractice(it.i);
}

function nextPuzzle() {
  const t = train.session;
  if (!t) return;
  t.pos += 1;
  if (t.pos >= t.queue.length) finishTraining(false);
  else openPuzzle();
}

function finishTraining(early) {
  const t = train.session;
  if (!t) return;
  train.session = null;
  train.summary = { solved: t.solved, missed: t.missed, early };
  renderTrainBar();
  if (state.practice) goTo(state.practice.i, false);
  showPage("train");
}

/** First answer to a puzzle in a session: move it up (solved) or back to the start (missed). */
function gradePuzzle(ok) {
  const t = train.session, it = currentPuzzle();
  if (!it || t.graded[t.pos] !== undefined) return;
  t.graded[t.pos] = ok;
  const repeat = t.again.has(it.id) && t.queue.indexOf(it) !== t.pos;   // the end-of-session repeat is practice only
  if (!repeat) {
    const rec = trainRecords(), now = Date.now();
    if (!rec[it.id] && !t.extra) countNewToday();
    const r = rec[it.id] || { box: 0, seen: 0, right: 0, due: now };
    r.seen += 1; r.last = now;
    if (ok) {
      t.solved += 1; r.right += 1;
      if (!t.extra) { r.box = Math.min(5, r.box + 1); r.due = now + TRAIN_DAYS[r.box] * DAY_MS; }
    } else {
      t.missed += 1; r.box = 1; r.due = now + DAY_MS;
    }
    rec[it.id] = r;
    saveTrainRecords(rec);
  }
  if (!ok && !t.again.has(it.id)) { t.again.add(it.id); t.queue.push(it); }
  renderTrainBar();
}

function renderTrainBar() {
  const t = train.session;
  $("trainBar").classList.toggle("hidden", !t);
  if (!t) return;
  const answered = Object.keys(t.graded).length;
  $("trainBarLabel").textContent = `🎯 Puzzle ${Math.min(t.pos + 1, t.queue.length)} of ${t.queue.length}`;
  $("trainBarFill").style.width = `${Math.round((answered / t.queue.length) * 100)}%`;
  $("trainBarScore").textContent = `✓ ${t.solved} · ✗ ${t.missed}`;
  $("trainBack").classList.toggle("hidden", !!currentPuzzle());
  $("trainNext").textContent = t.pos + 1 >= t.queue.length ? "Finish ✓" : "Next puzzle →";
}

$("trainStart").addEventListener("click", startTraining);
$("goTrain").addEventListener("click", () => showPage("train"));
$("trainNext").addEventListener("click", nextPuzzle);
$("trainEnd").addEventListener("click", () => finishTraining(true));
$("trainBack").addEventListener("click", () => { if (train.session) openPuzzle(); });

/* ================================================================ eval graph */

function renderGraph() {
  const svg = $("graph"), g = state.game;
  const show = g && g.mode === "game" && g.moves.length && state.view.graph;
  $("graphWrap").classList.toggle("hidden", !show);
  if (!show) return;
  const W = Math.max(200, svg.clientWidth || 400), H = 110;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const n = Math.max(g.total || 0, g.moves.length);
  const x = (i) => (i / n) * W, y = (w) => H - (w / 100) * H;
  let pts = `0,${y(50)}`;
  g.moves.forEach((m, i) => { pts += ` ${x(i + 1)},${y(winOf(m))}`; });
  const last = x(g.moves.length);
  const colours = { inaccuracy: "var(--c-inaccuracy)", mistake: "var(--c-mistake)", blunder: "var(--c-blunder)" };
  let marks = "";
  g.moves.forEach((m, i) => {
    const c = colours[m.cls] || (m.critical ? "var(--c-critical)" : null);
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
  drawOverlay({ arrows: s ? [{ from: s.from, to: s.to, colour: "#e2a400" }] : [], squares: [] });
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

$("edAnalyse").addEventListener("click", async () => {
  const typed = $("edFen").value.trim();
  const fen = typed && typed.split(/\s+/)[0] === editorBoard.fen() ? typed : editorFen();
  const btn = $("edAnalyse");
  btn.disabled = true; btn.textContent = "Analysing…";
  let d;
  try { d = await api("/api/position", { method: "POST", body: { fen, perspective: $("edPersp").value, level: state.profile?.level || null } }); }
  catch (e) { toast(e.message, true); return; }
  finally { btn.disabled = false; btn.textContent = "Analyse position"; }
  clearTimeout(pollTimer);
  state.game = { mode: "position", position: d, moves: [], warnings: d.warnings || [], headers: {}, annotations: {} };
  resetGameView();
  showPage("game");
  board.orientation($("edPersp").value);
  renderHeader(); renderReview(); renderPosition();
});

/** Board-editor mode: arrow for the engine's top move. */
/* ================================================================ board display options */

$("viewHideAnswers").addEventListener("change", (e) => {
  try { localStorage.setItem("lucidfish-hide-answers", e.target.checked ? "1" : "0"); } catch { /* ignore */ }
  if (state.page === "game" && state.game?.mode === "game" && state.cur >= 0) goTo(state.cur, false);
});

function applyView() {
  $("viewHideAnswers").checked = hideAnswers();
  $("evalbar").classList.toggle("off", !state.view.evalBar);
  $("evalLabel").classList.toggle("hidden", !state.view.evalBar);
  document.querySelectorAll("[data-view-opt]").forEach((cb) => { cb.checked = !!state.view[cb.dataset.viewOpt]; });
  try { localStorage.setItem("lucidfish-view", JSON.stringify(state.view)); } catch { /* ignore */ }
  renderGraph();
  if (state.page === "game") refreshBoard();
}
$("btnView").addEventListener("click", (e) => {
  e.stopPropagation();
  const menu = $("viewMenu"), open = menu.classList.contains("hidden");
  menu.classList.toggle("hidden", !open);
  $("btnView").setAttribute("aria-expanded", open);
});
document.addEventListener("click", (e) => { if (!e.target.closest(".view-opts")) $("viewMenu").classList.add("hidden"); });
$("viewMenu").addEventListener("change", (e) => {
  const opt = e.target.dataset.viewOpt;
  if (opt) { state.view[opt] = e.target.checked; applyView(); }
});
applyView();

function positionOverlay() {
  const first = state.view.bestArrow ? state.game.position.lines[0]?.steps?.[0] : null;
  drawOverlay({ arrows: first ? [{ from: first.from, to: first.to, colour: "#1f9d6b" }] : [], squares: [] });
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
      accuracy: g.accuracy || {}, side: g.side || null, chapters: g.chapters || [], annotations: g.annotations || {} } });
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
  $("setEscalate").checked = s.escalate !== false;
  $("setExpertProvider").innerHTML = `<option value="">Off — the main model does everything</option>`
    + s.providers.map((p) => `<option value="${p.id}">${esc(p.label)} · ${p.local ? "on this computer" : "cloud"}</option>`).join("");
  $("setExpertProvider").value = s.expert_provider || "";
  $("setExpertModel").value = s.expert_model || "";
  $("testExpertResult").textContent = "";
  renderExpert();
  $("setPrefetch").checked = s.prefetch !== false;
  $("setAutoReview").checked = s.auto_review !== false;
  $("setHideAnswers").checked = hideAnswers();
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
  api("/api/cache").then((c) => { $("cacheInfo").textContent = `${c.engine_positions.toLocaleString(LOCALE)} engine positions · ${c.opening_positions.toLocaleString(LOCALE)} opening positions stored`; }).catch(() => {});
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
  $("expertFields").style.opacity = $("setLlmEnabled").checked ? "1" : ".45";
}

/** The optional second model: model list, its API key, and what using it means. */
function renderExpert() {
  const id = $("setExpertProvider").value, p = id ? spec(id) : null;
  $("expertModelField").classList.toggle("hidden", !p);
  $("expertMore").classList.toggle("hidden", !p);
  $("expertKeyBlock").classList.toggle("hidden", !p?.key_env);
  if (!p) return;
  $("setExpertModel").placeholder = p.default_model || "model name";
  $("expertModelList").innerHTML = p.models.map((m) => `<option value="${esc(m)}">`).join("");
  $("setExpertKey").value = "";
  $("expertKeyStatus").textContent = p.key.present ? `✓ Key saved (${p.key.hint})`
    : p.needs_key ? `No ${p.label} key saved yet.` : "Optional — only needed if your server requires one.";
  const main = spec(draft.provider);
  $("expertNote").innerHTML = p.local
    ? (main.local ? "Both models run on this computer, one after the other, so it needs memory for both. On a 16 GB Mac, "
      + "pick a second model only slightly bigger than the main one, or use a cloud model instead." : "The second model runs on this computer.")
    : `Only the evidence for your key moments and the review (moves, engine lines and position facts) is sent to ${esc(p.label)}. `
      + "The running commentary stays with your main model.";
}
$("setExpertProvider").addEventListener("change", () => { $("setExpertModel").value = ""; $("testExpertResult").textContent = ""; renderExpert(); });
$("saveExpertKey").addEventListener("click", async () => {
  const id = $("setExpertProvider").value, key = $("setExpertKey").value.trim();
  if (!id || !key) { toast("Paste a key first.", true); return; }
  try {
    const d = await api("/api/settings/key", { method: "POST", body: { provider: id, key } });
    spec(id).key = d.key; renderExpert(); toast(d.message);
    if (id === draft.provider) renderKeyStatus(d.key);
  } catch (e) { toast(e.message, true); }
});
$("testExpert").addEventListener("click", async () => {
  const id = $("setExpertProvider").value, out = $("testExpertResult"), btn = $("testExpert");
  if (!id) return;
  out.className = "small"; out.textContent = "Testing…";
  btn.disabled = true;
  try {
    const d = await api("/api/settings/test", { method: "POST", body: {
      provider: id, model: $("setExpertModel").value.trim() || null, key: $("setExpertKey").value.trim() || null } });
    out.className = "result-ok"; out.textContent = `✓ ${d.message}`;
  } catch (e) { out.className = "result-bad"; out.textContent = `✗ ${e.message}`; }
  btn.disabled = false;
});
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
    renderExpert();
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
    auto_review: $("setAutoReview").checked,
    expert_provider: $("setExpertProvider").value, expert_model: $("setExpertModel").value.trim(),
    escalate: $("setEscalate").checked,
    depth: $("setDepth").value, threads: $("setThreads").value, hash_mb: $("setHash").value,
    stockfish_path: $("setStockfish").value.trim(), concurrency: $("setConcurrency").value || null,
  };
  if (p.local) body.base_url = $("setBaseUrl").value.trim();
  try { await api("/api/settings", { method: "POST", body }); }
  catch (e) { $("settingsError").textContent = e.message; return; }
  try { localStorage.setItem("lucidfish-hide-answers", $("setHideAnswers").checked ? "1" : "0"); } catch { /* ignore */ }
  if (state.page === "game" && state.game?.mode === "game" && state.cur >= 0) goTo(state.cur, false);
  closeModal("settingsModal");
  state.dismissed.clear();
  toast("Settings saved.");
  loadHealth(true);
});

/* ================================================================ keyboard + resize */

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("accountMenu").classList.contains("hidden")) { toggleAccountMenu(false); return; }
  if (e.key === "Escape" && modalOpen()) { document.querySelectorAll(".modal-back").forEach((m) => closeModal(m.id)); return; }
  if (modalOpen() || state.page !== "game" || ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
  if (e.key === "Escape" && state.preview) { exitPreview(); return; }
  if (state.explore) {
    if (e.key === "Escape") { exitExplore(); return; }
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); exploreStep(e.key === "ArrowLeft" ? -1 : 1); return; }
  }
  if (e.key.toLowerCase() === "e" && !e.metaKey && !e.ctrlKey) { state.explore ? exitExplore() : startExplore(); return; }
  if (e.key === "ArrowLeft") { e.preventDefault(); state.preview ? stepPreview(-1) : goTo(state.cur - 1); }
  else if (e.key === "ArrowRight") { e.preventDefault(); state.preview ? stepPreview(1) : goTo(state.cur + 1); }
  else if (e.key === "Home") { e.preventDefault(); goTo(0); }
  else if (e.key === "End") { e.preventDefault(); goTo((state.game?.moves.length || 0) - 1); }
  else if (e.key.toLowerCase() === "f") flip();
  else if (e.key.toLowerCase() === "d") { state.draw.mode = !state.draw.mode; renderDrawBar(); }
  else if (e.key.toLowerCase() === "a") {
    state.view.bestArrow = !state.view.bestArrow;
    applyView();
    toast(state.view.bestArrow ? "Best-move arrow on." : "Best-move arrow off.");
  }
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
  if (EXPORT) return;   // a shared copy has no queue
  // Live queue progress (also picks up games restored from the last session).
  await pollQueue();
  if (queue.data?.restored && queue.data.paused) openQueue();
})();
