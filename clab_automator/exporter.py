"""Build a containerlab topology from a live network.

Connects to real devices via NAPALM, pulls running configs and LLDP
neighbours, and writes a containerlab topology that mirrors the production
network: one node per device (with its running config as the startup
config) and one link per LLDP adjacency between inventoried devices.
"""

import logging
import re
from pathlib import Path
from typing import Optional

from .topology import dump_yaml

logger = logging.getLogger(__name__)

# NAPALM driver → containerlab kind
PLATFORM_KIND_MAP = {
    "ios": "cisco_iol",
    "iosxr": "cisco_xrd",
    "iosxr_netconf": "cisco_xrd",
    "eos": "arista_ceos",
    "nxos": "cisco_n9kv",
    "nxos_ssh": "cisco_n9kv",
    "junos": "juniper_vjunosrouter",
    "panos": "paloalto_panos",
    "fortios": "fortinet_fortigate",
    "sros": "nokia_sros",
    "srl": "nokia_srlinux",
}


class ExportError(Exception):
    """Raised when an export operation fails."""


def export_from_live_network(
    devices: list[dict],
    output_file: Optional[str | Path] = None,
    lab_name: str = "imported-topology",
    kind_map: Optional[dict[str, str]] = None,
) -> dict:
    """Build a containerlab topology by connecting to real network devices.

    Args:
        devices: List of device dicts, each with:
            - hostname (str): device FQDN or IP
            - platform (str): NAPALM driver name (ios, eos, junos, nxos_ssh, ...)
            - username / password (str)
            - kind (str, optional): containerlab kind (default: from platform)
            - image (str, optional): container image for the node
            - type (str, optional): containerlab node type (e.g. "L2" for IOL-L2)
            - optional_args (dict, optional): extra NAPALM args
        output_file: Write the topology here. Running configs are written to
            ``configs/<node>.cfg`` next to it; without an output file they
            are embedded inline.
        lab_name: Name for the generated lab.
        kind_map: Override the default platform → kind mapping.

    Returns:
        The topology dict.

    Requires: ``pip install napalm``
    """
    try:
        from napalm import get_network_driver
    except ImportError:
        raise ExportError(
            "NAPALM is required for live network export. "
            "Install it with: pip install 'clab-automator[napalm]'"
        )

    kinds = {**PLATFORM_KIND_MAP, **(kind_map or {})}
    output_path = Path(output_file) if output_file else None

    nodes: dict[str, dict] = {}
    collected: list[tuple[str, dict]] = []  # (node name, lldp neighbours)
    aliases: dict[str, str] = {}            # hostname/FQDN variants → node name

    for dev_def in devices:
        hostname = dev_def["hostname"]
        platform = dev_def["platform"]
        logger.info("Connecting to %s (%s)", hostname, platform)

        driver = get_network_driver(platform)
        device = driver(
            hostname=hostname,
            username=dev_def["username"],
            password=dev_def["password"],
            optional_args=dev_def.get("optional_args", {}),
        )
        try:
            device.open()
            facts = device.get_facts()
            running_config = device.get_config()["running"]
            neighbors = device.get_lldp_neighbors_detail()
            device.close()
        except Exception as exc:
            logger.error("Failed to collect data from %s: %s", hostname, exc)
            continue

        device_name = facts.get("hostname") or hostname
        node_name = _node_name(device_name)
        kind = dev_def.get("kind") or kinds.get(platform)
        if not kind:
            logger.warning("No containerlab kind for platform '%s' — using linux", platform)
            kind = "linux"

        node: dict = {"kind": kind}
        if dev_def.get("type"):
            node["type"] = dev_def["type"]
        if dev_def.get("image"):
            node["image"] = dev_def["image"]
        else:
            node["image"] = f"REPLACE-ME/{kind}:latest"
            logger.warning("No image given for %s — set 'image' in the output", node_name)

        if output_path:
            cfg_rel = Path("configs") / f"{node_name}.cfg"
            cfg_file = output_path.parent / cfg_rel
            cfg_file.parent.mkdir(parents=True, exist_ok=True)
            cfg_file.write_text(running_config)
            node["startup-config"] = cfg_rel.as_posix()
        else:
            node["startup-config"] = running_config

        nodes[node_name] = node
        collected.append((node_name, neighbors))
        for alias in (hostname, device_name, facts.get("fqdn")):
            if alias:
                aliases[alias.lower()] = node_name
                aliases[alias.lower().split(".")[0]] = node_name

    # Second pass — LLDP adjacencies between devices we actually imported
    links: list[dict] = []
    seen: set[tuple] = set()
    for node_name, neighbors in collected:
        for local_iface, neigh_list in neighbors.items():
            for neigh in neigh_list:
                remote_sys = (neigh.get("remote_system_name") or "").lower()
                remote_node = aliases.get(remote_sys) or aliases.get(remote_sys.split(".")[0])
                remote_iface = neigh.get("remote_port", "")
                if not remote_node or not remote_iface:
                    logger.info(
                        "Skipping LLDP neighbour %r on %s:%s (not in inventory)",
                        neigh.get("remote_system_name"), node_name, local_iface,
                    )
                    continue
                key = tuple(sorted([(node_name, local_iface), (remote_node, remote_iface)]))
                if key in seen:
                    continue
                seen.add(key)
                links.append({
                    "endpoints": [f"{node_name}:{local_iface}", f"{remote_node}:{remote_iface}"],
                })

    topo = {
        "name": lab_name,
        "topology": {"nodes": nodes, "links": links},
    }

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"# Imported from live network ({len(nodes)} devices) by clab-automator.\n"
            "# Interface names come from the devices; check they match the naming\n"
            "# each containerlab kind accepts before deploying.\n"
        )
        output_path.write_text(header + dump_yaml(topo))
        logger.info("Live network topology exported to %s", output_path)

    return topo


def _node_name(name: str) -> str:
    """Make a device hostname safe for use as a containerlab node name."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", name.split(".")[0]) or "node"
