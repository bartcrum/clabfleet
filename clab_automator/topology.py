"""Load and inspect native containerlab topology files.

Topology files are standard ``*.clab.yml`` files — anything containerlab
accepts works here too, and every file in ``topologies/`` can also be
deployed with plain ``containerlab deploy``.

Multi-host placement hints are expressed as ordinary node ``labels`` (which
containerlab passes through to the container), so they can live in
``defaults``, ``kinds`` or ``groups`` like any other label::

    topology:
      nodes:
        core1:
          kind: cisco_iol
          image: vrnetlab/cisco_iol:17.12.01
          labels:
            lab.host: clab-1          # pin to a cluster host
            lab.host-tags: core,spine # or prefer hosts with these tags
            lab.cpu: "2"              # placement estimate (vCPU)
            lab.ram: "4096"           # placement estimate (MB)

If ``lab.cpu`` / ``lab.ram`` are absent, the node's own ``cpu`` / ``memory``
limits are used, then a per-kind estimate.
"""

import copy
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

LABEL_HOST = "lab.host"
LABEL_HOST_TAGS = "lab.host-tags"
LABEL_CPU = "lab.cpu"
LABEL_RAM = "lab.ram"

# Pseudo-nodes allowed in brief link endpoints ("host:veth0", "macvlan:eno1")
SPECIAL_ENDPOINT_NODES = {"host", "mgmt-net", "macvlan"}

# Extended link types that connect exactly two lab nodes
P2P_LINK_TYPES = {"veth"}
# Extended link types with a single node endpoint
SINGLE_ENDPOINT_LINK_TYPES = {
    "host", "mgmt-net", "macvlan", "dummy", "vxlan", "vxlan-stitch",
}

# Rough (vCPU, RAM MB) needs per kind, used for placement when a node sets
# neither lab.cpu/lab.ram labels nor cpu/memory limits.
KIND_RESOURCE_ESTIMATES: dict[str, tuple[float, int]] = {
    "linux": (0.25, 128),
    "bridge": (0, 0),
    "ovs-bridge": (0, 0),
    "nokia_srlinux": (1, 2048),
    "nokia_sros": (2, 4096),
    "arista_ceos": (1, 2048),
    "cisco_iol": (0.5, 512),
    "cisco_xrd": (2, 2048),
    "cisco_xrv9k": (2, 16384),
    "cisco_c8000v": (1, 4096),
    "cisco_csr1000v": (1, 4096),
    "cisco_n9kv": (4, 10240),
    "cisco_ftdv": (4, 8192),
    "juniper_crpd": (1, 1024),
    "juniper_vjunosrouter": (4, 5120),
    "juniper_vjunosswitch": (4, 5120),
    "juniper_vjunosevolved": (4, 8192),
    "juniper_vsrx": (2, 4096),
    "fortinet_fortigate": (1, 2048),
    "paloalto_panos": (2, 6144),
    "sonic-vs": (1, 2048),
}
KIND_ALIASES = {
    "srl": "nokia_srlinux",
    "vr-sros": "nokia_sros",
    "ceos": "arista_ceos",
    "xrd": "cisco_xrd",
    "crpd": "juniper_crpd",
}
DEFAULT_RESOURCES = (1.0, 512)

# Node properties that may reference files relative to the topology file
FILE_PROPERTIES = ("startup-config", "license")
LIST_FILE_PROPERTIES = ("env-files",)


class TopologyError(Exception):
    """Raised when a topology file is invalid."""


@dataclass
class Endpoint:
    node: str
    interface: str
    extra: dict = field(default_factory=dict)  # mac / ipv4 / ipv6 per endpoint


@dataclass
class Link:
    """One entry of ``topology.links`` with its lab-node endpoints resolved."""
    index: int
    raw: dict
    endpoints: list[Endpoint]  # only real lab nodes, not host/macvlan/...

    @property
    def is_p2p(self) -> bool:
        return len(self.endpoints) == 2

    @property
    def node_names(self) -> list[str]:
        return [ep.node for ep in self.endpoints]

    @property
    def link_id(self) -> str:
        return f"link{self.index}"


@dataclass
class Topology:
    data: dict
    base_dir: Path
    path: Optional[Path] = None
    links: list[Link] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.data["name"]

    @property
    def nodes(self) -> dict[str, dict]:
        return self.data["topology"]["nodes"]

    def effective_node(self, name: str) -> dict:
        """Node definition with defaults → kinds → groups inheritance applied."""
        topo = self.data["topology"]
        node = self.nodes[name] or {}
        defaults = topo.get("defaults") or {}
        group = (topo.get("groups") or {}).get(node.get("group")) or {}
        kind = node.get("kind") or group.get("kind") or defaults.get("kind")
        kind_def = (topo.get("kinds") or {}).get(kind) or {}

        merged: dict = {}
        labels: dict = {}
        for layer in (defaults, kind_def, group, node):
            labels.update(layer.get("labels") or {})
            merged.update(copy.deepcopy(layer))
        merged["kind"] = kind
        merged["labels"] = labels
        return merged

    def node_resources(self, name: str) -> tuple[float, int]:
        """(vCPU, RAM MB) the placement engine should reserve for a node."""
        node = self.effective_node(name)
        labels = node["labels"]
        kind = KIND_ALIASES.get(node["kind"], node["kind"])
        est_cpu, est_ram = KIND_RESOURCE_ESTIMATES.get(kind, DEFAULT_RESOURCES)

        if LABEL_CPU in labels:
            cpu = float(labels[LABEL_CPU])
        elif node.get("cpu"):
            cpu = float(node["cpu"])
        else:
            cpu = est_cpu

        if LABEL_RAM in labels:
            ram = int(labels[LABEL_RAM])
        elif node.get("memory"):
            ram = parse_memory_mb(node["memory"])
        else:
            ram = est_ram
        return cpu, ram

    def placement_nodes(self) -> list[dict]:
        """Node summaries in the shape the placement engine expects."""
        result = []
        for name in self.nodes:
            labels = self.effective_node(name)["labels"]
            cpu, ram = self.node_resources(name)
            tags = [t.strip() for t in str(labels.get(LABEL_HOST_TAGS, "")).split(",")]
            result.append({
                "name": name,
                "cpu": cpu,
                "ram": ram,
                "host": labels.get(LABEL_HOST),
                "host_tags": [t for t in tags if t],
            })
        return result

    def link_memberships(self) -> list[dict]:
        """Flattened (node, network) pairs for placement adjacency."""
        return [
            {"node": ep.node, "interface": ep.interface, "network": link.link_id}
            for link in self.links
            for ep in link.endpoints
        ]

    def referenced_files(self) -> list[Path]:
        """Relative file paths the topology needs next to it on the lab host.

        Covers startup-config, license, env-files and bind-mount sources.
        Inline configs, URLs, absolute paths and containerlab magic paths
        (``__clabDir__`` etc.) are skipped. Paths are relative to base_dir.
        """
        found: set[PurePosixPath] = set()
        for name in self.nodes:
            node = self.effective_node(name)
            candidates = [node.get(p) for p in FILE_PROPERTIES]
            for prop in LIST_FILE_PROPERTIES:
                candidates.extend(node.get(prop) or [])
            for bind in node.get("binds") or []:
                candidates.append(str(bind).split(":", 1)[0])

            for value in candidates:
                rel = _relative_file(value, name)
                if rel is not None:
                    found.add(rel)

        result = []
        for rel in sorted(found):
            if ".." in rel.parts:
                logger.warning(
                    "Skipping '%s': files outside the topology directory are "
                    "not copied to remote hosts", rel,
                )
                continue
            if not (self.base_dir / rel).exists():
                logger.warning("Referenced file not found: %s", self.base_dir / rel)
                continue
            result.append(Path(rel))
        return result

    def to_yaml(self) -> str:
        return dump_yaml(self.data)


def _relative_file(value, node_name: str) -> Optional[PurePosixPath]:
    if not isinstance(value, str) or not value or "\n" in value:
        return None  # unset or inline blob
    if re.match(r"^[a-z][a-z0-9+.-]*://", value):
        return None  # URL
    value = value.replace("__clabNodeName__", node_name)
    if value.startswith(("/", "~", "__clab")):
        return None
    return PurePosixPath(value.removeprefix("./"))


def parse_memory_mb(value) -> int:
    """Parse a containerlab/docker memory string ("1Gb", "512MB", "2g") to MB."""
    if isinstance(value, (int, float)):
        return int(value / (1024 * 1024))  # bare numbers are bytes
    m = re.fullmatch(r"\s*([\d.]+)\s*([kmgt]?)i?b?\s*", str(value), re.IGNORECASE)
    if not m:
        raise TopologyError(f"Cannot parse memory value '{value}'")
    number, unit = float(m.group(1)), m.group(2).lower()
    factor = {"": 1 / (1024 * 1024), "k": 1 / 1024, "m": 1, "g": 1024, "t": 1024 * 1024}
    return int(number * factor[unit])


def load_topology(file_path: str | Path) -> Topology:
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"Topology file not found: {file_path}")
    with open(file_path) as fh:
        data = yaml.safe_load(fh)
    topo = topology_from_dict(data, base_dir=file_path.parent)
    topo.path = file_path
    return topo


def topology_from_dict(data: dict, base_dir: str | Path = ".") -> Topology:
    if not isinstance(data, dict):
        raise TopologyError("Topology file must be a YAML mapping")
    if not data.get("name"):
        raise TopologyError("Topology is missing the top-level 'name' field")
    # The name becomes a directory on lab hosts, so keep it path-safe
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", str(data["name"])):
        raise TopologyError(
            f"Invalid lab name '{data['name']}' — use letters, digits, '-', '_' and '.'"
        )
    section = data.get("topology")
    if not isinstance(section, dict) or not isinstance(section.get("nodes"), dict):
        raise TopologyError("Topology must have a 'topology.nodes' mapping")
    if not section["nodes"]:
        raise TopologyError("Topology has no nodes")

    topo = Topology(data=data, base_dir=Path(base_dir))
    groups = section.get("groups") or {}
    for name in topo.nodes:
        group = (topo.nodes[name] or {}).get("group")
        if group and group not in groups:
            raise TopologyError(f"Node '{name}' references unknown group '{group}'")
        if not topo.effective_node(name)["kind"]:
            raise TopologyError(f"Node '{name}' has no kind (set it on the node, group or defaults)")

    links = section.get("links") or []
    if not isinstance(links, list):
        raise TopologyError("'topology.links' must be a list")
    topo.links = [_parse_link(i, raw, topo.nodes) for i, raw in enumerate(links)]
    return topo


def _parse_link(index: int, raw: dict, nodes: dict) -> Link:
    if not isinstance(raw, dict):
        raise TopologyError(f"Link #{index} must be a mapping")
    link_type = raw.get("type")
    endpoints: list[Endpoint] = []

    if link_type is None:
        # Brief format: endpoints: ["a:eth1", "b:eth1"]
        eps = raw.get("endpoints")
        if not isinstance(eps, list) or len(eps) != 2:
            raise TopologyError(f"Link #{index} must have exactly 2 endpoints")
        for pos, ep in enumerate(eps):
            node, sep, iface = str(ep).partition(":")
            if not sep:
                raise TopologyError(f"Link #{index}: endpoint '{ep}' is not 'node:interface'")
            if node in SPECIAL_ENDPOINT_NODES:
                continue
            extra = {}
            for key in ("ipv4", "ipv6"):
                if isinstance(raw.get(key), list) and len(raw[key]) > pos:
                    extra[key] = raw[key][pos]
            endpoints.append(Endpoint(node, iface, extra))
    elif link_type in P2P_LINK_TYPES:
        eps = raw.get("endpoints")
        if not isinstance(eps, list) or len(eps) != 2:
            raise TopologyError(f"Link #{index} ({link_type}) must have exactly 2 endpoints")
        for ep in eps:
            endpoints.append(Endpoint(
                ep["node"], ep["interface"],
                {k: v for k, v in ep.items() if k not in ("node", "interface")},
            ))
    elif link_type in SINGLE_ENDPOINT_LINK_TYPES:
        ep = raw.get("endpoint") or {}
        if "node" not in ep:
            raise TopologyError(f"Link #{index} ({link_type}) needs an 'endpoint.node'")
        endpoints.append(Endpoint(ep["node"], ep.get("interface", "")))
    else:
        raise TopologyError(f"Link #{index}: unsupported link type '{link_type}'")

    for ep in endpoints:
        if ep.node not in nodes:
            raise TopologyError(f"Link #{index} references unknown node '{ep.node}'")
    return Link(index=index, raw=raw, endpoints=endpoints)


def dump_yaml(data: dict) -> str:
    """Serialise to YAML, keeping multi-line strings (configs) readable."""
    return yaml.dump(data, Dumper=_BlockDumper, default_flow_style=False,
                     sort_keys=False, allow_unicode=True)


class _BlockDumper(yaml.SafeDumper):
    pass


def _str_representer(dumper, value: str):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_BlockDumper.add_representer(str, _str_representer)
