"""Build the intended OSPF, BGP and EVPN views of a topology from its configs.

The result of :func:`routing_view` is plain JSON for the GUI's Routing tab
and ``clabfleet routing``. Nodes are the topology's node names; a protocol
edge records the topology link that carries it (``link``) when both ends
sit on the two sides of one link, so the GUI can draw it along the cable.
"""

import ipaddress
import itertools
import logging
from dataclasses import dataclass
from typing import Optional

from ..ifmap import canonical, naming_for, parse as parse_ifname
from ..topology import SPECIAL_ENDPOINT_NODES, Topology, _relative_file
from .parse import MAX_CONFIG_BYTES, Interface, NodeConfig, looks_like_ios, parse_config

logger = logging.getLogger(__name__)


# --- Reading configs ----------------------------------------------------------

def node_config_text(topo: Topology, name: str) -> tuple[Optional[str], str]:
    """A node's startup-config text, or (None, why not)."""
    value = topo.effective_node(name).get("startup-config")
    if not value:
        return None, "no startup-config"
    if not isinstance(value, str):
        return None, "startup-config is not text"
    if "\n" in value:
        return value, ""
    rel = _relative_file(value, name)
    if rel is None:
        return None, f"startup-config {value} is not a file next to the topology"
    base = topo.base_dir.resolve()
    path = (base / rel).resolve()
    if not path.is_relative_to(base):
        return None, f"startup-config {value} is outside the topology directory"
    try:
        with open(path, "rb") as fh:
            return fh.read(MAX_CONFIG_BYTES).decode("utf-8", "replace"), ""
    except OSError as exc:
        return None, f"cannot read {value}: {exc.strerror or exc}"


def config_iface(kind: str, endpoint: str) -> str:
    """Name a node's OS uses for a link endpoint (cEOS ``eth1`` -> ``Ethernet1``).

    Endpoints already written the OS's way (containerlab interface aliases)
    are kept.
    """
    naming = naming_for(kind)
    try:
        name = naming.config_name(endpoint)
        parsed = parse_ifname(name)
        if parsed is not None and naming.native(parsed) == endpoint:
            return canonical(name)
    except (ValueError, IndexError):
        pass
    return canonical(endpoint)


# --- Shared index -------------------------------------------------------------

@dataclass
class _Addr:
    node: str
    iface: Interface
    addr: ipaddress.IPv4Interface


class _Lab:
    def __init__(self, topo: Topology):
        self.topo = topo
        self.configs: dict[str, NodeConfig] = {}
        self.unparsed: list[dict] = []
        self.problems: list[dict] = []
        for name in topo.nodes:
            kind = topo.effective_node(name).get("kind", "")
            text, why = node_config_text(topo, name)
            if text is None:
                self.unparsed.append({"node": name, "reason": why})
            elif not looks_like_ios(text):
                self.unparsed.append({"node": name, "reason": f"config format of kind '{kind}' not supported yet"})
            else:
                try:
                    self.configs[name] = parse_config(text)
                except Exception as exc:  # a parser bug must not hide the other nodes
                    logger.exception("Parsing %s's config failed", name)
                    self.unparsed.append({"node": name, "reason": f"parse error: {exc}"})

        # (node, canonical config interface) -> link id, and the far end
        self.link_of: dict[tuple[str, str], str] = {}
        self.far_end: dict[tuple[str, str], tuple[str, str]] = {}
        for link in topo.links:
            raw_eps = link.raw.get("endpoints") or []
            if len(link.endpoints) != 2 or any(
                    str(ep).partition(":")[0] in SPECIAL_ENDPOINT_NODES for ep in raw_eps
                    if isinstance(ep, str)):
                continue
            ends = []
            for ep in link.endpoints:
                kind = topo.effective_node(ep.node).get("kind", "")
                ends.append((ep.node, config_iface(kind, ep.interface)))
            for (n1, i1), (n2, i2) in (ends, ends[::-1]):
                self.link_of[(n1, i1)] = link.link_id
                self.far_end[(n1, i1)] = (n2, i2)

        # IPv4 address -> owners (several owners is a problem, kept for reporting)
        self.owners: dict[str, list[_Addr]] = {}
        for name, cfg in self.configs.items():
            for iface, addr in cfg.owned_addresses():
                self.owners.setdefault(str(addr.ip), []).append(_Addr(name, iface, addr))
        # MLAG peers: node -> its peer (both point at each other)
        self.mlag_peer: dict[str, str] = {}
        for name, cfg in self.configs.items():
            peer = self.owner(cfg.mlag.peer_address) if cfg.mlag and cfg.mlag.peer_address else None
            other = self.configs.get(peer.node) if peer else None
            if peer and peer.node != name and other and other.mlag:
                back = self.owner(other.mlag.peer_address)
                if back and back.node == name:
                    self.mlag_peer[name] = peer.node
        for ip, owners in sorted(self.owners.items()):
            nodes = sorted({o.node for o in owners})
            if len(owners) > 1 and len({o.iface.vrf for o in owners}) == 1 \
                    and not all(o.iface.anycast for o in owners) and not self.shared_vtep(owners):
                where = ", ".join(f"{o.node} {o.iface.name}" for o in owners)
                self.problem("ip", nodes[0], f"{ip} is configured on {where}")

    def shared_vtep(self, owners: list) -> bool:
        """Is this the VTEP source address an MLAG pair shares (as it must)?"""
        nodes = {o.node for o in owners}
        if len(owners) != 2 or len(nodes) != 2:
            return False
        a, b = sorted(nodes)
        if self.mlag_peer.get(a) != b:
            return False
        return all((v := self.configs[o.node].vtep) and o.iface.name == canonical(v.source_interface)
                   for o in owners)

    def problem(self, protocol: str, node: str, message: str, severity: str = "warning") -> None:
        self.problems.append({"protocol": protocol, "node": node, "severity": severity, "message": message})

    def owner(self, ip: str, vrf: Optional[str] = None) -> Optional[_Addr]:
        owners = self.owners.get(ip) or []
        if vrf is not None:
            owners = [o for o in owners if o.iface.vrf == vrf] or owners
        return owners[0] if owners else None

    def link_between(self, a: str, ia: str, b: str, ib: str) -> Optional[str]:
        link = self.link_of.get((a, ia))
        return link if link and self.far_end.get((a, ia)) == (b, ib) else None

    def connected(self, node: str, ip: ipaddress.IPv4Address, vrf: str = "") -> Optional[Interface]:
        """The node's interface whose subnet contains ``ip``, if any."""
        cfg = self.configs.get(node)
        if not cfg:
            return None
        for iface in cfg.interfaces.values():
            if iface.vrf != vrf or iface.shutdown:
                continue
            for addr in iface.addresses:
                if ip in addr.network and ip != addr.ip:
                    return iface
        return None

    def router_id(self, node: str, configured: str) -> tuple[str, bool]:
        if configured:
            return configured, True
        return self.configs[node].derived_router_id(), False


@dataclass
class _OspfIf:
    """An interface that runs OSPF and can form adjacencies (not passive, not shut)."""
    node: str
    iface: Interface
    area: int

    @property
    def addr(self) -> ipaddress.IPv4Interface:
        return self.iface.primary

    def end(self) -> dict:
        return {"node": self.node, "iface": self.iface.name, "ip": str(self.addr.ip),
                "cost": self.iface.ospf_cost}


def _area_name(area: int) -> str:
    return str(area) if area < 2 ** 16 else str(ipaddress.IPv4Address(area))


# --- OSPF ---------------------------------------------------------------------

def _ospf(lab: _Lab) -> Optional[dict]:
    nodes: dict[str, dict] = {}
    members: list[_OspfIf] = []
    for name, cfg in lab.configs.items():
        procs = cfg.ospf
        if not procs and not any(i.ospf_area is not None for i in cfg.interfaces.values()):
            continue
        ifaces = []
        areas: set[int] = set()
        rid_cfg = next((p.router_id for p in procs if p.router_id and not p.vrf), "")
        for iface in cfg.interfaces.values():
            if not iface.primary:
                continue
            proc = next((p for p in procs if p.vrf == iface.vrf and
                         (not iface.ospf_process or p.pid == iface.ospf_process)), None)
            area = iface.ospf_area
            if area is None and proc:
                area = proc.area_for(iface.primary.ip)
            if area is None:
                continue
            passive = proc.is_passive(iface) if proc else bool(iface.ospf_passive)
            areas.add(area)
            ifaces.append({
                "name": iface.name, "ip": str(iface.primary), "area": _area_name(area),
                "cost": iface.ospf_cost, "network": iface.ospf_network,
                "passive": passive or iface.is_loopback, "shutdown": iface.shutdown,
                "vrf": iface.vrf,
            })
            if not (passive or iface.is_loopback or iface.shutdown):
                members.append(_OspfIf(name, iface, area))
        if not ifaces:
            continue
        rid, configured = lab.router_id(name, rid_cfg)
        nodes[name] = {
            "router_id": rid, "router_id_configured": configured,
            "areas": [_area_name(a) for a in sorted(areas)],
            "abr": len(areas) > 1 and 0 in areas,
            "processes": [p.pid for p in procs],
            "interfaces": ifaces,
        }
    if not nodes:
        return None

    # Adjacencies: OSPF interfaces sharing a subnet (and VRF)
    by_subnet: dict[tuple, list[_OspfIf]] = {}
    for m in members:
        by_subnet.setdefault((m.iface.vrf, m.addr.network), []).append(m)
    adjacencies = []
    for (vrf, subnet), group in sorted(by_subnet.items(),
                                       key=lambda kv: (kv[0][0], int(kv[0][1].network_address))):
        for a, b in itertools.combinations(sorted(group, key=lambda m: m.node), 2):
            if a.node == b.node:
                continue
            adj = {
                "a": a.end(), "b": b.end(),
                "area": _area_name(a.area) if a.area == b.area else None,
                "subnet": str(subnet), "vrf": vrf,
                "link": lab.link_between(a.node, a.iface.name, b.node, b.iface.name),
                "problems": [],
            }
            if a.area != b.area:
                adj["problems"].append(
                    f"area mismatch on {subnet}: {a.node} {a.iface.name} is in area "
                    f"{_area_name(a.area)}, {b.node} {b.iface.name} in area {_area_name(b.area)}")
            type_a = a.iface.ospf_network or "broadcast"
            type_b = b.iface.ospf_network or "broadcast"
            if type_a != type_b:
                adj["problems"].append(
                    f"network type differs on {subnet}: {a.node} {a.iface.name} is {type_a}, "
                    f"{b.node} {b.iface.name} is {type_b}")
            for msg in adj["problems"]:
                lab.problem("ospf", a.node, msg)
            adjacencies.append(adj)

    # Cabled neighbours that cannot form an adjacency with an OSPF interface
    active = {(m.node, m.iface.name): m for m in members}
    for (node, name), m in sorted(active.items()):
        far = lab.far_end.get((node, name))
        if not far:
            continue
        other = active.get(far)
        far_cfg = lab.configs.get(far[0])
        far_iface = far_cfg.interface(far[1]) if far_cfg else None
        if other is not None:
            if node < far[0] and other.addr.network != m.addr.network:
                lab.problem("ospf", node, f"{node} {name} ({m.addr}) and {far[0]} {far[1]} "
                                          f"({other.addr}) are not in the same subnet")
        elif far_cfg is None:
            continue  # the far end's config is not known
        elif far_iface is None or not far_iface.primary:
            lab.problem("ospf", node, f"{node} {name} runs OSPF but {far[0]} {far[1]} has no IPv4 address")
        elif far_iface.primary.network != m.addr.network:
            lab.problem("ospf", node, f"{node} {name} ({m.addr}) and {far[0]} {far[1]} "
                                      f"({far_iface.primary}) are not in the same subnet")
        elif far[0] not in nodes:
            lab.problem("ospf", node, f"{node} {name} sends OSPF hellos to {far[0]}, which runs "
                                      f"no OSPF (passive-interface?)", "info")
        else:
            lab.problem("ospf", node, f"{node} {name} runs OSPF but {far[0]} {far[1]} does not")

    areas = sorted({a for n in nodes.values() for a in n["areas"]}, key=lambda s: (len(s), s))
    return {"nodes": nodes, "adjacencies": _number(adjacencies, "ospf"), "areas": areas}


# --- BGP ----------------------------------------------------------------------

@dataclass
class _Peering:
    node: str
    nb: object            # BgpNeighbor
    ip: str               # address the node peers with
    remote: Optional[_Addr]
    remote_as: str
    families: list[str]
    source: Optional[str]  # address this node sources from, when known


def _bgp(lab: _Lab) -> Optional[dict]:
    nodes: dict[str, dict] = {}
    peerings: list[_Peering] = []
    for name, cfg in lab.configs.items():
        bgp = cfg.bgp
        if not bgp:
            continue
        rid, configured = lab.router_id(name, bgp.router_id)
        nodes[name] = {"asn": bgp.asn, "router_id": rid, "router_id_configured": configured,
                       "networks": bgp.networks, "neighbors": 0}
        for nb in bgp.peers():
            try:
                ipaddress.IPv4Address(nb.key)
            except ValueError:
                continue  # IPv6, prefix-based (dynamic) or interface neighbours
            nodes[name]["neighbors"] += 1
            remote = lab.owner(nb.key, nb.vrf)
            update_source = bgp.resolved(nb, "update_source")
            source = None
            if update_source:
                src_iface = cfg.interface(update_source)
                source = str(src_iface.primary.ip) if src_iface and src_iface.primary else None
            else:
                conn = lab.connected(name, ipaddress.IPv4Address(nb.key), nb.vrf)
                source = str(conn.primary.ip) if conn and conn.primary else None
            peerings.append(_Peering(name, nb, nb.key, remote, bgp.resolved(nb, "remote_as"),
                                     bgp.families(nb), source))
    if not nodes:
        return None

    # Pair the two sides of each session
    sessions = []
    paired: set[int] = set()
    for i, p in enumerate(peerings):
        if i in paired:
            continue
        paired.add(i)
        partner = None
        if p.remote and p.remote.node in nodes and p.remote.node != p.node:
            for j, q in enumerate(peerings):
                if j in paired or q.node != p.remote.node or not q.remote or q.remote.node != p.node:
                    continue
                if (p.source and q.ip != p.source) or (q.source and p.ip != q.source):
                    continue
                partner = j
                break
        if partner is not None:
            paired.add(partner)
        sessions.append(_session(lab, nodes, p, peerings[partner] if partner is not None else None))

    externals = {}
    for s in sessions:
        if s["external"]:
            externals[s["b"]["node"]] = {"ip": s["b"]["ip"], "asn": s["b"]["asn"]}
    return {
        "nodes": nodes,
        "sessions": _number(sessions, "bgp"),
        "external": externals,
        "asns": sorted({n["asn"] for n in nodes.values()}, key=lambda a: (len(a), a)),
    }


def _session(lab: _Lab, nodes: dict, p: _Peering, q: Optional[_Peering]) -> dict:
    asn_a = nodes[p.node]["asn"]
    problems = []
    if p.remote is None:
        far = f"ext:{p.ip}"
        b = {"node": far, "iface": "", "ip": p.ip, "asn": p.remote_as}
        external = True
    else:
        b = {"node": p.remote.node, "iface": p.remote.iface.name, "ip": p.ip,
             "asn": nodes.get(p.remote.node, {}).get("asn", "")}
        external = False
    a_owner = lab.owner(q.ip) if q else (lab.owner(p.source) if p.source else None)
    a_iface = a_owner.iface if a_owner and a_owner.node == p.node else None
    a = {"node": p.node, "iface": a_iface.name if a_iface else "",
         "ip": q.ip if q else (p.source or ""), "asn": asn_a}

    families = sorted(set(p.families) & set(q.families)) if q else p.families
    ebgp = (b["asn"] or p.remote_as) != asn_a
    session = {
        "a": a, "b": b, "type": "ebgp" if ebgp else "ibgp",
        "families": families, "external": external,
        "configured": "both" if q else "one-sided",
        "multihop": False, "link": None, "peer_groups": [g for g in (p.nb.peer_group, q.nb.peer_group if q else "") if g],
        "description": p.nb.description, "shutdown": p.nb.shutdown or bool(q and q.nb.shutdown),
        "problems": problems,
    }
    if external:
        return session

    remote_node = p.remote.node
    if remote_node not in nodes:
        problems.append(f"{p.node} peers with {remote_node} ({p.ip}), which has no BGP configured")
    elif q is None:
        problems.append(f"{p.node} peers with {remote_node} at {p.ip}, but {remote_node} has no "
                        f"matching neighbor for {p.node}" + (f" ({p.source})" if p.source else ""))
    for side, other_asn in ((p, b["asn"]), (q, asn_a)):
        if side and side.remote_as and other_asn and side.remote_as != other_asn \
                and side.remote_as not in ("internal", "external"):
            problems.append(f"{side.node} expects AS {side.remote_as} for {side.ip}, "
                            f"but that router is in AS {other_asn}")
    if q is not None:
        if set(p.families) != set(q.families):
            problems.append(f"address families differ: {p.node} {', '.join(p.families) or 'none'}, "
                            f"{q.node} {', '.join(q.families) or 'none'}")
        b_iface = p.remote.iface
        session["link"] = lab.link_between(p.node, a_iface.name, remote_node, b_iface.name) if a_iface else None
        direct = lab.connected(p.node, ipaddress.IPv4Address(p.ip)) is not None
        session["multihop"] = not direct
        for side, iface in ((p, a_iface), (q, b_iface)):
            if iface is None:
                continue
            side_bgp = lab.configs[side.node].bgp
            us = side_bgp.resolved(side.nb, "update_source")
            peer_ip = ipaddress.IPv4Address(side.ip)
            if not us and lab.connected(side.node, peer_ip) is None and iface.name:
                problems.append(f"{side.node} must source the session from {iface.name} "
                                f"(update-source) because its peer expects {iface.primary.ip if iface.primary else '?'}")
            if ebgp and not direct and not side_bgp.resolved(side.nb, "ebgp_multihop"):
                problems.append(f"{side.node}: eBGP to {side.ip} is not directly connected "
                                f"and has no ebgp-multihop")
    for msg in problems:
        lab.problem("bgp", p.node, msg)
    return session


# --- EVPN ---------------------------------------------------------------------

def _evpn(lab: _Lab, bgp: Optional[dict]) -> Optional[dict]:
    vteps: dict[str, dict] = {}
    for name, cfg in lab.configs.items():
        v = cfg.vtep
        if not v:
            continue
        src = cfg.interface(v.source_interface) if v.source_interface else None
        src_ip = str(src.primary.ip) if src and src.primary else ""
        rts = cfg.bgp
        l2 = [{"vni": vni, "vlan": vlan,
               **(rts.vlans[vlan].view() if rts and vlan in rts.vlans else {"rd": "", "import": [], "export": []})}
              for vni, vlan in sorted(v.l2_vnis.items())]
        l3 = [{"vni": vni, "vrf": vrf,
               **(rts.vrfs[vrf].view() if rts and vrf in rts.vrfs else {"rd": "", "import": [], "export": []})}
              for vni, vrf in sorted(v.l3_vnis.items())]
        vteps[name] = {
            "interface": v.interface, "source_interface": v.source_interface, "ip": src_ip,
            "udp_port": v.udp_port, "l2_vnis": l2, "l3_vnis": l3, "flood_list": v.flood_list,
            # The MLAG peer it is one VTEP with (same source address)
            "mlag_peer": lab.mlag_peer.get(name, ""),
        }
        if not v.source_interface:
            lab.problem("evpn", name, f"{name} {v.interface} has no source-interface")
        elif not src_ip:
            lab.problem("evpn", name, f"{name} VTEP source {v.source_interface} has no IPv4 address")

    sessions = [s["id"] for s in (bgp or {}).get("sessions", []) if "evpn" in s["families"]]
    if not vteps and not sessions:
        return None

    # VNI catalogue
    vnis: dict[int, dict] = {}
    for name, vt in vteps.items():
        for e in vt["l2_vnis"]:
            entry = vnis.setdefault(e["vni"], {"vni": e["vni"], "type": "l2", "members": [], "vlans": [], "vrfs": []})
            entry["members"].append(name)
            if e["vlan"] and e["vlan"] not in entry["vlans"]:
                entry["vlans"].append(e["vlan"])
        for e in vt["l3_vnis"]:
            entry = vnis.setdefault(e["vni"], {"vni": e["vni"], "type": "l3", "members": [], "vlans": [], "vrfs": []})
            entry["members"].append(name)
            if e["vrf"] and e["vrf"] not in entry["vrfs"]:
                entry["vrfs"].append(e["vrf"])
    for vni, entry in sorted(vnis.items()):
        if len(entry["members"]) == 1:
            lab.problem("evpn", entry["members"][0],
                        f"VNI {vni} is only configured on {entry['members'][0]}", "info")

    # Data plane: a VXLAN tunnel between every pair of VTEPs sharing a VNI
    tunnels = []
    for a, b in itertools.combinations(sorted(vteps), 2):
        if vteps[a]["ip"] and vteps[a]["ip"] == vteps[b]["ip"]:
            continue  # one VTEP (an MLAG pair): no tunnel between its halves
        shared = sorted(vni for vni, e in vnis.items() if a in e["members"] and b in e["members"])
        if shared:
            tunnels.append({"a": {"node": a, "ip": vteps[a]["ip"]}, "b": {"node": b, "ip": vteps[b]["ip"]},
                            "vnis": shared, "problems": []})

    # Control plane: VTEPs need an EVPN session (or a static flood list)
    evpn_nodes = set()
    for s in (bgp or {}).get("sessions", []):
        if s["id"] in sessions:
            evpn_nodes.update((s["a"]["node"], s["b"]["node"]))
    for name, vt in vteps.items():
        if name not in evpn_nodes and not vt["flood_list"]:
            lab.problem("evpn", name, f"{name} is a VTEP but has no BGP EVPN session or flood list")
    # Route-target checks for L2 VNIs with explicit RTs on every member
    for vni, entry in sorted(vnis.items()):
        rt_sets = []
        for name in entry["members"]:
            rows = vteps[name]["l2_vnis" if entry["type"] == "l2" else "l3_vnis"]
            row = next(r for r in rows if r["vni"] == vni)
            rt_sets.append((name, set(row["import"]), set(row["export"])))
        if all(imp or exp for _, imp, exp in rt_sets) and len(rt_sets) > 1:
            for (n1, imp1, _), (n2, _, exp2) in itertools.permutations(rt_sets, 2):
                if imp1 and exp2 and not imp1 & exp2:
                    lab.problem("evpn", n1, f"VNI {vni}: {n1} imports none of the route targets "
                                            f"{n2} exports ({', '.join(sorted(exp2))})")

    return {
        "vteps": vteps,
        "sessions": sessions,
        "speakers": sorted(evpn_nodes),
        "vnis": [vnis[k] for k in sorted(vnis)],
        "tunnels": _number(tunnels, "vxlan"),
    }


def _mlag(lab: _Lab, evpn: Optional[dict]) -> Optional[dict]:
    """MLAG pairs (EOS): who pairs with whom, over which peer-link, which
    ports are dual-homed (``mlag <n>``), and whether both sides agree."""
    nodes = {name: cfg for name, cfg in lab.configs.items() if cfg.mlag}
    if not nodes:
        return None
    pairs, unpaired = [], []
    for name, cfg in sorted(nodes.items()):
        m = cfg.mlag
        peer = lab.mlag_peer.get(name)
        if peer:
            continue  # checked as a pair below
        unpaired.append(name)
        if not m.peer_address:
            lab.problem("mlag", name, f"{name} has an MLAG configuration without a peer-address")
            continue
        owner = lab.owner(m.peer_address)
        if not owner:
            lab.problem("mlag", name, f"{name} MLAG peer-address {m.peer_address} is not configured on any node")
        elif owner.node == name:
            lab.problem("mlag", name, f"{name} MLAG peer-address {m.peer_address} is its own address")
        elif owner.node not in nodes:
            lab.problem("mlag", name, f"{name} MLAG peer-address {m.peer_address} is {owner.node} "
                                      f"{owner.iface.name}, which has no MLAG configuration")
        else:
            back = lab.owner(nodes[owner.node].mlag.peer_address)
            lab.problem("mlag", name, f"{name} names {owner.node} as its MLAG peer, but {owner.node}'s "
                                      f"peer-address ({nodes[owner.node].mlag.peer_address or 'none'}) is "
                                      f"{back.node if back else 'not a lab address'}")

    vteps = (evpn or {}).get("vteps") or {}
    for a, b in sorted({tuple(sorted(p)) for p in lab.mlag_peer.items()}):
        side = {}
        for name in (a, b):
            cfg = lab.configs[name]
            m = cfg.mlag
            local = cfg.interface(m.local_interface) if m.local_interface else None
            members = cfg.port_channel_members(m.peer_link) if m.peer_link else []
            side[name] = {
                "node": name, "domain": m.domain_id, "local_interface": m.local_interface,
                "address": str(local.primary.ip) if local and local.primary else "",
                "peer_address": m.peer_address, "peer_link": m.peer_link, "peer_link_members": members,
                "shutdown": m.shutdown,
            }
            if not m.local_interface:
                lab.problem("mlag", name, f"{name} MLAG has no local-interface")
            elif not (local and local.primary):
                lab.problem("mlag", name, f"{name} MLAG local-interface {m.local_interface} has no IPv4 address")
            if not m.peer_link:
                lab.problem("mlag", name, f"{name} MLAG has no peer-link")
            elif not members:
                lab.problem("mlag", name, f"{name} MLAG peer-link {m.peer_link} has no member interfaces")
            if m.shutdown:
                lab.problem("mlag", name, f"{name} MLAG is shut down", "info")
        # Peer-link: member links that reach the peer
        peer_links = []
        for name, other in ((a, b), (b, a)):
            reach = [lab.link_of[(name, i)] for i in side[name]["peer_link_members"]
                     if lab.far_end.get((name, i), ("", ""))[0] == other]
            if side[name]["peer_link_members"] and not reach:
                lab.problem("mlag", name, f"{name} peer-link {side[name]['peer_link']} has no link to {other}")
            peer_links += [x for x in reach if x not in peer_links]
        if side[a]["domain"] != side[b]["domain"]:
            lab.problem("mlag", a, f"MLAG peers {a} and {b} have different domain-ids "
                                   f"({side[a]['domain'] or 'none'}, {side[b]['domain'] or 'none'})", "error")
        # Dual-homed ports: "mlag <n>" on both sides, same VLAN
        ports_of = {}
        for name in (a, b):
            cfg = lab.configs[name]
            ports_of[name] = {i.mlag_id: i for i in cfg.interfaces.values() if i.mlag_id is not None}
        ports = []
        for mid in sorted(set(ports_of[a]) | set(ports_of[b])):
            pa, pb = ports_of[a].get(mid), ports_of[b].get(mid)
            if not (pa and pb):
                only = a if pa else b
                lab.problem("mlag", only, f"mlag {mid} is configured on {only} only, not on "
                                          f"{b if only == a else a}")
            if pa and pb and pa.access_vlan != pb.access_vlan:
                lab.problem("mlag", a, f"mlag {mid}: VLAN {pa.access_vlan or 'trunk'} on {a}, "
                                       f"{pb.access_vlan or 'trunk'} on {b}")
            behind = set()
            for name, port in ((a, pa), (b, pb)):
                if port:
                    for member in lab.configs[name].port_channel_members(port.name):
                        far = lab.far_end.get((name, member))
                        if far:
                            behind.add(far[0])
            ports.append({"mlag": mid, "a": pa.name if pa else "", "b": pb.name if pb else "",
                          "vlan": (pa or pb).access_vlan, "hosts": sorted(behind)})
        # VXLAN: a pair is one VTEP, so both must source it from one address
        va, vb = vteps.get(a), vteps.get(b)
        vtep_ip = ""
        if va and vb:
            if va["ip"] != vb["ip"]:
                lab.problem("mlag", a, f"MLAG peers {a} and {b} are VTEPs with different source addresses "
                                       f"({va['ip'] or 'none'}, {vb['ip'] or 'none'}); they should share one")
            else:
                vtep_ip = va["ip"]
        elif va or vb:
            lab.problem("mlag", a, f"Only {a if va else b} of the MLAG pair {a} / {b} is a VTEP")
        pairs.append({"domain": side[a]["domain"] or side[b]["domain"], "nodes": [a, b],
                      "a": side[a], "b": side[b], "peer_links": peer_links, "ports": ports, "vtep": vtep_ip})
    return {"pairs": _number(pairs, "mlag"), "unpaired": unpaired}


def _number(items: list[dict], prefix: str) -> list[dict]:
    for i, item in enumerate(items):
        item["id"] = f"{prefix}{i}"
    return items


# --- Entry point --------------------------------------------------------------

def routing_view(topo: Topology) -> dict:
    """Intended OSPF / BGP / EVPN views of a topology, from its startup configs."""
    lab = _Lab(topo)
    ospf = _ospf(lab)
    bgp = _bgp(lab)
    evpn = _evpn(lab, bgp)
    mlag = _mlag(lab, evpn)
    protocols = [name for name, view in (("ospf", ospf), ("bgp", bgp), ("evpn", evpn), ("mlag", mlag)) if view]
    return {
        "name": topo.name,
        "protocols": protocols,
        "ospf": ospf,
        "bgp": bgp,
        "evpn": evpn,
        "mlag": mlag,
        # Port-channel members per node: traffic out of a bundle leaves on them
        "port_channels": {name: pcs for name, cfg in sorted(lab.configs.items())
                          if (pcs := {i.name: cfg.port_channel_members(i.name) for i in cfg.interfaces.values()
                                      if i.name.lower().startswith("port-channel")})},
        "unparsed": lab.unparsed,
        "problems": lab.problems,
        # Which node owns each address (anycast gateways left out): resolves
        # live peers that the startup configs do not mention
        "addresses": {ip: {"node": owners[0].node, "iface": owners[0].iface.name}
                      for ip, owners in sorted(lab.owners.items())
                      if not any(o.iface.anycast for o in owners)},
    }
