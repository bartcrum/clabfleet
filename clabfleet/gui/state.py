"""State behind the GUI: workspace topologies, running labs, and jobs.

Everything here is synchronous; the web server calls it from worker threads.
"""

import copy
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..capture import sweep_helpers
from ..cluster import ClusterConfig, HostInfo, containerlab_version, create_runner, probe_host_resources
from ..deployer import LabDeployer, read_placement_record
from ..livestate import LiveCache, link_states, probe_ifaces, probe_stats
from ..nodes import InspectError, access_modes, inspect_all, parse_inspect
from ..readiness import ReadinessCache, check_ready
from ..execute import ssh_exec
from ..routing import routing_view
from ..routing.live import collect as collect_protocols, overlay as protocol_overlay
from ..snapshots import Snapshotter, diff_lab
from ..validate import validate_text
from .editing import EditConflict, apply_graph, set_positions, text_hash, write_if_unchanged
from ..runner import Runner
from ..topology import (
    LABEL_HOST,
    LABEL_HOST_TAGS,
    SPECIAL_ENDPOINT_NODES,
    Topology,
    dump_yaml,
    load_topology,
    topology_from_dict,
)

logger = logging.getLogger(__name__)

TOPOLOGY_SUFFIXES = (".clab.yml", ".clab.yaml")
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox"}
MAX_SCAN_DEPTH = 5
GUI_PROBE_TIMEOUT = 5  # seconds; the GUI re-probes not-ready nodes anyway
LIVE_INTERVAL = 5.0  # seconds between live link/CPU refreshes of a lab being viewed
PROTOCOL_INTERVAL = 10.0   # seconds between protocol state probes of an open lab
PROTOCOL_SSH_TIMEOUT = 10  # per SSH command on VM-based nodes
RUNTIME_TTL = 2.0  # seconds a `containerlab inspect` result is reused for

# ANSI escape sequences (colors, bold) in containerlab output
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# ----------------------------------------------------------------------
# Workspace topologies
# ----------------------------------------------------------------------

def find_topologies(roots: list[Path]) -> dict[str, Path]:
    """Find containerlab topology files under the workspace roots.

    Returns {id: path}; the id is the path relative to its root (prefixed
    with the root name when there are several roots) and is what the
    browser uses to refer to a topology. Symlinks that lead out of the
    root are left out: the GUI shows the file to viewers.
    """
    found: dict[str, Path] = {}
    for root in roots:
        root = root.resolve()
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            depth = len(Path(dirpath).parts) - base_depth
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in SKIP_DIRS and not d.startswith("clab-")
                and not d.startswith(".") and depth < MAX_SCAN_DEPTH
            )
            for fn in sorted(filenames):
                if fn.endswith(TOPOLOGY_SUFFIXES):
                    path = Path(dirpath) / fn
                    if not path.resolve().is_relative_to(root):
                        logger.warning("Ignoring %s: it links outside %s", path, root)
                        continue
                    rel = path.relative_to(root).as_posix()
                    topo_id = f"{root.name}/{rel}" if len(roots) > 1 else rel
                    found[topo_id] = path
    return found


def topology_view(topo: Topology) -> dict:
    """Nodes and links in a shape the diagram can draw."""
    nodes = []
    for name in topo.nodes:
        eff = topo.effective_node(name)
        labels = eff["labels"]
        pos = None
        if "graph-posX" in labels and "graph-posY" in labels:
            try:
                pos = [float(labels["graph-posX"]), float(labels["graph-posY"])]
            except ValueError:
                pass
        nodes.append({
            "name": name,
            "kind": eff["kind"],
            "image": eff.get("image", ""),
            "type": eff.get("type", ""),
            "group": eff.get("group", ""),
            "host_pin": labels.get(LABEL_HOST, ""),
            "host_tags": labels.get(LABEL_HOST_TAGS, ""),
            "pos": pos,
            "modes": access_modes(eff["kind"]),
        })

    links = []
    for link in topo.links:
        raw = link.raw
        link_type = raw.get("type", "veth")
        ends: list[dict] = []
        if "endpoints" in raw and raw.get("type") is None:
            for ep in raw["endpoints"]:
                node, _, iface = str(ep).partition(":")
                if node in SPECIAL_ENDPOINT_NODES:
                    ends.append({"special": node, "iface": iface})
                else:
                    ends.append({"node": node, "iface": iface})
        elif "endpoints" in raw:
            ends = [{"node": ep["node"], "iface": ep["interface"]} for ep in raw["endpoints"]]
        else:
            ep = raw.get("endpoint") or {}
            ends.append({"node": ep.get("node"), "iface": ep.get("interface", "")})
            far = raw.get("host-interface") or raw.get("remote") or ""
            ends.append({"special": link_type, "iface": str(far)})
        if len(ends) == 2:
            links.append({"id": link.link_id, "type": link_type, "a": ends[0], "b": ends[1]})
    return {"name": topo.name, "nodes": nodes, "links": links}


# ----------------------------------------------------------------------
# Running labs
# ----------------------------------------------------------------------

class UnloadableTopology(ValueError):
    """Editor text that is not a loadable topology; carries the validation report."""

    def __init__(self, report: dict):
        super().__init__("; ".join(report["errors"]) or "not a valid topology")
        self.report = report


@dataclass
class HostState:
    name: str
    ok: bool = False
    error: str = ""
    containers: list[dict] = field(default_factory=list)


class _Flight(Future):
    """One running inspect that concurrent ``runtime()`` callers share."""

    def __init__(self, generation: int):
        super().__init__()
        self.generation = generation


class Workspace:
    """Hosts + topology roots the GUI manages, with cached runners."""

    def __init__(self, cluster: ClusterConfig, roots: list[Path]):
        self.cluster = cluster
        self.roots = roots
        self._runners: dict[str, Runner] = {}
        self._runner_lock = threading.Lock()
        self._topologies: dict[str, Path] = {}
        self.readiness = ReadinessCache()
        self._probes = ThreadPoolExecutor(max_workers=8, thread_name_prefix="readiness")
        self.live = LiveCache(LIVE_INTERVAL)
        self.protocols = LiveCache(PROTOCOL_INTERVAL)
        self._live_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="live")
        self._last_runtime: Optional[tuple[float, list[HostState]]] = None
        self._runtime_lock = threading.Lock()
        self._runtime_flight: Optional[_Flight] = None  # the inspect running now
        self._runtime_generation = 0

    @property
    def multi_host(self) -> bool:
        return len(self.cluster.hosts) > 1

    def host(self, name: str) -> HostInfo:
        for h in self.cluster.hosts:
            if h.name == name:
                return h
        raise KeyError(f"Unknown host '{name}'")

    def runner(self, host: HostInfo) -> Runner:
        with self._runner_lock:
            if host.name not in self._runners:
                runner = create_runner(host)
                runner.interactive_sudo = False
                self._runners[host.name] = runner
            return self._runners[host.name]

    def drop_runner(self, host: HostInfo) -> None:
        with self._runner_lock:
            runner = self._runners.pop(host.name, None)
        if runner:
            runner.close()

    def close(self) -> None:
        self._probes.shutdown(wait=False, cancel_futures=True)
        self._live_pool.shutdown(wait=False, cancel_futures=True)
        with self._runner_lock:
            for runner in self._runners.values():
                runner.close()
            self._runners.clear()

    # --- topologies ---

    def topologies(self) -> list[dict]:
        self._topologies = find_topologies(self.roots)
        result = []
        for topo_id, path in self._topologies.items():
            entry = {"id": topo_id, "path": str(path)}
            try:
                topo = load_topology(path)
                entry.update(name=topo.name, nodes=len(topo.nodes))
            except Exception as exc:
                entry.update(name=path.name, error=str(exc))
            result.append(entry)
        return result

    def topology_path(self, topo_id: str) -> Path:
        if topo_id not in self._topologies:
            self._topologies = find_topologies(self.roots)
        if topo_id not in self._topologies:
            raise KeyError(f"Unknown topology '{topo_id}'")
        path = self._topologies[topo_id]
        # Again now: it may have been replaced by a symlink since the scan
        if not any(path.resolve().is_relative_to(root.resolve()) for root in self.roots):
            raise KeyError(f"Topology '{topo_id}' links outside the workspace")
        return path

    def workspace_labs(self) -> set[str]:
        """Names of the labs the workspace's topologies define."""
        self._topologies = find_topologies(self.roots)
        labs = set()
        for path in self._topologies.values():
            try:
                labs.add(load_topology(path).name)
            except Exception:  # noqa: BLE001 - an invalid file defines no lab
                continue
        return labs

    def topology_detail(self, topo_id: str) -> dict:
        path = self.topology_path(topo_id)
        text = path.read_text()
        detail = {"id": topo_id, "path": str(path), "yaml": text, "hash": text_hash(text)}
        try:
            topo = load_topology(path)
            detail.update(topology_view(topo))
            # Where the last deploy put each node (None if not deployed)
            detail["placement"] = read_placement_record(topo)
        except Exception as exc:
            detail["error"] = str(exc)
        return detail

    def routing(self, topo_id: str) -> dict:
        """Intended OSPF / BGP / EVPN views of a topology (from its configs)."""
        path = self.topology_path(topo_id)
        try:
            return routing_view(load_topology(path))
        except Exception as exc:
            return {"error": str(exc)}

    def routing_live(self, topo_id: str) -> dict:
        """The last live protocol state of a topology's lab, without waiting.

        Like ``live_state``: asking starts a background refresh when the
        snapshot is older than ``PROTOCOL_INTERVAL``.
        """
        path = self.topology_path(topo_id)
        snapshot, due = self.protocols.get(topo_id)
        if due and self.protocols.claim(topo_id):
            try:
                self._live_pool.submit(self._refresh_protocols, topo_id, path)
            except RuntimeError:  # shutting down
                self.protocols.release(topo_id)
        result = dict(snapshot) if snapshot else {"updated": None}
        result["refreshing"] = self.protocols.in_flight(topo_id)
        result["interval"] = self.protocols.interval
        return result

    def _refresh_protocols(self, topo_id: str, path: Path) -> None:
        try:
            snapshot = self.collect_protocols(path)
        except Exception as exc:  # noqa: BLE001 - shown in the GUI, retried next interval
            logger.debug("Protocol state of %s failed: %s", topo_id, exc)
            snapshot = {"updated": time.time(), "error": str(exc)}
        self.protocols.store(topo_id, snapshot)

    def collect_protocols(self, path: Path) -> dict:
        """Ask a lab's running nodes for their OSPF / BGP / EVPN state now."""
        topo = load_topology(path)
        view = routing_view(topo)
        containers, errors = {}, {}
        for host_state in self._recent_runtime():
            if not host_state.ok:
                errors[host_state.name] = host_state.error or "unreachable"
            for c in host_state.containers:
                if c["lab"] == topo.name:
                    containers[c["node"]] = c

        def node_io(c):
            host = self.host(c["host"])
            runner = self.runner(host)
            return runner, host.sudo, lambda cmd: ssh_exec(runner, c["kind"], c["ipv4"], cmd,
                                                           timeout=PROTOCOL_SSH_TIMEOUT)

        states = collect_protocols(view, containers, node_io)
        result = protocol_overlay(view, states, {n: c["state"] == "running"
                                                 for n, c in containers.items()})
        result["errors"] = errors
        return result

    def node_diff(self, topo_id: str, node: str, against: str) -> dict:
        """One node's config in the latest snapshot against ``previous`` or ``startup``."""
        if against not in ("previous", "startup"):
            raise ValueError("against must be 'previous' or 'startup'")
        out = diff_lab(self.topology_path(topo_id), nodes=[node], against=against)
        return {**out, **out["nodes"][0]}

    # --- editing ---

    def validate_yaml(self, topo_id: str, text: str) -> dict:
        """Check unsaved YAML for a topology (relative files resolve next to it)."""
        path = self.topology_path(topo_id)
        # Placement labels only mean something with several hosts
        report = validate_text(text, path.parent, self.cluster if self.multi_host else None,
                               name=topo_id)
        return {"ok": report.ok, "loadable": report.topology is not None,
                "errors": report.errors, "warnings": report.warnings,
                "summary": report.summary()}

    def save_yaml(self, topo_id: str, text: str, base_hash: str) -> dict:
        """Save editor text. Refuses text that is not a loadable topology
        (ValueError with the report) or a file changed since ``base_hash``."""
        path = self.topology_path(topo_id)
        report = self.validate_yaml(topo_id, text)
        if not report["loadable"]:
            raise UnloadableTopology(report)
        write_if_unchanged(path, text, base_hash)
        return {"detail": self.topology_detail(topo_id), "validation": report}

    def save_positions(self, topo_id: str, positions: dict, base_hash: str) -> dict:
        """Write node positions into the file as graph-posX/graph-posY labels."""
        path = self.topology_path(topo_id)
        clean: dict[str, tuple[float, float]] = {}
        for name, xy in (positions or {}).items():
            try:
                x, y = (float(v) for v in xy)
            except (TypeError, ValueError):
                raise ValueError(f"Bad position for node '{name}'") from None
            if not (math.isfinite(x) and math.isfinite(y)):
                raise ValueError(f"Bad position for node '{name}'")
            clean[str(name)] = (x, y)
        text = path.read_text()
        if base_hash and text_hash(text) != base_hash:
            raise EditConflict(f"{path.name} changed on disk; reload it before saving the layout")
        write_if_unchanged(path, set_positions(text, clean), text_hash(text))
        return self.topology_detail(topo_id)

    def apply_graph(self, topo_id: str, graph: dict, base_hash: str,
                    dry_run: bool = False) -> dict:
        """Change the file to match a graph drawn in the builder (see
        ``editing.apply_graph``). ``dry_run``: only return the new YAML and
        its validation."""
        path = self.topology_path(topo_id)
        text = path.read_text()
        if base_hash and text_hash(text) != base_hash:
            raise EditConflict(f"{path.name} changed on disk; reload it before saving")
        from ..templates import KINDS

        new = apply_graph(text, graph, {k: s.image for k, s in KINDS.items()})
        if dry_run:
            return {"yaml": new, "validation": self.validate_yaml(topo_id, new)}
        return self.save_yaml(topo_id, new, text_hash(text))

    def create_topology(self, file_name: str, lab_name: str, kind: str,
                        template: Optional[str] = None, params: Optional[dict] = None) -> str:
        """Write a new topology file in the first workspace directory: from
        a ``clabfleet new`` template, or one node of ``kind`` to build on.
        Returns its id. Never overwrites a file."""
        from ..templates import KINDS, generate, render

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", file_name or ""):
            raise ValueError("File names are letters, digits, '.', '_' and '-'")
        if not file_name.endswith((".clab.yml", ".clab.yaml")):
            file_name += ".clab.yml"
        if kind not in KINDS:
            raise ValueError(f"Unsupported kind '{kind}' (choose from {', '.join(KINDS)})")
        if template:
            text = render(generate(template, params, kind, name=lab_name), template,
                          params or {}, kind)
        else:
            data = {"name": lab_name, "topology": {
                "kinds": {kind: {"image": KINDS[kind].image}},
                "nodes": {"R1" if kind != "linux" else "Host-1": {"kind": kind}},
                "links": [],
            }}
            topology_from_dict(data)  # checks the lab name
            text = (f"# {lab_name}: drawn in the clabfleet GUI builder.\n"
                    "#\n# Deploy:\n#   clabfleet deploy <this file>\n\n" + dump_yaml(data))
        path = self.roots[0] / file_name
        with open(path, "x") as fh:  # x: refuses an existing file, even one just created
            fh.write(text)
        self._topologies = find_topologies(self.roots)
        for topo_id, p in self._topologies.items():
            if p.resolve() == path.resolve():
                return topo_id
        raise KeyError(f"{path} is not in the workspace")  # e.g. a hidden directory

    # --- runtime ---

    def runtime(self, max_age: float = RUNTIME_TTL) -> list[HostState]:
        """Containers running on each host (one `containerlab inspect` per host).

        Reuses a result younger than ``max_age`` seconds, and callers that
        arrive while an inspect is running wait for it instead of starting
        their own, so many browsers polling cost one inspect per host.
        """
        with self._runtime_lock:
            last = self._last_runtime
            if last and max_age > 0 and time.monotonic() - last[0] < max_age:
                return last[1]
            flight = self._runtime_flight
            leader = flight is None
            if leader:
                flight = self._runtime_flight = _Flight(self._runtime_generation)
        if not leader:
            return flight.result()
        try:
            states = self._inspect_runtime()
        except BaseException as exc:
            flight.set_exception(exc)
            raise
        finally:
            with self._runtime_lock:
                if self._runtime_flight is flight:
                    self._runtime_flight = None
        with self._runtime_lock:
            if flight.generation == self._runtime_generation:  # not invalidated meanwhile
                self._last_runtime = (time.monotonic(), states)
        flight.set_result(states)
        return states

    def invalidate_runtime(self) -> None:
        """Forget the cached runtime (labs changed): the next caller inspects again."""
        with self._runtime_lock:
            self._last_runtime = None
            self._runtime_flight = None
            self._runtime_generation += 1

    def _inspect_runtime(self) -> list[HostState]:
        states = []
        live: set[tuple] = set()
        for host in self.cluster.hosts:
            state = HostState(host.name)
            try:
                state.containers = parse_inspect(inspect_all(self.runner(host)), host.name)
                state.ok = True
            except InspectError as exc:
                state.error = str(exc)
            except Exception as exc:
                state.error = str(exc)
                self.drop_runner(host)  # reconnect next time
            self._annotate_ready(host, state.containers, live)
            states.append(state)
        self.readiness.prune(live)
        return states

    def _annotate_ready(self, host: HostInfo, containers: list[dict], live: set) -> None:
        """Set ``ready`` (True/False, None = not known yet) on each container.

        Probes run in the background so a slow node never delays the page.
        """
        for c in containers:
            key = (host.name, c["id"] or c["container"])
            live.add(key)
            if c["state"] != "running":
                c["ready"], c["ready_detail"] = False, c["state"] or "not running"
                continue
            last, due = self.readiness.get(key)
            c["ready"], c["ready_detail"] = last if last else (None, "checking")
            if due and self.readiness.claim(key):
                try:
                    self._probes.submit(self._probe_ready, key, host, dict(c))
                except RuntimeError:  # shutting down
                    self.readiness.release(key)

    def _probe_ready(self, key: tuple, host: HostInfo, container: dict) -> None:
        try:
            ok, detail = check_ready(self.runner(host), container, host.sudo,
                                     timeout=GUI_PROBE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - try again on the next refresh
            logger.debug("Readiness probe of %s failed: %s", container["container"], exc)
            self.readiness.release(key)
            return
        self.readiness.store(key, ok, detail)

    # --- live link and resource state ---

    def live_state(self, topo_id: str) -> dict:
        """The last live snapshot of a topology's lab, without waiting.

        Asking for it starts a background refresh when the snapshot is
        older than ``LIVE_INTERVAL``, so labs nobody looks at are not probed.
        """
        path = self.topology_path(topo_id)
        snapshot, due = self.live.get(topo_id)
        if due and self.live.claim(topo_id):
            try:
                self._live_pool.submit(self._refresh_live, topo_id, path)
            except RuntimeError:  # shutting down
                self.live.release(topo_id)
        result = dict(snapshot) if snapshot else {"updated": None, "nodes": {}, "links": {},
                                                  "errors": {}}
        result["refreshing"] = self.live.in_flight(topo_id)
        result["interval"] = self.live.interval
        return result

    def _refresh_live(self, topo_id: str, path: Path) -> None:
        try:
            snapshot = self.collect_live(path)
        except Exception as exc:  # noqa: BLE001 - shown in the GUI, retried next interval
            logger.debug("Live state of %s failed: %s", topo_id, exc)
            snapshot = {"updated": time.time(), "nodes": {}, "links": {},
                        "errors": {"": str(exc)}}
        self.live.store(topo_id, snapshot)

    def _recent_runtime(self) -> list[HostState]:
        """Containers per host, reusing the last inspect if it is recent."""
        last = self._last_runtime
        if last and time.monotonic() - last[0] < 2 * self.live.interval:
            return last[1]
        return self.runtime()

    def collect_live(self, path: Path) -> dict:
        """Probe a lab's running nodes now: interface states and CPU/memory.

        One ``docker exec`` per running node and one ``docker stats`` per
        host. A host or node that cannot be probed leaves its values unset.
        """
        topo = load_topology(path)
        view = topology_view(topo)
        errors: dict[str, str] = {}
        containers = {}
        for host_state in self._recent_runtime():
            if not host_state.ok:
                errors[host_state.name] = host_state.error or "unreachable"
            for c in host_state.containers:
                if c["lab"] == topo.name:
                    containers[c["node"]] = c
        running = [c for c in containers.values() if c["state"] == "running"]
        by_host: dict[str, list[str]] = {}
        for c in running:
            by_host.setdefault(c["host"], []).append(c["container"])

        stats: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="live-probe") as pool:
            iface_jobs = {c["node"]: pool.submit(self._probe_ifaces, c) for c in running}
            stats_jobs = {
                name: pool.submit(probe_stats, self.runner(self.host(name)), names,
                                  self.host(name).sudo)
                for name, names in by_host.items()
            }
            for name, job in stats_jobs.items():
                try:
                    stats.update(job.result())
                except Exception as exc:  # noqa: BLE001
                    errors[name] = f"docker stats: {exc}"
            ifaces = {node: job.result() for node, job in iface_jobs.items()}

        nodes, link_input = {}, {}
        for n in view["nodes"]:
            c = containers.get(n["name"])
            entry = {"state": c["state"] if c else "", "host": c["host"] if c else ""}
            entry.update(stats.get(c["container"], {}) if c else {})
            nodes[n["name"]] = entry
            link_input[n["name"]] = {"kind": n["kind"], "state": entry["state"],
                                     "ifaces": ifaces.get(n["name"])}
        return {"updated": time.time(), "nodes": nodes,
                "links": link_states(view["links"], link_input), "errors": errors}

    def _probe_ifaces(self, container: dict) -> Optional[dict]:
        try:
            host = self.host(container["host"])
            return probe_ifaces(self.runner(host), container["container"], host.sudo)
        except Exception as exc:  # noqa: BLE001 - reported as unknown
            logger.debug("Interface probe of %s failed: %s", container["container"], exc)
            return None

    def find_node(self, lab: str, node: str) -> dict:
        # Only labs of this workspace: viewers may follow node logs, and the
        # hosts can run other people's labs too
        if lab not in self.workspace_labs():
            raise KeyError(f"Lab '{lab}' is not in this workspace")
        for state in self.runtime():
            for c in state.containers:
                if c["lab"] == lab and c["node"] == node:
                    return c
        raise KeyError(f"Node '{node}' of lab '{lab}' is not running")

    def sweep_capture_helpers(self) -> None:
        """Remove capture helper containers left behind on any host (start-up)."""
        for host in self.cluster.hosts:
            try:
                removed = sweep_helpers(self.runner(host), host.sudo)
            except Exception as exc:  # noqa: BLE001 - best effort, tried at every start
                logger.debug("Capture helper sweep on %s failed: %s", host.name, exc)
                continue
            if removed:
                logger.warning("Removed %d leftover capture helper(s) on %s: %s",
                               len(removed), host.name, ", ".join(removed))

    def host_status(self) -> list[dict]:
        result = []
        for host in copy.deepcopy(self.cluster.hosts):
            entry = {"name": host.name, "host": host.host, "local": host.is_local}
            try:
                runner = self.runner(self.host(host.name))
                facts = probe_host_resources(runner, host)
                entry.update(
                    ok=True,
                    version=containerlab_version(runner),
                    cpus=facts.get("cpus"),
                    mem_total_mb=facts.get("MemTotal_mb"),
                    mem_available_mb=facts.get("MemAvailable_mb"),
                    vtep=host.vtep,
                    tags=host.tags,
                )
            except Exception as exc:
                entry.update(ok=False, error=str(exc))
                self.drop_runner(self.host(host.name))
            result.append(entry)
        return result


# ----------------------------------------------------------------------
# Jobs (deploy / destroy / save / snapshot): one per lab at a time, several labs at once
# ----------------------------------------------------------------------

@dataclass
class Job:
    id: str
    action: str
    topology: str
    lab: str = ""
    status: str = "running"  # running | ok | error | interrupted
    lines: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: Optional[float] = None
    result: Optional[dict] = None
    options: dict = field(default_factory=dict)
    user: str = ""  # who started it (multi-user GUI)

    def add(self, line: str) -> None:
        self.lines.append(ANSI_RE.sub("", line))

    def add_outcome(self, result: dict) -> None:
        """Spell out a partial or rolled-back deploy."""
        hosts = result.get("hosts", {})
        ok = [h for h, r in hosts.items() if "error" not in r]
        failed = [h for h, r in hosts.items() if "error" in r]
        status = result.get("status")
        if status == "partial":
            self.add(f"✗ partly deployed: running on {', '.join(ok)}, failed on "
                     f"{', '.join(failed)}. Destroy the lab to clean up.")
        elif status == "rolled-back":
            self.add(f"↺ rolled back: removed from {', '.join(result['rollback'])}")
        elif status == "rollback-failed":
            bad = [f"{h} ({r})" for h, r in result["rollback"].items() if r != "ok"]
            self.add(f"✗ rollback incomplete on {', '.join(bad)}. Destroy the lab to clean up.")

    def host_times(self) -> dict[str, float]:
        """Seconds each host took, from the result (empty while running)."""
        hosts = (self.result or {}).get("hosts", {})
        return {h: r["seconds"] for h, r in hosts.items() if isinstance(r, dict) and "seconds" in r}

    def summary(self) -> dict:
        """The job without its output lines (for job lists)."""
        return {
            "id": self.id, "action": self.action, "topology": self.topology, "lab": self.lab,
            "status": self.status, "started": self.started, "finished": self.finished,
            "options": self.options, "user": self.user, "host_times": self.host_times(),
            "line_count": len(self.lines),
        }

    def view(self, offset: int = 0) -> dict:
        return {**self.summary(), "lines": self.lines[offset:], "offset": len(self.lines),
                "result": self.result}

    def to_dict(self) -> dict:
        return {**self.view(0), "lines": self.lines}

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        return cls(
            id=str(data["id"]), action=data.get("action", ""), topology=data.get("topology", ""),
            lab=data.get("lab", ""), status=data.get("status", "error"),
            lines=list(data.get("lines") or []), started=data.get("started") or 0,
            finished=data.get("finished"), result=data.get("result"),
            options=data.get("options") or {}, user=data.get("user") or "",
        )


class JobHistory:
    """Finished and running jobs as JSON files in ``<workspace>/.clabfleet/jobs``.

    Keeps the newest ``keep`` jobs. Without a writable directory, history
    simply is not kept. Job output can show configs, so the directory is
    0700 and the files 0600.
    """

    def __init__(self, directory: Optional[Path], keep: int = 50):
        self.directory = directory
        self.keep = keep

    def load(self) -> list[Job]:
        if not self.directory or not self.directory.is_dir():
            return []
        jobs = []
        for path in self.directory.glob("*.json"):
            if path.is_symlink():
                continue
            try:
                job = Job.from_dict(json.loads(path.read_text()))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                logger.warning("Skipping unreadable job file %s: %s", path, exc)
                continue
            if job.status == "running":  # the GUI stopped while it ran
                job.status = "interrupted"
                job.add("✗ interrupted: the GUI stopped while this job was running")
            jobs.append(job)
        jobs.sort(key=lambda j: j.started)
        return jobs[-self.keep:]

    def save(self, job: Job) -> None:
        if not self.directory:
            return
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.directory.stat().st_mode & 0o077:
                self.directory.chmod(0o700)
            # A fresh name each time, so nothing in the directory can redirect the write
            fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=f".{job.id}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(json.dumps(job.to_dict()))
                os.replace(tmp, self.directory / f"{job.id}.json")
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            self._prune()
        except OSError as exc:
            logger.warning("Could not save job history in %s: %s", self.directory, exc)

    def _prune(self) -> None:
        files = sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old in files[:-self.keep]:
            old.unlink(missing_ok=True)


class _JobLogHandler(logging.Handler):
    """Copies clabfleet log records from the job's thread into the job."""

    def __init__(self, job: Job, thread_id: int):
        super().__init__(logging.INFO)
        self.job = job
        self.thread_id = thread_id

    def emit(self, record):
        if record.thread == self.thread_id and not record.name.startswith("clabfleet.gui"):
            self.job.add(f"» {record.getMessage()}")


class JobManager:
    ACTIONS = {"deploy", "redeploy", "destroy", "save", "snapshot"}
    OPTIONS = {"rollback"}  # deploy/redeploy only
    MAX_RUNNING = 4

    def __init__(self, workspace: Workspace, history: Optional[JobHistory] = None):
        self.workspace = workspace
        if history is None:
            root = workspace.roots[0] if workspace.roots else None
            history = JobHistory(root / ".clabfleet" / "jobs" if root else None)
        self.history = history
        self.jobs: dict[str, Job] = {j.id: j for j in history.load()}
        self._lock = threading.Lock()
        # Called (in the job's thread) when a job finishes, e.g. for the audit log
        self.on_finished: Optional[Callable[[Job], None]] = None

    def running(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.status == "running"]

    def recent(self, limit: int = 30) -> list[Job]:
        """Newest first."""
        return sorted(self.jobs.values(), key=lambda j: j.started, reverse=True)[:limit]

    def start(self, action: str, topo_id: str, options: Optional[dict] = None,
              user: str = "") -> Job:
        if action not in self.ACTIONS:
            raise ValueError(f"Unknown action '{action}'")
        options = {k: bool(v) for k, v in (options or {}).items() if k in self.OPTIONS}
        path = self.workspace.topology_path(topo_id)
        try:
            lab = load_topology(path).name
        except Exception:  # invalid file: the job itself will report why
            lab = topo_id
        with self._lock:
            running = self.running()
            for other in running:
                if other.topology == topo_id or other.lab == lab:
                    raise RuntimeError(
                        f"A job is already running for lab '{lab}' ({other.action})"
                    )
            if len(running) >= self.MAX_RUNNING:
                raise RuntimeError(
                    f"{len(running)} jobs are already running; wait for one to finish"
                )
            job = Job(id=uuid.uuid4().hex[:12], action=action, topology=topo_id,
                      lab=lab, options=options, user=user)
            self.jobs[job.id] = job
        self.history.save(job)
        threading.Thread(target=self._run, args=(job, path), daemon=True).start()
        return job

    def _run(self, job: Job, path: Path) -> None:
        handler = _JobLogHandler(job, threading.get_ident())
        pkg_logger = logging.getLogger("clabfleet")
        pkg_logger.addHandler(handler)
        if pkg_logger.getEffectiveLevel() > logging.INFO:
            pkg_logger.setLevel(logging.INFO)
        try:
            # Fresh copy: placement reserves resources on the HostInfo objects
            deployer = LabDeployer(
                copy.deepcopy(self.workspace.cluster),
                on_output=job.add,
                interactive_sudo=False,
            )
            job.add(f"$ {job.action} {job.topology}")
            if job.action in ("deploy", "redeploy"):
                result = deployer.deploy(path, reconfigure=job.action == "redeploy",
                                         rollback=job.options.get("rollback", False))
            elif job.action == "destroy":
                result = deployer.destroy(path)
            elif job.action == "snapshot":
                result = Snapshotter(copy.deepcopy(self.workspace.cluster), on_output=job.add,
                                     interactive_sudo=False).take(path)
                job.add(f"» snapshot {result['snapshot']}: {result['path']}")
            else:
                result = deployer.save(path)
            job.result = result
            errors = {h: r["error"] for h, r in result.get("hosts", {}).items() if "error" in r}
            for host, err in errors.items():
                job.add(f"✗ {host}: {err}")
            job.add_outcome(result)
            job.status = "error" if errors else "ok"
        except Exception as exc:
            logger.exception("Job %s failed", job.id)
            job.add(f"✗ {exc}")
            job.status = "error"
        finally:
            pkg_logger.removeHandler(handler)
            job.finished = time.time()
            times = job.host_times()
            if times:
                job.add("» time per host: " + ", ".join(f"{h} {t:g}s" for h, t in times.items()))
            job.add("✓ done" if job.status == "ok" else "✗ failed")
            self.workspace.invalidate_runtime()  # the lab's containers changed
            self.history.save(job)
            if self.on_finished:
                try:
                    self.on_finished(job)
                except Exception:  # noqa: BLE001 - never let a hook break a job
                    logger.exception("Job finished hook failed")
