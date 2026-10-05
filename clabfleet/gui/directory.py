"""Directory logins: OAuth 2.0 / OpenID Connect and LDAP / Active Directory.

Configured in a YAML file (``~/.clabfleet/directory.yaml`` by default, or
``clabfleet gui --directory FILE``) with an ``oidc:`` section, an ``ldap:``
section or both. It holds a client secret or a bind password, so like the
users file it is refused when other users could change it.

A directory user is not in the users file. Their role comes from their
groups: each section's ``roles:`` lists the groups that make someone an
operator or a viewer, and someone in none of them is refused. The local
users stay as they are, next to the directory, so the first admin still
gets in when the directory is down. A directory user whose name is also
a local user's is refused: the local user has that name.

What keeps a directory login valid afterwards:

- LDAP: the user is looked up again every ``recheck`` seconds with the
  bind account, so someone disabled or moved out of a group loses the
  role within minutes. A directory that does not answer for longer than
  ``GRACE`` ends the session.
- OIDC: the provider is not asked again, so the session ends after
  ``session_hours`` and the next login goes through the provider.

A changed configuration ends the directory sessions made with the old one
(``fingerprint``).

Nothing here imports aiohttp; the OIDC flow itself is in ``oidc.py``.
"""

import hashlib
import json
import logging
import os
import re
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

import yaml

from .auth import OPERATOR, ROLES, VIEWER, check_private

logger = logging.getLogger(__name__)

OIDC = "oidc"
LDAP = "ldap"
SOURCES = (OIDC, LDAP)

DEFAULT_DIRECTORY_FILE = Path("~/.clabfleet/directory.yaml")

# Directory names are not ours to choose (an email address, a UPN): any
# printable text of a sensible length
NAME_RE = re.compile(r"[^\x00-\x1f\x7f]{1,128}")
LOOPBACK_NAMES = ("127.0.0.1", "::1", "localhost")

GRACE = 3600  # seconds an LDAP login holds past its re-check while the directory is down
RETRY = 60    # seconds before a failed re-check is tried again

# Active Directory: the account by its logon name, unless it is disabled
AD_USER_FILTER = ("(&(objectCategory=person)(objectClass=user)(sAMAccountName={username})"
                  "(!(userAccountControl:1.2.840.113556.1.4.803:=2)))")
# Active Directory: every group the entry is in, through nested groups too
AD_IN_CHAIN = "(member:1.2.840.113556.1.4.1941:={dn})"

OIDC_KEYS = {"issuer", "client_id", "client_secret", "client_secret_env", "scopes",
             "username_claim", "groups_claim", "label", "roles", "session_hours", "ca_file"}
LDAP_KEYS = {"url", "start_tls", "ca_file", "bind_dn", "bind_password", "bind_password_env",
             "user_base", "user_filter", "username_attribute", "group_attribute",
             "nested_groups", "group_base", "label", "roles", "timeout", "recheck"}
# Not part of who gets in: changing them leaves sessions alone
UNFINGERPRINTED = {"client_secret", "client_secret_env", "bind_password", "bind_password_env",
                   "label", "timeout", "ca_file"}


class DirectoryError(Exception):
    """The directory could not be asked: unreachable, or set up wrongly."""


class LoginRefused(Exception):
    """The directory answered and this login is not let in.

    ``authenticated``: the person proved who they are and is refused for
    another reason (no group with a role), which they may be told."""

    def __init__(self, message: str, authenticated: bool = False):
        super().__init__(message)
        self.authenticated = authenticated


@dataclass(frozen=True)
class DirectoryUser:
    name: str
    role: Optional[str]      # None: in no group that has a role
    groups: tuple = ()       # the groups that gave the role
    subject: str = ""        # the directory's own id: an LDAP DN, an OIDC ``sub``


@dataclass(frozen=True)
class RoleMap:
    """Groups to roles. ``fold``: compare without case (LDAP DNs)."""
    operator: frozenset
    viewer: frozenset
    fold: bool = False

    def resolve(self, groups) -> tuple[Optional[str], tuple]:
        """(role, the groups that gave it); operator wins over viewer."""
        mine = {g.casefold() if self.fold else g: g for g in groups}
        for role, wanted in ((OPERATOR, self.operator), (VIEWER, self.viewer)):
            hits = sorted(mine[g] for g in wanted & mine.keys())
            if hits:
                return role, tuple(hits)
        return None, ()


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    client_id: str
    client_secret: str     # "" for a public client (PKCE only)
    scopes: tuple
    username_claim: str
    groups_claim: str
    label: str             # the login button says "Log in with <label>"
    roles: RoleMap
    session_hours: float
    ca_file: str
    fingerprint: str


@dataclass(frozen=True)
class LdapConfig:
    url: str
    start_tls: bool
    ca_file: str
    bind_dn: str           # "" to search anonymously
    bind_password: str
    user_base: str
    user_filter: str
    username_attribute: str
    group_attribute: str
    nested_groups: bool
    group_base: str
    label: str             # the login form says "your <label> name and password"
    roles: RoleMap
    timeout: float
    recheck: float
    fingerprint: str


def check_name(name) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name.strip()):
        raise LoginRefused("the directory gave no usable user name", authenticated=True)
    return name.strip()


def is_loopback_url(url: str) -> bool:
    return (urlsplit(url).hostname or "") in LOOPBACK_NAMES


# ----------------------------------------------------------------------
# The configuration file
# ----------------------------------------------------------------------

def _fingerprint(section: dict) -> str:
    kept = {k: v for k, v in section.items() if k not in UNFINGERPRINTED}
    return hashlib.sha256(json.dumps(kept, sort_keys=True, default=str).encode()).hexdigest()


def _text(section: dict, where: str, key: str, default: Optional[str] = None) -> str:
    value = section.get(key, default)
    if value is None:
        raise ValueError(f"{where}: '{key}' is missing")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: '{key}' must be text")
    return value.strip()


def _number(section: dict, where: str, key: str, default: float) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{where}: '{key}' must be a number above 0")
    return float(value)


def _flag(section: dict, where: str, key: str, default: bool) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{where}: '{key}' must be true or false")
    return value


def _secret(section: dict, where: str, key: str) -> str:
    """``key`` from the file, or from the environment variable ``key_env`` names."""
    env = section.get(f"{key}_env")
    if env is not None:
        if key in section:
            raise ValueError(f"{where}: give '{key}' or '{key}_env', not both")
        if not isinstance(env, str) or not os.environ.get(env):
            raise ValueError(f"{where}: the environment variable {env} ('{key}_env') is not set")
        return os.environ[env]
    value = section.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{where}: '{key}' must be text")
    return value


def _roles(section: dict, where: str, fold: bool = False) -> RoleMap:
    roles = section.get("roles")
    if not isinstance(roles, dict) or set(roles) - set(ROLES):
        raise ValueError(f"{where}: 'roles' maps {' and '.join(ROLES)} to their groups")
    out = {}
    for role in ROLES:
        groups = roles.get(role) or []
        groups = [groups] if isinstance(groups, (str, int)) else groups
        if (not isinstance(groups, list)
                or not all(isinstance(g, (str, int)) and str(g).strip() for g in groups)):
            raise ValueError(f"{where}: roles.{role} must be a list of groups")
        out[role] = frozenset(str(g).strip().casefold() if fold else str(g).strip()
                              for g in groups)
    if not out[OPERATOR] and not out[VIEWER]:
        raise ValueError(f"{where}: 'roles' names no group, so nobody could log in")
    return RoleMap(out[OPERATOR], out[VIEWER], fold)


def _ca_file(section: dict, where: str) -> str:
    ca_file = section.get("ca_file", "")
    if not isinstance(ca_file, str):
        raise ValueError(f"{where}: 'ca_file' must be a path")
    if ca_file and not Path(ca_file).expanduser().is_file():
        raise ValueError(f"{where}: ca_file {ca_file} not found")
    return str(Path(ca_file).expanduser()) if ca_file else ""


def _oidc_config(section: dict) -> OidcConfig:
    where = OIDC
    issuer = _text(section, where, "issuer").rstrip("/")
    if not issuer.startswith("https://") and not (issuer.startswith("http://")
                                                  and is_loopback_url(issuer)):
        raise ValueError(f"{where}: 'issuer' must be an https:// URL")
    scopes = section.get("scopes", ["openid", "profile", "email"])
    scopes = scopes.split() if isinstance(scopes, str) else scopes
    if not isinstance(scopes, list) or not all(isinstance(s, str) and s for s in scopes):
        raise ValueError(f"{where}: 'scopes' must be a list of scope names")
    if "openid" not in scopes:
        scopes = ["openid", *scopes]
    return OidcConfig(
        issuer=issuer,
        client_id=_text(section, where, "client_id"),
        client_secret=_secret(section, where, "client_secret"),
        scopes=tuple(scopes),
        username_claim=_text(section, where, "username_claim", "preferred_username"),
        groups_claim=_text(section, where, "groups_claim", "groups"),
        label=_text(section, where, "label", "single sign-on"),
        roles=_roles(section, where),
        session_hours=_number(section, where, "session_hours", 12),
        ca_file=_ca_file(section, where),
        fingerprint=_fingerprint(section),
    )


def _ldap_config(section: dict) -> LdapConfig:
    where = LDAP
    url = _text(section, where, "url")
    scheme = urlsplit(url).scheme
    if scheme not in ("ldap", "ldaps") or not urlsplit(url).hostname:
        raise ValueError(f"{where}: 'url' must be ldaps://host[:port] or ldap://host[:port]")
    start_tls = _flag(section, where, "start_tls", scheme == "ldap")
    if start_tls and scheme == "ldaps":
        raise ValueError(f"{where}: 'start_tls' is for ldap:// URLs; ldaps:// is TLS already")
    user_filter = _text(section, where, "user_filter", AD_USER_FILTER)
    if "{username}" not in user_filter:
        raise ValueError(f"{where}: 'user_filter' needs {{username}} where the name goes")
    user_base = _text(section, where, "user_base")
    roles = _roles(section, where, fold=True)
    for group in roles.operator | roles.viewer:
        if "=" not in group:
            raise ValueError(f"{where}: roles take the groups' full DNs "
                             f"(CN=...,OU=...,DC=...), not '{group}'")
    bind_dn = section.get("bind_dn", "")
    if not isinstance(bind_dn, str):
        raise ValueError(f"{where}: 'bind_dn' must be text")
    bind_password = _secret(section, where, "bind_password")
    if bind_dn and not bind_password:
        raise ValueError(f"{where}: 'bind_dn' needs 'bind_password' or 'bind_password_env'")
    return LdapConfig(
        url=url,
        start_tls=start_tls,
        ca_file=_ca_file(section, where),
        bind_dn=bind_dn.strip(),
        bind_password=bind_password,
        user_base=user_base,
        user_filter=user_filter,
        username_attribute=_text(section, where, "username_attribute", "sAMAccountName"),
        group_attribute=_text(section, where, "group_attribute", "memberOf"),
        nested_groups=_flag(section, where, "nested_groups", False),
        group_base=_text(section, where, "group_base", user_base),
        label=_text(section, where, "label", "directory"),
        roles=roles,
        timeout=_number(section, where, "timeout", 10),
        recheck=_number(section, where, "recheck", 300),
        fingerprint=_fingerprint(section),
    )


class Directory:
    """The configured directories: ``oidc`` (an ``OidcConfig``) and
    ``ldap`` (an ``LdapDirectory``), either of which may be None."""

    def __init__(self, oidc: Optional[OidcConfig] = None, ldap: "Optional[LdapDirectory]" = None,
                 path: Optional[Path] = None):
        self.oidc = oidc
        self.ldap = ldap
        self.path = path

    def fingerprint(self, source: str) -> Optional[str]:
        """The configuration a ``source`` login was made with; None if that
        directory is not configured (any more)."""
        if source == OIDC and self.oidc:
            return self.oidc.fingerprint
        if source == LDAP and self.ldap:
            return self.ldap.config.fingerprint
        return None

    def describe(self) -> list[str]:
        """Lines for the start-up message."""
        lines = []
        if self.oidc:
            lines.append(f"Single sign-on: {self.oidc.label} ({self.oidc.issuer})")
        if self.ldap:
            lines.append(f"Directory logins: {self.ldap.config.label} ({self.ldap.config.url})")
        return lines


def load_directory(path: Path) -> Directory:
    """The directory configuration in ``path``; ValueError says what is wrong with it."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Directory file not found: {path}")
    check_private(path, "Directory file")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Directory file {path}: {exc}") from exc
    if not isinstance(data, dict) or set(data) - set(SOURCES) or not data:
        raise ValueError(f"Directory file {path}: expected an 'oidc:' or an 'ldap:' section")
    configs = {}
    for source, known, build in ((OIDC, OIDC_KEYS, _oidc_config), (LDAP, LDAP_KEYS, _ldap_config)):
        section = data.get(source)
        if section is None:
            continue
        if not isinstance(section, dict):
            raise ValueError(f"Directory file {path}: '{source}:' must be a mapping")
        unknown = sorted(set(section) - known)
        if unknown:
            raise ValueError(f"Directory file {path}: {source}: unknown setting "
                             f"{', '.join(map(str, unknown))}")
        try:
            configs[source] = build(section)
        except ValueError as exc:
            raise ValueError(f"Directory file {path}: {exc}") from exc
    inline = [f"{s}.{k}" for s, k in ((OIDC, "client_secret"), (LDAP, "bind_password"))
              if (data.get(s) or {}).get(k)]
    if inline and path.stat().st_mode & 0o077:
        logger.warning("Directory file %s holds %s and is readable by other users; chmod 600 it",
                       path, " and ".join(inline))
    ldap = configs.get(LDAP)
    if ldap and urlsplit(ldap.url).scheme == "ldap" and not ldap.start_tls:
        logger.warning("ldap: %s without TLS (start_tls: false): passwords cross the network "
                       "in clear text", ldap.url)
    return Directory(configs.get(OIDC), LdapDirectory(ldap) if ldap else None, path)


# ----------------------------------------------------------------------
# LDAP / Active Directory
# ----------------------------------------------------------------------

class InvalidCredentials(Exception):
    """The directory refused the name and password of a bind."""


def escape_filter(value: str) -> str:
    """``value`` as a literal in an LDAP search filter (RFC 4515)."""
    return "".join(f"\\{ord(c):02x}" if c in "\\*()\x00" else c for c in value)


class Ldap3Connection:
    """One bound LDAP connection, through ldap3. ``user`` "" binds anonymously."""

    def __init__(self, config: LdapConfig, user: str, password: str, strategy=None):
        try:
            import ldap3
            from ldap3.core.exceptions import LDAPException, LDAPInvalidCredentialsResult
        except ImportError as exc:
            raise DirectoryError(
                "LDAP logins need the ldap3 package: pip install 'clabfleet[ldap]'") from exc
        self._ldap3, self._error = ldap3, LDAPException
        url = urlsplit(config.url)
        tls = ldap3.Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=config.ca_file or None)
        server = ldap3.Server(url.hostname, port=url.port, use_ssl=url.scheme == "ldaps",
                              tls=tls, get_info=ldap3.NONE, connect_timeout=config.timeout)
        self._conn = ldap3.Connection(
            server, user=user or None, password=password or None, raise_exceptions=True,
            read_only=True, auto_referrals=False, receive_timeout=config.timeout,
            client_strategy=strategy or ldap3.SYNC)
        try:
            self._conn.open()
            if config.start_tls:
                self._conn.start_tls()
            self._conn.bind()
        except LDAPInvalidCredentialsResult as exc:
            self.close()
            raise InvalidCredentials(str(exc)) from exc
        except (LDAPException, OSError) as exc:
            self.close()
            raise DirectoryError(f"{config.url}: {exc}") from exc

    def search(self, base: str, query: str, attributes: tuple = ()) -> list[tuple[str, dict]]:
        """(DN, {attribute: [values]}) of each entry under ``base`` that matches."""
        ldap3 = self._ldap3
        try:
            self._conn.search(base, query, search_scope=ldap3.SUBTREE,
                              attributes=list(attributes) or ldap3.NO_ATTRIBUTES)
        except (self._error, OSError) as exc:
            raise DirectoryError(f"search under {base}: {exc}") from exc
        out = []
        for entry in self._conn.response or []:
            if entry.get("type") != "searchResEntry":
                continue  # a referral to another domain
            attrs = {k: v if isinstance(v, list) else [v]
                     for k, v in (entry.get("attributes") or {}).items()}
            out.append((entry["dn"], attrs))
        return out

    def close(self) -> None:
        try:
            self._conn.unbind()
        except (self._error, OSError):
            pass


# (config, user DN, password) -> a connection with ``search`` and ``close``
Connect = Callable[[LdapConfig, str, str], Ldap3Connection]


class LdapDirectory:
    """Logins checked against LDAP / Active Directory. Blocking: call it
    off the event loop."""

    def __init__(self, config: LdapConfig, connect: Optional[Connect] = None):
        self.config = config
        self._connect = connect or Ldap3Connection

    def authenticate(self, username: str, password: str) -> DirectoryUser:
        """The user if ``password`` is theirs and a group gives them a role;
        LoginRefused otherwise."""
        refused = LoginRefused("invalid user name or password")
        # An empty password is an anonymous bind, which a directory accepts
        if not password or not NAME_RE.fullmatch(username or ""):
            raise refused
        conn = self._service()
        try:
            found = self._find(conn, username)
            if not found:
                raise refused
            dn, attrs = found
            try:
                self._connect(self.config, dn, password).close()
            except InvalidCredentials:
                raise refused from None
            user = self._user(conn, username, dn, attrs)
        finally:
            conn.close()
        if not user.role:
            raise LoginRefused(f"{user.name} is in no group that may use this GUI",
                               authenticated=True)
        return user

    def lookup(self, username: str) -> Optional[DirectoryUser]:
        """The user as the directory has them now, without their password
        (``role`` None: in no group with a role); None if there is no such
        user. For re-checks of open logins and ``clabfleet directory check``."""
        conn = self._service()
        try:
            found = self._find(conn, username)
            return self._user(conn, username, *found) if found else None
        finally:
            conn.close()

    def _service(self):
        try:
            return self._connect(self.config, self.config.bind_dn, self.config.bind_password)
        except InvalidCredentials as exc:
            raise DirectoryError(f"{self.config.url}: the bind account "
                                 f"{self.config.bind_dn or '(anonymous)'} was refused") from exc

    def _find(self, conn, username: str) -> Optional[tuple[str, dict]]:
        cfg = self.config
        query = cfg.user_filter.replace("{username}", escape_filter(username))
        found = conn.search(cfg.user_base, query, (cfg.username_attribute, cfg.group_attribute))
        if len(found) > 1:
            logger.warning("ldap: '%s' matches %d entries under %s; refusing it",
                           username, len(found), cfg.user_base)
        return found[0] if len(found) == 1 else None

    def _user(self, conn, username: str, dn: str, attrs: dict) -> DirectoryUser:
        cfg = self.config
        lower = {k.lower(): v for k, v in attrs.items()}
        if cfg.nested_groups:
            groups = [g for g, _ in conn.search(
                cfg.group_base, AD_IN_CHAIN.replace("{dn}", escape_filter(dn)))]
        else:
            groups = [g.decode(errors="replace") if isinstance(g, bytes) else str(g)
                      for g in lower.get(cfg.group_attribute.lower(), [])]
        # The directory's spelling of the name, so "Alice" and "alice" are one user
        names = lower.get(cfg.username_attribute.lower()) or [username]
        name = names[0].decode(errors="replace") if isinstance(names[0], bytes) else str(names[0])
        role, hits = cfg.roles.resolve(groups)
        return DirectoryUser(check_name(name), role, hits, dn)
