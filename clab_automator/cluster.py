"""Multi-host cluster inventory for containerlab.

Each host is a Linux server with Docker and containerlab installed. The
automator reaches remote hosts over SSH, copies each host its share of the
topology, and runs ``containerlab deploy`` there. Links between nodes on
different hosts become VXLAN links (``vxlan-stitch`` by default) that
containerlab creates and removes itself.

Cluster inventory YAML format::

    cluster:
      link_type: "vxlan-stitch"   # vxlan-stitch | vxlan
      vni_base: 1000              # first VNI for cross-host links
      dst_port: 14789             # VXLAN UDP port (containerlab default)
      mtu: 1450                   # optional — MTU for cross-host links

    hosts:
      - name: "clab-1"
        host: "192.168.1.101"     # SSH address ("localhost" = run locally)
        ssh_user: "netops"
        ssh_key: "~/.ssh/id_ed25519"
        sudo: true                # run containerlab with sudo -n
        vtep_ip: "10.0.0.1"       # VXLAN source address (default: host)
        max_cpu: 16               # vCPUs available for labs (default: nproc)
        max_ram: 65536            # MB available for labs (default: MemAvailable)
        tags: ["core"]            # optional — for affinity placement
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .runner import LocalRunner, Runner, SSHRunner

logger = logging.getLogger(__name__)

LOCAL_HOSTNAMES = {"localhost", "local", "127.0.0.1", "::1"}
LINK_TYPES = {"vxlan-stitch", "vxlan"}
DEFAULT_WORKDIR = "clab-automator"


class ClusterConfigError(Exception):
    """Raised when cluster configuration is invalid."""


@dataclass
class HostInfo:
    """One containerlab server in the cluster."""
    name: str
    host: str = "localhost"
    ssh_user: Optional[str] = None
    ssh_port: int = 22
    ssh_key: Optional[str] = None
    ssh_password: Optional[str] = None
    sudo: bool = False
    vtep_ip: Optional[str] = None
    workdir: str = DEFAULT_WORKDIR  # relative paths are under the user's home
    max_cpu: float = 0     # 0 = probe the host
    max_ram: int = 0       # 0 = probe the host (MB)
    tags: list[str] = field(default_factory=list)

    # Filled at runtime by placement
    used_cpu: float = 0
    used_ram: int = 0

    @property
    def is_local(self) -> bool:
        return self.host in LOCAL_HOSTNAMES

    @property
    def vtep(self) -> Optional[str]:
        """Address other hosts use as the VXLAN remote for this host."""
        if self.vtep_ip:
            return self.vtep_ip
        return None if self.is_local else self.host

    @property
    def available_cpu(self) -> float:
        if self.max_cpu <= 0:
            return 999999
        return max(0, self.max_cpu - self.used_cpu)

    @property
    def available_ram(self) -> int:
        if self.max_ram <= 0:
            return 999999
        return max(0, self.max_ram - self.used_ram)

    def can_fit(self, cpu: float, ram: int) -> bool:
        return self.available_cpu >= cpu and self.available_ram >= ram

    def reserve(self, cpu: float, ram: int) -> None:
        self.used_cpu += cpu
        self.used_ram += ram

    def lab_dir(self, lab_name: str) -> str:
        """Directory on the host that holds this lab's topology and files."""
        base = self.workdir
        if self.is_local:
            base_path = Path(base).expanduser()
            if not base_path.is_absolute():
                base_path = Path.home() / base_path
            return str(base_path / lab_name)
        return f"{base.rstrip('/')}/{lab_name}"


@dataclass
class ClusterConfig:
    """Parsed cluster inventory."""
    hosts: list[HostInfo]
    link_type: str = "vxlan-stitch"
    vni_base: int = 1000
    dst_port: int = 14789
    mtu: Optional[int] = None


def load_cluster_config(file_path: str | Path) -> ClusterConfig:
    """Load a cluster inventory YAML."""
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"Cluster config not found: {file_path}")

    with open(file_path) as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ClusterConfigError("Cluster config must be a YAML mapping")

    cluster_section = raw.get("cluster") or {}
    hosts_section = raw.get("hosts") or []
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
            ssh_user=h.get("ssh_user"),
            ssh_port=h.get("ssh_port", 22),
            ssh_key=h.get("ssh_key"),
            ssh_password=h.get("ssh_password"),
            sudo=h.get("sudo", False),
            vtep_ip=h.get("vtep_ip"),
            workdir=h.get("workdir", DEFAULT_WORKDIR),
            max_cpu=h.get("max_cpu", 0),
            max_ram=h.get("max_ram", 0),
            tags=h.get("tags", []),
        ))

    link_type = cluster_section.get("link_type", "vxlan-stitch")
    if link_type not in LINK_TYPES:
        raise ClusterConfigError(
            f"Invalid link_type '{link_type}' — must be one of {sorted(LINK_TYPES)}"
        )

    return ClusterConfig(
        hosts=hosts,
        link_type=link_type,
        vni_base=cluster_section.get("vni_base", 1000),
        dst_port=cluster_section.get("dst_port", 14789),
        mtu=cluster_section.get("mtu"),
    )


def create_runner(host_info: HostInfo) -> Runner:
    """Create a command runner for a host."""
    if host_info.is_local:
        runner = LocalRunner(sudo=host_info.sudo)
        runner.name = host_info.name
        return runner
    return SSHRunner(
        host=host_info.host,
        username=host_info.ssh_user,
        port=host_info.ssh_port,
        key_file=host_info.ssh_key,
        password=host_info.ssh_password,
        sudo=host_info.sudo,
        name=host_info.name,
    )


def probe_host_resources(runner: Runner, host_info: HostInfo) -> dict:
    """Fill in max_cpu/max_ram from the host when not set in the inventory.

    Returns the raw facts (cpus, mem_total_mb, mem_available_mb, containerlab
    version) for status display.
    """
    facts: dict = {}
    try:
        facts["cpus"] = int(runner.run(["nproc"], sudo=False).stdout.strip())
        meminfo = runner.run(["cat", "/proc/meminfo"], sudo=False).stdout
        for line in meminfo.splitlines():
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                facts[f"{key}_mb"] = int(rest.split()[0]) // 1024
    except Exception as exc:
        logger.warning("Could not probe resources on %s: %s", host_info.name, exc)
        return facts

    if host_info.max_cpu <= 0 and facts.get("cpus"):
        host_info.max_cpu = facts["cpus"]
    if host_info.max_ram <= 0 and facts.get("MemAvailable_mb"):
        host_info.max_ram = facts["MemAvailable_mb"]

    logger.info(
        "Host %s: CPU available=%s, RAM available=%sMB",
        host_info.name, host_info.available_cpu, host_info.available_ram,
    )
    return facts


def containerlab_version(runner: Runner) -> Optional[str]:
    """Return the containerlab version on a host, or None if not installed."""
    result = runner.run(["containerlab", "version"], check=False, sudo=False)
    if result.exit_code != 0:
        return None
    for line in result.stdout.splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() == "version":
            return value.strip()
    return result.stdout.strip().splitlines()[0] if result.stdout.strip() else "unknown"
