"""Named GUI users, roles, the audit log and remote-access safeguards."""

import asyncio
import json
import os
import re
import stat
import time

import pytest

pytest.importorskip("aiohttp")

from aiohttp import WSServerHandshakeError, web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request  # noqa: E402

from clabfleet import cli  # noqa: E402
from clabfleet.cluster import ClusterConfig, HostInfo  # noqa: E402
from clabfleet.gui import auth, server, state  # noqa: E402
from clabfleet.gui.auth import AuditLog, UserStore, allow_viewer, operator_only  # noqa: E402
from clabfleet.gui.state import Workspace  # noqa: E402

TOPO = """\
name: t
topology:
  nodes:
    a: {kind: linux, image: alpine}
"""


# ----------------------------------------------------------------------
# Users file
# ----------------------------------------------------------------------

def test_user_store_add_rotate_remove(tmp_path):
    path = tmp_path / "conf" / "users.yaml"
    store = UserStore(path)
    alice = store.add("alice")
    bob = store.add("bob", "viewer")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    assert alice not in text and bob not in text  # only hashes are stored
    assert auth.hash_token(alice) in text

    users = store.users()
    assert {n: u.role for n, u in users.items()} == {"alice": "operator", "bob": "viewer"}
    assert users["alice"].created.endswith("Z")
    assert store.authenticate(alice).name == "alice"
    assert store.authenticate(bob).name == "bob"
    assert store.authenticate("nope") is None
    assert store.authenticate("") is None

    with pytest.raises(ValueError, match="already exists"):
        store.add("alice")
    with pytest.raises(ValueError, match="Role"):
        store.add("carol", "admin")
    with pytest.raises(ValueError, match="User names"):
        store.add("../evil")

    new = store.rotate("alice")
    assert store.authenticate(alice) is None
    assert store.authenticate(new).name == "alice"

    store.remove("bob")
    assert store.authenticate(bob) is None
    with pytest.raises(KeyError):
        store.remove("bob")
    with pytest.raises(KeyError):
        store.rotate("bob")


def test_user_store_rereads_changes_and_fails_closed(tmp_path):
    path = tmp_path / "users.yaml"
    gui_view = UserStore(path)
    assert gui_view.users() == {}
    token = UserStore(path).add("alice")  # e.g. `clabfleet user add` while the GUI runs
    assert gui_view.authenticate(token).name == "alice"

    path.write_text("users:\n  alice: {role: root, token_sha256: x}\n")
    assert gui_view.authenticate(token) is None
    with pytest.raises(ValueError, match="role"):
        gui_view.validate()


def test_user_cli(tmp_path, capsys):
    users = str(tmp_path / "users.yaml")
    assert cli.main(["user", "add", "alice", "--users", users,
                     "--url", "https://lab.example.com:8650"]) == 0
    out = capsys.readouterr().out
    token = out.split("?token=")[1].split()[0]
    assert UserStore(users).authenticate(token).role == "operator"
    assert "https://lab.example.com:8650/?token=" in out

    assert cli.main(["user", "add", "bob", "--role", "viewer", "--users", users]) == 0
    capsys.readouterr()
    assert cli.main(["user", "list", "--users", users]) == 0
    listing = capsys.readouterr().out
    assert "alice" in listing and "viewer" in listing and token not in listing

    assert cli.main(["user", "rotate", "alice", "--users", users]) == 0
    assert UserStore(users).authenticate(token) is None
    assert cli.main(["user", "remove", "bob", "--users", users]) == 0
    assert set(UserStore(users).users()) == {"alice"}
    assert cli.main(["user", "remove", "bob", "--users", users]) == 1


def test_audit_log_lines(tmp_path):
    log = AuditLog(tmp_path / "a" / "audit.jsonl")
    log.record("login", auth.User("alice", "operator"), "10.0.0.5")
    log.record("job_finished", "bob", None, status="ok")
    AuditLog(None).record("ignored")  # off without a path
    lines = [json.loads(line) for line in (tmp_path / "a" / "audit.jsonl").read_text().splitlines()]
    assert lines[0]["user"] == "alice" and lines[0]["role"] == "operator"
    assert lines[0]["remote"] == "10.0.0.5" and lines[0]["event"] == "login"
    assert lines[1] == {**lines[1], "user": "bob", "role": None, "details": {"status": "ok"}}
    assert stat.S_IMODE((tmp_path / "a" / "audit.jsonl").stat().st_mode) == 0o600


# ----------------------------------------------------------------------
# Server
# ----------------------------------------------------------------------

def _workspace(tmp_path):
    (tmp_path / "t.clab.yml").write_text(TOPO)
    return Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])


def _fake_docker(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text('#!/bin/sh\necho "args: $*"\n')
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setattr(Workspace, "find_node", lambda self, lab, node: {
        "kind": "linux", "container": "clab-t-a", "ipv4": "", "host": "localhost"})


class FakeDeployer:
    def __init__(self, cluster, on_output=None, interactive_sudo=True):
        pass

    def save(self, path):
        return {"hosts": {"localhost": {"status": "saved"}}}


async def _login(client, token):
    client.session.cookie_jar.clear()
    return await client.get(f"/?token={token}", allow_redirects=False)


async def _drain(sock):
    out = b""
    async for msg in sock:
        if msg.type.name == "BINARY":
            out += msg.data
    return out.decode()


def _events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_roles_sessions_and_audit(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    monkeypatch.setattr(Workspace, "runtime", lambda self: [])
    monkeypatch.setattr(state, "LabDeployer", FakeDeployer)
    _fake_docker(tmp_path, monkeypatch)
    users = UserStore(tmp_path / "users.yaml")
    op_token = users.add("alice", "operator")
    view_token = users.add("bob", "viewer")
    audit_path = tmp_path / "audit.jsonl"

    async def dummy(request):
        return web.json_response({"ok": True})

    @allow_viewer
    async def dummy_open(request):
        return web.json_response({"ok": True})

    @operator_only
    async def dummy_secret(request):
        return web.json_response({"ok": True})

    async def scenario():
        app = server.create_app(ws, users=users, audit=AuditLog(audit_path))
        # Routes another feature might add later, without any auth code
        app.router.add_post("/api/dummy", dummy)
        app.router.add_delete("/api/dummy", dummy)
        app.router.add_get("/ws/dummy", dummy)
        app.router.add_post("/api/dummy-open", dummy_open)
        app.router.add_get("/api/dummy-secret", dummy_secret)
        async with TestClient(TestServer(app)) as client:
            # Not logged in
            assert (await client.get("/api/state")).status == 401
            assert (await client.get("/")).status == 401
            assert (await _login(client, "wrong")).status == 401
            assert (await client.get("/api/me")).status == 401

            # Viewer: reads yes, anything else no
            resp = await _login(client, view_token)
            assert resp.status == 302
            cookie = resp.headers["Set-Cookie"]
            assert "clabfleet_session=" in cookie and "HttpOnly" in cookie
            assert "SameSite=Strict" in cookie and view_token not in cookie
            assert await (await client.get("/api/me")).json() == {
                "user": "bob", "role": "viewer", "multi_user": True}
            for path in ("/", "/api/state", "/api/jobs", "/api/topologies/t.clab.yml"):
                assert (await client.get(path)).status == 200, path
            denied = [
                client.post("/api/jobs", json={"action": "save", "topology": "t.clab.yml"}),
                client.put("/api/topologies/t.clab.yml", json={"yaml": TOPO}),
                client.put("/api/positions/t.clab.yml", json={"positions": {}}),
                client.post("/api/validate/t.clab.yml", json={"yaml": TOPO}),
                client.get("/ws/terminal?lab=t&node=a&mode=shell"),
                client.post("/api/dummy"),
                client.delete("/api/dummy"),
                client.get("/ws/dummy"),
                client.get("/api/dummy-secret"),
            ]
            for request in denied:
                assert (await request).status == 403
            with pytest.raises(WSServerHandshakeError):
                await client.ws_connect("/ws/terminal?lab=t&node=a&mode=cli")
            assert (await client.post("/api/dummy-open")).status == 200
            # Logs are read-only, so viewers may follow them
            sock = await client.ws_connect("/ws/terminal?lab=t&node=a&mode=logs")
            assert "args: logs --follow" in await _drain(sock)
            assert (await client.post("/logout")).status == 200
            assert (await client.get("/api/state")).status == 401

            # Operator: everything
            resp = await _login(client, op_token)
            assert resp.status == 302
            assert (await client.post("/api/dummy")).status == 200
            assert (await client.get("/api/dummy-secret")).status == 200
            resp = await client.post("/api/jobs", json={"action": "save", "topology": "t.clab.yml"})
            assert resp.status == 200
            job_id = (await resp.json())["id"]
            for _ in range(100):
                job = await (await client.get(f"/api/jobs/{job_id}")).json()
                if job["status"] != "running":
                    break
                await asyncio.sleep(0.02)
            assert job["status"] == "ok" and job["user"] == "alice"
            detail = await (await client.get("/api/topologies/t.clab.yml")).json()
            resp = await client.put("/api/topologies/t.clab.yml", json={
                "yaml": TOPO + "# edited\n", "base_hash": detail["hash"]})
            assert resp.status == 200
            new_hash = (await resp.json())["detail"]["hash"]
            resp = await client.put("/api/positions/t.clab.yml", json={
                "positions": {"a": [1, 2]}, "base_hash": new_hash})
            assert resp.status == 200
            sock = await client.ws_connect("/ws/terminal?lab=t&node=a&mode=shell")
            assert re.search(r"args: exec -it -e CLABFLEET_TERMINAL=\w+ clab-t-a",
                             await _drain(sock))

            # Rotating a token ends that user's sessions at once
            users.rotate("alice")
            os.utime(users.path, ns=(time.time_ns() + 10**9,) * 2)  # same-second writes
            assert (await client.get("/api/state")).status == 401

    asyncio.run(scenario())

    events = _events(audit_path)
    by_event = {}
    for e in events:
        by_event.setdefault(e["event"], []).append(e)
    assert [e["details"] for e in by_event["login_failed"]] == [{"reason": "invalid token"}]
    assert [e["user"] for e in by_event["login"]] == ["bob", "alice"]
    assert all(e["remote"] == "127.0.0.1" for e in by_event["login"])
    assert by_event["logout"][0]["user"] == "bob"
    assert {e["details"]["path"] for e in by_event["denied"]} >= {
        "/api/jobs", "/api/topologies/t.clab.yml", "/ws/terminal", "/api/dummy"}
    started, = by_event["job_started"]
    assert started["user"] == "alice"
    assert started["details"] == {**started["details"], "action": "save",
                                  "topology": "t.clab.yml", "lab": "t", "options": {}}
    finished, = by_event["job_finished"]
    assert finished["user"] == "alice" and finished["details"]["status"] == "ok"
    assert by_event["topology_saved"][0]["details"]["topology"] == "t.clab.yml"
    assert by_event["positions_saved"][0]["details"]["nodes"] == ["a"]
    opened = [(e["user"], e["details"]["mode"]) for e in by_event["terminal_opened"]]
    assert opened == [("bob", "logs"), ("alice", "shell")]
    closed = by_event["terminal_closed"]
    assert [e["details"]["node"] for e in closed] == ["a", "a"]
    assert all(e["details"]["exit_code"] == 0 and e["details"]["seconds"] >= 0 for e in closed)

    # The job history remembers who ran the job
    history = state.JobManager(ws).jobs
    assert history[started["details"]["job"]].user == "alice"


def test_removed_user_and_no_users(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    users = UserStore(tmp_path / "users.yaml")
    token = users.add("alice", "viewer")

    async def scenario():
        app = server.create_app(ws, users=users)
        async with TestClient(TestServer(app)) as client:
            assert (await _login(client, token)).status == 302
            assert (await client.get("/api/me")).status == 200
            users.remove("alice")
            os.utime(users.path, ns=(time.time_ns() + 10**9,) * 2)
            assert (await client.get("/api/me")).status == 401
            # The single-token login does not exist in this mode
            assert (await _login(client, "")).status == 401

    asyncio.run(scenario())


def test_single_token_mode_is_one_operator(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)

    async def scenario():
        app = server.create_app(ws, "tok")
        async with TestClient(TestServer(app)) as client:
            resp = await _login(client, "tok")
            assert "clabfleet_token=tok" in resp.headers["Set-Cookie"]
            assert await (await client.get("/api/me")).json() == {
                "user": None, "role": "operator", "multi_user": False}
            resp = await client.get("/api/state")
            assert resp.headers["X-Frame-Options"] == "DENY"

    monkeypatch.setattr(Workspace, "runtime", lambda self: [])
    asyncio.run(scenario())


@pytest.mark.parametrize("origin, public_url, ok", [
    ("http://lab.example.com:8650", None, True),
    ("http://LAB.example.com:8650", None, True),
    ("http://lab.example.com", None, False),
    ("https://lab.example.com:8650", None, False),
    ("http://evil.example:8650", None, False),
    ("null", None, False),
    ("https://lab.example.com", "https://lab.example.com", True),
    ("https://lab.example.com:443", "https://lab.example.com/", True),
    ("https://evil.example", "https://lab.example.com", False),
])
def test_origin_check(tmp_path, origin, public_url, ok):
    app = server.create_app(_workspace(tmp_path), "tok", public_url=public_url)
    request = make_mocked_request("POST", "/api/jobs", app=app, headers={
        "Host": "lab.example.com:8650", "Origin": origin})
    assert server._same_origin(request) is ok


# ----------------------------------------------------------------------
# `clabfleet gui` start-up checks
# ----------------------------------------------------------------------

@pytest.fixture
def fake_run(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "run", lambda *a, **kw: calls.append(kw))
    return calls


def test_gui_refuses_remote_plain_http(tmp_path, fake_run, capsys):
    base = ["gui", "--dir", str(tmp_path), "--no-browser", "--users", str(tmp_path / "u.yaml")]
    UserStore(tmp_path / "u.yaml").add("alice")

    assert cli.main([*base, "--bind", "0.0.0.0"]) == 1
    assert "without TLS" in capsys.readouterr().err
    assert cli.main([*base, "--bind", "lab.example.com"]) == 1
    assert cli.main([*base, "--bind", "0.0.0.0", "--tls-cert", "x.pem"]) == 1
    assert not fake_run

    assert cli.main([*base, "--bind", "0.0.0.0", "--insecure-http"]) == 0
    assert "WARNING" in capsys.readouterr().err
    assert cli.main([*base, "--bind", "::1"]) == 0
    assert cli.main([*base]) == 0
    kw = fake_run[-1]
    assert kw["users"].path == tmp_path / "u.yaml"
    assert kw["audit"].path == tmp_path / "audit.jsonl"
    assert kw["ssl_context"] is None


def test_gui_users_file_selection(tmp_path, fake_run, monkeypatch, capsys):
    monkeypatch.setattr(auth, "DEFAULT_USERS_FILE", tmp_path / "home" / "users.yaml")
    base = ["gui", "--dir", str(tmp_path), "--no-browser"]
    # No users file: the single-token mode, no audit log unless asked for
    assert cli.main(base) == 0
    assert fake_run[-1]["users"] is None and fake_run[-1]["audit"].path is None
    assert cli.main([*base, "--audit-log", str(tmp_path / "a.jsonl")]) == 0
    assert fake_run[-1]["audit"].path == tmp_path / "a.jsonl"
    # An explicit --users must exist
    assert cli.main([*base, "--users", str(tmp_path / "missing.yaml")]) == 1
    assert "not found" in capsys.readouterr().err
    # The default file switches to named users when it exists
    UserStore(tmp_path / "home" / "users.yaml").add("alice")
    assert cli.main(base) == 0
    assert fake_run[-1]["users"].path == tmp_path / "home" / "users.yaml"
