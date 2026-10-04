"use strict";

// ---------------------------------------------------------------------------
// Command palette (Ctrl+K / Cmd+K): jump to a lab, tab or node, open a
// node's terminal or config diff, run lab actions, switch theme, ...
// Commands are built fresh each time it opens, from what can be done now.
//
// Uses app.js globals: S, $, h, showView, selectTopology, selectNode,
// runAction, applyTheme, THEMES, canOperate, runningJob, labStatus,
// currentLabName, nodeRuntime, kindName, MODE_LABEL, refreshState,
// refreshHosts; and from health.js: openHealth; from dock.js: openTerminal,
// openDiff, activatePane; from account.js: logout.
// ---------------------------------------------------------------------------

(() => {

const MAX_SHOWN = 60;
let commands = [];
let shown = [];
let active = 0;

function buildCommands() {
  const out = [];
  const add = (group, label, run, hint = "") => out.push({ group, label, run, hint });
  const op = canOperate();

  for (const t of S.state?.topologies || []) add("Labs", `Open ${t.name}`, () => selectTopology(t.id), t.id);
  const lab = currentLabName();
  const topo = S.selected?.type === "topo" && S.detail && !S.detail.error;

  if (lab) {
    for (const [view, label] of [["diagram", "Diagram"], ["nodes", "Nodes"], ["routing", "Routing"], ["yaml", "YAML"]]) {
      if (!document.querySelector(`.tab[data-view="${view}"]`)?.hidden) add("Go to", `${label} tab`, () => showView(view));
    }
    add("Go to", "Health panel", () => openHealth());
    add("Go to", "Activity panel", () => activatePane("activity", true));
  }

  if (topo) {
    const st = labStatus(S.detail.name);
    const busy = !!runningJob(S.selected.id);
    if (op && !busy) {
      const acts = st.deployed
        ? [["redeploy", "Redeploy"], ["save", "Save configs"], ["snapshot", "Snapshot"], ["stop", "Stop"], ["destroy", "Destroy lab…"]]
        : [["deploy", "Deploy"]];
      for (const [a, label] of acts) add("Lab", `${label} ${S.detail.name}`, () => runAction(a));
    }
    if (op && window.Builder && !window.Builder.editing) add("Lab", "Edit the drawing", () => { showView("diagram"); $("#edit-topo").click(); });
    if (op && st.deployed && window.Run) add("Lab", "Run a command on nodes…", () => window.Run.open());
    if (st.deployed && window.Trace) add("Lab", "Trace a path…", () => window.Trace.open(S.selectedNode || undefined));
    for (const n of S.detail.nodes) {
      const rt = nodeRuntime(S.detail.name, n.name);
      const running = rt?.state === "running";
      add("Nodes", n.name, () => { showView("diagram"); selectNode(n.name); }, kindName(n.kind));
      if (op && running) {
        for (const m of n.modes) add("Terminals", `${MODE_LABEL[m]} on ${n.name}`, () => openTerminal(S.detail.name, n.name, m));
      }
      if (op && rt) add("Terminals", `Logs of ${n.name}`, () => openTerminal(S.detail.name, n.name, "logs"));
      if (running && window.Trace) add("Trace", `Trace a path from ${n.name}…`, () => window.Trace.open(n.name));
      if (op && running) add("Configs", `Config drift of ${n.name}`, () => openDiff(S.selected.id, n.name, "running"),
        "running vs startup");
    }
  }

  if (op && window.Builder) add("Lab", "New lab…", () => window.Builder.newLab());
  for (const [theme, label] of Object.entries(THEMES)) add("Theme", `Theme: ${label}`, () => {
    try { localStorage.setItem("clab-theme", theme); } catch { /* private mode */ }
    applyTheme(theme);
  });
  if (S.me?.can_manage_users) add("Settings", "Users…", () => $("#users").click());
  if (S.me?.has_password) add("Settings", "Change password…", () => $("#password").click());
  add("Settings", "Refresh now", () => { refreshState(); refreshHosts(); });
  add("Settings", "Log out", () => logout());
  return out;
}

// Every word must appear; labels starting with the words come first
function match(query) {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return commands;
  const scored = [];
  for (const c of commands) {
    const text = `${c.label} ${c.hint}`.toLowerCase();
    if (!words.every((w) => text.includes(w))) continue;
    const label = c.label.toLowerCase();
    const score = (label.startsWith(words[0]) ? 0 : label.includes(` ${words[0]}`) ? 1 : 2) + label.length / 1000;
    scored.push([score, c]);
  }
  return scored.sort((a, b) => a[0] - b[0]).map(([, c]) => c);
}

function render() {
  const list = $("#palette-list");
  shown = match($("#palette-input").value).slice(0, MAX_SHOWN);
  active = Math.min(active, Math.max(0, shown.length - 1));
  let group = null;
  const items = [];
  shown.forEach((c, i) => {
    if (c.group !== group) {
      group = c.group;
      items.push(h("li", { class: "pal-group", role: "presentation" }, group));
    }
    items.push(h("li", {
      id: `pal-${i}`, role: "option", class: `pal-item${i === active ? " active" : ""}`,
      "aria-selected": i === active ? "true" : "false",
      onpointerdown: (ev) => { ev.preventDefault(); run(i); },
      onpointermove: () => { if (active !== i) { active = i; render(); } },
    }, h("span", {}, c.label), c.hint ? h("span", { class: "muted mono small" }, c.hint) : null));
  });
  if (!shown.length) items.push(h("li", { class: "pal-empty muted" }, "Nothing matches."));
  list.replaceChildren(...items);
  const input = $("#palette-input");
  if (shown.length) input.setAttribute("aria-activedescendant", `pal-${active}`);
  else input.removeAttribute("aria-activedescendant");
  $(`#pal-${active}`)?.scrollIntoView({ block: "nearest" });
}

function open() {
  const dlg = $("#palette");
  if (dlg.open) return;
  commands = buildCommands();
  active = 0;
  $("#palette-input").value = "";
  render();
  dlg.showModal();
  $("#palette-input").focus();
}

function run(i) {
  const c = shown[i];
  $("#palette").close();
  if (c) c.run();
}

function setup() {
  document.addEventListener("keydown", (ev) => {
    if (!(ev.ctrlKey || ev.metaKey) || ev.key.toLowerCase() !== "k" || ev.altKey) return;
    if (ev.target.closest?.(".xterm")) return;  // Ctrl+K belongs to the shell there
    if (!$("#login").hidden || !$("#pwchange").hidden) return;
    ev.preventDefault();
    open();
  });
  $("#palette-input").addEventListener("input", () => { active = 0; render(); });
  $("#palette-input").addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown") { ev.preventDefault(); active = Math.min(active + 1, shown.length - 1); render(); }
    else if (ev.key === "ArrowUp") { ev.preventDefault(); active = Math.max(active - 1, 0); render(); }
    else if (ev.key === "Enter") { ev.preventDefault(); run(active); }
  });
  $("#palette").addEventListener("click", (ev) => { if (ev.target === $("#palette")) $("#palette").close(); });
  $("#palette-open").addEventListener("click", open);
}

window.Palette = { open };
setup();
})();
