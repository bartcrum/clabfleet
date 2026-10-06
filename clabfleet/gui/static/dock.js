"use strict";

// ---------------------------------------------------------------------------
// Dock and terminals: the panes at the bottom, xterm tabs on a session
// websocket (terminal, log, live capture) and the config diff tab.
//
// Uses app.js globals: S, $, h, api, MODE_LABEL, nodeRuntime, canOperate,
// topoPath, renderDiagram; and from health.js: renderHealth, refreshEvents,
// healthFilter.
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

// Open the first terminal a node offers, if it runs and the user may;
// says whether it did
function openDefaultTerminal(id) {
  const node = S.detail?.nodes?.find((n) => n.name === id);
  const rt = node && nodeRuntime(S.detail.name, id);
  if (rt?.state !== "running" || !node.modes.length || !canOperate()) return false;
  openTerminal(S.detail.name, id, node.modes[0]);
  return true;
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
function openDiff(topoId, node, against = "startup") {
  const id = `diff-${++termSeq}`;
  const select = h("select", { class: "small", "aria-label": "Compare" },
    h("option", { value: "startup" }, "latest snapshot vs startup-config"),
    h("option", { value: "previous" }, "latest snapshot vs previous snapshot"),
    h("option", { value: "running" }, "running config now vs its startup config"));
  select.value = against;
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
      if (!d.diff) {
        pre.textContent = select.value === "running" ? "No drift: the running config matches the startup config."
          : "No differences.";
        return;
      }
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
  document.querySelector('.dock-tab[data-pane="events"]').addEventListener("click", () => { activatePane("events"); refreshEvents(); });
  $("#health-source").addEventListener("change", (ev) => { healthFilter.source = ev.target.value; renderHealth(); });
  $("#health-copy").addEventListener("click", copyHealth);
  $("#events-copy").addEventListener("click", copyEvents);
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
