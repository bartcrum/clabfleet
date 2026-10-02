"""Config snapshots and diffs (``clabfleet snapshot`` / ``clabfleet diff``).

``containerlab save`` leaves each node's config in the lab directory on its
host. A snapshot copies those files to a dated folder next to the topology::

    <topology dir>/snapshots/<lab>/<snapshot>/
        snapshot.json      # when, from which host, which kind, what was skipped
        Spine-1.cfg        # one file per node, named after the node
        srl1.json

Snapshots are named after the UTC time they were taken (``20261001T194212Z``)
unless given a name. Files are read with ``cat`` through the host's runner,
so remote hosts work the same as the local one; the placement record says
which host each node is on.

``diff`` compares a snapshot with the one before it, with any other
snapshot, or with the ``startup-config`` the topology gives the node.
"""

import difflib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .cluster import ClusterConfig, HostInfo, create_runner
from .deployer import LabDeployer, clab_dir, read_placement_record
from .execute import select_nodes
from .nodes import NO_SHELL_KINDS
from .runner import OutputCallback, Runner
from .topology import KIND_ALIASES, Topology, load_topology

logger = logging.getLogger(__name__)

SNAPSHOTS_DIR = "snapshots"
META_FILE = "snapshot.json"

# Where `containerlab save` leaves a node's config, relative to the node's
# directory in the lab directory, and the extension used in snapshots
SAVED_CONFIG = {
    "arista_ceos": ("flash/startup-config", ".cfg"),
    "nokia_srlinux": ("config/config.json", ".json"),
    "juniper_crpd": ("config/juniper.conf", ".conf"),
}
# Kinds that save somewhere a snapshot cannot read
UNREADABLE_SAVE = {
    "cisco_iol": "IOL saves to its binary NVRAM file",
}
# Lines that change on every save without a config change; diff ignores them
VOLATILE_LINES = {
    "arista_ceos": re.compile(r"^! Startup-config last modified at "),
}

# Names diff gives a meaning to, so snapshots cannot use them
RESERVED_NAMES = {"previous", "latest", "startup"}

PERMISSION_DENIED = "permission denied"

# A config file name in snapshot.json: a plain name inside the snapshot folder
FILE_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


class SnapshotError(Exception):
    """A snapshot or diff cannot be made at all (bad arguments, nothing found)."""


def canonical_kind(kind: str) -> str:
    return KIND_ALIASES.get(kind, kind)


def snapshot_root(topo: Topology, directory: Optional[str | Path] = None) -> Path:
    """Folder holding a lab's snapshots: ``<dir>/<lab>``, by default next to the topology."""
    base = Path(directory).expanduser() if directory else topo.base_dir / SNAPSHOTS_DIR
    return base / topo.name


def read_file(runner: Runner, path: str, host_sudo: bool) -> tuple[Optional[str], str]:
    """(text, error) of a file on a host. Retries with sudo if the host is
    configured for it and the file is not readable as the SSH/local user."""
    res = runner.run(["cat", "--", path], check=False, sudo=False)
    if res.exit_code != 0 and host_sudo and PERMISSION_DENIED in res.stderr.lower():
        res = runner.run(["cat", "--", path], check=False, sudo=True)
    if res.exit_code != 0:
        err = res.stderr.strip() or f"cat exited {res.exit_code}"
        if "no such file" in err.lower():
            err = "no saved config (run save, or the node is not deployed here)"
        return None, err
    return res.stdout, ""


# ----------------------------------------------------------------------
# Taking snapshots
# ----------------------------------------------------------------------

class Snapshotter:
    """Save a lab's configs and copy them into a local snapshot folder."""

    def __init__(
        self,
        cluster: ClusterConfig,
        on_output: Optional[OutputCallback] = None,
        interactive_sudo: bool = True,
    ):
        self.cluster = cluster
        self.on_output = on_output
        self.interactive_sudo = interactive_sudo
        self._runners: dict[str, Runner] = {}

    def take(
        self,
        topology_file: str | Path,
        nodes: Optional[list[str]] = None,
        directory: Optional[str | Path] = None,
        save: bool = True,
        name: Optional[str] = None,
    ) -> dict:
        """Returns {"lab", "snapshot", "path", "nodes", "skipped", "hosts"}.

        ``hosts`` is the result of the save per host (empty with save=False).
        """
        topo = load_topology(topology_file)
        selected = select_nodes(topo, nodes)
        root = snapshot_root(topo, directory)
        if name is not None:
            check_name(name)
            if (root / name).exists():
                raise SnapshotError(f"Snapshot '{name}' of lab '{topo.name}' already exists")

        summary: dict = {"lab": topo.name, "hosts": {}}
        if save:
            deployer = LabDeployer(self.cluster, on_output=self.on_output,
                                   interactive_sudo=self.interactive_sudo)
            summary["hosts"] = deployer.save(topology_file)["hosts"]
            failed = [h for h, r in summary["hosts"].items() if "error" in r]
            if failed:
                logger.warning("Save failed on %s; configs from there may be stale",
                               ", ".join(failed))

        record = read_placement_record(topo) or {}
        placed = record.get("nodes") if isinstance(record.get("nodes"), dict) else {}
        captured: dict[str, dict] = {}
        texts: dict[str, str] = {}
        skipped: dict[str, str] = {}
        try:
            for node in selected:
                kind = topo.effective_node(node)["kind"] or ""
                if kind in NO_SHELL_KINDS and not nodes:
                    continue  # bridges etc.: nothing to save
                info, text, reason = self._capture(topo, node, kind, placed.get(node))
                if info is None:
                    skipped[node] = reason
                    logger.info("Skipped %s: %s", node, reason)
                    continue
                captured[node] = info
                texts[node] = text
                logger.info("Captured %s from %s:%s", node, info["host"], info["source"])
        finally:
            self._close()

        if not captured:
            raise SnapshotError(
                f"No configs captured for lab '{topo.name}': "
                + "; ".join(f"{n}: {r}" for n, r in skipped.items())
            )

        taken = datetime.now(timezone.utc)
        name = name or _free_name(root, taken.strftime("%Y%m%dT%H%M%SZ"))
        path = root / name
        # Configs hold password hashes and keys: only the user may read them
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.mkdir(mode=0o700)
        for node, info in captured.items():
            _write_private(path / info["file"], texts[node])
        meta = {
            "version": 1,
            "lab": topo.name,
            "name": name,
            "taken_at": taken.isoformat(timespec="seconds"),
            "topology": str(topo.path) if topo.path else None,
            "saved": save,
            "nodes": captured,
            "skipped": skipped,
        }
        _write_private(path / META_FILE, json.dumps(meta, indent=2) + "\n")
        summary.update(snapshot=name, path=str(path), nodes=captured, skipped=skipped)
        return summary

    def _capture(
        self, topo: Topology, node: str, kind: str, host_name: Optional[str]
    ) -> tuple[Optional[dict], str, str]:
        """(info, text, reason): info is None when the node was skipped."""
        canon = canonical_kind(kind)
        if canon in UNREADABLE_SAVE:
            return None, "", UNREADABLE_SAVE[canon]
        if canon not in SAVED_CONFIG:
            return None, "", f"kind '{kind}' has no saved config"
        rel, ext = SAVED_CONFIG[canon]

        if host_name:
            hosts = [h for h in self.cluster.hosts if h.name == host_name]
            if not hosts:
                return None, "", f"its host '{host_name}' is not in the cluster"
        else:
            hosts = list(self.cluster.hosts)  # no record: look everywhere

        errors = []
        for host in hosts:
            source = f"{clab_dir(self.cluster, host, topo)}/{node}/{rel}"
            try:
                text, err = read_file(self._runner(host), source, host.sudo)
            except Exception as exc:  # noqa: BLE001 - an unreachable host skips its nodes
                text, err = None, f"unreachable: {exc}"
            if text is not None:
                info = {"host": host.name, "kind": kind, "file": f"{node}{ext}",
                        "source": source}
                return info, text, ""
            errors.append(err if len(hosts) == 1 else f"{host.name}: {err}")
        return None, "", "; ".join(errors)

    def _runner(self, host: HostInfo) -> Runner:
        if host.name not in self._runners:
            runner = create_runner(host)
            runner.interactive_sudo = self.interactive_sudo
            self._runners[host.name] = runner
        return self._runners[host.name]

    def _close(self) -> None:
        for runner in self._runners.values():
            runner.close()
        self._runners.clear()


def check_name(name: str) -> None:
    if (not name or name in RESERVED_NAMES or name.startswith(".")
            or not re.fullmatch(r"[A-Za-z0-9._-]+", name)):
        raise SnapshotError(
            f"Invalid snapshot name '{name}': use letters, digits, '.', '_' and '-', "
            f"not starting with '.', and not {', '.join(sorted(RESERVED_NAMES))}"
        )


def _write_private(path: Path, text: str) -> None:
    """Create a new file only the user can read (never through a symlink)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def _free_name(root: Path, base: str) -> str:
    name, n = base, 1
    while (root / name).exists():
        n += 1
        name = f"{base}-{n}"
    return name


# ----------------------------------------------------------------------
# Listing and reading snapshots
# ----------------------------------------------------------------------

def list_snapshots(topo: Topology, directory: Optional[str | Path] = None) -> list[dict]:
    """The lab's snapshots, oldest first (their snapshot.json contents plus ``path``)."""
    root = snapshot_root(topo, directory)
    if not root.is_dir():
        return []
    result = []
    for path in root.iterdir():
        meta_path = path / META_FILE
        # A snapshot folder may come from someone else (a downloaded lab), so
        # symlinks are not followed and snapshot.json is not trusted
        if path.is_symlink() or meta_path.is_symlink() or not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable snapshot %s: %s", path, exc)
            continue
        if not isinstance(meta, dict) or not isinstance(meta.get("nodes"), dict):
            logger.warning("Ignoring snapshot %s: bad %s", path, META_FILE)
            continue
        meta["nodes"] = {str(n): info for n, info in meta["nodes"].items()
                         if isinstance(info, dict)}
        for info in meta["nodes"].values():
            info["kind"] = str(info.get("kind") or "")
        skipped = meta.get("skipped")
        meta["skipped"] = ({str(n): str(r) for n, r in skipped.items()}
                           if isinstance(skipped, dict) else {})
        meta["taken_at"] = str(meta.get("taken_at") or "")
        meta["name"] = path.name
        meta["path"] = str(path)
        result.append(meta)
    result.sort(key=lambda m: (m["taken_at"], m["name"]))
    return result


def _find(snapshots: list[dict], name: str, lab: str) -> int:
    if not snapshots:
        raise SnapshotError(f"Lab '{lab}' has no snapshots yet (run: clabfleet snapshot)")
    if name == "latest":
        return len(snapshots) - 1
    for i, snap in enumerate(snapshots):
        if snap["name"] == name:
            return i
    raise SnapshotError(f"Lab '{lab}' has no snapshot '{name}'")


def snapshot_config(snap: dict, node: str) -> Optional[str]:
    info = snap["nodes"].get(node)
    if not info:
        return None
    # snapshot.json is just a file in the folder: only read plain file names
    # in the snapshot itself, not "../x", "/etc/x" or a symlink out of it
    name = info.get("file")
    if not isinstance(name, str) or name in (".", "..") or not FILE_NAME_RE.fullmatch(name):
        return None
    folder = Path(snap["path"]).resolve()
    path = (folder / name).resolve()
    if not path.is_relative_to(folder):
        return None
    try:
        return path.read_text()
    except OSError:
        return None


def startup_config(topo: Topology, node: str) -> tuple[Optional[str], str]:
    """(text, reason) of the startup-config the topology gives a node."""
    value = topo.effective_node(node).get("startup-config")
    if not value:
        return None, "no startup-config in the topology"
    if not isinstance(value, str):
        return None, "startup-config is not a string"
    if "\n" in value:
        return value, ""  # inline config
    if re.match(r"^[a-z][a-z0-9+.-]*://", value):
        return None, f"startup-config is a URL ({value})"
    value = value.replace("__clabNodeName__", node)
    if value.startswith("__clab"):
        return None, f"startup-config uses a containerlab path ({value})"
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = topo.base_dir / path
    # The GUI shows this to anyone who may read diffs, so only files in the
    # topology's folder (checked after symlinks), never e.g. ~/.ssh/id_rsa
    if not path.resolve().is_relative_to(topo.base_dir.resolve()):
        return None, "startup-config is outside the topology directory"
    try:
        return path.read_text(), ""
    except OSError as exc:
        return None, f"cannot read startup-config {path}: {exc.strerror or exc}"


# ----------------------------------------------------------------------
# Diffs
# ----------------------------------------------------------------------

@dataclass
class NodeDiff:
    node: str
    status: str  # same | changed | added | removed | skipped
    diff: str = ""
    reason: str = ""


def _lines(text: str, kind: str) -> list[str]:
    volatile = VOLATILE_LINES.get(canonical_kind(kind))
    lines = text.splitlines()
    if volatile:
        lines = [line for line in lines if not volatile.match(line)]
    return lines


def unified_diff(old: str, new: str, old_label: str, new_label: str, kind: str = "") -> str:
    """Unified diff text ("" when equal), ignoring the kind's volatile lines.

    Hunk line numbers count lines without the volatile ones.
    """
    lines = difflib.unified_diff(
        _lines(old, kind), _lines(new, kind), fromfile=old_label, tofile=new_label, lineterm="",
    )
    return "".join(line + "\n" for line in lines)


def diff_lab(
    topology_file: str | Path,
    nodes: Optional[list[str]] = None,
    against: str = "previous",
    from_snapshot: str = "latest",
    directory: Optional[str | Path] = None,
) -> dict:
    """Compare snapshot ``from_snapshot`` with ``against``.

    ``against`` is ``previous`` (the snapshot before it), ``startup`` (the
    topology's startup-config), ``latest`` or a snapshot name. Returns
    {"lab", "from", "against", "nodes": [NodeDiff dicts], "changed"}.
    """
    topo = load_topology(topology_file)
    snapshots = list_snapshots(topo, directory)
    new_i = _find(snapshots, from_snapshot, topo.name)
    new = snapshots[new_i]

    old = None
    if against == "previous":
        if new_i == 0:
            raise SnapshotError(
                f"Snapshot '{new['name']}' is the first of lab '{topo.name}'; "
                "there is nothing before it (try --against startup)"
            )
        old = snapshots[new_i - 1]
    elif against != "startup":
        old = snapshots[_find(snapshots, against, topo.name)]

    if nodes:
        selected = select_nodes(topo, nodes)
    else:
        # Every node in either side, in topology order, then any since removed
        present = set(new["nodes"]) | (set(old["nodes"]) if old else set())
        selected = [n for n in topo.nodes if n in present]
        selected += sorted(present - set(selected))

    results = []
    for node in selected:
        new_text = snapshot_config(new, node)
        kind = (new["nodes"].get(node) or (old or {}).get("nodes", {}).get(node) or {}).get("kind", "")
        new_label = f"{new['name']}/{node}"
        if old is None:
            old_text, reason = (startup_config(topo, node) if node in topo.nodes
                                else (None, "not in the topology"))
            old_label = f"startup/{node}"
            if old_text is None:
                results.append(NodeDiff(node, "skipped", reason=reason))
                continue
            if new_text is None:
                results.append(NodeDiff(node, "skipped",
                                        reason=_why_missing(new, node)))
                continue
        else:
            old_text = snapshot_config(old, node)
            old_label = f"{old['name']}/{node}"
            if old_text is None and new_text is None:
                results.append(NodeDiff(node, "skipped", reason=_why_missing(new, node)))
                continue
            if old_text is None:
                results.append(NodeDiff(node, "added", unified_diff(
                    "", new_text, "/dev/null", new_label, kind),
                    reason=f"not in snapshot '{old['name']}'"))
                continue
            if new_text is None:
                results.append(NodeDiff(node, "removed", unified_diff(
                    old_text, "", old_label, "/dev/null", kind),
                    reason=f"not in snapshot '{new['name']}'"))
                continue
        text = unified_diff(old_text, new_text, old_label, new_label, kind)
        results.append(NodeDiff(node, "changed" if text else "same", text))

    return {
        "lab": topo.name,
        "from": new["name"],
        "against": old["name"] if old else "startup",
        "nodes": [asdict(r) for r in results],
        "changed": sum(r.status in ("changed", "added", "removed") for r in results),
    }


def _why_missing(snap: dict, node: str) -> str:
    reason = (snap.get("skipped") or {}).get(node)
    if reason:
        return f"not captured in '{snap['name']}': {reason}"
    if node in snap["nodes"]:
        return f"config file missing from snapshot '{snap['name']}'"
    return f"not in snapshot '{snap['name']}'"
