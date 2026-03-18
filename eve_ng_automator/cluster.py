"""Multi-host cluster management for EVE-NG.

Manages a pool of EVE-NG servers and provides resource-aware placement
of nodes across them. Each host runs its own independent EVE-NG instance;
cross-host links are stitched together with GRE or VXLAN tunnels through
cloud (pnet) networks.

Cluster inventory YAML format::

    cluster:
      tunnel_mode: "gre"          # gre | vxlan
      tunnel_pnet: "pnet9"        # which cloud interface to use for tunnels
      tunnel_subnet: "172.16.255.0/24"  # point-to-point tunnel IPs

    hosts:
      - name: "eve-1"
        host: "192.168.1.101"
        username: "admin"
        password: "eve"
        port: 443
        ssl: true
        max_cpu: 16               # total vCPUs available for labs
        max_ram: 65536            # total RAM (MB) available for labs
        tags: ["core"]            # optional — for affinity placement

      - name: "eve-2"
        host: "192.168.1.102"
        username: "admin"
        password: "eve"
        max_cpu: 16
        max_ram: 65536
        tags: ["access"]

      - name: "eve-3"
        host: "192.168.1.103"
        username: "admin"
        password: "eve"
        max_cpu: 8
        max_ram: 32768
        tags: ["access"]
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .api_client import EveNgClient

logger = logging.getLogger(__name__)


class ClusterConfigError(Exception):
    """Raised when cluster configuration is invalid."""


@dataclass
class HostInfo:
    """Represents one EVE-NG server in the cluster."""
    name: str
    host: str
    username: str = "admin"
    password: str = "eve"
    port: int = 443
    ssl: bool = True
    verify_ssl: bool = False
    max_cpu: int = 0       # 0 = unlimited / unknown
    max_ram: int = 0       # 0 = unlimited / unknown (MB)
    tags: list[str] = field(default_factory=list)

    # Filled at runtime after querying the server
    used_cpu: int = 0
    used_ram: int = 0

    @property
    def available_cpu(self) -> int:
        if self.max_cpu <= 0:
            return 999999
        return max(0, self.max_cpu - self.used_cpu)

    @property
    def available_ram(self) -> int:
        if self.max_ram <= 0:
            return 999999
        return max(0, self.max_ram - self.used_ram)

    def can_fit(self, cpu: int, ram: int) -> bool:
        return self.available_cpu >= cpu and self.available_ram >= ram

    def reserve(self, cpu: int, ram: int) -> None:
        self.used_cpu += cpu
        self.used_ram += ram


@dataclass
class ClusterConfig:
    """Parsed cluster inventory."""
    hosts: list[HostInfo]
    tunnel_mode: str = "gre"        # gre | vxlan
    tunnel_pnet: str = "pnet9"      # cloud network for tunnel traffic
    tunnel_subnet: str = "172.16.255.0/24"


def load_cluster_config(file_path: str | Path) -> ClusterConfig:
    """Load a cluster inventory YAML."""
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"Cluster config not found: {file_path}")

    with open(file_path) as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ClusterConfigError("Cluster config must be a YAML mapping")

    cluster_section = raw.get("cluster", {})
    hosts_section = raw.get("hosts", [])

    if not hosts_section:
        raise ClusterConfigError("Cluster config must have at least one host")

    hosts = []
    seen_names = set()
    for i, h in enumerate(hosts_section):
        if "host" not in h:
            raise ClusterConfigError(f"Host #{i} missing 'host' field")
        name = h.get("name", h["host"])
        if name in seen_names:
            raise ClusterConfigError(f"Duplicate host name: {name}")
        seen_names.add(name)

        hosts.append(HostInfo(
            name=name,
            host=h["host"],
            username=h.get("username", "admin"),
            password=h.get("password", "eve"),
            port=h.get("port", 443),
            ssl=h.get("ssl", True),
            verify_ssl=h.get("verify_ssl", False),
            max_cpu=h.get("max_cpu", 0),
            max_ram=h.get("max_ram", 0),
            tags=h.get("tags", []),
        ))

    return ClusterConfig(
        hosts=hosts,
        tunnel_mode=cluster_section.get("tunnel_mode", "gre"),
        tunnel_pnet=cluster_section.get("tunnel_pnet", "pnet9"),
        tunnel_subnet=cluster_section.get("tunnel_subnet", "172.16.255.0/24"),
    )


def create_client(host_info: HostInfo) -> EveNgClient:
    """Create an EveNgClient for a specific host."""
    return EveNgClient(
        host=host_info.host,
        username=host_info.username,
        password=host_info.password,
        port=host_info.port,
        ssl=host_info.ssl,
        verify_ssl=host_info.verify_ssl,
    )


def probe_host_resources(client: EveNgClient, host_info: HostInfo) -> None:
    """Query a host's current resource usage and update HostInfo in-place.

    Uses the EVE-NG /api/status endpoint which returns CPU and memory info.
    """
    try:
        status = client.status()
        # EVE-NG status returns cpu/mem as percentages or absolute values
        # depending on version. We do best-effort parsing.
        if isinstance(status, dict):
            # Try to get actual CPU count from status
            cpu_used = status.get("cpu", 0)
            mem_info = status.get("mem", {})
            if isinstance(mem_info, dict):
                # mem may have "used" and "total" in KB or %
                used_mb = mem_info.get("used", 0)
                total_mb = mem_info.get("total", 0)
                if total_mb and host_info.max_ram <= 0:
                    host_info.max_ram = int(total_mb)
                if used_mb:
                    host_info.used_ram = int(used_mb)

            logger.info(
                "Host %s: CPU available=%d, RAM available=%dMB",
                host_info.name, host_info.available_cpu, host_info.available_ram,
            )
    except Exception as exc:
        logger.warning("Could not probe resources on %s: %s", host_info.name, exc)
