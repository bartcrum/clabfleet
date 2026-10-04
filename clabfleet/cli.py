"""CLI entrypoint for clabfleet.

Usage:
    clabfleet deploy <topology.clab.yml> [--reconfigure] [--dry-run] [--output-dir DIR]
                     [--pull] [--skip-image-check] [--skip-link-check] [--rollback]
                     [--wait [--wait-timeout SECONDS]]
    clabfleet deploy <topology.clab.yml> --cluster <cluster.yaml> [--strategy bin-pack]
    clabfleet destroy <topology.clab.yml> [--cluster <cluster.yaml>] [--keep-lab-dir]
    clabfleet save <topology.clab.yml> [--cluster <cluster.yaml>]
    clabfleet snapshot <topology.clab.yml> [--nodes GLOB] [--dir DIR] [--no-save] [--name NAME]
    clabfleet snapshot <topology.clab.yml> --list [--dir DIR]
    clabfleet diff <topology.clab.yml> [--nodes GLOB] [--from SNAPSHOT]
                   [--against previous|startup|latest|SNAPSHOT] [--json]
    clabfleet inspect [<topology.clab.yml>] [--cluster <cluster.yaml>]
    clabfleet exec <topology.clab.yml> <command> [--nodes GLOB] [--mode auto|cli|shell|ssh] [--json]
    clabfleet capture <topology.clab.yml> <node>:<iface> [-w FILE.pcap|-] [-f FILTER]
                      [-c COUNT] [--duration SECONDS] [--snaplen BYTES] [--via auto|node|helper]
    clabfleet status [--cluster <cluster.yaml>]
    clabfleet validate <topology.clab.yml>... [--cluster <cluster.yaml>] [--strict]
    clabfleet routing <topology.clab.yml> [--protocol ospf|bgp|evpn|mlag] [--live [--cluster <cluster.yaml>]] [--json]
    clabfleet export-live [<inventory>] [-o output.clab.yml] [--netbox URL | --nautobot URL
                          | --ansible FILE] [--filter KEY=VALUE] [--allowed-network CIDR]
                          [--sanitise [--allow-residual] | --no-sanitise]
                          [--include-neighbours] [--apply [--prune] | --overwrite] [--json]
    clabfleet new <template> [--spines N ...] [--kind KIND] [--image IMAGE] [-o FILE] [--force]
    clabfleet new --list
    clabfleet gui [--cluster <cluster.yaml>] [--dir DIR ...] [--port 8650]
                  [--bind ADDR] [--tls-cert CERT --tls-key KEY] [--users FILE | --single-token]
    clabfleet user add|passwd|list|remove|rotate [NAME] [--role operator|viewer] [--token]

Without --cluster, commands target a single host: this machine by default,
or a remote server over SSH with --host.
"""

import argparse
import getpass
import ipaddress
import json
import logging
import os
import signal
import socket
import sys
from pathlib import Path

import yaml

from .capture import (
    DEFAULT_HELPER_IMAGE,
    Capture,
    CaptureSpec,
    check_interface,
    find_container,
    parse_target,
)
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
from .runner import HOST_KEY_POLICIES
from .exporter import (
    ExportOptions,
    build_topology,
    collect_devices,
    load_report,
    pinned_from_report,
    report_path,
    write_result,
)
from .inventory import load_inventory, printable
from .resync import apply_diff, diff_topology, was_sanitised
from .routing import routing_view
from .routing.live import collect_lab
from .routing.report import PROTOCOLS, format_report
from .sanitise import SanitiseOptions
from .templates import (
    DEFAULT_KIND,
    DEFAULT_LINK_SUBNET,
    DEFAULT_LOOPBACK_SUBNET,
    KINDS,
    TEMPLATES,
    describe_templates,
    generate,
    render,
)
from .snapshots import Snapshotter, diff_lab, list_snapshots
from .topology import dump_yaml, load_topology
from .validate import validate_topology


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' is not a whole number") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


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
    parser.add_argument("--host-key-policy", choices=HOST_KEY_POLICIES,
                        default=os.environ.get("CLAB_HOST_KEY_POLICY", "accept-new"),
                        help="SSH host keys of --host: accept-new remembers a new key in "
                             "~/.clabfleet/known_hosts, strict only accepts known keys "
                             "(default: accept-new, or CLAB_HOST_KEY_POLICY)")
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

    def add_nodes_arg(p):
        p.add_argument("--nodes", action="append", metavar="GLOB",
                       help="Only nodes matching this glob (repeatable or "
                            "comma-separated, e.g. 'leaf*,spine1')")

    def add_snapshot_dir_arg(p):
        p.add_argument("--dir", dest="snapshot_dir", metavar="DIR",
                       help="Snapshot folder; each lab gets DIR/<lab>/ "
                            "(default: snapshots/ next to the topology)")

    # --- snapshot ---
    p_snap = sub.add_parser(
        "snapshot", help="Save the lab's configs and copy them to a local snapshot",
        description="Run save, then copy each node's saved config to "
                    "<dir>/<lab>/<UTC time>/<node>.cfg (dir defaults to "
                    "snapshots/ next to the topology).")
    p_snap.add_argument("topology", help="Topology file the lab was deployed from")
    add_cluster_arg(p_snap)
    add_nodes_arg(p_snap)
    add_snapshot_dir_arg(p_snap)
    p_snap.add_argument("--no-save", action="store_true",
                        help="Copy the configs saved last time without saving again")
    p_snap.add_argument("--name", help="Snapshot name (default: the UTC time)")
    p_snap.add_argument("--list", action="store_true",
                        help="List the lab's snapshots instead of taking one")
    p_snap.add_argument("--json", action="store_true", help="Print the result as JSON")

    # --- diff ---
    p_diff = sub.add_parser(
        "diff", help="Diff a config snapshot against another or the startup-config",
        description="Unified diff of a snapshot's configs. Exit code: 0 no "
                    "differences, 1 differences, 2 error.")
    p_diff.add_argument("topology", help="Topology file of the lab")
    add_nodes_arg(p_diff)
    add_snapshot_dir_arg(p_diff)
    p_diff.add_argument("--from", dest="from_snapshot", default="latest", metavar="SNAPSHOT",
                        help="Snapshot to look at (default: latest)")
    p_diff.add_argument("--against", default="previous",
                        metavar="previous|startup|latest|SNAPSHOT",
                        help="What to compare it with: the snapshot before it "
                             "(default), the topology's startup-config, the latest "
                             "snapshot or a named one")
    p_diff.add_argument("--json", action="store_true", help="Print results as JSON")

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
    pw = p_exec.add_mutually_exclusive_group()
    pw.add_argument("--password", dest="node_password",
                    help="SSH password on the nodes (default: CLAB_NODE_PASSWORD or admin). "
                         "Visible to other users in ps and kept in shell history: prefer "
                         "CLAB_NODE_PASSWORD or --ask-password")
    pw.add_argument("--ask-password", action="store_true",
                    help="Prompt for the nodes' SSH password")
    p_exec.add_argument("--parallel", type=int, default=8,
                        help="Nodes to run on at once (default: 8)")
    p_exec.add_argument("--timeout", type=float, default=60,
                        help="SSH connect/command timeout in seconds (default: 60)")
    p_exec.add_argument("--json", action="store_true", help="Print results as JSON")

    # --- capture ---
    p_cap = sub.add_parser(
        "capture", help="Capture packets on a lab node interface",
        description="Run tcpdump on a node interface and print a live decode, or write a "
                    "pcap with -w. Stream into Wireshark with: "
                    "clabfleet capture lab.clab.yml r1:eth1 -w - | wireshark -k -i -")
    p_cap.add_argument("topology", help="Topology file the lab was deployed from")
    p_cap.add_argument("target", metavar="node:iface", help="Node and interface, e.g. spine1:eth1")
    add_cluster_arg(p_cap)
    p_cap.add_argument("-w", "--write", metavar="FILE",
                       help="Write a pcap to FILE ('-' for stdout) instead of a text decode")
    p_cap.add_argument("-f", "--filter", default="", metavar="FILTER",
                       help="BPF capture filter, e.g. 'tcp port 179' (quote it)")
    p_cap.add_argument("-c", "--count", type=int, help="Stop after this many packets")
    p_cap.add_argument("--duration", type=float, metavar="SECONDS",
                       help="Stop after this many seconds")
    p_cap.add_argument("--snaplen", type=int, metavar="BYTES",
                       help="Bytes to keep per packet (default: tcpdump's, 262144)")
    p_cap.add_argument("--via", default="auto", choices=["auto", "node", "helper"],
                       help="auto (default): the node's own tcpdump if it has one, else a "
                            "helper container in the node's network namespace")
    p_cap.add_argument("--helper-image", default=os.environ.get("CLAB_CAPTURE_IMAGE"),
                       metavar="IMAGE",
                       help=f"Image with tcpdump for --via helper (default: "
                            f"{DEFAULT_HELPER_IMAGE}, or CLAB_CAPTURE_IMAGE)")

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

    # --- routing ---
    p_routing = sub.add_parser(
        "routing", help="Show the OSPF, BGP and EVPN design of a topology's configs",
        description="Read the nodes' startup configs (no host is contacted) and print "
                    "the intended OSPF adjacencies, BGP sessions and EVPN overlay, "
                    "with any inconsistencies found between the nodes.")
    p_routing.add_argument("topology", help="Topology file")
    p_routing.add_argument("--protocol", action="append", choices=PROTOCOLS,
                           help="Only this protocol (repeatable)")
    p_routing.add_argument("--live", action="store_true",
                           help="Also ask the running nodes for their protocol state "
                                "(read-only show commands) and compare")
    add_cluster_arg(p_routing)
    p_routing.add_argument("--json", action="store_true", help="Print the full view as JSON")

    # --- export-live ---
    p_live = sub.add_parser(
        "export-live", help="Build a topology from live network devices (NAPALM)",
        description="Connect to real devices with NAPALM and build a containerlab "
                    "topology from their configs and LLDP neighbours. If the output "
                    "file exists, report what changed instead of overwriting it.")
    p_live.add_argument("devices", nargs="?", metavar="inventory",
                        help="Device inventory: clabfleet YAML (devices:, defaults:, "
                             "source:, kinds:, images:) or an Ansible inventory (YAML/INI)")
    p_live.add_argument("-o", "--output", help="Output topology file "
                        "(configs are written to configs/ next to it, plus an "
                        "<name>.import-report.yaml)")
    p_live.add_argument("--lab-name", default="imported-topology",
                        help="Name for the generated lab")
    src = p_live.add_argument_group("device sources (instead of or with the inventory file)")
    src.add_argument("--netbox", metavar="URL",
                     help="Take devices from NetBox (token in NETBOX_TOKEN)")
    src.add_argument("--nautobot", metavar="URL",
                     help="Take devices from Nautobot (token in NAUTOBOT_TOKEN)")
    src.add_argument("--ansible", metavar="INVENTORY",
                     help="Take devices from an Ansible inventory file (YAML or INI)")
    src.add_argument("--filter", action="append", metavar="KEY=VALUE", default=[],
                     help="NetBox/Nautobot query filter, e.g. site=dc1, role=leaf, tag=lab "
                          "(repeatable); for Ansible only group=NAME")
    src.add_argument("--token-env", metavar="VAR",
                     help="Environment variable holding the NetBox/Nautobot token")
    src.add_argument("--allow-http", action="store_true",
                     help="Allow a plain http:// NetBox/Nautobot URL (the token is sent "
                          "unencrypted)")
    src.add_argument("--allowed-network", action="append", metavar="CIDR", default=[],
                     help="Only connect to devices whose address is in this network "
                          "(repeatable; added to allowed_networks: in the inventory)")
    conv = p_live.add_argument_group("conversion")
    conv.add_argument("--keep-interface-names", action="store_true",
                      help="Use the devices' interface names as they are instead of "
                           "mapping them to each kind's naming")
    conv.add_argument("--include-neighbours", action="store_true",
                      help="Add LLDP neighbours that are not in the inventory as "
                           "placeholder linux nodes")
    conv.add_argument("--neighbour-image", default="alpine:3", metavar="IMAGE",
                      help="Image for neighbour placeholders (default: alpine:3)")
    conv.add_argument("--sanitise", "--sanitize", action="store_true",
                      help="Remove or replace secrets, AAA/SNMP config and management "
                           "addresses in the saved configs (best effort: review them "
                           "before sharing)")
    conv.add_argument("--no-sanitise", "--no-sanitize", action="store_true",
                      help="Save configs as they are, even over a sanitised import")
    conv.add_argument("--allow-residual", action="store_true",
                      help="With --sanitise: write configs even if they still look like "
                           "they hold secrets")
    conv.add_argument("--mgmt-address", choices=["remove", "dhcp", "keep"], default="remove",
                      help="With --sanitise: what to do with management interface "
                           "addresses (default: remove)")
    conv.add_argument("--lab-user", default="admin",
                      help="With --sanitise: placeholder login added to configs "
                           "(default: admin)")
    conv.add_argument("--lab-password", default="admin",
                      help="With --sanitise: its password (default: admin)")
    sync = p_live.add_argument_group("re-sync (when the output file exists)")
    sync.add_argument("--apply", action="store_true",
                      help="Apply the reported changes to the existing topology "
                           "(hand edits are kept)")
    sync.add_argument("--prune", action="store_true",
                      help="With --apply: also delete nodes and links that are gone")
    sync.add_argument("--overwrite", action="store_true",
                      help="Replace the existing topology with a fresh import")
    sync.add_argument("--json", action="store_true", help="Print the change report as JSON")

    # --- new ---
    p_new = sub.add_parser("new", help="Generate a topology from a lab template",
                           description="Generate a ready-to-deploy topology "
                                       "(nodes, links, startup configs) from a template.")
    p_new.add_argument("--list", action="store_true", dest="list_templates",
                       help="List templates, their options and the supported kinds")
    templates = p_new.add_subparsers(dest="template", metavar="template")
    for tmpl in TEMPLATES.values():
        p_tmpl = templates.add_parser(tmpl.name, help=tmpl.description,
                                      description=tmpl.description)
        for param in tmpl.params:
            p_tmpl.add_argument(f"--{param.name}", type=int, metavar="N",
                                help=f"{param.help} (default: {param.default})")
        p_tmpl.add_argument("--kind", default=DEFAULT_KIND, choices=list(KINDS),
                            help=f"Node kind (default: {DEFAULT_KIND})")
        p_tmpl.add_argument("--image", help="Node image (default: per kind, see --list)")
        p_tmpl.add_argument("--name", help=f"Lab name (default: {tmpl.default_name})")
        p_tmpl.add_argument("--link-subnet", default=DEFAULT_LINK_SUBNET,
                            help=f"Subnet the /31 link addresses come from "
                                 f"(default: {DEFAULT_LINK_SUBNET})")
        p_tmpl.add_argument("--loopback-subnet", default=DEFAULT_LOOPBACK_SUBNET,
                            help=f"Subnet for /32 loopbacks, one /24 per tier "
                                 f"(default: {DEFAULT_LOOPBACK_SUBNET})")
        p_tmpl.add_argument("-o", "--output", metavar="FILE",
                            help="Write the topology here (default: print it)")
        p_tmpl.add_argument("--force", action="store_true",
                            help="Overwrite FILE if it exists")

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
    p_gui.add_argument("--users", metavar="FILE",
                       help="Users file for named logins with roles (default: "
                            "~/.clabfleet/users.yaml; when it does not exist yet, it "
                            "is created with the user admin and a random password, "
                            "printed at start-up, to be changed at the first login)")
    p_gui.add_argument("--single-token", action="store_true",
                       help="No users: print a random token at start-up and let "
                            "whoever has it in as an operator")
    p_gui.add_argument("--audit-log", metavar="FILE",
                       help="JSON Lines log of logins, jobs, edits and terminal "
                            "sessions (default with users: audit.jsonl next to the "
                            "users file)")
    p_gui.add_argument("--tls-cert", metavar="CERT", help="Serve HTTPS with this certificate (PEM)")
    p_gui.add_argument("--tls-key", metavar="KEY", help="Private key for --tls-cert (PEM)")
    p_gui.add_argument("--insecure-http", action="store_true",
                       help="Allow --bind to a non-loopback address without TLS "
                            "(tokens and terminal traffic travel in clear text)")
    p_gui.add_argument("--public-url", metavar="URL",
                       help="Address browsers use to reach the GUI, if it is not the "
                            "one it listens on (e.g. https://lab.example.com behind a "
                            "TLS proxy)")
    p_gui.add_argument("--max-sessions", type=_positive_int, default=64, metavar="N",
                       help="Open terminal tabs (shells, CLIs, logs) allowed in all "
                            "(default: 64)")
    p_gui.add_argument("--max-user-sessions", type=_positive_int, default=16, metavar="N",
                       help="Open terminal tabs allowed per user (default: 16)")

    # --- user ---
    p_user = sub.add_parser("user", help="Manage named GUI users and their logins")
    user_sub = p_user.add_subparsers(dest="user_command", required=True)

    def add_users_arg(p):
        p.add_argument("--users", metavar="FILE",
                       help="Users file (default: ~/.clabfleet/users.yaml)")

    def add_url_arg(p):
        p.add_argument("--url", metavar="URL",
                       help="GUI address for the printed login link "
                            "(default: https://<this host's name>:8650)")

    def add_password_arg(p):
        p.add_argument("--password-stdin", action="store_true",
                       help="Read the password from the first line of stdin instead of "
                            "asking (for scripts)")

    u_add = user_sub.add_parser("add", help="Add a user who logs in with a name and password")
    u_add.add_argument("name")
    u_add.add_argument("--role", default="operator", choices=["operator", "viewer"],
                       help="operator: everything (default); viewer: read-only")
    u_add.add_argument("--token", action="store_true",
                       help="Give the user a login token (printed once, with a login link) "
                            "instead of a password")
    add_password_arg(u_add)
    add_users_arg(u_add)
    add_url_arg(u_add)
    u_passwd = user_sub.add_parser("passwd", help="Set a user's password (ends their sessions)")
    u_passwd.add_argument("name")
    add_password_arg(u_passwd)
    add_users_arg(u_passwd)
    u_list = user_sub.add_parser("list", help="List users")
    add_users_arg(u_list)
    u_remove = user_sub.add_parser("remove", help="Remove a user (ends their sessions)")
    u_remove.add_argument("name")
    add_users_arg(u_remove)
    u_rotate = user_sub.add_parser("rotate", help="Give a user a new login token "
                                                  "(ends their sessions)")
    u_rotate.add_argument("name")
    add_users_arg(u_rotate)
    add_url_arg(u_rotate)

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
        host_key_policy=args.host_key_policy,
    )])


def _dispatch(args: argparse.Namespace) -> int:
    cmd = args.command

    if cmd == "export-live":
        return _export_live(args)

    if cmd == "validate":
        return _validate(args)

    if cmd == "routing" and not args.live:
        return _routing(args, None)

    if cmd == "new":
        return _new(args)

    if cmd == "diff":
        return _diff(args)

    if cmd == "snapshot" and args.list:
        return _list_snapshots(args)

    if cmd == "user":
        return _user(args)

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

    if cmd == "routing":
        return _routing(args, cluster)

    if cmd == "snapshot":
        return _snapshot(args, cluster)

    if cmd == "capture":
        return _capture(args, cluster)

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
    from .gui.auth import AUDIT_FILE_NAME, AuditLog

    roots = [Path(d).expanduser() for d in (args.dir or ["."])]
    for root in roots:
        if not root.is_dir():
            raise FileNotFoundError(f"Not a directory: {root}")

    ssl_context = None
    if bool(args.tls_cert) != bool(args.tls_key):
        raise ValueError("--tls-cert and --tls-key go together")
    if args.tls_cert:
        import ssl
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_context.load_cert_chain(args.tls_cert, args.tls_key)
    if not _is_loopback(args.bind) and not ssl_context:
        if not args.insecure_http:
            raise ValueError(
                f"Refusing to listen on {args.bind} without TLS: login tokens and terminal "
                "sessions would cross the network in clear text. Use --tls-cert/--tls-key, "
                "or --insecure-http if the network is trusted.")
        print(f"WARNING: listening on {args.bind} over plain HTTP (--insecure-http): "
              "tokens and terminal traffic are not encrypted.", file=sys.stderr)

    if args.single_token and args.users:
        raise ValueError("--single-token and --users exclude each other")
    users = None
    if not args.single_token:
        users = _user_store(args, must_exist=bool(args.users), bootstrap=not args.users)
        users.validate()
    audit_path = args.audit_log or (users.path.parent / AUDIT_FILE_NAME if users else None)
    run(Workspace(cluster, roots), host=args.bind, port=args.port,
        open_browser=not args.no_browser, users=users, audit=AuditLog(audit_path),
        ssl_context=ssl_context, public_url=args.public_url,
        max_sessions=args.max_sessions, max_user_sessions=args.max_user_sessions)
    return 0


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a host name: could resolve to anything


def _user_store(args: argparse.Namespace, must_exist: bool = True, bootstrap: bool = False):
    """The users file from --users or the default; None if the default does
    not exist and ``must_exist`` is false. With ``bootstrap``, a missing
    file is created with the first user (admin / admin, to be changed)."""
    from .gui.auth import DEFAULT_USERS_FILE, UserStore

    path = Path(args.users or DEFAULT_USERS_FILE).expanduser()
    if bootstrap and UserStore(path).bootstrap():
        print(f"Created {path} with the first user, admin, and a random password "
              "(shown below; it must be changed at the first login)", file=sys.stderr)
    if not must_exist and not path.exists():
        return None
    return UserStore(path)


def _user(args: argparse.Namespace) -> int:
    from .gui.auth import login_link

    store = _user_store(args)
    cmd = args.user_command
    if cmd == "list":
        users = store.validate() if store.path.exists() else {}
        if not users:
            print(f"No users in {store.path}")
            return 0
        width = max(len(n) for n in users)
        for name, user in sorted(users.items()):
            logins = "+".join(m for m, on in (("password", user.password),
                                              ("token", user.token_sha256)) if on)
            print(f"{name:<{width}}  {user.role:<8}  {logins:<14}  created {user.created}")
        return 0
    if cmd == "remove":
        store.remove(args.name)
        print(f"Removed user '{args.name}' from {store.path}")
        return 0
    if cmd == "passwd":
        if args.name not in (store.validate() if store.path.exists() else {}):
            raise KeyError(f"No user '{args.name}'")  # before asking for a password
        store.set_password(args.name, _new_password(args))
        print(f"Password set for '{args.name}'; their sessions have ended")
        return 0
    if cmd == "add" and not args.token:
        if store.path.exists() and args.name in store.validate():
            raise ValueError(f"User '{args.name}' already exists (use 'clabfleet user passwd')")
        store.add(args.name, args.role, password=_new_password(args))
        base = args.url or f"https://{socket.gethostname()}:8650"
        print(f"Added {args.role} '{args.name}' to {store.path}")
        print(f"They log in at {base}/ with their name and this password.")
        return 0

    if cmd == "add":
        token = store.add(args.name, args.role)
        print(f"Added {args.role} '{args.name}' to {store.path}")
    else:  # rotate
        token = store.rotate(args.name)
        print(f"New token for '{args.name}'; the old one and its sessions no longer work")
    base = args.url or f"https://{socket.gethostname()}:8650"
    print(f"\nToken (shown only once, it is not stored):\n\n    {token}\n")
    print(f"Login link (adjust the address to where the GUI runs):\n\n"
          f"    {login_link(base, token)}\n")
    return 0


def _new_password(args: argparse.Namespace) -> str:
    """A new password from stdin (--password-stdin) or asked twice."""
    from .gui.auth import check_new_password

    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass(f"Password for {args.name}: ")
        if getpass.getpass("Again: ") != password:
            raise ValueError("The passwords do not match")
    check_new_password(password)
    return password


def _exec(args: argparse.Namespace, cluster: ClusterConfig) -> int:
    password = args.node_password
    if args.ask_password:
        password = getpass.getpass("SSH password for the nodes: ")
    executor = LabExecutor(cluster, ssh_user=args.node_user, ssh_password=password,
                           parallel=args.parallel, timeout=args.timeout)
    out = executor.run(args.topology, " ".join(args.cmd), nodes=_node_patterns(args),
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


def _new(args: argparse.Namespace) -> int:
    if args.list_templates or not args.template:
        print(describe_templates())
        return 0 if args.list_templates else 2
    params = {p.name: getattr(args, p.name) for p in TEMPLATES[args.template].params}
    data = generate(args.template, params, kind=args.kind, image=args.image,
                    name=args.name, link_subnet=args.link_subnet,
                    loopback_subnet=args.loopback_subnet)
    text = render(data, args.template, params, args.kind)
    if not args.output:
        print(text, end="")
        return 0
    out = Path(args.output)
    if out.exists() and not args.force:
        raise FileExistsError(f"{out} already exists (use --force to overwrite)")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    topo = data["topology"]
    print(f"Wrote {out}: lab '{data['name']}', {len(topo['nodes'])} {args.kind} nodes, "
          f"{len(topo['links'])} links")
    return 0


def _node_patterns(args: argparse.Namespace) -> list[str] | None:
    return [p.strip() for arg in args.nodes or [] for p in arg.split(",") if p.strip()] or None


def _snapshot(args: argparse.Namespace, cluster: ClusterConfig) -> int:
    out = Snapshotter(cluster).take(
        args.topology, nodes=_node_patterns(args), directory=args.snapshot_dir,
        save=not args.no_save, name=args.name,
    )
    save_failed = {h: r["error"] for h, r in out["hosts"].items() if "error" in r}
    if args.json:
        print(json.dumps(out, indent=2))
        return 1 if save_failed else 0

    for host, err in save_failed.items():
        print(f"warning: save failed on {host}: {err}", file=sys.stderr)
    multi = len(cluster.hosts) > 1
    print(f"Snapshot {out['snapshot']} of lab '{out['lab']}': {out['path']}")
    for node, info in out["nodes"].items():
        print(f"  {node:<20} {info['file']}" + (f"  ({info['host']})" if multi else ""))
    for node, reason in out["skipped"].items():
        print(f"  {node:<20} skipped: {reason}")
    return 1 if save_failed else 0


def _list_snapshots(args: argparse.Namespace) -> int:
    topo = load_topology(args.topology)
    snapshots = list_snapshots(topo, args.snapshot_dir)
    if args.json:
        print(json.dumps(snapshots, indent=2))
        return 0
    if not snapshots:
        print(f"Lab '{topo.name}' has no snapshots.")
        return 0
    for snap in snapshots:
        skipped = len(snap.get("skipped") or {})
        print(f"{snap['name']:<24} {snap.get('taken_at', '?'):<26} "
              f"{len(snap['nodes'])} nodes" + (f", {skipped} skipped" if skipped else ""))
    return 0


def _diff(args: argparse.Namespace) -> int:
    """Exit 0 when nothing differs, 1 on differences, 2 on errors."""
    try:
        out = diff_lab(args.topology, nodes=_node_patterns(args), against=args.against,
                       from_snapshot=args.from_snapshot, directory=args.snapshot_dir)
    except Exception as exc:  # noqa: BLE001 - 2, not main()'s 1, so 1 means "differs"
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    rc = 1 if out["changed"] else 0
    if args.json:
        print(json.dumps(out, indent=2))
        return rc

    for r in out["nodes"]:
        if r["diff"]:
            sys.stdout.write(r["diff"])
    sys.stdout.flush()  # diffs before the summary when both go to a terminal
    for r in out["nodes"]:
        if r["status"] == "skipped":
            print(f"skipped {r['node']}: {r['reason']}", file=sys.stderr)
    compared = [r for r in out["nodes"] if r["status"] != "skipped"]
    print(f"{out['from']} vs {out['against']}: {out['changed']} of {len(compared)} "
          "nodes differ", file=sys.stderr)
    return rc
def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def _capture(args: argparse.Namespace, cluster: ClusterConfig) -> int:
    node, iface = parse_target(args.target)
    topo = load_topology(args.topology)
    iface = check_interface(topo, node, iface)
    spec = CaptureSpec(node, iface, format="pcap" if args.write else "text",
                       bpf_filter=args.filter, count=args.count, duration=args.duration,
                       snaplen=args.snaplen)
    host, container = find_container(cluster, topo, node)

    def message(line: str) -> None:
        print(line, file=sys.stderr, flush=True)

    to_file = args.write and args.write != "-"
    out = open(args.write, "wb") if to_file else sys.stdout.buffer
    # Stop tcpdump in the container on `kill` too, not only on Ctrl+C
    signal.signal(signal.SIGTERM, _raise_interrupt)
    runner = create_runner(host)
    capture = Capture(runner, container["container"], spec, host_sudo=host.sudo,
                      method=args.via, helper_image=args.helper_image, on_message=message)
    try:
        capture.start()
        where = f" on {host.name}" if len(cluster.hosts) > 1 else ""
        message(f"Capturing on {capture.describe()}{where}"
                + (f", writing {args.write}" if to_file else "")
                + ". Ctrl+C to stop.")
        while True:
            chunk = capture.read()
            if not chunk:
                break
            out.write(chunk)
            out.flush()
    except KeyboardInterrupt:
        capture.stop("interrupted")
    except BrokenPipeError:  # e.g. Wireshark closed
        capture.stop("output closed")
        # Nothing reads stdout any more: don't fail flushing it at exit
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    finally:
        capture.stop()
        runner.close()
        if to_file:
            out.close()
    if capture.stop_reason in ("interrupted", "output closed", "duration"):
        return 0
    return 0 if capture.exit_code in (0, None) else 1


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


def _routing(args: argparse.Namespace, cluster: ClusterConfig | None) -> int:
    topo = load_topology(args.topology)
    view = routing_view(topo)
    live = collect_lab(cluster, topo, view) if cluster is not None else None
    if args.json:
        print(json.dumps({**view, "live": live} if live else view, indent=2))
    else:
        print(format_report(view, args.protocol or PROTOCOLS, live), end="")
    return 0


def _export_live(args: argparse.Namespace) -> int:
    if not (args.devices or args.netbox or args.nautobot or args.ansible):
        raise ValueError("give an inventory file or --netbox, --nautobot or --ansible")
    if args.prune and not args.apply:
        raise ValueError("--prune only works with --apply")
    if args.sanitise and args.no_sanitise:
        raise ValueError("use --sanitise or --no-sanitise, not both")
    output = Path(args.output) if args.output else None
    if output is not None and output.exists() and (args.apply or args.overwrite) \
            and not (args.sanitise or args.no_sanitise) and was_sanitised(load_report(output)):
        raise ValueError(f"the last import into {output} was sanitised: add --sanitise "
                         "(or --no-sanitise to write the configs as they are)")
    filters: dict = {}
    for item in args.filter:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValueError(f"--filter wants KEY=VALUE, got {item!r}")
        filters.setdefault(key, []).append(value)
    filters = {k: v[0] if len(v) == 1 else v for k, v in filters.items()}

    inventory = load_inventory(args.devices, netbox=args.netbox, nautobot=args.nautobot,
                               ansible=args.ansible, filters=filters,
                               token_env=args.token_env, allow_http=args.allow_http,
                               allowed_networks=args.allowed_network)
    for msg in inventory.warnings:
        print(f"warning: {printable(msg)}", file=sys.stderr)
    if not inventory.devices:
        raise ValueError("the inventory has no devices")

    resync = output is not None and output.exists() and not args.overwrite
    if (args.apply or args.json) and not resync:
        raise ValueError("--apply and --json are for re-syncing an existing --output file")
    options = ExportOptions(
        lab_name=args.lab_name,
        kind_rules=inventory.kind_rules,
        image_rules=inventory.image_rules,
        map_interfaces=not args.keep_interface_names,
        sanitise=SanitiseOptions(mgmt=args.mgmt_address, user=args.lab_user,
                                 password=args.lab_password) if args.sanitise else None,
        allow_residual=args.allow_residual,
        include_neighbours=args.include_neighbours,
        neighbour_image=args.neighbour_image,
        inline_configs=output is None,
        pinned_interfaces=pinned_from_report(load_report(output)) if output else {},
    )
    if resync:
        options.lab_name = (yaml.safe_load(output.read_text()) or {}).get("name") \
            or args.lab_name

    collected = collect_devices(inventory.devices)
    if not collected:
        raise ValueError("no device could be reached")
    result = build_topology(collected, options)
    for msg in result.warnings:
        print(f"warning: {printable(msg)}", file=sys.stderr)
    if not args.sanitise and (not resync or args.apply):
        print("WARNING: --sanitise not given: the configs are saved as they are, with the "
              "network's passwords, keys and SNMP communities. Files are written readable "
              "by you only; do not share or commit them.", file=sys.stderr)

    if output is None:
        print(dump_yaml(result.topology))
        return 0
    if not resync:
        write_result(result, output)
        topo = result.topology["topology"]
        print(f"Wrote {output} ({len(topo['nodes'])} nodes, {len(topo['links'])} links); "
              f"report in {report_path(output)}")
        return 0

    diff = diff_topology(output, result)
    if args.json:
        print(json.dumps({"topology": str(output), "applied": args.apply and not diff.empty,
                          **diff.as_dict()}, indent=2))
    else:
        print(f"Changes since the last import of {output}:")
        print(diff.as_text())
    if diff.empty:
        return 0
    if not args.apply:
        if not args.json:
            print("\nNothing written. Re-run with --apply to update the topology "
                  "(add --prune to delete what is gone), or --overwrite to replace it.")
        return 0
    actions = apply_diff(output, result, diff, prune=args.prune)
    if not args.json:
        print()
        for action in actions:
            print(f"  {action}")
        skipped = len(diff.removed_nodes) + len(diff.removed_links)
        if skipped and not args.prune:
            print(f"  ({skipped} removed nodes/links kept; use --prune to delete them)")
    return 0


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
                try:
                    usage, usage_error = running_usage(inspect_all(runner)), None
                except Exception as exc:
                    usage, usage_error = {}, exc
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
        if usage_error is not None:
            print(f"  Labs: could not list ({usage_error})")
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
