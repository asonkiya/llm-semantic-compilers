"""Self-contained HTML visualization of the component graph.

Writes a single ``viz.html`` with the component data embedded as JSON and
a hand-rolled canvas force layout — no CDN, no network requests, matching
the local-first rule for the analysis layers. Open it with any browser.

Interactions: drag nodes, wheel-zoom, drag-background to pan, hover for a
tooltip, click for a detail panel, search box to highlight, legend to
toggle component kinds.
"""

from __future__ import annotations

import json
from pathlib import Path

from cgir.ir.component_spec import ComponentSpec


def write(out_dir: Path, specs: list[ComponentSpec]) -> Path:
    """Write ``<out_dir>/viz.html`` and return its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    data = _build_data(specs)
    html = _TEMPLATE.replace("__CGIR_JSON__", json.dumps(data, sort_keys=True))
    path = out_dir / "viz.html"
    path.write_text(html)
    return path


def _build_data(specs: list[ComponentSpec]) -> dict[str, object]:
    index_of = {spec.id: i for i, spec in enumerate(specs)}
    nodes = []
    for spec in specs:
        file = spec.trace[0].rsplit(":", 1)[0] if spec.trace else ""
        nodes.append(
            {
                "id": spec.id,
                "kind": spec.kind.value,
                "purity": spec.purity,
                "effects": spec.effects,
                "inputs": spec.inputs,
                "calls": spec.calls,
                "file": file,
                "trace": spec.trace,
                "signature": spec.signature,
            }
        )
    edges = []
    for spec in specs:
        for callee in spec.calls:
            target = index_of.get(callee)
            if target is not None:
                edges.append({"s": index_of[spec.id], "t": target})
    return {"nodes": nodes, "edges": edges}


_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CGIR component graph</title>
<style>
  html, body { margin: 0; height: 100%; overflow: hidden;
    font: 13px/1.4 -apple-system, "Segoe UI", Roboto, sans-serif;
    background: #0f1420; color: #dbe2ef; }
  #canvas { display: block; cursor: grab; }
  #topbar { position: fixed; top: 0; left: 0; right: 0; display: flex;
    gap: 12px; align-items: center; padding: 10px 14px;
    background: rgba(15,20,32,.88); border-bottom: 1px solid #232c44; }
  #topbar h1 { font-size: 14px; margin: 0; font-weight: 600; color: #8ea6d8; }
  #search { flex: 0 0 260px; padding: 5px 10px; border-radius: 6px;
    border: 1px solid #2c3757; background: #171e30; color: #dbe2ef; }
  #legend { display: flex; gap: 10px; flex-wrap: wrap; }
  .lg { display: flex; gap: 5px; align-items: center; cursor: pointer;
    opacity: 1; user-select: none; }
  .lg.off { opacity: .3; }
  .dot { width: 10px; height: 10px; border-radius: 50%; }
  #detail { position: fixed; top: 52px; right: 0; bottom: 0; width: 320px;
    background: rgba(18,24,40,.95); border-left: 1px solid #232c44;
    padding: 16px; overflow-y: auto; display: none; }
  #detail h2 { font-size: 13px; word-break: break-all; color: #9fd3a8;
    margin: 0 0 8px; }
  #detail dt { color: #8ea6d8; margin-top: 10px; font-size: 11px;
    text-transform: uppercase; letter-spacing: .05em; }
  #detail dd { margin: 2px 0 0; word-break: break-all; }
  #detail .close { position: absolute; top: 8px; right: 12px; cursor: pointer;
    color: #8ea6d8; }
  #tooltip { position: fixed; pointer-events: none; background: #1c2540;
    border: 1px solid #2c3757; border-radius: 6px; padding: 6px 10px;
    display: none; max-width: 380px; word-break: break-all; z-index: 10; }
  #hint { position: fixed; bottom: 10px; left: 14px; color: #56618a;
    font-size: 11px; }
</style>
</head>
<body>
<div id="topbar">
  <h1>CGIR component graph</h1>
  <input id="search" placeholder="search components…" autocomplete="off">
  <div id="legend"></div>
</div>
<canvas id="canvas"></canvas>
<div id="tooltip"></div>
<div id="detail"><span class="close" id="close">✕</span><div id="detail-body"></div></div>
<div id="hint">drag nodes · wheel to zoom · drag background to pan · click a node for details</div>
<script>
const DATA = /*CGIR_DATA*/__CGIR_JSON__/*END_CGIR_DATA*/;

const KIND_COLORS = {
  pure_function: "#68d391",
  orchestrator: "#63b3ed",
  state_transformer: "#f6ad55",
  effect_adapter: "#fc8181",
  unknown: "#a0aec0",
};

const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");
let W = 0, H = 0;
function resize() {
  W = window.innerWidth; H = window.innerHeight;
  canvas.width = W * devicePixelRatio; canvas.height = H * devicePixelRatio;
  canvas.style.width = W + "px"; canvas.style.height = H + "px";
  ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
}
window.addEventListener("resize", () => { resize(); kick(); });
resize();

// --- graph model -----------------------------------------------------------
const nodes = DATA.nodes.map((n, i) => {
  const angle = (i / Math.max(DATA.nodes.length, 1)) * Math.PI * 2;
  const r = 120 + (i % 7) * 60;
  return { ...n, x: W / 2 + Math.cos(angle) * r, y: H / 2 + Math.sin(angle) * r,
           vx: 0, vy: 0, deg: 0, fixed: false };
});
const edges = DATA.edges.map(e => ({ s: e.s, t: e.t }));
edges.forEach(e => { nodes[e.s].deg++; nodes[e.t].deg++; });
nodes.forEach(n => { n.r = 5 + Math.min(11, Math.sqrt(n.deg) * 2.4); });

// group pull: components in the same file drift together
const fileCenters = {};
nodes.forEach(n => {
  if (!fileCenters[n.file]) fileCenters[n.file] = { x: 0, y: 0, count: 0 };
});

const kindVisible = {};
Object.keys(KIND_COLORS).forEach(k => kindVisible[k] = true);

let view = { x: 0, y: 0, scale: 1 };
let alpha = 1;
function kick() { alpha = Math.max(alpha, 0.35); }

// --- physics ----------------------------------------------------------------
function step() {
  if (alpha < 0.005) return;
  alpha *= 0.985;
  const vis = nodes.filter(n => kindVisible[n.kind]);
  // repulsion — exact for small graphs, sampled for large ones so a
  // several-thousand-node repo stays interactive
  if (vis.length > 1200) {
    const K = 25, boost = 3;
    for (let i = 0; i < vis.length; i++) {
      const a = vis[i];
      if (a.fixed) continue;
      for (let k = 0; k < K; k++) {
        const b = vis[(Math.random() * vis.length) | 0];
        if (b === a) continue;
        let dx = a.x - b.x, dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 1; }
        if (d2 > 250000) continue;
        const f = (2600 * boost / d2) * alpha;
        const d = Math.sqrt(d2);
        a.vx += (dx / d) * f; a.vy += (dy / d) * f;
      }
    }
  } else {
    for (let i = 0; i < vis.length; i++) {
      const a = vis[i];
      for (let j = i + 1; j < vis.length; j++) {
        const b = vis[j];
        let dx = a.x - b.x, dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 1; }
        if (d2 > 250000) continue;
        const f = (2600 / d2) * alpha;
        const d = Math.sqrt(d2);
        const fx = (dx / d) * f, fy = (dy / d) * f;
        if (!a.fixed) { a.vx += fx; a.vy += fy; }
        if (!b.fixed) { b.vx -= fx; b.vy -= fy; }
      }
    }
  }
  // springs
  edges.forEach(e => {
    const a = nodes[e.s], b = nodes[e.t];
    if (!kindVisible[a.kind] || !kindVisible[b.kind]) return;
    const dx = b.x - a.x, dy = b.y - a.y;
    const d = Math.sqrt(dx * dx + dy * dy) || 1;
    const f = (d - 90) * 0.012 * alpha;
    const fx = (dx / d) * f, fy = (dy / d) * f;
    if (!a.fixed) { a.vx += fx; a.vy += fy; }
    if (!b.fixed) { b.vx -= fx; b.vy -= fy; }
  });
  // same-file cohesion + center gravity
  Object.values(fileCenters).forEach(c => { c.x = 0; c.y = 0; c.count = 0; });
  vis.forEach(n => {
    const c = fileCenters[n.file];
    c.x += n.x; c.y += n.y; c.count++;
  });
  vis.forEach(n => {
    const c = fileCenters[n.file];
    if (c.count > 1 && !n.fixed) {
      n.vx += ((c.x / c.count) - n.x) * 0.006 * alpha;
      n.vy += ((c.y / c.count) - n.y) * 0.006 * alpha;
    }
    if (!n.fixed) {
      n.vx += (W / 2 - n.x) * 0.0016 * alpha;
      n.vy += (H / 2 - n.y) * 0.0016 * alpha;
      n.vx *= 0.86; n.vy *= 0.86;
      n.x += n.vx; n.y += n.vy;
    }
  });
}

// --- rendering ---------------------------------------------------------------
let hovered = null, selected = null, query = "";
function matches(n) { return query && n.id.toLowerCase().includes(query); }

function draw() {
  ctx.clearRect(0, 0, W, H);
  ctx.save();
  ctx.translate(view.x, view.y);
  ctx.scale(view.scale, view.scale);

  const neighbor = new Set();
  const focus = selected || hovered;
  if (focus !== null) {
    edges.forEach(e => {
      if (e.s === focus) neighbor.add(e.t);
      if (e.t === focus) neighbor.add(e.s);
    });
  }

  edges.forEach(e => {
    const a = nodes[e.s], b = nodes[e.t];
    if (!kindVisible[a.kind] || !kindVisible[b.kind]) return;
    const hot = focus !== null && (e.s === focus || e.t === focus);
    ctx.strokeStyle = hot ? "#e9c46a" : "rgba(120,140,190,0.28)";
    ctx.lineWidth = (hot ? 1.8 : 0.8) / view.scale;
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    // arrowhead
    const dx = b.x - a.x, dy = b.y - a.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
    const tx = b.x - (dx / d) * (b.r + 4), ty = b.y - (dy / d) * (b.r + 4);
    const ang = Math.atan2(dy, dx), s = 5 / Math.sqrt(view.scale);
    ctx.fillStyle = ctx.strokeStyle;
    ctx.beginPath();
    ctx.moveTo(tx, ty);
    ctx.lineTo(tx - s * Math.cos(ang - 0.4), ty - s * Math.sin(ang - 0.4));
    ctx.lineTo(tx - s * Math.cos(ang + 0.4), ty - s * Math.sin(ang + 0.4));
    ctx.fill();
  });

  nodes.forEach((n, i) => {
    if (!kindVisible[n.kind]) return;
    const dim = (query && !matches(n)) ||
      (focus !== null && i !== focus && !neighbor.has(i));
    ctx.globalAlpha = dim ? 0.16 : 1;
    ctx.fillStyle = KIND_COLORS[n.kind] || KIND_COLORS.unknown;
    ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2); ctx.fill();
    if (i === selected) {
      ctx.strokeStyle = "#fff"; ctx.lineWidth = 2 / view.scale; ctx.stroke();
    }
    if (view.scale > 0.9 && !dim && (n.deg > 2 || view.scale > 1.6 || i === hovered)) {
      ctx.fillStyle = "#c8d3ea";
      ctx.font = `${11 / view.scale}px sans-serif`;
      const label = n.id.split(".").slice(-2).join(".");
      ctx.fillText(label, n.x + n.r + 3 / view.scale, n.y + 3 / view.scale);
    }
    ctx.globalAlpha = 1;
  });
  ctx.restore();
}

function frame() { step(); draw(); requestAnimationFrame(frame); }
requestAnimationFrame(frame);

// --- interactions ------------------------------------------------------------
function toWorld(px, py) {
  return { x: (px - view.x) / view.scale, y: (py - view.y) / view.scale };
}
function pick(px, py) {
  const p = toWorld(px, py);
  for (let i = nodes.length - 1; i >= 0; i--) {
    const n = nodes[i];
    if (!kindVisible[n.kind]) continue;
    const dx = p.x - n.x, dy = p.y - n.y;
    if (dx * dx + dy * dy <= (n.r + 3) * (n.r + 3)) return i;
  }
  return null;
}

let dragNode = null, panning = false, last = { x: 0, y: 0 }, moved = false;
canvas.addEventListener("mousedown", ev => {
  moved = false;
  const hit = pick(ev.clientX, ev.clientY);
  if (hit !== null) { dragNode = hit; nodes[hit].fixed = true; }
  else { panning = true; canvas.style.cursor = "grabbing"; }
  last = { x: ev.clientX, y: ev.clientY };
});
window.addEventListener("mousemove", ev => {
  const tooltip = document.getElementById("tooltip");
  if (dragNode !== null) {
    const p = toWorld(ev.clientX, ev.clientY);
    nodes[dragNode].x = p.x; nodes[dragNode].y = p.y;
    nodes[dragNode].vx = 0; nodes[dragNode].vy = 0;
    moved = true; kick();
  } else if (panning) {
    view.x += ev.clientX - last.x; view.y += ev.clientY - last.y;
    last = { x: ev.clientX, y: ev.clientY }; moved = true;
  } else {
    hovered = pick(ev.clientX, ev.clientY);
    if (hovered !== null) {
      const n = nodes[hovered];
      tooltip.style.display = "block";
      tooltip.style.left = (ev.clientX + 14) + "px";
      tooltip.style.top = (ev.clientY + 14) + "px";
      tooltip.textContent = `${n.id} — ${n.kind}` +
        (n.effects.length ? ` [${n.effects.join(", ")}]` : "");
      canvas.style.cursor = "pointer";
    } else {
      tooltip.style.display = "none";
      canvas.style.cursor = "grab";
    }
  }
});
window.addEventListener("mouseup", ev => {
  if (dragNode !== null && !moved) select(dragNode);
  else if (panning && !moved) select(null);
  if (dragNode !== null) nodes[dragNode].fixed = false;
  dragNode = null; panning = false;
  canvas.style.cursor = "grab";
});
canvas.addEventListener("wheel", ev => {
  ev.preventDefault();
  const factor = Math.exp(-ev.deltaY * 0.0012);
  const p = toWorld(ev.clientX, ev.clientY);
  view.scale = Math.min(6, Math.max(0.15, view.scale * factor));
  view.x = ev.clientX - p.x * view.scale;
  view.y = ev.clientY - p.y * view.scale;
}, { passive: false });

// --- detail panel -------------------------------------------------------------
function select(i) {
  selected = i;
  const panel = document.getElementById("detail");
  if (i === null) { panel.style.display = "none"; return; }
  const n = nodes[i];
  const callers = edges.filter(e => e.t === i).map(e => nodes[e.s].id);
  const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
  const list = xs => xs.length
    ? "<dd>" + xs.map(esc).join("</dd><dd>") + "</dd>" : "<dd>—</dd>";
  document.getElementById("detail-body").innerHTML = `
    <h2>${esc(n.id)}</h2>
    <dl>
      <dt>kind</dt><dd>${esc(n.kind)}</dd>
      <dt>purity</dt><dd>${n.purity}</dd>
      <dt>signature</dt><dd>${esc(n.signature || "—")}</dd>
      <dt>effects</dt>${list(n.effects)}
      <dt>inputs</dt>${list(n.inputs)}
      <dt>calls</dt>${list(n.calls)}
      <dt>called by</dt>${list(callers)}
      <dt>trace</dt>${list(n.trace)}
    </dl>`;
  panel.style.display = "block";
}
document.getElementById("close").addEventListener("click", () => select(null));

// --- search + legend -----------------------------------------------------------
document.getElementById("search").addEventListener("input", ev => {
  query = ev.target.value.trim().toLowerCase();
});
const legend = document.getElementById("legend");
const counts = {};
nodes.forEach(n => counts[n.kind] = (counts[n.kind] || 0) + 1);
Object.entries(KIND_COLORS).forEach(([kind, color]) => {
  if (!counts[kind]) return;
  const el = document.createElement("div");
  el.className = "lg";
  el.innerHTML = `<span class="dot" style="background:${color}"></span>` +
    `${kind} (${counts[kind]})`;
  el.addEventListener("click", () => {
    kindVisible[kind] = !kindVisible[kind];
    el.classList.toggle("off", !kindVisible[kind]);
    kick();
  });
  legend.appendChild(el);
});
</script>
</body>
</html>
"""
