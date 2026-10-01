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
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Optional

from .cluster import (
    ClusterConfig,
    HostInfo,
    create_runner,
    probe_host_resources,
)
from .placement import NodePlacement, PlacementError, PlacementPlan, compute_placement
from .runner import CommandError, OutputCallback, Runner
from .topology import Topology, dump_yaml, load_topology, topology_from_dict

logger = logging.getLogger(__name__)


# Prints NODOCKER if Docker is unreachable, else MISSING <image> per absent image
_IMAGE_CHECK_SCRIPT = (
    "docker version --format '{{.Server.Version}}' >/dev/null 2>&1 "
    "|| { echo NODOCKER; exit 0; }\n"
    'for i in "$@"; do docker image inspect "$i" >/dev/null 2>&1 || echo "MISSING $i"; done'
)

MAX_VNI = 2**24 - 1
# A `vni: 1234` line in a per-host topology file written by clabfleet
_VNI_LINE = re.compile(r"^(?P<path>.+?):\s*vni:\s*(?P<vni>\d+)\s*$")


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
        self._runners: dict[str, Runner] = {}

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

        Returns:
            Summary dict with placement, cross-host links and per-host results.
        """
        topo = load_topology(topology_file)
        summary: dict = {"lab": topo.name, "hosts": {}}

        try:
            plan = self._plan(topo, strategy)
            vni_base = self.cluster.vni_base
            needed = count_cross_host_links(topo, plan)
            if needed:
                vni_base = self._allocate_vnis(topo, needed)
                summary["vni_range"] = [vni_base, vni_base + needed - 1]
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

            if check_images:
                self._check_images(topo, host_topos, pull_images)

            for host in self.cluster.hosts:
                data = host_topos.get(host.name)
                if data is None:
                    continue
                try:
                    summary["hosts"][host.name] = self._deploy_on_host(
                        host, topo, data, reconfigure
                    )
                except Exception as exc:
                    logger.error("Deployment failed on %s: %s", host.name, exc)
                    summary["hosts"][host.name] = {"error": str(exc)}
        finally:
            self._close_runners()

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
            for host in self.cluster.hosts:
                summary["hosts"][host.name] = self._on_host(
                    host, topo, "destroy", ["--cleanup"] if cleanup else []
                )
                if cleanup and not self._in_place(host, topo):
                    result = summary["hosts"][host.name]
                    if result.get("status") == "ok":
                        self._runner(host).remove_tree(host.lab_dir(topo.name))
        finally:
            self._close_runners()
        return summary

    def save(self, topology_file: str | Path) -> dict:
        """Save running configs of all nodes (``containerlab save``) on every host."""
        topo = load_topology(topology_file)
        summary: dict = {"lab": topo.name, "hosts": {}}
        try:
            for host in self.cluster.hosts:
                summary["hosts"][host.name] = self._on_host(host, topo, "save", [])
        finally:
            self._close_runners()
        return summary

    def inspect(self, topology_file: Optional[str | Path] = None) -> dict:
        """Show running containers for a lab (or all labs) on every host."""
        topo = load_topology(topology_file) if topology_file else None
        summary: dict = {"hosts": {}}
        try:
            for host in self.cluster.hosts:
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
            self._close_runners()
        return summary

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @property
    def _multi_host(self) -> bool:
        return len(self.cluster.hosts) > 1

    def _in_place(self, host: HostInfo, topo: Topology) -> bool:
        """Single local host: run containerlab directly on the user's file."""
        return not self._multi_host and host.is_local and topo.path is not None

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
            try:
                probe_host_resources(self._runner(host), host)
            except Exception as exc:
                raise DeploymentError(
                    f"Host {host.name} ({host.host}) unreachable: {exc}"
                ) from exc

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

    def _allocate_vnis(self, topo: Topology, count: int) -> int:
        """First VNI of a free block of ``count`` VNIs for this lab."""
        used: set[int] = set()
        for host in self.cluster.hosts:
            try:
                by_lab = scan_used_vnis(self._runner(host), host)
            except Exception as exc:
                logger.warning(
                    "Could not check VNIs in use on %s (%s); VXLAN links may "
                    "clash with other labs", host.name, exc,
                )
                continue
            for lab, vnis in by_lab.items():
                if lab != topo.name:  # a redeploy may reuse its own VNIs
                    used |= vnis
        base = allocate_vni_block(used, self.cluster.vni_base, count)
        logger.info("Using VNIs %d-%d for '%s'", base, base + count - 1, topo.name)
        return base

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
            runner = self._runner(host)
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
        runner = self._runner(host)

        if self._in_place(host, topo):
            logger.info("Deploying '%s' locally from %s", topo.name, topo.path)
            self._run_clab(
                host, ["deploy", "-t", topo.path.name, *extra],
                cwd=str(topo.base_dir.resolve()), check=True, stream=True,
            )
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
        return {"status": "deployed", "nodes": nodes, "lab_dir": lab_dir}

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
            deployed = self._runner(host).exists(f"{lab_dir}/{topo_file}")
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
            result = self._runner(host).containerlab(
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

    def _runner(self, host: HostInfo) -> Runner:
        if host.name not in self._runners:
            runner = create_runner(host)
            runner.interactive_sudo = self.interactive_sudo
            self._runners[host.name] = runner
        return self._runners[host.name]

    def _close_runners(self) -> None:
        for runner in self._runners.values():
            runner.close()
        self._runners.clear()


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
