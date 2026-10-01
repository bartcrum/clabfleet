from pathlib import Path

import pytest

from clabfleet.topology import (
    TopologyError,
    load_topology,
    parse_memory_mb,
    topology_from_dict,
)

TOPOLOGIES = Path(__file__).parent.parent / "topologies"


@pytest.mark.parametrize("name", [
    "three_router_triangle.clab.yml",
    "spine_leaf.clab.yml",
    "large_campus.clab.yml",
])
def test_example_topologies_load(name):
    topo = load_topology(TOPOLOGIES / name)
    assert topo.nodes
    assert all(link.is_p2p for link in topo.links)


def test_inheritance_defaults_kinds_groups():
    topo = topology_from_dict({
        "name": "t",
        "topology": {
            "defaults": {"kind": "linux", "labels": {"a": "1"}},
            "kinds": {"nokia_srlinux": {"image": "srl", "labels": {"b": "2"}}},
            "groups": {"spines": {"kind": "nokia_srlinux", "labels": {"lab.host": "h1"}}},
            "nodes": {
                "s1": {"group": "spines", "labels": {"c": "3"}},
                "c1": {},
            },
        },
    })
    s1 = topo.effective_node("s1")
    assert s1["kind"] == "nokia_srlinux"
    assert s1["image"] == "srl"
    assert s1["labels"] == {"a": "1", "b": "2", "lab.host": "h1", "c": "3"}
    assert topo.effective_node("c1")["kind"] == "linux"
    placement = {n["name"]: n for n in topo.placement_nodes()}
    assert placement["s1"]["host"] == "h1"
    assert placement["s1"]["ram"] == 2048  # srlinux estimate


def test_resources_from_labels_and_limits():
    topo = topology_from_dict({
        "name": "t",
        "topology": {"nodes": {
            "a": {"kind": "linux", "labels": {"lab.cpu": "3", "lab.ram": "1000"}},
            "b": {"kind": "linux", "cpu": 2, "memory": "1Gb"},
            "c": {"kind": "something_new"},
        }},
    })
    assert topo.node_resources("a") == (3.0, 1000)
    assert topo.node_resources("b") == (2.0, 1024)
    assert topo.node_resources("c") == (1.0, 512)


@pytest.mark.parametrize("value,expected", [
    ("1Gb", 1024), ("512MB", 512), ("2g", 2048), ("1.5GiB", 1536), (1073741824, 1024),
])
def test_parse_memory(value, expected):
    assert parse_memory_mb(value) == expected


def test_link_formats():
    topo = topology_from_dict({
        "name": "t",
        "topology": {
            "nodes": {"a": {"kind": "linux"}, "b": {"kind": "linux"}},
            "links": [
                {"endpoints": ["a:eth1", "b:eth1"], "ipv4": ["10.0.0.1/30", "10.0.0.2/30"]},
                {"endpoints": ["a:eth2", "host:a-eth2"]},
                {"endpoints": ["macvlan:enp0s3", "b:eth2"]},
                {"type": "veth", "endpoints": [
                    {"node": "a", "interface": "eth3", "mac": "aa:c1:ab:00:00:01"},
                    {"node": "b", "interface": "eth3"},
                ]},
                {"type": "dummy", "endpoint": {"node": "a", "interface": "eth4"}},
            ],
        },
    })
    assert [l.node_names for l in topo.links] == [["a", "b"], ["a"], ["b"], ["a", "b"], ["a"]]
    assert topo.links[0].endpoints[1].extra == {"ipv4": "10.0.0.2/30"}
    assert topo.links[3].endpoints[0].extra == {"mac": "aa:c1:ab:00:00:01"}


@pytest.mark.parametrize("data,message", [
    ({"topology": {"nodes": {"a": {"kind": "linux"}}}}, "name"),
    ({"name": "../x", "topology": {"nodes": {"a": {"kind": "linux"}}}}, "Invalid lab name"),
    ({"name": "t", "topology": {"nodes": {"a": {}}}}, "no kind"),
    ({"name": "t", "topology": {"nodes": {"a": {"kind": "linux"}},
                                "links": [{"endpoints": ["a:e1", "zz:e1"]}]}}, "unknown node"),
    ({"name": "t", "topology": {"nodes": {"a": {"kind": "linux", "group": "g"}}}}, "unknown group"),
])
def test_validation_errors(data, message):
    with pytest.raises(TopologyError, match=message):
        topology_from_dict(data)


def test_referenced_files(tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "r1.cfg").write_text("hostname r1")
    (tmp_path / "configs" / "r2.cfg").write_text("hostname r2")
    (tmp_path / "lic.key").write_text("x")
    (tmp_path / "data").mkdir()
    topo = topology_from_dict({
        "name": "t",
        "topology": {
            "defaults": {"license": "lic.key"},
            "kinds": {"linux": {"startup-config": "configs/__clabNodeName__.cfg"}},
            "nodes": {
                "r1": {"kind": "linux", "binds": ["./data:/data", "__clabDir__/x:/x", "/abs:/abs"]},
                "r2": {"kind": "linux"},
                "r3": {"kind": "linux", "startup-config": "hostname r3\nend\n"},
                "r4": {"kind": "linux", "startup-config": "https://example.com/r4.cfg"},
            },
        },
    }, base_dir=tmp_path)
    assert sorted(map(str, topo.referenced_files())) == [
        "configs/r1.cfg", "configs/r2.cfg", "data", "lic.key",
    ]
