"use strict";

// ---------------------------------------------------------------------------
// Spare ports: ports a node has with nothing plugged in. The inspector lists
// a node's spare ports, cables one to a spare port of another node, and adds
// more of them. All of it is written into the topology file; a cable between
// two running nodes on one host is also made on the spot. A port that is
// added only exists on a node from its next deploy: nodes learn their ports
// when they boot.
//
// Uses app.js globals: S, $, h, api, toast, confirmDialog, canOperate,
// topoPath, yamlDirty, runningJob, labStatus, naturalCmp, reloadDetail,
// refreshState, refreshLive.
// ---------------------------------------------------------------------------

(() => {

const DEFAULT_ADD = 8;
// How a running lab gets what only a deploy can give it, without losing
// what was configured on its nodes since
const NEXT_DEPLOY = "Stop the lab, then Deploy it again: that keeps the nodes' configs. " +
  "(Redeploy would start them from their startup configs.)";
let picked = { node: null, from: "", to: "" };  // what the cable form shows, per node

// Other spare ports a port could be cabled to: other nodes first
function targets(node) {
  const mine = [], others = [];
  for (const n of S.detail.nodes) {
    for (const iface of n.spare || []) (n.name === node.name ? mine : others).push({ node: n.name, iface });
  }
  const order = (a, b) => naturalCmp(a.node, b.node) || naturalCmp(a.iface, b.iface);
  return [...others.sort(order), ...mine.sort(order)];
}

// Edits go into the saved file: not over unsaved text, nor under a job
function blocked() {
  if (yamlDirty()) return "Save or discard your YAML edits first";
  if (runningJob(S.selected.id)) return "A job is running for this lab";
  return "";
}

async function change(path, body, done) {
  const why = blocked();
  if (why) { toast(why); return; }
  let result;
  try {
    result = await api(`/api/${path}/${topoPath(S.selected.id)}`, {
      method: "POST", body: JSON.stringify({ ...body, base_hash: S.detail.hash }),
    });
  } catch (e) {
    toast(`Not done: ${e.message}`);
    return;
  }
  done(result);
  await reloadDetail(true);
  refreshState();
  refreshLive();
}

async function cable(node, from, to) {
  const [toNode, toIface] = to.split("\u0000");
  const here = `${node.name}:${from}`, there = `${toNode}:${toIface}`;
  const running = labStatus(S.detail.name, S.detail.nodes.length).deployed > 0;
  const other = S.detail.nodes.find((n) => n.name === toNode);
  const live = running && node.cable_live && other?.cable_live;
  if (!(await confirmDialog({
    title: `Cable ${here} to ${there}?`,
    body: [
      "The topology file gets a link between the two ports in place of the spare ports.",
      !running ? "The lab is not deployed: the cable is there at the next deploy."
        : live ? "The lab is running: the cable is also plugged in now, without a redeploy."
          : `On this running lab the cable cannot be plugged in now (a node's kind does not allow it): it is there after the next deploy. ${NEXT_DEPLOY}`,
    ],
    ok: "Cable them",
  }))) return;
  await change("cable", { a: { node: node.name, iface: from }, b: { node: toNode, iface: toIface } }, (r) => {
    toast(r.live ? `Cabled ${here} to ${there} on the running lab`
      : `Cable ${here} to ${there} saved: ${r.note}.${running ? ` ${NEXT_DEPLOY}` : ""}`, { ok: true });
    picked = { node: null, from: "", to: "" };
  });
}

async function addPorts(node, count) {
  await change("ports", { node: node.name, count }, (r) => {
    const span = r.ports.length > 1 ? `${r.ports[0]} to ${r.ports[r.ports.length - 1]}` : r.ports[0];
    toast(`${node.name}: ${r.ports.length} spare port${r.ports.length === 1 ? "" : "s"} added (${span})` +
      (r.applies === "now" ? "" : `. The running node gets them at its next deploy. ${NEXT_DEPLOY}`), { ok: true });
  });
}

// The inspector's section for a node of an open topology
function section(node) {
  if (S.selected?.type !== "topo" || !S.detail || S.detail.error) return [];
  const spare = [...(node.spare || [])].sort(naturalCmp);
  const op = canOperate();
  if (!spare.length && !op) return [];
  if (picked.node !== node.name) picked = { node: node.name, from: "", to: "" };
  const options = targets(node);
  const from = spare.includes(picked.from) ? picked.from : spare[0] || "";
  const choices = options.filter((t) => !(t.node === node.name && t.iface === from));
  const key = (t) => `${t.node}\u0000${t.iface}`;
  const to = choices.some((t) => key(t) === picked.to) ? picked.to : (choices[0] ? key(choices[0]) : "");
  picked.from = from;
  picked.to = to;

  const out = [h("h4", {}, `Spare ports${spare.length ? ` (${spare.length})` : ""}`)];
  out.push(spare.length
    ? h("div", { class: "spare-list mono", title: "Ports of this node with nothing plugged in" }, spare.join("  "))
    : h("p", { class: "muted small" }, "None. Add some to have ports to cable later."));
  if (!op) return out;

  if (spare.length && choices.length) {
    out.push(h("form", {
      class: "spare-form", onsubmit: (ev) => { ev.preventDefault(); cable(node, picked.from, picked.to); },
    },
      h("select", {
        "aria-label": "Spare port of this node", onchange: (ev) => { picked.from = ev.target.value; renderNodeCard(); },
      }, spare.map((i) => h("option", { value: i, selected: i === from }, i))),
      h("span", { class: "muted", "aria-hidden": "true" }, "↔"),
      h("select", {
        "aria-label": "Spare port to cable it to", onchange: (ev) => { picked.to = ev.target.value; },
      }, choices.map((t) => h("option", { value: key(t), selected: key(t) === to }, `${t.node}:${t.iface}`))),
      h("button", { class: "btn small", type: "submit", title: "Cable these two spare ports" }, "Cable")));
  } else if (spare.length) {
    out.push(h("p", { class: "muted small" }, "No other spare port to cable to: add ports to another node."));
  }
  const count = h("input", {
    type: "number", min: "1", max: "64", value: String(DEFAULT_ADD), class: "spare-count",
    "aria-label": "How many ports to add",
  });
  out.push(h("form", {
    class: "spare-form",
    onsubmit: (ev) => { ev.preventDefault(); addPorts(node, Number(count.value)); },
  },
    count,
    h("button", { class: "btn small", type: "submit", title: "Add spare ports to this node, in the topology file" },
      "Add ports")));
  return out;
}

window.Spare = { section };
})();
