"use strict";

// ---------------------------------------------------------------------------
// Routing tab: intended OSPF / BGP / EVPN views, read from the startup configs
// (/api/routing/<id>), and in Live mode the running state laid over them
// (/api/routing-live/<id>). Nodes sit where the Diagram tab puts them, so
// both tabs share one layout; dragging a node here moves it there too.
//
// Uses app.js globals: S, $, h, s, api, toast, NODE_W, NODE_H, PSEUDO_W,
// PSEUDO_H, graphModel, ensurePositions, savePositions, truncate, ifaceLabel,
// nodeRuntime, nodeState, canOperate, openTerminal.
// ---------------------------------------------------------------------------

const RT = {
  topo: null,           // topology id the data belongs to
  data: null,           // /api/routing/<id>
  loading: false,
  proto: null,          // "ospf" | "bgp" | "evpn"
  vni: "all",           // EVPN: "all" or a VNI number
  layer: "both",        // EVPN: "control" | "data" | "both"
  underlay: true,       // draw the physical links faintly
  showProblems: false,
  sel: null,            // {type: "node"|"edge", id}
  view: { x: 0, y: 0, k: 1 },
  fitted: false,
  extPos: {},           // positions of BGP peers outside the lab
  edges: {},            // edge id -> edge drawn in the current view
  mode: "intended",     // "intended" | "live"
  live: null,           // /api/routing-live/<id>, plus its topology id
  liveLoading: false,
};

const RT_PROTO_LABEL = { ospf: "OSPF", bgp: "BGP", evpn: "EVPN" };
const RT_PALETTE = 8;   // --c0 .. --c7 in app.css
const RT_VIEW_PREFS = "clab-routing";

function rtPrefsLoad() {
  try {
    const p = JSON.parse(localStorage.getItem(RT_VIEW_PREFS)) || {};
    if (typeof p.underlay === "boolean") RT.underlay = p.underlay;
    if (["control", "data", "both"].includes(p.layer)) RT.layer = p.layer;
    if (["intended", "live"].includes(p.mode)) RT.mode = p.mode;
  } catch { /* private mode */ }
}
function rtPrefsSave() {
  try { localStorage.setItem(RT_VIEW_PREFS, JSON.stringify({ underlay: RT.underlay, layer: RT.layer, mode: RT.mode })); } catch { /* private mode */ }
}

function rtVisible() {
  return !$("#view-routing").hidden;
}

function rtTopoId() {
  return S.selected?.type === "topo" ? S.selected.id : null;
}

async function rtLoad() {
  const id = rtTopoId();
  if (!id || RT.loading) return;
  RT.loading = true;
  try {
    const data = await api(`/api/routing/${encodeURIComponent(id).replace(/%2F/g, "/")}`);
    if (rtTopoId() !== id) return;
    const first = RT.topo !== id;
    RT.topo = id;
    RT.data = data;
    if (first || !data.protocols?.includes(RT.proto)) RT.proto = data.protocols?.[0] || null;
    if (RT.vni !== "all" && !data.evpn?.vnis?.some((v) => String(v.vni) === String(RT.vni))) RT.vni = "all";
    if (first) { RT.sel = null; RT.fitted = false; RT.showProblems = false; }
  } catch (e) {
    toast(`Could not read the routing configs: ${e.message}`);
  } finally {
    RT.loading = false;
  }
  rtRender();
}

function rtDeployed() {
  return !!S.detail && !S.detail.error && labStatus(S.detail.name).deployed > 0;
}

function rtLiveOn() {
  return RT.mode === "live" && rtDeployed();
}

function rtLiveData() {
  return rtLiveOn() && RT.live?.id === rtTopoId() && RT.live.updated ? RT.live : null;
}

// The server probes at most every few seconds whoever asks; the first
// answer for a lab is empty while its probes run
async function rtLoadLive() {
  const id = rtTopoId();
  if (!id || RT.liveLoading || !rtLiveOn()) return;
  RT.liveLoading = true;
  try {
    const data = await api(`/api/routing-live/${encodeURIComponent(id).replace(/%2F/g, "/")}`);
    if (rtTopoId() !== id) return;
    RT.live = { ...data, id };
  } catch (e) {
    return;  // keep the last data; the next refresh tries again
  } finally {
    RT.liveLoading = false;
  }
  rtRender();
  renderLabHead();
  clearTimeout(RT.liveRetry);
  if (!RT.live.updated || RT.live.refreshing) RT.liveRetry = setTimeout(rtLoadLive, 2000);
}

// --- geometry ----------------------------------------------------------------

function rtHull(points) {
  // Monotone chain convex hull
  const pts = [...points].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  if (pts.length < 3) return pts;
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lower = [], upper = [];
  for (const p of pts) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop();
    lower.push(p);
  }
  for (const p of pts.reverse()) {
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop();
    upper.push(p);
  }
  return lower.slice(0, -1).concat(upper.slice(0, -1));
}

// Tinted region around a group of nodes, with a label on top
function rtGroup(ids, P, color, label) {
  const pad = 14;
  const corners = [];
  for (const id of ids) {
    const p = P[id];
    if (!p) continue;
    const w = NODE_W / 2 + pad, hh = NODE_H / 2 + pad;
    corners.push([p[0] - w, p[1] - hh], [p[0] + w, p[1] - hh], [p[0] + w, p[1] + hh], [p[0] - w, p[1] + hh]);
  }
  if (!corners.length) return null;
  const hull = rtHull(corners);
  const d = `M${hull.map((p) => p.join(",")).join("L")}Z`;
  const top = Math.min(...corners.map((c) => c[1]));
  const left = Math.min(...corners.map((c) => c[0]));
  return s("g", { class: "rt-group", style: `--c: var(--c${color % RT_PALETTE})` },
    s("g", { class: "tint" }, s("path", { d })),
    s("text", { x: left + 4, y: top - 6 }, label));
}

// Path between two nodes; parallel edges between one pair fan out
function rtEdgeGeom(p0, p1, idx, count, flip) {
  const dx = p1[0] - p0[0], dy = p1[1] - p0[1];
  const len = Math.hypot(dx, dy) || 1;
  const off = (idx - (count - 1) / 2) * 30 * (flip ? -1 : 1);
  const c = [(p0[0] + p1[0]) / 2 - (dy / len) * off, (p0[1] + p1[1]) / 2 + (dx / len) * off];
  return { d: `M${p0[0]},${p0[1]} Q${c[0]},${c[1]} ${p1[0]},${p1[1]}`, c,
           mid: [(p0[0] + 2 * c[0] + p1[0]) / 4, (p0[1] + 2 * c[1] + p1[1]) / 4] };
}

// VXLAN tunnels bend below the VTEPs (sessions go up to the spines), longer
// ones further, so tunnels between VTEPs in one row do not cross the others
function rtArcGeom(p0, p1) {
  const dx = p1[0] - p0[0], dy = p1[1] - p0[1];
  const len = Math.hypot(dx, dy) || 1;
  let nx = -dy / len, ny = dx / len;
  if (ny < 0 || (ny === 0 && nx < 0)) { nx = -nx; ny = -ny; }
  const off = Math.max(40, Math.min(len * 0.18, 130));
  const c = [(p0[0] + p1[0]) / 2 + nx * off * 2, (p0[1] + p1[1]) / 2 + ny * off * 2];
  return { d: `M${p0[0]},${p0[1]} Q${c[0]},${c[1]} ${p1[0]},${p1[1]}`, c,
           mid: [(p0[0] + 2 * c[0] + p1[0]) / 4, (p0[1] + 2 * c[1] + p1[1]) / 4 + 14] };
}

// --- per-protocol models -------------------------------------------------------
// Each returns {nodes: {id: {sub, badge, off, warn}}, edges: [...], groups: [...], legend: [...]}
// An edge: {id, a, b, ai, bi (iface labels), cls, color, title, label}

function rtProblemNodes(proto) {
  const out = new Set();
  for (const p of RT.data.problems || []) {
    if ((p.protocol === proto || p.protocol === "ip") && p.severity !== "info") out.add(p.node);
  }
  return out;
}

function rtColorIndex(values) {
  return Object.fromEntries(values.map((v, i) => [v, i]));
}

function rtOspfModel() {
  const o = RT.data.ospf;
  const areaColor = rtColorIndex(o.areas);
  const warn = rtProblemNodes("ospf");
  const nodes = {};
  for (const n of S.detail.nodes) {
    const on = o.nodes[n.name];
    nodes[n.name] = on
      ? { sub: `rid ${on.router_id}${on.router_id_configured ? "" : "*"}`, badge: on.abr ? "ABR" : "", warn: warn.has(n.name) }
      : { sub: "no OSPF", off: true };
  }
  const edges = o.adjacencies.map((a) => {
    const cost = (c) => (c != null ? ` (${c})` : "");
    return {
      id: a.id, a: a.a.node, b: a.b.node,
      ai: `${a.a.iface}${cost(a.a.cost)}`, bi: `${a.b.iface}${cost(a.b.cost)}`,
      cls: `rt-edge${a.problems.length ? " warn" : ""}`,
      color: a.area != null ? areaColor[a.area] : null,
      title: `${a.a.node} ${a.a.iface} ↔ ${a.b.node} ${a.b.iface}\n${a.subnet}` +
        (a.area != null ? `, area ${a.area}` : "") + (a.problems.length ? `\n⚠ ${a.problems.join("\n⚠ ")}` : ""),
    };
  });
  const groups = [];
  if (o.areas.length > 1) {
    for (const area of o.areas) {
      const members = Object.entries(o.nodes).filter(([, n]) => n.areas.includes(area)).map(([k]) => k);
      groups.push({ ids: members, color: areaColor[area], label: `Area ${area}` });
    }
  }
  const legend = [
    ...o.areas.map((a) => ({ swatch: "line", color: areaColor[a], text: `area ${a}` })),
    { swatch: "text", text: "rid …*", note: "router-id derived (not configured)" },
    { swatch: "text", text: "Et0/1 (10)", note: "interface (OSPF cost)" },
  ];
  return { nodes, edges, groups, legend };
}

function rtBgpModel() {
  const b = RT.data.bgp;
  const asColor = rtColorIndex(b.asns);
  const warn = rtProblemNodes("bgp");
  const nodes = {};
  for (const n of S.detail.nodes) {
    const bn = b.nodes[n.name];
    nodes[n.name] = bn
      ? { sub: `rid ${bn.router_id}${bn.router_id_configured ? "" : "*"}`, warn: warn.has(n.name) }
      : { sub: "no BGP", off: true };
  }
  for (const [id, ext] of Object.entries(b.external || {})) {
    nodes[id] = { external: true, label: ext.ip, sub: ext.asn ? `AS ${ext.asn}` : "external" };
  }
  const edges = b.sessions.map((x) => rtSessionEdge(x));
  // Group routers by AS (single-router ASes too: the tint shows the boundary)
  const groups = b.asns.map((asn) => ({
    ids: Object.entries(b.nodes).filter(([, n]) => n.asn === asn).map(([k]) => k),
    color: asColor[asn], label: `AS ${asn}`,
  }));
  const legend = [
    { swatch: "line", cls: "fam-ipv4", text: "eBGP IPv4" },
    { swatch: "line", cls: "fam-evpn", text: "EVPN" },
    { swatch: "line", cls: "fam-ipv4 ibgp", text: "iBGP" },
    { swatch: "line", cls: "one-sided", text: "one side only" },
    { swatch: "line", cls: "fam-ipv4 warn", text: "problem" },
  ];
  return { nodes, edges, groups, legend };
}

function rtSessionEdge(x) {
  const fam = x.families.includes("evpn") ? "fam-evpn" : "fam-ipv4";
  const cls = ["rt-edge", "session", fam, x.type, x.configured === "both" ? "" : "one-sided",
               x.problems.length ? "warn" : "", x.shutdown ? "shut" : ""].filter(Boolean).join(" ");
  const end = (e) => `${e.node}${e.iface ? ` ${e.iface}` : ""} ${e.ip} (AS ${e.asn || "?"})`;
  return {
    id: x.id, a: x.a.node, b: x.b.node,
    ai: "", bi: "",
    cls,
    title: `${end(x.a)}\n↔ ${end(x.b)}\n${x.type.toUpperCase()} · ${x.families.join(", ") || "no address family"}` +
      (x.multihop ? " · multihop" : "") + (x.configured === "both" ? "" : "\nconfigured on one side only") +
      (x.problems.length ? `\n⚠ ${x.problems.join("\n⚠ ")}` : ""),
  };
}

function rtEvpnModel() {
  const e = RT.data.evpn;
  const vni = RT.vni === "all" ? null : Number(RT.vni);
  const warn = rtProblemNodes("evpn");
  const speakers = new Set(e.speakers);
  const nodes = {};
  for (const n of S.detail.nodes) {
    const vt = e.vteps[n.name];
    if (vt) {
      const member = vni == null || [...vt.l2_vnis, ...vt.l3_vnis].some((x) => x.vni === vni);
      nodes[n.name] = { sub: `VTEP ${vt.ip || "?"}`, off: !member, warn: warn.has(n.name) };
    } else if (speakers.has(n.name)) {
      nodes[n.name] = { sub: "EVPN route server", badge: "RS", off: vni != null, warn: warn.has(n.name) };
    } else {
      nodes[n.name] = { sub: "no EVPN", off: true };
    }
  }
  const edges = [];
  if (RT.layer !== "data") {
    const byId = Object.fromEntries((RT.data.bgp?.sessions || []).map((x) => [x.id, x]));
    for (const id of e.sessions) {
      const x = byId[id];
      if (!x) continue;
      // With a VNI picked, keep the sessions of the VTEPs that carry it
      if (vni != null && nodes[x.a.node]?.off !== false && nodes[x.b.node]?.off !== false) continue;
      edges.push(rtSessionEdge(x));
    }
  }
  if (RT.layer !== "control") {
    const vniInfo = Object.fromEntries(e.vnis.map((v) => [v.vni, v]));
    for (const t of e.tunnels) {
      const vnis = vni == null ? t.vnis : t.vnis.filter((v) => v === vni);
      if (!vnis.length) continue;
      const shown = vnis.length > 3 ? `${vnis.slice(0, 3).join(", ")} +${vnis.length - 3}` : vnis.join(", ");
      edges.push({
        id: t.id, a: t.a.node, b: t.b.node, ai: "", bi: "", arc: true,
        cls: "rt-edge tunnel",
        label: shown,
        title: `VXLAN ${t.a.node} ${t.a.ip} ↔ ${t.b.node} ${t.b.ip}\n` +
          vnis.map((v) => `VNI ${v} (${vniInfo[v]?.type === "l3" ? `VRF ${vniInfo[v].vrfs.join(", ")}` : `VLAN ${vniInfo[v]?.vlans.join(", ")}`})`).join("\n"),
      });
    }
  }
  const legend = [
    { swatch: "line", cls: "fam-evpn", text: "BGP EVPN session" },
    { swatch: "line", cls: "tunnel", text: "VXLAN tunnel (shared VNIs)" },
  ];
  return { nodes, edges, groups: [], legend };
}

// --- live layer --------------------------------------------------------------------

const RT_LIVE_TEXT = { up: "up", down: "DOWN", partial: "partly up", unknown: "state not known", missing: "not configured" };

function rtUptime(sec) {
  if (sec == null) return "";
  const d = Math.floor(sec / 86400), hh = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return d ? `${d}d ${hh}h` : hh ? `${hh}h ${m}m` : `${m}m ${sec % 60}s`;
}

function rtLiveEntry(edgeId) {
  const L = rtLiveData();
  if (!L) return null;
  return L.ospf?.[edgeId] || L.bgp?.[edgeId] || L.vxlan?.[edgeId] || null;
}

// The extra (unintended) neighbours shown in the current protocol view
function rtExtras() {
  const L = rtLiveData();
  if (!L) return [];
  const pick = { ospf: ["ospf"], bgp: ["bgp", "evpn"], evpn: RT.layer === "data" ? [] : ["evpn"] }[RT.proto] || [];
  return pick.flatMap((k) => (L.extra?.[k] || []).map((e, i) => ({ ...e, kind: k, id: `extra-${k}-${i}` })));
}

// Colour edges by their running state and add what runs but is not intended
function rtApplyLive(model) {
  const L = rtLiveData();
  for (const e of model.edges) {
    const st = rtLiveEntry(e.id);
    if (!st) continue;
    e.cls = `${e.cls} live live-${st.state}${st.drift ? " drift" : ""}`;
    const sides = st.families ? Object.entries(st.families).map(([fam, f]) => [fam, f.a, f.b]) : [["", st.a, st.b]];
    const lines = sides.map(([fam, a, b]) => `${fam ? `${fam}: ` : ""}${e.a} ${rtSideText(a)} · ${e.b} ${rtSideText(b)}`);
    e.title = `${e.title}\nLive: ${RT_LIVE_TEXT[st.state] || st.state}${st.detail ? ` (${st.detail})` : ""}\n${lines.join("\n")}`;
  }
  for (const x of rtExtras()) {
    const peer = x.peer || `ext:${x.ip}`;
    if (!model.nodes[peer]) {
      model.nodes[peer] = { external: true, label: x.ip, sub: x.asn ? `AS ${x.asn}` : x.router_id ? `rid ${x.router_id}` : "not in the lab" };
    }
    model.edges.push({
      id: x.id, a: x.node, b: peer, ai: "", bi: "",
      cls: `rt-edge extra live live-${x.state}`,
      title: `${x.node} → ${x.peer || x.ip}: running but not in the startup config\n` +
        `${x.kind.toUpperCase()} neighbor ${x.ip}${x.asn ? ` AS ${x.asn}` : ""} · ${x.detail || x.state}`,
    });
  }
  for (const [node, n] of Object.entries(L.nodes || {})) {
    if (Object.keys(n.errors || {}).length && model.nodes[node]) {
      model.nodes[node].warn = true;
    }
  }
}

function rtSideText(side) {
  if (!side) return "";
  if (side.state === "up") return `up${side.uptime != null ? ` ${rtUptime(side.uptime)}` : ""}${side.pfx_rcvd != null ? `, ${side.pfx_rcvd} pfx` : ""}`;
  return `${RT_LIVE_TEXT[side.state] || side.state}${side.detail && side.detail !== "Established" ? ` (${side.detail})` : ""}`;
}

// --- rendering -------------------------------------------------------------------

function rtRender() {
  if (!rtVisible()) return;
  const svg = $("#routing");
  const msg = $("#routing-empty");
  rtRenderBar();
  const d = RT.data;
  const fail = (text) => {
    svg.replaceChildren();
    msg.hidden = false;
    msg.replaceChildren(...text);
    $("#routing-card").hidden = true;
    $("#routing-legend").hidden = true;
    $("#routing-problems").hidden = true;
  };
  if (!S.detail || S.detail.error) return fail([`This topology could not be loaded${S.detail?.error ? `: ${S.detail.error}` : ""}.`]);
  if (!d || RT.topo !== rtTopoId()) { fail(["Reading configs…"]); rtLoad(); return; }
  if (d.error) return fail([`Could not read the configs: ${d.error}`]);
  if (!RT.proto) {
    const unread = (d.unparsed || []).filter((u) => u.reason !== "no startup-config");
    return fail([
      h("p", {}, "No OSPF, BGP or EVPN configuration was found in the nodes' startup configs."),
      unread.length ? h("ul", {}, unread.map((u) => h("li", {}, `${u.node}: ${u.reason}`))) : null,
    ]);
  }
  msg.hidden = true;

  // Positions: the diagram's, computed the same way if it was never shown
  const physical = graphModel();
  if (S.detail.nodes.some((n) => !S.positions[n.name])) ensurePositions(physical);
  const P = { ...S.positions };
  const model = { ospf: rtOspfModel, bgp: rtBgpModel, evpn: rtEvpnModel }[RT.proto]();
  if (rtLiveData()) rtApplyLive(model);
  rtPlaceExternals(model, P);
  RT.edges = Object.fromEntries(model.edges.map((e) => [e.id, e]));

  const g = s("g", { id: "rt-viewport", transform: `translate(${RT.view.x},${RT.view.y}) scale(${RT.view.k})` });
  const labels = s("g", { class: "labels" });

  for (const grp of model.groups) {
    const el = rtGroup(grp.ids, P, grp.color, grp.label);
    if (el) g.append(el);
  }

  if (RT.underlay) {
    for (const l of physical.links) {
      if (l.special || !P[l.a.id] || !P[l.b.id]) continue;
      const [p0, p1] = [P[l.a.id], P[l.b.id]];
      g.append(s("line", { class: "rt-underlay", x1: p0[0], y1: p0[1], x2: p1[0], y2: p1[1] }));
    }
  }

  // Edges, fanned out per node pair
  const pairCount = {}, pairIndex = {};
  const key = (e) => [e.a, e.b].sort().join("|");
  for (const e of model.edges) pairCount[key(e)] = (pairCount[key(e)] || 0) + 1;
  for (const e of model.edges) {
    const p0 = P[e.a], p1 = P[e.b];
    if (!p0 || !p1) continue;
    const k = key(e);
    const idx = (pairIndex[k] = (pairIndex[k] ?? -1) + 1);
    const geo = e.arc ? rtArcGeom(p0, p1) : rtEdgeGeom(p0, p1, idx, pairCount[k], e.a > e.b);
    const selected = RT.sel?.type === "edge" && RT.sel.id === e.id;
    const style = e.color != null ? `--c: var(--c${e.color % RT_PALETTE})` : null;
    const ends = `${e.a}|${e.b}`;
    g.append(s("path", { class: `${e.cls}${e.color != null ? " colored" : ""}${selected ? " selected" : ""}`, d: geo.d, style, "data-ends": ends }));
    g.append(s("path", { class: "link-hit", d: geo.d, "data-edge": e.id }, s("title", {}, e.title)));
    if (e.label) {
      labels.append(s("text", { class: "rt-label", x: geo.mid[0], y: geo.mid[1] - 4, "text-anchor": "middle", "data-ends": ends }, e.label));
    }
    for (const [end, from, alt, text] of [[e.a, p0, false, e.ai], [e.b, p1, true, e.bi]]) {
      if (text) {
        const label = ifaceLabel(from, geo.c, NODE_W, NODE_H, text, alt);
        label.dataset.ends = ends;
        labels.append(label);
      }
    }
  }

  // Nodes
  for (const n of S.detail.nodes) {
    const p = P[n.name];
    const info = model.nodes[n.name];
    if (!p || !info) continue;
    const rt = nodeRuntime(S.detail.name, n.name);
    const stClass = { running: "running", booting: "booting", partial: "other" }[nodeState(rt)] || "";
    const selected = RT.sel?.type === "node" && RT.sel.id === n.name;
    g.append(s("g", {
      class: `node${info.off ? " off" : ""}${selected ? " selected" : ""}`,
      transform: `translate(${p[0]},${p[1]})`, "data-id": n.name, "data-f": n.name,
    },
      s("rect", { x: -NODE_W / 2, y: -NODE_H / 2, width: NODE_W, height: NODE_H, rx: 9 }),
      statusGlyph(stClass, -NODE_W / 2 + 13, -7),
      s("text", { class: "name", x: -NODE_W / 2 + 24, y: -2 }, truncate(n.name, info.badge ? 11 : 14)),
      s("text", { class: "kind", x: -NODE_W / 2 + 24, y: 13 }, truncate(info.sub, 17)),
      info.badge ? s("text", { class: "rt-badge", x: NODE_W / 2 - 8, y: -2, "text-anchor": "end" }, info.badge) : null,
      info.warn ? s("text", { class: "rt-warn", x: NODE_W / 2 - 4, y: -NODE_H / 2 - 4, "text-anchor": "end" }, "⚠") : null,
      s("title", {}, `${n.name} — ${info.sub}`)));
  }
  for (const [id, info] of Object.entries(model.nodes)) {
    if (!info.external || !P[id]) continue;
    const [x, y] = P[id];
    g.append(s("g", { class: "pseudo rt-ext", transform: `translate(${x},${y})`, "data-id": id, "data-f": id },
      s("rect", { x: -PSEUDO_W / 2, y: -PSEUDO_H / 2 - 6, width: PSEUDO_W, height: PSEUDO_H + 12, rx: 6 }),
      s("text", { class: "name", "text-anchor": "middle", y: -2 }, truncate(info.label, 18)),
      s("text", { class: "name", "text-anchor": "middle", y: 12 }, truncate(info.sub, 18)),
      s("title", {}, `Peer outside the lab: ${info.label} (${info.sub})`)));
  }
  g.append(labels);
  svg.replaceChildren(g);
  rtRefocus();

  rtRenderLegend(rtLiveData() ? [
    { swatch: "line", cls: "live-up", text: "up" },
    { swatch: "line", cls: "live-down", text: "down / missing" },
    { swatch: "line", cls: "live-partial", text: "some address families down" },
    { swatch: "line", cls: "live-unknown", text: "not known (node not running or not readable)" },
    { swatch: "line", cls: "extra", text: "running, not in startup config" },
  ] : model.legend);
  rtRenderProblems();
  rtRenderCard();
  if (!RT.fitted) { rtFit(P); RT.fitted = true; }
}

// Peers outside the lab go below the router that peers with them
function rtPlaceExternals(model, P) {
  const perOwner = {};
  for (const e of model.edges) {
    for (const [ext, owner] of [[e.b, e.a], [e.a, e.b]]) {
      if (!model.nodes[ext]?.external || RT.extPos[ext] || !P[owner]) continue;
      const i = (perOwner[owner] = (perOwner[owner] ?? -1) + 1);
      RT.extPos[ext] = [P[owner][0] + (i - 0.5) * (PSEUDO_W + 20), P[owner][1] + 120];
    }
  }
  for (const [id, pos] of Object.entries(RT.extPos)) if (model.nodes[id]) P[id] = pos;
}

function rtRenderBar() {
  const d = RT.data;
  const deployed = rtDeployed();
  const modes = $("#routing-modes");
  modes.replaceChildren(...[["intended", "Intended"], ["live", "Live"]].map(([m, label]) => h("button", {
    class: `seg-btn${(m === "live") === rtLiveOn() ? " active" : ""}`,
    disabled: m === "live" && !deployed,
    title: m === "live" ? (deployed ? "Running protocol state, read from the nodes every few seconds" : "Deploy the lab to see its running state")
      : "What the startup configs say",
    onclick: () => { RT.mode = m; rtPrefsSave(); rtRender(); if (m === "live") rtLoadLive(); },
  }, label)));
  const status = $("#routing-live-status");
  const L = rtLiveData();
  status.hidden = !rtLiveOn();
  if (rtLiveOn()) {
    const errs = L ? Object.keys(L.errors || {}).length + Object.values(L.nodes || {}).filter((n) => Object.keys(n.errors || {}).length).length : 0;
    status.textContent = !L ? "reading the nodes…"
      : L.error ? `could not read: ${L.error}`
      : `read ${Math.max(0, Math.round(Date.now() / 1000 - L.updated))}s ago${errs ? ` · ${errs} not readable` : ""}`;
    status.classList.toggle("warn", !!(L && (L.error || errs)));
  }
  const protos = $("#routing-protos");
  protos.replaceChildren(...["ospf", "bgp", "evpn"].map((p) => h("button", {
    class: `seg-btn${RT.proto === p ? " active" : ""}`,
    disabled: !d?.protocols?.includes(p),
    title: d?.protocols?.includes(p) ? "" : `No ${RT_PROTO_LABEL[p]} in the startup configs`,
    onclick: () => { RT.proto = p; RT.sel = null; rtRender(); },
  }, RT_PROTO_LABEL[p])));

  const filters = $("#routing-filters");
  const parts = [];
  if (RT.proto === "evpn" && d?.evpn) {
    parts.push(h("select", {
      class: "small", "aria-label": "EVPN layer",
      onchange: (ev) => { RT.layer = ev.target.value; rtPrefsSave(); rtRender(); },
    }, [["both", "Control + data plane"], ["control", "Control plane (EVPN sessions)"], ["data", "Data plane (VXLAN tunnels)"]]
      .map(([v, t]) => h("option", { value: v, selected: RT.layer === v }, t))));
    parts.push(h("select", {
      class: "small", "aria-label": "VNI",
      onchange: (ev) => { RT.vni = ev.target.value; rtRender(); },
    }, h("option", { value: "all" }, "All VNIs"),
      d.evpn.vnis.map((v) => h("option", { value: v.vni, selected: String(RT.vni) === String(v.vni) },
        `VNI ${v.vni} · ${v.type === "l3" ? `VRF ${v.vrfs.join(", ")}` : `VLAN ${v.vlans.join(", ") || "?"}`}`))));
  }
  if (RT.proto) {
    parts.push(h("label", { class: "opt", title: "Show the physical links faintly behind the protocol view" },
      h("input", { type: "checkbox", checked: RT.underlay, onchange: (ev) => { RT.underlay = ev.target.checked; rtPrefsSave(); rtRender(); } }),
      " Cabling"));
  }
  filters.replaceChildren(...parts);

  const problems = rtProblems();
  const btn = $("#routing-problems-btn");
  const real = problems.filter((p) => p.severity !== "info").length;
  btn.hidden = !problems.length;
  btn.textContent = real ? `⚠ ${real} problem${real === 1 ? "" : "s"}` : `${problems.length} note${problems.length === 1 ? "" : "s"}`;
  btn.classList.toggle("warn", real > 0);
}

function rtProblems() {
  if (!RT.data?.problems || !RT.proto) return [];
  const out = RT.data.problems.filter((p) => p.protocol === RT.proto || p.protocol === "ip");
  const L = rtLiveData();
  if (!L) return out;
  const live = [];
  const label = (e) => `${e.a} ↔ ${e.b}`;
  for (const e of Object.values(RT.edges)) {
    const st = rtLiveEntry(e.id);
    if (st && (st.state === "down" || st.state === "partial")) {
      live.push({ node: e.a, severity: "live", message: `${label(e)} is ${RT_LIVE_TEXT[st.state]}${st.detail ? `: ${st.detail}` : ""}` });
    }
  }
  for (const e of Object.values(RT.edges)) {
    const drift = rtLiveEntry(e.id)?.drift;
    if (drift) live.push({ node: e.b, severity: "live", message: drift });
  }
  for (const x of rtExtras()) {
    live.push({ node: x.node, severity: "live", message: `${x.node} runs a ${x.kind.toUpperCase()} neighbor ${x.ip}${x.peer ? ` (${x.peer})` : ""} that is not in its startup config` });
  }
  for (const [node, n] of Object.entries(L.nodes || {})) {
    for (const [topic, err] of Object.entries(n.errors || {})) {
      live.push({ node, severity: "live", message: `${node}: could not read ${topic || "its state"}: ${err}` });
    }
  }
  return [...live, ...out];
}

function rtRenderProblems() {
  const list = $("#routing-problems");
  const problems = rtProblems();
  list.hidden = !RT.showProblems || !problems.length;
  if (list.hidden) return;
  list.replaceChildren(...problems.map((p) => h("li", {
    class: p.severity === "info" ? "info" : p.severity === "live" ? "live" : "warn",
    onclick: () => { RT.sel = { type: "node", id: p.node }; rtRender(); },
  }, p.message)));
}

function rtRenderLegend(items) {
  const el = $("#routing-legend");
  el.hidden = !items.length;
  el.replaceChildren(...items.map((it) => h("div", { class: "item", title: it.note || "" },
    it.swatch === "line"
      ? h("span", { class: `sw ${it.cls || ""}${it.color != null ? " colored" : ""}`,
                    style: it.color != null ? `--c: var(--c${it.color % RT_PALETTE})` : null })
      : h("span", { class: "sw-text mono" }, it.text),
    it.swatch === "line" ? it.text : it.note)));
}

// --- detail card -------------------------------------------------------------------

function rtDl(rows) {
  rows = rows.filter((r) => r && r[1] !== undefined && r[1] !== null && r[1] !== "");
  return h("dl", {}, rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
}

function rtTable(head, rows) {
  if (!rows.length) return null;
  return h("table", { class: "rt-table" },
    h("thead", {}, h("tr", {}, head.map((c) => h("th", {}, c)))),
    h("tbody", {}, rows.map((r) => h("tr", {}, r.map((c) => h("td", {}, c ?? ""))))));
}

function rtCardProblems(list) {
  if (!list?.length) return null;
  return h("ul", { class: "rt-card-problems" }, list.map((m) => h("li", {}, m)));
}

function rtRenderCard() {
  const card = $("#routing-card");
  const sel = RT.sel;
  const body = sel && (sel.type === "node" ? rtNodeCard(sel.id) : rtEdgeCard(sel.id));
  if (!body) { card.hidden = true; return; }
  if (rtLiveData()) body.parts.push(...(sel.type === "node" ? rtLiveNodeParts(sel.id) : rtLiveEdgeParts(sel.id)));
  card.replaceChildren(
    h("h3", {}, body.title, h("button", { class: "close", title: "Close", onclick: () => { RT.sel = null; rtRender(); } }, "×")),
    ...body.parts.filter(Boolean));
  card.hidden = false;
}

function rtNodeCard(id) {
  const d = RT.data;
  const nodeProblems = (d.problems || [])
    .filter((p) => p.node === id && (p.protocol === RT.proto || p.protocol === "ip")).map((p) => p.message);
  if (id.startsWith("ext:")) {
    const ext = d.bgp?.external?.[id] || rtExtras().find((x) => `ext:${x.ip}` === id);
    return ext && { title: ext.ip, parts: [rtDl([["Peer", "outside the lab"], ["AS", ext.asn]])] };
  }
  if (RT.proto === "ospf") {
    const n = d.ospf.nodes[id];
    if (!n) return { title: id, parts: [h("p", { class: "muted small" }, "No OSPF on this node.")] };
    return {
      title: id,
      parts: [
        rtDl([["Router ID", `${n.router_id}${n.router_id_configured ? "" : " (derived)"}`],
              ["Areas", n.areas.join(", ")], ["ABR", n.abr ? "yes" : ""], ["Process", n.processes.join(", ")]]),
        rtTable(["Interface", "Address", "Area", "Cost", ""], n.interfaces.map((i) => [
          i.name, i.ip, i.area, i.cost ?? "", [i.passive ? "passive" : "", i.shutdown ? "shut" : "", i.network === "point-to-point" ? "p2p" : ""].filter(Boolean).join(" ")])),
        rtCardProblems(nodeProblems),
      ],
    };
  }
  if (RT.proto === "bgp") {
    const n = d.bgp.nodes[id];
    if (!n) return { title: id, parts: [h("p", { class: "muted small" }, "No BGP on this node.")] };
    const sessions = d.bgp.sessions.filter((x) => x.a.node === id || x.b.node === id);
    return {
      title: id,
      parts: [
        rtDl([["AS", n.asn], ["Router ID", `${n.router_id}${n.router_id_configured ? "" : " (derived)"}`],
              ["Networks", n.networks.join(", ")]]),
        rtTable(["Peer", "Address", "Type", "AFI", ...(rtLiveData() ? ["Live"] : [])], sessions.map((x) => {
          const [, peer] = x.a.node === id ? [x.a, x.b] : [x.b, x.a];
          const live = rtLiveEntry(x.id);
          return [peer.node.replace(/^ext:/, ""), peer.ip, x.type + (x.configured === "both" ? "" : " ⚠"),
                  x.families.join(", "), ...(rtLiveData() ? [live ? RT_LIVE_TEXT[live.state] || live.state : ""] : [])];
        })),
        rtCardProblems(nodeProblems),
      ],
    };
  }
  const vt = d.evpn.vteps[id];
  if (!vt) {
    const n = d.evpn.sessions.filter((sid) => {
      const x = d.bgp?.sessions.find((y) => y.id === sid);
      return x && (x.a.node === id || x.b.node === id);
    }).length;
    return { title: id, parts: [rtDl([["Role", n ? "EVPN route server (no VTEP)" : "no EVPN"], ["EVPN sessions", n || ""]]), rtCardProblems(nodeProblems)] };
  }
  const rt = (x) => [...new Set([...x.import, ...x.export])].join(" ");
  return {
    title: id,
    parts: [
      rtDl([["VTEP", vt.ip], ["Source", `${vt.interface} → ${vt.source_interface}`],
            ["UDP port", vt.udp_port !== 4789 ? vt.udp_port : ""], ["Flood list", vt.flood_list.join(", ")]]),
      rtTable(["L2 VNI", "VLAN", "RD", "RT"], vt.l2_vnis.map((x) => [x.vni, x.vlan || "?", x.rd, rt(x)])),
      rtTable(["L3 VNI", "VRF", "RD", "RT"], vt.l3_vnis.map((x) => [x.vni, x.vrf, x.rd, rt(x)])),
      rtCardProblems(nodeProblems),
    ],
  };
}

function rtLiveEdgeParts(id) {
  const st = rtLiveEntry(id);
  const e = RT.edges[id];
  if (!st || !e) return [];
  const rows = st.families
    ? Object.entries(st.families).flatMap(([fam, f]) => [[`${fam} ${e.a}`, rtSideText(f.a)], [`${fam} ${e.b}`, rtSideText(f.b)]])
    : [[e.a, rtSideText(st.a)], [e.b, rtSideText(st.b)]];
  return [
    h("h4", { class: `rt-live-head live-${st.state}` }, `Live: ${RT_LIVE_TEXT[st.state] || st.state}`),
    st.detail ? h("p", { class: "small muted" }, st.detail) : null,
    rtDl(rows),
    st.drift ? h("p", { class: "small rt-drift" }, st.drift) : null,
  ];
}

function rtLiveNodeParts(id) {
  const L = rtLiveData();
  const n = L?.nodes?.[id];
  const extras = rtExtras().filter((x) => x.node === id);
  const parts = [];
  if (n) {
    parts.push(h("h4", { class: "rt-live-head" }, "Live"));
    parts.push(rtDl([["Read", n.collected.join(", ") || "nothing"],
                     ...Object.entries(n.errors || {}).map(([k, v]) => [`${k || "all"} failed`, v])]));
  }
  if (extras.length) {
    parts.push(h("p", { class: "small muted" }, "Running but not in the startup config:"));
    parts.push(rtTable(["Neighbor", "Peer", "State"], extras.map((x) => [x.ip, x.peer || (x.asn ? `AS ${x.asn}` : ""), x.detail || x.state])));
  }
  return parts;
}

function rtEdgeCard(id) {
  const d = RT.data;
  if (id.startsWith("extra-")) {
    const x = rtExtras().find((y) => y.id === id);
    if (!x) return null;
    return {
      title: `${x.kind.toUpperCase()} neighbor not in the config`,
      parts: [
        rtDl([["Node", x.node], ["Neighbor", x.ip], ["Peer", x.peer || "outside the lab"], ["AS", x.asn],
              ["Router ID", x.router_id], ["Interface", x.iface], ["State", x.detail || x.state],
              ["Up", rtUptime(x.uptime)], ["Prefixes", x.pfx_rcvd]]),
        h("p", { class: "small muted" }, "Configured on the running node (for example on the CLI) but not in its startup-config. Save configs to keep it."),
      ],
    };
  }
  if (id.startsWith("ospf")) {
    const a = d.ospf?.adjacencies.find((x) => x.id === id);
    if (!a) return null;
    return {
      title: "OSPF adjacency",
      parts: [
        rtTable(["", "Interface", "Address", "Cost"], [a.a, a.b].map((e) => [e.node, e.iface, e.ip, e.cost ?? ""])),
        rtDl([["Area", a.area ?? "mismatch"], ["Subnet", a.subnet], ["VRF", a.vrf], ["Cable", a.link ? "direct" : ""]]),
        rtCardProblems(a.problems),
      ],
    };
  }
  if (id.startsWith("bgp")) {
    const x = d.bgp?.sessions.find((y) => y.id === id);
    if (!x) return null;
    return {
      title: `${x.type === "ebgp" ? "eBGP" : "iBGP"} session`,
      parts: [
        rtTable(["", "Address", "AS", "Interface"], [x.a, x.b].map((e) => [e.node.replace(/^ext:/, ""), e.ip, e.asn, e.iface])),
        rtDl([["Families", x.families.join(", ") || "none"], ["Hops", x.multihop ? "multihop (loopbacks)" : "directly connected"],
              ["Peer groups", x.peer_groups.join(", ")], ["Description", x.description],
              ["Configured", x.configured === "both" ? "" : "on one side only"], ["Shutdown", x.shutdown ? "yes" : ""]]),
        rtCardProblems(x.problems),
      ],
    };
  }
  const t = d.evpn?.tunnels.find((y) => y.id === id);
  if (!t) return null;
  const info = Object.fromEntries(d.evpn.vnis.map((v) => [v.vni, v]));
  return {
    title: "VXLAN tunnel",
    parts: [
      rtDl([[t.a.node, t.a.ip], [t.b.node, t.b.ip]]),
      rtTable(["VNI", "Type", "VLAN / VRF"], t.vnis.map((v) => [v, info[v]?.type.toUpperCase(),
        info[v]?.type === "l3" ? info[v].vrfs.join(", ") : info[v]?.vlans.join(", ")])),
    ],
  };
}

// --- interaction ---------------------------------------------------------------------

function rtFit(P) {
  const svg = $("#routing");
  const pts = Object.values(P || S.positions);
  if (!pts.length) return;
  const w = svg.clientWidth || 800, hgt = svg.clientHeight || 500;
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const minX = Math.min(...xs) - NODE_W, maxX = Math.max(...xs) + NODE_W;
  const minY = Math.min(...ys) - NODE_H * 2, maxY = Math.max(...ys) + NODE_H * 1.5;
  const k = Math.min(1.4, Math.min(w / (maxX - minX), hgt / (maxY - minY)));
  RT.view = { k, x: w / 2 - ((minX + maxX) / 2) * k, y: hgt / 2 - ((minY + maxY) / 2) * k };
  $("#rt-viewport")?.setAttribute("transform", `translate(${RT.view.x},${RT.view.y}) scale(${RT.view.k})`);
}

let rtLastClick = { id: null, t: 0 };

let rtRefocus = () => {};

function rtSetup() {
  rtPrefsLoad();
  const svg = $("#routing");
  rtRefocus = setupFocus(svg, () => (RT.sel?.type === "node" ? RT.sel.id : null));
  let drag = null;
  const applyView = () => $("#rt-viewport")?.setAttribute("transform", `translate(${RT.view.x},${RT.view.y}) scale(${RT.view.k})`);

  svg.addEventListener("pointerdown", (ev) => {
    svg.setPointerCapture(ev.pointerId);
    const nodeEl = ev.target.closest(".node, .rt-ext");
    if (nodeEl) {
      const id = nodeEl.dataset.id;
      const ext = id.startsWith("ext:");
      const start = ext ? RT.extPos[id] : S.positions[id];
      drag = { type: "node", id, ext, sx: ev.clientX, sy: ev.clientY, start: [...start], moved: false };
    } else {
      const edge = ev.target.closest(".link-hit")?.dataset.edge || null;
      drag = { type: "pan", edge, sx: ev.clientX, sy: ev.clientY, start: { ...RT.view }, moved: false };
      svg.classList.add("panning");
    }
  });
  svg.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    const dx = ev.clientX - drag.sx, dy = ev.clientY - drag.sy;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    if (!drag.moved) return;
    if (drag.type === "node") {
      const pos = [drag.start[0] + dx / RT.view.k, drag.start[1] + dy / RT.view.k];
      if (drag.ext) RT.extPos[drag.id] = pos;
      else S.positions[drag.id] = pos;
      rtRender();
    } else {
      RT.view.x = drag.start.x + dx;
      RT.view.y = drag.start.y + dy;
      applyView();
    }
  });
  svg.addEventListener("pointerup", () => {
    svg.classList.remove("panning");
    if (!drag) return;
    if (drag.type === "node") {
      if (drag.moved) { if (!drag.ext) savePositions(); }
      else rtNodeClicked(drag.id);
    } else if (!drag.moved) {
      RT.sel = drag.edge ? { type: "edge", id: drag.edge } : null;
      rtRender();
    }
    drag = null;
  });
  svg.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const rect = svg.getBoundingClientRect();
    const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
    const k = Math.max(0.2, Math.min(3, RT.view.k * Math.exp(-ev.deltaY * 0.0015)));
    RT.view.x = mx - ((mx - RT.view.x) / RT.view.k) * k;
    RT.view.y = my - ((my - RT.view.y) / RT.view.k) * k;
    RT.view.k = k;
    applyView();
  }, { passive: false });

  $("#routing-fit").addEventListener("click", () => rtFit());
  $("#routing-reload").addEventListener("click", () => rtLoad());
  $("#routing-problems-btn").addEventListener("click", () => {
    RT.showProblems = !RT.showProblems;
    rtRenderProblems();
  });
}

// Double click opens the node's CLI, as in the Diagram tab
function rtNodeClicked(id) {
  const now = Date.now();
  const isDouble = rtLastClick.id === id && now - rtLastClick.t < 400;
  rtLastClick = { id, t: isDouble ? 0 : now };
  RT.sel = { type: "node", id };
  rtRender();
  if (!isDouble || id.startsWith("ext:")) return;
  const node = S.detail.nodes.find((n) => n.name === id);
  const rt = nodeRuntime(S.detail.name, id);
  if (rt?.state === "running" && node?.modes.length && canOperate()) openTerminal(S.detail.name, id, node.modes[0]);
}

// --- hooks called by app.js ---------------------------------------------------------

window.Routing = {
  // The Routing tab was opened
  show() { rtRender(); if (rtLiveOn()) rtLoadLive(); },
  // Another topology was selected
  reset() { RT.topo = null; RT.data = null; RT.live = null; RT.sel = null; RT.extPos = {}; RT.vni = "all"; },
  // The topology file changed (saved YAML, finished job): re-read the configs
  changed() { if (rtVisible()) rtLoad(); else RT.topo = null; },
  // Container states changed: refresh the status dots
  runtime() {
    if (!rtVisible() || !RT.data) return;
    if (rtLiveOn()) rtLoadLive();
    rtRender();
  },
  resize() { if (rtVisible() && RT.data) rtRender(); },
  // OSPF and BGP sessions up / total for the lab header, while the last
  // live read is recent; null otherwise
  liveSummary() {
    const L = RT.live;
    if (!L?.updated || L.id !== rtTopoId() || !rtDeployed()) return null;
    const age = Math.max(0, Math.round(Date.now() / 1000 - L.updated));
    if (age > 60) return null;
    const entries = [...Object.values(L.ospf || {}), ...Object.values(L.bgp || {})];
    return { up: entries.filter((e) => e.state === "up").length, total: entries.length, age };
  },
  // Open the Routing tab in Live mode
  showLive() {
    RT.mode = "live";
    rtPrefsSave();
    showView("routing");
  },
};

rtSetup();
