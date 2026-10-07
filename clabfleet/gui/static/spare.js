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
// refreshState, refreshLive; and from capture.js: selectLink.
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

async function unplug(l) {
  const here = `${l.a.node}:${l.a.iface}`, there = `${l.b.node}:${l.b.iface}`;
  const running = labStatus(S.detail.name, S.detail.nodes.length).deployed > 0;
  const kinds = [l.a.node, l.b.node].map((name) => S.detail.nodes.find((n) => n.name === name));
  const live = running && kinds.every((n) => n?.cable_live);
  if (!(await confirmDialog({
    title: `Unplug ${here} from ${there}?`,
    body: [
      "The topology file gets two spare ports in place of this link: the ports stay, with nothing plugged in.",
      !running ? "The lab is not deployed: the cable is gone at the next deploy."
        : live ? "The lab is running: the cable is also pulled now. What runs over it (sessions, traffic) goes down; the nodes' configs are not changed."
          : `On this running lab the cable cannot be pulled now (a node's kind does not allow it): it is gone after the next deploy. ${NEXT_DEPLOY}`,
    ],
    ok: "Unplug it", danger: true,
  }))) return;
  await change("uncable", { a: { node: l.a.node, iface: l.a.iface }, b: { node: l.b.node, iface: l.b.iface } }, (r) => {
    toast(r.live ? `Unplugged ${here} from ${there} on the running lab`
      : `Unplugging ${here} from ${there} saved: ${r.note}.${running ? ` ${NEXT_DEPLOY}` : ""}`, { ok: true });
    selectLink(null);  // the link is no more
  });
}

// For the link card: pull this cable (a plain link between two nodes)
function linkActions(l) {
  if (!canOperate() || S.selected?.type !== "topo" || !l.a.node || !l.b.node || l.type !== "veth") return [];
  return [h("h4", {}, "Cable"), h("span", { class: "open" },
    h("button", {
      class: "btn small danger", type: "button",
      title: "Pull this cable: its two ports become spare ports, in the file and on the running lab",
      onclick: () => unplug(l),
    }, "Unplug"))];
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

window.Spare = {
  section,
  linkActions,
  // Ask to cable two spare ports, given by name (the rack view's drag and drop)
  cable(aNode, aIface, bNode, bIface) {
    const node = S.detail?.nodes?.find((n) => n.name === aNode);
    if (node && canOperate() && S.selected?.type === "topo") return cable(node, aIface, `${bNode}\u0000${bIface}`);
    return null;
  },
};
})();
