"""Distributed topology deployer — orchestrates multi-host deployments.

Splits a topology across multiple EVE-NG servers, deploys the per-host
sub-topologies, sets up cross-host tunnels, and provides coordinated
teardown.

Workflow:
  1. Load cluster config + topology
  2. Probe host resources
  3. Run placement engine → decide which nodes go where
  4. Split topology into per-host sub-topologies
  5. Deploy each sub-topology via the single-host deployer
  6. Set up GRE/VXLAN tunnels for cross-host links
  7. Wire cross-host interfaces to pnet (cloud) networks
"""

import json
import logging
from pathlib import Path
from typing import Optional

from .api_client import EveNgClient, EveNgApiError
from .cluster import (
    ClusterConfig,
    HostInfo,
    create_client,
    load_cluster_config,
    probe_host_resources,
)
from .deployer import TopologyDeployer, DeploymentError
from .interconnect import InterconnectManager, Tunnel
from .placement import PlacementPlan, compute_placement, PlacementError
from .teardown import teardown_lab
from .topology_schema import load_topology, resolve_interface_id

logger = logging.getLogger(__name__)


class DistributedDeploymentError(Exception):
    """Raised when a distributed deployment fails."""


class DistributedDeployer:
    """Orchestrate topology deployment across a cluster of EVE-NG hosts."""

    def __init__(
        self,
        cluster_config: ClusterConfig,
        ssh_username: str = "root",
        ssh_password: Optional[str] = None,
        ssh_key_file: Optional[str] = None,
    ):
        self.cluster = cluster_config
        self.ssh_username = ssh_username
        self.ssh_password = ssh_password
        self.ssh_key_file = ssh_key_file

        self._clients: dict[str, EveNgClient] = {}
        self._placement: Optional[PlacementPlan] = None
        self._tunnels: list[Tunnel] = []
        self._interconnect: Optional[InterconnectManager] = None

    def deploy(
        self,
        topology_file: str | Path,
        strategy: str = "bin-pack",
        start_nodes: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Deploy a topology across the cluster.

        Args:
            topology_file: Path to topology YAML.
            strategy: Placement strategy ("bin-pack", "spread", "resource").
            start_nodes: Start all nodes after deployment.
            dry_run: Show placement plan without deploying.

        Returns:
            Summary dict with placements, per-host results, and tunnel info.
        """
        topo = load_topology(topology_file)
        lab = topo["lab"]
        nodes = topo.get("nodes", [])
        networks = topo.get("networks", [])
        links = topo.get("links", [])

        # --- Pro mode: deploy the full topology to the cluster master ---
        # EVE-NG Pro handles node placement and inter-host networking
        # internally, so we treat the cluster master as a single target.
        if self.cluster.is_pro:
            return self._deploy_pro(topo, start_nodes=start_nodes, dry_run=dry_run)

        summary = {
            "lab": lab["name"],
            "strategy": strategy,
            "hosts": {},
            "placement": {},
            "tunnels": [],
        }

        # --- Step 1: Connect to all hosts and probe resources ---
        logger.info("Connecting to %d cluster hosts", len(self.cluster.hosts))
        for host_info in self.cluster.hosts:
            client = create_client(host_info)
            try:
                client.login()
                probe_host_resources(client, host_info)
                self._clients[host_info.name] = client
                logger.info(
                    "Host %s: %dCPU / %dMB RAM available",
                    host_info.name, host_info.available_cpu, host_info.available_ram,
                )
            except Exception as exc:
                logger.error("Cannot connect to host %s: %s", host_info.name, exc)
                raise DistributedDeploymentError(
                    f"Host {host_info.name} ({host_info.host}) unreachable: {exc}"
                ) from exc

        # --- Step 2: Compute placement ---
        logger.info("Computing placement with strategy '%s'", strategy)
        try:
            self._placement = compute_placement(
                nodes, links, self.cluster.hosts, strategy
            )
        except PlacementError as exc:
            raise DistributedDeploymentError(f"Placement failed: {exc}") from exc

        placement_summary = self._placement.summary()
        summary["placement"] = placement_summary
        logger.info("Placement: %s", json.dumps(placement_summary, indent=2))

        if dry_run:
            summary["dry_run"] = True
            self._logout_all()
            return summary

        # --- Step 3: Split topology and deploy per-host ---
        host_topos = self._split_topology(topo)

        for host_name, host_topo in host_topos.items():
            client = self._clients[host_name]
            logger.info(
                "Deploying %d nodes on host '%s'",
                len(host_topo.get("nodes", [])), host_name,
            )
            deployer = TopologyDeployer(client)
            try:
                host_result = deployer._deploy(
                    host_topo, start_nodes=False, dry_run=False
                )
                summary["hosts"][host_name] = host_result
            except DeploymentError as exc:
                logger.error("Deployment failed on %s: %s", host_name, exc)
                summary["hosts"][host_name] = {"error": str(exc)}

        # --- Step 4: Set up cross-host tunnels ---
        if self._placement.cross_host_links:
            logger.info(
                "Setting up %d cross-host tunnels",
                len(self._placement.cross_host_links),
            )
            host_lookup = {h.name: h for h in self.cluster.hosts}
            self._interconnect = InterconnectManager(
                self.cluster,
                ssh_username=self.ssh_username,
                ssh_password=self.ssh_password,
                ssh_key_file=self.ssh_key_file,
            )
            try:
                self._tunnels = self._interconnect.setup_tunnels(
                    self._placement.cross_host_links, host_lookup
                )
                summary["tunnels"] = [
                    {
                        "id": t.tunnel_id,
                        "network": t.network_name,
                        "host_a": t.endpoint_a.host.name,
                        "host_b": t.endpoint_b.host.name,
                        "mode": self.cluster.tunnel_mode,
                    }
                    for t in self._tunnels
                ]
            except Exception as exc:
                logger.error("Tunnel setup failed: %s", exc)
                summary["tunnel_error"] = str(exc)

            # Wire cross-host interfaces to the pnet on each host
            self._wire_cross_host_links(topo, host_topos)

        # --- Step 5: Start nodes if requested ---
        if start_nodes:
            for host_name, client in self._clients.items():
                host_topo = host_topos.get(host_name)
                if host_topo:
                    lab_name = host_topo["lab"]["name"]
                    lab_path = f"{host_topo['lab']['path'].rstrip('/')}/{lab_name}"
                    try:
                        client.start_all_nodes(lab_path)
                        logger.info("Started nodes on %s", host_name)
                    except EveNgApiError as exc:
                        logger.error("Failed to start nodes on %s: %s", host_name, exc)

        self._logout_all()
        logger.info("Distributed deployment complete")
        return summary

    def teardown(
        self,
        topology_file: str | Path,
        remove_tunnels: bool = True,
    ) -> dict:
        """Tear down a distributed topology across the cluster.

        This reads the topology and cluster config, determines which labs
        exist on which hosts, and tears them all down.
        """
        topo = load_topology(topology_file)
        lab = topo["lab"]

        # Pro mode: teardown on the cluster master only
        if self.cluster.is_pro:
            return self._teardown_pro(topo)

        summary = {"lab": lab["name"], "hosts": {}}

        # Connect to all hosts
        for host_info in self.cluster.hosts:
            client = create_client(host_info)
            try:
                client.login()
                self._clients[host_info.name] = client
            except Exception as exc:
                logger.warning("Cannot connect to %s: %s", host_info.name, exc)
                continue

        # Build the lab path (same naming convention as deploy)
        lab_folder = lab.get("path", "/")

        # Tear down on each host
        for host_info in self.cluster.hosts:
            client = self._clients.get(host_info.name)
            if not client:
                continue

            # Each host gets a lab named "<lab_name>-<host_name>"
            host_lab_name = f"{lab['name']}-{host_info.name}"
            host_lab_path = f"{lab_folder.rstrip('/')}/{host_lab_name}"

            try:
                result = teardown_lab(client, host_lab_path)
                summary["hosts"][host_info.name] = result
            except Exception as exc:
                logger.warning(
                    "Teardown on %s failed (lab may not exist): %s",
                    host_info.name, exc,
                )
                summary["hosts"][host_info.name] = {"error": str(exc)}

        # Remove tunnels
        if remove_tunnels:
            interconnect = InterconnectManager(
                self.cluster,
                ssh_username=self.ssh_username,
                ssh_password=self.ssh_password,
                ssh_key_file=self.ssh_key_file,
            )
            interconnect.teardown_all_tunnels_on_hosts(self.cluster.hosts)
            summary["tunnels_removed"] = True

        self._logout_all()
        return summary

    def _deploy_pro(
        self, topo: dict, start_nodes: bool, dry_run: bool
    ) -> dict:
        """Deploy to EVE-NG Pro cluster master.

        Pro's native clustering handles node distribution and inter-host
        networking automatically, so we deploy the full topology as-is
        to the cluster master (first host in the inventory).
        """
        master = self.cluster.master
        logger.info(
            "Pro mode: deploying full topology to cluster master '%s' (%s)",
            master.name, master.host,
        )

        client = create_client(master)
        try:
            client.login()
        except Exception as exc:
            raise DistributedDeploymentError(
                f"Cannot connect to cluster master {master.name} "
                f"({master.host}): {exc}"
            ) from exc

        deployer = TopologyDeployer(client)
        try:
            result = deployer._deploy(topo, start_nodes=start_nodes, dry_run=dry_run)
        except DeploymentError as exc:
            raise DistributedDeploymentError(
                f"Deployment to cluster master failed: {exc}"
            ) from exc
        finally:
            try:
                client.logout()
            except Exception:
                pass

        return {
            "lab": topo["lab"]["name"],
            "edition": "pro",
            "master": master.name,
            "hosts": {master.name: result},
            "placement": "managed by EVE-NG Pro cluster",
            "tunnels": [],
        }

    def _teardown_pro(self, topo: dict) -> dict:
        """Tear down a lab on EVE-NG Pro cluster master."""
        master = self.cluster.master
        lab = topo["lab"]
        lab_folder = lab.get("path", "/")
        lab_path = f"{lab_folder.rstrip('/')}/{lab['name']}"

        logger.info(
            "Pro mode: tearing down lab '%s' on cluster master '%s'",
            lab_path, master.name,
        )

        client = create_client(master)
        try:
            client.login()
            result = teardown_lab(client, lab_path)
            client.logout()
        except Exception as exc:
            raise DistributedDeploymentError(
                f"Teardown on cluster master failed: {exc}"
            ) from exc

        return {
            "lab": lab["name"],
            "edition": "pro",
            "master": master.name,
            "hosts": {master.name: result},
        }

    def _split_topology(self, topo: dict) -> dict[str, dict]:
        """Split a topology into per-host sub-topologies.

        Each host gets its own lab with only the nodes placed there.
        Local links (both endpoints on same host) are included directly.
        Cross-host links are handled separately via tunnels.
        """
        lab = topo["lab"]
        all_nodes = {n["name"]: n for n in topo.get("nodes", [])}
        all_networks = {n["name"]: n for n in topo.get("networks", [])}
        links = topo.get("links", [])

        # Group nodes by host
        host_topos: dict[str, dict] = {}
        for host_info in self.cluster.hosts:
            node_names = self._placement.nodes_on_host(host_info.name)
            if not node_names:
                continue

            host_nodes = [all_nodes[n] for n in node_names if n in all_nodes]

            # Find local links: both node endpoints on this host
            host_node_set = set(node_names)
            host_links = []
            host_net_names = set()

            for link in links:
                if link["node"] in host_node_set:
                    net_name = link["network"]
                    # Check if all nodes on this network are local
                    net_nodes = {
                        l["node"] for l in links if l["network"] == net_name
                    }
                    if net_nodes.issubset(host_node_set):
                        host_links.append(link)
                        host_net_names.add(net_name)

            host_networks = [
                all_networks[n] for n in host_net_names if n in all_networks
            ]

            host_topos[host_info.name] = {
                "lab": {
                    **lab,
                    "name": f"{lab['name']}-{host_info.name}",
                    "description": (
                        f"{lab.get('description', '')} "
                        f"[host: {host_info.name}]"
                    ).strip(),
                },
                "nodes": host_nodes,
                "networks": host_networks,
                "links": host_links,
            }

        return host_topos

    def _wire_cross_host_links(
        self,
        topo: dict,
        host_topos: dict[str, dict],
    ) -> None:
        """Connect cross-host node interfaces to the tunnel pnet.

        For each cross-host link, the node interface needs to be connected
        to the pnet cloud network that carries the tunnel traffic.
        """
        links = topo.get("links", [])
        pnet = self.cluster.tunnel_pnet  # e.g. "pnet9"

        for xlink in self._placement.cross_host_links:
            net_name = xlink["network"]
            # Find all link entries for this network
            net_links = [l for l in links if l["network"] == net_name]

            for link in net_links:
                node_name = link["node"]
                iface_spec = link["interface"]
                host_name = self._placement.host_for_node(node_name)
                if not host_name:
                    continue

                client = self._clients.get(host_name)
                if not client:
                    continue

                host_topo = host_topos.get(host_name)
                if not host_topo:
                    continue

                lab_name = host_topo["lab"]["name"]
                lab_path = f"{host_topo['lab']['path'].rstrip('/')}/{lab_name}"

                # Create a pnet cloud network on the host lab if needed
                try:
                    pnet_net_name = f"tunnel-{net_name}"
                    result = client.create_network(lab_path, {
                        "name": pnet_net_name,
                        "type": pnet,
                        "visibility": 0,
                    })
                    # Get the network ID
                    net_id = _extract_net_id(result, client, lab_path, pnet_net_name)
                except EveNgApiError as exc:
                    logger.error(
                        "Failed to create tunnel network on %s: %s",
                        host_name, exc,
                    )
                    continue

                # Find the node ID on this host
                node_id = _find_node_id(client, lab_path, node_name)
                if node_id is None:
                    logger.error("Cannot find node '%s' on %s", node_name, host_name)
                    continue

                # Resolve and connect the interface
                try:
                    interfaces = client.get_node_interfaces(lab_path, node_id)
                    iface_id = resolve_interface_id(iface_spec, interfaces)
                    client.connect_interface(lab_path, node_id, iface_id, net_id)
                    logger.info(
                        "Connected %s:%s to tunnel pnet on %s",
                        node_name, iface_spec, host_name,
                    )
                except Exception as exc:
                    logger.error(
                        "Failed to wire %s:%s on %s: %s",
                        node_name, iface_spec, host_name, exc,
                    )

    def _logout_all(self) -> None:
        for name, client in self._clients.items():
            try:
                client.logout()
            except Exception:
                pass
        self._clients.clear()


def _extract_net_id(
    result, client: EveNgClient, lab_path: str, net_name: str
) -> int:
    """Extract network ID from create response, or look it up."""
    if isinstance(result, dict):
        for key in ("id", "data"):
            if key in result:
                val = result[key]
                if isinstance(val, int):
                    return val
                if isinstance(val, str) and val.isdigit():
                    return int(val)

    # Fall back to listing networks
    networks = client.list_networks(lab_path) or {}
    for nid, ninfo in networks.items():
        if ninfo.get("name") == net_name:
            return int(nid)

    raise DistributedDeploymentError(
        f"Cannot determine network ID for '{net_name}'"
    )


def _find_node_id(
    client: EveNgClient, lab_path: str, node_name: str
) -> int | None:
    """Find a node ID by name in a lab."""
    nodes = client.list_nodes(lab_path) or {}
    for nid, ninfo in nodes.items():
        if ninfo.get("name") == node_name:
            return int(nid)
    return None
