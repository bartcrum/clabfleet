import json

import pytest
import yaml

from clabfleet import deployer
from clabfleet.cluster import ClusterConfig, HostInfo
from clabfleet.deployer import (
    LabDeployer,
    hosts_for_lab,
    placement_record_path,
    read_placement_record,
)
from clabfleet.placement import NodePlacement, PlacementPlan
from clabfleet.runner import CommandResult
from clabfleet.topology import load_topology

TOPO = {
    "name": "t",
    "topology": {
        "nodes": {n: {"kind": "linux", "image": "alpine"} for n in ("a", "b", "c")},
        "links": [{"endpoints": ["a:eth1", "b:eth1"]}, {"endpoints": ["a:eth2", "c:eth1"]}],
    },
}


class QuietRunner:
    def run(self, args, cwd=None, check=True, sudo=None, on_output=None):
        return CommandResult(0, "", "")

    def close(self):
        pass


@pytest.fixture
def topo_file(tmp_path):
    path = tmp_path / "t.clab.yml"
    path.write_text(yaml.safe_dump(TOPO))
    return path


def _cluster(*names):
    return ClusterConfig(hosts=[HostInfo(n, f"10.0.0.{i}") for i, n in enumerate(names, 1)],
                         source="cluster.yaml")


def _stub_deploy(monkeypatch, mapping, calls):
    def plan(self, topo, strategy):
        p = PlacementPlan()
        for node, host in mapping.items():
            p.placements.append(NodePlacement(node, host, 1, 512))
        return p

    def deploy_on_host(self, host, topo, data, reconfigure):
        calls.append(host.name)
        return {"status": "deployed", "nodes": list(data["topology"]["nodes"])}

    monkeypatch.setattr(deployer, "create_runner", lambda host: QuietRunner())
    monkeypatch.setattr(LabDeployer, "_plan", plan)
    monkeypatch.setattr(LabDeployer, "_deploy_on_host", deploy_on_host)


def test_deploy_writes_record_and_dry_run_does_not(topo_file, monkeypatch):
    calls = []
    _stub_deploy(monkeypatch, {"a": "h1", "b": "h1", "c": "h2"}, calls)
    dep = LabDeployer(_cluster("h1", "h2", "h3"))

    dep.deploy(topo_file, dry_run=True)
    assert not placement_record_path(load_topology(topo_file)).exists()

    summary = dep.deploy(topo_file, strategy="spread", check_images=False)
    assert calls == ["h1", "h2"]
    record_file = topo_file.parent / "t.placement.json"
    assert summary["placement_record"] == str(record_file)
    record = json.loads(record_file.read_text())
    assert record["lab"] == "t"
    assert record["topology"] == "t.clab.yml"
    assert record["cluster"] == "cluster.yaml"
    assert record["strategy"] == "spread"
    assert record["nodes"] == {"a": "h1", "b": "h1", "c": "h2"}
    assert record["hosts"] == {
        "h1": {"address": "10.0.0.1", "lab_dir": "clabfleet/t", "nodes": ["a", "b"]},
        "h2": {"address": "10.0.0.2", "lab_dir": "clabfleet/t", "nodes": ["c"]},
    }
    assert record["vni_range"] == [1000, 1000]
    assert record["cross_host_links"] == [
        {"a": "a:eth2", "b": "c:eth1", "hosts": ["h1", "h2"], "vni": 1000}
    ]


def test_single_local_host_record_points_at_lab_dir_next_to_topology(topo_file, monkeypatch):
    _stub_deploy(monkeypatch, {"a": "localhost", "b": "localhost", "c": "localhost"}, [])
    LabDeployer(ClusterConfig(hosts=[HostInfo("localhost")])).deploy(topo_file, check_images=False)
    record = read_placement_record(load_topology(topo_file))
    assert record["strategy"] is None and record["vni_range"] is None
    assert record["hosts"]["localhost"]["lab_dir"] == str(topo_file.parent.resolve() / "clab-t")


def _write_record(topo_file, hosts, lab="t"):
    (topo_file.parent / "t.placement.json").write_text(json.dumps({
        "lab": lab, "hosts": {h: {} for h in hosts}, "nodes": {},
    }))


def test_hosts_for_lab_uses_record(topo_file, caplog):
    topo = load_topology(topo_file)
    cluster = _cluster("h1", "h2", "h3")
    assert [h.name for h in hosts_for_lab(cluster, topo)] == ["h1", "h2", "h3"]

    _write_record(topo_file, ["h3", "h1"])
    assert [h.name for h in hosts_for_lab(cluster, topo)] == ["h1", "h3"]

    _write_record(topo_file, ["h2", "gone"])
    assert [h.name for h in hosts_for_lab(cluster, topo)] == ["h2"]
    assert "gone" in caplog.text

    _write_record(topo_file, ["gone"])  # nothing in common: try every host
    assert len(hosts_for_lab(cluster, topo)) == 3

    _write_record(topo_file, ["h1"], lab="other")
    assert len(hosts_for_lab(cluster, topo)) == 3
    (topo_file.parent / "t.placement.json").write_text("{not json")
    assert read_placement_record(topo) is None


def test_destroy_targets_recorded_hosts_and_removes_record(topo_file, monkeypatch):
    monkeypatch.setattr(deployer, "create_runner", lambda host: QuietRunner())
    touched = []

    def on_host(self, host, topo, command, extra, parse_json=False):
        touched.append((command, host.name))
        return {"status": "ok"} if host.name != "bad" else {"status": "error", "error": "x"}

    monkeypatch.setattr(LabDeployer, "_on_host", on_host)
    record_file = topo_file.parent / "t.placement.json"

    _write_record(topo_file, ["h2"])
    dep = LabDeployer(_cluster("h1", "h2", "h3"))
    dep.save(topo_file)
    dep.inspect(topo_file)
    dep.destroy(topo_file, cleanup=False)
    assert touched == [("save", "h2"), ("inspect", "h2"), ("destroy", "h2")]
    assert not record_file.exists()

    # A failed destroy keeps the record so a retry still knows where to look
    _write_record(topo_file, ["h2", "bad"])
    LabDeployer(_cluster("h2", "bad")).destroy(topo_file, cleanup=False)
    assert record_file.exists()
