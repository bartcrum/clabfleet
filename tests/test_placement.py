import logging

import pytest

from clabfleet.cluster import HostInfo
from clabfleet.placement import PlacementError, compute_placement
from clabfleet.topology import topology_from_dict


def _hosts(*specs, tags=None):
    """HostInfo objects from (name, max_cpu, max_ram) tuples."""
    tags = tags or {}
    return [HostInfo(name, f"10.0.0.{i + 1}", max_cpu=cpu, max_ram=ram, tags=tags.get(name, []))
            for i, (name, cpu, ram) in enumerate(specs)]


def _node(name, cpu=1, ram=512, host=None, tags=()):
    return {"name": name, "cpu": cpu, "ram": ram, "host": host, "host_tags": list(tags)}


def _chain(*names):
    """Link memberships for a chain a-b-c-... (one network per link)."""
    links = []
    for i, (a, b) in enumerate(zip(names, names[1:])):
        links += [{"node": a, "interface": "eth1", "network": f"l{i}"},
                  {"node": b, "interface": "eth2", "network": f"l{i}"}]
    return links


def _where(plan):
    return {p.node_name: p.host_name for p in plan.placements}


# --- pinning ---------------------------------------------------------------

def test_pinned_node_goes_to_its_host_even_against_strategy():
    hosts = _hosts(("h1", 4, 4096), ("h2", 4, 4096))
    plan = compute_placement([_node("a"), _node("b", host="h2")], _chain("a", "b"), hosts)
    assert _where(plan) == {"b": "h2", "a": "h2"}  # a follows its neighbour
    assert [p.node_name for p in plan.placements] == ["b", "a"]  # pinned first


def test_pinned_to_unknown_host_fails():
    with pytest.raises(PlacementError, match="unknown host 'nope'"):
        compute_placement([_node("a", host="nope")], [], _hosts(("h1", 4, 4096)))


def test_pinned_host_without_room_fails():
    with pytest.raises(PlacementError, match="lacks resources"):
        compute_placement([_node("a", cpu=8, host="h1")], [], _hosts(("h1", 4, 4096)))


# --- tag affinity ----------------------------------------------------------

def test_tag_affinity_prefers_matching_host():
    hosts = _hosts(("h1", 8, 8192), ("h2", 8, 8192), tags={"h2": ["access"]})
    plan = compute_placement([_node("a", tags=["core", "access"])], [], hosts)
    assert _where(plan) == {"a": "h2"}


def test_unmatched_tags_fall_back_to_all_hosts(caplog):
    hosts = _hosts(("h1", 8, 8192), tags={"h1": ["core"]})
    with caplog.at_level(logging.WARNING, logger="clabfleet.placement"):
        plan = compute_placement([_node("a", tags=["edge"])], [], hosts)
    assert _where(plan) == {"a": "h1"}
    assert "No hosts match tags" in caplog.text


def test_tag_match_without_room_does_not_spill_to_other_hosts():
    hosts = _hosts(("h1", 8, 8192), ("h2", 1, 8192), tags={"h2": ["access"]})
    with pytest.raises(PlacementError, match="No host has enough resources for node 'a'"):
        compute_placement([_node("a", cpu=2, tags=["access"])], [], hosts)


# --- resources -------------------------------------------------------------

def test_no_host_with_room_fails():
    with pytest.raises(PlacementError, match=r"need 1CPU/9000MB"):
        compute_placement([_node("a", ram=9000)], [], _hosts(("h1", 4, 4096), ("h2", 4, 8192)))


def test_placement_reserves_resources_on_hosts():
    hosts = _hosts(("h1", 4, 4096))
    compute_placement([_node("a", cpu=1.5, ram=1000), _node("b", cpu=0.5, ram=24)], [], hosts)
    assert hosts[0].used_cpu == 2.0
    assert hosts[0].used_ram == 1024


def test_hosts_without_limits_are_unbounded():
    hosts = [HostInfo("h1", "10.0.0.1")]  # max_cpu/max_ram 0 = not probed/unlimited
    plan = compute_placement([_node(f"n{i}", cpu=4, ram=8192) for i in range(10)], [], hosts)
    assert set(_where(plan).values()) == {"h1"}


def test_no_hosts_fails():
    with pytest.raises(PlacementError, match="No hosts"):
        compute_placement([_node("a")], [], [])


# --- strategies ------------------------------------------------------------

def test_bin_pack_keeps_a_chain_on_one_host():
    hosts = _hosts(("h1", 4, 4096), ("h2", 8, 8192))
    names = ["a", "b", "c", "d"]
    plan = compute_placement([_node(n) for n in names], _chain(*names), hosts)
    assert set(_where(plan).values()) == {"h1"}  # tightest host that fits
    assert plan.cross_host_links == []


def test_bin_pack_overflows_to_next_host_and_reports_cross_links():
    hosts = _hosts(("h1", 2, 4096), ("h2", 8, 8192))
    names = ["a", "b", "c"]
    plan = compute_placement([_node(n) for n in names], _chain(*names), hosts)
    assert _where(plan) == {"a": "h1", "b": "h1", "c": "h2"}
    assert [link["network"] for link in plan.cross_host_links] == ["l1"]
    assert sorted(plan.cross_host_links[0]["nodes"]) == ["b", "c"]
    assert sorted(plan.cross_host_links[0]["hosts"]) == ["h1", "h2"]


def test_spread_balances_node_counts():
    hosts = _hosts(("h1", 8, 8192), ("h2", 8, 8192))
    names = ["a", "b", "c", "d"]
    plan = compute_placement([_node(n) for n in names], _chain(*names), hosts, "spread")
    counts = {}
    for host in _where(plan).values():
        counts[host] = counts.get(host, 0) + 1
    assert counts == {"h1": 2, "h2": 2}


def test_resource_picks_host_with_most_room():
    hosts = _hosts(("h1", 4, 4096), ("h2", 8, 8192))
    plan = compute_placement([_node("a")], [], hosts, "resource")
    assert _where(plan) == {"a": "h2"}


def test_biggest_nodes_are_placed_first():
    hosts = _hosts(("h1", 8, 8192))
    plan = compute_placement([_node("small"), _node("big", cpu=4, ram=4096)], [], hosts)
    assert [p.node_name for p in plan.placements] == ["big", "small"]


# --- with a real topology --------------------------------------------------

def test_topology_labels_drive_placement():
    topo = topology_from_dict({
        "name": "t",
        "topology": {
            "groups": {"access": {"kind": "linux", "labels": {"lab.host-tags": "access"}}},
            "nodes": {
                "core": {"kind": "arista_ceos", "labels": {"lab.host": "h1"}},
                "acc1": {"group": "access"},
                "acc2": {"group": "access"},
            },
            "links": [{"endpoints": ["core:eth1", "acc1:eth1"]},
                      {"endpoints": ["core:eth2", "acc2:eth1"]}],
        },
    })
    hosts = _hosts(("h1", 8, 8192), ("h2", 8, 8192), tags={"h2": ["access"]})
    plan = compute_placement(topo.placement_nodes(), topo.link_memberships(), hosts)
    assert _where(plan) == {"core": "h1", "acc1": "h2", "acc2": "h2"}
    assert len(plan.cross_host_links) == 2
    assert hosts[0].used_ram == 2048  # cEOS estimate
