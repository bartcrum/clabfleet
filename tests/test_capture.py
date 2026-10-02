import os
import threading
import time

import pytest
import yaml

from clabfleet import capture as cap
from clabfleet import cli
from clabfleet.capture import (
    NODE_STOP,
    NODE_WRAPPER,
    Capture,
    CaptureError,
    CaptureSpec,
    capture_command,
    check_interface,
    clean_filter,
    node_interfaces,
    parse_target,
    stop_command,
    sudo_prefix,
    tcpdump_args,
)
from clabfleet.cluster import HostInfo
from clabfleet.runner import CommandResult, LocalRunner, Runner, SSHRunner
from clabfleet.topology import topology_from_dict

TOPO = {
    "name": "lab",
    "topology": {
        "nodes": {
            "r1": {"kind": "arista_ceos", "image": "ceos"},
            "r2": {"kind": "linux", "image": "alpine"},
        },
        "links": [
            {"endpoints": ["r1:eth1", "r2:eth1"]},
            {"endpoints": ["r2:eth2", "host:r2-eth2"]},
        ],
    },
}


def test_parse_target_and_filter():
    assert parse_target("Spine-1:eth1") == ("Spine-1", "eth1")
    assert parse_target("r1:Ethernet1/1") == ("r1", "Ethernet1/1")
    for bad in ("r1", "r1:", ":eth1"):
        with pytest.raises(ValueError, match="not 'node:interface'"):
            parse_target(bad)

    assert clean_filter("  tcp   port 179 ") == "tcp port 179"
    assert clean_filter("host 10.0.0.1 and (icmp or arp)") == "host 10.0.0.1 and (icmp or arp)"
    assert clean_filter("") == ""
    # Newlines and tabs are whitespace, other control characters are refused
    assert clean_filter("tcp\nport 179") == "tcp port 179"
    with pytest.raises(ValueError, match="control characters"):
        clean_filter("tcp\x00")
    with pytest.raises(ValueError, match="must not start with '-'"):
        clean_filter("-w /etc/passwd")
    with pytest.raises(ValueError, match="longer than"):
        clean_filter("x" * 2000)


def test_spec_validation():
    CaptureSpec("r1", "eth1", count=5, duration=2.5, snaplen=96)
    for iface in ("", "-w", "eth1 eth2", "eth1;rm", "a" * 16, "eth1\n"):
        with pytest.raises(ValueError, match="Invalid interface"):
            CaptureSpec("r1", iface)
    with pytest.raises(ValueError, match="count"):
        CaptureSpec("r1", "eth1", count=0)
    with pytest.raises(ValueError, match="duration"):
        CaptureSpec("r1", "eth1", duration=-1)
    with pytest.raises(ValueError, match="format"):
        CaptureSpec("r1", "eth1", format="json")
    with pytest.raises(ValueError, match="must not start"):
        CaptureSpec("r1", "eth1", bpf_filter="--help")


def test_interfaces_come_from_the_topology():
    topo = topology_from_dict(TOPO)
    assert node_interfaces(topo, "r1") == ["eth1", "eth0"]
    assert node_interfaces(topo, "r2") == ["eth1", "eth2", "eth0"]
    check_interface(topo, "r2", "eth2")
    check_interface(topo, "r1", "eth0")  # management
    with pytest.raises(ValueError, match="no interface 'eth7'.*known: eth1, eth0"):
        check_interface(topo, "r1", "eth7")
    with pytest.raises(ValueError, match="no node 'r9'"):
        check_interface(topo, "r9", "eth1")


def test_interface_aliases_map_to_linux_names():
    topo = topology_from_dict({"name": "lab", "topology": {
        "nodes": {"s1": {"kind": "arista_ceos"}, "r1": {"kind": "cisco_iol"}},
        "links": [{"endpoints": ["s1:Ethernet1/2", "r1:Ethernet0/3"]}]}})
    assert check_interface(topo, "s1", "Ethernet1/2") == "eth1_2"
    assert check_interface(topo, "s1", "eth1_2") == "eth1_2"
    assert check_interface(topo, "r1", "Ethernet0/3") == "eth3"
    assert check_interface(topo, "r1", "eth0") == "eth0"
    # The kind's own name also works when the topology uses Linux names
    assert check_interface(topology_from_dict(TOPO), "r1", "Ethernet1") == "eth1"
    with pytest.raises(ValueError, match="no interface 'eth5'"):
        check_interface(topo, "r1", "eth5")


def test_tcpdump_args():
    assert tcpdump_args(CaptureSpec("r1", "eth1")) == [
        "tcpdump", "-i", "eth1", "--immediate-mode", "-l", "-nn"]
    spec = CaptureSpec("r1", "eth1", format="pcap", bpf_filter="tcp port 179", count=10, snaplen=128)
    assert tcpdump_args(spec) == [
        "tcpdump", "-i", "eth1", "--immediate-mode", "-U", "-w", "-",
        "-c", "10", "-s", "128", "--", "tcp port 179"]


def test_capture_commands():
    spec = CaptureSpec("r1", "eth1", bpf_filter="icmp", duration=30)
    node = capture_command(spec, "clab-lab-r1", "node")
    assert node[:6] == ["docker", "exec", "clab-lab-r1", "sh", "-c", NODE_WRAPPER]
    # $0, then the `timeout` backstop (duration + grace), then tcpdump argv
    assert node[6:8] == ["clabfleet-capture", "40"]
    assert node[8:] == tcpdump_args(spec)
    no_limit = capture_command(CaptureSpec("r1", "eth1"), "c", "node")
    assert no_limit[7] == "0"

    helper = capture_command(spec, "clab-lab-r1", "helper", "img:1", "cap-x")
    assert helper == [
        "docker", "run", "--rm", "--name", "cap-x", "--label", "clabfleet.capture=1",
        "--network", "container:clab-lab-r1", "--cap-add", "NET_RAW", "--cap-add", "NET_ADMIN",
        "img:1", *tcpdump_args(spec)]
    with pytest.raises(ValueError):
        capture_command(spec, "c", "helper")  # no container name
    with pytest.raises(ValueError):
        capture_command(spec, "c", "nsenter")

    assert stop_command("node", "c", pid=42) == [
        "docker", "exec", "c", "sh", "-c", NODE_STOP, "clabfleet-stop", "42"]
    assert stop_command("node", "c") is None
    assert stop_command("helper", "c", "cap-x") == ["docker", "rm", "-f", "cap-x"]


def test_sudo_prefix_local_and_remote(monkeypatch):
    local = LocalRunner(sudo=True)
    assert sudo_prefix(local) == ["sudo"]
    local.interactive_sudo = False   # the GUI never waits on a password prompt
    assert sudo_prefix(local) == ["sudo", "-n"]
    assert sudo_prefix(SSHRunner("h1", sudo=True)) == ["sudo", "-n"]

    started = []
    monkeypatch.setattr(cap, "LocalProcess", lambda argv: started.append(("local", argv)))
    monkeypatch.setattr(cap, "SSHProcess", lambda client, argv: started.append(("ssh", argv)))
    remote = SSHRunner("h1", sudo=True)
    monkeypatch.setattr(remote, "client", lambda: object())
    cap.spawn(local, ["docker", "ps"], sudo=True)
    cap.spawn(local, ["docker", "ps"], sudo=False)
    cap.spawn(remote, ["docker", "ps"], sudo=True)
    assert started == [
        ("local", ["sudo", "-n", "docker", "ps"]),
        ("local", ["docker", "ps"]),
        ("ssh", ["sudo", "-n", "docker", "ps"]),
    ]


# ----------------------------------------------------------------------
# Capture lifecycle with a fake host and process
# ----------------------------------------------------------------------

class FakeHost(Runner):
    """Answers the docker commands a capture runs on its host."""

    def __init__(self, has_tcpdump=True, has_image=True, docker_needs_sudo=False):
        super().__init__()
        self.name = "h1"
        self.has_tcpdump = has_tcpdump
        self.has_image = has_image
        self.docker_needs_sudo = docker_needs_sudo
        self.calls = []
        self.proc = None  # the capture's process, ended by stop commands

    def run(self, args, cwd=None, check=True, sudo=None, on_output=None):
        self.calls.append((list(args), bool(sudo)))
        if self.docker_needs_sudo and not sudo:
            return CommandResult(1, "", "permission denied while trying to connect to the "
                                        "Docker daemon socket")
        if args[:2] == ["docker", "version"]:
            return CommandResult(0, "27.0\n", "")
        if args[:2] == ["docker", "exec"] and args[3:] == cap.DETECT_TCPDUMP:
            return (CommandResult(0, "/usr/sbin/tcpdump\n", "") if self.has_tcpdump
                    else CommandResult(127, "", ""))
        if args[:3] == ["docker", "image", "inspect"]:
            return CommandResult(0 if self.has_image else 1, "", "")
        if self.proc and (NODE_STOP in args or args[:3] == ["docker", "rm", "-f"]):
            self.proc.end()
        return CommandResult(0, "", "")

    def commands(self, prefix):
        return [args for args, _ in self.calls if args[:len(prefix)] == prefix]


class FakeProcess:
    """A capture stream: yields ``chunks``, then blocks until closed if ``hold``."""

    def __init__(self, chunks=(), stderr=(), hold=False, exit_code=0):
        self.chunks = list(chunks)
        self.stderr = list(stderr)
        self.hold = hold
        self.exit_code = exit_code
        self.ended = threading.Event()   # tcpdump exited (killed)
        self.closed = threading.Event()  # our side closed

    def end(self):
        self.ended.set()

    def read(self, size):
        if self.chunks:
            return self.chunks.pop(0)
        if self.hold:
            self.ended.wait(10)
        return b""

    def stderr_lines(self):
        yield from self.stderr
        if self.hold:
            self.ended.wait(10)

    def wait(self, timeout=None):
        if self.hold and not self.ended.wait(timeout):
            return None
        return self.exit_code

    def close(self):
        self.closed.set()
        self.ended.set()


def _spawner(proc, record):
    def spawn(runner, argv, sudo):
        record.append((argv, sudo))
        runner.proc = proc
        return proc
    return spawn


def test_capture_uses_node_tcpdump_and_kills_it_by_pid():
    host = FakeHost()
    proc = FakeProcess([b"pkt1\n", b"pkt2\n"], stderr=[f"{cap.PID_MARKER} 321", "listening on eth1"],
                       hold=True)
    spawned, messages = [], []
    spec = CaptureSpec("r1", "eth1")
    c = Capture(host, "clab-lab-r1", spec, on_message=messages.append,
                spawner=_spawner(proc, spawned)).start()
    assert c.method == "node"
    assert spawned == [(capture_command(spec, "clab-lab-r1", "node"), False)]
    assert c.read() == b"pkt1\n"
    assert c.read() == b"pkt2\n"
    assert "node's tcpdump" in c.describe()

    c.stop()
    c.stop()  # idempotent
    assert host.commands(["docker", "exec", "clab-lab-r1", "sh", "-c", NODE_STOP]) == [
        stop_command("node", "clab-lab-r1", pid=321)]
    assert proc.closed.is_set()
    assert c.read() == b""
    assert messages == ["listening on eth1"]  # the PID line is not shown
    assert c.stopped and c.stop_reason == "stopped"


def test_capture_falls_back_to_helper_container_and_removes_it():
    host = FakeHost(has_tcpdump=False, has_image=False)
    proc = FakeProcess([b"\xd4\xc3\xb2\xa1"])
    spawned, messages = [], []
    c = Capture(host, "clab-lab-r2", CaptureSpec("r2", "eth1", format="pcap", count=1),
                helper_image="netshoot:test", on_message=messages.append,
                spawner=_spawner(proc, spawned)).start()
    assert c.method == "helper"
    assert host.commands(["docker", "pull"]) == [["docker", "pull", "netshoot:test"]]
    assert messages == ["Pulling capture helper image netshoot:test..."]
    argv = spawned[0][0]
    assert argv[:5] == ["docker", "run", "--rm", "--name", c.name]
    assert "container:clab-lab-r2" in argv and "netshoot:test" in argv

    # tcpdump ended by itself (count reached): the container is removed anyway
    assert c.read() == b"\xd4\xc3\xb2\xa1"
    assert c.read() == b""
    assert c.exit_code == 0
    assert host.commands(["docker", "rm"]) == [["docker", "rm", "-f", c.name]]
    c.stop()
    assert len(host.commands(["docker", "rm"])) == 1


def test_capture_helper_image_from_env_and_pull_failure(monkeypatch):
    monkeypatch.setenv("CLAB_CAPTURE_IMAGE", "registry.local/tcpdump:1")
    host = FakeHost(has_tcpdump=False, has_image=False)
    host.run_orig = host.run

    def run(args, **kw):
        if args[:2] == ["docker", "pull"]:
            return CommandResult(1, "", "pull access denied")
        return host.run_orig(args, **kw)

    host.run = run
    c = Capture(host, "c", CaptureSpec("r2", "eth1"), spawner=_spawner(FakeProcess(), []))
    assert c.helper_image == "registry.local/tcpdump:1"
    with pytest.raises(CaptureError, match="pull access denied"):
        c.start()


def test_capture_uses_sudo_when_docker_refuses():
    host = FakeHost(docker_needs_sudo=True)
    spawned = []
    proc = FakeProcess(stderr=[f"{cap.PID_MARKER} 7"])
    c = Capture(host, "c", CaptureSpec("r1", "eth1"), host_sudo=True,
                spawner=_spawner(proc, spawned)).start()
    assert spawned[0][1] is True
    c.stop()
    assert all(sudo for args, sudo in host.calls[1:])  # everything after the probe

    with pytest.raises(CaptureError, match="Docker is not usable"):
        Capture(FakeHost(docker_needs_sudo=True), "c", CaptureSpec("r1", "eth1"),
                host_sudo=False, spawner=_spawner(FakeProcess(), [])).start()


def test_capture_duration_stops_it():
    host = FakeHost()
    proc = FakeProcess(stderr=[f"{cap.PID_MARKER} 9"], hold=True)
    c = Capture(host, "c", CaptureSpec("r1", "eth1", duration=0.2),
                spawner=_spawner(proc, [])).start()
    started = time.monotonic()
    assert c.read() == b""  # blocks until the timer stops the capture
    assert time.monotonic() - started < 3
    assert c.stop_reason == "duration"
    assert host.commands(["docker", "exec", "c", "sh", "-c", NODE_STOP])


def test_capture_rejects_unknown_method():
    with pytest.raises(ValueError, match="Unknown capture method"):
        Capture(FakeHost(), "c", CaptureSpec("r1", "eth1"), method="nsenter")


def test_node_capture_end_to_end_with_fake_docker(tmp_path, monkeypatch):
    """The real wrapper and stop scripts, with `docker exec` running locally."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # docker exec <container> argv... → run argv here; docker version → ok
    (bindir / "docker").write_text(
        '#!/bin/sh\n[ "$1" = version ] && exit 0\n[ "$1" = exec ] || exit 1\n'
        'shift 2\nexec "$@"\n')
    (bindir / "tcpdump").write_text(
        '#!/bin/sh\necho "listening on $2" >&2\n'
        'while :; do echo "pkt on $2"; sleep 0.05; done\n')
    for f in bindir.iterdir():
        f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    messages = []
    c = Capture(LocalRunner(), "clab-lab-r1", CaptureSpec("r1", "eth1"),
                on_message=messages.append).start()
    assert c.method == "node"
    assert b"pkt on eth1" in c.read()
    assert c._pid_seen.wait(5)
    pid = c._pid
    c.stop()
    assert c.read() == b""
    for _ in range(50):
        if not os.path.exists(f"/proc/{pid}"):
            break
        time.sleep(0.1)
    assert not os.path.exists(f"/proc/{pid}")
    assert "listening on eth1" in messages


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

@pytest.fixture
def topo_file(tmp_path):
    path = tmp_path / "lab.clab.yml"
    path.write_text(yaml.safe_dump(TOPO, sort_keys=False))
    return path


class FakeCapture:
    instances = []

    def __init__(self, runner, container, spec, host_sudo=False, method="auto",
                 helper_image=None, on_message=None):
        self.container, self.spec, self.method = container, spec, method
        self.chunks = [b"\xd4\xc3\xb2\xa1rest" if spec.format == "pcap" else b"12:00 IP x > y\n"]
        self.stop_reason = ""
        self.exit_code = 0
        self.stops = 0
        FakeCapture.instances.append(self)

    def start(self):
        return self

    def describe(self):
        return f"{self.spec.node}:{self.spec.interface}"

    def read(self, size=65536):
        return self.chunks.pop(0) if self.chunks else b""

    def stop(self, reason="stopped"):
        self.stops += 1
        self.stop_reason = self.stop_reason or reason


def test_cli_capture_writes_pcap(topo_file, tmp_path, monkeypatch, capsys):
    FakeCapture.instances = []
    monkeypatch.setattr(cli, "Capture", FakeCapture)
    monkeypatch.setattr(cli, "find_container", lambda cluster, topo, node: (
        HostInfo("localhost"), {"container": f"clab-lab-{node}", "host": "localhost"}))
    out = tmp_path / "out.pcap"
    rc = cli.main(["capture", str(topo_file), "r2:eth1", "-w", str(out), "-f", "icmp",
                   "-c", "3", "--via", "helper"])
    assert rc == 0
    assert out.read_bytes() == b"\xd4\xc3\xb2\xa1rest"
    c = FakeCapture.instances[0]
    assert c.container == "clab-lab-r2" and c.method == "helper"
    assert (c.spec.format, c.spec.bpf_filter, c.spec.count) == ("pcap", "icmp", 3)
    assert c.stops >= 1
    assert "Capturing on r2:eth1" in capsys.readouterr().err

    # Text decode by default
    rc = cli.main(["capture", str(topo_file), "r1:eth1"])
    assert rc == 0
    assert FakeCapture.instances[-1].spec.format == "text"
    assert "12:00 IP x > y" in capsys.readouterr().out


def test_cli_capture_rejects_bad_targets(topo_file, capsys):
    assert cli.main(["capture", str(topo_file), "r1"]) == 1
    assert "not 'node:interface'" in capsys.readouterr().err
    assert cli.main(["capture", str(topo_file), "r1:eth9"]) == 1
    assert "no interface 'eth9'" in capsys.readouterr().err
    assert cli.main(["capture", str(topo_file), "r1:eth1", "--filter=-w x"]) == 1
    assert "must not start with '-'" in capsys.readouterr().err
