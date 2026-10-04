"""Node placement engine for multi-host deployments.

Decides which containerlab host each node should run on, based on:
  1. Explicit pinning (``lab.host`` node label)
  2. Tag affinity (``lab.host-tags`` label matches host ``tags``)
  3. Resource availability (CPU + RAM best-fit)
  4. Link locality — tries to keep connected nodes on the same host
     to minimise cross-host VXLAN links

Strategies:
  - "bin-pack":   fill each host before moving to the next (fewer VXLAN links)
  - "spread":     distribute nodes evenly across hosts (balanced load),
                  keeping neighbours together when hosts are equally loaded
  - "resource":   always pick the host with the most available resources
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from .cluster import HostInfo

logger = logging.getLogger(__name__)


class PlacementError(Exception):
    """Raised when no viable placement exists."""


@dataclass
class NodePlacement:
    """The result of placing a single node."""
    node_name: str
    host_name: str
    cpu: int
    ram: int


@dataclass
class PlacementPlan:
    """Full placement plan for a topology across a cluster."""
    placements: list[NodePlacement] = field(default_factory=list)
    cross_host_links: list[dict] = field(default_factory=list)

    def host_for_node(self, node_name: str) -> str | None:
        for p in self.placements:
            if p.node_name == node_name:
                return p.host_name
        return None

    def summary(self) -> dict:
        by_host: dict[str, list[str]] = defaultdict(list)
        for p in self.placements:
            by_host[p.host_name].append(p.node_name)
        return {
            "placements": dict(by_host),
            "cross_host_links": len(self.cross_host_links),
            "total_nodes": len(self.placements),
        }


def compute_placement(
    nodes: list[dict],
    links: list[dict],
    hosts: list[HostInfo],
    strategy: str = "bin-pack",
) -> PlacementPlan:
    """Compute where each node should be placed.

    Args:
        nodes: Node summaries from Topology.placement_nodes().
        links: Link memberships from Topology.link_memberships().
        hosts: Available cluster hosts (with current resource usage).
        strategy: "bin-pack" | "spread" | "resource".

    Returns:
        A PlacementPlan mapping each node to a host.
    """
    if not hosts:
        raise PlacementError("No hosts available")

    plan = PlacementPlan()

    # Build adjacency map: node → set of connected nodes
    # (for link-locality optimisation)
    adjacency = _build_adjacency(links)

    # Sort nodes: explicitly pinned nodes first, then by resource demand (desc)
    pinned = [n for n in nodes if n.get("host")]
    unpinned = [n for n in nodes if not n.get("host")]
    unpinned.sort(key=lambda n: (n.get("cpu", 1) * n.get("ram", 512)), reverse=True)
    ordered_nodes = pinned + unpinned

    for node_def in ordered_nodes:
        name = node_def["name"]
        cpu = node_def.get("cpu", 1)
        ram = node_def.get("ram", 512)

        # --- Explicit host pinning ---
        pinned_host = node_def.get("host")
        if pinned_host:
            host = _find_host_by_name(hosts, pinned_host)
            if not host:
                raise PlacementError(
                    f"Node '{name}' pinned to unknown host '{pinned_host}'"
                )
            if not host.can_fit(cpu, ram):
                raise PlacementError(
                    f"Node '{name}' pinned to '{pinned_host}' but host lacks "
                    f"resources (need {cpu}CPU/{ram}MB, have "
                    f"{host.available_cpu}CPU/{host.available_ram}MB)"
                )
            host.reserve(cpu, ram)
            plan.placements.append(NodePlacement(name, host.name, cpu, ram))
            continue

        # --- Tag affinity ---
        host_tags = node_def.get("host_tags", [])
        candidate_hosts = hosts
        if host_tags:
            candidate_hosts = [
                h for h in hosts
                if any(t in h.tags for t in host_tags)
            ]
            if not candidate_hosts:
                logger.warning(
                    "No hosts match tags %s for node '%s' — falling back to all hosts",
                    host_tags, name,
                )
                candidate_hosts = hosts

        # Filter to hosts that have enough resources
        viable = [h for h in candidate_hosts if h.can_fit(cpu, ram)]
        if not viable:
            raise PlacementError(
                f"No host has enough resources for node '{name}' "
                f"(need {cpu}CPU/{ram}MB)"
            )

        # --- Pick host by strategy ---
        chosen = _pick_host(viable, name, adjacency, plan, strategy)
        chosen.reserve(cpu, ram)
        plan.placements.append(NodePlacement(name, chosen.name, cpu, ram))

    # --- Identify cross-host links ---
    plan.cross_host_links = _find_cross_host_links(links, plan)

    logger.info(
        "Placement complete: %d nodes across %d hosts, %d cross-host links",
        len(plan.placements),
        len(set(p.host_name for p in plan.placements)),
        len(plan.cross_host_links),
    )
    return plan


def _pick_host(
    viable: list[HostInfo],
    node_name: str,
    adjacency: dict[str, set[str]],
    plan: PlacementPlan,
    strategy: str,
) -> HostInfo:
    """Pick the best host for a node from the viable candidates."""

    neighbors = adjacency.get(node_name, set())

    def colocated(h: HostInfo) -> int:
        """Neighbours of this node already placed on host h."""
        return sum(1 for n in neighbors if plan.host_for_node(n) == h.name)

    if strategy == "spread":
        # Pick the host with fewest placed nodes; among equally loaded hosts,
        # the one with most neighbours (fewer cross-host links)
        counts = defaultdict(int)
        for p in plan.placements:
            counts[p.host_name] += 1
        return min(viable, key=lambda h: (counts.get(h.name, 0), -colocated(h)))

    elif strategy == "resource":
        # Pick host with most available resources
        return max(viable, key=lambda h: (h.available_cpu, h.available_ram))

    else:  # bin-pack (default)
        # Prefer the host where the most neighbours already live
        # (reduces cross-host links), breaking ties by fewest available
        # resources (pack tightly).
        if neighbors:
            # Higher colocated = better; lower available = pack tighter
            return min(viable, key=lambda h: (-colocated(h), h.available_cpu + h.available_ram))
        else:
            # No neighbours yet — pack into the host with least remaining
            return min(viable, key=lambda h: (h.available_cpu + h.available_ram))


def _build_adjacency(links: list[dict]) -> dict[str, set[str]]:
    """Build a node adjacency map from the expanded link list."""
    # Group links by network
    net_to_nodes: dict[str, set[str]] = defaultdict(set)
    for link in links:
        net_to_nodes[link["network"]].add(link["node"])

    # Build adjacency from shared networks
    adjacency: dict[str, set[str]] = defaultdict(set)
    for net, nodes in net_to_nodes.items():
        node_list = list(nodes)
        for i, a in enumerate(node_list):
            for b in node_list[i + 1:]:
                adjacency[a].add(b)
                adjacency[b].add(a)

    return adjacency


def _find_host_by_name(hosts: list[HostInfo], name: str) -> HostInfo | None:
    for h in hosts:
        if h.name == name:
            return h
    return None


def _find_cross_host_links(links: list[dict], plan: PlacementPlan) -> list[dict]:
    """Identify links where the two endpoints are on different hosts."""
    # Group by network
    net_to_nodes: dict[str, set[str]] = defaultdict(set)
    for link in links:
        net_to_nodes[link["network"]].add(link["node"])

    cross_host = []
    for net_name, nodes in net_to_nodes.items():
        hosts_involved = set()
        for node in nodes:
            h = plan.host_for_node(node)
            if h:
                hosts_involved.add(h)
        if len(hosts_involved) > 1:
            cross_host.append({
                "network": net_name,
                "nodes": list(nodes),
                "hosts": list(hosts_involved),
            })

    return cross_host
