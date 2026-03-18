"""Topology exporter — extract a running EVE-NG lab into a reusable YAML file.

Two modes:
  1. Export from EVE-NG: reads an existing lab's nodes, networks, links, and
     startup configs via the REST API.
  2. Export from live network: connects to real devices (via NAPALM/Netmiko),
     pulls running configs and LLDP/CDP neighbor data, and builds a topology
     YAML that mirrors the production network.
"""

import logging
from pathlib import Path
from typing import Optional

import yaml

from .api_client import EveNgClient, EveNgApiError

logger = logging.getLogger(__name__)

# Maps EVE-NG template names to common vendor platforms for NAPALM
TEMPLATE_PLATFORM_MAP = {
    "vios": "ios",
    "iosvl2": "ios",
    "csr1000v": "ios",
    "iosxrv": "iosxr",
    "veos": "eos",
    "nxosv9k": "nxos_ssh",
    "nxosv": "nxos_ssh",
    "vjunos": "junos",
    "vsrx": "junos",
    "vmx": "junos",
    "paloalto": "panos",
    "fortinet": "fortios",
}


class ExportError(Exception):
    """Raised when an export operation fails."""


# ======================================================================
# Mode 1: Export from EVE-NG
# ======================================================================

def export_lab(
    client: EveNgClient,
    lab_path: str,
    output_file: Optional[str | Path] = None,
    include_configs: bool = True,
) -> dict:
    """Export an existing EVE-NG lab to a topology dict (and optionally a file).

    Args:
        client: Authenticated EveNgClient.
        lab_path: Path to the lab (e.g. "/my-lab" or "/folder/my-lab.unl").
        output_file: If set, write the YAML to this path.
        include_configs: If True, export startup configs for each node.

    Returns:
        The topology dict.
    """
    logger.info("Exporting lab '%s' from EVE-NG", lab_path)

    # Get lab metadata
    lab_info = client.get_lab(lab_path)

    topo = {
        "lab": {
            "name": lab_info.get("name", ""),
            "description": lab_info.get("description", ""),
            "author": lab_info.get("author", ""),
            "path": "/",
            "version": str(lab_info.get("version", "1")),
        },
        "nodes": [],
        "networks": [],
        "links": [],
    }

    # --- Nodes ---
    nodes_data = client.list_nodes(lab_path) or {}
    node_id_to_name = {}

    for node_id_str, node_info in nodes_data.items():
        node_id = int(node_id_str)
        name = node_info.get("name", f"node-{node_id}")
        node_id_to_name[node_id] = name

        node_entry = {
            "name": name,
            "template": node_info.get("template", ""),
            "image": node_info.get("image", ""),
            "ethernet": node_info.get("ethernet", 2),
            "serial": node_info.get("serial", 0),
            "ram": node_info.get("ram", 512),
            "cpu": node_info.get("cpu", 1),
            "console": node_info.get("console", "telnet"),
            "icon": node_info.get("icon", ""),
            "left": node_info.get("left", 0),
            "top": node_info.get("top", 0),
        }

        # Export startup config
        if include_configs:
            try:
                config_resp = client.get_node_config(lab_path, node_id)
                config_text = ""
                if isinstance(config_resp, dict):
                    config_text = config_resp.get("data", "")
                elif isinstance(config_resp, str):
                    config_text = config_resp
                if config_text:
                    node_entry["startup_config"] = config_text
            except EveNgApiError:
                logger.debug("No config available for node %s", name)

        topo["nodes"].append(node_entry)

    # --- Networks ---
    networks_data = client.list_networks(lab_path) or {}
    net_id_to_name = {}

    for net_id_str, net_info in networks_data.items():
        net_id = int(net_id_str)
        name = net_info.get("name", f"net-{net_id}")
        net_id_to_name[net_id] = name

        topo["networks"].append({
            "name": name,
            "type": net_info.get("type", "bridge"),
            "visibility": net_info.get("visibility", 1),
        })

    # --- Links (interface→network mappings) ---
    for node_id_str, node_info in nodes_data.items():
        node_id = int(node_id_str)
        node_name = node_id_to_name[node_id]

        try:
            interfaces = client.get_node_interfaces(lab_path, node_id)
        except EveNgApiError:
            continue

        for itype in ("ethernet", "serial"):
            ifaces = interfaces.get(itype, {})
            for iface_id_str, iface_info in ifaces.items():
                net_id = iface_info.get("network_id")
                if not net_id or net_id == 0:
                    continue
                net_name = net_id_to_name.get(int(net_id))
                if not net_name:
                    continue

                iface_name = iface_info.get("name", iface_id_str)
                topo["links"].append({
                    "node": node_name,
                    "interface": iface_name,
                    "network": net_name,
                })

    # --- Write output ---
    if output_file:
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as fh:
            yaml.dump(topo, fh, default_flow_style=False, sort_keys=False)
        logger.info("Topology exported to %s", output_path)

    return topo


# ======================================================================
# Mode 2: Export from live network (via NAPALM)
# ======================================================================

def export_from_live_network(
    devices: list[dict],
    output_file: Optional[str | Path] = None,
    lab_name: str = "imported-topology",
    template_map: Optional[dict[str, str]] = None,
) -> dict:
    """Build a topology YAML by connecting to real network devices.

    Uses NAPALM to pull running configs and LLDP/CDP neighbour data,
    then constructs a topology that mirrors the production network.

    Args:
        devices: List of device dicts, each with:
            - hostname (str): device FQDN or IP
            - platform (str): NAPALM driver name (ios, eos, junos, nxos_ssh, etc.)
            - username (str)
            - password (str)
            - template (str): EVE-NG template to use (e.g. "vios", "veos")
            - image (str, optional): EVE-NG image name
            - optional_args (dict, optional): extra NAPALM args
        output_file: Write YAML here if set.
        lab_name: Name for the generated lab.
        template_map: Override default platform→template mapping.

    Returns:
        The topology dict.

    Requires: ``pip install napalm``
    """
    try:
        from napalm import get_network_driver
    except ImportError:
        raise ExportError(
            "NAPALM is required for live network export. "
            "Install it with: pip install napalm"
        )

    tpl_map = {**TEMPLATE_PLATFORM_MAP, **(template_map or {})}

    topo = {
        "lab": {
            "name": lab_name,
            "description": f"Imported from live network ({len(devices)} devices)",
            "author": "eve-ng-automator",
            "path": "/",
        },
        "nodes": [],
        "links": [],
        "networks": [],
    }

    # Collect LLDP neighbors and configs from each device
    all_neighbors: dict[str, list[dict]] = {}
    seen_links: set[tuple] = set()
    net_counter = 0

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
            interfaces = device.get_interfaces()
            device.close()
        except Exception as exc:
            logger.error("Failed to collect data from %s: %s", hostname, exc)
            continue

        device_name = facts.get("hostname", hostname)
        ethernet_count = max(len(interfaces), 2)

        # Determine EVE-NG template
        template = dev_def.get("template", "")
        if not template:
            # Guess from platform or model
            for key, tpl in tpl_map.items():
                if key in platform.lower():
                    template = tpl
                    break
            if not template:
                template = "vios"  # fallback

        node_entry = {
            "name": device_name,
            "template": template,
            "ethernet": ethernet_count,
            "ram": dev_def.get("ram", 1024),
            "cpu": dev_def.get("cpu", 1),
            "console": "telnet",
            "startup_config": running_config,
        }
        if "image" in dev_def:
            node_entry["image"] = dev_def["image"]

        topo["nodes"].append(node_entry)
        all_neighbors[device_name] = []

        # Process LLDP neighbors to build links
        for local_iface, neigh_list in neighbors.items():
            for neigh in neigh_list:
                remote_name = neigh.get("remote_system_name", "")
                remote_iface = neigh.get("remote_port", "")
                if not remote_name:
                    continue

                # Create a canonical link key to avoid duplicates
                link_key = tuple(sorted([
                    (device_name, local_iface),
                    (remote_name, remote_iface),
                ]))
                if link_key in seen_links:
                    continue
                seen_links.add(link_key)

                net_name = f"link-{device_name}-{remote_name}-{net_counter}"
                net_counter += 1

                topo["networks"].append({
                    "name": net_name,
                    "type": "bridge",
                    "visibility": 0,
                })
                topo["links"].extend([
                    {
                        "node": device_name,
                        "interface": local_iface,
                        "network": net_name,
                    },
                    {
                        "node": remote_name,
                        "interface": remote_iface,
                        "network": net_name,
                    },
                ])

    if output_file:
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as fh:
            yaml.dump(topo, fh, default_flow_style=False, sort_keys=False)
        logger.info("Live network topology exported to %s", output_path)

    return topo
