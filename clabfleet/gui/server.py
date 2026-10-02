"""aiohttp web server for the clabfleet GUI.

Security: the GUI can open shells on lab nodes, so it only listens on
localhost by default and every request must be authenticated. Two modes:

- Single token (no users file): the random token printed at startup is
  exchanged for a SameSite=Strict cookie on first visit, like Jupyter.
  Whoever has it is an operator.
- Named users (``clabfleet user add``): each user's own token is exchanged
  for a server-side session, and the user's role decides what they may do
  (see ``_allowed``: anything but a plain read needs an operator unless the
  handler is marked ``allow_viewer``). Logins, jobs, edits and terminal
  sessions go to the audit log.

Websocket and POST requests must also come from the GUI's own origin, so
other websites open in the browser cannot drive it.
"""

import asyncio
import hmac
import json
import logging
import secrets
import socket
import ssl
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from aiohttp import WSMsgType, web
from yarl import URL

from ..capture import GUI_MAX_BYTES, CaptureError
from ..nodes import access_modes, terminal_command
from ..snapshots import SnapshotError
from .auth import (
    ACCESS_ATTR, OPERATOR, VIEWER, AuditLog, User, UserStore, allow_viewer, operator_only,
)
from .captures import open_capture, pcap_filename, spec_from_query
from .editing import EditConflict
from .state import Job, JobManager, UnloadableTopology, Workspace
from .terminals import CaptureSession, LocalTerminal, SSHTerminal

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
COOKIE = "clabfleet_token"            # single-token mode: the token itself
SESSION_COOKIE = "clabfleet_session"  # named users: a session id
SESSION_IDLE = 12 * 3600              # seconds without a request before a session ends
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
LOOPBACK_NAMES = ("127.0.0.1", "::1", "localhost")

# request[USER_KEY]: the User making the request (RequestKey needs aiohttp 3.12+)
USER_KEY = web.RequestKey("user", User) if hasattr(web, "RequestKey") else "clabfleet.user"


@dataclass
class _Session:
    name: str
    token_sha256: str  # the token the session came from; rotating it ends the session
    last_seen: float


class Auth:
    """Who a request comes from, in single-token or named-user mode."""

    def __init__(self, token: Optional[str] = None, users: Optional[UserStore] = None,
                 public_url: Optional[str] = None):
        if not token and not users:
            raise ValueError("Need a token or a users file")
        self.token = token
        self.users = users
        self.public_url = public_url
        self._sessions: dict[str, _Session] = {}

    @property
    def multi_user(self) -> bool:
        return self.users is not None

    def login(self, token: str) -> Optional[tuple[User, str, str]]:
        """(user, cookie name, cookie value) for a valid login token."""
        if not self.multi_user:
            if token and hmac.compare_digest(token, self.token):
                return User("", OPERATOR), COOKIE, token
            return None
        user = self.users.authenticate(token)
        if not user:
            return None
        self._expire()
        sid = secrets.token_urlsafe(32)
        self._sessions[sid] = _Session(user.name, user.token_sha256, time.monotonic())
        return user, SESSION_COOKIE, sid

    def identify(self, request: web.Request) -> Optional[User]:
        if not self.multi_user:
            candidate = request.cookies.get(COOKIE)
            if candidate and hmac.compare_digest(candidate, self.token):
                return User("", OPERATOR)
            return None
        session = self._sessions.get(request.cookies.get(SESSION_COOKIE, ""))
        if not session or time.monotonic() - session.last_seen > SESSION_IDLE:
            return None
        # Re-read the user each time: removed or rotated users are out at once
        user = self.users.get(session.name)
        if not user or not hmac.compare_digest(user.token_sha256, session.token_sha256):
            return None
        session.last_seen = time.monotonic()
        return user

    def logout(self, request: web.Request) -> None:
        self._sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)

    def _expire(self) -> None:
        cutoff = time.monotonic() - SESSION_IDLE
        for sid in [k for k, v in self._sessions.items() if v.last_seen < cutoff]:
            del self._sessions[sid]


WORKSPACE = web.AppKey("workspace", Workspace)
JOBS = web.AppKey("jobs", JobManager)
AUTH = web.AppKey("auth", Auth)
AUDIT = web.AppKey("audit", AuditLog)
CAPTURES = web.AppKey("captures", set)


def create_app(workspace: Workspace, token: Optional[str] = None, *,
               users: Optional[UserStore] = None, audit: Optional[AuditLog] = None,
               public_url: Optional[str] = None) -> web.Application:
    """The GUI app. Pass ``token`` for single-token mode or ``users`` for
    named users; ``public_url`` is the address browsers use when it differs
    from what the server sees (e.g. behind a TLS-terminating proxy)."""
    # Topologies with inline startup configs can be large
    app = web.Application(middlewares=[_auth_middleware], client_max_size=16 * 1024 * 1024)
    app[WORKSPACE] = workspace
    app[JOBS] = JobManager(workspace)
    app[AUTH] = Auth(token, users, public_url)
    app[AUDIT] = audit_log = audit or AuditLog(None)
    app[JOBS].on_finished = lambda job: _audit_job_finished(audit_log, job)
    app.on_response_prepare.append(_security_headers)
    app[CAPTURES] = set()  # running packet captures, stopped on shutdown
    app.router.add_get("/", _index)
    app.router.add_post("/logout", _logout)
    app.router.add_get("/api/me", _me)
    app.router.add_get("/api/state", _state)
    app.router.add_get("/api/hosts", _hosts)
    app.router.add_get("/api/topologies/{id:.+}", _topology)
    app.router.add_put("/api/topologies/{id:.+}", _save_topology)
    app.router.add_get("/api/live/{id:.+}", _live)
    app.router.add_post("/api/validate/{id:.+}", _validate)
    app.router.add_put("/api/positions/{id:.+}", _save_positions)
    app.router.add_get("/api/diff/{id:.+}", _node_diff)
    app.router.add_get("/api/jobs", _jobs)
    app.router.add_post("/api/jobs", _start_job)
    app.router.add_get("/api/jobs/{id}", _job)
    app.router.add_get("/api/capture", _capture_download)
    app.router.add_get("/ws/terminal", _terminal)
    app.router.add_get("/ws/capture", _capture_live)
    app.router.add_static("/static", STATIC_DIR)
    app.on_shutdown.append(_on_shutdown)
    return app


def run(workspace: Workspace, host: str = "127.0.0.1", port: int = 8650,
        open_browser: bool = True, *, users: Optional[UserStore] = None,
        audit: Optional[AuditLog] = None, ssl_context: Optional[ssl.SSLContext] = None,
        public_url: Optional[str] = None) -> None:
    token = None if users else secrets.token_urlsafe(24)
    app = create_app(workspace, token, users=users, audit=audit, public_url=public_url)
    base = public_url.rstrip("/") if public_url else _base_url(host, port, ssl_context is not None)

    async def _announce(_app):
        if users:
            url = f"{base}/"
            lines = [f"clabfleet GUI running at:\n\n    {url}\n",
                     f"Users log in with their own link from `clabfleet user add`: "
                     f"{base}/?token=<token>",
                     f"Users file: {users.path}"]
            if not users.users():
                lines.append("No users yet: add one with `clabfleet user add NAME`.")
        else:
            url = f"{base}/?token={token}"
            lines = [f"clabfleet GUI running at:\n\n    {url}\n"]
        if audit and audit.path:
            lines.append(f"Audit log: {audit.path}")
        lines.append("Press Ctrl+C to stop.")
        print("\n".join(lines), flush=True)
        if open_browser and not users:  # a named user's token is not ours to use
            asyncio.get_running_loop().run_in_executor(None, webbrowser.open, url)

    app.on_startup.append(_announce)
    web.run_app(app, host=host, port=port, ssl_context=ssl_context, print=None)


def _base_url(host: str, port: int, tls: bool) -> str:
    if host in LOOPBACK_NAMES:
        name = "localhost"
    elif host in ("0.0.0.0", "::", ""):
        name = socket.gethostname()
    else:
        name = f"[{host}]" if ":" in host else host
    return f"{'https' if tls else 'http'}://{name}:{port}"


async def _on_shutdown(app):
    # Kill tcpdump in the containers before the runners go away
    captures = list(app[CAPTURES])
    if captures:
        await asyncio.gather(*(asyncio.to_thread(c.stop) for c in captures))
    app[WORKSPACE].close()


# ----------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------

def current_user(request: web.Request) -> User:
    """The authenticated user of a request (set by the auth middleware)."""
    return request[USER_KEY]


def audit(request: web.Request, event: str, **details) -> None:
    """Record an event by the request's user in the audit log."""
    request.app[AUDIT].record(event, request.get(USER_KEY), request.remote, **details)


def _is_websocket(request: web.Request) -> bool:
    return (request.path.startswith("/ws/")
            or request.headers.get("Upgrade", "").lower() == "websocket")


def _allowed(request: web.Request, user: User) -> bool:
    """Default deny for viewers: only plain reads, unless the handler is
    marked with ``allow_viewer`` (or ``operator_only`` for a sensitive GET).
    So a new POST/PUT/DELETE route or websocket is operator-only until
    someone decides otherwise."""
    if user.is_operator:
        return True
    access = getattr(request.match_info.handler, ACCESS_ATTR, None)
    if access is not None:
        return access == VIEWER
    return request.method in SAFE_METHODS and not _is_websocket(request)


def _origin_key(url: URL) -> tuple:
    return (url.scheme.lower(), (url.host or "").lower(), url.port)  # port: default filled in


def _same_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return True  # not sent by a browser
    try:
        allowed = {_origin_key(URL(f"{request.scheme}://{request.host}"))}
        if request.app[AUTH].public_url:
            allowed.add(_origin_key(URL(request.app[AUTH].public_url)))
        return _origin_key(URL(origin)) in allowed
    except ValueError:
        return False


def _cookie_secure(request: web.Request) -> bool:
    public_url = request.app[AUTH].public_url
    return request.secure or bool(public_url and public_url.startswith("https:"))


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    if request.path.startswith("/static/"):
        return await handler(request)
    auth: Auth = request.app[AUTH]

    query_token = request.query.get("token")
    if request.path == "/" and query_token is not None:
        login = auth.login(query_token)
        if login:
            # Swap the URL token for a cookie and drop it from the address bar
            user, name, value = login
            resp = web.Response(status=302, headers={"Location": "/"})
            resp.set_cookie(name, value, httponly=True, samesite="Strict",
                            secure=_cookie_secure(request) or None)
            request.app[AUDIT].record("login", user, request.remote)
            return resp
        request.app[AUDIT].record("login_failed", None, request.remote, reason="invalid token")

    user = auth.identify(request)
    if not user:
        if request.path == "/":
            how = ("your own login link (from <code>clabfleet user add</code>)"
                   if auth.multi_user else
                   "the URL (including <code>?token=</code>) printed by <code>clabfleet gui</code>")
            return web.Response(status=401, content_type="text/html",
                                text=f"<p>Open the GUI with {how}.</p>")
        raise web.HTTPUnauthorized(text="missing or invalid token")
    request[USER_KEY] = user

    if request.method != "GET" or _is_websocket(request):
        if not _same_origin(request):
            raise web.HTTPForbidden(text="cross-origin request refused")
    if not _allowed(request, user):
        audit(request, "denied", method=request.method, path=request.path)
        raise web.HTTPForbidden(text=f"Not allowed for the {user.role} role")
    return await handler(request)


async def _security_headers(request, response):
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")


@allow_viewer
async def _logout(request):
    request.app[AUTH].logout(request)
    audit(request, "logout")
    resp = web.json_response({"ok": True})
    for name in (COOKIE, SESSION_COOKIE):
        resp.del_cookie(name)
    return resp


async def _me(request):
    user = current_user(request)
    return web.json_response({"user": user.name or None, "role": user.role,
                              "multi_user": request.app[AUTH].multi_user})


def _audit_job_finished(log: AuditLog, job: Job) -> None:
    log.record("job_finished", job.user, None, job=job.id, action=job.action,
               topology=job.topology, lab=job.lab, status=job.status,
               seconds=round((job.finished or time.time()) - job.started, 1))


# ----------------------------------------------------------------------
# Pages & API
# ----------------------------------------------------------------------

async def _index(request):
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


async def _state(request):
    ws: Workspace = request.app[WORKSPACE]
    topologies = await asyncio.to_thread(ws.topologies)
    runtime = await asyncio.to_thread(ws.runtime)
    jobs: JobManager = request.app[JOBS]
    return web.json_response({
        "hosts": [{"name": h.name, "host": h.host, "local": h.is_local} for h in ws.cluster.hosts],
        "multi_host": ws.multi_host,
        "topologies": topologies,
        "runtime": [
            {"host": s.name, "ok": s.ok, "error": s.error,
             "containers": [{**c, "modes": access_modes(c["kind"])} for c in s.containers]}
            for s in runtime
        ],
        "jobs": [j.summary() for j in jobs.recent()],
    })


async def _hosts(request):
    ws: Workspace = request.app[WORKSPACE]
    return web.json_response(await asyncio.to_thread(ws.host_status))


async def _topology(request):
    ws: Workspace = request.app[WORKSPACE]
    try:
        detail = await asyncio.to_thread(ws.topology_detail, request.match_info["id"])
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc))
    return web.json_response(detail)


async def _live(request):
    """Link states and node CPU/memory of a topology's running lab (cached;
    asking refreshes it in the background)."""
    ws: Workspace = request.app[WORKSPACE]
    try:
        live = await asyncio.to_thread(ws.live_state, request.match_info["id"])
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc))
    return web.json_response(live)


async def _json_body(request) -> dict:
    """The request's JSON object body; 400 for anything else."""
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="Request body must be JSON")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="Request body must be a JSON object")
    return body


def _refuse_while_busy(request, topo_id: str) -> None:
    for job in request.app[JOBS].running():
        if job.topology == topo_id:
            raise web.HTTPConflict(
                text=f"A {job.action} job is running for this lab; save when it finishes")


async def _validate(request):
    body = await _json_body(request)
    ws: Workspace = request.app[WORKSPACE]
    try:
        report = await asyncio.to_thread(ws.validate_yaml, request.match_info["id"],
                                         str(body.get("yaml", "")))
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc))
    return web.json_response(report)


async def _save_topology(request):
    topo_id = request.match_info["id"]
    body = await _json_body(request)
    _refuse_while_busy(request, topo_id)
    ws: Workspace = request.app[WORKSPACE]
    try:
        result = await asyncio.to_thread(ws.save_yaml, topo_id, str(body.get("yaml", "")),
                                         str(body.get("base_hash", "")))
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc))
    except UnloadableTopology as exc:
        return web.json_response({"error": str(exc), "validation": exc.report}, status=400)
    except EditConflict as exc:
        raise web.HTTPConflict(text=str(exc))
    audit(request, "topology_saved", topology=topo_id, hash=result["detail"].get("hash"))
    return web.json_response(result)


async def _save_positions(request):
    topo_id = request.match_info["id"]
    body = await _json_body(request)
    _refuse_while_busy(request, topo_id)
    ws: Workspace = request.app[WORKSPACE]
    try:
        detail = await asyncio.to_thread(ws.save_positions, topo_id, body.get("positions"),
                                         str(body.get("base_hash", "")))
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc))
    except EditConflict as exc:
        raise web.HTTPConflict(text=str(exc))
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    except RuntimeError as exc:  # ruamel.yaml missing
        raise web.HTTPNotImplemented(text=str(exc))
    audit(request, "positions_saved", topology=topo_id,
          nodes=sorted((body.get("positions") or {}).keys()))
    return web.json_response(detail)


async def _node_diff(request):
    """?node=X&against=previous|startup: the node's latest snapshot diff."""
    ws: Workspace = request.app[WORKSPACE]
    try:
        result = await asyncio.to_thread(
            ws.node_diff, request.match_info["id"], request.query.get("node", ""),
            request.query.get("against", "startup"))
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc))
    except (SnapshotError, ValueError) as exc:  # no snapshots yet, unknown node
        raise web.HTTPBadRequest(text=str(exc))
    return web.json_response(result)


async def _start_job(request):
    body = await _json_body(request)
    try:
        options = body.get("options") if isinstance(body.get("options"), dict) else None
        job = request.app[JOBS].start(body.get("action", ""), body.get("topology", ""), options,
                                      user=current_user(request).name)
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc))
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    except RuntimeError as exc:
        raise web.HTTPConflict(text=str(exc))
    audit(request, "job_started", job=job.id, action=job.action, topology=job.topology,
          lab=job.lab, options=job.options)
    return web.json_response(job.view())


async def _jobs(request):
    return web.json_response([j.summary() for j in request.app[JOBS].recent()])


async def _job(request):
    job = request.app[JOBS].jobs.get(request.match_info["id"])
    if not job:
        raise web.HTTPNotFound(text="unknown job")
    offset = int(request.query.get("offset", "0"))
    return web.json_response(job.view(offset))


# ----------------------------------------------------------------------
# Terminals
# ----------------------------------------------------------------------

@allow_viewer  # but only for the read-only Logs mode, checked below
async def _terminal(request):
    q = request.query
    mode = q.get("mode", "")
    read_only = not current_user(request).is_operator
    if read_only and mode != "logs":
        audit(request, "denied", method=request.method, path=request.path, mode=mode)
        raise web.HTTPForbidden(text="Viewers can only follow node logs")

    ws_resp = web.WebSocketResponse(heartbeat=30)
    await ws_resp.prepare(request)

    workspace: Workspace = request.app[WORKSPACE]
    cols = max(20, min(int(q.get("cols", "120")), 500))
    rows = max(5, min(int(q.get("rows", "30")), 200))

    async def fail(message: str):
        await ws_resp.send_bytes(f"\r\n\x1b[31m{message}\x1b[0m\r\n".encode())
        await ws_resp.send_str(json.dumps({"t": "exit", "code": -1}))
        await ws_resp.close()
        return ws_resp

    try:
        node = await asyncio.to_thread(workspace.find_node, q.get("lab", ""), q.get("node", ""))
        argv = terminal_command(mode, node["kind"], node["container"], node["ipv4"])
        host = workspace.host(node["host"])
        if host.is_local:
            session = LocalTerminal(argv, cols, rows)
        else:
            client = await asyncio.to_thread(workspace.runner(host).client)
            session = SSHTerminal(client, argv, cols, rows)
        session.start(asyncio.get_running_loop())
    except KeyError as exc:
        return await fail(exc.args[0])
    except Exception as exc:
        return await fail(str(exc))

    async def pump_output():
        async for chunk in session.output():
            if ws_resp.closed:
                return
            await ws_resp.send_bytes(chunk)
        if not ws_resp.closed:
            await ws_resp.send_str(json.dumps({"t": "exit", "code": session.exit_code}))
            await ws_resp.close()

    where = {"lab": q.get("lab", ""), "node": q.get("node", ""), "mode": mode, "host": node["host"]}
    audit(request, "terminal_opened", **where)
    opened = time.monotonic()
    pump = asyncio.create_task(pump_output())
    try:
        async for msg in ws_resp:
            if msg.type != WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            if data.get("t") == "i" and not read_only:  # viewers' logs take no input
                session.write(data.get("d", "").encode())
            elif data.get("t") == "r":
                session.resize(int(data["c"]), int(data["r"]))
    finally:
        session.close()
        pump.cancel()
        audit(request, "terminal_closed", **where, seconds=round(time.monotonic() - opened, 1),
              exit_code=session.exit_code)
    return ws_resp


# ----------------------------------------------------------------------
# Packet capture
# ----------------------------------------------------------------------

def _capture_error(exc: Exception) -> web.HTTPException:
    if isinstance(exc, KeyError):
        return web.HTTPNotFound(text=exc.args[0])
    if isinstance(exc, ValueError):
        return web.HTTPBadRequest(text=str(exc))
    return web.HTTPBadGateway(text=str(exc))


async def _capture_live(request):
    """Live decode of a node interface in a terminal tab.

    /ws/capture?topo=<id>&node=&iface=&filter=&count=&duration=
    """
    ws_resp = web.WebSocketResponse(heartbeat=30)
    await ws_resp.prepare(request)
    q = request.query
    loop = asyncio.get_running_loop()
    session = CaptureSession(loop)

    try:
        spec = spec_from_query(q, "text")
        capture, _ = await asyncio.to_thread(open_capture, request.app[WORKSPACE],
                                             q.get("topo", ""), spec, session.message)
    except Exception as exc:
        known = isinstance(exc, (KeyError, ValueError, CaptureError))
        text = _capture_error(exc).text if known else str(exc)
        await ws_resp.send_bytes(f"\r\n\x1b[31m{text}\x1b[0m\r\n".encode())
        await ws_resp.send_str(json.dumps({"t": "exit", "code": -1}))
        await ws_resp.close()
        return ws_resp

    captures = request.app[CAPTURES]
    captures.add(capture)
    session.capture = capture
    filt = f", filter '{spec.bpf_filter}'" if spec.bpf_filter else ""
    session.message(f"Capturing on {capture.describe()}{filt} for up to "
                    f"{spec.duration:g}s. Ctrl+C or close the tab to stop.")
    where = _capture_where(q, spec, "live")
    audit(request, "capture_started", **where)
    started = time.monotonic()
    try:
        session.start(loop)
        await _serve_session(ws_resp, session)
    finally:
        session.close()
        try:
            await session.wait_closed()
        finally:
            captures.discard(capture)
            audit(request, "capture_finished", **where,
                  seconds=round(time.monotonic() - started, 1))
    return ws_resp


async def _serve_session(ws_resp, session) -> None:
    """Shuttle a started session's output and the browser's input until either ends."""
    async def pump_output():
        async for chunk in session.output():
            if ws_resp.closed:
                return
            await ws_resp.send_bytes(chunk)
        if not ws_resp.closed:
            await ws_resp.send_str(json.dumps({"t": "exit", "code": session.exit_code}))
            await ws_resp.close()

    pump = asyncio.create_task(pump_output())
    try:
        async for msg in ws_resp:
            if msg.type != WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            if data.get("t") == "i":
                session.write(data.get("d", "").encode())
            elif data.get("t") == "r":
                session.resize(int(data["c"]), int(data["r"]))
    finally:
        session.close()
        pump.cancel()


def _capture_where(q, spec, mode: str) -> dict:
    return {"topology": q.get("topo", ""), "node": spec.node, "interface": spec.interface,
            "filter": spec.bpf_filter, "mode": mode}


def _client_gone(request) -> bool:
    transport = request.transport
    return transport is None or transport.is_closing()


@operator_only
async def _capture_download(request):
    """Stream a pcap of a node interface as a download.

    /api/capture?topo=<id>&node=&iface=&filter=&count=&duration=&snaplen=

    Ends at the capture's count or duration, at GUI_MAX_BYTES, or as soon as
    the browser cancels the download; tcpdump is stopped in every case.
    """
    q = request.query
    messages: list[str] = []
    try:
        spec = spec_from_query(q, "pcap")
        capture, lab = await asyncio.to_thread(open_capture, request.app[WORKSPACE],
                                               q.get("topo", ""), spec, messages.append)
    except (KeyError, ValueError, CaptureError) as exc:
        raise _capture_error(exc)

    captures = request.app[CAPTURES]
    captures.add(capture)
    where = _capture_where(q, spec, "pcap")
    audit(request, "capture_started", **where)
    started = time.monotonic()
    sent = 0
    try:
        read = asyncio.ensure_future(asyncio.to_thread(capture.read))
        # tcpdump fails fast on a bad filter: report that as an error
        # rather than as an empty download
        await asyncio.wait({read}, timeout=3)
        if read.done() and not read.result():
            detail = "; ".join(m for m in messages if "listening on" not in m)
            raise web.HTTPBadGateway(text=f"tcpdump failed: {detail or 'no output'}")

        resp = web.StreamResponse(headers={
            "Content-Type": "application/vnd.tcpdump.pcap",
            "Content-Disposition": f'attachment; filename="{pcap_filename(lab, spec)}"',
            "Cache-Control": "no-store",
        })
        await resp.prepare(request)
        while True:
            done, _ = await asyncio.wait({read}, timeout=1)
            if not done:
                if _client_gone(request):
                    break
                continue
            chunk = read.result()
            if not chunk:
                break
            try:
                await resp.write(chunk)
            except ConnectionError:
                break
            sent += len(chunk)
            if sent >= GUI_MAX_BYTES:
                break
            read = asyncio.ensure_future(asyncio.to_thread(capture.read))
        if not _client_gone(request):
            await resp.write_eof()
        return resp
    finally:
        # Shielded: a cancelled handler must still kill tcpdump
        try:
            await asyncio.shield(asyncio.to_thread(capture.stop))
        finally:
            captures.discard(capture)
            audit(request, "capture_finished", **where,
                  seconds=round(time.monotonic() - started, 1), bytes=sent)
