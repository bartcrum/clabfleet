import asyncio
import json
import os
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from clabfleet.cluster import ClusterConfig, HostInfo  # noqa: E402
from clabfleet.gui import server, state  # noqa: E402
from clabfleet.gui.state import (  # noqa: E402
    JobManager,
    Workspace,
    find_topologies,
    topology_view,
)
from clabfleet.nodes import access_modes, parse_inspect, terminal_command  # noqa: E402
from clabfleet.topology import topology_from_dict  # noqa: E402

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

        def destroy(self, path):
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


def test_server_requires_token_and_same_origin(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    monkeypatch.setattr(Workspace, "runtime", lambda self: [])

    async def scenario():
        app = server.create_app(ws, "secret-token")
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/api/state")).status == 401
            assert (await client.get("/")).status == 401
            assert (await client.get("/?token=wrong", allow_redirects=False)).status == 401

            resp = await client.get("/?token=secret-token", allow_redirects=False)
            assert resp.status == 302
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
            await client.get("/?token=tok", allow_redirects=False)
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
