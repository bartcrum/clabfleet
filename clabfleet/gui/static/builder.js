"use strict";

// ---------------------------------------------------------------------------
// Topology builder: draw a lab on the Diagram tab (drag kinds from a palette,
// drag between nodes to link them, edit them in the inspector) and save it
// as YAML. The drawing is a draft until Save; the server then changes only
// what differs in the file (/api/graph/<id>), keeping comments and configs.
//
// Uses app.js globals: S, $, h, s, api, toast, confirmDialog, NODE_W, NODE_H,
// renderDiagram, renderNodeCard, renderLabHead, syncInspector, selectNode,
// selectLink, selectTopology, savePositions, loadEditor, topoPath, canOperate,
// runningJob, yamlDirty, showView, naturalCmp.
// ---------------------------------------------------------------------------

(() => {  // own scope: only window.Builder is shared

const B = {
  editing: false,
  info: null,           // /api/builder: kinds and templates
  draft: null,          // {nodes: [{name, kind, image, imageSet, from, config}], links: [{id, a, b}]}
  initial: "",          // the draft as loaded, to tell whether anything changed
  view: null,           // the draft in the shape the diagram draws (cached)
  linkSeq: 0,
  linking: null,        // {from, line} while a link is being drawn
};

const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$/;
const KIND_LABEL = { arista_ceos: "Arista cEOS", cisco_iol: "Cisco IOL", linux: "Linux host" };

async function builderInfo() {
  if (!B.info) B.info = await api("/api/builder");
  return B.info;
}

// --- ports: eth1, eth2 ... or IOL's Ethernet0/1 ... -------------------------------

function portName(kind, n) {
  return kind === "cisco_iol" ? `Ethernet${Math.floor(n / 4)}/${n % 4}` : `eth${n}`;
}

function portIndex(iface) {
  let m = /^Ethernet(\d+)\/(\d+)$/.exec(iface);
  if (m) return Number(m[1]) * 4 + Number(m[2]);
  m = /(\d+)$/.exec(iface);
  return m ? Number(m[1]) : null;
}

// Ports a node uses: in drawn links and in links the builder leaves alone
function usedPorts(name) {
  const used = new Set();
  for (const l of B.draft.links) for (const e of [l.a, l.b]) if (e.node === name) used.add(e.iface);
  for (const l of specialLinks()) for (const e of [l.a, l.b]) if (e.node === name) used.add(e.iface);
  return used;
}

function nextPort(name) {
  const kind = draftNode(name)?.kind;
  const used = usedPorts(name);
  for (let n = 1; ; n++) if (!used.has(portName(kind, n))) return portName(kind, n);
}

// --- the draft ---------------------------------------------------------------------

// Links of other forms (host:, macvlan, ...): drawn, kept, not edited
function specialLinks() {
  return (S.detail?.links || []).filter((l) => !l.a.node || !l.b.node || l.type !== "veth");
}

function draftNode(name) {
  return B.draft?.nodes.find((n) => n.name === name);
}

function startDraft() {
  B.draft = {
    nodes: S.detail.nodes.map((n) => ({ name: n.name, kind: n.kind, image: n.image, imageSet: false,
                                        from: n.name, hadConfig: !!n.config })),
    links: S.detail.links.filter((l) => l.a.node && l.b.node && l.type === "veth")
      .map((l) => ({ id: `b${++B.linkSeq}`, a: { ...l.a }, b: { ...l.b } })),
  };
  B.initial = draftKey();
  changed(false);
}

function draftKey() {
  return JSON.stringify({ n: B.draft.nodes.map(({ name, kind, image, config }) => [name, kind, image, !!config]),
                          l: B.draft.links.map((l) => [l.a, l.b]) });
}

// Call after every change to the draft
function changed(render = true) {
  const kinds = B.info?.kinds || {};
  B.view = {
    ...S.detail,
    nodes: B.draft.nodes.map((n) => ({
      name: n.name, kind: n.kind, image: n.image || kinds[n.kind]?.image || "",
      type: "", group: "", host_pin: "", host_tags: "", pos: null, modes: [],
    })),
    links: [
      ...B.draft.links.map((l) => ({ id: l.id, type: "veth", a: l.a, b: l.b })),
      ...specialLinks().filter((l) => [l.a, l.b].every((e) => !e.node || draftNode(e.node))),
    ],
  };
  renderBar();
  if (render) { renderDiagram(false); renderNodeCard(); }
}

function isDirty() {
  return B.editing && !!B.draft && draftKey() !== B.initial;
}

// --- edit mode ---------------------------------------------------------------------

async function startEditing() {
  if (S.selected?.type !== "topo" || !S.detail || S.detail.error) return;
  if (yamlDirty()) { toast("Save or revert your YAML changes first"); return; }
  if (runningJob(S.selected.id)) { toast("A job is running for this lab; edit when it finishes"); return; }
  try { await builderInfo(); } catch (e) { toast(`Builder: ${e.message}`); return; }
  B.editing = true;
  startDraft();
  document.body.classList.add("building");
  $("#builder-bar").hidden = false;
  $("#builder-palette").hidden = false;
  $("#edit-topo").hidden = true;  // the edit bar has Save and Discard
  renderPalette();
  S.selectedLink = null;
  showView("diagram");
}

function stopEditing() {
  B.editing = false;
  B.draft = null;
  B.view = null;
  document.body.classList.remove("building");
  $("#builder-bar").hidden = true;
  $("#builder-palette").hidden = true;
  $("#edit-topo").hidden = false;
  S.selectedLink = null;
  if (S.selectedNode && !S.detail?.nodes?.some((n) => n.name === S.selectedNode)) S.selectedNode = null;
  renderDiagram(false);
  renderNodeCard();
}

async function confirmLeave() {
  if (!isDirty()) return true;
  const ok = await confirmDialog({
    title: "Discard the drawing?",
    body: "Your changes on the Diagram tab have not been saved.",
    ok: "Discard", danger: true,
  });
  if (ok) stopEditing();
  return ok;
}

function renderBar() {
  const n = B.draft.nodes.length, l = B.draft.links.length;
  $("#builder-status").textContent = `Editing ${S.detail.name} · ${n} node${n === 1 ? "" : "s"}, ` +
    `${l} link${l === 1 ? "" : "s"}${isDirty() ? " · unsaved changes" : ""}`;
  $("#builder-save").disabled = !isDirty();
}

function renderPalette() {
  $("#palette-items").replaceChildren(...Object.entries(B.info.kinds).map(([kind, k]) =>
    h("div", {
      class: "palette-item", "data-kind": kind, tabindex: "0", role: "button",
      title: `${KIND_LABEL[kind] || kind} (${k.image}). Drag onto the canvas, or press Enter to add one.`,
      onpointerdown: (ev) => startPaletteDrag(ev, kind),
      onkeydown: (ev) => { if (ev.key === "Enter") addNode(kind, null); },
    },
      h("span", { class: `palette-glyph ${kind === "linux" ? "host" : "router"}`, "aria-hidden": "true" }),
      h("span", {}, h("b", {}, KIND_LABEL[kind] || kind), h("span", { class: "muted mono" }, k.image)))));
}

// --- adding, linking, removing --------------------------------------------------------

function uniqueName(kind) {
  const base = kind === "linux" ? "Host-" : "R";
  for (let i = 1; ; i++) if (!draftNode(`${base}${i}`)) return `${base}${i}`;
}

function addNode(kind, pos) {
  const name = uniqueName(kind);
  if (!pos) {  // keyboard: below everything
    const ys = Object.values(S.positions).map((p) => p[1]);
    pos = [0, (ys.length ? Math.max(...ys) : 0) + 160];
  }
  B.draft.nodes.push({ name, kind, image: "", imageSet: false, from: null });
  S.positions[name] = pos;
  savePositions();
  selectNode(name);
  changed();
  ensureVisible(name);
}

// Pan so a node is clear of the canvas edges, the palette and the edit bar
// (the inspector opening for it narrows the canvas)
function ensureVisible(name) {
  const p = S.positions[name], svg = $("#diagram");
  if (!p) return;
  const w = svg.clientWidth, hgt = svg.clientHeight, k = S.view.k;
  const sx = p[0] * k + S.view.x, sy = p[1] * k + S.view.y;
  const mx = (NODE_W / 2 + 16) * k, my = (NODE_H / 2 + 16) * k;
  const left = 230, top = 60;  // palette and bar
  const dx = sx + mx > w ? w - mx - sx : sx - mx < left ? left + mx - sx : 0;
  const dy = sy + my > hgt ? hgt - my - sy : sy - my < top ? top + my - sy : 0;
  if (dx || dy) {
    S.view.x += dx;
    S.view.y += dy;
    renderDiagram(false);
  }
}

function addLink(a, b) {
  if (a === b) return;
  B.draft.links.push({ id: `b${++B.linkSeq}`, a: { node: a, iface: nextPort(a) }, b: { node: b, iface: "" } });
  const l = B.draft.links[B.draft.links.length - 1];
  l.b.iface = nextPort(b);
  S.selectedNode = null;
  selectLink(l.id);
  changed();
}

function removeNode(name) {
  B.draft.nodes = B.draft.nodes.filter((n) => n.name !== name);
  B.draft.links = B.draft.links.filter((l) => l.a.node !== name && l.b.node !== name);
  if (S.selectedNode === name) S.selectedNode = null;
  changed();
}

function removeLink(id) {
  B.draft.links = B.draft.links.filter((l) => l.id !== id);
  S.selectedLink = null;
  changed();
}

function renameNode(node, name) {
  if (name === node.name) return true;
  if (!NAME_RE.test(name)) { toast("Names are letters, digits, '-' and '_'"); return false; }
  if (draftNode(name)) { toast(`${name} exists already`); return false; }
  const old = node.name;
  node.name = name;
  for (const l of B.draft.links) for (const e of [l.a, l.b]) if (e.node === old) e.node = name;
  if (S.positions[old]) { S.positions[name] = S.positions[old]; delete S.positions[old]; savePositions(); }
  S.selectedNode = name;
  changed();
  return true;
}

// A new kind renames the node's ports the new kind's way (eth3 -> Ethernet0/3)
function setKind(node, kind) {
  for (const l of B.draft.links) {
    for (const e of [l.a, l.b]) {
      if (e.node !== node.name) continue;
      const n = portIndex(e.iface);
      if (n != null) e.iface = portName(kind, n);
    }
  }
  node.kind = kind;
  if (!node.imageSet) node.image = "";
  changed();
}

// --- inspector forms ---------------------------------------------------------------

function field(label, input) {
  return h("label", { class: "bfield" }, h("span", {}, label), input);
}

function renderInspector() {
  if (!B.editing) return false;
  const card = $("#node-card");
  $("#link-card").hidden = true;
  const node = S.selectedNode && draftNode(S.selectedNode);
  const link = S.selectedLink && B.draft.links.find((l) => l.id === S.selectedLink.id);
  if (!node && !link) { card.hidden = true; return true; }
  const kinds = B.info.kinds;
  if (node) {
    const nameIn = h("input", { class: "mono", value: node.name, spellcheck: "false", autocomplete: "off" });
    nameIn.addEventListener("change", () => { if (!renameNode(node, nameIn.value.trim())) nameIn.value = node.name; });
    const kindSel = h("select", {}, [...new Set([...Object.keys(kinds), node.kind])].map((k) =>
      h("option", { value: k, selected: k === node.kind }, KIND_LABEL[k] || k)));
    kindSel.addEventListener("change", () => setKind(node, kindSel.value));
    const imgIn = h("input", { class: "mono", value: node.imageSet ? node.image : "", spellcheck: "false",
                               placeholder: kinds[node.kind]?.image || node.image || "image", autocomplete: "off" });
    imgIn.addEventListener("change", () => {
      node.image = imgIn.value.trim();
      node.imageSet = !!node.image;
      changed();
    });
    const ports = B.draft.links.flatMap((l) => [[l.a, l.b, l], [l.b, l.a, l]])
      .filter(([me]) => me.node === node.name).sort((x, y) => naturalCmp(x[0].iface, y[0].iface));
    card.replaceChildren(
      h("h3", {}, node.name, h("button", { class: "close", title: "Close", "aria-label": "Close", onclick: () => selectNode(null) }, "×")),
      h("div", { class: "bform" },
        field("Name", nameIn), field("Kind", kindSel), field("Image", imgIn)),
      node.from ? null : h("p", { class: "muted small" }, "New: not in the file until you save."),
      h("p", { class: "muted small" }, node.config ? "Config: generated, written when you save."
        : node.hadConfig ? "Config: the one in the file." : "Config: none (the kind's defaults)."),
      ports.length ? h("h4", {}, "Links") : null,
      ports.length ? h("table", { class: "rt-table" }, h("tbody", {}, ports.map(([me, other, l]) =>
        h("tr", { class: "go", onclick: () => { S.selectedNode = null; selectLink(l.id); } },
          h("td", {}, me.iface), h("td", {}, `${other.node}:${other.iface}`))))) : null,
      h("h4", {}, "Actions"),
      h("span", { class: "open" }, h("button", { class: "btn small danger", onclick: () => removeNode(node.name) }, "Remove node")));
  } else {
    const end = (e) => {
      const input = h("input", { class: "mono", value: e.iface, spellcheck: "false", autocomplete: "off" });
      input.addEventListener("change", () => {
        const v = input.value.trim();
        if (!v || (v !== e.iface && usedPorts(e.node).has(v))) { toast(`${e.node} uses ${v || "an empty port"} already`); input.value = e.iface; return; }
        e.iface = v;
        changed();
      });
      return field(e.node, input);
    };
    card.replaceChildren(
      h("h3", {}, h("span", { class: "mono" }, `${link.a.node} ↔ ${link.b.node}`),
        h("button", { class: "close", title: "Close", "aria-label": "Close", onclick: () => selectLink(null) }, "×")),
      h("div", { class: "bform" }, end(link.a), end(link.b)),
      h("p", { class: "muted small" }, "Ports are numbered automatically; change them here if you need to."),
      h("h4", {}, "Actions"),
      h("span", { class: "open" }, h("button", { class: "btn small danger", onclick: () => removeLink(link.id) }, "Remove link")));
  }
  card.hidden = false;
  return true;
}

// --- pointer interactions on the canvas ------------------------------------------------

function svgPoint(ev) {
  const r = $("#diagram").getBoundingClientRect();
  return [(ev.clientX - r.left - S.view.x) / S.view.k, (ev.clientY - r.top - S.view.y) / S.view.k];
}

function nodeAt(ev) {
  return document.elementFromPoint(ev.clientX, ev.clientY)?.closest?.("#diagram .node")?.dataset.id || null;
}

// The ● handle on each node in edit mode: drag from it to another node to link
function decorateNode(el, id) {
  if (!B.editing) return;
  el.append(s("circle", { class: "link-handle", cx: NODE_W / 2, cy: 0, r: 6, "data-handle": id },
    s("title", {}, `Drag to another node to link ${id}`)));
}

function setupCanvas() {
  const svg = $("#diagram");
  // Capture phase: runs before the diagram's own drag / pan handling
  svg.addEventListener("pointerdown", (ev) => {
    const from = B.editing && ev.target.closest?.("[data-handle]")?.dataset.handle;
    if (!from) return;
    ev.stopImmediatePropagation();
    ev.preventDefault();
    svg.setPointerCapture(ev.pointerId);
    const [x, y] = S.positions[from];
    const line = s("line", { class: "link-draft", x1: x + NODE_W / 2, y1: y, x2: x + NODE_W / 2, y2: y });
    $("#viewport")?.append(line);
    B.linking = { from, line };
  }, true);
  svg.addEventListener("pointermove", (ev) => {
    if (!B.linking) return;
    const [x, y] = svgPoint(ev);
    B.linking.line.setAttribute("x2", x);
    B.linking.line.setAttribute("y2", y);
    const over = nodeAt(ev);
    for (const n of svg.querySelectorAll(".node.link-target")) n.classList.remove("link-target");
    if (over && over !== B.linking.from) svg.querySelector(`.node[data-id="${CSS.escape(over)}"]`)?.classList.add("link-target");
  });
  svg.addEventListener("pointerup", (ev) => {
    if (!B.linking) return;
    const { from, line } = B.linking;
    B.linking = null;
    line.remove();
    const to = nodeAt(ev);
    if (to && to !== from) addLink(from, to);
    else renderDiagram(false);
  });
  document.addEventListener("keydown", (ev) => {
    if (!B.editing || !["Delete", "Backspace"].includes(ev.key)) return;
    if (ev.target.closest?.("input, textarea, select, dialog")) return;
    if (S.selectedNode && draftNode(S.selectedNode)) { ev.preventDefault(); removeNode(S.selectedNode); }
    else if (S.selectedLink) { ev.preventDefault(); removeLink(S.selectedLink.id); }
  });
}

// Palette items are dragged with the pointer (not HTML drag and drop, which
// does not reach SVG well); a ghost follows until it is dropped on the canvas
function startPaletteDrag(ev, kind) {
  ev.preventDefault();
  const ghost = h("div", { class: "palette-ghost" }, KIND_LABEL[kind] || kind);
  document.body.append(ghost);
  const move = (e) => { ghost.style.left = `${e.clientX + 8}px`; ghost.style.top = `${e.clientY + 8}px`; };
  move(ev);
  const up = (e) => {
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", up);
    ghost.remove();
    const r = $("#diagram").getBoundingClientRect();
    if (e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom) {
      addNode(kind, svgPoint(e));
    }
  };
  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", up);
}

// --- saving -------------------------------------------------------------------------

function graphBody() {
  return {
    nodes: B.draft.nodes.map((n) => ({
      name: n.name, kind: n.kind,
      ...(n.imageSet ? { image: n.image } : {}),
      ...(n.from && n.from !== n.name ? { rename_from: n.from } : {}),
      ...(S.positions[n.name] ? { pos: S.positions[n.name] } : {}),
      ...(n.config ? { config: n.config } : {}),
    })),
    links: B.draft.links.map((l) => ({ a: `${l.a.node}:${l.a.iface}`, b: `${l.b.node}:${l.b.iface}` })),
  };
}

async function sendGraph(dryRun) {
  const res = await fetch(`/api/graph/${topoPath(S.selected.id)}`, {
    method: "PUT", credentials: "same-origin", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ graph: graphBody(), base_hash: S.detail.hash, dry_run: dryRun }),
  });
  if (res.status === 400 && res.headers.get("Content-Type")?.includes("json")) {
    const { validation } = await res.json();
    throw new Error(`Not a valid topology: ${(validation?.errors || []).join("; ")}`);
  }
  if (!res.ok) throw new Error((await res.text()) || res.statusText);
  return res.json();
}

async function save() {
  $("#builder-save").disabled = true;
  try {
    const { detail, validation } = await sendGraph(false);
    S.detail = detail;
    loadEditor();
    stopEditing();
    renderLabHead();
    window.Routing?.changed();
    toast(validation.errors.length ? `Saved with ${validation.errors.length} problem(s): see Health`
      : "Saved to the topology file");
  } catch (e) {
    toast(`Not saved: ${e.message}`);
    renderBar();
  }
}

async function preview() {
  try {
    const { yaml, validation } = await sendGraph(true);
    const v = validation;
    $("#preview-summary").textContent = !v.loadable ? `Not a loadable topology: ${v.errors.join("; ")}`
      : v.errors.length ? `${v.errors.length} problem(s): ${v.errors.join("; ")}`
      : v.warnings.length ? `Valid, ${v.warnings.length} warning(s): ${v.warnings.join("; ")}` : "Valid topology.";
    $("#preview-summary").className = `small${v.errors.length ? " bad" : ""}`;
    $("#preview-yaml").textContent = yaml;
    $("#preview-dlg").showModal();
  } catch (e) {
    toast(e.message);
  }
}

// --- generated configs -----------------------------------------------------------------

function openGenerate() {
  const kinds = B.info.kinds;
  const replaced = B.draft.nodes.filter((n) => kinds[n.kind] && (n.hadConfig || n.config)).map((n) => n.name);
  const skipped = B.draft.nodes.filter((n) => !kinds[n.kind]).map((n) => `${n.name} (${n.kind})`);
  const warn = $("#gen-warn");
  warn.textContent = [
    replaced.length ? `Replaces the configs of ${replaced.join(", ")}.` : "",
    skipped.length ? `Leaves out ${skipped.join(", ")}: no config generator for that kind.` : "",
  ].filter(Boolean).join(" ");
  warn.hidden = !warn.textContent;
  $("#gen-error").hidden = true;
  $("#gen-asn-row").hidden = $("#gen-routing").value !== "bgp";
  $("#gen-dlg").showModal();
}

async function generate(ev) {
  ev.preventDefault();
  if (ev.submitter?.value === "cancel") { $("#gen-dlg").close(); return; }
  try {
    const { configs, skipped } = await api("/api/builder/configs", {
      method: "POST",
      body: JSON.stringify({
        nodes: Object.fromEntries(B.draft.nodes.map((n) => [n.name, n.kind])),
        links: B.draft.links.map((l) => [l.a.node, l.a.iface, l.b.node, l.b.iface]),
        routing: $("#gen-routing").value, link_subnet: $("#gen-links").value.trim(),
        loopback_subnet: $("#gen-loops").value.trim(), asn: Number($("#gen-asn").value),
      }),
    });
    for (const n of B.draft.nodes) if (configs[n.name]) n.config = configs[n.name];
    $("#gen-dlg").close();
    changed();
    const n = Object.keys(configs).length;
    toast(`Configs generated for ${n} node${n === 1 ? "" : "s"}${skipped.length ? ` (not ${skipped.join(", ")})` : ""}. ` +
      "Preview them, then Save.");
  } catch (e) {
    $("#gen-error").textContent = e.message;
    $("#gen-error").hidden = false;
  }
}

// --- new lab ------------------------------------------------------------------------

async function openNewLab() {
  try { await builderInfo(); } catch (e) { toast(e.message); return; }
  if (!(await window.Builder.confirmLeave())) return;
  const dlg = $("#newlab-dlg");
  $("#newlab-template").replaceChildren(h("option", { value: "" }, "A blank canvas (one node)"),
    ...Object.entries(B.info.templates).map(([k, t]) => h("option", { value: k }, `Template: ${t.description}`)));
  $("#newlab-kind").replaceChildren(...Object.keys(B.info.kinds).map((k) => h("option", { value: k }, KIND_LABEL[k] || k)));
  $("#newlab-name").value = "";
  $("#newlab-file").value = "";
  $("#newlab-file").dataset.auto = "1";
  $("#newlab-error").hidden = true;
  renderNewLabParams();
  dlg.showModal();
  $("#newlab-name").focus();
}

function renderNewLabParams() {
  const t = B.info.templates[$("#newlab-template").value];
  $("#newlab-params").replaceChildren(...(t ? t.params.map((p) => field(p.help,
    h("input", { type: "number", "data-param": p.name, value: p.default, min: p.min, max: p.max }))) : []));
  const kind = $("#newlab-kind").value;
  $("#newlab-hint").textContent = t
    ? "Generated with addresses and routing configs, like `clabfleet new`. Edit it afterwards on the Diagram tab."
    : "Starts with one node; add more on the Diagram tab, which opens in edit mode.";
  if (kind && B.info.kinds[kind]) $("#newlab-hint").textContent += ` ${B.info.kinds[kind].note}`;
}

async function createLab(ev) {
  ev.preventDefault();
  if (ev.submitter?.value === "cancel") { $("#newlab-dlg").close(); return; }
  const template = $("#newlab-template").value;
  const params = {};
  for (const input of $("#newlab-params").querySelectorAll("[data-param]")) params[input.dataset.param] = Number(input.value);
  const err = $("#newlab-error");
  try {
    const { id } = await api("/api/topologies", {
      method: "POST",
      body: JSON.stringify({ file: $("#newlab-file").value.trim(), name: $("#newlab-name").value.trim(),
                             kind: $("#newlab-kind").value, template: template || null, params }),
    });
    $("#newlab-dlg").close();
    await refreshState();
    await selectTopology(id);
    showView("diagram");
    if (!template) startEditing();
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  }
}

function builderSetup() {
  setupCanvas();
  $("#edit-topo").addEventListener("click", startEditing);
  $("#builder-save").addEventListener("click", save);
  $("#builder-preview").addEventListener("click", preview);
  $("#builder-generate").addEventListener("click", openGenerate);
  $("#gen-form").addEventListener("submit", generate);
  $("#gen-routing").addEventListener("change", () => { $("#gen-asn-row").hidden = $("#gen-routing").value !== "bgp"; });
  $("#builder-discard").addEventListener("click", async () => { if (await confirmLeave()) stopEditing(); });
  $("#new-lab").addEventListener("click", openNewLab);
  $("#newlab-form").addEventListener("submit", createLab);
  $("#newlab-template").addEventListener("change", renderNewLabParams);
  $("#newlab-kind").addEventListener("change", renderNewLabParams);
  $("#newlab-name").addEventListener("input", () => {
    const file = $("#newlab-file");
    if (file.dataset.auto) file.value = $("#newlab-name").value.trim() ? `${$("#newlab-name").value.trim()}.clab.yml` : "";
  });
  $("#newlab-file").addEventListener("input", () => { delete $("#newlab-file").dataset.auto; });
}

window.Builder = {
  get editing() { return B.editing; },
  detail() { return B.editing ? B.view : null; },
  dirty: isDirty,
  confirmLeave,
  renderInspector,
  decorateNode,
  // Another topology was selected (after confirmLeave)
  reset() { if (B.editing) stopEditing(); },
};

builderSetup();
})();
