"""Strip secrets and management addressing from imported device configs.

``clabfleet export-live --sanitise`` runs every saved config through
:func:`sanitise_config` so production credentials never land in lab files.
It is best effort: configs hold secrets in more places than any list of
commands covers, so :func:`residual_secrets` then scans the result for
anything that still looks like one, and export-live refuses to write
configs it flags. What it does, per dialect:

IOS / IOS-XE / IOS-XR / NX-OS / EOS (line based, with indented blocks):

- removed with their sub-lines: ``enable secret/password``, ``username``,
  ``aaa ...``, TACACS+/RADIUS servers and keys, ``snmp-server community /
  user / host``, ``snmp mib community-map``, ``crypto pki|ca
  certificate/trustpoint`` blocks (and the certificates in them), ``key
  config-key``; also banners, comments, ``snmp-server location/contact``
- values replaced with a lab placeholder (both ends of an adjacency get the
  same one, so OSPF/BGP/HSRP authentication still comes up in the lab):
  OSPF/OSPFv3/IS-IS/BGP/HSRP/VRRP/GLBP/NHRP/PIM/BFD keys, key-chain
  ``key-string``, NTP authentication keys, ISAKMP/IKEv2/EzVPN pre-shared
  keys, WPA PSKs, credentials in URLs, ``event manager environment``
  values, ``--token``-style options, and any remaining ``password`` /
  ``secret`` value (line passwords, PPP, NX-OS BGP ...); descriptions and
  remarks that mention a password or key lose their text
- management interfaces (``Management*``, ``mgmt0``, or any interface in a
  management VRF): addresses removed, or replaced with DHCP, or kept;
  static routes in the management VRF or via the management subnet and
  ``ip default-gateway`` removed
- a placeholder login (``admin``/``admin`` by default) is added after the
  ``hostname`` line so the node stays reachable

Junos (curly-brace or ``set`` style): TACACS+/RADIUS servers,
``authentication-order``, SNMP communities and SNMPv3 users, SSH public
keys, local certificates, login messages and comments are removed; ``$9$``
and ``$8$`` secrets, and every quoted value of a statement marked
``## SECRET-DATA``, become a plain-text placeholder (Junos encrypts it again
on commit); every ``encrypted-password`` becomes the hash of ``admin@123``
(the vrnetlab default), since a fresh hash cannot be made without
``crypt``; management (``fxp0``/``em0``/``me0``) addresses as above, the
management routing instance and static routes via the management subnet
are removed.

PEM private keys are removed from every dialect. Other platforms are not
supported: the caller drops their configs. Reports only count changes per
category and give line numbers, never the values.
"""

import ipaddress
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
MGMT_VRFS = {"mgmt", "mgmt-vrf", "mgmt-intf", "management", "mgmt_vrf", "oob", "oob-mgmt",
             "mgmt_junos"}
# sha512-crypt of "admin@123", the vrnetlab vJunos default login
JUNOS_LAB_HASH = (
    "$6$clabfleetlab$REteZGBAf8T0uFVcqwiXbKvD1Hh1OnYSoHOaHfW2M/b/tPSk4P0Qoglfc0WmOZEqs"
    "1LrN/1IV8oyrG7pPvgEw0"
)
JUNOS_LAB_PASSWORD = "admin@123"
REMOVED_TEXT = "(removed by clabfleet)"


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
    text = _strip_private_keys(text, report)
    if dialect == "junos":
        return _sanitise_junos(text, opts, report), report
    return _sanitise_ios(text, dialect, opts, report), report


_PRIVATE_KEY = re.compile(
    r"-----BEGIN ([A-Z0-9 ]*)PRIVATE KEY-----.*?-----END \1PRIVATE KEY-----\n?", re.DOTALL)


def _strip_private_keys(text: str, report: SanitiseReport) -> str:
    new, n = _PRIVATE_KEY.subn("", text)
    if n:
        report.counts["crypto"] += n
    return new


def _hex_key(opts: SanitiseOptions, length: int) -> str:
    """Hex placeholder of ``length`` digits (the key's bytes, repeated)."""
    digits = opts.key.encode().hex() or "00"
    return (digits * (length // len(digits) + 1))[:length]


# --- IOS-like dialects --------------------------------------------------------

# (category, pattern on the stripped line): the line and its sub-lines are removed
_REMOVE_BLOCKS = [(cat, re.compile(rx, re.IGNORECASE)) for cat, rx in [
    ("enable", r"^enable (secret|password)\b"),
    ("users", r"^username\s"),
    ("aaa", r"^aaa\s"),
    ("aaa", r"^(tacacs|radius)(-server)?\s"),
    ("aaa", r"^ip (tacacs|radius)\s"),
    ("aaa", r"^feature tacacs\+"),
    ("snmp", r"^snmp-server (community|user|host)\s"),
    ("snmp", r"^snmp mib community-map\s"),
    ("info", r"^snmp-server (location|contact|chassis-id)\b"),
    ("crypto", r"^crypto (pki|ca) (certificate|trustpoint)\b"),
    ("crypto", r"^key config-key\b"),
]]
# Optional encryption type: 0, 3, 5, 7, ... or IOS-XR's "encrypted" / "clear"
_OPT_TYPE = r"(?:\s+(?:\d{1,2}|encrypted|clear)(?=\s))?"
_W = r"(?<!\S)"     # start of a word
# (category, pattern): group "pre" is kept, the value after it becomes the
# placeholder and group "tail", if any, is kept; text after the match stays.
# Tried in order, the first that matches wins (all its matches are replaced).
_REPLACE = [(cat, re.compile(rx, re.IGNORECASE)) for cat, rx in [
    ("ntp", rf"(?P<pre>^\s*ntp authentication-key \d+ \S+){_OPT_TYPE}\s+\S+.*$"),
    ("keys", rf"(?P<pre>{_W}key-string(?:\s+password)?){_OPT_TYPE}\s+\S.*?"
             r"(?P<tail>\s+timeout\s+\d+(?:\s+\d+)?)?\s*$"),
    ("keys", rf"(?P<pre>{_W}message-digest-key\s+\d+\s+\S+){_OPT_TYPE}\s+\S+"),
    ("keys", rf"(?P<pre>{_W}authentication-key){_OPT_TYPE}\s+\S+"),
    ("keys", rf"(?P<pre>{_W}neighbor\s+\S+\s+password){_OPT_TYPE}\s+\S+"),
    ("keys", rf"(?P<pre>{_W}(?:isis password|area-password|domain-password|lsp-password"
             rf"|hello-password)(?:\s+(?:hmac-md5|text))?){_OPT_TYPE}\s+(?!keychain\b)\S+"),
    ("keys", rf"(?P<pre>^\s*(?:isis )?authentication key){_OPT_TYPE}\s+\S+"),
    ("keys", rf"(?P<pre>{_W}(?:standby|vrrp|glbp)(?:\s+\d+)?(?:\s+peer)?\s+authentication"
             r"(?:\s+text)?)\s+(?!(?:md5|text|key-chain|key-string|ietf-md5)\b)\S+"),
    ("keys", rf"(?P<pre>{_W}crypto isakmp key){_OPT_TYPE}\s+\S+"),
    ("keys", rf"(?P<pre>{_W}pre-shared-key(?:\s+(?:address|hostname)\s+\S+(?:\s+\S+)?)?"
             rf"\s+key){_OPT_TYPE}\s+\S+"),
    ("keys", rf"(?P<pre>{_W}pre-shared-key(?:\s+(?:local|remote))?){_OPT_TYPE}"
             r"\s+(?!(?:address|hostname|key)\b)\S+"),
    ("keys", rf"(?P<pre>{_W}pre-share\s+key){_OPT_TYPE}\s+\S+"),
    ("keys", rf"(?P<pre>{_W}nhrp authentication){_OPT_TYPE}\s+\S+"),
    ("keys", rf"(?P<pre>{_W}hello-authentication\s+ah-md5){_OPT_TYPE}\s+\S+"),
    ("keys", rf"(?P<pre>{_W}(?:wpa-psk|set-key)\s+(?:ascii|hex)){_OPT_TYPE}\s+\S.*$"),
    ("other", r"(?P<pre>^\s*event manager environment\s+\S+)\s+\S.*$"),
]]
# Keys that must be hex: replaced with the placeholder's bytes in hex
_HEX_KEY = re.compile(rf"(?P<pre>{_W}(?:hex-key|ascii-key))(?P<val>\s+\S+)", re.IGNORECASE)
# OSPFv3 IPsec: "authentication ipsec spi 256 sha1 <40 hex>", "... md5 7 <key>",
# "encryption ipsec spi 300 esp aes-cbc 128 <hex> sha1 <hex>"
_IPSEC_LINE = re.compile(r"\bipsec\s+spi\s+\d+", re.IGNORECASE)
_IPSEC_KEY = re.compile(
    r"(?P<pre>(?<!\S)(?P<alg>md5|sha1|sha256|3des|des|aes-cbc(?:\s+(?P<bits>128|192|256))?))"
    rf"{_OPT_TYPE}\s+(?!(?:md5|sha1|sha256)\b)\S+", re.IGNORECASE)
_IPSEC_HEX_LEN = {"md5": 32, "sha1": 40, "sha256": 64, "des": 16, "3des": 48}
# Bare "authentication [text] X" inside an HSRP/VRRP/GLBP block (IOS-XR, NX-OS)
_FHRP_BLOCK = re.compile(r"^(?:router\s+)?(?:hsrp|vrrp|glbp)\b", re.IGNORECASE)
_BARE_AUTH = re.compile(
    rf"(?P<pre>^\s*authentication(?:\s+text)?){_OPT_TYPE}"
    r"\s+(?!(?:md5|text|key-chain|keychain|key-string|null|message-digest|ietf-md5)\b)\S+",
    re.IGNORECASE)
# EzVPN: "crypto isakmp client configuration group G" / " key X", and
# "crypto ipsec client ezvpn E" / " group G key X"
_EZVPN_BLOCK = re.compile(r"^crypto (isakmp client configuration group|ipsec client ezvpn)\b",
                          re.IGNORECASE)
_EZVPN_KEY = re.compile(rf"(?P<pre>^\s*(?:group\s+\S+\s+)?key){_OPT_TYPE}\s+\S+",
                        re.IGNORECASE)
# Applied to every line: "tftp://user:PASS@host", "exec /bin/agent --token X"
_URL_CREDENTIALS = re.compile(r"(?P<pre>://[^\s/@:]+:)[^\s@]+@")
_CLI_SECRET = re.compile(
    r"(?P<pre>(?<!\S)--(?:token|password|passwd|secret|api-?key|apikey|key|auth-token)"
    r"(?:=|\s+))(?P<val>[^\s=]+)", re.IGNORECASE)
# Free text that mentions a secret loses its text
_FREE_TEXT = re.compile(r"^(?P<pre>\s*(?:\d+\s+)?(?:description|remark)\s+)\S.*$",
                        re.IGNORECASE)
_SECRET_WORDS = re.compile(
    r"(?i)\b(pass(?:words?|wd|phrase)?|pwd|secrets?|keys?|community|psk|tokens?|credentials?)\b")
# Any other password/secret value: "password 7 0822455D0A16", "secret sha512 $6$..."
_GENERIC = re.compile(
    r"(?<!\S)(password|secret|passwd)"
    r"(?:\s+(?:\d{1,2}|sha512|sha256|md5|encrypted|clear|cleartext)(?=\s))?"
    r"\s+(?!(?:encryption|minimum|strength-check|secure-mode|policy|lifetime|history)\b)\S+",
    re.IGNORECASE)
_GENERIC_SKIP = re.compile(r"^(no|service|security|password encryption|password-policy)\b",
                           re.IGNORECASE)
_BANNER = re.compile(r"^banner\s+\S+(?:\s+(?P<rest>.*))?$", re.IGNORECASE)
_IFACE_RE = re.compile(r"^interface\s+(\S+)")
_VRF_MEMBER_RE = re.compile(r"^(?:ip )?vrf (?:forwarding|member)\s+(\S+)|^vrf\s+(\S+)$")
_MGMT_IFACE_RE = re.compile(r"^(management|mgmt|ma)\d", re.IGNORECASE)
_ADDR_RE = re.compile(r"^(\s*)(ip|ipv4|ipv6) address\b")
_MGMT_ROUTE_RE = re.compile(r"^ip route vrf (\S+)\s")


def _sanitise_ios(text: str, dialect: str, opts: SanitiseOptions,
                  report: SanitiseReport) -> str:
    lines = text.splitlines()
    mgmt_lines = _mgmt_interface_lines(lines)
    mgmt_nets = _ios_mgmt_networks(lines, mgmt_lines)
    out: list[str] = []
    skip_indent: Optional[int] = None   # dropping sub-lines deeper than this
    skip_to = -1                        # dropping lines up to this index (banners)
    block_header = ""                   # current top-level line
    parents: list[tuple[int, str]] = []  # (indent, line) of the enclosing blocks
    dhcp_done: set[int] = set()

    for i, line in enumerate(lines):
        if i <= skip_to:
            continue
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if skip_indent is not None:
            if stripped and indent > skip_indent:
                continue
            skip_indent = None
        if stripped:
            while parents and parents[-1][0] >= indent:
                parents.pop()
        if indent == 0 and stripped:
            block_header = stripped

        if indent == 0 and _BANNER.match(stripped):
            skip_to = _banner_end(lines, i)
            report.counts["info"] += 1
            continue
        if stripped.startswith("!") and stripped.strip("!").strip():
            report.counts["comments"] += 1
            continue

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
        if opts.mgmt != "keep" and _is_mgmt_route(stripped, indent, block_header, mgmt_nets):
            report.counts["mgmt"] += 1
            continue

        context = [p for _, p in parents]
        out.append(_replace_secrets(line, context, opts, report))
        if stripped:
            parents.append((indent, stripped))

    result = out
    if opts.user:
        result = _add_login(result, dialect, opts)
        report.notes.append(f"placeholder login '{opts.user}' added")
    return "\n".join(result) + ("\n" if text.endswith("\n") else "")


def _banner_end(lines: list[str], start: int) -> int:
    """Index of the last line of the banner starting at ``start``.

    ``banner motd ^C ... ^C`` (or any delimiter character), or EOS's
    ``banner login`` with its text ending in a line ``EOF``.
    """
    rest = (_BANNER.match(lines[start].strip()).group("rest") or "").strip()
    if not rest:
        delim, after = "EOF", None
    elif rest.startswith("^C"):
        delim, after = "^C", rest[2:]
    else:
        delim, after = rest[0], rest[1:]
    if after is not None and delim in after:
        return start
    for j in range(start + 1, len(lines)):
        if (lines[j].strip() == delim) if after is None else (delim in lines[j]):
            return j
    return len(lines) - 1      # unterminated: drop the rest rather than leak it


def _replace_secrets(line: str, context: list[str], opts: SanitiseOptions,
                     report: SanitiseReport) -> str:
    new = _URL_CREDENTIALS.sub(lambda m: f"{m.group('pre')}{opts.key}@", line)
    new = _CLI_SECRET.sub(lambda m: f"{m.group('pre')}{opts.key}", new)
    if new != line:
        report.counts["other"] += 1
        line = new
    m = _FREE_TEXT.match(line)
    if m:
        if _SECRET_WORDS.search(line[m.end("pre"):]):
            report.counts["other"] += 1
            return f"{m.group('pre')}{REMOVED_TEXT}"
        return line

    def placeholder(m: re.Match) -> str:
        tail = m.groupdict().get("tail") or ""
        return f"{m.group('pre')} {opts.key}{tail}"

    rules = list(_REPLACE)
    if any(_FHRP_BLOCK.match(c) for c in context):
        rules.append(("keys", _BARE_AUTH))
    if any(_EZVPN_BLOCK.match(c) for c in context):
        rules.append(("keys", _EZVPN_KEY))
    for category, rx in rules:
        new = rx.sub(placeholder, line)
        if new != line:
            report.counts[category] += 1
            return new
    new = _HEX_KEY.sub(lambda m: f"{m.group('pre')} {_hex_key(opts, 32)}", line)
    if new != line:
        report.counts["keys"] += 1
        return new
    if _IPSEC_LINE.search(line):
        def hex_placeholder(m: re.Match) -> str:
            alg = m.group("alg").split()[0].lower()
            length = _IPSEC_HEX_LEN.get(alg) or int(m.group("bits") or 128) // 4
            return f"{m.group('pre')} {_hex_key(opts, length)}"
        new = _IPSEC_KEY.sub(hex_placeholder, line)
        if new != line:
            report.counts["keys"] += 1
            return new
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


def _ios_mgmt_networks(lines: list[str], mgmt_lines: dict[int, int]) -> list:
    """Subnets of the management interfaces ("ip address A M", "A/len")."""
    nets = []
    for i in mgmt_lines:
        words = lines[i].split()
        if len(words) < 3 or not _ADDR_RE.match(lines[i]):
            continue
        spec = words[2] if "/" in words[2] or len(words) < 4 else f"{words[2]}/{words[3]}"
        try:
            nets.append(ipaddress.ip_interface(spec).network)
        except ValueError:
            continue
    return nets


def _in_nets(words, nets: list) -> bool:
    for word in words:
        try:
            addr = ipaddress.ip_interface(word.strip(";")).ip
        except ValueError:
            continue
        if any(addr in net for net in nets):
            return True
    return False


def _is_mgmt_route(stripped: str, indent: int, block_header: str, mgmt_nets: list) -> bool:
    if indent == 0:
        m = _MGMT_ROUTE_RE.match(stripped)
        if m and m.group(1).lower() in MGMT_VRFS or stripped.startswith("ip default-gateway "):
            return True
        # A static route via the management subnet
        return bool(re.match(r"^ipv?6? route\s", stripped)) and _in_nets(stripped.split()[2:],
                                                                         mgmt_nets)
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
_JUNOS_SECRET = re.compile(r'"\$[89]\$[^"]*"|(?<![\S"])\$[89]\$[^\s;"]+')
_JUNOS_KEYWORD_SECRET = re.compile(
    r'\b(secret|authentication-key|ascii-text|hexadecimal|key|value|simple-password|password'
    r'|shared-secret|psk)\s+"[^"]*"')
_JUNOS_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)


def _sanitise_junos(text: str, opts: SanitiseOptions, report: SanitiseReport) -> str:
    lines = _junos_drop_comments(_junos_join_quoted(text.splitlines()), report)
    if any(ln.startswith("set ") for ln in lines):
        out = _junos_set(lines, opts, report)
    else:
        lines = [part for line in lines for part in _junos_split(line)]
        out = _junos_curly(lines, opts, report)
    if report.counts["users"]:
        report.notes.append(f"login passwords set to '{JUNOS_LAB_PASSWORD}' (vrnetlab default)")
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _junos_scan(text: str):
    """Yield (index, char, in_quotes) for ``text``, honouring backslash escapes."""
    quoted = escaped = False
    for i, c in enumerate(text):
        if escaped:
            escaped = False
        elif quoted and c == "\\":
            escaped = True
        elif c == '"':
            quoted = not quoted
            yield i, c, True
            continue
        yield i, c, quoted


def _junos_join_quoted(lines: list[str]) -> list[str]:
    """Join lines inside a quoted string (certificates, multi-line messages) into one."""
    out: list[str] = []
    pending: list[str] = []
    quoted = escaped = False
    for line in lines:
        pending.append(line)
        for c in line:
            if escaped:
                escaped = False
            elif quoted and c == "\\":
                escaped = True
            elif c == '"':
                quoted = not quoted
        escaped = False
        if not quoted:
            out.append("\n".join(pending))
            pending = []
    if pending:
        out.append("\n".join(pending))
    return out


def _junos_drop_comments(lines: list[str], report: SanitiseReport) -> list[str]:
    """Drop ``#`` comment lines and ``/* ... */`` annotations."""
    out, in_annotation = [], False
    for line in lines:
        stripped = line.strip()
        if in_annotation or stripped.startswith("/*"):
            in_annotation = "*/" not in stripped
            report.counts["comments"] += 1
            continue
        if stripped.startswith("#"):
            report.counts["comments"] += 1
            continue
        out.append(line)
    return out


def _junos_comment(text: str) -> tuple[str, str]:
    """Split ``stmt; ## SECRET-DATA`` into (statement, comment), outside quotes."""
    for i, c, quoted in _junos_scan(text):
        if c == "#" and not quoted and text[i:i + 2] == "##":
            return text[:i].rstrip(), text[i:]
    return text, ""


def _junos_split(line: str) -> list[str]:
    """``isis { level 2 { authentication-key "x"; } }`` → one statement per line."""
    body, comment = _junos_comment(line)
    cuts = [i + 1 for i, c, quoted in _junos_scan(body) if not quoted and c in "{;}"]
    if not cuts or (len(cuts) == 1 and not body[cuts[0]:].strip()):
        return [line]
    indent = line[:len(line) - len(line.lstrip())]
    parts = [body[a:b].strip() for a, b in zip([0] + cuts, cuts + [len(body)])]
    parts = [p for p in parts if p]
    out, depth = [], 0
    for part in parts:
        if part.startswith("}"):
            depth = max(depth - 1, 0)
        out.append(f"{indent}{'    ' * depth}{part}")
        if part.endswith("{"):
            depth += 1
    if comment:
        out[-1] += f" {comment}"
    return out


def _junos_drop(stmt: str, path: list[str], mgmt_nets: list) -> Optional[str]:
    """Category if a statement (or block header) must be removed, else None."""
    words = [w for header in path for w in header.split()] + stmt.split()
    top = words[0] if words else ""
    if top == "system" and any(_JUNOS_AAA.match(w) for w in words):
        return "aaa"
    if top == "snmp" and any(w in ("community", "v3") for w in words):
        return "snmp"
    if top == "snmp" and len(words) > 1 and words[1] in ("location", "contact"):
        return "info"
    if top == "system" and words[1:2] == ["login"] and len(words) > 2 \
            and words[2] in ("message", "announcement"):
        return "info"
    if any(_JUNOS_SSH_KEY.match(w) for w in words):
        return "users"
    if top == "security" and "certificates" in words[:3] and "local" in words[:4]:
        return "crypto"
    if _is_mgmt_address(words):
        return "mgmt"
    if top == "routing-instances" and len(words) > 1 and words[1].lower() in MGMT_VRFS:
        return "mgmt"
    if top == "system" and words[1:2] == ["management-instance"]:
        return "mgmt"
    if top == "routing-options" and "static" in words[:3] and _in_nets(words, mgmt_nets):
        return "mgmt"
    return None


def _is_mgmt_address(words: list[str]) -> bool:
    return len(words) > 1 and words[0] == "interfaces" \
        and bool(_JUNOS_MGMT_IFACES.match(words[1])) and "address" in words


def _junos_mgmt_networks(statements) -> list:
    """Subnets of the management interfaces, from (path words + statement words)."""
    nets = []
    for words in statements:
        if _is_mgmt_address(words):
            spec = words[words.index("address") + 1:][:1]
            try:
                nets.append(ipaddress.ip_interface(spec[0].strip(";{")).network)
            except (ValueError, IndexError):
                continue
    return nets


def _junos_secrets(stmt: str, path: list[str], opts: SanitiseOptions,
                   report: SanitiseReport, marked: bool = False) -> str:
    new = _JUNOS_HASH.sub(lambda m: f'{m.group(1)}"{JUNOS_LAB_HASH}"{m.group(3)}', stmt)
    if new != stmt:
        report.counts["users"] += 1
        return new
    new = _JUNOS_SECRET.sub(f'"{opts.key}"', stmt)
    new = _JUNOS_KEYWORD_SECRET.sub(lambda m: f'{m.group(1)} "{opts.key}"', new)
    new = new.replace(f'hexadecimal "{opts.key}"', f'ascii-text "{opts.key}"')
    if marked:
        # Junos marks every statement holding a secret: whatever the keyword
        # was, no quoted value of it may survive
        if _JUNOS_QUOTED.search(new):
            new = _JUNOS_QUOTED.sub(f'"{opts.key}"', new)
        elif len(new.split()) > 1:
            new = f'{new.rsplit(None, 1)[0]} "{opts.key}"'
    if new != stmt:
        words = [w for header in path for w in header.split()] + stmt.split()
        report.counts["ntp" if words[:1] == ["ntp"] or "ntp" in words else "keys"] += 1
    return new


def _mgmt_dhcp(stmt: str, path: list[str], opts: SanitiseOptions) -> bool:
    """True if a dropped management address should become ``dhcp``."""
    words = [w for header in path for w in header.split()] + stmt.split()
    return opts.mgmt == "dhcp" and "inet6" not in words and _is_mgmt_address(words)


def _junos_set(lines: list[str], opts, report) -> list[str]:
    out = []
    dhcp_done: set[str] = set()
    nets = _junos_mgmt_networks(ln[4:].split() for ln in lines if ln.startswith("set "))
    for line in lines:
        if not line.startswith("set "):
            out.append(line)
            continue
        stmt, comment = _junos_comment(line[4:])
        category = _junos_drop(stmt, [], nets)
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
        out.append("set " + _junos_secrets(stmt, [], opts, report, "SECRET-DATA" in comment))
    return out


def _junos_curly(lines: list[str], opts, report) -> list[str]:
    nets = _junos_mgmt_networks(_junos_walk(lines))
    out: list[str] = []
    path: list[str] = []
    skip_depth = 0       # > 0 while inside a removed block
    dhcp_done: set[tuple] = set()
    for i, line in enumerate(lines):
        stripped, comment = _junos_comment(line.strip())
        indent = line[:len(line) - len(line.lstrip())]
        if skip_depth:
            skip_depth += _depth_change(stripped)
            continue
        is_block = stripped.endswith("{")
        is_leaf = stripped.endswith(";")
        if not (is_block or is_leaf):
            if stripped.startswith("}") and path:
                path.pop()
            out.append(line)
            continue
        stmt = stripped[:-1].strip()
        category = _junos_drop(stmt, path, nets)
        if not category and is_block and _junos_block_uses(lines, i, nets, path, stmt):
            category = "mgmt"     # "route 0.0.0.0/0 { next-hop <mgmt gateway>; }"
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
            new = _junos_secrets(stmt, path, opts, report, "SECRET-DATA" in comment)
            out.append(f"{indent}{new};")
    return out


def _depth_change(text: str) -> int:
    return sum((c == "{") - (c == "}") for _, c, quoted in _junos_scan(text) if not quoted)


def _junos_walk(lines: list[str]):
    """Yield path + statement words for every statement of a curly config."""
    path: list[str] = []
    for line in lines:
        stripped = _junos_comment(line.strip())[0]
        if stripped.endswith("{"):
            path.append(stripped[:-1].strip())
            yield [w for p in path for w in p.split()]
        elif stripped.endswith(";"):
            yield [w for p in path for w in p.split()] + stripped[:-1].split()
        elif stripped.startswith("}") and path:
            path.pop()


def _junos_block_uses(lines: list[str], start: int, nets: list, path: list[str],
                      stmt: str) -> bool:
    """True for a static route block whose body goes via a management subnet."""
    words = [w for p in path for w in p.split()] + stmt.split()
    if not nets or words[:1] != ["routing-options"] or "static" not in words \
            or not stmt.startswith("route "):
        return False
    depth = 0
    for line in lines[start:]:
        depth += _depth_change(line)
        if _in_nets(line.split(), nets):
            return True
        if depth <= 0:
            return False
    return False


# --- Residual check -----------------------------------------------------------

# A word that is followed by a secret (or, after sanitising, the placeholder)
_RESIDUAL_KEYWORDS = {
    "key", "secret", "password", "passwd", "psk", "wpa-psk", "pre-share", "pre-shared-key",
    "hex-key", "ascii-key", "key-string", "authentication-key", "message-digest-key",
    "encrypted", "set-key", "ascii-text", "hexadecimal", "community", "community-map",
    "authentication", "hello-authentication", "spi",
}
# Words between a keyword and its value: encryption types, key IDs, algorithms
_RESIDUAL_MODIFIERS = {
    "md5", "sha", "sha1", "sha256", "sha384", "sha512", "hmac-md5", "hmac-sha1",
    "hmac-sha-1", "hmac-sha256", "hmac-sha-256", "text", "ascii", "hex", "clear",
    "cleartext", "unencrypted", "type", "value", "keyed-md5", "keyed-sha1",
    "meticulous-keyed-md5", "meticulous-keyed-sha1", "key-id", "ah-md5", "ietf-md5",
    "esp", "aes-cbc", "3des", "des",
}
# Words after a keyword that show it is not followed by a secret
_RESIDUAL_NOT_VALUES = {
    "chain", "key-chain", "keychain", "config-key", "generate", "zeroize", "import", "export",
    "storage", "pubkey-chain", "encryption", "encryption-key", "min-length", "minimum",
    "strength-check", "secure-mode", "recovery", "message-digest", "null", "mode", "ipsec",
    "address", "hostname", "local", "remote", "port-control", "host-mode", "order",
    "priority", "periodic", "timer", "event", "open", "violation", "control-direction",
    "fallback", "linksec", "display", "critical", "key-management", "network-eap", "chap",
    "pap", "ms-chap", "ms-chapv2", "eap", "policy", "lifetime", "history",
    "{", "}", "[", "]", ";", "=",
}
# "authentication X" only counts in these commands (or as "authentication text X")
_RESIDUAL_AUTH_LINES = re.compile(r"^(authentication|standby|vrrp|glbp|ip nhrp|ipv6 nhrp|nhrp)\b")
_RESIDUAL_PATTERNS = [
    ("crypt-hash", re.compile(r"\$[0-9]\$")),
    ("private-key", re.compile(r"BEGIN [A-Z0-9 ]*PRIVATE KEY")),
    ("junos-secret-data", re.compile(r"SECRET-DATA")),
]
_RESIDUAL_URL = re.compile(r"://[^/\s@:]+:([^@\s]+)@")
_RESIDUAL_HEX_LINE = re.compile(r"(?i)\b(spi|key|auth\S*|esp)\b")
_RESIDUAL_HEX = re.compile(r"(?<![\w:.-])[0-9A-Fa-f]{24,}(?![\w:.-])")


def residual_secrets(text: str, opts: Optional[SanitiseOptions] = None
                     ) -> list[tuple[int, str]]:
    """(line number, category) for every line of a sanitised config that still
    looks like it holds a secret. Never returns the values themselves.

    A fail-closed check on the result of :func:`sanitise_config`: a keyword
    such as ``password``, ``key`` or ``community`` followed by anything but a
    placeholder, crypt hashes (``$1$``, ``$9$`` ...), private keys,
    credentials in URLs, Junos ``SECRET-DATA`` marks and long hex strings
    on key lines.
    """
    opts = opts or SanitiseOptions()
    found: list[tuple[int, str]] = []
    for n, line in enumerate(text.splitlines(), 1):
        for category in _residual_line(line, opts):
            if (n, category) not in found:
                found.append((n, category))
    return found


def _residual_line(line: str, opts: SanitiseOptions) -> list[str]:
    placeholders = {opts.key, opts.password, JUNOS_LAB_HASH, JUNOS_LAB_PASSWORD}
    out = []
    scrubbed = line.replace(JUNOS_LAB_HASH, "")
    for category, rx in _RESIDUAL_PATTERNS:
        if rx.search(scrubbed):
            out.append(category)
    for m in _RESIDUAL_URL.finditer(line):
        if m.group(1) not in placeholders:
            out.append("url-credentials")
    for m in _CLI_SECRET.finditer(line):
        if m.group("val") not in placeholders:
            out.append("option-secret")
    if _RESIDUAL_HEX_LINE.search(line):
        for m in _RESIDUAL_HEX.finditer(line):
            if m.group(0) != _hex_key(opts, len(m.group(0))):
                out.append("hex-key")
                break
    words = line.split()
    low = [w.lower() for w in words]
    stripped = line.strip().lower()
    for i, word in enumerate(low):
        if word not in _RESIDUAL_KEYWORDS and not word.endswith("-password"):
            continue
        if word == "community" and not stripped.startswith("snmp"):
            continue
        if word == "authentication" and not (_RESIDUAL_AUTH_LINES.match(stripped)
                                             or low[i + 1:i + 2] == ["text"]):
            continue
        if _residual_value(words[i + 1:], placeholders, opts):
            out.append(word)
    return out


def _residual_value(after: list[str], placeholders: set, opts: SanitiseOptions) -> bool:
    """True if the words after a keyword start with something that may be a secret."""
    for raw in after:
        value = raw.rstrip(";,").strip('"')
        low = value.lower()
        if not value or value in placeholders or low in _RESIDUAL_NOT_VALUES \
                or value == _hex_key(opts, len(value)):
            return False
        if low in _RESIDUAL_KEYWORDS or low.endswith("-password"):
            return False        # checked on its own
        if low in _RESIDUAL_MODIFIERS or re.fullmatch(r"\d{1,3}", value):
            continue
        return True
    return False
