"""Topology YAML schema definition and validation.

A topology file describes everything needed to recreate a lab in EVE-NG:
nodes, networks, links, and startup configs.
"""

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# YAML structure reference
# ---------------------------------------------------------------------------
# lab:
#   name: "my-lab"
#   description: "Lab description"
#   author: "netops"
#   path: "/"                       # EVE-NG folder
#   version: "1"
#
# nodes:
#   - name: "R1"
#     template: "vios"              # EVE-NG template name
#     image: "vios-adventerprisek9-m.vmdk.SPA-15.9.3M6"
#     ethernet: 8                   # number of ethernet interfaces
#     serial: 0                     # number of serial interfaces
#     ram: 512                      # MB
#     cpu: 1
#     console: "telnet"             # telnet | vnc
#     icon: "Router.png"
#     config: "Exported"            # "Exported" | "None"
#     startup_config: |             # inline config text (optional)
#       hostname R1
#       ...
#     startup_config_file: "configs/R1.cfg"  # or path to config file
#     left: 400                     # canvas X
#     top: 200                      # canvas Y
#
# networks:
#   - name: "Net-R1R2"
#     type: "bridge"                # bridge | ovs | pnet0..pnet9
#     visibility: 1
#   - name: "Management"
#     type: "pnet1"                 # mapped to a host bridge
#
# links:
#   - node: "R1"
#     interface: "Gi0/0"            # interface name or index
#     network: "Net-R1R2"
#   - node: "R2"
#     interface: "Gi0/0"
#     network: "Net-R1R2"
#   # Point-to-point shorthand:
#   - endpoints:
#       - node: "R1"
#         interface: "Gi0/1"
#       - node: "R2"
#         interface: "Gi0/1"

REQUIRED_LAB_FIELDS = {"name"}
REQUIRED_NODE_FIELDS = {"name", "template"}
REQUIRED_NETWORK_FIELDS = {"name", "type"}

# Maps friendly interface name prefixes to EVE-NG ethernet index offsets.
# These are template-dependent; this covers common Cisco IOS/IOS-XE patterns.
INTERFACE_NAME_PATTERNS = {
    "gi": "ethernet",
    "gigabitethernet": "ethernet",
    "fa": "ethernet",
    "fastethernet": "ethernet",
    "te": "ethernet",
    "tengigabitethernet": "ethernet",
    "eth": "ethernet",
    "ethernet": "ethernet",
    "e": "ethernet",
    "se": "serial",
    "serial": "serial",
    "s": "serial",
    "mgmt": "ethernet",
    "management": "ethernet",
    "lo": None,  # loopback — not a physical link
    "loopback": None,
}


class TopologyValidationError(Exception):
    """Raised when a topology YAML file is invalid."""


def load_topology(file_path: str | Path) -> dict:
    """Load and validate a topology YAML file.

    Returns the parsed topology dict with normalised fields.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"Topology file not found: {file_path}")

    with open(file_path) as fh:
        topo = yaml.safe_load(fh)

    if not isinstance(topo, dict):
        raise TopologyValidationError("Topology file must be a YAML mapping")

    _validate_topology(topo, base_dir=file_path.parent)
    return topo


def load_topology_from_string(yaml_str: str, base_dir: str | Path = ".") -> dict:
    """Load and validate topology from a YAML string."""
    topo = yaml.safe_load(yaml_str)
    if not isinstance(topo, dict):
        raise TopologyValidationError("Topology must be a YAML mapping")
    _validate_topology(topo, base_dir=Path(base_dir))
    return topo


def _validate_topology(topo: dict, base_dir: Path) -> None:
    """Validate and normalise the topology dict in-place."""
    # --- Lab section ---
    lab = topo.get("lab", {})
    missing = REQUIRED_LAB_FIELDS - set(lab.keys())
    if missing:
        raise TopologyValidationError(f"lab section missing fields: {missing}")
    lab.setdefault("path", "/")
    lab.setdefault("description", "")
    lab.setdefault("author", "")
    lab.setdefault("version", "1")
    topo["lab"] = lab

    # --- Nodes ---
    nodes = topo.get("nodes", [])
    if not isinstance(nodes, list):
        raise TopologyValidationError("'nodes' must be a list")
    node_names = set()
    for i, node in enumerate(nodes):
        missing = REQUIRED_NODE_FIELDS - set(node.keys())
        if missing:
            raise TopologyValidationError(
                f"node #{i} ({node.get('name', '?')}) missing fields: {missing}"
            )
        if node["name"] in node_names:
            raise TopologyValidationError(f"Duplicate node name: {node['name']}")
        node_names.add(node["name"])

        # Load startup config from file if specified
        cfg_file = node.get("startup_config_file")
        if cfg_file and "startup_config" not in node:
            cfg_path = base_dir / cfg_file
            if not cfg_path.exists():
                logger.warning("Config file %s not found for node %s", cfg_path, node["name"])
            else:
                node["startup_config"] = cfg_path.read_text()

        # Defaults
        node.setdefault("ethernet", 2)
        node.setdefault("serial", 0)
        node.setdefault("console", "telnet")
        node.setdefault("config", "Exported" if node.get("startup_config") else "None")

    # --- Networks ---
    networks = topo.get("networks", [])
    if not isinstance(networks, list):
        raise TopologyValidationError("'networks' must be a list")
    net_names = set()
    for i, net in enumerate(networks):
        missing = REQUIRED_NETWORK_FIELDS - set(net.keys())
        if missing:
            raise TopologyValidationError(
                f"network #{i} ({net.get('name', '?')}) missing fields: {missing}"
            )
        if net["name"] in net_names:
            raise TopologyValidationError(f"Duplicate network name: {net['name']}")
        net_names.add(net["name"])
        net.setdefault("visibility", 1)

    # --- Links ---
    links = topo.get("links", [])
    if not isinstance(links, list):
        raise TopologyValidationError("'links' must be a list")

    # Auto-create networks for point-to-point endpoint links
    auto_net_idx = 0
    expanded_links = []
    for link in links:
        if "endpoints" in link:
            eps = link["endpoints"]
            if len(eps) != 2:
                raise TopologyValidationError(
                    "Point-to-point 'endpoints' must have exactly 2 entries"
                )
            net_name = link.get("network")
            if not net_name:
                net_name = f"p2p-{eps[0]['node']}-{eps[1]['node']}-{auto_net_idx}"
                auto_net_idx += 1
            if net_name not in net_names:
                networks.append({
                    "name": net_name,
                    "type": link.get("type", "bridge"),
                    "visibility": 0,
                })
                net_names.add(net_name)
            for ep in eps:
                expanded_links.append({
                    "node": ep["node"],
                    "interface": ep["interface"],
                    "network": net_name,
                })
        else:
            expanded_links.append(link)

    topo["links"] = expanded_links
    topo["networks"] = networks


def resolve_interface_id(interface_spec: str | int, node_interfaces: dict) -> int:
    """Resolve a friendly interface name like 'Gi0/0' to an EVE-NG interface ID.

    ``node_interfaces`` is the dict returned by the API's get_node_interfaces().
    It has keys "ethernet" and "serial", each mapping int IDs to interface info.
    """
    # Already numeric
    if isinstance(interface_spec, int):
        return interface_spec

    interface_spec_lower = interface_spec.lower().replace(" ", "")

    # Try direct numeric match
    try:
        return int(interface_spec)
    except ValueError:
        pass

    # Search the API interface list by name
    for itype in ("ethernet", "serial"):
        ifaces = node_interfaces.get(itype, {})
        for iface_id, iface_info in ifaces.items():
            api_name = iface_info.get("name", "").lower().replace(" ", "")
            if api_name == interface_spec_lower:
                return int(iface_id)

    # Heuristic: parse prefix + slot/port → index
    # e.g. Gi0/0 → 0, Gi0/1 → 1, Gi0/0/0 → 0
    for prefix, itype in INTERFACE_NAME_PATTERNS.items():
        if interface_spec_lower.startswith(prefix):
            remainder = interface_spec_lower[len(prefix):]
            # strip any separators and get last number
            parts = remainder.replace("/", " ").replace(".", " ").split()
            if parts:
                try:
                    return int(parts[-1])
                except ValueError:
                    pass

    raise TopologyValidationError(
        f"Cannot resolve interface '{interface_spec}' — use a numeric ID or "
        f"a name matching the EVE-NG interface list"
    )


def dump_topology(topo: dict) -> str:
    """Serialise a topology dict back to YAML."""
    return yaml.dump(topo, default_flow_style=False, sort_keys=False)
