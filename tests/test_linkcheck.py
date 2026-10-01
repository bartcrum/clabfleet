import socket

import pytest

from clabfleet import cli, deployer, linkcheck
from clabfleet.cluster import ClusterConfig, HostInfo
from clabfleet.deployer import DeploymentError, LabDeployer
from clabfleet.linkcheck import all_pairs, check_links, describe, failures, vxlan_pairs
from clabfleet.runner import CommandResult, LocalRunner, Runner


def _free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def test_pairs():
    links = [{"hosts": ["h1", "h2"]}, {"hosts": ["h2", "h1"]}, {"hosts": ["h1", "h3"]}]
    assert vxlan_pairs(links) == [("h1", "h2"), ("h2", "h1"), ("h1", "h3"), ("h3", "h1")]
    hosts = [HostInfo("a", "10.0.0.1"), HostInfo("b", "10.0.0.2"), HostInfo("c", "10.0.0.3")]
    assert len(all_pairs(hosts)) == 6


def test_udp_probe_end_to_end_on_this_machine():
    # Two "hosts" that are both this machine: real listener and sender scripts
    port = _free_udp_port()
    hosts = [HostInfo("a", "localhost", vtep_ip="127.0.0.1"),
             HostInfo("b", "localhost", vtep_ip="127.0.0.1")]
    runners = {"a": LocalRunner(), "b": LocalRunner()}
    [r] = check_links(runners, hosts, [("a", "b")], port, listen_seconds=2)
    assert r["udp"] is True
    assert r["ping"] in (True, None)  # None where ping is not installed
    assert failures([r]) == []


def test_udp_probe_reports_port_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as held:
        held.bind(("0.0.0.0", 0))
        port = held.getsockname()[1]
        hosts = [HostInfo("a", "localhost", vtep_ip="127.0.0.1"),
                 HostInfo("b", "localhost", vtep_ip="127.0.0.1")]
        [r] = check_links({"a": LocalRunner(), "b": LocalRunner()}, hosts, [("a", "b")],
                          port, listen_seconds=1)
    assert r["udp"] is None
    assert "already in use" in r["note"]


class FakeHost(Runner):
    """ping answers with ping_rc; python3 listener prints READY but receives nothing."""

    def __init__(self, ping_rc=0, python=True):
        super().__init__()
        self.ping_rc, self.python = ping_rc, python

    def run(self, args, cwd=None, check=True, sudo=None, on_output=None):
        if args[0] == "ping":
            return CommandResult(self.ping_rc, "", "")
        if not self.python:
            if on_output:
                on_output("sh: python3: command not found")
            return CommandResult(127, "", "")
        if on_output:  # listener
            on_output("READY")
        return CommandResult(0, "", "")


def test_blocked_and_missing_tools_are_reported():
    hosts = [HostInfo("h1", "10.0.0.1"), HostInfo("h2", "10.0.0.2"), HostInfo("h3", "10.0.0.3"),
             HostInfo("me", "localhost")]
    runners = {"h1": FakeHost(), "h2": FakeHost(ping_rc=1), "h3": FakeHost(python=False),
               "me": FakeHost()}
    results = check_links(runners, hosts, [("h1", "h2"), ("h2", "h1"), ("h1", "h3"),
                                           ("h1", "me")], 14789, listen_seconds=0.2)
    r = {(x["from"], x["to"]): x for x in results}
    assert r[("h1", "h2")]["ping"] is True and r[("h1", "h2")]["udp"] is False
    assert "check firewalls on h2" in r[("h1", "h2")]["note"]
    assert r[("h2", "h1")]["ping"] is False
    assert r[("h1", "h3")]["udp"] is None and "could not run python3 on h3" in r[("h1", "h3")]["note"]
    assert r[("h1", "me")]["ping"] is None and "set vtep_ip" in r[("h1", "me")]["note"]
    assert [(x["from"], x["to"]) for x in failures(results)] == [("h1", "h2"), ("h2", "h1")]
    assert describe(r[("h1", "h2")]).startswith("h1 -> h2 (10.0.0.2): ping ok, udp FAILED.")


def test_deploy_stops_before_creating_anything_when_links_fail(monkeypatch, tmp_path):
    topo = tmp_path / "t.clab.yml"
    topo.write_text("name: t\ntopology:\n  nodes:\n    a: {kind: linux}\n    b: {kind: linux}\n"
                    "  links:\n    - endpoints: [a:eth1, b:eth1]\n")
    hosts = [HostInfo("h1", "10.0.0.1"), HostInfo("h2", "10.0.0.2")]
    monkeypatch.setattr(deployer, "create_runner", lambda host: FakeHost())
    monkeypatch.setattr(deployer, "scan_used_vnis", lambda runner, host: {})
    monkeypatch.setattr(linkcheck, "LISTEN_SECONDS", 0.2)

    def plan(self, t, strategy):
        from clabfleet.placement import NodePlacement, PlacementPlan
        p = PlacementPlan()
        p.placements += [NodePlacement("a", "h1", 1, 1), NodePlacement("b", "h2", 1, 1)]
        return p
    monkeypatch.setattr(LabDeployer, "_plan", plan)
    deployed = []
    monkeypatch.setattr(LabDeployer, "_deploy_on_host",
                        lambda self, host, *a: deployed.append(host.name) or {"status": "deployed"})

    with pytest.raises(DeploymentError, match="--skip-link-check"):
        LabDeployer(ClusterConfig(hosts=hosts)).deploy(topo, check_images=False)
    assert deployed == []
    assert not (tmp_path / "t.placement.json").exists()

    summary = LabDeployer(ClusterConfig(hosts=hosts)).deploy(
        topo, check_images=False, check_connectivity=False)
    assert deployed == ["h1", "h2"] and "link_check" not in summary


def test_cli_status_check_links(monkeypatch, capsys):
    monkeypatch.setattr(cli, "create_runner", lambda host: FakeHost())
    monkeypatch.setattr(cli, "_status", lambda cluster: 0)
    monkeypatch.setattr(linkcheck, "LISTEN_SECONDS", 0.2)
    rc = cli.main(["status", "--check-links", "--cluster", "topologies/cluster.yaml"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "VXLAN connectivity (UDP 14789):" in out
    assert "clab-1 -> clab-2 (192.168.1.102): ping ok, udp FAILED" in out
    assert "6 of 6 host pairs failed." in out
