"use strict";

// ---------- helpers ----------
async function api(path, opts) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

const GB = 1e9;
function fmtBytes(b) {
  if (b == null) return "—";
  if (b >= GB) return (b / GB).toFixed(2) + " Go";
  if (b >= 1e6) return (b / 1e6).toFixed(0) + " Mo";
  return (b / 1e3).toFixed(0) + " Ko";
}
function fmtBitrate(bps) {
  if (!bps) return "—";
  return (bps / 1e6).toFixed(1) + " Mb/s";
}
function fmtDur(s) {
  if (!s) return "—";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}h${String(m).padStart(2, "0")}` : `${m} min`;
}
function fmtRes(w, h) { return w && h ? `${w}×${h}` : "—"; }
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

let toastTimer;
function toast(msg, bad) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 4000);
}

// ---------- state ----------
const state = {
  view: "scan",
  profiles: [],
  movies: [],
  series: [],
  openSeries: null,      // {slug, ...} detail
  jobs: [],
  queuePaused: false,    // global queue pause (no new jobs start)
  selMovies: new Set(),
  selEpisodes: new Set(), // episode file ids selected in the open series
  movieSort: { key: "score", dir: "desc" },
  seriesSort: { key: "est_gain_bytes", dir: "desc" },
  scan: { running: false, done: 0, total: 0, probed: 0, cached: 0, errors: 0 },
  codec: "X265",
  eight_bit: false,
  profile: "Light",
};

// ---------- websocket ----------
let ws;
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => setConn(true);
  ws.onclose = () => { setConn(false); setTimeout(connectWS, 2000); };
  ws.onmessage = (ev) => handleWS(JSON.parse(ev.data));
}
function setConn(on) {
  document.getElementById("ws-dot").className = "dot " + (on ? "online" : "offline");
  document.getElementById("ws-label").textContent = on ? "connecté" : "hors ligne";
}
function handleWS(m) {
  switch (m.type) {
    case "snapshot":
      state.jobs = m.jobs || [];
      if (m.scan) Object.assign(state.scan, m.scan);
      refreshQueueBadge();
      if (state.view === "queue") render();
      if (state.view === "scan") render();
      break;
    case "scan_progress":
      Object.assign(state.scan, m, { running: true });
      if (state.view === "scan") renderScanProgress();
      break;
    case "scan_done":
      state.scan.running = false;
      if (state.view === "scan") { renderScanProgress(); loadExcluded(); }
      reloadLibrary();
      toast("Scan terminé");
      break;
    case "job_progress":
      patchJob(m.job_id, { progress: m.progress, speed: m.speed, eta_s: m.eta_s, state: m.state });
      if (state.view === "queue") render();
      break;
    case "job_state":
      patchJob(m.job_id, { state: m.state, error_message: m.error });
      // Fetch fresh job for validation / sizes.
      fetchJobs();
      if (m.state === "DONE") loadStats();
      break;
    case "stats":
      setStats(m.total_gain_bytes, m.total_encodes_done);
      break;
    case "queue":
      if (typeof m.paused === "boolean") state.queuePaused = m.paused;
      refreshQueueBadge(m);
      if (state.view === "queue") render();
      break;
    case "media_updated":
      // A processed file was re-probed/re-scored -> refresh the library views.
      reloadLibrary();
      if (state.view === "series" && state.openSeries) {
        refreshSeriesDetail().then(() => { if (state.view === "series") renderSeriesDetail(); });
      }
      break;
  }
}
function patchJob(id, fields) {
  const j = state.jobs.find(j => j.id === id);
  if (j) Object.assign(j, fields);
}
async function fetchJobs() {
  try {
    const r = await api("/api/jobs");
    state.jobs = r.jobs;
    state.queuePaused = !!r.paused;
    refreshQueueBadge();
    if (state.view === "queue") render();
  } catch (_) {}
}
function setStats(totalGain, count) {
  const el = document.getElementById("gain-total");
  if (!el) return;
  if (!count) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  el.textContent = `${fmtBytes(totalGain)} économisés · ${count} réencodage${count > 1 ? "s" : ""}`;
}
async function loadStats() {
  try {
    const s = await api("/api/stats");
    setStats(s.total_gain_bytes, s.total_encodes_done);
  } catch (_) {}
}
function refreshQueueBadge(q) {
  const active = state.jobs.filter(j => !["DONE", "REJECTED", "CANCELLED", "FAILED"].includes(j.state)).length;
  const badge = document.getElementById("queue-badge");
  badge.textContent = active;
  badge.classList.toggle("hidden", active === 0);
}

// ---------- navigation ----------
document.querySelectorAll(".tab").forEach(btn => {
  btn.addEventListener("click", () => {
    state.view = btn.dataset.view;
    document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b === btn));
    render();
  });
});

// ---------- data loads ----------
function cp() { return `codec=${state.codec}&profile=${encodeURIComponent(state.profile)}`; }

async function loadMovies() {
  state.movies = (await api(`/api/movies?${cp()}`)).movies;
}
async function loadSeries() {
  state.series = (await api(`/api/series?${cp()}`)).series;
}
async function clearCache(kind) {
  const label = kind === "movies" ? "films" : "séries";
  if (!confirm(`Vider le cache ${label} ?\nLes données analysées seront supprimées. Les fichiers sur le disque ne sont PAS touchés ; un nouveau scan les régénère.`)) return;
  try {
    const r = await api(`/api/${kind}`, { method: "DELETE" });
    toast(`Cache ${label} vidé (${r.removed} entrée(s))`);
    if (kind === "movies") { state.selMovies = new Set(); await loadMovies(); renderMovies(); }
    else { state.openSeries = null; state.selEpisodes = new Set(); await loadSeries(); renderSeries(); }
  } catch (e) { toast(e.message, true); }
}
async function refreshSeriesDetail() {
  if (!state.openSeries) return;
  state.openSeries = await api(`/api/series/${encodeURIComponent(state.openSeries.slug)}?${cp()}`);
}
async function reloadLibrary() {
  try {
    await Promise.all([loadMovies(), loadSeries()]);
    if (["movies", "series"].includes(state.view)) render();
  } catch (e) { toast(e.message, true); }
}

// Re-fetch the current view's gains/order when codec or profile changes.
async function onEncodeParamsChanged() {
  try {
    if (state.view === "movies") { await loadMovies(); renderMovies(); }
    else if (state.view === "series") {
      if (state.openSeries) { await refreshSeriesDetail(); renderSeriesDetail(); }
      else { await loadSeries(); renderSeries(); }
    }
  } catch (e) { toast(e.message, true); }
}

// ---------- render dispatch ----------
const app = document.getElementById("app");
function render() {
  if (state.view === "scan") return renderScan();
  if (state.view === "movies") return renderMovies();
  if (state.view === "series") return renderSeries();
  if (state.view === "queue") return renderQueue();
  if (state.view === "logs") return renderLogs();
  if (state.view === "settings") return renderSettings();
}

// ---------- shared encode controls ----------
// Position-based quality descriptor (profiles are ordered quality -> compressed).
function profileTier(i, n) {
  if (i === 0) return "qualité max";
  if (i === n - 1) return "compression max";
  return "intermédiaire";
}
function encodeControls() {
  const n = state.profiles.length;
  const opts = state.profiles.map((p, i) =>
    `<option value="${esc(p.name)}" ${p.name === state.profile ? "selected" : ""}>${esc(p.name)} — ${profileTier(i, n)}</option>`).join("");
  return `
    <label class="field" style="margin:0">
      <span>Codec</span>
      <select id="sel-codec">
        <option value="X265" ${state.codec === "X265" && !state.eight_bit ? "selected" : ""}>HEVC x265 10-bit</option>
        <option value="X265-8" ${state.codec === "X265" && state.eight_bit ? "selected" : ""}>HEVC x265 8-bit — + compatible</option>
        <option value="SVTAV1" ${state.codec === "SVTAV1" && !state.eight_bit ? "selected" : ""}>AV1 (SVT) 10-bit</option>
        <option value="SVTAV1-8" ${state.codec === "SVTAV1" && state.eight_bit ? "selected" : ""}>AV1 (SVT) 8-bit — + compatible</option>
      </select>
    </label>
    <label class="field" style="margin:0">
      <span>Profil</span>
      <select id="sel-profile">${opts}</select>
    </label>`;
}
function bindEncodeControls() {
  const c = document.getElementById("sel-codec");
  const p = document.getElementById("sel-profile");
  if (c) c.addEventListener("change", () => {
    state.codec = c.value.startsWith("X265") ? "X265" : "SVTAV1";
    state.eight_bit = c.value.endsWith("-8");
    onEncodeParamsChanged();
  });
  if (p) p.addEventListener("change", () => { state.profile = p.value; onEncodeParamsChanged(); });
}

// ---------- SCAN ----------
function renderScan() {
  app.innerHTML = `
    <h2>Scanner la bibliothèque</h2>
    <div class="panel">
      <label class="field">
        <span>Chemin racine (les sous-dossiers sont parcourus récursivement — NAS supporté, ex. \\\\nas\\films)</span>
        <input type="text" id="scan-path" placeholder="D:\\Films  ou  \\\\nas\\media" />
      </label>
      <div class="row">
        <label class="muted"><input type="checkbox" id="scan-force" /> Forcer la ré-analyse (ignorer le cache)</label>
        <div class="spacer"></div>
        <button class="btn" id="scan-btn">Lancer le scan</button>
        <button class="btn ghost" id="scan-cancel">Annuler</button>
      </div>
    </div>
    <div class="panel" id="scan-prog"></div>
    <div class="panel" id="excluded-panel"></div>`;
  document.getElementById("scan-btn").addEventListener("click", startScan);
  document.getElementById("scan-cancel").addEventListener("click", () => api("/api/scan/cancel", { method: "POST" }));
  const saved = localStorage.getItem("vlo-path");
  if (saved) document.getElementById("scan-path").value = saved;
  renderScanProgress();
  loadExcluded();
}
async function startScan() {
  const path = document.getElementById("scan-path").value.trim();
  if (!path) return toast("Indique un chemin", true);
  localStorage.setItem("vlo-path", path);
  const force = document.getElementById("scan-force").checked;
  try {
    await api("/api/scan", { method: "POST", body: JSON.stringify({ root_path: path, force }) });
    state.scan = { running: true, done: 0, total: 0, probed: 0, cached: 0, errors: 0 };
    renderScanProgress();
    toast("Scan démarré");
  } catch (e) { toast(e.message, true); }
}
function renderScanProgress() {
  const el = document.getElementById("scan-prog");
  if (!el) return;
  const s = state.scan;
  const pct = s.total ? Math.round(s.done / s.total * 100) : 0;
  el.innerHTML = `
    <div class="row" style="justify-content:space-between">
      <strong>${s.running ? "Analyse en cours…" : "Dernier scan"}</strong>
      <span class="muted">${s.done}/${s.total}</span>
    </div>
    <div class="bigprogress" style="margin:12px 0"><span style="width:${pct}%"></span><em>${pct}%</em></div>
    <div class="row" style="gap:18px">
      <span class="chip good">${s.probed} analysés</span>
      <span class="chip">${s.cached} en cache</span>
      ${s.errors ? `<span class="chip warn" title="Fichiers que ffprobe n'a pas pu lire (corrompus, tronqués…)">${s.errors} illisibles</span>` : ""}
    </div>
    <div class="muted" style="margin-top:10px;word-break:break-all">${esc(s.current_path || "")}</div>`;
}

const EXCL_GROUPS = [
  { key: "reencoded", label: "Déjà réencodés par l'application", cls: "good",
    hint: "Traités par l'app : ne sont plus proposés au réencodage." },
  { key: "unreadable", label: "Illisibles / corrompus", cls: "bad",
    hint: "ffprobe n'a pas pu lire ces fichiers (conteneur corrompu/tronqué). À re-télécharger ou réparer." },
  { key: "dolby_vision", label: "Dolby Vision (exclus par défaut)", cls: "hdr",
    hint: "Exclus car un réencode CPU casse souvent la métadonnée Dolby Vision. Activable dans Réglages." },
  { key: "efficient", label: "Déjà efficaces / gain nul", cls: "",
    hint: "Déjà bien compressés : pas de gain à attendre d'un réencode." },
  { key: "other", label: "Autres", cls: "" },
];

async function loadExcluded() {
  const el = document.getElementById("excluded-panel");
  if (!el) return;
  let items;
  try { items = (await api("/api/excluded")).excluded; }
  catch (e) { el.innerHTML = ""; return; }
  if (!items.length) { el.innerHTML = `<div class="muted">Aucun fichier ignoré.</div>`; return; }

  const groups = EXCL_GROUPS
    .map(g => ({ ...g, files: items.filter(i => i.category === g.key) }))
    .filter(g => g.files.length);

  el.innerHTML = `<h3 style="margin:0 0 12px">Fichiers ignorés <span class="muted">(${items.length})</span></h3>` +
    groups.map(g => `
      <div class="season">
        <div class="shead" data-excl="${g.key}">
          <span class="chip ${g.cls}">${g.files.length}</span>
          <strong>${g.label}</strong>
          ${g.hint ? `<span class="muted">${esc(g.hint)}</span>` : ""}
          <span class="spacer"></span><span class="muted">▾</span>
        </div>
        <div class="body hidden" id="excl-${g.key}">
          ${g.files.map(f => `
            <div class="excl-row">
              <div class="name">${esc(f.filename)}</div>
              <div class="muted" style="word-break:break-all">${esc(f.path)}</div>
              <div class="chip ${g.cls}" style="margin-top:4px">${esc(f.reason || "")}</div>
            </div>`).join("")}
        </div>
      </div>`).join("");

  el.querySelectorAll("[data-excl]").forEach(h => h.addEventListener("click", () => {
    document.getElementById(`excl-${h.dataset.excl}`).classList.toggle("hidden");
  }));
}

// ---------- content type badge + manual override ----------
function typeBadge(item) {
  const t = item.content_type === "animation"
    ? (item.is_anime ? { c: "hdr", l: "Anime" } : { c: "warn", l: "Animation" })
    : { c: "", l: "Film" };
  return `<button class="chip ${t.c} type-badge" data-type="${item.id}"
    title="Type: ${t.l} — cliquer pour corriger">${t.l}</button>`;
}
// Read-only type chip (e.g. for the series list, where there's no single file id).
function typeChip(content_type, is_anime, liveLabel = "Film") {
  const t = content_type === "animation"
    ? (is_anime ? { c: "hdr", l: "Anime" } : { c: "warn", l: "Animation" })
    : { c: "", l: liveLabel };
  return `<span class="chip ${t.c}" title="Type: ${t.l}">${t.l}</span>`;
}
async function cycleContentType(id) {
  const item = state.movies.find(m => m.id === id)
    || (state.openSeries ? state.openSeries.seasons.flatMap(s => s.episodes).find(e => e.id === id) : null);
  if (!item) return;
  let payload;
  if (item.content_type !== "animation") payload = { content_type: "animation", is_anime: false };
  else if (!item.is_anime) payload = { content_type: "animation", is_anime: true };
  else payload = { content_type: "live_action", is_anime: false };
  try {
    await api(`/api/media/${id}/content_type`, { method: "POST", body: JSON.stringify(payload) });
    if (state.view === "movies") { await loadMovies(); renderMovies(); }
    else if (state.view === "series" && state.openSeries) { await refreshSeriesDetail(); renderSeriesDetail(); }
  } catch (e) { toast(e.message, true); }
}
function bindTypeBadges() {
  app.querySelectorAll("[data-type]").forEach(b =>
    b.addEventListener("click", (e) => { e.stopPropagation(); cycleContentType(+b.dataset.type); }));
}
function gainBar(bytes, maxBytes) {
  const pct = maxBytes > 0 ? Math.round((bytes || 0) / maxBytes * 100) : 0;
  return `<div class="gain-cell"><div class="gain-bar"><span style="width:${pct}%"></span></div>
    <span class="gain-val">${fmtBytes(bytes)}</span></div>`;
}

// ---------- sortable tables (client-side) ----------
function sortRows(arr, key, dir) {
  const s = [...arr].sort((a, b) => (a[key] ?? 0) - (b[key] ?? 0));
  return dir === "asc" ? s : s.reverse();
}
function sortArrow(st, key) {
  return st.key === key ? (st.dir === "desc" ? " ▼" : " ▲") : "";
}
function applySort(st, key, rerender) {
  if (st.key === key) st.dir = st.dir === "desc" ? "asc" : "desc";
  else { st.key = key; st.dir = "desc"; }
  rerender();
}

// ---------- MOVIES ----------
function renderMovies() {
  const ms = sortRows(state.movies, state.movieSort.key, state.movieSort.dir);
  const maxGain = ms.reduce((a, m) => Math.max(a, m.est_gain_bytes || 0), 0);
  const ar = (k) => sortArrow(state.movieSort, k);
  const rows = ms.map(m => {
    const sel = state.selMovies.has(m.id);
    return `<tr>
      <td><input type="checkbox" data-mid="${m.id}" ${sel ? "checked" : ""}></td>
      <td class="cell-file">${esc(m.filename)} ${m.is_hdr ? '<span class="chip hdr">HDR</span>' : ""}</td>
      <td>${typeBadge(m)}</td>
      <td>${codecBadge(m.vcodec)}</td>
      <td>${fmtRes(m.width, m.height)}</td>
      <td class="num col-sec">${fmtDur(m.duration_s)}</td>
      <td class="num">${fmtBytes(m.size_bytes)}</td>
      <td class="num col-sec">${fmtBitrate(m.video_bitrate_bps)}</td>
      <td class="num">${m.overhead_ratio ? m.overhead_ratio.toFixed(1) + "×" : "—"}</td>
      <td>${gainBar(m.est_gain_bytes, maxGain)}</td>
      <td class="col-sec"><div class="score-bar"><span style="width:${Math.min(100, m.score || 0)}%"></span></div></td>
    </tr>`;
  }).join("");

  app.innerHTML = `
    <div class="row" style="justify-content:space-between;align-items:center">
      <h2 style="margin:0">Films à traiter en priorité <span class="muted">(${ms.length})</span></h2>
      <button class="btn ghost sm" id="m-clear" title="Supprimer les données analysées des films (le disque n'est pas touché)">🗑 Vider le cache films</button>
    </div>
    ${ms.length === 0 ? `<div class="empty">Aucun film candidat. Lance un scan.</div>` : `
    <div class="panel" style="padding:0">
      <div class="table-wrap"><table>
        <thead><tr>
          <th><input type="checkbox" id="m-all"></th>
          <th>Fichier</th><th>Type</th><th>Codec</th><th>Résolution</th><th class="num col-sec">Durée</th>
          <th class="num">Taille</th><th class="num col-sec">Débit</th>
          <th class="num sortable" data-msort="overhead_ratio">Surdébit${ar("overhead_ratio")}</th>
          <th class="sortable" data-msort="est_gain_bytes">Gain estimé${ar("est_gain_bytes")}</th>
          <th class="col-sec sortable" data-msort="score">Score${ar("score")}</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
    </div>
    <div class="selbar">
      ${encodeControls()}
      <div class="spacer"></div>
      <div id="m-sum" class="muted"></div>
      <button class="btn" id="m-encode">Encoder la sélection</button>
    </div>`}`;

  const mClear = document.getElementById("m-clear");
  if (mClear) mClear.addEventListener("click", () => clearCache("movies"));
  if (ms.length === 0) return;
  bindEncodeControls();
  bindTypeBadges();
  app.querySelectorAll("[data-msort]").forEach(h => h.addEventListener("click",
    () => applySort(state.movieSort, h.dataset.msort, renderMovies)));
  document.getElementById("m-all").addEventListener("change", e => {
    state.selMovies = e.target.checked ? new Set(ms.map(m => m.id)) : new Set();
    renderMovies();
  });
  app.querySelectorAll("input[data-mid]").forEach(cb => cb.addEventListener("change", () => {
    const id = +cb.dataset.mid;
    cb.checked ? state.selMovies.add(id) : state.selMovies.delete(id);
    updateMovieSum();
  }));
  document.getElementById("m-encode").addEventListener("click", encodeMovies);
  updateMovieSum();
}
function updateMovieSum() {
  const sel = state.movies.filter(m => state.selMovies.has(m.id));
  const gain = sel.reduce((a, m) => a + (m.est_gain_bytes || 0), 0);
  const el = document.getElementById("m-sum");
  if (el) el.textContent = `${sel.length} sélectionné(s) · gain estimé ${fmtBytes(gain)}`;
  const btn = document.getElementById("m-encode");
  if (btn) btn.disabled = sel.length === 0;
}
async function encodeMovies() {
  const ids = [...state.selMovies];
  try {
    const r = await api("/api/jobs/batch", {
      method: "POST",
      body: JSON.stringify({ codec: state.codec, profile_name: state.profile, eight_bit: state.eight_bit, file_ids: ids }),
    });
    toast(`${r.count} film(s) ajouté(s) à la file`);
    state.selMovies = new Set();
    fetchJobs();
    renderMovies();
  } catch (e) { toast(e.message, true); }
}

// ---------- SERIES ----------
function episodeStateBadge(e) {
  if (e.reencoded) return `<span class="chip good" title="Déjà réencodé par l'application">✓ réencodé</span>`;
  if (e.excluded_reason) return `<span class="chip warn" title="${esc(e.excluded_reason)}">ignoré</span>`;
  return `<span class="chip good">candidat</span>`;
}
const _CODEC_LABELS = {
  hevc: "HEVC", h265: "HEVC", av1: "AV1", h264: "H.264", avc: "H.264",
  vc1: "VC-1", mpeg2video: "MPEG-2", mpeg4: "MPEG-4", vp9: "VP9", vp8: "VP8",
  msmpeg4v3: "DivX", wmv3: "WMV3",
};
function codecBadge(vcodec) {
  if (!vcodec) return "—";
  const key = String(vcodec).toLowerCase();
  const label = _CODEC_LABELS[key] || vcodec.toUpperCase();
  // HEVC/AV1 are the efficient targets -> highlight as "good", legacy codecs neutral.
  const cls = (key === "hevc" || key === "h265" || key === "av1") ? "good" : "";
  return `<span class="chip ${cls}" title="Codec vidéo : ${esc(label)}">${esc(label)}</span>`;
}
function epLabel(e) {
  const sn = e.season != null ? "S" + String(e.season).padStart(2, "0") : "";
  const en = e.episode != null ? "E" + String(e.episode).padStart(2, "0") : "";
  return sn + en || "—";
}

function renderSeries() {
  if (state.openSeries) return renderSeriesDetail();
  const ss = sortRows(state.series, state.seriesSort.key, state.seriesSort.dir);
  const maxGain = ss.reduce((a, s) => Math.max(a, s.est_gain_bytes || 0), 0);
  const ar = (k) => sortArrow(state.seriesSort, k);
  const rows = ss.map(s => `
    <tr data-slug="${esc(s.series_slug)}" class="srow">
      <td class="cell-file">${esc(s.series_title || s.series_slug)}</td>
      <td>${typeChip(s.content_type, s.is_anime, "Live action")}</td>
      <td class="num">${s.n_candidates}/${s.n_episodes}</td>
      <td>${gainBar(s.est_gain_bytes, maxGain)}</td>
      <td class="col-sec"><div class="score-bar"><span style="width:${Math.min(100, s.top_score || 0)}%"></span></div></td>
      <td><button class="btn sm ghost">Ouvrir →</button></td>
    </tr>`).join("");
  app.innerHTML = `
    <div class="row" style="justify-content:space-between;align-items:center">
      <h2 style="margin:0">Séries <span class="muted">(${ss.length})</span>
        <span class="muted" style="font-weight:400;font-size:13px">— triées par gain estimé</span></h2>
      <button class="btn ghost sm" id="s-clear" title="Supprimer les données analysées des séries (le disque n'est pas touché)">🗑 Vider le cache séries</button>
    </div>
    ${ss.length === 0 ? `<div class="empty">Aucune série détectée. Lance un scan.</div>` : `
    <div class="panel" style="padding:0"><div class="table-wrap"><table>
      <thead><tr><th>Série</th>
      <th>Type</th>
      <th class="num sortable" data-ssort="n_candidates">Candidats${ar("n_candidates")}</th>
      <th class="sortable" data-ssort="est_gain_bytes">Gain estimé${ar("est_gain_bytes")}</th>
      <th class="col-sec sortable" data-ssort="top_score">Score max${ar("top_score")}</th>
      <th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div></div>`}`;
  const sClear = document.getElementById("s-clear");
  if (sClear) sClear.addEventListener("click", () => clearCache("series"));
  app.querySelectorAll("[data-ssort]").forEach(h => h.addEventListener("click", (e) => {
    e.stopPropagation(); applySort(state.seriesSort, h.dataset.ssort, renderSeries);
  }));
  app.querySelectorAll(".srow").forEach(tr => tr.addEventListener("click", () => openSeries(tr.dataset.slug)));
}
async function openSeries(slug) {
  try {
    state.openSeries = await api(`/api/series/${encodeURIComponent(slug)}?${cp()}`);
    state.selEpisodes = new Set();
    renderSeriesDetail();
  } catch (e) { toast(e.message, true); }
}
function renderSeriesDetail() {
  const s = state.openSeries;
  const allEps = s.seasons.flatMap(se => se.episodes);
  const maxGain = allEps.reduce((a, e) => Math.max(a, e.est_gain_bytes || 0), 0);

  const seasons = s.seasons.map((se, idx) => {
    const ids = se.episodes.map(e => e.id);
    const allSel = ids.length > 0 && ids.every(id => state.selEpisodes.has(id));
    const eps = se.episodes.map(e => `
      <tr>
        <td><input type="checkbox" data-ep="${e.id}" ${state.selEpisodes.has(e.id) ? "checked" : ""}></td>
        <td>${epLabel(e)}</td>
        <td class="cell-file">${esc(e.filename)}</td>
        <td>${typeBadge(e)}</td>
        <td>${codecBadge(e.vcodec)}</td>
        <td>${fmtRes(e.width, e.height)}</td>
        <td class="num">${fmtBytes(e.size_bytes)}</td>
        <td class="num col-sec">${e.overhead_ratio ? e.overhead_ratio.toFixed(1) + "×" : "—"}</td>
        <td>${gainBar(e.est_gain_bytes, maxGain)}</td>
        <td>${episodeStateBadge(e)}</td>
      </tr>`).join("");
    return `
      <div class="season">
        <div class="shead">
          <input type="checkbox" data-season-all="${idx}" ${allSel ? "checked" : ""} title="Tout cocher">
          <strong>${se.season != null ? "Saison " + se.season : "Autres"}</strong>
          <span class="muted">${se.n_candidates} candidat(s) · gain ${fmtBytes(se.est_gain_bytes)}</span>
          <span class="spacer"></span>
          <button class="btn ghost sm" data-cand="${idx}" ${se.n_candidates === 0 ? "disabled" : ""}>Sélectionner les candidats</button>
        </div>
        <div class="body"><div class="table-wrap"><table>
          <thead><tr><th></th><th>Ép.</th><th>Fichier</th><th>Type</th><th>Codec</th><th>Résolution</th>
          <th class="num">Taille</th><th class="num col-sec">Surdébit</th><th>Gain</th><th>État</th></tr></thead>
          <tbody>${eps}</tbody></table></div></div>
      </div>`;
  }).join("");

  const selList = allEps.filter(e => state.selEpisodes.has(e.id));
  const selGain = selList.reduce((a, e) => a + (e.est_gain_bytes || 0), 0);

  app.innerHTML = `
    <div class="row" style="margin-bottom:12px">
      <button class="btn ghost sm" id="back">← Séries</button>
      <h2 style="margin:0">${esc(s.series_title)}</h2>
    </div>
    ${seasons}
    <div class="selbar">
      ${encodeControls()}
      <div class="spacer"></div>
      <div class="muted">${selList.length} épisode(s) · gain estimé ${fmtBytes(selGain)}</div>
      <button class="btn" id="s-encode" ${selList.length === 0 ? "disabled" : ""}>Encoder la sélection</button>
    </div>`;

  document.getElementById("back").addEventListener("click", () => { state.openSeries = null; renderSeries(); });
  bindEncodeControls();
  bindTypeBadges();
  app.querySelectorAll("input[data-ep]").forEach(cb => cb.addEventListener("change", () => {
    const id = +cb.dataset.ep;
    cb.checked ? state.selEpisodes.add(id) : state.selEpisodes.delete(id);
    renderSeriesDetail();
  }));
  app.querySelectorAll("input[data-season-all]").forEach(cb => cb.addEventListener("change", () => {
    const se = s.seasons[+cb.dataset.seasonAll];
    for (const e of se.episodes) cb.checked ? state.selEpisodes.add(e.id) : state.selEpisodes.delete(e.id);
    renderSeriesDetail();
  }));
  app.querySelectorAll("[data-cand]").forEach(b => b.addEventListener("click", () => {
    const se = s.seasons[+b.dataset.cand];
    for (const e of se.episodes) if (!e.excluded_reason && !e.reencoded) state.selEpisodes.add(e.id);
    renderSeriesDetail();
  }));
  document.getElementById("s-encode").addEventListener("click", encodeSeriesSelection);
}
async function encodeSeriesSelection() {
  const ids = [...state.selEpisodes];
  if (!ids.length) return;
  try {
    const r = await api("/api/jobs/batch", {
      method: "POST",
      body: JSON.stringify({ codec: state.codec, profile_name: state.profile, eight_bit: state.eight_bit, file_ids: ids }),
    });
    toast(`${r.count} épisode(s) ajouté(s) à la file`);
    state.selEpisodes = new Set();
    fetchJobs();
    renderSeriesDetail();
  } catch (e) { toast(e.message, true); }
}

// ---------- QUEUE ----------
const TERMINAL_STATES = ["DONE", "REJECTED", "CANCELLED", "FAILED"];
const STOPPABLE_STATES = ["QUEUED", "COPYING_IN", "READY", "ENCODING", "PAUSED"];
function renderQueue() {
  const jobs = [...state.jobs].reverse();
  const nTerminal = jobs.filter(j => TERMINAL_STATES.includes(j.state)).length;
  const nStoppable = jobs.filter(j => STOPPABLE_STATES.includes(j.state)).length;
  const paused = state.queuePaused;
  const banner = paused
    ? `<div class="chip warn" style="margin-bottom:12px">⏸ File en pause — aucun nouveau job ne démarre tant que tu n'as pas repris.</div>`
    : "";
  const header = `
    <div class="row" style="justify-content:space-between;margin-bottom:14px">
      <h2 style="margin:0">File d'attente</h2>
      <div class="row">
        <button class="btn ghost sm" id="q-pause-all" ${paused || nStoppable === 0 ? "disabled" : ""}>⏸ Tout mettre en pause</button>
        <button class="btn good sm" id="q-resume-all" ${paused ? "" : "disabled"}>▶ Tout reprendre</button>
        <button class="btn bad sm" id="q-stop-all" ${nStoppable === 0 ? "disabled" : ""}>⏹ Tout arrêter</button>
        <button class="btn ghost sm" id="q-clear" ${nTerminal === 0 ? "disabled" : ""}>
          Nettoyer (${nTerminal})</button>
      </div>
    </div>` + banner;
  if (jobs.length === 0) { app.innerHTML = header + `<div class="empty">Aucun job.</div>`; return; }
  app.innerHTML = header + jobs.map(jobCard).join("");

  const onClick = (id, fn) => { const b = document.getElementById(id); if (b) b.addEventListener("click", fn); };
  const bulk = async (url, msg) => {
    try { await api(url, { method: "POST" }); toast(msg); fetchJobs(); }
    catch (e) { toast(e.message, true); }
  };
  onClick("q-pause-all", () => bulk("/api/jobs/pause-all", "File en pause (jobs en cours et à venir)"));
  onClick("q-resume-all", () => bulk("/api/jobs/resume-all", "File relancée"));
  onClick("q-stop-all", () => {
    if (!confirm(`Arrêter ${nStoppable} job(s) en cours/en attente ? La progression sera perdue.`)) return;
    bulk("/api/jobs/stop-all", "Tous les jobs arrêtés");
  });
  const clearBtn = document.getElementById("q-clear");
  if (clearBtn) clearBtn.addEventListener("click", async () => {
    try {
      const r = await api("/api/jobs/clear", { method: "POST" });
      toast(`${r.removed} job(s) retiré(s)`);
      fetchJobs();
    } catch (e) { toast(e.message, true); }
  });
  jobs.forEach(j => {
    const root = document.getElementById(`job-${j.id}`);
    if (!root) return;
    root.querySelectorAll("[data-act]").forEach(b => b.addEventListener("click", () => jobAction(j.id, b.dataset.act)));
    const del = root.querySelector("[data-del]");
    if (del) del.addEventListener("click", () => deleteJob(j.id));
  });
}
async function deleteJob(id) {
  try {
    await api(`/api/jobs/${id}`, { method: "DELETE" });
    fetchJobs();
  } catch (e) { toast(e.message, true); }
}
function jobCard(j) {
  const pct = Math.round((j.progress || 0) * 100);
  let body = "";
  if (j.state === "ENCODING") {
    body = `<div class="progress"><span style="width:${pct}%"></span></div>
      <div class="meta" style="margin-top:6px">${pct}% · ${j.speed || "—"} · ETA ${fmtDur(j.eta_s)}</div>`;
  } else if (j.state === "PAUSED") {
    body = `<div class="progress paused"><span style="width:${pct}%"></span></div>
      <div class="meta" style="margin-top:6px">⏸ En pause · ${pct}%</div>`;
  } else if (j.state === "AWAITING_CONFIRMATION") {
    body = validationBlock(j) + `<div class="row" style="margin-top:10px">
      <button class="btn good sm" data-act="confirm">Valider et remplacer</button>
      <button class="btn bad sm" data-act="reject">Rejeter</button></div>`;
  } else if (j.state === "FAILED") {
    body = `<div class="chip bad">${esc(j.error_message || "échec")}</div>`;
  } else if (["DONE"].includes(j.state)) {
    body = `<div class="meta">Remplacé · gain ${fmtBytes(j.gain_bytes)} (${fmtBytes(j.size_src_bytes)} → ${fmtBytes(j.size_out_bytes)})</div>`;
  } else {
    body = `<div class="meta">${stateLabel(j.state)}</div>`;
  }
  const terminal = TERMINAL_STATES.includes(j.state);
  let actions = "";
  if (j.state === "ENCODING") {
    actions = `<button class="btn ghost sm" data-act="pause" title="Mettre en pause">⏸ Pause</button>
      <button class="btn bad sm" data-act="forcestop" title="Forcer l'arrêt">⏹ Arrêter</button>`;
  } else if (j.state === "PAUSED") {
    actions = `<button class="btn good sm" data-act="resume" title="Reprendre">▶ Reprendre</button>
      <button class="btn bad sm" data-act="forcestop" title="Forcer l'arrêt">⏹ Arrêter</button>`;
  } else if (["QUEUED", "COPYING_IN", "READY"].includes(j.state)) {
    actions = `<button class="btn ghost sm" data-act="cancel">Annuler</button>`;
  } else if (terminal) {
    actions = `<button class="btn ghost sm job-del" data-del="${j.id}" title="Retirer de la liste">✕</button>`;
  }
  return `<div class="job" id="job-${j.id}">
    <div class="head">
      <div><div class="name">${esc(j.filename)}</div>
      <div class="meta">${j.codec === "SVTAV1" ? "AV1" : "HEVC"} ${j.eight_bit ? "8-bit" : "10-bit"} · ${esc(j.profile_name)} · CRF ${j.crf} · preset ${esc(j.preset)}</div></div>
      <div style="display:flex;gap:8px;align-items:center">
        <span class="state ${j.state}">${stateLabel(j.state)}</span>
        ${actions}
      </div>
    </div>
    ${body}
  </div>`;
}
function validationBlock(j) {
  let report;
  try { report = JSON.parse(j.validation_json); } catch (_) { return ""; }
  const checks = (report.checks || []).map(c =>
    `<span class="check ${c.passed ? "ok" : "ko"}" title="${esc(c.detail)}">${esc(c.name)}</span>`).join("");
  return `<div class="meta">Gain ${fmtBytes(report.gain_bytes)} (${fmtBytes(report.size_src_bytes)} → ${fmtBytes(report.size_out_bytes)})</div>
    <div class="checks">${checks}</div>`;
}
function stateLabel(s) {
  return ({
    QUEUED: "En attente", COPYING_IN: "Copie locale…", READY: "Prêt (copié)",
    ENCODING: "Encodage", PAUSED: "En pause", VALIDATING: "Validation…",
    AWAITING_CONFIRMATION: "À valider",
    COPYING_BACK: "Renvoi…", REPLACING: "Remplacement…", DONE: "Terminé",
    REJECTED: "Rejeté", CANCELLED: "Annulé", FAILED: "Échec",
  })[s] || s;
}
async function jobAction(id, act) {
  try {
    if (act === "forcestop") {
      if (!confirm("Forcer l'arrêt de cet encodage ? La progression en cours sera perdue.")) return;
      await api(`/api/jobs/${id}/cancel`, { method: "POST" });
      toast("Encodage arrêté");
      fetchJobs();
      return;
    }
    await api(`/api/jobs/${id}/${act}`, { method: "POST" });
    if (act === "confirm") toast("Fichier remplacé");
    if (act === "reject") toast("Job rejeté");
    if (act === "pause") toast("Encodage en pause");
    if (act === "resume") toast("Encodage repris");
    fetchJobs();
  } catch (e) { toast(e.message, true); }
}

// ---------- LOGS ----------
let logsTimer;
function renderLogs() {
  clearInterval(logsTimer);
  app.innerHTML = `
    <div class="row" style="justify-content:space-between;margin-bottom:14px">
      <h2 style="margin:0">Logs</h2>
      <div class="row">
        <label class="field" style="margin:0"><span>Niveau</span>
          <select id="log-level">
            <option value="">Tout</option>
            <option value="INFO">INFO+</option>
            <option value="WARNING" selected>WARNING+</option>
            <option value="ERROR">ERROR seul</option>
          </select></label>
        <button class="btn ghost sm" id="log-refresh">Rafraîchir</button>
        <button class="btn ghost sm" id="log-clear">Vider</button>
      </div>
    </div>
    <div class="panel" style="padding:0"><div id="log-list" class="logs"></div></div>`;
  document.getElementById("log-level").addEventListener("change", loadLogs);
  document.getElementById("log-refresh").addEventListener("click", loadLogs);
  document.getElementById("log-clear").addEventListener("click", async () => {
    await api("/api/logs/clear", { method: "POST" }); loadLogs();
  });
  loadLogs();
  logsTimer = setInterval(() => { if (state.view === "logs") loadLogs(); else clearInterval(logsTimer); }, 3000);
}
async function loadLogs() {
  const levelSel = document.getElementById("log-level");
  const list = document.getElementById("log-list");
  if (!levelSel || !list) return;
  const level = levelSel.value;
  try {
    const { logs } = await api(`/api/logs?limit=500${level ? "&level=" + level : ""}`);
    if (!logs.length) { list.innerHTML = `<div class="empty">Aucun log.</div>`; return; }
    list.innerHTML = logs.slice().reverse().map(r => `
      <div class="logline ${r.level}">
        <span class="lt">${new Date(r.ts * 1000).toLocaleTimeString()}</span>
        <span class="ll ${r.level}">${r.level}</span>
        <span class="ln">${esc(r.logger)}</span>
        <span class="lm">${esc(r.message)}</span>
      </div>`).join("");
  } catch (e) { toast(e.message, true); }
}

// ---------- SETTINGS ----------
function bandRows(bands, prefix) {
  return bands.map((b, i) => {
    const hi = b.height_max >= 100000 ? "∞" : b.height_max + "px";
    return `<tr><td>${b.height_min}–${hi}</td>
      <td><input type="text" class="bpp-in" data-grp="${prefix}" data-hmin="${b.height_min}"
        data-hmax="${b.height_max}" value="${b.bpp_target}" style="width:90px"></td></tr>`;
  }).join("");
}
function collectBands(prefix) {
  return [...document.querySelectorAll(`.bpp-in[data-grp="${prefix}"]`)]
    .map(el => [+el.dataset.hmin, +el.dataset.hmax, +el.value]);
}

async function renderSettings() {
  let data;
  try { data = await api("/api/settings"); } catch (e) { return toast(e.message, true); }
  const sc = data.scoring;
  const cd = data.content_detection;
  const enc = data.encoding;
  const np = state.profiles.length;
  const profiles = state.profiles.map((p, i) => `
    <tr>
      <td><strong>${esc(p.name)}</strong></td>
      <td><span class="chip">${profileTier(i, np)}</span></td>
      <td class="num">${p.crf_x265}</td><td>${esc(p.preset_x265)}</td>
      <td class="num">${p.crf_av1}</td><td class="num">${p.preset_av1}</td>
    </tr>`).join("");

  app.innerHTML = `
    <h2>Réglages</h2>
    <div class="panel">
      <h3 style="margin-top:0">Pondération du score</h3>
      <div class="row">
        <label class="field"><span>Poids surdébit</span><input type="text" id="set-wo" value="${sc.weight_overhead}"></label>
        <label class="field"><span>Poids gain</span><input type="text" id="set-wg" value="${sc.weight_gain}"></label>
        <label class="field"><span>Réf. gain (Go)</span><input type="text" id="set-gr" value="${sc.gain_ref_gb}"></label>
        <label class="field"><span>Surdébit min.</span><input type="text" id="set-mo" value="${sc.min_overhead_ratio}"></label>
      </div>
      <label class="muted"><input type="checkbox" id="set-dv" ${sc.exclude_dolby_vision ? "checked" : ""}> Exclure les fichiers Dolby Vision (recommandé)</label>
      <div class="row" style="margin-top:14px"><button class="btn" id="set-save">Enregistrer</button>
      <span class="muted">Relance un scan pour recalculer les scores.</span></div>
    </div>

    <div class="panel">
      <h3 style="margin-top:0">Détection du type de contenu (TMDB)</h3>
      <label class="field"><span>Clé API TMDB (gratuite sur themoviedb.org) — détecte automatiquement l'animation/anime des films et séries.</span>
        <input type="text" id="set-tmdb-key" value="${esc(cd.tmdb_api_key || "")}" placeholder="laisser vide pour désactiver"></label>
      <label class="muted"><input type="checkbox" id="set-tmdb-on" ${cd.tmdb_enabled ? "checked" : ""}> Activer la détection TMDB</label>
      <label class="field" style="margin-top:12px"><span>Mots-clés de repli (un par ligne) — utilisés si TMDB est absent/hors-ligne.</span>
        <textarea id="set-kw" rows="3" style="width:100%;background:var(--panel-2);border:1px solid var(--line);color:var(--text);border-radius:8px;padding:9px">${esc((cd.animation_keywords || []).join("\\n"))}</textarea></label>
      <div class="row"><button class="btn" id="set-cd-save">Enregistrer la détection</button>
      <span class="muted">Prise en compte au prochain scan (résultats mis en cache).</span></div>
    </div>

    <div class="panel">
      <h3 style="margin-top:0">Encodage & nom de sortie</h3>
      <div class="row">
        <label class="field" style="max-width:220px"><span>Encodages simultanés (1 encode 1080p exploite mal un 16c/32t)</span>
          <input type="text" id="set-par" value="${enc.max_parallel_encodes}"></label>
        <label class="field" style="flex:1"><span>Tag ajouté au nom de fichier (ex. «&nbsp; x265&nbsp;») — laisser vide pour ne rien ajouter</span>
          <input type="text" id="set-tag" value="${esc(enc.filename_tag || "")}" placeholder=" x265"></label>
      </div>
      <label class="muted"><input type="checkbox" id="set-rw" ${enc.rewrite_codec_tags ? "checked" : ""}> Réécrire les tokens de codec dans le nom/titre (x264→x265…) — sans effet sur les noms Radarr propres</label>
      <label class="muted"><input type="checkbox" id="set-audio-opus" ${enc.audio_lossless_to_opus ? "checked" : ""}> Compresser l'audio lossless (TrueHD/DTS-HD MA/PCM/FLAC → Opus, transparent) — les pistes déjà compressées (AC3/AAC/DTS) restent intactes</label>
      <div class="row" style="margin-top:14px"><button class="btn" id="set-enc-save">Enregistrer</button>
      <span class="muted">⚠️ Si Radarr/Sonarr gère tes noms, il peut renommer après coup. Métadonnées vidéo (débit) corrigées automatiquement à chaque encode.</span></div>
    </div>

    <div class="panel">
      <h3 style="margin-top:0">Tables de référence (bits/pixel cible)</h3>
      <div class="row" style="align-items:flex-start;gap:30px">
        <div><div class="muted" style="margin-bottom:6px">Films (live action)</div>
          <table><thead><tr><th>Hauteur</th><th>bpp</th></tr></thead>
          <tbody>${bandRows(data.reference_bands, "live")}</tbody></table></div>
        <div><div class="muted" style="margin-bottom:6px">Animation / Anime</div>
          <table><thead><tr><th>Hauteur</th><th>bpp</th></tr></thead>
          <tbody>${bandRows(data.animation_bands, "anim")}</tbody></table></div>
      </div>
      <div class="row" style="margin-top:14px"><button class="btn" id="set-bands-save">Enregistrer les tables</button>
      <span class="muted">bpp plus bas = on attend un fichier plus compressé. L'animation compresse mieux → cibles plus basses.</span></div>
    </div>

    <div class="panel">
      <h3 style="margin-top:0">Profils d'encodage</h3>
      <div class="muted" style="margin-bottom:8px">Du plus qualitatif au plus compressé.</div>
      <div class="table-wrap"><table><thead><tr><th>Profil</th><th>Niveau</th><th class="num">CRF x265</th><th>Preset x265</th>
      <th class="num">CRF AV1</th><th class="num">Preset AV1</th></tr></thead>
      <tbody>${profiles}</tbody></table></div>
      <div class="muted" style="margin-top:8px">CRF plus bas = meilleure qualité / fichier plus gros. Le gain estimé est calculé à partir du CRF et de la table bits/pixel ci-dessus.</div>
    </div>

    <div class="panel">
      <h3 style="margin-top:0">ffmpeg</h3>
      <div id="ffmpeg-info" class="muted">Chargement…</div>
      <div class="row" style="margin-top:12px">
        <button class="btn" id="ff-check">Vérifier les mises à jour</button>
        <button class="btn" id="ff-update">Mettre à jour ffmpeg</button>
      </div>
      <div class="muted" style="margin-top:8px">Build GPL (libx265 + libsvtav1). La mise à jour est impossible pendant un encodage.</div>
    </div>

    <div class="panel">
      <h3 style="margin-top:0">Dossier de travail local</h3>
      <label class="field"><span>Les fichiers sont copiés ici depuis le NAS, encodés, puis renvoyés. Doit être sur un disque local rapide avec assez d'espace.</span>
        <input type="text" id="set-wd" value="${esc(data.work_dir)}"></label>
      <div class="row"><button class="btn" id="set-wd-save">Enregistrer le dossier</button>
      <span class="muted">Tolérance de durée : ${data.duration_tolerance_pct}%</span></div>
    </div>`;

  document.getElementById("set-wd-save").addEventListener("click", async () => {
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify({
        work_dir: document.getElementById("set-wd").value.trim(),
      })});
      toast("Dossier de travail enregistré");
    } catch (e) { toast(e.message, true); }
  });

  document.getElementById("set-save").addEventListener("click", async () => {
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify({
        weight_overhead: +document.getElementById("set-wo").value,
        weight_gain: +document.getElementById("set-wg").value,
        gain_ref_gb: +document.getElementById("set-gr").value,
        min_overhead_ratio: +document.getElementById("set-mo").value,
        exclude_dolby_vision: document.getElementById("set-dv").checked,
      })});
      toast("Réglages enregistrés");
    } catch (e) { toast(e.message, true); }
  });

  document.getElementById("set-cd-save").addEventListener("click", async () => {
    const kws = document.getElementById("set-kw").value
      .split(/[\n,]/).map(s => s.trim()).filter(Boolean);
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify({
        tmdb_api_key: document.getElementById("set-tmdb-key").value.trim(),
        tmdb_enabled: document.getElementById("set-tmdb-on").checked,
        animation_keywords: kws,
      })});
      toast("Détection enregistrée");
    } catch (e) { toast(e.message, true); }
  });

  document.getElementById("set-enc-save").addEventListener("click", async () => {
    const par = parseInt(document.getElementById("set-par").value, 10);
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify({
        max_parallel_encodes: Number.isFinite(par) && par > 0 ? par : 1,
        filename_tag: document.getElementById("set-tag").value,
        rewrite_codec_tags: document.getElementById("set-rw").checked,
        audio_lossless_to_opus: document.getElementById("set-audio-opus").checked,
      })});
      toast("Réglages d'encodage enregistrés");
    } catch (e) { toast(e.message, true); }
  });

  document.getElementById("set-bands-save").addEventListener("click", async () => {
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify({
        reference_bands: collectBands("live"),
        animation_bands: collectBands("anim"),
      })});
      toast("Tables enregistrées — relance un scan pour recalculer");
    } catch (e) { toast(e.message, true); }
  });

  loadFfmpegInfo();
  document.getElementById("ff-check").addEventListener("click", () => loadFfmpegInfo());
  document.getElementById("ff-update").addEventListener("click", async () => {
    const btn = document.getElementById("ff-update");
    btn.disabled = true; btn.textContent = "Téléchargement…";
    try {
      const r = await api("/api/ffmpeg/update", { method: "POST" });
      toast("ffmpeg mis à jour" + (r.version ? ` (${r.version})` : ""));
      await loadFfmpegInfo();
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; btn.textContent = "Mettre à jour ffmpeg"; }
  });
}

async function loadFfmpegInfo() {
  const el = document.getElementById("ffmpeg-info");
  if (!el) return;
  el.textContent = "Chargement…";
  let d;
  try { d = await api("/api/ffmpeg"); } catch (e) { el.textContent = "Erreur : " + e.message; return; }
  let status;
  if (!d.version) status = `<span class="chip bad">❌ ffmpeg introuvable</span>`;
  else if (d.update_available === true) status = `<span class="chip warn">⬆️ mise à jour disponible</span>`;
  else if (d.update_available === false) status = `<span class="chip good">✅ à jour</span>`;
  else status = `<span class="chip">⚠️ vérification impossible (hors-ligne)</span>`;
  const ver = d.version ? esc(d.version) : "—";
  const date = d.build_date ? ` · build ${esc(d.build_date)}` : "";
  el.innerHTML = `${status}<div style="margin-top:6px">Version : <strong>${ver}</strong>${date}</div>`
    + `<div class="muted" style="margin-top:4px">${esc(d.path || "")}</div>`;
}

// ---------- boot ----------
(async function boot() {
  try { state.profiles = (await api("/api/profiles")).profiles; } catch (_) {}
  // Defensive: order quality -> most compressed (lower CRF = higher quality).
  state.profiles.sort((a, b) => a.crf_x265 - b.crf_x265);
  if (state.profiles.length && !state.profiles.find(p => p.name === state.profile))
    state.profile = state.profiles[0].name;
  connectWS();
  await reloadLibrary();
  await fetchJobs();
  loadStats();
  render();
})();
