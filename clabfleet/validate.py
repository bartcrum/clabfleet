"""Check a topology (and optionally a cluster inventory) without touching any host."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .cluster import ClusterConfig
from .nodes import known_kinds
from .topology import LABEL_HOST, Topology, load_topology, topology_from_dict

# Properties containerlab reads at deploy time; a missing file fails the deploy.
# A missing bind source is only a warning: some setups create it first.
REQUIRED_FILE_PROPS = {"startup-config", "license", "env-files"}


@dataclass
class ValidationReport:
    path: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    topology: Optional[Topology] = None

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        if self.topology is None:
            return "invalid"
        counts = (f"{_plural(len(self.topology.nodes), 'node')}, "
                  f"{_plural(len(self.topology.links), 'link')}")
        if not self.errors and not self.warnings:
            return f"ok ({counts})"
        parts = []
        if self.errors:
            parts.append(_plural(len(self.errors), "error"))
        if self.warnings:
            parts.append(_plural(len(self.warnings), "warning"))
        return f"{', '.join(parts)} ({counts})"


def validate_topology(
    path: str | Path, cluster: Optional[ClusterConfig] = None
) -> ValidationReport:
    """Load a topology and report problems that would break or degrade a deploy.

    With ``cluster``, placement labels are checked against its hosts too.
    """
    report = ValidationReport(str(path))
    try:
        topo = load_topology(path)
    except Exception as exc:  # noqa: BLE001 - report any load failure as invalid
        report.errors.append(_load_error(exc))
        return report
    return _check(topo, cluster, report)


def validate_text(
    text: str, base_dir: str | Path, cluster: Optional[ClusterConfig] = None,
    name: str = "<editor>",
) -> ValidationReport:
    """Same checks for unsaved YAML (e.g. the GUI editor); files resolve from base_dir."""
    report = ValidationReport(name)
    try:
        data = yaml.safe_load(text)
        topo = topology_from_dict(data, base_dir=base_dir)
    except Exception as exc:  # noqa: BLE001 - report any load failure as invalid
        report.errors.append(_load_error(exc))
        return report
    return _check(topo, cluster, report)


def _load_error(exc: Exception) -> str:
    if isinstance(exc, yaml.MarkedYAMLError) and exc.problem_mark is not None:
        mark = exc.problem_mark
        return f"YAML syntax error at line {mark.line + 1}, column {mark.column + 1}: {exc.problem}"
    if isinstance(exc, KeyError):
        return f"missing key {exc}"
    return str(exc)


def _check(topo: Topology, cluster: Optional[ClusterConfig],
           report: ValidationReport) -> ValidationReport:
    report.topology = topo
    _check_files(topo, report)
    _check_kinds(topo, report)
    _check_interfaces(topo, report)
    if cluster is not None:
        _check_placement_labels(topo, cluster, report)
    return report


def _check_files(topo: Topology, report: ValidationReport) -> None:
    for ref in topo.file_references():
        where = f"node '{ref.node}' {ref.prop} '{ref.path}'"
        if ".." in ref.path.parts:
            report.warnings.append(
                f"{where} is outside the topology directory and is not copied to remote hosts"
            )
        elif not (topo.base_dir / ref.path).exists():
            if ref.prop in REQUIRED_FILE_PROPS:
                report.errors.append(f"{where} does not exist")
            else:
                report.warnings.append(f"{where} does not exist")


def _check_kinds(topo: Topology, report: ValidationReport) -> None:
    known = known_kinds()
    unknown: dict[str, list[str]] = {}
    for name in topo.nodes:
        kind = topo.effective_node(name)["kind"]
        if kind not in known:
            unknown.setdefault(kind, []).append(name)
    for kind, nodes in unknown.items():
        report.warnings.append(
            f"kind '{kind}' ({', '.join(nodes)}) is not known to clabfleet: placement "
            "uses a default 1 vCPU / 512 MB estimate and the GUI offers shell/SSH only"
        )


def _check_interfaces(topo: Topology, report: ValidationReport) -> None:
    seen: dict[tuple[str, str], int] = {}
    for link in topo.links:
        for ep in link.endpoints:
            if not ep.interface:
                continue
            key = (ep.node, ep.interface)
            if key in seen and seen[key] != link.index:
                report.errors.append(
                    f"interface {ep.node}:{ep.interface} is used by link #{seen[key]} "
                    f"and link #{link.index}"
                )
            seen.setdefault(key, link.index)


def _check_placement_labels(
    topo: Topology, cluster: ClusterConfig, report: ValidationReport
) -> None:
    host_names = {h.name for h in cluster.hosts}
    all_tags = {t for h in cluster.hosts for t in h.tags}
    for node in topo.placement_nodes():
        if node["host"] and node["host"] not in host_names:
            report.errors.append(
                f"node '{node['name']}' is pinned ({LABEL_HOST}) to '{node['host']}', "
                f"which is not in the cluster ({', '.join(sorted(host_names))})"
            )
        if node["host_tags"] and not node["host"] and not set(node["host_tags"]) & all_tags:
            report.warnings.append(
                f"node '{node['name']}' prefers host tags {', '.join(node['host_tags'])}, "
                "but no cluster host has any of them (placement falls back to all hosts)"
            )
    if len(cluster.hosts) > 1:
        for host in cluster.hosts:
            if not host.vtep:
                report.warnings.append(
                    f"cluster host '{host.name}' has no vtep_ip: links between it and "
                    "other hosts cannot be created"
                )


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"
