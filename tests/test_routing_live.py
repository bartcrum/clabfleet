import asyncio
import json
import textwrap
from pathlib import Path

import pytest

from clabfleet.cli import main
from clabfleet.routing import routing_view
from clabfleet.routing import live as live_mod
from clabfleet.routing.live import (
    NodeState,
    collect,
    collect_node,
    eos_script,
    ios_duration,
    overlay,
    parse_outputs,
    split_markers,
    wanted_topics,
)
from clabfleet.runner import CommandResult
from clabfleet.topology import load_topology

TOPOLOGIES = Path(__file__).resolve().parent.parent / "topologies"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "routing"
NOW = 1790910000.0

# Recorded from a running cEOS 4.35 spine (fixtures/routing/spine_leaf_underlay.clab.yml)
EOS_SPINE1 = (FIXTURES / "eos_spine1.txt").read_text()

EOS_OSPF = json.dumps({"vrfs": {"default": {"instList": {"1": {"ospfNeighborEntries": [
    {"routerId": "10.255.0.2", "priority": 0, "drState": "P2P", "interfaceName": "Ethernet1",
     "adjacencyState": "full", "interfaceAddress": "10.0.0.1", "details": {"areaId": "0.0.0.0"}},
    {"routerId": "10.255.0.9", "priority": 1, "drState": "DR", "interfaceName": "Ethernet7",
     "adjacencyState": "init", "interfaceAddress": "10.0.7.9", "details": {"areaId": "0.0.0.0"}},
]}}}}})

IOS_BGP = textwrap.dedent("""\
    BGP router identifier 2.2.2.2, local AS number 65002
    BGP table version is 9, main routing table version 9

    Neighbor        V           AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  State/PfxRcd
    10.0.12.1       4        65001      12      10        9    0    0 00:05:12        4
    10.0.23.3       4        65003       0       0        1    0    0 never    Idle
    10.0.24.4       4        65004       0       0        1    0    0 1d02h    Idle (Admin)
    """)

IOS_OSPF = textwrap.dedent("""\

    Neighbor ID     Pri   State           Dead Time   Address         Interface
    1.1.1.1           1   FULL/DR         00:00:38    10.0.12.1       Ethernet0/1
    3.3.3.3           0   FULL/  -        00:00:31    10.0.23.3       Ethernet0/2
    9.9.9.9           1   INIT/DROTHER    00:00:31    10.0.99.9       Ethernet1/0
    """)

IOS_NVE = textwrap.dedent("""\
    Interface  VNI      Type Peer-IP          RMAC/Num_RTs   eVNI     state flags UP time
    nve1       10010    L2CP 10.255.2.3       4              10010      UP   N/A  00:10:21
    nve1       10020    L2CP 10.255.2.4       4              10020    DOWN   N/A  00:00:02
    """)


# --- parsing ------------------------------------------------------------------

def test_recorded_eos_output_parses():
    st = parse_outputs("eos", split_markers(EOS_SPINE1), NOW)
    assert sorted(st.collected) == ["bgp", "evpn", "vxlan"] and not st.errors
    assert sorted(st.bgp) == ["10.0.1.1", "10.0.1.3", "10.0.1.5", "10.0.1.7"]
    peer = st.bgp["10.0.1.1"]
    assert peer["established"] and peer["asn"] == "65001" and peer["pfx_rcvd"] == 1
    assert peer["uptime"] > 0
    assert st.evpn == {} and st.vxlan == {}


def test_eos_script_marks_each_command_and_a_failing_one_does_not_shift_the_rest():
    script = eos_script(["bgp", "vxlan"])
    assert "echo '@@clabfleet bgp'" in script and "show vxlan vtep | json" in script
    out = split_markers("@@clabfleet bgp\n> show ip bgp summary vrf all | json\n% Invalid input\n"
                        '@@clabfleet vxlan\n{"vteps": {"10.255.2.3": {}}}\n')
    st = parse_outputs("eos", out, NOW)
    assert st.collected == ["vxlan"] and "Invalid input" in st.errors["bgp"]
    assert st.vxlan == {"10.255.2.3": True}


def test_eos_ospf_and_vxlan_layouts():
    st = parse_outputs("eos", {"ospf": EOS_OSPF,
                               "vxlan": '{"interfaces": {"Vxlan1": {"vteps": ["10.255.2.2", "10.255.2.4"]}}}'},
                       NOW)
    assert [(n["router_id"], n["state"], n["iface"]) for n in st.ospf] == [
        ("10.255.0.2", "full", "Ethernet1"), ("10.255.0.9", "init", "Ethernet7")]
    assert sorted(st.vxlan) == ["10.255.2.2", "10.255.2.4"]


def test_ios_text_parsers():
    st = parse_outputs("ios", {"bgp": IOS_BGP, "ospf": IOS_OSPF, "vxlan": IOS_NVE}, NOW)
    assert st.bgp["10.0.12.1"]["established"] and st.bgp["10.0.12.1"]["uptime"] == 312
    assert st.bgp["10.0.12.1"]["pfx_rcvd"] == 4
    assert st.bgp["10.0.23.3"]["state"] == "Idle"
    assert st.bgp["10.0.24.4"]["state"] == "Idle (Admin)"
    assert [(n["router_id"], n["state"], n["iface"]) for n in st.ospf] == [
        ("1.1.1.1", "full", "Ethernet0/1"), ("3.3.3.3", "full", "Ethernet0/2"),
        ("9.9.9.9", "init", "Ethernet1/0")]
    assert st.vxlan == {"10.255.2.3": True, "10.255.2.4": False}
    # No OSPF neighbours: IOS prints nothing at all; BGP not running is not an error
    st = parse_outputs("ios", {"ospf": "", "bgp": "% BGP not active\n"}, NOW)
    assert st.ospf == [] and st.bgp == {} and not st.errors


def test_ios_durations():
    assert ios_duration("00:05:12") == 312
    assert ios_duration("1d02h") == 93600
    assert ios_duration("2w3d") == 2 * 604800 + 3 * 86400
    assert ios_duration("never") is None


def test_wanted_topics_follow_the_configs():
    evpn = routing_view(load_topology(TOPOLOGIES / "evpn_fabric.clab.yml"))
    # Leaf-1 has an L3 VNI: its VRF routes name the VTEPs behind it
    assert wanted_topics(evpn, "Leaf-1") == ["bgp", "evpn", "vxlan", "vrf_vteps"]
    assert wanted_topics(evpn, "Spine-1") == ["bgp", "evpn"]
    assert wanted_topics(evpn, "Host-1") == []
    tri = routing_view(load_topology(TOPOLOGIES / "three_router_triangle.clab.yml"))
    assert wanted_topics(tri, "R1") == ["ospf"]


# --- collecting -----------------------------------------------------------------

class FakeRunner:
    def __init__(self, stdout="", code=0, stderr=""):
        self.calls = []
        self.result = CommandResult(code, stdout, stderr)

    def run(self, args, cwd=None, check=True, sudo=None, on_output=None):
        self.calls.append(args)
        return self.result


def test_collect_node_eos_uses_one_docker_exec():
    runner = FakeRunner(EOS_SPINE1)
    st = collect_node("eos", ["bgp", "evpn", "vxlan"], {"container": "clab-x-Spine-1"},
                      runner, False, None, NOW)
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[:6] == ["timeout", "20", "docker", "exec", "clab-x-Spine-1", "sh"]
    assert st.collected == ["bgp", "evpn", "vxlan"]

    failed = collect_node("eos", ["bgp"], {"container": "c"}, FakeRunner("", 1, "No such container"),
                          False, None, NOW)
    assert failed.errors == {"bgp": "No such container"}


def test_collect_node_ios_over_ssh_stops_after_a_login_failure():
    asked = []

    def ssh(cmd):
        asked.append(cmd)
        raise RuntimeError("Authentication failed.")

    st = collect_node("ios", ["ospf", "bgp"], {"container": "c"}, None, False, ssh, NOW)
    assert asked == ["show ip ospf neighbor"]
    assert "Authentication" in st.errors["ospf"] and st.errors["bgp"] == "no output"


def test_collect_skips_nodes_without_protocols_and_unsupported_kinds():
    view = routing_view(load_topology(TOPOLOGIES / "evpn_fabric.clab.yml"))
    containers = {
        "Leaf-1": {"kind": "arista_ceos", "state": "running", "container": "l1"},
        "Leaf-2": {"kind": "arista_ceos", "state": "exited", "container": "l2"},
        "Spine-1": {"kind": "juniper_crpd", "state": "running", "container": "s1"},
        "Host-1": {"kind": "linux", "state": "running", "container": "h1"},
    }
    runner = FakeRunner("@@clabfleet bgp\n{}\n@@clabfleet evpn\n{}\n@@clabfleet vxlan\n{}\n")
    states = collect(view, containers, lambda c: (runner, False, None))
    assert sorted(states) == ["Leaf-1", "Spine-1"]
    assert "not supported" in states["Spine-1"].errors["bgp"]
    assert states["Leaf-1"].collected == ["bgp", "evpn", "vxlan"]


# --- overlay ----------------------------------------------------------------------

def _bgp_peer(ip, asn, state="Established", pfx=3):
    up = state == "Established"
    return {"ip": ip, "vrf": "default", "asn": asn, "state": state, "established": up,
            "uptime": 60 if up else None, "pfx_rcvd": pfx if up else None, "pfx_accepted": None}


def test_overlay_bgp_states_extras_and_drift():
    view = routing_view(load_topology(FIXTURES / "spine_leaf_underlay.clab.yml"))
    sessions = {(s["a"]["node"], s["b"]["node"]): s for s in view["bgp"]["sessions"]}
    s1l1, s1l2, s2l1 = (sessions[("Spine-1", "Leaf-1")], sessions[("Spine-1", "Leaf-2")],
                        sessions[("Spine-2", "Leaf-1")])
    states = {
        "Spine-1": NodeState("eos", ["bgp"], bgp={
            "10.0.1.1": _bgp_peer("10.0.1.1", "65001"),
            "10.0.1.3": _bgp_peer("10.0.1.3", "65001", "Active"),
            "10.9.9.9": _bgp_peer("10.9.9.9", "64999", "Connect"),        # not intended
        }),
        "Leaf-1": NodeState("eos", ["bgp"], bgp={"10.0.1.0": _bgp_peer("10.0.1.0", "65000")}),
        "Leaf-2": NodeState("eos", errors={"bgp": "timed out"}),
    }
    running = {n: n != "Leaf-4" for n in view["bgp"]["nodes"]}
    ov = overlay(view, states, running, NOW)

    assert ov["bgp"][s1l1["id"]]["state"] == "up"
    down = ov["bgp"][s1l2["id"]]
    assert down["state"] == "down" and down["detail"] == "ipv4: Active"
    assert down["families"]["ipv4"]["b"]["detail"] == "timed out"
    # Leaf-1 lost its Spine-2 neighbor in the running config
    missing = ov["bgp"][s2l1["id"]]
    assert missing["state"] == "down" and "no 10.0.2.0 neighbor" in missing["detail"]
    s1l4 = sessions[("Spine-1", "Leaf-4")]
    assert ov["bgp"][s1l4["id"]]["families"]["ipv4"]["b"]["detail"] == "not running"
    assert ov["extra"]["bgp"] == [{"node": "Spine-1", "ip": "10.9.9.9", "peer": "", "asn": "64999",
                                   "state": "down", "detail": "Connect", "uptime": None,
                                   "pfx_rcvd": None}]
    assert ov["summary"]["bgp"]["up"] == 1
    assert ov["nodes"]["Leaf-2"]["errors"] == {"bgp": "timed out"}


def test_overlay_flags_a_running_neighbor_missing_from_the_startup_config():
    text = (FIXTURES / "spine_leaf_underlay.clab.yml").read_text().replace(
        "         neighbor 10.0.2.0 peer group SPINES\n         network 10.255.1.1/32",
        "         network 10.255.1.1/32", 1)
    path = TOPOLOGIES.parent / "tests" / "fixtures" / "routing"
    from clabfleet.topology import topology_from_dict
    import yaml
    view = routing_view(topology_from_dict(yaml.safe_load(text), path))
    s = next(s for s in view["bgp"]["sessions"] if s["a"]["node"] == "Spine-2" and s["b"]["node"] == "Leaf-1")
    assert s["configured"] == "one-sided"
    states = {"Spine-2": NodeState("eos", ["bgp"], bgp={"10.0.2.1": _bgp_peer("10.0.2.1", "65001")}),
              "Leaf-1": NodeState("eos", ["bgp"], bgp={"10.0.2.0": _bgp_peer("10.0.2.0", "65000")})}
    ov = overlay(view, states, {"Spine-2": True, "Leaf-1": True}, NOW)
    entry = ov["bgp"][s["id"]]
    assert entry["state"] == "up" and "Leaf-1 has a neighbor 10.0.2.0" in entry["drift"]
    assert ov["extra"]["bgp"] == []


def test_overlay_ospf_from_ios_text():
    view = routing_view(load_topology(TOPOLOGIES / "three_router_triangle.clab.yml"))
    r1 = parse_outputs("ios", {"ospf": textwrap.dedent("""\
        Neighbor ID     Pri   State           Dead Time   Address         Interface
        2.2.2.2           1   FULL/BDR        00:00:38    10.0.12.2       Ethernet0/1
        3.3.3.3           1   EXSTART/DR      00:00:38    10.0.13.3       Ethernet0/2
        7.7.7.7           1   FULL/DR         00:00:38    10.0.17.7       Ethernet1/0
        """)}, NOW)
    ov = overlay(view, {"R1": r1}, {"R1": True, "R2": True, "R3": False}, NOW)
    by_pair = {(a["a"]["node"], a["b"]["node"]): ov["ospf"][a["id"]] for a in view["ospf"]["adjacencies"]}
    assert by_pair[("R1", "R2")]["state"] == "up"           # R2 not read: R1's view is enough
    assert by_pair[("R1", "R3")]["state"] == "down" and by_pair[("R1", "R3")]["detail"] == "EXSTART"
    assert by_pair[("R2", "R3")]["state"] == "unknown"
    assert [e["router_id"] for e in ov["extra"]["ospf"]] == ["7.7.7.7"]


def test_overlay_vxlan_tunnels():
    view = routing_view(load_topology(TOPOLOGIES / "evpn_fabric.clab.yml"))
    states = {
        "Leaf-1": NodeState("eos", ["vxlan"], vxlan={"10.255.2.3": True}),
        "Leaf-3": NodeState("eos", ["vxlan"], vxlan={"10.255.2.1": True}),
        "Leaf-2": NodeState("eos", ["vxlan"], vxlan={}),
    }
    ov = overlay(view, states, {f"Leaf-{i}": True for i in range(1, 5)}, NOW)
    tunnels = {(t["a"]["node"], t["b"]["node"]): ov["vxlan"][t["id"]] for t in view["evpn"]["tunnels"]}
    assert tunnels[("Leaf-1", "Leaf-3")]["state"] == "up"
    assert tunnels[("Leaf-1", "Leaf-2")]["state"] == "down"
    assert "has not learned" in tunnels[("Leaf-1", "Leaf-2")]["detail"]
    assert tunnels[("Leaf-3", "Leaf-4")]["state"] == "down"   # Leaf-3 read, has not learned Leaf-4


def test_eos_vrf_routes_name_the_l3_vni_vteps():
    # Recorded from Leaf-1 of topologies/spine_leaf.clab.yml (cEOS 4.35):
    # Leaf-2 and Leaf-4 share only the L3 VNI with it, so `show vxlan vtep`
    # does not list them, but routes in VRF TENANT go "via VTEP" to them
    text = (FIXTURES / "eos_leaf1_vrf_routes.json").read_text()
    st = parse_outputs("eos", {"vrf_vteps": text}, NOW)
    assert st.collected == ["vrf_vteps"] and st.vrf_vteps == {"10.255.2.2": True, "10.255.2.4": True}
    assert "show ip route vrf all | json" in eos_script(["vrf_vteps"])

    view = routing_view(load_topology(TOPOLOGIES / "spine_leaf.clab.yml"))
    states = {"Leaf-1": NodeState("eos", ["vxlan", "vrf_vteps"], vxlan={"10.255.2.3": True},
                                  vrf_vteps=st.vrf_vteps),
              "Leaf-2": NodeState("eos", ["vxlan", "vrf_vteps"], vxlan={"10.255.2.4": True},
                                  vrf_vteps={"10.255.2.1": True})}
    ov = overlay(view, states, {f"Leaf-{i}": True for i in range(1, 5)}, NOW)
    tunnels = {(t["a"]["node"], t["b"]["node"]): ov["vxlan"][t["id"]] for t in view["evpn"]["tunnels"]}
    assert tunnels[("Leaf-1", "Leaf-2")]["state"] == "up"      # L3 VNI only
    assert tunnels[("Leaf-1", "Leaf-3")]["a"]["state"] == "up"  # L2 VNI: from show vxlan vtep


def test_ios_skips_eos_only_topics():
    runner = FakeRunner()
    asked = []
    st = collect_node("ios", ["vxlan", "vrf_vteps"], {"container": "c"}, runner, False,
                      lambda cmd: (asked.append(cmd), (0, IOS_NVE))[1], NOW)
    assert asked == ["show nve peers"] and "vrf_vteps" not in st.errors


# --- CLI and GUI -------------------------------------------------------------------

def test_cli_routing_live(monkeypatch, capsys):
    view_path = FIXTURES / "spine_leaf_underlay.clab.yml"
    view = routing_view(load_topology(view_path))
    first = view["bgp"]["sessions"][0]

    def fake_collect_lab(cluster, topo, v):
        assert cluster.hosts[0].name == "localhost"
        states = {first["a"]["node"]: NodeState("eos", ["bgp"], bgp={
            first["b"]["ip"]: _bgp_peer(first["b"]["ip"], "65001", "Active")})}
        return {**overlay(v, states, {first["a"]["node"]: True}, NOW), "errors": {}}

    monkeypatch.setattr("clabfleet.cli.collect_lab", fake_collect_lab)
    assert main(["routing", str(view_path), "--live"]) == 0
    out = capsys.readouterr().out
    assert "[DOWN: ipv4: Active]" in out
    # Spine-1 was read: its other leaves are missing from its running config
    assert "no 10.0.1.3 neighbor in the running config" in out
    assert "Live: BGP sessions: 4 down, 4 unknown" in out

    assert main(["routing", str(view_path), "--live", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["live"]["summary"]["bgp"]["down"] == 4


def test_gui_routing_live_endpoint(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp")
    from aiohttp.test_utils import TestClient, TestServer

    from clabfleet.cluster import ClusterConfig, HostInfo
    from clabfleet.gui import server
    from clabfleet.gui.state import Workspace

    (tmp_path / "sl.clab.yml").write_text((FIXTURES / "spine_leaf_underlay.clab.yml").read_text())
    ws = Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])
    monkeypatch.setattr(Workspace, "runtime", lambda self, *a, **k: [])
    calls = []

    def fake_collect(self, path):
        calls.append(path)
        return {"updated": NOW, "bgp": {}, "summary": {}}

    monkeypatch.setattr(Workspace, "collect_protocols", fake_collect)

    async def scenario():
        app = server.create_app(ws, "tok")
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/api/routing-live/sl.clab.yml")).status == 401
            await client.post("/login", json={"token": "tok"})
            first = await (await client.get("/api/routing-live/sl.clab.yml")).json()
            assert first["updated"] is None and first["interval"] == 10.0
            for _ in range(50):
                data = await (await client.get("/api/routing-live/sl.clab.yml")).json()
                if data["updated"]:
                    break
                await asyncio.sleep(0.02)
            assert data["updated"] == NOW and len(calls) == 1      # cached until due
            assert (await client.get("/api/routing-live/nope.clab.yml")).status == 404

    asyncio.run(scenario())


def test_workspace_collects_protocols_from_running_containers(tmp_path, monkeypatch):
    from clabfleet.cluster import ClusterConfig, HostInfo
    from clabfleet.gui.state import HostState, Workspace

    (tmp_path / "sl.clab.yml").write_text((FIXTURES / "spine_leaf_underlay.clab.yml").read_text())
    ws = Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])
    containers = [{"lab": "spine-leaf-fabric", "node": "Spine-1", "kind": "arista_ceos",
                   "state": "running", "container": "clab-spine-leaf-fabric-Spine-1",
                   "host": "localhost", "ipv4": "172.20.20.3"}]
    monkeypatch.setattr(Workspace, "_recent_runtime",
                        lambda self: [HostState("localhost", True, "", containers)])
    runner = FakeRunner(EOS_SPINE1)
    monkeypatch.setattr(Workspace, "runner", lambda self, host: runner)
    result = ws.collect_protocols(tmp_path / "sl.clab.yml")
    assert result["summary"]["bgp"] == {"up": 4, "unknown": 4}
    assert result["nodes"]["Spine-1"]["collected"] == ["bgp"]
    assert "bgp" in runner.calls[0][-1] and "evpn" not in runner.calls[0][-1]
    ws.close()


def test_live_module_only_reads():
    """Every command sent to a node is a show command."""
    for cmd in [*live_mod.EOS_COMMANDS.values(), *live_mod.IOS_COMMANDS.values()]:
        assert cmd.startswith("show ")
