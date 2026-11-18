"""Build a containerlab topology from device config files alone.

``clabfleet import-configs`` and the GUI's "Import configs" take the
configs a tool such as Arista AVD generates (``intended/configs/*.cfg``)
and build a lab from them, without touching a device: one node per file,
with the file as its startup config.

There is no LLDP to read, so the links are worked out from the configs
(:func:`infer_links`):

- **descriptions** first: AVD describes every fabric port with its peer
  and the peer's port (``P2P_dc1-spine1_Ethernet1``,
  ``MLAG_dc1-leaf1b_Ethernet3``, ``L2_dc1-leaf1c_Ethernet1``; older
  releases ``P2P_LINK_TO_DC1-SPINE1_Ethernet1``). Any description that
  holds the hostname of another uploaded config followed by an Ethernet
  port name counts, whatever is around it, so customised descriptions
  work as long as they keep those two.
- **addresses** as a cross-check and a fallback: the two ends of a routed
  link share a /31 or /30. A described link whose addresses disagree is
  kept with a warning; two routed ports that share such a subnet and have
  no usable description become a link too.

Ports whose description names something that was not uploaded (servers,
or a switch whose config is missing) get no link and are listed in the
report. The rest (kinds, images, interface names, sanitising, writing) is
:func:`clabfleet.exporter.build_topology`, fed the inferred links in the
place of LLDP data. Only EOS configs are supported.
"""

import ipaddress
import re
from dataclasses import dataclass
from pathlib import PurePath
from typing import Optional

from . import ifmap
from .exporter import (
    DeviceData,
    ExportError,
    ExportOptions,
    ImportResult,
    build_topology,
)
from .inventory import printable
from .routing.parse import Interface, parse_config

PLATFORM = "eos"
CONFIG_SUFFIXES = (".cfg", ".conf", ".txt")
MAX_FILES = 500
MAX_CONFIG_BYTES = 2 * 1024 * 1024
# The cEOS management port is Management0; AVD configs for hardware switches
# usually have Management1
CEOS_MGMT = "Management0"

_HOSTNAME_RE = re.compile(r"^hostname[ \t]+(\S+)", re.MULTILINE)
_MGMT_STANZA_RE = re.compile(r"^interface[ \t]+(Management\d+(?:/\d+)*)[ \t]*$", re.MULTILINE)
# A port name inside a description: Ethernet1, Ethernet49/1, Et3
_PORT = r"[A-Za-z]{2,}[ ]?\d+(?:/\d+)*"


class ConfigImportError(ExportError):
    pass


@dataclass(frozen=True)
class InferredLink:
    a: str
    a_if: str
    b: str
    b_if: str
    source: str             # "descriptions" (both ends), "description" (one end), "addresses"
    subnet: str = ""        # the subnet both ends are in, when they are

    def report(self) -> dict:
        entry = {"endpoints": [f"{self.a}:{self.a_if}", f"{self.b}:{self.b_if}"],
                 "from": self.source}
        if self.subnet:
            entry["subnet"] = self.subnet
        return entry


def hostname_of(file_name: str, text: str) -> str:
    """The config's ``hostname``, else the file name without its suffix."""
    m = _HOSTNAME_RE.search(text)
    if m:
        return m.group(1)
    stem = PurePath(file_name.replace("\\", "/")).name
    for suffix in CONFIG_SUFFIXES:
        if stem.lower().endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def _routed_subnet(iface: Interface) -> Optional[ipaddress.IPv4Network]:
    addr = iface.primary
    return addr.network if addr is not None and addr.network.prefixlen in (30, 31) else None


def _host_pattern(hosts: dict[str, str]) -> Optional[re.Pattern]:
    """Matches ``<hostname><separator><port>`` for the known hostnames
    (and their short names), longest first so ``leaf1`` never wins over
    ``leaf10``. The name must start the description or follow a separator
    that is not part of a hostname."""
    if not hosts:
        return None
    names = "|".join(re.escape(h) for h in sorted(hosts, key=lambda h: (-len(h), h)))
    return re.compile(rf"(?<![A-Za-z0-9.-])({names})[\s_:,|/-]+({_PORT})(?![\w/.])", re.IGNORECASE)


def _described_peer(description: str, pattern: Optional[re.Pattern], hosts: dict[str, str],
                    own: str) -> Optional[tuple[str, str]]:
    """(hostname, canonical port) a description points at, if any."""
    if pattern is None:
        return None
    for m in pattern.finditer(description):
        host = hosts[m.group(1).lower()]
        port = ifmap.canonical(m.group(2))
        if host != own and ifmap.is_physical(port):
            return host, port
    return None


# A description that ends in a port name but names no uploaded device
_UNKNOWN_PEER_RE = re.compile(rf"(?:^|[\s_:,|-])({_PORT})\s*$")


def infer_links(configs: dict[str, str]) -> tuple[list[InferredLink], list[dict], list[str]]:
    """Links between the devices in ``configs`` (hostname → config text).

    Returns the links, the ports that seem to face a device that is not in
    ``configs`` (``{"port", "description"}``), and warnings.
    """
    warnings: list[str] = []
    # lower-case hostname or short name → hostname
    hosts: dict[str, str] = {}
    for name in configs:
        hosts.setdefault(name.lower(), name)
    for name in configs:
        hosts.setdefault(name.lower().split(".")[0], name)
    pattern = _host_pattern(hosts)

    ports: dict[tuple[str, str], Interface] = {}
    for host, text in configs.items():
        for iface in parse_config(text).interfaces.values():
            if ifmap.is_physical(iface.name):
                ports[(host, ifmap.canonical(iface.name))] = iface

    def order(key):
        return (key[0], ifmap.natural_key(key[1]))

    claims: dict[tuple[str, str], tuple[str, str]] = {}
    unmatched: list[dict] = []
    for key in sorted(ports, key=order):
        description = ports[key].description
        peer = _described_peer(description, pattern, hosts, key[0])
        if peer:
            claims[key] = peer
        elif description:
            m = _UNKNOWN_PEER_RE.search(description)
            if m and ifmap.is_physical(m.group(1)):
                unmatched.append({"port": f"{key[0]}:{key[1]}", "description": description})

    links: list[InferredLink] = []
    used: set[tuple[str, str]] = set()

    def add(a, b, source):
        net_a = _routed_subnet(ports[a]) if a in ports else None
        net_b = _routed_subnet(ports[b]) if b in ports else None
        subnet = str(net_a) if net_a is not None and net_a == net_b else ""
        if source != "addresses" and a in ports and b in ports:
            addr_a, addr_b = ports[a].primary, ports[b].primary
            if addr_a is not None and addr_b is not None and addr_a.network != addr_b.network:
                warnings.append(
                    f"link {a[0]}:{a[1]} -- {b[0]}:{b[1]} is in the descriptions, but the "
                    f"addresses are in different subnets ({addr_a} and {addr_b})")
        links.append(InferredLink(a[0], a[1], b[0], b[1], source, subnet))
        used.update((a, b))

    # Both ends name each other
    for a in sorted(claims, key=order):
        b = claims[a]
        if a not in used and b not in used and claims.get(b) == a:
            add(a, b, "descriptions")
    # One end only: the other has no description that names a device
    for a in sorted(claims, key=order):
        b = claims[a]
        if a in used:
            continue
        if b in used or b in claims:
            other = claims.get(b)
            taken = f"{b[0]}:{b[1]} " + (f"is described as facing {other[0]}:{other[1]}"
                                         if other and other != a else "is in another link")
            warnings.append(f"link {a[0]}:{a[1]} -- {b[0]}:{b[1]} left out: {taken}")
            continue
        add(a, b, "description")
        if b in ports:
            warnings.append(f"link {a[0]}:{a[1]} -- {b[0]}:{b[1]} comes from the description "
                            f"of {a[0]}:{a[1]} only")
        else:
            warnings.append(f"link {a[0]}:{a[1]} -- {b[0]}:{b[1]}: {b[0]} has no "
                            f"{b[1]} in its config")

    # Routed ports left over: two of them in one /31 or /30 are a link
    by_subnet: dict[ipaddress.IPv4Network, list[tuple[str, str]]] = {}
    for key in sorted(ports, key=order):
        net = _routed_subnet(ports[key])
        if key not in used and net is not None and not ports[key].anycast:
            by_subnet.setdefault(net, []).append(key)
    for net, members in by_subnet.items():
        if len(members) == 2 and members[0][0] != members[1][0]:
            add(members[0], members[1], "addresses")
        elif len({m[0] for m in members}) > 1:
            warnings.append(f"{net} is on {', '.join(f'{h}:{p}' for h, p in members)}: "
                            "no link made from these addresses")

    if unmatched:
        shown = ", ".join(u["port"] for u in unmatched[:5])
        more = f" and {len(unmatched) - 5} more" if len(unmatched) > 5 else ""
        warnings.append(f"{len(unmatched)} port(s) face a device whose config was not given "
                        f"and get no link: {shown}{more}")
    return links, unmatched, warnings


def _ceos_management(text: str) -> tuple[str, Optional[str]]:
    """Rename the config's management port to cEOS's Management0.

    Returns the config and the old name (None when nothing changed)."""
    stanzas = _MGMT_STANZA_RE.findall(text)
    if not stanzas or CEOS_MGMT in stanzas:
        return text, None
    old = stanzas[0]
    return re.sub(rf"\b{re.escape(old)}(?![\w/])", CEOS_MGMT, text), old


def import_configs(files: dict[str, str], options: Optional[ExportOptions] = None,
                   image: Optional[str] = None, rename_management: bool = True) -> ImportResult:
    """Build a topology from config files (file name → text).

    ``image`` is the node image (without one, ``options.image_rules``
    decide, else a placeholder). ``rename_management`` renames each
    config's management interface to ``Management0``, the one a cEOS node
    has.
    """
    options = options or ExportOptions()
    if not files:
        raise ConfigImportError("no config files given")
    if len(files) > MAX_FILES:
        raise ConfigImportError(f"too many config files ({len(files)}, at most {MAX_FILES})")

    configs: dict[str, str] = {}
    origin: dict[str, str] = {}
    for file_name in sorted(files):
        text = files[file_name]
        shown = printable(file_name)
        if not isinstance(text, str) or "\x00" in text:
            raise ConfigImportError(f"{shown} is not a text file")
        if len(text.encode("utf-8", "replace")) > MAX_CONFIG_BYTES:
            raise ConfigImportError(f"{shown} is larger than {MAX_CONFIG_BYTES // 1024} KB")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not re.search(r"^(hostname|interface)[ \t]+\S", text, re.MULTILINE):
            raise ConfigImportError(f"{shown} does not look like a device config "
                                    "(no hostname or interface line)")
        host = hostname_of(file_name, text)
        if host in configs:
            raise ConfigImportError(f"{shown} and {printable(origin[host])} are both the "
                                    f"config of '{printable(host)}'")
        configs[host], origin[host] = text, file_name

    links, unmatched, warnings = infer_links(configs)

    neighbours: dict[str, dict[str, list[dict]]] = {host: {} for host in configs}
    for link in links:
        neighbours[link.a].setdefault(link.a_if, []).append(
            {"remote_system_name": link.b, "remote_port": link.b_if})
        neighbours[link.b].setdefault(link.b_if, []).append(
            {"remote_system_name": link.a, "remote_port": link.a_if})

    renamed: dict[str, str] = {}
    collected: list[DeviceData] = []
    for host, text in configs.items():
        if rename_management:
            text, old = _ceos_management(text)
            if old:
                renamed[host] = old
        spec = {"hostname": host, "name": host, "platform": PLATFORM, "vendor": "Arista"}
        if image:
            spec["image"] = image
        collected.append(DeviceData(spec, {"hostname": host}, text, neighbours[host]))

    result = build_topology(collected, options)

    # build_topology names nodes after the hostnames, made safe and unique
    node_of = {dev.spec["hostname"]: name
               for dev, name in zip(collected, result.topology["topology"]["nodes"])}
    for host, old in renamed.items():
        result.report["nodes"][node_of[host]]["management_interface"] = \
            f"{old} renamed {CEOS_MGMT}"
    for host, name in node_of.items():
        entry = result.report["nodes"][name]
        entry.pop("source", None)
        entry["file"] = printable(PurePath(origin[host].replace("\\", "/")).name)
        if entry.get("image_from") == "inventory":
            entry["image_from"] = "given"
    result.report["source"] = "config files"
    result.report["links"] = [link.report() for link in links]
    if unmatched:
        result.report["ports_without_peer"] = unmatched
    if not links and len(configs) > 1:
        warnings.append("no links found: the interface descriptions name none of the other "
                        "configs' hostnames and no two routed ports share a /31 or /30")
    extra = [printable(w) for w in warnings]
    result.warnings[:0] = extra
    result.report["warnings"] = result.warnings
    return result
