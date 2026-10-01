from pathlib import Path

import pytest
import yaml

from clabfleet import cli, deployer
from clabfleet.cluster import ClusterConfig, HostInfo, load_cluster_config
from clabfleet.deployer import DeploymentError, split_topology
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


def test_cli_cluster_dry_run_writes_host_files(tmp_path, monkeypatch, capsys):
    # No SSH in tests: use the inventory limits as-is
    monkeypatch.setattr(deployer, "probe_host_resources", lambda runner, host: {})

    rc = cli.main([
        "deploy", str(TOPOLOGIES / "large_campus.clab.yml"),
        "--cluster", str(TOPOLOGIES / "cluster.yaml"),
        "--dry-run", "--output-dir", str(tmp_path),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"dry_run": true' in out

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


def test_cli_single_host_dry_run(capsys):
    rc = cli.main(["deploy", str(TOPOLOGIES / "three_router_triangle.clab.yml"), "--dry-run"])
    assert rc == 0
    assert '"localhost": [' in capsys.readouterr().out
