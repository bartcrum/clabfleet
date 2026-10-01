"""Is a lab node ready to use, beyond its container running?

Network OS containers start long before their CLI or SSH answers (cEOS
takes a minute, VM-based kinds several). A node counts as ready when:

- its container has a Docker health check (vrnetlab images do) that
  reports healthy, or else
- for kinds with a CLI reachable via ``docker exec`` (cEOS, SR Linux,
  cRPD): the CLI answers ``show version``
- for kinds reached over SSH (Cisco IOL, other VM-based kinds): the
  management address answers with an SSH banner
- for everything else: the container is running
"""

import logging
import threading
import time
from typing import Callable, Optional

from .nodes import KIND_CLI_EXEC, SSH_CLI_KINDS, run_docker

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 15  # seconds per probe command on the host

SSH_PORT = 22
# argv: address port. Prints the first 4 bytes the SSH port sends ("SSH-" when up).
_SSH_BANNER = 'exec 3<>"/dev/tcp/$0/$1" && head -c 4 <&3'


def check_ready(
    runner, container: dict, host_sudo: bool = False, timeout: int = PROBE_TIMEOUT
) -> tuple[bool, str]:
    """(ready, detail) for one container, probing from its host."""
    if container.get("state") != "running":
        return False, container.get("state") or "not running"
    status = (container.get("status") or "").lower()
    if "(healthy)" in status:
        return True, "healthy"
    if "health: starting" in status:
        return False, "health check starting"
    if "(unhealthy)" in status:
        return False, "health check failing"

    kind = container.get("kind", "")
    if kind in KIND_CLI_EXEC:
        argv = ["timeout", str(timeout), "docker", "exec", container["container"],
                *KIND_CLI_EXEC[kind], "show version"]
        res = run_docker(runner, argv, host_sudo)
        return (True, "CLI answers") if res.exit_code == 0 else (False, "CLI not up yet")
    if kind in SSH_CLI_KINDS:
        ipv4 = container.get("ipv4")
        if not ipv4:
            return False, "no management address yet"
        res = runner.run(["timeout", str(timeout), "bash", "-c", _SSH_BANNER, ipv4,
                          str(SSH_PORT)],
                         check=False, sudo=False)
        if res.stdout.startswith("SSH-"):
            return True, "SSH answers"
        if res.exit_code == 127:
            return False, "cannot probe SSH (no bash/timeout on the host)"
        return False, "SSH not up yet"
    return True, "running"


def wait_until_ready(
    poll: Callable[[list[str]], dict[str, tuple[bool, str]]],
    expected: list[str],
    timeout: float,
    interval: float = 5.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Poll pending nodes until all are ready or ``timeout`` seconds pass.

    ``poll(pending)`` returns {node: (ready, detail)} for the nodes it
    could check. A node stays ready once it has been seen ready.

    Returns {"ready": bool, "seconds": float, "pending": {node: detail}}.
    """
    start = clock()
    ready: set[str] = set()
    details: dict[str, str] = {n: "not created yet" for n in expected}
    last_progress = None
    while True:
        pending = [n for n in expected if n not in ready]
        if pending:
            for node, (ok, detail) in poll(pending).items():
                details[node] = detail
                if ok:
                    ready.add(node)
            pending = [n for n in expected if n not in ready]
        elapsed = clock() - start
        if not pending:
            logger.info("All %d nodes ready after %ds", len(expected), elapsed)
            return {"ready": True, "seconds": round(elapsed, 1), "pending": {}}
        progress = (f"{len(ready)}/{len(expected)} nodes ready, waiting for "
                    + ", ".join(f"{n} ({details[n]})" for n in pending[:6])
                    + (f" and {len(pending) - 6} more" if len(pending) > 6 else ""))
        if progress != last_progress:
            logger.info(progress)
            last_progress = progress
        if elapsed >= timeout:
            logger.error("Timed out after %ds: %s", elapsed, progress)
            return {"ready": False, "seconds": round(elapsed, 1),
                    "pending": {n: details[n] for n in pending}}
        sleep(min(interval, max(0.0, timeout - elapsed)))


class ReadinessCache:
    """Readiness results per container, shared by the GUI's request threads.

    A ready container stays ready. A not-ready one is due for another probe
    ``recheck`` seconds after its last one; until then (and while the probe
    runs) its last result is served. ``claim`` makes sure only one probe
    per container is in flight.
    """

    def __init__(self, recheck: float = 10.0, clock: Callable[[], float] = time.monotonic):
        self.recheck = recheck
        self.clock = clock
        self._seen: dict[tuple, tuple[bool, str, float]] = {}
        self._in_flight: set[tuple] = set()
        self._lock = threading.Lock()

    def get(self, key: tuple) -> tuple[Optional[tuple[bool, str]], bool]:
        """(last (ready, detail) or None, whether a new probe is due)."""
        with self._lock:
            hit = self._seen.get(key)
            if hit is None:
                return None, True
            ok, detail, when = hit
            return (ok, detail), not ok and self.clock() - when >= self.recheck

    def claim(self, key: tuple) -> bool:
        """True if the caller should probe now (no probe in flight)."""
        with self._lock:
            if key in self._in_flight:
                return False
            self._in_flight.add(key)
            return True

    def store(self, key: tuple, ok: bool, detail: str) -> None:
        with self._lock:
            self._seen[key] = (ok, detail, self.clock())
            self._in_flight.discard(key)

    def release(self, key: tuple) -> None:
        with self._lock:
            self._in_flight.discard(key)

    def prune(self, live: set[tuple]) -> None:
        with self._lock:
            for key in [k for k in self._seen if k not in live]:
                del self._seen[key]
