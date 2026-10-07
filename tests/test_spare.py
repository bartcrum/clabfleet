"""Spare ports: ports with nothing plugged in, added and cabled later."""

import asyncio
import json

import pytest

from clabfleet import deployer as deployer_mod
from clabfleet import spare
from clabfleet.cluster import ClusterConfig, HostInfo
from clabfleet.deployer import LabDeployer
from clabfleet.runner import CommandError, CommandResult, Runner
from clabfleet.topology import is_spare_link, load_topology, topology_from_dict

pytest.importorskip("aiohttp")
pytest.importorskip("ruamel.yaml")

from clabfleet.gui import state  # noqa: E402
from clabfleet.gui.editing import add_spare_ports, cable_spare_ports  # noqa: E402
from clabfleet.gui.state import HostState, Workspace, topology_view  # noqa: E402

LAB = """\
name: t   # my lab
topology:
  nodes:
    sw1: {kind: arista_ceos, image: ceos}   # a spine
    sw2: {kind: arista_ceos, image: ceos}
    r1: {kind: cisco_iol, image: iol}
    h1: {kind: linux, image: alpine}
  links:
    - endpoints: ["sw1:eth1", "sw2:eth1"]   # the uplink
"""


def spare_link(node, iface):
    return {"type": "dummy", "endpoint": {"node": node, "interface": iface},
            "labels": {"lab.spare": "true"}}


def topo(*links):
    return topology_from_dict({"name": "t", "topology": {
        "nodes": {"sw1": {"kind": "arista_ceos"}, "sw2": {"kind": "ceos"},
                  "r1": {"kind": "cisco_iol"}, "h1": {"kind": "linux"}},
        "links": [{"endpoints": ["sw1:eth1", "sw2:eth1"]}, *links]}})


# ----------------------------------------------------------------------
# The model
# ----------------------------------------------------------------------

def test_a_spare_port_is_a_labelled_dummy_link():
    assert is_spare_link(spare_link("sw1", "eth2"))
    for other in ({"type": "dummy", "endpoint": {"node": "sw1", "interface": "eth2"}},
                  {**spare_link("sw1", "eth2"), "labels": {"lab.spare": "no"}},
                  {**spare_link("sw1", "eth2"), "type": "host"},
                  {"endpoints": ["sw1:eth1", "sw2:eth1"]}, "sw1:eth1", None):
        assert not is_spare_link(other)
    t = topo(spare_link("sw1", "eth3"), spare_link("sw1", "eth2"), spare_link("sw2", "eth2"),
             {"type": "dummy", "endpoint": {"node": "h1", "interface": "eth1"}})
    assert t.spare_ports() == {"sw1": ["eth3", "eth2"], "sw2": ["eth2"]}
    # The diagram's links are the cables; spare ports belong to their node
    view = topology_view(t)
    assert {n["name"]: n["spare"] for n in view["nodes"]} == {
        "sw1": ["eth3", "eth2"], "sw2": ["eth2"], "r1": [], "h1": []}
    assert [(link["a"].get("node"), link["type"]) for link in view["links"]] == [
        ("sw1", "veth"), ("h1", "dummy")]
    assert {n["name"]: n["cable_live"] for n in view["nodes"]} == {
        "sw1": True, "sw2": True, "r1": False, "h1": True}


def test_new_ports_take_the_kinds_next_free_names():
    t = topo(spare_link("sw1", "eth2"), {"type": "dummy", "endpoint": {"node": "sw1", "interface": "eth4"}})
    assert spare.next_ports(t, "sw1", 3) == ["eth3", "eth5", "eth6"]   # eth1, eth2, eth4 are taken
    assert spare.next_ports(t, "r1", 4) == ["Ethernet0/1", "Ethernet0/2", "Ethernet0/3", "Ethernet1/0"]
    assert spare.next_ports(t, "h1", 1) == ["eth1"]
    for count in (0, -1, 65, "8", True, None, 1.5):
        with pytest.raises(ValueError, match="between 1 and 64"):
            spare.next_ports(t, "sw1", count)
    with pytest.raises(KeyError, match="No node 'nope'"):
        spare.next_ports(t, "nope", 1)
    # A kind with a fixed number of ports runs out
    full = topo(*[spare_link("r1", name) for name in spare.next_ports(t, "r1", 60)])
    assert len(spare.next_ports(full, "r1", 3)) == 3
    with pytest.raises(ValueError, match="r1 .* has only 3 more ports to give"):
        spare.next_ports(full, "r1", 4)
    assert spare.live_problem(t, "sw2") is None and "cisco_iol" in spare.live_problem(t, "r1")


# ----------------------------------------------------------------------
# The file
# ----------------------------------------------------------------------

def test_file_edits_keep_the_rest_of_the_file():
    text = add_spare_ports(add_spare_ports(LAB, "sw1", ["eth2", "eth3"]), "sw2", ["eth2"])
    assert "# my lab" in text and "# a spine" in text and "# the uplink" in text
    assert text.count('labels: {lab.spare: "true"}') == 3
    assert "endpoint: {node: sw1, interface: eth3}" in text
    assert topology_from_dict(__import__("yaml").safe_load(text)).spare_ports() == {
        "sw1": ["eth2", "eth3"], "sw2": ["eth2"]}

    cabled = cable_spare_ports(text, ("sw1", "eth3"), ("sw2", "eth2"))
    assert '- endpoints: ["sw1:eth3", "sw2:eth2"]' in cabled and "# the uplink" in cabled
    t = topology_from_dict(__import__("yaml").safe_load(cabled))
    assert t.spare_ports() == {"sw1": ["eth2"]} and len(t.links) == 3

    for a, b, error in ((("sw1", "eth1"), ("sw2", "eth2"), "sw1:eth1 is not a spare port"),
                        (("sw1", "eth2"), ("sw2", "eth9"), "sw2:eth9 is not a spare port"),
                        (("sw1", "eth2"), ("sw1", "eth2"), "cannot be cabled to itself")):
        with pytest.raises(ValueError, match=error):
            cable_spare_ports(text, a, b)
    # Two spare ports of one node: a loop cable
    assert '["sw1:eth2", "sw1:eth3"]' in cable_spare_ports(text, ("sw1", "eth2"), ("sw1", "eth3"))
    # A file without links yet gets its list
    bare = "name: t\ntopology:\n  nodes:\n    a: {kind: linux}\n"
    assert "links:" in add_spare_ports(bare, "a", ["eth1"])


# ----------------------------------------------------------------------
# On the hosts
# ----------------------------------------------------------------------

class Recorder(Runner):
    """Records what would run; ``fail``: argv prefixes that exit 1."""

    name = "h"

    def __init__(self, fail=(), inspect=None):
        super().__init__()
        self.calls, self.fail, self.inspect = [], fail, inspect or {}

    def run(self, args, cwd=None, check=True, sudo=None, on_output=None):
        self.calls.append(list(args))
        line = " ".join(args)
        code = 1 if any(f in line if isinstance(f, str) else args[:len(f)] == list(f)
                        for f in self.fail) else 0
        out = json.dumps(self.inspect) if args[:2] == ["containerlab", "inspect"] else ""
        if check and code:
            raise CommandError(" ".join(args), code, "it failed")
        return CommandResult(code, out, "it failed" if code else "")

    def close(self):
        pass


def test_unplug_turns_the_carrier_of_spare_ports_off():
    runner = Recorder(fail=["clab-t-h1"])
    failed = spare.unplug(runner, {"sw1": "clab-t-sw1", "h1": "clab-t-h1"},
                          {"sw1": ["eth2", "eth3"], "h1": ["eth1"], "elsewhere": ["eth1"]})
    assert runner.calls == [
        ["docker", "exec", "clab-t-sw1", "ip", "link", "set", "eth2", "carrier", "off"],
        ["docker", "exec", "clab-t-sw1", "ip", "link", "set", "eth3", "carrier", "off"],
        ["docker", "exec", "clab-t-h1", "ip", "link", "set", "eth1", "carrier", "off"]]
    assert failed == ["h1:eth1"]   # BusyBox: it shows as up, nothing more


def test_a_live_cable_replaces_both_placeholders_with_a_veth_pair():
    runner = Recorder(fail=[("docker", "exec", "clab-t-sw2", "ip", "link", "del")])  # already unplugged
    spare.cable_live(runner, ("clab-t-sw1", "eth3"), ("clab-t-sw2", "eth2"))
    assert runner.calls == [
        ["docker", "exec", "clab-t-sw1", "ip", "link", "del", "eth3"],
        ["docker", "exec", "clab-t-sw2", "ip", "link", "del", "eth2"],
        ["containerlab", "tools", "veth", "create", "-a", "clab-t-sw1:eth3", "-b", "clab-t-sw2:eth2"]]
    with pytest.raises(CommandError):
        spare.cable_live(Recorder(fail=["tools veth create"]), ("a", "eth1"), ("b", "eth1"))
    assert spare.port_exists(Recorder(), "c", "eth1") and not spare.port_exists(
        Recorder(fail=["ip link show"]), "c", "eth1")


def _inspect(lab, *nodes):
    return {lab: [{"Names": [f"clab-{lab}-{n}"], "State": "running",
                   "Labels": {"containerlab": lab, "clab-node-name": n, "clab-node-kind": "arista_ceos"}}
                  for n in nodes]}


def test_a_deploy_leaves_spare_ports_looking_unplugged(tmp_path, monkeypatch):
    path = tmp_path / "t.clab.yml"
    path.write_text(add_spare_ports(LAB, "sw1", ["eth2", "eth3"]))
    runner = Recorder(inspect=_inspect("t", "sw1", "sw2", "r1", "h1"))
    monkeypatch.setattr(deployer_mod, "create_runner", lambda host: runner)
    result = LabDeployer(ClusterConfig(hosts=[HostInfo("localhost")])).deploy(
        path, check_images=False)
    assert result["hosts"]["localhost"]["status"] == "deployed"
    carrier = [c for c in runner.calls if c[-2:] == ["carrier", "off"]]
    assert [(c[2], c[6]) for c in carrier] == [("clab-t-sw1", "eth2"), ("clab-t-sw1", "eth3")]
    deploy = next(i for i, c in enumerate(runner.calls) if c[:2] == ["containerlab", "deploy"])
    assert deploy < runner.calls.index(carrier[0])   # once the containers are there

    # It is for looks: not being able to does not fail the deploy
    broken = Recorder(fail=["containerlab inspect"])
    monkeypatch.setattr(deployer_mod, "create_runner", lambda host: broken)
    assert LabDeployer(ClusterConfig(hosts=[HostInfo("localhost")])).deploy(
        path, check_images=False)["hosts"]["localhost"]["status"] == "deployed"
    # No spare ports, nothing extra to run
    path.write_text(LAB)
    plain = Recorder()
    monkeypatch.setattr(deployer_mod, "create_runner", lambda host: plain)
    LabDeployer(ClusterConfig(hosts=[HostInfo("localhost")])).deploy(path, check_images=False)
    assert not any(c[:2] == ["containerlab", "inspect"] for c in plain.calls)


# ----------------------------------------------------------------------
# The GUI's two operations
# ----------------------------------------------------------------------

@pytest.fixture
def lab_ws(tmp_path, monkeypatch):
    """A workspace with the lab, its hosts given recording runners;
    ``ws.running(...)`` says which nodes run and where."""
    (tmp_path / "t.clab.yml").write_text(LAB)
    ws = Workspace(ClusterConfig(hosts=[HostInfo("localhost"), HostInfo("h2", host="10.0.0.2")]),
                   [tmp_path])
    ws.rec = Recorder()
    monkeypatch.setattr(ws, "runner", lambda host: ws.rec)
    containers = []

    def running(**where):
        containers[:] = [{"lab": "t", "node": n, "container": f"clab-t-{n}", "state": "running",
                          "kind": "arista_ceos", "host": host} for n, host in where.items()]

    monkeypatch.setattr(ws, "runtime", lambda max_age=0: [HostState("localhost", ok=True,
                                                                    containers=list(containers))])
    monkeypatch.setattr(ws, "_recent_runtime", lambda: ws.runtime())
    ws.running = running
    yield ws
    ws.close()


def test_adding_ports_writes_them_into_the_file(lab_ws):
    ws = lab_ws
    result = ws.add_ports("t.clab.yml", "sw1", 8)
    assert result["ports"] == [f"eth{i}" for i in range(2, 10)] and result["applies"] == "now"
    assert next(n["spare"] for n in result["detail"]["nodes"] if n["name"] == "sw1") == result["ports"]
    assert load_topology(ws.topology_path("t.clab.yml")).spare_ports()["sw1"] == result["ports"]
    # More of them go on from there; a running node gets them at its next deploy
    ws.running(sw1="localhost")
    more = ws.add_ports("t.clab.yml", "sw1", 2, result["detail"]["hash"])
    assert more["ports"] == ["eth10", "eth11"] and more["applies"] == "next deploy"
    assert ws.rec.calls == []   # nothing is done to the running lab
    with pytest.raises(state.EditConflict, match="changed on disk"):
        ws.add_ports("t.clab.yml", "sw1", 1, result["detail"]["hash"])   # a stale page
    with pytest.raises(ValueError):
        ws.add_ports("t.clab.yml", "sw1", 0)
    with pytest.raises(KeyError):
        ws.add_ports("t.clab.yml", "nope", 1)


def test_a_cable_goes_into_the_file_and_onto_the_running_lab(lab_ws):
    ws = lab_ws
    for node in ("sw1", "sw2", "r1"):
        ws.add_ports("t.clab.yml", node, 3)
    A, B = ("sw1", "eth2"), ("sw2", "eth2")

    def links():
        return ["-".join(f"{e.node}:{e.interface}" for e in link.endpoints)
                for link in load_topology(ws.topology_path("t.clab.yml")).links if link.is_p2p]

    # Not deployed: the file only
    result = ws.cable("t.clab.yml", A, B)
    assert (result["live"], result["note"]) == (
        False, "the lab is not deployed: the cable is there at the next deploy")
    assert links() == ["sw1:eth1-sw2:eth1", "sw1:eth2-sw2:eth2"] and ws.rec.calls == []
    with pytest.raises(ValueError, match="sw1:eth2 is not a spare port"):
        ws.cable("t.clab.yml", A, ("sw2", "eth3"))   # it is taken now

    # Running, both on one host: also plugged in now
    ws.running(sw1="localhost", sw2="localhost", r1="localhost")
    result = ws.cable("t.clab.yml", ("sw1", "eth3"), ("sw2", "eth3"))
    assert (result["live"], result["note"]) == (True, "")
    assert ws.rec.calls[-1] == ["containerlab", "tools", "veth", "create",
                                "-a", "clab-t-sw1:eth3", "-b", "clab-t-sw2:eth3"]
    assert links()[-1] == "sw1:eth3-sw2:eth3"
    assert next(n["spare"] for n in result["detail"]["nodes"] if n["name"] == "sw1") == ["eth4"]


@pytest.mark.parametrize("setup, a, b, note", [
    ({"sw1": "localhost"}, ("sw1", "eth2"), ("sw2", "eth2"), "a node of this cable is not running"),
    ({"sw1": "localhost", "sw2": "h2"}, ("sw1", "eth2"), ("sw2", "eth2"), "on different hosts"),
    ({"sw1": "localhost", "r1": "localhost"}, ("sw1", "eth2"), ("r1", "Ethernet0/1"),
     "r1 is of kind 'cisco_iol'"),
])
def test_cables_that_wait_for_the_next_deploy(lab_ws, setup, a, b, note):
    ws = lab_ws
    for node in ("sw1", "sw2", "r1"):
        ws.add_ports("t.clab.yml", node, 2)
    ws.running(**setup)
    result = ws.cable("t.clab.yml", a, b)
    assert not result["live"] and note in result["note"]
    assert not any(c[:3] == ["containerlab", "tools", "veth"] for c in ws.rec.calls)
    # The file has it all the same
    left = load_topology(ws.topology_path("t.clab.yml")).spare_ports()
    assert a[1] not in left.get(a[0], []) and b[1] not in left.get(b[0], [])


def test_a_port_the_running_node_lacks_or_a_failed_cable_is_said(lab_ws):
    ws = lab_ws
    for node in ("sw1", "sw2"):
        ws.add_ports("t.clab.yml", node, 3)
    ws.running(sw1="localhost", sw2="localhost")
    # Added after the node booted: not on the node, so not plugged in now
    ws.rec = Recorder(fail=[("docker", "exec", "clab-t-sw2", "ip", "link", "show")])
    result = ws.cable("t.clab.yml", ("sw1", "eth2"), ("sw2", "eth2"))
    assert not result["live"] and "sw2:eth2 was added after sw2 booted" in result["note"]
    assert not any(c[4:6] == ["link", "del"] for c in ws.rec.calls)   # nothing was unplugged
    # containerlab could not: the file has the cable, and the reason is given
    ws.rec = Recorder(fail=["tools veth create"])
    result = ws.cable("t.clab.yml", ("sw1", "eth3"), ("sw2", "eth3"))
    assert not result["live"] and "could not be made on the running lab" in result["note"]
    assert "it is there after the next deploy" in result["note"]


def test_ports_and_cable_endpoints(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from clabfleet.gui import server
    from clabfleet.gui.auth import AuditLog, UserStore

    (tmp_path / "t.clab.yml").write_text(LAB)
    ws = Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])
    ws.runtime = lambda max_age=0: [HostState("localhost", ok=True)]
    ws._recent_runtime = ws.runtime
    users = UserStore(tmp_path / "users.yaml")
    operator, viewer = users.add("olga"), users.add("vic", "viewer")
    audit_path = tmp_path / "audit.jsonl"

    async def scenario():
        app = server.create_app(ws, users=users, audit=AuditLog(audit_path))
        async with TestClient(TestServer(app)) as client:
            await client.post("/login", json={"token": viewer})
            assert (await client.post("/api/ports/t.clab.yml", json={"node": "sw1", "count": 2})).status == 403
            assert (await client.post("/api/cable/t.clab.yml", json={})).status == 403
            # ... but a viewer sees the spare ports
            await client.post("/login", json={"token": operator, "switch": True})
            for node in ("sw1", "sw2"):
                resp = await client.post("/api/ports/t.clab.yml", json={"node": node, "count": 2})
                assert resp.status == 200 and (await resp.json())["ports"] == ["eth2", "eth3"]
            for bad in ({"node": "sw1", "count": 0}, {"node": "sw1", "count": "many"}, {"count": 2}):
                assert (await client.post("/api/ports/t.clab.yml", json=bad)).status == 400, bad
            assert (await client.post("/api/ports/t.clab.yml", json={"node": "x", "count": 1})).status == 404
            body = {"a": {"node": "sw1", "iface": "eth2"}, "b": {"node": "sw2", "iface": "eth3"}}
            resp = await client.post("/api/cable/t.clab.yml", json=body)
            result = await resp.json()
            assert resp.status == 200 and result["live"] is False and "not deployed" in result["note"]
            assert (await client.post("/api/cable/t.clab.yml", json=body)).status == 400   # taken
            for bad in ({}, {"a": "sw1:eth3", "b": "sw2:eth2"}, {"a": {"node": "sw1"}, "b": body["b"]}):
                assert (await client.post("/api/cable/t.clab.yml", json=bad)).status == 400, bad
            await client.post("/login", json={"token": viewer, "switch": True})
            detail = await (await client.get("/api/topologies/t.clab.yml")).json()
            assert next(n["spare"] for n in detail["nodes"] if n["name"] == "sw1") == ["eth3"]

    asyncio.run(scenario())
    ws.close()
    events = [json.loads(line) for line in audit_path.read_text().splitlines()]
    added = [e["details"] for e in events if e["event"] == "ports_added"]
    assert added == [{"topology": "t.clab.yml", "node": n, "ports": ["eth2", "eth3"]} for n in ("sw1", "sw2")]
    (cabled,) = [e["details"] for e in events if e["event"] == "cabled"]
    assert (cabled["a"], cabled["b"], cabled["live"]) == ("sw1:eth2", "sw2:eth3", False)
