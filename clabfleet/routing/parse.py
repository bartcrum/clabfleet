"""Parse IOS-style configs (Cisco IOS / IOS-XE / NX-OS, Arista EOS) into the
routing facts the logical view needs: interface addresses, OSPF processes,
BGP neighbours and address families, the VXLAN / EVPN setup, and MLAG
(EOS: the ``mlag configuration`` block, port-channels and their ``mlag``
ids).

The parser reads the indentation tree of the config and picks out the
statements it knows, so anything else in the config is simply ignored. It
understands what the configuration says, not what the device would do
with it: defaults are applied only where the view depends on them (OSPF
router-id, IPv4 unicast activation of BGP neighbours).
"""

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Optional

from ..ifmap import canonical

MAX_CONFIG_BYTES = 2 * 1024 * 1024


# --- Indentation tree ---------------------------------------------------------

@dataclass
class Line:
    text: str
    children: list["Line"] = field(default_factory=list)

    @property
    def words(self) -> list[str]:
        return self.text.split()


def config_tree(text: str) -> list[Line]:
    """Top-level statements of an IOS-style config, each with its sub-mode lines."""
    root = Line("")
    stack: list[tuple[int, Line]] = [(-1, root)]
    for raw in text.expandtabs(4).splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith(("!", "#")) or stripped == "end":
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        while stack[-1][0] >= indent:
            stack.pop()
        line = Line(stripped)
        stack[-1][1].children.append(line)
        stack.append((indent, line))
    return root.children


def looks_like_ios(text: str) -> bool:
    """True for configs this parser understands (not Junos, SR Linux JSON, ...)."""
    return bool(re.search(r"^(interface|router|hostname)\s", text, re.MULTILINE))


# --- Facts --------------------------------------------------------------------

@dataclass
class Interface:
    name: str                                   # canonical, e.g. Ethernet1, Loopback0
    addresses: list[ipaddress.IPv4Interface] = field(default_factory=list)
    vrf: str = ""
    shutdown: bool = False
    description: str = ""
    ospf_area: Optional[int] = None             # set on the interface itself
    ospf_process: str = ""                      # process the interface-level area is for
    ospf_cost: Optional[int] = None
    ospf_network: str = ""                      # point-to-point, broadcast, ...
    ospf_passive: Optional[bool] = None         # interface-level passive (NX-OS, EOS)
    anycast: bool = False                       # same gateway address on every leaf (EVPN)
    channel_group: Optional[int] = None         # member of Port-Channel<n>
    mlag_id: Optional[int] = None               # EOS "mlag <n>" on a port-channel
    access_vlan: Optional[int] = None

    @property
    def primary(self) -> Optional[ipaddress.IPv4Interface]:
        return self.addresses[0] if self.addresses else None

    @property
    def is_loopback(self) -> bool:
        return self.name.lower().startswith("loopback")


@dataclass
class OspfNetwork:
    address: int
    wildcard: int
    area: int

    def matches(self, ip: ipaddress.IPv4Address) -> bool:
        return (int(ip) & ~self.wildcard) == (self.address & ~self.wildcard)


@dataclass
class OspfProcess:
    pid: str
    vrf: str = ""
    router_id: str = ""
    networks: list[OspfNetwork] = field(default_factory=list)
    passive_default: bool = False
    passive: set[str] = field(default_factory=set)
    not_passive: set[str] = field(default_factory=set)

    def area_for(self, ip: ipaddress.IPv4Address) -> Optional[int]:
        """Area of the most specific ``network`` statement covering ``ip``."""
        best = None
        for net in self.networks:
            if net.matches(ip) and (best is None or bin(net.wildcard).count("1") < bin(best.wildcard).count("1")):
                best = net
        return best.area if best else None

    def is_passive(self, iface: Interface) -> bool:
        if iface.ospf_passive is not None:
            return iface.ospf_passive
        if self.passive_default:
            return iface.name not in self.not_passive
        return iface.name in self.passive


@dataclass
class BgpNeighbor:
    key: str                                    # address, or peer group name
    is_group: bool = False
    vrf: str = ""
    peer_group: str = ""
    remote_as: str = ""
    update_source: str = ""
    ebgp_multihop: bool = False
    description: str = ""
    shutdown: bool = False
    # Address family -> explicitly activated (True) or deactivated (False)
    activate: dict[str, bool] = field(default_factory=dict)


@dataclass
class RouteTargets:
    rd: str = ""
    import_rts: list[str] = field(default_factory=list)
    export_rts: list[str] = field(default_factory=list)

    def view(self) -> dict:
        return {"rd": self.rd, "import": self.import_rts, "export": self.export_rts}


@dataclass
class Bgp:
    asn: str
    router_id: str = ""
    default_ipv4: bool = True
    neighbors: dict[str, BgpNeighbor] = field(default_factory=dict)   # key: (vrf, key) joined
    networks: list[str] = field(default_factory=list)
    vlans: dict[int, RouteTargets] = field(default_factory=dict)       # EVPN MAC-VRFs
    vrfs: dict[str, RouteTargets] = field(default_factory=dict)        # EVPN IP-VRFs

    def neighbor(self, key: str, vrf: str = "") -> BgpNeighbor:
        k = f"{vrf}|{key}"
        if k not in self.neighbors:
            self.neighbors[k] = BgpNeighbor(key, vrf=vrf)
        return self.neighbors[k]

    def group(self, name: str, vrf: str = "") -> Optional[BgpNeighbor]:
        nb = self.neighbors.get(f"{vrf}|{name}") or self.neighbors.get(f"|{name}")
        return nb if nb and nb.is_group else None

    def peers(self) -> list[BgpNeighbor]:
        return [nb for nb in self.neighbors.values() if not nb.is_group]

    def resolved(self, nb: BgpNeighbor, attr: str):
        """A neighbour setting, falling back to its peer group's."""
        value = getattr(nb, attr)
        if not value and nb.peer_group:
            group = self.group(nb.peer_group, nb.vrf)
            if group:
                value = getattr(group, attr)
        return value

    def families(self, nb: BgpNeighbor) -> list[str]:
        """Address families a neighbour is activated in."""
        group = self.group(nb.peer_group, nb.vrf) if nb.peer_group else None

        def state(af: str) -> Optional[bool]:
            if af in nb.activate:
                return nb.activate[af]
            if group and af in group.activate:
                return group.activate[af]
            return None

        afs = []
        if state("ipv4") or (state("ipv4") is None and self.default_ipv4):
            afs.append("ipv4")
        if state("evpn"):
            afs.append("evpn")
        return afs


@dataclass
class Vtep:
    interface: str                              # Vxlan1, nve1
    source_interface: str = ""
    udp_port: int = 4789
    l2_vnis: dict[int, int] = field(default_factory=dict)   # vni -> vlan (0 if unknown)
    l3_vnis: dict[int, str] = field(default_factory=dict)   # vni -> vrf
    flood_list: list[str] = field(default_factory=list)     # static head-end replication peers
    bgp_learning: bool = True                               # host-reachability / EVPN implied


@dataclass
class Mlag:
    """EOS ``mlag configuration``."""
    domain_id: str = ""
    local_interface: str = ""                   # Vlan4094
    peer_address: str = ""
    peer_link: str = ""                         # Port-Channel10
    shutdown: bool = False


@dataclass
class NodeConfig:
    hostname: str = ""
    interfaces: dict[str, Interface] = field(default_factory=dict)
    ospf: list[OspfProcess] = field(default_factory=list)
    bgp: Optional[Bgp] = None
    vtep: Optional[Vtep] = None
    mlag: Optional[Mlag] = None

    def port_channel_members(self, name: str) -> list[str]:
        """Interfaces in Port-Channel<n> (``channel-group <n>``)."""
        number = re.search(r"(\d+)$", name)
        if not number:
            return []
        return sorted((i.name for i in self.interfaces.values() if i.channel_group == int(number.group(1))),
                      key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n)])

    def interface(self, name: str) -> Optional[Interface]:
        return self.interfaces.get(canonical(name))

    def owned_addresses(self) -> list[tuple[Interface, ipaddress.IPv4Interface]]:
        return [(i, a) for i in self.interfaces.values() for a in i.addresses]

    def derived_router_id(self) -> str:
        """IOS / EOS default: highest loopback address, else highest interface address."""
        def highest(ifaces):
            ips = [a.ip for i in ifaces if not i.shutdown for a in i.addresses]
            return str(max(ips)) if ips else ""
        return (highest(i for i in self.interfaces.values() if i.is_loopback)
                or highest(self.interfaces.values()))


# --- Parsing ------------------------------------------------------------------

def _area(value: str) -> Optional[int]:
    try:
        return int(ipaddress.IPv4Address(value)) if "." in value else int(value)
    except ValueError:
        return None


def _address(words: list[str]) -> Optional[ipaddress.IPv4Interface]:
    """``ip address 10.0.0.1/31`` or ``ip address 10.0.0.1 255.255.255.254``."""
    try:
        if "/" in words[0]:
            return ipaddress.IPv4Interface(words[0])
        if len(words) > 1:
            return ipaddress.IPv4Interface(f"{words[0]}/{words[1]}")
    except (ValueError, IndexError):
        pass
    return None


def _ranges(text: str) -> list[int]:
    """``10,20-22`` -> [10, 20, 21, 22]."""
    out = []
    for part in text.split(","):
        lo, _, hi = part.partition("-")
        try:
            out.extend(range(int(lo), int(hi or lo) + 1))
        except ValueError:
            return []
    return out


def parse_config(text: str) -> NodeConfig:
    cfg = NodeConfig()
    vlan_vni: dict[int, int] = {}       # NX-OS vn-segment / IOS-XE vlan configuration
    vrf_vni: dict[str, int] = {}        # NX-OS vrf context
    for line in config_tree(text[:MAX_CONFIG_BYTES]):
        w = line.words
        if w[0] == "hostname" and len(w) > 1:
            cfg.hostname = w[1]
        elif w[0] == "interface" and len(w) > 1:
            _parse_interface(cfg, canonical(" ".join(w[1:])), line.children)
        elif w[:2] == ["router", "ospf"] and len(w) > 2:
            cfg.ospf.append(_parse_ospf(w, line.children))
        elif w[:2] == ["router", "bgp"] and len(w) > 2:
            cfg.bgp = _parse_bgp(w[2], line.children)
        elif w[0] == "vlan" and len(w) > 1:
            # NX-OS "vlan 10 / vn-segment 10010", IOS-XE
            # "vlan configuration 10 / member evpn-instance 10 vni 10010"
            vlans = _ranges(w[2]) if w[1] == "configuration" and len(w) > 2 else _ranges(w[1])
            for child in line.children:
                cw = child.words
                vni = ""
                if cw[0] == "vn-segment" and len(cw) > 1:
                    vni = cw[1]
                elif cw[:2] == ["member", "evpn-instance"] and "vni" in cw[:-1]:
                    vni = cw[cw.index("vni") + 1]
                if vni.isdigit():
                    for vlan in vlans:
                        vlan_vni[vlan] = int(vni)
        elif w[:2] == ["mlag", "configuration"]:
            cfg.mlag = _parse_mlag(line.children)
        elif w[:2] == ["vrf", "context"] and len(w) > 2:
            for child in line.children:
                if child.words[0] == "vni" and len(child.words) > 1 and child.words[1].isdigit():
                    vrf_vni[w[2]] = int(child.words[1])
    if cfg.vtep:
        vni_vlan = {vni: vlan for vlan, vni in vlan_vni.items()}
        for vni, vlan in cfg.vtep.l2_vnis.items():
            if not vlan:
                cfg.vtep.l2_vnis[vni] = vni_vlan.get(vni, 0)
        for vrf, vni in vrf_vni.items():     # NX-OS: "member vni X associate-vrf"
            if vni in cfg.vtep.l3_vnis and not cfg.vtep.l3_vnis[vni]:
                cfg.vtep.l3_vnis[vni] = vrf
    return cfg


def _parse_interface(cfg: NodeConfig, name: str, children: list[Line]) -> None:
    iface = cfg.interfaces.setdefault(name, Interface(name))
    low = name.lower()
    if low.startswith(("vxlan", "nve")):
        cfg.vtep = _parse_vtep(name, children)
        return
    for child in children:
        w = child.words
        if w[:2] == ["ip", "address"] and len(w) > 2:
            # EOS anycast gateway: ip address virtual 10.10.10.1/24
            addr = _address(w[3:] if w[2] == "virtual" else w[2:])
            if addr is None:
                continue
            iface.anycast = iface.anycast or w[2] == "virtual"
            if "secondary" in w[3:]:
                iface.addresses.append(addr)
            else:
                iface.addresses.insert(0, addr)
        elif w[:4] == ["fabric", "forwarding", "mode", "anycast-gateway"]:  # NX-OS
            iface.anycast = True
        elif w[0] == "shutdown":
            iface.shutdown = True
        elif w[:2] == ["no", "shutdown"]:
            iface.shutdown = False
        elif w[0] == "description":
            iface.description = child.text.partition(" ")[2]
        elif w[0] == "vrf" and len(w) > 1:
            # EOS "vrf X", IOS-XE "vrf forwarding X", NX-OS "vrf member X"
            iface.vrf = w[-1]
        elif w[0] == "channel-group" and len(w) > 1 and w[1].isdigit():
            iface.channel_group = int(w[1])
        elif w[0] == "mlag" and len(w) > 1 and w[1].isdigit():
            iface.mlag_id = int(w[1])
        elif w[:3] == ["switchport", "access", "vlan"] and len(w) > 3 and w[3].isdigit():
            iface.access_vlan = int(w[3])
        elif w[:3] == ["ip", "vrf", "forwarding"] and len(w) > 3:
            iface.vrf = w[3]
        elif w[:2] == ["ip", "ospf"] and len(w) > 2:
            _parse_ospf_iface(iface, w[2:])
        elif w[:3] == ["ip", "router", "ospf"] and "area" in w:
            # NX-OS: ip router ospf <tag> area <area>
            iface.ospf_process = w[3] if len(w) > 3 else ""
            iface.ospf_area = _area(w[w.index("area") + 1]) if w.index("area") + 1 < len(w) else None


def _parse_ospf_iface(iface: Interface, w: list[str]) -> None:
    if w[0] == "area" and len(w) > 1:                       # EOS: ip ospf area 0
        iface.ospf_area = _area(w[1])
    elif len(w) > 2 and w[1] == "area":                     # IOS: ip ospf 1 area 0
        iface.ospf_process = w[0]
        iface.ospf_area = _area(w[2])
    elif w[0] == "cost" and len(w) > 1 and w[1].isdigit():
        iface.ospf_cost = int(w[1])
    elif w[0] == "network" and len(w) > 1:
        iface.ospf_network = w[1]
    elif w[0] in ("passive", "passive-interface"):
        iface.ospf_passive = True


def _parse_ospf(words: list[str], children: list[Line]) -> OspfProcess:
    proc = OspfProcess(words[2])
    if len(words) > 4 and words[3] == "vrf":
        proc.vrf = words[4]
    for child in children:
        w = child.words
        if w[0] == "router-id" and len(w) > 1:
            proc.router_id = w[1]
        elif w[0] == "network" and "area" in w:
            area = _area(w[w.index("area") + 1]) if w.index("area") + 1 < len(w) else None
            try:
                if "/" in w[1]:
                    net = ipaddress.IPv4Network(w[1], strict=False)
                    address, wildcard = int(net.network_address), int(net.hostmask)
                else:
                    address = int(ipaddress.IPv4Address(w[1]))
                    wildcard = int(ipaddress.IPv4Address(w[2]))
            except (ValueError, IndexError):
                continue
            if area is not None:
                proc.networks.append(OspfNetwork(address, wildcard, area))
        elif w[:2] == ["passive-interface", "default"]:
            proc.passive_default = True
        elif w[0] == "passive-interface" and len(w) > 1:
            proc.passive.add(canonical(" ".join(w[1:])))
        elif w[:2] == ["no", "passive-interface"] and len(w) > 2:
            proc.not_passive.add(canonical(" ".join(w[2:])))
    return proc


_AF_NAMES = {
    ("ipv4",): "ipv4", ("ipv4", "unicast"): "ipv4",
    ("evpn",): "evpn", ("l2vpn", "evpn"): "evpn",
}


def _parse_bgp(asn: str, children: list[Line]) -> Bgp:
    bgp = Bgp(asn)
    for child in children:
        w = child.words
        if w[0] == "router-id" and len(w) > 1:
            bgp.router_id = w[1]
        elif w[:2] == ["bgp", "router-id"] and len(w) > 2:
            bgp.router_id = w[2]
        elif w[:4] == ["no", "bgp", "default", "ipv4-unicast"]:
            bgp.default_ipv4 = False
        elif w[0] == "neighbor" and len(w) > 1:
            _parse_neighbor(bgp, w, child.children, "", None)
        elif w[0] == "network" and len(w) > 1:
            _parse_network(bgp, w)
        elif w[0] == "address-family" and len(w) > 1:
            if "vrf" in w:                                   # IOS: address-family ipv4 vrf X
                vrf = w[w.index("vrf") + 1] if w.index("vrf") + 1 < len(w) else ""
                for sub in child.children:
                    if sub.words[0] == "neighbor":
                        _parse_neighbor(bgp, sub.words, sub.children, vrf, "ipv4")
                continue
            af = _AF_NAMES.get(tuple(w[1:3])) or _AF_NAMES.get(tuple(w[1:2]))
            if af:
                _parse_af(bgp, af, child.children, "")
        elif w[0] == "vlan" and len(w) > 1:                  # EOS MAC-VRF
            rts = _parse_rts(child.children)
            for vlan in _ranges(w[1]):
                bgp.vlans[vlan] = rts
        elif w[0] == "vrf" and len(w) > 1:                   # EOS / NX-OS IP-VRF
            vrf = w[1]
            bgp.vrfs[vrf] = _parse_rts(child.children)
            for sub in child.children:
                sw = sub.words
                if sw[0] == "neighbor":
                    _parse_neighbor(bgp, sw, sub.children, vrf, None)
                elif sw[0] == "address-family" and len(sw) > 1:
                    af = _AF_NAMES.get(tuple(sw[1:3])) or _AF_NAMES.get(tuple(sw[1:2]))
                    if af:
                        _parse_af(bgp, af, sub.children, vrf)
    return bgp


def _parse_neighbor(bgp: Bgp, w: list[str], children: list[Line], vrf: str,
                    af: Optional[str]) -> None:
    key = w[1]
    rest = w[2:]
    if rest in (["peer-group"], ["peer", "group"]):          # define a group
        bgp.neighbor(key, vrf).is_group = True
        return
    nb = bgp.neighbor(key, vrf)
    if rest[:2] == ["peer", "group"] and len(rest) > 2:      # EOS: neighbor X peer group G
        nb.peer_group = rest[2]
    elif rest[:1] == ["peer-group"] and len(rest) > 1:       # IOS: neighbor X peer-group G
        nb.peer_group = rest[1]
    elif rest[:1] == ["remote-as"] and len(rest) > 1:
        nb.remote_as = rest[1]
    elif rest[:1] == ["update-source"] and len(rest) > 1:
        nb.update_source = canonical(" ".join(rest[1:]))
    elif rest[:1] == ["ebgp-multihop"] or rest[:1] == ["disable-connected-check"]:
        nb.ebgp_multihop = True
    elif rest[:1] == ["description"]:
        nb.description = " ".join(rest[1:])
    elif rest[:1] == ["shutdown"]:
        nb.shutdown = True
    elif rest[:1] == ["activate"]:
        nb.activate[af or "ipv4"] = True
    # NX-OS: neighbor X / remote-as N / address-family l2vpn evpn
    for child in children:
        cw = child.words
        if cw[0] == "address-family" and len(cw) > 1:
            fam = _AF_NAMES.get(tuple(cw[1:3])) or _AF_NAMES.get(tuple(cw[1:2]))
            if fam:
                nb.activate[fam] = True
        elif cw[0] == "inherit" and cw[1:2] == ["peer"] and len(cw) > 2:
            nb.peer_group = cw[2]
        else:
            _parse_neighbor(bgp, ["neighbor", key, *cw], [], vrf, af)


def _parse_network(bgp: Bgp, w: list[str]) -> None:
    """``network 10.0.0.0/24`` or ``network 10.0.0.0 mask 255.255.255.0``."""
    try:
        if len(w) > 3 and w[2] == "mask":
            prefix = ipaddress.IPv4Network(f"{w[1]}/{w[3]}", strict=False)
        else:
            prefix = ipaddress.IPv4Network(w[1], strict=False)
    except ValueError:
        return
    if str(prefix) not in bgp.networks:
        bgp.networks.append(str(prefix))


def _parse_af(bgp: Bgp, af: str, children: list[Line], vrf: str) -> None:
    for child in children:
        w = child.words
        if w[0] == "network" and len(w) > 1 and af == "ipv4" and not vrf:
            _parse_network(bgp, w)
            continue
        if w[0] == "neighbor" and len(w) > 2 and w[-1] == "activate":
            bgp.neighbor(w[1], vrf).activate[af] = True
        elif w[:2] == ["no", "neighbor"] and len(w) > 3 and w[-1] == "activate":
            bgp.neighbor(w[2], vrf).activate[af] = False
        elif w[0] == "neighbor" and len(w) > 2:
            _parse_neighbor(bgp, w, child.children, vrf, af)


def _parse_rts(children: list[Line]) -> RouteTargets:
    rts = RouteTargets()
    for child in children:
        w = child.words
        if w[0] == "rd" and len(w) > 1:
            rts.rd = w[1]
        elif w[0] == "route-target" and len(w) > 2:
            # route-target both|import|export [evpn] <rt>
            value = w[-1]
            if w[1] in ("both", "import"):
                rts.import_rts.append(value)
            if w[1] in ("both", "export"):
                rts.export_rts.append(value)
    return rts


def _parse_vtep(name: str, children: list[Line]) -> Vtep:
    vtep = Vtep(name)
    for child in children:
        w = child.words
        if w[0] == "vxlan" and len(w) > 1:                  # EOS: vxlan ... on Vxlan1
            w = w[1:]
        if w[0] == "source-interface" and len(w) > 1:
            vtep.source_interface = canonical(" ".join(w[1:]))
        elif w[0] == "udp-port" and len(w) > 1 and w[1].isdigit():
            vtep.udp_port = int(w[1])
        elif w[0] == "vlan" and "vni" in w and len(w) > 3:
            vlans = _ranges(w[1])
            vnis = _ranges(w[w.index("vni") + 1])
            for vlan, vni in zip(vlans, vnis):
                vtep.l2_vnis[vni] = vlan
        elif w[0] == "vrf" and "vni" in w and len(w) > 3:
            try:
                vtep.l3_vnis[int(w[w.index("vni") + 1])] = w[1]
            except ValueError:
                pass
        elif w[:2] == ["flood", "vtep"]:
            vtep.flood_list.extend(w[2:])
        elif w[:2] == ["member", "vni"] and len(w) > 2:     # IOS-XE / NX-OS nve
            for vni in _ranges(w[2]):
                if "vrf" in w and w.index("vrf") + 1 < len(w):
                    vtep.l3_vnis[vni] = w[w.index("vrf") + 1]
                elif "associate-vrf" in w:
                    vtep.l3_vnis[vni] = ""
                else:
                    vtep.l2_vnis[vni] = 0
            for sub in child.children:
                # ingress-replication static / peer-ip X (no EVPN learning)
                if sub.words[0] == "peer-ip" and len(sub.words) > 1:
                    vtep.flood_list.append(sub.words[1])
        elif w[:2] == ["host-reachability", "protocol"]:
            vtep.bgp_learning = w[2:3] == ["bgp"]
    return vtep


def _parse_mlag(children: list[Line]) -> Mlag:
    m = Mlag()
    for child in children:
        w = child.words
        if w[0] == "domain-id" and len(w) > 1:
            m.domain_id = w[1]
        elif w[0] == "local-interface" and len(w) > 1:
            m.local_interface = canonical(w[1])
        elif w[0] == "peer-address" and len(w) > 1 and w[1] != "heartbeat":
            m.peer_address = w[1]
        elif w[0] == "peer-link" and len(w) > 1:
            m.peer_link = canonical(w[1])
        elif w[0] == "shutdown":
            m.shutdown = True
    return m
