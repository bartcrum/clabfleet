"""CLI entrypoint for clabfleet.

Usage:
    clabfleet deploy <topology.clab.yml> [--reconfigure] [--dry-run] [--output-dir DIR]
                     [--pull] [--skip-image-check] [--skip-link-check] [--rollback]
                     [--wait [--wait-timeout SECONDS]]
    clabfleet deploy <topology.clab.yml> --cluster <cluster.yaml> [--strategy bin-pack]
    clabfleet destroy <topology.clab.yml> [--cluster <cluster.yaml>] [--keep-lab-dir]
    clabfleet save <topology.clab.yml> [--cluster <cluster.yaml>]
    clabfleet inspect [<topology.clab.yml>] [--cluster <cluster.yaml>]
    clabfleet exec <topology.clab.yml> <command> [--nodes GLOB] [--mode auto|cli|shell|ssh] [--json]
    clabfleet status [--cluster <cluster.yaml>]
    clabfleet validate <topology.clab.yml>... [--cluster <cluster.yaml>] [--strict]
    clabfleet export-live <devices.yaml> [-o output.clab.yml]
    clabfleet gui [--cluster <cluster.yaml>] [--dir DIR ...] [--port 8650]

Without --cluster, commands target a single host: this machine by default,
or a remote server over SSH with --host.
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import yaml

from .cluster import (
    ClusterConfig,
    HostInfo,
    containerlab_version,
    create_runner,
    load_cluster_config,
    probe_host_resources,
)
from .deployer import LabDeployer
from .execute import LabExecutor
from .linkcheck import all_pairs, check_links, describe, failures
from .nodes import inspect_all, running_usage
from .exporter import export_from_live_network
from .topology import dump_yaml
from .validate import validate_topology


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clabfleet",
        description="Automate containerlab deployments on one or many hosts",
    )

    # Single-host target options
    parser.add_argument("--host", default=os.environ.get("CLAB_HOST", "localhost"),
                        help="Lab host to run containerlab on (default: localhost, "
                             "or CLAB_HOST env var)")
    parser.add_argument("--ssh-user", default=os.environ.get("CLAB_SSH_USER"),
                        help="SSH username for --host (or CLAB_SSH_USER)")
    parser.add_argument("--ssh-key", default=os.environ.get("CLAB_SSH_KEY"),
                        help="SSH private key for --host (or CLAB_SSH_KEY)")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--sudo", action="store_true",
                        default=os.environ.get("CLAB_SUDO", "") not in ("", "0", "false"),
                        help="Run containerlab with sudo (or CLAB_SUDO=1)")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="Increase verbosity (-v, -vv)")

    sub = parser.add_subparsers(dest="command", required=True)

    def add_cluster_arg(p):
        p.add_argument("--cluster", metavar="CLUSTER_YAML",
                       help="Operate on all hosts in a cluster inventory")

    # --- deploy ---
    p_deploy = sub.add_parser("deploy", help="Deploy a containerlab topology")
    p_deploy.add_argument("topology", help="Path to a containerlab topology file")
    add_cluster_arg(p_deploy)
    p_deploy.add_argument("--strategy", default="bin-pack",
                          choices=["bin-pack", "spread", "resource"],
                          help="Placement strategy for multi-host (default: bin-pack)")
    p_deploy.add_argument("--reconfigure", action="store_true",
                          help="Destroy and redeploy nodes, discarding their lab state")
    p_deploy.add_argument("--dry-run", action="store_true",
                          help="Show placement and per-host topologies without deploying")
    p_deploy.add_argument("--output-dir", metavar="DIR",
                          help="Write the per-host topology files to DIR")
    p_deploy.add_argument("--pull", action="store_true",
                          help="docker pull node images that are missing on their host")
    p_deploy.add_argument("--skip-image-check", action="store_true",
                          help="Deploy without first checking that node images exist")
    p_deploy.add_argument("--wait", action="store_true",
                          help="Wait until every node's CLI or SSH answers")
    p_deploy.add_argument("--wait-timeout", type=float, default=900, metavar="SECONDS",
                          help="How long --wait waits (default: 900)")
    p_deploy.add_argument("--rollback", action="store_true",
                          help="If any host fails, destroy the lab everywhere it was "
                               "deployed instead of leaving it partly running")
    p_deploy.add_argument("--skip-link-check", action="store_true",
                          help="Deploy across hosts without first checking that they "
                               "reach each other on the VXLAN port")

    # --- destroy ---
    p_destroy = sub.add_parser("destroy", aliases=["teardown"], help="Destroy a lab")
    p_destroy.add_argument("topology", help="Path to the topology file the lab was deployed from")
    add_cluster_arg(p_destroy)
    p_destroy.add_argument("--keep-lab-dir", action="store_true",
                           help="Keep the lab directory (saved configs, certs, copied files)")

    # --- save ---
    p_save = sub.add_parser("save", help="Save running configs of all lab nodes")
    p_save.add_argument("topology", help="Path to the topology file")
    add_cluster_arg(p_save)

    # --- inspect ---
    p_inspect = sub.add_parser("inspect", help="Show running lab containers")
    p_inspect.add_argument("topology", nargs="?",
                           help="Topology file (default: all labs on the host(s))")
    add_cluster_arg(p_inspect)

    # --- exec ---
    p_exec = sub.add_parser(
        "exec", help="Run a command on the nodes of a deployed lab",
        description="Run a command on lab nodes in parallel. Quote the command, "
                    "or put it after '--' if it has options: "
                    "clabfleet exec lab.clab.yml -- ip -br addr")
    p_exec.add_argument("topology", help="Topology file the lab was deployed from")
    p_exec.add_argument("cmd", nargs="+", metavar="command", help="Command to run")
    add_cluster_arg(p_exec)
    p_exec.add_argument("--nodes", action="append", metavar="GLOB",
                        help="Only nodes matching this glob (repeatable or "
                             "comma-separated, e.g. 'leaf*,spine1')")
    p_exec.add_argument("--mode", default="auto", choices=["auto", "cli", "shell", "ssh"],
                        help="auto (default): the node's CLI if it has one via docker "
                             "exec, SSH for VM-based kinds such as IOL, else a shell")
    p_exec.add_argument("--user", dest="node_user",
                        help="SSH username on the nodes (default: per kind, usually admin)")
    p_exec.add_argument("--password", dest="node_password",
                        help="SSH password on the nodes (default: CLAB_NODE_PASSWORD or admin)")
    p_exec.add_argument("--parallel", type=int, default=8,
                        help="Nodes to run on at once (default: 8)")
    p_exec.add_argument("--timeout", type=float, default=60,
                        help="SSH connect/command timeout in seconds (default: 60)")
    p_exec.add_argument("--json", action="store_true", help="Print results as JSON")

    # --- status ---
    p_status = sub.add_parser("status", help="Show containerlab version and resources per host")
    add_cluster_arg(p_status)
    p_status.add_argument("--check-links", action="store_true",
                          help="Also check every pair of hosts reaches the other on the "
                               "VXLAN port (ping and UDP)")

    # --- validate ---
    p_validate = sub.add_parser(
        "validate", help="Check topology files without touching any host")
    p_validate.add_argument("topologies", nargs="+", metavar="topology",
                            help="Topology file(s) to check")
    add_cluster_arg(p_validate)
    p_validate.add_argument("--strict", action="store_true",
                            help="Fail on warnings as well as errors")

    # --- export-live ---
    p_live = sub.add_parser("export-live",
                            help="Build a topology from live network devices (NAPALM)")
    p_live.add_argument("devices", help="YAML file with device connection info")
    p_live.add_argument("-o", "--output", help="Output topology file "
                        "(configs are written to configs/ next to it)")
    p_live.add_argument("--lab-name", default="imported-topology",
                        help="Name for the generated lab")

    # --- gui ---
    p_gui = sub.add_parser("gui", help="Open the web GUI")
    add_cluster_arg(p_gui)
    p_gui.add_argument("--dir", action="append", metavar="DIR",
                       help="Directory to search for *.clab.yml topologies "
                            "(repeatable; default: current directory)")
    p_gui.add_argument("--port", type=int, default=8650, help="Port (default: 8650)")
    p_gui.add_argument("--bind", default="127.0.0.1",
                       help="Address to listen on (default: 127.0.0.1 — the GUI "
                            "opens shells on lab nodes, so keep it local)")
    p_gui.add_argument("--no-browser", action="store_true",
                       help="Don't open a browser window")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    level = logging.WARNING
    if args.verbose >= 2:
        level = logging.DEBUG
    elif args.verbose >= 1:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if args.verbose < 2:
        logging.getLogger("paramiko").setLevel(logging.WARNING)

    try:
        return _dispatch(args)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if args.verbose >= 2:
            import traceback
            traceback.print_exc()
        return 1


def _cluster_from_args(args: argparse.Namespace) -> ClusterConfig:
    if getattr(args, "cluster", None):
        return load_cluster_config(args.cluster)
    return ClusterConfig(hosts=[HostInfo(
        name=args.host,
        host=args.host,
        ssh_user=args.ssh_user,
        ssh_port=args.ssh_port,
        ssh_key=args.ssh_key,
        ssh_password=os.environ.get("CLAB_SSH_PASS"),
        sudo=args.sudo,
    )])


def _dispatch(args: argparse.Namespace) -> int:
    cmd = args.command

    if cmd == "export-live":
        with open(Path(args.devices)) as fh:
            devices = yaml.safe_load(fh)
        if not isinstance(devices, list):
            devices = devices.get("devices", [])
        topo = export_from_live_network(
            devices, output_file=args.output, lab_name=args.lab_name,
        )
        if not args.output:
            print(dump_yaml(topo))
        return 0

    if cmd == "validate":
        return _validate(args)

    cluster = _cluster_from_args(args)

    if cmd == "gui":
        return _gui(args, cluster)

    if cmd == "status":
        rc = _status(cluster)
        if args.check_links:
            rc = max(rc, _check_links(cluster))
        return rc

    if cmd == "exec":
        return _exec(args, cluster)

    deployer = LabDeployer(cluster)
    if cmd == "deploy":
        summary = deployer.deploy(
            args.topology,
            strategy=args.strategy,
            reconfigure=args.reconfigure,
            dry_run=args.dry_run,
            output_dir=args.output_dir,
            check_images=not args.skip_image_check,
            pull_images=args.pull,
            check_connectivity=not args.skip_link_check,
            rollback=args.rollback,
            wait=args.wait,
            wait_timeout=args.wait_timeout,
        )
    elif cmd in ("destroy", "teardown"):
        summary = deployer.destroy(args.topology, cleanup=not args.keep_lab_dir)
    elif cmd == "save":
        summary = deployer.save(args.topology)
    elif cmd == "inspect":
        summary = deployer.inspect(args.topology)
    else:
        raise ValueError(f"Unknown command {cmd}")

    print(json.dumps(summary, indent=2, default=str))
    failed = [
        name for name, result in summary.get("hosts", {}).items()
        if "error" in result
    ]
    not_ready = summary.get("readiness", {}).get("ready") is False
    return 1 if failed or not_ready else 0


def _gui(args: argparse.Namespace, cluster: ClusterConfig) -> int:
    try:
        from .gui.server import run
        from .gui.state import Workspace
    except ImportError as exc:
        raise RuntimeError(
            f"The GUI needs extra packages ({exc.name}). "
            "Install them with: pip install 'clabfleet[gui]'"
        ) from exc
    roots = [Path(d).expanduser() for d in (args.dir or ["."])]
    for root in roots:
        if not root.is_dir():
            raise FileNotFoundError(f"Not a directory: {root}")
    run(Workspace(cluster, roots), host=args.bind, port=args.port,
        open_browser=not args.no_browser)
    return 0


def _exec(args: argparse.Namespace, cluster: ClusterConfig) -> int:
    patterns = [p.strip() for arg in args.nodes or [] for p in arg.split(",") if p.strip()]
    executor = LabExecutor(cluster, ssh_user=args.node_user, ssh_password=args.node_password,
                           parallel=args.parallel, timeout=args.timeout)
    out = executor.run(args.topology, " ".join(args.cmd), nodes=patterns or None,
                       mode=args.mode)
    results = out["results"]
    failed = [r for r in results if r["error"] or r["exit_code"] != 0]

    if args.json:
        print(json.dumps(out, indent=2))
        return 1 if failed or not results else 0

    for host, err in out["host_errors"].items():
        print(f"warning: {host}: {err}", file=sys.stderr)
    multi = len(cluster.hosts) > 1
    for r in results:
        where = ", ".join(p for p in (r["kind"], r["mode"], r["host"] if multi else "") if p)
        print(f"=== {r['node']} ({where}) ===")
        if r["error"]:
            print(f"error: {r['error']}")
        else:
            text = r["output"].rstrip("\n")
            if text:
                print(text)
            if r["exit_code"] != 0:
                print(f"[exit {r['exit_code']}]")
        print()
    if not results:
        print("No nodes to run on.", file=sys.stderr)
    elif failed:
        print(f"{len(failed)} of {len(results)} nodes failed: "
              f"{', '.join(r['node'] for r in failed)}", file=sys.stderr)
    return 1 if failed or not results else 0


def _validate(args: argparse.Namespace) -> int:
    # Only an explicit --cluster is checked: never probe or contact hosts here
    cluster = load_cluster_config(args.cluster) if args.cluster else None
    failed = 0
    for path in args.topologies:
        report = validate_topology(path, cluster)
        print(f"{path}: {report.summary()}")
        for msg in report.errors:
            print(f"  error: {msg}")
        for msg in report.warnings:
            print(f"  warning: {msg}")
        if report.errors or (args.strict and report.warnings):
            failed += 1
    return 1 if failed else 0


def _check_links(cluster: ClusterConfig) -> int:
    if len(cluster.hosts) < 2:
        print("Link check: only one host, nothing to check.")
        return 0
    runners = {h.name: create_runner(h) for h in cluster.hosts}
    try:
        results = check_links(runners, cluster.hosts, all_pairs(cluster.hosts),
                              cluster.dst_port)
    finally:
        for runner in runners.values():
            runner.close()
    print(f"VXLAN connectivity (UDP {cluster.dst_port}):")
    for r in results:
        print(f"  {describe(r)}")
    bad = failures(results)
    if bad:
        print(f"{len(bad)} of {len(results)} host pairs failed.")
    return 1 if bad else 0


def _status(cluster: ClusterConfig) -> int:
    unreachable = 0
    for host in cluster.hosts:
        label = host.name if host.name == host.host else f"{host.name} ({host.host})"
        try:
            with create_runner(host) as runner:
                facts = probe_host_resources(runner, host)
                version = containerlab_version(runner)
        except Exception as exc:
            print(f"{label}: UNREACHABLE — {exc}\n")
            unreachable += 1
            continue

        print(label)
        print(f"  containerlab: {version or 'NOT INSTALLED'}")
        print(f"  CPU:  {facts.get('cpus', '?')} cores"
              f" (placement limit {host.max_cpu or 'unlimited'})")
        print(f"  RAM:  {facts.get('MemAvailable_mb', '?')}MB available"
              f" / {facts.get('MemTotal_mb', '?')}MB total"
              f" (placement limit {host.max_ram or 'unlimited'}MB)")
        try:
            with create_runner(host) as runner:
                usage = running_usage(inspect_all(runner))
        except Exception as exc:
            print(f"  Labs: could not list ({exc})")
        else:
            for lab, u in sorted(usage.items()):
                print(f"  Lab {lab}: {u['nodes']} running nodes, about "
                      f"{u['cpu']:g} vCPU / {u['ram']}MB")
        if len(cluster.hosts) > 1:
            print(f"  VTEP: {host.vtep or 'NOT SET — set vtep_ip for cross-host links'}")
            print(f"  Tags: {', '.join(host.tags) or '(none)'}")
        print()
    return 1 if unreachable else 0


if __name__ == "__main__":
    sys.exit(main())
