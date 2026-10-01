"""CLI entrypoint for clab-automator.

Usage:
    clab-automator deploy <topology.clab.yml> [--reconfigure] [--dry-run] [--output-dir DIR]
    clab-automator deploy <topology.clab.yml> --cluster <cluster.yaml> [--strategy bin-pack]
    clab-automator destroy <topology.clab.yml> [--cluster <cluster.yaml>] [--keep-lab-dir]
    clab-automator save <topology.clab.yml> [--cluster <cluster.yaml>]
    clab-automator inspect [<topology.clab.yml>] [--cluster <cluster.yaml>]
    clab-automator status [--cluster <cluster.yaml>]
    clab-automator export-live <devices.yaml> [-o output.clab.yml]
    clab-automator gui [--cluster <cluster.yaml>] [--dir DIR ...] [--port 8650]

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
from .exporter import export_from_live_network
from .topology import dump_yaml


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clab-automator",
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

    # --- status ---
    p_status = sub.add_parser("status", help="Show containerlab version and resources per host")
    add_cluster_arg(p_status)

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

    cluster = _cluster_from_args(args)

    if cmd == "gui":
        return _gui(args, cluster)

    if cmd == "status":
        return _status(cluster)

    deployer = LabDeployer(cluster)
    if cmd == "deploy":
        summary = deployer.deploy(
            args.topology,
            strategy=args.strategy,
            reconfigure=args.reconfigure,
            dry_run=args.dry_run,
            output_dir=args.output_dir,
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
    return 1 if failed else 0


def _gui(args: argparse.Namespace, cluster: ClusterConfig) -> int:
    try:
        from .gui.server import run
        from .gui.state import Workspace
    except ImportError as exc:
        raise RuntimeError(
            f"The GUI needs extra packages ({exc.name}). "
            "Install them with: pip install 'clab-automator[gui]'"
        ) from exc
    roots = [Path(d).expanduser() for d in (args.dir or ["."])]
    for root in roots:
        if not root.is_dir():
            raise FileNotFoundError(f"Not a directory: {root}")
    run(Workspace(cluster, roots), host=args.bind, port=args.port,
        open_browser=not args.no_browser)
    return 0


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
        if len(cluster.hosts) > 1:
            print(f"  VTEP: {host.vtep or 'NOT SET — set vtep_ip for cross-host links'}")
            print(f"  Tags: {', '.join(host.tags) or '(none)'}")
        print()
    return 1 if unreachable else 0


if __name__ == "__main__":
    sys.exit(main())
