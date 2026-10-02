import asyncio
import json
import textwrap
from pathlib import Path

import pytest

from clabfleet.cli import main
from clabfleet.routing import routing_view
from clabfleet.routing.graph import config_iface
from clabfleet.routing.parse import parse_config
from clabfleet.topology import load_topology, topology_from_dict

TOPOLOGIES = Path(__file__).resolve().parent.parent / "topologies"


def _topo(nodes: dict, links: list, kind: str = "arista_ceos"):
    """Topology from {name: config text} and [("a:eth1", "b:eth1"), ...]."""
    return topology_from_dict({
        "name": "t",
        "topology": {
            "nodes": {n: {"kind": kind, "startup-config": textwrap.dedent(c)} for n, c in nodes.items()},
            "links": [{"endpoints": list(pair)} for pair in links],
        },
    })


def _messages(view, protocol=None):
    return [p["message"] for p in view["problems"] if protocol in (None, p["protocol"])]


# --- parsing ------------------------------------------------------------------

def test_ios_wildcard_networks_pick_the_most_specific_area():
    cfg = parse_config(textwrap.dedent("""\
        interface Loopback0
         ip address 10.255.0.1 255.255.255.255
        interface Ethernet0/1
         ip address 10.1.0.1 255.255.255.0
        interface Ethernet0/2
         ip address 192.0.2.1 255.255.255.0
         ip ospf 1 area 7
        router ospf 1
         network 0.0.0.0 255.255.255.255 area 0
         network 10.1.0.0 0.0.0.255 area 1
        """))
    proc = cfg.ospf[0]
    assert proc.area_for(cfg.interface("Loopback0").primary.ip) == 0
    assert proc.area_for(cfg.interface("Et0/1").primary.ip) == 1
    assert cfg.interface("Ethernet0/2").ospf_area == 7
    assert cfg.derived_router_id() == "10.255.0.1"


def test_bgp_peer_groups_and_address_families():
    cfg = parse_config(textwrap.dedent("""\
        router bgp 65001
           no bgp default ipv4-unicast
           neighbor UNDERLAY peer group
           neighbor UNDERLAY remote-as 65000
           neighbor EVPN peer group
           neighbor EVPN update-source Loopback0
           neighbor 10.0.0.0 peer group UNDERLAY
           neighbor 10.255.0.1 peer group EVPN
           neighbor 10.255.0.1 remote-as 65000
           address-family evpn
              neighbor EVPN activate
           address-family ipv4
              neighbor UNDERLAY activate
              network 10.255.1.1/32
        """))
    bgp = cfg.bgp
    peers = {nb.key: nb for nb in bgp.peers()}
    assert set(peers) == {"10.0.0.0", "10.255.0.1"}
    assert bgp.families(peers["10.0.0.0"]) == ["ipv4"]
    assert bgp.families(peers["10.255.0.1"]) == ["evpn"]
    assert bgp.resolved(peers["10.0.0.0"], "remote_as") == "65000"
    assert bgp.resolved(peers["10.255.0.1"], "update_source") == "Loopback0"
    assert bgp.networks == ["10.255.1.1/32"]


def test_nxos_nve_vnis_and_l3_vni():
    cfg = parse_config(textwrap.dedent("""\
        vlan 10
          vn-segment 10010
        vrf context TENANT
          vni 50001
        interface loopback1
          ip address 10.255.2.1/32
        interface nve1
          host-reachability protocol bgp
          source-interface loopback1
          member vni 10010
            ingress-replication protocol bgp
          member vni 50001 associate-vrf
        """))
    v = cfg.vtep
    assert v.source_interface == "Loopback1"
    assert v.l2_vnis == {10010: 10}
    assert v.l3_vnis == {50001: "TENANT"}


def test_config_iface_maps_endpoints_to_os_names():
    assert config_iface("arista_ceos", "eth1") == "Ethernet1"
    assert config_iface("arista_ceos", "Ethernet3") == "Ethernet3"   # interface alias
    assert config_iface("cisco_iol", "Ethernet0/1") == "Ethernet0/1"
    assert config_iface("nokia_srlinux", "e1-2") == "ethernet-1/2"


# --- the repository's example labs ---------------------------------------------

def test_triangle_ospf_adjacencies_follow_the_links():
    view = routing_view(load_topology(TOPOLOGIES / "three_router_triangle.clab.yml"))
    assert view["protocols"] == ["ospf"]
    adj = view["ospf"]["adjacencies"]
    assert len(adj) == 3 and all(a["link"] and a["area"] == "0" for a in adj)
    assert view["ospf"]["nodes"]["R1"]["router_id"] == "1.1.1.1"
    assert not view["ospf"]["nodes"]["R1"]["router_id_configured"]
    assert view["problems"] == []


def test_spine_leaf_bgp_sessions_are_paired():
    view = routing_view(load_topology(TOPOLOGIES / "spine_leaf.clab.yml"))
    sessions = view["bgp"]["sessions"]
    assert len(sessions) == 8
    assert all(s["configured"] == "both" and s["type"] == "ebgp" and s["link"] for s in sessions)
    assert view["problems"] == []


def test_evpn_fabric_overlay():
    view = routing_view(load_topology(TOPOLOGIES / "evpn_fabric.clab.yml"))
    assert view["protocols"] == ["bgp", "evpn"]
    evpn = view["evpn"]
    assert sorted(evpn["vteps"]) == ["Leaf-1", "Leaf-2", "Leaf-3", "Leaf-4"]
    assert evpn["vteps"]["Leaf-1"]["ip"] == "10.255.2.1"
    vnis = {v["vni"]: v for v in evpn["vnis"]}
    assert vnis[10010]["members"] == ["Leaf-1", "Leaf-3"] and vnis[10010]["vlans"] == [10]
    assert vnis[50001]["type"] == "l3" and len(vnis[50001]["members"]) == 4
    assert len(evpn["tunnels"]) == 6
    assert len(evpn["sessions"]) == 8                       # every leaf to both spines
    overlay = [s for s in view["bgp"]["sessions"] if s["id"] in evpn["sessions"]]
    assert all(s["multihop"] and s["link"] is None for s in overlay)
    assert view["problems"] == []                           # anycast gateways are not duplicates


def test_campus_flags_ospf_towards_routers_without_ospf():
    view = routing_view(load_topology(TOPOLOGIES / "large_campus.clab.yml"))
    notes = [p for p in view["problems"] if p["protocol"] == "ospf"]
    assert len(notes) == 4 and all(p["severity"] == "info" for p in notes)


# --- problems -------------------------------------------------------------------

R1 = textwrap.dedent("""\
    interface Ethernet1
       no switchport
       ip address 10.0.0.0/31
    interface Loopback0
       ip address 10.255.0.1/32
    """)
R2 = textwrap.dedent("""\
    interface Ethernet1
       no switchport
       ip address 10.0.0.1/31
    interface Loopback0
       ip address 10.255.0.2/32
    """)


def test_one_sided_session_and_remote_as_mismatch():
    view = routing_view(_topo({
        "r1": R1 + "router bgp 65001\n   neighbor 10.0.0.1 remote-as 65009\n",
        "r2": R2 + "router bgp 65002\n",
    }, [("r1:eth1", "r2:eth1")]))
    s = view["bgp"]["sessions"][0]
    assert s["configured"] == "one-sided"
    msgs = " | ".join(s["problems"])
    assert "no matching neighbor" in msgs and "expects AS 65009" in msgs


def test_loopback_peering_needs_update_source_and_multihop():
    view = routing_view(_topo({
        "r1": R1 + "router bgp 65001\n   neighbor 10.255.0.2 remote-as 65002\n",
        "r2": R2 + "router bgp 65002\n   neighbor 10.255.0.1 remote-as 65001\n"
                   "   neighbor 10.255.0.1 update-source Loopback0\n"
                   "   neighbor 10.255.0.1 ebgp-multihop 2\n",
    }, [("r1:eth1", "r2:eth1")]))
    msgs = _messages(view, "bgp")
    assert any("r1 must source the session from Loopback0" in m for m in msgs)
    assert any("r1: eBGP to 10.255.0.2 is not directly connected" in m for m in msgs)
    assert not any(m.startswith("r2") for m in msgs)


def test_peer_outside_the_lab_is_external():
    view = routing_view(_topo({"r1": R1 + "router bgp 65001\n   neighbor 198.51.100.1 remote-as 64999\n"}, []))
    s = view["bgp"]["sessions"][0]
    assert s["external"] and s["b"]["node"] == "ext:198.51.100.1"
    assert view["bgp"]["external"] == {"ext:198.51.100.1": {"ip": "198.51.100.1", "asn": "64999"}}


def test_ospf_area_and_network_type_mismatch():
    view = routing_view(_topo({
        "r1": R1.replace("10.0.0.0/31", "10.0.0.0/31\n   ip ospf area 0\n   ip ospf network point-to-point"),
        "r2": R2.replace("10.0.0.1/31", "10.0.0.1/31\n   ip ospf area 1"),
    }, [("r1:eth1", "r2:eth1")]))
    adj = view["ospf"]["adjacencies"][0]
    assert adj["area"] is None and adj["link"] == "link0"
    msgs = " | ".join(adj["problems"])
    assert "area mismatch" in msgs and "network type differs" in msgs


def test_ospf_on_one_side_and_subnet_mismatch():
    view = routing_view(_topo({
        "r1": R1 + "router ospf 1\n   network 10.0.0.0/8 area 0\n",
        "r2": R2 + "router ospf 1\n   network 10.255.0.0/16 area 0\n",
        "r3": R1.replace("10.0.0.0/31", "10.0.9.0/31") + "router ospf 1\n   network 10.0.0.0/8 area 0\n",
        "r4": R2.replace("10.0.0.1/31", "10.0.8.1/31") + "router ospf 1\n   network 10.0.0.0/8 area 0\n",
    }, [("r1:eth1", "r2:eth1"), ("r3:eth1", "r4:eth1")]))
    msgs = _messages(view, "ospf")
    assert "r1 Ethernet1 runs OSPF but r2 Ethernet1 does not" in msgs
    assert any("are not in the same subnet" in m and m.startswith("r3") for m in msgs)
    assert view["ospf"]["adjacencies"] == []


def test_duplicate_address_and_evpn_problems():
    vtep = "interface Vxlan1\n   vxlan source-interface Loopback0\n   vxlan vlan 10 vni 10010\n"
    view = routing_view(_topo({"r1": R1 + vtep, "r2": R1}, []))
    msgs = _messages(view)
    assert "10.0.0.0 is configured on r1 Ethernet1, r2 Ethernet1" in msgs
    assert "r1 is a VTEP but has no BGP EVPN session or flood list" in msgs
    assert "VNI 10010 is only configured on r1" in msgs


def test_startup_config_files_are_read_inside_the_topology_dir_only(tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "r1.cfg").write_text(textwrap.dedent(R1) + "router ospf 1\n network 10.0.0.0/8 area 0\n")
    (tmp_path / "secret.cfg").write_text("interface Loopback9\n ip address 1.2.3.4/32\n")
    lab = tmp_path / "lab"
    lab.mkdir()
    (lab / "configs").symlink_to(tmp_path / "configs")
    (lab / "t.clab.yml").write_text(textwrap.dedent("""\
        name: t
        topology:
          nodes:
            r1: {kind: arista_ceos, startup-config: configs/r1.cfg}
            r2: {kind: arista_ceos, startup-config: ../secret.cfg}
            r3: {kind: arista_ceos, startup-config: missing.cfg}
            h1: {kind: linux}
        """))
    view = routing_view(load_topology(lab / "t.clab.yml"))
    reasons = {u["node"]: u["reason"] for u in view["unparsed"]}
    assert set(reasons) == {"r1", "r2", "r3", "h1"}           # the symlink leaves the dir too
    assert "outside the topology directory" in reasons["r2"]
    assert "cannot read" in reasons["r3"]
    assert reasons["h1"] == "no startup-config"

    (lab / "configs").unlink()
    (lab / "configs").mkdir()
    (lab / "configs" / "r1.cfg").write_text((tmp_path / "configs" / "r1.cfg").read_text())
    view = routing_view(load_topology(lab / "t.clab.yml"))
    assert view["ospf"]["nodes"]["r1"]["areas"] == ["0"]


def test_non_ios_configs_are_reported_not_parsed():
    topo = topology_from_dict({"name": "t", "topology": {"nodes": {
        "j": {"kind": "juniper_crpd", "startup-config": "set system host-name j\nset interfaces lo0\n"}}}})
    view = routing_view(topo)
    assert view["protocols"] == []
    assert "not supported" in view["unparsed"][0]["reason"]


# --- CLI and GUI ---------------------------------------------------------------

def test_cli_routing_report(capsys):
    assert main(["routing", str(TOPOLOGIES / "evpn_fabric.clab.yml"), "--protocol", "evpn"]) == 0
    out = capsys.readouterr().out
    assert "VTEP Leaf-1" in out and "10010    L2 VLAN 10" in out
    assert "OSPF" not in out and "not parsed" not in out

    assert main(["routing", str(TOPOLOGIES / "three_router_triangle.clab.yml"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["protocols"] == ["ospf"]


def test_gui_routing_endpoint(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp")
    from aiohttp.test_utils import TestClient, TestServer

    from clabfleet.cluster import ClusterConfig, HostInfo
    from clabfleet.gui import server
    from clabfleet.gui.state import Workspace

    (tmp_path / "tri.clab.yml").write_text((TOPOLOGIES / "three_router_triangle.clab.yml").read_text())
    (tmp_path / "bad.clab.yml").write_text("name: [oops\n")
    ws = Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])
    monkeypatch.setattr(Workspace, "runtime", lambda self, *a, **k: [])

    async def scenario():
        app = server.create_app(ws, "tok")
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/api/routing/tri.clab.yml")).status == 401
            await client.post("/login", json={"token": "tok"})
            view = await (await client.get("/api/routing/tri.clab.yml")).json()
            assert view["protocols"] == ["ospf"] and len(view["ospf"]["adjacencies"]) == 3
            assert "error" in await (await client.get("/api/routing/bad.clab.yml")).json()
            assert (await client.get("/api/routing/missing.clab.yml")).status == 404
            assert (await client.get("/static/routing.js")).status == 200

    asyncio.run(scenario())
