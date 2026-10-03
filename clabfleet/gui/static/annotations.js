"use strict";

// ---------------------------------------------------------------------------
// Diagram annotations (feature 12): sticky notes and labelled boxes ("DC1",
// "tenant A") on the Diagram, kept next to the topology file
// (<file>.notes.json). Everyone sees them; operators add them with Note and
// Box, drag them, drag a box's corner to resize it, double-click to edit,
// and Delete to remove the selected one.
//
// Uses app.js globals: S, $, h, s, api, toast, topoPath, canOperate,
// renderDiagram, announce.
// ---------------------------------------------------------------------------

(() => {

const CHAR_W = 7.2, LINE_H = 16, NOTE_PAD = 8, WRAP = 32;
const PALETTE = 8;
let sel = null;          // id of the selected annotation
let drag = null;
let saveTimer = null;
let lastClick = { id: null, t: 0 };

function data() {
  if (!S.detail || S.detail.error) return null;
  S.detail.annotations ||= { notes: [], boxes: [] };
  return S.detail.annotations;
}

function find(id) {
  const d = data();
  return d && (d.notes.find((n) => n.id === id) || d.boxes.find((b) => b.id === id));
}

function newId() {
  return Math.random().toString(36).slice(2, 10);
}

// Lines of a note, wrapped at about WRAP characters
function wrap(text) {
  const out = [];
  for (const para of (text || " ").split("\n")) {
    let line = "";
    for (const word of para.split(" ")) {
      if (line && (line + " " + word).length > WRAP) { out.push(line); line = word; }
      else line = line ? `${line} ${word}` : word;
    }
    out.push(line);
  }
  return out;
}

function save() {
  clearTimeout(saveTimer);
  const id = S.selected?.id;
  saveTimer = setTimeout(async () => {
    const d = data();
    if (!d || S.selected?.id !== id) return;
    try {
      S.detail.annotations = await api(`/api/annotations/${topoPath(id)}`, { method: "PUT", body: JSON.stringify(d) });
    } catch (e) {
      toast(`Notes not saved: ${e.message}`);
    }
  }, 300);
}

// Called by renderDiagram: boxes behind the links, notes on top
function decorate(g) {
  const d = data();
  const op = canOperate();
  for (const b of document.querySelectorAll(".diagram-tools .anno-tool")) b.hidden = !op || !d;
  if (!d) return;
  const first = g.firstChild;
  for (const b of d.boxes) {
    const picked = op && sel === b.id;
    g.insertBefore(s("g", { class: `anno anno-box${picked ? " selected" : ""}`, "data-anno": b.id,
                            style: `--c: var(--c${b.color % PALETTE})` },
      s("rect", { class: "anno-body", x: b.x, y: b.y, width: b.w, height: b.h, rx: 12 }),
      s("text", { x: b.x + 12, y: b.y + 20 }, b.label),
      picked ? s("rect", { class: "anno-handle", "data-resize": "1", x: b.x + b.w - 7, y: b.y + b.h - 7, width: 14, height: 14, rx: 3 }) : null,
      s("title", {}, b.label || "Box")), first);
  }
  for (const n of d.notes) {
    const lines = wrap(n.text);
    const w = Math.max(...lines.map((l) => l.length)) * CHAR_W + NOTE_PAD * 2;
    const hgt = lines.length * LINE_H + NOTE_PAD * 2 - 4;
    g.append(s("g", { class: `anno anno-note${op && sel === n.id ? " selected" : ""}`, "data-anno": n.id },
      s("rect", { class: "anno-body", x: n.x, y: n.y, width: w, height: hgt, rx: 4 }),
      ...lines.map((l, i) => s("text", { x: n.x + NOTE_PAD, y: n.y + NOTE_PAD + 11 + i * LINE_H }, l))));
  }
}

// --- adding and editing ----------------------------------------------------

function center() {
  const svg = $("#diagram");
  const k = S.view.k || 1;
  return [((svg.clientWidth || 800) / 2 - S.view.x) / k, ((svg.clientHeight || 500) / 2 - S.view.y) / k];
}

async function add(kind) {
  const d = data();
  if (!d) return;
  const [x, y] = center();
  const item = kind === "note"
    ? { id: newId(), x: Math.round(x - 60), y: Math.round(y - 20), text: "" }
    : { id: newId(), x: Math.round(x - 160), y: Math.round(y - 100), w: 320, h: 200, label: "", color: d.boxes.length % PALETTE };
  const text = await edit(item, kind);
  if (text === null) return;
  (kind === "note" ? d.notes : d.boxes).push(item);
  sel = item.id;
  renderDiagram(false);
  save();
}

// The text dialog; resolves to the new text (applied) or null if cancelled
function edit(item, kind = "text" in item ? "note" : "box") {
  const dlg = $("#anno-dialog");
  const isNote = kind === "note";
  $("#anno-title").textContent = isNote ? "Note" : "Box";
  const input = $("#anno-text");
  input.value = isNote ? item.text : item.label;
  input.rows = isNote ? 4 : 1;
  input.placeholder = isNote ? "What to say" : "Label, e.g. DC1 or tenant A";
  const colors = $("#anno-colors");
  colors.hidden = isNote;
  colors.replaceChildren(...Array.from({ length: PALETTE }, (_, i) => h("button", {
    type: "button", class: `anno-swatch${item.color === i ? " active" : ""}`, style: `--c: var(--c${i})`,
    "aria-label": `Colour ${i + 1}`, "aria-pressed": String(item.color === i),
    onclick: () => {
      item.color = i;
      for (const b of colors.children) b.classList.toggle("active", b === colors.children[i]);
    },
  })));
  dlg.returnValue = "";
  dlg.showModal();
  input.focus();
  input.select();
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => {
      if (dlg.returnValue !== "ok") { resolve(null); return; }
      const text = input.value.slice(0, 500);
      if (isNote) item.text = text; else item.label = text;
      resolve(text);
    }, { once: true });
  });
}

function remove(id) {
  const d = data();
  if (!d) return;
  d.notes = d.notes.filter((n) => n.id !== id);
  d.boxes = d.boxes.filter((b) => b.id !== id);
  if (sel === id) sel = null;
  announce("Annotation removed");
  renderDiagram(false);
  save();
}

// --- pointer: in the capture phase, so the diagram's own pan/drag never sees it

function setupPointer() {
  const svg = $("#diagram");
  svg.addEventListener("pointerdown", (ev) => {
    const el = ev.target.closest?.(".anno");
    if (!el || !canOperate() || window.Builder?.editing) {
      if (sel && !el) { sel = null; }  // a click elsewhere drops the selection
      return;
    }
    ev.stopPropagation();
    svg.setPointerCapture(ev.pointerId);
    const item = find(el.dataset.anno);
    if (!item) return;
    drag = { item, resize: !!ev.target.dataset.resize, sx: ev.clientX, sy: ev.clientY,
             start: { x: item.x, y: item.y, w: item.w, h: item.h }, moved: false };
  }, true);
  svg.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    ev.stopPropagation();
    const dx = (ev.clientX - drag.sx) / S.view.k, dy = (ev.clientY - drag.sy) / S.view.k;
    if (Math.abs(dx) + Math.abs(dy) > 3 / S.view.k) drag.moved = true;
    if (!drag.moved) return;
    const it = drag.item;
    if (drag.resize) {
      it.w = Math.max(40, Math.round(drag.start.w + dx));
      it.h = Math.max(30, Math.round(drag.start.h + dy));
    } else {
      it.x = Math.round(drag.start.x + dx);
      it.y = Math.round(drag.start.y + dy);
    }
    renderDiagram(false);
  }, true);
  svg.addEventListener("pointerup", async (ev) => {
    if (!drag) return;
    ev.stopPropagation();
    const { item, moved } = drag;
    drag = null;
    if (moved) { save(); return; }
    const now = Date.now();
    const double = lastClick.id === item.id && now - lastClick.t < 400;
    lastClick = { id: item.id, t: double ? 0 : now };
    sel = item.id;
    renderDiagram(false);
    if (double && (await edit(item)) !== null) {
      renderDiagram(false);
      save();
    }
  }, true);
  document.addEventListener("keydown", (ev) => {
    if (!sel || (ev.key !== "Delete" && ev.key !== "Backspace")) return;
    if (ev.target.closest?.("input, textarea, select, [contenteditable], dialog, .xterm")) return;
    if ($("#view-diagram").hidden) return;
    ev.preventDefault();
    remove(sel);
  });
}

function setup() {
  $("#add-note").addEventListener("click", () => add("note"));
  $("#add-box").addEventListener("click", () => add("box"));
  // Enter saves (Shift+Enter is a new line in a note)
  $("#anno-text").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); $("#anno-dialog").close("ok"); }
  });
  setupPointer();
}

window.Annotations = { decorate, wrap, reset() { sel = null; drag = null; } };
setup();
})();
