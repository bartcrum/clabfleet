"""Live link and resource state of running lab nodes, for the GUI diagram.

Two probes, both read-only:

- interface state: one ``docker exec <container> sh -c ...`` per node that
  prints every interface's ``operstate``, ``flags`` (admin up or down) and
  ``ifalias`` from ``/sys/class/net``. That works on any Linux container with ``sh``,
  network OS containers included; a container without ``sh`` reports its
  links as unknown.
- resources: one ``docker stats --no-stream`` per host for all of a lab's
  running containers on it.

Topology files name interfaces the way the kind does (``Ethernet1`` on
cEOS, ``Ethernet0/1`` on IOL, ``ethernet-1/1`` on SR Linux) when
containerlab's interface aliases are used; ``linux_iface_names`` maps those
to the Linux names inside the container.

For VM-based kinds (vrnetlab images) the container's interface is the
VM's wire, not the VM's own port, so a port shut inside the VM still shows
as up here.
"""

import json
import logging
import re
import threading
import time
from typing import Callable, Optional

from .nodes import run_docker

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 5  # seconds per probe command on the host

# Prints "<name>\t<operstate>\t<flags>\t<rx bytes>\t<tx bytes>\t<ifalias>"
# per interface (the alias last: it may hold tabs)
_IFACE_SCRIPT = (
    'cd /sys/class/net || exit 1; for i in *; do '
    'printf "%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n" "$i" "$(cat "$i/operstate" 2>/dev/null)" '
    '"$(cat "$i/flags" 2>/dev/null)" "$(cat "$i/statistics/rx_bytes" 2>/dev/null)" '
    '"$(cat "$i/statistics/tx_bytes" 2>/dev/null)" "$(cat "$i/ifalias" 2>/dev/null)"; done'
)
IFF_UP = 0x1  # interface flag: administratively up

# Kernel operstate (RFC 2863) → what the diagram shows. "unknown" is what
# drivers without carrier reporting (loopback, tun, some virtual NICs) say.
_OPER_UP = {"up"}
_OPER_DOWN = {"down", "lowerlayerdown", "notpresent", "dormant"}


def oper_to_state(oper: str) -> str:
    """up, down or unknown for a kernel operstate string."""
    oper = (oper or "").strip().lower()
    if oper in _OPER_UP:
        return "up"
    if oper in _OPER_DOWN:
        return "down"
    return "unknown"


def parse_iface_states(text: str) -> dict[str, dict]:
    """Parse the interface probe's output: {name: {"oper", "admin_up", "rx",
    "tx", "alias"}}.

    ``admin_up`` is None when the flags could not be read, ``rx``/``tx``
    (byte counters) when the statistics could not.
    """
    def count(value):
        value = value.strip()
        return int(value) if value.isdigit() else None

    ifaces = {}
    for line in text.splitlines():
        parts = line.split("\t", 5)
        if len(parts) <= 4:  # name, oper, flags, alias: without counters
            parts = (parts + [""] * 3)[:3] + ["", ""] + parts[3:4]
        name, oper, flags, rx, tx, alias = (parts + [""] * 5)[:6]
        name = name.strip()
        if not name or name == "*":  # "*": the glob matched nothing
            continue
        try:
            admin_up = bool(int(flags.strip(), 16) & IFF_UP)
        except ValueError:
            admin_up = None
        ifaces[name] = {"oper": oper.strip().lower(), "admin_up": admin_up,
                        "rx": count(rx), "tx": count(tx), "alias": alias.strip()}
    return ifaces


# containerlab interface aliases per kind: pattern → Linux name builder
_ALIASES: dict[str, list[tuple[re.Pattern, Callable[[re.Match], str]]]] = {}


def _alias(kinds: tuple[str, ...], pattern: str, build: Callable[[re.Match], str]) -> None:
    for kind in kinds:
        _ALIASES.setdefault(kind, []).append((re.compile(pattern, re.IGNORECASE), build))


# cEOS: Ethernet1 → eth1, Ethernet1/2 → eth1_2
_alias(("arista_ceos", "ceos"), r"(?:ethernet|et)(\d+)(?:/(\d+))?",
       lambda m: f"eth{m[1]}" + (f"_{m[2]}" if m[2] else ""))
# IOL: Ethernet<slot>/<port> → eth<slot*4+port> (Ethernet0/0 is management)
_alias(("cisco_iol",), r"(?:ethernet|et|e)(\d+)/(\d+)",
       lambda m: f"eth{int(m[1]) * 4 + int(m[2])}")
# SR Linux: ethernet-1/1 → e1-1, ethernet-1/3/1 → e1-3-1
_alias(("nokia_srlinux", "srl"), r"ethernet-(\d+)/(\d+)(?:/(\d+))?",
       lambda m: f"e{m[1]}-{m[2]}" + (f"-{m[3]}" if m[3] else ""))

_LINUX_NAME = re.compile(r"eth\d+")


def linux_iface_names(kind: str, iface: str) -> tuple[list[str], bool]:
    """Names a topology endpoint's interface may have inside the container.

    Returns (candidates, sure): ``sure`` means a missing interface really
    is missing (a Linux-style name, or a known alias), rather than a naming
    scheme clabfleet does not know.
    """
    for pattern, build in _ALIASES.get(kind, []):
        m = pattern.fullmatch(iface)
        if m:
            return [build(m), iface], True
    return [iface], bool(_LINUX_NAME.fullmatch(iface))


def find_iface(ifaces: dict, kind: str, iface: str) -> Optional[str]:
    """The name a topology endpoint's interface has in the probe result."""
    candidates, _ = linux_iface_names(kind, iface)
    found = next((c for c in candidates if c in ifaces), None)
    if found is None:  # containerlab sets no ifalias, but other tools might
        found = next((n for n, i in ifaces.items() if i["alias"] == iface), None)
    return found


def endpoint_state(ifaces: Optional[dict], kind: str, iface: str) -> tuple[str, str]:
    """(up|down|unknown, detail) of one link end, given its node's probe result."""
    if ifaces is None:
        return "unknown", "interface state not available"
    _, sure = linux_iface_names(kind, iface)
    found = find_iface(ifaces, kind, iface)
    if found is None:
        return ("down", "interface missing") if sure else ("unknown", "interface not found")
    info = ifaces[found]
    state = oper_to_state(info["oper"])
    # Tell which end was shut: the other end of a veth just loses its carrier
    detail = info["oper"] or "unknown"
    if state == "down" and info["admin_up"] is False:
        detail = "admin down"
    elif state == "down" and info["oper"] in ("down", "lowerlayerdown"):
        detail = "no carrier"
    return state, detail if found == iface else f"{found} {detail}"


def link_states(links: list[dict], nodes: dict[str, dict]) -> dict[str, dict]:
    """State of each diagram link from its ends' interface states.

    ``links`` are ``topology_view`` links; ``nodes`` maps node name to
    {"kind", "state" (container state, "" if not deployed), "ifaces"}.
    A link is down if either lab-node end is down, unknown if an end
    cannot be told, else up. Special ends (host, macvlan, ...) are not
    probed.
    """
    result = {}
    for link in links:
        ends = {}
        for side in ("a", "b"):
            end = link[side]
            if not end.get("node"):
                continue
            node = nodes.get(end["node"]) or {}
            if not node.get("state"):
                state, detail = "unknown", "not deployed"
            elif node["state"] != "running":
                state, detail = "down", f"container {node['state']}"
            else:
                state, detail = endpoint_state(node.get("ifaces"), node.get("kind", ""),
                                               end["iface"])
            ends[side] = {"node": end["node"], "iface": end["iface"],
                          "state": state, "detail": detail}
            found = node.get("ifaces") and find_iface(node["ifaces"], node.get("kind", ""), end["iface"])
            if found:  # byte counters, for link_rates
                ends[side]["rx"] = node["ifaces"][found].get("rx")
                ends[side]["tx"] = node["ifaces"][found].get("tx")
        states = {e["state"] for e in ends.values()}
        overall = "down" if "down" in states else "unknown" if "unknown" in states or not ends \
            else "up"
        result[link["id"]] = {"state": overall, **ends}
    return result


def link_rates(previous: Optional[dict], current: dict) -> None:
    """Add each link's traffic, in bits per second, to ``current`` (a live
    snapshot) from the byte counters of the read before: ``"rate": {"ab",
    "ba"}``, a to b from a's transmit counter (else b's receive), b to a
    likewise. Nothing when a counter is missing or went back (a restart)."""
    if not previous or not previous.get("updated") or not current.get("updated"):
        return
    seconds = current["updated"] - previous["updated"]
    if seconds <= 0:
        return
    before = previous.get("links") or {}

    def delta(link_id, side, key):
        new = (current["links"][link_id].get(side) or {}).get(key)
        old = ((before.get(link_id) or {}).get(side) or {}).get(key)
        if new is None or old is None or new < old:
            return None
        return (new - old) * 8 / seconds

    for link_id in current.get("links") or {}:
        ab = delta(link_id, "a", "tx")
        ab = ab if ab is not None else delta(link_id, "b", "rx")
        ba = delta(link_id, "b", "tx")
        ba = ba if ba is not None else delta(link_id, "a", "rx")
        if ab is not None or ba is not None:
            current["links"][link_id]["rate"] = {"ab": round(ab or 0), "ba": round(ba or 0)}


# --- docker stats ------------------------------------------------------

_UNITS = {
    "b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4,
    "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4,
}


def parse_size(text: str) -> Optional[int]:
    """Bytes for a docker size string ("870.9MiB", "1.2kB", "0B"); None for "--"."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([kmgt]?i?b)\s*", text or "", re.IGNORECASE)
    if not m:
        return None
    try:
        return int(float(m[1]) * _UNITS[m[2].lower()])
    except (ValueError, KeyError):
        return None


def _percent(text: str) -> Optional[float]:
    try:
        return float(str(text).strip().rstrip("%"))
    except ValueError:
        return None


def parse_docker_stats(text: str) -> dict[str, dict]:
    """Parse ``docker stats --no-stream --format '{{json .}}'`` output.

    Returns {container name: {"cpu", "mem", "mem_limit", "mem_percent"}};
    values docker could not measure ("--", stopped containers) are None.
    """
    stats = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        name = str(row.get("Name") or row.get("Container") or "").lstrip("/")
        if not name:
            continue
        used, _, limit = str(row.get("MemUsage", "")).partition("/")
        stats[name] = {
            "cpu": _percent(row.get("CPUPerc", "")),
            "mem": parse_size(used),
            "mem_limit": parse_size(limit),
            "mem_percent": _percent(row.get("MemPerc", "")),
        }
    return stats


def probe_ifaces(runner, container: str, host_sudo: bool = False,
                 timeout: int = PROBE_TIMEOUT) -> Optional[dict]:
    """Interface states inside a container, or None if they cannot be read."""
    res = run_docker(runner, ["timeout", str(timeout), "docker", "exec", container,
                              "sh", "-c", _IFACE_SCRIPT], host_sudo)
    if res.exit_code != 0:
        logger.debug("Interface probe of %s failed: %s", container, res.stderr.strip()[-200:])
        return None
    return parse_iface_states(res.stdout)


def probe_stats(runner, containers: list[str], host_sudo: bool = False,
                timeout: int = PROBE_TIMEOUT) -> dict[str, dict]:
    """CPU and memory of containers on one host (one ``docker stats`` call)."""
    if not containers:
        return {}
    res = run_docker(runner, ["timeout", str(timeout), "docker", "stats", "--no-stream",
                              "--format", "{{json .}}", *containers], host_sudo)
    stats = parse_docker_stats(res.stdout)
    if res.exit_code != 0 and not stats:
        raise RuntimeError((res.stderr or res.stdout).strip()[-300:] or
                           f"docker stats exited {res.exit_code}")
    return stats


class LiveCache:
    """Latest live snapshot per lab, refreshed in the background on demand.

    Readers get the last snapshot at once. A lab is due for a refresh when
    its snapshot is ``interval`` seconds old, counted from when the last
    refresh finished, so however many browser tabs ask, a lab is probed at
    most once per interval and only while someone asks. ``claim`` makes
    sure only one refresh per lab is in flight.
    """

    def __init__(self, interval: float = 5.0, clock: Callable[[], float] = time.monotonic):
        self.interval = interval
        self.clock = clock
        self._seen: dict[str, tuple[dict, float]] = {}
        self._in_flight: set[str] = set()
        self._lock = threading.Lock()

    def get(self, key: str) -> tuple[Optional[dict], bool]:
        """(last snapshot or None, whether a refresh is due)."""
        with self._lock:
            hit = self._seen.get(key)
            if hit is None:
                return None, True
            snapshot, when = hit
            return snapshot, self.clock() - when >= self.interval

    def claim(self, key: str) -> bool:
        """True if the caller should refresh now (no refresh in flight)."""
        with self._lock:
            if key in self._in_flight:
                return False
            self._in_flight.add(key)
            return True

    def in_flight(self, key: str) -> bool:
        with self._lock:
            return key in self._in_flight

    def store(self, key: str, snapshot: dict) -> None:
        with self._lock:
            self._seen[key] = (snapshot, self.clock())
            self._in_flight.discard(key)

    def release(self, key: str) -> None:
        with self._lock:
            self._in_flight.discard(key)
