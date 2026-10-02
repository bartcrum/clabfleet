"""aiohttp web server for the clabfleet GUI.

Security: the GUI can open shells on lab nodes, so it only listens on
localhost by default and every request must be authenticated. Two modes:

- Single token (``--single-token``): whoever has the random token printed
  at startup is an operator.
- Named users (the users file; a new install starts with the user admin
  and a random password printed at start-up, which it must change first): each user logs in with a password or
  their own token, and the user's role decides what they may do (see
  ``_allowed``: anything but a plain read needs an operator unless the
  handler is marked ``allow_viewer``). Logins, jobs, edits and terminal
  sessions go to the audit log.

In both modes passwords and tokens are only sent in the body of
``POST /login`` (login links carry the token in the URL fragment, which
browsers do not send to the server) and are exchanged for a random
session id in a SameSite=Strict cookie; logging out ends the session.
Sessions are kept on disk (hashed) so a restart on the same port logs
nobody out. Websocket and POST requests must also come from the GUI's
own origin, so other websites open in the browser cannot drive it.
"""

import asyncio
import contextlib
import functools
import hmac
import json
import logging
import secrets
import socket
import ssl
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from aiohttp import WSCloseCode, WSMsgType, web
from aiohttp.abc import AbstractAccessLogger
from yarl import URL

from ..capture import GUI_MAX_BYTES, CaptureError
from ..nodes import access_modes, run_docker, terminal_command, terminal_stop_command
from ..snapshots import SnapshotError
from .auth import (
    ACCESS_ATTR, BOOTSTRAP_USER, DEFAULT_USERS_FILE, OPERATOR, VIEWER,
    AuditLog, SessionFile, User, UserStore, allow_viewer, check_new_password, hash_token,
    login_link, operator_only, temporary_password,
)
from .captures import open_capture, pcap_filename, spec_from_query
from .editing import EditConflict
from .sessions import (
    CAPTURE, MAX_SESSIONS, MAX_USER_SESSIONS, TERMINAL, OpenSession, SessionLimitError,
    SessionRegistry,
)
from .state import Job, JobManager, UnloadableTopology, Workspace
from .terminals import CaptureSession, LocalTerminal, SSHTerminal

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "clabfleet_session"  # plus the instance id; "__Host-" prefix over HTTPS
SESSION_IDLE = 7 * 24 * 3600          # seconds without a request before a session ends
SESSION_MAX_AGE = 30 * 24 * 3600      # seconds after login a session ends regardless
SESSION_SAVE_EVERY = 60               # seconds between writes of last-use times only
SESSION_FILE = "gui-sessions-{instance}.json"  # next to the users file, or in ~/.clabfleet
SESSIONS_PER_USER = 20                # a further login ends that user's oldest session
LOGIN_FAILURES = 5                    # failed logins allowed per remote address ...
LOGIN_WINDOW = 60                     # ... in this many seconds; then 429 until it ends
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PUBLIC_PATHS = {"/", "/login"}        # the page (it shows the login form) and the login
MUST_CHANGE_PATHS = {"/api/me", "/api/password", "/logout"}  # all a must_change user may do
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
LOOPBACK_NAMES = ("127.0.0.1", "::1", "localhost")
SEND_TIMEOUT = 30       # seconds a browser may take to accept output before it is cut off
CLOSE_TIMEOUT = 3       # seconds to close a websocket politely before dropping the connection
REVALIDATE_INTERVAL = 5  # seconds between checks that open sessions' logins are still valid
SHUTDOWN_TIMEOUT = 5    # seconds open requests get to finish when the GUI stops

# request[USER_KEY]: the User making the request (RequestKey needs aiohttp 3.12+)
USER_KEY = web.RequestKey("user", User) if hasattr(web, "RequestKey") else "clabfleet.user"


@dataclass
class _Session:
    name: str
    credential: str    # User.credential at login: a new token or password ends the session
    created: float     # Unix seconds, so sessions kept on disk survive a restart
    last_seen: float


@dataclass
class _Failures:
    window_start: float
    count: int = 0    # failed logins in this window
    refused: int = 0  # attempts refused with 429 in this window


class Auth:
    """Who a request comes from, in single-token or named-user mode.

    Both modes hand out the same kind of session: a random id in a cookie,
    mapped to a user here. The token itself never goes into a cookie.

    With a ``session_file``, sessions are kept on disk (by the hash of their
    id) and a restarted GUI with the same ``instance`` (its port) takes them
    over, so nobody has to log in again. In single-token mode the token
    changes with each start; sessions then belong to "the operator" rather
    than to one token."""

    def __init__(self, token: Optional[str] = None, users: Optional[UserStore] = None,
                 public_url: Optional[str] = None, *, instance: Optional[str] = None,
                 session_file: Optional[SessionFile] = None):
        if not token and not users:
            raise ValueError("Need a token or a users file")
        self.token = token
        self.users = users
        self.public_url = public_url
        # Browsers send a host's cookies to all of its ports, so each GUI
        # instance has its own cookie name
        self.instance = instance or secrets.token_hex(4)
        self._operator = None if users else User("", OPERATOR, hash_token(token))
        self._file = session_file
        self._sessions: dict[str, _Session] = {}  # by hash_token(session id)
        if session_file:
            self._sessions = {k: _Session(**v) for k, v in session_file.load().items()}
            self._expire()
        self._saved = time.time()
        self._failures: dict[str, _Failures] = {}

    @property
    def multi_user(self) -> bool:
        return self.users is not None

    def cookie_name(self, secure: bool) -> str:
        name = f"{SESSION_COOKIE}_{self.instance}"
        # The browser only accepts a __Host- cookie with Secure, Path=/ and
        # no Domain, so nothing else on the host can set or widen it
        return f"__Host-{name}" if secure else name

    def check_token(self, token) -> Optional[User]:
        """The user a login token belongs to, or None."""
        if not isinstance(token, str) or not token:
            return None
        try:
            raw = token.encode()
        except UnicodeEncodeError:  # a lone surrogate from JSON
            return None
        if not self.multi_user:
            return self._operator if hmac.compare_digest(raw, self.token.encode()) else None
        return self.users.authenticate(token)

    def check_password(self, name, password) -> Optional[User]:
        """The named user if ``password`` is theirs (slow: scrypt)."""
        if not self.multi_user or not isinstance(name, str) or not isinstance(password, str):
            return None
        return self.users.check_password(name, password)

    def start_session(self, user: User) -> str:
        """A new session id for ``user``. Beyond SESSIONS_PER_USER, the
        user's oldest sessions end."""
        self._expire()
        now = time.time()
        mine = sorted((s.created, key) for key, s in self._sessions.items() if s.name == user.name)
        for _, key in mine[:max(0, len(mine) - SESSIONS_PER_USER + 1)]:
            del self._sessions[key]
        sid = secrets.token_urlsafe(32)
        # Single-token mode: no token to tie the session to (it changes per start)
        tied = user.credential if self.multi_user else ""
        self._sessions[hash_token(sid)] = _Session(user.name, tied, now, now)
        self._save()
        return sid

    def identify(self, request: web.Request, touch: bool = True) -> Optional[User]:
        """The request's user, or None. ``touch=False`` checks a long-lived
        request (an open terminal) without counting it as activity."""
        session = self._sessions.get(self._session_key(request))
        now = time.time()
        if not session or self._expired(session, now):
            return None
        if self.multi_user:
            # Re-read the user each time: removed or rotated users are out at once
            user = self.users.get(session.name)
            if not user or not hmac.compare_digest(user.credential, session.credential):
                return None
        elif session.name:  # a named user's session from a run with a users file
            return None
        else:
            user = self._operator
        if touch:
            session.last_seen = now
            if now - self._saved > SESSION_SAVE_EVERY:
                self._save()
        return user

    def logout(self, request: web.Request) -> None:
        if self._sessions.pop(self._session_key(request), None):
            self._save()

    def _session_key(self, request: web.Request) -> str:
        sid = request.cookies.get(self.cookie_name(_cookie_secure(request)), "")
        return hash_token(sid) if sid else ""

    @staticmethod
    def _expired(session: _Session, now: float) -> bool:
        return (now - session.last_seen > SESSION_IDLE
                or now - session.created > SESSION_MAX_AGE)

    def _expire(self) -> None:
        now = time.time()
        for key in [k for k, v in self._sessions.items() if self._expired(v, now)]:
            del self._sessions[key]

    def _save(self) -> None:
        self._saved = time.time()
        if self._file:
            self._file.save({k: asdict(v) for k, v in self._sessions.items()})

    # --- failed logins, per remote address and per user name ---
    # (keys: the address, or "user:<name>" so guesses at one account from
    # many addresses are limited too)

    def throttled(self, remote: str) -> int:
        """0 if ``remote`` may try to log in, else how many of its attempts
        have been refused in the current window (1 for the first)."""
        entry = self._window(remote)
        if entry.count < LOGIN_FAILURES:
            return 0
        entry.refused += 1
        return entry.refused

    def login_failed(self, remote: str) -> None:
        self._window(remote).count += 1

    def _window(self, remote: str) -> _Failures:
        now = time.monotonic()
        if len(self._failures) > 1000:  # forget finished windows
            self._failures = {k: v for k, v in self._failures.items()
                              if now - v.window_start < LOGIN_WINDOW}
        entry = self._failures.get(remote)
        if not entry or now - entry.window_start >= LOGIN_WINDOW:
            entry = self._failures[remote] = _Failures(now)
        return entry


WORKSPACE = web.AppKey("workspace", Workspace)
JOBS = web.AppKey("jobs", JobManager)
AUTH = web.AppKey("auth", Auth)
AUDIT = web.AppKey("audit", AuditLog)
CAPTURES = web.AppKey("captures", set)
SESSIONS = web.AppKey("sessions", SessionRegistry)
CAPTURE_POOL = web.AppKey("capture_pool", ThreadPoolExecutor)


def create_app(workspace: Workspace, token: Optional[str] = None, *,
               users: Optional[UserStore] = None, audit: Optional[AuditLog] = None,
               public_url: Optional[str] = None, max_sessions: int = MAX_SESSIONS,
               max_user_sessions: int = MAX_USER_SESSIONS, instance: Optional[str] = None,
               session_file: Optional[SessionFile] = None) -> web.Application:
    """The GUI app. Pass ``token`` for single-token mode or ``users`` for
    named users; ``public_url`` is the address browsers use when it differs
    from what the server sees (e.g. behind a TLS-terminating proxy).
    ``max_sessions``/``max_user_sessions`` cap open terminal tabs.
    ``instance`` names the session cookie and ``session_file`` keeps logins
    across restarts (see ``Auth``)."""
    # Topologies with inline startup configs can be large
    app = web.Application(middlewares=[_auth_middleware], client_max_size=16 * 1024 * 1024)
    app[WORKSPACE] = workspace
    app[JOBS] = JobManager(workspace)
    app[AUTH] = Auth(token, users, public_url, instance=instance, session_file=session_file)
    app[AUDIT] = audit_log = audit or AuditLog(None)
    app[JOBS].on_finished = lambda job: _audit_job_finished(audit_log, job)
    app.on_response_prepare.append(_security_headers)
    app[CAPTURES] = set()  # running packet captures, stopped on shutdown
    app[SESSIONS] = sessions = SessionRegistry(max_user_sessions, max_sessions)
    # Captures block a thread each while they run: their own pool, so they
    # can never starve the default one the rest of the GUI uses
    app[CAPTURE_POOL] = ThreadPoolExecutor(max_workers=2 * sessions.limits[CAPTURE][1] + 2,
                                           thread_name_prefix="capture")
    app.cleanup_ctx.append(_watch_sessions)
    app.router.add_get("/", _index)
    app.router.add_post("/login", _login)
    app.router.add_post("/logout", _logout)
    app.router.add_get("/api/me", _me)
    app.router.add_post("/api/password", _change_password)
    app.router.add_get("/api/users", _list_users)
    app.router.add_post("/api/users", _add_user)
    app.router.add_patch("/api/users/{name}", _update_user)
    app.router.add_post("/api/users/{name}/reset", _reset_user)
    app.router.add_delete("/api/users/{name}", _remove_user)
    app.router.add_get("/api/state", _state)
    app.router.add_get("/api/hosts", _hosts)
    app.router.add_get("/api/topologies/{id:.+}", _topology)
    app.router.add_put("/api/topologies/{id:.+}", _save_topology)
    app.router.add_get("/api/live/{id:.+}", _live)
    app.router.add_get("/api/routing/{id:.+}", _routing)
    app.router.add_get("/api/routing-live/{id:.+}", _routing_live)
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
    app.on_cleanup.append(_on_cleanup)
    return app


def run(workspace: Workspace, host: str = "127.0.0.1", port: int = 8650,
        open_browser: bool = True, *, users: Optional[UserStore] = None,
        audit: Optional[AuditLog] = None, ssl_context: Optional[ssl.SSLContext] = None,
        public_url: Optional[str] = None, max_sessions: int = MAX_SESSIONS,
        max_user_sessions: int = MAX_USER_SESSIONS) -> None:
    token = None if users else secrets.token_urlsafe(24)
    # Logins are kept per port: a restart on the same port keeps them
    state_dir = users.path.parent if users else DEFAULT_USERS_FILE.expanduser().parent
    session_file = SessionFile(state_dir / SESSION_FILE.format(instance=port))
    app = create_app(workspace, token, users=users, audit=audit, public_url=public_url,
                     max_sessions=max_sessions, max_user_sessions=max_user_sessions,
                     instance=str(port), session_file=session_file)
    base = public_url.rstrip("/") if public_url else _base_url(host, port, ssl_context is not None)

    async def _announce(_app):
        url, text = startup_message(base, token, users, audit)
        print(text, flush=True)
        if open_browser and not users:  # a named user's token is not ours to use
            asyncio.get_running_loop().run_in_executor(None, webbrowser.open, url)

    async def _sweep(_app):
        # Capture helpers a crashed GUI left behind; in the background, as
        # remote hosts may be slow to answer
        asyncio.get_running_loop().run_in_executor(None, workspace.sweep_capture_helpers)

    app.on_startup.append(_announce)
    app.on_startup.append(_sweep)
    # AccessLogger: request lines without query strings (no tokens in logs)
    web.run_app(app, host=host, port=port, ssl_context=ssl_context, print=None,
                access_log_class=AccessLogger, shutdown_timeout=SHUTDOWN_TIMEOUT)


def startup_message(base: str, token: Optional[str], users: Optional[UserStore],
                    audit: Optional[AuditLog]) -> tuple[str, str]:
    """(URL to open, text to print) when the GUI starts."""
    if users:
        url = f"{base}/"
        lines = [f"clabfleet GUI running at:\n\n    {url}\n",
                 "Users log in with their name and password (or token link).",
                 f"Users file: {users.path}"]
        first = users.get(BOOTSTRAP_USER)
        if first and first.must_change:
            password = users.initial_password()
            lines.append(
                f"\nFirst login: {BOOTSTRAP_USER} / "
                f"{password or '(see ' + str(users.initial_password_file) + ')'}\n"
                f"(also in {users.initial_password_file} until it is changed). "
                "It asks for a new password first.\n")
        if not users.users():
            lines.append("No users yet: add one with `clabfleet user add NAME`.")
    else:
        url = login_link(base, token)
        lines = [f"clabfleet GUI running at:\n\n    {url}\n",
                 "To log in with a name and password instead, add yourself with "
                 "`clabfleet user add NAME` and restart the GUI."]
    if audit and audit.path:
        lines.append(f"Audit log: {audit.path}")
    lines.append("Press Ctrl+C to stop.")
    return url, "\n".join(lines)


class AccessLogger(AbstractAccessLogger):
    """aiohttp's access log without query strings: old ``/?token=`` links
    and websocket parameters stay out of the log."""

    def log(self, request, response, time):
        self.logger.info('%s "%s %s HTTP/%d.%d" %s %s %.3fs', request.remote, request.method,
                         request.rel_url.raw_path, *request.version, response.status,
                         response.body_length, time)


def _base_url(host: str, port: int, tls: bool) -> str:
    if host in LOOPBACK_NAMES:
        name = "localhost"
    elif host in ("0.0.0.0", "::", ""):
        name = socket.gethostname()
    else:
        name = f"[{host}]" if ":" in host else host
    return f"{'https' if tls else 'http'}://{name}:{port}"


async def _on_shutdown(app):
    # End terminals and captures (their handlers clean up behind them), and
    # kill tcpdump in the containers before the runners go away
    await asyncio.gather(*(_end_session(s, WSCloseCode.GOING_AWAY, "the GUI is stopping")
                           for s in app[SESSIONS]), return_exceptions=True)
    loop = asyncio.get_running_loop()
    captures = list(app[CAPTURES])
    if captures:
        await asyncio.gather(*(loop.run_in_executor(app[CAPTURE_POOL], c.stop)
                               for c in captures))
    app[WORKSPACE].close()


async def _on_cleanup(app):
    app[CAPTURE_POOL].shutdown(wait=False, cancel_futures=True)


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
    if request.path.startswith("/static/") or request.path in PUBLIC_PATHS:
        return await handler(request)

    auth: Auth = request.app[AUTH]
    user = auth.identify(request)
    if not user:
        # Tells the login form what to ask for: a name and password (named
        # users, who may also have a token) or the start-up token
        mode = "password" if auth.multi_user else "token"
        raise web.HTTPUnauthorized(text="not logged in", headers={"X-Clabfleet-Login": mode})
    request[USER_KEY] = user
    if user.must_change and request.path not in MUST_CHANGE_PATHS:
        raise web.HTTPForbidden(text="Set a new password first")

    if request.method != "GET" or _is_websocket(request):
        if not _same_origin(request):
            raise web.HTTPForbidden(text="cross-origin request refused")
    if not _allowed(request, user):
        audit(request, "denied", method=request.method, path=request.path)
        raise web.HTTPForbidden(text=f"Not allowed for the {user.role} role")
    return await handler(request)


async def _security_headers(request, response):
    headers = response.headers
    headers.setdefault("Content-Security-Policy", CSP)
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "no-referrer")
    if _cookie_secure(request):
        headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if not request.path.startswith("/static/"):
        headers.setdefault("Cache-Control", "no-store")
    else:
        # Revalidate (a cheap 304 via the ETag) so an upgraded GUI never runs
        # a cached app.js against the new page
        headers.setdefault("Cache-Control", "no-cache")


async def _login(request):
    """Swap a password or a token for a session cookie:
    ``{"username": ..., "password": ...}`` or ``{"token": ...}``, plus
    ``"switch": bool``.

    Only from the GUI's own origin and as JSON, which a form on another
    site cannot send. A session of a different user is only replaced with
    ``switch``, so a stranger's login link cannot quietly swap accounts.
    Failures are limited per address and, for passwords, per user name."""
    auth: Auth = request.app[AUTH]
    remote = request.remote or ""
    if not _same_origin(request):
        raise web.HTTPForbidden(text="cross-origin request refused")
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text="expected JSON")
    body = await _json_body(request)
    username = body.get("username")
    by_password = "username" in body or "password" in body
    keys = [remote] + ([f"user:{username}"] if by_password and isinstance(username, str) else [])
    for key in keys:
        refused = auth.throttled(key)
        if refused:
            if refused == 1:  # one audit line per address (or name) and window
                details = {"username": username} if key != remote else {}
                request.app[AUDIT].record("login_throttled", None, remote, **details,
                                          failures=LOGIN_FAILURES, seconds=LOGIN_WINDOW)
            raise web.HTTPTooManyRequests(text="too many failed logins; try again in a minute",
                                          headers={"Retry-After": str(LOGIN_WINDOW)})
    if by_password:
        user = await asyncio.get_running_loop().run_in_executor(
            None, auth.check_password, username, body.get("password"))
    else:
        user = auth.check_token(body.get("token"))
    if not user:
        for key in keys:
            auth.login_failed(key)
        what = "user name or password" if by_password else "token"
        details = {"username": username} if by_password and isinstance(username, str) else {}
        request.app[AUDIT].record("login_failed", None, remote, reason=f"invalid {what}",
                                  **details)
        raise web.HTTPUnauthorized(text=f"invalid {what}")

    current = auth.identify(request)
    if current and current.name != user.name and body.get("switch") is not True:
        return web.json_response({"error": "logged in as another user",
                                  "user": current.name}, status=409)
    auth.logout(request)  # a login always starts a fresh session
    secure = _cookie_secure(request)
    resp = web.json_response({"user": user.name or None, "role": user.role})
    # Max-Age: the login outlives the browser window, up to the session's end
    resp.set_cookie(auth.cookie_name(secure), auth.start_session(user), path="/",
                    max_age=SESSION_MAX_AGE, httponly=True, samesite="Strict",
                    secure=secure or None)
    request.app[AUDIT].record("login", user, remote,
                              method="password" if by_password else "token")
    return resp


@allow_viewer
async def _logout(request):
    auth: Auth = request.app[AUTH]
    auth.logout(request)
    audit(request, "logout")
    resp = web.json_response({"ok": True})
    secure = _cookie_secure(request)
    resp.del_cookie(auth.cookie_name(secure), path="/", secure=secure or None,
                    httponly=True, samesite="Strict")
    return resp


async def _me(request):
    user = current_user(request)
    return web.json_response({"user": user.name or None, "role": user.role,
                              "multi_user": request.app[AUTH].multi_user,
                              "has_password": bool(user.password),
                              "must_change": user.must_change,
                              "can_manage_users": request.app[AUTH].multi_user
                              and user.is_operator})


# --- user management (operators; named-user mode only) ---

def _user_view(user: User) -> dict:
    return {"name": user.name, "role": user.role, "password": bool(user.password),
            "token": bool(user.token_sha256), "must_change": user.must_change,
            "created": user.created}


def _user_store(request) -> UserStore:
    auth: Auth = request.app[AUTH]
    if not auth.multi_user:
        raise web.HTTPNotFound(text="There are no users with --single-token")
    return auth.users


async def _in_thread(fn, *args, **kwargs):
    """Run a users-file change off the event loop (password hashing is slow)."""
    try:
        return await asyncio.get_running_loop().run_in_executor(
            None, functools.partial(fn, *args, **kwargs))
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc.args[0]) if exc.args else "No such user")
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))


def _new_login(request, store: UserStore, name: str, login: str, fn) -> dict:
    """Give ``name`` a temporary password or a token with ``fn``; the secret
    to hand over, shown once."""
    if login == "password":
        password = temporary_password()
        fn(password)
        return {"password": password}
    token = fn(None)
    auth: Auth = request.app[AUTH]
    base = (auth.public_url or f"{request.scheme}://{request.host}").rstrip("/")
    return {"token": token, "link": login_link(base, token)}


@operator_only
async def _list_users(request):
    users = _user_store(request).users()
    return web.json_response({"users": [_user_view(u) for _, u in sorted(users.items())],
                              "you": current_user(request).name})


async def _add_user(request):
    """``{"name", "role", "login": "password"|"token"}``: a temporary
    password the user replaces at the first login, or a login token."""
    store = _user_store(request)
    body = await _json_body(request)
    name, role, login = body.get("name"), body.get("role", OPERATOR), body.get("login", "password")
    if not isinstance(name, str) or login not in ("password", "token"):
        raise web.HTTPBadRequest(text="Expected a name and a login of password or token")

    def add(password):
        if password:
            return store.add(name, role, password, must_change=True)
        return store.add(name, role)

    secret = await _in_thread(_new_login, request, store, name, login, add)
    audit(request, "user_added", name=name, role=role, login=login)
    return web.json_response({"user": _user_view(store.get(name)), **secret})


async def _update_user(request):
    """``{"role": ...}`` for another user."""
    store, name = _user_store(request), request.match_info["name"]
    if name == current_user(request).name:
        raise web.HTTPBadRequest(text="You cannot change your own role")
    role = (await _json_body(request)).get("role")
    await _in_thread(store.set_role, name, role)
    audit(request, "user_role_changed", name=name, role=role)
    return web.json_response({"user": _user_view(store.get(name))})


async def _reset_user(request):
    """``{"login": "password"|"token"}``: a new temporary password or a new
    token for another user; their sessions end."""
    store, name = _user_store(request), request.match_info["name"]
    if name == current_user(request).name:
        raise web.HTTPBadRequest(text="Use Password in the top bar to change your own")
    login = (await _json_body(request)).get("login", "password")
    if login not in ("password", "token"):
        raise web.HTTPBadRequest(text="login must be password or token")
    if not store.get(name):
        raise web.HTTPNotFound(text=f"No user '{name}'")

    def reset(password):
        return (store.set_password(name, password, must_change=True) if password
                else store.rotate(name))

    secret = await _in_thread(_new_login, request, store, name, login, reset)
    audit(request, "user_login_reset", name=name, login=login)
    return web.json_response({"user": _user_view(store.get(name)), **secret})


async def _remove_user(request):
    store, name = _user_store(request), request.match_info["name"]
    if name == current_user(request).name:
        raise web.HTTPBadRequest(text="You cannot remove yourself")
    await _in_thread(store.remove, name, keep_operator=True)
    audit(request, "user_removed", name=name)
    return web.json_response({"removed": name})


@allow_viewer
async def _change_password(request):
    """Set your own password: ``{"current": ..., "new": ...}``. Your other
    sessions end; this one goes on with a new cookie. Wrong current
    passwords count as failed logins (per address and per user name)."""
    auth: Auth = request.app[AUTH]
    user = current_user(request)
    if not auth.multi_user or not user.password:
        raise web.HTTPBadRequest(text="This login has no password to change")
    body = await _json_body(request)
    current, new = body.get("current"), body.get("new")
    if not isinstance(current, str) or not isinstance(new, str):
        raise web.HTTPBadRequest(text="Expected the current and the new password")
    keys = [request.remote or "", f"user:{user.name}"]
    if any(auth.throttled(k) for k in keys):
        raise web.HTTPTooManyRequests(text="too many failed logins; try again in a minute",
                                      headers={"Retry-After": str(LOGIN_WINDOW)})
    loop = asyncio.get_running_loop()
    if not await loop.run_in_executor(None, auth.check_password, user.name, current):
        for key in keys:
            auth.login_failed(key)
        audit(request, "password_change_failed", reason="wrong current password")
        raise web.HTTPForbidden(text="The current password is wrong")
    if new == current:
        raise web.HTTPBadRequest(text="Choose a password different from the current one")
    try:
        check_new_password(new)
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    await loop.run_in_executor(None, auth.users.set_password, user.name, new)
    audit(request, "password_changed")
    auth.logout(request)
    user = auth.users.get(user.name)
    secure = _cookie_secure(request)
    resp = web.json_response({"user": user.name, "role": user.role})
    resp.set_cookie(auth.cookie_name(secure), auth.start_session(user), path="/",
                    max_age=SESSION_MAX_AGE, httponly=True, samesite="Strict",
                    secure=secure or None)
    return resp


def _audit_job_finished(log: AuditLog, job: Job) -> None:
    log.record("job_finished", job.user, None, job=job.id, action=job.action,
               topology=job.topology, lab=job.lab, status=job.status,
               seconds=round((job.finished or time.time()) - job.started, 1))


# ----------------------------------------------------------------------
# Pages & API
# ----------------------------------------------------------------------

async def _index(request):
    """The page, for anyone: without a session it shows the login form."""
    token = request.query.get("token")
    if token is not None:
        # Deprecated ?token= link: move the token into the fragment, where
        # the page picks it up and posts it to /login
        raise web.HTTPFound(f"/#token={quote(token, safe='')}")
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


async def _routing(request):
    """Intended OSPF / BGP / EVPN views, read from the topology's configs."""
    ws: Workspace = request.app[WORKSPACE]
    try:
        view = await asyncio.to_thread(ws.routing, request.match_info["id"])
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc))
    return web.json_response(view)


async def _routing_live(request):
    """Live OSPF / BGP / EVPN state of a topology's running lab (cached;
    asking refreshes it in the background)."""
    ws: Workspace = request.app[WORKSPACE]
    try:
        live = await asyncio.to_thread(ws.routing_live, request.match_info["id"])
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


@operator_only  # configs hold password hashes and keys
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
    try:
        offset = max(0, int(request.query.get("offset", "0")))
    except ValueError:
        raise web.HTTPBadRequest(text="offset must be a whole number")
    return web.json_response(job.view(offset))


# ----------------------------------------------------------------------
# Long-lived sessions: terminals, live captures, pcap downloads
# ----------------------------------------------------------------------

def _open_session(request, kind: str, ws_resp=None) -> OpenSession:
    """Count a new session against the limits (SessionLimitError if over)."""
    end = functools.partial(_close_ws, ws_resp) if ws_resp is not None else None
    return request.app[SESSIONS].open(kind, current_user(request), request, end)


def _session_ok(entry: OpenSession) -> bool:
    """Whether the login a session was opened with still holds: the user
    exists with the same token, has not logged out, and is still an
    operator if it was one."""
    user = entry.request.app[AUTH].identify(entry.request, touch=False)
    return user is not None and (user.is_operator or not entry.user.is_operator)


async def _end_session(entry: OpenSession, code: int, reason: str) -> None:
    if entry.ending or entry.end is None:
        return
    entry.ending = True
    await entry.end(code, reason)


async def _close_ws(ws_resp, code: int, reason: str) -> None:
    """Close a websocket, dropping the connection if the browser does not answer."""
    if ws_resp.closed:
        return
    try:
        await asyncio.wait_for(_close_politely(ws_resp, code, reason), CLOSE_TIMEOUT)
    except (asyncio.TimeoutError, ConnectionError, RuntimeError):
        pass  # aiohttp drops the connection when a close times out


async def _close_politely(ws_resp, code: int, reason: str) -> None:
    await ws_resp.send_bytes(f"\r\n\x1b[31m[{reason}]\x1b[0m\r\n".encode())
    await ws_resp.close(code=code, message=reason.encode())


async def _revalidate_sessions(app) -> None:
    ended = []
    for entry in app[SESSIONS]:
        if not entry.ending and entry.end is not None and not _session_ok(entry):
            app[AUDIT].record("session_revoked", entry.user, entry.request.remote,
                              kind=entry.kind, path=entry.request.path)
            ended.append(_end_session(entry, WSCloseCode.POLICY_VIOLATION,
                                      "login no longer valid"))
    if ended:
        await asyncio.gather(*ended, return_exceptions=True)


async def _watch_sessions(app):
    """Background task: end sessions whose login was removed, rotated or
    logged out (incoming messages are checked as they arrive, too)."""
    async def watch():
        while True:
            await asyncio.sleep(REVALIDATE_INTERVAL)
            try:
                await _revalidate_sessions(app)
            except Exception:  # noqa: BLE001 - keep watching
                logger.exception("Session check failed")

    task = asyncio.create_task(watch())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _refuse(ws_resp, message: str):
    """Show an error in the tab and end it."""
    await ws_resp.send_bytes(f"\r\n\x1b[31m{message}\x1b[0m\r\n".encode())
    await ws_resp.send_str(json.dumps({"t": "exit", "code": -1}))
    await ws_resp.close()
    return ws_resp


def _clamp_size(cols, rows) -> tuple[int, int]:
    """Terminal size within sane bounds; TypeError/ValueError/OverflowError
    for values that are not numbers."""
    return max(20, min(int(cols), 500)), max(5, min(int(rows), 200))


def _parse_message(raw: str) -> Optional[dict]:
    """A websocket message from the browser: {"t": "i", "d": text} (input)
    or {"t": "r", "c": cols, "r": rows} (resize). None for anything else."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("t") == "i" and isinstance(data.get("d"), str):
        return {"t": "i", "d": data["d"]}
    if data.get("t") == "r":
        try:
            cols, rows = _clamp_size(data.get("c"), data.get("r"))
        except (TypeError, ValueError, OverflowError):
            return None
        return {"t": "r", "c": cols, "r": rows}
    return None


async def _serve_session(request, ws_resp, session, entry: OpenSession,
                         read_only: bool = False) -> None:
    """Shuttle a started session's output and the browser's input until either ends."""
    async def pump_output():
        async for chunk in session.output():
            if ws_resp.closed:
                return
            try:
                # aiohttp waits here while the browser is not reading; the
                # session stops reading its command meanwhile (backpressure)
                await asyncio.wait_for(ws_resp.send_bytes(chunk), SEND_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("Dropping a %s session of %s: the browser has not read "
                               "its output for %ss", entry.kind, entry.user.name or "operator",
                               SEND_TIMEOUT)
                if request.transport is not None:
                    request.transport.abort()
                return
            except ConnectionError:
                return
        if not ws_resp.closed:
            await ws_resp.send_str(json.dumps({"t": "exit", "code": session.exit_code}))
            await ws_resp.close()

    pump = asyncio.create_task(pump_output())
    try:
        async for msg in ws_resp:
            if msg.type != WSMsgType.TEXT:
                continue
            if not _session_ok(entry):
                await _end_session(entry, WSCloseCode.POLICY_VIOLATION,
                                   "login no longer valid")
                break
            data = _parse_message(msg.data)
            if data is None:
                continue
            if data["t"] == "i":
                if read_only:  # viewers' logs take no input
                    continue
                if not session.write(data["d"].encode(errors="replace")):
                    await ws_resp.send_bytes(
                        b"\r\n\x1b[31m[input dropped: the node is not reading it]\x1b[0m\r\n")
            else:
                session.resize(data["c"], data["r"])
    finally:
        session.close()
        pump.cancel()


def _stop_terminal(runner, host_sudo: bool, container: str, tag: str) -> None:
    """End what a closed CLI/shell tab left running in the container."""
    res = run_docker(runner, terminal_stop_command(container, tag), host_sudo)
    if res.exit_code != 0:
        logger.warning("Could not stop terminal processes in %s: %s", container,
                       (res.stderr or res.stdout).strip()[-300:])


@allow_viewer  # but only for the read-only Logs mode, checked below
async def _terminal(request):
    q = request.query
    mode = q.get("mode", "")
    read_only = not current_user(request).is_operator
    if read_only and mode != "logs":
        audit(request, "denied", method=request.method, path=request.path, mode=mode)
        raise web.HTTPForbidden(text="Viewers can only follow node logs")
    try:
        cols, rows = _clamp_size(q.get("cols", "120"), q.get("rows", "30"))
    except (TypeError, ValueError, OverflowError):
        raise web.HTTPBadRequest(text="cols and rows must be whole numbers")

    ws_resp = web.WebSocketResponse(heartbeat=30)
    await ws_resp.prepare(request)
    try:
        entry = _open_session(request, TERMINAL, ws_resp)
    except SessionLimitError as exc:
        audit(request, "denied", method=request.method, path=request.path, mode=mode,
              reason=str(exc))
        return await _refuse(ws_resp, str(exc))
    try:
        return await _run_terminal(request, ws_resp, entry, mode, cols, rows, read_only)
    finally:
        request.app[SESSIONS].close(entry)


async def _run_terminal(request, ws_resp, entry, mode, cols, rows, read_only):
    q = request.query
    workspace: Workspace = request.app[WORKSPACE]
    try:
        node = await asyncio.to_thread(workspace.find_node, q.get("lab", ""), q.get("node", ""))
        host = workspace.host(node["host"])
        runner = await asyncio.to_thread(workspace.runner, host)
        # CLI and shell tabs tag their processes so closing the tab can end
        # them inside the container (the docker client going away does not)
        tag = secrets.token_hex(8) if mode in ("cli", "shell") else ""
        argv = terminal_command(mode, node["kind"], node["container"], node["ipv4"], tag)
        cleanup = (functools.partial(_stop_terminal, runner, host.sudo, node["container"], tag)
                   if tag else None)
        if host.is_local:
            session = LocalTerminal(argv, cols, rows, cleanup, tty=mode != "logs")
        else:
            client = await asyncio.to_thread(runner.client)
            session = SSHTerminal(client, argv, cols, rows, cleanup)
        await session.start(asyncio.get_running_loop())
    except KeyError as exc:
        return await _refuse(ws_resp, exc.args[0])
    except Exception as exc:
        return await _refuse(ws_resp, str(exc))

    where = {"lab": q.get("lab", ""), "node": q.get("node", ""), "mode": mode, "host": node["host"]}
    audit(request, "terminal_opened", **where)
    opened = time.monotonic()
    try:
        await _serve_session(request, ws_resp, session, entry, read_only)
    finally:
        session.close()
        await session.wait_closed()
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
    try:
        entry = _open_session(request, CAPTURE, ws_resp)
    except SessionLimitError as exc:
        audit(request, "denied", method=request.method, path=request.path, reason=str(exc))
        return await _refuse(ws_resp, str(exc))
    try:
        return await _run_capture_live(request, ws_resp, entry)
    finally:
        request.app[SESSIONS].close(entry)


async def _run_capture_live(request, ws_resp, entry):
    q = request.query
    loop = asyncio.get_running_loop()
    pool = request.app[CAPTURE_POOL]
    session = CaptureSession(loop, executor=pool)

    try:
        spec = spec_from_query(q, "text")
        capture, _ = await loop.run_in_executor(pool, open_capture, request.app[WORKSPACE],
                                                q.get("topo", ""), spec, session.message)
    except Exception as exc:
        known = isinstance(exc, (KeyError, ValueError, CaptureError))
        return await _refuse(ws_resp, _capture_error(exc).text if known else str(exc))

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
        await session.start(loop)
        await _serve_session(request, ws_resp, session, entry)
    finally:
        session.close()
        try:
            await session.wait_closed()
        finally:
            captures.discard(capture)
            audit(request, "capture_finished", **where,
                  seconds=round(time.monotonic() - started, 1))
    return ws_resp


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
    try:
        entry = _open_session(request, CAPTURE)
    except SessionLimitError as exc:
        audit(request, "denied", method=request.method, path=request.path, reason=str(exc))
        raise web.HTTPTooManyRequests(text=str(exc))
    try:
        return await _run_capture_download(request, entry)
    finally:
        request.app[SESSIONS].close(entry)


async def _run_capture_download(request, entry):
    q = request.query
    loop = asyncio.get_running_loop()
    pool = request.app[CAPTURE_POOL]
    messages: list[str] = []
    try:
        spec = spec_from_query(q, "pcap")
        capture, lab = await loop.run_in_executor(pool, open_capture, request.app[WORKSPACE],
                                                  q.get("topo", ""), spec, messages.append)
    except (KeyError, ValueError, CaptureError) as exc:
        raise _capture_error(exc)

    async def end(code, reason):
        await loop.run_in_executor(pool, capture.stop, reason)
    entry.end = end

    captures = request.app[CAPTURES]
    captures.add(capture)
    where = _capture_where(q, spec, "pcap")
    audit(request, "capture_started", **where)
    started = time.monotonic()
    sent = 0
    try:
        read = loop.run_in_executor(pool, capture.read)
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
                await asyncio.wait_for(resp.write(chunk), SEND_TIMEOUT)
            except (ConnectionError, asyncio.TimeoutError):
                break
            sent += len(chunk)
            if sent >= GUI_MAX_BYTES:
                break
            read = loop.run_in_executor(pool, capture.read)
        stop = loop.run_in_executor(pool, capture.stop)  # before the browser sees the end
        await asyncio.shield(stop)
        if not _client_gone(request):
            await resp.write_eof()
        return resp
    finally:
        # Shielded: a cancelled handler must still kill tcpdump (stop() is idempotent)
        try:
            await asyncio.shield(loop.run_in_executor(pool, capture.stop))
        finally:
            captures.discard(capture)
            audit(request, "capture_finished", **where,
                  seconds=round(time.monotonic() - started, 1), bytes=sent)
