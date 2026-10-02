"""Map real-device interface names onto the names a containerlab kind accepts.

A device imported from the live network reports interfaces such as
``GigabitEthernet0/0/1``, ``Te1/1`` or ``Ethernet49/1``, but each
containerlab kind only accepts its own link endpoint names (``Ethernet0/1``
for IOL, ``eth1`` for cEOS, ``e1-1`` for SR Linux, ...). For every node the
exporter builds an :class:`InterfaceMap` that gives each original interface:

- an *endpoint* name, used in the topology's ``links``
- a *config* name, the same port as the node's OS calls it, used to rewrite
  ``interface`` stanzas and references in the saved startup config

Rules, per node:

1. Names are canonicalised first: abbreviations are expanded (``Gi0/1`` →
   ``GigabitEthernet0/1``, ``Te``, ``Fo``, ``Hu``, ``Et``, ...), so both ends
   of an LLDP adjacency agree whatever form each side reports.
2. Management ports (``Management1``, ``mgmt0``, ``fxp0``, ``em0``, ``me0``)
   and logical interfaces (loopbacks, VLANs, port-channels, tunnels) are
   never mapped; containerlab provides the management port itself.
3. A 1:1 (*native*) mapping is used when the original already names a port
   the kind has, e.g. ``Ethernet49/1`` → cEOS ``eth49_1``, ``ge-0/0/3`` →
   vJunos ``eth4``, ``GigabitEthernet0/0/2`` → IOL ``Ethernet0/2`` (the last
   two numbers as slot/port).
4. Everything else gets the lowest free port in the kind's order, interfaces
   used by links first, then the other physical interfaces in the config,
   each group in natural order, so the result is deterministic.
5. Interfaces beyond the kind's port limit are reported as dropped: their
   links are left out and their (IOS-style) config stanzas removed.
"""

import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional

from .topology import KIND_ALIASES

# Abbreviations IOS / IOS-XE / NX-OS / EOS print or accept → canonical prefix
ABBREVIATIONS = {
    "fa": "FastEthernet",
    "gi": "GigabitEthernet", "gig": "GigabitEthernet", "gige": "GigabitEthernet",
    "tw": "TwoGigabitEthernet",
    "fi": "FiveGigabitEthernet",
    "te": "TenGigabitEthernet", "ten": "TenGigabitEthernet", "tengig": "TenGigabitEthernet",
    "tengige": "TenGigabitEthernet",
    "twe": "TwentyFiveGigE", "tf": "TwentyFiveGigE",
    "twentyfivegigabitethernet": "TwentyFiveGigE",
    "fo": "FortyGigabitEthernet", "fortygige": "FortyGigabitEthernet",
    "fiftygige": "FiftyGigE",
    "hu": "HundredGigE", "hundredgigabitethernet": "HundredGigE",
    "fh": "FourHundredGigE",
    "e": "Ethernet", "et": "Ethernet", "eth": "Ethernet",
    "ma": "Management", "mgmt": "mgmt",
    "po": "Port-channel", "lo": "Loopback", "vl": "Vlan", "tu": "Tunnel",
    "se": "Serial",
}
CANONICAL_PREFIXES = sorted(set(ABBREVIATIONS.values()) | {
    "AppGigabitEthernet", "TwentyFiveGigE", "FiftyGigE", "FourHundredGigE",
    "Management", "Port-channel", "Loopback", "Vlan", "Tunnel", "Serial",
})
# Data-plane Ethernet port prefixes (after canonicalisation)
ETHERNET_PREFIXES = {
    "FastEthernet", "GigabitEthernet", "TwoGigabitEthernet", "FiveGigabitEthernet",
    "TenGigabitEthernet", "TwentyFiveGigE", "FortyGigabitEthernet", "FiftyGigE",
    "HundredGigE", "FourHundredGigE", "AppGigabitEthernet", "Ethernet",
    # Junos and SR Linux; "" is a bare port number such as SR OS "1/1/3"
    "ge-", "xe-", "et-", "mge-", "ethernet-", "",
}
# Logical interfaces (lower case, after canonicalisation): never mapped
LOGICAL_PREFIXES = {
    "port-channel", "loopback", "vlan", "tunnel", "serial", "bundle-ether", "ae", "lo",
    "irb", "nve", "bdi", "bvi", "null", "dialer", "virtual-access", "virtual-template",
    "vxlan", "vlan-interface", "lag-", "ae-",
}
MGMT_RE = re.compile(r"^(management|mgmt|fxp|em|me|vme)\d", re.IGNORECASE)

_NAME_RE = re.compile(r"^([A-Za-z][A-Za-z-]*?)?\s?(\d[\d/:_-]*)(\.\d+)?$")


@dataclass(frozen=True)
class IfName:
    """A parsed interface name: canonical prefix, port numbers, subinterface."""
    prefix: str
    number: str
    sub: str = ""

    @property
    def nums(self) -> tuple[int, ...]:
        return tuple(int(n) for n in re.findall(r"\d+", self.number))

    @property
    def name(self) -> str:
        return f"{self.prefix}{self.number}{self.sub}"


def parse(name: str) -> Optional[IfName]:
    """Parse an interface name, expanding abbreviations. None if unrecognised."""
    m = _NAME_RE.match(name.strip())
    if not m:
        return None
    prefix, number, sub = m.group(1) or "", m.group(2), m.group(3) or ""
    return IfName(_canonical_prefix(prefix), number, sub)


def _canonical_prefix(prefix: str) -> str:
    low = prefix.lower()
    if not low:
        return ""
    if low.endswith("-"):  # Junos / SR Linux style, already canonical
        return low
    if low in ABBREVIATIONS:
        return ABBREVIATIONS[low]
    for full in CANONICAL_PREFIXES:
        if full.lower() == low:
            return full
    # IOS accepts any unambiguous prefix ("Giga0/1", "TenG1/1")
    matches = [full for full in CANONICAL_PREFIXES
               if len(low) >= 2 and full.lower().startswith(low)]
    if len(matches) == 1:
        return matches[0]
    return prefix


def canonical(name: str) -> str:
    """Canonical form of an interface name (abbreviations expanded)."""
    parsed = parse(name)
    return parsed.name if parsed else name.strip()


def is_management(name: str) -> bool:
    return bool(MGMT_RE.match(canonical(name)))


def is_physical(name: str) -> bool:
    """True for a data-plane Ethernet port (not a subinterface or logical one)."""
    parsed = parse(name)
    return (parsed is not None and not parsed.sub and parsed.prefix in ETHERNET_PREFIXES
            and not is_management(name))


def is_logical(name: str) -> bool:
    """True for a recognised non-port interface: subinterface, loopback, VLAN,
    port-channel, tunnel, ... Unrecognised names (``ens3``, a MAC address
    given as an LLDP port ID) are not logical."""
    parsed = parse(name)
    return parsed is not None and (bool(parsed.sub)
                                   or parsed.prefix.lower() in LOGICAL_PREFIXES)


def natural_key(name: str):
    parsed = parse(name)
    if parsed is None:
        return ("~", (), name)
    return (parsed.prefix.lower(), parsed.nums, parsed.sub)


# --- Per-kind naming ----------------------------------------------------------

class _EthNaming:
    """``eth1``, ``eth2``, ... in the topology; config names are the same.

    Used for linux and every kind without a more specific rule.
    """
    max_ports: Optional[int] = None
    rewrites_config = False  # config names are not known for this kind

    def slot(self, index: int) -> str:
        return f"eth{index + 1}"

    def slots(self) -> Iterator[str]:
        i = 0
        while self.max_ports is None or i < self.max_ports:
            yield self.slot(i)
            i += 1

    def native(self, parsed: IfName) -> Optional[str]:
        nums = parsed.nums
        if parsed.prefix == "Ethernet" and len(nums) == 1 and nums[0] >= 1:
            return self._limit(f"eth{nums[0]}", nums[0])
        return None

    def config_name(self, endpoint: str) -> str:
        return endpoint

    def _limit(self, name: str, position: int) -> Optional[str]:
        """``name`` if port ``position`` (1-based) is within the port limit."""
        if self.max_ports is not None and position > self.max_ports:
            return None
        return name


class _IolNaming(_EthNaming):
    """Cisco IOL: ``Ethernet<slot>/<port>``, 4 ports per slot, 16 slots.

    ``Ethernet0/0`` is the management port, so data ports start at
    ``Ethernet0/1``.
    """
    max_ports = 63
    rewrites_config = True

    def slot(self, index: int) -> str:
        n = index + 1
        return f"Ethernet{n // 4}/{n % 4}"

    def native(self, parsed: IfName) -> Optional[str]:
        nums = parsed.nums
        if parsed.prefix not in ETHERNET_PREFIXES or len(nums) < 2:
            return None
        slot, port = nums[-2:]
        if 0 <= slot <= 15 and 0 <= port <= 3 and (slot, port) != (0, 0):
            return f"Ethernet{slot}/{port}"
        return None


class _CeosNaming(_EthNaming):
    """Arista cEOS: ``eth<N>`` is ``Ethernet<N>``, ``eth<N>_<M>`` is ``Ethernet<N>/<M>``."""
    rewrites_config = True

    def native(self, parsed: IfName) -> Optional[str]:
        nums = parsed.nums
        if parsed.prefix != "Ethernet" or not nums or min(nums) < 1 or len(nums) > 2:
            return None
        return "eth" + "_".join(str(n) for n in nums)

    def config_name(self, endpoint: str) -> str:
        return "Ethernet" + endpoint[3:].replace("_", "/")


class _N9kvNaming(_EthNaming):
    """Cisco Nexus 9000v: ``eth<N>`` is ``Ethernet1/<N>``."""
    rewrites_config = True

    def native(self, parsed: IfName) -> Optional[str]:
        nums = parsed.nums
        if parsed.prefix == "Ethernet" and len(nums) == 2 and nums[0] == 1 and nums[1] >= 1:
            return f"eth{nums[1]}"
        return None

    def config_name(self, endpoint: str) -> str:
        return f"Ethernet1/{endpoint[3:]}"


class _SrlNaming(_EthNaming):
    """Nokia SR Linux: ``e1-<N>`` is ``ethernet-1/<N>``."""
    rewrites_config = True

    def slot(self, index: int) -> str:
        return f"e1-{index + 1}"

    def native(self, parsed: IfName) -> Optional[str]:
        nums = parsed.nums
        if parsed.prefix == "ethernet-" and len(nums) == 2 and min(nums) >= 1:
            return f"e{nums[0]}-{nums[1]}"
        return None

    def config_name(self, endpoint: str) -> str:
        return "ethernet-" + endpoint[1:].replace("-", "/")


class _XrdNaming(_EthNaming):
    """Cisco XRd: ``Gi0-0-0-<N>`` is ``GigabitEthernet0/0/0/<N>``."""
    rewrites_config = True

    def slot(self, index: int) -> str:
        return f"Gi0-0-0-{index}"

    def native(self, parsed: IfName) -> Optional[str]:
        nums = parsed.nums
        if parsed.prefix in ETHERNET_PREFIXES and len(nums) == 4 and nums[:3] == (0, 0, 0):
            return f"Gi0-0-0-{nums[3]}"
        return None

    def config_name(self, endpoint: str) -> str:
        return "GigabitEthernet" + endpoint[2:].replace("-", "/")


class _XrvNaming(_EthNaming):
    """Cisco XRv9k (vrnetlab): ``eth<N>`` is ``GigabitEthernet0/0/0/<N-1>``."""
    rewrites_config = True

    def native(self, parsed: IfName) -> Optional[str]:
        nums = parsed.nums
        if parsed.prefix in ETHERNET_PREFIXES and len(nums) == 4 and nums[:3] == (0, 0, 0):
            return self._limit(f"eth{nums[3] + 1}", nums[3] + 1)
        return None

    def config_name(self, endpoint: str) -> str:
        return f"GigabitEthernet0/0/0/{int(endpoint[3:]) - 1}"


class _JunosNaming(_EthNaming):
    """vJunos / vSRX (vrnetlab): ``eth<N>`` is ``<prefix>-0/0/<N-1>``."""
    rewrites_config = True

    def __init__(self, prefix: str = "ge-", max_ports: Optional[int] = None):
        self.prefix = prefix
        self.max_ports = max_ports

    def native(self, parsed: IfName) -> Optional[str]:
        nums = parsed.nums
        if parsed.prefix in ("ge-", "xe-", "et-", "mge-") and len(nums) == 3 \
                and nums[:2] == (0, 0):
            return self._limit(f"eth{nums[2] + 1}", nums[2] + 1)
        return None

    def config_name(self, endpoint: str) -> str:
        return f"{self.prefix}0/0/{int(endpoint[3:]) - 1}"


class _CsrNaming(_EthNaming):
    """Cisco CSR1000v / Catalyst 8000v (vrnetlab): ``eth<N>`` is
    ``GigabitEthernet<N+1>`` (GigabitEthernet1 is management)."""
    rewrites_config = True

    def native(self, parsed: IfName) -> Optional[str]:
        nums = parsed.nums
        if parsed.prefix == "GigabitEthernet" and len(nums) == 1 and nums[0] >= 2:
            return f"eth{nums[0] - 1}"
        return None

    def config_name(self, endpoint: str) -> str:
        return f"GigabitEthernet{int(endpoint[3:]) + 1}"


class _SrosNaming(_EthNaming):
    """Nokia SR OS (vrnetlab): ``eth<N>`` is port ``1/1/<N>``."""
    rewrites_config = True

    def native(self, parsed: IfName) -> Optional[str]:
        nums = parsed.nums
        if parsed.prefix == "" and len(nums) == 3 and nums[:2] == (1, 1) and nums[2] >= 1:
            return f"eth{nums[2]}"
        return None

    def config_name(self, endpoint: str) -> str:
        return f"1/1/{endpoint[3:]}"


KIND_NAMING: dict[str, _EthNaming] = {
    "cisco_iol": _IolNaming(),
    "arista_ceos": _CeosNaming(),
    "cisco_n9kv": _N9kvNaming(),
    "nokia_srlinux": _SrlNaming(),
    "cisco_xrd": _XrdNaming(),
    "cisco_xrv9k": _XrvNaming(),
    "juniper_vjunosrouter": _JunosNaming("ge-", max_ports=10),
    "juniper_vjunosswitch": _JunosNaming("ge-", max_ports=10),
    "juniper_vjunosevolved": _JunosNaming("et-", max_ports=12),
    "juniper_vsrx": _JunosNaming("ge-"),
    "cisco_csr1000v": _CsrNaming(),
    "cisco_c8000v": _CsrNaming(),
    "nokia_sros": _SrosNaming(),
}
_DEFAULT_NAMING = _EthNaming()


def naming_for(kind: str) -> _EthNaming:
    return KIND_NAMING.get(KIND_ALIASES.get(kind, kind), _DEFAULT_NAMING)


# --- Mapping ------------------------------------------------------------------

@dataclass
class InterfaceMap:
    """Original (canonical) interface name → containerlab endpoint and config name."""
    kind: str
    endpoints: dict[str, str] = field(default_factory=dict)
    config_names: dict[str, str] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)
    rewrites_config: bool = False

    def endpoint(self, original: str) -> Optional[str]:
        return self.endpoints.get(canonical(original))

    def report(self) -> dict[str, str]:
        """{original: endpoint}, in natural order."""
        return {k: self.endpoints[k] for k in sorted(self.endpoints, key=natural_key)}


def identity_map(kind: str, names: Iterable[str]) -> InterfaceMap:
    """Keep every name as is (``--keep-interface-names``)."""
    imap = InterfaceMap(kind)
    for name in names:
        imap.endpoints[canonical(name)] = name
    return imap


def map_interfaces(
    kind: str,
    link_ifaces: Iterable[str],
    config_ifaces: Iterable[str] = (),
    pinned: Optional[dict[str, str]] = None,
) -> InterfaceMap:
    """Allocate target names for a node's interfaces (see the module docstring).

    ``pinned`` holds assignments from a previous import (original → endpoint)
    that are kept when still valid, so a re-sync does not renumber ports.
    """
    naming = naming_for(kind)
    imap = InterfaceMap(kind, rewrites_config=naming.rewrites_config)
    valid = set(naming.slots()) if naming.max_ports is not None else None

    def candidates(names, strict):
        out = []
        for name in names:
            canon = canonical(name)
            usable = is_physical(canon) if strict else not (
                is_management(canon) or is_logical(canon))
            if usable and canon not in out:
                out.append(canon)
        return sorted(out, key=natural_key)

    links = candidates(link_ifaces, strict=False)
    others = [n for n in candidates(config_ifaces, strict=True) if n not in links]
    taken: set[str] = set()

    def assign(original: str, endpoint: str) -> None:
        imap.endpoints[original] = endpoint
        imap.config_names[original] = naming.config_name(endpoint)
        taken.add(endpoint)

    for original, endpoint in (pinned or {}).items():
        original = canonical(original)
        if original in links + others and endpoint not in taken \
                and (valid is None or endpoint in valid):
            assign(original, endpoint)

    free = naming.slots()
    for group in (links, others):
        pending = [n for n in group if n not in imap.endpoints]
        for original in list(pending):
            parsed = parse(original)
            native = naming.native(parsed) if parsed else None
            if native and native not in taken:
                assign(original, native)
                pending.remove(original)
        for original in pending:
            endpoint = next((s for s in free if s not in taken), None)
            if endpoint is None:
                imap.dropped.append(original)
            else:
                assign(original, endpoint)
    return imap


# --- Config rewriting ---------------------------------------------------------

_IOS_IFACE_RE = re.compile(r"^interface\s+(\S+)", re.MULTILINE)
_SET_IFACE_RE = re.compile(r"^set interfaces (\S+)", re.MULTILINE)
_DESCRIPTION_RE = re.compile(r"^\s*(set .* )?description\s")


def config_interfaces(text: str) -> list[str]:
    """Interface names defined in a config (IOS/EOS/NX-OS stanzas or Junos)."""
    names = _IOS_IFACE_RE.findall(text) + _SET_IFACE_RE.findall(text)
    names += _junos_block_interfaces(text)
    return list(dict.fromkeys(names))


def _junos_block_interfaces(text: str) -> list[str]:
    names, depth, in_interfaces = [], 0, False
    for line in text.splitlines():
        stripped = line.strip()
        if depth == 0 and stripped == "interfaces {":
            in_interfaces = True
        elif in_interfaces and depth == 1 and stripped.endswith("{"):
            names.append(stripped[:-1].strip())
        depth += stripped.count("{") - stripped.count("}")
        if depth == 0:
            in_interfaces = False
    return names


def _variants(original: str) -> set[str]:
    """Spellings of an interface that may appear in a config, as regex fragments."""
    parsed = parse(original)
    if parsed is None:
        return {re.escape(original)}
    number = re.escape(parsed.number)
    # The full name may have a space before the number ("Ethernet 1/1");
    # abbreviations only without one, so "e 1" in free text is left alone
    found = {re.escape(parsed.prefix) + r" ?" + number}
    found.update(re.escape(abbr) + number
                 for abbr, full in ABBREVIATIONS.items() if full == parsed.prefix)
    return found


def rewrite_config(text: str, imap: InterfaceMap, raw_names: Iterable[str] = ()) -> str:
    """Rename every mapped interface in a config in one pass.

    Handles full names, abbreviations and subinterfaces (``Gi0/1.100``).
    IOS-style ``interface`` stanzas of dropped interfaces are removed. Does
    nothing for kinds whose config naming is unknown.
    """
    if not imap.rewrites_config:
        return text
    if imap.dropped:
        text = _drop_stanzas(text, set(imap.dropped))
    renames = {k: v for k, v in imap.config_names.items() if k != v}
    if not renames:
        return text
    fragments = set()
    for original in renames:
        fragments |= _variants(original)
    for raw in raw_names:
        if canonical(raw) in renames:
            fragments.add(re.escape(raw))
    alternation = "|".join(sorted(fragments, key=len, reverse=True))
    pattern = re.compile(rf"(?<![\w/.:-])(?:{alternation})(?![\w/:])", re.IGNORECASE)

    def repl(m: re.Match) -> str:
        new = renames.get(canonical(m.group(0).replace(" ", "")))
        return new if new is not None else m.group(0)

    # Descriptions are free text and usually name the *remote* port: keep them
    return "".join(
        line if _DESCRIPTION_RE.match(line) else pattern.sub(repl, line)
        for line in text.splitlines(keepends=True)
    )


def _drop_stanzas(text: str, names: set[str]) -> str:
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        if skipping and line[:1] in (" ", "\t"):
            continue
        skipping = False
        m = _IOS_IFACE_RE.match(line)
        if m and canonical(m.group(1)) in names:
            skipping = True
            continue
        out.append(line)
    return "".join(out)
