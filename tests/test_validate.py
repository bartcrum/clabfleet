from pathlib import Path

import yaml

from clabfleet import cli
from clabfleet.cluster import ClusterConfig, HostInfo
from clabfleet.validate import validate_topology

TOPOLOGIES = Path(__file__).parent.parent / "topologies"


def _write(tmp_path, data, name="t.clab.yml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data))
    return path


def _topo(nodes, links=()):
    return {"name": "t", "topology": {"nodes": nodes, "links": list(links)}}


def test_examples_are_clean():
    for path in sorted(TOPOLOGIES.glob("*.clab.yml")):
        report = validate_topology(path)
        assert report.ok and not report.warnings, (path, report.errors, report.warnings)


def test_load_errors_are_reported_not_raised(tmp_path):
    report = validate_topology(_write(tmp_path, _topo({"a": {"kind": "linux"}},
                                                      [{"endpoints": ["a:eth1", "zz:eth1"]}])))
    assert not report.ok
    assert "unknown node 'zz'" in report.errors[0]
    assert report.summary() == "invalid"

    report = validate_topology(tmp_path / "missing.clab.yml")
    assert "not found" in report.errors[0]

    bad = tmp_path / "veth.clab.yml"
    bad.write_text(yaml.safe_dump(_topo({"a": {"kind": "linux"}},
                                        [{"type": "veth", "endpoints": [{"interface": "e1"}, {}]}])))
    assert "missing key 'node'" in validate_topology(bad).errors[0]


def test_missing_and_outside_files(tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "a.cfg").write_text("hostname a")
    report = validate_topology(_write(tmp_path, _topo({
        "a": {"kind": "linux", "startup-config": "configs/a.cfg"},
        "b": {"kind": "linux", "startup-config": "configs/b.cfg",
              "binds": ["data/b:/data", "/abs:/abs", "../shared:/shared"]},
        "c": {"kind": "linux", "startup-config": "hostname c\ninterface eth1\n"},
    })))
    assert report.errors == ["node 'b' startup-config 'configs/b.cfg' does not exist"]
    assert report.warnings == [
        "node 'b' binds 'data/b' does not exist",
        "node 'b' binds '../shared' is outside the topology directory and is not copied "
        "to remote hosts",
    ]


def test_symlink_out_of_the_folder_is_reported(tmp_path):
    (tmp_path / "secret").write_text("x")
    lab = tmp_path / "lab"
    lab.mkdir()
    (lab / "a.cfg").symlink_to(tmp_path / "secret")
    report = validate_topology(_write(lab, _topo({
        "a": {"kind": "linux", "startup-config": "a.cfg"}})))
    assert report.errors == []
    assert report.warnings == [
        "node 'a' startup-config 'a.cfg' is a symlink to a file outside the topology "
        "directory and is not copied to remote hosts",
    ]


def test_unknown_kind_and_duplicate_interface(tmp_path):
    report = validate_topology(_write(tmp_path, _topo(
        {"a": {"kind": "linux"}, "b": {"kind": "acme_os"}, "c": {"kind": "acme_os"}},
        [{"endpoints": ["a:eth1", "b:eth1"]},
         {"endpoints": ["a:eth1", "c:eth1"]},
         {"endpoints": ["c:eth2", "host:c-eth2"]}],
    )))
    assert report.errors == ["interface a:eth1 is used by link #0 and link #1"]
    assert len(report.warnings) == 1
    assert "kind 'acme_os' (b, c) is not known" in report.warnings[0]
    assert report.summary() == "1 error, 1 warning (3 nodes, 3 links)"


def test_cluster_label_checks(tmp_path):
    path = _write(tmp_path, _topo({
        "a": {"kind": "linux", "labels": {"lab.host": "h9"}},
        "b": {"kind": "linux", "labels": {"lab.host-tags": "edge,dmz"}},
        "c": {"kind": "linux", "labels": {"lab.host-tags": "core"}},
    }))
    cluster = ClusterConfig(hosts=[HostInfo("h1", "10.0.0.1", tags=["core"]),
                                   HostInfo("me", "localhost")])
    report = validate_topology(path, cluster)
    assert report.errors == [
        "node 'a' is pinned (lab.host) to 'h9', which is not in the cluster (h1, me)"
    ]
    assert report.warnings == [
        "node 'b' prefers host tags edge, dmz, but no cluster host has any of them "
        "(placement falls back to all hosts)",
        "cluster host 'me' has no vtep_ip: links between it and other hosts cannot be created",
    ]
    # Without a cluster, placement labels are not checked
    assert validate_topology(path).ok


def test_cli_validate_exit_codes(tmp_path, capsys):
    good = _write(tmp_path, _topo({"a": {"kind": "linux"}}), "good.clab.yml")
    warn = _write(tmp_path, _topo({"a": {"kind": "acme_os"}}), "warn.clab.yml")
    bad = _write(tmp_path, _topo({"a": {"kind": "linux", "license": "lic.txt"}}), "bad.clab.yml")

    assert cli.main(["validate", str(good), str(warn)]) == 0
    assert cli.main(["validate", "--strict", str(good), str(warn)]) == 1
    assert cli.main(["validate", str(good), str(bad)]) == 1
    out = capsys.readouterr().out
    assert f"{good}: ok (1 node, 0 links)" in out
    assert "  warning: kind 'acme_os'" in out
    assert "  error: node 'a' license 'lic.txt' does not exist" in out
