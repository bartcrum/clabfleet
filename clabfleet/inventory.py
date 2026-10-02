"""Device lists for ``clabfleet export-live``.

Devices can come from:

- the clabfleet inventory YAML (``devices:``, see
  ``topologies/live_devices_example.yaml``)
- NetBox or Nautobot, over their REST API (token from an environment
  variable, filters such as site/location, role and tag, pagination)
- an Ansible inventory, YAML or INI, with ``group_vars/`` and ``host_vars/``
  next to it

The YAML inventory can also hold ``defaults:`` (shared credentials and
NAPALM options), a ``source:`` (NetBox/Nautobot/Ansible settings), and
``kinds:`` / ``images:`` rules that pick a containerlab kind and image from
a device's platform, vendor, model, version, role, site, tags or groups.

Credentials are never logged. Per device they come from the device entry,
then ``defaults:``, then ``<field>_env`` variables named there, then the
``CLABFLEET_DEVICE_USERNAME`` / ``CLABFLEET_DEVICE_PASSWORD`` environment
variables.

The NetBox/Nautobot token only ever goes to the configured URL: HTTPS is
required (unless ``allow_http``), redirects are refused, and pagination
links must point at the same host. ``allowed_networks:`` (CIDRs) refuses
devices whose address lies outside them, so a changed primary IP in NetBox
cannot send the device password somewhere else.
"""

import ipaddress
import json
import logging
import os
import re
import shlex
import socket
import ssl
import string
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import yaml

logger = logging.getLogger(__name__)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: urllib would resend the Authorization header to any host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None     # urllib then raises HTTPError with the 3xx code


def _open(req, timeout=None, context=None):
    """``urlopen`` for http(s) only, without redirects (and without file:, ftp:, data:)."""
    director = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler(), urllib.request.UnknownHandler(),
                    urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(context=context),
                    urllib.request.HTTPDefaultErrorHandler(), _NoRedirect(),
                    urllib.request.HTTPErrorProcessor()):
        director.add_handler(handler)
    return director.open(req, timeout=timeout)


urlopen = _open  # replaced in tests

ENV_USERNAME = "CLABFLEET_DEVICE_USERNAME"
ENV_PASSWORD = "CLABFLEET_DEVICE_PASSWORD"
DEFAULT_TOKEN_ENV = {"netbox": "NETBOX_TOKEN", "nautobot": "NAUTOBOT_TOKEN"}
PAGE_SIZE = 200
MAX_PAGES = 1000
MAX_HOST_RANGE = 4096     # hosts from one Ansible range such as leaf[01:99]

# Fields rules can match on (regex, whole value, case-insensitive)
RULE_FIELDS = ("kind", "type", "platform", "vendor", "model", "version", "role", "site", "tag",
               "group", "hostname", "name")

# Ansible network OS → NAPALM driver
ANSIBLE_NETWORK_OS = {
    "ios": "ios", "cisco.ios.ios": "ios",
    "iosxr": "iosxr", "cisco.iosxr.iosxr": "iosxr",
    "nxos": "nxos_ssh", "cisco.nxos.nxos": "nxos_ssh",
    "eos": "eos", "arista.eos.eos": "eos",
    "junos": "junos", "junipernetworks.junos.junos": "junos", "juniper.device": "junos",
    "sros": "sros", "nokia.sros.md": "sros", "nokia.sros.classic": "sros",
    "fortios": "fortios", "fortinet.fortios.fortios": "fortios",
    "panos": "panos", "paloaltonetworks.panos": "panos",
}
# Substrings of NetBox/Nautobot platform slugs or network drivers → NAPALM driver,
# tried in order when the platform does not name its NAPALM driver
PLATFORM_GUESSES = [
    ("iosxr", "iosxr"), ("ios-xr", "iosxr"), ("ios_xr", "iosxr"),
    ("nxos", "nxos_ssh"), ("nx-os", "nxos_ssh"),
    ("ios", "ios"),
    ("eos", "eos"),
    ("junos", "junos"),
    ("sros", "sros"), ("sr-os", "sros"),
    ("srlinux", "srl"),
    ("panos", "panos"), ("pan-os", "panos"),
    ("fortios", "fortios"),
]


class InventoryError(Exception):
    """The device list could not be loaded."""


# --- kind / image rules -------------------------------------------------------

@dataclass
class Rule:
    """``match`` fields (compiled regexes) → ``values`` (kind/type or image)."""
    index: int
    match: dict[str, re.Pattern]
    values: dict

    def matches(self, attrs: dict) -> bool:
        for key, rx in self.match.items():
            value = attrs.get(key)
            values = value if isinstance(value, (list, tuple, set)) else [value]
            if not any(v is not None and rx.fullmatch(str(v)) for v in values):
                return False
        return True


def parse_rules(raw, section: str, targets: tuple[str, ...]) -> list[Rule]:
    """Parse an ``images:`` or ``kinds:`` list. ``targets[0]`` is required."""
    rules = []
    for i, item in enumerate(raw or [], 1):
        if not isinstance(item, dict) or not item.get(targets[0]):
            raise InventoryError(f"{section} rule #{i} needs '{targets[0]}'")
        match, values = {}, {}
        for key, value in item.items():
            if key in targets and not (section == "images" and key == "kind"):
                values[key] = value
            elif key in RULE_FIELDS:
                try:
                    match[key] = re.compile(str(value), re.IGNORECASE)
                except re.error as exc:
                    raise InventoryError(f"{section} rule #{i}: bad regex for {key}: {exc}")
            else:
                raise InventoryError(
                    f"{section} rule #{i}: unknown field '{key}' "
                    f"(match on {', '.join(RULE_FIELDS)})")
        rules.append(Rule(i, match, values))
    return rules


def first_match(rules: list[Rule], attrs: dict) -> Optional[Rule]:
    return next((r for r in rules if r.matches(attrs)), None)


# --- inventory ----------------------------------------------------------------

@dataclass
class Inventory:
    devices: list[dict]
    kind_rules: list[Rule] = field(default_factory=list)
    image_rules: list[Rule] = field(default_factory=list)
    source: str = "file"
    warnings: list[str] = field(default_factory=list)


def load_inventory(
    path: Optional[str | Path] = None,
    *,
    netbox: Optional[str] = None,
    nautobot: Optional[str] = None,
    ansible: Optional[str | Path] = None,
    filters: Optional[dict] = None,
    token_env: Optional[str] = None,
    env: Optional[dict] = None,
    allow_http: bool = False,
    allowed_networks: Optional[list[str]] = None,
) -> Inventory:
    """Load devices and rules from a file and/or NetBox, Nautobot or Ansible.

    Explicit ``netbox`` / ``nautobot`` / ``ansible`` arguments (the CLI
    flags) override a ``source:`` section in the file; ``filters`` are
    merged over the file's. ``allowed_networks`` are added to the file's
    ``allowed_networks:``; devices outside them are left out with a warning.
    """
    env = os.environ if env is None else env
    data: dict = {}
    warnings: list[str] = []
    if path:
        path = Path(path)
        kind = detect_format(path)
        if kind == "ansible":
            if ansible:
                raise InventoryError("give the Ansible inventory once (file or --ansible)")
            ansible = path
        else:
            data = _load_native(path)

    source = dict(data.get("source") or {})
    from_cli = bool(netbox or nautobot or ansible)
    if from_cli:
        source = {k: v for k, v in source.items()
                  if k in ("platform_map", "verify_tls", "allow_http")}
        if sum(bool(x) for x in (netbox, nautobot, ansible)) > 1:
            raise InventoryError("use only one of --netbox, --nautobot and --ansible")
        if netbox:
            source.update(type="netbox", url=netbox)
        elif nautobot:
            source.update(type="nautobot", url=nautobot)
        else:
            source.update(type="ansible", path=str(ansible))
    merged_filters = {**(source.get("filters") or {}), **(filters or {})}
    if token_env:
        source["token_env"] = token_env
    if allow_http:
        source["allow_http"] = True

    stype = source.get("type") or ("file" if not source else None)
    if stype not in ("file", "netbox", "nautobot", "ansible"):
        raise InventoryError(
            f"source.type must be netbox, nautobot or ansible (got {stype!r})")

    devices = list(data.get("devices") or [])
    if stype in ("netbox", "nautobot"):
        devices += _rest_devices(stype, source, merged_filters, env, warnings)
    elif stype == "ansible":
        ans_path = Path(source.get("path") or "")
        if not source.get("path"):
            raise InventoryError("source.path is required for an Ansible source")
        if path and not from_cli and not ans_path.is_absolute():
            ans_path = Path(path).parent / ans_path  # relative to the inventory file
        devices += ansible_devices(ans_path, merged_filters, warnings)
    elif merged_filters:
        raise InventoryError("--filter needs a NetBox, Nautobot or Ansible source")

    defaults = data.get("defaults") or {}
    devices = [_with_credentials(d, defaults, env) for d in devices]
    _check_devices(devices)
    networks = _parse_networks([*_as_list(data.get("allowed_networks")),
                                *(allowed_networks or [])])
    if networks:
        devices = _in_networks(devices, networks, warnings)
    return Inventory(
        devices=devices,
        kind_rules=parse_rules(data.get("kinds"), "kinds", ("kind", "type")),
        image_rules=parse_rules(data.get("images"), "images", ("image", "kind")),
        source=stype,
        warnings=warnings,
    )


def detect_format(path: Path) -> str:
    """'ansible' for an Ansible inventory (INI or YAML), else 'native'."""
    if not path.exists():
        raise InventoryError(f"inventory file not found: {path}")
    text = path.read_text()
    if path.suffix.lower() in (".ini", ".cfg", ".hosts") or _looks_ini(text):
        return "ansible"
    try:
        data = yaml.load(text, Loader=_AnsibleLoader)
    except yaml.YAMLError:
        return "native"
    if isinstance(data, dict) and not ({"devices", "source", "defaults"} & set(data)):
        if "all" in data or any(isinstance(v, dict) and ({"hosts", "children"} & set(v))
                                for v in data.values()):
            return "ansible"
    return "native"


def _looks_ini(text: str) -> bool:
    """An INI inventory: has a [section] line and is not a YAML mapping."""
    if not re.search(r"^\s*\[[^\]]+\]\s*$", text, re.MULTILINE):
        return False
    try:
        return not isinstance(yaml.load(text, Loader=_AnsibleLoader), dict)
    except yaml.YAMLError:
        return True


def _load_native(path: Path) -> dict:
    with open(path) as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if isinstance(data, list):
        return {"devices": data}
    if not isinstance(data, dict):
        raise InventoryError(f"{path}: expected a mapping with 'devices:' or a list")
    return data


def _with_credentials(device: dict, defaults: dict, env) -> dict:
    """Device entry with defaults and credentials filled in (from env if needed)."""
    out = {**defaults, **device}
    opts = {**(defaults.get("optional_args") or {}), **(device.get("optional_args") or {})}
    if opts:
        out["optional_args"] = opts
    for key, env_default in (("username", ENV_USERNAME), ("password", ENV_PASSWORD)):
        if out.get(key) in (None, ""):
            var = out.get(f"{key}_env") or env_default
            if env.get(var):
                out[key] = env[var]
        out.pop(f"{key}_env", None)
    return out


def _check_devices(devices: list[dict]) -> None:
    problems = []
    for i, dev in enumerate(devices, 1):
        label = dev.get("hostname") or dev.get("name") or f"#{i}"
        missing = [k for k in ("hostname", "platform", "username", "password")
                   if not dev.get(k)]
        if missing:
            problems.append(f"{label}: missing {', '.join(missing)}")
    if problems:
        raise InventoryError(
            "incomplete device entries (credentials can come from defaults: or the "
            f"{ENV_USERNAME} / {ENV_PASSWORD} environment variables):\n  "
            + "\n  ".join(problems))


def _as_list(value) -> list:
    return [] if value is None else [value] if isinstance(value, str) else list(value)


def _parse_networks(raw: list) -> list:
    networks = []
    for item in raw:
        try:
            networks.append(ipaddress.ip_network(str(item).strip(), strict=False))
        except ValueError:
            raise InventoryError(f"allowed_networks: not a network: {item!r}") from None
    return networks


def resolve(host: str) -> list[str]:
    """Every address ``host`` resolves to (replaced in tests)."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return []
    return sorted({info[4][0].split("%", 1)[0] for info in infos})


def _in_networks(devices: list[dict], networks: list, warnings: list) -> list[dict]:
    """Devices whose address (every address a name resolves to) is in ``networks``."""
    kept, refused = [], []
    for dev in devices:
        host = str(dev["hostname"])
        try:
            addrs = [ipaddress.ip_address(host)]
        except ValueError:
            addrs = [ipaddress.ip_address(a) for a in resolve(host)]
        if addrs and all(any(a in net for net in networks) for a in addrs):
            kept.append(dev)
        else:
            shown = ", ".join(map(str, addrs)) or "does not resolve"
            refused.append(f"{dev.get('name') or host} ({host}: {shown})")
    if refused:
        warnings.append(printable(
            f"left out {len(refused)} device(s) outside allowed_networks "
            f"({', '.join(map(str, networks))}): " + "; ".join(refused)))
    return kept


def printable(text) -> str:
    """``text`` with control characters escaped, safe to print on a terminal.

    Device names, platforms and LLDP data come from devices or NetBox and
    could carry escape sequences.
    """
    return "".join(c if c.isprintable() else
                   rf"\x{ord(c):02x}" if ord(c) < 0x100 else rf"\u{ord(c):04x}"
                   for c in str(text))


# --- NetBox / Nautobot ----------------------------------------------------------

def _rest_devices(stype: str, source: dict, filters: dict, env, warnings) -> list[dict]:
    url = source.get("url")
    if not url:
        raise InventoryError(f"source.url is required for {stype}")
    var = source.get("token_env") or DEFAULT_TOKEN_ENV[stype]
    token = env.get(var)
    if not token:
        raise InventoryError(f"set the {stype} API token in the {var} environment variable")
    verify_tls = source.get("verify_tls", True) is not False
    client = RestClient(url, token, verify_tls=verify_tls,
                        allow_http=bool(source.get("allow_http")))
    if client.scheme == "http":
        warnings.append(f"the {stype} API token is sent unencrypted to {client.base_url} "
                        "(allow_http): anyone on the path can read it")
    elif not verify_tls:
        warnings.append(f"TLS certificate verification is off for {client.base_url} "
                        "(verify_tls: false): the API token can be intercepted")
    platform_map = source.get("platform_map") or {}
    if stype == "netbox":
        return netbox_devices(client, filters, platform_map, warnings)
    return nautobot_devices(client, filters, platform_map, warnings)


class RestClient:
    """Minimal NetBox/Nautobot REST client: token auth, JSON, pagination."""

    def __init__(self, base_url: str, token: str, verify_tls: bool = True,
                 timeout: float = 30, opener: Optional[Callable] = None,
                 allow_http: bool = False):
        self.base_url = base_url.rstrip("/")
        parts = urllib.parse.urlsplit(self.base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise InventoryError(f"not an http(s) URL: {printable(_safe_url(base_url))}")
        if parts.scheme == "http" and not allow_http:
            raise InventoryError(
                f"{_safe_url(base_url)} would send the API token unencrypted: use https, "
                "or set source.allow_http: true (--allow-http) on a trusted network")
        if parts.username or parts.password:
            raise InventoryError("put the API token in its environment variable, "
                                 "not in the URL")
        self.scheme, self._netloc, self._host = parts.scheme, parts.netloc, parts.hostname
        self._token = token
        self.timeout = timeout
        self._opener = opener
        self._context = None if verify_tls else ssl._create_unverified_context()

    def get_all(self, path: str, params: Optional[dict] = None) -> list[dict]:
        """GET every page of a list endpoint."""
        query = urllib.parse.urlencode({**(params or {}), "limit": PAGE_SIZE}, doseq=True)
        url: Optional[str] = f"{self.base_url}{path}?{query}"
        results: list[dict] = []
        for _ in range(MAX_PAGES):
            if not url:
                return results
            page = self.get_json(url)
            if not isinstance(page, dict) or not isinstance(page.get("results"), list):
                raise InventoryError(f"unexpected response from {_safe_url(url)}")
            results.extend(page["results"])
            url = self._next_url(page.get("next"))
        raise InventoryError(f"more than {MAX_PAGES} pages from {path}")

    def _next_url(self, nxt) -> Optional[str]:
        """The next page, always fetched from the base URL's scheme, host and port.

        Behind a TLS-terminating proxy NetBox links to ``http://`` on the
        same host, so the scheme and port are taken from the base URL; a
        link to any other host (or a ``file:`` URL) is refused rather than
        sent the token.
        """
        if not nxt:
            return None
        parts = urllib.parse.urlsplit(urllib.parse.urljoin(self.base_url + "/", str(nxt)))
        if parts.scheme not in ("http", "https") or \
                (parts.hostname or "").lower() != self._host.lower():
            raise InventoryError(
                f"refusing pagination link to {printable(_safe_url(str(nxt)))}: not on "
                f"{self._host} (the API token is only sent to the configured URL)")
        return urllib.parse.urlunsplit((self.scheme, self._netloc, parts.path, parts.query, ""))

    def get_json(self, url: str):
        req = urllib.request.Request(url, headers={
            "Authorization": f"Token {self._token}",
            "Accept": "application/json",
        })
        kwargs = {"timeout": self.timeout}
        if self._context is not None:
            kwargs["context"] = self._context
        opener = self._opener or urlopen
        try:
            with opener(req, **kwargs) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            hint = " (check the API token)" if exc.code in (401, 403) else ""
            if 300 <= exc.code < 400:
                hint = (" (redirects are not followed, so the token is not sent elsewhere: "
                        "use the URL the server redirects to)")
            raise InventoryError(
                f"HTTP {exc.code} {exc.reason} from {_safe_url(url)}{hint}") from None
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise InventoryError(f"cannot reach {_safe_url(url)}: {reason}") from None
        try:
            return json.loads(body)
        except ValueError:
            raise InventoryError(f"invalid JSON from {_safe_url(url)}") from None


def _safe_url(url: str) -> str:
    """URL without its query string (filters may be long; never holds the token)."""
    return url.split("?", 1)[0]


def _name(obj, *keys: str) -> Optional[str]:
    """First non-empty key of a nested NetBox/Nautobot object (or the value itself)."""
    if obj is None:
        return None
    if not isinstance(obj, dict):
        return str(obj)
    for key in keys:
        if obj.get(key):
            return str(obj[key])
    return None


def _address(*candidates) -> Optional[str]:
    for ip in candidates:
        addr = _name(ip, "address")
        if addr:
            return addr.split("/", 1)[0]
    return None


def _guess_driver(*names: Optional[str]) -> Optional[str]:
    for name in names:
        low = (name or "").lower()
        for needle, driver in PLATFORM_GUESSES:
            if needle in low:
                return driver
    return None


def netbox_devices(client: RestClient, filters: dict, platform_map: dict,
                   warnings: list) -> list[dict]:
    """Devices from NetBox ``/api/dcim/devices/``.

    The NAPALM driver comes from ``platform_map`` (platform slug → driver),
    else the platform's ``napalm_driver`` field (NetBox < 4), else a guess
    from the platform slug. The address is the primary IP, else the name.
    """
    drivers = {}
    for plat in client.get_all("/api/dcim/platforms/"):
        if plat.get("napalm_driver"):
            drivers[plat.get("slug")] = plat["napalm_driver"]
    devices = []
    for dev in client.get_all("/api/dcim/devices/", filters):
        name = dev.get("name")
        platform = dev.get("platform") or {}
        slug = _name(platform, "slug", "name")
        driver = platform_map.get(slug) or drivers.get(slug) or _guess_driver(slug)
        if not name or not driver:
            warnings.append(printable(
                f"NetBox device {name or dev.get('id')}: no NAPALM driver for "
                f"platform {slug!r} (add it to source.platform_map), skipped"))
            continue
        device_type = dev.get("device_type") or {}
        devices.append(_drop_none({
            "hostname": _address(dev.get("primary_ip"), dev.get("primary_ip4"),
                                 dev.get("primary_ip6")) or name,
            "name": name,
            "platform": driver,
            "vendor": _name(device_type.get("manufacturer"), "name", "slug"),
            "model": _name(device_type, "model", "slug"),
            "role": _name(dev.get("role") or dev.get("device_role"), "slug", "name"),
            "site": _name(dev.get("site"), "slug", "name"),
            "tags": [_name(t, "slug", "name") for t in dev.get("tags") or []],
        }))
    return devices


def nautobot_devices(client: RestClient, filters: dict, platform_map: dict,
                     warnings: list) -> list[dict]:
    """Devices from Nautobot ``/api/dcim/devices/?depth=1``.

    The NAPALM driver comes from ``platform_map`` (platform name → driver),
    else the platform's ``network_driver_mappings.napalm`` or
    ``napalm_driver``, else a guess from its network driver or name.
    """
    devices = []
    for dev in client.get_all("/api/dcim/devices/", {"depth": 1, **filters}):
        name = dev.get("name")
        platform = dev.get("platform") or {}
        pname = _name(platform, "name", "display")
        mappings = platform.get("network_driver_mappings") or {} \
            if isinstance(platform, dict) else {}
        driver = (platform_map.get(pname)
                  or platform_map.get(_name(platform, "network_driver"))
                  or mappings.get("napalm")
                  or (platform.get("napalm_driver") if isinstance(platform, dict) else None)
                  or _guess_driver(_name(platform, "network_driver"), pname))
        if not name or not driver:
            warnings.append(printable(
                f"Nautobot device {name or dev.get('id')}: no NAPALM driver for "
                f"platform {pname!r} (add it to source.platform_map), skipped"))
            continue
        device_type = dev.get("device_type") or {}
        location = dev.get("location") or dev.get("site")
        devices.append(_drop_none({
            "hostname": _address(dev.get("primary_ip4"), dev.get("primary_ip"),
                                 dev.get("primary_ip6")) or name,
            "name": name,
            "platform": driver,
            "vendor": _name(device_type.get("manufacturer"), "name"),
            "model": _name(device_type, "model", "display"),
            "role": _name(dev.get("role") or dev.get("device_role"), "name", "slug"),
            "site": _name(location, "name", "slug"),
            "tags": [_name(t, "name", "slug") for t in dev.get("tags") or []],
        }))
    return devices


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, [], "")}


# --- Ansible ------------------------------------------------------------------

class _Vaulted(str):
    """An ``!vault`` value: encrypted, so unusable here (and empty)."""


class _AnsibleLoader(yaml.SafeLoader):
    pass


_AnsibleLoader.add_constructor("!vault", lambda loader, node: _Vaulted(""))
_AnsibleLoader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


@dataclass
class _Group:
    vars: dict = field(default_factory=dict)
    hosts: dict = field(default_factory=dict)     # host → inline vars
    children: list = field(default_factory=list)


def ansible_devices(path: Path, filters: Optional[dict] = None,
                    warnings: Optional[list] = None) -> list[dict]:
    """Devices from an Ansible inventory (YAML or INI).

    Variables merge like Ansible's: ``all`` group, then other groups by depth
    (parents before children) and name, then the host's own; files in
    ``group_vars/`` and ``host_vars/`` next to the inventory override the
    inventory's own variables. ``filters={"group": NAME}`` keeps only hosts
    in that group (directly or through child groups).
    """
    warnings = warnings if warnings is not None else []
    if not path.exists():
        raise InventoryError(f"Ansible inventory not found: {path}")
    text = path.read_text()
    groups = _parse_ini(text) if _looks_ini(text) or path.suffix.lower() == ".ini" \
        else _parse_ansible_yaml(text, path)
    host_files = _load_vars_dirs(path.parent, groups)

    depth = _group_depths(groups)
    host_groups: dict[str, set] = {}
    for gname, group in groups.items():
        for host in group.hosts:
            host_groups.setdefault(host, set()).add(gname)

    def ancestors(name: str, seen=None) -> set:
        seen = seen or set()
        for parent, group in groups.items():
            if name in group.children and parent not in seen:
                seen.add(parent)
                ancestors(parent, seen)
        return seen

    wanted = (filters or {}).get("group")
    unknown = set(filters or {}) - {"group"}
    if unknown:
        raise InventoryError(f"Ansible inventories only filter on group (got {', '.join(unknown)})")
    devices = []
    for host in sorted(host_groups):
        member = set(host_groups[host])
        for g in list(member):
            member |= ancestors(g)
        member.add("all")
        if wanted and wanted not in member:
            continue
        hostvars: dict = {}
        for g in sorted(member, key=lambda g: (depth.get(g, 0), g)):
            hostvars.update(groups[g].vars if g in groups else {})
        for g in sorted(host_groups[host], key=lambda g: (depth.get(g, 0), g)):
            hostvars.update(groups[g].hosts.get(host) or {})
        hostvars.update(host_files.get(host) or {})
        dev = _ansible_host(host, hostvars, sorted(member - {"all"}), warnings)
        if dev:
            devices.append(dev)
    return devices


def _ansible_host(host: str, hv: dict, groups: list, warnings: list) -> Optional[dict]:
    platform = hv.get("napalm_platform") or hv.get("napalm_driver") or hv.get("dev_os")
    if not platform:
        nos = str(hv.get("ansible_network_os") or "")
        platform = ANSIBLE_NETWORK_OS.get(nos) or ANSIBLE_NETWORK_OS.get(nos.split(".")[-1])
    if not platform:
        warnings.append(printable(f"Ansible host {host}: no ansible_network_os NAPALM knows "
                                  f"({hv.get('ansible_network_os')!r}), skipped"))
        return None
    dev = {
        "hostname": str(hv.get("ansible_host") or host),
        "name": host,
        "platform": platform,
        "groups": groups,
    }
    user = hv.get("ansible_user") or hv.get("ansible_ssh_user")
    password = next((hv[k] for k in ("ansible_password", "ansible_ssh_pass",
                                     "ansible_httpapi_pass") if hv.get(k) is not None), None)
    if isinstance(password, _Vaulted):
        warnings.append(printable(f"Ansible host {host}: vault-encrypted password not "
                                  f"supported; set {ENV_PASSWORD} instead"))
        password = None
    if user:
        dev["username"] = str(user)
    if password:
        dev["password"] = str(password)
    opts = dict(hv.get("napalm_optional_args") or {})
    if hv.get("ansible_port"):
        opts.setdefault("port", int(hv["ansible_port"]))
    secret = hv.get("ansible_become_password") or hv.get("ansible_become_pass")
    if secret and not isinstance(secret, _Vaulted):
        opts.setdefault("secret", str(secret))
    if opts:
        dev["optional_args"] = opts
    for var, key in (("clab_kind", "kind"), ("clab_image", "image"), ("clab_type", "type")):
        if hv.get(var):
            dev[key] = str(hv[var])
    return dev


def _parse_ansible_yaml(text: str, path: Path) -> dict[str, _Group]:
    try:
        data = yaml.load(text, Loader=_AnsibleLoader) or {}
    except yaml.YAMLError as exc:
        raise InventoryError(f"{path}: {exc}") from None
    if not isinstance(data, dict):
        raise InventoryError(f"{path}: not an Ansible YAML inventory")
    groups: dict[str, _Group] = {}

    def walk(name: str, node) -> None:
        group = groups.setdefault(name, _Group())
        node = node or {}
        group.vars.update(node.get("vars") or {})
        for host, hvars in (node.get("hosts") or {}).items():
            group.hosts.setdefault(str(host), {}).update(hvars or {})
        for child, cnode in (node.get("children") or {}).items():
            if child not in group.children:
                group.children.append(child)
            walk(child, cnode)

    for name, node in data.items():
        walk(name, node)
    groups.setdefault("all", _Group())
    for name in list(groups):
        if name != "all" and name not in groups["all"].children:
            if not any(name in g.children for g in groups.values()):
                groups["all"].children.append(name)
    return groups


def _parse_ini(text: str) -> dict[str, _Group]:
    groups: dict[str, _Group] = {"all": _Group(), "ungrouped": _Group()}
    section, mode = "ungrouped", "hosts"
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        m = re.match(r"^\[([^\]:]+)(?::(vars|children))?\]$", line)
        if m:
            section, mode = m.group(1), m.group(2) or "hosts"
            groups.setdefault(section, _Group())
            continue
        group = groups[section]
        if mode == "vars":
            key, _, value = line.partition("=")
            group.vars[key.strip()] = _ini_value(value.strip())
        elif mode == "children":
            groups.setdefault(line, _Group())
            group.children.append(line)
        else:
            parts = shlex.split(line, comments=True)
            if not parts:
                continue
            hvars = {}
            for item in parts[1:]:
                key, _, value = item.partition("=")
                hvars[key] = _ini_value(value)
            for host in _expand_range(parts[0]):
                group.hosts.setdefault(host, {}).update(hvars)
    for name in groups:
        if name != "all" and not any(name in g.children for g in groups.values()):
            groups["all"].children.append(name)
    return groups


def _ini_value(value: str):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _expand_range(pattern: str) -> list[str]:
    """``leaf[01:03]`` → leaf01, leaf02, leaf03; ``sw-[a:c]`` → sw-a, sw-b, sw-c."""
    m = re.search(r"\[([0-9a-z]+):([0-9a-z]+)\]", pattern)
    if not m:
        return [pattern]
    start, end = m.group(1), m.group(2)
    letters = string.ascii_lowercase
    if start.isdigit() and end.isdigit():
        if int(end) - int(start) >= MAX_HOST_RANGE:
            raise InventoryError(f"host range {pattern!r} is too large "
                                 f"(at most {MAX_HOST_RANGE} hosts)")
        width = len(start) if start.startswith("0") else 0
        items = [str(i).zfill(width) for i in range(int(start), int(end) + 1)]
    elif len(start) == 1 and len(end) == 1 and start in letters and end in letters:
        items = list(letters[letters.index(start):letters.index(end) + 1])
    else:
        raise InventoryError(f"bad host range {pattern!r} (use [01:10] or [a:f])")
    head, tail = pattern[:m.start()], pattern[m.end():]
    hosts: list[str] = []
    for item in items:
        hosts += _expand_range(f"{head}{item}{tail}")
        if len(hosts) > MAX_HOST_RANGE:
            raise InventoryError(f"host range {pattern!r} is too large "
                                 f"(at most {MAX_HOST_RANGE} hosts)")
    return hosts


def _load_vars_dirs(base: Path, groups: dict[str, _Group]) -> dict[str, dict]:
    """Merge group_vars/ into the groups; return host_vars/ per host."""
    for name, group in groups.items():
        group.vars.update(_vars_files(base / "group_vars", name))
    hosts = {h for g in groups.values() for h in g.hosts}
    return {h: hv for h in hosts if (hv := _vars_files(base / "host_vars", h))}


def _vars_files(directory: Path, name: str) -> dict:
    out: dict = {}
    files = [directory / f"{name}{ext}" for ext in ("", ".yml", ".yaml")]
    if (directory / name).is_dir():
        files = sorted((directory / name).glob("*.y*ml"))
    for f in files:
        if f.is_file():
            try:
                data = yaml.load(f.read_text(), Loader=_AnsibleLoader)
            except yaml.YAMLError as exc:
                raise InventoryError(f"{f}: {exc}") from None
            if isinstance(data, dict):
                out.update(data)
    return out


def _group_depths(groups: dict[str, _Group]) -> dict[str, int]:
    depth = {"all": 0}
    frontier = ["all"]
    while frontier:
        nxt = []
        for name in frontier:
            for child in groups.get(name, _Group()).children:
                if depth.get(child, -1) < depth[name] + 1 and depth[name] < 50:
                    depth[child] = depth[name] + 1
                    nxt.append(child)
        frontier = nxt
    return depth
