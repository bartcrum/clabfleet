"""Named GUI users, roles and the audit log.

Users live in a YAML file (``~/.clabfleet/users.yaml`` by default, mode
0600) managed with ``clabfleet user``. A users file that another user could
change (group/world-writable, or owned by someone else, or in such a
directory) is refused: whoever can write it can make themselves an operator. Each user has a role and the SHA-256
of a random token; the token itself is shown once and never stored. A
plain hash is enough because tokens are 256-bit random strings, not
passwords.

Roles:

- ``operator``: everything (jobs, terminals, edits)
- ``viewer``: read-only (state, diagrams, YAML, job output, node logs)

The server decides access per route (see ``allow_viewer`` and
``operator_only``): anything that is not a plain read needs an operator
unless the handler says otherwise, so new endpoints are protected by
default.

Nothing here imports aiohttp, so the CLI can manage users without it.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import stat
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

OPERATOR = "operator"
VIEWER = "viewer"
ROLES = (OPERATOR, VIEWER)

DEFAULT_USERS_FILE = Path("~/.clabfleet/users.yaml")
AUDIT_FILE_NAME = "audit.jsonl"  # default: next to the users file

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")

# Route access markers, set on handler functions
ACCESS_ATTR = "_clabfleet_access"


def allow_viewer(handler):
    """Let viewers call this handler even though it is not a plain GET
    (the handler must enforce anything finer itself)."""
    setattr(handler, ACCESS_ATTR, VIEWER)
    return handler


def operator_only(handler):
    """Keep viewers out of a GET handler (e.g. a download of node data)."""
    setattr(handler, ACCESS_ATTR, OPERATOR)
    return handler


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def login_link(base: str, token: str) -> str:
    """A login link. The token goes in the fragment, which browsers do not
    send to the server, so it stays out of access logs and proxies; the
    page posts it to /login and drops it from the address bar."""
    return f"{base.rstrip('/')}/#token={token}"


@dataclass
class User:
    name: str  # "" for the single-token mode's anonymous operator
    role: str
    token_sha256: str = ""
    created: str = ""

    @property
    def is_operator(self) -> bool:
        return self.role == OPERATOR

    def to_dict(self) -> dict:
        return {"role": self.role, "token_sha256": self.token_sha256, "created": self.created}


class UserStore:
    """The users file. Re-read when it changes on disk, so users added,
    removed or rotated with the CLI take effect without a GUI restart."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self._users: dict[str, User] = {}
        self._stamp: Optional[tuple] = None
        self._lock = threading.Lock()

    # --- reading ---

    def users(self) -> dict[str, User]:
        with self._lock:
            try:
                st = self.path.stat()
            except FileNotFoundError:
                self._users, self._stamp = {}, None
                return {}
            stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
            if stamp != self._stamp:
                try:
                    self._users = self._read()
                except (OSError, ValueError, yaml.YAMLError) as exc:
                    # Fail closed: a broken file lets nobody in
                    logger.error("Cannot read users file %s: %s", self.path, exc)
                    self._users = {}
                self._stamp = stamp
            return dict(self._users)

    def _read(self) -> dict[str, User]:
        self._check_ownership()
        if self.path.stat().st_mode & 0o077:
            logger.warning("Users file %s is readable by other users; chmod 600 it", self.path)
        data = yaml.safe_load(self.path.read_text()) or {}
        if not isinstance(data, dict) or not isinstance(data.get("users") or {}, dict):
            raise ValueError("expected a 'users:' mapping")
        users = {}
        for name, entry in (data.get("users") or {}).items():
            if not isinstance(entry, dict):
                raise ValueError(f"user '{name}': expected a mapping")
            role = entry.get("role")
            if role not in ROLES:
                raise ValueError(f"user '{name}': role must be one of {', '.join(ROLES)}")
            digest = str(entry.get("token_sha256", ""))
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"user '{name}': token_sha256 is not a SHA-256 hex digest")
            users[str(name)] = User(str(name), role, digest, str(entry.get("created", "")))
        return users

    def _check_ownership(self) -> None:
        """Refuse a users file (or its directory) other users could change."""
        check_private(self.path, "Users file")

    def validate(self) -> dict[str, User]:
        """The users, raising if the file is unreadable (for start-up checks)."""
        if not self.path.is_file():
            raise FileNotFoundError(f"Users file not found: {self.path}")
        return self._read()

    def get(self, name: str) -> Optional[User]:
        return self.users().get(name)

    def authenticate(self, token: Optional[str]) -> Optional[User]:
        if not token:
            return None
        digest = hash_token(token)
        found = None
        for user in self.users().values():  # no early exit: constant work per user
            if hmac.compare_digest(digest, user.token_sha256):
                found = user
        return found

    # --- changes (CLI) ---

    def add(self, name: str, role: str = OPERATOR) -> str:
        """Create a user; returns its token (shown once, not stored)."""
        if not NAME_RE.match(name):
            raise ValueError("User names are 1-32 letters, digits, '.', '_' or '-'")
        if role not in ROLES:
            raise ValueError(f"Role must be one of {', '.join(ROLES)}")
        users = self._load_for_update()
        if name in users:
            raise ValueError(f"User '{name}' already exists (use 'clabfleet user rotate')")
        token = new_token()
        users[name] = User(name, role, hash_token(token), now_iso())
        self._write(users)
        return token

    def rotate(self, name: str) -> str:
        """Give a user a new token; the old one (and its sessions) stop working."""
        users = self._load_for_update()
        if name not in users:
            raise KeyError(f"No user '{name}'")
        token = new_token()
        users[name].token_sha256 = hash_token(token)
        self._write(users)
        return token

    def remove(self, name: str) -> None:
        users = self._load_for_update()
        if name not in users:
            raise KeyError(f"No user '{name}'")
        del users[name]
        self._write(users)

    def _load_for_update(self) -> dict[str, User]:
        return self._read() if self.path.exists() else {}

    def _write(self, users: dict[str, User]) -> None:
        text = ("# clabfleet GUI users; manage with `clabfleet user`.\n"
                "# Only SHA-256 hashes of the tokens are stored.\n")
        text += yaml.safe_dump({"users": {n: u.to_dict() for n, u in sorted(users.items())}},
                               sort_keys=False)
        write_private(self.path, text)


def check_private(path: Path, what: str) -> None:
    """Refuse a file (or its directory) that other users could change."""
    for p, desc in ((path, what), (path.parent, f"Directory of {what[0].lower()}{what[1:]}")):
        st = p.stat()
        if st.st_uid not in (os.getuid(), 0):
            raise ValueError(f"{desc} {p} is not owned by you; refusing to use it")
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError(f"{desc} {p} is writable by other users; refusing to use it "
                             "(chmod go-w it)")


def write_private(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` as a new 0600 file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # mkstemp: a fresh 0600 file, so nothing planted in the directory
    # can redirect or pre-open the write
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class SessionFile:
    """GUI login sessions kept on disk, so a restart does not log anyone out.

    Holds the SHA-256 of each session id, never the id itself: reading the
    file does not let anyone use a session. Each entry also has the user
    name, the hash of the token the session came from (rotating the token
    ends it) and its creation and last-use times (Unix seconds). A file
    other users could change is not used, as for the users file.
    """

    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self.usable = True

    def load(self) -> dict[str, dict]:
        """Session hash -> {name, token_sha256, created, last_seen}."""
        try:
            check_private(self.path, "Sessions file")
            data = json.loads(self.path.read_text())
            sessions = data.get("sessions") if isinstance(data, dict) else None
            if not isinstance(sessions, dict):
                raise ValueError("expected a 'sessions' mapping")
            out = {}
            for key, s in sessions.items():
                if re.fullmatch(r"[0-9a-f]{64}", str(key)) and isinstance(s, dict):
                    out[key] = {"name": str(s.get("name", "")),
                                "token_sha256": str(s.get("token_sha256", "")),
                                "created": float(s.get("created", 0)),
                                "last_seen": float(s.get("last_seen", 0))}
            return out
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError) as exc:
            # Fail closed: nobody is logged in, and the file is left alone
            logger.error("Not using sessions file %s: %s", self.path, exc)
            self.usable = False
            return {}

    def save(self, sessions: dict[str, dict]) -> None:
        if not self.usable:
            return
        try:
            write_private(self.path, json.dumps({"version": 1, "sessions": sessions}, indent=1))
        except OSError as exc:
            logger.warning("Could not write sessions file %s: %s", self.path, exc)


class AuditLog:
    """Append-only JSON Lines log of who did what. Without a path it is off.

    Each line: ``{"ts", "user", "role", "remote", "event", "details"}``.
    """

    def __init__(self, path: Optional[Path]):
        self.path = Path(path).expanduser() if path else None
        self._lock = threading.Lock()

    def record(self, event: str, user: "User | str | None" = None,
               remote: Optional[str] = None, **details) -> None:
        """Append one event; ``user`` is a User or just a name."""
        if not self.path:
            return
        if isinstance(user, User):
            name, role = user.name or None, user.role
        else:
            name, role = user or None, None
        line = json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "user": name,
            "role": role,
            "remote": remote,
            "event": event,
            "details": details,
        }, default=str)
        with self._lock:
            try:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if self.path.is_symlink():
                    raise OSError(f"{self.path} is a symlink; not following it")
                fd = os.open(self.path,
                             os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "a") as fh:
                    fh.write(line + "\n")
            except OSError as exc:
                logger.warning("Could not write audit log %s: %s", self.path, exc)
