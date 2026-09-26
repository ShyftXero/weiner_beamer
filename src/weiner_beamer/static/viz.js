// Toolpath visualizer: hotdog outline, in-focus zone, travel moves, burn
// strokes, completed strokes and the live head position.  Coordinates are the
// dog frame in mm (origin = centre of the dog's top, +Y across); SVG y is flipped.
"use strict";

const Viz = (() => {
  const NS = "http://www.w3.org/2000/svg";
  let uid = 0;
  const f = (v) => (Math.round(v * 100) / 100).toString();

  function stadium(len, dia) {
    const r = dia / 2, h = Math.max(len / 2 - r, 0);
    return `M${f(-h)},${f(-r)} L${f(h)},${f(-r)} A${f(r)},${f(r)} 0 0 1 ${f(h)},${f(r)} ` +
           `L${f(-h)},${f(r)} A${f(r)},${f(r)} 0 0 1 ${f(-h)},${f(-r)} Z`;
  }

  // Build once per toolpath; cheap updates afterwards.
  function render(el, data, opts = {}) {
    const key = opts.key ?? JSON.stringify([data.ops.length, data.bbox, data.dog]);
    let st = el._viz;
    if (!st || st.key !== key) {
      st = el._viz = build(el, data, opts, key);
    }
    update(st, opts);
    return st;
  }

  function build(el, data, opts, key) {
    const dog = data.dog, [zw, zh] = data.safe_zone;
    const L = dog.length, D = dog.diameter;
    const bb = data.bbox || [-L / 2, -D / 2, L / 2, D / 2];
    const x0 = Math.min(-L / 2, bb[0]) - 6, x1 = Math.max(L / 2, bb[2]) + 6;
    const y0 = Math.min(-D / 2, bb[1]) - 6, y1 = Math.max(D / 2, bb[3]) + 6;
    const segs = data.ops.map((o) => {
      let s = "";
      o.pts.forEach((p, i) => { s += (i ? "L" : "M") + f(p[0]) + "," + f(-p[1]); });
      return s;
    });
    let travel = "";
    if (opts.travel !== false && data.ops.length < 6000) {
      let last = [0, 0];
      for (const o of data.ops) {
        const p = o.pts[0];
        travel += `M${f(last[0])},${f(-last[1])}L${f(p[0])},${f(-p[1])}`;
        last = o.pts[o.pts.length - 1];
      }
    }
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", `${f(x0)} ${f(-y1)} ${f(x1 - x0)} ${f(y1 - y0)}`);
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", `toolpath: ${data.ops.length} strokes on a ${L} x ${D} mm dog`);
    const gid = `dogshade${++uid}`;  // ids are document-global; one per svg
    svg.innerHTML = `
      <defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="var(--dog)" stop-opacity=".75"/>
        <stop offset=".5" stop-color="var(--dog)"/>
        <stop offset="1" stop-color="var(--dog)" stop-opacity=".75"/></linearGradient></defs>
      <path d="${stadium(L, D)}" fill="url(#${gid})" stroke="var(--dog-edge)" stroke-width="1" vector-effect="non-scaling-stroke"/>
      <rect x="${f(-zw / 2)}" y="${f(-zh / 2)}" width="${f(zw)}" height="${f(zh)}" fill="none"
        stroke="var(--zone)" stroke-width="1" stroke-dasharray="4 3" vector-effect="non-scaling-stroke" opacity=".8"/>
      <path class="travel" d="${travel}" fill="none" stroke="var(--travel)" stroke-width=".6"
        stroke-dasharray="2 2" vector-effect="non-scaling-stroke" opacity=".6"/>
      <path class="todo" fill="none" stroke="var(--burn)" stroke-width="1.3" stroke-linecap="round"
        stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
      <path class="done" fill="none" stroke="var(--done)" stroke-width="1.6" stroke-linecap="round"
        stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
      <line x1="-3" y1="0" x2="3" y2="0" stroke="var(--zone)" stroke-width="1" vector-effect="non-scaling-stroke"/>
      <line x1="0" y1="-3" x2="0" y2="3" stroke="var(--zone)" stroke-width="1" vector-effect="non-scaling-stroke"/>
      <g class="head" visibility="hidden">
        <circle r="2.2" fill="none" stroke="var(--danger)" stroke-width="1.5" vector-effect="non-scaling-stroke"/>
        <circle r=".6" fill="var(--danger)"/>
      </g>`;
    el.replaceChildren(svg);
    return { key, svg, segs, n: data.ops.length, todo: svg.querySelector(".todo"),
             done: svg.querySelector(".done"), head: svg.querySelector(".head"), lastDone: null };
  }

  function update(st, opts) {
    // done = index of the last completed stroke across passes; show it mod n
    let done = opts.doneOp ?? -1;
    const passesDone = st.n ? Math.floor((done + 1) / st.n) : 0;
    const within = st.n ? (done + 1) % st.n : 0;
    const cut = passesDone > 0 && within === 0 ? st.n : within;
    if (st.lastDone !== cut) {
      st.done.setAttribute("d", st.segs.slice(0, cut).join(""));
      st.todo.setAttribute("d", st.segs.slice(cut).join(""));
      st.lastDone = cut;
    }
    if (opts.head) {
      st.head.setAttribute("visibility", "visible");
      st.head.setAttribute("transform", `translate(${f(opts.head[0])},${f(-opts.head[1])})`);
    } else {
      st.head.setAttribute("visibility", "hidden");
    }
  }

  return { render };
})();
