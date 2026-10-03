"use strict";

// ---------------------------------------------------------------------------
// Export a canvas (Diagram or Routing) as SVG or PNG, light or dark
// (feature 11). The file stands alone: the drawing's CSS rules with the
// theme's colours resolved, the device glyphs it uses, a background.
//
// Uses app.js globals: S, $, h, s, currentLabName.
// ---------------------------------------------------------------------------

(() => {

const PAD = 30;

// The theme's colour and font tokens, read with that theme applied for a
// moment (the page's own theme is put back at once)
function themeTokens(theme) {
  const root = document.documentElement;
  const prev = root.dataset.theme;
  root.dataset.theme = theme;
  const cs = getComputedStyle(root);
  const names = new Set();
  for (const sheet of document.styleSheets) {
    let rules;
    try { rules = sheet.cssRules; } catch { continue; }  // another origin
    for (const rule of rules) for (const m of rule.cssText.matchAll(/--[\w-]+/g)) names.add(m[0]);
  }
  const out = [...names].map((n) => `${n}: ${cs.getPropertyValue(n).trim()};`).filter((d) => !d.endsWith(": ;"));
  if (prev === undefined) delete root.dataset.theme; else root.dataset.theme = prev;
  return out.join(" ");
}

// The CSS rules that draw a canvas (selectors on .diagram)
function canvasRules() {
  const out = [];
  for (const sheet of document.styleSheets) {
    let rules;
    try { rules = sheet.cssRules; } catch { continue; }
    for (const rule of rules) if (rule.selectorText?.includes(".diagram")) out.push(rule.cssText);
  }
  return out.join("\n");
}

function build(svg, viewportId, theme) {
  const vp = svg.querySelector(`#${viewportId}`);
  if (!vp) throw new Error("Nothing to export yet");
  const box = vp.getBBox();
  const x = box.x - PAD, y = box.y - PAD, w = box.width + 2 * PAD, hgt = box.height + 2 * PAD;
  const clone = svg.cloneNode(true);
  clone.removeAttribute("id");
  for (const a of ["tabindex", "role", "aria-roledescription", "aria-activedescendant", "style"]) clone.removeAttribute(a);
  clone.setAttribute("class", "diagram");
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  clone.setAttribute("viewBox", `${x} ${y} ${w} ${hgt}`);
  clone.setAttribute("width", Math.ceil(w));
  clone.setAttribute("height", Math.ceil(hgt));
  const cvp = clone.querySelector(`#${viewportId}`);
  cvp.removeAttribute("transform");
  // Interaction-only parts and states
  for (const el of clone.querySelectorAll(".link-hit, .link-handle, .link-draft, .anno-handle, title")) el.remove();
  for (const el of clone.querySelectorAll(".hl, .kb, .selected, .link-target")) el.classList.remove("hl", "kb", "selected", "link-target");
  // The glyphs it uses, from the page's sprite
  const defs = s("defs", {});
  const used = new Set([...clone.querySelectorAll("use")].map((u) => u.getAttribute("href")));
  for (const ref of used) {
    const symbol = document.querySelector(ref);
    if (symbol) defs.append(symbol.cloneNode(true));
  }
  const style = s("style", {}, `svg.diagram { ${themeTokens(theme)} font: 14px var(--sans); }\n` +
    `.diagram .dev { fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }\n` +
    canvasRules());
  const bg = s("rect", { x, y, width: w, height: hgt, style: "fill: var(--bg)" });
  clone.prepend(style, defs, bg);
  return { text: new XMLSerializer().serializeToString(clone), width: Math.ceil(w), height: Math.ceil(hgt) };
}

function save(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = h("a", { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

async function exportCanvas(svgId, viewportId, format, theme) {
  const { text, width, height } = build($(`#${svgId}`), viewportId, theme);
  const view = svgId === "routing" ? "routing" : "diagram";
  const name = `${currentLabName() || "lab"}-${view}-${theme}.${format}`.replace(/[^\w.-]/g, "_");
  const svgBlob = new Blob([text], { type: "image/svg+xml" });
  if (format === "svg") return save(svgBlob, name);
  // PNG at twice the size, drawn from the same SVG
  const url = URL.createObjectURL(svgBlob);
  try {
    const img = new Image();
    await new Promise((ok, fail) => { img.onload = ok; img.onerror = () => fail(new Error("Could not draw the SVG")); img.src = url; });
    const canvas = h("canvas", { width: width * 2, height: height * 2 });
    const ctx = canvas.getContext("2d");
    ctx.scale(2, 2);
    ctx.drawImage(img, 0, 0, width, height);
    const png = await new Promise((ok) => canvas.toBlob(ok, "image/png"));
    save(png, name);
  } finally {
    URL.revokeObjectURL(url);
  }
}

// One ⋯-style menu per canvas
function setup() {
  for (const [btnId, svgId, vpId] of [["export-diagram", "diagram", "viewport"], ["export-routing", "routing", "rt-viewport"]]) {
    const btn = $(`#${btnId}`);
    if (!btn) continue;
    const menu = h("div", { class: "menu export-menu", role: "menu", hidden: true },
      ...[["svg", "light"], ["svg", "dark"], ["png", "light"], ["png", "dark"]].map(([f, theme]) =>
        h("button", { class: "menu-item", role: "menuitem", onclick: async () => {
          menu.hidden = true;
          try { await exportCanvas(svgId, vpId, f, theme); } catch (e) { toast(`Export failed: ${e.message}`); }
        } }, `${f.toUpperCase()}, ${theme}`)));
    btn.after(menu);
    btn.addEventListener("click", () => { menu.hidden = !menu.hidden; });
    document.addEventListener("pointerdown", (ev) => {
      if (!menu.hidden && !ev.target.closest(".export-wrap")) menu.hidden = true;
    });
  }
}

window.Export = { build, exportCanvas };
setup();
})();
