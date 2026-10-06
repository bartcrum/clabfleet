"use strict";

// ---------------------------------------------------------------------------
// Health: everything wrong with the open lab in one list, and Events: the
// changes between live reads (feature 2).
//
// Uses app.js globals: S, $, h, s, api, topoPath, copyText, renderLabHead,
// currentLabName, showView, labContainers, labStatus, nodeRuntime,
// fmtDuration, liveData, downEnds, canOperate, runningJob, runAction,
// selectNode, announce; and from capture.js: endpointLabel, selectLink; from
// dock.js: openDiff, activatePane.
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
    for (const p of window.Routing?.health() || []) {
      add(p.sev, "routing", p.msg, p.go, p.tag);
      items[items.length - 1].drift = p.drift;
    }
  }
  return items.sort((a, b) => HEALTH_SEV_ORDER[a.sev] - HEALTH_SEV_ORDER[b.sev]);
}

// Drift (feature 4): the running config differs from the startup config.
// One click to see how, one to keep it.
function driftActions(node) {
  if (!canOperate() || S.selected?.type !== "topo" || !node || node.startsWith("ext:")) return null;
  return h("span", { class: "drift-actions" },
    h("button", { class: "btn small", title: `Diff ${node}'s running config, read now, against its startup-config`,
                  onclick: () => openDiff(S.selected.id, node, "running") }, "Config diff"),
    h("button", { class: "btn small", title: "Save every node's running config into the lab directory",
                  disabled: !!runningJob(S.selected.id), onclick: () => runAction("save") }, "Save configs"));
}

// ---------------------------------------------------------------------------
// Events: changes between live reads (feature 2)
// ---------------------------------------------------------------------------

const EVENT_WINDOW = 30 * 60;  // the strip's span, seconds
const EVENT_GOOD = new Set(["up"]);
let eventsCache = { id: null, events: [] };

async function refreshEvents() {
  const id = S.selected?.type === "topo" ? S.selected.id : null;
  if (!id) { eventsCache = { id: null, events: [] }; renderEvents(); return; }
  try {
    const { events } = await api(`/api/events/${topoPath(id)}`);
    if (S.selected?.id === id) eventsCache = { id, events };
  } catch (e) { return; }
  renderEvents();
}

function eventSev(e) {
  return EVENT_GOOD.has(e.to) ? "ok" : ["down", "missing"].includes(e.to) ? "error" : "warn";
}

function focusEvent(e) {
  if (e.kind === "link") { showView("diagram"); selectLink(e.id); }
  else window.Routing?.focus(e.kind, { type: "edge", id: e.id }, true);
}

// A change as a line of text, to paste somewhere, with its full time (UTC):
// "2026-10-06 16:02:33 UTC  Spine-1:eth1 ↔ Leaf-1:eth1: up → down (no carrier)"
function eventLine(e) {
  const when = new Date(e.t * 1000).toISOString().slice(0, 19).replace("T", " ");
  return `${when} UTC  ${e.label}: ${e.from} → ${e.to}${e.detail ? ` (${e.detail})` : ""}`;
}

// The changes on show as text, newest first, under a line saying which lab
let eventsShown = [];
function copyEvents() {
  const n = eventsShown.length;
  const what = `${n} change${n === 1 ? "" : "s"}`;
  copyText([`${currentLabName()}: ${what}, newest first`, ...eventsShown.map(eventLine)].join("\n"), what);
}

function renderEvents() {
  const now = Date.now() / 1000;
  const events = eventsCache.id && eventsCache.id === S.selected?.id ? eventsCache.events : [];
  const recent = events.filter((e) => now - e.t < 600).length;
  $("#events-count").textContent = recent ? String(recent) : "";
  // The strip: one mark per change in the last half hour, by what it went to
  const strip = $("#events-strip");
  const w = strip.clientWidth || 600, hgt = 26;
  const marks = events.filter((e) => now - e.t < EVENT_WINDOW).map((e) => {
    const x = w - ((now - e.t) / EVENT_WINDOW) * (w - 8) - 4;
    return s("rect", { class: `ev-mark ${eventSev(e)}`, x: x - 1.5, y: 4, width: 3, height: hgt - 8, rx: 1 },
      s("title", {}, `${new Date(e.t * 1000).toLocaleTimeString()} ${e.label}: ${e.from} → ${e.to}`));
  });
  strip.setAttribute("viewBox", `0 0 ${w} ${hgt}`);
  strip.replaceChildren(s("line", { class: "ev-axis", x1: 4, y1: hgt / 2, x2: w - 4, y2: hgt / 2 }),
    s("text", { class: "ev-tick", x: 4, y: hgt - 2 }, "30 min ago"),
    s("text", { class: "ev-tick", x: w - 4, y: hgt - 2, "text-anchor": "end" }, "now"), ...marks);
  const list = $("#events-list");
  eventsShown = [...events].reverse().slice(0, 200);
  $("#events-copy").hidden = !eventsShown.length;
  if (!events.length) {
    list.replaceChildren(h("li", { class: "health-empty" },
      S.selected?.type === "topo" ? "No changes seen yet." : "Select a lab to see its changes."));
    return;
  }
  list.replaceChildren(...eventsShown.map((e) => h("li", { class: "health-item" },
    h("button", { class: `health-row ${eventSev(e)}`, onclick: () => focusEvent(e) },
      h("span", { class: `sev ${eventSev(e) === "ok" ? "info ok" : eventSev(e)}` }),
      h("span", { class: "tag mono" }, new Date(e.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })),
      h("span", { class: "msg" }, `${e.label}: ${e.from} → `, h("b", {}, e.to), e.detail ? ` (${e.detail})` : "")),
    h("button", {
      class: "icon-btn health-copy-one", type: "button", title: "Copy this line",
      "aria-label": `Copy: ${e.label}: ${e.from} to ${e.to}`,
      onclick: () => copyText(eventLine(e), "the line"),
    }, s("svg", { class: "ico", "aria-hidden": "true" }, s("use", { href: "#i-copy" }))))));
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

// Problems that appear or clear in the open lab are announced (E2); the
// first look at a lab only sets the baseline
let healthSeen = { lab: null, keys: new Set() };
function announceHealth(items) {
  const lab = currentLabName();
  const keys = new Map(items.filter((i) => i.sev !== "info").map((i) => [`${i.tag}|${i.msg}`, i]));
  if (lab !== healthSeen.lab) { healthSeen = { lab, keys: new Set(keys.keys()) }; return; }
  const added = [...keys.keys()].filter((k) => !healthSeen.keys.has(k));
  const cleared = [...healthSeen.keys].filter((k) => !keys.has(k));
  healthSeen.keys = new Set(keys.keys());
  const say = (list, what, text) => {
    if (list.length > 3) announce(`${list.length} ${what} in ${lab}`);
    else for (const k of list) announce(text(k));
  };
  say(added, "new problems", (k) => `Problem: ${keys.get(k).msg}`);
  say(cleared, "problems cleared", (k) => `Cleared: ${k.split("|").slice(1).join("|")}`);
}

// The Health tab, and the problem count for the header
// A problem as a line of text, to paste somewhere: "ERROR  Hosts: h2 cannot be reached: ..."
const HEALTH_SEV_TEXT = { error: "ERROR", warn: "WARN ", info: "NOTE " };
function healthLine(i) {
  return `${HEALTH_SEV_TEXT[i.sev] || i.sev}  ${i.tag}: ${i.msg}`;
}

// The problems on show as text, under a line saying which lab and when
let healthShown = [];
function copyHealth() {
  const c = healthCounts(healthShown);
  const count = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const head = `${currentLabName()}: ${count(healthShown.length, "problem")} ` +
    `(${count(c.error, "error")}, ${count(c.warn, "warning")}) at ${new Date().toISOString().slice(0, 16).replace("T", " ")} UTC`;
  copyText([head, ...healthShown.map(healthLine)].join("\n"), count(healthShown.length, "problem"));
}

function renderHealth(items = healthItems()) {
  announceHealth(items);
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
  healthShown = currentLabName() ? shown : [];
  $("#health-copy").hidden = !healthShown.length;
  const list = $("#health-list");
  if (!currentLabName()) {
    list.replaceChildren(h("li", { class: "health-empty" }, "Select a lab to check it."));
  } else if (!shown.length) {
    list.replaceChildren(h("li", { class: "health-empty" },
      items.length ? "Nothing matches this filter." : `No problems found in ${currentLabName()}.`));
  } else {
    list.replaceChildren(...shown.map((i) => h("li", { class: "health-item" },
      h("button", { class: `health-row ${i.sev}`, onclick: i.go || null, disabled: !i.go },
        h("span", { class: `sev ${i.sev}`, "aria-label": { error: "Error", warn: "Warning", info: "Note" }[i.sev] }),
        h("span", { class: "tag" }, i.tag),
        h("span", { class: "msg" }, i.msg)),
      i.drift ? driftActions(i.drift) : null,
      h("button", {
        class: "icon-btn health-copy-one", type: "button", title: "Copy this line", "aria-label": `Copy: ${i.msg}`,
        onclick: () => copyText(healthLine(i), "the line"),
      }, s("svg", { class: "ico", "aria-hidden": "true" }, s("use", { href: "#i-copy" }))))));
  }
  return c;
}
