"""aiohttp web server for the clabfleet GUI.

Security: the GUI can open shells on lab nodes, so it only listens on
localhost by default and every request must carry the random token printed
at startup (exchanged for a SameSite=Strict cookie on first visit, like
Jupyter). Websocket and POST requests must also come from the GUI's own
origin, so other websites open in the browser cannot drive it.
"""

import asyncio
import hmac
import json
import logging
import secrets
import webbrowser
from pathlib import Path

from aiohttp import WSMsgType, web

from ..nodes import access_modes, terminal_command
from .state import JobManager, Workspace
from .terminals import LocalTerminal, SSHTerminal

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
COOKIE = "clabfleet_token"

WORKSPACE = web.AppKey("workspace", Workspace)
JOBS = web.AppKey("jobs", JobManager)
TOKEN = web.AppKey("token", str)


def create_app(workspace: Workspace, token: str) -> web.Application:
    app = web.Application(middlewares=[_auth_middleware])
    app[WORKSPACE] = workspace
    app[JOBS] = JobManager(workspace)
    app[TOKEN] = token
    app.router.add_get("/", _index)
    app.router.add_get("/api/state", _state)
    app.router.add_get("/api/hosts", _hosts)
    app.router.add_get("/api/topologies/{id:.+}", _topology)
    app.router.add_post("/api/jobs", _start_job)
    app.router.add_get("/api/jobs/{id}", _job)
    app.router.add_get("/ws/terminal", _terminal)
    app.router.add_static("/static", STATIC_DIR)
    app.on_shutdown.append(_on_shutdown)
    return app


def run(workspace: Workspace, host: str = "127.0.0.1", port: int = 8650,
        open_browser: bool = True) -> None:
    token = secrets.token_urlsafe(24)
    app = create_app(workspace, token)
    url = f"http://{'localhost' if host in ('127.0.0.1', '::1') else host}:{port}/?token={token}"

    async def _announce(_app):
        print(f"clabfleet GUI running at:\n\n    {url}\n\nPress Ctrl+C to stop.", flush=True)
        if open_browser:
            asyncio.get_running_loop().run_in_executor(None, webbrowser.open, url)

    app.on_startup.append(_announce)
    web.run_app(app, host=host, port=port, print=None)


async def _on_shutdown(app):
    app[WORKSPACE].close()


# ----------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------

def _token_ok(request: web.Request, candidate: str | None) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate, request.app[TOKEN])


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    if request.path.startswith("/static/"):
        return await handler(request)

    query_token = request.query.get("token")
    if request.path == "/" and _token_ok(request, query_token):
        # Swap the URL token for a cookie and drop it from the address bar
        resp = web.Response(status=302, headers={"Location": "/"})
        resp.set_cookie(COOKIE, query_token, httponly=True, samesite="Strict")
        return resp

    if not _token_ok(request, request.cookies.get(COOKIE)):
        if request.path == "/":
            return web.Response(
                status=401, content_type="text/html",
                text="<p>Open the GUI with the URL (including <code>?token=</code>) "
                     "printed by <code>clabfleet gui</code>.</p>",
            )
        raise web.HTTPUnauthorized(text="missing or invalid token")

    if request.method != "GET" or request.path.startswith("/ws/"):
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden(text="cross-origin request refused")
    return await handler(request)


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
        "job": jobs.current.view(len(jobs.current.lines)) if jobs.current else None,
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


async def _start_job(request):
    body = await request.json()
    try:
        options = body.get("options") if isinstance(body.get("options"), dict) else None
        job = request.app[JOBS].start(body.get("action", ""), body.get("topology", ""), options)
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc))
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    except RuntimeError as exc:
        raise web.HTTPConflict(text=str(exc))
    return web.json_response(job.view())


async def _job(request):
    job = request.app[JOBS].jobs.get(request.match_info["id"])
    if not job:
        raise web.HTTPNotFound(text="unknown job")
    offset = int(request.query.get("offset", "0"))
    return web.json_response(job.view(offset))


# ----------------------------------------------------------------------
# Terminals
# ----------------------------------------------------------------------

async def _terminal(request):
    ws_resp = web.WebSocketResponse(heartbeat=30)
    await ws_resp.prepare(request)

    workspace: Workspace = request.app[WORKSPACE]
    q = request.query
    cols = max(20, min(int(q.get("cols", "120")), 500))
    rows = max(5, min(int(q.get("rows", "30")), 200))

    async def fail(message: str):
        await ws_resp.send_bytes(f"\r\n\x1b[31m{message}\x1b[0m\r\n".encode())
        await ws_resp.send_str(json.dumps({"t": "exit", "code": -1}))
        await ws_resp.close()
        return ws_resp

    try:
        node = await asyncio.to_thread(workspace.find_node, q.get("lab", ""), q.get("node", ""))
        argv = terminal_command(q.get("mode", ""), node["kind"], node["container"], node["ipv4"])
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
    return ws_resp
