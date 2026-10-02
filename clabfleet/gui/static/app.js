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
const THEMES = { system: "◐ System", light: "☀ Light", dark: "☾ Dark" };
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
  if (res.status === 401) showLogin("Your session has ended. Log in again.");
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

let toastTimer;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 6000);
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
function statusGlyph(state, cx, cy) {
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
  return s("g", { class: `status ${cls}` }, ...shape);
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
  refreshLive();
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
  // Counts are in the status strip below
  badge.textContent = st.state === "stopped" ? "not deployed" : st.state;

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
  actions.querySelector('[data-action="deploy"]').hidden = st.deployed > 0;
  $("#lab-secondary").hidden = st.deployed === 0;
  for (const btn of actions.querySelectorAll("[data-action]")) {
    const a = btn.dataset.action;
    const enabled = a === "deploy" ? st.deployed === 0 : st.deployed > 0;
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
    items.push(stat("", isTopo ? `${st.total ?? 0} nodes` : "no containers", "", toNodes));
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
  menu.querySelector("[data-action]").addEventListener("click", () => setOpen(false));
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
// Health: everything wrong with the open lab in one list
// ---------------------------------------------------------------------------

const HEALTH_SOURCES = { topology: "Topology file", nodes: "Nodes", links: "Links", hosts: "Hosts", routing: "Routing" };
const HEALTH_SEV_ORDER = { error: 0, warn: 1, info: 2 };
const BOOT_SLOW_SEC = 300;           // booting longer than this is reported
const bootSeen = new Map();          // "lab/node" -> when it was first seen booting (ms)
const healthFilter = { sev: "all", source: "all" };

// Checks of the saved topology file, for its validation errors and warnings
async function validateSaved() {
  if (S.selected?.type !== "topo" || !S.detail || S.detail.error) return;
  const { id } = S.selected, { yaml, hash } = S.detail;
  if (S.fileValidation?.id === id && S.fileValidation.hash === hash) return;
  try {
    const report = await api(`/api/validate/${topoPath(id)}`, { method: "POST", body: JSON.stringify({ yaml }) });
    if (S.selected?.id === id && S.detail?.hash === hash) {
      S.fileValidation = { id, hash, report };
      renderLabHead();
    }
  } catch (e) { /* the next reload tries again */ }
}

// One item per problem: {sev: error|warn|info, source, tag, msg, go}
function healthItems() {
  const items = [];
  const lab = currentLabName();
  if (!lab) return items;
  const add = (sev, source, msg, go, tag) => items.push({ sev, source, msg, go, tag: tag || HEALTH_SOURCES[source] });
  const isTopo = S.selected.type === "topo";
  const toYaml = () => showView("yaml");

  if (isTopo && S.detail?.error) add("error", "topology", `The topology file could not be loaded: ${S.detail.error}`, toYaml);
  const fv = isTopo && S.fileValidation?.id === S.selected.id && S.fileValidation.hash === S.detail?.hash
    ? S.fileValidation.report : null;
  for (const e of fv?.errors || []) add("error", "topology", e, toYaml);
  for (const w of fv?.warnings || []) add("warn", "topology", w, toYaml);

  // Hosts the lab runs on, or every host while it does not run
  const used = new Set(labContainers(lab).map((c) => c.host));
  for (const r of S.state?.runtime || []) {
    if (!r.ok && (!used.size || used.has(r.host))) add("error", "hosts", `${r.host} cannot be reached: ${r.error || "no answer"}`);
  }

  const st = labStatus(lab);
  if (st.deployed) {
    const names = isTopo && S.detail?.nodes ? S.detail.nodes.map((n) => n.name) : labContainers(lab).map((c) => c.node);
    const now = Date.now();
    for (const name of names) {
      const rt = nodeRuntime(lab, name);
      const go = () => revealNode(name);
      const key = `${lab}/${name}`;
      if (rt?.state === "running" && rt.ready === false) {
        if (!bootSeen.has(key)) bootSeen.set(key, now);
        const sec = (now - bootSeen.get(key)) / 1000;
        if (sec > BOOT_SLOW_SEC) add("warn", "nodes", `${name} has been booting for ${fmtDuration(sec)}${rt.ready_detail ? `: ${rt.ready_detail}` : ""}`, go);
        continue;
      }
      bootSeen.delete(key);
      if (!rt) add("warn", "nodes", `${name} is not deployed, but the rest of the lab is`, go);
      else if (rt.state !== "running") add("error", "nodes", `${name} is not running: ${rt.status || rt.state}`, go);
    }
  }

  const live = liveData();
  for (const l of (isTopo && live && S.detail?.links) || []) {
    const ls = live.links?.[l.id];
    if (ls?.state === "down") {
      add("error", "links", `${endpointLabel(l.a)} ↔ ${endpointLabel(l.b)} is down: ${downEnds(ls).join(", ")}`,
        () => { showView("diagram"); selectLink(l.id); });
    }
  }

  if (isTopo) {
    for (const p of window.Routing?.health() || []) add(p.sev, "routing", p.msg, p.go, p.tag);
  }
  return items.sort((a, b) => HEALTH_SEV_ORDER[a.sev] - HEALTH_SEV_ORDER[b.sev]);
}

// Select a node on the Diagram (or the Nodes table for labs without a file)
function revealNode(name) {
  if (S.selected?.type === "topo" && S.detail?.nodes) {
    showView("diagram");
    selectNode(name);
  } else {
    showView("nodes");
  }
}

function healthCounts(items) {
  return { error: items.filter((i) => i.sev === "error").length, warn: items.filter((i) => i.sev === "warn").length };
}

function healthOpen() {
  return document.querySelector('.pane[data-pane="health"]').classList.contains("active") &&
    !$("#dock").classList.contains("collapsed");
}

function openHealth(source = "all") {
  healthFilter.source = source;
  healthFilter.sev = "all";
  activatePane("health", true);
  renderHealth();
}

// The Health tab, and the problem count for the header
function renderHealth(items = healthItems()) {
  const c = healthCounts(items);
  $("#health-dot").className = `dot ${!currentLabName() ? "" : c.error ? "error" : c.warn ? "partial" : "running"}`;
  $("#health-count").textContent = c.error + c.warn ? String(c.error + c.warn) : "";
  if (healthOpen()) window.Routing?.pollLive();

  $("#health-sev").replaceChildren(...[["all", "All", items.length], ["error", "Errors", c.error], ["warn", "Warnings", c.warn]]
    .map(([k, label, n]) => h("button", {
      class: `seg-btn${healthFilter.sev === k ? " active" : ""}`,
      onclick: () => { healthFilter.sev = k; renderHealth(); },
    }, `${label} ${n}`)));
  const sel = $("#health-source");
  sel.replaceChildren(h("option", { value: "all" }, "All sources"),
    ...Object.entries(HEALTH_SOURCES).map(([k, label]) => h("option", { value: k }, label)));
  sel.value = healthFilter.source;

  const live = liveData(), rl = window.Routing?.liveAge();
  const ago = (t) => `${Math.max(0, Math.round(Date.now() / 1000 - t))}s ago`;
  $("#health-checks").textContent = !currentLabName() ? "" : [
    "Checked: file, nodes, hosts",
    live?.updated ? `links (read ${ago(live.updated)})` : null,
    window.Routing?.loaded() ? "routing configs" : null,
    rl != null ? `live routing (read ${rl}s ago)` : null,
  ].filter(Boolean).join(" · ");

  const shown = items.filter((i) => (healthFilter.sev === "all" || i.sev === healthFilter.sev) &&
    (healthFilter.source === "all" || i.source === healthFilter.source));
  const list = $("#health-list");
  if (!currentLabName()) {
    list.replaceChildren(h("li", { class: "health-empty" }, "Select a lab to check it."));
  } else if (!shown.length) {
    list.replaceChildren(h("li", { class: "health-empty" },
      items.length ? "Nothing matches this filter." : `No problems found in ${currentLabName()}.`));
  } else {
    list.replaceChildren(...shown.map((i) => h("li", {},
      h("button", { class: `health-row ${i.sev}`, onclick: i.go || null, disabled: !i.go },
        h("span", { class: `sev ${i.sev}`, "aria-label": { error: "Error", warn: "Warning", info: "Note" }[i.sev] }),
        h("span", { class: "tag" }, i.tag),
        h("span", { class: "msg" }, i.msg)))));
  }
  return c;
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
    const ls = live?.links?.[l.id];
    const down = ls?.state === "down";
    const stateText = down ? `\nDOWN: ${downEnds(ls).join(", ")}` : ls?.state === "up" ? "\nup" : "";
    const d = `M${p0[0]},${p0[1]} Q${c[0]},${c[1]} ${p1[0]},${p1[1]}`;
    const ends = `${l.a.id}|${l.b.id}`;
    g.append(s("path", {
      class: `link${l.special ? " special" : ""}${cross ? " cross" : ""}${down ? " down" : ""}${selectedLink()?.id === l.id ? " selected" : ""}`,
      d, "data-ends": ends,
    }, s("title", {}, `${l.a.id}:${l.a.iface} ↔ ${l.b.id}:${l.b.iface}${vxlan}${stateText}`)));
    // Wide invisible stroke so links are easy to click (packet capture)
    g.append(s("path", { class: "link-hit", d, "data-link": l.id },
      s("title", {}, `${l.a.id}:${l.a.iface} ↔ ${l.b.id}:${l.b.iface}${vxlan}${stateText}\nClick to capture packets`)));
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
    const usage = res && res.cpu != null ? `${fmtCpu(res.cpu)} · ${fmtBytes(res.mem)}` : "";
    const el = s("g", {
      class: `node${S.selectedNode === nd.id ? " selected" : ""}`,
      transform: `translate(${x},${y})`, "data-id": nd.id, "data-f": nd.id,
    },
      s("rect", { x: -NODE_W / 2, y: -NODE_H / 2, width: NODE_W, height: NODE_H, rx: 9 }),
      statusGlyph(stClass, -NODE_W / 2 + 13, -7),
      s("text", { class: "name", x: -NODE_W / 2 + 24, y: -2 }, truncate(nd.id, 14)),
      s("text", { class: "kind", x: -NODE_W / 2 + 24, y: 13 }, truncate(nd.node.kind, 16)),
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
    g.append(el);
  }

  g.append(labels);
  svg.replaceChildren(g);
  refocusDiagram();
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
  refocusDiagram = setupFocus(svg, () => S.selectedNode);
  let drag = null;

  svg.addEventListener("pointerdown", (ev) => {
    const nodeEl = ev.target.closest(".node");
    svg.setPointerCapture(ev.pointerId);
    if (nodeEl) {
      const id = nodeEl.dataset.id;
      drag = { type: "node", id, sx: ev.clientX, sy: ev.clientY, start: [...S.positions[id]], moved: false };
    } else {
      // A press on a link that does not move is a click on the link
      const link = ev.target.closest(".link-hit")?.dataset.link || null;
      drag = { type: "pan", link, sx: ev.clientX, sy: ev.clientY, start: { ...S.view }, moved: false };
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
      if (drag.link) selectLink(drag.link);
      else selectNode(null);
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
  if (rt?.state === "running" && node?.modes.length && canOperate()) openTerminal(S.detail.name, id, node.modes[0]);
}

function selectNode(id) {
  S.selectedNode = id;
  S.selectedLink = null;
  renderDiagram(false);
  renderNodeCard();
}

function renderNodeCard() {
  renderLinkCard();  // the link card refreshes at the same points
  const card = $("#node-card");
  const node = S.detail?.nodes?.find((n) => n.name === S.selectedNode);
  if (!node) { card.hidden = true; return; }
  const lab = S.detail.name;
  const rt = nodeRuntime(lab, node.name);
  const running = rt?.state === "running";
  const live = liveData();
  const res = running ? live?.nodes?.[node.name] : null;
  const down = Object.values(live?.links || {})
    .flatMap((ls) => [ls.a, ls.b])
    .filter((e) => e?.node === node.name && e.state === "down")
    .map((e) => `${e.iface} (${e.detail})`);
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
    down.length ? ["Links down", down.join(", ")] : null,
    S.state?.multi_host ? ["Host", rt?.host || placedHost(node.name) || (node.host_pin && `${node.host_pin} (pinned)`) || (node.host_tags && `tags: ${node.host_tags}`)] : null,
  ].filter((r) => r && r[1]);
  card.replaceChildren(
    h("h3", {},
      h("span", { class: `dot ${nodeState(rt)}` }),
      node.name,
      h("button", { class: "close", title: "Close", onclick: () => selectNode(null) }, "×")),
    h("dl", {}, rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])),
    openButtons(lab, node.name, node.modes, running, !!rt),
    S.selected?.type === "topo" && canOperate() && h("span", { class: "open diff-open" },
      h("button", {
        class: "btn small ghost",
        title: `Diff ${node.name}'s config in the latest snapshot`,
        onclick: () => openDiff(S.selected.id, node.name),
      }, "Config diff")));
  card.hidden = false;
}

// ---------------------------------------------------------------------------
// Packet capture (click a link, pick a side)
// ---------------------------------------------------------------------------

const CAPTURE_MAX_SECONDS = 600;   // server limit for downloads (live: 1800)
const CAPTURE_MAX_COUNT = 100000;
const captureForm = { side: "a", filter: "", count: "", duration: "60" };
const downloads = new Map();       // id -> {label, bytes, ctrl, stopped}
let downloadSeq = 0;
let linkCardKey = null;

function selectedLink() {
  const sel = S.selectedLink;
  if (!sel || S.selected?.type !== "topo" || S.selected.id !== sel.topo) return null;
  return S.detail?.links?.find((l) => l.id === sel.id) || null;
}

function selectLink(id) {
  S.selectedLink = id && S.selected?.type === "topo" ? { topo: S.selected.id, id } : null;
  S.selectedNode = null;
  renderDiagram(false);
  renderNodeCard();
}

function endpointLabel(e) {
  return e.node ? `${e.node}:${e.iface}` : `${e.special}${e.iface ? `:${e.iface}` : ""}`;
}

// Can this side of a link be captured on? Only running lab nodes can.
function captureSide(e) {
  if (!e.node) return { ok: false, why: "not a lab node" };
  const rt = nodeRuntime(S.detail.name, e.node);
  return rt?.state === "running" ? { ok: true, why: "" } : { ok: false, why: "not running" };
}

function renderLinkCard() {
  const card = $("#link-card");
  const l = selectedLink();
  if (!l) { card.hidden = true; linkCardKey = null; return; }
  // Built once per link so a refresh never clears what is being typed
  const key = `${S.selected.id}|${l.id}|${endpointLabel(l.a)}|${endpointLabel(l.b)}`;
  if (key !== linkCardKey) { buildLinkCard(card, l); linkCardKey = key; }
  syncLinkCard(card, l);
  card.hidden = false;
}

function buildLinkCard(card, l) {
  const f = captureForm;
  const field = (name, attrs) => h("input", {
    ...attrs, value: f[name], oninput: (ev) => { f[name] = ev.target.value; },
    onkeydown: (ev) => { if (ev.key === "Enter") startCapture("live"); },
  });
  card.replaceChildren(
    h("h3", {}, "Capture packets",
      h("button", { class: "close", title: "Close", onclick: () => selectLink(null) }, "×")),
    h("div", { class: "sides" }, ["a", "b"].map((k) =>
      h("label", { class: "side", "data-side": k },
        h("input", { type: "radio", name: "cap-side", value: k, onchange: () => { f.side = k; syncLinkCard(card, l); } }),
        h("span", { class: "mono" }, endpointLabel(l[k])),
        h("span", { class: "why muted small" })))),
    h("label", { class: "field" }, h("span", {}, "Filter"),
      field("filter", { class: "mono", placeholder: "e.g. tcp port 179", spellcheck: "false", autocomplete: "off" })),
    h("div", { class: "row" },
      h("label", { class: "field" }, h("span", {}, "Packets"),
        field("count", { type: "number", min: 1, max: CAPTURE_MAX_COUNT, placeholder: "no limit" })),
      h("label", { class: "field" }, h("span", {}, "Seconds"),
        field("duration", { type: "number", min: 1, max: CAPTURE_MAX_SECONDS }))),
    h("div", { class: "open" },
      h("button", { class: "btn small primary", "data-act": "live", title: "Decode packets live in a tab below",
        onclick: () => startCapture("live") }, "Live"),
      h("button", { class: "btn small", "data-act": "pcap", title: "Capture to a .pcap file for Wireshark",
        onclick: () => startCapture("pcap") }, "Download .pcap")),
    h("ul", { class: "downloads" }));
}

function syncLinkCard(card, l) {
  const sides = { a: captureSide(l.a), b: captureSide(l.b) };
  if (!sides[captureForm.side].ok) {
    const other = captureForm.side === "a" ? "b" : "a";
    if (sides[other].ok) captureForm.side = other;
  }
  for (const k of ["a", "b"]) {
    const label = card.querySelector(`.side[data-side="${k}"]`);
    const radio = label.querySelector("input");
    radio.disabled = !sides[k].ok;
    radio.checked = captureForm.side === k;
    label.classList.toggle("off", !sides[k].ok);
    label.querySelector(".why").textContent = sides[k].why;
  }
  const ok = sides[captureForm.side].ok;
  for (const btn of card.querySelectorAll("button[data-act]")) btn.disabled = !ok;
  renderDownloads();
}

function captureParams() {
  const l = selectedLink();
  const e = l?.[captureForm.side];
  if (!e?.node || S.selected?.type !== "topo") return null;
  const p = { topo: S.selected.id, node: e.node, iface: e.iface, duration: captureForm.duration || "60" };
  if (captureForm.filter.trim()) p.filter = captureForm.filter.trim();
  if (captureForm.count) p.count = captureForm.count;
  return p;
}

function startCapture(kind) {
  const p = captureParams();
  if (!p) return;
  if (kind === "live") {
    openTermTab(`${p.node}:${p.iface} · Capture`, "/ws/capture", p,
      { closeTitle: "Stop capture", blink: false });
  } else {
    downloadPcap(p);
  }
}

// Streamed with fetch rather than a plain link, so it can be stopped
// early and still keep what was captured so far
async function downloadPcap(p) {
  const id = ++downloadSeq;
  const dl = { label: `${p.node}:${p.iface}`, bytes: 0, ctrl: new AbortController(), stopped: false };
  downloads.set(id, dl);
  renderDownloads();
  const chunks = [];
  let name = `${p.node}-${p.iface}.pcap`.replace(/[^\w.-]/g, "_");
  try {
    const res = await fetch(`/api/capture?${new URLSearchParams(p)}`,
      { signal: dl.ctrl.signal, credentials: "same-origin" });
    if (!res.ok) throw new Error((await res.text()) || res.statusText);
    name = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "")?.[1] || name;
    const reader = res.body.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      dl.bytes += value.length;
      renderDownloads();
    }
  } catch (e) {
    if (!dl.stopped) {
      downloads.delete(id);
      renderDownloads();
      toast(`Capture failed: ${e.message}`);
      return;
    }
  }
  downloads.delete(id);
  renderDownloads();
  if (!chunks.length) { toast(`No packets captured on ${dl.label}`); return; }
  const url = URL.createObjectURL(new Blob(chunks, { type: "application/vnd.tcpdump.pcap" }));
  const a = h("a", { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

function renderDownloads() {
  const list = document.querySelector("#link-card .downloads");
  if (!list) return;
  list.replaceChildren(...[...downloads].map(([id, dl]) => h("li", {},
    h("span", { class: "dot busy" }),
    h("span", { class: "mono small" }, `${dl.label} · ${fmtBytes(dl.bytes)}`),
    h("button", {
      class: "btn small ghost", disabled: dl.stopped, title: "Stop capturing and save the file",
      onclick: () => { dl.stopped = true; dl.ctrl.abort(); },
    }, "Stop & save"))));
}

function fmtBytes(n) {
  return n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : n >= 1024 ? `${(n / 1024).toFixed(0)} KB` : `${n} B`;
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
  if (action === "destroy") {
    return {
      title: `Destroy ${lab}?`, ok: "Destroy lab", danger: true, typeToConfirm: big,
      body: [
        `Removes ${n}${where}, and the lab directory clab-${lab}/ with the configs saved there by Save configs.`,
        "Snapshots and the topology file are kept. Take a Snapshot first to keep the running configs.",
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
  const logs = mode === "logs";
  openTermTab(`${node} · ${MODE_LABEL[mode]}`, "/ws/terminal", { lab, node, mode },
    { closeTitle: logs ? "Close log" : "Close terminal", blink: !logs, readOnly: logs });
}

// A dock tab with an xterm attached to a session websocket (terminal,
// log or live packet capture)
function openTermTab(label, wsPath, params, opts = {}) {
  const id = `term-${++termSeq}`;
  const pane = h("div", { class: "pane term", "data-pane": id });
  $("#dock-panes").append(pane);

  const tab = h("button", { class: "dock-tab", "data-pane": id, onclick: () => activatePane(id) },
    h("span", { class: "dot busy" }),
    label,
    h("span", {
      class: "x", role: "button", title: opts.closeTitle || "Close",
      onclick: (ev) => { ev.stopPropagation(); closeTerminal(id); },
    }, "×"));
  $("#dock-tabs").append(tab);

  const term = new Terminal({
    fontFamily: cssVar("--mono") || "monospace",
    fontSize: 13,
    cursorBlink: !!opts.blink,
    disableStdin: !!opts.readOnly,
    scrollback: 5000,
    theme: { background: cssVar("--term-bg") || "#0b0d10" },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(pane);
  activatePane(id, true);
  fit.fit();

  const qs = new URLSearchParams({ ...params, cols: term.cols, rows: term.rows });
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}${wsPath}?${qs}`);
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

// A node's latest snapshot against its startup-config or the snapshot before
function openDiff(topoId, node) {
  const id = `diff-${++termSeq}`;
  const select = h("select", { class: "small", "aria-label": "Compare with" },
    h("option", { value: "startup" }, "vs startup-config"),
    h("option", { value: "previous" }, "vs previous snapshot"));
  const meta = h("span", { class: "muted small mono" });
  const pre = h("pre", { class: "activity mono" }, "Loading…");
  const pane = h("div", { class: "pane activity-pane", "data-pane": id },
    h("div", { class: "activity-bar" }, select, meta), pre);
  $("#dock-panes").append(pane);
  $("#dock-tabs").append(h("button", { class: "dock-tab", "data-pane": id, onclick: () => activatePane(id) },
    `${node} · Diff`,
    h("span", {
      class: "x", role: "button", title: "Close diff",
      onclick: (ev) => { ev.stopPropagation(); closeTerminal(id); },
    }, "×")));
  activatePane(id, true);

  const load = async () => {
    pre.textContent = "Loading…";
    meta.textContent = "";
    try {
      const qs = new URLSearchParams({ node, against: select.value });
      const d = await api(`/api/diff/${topoPath(topoId)}?${qs}`);
      meta.textContent = `${d.from} vs ${d.against}`;
      if (d.status === "skipped") { pre.textContent = `Not compared: ${d.reason}`; return; }
      if (!d.diff) { pre.textContent = "No differences."; return; }
      pre.replaceChildren(...d.diff.split("\n").map((line) => {
        const cls = /^(\+\+\+|---)/.test(line) ? "info" : line.startsWith("+") ? "ok" : line.startsWith("-") ? "err" : line.startsWith("@@") ? "info" : null;
        return cls ? h("span", { class: cls }, line + "\n") : line + "\n";
      }));
    } catch (e) {
      pre.textContent = e.message;
    }
  };
  select.addEventListener("change", load);
  load();
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
  document.querySelector('.dock-tab[data-pane="health"]').addEventListener("click", () => { activatePane("health"); renderHealth(); });
  $("#health-source").addEventListener("change", (ev) => { healthFilter.source = ev.target.value; renderHealth(); });
  renderHealth();

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
  renderRuntimeOverlays();
}

// Login links carry the token in the URL fragment (#token=...), which the
// browser does not send to the server. It is posted to /login instead and
// dropped from the address bar.
async function postLogin(token, switchUser = false) {
  const res = await fetch("/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token, switch: switchUser }),
  });
  if (res.status === 409) {
    // Logged in as someone else: only switch if the user says so
    const { user } = await res.json();
    if (await confirmDialog({
      title: "Switch user?",
      body: `You are logged in as ${user}. Log in as the user of this link instead?`,
      ok: "Switch user",
    })) {
      await postLogin(token, true);
    }
    return;
  }
  if (!res.ok) throw new Error((await res.text()) || res.statusText);
}

function showLogin(message) {
  $("#login").hidden = false;
  const err = $("#login-error");
  err.textContent = message || "";
  err.hidden = !message;
  $("#login-token").focus();
}

async function boot() {
  $("#login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      await postLogin($("#login-token").value.trim());
      location.replace("/");
    } catch (e) {
      showLogin(e.message);
    }
  });
  const token = new URLSearchParams(location.hash.slice(1)).get("token");
  if (token !== null) {
    history.replaceState(null, "", location.pathname + location.search);
    try {
      await postLogin(token);
    } catch (e) {
      showLogin(e.message);
      return;
    }
  }
  const res = await fetch("/api/me");
  if (res.status === 401) {
    showLogin();
    return;
  }
  setup();
}

async function logout() {
  try {
    await api("/logout", { method: "POST" });
  } catch (e) { /* the session is gone either way */ }
  location.href = "/";
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
  applyTheme(currentTheme);
  $("#theme").addEventListener("click", () => {
    const order = Object.keys(THEMES);
    const next = order[(order.indexOf(currentTheme) + 1) % order.length];
    try { localStorage.setItem("clab-theme", next); } catch { /* private mode */ }
    applyTheme(next);
  });
  $("#logout").addEventListener("click", logout);
  const rollback = $("#opt-rollback");
  try { rollback.checked = localStorage.getItem("clab-rollback") === "1"; } catch { /* private mode */ }
  rollback.addEventListener("change", () => {
    try { localStorage.setItem("clab-rollback", rollback.checked ? "1" : "0"); } catch { /* private mode */ }
  });
  setupDiagramInteraction();
  setupEditor();
  setupDock();
  window.addEventListener("resize", () => { renderDiagram(false); window.Routing?.resize(); });

  loadMe();
  refreshState();
  refreshHosts();
  $("#job-select").addEventListener("change", (e) => trackJob(e.target.value, false));
  setInterval(() => { if (!document.hidden) refreshState(); }, 5000);
  setInterval(() => { if (!document.hidden) renderJobMeta(); }, 1000);
  setInterval(() => { if (!document.hidden) refreshHosts(); }, 30000);
}

boot();
