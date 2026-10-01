"""How to reach lab nodes: per-kind CLI commands, terminals and inspect output.

Shared by the CLI (``clabfleet exec``, ``clabfleet validate``) and the GUI
(terminals, node tables).
"""

import json

from .topology import (
    KIND_ALIASES,
    KIND_RESOURCE_ESTIMATES,
    LABEL_CPU,
    LABEL_RAM,
    kind_estimate,
)

INSPECT_ARGS = ["inspect", "--all", "--details", "--format", "json"]


class InspectError(Exception):
    """`containerlab inspect` failed on a host."""


DOCKER_DENIED = "permission denied while trying to connect to the docker"


def run_docker(runner, argv: list[str], host_sudo: bool):
    """Run a docker command without sudo, retrying with sudo if Docker refuses
    and the host is configured to use sudo."""
    res = runner.run(argv, check=False, sudo=False)
    if res.exit_code != 0 and host_sudo and DOCKER_DENIED in (res.stderr + res.stdout).lower():
        res = runner.run(argv, check=False, sudo=True)
    return res

# Command that gives a node's native CLI via `docker exec` (interactive)
KIND_CLI = {
    "arista_ceos": ["Cli"],
    "ceos": ["Cli"],
    "nokia_srlinux": ["sr_cli"],
    "srl": ["sr_cli"],
    "juniper_crpd": ["cli"],
    "crpd": ["cli"],
}
# Same CLIs run non-interactively: the command string is appended as one argument
KIND_CLI_EXEC = {
    "arista_ceos": ["Cli", "-p", "15", "-c"],
    "ceos": ["Cli", "-p", "15", "-c"],
    "nokia_srlinux": ["sr_cli"],
    "srl": ["sr_cli"],
    "juniper_crpd": ["cli", "-c"],
    "crpd": ["cli", "-c"],
}
# Kinds whose CLI is only reachable over SSH (VM-based / vrnetlab images)
SSH_CLI_KINDS = {
    "cisco_iol", "cisco_xrv9k", "cisco_csr1000v", "cisco_c8000v", "cisco_n9kv",
    "cisco_ftdv", "juniper_vjunosrouter", "juniper_vjunosswitch",
    "juniper_vjunosevolved", "juniper_vsrx", "nokia_sros", "vr-sros",
    "fortinet_fortigate", "paloalto_panos",
}
# Kinds with no shell worth opening
NO_SHELL_KINDS = {"bridge", "ovs-bridge", "host", "ext-container"}
# Default SSH usernames containerlab sets up per kind
KIND_SSH_USER = {"juniper_crpd": "root", "crpd": "root", "linux": "root"}
# containerlab's default login on most network kinds
DEFAULT_SSH_PASSWORD = "admin"

SHELL_CMD = ["sh", "-c", "command -v bash >/dev/null 2>&1 && exec bash -l || exec sh -l"]


def known_kinds() -> set[str]:
    """Every kind clabfleet has placement estimates or access rules for."""
    return (
        set(KIND_RESOURCE_ESTIMATES) | set(KIND_ALIASES) | set(KIND_CLI)
        | SSH_CLI_KINDS | NO_SHELL_KINDS
    )


def access_modes(kind: str) -> list[str]:
    """Terminal modes offered for a node kind, best first."""
    if kind in NO_SHELL_KINDS:
        return []
    if kind in KIND_CLI:
        return ["cli", "shell", "ssh"]
    if kind in SSH_CLI_KINDS:
        return ["ssh", "shell"]
    return ["shell", "ssh"]


def terminal_command(mode: str, kind: str, container: str, ipv4: str) -> list[str]:
    """argv to run (on the node's host) for an interactive terminal."""
    if mode == "cli":
        if kind not in KIND_CLI:
            raise ValueError(f"No CLI command known for kind '{kind}'")
        return ["docker", "exec", "-it", container, *KIND_CLI[kind]]
    if mode == "shell":
        return ["docker", "exec", "-it", container, *SHELL_CMD]
    if mode == "ssh":
        if not ipv4:
            raise ValueError("Node has no management IPv4 address")
        user = KIND_SSH_USER.get(kind, "admin")
        return [
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
            f"{user}@{ipv4}",
        ]
    raise ValueError(f"Unknown terminal mode '{mode}'")


def parse_inspect(data: dict, host_name: str) -> list[dict]:
    """Normalise `containerlab inspect --all --details --format json` output."""
    if not isinstance(data, dict):
        return []
    containers = []
    for lab_name, items in data.items():
        for c in items or []:
            labels = c.get("Labels") or {}
            net = c.get("NetworkSettings") or {}
            names = c.get("Names") or [c.get("name", "")]
            containers.append({
                "id": c.get("Id") or c.get("ID") or c.get("container_id", ""),
                "lab": labels.get("containerlab", lab_name),
                "node": labels.get("clab-node-name") or names[0],
                "container": names[0],
                "kind": labels.get("clab-node-kind") or c.get("kind", ""),
                "image": c.get("Image") or c.get("image", ""),
                "state": c.get("State") or c.get("state", ""),
                "status": c.get("Status") or c.get("status", ""),
                "ipv4": net.get("IPv4addr") or "",
                "topo_file": labels.get("clab-topo-file", ""),
                "host": host_name,
            })
    return containers


def inspect_all(runner) -> dict:
    """Raw `containerlab inspect --all --details` JSON for a host ({} if no labs)."""
    result = runner.containerlab(INSPECT_ARGS, check=False)
    if result.exit_code != 0:
        raise InspectError((result.stderr or result.stdout).strip()[-500:])
    out = result.stdout.strip()
    if not out:
        return {}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {}  # e.g. "no containers found" from older versions
    return data if isinstance(data, dict) else {}


def running_usage(data: dict, exclude_lab: str | None = None) -> dict[str, dict]:
    """Estimated resources of running lab containers, per lab.

    Uses a container's ``lab.cpu`` / ``lab.ram`` labels when it has them,
    else the per-kind estimate. Returns {lab: {"nodes", "cpu", "ram"}}.
    """
    usage: dict[str, dict] = {}
    for lab_name, items in (data or {}).items():
        for c in items or []:
            labels = c.get("Labels") or {}
            lab = labels.get("containerlab", lab_name)
            state = c.get("State") or c.get("state", "")
            if lab == exclude_lab or state != "running":
                continue
            kind = labels.get("clab-node-kind") or c.get("kind", "")
            cpu, ram = kind_estimate(kind)
            try:
                cpu = float(labels.get(LABEL_CPU, cpu))
                ram = int(labels.get(LABEL_RAM, ram))
            except (TypeError, ValueError):
                pass
            entry = usage.setdefault(lab, {"nodes": 0, "cpu": 0.0, "ram": 0})
            entry["nodes"] += 1
            entry["cpu"] += cpu
            entry["ram"] += ram
    return usage
