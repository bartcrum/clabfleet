"""Spare ports: ports a node has with nothing plugged in, to cable later.

In the topology file a spare port is a containerlab dummy link with the
label ``lab.spare`` (``topology.LABEL_SPARE``), so the node boots with the
interface whoever deploys the file. Two things are done on top of that:

- After a deploy the interface's carrier is turned off, so the node shows
  the port as not connected instead of up (``unplug``). Where the node's
  ``ip`` cannot do that (BusyBox), the port just shows as up.
- A cable between two spare ports of a running lab is made on the spot
  (``cable_live``): both dummy interfaces are removed and containerlab
  creates a veth pair in their place. The node keeps the port through
  this; on Arista cEOS it goes to not connected and back. The file gets
  an ordinary link for it, so a later deploy makes the same cable.
- A cable is pulled the other way round (``unplug_live``): the veth pair
  goes and each end gets its dummy interface back, carrier off, so that
  the two ports are spare ports again, as after a deploy of the file,
  which has two spare ports in place of the link.

A node only learns its ports when it boots: a port added to the file
appears at the next deploy, not on a running node.

Live cabling is for kinds whose ports are plain Linux interfaces of the
container (``LIVE_KINDS``). Kinds that run a VM inside the container bridge
their ports to it at start and would not see the change.
"""

import logging
from typing import Optional

from .ifmap import naming_for
from .nodes import run_docker
from .runner import CommandError, Runner
from .topology import Topology, canonical_kind

logger = logging.getLogger(__name__)

MAX_ADD = 64  # ports added to a node in one go
LIVE_KINDS = {"arista_ceos", "linux"}


def used_interfaces(topo: Topology, node: str) -> set[str]:
    """Every interface of ``node`` a link of the topology names, spare ones too."""
    return {ep.interface for link in topo.links for ep in link.endpoints
            if ep.node == node and ep.interface}


def next_ports(topo: Topology, node: str, count: int) -> list[str]:
    """Names for ``count`` more ports of ``node``: the kind's next free ones
    (eth7, eth8, ... or Ethernet1/3, ...). ValueError when the kind has no
    such number of ports left."""
    if node not in topo.nodes:
        raise KeyError(f"No node '{node}' in {topo.name}")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_ADD:
        raise ValueError(f"Add between 1 and {MAX_ADD} ports at a time")
    kind = topo.effective_node(node)["kind"] or ""
    used = {name.lower() for name in used_interfaces(topo, node)}
    found: list[str] = []
    for name in naming_for(kind).slots():
        if name.lower() not in used:
            found.append(name)
            if len(found) == count:
                return found
    raise ValueError(f"{node} ({kind}) has only {len(found)} more port"
                     f"{'' if len(found) == 1 else 's'} to give")


def live_problem(topo: Topology, node: str) -> Optional[str]:
    """Why a port of ``node`` cannot be cabled while the lab runs, or None."""
    kind = canonical_kind(topo.effective_node(node)["kind"] or "")
    if kind in LIVE_KINDS:
        return None
    return (f"{node} is of kind '{kind}', whose ports are not plain interfaces "
            "of its container")


def unplug(runner: Runner, containers: dict[str, str], ports: dict[str, list[str]],
           host_sudo: bool = False) -> list[str]:
    """Turn the carrier of spare ports off, so that their nodes show them
    as not connected. ``containers``: node -> container name, for the nodes
    on this host. Returns the ports ("node:iface") where it did not work;
    they show as up, which harms nothing."""
    failed = []
    for node, ifaces in ports.items():
        container = containers.get(node)
        if not container:
            continue
        for iface in ifaces:
            res = run_docker(runner, ["docker", "exec", container, "ip", "link", "set", iface,
                                      "carrier", "off"], host_sudo)
            if res.exit_code != 0:
                failed.append(f"{node}:{iface}")
    return failed


def port_exists(runner: Runner, container: str, iface: str, host_sudo: bool = False) -> bool:
    """Does the running container have this interface? A spare port added
    to the file after the node booted is not there, and the node would not
    notice a cable plugged into it."""
    res = run_docker(runner, ["docker", "exec", container, "ip", "link", "show", iface], host_sudo)
    return res.exit_code == 0


def unplug_live(runner: Runner, a: tuple[str, str], b: tuple[str, str],
                host_sudo: bool = False) -> list[str]:
    """Pull the cable between two ports of running containers on one host:
    ``a`` and ``b`` are (container, interface). The veth pair is removed
    (taking one end takes both) and each port gets a dummy interface with
    its carrier off, as a spare port has after a deploy. CommandError if
    the cable could not be removed. Returns the ports ("container:iface")
    left without an interface or with their carrier on: there the node
    shows the port as it likes, and a later live cable still works."""
    res = run_docker(runner, ["docker", "exec", a[0], "ip", "link", "del", a[1]], host_sudo)
    if res.exit_code != 0:
        raise CommandError(f"ip link del {a[1]} in {a[0]}", res.exit_code,
                           (res.stderr or res.stdout).strip() or "the interface is not there")
    # Gone with its other end; if not (it was no veth pair), take that too
    run_docker(runner, ["docker", "exec", b[0], "ip", "link", "del", b[1]], host_sudo)
    odd = []
    for container, iface in (a, b):
        steps = (["ip", "link", "add", iface, "type", "dummy"], ["ip", "link", "set", iface, "up"],
                 ["ip", "link", "set", iface, "carrier", "off"])
        if any(run_docker(runner, ["docker", "exec", container, *step], host_sudo).exit_code != 0
               for step in steps):
            odd.append(f"{container}:{iface}")
    return odd


def cable_live(runner: Runner, a: tuple[str, str], b: tuple[str, str],
               host_sudo: bool = False) -> None:
    """Cable two ports of running containers on one host: ``a`` and ``b``
    are (container, interface). What is there under those names (the spare
    port's dummy interface, or nothing after an earlier unplug) goes, and
    containerlab makes the veth pair. Raises what containerlab says if it
    cannot."""
    for container, iface in (a, b):
        # No such interface is fine: the port was unplugged before
        run_docker(runner, ["docker", "exec", container, "ip", "link", "del", iface], host_sudo)
    runner.containerlab(["tools", "veth", "create", "-a", f"{a[0]}:{a[1]}",
                         "-b", f"{b[0]}:{b[1]}"])
