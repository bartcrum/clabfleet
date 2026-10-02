"""Build a containerlab topology from a live network.

Connects to real devices via NAPALM, pulls running configs and LLDP
neighbours, and writes a containerlab topology that mirrors the production
network: one node per device (with its running config as the startup
config) and one link per LLDP adjacency between inventoried devices.

Collecting (:func:`collect_devices`, the only part that talks to devices)
is separate from building (:func:`build_topology`, pure), so the build can
be tested with recorded data and compared with an existing topology
(``clabfleet.resync``) before anything is written. The build:

- picks each node's kind and image (device entry, then ``kinds:`` /
  ``images:`` rules from the inventory, then the platform default)
- maps interface names to the kind's naming (``clabfleet.ifmap``) in links
  and configs
- optionally sanitises configs (``clabfleet.sanitise``), and then refuses
  to go on if a config still looks like it holds a secret, unless told to
- optionally adds LLDP neighbours that are not in the inventory as
  placeholder ``linux`` nodes

Configs hold production secrets unless sanitised, so the configs, the
topology and the report are written readable by their owner only.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from . import ifmap
from .inventory import Rule, first_match, printable
from .sanitise import SanitiseOptions, residual_secrets, sanitise_config
from .topology import dump_yaml

logger = logging.getLogger(__name__)

# NAPALM driver → containerlab kind
PLATFORM_KIND_MAP = {
    "ios": "cisco_iol",
    "iosxr": "cisco_xrd",
    "iosxr_netconf": "cisco_xrd",
    "eos": "arista_ceos",
    "nxos": "cisco_n9kv",
    "nxos_ssh": "cisco_n9kv",
    "junos": "juniper_vjunosrouter",
    "panos": "paloalto_panos",
    "fortios": "fortinet_fortigate",
    "sros": "nokia_sros",
    "srl": "nokia_srlinux",
}
PLACEHOLDER_IMAGE = "REPLACE-ME/{kind}:latest"
DEFAULT_NEIGHBOUR_IMAGE = "alpine:3"
CONFIG_DIR = "configs"
# NAPALM optional_args set unless the inventory gives them. The SSH-based
# drivers (Netmiko) then check host keys against ~/.ssh/known_hosts: a known
# device whose key changed is refused, an unknown one is still accepted
# (set ssh_strict: true to refuse those too).
SAFE_OPTIONAL_ARGS = {
    "ios": {"system_host_keys": True},
    "iosxr": {"system_host_keys": True},
    "nxos_ssh": {"system_host_keys": True},
}
# Interface names taken as they are (--keep-interface-names)
KEPT_IFACE_RE = re.compile(r"^[A-Za-z0-9_./:-]{1,64}$")


class ExportError(Exception):
    """Raised when an export operation fails."""


class ResidualSecretsError(ExportError):
    """Sanitised configs that still look like they hold secrets."""

    def __init__(self, findings: dict[str, list[tuple[int, str]]]):
        self.findings = findings
        lines = [printable(f"  {node}: line {n} ({category})")
                 for node, found in findings.items() for n, category in found]
        super().__init__(
            "sanitised configs still look like they hold secrets (values not shown):\n"
            + "\n".join(lines) + "\nNothing written. Check those lines in the device "
            "configs; --allow-residual writes the configs anyway.")


@dataclass
class DeviceData:
    """What NAPALM returned for one inventory device."""
    spec: dict          # the inventory entry
    facts: dict
    config: str         # running config
    lldp: dict          # get_lldp_neighbors_detail(): local port → [neighbour, ...]


@dataclass
class ExportOptions:
    lab_name: str = "imported-topology"
    kind_map: dict = field(default_factory=dict)          # platform → kind overrides
    kind_rules: list[Rule] = field(default_factory=list)
    image_rules: list[Rule] = field(default_factory=list)
    map_interfaces: bool = True
    sanitise: Optional[SanitiseOptions] = None            # None: keep configs as they are
    allow_residual: bool = False                          # write configs the check flags
    include_neighbours: bool = False
    neighbour_image: str = DEFAULT_NEIGHBOUR_IMAGE
    inline_configs: bool = False                          # embed configs, no files
    # node → {original interface: endpoint} from a previous import report,
    # so a re-sync keeps earlier assignments
    pinned_interfaces: dict = field(default_factory=dict)


@dataclass
class ImportResult:
    topology: dict
    configs: dict[str, str]          # node → startup config text (file mode)
    report: dict
    warnings: list[str]


def collect_devices(devices: list[dict]) -> list[DeviceData]:
    """Connect to each device with NAPALM and fetch facts, config and LLDP.

    Devices that fail are logged and skipped. Requires ``pip install napalm``.
    """
    try:
        from napalm import get_network_driver
    except ImportError:
        raise ExportError(
            "NAPALM is required for live network export. "
            "Install it with: pip install 'clabfleet[napalm]'"
        )
    collected = []
    for dev_def in devices:
        hostname = dev_def["hostname"]
        platform = dev_def["platform"]
        logger.info("Connecting to %s (%s)", printable(hostname), printable(platform))
        try:
            driver = get_network_driver(platform)
            device = driver(
                hostname=hostname,
                username=dev_def["username"],
                password=dev_def["password"],
                optional_args={**SAFE_OPTIONAL_ARGS.get(platform, {}),
                               **(dev_def.get("optional_args") or {})},
            )
            device.open()
            try:
                facts = device.get_facts()
                running_config = device.get_config()["running"]
                neighbors = device.get_lldp_neighbors_detail()
            finally:
                device.close()
        except Exception as exc:
            logger.error("Failed to collect data from %s: %s", printable(hostname),
                         printable(exc))
            continue
        spec = {k: v for k, v in dev_def.items()
                if k not in ("username", "password", "optional_args")}
        collected.append(DeviceData(spec, facts or {}, running_config or "", neighbors or {}))
    return collected


def export_from_live_network(
    devices: list[dict],
    output_file: Optional[str | Path] = None,
    lab_name: str = "imported-topology",
    kind_map: Optional[dict[str, str]] = None,
    options: Optional[ExportOptions] = None,
) -> dict:
    """Build a containerlab topology by connecting to real network devices.

    Args:
        devices: List of device dicts, each with:
            - hostname (str): device FQDN or IP
            - platform (str): NAPALM driver name (ios, eos, junos, nxos_ssh, ...)
            - username / password (str)
            - kind (str, optional): containerlab kind (default: rules, then platform)
            - image (str, optional): container image for the node
            - type (str, optional): containerlab node type (e.g. "L2" for IOL-L2)
            - optional_args (dict, optional): extra NAPALM args
        output_file: Write the topology here. Running configs are written to
            ``configs/<node>.cfg`` next to it and a report of interface
            renames, images and sanitising to ``<name>.import-report.yaml``;
            without an output file configs are embedded inline.
        lab_name: Name for the generated lab.
        kind_map: Override the default platform → kind mapping.
        options: Everything else (see :class:`ExportOptions`); its lab_name
            and kind_map are used when given.

    Returns:
        The topology dict.
    """
    options = options or ExportOptions(lab_name=lab_name, kind_map=dict(kind_map or {}))
    options.inline_configs = output_file is None
    result = build_topology(collect_devices(devices), options)
    if output_file:
        write_result(result, output_file)
    return result.topology


# --- Building -----------------------------------------------------------------

@dataclass
class _Node:
    name: str
    kind: str
    data: dict                                   # topology node entry
    device: Optional[DeviceData] = None          # None for neighbour placeholders
    link_ifaces: list[str] = field(default_factory=list)
    imap: Optional[ifmap.InterfaceMap] = None
    report: dict = field(default_factory=dict)


def build_topology(collected: list[DeviceData], options: ExportOptions) -> ImportResult:
    """Turn collected device data into a topology, configs and a report."""
    warnings: list[str] = []
    nodes: dict[str, _Node] = {}
    aliases: dict[str, str] = {}        # hostname/FQDN variants → node name
    placeholders: list[str] = []

    for dev in collected:
        name = _unique(_node_name(dev.facts.get("hostname") or dev.spec.get("name")
                                  or dev.spec["hostname"]), nodes)
        node = _device_node(name, dev, options, warnings)
        nodes[name] = node
        for alias in (dev.spec["hostname"], dev.spec.get("name"), dev.facts.get("hostname"),
                      dev.facts.get("fqdn")):
            _add_alias(aliases, alias, name)

    # LLDP adjacencies, with original interface names
    raw_links: list[tuple[str, str, str, str]] = []
    seen: set[frozenset] = set()
    used: set[tuple[str, str]] = set()  # (node, canonical port) already in a link
    for node in list(nodes.values()):
        if node.device is None:
            continue
        lldp = node.device.lldp
        for local_iface in sorted(lldp, key=ifmap.natural_key):
            if ifmap.is_management(local_iface):
                logger.info("Skipping LLDP neighbours on management port %s:%s",
                            node.name, printable(local_iface))
                continue
            for neigh in lldp[local_iface] or []:
                remote = _remote_node(neigh, aliases, nodes, options, placeholders)
                remote_iface = str(neigh.get("remote_port") or "")
                if remote and not remote_iface and remote in placeholders:
                    remote_iface = f"to-{node.name}-{local_iface}"
                if not remote or not remote_iface:
                    logger.info("Skipping LLDP neighbour %r on %s:%s (not in inventory)",
                                neigh.get("remote_system_name"), node.name,
                                printable(local_iface))
                    continue
                if remote not in placeholders and ifmap.is_management(remote_iface):
                    logger.info("Skipping management link %s:%s -- %s:%s",
                                node.name, printable(local_iface), remote,
                                printable(remote_iface))
                    continue
                if not options.map_interfaces:
                    bad = [i for i in (local_iface, remote_iface) if not KEPT_IFACE_RE.match(i)]
                    if bad:
                        warnings.append(
                            f"link {node.name}:{local_iface} -- {remote}:{remote_iface} left "
                            f"out: {', '.join(map(repr, bad))} is not a usable interface name")
                        continue
                key = frozenset([(node.name, ifmap.canonical(local_iface)),
                                 (remote, ifmap.canonical(remote_iface))])
                if key in seen:
                    continue
                seen.add(key)
                clash = key & used
                if clash:
                    # Stale or one-sided LLDP data: a port can only be in one link
                    warnings.append(
                        f"link {node.name}:{local_iface} -- {remote}:{remote_iface} left out: "
                        f"{', '.join(f'{n}:{i}' for n, i in sorted(clash))} already used")
                    continue
                used |= key
                raw_links.append((node.name, local_iface, remote, remote_iface))
                node.link_ifaces.append(local_iface)
                nodes[remote].link_ifaces.append(remote_iface)

    # Interface maps
    for node in nodes.values():
        config_ifaces = ifmap.config_interfaces(node.device.config) if node.device else []
        if options.map_interfaces:
            node.imap = ifmap.map_interfaces(
                node.kind, node.link_ifaces, config_ifaces,
                pinned=(options.pinned_interfaces or {}).get(node.name))
        else:
            node.imap = ifmap.identity_map(node.kind, node.link_ifaces)
        node.report["interfaces"] = node.imap.report()
        if node.imap.dropped:
            node.report["dropped_interfaces"] = list(node.imap.dropped)
            warnings.append(
                f"{node.name}: no free {node.kind} port for {', '.join(node.imap.dropped)}")

    links = []
    for a, a_if, b, b_if in raw_links:
        a_ep, b_ep = nodes[a].imap.endpoint(a_if), nodes[b].imap.endpoint(b_if)
        if not a_ep or not b_ep:
            warnings.append(f"link {a}:{a_if} -- {b}:{b_if} left out (interface not mapped)")
            continue
        links.append({"endpoints": [f"{a}:{a_ep}", f"{b}:{b_ep}"]})

    # Configs
    configs: dict[str, str] = {}
    residual: dict[str, list[tuple[int, str]]] = {}
    for node in nodes.values():
        if node.device is None:
            continue
        text = _node_config(node, options, warnings)
        if text is None:
            continue
        if options.sanitise is not None:
            found = residual_secrets(text, options.sanitise)
            if found:
                residual[node.name] = found
                node.report["sanitised"]["residual"] = [
                    {"line": n, "category": category} for n, category in found]
        if options.inline_configs:
            node.data["startup-config"] = text
        else:
            configs[node.name] = text
            node.data["startup-config"] = f"{CONFIG_DIR}/{node.name}.cfg"

    if residual and not options.allow_residual:
        raise ResidualSecretsError(residual)
    for name, found in residual.items():
        warnings.append(f"{name}: config may still hold secrets at line(s) "
                        f"{', '.join(f'{n} ({c})' for n, c in found)} (--allow-residual)")

    missing = [n.name for n in nodes.values()
               if str(n.data.get("image", "")).startswith("REPLACE-ME/")]
    if missing:
        warnings.append(f"no image for {', '.join(missing)}: set 'image' on the device or "
                        "add an images: rule to the inventory")

    # Device and LLDP strings end up in warnings: no terminal escapes
    warnings[:] = [printable(w) for w in warnings]
    topo = {
        "name": options.lab_name,
        "topology": {"nodes": {n.name: n.data for n in nodes.values()}, "links": links},
    }
    report = {
        "lab": options.lab_name,
        "sanitised": options.sanitise is not None,
        "nodes": {n.name: n.report for n in nodes.values()},
        "warnings": warnings,
    }
    return ImportResult(topo, configs, report, warnings)


def _device_node(name: str, dev: DeviceData, options: ExportOptions, warnings) -> _Node:
    spec, facts = dev.spec, dev.facts
    platform = spec["platform"]
    attrs = {
        "platform": platform,
        "vendor": facts.get("vendor") or spec.get("vendor"),
        "model": facts.get("model") or spec.get("model"),
        "version": facts.get("os_version") or spec.get("version"),
        "role": spec.get("role"),
        "site": spec.get("site"),
        "group": spec.get("groups") or [],
        "tag": spec.get("tags") or [],
        "hostname": spec["hostname"],
        "name": name,
    }
    # A sanitised import does not record the device's management address
    report: dict = {} if options.sanitise is not None else {"source": spec["hostname"]}
    report["platform"] = platform
    for key in ("vendor", "model", "version"):
        if attrs[key]:
            report[key] = attrs[key]

    kind, node_type = spec.get("kind"), spec.get("type")
    rule = None if kind else first_match(options.kind_rules, attrs)
    if rule:
        kind = rule.values["kind"]
        node_type = node_type or rule.values.get("type")
        report["kind_rule"] = rule.index
    if not kind:
        kind = {**PLATFORM_KIND_MAP, **(options.kind_map or {})}.get(platform)
    if not kind:
        warnings.append(f"{name}: no containerlab kind for platform '{platform}', using linux")
        kind = "linux"
    attrs["kind"], attrs["type"] = kind, node_type

    node: dict = {"kind": kind}
    if node_type:
        node["type"] = node_type
    image = spec.get("image")
    if image:
        report["image_from"] = "inventory"
    else:
        rule = first_match(options.image_rules, attrs)
        if rule:
            image = rule.values["image"]
            report["image_from"] = f"images rule #{rule.index}"
        else:
            image = PLACEHOLDER_IMAGE.format(kind=kind)
            report["image_from"] = "placeholder"
    node["image"] = image
    report.update(kind=kind, image=image)
    return _Node(name, kind, node, device=dev, report=report)


def _remote_node(neigh: dict, aliases: dict, nodes: dict, options: ExportOptions,
                 placeholders: list) -> Optional[str]:
    """Node name for an LLDP neighbour; creates a placeholder if allowed."""
    remote_sys = str(neigh.get("remote_system_name") or "").strip().lower()
    # An empty name (or ".example.com") must not match a device by accident
    found = remote_sys.split(".")[0] and (aliases.get(remote_sys)
                                          or aliases.get(remote_sys.split(".")[0]))
    if found or not options.include_neighbours:
        return found or None
    ident = remote_sys and neigh.get("remote_system_name") or neigh.get("remote_chassis_id")
    if not ident:
        return None
    ident = str(ident).strip()
    name = _unique(_node_name(ident), nodes)
    report = {"neighbour": True, "kind": "linux", "image": options.neighbour_image}
    if options.sanitise is None:
        report = {"source": ident, **report}
    nodes[name] = _Node(name, "linux", {"kind": "linux", "image": options.neighbour_image},
                        report=report)
    placeholders.append(name)
    for alias in (remote_sys, ident):
        _add_alias(aliases, alias, name)
    return name


def _add_alias(aliases: dict, alias, name: str) -> None:
    """Register a hostname/FQDN (and its short name) for LLDP lookups."""
    alias = str(alias or "").strip().lower()
    for key in (alias, alias.split(".")[0]):
        if key:
            aliases.setdefault(key, name)


def _node_config(node: _Node, options: ExportOptions, warnings: list) -> Optional[str]:
    text = node.device.config
    if options.sanitise is not None:
        text, rep = sanitise_config(text, node.device.spec["platform"], options.sanitise)
        node.report["sanitised"] = rep.as_dict()
        if text is None:
            warnings.append(f"{node.name}: config not saved, sanitising is not supported "
                            f"for platform '{node.device.spec['platform']}'")
            return None
    if options.map_interfaces:
        raw = list(node.link_ifaces)
        text = ifmap.rewrite_config(text, node.imap, raw_names=raw)
    return text


def _unique(name: str, taken) -> str:
    if name not in taken:
        return name
    i = 2
    while f"{name}-{i}" in taken:
        i += 1
    return f"{name}-{i}"


def _node_name(name: str) -> str:
    """Make a device hostname safe for use as a containerlab node name.

    Node names start with a letter or digit and hold only letters, digits,
    ``_`` and ``-``.
    """
    return re.sub(r"[^A-Za-z0-9_-]", "-", name.split(".")[0]).lstrip("_-") or "node"


# --- Writing ------------------------------------------------------------------

def report_path(output_file: str | Path) -> Path:
    """``lab.clab.yml`` → ``lab.import-report.yaml`` next to it."""
    output = Path(output_file)
    stem = output.name
    for suffix in (".clab.yml", ".clab.yaml", ".yml", ".yaml"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return output.with_name(f"{stem}.import-report.yaml")


def load_report(output_file: str | Path) -> dict:
    """The previous import report next to ``output_file`` ({} if none)."""
    path = report_path(output_file)
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def pinned_from_report(report: dict) -> dict[str, dict[str, str]]:
    return {name: dict(entry.get("interfaces") or {})
            for name, entry in (report.get("nodes") or {}).items()
            if isinstance(entry, dict)}


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path``, readable and writable by its owner only (0600)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        os.fchmod(fh.fileno(), 0o600)    # an existing file keeps its mode otherwise
        fh.write(text)


def write_configs(result: ImportResult, base_dir: Path, nodes=None) -> None:
    cfg_dir = base_dir / CONFIG_DIR
    for name, text in result.configs.items():
        if nodes is not None and name not in nodes:
            continue
        if not cfg_dir.is_dir():
            cfg_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        cfg_dir.chmod(0o700)
        write_private(cfg_dir / f"{name}.cfg", text)


def write_report(result: ImportResult, output_file: str | Path) -> Path:
    path = report_path(output_file)
    write_private(path,
                  "# Written by clabfleet export-live: interface renames (original: "
                  "endpoint),\n# image choice and sanitising per node. Re-syncs reuse the "
                  "interface map.\n" + dump_yaml(result.report))
    return path


def write_result(result: ImportResult, output_file: str | Path) -> None:
    """Write the topology, its configs and the import report."""
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_configs(result, output_path.parent)
    n_nodes = len(result.topology["topology"]["nodes"])
    header = (
        f"# Imported from live network ({n_nodes} nodes) by clabfleet.\n"
        f"# Interface renames are listed in {report_path(output_path).name}.\n"
    )
    # 0600 too: with inline configs, or unsanitised ones, it holds secrets
    write_private(output_path, header + dump_yaml(result.topology))
    write_report(result, output_path)
    logger.info("Live network topology exported to %s", output_path)
