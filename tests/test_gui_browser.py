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
import os
import re
import shutil
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
# Google Chrome first: on Ubuntu (and GitHub's runners) /usr/bin/chromium
# can be a stub for a snap that is not installed, which never starts
BROWSERS = ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser")


def _browser():
    path = os.environ.get("CLABFLEET_BROWSER") or next(filter(None, map(shutil.which, BROWSERS)), None)
    if not path:
        if os.environ.get("CLABFLEET_REQUIRE_BROWSER") == "1":
            pytest.fail("No Chrome or Chromium found, and CLABFLEET_REQUIRE_BROWSER=1")
        pytest.skip("No Chrome or Chromium for the browser smoke test")
    return path


def _fake_lab(monkeypatch, ws):
    """spine-leaf-fabric runs on a fake host; live reads answer at once."""
    from clabfleet.topology import load_topology

    topo = load_topology(TOPOLOGIES / "spine_leaf.clab.yml")
    containers = [{
        "lab": topo.name, "node": n, "state": "running", "status": "Up 2 minutes", "ready": True,
        "kind": topo.effective_node(n)["kind"], "host": "localhost", "container": f"clab-{topo.name}-{n}",
        "cpu": 1.0, "ram": 512,
        "image": topo.effective_node(n).get("image", ""), "ipv4": f"172.20.20.{i + 2}",
        "topo_file": str(TOPOLOGIES / "spine_leaf.clab.yml"),
    } for i, n in enumerate(topo.nodes)]
    monkeypatch.setattr(Workspace, "runtime", lambda self, max_age=0: [
        HostState("localhost", ok=True, containers=containers)])
    monkeypatch.setattr(Workspace, "host_status", lambda self: [
        {"name": "localhost", "host": "localhost", "local": True, "ok": True, "version": "0.79.0",
         "cpus": 8, "mem_total_mb": 16000, "mem_available_mb": 8000, "max_cpu": 8, "max_ram": 8000,
         "max_ram_set": False, "vtep": None, "tags": [], "error": None}])
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
    # A link that went down, for the Events tab
    for state in ("up", "down"):
        ws.events.observe("spine_leaf.clab.yml", "links",
                          {"link:x": {"kind": "link", "id": "x", "state": state, "label": "Spine-1:eth1 ↔ Leaf-1:eth1"}},
                          time.time())

    async def scenario():
        srv = TestServer(server.create_app(ws, "tok"), host="127.0.0.1")
        await srv.start_server()
        profile = tempfile.mkdtemp(prefix="clabfleet-browser-")
        log = open(os.path.join(profile, "browser.log"), "w+")
        proc = subprocess.Popen(
            # Port 0: Chrome picks a free one and prints it ("DevTools listening on ws://...")
            [browser, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}",
             "--no-first-run", "--no-default-browser-check", "--disable-gpu", "--no-sandbox",
             "--window-size=1440,900", "about:blank"],
            stdout=log, stderr=subprocess.STDOUT)
        try:
            async with aiohttp.ClientSession() as http:
                tabs = None
                deadline = time.monotonic() + 45  # a cold start on a busy CI runner is slow
                while tabs is None and proc.poll() is None and time.monotonic() < deadline:
                    log.seek(0)
                    m = re.search(r"DevTools listening on ws://[^:/]+:(\d+)/", log.read())
                    if m:
                        try:
                            async with http.get(f"http://127.0.0.1:{m[1]}/json") as resp:
                                tabs = await resp.json()
                        except aiohttp.ClientError:
                            pass
                    if tabs is None:
                        await asyncio.sleep(0.1)
                if proc.poll() is not None or tabs is None:
                    log.seek(0)
                    raise AssertionError(f"{browser} did not start its DevTools "
                                         f"(exit {proc.poll()}):\n{log.read()[-2000:]}")
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
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            log.close()
            shutil.rmtree(profile, ignore_errors=True)
            await srv.close()

    asyncio.run(scenario())


async def steps(page):
    # Start page with the lab cards
    await page.until("document.querySelectorAll('#welcome-labs .card:not(.skeleton)').length === 2")
    await page.until("document.querySelectorAll('#topo-list .lab-item').length === 2")
    # Hosts page, from the host chip: the host's capacity and the lab on it
    await page.until("!!document.querySelector('button.host-chip')")
    await page.js("document.querySelector('button.host-chip').click()")
    await page.until("!document.querySelector('#cluster').hidden && "
                     "document.querySelectorAll('.host-card').length === 1")
    assert await page.js("document.querySelector('#empty').hidden") is True
    assert await page.js("document.querySelector('#cluster-summary').textContent") == (
        "1 host · 1 lab deployed")
    assert await page.js("document.querySelectorAll('.host-card .meter').length") == 2  # memory, vCPU
    meters = await page.js("document.querySelector('.meters').innerText")
    assert "7.8 GB of 15.6 GB in use" in meters
    # Ten nodes at one vCPU each on a host that may use eight
    assert "10 of 8 counted for running labs" in meters and "over capacity" in meters
    assert "spine-leaf-fabric" in await page.js("document.querySelector('.host-labs').innerText")
    await page.js("document.querySelector('#cluster-close').click()")
    assert await page.js("document.querySelector('#empty').hidden") is False
    assert await page.js("document.querySelector('#cluster').hidden") is True
    # The deployed lab: its diagram, a node face per node, fresh live data
    await page.js("selectTopology('spine_leaf.clab.yml')")
    await page.until("document.querySelectorAll('#diagram .node').length === 10")
    assert await page.js("document.querySelectorAll('#diagram .node use.dev').length") == 10
    await page.until("document.querySelector('#diagram-fresh').textContent.startsWith('live')")
    assert "running" in await page.js("document.querySelector('#lab-stats').textContent")
    # Rack view: one rack for the one host, a device per node, a cable per link; then back
    await page.js("document.querySelector('#racks-toggle').click()")
    await page.until("document.querySelectorAll('#diagram .rack-dev').length === 10")
    assert await page.js("document.querySelectorAll('#diagram .rack').length") == 1
    assert await page.js("document.querySelectorAll('#diagram .cable').length") == \
        await page.js("document.querySelectorAll('#diagram .link-hit').length") > 0
    assert await page.js("document.body.classList.contains('racks-view')")
    await page.js("nodeClicked('Spine-1')")
    await page.until("document.querySelector('#diagram .rack-dev.selected')?.dataset.id === 'Spine-1'")
    await page.js("document.querySelector('#racks-toggle').click()")
    await page.until("!document.querySelector('#diagram .rack-dev')")
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
    # Its problems can be copied out: one line, or all that are listed.
    # (Two problems of our own, drawn and clicked in one go: the panel
    # redraws itself with the lab's real ones every few seconds.)
    problems = ("[{sev: 'error', source: 'hosts', tag: 'Hosts', msg: 'clab-2 cannot be reached: no answer'},"
                " {sev: 'warn', source: 'routing', tag: 'BGP', msg: 'Leaf-1 has a one-sided session'}]")
    await page.js("window._copied = []; "
                  "navigator.clipboard.writeText = async (text) => { window._copied.push(text); }")
    await page.js(f"renderHealth({problems}); "
                  "document.querySelector('#health-list .health-copy-one').click()")
    await page.until("window._copied.length === 1")
    assert await page.js("window._copied[0]") == "ERROR  Hosts: clab-2 cannot be reached: no answer"
    assert await page.js(f"renderHealth({problems}); document.querySelector('#health-copy').hidden") is False
    await page.js(f"renderHealth({problems}); document.querySelector('#health-copy').click()")
    await page.until("window._copied.length === 2")
    copied = (await page.js("window._copied[1]")).split("\n")
    assert copied[0].startswith("spine-leaf-fabric: 2 problems (1 error, 1 warning) at ")
    assert copied[1:] == ["ERROR  Hosts: clab-2 cannot be reached: no answer",
                          "WARN   BGP: Leaf-1 has a one-sided session"]
    # Where the browser withholds the clipboard (plain HTTP on another
    # machine), the selection is copied the older way
    await page.js("navigator.clipboard.writeText = async () => { throw new Error('denied'); }; "
                  "document.execCommand = (cmd) => { window._old = "
                  "[cmd, document.activeElement.value]; return true; }")
    await page.js(f"renderHealth({problems}); "
                  "document.querySelectorAll('#health-list .health-copy-one')[1].click()")
    await page.until("!!window._old")
    assert await page.js("window._old") == ["copy", "WARN   BGP: Leaf-1 has a one-sided session"]
    # Where it allows neither, the text is shown, selected, to copy by hand
    await page.js("document.execCommand = () => false")
    await page.js(f"renderHealth({problems}); document.querySelector('#health-copy').click()")
    await page.until("document.querySelector('#confirm').open")
    shown = await page.js("document.querySelector('#confirm .copy-text').textContent")
    assert shown.split("\n")[1:] == copied[1:]
    assert await page.js("getSelection().toString()") == shown
    await page.js("document.querySelector('#confirm-ok').click()")
    # With nothing listed there is nothing to copy
    assert await page.js("renderHealth([]); document.querySelector('#health-copy').hidden") is True
    await page.js("openDiff('spine_leaf.clab.yml', 'Leaf-1', 'running')")
    await page.until("[...document.querySelectorAll('.pane.activity-pane pre')].some(p => p.textContent.includes('No drift'))")
    # Every dock tab that is always there shows its pane when clicked
    for pane in ("activity", "health", "run", "trace", "events"):
        await page.js(f"document.querySelector('.dock-tab[data-pane=\"{pane}\"]').click()")
        assert await page.js("document.querySelector('.pane.active').dataset.pane") == pane, pane
        assert await page.js("document.querySelector('.dock-tab.active').dataset.pane") == pane
    # Events: the recorded change, in the list and on the strip
    await page.js("document.querySelector('.dock-tab[data-pane=\"events\"]').click()")
    await page.until("document.querySelectorAll('#events-list .health-row').length === 1")
    assert await page.js("document.querySelectorAll('#events-strip .ev-mark.error').length") == 1
    # The change can be copied out, by itself or with the rest of the list
    await page.js("window._copied = []; "
                  "navigator.clipboard.writeText = async (text) => { window._copied.push(text); }")
    await page.js("document.querySelector('#events-list .health-copy-one').click()")
    await page.until("window._copied.length === 1")
    line = await page.js("window._copied[0]")
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC  Spine-1:eth1 ↔ Leaf-1:eth1: up → down", line)
    assert await page.js("document.querySelector('#events-copy').hidden") is False
    await page.js("document.querySelector('#events-copy').click()")
    await page.until("window._copied.length === 2")
    assert await page.js("window._copied[1]") == f"spine-leaf-fabric: 1 change, newest first\n{line}"
    # Export: both canvases build standalone SVG in both themes
    for svg, vp in (("diagram", "viewport"), ("routing", "rt-viewport")):
        await page.js(f"showView('{svg}')")
        await page.until(f"!!document.querySelector('#{vp}')")
        for theme in ("light", "dark"):
            text = await page.js(f"window.Export.build(document.querySelector('#{svg}'), '{vp}', '{theme}').text")
            assert text.startswith("<svg") and "--bg:" in text and "<symbol" in text, svg
    await page.js("showView('diagram')")
    # Stop: on a deployed lab, asks first (cancelled here)
    await page.js("document.querySelector('[data-action=\"stop\"]').click()")
    await page.until("document.querySelector('#confirm').open")
    assert "Stop" in await page.js("document.querySelector('#confirm-title').textContent")
    await page.js("document.querySelector('#confirm-cancel').click()")
    await page.until("!document.querySelector('#confirm').open")
    # Job steps: a two-host deploy's lines, half way, then failed on one host
    steps = await page.js("""(() => {
      const st = newSteps({action: 'deploy', status: 'running'});
      const lines = ["$ deploy t.clab.yml", "» Probing 2 cluster hosts", "» Computing placement with strategy 'bin-pack'",
        "» Pulling ceos:4.35 on clab-1", "» Running containerlab deploy on clab-1", "» Running containerlab deploy on clab-2",
        "[clab-1] 19:26:17 INFO Creating container name=Leaf-1", "[clab-1] 19:26:18 INFO Created link: Spine-1:eth1 ▪┄┄▪ Leaf-1:eth1",
        "[clab-2] 19:26:17 INFO Creating container name=Leaf-2"];
      lines.forEach((l, i) => stepLine(st, l, i));
      const now = {}; for (const [hst, row] of st.rows) now[hst] = Object.fromEntries(Object.entries(row).map(([k, v]) => [k, v.state]));
      finishSteps(st, 'error', 20);
      const end = {}; for (const [hst, row] of st.rows) end[hst] = Object.fromEntries(Object.entries(row).map(([k, v]) => [k, v.state]));
      return {now, end};
    })()""")
    assert steps["now"]["lab"]["plan"] == "done" and steps["now"]["lab"]["images"] == "active"
    assert steps["now"]["clab-1"]["deploy"] == "done" and steps["now"]["clab-1"]["links"] == "active"
    assert steps["now"]["clab-2"]["deploy"] == "active" and steps["now"]["clab-2"]["links"] == "pending"
    assert steps["end"]["clab-2"]["deploy"] == "failed" and steps["end"]["clab-1"]["links"] == "failed"
    # Run on nodes: the tab opens with the selected node; the line diff
    await page.js("window.Run.open()")
    await page.until("!document.querySelector('.pane[data-pane=\"run\"]').classList.contains('active') === false")
    diff = await page.js("window.Run.lineDiff('a\\nb\\nc', 'a\\nB\\nc').map(l => l.op + l.text).join('|')")
    assert diff == " a|+B|-b| c" or diff == " a|-b|+B| c", diff
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
    await page.js("document.querySelector('#side-toggle').click()")
    for _ in range(4):  # every theme, back to the first
        await page.js("document.querySelector('#theme').click()")
        await page.until("document.querySelectorAll('#diagram .node').length > 0")
    # A lab that is not deployed
    await page.js("selectTopology('evpn_fabric.clab.yml')")
    await page.until("document.querySelectorAll('#diagram .node').length === 10")
    await page.js("showView('routing')")
    await page.until("document.querySelectorAll('#routing .node').length > 0")
    # Spare ports: add some to two nodes, then cable one to the other. The
    # lab is not deployed, so all of it goes into the file.
    await page.js("showView('diagram')")
    links = await page.js("S.detail.links.length")
    for node in ("Leaf-1", "Leaf-2"):
        await page.js(f"selectNode('{node}')")
        await page.until("!!document.querySelector('#node-card .spare-count')")
        assert "null" not in await page.js("document.querySelector('#node-card').textContent")
        await page.js("document.querySelector('#node-card .spare-count').value = '2'; "
                      "document.querySelector('#node-card .spare-count').form.requestSubmit()")
        await page.until(f"(S.detail.nodes.find(n => n.name === '{node}').spare || []).length === 2")
    await page.js("selectNode('Leaf-1')")
    await page.until("document.querySelectorAll('#node-card .spare-form select').length === 2")
    spare = await page.js("document.querySelector('#node-card .spare-list').textContent")
    first, second = spare.split()
    assert await page.js("document.querySelectorAll('#node-card .spare-form select')[1]"
                         ".selectedOptions[0].textContent") == f"Leaf-2:{first}"   # other nodes first
    await page.js("document.querySelector('#node-card .spare-form').requestSubmit()")
    await page.until("document.querySelector('#confirm').open")
    assert await page.js("document.querySelector('#confirm-title').textContent") == (
        f"Cable Leaf-1:{first} to Leaf-2:{first}?")
    assert "at the next deploy" in await page.js("document.querySelector('#confirm-body').textContent")
    await page.js("document.querySelector('#confirm-ok').click()")
    await page.until(f"S.detail.links.length === {links + 1}")
    assert await page.js("S.detail.nodes.find(n => n.name === 'Leaf-1').spare") == [second]
    assert f'["Leaf-1:{first}", "Leaf-2:{first}"]' in await page.js("S.detail.yaml")
    # The rack view shows what is left as empty jacks
    await page.js("document.querySelector('#racks-toggle').click()")
    await page.until("document.querySelectorAll('#diagram .jack.spare').length === 2")
    await page.js("document.querySelector('#racks-toggle').click()")
    await asyncio.sleep(0.5)  # let late renders throw, if they would
