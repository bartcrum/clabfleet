"""Named GUI users, roles, the audit log and remote-access safeguards."""

import asyncio
import json
import logging
import os
import re
import stat
import time
from urllib.parse import unquote

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


@pytest.fixture(autouse=True)
def cheap_scrypt(monkeypatch):
    """Passwords hashed at a low cost, so the tests stay fast (stored
    hashes carry their parameters, so checks use the same cost)."""
    monkeypatch.setattr(auth, "SCRYPT_N", 2 ** 10)


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


def test_user_store_refuses_files_others_can_change(tmp_path, caplog, monkeypatch):
    conf = tmp_path / "conf"
    store = UserStore(conf / "users.yaml")
    token = store.add("alice")
    assert stat.S_IMODE(conf.stat().st_mode) == 0o700
    assert [p.name for p in conf.iterdir()] == ["users.yaml"]  # no temporary left over

    store.path.chmod(0o620)
    assert store.authenticate(token) is None  # fails closed
    assert "writable by other users" in caplog.text
    with pytest.raises(ValueError, match="Users file .* writable by other users"):
        store.validate()
    with pytest.raises(ValueError, match="writable by other users"):
        store.add("mallory")
    store.path.chmod(0o600)

    conf.chmod(0o777)
    with pytest.raises(ValueError, match="Directory of users file .* writable"):
        store.validate()
    conf.chmod(0o700)
    assert store.validate()["alice"].role == "operator"

    uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: uid + 4242)  # as if someone else owned it
    with pytest.raises(ValueError, match="not owned by you"):
        store.validate()


def test_user_store_write_ignores_planted_temporary(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    (tmp_path / ".users.yaml.tmp").symlink_to(victim)  # the old fixed name
    UserStore(tmp_path / "users.yaml").add("alice")
    assert victim.read_text() == "untouched"
    assert stat.S_IMODE((tmp_path / "users.yaml").stat().st_mode) == 0o600


def test_audit_log_does_not_follow_symlinks(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    (tmp_path / "audit.jsonl").symlink_to(victim)
    AuditLog(tmp_path / "audit.jsonl").record("login", "alice")
    assert victim.read_text() == "untouched"


def test_user_cli(tmp_path, capsys):
    users = str(tmp_path / "users.yaml")
    assert cli.main(["user", "add", "alice", "--token", "--users", users,
                     "--url", "https://lab.example.com:8650"]) == 0
    out = capsys.readouterr().out
    token = out.split("#token=")[1].split()[0]
    assert UserStore(users).authenticate(token).role == "operator"
    assert "https://lab.example.com:8650/#token=" in out

    assert cli.main(["user", "add", "bob", "--role", "viewer", "--token", "--users", users]) == 0
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


async def _login(client, token, **body):
    client.session.cookie_jar.clear()
    return await client.post("/login", json={"token": token, **body})


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
            assert (await client.get("/")).status == 200  # the login form
            assert (await _login(client, "wrong")).status == 401
            assert (await client.get("/api/me")).status == 401

            # Viewer: reads yes, anything else no
            resp = await _login(client, view_token)
            assert resp.status == 200
            cookie = resp.headers["Set-Cookie"]
            assert "clabfleet_session_" in cookie and "HttpOnly" in cookie
            assert "SameSite=Strict" in cookie and view_token not in cookie
            assert await (await client.get("/api/me")).json() == {
                "user": "bob", "role": "viewer", "multi_user": True,
                "has_password": False, "must_change": False}
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
            assert resp.status == 200
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
            assert (await _login(client, token)).status == 200
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
        other = server.create_app(ws, "tok")
        async with TestClient(TestServer(app)) as client:
            resp = await _login(client, "tok")
            assert resp.status == 200
            # The cookie holds a session id, never the token, under a name of
            # its own so another GUI on the same host does not see it
            name = f"clabfleet_session_{app[server.AUTH].instance}"
            assert name != f"clabfleet_session_{other[server.AUTH].instance}"
            cookie = resp.cookies[name]
            assert cookie.value != "tok" and len(cookie.value) >= 43
            assert cookie["httponly"] and cookie["samesite"] == "Strict"
            assert cookie["path"] == "/" and not cookie["secure"]
            assert await (await client.get("/api/me")).json() == {
                "user": None, "role": "operator", "multi_user": False,
                "has_password": False, "must_change": False}
            resp = await client.get("/api/state")
            assert resp.headers["X-Frame-Options"] == "DENY"

            # Logging out ends the session on the server, not just the cookie
            assert (await client.post("/logout")).status == 200
            client.session.cookie_jar.update_cookies({name: cookie.value})
            assert (await client.get("/api/me")).status == 401

    monkeypatch.setattr(Workspace, "runtime", lambda self: [])
    asyncio.run(scenario())


def test_old_token_links_move_the_token_into_the_fragment(tmp_path):
    async def scenario():
        app = server.create_app(_workspace(tmp_path), "tok")
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/?token=a+b/c", allow_redirects=False)
            assert resp.status == 302
            location = resp.headers["Location"]
            assert location.startswith("/#token=") and unquote(location[8:]) == "a b/c"
            assert "Set-Cookie" not in resp.headers  # no login without the POST
            assert (await client.get("/api/me")).status == 401

    asyncio.run(scenario())


def test_login_post_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "LOGIN_FAILURES", 100)
    audit_path = tmp_path / "audit.jsonl"

    async def scenario():
        app = server.create_app(_workspace(tmp_path), "tok", audit=AuditLog(audit_path))
        async with TestClient(TestServer(app)) as client:
            # Login CSRF: other sites cannot post a login, not even as a form
            resp = await client.post("/login", json={"token": "tok"},
                                     headers={"Origin": "http://evil.example"})
            assert resp.status == 403
            assert (await client.post("/login", data={"token": "tok"})).status == 415
            assert (await client.post("/login", data="tok",
                                      headers={"Content-Type": "text/plain"})).status == 415
            for body in (["tok"], {"token": 1}, {}):
                assert (await client.post("/login", json=body)).status in (400, 401)
            # Not ASCII: a plain 401, not a crash in compare_digest
            for bad in ("tøk", "\ud800", "トークン"):
                resp = await client.post("/login", data=json.dumps({"token": bad}),
                                         headers={"Content-Type": "application/json"})
                assert resp.status in (401, 429), bad
            assert (await client.get("/login")).status == 405
            origin = str(client.make_url("/")).rstrip("/")
            resp = await client.post("/login", json={"token": "tok"}, headers={"Origin": origin})
            assert resp.status == 200

    asyncio.run(scenario())
    assert server.Auth("tok").check_token("tøk") is None


def test_failed_logins_are_throttled_per_address(tmp_path, monkeypatch):
    audit_path = tmp_path / "audit.jsonl"
    clock = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])

    async def scenario():
        app = server.create_app(_workspace(tmp_path), "tok", audit=AuditLog(audit_path))
        async with TestClient(TestServer(app)) as client:
            for _ in range(server.LOGIN_FAILURES):
                assert (await _login(client, "wrong")).status == 401
            for _ in range(10):
                resp = await _login(client, "wrong")
                assert resp.status == 429
                assert resp.headers["Retry-After"] == str(server.LOGIN_WINDOW)
            # The right token waits too, or guessing could go on
            assert (await _login(client, "tok")).status == 429
            # Another address is not affected
            auth = app[server.AUTH]
            assert auth.throttled("10.0.0.9") == 0
            clock[0] += server.LOGIN_WINDOW
            assert (await _login(client, "tok")).status == 200

    asyncio.run(scenario())
    events = [e["event"] for e in _events(audit_path)]
    # One line for the whole throttled window, not one per attempt
    assert events == ["login_failed"] * server.LOGIN_FAILURES + ["login_throttled", "login"]
    throttled = _events(audit_path)[server.LOGIN_FAILURES]
    assert throttled["details"] == {"failures": server.LOGIN_FAILURES,
                                    "seconds": server.LOGIN_WINDOW}


def test_session_limits(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(server.time, "time", lambda: clock[0])
    users = UserStore(tmp_path / "users.yaml")
    users.add("alice")
    users.add("bob", "viewer")
    auth = server.Auth(users=users)
    app = server.create_app(_workspace(tmp_path), users=users)

    def request(sid):
        return make_mocked_request("GET", "/api/me", app=app,
                                   headers={"Cookie": f"{auth.cookie_name(False)}={sid}"})

    # Each further login beyond the cap ends the user's oldest session
    first = auth.start_session(users.get("alice"))
    clock[0] += 1
    sids = [auth.start_session(users.get("alice")) for _ in range(server.SESSIONS_PER_USER - 1)]
    bobs = auth.start_session(users.get("bob"))
    assert auth.identify(request(first)).name == "alice"
    clock[0] += 1
    sids.append(auth.start_session(users.get("alice")))
    assert auth.identify(request(first)) is None
    assert all(auth.identify(request(s)).name == "alice" for s in sids)
    assert auth.identify(request(bobs)).name == "bob"

    # Busy sessions still end SESSION_MAX_AGE after login
    for _ in range(server.SESSION_MAX_AGE // 3600 - 1):
        clock[0] += 3600
        assert auth.identify(request(bobs)).name == "bob"
    clock[0] += 3600
    assert auth.identify(request(bobs)) is None
    # Idle ones after SESSION_IDLE
    sid = auth.start_session(users.get("bob"))
    clock[0] += server.SESSION_IDLE + 1
    assert auth.identify(request(sid)) is None


def test_login_does_not_quietly_switch_users(tmp_path):
    users = UserStore(tmp_path / "users.yaml")
    alice, bob = users.add("alice"), users.add("bob", "viewer")

    async def scenario():
        app = server.create_app(_workspace(tmp_path), users=users)
        async with TestClient(TestServer(app)) as client:
            assert (await _login(client, alice)).status == 200
            # A link with someone else's token: refused unless asked for
            resp = await client.post("/login", json={"token": bob})
            assert resp.status == 409 and (await resp.json())["user"] == "alice"
            assert (await (await client.get("/api/me")).json())["user"] == "alice"
            # Logging in again as the same user is fine
            assert (await client.post("/login", json={"token": alice})).status == 200
            resp = await client.post("/login", json={"token": bob, "switch": True})
            assert resp.status == 200
            assert (await (await client.get("/api/me")).json())["user"] == "bob"

    asyncio.run(scenario())


def test_https_cookie_and_headers(tmp_path):
    app = server.create_app(_workspace(tmp_path), "tok", public_url="https://lab.example.com")

    async def scenario():
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/login", json={"token": "tok"})
            name = f"__Host-clabfleet_session_{app[server.AUTH].instance}"
            cookie = resp.cookies[name]
            assert cookie["secure"] and cookie["path"] == "/" and not cookie["domain"]
            for path in ("/", "/api/me", "/static/app.js"):
                resp = await client.get(path, headers={"Cookie": f"{name}={cookie.value}"})
                assert resp.status == 200, path
                assert resp.headers["Strict-Transport-Security"].startswith("max-age=")
                csp = resp.headers["Content-Security-Policy"]
                assert "script-src 'self';" in csp and "frame-ancestors 'none'" in csp
            assert resp.headers.get("Cache-Control") != "no-store"  # static files may be cached
            resp = await client.get("/api/me", headers={"Cookie": f"{name}={cookie.value}"})
            assert resp.headers["Cache-Control"] == "no-store"
            resp = await client.post("/logout", headers={"Cookie": f"{name}={cookie.value}"})
            deleted = resp.cookies[name]
            assert deleted.value == "" and deleted["secure"]  # else the browser ignores it

    asyncio.run(scenario())


def test_plain_http_headers(tmp_path):
    async def scenario():
        app = server.create_app(_workspace(tmp_path), "tok")
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/")
            assert "Strict-Transport-Security" not in resp.headers
            assert resp.headers["Cache-Control"] == "no-store"
            assert resp.headers["Content-Security-Policy"] == server.CSP

    asyncio.run(scenario())


def test_page_has_nothing_the_csp_blocks():
    html = (server.STATIC_DIR / "index.html").read_text()
    assert "<script>" not in html and "<style" not in html
    assert not re.search(r"\son[a-z]+=", html) and "style=" not in html
    assert "javascript:" not in html


def test_access_log_has_no_tokens(tmp_path, caplog):
    async def scenario():
        app = server.create_app(_workspace(tmp_path), "secret-tok")
        test_server = TestServer(app)
        await test_server.start_server(access_log_class=server.AccessLogger)
        async with TestClient(test_server) as client:
            await client.get("/?token=secret-tok", allow_redirects=False)
            await client.post("/login", json={"token": "secret-tok"})
            await client.get("/api/me?x=secret-tok")

    with caplog.at_level(logging.INFO, logger="aiohttp.access"):
        asyncio.run(scenario())
    lines = [r.getMessage() for r in caplog.records if r.name == "aiohttp.access"]
    assert len(lines) == 3
    assert '"GET / HTTP/1.1" 302' in lines[0] and '"POST /login HTTP/1.1" 200' in lines[1]
    assert not any("secret-tok" in line for line in lines)


def test_startup_message_and_links(tmp_path):
    url, text = server.startup_message("http://localhost:8650", "tok", None, None)
    assert url == "http://localhost:8650/#token=tok" and url in text and "?token" not in text
    assert "clabfleet user add" in text  # how to log in with a password instead
    users = UserStore(tmp_path / "users.yaml")
    url, text = server.startup_message("https://lab:8650", None, users, AuditLog(None))
    assert url == "https://lab:8650/" and "name and password" in text and "#token" not in text
    assert auth.login_link("https://lab:8650/", "t") == "https://lab:8650/#token=t"


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
    home = tmp_path / "home" / "users.yaml"
    # --single-token: no users, no audit log unless asked for
    assert cli.main([*base, "--single-token"]) == 0
    assert fake_run[-1]["users"] is None and fake_run[-1]["audit"].path is None
    assert cli.main([*base, "--single-token", "--audit-log", str(tmp_path / "a.jsonl")]) == 0
    assert fake_run[-1]["audit"].path == tmp_path / "a.jsonl"
    assert not home.exists()
    assert cli.main([*base, "--single-token", "--users", str(home)]) == 1
    # An explicit --users must exist (it is not created)
    assert cli.main([*base, "--users", str(tmp_path / "missing.yaml")]) == 1
    assert "not found" in capsys.readouterr().err
    assert not (tmp_path / "missing.yaml").exists()
    # No default file yet: created with admin / admin, who must change it
    assert cli.main(base) == 0
    assert fake_run[-1]["users"].path == home
    assert fake_run[-1]["audit"].path == home.parent / "audit.jsonl"
    assert "admin / admin" in capsys.readouterr().err
    admin = UserStore(home).check_password("admin", "admin")
    assert admin.role == "operator" and admin.must_change
    # Once it exists it is used as it is
    UserStore(home).add("alice")
    UserStore(home).set_password("admin", "a good new password")
    assert cli.main(base) == 0
    assert "admin / admin" not in capsys.readouterr().err
    assert set(fake_run[-1]["users"].users()) == {"admin", "alice"}


def test_run_installs_the_access_logger(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(server.web, "run_app", lambda app, **kw: calls.append(kw))
    server.run(_workspace(tmp_path), open_browser=False)
    assert calls[0]["access_log_class"] is server.AccessLogger


# ----------------------------------------------------------------------
# Logins kept across restarts
# ----------------------------------------------------------------------

def _restart(tmp_path, token=None, users=None):
    """A GUI app as ``run`` makes it: sessions kept per port in a file."""
    sessions = auth.SessionFile(tmp_path / "state" / "gui-sessions-8650.json")
    return server.create_app(_workspace(tmp_path), token, users=users, instance="8650",
                             session_file=sessions)


def test_logins_survive_a_restart(tmp_path):
    users = UserStore(tmp_path / "users.yaml")
    alice, bob = users.add("alice"), users.add("bob", "viewer")
    path = tmp_path / "state" / "gui-sessions-8650.json"

    async def scenario():
        cookies = {}
        async with TestClient(TestServer(_restart(tmp_path, users=users))) as client:
            for name, tok in (("alice", alice), ("bob", bob)):
                client.session.cookie_jar.clear()
                resp = await _login(client, tok)
                assert resp.status == 200
                assert f"Max-Age={server.SESSION_MAX_AGE}" in resp.headers["Set-Cookie"]
                cookies[name] = resp.cookies["clabfleet_session_8650"].value
        # Only hashes of the session ids, in a private file
        text = path.read_text()
        assert all(sid not in text for sid in cookies.values())
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

        users.rotate("bob")  # ends bob's sessions, even those on disk
        async with TestClient(TestServer(_restart(tmp_path, users=users))) as client:
            name = "clabfleet_session_8650"
            client.session.cookie_jar.update_cookies({name: cookies["alice"]})
            assert (await (await client.get("/api/me")).json())["user"] == "alice"
            client.session.cookie_jar.update_cookies({name: cookies["bob"]})
            assert (await client.get("/api/me")).status == 401
            # Logging out ends the session for the next run too
            client.session.cookie_jar.update_cookies({name: cookies["alice"]})
            assert (await client.post("/logout")).status == 200
        async with TestClient(TestServer(_restart(tmp_path, users=users))) as client:
            client.session.cookie_jar.update_cookies({name: cookies["alice"]})
            assert (await client.get("/api/me")).status == 401

    asyncio.run(scenario())


def test_single_token_logins_survive_a_new_token(tmp_path):
    async def scenario():
        async with TestClient(TestServer(_restart(tmp_path, "first"))) as client:
            assert (await _login(client, "first")).status == 200
            jar = client.session.cookie_jar.filter_cookies(client.make_url("/"))
            sid = jar["clabfleet_session_8650"].value
        # The next start prints another token; the browser stays logged in
        async with TestClient(TestServer(_restart(tmp_path, "second"))) as client:
            client.session.cookie_jar.update_cookies({"clabfleet_session_8650": sid})
            me = await (await client.get("/api/me")).json()
            assert me == {"user": None, "role": "operator", "multi_user": False,
                          "has_password": False, "must_change": False}
            assert (await _login(client, "first")).status == 401  # the old token is gone

    asyncio.run(scenario())


def test_sessions_do_not_cross_modes_and_expire_on_disk(tmp_path, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(server.time, "time", lambda: clock[0])
    users = UserStore(tmp_path / "users.yaml")
    users.add("alice")
    path = tmp_path / "s.json"
    named = server.Auth(users=users, instance="1", session_file=auth.SessionFile(path))
    sid = named.start_session(users.get("alice"))
    single = server.Auth("tok", instance="1", session_file=auth.SessionFile(path))
    req = make_mocked_request("GET", "/api/me", app=server.create_app(_workspace(tmp_path), "tok"),
                              headers={"Cookie": f"{single.cookie_name(False)}={sid}"})
    assert single.identify(req) is None  # a named user's session is no operator's
    # Expired sessions are dropped when the next run loads the file
    clock[0] += server.SESSION_IDLE + 1
    later = server.Auth(users=users, instance="1", session_file=auth.SessionFile(path))
    later.start_session(users.get("alice"))
    assert len(json.loads(path.read_text())["sessions"]) == 1


def test_sessions_file_others_can_change_is_not_used(tmp_path, caplog):
    path = tmp_path / "gui-sessions-8650.json"
    path.write_text(json.dumps({"version": 1, "sessions": {}}))
    path.chmod(0o666)
    store = auth.SessionFile(path)
    with caplog.at_level(logging.ERROR):
        assert store.load() == {}
    assert "writable by other users" in caplog.text
    store.save({"a" * 64: {}})  # left alone
    assert json.loads(path.read_text())["sessions"] == {}


# ----------------------------------------------------------------------
# Names and passwords
# ----------------------------------------------------------------------

PASSWORD = "correct horse battery"


def test_user_store_passwords(tmp_path):
    store = UserStore(tmp_path / "users.yaml")
    assert store.add("alice", password=PASSWORD) is None  # no token
    text = (tmp_path / "users.yaml").read_text()
    assert PASSWORD not in text and "password: scrypt$1024$8$1$" in text
    assert "token_sha256" not in text
    alice = store.check_password("alice", PASSWORD)
    assert alice.name == "alice" and alice.role == "operator"
    for name, pw in (("alice", "wrong password!"), ("nobody", PASSWORD), ("alice", ""),
                     ("alice", None), (None, PASSWORD), ("alice", "\ud800")):
        assert store.check_password(name, pw) is None
    assert store.authenticate("") is None  # no token: an empty one matches nothing

    with pytest.raises(ValueError, match="at least 12"):
        store.add("bob", password="short")
    with pytest.raises(ValueError, match="at least 12"):
        store.set_password("alice", "short")
    with pytest.raises(KeyError):
        store.set_password("nobody", PASSWORD)

    # A new password, or a token next to it, changes the user's credential
    before = store.get("alice").credential
    store.set_password("alice", "another good password")
    assert store.check_password("alice", PASSWORD) is None
    changed = store.get("alice").credential
    assert changed != before
    token = store.rotate("alice")
    assert store.authenticate(token).name == "alice" and store.get("alice").credential != changed
    assert store.check_password("alice", "another good password").name == "alice"


@pytest.mark.parametrize("entry, error", [
    ({"role": "operator"}, "neither a password nor a token"),
    ({"role": "operator", "password": "plain text"}, "not an scrypt hash"),
])
def test_user_store_refuses_bad_password_entries(tmp_path, caplog, entry, error):
    path = tmp_path / "users.yaml"
    path.write_text(json.dumps({"users": {"alice": entry}}))
    path.chmod(0o600)
    with pytest.raises(ValueError, match=error):
        UserStore(path).validate()


def test_password_login(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "LOGIN_FAILURES", 100)
    users = UserStore(tmp_path / "users.yaml")
    users.add("alice", password=PASSWORD)
    users.add("bob", "viewer")  # a token user
    audit_path = tmp_path / "audit.jsonl"

    async def scenario():
        app = server.create_app(_workspace(tmp_path), users=users, audit=AuditLog(audit_path))
        async with TestClient(TestServer(app)) as client:
            # The login form learns what to ask for from the 401
            resp = await client.get("/api/me")
            assert resp.status == 401 and resp.headers["X-Clabfleet-Login"] == "password"
            for body in ({"username": "alice", "password": "wrong password!"},
                         {"username": "nobody", "password": PASSWORD},
                         {"username": "alice"}, {"username": ["alice"], "password": PASSWORD},
                         {"password": PASSWORD}):
                resp = await client.post("/login", json=body)
                assert resp.status == 401, body
                assert await resp.text() == "invalid user name or password"
            resp = await client.post("/login", json={"username": "alice", "password": PASSWORD})
            assert resp.status == 200 and await resp.json() == {"user": "alice", "role": "operator"}
            assert PASSWORD not in resp.headers["Set-Cookie"]
            assert (await (await client.get("/api/me")).json())["user"] == "alice"
            # A new password ends the session
            users.set_password("alice", "another good password")
            assert (await client.get("/api/me")).status == 401

    asyncio.run(scenario())
    events = _events(audit_path)
    failed = [e for e in events if e["event"] == "login_failed"]
    assert failed[0]["details"] == {"reason": "invalid user name or password", "username": "alice"}
    assert failed[1]["details"]["username"] == "nobody"
    assert PASSWORD not in audit_path.read_text()
    assert [e["details"] for e in events if e["event"] == "login"] == [{"method": "password"}]


def test_single_token_mode_asks_for_the_token(tmp_path):
    async def scenario():
        async with TestClient(TestServer(server.create_app(_workspace(tmp_path), "tok"))) as client:
            resp = await client.get("/api/me")
            assert resp.status == 401 and resp.headers["X-Clabfleet-Login"] == "token"
            # No users: no password can be right
            resp = await client.post("/login", json={"username": "", "password": "tok"})
            assert resp.status == 401

    asyncio.run(scenario())


def test_failed_passwords_are_throttled_per_user_name(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    users = UserStore(tmp_path / "users.yaml")
    users.add("alice", password=PASSWORD)
    audit_path = tmp_path / "audit.jsonl"

    async def scenario():
        app = server.create_app(_workspace(tmp_path), users=users, audit=AuditLog(audit_path))
        auth_ = app[server.AUTH]
        async with TestClient(TestServer(app)) as client:
            for _ in range(server.LOGIN_FAILURES):
                resp = await client.post("/login", json={"username": "alice", "password": "nope"})
                assert resp.status == 401
            # The name is locked for the window, whatever the address
            auth_._failures.pop("127.0.0.1")  # as if from another address
            resp = await client.post("/login", json={"username": "alice", "password": PASSWORD})
            assert resp.status == 429
            # Other names are not
            assert auth_.throttled("user:bob") == 0
            clock[0] += server.LOGIN_WINDOW
            resp = await client.post("/login", json={"username": "alice", "password": PASSWORD})
            assert resp.status == 200

    asyncio.run(scenario())
    throttled = [e for e in _events(audit_path) if e["event"] == "login_throttled"]
    assert throttled[0]["details"]["username"] == "alice"


def test_user_cli_passwords(tmp_path, capsys, monkeypatch):
    users = str(tmp_path / "users.yaml")

    def run(*argv, stdin=None, prompts=()):
        answers = iter(prompts)
        monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
        monkeypatch.setattr(cli.sys, "stdin", __import__("io").StringIO(stdin or ""))
        return cli.main(["user", *argv, "--users", users])

    assert run("add", "alice", prompts=[PASSWORD, PASSWORD]) == 0
    out = capsys.readouterr().out
    assert "#token" not in out and "name and this password" in out
    assert UserStore(users).check_password("alice", PASSWORD).name == "alice"
    assert run("add", "alice", prompts=[PASSWORD, PASSWORD]) == 1  # exists
    assert run("add", "bob", prompts=[PASSWORD, "something else!"]) == 1  # mismatch
    assert "do not match" in capsys.readouterr().err
    assert run("add", "bob", "--password-stdin", stdin="short\n") == 1
    assert "at least 12" in capsys.readouterr().err
    assert run("add", "bob", "--role", "viewer", "--password-stdin", stdin=PASSWORD + "\n") == 0
    assert UserStore(users).check_password("bob", PASSWORD).role == "viewer"

    assert run("passwd", "nobody", prompts=[]) == 1  # before asking anything
    assert run("passwd", "alice", "--password-stdin", stdin="another good password\n") == 0
    assert UserStore(users).check_password("alice", "another good password")
    capsys.readouterr()
    assert run("list") == 0
    listing = capsys.readouterr().out
    assert re.search(r"alice\s+operator\s+password\s", listing)
    assert run("rotate", "bob") == 0
    capsys.readouterr()
    assert run("list") == 0
    assert re.search(r"bob\s+viewer\s+password\+token\s", capsys.readouterr().out)


# ----------------------------------------------------------------------
# First login: admin / admin, then a new password
# ----------------------------------------------------------------------

def test_first_admin_must_set_a_new_password(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "LOGIN_FAILURES", 100)
    users = UserStore(tmp_path / "users.yaml")
    assert users.bootstrap() and not users.bootstrap()  # only once
    audit_path = tmp_path / "audit.jsonl"
    new = "a much better password"

    async def scenario():
        app = server.create_app(_workspace(tmp_path), users=users, audit=AuditLog(audit_path))
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/login", json={"username": "admin", "password": "admin"})
            assert resp.status == 200
            me = await (await client.get("/api/me")).json()
            assert me["must_change"] is True and me["has_password"] is True
            # Nothing else until the password changes, terminals included
            for path in ("/api/state", "/api/jobs", "/api/topologies/t.clab.yml"):
                assert (await client.get(path)).status == 403, path
            assert (await client.post("/api/jobs", json={})).status == 403
            with pytest.raises(WSServerHandshakeError):
                await client.ws_connect("/ws/terminal?lab=t&node=a&mode=shell")

            change = lambda body: client.post("/api/password", json=body)  # noqa: E731
            assert (await change({"current": "wrong", "new": new})).status == 403
            assert (await change({"current": "admin", "new": "admin"})).status == 400
            assert (await change({"current": "admin", "new": "short"})).status == 400
            assert (await change({"current": "admin"})).status == 400
            resp = await change({"current": "admin", "new": new})
            assert resp.status == 200
            # This browser goes on with a new session; the rest is open now
            assert (await (await client.get("/api/me")).json())["must_change"] is False
            assert (await client.get("/api/state")).status == 200

        assert users.check_password("admin", "admin") is None
        assert users.check_password("admin", new).must_change is False

    asyncio.run(scenario())
    events = [e["event"] for e in _events(audit_path)]
    assert "password_change_failed" in events and "password_changed" in events
    assert "admin" not in [e["details"].get("new") for e in _events(audit_path)]


def test_password_change_by_other_users(tmp_path):
    users = UserStore(tmp_path / "users.yaml")
    users.add("alice", "viewer", password=PASSWORD)
    bob = users.add("bob")  # token only

    async def scenario():
        app = server.create_app(_workspace(tmp_path), users=users)
        async with TestClient(TestServer(app)) as client:
            # Viewers may change their own password
            await client.post("/login", json={"username": "alice", "password": PASSWORD})
            other = TestClient(TestServer(app))
            async with other:
                await other.post("/login", json={"username": "alice", "password": PASSWORD})
                resp = await client.post("/api/password",
                                         json={"current": PASSWORD, "new": "alice's new password"})
                assert resp.status == 200
                assert (await other.get("/api/me")).status == 401  # her other sessions end
            # A token user has no password to change
            assert (await _login(client, bob)).status == 200
            resp = await client.post("/api/password", json={"current": "", "new": PASSWORD})
            assert resp.status == 400

    asyncio.run(scenario())


def test_single_token_mode_has_no_password_to_change(tmp_path):
    async def scenario():
        async with TestClient(TestServer(server.create_app(_workspace(tmp_path), "tok"))) as client:
            assert (await _login(client, "tok")).status == 200
            resp = await client.post("/api/password", json={"current": "tok", "new": PASSWORD})
            assert resp.status == 400

    asyncio.run(scenario())


def test_startup_message_tells_the_first_login(tmp_path):
    users = UserStore(tmp_path / "users.yaml")
    users.bootstrap()
    _, text = server.startup_message("http://localhost:8650", None, users, None)
    assert "admin / admin" in text
    users.set_password("admin", "a good new password")
    _, text = server.startup_message("http://localhost:8650", None, users, None)
    assert "admin / admin" not in text
