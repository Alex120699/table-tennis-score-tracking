// Plataforma de análisis de partidos: historial, nuevo análisis y ficha de partido.
// Vistas por hash: #/  ·  #/nuevo[?src=...]  ·  #/partido/<id>

const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const fmt = (s) => {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, ss = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(ss).padStart(2, "0");
};
// "AAAA-MM-DD" se interpreta como fecha local (new Date("2026-10-25") sería medianoche UTC).
const fmtDate = (iso) => {
  if (!iso) return "";
  const d = /^\d{4}-\d{2}-\d{2}$/.test(iso) ? new Date(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10)) : new Date(iso);
  return d.toLocaleDateString("es-ES", { day: "numeric", month: "short", year: "numeric" });
};

async function api(path, body, method) {
  const opts = body === undefined && !method ? {} : {
    method: method || "POST", headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  const ct = res.headers.get("content-type") || "";
  if (!res.ok) {
    const msg = ct.includes("json") ? (await res.json()).error : res.statusText;
    throw new Error(msg || "Error");
  }
  return ct.includes("json") ? res.json() : res.blob();
}

function loadImage(src) {
  return new Promise((ok, fail) => { const i = new Image(); i.onload = () => ok(i); i.onerror = fail; i.src = src; });
}
function setStatus(id, msg, error) { $(id).textContent = msg || ""; $(id).classList.toggle("error", !!error); }

function youtubeLink(source, t) {
  try {
    const u = new URL(source);
    const id = u.hostname.includes("youtu.be") ? u.pathname.slice(1) : u.searchParams.get("v");
    return id ? `https://www.youtube.com/watch?v=${id}&t=${Math.max(0, Math.floor(t))}s` : null;
  } catch { return null; }
}

function resultText(m) {
  const [a, b] = m.games_won;
  if (m.winner !== null) {
    const w = m.winner, won = m.games_won[w], lost = m.games_won[1 - w];
    return `${m.names[w]} gana ${won}–${lost}`;
  }
  if (m.status === "running") return `En curso · ${a}–${b}`;
  return `Sin terminar · ${a}–${b}`;
}
const STATUS_TEXT = { running: "analizando", done: "completo", stopped: "detenido", error: "error" };

// =========================================================== router
const VIEWS = { history: "view-history", new: "view-new", match: "view-match", players: "view-players", player: "view-player" };
let currentView = null, currentMatchId = null;

function route() {
  const h = location.hash || "#/";
  let view = "history", arg = null;
  if (h.startsWith("#/nuevo")) { view = "new"; arg = new URLSearchParams(h.split("?")[1] || ""); }
  else if (h.startsWith("#/partido/")) { view = "match"; arg = parseInt(h.split("/")[2], 10); }
  else if (h.startsWith("#/jugadores")) view = "players";
  else if (h.startsWith("#/jugador/")) { view = "player"; arg = decodeURIComponent(h.slice("#/jugador/".length)); }
  for (const [k, id] of Object.entries(VIEWS)) $(id).classList.toggle("hidden", k !== view);
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === view || (view === "player" && a.dataset.nav === "players")));
  currentView = view;
  currentMatchId = view === "match" ? arg : null;
  if (view === "history") showHistory();
  if (view === "new") showNew(arg);
  if (view === "match") showMatch(arg);
  if (view === "players") showPlayers();
  if (view === "player") showPlayer(arg);
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);

// =========================================================== tarea en curso (cabecera)
let job = null, jobTimer = null;
async function pollJob() {
  clearTimeout(jobTimer);
  try { job = await api("/api/job"); } catch { jobTimer = setTimeout(pollJob, 3000); return; }
  const pill = $("job-pill");
  if (job.stage === "running") {
    pill.textContent = `Analizando… ${(job.progress * 100).toFixed(0)}%`;
    pill.href = `#/partido/${job.match_id}`;
    pill.classList.remove("hidden");
  } else if (job.stage === "downloading") {
    pill.textContent = job.message || "Descargando…"; pill.href = "#/nuevo"; pill.classList.remove("hidden");
  } else pill.classList.add("hidden");
  onJob(job);
  jobTimer = setTimeout(pollJob, job.stage === "running" || job.stage === "downloading" ? 1000 : 5000);
}

function onJob(j) {
  if (currentView === "new") newOnJob(j);
  if (currentView === "match" && j.match_id === currentMatchId) matchOnJob(j);
}

// =========================================================== historial
let allMatches = [];
async function showHistory() {
  allMatches = await api("/api/matches");
  renderHistory();
}
$("search").addEventListener("input", renderHistory);

function renderHistory() {
  const q = $("search").value.trim().toLowerCase();
  const list = allMatches.filter((m) => !q || `${m.names.join(" ")} ${m.title}`.toLowerCase().includes(q));
  $("match-empty").classList.toggle("hidden", allMatches.length > 0);
  $("match-list").innerHTML = list.map((m) => `
    <a class="match" href="#/partido/${m.id}">
      ${m.thumb ? `<img src="/api/matches/${m.id}/thumb" alt="" loading="lazy">` : `<div class="noimg"></div>`}
      <div class="body">
        <div class="players">${esc(m.names[0])} vs ${esc(m.names[1])}</div>
        <div class="result">${esc(resultText(m))}</div>
        <div class="meta">${m.games.map((g) => `${g.points[0]}–${g.points[1]}`).join(" · ") || "&nbsp;"}</div>
        <div class="meta">${m.match_date ? fmtDate(m.match_date) : `sin fecha · analizado ${fmtDate(m.created_at)}`} · ${fmt(m.duration)}
          ${m.status !== "done" ? ` · <span class="badge">${STATUS_TEXT[m.status] || m.status}</span>` : ""}</div>
      </div>
    </a>`).join("");
}

// =========================================================== nuevo análisis: calibración
// Cada canvas tiene resolución interna "píxeles de imagen × zoom"; toImage() convierte la
// posición del ratón a coordenadas de la imagen que muestra.
// Una "posición de cámara" (segment) por cada tramo del vídeo con un encuadre distinto:
//   start: desde qué segundo se usa · t: fotograma donde se dibujó el marcador (plantilla del
//   seguimiento) · rough: zona amplia · roi: marcador ajustado · shear: inclinación.
// S.rough/roi/shear/calibT leen y escriben la posición activa, la que corresponde al fotograma visible.
const newSegment = (start, shear = 0) => ({ start, t: null, rough: null, roi: null, shear });
const S = {
  video: null, t: 0, frameImg: null, boardImg: null, segments: [newSegment(0)], seg: 0,
  get cur() { return this.segments[this.seg]; },
  get rough() { return this.cur.rough; }, set rough(v) { this.cur.rough = v; },
  get roi() { return this.cur.roi; }, set roi(v) { this.cur.roi = v; },
  get shear() { return this.cur.shear; }, set shear(v) { this.cur.shear = v; },
  get calibT() { return this.cur.t; }, set calibT(v) { this.cur.t = v; },
};
const segIndexAt = (t) => S.segments.reduce((best, seg, i) => (seg.start <= t ? i : best), 0);

function renderSegments() {
  const many = S.segments.length > 1;
  $("seg-chips").innerHTML = S.segments.map((seg, i) =>
    `<button class="game ${i === S.seg ? "active" : ""}" data-i="${i}">${i + 1} · desde ${fmt(seg.start)}` +
    `${seg.roi ? "" : " <span style='color:var(--danger)'>sin marcar</span>"}</button>`).join("");
  $("btn-del-seg").classList.toggle("hidden", S.seg === 0);
  $("shear").value = S.shear; $("shear-val").textContent = S.shear.toFixed(2);
  const action = S.rough ? "puedes volver a arrastrar aquí para elegir otra zona." : "arrastra un rectángulo amplio alrededor del marcador.";
  $("frame-hint").textContent = many
    ? `Posición ${S.seg + 1} (desde ${fmt(S.cur.start)}): ${action}`
    : action.charAt(0).toUpperCase() + action.slice(1);
}
function switchSegment(i) {
  if (i === S.seg) return;
  S.seg = i; S.boardImg = null;
  $("test-result").textContent = ""; $("test-raw").textContent = "";
  refreshCalib(true);
}
$("seg-chips").onclick = (e) => {
  const b = e.target.closest("button[data-i]"); if (!b) return;
  const seg = S.segments[+b.dataset.i];
  setTime(seg.t ?? seg.start, true);
};
$("btn-add-seg").onclick = () => {
  if (S.segments.some((seg) => seg.start === S.t)) return;
  S.segments.push(newSegment(S.t, S.shear));
  S.segments.sort((a, b) => a.start - b.start);
  S.seg = -1; switchSegment(segIndexAt(S.t));
};
$("btn-del-seg").onclick = () => {
  if (S.seg === 0) return;
  S.segments.splice(S.seg, 1);
  S.seg = -1; switchSegment(segIndexAt(S.t));
};

function makePicker(canvas, { toImage, draw, onRect }) {
  let start = null;
  const pos = (e) => {
    const r = canvas.getBoundingClientRect();
    return toImage((e.clientX - r.left) * canvas.width / r.width, (e.clientY - r.top) * canvas.height / r.height);
  };
  canvas.addEventListener("pointerdown", (e) => { start = pos(e); canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener("pointermove", (e) => { if (start) draw(rectOf(start, pos(e))); });
  canvas.addEventListener("pointerup", (e) => {
    if (!start) return;
    const r = rectOf(start, pos(e)); start = null;
    if (r.w >= 3 && r.h >= 3) onRect(r); else draw(null);
  });
}
const rectOf = (a, b) => ({ x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(a.x - b.x), h: Math.abs(a.y - b.y) });

function strokeRect(ctx, r, z, color, dash) {
  ctx.save();
  ctx.strokeStyle = color; ctx.lineWidth = 2 * (ctx.canvas.width / ctx.canvas.getBoundingClientRect().width || 1);
  if (dash) ctx.setLineDash([6 * ctx.lineWidth, 4 * ctx.lineWidth]);
  ctx.strokeRect(r.x * z.s + z.ox, r.y * z.s + z.oy, r.w * z.s, r.h * z.s);
  ctx.restore();
}
function clampRect(r) {
  const W = S.video.width, H = S.video.height;
  const x = Math.max(0, Math.round(r.x)), y = Math.max(0, Math.round(r.y));
  return { x, y, w: Math.min(W - x, Math.round(r.w)), h: Math.min(H - y, Math.round(r.h)) };
}

// 1) fotograma completo
const frameCanvas = $("frame-canvas");
function drawFrame(temp) {
  if (!S.frameImg) return;
  const ctx = frameCanvas.getContext("2d");
  frameCanvas.width = S.frameImg.naturalWidth; frameCanvas.height = S.frameImg.naturalHeight;
  ctx.drawImage(S.frameImg, 0, 0);
  const z = { s: 1, ox: 0, oy: 0 };
  if (S.rough) strokeRect(ctx, S.rough, z, "#ffd23f", true);
  if (S.roi) strokeRect(ctx, S.roi, z, css("--sel"));
  if (temp) strokeRect(ctx, temp, z, css("--sel"));
}
makePicker(frameCanvas, {
  toImage: (x, y) => ({ x, y }),
  draw: drawFrame,
  onRect: (r) => {
    const m = Math.max(r.w, r.h) * 0.25;  // margen para ajustar cómodamente en el zoom
    S.rough = clampRect({ x: r.x - m, y: r.y - m, w: r.w + 2 * m, h: r.h + 2 * m });
    S.roi = null; S.boardImg = null;
    refreshCalib();
  },
});

// 2) zoom de la zona amplia
const zoomCanvas = $("zoom-canvas");
const zoomFactor = () => Math.max(1, Math.floor(900 / S.rough.w));
function drawZoom(temp) {
  if (!S.rough || !S.frameImg) return;
  const z = zoomFactor(), ctx = zoomCanvas.getContext("2d");
  zoomCanvas.width = S.rough.w * z; zoomCanvas.height = S.rough.h * z;
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(S.frameImg, S.rough.x, S.rough.y, S.rough.w, S.rough.h, 0, 0, zoomCanvas.width, zoomCanvas.height);
  const m = { s: z, ox: -S.rough.x * z, oy: -S.rough.y * z };
  if (S.roi) strokeRect(ctx, S.roi, m, css("--sel"));
  if (temp) strokeRect(ctx, temp, m, css("--sel"));
}
makePicker(zoomCanvas, {
  toImage: (x, y) => { const z = zoomFactor(); return { x: S.rough.x + x / z, y: S.rough.y + y / z }; },
  draw: drawZoom,
  onRect: (r) => { S.roi = clampRect(r); S.calibT = S.t; $("test-result").textContent = ""; refreshCalib(true); },
});

// 3) marcador enderezado (lo que lee el OCR)
const boardCanvas = $("board-canvas");
function drawBoard() {
  if (!S.boardImg) return;
  const z = Math.max(1, Math.floor(900 / S.roi.w)), ctx = boardCanvas.getContext("2d");
  boardCanvas.width = S.roi.w * z; boardCanvas.height = S.roi.h * z;
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(S.boardImg, 0, 0, boardCanvas.width, boardCanvas.height);
  updateRunButton();
}
async function loadBoard() {
  if (!S.roi) return;
  const blob = await api("/api/board", { t: S.t, roi: S.roi, shear: S.shear });
  S.boardImg = await loadImage(URL.createObjectURL(blob));
  drawBoard();
}
let shearTimer = null;
$("shear").oninput = (e) => {
  S.shear = parseFloat(e.target.value); $("shear-val").textContent = S.shear.toFixed(2);
  clearTimeout(shearTimer); shearTimer = setTimeout(loadBoard, 120);
};

function refreshCalib(boardChanged) {
  drawFrame(); drawZoom();
  $("zoom-panel").classList.toggle("hidden", !S.rough);
  $("board-panel").classList.toggle("hidden", !S.roi);
  renderSegments();
  if (S.roi && (boardChanged || !S.boardImg)) loadBoard();
  updateRunButton();
}

const calibration = () => ({
  interval: parseFloat($("interval").value),
  segments: S.segments.map((seg) => ({ start: seg.start, t: seg.t ?? seg.start, roi: seg.roi, shear: seg.shear, rough: seg.rough })),
});
const scoreHtml = (s) => `${s[0]} <small>(${s[1]})</small> – <small>(${s[2]})</small> ${s[3]}`;

$("btn-test").onclick = async () => {
  $("test-result").textContent = "Leyendo…"; $("test-raw").textContent = "";
  try {
    const r = await api("/api/test", { t: S.t, roi: S.roi, shear: S.shear });  // el fotograma que se está viendo
    $("test-result").innerHTML = r.reading ? scoreHtml(r.reading) : "No se pudo leer";
    $("test-raw").textContent = `OCR: "${r.text}" · confianza ${(r.confidence * 100).toFixed(0)}%`;
  } catch (e) { $("test-result").textContent = e.message; }
};

// Prueba rápida en 20 momentos del vídeo: detecta un recuadro que solo funciona en el fotograma
// elegido, o un tramo en el que la cámara cambió de posición.
$("btn-test-video").onclick = async () => {
  if (S.segments.some((seg) => !seg.roi)) { $("test-result").textContent = "Marca el marcador en todas las posiciones de cámara"; return; }
  $("test-result").textContent = "Comprobando en 20 momentos del vídeo…"; $("test-raw").textContent = ""; $("test-samples").innerHTML = "";
  $("btn-test-video").disabled = true;
  try {
    const r = await api("/api/test-video", calibration());
    const pct = Math.round(100 * r.readable / r.total);
    $("test-result").innerHTML = `Legible en ${r.readable} de ${r.total} momentos <small>(${pct} %)</small>`;
    // 3+ momentos seguidos sin lectura: lo típico cuando la cámara cambia de sitio.
    let run = 0, lostFrom = null;
    r.samples.forEach((s, i) => { run = s.reading ? 0 : run + 1; if (run === 3 && lostFrom === null) lostFrom = r.samples[i - 2].t; });
    $("test-raw").textContent = pct >= 80 && lostFrom === null
      ? "Buena calibración. Los momentos ilegibles suelen ser jugadores tapando el marcador."
      : lostFrom !== null
        ? `Desde ~${fmt(lostFrom)} casi no se lee. Si la cámara cambió de posición, ve a ese momento y pulsa «+ Nueva posición desde este fotograma». Si no, ajusta el recuadro solo a las cifras.`
        : "Pocas lecturas: ajusta el recuadro solo a las cifras (sin rayas ni manos) y revisa la inclinación. Pulsa un momento para verlo.";
    const many = S.segments.length > 1;
    $("test-samples").innerHTML = r.samples.map((s) =>
      `<button class="game" data-t="${Math.round(s.t)}" title="OCR: ${esc(s.text)}">${many ? `P${s.segment + 1} · ` : ""}${fmt(s.t)} · ` +
      `${s.reading ? s.reading.join("-") : "<span style='color:var(--danger)'>ilegible</span>"}</button>`).join("");
  } catch (e) { $("test-result").textContent = e.message; }
  $("btn-test-video").disabled = false;
};
$("test-samples").onclick = (e) => {
  const b = e.target.closest("button[data-t]"); if (b) setTime(+b.dataset.t, true).then(() => frameCanvas.scrollIntoView({ behavior: "smooth" }));
};

let frameTimer = null;
function setTime(t, immediate) {
  S.t = Math.max(0, Math.min(Math.floor(S.video.duration) - 1, Math.round(t)));
  $("t-range").value = S.t; $("t-num").value = S.t; $("t-fmt").textContent = fmt(S.t);
  switchSegment(segIndexAt(S.t));  // el fotograma visible decide qué posición de cámara se edita
  clearTimeout(frameTimer);
  return new Promise((done) => {
    frameTimer = setTimeout(async () => {
      S.frameImg = await loadImage(`/api/frame?t=${S.t}&_=${Date.now()}`);
      drawFrame(); drawZoom();
      if (S.roi) await loadBoard();
      done();
    }, immediate ? 0 : 150);
  });
}
$("t-range").oninput = (e) => setTime(+e.target.value);
$("t-num").onchange = (e) => setTime(+e.target.value, true);

// =========================================================== nuevo análisis: flujo
$("btn-load").onclick = async () => {
  const source = $("source").value.trim();
  if (!source) return;
  try { await api("/api/videos", { source }); S.video = null; pollJob(); }
  catch (e) { setStatus("video-status", e.message, true); }
};
$("source").addEventListener("keydown", (e) => { if (e.key === "Enter") $("btn-load").click(); });

// Si se llega desde "Reanalizar", el análisis sustituye a ese partido en lugar de crear otro.
let replaceId = null;
function showNew(params) {
  const src = params?.get("src");
  replaceId = params?.get("replace") ? parseInt(params.get("replace"), 10) : null;
  $("btn-run").textContent = replaceId ? `Reanalizar y sustituir el partido #${replaceId}` : "Analizar y guardar en el historial";
  if (src) {
    $("source").value = src;
    history.replaceState(null, "", "#/nuevo");
    if (!job || job.video?.source !== src) { $("btn-load").click(); return; }
  }
  if (job) newOnJob(job);
  pollJob();
}

function newOnJob(j) {
  $("dl-progress").classList.toggle("hidden", j.stage !== "downloading");
  $("dl-progress").firstElementChild.style.width = j.download_pct + "%";
  $("btn-load").disabled = j.stage === "downloading" || j.stage === "running";
  if (j.stage === "downloading") setStatus("video-status", j.message);
  else if (j.stage === "error") setStatus("video-status", j.message, true);
  const ready = !!j.video && j.stage !== "downloading";
  $("s-calib").classList.toggle("hidden", !ready);
  $("s-run").classList.toggle("hidden", !ready);
  if (ready && S.video?.path !== j.video.path) onVideoReady(j);
  if (j.stage === "running") setStatus("run-status", "Hay un análisis en curso; espera a que termine para lanzar otro.");
  else if ($("run-status").textContent.startsWith("Hay un análisis")) setStatus("run-status", "");
  updateRunButton();
}

function onVideoReady(j) {
  const v = j.video;
  S.video = v; S.boardImg = null; S.segments = [newSegment(0)]; S.seg = 0;
  if (!$("source").value) $("source").value = v.source;
  $("t-range").max = Math.floor(v.duration) - 1;
  $("test-result").textContent = ""; $("test-raw").textContent = ""; $("test-samples").innerHTML = "";
  setStatus("video-status", `${v.title} · ${v.width}×${v.height} · ${fmt(v.duration)}`);
  // "Jugador A vs Jugador B" en el título -> nombres por defecto.
  const vs = v.title.split(" - ").pop().split(/\s+vs\.?\s+/i);
  if (vs.length === 2) { $("name1").value = vs[0].trim(); $("name2").value = vs[1].trim(); }
  $("match-date").value = v.date || "";
  const c = j.calibration;
  if (c && c.segments?.length) {
    S.segments = c.segments.map((seg) => ({ ...newSegment(seg.start || 0, seg.shear || 0), t: seg.t, roi: seg.roi,
      rough: seg.rough || clampRect({ x: seg.roi.x - seg.roi.w, y: seg.roi.y - seg.roi.h, w: seg.roi.w * 3, h: seg.roi.h * 3 }) }));
    if (c.interval) $("interval").value = String(c.interval);
    if (c.names) { $("name1").value = c.names[0]; $("name2").value = c.names[1]; }
    if (c.best_of) $("best-of").value = c.best_of;
    setStatus("video-status", $("video-status").textContent + " · calibración del análisis anterior cargada");
  }
  setTime(c?.segments?.length ? (S.segments[0].t ?? 0) : v.start, true).then(() => refreshCalib(true));
}

function updateRunButton() {
  $("btn-run").disabled = !S.video || S.segments.some((seg) => !seg.roi) || job?.stage === "running";
}
$("btn-run").onclick = async () => {
  try {
    const r = await api("/api/matches", {
      calibration: calibration(), names: [$("name1").value, $("name2").value],
      best_of: parseInt($("best-of").value, 10), swap: $("swap-new").value, replace_id: replaceId,
      match_date: $("match-date").value || null,
    });
    location.hash = `#/partido/${r.id}`;
    pollJob();
  } catch (e) { setStatus("run-status", e.message, true); }
};

// =========================================================== ficha de partido
let M = null, matchTimer = null, chart = null;

async function showMatch(id) {
  clearTimeout(matchTimer);
  editMode = false; editing = null; $("m-edit-error").textContent = "";
  selectedIndex = null; viewT = null;
  $("m-frame-wrap").classList.add("hidden"); $("m-ocr").classList.add("hidden");
  $("m-frame-cap").classList.add("hidden"); $("m-frame-hint").classList.remove("hidden");
  try { M = await api(`/api/matches/${id}`); }
  catch { $("m-banner").textContent = "Partido no encontrado"; return; }
  renderMatch();
  if (M.status === "running") matchTimer = setTimeout(() => refreshMatch(id), 2000);
}
async function refreshMatch(id) {
  if (currentMatchId !== id) return;
  M = await api(`/api/matches/${id}`);
  renderMatch();
  if (M.status === "running") matchTimer = setTimeout(() => refreshMatch(id), 2000);
}

function matchOnJob(j) {
  const running = j.stage === "running";
  $("m-running").classList.toggle("hidden", !running);
  if (!running) return;
  $("m-progress").style.width = (j.progress * 100).toFixed(1) + "%";
  const elapsed = Date.now() / 1000 - j.started_at;
  const eta = j.progress > 0.01 ? elapsed * (1 - j.progress) / j.progress : null;
  const r = j.last_reading;
  $("m-progress-text").textContent = j.progress === 0 ? j.message :
    `${fmt(j.current_time)} / ${fmt(M?.duration || 0)}` + (eta ? ` · quedan ~${fmt(eta)}` : "") +
    ` · última lectura: ${r ? `${r[0]} (${r[1]}) – (${r[2]}) ${r[3]}` : "ilegible"}`;
}

function eventText(ev, nm) {
  return ev.replace(/^punto A$/, `punto ${nm[0]}`).replace(/^punto B$/, `punto ${nm[1]}`)
    .replace(/^juego A$/, `juego para ${nm[0]}`).replace(/^juego B$/, `juego para ${nm[1]}`);
}

function renderMatch() {
  const nm = M.names;
  $("m-title").textContent = M.title;
  $("m-banner").textContent = resultText(M);
  $("m-meta").innerHTML = (M.match_date ? `Jugado el ${fmtDate(M.match_date)} · ` : "") +
    `Analizado el ${fmtDate(M.created_at)} · ${fmt(M.duration)} de vídeo` +
    (M.status !== "done" ? ` · ${STATUS_TEXT[M.status] || M.status}${M.message ? ": " + esc(M.message) : ""}` : "") +
    (youtubeLink(M.source, 0) ? ` · <a href="${esc(youtubeLink(M.source, 0))}" target="_blank" rel="noopener">Ver en YouTube</a>` : "");
  for (const [id, v] of [["m-name1", nm[0]], ["m-name2", nm[1]], ["m-date", M.match_date || ""]]) if (document.activeElement !== $(id)) $(id).value = v;
  $("m-best-of").value = M.best_of; $("m-swap").value = M.swap;
  $("m-players").innerHTML = `<a href="#/jugador/${encodeURIComponent(playerKey(nm[0]))}">${esc(nm[0])}</a> vs ` +
    `<a href="#/jugador/${encodeURIComponent(playerKey(nm[1]))}">${esc(nm[1])}</a>`;
  const first = $("m-first");
  first.options[1].textContent = nm[0]; first.options[2].textContent = nm[1];
  first.value = M.first_server === null ? "" : String(M.first_server);
  renderStats(M.stats, nm);
  $("m-swap-info").textContent = M.swap === "auto" ? `(${M.swap_detected ? "detectado: sí" : "detectado: no"})` : "";
  $("m-csv").href = `/api/matches/${M.id}/csv`;
  $("m-log").href = `/api/matches/${M.id}/log`;
  $("m-running").classList.toggle("hidden", M.status !== "running");

  $("m-games").innerHTML = M.games.map((g, i) =>
    `<div class="game">Juego ${i + 1}: <b>${g.points[0]}–${g.points[1]}</b> <span class="muted">${esc(nm[g.winner])}` +
    `${g.inferred ? " · último punto deducido" : ""}</span></div>`).join("");

  renderEvents();
  drawChart(M.players, M.games, nm, M.status === "running" ? (job?.current_time ?? null) : M.duration);
}

// ---------------------------------------------------------------- tabla de puntos y correcciones
// editing: null | { index, insert }  — index es el evento editado o, si insert, tras el que se añade.
let editMode = false, editing = null;

const parseTime = (txt) => String(txt).trim().split(":").reduce((acc, part) => acc * 60 + Number(part), 0);

function editRow(i, t, points, games, insert) {
  return `<tr class="editing" data-edit="${i}" data-insert="${insert ? 1 : ""}">` +
    `<td><input class="t-in" value="${fmt(t)}" title="Minuto:segundo del vídeo"></td>` +
    `<td><input type="number" min="0" max="40" value="${points[0]}">–<input type="number" min="0" max="40" value="${points[1]}"></td>` +
    `<td><input type="number" min="0" max="4" value="${games[0]}">–<input type="number" min="0" max="4" value="${games[1]}"></td>` +
    `<td colspan="3"><button class="primary" data-act="save">Guardar</button> <button data-act="cancel">Cancelar</button>` +
    `${insert ? ' <span class="muted" style="font-size:13px">punto nuevo</span>' : ""}</td></tr>`;
}

function renderEvents() {
  const nm = M.names, pl = M.players;
  const canEdit = M.status !== "running";
  if (!canEdit) { editMode = false; editing = null; }
  $("m-th-score").textContent = `${nm[0]} – ${nm[1]}`;
  $("m-th-actions").classList.toggle("hidden", !editMode);
  $("m-edit").classList.toggle("hidden", !canEdit);
  $("m-edit").textContent = editMode ? "Terminar corrección" : "Corregir puntos";
  $("m-edit").classList.toggle("primary", editMode);
  $("m-edit-hint").classList.toggle("hidden", !editMode);
  $("m-restore").classList.toggle("hidden", !(editMode && M.has_ocr_backup));
  $("m-edit-info").textContent = M.manual_edits
    ? `${M.manual_edits} corrección${M.manual_edits === 1 ? "" : "es"} manual${M.manual_edits === 1 ? "" : "es"}`
    : M.has_ocr_backup ? "Corregido a mano" : "";
  const rows = [];
  pl.forEach((p, i) => {
    const e = M.events[i];
    if (editing && !editing.insert && editing.index === i) rows.push(editRow(i, p.t, p.points, p.games, false));
    else rows.push(`<tr data-i="${i}"><td>${fmt(p.t)}</td><td>${p.points[0]}–${p.points[1]}</td>` +
      `<td>${p.games[0]}–${p.games[1]}</td><td>${esc(eventText(p.event, nm))}</td>` +
      `<td class="muted">${e.manual ? '<span class="badge manual">manual</span>' : `${e.score[0]} (${e.score[1]}) – (${e.score[2]}) ${e.score[3]}`}</td>` +
      (editMode ? `<td class="actions"><button data-act="edit">Editar</button> <button data-act="delete">Borrar</button> ` +
        `<button data-act="insert" title="Añadir un punto que el OCR no leyó, después de este">+ Después</button></td>` : "") + `</tr>`);
    if (editing && editing.insert && editing.index === i) {
      // Punto nuevo: a mitad de camino hasta el siguiente cambio, con el marcador de esta fila.
      const next = pl[i + 1], t = next ? (p.t + next.t) / 2 : p.t + 10;
      rows.push(editRow(i, t, p.points, p.games, true));
    }
  });
  if (editing && editing.insert && editing.index === -1) rows.unshift(editRow(-1, 0, [0, 0], [0, 0], true));
  // Rehacer la tabla sin perder la posición: se conserva el scroll y la fila seleccionada,
  // y la fila que se está editando queda a la vista con el cursor en los puntos.
  const box = $("m-events").closest(".table-scroll"), keep = box.scrollTop, pageY = window.scrollY;
  $("m-events").innerHTML = rows.reverse().join("");
  box.scrollTop = keep; window.scrollTo(window.scrollX, pageY);
  if (selectedIndex !== null) document.querySelector(`#m-events tr[data-i="${selectedIndex}"]`)?.classList.add("sel");
  const editingRow = document.querySelector("#m-events tr.editing");
  if (editingRow) {
    const top = editingRow.getBoundingClientRect().top - box.getBoundingClientRect().top + box.scrollTop;
    const bottom = top + editingRow.offsetHeight;
    if (top < box.scrollTop + 30 || bottom > box.scrollTop + box.clientHeight) box.scrollTop = Math.max(0, top - box.clientHeight / 3);
    editingRow.querySelectorAll("input")[1]?.focus({ preventScroll: true });
  }
}

$("m-edit").onclick = () => { editMode = !editMode; editing = null; $("m-edit-error").textContent = ""; renderEvents(); };
$("m-restore").onclick = async () => {
  if (!confirm("¿Deshacer todas las correcciones manuales y volver a la lectura del OCR?")) return;
  await saveEdit(() => api(`/api/matches/${M.id}/events/restore`, {}));
};

async function saveEdit(request) {
  $("m-edit-error").textContent = "";
  try { M = await request(); editing = null; selectedIndex = null; renderMatch(); }
  catch (e) { $("m-edit-error").textContent = e.message; }
}

// Visor: fotograma del punto seleccionado con el recuadro que leyó el OCR, el recorte que recibió
// y las lecturas de los segundos alrededor (las del log del análisis, o leídas ahora si no hay log).
let selectedIndex = null, viewT = null, inspectSeq = 0;

function showFrame(i) {
  selectedIndex = i;
  document.querySelectorAll("#m-events tr.sel").forEach((r) => r.classList.remove("sel"));
  document.querySelector(`#m-events tr[data-i="${i}"]`)?.classList.add("sel");
  const p = M.players[i], prev = M.players[i - 1];
  // El marcador cambia después del punto: el vídeo de YouTube empieza donde quedó el marcador anterior.
  const yt = youtubeLink(M.source, prev ? prev.t : p.t - 15);
  $("m-frame-cap").innerHTML = `<span>${fmt(p.t)} · ${esc(M.names[0])} ${p.points[0]}–${p.points[1]} ${esc(M.names[1])}</span>` +
    (yt ? `<a href="${esc(yt)}" target="_blank" rel="noopener">Ver el punto en YouTube</a>` : "");
  $("m-frame-cap").classList.remove("hidden");
  showMoment(Math.round(p.t));
}

async function showMoment(t) {
  viewT = Math.max(0, Math.min(Math.floor(M.duration), t));
  const seq = ++inspectSeq;
  $("m-frame-wrap").classList.remove("hidden"); $("m-frame-hint").classList.add("hidden");
  $("m-roi").classList.add("hidden");
  $("m-frame").src = `/api/matches/${M.id}/frame?t=${viewT}`;
  $("m-ocr").classList.remove("hidden");
  $("m-ocr-note").textContent = "Cargando lecturas…";
  let r;
  try { r = await api(`/api/matches/${M.id}/inspect?t=${viewT}`); }
  catch (e) { if (seq === inspectSeq) $("m-ocr-note").textContent = e.message; return; }
  if (seq !== inspectSeq) return;  // el usuario ya pidió otro momento

  // Lecturas que confirmaron la fila seleccionada: las que dan su marcador a partir de su instante.
  const ev = selectedIndex !== null ? M.events[selectedIndex] : null;
  const confirms = (s) => ev && s.reading && s.t >= ev.t && s.reading.join() === ev.score.join();
  const current = r.samples.reduce((best, s) => (!best || Math.abs(s.t - viewT) < Math.abs(best.t - viewT) ? s : best), null);
  $("m-ocr-rows").innerHTML = r.samples.map((s) =>
    `<tr data-t="${s.t}" class="${s === current ? "now" : ""} ${confirms(s) ? "confirm" : ""}">` +
    `<td>${fmt(s.t)}</td><td>"${esc(s.text)}"</td><td>${Math.round(s.confidence * 100)} %</td>` +
    `<td>${s.reading ? `${s.reading[0]} (${s.reading[1]}) – (${s.reading[2]}) ${s.reading[3]}` : "<span style='color:var(--danger)'>ilegible</span>"}</td></tr>`).join("");
  $("m-ocr-note").textContent = (r.source === "log"
    ? "Lecturas guardadas del análisis, una por segundo (izquierda – derecha del marcador)."
    : "Este partido no tiene registro: lecturas hechas ahora con la misma calibración.") +
    " ✓ = lecturas que confirmaron la fila; hacen falta 3 iguales seguidas." +
    (r.samples.length ? "" : " No hay lecturas en este momento.");

  if (current) {
    const roi = current.roi;
    $("m-board").src = `/api/matches/${M.id}/board?t=${current.t}&x=${roi.x}&y=${roi.y}&w=${roi.w}&h=${roi.h}&shear=${r.shear}`;
    placeRoi(roi);
  }
}

function placeRoi(roi) {
  const img = $("m-frame"), box = $("m-roi");
  const place = () => {
    const W = img.naturalWidth, H = img.naturalHeight;
    if (!W) return;
    Object.assign(box.style, { left: `${100 * roi.x / W}%`, top: `${100 * roi.y / H}%`,
      width: `${100 * roi.w / W}%`, height: `${100 * roi.h / H}%` });
    box.classList.remove("hidden");
  };
  if (img.complete && img.naturalWidth) place(); else img.onload = place;
}

$("m-prev").onclick = () => viewT !== null && showMoment(viewT - 1);
$("m-next").onclick = () => viewT !== null && showMoment(viewT + 1);
$("m-ocr-rows").onclick = (e) => { const tr = e.target.closest("tr[data-t]"); if (tr) showMoment(+tr.dataset.t); };

$("m-events").onclick = async (e) => {
  const btn = e.target.closest("button[data-act]");
  const tr = e.target.closest("tr");
  if (!tr) return;
  if (!btn) {
    if (tr.dataset.i !== undefined && !e.target.closest("input")) showFrame(+tr.dataset.i);
    return;
  }
  const act = btn.dataset.act;
  if (act === "edit" || act === "insert") {
    const i = +tr.dataset.i;
    editing = { index: i, insert: act === "insert" };
    renderEvents();
    showFrame(i);  // para comparar con lo que se ve en el marcador mientras se corrige
    return;
  }
  if (act === "cancel") { editing = null; renderEvents(); return; }
  if (act === "delete") {
    const i = +tr.dataset.i;
    await saveEdit(() => api(`/api/matches/${M.id}/events/${i}`, undefined, "DELETE"));
    return;
  }
  if (act === "save") {
    const v = [...tr.querySelectorAll("input")].map((inp) => inp.value);
    const body = { t: parseTime(v[0]), points: [+v[1], +v[2]], games: [+v[3], +v[4]] };
    if (Number.isNaN(body.t)) { $("m-edit-error").textContent = "Tiempo no válido: usa minuto:segundo, p. ej. 9:48"; return; }
    const i = +tr.dataset.edit;
    await saveEdit(() => tr.dataset.insert
      ? api(`/api/matches/${M.id}/events`, { ...body, after: i })
      : api(`/api/matches/${M.id}/events/${i}`, body, "PUT"));
  }
};

async function patchMatch(body) {
  M = await api(`/api/matches/${M.id}`, body, "PATCH");
  renderMatch();
}
$("m-name1").onchange = $("m-name2").onchange = () => patchMatch({ names: [$("m-name1").value, $("m-name2").value] });
$("m-best-of").onchange = () => patchMatch({ best_of: parseInt($("m-best-of").value, 10) });
$("m-swap").onchange = () => patchMatch({ swap: $("m-swap").value });
$("m-date").onchange = () => patchMatch({ match_date: $("m-date").value || null });
$("m-first").onchange = () => patchMatch({ first_server: $("m-first").value === "" ? null : +$("m-first").value });
$("m-stop").onclick = () => api("/api/job/stop", {});
$("m-reanalyze").onclick = () => {
  if (M.manual_edits && !confirm(`Reanalizar sustituye los puntos y se perderán ${M.manual_edits} correcciones manuales. ¿Continuar?`)) return;
  location.hash = `#/nuevo?src=${encodeURIComponent(M.source)}&replace=${M.id}`;
};
$("m-delete").onclick = async () => {
  if (!confirm(`¿Borrar del historial "${M.names[0]} vs ${M.names[1]}"? El vídeo descargado no se borra.`)) return;
  try { await api(`/api/matches/${M.id}`, undefined, "DELETE"); location.hash = "#/"; }
  catch (e) { alert(e.message); }
};

// =========================================================== estadísticas
// Misma normalización que stats.player_key: sin tildes, minúsculas, espacios -> guiones.
const playerKey = (name) => name.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase().trim().split(/\s+/).join("-");
const pct = (a, b) => (b ? Math.round((100 * a) / b) : null);
const ratio = (a, b) => (b ? `${a}/${b} <small>${pct(a, b)} %</small>` : `<small>—</small>`);
const meter = (p, color) => (p === null ? "" : `<div class="meter"><i style="width:${p}%;background:${color}"></i></div>`);

function renderStats(s, nm) {
  const [A, B] = s.players, c = [css("--series-1"), css("--series-2")];
  const row = (label, cells, pcts) => `<div class="cmp-row">` +
    `<div class="v a">${cells[0]}${pcts ? meter(pcts[0], c[0]) : ""}</div><div class="lbl">${label}</div>` +
    `<div class="v b">${cells[1]}${pcts ? meter(pcts[1], c[1]) : ""}</div></div>`;
  const run = (r) => (r ? `${r.length} <small>de ${r.from.join("–")} a ${r.to.join("–")} · J${r.game}</small>` : "<small>—</small>");
  const comeback = (cb) => (cb ? `−${cb.deficit} <small>y ganó ${cb.final.join("–")} · J${cb.game}</small>` : "<small>—</small>");
  const serveKnown = s.first_server !== null;
  const both = (f) => [f(A), f(B)];
  $("m-stats").innerHTML =
    `<div class="cmp-head"><span>${esc(nm[0])}<i class="swatch" style="background:${c[0]}"></i></span><span></span>` +
    `<span><i class="swatch" style="background:${c[1]}"></i>${esc(nm[1])}</span></div>` +
    row("Puntos ganados", both((p) => `${p.points_won} <small>${p.points_pct === null ? "—" : Math.round(p.points_pct)} %</small>`), both((p) => p.points_pct)) +
    (serveKnown
      ? row("Puntos con su saque", both((p) => ratio(p.serve_won, p.serve_played)), both((p) => pct(p.serve_won, p.serve_played))) +
        row("Puntos al resto", both((p) => ratio(p.return_won, p.return_played)), both((p) => pct(p.return_won, p.return_played)))
      : "") +
    row("Bolas de juego convertidas", both((p) => ratio(p.gp_converted, p.gp_had)), both((p) => pct(p.gp_converted, p.gp_had))) +
    row("Bolas de juego salvadas", both((p) => ratio(p.gp_saved, p.gp_faced)), both((p) => pct(p.gp_saved, p.gp_faced))) +
    row("Juegos a 10–10 ganados", both((p) => (s.deuce_games ? `${p.deuce_won}/${s.deuce_games}` : "<small>—</small>"))) +
    row("Mayor racha", both((p) => run(p.longest_run))) +
    row("Mayor remontada en un juego", both((p) => comeback(p.comeback)));

  const notes = [];
  if (!serveKnown) notes.push("Indica arriba quién sacó el primer punto del partido para ver los puntos con saque y al resto.");
  if (s.point_time.avg) notes.push(`Tiempo medio entre puntos: ${Math.round(s.point_time.avg)} s (incluye las pausas); el más largo, ${Math.round(s.point_time.max)} s, terminó en ${fmt(s.point_time.max_t)}.`);
  if (s.points.unordered) notes.push(`${s.points.unordered} puntos se leyeron de golpe sin saber su orden: cuentan en el total, pero no en saque, rachas ni bolas de juego.`);
  $("m-stats-note").textContent = notes.join(" ");

  $("m-game-rows").innerHTML = s.games.map((g) => {
    const extra = [g.deuce ? "10–10" : "", g.comeback ? `remontada de ${g.comeback}` : "", g.finished ? "" : "sin terminar"].filter(Boolean).join(" · ");
    return `<tr><td>${g.number}</td><td>${g.points ? g.points.join("–") : "—"}</td>` +
      `<td>${g.winner === null ? "—" : esc(nm[g.winner])}</td><td>${fmt(g.duration)}</td>` +
      `<td>${g.longest_run.join("–")}</td><td>${g.max_lead.join("–")}</td><td class="muted">${extra}</td></tr>`;
  }).join("");
}

// =========================================================== jugadores
let allPlayers = [];
async function showPlayers() {
  allPlayers = await api("/api/players");
  renderPlayers();
}
$("player-search").addEventListener("input", renderPlayers);
function renderPlayers() {
  const q = playerKey($("player-search").value || " ").replace(/^-$/, "");
  const pctCell = (a) => (a[1] ? `${pct(a[0], a[1])} %` : "—");
  $("player-rows").innerHTML = allPlayers.filter((p) => !q || p.key.includes(q)).map((p) =>
    `<tr data-key="${esc(p.key)}"><td><b>${esc(p.name)}</b></td><td>${p.matches}</td><td>${p.wins}–${p.losses}</td>` +
    `<td>${p.games[0]}–${p.games[1]}</td><td>${pctCell([p.points[0], p.points[0] + p.points[1]])}</td>` +
    `<td>${pctCell(p.serve)}</td><td>${pctCell(p.return)}</td></tr>`).join("") ||
    `<tr><td colspan="7" class="muted">Sin jugadores todavía.</td></tr>`;
}
$("player-rows").onclick = (e) => {
  const tr = e.target.closest("tr[data-key]"); if (tr) location.hash = `#/jugador/${encodeURIComponent(tr.dataset.key)}`;
};

let playerChart = null;
async function showPlayer(key) {
  let P;
  try { P = await api(`/api/players/${encodeURIComponent(key)}`); }
  catch { $("p-name").textContent = "Jugador no encontrado"; return; }
  const t = P.totals, totalPts = t.points[0] + t.points[1];
  $("p-name").textContent = P.name;
  $("p-sub").textContent = `${t.matches} partido${t.matches === 1 ? "" : "s"} en el historial`;
  const tile = (k, n, s) => `<div class="tile"><div class="k">${k}</div><div class="n">${n}</div><div class="s">${s}</div></div>`;
  const rate = (a) => (a[1] ? `${pct(a[0], a[1])} %` : "—");
  $("p-tiles").innerHTML =
    tile("Partidos", `${t.wins}–${t.losses}`, "ganados–perdidos" + (t.matches - t.wins - t.losses ? ` · ${t.matches - t.wins - t.losses} sin terminar` : "")) +
    tile("Juegos", `${t.games[0]}–${t.games[1]}`, "ganados–perdidos") +
    tile("Puntos ganados", rate([t.points[0], totalPts]), `${t.points[0]} de ${totalPts}`) +
    tile("Con su saque", rate(t.serve), t.serve[1] ? `${t.serve[0]} de ${t.serve[1]}` : "falta indicar quién sacó primero") +
    tile("Al resto", rate(t.return), t.return[1] ? `${t.return[0]} de ${t.return[1]}` : "falta indicar quién sacó primero") +
    tile("Bolas de juego", rate(t.gp), `${t.gp[0]} convertidas de ${t.gp[1]}`) +
    tile("Bolas salvadas", rate(t.gp_saved), `${t.gp_saved[0]} de ${t.gp_saved[1]}`) +
    tile("Juegos a 10–10", t.deuce[1] ? `${t.deuce[0]}–${t.deuce[1] - t.deuce[0]}` : "—", "ganados–perdidos");

  $("p-h2h").innerHTML = P.head_to_head.map((h) =>
    `<tr data-key="${esc(h.key)}"><td>${esc(h.name)}</td><td>${h.matches}</td><td>${h.wins}–${h.losses}</td>` +
    `<td>${h.games[0]}–${h.games[1]}</td><td>${rate([h.points[0], h.points[0] + h.points[1]])}</td></tr>`).join("");
  $("p-matches").innerHTML = P.matches.slice().reverse().map((m) =>
    `<tr data-id="${m.id}"><td>${fmtDate(m.date)}</td><td>${esc(m.rival)}</td>` +
    `<td>${m.won === null ? "sin terminar " : m.won ? "ganó " : "perdió "}${m.games[0]}–${m.games[1]}</td>` +
    `<td>${m.points_pct === null ? "—" : Math.round(m.points_pct) + " %"}</td></tr>`).join("");

  // % de puntos por partido, en orden cronológico, con la referencia del 50 %.
  const labels = P.matches.map((m) => `${fmtDate(m.date)} · ${m.rival}`);
  const c1 = css("--series-1"), grid = css("--grid"), tick = css("--text-secondary"), muted = css("--text-muted");
  const data = {
    labels,
    datasets: [
      { label: "% de puntos", data: P.matches.map((m) => m.points_pct), borderColor: c1, backgroundColor: c1,
        borderWidth: 2, pointRadius: 4, pointHoverRadius: 6 },
      { label: "50 %", data: P.matches.map(() => 50), borderColor: muted, borderWidth: 1, borderDash: [4, 4],
        pointRadius: 0, pointHitRadius: 0 },
    ],
  };
  if (playerChart) playerChart.destroy();
  playerChart = new Chart($("p-chart"), {
    type: "line", data,
    options: {
      animation: false, maintainAspectRatio: false,
      scales: {
        x: { grid: { color: grid }, ticks: { color: tick, maxRotation: 0, autoSkip: true } },
        y: { min: 0, max: 100, grid: { color: grid }, ticks: { color: tick, callback: (v) => v + " %" } },
      },
      plugins: {
        legend: { display: false },
        tooltip: { filter: (i) => i.datasetIndex === 0, callbacks: { label: (i) => `${i.parsed.y} % de puntos` } },
      },
    },
  });
}
$("p-h2h").onclick = (e) => { const tr = e.target.closest("tr[data-key]"); if (tr) location.hash = `#/jugador/${encodeURIComponent(tr.dataset.key)}`; };
$("p-matches").onclick = (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) location.hash = `#/partido/${tr.dataset.id}`; };

// Gráfica: líneas verticales en cada cambio de juego + cursor vertical al pasar el ratón.
const overlayPlugin = {
  id: "overlay",
  afterDatasetsDraw(c, _a, opts) {
    const { ctx, chartArea: a, scales: { x } } = c;
    ctx.save();
    ctx.strokeStyle = css("--text-muted"); ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
    ctx.fillStyle = css("--text-secondary"); ctx.font = "12px system-ui"; ctx.textAlign = "left";
    (opts.games || []).forEach((g, i) => {
      const px = x.getPixelForValue(g.t / 60);
      ctx.beginPath(); ctx.moveTo(px, a.top); ctx.lineTo(px, a.bottom); ctx.stroke();
      ctx.fillText(`Juego ${i + 1}: ${g.points[0]}–${g.points[1]}`, Math.min(px + 4, a.right - 90), a.top + 12);
    });
    const act = c.tooltip?.getActiveElements() || [];
    if (act.length) {
      ctx.setLineDash([]);
      ctx.beginPath(); ctx.moveTo(act[0].element.x, a.top); ctx.lineTo(act[0].element.x, a.bottom); ctx.stroke();
    }
    ctx.restore();
  },
};

function drawChart(pl, games, nm, endT) {
  const pts = (i) => {
    const d = pl.map((p) => ({ x: p.t / 60, y: p.points[i] }));
    // Prolonga el último marcador hasta el instante actual (o el final del vídeo).
    if (d.length) d.push({ x: Math.max(endT ?? 0, pl[pl.length - 1].t) / 60, y: pl[pl.length - 1].points[i] });
    return d;
  };
  const c1 = css("--series-1"), c2 = css("--series-2");
  const data = {
    datasets: [
      { label: nm[0], data: pts(0), borderColor: c1, backgroundColor: c1 },
      { label: nm[1], data: pts(1), borderColor: c2, backgroundColor: c2 },
    ].map((d) => ({ ...d, stepped: true, borderWidth: 2, pointRadius: 0, pointHitRadius: 10, pointHoverRadius: 4 })),
  };
  $("m-legend").innerHTML = `<span><i style="background:${c1}"></i>${esc(nm[0])}</span><span><i style="background:${c2}"></i>${esc(nm[1])}</span>`;
  if (chart) { chart.data = data; chart.options.plugins.overlay.games = games; chart.update("none"); return; }
  const grid = css("--grid"), tick = css("--text-secondary");
  chart = new Chart($("m-chart"), {
    type: "line", data, plugins: [overlayPlugin],
    options: {
      animation: false, maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { type: "linear", title: { display: true, text: "Minuto del vídeo", color: tick },
             grid: { color: grid }, ticks: { color: tick, callback: (v) => fmt(v * 60) } },
        y: { beginAtZero: true, title: { display: true, text: "Puntos en el juego", color: tick },
             grid: { color: grid }, ticks: { color: tick, precision: 0 } },
      },
      plugins: {
        legend: { display: false },
        overlay: { games },
        tooltip: { callbacks: { title: (items) => fmt(items[0].parsed.x * 60) } },
      },
    },
  });
}

// =========================================================== arranque
pollJob().then(route);
