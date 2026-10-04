"use strict";

// ---------------------------------------------------------------------------
// What if (feature 6): reversible failures on a running lab. The buttons
// of the link card and the node card that shut an interface, isolate a
// node or stop it, and put it back.
//
// Uses app.js globals: S, $, h, api, toast, confirmDialog, topoPath,
// announce, refreshState, refreshLive, canOperate, nodeRuntime.
// ---------------------------------------------------------------------------

function isShut(end) {
  return end?.state === "down" && /admin down$/.test(end.detail || "");
}

async function whatif(action, node, iface = "", confirm = null) {
  if (confirm && !(await confirmDialog(confirm))) return;
  try {
    const { done } = await api(`/api/whatif/${topoPath(S.selected.id)}`, {
      method: "POST", body: JSON.stringify({ action, node, iface }),
    });
    announce(done);
    toast(done, { ok: true, actions: [{ label: "Watch Routing › Live", run: () => window.Routing?.showLive() }] });
  } catch (e) {
    toast(`Not done: ${e.message}`);
  }
  await refreshState();
  setTimeout(refreshLive, 1500);  // the next probe sees the change
}

function linkWhatif(l, ls) {
  if (!canOperate() || S.selected?.type !== "topo") return [];
  const ends = [["a", l.a], ["b", l.b]].filter(([, e]) => e.node && nodeRuntime(S.detail.name, e.node)?.state === "running");
  if (!ends.length) return [];
  return [h("h4", {}, "What if"), h("span", { class: "open" }, ends.map(([side, e]) => isShut(ls?.[side])
    ? h("button", { class: "btn small", onclick: () => whatif("link-up", e.node, e.iface) }, `No shut ${e.node}:${e.iface}`)
    : h("button", {
        class: "btn small", title: `Take ${e.node}:${e.iface} administratively down; the far end loses its carrier`,
        onclick: () => whatif("link-down", e.node, e.iface, {
          title: `Shut ${e.node}:${e.iface}?`, ok: "Shut it", danger: true,
          body: [`Takes ${e.node}:${e.iface} down inside the node, like a shut port: the link goes down and the protocols over it reconverge.`,
                 "No shut brings it back. Nothing is saved to any config."],
        }),
      }, `Shut ${e.node}:${e.iface}`)))];
}

function nodeWhatif(node, rt, live) {
  if (!canOperate() || S.selected?.type !== "topo" || !rt) return [];
  const paused = rt.state === "paused";
  const ends = S.detail.links.flatMap((l) => [[l, "a"], [l, "b"]]).filter(([l, side]) => l[side].node === node.name);
  const shut = ends.filter(([l, side]) => isShut(live?.links?.[l.id]?.[side]));
  const buttons = [paused
    ? h("button", { class: "btn small", onclick: () => whatif("resume", node.name) }, "Resume")
    : rt.state === "running" ? h("button", {
        class: "btn small", title: "Pause the node: its links stay up but it stops answering, so its neighbours time out",
        onclick: () => whatif("freeze", node.name, "", {
          title: `Freeze ${node.name}?`, ok: "Freeze it", danger: true,
          body: [`Pauses ${node.name} (docker pause): its links stay up but it stops answering, like a hung box. Its neighbours time out and reconverge.`,
                 "Resume brings it back where it was."],
        }),
      }, "Freeze") : null];
  if (rt.state === "running" && ends.length) {
    buttons.push(shut.length
      ? h("button", { class: "btn small", onclick: () => restoreLinks(node.name, shut) }, "Restore its links")
      : h("button", { class: "btn small", onclick: () => isolate(node.name, ends) }, "Shut all its links"));
  }
  return [h("h4", {}, "What if"), h("span", { class: "open" }, buttons.filter(Boolean))];
}

async function isolate(name, ends) {
  if (!(await confirmDialog({
    title: `Isolate ${name}?`, ok: "Shut its links", danger: true,
    body: [`Takes all ${ends.length} of ${name}'s link interfaces down inside the node, cutting it off. Restore its links undoes it.`],
  }))) return;
  for (const [l, side] of ends) await whatif("link-down", name, l[side].iface);
}

async function restoreLinks(name, shut) {
  for (const [l, side] of shut) await whatif("link-up", name, l[side].iface);
}
