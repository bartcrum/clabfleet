"""Jobs behind the GUI (deploy / destroy / save / snapshot) and their history.

One job per lab at a time, several labs at once. Each job runs in its own
thread.
"""

import copy
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..deployer import LabDeployer
from ..runner import CommandCancelled
from ..snapshots import Snapshotter
from ..topology import load_topology
from .editing import replace_file
from .state import Workspace

logger = logging.getLogger(__name__)

# ANSI escape sequences (colors, bold) in containerlab output
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@dataclass
class Job:
    id: str
    action: str
    topology: str
    lab: str = ""
    status: str = "running"  # running | ok | error | cancelled | interrupted
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
            replace_file(self.directory / f"{job.id}.json", json.dumps(job.to_dict()))
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
    # stop: save the configs, then destroy the containers but keep the lab
    # directory, so the next deploy starts from them
    ACTIONS = {"deploy", "redeploy", "stop", "destroy", "save", "snapshot"}
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
        self._abort: dict[str, Callable[[], None]] = {}   # running job -> how to stop it
        self._cancelled_by: dict[str, str] = {}
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

    def cancel(self, job_id: str, user: str = "") -> Job:
        """Stop a running job: the command it waits for is given up and the
        job ends as cancelled, which frees its place and its lab for other
        jobs. Nothing is undone: what containerlab had started is left as it
        is, and a command on a remote host or under sudo may still finish.
        KeyError for an unknown job, ValueError for one that is not running."""
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(f"No job '{job_id}'")
            if job.status != "running":
                raise ValueError(f"That job is not running any more ({job.status})")
            first = job_id not in self._cancelled_by
            self._cancelled_by.setdefault(job_id, user)
            abort = self._abort.get(job_id)
        if first:
            job.add(f"✗ cancel requested{f' by {user}' if user else ''}")
        if abort:
            abort()
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
            snapshotter = Snapshotter(copy.deepcopy(self.workspace.cluster), on_output=job.add,
                                      interactive_sudo=False)
            with self._lock:
                self._abort[job.id] = lambda: (deployer.abort(), snapshotter.abort())
                cancelled = job.id in self._cancelled_by
            if cancelled:  # asked for before there was anything to stop
                deployer.abort()
                snapshotter.abort()
            job.add(f"$ {job.action} {job.topology}")
            if job.action in ("deploy", "redeploy"):
                result = deployer.deploy(path, reconfigure=job.action == "redeploy",
                                         rollback=job.options.get("rollback", False))
            elif job.action == "stop":
                result = deployer.stop(path)
            elif job.action == "destroy":
                result = deployer.destroy(path)
            elif job.action == "snapshot":
                result = snapshotter.take(path)
                job.add(f"» snapshot {result['snapshot']}: {result['path']}")
            else:
                result = deployer.save(path)
            job.result = result
            errors = {h: r["error"] for h, r in result.get("hosts", {}).items() if "error" in r}
            for host, err in errors.items():
                job.add(f"✗ {host}: {err}")
            job.add_outcome(result)
            job.status = "error" if errors else "ok"
        except CommandCancelled:
            job.add("✗ cancelled. Nothing was undone: what containerlab had started is left as "
                    "it is, and a command already running on a host may still finish there. "
                    "Look at the lab, then Destroy or Redeploy it.")
            job.status = "cancelled"
        except Exception as exc:
            logger.exception("Job %s failed", job.id)
            job.add(f"✗ {exc}")
            job.status = "error"
        finally:
            with self._lock:
                self._abort.pop(job.id, None)
                self._cancelled_by.pop(job.id, None)
            pkg_logger.removeHandler(handler)
            job.finished = time.time()
            times = job.host_times()
            if times:
                job.add("» time per host: " + ", ".join(f"{h} {t:g}s" for h, t in times.items()))
            job.add({"ok": "✓ done", "cancelled": "✗ cancelled"}.get(job.status, "✗ failed"))
            self.workspace.invalidate_runtime()  # the lab's containers changed
            self.history.save(job)
            if self.on_finished:
                try:
                    self.on_finished(job)
                except Exception:  # noqa: BLE001 - never let a hook break a job
                    logger.exception("Job finished hook failed")
