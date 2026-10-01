import json

import pytest
import yaml

from clabfleet import cli, execute
from clabfleet.cluster import ClusterConfig, HostInfo
from clabfleet.execute import LabExecutor, docker_exec_argv, resolve_mode, select_nodes
from clabfleet.runner import CommandResult, Runner, SSHRunner
from clabfleet.topology import topology_from_dict

TOPO = {
    "name": "lab",
    "topology": {
        "nodes": {
            "spine1": {"kind": "arista_ceos", "image": "ceos"},
            "leaf1": {"kind": "arista_ceos", "image": "ceos"},
            "r1": {"kind": "cisco_iol", "image": "iol"},
            "srv": {"kind": "linux", "image": "alpine"},
            "br": {"kind": "bridge"},
            "down": {"kind": "linux", "image": "alpine"},
        },
    },
}


def _container(node, kind, host_ip="172.20.20.2"):
    return {
        "Names": [f"clab-lab-{node}"],
        "Labels": {"containerlab": "lab", "clab-node-name": node, "clab-node-kind": kind},
        "State": "running", "NetworkSettings": {"IPv4addr": host_ip},
    }


class FakeHost(Runner):
    """A lab host: answers `containerlab inspect` and `docker exec`."""

    def __init__(self, containers, docker_needs_sudo=False):
        super().__init__()
        self.containers = containers
        self.docker_needs_sudo = docker_needs_sudo
        self.calls = []

    def run(self, args, cwd=None, check=True, sudo=None, on_output=None):
        self.calls.append((list(args), bool(sudo)))
        if args[0] == "containerlab":
            return CommandResult(0, json.dumps({"lab": self.containers}), "")
        assert args[:2] == ["docker", "exec"]
        if self.docker_needs_sudo and not sudo:
            return CommandResult(1, "", "permission denied while trying to connect to the "
                                        "Docker daemon socket at unix:///var/run/docker.sock")
        container, rest = args[2], args[3:]
        if rest[-1] == "false":
            return CommandResult(3, "", "boom\n")
        return CommandResult(0, f"{container}: {' '.join(rest)}\n", "")


@pytest.fixture
def topo_file(tmp_path):
    path = tmp_path / "lab.clab.yml"
    path.write_text(yaml.safe_dump(TOPO, sort_keys=False))
    return path


def _patch_hosts(monkeypatch, hosts):
    monkeypatch.setattr(execute, "create_runner", lambda host: hosts[host.name])
    monkeypatch.setattr(LabExecutor, "_ssh_exec",
                        lambda self, runner, kind, ip, cmd: (0, f"ssh {ip}: {cmd}\n"))


def test_resolve_mode_per_kind():
    assert resolve_mode("auto", "arista_ceos") == "cli"
    assert resolve_mode("auto", "cisco_iol") == "ssh"
    assert resolve_mode("auto", "linux") == "shell"
    assert resolve_mode("shell", "arista_ceos") == "shell"
    with pytest.raises(ValueError, match="use --mode ssh"):
        resolve_mode("cli", "cisco_iol")
    with pytest.raises(ValueError, match="use --mode shell"):
        resolve_mode("cli", "linux")
    with pytest.raises(ValueError, match="no CLI or shell"):
        resolve_mode("auto", "bridge")


def test_docker_exec_argv():
    assert docker_exec_argv("cli", "arista_ceos", "c", "show ver") == [
        "docker", "exec", "c", "Cli", "-p", "15", "-c", "show ver"]
    assert docker_exec_argv("cli", "nokia_srlinux", "c", "show version") == [
        "docker", "exec", "c", "sr_cli", "show version"]
    assert docker_exec_argv("shell", "linux", "c", "ip -br a") == [
        "docker", "exec", "c", "sh", "-c", "ip -br a"]


def test_select_nodes_globs():
    topo = topology_from_dict(TOPO)
    assert select_nodes(topo, None) == list(TOPO["topology"]["nodes"])
    assert select_nodes(topo, ["leaf*", "spine1", "leaf1"]) == ["spine1", "leaf1"]
    with pytest.raises(ValueError, match=r"matches 'core\*'"):
        select_nodes(topo, ["core*"])


def test_exec_runs_each_node_the_right_way(topo_file, monkeypatch):
    host = FakeHost([_container("spine1", "arista_ceos"), _container("leaf1", "arista_ceos"),
                     _container("r1", "cisco_iol", "172.20.20.9"),
                     _container("srv", "linux"), _container("br", "bridge")])
    _patch_hosts(monkeypatch, {"localhost": host})
    out = LabExecutor(ClusterConfig(hosts=[HostInfo("localhost")])).run(topo_file, "show ver")

    results = {r["node"]: r for r in out["results"]}
    assert list(results) == ["spine1", "leaf1", "r1", "srv", "down"]  # bridge skipped
    assert results["spine1"]["mode"] == "cli"
    assert results["spine1"]["output"] == "clab-lab-spine1: Cli -p 15 -c show ver\n"
    assert results["r1"]["mode"] == "ssh"
    assert results["r1"]["output"] == "ssh 172.20.20.9: show ver\n"
    assert results["srv"]["output"] == "clab-lab-srv: sh -c show ver\n"
    assert results["down"]["error"] == "not running"
    assert all(results[n]["exit_code"] == 0 for n in ("spine1", "leaf1", "r1", "srv"))
    assert out["host_errors"] == {}


def test_exec_explicit_node_selection_reports_unusable_nodes(topo_file, monkeypatch):
    host = FakeHost([_container("br", "bridge"), _container("srv", "linux")])
    _patch_hosts(monkeypatch, {"localhost": host})
    out = LabExecutor(ClusterConfig(hosts=[HostInfo("localhost")])).run(
        topo_file, "false", nodes=["br", "srv"], mode="auto")
    results = {r["node"]: r for r in out["results"]}
    assert results["br"]["error"] == "kind 'bridge' has no CLI or shell"
    assert results["srv"]["exit_code"] == 3
    assert results["srv"]["output"] == "boom\n"


def test_exec_retries_docker_with_sudo(topo_file, monkeypatch):
    host = FakeHost([_container("srv", "linux")], docker_needs_sudo=True)
    _patch_hosts(monkeypatch, {"localhost": host})
    out = LabExecutor(ClusterConfig(hosts=[HostInfo("localhost", sudo=True)])).run(
        topo_file, "id", nodes=["srv"])
    assert out["results"][0]["exit_code"] == 0
    assert [sudo for args, sudo in host.calls if args[0] == "docker"] == [False, True]


def test_exec_multi_host_uses_placement_record(topo_file, monkeypatch):
    h1 = FakeHost([_container("spine1", "arista_ceos")])
    h2 = FakeHost([_container("srv", "linux")])
    h3 = FakeHost([])
    _patch_hosts(monkeypatch, {"h1": h1, "h2": h2, "h3": h3})
    (topo_file.parent / "lab.placement.json").write_text(json.dumps(
        {"lab": "lab", "hosts": {"h1": {}, "h2": {}}, "nodes": {}}))
    cluster = ClusterConfig(hosts=[HostInfo("h1", "10.0.0.1"), HostInfo("h2", "10.0.0.2"),
                                   HostInfo("h3", "10.0.0.3")])
    out = LabExecutor(cluster).run(topo_file, "x", nodes=["spine1", "srv"])
    assert {r["node"]: r["host"] for r in out["results"]} == {"spine1": "h1", "srv": "h2"}
    assert h3.calls == []


def test_exec_reports_unreachable_hosts(topo_file, monkeypatch):
    class Down(FakeHost):
        def run(self, *a, **kw):
            raise OSError("no route")

    _patch_hosts(monkeypatch, {"h1": Down([]), "h2": FakeHost([_container("srv", "linux")])})
    cluster = ClusterConfig(hosts=[HostInfo("h1", "10.0.0.1"), HostInfo("h2", "10.0.0.2")])
    out = LabExecutor(cluster).run(topo_file, "x", nodes=["srv", "spine1"])
    assert out["host_errors"] == {"h1": "unreachable: no route"}
    assert [r["error"] for r in out["results"]] == ["not running", ""]


class FakeSSHClient:
    instances = []

    def __init__(self):
        self.connected = None
        FakeSSHClient.instances.append(self)

    def set_missing_host_key_policy(self, policy):
        pass

    def connect(self, host, **kw):
        self.connected = (host, kw)

    def exec_command(self, command, timeout=None):
        class Chan:
            def recv_exit_status(self):
                return -1

        class Stream:
            def __init__(self, data):
                self.data, self.channel = data, Chan()

            def read(self):
                return self.data

        return None, Stream(f"out of {command}".encode()), Stream(b"")

    def close(self):
        pass


def test_ssh_exec_tunnels_through_remote_host(monkeypatch):
    paramiko = pytest.importorskip("paramiko")
    monkeypatch.setattr(paramiko, "SSHClient", FakeSSHClient)
    opened = []

    class Transport:
        def open_channel(self, kind, dest, src, timeout=None):
            opened.append((kind, dest))
            return "tunnel"

    class Client:
        def get_transport(self):
            return Transport()

    runner = SSHRunner("10.0.0.1")
    monkeypatch.setattr(runner, "client", lambda: Client())
    ex = LabExecutor(ClusterConfig(hosts=[HostInfo("h1", "10.0.0.1")]), ssh_password="pw")
    assert ex._ssh_exec(runner, "cisco_iol", "172.20.20.3", "show ip int br") == (
        0, "out of show ip int br")  # missing exit status counts as success
    assert opened == [("direct-tcpip", ("172.20.20.3", 22))]
    host, kw = FakeSSHClient.instances[-1].connected
    assert host == "172.20.20.3"
    assert kw["sock"] == "tunnel" and kw["username"] == "admin" and kw["password"] == "pw"

    # Local host: connect directly, kind-specific default user
    ex._ssh_exec(Runner(), "juniper_crpd", "172.20.20.4", "show version")
    host, kw = FakeSSHClient.instances[-1].connected
    assert kw["sock"] is None and kw["username"] == "root"


def test_cli_exec_output_and_exit_code(topo_file, monkeypatch, capsys):
    host = FakeHost([_container("spine1", "arista_ceos"), _container("srv", "linux")])
    _patch_hosts(monkeypatch, {"localhost": host})

    assert cli.main(["exec", str(topo_file), "show", "version", "--nodes", "spine1"]) == 0
    out = capsys.readouterr().out
    assert "=== spine1 (arista_ceos, cli) ===" in out
    assert "Cli -p 15 -c show version" in out

    assert cli.main(["exec", str(topo_file), "--nodes", "srv,spine*", "--", "false"]) == 1
    captured = capsys.readouterr()
    assert "[exit 3]" in captured.out
    assert "2 of 2 nodes failed" in captured.err

    assert cli.main(["exec", str(topo_file), "--json", "--nodes", "srv", "uptime"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["results"][0]["output"] == "clab-lab-srv: sh -c uptime\n"

    assert cli.main(["exec", str(topo_file), "x", "--nodes", "nope*"]) == 1
    assert "No node of lab 'lab' matches 'nope*'" in capsys.readouterr().err
