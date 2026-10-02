"""Packet captures started from the GUI: a pcap download or a live decode tab.

Every GUI capture has a duration (capped) and an optional packet count
(capped), so a forgotten tab or download cannot capture forever. The web
server stops the capture as soon as the browser goes away.
"""

import time
from typing import Optional

from ..capture import (
    GUI_DEFAULT_DURATION,
    GUI_MAX_COUNT,
    GUI_MAX_DURATION,
    GUI_MAX_LIVE_DURATION,
    Capture,
    CaptureSpec,
    OnMessage,
    check_interface,
)
from ..topology import load_topology
from .state import Workspace


def _number(query, name: str, kind=int) -> Optional[float]:
    raw = str(query.get(name, "")).strip()
    if not raw:
        return None
    try:
        value = kind(raw)
    except ValueError:
        raise ValueError(f"'{name}' must be a number") from None
    if not value > 0:
        raise ValueError(f"'{name}' must be greater than zero")
    return value


def spec_from_query(query, fmt: str) -> CaptureSpec:
    """A capture spec from request parameters, with the GUI's limits applied.

    Parameters: node, iface, filter, count, duration (seconds), snaplen.
    """
    max_duration = GUI_MAX_DURATION if fmt == "pcap" else GUI_MAX_LIVE_DURATION
    duration = _number(query, "duration", float) or GUI_DEFAULT_DURATION
    count = _number(query, "count")
    if duration > max_duration:
        raise ValueError(f"duration is limited to {max_duration} seconds")
    if count is not None and count > GUI_MAX_COUNT:
        raise ValueError(f"count is limited to {GUI_MAX_COUNT} packets")
    return CaptureSpec(
        node=str(query.get("node", "")), interface=str(query.get("iface", "")), format=fmt,
        bpf_filter=str(query.get("filter", "")), count=count, duration=duration,
        snaplen=_number(query, "snaplen"),
    )


def open_capture(workspace: Workspace, topo_id: str, spec: CaptureSpec,
                 on_message: Optional[OnMessage] = None) -> tuple[Capture, str]:
    """Start a capture on a node of a workspace topology; returns it and the lab name.

    KeyError: unknown topology or node not running. ValueError: the node
    or interface is not in the topology. CaptureError: Docker or the helper
    image is not usable on the node's host.
    """
    topo = load_topology(workspace.topology_path(topo_id))
    check_interface(topo, spec.node, spec.interface)
    node = workspace.find_node(topo.name, spec.node)
    host = workspace.host(node["host"])
    capture = Capture(workspace.runner(host), node["container"], spec, host_sudo=host.sudo,
                      on_message=on_message)
    capture.start()
    return capture, topo.name


def pcap_filename(lab: str, spec: CaptureSpec) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{lab}-{spec.node}-{spec.interface}-{stamp}.pcap"
    # Interface names may contain '/' (and quotes would break the header)
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)
