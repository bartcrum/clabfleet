"use strict";

// ---------------------------------------------------------------------------
// The link card and packet capture (click a link, pick a side): a live
// tcpdump in a dock tab or a pcap download.
//
// Uses app.js globals: S, $, h, api, toast, diagramDetail, renderDiagram,
// renderNodeCard, nodeRuntime, liveData, nodeHost, crossLinkVnis, fmtRate;
// and from dock.js: openTermTab.
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
  return diagramDetail()?.links?.find((l) => l.id === sel.id) || null;
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
    h("h3", {}, h("span", { class: "mono" }, `${endpointLabel(l.a)} ↔ ${endpointLabel(l.b)}`),
      h("button", { class: "close", title: "Close", "aria-label": "Close", onclick: () => selectLink(null) }, "×")),
    h("h4", {}, "Overview"),
    h("dl", { class: "link-overview" }),
    h("div", { class: "whatif" }),
    h("h4", {}, "Capture packets"),
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
  const ls = liveData()?.links?.[l.id];
  const lab = S.detail.name;
  const ha = l.a.node && nodeHost(lab, l.a.node), hb = l.b.node && nodeHost(lab, l.b.node);
  const vni = crossLinkVnis()[`${endpointLabel(l.a)}|${endpointLabel(l.b)}`];
  const endState = (e) => (e ? (e.state === "down" ? `down · ${e.detail}` : e.state) : "");
  const rows = [
    ["State", ls ? (ls.state === "down" ? "down" : ls.state) : "not known"],
    ls?.a ? [endpointLabel(l.a), endState(ls.a)] : null,
    ls?.b ? [endpointLabel(l.b), endState(ls.b)] : null,
    l.type ? ["Type", l.type] : null,
    S.state?.multi_host && ha && hb && ha !== hb ? ["VXLAN", `${ha} ↔ ${hb}${vni !== undefined ? `, VNI ${vni}` : ""}`] : null,
    ls?.rate ? ["Traffic", `${l.a.node || "a"} → ${l.b.node || "b"} ${fmtRate(ls.rate.ab)} · back ${fmtRate(ls.rate.ba)}`] : null,
  ].filter(Boolean);
  card.querySelector(".link-overview").replaceChildren(...rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
  card.querySelector(".whatif").replaceChildren(...linkWhatif(l, ls));
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
    h("span", { class: "mono small" }, `${dl.label} · ${fmtSize(dl.bytes)}`),
    h("button", {
      class: "btn small ghost", disabled: dl.stopped, title: "Stop capturing and save the file",
      onclick: () => { dl.stopped = true; dl.ctrl.abort(); },
    }, "Stop & save"))));
}

// Capture download sizes (fmtBytes is for memory, in MiB / GiB)
function fmtSize(n) {
  return n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : n >= 1024 ? `${(n / 1024).toFixed(0)} KB` : `${n} B`;
}
