"""CLI entrypoint for eve-ng-automator.

Usage:
    eve-ng-automator deploy <topology.yaml> [--start] [--dry-run]
    eve-ng-automator deploy <topology.yaml> --cluster <cluster.yaml> [--strategy bin-pack]
    eve-ng-automator teardown <lab-path> [--keep-lab] [--no-wipe]
    eve-ng-automator teardown --cluster <cluster.yaml> <topology.yaml>
    eve-ng-automator export <lab-path> [-o output.yaml] [--no-configs]
    eve-ng-automator export-live <devices.yaml> [-o output.yaml]
    eve-ng-automator cluster-status <cluster.yaml>
    eve-ng-automator list-templates
    eve-ng-automator list-labs [<folder>]
    eve-ng-automator status
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import yaml

from .api_client import EveNgClient
from .cluster import load_cluster_config, create_client, probe_host_resources
from .deployer import TopologyDeployer
from .distributed import DistributedDeployer
from .exporter import export_lab, export_from_live_network
from .teardown import teardown_lab, stop_lab


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eve-ng-automator",
        description="Automate EVE-NG lab deployment and teardown",
    )

    # Global connection options
    parser.add_argument("--host", default=os.environ.get("EVE_NG_HOST", ""),
                        help="EVE-NG host (or EVE_NG_HOST env var)")
    parser.add_argument("--username", default=os.environ.get("EVE_NG_USER", "admin"),
                        help="EVE-NG username (or EVE_NG_USER env var)")
    parser.add_argument("--password", default=os.environ.get("EVE_NG_PASS", "eve"),
                        help="EVE-NG password (or EVE_NG_PASS env var)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("EVE_NG_PORT", "443")))
    parser.add_argument("--no-ssl", action="store_true")
    parser.add_argument("--verify-ssl", action="store_true")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="Increase verbosity (-v, -vv)")

    sub = parser.add_subparsers(dest="command", required=True)

    # --- deploy ---
    p_deploy = sub.add_parser("deploy", help="Deploy a topology from YAML")
    p_deploy.add_argument("topology", help="Path to topology YAML file")
    p_deploy.add_argument("--start", action="store_true",
                          help="Start all nodes after deployment")
    p_deploy.add_argument("--dry-run", action="store_true",
                          help="Validate and show what would be created")
    p_deploy.add_argument("--cluster", metavar="CLUSTER_YAML",
                          help="Deploy across multiple hosts using cluster config")
    p_deploy.add_argument("--strategy", default="bin-pack",
                          choices=["bin-pack", "spread", "resource"],
                          help="Placement strategy for multi-host (default: bin-pack)")
    p_deploy.add_argument("--ssh-user", default="root",
                          help="SSH username for tunnel setup (default: root)")
    p_deploy.add_argument("--ssh-pass",
                          help="SSH password for tunnel setup")
    p_deploy.add_argument("--ssh-key",
                          help="SSH private key file for tunnel setup")

    # --- teardown ---
    p_tear = sub.add_parser("teardown", help="Tear down a lab")
    p_tear.add_argument("lab_path", help="Lab path (e.g. /my-lab) or topology YAML for cluster mode")
    p_tear.add_argument("--keep-lab", action="store_true",
                        help="Stop and wipe nodes but don't delete the lab")
    p_tear.add_argument("--no-wipe", action="store_true",
                        help="Don't wipe node NVRAM before deletion")
    p_tear.add_argument("--cluster", metavar="CLUSTER_YAML",
                        help="Tear down across multiple hosts using cluster config")
    p_tear.add_argument("--ssh-user", default="root",
                        help="SSH username for tunnel teardown")
    p_tear.add_argument("--ssh-pass", help="SSH password for tunnel teardown")
    p_tear.add_argument("--ssh-key", help="SSH private key file for tunnel teardown")

    # --- stop ---
    p_stop = sub.add_parser("stop", help="Stop all nodes in a lab")
    p_stop.add_argument("lab_path", help="Lab path")

    # --- export ---
    p_export = sub.add_parser("export", help="Export an EVE-NG lab to YAML")
    p_export.add_argument("lab_path", help="Lab path to export")
    p_export.add_argument("-o", "--output", help="Output YAML file")
    p_export.add_argument("--no-configs", action="store_true",
                          help="Skip exporting startup configs")

    # --- export-live ---
    p_live = sub.add_parser("export-live",
                            help="Export topology from live network devices")
    p_live.add_argument("devices", help="YAML file with device connection info")
    p_live.add_argument("-o", "--output", help="Output YAML file")
    p_live.add_argument("--lab-name", default="imported-topology",
                        help="Name for the generated lab")

    # --- cluster-status ---
    p_cs = sub.add_parser("cluster-status",
                          help="Show status of all hosts in a cluster")
    p_cs.add_argument("cluster_config", help="Path to cluster YAML")

    # --- list-templates ---
    sub.add_parser("list-templates", help="List available node templates")

    # --- list-labs ---
    p_labs = sub.add_parser("list-labs", help="List labs in a folder")
    p_labs.add_argument("folder", nargs="?", default="/", help="Folder path")

    # --- status ---
    sub.add_parser("status", help="Show EVE-NG server status")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Configure logging
    level = logging.WARNING
    if args.verbose >= 2:
        level = logging.DEBUG
    elif args.verbose >= 1:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Commands that use cluster config instead of a single --host
    cluster_commands = {"cluster-status"}
    uses_cluster = (
        args.command in cluster_commands
        or (args.command == "deploy" and getattr(args, "cluster", None))
        or (args.command == "teardown" and getattr(args, "cluster", None))
    )

    if uses_cluster:
        try:
            return _dispatch_cluster(args)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            if args.verbose >= 2:
                import traceback
                traceback.print_exc()
            return 1

    if not args.host:
        print("Error: --host is required (or set EVE_NG_HOST env var)", file=sys.stderr)
        return 1

    client = EveNgClient(
        host=args.host,
        username=args.username,
        password=args.password,
        port=args.port,
        ssl=not args.no_ssl,
        verify_ssl=args.verify_ssl,
    )

    try:
        with client:
            return _dispatch(args, client)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if args.verbose >= 2:
            import traceback
            traceback.print_exc()
        return 1


def _dispatch(args: argparse.Namespace, client: EveNgClient) -> int:
    cmd = args.command

    if cmd == "deploy":
        deployer = TopologyDeployer(client)
        summary = deployer.deploy_from_file(
            args.topology,
            start_nodes=args.start,
            dry_run=args.dry_run,
        )
        print(json.dumps(summary, indent=2, default=str))

    elif cmd == "teardown":
        summary = teardown_lab(
            client,
            args.lab_path,
            delete_lab=not args.keep_lab,
            wipe_nodes=not args.no_wipe,
        )
        print(json.dumps(summary, indent=2))

    elif cmd == "stop":
        stop_lab(client, args.lab_path)
        print(f"All nodes stopped in {args.lab_path}")

    elif cmd == "export":
        topo = export_lab(
            client,
            args.lab_path,
            output_file=args.output,
            include_configs=not args.no_configs,
        )
        if not args.output:
            print(yaml.dump(topo, default_flow_style=False, sort_keys=False))

    elif cmd == "export-live":
        devices_path = Path(args.devices)
        with open(devices_path) as fh:
            devices = yaml.safe_load(fh)
        if not isinstance(devices, list):
            devices = devices.get("devices", [])
        topo = export_from_live_network(
            devices,
            output_file=args.output,
            lab_name=args.lab_name,
        )
        if not args.output:
            print(yaml.dump(topo, default_flow_style=False, sort_keys=False))

    elif cmd == "list-templates":
        templates = client.list_templates()
        if isinstance(templates, dict):
            for name, info in sorted(templates.items()):
                desc = info if isinstance(info, str) else info.get("description", "")
                print(f"  {name:30s} {desc}")
        else:
            print(json.dumps(templates, indent=2))

    elif cmd == "list-labs":
        labs = client.list_labs(args.folder)
        if isinstance(labs, dict):
            for name, info in sorted(labs.items()):
                print(f"  {name}")
        elif isinstance(labs, list):
            for item in labs:
                print(f"  {item}")
        else:
            print(json.dumps(labs, indent=2))

    elif cmd == "status":
        status = client.status()
        print(json.dumps(status, indent=2))

    return 0


def _dispatch_cluster(args: argparse.Namespace) -> int:
    """Handle commands that operate on a cluster of EVE-NG hosts."""
    cmd = args.command

    if cmd == "cluster-status":
        cluster = load_cluster_config(args.cluster_config)
        print(f"Cluster: {len(cluster.hosts)} hosts, edition={cluster.edition}")
        if cluster.is_pro:
            print(f"  Mode: EVE-NG Pro (native clustering via master '{cluster.master.name}')")
        else:
            print(f"  Mode: Community (DIY tunnels: {cluster.tunnel_mode}, pnet={cluster.tunnel_pnet})")
        print()

        for host_info in cluster.hosts:
            client = create_client(host_info)
            try:
                client.login()
                probe_host_resources(client, host_info)
                status = client.status()
                client.logout()
                print(f"  {host_info.name} ({host_info.host})")
                print(f"    CPU: {host_info.available_cpu} available"
                      f" / {host_info.max_cpu} max")
                print(f"    RAM: {host_info.available_ram}MB available"
                      f" / {host_info.max_ram}MB max")
                print(f"    Tags: {host_info.tags or '(none)'}")
                if isinstance(status, dict):
                    ver = status.get("version", "unknown")
                    print(f"    Version: {ver}")
                print()
            except Exception as exc:
                print(f"  {host_info.name} ({host_info.host}): UNREACHABLE — {exc}")
                print()

    elif cmd == "deploy":
        cluster = load_cluster_config(args.cluster)
        deployer = DistributedDeployer(
            cluster,
            ssh_username=getattr(args, "ssh_user", "root"),
            ssh_password=getattr(args, "ssh_pass", None),
            ssh_key_file=getattr(args, "ssh_key", None),
        )
        summary = deployer.deploy(
            args.topology,
            strategy=args.strategy,
            start_nodes=args.start,
            dry_run=args.dry_run,
        )
        print(json.dumps(summary, indent=2, default=str))

    elif cmd == "teardown":
        cluster = load_cluster_config(args.cluster)
        deployer = DistributedDeployer(
            cluster,
            ssh_username=getattr(args, "ssh_user", "root"),
            ssh_password=getattr(args, "ssh_pass", None),
            ssh_key_file=getattr(args, "ssh_key", None),
        )
        summary = deployer.teardown(args.lab_path)
        print(json.dumps(summary, indent=2, default=str))

    return 0


if __name__ == "__main__":
    sys.exit(main())
