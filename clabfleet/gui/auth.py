"""Named GUI users, roles and the audit log.

Users live in a YAML file (``~/.clabfleet/users.yaml`` by default, mode
0600) managed with ``clabfleet user``. Each user has a role and the SHA-256
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
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        text = ("# clabfleet GUI users; manage with `clabfleet user`.\n"
                "# Only SHA-256 hashes of the tokens are stored.\n")
        text += yaml.safe_dump({"users": {n: u.to_dict() for n, u in sorted(users.items())}},
                               sort_keys=False)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)  # in case it existed with other permissions
        os.replace(tmp, self.path)


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
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a") as fh:
                    fh.write(line + "\n")
            except OSError as exc:
                logger.warning("Could not write audit log %s: %s", self.path, exc)
