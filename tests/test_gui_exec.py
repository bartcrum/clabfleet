import asyncio
import json

import pytest

pytest.importorskip("aiohttp")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from clabfleet import execute  # noqa: E402
from clabfleet.cluster import ClusterConfig, HostInfo  # noqa: E402
from clabfleet.gui import server  # noqa: E402
from clabfleet.gui.auth import AuditLog, UserStore  # noqa: E402
from clabfleet.gui.state import Workspace  # noqa: E402

TOPO = "name: t\ntopology:\n  nodes:\n    R1: {kind: linux, image: a}\n    R2: {kind: linux, image: a}\n"


def test_run_on_nodes(tmp_path, monkeypatch):
    (tmp_path / "t.clab.yml").write_text(TOPO)
    ws = Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])
    users = UserStore(tmp_path / "users.yaml")
    op, viewer = users.add("op"), users.add("vic", "viewer")
    calls = []

    def run(self, path, command, nodes=None, mode="auto"):
        calls.append((path.name, command, nodes, mode, self.timeout))
        if nodes == ["Z*"]:
            raise ValueError("No node of lab 't' matches 'Z*'")
        return {"lab": "t", "command": command, "host_errors": {}, "results": [
            {"node": "R1", "host": "localhost", "kind": "linux", "mode": "shell", "exit_code": 0,
             "output": "x" * (server.EXEC_MAX_OUTPUT + 10), "error": ""},
            {"node": "R2", "host": "", "kind": "linux", "mode": "", "exit_code": None,
             "output": "", "error": "not running"}]}

    monkeypatch.setattr(execute.LabExecutor, "run", run)
    audit_path = tmp_path / "audit.jsonl"

    async def scenario():
        app = server.create_app(ws, users=users, audit=AuditLog(audit_path))
        async with TestClient(TestServer(app)) as client:
            await client.post("/login", json={"token": viewer})
            assert (await client.post("/api/exec/t.clab.yml", json={"command": "uname"})).status == 403
            await client.post("/logout")
            await client.post("/login", json={"token": op})
            resp = await client.post("/api/exec/t.clab.yml",
                                     json={"command": "uname -a", "nodes": ["R*", " "], "mode": "shell"})
            body = await resp.json()
            assert resp.status == 200 and calls[-1] == ("t.clab.yml", "uname -a", ["R*"], "shell", server.EXEC_TIMEOUT)
            r1, r2 = body["results"]
            assert r1["output"].endswith("[output cut]\n") and len(r1["output"]) < server.EXEC_MAX_OUTPUT + 20
            assert r2["error"] == "not running"
            resp = await client.post("/api/exec/t.clab.yml", json={"command": "x"})
            assert resp.status == 200 and calls[-1][2] is None  # no patterns: every node
            for body, status in [({"command": "  "}, 400), ({"command": "x" * 2001}, 400),
                                 ({"command": "x", "mode": "telnet"}, 400), ({"command": "x", "nodes": "R1"}, 400),
                                 ({"command": "x", "nodes": ["Z*"]}, 400)]:
                assert (await client.post("/api/exec/t.clab.yml", json=body)).status == status, body
            assert (await client.post("/api/exec/nope.clab.yml", json={"command": "x"})).status == 404

    asyncio.run(scenario())
    execs = [json.loads(line) for line in audit_path.read_text().splitlines() if '"exec"' in line]
    assert execs[0]["user"] == "op" and execs[0]["details"]["command"] == "uname -a"
