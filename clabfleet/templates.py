"""Generate ready-to-deploy topologies from a few lab templates (clabfleet new).

A template only describes the graph: which nodes exist, in which tier, and
which pairs are linked. The builder allocates addresses and interface
numbers, and a per-kind renderer turns each node into containerlab node
settings (interface names, startup-config or exec commands).

Addressing is the same for every template and kind:

- every link is a /31 taken in order from the link subnet (default
  10.0.0.0/16); the first node named in the link gets the lower address
- every node has a /32 loopback, ``<loopback subnet> + tier * 256 + n``,
  so with the default 10.255.0.0/16 tier 0 is 10.255.0.n, tier 1
  10.255.1.n and so on

Adding a template means writing one build function and a ``Template``
entry; adding a kind means one ``KindSpec`` with an interface namer and a
renderer.

``generate_configs`` does the same for a graph drawn in the GUI builder:
any mix of kinds, on the ports drawn. Routers (cEOS, IOL) run the routing
protocol with each other; Linux hosts get addresses and a default route
through the router they are cabled to, and that router announces the
host's subnet.
"""

import ipaddress
from dataclasses import dataclass, field
from typing import Callable, Optional

from .topology import dump_yaml, topology_from_dict

DEFAULT_LINK_SUBNET = "10.0.0.0/16"
DEFAULT_LOOPBACK_SUBNET = "10.255.0.0/16"
DEFAULT_ASN = 65000
MAX_NODES_PER_TIER = 254  # loopbacks are .1 to .254 within a tier's /24
MAX_ECMP_PATHS = 32       # IOS caps eBGP maximum-paths at 32


class TemplateError(Exception):
    """Raised when template parameters cannot produce a valid topology."""


# --- Plan: the graph with addresses, independent of kind ---

@dataclass
class Interface:
    index: int                              # 1-based data port number
    peer: str
    address: ipaddress.IPv4Interface        # this end's /31
    peer_address: ipaddress.IPv4Address


@dataclass
class PlanNode:
    name: str
    tier: int
    loopback: ipaddress.IPv4Address
    asn: Optional[int] = None               # set for BGP routers
    max_paths: int = 1                      # BGP ECMP paths (leaves)
    interfaces: list[Interface] = field(default_factory=list)
    kind: str = ""                          # set by generate_configs (mixed kinds)


@dataclass
class Plan:
    routing: str                            # "bgp" or "ospf"
    nodes: dict[str, PlanNode] = field(default_factory=dict)
    links: list[tuple[str, int, str, int]] = field(default_factory=list)  # a, a_if, b, b_if


class _Builder:
    def __init__(self, routing: str, link_subnet: str, loopback_subnet: str):
        self.plan = Plan(routing)
        self._links = _network(link_subnet, "link subnet").subnets(new_prefix=31)
        self._loopbacks = _network(loopback_subnet, "loopback subnet")
        if self._loopbacks.overlaps(_network(link_subnet, "link subnet")):
            raise TemplateError(f"Link subnet {link_subnet} overlaps loopback "
                                f"subnet {loopback_subnet}")
        self._per_tier: dict[int, int] = {}

    def node(self, name: str, tier: int, asn: Optional[int] = None, kind: str = "") -> PlanNode:
        n = self._per_tier.get(tier, 0) + 1
        if n > MAX_NODES_PER_TIER:
            raise TemplateError(f"At most {MAX_NODES_PER_TIER} nodes per tier")
        self._per_tier[tier] = n
        loopback = self._loopbacks.network_address + tier * 256 + n
        if loopback not in self._loopbacks or loopback == self._loopbacks.broadcast_address:
            raise TemplateError(f"Loopback subnet {self._loopbacks} is too small "
                                f"for {tier + 1} tiers of nodes (use a /22 or larger)")
        node = PlanNode(name, tier, loopback, asn, kind=kind)
        self.plan.nodes[name] = node
        return node

    def link(self, a: str, b: str, a_index: Optional[int] = None,
             b_index: Optional[int] = None) -> None:
        """Link two nodes on their next ports, or on the ports given."""
        try:
            subnet = next(self._links)
        except StopIteration:
            raise TemplateError("Link subnet is too small for this many links") from None
        low, high = subnet[0], subnet[1]
        ends = []
        for name, mine, theirs, peer, given in ((a, low, high, b, a_index), (b, high, low, a, b_index)):
            node = self.plan.nodes[name]
            index = given or len(node.interfaces) + 1
            node.interfaces.append(Interface(
                index, peer, ipaddress.IPv4Interface(f"{mine}/31"), theirs))
            ends.append(index)
        self.plan.links.append((a, ends[0], b, ends[1]))


def _network(value: str, what: str) -> ipaddress.IPv4Network:
    try:
        return ipaddress.IPv4Network(value)
    except ValueError as exc:
        raise TemplateError(f"Invalid {what} '{value}': {exc}") from None


# --- Templates ---

@dataclass
class Param:
    name: str           # option name without dashes, e.g. "spines"
    default: int
    help: str
    minimum: int = 1
    maximum: int = MAX_NODES_PER_TIER


@dataclass
class Template:
    name: str
    description: str
    default_name: str   # lab name unless --name is given
    params: list[Param]
    build: Callable[[_Builder, dict], None]
    routing: str


def _build_spine_leaf(b: _Builder, p: dict) -> None:
    # eBGP underlay: spines share one ASN, each leaf has its own
    spines = [b.node(f"Spine-{i}", 0, p["asn"]) for i in range(1, p["spines"] + 1)]
    leaves = [b.node(f"Leaf-{i}", 1, p["asn"] + i) for i in range(1, p["leaves"] + 1)]
    for leaf in leaves:
        leaf.max_paths = min(len(spines), MAX_ECMP_PATHS)
    for spine in spines:
        for leaf in leaves:
            b.link(spine.name, leaf.name)


def _build_ring(b: _Builder, p: dict) -> None:
    names = [b.node(f"R{i}", 0).name for i in range(1, p["nodes"] + 1)]
    for i, name in enumerate(names):
        b.link(name, names[(i + 1) % len(names)])


def _build_campus(b: _Builder, p: dict) -> None:
    cores = [b.node(f"Core-{i}", 0).name for i in range(1, p["core"] + 1)]
    dists = [b.node(f"Dist-{i}", 1).name for i in range(1, p["dist"] + 1)]
    access = [b.node(f"Access-{i}", 2).name for i in range(1, p["access"] + 1)]
    for i, a in enumerate(cores):
        for c in cores[i + 1:]:
            b.link(a, c)
    for core in cores:
        for dist in dists:
            b.link(core, dist)
    # Access switches are spread over the distribution nodes in blocks
    for i, acc in enumerate(access):
        b.link(dists[i * len(dists) // len(access)], acc)


TEMPLATES: dict[str, Template] = {t.name: t for t in [
    Template(
        "spine-leaf", "Spine-leaf fabric with an eBGP underlay", "spine-leaf",
        [Param("spines", 2, "Number of spines"),
         Param("leaves", 4, "Number of leaves"),
         Param("asn", DEFAULT_ASN, "Spine ASN; leaf n gets ASN + n",
               minimum=1, maximum=4294967294 - MAX_NODES_PER_TIER)],
        _build_spine_leaf, "bgp",
    ),
    Template(
        "ring", "Routers in a ring running OSPF", "ring",
        [Param("nodes", 4, "Number of routers", minimum=3)],
        _build_ring, "ospf",
    ),
    Template(
        "campus", "Core / distribution / access campus running OSPF", "campus",
        [Param("core", 2, "Number of core nodes (fully meshed)"),
         Param("dist", 2, "Number of distribution nodes (each linked to every core)"),
         Param("access", 4, "Number of access nodes (one uplink each)")],
        _build_campus, "ospf",
    ),
]}


# --- Kinds ---

def _ceos_if(index: int) -> tuple[str, str]:
    return f"eth{index}", f"Ethernet{index}"


def _iol_if(index: int) -> tuple[str, str]:
    # IOL ports go Ethernet0/0-0/3, 1/0-1/3, ...; 0/0 is management
    name = f"Ethernet{index // 4}/{index % 4}"
    return name, name


def _linux_if(index: int) -> tuple[str, str]:
    return f"eth{index}", f"eth{index}"


def _render_ceos(node: PlanNode, plan: Plan) -> dict:
    lines = [f"hostname {node.name}", "!",
             "username admin privilege 15 secret admin", "!",
             "ip routing", "!",
             "interface Loopback0", f" ip address {node.loopback}/32", "!"]
    for i in node.interfaces:
        lines += [f"interface {_ceos_if(i.index)[1]}", f" description to-{i.peer}",
                  " no switchport", f" ip address {i.address}"]
        if plan.routing == "ospf":
            lines.append(" ip ospf network point-to-point")
        lines.append("!")
    if plan.routing == "bgp":
        lines += [f"router bgp {node.asn}", f" router-id {node.loopback}"]
        if node.max_paths > 1:
            lines.append(f" maximum-paths {node.max_paths}")
        for i in _router_ports(node, plan):
            lines += [f" neighbor {i.peer_address} remote-as {plan.nodes[i.peer].asn}",
                      f" neighbor {i.peer_address} description {i.peer}"]
        lines.append(f" network {node.loopback}/32")
        lines += [f" network {i.address.network}" for i in _host_ports(node, plan)]
    else:
        lines += ["router ospf 1", f" router-id {node.loopback}",
                  f" network {node.loopback}/32 area 0.0.0.0"]
        lines += [f" network {i.address.network} area 0.0.0.0" for i in node.interfaces]
    return {"startup-config": "\n".join(lines) + "\n"}


def _render_iol(node: PlanNode, plan: Plan) -> dict:
    lines = [f"hostname {node.name}", "!",
             "interface Loopback0", f" ip address {node.loopback} 255.255.255.255", "!"]
    for i in node.interfaces:
        lines += [f"interface {_iol_if(i.index)[1]}", f" description to-{i.peer}",
                  f" ip address {i.address.ip} {i.address.netmask}"]
        if plan.routing == "ospf":
            lines.append(" ip ospf network point-to-point")
        lines += [" no shutdown", "!"]
    if plan.routing == "bgp":
        lines += [f"router bgp {node.asn}", f" bgp router-id {node.loopback}"]
        if node.max_paths > 1:
            lines.append(f" maximum-paths {node.max_paths}")
        for i in _router_ports(node, plan):
            lines += [f" neighbor {i.peer_address} remote-as {plan.nodes[i.peer].asn}",
                      f" neighbor {i.peer_address} description {i.peer}"]
        lines.append(f" network {node.loopback} mask 255.255.255.255")
        lines += [f" network {i.address.network.network_address} mask {i.address.netmask}"
                  for i in _host_ports(node, plan)]
    else:
        lines += ["router ospf 1", f" router-id {node.loopback}",
                  f" network {node.loopback} 0.0.0.0 area 0"]
        lines += [f" network {i.address.network.network_address} "
                  f"{i.address.network.hostmask} area 0" for i in node.interfaces]
    lines += ["!", "end"]
    return {"startup-config": "\n".join(lines) + "\n"}


def _render_linux(node: PlanNode, plan: Plan) -> dict:
    # No routing daemon: nodes get their addresses and reach direct neighbours,
    # and everything else through the first router they are cabled to
    cmds = [f"ip addr add {node.loopback}/32 dev lo"]
    cmds += [f"ip addr add {i.address} dev {_linux_if(i.index)[1]}" for i in node.interfaces]
    # (templates have no hosts next to routers: only drawn graphs, with kinds set)
    gateway = next(iter(_router_ports(node, plan)), None) if node.kind else None
    if gateway:
        cmds.append(f"ip route replace default via {gateway.peer_address}")
    return {"exec": cmds}


def _is_router(node: PlanNode) -> bool:
    return node.asn is not None or node.kind in ROUTER_KINDS


def _router_ports(node: PlanNode, plan: Plan) -> list[Interface]:
    """Ports facing a router (all of them in a template: there are no hosts)."""
    return [i for i in node.interfaces if not plan.nodes[i.peer].kind or _is_router(plan.nodes[i.peer])]


def _host_ports(node: PlanNode, plan: Plan) -> list[Interface]:
    """Ports facing a Linux host (only in drawn graphs)."""
    return [i for i in node.interfaces if plan.nodes[i.peer].kind and not _is_router(plan.nodes[i.peer])]


@dataclass
class KindSpec:
    image: str
    interface: Callable[[int], tuple[str, str]]  # port → (link endpoint name, config name)
    render: Callable[[PlanNode, Plan], dict]
    note: str


KINDS: dict[str, KindSpec] = {
    "arista_ceos": KindSpec(
        "ceos:4.35.6M", _ceos_if, _render_ceos,
        "Links use eth1, eth2, ... which EOS shows as Ethernet1, Ethernet2.",
    ),
    "cisco_iol": KindSpec(
        "vrnetlab/cisco_iol:17.12.01", _iol_if, _render_iol,
        "Ethernet0/0 is the management interface, so links start at Ethernet0/1.",
    ),
    "linux": KindSpec(
        "alpine:3.20", _linux_if, _render_linux,
        "Linux nodes get addresses only (no routing): each node reaches its "
        "direct neighbours. Public images need --pull on deploy.",
    ),
}
DEFAULT_KIND = "arista_ceos"
ROUTER_KINDS = {"arista_ceos", "cisco_iol"}


def port_index(kind: str, iface: str) -> Optional[int]:
    """The data port number of a link endpoint name (inverse of the namers)."""
    import re

    if kind == "cisco_iol":
        m = re.fullmatch(r"Ethernet(\d+)/(\d+)", iface)
        return int(m[1]) * 4 + int(m[2]) if m else None
    m = re.fullmatch(r"eth(\d+)", iface)
    return int(m[1]) if m else None


def generate_configs(
    nodes: dict[str, str],
    links: list[tuple[str, str, str, str]],
    routing: str = "ospf",
    link_subnet: str = DEFAULT_LINK_SUBNET,
    loopback_subnet: str = DEFAULT_LOOPBACK_SUBNET,
    asn: int = DEFAULT_ASN,
) -> tuple[dict[str, dict], list[str]]:
    """Configs for a drawn graph: ``nodes`` maps name to kind, ``links`` are
    (a, a_port, b, b_port). Every router gets its own ASN from ``asn`` up
    (eBGP) or OSPF area 0. Returns ({node: startup-config or exec}, nodes of
    kinds it cannot configure, which are left out with their links)."""
    if routing not in ("ospf", "bgp"):
        raise TemplateError("Routing must be ospf or bgp")
    if not 1 <= int(asn) <= 4294967294 - len(nodes):
        raise TemplateError("ASN out of range")
    skipped = sorted(n for n, k in nodes.items() if k not in KINDS)
    b = _Builder(routing, link_subnet, loopback_subnet)
    next_asn = int(asn)
    for name, kind in nodes.items():  # in drawing order: R1 gets the first loopback
        if name in skipped:
            continue
        router = kind in ROUTER_KINDS
        b.node(name, 0 if router else 1, next_asn if router and routing == "bgp" else None, kind)
        if router and routing == "bgp":
            next_asn += 1
    used: dict[str, set[int]] = {}
    for a, a_port, z, z_port in links:
        if a in skipped or z in skipped:
            continue
        idx = []
        for name, port in ((a, a_port), (z, z_port)):
            i = port_index(nodes[name], port)
            if not i:
                raise TemplateError(f"{name}: {port} is not a {nodes[name]} data port "
                                    f"(like {KINDS[nodes[name]].interface(1)[0]})")
            if i in used.setdefault(name, set()):
                raise TemplateError(f"{name}: {port} is used by two links")
            used[name].add(i)
            idx.append(i)
        b.link(a, z, idx[0], idx[1])
    plan = b.plan
    for node in plan.nodes.values():
        node.interfaces.sort(key=lambda i: i.index)
    return ({n.name: KINDS[n.kind].render(n, plan) for n in plan.nodes.values()}, skipped)


# --- Generation ---

def generate(
    template: str,
    params: Optional[dict] = None,
    kind: str = DEFAULT_KIND,
    image: Optional[str] = None,
    name: Optional[str] = None,
    link_subnet: str = DEFAULT_LINK_SUBNET,
    loopback_subnet: str = DEFAULT_LOOPBACK_SUBNET,
) -> dict:
    """Build a containerlab topology dict from a template.

    ``params`` holds the template's own options (e.g. spines, leaves);
    missing ones take their defaults.
    """
    tmpl = TEMPLATES.get(template)
    if tmpl is None:
        raise TemplateError(f"Unknown template '{template}' "
                            f"(choose from {', '.join(TEMPLATES)})")
    spec = KINDS.get(kind)
    if spec is None:
        raise TemplateError(f"Unsupported kind '{kind}' (choose from {', '.join(KINDS)})")
    values = resolve_params(tmpl, params)

    builder = _Builder(tmpl.routing, link_subnet, loopback_subnet)
    tmpl.build(builder, values)
    plan = builder.plan

    nodes = {n.name: {"kind": kind, **spec.render(n, plan)} for n in plan.nodes.values()}
    links = [
        {"endpoints": [f"{a}:{spec.interface(ai)[0]}", f"{b}:{spec.interface(bi)[0]}"]}
        for a, ai, b, bi in plan.links
    ]
    data = {
        "name": name or tmpl.default_name,
        "topology": {
            "kinds": {kind: {"image": image or spec.image}},
            "nodes": nodes,
            "links": links,
        },
    }
    topology_from_dict(data)  # same checks as loading the file later
    return data


def resolve_params(tmpl: Template, params: Optional[dict]) -> dict:
    """Template options with defaults filled in and ranges checked."""
    params = {k: v for k, v in (params or {}).items() if v is not None}
    unknown = set(params) - {p.name for p in tmpl.params}
    if unknown:
        raise TemplateError(f"Template '{tmpl.name}' has no option(s) "
                            f"{', '.join(sorted(unknown))}")
    values = {}
    for p in tmpl.params:
        value = int(params.get(p.name, p.default))
        if not p.minimum <= value <= p.maximum:
            raise TemplateError(f"--{p.name} must be between {p.minimum} and {p.maximum}")
        values[p.name] = value
    return values


def render(data: dict, template: str, params: dict, kind: str) -> str:
    """Topology YAML with a header saying how it was generated and how to deploy it."""
    tmpl = TEMPLATES[template]
    values = resolve_params(tmpl, params)
    opts = " ".join(f"--{k} {v}" for k, v in values.items())
    header = [
        f"# {tmpl.description} ({kind}), generated with:",
        f"#   clabfleet new {template} {opts} --kind {kind}",
        "#",
        "# Point-to-point links are /31s; every node has a /32 Loopback0.",
        f"# {KINDS[kind].note}",
        "#",
        "# Deploy:",
        "#   clabfleet deploy <this file>",
        "",
    ]
    return "\n".join(header) + "\n" + dump_yaml(data)


def describe_templates() -> str:
    """Text for `clabfleet new --list`."""
    out = ["Templates:"]
    for t in TEMPLATES.values():
        out.append(f"  {t.name:<11} {t.description}")
        for p in t.params:
            out.append(f"      --{p.name:<8} {p.help} (default {p.default})")
    out.append("")
    out.append(f"Kinds (--kind, default {DEFAULT_KIND}):")
    for name, spec in KINDS.items():
        out.append(f"  {name:<11} image {spec.image}")
    return "\n".join(out)
