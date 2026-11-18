"use strict";

// ---------------------------------------------------------------------------
// Import configs: a lab from uploaded device config files, such as the
// intended/configs/*.cfg Arista AVD writes. Files are chosen or dropped (a
// dropped folder gives its config files), read in the browser and posted to
// /api/import-configs, which writes <lab>/<lab>.clab.yml with the configs
// next to it and answers with what it built and its warnings.
//
// Uses app.js globals: $, h, api, toast, refreshState, selectTopology,
// showView; and window.Builder.confirmLeave.
// ---------------------------------------------------------------------------

(() => {  // own scope: only window.ImportConfigs is shared

const SUFFIXES = [".cfg", ".conf", ".txt"];
const MAX_FILE = 2 * 1024 * 1024;      // as the server: clabfleet.cfgimport.MAX_CONFIG_BYTES
const MAX_TOTAL = 12 * 1024 * 1024;    // the server takes 16 MB of JSON
const MAX_FILES = 500;

const I = {
  files: new Map(),   // name → File
  done: null,         // the server's answer, once imported
  image: "",          // the default cEOS image, for the placeholder
};

function isConfig(name) {
  return SUFFIXES.some((suffix) => name.toLowerCase().endsWith(suffix));
}

function showError(msg) {
  const err = $("#importcfg-error");
  err.textContent = msg || "";
  err.hidden = !msg;
}

function renderFiles() {
  $("#importcfg-files").replaceChildren(...[...I.files.keys()].sort().map((name) =>
    h("li", {}, h("span", {}, name),
      h("button", { type: "button", title: `Remove ${name}`, "aria-label": `Remove ${name}`,
                    onclick: () => { I.files.delete(name); renderFiles(); } }, "×"))));
  $("#importcfg-ok").disabled = !I.files.size;
}

function addFiles(files) {
  const skipped = [];
  for (const file of files) {
    if (!isConfig(file.name)) skipped.push(`${file.name} (not a .cfg, .conf or .txt file)`);
    else if (file.size > MAX_FILE) skipped.push(`${file.name} (larger than 2 MB)`);
    else if (I.files.size >= MAX_FILES && !I.files.has(file.name)) skipped.push(`${file.name} (more than ${MAX_FILES} files)`);
    else I.files.set(file.name, file);
  }
  showError(skipped.length ? `Left out: ${skipped.join(", ")}` : "");
  renderFiles();
}

// The files of a drop: plain files, and the files directly in a dropped folder
async function droppedFiles(dataTransfer) {
  const entries = [...(dataTransfer.items || [])]
    .map((item) => (item.webkitGetAsEntry ? item.webkitGetAsEntry() : null));
  if (!entries.some((e) => e && e.isDirectory)) return [...dataTransfer.files];
  const files = [];
  const fileOf = (entry) => new Promise((resolve, reject) => entry.file(resolve, reject));
  for (const entry of entries) {
    if (!entry) continue;
    if (entry.isFile) { files.push(await fileOf(entry)); continue; }
    const reader = entry.createReader();
    for (;;) {  // readEntries gives a folder in batches, and an empty one at the end
      const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
      if (!batch.length) break;
      for (const child of batch) if (child.isFile && isConfig(child.name)) files.push(await fileOf(child));
    }
  }
  return files;
}

async function open(files = []) {
  if (window.Builder && !(await window.Builder.confirmLeave())) return;
  I.files.clear();
  I.done = null;
  $("#importcfg-name").value = "";
  $("#importcfg-image").value = "";
  $("#importcfg-sanitise").checked = true;
  $("#importcfg-residual").checked = false;
  $("#importcfg-residual-row").hidden = true;
  $("#importcfg-fields").hidden = false;
  $("#importcfg-result").hidden = true;
  $("#importcfg-cancel").hidden = false;
  $("#importcfg-ok").textContent = "Import";
  showError("");
  renderFiles();
  if (files.length) addFiles(files);
  $("#importcfg-dlg").showModal();
  if (!I.image) {
    try { I.image = (await api("/api/builder")).kinds.arista_ceos?.image || ""; } catch { /* the server has a default */ }
  }
  $("#importcfg-image").placeholder = I.image;
}

function showResult(result) {
  I.done = result;
  $("#importcfg-fields").hidden = true;
  $("#importcfg-result").hidden = false;
  const count = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  $("#importcfg-summary").textContent =
    `Built ${count(result.nodes, "node")} and ${count(result.links, "link")}`
    + (result.sanitised ? ", configs sanitised." : ". The configs were saved as they are, secrets included.")
    + (result.warnings.length ? ` ${count(result.warnings.length, "note")} from the import:` : "");
  $("#importcfg-warnings").replaceChildren(...result.warnings.map((w) => h("li", {}, w)));
  $("#importcfg-cancel").hidden = true;
  $("#importcfg-ok").textContent = "Open the lab";
  $("#importcfg-ok").disabled = false;
  $("#importcfg-ok").focus();
}

async function submit(ev) {
  ev.preventDefault();
  const dlg = $("#importcfg-dlg");
  if (ev.submitter?.value === "cancel") { dlg.close(); return; }
  if (I.done) {
    dlg.close();
    await selectTopology(I.done.id);
    showView("diagram");
    return;
  }
  if (!I.files.size) { showError("Choose at least one config file."); return; }
  let total = 0;
  for (const file of I.files.values()) total += file.size;
  if (total > MAX_TOTAL) { showError("These files are more than 12 MB together: import fewer at once."); return; }
  const ok = $("#importcfg-ok");
  ok.disabled = true;
  showError("");
  try {
    const files = [];
    for (const [name, file] of I.files) files.push({ name, text: await file.text() });
    const res = await fetch("/api/import-configs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: $("#importcfg-name").value.trim(), files,
        image: $("#importcfg-image").value.trim(),
        sanitise: $("#importcfg-sanitise").checked,
        allow_residual: $("#importcfg-sanitise").checked && $("#importcfg-residual").checked,
      }),
    });
    if (!res.ok) {
      // 422: the check after sanitising still found something that looks like a secret
      if (res.status === 422) $("#importcfg-residual-row").hidden = false;
      throw new Error((await res.text()) || res.statusText);
    }
    const result = await res.json();
    await refreshState();
    showResult(result);
  } catch (e) {
    showError(e.message);
    ok.disabled = false;
  }
}

function setup() {
  const drop = $("#importcfg-drop");
  const input = $("#importcfg-input");
  $("#import-cfg").addEventListener("click", () => open());
  $("#importcfg-form").addEventListener("submit", submit);
  $("#importcfg-browse").addEventListener("click", () => input.click());
  input.addEventListener("change", () => { addFiles([...input.files]); input.value = ""; });
  for (const type of ["dragenter", "dragover"]) {
    drop.addEventListener(type, (ev) => { ev.preventDefault(); drop.classList.add("over"); });
  }
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", async (ev) => {
    ev.preventDefault();
    drop.classList.remove("over");
    try { addFiles(await droppedFiles(ev.dataTransfer)); } catch (e) { showError(`Could not read the drop: ${e.message}`); }
  });
  $("#importcfg-sanitise").addEventListener("change", () => {
    if (!$("#importcfg-sanitise").checked) $("#importcfg-residual-row").hidden = true;
  });
}

window.ImportConfigs = { open };

setup();
})();
