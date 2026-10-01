"""Check that cluster hosts can reach each other for VXLAN links.

For every (source, destination) host pair:

- ping: ``ping`` from the source to the destination's VXLAN address
- udp: a short UDP listener on the destination's VXLAN port receives
  tagged datagrams sent from the source. This is what catches a firewall
  (e.g. firewalld on RHEL) dropping the VXLAN port while ping still works.

The probes use ``ping`` and ``python3`` on the hosts. When either is
missing, or the port is already held by a running VXLAN link, that probe
is reported as not tested (None) rather than failed.
"""

import logging
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .cluster import HostInfo
from .runner import Runner

logger = logging.getLogger(__name__)

LISTEN_SECONDS = 4.0
READY_TIMEOUT = 15.0

# argv: port seconds. Prints READY once bound (or BUSY), then GOT <token> per datagram.
_LISTEN = r"""
import socket, sys, time
port, secs = int(sys.argv[1]), float(sys.argv[2])
try:
    s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    s.bind(("::", port))
except OSError:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("0.0.0.0", port))
    except OSError as exc:
        print("BUSY", exc, flush=True)
        sys.exit(0)
print("READY", flush=True)
end = time.time() + secs
while True:
    left = end - time.time()
    if left <= 0:
        break
    s.settimeout(left)
    try:
        data, _ = s.recvfrom(256)
    except socket.timeout:
        break
    print("GOT", data.decode(errors="replace"), flush=True)
"""

# argv: address port token
_SEND = r"""
import socket, sys, time
addr, port, token = sys.argv[1], int(sys.argv[2]), sys.argv[3]
family, kind, proto, _, target = socket.getaddrinfo(addr, port, type=socket.SOCK_DGRAM)[0]
s = socket.socket(family, kind, proto)
for _ in range(15):
    s.sendto(token.encode(), target)
    time.sleep(0.1)
"""


def vxlan_pairs(cross_links: list[dict]) -> list[tuple[str, str]]:
    """Both directions of every host pair that shares a cross-host link."""
    pairs: list[tuple[str, str]] = []
    for link in cross_links:
        a, b = link["hosts"]
        for pair in ((a, b), (b, a)):
            if pair not in pairs:
                pairs.append(pair)
    return pairs


def all_pairs(hosts: list[HostInfo]) -> list[tuple[str, str]]:
    return [(a.name, b.name) for a in hosts for b in hosts if a.name != b.name]


def check_links(
    runners: dict[str, Runner],
    hosts: list[HostInfo],
    pairs: list[tuple[str, str]],
    port: int,
    listen_seconds: Optional[float] = None,
) -> list[dict]:
    """Probe each (src, dst) pair. Returns one dict per pair:

    {"from", "to", "address", "ping": bool|None, "udp": bool|None, "note"}
    """
    by_name = {h.name: h for h in hosts}
    results = {
        (src, dst): {"from": src, "to": dst, "address": by_name[dst].vtep,
                     "ping": None, "udp": None, "note": ""}
        for src, dst in pairs
    }
    for (src, dst), r in results.items():
        if not r["address"]:
            r["note"] = f"{dst} has no VXLAN address (set vtep_ip)"
    testable = [p for p, r in results.items() if r["address"]]
    if not testable:
        return list(results.values())

    with ThreadPoolExecutor(max_workers=min(16, len(testable))) as pool:
        for pair, ok in zip(testable, pool.map(
                lambda p: _ping(runners[p[0]], results[p]["address"]), testable)):
            results[pair]["ping"] = ok

    _udp_probe(runners, testable, results, port, listen_seconds or LISTEN_SECONDS)
    return list(results.values())


def _ping(runner: Runner, address: str) -> Optional[bool]:
    try:
        res = runner.run(["ping", "-c", "2", "-W", "2", address], check=False, sudo=False)
    except Exception as exc:
        logger.warning("ping via %s failed to run: %s", runner.name, exc)
        return None
    if res.exit_code == 127:
        return None  # no ping on the host
    return res.exit_code == 0


def _udp_probe(runners, pairs, results, port, listen_seconds) -> None:
    dsts = sorted({dst for _, dst in pairs})
    ready = {d: threading.Event() for d in dsts}
    output: dict[str, list[str]] = {d: [] for d in dsts}

    def listen(dst: str) -> None:
        def on_line(line: str) -> None:
            output[dst].append(line)
            if line.startswith(("READY", "BUSY")):
                ready[dst].set()
        try:
            runners[dst].run(["python3", "-c", _LISTEN, str(port), str(listen_seconds)],
                             check=False, sudo=False, on_output=on_line)
        except Exception as exc:
            output[dst].append(f"ERROR {exc}")
        finally:
            ready[dst].set()

    listeners = [threading.Thread(target=listen, args=(d,), daemon=True) for d in dsts]
    for t in listeners:
        t.start()
    for d in dsts:
        ready[d].wait(READY_TIMEOUT)

    def state(dst: str) -> str:
        lines = output[dst]
        if any(line.startswith("READY") for line in lines):
            return "ready"
        if any(line.startswith("BUSY") for line in lines):
            return "busy"
        return "error"

    tokens = {pair: f"clabfleet-{secrets.token_hex(6)}" for pair in pairs}
    to_send = [p for p in pairs if state(p[1]) == "ready"]

    def send(pair) -> None:
        src, dst = pair
        try:
            runners[src].run(["python3", "-c", _SEND, results[pair]["address"], str(port),
                              tokens[pair]], check=False, sudo=False)
        except Exception as exc:
            results[pair]["note"] = f"could not send from {src}: {exc}"

    if to_send:
        with ThreadPoolExecutor(max_workers=min(16, len(to_send))) as pool:
            list(pool.map(send, to_send))
    for t in listeners:
        t.join(listen_seconds + READY_TIMEOUT)

    for pair in pairs:
        src, dst = pair
        st = state(dst)
        r = results[pair]
        if st == "busy":
            r["note"] = f"UDP {port} on {dst} is already in use (a VXLAN link is up); not tested"
        elif st == "error":
            r["note"] = (f"UDP not tested: could not run python3 on {dst} "
                         f"({' '.join(output[dst])[-200:].strip() or 'no output'})")
        elif not r["note"]:
            got = any(line == f"GOT {tokens[pair]}" for line in output[dst])
            r["udp"] = got
            if not got:
                r["note"] = (f"UDP {port} from {src} to {dst} ({r['address']}) gets no "
                             f"answer: check firewalls on {dst} and in between")


def failures(results: list[dict]) -> list[dict]:
    """Pairs where a probe ran and failed."""
    return [r for r in results if r["ping"] is False or r["udp"] is False]


def describe(r: dict) -> str:
    def word(v):
        return "ok" if v else ("FAILED" if v is False else "not tested")
    line = (f"{r['from']} -> {r['to']} ({r['address'] or 'no address'}): "
            f"ping {word(r['ping'])}, udp {word(r['udp'])}")
    return f"{line}. {r['note']}" if r["note"] else line
