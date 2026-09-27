"use strict";
// WeinerBeamer operator UI.  Plain JS, no build step.

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (s) => { s = Math.max(0, Math.round(s || 0)); return s >= 60 ? `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s` : `${s}s`; };
const round = (v, d = 2) => Math.round(v * 10 ** d) / 10 ** d;

async function api(path, { method = "GET", json, form } = {}) {
  const opt = { method, headers: {} };
  if (json !== undefined) { opt.body = JSON.stringify(json); opt.headers["Content-Type"] = "application/json"; opt.method = method === "GET" ? "POST" : method; }
  if (form) { opt.body = form; opt.method = method === "GET" ? "POST" : method; }
  const r = await fetch(path, opt);
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((body && body.detail) ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)) : `${r.status} ${r.statusText}`);
  return body;
}

function toast(msg, bad = false) {
  const t = document.createElement("div");
  t.className = "toast" + (bad ? " bad" : "");
  t.textContent = msg;
  $("#toasts").append(t);
  setTimeout(() => t.remove(), bad ? 7000 : 3500);
}
const guard = (fn) => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message, true); } };

// ---------------------------------------------------------------------------
const App = {
  meta: null, S: null, view: "queue", kind: "text", editing: null, machineSel: null,
  tpCache: new Map(), lastQueueSig: "", lastStripSig: "", step: 1,
};

// ---- composer field schemas -------------------------------------------------
const MODES = [["outline", "Outline"], ["fill", "Filled"], ["both", "Filled + outline"]];
const FIELDS = {
  text: [
    { k: "text", l: "Text", t: "textarea", d: "GO TEAM!", full: true },
    { k: "font", l: "Font", t: "select", opts: () => App.meta.fonts.map((x) => [x, x]) },
    { k: "mode", l: "Style", t: "select", opts: () => MODES, d: "both" },
    { k: "size_mm", l: "Letter height mm (0 = fit dog)", t: "num", d: 0, step: 0.5 },
    { k: "power", l: "Power %", t: "pct", d: 60 },
    { k: "feed", l: "Base speed mm/min", t: "num", d: 800, step: 50 },
    { k: "border", l: "Border", t: "bool" },
    { k: "rotate_180", l: "Rotate 180°", t: "bool" },
    { k: "fill_power", l: "Fill power %", t: "pct", d: 60, adv: true },
    { k: "fill_feed", l: "Fill speed", t: "num", d: 1200, step: 50, adv: true },
    { k: "hatch_spacing", l: "Fill line spacing mm", t: "num", d: 0.25, step: 0.05, adv: true },
    { k: "hatch_angle", l: "Fill angle °", t: "num", d: 0, adv: true },
    { k: "letter_spacing", l: "Letter spacing (em)", t: "num", d: 0, step: 0.02, adv: true },
    { k: "line_spacing", l: "Line spacing", t: "num", d: 1.15, step: 0.05, adv: true },
  ],
  pattern: [
    { k: "pattern", l: "Pattern", t: "select", opts: () => App.meta.patterns.map((x) => [x, x]), d: "crosshatch" },
    { k: "spacing", l: "Spacing mm", t: "num", d: 2, step: 0.25 },
    { k: "angle", l: "Angle °", t: "num", d: 45, pat: ["crosshatch", "stripes", "grill"] },
    { k: "period", l: "Wavelength mm", t: "num", d: 6, step: 0.5, pat: ["zigzag", "wave"] },
    { k: "rows", l: "Rows", t: "num", d: 1, pat: ["zigzag", "wave"] },
    { k: "bars", l: "Bars", t: "num", d: 5, pat: ["grill"] },
    { k: "bar_width", l: "Bar width mm", t: "num", d: 1.6, step: 0.1, pat: ["grill"] },
    { k: "double", l: "Double line", t: "bool", d: true, pat: ["border"] },
    { k: "power", l: "Power %", t: "pct", d: 60 },
    { k: "feed", l: "Base speed mm/min", t: "num", d: 900, step: 50 },
    { k: "width_mm", l: "Width mm (0 = fit)", t: "num", d: 0, adv: true },
    { k: "height_mm", l: "Height mm (0 = fit)", t: "num", d: 0, adv: true },
    { k: "hatch_spacing", l: "Checker fill spacing", t: "num", d: 0.3, step: 0.05, adv: true, pat: ["checker"] },
  ],
  image: [
    { k: "_file", l: "Image (PNG/JPG, logos work best)", t: "file", full: true },
    { k: "width_mm", l: "Width mm (0 = fit)", t: "num", d: 0 },
    { k: "line_interval", l: "Line interval mm", t: "num", d: 0.2, step: 0.05 },
    { k: "power", l: "Power %", t: "pct", d: 50 },
    { k: "feed", l: "Base speed mm/min", t: "num", d: 1500, step: 50 },
    { k: "dither", l: "Dither (photos)", t: "bool" },
    { k: "invert", l: "Invert", t: "bool" },
    { k: "threshold", l: "Threshold (0-255)", t: "num", d: 128, adv: true },
    { k: "rotate_180", l: "Rotate 180°", t: "bool", adv: true },
  ],
  testgrid: [
    { k: "powers", l: "Powers % (columns)", t: "list", d: "30, 45, 60, 75, 90", pct: true, full: true },
    { k: "feeds", l: "Speeds mm/min (rows)", t: "list", d: "600, 1200", full: true },
    { k: "cell_mm", l: "Cell mm", t: "num", d: 4, step: 0.5 },
    { k: "hatch_spacing", l: "Fill spacing mm", t: "num", d: 0.25, step: 0.05 },
  ],
};

function fieldHTML(f, val) {
  const name = `p.${f.k}`;
  const v = val ?? f.d ?? "";
  const cls = f.full ? ' style="grid-column: 1 / -1"' : "";
  const data = f.pat ? ` data-pat="${f.pat.join(",")}"` : "";
  switch (f.t) {
    case "textarea": return `<label${cls}${data}>${esc(f.l)}<textarea name="${name}" rows="2" maxlength="200">${esc(v)}</textarea></label>`;
    case "select": return `<label${cls}${data}>${esc(f.l)}<select name="${name}">${f.opts().map(([k, l]) => `<option value="${esc(k)}"${k === v ? " selected" : ""}>${esc(l)}</option>`).join("")}</select></label>`;
    case "bool": return `<label class="check"${data}><input type="checkbox" name="${name}"${(val ?? f.d) ? " checked" : ""}> ${esc(f.l)}</label>`;
    case "file": return `<label${cls}${data}>${esc(f.l)}<input type="file" name="_file" accept="image/png,image/jpeg,image/gif,image/webp,image/bmp"></label>`;
    case "pct": return `<label${cls}${data}>${esc(f.l)}<input type="number" name="${name}" min="0" max="100" step="1" value="${esc(v)}"></label>`;
    case "list": return `<label${cls}${data}>${esc(f.l)}<input name="${name}" value="${esc(v)}"></label>`;
    default: return `<label${cls}${data}>${esc(f.l)}<input type="number" name="${name}" step="${f.step ?? 1}" value="${esc(v)}"></label>`;
  }
}

function renderKindFields(params = {}) {
  const fs = FIELDS[App.kind];
  const toVal = (f) => {
    if (!(f.k in params)) return undefined;
    const v = params[f.k];
    if (f.t === "pct") return Math.round(v * 100);
    if (f.t === "list") return (v || []).map((x) => (f.pct ? Math.round(x * 100) : x)).join(", ");
    return v;
  };
  const basic = fs.filter((f) => !f.adv), adv = fs.filter((f) => f.adv);
  $("#kind-fields").innerHTML =
    `<div class="grid2">${basic.map((f) => fieldHTML(f, toVal(f))).join("")}</div>` +
    (adv.length ? `<details class="more"><summary>Advanced</summary><div class="grid2">${adv.map((f) => fieldHTML(f, toVal(f))).join("")}</div></details>` : "");
  if (App.editing && App.kind === "image") {
    $("#kind-fields input[type=file]").insertAdjacentHTML("afterend", `<span class="small muted">Leave empty to keep the current image</span>`);
  }
  syncPatternFields();
}

function syncPatternFields() {
  const pat = $("#job-form [name='p.pattern']")?.value;
  $$("#kind-fields [data-pat]").forEach((el) => el.classList.toggle("hidden", !!pat && !el.dataset.pat.split(",").includes(pat)));
}

function readSpec() {
  const form = $("#job-form");
  const params = {};
  for (const f of FIELDS[App.kind]) {
    if (f.t === "file") continue;
    const el = form.elements[`p.${f.k}`];
    if (!el) continue;
    if (f.t === "bool") params[f.k] = el.checked;
    else if (f.t === "pct") params[f.k] = Math.max(0, Math.min(100, +el.value || 0)) / 100;
    else if (f.t === "num") params[f.k] = +el.value || 0;
    else if (f.t === "list") params[f.k] = el.value.split(/[,\s]+/).filter(Boolean).map(Number).filter((x) => !isNaN(x)).map((x) => (f.pct ? x / 100 : x));
    else params[f.k] = el.value;
  }
  return {
    kind: App.kind, params,
    title: form.elements.title.value.trim() || (params.text ? params.text.split("\n")[0].slice(0, 40) : App.kind),
    customer: form.elements.customer.value.trim(),
    machine_id: form.elements.machine_id.value || null,
    dog_type: form.elements.dog_type.value || null,
    copies: +form.elements.copies.value || 1,
    passes: +form.elements.passes.value || 1,
    hold: form.elements.hold.checked,
  };
}

function specForm(spec) {
  const fd = new FormData();
  fd.append("spec", JSON.stringify(spec));
  const file = $("#job-form input[type=file]")?.files?.[0];
  if (file) fd.append("image", file);
  else if (App.editing) fd.append("job_id", App.editing);
  return fd;
}

let previewTimer = null, previewSeq = 0;
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, 300);
}
async function runPreview() {
  const seq = ++previewSeq;
  const spec = readSpec();
  const info = $("#preview-info");
  if (spec.kind === "image" && !$("#job-form input[type=file]")?.files?.[0] && !App.editing) {
    $("#preview").replaceChildren();
    info.innerHTML = `<span>Choose an image to preview</span>`;
    return;
  }
  try {
    const d = await api("/api/preview", { form: specForm(spec) });
    if (seq !== previewSeq) return;
    Viz.render($("#preview"), d, { key: "preview-" + seq });
    info.innerHTML = previewInfo(d);
  } catch (e) {
    if (seq !== previewSeq) return;
    info.innerHTML = `<span class="warn">${esc(e.message)}</span>`;
  }
}
function previewInfo(d) {
  const bb = d.bbox;
  return `<span>⏱ ~${fmtTime(d.stats.est_seconds)}</span><span>${d.ops.length} strokes</span>` +
    (bb ? `<span>${round(bb[2] - bb[0], 1)} × ${round(bb[3] - bb[1], 1)} mm</span>` : "") +
    `<span>dog ${d.dog.length}×${d.dog.diameter} (${esc(d.dog.type)})</span>` +
    `<span class="legend"><i style="background:var(--zone)"></i>in focus</span>` +
    d.warnings.map((w) => `<span class="warn">⚠ ${esc(w)}</span>`).join("");
}

function setKind(kind, params) {
  App.kind = kind;
  $$("#kind-seg button").forEach((b) => b.classList.toggle("on", b.dataset.kind === kind));
  renderKindFields(params);
  schedulePreview();
}

function fillRoutingSelects() {
  const f = $("#job-form");
  const ms = App.S?.machines || [];
  const cur = f.elements.machine_id.value, curD = f.elements.dog_type.value;
  f.elements.machine_id.innerHTML = `<option value="">Any machine</option>` +
    ms.map((m) => `<option value="${esc(m.config.id)}">${esc(m.config.name)}</option>`).join("");
  f.elements.machine_id.value = ms.some((m) => m.config.id === cur) ? cur : "";
  f.elements.dog_type.innerHTML = `<option value="">Any dog</option>` +
    Object.keys(App.meta.dog_presets).map((k) => `<option value="${k}">${k}</option>`).join("");
  f.elements.dog_type.value = curD;
}

const submitJob = guard(async () => {
  const spec = readSpec();
  if (App.editing) {
    await api(`/api/jobs/${App.editing}/edit`, { form: specForm({ ...spec, requeue: true }) });
    toast(`Job #${App.editing} updated and requeued`);
    endEdit();
  } else {
    const r = await api("/api/jobs", { form: specForm(spec) });
    toast(r.ids.length > 1 ? `Queued ${r.ids.length} copies (#${r.ids[0]}…)` : `Queued #${r.ids[0]}`);
    const f = $("#job-form");
    f.elements.title.value = ""; f.elements.customer.value = ""; f.elements.copies.value = 1;
  }
  poll();
});

function startEdit(job) {
  App.editing = job.id;
  const f = $("#job-form");
  f.elements.title.value = job.title; f.elements.customer.value = job.customer;
  f.elements.machine_id.value = job.machine_id || ""; f.elements.dog_type.value = job.dog_type || "";
  f.elements.passes.value = job.passes; f.elements.copies.value = 1;
  $("#composer-title").textContent = `Editing job #${job.id}`;
  $("#add-job").textContent = "Save & requeue";
  $("#cancel-edit").classList.remove("hidden");
  $$("#kind-seg button").forEach((b) => (b.disabled = b.dataset.kind !== job.kind));
  setKind(job.kind, job.params);
  showView("queue");
  window.scrollTo({ top: 0, behavior: "smooth" });
}
function endEdit() {
  App.editing = null;
  $("#composer-title").textContent = "New dog";
  $("#add-job").textContent = "Add to queue";
  $("#cancel-edit").classList.add("hidden");
  $$("#kind-seg button").forEach((b) => (b.disabled = false));
  setKind(App.kind);
}

// ---- state polling ------------------------------------------------------------
let pollFail = 0;
async function poll() {
  try {
    const fin = $("#show-finished").checked ? 40 : 0;
    App.S = await api(`/api/state?finished=${fin}`);
    if (pollFail > 2) toast("Server is back");
    pollFail = 0;
  } catch (e) {
    if (++pollFail === 3) toast("Lost contact with the WeinerBeamer server - retrying", true);
    return;
  }
  renderStrip();
  renderQueue();
  if (App.view === "machines") renderMachineDetail();
}

const machineById = (id) => App.S?.machines.find((m) => m.config.id === id);
const jobById = (id) => App.S?.jobs.find((j) => j.id === id);

function statePill(m) {
  const st = m.status || {};
  const a = m.activity;
  if (m.config.firmware === "manual") return `<span class="pill">manual export</span>`;
  if (!m.config.enabled) return `<span class="pill">disabled</span>`;
  if (!st.connected) return `<span class="pill bad">offline</span>`;
  if (st.state === "Alarm") return `<span class="pill bad">ALARM</span>`;
  if (st.paused || String(st.state).startsWith("Hold")) return `<span class="pill warn">paused</span>`;
  if (a) return `<span class="pill run">${a.kind === "utility" ? esc(a.label) : "burning"}</span>`;
  return `<span class="pill ok">${esc(st.state || "idle")}</span>`;
}

// Machine head position mapped back into the dog frame of a given slot.
function headInDogFrame(m, slot = 0) {
  const w = m.status?.wpos; if (!w) return null;
  const jig = m.config.jig, ang = -(m.config.align.angle || 0) * Math.PI / 180;
  const x = w[0] - jig.pitch_x * slot, y = w[1] - jig.pitch_y * slot;
  return [x * Math.cos(ang) - y * Math.sin(ang), x * Math.sin(ang) + y * Math.cos(ang)];
}

async function toolpath(jobId, mid) {
  const job = jobById(jobId);
  const key = `${jobId}:${mid}:${job?.updated}`;
  if (!App.tpCache.has(key)) {
    App.tpCache.set(key, api(`/api/jobs/${jobId}/toolpath?machine=${encodeURIComponent(mid || "")}`).catch((e) => { App.tpCache.delete(key); throw e; }));
    if (App.tpCache.size > 40) App.tpCache.delete(App.tpCache.keys().next().value);
  }
  return App.tpCache.get(key);
}

function liveViz(el, m) {
  const a = m.activity;
  if (!a || a.kind !== "batch" || a.job_id == null) { el.replaceChildren(); el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  toolpath(a.job_id, m.config.id).then((d) => {
    Viz.render(el, d, { key: `live-${a.job_id}-${m.config.id}`, doneOp: a.op_done, head: headInDogFrame(m, a.slot) });
  }).catch(() => {});
}

function progressHTML(m) {
  const a = m.activity;
  if (!a) return "";
  if (a.kind === "utility") return `<div class="sub">${esc(a.label)}…</div>`;
  if (a.manual) return `<div class="sub">Waiting for the operator to run exported G-code and mark jobs done.</div>`;
  if (a.job_id == null) return `<div class="sub">starting…</div>`;
  const pct = Math.round((a.fraction || 0) * 100);
  const slots = a.plan?.length || 1;
  return `<div class="sub">#${a.job_id} ${esc(a.title || "")} · slot ${a.slot + 1}${slots > 1 ? ` of ${slots}` : ""}${a.start_op ? " · resumed" : ""}</div>
    <div class="progress" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><div style="width:${pct}%"></div></div>
    <div class="row sub"><span>${pct}%</span><span>stroke ${Math.max(0, a.op_done + 1)} / ${a.total_ops}</span><span>ETA ${fmtTime(a.eta)}</span><span>elapsed ${fmtTime(a.elapsed)}</span></div>`;
}

// ---- darkness / speed dial ----------------------------------------------------
const speedFactor = (m, step) => Math.pow(1 + m.config.speed.step_pct / 100, step ?? m.config.speed.step);
function speedText(m, step) {
  step = step ?? m.config.speed.step;
  const f = speedFactor(m, step);
  const sign = step > 0 ? "+" : "";
  const how = step === 0 ? "base speed" : step < 0 ? "slower · darker" : "faster · lighter";
  return `<b>${sign}${step}</b> <span class="muted">×${f.toFixed(2)} · ${how}</span>`;
}
function speedHTML(m) {
  const st = m.config.speed.step;
  const ov = m.status?.feed_override;
  const live = m.activity?.kind === "batch" && ov && ov !== 100 ? `<span class="pill warn" title="live feed override on the job burning now">live ${ov}%</span>` : "";
  return `<div class="speed" data-mid="${esc(m.config.id)}">
    <div class="row sp-head"><span class="sp-lbl">Speed</span><span class="sp-val">${speedText(m)}</span>${live}</div>
    <div class="sp-ctl">
      <button class="icon" data-sp="-1" title="Slower = darker" ${st <= -10 ? "disabled" : ""}>− slower</button>
      <div class="sp-track">
        <input type="range" min="-10" max="10" step="1" value="${st}" aria-label="Speed dial, -10 slowest to +10 fastest">
        <div class="sp-scale"><span>−10 darker</span><span>0</span><span>lighter +10</span></div>
      </div>
      <button class="icon" data-sp="1" title="Faster = lighter" ${st >= 10 ? "disabled" : ""}>faster +</button>
    </div>
  </div>`;
}
const setSpeed = guard(async (mid, step) => {
  step = Math.max(-10, Math.min(10, Math.round(step)));
  const r = await api(`/api/machines/${mid}/speed`, { json: { step } });
  if (r.note) toast(r.note);
  else if (r.live) toast(`Speed ${step > 0 ? "+" : ""}${step} - applied to the burn in progress (${r.live}%)`);
  App.lastStripSig = "";
  await poll();
  const m = machineById(mid);
  if (m && $("#modal").open) refreshSpeed($("#modal-body"), m);
});
el_on(document, "input", ".speed input[type=range]", (el) => {
  const m = machineById(el.closest(".speed").dataset.mid);
  if (m) el.closest(".speed").querySelector(".sp-val").innerHTML = speedText(m, +el.value);
});
// Debounced so a burst of arrow-key taps sends one change, not one per tap.
let speedTimer = null;
el_on(document, "change", ".speed input[type=range]", (el) => {
  const mid = el.closest(".speed").dataset.mid, v = +el.value;
  App.dragging = true;
  clearTimeout(speedTimer);
  speedTimer = setTimeout(() => { App.dragging = false; setSpeed(mid, v); }, 450);
});
el_on(document, "pointerdown", ".speed input[type=range]", () => (App.dragging = true));
el_on(document, "click", ".speed button[data-sp]", (b) => {
  const box = b.closest(".speed");
  const m = machineById(box.dataset.mid);
  if (m) setSpeed(box.dataset.mid, m.config.speed.step + +b.dataset.sp);
});
function refreshSpeed(root, m) {
  const box = root?.querySelector(`.speed[data-mid="${CSS.escape(m.config.id)}"]`);
  if (!box || box.contains(document.activeElement) && App.dragging) return;
  const fresh = document.createElement("div");
  fresh.innerHTML = speedHTML(m);
  box.replaceWith(fresh.firstElementChild);
}

function renderStrip() {
  const el = $("#machine-strip");
  const ms = App.S.machines;
  const sig = JSON.stringify(ms.map((m) => [m.config.id, m.config.name, m.config.enabled, m.status.connected, m.status.ready, m.status.state,
    m.status.paused, m.status.last_error, m.activity && [m.activity.kind, m.activity.job_id, m.activity.index], m.queued_count, m.config.jig.slots,
    m.config.speed, m.status.feed_override]));
  if (sig !== App.lastStripSig && !App.dragging) {
    App.lastStripSig = sig;
    el.innerHTML = ms.map((m) => {
      const c = m.config, a = m.activity, st = m.status;
      const busy = !!a;
      const n = Math.min(c.jig.slots, m.queued_count);
      const offline = c.firmware !== "manual" && !st.ready;
      return `<div class="mcard" data-mid="${esc(c.id)}">
        <div class="row" style="justify-content:space-between"><span class="name" title="${esc(c.name)}">${esc(c.name)}</span>${statePill(m)}</div>
        <div class="sub">${esc(c.firmware)} · ${esc(c.port)} · ${esc(c.dog.type)} dog · ${c.jig.slots} slot${c.jig.slots > 1 ? "s" : ""}</div>
        <div class="prog"></div>
        <div class="viz mini hidden" style="aspect-ratio: 16/4"></div>
        ${c.firmware === "manual" ? "" : speedHTML(m)}
        ${st.last_error ? `<div class="err">⚠ ${esc(st.last_error)}</div>` : ""}
        <div class="row">
          ${busy && a.kind === "batch" && !a.manual ? `
            <button data-act="${st.paused ? "resume" : "pause"}">${st.paused ? "Resume" : "Pause"}</button>
            <button data-act="abort">Abort</button>` : ""}
          ${busy && a.kind === "utility" ? `<button data-act="abort">Stop</button>` : ""}
          ${!busy ? `<button class="primary" data-act="run" ${n && !offline ? "" : "disabled"}>${n ? `Load & run next ${n}` : "Queue empty"}</button>` : ""}
          <button class="ghost" data-act="align">Align…</button>
        </div>
        <div class="sub">${m.queued_count} queued for this machine</div>
      </div>`;
    }).join("") || `<div class="panel muted">No machines yet - add one in Settings.</div>`;
  }
  ms.forEach((m) => {
    const card = el.querySelector(`[data-mid="${CSS.escape(m.config.id)}"]`);
    if (!card) return;
    card.querySelector(".prog").innerHTML = progressHTML(m);
    liveViz(card.querySelector(".viz"), m);
  });
}

el_on("#machine-strip", "click", "button[data-act]", guard(async (b) => {
  const mid = b.closest("[data-mid]").dataset.mid;
  const act = b.dataset.act;
  if (act === "run") return runModal(mid, null, false);
  if (act === "align") { App.machineSel = mid; return showView("machines"); }
  if (act === "abort" && !confirm("Abort? The laser stops now; the job can be resumed or re-run later.")) return;
  await api(`/api/machines/${mid}/${act}`, { json: {} });
  poll();
}));

// ---- queue ------------------------------------------------------------------
function jobWhat(j) {
  const p = j.params;
  if (j.kind === "text") return `“${esc((p.text || "").replace(/\n/g, " / ").slice(0, 40))}”`;
  if (j.kind === "pattern") return esc(p.pattern);
  if (j.kind === "image") return "image";
  return "test grid";
}

function renderQueue() {
  const jobs = App.S.jobs;
  const names = Object.fromEntries(App.S.machines.map((m) => [m.config.id, m.config.name]));
  const sig = JSON.stringify([jobs.map((j) => [j.id, j.status, j.updated, j.runs]), Object.keys(names), App.selected,
    App.S.machines.map((m) => m.activity?.job_id)]);
  if (sig === App.lastQueueSig) return;
  App.lastQueueSig = sig;
  if (!jobs.length) { $("#queue").innerHTML = `<p class="muted">Nothing queued. Design a dog on the left and hit “Add to queue”.</p>`; return; }
  const rows = jobs.map((j) => {
    const fin = ["done", "failed", "canceled", "interrupted"].includes(j.status);
    const acts = [];
    if (j.status === "queued") acts.push(`<button class="icon" data-a="up" title="Move up">▲</button>`, `<button class="icon" data-a="down" title="Move down">▼</button>`, `<button class="icon" data-a="top" title="Move to top">⤒</button>`, `<button class="icon" data-a="hold">Hold</button>`);
    if (j.status === "held") acts.push(`<button class="icon" data-a="release">Release</button>`);
    if (fin) acts.push(`<button class="icon primary" data-a="rerun" title="Burn this design again (another dog or a do-over)">Run again</button>`);
    if (j.status === "interrupted" && j.last_op >= 0) acts.push(`<button class="icon" data-a="resume" title="Continue from stroke ${j.last_op + 2}">Resume</button>`);
    if (fin) acts.push(`<button class="icon" data-a="requeue" title="Put back in the queue">Requeue</button>`);
    if (j.status !== "running") acts.push(`<button class="icon" data-a="edit">Edit</button>`);
    acts.push(`<button class="icon" data-a="duplicate" title="Duplicate">⧉</button>`);
    if (j.status === "queued" || j.status === "held") acts.push(`<button class="icon" data-a="cancel" title="Cancel">✕</button>`);
    if (j.status === "running" && j.last_machine && machineById(j.last_machine)?.config.firmware === "manual") {
      acts.push(`<a class="icon" href="/api/jobs/${j.id}/gcode?machine=${encodeURIComponent(j.last_machine)}" download><button class="icon">G-code</button></a>`,
        `<button class="icon primary" data-a="mark_done">Done</button>`, `<button class="icon" data-a="mark_failed">Failed</button>`);
    }
    const target = j.machine_id ? esc(names[j.machine_id] || j.machine_id) : `<span class="muted">any</span>`;
    let stat = j.status;
    if (j.status === "running") {
      const act = machineById(j.last_machine)?.activity;
      const slot = act?.plan?.findIndex((p) => p.job_id === j.id) ?? -1;
      const where = esc(names[j.last_machine] || j.last_machine);
      stat = act?.manual ? `exported to ${where}`
        : act?.job_id === j.id ? `burning on ${where}`
        : slot >= 0 ? `loaded in slot ${slot + 1} on ${where}, waiting` : `running on ${where}`;
    }
    return `<tr data-jid="${j.id}" class="${fin ? "finished" : ""} ${App.selected === j.id ? "sel" : ""}">
      <td class="muted">${j.id}</td>
      <td class="title" title="${esc(j.title)}">${esc(j.title)}${j.customer ? ` <span class="muted small">· ${esc(j.customer)}</span>` : ""}</td>
      <td class="hide-sm">${jobWhat(j)}</td>
      <td class="hide-sm">${target}${j.dog_type ? ` <span class="pill">${esc(j.dog_type)}</span>` : ""}</td>
      <td class="status-${j.status}" title="${esc(j.error || "")}">${stat}${j.error && j.status !== "done" ? " ⚠" : ""}</td>
      <td class="hide-sm muted">${j.runs || ""}</td>
      <td class="acts">${acts.join(" ")}</td></tr>`;
  }).join("");
  $("#queue").innerHTML = `<div class="table-scroll"><table><thead><tr><th>#</th><th>Title</th><th class="hide-sm">Design</th><th class="hide-sm">Target</th><th>Status</th><th class="hide-sm">Runs</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

el_on("#queue", "click", "button[data-a], td.title", guard(async (el) => {
  const jid = +el.closest("tr").dataset.jid;
  if (el.matches("td.title")) return jobModal(jid);
  const a = el.dataset.a;
  const job = jobById(jid);
  if (a === "edit") return startEdit(job);
  if (a === "rerun") return rerunModal(job, false);
  if (a === "resume") return rerunModal(job, true);
  if (a === "cancel" && !confirm(`Cancel job #${jid}?`)) return;
  await api(`/api/jobs/${jid}/action`, { json: { action: a } });
  poll();
}));

// ---- modals --------------------------------------------------------------------
function modal(html) {
  const d = $("#modal");
  $("#modal-body").innerHTML = html;
  if (!d.open) d.showModal();
  return $("#modal-body");
}
const closeModal = () => $("#modal").close();

async function jobModal(jid) {
  App.selected = jid;
  const j = await api(`/api/jobs/${jid}`);
  const b = modal(`<h2>#${j.id} ${esc(j.title)}</h2>
    <div class="viz-wrap"><div class="viz" id="jm-viz"></div><div class="viz-info" id="jm-info"></div></div>
    <div class="small">Status: <b class="status-${j.status}">${j.status}</b>${j.error ? ` - <span class="err">${esc(j.error)}</span>` : ""}</div>
    <h3>Runs</h3>
    ${j.run_history.length ? `<table class="small"><tr><th>When</th><th>Machine</th><th>Slot</th><th>Speed</th><th>Result</th><th>Strokes</th></tr>
      ${j.run_history.map((r) => `<tr><td>${new Date(r.started * 1000).toLocaleTimeString()}</td><td>${esc(r.machine_id)}</td><td>${r.slot + 1}</td><td>${r.speed_step > 0 ? "+" : ""}${r.speed_step ?? 0}</td>
      <td title="${esc(r.error || "")}">${esc(r.result || "running")}</td><td>${r.start_op}–${r.last_op + 1} / ${r.total_ops}</td></tr>`).join("")}</table>` : `<p class="muted small">Never run.</p>`}
    <div class="actions"><button data-x="close">Close</button></div>`);
  b.querySelector("[data-x=close]").onclick = closeModal;
  try {
    const d = await api(`/api/jobs/${jid}/toolpath`);
    Viz.render($("#jm-viz"), d, { key: "jm" + jid + Date.now(), doneOp: j.status === "done" ? d.ops.length * j.passes - 1 : j.last_op });
    $("#jm-info").innerHTML = previewInfo(d);
  } catch (e) { $("#jm-info").innerHTML = `<span class="warn">${esc(e.message)}</span>`; }
}

function rerunModal(job, resume) {
  const ms = App.S.machines.filter((m) => m.config.enabled);
  const def = resume ? job.last_machine : (job.machine_id || job.last_machine || ms[0]?.config.id);
  const opts = (resume ? ms.filter((m) => m.config.id === job.last_machine) : ms)
    .map((m) => `<option value="${esc(m.config.id)}"${m.config.id === def ? " selected" : ""}>${esc(m.config.name)}</option>`).join("");
  const b = modal(`<h2>${resume ? "Resume" : "Run again"}: #${job.id} ${esc(job.title)}</h2>
    <p class="muted">${resume
      ? `Continues from stroke ${job.last_op + 2} on the same machine. Only do this if the dog has not moved since it stopped.`
      : "Burns the whole design again - on a fresh dog, or over the same dog to darken it."}</p>
    <label>Machine<select id="rr-m">${opts}</select></label>
    <div class="actions"><button data-x="cancel">Cancel</button><button class="primary" data-x="go">Next: load dog</button></div>`);
  b.querySelector("[data-x=cancel]").onclick = closeModal;
  b.querySelector("[data-x=go]").onclick = () => runModal($("#rr-m").value, [job.id], resume);
}

function runModal(mid, jobIds, resume) {
  const m = machineById(mid);
  if (!m) return;
  const ids = jobIds || m.next_jobs;
  const slots = ids.map((id, i) => {
    const j = jobById(id);
    return `<li>Slot ${i + 1}${i === 0 ? " (at work zero)" : ""}: <b>#${id} ${esc(j?.title || "")}</b></li>`;
  }).join("");
  const manual = m.config.firmware === "manual";
  const b = modal(`<h2>${manual ? "Export" : "Load & burn"} on ${esc(m.config.name)}</h2>
    <ol class="slotlist">${slots}</ol>
    ${manual ? `<p class="muted">This machine has no live link. Download each job's G-code from the queue, run it in your sender, then mark it done or failed.</p>`
      : `<div class="checklist">
        <label class="check"><input type="checkbox" class="ck"> Dog${ids.length > 1 ? "s are" : " is"} on the template/jig in ${ids.length > 1 ? "those slots" : "slot 1"}, straight and centred</label>
        <label class="check"><input type="checkbox" class="ck"> Work zero is on the centre of the dog top and focus (Z) is set</label>
        <label class="check"><input type="checkbox" class="ck"> Area clear · eyewear on / enclosure closed</label>
      </div>
      ${speedHTML(m)}
      <div class="row small"><button data-x="frame">Trace outline first (laser off)</button><span class="muted">Moves around slot 1's design so you can check it lands on the dog.</span></div>`}
    <div class="actions"><button data-x="cancel">Cancel</button><button class="primary" data-x="go" ${manual ? "" : "disabled"}>${manual ? "Start export" : `🔥 Burn ${ids.length} dog${ids.length > 1 ? "s" : ""}`}</button></div>`);
  const go = b.querySelector("[data-x=go]");
  $$(".ck", b).forEach((c) => (c.onchange = () => (go.disabled = !$$(".ck", b).every((x) => x.checked))));
  b.querySelector("[data-x=cancel]").onclick = closeModal;
  const fr = b.querySelector("[data-x=frame]");
  if (fr) fr.onclick = guard(async () => { await api(`/api/machines/${mid}/frame`, { json: { job_id: ids[0], slot: 0 } }); toast("Tracing outline…"); });
  go.onclick = guard(async () => {
    const r = await api(`/api/machines/${mid}/run`, { json: { job_ids: jobIds || ids, resume } });
    closeModal();
    toast(`${manual ? "Exporting" : "Burning"} ${r.plan.length} job(s) on ${m.config.name}`);
    poll();
  });
}

// ---- machines & alignment view ----------------------------------------------
function renderMachineSeg() {
  const ms = App.S?.machines || [];
  if (!App.machineSel || !machineById(App.machineSel)) App.machineSel = ms[0]?.config.id;
  $("#machine-seg").innerHTML = ms.map((m) => `<button data-mid="${esc(m.config.id)}" class="${m.config.id === App.machineSel ? "on" : ""}">${esc(m.config.name)}</button>`).join("");
}
el_on("#machine-seg", "click", "button", (b) => { App.machineSel = b.dataset.mid; App.detailBuiltFor = null; renderMachineSeg(); renderMachineDetail(); });

function buildMachineDetail(m) {
  const c = m.config, manual = c.firmware === "manual";
  const slotOpts = Array.from({ length: c.jig.slots }, (_, i) => `<option value="${i}">slot ${i + 1}</option>`).join("");
  $("#machine-detail").innerHTML = `
  <div class="mdetail">
    <div class="stack">
      <div class="panel">
        <div class="panel-head"><h2>${esc(c.name)}</h2><span id="md-pill"></span></div>
        <div class="sub muted small">${esc(c.firmware)} · ${esc(c.port)} · laser ${esc(c.laser.mode)} S0-${c.laser.s_max} · ${esc(c.dog.type)} dog ${c.dog.length}×${c.dog.diameter} mm</div>
        <div id="md-err" class="err"></div>
        <div id="md-prog" style="margin:8px 0"></div>
        <div class="viz-wrap"><div class="viz" id="md-viz"></div><div class="viz-info" id="md-vinfo"></div></div>
        ${manual ? "" : `<div id="md-speed" style="margin-top:10px">${speedHTML(m)}</div>`}
        <div class="actions">
          <select id="md-job" style="width:auto;max-width:260px"></select>
          <button id="md-show">Show</button>
          <button data-m="run" class="primary">Load &amp; run next</button>
          <button data-m="pause">Pause</button><button data-m="resume">Resume</button><button data-m="abort">Abort</button>
          <button data-m="estop" class="danger">E-STOP</button>
          <button data-m="reconnect" class="ghost">Reconnect</button>
        </div>
      </div>
      ${manual ? "" : `
      <div class="panel wizard">
        <h3>Loose dog (no jig): find centre &amp; angle</h3>
        <ol>
          <li>Pointer on, jog to the <b>left</b> tip of the dog's centreline → <button data-m="mark" data-end="a">Mark left end</button> <span id="md-a" class="muted small"></span></li>
          <li>Jog to the <b>right</b> tip → <button data-m="mark" data-end="b">Mark right end</button> <span id="md-b" class="muted small"></span></li>
          <li><button data-m="center" class="primary">Centre &amp; square to dog</button> moves to the middle, zeroes X/Y and rotates every design to match.</li>
        </ol>
        <div class="row small">Current angle: <b id="md-angle"></b>° <span id="md-mlen" class="muted"></span><button data-m="angle0" class="ghost">Reset angle</button></div>
      </div>
      <div class="panel">
        <h3>Camera overlay</h3>
        <p class="muted small">Point a phone/webcam at the bed. The crosshair and outline help line up dogs by eye.</p>
        <div class="row"><select id="cam-dev" style="width:auto"></select><button id="cam-go">Start camera</button>
          <label class="small">Outline size <input type="range" id="cam-scale" min="10" max="95" value="60" style="width:120px"></label></div>
        <div class="cam hidden" id="cam"><video id="cam-v" autoplay playsinline muted></video>
          <svg viewBox="0 0 100 100" preserveAspectRatio="none"><line x1="50" y1="0" x2="50" y2="100" stroke="#ff3b3b" stroke-width=".3"/>
          <line x1="0" y1="50" x2="100" y2="50" stroke="#ff3b3b" stroke-width=".3"/><rect id="cam-dog" fill="none" stroke="#f2b705" stroke-width=".5" rx="4"/></svg></div>
      </div>`}
    </div>
    ${manual ? `<div class="panel"><h3>Manual machine</h3><p class="muted">No live connection. Jobs routed here are exported as ${esc(c.manual_dialect)} G-code for another sender; mark them done in the queue.</p></div>` : `
    <div class="stack">
      <div class="panel">
        <h3>Position (work)</h3>
        <div class="dro"><div><span>X</span><b id="md-x">-</b></div><div><span>Y</span><b id="md-y">-</b></div><div><span>Z</span><b id="md-z">-</b></div></div>
        <h3 style="margin-top:12px">Jog <span class="muted small" style="text-transform:none">(arrow keys, PgUp/PgDn for Z)</span></h3>
        <div class="row" style="align-items:flex-start;gap:16px">
          <div class="jog">
            <button data-j="-1,1,0">↖</button><button data-j="0,1,0">↑</button><button data-j="1,1,0">↗</button><span></span><button class="zc" data-j="0,0,1">Z+</button>
            <button data-j="-1,0,0">←</button><button data-m="goto_zero" title="Go to work zero">⌖</button><button data-j="1,0,0">→</button><span></span><span></span>
            <button data-j="-1,-1,0">↙</button><button data-j="0,-1,0">↓</button><button data-j="1,-1,0">↘</button><span></span><button class="zc" data-j="0,0,-1">Z−</button>
          </div>
          <div class="stack" style="gap:8px">
            <div class="steps">${[0.1, 1, 5, 10, 50].map((s) => `<button data-step="${s}" class="${s === App.step ? "on" : ""}">${s}</button>`).join("")}</div>
            <label>Feed mm/min<input id="md-feed" type="number" value="1500" step="100"></label>
          </div>
        </div>
      </div>
      <div class="panel">
        <h3>Set up</h3>
        <div class="row">
          <button data-m="home">Home</button><button data-m="unlock">Unlock</button>
          <button data-m="zero" data-axes="XY">Zero X/Y here</button><button data-m="zero" data-axes="Z">Zero Z (focus) here</button>
        </div>
        <div class="row" style="margin-top:8px">
          <button id="md-pointer">Pointer on</button>
          <select id="md-slot" style="width:auto">${slotOpts}</select><button data-m="goto_slot">Go to slot</button>
          <button data-m="frame_zone">Trace in-focus zone</button>
        </div>
        <div class="row" style="margin-top:8px">
          <button data-m="template">Burn placement template…</button>
          <span class="muted small">Tape card/wood to the bed, zero on it, burn ${c.jig.slots} dog outline${c.jig.slots > 1 ? "s" : ""}. Then just set dogs on the outlines.</span>
        </div>
      </div>
      <div class="panel">
        <h3>Console</h3>
        <form id="md-console" class="row" onsubmit="return false"><input id="md-cmd" placeholder="${c.firmware === "grbl" ? "e.g. $$  or  G0 X10" : "e.g. M114  or  G0 X10 F3000"}" maxlength="120" style="flex:1"><button>Send</button></form>
        <div class="log" id="md-log" style="margin-top:8px"></div>
      </div>
    </div>`}
  </div>`;
  App.detailBuiltFor = detailSig(c);
  App.pointerOn = false;
  wireCamera(c);
}

// The dial changes config.speed often; don't rebuild the whole page (and kill the camera) for it.
const detailSig = (c) => c.id + ":" + JSON.stringify({ ...c, speed: undefined });

function renderMachineDetail() {
  renderMachineSeg();
  const m = machineById(App.machineSel);
  if (!m) { $("#machine-detail").innerHTML = `<p class="muted">No machines.</p>`; return; }
  if (App.detailBuiltFor !== detailSig(m.config)) buildMachineDetail(m);
  else if (!App.dragging) refreshSpeed($("#md-speed"), m);
  const st = m.status, a = m.activity, c = m.config;
  $("#md-pill").innerHTML = statePill(m);
  $("#md-err").textContent = st.last_error ? "⚠ " + st.last_error : "";
  $("#md-prog").innerHTML = progressHTML(m);
  const busy = !!a;
  $$("#machine-detail [data-m], #machine-detail [data-j]").forEach((b) => {
    const k = b.dataset.m;
    if (["pause", "resume", "abort", "estop"].includes(k)) b.disabled = !busy && k !== "estop";
    else if (k === "reconnect") b.disabled = busy;
    else b.disabled = busy || !st.ready;
  });
  if (st.wpos && $("#md-x")) {
    $("#md-x").textContent = st.wpos[0].toFixed(2); $("#md-y").textContent = st.wpos[1].toFixed(2); $("#md-z").textContent = st.wpos[2].toFixed(2);
  }
  if ($("#md-angle")) {
    $("#md-angle").textContent = (c.align.angle || 0).toFixed(2);
    $("#md-a").textContent = c.align.end_a ? `(${c.align.end_a.join(", ")})` : "";
    $("#md-b").textContent = c.align.end_b ? `(${c.align.end_b.join(", ")})` : "";
    $("#md-mlen").textContent = c.align.measured_length ? `measured dog ${c.align.measured_length} mm (profile ${c.dog.length})` : "";
  }
  const log = $("#md-log");
  if (log) {
    const txt = (m.log || []).join("\n");
    if (log.textContent !== txt) { const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 8; log.textContent = txt; if (stick) log.scrollTop = log.scrollHeight; }
  }
  // job picker for framing / viewing
  const sel = $("#md-job");
  const eligible = App.S.jobs.filter((j) => j.status !== "canceled" && (!j.machine_id || j.machine_id === c.id));
  const sig = eligible.map((j) => j.id + j.title).join("|");
  if (sel.dataset.sig !== sig) {
    const cur = sel.value;
    sel.innerHTML = `<option value="">(in-focus zone)</option>` + eligible.map((j) => `<option value="${j.id}">#${j.id} ${esc(j.title)} · ${j.status}</option>`).join("");
    sel.value = eligible.some((j) => String(j.id) === cur) ? cur : (m.next_jobs[0] ? String(m.next_jobs[0]) : "");
    sel.dataset.sig = sig;
  }
  // visualizer: the running job, else the picked job, with the live head
  const vz = $("#md-viz");
  const jid = a?.job_id ?? (+sel.value || null);
  const slot = a?.slot ?? (+($("#md-slot")?.value) || 0);
  if (jid) {
    toolpath(jid, c.id).then((d) => {
      Viz.render(vz, d, { key: `md-${jid}-${c.id}`, doneOp: a?.job_id === jid ? a.op_done : -1, head: headInDogFrame(m, slot) });
      $("#md-vinfo").innerHTML = previewInfo(d) + `<span class="legend"><i style="background:var(--done)"></i>burned</span><span class="legend"><i style="background:var(--danger)"></i>head</span>`;
    }).catch((e) => ($("#md-vinfo").innerHTML = `<span class="warn">${esc(e.message)}</span>`));
  } else {
    const [zw, zh] = m.safe_zone;
    Viz.render(vz, { ops: [], bbox: null, dog: c.dog, safe_zone: [zw, zh] }, { key: `md-empty-${c.id}-${JSON.stringify(c.dog)}`, head: headInDogFrame(m, slot) });
    $("#md-vinfo").innerHTML = `<span>No job selected - showing the dog, in-focus zone and head position.</span>`;
  }
}

async function jog(dx, dy, dz) {
  const mid = App.machineSel, s = App.step;
  const feed = +($("#md-feed")?.value) || 1500;
  await api(`/api/machines/${mid}/jog`, { json: { dx: dx * s, dy: dy * s, dz: dz * Math.min(s, 5), feed } });
}

el_on("#machine-detail", "click", "button", guard(async (b) => {
  const mid = App.machineSel;
  const m = machineById(mid);
  if (b.dataset.step) { App.step = +b.dataset.step; $$(".steps button").forEach((x) => x.classList.toggle("on", x === b)); return; }
  if (b.dataset.j) { const [x, y, z] = b.dataset.j.split(",").map(Number); return jog(x, y, z); }
  if (b.id === "md-show") { App.detailBuiltFor = App.detailBuiltFor; return renderMachineDetail(); }
  if (b.id === "md-pointer") {
    if (!App.pointerOn && !confirm("Turn the laser on at pointer power? Wear eyewear - even low power is not eye-safe.")) return;
    App.pointerOn = !App.pointerOn;
    await api(`/api/machines/${mid}/pointer`, { json: { on: App.pointerOn } });
    b.textContent = App.pointerOn ? "Pointer OFF" : "Pointer on";
    b.classList.toggle("danger", App.pointerOn);
    return;
  }
  const k = b.dataset.m;
  if (!k) return;
  if (k === "run") return m.next_jobs.length ? runModal(mid, null, false) : toast("No queued jobs for this machine", true);
  if (k === "abort" && !confirm("Abort the current job?")) return;
  if (k === "template") {
    if (!confirm(`Burn ${m.config.jig.slots} dog outline(s) at ${Math.round(m.config.jig.template_power * 100)}% power onto whatever is under the head, starting at work zero?`)) return;
  }
  if (k === "home" && !confirm("Home the machine? Make sure nothing is in the way.")) return;
  const body = { axes: b.dataset.axes, end: b.dataset.end, slot: +($("#md-slot")?.value || 0) };
  if (k === "frame_zone") { await api(`/api/machines/${mid}/frame`, { json: { job_id: +$("#md-job").value || null, slot: body.slot } }); return toast("Tracing…"); }
  if (k === "angle0") { await api(`/api/machines/${mid}/angle`, { json: { angle: 0 } }); return poll(); }
  const r = await api(`/api/machines/${mid}/${k}`, { json: body });
  if (k === "center") toast(`Centred. Dog angle ${r.angle}°, length ${r.measured_length} mm`);
  if (k === "zero") toast(`Zeroed ${body.axes}`);
  poll();
}));
el_on("#machine-detail", "change", "#md-job, #md-slot", () => renderMachineDetail());
el_on("#machine-detail", "submit", "#md-console", guard(async () => {
  const inp = $("#md-cmd");
  const r = await api(`/api/machines/${App.machineSel}/console`, { json: { line: inp.value } });
  toast(`${inp.value} → ${r.replies.join(" ")}`);
  inp.value = "";
  poll();
}));

// Keyboard: arrows jog on the machines view; Esc twice = E-STOP ALL anywhere.
let lastEsc = 0;
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    const now = Date.now();
    if (now - lastEsc < 800) estopAll();
    lastEsc = now;
    return;
  }
  if (App.view !== "machines" || e.target.closest("input, textarea, select") || $("#modal").open) return;
  const map = { ArrowLeft: [-1, 0, 0], ArrowRight: [1, 0, 0], ArrowUp: [0, 1, 0], ArrowDown: [0, -1, 0], PageUp: [0, 0, 1], PageDown: [0, 0, -1] };
  if (map[e.key]) { e.preventDefault(); guard(jog)(...map[e.key]); }
});

async function wireCamera(c) {
  const go = $("#cam-go");
  if (!go) return;
  const sel = $("#cam-dev");
  const scale = $("#cam-scale");
  try { scale.value = localStorage.getItem("wb-cam-scale") || 60; } catch { /* storage blocked */ }
  const drawDog = () => {
    const w = +scale.value, h = w * c.dog.diameter / c.dog.length * (16 / 9);
    const r = $("#cam-dog");
    r.setAttribute("x", 50 - w / 2); r.setAttribute("y", 50 - h / 2); r.setAttribute("width", w); r.setAttribute("height", h);
  };
  scale.oninput = () => { drawDog(); try { localStorage.setItem("wb-cam-scale", scale.value); } catch { /* ignore */ } };
  drawDog();
  const listDevs = async () => {
    if (!navigator.mediaDevices?.enumerateDevices) { sel.innerHTML = `<option>camera needs https or localhost</option>`; return; }
    const devs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
    sel.innerHTML = devs.map((d, i) => `<option value="${d.deviceId}">${esc(d.label || "Camera " + (i + 1))}</option>`).join("") || `<option value="">default camera</option>`;
  };
  listDevs().catch(() => {});
  go.onclick = guard(async () => {
    const v = $("#cam-v");
    if (v.srcObject) { v.srcObject.getTracks().forEach((t) => t.stop()); v.srcObject = null; $("#cam").classList.add("hidden"); go.textContent = "Start camera"; return; }
    if (!navigator.mediaDevices?.getUserMedia) throw new Error("Camera access needs https or localhost");
    v.srcObject = await navigator.mediaDevices.getUserMedia({ video: sel.value ? { deviceId: { exact: sel.value } } : true });
    $("#cam").classList.remove("hidden");
    go.textContent = "Stop camera";
    listDevs().catch(() => {});
  });
}

// ---- settings: machine editor ----------------------------------------------------
const MSCHEMA = [
  ["Connection", [["name", "text", "Name"], ["enabled", "bool", "Enabled"], ["firmware", "select", "Firmware", ["grbl", "marlin", "manual"]],
    ["port", "text", "Port"], ["baud", "number", "Baud"], ["stall_timeout", "number", "Stall timeout s"], ["manual_dialect", "select", "Export dialect (manual)", ["grbl", "marlin"]]]],
  ["Laser", [["laser.mode", "select", "Laser control", ["m4", "m3", "inline", "fan"]], ["laser.s_max", "number", "Max S value"],
    ["laser.power_scale", "number", "Power scale (calibration)"], ["laser.max_feed", "number", "Max feed mm/min"],
    ["laser.pointer_power", "number", "Pointer power (0-0.05)"], ["laser.frame_feed", "number", "Trace feed mm/min"]]],
  ["Marlin link", [["marlin.checksums", "bool", "Line numbers + checksums"], ["marlin.inflight", "number", "Lines in flight"]]],
  ["Hotdog", [["dog.type", "select", "Dog type", () => Object.keys(App.meta.dog_presets)], ["dog.length", "number", "Length mm"],
    ["dog.diameter", "number", "Diameter mm"], ["dog.focus_tolerance", "number", "Depth of focus ± mm"], ["dog.end_margin", "number", "End margin mm"],
    ["dog.wrap", "bool", "Wrap design around curve"], ["dog.z_follow", "bool", "Z follows the curve"]]],
  ["Speed dial", [["speed.step", "number", "Dial -10 (darker) … +10 (lighter)"], ["speed.step_pct", "number", "% speed change per step"]]],
  ["Jig / template", [["jig.slots", "number", "Slots"], ["jig.pitch_x", "number", "Slot pitch X mm"], ["jig.pitch_y", "number", "Slot pitch Y mm"],
    ["jig.template_power", "number", "Template power (0-1)"], ["jig.template_feed", "number", "Template feed"]]],
];
const getPath = (o, p) => p.split(".").reduce((a, k) => a?.[k], o);
const setPath = (o, p, v) => { const ks = p.split("."); let t = o; ks.slice(0, -1).forEach((k) => (t = t[k] ??= {})); t[ks.at(-1)] = v; };

function machineFormHTML(cfg, isNew) {
  const fields = MSCHEMA.map(([legend, fs]) => `<fieldset><legend>${legend}</legend>${fs.map(([p, t, l, opts]) => {
    const v = getPath(cfg, p);
    if (t === "bool") return `<label class="check"><input type="checkbox" name="${p}"${v ? " checked" : ""}> ${l}</label>`;
    if (t === "select") { const o = typeof opts === "function" ? opts() : opts; return `<label>${l}<select name="${p}">${o.map((x) => `<option${x === v ? " selected" : ""}>${x}</option>`).join("")}</select></label>`; }
    return `<label>${l}<input name="${p}" type="${t}" step="any" value="${esc(v)}"></label>`;
  }).join("")}</fieldset>`).join("");
  return `<form data-id="${esc(cfg.id || "")}" onsubmit="return false">${fields}
    <label>Notes<textarea name="notes">${esc(cfg.notes || "")}</textarea></label>
    <div class="actions">${isNew ? "" : `<button data-s="delete" class="ghost">Delete machine</button>`}<button data-s="save" class="primary">${isNew ? "Add machine" : "Save"}</button></div></form>`;
}

function renderMachineEditor(openNew = false) {
  const ms = App.S?.machines || [];
  const parts = ms.map((m) => `<details class="medit"${m.config.id === (openNew ? null : App.machineSel) ? ' open' : ''}><summary>${esc(m.config.name)} <span class="muted small">${esc(m.config.firmware)} · ${esc(m.config.port)}</span></summary>${machineFormHTML(m.config, false)}</details>`);
  if (openNew) parts.unshift(`<details class="medit" open><summary>New machine</summary>${machineFormHTML({ ...App.meta.machine_defaults, name: "New machine" }, true)}</details>`);
  parts.push(`<div class="row" style="margin-top:8px"><button id="scan-ports">Scan serial ports</button><span id="scan-result" class="muted small"></span></div>`);
  $("#machine-editor").innerHTML = parts.join("");
}

el_on("#machine-editor", "change", "select[name='dog.type']", (sel) => {
  const p = App.meta.dog_presets[sel.value]; const f = sel.form;
  if (p) { f.elements["dog.length"].value = p.length; f.elements["dog.diameter"].value = p.diameter; }
});
el_on("#machine-editor", "change", "select[name='firmware']", (sel) => {
  const f = sel.form;
  if (sel.value === "marlin") { f.elements["laser.mode"].value = "fan"; f.elements["laser.s_max"].value = 255; }
  if (sel.value === "grbl") { f.elements["laser.mode"].value = "m4"; f.elements["laser.s_max"].value = 1000; }
});
el_on("#machine-editor", "click", "button[data-s]", guard(async (b) => {
  const f = b.closest("form");
  if (b.dataset.s === "delete") {
    if (!confirm("Delete this machine? Jobs targeted at it go back to 'any machine'.")) return;
    await api(`/api/machines/${f.dataset.id}`, { method: "DELETE" });
  } else {
    const cfg = f.dataset.id ? { id: f.dataset.id } : {};
    MSCHEMA.forEach(([, fs]) => fs.forEach(([p, t]) => {
      const el = f.elements[p];
      setPath(cfg, p, t === "bool" ? el.checked : t === "number" ? +el.value : el.value);
    }));
    cfg.notes = f.elements.notes.value;
    const m = await api("/api/machines", { json: cfg });
    toast(`Saved ${m.name}`);
  }
  await poll();
  renderMachineEditor();
  fillRoutingSelects();
}));

el_on(document, "click", "#scan-ports", guard(async () => {
  const res = await api("/api/serial-scan");
  const sel = $("#machine-editor select[name='port']");
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = `<option value="">${cur || "(manual)"}</option>` +
    res.map((p) => `<option value="${esc(p.path)}"${p.path === cur ? " selected" : ""}>${esc(p.path)} — ${esc(p.description)}</option>`).join("");
  const result = $("#scan-result");
  if (result) result.textContent = `${res.length} port(s) found`;
  setTimeout(() => { if (result) result.textContent = ""; }, 4000);
}));

async function renderHistory() {
  const runs = await api("/api/runs?limit=200");
  $("#history").innerHTML = runs.length ? `<div class="table-scroll"><table><tr><th>Started</th><th>Job</th><th>Machine</th><th>Slot</th><th>Kind</th><th>Speed</th><th>Result</th><th>Strokes</th><th>Duration</th><th>Error</th></tr>
    ${runs.map((r) => `<tr><td>${new Date(r.started * 1000).toLocaleString()}</td><td>${r.job_id ?? ""}</td><td>${esc(r.machine_id)}</td><td>${r.slot + 1}</td><td>${esc(r.kind)}</td>
      <td>${r.speed_step > 0 ? "+" : ""}${r.speed_step ?? 0} <span class="muted">×${(r.speed_factor ?? 1).toFixed(2)}</span></td>
      <td class="status-${esc(r.result)}">${esc(r.result || "running")}</td><td>${r.start_op}–${r.last_op + 1}/${r.total_ops}</td>
      <td>${r.ended ? fmtTime(r.ended - r.started) : ""}</td><td class="err">${esc(r.error || "")}</td></tr>`).join("")}</table></div>` : `<p class="muted">No runs yet.</p>`;
}

// ---- global ----------------------------------------------------------------------
async function estopAll() {
  try { await api("/api/estop", { json: {} }); toast("E-STOP sent to every machine", true); } catch (e) { toast("E-STOP FAILED: " + e.message + " - cut power!", true); }
  poll();
}

function showView(v) {
  App.view = v;
  $$(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.view === v));
  $$(".view").forEach((s) => s.classList.toggle("on", s.id === "view-" + v));
  if (v === "machines") { App.detailBuiltFor = null; renderMachineDetail(); }
  if (v === "settings") renderMachineEditor();
  if (v === "history") guard(renderHistory)();
}

function el_on(root, ev, sel, fn) {
  document.addEventListener(ev, (e) => {
    const r = typeof root === "string" ? document.querySelector(root) : root;
    const t = e.target.closest(sel);
    if (r && t && r.contains(t)) fn(t, e);
  });
}

async function init() {
  App.meta = await api("/api/meta");
  $$(".tabs button").forEach((b) => (b.onclick = () => showView(b.dataset.view)));
  $$("#kind-seg button").forEach((b) => (b.onclick = () => setKind(b.dataset.kind)));
  $("#estop-all").onclick = estopAll;
  $("#add-job").onclick = submitJob;
  $("#cancel-edit").onclick = endEdit;
  $("#add-machine").onclick = () => renderMachineEditor(true);
  $("#show-finished").onchange = () => { App.lastQueueSig = ""; poll(); };
  $("#job-form").addEventListener("input", (e) => { if (e.target.name === "p.pattern") syncPatternFields(); schedulePreview(); });
  $("#job-form").addEventListener("change", schedulePreview);
  await poll();
  fillRoutingSelects();
  setKind("text");
  setInterval(poll, 700);
}
init().catch((e) => toast("Failed to start: " + e.message, true));
