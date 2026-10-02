import asyncio
import json
import threading

import pytest

pytest.importorskip("aiohttp")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from clabfleet.capture import GUI_MAX_COUNT, CaptureError  # noqa: E402
from clabfleet.cluster import ClusterConfig, HostInfo  # noqa: E402
from clabfleet.gui import captures, server  # noqa: E402
from clabfleet.gui.auth import AuditLog, UserStore  # noqa: E402
from clabfleet.gui.state import Workspace  # noqa: E402

TOPO = """\
name: t
topology:
  nodes:
    a: {kind: linux, image: alpine}
    b: {kind: linux, image: alpine}
  links:
    - endpoints: ["a:eth1", "b:eth1"]
"""


class FakeCapture:
    """Yields ``chunks``; then, if ``hold``, blocks until stopped."""

    def __init__(self, spec, chunks, hold=False):
        self.spec = spec
        self.chunks = list(chunks)
        self.hold = hold
        self.stopped = False
        self.stop_reason = ""
        self.exit_code = None
        self.stop_calls = 0
        self._stop = threading.Event()

    def describe(self):
        return f"{self.spec.node}:{self.spec.interface} (fake)"

    def read(self, size=65536):
        if self.chunks:
            return self.chunks.pop(0)
        if self.hold:
            self._stop.wait(10)
        self.exit_code = 0
        return b""

    def stop(self, reason="stopped"):
        self.stop_calls += 1
        if not self.stopped:
            self.stopped, self.stop_reason = True, reason
        self._stop.set()


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "t.clab.yml").write_text(TOPO)
    return Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])


def _fake_open(made, chunks, hold=False):
    def open_capture(workspace, topo_id, spec, on_message=None):
        if topo_id != "t.clab.yml":
            raise KeyError(f"Unknown topology '{topo_id}'")
        if on_message:
            on_message("listening on eth1")
        cap = FakeCapture(spec, chunks, hold)
        made.append(cap)
        return cap, "t"
    return open_capture


async def _client(app):
    client = TestClient(TestServer(app))
    await client.start_server()
    await client.get("/?token=tok", allow_redirects=False)
    return client


def test_spec_from_query_applies_gui_limits():
    spec = captures.spec_from_query({"node": "a", "iface": "eth1", "filter": " icmp "}, "pcap")
    assert (spec.duration, spec.count, spec.bpf_filter, spec.format) == (60, None, "icmp", "pcap")
    spec = captures.spec_from_query({"node": "a", "iface": "eth1", "duration": "1200"}, "text")
    assert spec.duration == 1200  # live tabs may run longer than downloads
    with pytest.raises(ValueError, match="limited to 600"):
        captures.spec_from_query({"node": "a", "iface": "eth1", "duration": "1200"}, "pcap")
    with pytest.raises(ValueError, match="limited to"):
        captures.spec_from_query({"node": "a", "iface": "eth1", "count": str(GUI_MAX_COUNT + 1)}, "pcap")
    for bad in ({"count": "x"}, {"count": "1.5"}, {"duration": "0"}, {"iface": "-i"},
                {"filter": "-r /etc/shadow"}):
        with pytest.raises(ValueError):
            captures.spec_from_query({"node": "a", "iface": "eth1", **bad}, "pcap")
    assert captures.pcap_filename("lab", captures.spec_from_query(
        {"node": "r1", "iface": "Ethernet1/1"}, "pcap")).startswith("lab-r1-Ethernet1_1-")


def test_open_capture_checks_the_interface_before_touching_hosts(ws, monkeypatch):
    monkeypatch.setattr(Workspace, "find_node", lambda *a: pytest.fail("must not look up nodes"))
    spec = captures.spec_from_query({"node": "a", "iface": "eth9"}, "pcap")
    with pytest.raises(ValueError, match="no interface 'eth9'"):
        captures.open_capture(ws, "t.clab.yml", spec)
    with pytest.raises(KeyError):
        captures.open_capture(ws, "missing.clab.yml", spec)


def test_capture_endpoints_need_the_token(ws):
    async def scenario():
        app = server.create_app(ws, "tok")
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/capture?topo=t.clab.yml&node=a&iface=eth1")
            assert resp.status == 401
            resp = await client.get("/ws/capture?topo=t.clab.yml&node=a&iface=eth1")
            assert resp.status == 401

    asyncio.run(scenario())


def test_captures_are_operator_only_and_audited(ws, tmp_path, monkeypatch):
    made = []
    monkeypatch.setattr(server, "open_capture", _fake_open(made, [b"\xd4\xc3\xb2\xa1"]))
    users = UserStore(tmp_path / "users.yaml")
    op_token, view_token = users.add("alice", "operator"), users.add("bob", "viewer")
    audit_path = tmp_path / "audit.jsonl"
    url = "/api/capture?topo=t.clab.yml&node=a&iface=eth1&filter=icmp"

    async def scenario():
        app = server.create_app(ws, users=users, audit=AuditLog(audit_path))
        async with TestClient(TestServer(app)) as client:
            await client.get(f"/?token={view_token}", allow_redirects=False)
            assert (await client.get(url)).status == 403
            assert (await client.get(url.replace("/api/", "/ws/"))).status == 403
            assert made == []
            await client.post("/logout")
            await client.get(f"/?token={op_token}", allow_redirects=False)
            resp = await client.get(url)
            assert resp.status == 200 and await resp.read() == b"\xd4\xc3\xb2\xa1"

    asyncio.run(scenario())
    events = [json.loads(line) for line in audit_path.read_text().splitlines()]
    started, finished = [e for e in events if e["event"].startswith("capture_")]
    assert started["event"] == "capture_started" and started["user"] == "alice"
    assert started["details"] == {"topology": "t.clab.yml", "node": "a", "interface": "eth1",
                                  "filter": "icmp", "mode": "pcap"}
    assert finished["event"] == "capture_finished" and finished["details"]["bytes"] == 4


def test_pcap_download_streams_and_stops(ws, monkeypatch):
    made = []
    monkeypatch.setattr(server, "open_capture", _fake_open(made, [b"\xd4\xc3\xb2\xa1", b"pkt"]))

    async def scenario():
        client = await _client(server.create_app(ws, "tok"))
        try:
            resp = await client.get("/api/capture?topo=t.clab.yml&node=a&iface=eth1&count=5")
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "application/vnd.tcpdump.pcap"
            assert 'filename="t-a-eth1-' in resp.headers["Content-Disposition"]
            assert await resp.read() == b"\xd4\xc3\xb2\xa1pkt"
            assert made[0].spec.count == 5 and made[0].spec.duration == 60
            assert made[0].stop_calls >= 1

            resp = await client.get("/api/capture?topo=t.clab.yml&node=a&iface=eth1&duration=9999")
            assert resp.status == 400
            resp = await client.get("/api/capture?topo=nope.clab.yml&node=a&iface=eth1")
            assert resp.status == 404
            assert client.app[server.CAPTURES] == set()
        finally:
            await client.close()

    asyncio.run(scenario())


def test_pcap_download_reports_tcpdump_errors(ws, monkeypatch):
    made = []
    monkeypatch.setattr(server, "open_capture", _fake_open(made, []))  # no output at all

    def failing(workspace, topo_id, spec, on_message=None):
        raise CaptureError("Docker is not usable on localhost")

    async def scenario():
        client = await _client(server.create_app(ws, "tok"))
        try:
            resp = await client.get("/api/capture?topo=t.clab.yml&node=a&iface=eth1")
            assert resp.status == 502
            assert "tcpdump failed" in await resp.text()
            assert made[0].stop_calls >= 1
            monkeypatch.setattr(server, "open_capture", failing)
            resp = await client.get("/api/capture?topo=t.clab.yml&node=a&iface=eth1")
            assert resp.status == 502
            assert "Docker is not usable" in await resp.text()
        finally:
            await client.close()

    asyncio.run(scenario())


def test_pcap_download_stops_when_the_browser_goes_away(ws, monkeypatch):
    made = []
    monkeypatch.setattr(server, "open_capture", _fake_open(made, [b"\xd4\xc3\xb2\xa1"], hold=True))

    async def scenario():
        client = await _client(server.create_app(ws, "tok"))
        try:
            resp = await client.get("/api/capture?topo=t.clab.yml&node=a&iface=eth1")
            assert await resp.content.read(4) == b"\xd4\xc3\xb2\xa1"
            assert not made[0].stopped
            resp.close()  # cancel the download
            for _ in range(60):
                if made[0].stopped:
                    break
                await asyncio.sleep(0.1)
            assert made[0].stopped
            for _ in range(20):
                if not client.app[server.CAPTURES]:
                    break
                await asyncio.sleep(0.1)
            assert client.app[server.CAPTURES] == set()
        finally:
            await client.close()

    asyncio.run(scenario())


def test_live_capture_websocket(ws, monkeypatch):
    made = []
    monkeypatch.setattr(server, "open_capture",
                        _fake_open(made, [b"12:00:00 IP a > b\n12:00:01 IP b > a\n"], hold=True))

    async def scenario():
        client = await _client(server.create_app(ws, "tok"))
        try:
            sock = await client.ws_connect(
                "/ws/capture?topo=t.clab.yml&node=a&iface=eth1&filter=icmp&cols=80&rows=24")
            output = b""
            while b"12:00:01" not in output:
                msg = await asyncio.wait_for(sock.receive(), 5)
                output += msg.data
            assert b"listening on eth1" in output
            assert b"filter 'icmp'" in output
            assert b"IP a > b\r\n" in output  # no pty: newlines made terminal-safe
            assert made[0].spec.format == "text"

            await sock.send_str(json.dumps({"t": "i", "d": "\x03"}))  # Ctrl+C stops it
            exit_msg = None
            async for msg in sock:
                if msg.type.name == "TEXT":
                    exit_msg = json.loads(msg.data)
            assert exit_msg == {"t": "exit", "code": 0}
            assert made[0].stop_reason == "interrupted"

            # Closing the tab stops the capture too
            sock = await client.ws_connect("/ws/capture?topo=t.clab.yml&node=a&iface=eth1")
            await asyncio.wait_for(sock.receive(), 5)
            await sock.close()
            for _ in range(50):
                if made[1].stopped:
                    break
                await asyncio.sleep(0.1)
            assert made[1].stopped

            # Errors are shown in the tab
            sock = await client.ws_connect("/ws/capture?topo=t.clab.yml&node=a&iface=-x")
            output = b""
            async for msg in sock:
                if msg.type.name == "BINARY":
                    output += msg.data
            assert b"Invalid interface" in output
        finally:
            await client.close()

    asyncio.run(scenario())


def test_shutdown_stops_running_captures(ws, monkeypatch):
    async def scenario():
        app = server.create_app(ws, "tok")
        cap = FakeCapture(None, [], hold=True)
        app[server.CAPTURES].add(cap)
        await server._on_shutdown(app)
        assert cap.stopped

    asyncio.run(scenario())
