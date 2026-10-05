"use strict";

// ---------------------------------------------------------------------------
// State & helpers
// ---------------------------------------------------------------------------

const S = {
  me: null,             // /api/me: {user, role, multi_user}
  state: null,          // /api/state
  selected: null,       // {type: "topo", id} | {type: "lab", lab}
  detail: null,         // /api/topologies/<id>
  selectedNode: null,
  selectedLink: null,   // {topo, id} of the clicked diagram link (packet capture)
  positions: {},       // node name -> [x, y] for the open topology
  view: { x: 0, y: 0, k: 1 },
  jobPolling: null,
  live: null,           // /api/live/<id> for the open topology, plus its id
};

const $ = (sel) => document.querySelector(sel);

// Colour theme: "system" follows prefers-color-scheme; light and dark set
// data-theme on <html>, which app.css checks before the media query
const THEMES = { system: "◐ System", light: "☀ Light", dark: "☾ Dark", contrast: "◑ High contrast" };
function savedTheme() {
  try { return THEMES[localStorage.getItem("clab-theme")] ? localStorage.getItem("clab-theme") : "system"; } catch { return "system"; }
}
let currentTheme = savedTheme();
function applyTheme(theme) {
  currentTheme = theme;
  if (theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  const btn = document.getElementById("theme");
  if (btn) btn.textContent = THEMES[theme];
}
applyTheme(currentTheme);  // before first render, so the page does not flash

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
  if (res.status === 401) {
    showLogin("Your session has ended. Log in again.", res);
  }
  if (!res.ok) throw new Error((await res.text()) || res.statusText);
  return res.json();
}

// In-app confirmation (replaces window.confirm): states the impact, keeps
// focus on Cancel, and for big labs asks to type the name. Resolves true
// only for the confirm button.
function confirmDialog({ title, body, ok = "OK", danger = false, typeToConfirm = null }) {
  const dlg = $("#confirm");
  if (dlg.open) dlg.close("cancel");
  $("#confirm-title").textContent = title;
  $("#confirm-body").replaceChildren(...[body].flat().filter(Boolean).map(
    (x) => (x instanceof Node ? x : h("p", {}, x))));
  const okBtn = $("#confirm-ok");
  okBtn.textContent = ok;
  okBtn.className = `btn ${danger ? "danger-solid" : "primary"}`;
  const typeRow = $("#confirm-type"), input = $("#confirm-input");
  typeRow.hidden = !typeToConfirm;
  $("#confirm-name").textContent = typeToConfirm || "";
  input.value = "";
  okBtn.disabled = !!typeToConfirm;
  input.oninput = () => { okBtn.disabled = input.value !== typeToConfirm; };
  input.onkeydown = (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); if (!okBtn.disabled) dlg.close("ok"); }
  };
  dlg.returnValue = "";
  dlg.showModal();
  (typeToConfirm ? input : $("#confirm-cancel")).focus();
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
  });
}

// Screen readers (E2): a polite live region. Each message is its own node,
// removed after a while, so repeated messages are still read.
function announce(msg) {
  const box = $("#announcer");
  if (!box || !msg) return;
  const line = h("div", {}, msg);
  box.append(line);
  setTimeout(() => line.remove(), 8000);
}

let toastTimer;
// ``opts``: {ok: true} for good news (a neutral border), and actions,
// [{label, run}], shown as buttons (the toast then stays a little longer)
function toast(msg, opts = {}) {
  const el = $("#toast");
  const actions = opts.actions || [];
  el.replaceChildren(h("span", {}, msg), ...actions.map((a) => h("button", {
    class: "btn small", onclick: () => { el.hidden = true; a.run(); },
  }, a.label)));
  el.classList.toggle("good", !!opts.ok);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), actions.length ? 12000 : 6000);
}

// A finished job, success or not: say so, with a way to its output and lab
function jobDone(j) {
  const ok = j.status === "ok";
  toast(`${j.action} ${j.lab || j.topology} ${ok ? "finished" : "failed"}`, {
    ok,
    actions: [
      { label: "View", run: () => { activatePane("activity", true); trackJob(j.id, false); } },
      ...(S.selected?.id === j.topology ? [] : [{ label: "Open lab", run: () => selectTopology(j.topology) }]),
    ],
  });
}

const MODE_LABEL = { cli: "CLI", shell: "Shell", ssh: "SSH", logs: "Logs" };

// Viewers are read-only (the server enforces it; this just hides controls)
function canOperate() {
  return S.me?.role !== "viewer";
}

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

// Status glyph for a node box on the canvas: same shapes as the .dot spans
// (ring, disc with a check, half ring, diamond) so state never relies on
// colour alone. ``state`` is a nodeState() value or "other".
function statusGlyph(state, cx, cy, badge = false) {
  const r = 5;
  const cls = state === "partial" ? "other" : state;
  let shape;
  if (cls === "running") {
    shape = [s("circle", { class: "disc", cx, cy, r: r + 0.5 }),
             s("path", { class: "mark", d: `M${cx - 2.4},${cy + 0.2} l1.7,1.8 l3.2,-3.6` })];
  } else if (cls === "booting") {
    shape = [s("circle", { class: "ring", cx, cy, r }),
             s("path", { class: "half", d: `M${cx},${cy - r} A${r},${r} 0 0 1 ${cx},${cy + r} Z` })];
  } else if (cls === "other") {
    shape = [s("path", { class: "diamond", d: `M${cx},${cy - r - 1} l${r + 1},${r + 1} l${-r - 1},${r + 1} l${-r - 1},${-r - 1} Z` })];
  } else {
    shape = [s("circle", { class: "ring", cx, cy, r: r - 0.5 })];
  }
  // A badge sits on the device glyph: a ring of the node's colour keeps it apart
  return s("g", { class: `status ${cls}` },
    badge ? s("circle", { class: "badge-bg", cx, cy, r: r + 2.5 }) : null, ...shape);
}

// What a node is, for its device glyph (A1): from its name first (the
// layout's role words), then its kind
const ROLE_BY_NAME = [
  [/(host|server|client|srv|^pc|^h\d)/i, "host"],
  [/(firewall|^fw)/i, "firewall"],
  [/(spine|leaf|tor|access|^sw|switch|agg|dist)/i, "switch"],
  [/(core|border|wan|edge|router|rtr|^pe\d|^p\d|^r\d)/i, "router"],
];
const ROLE_BY_KIND = {
  linux: "host", bridge: "switch", "ovs-bridge": "switch",
  arista_ceos: "switch", ceos: "switch", nokia_srlinux: "switch", srl: "switch", cisco_n9kv: "switch",
  cisco_iol: "router", cisco_csr1000v: "router", cisco_c8000v: "router", cisco_xrd: "router",
  juniper_crpd: "router", juniper_vmx: "router", juniper_vjunosrouter: "router",
  fortinet_fortigate: "firewall", paloalto_panos: "firewall", checkpoint_cloudguard: "firewall",
};
const KIND_NAMES = {
  arista_ceos: "Arista cEOS", ceos: "Arista cEOS", cisco_iol: "Cisco IOL", linux: "Linux",
  nokia_srlinux: "Nokia SR Linux", srl: "Nokia SR Linux", juniper_crpd: "Juniper cRPD",
  cisco_xrd: "Cisco XRd", cisco_csr1000v: "Cisco CSR1000v", cisco_c8000v: "Cisco C8000v",
  bridge: "Linux bridge", "ovs-bridge": "OVS bridge",
};

function nodeRole(name, kind) {
  for (const [re, role] of ROLE_BY_NAME) if (re.test(name)) return role;
  return ROLE_BY_KIND[kind] || "generic";
}

function kindName(kind) {
  return KIND_NAMES[kind] || kind || "";
}

// Motion (C5): a node or edge whose state changed since the last drawing
// pulses once. The first look at it only sets the baseline; nothing moves
// under prefers-reduced-motion (the stylesheet stops every animation).
const PULSE_MS = 1200;
const seenStates = new Map();  // key -> {state, at: when it last changed}

// Milliseconds since the state of ``key`` changed, while its pulse runs;
// null otherwise. Redraws within the pulse continue it (negative delay)
// instead of starting it again.
function noteState(key, state) {
  const now = performance.now();
  const seen = seenStates.get(key);
  if (!seen) { seenStates.set(key, { state, at: -Infinity }); return null; }
  if (seen.state !== state) { seen.state = state; seen.at = now; }
  const elapsed = now - seen.at;
  return elapsed < PULSE_MS ? elapsed : null;
}

function pulseAttrs(elapsed) {
  return elapsed == null ? {} : { style: `--pulse-delay: -${Math.round(elapsed)}ms` };
}

// Node box sizes on both canvases
const NODE_W = 148, NODE_H = 48, PSEUDO_W = 112, PSEUDO_H = 30;

// The left part of a node box: its device glyph with the status shape on
// its corner, like a presence badge (left free: the right edge carries the
// Routing badges and the builder's link handle)
const FACE_X = -NODE_W / 2 + 8, TEXT_X = -NODE_W / 2 + 38;
function nodeFace(name, kind, state) {
  return s("g", { class: "face" },
    s("use", { class: `dev dev-${nodeRole(name, kind)}`, href: `#d-${nodeRole(name, kind)}`,
               x: FACE_X, y: -12, width: 22, height: 22 }),
    statusGlyph(state, FACE_X + 21, 8, true));
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
  return `${JOB_ICON[j.status] || "•"} ${j.action} ${j.lab || j.topology} · ${when}` +
    (j.user ? ` · ${j.user}` : "");
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
    (hosts ? ` · ${hosts}` : "") + (j.user ? ` · by ${j.user}` : "");
}

// ---------------------------------------------------------------------------
// Data refresh
// ---------------------------------------------------------------------------

async function refreshState() {
  const before = new Map((S.state?.jobs || []).map((j) => [j.id, j.status]));
  try {
    S.state = await api("/api/state");
  } catch (e) {
    // D3: a banner while the server does not answer, instead of a toast each time
    if ($("#offline").hidden) announce("Cannot reach the clabfleet server");
    $("#offline").hidden = false;
    document.body.classList.add("server-offline");
    return;
  }
  if (!$("#offline").hidden) announce("Connected to the clabfleet server again");
  $("#offline").hidden = true;
  document.body.classList.remove("server-offline");
  // Jobs that finished in the background (the viewed one reports itself)
  for (const j of S.state.jobs) {
    if (before.get(j.id) === "running" && j.status !== "running" && j.id !== S.viewJob) {
      jobDone(j);
      announce(`${j.action} ${j.lab} ${j.status === "ok" ? "finished" : "failed"}`);
      if (S.selected?.type === "topo" && S.selected.id === j.topology) reloadDetail();
    }
  }
  for (const r of S.state.runtime) {
    if (!r.ok && r.error) console.warn(`host ${r.host}: ${r.error}`);
  }
  renderSidebar();
  renderLabHead();
  renderRuntimeOverlays();
  refreshLive();
  refreshEvents();
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
    S.hostInfo = hosts;
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

// Link states and CPU/memory of the open lab, fetched along with the state.
// The server answers from its cache and refreshes it in the background.
async function refreshLive() {
  const id = S.selected?.type === "topo" ? S.selected.id : null;
  if (!id || !S.detail || S.detail.error || !labStatus(S.detail.name).deployed) {
    if (S.live) { S.live = null; renderDiagram(false); renderNodeCard(); renderLabHead(); }
    return;
  }
  let data;
  try {
    data = await api(`/api/live/${topoPath(id)}`);
  } catch (e) {
    return;  // keep the last data; the next refresh tries again
  }
  if (S.selected?.type !== "topo" || S.selected.id !== id) return;
  S.live = { ...data, id };
  renderDiagram(false);
  renderNodeCard();
  renderLabHead();
  // First request for this lab: its probes are still running
  clearTimeout(S.liveRetry);
  if (!data.updated) S.liveRetry = setTimeout(refreshLive, 2500);
}

// How fresh live data is (D3), the same way for every live view: stale once
// six refresh intervals (at least 30 s) passed without a new read
function freshness(data) {
  if (!data?.updated) return { text: "reading the nodes…", stale: false };
  const age = Math.max(0, Date.now() / 1000 - data.updated);
  const stale = age > Math.max(30, 6 * (data.interval || 5));
  return { stale, text: `${stale ? "stale: last read" : "live: read"} ${fmtDuration(age)} ago` };
}

// Hosts of the open lab that do not answer: its live state cannot be fresh
function unreachableHosts() {
  const lab = currentLabName();
  const used = new Set(labContainers(lab).map((c) => c.host));
  return (S.state?.runtime || []).filter((r) => !r.ok && used.has(r.host)).map((r) => r.host);
}

// The Diagram's freshness label and stale look
function renderFreshness() {
  const label = $("#diagram-fresh"), svg = $("#diagram");
  const deployed = S.selected?.type === "topo" && S.detail && labStatus(S.detail.name).deployed;
  label.hidden = !deployed;
  if (!deployed) { svg.classList.remove("stale"); return; }
  const f = freshness(liveData());
  const down = unreachableHosts();
  const offline = document.body.classList.contains("server-offline");
  const stale = f.stale || down.length > 0 || offline;
  label.textContent = offline ? "server not answering: showing the last state"
    : down.length ? `${down.join(", ")} not answering: showing the last state` : f.text;
  label.classList.toggle("stale", stale);
  svg.classList.toggle("stale", stale);
}

function liveData() {
  return S.live && S.selected?.type === "topo" && S.live.id === S.selected.id ? S.live : null;
}

// "Spine-1:eth1 down" for each end of a link that is down
function downEnds(ls) {
  return [ls?.a, ls?.b].filter((e) => e?.state === "down").map((e) => `${e.node}:${e.iface} ${e.detail}`);
}

function fmtBytes(b) {
  if (b == null) return "?";
  if (b >= 1024 ** 3) return `${(b / 1024 ** 3).toFixed(1)} GiB`;
  return `${Math.round(b / 1024 ** 2)} MiB`;
}

// Link traffic (feature 5): bits per second, and the stroke it gets
function fmtRate(bps) {
  if (bps == null) return "?";
  for (const [unit, size] of [["Gb/s", 1e9], ["Mb/s", 1e6], ["kb/s", 1e3]]) {
    if (bps >= size) return `${(bps / size).toFixed(bps >= 10 * size ? 0 : 1)} ${unit}`;
  }
  return `${Math.round(bps)} b/s`;
}

// Thicker by orders of magnitude from 10 kb/s (1.6 px idle, up to 7 px)
const BUSY_BPS = 10e3;  // a link carrying less is drawn as idle

function linkWidth(bps) {
  if (!bps || bps < BUSY_BPS) return null;
  return Math.min(7, 1.6 + 1.1 * Math.log10(bps / BUSY_BPS) + 0.6).toFixed(1);
}

// A diagram link's live state and tooltip, the same for the logical and the
// rack drawing; ``where(vni)`` is how the drawing words a link between hosts
function linkLive(l, live, vnis, where) {
  const ls = live?.links?.[l.id];
  const down = ls?.state === "down";
  const rate = ls?.rate;
  const vni = vnis[`${l.a.id}:${l.a.iface}|${l.b.id}:${l.b.iface}`];
  const title = `${l.a.id}:${l.a.iface} ↔ ${l.b.id}:${l.b.iface}${where(vni)}` +
    (down ? `\nDOWN: ${downEnds(ls).join(", ")}` : ls?.state === "up" ? "\nup" : "") +
    (rate ? `\n${l.a.id} → ${l.b.id} ${fmtRate(rate.ab)}, ${l.b.id} → ${l.a.id} ${fmtRate(rate.ba)}` : "");
  return { ls, down, vni, title, bps: rate && !down ? Math.max(rate.ab || 0, rate.ba || 0) : 0 };
}

function fmtCpu(pct) {
  return pct == null ? "?" : `${pct < 10 ? pct.toFixed(1) : Math.round(pct)}%`;
}

function fmtMem(mb) {
  if (mb == null) return "?";
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`;
}

// ---------------------------------------------------------------------------
// Sidebar
// ---------------------------------------------------------------------------

// A ring filling up as nodes run (B4): reads at a glance even collapsed
function progressRing(running, total, state) {
  const r = 6.5, c = 2 * Math.PI * r, part = total ? Math.min(running / total, 1) : 0;
  return s("svg", { class: `ring ${state}`, width: 18, height: 18, viewBox: "0 0 18 18", "aria-hidden": "true" },
    s("circle", { class: "track", cx: 9, cy: 9, r }),
    s("circle", { class: "fill", cx: 9, cy: 9, r, "stroke-dasharray": `${part * c} ${c}`,
                  transform: "rotate(-90 9 9)" }));
}

// The sidebar's topologies (B4): filtered by the search box, deployed labs
// first, grouped by folder when the workspace has subfolders
function renderSidebar() {
  const topos = S.state.topologies;
  const topoNames = new Set(topos.map((t) => t.name));
  const words = ($("#topo-search")?.value || "").toLowerCase().split(/\s+/).filter(Boolean);
  const shown = topos.filter((t) => words.every((w) => `${t.name} ${t.id}`.toLowerCase().includes(w)))
    .map((t) => ({ t, st: labStatus(t.name, t.nodes) }))
    .sort((a, b) => (b.st.deployed > 0) - (a.st.deployed > 0) || naturalCmp(a.t.id, b.t.id));
  const folder = (id) => (id.includes("/") ? id.slice(0, id.lastIndexOf("/")) : "");
  const grouped = new Set(shown.map((x) => folder(x.t.id))).size > 1;
  const items = [];
  let group = null;
  for (const { t, st } of grouped ? [...shown].sort((a, b) => naturalCmp(folder(a.t.id), folder(b.t.id))) : shown) {
    if (grouped && folder(t.id) !== group) {
      group = folder(t.id);
      items.push(h("li", { class: "lab-group" }, group || "(top level)"));
    }
    const active = S.selected?.type === "topo" && S.selected.id === t.id;
    const busy = !!runningJob(t.id);
    const state = busy ? "busy" : t.error ? "error" : st.state;
    items.push(h("li", {},
      h("button", {
        class: `lab-item${active ? " active" : ""}`,
        onclick: () => selectTopology(t.id),
        title: `${t.name}\n${t.error || t.path}${st.deployed ? `\n${st.running}/${t.nodes} running` : ""}`,
      },
        h("span", { class: `dot ${state}` }),
        h("span", { class: "txt" },
          h("span", { class: "title" }, t.name),
          h("span", { class: "sub" }, t.id)),
        st.deployed ? h("span", { class: "count", "aria-label": `${st.running} of ${t.nodes} running` },
          progressRing(st.running, t.nodes, st.state), `${st.running}/${t.nodes}`) : null)));
  }
  if (!shown.length) items.push(h("li", { class: "lab-none muted small" }, topos.length ? "No lab matches." : "No topologies."));
  $("#topo-list").replaceChildren(...items);

  renderWelcome();
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

// First run and nothing selected (D1): the topologies as cards, and ways
// to start a new lab
let welcomeTemplates = null;

async function renderWelcome() {
  if (S.selected || !S.state) return;
  const topos = S.state.topologies;
  $("#welcome-labs").replaceChildren(...(topos.length ? topos.map((t) => {
    const st = labStatus(t.name, t.nodes);
    return h("button", { class: "card", onclick: () => selectTopology(t.id), title: t.error || t.path },
      h("span", { class: "card-head" },
        h("span", { class: `dot ${t.error ? "error" : st.state}` }),
        h("b", {}, t.name)),
      h("span", { class: "muted mono small" }, t.id),
      h("span", { class: "muted small" }, t.error ? "cannot be loaded"
        : `${t.nodes} node${t.nodes === 1 ? "" : "s"} · ${st.deployed ? `${st.running}/${t.nodes} running` : "not deployed"}`));
  }) : [h("p", { class: "muted" }, "No *.clab.yml files in the workspace yet.")]));
  if (!canOperate() || !window.Builder) return;
  if (!welcomeTemplates) {
    try { welcomeTemplates = await window.Builder.templates(); } catch { return; }
  }
  $("#welcome-new").replaceChildren(
    h("button", { class: "card new", onclick: () => window.Builder.newLab() },
      h("span", { class: "card-head" },
        s("svg", { class: "ico", "aria-hidden": "true" }, s("use", { href: "#i-plus" })), h("b", {}, "Blank canvas")),
      h("span", { class: "muted small" }, "Draw nodes and links, then generate configs")),
    ...Object.entries(welcomeTemplates).map(([name, t]) =>
      h("button", { class: "card new", onclick: () => window.Builder.newLab(name) },
        h("span", { class: "card-head" }, h("b", {}, name)),
        h("span", { class: "muted small" }, t.description))));
}

// ---------------------------------------------------------------------------
// YAML editor
// ---------------------------------------------------------------------------

function topoPath(id) {
  return encodeURIComponent(id).replace(/%2F/g, "/");
}

function yamlDirty() {
  return S.selected?.type === "topo" && !!S.detail && $("#yaml").value !== S.detail.yaml;
}

// Ask before throwing away unsaved editor changes
async function confirmDiscard() {
  if (window.Builder?.dirty() && !(await window.Builder.confirmLeave())) return false;
  return !yamlDirty() || confirmDialog({
    title: "Discard unsaved changes?",
    body: `Your edits to ${S.selected.id} in the YAML tab have not been saved.`,
    ok: "Discard changes", danger: true,
  });
}

function loadEditor() {
  $("#yaml").value = S.detail?.yaml || "";
  S.validation = null;
  renderEditorState();
}

function renderEditorState() {
  const dirty = yamlDirty();
  const busy = S.selected?.type === "topo" && !!runningJob(S.selected.id);
  const v = S.validation;
  const unloadable = dirty && v && !v.loadable;
  $("#yaml-save").disabled = !dirty || busy || unloadable || S.saving;
  $("#yaml-revert").disabled = !dirty || S.saving;
  const status = $("#yaml-status");
  let text = dirty ? "Unsaved changes" : "Saved";
  if (busy && dirty) text += " · a job is running for this lab, save when it finishes";
  if (v && dirty) text += v.errors.length ? ` · ${v.errors.length} error${v.errors.length > 1 ? "s" : ""}`
    : v.warnings.length ? ` · ${v.warnings.length} warning${v.warnings.length > 1 ? "s" : ""}` : " · valid";
  if (unloadable) text += " · fix it before saving";
  status.textContent = text;
  status.className = `muted small${unloadable ? " bad" : dirty ? " dirty" : ""}`;
  const list = $("#yaml-problems");
  const items = v && dirty ? [
    ...v.errors.map((e) => h("li", { class: "err" }, e)),
    ...v.warnings.map((w) => h("li", { class: "warn" }, w)),
  ] : [];
  list.replaceChildren(...items);
  list.hidden = !items.length;
}

function scheduleValidation() {
  clearTimeout(S.validateTimer);
  renderEditorState();
  if (!yamlDirty()) return;
  const id = S.selected.id, text = $("#yaml").value;
  S.validateTimer = setTimeout(async () => {
    try {
      const report = await api(`/api/validate/${topoPath(id)}`, { method: "POST", body: JSON.stringify({ yaml: text }) });
      if (S.selected?.id === id && $("#yaml").value === text) {
        S.validation = report;
        renderEditorState();
      }
    } catch (e) { /* the next keystroke tries again */ }
  }, 600);
}

async function saveYaml() {
  if (!yamlDirty() || S.saving) return;
  const id = S.selected.id, text = $("#yaml").value;
  S.saving = true;
  renderEditorState();
  try {
    const res = await fetch(`/api/topologies/${topoPath(id)}`, {
      method: "PUT", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ yaml: text, base_hash: S.detail.hash }),
    });
    if (res.status === 400) {
      S.validation = (await res.json()).validation;
      toast("Not saved: the YAML is not a valid topology");
    } else if (!res.ok) {
      toast(`Not saved: ${await res.text()}`);
    } else {
      const { detail, validation } = await res.json();
      if (S.selected?.id === id) {
        S.detail = detail;
        S.validation = validation;
        S.fileValidation = { id, hash: detail.hash, report: validation };
        renderDiagram(false);
        renderNodesTable();
        renderNodeCard();
        renderLabHead();
        toast(validation.errors.length ? `Saved with ${validation.errors.length} error(s)` : "Saved");
      }
      refreshState();
    }
  } catch (e) {
    toast(`Not saved: ${e.message}`);
  } finally {
    S.saving = false;
    renderEditorState();
  }
}

async function saveLayout() {
  if (S.selected?.type !== "topo" || !S.detail) return;
  if (yamlDirty()) { toast("Save or revert your YAML changes first"); return; }
  const positions = {};
  for (const n of S.detail.nodes) if (S.positions[n.name]) positions[n.name] = S.positions[n.name];
  try {
    const detail = await api(`/api/positions/${topoPath(S.selected.id)}`, {
      method: "PUT", body: JSON.stringify({ positions, base_hash: S.detail.hash }),
    });
    S.detail = detail;
    loadEditor();
    toast("Layout saved to the topology file");
  } catch (e) {
    toast(`Layout not saved: ${e.message}`);
  }
}

function setupEditor() {
  const ta = $("#yaml");
  ta.addEventListener("input", scheduleValidation);
  ta.addEventListener("keydown", (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") {
      ev.preventDefault();
      saveYaml();
    } else if (ev.key === "Tab" && !ev.shiftKey && !ev.ctrlKey && !ev.metaKey) {
      ev.preventDefault();  // YAML has no tabs: indent with spaces
      document.execCommand("insertText", false, "  ") || ta.setRangeText("  ", ta.selectionStart, ta.selectionEnd, "end");
      scheduleValidation();
    }
  });
  $("#yaml-save").addEventListener("click", saveYaml);
  $("#yaml-revert").addEventListener("click", async () => {
    await reloadDetail(true);
  });
  $("#save-layout").addEventListener("click", saveLayout);
  window.addEventListener("beforeunload", (ev) => {
    if (yamlDirty()) { ev.preventDefault(); ev.returnValue = ""; }
  });
}

async function selectTopology(id) {
  if (S.selected?.type === "topo" && S.selected.id === id) { reloadDetail(); return; }
  if (!(await confirmDiscard())) return;
  window.Builder?.reset();
  window.Run?.reset();
  window.Trace?.reset();
  window.Lanes?.reset();
  window.Annotations?.reset();
  S.selected = { type: "topo", id };
  S.selectedNode = null;
  try {
    S.detail = await api(`/api/topologies/${encodeURIComponent(id).replace(/%2F/g, "/")}`);
  } catch (e) {
    toast(e.message);
    return;
  }
  S.positions = loadPositions(id);
  S.live = null;
  $("#empty").hidden = true;
  $("#lab").hidden = false;
  loadEditor();
  window.Routing?.reset();
  setTabsAvailable(["diagram", "nodes", "routing", "yaml"]);
  renderSidebar();
  renderLabHead();
  renderDiagram(true);
  renderNodesTable();
  renderNodeCard();
  refreshLive();
  validateSaved();
  window.Routing?.prefetch();
}

// Re-fetch the selected topology (e.g. after a job changed its placement
// record). Unsaved editor text is kept unless ``discardEdits``.
async function reloadDetail(discardEdits) {
  if (S.selected?.type !== "topo") return;
  const id = S.selected.id;
  const keepEdits = !discardEdits && yamlDirty() ? $("#yaml").value : null;
  let detail;
  try {
    detail = await api(`/api/topologies/${encodeURIComponent(id).replace(/%2F/g, "/")}`);
  } catch (e) {
    return;
  }
  if (S.selected?.type !== "topo" || S.selected.id !== id) return;
  S.detail = detail;
  if (keepEdits === null) loadEditor();
  else { $("#yaml").value = keepEdits; renderEditorState(); }
  renderDiagram(false);
  renderNodesTable();
  renderNodeCard();
  window.Routing?.changed();
  validateSaved();
}

async function selectOtherLab(lab) {
  if (!(await confirmDiscard())) return;
  window.Builder?.reset();
  S.selected = { type: "lab", lab };
  S.detail = null;
  S.live = null;
  S.selectedNode = null;
  $("#empty").hidden = true;
  $("#lab").hidden = false;
  setTabsAvailable(["nodes"]);
  renderSidebar();
  renderLabHead();
  renderNodesTable();
}

// When a topology's kept lab directory last had a config saved (unix time), if it has one
function savedAt(topoId) {
  return (S.state?.topologies || []).find((t) => t.id === topoId)?.saved_at || null;
}

// "07:18" today, "2 Oct 07:18" before
function fmtWhen(unix) {
  const d = new Date(unix * 1000);
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString() ? time
    : `${d.toLocaleDateString([], { day: "numeric", month: "short" })} ${time}`;
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
  // Counts are in the status strip below. A lab that is not running may
  // have its configs kept from a Stop: the next deploy starts from them
  const saved = isTopo && st.state === "stopped" ? savedAt(S.selected.id) : null;
  badge.textContent = saved ? `stopped · configs saved ${fmtWhen(saved)}`
    : st.state === "stopped" ? "not deployed" : st.state;
  badge.title = saved ? `The lab directory clab-${lab}/ holds configs saved ${new Date(saved * 1000).toLocaleString()}. Deploy starts the nodes from them; "Discard saved configs" in the ⋯ menu starts over from the topology.` : "";

  const topoFile = labContainers(lab)[0]?.topo_file;
  const path = isTopo ? S.detail?.path || "" : topoFile || "";
  const pathBtn = $("#lab-path");
  pathBtn.hidden = !path;
  pathBtn.dataset.path = path;
  pathBtn.title = `${isTopo ? "" : "Deployed from "}${path}\nClick to copy the path`;
  pathBtn.setAttribute("aria-label", `Copy path ${path}`);

  renderLabStats(lab, st, isTopo);

  // One primary action: Deploy while nothing runs; the rest once it does
  const actions = $("#lab-actions");
  actions.hidden = !isTopo;
  const busy = isTopo && !!runningJob(S.selected.id);
  if (isTopo) renderEditorState();
  const deployBtn = actions.querySelector('[data-action="deploy"]');
  deployBtn.hidden = st.deployed > 0;
  deployBtn.lastChild.textContent = saved ? "Deploy from saved configs" : "Deploy";
  $("#lab-secondary").hidden = st.deployed === 0;
  // Destroy also removes the kept lab directory of a stopped lab
  const destroyBtn = actions.querySelector('[data-action="destroy"]');
  destroyBtn.lastChild.textContent = saved ? "Discard saved configs…" : "Destroy lab…";
  for (const btn of actions.querySelectorAll("[data-action]")) {
    const a = btn.dataset.action;
    const enabled = a === "deploy" ? st.deployed === 0 : st.deployed > 0 || (a === "destroy" && !!saved);
    btn.disabled = busy || !enabled || !!S.detail?.error;
  }
}

// "6/6 running · 2 hosts · 8/8 sessions up"; each stat opens its source
function renderLabStats(lab, st, isTopo) {
  const stat = (cls, content, title, onclick) => onclick
    ? h("button", { class: `stat ${cls}`, title, onclick }, content)
    : h("span", { class: `stat ${cls}`, title }, content);
  const items = [];
  const toNodes = () => showView("nodes");
  if (!st.deployed) {
    const total = st.total ?? 0;
    items.push(stat("", isTopo ? `${total} node${total === 1 ? "" : "s"}` : "no containers", "", toNodes));
  } else if (st.booting) {
    items.push(stat("warn", [h("b", {}, `${st.ready}/${st.total}`), " ready"], `${st.booting} booting`, toNodes));
  } else {
    const down = st.total - st.running;
    items.push(stat(down ? "warn" : "", [h("b", {}, `${st.running}/${st.total}`), " running"],
      down ? `${down} node${down === 1 ? "" : "s"} not running` : "", toNodes));
  }
  if (S.state?.multi_host && st.deployed) {
    const hosts = [...new Set(labContainers(lab).map((c) => c.host))].sort();
    items.push(stat("", [h("b", {}, hosts.length), ` host${hosts.length === 1 ? "" : "s"}`], hosts.join(", ")));
  }
  const ses = isTopo ? window.Routing?.liveSummary() : null;
  if (ses?.total) {
    const down = ses.total - ses.up;
    items.push(stat(down ? "bad" : "", [h("b", {}, `${ses.up}/${ses.total}`), " sessions up"],
      `OSPF and BGP sessions, read ${ses.age}s ago`, () => window.Routing.showLive()));
  }
  const c = renderHealth();
  const n = c.error + c.warn;
  items.push(stat(c.error ? "bad" : c.warn ? "warn" : "", n ? [h("b", {}, n), ` problem${n === 1 ? "" : "s"}`] : "no problems",
    "Open the Health panel", () => openHealth()));
  $("#lab-stats").replaceChildren(...items.flatMap((el, i) => (i ? [h("span", { class: "sep", "aria-hidden": "true" }, "·"), el] : [el])));
}

async function copyLabPath() {
  const path = $("#lab-path").dataset.path;
  try {
    await navigator.clipboard.writeText(path);
    toast(`Copied ${path}`);
  } catch {
    toast(path);  // no clipboard (plain http): show it to copy by hand
  }
}

function setupLabMenu() {
  const btn = $("#lab-more"), menu = $("#lab-menu");
  const setOpen = (open) => {
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
    if (open) menu.querySelector("input, button:not(:disabled)")?.focus();
  };
  btn.addEventListener("click", () => setOpen(menu.hidden));
  document.addEventListener("pointerdown", (ev) => {
    if (!menu.hidden && !ev.target.closest(".menu-wrap")) setOpen(false);
  });
  menu.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") { setOpen(false); btn.focus(); }
  });
  // Any action in it (on narrow screens the secondary actions live here too)
  menu.addEventListener("click", (ev) => { if (ev.target.closest("[data-action]")) setOpen(false); });
}

let setSidebarCollapsed = null;
// E4: narrow screens. The secondary lab actions move into the ⋯ menu, the
// sidebar starts as a rail and opens over the page, closing on a pick.
const NARROW = window.matchMedia("(max-width: 760px)");
function applyNarrow() {
  const group = $("#lab-secondary"), menu = $("#lab-menu");
  if (NARROW.matches && group.parentElement !== menu) {
    menu.prepend(group, h("hr", { class: "narrow-sep" }));
    group.classList.add("in-menu");
    for (const b of group.querySelectorAll(".btn")) b.setAttribute("role", "menuitem");
  } else if (!NARROW.matches && group.parentElement === menu) {
    menu.querySelector(".narrow-sep")?.remove();
    $("#lab-actions .menu-wrap").before(group);
    group.classList.remove("in-menu");
    for (const b of group.querySelectorAll(".btn")) b.removeAttribute("role");
  }
  if (NARROW.matches) setSidebarCollapsed?.(true, false);
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
  for (const v of ["diagram", "nodes", "routing", "yaml"]) $(`#view-${v}`).hidden = v !== view;
  if (view === "diagram") renderDiagram(false);
  if (view === "routing") window.Routing?.show();
  syncInspector();
}

function currentView() {
  return document.querySelector(".tab.active")?.dataset.view;
}

// Show the inspector while the current tab has a selection to describe.
// Each card says on which tabs it belongs (data-views).
function syncInspector() {
  const insp = $("#inspector");
  const view = currentView();
  let any = false;
  for (const card of insp.querySelectorAll(".insp-card")) {
    const here = card.dataset.views.split(" ").includes(view);
    card.classList.toggle("off-view", !here);
    if (here && !card.hidden && getComputedStyle(card).display !== "none") any = true;
  }
  insp.hidden = !any;
}

const INSPECTOR_MIN = 260;

function setupInspector() {
  const insp = $("#inspector"), handle = $("#inspector-resize");
  try {
    const w = Number(localStorage.getItem("clab-inspector-w"));
    if (w >= INSPECTOR_MIN) insp.style.width = `${w}px`;
  } catch { /* private mode */ }
  handle.addEventListener("pointerdown", (ev) => {
    handle.setPointerCapture(ev.pointerId);
    handle.classList.add("dragging");
    const startX = ev.clientX, startW = insp.offsetWidth;
    const move = (e) => { insp.style.width = `${Math.max(INSPECTOR_MIN, startW + startX - e.clientX)}px`; };
    const up = () => {
      handle.classList.remove("dragging");
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      try { localStorage.setItem("clab-inspector-w", String(insp.offsetWidth)); } catch { /* private mode */ }
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
  });
}

function renderRuntimeOverlays() {
  if (!S.selected) return;
  if (S.selected.type === "topo" && S.detail) {
    renderDiagram(false);
    renderNodeCard();
    window.Routing?.runtime();
  }
  renderNodesTable();
}

// ---------------------------------------------------------------------------
// Nodes table
// ---------------------------------------------------------------------------

// Terminal buttons for a node; Logs also works for a stopped container
function openButtons(lab, node, modes, running, exists) {
  const buttons = (canOperate() ? modes || [] : []).map((m) =>
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
  const pick = S.selected.type === "topo";
  $("#node-rows").replaceChildren(...rows.map((n) => {
    const running = n.rt?.state === "running";
    return h("tr", {
      class: pick ? `pick${S.selectedNode === n.name ? " selected" : ""}` : null,
      onclick: pick ? (ev) => { if (!ev.target.closest("button")) selectNode(S.selectedNode === n.name ? null : n.name); } : null,
    },
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


function loadPositions(id) {
  try { return JSON.parse(localStorage.getItem(`clab-pos:${id}`)) || {}; } catch { return {}; }
}
function savePositions() {
  if (S.selected?.type !== "topo") return;
  try { localStorage.setItem(`clab-pos:${S.selected.id}`, JSON.stringify(S.positions)); } catch { /* private mode */ }
}

// The topology the Diagram draws: the builder's draft while editing, else the file
function diagramDetail() {
  return window.Builder?.detail() || S.detail;
}

function graphModel() {
  const D = diagramDetail();
  const nodes = D.nodes.map((n) => ({ id: n.name, node: n, pseudo: false }));
  const links = [];
  let pseudoCount = 0;
  for (const l of D.links) {
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

let diagramModel = null;
let refocusDiagram = () => {};

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
  const live = liveData();

  const g = s("g", { id: "viewport", transform: `translate(${S.view.x},${S.view.y}) scale(${S.view.k})` });

  // Rack view: hosts as racks, links as cables (racks.js)
  window.Racks?.sync();
  if (window.Racks?.active()) {
    window.Racks.draw(g, diagramModel);
    showDiagram(svg, g, fit);
    return;
  }

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
    const { ls, down, title, bps } = linkLive(l, live, vnis, (vni) =>
      (cross ? `  (VXLAN ${ha} ↔ ${hb}${vni !== undefined ? `, VNI ${vni}` : ""})` : ""));
    const d = `M${p0[0]},${p0[1]} Q${c[0]},${c[1]} ${p1[0]},${p1[1]}`;
    const ends = `${l.a.id}|${l.b.id}`;
    const width = linkWidth(bps);
    g.append(s("path", {
      class: `link${l.special ? " special" : ""}${cross ? " cross" : ""}${down ? " down" : ""}${selectedLink()?.id === l.id ? " selected" : ""}${width ? " busy" : ""}`,
      d, "data-ends": ends, style: width ? `stroke-width: ${width}` : null,
    }, s("title", {}, title)));
    // Wide invisible stroke so links are easy to click (packet capture)
    g.append(s("path", { class: "link-hit", d, "data-link": l.id },
      s("title", {}, `${title}\nClick to capture packets`)));
    for (const [end, from, alt, side] of [[l.a, p0, false, "a"], [l.b, p1, true, "b"]]) {
      if (!end.iface) continue;
      const pseudo = end.id.startsWith("~");
      const label = ifaceLabel(from, c, pseudo ? PSEUDO_W : NODE_W, pseudo ? PSEUDO_H : NODE_H, end.iface, alt);
      if (ls?.[side]?.state === "down") label.classList.add("down");
      label.dataset.ends = ends;
      labels.append(label);
    }
  }

  // Nodes
  for (const nd of diagramModel.nodes) {
    const [x, y] = P[nd.id];
    if (nd.pseudo) {
      g.append(s("g", { class: "pseudo", transform: `translate(${x},${y})`, "data-f": nd.id },
        s("rect", { x: -PSEUDO_W / 2, y: -PSEUDO_H / 2, width: PSEUDO_W, height: PSEUDO_H, rx: 6 }),
        s("text", { class: "name", "text-anchor": "middle", y: 4 }, truncate(nd.label, 18))));
      continue;
    }
    const rt = nodeRuntime(lab, nd.id);
    const stClass = { running: "running", booting: "booting", partial: "other" }[nodeState(rt)] || "";
    const res = rt?.state === "running" ? live?.nodes?.[nd.id] : null;
    const changed = noteState(`${lab}/${nd.id}`, stClass);
    const usage = res && res.cpu != null ? `${fmtCpu(res.cpu)} · ${fmtBytes(res.mem)}` : "";
    const el = s("g", {
      class: `node${S.selectedNode === nd.id ? " selected" : ""}${changed != null ? " pulse" : ""}`,
      transform: `translate(${x},${y})`, "data-id": nd.id, "data-f": nd.id, ...pulseAttrs(changed),
      role: "img", "aria-label": nodeLabel(nd.id),
    },
      s("rect", { x: -NODE_W / 2, y: -NODE_H / 2, width: NODE_W, height: NODE_H, rx: 9 }),
      nodeFace(nd.id, nd.node.kind, stClass),
      s("text", { class: "name", x: TEXT_X, y: -2 }, truncate(nd.id, 13)),
      s("text", { class: "kind", x: TEXT_X, y: 13 }, truncate(kindName(nd.node.kind), 16)),
      multi && (nodeHost(lab, nd.id) || nd.node.host_pin)
        ? s("text", { class: "hostbadge", x: NODE_W / 2 - 6, y: NODE_H / 2 + 13, "text-anchor": "end" }, `@${nodeHost(lab, nd.id) || nd.node.host_pin}`)
        : null,
      // CPU bar along the bottom of the box, full at one busy core
      usage ? s("rect", { class: "cputrack", x: -NODE_W / 2 + 9, y: NODE_H / 2 - 6, width: NODE_W - 18, height: 2.5, rx: 1.25 }) : null,
      usage ? s("rect", {
        class: `cpubar${res.cpu >= 80 ? " hot" : ""}`, x: -NODE_W / 2 + 9, y: NODE_H / 2 - 6,
        width: Math.max(1.5, ((NODE_W - 18) * Math.min(res.cpu, 100)) / 100), height: 2.5, rx: 1.25,
      }) : null,
      s("title", {}, `${nd.id} (${nd.node.kind}) — ${nodeStateText(rt)}` +
        (usage ? `\nCPU ${fmtCpu(res.cpu)} · memory ${fmtBytes(res.mem)}` : "")));
    window.Builder?.decorateNode(el, nd.id);
    g.append(el);
  }

  window.Trace?.decorate(g, P);
  window.Annotations?.decorate(g);
  window.Lanes?.decorate(g, P);  // last: behind the boxes
  g.append(labels);
  showDiagram(svg, g, fit);
}

function showDiagram(svg, g, fit) {
  svg.replaceChildren(g);
  applyView(svg, "viewport", S.view);
  markKb(svg);
  renderFreshness();
  refocusDiagram();
  if (fit) fitDiagram();
}

// Where each node is drawn now: the rack view's slots or the logical positions
function drawnPositions() {
  return window.Racks?.active() ? window.Racks.positions() : S.positions;
}

const naturalCmp = (a, b) => a.localeCompare(b, undefined, { numeric: true });

function truncate(str, n) {
  str = String(str ?? "");
  return str.length > n ? str.slice(0, n - 1) + "…" : str;
}

// What a screen reader hears for a node of the Diagram
function nodeLabel(id) {
  const D = diagramDetail();
  const node = D?.nodes?.find((n) => n.name === id);
  if (!node) return id;
  const rt = nodeRuntime(D.name, id);
  const links = (D.links || []).filter((l) => l.a.node === id || l.b.node === id).length;
  const down = Object.values(liveData()?.links || {}).flatMap((ls) => [ls.a, ls.b])
    .filter((e) => e?.node === id && e.state === "down").length;
  return `${id}, ${kindName(node.kind)}, ${nodeStateText(rt)}, ${links} link${links === 1 ? "" : "s"}` +
    (down ? `, ${down} down` : "");
}

function fitDiagram() {
  const svg = $("#diagram");
  const pts = Object.values(S.positions);
  const racks = window.Racks?.active() ? window.Racks.bounds() : null;
  if (!pts.length && !racks) return;
  S.view = fitBounds(svg, racks || nodeBounds(pts));
  applyView(svg, "viewport", S.view);
}

function setupDiagramInteraction() {
  const svg = $("#diagram");
  refocusDiagram = setupFocus(svg, () => S.selectedNode);
  nodeDoubleClick = doubleClicks();
  const apply = () => applyView(svg, "viewport", S.view);

  setupCanvasPointer(svg, {
    view: () => S.view,
    apply,
    press: (ev) => {
      const nodeEl = ev.target.closest(".node");
      // Rack slots are computed: a press on a device is a click, a drag pans
      if (nodeEl && window.Racks?.active()) return { type: "pan", node: nodeEl.dataset.id };
      if (nodeEl) {
        const id = nodeEl.dataset.id;
        return { type: "node", id, start: [...S.positions[id]] };
      }
      // A press on a link that does not move is a click on the link
      return { type: "pan", link: ev.target.closest(".link-hit")?.dataset.link || null };
    },
    move: (drag, pos) => {
      S.positions[drag.id] = pos;
      renderDiagram(false);
    },
    drop: () => savePositions(),
    click: (drag) => {
      if (drag.type === "node") nodeClicked(drag.id);
      else if (drag.node) nodeClicked(drag.node);
      else if (drag.link) selectLink(drag.link);
      else selectNode(null);
    },
  });

  // Node keys first: they claim the arrows while on a node, zoom pans otherwise
  setupNodeKeys(svg, {
    nodes: () => (diagramModel?.nodes || []).filter((n) => !n.pseudo).map((n) => ({ id: n.id, pos: drawnPositions()[n.id] })),
    links: () => (diagramModel?.links || []).map((l) => [l.a.id, l.b.id]),
    label: nodeLabel,
    selected: () => S.selectedNode,
    select: (id) => selectNode(id),
    open: (id) => { if (!openDefaultTerminal(id)) announce(`${id} has no terminal to open`); },
    view: () => S.view,
    apply,
  });
  setupZoom(svg, () => S.view, fitDiagram, apply);
  $("#relayout").addEventListener("click", () => {
    window.Lanes?.reset();
    S.positions = {};
    savePositions();
    for (const n of S.detail?.nodes || []) n.pos = null;
    renderDiagram(true);
  });
}

// Double click opens the node's CLI
let nodeDoubleClick = () => false;
function nodeClicked(id) {
  const isDouble = nodeDoubleClick(id);
  selectNode(id);
  if (isDouble && !window.Builder?.editing) openDefaultTerminal(id);
}

function selectNode(id) {
  S.selectedNode = id;
  S.selectedLink = null;
  renderDiagram(false);
  renderNodeCard();
  if (currentView() === "nodes") renderNodesTable();
}

function renderNodeCard() {
  if (window.Builder?.renderInspector()) { syncInspector(); return; }  // editing: its forms
  renderLinkCard();  // the link card refreshes at the same points
  const card = $("#node-card");
  const node = S.detail?.nodes?.find((n) => n.name === S.selectedNode);
  if (!node) { card.hidden = true; syncInspector(); return; }
  const lab = S.detail.name;
  const rt = nodeRuntime(lab, node.name);
  const running = rt?.state === "running";
  const live = liveData();
  const res = running ? live?.nodes?.[node.name] : null;
  const rows = [
    ["Kind", node.kind + (node.type ? ` (${node.type})` : "")],
    ["Image", rt?.image || node.image],
    ["State", nodeStateText(rt)],
    ["Mgmt", rt?.ipv4],
    ["Container", rt?.container],
    res?.cpu != null ? ["CPU", fmtCpu(res.cpu)] : null,
    res?.mem != null ? ["Memory", `${fmtBytes(res.mem)}` +
      (res.mem_limit ? ` of ${fmtBytes(res.mem_limit)}` : "") +
      (res.mem_percent != null ? ` (${res.mem_percent}%)` : "")] : null,
    S.state?.multi_host ? ["Host", rt?.host || placedHost(node.name) || (node.host_pin && `${node.host_pin} (pinned)`) || (node.host_tags && `tags: ${node.host_tags}`)] : null,
  ].filter((r) => r && r[1]);
  const protos = window.Routing?.nodeSummary(node.name) || [];
  card.replaceChildren(
    h("h3", {},
      h("span", { class: `dot ${nodeState(rt)}` }),
      node.name,
      h("button", { class: "close", title: "Close", "aria-label": "Close", onclick: () => selectNode(null) }, "×")),
    h("h4", {}, "Overview"),
    h("dl", {}, rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])),
    ...(nodeInterfaces(node.name, live) || []),
    protos.length ? h("h4", {}, "Protocols") : null,
    protos.length ? h("div", { class: "proto-list" }, protos.map((p) => h("button", {
      class: "proto-row", title: `Show ${node.name} in the Routing tab (${p.label})`, onclick: p.go,
    }, h("b", {}, p.label), h("span", { class: "mono" }, p.text), h("span", { class: "go-arrow", "aria-hidden": "true" }, "→")))) : null,
    h("h4", {}, "Actions"),
    openButtons(lab, node.name, node.modes, running, !!rt),
    S.selected?.type === "topo" && canOperate() && h("span", { class: "open diff-open" },
      h("button", {
        class: "btn small ghost",
        title: `Diff ${node.name}'s config in the latest snapshot`,
        onclick: () => openDiff(S.selected.id, node.name),
      }, "Config diff")),
    ...nodeWhatif(node, rt, live));
  card.hidden = false;
  syncInspector();
}

// The node's links: interface, far end and (live) state; a row selects the link
function nodeInterfaces(name, live) {
  const rows = [];
  for (const l of S.detail.links) {
    for (const [mine, other, side] of [[l.a, l.b, "a"], [l.b, l.a, "b"]]) {
      if (mine.node !== name) continue;
      const end = live?.links?.[l.id]?.[side];
      rows.push({ l, iface: mine.iface, peer: endpointLabel(other), state: end?.state, detail: end?.detail });
    }
  }
  if (!rows.length) return null;
  rows.sort((x, y) => naturalCmp(x.iface || "", y.iface || ""));
  const showState = rows.some((r) => r.state);
  return [
    h("h4", {}, "Interfaces"),
    h("table", { class: "rt-table" },
      h("thead", {}, h("tr", {}, h("th", {}, "Interface"), h("th", {}, "Peer"), showState ? h("th", {}, "State") : null)),
      h("tbody", {}, rows.map((r) => h("tr", {
        class: "go", title: `Select the link ${name}:${r.iface} ↔ ${r.peer}`,
        onclick: () => { showView("diagram"); selectLink(r.l.id); },
      },
        h("td", {}, r.iface),
        h("td", {}, r.peer),
        showState ? h("td", { class: r.state || "", title: r.detail || "" }, r.state === "down" ? `down · ${r.detail}` : r.state || "") : null)))),
  ];
}

// ---------------------------------------------------------------------------
// Jobs
// ---------------------------------------------------------------------------

// Labs with at least this many nodes ask for their name before Destroy / Redeploy
const TYPE_TO_CONFIRM_NODES = 10;

// What Destroy / Redeploy will do to this lab, for the confirmation dialog
function actionImpact(action, lab) {
  const cs = labContainers(lab);
  const hosts = [...new Set(cs.map((c) => c.host))].sort();
  const where = hosts.length > 1 ? ` on ${hosts.length} hosts (${hosts.join(", ")})` : hosts.length ? ` on ${hosts[0]}` : "";
  const n = `${cs.length} node${cs.length === 1 ? "" : "s"}`;
  const big = cs.length >= TYPE_TO_CONFIRM_NODES ? lab : null;
  if (action === "destroy" && !cs.length) {
    return {
      title: `Discard the saved configs of ${lab}?`, ok: "Discard saved configs", danger: true,
      body: [
        `Removes the lab directory clab-${lab}/ with the configs saved there. The next Deploy starts every node from the topology's startup configs.`,
        "Snapshots and the topology file are kept.",
      ],
    };
  }
  if (action === "destroy") {
    return {
      title: `Destroy ${lab}?`, ok: "Destroy lab", danger: true, typeToConfirm: big,
      body: [
        `Removes ${n}${where}, and the lab directory clab-${lab}/ with the configs saved there by Save configs.`,
        "Snapshots and the topology file are kept. Take a Snapshot first to keep the running configs.",
      ],
    };
  }
  if (action === "stop") {
    return {
      title: `Stop ${lab}?`, ok: "Stop lab", danger: true,
      body: [
        `Saves every node's running config, then removes ${n}${where} to free their memory and disk.`,
        `The lab directory clab-${lab}/ with the saved configs stays, so the next Deploy starts from them. If saving fails, nothing is removed. Destroy (in the ⋯ menu) also deletes the lab directory.`,
      ],
    };
  }
  if (action === "redeploy") {
    return {
      title: `Redeploy ${lab}?`, ok: "Redeploy lab", danger: true, typeToConfirm: big,
      body: [
        `Destroys and recreates ${n}${where} from their startup configs.`,
        "Changes made on the running nodes since the deploy are lost unless you Save configs or take a Snapshot first.",
      ],
    };
  }
  return null;
}

async function runAction(action) {
  if (S.selected?.type !== "topo") return;
  const lab = S.detail.name;
  const impact = actionImpact(action, lab);
  if (impact && !(await confirmDialog(impact))) return;
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

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

async function loadMe() {
  try {
    S.me = await api("/api/me");
  } catch (e) {
    toast(`Could not load your user: ${e.message}`);
    return;
  }
  document.body.classList.toggle("viewer", !canOperate());
  $("#yaml").readOnly = !canOperate();
  const who = $("#whoami");
  who.hidden = !S.me.multi_user;
  who.replaceChildren(h("b", {}, S.me.user || ""), ` · ${S.me.role}`);
  $("#logout").hidden = false;  // ends the session in both modes
  $("#password").hidden = !S.me.has_password;
  $("#users").hidden = !S.me.can_manage_users;
  renderRuntimeOverlays();
}

async function boot() {
  $("#pw-form").addEventListener("submit", submitPassword);
  $("#pw-cancel").addEventListener("click", () => { $("#pwchange").hidden = true; });
  $("#login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const creds = login.mode === "token" || login.useToken
      ? { token: $("#login-token").value.trim() }
      : { username: $("#login-user").value.trim(), password: $("#login-pass").value };
    try {
      await postLogin(creds);
      location.replace("/");
    } catch (e) {
      $("#login-pass").value = "";
      showLogin(e.message);
    }
  });
  $("#login-switch").addEventListener("click", () => {
    login.useToken = !login.useToken;
    showLogin();
  });
  // #token=...: a login link. #login_error=...: single sign-on did not work out
  const fragment = new URLSearchParams(location.hash.slice(1));
  const token = fragment.get("token");
  let linkError = fragment.get("login_error");
  if (token !== null || linkError !== null) {
    history.replaceState(null, "", location.pathname + location.search);
  }
  if (token !== null) {
    try {
      await postLogin({ token });
    } catch (e) {
      linkError = e.message;
    }
  }
  const res = await fetch("/api/me");
  if (res.status === 401) {
    showLogin(linkError, res);
    return;
  }
  // First login (admin / admin): a new password before anything else
  if ((await res.json()).must_change) {
    showPasswordForm(true);
    return;
  }
  setup();
}

function setup() {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => showView(tab.dataset.view));
  }
  for (const btn of document.querySelectorAll("#lab-actions [data-action]")) {
    btn.addEventListener("click", () => runAction(btn.dataset.action));
  }
  setupLabMenu();
  $("#lab-path").addEventListener("click", copyLabPath);
  $("#refresh").addEventListener("click", () => { refreshState(); refreshHosts(); });
  $("#topo-search").addEventListener("input", () => renderSidebar());
  const side = $("#sidebar"), toggle = $("#side-toggle");
  const setCollapsed = (on, persist = true) => {
    side.classList.toggle("collapsed", on);
    toggle.textContent = on ? "»" : "«";
    toggle.title = on ? "Expand the sidebar" : "Collapse the sidebar";
    toggle.setAttribute("aria-label", toggle.title);
    toggle.setAttribute("aria-expanded", String(!on));
    if (persist) try { localStorage.setItem("clab-sidebar", on ? "collapsed" : ""); } catch { /* private mode */ }
    requestAnimationFrame(() => { renderDiagram(false); window.Routing?.resize(); });
  };
  setSidebarCollapsed = setCollapsed;
  applyNarrow();
  NARROW.addEventListener("change", applyNarrow);
  // On a narrow screen the open sidebar covers the page: a pick closes it
  side.addEventListener("click", (ev) => {
    if (NARROW.matches && ev.target.closest(".lab-item") && !side.classList.contains("collapsed")) setCollapsed(true, false);
  });
  try { if (localStorage.getItem("clab-sidebar") === "collapsed") setCollapsed(true); } catch { /* private mode */ }
  toggle.addEventListener("click", () => setCollapsed(!side.classList.contains("collapsed")));
  applyTheme(currentTheme);
  $("#theme").addEventListener("click", () => {
    const order = Object.keys(THEMES);
    const next = order[(order.indexOf(currentTheme) + 1) % order.length];
    try { localStorage.setItem("clab-theme", next); } catch { /* private mode */ }
    applyTheme(next);
  });
  $("#logout").addEventListener("click", logout);
  $("#password").addEventListener("click", () => showPasswordForm(false));
  $("#users").addEventListener("click", openUsers);
  $("#users-close").addEventListener("click", () => $("#users-dlg").close());
  $("#users-add").addEventListener("submit", addUser);
  const rollback = $("#opt-rollback");
  try { rollback.checked = localStorage.getItem("clab-rollback") === "1"; } catch { /* private mode */ }
  rollback.addEventListener("change", () => {
    try { localStorage.setItem("clab-rollback", rollback.checked ? "1" : "0"); } catch { /* private mode */ }
  });
  setupDiagramInteraction();
  setupInspector();
  setupEditor();
  setupDock();
  window.addEventListener("resize", () => { renderDiagram(false); window.Routing?.resize(); });

  loadMe();
  refreshState();
  refreshHosts();
  $("#job-select").addEventListener("change", (e) => trackJob(e.target.value, false));
  setInterval(() => { if (!document.hidden) refreshState(); }, 5000);
  setInterval(() => {
    if (document.hidden) return;
    renderJobMeta();
    renderFreshness();
    if (S.steps?.live && S.steps.status === "running") renderSteps();
  }, 1000);
  setInterval(() => { if (!document.hidden) refreshHosts(); }, 30000);
}

// boot() is called from main.js, once every script has loaded
