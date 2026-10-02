"""Long-lived GUI sessions: backpressure, limits, revocation, clean-up, shutdown."""

import asyncio
import json
import logging
import os
import socket
import subprocess
import threading
import time

import pytest

pytest.importorskip("aiohttp")

from aiohttp import WSServerHandshakeError  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from clabfleet import cli  # noqa: E402
from clabfleet.cluster import ClusterConfig, HostInfo  # noqa: E402
from clabfleet.gui import server, state, terminals  # noqa: E402
from clabfleet.gui.auth import User, UserStore  # noqa: E402
from clabfleet.gui.sessions import (  # noqa: E402
    CAPTURE, TERMINAL, SessionLimitError, SessionRegistry,
)
from clabfleet.gui.state import HostState, Workspace  # noqa: E402
from clabfleet.gui.terminals import (  # noqa: E402
    MAX_INPUT, QUEUE_CHUNKS, CaptureSession, LocalTerminal, SSHTerminal,
)
from clabfleet.nodes import TERMINAL_STOP, terminal_command, terminal_stop_command  # noqa: E402

TOPO = """\
name: t
topology:
  nodes:
    a: {kind: linux, image: alpine}
"""


def _workspace(tmp_path):
    (tmp_path / "t.clab.yml").write_text(TOPO)
    return Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])


def _fake_docker(tmp_path, monkeypatch, body='echo "args: $*"; exec sleep 30'):
    """A `docker` that prints its args and keeps running; the terminal stop
    command (docker exec ... clabfleet-stop TAG) is logged to stops.log."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        f'case "$*" in *clabfleet-stop*) echo "$*" >> "{tmp_path}/stops.log"; exit 0 ;; esac\n'
        f"{body}\n")
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setattr(Workspace, "find_node", lambda self, lab, node: {
        "kind": "linux", "container": "clab-t-a", "ipv4": "", "host": "localhost"})
    monkeypatch.setattr(Workspace, "runtime", lambda self, max_age=0: [])


async def _login(client, token):
    client.session.cookie_jar.clear()
    return await client.post("/login", json={"token": token})


async def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def _read_until(sock, text: bytes, timeout=5.0):
    out = b""
    while text not in out:
        msg = await asyncio.wait_for(sock.receive(), timeout)
        if msg.type.name != "BINARY":
            raise AssertionError(f"socket ended before {text!r}: {msg} {out!r}")
        out += msg.data
    return out


async def _close_code(sock, timeout=5.0):
    """Read until the server closes; the close code."""
    while True:
        msg = await asyncio.wait_for(sock.receive(), timeout)
        if msg.type.name in ("CLOSE", "CLOSING", "CLOSED"):
            return sock.close_code


# ----------------------------------------------------------------------
# Backpressure
# ----------------------------------------------------------------------

@pytest.mark.parametrize("tty", [True, False])
def test_local_terminal_pauses_reading_when_the_browser_lags(tty):
    async def scenario():
        session = LocalTerminal(["yes", "0123456789" * 50], 80, 24, tty=tty)
        await session.start(asyncio.get_running_loop())
        try:
            # Nobody reads: the queue fills up, then the pty/pipe is left alone
            await _until(lambda: not session._reading)
            await asyncio.sleep(0.2)
            assert session._queue.qsize() == QUEUE_CHUNKS
            # Taking output resumes reading, and the queue never grows past the bound
            out = session.output()
            for _ in range(QUEUE_CHUNKS * 4):
                chunk = await asyncio.wait_for(out.__anext__(), 5)
                assert chunk.startswith(b"0123") or b"0123" in chunk
                assert session._queue.qsize() <= QUEUE_CHUNKS
            if not tty:  # no pty: newlines are made terminal-safe here
                assert b"\r\n" in chunk
        finally:
            session.close()
            await session.wait_closed()
        assert session._proc.returncode is not None  # reaped, no zombie

    asyncio.run(scenario())


class FakeChannel:
    """A paramiko channel whose command prints without end."""

    def __init__(self):
        self.closed = False
        self.sent = b""
        self.sizes = []
        self.reads = 0

    def get_pty(self, **kw):
        pass

    def exec_command(self, cmd):
        self.cmd = cmd

    def recv(self, size):
        if self.closed:
            return b""
        self.reads += 1
        return b"x" * 1000

    def recv_exit_status(self):
        return 0

    def exit_status_ready(self):
        return False

    def sendall(self, data):
        time.sleep(0.05)  # a slow link: never on the event loop
        self.sent += data

    def resize_pty(self, width, height):
        self.sizes.append((width, height))

    def close(self):
        self.closed = True


class FakeSSH:
    def __init__(self, chan):
        self.chan = chan

    def get_transport(self):
        return self

    def open_session(self, timeout=None):
        return self.chan


def test_ssh_terminal_reader_blocks_on_a_full_queue_and_writes_off_loop():
    chan = FakeChannel()
    cleaned = []

    async def scenario():
        session = SSHTerminal(FakeSSH(chan), ["docker", "exec", "-it", "c", "sh"], 80, 24,
                              cleanup=lambda: cleaned.append(True))
        await session.start(asyncio.get_running_loop())
        await _until(lambda: session._queue.qsize() == QUEUE_CHUNKS)
        reads = chan.reads
        await asyncio.sleep(0.3)
        assert chan.reads <= reads + 1  # the reader thread waits for room
        assert session._queue.qsize() == QUEUE_CHUNKS

        out = session.output()
        for _ in range(QUEUE_CHUNKS * 2):
            await asyncio.wait_for(out.__anext__(), 5)
        assert chan.reads > reads

        # Input and resizes go through a thread: a slow sendall does not block us
        started = time.monotonic()
        for _ in range(5):
            assert session.write(b"ls\r")
        session.resize(100, 30)
        assert time.monotonic() - started < 0.1
        assert not session.write(b"x" * (MAX_INPUT + 1))  # too much pending
        await _until(lambda: chan.sent == b"ls\r" * 5 and chan.sizes == [(100, 30)])

        session.close()
        await session.wait_closed()
        assert chan.closed and cleaned == [True]

    asyncio.run(scenario())


def test_capture_session_messages_never_block_and_are_bounded():
    async def scenario():
        session = CaptureSession(asyncio.get_running_loop())
        for i in range(QUEUE_CHUNKS * 3):
            session.message(f"line {i}")
        await asyncio.sleep(0.05)
        assert session._queue.qsize() <= QUEUE_CHUNKS + terminals.MESSAGE_SLACK

    asyncio.run(scenario())


def test_local_terminal_buffers_input_the_command_has_not_taken():
    async def scenario():
        loop = asyncio.get_running_loop()
        read_fd, write_fd = os.pipe()
        os.set_blocking(write_fd, False)
        session = LocalTerminal(["true"], 80, 24)
        session._loop, session._fd = loop, write_fd
        try:
            data = bytes(range(256)) * 2048  # 512 KiB, more than a pipe holds
            assert session.write(data)
            assert session._pending and session._writing  # the rest waits for the pipe
            assert not session.write(b"x" * MAX_INPUT)    # over the cap: refused
            received = bytearray()
            while len(received) < len(data):
                received += await loop.run_in_executor(None, os.read, read_fd, 65536)
            assert bytes(received) == data  # nothing lost, in order
            await _until(lambda: not session._writing)
        finally:
            os.close(read_fd)
            os.close(write_fd)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# Ending what a terminal started inside the container
# ----------------------------------------------------------------------

def test_terminal_commands_are_tagged_and_stoppable():
    argv = terminal_command("shell", "linux", "clab-t-a", "", tag="abc")
    assert argv[:5] == ["docker", "exec", "-it", "-e", "CLABFLEET_TERMINAL=abc"]
    assert argv[5] == "clab-t-a"
    assert terminal_command("cli", "ceos", "c", "", tag="abc")[3:6] == [
        "-e", "CLABFLEET_TERMINAL=abc", "c"]
    assert terminal_command("shell", "linux", "c", "")[:4] == ["docker", "exec", "-it", "c"]
    assert terminal_stop_command("c", "abc") == [
        "docker", "exec", "c", "sh", "-c", TERMINAL_STOP, "clabfleet-stop", "abc"]


def test_terminal_stop_script_kills_only_tagged_processes():
    # The script runs inside a container normally; here it scans this
    # machine's /proc, which works the same
    tagged = subprocess.Popen(
        ["sh", "-c", "sleep 300 & env -i sleep 301 & wait"],
        env={**os.environ, "CLABFLEET_TERMINAL": "t0ken"}, start_new_session=True)
    other_tag = subprocess.Popen(["sleep", "302"], start_new_session=True,
                                 env={**os.environ, "CLABFLEET_TERMINAL": "other"})
    untagged = subprocess.Popen(["sleep", "303"], start_new_session=True)
    try:
        time.sleep(0.2)
        res = subprocess.run(["sh", "-c", TERMINAL_STOP, "clabfleet-stop", "t0ken"],
                             capture_output=True, text=True, timeout=30)
        assert res.returncode == 0, res.stderr
        assert res.stderr == ""
        tagged.wait(5)
        # The env -i child is not tagged, but it is in the tagged shell's session
        left = subprocess.run(["pgrep", "-s", str(tagged.pid)], capture_output=True, text=True)
        assert left.stdout.strip() == ""
        assert other_tag.poll() is None and untagged.poll() is None
    finally:
        for p in (tagged, other_tag, untagged):
            p.kill()
            p.wait()


def test_closing_a_shell_tab_stops_it_in_the_container(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    _fake_docker(tmp_path, monkeypatch)

    async def scenario():
        app = server.create_app(ws, "tok")
        async with TestClient(TestServer(app)) as client:
            await _login(client, "tok")
            sock = await client.ws_connect("/ws/terminal?lab=t&node=a&mode=shell")
            out = await _read_until(sock, b"clab-t-a")
            tag = out.split(b"CLABFLEET_TERMINAL=")[1].split()[0].decode()
            await sock.close()
            await _until(lambda: (tmp_path / "stops.log").exists())
            assert (tmp_path / "stops.log").read_text().split()[-2:] == ["clabfleet-stop", tag]

            # Logs tabs have nothing to stop in the container
            (tmp_path / "stops.log").unlink()
            sock = await client.ws_connect("/ws/terminal?lab=t&node=a&mode=logs")
            await _read_until(sock, b"logs")
            await sock.close()
            await _until(lambda: len(app[server.SESSIONS]) == 0)
            await asyncio.sleep(0.2)
            assert not (tmp_path / "stops.log").exists()

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# Limits
# ----------------------------------------------------------------------

def test_session_registry_limits():
    reg = SessionRegistry(max_user_sessions=2, max_sessions=3, max_user_captures=1,
                          max_captures=1)
    alice, bob = User("alice", "operator"), User("bob", "viewer")
    a1 = reg.open(TERMINAL, alice, None)
    reg.open(TERMINAL, alice, None)
    with pytest.raises(SessionLimitError, match="You already have 2"):
        reg.open(TERMINAL, alice, None)
    reg.open(TERMINAL, bob, None)
    with pytest.raises(SessionLimitError, match="The GUI already has 3"):
        reg.open(TERMINAL, bob, None)
    reg.open(CAPTURE, bob, None)  # captures count separately
    with pytest.raises(SessionLimitError, match="packet captures"):
        reg.open(CAPTURE, alice, None)
    reg.close(a1)
    reg.close(a1)  # twice is fine
    reg.open(TERMINAL, bob, None)
    assert reg.count(TERMINAL) == 3 and reg.count(TERMINAL, "bob") == 2


def test_terminal_limits_over_websocket(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    _fake_docker(tmp_path, monkeypatch)
    users = UserStore(tmp_path / "users.yaml")
    alice, bob = users.add("alice", "viewer"), users.add("bob", "viewer")

    async def scenario():
        app = server.create_app(ws, users=users, max_user_sessions=2, max_sessions=3)
        async with TestClient(TestServer(app)) as client:
            await _login(client, alice)
            url = "/ws/terminal?lab=t&node=a&mode=logs"
            socks = [await client.ws_connect(url) for _ in range(2)]
            for s in socks:
                await _read_until(s, b"args")
            sock = await client.ws_connect(url)
            assert b"You already have 2 open terminal sessions" in await _read_until(sock, b"limit")
            assert json.loads((await sock.receive()).data) == {"t": "exit", "code": -1}

            await _login(client, bob)
            socks.append(await client.ws_connect(url))
            await _read_until(socks[-1], b"args")
            sock = await client.ws_connect(url)
            assert b"The GUI already has 3" in await _read_until(sock, b"limit")
            # Closing one makes room again
            await socks.pop().close()
            await _until(lambda: len(app[server.SESSIONS]) == 2)
            socks.append(await client.ws_connect(url))
            await _read_until(socks[-1], b"args")
            for s in socks:
                await s.close()

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# Revoked logins
# ----------------------------------------------------------------------

def test_removed_rotated_or_logged_out_users_lose_their_terminals(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    _fake_docker(tmp_path, monkeypatch)
    users = UserStore(tmp_path / "users.yaml")
    tokens = {name: users.add(name, "operator") for name in ("alice", "bob", "carol")}

    def touch_users():
        os.utime(users.path, ns=(time.time_ns() + 10**9,) * 2)  # same-second writes

    async def scenario():
        app = server.create_app(ws, users=users)
        async with TestClient(TestServer(app)) as client:
            socks = {}
            for name, token in tokens.items():
                await _login(client, token)
                socks[name] = await client.ws_connect("/ws/terminal?lab=t&node=a&mode=shell")
                await _read_until(socks[name], b"args")

            # Removed: the periodic check closes the socket (policy violation)
            users.remove("alice")
            touch_users()
            await server._revalidate_sessions(app)
            assert await _close_code(socks["alice"]) == 1008
            assert not socks["bob"].closed and not socks["carol"].closed

            # Rotated: the next message from the browser is refused
            users.rotate("bob")
            touch_users()
            await socks["bob"].send_str(json.dumps({"t": "i", "d": "ls\r"}))
            assert await _close_code(socks["bob"]) == 1008

            # Logged out (the cookie jar holds carol's session)
            assert (await client.post("/logout")).status == 200
            await server._revalidate_sessions(app)
            assert await _close_code(socks["carol"]) == 1008
            await _until(lambda: len(app[server.SESSIONS]) == 0)

    asyncio.run(scenario())


def test_downgraded_operator_loses_shells_but_single_token_mode_is_unaffected(tmp_path,
                                                                              monkeypatch):
    ws = _workspace(tmp_path)
    _fake_docker(tmp_path, monkeypatch)
    users = UserStore(tmp_path / "users.yaml")
    token = users.add("alice", "operator")

    async def scenario():
        app = server.create_app(ws, users=users)
        async with TestClient(TestServer(app)) as client:
            await _login(client, token)
            sock = await client.ws_connect("/ws/terminal?lab=t&node=a&mode=shell")
            await _read_until(sock, b"args")
            # Same token, now a viewer
            text = users.path.read_text().replace("role: operator", "role: viewer")
            users.path.write_text(text)
            os.utime(users.path, ns=(time.time_ns() + 10**9,) * 2)
            await server._revalidate_sessions(app)
            assert await _close_code(sock) == 1008

        app = server.create_app(ws, "tok")
        async with TestClient(TestServer(app)) as client:
            await _login(client, "tok")
            sock = await client.ws_connect("/ws/terminal?lab=t&node=a&mode=shell")
            await _read_until(sock, b"args")
            await server._revalidate_sessions(app)
            await sock.send_str(json.dumps({"t": "i", "d": "x"}))
            await asyncio.sleep(0.2)
            assert not sock.closed
            await sock.close()

    asyncio.run(scenario())


def test_session_watcher_runs_in_the_background(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    _fake_docker(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "REVALIDATE_INTERVAL", 0.05)
    users = UserStore(tmp_path / "users.yaml")
    token = users.add("alice", "viewer")

    async def scenario():
        app = server.create_app(ws, users=users)
        async with TestClient(TestServer(app)) as client:
            await _login(client, token)
            sock = await client.ws_connect("/ws/terminal?lab=t&node=a&mode=logs")
            await _read_until(sock, b"args")
            users.remove("alice")
            os.utime(users.path, ns=(time.time_ns() + 10**9,) * 2)
            assert await _close_code(sock) == 1008

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# Bad input
# ----------------------------------------------------------------------

def test_parse_message():
    p = server._parse_message
    assert p('{"t": "i", "d": "ls"}') == {"t": "i", "d": "ls"}
    assert p('{"t": "r", "c": 70000, "r": 1}') == {"t": "r", "c": 500, "r": 5}
    assert p('{"t": "r", "c": "90", "r": 30}') == {"t": "r", "c": 90, "r": 30}
    for bad in ("[1, 2]", "nope", '"str"', "null", '{"t": "i", "d": 5}', '{"t": "x"}',
                '{"t": "r", "c": "abc", "r": 1}', '{"t": "r"}', '{"t": "r", "c": 1e400, "r": 1}'):
        assert p(bad) is None, bad


def test_malformed_messages_are_ignored(tmp_path, monkeypatch, caplog):
    ws = _workspace(tmp_path)
    _fake_docker(tmp_path, monkeypatch, body='echo "args: $*"; exec cat')

    async def scenario():
        app = server.create_app(ws, "tok")
        async with TestClient(TestServer(app)) as client:
            await _login(client, "tok")
            with pytest.raises(WSServerHandshakeError) as exc:
                await client.ws_connect("/ws/terminal?lab=t&node=a&mode=shell&cols=abc")
            assert exc.value.status == 400
            assert (await client.get("/api/jobs/x?offset=1")).status == 404

            sock = await client.ws_connect("/ws/terminal?lab=t&node=a&mode=shell")
            await _read_until(sock, b"args")
            for bad in ("[1, 2]", "not json", '{"t": "r", "c": 70000, "r": 70000}',
                        '{"t": "r", "c": "x", "r": 1}', '{"t": "i", "d": {"a": 1}}',
                        '{"t": "i", "d": "\\ud800"}'):
                await sock.send_str(bad)
            await sock.send_str(json.dumps({"t": "i", "d": "still here\r"}))
            await _read_until(sock, b"still here")
            await sock.close()

    caplog.set_level(logging.ERROR)
    asyncio.run(scenario())
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_job_offset_is_validated(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    monkeypatch.setattr(Workspace, "runtime", lambda self, max_age=0: [])

    async def scenario():
        app = server.create_app(ws, "tok")
        app[server.JOBS].jobs["j1"] = state.Job("j1", "save", "t.clab.yml", lines=["a", "b"])
        async with TestClient(TestServer(app)) as client:
            await _login(client, "tok")
            assert (await client.get("/api/jobs/j1?offset=abc")).status == 400
            resp = await client.get("/api/jobs/j1?offset=-5")
            assert (await resp.json())["lines"] == ["a", "b"]
            assert (await (await client.get("/api/jobs/j1?offset=1")).json())["lines"] == ["b"]

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# Shutdown
# ----------------------------------------------------------------------

def test_shutdown_closes_open_terminals(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    _fake_docker(tmp_path, monkeypatch)

    async def scenario():
        app = server.create_app(ws, "tok")
        async with TestClient(TestServer(app)) as client:
            await _login(client, "tok")
            sock = await client.ws_connect("/ws/terminal?lab=t&node=a&mode=shell")
            await _read_until(sock, b"args")
            started = time.monotonic()
            await server._on_shutdown(app)
            assert await _close_code(sock) == 1001  # going away
            await _until(lambda: len(app[server.SESSIONS]) == 0)
            await _until(lambda: (tmp_path / "stops.log").exists())  # shell stopped in the node
            assert time.monotonic() - started < 4

    asyncio.run(scenario())


def test_gui_session_limit_flags(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "run", lambda *a, **kw: calls.append(kw))
    base = ["gui", "--dir", str(tmp_path), "--no-browser"]
    assert cli.main(base) == 0
    assert calls[-1]["max_sessions"] == 64 and calls[-1]["max_user_sessions"] == 16
    assert cli.main([*base, "--max-sessions", "5", "--max-user-sessions", "2"]) == 0
    assert calls[-1]["max_sessions"] == 5 and calls[-1]["max_user_sessions"] == 2
    with pytest.raises(SystemExit):
        cli.main([*base, "--max-sessions", "0"])


# ----------------------------------------------------------------------
# Runtime cache
# ----------------------------------------------------------------------

def test_runtime_is_cached_and_shared(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    calls = []
    gate = threading.Event()

    def inspect(self):
        calls.append(time.monotonic())
        gate.wait(5)
        return [HostState("localhost", ok=True, containers=[{"n": len(calls)}])]

    monkeypatch.setattr(Workspace, "_inspect_runtime", inspect)
    results = []
    threads = [threading.Thread(target=lambda: results.append(ws.runtime())) for _ in range(8)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(5)
    assert len(calls) == 1  # one inspect for eight callers
    assert len(results) == 8 and all(r is results[0] for r in results)

    assert ws.runtime() is results[0]       # reused while fresh
    assert ws.runtime(max_age=0) is not results[0]  # unless asked for a fresh one
    assert len(calls) == 2
    ws.invalidate_runtime()                 # e.g. a job finished
    assert ws.runtime()[0].containers == [{"n": 3}]

    def failing(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(Workspace, "_inspect_runtime", failing)
    ws.invalidate_runtime()
    with pytest.raises(RuntimeError):
        ws.runtime()
    assert ws._runtime_flight is None  # the next caller tries again


def test_finished_jobs_invalidate_the_runtime_cache(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    invalidated = threading.Event()
    monkeypatch.setattr(Workspace, "invalidate_runtime", lambda self: invalidated.set())

    class Deployer:
        def __init__(self, *a, **kw):
            pass

        def save(self, path):
            return {"hosts": {}}

    monkeypatch.setattr(state, "LabDeployer", Deployer)
    jobs = state.JobManager(ws, state.JobHistory(None))
    jobs.start("save", "t.clab.yml")
    assert invalidated.wait(5)


def test_a_browser_that_never_reads_is_dropped(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    _fake_docker(tmp_path, monkeypatch, body="exec yes 'lots of log output'")
    monkeypatch.setattr(server, "SEND_TIMEOUT", 0.5)

    async def scenario():
        app = server.create_app(ws, "tok")
        async with TestClient(TestServer(app)) as client:
            # A hand-made websocket client with a tiny receive buffer that
            # never reads what the server sends
            await _login(client, "tok")
            cookie = "; ".join(f"{k}={m.value}" for k, m in
                               client.session.cookie_jar.filter_cookies(client.make_url("/")).items())
            sock = socket.socket()
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            sock.connect((client.host, client.port))
            sock.sendall(
                b"GET /ws/terminal?lab=t&node=a&mode=logs HTTP/1.1\r\n"
                b"Host: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n"
                + f"Cookie: {cookie}\r\n\r\n".encode())
            try:
                await _until(lambda: len(app[server.SESSIONS]) == 1)
                # Output stops being read from `docker logs` while nobody takes it,
                # and the connection is dropped once the send times out
                await _until(lambda: len(app[server.SESSIONS]) == 0, timeout=10)
            finally:
                sock.close()

    asyncio.run(scenario())
