"""Cross-host interconnect manager.

When a topology spans multiple EVE-NG servers, nodes on different hosts
need L2 connectivity between them. This module manages that by:

  1. Creating GRE or VXLAN tunnels between the EVE-NG hosts
  2. Bridging those tunnels into ``pnet`` (cloud) networks on each host
  3. Connecting the relevant node interfaces to the pnet on each side

Architecture::

    Host A                           Host B
    ┌──────────────┐                ┌──────────────┐
    │  Node R1     │                │  Node R2     │
    │   Gi0/1 ─────┤                ├───── Gi0/0   │
    │              │                │              │
    │  pnet9 ──────┼── GRE tunnel ──┼────── pnet9  │
    └──────────────┘                └──────────────┘

The tunnel endpoints are the management IPs of the EVE-NG hosts themselves.
Each cross-host link gets a unique tunnel (GRE key or VXLAN VNI) to isolate
traffic.

Requirements:
  - SSH access to EVE-NG hosts (for tunnel creation via shell commands)
  - The ``pnet`` interface must be pre-configured on each host or this module
    will create the necessary bridges and tunnel interfaces via SSH.
"""

import ipaddress
import logging
from dataclasses import dataclass, field
from typing import Optional

import paramiko

from .cluster import ClusterConfig, HostInfo

logger = logging.getLogger(__name__)


class InterconnectError(Exception):
    """Raised when tunnel setup fails."""


@dataclass
class TunnelEndpoint:
    """One side of a cross-host tunnel."""
    host: HostInfo
    bridge_name: str       # e.g. "pnet9"
    tunnel_iface: str      # e.g. "gre-eve2-101"
    local_ip: str          # host management IP
    remote_ip: str         # remote host management IP


@dataclass
class Tunnel:
    """A point-to-point tunnel connecting two hosts for a specific link."""
    tunnel_id: int         # GRE key or VXLAN VNI
    network_name: str      # topology network name this tunnel carries
    endpoint_a: TunnelEndpoint
    endpoint_b: TunnelEndpoint


class InterconnectManager:
    """Manages GRE/VXLAN tunnels between EVE-NG hosts.

    Usage::

        mgr = InterconnectManager(cluster_config)
        tunnels = mgr.setup_tunnels(cross_host_links, placement_plan)
        # ... later ...
        mgr.teardown_tunnels(tunnels)
    """

    def __init__(
        self,
        cluster_config: ClusterConfig,
        ssh_username: str = "root",
        ssh_password: Optional[str] = None,
        ssh_key_file: Optional[str] = None,
    ):
        self.config = cluster_config
        self.ssh_username = ssh_username
        self.ssh_password = ssh_password
        self.ssh_key_file = ssh_key_file
        self._tunnel_counter = 100  # starting GRE key / VNI
        self._ssh_clients: dict[str, paramiko.SSHClient] = {}

    def setup_tunnels(
        self,
        cross_host_links: list[dict],
        host_lookup: dict[str, HostInfo],
    ) -> list[Tunnel]:
        """Create tunnels for all cross-host links.

        Args:
            cross_host_links: From PlacementPlan.cross_host_links.
                Each entry: {"network": str, "nodes": [...], "hosts": [...]}
            host_lookup: Map of host name → HostInfo.

        Returns:
            List of Tunnel objects created.
        """
        tunnels = []
        bridge = self.config.tunnel_pnet  # e.g. "pnet9"

        for link_info in cross_host_links:
            net_name = link_info["network"]
            host_names = link_info["hosts"]

            if len(host_names) != 2:
                logger.warning(
                    "Network '%s' spans %d hosts — only 2-host tunnels supported, skipping",
                    net_name, len(host_names),
                )
                continue

            host_a = host_lookup[host_names[0]]
            host_b = host_lookup[host_names[1]]

            tunnel = self._create_tunnel(host_a, host_b, net_name, bridge)
            tunnels.append(tunnel)

        return tunnels

    def teardown_tunnels(self, tunnels: list[Tunnel]) -> None:
        """Remove all tunnels."""
        for tunnel in tunnels:
            self._destroy_tunnel(tunnel)
        self._close_ssh_connections()

    def teardown_all_tunnels_on_hosts(self, hosts: list[HostInfo]) -> None:
        """Remove all eve-ng-automator tunnels from the specified hosts.

        Useful for cleanup — finds and removes tunnel interfaces matching
        our naming pattern.
        """
        for host in hosts:
            try:
                ssh = self._get_ssh(host)
                # Find our tunnel interfaces by naming pattern
                _, stdout, _ = ssh.exec_command(
                    "ip -o link show | grep 'eveng-tun-' | awk -F': ' '{print $2}'"
                )
                ifaces = stdout.read().decode().strip().split("\n")
                for iface in ifaces:
                    iface = iface.strip()
                    if iface:
                        logger.info("Removing tunnel interface %s on %s", iface, host.name)
                        ssh.exec_command(f"ip link delete {iface}")
            except Exception as exc:
                logger.warning("Failed to clean tunnels on %s: %s", host.name, exc)

        self._close_ssh_connections()

    def _create_tunnel(
        self,
        host_a: HostInfo,
        host_b: HostInfo,
        network_name: str,
        bridge: str,
    ) -> Tunnel:
        """Create a single tunnel between two hosts."""
        tunnel_id = self._tunnel_counter
        self._tunnel_counter += 1

        mode = self.config.tunnel_mode
        # Interface names must be short (max 15 chars for Linux)
        iface_a = f"eveng-tun-{tunnel_id}"
        iface_b = f"eveng-tun-{tunnel_id}"

        tunnel = Tunnel(
            tunnel_id=tunnel_id,
            network_name=network_name,
            endpoint_a=TunnelEndpoint(
                host=host_a,
                bridge_name=bridge,
                tunnel_iface=iface_a,
                local_ip=host_a.host,
                remote_ip=host_b.host,
            ),
            endpoint_b=TunnelEndpoint(
                host=host_b,
                bridge_name=bridge,
                tunnel_iface=iface_b,
                local_ip=host_b.host,
                remote_ip=host_a.host,
            ),
        )

        logger.info(
            "Creating %s tunnel #%d for network '%s': %s ↔ %s",
            mode, tunnel_id, network_name, host_a.name, host_b.name,
        )

        # Create tunnel on both sides
        for ep in (tunnel.endpoint_a, tunnel.endpoint_b):
            self._setup_tunnel_endpoint(ep, mode, tunnel_id)

        return tunnel

    def _setup_tunnel_endpoint(
        self,
        ep: TunnelEndpoint,
        mode: str,
        tunnel_id: int,
    ) -> None:
        """SSH into a host and create the tunnel interface + bridge membership."""
        ssh = self._get_ssh(ep.host)

        if mode == "vxlan":
            create_cmd = (
                f"ip link add {ep.tunnel_iface} type vxlan "
                f"id {tunnel_id} "
                f"local {ep.local_ip} "
                f"remote {ep.remote_ip} "
                f"dstport 4789"
            )
        else:  # GRE (default)
            create_cmd = (
                f"ip link add {ep.tunnel_iface} type gretap "
                f"local {ep.local_ip} "
                f"remote {ep.remote_ip} "
                f"key {tunnel_id}"
            )

        # Determine the bridge name that corresponds to the pnet
        # EVE-NG maps pnet0 → pnet0, pnet1 → pnet1, etc.
        bridge_name = ep.bridge_name

        commands = [
            # Create tunnel interface
            create_cmd,
            # Bring it up
            f"ip link set {ep.tunnel_iface} up",
            # Ensure the bridge exists
            f"brctl addbr {bridge_name} 2>/dev/null || true",
            f"ip link set {bridge_name} up",
            # Add tunnel to the bridge
            f"brctl addif {bridge_name} {ep.tunnel_iface} 2>/dev/null || true",
        ]

        for cmd in commands:
            logger.debug("SSH %s: %s", ep.host.name, cmd)
            _, stdout, stderr = ssh.exec_command(cmd)
            exit_code = stdout.channel.recv_exit_status()
            if exit_code != 0:
                err = stderr.read().decode().strip()
                # "already exists" is OK — idempotent
                if "exists" not in err.lower():
                    logger.warning(
                        "Command failed on %s (exit %d): %s → %s",
                        ep.host.name, exit_code, cmd, err,
                    )

    def _destroy_tunnel(self, tunnel: Tunnel) -> None:
        """Remove a tunnel from both hosts."""
        for ep in (tunnel.endpoint_a, tunnel.endpoint_b):
            try:
                ssh = self._get_ssh(ep.host)
                cmd = f"ip link delete {ep.tunnel_iface}"
                logger.debug("SSH %s: %s", ep.host.name, cmd)
                ssh.exec_command(cmd)
            except Exception as exc:
                logger.warning(
                    "Failed to remove tunnel %s on %s: %s",
                    ep.tunnel_iface, ep.host.name, exc,
                )

    def _get_ssh(self, host: HostInfo) -> paramiko.SSHClient:
        """Get or create an SSH connection to a host."""
        if host.name not in self._ssh_clients:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            connect_kwargs = {
                "hostname": host.host,
                "username": self.ssh_username,
                "timeout": 30,
            }
            if self.ssh_key_file:
                connect_kwargs["key_filename"] = self.ssh_key_file
            elif self.ssh_password:
                connect_kwargs["password"] = self.ssh_password
            else:
                # Fall back to host password (EVE-NG root often matches)
                connect_kwargs["password"] = host.password

            logger.info("Opening SSH connection to %s (%s)", host.name, host.host)
            ssh.connect(**connect_kwargs)
            self._ssh_clients[host.name] = ssh

        return self._ssh_clients[host.name]

    def _close_ssh_connections(self) -> None:
        for name, ssh in self._ssh_clients.items():
            try:
                ssh.close()
            except Exception:
                pass
        self._ssh_clients.clear()
