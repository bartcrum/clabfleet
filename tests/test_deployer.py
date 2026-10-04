import json
from pathlib import Path

import pytest
import yaml

from clabfleet import cli, deployer
from clabfleet.cluster import ClusterConfig, HostInfo, load_cluster_config, probe_host_resources
from clabfleet.deployer import (
    DeploymentError,
    LabDeployer,
    allocate_vni_block,
    count_cross_host_links,
    scan_used_vnis,
    split_topology,
)
from clabfleet.runner import CommandError, CommandResult
from clabfleet.placement import NodePlacement, PlacementPlan
from clabfleet.topology import topology_from_dict

TOPOLOGIES = Path(__file__).parent.parent / "topologies"


def _plan(mapping):
    plan = PlacementPlan()
    for node, host in mapping.items():
        plan.placements.append(NodePlacement(node, host, 1, 512))
    return plan


def _topo():
    return topology_from_dict({
        "name": "t",
        "topology": {
            "nodes": {n: {"kind": "linux"} for n in ("a", "b", "c")},
            "links": [
                {"endpoints": ["a:eth1", "b:eth1"]},
                {"endpoints": ["a:eth2", "c:eth1"], "mtu": 1400},
                {"endpoints": ["c:eth2", "host:c-eth2"]},
            ],
        },
    })


def test_split_creates_matching_vxlan_pairs():
    cluster = ClusterConfig(hosts=[HostInfo("h1", "10.0.0.1"), HostInfo("h2", "10.0.0.2")])
    host_topos, cross = split_topology(_topo(), _plan({"a": "h1", "b": "h1", "c": "h2"}), cluster)

    h1, h2 = host_topos["h1"]["topology"], host_topos["h2"]["topology"]
    assert list(h1["nodes"]) == ["a", "b"]
    assert list(h2["nodes"]) == ["c"]
    assert h1["links"][0] == {"endpoints": ["a:eth1", "b:eth1"]}
    assert h1["links"][1] == {
        "type": "vxlan-stitch",
        "endpoint": {"node": "a", "interface": "eth2"},
        "remote": "10.0.0.2", "vni": 1000, "dst-port": 14789, "mtu": 1400,
    }
    assert h2["links"][0]["remote"] == "10.0.0.1"
    assert h2["links"][0]["vni"] == 1000
    assert h2["links"][1] == {"endpoints": ["c:eth2", "host:c-eth2"]}
    assert cross == [{"a": "a:eth2", "b": "c:eth1", "hosts": ["h1", "h2"], "vni": 1000}]


def test_split_requires_vtep_for_local_host():
    cluster = ClusterConfig(hosts=[HostInfo("me", "localhost"), HostInfo("h2", "10.0.0.2")])
    with pytest.raises(DeploymentError, match="vtep_ip"):
        split_topology(_topo(), _plan({"a": "me", "b": "me", "c": "h2"}), cluster)


def test_split_single_host_is_unchanged():
    topo = _topo()
    cluster = ClusterConfig(hosts=[HostInfo("h1", "10.0.0.1")])
    host_topos, cross = split_topology(topo, _plan({"a": "h1", "b": "h1", "c": "h1"}), cluster)
    assert cross == []
    assert host_topos["h1"] == topo.data


def test_cluster_example_loads():
    cluster = load_cluster_config(TOPOLOGIES / "cluster.yaml")
    assert [h.name for h in cluster.hosts] == ["clab-1", "clab-2", "clab-3"]
    assert cluster.hosts[2].vtep == "10.10.10.3"
    assert cluster.mtu == 1450


def test_cluster_host_key_policy(tmp_path):
    from clabfleet.cluster import ClusterConfigError, create_runner
    path = tmp_path / "cluster.yaml"
    path.write_text(yaml.safe_dump({
        "cluster": {"host_key_policy": "strict"},
        "hosts": [{"name": "a", "host": "10.0.0.1"},
                  {"name": "b", "host": "10.0.0.2", "host_key_policy": "accept-new"}],
    }))
    cluster = load_cluster_config(path)
    assert [h.host_key_policy for h in cluster.hosts] == ["strict", "accept-new"]
    assert create_runner(cluster.hosts[0]).host_key_policy == "strict"
    path.write_text(yaml.safe_dump({"hosts": [{"host": "10.0.0.1", "host_key_policy": "yolo"}]}))
    with pytest.raises(ClusterConfigError, match="host_key_policy"):
        load_cluster_config(path)

def test_cli_cluster_dry_run_writes_host_files(tmp_path, monkeypatch, capsys):
    # No SSH in tests: use the inventory limits as-is, and another lab holds VNI 1000
    monkeypatch.setattr(deployer, "probe_host_resources", lambda runner, host: {})
    monkeypatch.setattr(deployer, "inspect_all", lambda runner: {})
    monkeypatch.setattr(deployer, "scan_used_vnis",
                        lambda runner, host: {"other": {1000}} if host.name == "clab-2" else {})

    rc = cli.main([
        "deploy", str(TOPOLOGIES / "large_campus.clab.yml"),
        "--cluster", str(TOPOLOGIES / "cluster.yaml"),
        "--dry-run", "--output-dir", str(tmp_path),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("campus-network: dry run, nothing deployed\n")
    assert "  clab-1 (2 nodes): Core-1, Core-2\n" in out
    assert "8 links between hosts, VNI 1001-1008:" in out  # 1000 is taken by the other lab
    assert "Core-1:Ethernet0/2 - Dist-1:Ethernet0/1" in out and "clab-1 - clab-2  VNI 1001" in out

    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == [
        "campus-network.clab-1.clab.yml",
        "campus-network.clab-2.clab.yml",
        "campus-network.clab-3.clab.yml",
    ]
    clab1 = yaml.safe_load((tmp_path / files[0]).read_text())
    assert {"Core-1", "Core-2"} <= set(clab1["topology"]["nodes"])
    # Every node lands on exactly one host
    all_nodes = []
    for f in files:
        all_nodes += list(yaml.safe_load((tmp_path / f).read_text())["topology"]["nodes"])
    assert sorted(all_nodes) == sorted(
        yaml.safe_load((TOPOLOGIES / "large_campus.clab.yml").read_text())["topology"]["nodes"]
    )
    vnis = sorted(link["vni"] for f in files
                  for link in yaml.safe_load((tmp_path / f).read_text())["topology"].get("links", [])
                  if "vni" in link)
    assert vnis and vnis[0] == 1001  # 1000 is taken by the other lab


def test_cli_single_host_dry_run(capsys):
    rc = cli.main(["deploy", str(TOPOLOGIES / "three_router_triangle.clab.yml"), "--dry-run"])
    assert rc == 0
    assert capsys.readouterr().out == ("three-router-triangle: dry run, nothing deployed\n"
                                       "  localhost (3 nodes): R1, R2, R3\n")
    # --json: the full result, for scripts
    assert cli.main(["deploy", str(TOPOLOGIES / "three_router_triangle.clab.yml"), "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "lab": "three-router-triangle", "hosts": {}, "placement": {"localhost": ["R1", "R2", "R3"]},
        "cross_host_links": [], "dry_run": True}


def test_cli_summaries_for_people():
    text = cli._summary_text
    assert text("deploy", {
        "lab": "lab", "status": "partial", "placement": {"a": ["r1", "r2"], "b": ["r3"]},
        "hosts": {"a": {"status": "deployed", "nodes": ["r1", "r2"], "seconds": 12.5},
                  "b": {"error": "12:00:01 INFO Parsing\n   ERROR  \n  image x:1 not found", "seconds": 0.4}},
        "readiness": {"ready": False, "seconds": 900, "pending": {"r1": "CLI not answering"}},
    }) == ("lab: PARTLY deployed\n"
           "  a  2 nodes  deployed                     12.5s\n"
           "  b  1 node   FAILED: image x:1 not found  0.4s\n"
           "  NOT ready after 900s: r1 (CLI not answering)")
    assert text("destroy", {"lab": "lab", "hosts": {"a": {"status": "ok", "seconds": 1.3},
                                                    "b": {"status": "not-deployed", "seconds": 0.1}}}) == (
        "lab: destroyed\n  a  ok            1.3s\n  b  not deployed  0.1s")
    assert text("save", {"lab": "lab", "hosts": {"a": {"error": "no route to host"}}}) == (
        "lab: save FAILED\n  a  FAILED: no route to host")
    assert text("inspect", {"hosts": {
        "a": {"status": "ok", "data": {"lab": [{"name": "clab-lab-r1", "kind": "linux", "state": "running",
                                                "ipv4_address": "172.20.20.2/24"}]}},
        "b": {"status": "error", "error": "ERROR\n could not get container: containers not found."},
        "c": {"status": "ok", "data": {}},
    }}) == "lab on a: 1 node\n  r1  linux  running  172.20.20.2\nb: not deployed\nc: no labs running"


def test_split_uses_given_vni_base():
    cluster = ClusterConfig(hosts=[HostInfo("h1", "10.0.0.1"), HostInfo("h2", "10.0.0.2")])
    plan = _plan({"a": "h1", "b": "h2", "c": "h2"})
    assert count_cross_host_links(_topo(), plan) == 2
    host_topos, cross = split_topology(_topo(), plan, cluster, vni_base=5000)
    assert [c["vni"] for c in cross] == [5000, 5001]
    assert [link["vni"] for link in host_topos["h1"]["topology"]["links"]] == [5000, 5001]


def test_allocate_vni_block_skips_used_ranges():
    assert allocate_vni_block(set(), 1000, 3) == 1000
    assert allocate_vni_block({1000, 1001}, 1000, 3) == 1002
    assert allocate_vni_block({1001, 1005}, 1000, 3) == 1002
    assert allocate_vni_block({1001, 1005}, 1000, 4) == 1006
    assert allocate_vni_block({999, 1003}, 1000, 3) == 1000
    with pytest.raises(DeploymentError, match="No free block"):
        allocate_vni_block({2**24 - 2}, 2**24 - 3, 2)


class FakeRunner:
    def __init__(self, stdout="", exc=None):
        self.stdout, self.exc, self.calls = stdout, exc, []

    def run(self, args, cwd=None, check=True, sudo=None, on_output=None):
        self.calls.append(args)
        if self.exc:
            raise self.exc
        return CommandResult(0, self.stdout, "")

    def close(self):
        pass


def test_scan_used_vnis_groups_by_lab_dir():
    runner = FakeRunner(
        "clabfleet/campus/campus.clab.yml:    vni: 1000\n"
        "clabfleet/campus/campus.clab.yml:    vni: 1001\n"
        "clabfleet/dc/dc.clab.yml:  vni: 1002\n"
        "garbage line\n"
    )
    used = scan_used_vnis(runner, HostInfo("h1", "10.0.0.1"))
    assert used == {"campus": {1000, 1001}, "dc": {1002}}
    script = runner.calls[0][2]
    assert runner.calls[0][:2] == ["sh", "-c"]
    assert "clabfleet/*/*.clab.yml" in script


def test_deploy_dry_run_allocates_around_other_labs(monkeypatch):
    cluster = ClusterConfig(hosts=[HostInfo("h1", "10.0.0.1"), HostInfo("h2", "10.0.0.2")])
    runners = {
        # this lab's own old VNIs are free to reuse; "dc" holds 1000-1001
        "h1": FakeRunner("clabfleet/t/t.clab.yml: vni: 1002\nclabfleet/dc/dc.clab.yml: vni: 1000\n"),
        "h2": FakeRunner("clabfleet/dc/dc.clab.yml: vni: 1001\n"),
    }
    monkeypatch.setattr(deployer, "create_runner", lambda host: runners[host.name])
    monkeypatch.setattr(LabDeployer, "_plan",
                        lambda self, topo, strategy: _plan({"a": "h1", "b": "h2", "c": "h2"}))
    monkeypatch.setattr(deployer, "load_topology", lambda path: _topo())

    summary = LabDeployer(cluster).deploy("t.clab.yml", dry_run=True)
    assert summary["vni_range"] == [1002, 1003]
    assert [c["vni"] for c in summary["cross_host_links"]] == [1002, 1003]


def test_vni_scan_failure_is_not_fatal(monkeypatch, caplog):
    cluster = ClusterConfig(hosts=[HostInfo("h1", "10.0.0.1"), HostInfo("h2", "10.0.0.2")])
    runners = {"h1": FakeRunner(exc=OSError("ssh down")), "h2": FakeRunner("")}
    monkeypatch.setattr(deployer, "create_runner", lambda host: runners[host.name])
    monkeypatch.setattr(LabDeployer, "_plan",
                        lambda self, topo, strategy: _plan({"a": "h1", "b": "h2", "c": "h1"}))
    monkeypatch.setattr(deployer, "load_topology", lambda path: _topo())
    summary = LabDeployer(cluster).deploy("t.clab.yml", dry_run=True)
    assert summary["vni_range"] == [1000, 1000]
    assert "Could not check VNIs in use on h1" in caplog.text


def test_probe_raises_when_host_unreachable():
    host = HostInfo("h1", "10.0.0.1")
    with pytest.raises(OSError):
        probe_host_resources(FakeRunner(exc=OSError("timed out")), host)


def test_probe_tolerates_failing_commands(caplog):
    host = HostInfo("h1", "10.0.0.1", max_cpu=4)
    runner = FakeRunner(exc=CommandError("nproc", 127, "not found"))
    assert probe_host_resources(runner, host) == {}
    assert host.max_cpu == 4
    assert "Could not probe resources on h1" in caplog.text
    assert probe_host_resources(FakeRunner("garbage"), host) == {}


def test_unreachable_host_fails_planning_at_once(monkeypatch):
    # The first failed connection ends the deploy; no further hosts or
    # steps (running labs, VNI scan) wait out their own SSH timeouts
    cluster = ClusterConfig(hosts=[HostInfo("h1", "10.0.0.1", max_cpu=4, max_ram=8192),
                                   HostInfo("h2", "10.0.0.2", max_cpu=4, max_ram=8192)])
    runner = FakeRunner(exc=OSError("timed out"))
    monkeypatch.setattr(deployer, "create_runner", lambda host: runner)
    monkeypatch.setattr(deployer, "load_topology", lambda path: _topo())
    with pytest.raises(DeploymentError, match=r"h1 \(10.0.0.1\) unreachable: timed out"):
        LabDeployer(cluster).deploy("t.clab.yml", dry_run=True)
    assert runner.calls == [["nproc"]]


def _running(lab, *nodes):
    """inspect --all JSON for running containers: (name, kind[, labels])."""
    items = []
    for node in nodes:
        name, kind, *extra = node
        labels = {"containerlab": lab, "clab-node-name": name, "clab-node-kind": kind}
        labels.update(extra[0] if extra else {})
        items.append({"Names": [f"clab-{lab}-{name}"], "Labels": labels, "State": "running"})
    return {lab: items}


def test_running_usage_estimates_per_lab():
    from clabfleet.nodes import running_usage
    data = {**_running("dc", ("s1", "arista_ceos"), ("h1", "linux", {"lab.cpu": "2", "lab.ram": "100"})),
            **_running("t", ("x", "arista_ceos"))}
    data["dc"].append({"Labels": {"containerlab": "dc", "clab-node-kind": "linux"}, "State": "exited"})
    assert running_usage(data, exclude_lab="t") == {"dc": {"nodes": 2, "cpu": 3.0, "ram": 2148}}
    assert set(running_usage(data)) == {"dc", "t"}


def test_placement_reserves_other_labs(monkeypatch):
    # h1 has 4 vCPU but another lab's three cEOS nodes use 3 of them
    busy = _running("dc", ("s1", "arista_ceos"), ("s2", "arista_ceos"), ("s3", "arista_ceos"))
    inspected = {"h1": {**busy, **_running("t", ("a", "linux"))}, "h2": {}}
    hosts = [HostInfo("h1", "10.0.0.1", max_cpu=4, max_ram=65536),
             HostInfo("h2", "10.0.0.2", max_cpu=4)]
    class Named:
        def __init__(self, name):
            self.name = name

        def close(self):
            pass

    monkeypatch.setattr(deployer, "create_runner", lambda host: Named(host.name))
    monkeypatch.setattr(deployer, "inspect_all", lambda runner: inspected[runner.name])
    probed = {"h2": 8192}

    def probe(runner, host):
        if host.max_ram <= 0:
            host.max_ram = probed[host.name]
        return {}
    monkeypatch.setattr(deployer, "probe_host_resources", probe)

    dep = LabDeployer(ClusterConfig(hosts=hosts))
    topo = topology_from_dict({"name": "t", "topology": {
        "nodes": {"a": {"kind": "linux", "labels": {"lab.cpu": "2"}}}}})
    plan = dep._plan(topo, "bin-pack")
    assert plan.host_for_node("a") == "h2"  # only 1 vCPU left on h1
    assert hosts[0].used_cpu == 3 and hosts[0].used_ram == 3 * 2048  # explicit max_ram
    assert hosts[1].used_cpu == 2 and hosts[1].used_ram == 128  # just this lab's node


def test_deploys_in_flight_count_for_other_labs(monkeypatch):
    # Another lab is being deployed in this process: its VNIs and host
    # resources must be avoided even though nothing exists on the hosts yet
    cluster = ClusterConfig(hosts=[HostInfo("h1", "10.0.0.1", max_cpu=4, max_ram=8192),
                                   HostInfo("h2", "10.0.0.2", max_cpu=4, max_ram=8192)])
    monkeypatch.setattr(deployer, "create_runner", lambda host: FakeRunner(""))
    monkeypatch.setattr(deployer, "probe_host_resources", lambda runner, host: {})
    monkeypatch.setattr(deployer, "inspect_all", lambda runner: {})
    other = PlacementPlan()
    other.placements.append(NodePlacement("x", "h1", 3, 1024))
    deployer._register_in_flight("other", other, range(1000, 1002))
    try:
        topo = topology_from_dict({"name": "t", "topology": {
            "nodes": {"a": {"kind": "linux", "labels": {"lab.cpu": "2"}},
                      "b": {"kind": "linux", "labels": {"lab.cpu": "2"}}},
            "links": [{"endpoints": ["a:eth1", "b:eth1"]}]}})
        monkeypatch.setattr(deployer, "load_topology", lambda path: topo)
        summary = LabDeployer(cluster).deploy("t.clab.yml", dry_run=True)
        assert summary["placement"] == {"h2": ["a", "b"]}  # h1 has 1 vCPU left
        assert "vni_range" not in summary
        spread = LabDeployer(ClusterConfig(hosts=[HostInfo("h1", "10.0.0.1"),
                                                  HostInfo("h2", "10.0.0.2")])).deploy(
            "t.clab.yml", dry_run=True, strategy="spread")
        assert spread["vni_range"] == [1002, 1002]  # 1000-1001 are in flight
    finally:
        deployer._unregister_in_flight("other")

    summary = LabDeployer(ClusterConfig(hosts=[HostInfo("h1", "10.0.0.1"),
                                               HostInfo("h2", "10.0.0.2")])).deploy(
        "t.clab.yml", dry_run=True, strategy="spread")
    assert summary["vni_range"] == [1000, 1000]  # free again once "other" finished
    assert deployer._IN_FLIGHT == {}
