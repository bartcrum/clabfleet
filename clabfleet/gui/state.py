"""State behind the GUI: workspace topologies and running labs.

Everything here is synchronous; the web server calls it from worker threads.
"""

import copy
import ipaddress
import logging
import math
import os
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..capture import sweep_helpers
from ..cluster import ClusterConfig, HostInfo, containerlab_version, create_runner, probe_host_resources
from ..deployer import clab_dir, read_placement_record, runs_in_place
from ..livestate import LiveCache, link_rates, link_states, linux_iface_names, probe_ifaces, probe_stats
from ..nodes import InspectError, access_modes, inspect_all, parse_inspect, run_docker
from ..readiness import ReadinessCache, check_ready
from ..execute import ssh_exec
from ..routing import routing_view
from ..routing.live import collect as collect_protocols, family as cli_family, overlay as protocol_overlay
from ..snapshots import SAVED_CONFIG, diff_lab, startup_config, unified_diff
from ..spare import cable_live, live_problem, next_ports, port_exists, unplug_live
from ..validate import validate_text
from . import annotations
from .editing import (
    EditConflict, add_spare_ports, apply_graph, cable_spare_ports, set_positions, text_hash,
    uncable_ports, write_if_unchanged,
)
from .events import EventLog, link_items, protocol_items
from ..runner import CommandTimeout, Runner, deadline
from ..topology import (
    LABEL_HOST,
    LABEL_HOST_TAGS,
    SPECIAL_ENDPOINT_NODES,
    Topology,
    canonical_kind,
    dump_yaml,
    load_topology,
    topology_from_dict,
)

logger = logging.getLogger(__name__)

LINUX_KINDS = {"linux"}  # kinds whose routes are the kernel's own (no routing daemon)

TOPOLOGY_SUFFIXES = (".clab.yml", ".clab.yaml")
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox"}
MAX_SCAN_DEPTH = 5
GUI_PROBE_TIMEOUT = 5  # seconds; the GUI re-probes not-ready nodes anyway
LIVE_INTERVAL = 5.0  # seconds between live link/CPU refreshes of a lab being viewed
PROTOCOL_INTERVAL = 10.0   # seconds between protocol state probes of an open lab
PROTOCOL_SSH_TIMEOUT = 10  # per SSH command on VM-based nodes
RUNTIME_TTL = 2.0  # seconds a `containerlab inspect` result is reused for

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
    spare = topo.spare_ports()
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
            "config": bool(eff.get("startup-config") or eff.get("exec")),
            # Ports with nothing plugged in (spare.py), and why one of this
            # node's ports could not be cabled while the lab runs, if so
            "spare": spare.get(name, []),
            "cable_live": live_problem(topo, name) is None,
        })

    links = []
    for link in topo.links:
        if link.is_spare:
            continue  # shown as the node's empty ports
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


# What a read-only command on a node may consist of (``Workspace.node_command``)
SAFE_COMMAND = re.compile(r"[A-Za-z0-9 _.:/|-]+")

HOST_POLL_TIMEOUT = 20  # seconds a host gets to answer one of the GUI's polls
HOST_RETRY = 30         # seconds before a host that did not answer is tried again


class HostUnreachable(Exception):
    """A host did not answer lately, and is not asked again just yet."""


SKETCH_MAX_NODES = 60  # larger labs get no thumbnail: too small to read, too much to send


def topology_sketch(topo: Topology) -> Optional[dict]:
    """A topology cut down to what a thumbnail needs: ``nodes`` as
    ``[name, kind, saved position or None]`` and ``links`` as pairs of node
    indexes (links between two nodes only). None for a lab too large or
    too small to be worth drawing."""
    view = topology_view(topo)
    if not 2 <= len(view["nodes"]) <= SKETCH_MAX_NODES:
        return None
    index = {n["name"]: i for i, n in enumerate(view["nodes"])}
    links = [[index[link["a"]["node"]], index[link["b"]["node"]]] for link in view["links"]
             if link["a"].get("node") in index and link["b"].get("node") in index]
    return {"nodes": [[n["name"], n["kind"], n["pos"]] for n in view["nodes"]], "links": links}


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
        # Hosts that did not answer: name -> (when, why, a retry is under way).
        # They are not dialled again on every poll, which would make each one
        # wait for the connection to time out
        self._down: dict[str, tuple[float, str, bool]] = {}
        self._hosts_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="hosts")
        self._topologies: dict[str, Path] = {}
        self.readiness = ReadinessCache()
        self._probes = ThreadPoolExecutor(max_workers=8, thread_name_prefix="readiness")
        self.live = LiveCache(LIVE_INTERVAL)
        self.protocols = LiveCache(PROTOCOL_INTERVAL)
        self.events = EventLog()  # changes between live reads, for the timeline
        self._shut_routes: dict[tuple, tuple[str, list[str]]] = {}  # what-if: routes to put back
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
        """The host's runner. HostUnreachable while the host is known not
        to answer (see ``ask_host``), so nothing waits on it in vain."""
        down = self._down.get(host.name)
        if down:
            raise HostUnreachable(down[1])
        return self._runner(host)

    def ask_host(self, host: HostInfo, ask: Callable[[Runner], Any]) -> Any:
        """``ask(runner)`` within HOST_POLL_TIMEOUT, for the GUI's polls.

        A remote host that does not answer (unreachable, or too slow) is
        marked down: its error is returned at once from then on, to
        everything that wants its runner, and it is tried again in the
        background every HOST_RETRY seconds until it answers. So a dead host
        costs one wait, not one per poll.

        This machine is never marked down: there is no connection to it that
        could be lost, and a slow answer (a busy Docker during a deploy)
        means it is busy, not gone. A poll that runs out of time fails by
        itself, and terminals, live state and the rest go on working."""
        down = self._down.get(host.name)
        if down:
            when, why, retrying = down
            if not retrying and time.monotonic() - when >= HOST_RETRY:
                self._down[host.name] = (when, why, True)
                try:
                    self._hosts_pool.submit(self._retry_host, host)
                except RuntimeError:  # shutting down
                    pass
            raise HostUnreachable(why)
        try:
            with deadline(HOST_POLL_TIMEOUT):
                return ask(self.runner(host))
        except InspectError:
            raise  # the host answered: containerlab had something to say
        except Exception as exc:
            if host.is_local:
                raise HostUnreachable(self._why(exc)) from exc
            raise HostUnreachable(self._mark_down(host, exc)) from exc

    @staticmethod
    def _why(exc: Exception) -> str:
        return (f"no answer within {HOST_POLL_TIMEOUT} seconds" if isinstance(exc, CommandTimeout)
                else str(exc) or type(exc).__name__)

    def _mark_down(self, host: HostInfo, exc: Exception) -> str:
        why = self._why(exc)
        logger.warning("Host %s does not answer (%s); trying again every %ds",
                       host.name, why, HOST_RETRY)
        self._down[host.name] = (time.monotonic(), why, False)
        self.drop_runner(host)
        return why

    def _retry_host(self, host: HostInfo) -> None:
        try:
            with deadline(HOST_POLL_TIMEOUT):
                self._runner(host).run(["true"], sudo=False)
        except Exception as exc:  # noqa: BLE001 - still down
            self._mark_down(host, exc)
        else:
            logger.info("Host %s answers again", host.name)
            self._down.pop(host.name, None)
            self.invalidate_runtime()

    def _runner(self, host: HostInfo) -> Runner:
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
        self._hosts_pool.shutdown(wait=False, cancel_futures=True)
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
                entry.update(name=topo.name, nodes=len(topo.nodes), saved_at=self._saved_at(topo),
                             sketch=topology_sketch(topo))
            except Exception as exc:
                entry.update(name=path.name, error=str(exc))
            result.append(entry)
        return result

    def _saved_at(self, topo) -> Optional[float]:
        """When the lab's kept directory last had a config saved (Stop, or a
        destroy that keeps the lab directory): the next deploy starts from
        those configs. None without such a directory. Known for a lab that
        runs in place on this machine; the directory is also there while the
        lab runs, so this only says something about a lab that does not."""
        host = self.cluster.hosts[0]
        if not runs_in_place(self.cluster, host, topo):
            return None
        kept = Path(clab_dir(self.cluster, host, topo))
        times = []
        for node in topo.nodes:
            # The node's saved config where its kind has one, and its directory
            saved = SAVED_CONFIG.get(canonical_kind(topo.effective_node(node)["kind"] or ""))
            for path in [kept / node] + ([kept / node / saved[0]] if saved else []):
                try:
                    times.append(path.stat().st_mtime)
                except OSError:
                    continue
        return max(times, default=None)

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
        detail = {"id": topo_id, "path": str(path), "yaml": text, "hash": text_hash(text),
                  "annotations": annotations.load(path)}
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
        else:
            try:
                view = routing_view(load_topology(path))
                self.events.observe(topo_id, "protocols", protocol_items(snapshot, view),
                                    snapshot["updated"])
            except Exception as exc:  # noqa: BLE001 - the timeline must not break the probe
                logger.debug("Protocol events of %s: %s", topo_id, exc)
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
        """One node's config in the latest snapshot against ``previous`` or
        ``startup``; or with ``running``, its running config read now against
        its startup-config (for drift, nothing saved first)."""
        if against == "running":
            return self.running_diff(topo_id, node)
        if against not in ("previous", "startup"):
            raise ValueError("against must be 'previous', 'startup' or 'running'")
        out = diff_lab(self.topology_path(topo_id), nodes=[node], against=against)
        return {**out, **out["nodes"][0]}

    def _vteps(self, topo) -> dict[str, list[str]]:
        """VTEP address -> its nodes (both halves of an MLAG pair), from the
        startup configs."""
        evpn = routing_view(topo).get("evpn") or {}
        out: dict[str, list[str]] = {}
        for n, v in sorted((evpn.get("vteps") or {}).items()):
            if v.get("ip"):
                out.setdefault(v["ip"], []).append(n)
        return out

    def evpn_routes(self, topo_id: str, node: str) -> list[dict]:
        """A cEOS VTEP's EVPN routes: hosts and prefixes per VNI, and from
        which VTEP (``routing.evpn_routes``)."""
        from ..routing.evpn_routes import parse_eos_evpn_routes

        topo = load_topology(self.topology_path(topo_id))
        if node not in topo.nodes:
            raise ValueError(f"No node '{node}' in {topo_id}")
        if cli_family(topo.effective_node(node)["kind"] or "") != "eos":
            raise ValueError("EVPN routes can be read from cEOS nodes only")
        ask = lambda cmd: self.node_command(topo.name, node, cmd, shell=False)  # noqa: E731
        return parse_eos_evpn_routes(ask("show bgp evpn route-type mac-ip detail | json"),
                                     ask("show bgp evpn route-type ip-prefix ipv4 detail | json"),
                                     {ip: " + ".join(nodes) for ip, nodes in self._vteps(topo).items()})

    def trace(self, topo_id: str, src: str, dst: str) -> dict:
        """Path trace from node ``src`` to a node or an address
        (``routing.trace``). A node destination is its router-id, or for a
        host its first address outside the management network; an address
        that is a host's is traced to that host."""
        return self.trace_topology(load_topology(self.topology_path(topo_id)), src, dst)

    def trace_topology(self, topo: Topology, src: str, dst: str) -> dict:
        """``trace`` for a topology already loaded (the CLI's, which need
        not be in the workspace)."""
        from ..routing.trace import Lab, TraceError, trace

        if src not in topo.nodes:
            raise ValueError(f"No node '{src}' in {topo.name}")
        dst_node = dst if dst in topo.nodes else None
        if dst_node:
            dst = self._node_address(topo, dst_node)
        kinds = {n: topo.effective_node(n)["kind"] or "" for n in topo.nodes}
        if not dst_node:
            dst_node = self._address_owner(topo, kinds, dst)
        view = routing_view(topo)
        lab = Lab(topology_view(topo), kinds, self._vteps(topo), view.get("port_channels"))
        try:
            result = trace(lab, lambda node, cmd: self.node_command(topo.name, node, cmd), src, dst, dst_node)
        except TraceError as exc:
            raise ValueError(str(exc)) from None
        return {"src": src, "dst": dst, "dst_node": dst_node, **result}

    def _node_address(self, topo, node: str) -> str:
        view = routing_view(topo)
        for proto in ("bgp", "ospf"):
            rid = ((view.get(proto) or {}).get("nodes") or {}).get(node, {}).get("router_id")
            if rid:
                return rid
        addresses = self._host_addresses(topo, node)
        if addresses:
            return addresses[0]
        raise ValueError(f"{node} has no address to trace to; give an IP address")

    def _host_addresses(self, topo, node: str) -> list[str]:
        """A running host's IPv4 addresses outside loopback and management."""
        text = self.node_command(topo.name, node, "ip -o -4 addr show")
        return [parts[3].split("/")[0] for parts in map(str.split, text.splitlines())
                if len(parts) > 3 and parts[1] not in ("lo", "eth0") and parts[2] == "inet"]

    def _address_owner(self, topo, kinds: dict, address: str) -> Optional[str]:
        """The Linux host that has ``address``, if one does. Knowing it, a
        trace can go on to the host from a switch that has no MAC entry for
        it (aged out), as it does for a destination given by name."""
        try:
            address = str(ipaddress.IPv4Address(address))
        except ValueError:
            return None  # trace() says what is wrong with it
        for node, kind in kinds.items():
            if kind != "linux":
                continue
            try:
                if address in self._host_addresses(topo, node):
                    return node
            except Exception:  # noqa: BLE001 - not running, or it did not answer
                continue
        return None

    WHATIF = {"link-down", "link-up", "freeze", "resume"}

    def whatif(self, topo_id: str, action: str, node: str, iface: str = "") -> str:
        """A reversible failure on a running lab (what-if): shut or restore one
        interface inside a node (``ip link set``), or freeze or resume a node
        (``docker pause``: its links stay up, it stops answering, so its
        neighbours time out). Returns what was done."""
        if action not in self.WHATIF:
            raise ValueError(f"action must be one of {', '.join(sorted(self.WHATIF))}")
        topo = load_topology(self.topology_path(topo_id))
        if node not in topo.nodes:
            raise ValueError(f"No node '{node}' in {topo_id}")
        container = next((c for hs in self.runtime(max_age=0) for c in hs.containers
                          if c["lab"] == topo.name and c["node"] == node), None)
        if not container:
            raise ValueError(f"{node} is not deployed")
        host = self.host(container["host"])
        runner = self.runner(host)
        name = container["container"]
        try:
            if action in ("freeze", "resume"):
                verb = "pause" if action == "freeze" else "unpause"
                res = run_docker(runner, ["docker", verb, name], host.sudo)
                if res.exit_code != 0:
                    raise RuntimeError((res.stderr or res.stdout).strip()[-300:] or f"docker {verb} failed")
                return f"{node} {'frozen' if action == 'freeze' else 'resumed'}"
            if container.get("state") != "running":
                raise ValueError(f"{node} is not running")
            if not any(e.get("iface") == iface for link in topology_view(topo)["links"]
                       for e in (link["a"], link["b"]) if e.get("node") == node):
                raise ValueError(f"{node} has no link on '{iface}'")
            state = "down" if action == "link-down" else "up"
            candidates, _ = linux_iface_names(container.get("kind", ""), iface)
            errors = []
            key = (topo.name, node, iface)
            for linux in candidates:  # the kernel name, then the topology's own
                if state == "down" and container.get("kind") in LINUX_KINDS:
                    # The kernel drops a down interface's routes (a host's default
                    # route): keep them to put back on no shut
                    routes = run_docker(runner, ["docker", "exec", name, "ip", "-4", "route", "show",
                                                 "dev", linux], host.sudo)
                    if routes.exit_code == 0:
                        kept = [r.strip() for r in routes.stdout.splitlines() if " via " in f" {r} "]
                        if kept:
                            self._shut_routes[key] = (linux, kept)
                res = run_docker(runner, ["docker", "exec", name, "ip", "link", "set", "dev", linux, state],
                                 host.sudo)
                if res.exit_code == 0:
                    if state == "up":
                        self._restore_routes(runner, host, name, key)
                    return f"{node}:{iface} {'shut' if state == 'down' else 'up again'}"
                errors.append((res.stderr or res.stdout).strip()[-200:])
            raise RuntimeError(f"ip link set {state} failed: {'; '.join(errors)}")
        finally:
            self.invalidate_runtime()

    def _restore_routes(self, runner, host, container: str, key: tuple) -> None:
        """Put back the routes a shut interface lost (see ``whatif``)."""
        linux, routes = self._shut_routes.pop(key, (None, []))
        for route in routes:
            res = run_docker(runner, ["docker", "exec", container, "ip", "route", "replace",
                                      *route.split(), "dev", linux], host.sudo)
            if res.exit_code != 0:
                logger.warning("Could not restore route '%s' on %s: %s", route, container,
                               (res.stderr or res.stdout).strip()[-200:])

    def running_diff(self, topo_id: str, node: str) -> dict:
        """A node's running config, read now, against its startup config.

        On cEOS this is EOS's own ``show running-config diffs``: both sides
        in its canonical form, against the startup-config on the node's
        flash (what it booted with, or what Save configs last wrote). Other
        kinds get a text diff with the topology's startup-config, which can
        show lines the OS only writes differently.
        """
        topo = load_topology(self.topology_path(topo_id))
        if node not in topo.nodes:
            raise ValueError(f"No node '{node}' in {topo_id}")
        kind = topo.effective_node(node)["kind"] or ""
        out = {"lab": topo.name, "node": node, "from": "running-config"}
        if cli_family(kind) == "eos":
            text = self.running_config(topo.name, node, "show running-config diffs")
            diff = "".join(line + "\n" for line in text.splitlines()
                           if line.strip() and not line.startswith("> "))
            return {**out, "against": "startup-config on the node",
                    "status": "changed" if diff else "same", "diff": diff}
        startup, why = startup_config(topo, node)
        if startup is None:
            return {**out, "against": "startup-config", "status": "skipped", "reason": why, "diff": ""}
        running = self.running_config(topo.name, node)
        diff = unified_diff(startup, running, f"{node} startup-config", f"{node} running-config", kind)
        return {**out, "against": "startup-config (text diff)",
                "status": "changed" if diff else "same", "diff": diff}

    def running_config(self, lab: str, node: str, command: str = "show running-config") -> str:
        """``command`` (``show running-config``) on a running node: through
        ``docker exec`` on cEOS, over SSH on IOS kinds. ValueError for others."""
        return self.node_command(lab, node, command, shell=False)

    def node_command(self, lab: str, node: str, command: str, shell: bool = True) -> str:
        """A read-only command on a running node: the CLI on cEOS (``Cli``),
        SSH on IOS kinds, and with ``shell`` the program itself on any other
        kind (no shell runs it, so nothing in it is expanded).

        Commands are built from addresses, interface and VRF names, some of
        them read from other nodes: one with anything but plain command text
        in it (a newline, ``;``, ``$``, quotes) is refused, as it could be a
        second command on a node's CLI."""
        if not SAFE_COMMAND.fullmatch(command):
            raise ValueError("refusing to run a command with unexpected characters on a node")
        container = next((c for hs in self._recent_runtime() for c in hs.containers
                          if c["lab"] == lab and c["node"] == node), None)
        if not container or container.get("state") != "running":
            raise ValueError(f"{node} is not running")
        fam = cli_family(container.get("kind", ""))
        host = self.host(container["host"])
        runner = self.runner(host)
        if fam == "eos":
            res = run_docker(runner, ["docker", "exec", container["container"], "Cli", "-p", "15",
                                      "-c", command], host.sudo)
            if res.exit_code != 0:
                raise RuntimeError((res.stderr or res.stdout).strip()[-300:] or f"{command} failed")
            return res.stdout
        if fam == "ios":
            code, text = ssh_exec(runner, container["kind"], container["ipv4"], command,
                                  timeout=PROTOCOL_SSH_TIMEOUT)
            if code != 0:
                raise RuntimeError(text.strip()[-300:] or f"{command} failed")
            return text
        if shell:
            res = run_docker(runner, ["timeout", "15", "docker", "exec", container["container"],
                                      *command.split()], host.sudo)
            if res.exit_code != 0:
                raise RuntimeError((res.stderr or res.stdout).strip()[-300:] or f"{command} failed")
            return res.stdout
        raise ValueError(f"Reading the running config of kind '{container.get('kind')}' is not supported")

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

    def save_annotations(self, topo_id: str, data) -> dict:
        """Notes and boxes on the lab's diagram (``annotations``)."""
        return annotations.save(self.topology_path(topo_id), data)

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

    # --- spare ports (spare.py) ---

    def add_ports(self, topo_id: str, node: str, count: int, base_hash: str = "") -> dict:
        """Give ``node`` ``count`` more spare ports, in the file. A running
        node does not see them: nodes learn their ports when they boot.
        Returns {"detail", "ports": the new ports, "applies": "now" when the
        lab is not deployed, else "next deploy"}."""
        path = self.topology_path(topo_id)
        text = path.read_text()
        if base_hash and text_hash(text) != base_hash:
            raise EditConflict(f"{path.name} changed on disk; reload it before adding ports")
        topo = load_topology(path)
        ports = next_ports(topo, node, count)
        saved = self.save_yaml(topo_id, add_spare_ports(text, node, ports), text_hash(text))
        deployed = any(c["lab"] == topo.name for hs in self._recent_runtime() for c in hs.containers)
        return {**saved, "ports": ports, "applies": "next deploy" if deployed else "now"}

    def cable(self, topo_id: str, a: tuple[str, str], b: tuple[str, str],
              base_hash: str = "") -> dict:
        """Cable the spare ports ``a`` and ``b`` (each (node, interface)).

        The file gets a link in their place. If the lab runs with both nodes
        on one host, and both are of a kind whose ports are plain interfaces,
        the cable is also made on the spot (``spare.cable_live``). Returns
        {"detail", "live": bool, "note": why not live, or what went wrong
        making it}: the file is changed either way, so the next deploy has
        the cable."""
        path = self.topology_path(topo_id)
        text = path.read_text()
        if base_hash and text_hash(text) != base_hash:
            raise EditConflict(f"{path.name} changed on disk; reload it before cabling")
        topo = load_topology(path)
        for node, _ in (a, b):
            if node not in topo.nodes:
                raise KeyError(f"No node '{node}' in {topo.name}")
        new = cable_spare_ports(text, a, b)  # ValueError if either is not a spare port

        # What the lab looks like now, before the file changes under it
        running = {c["node"]: c for hs in self.runtime(max_age=0) for c in hs.containers
                   if c["lab"] == topo.name}
        ends = [running.get(node) for node, _ in (a, b)]
        note = ""
        if not running:
            note = "the lab is not deployed: the cable is there at the next deploy"
        elif not all(c and c["state"] == "running" for c in ends):
            note = "a node of this cable is not running: the cable is there at the next deploy"
        elif ends[0]["host"] != ends[1]["host"]:
            note = ("the nodes are on different hosts, where a cable is a VXLAN link made at "
                    "deploy: it is there after the next deploy")
        else:
            note = next((f"{why}: the cable is there after the next deploy" for why in
                         (live_problem(topo, node) for node, _ in (a, b)) if why), "")

        host = None
        if not note:
            # A port added since the node booted is in the file only
            host = self.host(ends[0]["host"])
            for (node, iface), c in zip((a, b), ends):
                if not port_exists(self.runner(host), c["container"], iface, host.sudo):
                    note = (f"{node}:{iface} was added after {node} booted, so the running node "
                            "does not have it: the cable is there after the next deploy")
                    break

        saved = self.save_yaml(topo_id, new, text_hash(text))
        if note:
            return {**saved, "live": False, "note": note}
        try:
            cable_live(self.runner(host), (ends[0]["container"], a[1]),
                       (ends[1]["container"], b[1]), host.sudo)
        except Exception as exc:  # noqa: BLE001 - the file has the cable; say what happened
            logger.warning("Live cable %s:%s - %s:%s failed: %s", *a, *b, exc)
            return {**saved, "live": False,
                    "note": f"saved, but it could not be made on the running lab ({exc}): "
                            "it is there after the next deploy"}
        return {**saved, "live": True, "note": ""}

    def uncable(self, topo_id: str, a: tuple[str, str], b: tuple[str, str],
                base_hash: str = "") -> dict:
        """Pull the cable between the ports ``a`` and ``b`` (each (node,
        interface)): the file gets two spare ports in place of the link, and
        where ``cable`` would have plugged it in on the running lab, it is
        pulled there too (``spare.unplug_live``). Returns {"detail", "live",
        "note"} as ``cable`` does."""
        path = self.topology_path(topo_id)
        text = path.read_text()
        if base_hash and text_hash(text) != base_hash:
            raise EditConflict(f"{path.name} changed on disk; reload it before unplugging")
        topo = load_topology(path)
        new = uncable_ports(text, a, b)  # ValueError if there is no such plain link

        running = {c["node"]: c for hs in self.runtime(max_age=0) for c in hs.containers
                   if c["lab"] == topo.name}
        ends = [running.get(node) for node, _ in (a, b)]
        note = ""
        if not running:
            note = "the lab is not deployed: the cable is gone at the next deploy"
        elif not all(c and c["state"] == "running" for c in ends):
            note = "a node of this cable is not running: the cable is gone at the next deploy"
        elif ends[0]["host"] != ends[1]["host"]:
            note = ("the nodes are on different hosts, where a cable is a VXLAN link made at "
                    "deploy: it is gone after the next deploy")
        else:
            note = next((f"{why}: the cable is gone after the next deploy" for why in
                         (live_problem(topo, node) for node, _ in (a, b)) if why), "")

        saved = self.save_yaml(topo_id, new, text_hash(text))
        if note:
            return {**saved, "live": False, "note": note}
        host = self.host(ends[0]["host"])
        try:
            odd = unplug_live(self.runner(host), (ends[0]["container"], a[1]),
                              (ends[1]["container"], b[1]), host.sudo)
        except Exception as exc:  # noqa: BLE001 - the file has it; say what happened
            logger.warning("Live unplug %s:%s - %s:%s failed: %s", *a, *b, exc)
            return {**saved, "live": False,
                    "note": f"saved, but it could not be pulled on the running lab ({exc}): "
                            "it is gone after the next deploy"}
        if odd:
            logger.info("Unplugged, but no placeholder could be put back on %s", ", ".join(odd))
        return {**saved, "live": True, "note": ""}

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
        """Every host at once, each within its time limit: the page waits
        for the slowest host that answers, not for the sum of them all, and
        not at all for one already known to be down."""
        def inspect(host: HostInfo) -> HostState:
            state = HostState(host.name)
            try:
                state.containers = parse_inspect(self.ask_host(host, inspect_all), host.name)
                state.ok = True
            except Exception as exc:  # noqa: BLE001 - shown as the host's error
                state.error = str(exc)
            return state

        hosts = self.cluster.hosts
        states = [inspect(hosts[0])] if len(hosts) == 1 else list(self._hosts_pool.map(inspect, hosts))
        live: set[tuple] = set()
        for host, state in zip(hosts, states):
            self._annotate_ready(host, state.containers, live)
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
        else:
            self.events.observe(topo_id, "links", link_items(snapshot), snapshot["updated"])
            link_rates(self.live.get(topo_id)[0], snapshot)
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
        """Each host as it is now: reachable, containerlab's version, its
        CPUs and memory, and what placement may use of them (``max_cpu``,
        ``max_ram``; ``max_ram_set`` when the inventory sets the RAM, which
        is when placement reserves running labs' RAM against it)."""
        def status(host: HostInfo) -> dict:
            entry = {"name": host.name, "host": host.host, "local": host.is_local}
            ram_set = host.max_ram > 0

            def probe(runner: Runner) -> dict:
                facts = probe_host_resources(runner, host)
                return {**facts, "version": containerlab_version(runner)}

            try:
                facts = self.ask_host(self.host(host.name), probe)
            except Exception as exc:  # noqa: BLE001 - shown as the host's error
                entry.update(ok=False, error=str(exc))
                return entry
            entry.update(
                ok=True,
                version=facts["version"],
                cpus=facts.get("cpus"),
                mem_total_mb=facts.get("MemTotal_mb"),
                mem_available_mb=facts.get("MemAvailable_mb"),
                vtep=host.vtep,
                tags=host.tags,
                max_cpu=host.max_cpu,  # probed into the copy when the inventory has none
                max_ram=host.max_ram,
                max_ram_set=ram_set,
            )
            return entry

        hosts = copy.deepcopy(self.cluster.hosts)
        return [status(hosts[0])] if len(hosts) == 1 else list(self._hosts_pool.map(status, hosts))
