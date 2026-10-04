"use strict";

// ---------------------------------------------------------------------------
// Rack view: the Diagram drawn as the machine room. Each lab host is a rack,
// each node a device in its slots with one port per interface the topology
// uses, and each link a cable between those ports. Links inside a host loop
// through the cable manager beside its rack; links between hosts (VXLAN)
// run through the cable tray above the racks, labelled with their VNI.
// Cable colour is the link's role, port LEDs its live state.
//
// Nodes and links carry the same data attributes as the logical drawing
// (data-id, data-f, data-ends, data-link), so selection, hover focus,
// terminals, capture and export work unchanged. Positions here are computed,
// never dragged: Save layout and Auto layout belong to the logical view.
//
// Uses app.js globals: S, $, s, announce, renderDiagram, nodeRuntime,
// nodeState, nodeHost, statusGlyph, kindName, nodeRole, naturalCmp,
// crossLinkVnis, liveData, linkLive, BUSY_BPS, fmtCpu, fmtBytes, fmtMem,
// nodeLabel, truncate; and from canvas.js: tierOf; from capture.js:
// selectedLink.
// ---------------------------------------------------------------------------

(() => {

const KEY = "clab-diagram-view";
const RW = 330;           // rack width
const GAP = 76;           // between racks: room for the cable manager
const HEAD = 58;          // rack header
const U = 32;             // one rack unit
const RAIL = 16;
const FACE_W = RW - 2 * RAIL;
const TEXT_W = 150;       // left part of a faceplate: status, name, kind
const PITCH = 17;         // port spacing, wider when the port names are long
const LANE = 7;           // cable tray lane spacing
const MGR_LANES = 16;     // cable manager lanes before they repeat

let on = false;
try { on = localStorage.getItem(KEY) === "racks"; } catch { /* private mode */ }
let pos = {};             // node id -> centre of its faceplate (keys, fit)
let box = null;           // bounds of the drawing

function active() {
  return on && !window.Builder?.editing && !!S.detail && !S.detail.error;
}

// The rack a node goes in: its host, the only host, or "not placed"
function rackName(lab, nd, model) {
  if (nd.pseudo) {
    const l = model.links.find((x) => x.a.id === nd.id || x.b.id === nd.id);
    const other = l && (l.a.id === nd.id ? l.b.id : l.a.id);
    const peer = other && model.nodes.find((n) => n.id === other);
    return peer && !peer.pseudo ? rackName(lab, peer, model) : "not placed";
  }
  if (!S.state?.multi_host) return S.state?.hosts?.[0]?.name || "this host";
  return nodeHost(lab, nd.id) || nd.node.host_pin || "not placed";
}

// Ports of each node: the interfaces its links use, in natural order
function portsOf(model) {
  const ports = {};
  for (const l of model.links) {
    for (const e of [l.a, l.b]) (ports[e.id] ||= []).push(e.iface);
  }
  for (const id in ports) ports[id] = [...new Set(ports[id])].sort(naturalCmp);
  return ports;
}

// "eth12" -> "12", "e1-1" -> "1-1", "Ethernet3" -> "3": what fits under a jack
function portLabel(iface) {
  const m = /(\d+(?:[/:-]\d+)*)$/.exec(iface || "");
  return truncate(m ? m[1] : iface || "", 4);
}

// Port spacing of a device and how many fit in a row: each port needs room
// for its LED and its name above the jack
function portGeom(list) {
  const chars = Math.max(0, ...(list || []).map((i) => portLabel(i).length));
  const pitch = Math.max(PITCH, Math.ceil(8.5 + chars * 4.6));
  return { pitch, perRow: Math.floor((FACE_W - TEXT_W - 10) / pitch) };
}

// What a cable is for: to a host, a pair's parallel links, the fabric, or outside the lab
function cableKind(l, model, pairs) {
  if (l.special) return "special";
  const role = (id) => {
    const nd = model.nodes.find((n) => n.id === id);
    return nd && !nd.pseudo ? nodeRole(nd.id, nd.node.kind) : "";
  };
  if (role(l.a.id) === "host" || role(l.b.id) === "host") return "access";
  if (pairs[[l.a.id, l.b.id].sort().join("|")] > 1) return "peer";
  return "fabric";
}

function hostInfo(name) {
  const cfg = (S.state?.hosts || []).find((x) => x.name === name);
  const live = (S.hostInfo || []).find((x) => x.name === name);
  return { address: cfg?.host || live?.host || "", live };
}

// Draws the racks into ``g`` (the diagram's viewport group)
function draw(g, model) {
  const lab = S.detail.name;
  const live = liveData();
  const vnis = crossLinkVnis();
  const ports = portsOf(model);
  pos = {};

  // Racks in the cluster's host order, the unplaced last
  const order = (S.state?.hosts || []).map((x) => x.name);
  const byRack = new Map();
  for (const nd of model.nodes) {
    const r = rackName(lab, nd, model);
    if (!byRack.has(r)) byRack.set(r, []);
    byRack.get(r).push(nd);
  }
  const rank = (r) => (r === "not placed" ? 1e6 : order.includes(r) ? order.indexOf(r) : 1e5);
  const racks = [...byRack.keys()].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));

  // Slots: tiers top down (spines first, hosts last), a spare unit between tiers
  const slots = {};       // id -> {rack index, unit, rows}
  let units = 0;
  racks.forEach((r, i) => {
    const tier = (nd) => (nd.pseudo ? 9 : tierOf(nd) < 0 ? 2 : tierOf(nd));
    const nodes = byRack.get(r).sort((a, b) => tier(a) - tier(b) || naturalCmp(a.id, b.id));
    let u = 0, last = null;
    for (const nd of nodes) {
      if (last !== null && tier(nd) !== last) u++;
      last = tier(nd);
      const rows = Math.max(1, Math.ceil((ports[nd.id]?.length || 0) / portGeom(ports[nd.id]).perRow));
      slots[nd.id] = { rack: i, unit: u, rows };
      u += rows;
    }
    units = Math.max(units, u + 1);
  });

  // Cables between racks get a lane each in the tray
  const crossOf = (l) => slots[l.a.id] && slots[l.b.id] && slots[l.a.id].rack !== slots[l.b.id].rack;
  const nCross = model.links.filter(crossOf).length;
  const trayH = nCross ? 28 + nCross * LANE : 0;
  const top = nCross ? trayH + 34 : 0;
  const width = racks.length * RW + (racks.length - 1) * GAP + GAP;
  const height = HEAD + units * U + 14;
  box = { x0: -10, y0: nCross ? -18 : -10, x1: width, y1: top + height + 10 };

  const furniture = s("g", { class: "rack-room" });
  const devices = s("g", {});
  const cables = s("g", {});
  const rings = s("g", {});

  if (nCross) {
    furniture.append(
      s("text", { class: "tray-label", x: 4, y: -6 }, "Cable tray · links between hosts (VXLAN)"),
      s("rect", { class: "tray", x: 0, y: 0, width: width - GAP / 2, height: trayH, rx: 4 }));
  }

  const port = {};        // "node:iface" -> {x, y, rack, rx, led, ring}
  racks.forEach((r, i) => {
    const rx = i * (RW + GAP), ry = top;
    const info = hostInfo(r);
    const labMem = byRack.get(r).reduce((sum, nd) => sum + (live?.nodes?.[nd.id]?.mem || 0), 0);
    const res = info.live?.ok
      ? `${info.live.cpus ?? "?"} CPU · ${fmtMem(info.live.mem_available_mb)} free` + (labMem ? ` · lab uses ${fmtBytes(labMem)}` : "")
      : info.live ? "not answering" : "";
    furniture.append(s("g", { class: `rack${r === "not placed" ? " unplaced" : ""}` },
      s("rect", { class: "frame", x: rx, y: ry, width: RW, height, rx: 5 }),
      s("text", { class: "rk-name", x: rx + 14, y: ry + 22 }, r),
      s("text", { class: "rk-meta", x: rx + 14, y: ry + 37 }, r === "not placed" ? "deploy to place these nodes" : info.address === r ? "" : info.address),
      s("text", { class: "rk-meta", x: rx + 14, y: ry + 50 }, res),
      s("rect", { class: "rail", x: rx, y: ry + HEAD, width: RAIL, height: units * U + 6 }),
      s("rect", { class: "rail", x: rx + RW - RAIL, y: ry + HEAD, width: RAIL, height: units * U + 6 }),
      ...Array.from({ length: units }, (_, u) => s("text", { class: "unum", x: rx + 4, y: ry + HEAD + 3 + u * U + 18 }, String(u + 1))),
      s("line", { class: "mgr", x1: rx + RW + 12, y1: ry + HEAD, x2: rx + RW + 12, y2: ry + height - 10 })));

    for (const nd of byRack.get(r)) {
      const sl = slots[nd.id];
      const x = rx + RAIL, y = ry + HEAD + 3 + sl.unit * U, h = sl.rows * U - 3;
      pos[nd.id] = [x + FACE_W / 2, y + h / 2];
      const rt = nd.pseudo ? null : nodeRuntime(lab, nd.id);
      const st = { running: "running", booting: "booting", partial: "other" }[nodeState(rt)] || "";
      const use = rt?.state === "running" ? live?.nodes?.[nd.id] : null;
      const sub = nd.pseudo ? "outside the lab"
        : kindName(nd.node.kind) + (use?.cpu != null ? ` · ${fmtCpu(use.cpu)} · ${fmtBytes(use.mem)}` : "");
      // Text gets the faceplate up to the first row of ports
      const list = ports[nd.id] || [];
      const { pitch, perRow } = portGeom(list);
      const room = FACE_W - 34 - Math.min(perRow, list.length) * pitch;
      const el = s("g", {
        class: `${nd.pseudo ? "pseudo" : "node"} rack-dev${S.selectedNode === nd.id ? " selected" : ""}`,
        "data-f": nd.id, ...(nd.pseudo ? {} : { "data-id": nd.id, role: "img", "aria-label": nodeLabel(nd.id) }),
      },
        s("rect", { class: "face", x, y, width: FACE_W, height: h, rx: 2 }),
        nd.pseudo ? null : statusGlyph(st, x + 12, y + 14),
        s("text", { class: "name", x: x + 24, y: y + 14 }, truncate(nd.pseudo ? nd.label : nd.id, Math.floor(room / 7.5))),
        s("text", { class: "kind", x: x + 24, y: y + 25 }, truncate(sub, Math.floor(room / 5.8))),
        nd.pseudo ? null : s("title", {}, nodeLabel(nd.id)));
      // LED and name sit above the jack: cables leave it sideways and sag below
      list.forEach((iface, k) => {
        const row = Math.floor(k / perRow), col = k % perRow;
        const inRow = Math.min(perRow, list.length - row * perRow);
        const px = x + FACE_W - 10 - (inRow - col) * pitch + 2, py = y + 12 + row * U;
        const led = s("rect", { class: "led", x: px, y: py - 6.5, width: 3, height: 3 });
        el.append(s("rect", { class: "jack", x: px, y: py, width: 13, height: 11, rx: 1.5 }), led,
          s("text", { class: "pnum", x: px + 4.5, y: py - 2.5 }, portLabel(iface)));
        const ring = s("rect", { class: "port-ring", x: px - 2, y: py - 10, width: pitch, height: 23, rx: 2.5 });
        rings.append(ring);
        port[`${nd.id}:${iface}`] = { x: px + 6.5, y: py + 5.5, rack: sl.rack, rx, led, ring };
      });
      devices.append(el);
    }
  });

  // Cables
  const pairs = {};
  for (const l of model.links) { const k = [l.a.id, l.b.id].sort().join("|"); pairs[k] = (pairs[k] || 0) + 1; }
  const mgr = racks.map(() => 0);
  let lane = 0;
  for (const l of model.links) {
    const A = port[`${l.a.id}:${l.a.iface}`], B = port[`${l.b.id}:${l.b.iface}`];
    if (!A || !B) continue;
    const { ls, down, vni, title, bps } = linkLive(l, live, vnis, (v) =>
      (A.rack !== B.rack ? `\nbetween hosts${v !== undefined ? `, VNI ${v}` : ""}` : ""));
    const kind = cableKind(l, model, pairs);
    let d, label = null;
    if (A.rack !== B.rack) {
      // Up the manager beside one rack, along the tray, down beside the other
      const ty = 16 + lane++ * LANE;
      const mA = A.rx + RW + 5 + (mgr[A.rack]++ % MGR_LANES) * 3.4;
      const mB = B.rx + RW + 5 + (mgr[B.rack]++ % MGR_LANES) * 3.4;
      const sg = Math.sign(mB - mA) || 1;
      d = `M${A.x},${A.y} C${A.x + 6},${A.y + 10} ${mA},${A.y + 6} ${mA},${A.y - 8} L${mA},${ty + 12} Q${mA},${ty} ${mA + sg * 12},${ty} ` +
          `L${mB - sg * 12},${ty} Q${mB},${ty} ${mB},${ty + 12} L${mB},${B.y - 8} C${mB},${B.y + 6} ${B.x + 6},${B.y + 10} ${B.x},${B.y}`;
      if (vni !== undefined) label = s("text", { class: "vni", x: (mA + mB) / 2 + (lane % 2 ? -40 : 40) - 18, y: ty - 1.5, "data-ends": `${l.a.id}|${l.b.id}` }, `VNI ${vni}`);
    } else {
      // A loop out to the cable manager and back
      const k = mgr[A.rack]++ % MGR_LANES;
      const mx = A.rx + RW + 6 + k * 3.2 + Math.min(14, Math.abs(A.y - B.y) / 7);
      // Along the faceplate it droops below the jacks, clear of the port names,
      // and only turns up or down once it is past the rail
      const ex = A.rx + RW - RAIL + 2, sag = 8 + (k % 3) * 2;
      d = `M${A.x},${A.y} C${A.x + 5},${A.y + sag} ${ex - 10},${A.y + 2} ${ex},${A.y + 2} Q${mx},${A.y + 2} ${mx},${(A.y + B.y) / 2} ` +
          `Q${mx},${B.y + 2} ${ex},${B.y + 2} C${ex - 10},${B.y + 2} ${B.x + 5},${B.y + sag} ${B.x},${B.y}`;
    }
    const ends = `${l.a.id}|${l.b.id}`;
    cables.append(s("path", {
      class: `link cable ${kind}${down ? " down" : ""}${selectedLink()?.id === l.id ? " selected" : ""}`, d, "data-ends": ends,
    }, s("title", {}, title)));
    if (bps >= BUSY_BPS) {
      const dur = Math.max(0.6, 3 - 0.5 * Math.log10(bps / BUSY_BPS)).toFixed(2);
      cables.append(s("path", { class: "flow", d, "data-ends": ends, style: `--flow-dur: ${dur}s` }));
    }
    if (label) cables.append(label);
    cables.append(s("path", { class: "link-hit", d, "data-link": l.id }, s("title", {}, `${title}\nClick to capture packets`)));
    for (const [P, side] of [[A, "a"], [B, "b"]]) {
      const state = ls?.[side]?.state;
      P.led.classList.add(state === "down" ? "down" : state === "up" ? "up" : "off");
      P.ring.dataset.ends = ends;
      cables.append(s("rect", { class: "boot", x: P.x - 4, y: P.y - 3.5, width: 8, height: 7, rx: 1 }));
    }
  }

  g.append(furniture, devices, cables, rings);
}

function toggle() {
  on = !on;
  try { localStorage.setItem(KEY, on ? "racks" : "logical"); } catch { /* private mode */ }
  announce(on ? "Rack view: each host is a rack, links are cables between ports" : "Logical view");
  sync();
  renderDiagram(true);
}

// The switch's look, and what only the logical view offers
function sync() {
  const btn = $("#racks-toggle");
  btn.setAttribute("aria-pressed", String(on));
  btn.classList.toggle("active", on);
  document.body.classList.toggle("racks-view", active());
}

function setup() {
  $("#racks-toggle").addEventListener("click", toggle);
  sync();
}

window.Racks = {
  active, draw, toggle, sync,
  positions: () => pos,
  bounds: () => box,
  get on() { return on; },
};
setup();
})();
