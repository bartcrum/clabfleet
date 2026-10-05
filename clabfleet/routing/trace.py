"""Path trace: the hops traffic to an address takes through a running lab.

Walks the routing tables the way someone at the CLI would: on each node,
look the destination up, follow every next hop (ECMP branches) to the
node behind that interface, and go on until a node owns the address.

- cEOS (``show ip route vrf all <dst> | json``): the VRF the trace is in.
  Traffic routed to the node arrives in the VRF of the interface it was
  sent to, the one with the sender's next-hop address on its subnet, and
  is looked up there only: a route in another VRF does not carry it. An
  address on a connected subnet that is the node's own (a gateway, also a
  virtual one) is reached there. Only
  where the trace starts, or where the VRF cannot be told, the VRF with
  the most specific route is taken. A route via a VTEP is a VXLAN
  hop to that VTEP (an EVPN overlay hop) in the same VRF. A connected
  route on a VLAN interface is finished at layer 2: ARP (or the MAC the
  sending host had) gives the port behind the VLAN, or Vxlan1 and the
  VXLAN table the remote VTEP, which bridges it to its own port. When the
  MAC has aged out of the tables, its EVPN MAC/IP route names the VTEP;
  failing that the VLAN's flood list does, and a VTEP the destination
  node hangs off sends it down that link.
- Linux (``ip route get <dst>``): the gateway's interface, or the
  connected interface.
- IOS kinds (``show ip route <dst>`` text): next hops and their
  interfaces.

``ask(node, command)`` runs a command on a node and returns its output;
nodes are described by the topology view and the containers. The walk
stops after ``MAX_HOPS`` and never visits a node twice in one VRF.
"""

import ipaddress
import json
import re
from collections import deque
from typing import Callable, Optional

from ..livestate import linux_iface_names
from .live import family

MAX_HOPS = 16
LINUX = {"linux"}


class TraceError(Exception):
    pass


def same_iface(kind: str, topo_iface: str, os_iface: str) -> bool:
    """Is ``os_iface`` (as the node's OS names it) the topology's ``topo_iface``?"""
    a = set(linux_iface_names(kind, topo_iface)[0]) | {topo_iface}
    b = set(linux_iface_names(kind, os_iface)[0]) | {os_iface}
    return bool({x.lower() for x in a} & {x.lower() for x in b})


class Lab:
    """What the walk needs to know about the lab."""

    def __init__(self, view: dict, kinds: dict[str, str], vteps: dict[str, list[str]],
                 port_channels: Optional[dict[str, dict[str, list[str]]]] = None):
        self.links = [link for link in view["links"] if link["a"].get("node") and link["b"].get("node")]
        self.kinds = kinds    # node -> kind
        self.vteps = vteps    # VTEP address -> its nodes (two for an MLAG pair)
        self.port_channels = port_channels or {}  # node -> Port-channel<n> -> member interfaces

    def members(self, node: str, os_iface: str) -> list[str]:
        """The member interfaces of a port-channel (from the config), else []."""
        from ..ifmap import canonical
        return (self.port_channels.get(node) or {}).get(canonical(os_iface), [])

    def link_to(self, node: str, peer: str) -> Optional[str]:
        """``node``'s topology interface on a link to ``peer``."""
        for link in self.links:
            for me, other in ((link["a"], link["b"]), (link["b"], link["a"])):
                if me["node"] == node and other["node"] == peer:
                    return me["iface"]
        return None

    def neighbour(self, node: str, os_iface: str) -> Optional[tuple[str, str, str]]:
        """(peer node, my topology iface, peer iface) behind ``os_iface``."""
        kind = self.kinds.get(node, "")
        for link in self.links:
            for me, other in ((link["a"], link["b"]), (link["b"], link["a"])):
                if me["node"] == node and same_iface(kind, me["iface"], os_iface):
                    return other["node"], me["iface"], other["iface"]
        return None


# --- per-kind lookups: each returns ("reached" | "next" | "none", [next hops], text)
# A next hop: {"iface": OS name} or {"vtep": address, "vni": n}; "l2": True
# when the hop is the last one at layer 2 (bridged to the address)

def _eos_mac(mac: str) -> str:
    """aa:c1:ab:8f:40:85 -> aac1.ab8f.4085 (EOS's way)."""
    digits = mac.replace(":", "").replace(".", "").lower()
    return ".".join(digits[i:i + 4] for i in (0, 4, 8)) if len(digits) == 12 else mac


def _evpn_vteps(ask, node: str, want: str) -> list:
    """VTEPs advertising an EVPN MAC/IP route for ``want`` (a MAC or an IP).
    EOS does not filter these by address reliably, so all are read."""
    data = json.loads(ask(node, "show bgp evpn route-type mac-ip | json"))
    hops, seen = [], set()
    for key, route in (data.get("evpnRoutes") or {}).items():
        words = key.split()
        if "mac-ip" not in words or want not in words[words.index("mac-ip") + 1:]:
            continue
        mac = words[words.index("mac-ip") + 1]
        for path in route.get("evpnRoutePaths") or []:
            vtep = path.get("nextHop")
            if vtep and vtep not in seen:
                seen.add(vtep)
                hops.append({"vtep": vtep, "l2": True, "mac": mac})
    return hops


def _eos_flood(ask, node: str, vlan_iface: str) -> list:
    """Last resort for an unknown address on a VLAN: the VTEPs it is flooded to."""
    vlan = vlan_iface.lower().removeprefix("vlan")
    data = json.loads(ask(node, f"show vxlan flood vtep vlan {vlan} | json"))
    vteps = ((data.get("floodMap") or {}).get(vlan) or {}).get("vteps") or []
    return [{"vtep": v, "l2": True, "flood": True} for v in vteps]


def _eos_ingress_vrf(ask, node: str, gateway: str) -> Optional[str]:
    """The VRF traffic sent to ``gateway`` arrives in on ``node``: the one
    whose connected subnet has that address. None if it cannot be told (no
    such subnet, or the same one in several VRFs)."""
    try:
        data = json.loads(ask(node, f"show ip route vrf all {gateway} | json"))
    except Exception:  # noqa: BLE001 - no answer: the lookup that follows says so
        return None
    found = [name for name, v in (data.get("vrfs") or {}).items()
             if any(r.get("directlyConnected") or r.get("routeType") == "connected"
                    for r in (v.get("routes") or {}).values())]
    return found[0] if len(found) == 1 else None


def _eos_owns(ask, node: str, iface: str, dst: str) -> bool:
    """Is ``dst`` one of the node's own addresses on ``iface`` (primary,
    secondary or virtual, as an anycast gateway is)?"""
    try:
        data = json.loads(ask(node, f"show ip interface {iface} | json"))
    except Exception:  # noqa: BLE001 - no answer: go on as for any address on the subnet
        return False
    found = set()

    def walk(value) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("address"), str):
                found.add(value["address"])
            for key, inner in value.items():
                if key in ("secondaryIps", "virtualSecondaryIps") and isinstance(inner, dict):
                    found.update(inner)  # keyed by address
                walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)

    for entry in (data.get("interfaces") or {}).values():
        walk(entry.get("interfaceAddress"))
        walk(entry.get("interfaceAddressBrief"))
    return dst in found


def _eos_lookup(ask, node: str, dst: str, vrf: Optional[str],
                mac: Optional[str] = None) -> tuple[str, list, str, Optional[str]]:
    data = json.loads(ask(node, f"show ip route vrf all {dst} | json"))
    best = None
    for name, v in (data.get("vrfs") or {}).items():
        if vrf and name != vrf:
            continue
        for prefix, route in (v.get("routes") or {}).items():
            length = int(prefix.split("/")[1]) if "/" in prefix else 32
            if best is None or length > best[0]:
                best = (length, name, prefix, route)
    if best is None:
        return "none", [], f"no route to {dst}" + (f" in VRF {vrf}" if vrf else ""), vrf
    _, vrf, prefix, route = best
    text = f"{prefix} {route.get('routeType', '')}" + (f" (VRF {vrf})" if vrf != "default" else "")
    vias = route.get("vias") or []
    if route.get("directlyConnected") or route.get("routeType") == "connected":
        iface = (vias[0] if vias else {}).get("interface", "")
        if prefix.endswith("/32") or not iface:
            return "reached", [], text, vrf
        if _eos_owns(ask, node, iface, dst):
            return "reached", [], f"{text}: {dst} is {node}'s own address on {iface}", vrf
        if iface.lower().startswith("vlan"):
            hops = _eos_layer2(ask, node, dst, vrf, mac) or _eos_flood(ask, node, iface)
            return "next", hops, text, vrf
        return "next", [{"iface": iface, "l2": True}], text, vrf
    hops = []
    for via in vias:
        if via.get("vtepAddr"):
            hops.append({"vtep": via["vtepAddr"], "vni": via.get("vni")})
        elif via.get("interface"):
            hops.append({"iface": via["interface"], "gw": via.get("nexthopAddr")})
    return ("next" if hops else "none"), hops, text, vrf


def _eos_layer2(ask, node: str, dst: str, vrf: str, mac: Optional[str] = None) -> list:
    """The port (or remote VTEP) behind a VLAN interface for ``dst``. ``mac``:
    the address's MAC if the hop before knew it (a host on the same subnet)."""
    arp = json.loads(ask(node, f"show ip arp vrf {vrf} {dst} | json"))
    entry = next(iter(arp.get("ipV4Neighbors") or []), None)
    if entry:
        # "Vlan20, Ethernet3"; "Vlan20, not learned" once the MAC aged out
        for port in (p.strip() for p in entry.get("interface", "").split(",")[1:]):
            if port and not port.lower().startswith(("vx", "not learned")):
                return [{"iface": port, "l2": True}]
        mac = entry.get("hwAddress") or mac
    if mac:
        # Follow the MAC: a port, or Vxlan1 and its VTEP, or its EVPN route
        found = _eos_bridge(ask, node, mac, remote=True)
        if found:
            return found
    # The EVPN MAC/IP route for the address says which VTEP has it
    return _evpn_vteps(ask, node, dst)


def _eos_bridge(ask, node: str, mac: str, remote: bool = False) -> list:
    """Where a MAC is: its local port, or with ``remote`` the VTEP behind
    Vxlan1 (from the VXLAN address table)."""
    mac = _eos_mac(mac)
    table = json.loads(ask(node, f"show mac address-table address {mac} | json"))
    for e in (table.get("unicastTable") or {}).get("tableEntries") or []:
        if not e.get("interface", "").lower().startswith("vx"):
            return [{"iface": e["interface"], "l2": True}]
    if remote:
        vx = json.loads(ask(node, f"show vxlan address-table address {mac} | json"))
        addr = next(iter(vx.get("addresses") or []), {})
        if addr.get("vteps"):
            return [{"vtep": v, "l2": True, "mac": mac} for v in addr["vteps"]]
        # Aged out of the tables, still in EVPN: the VTEP that advertises it
        return _evpn_vteps(ask, node, mac)
    return []


_LINUX_ROUTE = re.compile(r"(?:via (?P<via>\S+) )?dev (?P<dev>\S+)")


_LLADDR = re.compile(r"lladdr ([0-9a-f:]{17})", re.IGNORECASE)


def _linux_lookup(ask, node: str, dst: str) -> tuple[str, list, str]:
    text = ask(node, f"ip route get {dst}").strip()
    first = text.splitlines()[0] if text else ""
    if first.startswith("local "):
        return "reached", [], first
    m = _LINUX_ROUTE.search(first)
    if not m:
        return "none", [], first or f"no route to {dst}"
    hop = {"iface": m["dev"], "l2": not m["via"], "gw": m["via"]}
    if not m["via"]:  # same subnet: the host knows the address's MAC
        neigh = _LLADDR.search(ask(node, f"ip neigh show {dst}"))
        if neigh:
            hop["mac"] = neigh[1]
    return "next", [hop], first


def _linux_bond_members(ask, node: str, dev: str) -> list[str]:
    """The interfaces enslaved to a Linux bond (``ip -o link show master``)."""
    if not re.fullmatch(r"[\w.-]{1,15}", dev):
        return []
    try:
        text = ask(node, f"ip -o link show master {dev}")
    except Exception:  # noqa: BLE001 - not a bond, or no answer
        return []
    return [m.group(1).split("@")[0] for m in re.finditer(r"^\d+:\s+([^:\s]+):", text, re.M)]


_IOS_VIA = re.compile(r"(\d+\.\d+\.\d+\.\d+)(?:, from [^,]+)?(?:, [^,]+ ago)?, via (\S+)")
_IOS_CONNECTED = re.compile(r"directly connected, via (\S+)")


def _ios_lookup(ask, node: str, dst: str) -> tuple[str, list, str]:
    text = ask(node, f"show ip route {dst}")
    if "% Network not in table" in text or "% Subnet not in table" in text:
        return "none", [], f"no route to {dst}"
    first = next((ln.strip() for ln in text.splitlines() if ln.strip().startswith("Routing entry")), "")
    connected = _IOS_CONNECTED.search(text)
    if connected:
        if "/32" in first or "255.255.255.255" in text:
            return "reached", [], first
        return "next", [{"iface": connected[1], "l2": True}], first
    hops = [{"iface": m[2], "gw": m[1]} for m in _IOS_VIA.finditer(text)]
    return ("next" if hops else "none"), hops, first


def trace(lab: Lab, ask: Callable[[str, str], str], src: str, dst: str,
          dst_node: Optional[str] = None) -> dict:
    """Hops from ``src`` towards address ``dst`` (owned by ``dst_node`` if
    known). Returns {"reached", "hops": [{"node", "vrf", "route", "error"?}],
    "edges": [{"a", "b", "a_iface", "b_iface", "overlay", "vni", "flood"}]}."""
    # IPv4 only, and in its plain form: the address goes into commands on
    # the nodes. (An IPv6 address may carry a scope id of any text, which a
    # shell would run.)
    try:
        dst = str(ipaddress.IPv4Address(dst))
    except ValueError:
        raise TraceError(f"'{dst}' is not an IPv4 address") from None
    hops, edges = [], []
    seen: set[tuple[str, Optional[str]]] = set()
    # node, VRF, MAC to bridge (after an L2 VXLAN hop), MAC known for dst,
    # the address the hop before routed the traffic to (its next hop)
    queue = deque([(src, None, None, None, None)])
    reached = False
    while queue and len(hops) < MAX_HOPS:
        node, vrf, bridge, known_mac, gateway = queue.popleft()
        kind = lab.kinds.get(node, "")
        eos, ios = family(kind) == "eos", family(kind) == "ios"
        if eos and vrf is None and gateway:
            vrf = _eos_ingress_vrf(ask, node, gateway)
        if (node, vrf) in seen:
            continue
        seen.add((node, vrf))
        hop = {"node": node, "vrf": vrf}
        hops.append(hop)
        if node == dst_node:
            hop["route"] = "destination"
            reached = True
            continue
        try:
            if bridge and eos:
                status, nexts, hop["route"] = "next", _eos_bridge(ask, node, bridge), "bridged (VXLAN)"
            elif eos:
                status, nexts, hop["route"], vrf = _eos_lookup(ask, node, dst, vrf, known_mac)
                hop["vrf"] = vrf
            elif kind in LINUX:
                status, nexts, hop["route"] = _linux_lookup(ask, node, dst)
            elif ios:
                status, nexts, hop["route"] = _ios_lookup(ask, node, dst)
            else:
                hop["error"] = f"cannot look up routes on kind '{kind}'"
                continue
        except (TraceError, ValueError, KeyError) as exc:
            hop["error"] = str(exc) or type(exc).__name__
            continue
        except Exception as exc:  # noqa: BLE001 - the node did not answer
            hop["error"] = f"could not ask: {exc}"
            continue
        if status == "reached":
            reached = True
            continue
        if dst_node and eos and all(n.get("flood") for n in nexts):
            # The address is in no table (its MAC aged out): if the destination
            # hangs off this node, that is where the frame goes
            direct = lab.link_to(node, dst_node)
            if direct:
                nexts = [{"iface": direct, "l2": True}]
                hop.setdefault("notes", []).append(f"{dst_node}'s MAC is not learned here; it is on {direct}")
        for nxt in nexts:
            if "vtep" in nxt:
                peers = lab.vteps.get(nxt["vtep"]) or []
                if not peers:
                    hop.setdefault("notes", []).append(f"VTEP {nxt['vtep']} is not a lab node")
                    continue
                if len(peers) > 1:
                    hop.setdefault("notes", []).append(
                        f"VTEP {nxt['vtep']} is the MLAG pair {' + '.join(peers)}: either may take it")
                mac = nxt.get("mac") or (known_mac if nxt.get("flood") else None)
                if nxt.get("l2") and not mac:
                    arp = json.loads(ask(node, f"show ip arp vrf {vrf} {dst} | json"))
                    mac = next(iter(arp.get("ipV4Neighbors") or []), {}).get("hwAddress")
                for peer in peers:
                    if nxt.get("flood") and (peer, vrf) in seen:
                        continue  # never flooded back
                    edges.append({"a": node, "b": peer, "a_iface": "", "b_iface": "", "overlay": True,
                                  "vni": nxt.get("vni"), "flood": bool(nxt.get("flood"))})
                    queue.append((peer, vrf, mac, mac, None))
                continue
            # A bundle (EOS port-channel, Linux bond) leaves on its members:
            # each is a branch (the hash picks one per flow)
            ifaces = [nxt["iface"]]
            if not lab.neighbour(node, nxt["iface"]):
                members = lab.members(node, nxt["iface"])
                if not members and kind in LINUX:
                    members = _linux_bond_members(ask, node, nxt["iface"])
                if members:
                    ifaces = members
                    hop.setdefault("notes", []).append(f"{nxt['iface']} is a bundle of {', '.join(members)}")
            for iface in ifaces:
                behind = lab.neighbour(node, iface)
                if not behind:
                    hop.setdefault("notes", []).append(f"{iface} leads out of the lab")
                    continue
                peer, my_iface, peer_iface = behind
                edges.append({"a": node, "b": peer, "a_iface": my_iface, "b_iface": peer_iface,
                              "overlay": False, "vni": None, "flood": False})
                # Entering a host or a node at layer 2 starts its own lookup;
                # routed to a node, it arrives in the VRF its next hop is in
                queue.append((peer, None, None, nxt.get("mac"), nxt.get("gw")))
    unique = []
    for e in edges:
        if e not in unique:
            unique.append(e)
    return {"reached": reached, "hops": hops, "edges": unique, "truncated": bool(queue)}
