"use strict";

// ---------------------------------------------------------------------------
// Path trace (feature 1): which way traffic from a node to another node or
// an address goes, read from the routing tables of the running lab. The
// hops are listed in a dock tab and drawn on the diagram: links on the path
// highlighted (every ECMP branch), VXLAN hops as arcs between the VTEPs.
//
// Uses app.js globals: S, $, h, s, api, toast, topoPath, announce,
// renderDiagram; and from dock.js: activatePane.
// ---------------------------------------------------------------------------

(() => {

let last = null;  // {lab, src, dst, dst_node, reached, hops, edges, truncated}

function nodes() {
  return S.selected?.type === "topo" && S.detail && !S.detail.error ? S.detail.nodes.map((n) => n.name) : [];
}

function fillForm(src) {
  const names = nodes();
  const sel = $("#trace-src");
  const keep = src || sel.value || S.selectedNode || names[0] || "";
  sel.replaceChildren(...names.map((n) => h("option", { value: n, selected: n === keep }, n)));
  $("#trace-nodes").replaceChildren(...names.map((n) => h("option", { value: n })));
}

function hopRow(hop, i) {
  return h("li", { class: "trace-hop" },
    h("span", { class: "trace-n mono" }, String(i + 1)),
    h("div", {},
      h("div", {}, h("b", {}, hop.node), hop.vrf && hop.vrf !== "default" ? h("span", { class: "muted small" }, ` VRF ${hop.vrf}`) : null),
      hop.route ? h("div", { class: "mono small" }, hop.route) : null,
      hop.error ? h("div", { class: "run-err small" }, hop.error) : null,
      ...(hop.notes || []).map((n) => h("div", { class: "muted small" }, n))));
}

function edgeText(e) {
  if (e.overlay) return `${e.a} ⇢ ${e.b}  VXLAN${e.vni ? ` VNI ${e.vni}` : ""}${e.flood ? " (flooded: address not learned)" : ""}`;
  return `${e.a}:${e.a_iface} → ${e.b}:${e.b_iface}`;
}

function render() {
  const out = $("#trace-out");
  if (!last || last.lab !== S.detail?.name) {
    out.replaceChildren(h("p", { class: "muted small run-hint" },
      "Follows the routing and bridging tables hop by hop, as they are now: every equal-cost branch, and VXLAN between VTEPs. Nothing is sent and nothing changes."));
    return;
  }
  const ecmp = new Set(last.edges.map((e) => e.a)).size < last.edges.length;
  out.replaceChildren(
    h("p", { class: `trace-verdict ${last.reached ? "ok" : "err"}` },
      last.reached ? `${last.src} reaches ${last.dst_node || last.dst}` : `${last.src} does not reach ${last.dst_node || last.dst}`,
      last.dst_node ? h("span", { class: "muted small" }, ` (${last.dst})`) : null,
      ecmp ? h("span", { class: "muted small" }, " · equal-cost paths") : null,
      last.truncated ? h("span", { class: "muted small" }, " · stopped after 16 hops") : null),
    h("div", { class: "trace-cols" },
      h("ol", { class: "trace-hops" }, last.hops.map(hopRow)),
      h("ul", { class: "trace-edges mono small" }, last.edges.map((e) => h("li", { class: e.overlay ? "overlay" : "" }, edgeText(e))))));
}

async function trace(ev) {
  ev?.preventDefault();
  if (S.selected?.type !== "topo") { toast("Open a lab first"); return; }
  const src = $("#trace-src").value, dst = $("#trace-dst").value.trim();
  if (!src || !dst) return;
  const btn = $("#trace-go");
  btn.disabled = true;
  btn.textContent = "Tracing…";
  try {
    last = { lab: S.detail.name, ...(await api(`/api/trace/${topoPath(S.selected.id)}`, {
      method: "POST", body: JSON.stringify({ src, dst }),
    })) };
    announce(last.reached ? `Path found in ${last.hops.length} hops` : "No path");
    if (!$("#view-diagram").hidden) renderDiagram(false);
  } catch (e) {
    toast(`Not traced: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Trace";
  }
  render();
}

// Called by renderDiagram: mark the path on the freshly drawn diagram
function decorate(g, P) {
  if (!last || last.lab !== S.detail?.name) return;
  const onPath = new Set(last.hops.map((x) => x.node));
  for (const el of g.querySelectorAll(".node[data-id]")) {
    const id = el.dataset.id;
    if (!onPath.has(id)) continue;
    el.classList.add("traced");
    if (id === last.src) el.classList.add("trace-src");
    if (id === (last.dst_node || "")) el.classList.add("trace-dst");
  }
  const under = new Set(last.edges.filter((e) => !e.overlay).map((e) => [e.a, e.b].sort().join("|")));
  for (const el of g.querySelectorAll("path.link[data-ends]")) {
    if (under.has(el.dataset.ends.split("|").sort().join("|"))) el.classList.add("traced");
  }
  // VXLAN hops: an arc between the VTEPs, behind the nodes
  const first = g.querySelector(".node, .pseudo");
  for (const e of last.edges.filter((x) => x.overlay)) {
    const p0 = P[e.a], p1 = P[e.b];
    if (!p0 || !p1) continue;
    const dx = p1[0] - p0[0], dy = p1[1] - p0[1], len = Math.hypot(dx, dy) || 1;
    const bend = Math.min(90, len / 3);
    const c = [(p0[0] + p1[0]) / 2 + (dy / len) * bend, (p0[1] + p1[1]) / 2 - (dx / len) * bend];
    const path = s("path", {
      class: `trace-overlay${e.flood ? " flood" : ""}`, d: `M${p0[0]},${p0[1]} Q${c[0]},${c[1]} ${p1[0]},${p1[1]}`,
    }, s("title", {}, edgeText(e)));
    g.insertBefore(path, first);
  }
}

function clear() {
  last = null;
  render();
  if (!$("#view-diagram").hidden) renderDiagram(false);
}

// Open the tab, from a node (the selected one by default) to ``dst``
function open(src, dst) {
  activatePane("trace", true);
  fillForm(src);
  if (dst !== undefined) $("#trace-dst").value = dst;
  render();
  if (src && dst) trace();
  else $("#trace-dst").focus();
}

function setup() {
  $("#trace-form").addEventListener("submit", trace);
  $("#trace-clear").addEventListener("click", clear);
  document.querySelector('.dock-tab[data-pane="trace"]').addEventListener("click", () => {
    activatePane("trace");
    fillForm();
    render();
  });
  render();
}

window.Trace = { open, decorate, reset() { last = null; render(); } };
setup();
})();
