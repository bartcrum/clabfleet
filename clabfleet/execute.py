"""Run a command on the nodes of a deployed lab (``clabfleet exec``).

Each node is reached the best way its kind allows:

- ``cli``: the node's own CLI, non-interactively, via ``docker exec``
  (cEOS ``Cli -c``, SR Linux ``sr_cli``, cRPD ``cli -c``)
- ``ssh``: SSH to the node's management address, for VM-based kinds such
  as Cisco IOL whose CLI is only reachable that way. On a remote lab host
  the connection is tunnelled through the host's SSH session, since
  management addresses are only reachable from the host itself.
- ``shell``: ``sh -c`` inside the container via ``docker exec``
"""

import fnmatch
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from .cluster import ClusterConfig, HostInfo, create_runner
from .deployer import hosts_for_lab
from .nodes import (
    DEFAULT_SSH_PASSWORD,
    KIND_CLI_EXEC,
    KIND_SSH_USER,
    NO_SHELL_KINDS,
    SSH_CLI_KINDS,
    InspectError,
    inspect_all,
    parse_inspect,
    run_docker,
)
from .runner import Runner, SSHRunner
from .topology import Topology, load_topology

logger = logging.getLogger(__name__)

MODES = ("auto", "cli", "shell", "ssh")


@dataclass
class NodeResult:
    node: str
    host: str = ""
    kind: str = ""
    mode: str = ""
    exit_code: Optional[int] = None
    output: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.exit_code == 0


def resolve_mode(requested: str, kind: str) -> str:
    """Concrete mode for a node: ``auto`` picks the kind's best one."""
    if kind in NO_SHELL_KINDS:
        raise ValueError(f"kind '{kind}' has no CLI or shell")
    if requested == "auto":
        if kind in KIND_CLI_EXEC:
            return "cli"
        if kind in SSH_CLI_KINDS:
            return "ssh"
        return "shell"
    if requested == "cli" and kind not in KIND_CLI_EXEC:
        hint = "ssh" if kind in SSH_CLI_KINDS else "shell"
        raise ValueError(f"no CLI command known for kind '{kind}' (use --mode {hint})")
    if requested not in MODES:
        raise ValueError(f"unknown mode '{requested}'")
    return requested


def docker_exec_argv(mode: str, kind: str, container: str, command: str) -> list[str]:
    if mode == "cli":
        return ["docker", "exec", container, *KIND_CLI_EXEC[kind], command]
    if mode == "shell":
        return ["docker", "exec", container, "sh", "-c", command]
    raise ValueError(f"mode '{mode}' does not use docker exec")


def select_nodes(topo: Topology, patterns: Optional[list[str]]) -> list[str]:
    """Topology nodes matching any glob in ``patterns`` (all nodes if none)."""
    if not patterns:
        return list(topo.nodes)
    selected = []
    for pattern in patterns:
        matches = [n for n in topo.nodes if fnmatch.fnmatchcase(n, pattern)]
        if not matches:
            raise ValueError(f"No node of lab '{topo.name}' matches '{pattern}'")
        selected += [n for n in matches if n not in selected]
    return [n for n in topo.nodes if n in selected]  # topology order


class LabExecutor:
    """Run one command on many lab nodes, in parallel."""

    def __init__(
        self,
        cluster: ClusterConfig,
        ssh_user: Optional[str] = None,
        ssh_password: Optional[str] = None,
        parallel: int = 8,
        timeout: float = 60,
    ):
        self.cluster = cluster
        self.ssh_user = ssh_user
        self.ssh_password = (
            ssh_password or os.environ.get("CLAB_NODE_PASSWORD") or DEFAULT_SSH_PASSWORD
        )
        self.parallel = max(1, parallel)
        self.timeout = timeout
        self._runners: dict[str, Runner] = {}

    def run(
        self,
        topology_file: str | Path,
        command: str,
        nodes: Optional[list[str]] = None,
        mode: str = "auto",
    ) -> dict:
        """Returns {"lab", "command", "results": [NodeResult dicts], "host_errors"}."""
        if mode not in MODES:
            raise ValueError(f"Unknown mode '{mode}' (choose from {', '.join(MODES)})")
        topo = load_topology(topology_file)
        selected = select_nodes(topo, nodes)
        try:
            containers, host_errors = self._running_nodes(topo)
            jobs = []
            results: dict[str, NodeResult] = {}
            for name in selected:
                result = NodeResult(name)
                results[name] = result
                container = containers.get(name)
                result.kind = (container or {}).get("kind") or topo.effective_node(name)["kind"]
                if result.kind in NO_SHELL_KINDS and not nodes:
                    del results[name]  # bridges etc.: nothing to run on
                    continue
                if container is None:
                    result.error = "not running"
                    continue
                result.host = container["host"]
                try:
                    result.mode = resolve_mode(mode, result.kind)
                except ValueError as exc:
                    result.error = str(exc)
                    continue
                jobs.append((result, container))

            with ThreadPoolExecutor(max_workers=self.parallel) as pool:
                for _ in pool.map(lambda job: self._exec(job[0], job[1], command), jobs):
                    pass
        finally:
            self._close()

        return {
            "lab": topo.name,
            "command": command,
            "results": [asdict(r) for r in results.values()],
            "host_errors": host_errors,
        }

    # ------------------------------------------------------------------

    def _running_nodes(self, topo: Topology) -> tuple[dict[str, dict], dict[str, str]]:
        """node name → container info for the lab, plus per-host errors."""
        found: dict[str, dict] = {}
        errors: dict[str, str] = {}
        for host in hosts_for_lab(self.cluster, topo):
            try:
                data = inspect_all(self._runner(host))
            except InspectError as exc:
                errors[host.name] = str(exc)
                continue
            except Exception as exc:
                errors[host.name] = f"unreachable: {exc}"
                continue
            for c in parse_inspect(data, host.name):
                if c["lab"] == topo.name:
                    found[c["node"]] = c
        return found, errors

    def _exec(self, result: NodeResult, container: dict, command: str) -> None:
        host = self._host(result.host)
        runner = self._runner(host)
        try:
            if result.mode == "ssh":
                code, output = self._ssh_exec(runner, result.kind, container["ipv4"], command)
            else:
                argv = docker_exec_argv(result.mode, result.kind, container["container"], command)
                res = run_docker(runner, argv, host.sudo)
                code, output = res.exit_code, res.stdout + res.stderr
        except Exception as exc:  # noqa: BLE001 - one node failing must not stop the rest
            result.error = str(exc) or type(exc).__name__
            return
        result.exit_code, result.output = code, output

    def _ssh_exec(self, runner: Runner, kind: str, ipv4: str, command: str) -> tuple[int, str]:
        return ssh_exec(runner, kind, ipv4, command, user=self.ssh_user,
                        password=self.ssh_password, timeout=self.timeout)

    def _host(self, name: str) -> HostInfo:
        return next(h for h in self.cluster.hosts if h.name == name)

    def _runner(self, host: HostInfo) -> Runner:
        if host.name not in self._runners:
            self._runners[host.name] = create_runner(host)
        return self._runners[host.name]

    def _close(self) -> None:
        for runner in self._runners.values():
            runner.close()
        self._runners.clear()


def ssh_exec(runner: Runner, kind: str, ipv4: str, command: str, *,
             user: Optional[str] = None, password: Optional[str] = None,
             timeout: float = 60) -> tuple[int, str]:
    """Run ``command`` over SSH on a node's management address: (exit code, output).

    On a remote lab host the connection is tunnelled through the host's SSH
    session, since management addresses are only reachable from the host.
    """
    import paramiko

    if not ipv4:
        raise ValueError("node has no management IPv4 address")
    password = password or os.environ.get("CLAB_NODE_PASSWORD") or DEFAULT_SSH_PASSWORD
    sock = None
    if isinstance(runner, SSHRunner):
        transport = runner.client().get_transport()
        sock = transport.open_channel("direct-tcpip", (ipv4, 22), ("127.0.0.1", 0),
                                      timeout=timeout)
    client = paramiko.SSHClient()
    # Lab nodes get fresh host keys on every deploy; the terminal in the
    # GUI skips host key checks for the same reason
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            ipv4, username=user or KIND_SSH_USER.get(kind, "admin"),
            password=password, sock=sock, timeout=timeout,
            banner_timeout=timeout, auth_timeout=timeout,
            look_for_keys=False, allow_agent=False,
        )
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        output = stdout.read().decode(errors="replace") + stderr.read().decode(errors="replace")
        code = stdout.channel.recv_exit_status()
    finally:
        client.close()
    # Network OSes often close the channel without an exit status (-1)
    return (0 if code == -1 else code), output
