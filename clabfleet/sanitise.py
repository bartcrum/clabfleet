"""Strip secrets and management addressing from imported device configs.

``clabfleet export-live --sanitise`` runs every saved config through
:func:`sanitise_config` so production credentials never land in lab files.
What it does, per dialect:

IOS / IOS-XE / IOS-XR / NX-OS / EOS (line based, with indented blocks):

- removed with their sub-lines: ``enable secret/password``, ``username``,
  ``aaa ...``, TACACS+/RADIUS servers and keys, ``snmp-server community /
  user / host``, ``crypto pki|ca certificate/trustpoint`` blocks (and the
  certificates in them), ``key config-key``
- values replaced with a lab placeholder (both ends of an adjacency get the
  same one, so OSPF/BGP/HSRP authentication still comes up in the lab):
  OSPF/IS-IS/BGP/HSRP/VRRP keys, key-chain ``key-string``, NTP
  authentication keys, ISAKMP and IKE pre-shared keys, and any remaining
  ``password`` / ``secret`` value (line passwords, PPP, NX-OS BGP ...)
- management interfaces (``Management*``, ``mgmt0``, or any interface in a
  management VRF): addresses removed, or replaced with DHCP, or kept;
  static routes in the management VRF and ``ip default-gateway`` removed
- a placeholder login (``admin``/``admin`` by default) is added after the
  ``hostname`` line so the node stays reachable

Junos (curly-brace or ``set`` style): TACACS+/RADIUS servers,
``authentication-order``, SNMP communities and SNMPv3 users and SSH public
keys are removed; ``$9$`` secrets become a plain-text placeholder (Junos
encrypts it again on commit); every ``encrypted-password`` becomes the hash
of ``admin@123`` (the vrnetlab default), since a fresh hash cannot be made
without ``crypt``; management (``fxp0``/``em0``/``me0``) addresses as above.

Other platforms are not supported: the caller drops their configs.
Reports only count changes per category, never the values.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

# NAPALM driver → config dialect
PLATFORM_DIALECT = {
    "ios": "ios",
    "iosxr": "iosxr",
    "iosxr_netconf": "iosxr",
    "eos": "eos",
    "nxos": "nxos",
    "nxos_ssh": "nxos",
    "junos": "junos",
}
MGMT_MODES = ("remove", "dhcp", "keep")
# VRF names used for out-of-band management (compared case-insensitively)
MGMT_VRFS = {"mgmt", "mgmt-vrf", "mgmt-intf", "management", "mgmt_vrf", "oob", "oob-mgmt"}
# sha512-crypt of "admin@123", the vrnetlab vJunos default login
JUNOS_LAB_HASH = (
    "$6$clabfleetlab$REteZGBAf8T0uFVcqwiXbKvD1Hh1OnYSoHOaHfW2M/b/tPSk4P0Qoglfc0WmOZEqs"
    "1LrN/1IV8oyrG7pPvgEw0"
)
JUNOS_LAB_PASSWORD = "admin@123"


@dataclass
class SanitiseOptions:
    mgmt: str = "remove"         # what to do with management addresses (MGMT_MODES)
    user: str = "admin"          # placeholder login added to the config
    password: str = "admin"
    key: str = "lab-key"         # placeholder for protocol keys (max 8 chars for OSPF)


@dataclass
class SanitiseReport:
    dialect: str
    counts: Counter = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        out: dict = {"dialect": self.dialect, "changes": dict(sorted(self.counts.items()))}
        if self.notes:
            out["notes"] = list(self.notes)
        return out


def dialect_for(platform: str) -> Optional[str]:
    return PLATFORM_DIALECT.get((platform or "").lower())


def sanitise_config(
    text: str, platform: str, opts: Optional[SanitiseOptions] = None
) -> tuple[Optional[str], SanitiseReport]:
    """Return (sanitised config, report); config is None if the platform is unsupported."""
    opts = opts or SanitiseOptions()
    if opts.mgmt not in MGMT_MODES:
        raise ValueError(f"mgmt must be one of {', '.join(MGMT_MODES)}")
    dialect = dialect_for(platform)
    report = SanitiseReport(dialect or "unsupported")
    if dialect is None:
        report.notes.append(f"sanitising is not supported for platform '{platform}'")
        return None, report
    if dialect == "junos":
        return _sanitise_junos(text, opts, report), report
    return _sanitise_ios(text, dialect, opts, report), report


# --- IOS-like dialects --------------------------------------------------------

# (category, pattern on the stripped line): the line and its sub-lines are removed
_REMOVE_BLOCKS = [
    ("enable", re.compile(r"^enable (secret|password)\b")),
    ("users", re.compile(r"^username\s")),
    ("aaa", re.compile(r"^aaa\s")),
    ("aaa", re.compile(r"^(tacacs|radius)(-server)?\s")),
    ("aaa", re.compile(r"^ip (tacacs|radius)\s")),
    ("aaa", re.compile(r"^feature tacacs\+")),
    ("snmp", re.compile(r"^snmp-server (community|user|host)\s")),
    ("crypto", re.compile(r"^crypto (pki|ca) (certificate|trustpoint)\b")),
    ("crypto", re.compile(r"^key config-key\b")),
]
# Optional encryption type: 0, 3, 5, 7, ... or IOS-XR's "encrypted" / "clear"
_OPT_TYPE = r"(?:\s+(?:\d{1,2}|encrypted|clear)(?=\s))?"
# (category, pattern, keep-tail): group 1 is kept, the value replaced by the
# placeholder; with keep-tail the last group (text after the value) is kept
_REPLACE = [
    ("keys", re.compile(rf"^(\s*ip ospf authentication-key){_OPT_TYPE}\s+\S+.*$"), False),
    ("keys", re.compile(rf"^(\s*ip ospf message-digest-key \d+ md5){_OPT_TYPE}\s+\S+.*$"), False),
    ("keys", re.compile(rf"^(\s*neighbor \S+ password){_OPT_TYPE}\s+\S+.*$"), False),
    ("keys", re.compile(rf"^(.*\bkey-string){_OPT_TYPE}\s+\S+\s*$"), False),
    ("keys", re.compile(r"^(\s*(?:isis password|area-password|domain-password))\s+\S+.*$"),
     False),
    ("keys", re.compile(rf"^(\s*(?:isis )?authentication key){_OPT_TYPE}\s+\S+.*$"), False),
    ("keys", re.compile(rf"^(\s*authentication-key){_OPT_TYPE}\s+\S+.*$"), False),
    ("keys", re.compile(
        r"^(\s*(?:standby|vrrp)(?: \d+)? authentication(?: text)?)\s+(?!md5\b|text\b)\S+\s*$"),
     False),
    ("keys", re.compile(rf"^(\s*crypto isakmp key){_OPT_TYPE}\s+\S+(.*)$"), True),
    ("keys", re.compile(
        r"^(\s*pre-shared-key(?:\s+(?:address|hostname)\s+\S+(?:\s+\S+)?)?\s+key)"
        rf"{_OPT_TYPE}\s+\S+\s*$"), False),
    ("keys", re.compile(
        rf"^(\s*pre-shared-key(?:\s+(?:local|remote))?){_OPT_TYPE}\s+(?!address\b|hostname\b|key\b)"
        r"\S+\s*$"), False),
    ("ntp", re.compile(rf"^(\s*ntp authentication-key \d+ \S+){_OPT_TYPE}\s+\S+.*$"), False),
]
# Any other password/secret value: "password 7 0822455D0A16", "secret sha512 $6$..."
_GENERIC = re.compile(
    r"(?<!\S)(password|secret)"
    r"(?:\s+(?:\d{1,2}|sha512|sha256|md5|encrypted|clear|cleartext)(?=\s))?"
    r"\s+\S+"
)
_GENERIC_SKIP = re.compile(r"^(no|service|security|password encryption|password-policy)\b")
_IFACE_RE = re.compile(r"^interface\s+(\S+)")
_VRF_MEMBER_RE = re.compile(r"^(?:ip )?vrf (?:forwarding|member)\s+(\S+)|^vrf\s+(\S+)$")
_MGMT_IFACE_RE = re.compile(r"^(management|mgmt|ma)\d", re.IGNORECASE)
_ADDR_RE = re.compile(r"^(\s*)(ip|ipv4|ipv6) address\b")
_MGMT_ROUTE_RE = re.compile(r"^ip route vrf (\S+)\s")


def _sanitise_ios(text: str, dialect: str, opts: SanitiseOptions,
                  report: SanitiseReport) -> str:
    lines = text.splitlines()
    mgmt_lines = _mgmt_interface_lines(lines)
    out: list[str] = []
    skip_indent: Optional[int] = None   # dropping sub-lines deeper than this
    block_header = ""                   # current top-level line
    dhcp_done: set[int] = set()

    for i, line in enumerate(lines):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if skip_indent is not None:
            if stripped and indent > skip_indent:
                continue
            skip_indent = None
        if indent == 0 and stripped:
            block_header = stripped

        removed = next((cat for cat, rx in _REMOVE_BLOCKS if rx.match(stripped)), None)
        if removed:
            report.counts[removed] += 1
            skip_indent = indent
            continue

        # Management addressing
        if i in mgmt_lines and _ADDR_RE.match(line):
            report.counts["mgmt"] += 1
            m = _ADDR_RE.match(line)
            if opts.mgmt == "keep":
                out.append(line)
            elif opts.mgmt == "dhcp" and m.group(2) != "ipv6" and mgmt_lines[i] not in dhcp_done:
                dhcp_done.add(mgmt_lines[i])
                out.append(f"{m.group(1)}{m.group(2)} address dhcp")
            continue
        if opts.mgmt != "keep" and _is_mgmt_route(stripped, indent, block_header):
            report.counts["mgmt"] += 1
            continue

        out.append(_replace_secrets(line, opts, report))

    result = out
    if opts.user:
        result = _add_login(result, dialect, opts)
        report.notes.append(f"placeholder login '{opts.user}' added")
    return "\n".join(result) + ("\n" if text.endswith("\n") else "")


def _replace_secrets(line: str, opts: SanitiseOptions, report: SanitiseReport) -> str:
    for category, rx, keep_tail in _REPLACE:
        m = rx.match(line)
        if m:
            report.counts[category] += 1
            tail = m.group(m.lastindex) if keep_tail else ""
            return f"{m.group(1)} {opts.key}{tail}"
    stripped = line.strip()
    if _GENERIC_SKIP.match(stripped) or not _GENERIC.search(line):
        return line
    report.counts["other"] += 1
    return _GENERIC.sub(lambda m: f"{m.group(1)} {opts.key}", line)


def _mgmt_interface_lines(lines: list[str]) -> dict[int, int]:
    """Line index → stanza start, for every sub-line of a management interface."""
    found: dict[int, int] = {}
    i = 0
    while i < len(lines):
        m = _IFACE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        start, j = i, i + 1
        while j < len(lines) and lines[j][:1] in (" ", "\t"):
            j += 1
        body = [ln.strip() for ln in lines[start + 1:j]]
        vrfs = {(vm.group(1) or vm.group(2)).lower()
                for vm in map(_VRF_MEMBER_RE.match, body) if vm}
        if _MGMT_IFACE_RE.match(m.group(1)) or vrfs & MGMT_VRFS:
            for k in range(start + 1, j):
                found[k] = start
        i = j
    return found


def _is_mgmt_route(stripped: str, indent: int, block_header: str) -> bool:
    if indent == 0:
        m = _MGMT_ROUTE_RE.match(stripped)
        return bool(m and m.group(1).lower() in MGMT_VRFS) \
            or stripped.startswith("ip default-gateway ")
    # NX-OS: "vrf context management" / "  ip route 0.0.0.0/0 10.0.0.1"
    m = re.match(r"^vrf context (\S+)$", block_header)
    return bool(m and m.group(1).lower() in MGMT_VRFS and stripped.startswith("ip route "))


def _add_login(lines: list[str], dialect: str, opts: SanitiseOptions) -> list[str]:
    user, pw = opts.user, opts.password
    if dialect == "eos":
        login = [f"username {user} privilege 15 secret {pw}"]
    elif dialect == "nxos":
        login = ["no password strength-check",
                 f"username {user} password {pw} role network-admin"]
    elif dialect == "iosxr":
        login = [f"username {user}", " group root-lr", " group cisco-support",
                 f" secret 0 {pw}", "!"]
    else:
        login = [f"username {user} privilege 15 secret 0 {pw}"]
    at = next((i + 1 for i, ln in enumerate(lines) if ln.startswith("hostname ")), 0)
    return lines[:at] + login + lines[at:]


# --- Junos --------------------------------------------------------------------

_JUNOS_MGMT_IFACES = re.compile(r"^(fxp\d+|em\d+|me\d+|vme)$")
_JUNOS_AAA = re.compile(r"^(tacplus-server|radius-server|accounting-server|authentication-order)\b")
_JUNOS_SSH_KEY = re.compile(r"^ssh-(rsa|dsa|dss|ecdsa|ed25519)\b")
_JUNOS_HASH = re.compile(r'(encrypted-password\s+)("[^"]*"|\S+?)(;|$)')
_JUNOS_SECRET = re.compile(r'"\$9\$[^"]*"')
_JUNOS_KEYWORD_SECRET = re.compile(
    r'\b(secret|authentication-key|ascii-text|hexadecimal|key|value)\s+"[^"]*"')
_JUNOS_ADDRESS = re.compile(r"^address\s+\S+")


def _sanitise_junos(text: str, opts: SanitiseOptions, report: SanitiseReport) -> str:
    lines = text.splitlines()
    if any(ln.startswith("set ") for ln in lines):
        out = _junos_set(lines, opts, report)
    else:
        out = _junos_curly(lines, opts, report)
    if report.counts["users"]:
        report.notes.append(f"login passwords set to '{JUNOS_LAB_PASSWORD}' (vrnetlab default)")
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _junos_drop(stmt: str, path: list[str]) -> Optional[str]:
    """Category if a statement (or block header) must be removed, else None."""
    words = [w for header in path for w in header.split()] + stmt.split()
    top = words[0] if words else ""
    if top == "system" and any(_JUNOS_AAA.match(w) for w in words):
        return "aaa"
    if top == "snmp" and any(w in ("community", "v3") for w in words):
        return "snmp"
    if any(_JUNOS_SSH_KEY.match(w) for w in words):
        return "users"
    if top == "interfaces" and len(words) > 1 and _JUNOS_MGMT_IFACES.match(words[1]) \
            and "address" in words:
        return "mgmt"
    return None


def _junos_secrets(stmt: str, path: list[str], opts: SanitiseOptions,
                   report: SanitiseReport) -> str:
    new = _JUNOS_HASH.sub(lambda m: f'{m.group(1)}"{JUNOS_LAB_HASH}"{m.group(3)}', stmt)
    if new != stmt:
        report.counts["users"] += 1
        return new
    new = _JUNOS_SECRET.sub(f'"{opts.key}"', stmt)
    new = _JUNOS_KEYWORD_SECRET.sub(lambda m: f'{m.group(1)} "{opts.key}"', new)
    new = new.replace(f'hexadecimal "{opts.key}"', f'ascii-text "{opts.key}"')
    if new != stmt:
        words = [w for header in path for w in header.split()] + stmt.split()
        report.counts["ntp" if words[:1] == ["ntp"] or "ntp" in words else "keys"] += 1
    return new


def _mgmt_dhcp(stmt: str, path: list[str], opts: SanitiseOptions) -> bool:
    """True if a dropped management address should become ``dhcp``."""
    words = [w for header in path for w in header.split()] + stmt.split()
    return opts.mgmt == "dhcp" and "inet6" not in words


def _junos_set(lines: list[str], opts, report) -> list[str]:
    out = []
    dhcp_done: set[str] = set()
    for line in lines:
        if not line.startswith("set "):
            out.append(line)
            continue
        stmt = line[4:]
        category = _junos_drop(stmt, [])
        if category:
            report.counts[category] += 1
            if category == "mgmt" and opts.mgmt == "keep":
                out.append(line)
            elif category == "mgmt" and _mgmt_dhcp(stmt, [], opts):
                prefix = stmt[:stmt.index(" address")]
                if prefix not in dhcp_done:
                    dhcp_done.add(prefix)
                    out.append(f"set {prefix} dhcp")
            continue
        out.append("set " + _junos_secrets(stmt, [], opts, report))
    return out


def _junos_curly(lines: list[str], opts, report) -> list[str]:
    out: list[str] = []
    path: list[str] = []
    skip_depth = 0       # > 0 while inside a removed block
    dhcp_done: set[tuple] = set()
    for line in lines:
        # Drop trailing "## SECRET-DATA" style annotations
        stripped = line.strip() if line.lstrip().startswith("#") \
            else re.sub(r"\s*##.*$", "", line.strip())
        indent = line[:len(line) - len(line.lstrip())]
        if skip_depth:
            skip_depth += stripped.count("{") - stripped.count("}")
            continue
        is_block = stripped.endswith("{")
        is_leaf = stripped.endswith(";") and not stripped.startswith(("#", "/*"))
        if not (is_block or is_leaf):
            if stripped.startswith("}") and path:
                path.pop()
            out.append(line)
            continue
        stmt = stripped[:-1].strip()
        category = _junos_drop(stmt, path)
        if category and not (category == "mgmt" and opts.mgmt == "keep"):
            report.counts[category] += 1
            if category == "mgmt" and _mgmt_dhcp(stmt, path, opts) \
                    and tuple(path) not in dhcp_done:
                dhcp_done.add(tuple(path))
                out.append(f"{indent}dhcp;")
            if is_block:
                skip_depth = 1
            continue
        if category:
            report.counts[category] += 1
        if is_block:
            path.append(stmt)
            out.append(line)
        else:
            out.append(f"{indent}{_junos_secrets(stmt, path, opts, report)};")
    return out
