import json

import pytest

from clabfleet.routing.evpn_routes import parse_eos_evpn_routes
from clabfleet.routing.trace import Lab, TraceError, same_iface, trace

# H1 - L1 = S1/S2 = L2 - H2, and H3 on L2 (the spine-leaf EVPN lab, small)
LINKS = [("H1", "eth1", "L1", "eth3"), ("L1", "eth1", "S1", "eth1"), ("L1", "eth2", "S2", "eth1"),
         ("S1", "eth2", "L2", "eth1"), ("S2", "eth2", "L2", "eth2"), ("L2", "eth3", "H2", "eth1"),
         ("L2", "eth4", "H3", "eth1")]
KINDS = {"H1": "linux", "H2": "linux", "H3": "linux", "L1": "arista_ceos", "L2": "arista_ceos",
         "S1": "arista_ceos", "S2": "arista_ceos"}
VTEPS = {"10.255.2.1": "L1", "10.255.2.2": "L2"}


def lab():
    view = {"links": [{"a": {"node": a, "iface": ai}, "b": {"node": b, "iface": bi}} for a, ai, b, bi in LINKS]}
    return Lab(view, KINDS, {ip: [n] for ip, n in VTEPS.items()})


def route(vrf, prefix, kind, vias, connected=False):
    return json.dumps({"vrfs": {vrf: {"routes": {prefix: {"routeType": kind, "vias": vias, "directlyConnected": connected}}}}})


EMPTY_MAC = json.dumps({"unicastTable": {"tableEntries": []}})


def asker(answers):
    asked = []

    def ask(node, command):
        asked.append((node, command))
        for (n, prefix), out in answers.items():
            if n == node and command.startswith(prefix):
                return out
        raise RuntimeError(f"unexpected {node}: {command}")
    ask.asked = asked
    return ask


def test_same_iface():
    assert same_iface("arista_ceos", "eth3", "Ethernet3")
    assert same_iface("cisco_iol", "Ethernet0/1", "eth1")
    assert not same_iface("arista_ceos", "eth3", "Ethernet4")


def test_underlay_ecmp_to_an_address():
    ask = asker({
        ("L1", "show ip route"): route("default", "10.255.1.2/32", "eBGP",
                                       [{"interface": "Ethernet1"}, {"interface": "Ethernet2"}]),
        ("S1", "show ip route"): route("default", "10.255.1.2/32", "eBGP", [{"interface": "Ethernet2"}]),
        ("S2", "show ip route"): route("default", "10.255.1.2/32", "eBGP", [{"interface": "Ethernet2"}]),
        ("L2", "show ip route"): route("default", "10.255.1.2/32", "connected", [{"interface": "Loopback0"}], True),
    })
    r = trace(lab(), ask, "L1", "10.255.1.2")
    assert r["reached"] and [h["node"] for h in r["hops"]] == ["L1", "S1", "S2", "L2"]
    pairs = [(e["a"], e["b"], e["a_iface"]) for e in r["edges"]]
    # Both spines, and L2 once each from them (no duplicates)
    assert pairs == [("L1", "S1", "eth1"), ("L1", "S2", "eth2"), ("S1", "L2", "eth2"), ("S2", "L2", "eth2")]


def test_routed_over_vxlan_to_a_host():
    ask = asker({
        ("H1", "ip route get"): "10.20.20.12 via 10.10.10.1 dev eth1  src 10.10.10.11 \n",
        ("L1", "show ip route"): route("TENANT", "10.20.20.12/32", "eBGP",
                                       [{"vtepAddr": "10.255.2.2", "vni": 50001, "localInterface": "Vxlan1"}]),
        ("L2", "show ip route"): route("TENANT", "10.20.20.0/24", "connected", [{"interface": "Vlan20"}], True),
        ("L2", "show ip arp"): json.dumps({"ipV4Neighbors": [{"hwAddress": "aac1.abab.df07", "interface": "Vlan20, Ethernet3"}]}),
    })
    r = trace(lab(), ask, "H1", "10.20.20.12", "H2")
    assert r["reached"]
    assert [h["node"] for h in r["hops"]] == ["H1", "L1", "L2", "H2"]
    assert r["hops"][1]["vrf"] == "TENANT" and "(VRF TENANT)" in r["hops"][1]["route"]
    overlay = [e for e in r["edges"] if e["overlay"]]
    assert overlay == [{"a": "L1", "b": "L2", "a_iface": "", "b_iface": "", "overlay": True, "vni": 50001, "flood": False}]
    assert r["edges"][-1]["a_iface"] == "eth3"  # Ethernet3 is the topology's eth3
    # L2 looked the address up in L1's VRF
    assert ("L2", "show ip route vrf all 10.20.20.12 | json") in ask.asked


def test_bridged_with_aged_out_mac_floods_and_finds_the_host():
    ask = asker({
        ("H1", "ip route get"): "10.10.10.13 dev eth1  src 10.10.10.11 \n",
        ("H1", "ip neigh show"): "10.10.10.13 dev eth1 lladdr aa:c1:ab:8f:40:85 STALE\n",
        ("L1", "show ip route"): route("TENANT", "10.10.10.0/24", "connected", [{"interface": "Vlan10"}], True),
        ("L1", "show ip arp"): json.dumps({"ipV4Neighbors": []}),
        ("L1", "show mac address-table"): EMPTY_MAC,
        ("L1", "show vxlan address-table"): json.dumps({"addresses": []}),
        ("L1", "show bgp evpn route-type mac-ip"): json.dumps({"evpnRoutes": {}}),
        ("L1", "show vxlan flood vtep vlan 10"): json.dumps({"floodMap": {"10": {"vteps": ["10.255.2.2"]}}}),
        ("L2", "show mac address-table"): EMPTY_MAC,
    })
    r = trace(lab(), ask, "H1", "10.10.10.13", "H3")
    assert r["reached"]
    assert ("L1", "show mac address-table address aac1.ab8f.4085 | json") in ask.asked  # the host's MAC, EOS style
    flood = [e for e in r["edges"] if e["overlay"]]
    assert flood and flood[0]["flood"] and flood[0]["b"] == "L2"
    assert r["edges"][-1] == {"a": "L2", "b": "H3", "a_iface": "eth4", "b_iface": "eth1", "overlay": False,
                              "vni": None, "flood": False}
    assert "not learned" in r["hops"][2]["notes"][0]


def test_bridged_mac_found_by_its_evpn_route():
    ask = asker({
        ("H1", "ip route get"): "10.10.10.13 dev eth1  src 10.10.10.11 \n",
        ("H1", "ip neigh show"): "",
        ("L1", "show ip route"): route("TENANT", "10.10.10.0/24", "connected", [{"interface": "Vlan10"}], True),
        ("L1", "show ip arp"): json.dumps({"ipV4Neighbors": []}),
        ("L1", "show bgp evpn route-type mac-ip"): json.dumps({"evpnRoutes": {
            "RD: 10.255.1.2:10010 mac-ip aac1.ab8f.4085 10.10.10.13": {"evpnRoutePaths": [{"nextHop": "10.255.2.2"}]},
            "RD: 10.255.1.2:10010 mac-ip aac1.ab8f.4085": {"evpnRoutePaths": [{"nextHop": "10.255.2.2"}]}}}),
        ("L2", "show mac address-table"): json.dumps({"unicastTable": {"tableEntries": [
            {"interface": "Ethernet4", "macAddress": "aa:c1:ab:8f:40:85"}]}}),
        ("H3", "ip route get"): "local 10.10.10.13 dev lo table local src 10.10.10.13 \n",
    })
    r = trace(lab(), ask, "H1", "10.10.10.13")  # an address: H3 says it is its own
    assert r["reached"] and [h["node"] for h in r["hops"]] == ["H1", "L1", "L2", "H3"]
    assert any(e["b"] == "H3" and e["a_iface"] == "eth4" for e in r["edges"])
    assert not any(e.get("flood") for e in r["edges"])


def test_dead_ends_and_errors():
    ask = asker({
        ("H1", "ip route get"): "8.8.8.8 via 10.10.10.1 dev eth1\n",
        ("L1", "show ip route"): json.dumps({"vrfs": {"default": {"routes": {}}}}),
    })
    r = trace(lab(), ask, "H1", "8.8.8.8")
    assert not r["reached"] and r["hops"][1]["route"] == "no route to 8.8.8.8"
    with pytest.raises(TraceError, match="not an IP"):
        trace(lab(), ask, "H1", "Leaf-9")

    def fails(node, command):
        raise RuntimeError("container is not running")
    r = trace(lab(), fails, "H1", "10.0.0.1")
    assert r["hops"] == [{"node": "H1", "vrf": None, "error": "could not ask: container is not running"}]
    weird = Lab({"links": []}, {"X": "nokia_srlinux"}, {})
    assert "cannot look up routes" in trace(weird, fails, "X", "10.0.0.1")["hops"][0]["error"]
    # A next hop out of an interface no link uses
    ask = asker({("L1", "show ip route"): route("default", "0.0.0.0/0", "static", [{"interface": "Ethernet9"}])})
    assert trace(lab(), ask, "L1", "9.9.9.9")["hops"][0]["notes"] == ["Ethernet9 leads out of the lab"]


def test_parse_eos_evpn_routes():
    mac_ip = json.dumps({"evpnRoutes": {
        "RD: 10.255.1.3:10010 mac-ip aac1.ab8f.4085": {"evpnRoutePaths": [
            {"nextHop": "10.255.2.2", "routeType": {"active": True}, "routeDetail": {"label": {"value": "10010"}}}]},
        "RD: 10.255.1.1:10010 mac-ip aac1.abb1.0eff 10.10.10.11": {"evpnRoutePaths": [
            {"nextHop": "", "routeType": {"active": True},
             "routeDetail": {"label": {"value": "10010"}, "l3Label": {"value": "50001"}}}]},
        "RD: x mac-ip aaaa.bbbb.cccc": {"evpnRoutePaths": []}}})
    prefix = json.dumps({"evpnRoutes": {
        "RD: 10.255.1.2:50001 ip-prefix 10.20.20.0/24": {"evpnRoutePaths": [
            {"nextHop": "10.255.2.9", "routeType": {"active": False}, "routeDetail": {"label": {"value": "50001"}}},
            {"nextHop": "10.255.2.2", "routeType": {"active": True}, "routeDetail": {"label": {"value": "50001"}}}]}}})
    rows = parse_eos_evpn_routes(mac_ip, prefix, VTEPS)
    assert rows == [
        {"type": "mac-ip", "vni": 10010, "l3_vni": 50001, "mac": "aac1.abb1.0eff", "ip": "10.10.10.11",
         "prefix": "", "vtep": "", "from": "", "local": True},
        {"type": "mac-ip", "vni": 10010, "l3_vni": None, "mac": "aac1.ab8f.4085", "ip": "", "prefix": "",
         "vtep": "10.255.2.2", "from": "L2", "local": False},
        {"type": "ip-prefix", "vni": 50001, "l3_vni": None, "mac": "", "ip": "", "prefix": "10.20.20.0/24",
         "vtep": "10.255.2.2", "from": "L2", "local": False},  # the active path, not the first
    ]
    assert parse_eos_evpn_routes("", "  ", {}) == []


def test_an_address_is_traced_to_the_host_that_has_it(tmp_path, monkeypatch):
    """With the owner known, the trace can go on past a switch that has no
    MAC entry for the host, as it does for a destination given by name."""
    from clabfleet.cluster import ClusterConfig, HostInfo
    from clabfleet.gui.state import Workspace
    from clabfleet.routing import trace as trace_mod

    (tmp_path / "t.clab.yml").write_text(
        "name: t\ntopology:\n  nodes:\n"
        "    L1: {kind: arista_ceos, image: a}\n"
        "    H1: {kind: linux, image: a}\n"
        "    H2: {kind: linux, image: a}\n"
        "    H3: {kind: linux, image: a}\n"
        "  links:\n    - endpoints: ['H1:eth1', 'L1:eth1']\n")
    ws = Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])
    asked = []

    def node_command(self, lab, node, command, shell=True):
        asked.append(node)
        if node == "H3":
            raise ValueError("H3 is not running")
        address = {"H1": "10.10.10.11", "H2": "10.10.10.12"}[node]
        return ("1: lo    inet 127.0.0.1/8 scope host lo\n"
                "2: eth0    inet 172.20.20.5/24 brd 172.20.20.255 scope global eth0\n"
                f"3: eth1    inet {address}/24 scope global eth1\n")

    seen = {}

    def fake_trace(lab, ask, src, dst, dst_node=None):
        seen.update(dst=dst, dst_node=dst_node)
        return {"reached": True, "hops": [], "edges": [], "truncated": False}

    monkeypatch.setattr(Workspace, "node_command", node_command)
    monkeypatch.setattr(trace_mod, "trace", fake_trace)

    assert ws.trace("t.clab.yml", "H1", "10.10.10.12")["dst_node"] == "H2"
    assert seen == {"dst": "10.10.10.12", "dst_node": "H2"} and "L1" not in asked  # hosts only

    # Nobody's address (a host that is not running is skipped), a management
    # address, and something that is no address: traced as given
    for dst in ("10.10.10.99", "172.20.20.5", "nonsense"):
        assert ws.trace("t.clab.yml", "H1", dst)["dst_node"] is None
        assert seen == {"dst": dst, "dst_node": None}

    # By name, as before: the host's own address, and nobody else is asked
    asked.clear()
    assert ws.trace("t.clab.yml", "H1", "H2")["dst"] == "10.10.10.12"
    assert seen == {"dst": "10.10.10.12", "dst_node": "H2"} and asked == ["H2"]


def test_trace_and_evpn_endpoints(tmp_path, monkeypatch):
    import asyncio

    pytest.importorskip("aiohttp")
    from aiohttp.test_utils import TestClient, TestServer

    from clabfleet.cluster import ClusterConfig, HostInfo
    from clabfleet.gui import server
    from clabfleet.gui.auth import UserStore
    from clabfleet.gui.state import Workspace

    (tmp_path / "t.clab.yml").write_text("name: t\ntopology:\n  nodes:\n    R1: {kind: linux, image: a}\n")
    ws = Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])
    users = UserStore(tmp_path / "users.yaml")
    viewer = users.add("vic", "viewer")

    def fake_trace(self, topo_id, src, dst):
        if dst == "bad":
            raise ValueError("'bad' is not an IP address")
        if dst == "boom":
            raise RuntimeError("docker is gone")
        return {"src": src, "dst": dst, "reached": True, "hops": [], "edges": []}

    def fake_routes(self, topo_id, node):
        if node != "R1":
            raise ValueError("EVPN routes can be read from cEOS nodes only")
        return [{"type": "mac-ip", "vni": 10}]

    monkeypatch.setattr(Workspace, "trace", fake_trace)
    monkeypatch.setattr(Workspace, "evpn_routes", fake_routes)

    async def scenario():
        async with TestClient(TestServer(server.create_app(ws, users=users))) as client:
            await client.post("/login", json={"token": viewer})  # read-only: viewers may
            resp = await client.post("/api/trace/t.clab.yml", json={"src": "R1", "dst": " 10.0.0.1 "})
            assert resp.status == 200 and (await resp.json())["dst"] == "10.0.0.1"
            for body, status in [({"src": "R1"}, 400), ({"src": "R1", "dst": "bad"}, 400),
                                 ({"src": 1, "dst": "x"}, 400), ({"src": "R1", "dst": "boom"}, 502)]:
                assert (await client.post("/api/trace/t.clab.yml", json=body)).status == status, body
            resp = await client.get("/api/evpn-routes/t.clab.yml?node=R1")
            assert resp.status == 200 and (await resp.json())["routes"] == [{"type": "mac-ip", "vni": 10}]
            assert (await client.get("/api/evpn-routes/t.clab.yml?node=X")).status == 400

    asyncio.run(scenario())


def test_ios_routes():
    view = {"links": [{"a": {"node": "R1", "iface": "Ethernet0/1"}, "b": {"node": "R2", "iface": "Ethernet0/1"}}]}
    ios_lab = Lab(view, {"R1": "cisco_iol", "R2": "cisco_iol"}, {})
    ask = asker({
        ("R1", "show ip route"): (
            "Routing entry for 10.0.0.2/32\n  Known via \"ospf 1\", distance 110, metric 11, type intra area\n"
            "  Routing Descriptor Blocks:\n  * 10.1.1.2, from 10.0.0.2, 00:05:12 ago, via Ethernet0/1\n"),
        ("R2", "show ip route"): (
            "Routing entry for 10.0.0.2/32\n  Known via \"connected\", distance 0, metric 0 (connected, via interface)\n"
            "  Routing Descriptor Blocks:\n  * directly connected, via Loopback0\n"),
    })
    r = trace(ios_lab, ask, "R1", "10.0.0.2")
    assert r["reached"] and r["hops"][0]["route"] == "Routing entry for 10.0.0.2/32"
    assert r["edges"] == [{"a": "R1", "b": "R2", "a_iface": "Ethernet0/1", "b_iface": "Ethernet0/1",
                           "overlay": False, "vni": None, "flood": False}]
    ask = asker({("R1", "show ip route"): "% Network not in table\n"})
    assert trace(ios_lab, ask, "R1", "10.9.9.9")["hops"][0]["route"] == "no route to 10.9.9.9"


def test_mlag_pair_and_bundles():
    # H1 bonded to L1 + L2 (pair A, VTEP .12); H3 bonded to L3 + L4 (pair B, VTEP .34)
    links = [("H1", "eth1", "L1", "eth5"), ("H1", "eth2", "L2", "eth5"),
             ("H3", "eth1", "L3", "eth5"), ("H3", "eth2", "L4", "eth5")]
    view = {"links": [{"a": {"node": a, "iface": ai}, "b": {"node": b, "iface": bi}} for a, ai, b, bi in links]}
    kinds = {"H1": "linux", "H3": "linux", **{f"L{i}": "arista_ceos" for i in range(1, 5)}}
    pcs = {n: {"Port-channel5": ["Ethernet5"]} for n in ("L1", "L2", "L3", "L4")}
    mlag_lab = Lab(view, kinds, {"10.255.2.12": ["L1", "L2"], "10.255.2.34": ["L3", "L4"]}, pcs)
    via_l3 = route("TENANT", "10.20.20.14/32", "eBGP", [{"vtepAddr": "10.255.2.34", "vni": 50001}])
    arp = json.dumps({"ipV4Neighbors": [{"hwAddress": "aac1.ab00.0014", "interface": "Vlan20, Port-Channel5"}]})
    here = route("TENANT", "10.20.20.0/24", "connected", [{"interface": "Vlan20"}], True)
    ask = asker({
        ("H1", "ip route get"): "10.20.20.14 via 10.10.10.1 dev bond0 src 10.10.10.11\n",
        ("H1", "ip -o link show master bond0"): (
            "198: eth1@if199: <BROADCAST,MULTICAST,SLAVE,UP> mtu 9500 master bond0 state UP\n"
            "200: eth2@if201: <BROADCAST,MULTICAST,SLAVE,UP> mtu 9500 master bond0 state UP\n"),
        ("L1", "show ip route"): via_l3, ("L2", "show ip route"): via_l3,
        ("L3", "show ip route"): here, ("L4", "show ip route"): here,
        ("L3", "show ip arp"): arp, ("L4", "show ip arp"): arp,
    })
    r = trace(mlag_lab, ask, "H1", "10.20.20.14", "H3")
    assert r["reached"]
    pairs = {(e["a"], e["b"], "vxlan" if e["overlay"] else e["a_iface"]) for e in r["edges"]}
    # Both bond members, each leaf to both halves of the far VTEP, each half down its port-channel member
    assert {("H1", "L1", "eth1"), ("H1", "L2", "eth2"), ("L1", "L3", "vxlan"), ("L1", "L4", "vxlan"),
            ("L2", "L3", "vxlan"), ("L2", "L4", "vxlan"), ("L3", "H3", "eth5"), ("L4", "H3", "eth5")} <= pairs
    notes = [n for h in r["hops"] for n in h.get("notes", [])]
    assert "bond0 is a bundle of eth1, eth2" in notes
    assert "VTEP 10.255.2.34 is the MLAG pair L3 + L4: either may take it" in notes
    assert "Port-Channel5 is a bundle of Ethernet5" in notes


def test_trace_cli(tmp_path, monkeypatch, capsys):
    """`clabfleet trace`: the GUI's walk from the command line."""
    from clabfleet import cli
    from clabfleet.gui.state import Workspace
    from clabfleet.routing import trace as trace_mod

    # Any file name will do: the CLI's topology need not look like a workspace's
    path = tmp_path / "lab.yaml"
    path.write_text(
        "name: t\ntopology:\n  nodes:\n"
        "    L1: {kind: arista_ceos, image: a}\n"
        "    H1: {kind: linux, image: a}\n"
        "    H2: {kind: linux, image: a}\n"
        "  links:\n    - endpoints: ['H1:eth1', 'L1:eth1']\n")
    answer = {
        "reached": True, "truncated": False,
        "hops": [{"node": "H1", "vrf": None, "route": "10.0.0.2 via 10.0.0.1 dev eth1 ",
                  "notes": ["a note"]},
                 {"node": "L1", "vrf": "TENANT", "route": "10.0.0.0/24 connected"},
                 {"node": "L2", "vrf": "default", "error": "L2 is not running"},
                 {"node": "H2", "vrf": None, "route": "destination"}],
        "edges": [{"a": "H1", "b": "L1", "a_iface": "eth1", "b_iface": "eth1", "overlay": False,
                   "vni": None, "flood": False},
                  {"a": "L1", "b": "L2", "a_iface": "", "b_iface": "", "overlay": True,
                   "vni": 10010, "flood": False},
                  {"a": "L1", "b": "L3", "a_iface": "", "b_iface": "", "overlay": True,
                   "vni": None, "flood": True}],
    }
    seen = {}

    def fake_trace(lab, ask, src, dst, dst_node=None):
        seen.update(src=src, dst=dst, dst_node=dst_node)
        return answer

    monkeypatch.setattr(trace_mod, "trace", fake_trace)
    monkeypatch.setattr(Workspace, "node_command", lambda self, lab, node, command, shell=True: (
        "3: eth1    inet 10.0.0.2/24 scope global eth1\n"))

    assert cli.main(["trace", str(path), "H1", "H2"]) == 0
    assert seen == {"src": "H1", "dst": "10.0.0.2", "dst_node": "H2"}
    assert capsys.readouterr().out == (
        "H1 reaches H2 (10.0.0.2) · equal-cost paths\n"
        "\n"
        "  1. H1               10.0.0.2 via 10.0.0.1 dev eth1\n"
        "                      a note\n"
        "  2. L1 [VRF TENANT]  10.0.0.0/24 connected\n"
        "  3. L2               error: L2 is not running\n"
        "  4. H2               destination\n"
        "\n"
        "Links taken:\n"
        "  H1:eth1 -> L1:eth1\n"
        "  L1 ~> L2  VXLAN VNI 10010\n"
        "  L1 ~> L3  VXLAN (flooded: address not learned)\n")

    assert cli.main(["trace", str(path), "H1", "H2", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "src": "H1", "dst": "10.0.0.2", "dst_node": "H2", **answer}

    # Not reached: exit 1, and the verdict says so
    answer = {"reached": False, "truncated": True, "edges": [],
              "hops": [{"node": "H1", "vrf": None, "route": "no route to 192.0.2.9"}]}
    assert cli.main(["trace", str(path), "H1", "192.0.2.9"]) == 1
    assert capsys.readouterr().out == (
        "H1 does not reach 192.0.2.9 · stopped after the hop limit\n"
        "\n"
        "  1. H1  no route to 192.0.2.9\n")

    # A node that is not in the topology, and a file that is not there
    assert cli.main(["trace", str(path), "Nope", "H2"]) == 1
    assert "No node 'Nope' in t" in capsys.readouterr().err
    assert cli.main(["trace", str(tmp_path / "missing.clab.yml"), "H1", "H2"]) == 1


def vrfs(**routes):
    """A ``show ip route vrf all`` answer: {vrf: (prefix, kind, vias, connected) or None}."""
    return json.dumps({"vrfs": {
        name: {"routes": {} if r is None else {
            r[0]: {"routeType": r[1], "vias": r[2], "directlyConnected": r[3]}}}
        for name, r in routes.items()}})


GATEWAY_IN_TENANT = vrfs(default=None,
                         TENANT=("10.10.10.0/24", "connected", [{"interface": "Vlan10"}], True))


def test_routed_traffic_stays_in_the_vrf_it_arrives_in():
    """A host's gateway is in VRF TENANT: a route the default VRF has to
    the destination does not carry the host's traffic."""
    only_default = vrfs(default=("10.255.0.1/32", "eBGP", [{"interface": "Ethernet1"}], False),
                        TENANT=None)
    ask = asker({
        ("H1", "ip route get"): "10.255.0.1 via 10.10.10.1 dev eth1 src 10.10.10.11 uid 0",
        ("L1", "show ip route vrf all 10.10.10.1 "): GATEWAY_IN_TENANT,
        ("L1", "show ip route vrf all 10.255.0.1 "): only_default,
    })
    r = trace(lab(), ask, "H1", "10.255.0.1")
    assert not r["reached"] and [h["node"] for h in r["hops"]] == ["H1", "L1"]
    assert r["hops"][1]["vrf"] == "TENANT"
    assert r["hops"][1]["route"] == "no route to 10.255.0.1 in VRF TENANT"

    # From the switch itself there is no arrival: the VRF that has the route, as before
    ask = asker({("L1", "show ip route vrf all 10.255.0.1 "): only_default,
                 ("S1", "show ip route"): route("default", "10.255.0.1/32", "connected",
                                                [{"interface": "Loopback0"}], True)})
    assert trace(lab(), ask, "L1", "10.255.0.1")["reached"]

    # The gateway's subnet in two VRFs, or nowhere, or no answer about it:
    # the VRF cannot be told, and the most specific route is taken as before
    both = vrfs(default=("10.10.10.0/24", "connected", [{"interface": "Vlan10"}], True),
                TENANT=("10.10.10.0/24", "connected", [{"interface": "Vlan10"}], True))
    for about_gateway in (both, vrfs(default=None), None):
        answers = {
            ("H1", "ip route get"): "10.255.0.1 via 10.10.10.1 dev eth1 src 10.10.10.11 uid 0",
            ("L1", "show ip route vrf all 10.255.0.1 "): only_default,
            ("S1", "show ip route"): route("default", "10.255.0.1/32", "connected",
                                           [{"interface": "Loopback0"}], True)}
        if about_gateway:
            answers[("L1", "show ip route vrf all 10.10.10.1 ")] = about_gateway
        r = trace(lab(), asker(answers), "H1", "10.255.0.1")
        assert r["reached"] and r["hops"][1]["vrf"] == "default"


def test_routed_between_switches_in_the_next_hops_vrf():
    """A VRF-lite hop: the next switch looks the address up in the VRF its
    end of the link is in, not in whichever VRF has a route."""
    ask = asker({
        ("L1", "show ip route vrf all 10.9.9.9 "): vrfs(RED=(
            "10.9.9.0/24", "eBGP", [{"interface": "Ethernet1", "nexthopAddr": "10.0.1.0"}], False)),
        ("S1", "show ip route vrf all 10.0.1.0 "): vrfs(
            default=None, RED=("10.0.1.0/31", "connected", [{"interface": "Ethernet1"}], True)),
        ("S1", "show ip route vrf all 10.9.9.9 "): vrfs(
            default=("10.9.9.9/32", "connected", [{"interface": "Loopback9"}], True), RED=None),
    })
    r = trace(lab(), ask, "L1", "10.9.9.9")
    assert not r["reached"] and r["hops"][1] == {
        "node": "S1", "vrf": "RED", "route": "no route to 10.9.9.9 in VRF RED"}


def test_a_switchs_own_address_on_a_subnet_is_reached():
    """A gateway address, also a virtual (anycast) one, is the switch
    itself: not an address to look for on the VLAN."""
    def interface(**addresses):
        return json.dumps({"interfaces": {"Vlan20": {"interfaceAddress": addresses}}})

    connected = vrfs(default=None,
                     TENANT=("10.20.20.0/24", "connected", [{"interface": "Vlan20"}], True))
    for own in (interface(primaryIp={"address": "0.0.0.0", "maskLen": 0},
                          virtualIp={"address": "10.20.20.1", "maskLen": 24}),
                interface(primaryIp={"address": "10.20.20.1", "maskLen": 24}),
                interface(primaryIp={"address": "10.20.20.2", "maskLen": 24},
                          secondaryIps={"10.20.20.1": {"address": "10.20.20.1", "maskLen": 24}})):
        ask = asker({
            ("H1", "ip route get"): "10.20.20.1 via 10.10.10.1 dev eth1 src 10.10.10.11 uid 0",
            ("L1", "show ip route vrf all 10.10.10.1 "): GATEWAY_IN_TENANT,
            ("L1", "show ip route vrf all 10.20.20.1 "): connected,
            ("L1", "show ip interface Vlan20"): own,
        })
        r = trace(lab(), ask, "H1", "10.20.20.1")
        assert r["reached"] and [h["node"] for h in r["hops"]] == ["H1", "L1"]
        assert r["hops"][1]["route"] == (
            "10.20.20.0/24 connected (VRF TENANT): 10.20.20.1 is L1's own address on Vlan20")

    # Another address on that subnet is looked for on the VLAN, as before
    ask = asker({
        ("H1", "ip route get"): "10.20.20.9 via 10.10.10.1 dev eth1 src 10.10.10.11 uid 0",
        ("L1", "show ip route vrf all 10.10.10.1 "): GATEWAY_IN_TENANT,
        ("L1", "show ip route vrf all 10.20.20.9 "): connected,
        ("L1", "show ip interface Vlan20"): interface(virtualIp={"address": "10.20.20.1"}),
        ("L1", "show ip arp"): json.dumps({"ipV4Neighbors": []}),
        ("L1", "show vxlan flood"): json.dumps({}),
        ("L1", "show bgp evpn"): json.dumps({}),
    })
    r = trace(lab(), ask, "H1", "10.20.20.9")
    assert not r["reached"] and ("L1", "show ip arp vrf TENANT 10.20.20.9 | json") in ask.asked
