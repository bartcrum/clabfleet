"use strict";

// ---------------------------------------------------------------------------
// Hosts page: each lab host with its capacity, the labs running on it and
// which of them span hosts. Opened from the host chips in the top bar.
//
// A host's numbers are of two kinds, and the page says which is which:
// memory in use is measured on the host; the vCPU (and RAM) of labs are
// what placement counts for their running nodes (a node's lab.cpu / lab.ram
// labels, else an estimate for its kind), against what the host may use.
//
// Uses app.js globals: S, $, h, fmtMem, naturalCmp, confirmDiscard,
// renderSidebar, selectTopology, selectOtherLab.
// ---------------------------------------------------------------------------

(() => {

let open = false;

// host -> lab -> {nodes, running, cpu, ram} (cpu and ram of running nodes)
function labsByHost() {
  const out = new Map();
  for (const hs of S.state?.runtime || []) {
    for (const c of hs.containers || []) {
      const labs = out.get(c.host) || out.set(c.host, new Map()).get(c.host);
      const lab = labs.get(c.lab) || labs.set(c.lab, { nodes: 0, running: 0, cpu: 0, ram: 0 }).get(c.lab);
      lab.nodes += 1;
      if (c.state === "running") {
        lab.running += 1;
        lab.cpu += c.cpu || 0;
        lab.ram += c.ram || 0;
      }
    }
  }
  return out;
}

// A capacity bar: how much of ``total`` is taken, in words and as a bar
function meter(label, used, total, text, note) {
  const part = total > 0 ? Math.min(used / total, 1) : 0;
  const level = used > total ? "over" : part >= 0.8 ? "high" : "";
  return h("div", { class: "meter-row" },
    h("div", { class: "meter-text" },
      h("b", {}, label), h("span", {}, text),
      used > total ? h("span", { class: "meter-over" }, "over capacity") : null),
    h("div", { class: `meter ${level}`, role: "img", "aria-label": `${label}: ${text}` },
      h("div", { class: "meter-fill", style: `width: ${(part * 100).toFixed(1)}%` })),
    note ? h("div", { class: "muted small" }, note) : null);
}

const cpus = (n) => (Number.isInteger(n) ? String(n) : n.toFixed(1));

function openLab(lab) {
  const topo = (S.state?.topologies || []).find((t) => t.name === lab);
  if (topo) selectTopology(topo.id); else selectOtherLab(lab);
}

function hostCard(hst, labs, hostsOf) {
  const head = h("div", { class: "host-head" },
    h("span", { class: `dot ${hst.ok && hst.version ? "running" : "error"}` }),
    h("h2", {}, hst.name),
    h("span", { class: "muted small" }, [hst.local ? "this machine" : hst.host,
      hst.ok ? `containerlab ${hst.version || "not installed"}` : null,
      hst.vtep ? `VTEP ${hst.vtep}` : null,
      hst.tags?.length ? `tags: ${hst.tags.join(", ")}` : null].filter(Boolean).join(" · ")));
  if (!hst.ok) {
    return h("section", { class: "host-card" }, head,
      h("p", { class: "login-error small" }, `Not reachable: ${hst.error || "no answer"}`));
  }
  const rows = [...labs.entries()].sort((a, b) => naturalCmp(a[0], b[0]));
  const reserved = rows.reduce((sum, [, l]) => ({ cpu: sum.cpu + l.cpu, ram: sum.ram + l.ram }), { cpu: 0, ram: 0 });
  const used = hst.mem_total_mb != null && hst.mem_available_mb != null ? hst.mem_total_mb - hst.mem_available_mb : null;
  return h("section", { class: "host-card" }, head,
    h("div", { class: "meters" },
      used != null ? meter("Memory", used, hst.mem_total_mb,
        `${fmtMem(used)} of ${fmtMem(hst.mem_total_mb)} in use`, "measured on the host") : null,
      hst.max_cpu ? meter("vCPU", reserved.cpu, hst.max_cpu,
        `${cpus(reserved.cpu)} of ${cpus(hst.max_cpu)} counted for running labs`,
        "what placement counts per node, not CPU load") : null,
      hst.max_ram_set ? meter("RAM for labs", reserved.ram, hst.max_ram,
        `${fmtMem(reserved.ram)} of ${fmtMem(hst.max_ram)} counted for running labs`,
        "against max_ram in the cluster file") : null),
    rows.length ? h("table", { class: "nodes host-labs" },
      h("thead", {}, h("tr", {}, h("th", {}, "Lab"), h("th", {}, "Nodes here"), h("th", {}, "vCPU"),
        h("th", {}, "RAM"), h("th", {}, "Also on"))),
      h("tbody", {}, rows.map(([lab, l]) => {
        const others = [...(hostsOf.get(lab) || [])].filter((x) => x !== hst.name).sort(naturalCmp);
        return h("tr", { class: "pick", title: `Open ${lab}`, onclick: () => openLab(lab) },
          h("td", {}, h("b", {}, lab)),
          h("td", {}, l.running === l.nodes ? `${l.nodes} running` : `${l.running} of ${l.nodes} running`),
          h("td", { class: "mono" }, cpus(l.cpu)),
          h("td", { class: "mono" }, fmtMem(l.ram)),
          h("td", {}, others.length ? others.join(", ") : h("span", { class: "muted" }, "this host only")));
      })))
      : h("p", { class: "muted small" }, "No labs on this host."));
}

function render() {
  if (!open) return;
  const hosts = S.hostInfo || [];
  const byHost = labsByHost();
  const hostsOf = new Map();  // lab -> the hosts it has nodes on
  for (const [host, labs] of byHost) {
    for (const lab of labs.keys()) (hostsOf.get(lab) || hostsOf.set(lab, new Set()).get(lab)).add(host);
  }
  const spanning = [...hostsOf.values()].filter((set) => set.size > 1).length;
  const count = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  $("#cluster-summary").textContent = hosts.length
    ? [count(hosts.length, "host"), `${count(hostsOf.size, "lab")} deployed`,
       hosts.length > 1 ? `${spanning} across hosts` : null].filter(Boolean).join(" · ")
    : "Asking the hosts…";
  $("#cluster-hosts").replaceChildren(...hosts.map((hst) => hostCard(hst, byHost.get(hst.name) || new Map(), hostsOf)));
}

async function show() {
  if (open) return;
  if (!(await confirmDiscard())) return;
  window.Builder?.reset();
  S.selected = null;
  S.detail = null;
  S.live = null;
  S.selectedNode = null;
  open = true;
  $("#empty").hidden = true;
  $("#lab").hidden = true;
  $("#cluster").hidden = false;
  renderSidebar();
  render();
  $("#cluster-title").focus();
}

// ``welcome``: back to the start page (a lab being opened shows itself)
function close(welcome) {
  if (!open) return;
  open = false;
  $("#cluster").hidden = true;
  if (welcome) $("#empty").hidden = false;
}

$("#cluster-close").addEventListener("click", () => close(true));

window.Hosts = { show, close, render };
})();
