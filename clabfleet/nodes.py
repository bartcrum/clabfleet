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

LOG_TAIL_LINES = 2000  # earlier container log lines are skipped in the Logs tab

SHELL_CMD = ["sh", "-c", "command -v bash >/dev/null 2>&1 && exec bash -l || exec sh -l"]

# Killing the local `docker exec` client leaves the shell it started running
# in the container. So CLI and shell tabs pass a random tag in this variable,
# which every process they start inherits, and closing the tab runs
# TERMINAL_STOP in the container: it hangs up (then kills) the tagged
# processes and the sessions they lead -- never anything untagged.
TERMINAL_TAG_ENV = "CLABFLEET_TERMINAL"
TERMINAL_STOP = (
    f't="{TERMINAL_TAG_ENV}=$1"; '
    # sid PID: the session of a process (field 4 after "comm) " in /proc/PID/stat)
    'sid() { read -r st 2>/dev/null <"/proc/$1/stat" || return 1; set -- ${st##*) }; echo "$4"; }; '
    'for i in 1 2 3 4 5 6 7 8 9 10; do '
    'tagged=""; leaders=" "; '
    'for d in /proc/[0-9]*; do p=${d#/proc/}; '
    'case "$(tr "\\000" "\\n" 2>/dev/null <"$d/environ")" in *"$t"*) '
    'tagged="$tagged $p"; [ "$(sid "$p")" = "$p" ] && [ "$p" != 1 ] && leaders="$leaders$p "; '
    'esac; done; '
    'pids=$tagged; '
    'if [ "$leaders" != " " ]; then for d in /proc/[0-9]*; do p=${d#/proc/}; '
    'case "$leaders" in *" $(sid "$p") "*) pids="$pids $p" ;; esac; done; fi; '
    '[ -n "$pids" ] || exit 0; '
    'if [ "$i" -le 5 ]; then kill -HUP $pids 2>/dev/null; else kill -KILL $pids 2>/dev/null; fi; '
    'sleep 0.2; '
    'done; exit 0'
)


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


def terminal_command(mode: str, kind: str, container: str, ipv4: str,
                     tag: str = "") -> list[str]:
    """argv to run (on the node's host) for a terminal tab: cli, shell, ssh or logs.

    ``tag`` marks the processes of a CLI or shell tab for ``terminal_stop_command``.
    """
    exec_it = ["docker", "exec", "-it"]
    if tag:
        exec_it += ["-e", f"{TERMINAL_TAG_ENV}={tag}"]
    if mode == "cli":
        if kind not in KIND_CLI:
            raise ValueError(f"No CLI command known for kind '{kind}'")
        return [*exec_it, container, *KIND_CLI[kind]]
    if mode == "shell":
        return [*exec_it, container, *SHELL_CMD]
    if mode == "ssh":
        if not ipv4:
            raise ValueError("Node has no management IPv4 address")
        user = KIND_SSH_USER.get(kind, "admin")
        return [
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
            f"{user}@{ipv4}",
        ]
    if mode == "logs":
        # Works for stopped containers too, which is when logs matter most
        return ["docker", "logs", "--follow", "--tail", str(LOG_TAIL_LINES), container]
    raise ValueError(f"Unknown terminal mode '{mode}'")


def terminal_stop_command(container: str, tag: str) -> list[str]:
    """argv (on the node's host) that ends what a tagged terminal tab started."""
    return ["docker", "exec", container, "sh", "-c", TERMINAL_STOP, "clabfleet-stop", tag]


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
