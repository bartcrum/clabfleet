"""Topology deployer — builds an EVE-NG lab from a topology definition.

Workflow:
  1. Create the lab
  2. Create all nodes (with interface counts matching the topology)
  3. Upload startup configs
  4. Create all networks (bridges)
  5. Wire interfaces to networks
  6. Optionally start all nodes
"""

import logging
import time
from pathlib import Path
from typing import Optional

from .api_client import EveNgClient, EveNgApiError
from .topology_schema import (
    load_topology,
    load_topology_from_string,
    resolve_interface_id,
)

logger = logging.getLogger(__name__)


class DeploymentError(Exception):
    """Raised when a deployment step fails."""


class TopologyDeployer:
    """Deploy a topology YAML into a running EVE-NG instance."""

    def __init__(self, client: EveNgClient):
        self.client = client
        # Maps topology names → EVE-NG IDs created during deployment
        self._node_ids: dict[str, int] = {}
        self._network_ids: dict[str, int] = {}
        self._lab_path: str = ""

    def deploy_from_file(
        self,
        topology_file: str | Path,
        start_nodes: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Deploy a topology from a YAML file.

        Returns a summary dict with created resource IDs.
        """
        topo = load_topology(topology_file)
        return self._deploy(topo, start_nodes=start_nodes, dry_run=dry_run)

    def deploy_from_string(
        self,
        yaml_str: str,
        start_nodes: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Deploy a topology from a YAML string."""
        topo = load_topology_from_string(yaml_str)
        return self._deploy(topo, start_nodes=start_nodes, dry_run=dry_run)

    def _deploy(self, topo: dict, start_nodes: bool, dry_run: bool) -> dict:
        lab = topo["lab"]
        nodes = topo.get("nodes", [])
        networks = topo.get("networks", [])
        links = topo.get("links", [])

        lab_name = lab["name"]
        lab_folder = lab.get("path", "/")
        self._lab_path = f"{lab_folder.rstrip('/')}/{lab_name}"

        summary = {
            "lab": self._lab_path,
            "nodes": {},
            "networks": {},
            "links": [],
        }

        if dry_run:
            logger.info("[DRY RUN] Would deploy lab '%s'", lab_name)
            logger.info("[DRY RUN] %d nodes, %d networks, %d links",
                        len(nodes), len(networks), len(links))
            summary["dry_run"] = True
            return summary

        # --- Step 1: Create lab ---
        logger.info("Creating lab '%s' in folder '%s'", lab_name, lab_folder)
        try:
            self.client.create_lab(
                name=lab_name,
                path=lab_folder,
                version=lab.get("version", "1"),
                description=lab.get("description", ""),
                author=lab.get("author", ""),
            )
        except EveNgApiError as exc:
            if "already exists" in str(exc).lower():
                logger.warning("Lab '%s' already exists — continuing", lab_name)
            else:
                raise DeploymentError(f"Failed to create lab: {exc}") from exc

        # --- Step 2: Create nodes ---
        for node_def in nodes:
            self._create_node(node_def)

        # --- Step 3: Upload startup configs ---
        for node_def in nodes:
            config_text = node_def.get("startup_config")
            if config_text:
                node_id = self._node_ids[node_def["name"]]
                logger.info("Uploading startup config for %s (id=%d)",
                            node_def["name"], node_id)
                try:
                    self.client.set_node_config(
                        self._lab_path, node_id, config_text
                    )
                except EveNgApiError as exc:
                    logger.error("Failed to set config for %s: %s",
                                 node_def["name"], exc)

        # --- Step 4: Create networks ---
        for net_def in networks:
            self._create_network(net_def)

        # --- Step 5: Wire links ---
        for link_def in links:
            self._wire_link(link_def)

        # --- Step 6: Optionally start nodes ---
        if start_nodes:
            logger.info("Starting all nodes in lab '%s'", lab_name)
            try:
                self.client.start_all_nodes(self._lab_path)
            except EveNgApiError as exc:
                logger.error("Failed to start nodes: %s", exc)

        summary["nodes"] = dict(self._node_ids)
        summary["networks"] = dict(self._network_ids)
        summary["links"] = links
        logger.info(
            "Deployment complete: %d nodes, %d networks, %d links",
            len(self._node_ids),
            len(self._network_ids),
            len(links),
        )
        return summary

    def _create_node(self, node_def: dict) -> None:
        name = node_def["name"]
        logger.info("Creating node '%s' (template=%s)", name, node_def["template"])

        payload = {
            "type": node_def.get("type", "qemu"),
            "template": node_def["template"],
            "name": name,
            "ethernet": node_def.get("ethernet", 2),
            "serial": node_def.get("serial", 0),
            "console": node_def.get("console", "telnet"),
            "config": node_def.get("config", "Unconfigured"),
        }

        # Optional fields — only include if specified
        for key in ("image", "ram", "cpu", "icon", "left", "top", "delay"):
            if key in node_def:
                payload[key] = node_def[key]

        try:
            result = self.client.create_node(self._lab_path, payload)
            # EVE-NG returns the node ID in the response
            node_id = _extract_id(result)
            self._node_ids[name] = node_id
            logger.info("Created node '%s' → id=%d", name, node_id)
        except EveNgApiError as exc:
            raise DeploymentError(f"Failed to create node '{name}': {exc}") from exc

    def _create_network(self, net_def: dict) -> None:
        name = net_def["name"]
        logger.info("Creating network '%s' (type=%s)", name, net_def["type"])

        payload = {
            "name": name,
            "type": net_def["type"],
            "visibility": net_def.get("visibility", 1),
        }
        for key in ("left", "top"):
            if key in net_def:
                payload[key] = net_def[key]

        try:
            result = self.client.create_network(self._lab_path, payload)
            net_id = _extract_id(result)
            self._network_ids[name] = net_id
            logger.info("Created network '%s' → id=%d", name, net_id)
        except EveNgApiError as exc:
            raise DeploymentError(
                f"Failed to create network '{name}': {exc}"
            ) from exc

    def _wire_link(self, link_def: dict) -> None:
        node_name = link_def["node"]
        iface_spec = link_def["interface"]
        net_name = link_def["network"]

        node_id = self._node_ids.get(node_name)
        if node_id is None:
            raise DeploymentError(f"Link references unknown node '{node_name}'")
        net_id = self._network_ids.get(net_name)
        if net_id is None:
            raise DeploymentError(f"Link references unknown network '{net_name}'")

        # Resolve the interface name → numeric ID
        try:
            interfaces = self.client.get_node_interfaces(self._lab_path, node_id)
            iface_id = resolve_interface_id(iface_spec, interfaces)
        except Exception as exc:
            raise DeploymentError(
                f"Cannot resolve interface '{iface_spec}' on node '{node_name}': {exc}"
            ) from exc

        logger.info(
            "Connecting %s:%s (iface=%d) → %s (net=%d)",
            node_name, iface_spec, iface_id, net_name, net_id,
        )
        try:
            self.client.connect_interface(
                self._lab_path, node_id, iface_id, net_id
            )
        except EveNgApiError as exc:
            raise DeploymentError(
                f"Failed to connect {node_name}:{iface_spec} → {net_name}: {exc}"
            ) from exc


def _extract_id(api_response) -> int:
    """Extract the resource ID from an EVE-NG API create response.

    The API returns the ID in various formats depending on version.
    """
    if isinstance(api_response, dict):
        for key in ("id", "data"):
            if key in api_response:
                val = api_response[key]
                if isinstance(val, int):
                    return val
                if isinstance(val, str) and val.isdigit():
                    return int(val)
                if isinstance(val, dict) and "id" in val:
                    return int(val["id"])
    if isinstance(api_response, (int, str)):
        return int(api_response)
    raise DeploymentError(f"Cannot extract ID from API response: {api_response}")
