"""State behind the GUI: workspace topologies, running labs, and jobs.

Everything here is synchronous; the web server calls it from worker threads.
"""

import copy
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..cluster import ClusterConfig, HostInfo, containerlab_version, create_runner, probe_host_resources
from ..deployer import LabDeployer, read_placement_record
from ..nodes import access_modes, parse_inspect, terminal_command  # noqa: F401 (re-exported)
from ..runner import Runner
from ..topology import (
    LABEL_HOST,
    LABEL_HOST_TAGS,
    SPECIAL_ENDPOINT_NODES,
    Topology,
    load_topology,
)

logger = logging.getLogger(__name__)

TOPOLOGY_SUFFIXES = (".clab.yml", ".clab.yaml")
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox"}
MAX_SCAN_DEPTH = 5

# ANSI escape sequences (colors, bold) in containerlab output
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# ----------------------------------------------------------------------
# Workspace topologies
# ----------------------------------------------------------------------

def find_topologies(roots: list[Path]) -> dict[str, Path]:
    """Find containerlab topology files under the workspace roots.

    Returns {id: path}; the id is the path relative to its root (prefixed
    with the root name when there are several roots) and is what the
    browser uses to refer to a topology.
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

@dataclass
class HostState:
    name: str
    ok: bool = False
    error: str = ""
    containers: list[dict] = field(default_factory=list)


class Workspace:
    """Hosts + topology roots the GUI manages, with cached runners."""

    def __init__(self, cluster: ClusterConfig, roots: list[Path]):
        self.cluster = cluster
        self.roots = roots
        self._runners: dict[str, Runner] = {}
        self._runner_lock = threading.Lock()
        self._topologies: dict[str, Path] = {}

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
        return self._topologies[topo_id]

    def topology_detail(self, topo_id: str) -> dict:
        path = self.topology_path(topo_id)
        detail = {"id": topo_id, "path": str(path), "yaml": path.read_text()}
        try:
            topo = load_topology(path)
            detail.update(topology_view(topo))
            # Where the last deploy put each node (None if not deployed)
            detail["placement"] = read_placement_record(topo)
        except Exception as exc:
            detail["error"] = str(exc)
        return detail

    # --- runtime ---

    def runtime(self) -> list[HostState]:
        """Containers running on each host (one `containerlab inspect` per host)."""
        states = []
        for host in self.cluster.hosts:
            state = HostState(host.name)
            try:
                result = self.runner(host).containerlab(
                    ["inspect", "--all", "--details", "--format", "json"], check=False
                )
                if result.exit_code != 0:
                    state.error = (result.stderr or result.stdout).strip()[-500:]
                else:
                    out = result.stdout.strip()
                    state.containers = parse_inspect(json.loads(out) if out else {}, host.name)
                    state.ok = True
            except Exception as exc:
                state.error = str(exc)
                self.drop_runner(host)  # reconnect next time
            states.append(state)
        return states

    def find_node(self, lab: str, node: str) -> dict:
        for state in self.runtime():
            for c in state.containers:
                if c["lab"] == lab and c["node"] == node:
                    return c
        raise KeyError(f"Node '{node}' of lab '{lab}' is not running")

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
# Jobs (deploy / destroy / save), one at a time
# ----------------------------------------------------------------------

@dataclass
class Job:
    id: str
    action: str
    topology: str
    status: str = "running"  # running | ok | error
    lines: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: Optional[float] = None
    result: Optional[dict] = None

    def add(self, line: str) -> None:
        self.lines.append(ANSI_RE.sub("", line))

    def view(self, offset: int = 0) -> dict:
        return {
            "id": self.id, "action": self.action, "topology": self.topology,
            "status": self.status, "started": self.started, "finished": self.finished,
            "lines": self.lines[offset:], "offset": len(self.lines),
            "result": self.result,
        }


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
    ACTIONS = {"deploy", "redeploy", "destroy", "save"}

    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self.current: Optional[Job] = None

    def start(self, action: str, topo_id: str) -> Job:
        if action not in self.ACTIONS:
            raise ValueError(f"Unknown action '{action}'")
        path = self.workspace.topology_path(topo_id)
        with self._lock:
            if self.current and self.current.status == "running":
                raise RuntimeError(
                    f"Another job is running ({self.current.action} {self.current.topology})"
                )
            job = Job(id=uuid.uuid4().hex[:12], action=action, topology=topo_id)
            self.jobs[job.id] = job
            self.current = job
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
                result = deployer.deploy(path, reconfigure=job.action == "redeploy")
            elif job.action == "destroy":
                result = deployer.destroy(path)
            else:
                result = deployer.save(path)
            job.result = result
            errors = {h: r["error"] for h, r in result.get("hosts", {}).items() if "error" in r}
            for host, err in errors.items():
                job.add(f"✗ {host}: {err}")
            job.status = "error" if errors else "ok"
        except Exception as exc:
            logger.exception("Job %s failed", job.id)
            job.add(f"✗ {exc}")
            job.status = "error"
        finally:
            pkg_logger.removeHandler(handler)
            job.finished = time.time()
            job.add("✓ done" if job.status == "ok" else "✗ failed")
