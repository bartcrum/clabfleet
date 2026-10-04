"""Live protocol state of a running lab, laid over the intended routing view.

Each running node is asked only about the protocols its startup config
uses, the cheapest way its kind allows:

- Arista cEOS: one ``docker exec`` running ``Cli`` once per command with
  ``| json``, each output preceded by a marker line so a failing command
  cannot shift the others.
- Cisco IOS / IOS-XE (IOL, CSR, Cat8kv): ``show`` commands over SSH to the
  management address, parsed from text.

:func:`overlay` then matches what the nodes report to the intended view
(:func:`clabfleet.routing.routing_view`): a state for every OSPF adjacency,
BGP session, VXLAN tunnel and MLAG pair, plus the sessions and neighbours that run but
were not in the startup configs (``extra``), usually changes made on the
CLI since the deploy.

Everything here only reads: no command changes a node.
"""

import ipaddress
import json
import logging
import re
import shlex
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..ifmap import canonical
from ..nodes import run_docker
from ..topology import canonical_kind

logger = logging.getLogger(__name__)

COLLECT_TIMEOUT = 20  # seconds per node

EOS_KINDS = {"arista_ceos"}
IOS_KINDS = {"cisco_iol", "cisco_csr1000v", "cisco_c8000v"}

EOS_COMMANDS = {
    "ospf": "show ip ospf neighbor vrf all",
    "bgp": "show ip bgp summary vrf all",
    "evpn": "show bgp evpn summary",
    "vxlan": "show vxlan vtep",
    # EOS lists only VTEPs it shares an L2 VNI with; peers reached only over
    # an L3 VNI show up as next hops ("via VTEP ...") of the VRFs' routes
    "vrf_vteps": "show ip route vrf all",
    "mlag": "show mlag",
}
IOS_COMMANDS = {
    "ospf": "show ip ospf neighbor",
    "bgp": "show ip bgp summary",
    "evpn": "show bgp l2vpn evpn summary",
    "vxlan": "show nve peers",
}
MARKER = "@@clabfleet "

UP_OSPF = {"full", "2way", "2-way"}


def family(kind: str) -> Optional[str]:
    """``eos`` / ``ios`` for kinds whose protocol state can be read, else None."""
    kind = canonical_kind(kind)
    if kind in EOS_KINDS:
        return "eos"
    if kind in IOS_KINDS:
        return "ios"
    return None


def wanted_topics(view: dict, node: str) -> list[str]:
    """Which protocols to ask a node about, from what its config runs."""
    topics = []
    if node in ((view.get("ospf") or {}).get("nodes") or {}):
        topics.append("ospf")
    bgp = view.get("bgp") or {}
    if node in (bgp.get("nodes") or {}):
        families = {f for s in bgp["sessions"] if node in (s["a"]["node"], s["b"]["node"])
                    for f in s["families"]}
        if "ipv4" in families or not families:
            topics.append("bgp")
        if "evpn" in families:
            topics.append("evpn")
    vtep = ((view.get("evpn") or {}).get("vteps") or {}).get(node)
    if vtep:
        topics.append("vxlan")
        if vtep.get("l3_vnis"):
            topics.append("vrf_vteps")  # EOS only; IOS's NVE peers include them
    if any(node in p["nodes"] for p in (view.get("mlag") or {}).get("pairs") or []):
        topics.append("mlag")  # EOS only
    return topics


# --- Collecting -----------------------------------------------------------------

def eos_script(topics: list[str]) -> str:
    """``sh`` script printing a marker and the JSON output of each command."""
    parts = []
    for topic in topics:
        parts.append(f"echo {shlex.quote(MARKER + topic)}")
        parts.append(f"Cli -p 15 -c {shlex.quote(EOS_COMMANDS[topic] + ' | json')} 2>&1")
    return "; ".join(parts)


def split_markers(text: str) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith(MARKER):
            current = line[len(MARKER):].strip()
            out[current] = []
        elif current is not None:
            out[current].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


@dataclass
class NodeState:
    """What one node reported (parsed), per topic."""
    family: str = ""
    collected: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    ospf: list[dict] = field(default_factory=list)
    bgp: dict[str, dict] = field(default_factory=dict)     # peer address -> session
    evpn: dict[str, dict] = field(default_factory=dict)
    vxlan: dict[str, bool] = field(default_factory=dict)   # remote VTEP -> up
    vrf_vteps: dict[str, bool] = field(default_factory=dict)  # VTEPs next hop of VRF routes
    mlag: dict = field(default_factory=dict)               # show mlag: state, peer, ports

    def view(self) -> dict:
        return {"family": self.family, "collected": self.collected, "errors": self.errors}


def parse_outputs(fam: str, outputs: dict[str, str], now: float) -> NodeState:
    state = NodeState(fam)
    parsers = EOS_PARSERS if fam == "eos" else IOS_PARSERS
    for topic, text in outputs.items():
        try:
            result = parsers[topic](text, now)
        except ValueError as exc:
            state.errors[topic] = str(exc)
            continue
        setattr(state, topic, result)
        state.collected.append(topic)
    return state


def collect_node(fam: str, topics: list[str], container: dict, runner, host_sudo: bool,
                 ssh: Callable[[str], tuple[int, str]], now: Optional[float] = None) -> NodeState:
    """Ask one running node about ``topics``. ``ssh(command)`` runs a command on
    the node over SSH (IOS kinds); cEOS goes through ``docker exec``."""
    now = now or time.time()
    outputs: dict[str, str] = {}
    errors: dict[str, str] = {}
    if fam == "eos":
        res = run_docker(runner, ["timeout", str(COLLECT_TIMEOUT), "docker", "exec",
                                  container["container"], "sh", "-c", eos_script(topics)], host_sudo)
        if res.exit_code != 0 and MARKER not in res.stdout:
            msg = (res.stderr or res.stdout).strip()[-200:] or f"exit code {res.exit_code}"
            return NodeState(fam, errors={t: msg for t in topics})
        outputs = {k: v for k, v in split_markers(res.stdout).items() if k in topics}
    else:
        topics = [t for t in topics if t in IOS_COMMANDS]
        for topic in topics:
            try:
                code, out = ssh(IOS_COMMANDS[topic])
            except Exception as exc:  # noqa: BLE001 - reported per node
                errors[topic] = str(exc) or type(exc).__name__
                if "auth" in str(exc).lower() or "timed out" in str(exc).lower():
                    break  # the other commands would fail the same way
                continue
            outputs[topic] = out
    state = parse_outputs(fam, outputs, now)
    state.errors.update(errors)
    for topic in topics:
        if topic not in state.collected and topic not in state.errors:
            state.errors[topic] = "no output"
    return state


def collect(view: dict, containers: dict[str, dict],
            node_io: Callable[[dict], tuple[object, bool, Callable[[str], tuple[int, str]]]],
            parallel: int = 8) -> dict[str, NodeState]:
    """Collect from every running node of the view that has protocols to ask about.

    ``containers`` maps node name to its inspect entry; ``node_io(container)``
    returns (host runner, host uses sudo, ssh function for that node).
    """
    now = time.time()
    jobs = {}
    states: dict[str, NodeState] = {}
    with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="routing-live") as pool:
        for node, c in containers.items():
            if c.get("state") != "running":
                continue
            fam = family(c.get("kind", ""))
            topics = wanted_topics(view, node)
            if not topics:
                continue
            if fam is None:
                states[node] = NodeState("", errors={t: f"kind '{c.get('kind')}' not supported"
                                                     for t in topics})
                continue

            def job(c=c, fam=fam, topics=topics):
                runner, sudo, ssh = node_io(c)
                return collect_node(fam, topics, c, runner, sudo, ssh, now)
            jobs[node] = pool.submit(job)
        for node, fut in jobs.items():
            try:
                states[node] = fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Protocol state of %s failed: %s", node, exc)
                states[node] = NodeState(family(containers[node].get("kind", "")) or "",
                                         errors={"": str(exc) or type(exc).__name__})
    return states


# --- Parsing: Arista EOS (JSON) ---------------------------------------------------

def _json(text: str):
    start = text.find("{")
    if start < 0:
        raise ValueError(text.strip().splitlines()[-1][:200] if text.strip() else "no output")
    try:
        return json.JSONDecoder().raw_decode(text[start:])[0]
    except json.JSONDecodeError as exc:
        raise ValueError(f"unreadable JSON: {exc}") from None


def _eos_bgp(text: str, now: float) -> dict[str, dict]:
    data = _json(text)
    peers: dict[str, dict] = {}
    for vrf_name, vrf in (data.get("vrfs") or {}).items():
        for ip, p in (vrf.get("peers") or {}).items():
            if ip in peers and vrf_name != "default":
                continue
            state = p.get("peerState", "")
            since = p.get("upDownTime")
            peers[ip] = {
                "ip": ip, "vrf": vrf_name, "asn": str(p.get("asn", "")), "state": state,
                "established": state == "Established",
                "uptime": max(0, int(now - since)) if isinstance(since, (int, float)) else None,
                "pfx_rcvd": p.get("prefixReceived"), "pfx_accepted": p.get("prefixAccepted"),
            }
    return peers


def _eos_ospf(text: str, now: float) -> list[dict]:
    data = _json(text)
    out = []

    def walk(obj, vrf):
        if isinstance(obj, dict):
            if "routerId" in obj and ("adjacencyState" in obj or "state" in obj):
                details = obj.get("details") or {}
                out.append({
                    "router_id": obj.get("routerId", ""), "vrf": vrf,
                    "address": obj.get("interfaceAddress", ""),
                    "iface": obj.get("interfaceName", ""),
                    "state": str(obj.get("adjacencyState") or obj.get("state") or "").lower(),
                    "area": str(details.get("areaId", "")),
                })
                return
            for v in obj.values():
                walk(v, vrf)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, vrf)

    for vrf_name, vrf in (data.get("vrfs") or {}).items():
        walk(vrf, vrf_name)
    return out


def _ipv4(value) -> bool:
    try:
        ipaddress.IPv4Address(str(value))
        return True
    except ValueError:
        return False


def _eos_vxlan(text: str, now: float) -> dict[str, bool]:
    """Remote VTEPs from ``show vxlan vtep``: whatever addresses it lists
    (the layout differs between EOS releases: a list, or keyed by address)."""
    data = _json(text)
    found: dict[str, bool] = {}

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if _ipv4(k):
                    found[k] = True
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
        elif isinstance(obj, str) and _ipv4(obj):
            found[obj] = True

    walk(data)
    return found


def _eos_vrf_vteps(text: str, now: float) -> dict[str, bool]:
    """VTEPs that VRF routes point at (``vtepAddr`` of their next hops) in
    ``show ip route vrf all``: the far ends of L3 VNI tunnels."""
    data = _json(text)
    found: dict[str, bool] = {}

    def walk(obj):
        if isinstance(obj, dict):
            addr = obj.get("vtepAddr")
            if isinstance(addr, str) and _ipv4(addr):
                found[addr] = True
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)
    return found


def _eos_mlag(text: str, now: float) -> dict:
    """``show mlag``: whether the pair formed (``state`` active, ``negStatus``
    connected), the peer-link, config sanity, and its ports' states
    (Active-full: both halves up; Active-partial: one half only)."""
    data = _json(text)
    ports = data.get("mlagPorts") or {}
    return {
        "state": str(data.get("state", "")),
        "negotiation": str(data.get("negStatus", "")),
        "peer_link": str(data.get("peerLinkStatus", "")),
        "local_interface": str(data.get("localIntfStatus", "")),
        "sanity": str(data.get("configSanity", "")),
        "domain": str(data.get("domainId", "")),
        "ports_full": int(ports.get("Active-full") or 0),
        "ports_partial": int(ports.get("Active-partial") or 0),
        "ports_down": int(ports.get("Inactive") or 0) + int(ports.get("Disabled") or 0),
    }


EOS_PARSERS = {"ospf": _eos_ospf, "bgp": _eos_bgp, "evpn": _eos_bgp, "vxlan": _eos_vxlan,
               "vrf_vteps": _eos_vrf_vteps, "mlag": _eos_mlag}


# --- Parsing: Cisco IOS (text) ------------------------------------------------------

_DURATION = re.compile(r"(\d+)([ywdhms])")
_UNIT = {"y": 31536000, "w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def ios_duration(text: str) -> Optional[int]:
    """``00:05:12``, ``1d02h``, ``2w3d`` -> seconds; ``never`` -> None."""
    text = text.strip().lower()
    if re.fullmatch(r"\d+:\d+:\d+", text):
        h, m, s = (int(x) for x in text.split(":"))
        return h * 3600 + m * 60 + s
    parts = _DURATION.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        return None
    return sum(int(n) * _UNIT[u] for n, u in parts)


def _ios_bgp(text: str, now: float) -> dict[str, dict]:
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.split()[:2] == ["Neighbor", "V"])
    except StopIteration:
        if "not active" in text.lower() or "% bgp" in text.lower():
            return {}
        raise ValueError(_last_line(text)) from None
    peers = {}
    for ln in lines[start + 1:]:
        tok = ln.split()
        if len(tok) < 10 or not _ipv4(tok[0]):
            continue
        rest = " ".join(tok[9:])
        established = rest.isdigit()
        peers[tok[0]] = {
            "ip": tok[0], "vrf": "default", "asn": tok[2],
            "state": "Established" if established else rest,
            "established": established,
            "uptime": ios_duration(tok[8]) if established else None,
            "pfx_rcvd": int(rest) if established else None, "pfx_accepted": None,
        }
    return peers


_IOS_OSPF_ROW = re.compile(
    r"^(\d+\.\d+\.\d+\.\d+)\s+(\d+)\s+(\S+(?:\s+-)?)\s+(\S+)\s+(\d+\.\d+\.\d+\.\d+)\s+(\S+)\s*$")


def _ios_ospf(text: str, now: float) -> list[dict]:
    if "Neighbor ID" not in text:
        if text.strip() and "%" in text:
            raise ValueError(_last_line(text))
        return []  # no neighbours: IOS prints nothing at all
    out = []
    for ln in text.splitlines():
        m = _IOS_OSPF_ROW.match(ln.strip())
        if m:
            state = m[3].split("/")[0].lower()
            out.append({"router_id": m[1], "vrf": "default", "address": m[5],
                        "iface": m[6], "state": state, "area": ""})
    return out


def _ios_nve(text: str, now: float) -> dict[str, bool]:
    peers: dict[str, bool] = {}
    for ln in text.splitlines():
        tok = ln.split()
        ips = [t for t in tok if _ipv4(t)]
        if not ips or not tok[0].lower().startswith("nve"):
            continue
        up = any(t.upper() == "UP" for t in tok)
        peers[ips[0]] = peers.get(ips[0], False) or up
    return peers


def _last_line(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1][:200] if lines else "no output"


IOS_PARSERS = {"ospf": _ios_ospf, "bgp": _ios_bgp, "evpn": _ios_bgp, "vxlan": _ios_nve}


# --- Overlay ------------------------------------------------------------------------

def _side(states: dict[str, NodeState], node: str, topic: str, running: dict[str, bool]):
    """(NodeState or None, why unknown)."""
    if node.startswith("ext:"):
        return None, "outside the lab"
    if not running.get(node):
        return None, "not running"
    st = states.get(node)
    if st is None:
        return None, "not collected"
    if topic not in st.collected:
        return None, st.errors.get(topic) or st.errors.get("") or "not collected"
    return st, ""


def _bgp_side(states, running, node: str, peer_ip: str, topic: str) -> dict:
    st, why = _side(states, node, topic, running)
    if st is None:
        return {"state": "unknown", "detail": why}
    p = getattr(st, topic).get(peer_ip)
    if p is None:
        return {"state": "missing", "detail": f"no {peer_ip} neighbor in the running config"}
    return {"state": "up" if p["established"] else "down", "detail": p["state"],
            "uptime": p["uptime"], "pfx_rcvd": p["pfx_rcvd"], "pfx_accepted": p["pfx_accepted"],
            "asn": p["asn"]}


def _combine(sides: list[dict]) -> tuple[str, str]:
    """One state for a session or adjacency from what its ends report."""
    known = [s for s in sides if s["state"] != "unknown"]
    if not known:
        return "unknown", sides[0]["detail"] if sides else ""
    if all(s["state"] == "up" for s in known):
        return "up", ""
    bad = next(s for s in known if s["state"] != "up")
    return "down", bad["detail"]


def overlay(view: dict, states: dict[str, NodeState], running: dict[str, bool],
            now: Optional[float] = None) -> dict:
    """Live state per intended adjacency / session / tunnel, plus what runs
    but was not intended."""
    now = now or time.time()
    out: dict = {"updated": now, "nodes": {}, "ospf": {}, "bgp": {}, "vxlan": {}, "mlag": {},
                 "extra": {"ospf": [], "bgp": [], "evpn": []}, "summary": {}}
    for node, st in states.items():
        out["nodes"][node] = st.view()
    addresses = view.get("addresses") or {}
    owner = {ip: a["node"] for ip, a in addresses.items()}

    # BGP (and the EVPN address family of the same sessions)
    matched: set[tuple[str, str, str]] = set()
    for s in (view.get("bgp") or {}).get("sessions", []):
        families = s["families"] or ["ipv4"]
        per_family = {}
        sides_all = {"a": [], "b": []}
        for fam in families:
            topic = "evpn" if fam == "evpn" else "bgp"
            a = _bgp_side(states, running, s["a"]["node"], s["b"]["ip"], topic)
            b = (_bgp_side(states, running, s["b"]["node"], s["a"]["ip"], topic)
                 if not s["external"] else {"state": "unknown", "detail": "outside the lab"})
            matched.add((s["a"]["node"], topic, s["b"]["ip"]))
            matched.add((s["b"]["node"], topic, s["a"]["ip"]))
            state, detail = _combine([a, b])
            per_family[fam] = {"state": state, "detail": detail, "a": a, "b": b}
            sides_all["a"].append(a)
            sides_all["b"].append(b)
        states_ = [f["state"] for f in per_family.values()]
        if "down" in states_:
            state = "down"
            detail = next(f"{fam}: {f['detail']}" for fam, f in per_family.items() if f["state"] == "down")
        elif states_ and all(x == "up" for x in states_):
            state, detail = "up", ""
        elif "up" in states_:
            state, detail = "partial", ", ".join(f"{fam} {f['state']}" for fam, f in per_family.items())
        else:
            state, detail = "unknown", next(iter(per_family.values()))["detail"]
        entry = {"state": state, "detail": detail, "families": per_family}
        # One-sided in the startup configs, yet the other node runs the neighbor
        if s["configured"] != "both" and not s["external"] and any(
                f["b"]["state"] in ("up", "down") for f in per_family.values()):
            entry["drift"] = (f"{s['b']['node']} has a neighbor {s['a']['ip']} in its running "
                              f"config that its startup config does not have")
        out["bgp"][s["id"]] = entry

    for node, st in states.items():
        for topic in ("bgp", "evpn"):
            for ip, p in getattr(st, topic).items():
                if (node, topic, ip) in matched:
                    continue
                out["extra"]["bgp" if topic == "bgp" else "evpn"].append({
                    "node": node, "ip": ip, "peer": owner.get(ip, ""), "asn": p["asn"],
                    "state": "up" if p["established"] else "down", "detail": p["state"],
                    "uptime": p["uptime"], "pfx_rcvd": p["pfx_rcvd"],
                })

    # OSPF
    ospf = view.get("ospf") or {}
    rid = {n: v["router_id"] for n, v in (ospf.get("nodes") or {}).items()}
    seen_nbrs: set[tuple[str, int]] = set()

    def ospf_side(node, peer, peer_ip, peer_iface_hint):
        st, why = _side(states, node, "ospf", running)
        if st is None:
            return {"state": "unknown", "detail": why}
        for i, nb in enumerate(st.ospf):
            if nb["address"] == peer_ip or (nb["router_id"] == rid.get(peer) and peer_iface_hint
                                            and _same_iface(nb["iface"], peer_iface_hint)):
                seen_nbrs.add((node, i))
                up = nb["state"] in UP_OSPF
                return {"state": "up" if up else "down", "detail": nb["state"].upper(),
                        "router_id": nb["router_id"]}
        return {"state": "down", "detail": "no neighbor"}

    for adj in ospf.get("adjacencies", []):
        a = ospf_side(adj["a"]["node"], adj["b"]["node"], adj["b"]["ip"], adj["a"]["iface"])
        b = ospf_side(adj["b"]["node"], adj["a"]["node"], adj["a"]["ip"], adj["b"]["iface"])
        state, detail = _combine([a, b])
        out["ospf"][adj["id"]] = {"state": state, "detail": detail, "a": a, "b": b}
    for node, st in states.items():
        for i, nb in enumerate(st.ospf):
            if (node, i) in seen_nbrs:
                continue
            peer = owner.get(nb["address"]) or next((n for n, r in rid.items() if r == nb["router_id"]), "")
            out["extra"]["ospf"].append({
                "node": node, "peer": peer, "router_id": nb["router_id"], "ip": nb["address"],
                "iface": nb["iface"], "state": "up" if nb["state"] in UP_OSPF else "down",
                "detail": nb["state"].upper(),
            })

    # VXLAN tunnels: each VTEP knows the other as a remote VTEP
    for t in ((view.get("evpn") or {}).get("tunnels") or []):
        sides = []
        for me, other in ((t["a"], t["b"]), (t["b"], t["a"])):
            st, why = _side(states, me["node"], "vxlan", running)
            if st is None:
                sides.append({"state": "unknown", "detail": why})
            elif other["ip"] in st.vxlan or other["ip"] in st.vrf_vteps:
                up = st.vxlan.get(other["ip"], st.vrf_vteps.get(other["ip"]))
                sides.append({"state": "up" if up else "down",
                              "detail": "" if up else f"{other['ip']} down"})
            else:
                sides.append({"state": "down", "detail": f"{me['node']} has not learned {other['ip']}"})
        state, detail = _combine(sides)
        out["vxlan"][t["id"]] = {"state": state, "detail": detail, "a": sides[0], "b": sides[1]}

    # MLAG pairs: each half active and connected, peer-link up, ports dual-homed
    for p in ((view.get("mlag") or {}).get("pairs") or []):
        sides = [_mlag_side(states, running, n) for n in p["nodes"]]
        state, detail = _combine(sides)
        if state == "up":
            partial = max(s.get("ports_partial", 0) for s in sides)
            if partial:
                state = "partial"
                detail = f"{partial} dual-homed port{'s' if partial > 1 else ''} up on one side only"
        out["mlag"][p["id"]] = {"state": state, "detail": detail, "a": sides[0], "b": sides[1]}

    for key in ("ospf", "bgp", "vxlan", "mlag"):
        counts: dict[str, int] = {}
        for v in out[key].values():
            counts[v["state"]] = counts.get(v["state"], 0) + 1
        out["summary"][key] = counts
    return out


def _mlag_side(states, running, node: str) -> dict:
    st, why = _side(states, node, "mlag", running)
    if st is None:
        return {"state": "unknown", "detail": why}
    m = st.mlag
    bad = []
    if m.get("state") != "active":
        bad.append(f"state {m.get('state') or 'unknown'}")
    if m.get("negotiation") and m["negotiation"] != "connected":
        bad.append(f"peer {m['negotiation']}")
    if m.get("peer_link") and m["peer_link"] != "up":
        bad.append(f"peer-link {m['peer_link']}")
    if m.get("sanity") and m["sanity"] != "consistent":
        bad.append(f"config {m['sanity']}")
    return {"state": "down" if bad else "up", "detail": ", ".join(bad),
            "ports_full": m.get("ports_full", 0), "ports_partial": m.get("ports_partial", 0)}


def _same_iface(a: str, b: str) -> bool:
    return canonical(a) == canonical(b)


def collect_lab(cluster, topo, view: dict, timeout: float = 15) -> dict:
    """Live overlay for the CLI: inspect the lab's hosts, then ask its nodes."""
    from ..cluster import create_runner
    from ..deployer import hosts_for_lab
    from ..execute import ssh_exec
    from ..nodes import InspectError, inspect_all, parse_inspect

    runners = {}
    containers: dict[str, dict] = {}
    errors: dict[str, str] = {}
    try:
        for host in hosts_for_lab(cluster, topo):
            try:
                runners[host.name] = create_runner(host)
                data = inspect_all(runners[host.name])
            except InspectError as exc:
                errors[host.name] = str(exc)
                continue
            except Exception as exc:  # noqa: BLE001
                errors[host.name] = f"unreachable: {exc}"
                continue
            for c in parse_inspect(data, host.name):
                if c["lab"] == topo.name:
                    containers[c["node"]] = c
        sudo = {h.name: h.sudo for h in cluster.hosts}

        def node_io(c):
            runner = runners[c["host"]]
            return runner, sudo.get(c["host"], False), lambda cmd: ssh_exec(
                runner, c["kind"], c["ipv4"], cmd, timeout=timeout)

        states = collect(view, containers, node_io)
    finally:
        for runner in runners.values():
            runner.close()
    result = overlay(view, states, {n: c["state"] == "running" for n, c in containers.items()})
    result["errors"] = errors
    return result
