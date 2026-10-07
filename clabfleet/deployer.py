"""Deploy, destroy, save and inspect containerlab labs on one or more hosts.

Single host:
  - Local:  ``containerlab`` runs directly against your topology file, so the
    lab directory (``clab-<name>/``) is created next to it, exactly as if you
    had run containerlab yourself.
  - Remote: the topology and the files it references (startup configs,
    licenses, bind sources) are copied to ``~/clabfleet/<lab>/`` on the
    host over SSH and containerlab runs there.

Multiple hosts (cluster):
  1. Probe each host's CPU/RAM
  2. Run the placement engine → decide which nodes go where
  3. Split the topology into one sub-topology per host. Links between nodes
     on different hosts become a pair of ``vxlan-stitch`` (or ``vxlan``)
     links — one on each host, pointing at the other host's VTEP address,
     sharing a unique VNI
  4. Copy each sub-topology to its host and run ``containerlab deploy``

containerlab creates and removes the VXLAN links itself, so ``destroy``
only needs to run ``containerlab destroy`` on every host.

Each deploy writes ``<lab>.placement.json`` next to the topology file: the
host of every node, the VNI range and the cross-host links. ``destroy``,
``save``, ``inspect`` and ``exec`` then only contact the hosts listed
there, and the GUI uses it to show where nodes run.

VNIs are unique across the cluster: before splitting, every host's lab
directories are scanned for VNIs that other labs already use, and this
lab takes the first free block at or above ``vni_base``.
"""

import copy
import json
import logging
import posixpath
import re
import shlex
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

from .cluster import (
    ClusterConfig,
    HostInfo,
    RunnerPool,
    create_runner,
    probe_host_resources,
)
from .linkcheck import check_links, describe, failures, vxlan_pairs
from .nodes import NO_SHELL_KINDS, inspect_all, parse_inspect, running_usage
from .placement import NodePlacement, PlacementError, PlacementPlan, compute_placement
from .readiness import check_ready, wait_until_ready
from .runner import CommandError, OutputCallback, Runner, deadline
from .spare import unplug
from .topology import Topology, dump_yaml, load_topology, topology_from_dict

logger = logging.getLogger(__name__)


# Prints NODOCKER if Docker is unreachable, else MISSING <image> per absent image
_IMAGE_CHECK_SCRIPT = (
    "docker version --format '{{.Server.Version}}' >/dev/null 2>&1 "
    "|| { echo NODOCKER; exit 0; }\n"
    'for i in "$@"; do docker image inspect "$i" >/dev/null 2>&1 || echo "MISSING $i"; done'
)

PLACEMENT_RECORD_SUFFIX = ".placement.json"

MAX_VNI = 2**24 - 1
# A `vni: 1234` line in a per-host topology file written by clabfleet
_VNI_LINE = re.compile(r"^(?P<path>.+?):\s*vni:\s*(?P<vni>\d+)\s*$")


# Deploys running in one process (the GUI runs several jobs at once) plan
# one at a time. A lab's VNIs and resources only become visible to other
# deploys once its files and containers exist on the hosts, so until its
# deploy finishes it is registered here and planning counts it in.
_PLAN_LOCK = threading.RLock()
PLAN_TIMEOUT = 120  # seconds the hosts get, together, to answer a deploy's planning
_IN_FLIGHT: dict[str, dict] = {}  # lab → {"vnis": set, "hosts": {host: (cpu, ram)}}


def _register_in_flight(lab: str, plan: PlacementPlan, vnis: range) -> None:
    hosts: dict[str, tuple[float, int]] = {}
    for p in plan.placements:
        cpu, ram = hosts.get(p.host_name, (0, 0))
        hosts[p.host_name] = (cpu + p.cpu, ram + p.ram)
    with _PLAN_LOCK:
        _IN_FLIGHT[lab] = {"vnis": set(vnis), "hosts": hosts}


def _unregister_in_flight(lab: str) -> None:
    with _PLAN_LOCK:
        _IN_FLIGHT.pop(lab, None)


def _since(started: float) -> float:
    return round(time.monotonic() - started, 1)


class DeploymentError(Exception):
    """Raised when a deployment step fails."""


class LabDeployer:
    """Run containerlab lifecycle commands across the hosts of a cluster.

    A single-host setup is simply a cluster with one host.
    """

    def __init__(
        self,
        cluster: ClusterConfig,
        on_output: Optional[OutputCallback] = None,
        interactive_sudo: bool = True,
    ):
        """
        Args:
            on_output: Receives containerlab's output line by line for
                deploy/destroy/save (prefixed with the host name when
                there are several hosts).
            interactive_sudo: False to never wait on a sudo password prompt.
        """
        if not cluster.hosts:
            raise DeploymentError("No hosts configured")
        self.cluster = cluster
        self.on_output = on_output
        self.interactive_sudo = interactive_sudo
        self._runners = RunnerPool(lambda host: create_runner(host), interactive_sudo)

    def abort(self) -> None:
        """Stop the job this deployer is doing (from another thread): the
        command it waits for is given up and ``CommandCancelled`` ends the
        job. What containerlab had done by then stays as it is, and a
        command on a remote host or under sudo may still finish there."""
        self._runners.abort()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def deploy(
        self,
        topology_file: str | Path,
        strategy: str = "bin-pack",
        reconfigure: bool = False,
        dry_run: bool = False,
        output_dir: Optional[str | Path] = None,
        check_images: bool = True,
        pull_images: bool = False,
        check_connectivity: bool = True,
        rollback: bool = False,
        wait: bool = False,
        wait_timeout: float = 900,
    ) -> dict:
        """Deploy a topology.

        Args:
            topology_file: Path to a containerlab topology file.
            strategy: Placement strategy for multi-host ("bin-pack", "spread", "resource").
            reconfigure: Pass ``--reconfigure`` to containerlab (redeploy from scratch).
            dry_run: Compute placement and per-host topologies without deploying.
            output_dir: Also write the per-host topology files here.
            check_images: Before deploying, fail if a node's image is missing
                on the host it is placed on.
            pull_images: ``docker pull`` missing images before checking.
            check_connectivity: Before deploying a lab with cross-host
                links, check the hosts reach each other on the VXLAN port.
            rollback: If any host fails, destroy the lab on every host it was
                deployed to (including the failed one, which may hold
                partly created nodes) instead of leaving it partly running.

            wait: After deploying, wait until every node is ready (its CLI or
                SSH answers, see clabfleet.readiness), up to ``wait_timeout``
                seconds. The result is in the summary's ``readiness``.

        The summary's ``status`` is ``deployed``, ``partial`` (some hosts
        failed), ``failed`` (all failed), ``rolled-back`` or
        ``rollback-failed``.

        Returns:
            Summary dict with placement, cross-host links and per-host results.
        """
        topo = load_topology(topology_file)
        summary: dict = {"lab": topo.name, "hosts": {}}

        registered = False
        try:
            # Planning asks every host and holds the lock every deploy of
            # this process plans under: a host that stopped answering must
            # fail this plan, not hold all the others
            with _PLAN_LOCK, deadline(PLAN_TIMEOUT):
                plan = self._plan(topo, strategy)
                vni_base = self.cluster.vni_base
                needed = count_cross_host_links(topo, plan)
                if needed:
                    vni_base = self._allocate_vnis(topo, needed)
                    summary["vni_range"] = [vni_base, vni_base + needed - 1]
                if not dry_run:
                    _register_in_flight(topo.name, plan, range(vni_base, vni_base + needed))
                    registered = True
            host_topos, cross_links = split_topology(
                topo, plan, self.cluster, vni_base=vni_base
            )
            summary["placement"] = plan.summary()["placements"]
            summary["cross_host_links"] = cross_links

            if output_dir:
                summary["written"] = _write_host_topologies(
                    topo, host_topos, Path(output_dir), multi=self._multi_host
                )

            if dry_run:
                summary["dry_run"] = True
                return summary

            if check_connectivity and cross_links:
                summary["link_check"] = self._check_connectivity(cross_links)

            if check_images:
                self._check_images(topo, host_topos, pull_images)

            record = self._placement_record(topo, plan, host_topos, cross_links,
                                            strategy, summary.get("vni_range"))
            # Redeploying over a running lab can move nodes off a host; keep
            # such hosts listed so destroy still cleans up what is left there
            previous = read_placement_record(topo) or {}
            for name, entry in (previous.get("hosts") or {}).items():
                if name not in record["hosts"] and isinstance(entry, dict):
                    record["hosts"][name] = {**entry, "nodes": [], "stale": True}
            path = write_placement_record(topo, record)
            if path:
                summary["placement_record"] = str(path)

            attempted: list[HostInfo] = []
            for host in self.cluster.hosts:
                data = host_topos.get(host.name)
                if data is None:
                    continue
                attempted.append(host)
                started = time.monotonic()
                try:
                    summary["hosts"][host.name] = self._deploy_on_host(
                        host, topo, data, reconfigure
                    )
                except Exception as exc:
                    logger.error("Deployment failed on %s: %s", host.name, exc)
                    summary["hosts"][host.name] = {"error": str(exc)}
                summary["hosts"][host.name]["seconds"] = _since(started)
                if rollback and "error" in summary["hosts"][host.name]:
                    break  # no point deploying the rest

            failed = [h for h, r in summary["hosts"].items() if "error" in r]
            if failed and rollback:
                summary["rollback"] = self._rollback(topo, attempted)
                ok = all(r == "ok" for r in summary["rollback"].values())
                summary["status"] = "rolled-back" if ok else "rollback-failed"
            elif not failed:
                summary["status"] = "deployed"
            elif len(failed) < len(summary["hosts"]):
                summary["status"] = "partial"
                logger.warning(
                    "'%s' is only partly deployed: running on %s, failed on %s. "
                    "Destroy it to clean up, or deploy with --rollback next time.",
                    topo.name,
                    ", ".join(h for h in summary["hosts"] if h not in failed),
                    ", ".join(failed),
                )
            else:
                summary["status"] = "failed"

            if wait and summary["status"] in ("deployed", "partial"):
                deployed_on = [h for h in attempted if "error" not in summary["hosts"][h.name]]
                summary["readiness"] = self._wait_ready(topo, deployed_on, host_topos,
                                                        wait_timeout)
        finally:
            if registered:
                _unregister_in_flight(topo.name)
            self._runners.close()

        return summary

    def destroy(self, topology_file: str | Path, cleanup: bool = True) -> dict:
        """Destroy a lab on every host it may be deployed on.

        Args:
            cleanup: Remove the lab directory (and, for copied labs, the
                uploaded topology and config files) as well.
        """
        topo = load_topology(topology_file)
        summary: dict = {"lab": topo.name, "hosts": {}}
        try:
            for host in hosts_for_lab(self.cluster, topo):
                started = time.monotonic()
                summary["hosts"][host.name] = self._on_host(
                    host, topo, "destroy", ["--cleanup"] if cleanup else []
                )
                summary["hosts"][host.name]["seconds"] = _since(started)
                if cleanup and not self._in_place(host, topo):
                    result = summary["hosts"][host.name]
                    if result.get("status") == "ok":
                        self._runners.get(host).remove_tree(host.lab_dir(topo.name))
        finally:
            self._runners.close()
        if all(r.get("status") in ("ok", "not-deployed") for r in summary["hosts"].values()):
            remove_placement_record(topo)
        return summary

    def stop(self, topology_file: str | Path) -> dict:
        """Save the running configs, then remove the containers and keep the
        lab directory, so the next deploy starts from the saved configs. A
        failed save on any host stops nothing: its unsaved changes would be
        lost. The summary's ``stopped`` says whether the lab was removed."""
        saved = self.save(topology_file)
        failed = [h for h, r in saved.get("hosts", {}).items() if "error" in r]
        if failed:
            logger.error("not stopped: saving the configs failed on %s; nothing was removed",
                         ", ".join(failed))
            return {**saved, "stopped": False}
        logger.info("Configs saved; removing the containers, keeping the lab directory")
        return {**self.destroy(topology_file, cleanup=False), "stopped": True}

    def save(self, topology_file: str | Path) -> dict:
        """Save running configs of all nodes (``containerlab save``) on every host."""
        topo = load_topology(topology_file)
        summary: dict = {"lab": topo.name, "hosts": {}}
        try:
            for host in hosts_for_lab(self.cluster, topo):
                started = time.monotonic()
                summary["hosts"][host.name] = self._on_host(host, topo, "save", [])
                summary["hosts"][host.name]["seconds"] = _since(started)
        finally:
            self._runners.close()
        return summary

    def inspect(self, topology_file: Optional[str | Path] = None) -> dict:
        """Show running containers for a lab (or all labs) on every host."""
        topo = load_topology(topology_file) if topology_file else None
        summary: dict = {"hosts": {}}
        hosts = hosts_for_lab(self.cluster, topo) if topo else self.cluster.hosts
        try:
            for host in hosts:
                if topo:
                    result = self._on_host(
                        host, topo, "inspect", ["--format", "json"], parse_json=True
                    )
                else:
                    result = self._run_clab(
                        host, ["inspect", "--all", "--format", "json"],
                        cwd=None, parse_json=True,
                    )
                summary["hosts"][host.name] = result
        finally:
            self._runners.close()
        return summary

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @property
    def _multi_host(self) -> bool:
        return len(self.cluster.hosts) > 1

    def _in_place(self, host: HostInfo, topo: Topology) -> bool:
        """Single local host: run containerlab directly on the user's file."""
        return runs_in_place(self.cluster, host, topo)

    def _plan(self, topo: Topology, strategy: str) -> PlacementPlan:
        if not self._multi_host:
            host = self.cluster.hosts[0]
            plan = PlacementPlan()
            for node in topo.placement_nodes():
                plan.placements.append(
                    NodePlacement(node["name"], host.name, node["cpu"], node["ram"])
                )
            return plan

        logger.info("Probing %d cluster hosts", len(self.cluster.hosts))
        for host in self.cluster.hosts:
            # A RAM budget from the inventory is for labs in total; a probed
            # one (MemAvailable) already excludes what running labs use
            explicit_ram = host.max_ram > 0
            try:
                probe_host_resources(self._runners.get(host), host)
            except Exception as exc:
                raise DeploymentError(
                    f"Host {host.name} ({host.host}) unreachable: {exc}"
                ) from exc
            self._reserve_running_labs(host, topo.name, reserve_ram=explicit_ram)

        logger.info("Computing placement with strategy '%s'", strategy)
        try:
            return compute_placement(
                topo.placement_nodes(),
                topo.link_memberships(),
                self.cluster.hosts,
                strategy,
            )
        except PlacementError as exc:
            raise DeploymentError(f"Placement failed: {exc}") from exc

    def _wait_ready(
        self, topo: Topology, hosts: list[HostInfo], host_topos: dict[str, dict],
        timeout: float,
    ) -> dict:
        """Wait until the lab's nodes on ``hosts`` are ready (see readiness)."""
        expected = [
            n for h in hosts for n in host_topos[h.name]["topology"]["nodes"]
            if topo.effective_node(n)["kind"] not in NO_SHELL_KINDS
        ]
        logger.info("Waiting up to %ds for %d nodes to be ready", timeout, len(expected))

        def poll(pending: list[str]) -> dict[str, tuple[bool, str]]:
            found: dict[str, tuple[HostInfo, dict]] = {}
            for host in hosts:
                try:
                    data = inspect_all(self._runners.get(host))
                except Exception as exc:
                    logger.warning("Could not inspect %s: %s", host.name, exc)
                    continue
                for c in parse_inspect(data, host.name):
                    if c["lab"] == topo.name and c["node"] in pending:
                        found[c["node"]] = (host, c)
            if not found:
                return {}
            with ThreadPoolExecutor(max_workers=min(16, len(found))) as pool:
                checks = pool.map(
                    lambda item: check_ready(self._runners.get(item[0]), item[1], item[0].sudo),
                    found.values(),
                )
                return dict(zip(found, checks))

        return wait_until_ready(poll, expected, timeout)

    def _rollback(self, topo: Topology, hosts: list[HostInfo]) -> dict[str, str]:
        """Destroy the lab on hosts after a failed deploy. Returns host → outcome."""
        logger.warning("Rolling back '%s' on %s", topo.name, ", ".join(h.name for h in hosts))
        outcome: dict[str, str] = {}
        for host in hosts:
            try:
                result = self._on_host(host, topo, "destroy", ["--cleanup"])
                if result.get("status") == "ok" and not self._in_place(host, topo):
                    self._runners.get(host).remove_tree(host.lab_dir(topo.name))
            except Exception as exc:  # noqa: BLE001 - keep rolling back the others
                result = {"status": "error", "error": str(exc)}
            status = result.get("status")
            if status in ("ok", "not-deployed"):
                outcome[host.name] = "ok"
            else:
                outcome[host.name] = f"error: {result.get('error', status)}"
                logger.error("Rollback failed on %s: %s", host.name, outcome[host.name])
        if all(v == "ok" for v in outcome.values()):
            remove_placement_record(topo)
        return outcome

    def _reserve_running_labs(self, host: HostInfo, lab: str, reserve_ram: bool) -> None:
        """Reserve the estimated CPU (and RAM) of other labs on a host.

        Covers labs running there and labs this process is deploying right
        now (whose containers may not exist yet). Call with _PLAN_LOCK held.
        """
        in_flight = {name: e for name, e in _IN_FLIGHT.items() if name != lab}
        try:
            usage = running_usage(inspect_all(self._runners.get(host)), exclude_lab=lab)
        except Exception as exc:
            logger.warning("Could not list running labs on %s (%s); placement "
                           "ignores them", host.name, exc)
            usage = {}
        cpu = ram = 0
        for name, u in usage.items():
            if name not in in_flight:  # counted below instead
                cpu, ram = cpu + u["cpu"], ram + u["ram"]
        for e in in_flight.values():
            c, r = e["hosts"].get(host.name, (0, 0))
            cpu, ram = cpu + c, ram + r
        labs = sorted(set(usage) | {n for n, e in in_flight.items() if host.name in e["hosts"]})
        if not labs:
            return
        if not reserve_ram:
            ram = 0
        host.reserve(cpu, ram)
        logger.info(
            "Host %s: labs %s reserve %.1f vCPU%s", host.name,
            ", ".join(labs), cpu, f" / {ram}MB" if reserve_ram else "",
        )

    def _allocate_vnis(self, topo: Topology, count: int) -> int:
        """First VNI of a free block of ``count`` VNIs for this lab."""
        used: set[int] = set()
        for host in self.cluster.hosts:
            try:
                by_lab = scan_used_vnis(self._runners.get(host), host)
            except Exception as exc:
                logger.warning(
                    "Could not check VNIs in use on %s (%s); VXLAN links may "
                    "clash with other labs", host.name, exc,
                )
                continue
            for lab, vnis in by_lab.items():
                if lab != topo.name:  # a redeploy may reuse its own VNIs
                    used |= vnis
        for lab, entry in _IN_FLIGHT.items():  # deploys not yet on the hosts
            if lab != topo.name:
                used |= entry["vnis"]
        base = allocate_vni_block(used, self.cluster.vni_base, count)
        logger.info("Using VNIs %d-%d for '%s'", base, base + count - 1, topo.name)
        return base

    def _placement_record(
        self,
        topo: Topology,
        plan: PlacementPlan,
        host_topos: dict[str, dict],
        cross_links: list[dict],
        strategy: str,
        vni_range: Optional[list[int]],
    ) -> dict:
        hosts = {}
        for host in self.cluster.hosts:
            data = host_topos.get(host.name)
            if data is None:
                continue
            if self._in_place(host, topo):
                lab_dir = str(topo.base_dir.resolve() / f"clab-{topo.name}")
            else:
                lab_dir = host.lab_dir(topo.name)
            hosts[host.name] = {
                "address": host.host,
                "lab_dir": lab_dir,
                "nodes": list(data["topology"]["nodes"]),
            }
        return {
            "version": 1,
            "lab": topo.name,
            "topology": topo.path.name if topo.path else None,
            "cluster": self.cluster.source,
            "strategy": strategy if self._multi_host else None,
            "deployed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "hosts": hosts,
            "nodes": {p.node_name: p.host_name for p in plan.placements},
            "vni_range": vni_range,
            "cross_host_links": cross_links,
        }

    def _check_connectivity(self, cross_links: list[dict]) -> list[dict]:
        """Raise DeploymentError if hosts sharing links cannot reach each other."""
        pairs = vxlan_pairs(cross_links)
        hosts = {h.name: h for h in self.cluster.hosts}
        runners = {name: self._runners.get(hosts[name]) for pair in pairs for name in pair}
        logger.info("Checking VXLAN connectivity between %s", ", ".join(sorted(runners)))
        results = check_links(runners, self.cluster.hosts, pairs, self.cluster.dst_port)
        bad = failures(results)
        for r in results:
            if r not in bad and r["note"]:
                logger.warning("Link check: %s", describe(r))
        if bad:
            raise DeploymentError(
                "Lab hosts cannot reach each other for VXLAN links:\n"
                + "".join(f"  {describe(r)}\n" for r in bad)
                + f"VXLAN needs UDP {self.cluster.dst_port} open between the hosts. "
                "Fix the network or firewall, or skip this check with --skip-link-check."
            )
        return results

    def _check_images(self, topo: Topology, host_topos: dict[str, dict], pull: bool) -> None:
        """Raise DeploymentError listing every image missing on its target host."""
        problems: dict[str, dict[str, list[str]]] = {}
        for host in self.cluster.hosts:
            data = host_topos.get(host.name)
            if data is None:
                continue
            images = node_images(topo, data["topology"]["nodes"])
            if not images:
                continue
            runner = self._runners.get(host)
            sudo = False
            missing = missing_images(runner, list(images), sudo=False)
            if missing is None and host.sudo:
                sudo = True
                missing = missing_images(runner, list(images), sudo=True)
            if missing is None:
                logger.warning(
                    "Cannot reach Docker on %s to check images; skipping the check there",
                    host.name,
                )
                continue
            if missing and pull:
                for image in missing:
                    logger.info("Pulling %s on %s", image, host.name)
                    runner.run(["docker", "pull", image], check=False, sudo=sudo,
                               on_output=self._output_for(host))
                missing = missing_images(runner, missing, sudo=sudo) or []
            if missing:
                problems[host.name] = {image: images[image] for image in missing}

        if problems:
            lines = ["Images missing on lab hosts:"]
            for host_name, images in problems.items():
                for image, nodes in images.items():
                    lines.append(f"  {host_name}: {image} ({', '.join(nodes)})")
            lines.append(
                "Build or import them on those hosts"
                + ("" if pull else ", re-run with --pull to pull them from their registry")
                + ", or skip this check with --skip-image-check."
            )
            raise DeploymentError("\n".join(lines))

    def _output_for(self, host: HostInfo) -> Optional[OutputCallback]:
        if not self.on_output:
            return None
        prefix = f"[{host.name}] " if self._multi_host else ""
        return lambda line: self.on_output(prefix + line)

    def _deploy_on_host(
        self, host: HostInfo, topo: Topology, data: dict, reconfigure: bool
    ) -> dict:
        extra = ["--reconfigure"] if reconfigure else []
        nodes = list(data["topology"]["nodes"])
        runner = self._runners.get(host)

        if self._in_place(host, topo):
            logger.info("Deploying '%s' locally from %s", topo.name, topo.path)
            self._run_clab(
                host, ["deploy", "-t", topo.path.name, *extra],
                cwd=str(topo.base_dir.resolve()), check=True, stream=True,
            )
            self._unplug_spares(host, topo, nodes)
            return {"status": "deployed", "nodes": nodes,
                    "lab_dir": str(topo.base_dir.resolve() / f"clab-{topo.name}")}

        lab_dir = host.lab_dir(topo.name)
        host_topo = topology_from_dict(data, base_dir=topo.base_dir)
        logger.info("Copying '%s' (%d nodes) to %s:%s", topo.name, len(nodes), host.name, lab_dir)
        runner.makedirs(lab_dir)
        for rel in host_topo.referenced_files():
            runner.put_file(topo.base_dir / rel, f"{lab_dir}/{rel.as_posix()}")
        topo_file = f"{topo.name}.clab.yml"
        runner.write_text(f"{lab_dir}/{topo_file}", dump_yaml(data))

        logger.info("Running containerlab deploy on %s", host.name)
        self._run_clab(host, ["deploy", "-t", topo_file, *extra], cwd=lab_dir,
                       check=True, stream=True)
        self._unplug_spares(host, topo, nodes)
        return {"status": "deployed", "nodes": nodes, "lab_dir": lab_dir}

    def _unplug_spares(self, host: HostInfo, topo: Topology, nodes: list[str]) -> None:
        """Make the spare ports of the nodes just deployed on ``host`` look
        unplugged (``spare.unplug``). Never fails a deploy: a port that
        cannot be changed shows as up."""
        ports = {n: ifaces for n, ifaces in topo.spare_ports().items() if n in nodes}
        if not ports:
            return
        runner = self._runners.get(host)
        try:
            containers = {c["node"]: c["container"]
                          for c in parse_inspect(inspect_all(runner), host.name)
                          if c["lab"] == topo.name}
            failed = unplug(runner, containers, ports, host.sudo)
        except Exception as exc:  # noqa: BLE001 - cosmetic: the ports work either way
            logger.warning("Could not set the spare ports on %s to unplugged: %s", host.name, exc)
            return
        count = sum(map(len, ports.values()))
        logger.info("%d spare port%s on %s", count, "" if count == 1 else "s", host.name)
        if failed:
            logger.info("Spare ports that show as up, not as unplugged (the node's `ip` cannot "
                        "turn a carrier off): %s", ", ".join(failed))

    def _on_host(
        self,
        host: HostInfo,
        topo: Topology,
        command: str,
        extra: list[str],
        parse_json: bool = False,
    ) -> dict:
        """Run ``containerlab <command> -t <topo>`` wherever the lab lives on a host."""
        if self._in_place(host, topo):
            return self._run_clab(
                host, [command, "-t", topo.path.name, *extra],
                cwd=str(topo.base_dir.resolve()), parse_json=parse_json,
                stream=not parse_json,
            )

        lab_dir = host.lab_dir(topo.name)
        topo_file = f"{topo.name}.clab.yml"
        try:
            deployed = self._runners.get(host).exists(f"{lab_dir}/{topo_file}")
        except Exception as exc:
            return {"status": "error", "error": f"unreachable: {exc}"}
        if not deployed:
            return {"status": "not-deployed"}
        return self._run_clab(
            host, [command, "-t", topo_file, *extra], cwd=lab_dir,
            parse_json=parse_json, stream=not parse_json,
        )

    def _run_clab(
        self,
        host: HostInfo,
        args: list[str],
        cwd: Optional[str],
        check: bool = False,
        parse_json: bool = False,
        stream: bool = False,
    ) -> dict:
        on_output = self._output_for(host) if stream else None
        try:
            result = self._runners.get(host).containerlab(
                args, cwd=cwd, check=check, on_output=on_output
            )
        except CommandError:
            raise
        except Exception as exc:
            if check:
                raise
            return {"status": "error", "error": str(exc)}

        if result.exit_code != 0:
            return {"status": "error", "error": result.stderr.strip() or result.stdout.strip()}
        out: dict = {"status": "ok"}
        if parse_json:
            try:
                out["data"] = json.loads(result.stdout) if result.stdout.strip() else {}
            except json.JSONDecodeError:
                out["output"] = result.stdout
        return out


def runs_in_place(cluster: ClusterConfig, host: HostInfo, topo: Topology) -> bool:
    """Single local host: containerlab runs directly on the user's file."""
    return len(cluster.hosts) == 1 and host.is_local and topo.path is not None


def clab_dir(cluster: ClusterConfig, host: HostInfo, topo: Topology) -> str:
    """The ``clab-<lab>`` directory containerlab creates for the lab on a host.

    It holds one directory per node, where ``containerlab save`` leaves the
    node's config.
    """
    if runs_in_place(cluster, host, topo):
        return str(topo.base_dir.resolve() / f"clab-{topo.name}")
    return f"{host.lab_dir(topo.name)}/clab-{topo.name}"


def placement_record_path(topo: Topology) -> Optional[Path]:
    """Where the placement record of a topology lives (next to its file)."""
    if topo.path is None:
        return None
    return topo.path.parent / f"{topo.name}{PLACEMENT_RECORD_SUFFIX}"


def read_placement_record(topo: Topology) -> Optional[dict]:
    path = placement_record_path(topo)
    if path is None or not path.exists():
        return None
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable placement record %s: %s", path, exc)
        return None
    if not isinstance(record, dict) or record.get("lab") != topo.name:
        logger.warning("Ignoring placement record %s: it is not for lab '%s'", path, topo.name)
        return None
    return record


def write_placement_record(topo: Topology, record: dict) -> Optional[Path]:
    path = placement_record_path(topo)
    if path is None:
        return None
    try:
        path.write_text(json.dumps(record, indent=2) + "\n")
    except OSError as exc:
        logger.warning("Could not write placement record %s: %s", path, exc)
        return None
    return path


def remove_placement_record(topo: Topology) -> None:
    path = placement_record_path(topo)
    if path is not None and path.exists():
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("Could not remove placement record %s: %s", path, exc)


def hosts_for_lab(cluster: ClusterConfig, topo: Topology) -> list[HostInfo]:
    """Cluster hosts a deployed lab lives on, from its placement record.

    Without a usable record, every host in the cluster.
    """
    record = read_placement_record(topo)
    if not record or not isinstance(record.get("hosts"), dict):
        return list(cluster.hosts)
    names = set(record["hosts"])
    hosts = [h for h in cluster.hosts if h.name in names]
    unknown = names - {h.name for h in hosts}
    if unknown:
        logger.warning(
            "Lab '%s' was deployed on host(s) %s, which are not in the current "
            "cluster", topo.name, ", ".join(sorted(unknown)),
        )
    if not hosts:
        return list(cluster.hosts)
    logger.info("Lab '%s' is on %s (from its placement record)",
                topo.name, ", ".join(h.name for h in hosts))
    return hosts


def node_images(topo: Topology, node_names) -> dict[str, list[str]]:
    """image → nodes using it, for nodes whose image containerlab won't pull itself.

    Nodes without an image (bridges, kinds with a built-in default) and
    nodes with ``image-pull-policy: always`` are left out.
    """
    images: dict[str, list[str]] = {}
    for name in node_names:
        node = topo.effective_node(name)
        image = node.get("image")
        if not image:
            continue
        if str(node.get("image-pull-policy", "")).lower() == "always":
            continue
        images.setdefault(str(image), []).append(name)
    return images


def missing_images(runner: Runner, images: list[str], sudo: bool) -> Optional[list[str]]:
    """Images not present on a host, or None when Docker cannot be reached."""
    result = runner.run(["sh", "-c", _IMAGE_CHECK_SCRIPT, "sh", *images],
                        check=False, sudo=sudo)
    lines = result.stdout.splitlines()
    if "NODOCKER" in lines or result.exit_code != 0:
        return None
    return [line[len("MISSING "):] for line in lines if line.startswith("MISSING ")]


def count_cross_host_links(topo: Topology, plan: PlacementPlan) -> int:
    """Number of links whose nodes the plan puts on different hosts."""
    return sum(
        1 for link in topo.links
        if len({plan.host_for_node(n) for n in link.node_names}) > 1
    )


def scan_used_vnis(runner: Runner, host: HostInfo) -> dict[str, set[int]]:
    """VNIs used by labs clabfleet has deployed on a host, per lab name.

    Reads the ``vni:`` lines of the per-host topology files in the host's
    lab directories (``<workdir>/<lab>/<lab>.clab.yml``). A lab directory
    exists until the lab is destroyed with cleanup, so a destroyed lab kept
    with ``--keep-lab-dir`` still holds its VNIs.
    """
    workdir = posixpath.dirname(host.lab_dir("_"))
    script = (
        "grep -H -E '^[[:space:]]*vni:[[:space:]]*[0-9]+[[:space:]]*$' "
        f"{shlex.quote(workdir)}/*/*.clab.yml 2>/dev/null; true"
    )
    result = runner.run(["sh", "-c", script], check=False, sudo=False)
    used: dict[str, set[int]] = defaultdict(set)
    for line in result.stdout.splitlines():
        m = _VNI_LINE.match(line)
        if m:
            used[PurePosixPath(m.group("path")).parent.name].add(int(m.group("vni")))
    return dict(used)


def allocate_vni_block(used: set[int], start: int, count: int) -> int:
    """Lowest VNI >= start such that start..start+count-1 avoids ``used``."""
    base = start
    while True:
        if base + count - 1 > MAX_VNI:
            raise DeploymentError(
                f"No free block of {count} VNIs at or above {start} "
                f"({len(used)} VNIs are in use by other labs)"
            )
        clash = [v for v in used if base <= v < base + count]
        if not clash:
            return base
        base = max(clash) + 1


def split_topology(
    topo: Topology,
    plan: PlacementPlan,
    cluster: ClusterConfig,
    vni_base: Optional[int] = None,
) -> tuple[dict[str, dict], list[dict]]:
    """Split a topology into one containerlab topology per host.

    Cross-host links get consecutive VNIs from ``vni_base`` (default: the
    cluster's ``vni_base``).

    Returns (host name → topology dict, list of cross-host link descriptions).
    Hosts with no nodes are omitted.
    """
    hosts = {h.name: h for h in cluster.hosts}
    host_links: dict[str, list[dict]] = defaultdict(list)
    cross_links: list[dict] = []
    vni = cluster.vni_base if vni_base is None else vni_base

    for link in topo.links:
        link_hosts = {plan.host_for_node(n) for n in link.node_names}
        if len(link_hosts) == 1:
            host_links[link_hosts.pop()].append(link.raw)
            continue

        # Two lab nodes on different hosts → one VXLAN link on each side
        a, b = link.endpoints
        host_a = hosts[plan.host_for_node(a.node)]
        host_b = hosts[plan.host_for_node(b.node)]
        for ep, local, remote in ((a, host_a, host_b), (b, host_b, host_a)):
            if not remote.vtep:
                raise DeploymentError(
                    f"Link {a.node}:{a.interface} ↔ {b.node}:{b.interface} crosses "
                    f"hosts, but host '{remote.name}' has no reachable address — "
                    f"set 'vtep_ip' for it in the cluster config"
                )
            vx_link = {
                "type": cluster.link_type,
                "endpoint": {"node": ep.node, "interface": ep.interface, **ep.extra},
                "remote": remote.vtep,
                "vni": vni,
                "dst-port": cluster.dst_port,
            }
            mtu = link.raw.get("mtu") or cluster.mtu
            if mtu:
                vx_link["mtu"] = mtu
            for key in ("vars", "labels"):
                if key in link.raw:
                    vx_link[key] = copy.deepcopy(link.raw[key])
            host_links[local.name].append(vx_link)

        cross_links.append({
            "a": f"{a.node}:{a.interface}",
            "b": f"{b.node}:{b.interface}",
            "hosts": [host_a.name, host_b.name],
            "vni": vni,
        })
        vni += 1

    host_topos: dict[str, dict] = {}
    for host in cluster.hosts:
        node_names = [n for n in topo.nodes if plan.host_for_node(n) == host.name]
        if not node_names:
            continue
        data = copy.deepcopy(topo.data)
        data["topology"]["nodes"] = {n: data["topology"]["nodes"][n] for n in node_names}
        data["topology"]["links"] = host_links.get(host.name, [])
        if not data["topology"]["links"]:
            del data["topology"]["links"]
        host_topos[host.name] = data

    return host_topos, cross_links


def _write_host_topologies(
    topo: Topology, host_topos: dict[str, dict], output_dir: Path, multi: bool
) -> list[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for host_name, data in host_topos.items():
        suffix = f".{host_name}" if multi else ""
        path = output_dir / f"{topo.name}{suffix}.clab.yml"
        path.write_text(dump_yaml(data))
        written.append(str(path))
    return written
