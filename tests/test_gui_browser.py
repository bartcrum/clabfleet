"""Browser smoke test of the GUI: load the page in headless Chrome against a
real server (fake hosts, one lab "deployed"), click through the main
screens, and fail on any uncaught JavaScript error or console.error.

pytest only reaches the Python side; this catches page script errors such
as a constant used before it is defined, which stop the whole GUI.

Needs Chrome or Chromium (CLABFLEET_BROWSER, or chromium / google-chrome on
PATH). Without one the test is skipped, unless CLABFLEET_REQUIRE_BROWSER=1
(set in CI), which makes a missing browser a failure.
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")
pytest.importorskip("ruamel.yaml")

import aiohttp  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from clabfleet.cluster import ClusterConfig, HostInfo  # noqa: E402
from clabfleet.gui import server  # noqa: E402
from clabfleet.gui.state import HostState, Workspace  # noqa: E402

TOPOLOGIES = Path(__file__).resolve().parent.parent / "topologies"
BROWSERS = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome")


def _browser():
    path = os.environ.get("CLABFLEET_BROWSER") or next(filter(None, map(shutil.which, BROWSERS)), None)
    if not path:
        if os.environ.get("CLABFLEET_REQUIRE_BROWSER") == "1":
            pytest.fail("No Chrome or Chromium found, and CLABFLEET_REQUIRE_BROWSER=1")
        pytest.skip("No Chrome or Chromium for the browser smoke test")
    return path


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _fake_lab(monkeypatch, ws):
    """spine-leaf-fabric runs on a fake host; live reads answer at once."""
    from clabfleet.topology import load_topology

    topo = load_topology(TOPOLOGIES / "spine_leaf.clab.yml")
    containers = [{
        "lab": topo.name, "node": n, "state": "running", "status": "Up 2 minutes", "ready": True,
        "kind": topo.effective_node(n)["kind"], "host": "localhost", "container": f"clab-{topo.name}-{n}",
        "image": topo.effective_node(n).get("image", ""), "ipv4": f"172.20.20.{i + 2}",
        "topo_file": str(TOPOLOGIES / "spine_leaf.clab.yml"),
    } for i, n in enumerate(topo.nodes)]
    monkeypatch.setattr(Workspace, "runtime", lambda self, max_age=0: [
        HostState("localhost", ok=True, containers=containers)])
    monkeypatch.setattr(Workspace, "host_status", lambda self: [
        {"name": "localhost", "host": "localhost", "ok": True, "version": "0.79.0", "cpus": 8,
         "mem_available_mb": 8000, "error": None}])
    now = time.time
    monkeypatch.setattr(Workspace, "live_state", lambda self, topo_id: {
        "updated": now(), "interval": 5, "nodes": {}, "links": {}, "errors": {}, "refreshing": False})
    monkeypatch.setattr(Workspace, "routing_live", lambda self, topo_id: {
        "updated": now(), "interval": 10, "ospf": {}, "bgp": {}, "vxlan": {}, "nodes": {}, "errors": {},
        "extra": {"ospf": [], "bgp": [], "evpn": []}, "refreshing": False})
    monkeypatch.setattr(Workspace, "running_config", lambda self, lab, node, command="": "")


class Page:
    """A Chrome tab over the DevTools protocol; collects page errors."""

    def __init__(self, ws):
        self.ws, self.seq, self.errors = ws, 0, []

    async def cmd(self, method, **params):
        self.seq += 1
        my = self.seq
        await self.ws.send_json({"id": my, "method": method, "params": params})
        while True:
            msg = await asyncio.wait_for(self.ws.receive_json(), 30)
            if msg.get("method") == "Runtime.exceptionThrown":
                d = msg["params"]["exceptionDetails"]
                self.errors.append(f"{d.get('exception', {}).get('description') or d.get('text')} "
                                   f"({d.get('url', '')}:{d.get('lineNumber')})")
            elif msg.get("method") == "Runtime.consoleAPICalled" and msg["params"]["type"] == "error":
                self.errors.append("console.error: " + " ".join(
                    str(a.get("value", a.get("description", ""))) for a in msg["params"]["args"]))
            if msg.get("id") == my:
                if "error" in msg:
                    raise AssertionError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def js(self, expression):
        """Evaluate in the page; awaits promises; raises on a thrown error."""
        res = await self.cmd("Runtime.evaluate", expression=expression, awaitPromise=True,
                             returnByValue=True)
        if "exceptionDetails" in res:
            raise AssertionError(f"{expression[:80]}: {res['exceptionDetails'].get('exception', {}).get('description')}")
        return res.get("result", {}).get("value")

    async def until(self, expression, timeout=10):
        deadline = time.monotonic() + timeout
        while not await self.js(expression):
            if time.monotonic() > deadline:
                errors = "".join(f"\n  page error: {e}" for e in self.errors)
                raise AssertionError(f"timed out waiting for: {expression}{errors}")
            await asyncio.sleep(0.1)

    async def key(self, key):
        for kind in ("keyDown", "keyUp"):
            await self.cmd("Input.dispatchKeyEvent", type=kind, key=key, code=key,
                           windowsVirtualKeyCode={"Escape": 27, "Enter": 13, "ArrowDown": 40}.get(key, 0))


def test_gui_pages_load_without_errors(tmp_path, monkeypatch):
    browser = _browser()
    for name in ("spine_leaf.clab.yml", "evpn_fabric.clab.yml"):
        shutil.copy(TOPOLOGIES / name, tmp_path / name)
    ws = Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])
    _fake_lab(monkeypatch, ws)

    async def scenario():
        srv = TestServer(server.create_app(ws, "tok"), host="127.0.0.1")
        await srv.start_server()
        port = _free_port()
        profile = tempfile.mkdtemp(prefix="clabfleet-browser-")
        proc = subprocess.Popen(
            [browser, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
             "--no-first-run", "--no-default-browser-check", "--disable-gpu", "--no-sandbox",
             "--window-size=1440,900", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            async with aiohttp.ClientSession() as http:
                for _ in range(100):
                    try:
                        async with http.get(f"http://127.0.0.1:{port}/json") as resp:
                            tabs = await resp.json()
                        break
                    except aiohttp.ClientError:
                        await asyncio.sleep(0.1)
                else:
                    raise AssertionError("Chrome's DevTools did not come up")
                tab = next(t for t in tabs if t["type"] == "page")
                async with http.ws_connect(tab["webSocketDebuggerUrl"], max_msg_size=0) as sock:
                    page = Page(sock)
                    await page.cmd("Runtime.enable")
                    await page.cmd("Page.enable")
                    await page.cmd("Page.navigate", url=f"{srv.make_url('/')}#token=tok")
                    await steps(page)
                    assert page.errors == [], "\n".join(page.errors)
        finally:
            proc.terminate()
            proc.wait(10)
            shutil.rmtree(profile, ignore_errors=True)
            await srv.close()

    asyncio.run(scenario())


async def steps(page):
    # Start page with the lab cards
    await page.until("document.querySelectorAll('#welcome-labs .card:not(.skeleton)').length === 2")
    await page.until("document.querySelectorAll('#topo-list .lab-item').length === 2")
    # The deployed lab: its diagram, a node face per node, fresh live data
    await page.js("selectTopology('spine_leaf.clab.yml')")
    await page.until("document.querySelectorAll('#diagram .node').length === 10")
    assert await page.js("document.querySelectorAll('#diagram .node use.dev').length") == 10
    await page.until("document.querySelector('#diagram-fresh').textContent.startsWith('live')")
    assert "running" in await page.js("document.querySelector('#lab-stats').textContent")
    # Inspector, tabs, routing (intended and live), YAML
    await page.js("selectNode('Leaf-1')")
    await page.until("!document.querySelector('#inspector').hidden")
    for view in ("nodes", "routing", "yaml", "diagram"):
        await page.js(f"showView('{view}')")
        await page.until(f"!document.querySelector('#view-{view}').hidden")
    await page.js("showView('routing'); window.Routing.showLive()")
    await page.until("document.querySelectorAll('#routing .node').length > 0")
    await page.js("document.querySelectorAll('#routing-protos .seg-btn')[2].click()")  # EVPN
    await page.until("document.querySelectorAll('#routing .rt-edge.tunnel').length === 6")
    await page.js("showView('diagram')")
    # Health and a drift diff tab
    await page.js("openHealth()")
    await page.until("document.querySelector('#health-list').children.length > 0")
    await page.js("openDiff('spine_leaf.clab.yml', 'Leaf-1', 'running')")
    await page.until("[...document.querySelectorAll('.pane.activity-pane pre')].some(p => p.textContent.includes('No drift'))")
    # Command palette: find a node and jump to it
    await page.js("window.Palette.open()")
    await page.js("const i = document.querySelector('#palette-input'); i.value = 'leaf-3'; "
                  "i.dispatchEvent(new Event('input'))")
    await page.until("document.querySelectorAll('#palette-list .pal-item').length > 0")
    await page.key("Enter")
    await page.until("S.selectedNode === 'Leaf-3'")
    # Keyboard on the canvas
    await page.js("document.querySelector('#diagram').focus()")
    await page.key("n")
    await page.until("document.querySelector('#diagram').dataset.kb")
    await page.key("ArrowDown")
    await page.key("Escape")
    # Builder: edit mode on and off
    await page.js("document.querySelector('#edit-topo').click()")
    await page.until("window.Builder.editing")
    await page.until("document.querySelectorAll('#palette-items .palette-item').length === 3")
    await page.js("document.querySelector('#builder-discard').click()")
    await page.until("!window.Builder.editing")
    # Sidebar filter and collapse; the other theme
    await page.js("const f = document.querySelector('#topo-search'); f.value = 'evpn'; f.dispatchEvent(new Event('input'))")
    await page.until("document.querySelectorAll('#topo-list .lab-item').length === 1")
    await page.js("document.querySelector('#side-toggle').click()")
    await page.until("document.querySelector('#sidebar').classList.contains('collapsed')")
    await page.js("document.querySelector('#side-toggle').click(); document.querySelector('#theme').click()")
    # A lab that is not deployed
    await page.js("selectTopology('evpn_fabric.clab.yml')")
    await page.until("document.querySelectorAll('#diagram .node').length === 10")
    await page.js("showView('routing')")
    await page.until("document.querySelectorAll('#routing .node').length > 0")
    await asyncio.sleep(0.5)  # let late renders throw, if they would
