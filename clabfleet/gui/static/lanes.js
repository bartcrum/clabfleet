"use strict";

// ---------------------------------------------------------------------------
// Host lanes (C4): in a multi-host lab, one tinted lane per host, its nodes
// moved into it (keeping their layout within the host), so links between
// hosts (VXLAN) are the ones crossing lane borders. Turning it off puts the
// nodes back; Save layout keeps the lanes.
//
// Uses app.js globals: S, $, s, NODE_W, NODE_H, nodeHost, renderDiagram,
// announce.
// ---------------------------------------------------------------------------

(() => {

const GAP = 36;          // between lanes
const PALETTE = 8;       // --c0 .. --c7
let on = false;
let before = null;       // positions to restore when turned off

function hostOf(name) {
  const node = S.detail?.nodes?.find((n) => n.name === name);
  return nodeHost(S.detail?.name, name) || node?.host_pin || "";
}

function hosts() {
  const by = new Map();
  for (const n of S.detail?.nodes || []) {
    const host = hostOf(n.name) || "not placed";
    if (!by.has(host)) by.set(host, []);
    by.get(host).push(n.name);
  }
  // Placed hosts by name, the unplaced last
  return [...by.entries()].sort(([a], [b]) => (a === "not placed") - (b === "not placed") || a.localeCompare(b));
}

function available() {
  return !!S.state?.multi_host && S.detail && !S.detail.error && hosts().filter(([h]) => h !== "not placed").length >= 2;
}

function layout() {
  const P = S.positions;
  let top = 0;
  for (const [, names] of hosts()) {
    const ys = names.filter((n) => P[n]).map((n) => P[n][1]);
    if (!ys.length) continue;
    const minY = Math.min(...ys), maxY = Math.max(...ys);
    for (const n of names) if (P[n]) P[n] = [P[n][0], top + NODE_H * 1.4 + (P[n][1] - minY)];
    top += (maxY - minY) + NODE_H * 2.8 + GAP;
  }
}

function toggle() {
  if (!available()) return;
  on = !on;
  if (on) {
    before = JSON.parse(JSON.stringify(S.positions));
    layout();
  } else if (before) {
    S.positions = before;
    before = null;
  }
  announce(on ? "Nodes grouped in one lane per host" : "Lanes off");
  renderDiagram(true);
}

// Called by renderDiagram: the lanes behind everything else
function decorate(g, P) {
  const btn = $("#lanes-toggle");
  const can = available();
  if (btn) {
    btn.hidden = !can;
    btn.setAttribute("aria-pressed", String(on && can));
    btn.classList.toggle("active", on && can);
  }
  if (!on || !can) return;
  const all = Object.values(P);
  if (!all.length) return;
  const x0 = Math.min(...all.map((p) => p[0])) - NODE_W, x1 = Math.max(...all.map((p) => p[0])) + NODE_W;
  const first = g.firstChild;
  hosts().forEach(([host, names], i) => {
    const ys = names.filter((n) => P[n]).map((n) => P[n][1]);
    if (!ys.length) return;
    const y0 = Math.min(...ys) - NODE_H * 1.2, y1 = Math.max(...ys) + NODE_H * 1.2;
    g.insertBefore(s("g", { class: "lane", style: `--c: var(--c${i % PALETTE})` },
      s("rect", { x: x0, y: y0, width: x1 - x0, height: y1 - y0, rx: 14 }),
      s("text", { x: x0 + 12, y: y0 + 18 }, host)), first);
  });
}

function reset() {
  on = false;
  before = null;
}

function setup() {
  $("#lanes-toggle").addEventListener("click", toggle);
}

window.Lanes = { decorate, toggle, reset, get on() { return on; } };
setup();
})();
