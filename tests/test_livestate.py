import asyncio
import json
import time

import pytest

from clabfleet.cluster import ClusterConfig, HostInfo
from clabfleet.gui.state import Workspace, topology_view
from clabfleet.livestate import (
    LiveCache,
    endpoint_state,
    link_rates,
    link_states,
    linux_iface_names,
    oper_to_state,
    parse_docker_stats,
    parse_iface_states,
    parse_size,
    probe_ifaces,
    probe_stats,
)
from clabfleet.runner import CommandResult, Runner
from clabfleet.topology import topology_from_dict

# Interface probe output as cEOS prints it (trimmed), and an alpine node
# whose eth1 was set down (flags without IFF_UP) and whose eth2's peer was
CEOS_IFACES = ("eth0\tup\t0x1003\t\neth1\tup\t0x1003\t\neth2\tup\t0x1003\t\n"
               "fabric\tunknown\t0x1003\t\nlo\tunknown\t0x9\t\n")
ALPINE_IFACES = ("eth0\tup\t0x1003\t\neth1\tdown\t0x1002\t\neth2\tlowerlayerdown\t0x1003\t\n"
                 "lo\tunknown\t0x9\t\n")


def test_parse_iface_states_and_operstates():
    ifaces = parse_iface_states(CEOS_IFACES + "Gi0-0-0-0\tup\t0x1003\t1200\t3400\tto core\tR1\n")
    assert ifaces["eth1"] == {"oper": "up", "admin_up": True, "rx": None, "tx": None, "alias": ""}
    assert ifaces["Gi0-0-0-0"]["alias"] == "to core\tR1"  # the alias comes last: tabs and all
    assert (ifaces["Gi0-0-0-0"]["rx"], ifaces["Gi0-0-0-0"]["tx"]) == (1200, 3400)
    assert parse_iface_states(ALPINE_IFACES)["eth1"]["admin_up"] is False
    assert parse_iface_states("*\t\t\t\n") == {}  # /sys/class/net glob matched nothing
    assert parse_iface_states("eth1\n")["eth1"] == {"oper": "", "admin_up": None, "rx": None,
                                                    "tx": None, "alias": ""}
    assert [oper_to_state(s) for s in ("up", "UP", "down", "lowerlayerdown", "dormant",
                                       "notpresent", "unknown", "", "testing")] == [
        "up", "up", "down", "down", "down", "down", "unknown", "unknown", "unknown"]


def test_interface_aliases_map_to_linux_names():
    assert linux_iface_names("arista_ceos", "Ethernet1") == (["eth1", "Ethernet1"], True)
    assert linux_iface_names("ceos", "Et3/2")[0][0] == "eth3_2"
    assert linux_iface_names("cisco_iol", "Ethernet0/1")[0][0] == "eth1"
    assert linux_iface_names("cisco_iol", "Ethernet1/0")[0][0] == "eth4"
    assert linux_iface_names("cisco_iol", "e1/3")[0][0] == "eth7"
    assert linux_iface_names("nokia_srlinux", "ethernet-1/1")[0][0] == "e1-1"
    assert linux_iface_names("srl", "ethernet-1/3/1")[0][0] == "e1-3-1"
    assert linux_iface_names("linux", "eth1") == (["eth1"], True)
    assert linux_iface_names("arista_ceos", "eth2") == (["eth2"], True)
    # A naming scheme clabfleet does not know: not found means unknown, not down
    assert linux_iface_names("cisco_xrd", "Gi0-0-0-0") == (["Gi0-0-0-0"], False)


def test_endpoint_state():
    ceos = parse_iface_states(CEOS_IFACES)
    alpine = parse_iface_states(ALPINE_IFACES)
    assert endpoint_state(ceos, "arista_ceos", "Ethernet1") == ("up", "eth1 up")
    assert endpoint_state(ceos, "arista_ceos", "eth2") == ("up", "up")
    assert endpoint_state(alpine, "linux", "eth1") == ("down", "admin down")
    assert endpoint_state(alpine, "linux", "eth2") == ("down", "no carrier")
    dormant = parse_iface_states("eth1\tdormant\t0x1003\t\n")
    assert endpoint_state(dormant, "linux", "eth1") == ("down", "dormant")
    # Gone, e.g. after the container restarted: containerlab does not re-create it
    assert endpoint_state(alpine, "linux", "eth5") == ("down", "interface missing")
    assert endpoint_state(alpine, "cisco_xrd", "Gi0-0-0-1") == ("unknown", "interface not found")
    assert endpoint_state(None, "linux", "eth1") == ("unknown", "interface state not available")
    aliased = parse_iface_states("eth1\tdown\t0x1003\tto-core\n")
    assert endpoint_state(aliased, "sonic-vs", "to-core") == ("down", "eth1 no carrier")


def test_link_states_map_interfaces_to_topology_links():
    topo = topology_from_dict({
        "name": "t",
        "topology": {
            "nodes": {"sw": {"kind": "arista_ceos"}, "r1": {"kind": "cisco_iol"},
                      "pc": {"kind": "linux"}, "pc2": {"kind": "linux"},
                      "new": {"kind": "linux"}},
            "links": [
                {"endpoints": ["sw:Ethernet1", "r1:Ethernet0/1"]},
                {"endpoints": ["sw:eth2", "pc:eth1"]},
                {"endpoints": ["pc:eth2", "host:pc-eth2"]},
                {"endpoints": ["pc2:eth1", "new:eth1"]},
                {"endpoints": ["sw:Ethernet9", "pc2:eth2"]},
            ],
        },
    })
    nodes = {
        "sw": {"kind": "arista_ceos", "state": "running", "ifaces": parse_iface_states(CEOS_IFACES)},
        "r1": {"kind": "cisco_iol", "state": "running",
               "ifaces": parse_iface_states("eth0\tup\t0x1003\t\neth1\tup\t0x1003\t\n")},
        "pc": {"kind": "linux", "state": "running", "ifaces": parse_iface_states(ALPINE_IFACES)},
        "pc2": {"kind": "linux", "state": "exited", "ifaces": None},
        "new": {"kind": "linux", "state": "", "ifaces": None},
    }
    links = link_states(topology_view(topo)["links"], nodes)
    assert links["link0"] == {
        "state": "up",
        "a": {"node": "sw", "iface": "Ethernet1", "state": "up", "detail": "eth1 up", "rx": None, "tx": None},
        "b": {"node": "r1", "iface": "Ethernet0/1", "state": "up", "detail": "eth1 up", "rx": None, "tx": None},
    }
    assert links["link1"]["state"] == "down"
    assert (links["link1"]["a"]["state"], links["link1"]["b"]["state"]) == ("up", "down")
    assert links["link2"]["state"] == "down" and "b" not in links["link2"]  # host end not probed
    assert links["link3"]["state"] == "down"
    assert links["link3"]["a"]["detail"] == "container exited"
    assert links["link3"]["b"] == {"node": "new", "iface": "eth1", "state": "unknown",
                                   "detail": "not deployed"}
    assert links["link4"]["a"]["detail"] == "interface missing"


def test_parse_docker_stats_and_units():
    out = "\n".join([
        json.dumps({"Name": "clab-l-sw", "CPUPerc": "0.90%", "MemPerc": "5.66%",
                    "MemUsage": "870.9MiB / 15.03GiB"}),
        json.dumps({"Name": "clab-l-pc", "CPUPerc": "112.5%", "MemPerc": "0.01%",
                    "MemUsage": "1.2kB / 2GB"}),
        json.dumps({"Name": "clab-l-gone", "CPUPerc": "--", "MemPerc": "--",
                    "MemUsage": "-- / --"}),
        json.dumps({"Container": "abc123", "CPUPerc": "0.00%", "MemPerc": "0.00%",
                    "MemUsage": "0B / 0B"}),
        "Error response from daemon: No such container: clab-l-x",
        "{broken json",
    ])
    stats = parse_docker_stats(out)
    assert stats["clab-l-sw"] == {"cpu": 0.9, "mem": int(870.9 * 1024 ** 2),
                                  "mem_limit": int(15.03 * 1024 ** 3), "mem_percent": 5.66}
    assert stats["clab-l-pc"] == {"cpu": 112.5, "mem": 1200, "mem_limit": 2 * 10 ** 9,
                                  "mem_percent": 0.01}
    assert stats["clab-l-gone"] == {"cpu": None, "mem": None, "mem_limit": None,
                                    "mem_percent": None}
    assert stats["abc123"]["mem"] == 0
    assert set(stats) == {"clab-l-sw", "clab-l-pc", "clab-l-gone", "abc123"}
    assert parse_size("1.5TiB") == int(1.5 * 1024 ** 4)
    assert parse_size("12 MB") == 12 * 10 ** 6
    assert parse_size("--") is None and parse_size("") is None and parse_size("7XB") is None


class Docker(Runner):
    """Fake host: answers the interface probe per container and docker stats."""

    def __init__(self, ifaces=None, stats="", stats_rc=0, denied=False):
        super().__init__()
        self.ifaces = ifaces or {}
        self.stats, self.stats_rc, self.denied = stats, stats_rc, denied
        self.calls = []

    def run(self, args, cwd=None, check=True, sudo=None, on_output=None):
        self.calls.append((list(args), sudo))
        if args[0] == "containerlab":
            return CommandResult(0, json.dumps({"l": [
                {"Id": str(i), "Names": [f"clab-l-{n}"], "State": st, "Status": "Up",
                 "Labels": {"containerlab": "l", "clab-node-name": n, "clab-node-kind": k}}
                for i, (n, k, st) in enumerate([("sw", "arista_ceos", "running"),
                                                ("pc", "linux", "running"),
                                                ("old", "linux", "exited")])]}), "")
        if self.denied and not sudo:
            return CommandResult(1, "", "permission denied while trying to connect to the docker")
        if "stats" in args:
            return CommandResult(self.stats_rc, self.stats, "" if not self.stats_rc else "boom")
        container = args[args.index("exec") + 1]
        if container in self.ifaces:
            return CommandResult(0, self.ifaces[container], "")
        return CommandResult(126, "", 'exec: "sh": executable file not found')


def test_probes_use_one_stats_call_and_sudo_retry():
    host = Docker(ifaces={"c1": ALPINE_IFACES}, denied=True,
                  stats=json.dumps({"Name": "c1", "CPUPerc": "1%", "MemPerc": "1%",
                                    "MemUsage": "1MiB / 1GiB"}))
    assert probe_ifaces(host, "c1", host_sudo=True)["eth1"]["oper"] == "down"
    assert host.calls[-2] == (["timeout", "5", "docker", "exec", "c1", "sh", "-c",
                               host.calls[-1][0][-1]], False)
    assert host.calls[-1][1] is True
    assert probe_ifaces(host, "no-shell", host_sudo=True) is None

    host.calls.clear()
    assert probe_stats(host, ["c1", "c2"], host_sudo=True)["c1"]["cpu"] == 1.0
    assert host.calls[-1] == (["timeout", "5", "docker", "stats", "--no-stream", "--format",
                               "{{json .}}", "c1", "c2"], True)
    assert probe_stats(host, []) == {}
    with pytest.raises(RuntimeError, match="boom"):
        probe_stats(Docker(stats_rc=1), ["c1"])


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_live_cache_refreshes_once_per_interval():
    clock = FakeClock()
    cache = LiveCache(interval=5, clock=clock)
    assert cache.get("t") == (None, True)
    assert cache.claim("t") and not cache.claim("t") and cache.in_flight("t")
    cache.store("t", {"n": 1})
    assert not cache.in_flight("t")
    clock.now = 4.9
    assert cache.get("t") == ({"n": 1}, False)
    clock.now = 5
    assert cache.get("t") == ({"n": 1}, True)
    assert cache.claim("t")
    cache.release("t")
    assert cache.claim("t")


TOPO = """\
name: l
topology:
  nodes:
    sw: {kind: arista_ceos}
    pc: {kind: linux}
    old: {kind: linux}
  links:
    - endpoints: ["sw:Ethernet1", "pc:eth1"]
    - endpoints: ["sw:Ethernet2", "pc:eth2"]
    - endpoints: ["pc:eth3", "old:eth1"]
"""


def _live_workspace(tmp_path, monkeypatch, host):
    (tmp_path / "l.clab.yml").write_text(TOPO)
    ws = Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])
    monkeypatch.setattr(ws, "runner", lambda h: host)
    ws.topologies()
    return ws


def _host():
    return Docker(
        ifaces={"clab-l-sw": CEOS_IFACES, "clab-l-pc": "eth1\tup\t0x1003\t\neth2\tdown\t0x1002\t\n"},
        stats="\n".join(json.dumps({"Name": f"clab-l-{n}", "CPUPerc": "2.50%", "MemPerc": "1%",
                                    "MemUsage": "100MiB / 1GiB"}) for n in ("sw", "pc")))


def test_workspace_live_state_probes_in_background(tmp_path, monkeypatch):
    host = _host()
    ws = _live_workspace(tmp_path, monkeypatch, host)
    first = ws.live_state("l.clab.yml")
    assert first["updated"] is None and first["links"] == {}  # never waits on probes
    for _ in range(200):
        live = ws.live_state("l.clab.yml")
        if live["updated"]:
            break
        time.sleep(0.02)
    assert live["refreshing"] is False and live["interval"] == 5.0
    assert live["errors"] == {}
    assert live["nodes"]["sw"] == {"state": "running", "host": "localhost", "cpu": 2.5,
                                   "mem": 100 * 1024 ** 2, "mem_limit": 1024 ** 3,
                                   "mem_percent": 1.0}
    assert live["nodes"]["old"] == {"state": "exited", "host": "localhost"}
    assert {k: v["state"] for k, v in live["links"].items()} == {
        "link0": "up", "link1": "down", "link2": "down"}
    assert live["links"]["link1"]["b"]["detail"] == "admin down"

    # Within the interval, more requests (other tabs) do not probe again
    stats_calls = sum("stats" in args for args, _ in host.calls)
    assert stats_calls == 1  # one docker stats for both running nodes on the host
    for _ in range(5):
        ws.live_state("l.clab.yml")
    time.sleep(0.05)
    assert sum("stats" in args for args, _ in host.calls) == 1
    # Interface probes for running nodes only (readiness probes also use exec)
    assert sum("sh" in args for args, _ in host.calls) == 2
    with pytest.raises(KeyError):
        ws.live_state("missing.clab.yml")
    ws.close()


def test_live_state_degrades_when_docker_fails(tmp_path, monkeypatch):
    host = Docker(stats_rc=1)  # stats fail, no node has sh
    ws = _live_workspace(tmp_path, monkeypatch, host)
    live = ws.collect_live(tmp_path / "l.clab.yml")
    assert live["errors"] == {"localhost": "docker stats: boom"}
    assert live["nodes"]["sw"] == {"state": "running", "host": "localhost"}
    assert live["links"]["link0"]["state"] == "unknown"
    assert live["links"]["link2"]["state"] == "down"  # "old" is not running

    class Down(Runner):
        def run(self, *a, **kw):
            raise OSError("no route to host")
    monkeypatch.setattr(ws, "runner", lambda h: Down())
    ws._last_runtime = None
    live = ws.collect_live(tmp_path / "l.clab.yml")
    assert live["errors"] == {"localhost": "no route to host"}
    assert {v["state"] for v in live["links"].values()} == {"unknown"}
    ws.close()


def test_live_api(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp")
    from aiohttp.test_utils import TestClient, TestServer

    from clabfleet.gui import server

    ws = _live_workspace(tmp_path, monkeypatch, _host())

    async def scenario():
        app = server.create_app(ws, "tok")
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/api/live/l.clab.yml")).status == 401
            await client.post("/login", json={"token": "tok"})
            for _ in range(200):
                resp = await client.get("/api/live/l.clab.yml")
                assert resp.status == 200
                body = await resp.json()
                if body["updated"]:
                    break
                await asyncio.sleep(0.02)
            assert (await client.get("/api/live/nope.clab.yml")).status == 404
            return body

    body = asyncio.run(scenario())
    assert body["links"]["link1"]["state"] == "down"
    assert body["nodes"]["pc"]["cpu"] == 2.5
    ws.close()


def test_link_rates_from_byte_counters():
    def snap(t, a_tx, b_tx, b_rx=None):
        return {"updated": t, "links": {"L": {"state": "up", "a": {"tx": a_tx, "rx": 0},
                                              "b": {"tx": b_tx, "rx": b_rx}}}}
    current = snap(110, 1_000_000 + 125_000, 500)
    link_rates(snap(100, 1_000_000, 0), current)
    assert current["links"]["L"]["rate"] == {"ab": 100_000, "ba": 400}  # bits per second
    # a counter that went back (the node restarted): no rate from it
    current = snap(120, 10, 900)
    link_rates(snap(110, 5000, 500), current)
    assert current["links"]["L"]["rate"] == {"ab": 0, "ba": 320}
    # no read before, or no time between: nothing
    current = snap(130, 1, 1)
    link_rates(None, current)
    link_rates(snap(130, 0, 0), current)
    assert "rate" not in current["links"]["L"]
