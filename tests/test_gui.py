import asyncio
import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from clabfleet.cluster import ClusterConfig, HostInfo  # noqa: E402
from clabfleet.deployer import LabDeployer  # noqa: E402
from clabfleet.gui import server, state  # noqa: E402
from clabfleet.gui.state import (  # noqa: E402
    JobManager,
    Workspace,
    find_topologies,
    topology_view,
)
from clabfleet.nodes import access_modes, parse_inspect, terminal_command  # noqa: E402
from clabfleet.topology import load_topology, topology_from_dict  # noqa: E402

TOPO = """\
name: t
topology:
  nodes:
    a: {kind: linux, image: alpine}
    b: {kind: linux, image: alpine}
"""


def test_find_topologies_skips_lab_and_hidden_dirs(tmp_path):
    (tmp_path / "labs").mkdir()
    (tmp_path / "labs" / "one.clab.yml").write_text(TOPO)
    (tmp_path / "two.clab.yaml").write_text(TOPO)
    for skipped in ("clab-t", ".venv", ".git", "node_modules"):
        (tmp_path / skipped).mkdir()
        (tmp_path / skipped / "x.clab.yml").write_text(TOPO)
    (tmp_path / "not-a-topology.yml").write_text(TOPO)

    assert sorted(find_topologies([tmp_path])) == ["labs/one.clab.yml", "two.clab.yaml"]

    other = tmp_path / "other"
    other.mkdir()
    (other / "three.clab.yml").write_text(TOPO)
    ids = find_topologies([tmp_path / "labs", other])
    assert sorted(ids) == ["labs/one.clab.yml", "other/three.clab.yml"]


def test_find_topologies_ignores_symlinks_out_of_the_workspace(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.clab.yml").write_text(TOPO)
    root = tmp_path / "ws"
    (root / "labs").mkdir(parents=True)
    (root / "labs" / "real.clab.yml").write_text(TOPO)
    (root / "evil.clab.yml").symlink_to(outside / "secret.clab.yml")
    (root / "alias.clab.yml").symlink_to(root / "labs" / "real.clab.yml")  # inside: fine
    (root / "linked-dir").symlink_to(outside)  # not walked into
    assert sorted(find_topologies([root])) == ["alias.clab.yml", "labs/real.clab.yml"]

    # Swapped for a symlink after the scan: refused when it is read
    ws = Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [root])
    ws.topologies()
    (root / "labs" / "real.clab.yml").unlink()
    (root / "labs" / "real.clab.yml").symlink_to(outside / "secret.clab.yml")
    with pytest.raises(KeyError, match="outside the workspace"):
        ws.topology_detail("labs/real.clab.yml")


def test_find_node_only_in_workspace_labs(tmp_path, monkeypatch):
    (tmp_path / "t.clab.yml").write_text(TOPO)
    ws = Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])
    containers = [{"lab": lab, "node": "a", "kind": "linux", "container": f"clab-{lab}-a",
                   "ipv4": "", "host": "localhost"} for lab in ("t", "private")]
    monkeypatch.setattr(Workspace, "runtime", lambda self: [
        state.HostState("localhost", ok=True, containers=containers)])
    assert ws.find_node("t", "a")["container"] == "clab-t-a"
    with pytest.raises(KeyError, match="not in this workspace"):
        ws.find_node("private", "a")

    # A viewer cannot follow logs of a lab that is not in the workspace
    from clabfleet.gui.auth import UserStore
    users = UserStore(tmp_path / "users.yaml")
    viewer = users.add("bob", "viewer")

    async def scenario():
        app = server.create_app(ws, users=users)
        async with TestClient(TestServer(app)) as client:
            client.session.cookie_jar.clear()
            await client.post("/login", json={"token": viewer})
            sock = await client.ws_connect("/ws/terminal?lab=private&node=a&mode=logs")
            out = b""
            async for msg in sock:
                if msg.type.name == "BINARY":
                    out += msg.data
            return out.decode()

    assert "not in this workspace" in asyncio.run(scenario())


def test_topology_view_links_and_special_endpoints():
    topo = topology_from_dict({
        "name": "t",
        "topology": {
            "nodes": {
                "s1": {"kind": "arista_ceos", "labels": {"lab.host": "h1", "graph-posX": "10", "graph-posY": "20"}},
                "l1": {"kind": "linux"},
            },
            "links": [
                {"endpoints": ["s1:eth1", "l1:eth1"]},
                {"endpoints": ["l1:eth2", "host:l1-eth2"]},
                {"type": "dummy", "endpoint": {"node": "s1", "interface": "eth9"}},
            ],
        },
    })
    view = topology_view(topo)
    s1 = view["nodes"][0]
    assert s1["host_pin"] == "h1"
    assert s1["pos"] == [10.0, 20.0]
    assert s1["modes"] == ["cli", "shell", "ssh"]
    assert [(l["a"], l["b"]) for l in view["links"]] == [
        ({"node": "s1", "iface": "eth1"}, {"node": "l1", "iface": "eth1"}),
        ({"node": "l1", "iface": "eth2"}, {"special": "host", "iface": "l1-eth2"}),
        ({"node": "s1", "iface": "eth9"}, {"special": "dummy", "iface": ""}),
    ]


def test_parse_inspect_details():
    data = {"lab1": [{
        "Names": ["clab-lab1-r1"],
        "Labels": {"containerlab": "lab1", "clab-node-name": "r1", "clab-node-kind": "arista_ceos",
                   "clab-topo-file": "/x/lab1.clab.yml"},
        "State": "running", "Status": "Up 5 minutes", "Image": "ceos:4.35.6M",
        "NetworkSettings": {"IPv4addr": "172.20.20.2"},
    }]}
    assert parse_inspect(data, "h1") == [{
        "id": "", "lab": "lab1", "node": "r1", "container": "clab-lab1-r1", "kind": "arista_ceos",
        "image": "ceos:4.35.6M", "state": "running", "status": "Up 5 minutes",
        "ipv4": "172.20.20.2", "topo_file": "/x/lab1.clab.yml", "host": "h1",
    }]
    assert parse_inspect({}, "h1") == []


def test_access_modes_and_commands():
    assert access_modes("arista_ceos") == ["cli", "shell", "ssh"]
    assert access_modes("cisco_iol") == ["ssh", "shell"]
    assert access_modes("linux") == ["shell", "ssh"]
    assert access_modes("bridge") == []
    assert terminal_command("cli", "arista_ceos", "c1", "") == ["docker", "exec", "-it", "c1", "Cli"]
    assert terminal_command("shell", "linux", "c1", "")[:4] == ["docker", "exec", "-it", "c1"]
    assert terminal_command("ssh", "cisco_iol", "c1", "172.20.20.3")[-1] == "admin@172.20.20.3"
    with pytest.raises(ValueError):
        terminal_command("cli", "linux", "c1", "")
    with pytest.raises(ValueError):
        terminal_command("ssh", "linux", "c1", "")


def _workspace(tmp_path):
    (tmp_path / "t.clab.yml").write_text(TOPO)
    return Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])


def _wait(job):
    for _ in range(100):
        if job.status != "running":
            return
        time.sleep(0.02)


def test_job_manager_runs_labs_in_parallel_and_keeps_history(tmp_path, monkeypatch):
    calls = []
    release = threading.Event()

    class FakeDeployer:
        def __init__(self, cluster, on_output=None, interactive_sudo=True):
            assert interactive_sudo is False
            self.on_output = on_output

        def deploy(self, path, reconfigure=False, rollback=False):
            calls.append(("deploy", Path(path).name, reconfigure, rollback))
            self.on_output("\x1b[1mINFO\x1b[0m Creating container")
            release.wait(5)
            return {"hosts": {"localhost": {"status": "deployed", "seconds": 1.5}}}

        def destroy(self, path, cleanup=True):
            return {"hosts": {"localhost": {"error": "boom"}}}

    monkeypatch.setattr(state, "LabDeployer", FakeDeployer)
    ws = _workspace(tmp_path)
    (tmp_path / "u.clab.yml").write_text(TOPO.replace("name: t", "name: u"))
    (tmp_path / "t-copy.clab.yml").write_text(TOPO)  # same lab name "t"
    ws.topologies()
    jobs = JobManager(ws)

    job = jobs.start("redeploy", "t.clab.yml", {"rollback": 1, "format": True})
    assert job.options == {"rollback": True}  # unknown options dropped
    assert job.lab == "t"
    with pytest.raises(RuntimeError, match="already running for lab 't'"):
        jobs.start("destroy", "t.clab.yml")
    with pytest.raises(RuntimeError, match="already running for lab 't'"):
        jobs.start("deploy", "t-copy.clab.yml")
    other = jobs.start("deploy", "u.clab.yml")  # a different lab runs alongside
    assert {j.id for j in jobs.running()} == {job.id, other.id}

    release.set()
    _wait(job)
    _wait(other)
    assert job.status == other.status == "ok"
    assert sorted(calls) == [("deploy", "t.clab.yml", True, True),
                             ("deploy", "u.clab.yml", False, False)]
    assert "INFO Creating container" in job.lines  # ANSI codes stripped
    assert "» time per host: localhost 1.5s" in job.lines
    assert job.summary()["host_times"] == {"localhost": 1.5}

    job2 = jobs.start("destroy", "t.clab.yml")
    _wait(job2)
    assert job2.status == "error"
    assert "✗ localhost: boom" in job2.lines

    monkeypatch.setattr(JobManager, "MAX_RUNNING", 0)
    with pytest.raises(RuntimeError, match="wait for one to finish"):
        jobs.start("deploy", "u.clab.yml")
    monkeypatch.setattr(JobManager, "MAX_RUNNING", 4)
    with pytest.raises(ValueError):
        jobs.start("format-disk", "t.clab.yml")
    with pytest.raises(KeyError):
        jobs.start("deploy", "../../etc/passwd")

    # History survives a restart; a job that was running is marked interrupted
    history_dir = tmp_path / ".clabfleet" / "jobs"
    stuck = state.Job("stuck", "deploy", "u.clab.yml", lab="u", started=1.0)
    (history_dir / "stuck.json").write_text(json.dumps(stuck.to_dict()))
    (history_dir / "junk.json").write_text("{not json")
    reloaded = JobManager(ws)
    ids = [j.id for j in reloaded.recent()]
    assert set(ids) == {job.id, other.id, job2.id, "stuck"}
    assert ids[0] == job2.id and ids[-1] == "stuck"  # newest first
    assert reloaded.jobs[job.id].lines == job.lines
    assert reloaded.jobs["stuck"].status == "interrupted"


def test_job_history_keeps_newest(tmp_path):
    history = state.JobHistory(tmp_path / "jobs", keep=3)
    for i in range(5):
        job = state.Job(f"j{i}", "save", "t", started=float(i), status="ok")
        history.save(job)
        os.utime(tmp_path / "jobs" / f"j{i}.json", (i, i))
    assert [j.id for j in history.load()] == ["j2", "j3", "j4"]
    assert state.JobHistory(None).load() == []


def test_job_history_is_private_and_not_redirected(tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir(mode=0o755)
    jobs.chmod(0o755)
    target = tmp_path / "victim"
    target.write_text("untouched")
    (jobs / ".j1.tmp").symlink_to(target)  # the old fixed temporary name
    (jobs / "planted.json").symlink_to(target)
    history = state.JobHistory(jobs)
    history.save(state.Job("j1", "save", "t", status="ok", lines=["x"]))
    assert target.read_text() == "untouched"
    assert stat.S_IMODE(jobs.stat().st_mode) == 0o700
    assert stat.S_IMODE((jobs / "j1.json").stat().st_mode) == 0o600
    assert not [p for p in jobs.iterdir() if p.name.endswith(".tmp") and not p.is_symlink()]
    assert [(j.id, j.lines) for j in history.load()] == [("j1", ["x"])]


def test_server_requires_token_and_same_origin(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    monkeypatch.setattr(Workspace, "runtime", lambda self: [])

    async def scenario():
        app = server.create_app(ws, "secret-token")
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/api/state")).status == 401
            assert (await client.get("/")).status == 200  # the page, with its login form
            assert (await client.post("/login", json={"token": "wrong"})).status == 401

            resp = await client.post("/login", json={"token": "secret-token"})
            assert resp.status == 200
            assert "SameSite=Strict" in resp.headers["Set-Cookie"]

            resp = await client.get("/api/state")
            assert resp.status == 200
            body = await resp.json()
            assert [t["id"] for t in body["topologies"]] == ["t.clab.yml"]
            assert body["jobs"] == []
            assert await (await client.get("/api/jobs")).json() == []

            assert (await client.get("/static/app.js")).status == 200
            resp = await client.post("/api/jobs", json={"action": "save", "topology": "t.clab.yml"},
                                     headers={"Origin": "http://evil.example"})
            assert resp.status == 403
            resp = await client.post("/api/jobs", json={"action": "nope", "topology": "t.clab.yml"})
            assert resp.status == 400
            for path in ("/api/jobs", "/api/validate/t.clab.yml"):
                assert (await client.post(path, data="not json")).status == 400
                assert (await client.post(path, json=["a", "list"])).status == 400
            resp = await client.get("/api/topologies/t.clab.yml")
            detail = await resp.json()
            assert detail["nodes"][0]["name"] == "a"
            assert detail["placement"] is None
            (tmp_path / "t.placement.json").write_text(
                '{"lab": "t", "hosts": {"localhost": {}}, "nodes": {"a": "localhost"}}')
            detail = await (await client.get("/api/topologies/t.clab.yml")).json()
            assert detail["placement"]["nodes"] == {"a": "localhost"}
            assert (await client.get("/api/topologies/missing.clab.yml")).status == 404

    asyncio.run(scenario())


def test_job_outcome_lines():
    job = state.Job("j", "deploy", "t")
    job.add_outcome({"status": "partial", "hosts": {"h1": {"status": "deployed"}, "h2": {"error": "x"}}})
    job.add_outcome({"status": "rolled-back", "hosts": {}, "rollback": {"h1": "ok", "h2": "ok"}})
    job.add_outcome({"status": "rollback-failed", "hosts": {}, "rollback": {"h1": "ok", "h2": "error: y"}})
    job.add_outcome({"status": "deployed", "hosts": {"h1": {}}})
    assert job.lines == [
        "✗ partly deployed: running on h1, failed on h2. Destroy the lab to clean up.",
        "↺ rolled back: removed from h1, h2",
        "✗ rollback incomplete on h2 (error: y). Destroy the lab to clean up.",
    ]


def test_logs_mode_command():
    assert terminal_command("logs", "cisco_iol", "clab-l-r1", "") == [
        "docker", "logs", "--follow", "--tail", "2000", "clab-l-r1"]


def test_logs_terminal_over_websocket(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text('#!/bin/sh\necho "args: $*"\necho "booting line 1"\n')
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    ws = _workspace(tmp_path)
    monkeypatch.setattr(Workspace, "find_node", lambda self, lab, node: {
        "kind": "cisco_iol", "container": "clab-l-r1", "ipv4": "", "host": "localhost"})

    async def scenario():
        app = server.create_app(ws, "tok")
        async with TestClient(TestServer(app)) as client:
            await client.post("/login", json={"token": "tok"})
            sock = await client.ws_connect("/ws/terminal?lab=l&node=r1&mode=logs&cols=80&rows=24")
            output, exit_msg = b"", None
            async for msg in sock:
                if msg.type.name == "BINARY":
                    output += msg.data
                elif msg.type.name == "TEXT":
                    exit_msg = json.loads(msg.data)
            return output.decode(), exit_msg

    output, exit_msg = asyncio.run(scenario())
    assert "args: logs --follow --tail 2000 clab-l-r1" in output
    assert "booting line 1" in output
    assert exit_msg == {"t": "exit", "code": 0}


def test_topologies_say_when_a_kept_lab_directory_had_configs_saved(tmp_path):
    ws = _workspace(tmp_path)
    assert ws.topologies()[0]["saved_at"] is None  # never deployed: no lab directory
    lab = ws.topologies()[0]["name"]
    node = next(iter(load_topology(tmp_path / "t.clab.yml").nodes))
    kept = tmp_path / f"clab-{lab}" / node
    kept.mkdir(parents=True)
    os.utime(kept, (1000, 1000))
    assert ws.topologies()[0]["saved_at"] == 1000
    # Several hosts: the lab directory is not next to the file, nothing is said
    many = Workspace(ClusterConfig(hosts=[HostInfo("localhost"), HostInfo("b", host="10.0.0.2")]), [tmp_path])
    assert many.topologies()[0]["saved_at"] is None


def test_stop_saves_first_and_keeps_the_lab_directory(tmp_path, monkeypatch):
    calls = []
    save_fails = [False]

    class FakeDeployer:
        def __init__(self, cluster, on_output=None, interactive_sudo=True):
            pass

        def save(self, path):
            calls.append("save")
            if save_fails[0]:
                return {"hosts": {"localhost": {"error": "node unreachable", "seconds": 0.1}}}
            return {"hosts": {"localhost": {"status": "saved", "seconds": 0.2}}}

        def destroy(self, path, cleanup=True):
            calls.append(("destroy", cleanup))
            return {"hosts": {"localhost": {"status": "ok", "seconds": 0.5}}}

        stop = LabDeployer.stop  # the real one, over the fake save and destroy

    monkeypatch.setattr(state, "LabDeployer", FakeDeployer)
    ws = _workspace(tmp_path)
    ws.topologies()
    jobs = JobManager(ws)
    job = jobs.start("stop", "t.clab.yml")
    _wait(job)
    assert job.status == "ok" and calls == ["save", ("destroy", False)]
    job = jobs.start("destroy", "t.clab.yml")
    _wait(job)
    assert calls[-1] == ("destroy", True)
    # A failed save removes nothing
    calls.clear()
    save_fails[0] = True
    job = jobs.start("stop", "t.clab.yml")
    _wait(job)
    assert job.status == "error" and calls == ["save"]
    assert any("not stopped" in line for line in job.lines)
