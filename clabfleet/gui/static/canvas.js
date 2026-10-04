"use strict";

// ---------------------------------------------------------------------------
// Canvas toolkit, shared by the Diagram and Routing tabs: where nodes go
// when the file gives no positions (tiers by role, else a force layout),
// interface labels, hover focus, zoom and pan, pointer and keyboard
// handling. Each canvas passes its own view and callbacks.
//
// Uses app.js globals: S, $, s, naturalCmp, NODE_W, NODE_H, announce.
// ---------------------------------------------------------------------------

// --- layout ---------------------------------------------------------------------------

function autoLayout(nodes, links, fixed) {
  // Small force-directed layout; fixed positions are kept as anchors.
  const pos = {};
  const n = nodes.length;
  nodes.forEach((nd, i) => {
    pos[nd.id] = fixed[nd.id] ? [...fixed[nd.id]]
      : [Math.cos((2 * Math.PI * i) / n) * 220 * Math.sqrt(n / 6 + 0.5),
         Math.sin((2 * Math.PI * i) / n) * 160 * Math.sqrt(n / 6 + 0.5)];
  });
  const k = 190;
  let temp = 80;
  for (let it = 0; it < 500; it++) {
    const disp = Object.fromEntries(nodes.map((nd) => [nd.id, [0, 0]]));
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const a = nodes[i].id, b = nodes[j].id;
        let dx = pos[a][0] - pos[b][0], dy = pos[a][1] - pos[b][1];
        let d = Math.hypot(dx, dy) || 0.01;
        const f = (k * k) / d;
        dx /= d; dy /= d;
        disp[a][0] += dx * f; disp[a][1] += dy * f;
        disp[b][0] -= dx * f; disp[b][1] -= dy * f;
      }
    }
    for (const l of links) {
      const a = l.a.id, b = l.b.id;
      let dx = pos[a][0] - pos[b][0], dy = pos[a][1] - pos[b][1];
      const d = Math.hypot(dx, dy) || 0.01;
      const f = (d * d) / k;
      dx /= d; dy /= d;
      disp[a][0] -= dx * f; disp[a][1] -= dy * f;
      disp[b][0] += dx * f; disp[b][1] += dy * f;
    }
    for (const nd of nodes) {
      if (fixed[nd.id]) continue;
      const [dx, dy] = disp[nd.id];
      const d = Math.hypot(dx, dy) || 0.01;
      pos[nd.id][0] += (dx / d) * Math.min(d, temp) - pos[nd.id][0] * 0.002;
      pos[nd.id][1] += (dy / d) * Math.min(d, temp) - pos[nd.id][1] * 0.002;
    }
    temp = Math.max(2, temp * 0.985);
  }
  return pos;
}

// Role keywords → row (top to bottom) for the tiered layout
const TIERS = [
  /(super-?spine|^ss\d|core|spine|wan|border|^pe\d|^p\d)/i,
  /(dist|agg|^ds\d)/i,
  /(leaf|access|tor|^sw|switch|edge)/i,
  /(host|server|client|srv|^pc|^h\d|linux)/i,
];

function tierOf(nd) {
  const name = nd.pseudo ? "" : nd.id;
  for (let i = 0; i < TIERS.length; i++) if (TIERS[i].test(name)) return i;
  if (!nd.pseudo && nd.node && ["linux", "bridge", "ovs-bridge"].includes(nd.node.kind)) return 3;
  return -1;
}

function tieredLayout(nodes, links) {
  // Rows by role; pseudo endpoints (host:, macvlan:, ...) go one row below
  // their node. Within a row, order by the neighbours' average position in
  // the row above (one barycenter pass) to reduce crossings.
  const real = nodes.filter((n) => !n.pseudo);
  const tier = {};
  for (const n of real) tier[n.id] = tierOf(n);
  const used = [...new Set(Object.values(tier))].sort((a, b) => a - b);
  const rowOf = Object.fromEntries(real.map((n) => [n.id, used.indexOf(tier[n.id])]));
  const nbrs = {};
  for (const l of links) {
    (nbrs[l.a.id] ||= []).push(l.b.id);
    (nbrs[l.b.id] ||= []).push(l.a.id);
  }
  for (const n of nodes.filter((n) => n.pseudo)) {
    const owner = (nbrs[n.id] || [])[0];
    rowOf[n.id] = (rowOf[owner] ?? used.length - 1) + 1;
  }
  const rows = [];
  for (const n of nodes) (rows[rowOf[n.id]] ||= []).push(n.id);
  const pos = {};
  const GAP_X = 240, GAP_Y = 160;
  rows.forEach((row, r) => {
    if (!row) return;
    row.sort(naturalCmp);
    if (r > 0) {
      const bary = (id) => {
        const xs = (nbrs[id] || []).filter((m) => pos[m] && rowOf[m] < r).map((m) => pos[m][0]);
        return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
      };
      const keyed = row.map((id, i) => [id, bary(id) ?? i * GAP_X, i]);
      keyed.sort((a, b) => a[1] - b[1] || a[2] - b[2]);
      row.splice(0, row.length, ...keyed.map((k) => k[0]));
    }
    row.forEach((id, i) => { pos[id] = [(i - (row.length - 1) / 2) * GAP_X, r * GAP_Y]; });
  });
  return pos;
}

function separate(pos, nodes, fixed) {
  // Push apart overlapping node boxes (force layout can stack them)
  const minDx = NODE_W + 30, minDy = NODE_H + 40;
  for (let it = 0; it < 60; it++) {
    let moved = false;
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = pos[nodes[i].id], b = pos[nodes[j].id];
        const dx = b[0] - a[0], dy = b[1] - a[1];
        const ox = minDx - Math.abs(dx), oy = minDy - Math.abs(dy);
        if (ox <= 0 || oy <= 0) continue;
        moved = true;
        const fa = fixed[nodes[i].id] ? 0 : 1, fb = fixed[nodes[j].id] ? 0 : 1;
        if (!fa && !fb) continue;
        const share = 1 / (fa + fb);
        if (ox / minDx < oy / minDy) {
          const push = (ox + 1) * share * (dx >= 0 ? 1 : -1);
          a[0] -= push * fa; b[0] += push * fb;
        } else {
          const push = (oy + 1) * share * (dy >= 0 ? 1 : -1);
          a[1] -= push * fa; b[1] += push * fb;
        }
      }
    }
    if (!moved) break;
  }
  return pos;
}

function ensurePositions(model) {
  const fixed = {};
  for (const nd of model.nodes) {
    if (S.positions[nd.id]) fixed[nd.id] = S.positions[nd.id];
    else if (nd.node?.pos) fixed[nd.id] = nd.node.pos;
  }
  const missing = model.nodes.some((nd) => !fixed[nd.id]);
  let pos = fixed;
  if (missing) {
    const real = model.nodes.filter((n) => !n.pseudo);
    const roleNamed = real.filter((n) => tierOf(n) >= 0).length;
    const useTiers = !Object.keys(fixed).length && real.length > 1 && roleNamed === real.length;
    pos = useTiers ? tieredLayout(model.nodes, model.links)
      : separate(autoLayout(model.nodes, model.links, fixed), model.nodes, fixed);
  }
  for (const nd of model.nodes) S.positions[nd.id] = pos[nd.id];
}

// --- labels and hover focus -----------------------------------------------------------

// Interface label just outside the node box, where the link leaves it.
// `alt` puts the far end's label below a horizontal link so the two
// labels of a short link don't collide.
function ifaceLabel(center, toward, w, hgt, text, alt) {
  let dx = toward[0] - center[0], dy = toward[1] - center[1];
  const len = Math.hypot(dx, dy) || 1;
  dx /= len; dy /= len;
  const edgeX = Math.abs(dx) > 1e-6 ? w / 2 / Math.abs(dx) : Infinity;
  const edgeY = Math.abs(dy) > 1e-6 ? hgt / 2 / Math.abs(dy) : Infinity;
  let x, y, anchor;
  if (edgeX < edgeY) {           // leaves through the left/right side
    x = center[0] + dx * (edgeX + 6);
    y = center[1] + dy * (edgeX + 6) + (alt ? 13 : -5);
    anchor = dx > 0 ? "start" : "end";
  } else {                       // leaves through the top/bottom
    x = center[0] + dx * (edgeY + 4);
    y = center[1] + dy * (edgeY + 4) + (dy > 0 ? 12 : -5);
    anchor = Math.abs(dx) < 0.25 ? "middle" : dx > 0 ? "start" : "end";
    if (anchor !== "middle") x += dx > 0 ? 5 : -5;
  }
  return s("text", { class: "iface", x, y, "text-anchor": anchor }, text);
}

// Focus on hover or selection: the node, its links and its neighbours stay,
// everything else fades. Nodes carry data-f (their id), links and their
// labels data-ends ("a|b"); used by the Diagram and Routing tabs.
function applyFocus(svg, id) {
  if (!id) { svg.classList.remove("focusing"); return; }
  const near = new Set([id]);
  for (const el of svg.querySelectorAll("[data-ends]")) {
    const [a, b] = el.dataset.ends.split("|");
    const on = a === id || b === id;
    el.classList.toggle("hl", on);
    if (on) { near.add(a); near.add(b); }
  }
  for (const el of svg.querySelectorAll("[data-f]")) el.classList.toggle("hl", near.has(el.dataset.f));
  svg.classList.add("focusing");
}

// Wires hover focus on ``svg``; ``selected()`` gives the node to focus when
// nothing is hovered. Returns a function to call after each re-render.
function setupFocus(svg, selected) {
  let hover = null;
  const refresh = () => applyFocus(svg, hover || selected());
  svg.addEventListener("pointerover", (ev) => {
    if (ev.buttons) return;  // dragging or panning
    const f = ev.target.closest("[data-f]")?.dataset.f || null;
    if (f !== hover) { hover = f; refresh(); }
  });
  svg.addEventListener("pointerleave", () => { hover = null; refresh(); });
  return refresh;
}

// --- zoom, pan, pointer and keys ------------------------------------------------------

// Zoom and pan helpers for both canvases. ``view`` is {x, y, k}; apply()
// writes it to the canvas.
const ZOOM_MIN = 0.2, ZOOM_MAX = 3, LOD_FAR = 0.6;

// Below LOD_FAR, interface names, kinds and edge labels hide (C3): names
// and status stay readable instead of piling up
function applyView(svg, viewportId, view) {
  svg.querySelector(`#${viewportId}`)?.setAttribute("transform", `translate(${view.x},${view.y}) scale(${view.k})`);
  svg.classList.toggle("lod-far", view.k < LOD_FAR);
  const level = svg.parentElement.querySelector(".zoom-level");
  if (level) level.textContent = `${Math.round(view.k * 100)}%`;
  if (viewportId === "viewport") window.Minimap?.update();
}

// Zoom by ``factor`` around a point of the canvas (its centre by default)
function zoomAt(svg, view, factor, mx, my) {
  if (mx == null) { mx = svg.clientWidth / 2; my = svg.clientHeight / 2; }
  const k = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, view.k * factor));
  view.x = mx - ((mx - view.x) / view.k) * k;
  view.y = my - ((my - view.y) / view.k) * k;
  view.k = k;
}

// Zoom buttons and keys (+ - 0 1, arrows) for one canvas. ``fit()`` fits
// the lab, ``apply()`` redraws the transform; returns nothing.
function setupZoom(svg, view, fit, apply) {
  const tools = svg.parentElement.querySelector(".zoom-tools");
  const act = (what) => {
    if (what === "fit") return fit();
    if (what === "in") zoomAt(svg, view(), 1.25);
    else if (what === "out") zoomAt(svg, view(), 0.8);
    else if (what === "reset") zoomAt(svg, view(), 1 / view().k);
    apply();
  };
  tools?.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-zoom]");
    if (btn) act(btn.dataset.zoom);
  });
  svg.addEventListener("keydown", (ev) => {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const keys = { "+": "in", "=": "in", "-": "out", "_": "out", 0: "fit", 1: "reset" };
    const pan = { ArrowLeft: [40, 0], ArrowRight: [-40, 0], ArrowUp: [0, 40], ArrowDown: [0, -40] };
    if (keys[ev.key]) { ev.preventDefault(); act(keys[ev.key]); }
    else if (pan[ev.key] && !ev.defaultPrevented) {
      ev.preventDefault();
      view().x += pan[ev.key][0];
      view().y += pan[ev.key][1];
      apply();
    }
  });
}

// Pointer handling for one canvas: a press that moves drags a node or pans,
// one that does not is a click; the wheel zooms at the pointer. ``o``:
// {view(), apply(), press(ev) -> {type: "node", id, start: [x, y]} or
// {type: "pan"}, with whatever else click() needs, move(drag, pos) for a
// node being dragged, drop(drag) when it is let go, click(drag)}.
function setupCanvasPointer(svg, o) {
  let drag = null;

  svg.addEventListener("pointerdown", (ev) => {
    svg.setPointerCapture(ev.pointerId);
    drag = { ...o.press(ev), sx: ev.clientX, sy: ev.clientY, moved: false };
    if (drag.type === "pan") {
      drag.start = { ...o.view() };
      svg.classList.add("panning");
    }
  });
  svg.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    const dx = ev.clientX - drag.sx, dy = ev.clientY - drag.sy;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    if (!drag.moved) return;
    const view = o.view();
    if (drag.type === "node") {
      o.move(drag, [drag.start[0] + dx / view.k, drag.start[1] + dy / view.k]);
    } else {
      view.x = drag.start.x + dx;
      view.y = drag.start.y + dy;
      o.apply();
    }
  });
  svg.addEventListener("pointerup", () => {
    svg.classList.remove("panning");
    if (!drag) return;
    if (!drag.moved) o.click(drag);
    else if (drag.type === "node") o.drop(drag);
    drag = null;
  });
  svg.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const rect = svg.getBoundingClientRect();
    const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
    zoomAt(svg, o.view(), Math.exp(-ev.deltaY * 0.0015), mx, my);
    o.apply();
  }, { passive: false });
}

// Clicks re-render the canvas, so double clicks are detected here rather
// than with the browser's dblclick event (its target may already be gone).
// Returns a function for one canvas: is this click on ``id`` the second of two?
function doubleClicks() {
  let last = { id: null, t: 0 };
  return (id) => {
    const now = Date.now();
    const isDouble = last.id === id && now - last.t < 400;
    last = { id, t: isDouble ? 0 : now };
    return isDouble;
  };
}

// The box around node positions, with room for the boxes and their labels
function nodeBounds(pts, above = 1.5) {
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  return {
    x0: Math.min(...xs) - NODE_W, x1: Math.max(...xs) + NODE_W,
    y0: Math.min(...ys) - NODE_H * above, y1: Math.max(...ys) + NODE_H * 1.5,
  };
}

// The view that centres a box of the drawing ({x0, y0, x1, y1}) in the
// canvas, as large as fits (140% at most)
function fitBounds(svg, box) {
  const w = svg.clientWidth || 800, hgt = svg.clientHeight || 500;
  const k = Math.min(1.4, Math.min(w / (box.x1 - box.x0), hgt / (box.y1 - box.y0)));
  return { k, x: w / 2 - ((box.x0 + box.x1) / 2) * k, y: hgt / 2 - ((box.y0 + box.y1) / 2) * k };
}

// Keyboard navigation between nodes on a canvas (E1). Focus via the
// keyboard lands on a node; arrows move to the nearest node that way,
// connected ones first; Enter selects; Escape leaves the nodes, and arrows
// pan again (Shift+arrows always pan). ``o``: {nodes() -> [{id, pos}],
// links() -> [[a, b]], label(id), select(id), open(id)?, view(), apply()}.
function setupNodeKeys(svg, o) {
  const DIRS = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
  const set = (id) => {
    svg.dataset.kb = id || "";
    markKb(svg);
    if (!id) return;
    const n = o.nodes().find((x) => x.id === id);
    if (n) { panToShow(svg, o.view(), n.pos); o.apply(); }
    announce(o.label(id));
  };
  const step = (from, [dx, dy]) => {
    const nodes = o.nodes(), here = nodes.find((n) => n.id === from);
    if (!here) return nodes[0]?.id;
    const linked = new Set(o.links().flatMap(([a, b]) => (a === from ? [b] : b === from ? [a] : [])));
    let best = null;
    for (const pool of [nodes.filter((n) => linked.has(n.id)), nodes]) {
      for (const n of pool) {
        if (n.id === from) continue;
        const vx = n.pos[0] - here.pos[0], vy = n.pos[1] - here.pos[1];
        const dist = Math.hypot(vx, vy) || 1, cos = (vx * dx + vy * dy) / dist;
        if (cos < 0.35) continue;  // not that way
        const score = dist * (2 - cos);
        if (!best || score < best.score) best = { id: n.id, score };
      }
      if (best) break;
    }
    return best?.id;
  };
  svg.addEventListener("focus", () => {
    if (svg.dataset.kb || !svg.matches(":focus-visible")) return;
    const nodes = [...o.nodes()].sort((a, b) => a.pos[1] - b.pos[1] || a.pos[0] - b.pos[0]);
    set(o.selected?.() || nodes[0]?.id);
  });
  svg.addEventListener("keydown", (ev) => {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const kb = svg.dataset.kb;
    if (DIRS[ev.key] && !ev.shiftKey && kb) {
      ev.preventDefault();  // not a pan
      const next = step(kb, DIRS[ev.key]);
      if (next) set(next); else announce(`Nothing that way from ${kb}`);
    } else if (ev.key === "Escape" && kb) {
      ev.preventDefault();
      set(null);
      announce("Left the nodes: arrow keys pan");
    } else if ((ev.key === "Enter" || ev.key === " ") && kb) {
      ev.preventDefault();
      o.select(kb);
    } else if (ev.key.toLowerCase() === "t" && kb && o.open) {
      ev.preventDefault();
      o.open(kb);
    } else if (ev.key.toLowerCase() === "n" && !kb) {
      ev.preventDefault();  // back onto the nodes
      set(o.selected?.() || o.nodes()[0]?.id);
    }
  });
}

// Mark the keyboard's node after a redraw, and point assistive tech at it
function markKb(svg) {
  for (const el of svg.querySelectorAll(".node.kb")) el.classList.remove("kb");
  const id = svg.dataset.kb;
  const el = id && [...svg.querySelectorAll(".node")].find((n) => n.dataset.id === id);
  if (el) {
    el.classList.add("kb");
    el.id = `${svg.id}-kb`;
    svg.setAttribute("aria-activedescendant", el.id);
  } else {
    svg.removeAttribute("aria-activedescendant");
  }
}

// Pan so a point of the drawing is well inside the canvas
function panToShow(svg, view, [x, y]) {
  const w = svg.clientWidth, hgt = svg.clientHeight, m = 90;
  const sx = x * view.k + view.x, sy = y * view.k + view.y;
  if (sx < m) view.x += m - sx; else if (sx > w - m) view.x -= sx - (w - m);
  if (sy < m) view.y += m - sy; else if (sy > hgt - m) view.y -= sy - (hgt - m);
}
