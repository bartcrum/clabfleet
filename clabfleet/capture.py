"""Packet capture on lab node interfaces (``clabfleet capture``, GUI captures).

``tcpdump`` runs in the node's network namespace on the node's host, through
the host's runner (locally or over SSH):

- ``node``: the node's own tcpdump via ``docker exec`` (cEOS has one)
- ``helper``: a throwaway container sharing the node's network namespace
  (``docker run --net container:<node>``) from an image that has tcpdump,
  for nodes without one (alpine, most linux kinds)

``auto`` uses the node's tcpdump when it has one. Output is either pcap
bytes (``tcpdump -U -w -``) or a live text decode (``tcpdump -l -nn``).

Stopping the local ``docker`` client does not stop the process inside the
container, so every capture is stopped explicitly: the node's tcpdump is
killed by PID with ``docker exec <node> kill``, and the helper container is
removed with ``docker rm -f``. With a duration, the node's tcpdump also runs
under ``timeout`` when the node has it, as a backstop should clabfleet itself
die before it can stop the capture.
"""

import logging
import os
import re
import shlex
import subprocess
import threading
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from .cluster import ClusterConfig, HostInfo, create_runner
from .deployer import hosts_for_lab
from .livestate import linux_iface_names
from .nodes import DOCKER_DENIED, InspectError, inspect_all, parse_inspect
from .runner import Runner, SSHRunner
from .topology import Topology

logger = logging.getLogger(__name__)

FORMATS = ("text", "pcap")
METHODS = ("auto", "node", "helper")
# Any image with tcpdump and a default entrypoint works; override with
# --helper-image or CLAB_CAPTURE_IMAGE
DEFAULT_HELPER_IMAGE = "nicolaka/netshoot:latest"
MGMT_INTERFACE = "eth0"
MAX_FILTER_LEN = 1024
# Linux interface names: at most 15 characters, and never an option
IFACE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,14}")

# GUI captures always stop on their own: a duration is required, and both
# it and the packet count are capped
GUI_DEFAULT_DURATION = 60
GUI_MAX_DURATION = 600
GUI_MAX_LIVE_DURATION = 1800
GUI_MAX_COUNT = 100_000
GUI_MAX_BYTES = 200 * 1024 * 1024  # a pcap download stops here

PID_MARKER = "clabfleet-capture-pid"
# Prints its PID (which tcpdump keeps through exec) so the capture can be
# killed later, then runs tcpdump, under `timeout` when a backstop is set
NODE_WRAPPER = (
    f'echo "{PID_MARKER} $$" >&2; d=$1; shift; '
    'if [ "$d" != 0 ] && command -v timeout >/dev/null 2>&1; '
    'then exec timeout -s INT "$d" "$@"; fi; exec "$@"'
)
# Stops the PID from NODE_WRAPPER if it is still our tcpdump (or timeout)
NODE_STOP = (
    'p=$1; c=$(cat /proc/$p/comm 2>/dev/null) || exit 0; '
    'case $c in tcpdump|timeout) ;; *) exit 0 ;; esac; '
    'kill -INT "$p" 2>/dev/null; '
    'for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$p" 2>/dev/null || exit 0; sleep 0.2; done; '
    'kill -KILL "$p" 2>/dev/null; exit 0'
)
DETECT_TCPDUMP = ["sh", "-c", "command -v tcpdump"]
BACKSTOP_GRACE = 10  # seconds past the duration before `timeout` steps in

OnMessage = Callable[[str], None]


class CaptureError(Exception):
    """A capture could not be set up."""


@dataclass
class CaptureSpec:
    node: str
    interface: str
    format: str = "text"            # text | pcap
    bpf_filter: str = ""
    count: Optional[int] = None     # stop after this many packets
    duration: Optional[float] = None  # stop after this many seconds
    snaplen: Optional[int] = None   # bytes per packet (tcpdump default: 262144)

    def __post_init__(self):
        if self.format not in FORMATS:
            raise ValueError(f"Unknown capture format '{self.format}'")
        if not IFACE_RE.fullmatch(self.interface or ""):
            raise ValueError(f"Invalid interface name '{self.interface}'")
        self.bpf_filter = clean_filter(self.bpf_filter)
        for name in ("count", "snaplen"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or value <= 0):
                raise ValueError(f"{name} must be a positive whole number")
        if self.duration is not None and not self.duration > 0:
            raise ValueError("duration must be a positive number of seconds")


def parse_target(target: str) -> tuple[str, str]:
    """``node:iface`` → (node, iface)."""
    node, sep, iface = target.partition(":")
    if not sep or not node or not iface:
        raise ValueError(f"Capture target '{target}' is not 'node:interface'")
    return node, iface


def clean_filter(text: str) -> str:
    """A BPF filter that is safe to pass to tcpdump as one argument.

    It never goes through a shell, but it must not look like a tcpdump
    option, and control characters have no place in it.
    """
    text = " ".join(str(text or "").split())
    if len(text) > MAX_FILTER_LEN:
        raise ValueError(f"Capture filter is longer than {MAX_FILTER_LEN} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValueError("Capture filter contains control characters")
    if text.startswith("-"):
        raise ValueError("Capture filter must not start with '-'")
    return text


def node_interfaces(topo: Topology, node: str) -> list[str]:
    """Interfaces of a node that captures may use: its links, then eth0."""
    if node not in topo.nodes:
        raise ValueError(f"Lab '{topo.name}' has no node '{node}'")
    names = [ep.interface for link in topo.links for ep in link.endpoints
             if ep.node == node and ep.interface]
    return list(dict.fromkeys(names + [MGMT_INTERFACE]))


def check_interface(topo: Topology, node: str, iface: str) -> str:
    """The name tcpdump needs inside the container for a node interface.

    ``iface`` may be the topology's name (e.g. cEOS ``Ethernet1``) or the
    Linux name (``eth1``).
    """
    known = node_interfaces(topo, node)
    kind = topo.effective_node(node)["kind"]
    for name in known:
        linux = linux_iface_names(kind, name)[0][0]
        if iface in (name, linux):
            return linux
    raise ValueError(f"Node '{node}' has no interface '{iface}' in the topology "
                     f"(known: {', '.join(known)})")


def tcpdump_args(spec: CaptureSpec) -> list[str]:
    """tcpdump argv for a capture (run inside the node's network namespace)."""
    args = ["tcpdump", "-i", spec.interface, "--immediate-mode"]
    if spec.format == "pcap":
        args += ["-U", "-w", "-"]   # flush every packet to stdout
    else:
        args += ["-l", "-nn"]       # line-buffered, no name lookups
    if spec.count:
        args += ["-c", str(spec.count)]
    if spec.snaplen:
        args += ["-s", str(spec.snaplen)]
    if spec.bpf_filter:
        args += ["--", spec.bpf_filter]
    return args


def capture_command(spec: CaptureSpec, container: str, method: str,
                    helper_image: str = DEFAULT_HELPER_IMAGE, name: str = "") -> list[str]:
    """argv (on the node's host) that streams the capture to stdout."""
    tcpdump = tcpdump_args(spec)
    if method == "node":
        backstop = int(spec.duration + BACKSTOP_GRACE) if spec.duration else 0
        return ["docker", "exec", container, "sh", "-c", NODE_WRAPPER, "clabfleet-capture",
                str(backstop), *tcpdump]
    if method == "helper":
        if not name:
            raise ValueError("A helper container needs a name")
        return ["docker", "run", "--rm", "--name", name, "--label", "clabfleet.capture=1",
                "--network", f"container:{container}",
                "--cap-add", "NET_RAW", "--cap-add", "NET_ADMIN",
                helper_image, *tcpdump]
    raise ValueError(f"Unknown capture method '{method}'")


def stop_command(method: str, container: str, name: str = "",
                 pid: Optional[int] = None) -> Optional[list[str]]:
    """argv that stops a running capture (None if there is nothing to stop)."""
    if method == "node":
        if pid is None:
            return None
        return ["docker", "exec", container, "sh", "-c", NODE_STOP, "clabfleet-stop", str(pid)]
    if method == "helper":
        return ["docker", "rm", "-f", name]
    raise ValueError(f"Unknown capture method '{method}'")


def sudo_prefix(runner: Runner) -> list[str]:
    # Remote sudo must be NOPASSWD, as for every other command
    if isinstance(runner, SSHRunner) or not runner.interactive_sudo:
        return ["sudo", "-n"]
    return ["sudo"]


def find_container(cluster: ClusterConfig, topo: Topology, node: str) -> tuple[HostInfo, dict]:
    """The host and running container of a lab node."""
    errors = []
    for host in hosts_for_lab(cluster, topo):
        try:
            with create_runner(host) as runner:
                data = inspect_all(runner)
        except InspectError as exc:
            errors.append(f"{host.name}: {exc}")
            continue
        except Exception as exc:
            errors.append(f"{host.name}: unreachable: {exc}")
            continue
        for c in parse_inspect(data, host.name):
            if c["lab"] == topo.name and c["node"] == node:
                if c["state"] != "running":
                    raise CaptureError(f"Node '{node}' is not running ({c['state']})")
                return host, c
    detail = f" ({'; '.join(errors)})" if errors else ""
    raise CaptureError(f"Node '{node}' of lab '{topo.name}' is not running{detail}")


# ----------------------------------------------------------------------
# Streaming processes
# ----------------------------------------------------------------------

class LocalProcess:
    """A command on this machine with binary stdout and line-wise stderr."""

    def __init__(self, argv: list[str]):
        logger.debug("local$ %s", shlex.join(argv))
        self._proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, start_new_session=True)

    def read(self, size: int) -> bytes:
        return self._proc.stdout.read1(size)

    def stderr_lines(self):
        for line in self._proc.stderr:
            yield line.decode(errors="replace").rstrip("\n")

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        try:
            return self._proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            if self.wait(3) is None:
                self._proc.kill()
                self._proc.wait()
        for pipe in (self._proc.stdout, self._proc.stderr):
            try:
                pipe.close()
            except Exception:
                pass


class SSHProcess:
    """A command on a remote host in its own SSH channel."""

    def __init__(self, ssh_client, argv: list[str]):
        cmd = shlex.join(argv)
        logger.debug("remote$ %s", cmd)
        self._chan = ssh_client.get_transport().open_session()
        self._chan.exec_command(cmd)

    def read(self, size: int) -> bytes:
        return self._chan.recv(size)

    def stderr_lines(self):
        buf = b""
        while True:
            data = self._chan.recv_stderr(65536)
            if not data:
                break
            buf += data
            *lines, buf = buf.split(b"\n")
            for line in lines:
                yield line.decode(errors="replace")
        if buf:
            yield buf.decode(errors="replace")

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        if timeout is not None and not self._chan.status_event.wait(timeout):
            return None
        return self._chan.recv_exit_status()

    def close(self) -> None:
        try:
            self._chan.close()
        except Exception:
            pass


def spawn(runner: Runner, argv: list[str], sudo: bool):
    """Start a streaming command on the runner's host."""
    argv = (sudo_prefix(runner) if sudo else []) + list(argv)
    if isinstance(runner, SSHRunner):
        return SSHProcess(runner.client(), argv)
    return LocalProcess(argv)


# ----------------------------------------------------------------------
# A running capture
# ----------------------------------------------------------------------

class Capture:
    """One tcpdump on a node interface, streamed from the node's host.

    ``start()`` picks the method and launches it, ``read()`` returns output
    (b"" at the end), ``stop()`` kills tcpdump inside the container and is
    safe to call more than once and from any thread. The duration, if any,
    is enforced with a timer that calls ``stop()``.
    """

    def __init__(
        self,
        runner: Runner,
        container: str,
        spec: CaptureSpec,
        host_sudo: bool = False,
        method: str = "auto",
        helper_image: Optional[str] = None,
        on_message: Optional[OnMessage] = None,
        spawner=spawn,
    ):
        if method not in METHODS:
            raise ValueError(f"Unknown capture method '{method}' (choose from {', '.join(METHODS)})")
        self.runner = runner
        self.container = container
        self.spec = spec
        self.host_sudo = host_sudo
        self.requested_method = method
        self.method = ""
        self.helper_image = (helper_image or os.environ.get("CLAB_CAPTURE_IMAGE")
                             or DEFAULT_HELPER_IMAGE)
        self.name = f"clabfleet-capture-{uuid.uuid4().hex[:10]}"
        self.on_message = on_message or (lambda line: None)
        self.exit_code: Optional[int] = None
        self.stopped = False   # stopped by us (duration, Ctrl+C, disconnect)
        self.stop_reason = ""
        self._spawner = spawner
        self._sudo = False
        self._proc = None
        self._pid: Optional[int] = None
        self._pid_seen = threading.Event()
        self._stderr_done = threading.Event()
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._cleaned = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False

    # --- start ---

    def start(self) -> "Capture":
        self._sudo = self._docker_needs_sudo()
        self.method = self._resolve_method()
        if self.method == "helper":
            self._ensure_helper_image()
        argv = capture_command(self.spec, self.container, self.method,
                               self.helper_image, self.name)
        with self._lock:
            if self.stopped:  # stopped while starting
                raise CaptureError("Capture was stopped before it started")
            self._proc = self._spawner(self.runner, argv, self._sudo)
        threading.Thread(target=self._read_stderr, daemon=True,
                         name="capture-stderr").start()
        if self.spec.duration:
            self._timer = threading.Timer(self.spec.duration, self.stop, kwargs={"reason": "duration"})
            self._timer.daemon = True
            self._timer.start()
        return self

    def describe(self) -> str:
        how = (f"helper container {self.helper_image}" if self.method == "helper"
               else "the node's tcpdump")
        return f"{self.spec.node}:{self.spec.interface} ({self.container}, {how})"

    def _docker(self, argv: list[str], check: bool = False):
        return self.runner.run(argv, check=check, sudo=self._sudo)

    def _docker_needs_sudo(self) -> bool:
        res = self.runner.run(["docker", "version", "--format", "{{.Server.Version}}"],
                              check=False, sudo=False)
        if res.exit_code == 0:
            return False
        if self.host_sudo and DOCKER_DENIED in (res.stderr + res.stdout).lower():
            return True
        raise CaptureError(f"Docker is not usable on {self.runner.name}: "
                           f"{(res.stderr or res.stdout).strip()[-300:]}")

    def _resolve_method(self) -> str:
        if self.requested_method != "auto":
            return self.requested_method
        res = self._docker(["docker", "exec", self.container, *DETECT_TCPDUMP])
        return "node" if res.exit_code == 0 and res.stdout.strip() else "helper"

    def _ensure_helper_image(self) -> None:
        if self._docker(["docker", "image", "inspect", self.helper_image]).exit_code == 0:
            return
        self.on_message(f"Pulling capture helper image {self.helper_image}...")
        res = self._docker(["docker", "pull", self.helper_image])
        if res.exit_code != 0:
            raise CaptureError(f"Could not pull capture helper image {self.helper_image}: "
                               f"{(res.stderr or res.stdout).strip()[-300:]}")

    def _read_stderr(self) -> None:
        try:
            for line in self._proc.stderr_lines():
                if line.startswith(PID_MARKER) and self._pid is None:
                    try:
                        self._pid = int(line.split()[1])
                    except (IndexError, ValueError):
                        pass
                    self._pid_seen.set()
                    continue
                if line.strip():
                    self.on_message(line)
        except Exception as exc:  # noqa: BLE001 - the stream ended one way or another
            logger.debug("Capture stderr reader stopped: %s", exc)
        finally:
            self._pid_seen.set()
            self._stderr_done.set()

    # --- output ---

    def read(self, size: int = 65536) -> bytes:
        """Next chunk of output; b"" once the capture has ended."""
        if self._proc is None:
            return b""
        try:
            data = self._proc.read(size)
        except Exception:  # noqa: BLE001 - closed underneath us by stop()
            data = b""
        if not data:
            self._finish()
        return data

    def _finish(self) -> None:
        if self.exit_code is None and self._proc is not None:
            self.exit_code = self._proc.wait(5)
            self._stderr_done.wait(2)  # let tcpdump's last words through
        self._cleanup()

    # --- stop ---

    def stop(self, reason: str = "stopped") -> None:
        """Kill tcpdump in the container (or the helper container)."""
        with self._lock:
            if not self.stopped:
                self.stopped = True
                self.stop_reason = reason
        self._cleanup()

    def _cleanup(self) -> None:
        with self._lock:
            if self._cleaned or self._proc is None:
                return
            self._cleaned = True
        if self._timer:
            self._timer.cancel()
        if self.method == "node":
            self._pid_seen.wait(5)
        argv = stop_command(self.method, self.container, self.name, self._pid)
        if argv:
            try:
                res = self._docker(argv)
                # docker rm -f of a helper that already exited is fine
                if res.exit_code != 0 and "no such container" not in res.stderr.lower():
                    logger.warning("Could not stop capture on %s: %s", self.container,
                                   (res.stderr or res.stdout).strip())
            except Exception as exc:  # noqa: BLE001 - still close our side
                logger.warning("Could not stop capture on %s: %s", self.container, exc)
        elif self.method == "node":
            logger.debug("No tcpdump PID seen for %s; nothing to kill", self.container)
        code = self._proc.wait(5)
        if code is None:
            logger.debug("Capture client still running after stop; closing it")
        elif self.exit_code is None:
            self.exit_code = code
        self._proc.close()
