"use strict";

// ---------------------------------------------------------------------------
// Run a command on many nodes (feature 8): `clabfleet exec` in the GUI.
// A dock tab with node globs, a mode and a command; the outputs as a list,
// side by side, or as diffs against the first node's.
//
// Uses app.js globals: S, $, h, api, toast, topoPath, canOperate, announce.
// ---------------------------------------------------------------------------

(() => {

const HISTORY_KEY = "clab-run-history";
const HISTORY_MAX = 20;
const DIFF_MAX_LINES = 2000;  // longer outputs are compared as a whole
let last = null;              // the last {command, results}
let view = "list";

function history() {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY)) || []; } catch { return []; }
}

function remember(command) {
  const list = [command, ...history().filter((c) => c !== command)].slice(0, HISTORY_MAX);
  try { localStorage.setItem(HISTORY_KEY, JSON.stringify(list)); } catch { /* private mode */ }
  renderHistory();
}

function renderHistory() {
  $("#run-history").replaceChildren(...history().map((c) => h("option", { value: c })));
}

// Line diff of two outputs (longest common subsequence), unified-style
function lineDiff(a, b) {
  const x = a.split("\n"), y = b.split("\n");
  if (x.length > DIFF_MAX_LINES || y.length > DIFF_MAX_LINES) {
    return a === b ? [] : [{ op: " ", text: "(too long to compare line by line: the outputs differ)" }];
  }
  const n = x.length, m = y.length;
  const lcs = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = x[i] === y[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const out = [];
  let i = 0, j = 0;
  while (i < n || j < m) {
    if (i < n && j < m && x[i] === y[j]) { out.push({ op: " ", text: x[i] }); i++; j++; }
    else if (j < m && (i === n || lcs[i][j + 1] >= lcs[i + 1][j])) { out.push({ op: "+", text: y[j] }); j++; }
    else { out.push({ op: "-", text: x[i] }); i++; }
  }
  return out.some((l) => l.op !== " ") ? out : [];
}

function status(r) {
  if (r.error) return { cls: "error", text: r.error };
  if (r.exit_code === 0) return { cls: "ok", text: `${r.mode} · exit 0` };
  return { cls: "warn", text: `${r.mode} · exit ${r.exit_code}` };
}

function head(r) {
  const st = status(r);
  return h("div", { class: "run-head" },
    h("span", { class: `sev ${st.cls === "ok" ? "info ok" : st.cls}` }),
    h("b", {}, r.node), h("span", { class: "muted small" }, `${st.text}${r.host ? ` · ${r.host}` : ""}`));
}

function render() {
  const out = $("#run-out");
  for (const b of document.querySelectorAll("#run-view .seg-btn")) b.classList.toggle("active", b.dataset.view === view);
  if (!last) {
    out.replaceChildren(h("p", { class: "muted small run-hint" },
      "Runs one command on every node that matches, in parallel (the CLI on cEOS and SR Linux, SSH on VM kinds, else a shell), like clabfleet exec."));
    return;
  }
  const results = last.results;
  const errs = Object.entries(last.host_errors || {}).map(([hst, e]) => h("p", { class: "run-err small" }, `${hst}: ${e}`));
  if (view === "side") {
    out.replaceChildren(...errs, h("div", { class: "run-side" }, results.map((r) =>
      h("div", { class: "run-col" }, head(r), h("pre", { class: "mono" }, r.output || "")))));
  } else if (view === "diff") {
    const base = results.find((r) => !r.error);
    out.replaceChildren(...errs, ...results.map((r) => {
      if (!base) return h("div", { class: "run-block" }, head(r));
      if (r === base) return h("div", { class: "run-block" }, head(r), h("p", { class: "muted small" }, "The others are compared with this node."));
      if (r.error) return h("div", { class: "run-block" }, head(r));
      const d = lineDiff(base.output || "", r.output || "");
      return h("div", { class: "run-block" }, head(r), d.length
        ? h("pre", { class: "mono" }, d.map((l) => h("span", { class: l.op === "+" ? "ok" : l.op === "-" ? "err" : "" }, `${l.op} ${l.text}\n`)))
        : h("p", { class: "muted small" }, `Same as ${base.node}.`));
    }));
  } else {
    out.replaceChildren(...errs, ...results.map((r) =>
      h("div", { class: "run-block" }, head(r), r.output ? h("pre", { class: "mono" }, r.output) : null)));
  }
}

async function run(ev) {
  ev.preventDefault();
  if (S.selected?.type !== "topo") { toast("Open a lab first"); return; }
  const command = $("#run-cmd").value.trim();
  if (!command) return;
  const nodes = $("#run-nodes").value.split(/[\s,]+/).filter(Boolean);
  const btn = $("#run-go");
  btn.disabled = true;
  btn.textContent = "Running…";
  try {
    last = await api(`/api/exec/${topoPath(S.selected.id)}`, {
      method: "POST", body: JSON.stringify({ command, nodes, mode: $("#run-mode").value }),
    });
    remember(command);
    const bad = last.results.filter((r) => r.error || r.exit_code !== 0).length;
    announce(`Ran on ${last.results.length} nodes${bad ? `, ${bad} failed` : ""}`);
  } catch (e) {
    last = null;
    toast(`Not run: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Run";
  }
  render();
}

// Open the tab for a lab, with the selected node (or every node) filled in
function open(nodes) {
  activatePane("run", true);
  if (nodes) $("#run-nodes").value = nodes;
  else if (!$("#run-nodes").value) $("#run-nodes").value = S.selectedNode || "*";
  $("#run-cmd").focus();
}

function setup() {
  $("#run-form").addEventListener("submit", run);
  $("#run-view").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-view]");
    if (b) { view = b.dataset.view; render(); }
  });
  document.querySelector('.dock-tab[data-pane="run"]').addEventListener("click", () => open());
  renderHistory();
  render();
}

window.Run = { open, lineDiff, reset() { last = null; render(); } };
setup();
})();
