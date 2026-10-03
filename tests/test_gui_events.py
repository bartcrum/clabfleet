import asyncio
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from clabfleet.cluster import ClusterConfig, HostInfo  # noqa: E402
from clabfleet.gui import server  # noqa: E402
from clabfleet.gui.events import EventLog, link_items, protocol_items  # noqa: E402
from clabfleet.gui.state import Workspace  # noqa: E402
from clabfleet.routing import routing_view  # noqa: E402
from clabfleet.topology import load_topology  # noqa: E402

TOPOLOGIES = Path(__file__).resolve().parent.parent / "topologies"


def _link(state, a_state="up"):
    return {"links": {"L1": {"state": state,
                             "a": {"node": "R1", "iface": "eth1", "state": a_state, "detail": "admin down"},
                             "b": {"node": "R2", "iface": "eth1", "state": "up", "detail": ""}}}}


def test_event_log_records_changes_only():
    log = EventLog(keep=3)
    assert log.observe("t", "links", link_items(_link("up")), 100) == []  # the first read: a baseline
    assert log.observe("t", "links", link_items(_link("up")), 105) == []  # nothing changed
    new = log.observe("t", "links", link_items(_link("down", "down")), 110)
    assert new == [{"t": 110, "kind": "link", "id": "L1", "label": "R1:eth1 ↔ R2:eth1",
                    "from": "up", "to": "down", "detail": "R1:eth1 admin down"}]
    log.observe("t", "links", link_items(_link("up")), 120)
    assert [(e["from"], e["to"]) for e in log.events("t")] == [("up", "down"), ("down", "up")]
    assert [e["t"] for e in log.events("t", since=110)] == [120]
    # Sources and labs are apart; the log keeps the last ``keep`` per lab
    assert log.events("other") == []
    for i in range(5):
        log.observe("t", "links", link_items(_link("down" if i % 2 == 0 else "up")), 200 + i)
    assert len(log.events("t")) == 3 and log.events("t")[-1]["t"] == 204


def test_protocol_items_are_named_after_their_ends():
    view = routing_view(load_topology(TOPOLOGIES / "spine_leaf.clab.yml"))
    session = view["bgp"]["sessions"][0]
    tunnel = view["evpn"]["tunnels"][0]
    items = protocol_items({"bgp": {session["id"]: {"state": "down", "detail": "Active"}},
                            "vxlan": {tunnel["id"]: {"state": "up"}},
                            "ospf": {"gone": {"state": "up"}}}, view)
    bgp = items[f"bgp:{session['id']}"]
    assert bgp["label"] == f"BGP {session['a']['node']} ↔ {session['b']['node']}"
    assert bgp["kind"] == "bgp" and bgp["state"] == "down" and bgp["detail"] == "Active"
    assert items[f"vxlan:{tunnel['id']}"]["label"].startswith("VXLAN ")
    assert "ospf:gone" not in items  # not in the view: no name, not recorded


def test_refreshes_feed_the_timeline(tmp_path, monkeypatch):
    (tmp_path / "t.clab.yml").write_text("name: t\ntopology:\n  nodes:\n    R1: {kind: linux, image: a}\n"
                                         "    R2: {kind: linux, image: a}\n"
                                         "  links:\n    - endpoints: [R1:eth1, R2:eth1]\n")
    ws = Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])
    ws.topologies()
    reads = iter([{"updated": 1.0, **_link("up")}, {"updated": 2.0, **_link("down", "down")},
                  RuntimeError("host gone"), {"updated": 4.0, **_link("up")}])

    def collect_live(self, path):
        read = next(reads)
        if isinstance(read, Exception):
            raise read
        return read

    monkeypatch.setattr(Workspace, "collect_live", collect_live)
    for _ in range(4):
        ws._refresh_live("t.clab.yml", tmp_path / "t.clab.yml")
    # The failed read in between neither records nor resets anything
    assert [(e["to"], e["t"]) for e in ws.events.events("t.clab.yml")] == [("down", 2.0), ("up", 4.0)]

    async def scenario():
        async with TestClient(TestServer(server.create_app(ws, "tok"))) as client:
            await client.post("/login", json={"token": "tok"})
            body = await (await client.get("/api/events/t.clab.yml?since=3")).json()
            assert [e["to"] for e in body["events"]] == ["up"]
            assert (await client.get("/api/events/nope.clab.yml")).status == 404
            assert (await client.get("/api/events/t.clab.yml?since=x")).status == 400

    asyncio.run(scenario())
