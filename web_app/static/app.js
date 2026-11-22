"use strict";
/* CaspianX front end. Every label, count and value comes from the API: no hard-coded wells, dates or results. */
const S = { sid: null, sum: null, wells: [], well: null, scope: "field", model: null, pta: [], interp: null, ev: null,
  sort: { k: "skin", d: -1 }, range: "all", lastDay: null, firstDay: null, layers: { band: true, tests: true, shut: true, a: false, b: false },
  mlayers: { shut: true, bu: true }, vfmData: null, syncing: false };
const C = { oil: "#f2a446", water: "#4f8ff0", gas: "#a8d8ff", red: "#e0503a", gold: "#ffc861", teal: "#749faa", teal2: "#9cc3cc",
  ink: "#ffffff", muted: "rgba(255,255,255,0.66)", dim: "rgba(255,255,255,0.44)", grid: "rgba(255,255,255,0.07)", line: "rgba(255,255,255,0.16)",
  bg: "#0d1314", bg2: "#0a1011", tech: "#76a8ff", sand: "#ffe2a3" };
const PH = ["oil", "water", "gas"];
const UNIT = { oil: "STB/d", water: "STB/d", gas: "Mscf/d" };
const $ = (q) => document.querySelector(q);
const $$ = (q) => Array.from(document.querySelectorAll(q));
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const isNum = (v) => v !== null && v !== undefined && v !== "" && Number.isFinite(Number(v));
const fmt = (v, d = 0) => (isNum(v) ? Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }) : "N/A");
const sgn = (v, d = 2) => (isNum(v) ? (v > 0 ? "+" : "") + fmt(v, d) : "N/A");
const CFG = { displaylogo: false, responsive: true, modeBarButtonsToRemove: ["lasso2d", "select2d", "autoScale2d"] };
const AX = (o = {}) => { const t = Object.assign({ font: { size: 12, color: C.muted } }, o.title || {});
  return Object.assign({ gridcolor: C.grid, zerolinecolor: C.grid, linecolor: C.line, tickfont: { family: "JetBrains Mono, monospace", size: 11, color: C.muted } }, o, { title: t }); };
function lay(o = {}) {
  return Object.assign({ paper_bgcolor: C.bg, plot_bgcolor: C.bg, font: { family: "Archivo, Inter, Segoe UI, sans-serif", color: C.ink, size: 12 },
    margin: { l: 70, r: 20, t: 16, b: 40 }, xaxis: AX(), yaxis: AX(), showlegend: true,
    legend: { orientation: "h", y: 1.02, yanchor: "bottom", x: 0, font: { size: 12, color: C.muted }, bgcolor: "rgba(0,0,0,0)" },
    hoverlabel: { bgcolor: "#111a1c", bordercolor: C.line, font: { family: "JetBrains Mono, monospace", size: 12, color: C.ink } },
    hovermode: "x unified" }, o);
}
const LOGAX = (title) => AX({ type: "log", title: { text: title }, dtick: 1, exponentformat: "power", showexponent: "all",
  minor: { ticks: "inside", ticklen: 3, tickcolor: C.line, showgrid: true, gridcolor: "rgba(255,255,255,0.03)" } });

function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove("show"), 10000); }
function progress(pct, text, err = false) {
  const p = $("#prog"); p.style.display = pct === null ? "none" : "block"; if (pct !== null) p.firstElementChild.style.width = Math.max(2, Math.min(100, pct)) + "%";
  const t = $("#progTxt"); t.textContent = text || ""; t.className = "progtxt" + (err ? " err" : "");
}
function netError(e) {
  return (e && /fetch|network|load failed/i.test(e.message || "")) ?
    "The server did not answer (it may have been closed, restarted or run out of memory). Check the server window, start it again and re-upload." : (e.message || String(e));
}
async function api(path, opts, retried) {
  let r; try { r = await fetch(path, opts); } catch (e) { throw new Error(netError(e)); }
  // Serverless hosts can route a request to a fresh instance that never saw this session: reload the same input once.
  if (r.status === 404 && !retried && S.source && /[?&]sid=/.test(path)) {
    await resync(); return api(path.replace(/sid=[^&]+/, "sid=" + S.sid), opts, true);
  }
  let j; try { j = await r.json(); } catch (e) { throw new Error(`HTTP ${r.status}: the server returned a non-JSON response`); }
  if (!r.ok || j.ok === false) throw new Error(j.error || j.detail || `HTTP ${r.status}`);
  return j;
}
function dayShapes(days, color, opacity = 0.2) {
  return (days || []).map((d) => ({ type: "rect", xref: "x", yref: "paper", x0: d + "T00:00", x1: d + "T23:59", y0: 0, y1: 1, fillcolor: color, opacity, line: { width: 0 }, layer: "below" }));
}
function xRange() {
  if (S.range === "all" || !S.lastDay) return null;
  const end = new Date(S.lastDay + "T23:59:00Z"); const start = new Date(end.getTime() - (+S.range) * 86400000);
  const first = new Date(S.firstDay + "T00:00:00Z");
  return [(start < first ? first : start).toISOString().slice(0, 10), S.lastDay + "T23:59"];
}
function applyRange(o) { const r = xRange(); if (r) { o.range = r; o.autorange = false; } else o.autorange = true; return o; }
function syncZoom(id, others = []) {
  const g = document.getElementById(id);
  if (!g || g.__synced) return; g.__synced = true;
  g.on("plotly_relayout", (ev) => {
    if (S.syncing) return;
    const r0 = ev["xaxis.range[0]"], r1 = ev["xaxis.range[1]"], auto = ev["xaxis.autorange"];
    if (r0 === undefined && !auto) return;
    S.syncing = true;
    const upd = auto ? { "xaxis.autorange": true } : { "xaxis.range": [r0, r1] };
    others.forEach((o) => { const e = document.getElementById(o); if (e && e.data) Plotly.relayout(e, upd); });
    setTimeout(() => (S.syncing = false), 50);
  });
}

// ------------------------------------------------------------------ data loading
async function resync() {
  const j = S.source.kind === "demo" ? await api("/api/demo", { method: "POST" }, true) : await uploadJob(S.source.files, true);
  S.sid = j.sid;
}
async function uploadJob(files, quiet) {
  const j = await uploadXHR(files, quiet);
  if (j.state === "done") return j.result;
  if (j.state === "error") throw new Error(j.error);
  if (!quiet) progress(100, "Upload complete · processing");
  return waitJob(j.job);
}
async function waitJob(jid) {
  for (;;) {
    await new Promise((r) => setTimeout(r, 1200));
    const j = await api(`/api/job/${jid}`);
    if (j.state === "done") return j.result;
    if (j.state === "error") throw new Error(j.error);
    progress(100, `Processing · ${j.stage || "working"} · ${fmt(j.elapsed_s, 0)} s`);
  }
}
function uploadXHR(files, quiet) {
  return new Promise((resolve, reject) => {
    const fd = new FormData(); let total = 0;
    Array.from(files).forEach((f) => { fd.append("files", f, f.name); total += f.size; });
    const x = new XMLHttpRequest(); x.open("POST", "/api/upload");
    x.upload.onprogress = (e) => { if (e.lengthComputable && !quiet) progress((e.loaded / e.total) * 100, `Uploading ${fmt(e.loaded / 1e6, 1)} / ${fmt(e.total / 1e6, 1)} MB`); };
    x.onload = () => { let j; try { j = JSON.parse(x.responseText); } catch (e) { return reject(new Error(`HTTP ${x.status}: non-JSON response`)); }
      if (x.status >= 400 || j.ok === false) return reject(new Error(j.error || j.detail || `HTTP ${x.status}`)); resolve(j); };
    x.onerror = () => reject(new Error(netError(new Error("network"))));
    if (!quiet) progress(0, `Uploading ${files.length} file(s) · ${fmt(total / 1e6, 1)} MB`);
    x.send(fd);
  });
}
async function upload(files) {
  if (!files || !files.length) return;
  try { const keep = Array.from(files); const res = await uploadJob(keep); S.source = { kind: "files", files: keep }; await apply(res); }
  catch (e) { progress(null); progress(null, e.message, true); toast(e.message); }
}
async function demo() {
  try { progress(100, "Generating synthetic demo field"); const res = await api("/api/demo", { method: "POST" }); S.source = { kind: "demo" }; await apply(res); }
  catch (e) { progress(null, e.message, true); toast(e.message); }
}
async function apply(j) {
  S.sid = j.sid; S.sum = j; S.wells = j.wells; S.well = j.wells[0]; S.ev = null; S.firstDay = j.period[0]; S.lastDay = j.period[1];
  $("#well").innerHTML = j.wells.map((w) => `<option>${esc(w)}</option>`).join(""); $("#well").disabled = false;
  $$("[data-exp]").forEach((b) => (b.disabled = false));
  header();
  const [m, p, it] = await Promise.all([S.model || api("/api/model"), api(`/api/pta?sid=${S.sid}`), api(`/api/interpret?sid=${S.sid}`)]);
  S.model = m; S.pta = p.events; S.interp = it;
  progress(null, `${j.dataset_kind === "DEMO" ? "Demo" : "Field data"} loaded · ${j.wells.length} wells · ${j.n_buildups} build-ups`);
  await Promise.all([overview(), measurements(), vfm()]);
  pta();
}
function header() {
  const j = S.sum;
  $("#dsPill").innerHTML = `<b>${j.wells.length}</b> wells · <b>${esc(j.period[0])}</b> → <b>${esc(j.period[1])}</b> · <b>${j.n_buildups}</b> build-ups`;
  const b = $("#dsBadge"); b.textContent = j.dataset_kind; b.className = "badge " + (j.dataset_kind === "DEMO" ? "demo" : "field");
  const notes = (j.warnings || []).length; const n = $("#notesCnt"); n.textContent = notes; n.className = "cnt" + (notes ? "" : " zero");
  $("#drawerBody").innerHTML = `<h5 class="label">Engineering notes (${notes})</h5><ul>${(j.warnings || []).map((w) => `<li>${esc(w)}</li>`).join("") || "<li>none</li>"}</ul>
    <h5 class="label">Input check</h5><ul>${(j.messages || []).map(([lv, m]) => `<li><span class="sev ${lv === "ok" ? "info" : lv === "bad" ? "alert" : "warn"}">${esc(lv)}</span> ${esc(m)}</li>`).join("")}</ul>
    <h5 class="label">Source</h5><ul><li>${esc(j.source)}</li></ul>`;
  $("#railFoot").textContent = `${j.dataset_kind === "DEMO" ? "Synthetic demo" : "Field data"} · ${j.well_days} well-days`;
}

// ------------------------------------------------------------------ 01 overview
async function overview() {
  const field = S.scope === "field";
  $("#ovScope").textContent = field ? `· ${S.wells.length} wells` : `· ${S.well}`;
  $$("#ovSeg button").forEach((b) => b.classList.toggle("on", b.dataset.s === S.scope));
  const v = await api(`/api/vfm?sid=${S.sid}&well=${encodeURIComponent(field ? "ALL" : S.well)}`);
  const i = v.dates.length - 1;
  const q = { oil: v.oil.rate[i], water: v.water.rate[i], gas: v.gas.rate[i] };
  const wc = q.oil + q.water > 0 ? (q.water / (q.oil + q.water)) * 100 : null;
  const gor = q.oil > 0 ? (q.gas * 1000) / q.oil : null;
  const evs = S.pta.filter((e) => field || e.well === S.well);
  const al = (S.interp.alerts || []).filter((a) => ["alert", "warn"].includes(a.severity) && (field || a.well === S.well));
  $("#ovDay").textContent = `latest day ${v.dates[i] || "N/A"}`;
  const k = [["oil", "Oil", fmt(q.oil), "STB/d", "vfm"], ["water", "Water", fmt(q.water), "STB/d", "vfm"], ["gas", "Gas", fmt(q.gas), "Mscf/d", "vfm"],
    ["teal", "Water cut", fmt(wc, 1), "% · derived from rates", "vfm"], ["teal", "GOR", fmt(gor), "scf/STB · derived from rates", "vfm"],
    ["gold", "Build-ups · alerts", `${evs.length} <span class="c-dim">·</span> <span class="${al.length ? "c-red" : ""}">${al.length}</span>`, "detected · open", "pta"]];
  $("#kpis").innerHTML = k.map(([c, l, val, u, go]) => `<button class="kpi" data-go="${go}"><div class="label"><span class="dot" style="background:var(--${c === "teal" ? "teal" : c})"></span>${l}</div>
    <div class="v ${c === "teal" ? "" : "c-" + c}">${val}</div><div class="u">${u}</div></button>`).join("");
  $$("#kpis .kpi").forEach((b) => b.addEventListener("click", () => { if (!field) return tab(b.dataset.go); if (b.dataset.go === "pta") return tab("pta"); tab("vfm"); }));
  $("#ovChartScope").textContent = field ? "· field total" : `· ${S.well}`;
  const tr = [
    { x: v.dates, y: v.oil.rate, name: "Oil, STB/d", line: { color: C.oil, width: 2 }, hovertemplate: "%{y:,.0f} STB/d" },
    { x: v.dates, y: v.water.rate, name: "Water, STB/d", line: { color: C.water, width: 1.8 }, hovertemplate: "%{y:,.0f} STB/d" },
    { x: v.dates, y: v.gas.rate, name: "Gas, Mscf/d", yaxis: "y2", line: { color: C.gas, width: 1.8 }, hovertemplate: "%{y:,.0f} Mscf/d" }];
  Plotly.react("ovChart", tr, lay({ yaxis: AX({ title: { text: "Oil · water, STB/d" }, rangemode: "tozero", domain: [0.4, 1] }),
    yaxis2: AX({ title: { text: "Gas, Mscf/d" }, rangemode: "tozero", domain: [0, 0.32] }), xaxis: applyRange(AX({ type: "date", anchor: "y2" })),
    margin: { l: 76, r: 20, t: 30, b: 44 }, shapes: field ? [] : dayShapes(v.shutin_days, C.red) }), CFG);
  queue(field);
}

function queue(field) {
  const evs = S.pta.filter((e) => field || e.well === S.well).slice().sort((a, b) => (b.skin ?? -1e9) - (a.skin ?? -1e9));
  const al = (S.interp.alerts || []).filter((a) => field || a.well === S.well)
    .sort((a, b) => ({ alert: 0, warn: 1, info: 2 }[a.severity] ?? 3) - ({ alert: 0, warn: 1, info: 2 }[b.severity] ?? 3));
  const open = al.filter((a) => a.severity !== "info");
  $("#qCount").textContent = `${evs.length} build-ups · ${open.length} alerts`;
  let h = `<div class="qsec">Build-ups · ranked by skin</div>`;
  if (evs.length) {
    h += evs.map((e) => `<div class="qitem" data-ev="${e.id}"><div class="t1"><b class="mono">${esc(e.well)}</b><span>${esc(e.t_shut)} · ${esc(e.class || "")}</span></div>
      <div class="vals"><span class="sym">k</span> ${fmt(e.k_md, 1)} mD &nbsp; <span class="sym">S</span> ${sgn(e.skin)} &nbsp; <span class="sym">m</span> ${fmt(e.m_psi_cycle, 2)} psi/cycle &nbsp; <span class="sym">Δp<sub>skin</sub></span> ${fmt(e.dp_skin_psi, 1)} psi</div></div>`).join("");
  } else if (!field && S.pta.length) {
    const first = S.pta.slice().sort((a, b) => (b.skin ?? -1e9) - (a.skin ?? -1e9))[0];
    h += `<div class="na">No build-up detected for <b>${esc(S.well)}</b> · ${S.pta.length} build-up(s) found field-wide.<br>
      <button class="btn" id="qField">Switch to Field view</button><button class="btn" id="qJump">Open ${esc(first.well)} build-up</button></div>`;
  } else {
    h += `<div class="na">No build-up record in the loaded data.</div>`;
  }
  h += `<div class="qsec" style="margin-top:12px">Alerts</div>`;
  h += al.length ? al.map((a) => `<div class="qitem" data-alw="${esc(a.well)}" data-alt="${esc(a.type)}"><div class="t1"><span class="sev ${esc(a.severity)}">${esc(a.severity)}</span><b class="mono">${esc(a.well)}</b><span>${esc(a.type)}</span></div>
      <div class="msg">${esc(a.message)}</div></div>`).join("") : `<div class="na">No alerts${field ? "" : " for " + esc(S.well)}.</div>`;
  $("#queue").innerHTML = h;
  $$("#queue [data-ev]").forEach((d) => d.addEventListener("click", () => openEvent(+d.dataset.ev)));
  $$("#queue [data-alw]").forEach((d) => d.addEventListener("click", () => {
    const w = d.dataset.alw; const t = d.dataset.alt; const ev = S.pta.find((e) => e.well === w);
    if (t === "high skin" && ev) return openEvent(ev.id);
    if (S.wells.includes(w)) { setWell(w); tab(t === "erosional" || t === "data quality" ? "vfm" : "meas"); }
  }));
  const qf = $("#qField"); if (qf) qf.addEventListener("click", () => { S.scope = "field"; overview(); });
  const qj = $("#qJump"); if (qj) qj.addEventListener("click", () => { const first = S.pta.slice().sort((a, b) => (b.skin ?? -1e9) - (a.skin ?? -1e9))[0]; openEvent(first.id); });
}

// ------------------------------------------------------------------ 02 measurements
async function measurements() {
  $("#measScope").textContent = `· ${S.well}`;
  const m = await api(`/api/measurements?sid=${S.sid}&well=${encodeURIComponent(S.well)}`);
  const keys = Object.keys(m.series); const n = keys.length; const gap = 0.02;
  const colors = { "WHP": C.oil, "Downstream": C.sand, "P_wf": C.gold, "Choke": C.tech, "Gas lift": C.gas, "Uptime": C.teal2 };
  const col = (k) => Object.entries(colors).find(([p]) => k.startsWith(p))?.[1] || C.water;
  const tr = keys.map((k, i) => ({ x: m.dates, y: m.series[k], name: k, yaxis: "y" + (i ? i + 1 : ""), line: { width: 1.6, color: col(k) },
    hovertemplate: `%{y:,.2f} ${esc(m.units[k] || "")}<extra>${esc(k.split(",")[0])}</extra>` }));
  const L = lay({ showlegend: false, margin: { l: 96, r: 20, t: 10, b: 40 }, height: 108 * n + 60 });
  keys.forEach((k, i) => {
    const ax = AX({ domain: [1 - (i + 1) / n + gap, 1 - i / n - gap], title: { text: k.replace(", ", "<br>"), font: { size: 11, color: C.muted }, standoff: 6 } });
    if (k.startsWith("Uptime")) Object.assign(ax, { range: [0, 1.05], autorange: false, dtick: 0.5 });
    if (k.startsWith("Choke")) Object.assign(ax, { rangemode: "tozero" });
    L["yaxis" + (i ? i + 1 : "")] = ax;
  });
  L.xaxis = applyRange(AX({ anchor: "y" + n, type: "date" }));
  L.shapes = (S.mlayers.shut ? dayShapes(m.shutin_days, C.red, 0.25) : []).concat(S.mlayers.bu ? (m.buildups || []).map((b) => ({ type: "rect", xref: "x", yref: "paper", x0: b.start, x1: b.end, y0: 0, y1: 1,
    fillcolor: C.gold, opacity: 0.14, line: { width: 1, color: C.gold, dash: "dot" }, layer: "below" })) : []);
  const el = document.getElementById("measChart"); el.style.height = (108 * n + 60) + "px";
  Plotly.react("measChart", tr, L, CFG);
  $("#measNote").textContent = (m.temperature_available ? "" : "Temperature is not in this dataset's daily sensor file (shown automatically when present). ") +
    `${m.shutin_days.length} shut-in day(s) and ${m.buildups.length} build-up record(s) for ${S.well}.`;
}

// ------------------------------------------------------------------ 03 VFM
function vfmMetrics() {
  const V = S.model.validation || {}; const fw = V.forward_90d || {}; const lo = V.leave_one_well_out || {}; const cov = V["p10_p90_coverage_%"] || {};
  const val = (o, p) => o[p]?.["MAPE_%"] ?? o[p];
  const row = (o, d = 2) => PH.map((p) => `<span class="c-${p}"><i>${p}</i>${fmt(val(o, p), d)}${isNum(val(o, p)) ? "%" : ""}</span>`).join("");
  const tr = S.model.training;
  $("#vfmMetrics").innerHTML = `
    <div class="metric"><div class="label">Forward-CV MAPE · 90-day horizon · n = ${fw.oil?.n ?? "N/A"}</div><div class="row">${row(fw)}</div></div>
    <div class="metric"><div class="label">Unseen well (LOWO) MAPE · n = ${lo.oil?.n ?? "N/A"}</div><div class="row">${row(lo)}</div></div>
    <div class="metric"><div class="label">P10–P90 coverage · nominal 80 %</div><div class="row">${row(cov, 1)}</div></div>
    <div class="metric"><div class="label">Training set</div><div class="row"><span>${tr.tests} <i>tests</i></span><span>${tr.wells} <i>wells</i></span></div>
      <div class="note mono" style="margin-top:2px">${esc(tr.first_test)} → ${esc(tr.last_test)}</div></div>`;
}
async function vfm() {
  vfmMetrics(); method();
  $("#vfmScope").textContent = `· ${S.well}`;
  S.vfmData = await api(`/api/vfm?sid=${S.sid}&well=${encodeURIComponent(S.well)}`);
  $$("#vfmLayers .chip").forEach((c) => { const l = c.dataset.l; c.classList.toggle("on", !!S.layers[l]);
    c.disabled = (l === "a" && !S.vfmData.layers?.model_a) || (l === "b" && !S.vfmData.layers?.model_b) || (l === "tests" && !S.vfmData.tests.length); });
  drawVfm(); residuals(S.vfmData);
}
function drawVfm() {
  const v = S.vfmData; if (!v) return; const Ly = S.layers;
  const tr = []; const L = lay({ showlegend: false, margin: { l: 76, r: 20, t: 20, b: 40 } });
  PH.forEach((ph, i) => {
    const ya = "y" + (i ? i + 1 : ""); const cd = v.dates.map((_, j) => [v[ph].p10[j], v[ph].p90[j]]);
    if (Ly.band) {
      tr.push({ x: v.dates, y: v[ph].p90, yaxis: ya, line: { width: 0 }, hoverinfo: "skip" });
      tr.push({ x: v.dates, y: v[ph].p10, yaxis: ya, fill: "tonexty", fillcolor: C[ph] + "33", line: { width: 0 }, hoverinfo: "skip" });
    }
    if (Ly.a && v.layers?.model_a) tr.push({ x: v.dates, y: v.layers.model_a[ph], yaxis: ya, line: { color: C.ink, width: 1, dash: "dot" }, hovertemplate: `model A %{y:,.1f}<extra></extra>` });
    if (Ly.b && v.layers?.model_b) tr.push({ x: v.dates, y: v.layers.model_b[ph], yaxis: ya, line: { color: C.teal2, width: 1, dash: "dash" }, hovertemplate: `model B %{y:,.1f}<extra></extra>` });
    tr.push({ x: v.dates, y: v[ph].rate, yaxis: ya, customdata: cd, line: { color: C[ph], width: 2 },
      hovertemplate: `<b>${ph}</b> %{y:,.1f} ${UNIT[ph]}` + (Ly.band ? ` · P10 %{customdata[0]:,.1f} · P90 %{customdata[1]:,.1f}` : "") + `<extra></extra>` });
    if (Ly.tests && v.tests.length) tr.push({ x: v.tests.map((t) => t.date), y: v.tests.map((t) => t[ph]), yaxis: ya, mode: "markers",
      marker: { color: C.bg, size: 8, line: { color: C[ph], width: 2 } }, hovertemplate: `separator test %{y:,.1f} ${UNIT[ph]}<extra></extra>` });
    L["yaxis" + (i ? i + 1 : "")] = AX({ domain: [1 - (i + 1) / 3 + 0.035, 1 - i / 3 - 0.035], title: { text: `${ph}, ${UNIT[ph]}` }, rangemode: "tozero" });
  });
  L.xaxis = applyRange(AX({ anchor: "y3", type: "date" }));
  L.shapes = Ly.shut ? dayShapes(v.shutin_days, C.red, 0.25) : []; L.annotations = [];
  const tl = v.train_last;
  if (tl && v.dates.length && tl >= v.dates[0] && tl < v.dates[v.dates.length - 1]) {
    L.shapes.push({ type: "line", xref: "x", yref: "paper", x0: tl, x1: tl, y0: 0, y1: 1, line: { color: C.gold, width: 1, dash: "dash" } });
    L.annotations.push({ x: tl, y: 1.02, xref: "x", yref: "paper", text: "last training test →", showarrow: false, xanchor: "left", font: { family: "JetBrains Mono", size: 11, color: C.gold } });
  } else if (tl && v.dates.length && tl < v.dates[0]) {
    L.annotations.push({ x: 0, y: 1.02, xref: "paper", yref: "paper", text: `prediction period · after the last training test (${tl})`, showarrow: false, xanchor: "left", font: { family: "JetBrains Mono", size: 11, color: C.gold } });
  }
  Plotly.react("vfmChart", tr, L, CFG);
}
function residuals(v) {
  $("#residScope").textContent = `· ${S.well}`;
  const r = v.residuals || [];
  if (!r.length) { $("#residWrap").innerHTML = `<div class="na">No separator test for ${esc(S.well)} inside the loaded period, so there is nothing to compare here. Validated out-of-sample accuracy is in the forward-CV card above.</div>`; $("#residNote").textContent = ""; return; }
  const last = S.model.training.last_test; const ins = r.every((x) => x.date <= last);
  $("#residNote").textContent = ins ? "in-sample · these tests were used for training" : "includes tests after the training period";
  const mape = (p) => { const e = r.map((x) => x[p].err_pct).filter(isNum).map(Math.abs); return e.length ? e.reduce((a, b) => a + b, 0) / e.length : null; };
  $("#residWrap").innerHTML = `<table><tr><th>Test date</th>${PH.map((p) => `<th class="n">${p} pred</th><th class="n">${p} test</th><th class="n">err %</th>`).join("")}</tr>
    ${r.map((x) => `<tr><td class="mono">${esc(x.date)}</td>${PH.map((p) => `<td class="n c-${p}">${fmt(x[p].pred, 1)}</td><td class="n">${fmt(x[p].test, 1)}</td><td class="n ${Math.abs(x[p].err_pct) > 10 ? "c-red" : ""}">${sgn(x[p].err_pct, 1)}</td>`).join("")}</tr>`).join("")}
    <tr><td class="label">MAPE</td>${PH.map((p) => `<td></td><td></td><td class="n c-gold">${fmt(mape(p), 2)}</td>`).join("")}</tr></table>`;
}
function method() {
  const m = S.model; if (!m) return;
  $("#methodGrid").innerHTML = `
    <div class="mbox"><h5>Model A · choke physics + Ridge</h5><div class="eq">${esc(m.model_a.equation)}</div><div class="eq">${esc(m.model_a.split)}</div>
      <p>${esc(m.model_a.calibration)}</p><p>Residual features</p><ul>${m.model_a.features.map((f) => `<li class="mono">${esc(f)}</li>`).join("")}</ul></div>
    <div class="mbox"><h5>Model B · direct 3-target Ridge</h5><div class="eq">${esc(m.model_b.equation)}</div><p>Ridge alpha <span class="mono">${esc(m.model_b.alpha ?? "N/A")}</span>, chosen on an inner time split</p>
      <p>Features</p><ul>${m.model_b.features.map((f) => `<li class="mono">${esc(f)}</li>`).join("")}</ul></div>
    <div class="mbox"><h5>Blend · uncertainty</h5><div class="eq">${esc(m.blend.equation)}</div>
      <p class="mono">w_A · ${PH.map((p) => `<span class="c-${p}">${p} ${m.blend.w_A[p]}</span>`).join(" · ")}</p>
      <p>${esc(m.band.basis)}</p><p class="mono">forward band scale · ${PH.map((p) => `${p} ${m.band.forward_scale[p]}`).join(" · ")}</p>
      <p>${esc(m.file)} · ${m.training.tests} tests · ${m.training.wells} wells · trend horizon ${m.trend_horizon_days} d</p></div>`;
}

// ------------------------------------------------------------------ 04 PTA
function shadeX(x0, x1) { return { type: "rect", xref: "x", yref: "paper", x0, x1, y0: 0, y1: 1, fillcolor: C.teal, opacity: 0.18, line: { width: 0 }, layer: "below" }; }
function pta() {
  const sel = $("#evSel"); const note = $("#ptaNotice");
  sel.innerHTML = S.pta.map((e) => `<option value="${e.id}">${esc(e.well)} · shut-in ${esc(e.t_shut)}</option>`).join("") || "<option>no build-up in the loaded data</option>";
  sel.disabled = !S.pta.length;
  if (!S.pta.length) {
    note.classList.remove("show"); $("#sig").innerHTML = ""; ["pLog", "pHor", "pMdh", "pCart"].forEach((id) => Plotly.purge(id));
    $("#eqs").innerHTML = `<div class="na">No build-up record in the loaded data (needs shut-in pressure records or a per-minute file).</div>`;
    $("#interp").innerHTML = ""; $("#ptaSrc").textContent = ""; wellTable(); return;
  }
  const own = S.pta.filter((e) => e.well === S.well);
  if (S.ev === null || !S.pta.find((e) => e.id === S.ev)) S.ev = (own[0] || S.pta[0]).id;
  const cur = S.pta.find((e) => e.id === S.ev);
  if (!own.length) {
    note.innerHTML = `Selected well <b class="mono">${esc(S.well)}</b> has no build-up event — showing <b class="mono">${esc(cur.well)}</b>. <button class="btn" id="ptaToWell">Show ${esc(cur.well)} everywhere</button>`;
    note.classList.add("show");
    $("#ptaToWell").addEventListener("click", () => setWell(cur.well));
  } else note.classList.remove("show");
  sel.value = S.ev; drawEvent(); wellTable();
}
function openEvent(id) { S.ev = id; const e = S.pta.find((x) => x.id === id); if (e && S.wells.includes(e.well) && e.well !== S.well) setWell(e.well, false); tab("pta"); pta(); }

function drawEvent() {
  const e = S.pta.find((x) => x.id === S.ev); if (!e) return;
  const [w0, w1] = e.window_hr; const hasW = isNum(w0) && isNum(w1);
  const demo = S.sum.dataset_kind === "DEMO";
  $("#ptaSrc").innerHTML = demo ? `<span class="sev info">synthetic demo</span> analytic curve: R² near 1 is expected` : `<span class="sev warn">field gauge</span> raw downhole pressure points`;
  $("#ptaFlag").innerHTML = e.not_reported ? `<span class="c-red">not reported · ${esc(e.not_reported)}</span>` : (e.flags ? esc(e.flags) : "");
  const t = [["k", "", fmt(e.k_md, 1) + " mD", "permeability", 1], ["S", "", sgn(e.skin), "skin", 1], ["m", "", fmt(e.m_psi_cycle, 2) + " psi/cycle", "Horner slope", 1],
    ["Δp", "skin", fmt(e.dp_skin_psi, 1) + " psi", "skin pressure drop", 1], ["p*", "", fmt(e.p_star_psi, 1) + " psia", "extrapolated pressure"],
    ["Δt", "MTR", hasW ? `${fmt(w0, 2)}–${fmt(w1, 2)} h` : "N/A", `radial window · n = ${e.n_points ?? "N/A"}`],
    ["R", "2", fmt(e.r2, 4), `RMSE ${fmt(e.rmse_psi, 3)} psi · n = ${e.n_points ?? "N/A"}`], ["q", "used", fmt(e.q_oil_stbd, 1) + " STB/d", "VFM oil rate"]];
  $("#sig").innerHTML = t.map(([s, sub, v, nm, key]) => `<div class="tile ${key ? "key" : ""}"><span class="sym">${esc(s)}${sub ? (s === "R" ? `<sup>${sub}</sup>` : `<sub>${sub}</sub>`) : ""}</span><div class="val" title="${esc(v)}">${v}</div><div class="nm">${nm}</div></div>`).join("");
  const iarf = e.m_psi_cycle / 2.303;
  const base = { margin: { l: 66, r: 16, t: 34, b: 78 }, hovermode: "closest", legend: { orientation: "h", y: -0.28, yanchor: "top", x: 0, font: { size: 11, color: C.muted } } };
  const title = (s) => ({ text: s, font: { size: 13, color: C.ink }, x: 0.015, xanchor: "left", y: 0.97 });
  Plotly.react("pLog", [
    { x: e.te_hr, y: e.dp, mode: "markers", marker: { size: 4, color: C.water }, name: "ΔP = p_ws − p_wf", hovertemplate: "Δt_e %{x:.3f} h · ΔP %{y:.2f} psi<extra></extra>" },
    { x: e.te_hr, y: e.deriv, mode: "markers", marker: { size: 4, color: C.oil }, name: "Bourdet derivative (L = 0.1)", hovertemplate: "Δt_e %{x:.3f} h · derivative %{y:.2f} psi<extra></extra>" },
    ...(hasW ? [{ x: [w0, w1], y: [iarf, iarf], mode: "lines", line: { dash: "dash", color: C.gold, width: 1.5 }, name: "IARF level m / 2.303", hoverinfo: "skip" }] : [])],
    lay(Object.assign({}, base, { title: title("Log-log diagnostic"), xaxis: LOGAX("equivalent time Δt_e, h"), yaxis: LOGAX("ΔP and derivative, psi"), shapes: hasW ? [shadeX(w0, w1)] : [] })), CFG);
  const inW = e.dt_hr.map((d) => hasW && d >= w0 && d <= w1);
  Plotly.react("pHor", [
    { x: e.horner_ratio, y: e.pws, mode: "markers", marker: { size: 4, color: inW.map((b) => (b ? C.oil : C.gas)) }, name: "p_ws (radial window in amber)", hovertemplate: "(t_p+Δt)/Δt %{x:.3s} · p_ws %{y:.1f} psia<extra></extra>" },
    { x: e.horner_ratio, y: e.horner_line, mode: "lines", line: { color: C.sand, width: 1.3 }, name: `Horner line · m = ${fmt(e.m_psi_cycle, 2)} psi/cycle`, hoverinfo: "skip" }],
    lay(Object.assign({}, base, { title: title("Horner semilog"), xaxis: Object.assign(LOGAX("Horner time ratio (t_p + Δt)/Δt"), { autorange: "reversed" }), yaxis: AX({ title: { text: "p_ws, psia" } }) })), CFG);
  Plotly.react("pMdh", [{ x: e.dt_hr, y: e.pws, mode: "markers", marker: { size: 4, color: inW.map((b) => (b ? C.oil : C.tech)) }, name: "p_ws", hovertemplate: "Δt %{x:.3f} h · %{y:.1f} psia<extra></extra>" }],
    lay(Object.assign({}, base, { title: title("MDH"), showlegend: false, margin: { l: 66, r: 16, t: 34, b: 48 }, xaxis: LOGAX("shut-in time Δt, h"), yaxis: AX({ title: { text: "p_ws, psia" } }), shapes: hasW ? [shadeX(w0, w1)] : [] })), CFG);
  Plotly.react("pCart", [{ x: e.dt_hr, y: e.pws, mode: "lines", line: { color: C.gas, width: 1.8 }, name: "p_ws", hovertemplate: "Δt %{x:.2f} h · %{y:.1f} psia<extra></extra>" }],
    lay(Object.assign({}, base, { title: title("Cartesian build-up"), showlegend: false, margin: { l: 66, r: 16, t: 34, b: 48 }, xaxis: AX({ title: { text: "shut-in time Δt, h" } }), yaxis: AX({ title: { text: "p_ws, psia" } }), shapes: hasW ? [shadeX(w0, w1)] : [] })), CFG);
  equations(e); interpretation(e);
}
function equations(e) {
  const p = e.pvt || {};
  const ok = [e.q_oil_stbd, p.Bo, p.mu, e.m_psi_cycle, p.h_ft].every(isNum);
  const ok2 = [e.p1hr_psi, e.pwf_psi, e.m_psi_cycle, e.k_md, p.phi, p.mu, p.ct, p.rw_ft].every(isNum);
  $("#eqs").innerHTML = `
    <div class="eqrow"><div class="f">k = 162.6 · q · B · μ / (m · h)</div>
      <div class="s">${ok ? `162.6 × ${fmt(e.q_oil_stbd, 1)} × ${fmt(p.Bo, 3)} × ${fmt(p.mu, 3)} / (${fmt(e.m_psi_cycle, 3)} × ${fmt(p.h_ft, 1)})` : "inputs unavailable"}</div><div class="r">k = ${fmt(e.k_md, 2)} mD</div></div>
    <div class="eqrow"><div class="f">s = 1.151 · [ (p<sub>1h</sub> − p<sub>wf</sub>) / m − log₁₀( k / (φ μ c<sub>t</sub> r<sub>w</sub>²) ) + 3.23 ]</div>
      <div class="s">${ok2 ? `p1h ${fmt(e.p1hr_psi, 1)} · p_wf ${fmt(e.pwf_psi, 1)} psia · m ${fmt(e.m_psi_cycle, 3)} · k ${fmt(e.k_md, 2)} · φ ${fmt(p.phi, 3)} · μ ${fmt(p.mu, 3)} cp · c_t ${Number(p.ct).toExponential(3)} 1/psi · r_w ${fmt(p.rw_ft, 3)} ft` : "inputs unavailable"}</div><div class="r">S = ${sgn(e.skin, 3)}</div></div>
    <div class="eqrow"><div class="f">Δp<sub>skin</sub> = 0.87 · m · s</div><div class="s">0.87 × ${fmt(e.m_psi_cycle, 4)} × ${fmt(e.skin, 4)}</div><div class="r">Δp_skin = ${fmt(e.dp_skin_psi, 2)} psi</div></div>
    <div class="eqrow"><div class="f">Fit quality on the radial window</div><div class="s">n = ${e.n_points ?? "N/A"} raw points · R² = ${fmt(e.r2, 4)} · RMSE = ${fmt(e.rmse_psi, 3)} psi</div></div>
    <div class="eqrow"><div class="f">q = VFM oil rate on the last full-uptime day before shut-in · t<sub>p</sub> = ${fmt(e.tp_hr, 0)} h</div>
      <div class="s">rate source ${esc(e.q_source_date || "N/A")} · build-up ${fmt(e.duration_hr, 1)} h · C (data) ${isNum(e.C_bbl_psi) ? Number(e.C_bbl_psi).toExponential(3) : "N/A"} bbl/psi</div></div>`;
}
function interpretation(e) {
  const it = (S.interp.events || []).find((x) => x.id === e.id);
  $("#interpCls").textContent = it ? `${it.well} · ${it.class || ""}` : "";
  $("#interp").innerHTML = it && it.lines.length ? `<ul>${it.lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>` : `<div class="na">N/A</div>`;
}
function wellTable() {
  const rows = (S.interp?.wells || []).slice();
  const cols = [["well", "Well", "t"], ["t_shut", "Last build-up", "t"], ["k_md", "k, mD", "n", 1], ["skin", "Skin S", "n", 2, true], ["dp_skin_psi", "Δp skin, psi", "n", 1],
    ["class", "Condition", "t"], ["dq_stb_d", "Gain if S → 0, STB/d", "n", 0], ["erosional_ratio", "v / v_erosional", "n", 2], ["n_alerts", "Alerts", "n", 0]];
  const { k, d } = S.sort;
  rows.sort((a, b) => { const x = a[k], y = b[k]; if (x === null || x === undefined) return 1; if (y === null || y === undefined) return -1; return (x > y ? 1 : x < y ? -1 : 0) * d; });
  $("#wellTable").innerHTML = `<tr>${cols.map(([c, l, al]) => `<th class="${al} sortable" data-k="${c}">${l}${k === c ? (d > 0 ? " ▲" : " ▼") : ""}</th>`).join("")}</tr>` +
    rows.map((r) => `<tr class="click ${r.well === S.well ? "sel" : ""}" data-w="${esc(r.well)}" data-ev="${r.event_id ?? ""}">${cols.map(([c, , al, dec, signed]) => {
      let v = r[c]; let cls = al;
      if (al === "n") { if (c === "erosional_ratio" && isNum(v) && v >= 0.8) cls += " c-red"; if (c === "skin" && isNum(v) && v >= 2) cls += " c-gold"; v = (v === null || v === undefined) ? "–" : (signed ? sgn(v, dec) : fmt(v, dec)); }
      else v = esc(v ?? "–");
      return `<td class="${cls}">${v}</td>`;
    }).join("")}</tr>`).join("");
  $$("#wellTable th.sortable").forEach((th) => th.addEventListener("click", () => { const c = th.dataset.k; S.sort = { k: c, d: S.sort.k === c ? -S.sort.d : -1 }; wellTable(); }));
  $$("#wellTable tr.click").forEach((tr) => tr.addEventListener("click", () => { const ev = tr.dataset.ev; if (ev !== "") openEvent(+ev); else { setWell(tr.dataset.w); tab("vfm"); } }));
  const al = S.interp?.alerts || [];
  $("#alertCount").textContent = `${al.filter((a) => a.severity !== "info").length} open · ${al.length} total`;
  $("#alertTable").innerHTML = `<tr><th>Severity</th><th>Well</th><th>Type</th><th>Detail</th></tr>` +
    (al.map((a) => `<tr class="click" data-w="${esc(a.well)}"><td><span class="sev ${esc(a.severity)}">${esc(a.severity)}</span></td><td class="mono">${esc(a.well)}</td><td>${esc(a.type)}</td><td style="white-space:normal;color:var(--muted)">${esc(a.message)}</td></tr>`).join("") || `<tr><td class="c-dim" colspan="4">No alerts.</td></tr>`);
  $$("#alertTable tr.click").forEach((tr) => tr.addEventListener("click", () => { const w = tr.dataset.w; const ev = S.pta.find((e) => e.well === w); if (ev) openEvent(ev.id); else if (S.wells.includes(w)) { setWell(w); tab("vfm"); } }));
}

// ------------------------------------------------------------------ navigation & controls
function tab(name) {
  $$(".tabs button").forEach((x) => x.classList.toggle("on", x.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("on", t.id === "tab-" + name));
  $$("#tab-" + name + " .js-plotly-plot").forEach((g) => Plotly.Plots.resize(g));
  window.scrollTo({ top: 0 });
}
function setWell(w, redrawPta = true) {
  S.well = w; $("#well").value = w;
  const own = S.pta.filter((e) => e.well === w); if (own.length) S.ev = own[0].id;
  const p = [measurements(), vfm()]; if (S.scope === "well") p.push(overview()); else queue(true);
  Promise.all(p).catch((e) => toast(e.message));
  if (redrawPta) pta();
}
function rerangeAll() {
  [["ovChart"], ["measChart"], ["vfmChart"]].forEach(([id]) => { const g = document.getElementById(id); if (g && g.data) Plotly.relayout(g, xRange() ? { "xaxis.range": xRange(), "xaxis.autorange": false } : { "xaxis.autorange": true }); });
}
$$(".tabs button").forEach((b) => b.addEventListener("click", () => tab(b.dataset.tab)));
$("#well").addEventListener("change", (e) => setWell(e.target.value));
$("#evSel").addEventListener("change", (e) => { S.ev = +e.target.value; drawEvent(); });
$$("#ovSeg button").forEach((b) => b.addEventListener("click", () => { if (!S.sid) return; S.scope = b.dataset.s; overview().catch((e) => toast(e.message)); }));
$$("#rangeSeg button").forEach((b) => b.addEventListener("click", () => { S.range = b.dataset.r; $$("#rangeSeg button").forEach((x) => x.classList.toggle("on", x === b)); if (S.sid) rerangeAll(); }));
$$("#vfmLayers .chip").forEach((c) => c.addEventListener("click", () => { if (c.disabled) return; S.layers[c.dataset.l] = !S.layers[c.dataset.l]; c.classList.toggle("on", S.layers[c.dataset.l]); drawVfm(); }));
$$("[data-ml]").forEach((c) => c.addEventListener("click", () => { S.mlayers[c.dataset.ml] = !S.mlayers[c.dataset.ml]; c.classList.toggle("on", S.mlayers[c.dataset.ml]); if (S.sid) measurements(); }));
$("#btnDemo").addEventListener("click", demo);
$("#file").addEventListener("change", (e) => { upload(e.target.files); e.target.value = ""; });
const drop = $("#drop");
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => upload(e.dataTransfer.files));
$$("[data-exp]").forEach((b) => b.addEventListener("click", async () => { if (!S.sid) return;
  try {
    let r = await fetch(`/api/export/${b.dataset.exp}?sid=${S.sid}`);
    if (r.status === 404 && S.source) { await resync(); r = await fetch(`/api/export/${b.dataset.exp}?sid=${S.sid}`); }
    if (!r.ok) throw new Error(`Export failed (HTTP ${r.status})`);
    const name = (/filename="?([^";]+)"?/.exec(r.headers.get("Content-Disposition") || "") || [])[1] || `caspianx_${b.dataset.exp}`;
    const url = URL.createObjectURL(await r.blob()); const a = document.createElement("a"); a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch (e) { toast(e.message.includes("fetch") ? netError(e) : e.message); } }));
$("#btnNotes").addEventListener("click", () => $("#drawer").classList.toggle("open"));
$("#drawerX").addEventListener("click", () => $("#drawer").classList.remove("open"));
// keep zoom in sync between the overview, telemetry and VFM charts
window.addEventListener("load", () => setTimeout(() => {}, 0));
const _react = Plotly.react;
Plotly.react = function (id, ...rest) { const r = _react.call(Plotly, id, ...rest); const g = typeof id === "string" ? document.getElementById(id) : id;
  if (g && ["ovChart", "measChart", "vfmChart"].includes(g.id)) Promise.resolve(r).then(() => syncZoom(g.id, ["ovChart", "measChart", "vfmChart"].filter((x) => x !== g.id))); return r; };
api("/api/health").then((h) => { if (!h.model_loaded) toast("Model file missing: web_app/models/vfm_model_FINAL.pkl"); }).catch((e) => toast(e.message));
