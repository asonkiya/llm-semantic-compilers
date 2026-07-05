"""Self-contained HTML visualization of the component graph.

Writes a single ``viz.html`` with the component data embedded as JSON and
a hand-rolled canvas force layout — no CDN, no network requests, matching
the local-first rule for the analysis layers. Open it with any browser.

Three views over the same data:

* **Components** — force layout, one node per component, file hulls.
* **Files** — one node per file, edges aggregated with call counts:
  the architecture overview.
* **Layers** — columns by call depth (routes → services → repos → types),
  left to right.

Interactions: drag nodes, wheel-zoom, drag-background to pan, hover for a
tooltip, click for a transitive trace + detail panel, search box to
highlight, legend to toggle component kinds, sliders for spacing / link
length / node size.
"""

from __future__ import annotations

import json
from pathlib import Path

from cgir.ir.component_spec import ComponentSpec


def write(
    out_dir: Path,
    specs: list[ComponentSpec],
    arg_flows: dict[str, list[dict[str, object]]] | None = None,
) -> Path:
    """Write ``<out_dir>/viz.html`` and return its path.

    ``arg_flows`` (from :mod:`cgir.analyses.param_flow`, keyed by spec id)
    adds caller→callee argument edges typed by the parameter annotation.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    data = _build_data(specs, arg_flows)
    html = _TEMPLATE.replace("__CGIR_JSON__", json.dumps(data, sort_keys=True))
    path = out_dir / "viz.html"
    path.write_text(html)
    return path


def _build_data(
    specs: list[ComponentSpec],
    arg_flows: dict[str, list[dict[str, object]]] | None = None,
) -> dict[str, object]:
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
                "constructs": spec.constructs,
                "outputs": spec.outputs,
                "param_types": _param_types(spec.signature),
                "entrypoint": spec.entrypoint,
                "file": file,
                "trace": spec.trace,
                "signature": spec.signature,
            }
        )
    # Synthetic nodes for constructed types — the data model becomes visible.
    type_names = sorted({t for spec in specs for t in spec.constructs})
    for type_name in type_names:
        index_of[type_name] = len(nodes)
        nodes.append(
            {
                "id": type_name,
                "kind": "type",
                "purity": None,
                "effects": [],
                "inputs": [],
                "calls": [],
                "constructs": [],
                "outputs": [],
                "param_types": [],
                "entrypoint": None,
                "file": "(types)",
                "trace": [],
                "signature": None,
            }
        )
    edges = []
    for spec in specs:
        for callee in spec.calls:
            target = index_of.get(callee)
            if target is not None:
                callee_outputs = specs[target].outputs if target < len(specs) else []
                edges.append(
                    {
                        "s": index_of[spec.id],
                        "t": target,
                        "kind": "call",
                        "type": callee_outputs[0] if callee_outputs else None,
                    }
                )
        for type_name in spec.constructs:
            edges.append(
                {
                    "s": index_of[spec.id],
                    "t": index_of[type_name],
                    "kind": "construct",
                    "type": type_name.rsplit(".", 1)[-1],
                }
            )
    if arg_flows:
        spec_by_id = {spec.id: spec for spec in specs}
        for caller_id, entries in arg_flows.items():
            src = index_of.get(caller_id)
            caller = spec_by_id.get(caller_id)
            if src is None or caller is None:
                continue
            annotations = dict(_param_items(caller.signature))
            for entry in entries:
                dst = index_of.get(str(entry.get("callee")))
                if dst is None:
                    continue
                raw_params = entry.get("params")
                params = [str(p) for p in raw_params] if isinstance(raw_params, list) else []
                labels = [annotations.get(p) or p for p in params]
                edges.append({"s": src, "t": dst, "kind": "arg", "type": ", ".join(labels)})
    return {"nodes": nodes, "edges": edges}


def _param_items(signature: str | None) -> list[tuple[str, str | None]]:
    """Best-effort ``(name, annotation)`` pairs from a rendered signature.

    ``f(price: float, table: dict[str, int], x=1) -> float`` gives
    ``[("price", "float"), ("table", "dict[str, int]"), ("x", None)]`` —
    bracket-aware comma split, defaults stripped.
    """
    if not signature or "(" not in signature or ")" not in signature:
        return []
    start = signature.index("(") + 1
    end = len(signature)
    depth = 0
    for i in range(start, len(signature)):
        ch = signature[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0 and ch == ")":
                end = i
                break
            depth -= 1
    inner = signature[start:end]
    if not inner.strip():
        return []
    parts: list[str] = []
    depth = 0
    current = ""
    for ch in inner:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    items: list[tuple[str, str | None]] = []
    for part in parts:
        name, _, annotation = part.partition(":")
        name = name.split("=", 1)[0].strip().lstrip("*")
        annotation = annotation.split("=", 1)[0].strip()
        items.append((name, annotation or None))
    return items


def _param_types(signature: str | None) -> list[str]:
    """Annotation list from :func:`_param_items`; unannotated marked ``?``."""
    return [annotation or "?" for _, annotation in _param_items(signature)]


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
    gap: 14px; align-items: center; flex-wrap: wrap; padding: 10px 14px;
    background: rgba(15,20,32,.92); border-bottom: 1px solid #232c44; }
  #topbar h1 { font-size: 14px; margin: 0; font-weight: 600; color: #8ea6d8; }
  #views { display: flex; border: 1px solid #2c3757; border-radius: 8px;
    overflow: hidden; }
  .vw { padding: 6px 14px; background: #171e30; color: #8ea6d8; border: none;
    cursor: pointer; font-size: 13px; }
  .vw + .vw { border-left: 1px solid #2c3757; }
  .vw.active { background: #2c3757; color: #fff; }
  .vw:hover:not(.active) { color: #dbe2ef; }
  #search { flex: 0 0 220px; padding: 6px 10px; border-radius: 6px;
    border: 1px solid #2c3757; background: #171e30; color: #dbe2ef; }
  #fit { padding: 6px 12px; border-radius: 6px; border: 1px solid #2c3757;
    background: #171e30; color: #8ea6d8; cursor: pointer; }
  #fit:hover { color: #dbe2ef; border-color: #46578a; }
  #sliders { display: flex; gap: 18px; align-items: center; }
  #sliders label { display: flex; flex-direction: column; gap: 2px;
    font-size: 10px; text-transform: uppercase; letter-spacing: .06em;
    color: #56618a; user-select: none; }
  #sliders input[type="range"] { width: 180px; height: 22px;
    accent-color: #63b3ed; cursor: pointer; }
  #legend { display: flex; gap: 10px; flex-wrap: wrap; }
  .lg { display: flex; gap: 5px; align-items: center; cursor: pointer;
    opacity: 1; user-select: none; }
  .lg.off { opacity: .3; }
  .dot { width: 10px; height: 10px; border-radius: 50%; }
  #detail { position: fixed; top: 96px; right: 0; bottom: 0; width: 320px;
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
  <h1>CGIR</h1>
  <div id="views">
    <button class="vw active" data-view="components">Components</button>
    <button class="vw" data-view="files">Files</button>
    <button class="vw" data-view="layers">Layers</button>
    <button class="vw" data-view="flow">Flow</button>
  </div>
  <input id="search" placeholder="search…" autocomplete="off">
  <button id="fit" title="Zoom to fit (F)">⤢ fit</button>
  <div id="sliders">
    <label>spacing <input type="range" id="s-spacing" min="0.3" max="3" step="0.05" value="1"></label>
    <label>link length <input type="range" id="s-links" min="0.3" max="3" step="0.05" value="1"></label>
    <label>node size <input type="range" id="s-size" min="0.5" max="2.5" step="0.05" value="1"></label>
  </div>
  <div id="legend"></div>
</div>
<canvas id="canvas"></canvas>
<div id="tooltip"></div>
<div id="detail"><span class="close" id="close">✕</span><div id="detail-body"></div></div>
<div id="hint">drag nodes · wheel to zoom · drag background to pan · click a node to trace it</div>
<script>
const DATA = /*CGIR_DATA*/__CGIR_JSON__/*END_CGIR_DATA*/;

const KIND_COLORS = {
  pure_function: "#68d391",
  orchestrator: "#63b3ed",
  state_transformer: "#f6ad55",
  effect_adapter: "#fc8181",
  unknown: "#a0aec0",
  type: "#b794f4",
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

// --- tunables (sliders) ------------------------------------------------------
let SP = 1;   // spacing: cluster padding + repulsion
let LK = 1;   // link length: spring rest distances
let SZ = 1;   // node size
const rad = n => n.r * SZ;

// --- base graph model --------------------------------------------------------
const fileNames = [...new Set(DATA.nodes.map(n => n.file))];
const fileSlot = {};
fileNames.forEach((f, i) => { fileSlot[f] = i; });
const ringR = Math.max(380, fileNames.length * 52);
function seedPos(slotCount, slot, i) {
  const slotAngle = (slot / Math.max(slotCount, 1)) * Math.PI * 2;
  const jitterAngle = (i * 2.399963) % (Math.PI * 2); // golden-angle spread
  const jitterR = 20 + (i % 5) * 14;
  return {
    x: W / 2 + Math.cos(slotAngle) * ringR + Math.cos(jitterAngle) * jitterR,
    y: H / 2 + Math.sin(slotAngle) * ringR + Math.sin(jitterAngle) * jitterR,
  };
}
const baseNodes = DATA.nodes.map((n, i) => {
  const p = seedPos(fileNames.length, fileSlot[n.file], i);
  return { ...n, x: p.x, y: p.y, vx: 0, vy: 0, deg: 0, fixed: false, layer: 0 };
});
const allEdges = DATA.edges.map(e => ({ s: e.s, t: e.t, kind: e.kind || "call", type: e.type, w: 1 }));
// PDG-derived arg edges only appear in the Flow view; the structural views
// use call/construct edges so pairs aren't double-drawn.
const argEdges = allEdges.filter(e => e.kind === "arg");
const baseEdges = allEdges.filter(e => e.kind !== "arg");
baseEdges.forEach(e => { baseNodes[e.s].deg++; baseNodes[e.t].deg++; });
baseNodes.forEach(n => { n.r = 5 + Math.min(11, Math.sqrt(n.deg) * 2.4); });

// layer = longest call-path depth from any root (bounded relaxation, cycle-safe)
{
  for (let pass = 0; pass < 24; pass++) {
    let changed = false;
    baseEdges.forEach(e => {
      if (baseNodes[e.t].layer < baseNodes[e.s].layer + 1) {
        baseNodes[e.t].layer = baseNodes[e.s].layer + 1;
        changed = true;
      }
    });
    if (!changed) break;
  }
}

// --- flow view: edges follow DATA, not calls ----------------------------------
// A callee returning a value sends data back up: edge callee -> caller,
// colored by the returned type. Void calls are "commands" (effects, thin
// gray). Constructs reverse too: the type feeds the function that builds it.
const flowEdges = [];
baseEdges.forEach(e => {
  if (e.kind === "construct") {
    flowEdges.push({ s: e.t, t: e.s, kind: "data", type: e.type, w: 1 });
    return;
  }
  const callee = baseNodes[e.t];
  const rt = (callee.outputs && callee.outputs[0]) || null;
  if (rt && rt !== "None") {
    flowEdges.push({ s: e.t, t: e.s, kind: "data", type: rt, w: 1 });
  } else {
    flowEdges.push({ s: e.s, t: e.t, kind: "command", type: null, w: 1 });
  }
});
{
  // Stage layout ignores arg edges (they run opposite the return edges and
  // would cycle every caller/callee pair).
  baseNodes.forEach(n => { n.flowLayer = 0; });
  for (let pass = 0; pass < 24; pass++) {
    let changed = false;
    flowEdges.forEach(e => {
      if (baseNodes[e.t].flowLayer < baseNodes[e.s].flowLayer + 1) {
        baseNodes[e.t].flowLayer = baseNodes[e.s].flowLayer + 1;
        changed = true;
      }
    });
    if (!changed) break;
  }
}
argEdges.forEach(e => flowEdges.push(e));

function typeColor(t) {
  let h = 0;
  for (let i = 0; i < t.length; i++) h = (h * 31 + t.charCodeAt(i)) % 360;
  return `hsl(${h}, 62%, 64%)`;
}

// --- files view: one node per file, aggregated weighted edges -----------------
function buildFilesView() {
  const byFile = {};
  baseNodes.forEach(n => { (byFile[n.file] = byFile[n.file] || []).push(n); });
  const fnodes = [];
  const idx = {};
  Object.entries(byFile).forEach(([file, members]) => {
    const kinds = {};
    members.forEach(m => { kinds[m.kind] = (kinds[m.kind] || 0) + 1; });
    const domKind = Object.entries(kinds).sort((a, b) => b[1] - a[1])[0][0];
    const effects = [...new Set(members.flatMap(m => m.effects))].sort();
    idx[file] = fnodes.length;
    const p = seedPos(Object.keys(byFile).length, fnodes.length, fnodes.length);
    fnodes.push({
      id: file, kind: domKind, file, purity: null, effects,
      inputs: [], calls: [], constructs: [], outputs: [], trace: [],
      signature: null, members: members.map(m => m.id).sort(),
      x: p.x, y: p.y, vx: 0, vy: 0, deg: 0, fixed: false, layer: 0,
    });
  });
  const weight = {};
  baseEdges.forEach(e => {
    const fa = baseNodes[e.s].file, fb = baseNodes[e.t].file;
    if (fa === fb) return;
    const key = idx[fa] + ":" + idx[fb] + ":" + e.kind;
    weight[key] = (weight[key] || 0) + 1;
  });
  const fedges = Object.entries(weight).map(([key, w]) => {
    const [s, t, kind] = key.split(":");
    return { s: +s, t: +t, kind, type: w > 1 ? w + " calls" : null, w };
  });
  fedges.forEach(e => { fnodes[e.s].deg += e.w; fnodes[e.t].deg += e.w; });
  fnodes.forEach(n => {
    n.r = 9 + Math.min(18, 3.2 * Math.sqrt((byFile[n.id] || []).length));
  });
  return { nodes: fnodes, edges: fedges };
}

// --- active view state ---------------------------------------------------------
let currentView = "components";
let nodes = baseNodes, edges = baseEdges;
let outAdj = [], inAdj = [];
function rebuildAdjacency() {
  outAdj = nodes.map(() => []); inAdj = nodes.map(() => []);
  edges.forEach((e, ei) => { outAdj[e.s].push({ n: e.t, ei }); inAdj[e.t].push({ n: e.s, ei }); });
}
rebuildAdjacency();

const LAYER_GAP = 260;
function isPinnedView() { return currentView === "layers" || currentView === "flow"; }
function pinnedLayer(n) { return currentView === "flow" ? n.flowLayer : n.layer; }
function layerX(n) { return 160 + pinnedLayer(n) * LAYER_GAP * LK; }

// Deterministic grid layout for pinned views: tight column slots, tall
// columns wrap sideways, and unconnected nodes park in a labeled block
// below instead of drifting to the fringes.
let isolatedTop = null;
let pinnedPitch = 44;
function layoutPinned() {
  // flow boxes are ~34px tall; give them clear air between rows
  const pitch = currentView === "flow" ? 54 : 44;
  pinnedPitch = pitch;
  const maxRows = 40;
  const isolated = [], connected = [];
  nodes.forEach((n, i) => {
    const degHere = outAdj[i].length + inAdj[i].length;
    (degHere === 0 ? isolated : connected).push(n);
  });
  const byLayer = {};
  connected.forEach(n => {
    (byLayer[pinnedLayer(n)] = byLayer[pinnedLayer(n)] || []).push(n);
  });
  const layerKeys = Object.keys(byLayer).map(Number).sort((x, y) => x - y);
  layerKeys.forEach(l =>
    byLayer[l].sort((a, b) => (a.file + a.id).localeCompare(b.file + b.id)));

  // Barycenter sweeps: order each column by the mean row of its neighbors
  // so edges run roughly horizontally instead of slashing across the grid.
  const indexOfNode = new Map(nodes.map((n, i) => [n, i]));
  const rowOf = new Map();
  layerKeys.forEach(l => byLayer[l].forEach((n, i) => rowOf.set(n, i)));
  const meanNeighborRow = n => {
    const i = indexOfNode.get(n);
    let sum = 0, count = 0;
    outAdj[i].forEach(({ n: t }) => {
      if (rowOf.has(nodes[t])) { sum += rowOf.get(nodes[t]); count++; }
    });
    inAdj[i].forEach(({ n: s }) => {
      if (rowOf.has(nodes[s])) { sum += rowOf.get(nodes[s]); count++; }
    });
    return count ? sum / count : rowOf.get(n);
  };
  for (let sweep = 0; sweep < 4; sweep++) {
    const keys = sweep % 2 === 0 ? layerKeys : [...layerKeys].reverse();
    keys.forEach(l => {
      byLayer[l].sort((a, b) =>
        meanNeighborRow(a) - meanNeighborRow(b) ||
        (a.file + a.id).localeCompare(b.file + b.id));
      byLayer[l].forEach((n, i) => rowOf.set(n, i));
    });
  }

  let maxBottom = 200;
  layerKeys.forEach(l => {
    byLayer[l].forEach((n, i) => {
      const col = Math.floor(i / maxRows);
      const row = i % maxRows;
      n.x = layerX(n) + col * 230;
      n.y = 200 + row * pitch;
      n.vx = 0; n.vy = 0;
    });
    maxBottom = Math.max(maxBottom, 200 + Math.min(byLayer[l].length, maxRows) * pitch);
  });
  if (isolated.length) {
    isolatedTop = maxBottom + 130;
    const cols = Math.max(1, Math.ceil(Math.sqrt(isolated.length * 2)));
    isolated.sort((a, b) => (a.file + a.id).localeCompare(b.file + b.id));
    isolated.forEach((n, i) => {
      n.x = 160 + (i % cols) * 250;
      n.y = isolatedTop + Math.floor(i / cols) * pitch;
      n.vx = 0; n.vy = 0;
    });
  } else {
    isolatedTop = null;
  }
}

function setView(name) {
  currentView = name;
  select(null);
  hovered = null;
  if (name === "files") {
    const v = buildFilesView();
    nodes = v.nodes; edges = v.edges;
  } else if (name === "flow") {
    nodes = baseNodes; edges = flowEdges;
  } else {
    nodes = baseNodes; edges = baseEdges;
  }
  rebuildAdjacency();
  if (isPinnedView()) layoutPinned();
  buildLegend();
  userAdjustedView = false;
  frameCount = 0;
  alpha = 1;
  document.querySelectorAll(".vw").forEach(b =>
    b.classList.toggle("active", b.dataset.view === name));
}
document.querySelectorAll(".vw").forEach(b =>
  b.addEventListener("click", () => setView(b.dataset.view)));

const kindVisible = {};
Object.keys(KIND_COLORS).forEach(k => kindVisible[k] = true);

let view = { x: 0, y: 0, scale: 1 };
let alpha = 1;
function kick() { alpha = Math.max(alpha, 0.35); }

// --- physics ----------------------------------------------------------------
const fileCenters = {};
function step() {
  if (isPinnedView()) return; // pinned views are deterministic grids
  if (alpha < 0.005) return;
  alpha *= 0.985;
  const vis = nodes.filter(n => kindVisible[n.kind]);
  // repulsion — exact for small graphs, sampled for large ones
  const rep = 2600 * SP;
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
        if (d2 > 250000 * SP) continue;
        const f = (rep * boost / d2) * alpha;
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
        if (d2 > 250000 * SP) continue;
        const f = (rep / d2) * alpha;
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
    const base = currentView === "files" ? 260
      : (a.file === b.file ? 70 : 380);
    const rest = base * LK;
    const f = (d - rest) * 0.006 * alpha;
    const fx = (dx / d) * f, fy = (dy / d) * f;
    if (!a.fixed) { a.vx += fx; a.vy += fy; }
    if (!b.fixed) { b.vx -= fx; b.vy -= fy; }
  });
  // same-file cohesion + cluster separation (components view only)
  Object.keys(fileCenters).forEach(k => delete fileCenters[k]);
  vis.forEach(n => {
    const c = fileCenters[n.file] = fileCenters[n.file] || { x: 0, y: 0, count: 0 };
    c.x += n.x; c.y += n.y; c.count++;
  });
  if (currentView === "components") {
    const centerList = Object.entries(fileCenters).filter(([, c]) => c.count > 0);
    for (let i = 0; i < centerList.length; i++) {
      for (let j = i + 1; j < centerList.length; j++) {
        const [fa, a] = centerList[i], [fb, b] = centerList[j];
        const ax = a.x / a.count, ay = a.y / a.count;
        const bx = b.x / b.count, by = b.y / b.count;
        let dx = ax - bx, dy = ay - by;
        let d = Math.hypot(dx, dy);
        if (d < 1) { dx = 1; dy = 0; d = 1; }
        const ra = 34 + 13 * Math.sqrt(a.count), rb = 34 + 13 * Math.sqrt(b.count);
        const minD = ra + rb + 150 * SP;
        if (d < minD) {
          const f = ((minD - d) / d) * 0.07 * alpha;
          const fx = dx * f, fy = dy * f;
          vis.forEach(n => {
            if (n.fixed) return;
            if (n.file === fa) { n.vx += fx; n.vy += fy; }
            else if (n.file === fb) { n.vx -= fx; n.vy -= fy; }
          });
        }
      }
    }
  }
  vis.forEach(n => {
    const c = fileCenters[n.file];
    if (currentView === "components" && c.count > 1 && !n.fixed) {
      n.vx += ((c.x / c.count) - n.x) * 0.025 * alpha;
      n.vy += ((c.y / c.count) - n.y) * 0.025 * alpha;
    }
    if (!n.fixed) {
      n.vx += (W / 2 - n.x) * (0.0002 / SP) * alpha;
      n.vy += (H / 2 - n.y) * (0.0002 / SP) * alpha;
      n.vx *= 0.86; n.vy *= 0.86;
      n.x += n.vx; n.y += n.vy;
    }
  });
}

// --- fit view ----------------------------------------------------------------
let userAdjustedView = false;
let frameCount = 0;
function fitView() {
  const vis = nodes.filter(n => kindVisible[n.kind]);
  if (!vis.length) return;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  vis.forEach(n => {
    if (n.x < minX) minX = n.x;
    if (n.x > maxX) maxX = n.x;
    if (n.y < minY) minY = n.y;
    if (n.y > maxY) maxY = n.y;
  });
  const pad = 90;
  const sx = W / (maxX - minX + 2 * pad), sy = (H - 110) / (maxY - minY + 2 * pad);
  view.scale = Math.min(1.4, Math.max(0.08, Math.min(sx, sy)));
  view.x = W / 2 - ((minX + maxX) / 2) * view.scale;
  view.y = (H + 96) / 2 - ((minY + maxY) / 2) * view.scale;
}

// --- transitive tracing --------------------------------------------------------
function traceFrom(start) {
  const tracedNodes = new Set([start]), tracedEdges = new Set();
  [outAdj, inAdj].forEach(adj => {
    const frontier = [start], visited = new Set([start]);
    while (frontier.length) {
      const cur = frontier.pop();
      adj[cur].forEach(({ n, ei }) => {
        tracedEdges.add(ei); tracedNodes.add(n);
        if (!visited.has(n)) { visited.add(n); frontier.push(n); }
      });
    }
  });
  return { nodes: tracedNodes, edges: tracedEdges };
}
let traced = null;

// --- rendering ---------------------------------------------------------------
let hovered = null, selected = null, query = "";
function matches(n) { return query && n.id.toLowerCase().includes(query); }

const short = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
// text with a dark halo so labels stay legible over edges
function haloText(text, x, y, color, sizePx) {
  ctx.font = `${sizePx}px sans-serif`;
  ctx.strokeStyle = "rgba(15,20,32,0.85)";
  ctx.lineWidth = sizePx / 3.5;
  ctx.lineJoin = "round";
  ctx.strokeText(text, x, y);
  ctx.fillStyle = color;
  ctx.fillText(text, x, y);
}

function draw() {
  ctx.clearRect(0, 0, W, H);
  ctx.save();
  ctx.translate(view.x, view.y);
  ctx.scale(view.scale, view.scale);

  // file cluster hulls (components view only)
  if (currentView === "components") {
    const groups = {};
    nodes.forEach(n => {
      if (!kindVisible[n.kind]) return;
      (groups[n.file] = groups[n.file] || []).push(n);
    });
    Object.entries(groups).forEach(([file, members]) => {
      if (members.length < 2) return;
      let cx = 0, cy = 0;
      members.forEach(m => { cx += m.x; cy += m.y; });
      cx /= members.length; cy /= members.length;
      let r = 0;
      members.forEach(m => {
        const d = Math.hypot(m.x - cx, m.y - cy) + rad(m);
        if (d > r) r = d;
      });
      r += 20;
      ctx.fillStyle = "rgba(90,110,170,0.08)";
      ctx.strokeStyle = "rgba(90,110,170,0.28)";
      ctx.lineWidth = 1 / view.scale;
      ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      if (view.scale > 0.45) {
        ctx.textAlign = "center";
        haloText(file, cx, cy - r - 6 / view.scale, "#8ea6d8", 12 / view.scale);
        ctx.textAlign = "left";
      }
    });
  }

  // layer guides (pinned views)
  if (isPinnedView()) {
    const maxLayer = Math.max(...nodes.map(n => pinnedLayer(n)), 0);
    const guideLabel = currentView === "flow" ? "stage " : "depth ";
    ctx.textAlign = "center";
    for (let l = 0; l <= maxLayer; l++) {
      const x = 160 + l * LAYER_GAP * LK;
      ctx.strokeStyle = "rgba(90,110,170,0.12)";
      ctx.lineWidth = 1 / view.scale;
      ctx.beginPath(); ctx.moveTo(x, -100000); ctx.lineTo(x, 100000); ctx.stroke();
      const wy = (110 - view.y) / view.scale;
      haloText(guideLabel + l, x, wy, "#7484b8", 12 / view.scale);
    }
    if (isolatedTop !== null) {
      ctx.textAlign = "left";
      haloText("unconnected", 160, isolatedTop - 24, "#7484b8", 12 / view.scale);
    }
    ctx.textAlign = "left";
  }

  // Selected node: highlight the full transitive trace. Hover: 1-hop.
  const focus = selected !== null ? selected : hovered;
  const neighbor = new Set();
  if (focus !== null && traced === null) {
    edges.forEach(e => {
      if (e.s === focus) neighbor.add(e.t);
      if (e.t === focus) neighbor.add(e.s);
    });
  }
  const nodeHot = i => traced ? traced.nodes.has(i)
    : (focus === null || i === focus || neighbor.has(i));
  const edgeHot = (e, ei) => traced ? traced.edges.has(ei)
    : (focus !== null && (e.s === focus || e.t === focus));

  // Dense pinned views: idle edges recede; hover/click brings a path forward.
  const pinned = isPinnedView();
  const dense = pinned && edges.length > 250;
  edges.forEach((e, ei) => {
    const a = nodes[e.s], b = nodes[e.t];
    if (!kindVisible[a.kind] || !kindVisible[b.kind]) return;
    const hot = edgeHot(e, ei);
    const hotFocus = hot && focus !== null;
    const dimmed = focus !== null && !hot;
    const baseW = 0.8 + (e.w ? Math.min(4, Math.sqrt(e.w) - 1) : 0);
    let idleColor = "rgba(120,140,190,0.28)";
    if (currentView === "flow") {
      // data/arg edges carry their type's color; commands stay faint gray
      idleColor = (e.kind === "data" || e.kind === "arg") && e.type
        ? typeColor(e.type) : "rgba(120,140,190,0.18)";
    }
    ctx.strokeStyle = hotFocus ? "#e9c46a"
      : dimmed ? "rgba(120,140,190,0.08)" : idleColor;
    let edgeAlpha = 1;
    if (!hotFocus && !dimmed) {
      if (currentView === "flow") edgeAlpha = 0.6;
      if (dense) edgeAlpha = 0.18;
    }
    ctx.globalAlpha = edgeAlpha;
    ctx.lineWidth = (hotFocus ? baseW + 1 : baseW) / view.scale;
    if (e.kind === "construct" || e.kind === "command") {
      ctx.setLineDash([5 / view.scale, 4 / view.scale]);
    } else if (e.kind === "arg") {
      ctx.setLineDash([2 / view.scale, 3 / view.scale]);
    }
    const dx = b.x - a.x, dy = b.y - a.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
    const bOffset = (currentView === "flow" && b._bw) ? Math.min(b._bw, b._bh) + 3 : rad(b) + 4;
    let tx, ty, ang, midX, midY;
    if (pinned) {
      // horizontal-tangent S-curve (dagre-style): edges leave and enter
      // columns sideways instead of slashing across the grid
      const dir = Math.sign(dx) || 1;
      const bend = Math.max(50, Math.abs(dx) * 0.4);
      const c1x = a.x + bend * dir, c2x = b.x - bend * dir;
      tx = b.x - dir * bOffset; ty = b.y;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.bezierCurveTo(c1x, a.y, c2x, b.y, tx, ty);
      ctx.stroke();
      ang = dir > 0 ? 0 : Math.PI;
      midX = (a.x + 3 * c1x + 3 * c2x + b.x) / 8;
      midY = (a.y + b.y) / 2;
    } else {
      tx = b.x - (dx / d) * bOffset; ty = b.y - (dy / d) * bOffset;
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      ang = Math.atan2(dy, dx);
      midX = (a.x + b.x) / 2; midY = (a.y + b.y) / 2;
    }
    ctx.setLineDash([]);
    // arrowhead
    const s = 5 / Math.sqrt(view.scale);
    ctx.fillStyle = ctx.strokeStyle;
    ctx.beginPath();
    ctx.moveTo(tx, ty);
    ctx.lineTo(tx - s * Math.cos(ang - 0.4), ty - s * Math.sin(ang - 0.4));
    ctx.lineTo(tx - s * Math.cos(ang + 0.4), ty - s * Math.sin(ang + 0.4));
    ctx.fill();
    // data-type / weight label: in dense views only when hot or zoomed in
    const wantLabel = dense ? (hot || view.scale > 1.6)
      : (view.scale > 1.3 || hot || currentView === "flow");
    if (e.type && !dimmed && wantLabel) {
      const color = currentView === "flow" && (e.kind === "data" || e.kind === "arg")
        ? typeColor(e.type)
        : e.kind === "construct" ? "#b794f4" : "#8ea6d8";
      haloText(short(e.type, 24), midX + 4 / view.scale, midY - 4 / view.scale,
        color, 10 / view.scale);
    }
    ctx.globalAlpha = 1;
  });

  nodes.forEach((n, i) => {
    if (!kindVisible[n.kind]) return;
    const dim = (query && !matches(n)) || (focus !== null && !nodeHot(i));
    ctx.globalAlpha = dim ? 0.16 : 1;
    const kindColor = KIND_COLORS[n.kind] || KIND_COLORS.unknown;
    const r = rad(n);
    if (currentView === "flow" && n.kind !== "type") {
      // transformation box: name + return type, bordered by kind
      const name = short(n.id.split(".").slice(-1)[0], 24);
      const rt = n.outputs && n.outputs[0] ? short(n.outputs[0], 22) : null;
      ctx.font = "12px sans-serif";
      const tw = ctx.measureText(name).width;
      const rw = rt ? ctx.measureText("-> " + rt).width : 0;
      const bw = Math.max(tw, rw) / 2 + 10;
      const bh = rt ? 18 : 12;
      n._bw = bw; n._bh = bh;
      ctx.fillStyle = "#1a2238";
      ctx.strokeStyle = kindColor;
      ctx.lineWidth = i === selected ? 2.5 : 1.3;
      ctx.beginPath();
      ctx.roundRect(n.x - bw, n.y - bh, bw * 2, bh * 2, 6);
      ctx.fill(); ctx.stroke();
      ctx.textAlign = "center";
      ctx.font = "12px sans-serif";
      ctx.fillStyle = dim ? "#56618a" : "#dbe2ef";
      ctx.fillText(name, n.x, n.y + (rt ? -3 : 4));
      if (rt) {
        ctx.font = "10px sans-serif";
        ctx.fillStyle = typeColor(n.outputs[0]);
        ctx.fillText("-> " + rt, n.x, n.y + 11);
      }
      ctx.textAlign = "left";
      ctx.globalAlpha = 1;
      return;
    }
    n._bw = null; n._bh = null;
    ctx.fillStyle = kindColor;
    ctx.beginPath();
    if (n.kind === "type") {
      ctx.rect(n.x - r, n.y - r, r * 2, r * 2);
    } else {
      ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
    }
    ctx.fill();
    if (n.entrypoint) {
      // outer ring: reachable from the outside world (HTTP/CLI/task)
      ctx.strokeStyle = "#e9c46a";
      ctx.lineWidth = 1.4 / view.scale;
      ctx.beginPath();
      ctx.arc(n.x, n.y, r + 3.5 / view.scale, 0, Math.PI * 2);
      ctx.stroke();
    }
    if (i === selected) {
      ctx.strokeStyle = "#fff"; ctx.lineWidth = 2 / view.scale;
      ctx.beginPath();
      if (n.kind === "type") ctx.rect(n.x - r, n.y - r, r * 2, r * 2);
      else ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
      ctx.stroke();
    }
    // Label thinning: only draw when rows have on-screen room (pinned
    // views), or per the density rules elsewhere. Hot nodes always label.
    const isHot = i === hovered || i === selected || (traced && traced.nodes.has(i));
    let wantLabel;
    if (isPinnedView()) {
      wantLabel = isHot || pinnedPitch * view.scale >= 14;
    } else if (currentView === "files") {
      wantLabel = view.scale > 0.3;
    } else {
      wantLabel = view.scale > 0.9 && (n.deg > 2 || view.scale > 1.6 || isHot);
    }
    if (wantLabel && !dim) {
      const label = currentView === "files" ? short(n.id, 34)
        : short(n.id.split(".").slice(-2).join("."), 28);
      haloText(label, n.x + r + 4 / view.scale, n.y + 3.5 / view.scale,
        "#c8d3ea", 11 / view.scale);
    }
    ctx.globalAlpha = 1;
  });
  ctx.restore();
}

function frame() {
  step();
  frameCount++;
  if (!userAdjustedView && (frameCount === 5 || frameCount === 200)) fitView();
  draw();
  requestAnimationFrame(frame);
}
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
    if (currentView === "flow" && n._bw) {
      if (Math.abs(p.x - n.x) <= n._bw + 3 && Math.abs(p.y - n.y) <= n._bh + 3) return i;
      continue;
    }
    const dx = p.x - n.x, dy = p.y - n.y;
    const r = rad(n) + 3;
    if (dx * dx + dy * dy <= r * r) return i;
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
    userAdjustedView = true;
  } else {
    hovered = pick(ev.clientX, ev.clientY);
    if (hovered !== null) {
      const n = nodes[hovered];
      tooltip.style.display = "block";
      tooltip.style.left = (ev.clientX + 14) + "px";
      tooltip.style.top = (ev.clientY + 14) + "px";
      tooltip.textContent = n.members
        ? `${n.id} — ${n.members.length} components`
        : (n.entrypoint ? `${n.entrypoint} · ` : "") +
          `${n.id} — ${n.kind}` + (n.effects.length ? ` [${n.effects.join(", ")}]` : "");
      canvas.style.cursor = "pointer";
    } else {
      tooltip.style.display = "none";
      canvas.style.cursor = "grab";
    }
  }
});
window.addEventListener("mouseup", () => {
  if (dragNode !== null && !moved) select(dragNode);
  else if (panning && !moved) select(null);
  if (dragNode !== null) nodes[dragNode].fixed = false;
  dragNode = null; panning = false;
  canvas.style.cursor = "grab";
});
canvas.addEventListener("wheel", ev => {
  ev.preventDefault();
  userAdjustedView = true;
  const factor = Math.exp(-ev.deltaY * 0.0012);
  const p = toWorld(ev.clientX, ev.clientY);
  view.scale = Math.min(6, Math.max(0.08, view.scale * factor));
  view.x = ev.clientX - p.x * view.scale;
  view.y = ev.clientY - p.y * view.scale;
}, { passive: false });

// --- detail panel -------------------------------------------------------------
function select(i) {
  selected = i;
  traced = i === null ? null : traceFrom(i);
  const panel = document.getElementById("detail");
  if (i === null) { panel.style.display = "none"; return; }
  const n = nodes[i];
  const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
  const list = xs => xs.length
    ? "<dd>" + xs.map(esc).join("</dd><dd>") + "</dd>" : "<dd>—</dd>";
  if (n.members) {
    const outFiles = edges.filter(e => e.s === i).map(e => `${nodes[e.t].id} (${e.w})`);
    const inFiles = edges.filter(e => e.t === i).map(e => `${nodes[e.s].id} (${e.w})`);
    document.getElementById("detail-body").innerHTML = `
      <h2>${esc(n.id)}</h2>
      <dl>
        <dt>calls into</dt>${list(outFiles)}
        <dt>called from</dt>${list(inFiles)}
        <dt>components (${n.members.length})</dt>${list(n.members)}
      </dl>`;
  } else {
    const callers = edges.filter(e => e.t === i && e.kind === "call").map(e => nodes[e.s].id);
    document.getElementById("detail-body").innerHTML = `
      <h2>${esc(n.id)}</h2>
      <dl>
        <dt>kind</dt><dd>${esc(n.kind)}</dd>
        <dt>entrypoint</dt><dd>${esc(n.entrypoint || "—")}</dd>
        <dt>purity</dt><dd>${n.purity == null ? "—" : n.purity}</dd>
        <dt>signature</dt><dd>${esc(n.signature || "—")}</dd>
        <dt>returns</dt>${list(n.outputs || [])}
        <dt>effects</dt>${list(n.effects)}
        <dt>inputs</dt>${list(n.inputs)}
        <dt>calls</dt>${list(n.calls)}
        <dt>constructs</dt>${list(n.constructs || [])}
        <dt>called by</dt>${list(callers)}
        <dt>trace</dt>${list(n.trace)}
      </dl>`;
  }
  panel.style.display = "block";
}
document.getElementById("close").addEventListener("click", () => select(null));

// --- search, fit, sliders, legend ----------------------------------------------
document.getElementById("search").addEventListener("input", ev => {
  query = ev.target.value.trim().toLowerCase();
});
document.getElementById("fit").addEventListener("click", () => {
  userAdjustedView = false;
  fitView();
});
window.addEventListener("keydown", ev => {
  if (ev.key === "f" && document.activeElement !== document.getElementById("search")) {
    fitView();
  }
});
document.getElementById("s-spacing").addEventListener("input", ev => {
  SP = +ev.target.value; kick();
  if (!userAdjustedView) fitView();
});
document.getElementById("s-links").addEventListener("input", ev => {
  LK = +ev.target.value; kick();
  if (isPinnedView()) layoutPinned();
  if (!userAdjustedView) fitView();
});
document.getElementById("s-size").addEventListener("input", ev => {
  SZ = +ev.target.value;
});

const legend = document.getElementById("legend");
function buildLegend() {
  legend.innerHTML = "";
  const counts = {};
  nodes.forEach(n => { counts[n.kind] = (counts[n.kind] || 0) + 1; });
  Object.entries(KIND_COLORS).forEach(([kind, color]) => {
    if (!counts[kind]) return;
    const el = document.createElement("div");
    el.className = "lg" + (kindVisible[kind] ? "" : " off");
    el.innerHTML = `<span class="dot" style="background:${color}"></span>` +
      `${kind} (${counts[kind]})`;
    el.addEventListener("click", () => {
      kindVisible[kind] = !kindVisible[kind];
      el.classList.toggle("off", !kindVisible[kind]);
      kick();
    });
    legend.appendChild(el);
  });
}
buildLegend();
</script>
</body>
</html>
"""
