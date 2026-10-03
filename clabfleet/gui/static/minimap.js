"use strict";

// ---------------------------------------------------------------------------
// Minimap (C1): for labs with many nodes, the whole diagram small in a
// corner, with the part in view outlined. Click or drag in it to move there.
//
// Uses app.js globals: S, $, s, diagramModel, applyView.
// ---------------------------------------------------------------------------

(() => {

const MIN_NODES = 16;      // shown from this many nodes
const W = 180, H = 120;    // size of the map in pixels
const PAD = 60;            // world units around the nodes

let bounds = null;         // world box drawn in the map: {x, y, w, h}

function shown() {
  const nodes = (diagramModel?.nodes || []).filter((n) => !n.pseudo);
  return !$("#view-diagram").hidden && nodes.length >= MIN_NODES && !window.Racks?.active();
}

// The part of the world the diagram shows now
function viewBox() {
  const svg = $("#diagram");
  const k = S.view.k || 1;
  return { x: -S.view.x / k, y: -S.view.y / k, w: (svg.clientWidth || 800) / k, h: (svg.clientHeight || 500) / k };
}

function update() {
  const box = $("#minimap");
  if (!box) return;
  if (!shown()) { box.hidden = true; bounds = null; return; }
  const P = S.positions;
  const pts = Object.values(P);
  if (!pts.length) { box.hidden = true; return; }
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  let x0 = Math.min(...xs) - PAD, x1 = Math.max(...xs) + PAD;
  let y0 = Math.min(...ys) - PAD, y1 = Math.max(...ys) + PAD;
  // Keep the map's shape: grow the shorter side around the middle
  const scale = Math.max((x1 - x0) / W, (y1 - y0) / H);
  const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
  x0 = cx - (W * scale) / 2; x1 = cx + (W * scale) / 2;
  y0 = cy - (H * scale) / 2; y1 = cy + (H * scale) / 2;
  bounds = { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };

  const v = viewBox();
  const map = $("#minimap svg");
  map.setAttribute("viewBox", `${x0} ${y0} ${x1 - x0} ${y1 - y0}`);
  map.replaceChildren(
    ...(diagramModel?.links || []).filter((l) => P[l.a.id] && P[l.b.id]).map((l) => s("line", {
      class: "mm-link", x1: P[l.a.id][0], y1: P[l.a.id][1], x2: P[l.b.id][0], y2: P[l.b.id][1],
    })),
    // Nodes at least a few map pixels big, however large the lab
    ...(diagramModel?.nodes || []).filter((n) => P[n.id]).map((n) => {
      const w = Math.max(80, scale * 3), hh = Math.max(32, scale * 3);
      return s("rect", {
        class: `mm-node${S.selectedNode === n.id ? " selected" : ""}`,
        x: P[n.id][0] - w / 2, y: P[n.id][1] - hh / 2, width: w, height: hh, rx: hh / 4,
      });
    }),
    s("rect", { class: "mm-view", x: v.x, y: v.y, width: v.w, height: v.h }));
  box.hidden = false;
}

// Centre the diagram on the world point under the pointer
function moveTo(ev) {
  if (!bounds) return;
  const r = $("#minimap svg").getBoundingClientRect();
  const wx = bounds.x + ((ev.clientX - r.left) / r.width) * bounds.w;
  const wy = bounds.y + ((ev.clientY - r.top) / r.height) * bounds.h;
  const svg = $("#diagram");
  S.view.x = (svg.clientWidth || 800) / 2 - wx * S.view.k;
  S.view.y = (svg.clientHeight || 500) / 2 - wy * S.view.k;
  applyView(svg, "viewport", S.view);
}

function setup() {
  const box = $("#minimap");
  let dragging = false;
  box.addEventListener("pointerdown", (ev) => {
    dragging = true;
    box.setPointerCapture(ev.pointerId);
    moveTo(ev);
  });
  box.addEventListener("pointermove", (ev) => { if (dragging) moveTo(ev); });
  box.addEventListener("pointerup", () => { dragging = false; });
  window.addEventListener("resize", update);
}

window.Minimap = { update };
setup();
})();
