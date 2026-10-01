"use strict";

// ---------------------------------------------------------------------------
// State & helpers
// ---------------------------------------------------------------------------

const S = {
  state: null,          // /api/state
  selected: null,       // {type: "topo", id} | {type: "lab", lab}
  detail: null,         // /api/topologies/<id>
  selectedNode: null,
  positions: {},        // node name -> [x, y] for the open topology
  view: { x: 0, y: 0, k: 1 },
  jobPolling: null,
};

const $ = (sel) => document.querySelector(sel);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const SVGNS = "http://www.w3.org/2000/svg";
function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, v);
  for (const c of children.flat()) {
    if (c == null) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: opts.body ? { "Content-Type": "application/json" } : undefined,
  });
  if (!res.ok) throw new Error((await res.text()) || res.statusText);
  return res.json();
}

let toastTimer;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 6000);
}

const MODE_LABEL = { cli: "CLI", shell: "Shell", ssh: "SSH", logs: "Logs" };

// ---------------------------------------------------------------------------
// Runtime helpers
// ---------------------------------------------------------------------------

function allContainers() {
  return (S.state?.runtime || []).flatMap((r) => r.containers);
}

function labContainers(lab) {
  return allContainers().filter((c) => c.lab === lab);
}

function nodeRuntime(lab, node) {
  return labContainers(lab).find((c) => c.node === node);
}

// Host the last deploy placed a node on (from the lab's placement record)
function placedHost(node) {
  return S.detail?.placement?.nodes?.[node] || "";
}

function nodeHost(lab, node) {
  return nodeRuntime(lab, node)?.host || placedHost(node);
}

// VNI of each cross-host link from the placement record, keyed by "node:iface|node:iface"
function crossLinkVnis() {
  const vnis = {};
  for (const c of S.detail?.placement?.cross_host_links || []) {
    vnis[`${c.a}|${c.b}`] = vnis[`${c.b}|${c.a}`] = c.vni;
  }
  return vnis;
}

function labStatus(lab, total) {
  const cs = labContainers(lab);
  const running = cs.filter((c) => c.state === "running").length;
  const booting = cs.filter((c) => c.state === "running" && c.ready === false).length;
  let state = "stopped";
  if (cs.length && running === (total ?? cs.length) && running === cs.length) state = booting ? "booting" : "running";
  else if (cs.length) state = "partial";
  return { running, booting, ready: running - booting, total: total ?? cs.length, state, deployed: cs.length };
}

// Node state for dots and labels: running (ready), booting (container up,
// CLI/SSH not yet), partial (container not running), or "" (not deployed)
function nodeState(rt) {
  if (!rt) return "";
  if (rt.state !== "running") return "partial";
  return rt.ready === false ? "booting" : "running";
}

function nodeStateText(rt) {
  if (!rt) return "not deployed";
  if (rt.state === "running" && rt.ready === false) return `booting · ${rt.ready_detail || ""}`;
  return rt.status || rt.state;
}

// The running job of a topology, if any (several labs can run jobs at once)
function runningJob(topoId) {
  return (S.state?.jobs || []).find((j) => j.status === "running" && j.topology === topoId);
}

function fmtDuration(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60), r = sec % 60;
  return m < 60 ? `${m}m ${String(r).padStart(2, "0")}s` : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

const JOB_ICON = { running: "⟳", ok: "✓", error: "✗", interrupted: "✗" };

function jobLabel(j) {
  const when = new Date(j.started * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return `${JOB_ICON[j.status] || "•"} ${j.action} ${j.lab || j.topology} · ${when}`;
}

function renderJobPicker() {
  const sel = $("#job-select");
  const jobs = S.state?.jobs || [];
  sel.hidden = !jobs.length;
  sel.replaceChildren(...jobs.map((j) => h("option", { value: j.id }, jobLabel(j))));
  if (S.viewJob) sel.value = S.viewJob;
  renderJobMeta();
}

function renderJobMeta() {
  const j = (S.state?.jobs || []).find((x) => x.id === S.viewJob);
  const meta = $("#job-meta");
  if (!j) { meta.textContent = ""; return; }
  const end = j.finished || Date.now() / 1000;
  const hosts = Object.entries(j.host_times || {}).map(([hst, t]) => `${hst} ${fmtDuration(t)}`).join(", ");
  meta.textContent = `${j.status === "running" ? "running" : j.status} · ${fmtDuration(end - j.started)}` +
    (hosts ? ` · ${hosts}` : "");
}

// ---------------------------------------------------------------------------
// Data refresh
// ---------------------------------------------------------------------------

async function refreshState() {
  const before = new Map((S.state?.jobs || []).map((j) => [j.id, j.status]));
  try {
    S.state = await api("/api/state");
  } catch (e) {
    toast(`Could not load state: ${e.message}`);
    return;
  }
  // Jobs that finished in the background (the viewed one reports itself)
  for (const j of S.state.jobs) {
    if (before.get(j.id) === "running" && j.status !== "running" && j.id !== S.viewJob) {
      if (j.status !== "ok") toast(`${j.action} ${j.lab} failed — see Activity`);
      if (S.selected?.type === "topo" && S.selected.id === j.topology) reloadDetail();
    }
  }
  for (const r of S.state.runtime) {
    if (!r.ok && r.error) console.warn(`host ${r.host}: ${r.error}`);
  }
  renderSidebar();
  renderLabHead();
  renderRuntimeOverlays();
  if (!S.viewJob && S.state.jobs.length) {
    const running = S.state.jobs.find((j) => j.status === "running");
    trackJob((running || S.state.jobs[0]).id, false);
  }
  renderJobPicker();
}

async function refreshHosts() {
  const box = $("#hosts");
  try {
    const hosts = await api("/api/hosts");
    box.replaceChildren(...hosts.map((hst) => {
      const meta = hst.ok
        ? `clab ${hst.version || "missing"} · ${hst.cpus ?? "?"} CPU · ${fmtMem(hst.mem_available_mb)} free`
        : "unreachable";
      return h("span", { class: "host-chip", title: hst.error || hst.host },
        h("span", { class: `dot ${hst.ok && hst.version ? "running" : "error"}` }),
        h("span", {}, hst.name),
        h("span", { class: "meta" }, meta));
    }));
  } catch (e) {
    box.replaceChildren(h("span", { class: "host-chip" }, `hosts: ${e.message}`));
  }
}

function fmtMem(mb) {
  if (mb == null) return "?";
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`;
}

// ---------------------------------------------------------------------------
// Sidebar
// ---------------------------------------------------------------------------

function renderSidebar() {
  const topos = S.state.topologies;
  const topoNames = new Set(topos.map((t) => t.name));

  $("#topo-list").replaceChildren(...topos.map((t) => {
    const st = labStatus(t.name, t.nodes);
    const active = S.selected?.type === "topo" && S.selected.id === t.id;
    const busy = !!runningJob(t.id);
    return h("li", {},
      h("button", {
        class: `lab-item${active ? " active" : ""}`,
        onclick: () => selectTopology(t.id),
        title: t.error || t.path,
      },
        h("span", { class: `dot ${busy ? "busy" : t.error ? "error" : st.state}` }),
        h("span", { class: "txt" },
          h("span", { class: "title" }, t.name),
          h("span", { class: "sub" }, t.id)),
        st.deployed ? h("span", { class: "count" }, `${st.running}/${t.nodes}`) : null));
  }));

  const others = [...new Set(allContainers().map((c) => c.lab))].filter((l) => !topoNames.has(l));
  $("#others-section").hidden = !others.length;
  $("#other-list").replaceChildren(...others.map((lab) => {
    const st = labStatus(lab);
    const active = S.selected?.type === "lab" && S.selected.lab === lab;
    const hostsUsed = [...new Set(labContainers(lab).map((c) => c.host))].join(", ");
    return h("li", {},
      h("button", { class: `lab-item${active ? " active" : ""}`, onclick: () => selectOtherLab(lab) },
        h("span", { class: `dot ${st.state}` }),
        h("span", { class: "txt" },
          h("span", { class: "title" }, lab),
          h("span", { class: "sub" }, hostsUsed)),
        h("span", { class: "count" }, `${st.running}/${st.total}`)));
  }));
}

// ---------------------------------------------------------------------------
// Lab selection & header
// ---------------------------------------------------------------------------

async function selectTopology(id) {
  S.selected = { type: "topo", id };
  S.selectedNode = null;
  try {
    S.detail = await api(`/api/topologies/${encodeURIComponent(id).replace(/%2F/g, "/")}`);
  } catch (e) {
    toast(e.message);
    return;
  }
  S.positions = loadPositions(id);
  $("#empty").hidden = true;
  $("#lab").hidden = false;
  $("#yaml").textContent = S.detail.yaml;
  setTabsAvailable(["diagram", "nodes", "yaml"]);
  renderSidebar();
  renderLabHead();
  renderDiagram(true);
  renderNodesTable();
  renderNodeCard();
}

// Re-fetch the selected topology (e.g. after a job changed its placement record)
async function reloadDetail() {
  if (S.selected?.type !== "topo") return;
  const id = S.selected.id;
  let detail;
  try {
    detail = await api(`/api/topologies/${encodeURIComponent(id).replace(/%2F/g, "/")}`);
  } catch (e) {
    return;
  }
  if (S.selected?.type !== "topo" || S.selected.id !== id) return;
  S.detail = detail;
  renderDiagram(false);
  renderNodesTable();
  renderNodeCard();
}

function selectOtherLab(lab) {
  S.selected = { type: "lab", lab };
  S.detail = null;
  S.selectedNode = null;
  $("#empty").hidden = true;
  $("#lab").hidden = false;
  setTabsAvailable(["nodes"]);
  renderSidebar();
  renderLabHead();
  renderNodesTable();
}

function currentLabName() {
  if (!S.selected) return null;
  return S.selected.type === "topo" ? S.detail?.name : S.selected.lab;
}

function renderLabHead() {
  if (!S.selected) return;
  const lab = currentLabName();
  const isTopo = S.selected.type === "topo";
  const total = isTopo ? S.detail?.nodes?.length : undefined;
  const st = labStatus(lab, total);

  $("#lab-name").textContent = lab || "";
  const badge = $("#lab-state");
  badge.className = `badge ${st.state}`;
  badge.textContent = st.state === "stopped" ? "not deployed"
    : st.state === "booting" ? `booting · ${st.ready}/${st.total} ready`
    : `${st.state} · ${st.running}/${st.total}`;

  const topoFile = labContainers(lab)[0]?.topo_file;
  $("#lab-path").textContent = isTopo ? S.detail?.path || "" : topoFile ? `deployed from ${topoFile}` : "";

  const actions = $("#lab-actions");
  actions.hidden = !isTopo;
  const busy = isTopo && !!runningJob(S.selected.id);
  for (const btn of actions.querySelectorAll("button")) {
    const a = btn.dataset.action;
    const enabled = a === "deploy" ? st.deployed === 0 : st.deployed > 0;
    btn.disabled = busy || !enabled || !!S.detail?.error;
  }
}

function setTabsAvailable(views) {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.hidden = !views.includes(tab.dataset.view);
  }
  const current = document.querySelector(".tab.active")?.dataset.view;
  showView(views.includes(current) ? current : views[0]);
}

function showView(view) {
  for (const tab of document.querySelectorAll(".tab")) tab.classList.toggle("active", tab.dataset.view === view);
  for (const v of ["diagram", "nodes", "yaml"]) $(`#view-${v}`).hidden = v !== view;
  if (view === "diagram") renderDiagram(false);
}

function renderRuntimeOverlays() {
  if (!S.selected) return;
  if (S.selected.type === "topo" && S.detail) {
    renderDiagram(false);
    renderNodeCard();
  }
  renderNodesTable();
}

// ---------------------------------------------------------------------------
// Nodes table
// ---------------------------------------------------------------------------

// Terminal buttons for a node; Logs also works for a stopped container
function openButtons(lab, node, modes, running, exists) {
  const buttons = (modes || []).map((m) =>
    h("button", {
      class: "btn small",
      disabled: !running,
      title: running ? `Open ${MODE_LABEL[m]} on ${node}` : "Node is not running",
      onclick: () => openTerminal(lab, node, m),
    }, MODE_LABEL[m]));
  if (modes?.length) {
    buttons.push(h("button", {
      class: "btn small ghost",
      disabled: !exists,
      title: exists ? `Follow the container log of ${node}` : "Node is not deployed",
      onclick: () => openTerminal(lab, node, "logs"),
    }, MODE_LABEL.logs));
  }
  return h("span", { class: "open" }, buttons);
}

function renderNodesTable() {
  const lab = currentLabName();
  if (!lab) return;
  const multi = S.state?.multi_host;
  $("#host-col").hidden = !multi;

  let rows;
  if (S.selected.type === "topo" && S.detail?.nodes) {
    rows = S.detail.nodes.map((n) => ({ ...n, rt: nodeRuntime(lab, n.name) }));
  } else {
    rows = labContainers(lab).map((c) => ({ name: c.node, kind: c.kind, image: c.image, modes: c.modes, rt: c }));
  }
  $("#node-rows").replaceChildren(...rows.map((n) => {
    const running = n.rt?.state === "running";
    return h("tr", {},
      h("td", {}, h("strong", {}, n.name)),
      h("td", { class: "mono small" }, n.kind + (n.type ? ` (${n.type})` : "")),
      h("td", { class: "img" }, n.rt?.image || n.image || ""),
      multi ? h("td", { class: "mono small" }, n.rt?.host || placedHost(n.name) || (n.host_pin ? `${n.host_pin} (pinned)` : "")) : null,
      h("td", { title: n.rt?.ready_detail || "" }, h("span", { class: `dot ${nodeState(n.rt)}` }), " ",
        nodeStateText(n.rt)),
      h("td", { class: "mono small" }, n.rt?.ipv4 || ""),
      h("td", {}, openButtons(lab, n.name, n.modes, running, !!n.rt)));
  }));
}

// ---------------------------------------------------------------------------
// Diagram
// ---------------------------------------------------------------------------

const NODE_W = 136, NODE_H = 48, PSEUDO_W = 112, PSEUDO_H = 30;

function loadPositions(id) {
  try { return JSON.parse(localStorage.getItem(`clab-pos:${id}`)) || {}; } catch { return {}; }
}
function savePositions() {
  if (S.selected?.type !== "topo") return;
  try { localStorage.setItem(`clab-pos:${S.selected.id}`, JSON.stringify(S.positions)); } catch { /* private mode */ }
}

function graphModel() {
  const nodes = S.detail.nodes.map((n) => ({ id: n.name, node: n, pseudo: false }));
  const links = [];
  let pseudoCount = 0;
  for (const l of S.detail.links) {
    const ends = [l.a, l.b].map((e) => {
      if (e.node) return { id: e.node, iface: e.iface };
      const id = `~${e.special}:${pseudoCount++}`;
      nodes.push({ id, pseudo: true, label: e.iface ? `${e.special}:${e.iface}` : e.special });
      return { id, iface: "" };
    });
    links.push({ id: l.id, type: l.type, a: ends[0], b: ends[1], special: !l.a.node || !l.b.node });
  }
  return { nodes, links };
}

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

const naturalCmp = (a, b) => a.localeCompare(b, undefined, { numeric: true });

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

let diagramModel = null;

function renderDiagram(fit) {
  const svg = $("#diagram");
  const err = $("#diagram-error");
  if (!S.detail || $("#view-diagram").hidden) return;
  if (S.detail.error) {
    svg.replaceChildren();
    err.hidden = false;
    err.textContent = `This topology could not be loaded: ${S.detail.error}`;
    return;
  }
  err.hidden = true;

  diagramModel = graphModel();
  ensurePositions(diagramModel);
  const lab = S.detail.name;
  const multi = S.state?.multi_host;
  const P = S.positions;
  const vnis = crossLinkVnis();

  const g = s("g", { id: "viewport", transform: `translate(${S.view.x},${S.view.y}) scale(${S.view.k})` });

  // Interface labels go in their own layer above the nodes
  const labels = s("g", { class: "labels" });

  // Links (spread parallel links between the same pair)
  const pairCount = {}, pairIndex = {};
  for (const l of diagramModel.links) {
    const key = [l.a.id, l.b.id].sort().join("|");
    pairCount[key] = (pairCount[key] || 0) + 1;
  }
  for (const l of diagramModel.links) {
    const key = [l.a.id, l.b.id].sort().join("|");
    const idx = (pairIndex[key] = (pairIndex[key] ?? -1) + 1);
    const p0 = P[l.a.id], p1 = P[l.b.id];
    const dx = p1[0] - p0[0], dy = p1[1] - p0[1];
    const len = Math.hypot(dx, dy) || 1;
    const off = (idx - (pairCount[key] - 1) / 2) * 26 * (l.a.id < l.b.id ? 1 : -1);
    const c = [(p0[0] + p1[0]) / 2 - (dy / len) * off, (p0[1] + p1[1]) / 2 + (dx / len) * off];
    const ha = nodeHost(lab, l.a.id), hb = nodeHost(lab, l.b.id);
    const cross = multi && ha && hb && ha !== hb;
    const vni = vnis[`${l.a.id}:${l.a.iface}|${l.b.id}:${l.b.iface}`];
    const vxlan = cross ? `  (VXLAN ${ha} ↔ ${hb}${vni !== undefined ? `, VNI ${vni}` : ""})` : "";
    g.append(s("path", {
      class: `link${l.special ? " special" : ""}${cross ? " cross" : ""}`,
      d: `M${p0[0]},${p0[1]} Q${c[0]},${c[1]} ${p1[0]},${p1[1]}`,
    }, s("title", {}, `${l.a.id}:${l.a.iface} ↔ ${l.b.id}:${l.b.iface}${vxlan}`)));
    for (const [end, from, alt] of [[l.a, p0, false], [l.b, p1, true]]) {
      if (!end.iface) continue;
      const pseudo = end.id.startsWith("~");
      labels.append(ifaceLabel(from, c, pseudo ? PSEUDO_W : NODE_W, pseudo ? PSEUDO_H : NODE_H, end.iface, alt));
    }
  }

  // Nodes
  for (const nd of diagramModel.nodes) {
    const [x, y] = P[nd.id];
    if (nd.pseudo) {
      g.append(s("g", { class: "pseudo", transform: `translate(${x},${y})` },
        s("rect", { x: -PSEUDO_W / 2, y: -PSEUDO_H / 2, width: PSEUDO_W, height: PSEUDO_H, rx: 6 }),
        s("text", { class: "name", "text-anchor": "middle", y: 4 }, truncate(nd.label, 18))));
      continue;
    }
    const rt = nodeRuntime(lab, nd.id);
    const stClass = { running: "running", booting: "booting", partial: "other" }[nodeState(rt)] || "";
    const el = s("g", {
      class: `node${S.selectedNode === nd.id ? " selected" : ""}`,
      transform: `translate(${x},${y})`, "data-id": nd.id,
    },
      s("rect", { x: -NODE_W / 2, y: -NODE_H / 2, width: NODE_W, height: NODE_H, rx: 9 }),
      s("circle", { class: `status ${stClass}`, cx: -NODE_W / 2 + 13, cy: -7, r: 4.5 }),
      s("text", { class: "name", x: -NODE_W / 2 + 24, y: -2 }, truncate(nd.id, 14)),
      s("text", { class: "kind", x: -NODE_W / 2 + 24, y: 13 }, truncate(nd.node.kind, 17)),
      multi && (nodeHost(lab, nd.id) || nd.node.host_pin)
        ? s("text", { class: "hostbadge", x: NODE_W / 2 - 6, y: NODE_H / 2 + 13, "text-anchor": "end" }, `@${nodeHost(lab, nd.id) || nd.node.host_pin}`)
        : null,
      s("title", {}, `${nd.id} (${nd.node.kind}) — ${nodeStateText(rt)}`));
    g.append(el);
  }

  g.append(labels);
  svg.replaceChildren(g);
  if (fit) fitDiagram();
}

function truncate(str, n) {
  str = String(str ?? "");
  return str.length > n ? str.slice(0, n - 1) + "…" : str;
}

function fitDiagram() {
  const svg = $("#diagram");
  const pts = Object.values(S.positions);
  if (!pts.length) return;
  const w = svg.clientWidth || 800, hgt = svg.clientHeight || 500;
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const minX = Math.min(...xs) - NODE_W, maxX = Math.max(...xs) + NODE_W;
  const minY = Math.min(...ys) - NODE_H * 1.5, maxY = Math.max(...ys) + NODE_H * 1.5;
  const k = Math.min(1.4, Math.min(w / (maxX - minX), hgt / (maxY - minY)));
  S.view = { k, x: w / 2 - ((minX + maxX) / 2) * k, y: hgt / 2 - ((minY + maxY) / 2) * k };
  const vp = $("#viewport");
  if (vp) vp.setAttribute("transform", `translate(${S.view.x},${S.view.y}) scale(${S.view.k})`);
}

function setupDiagramInteraction() {
  const svg = $("#diagram");
  let drag = null;

  svg.addEventListener("pointerdown", (ev) => {
    const nodeEl = ev.target.closest(".node");
    svg.setPointerCapture(ev.pointerId);
    if (nodeEl) {
      const id = nodeEl.dataset.id;
      drag = { type: "node", id, sx: ev.clientX, sy: ev.clientY, start: [...S.positions[id]], moved: false };
    } else {
      drag = { type: "pan", sx: ev.clientX, sy: ev.clientY, start: { ...S.view }, moved: false };
      svg.classList.add("panning");
    }
  });
  svg.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    const dx = ev.clientX - drag.sx, dy = ev.clientY - drag.sy;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    if (!drag.moved) return;
    if (drag.type === "node") {
      S.positions[drag.id] = [drag.start[0] + dx / S.view.k, drag.start[1] + dy / S.view.k];
      renderDiagram(false);
    } else {
      S.view.x = drag.start.x + dx;
      S.view.y = drag.start.y + dy;
      $("#viewport")?.setAttribute("transform", `translate(${S.view.x},${S.view.y}) scale(${S.view.k})`);
    }
  });
  svg.addEventListener("pointerup", () => {
    svg.classList.remove("panning");
    if (!drag) return;
    if (drag.type === "node") {
      if (drag.moved) savePositions();
      else nodeClicked(drag.id);
    } else if (!drag.moved) {
      selectNode(null);
    }
    drag = null;
  });
  svg.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const rect = svg.getBoundingClientRect();
    const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
    const k = Math.max(0.2, Math.min(3, S.view.k * Math.exp(-ev.deltaY * 0.0015)));
    S.view.x = mx - ((mx - S.view.x) / S.view.k) * k;
    S.view.y = my - ((my - S.view.y) / S.view.k) * k;
    S.view.k = k;
    $("#viewport")?.setAttribute("transform", `translate(${S.view.x},${S.view.y}) scale(${S.view.k})`);
  }, { passive: false });

  $("#fit").addEventListener("click", fitDiagram);
  $("#relayout").addEventListener("click", () => {
    S.positions = {};
    savePositions();
    for (const n of S.detail?.nodes || []) n.pos = null;
    renderDiagram(true);
  });
}

// Clicks re-render the diagram, so double clicks are detected here rather
// than with the browser's dblclick event (its target may already be gone).
let lastClick = { id: null, t: 0 };
function nodeClicked(id) {
  const now = Date.now();
  const isDouble = lastClick.id === id && now - lastClick.t < 400;
  lastClick = { id, t: isDouble ? 0 : now };
  selectNode(id);
  if (!isDouble) return;
  const node = S.detail.nodes.find((n) => n.name === id);
  const rt = nodeRuntime(S.detail.name, id);
  if (rt?.state === "running" && node?.modes.length) openTerminal(S.detail.name, id, node.modes[0]);
}

function selectNode(id) {
  S.selectedNode = id;
  renderDiagram(false);
  renderNodeCard();
}

function renderNodeCard() {
  const card = $("#node-card");
  const node = S.detail?.nodes?.find((n) => n.name === S.selectedNode);
  if (!node) { card.hidden = true; return; }
  const lab = S.detail.name;
  const rt = nodeRuntime(lab, node.name);
  const running = rt?.state === "running";
  const rows = [
    ["Kind", node.kind + (node.type ? ` (${node.type})` : "")],
    ["Image", rt?.image || node.image],
    ["State", nodeStateText(rt)],
    ["Mgmt", rt?.ipv4],
    ["Container", rt?.container],
    S.state?.multi_host ? ["Host", rt?.host || placedHost(node.name) || (node.host_pin && `${node.host_pin} (pinned)`) || (node.host_tags && `tags: ${node.host_tags}`)] : null,
  ].filter((r) => r && r[1]);
  card.replaceChildren(
    h("h3", {},
      h("span", { class: `dot ${nodeState(rt)}` }),
      node.name,
      h("button", { class: "close", title: "Close", onclick: () => selectNode(null) }, "×")),
    h("dl", {}, rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])),
    openButtons(lab, node.name, node.modes, running, !!rt));
  card.hidden = false;
}

// ---------------------------------------------------------------------------
// Jobs
// ---------------------------------------------------------------------------

const CONFIRM = {
  destroy: (lab) => `Destroy lab "${lab}"? All nodes are removed and unsaved configs are lost.`,
  redeploy: (lab) => `Redeploy lab "${lab}"? Every node is recreated from scratch and unsaved configs are lost.`,
};

async function runAction(action) {
  if (S.selected?.type !== "topo") return;
  const lab = S.detail.name;
  if (CONFIRM[action] && !confirm(CONFIRM[action](lab))) return;
  try {
    const options = action === "deploy" || action === "redeploy" ? { rollback: $("#opt-rollback").checked } : {};
    const job = await api("/api/jobs", { method: "POST", body: JSON.stringify({ action, topology: S.selected.id, options }) });
    S.state.jobs = [job, ...(S.state.jobs || [])];
    renderLabHead();
    renderSidebar();
    trackJob(job.id, true);
  } catch (e) {
    toast(e.message);
  }
}

function appendActivity(lines) {
  const pre = $("#activity");
  if (pre.dataset.fresh !== "1") { pre.textContent = ""; pre.dataset.fresh = "1"; }
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
  for (const line of lines) {
    const cls = /^✗|ERRO|Error/.test(line) ? "err" : /^[✓↺]/.test(line) ? "ok" : /^(»|\$)/.test(line) ? "info" : null;
    pre.append(cls ? h("span", { class: cls }, line + "\n") : line + "\n");
  }
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

// Show a job's output in the Activity pane, following it while it runs
function trackJob(id, fromStart) {
  clearTimeout(S.jobPolling);
  S.jobPolling = null;
  S.viewJob = id;
  const pre = $("#activity");
  pre.dataset.fresh = "";
  pre.textContent = "";
  let offset = 0;
  let sawRunning = false;
  if (fromStart) activatePane("activity", true);
  renderJobPicker();

  const poll = async () => {
    let job;
    try {
      job = await api(`/api/jobs/${id}?offset=${offset}`);
    } catch (e) {
      if (S.viewJob === id) S.jobPolling = setTimeout(poll, 2000);
      return;
    }
    if (S.viewJob !== id) return;  // switched to another job meanwhile
    if (job.lines.length) appendActivity(job.lines);
    offset = job.offset;
    const known = (S.state?.jobs || []).find((j) => j.id === id);
    if (known) Object.assign(known, { status: job.status, finished: job.finished, host_times: job.host_times });
    renderJobMeta();
    $("#activity-dot").className = `dot ${job.status === "running" ? "busy" : job.status === "ok" ? "running" : "error"}`;
    if (job.status === "running") {
      sawRunning = true;
      S.jobPolling = setTimeout(poll, 700);
      return;
    }
    S.jobPolling = null;
    if (!sawRunning) return;  // a finished job opened from history
    if (job.status !== "ok") toast(`${job.action} failed — see Activity`);
    await refreshState();
    await reloadDetail();
    refreshHosts();
  };
  poll();
}

// ---------------------------------------------------------------------------
// Dock & terminals
// ---------------------------------------------------------------------------

let termSeq = 0;
const terms = new Map(); // pane id -> {term, fit, ws}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function activatePane(id, expand) {
  for (const t of document.querySelectorAll(".dock-tab")) t.classList.toggle("active", t.dataset.pane === id);
  for (const p of document.querySelectorAll(".pane")) p.classList.toggle("active", p.dataset.pane === id);
  if (expand) setDockCollapsed(false);
  const t = terms.get(id);
  if (t) requestAnimationFrame(() => { t.fit.fit(); t.term.focus(); });
}

function setDockCollapsed(collapsed) {
  $("#dock").classList.toggle("collapsed", collapsed);
  $("#dock-toggle").textContent = collapsed ? "▴" : "▾";
  if (!collapsed) for (const t of terms.values()) requestAnimationFrame(() => t.fit.fit());
}

function openTerminal(lab, node, mode) {
  const id = `term-${++termSeq}`;
  const pane = h("div", { class: "pane term", "data-pane": id });
  $("#dock-panes").append(pane);

  const tab = h("button", { class: "dock-tab", "data-pane": id, onclick: () => activatePane(id) },
    h("span", { class: "dot busy" }),
    `${node} · ${MODE_LABEL[mode]}`,
    h("span", {
      class: "x", role: "button", title: mode === "logs" ? "Close log" : "Close terminal",
      onclick: (ev) => { ev.stopPropagation(); closeTerminal(id); },
    }, "×"));
  $("#dock-tabs").append(tab);

  const logs = mode === "logs";
  const term = new Terminal({
    fontFamily: cssVar("--mono") || "monospace",
    fontSize: 13,
    cursorBlink: !logs,
    disableStdin: logs,
    scrollback: 5000,
    theme: { background: cssVar("--term-bg") || "#0b0d10" },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(pane);
  activatePane(id, true);
  fit.fit();

  const qs = new URLSearchParams({ lab, node, mode, cols: term.cols, rows: term.rows });
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/terminal?${qs}`);
  ws.binaryType = "arraybuffer";
  const dot = tab.querySelector(".dot");

  ws.onopen = () => { dot.className = "dot running"; };
  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      const msg = JSON.parse(ev.data);
      if (msg.t === "exit") {
        term.write(`\r\n\x1b[90m[session ended${msg.code != null && msg.code >= 0 ? `, exit ${msg.code}` : ""}]\x1b[0m\r\n`);
        dot.className = "dot";
      }
      return;
    }
    term.write(new Uint8Array(ev.data));
  };
  ws.onclose = () => { dot.className = "dot"; };
  term.onData((d) => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: "i", d })); });
  term.onResize(({ cols, rows }) => {
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: "r", c: cols, r: rows }));
  });
  const ro = new ResizeObserver(() => { if (pane.classList.contains("active")) fit.fit(); });
  ro.observe(pane);

  terms.set(id, { term, fit, ws, ro });
}

function closeTerminal(id) {
  const t = terms.get(id);
  if (t) {
    t.ws.close();
    t.ro.disconnect();
    t.term.dispose();
    terms.delete(id);
  }
  const wasActive = document.querySelector(`.dock-tab[data-pane="${id}"]`)?.classList.contains("active");
  document.querySelector(`.dock-tab[data-pane="${id}"]`)?.remove();
  document.querySelector(`.pane[data-pane="${id}"]`)?.remove();
  if (wasActive) {
    const last = [...document.querySelectorAll(".dock-tab")].pop();
    activatePane(last.dataset.pane);
  }
}

function setupDock() {
  const dock = $("#dock");
  $("#dock-toggle").addEventListener("click", () => setDockCollapsed(!dock.classList.contains("collapsed")));
  $("#dock-tabs").addEventListener("click", (ev) => {
    const tab = ev.target.closest(".dock-tab");
    if (tab && dock.classList.contains("collapsed")) setDockCollapsed(false);
  });
  document.querySelector('.dock-tab[data-pane="activity"]').addEventListener("click", () => activatePane("activity"));

  const handle = $("#dock-resize");
  handle.addEventListener("pointerdown", (ev) => {
    handle.setPointerCapture(ev.pointerId);
    const startY = ev.clientY, startH = dock.offsetHeight;
    const move = (e) => { dock.style.height = `${Math.max(120, startH + startY - e.clientY)}px`; };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      for (const t of terms.values()) t.fit.fit();
      renderDiagram(false);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
  });
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

function setup() {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => showView(tab.dataset.view));
  }
  for (const btn of document.querySelectorAll("#lab-actions button")) {
    btn.addEventListener("click", () => runAction(btn.dataset.action));
  }
  $("#refresh").addEventListener("click", () => { refreshState(); refreshHosts(); });
  const rollback = $("#opt-rollback");
  try { rollback.checked = localStorage.getItem("clab-rollback") === "1"; } catch { /* private mode */ }
  rollback.addEventListener("change", () => {
    try { localStorage.setItem("clab-rollback", rollback.checked ? "1" : "0"); } catch { /* private mode */ }
  });
  setupDiagramInteraction();
  setupDock();
  window.addEventListener("resize", () => renderDiagram(false));

  refreshState();
  refreshHosts();
  $("#job-select").addEventListener("change", (e) => trackJob(e.target.value, false));
  setInterval(() => { if (!document.hidden) refreshState(); }, 5000);
  setInterval(() => { if (!document.hidden) renderJobMeta(); }, 1000);
  setInterval(() => { if (!document.hidden) refreshHosts(); }, 30000);
}

setup();
