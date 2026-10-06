"""Command runners — execute containerlab on a lab host.

A lab host is either the local machine (commands run via subprocess) or a
remote Linux server reached over SSH (commands run via paramiko, files are
copied with SFTP). Both expose the same small interface so the deployer does
not care where containerlab actually runs.
"""

import contextlib
import logging
import os
import posixpath
import selectors
import shlex
import shutil
import socket
import stat
import subprocess
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# Called with each line of output as a command runs (for live job logs)
OutputCallback = Callable[[str], None]

logger = logging.getLogger(__name__)


class CommandError(Exception):
    """Raised when a command exits non-zero and ``check`` is set."""

    def __init__(self, command: str, exit_code: int, stderr: str):
        super().__init__(
            f"Command failed (exit {exit_code}): {command}\n{stderr.strip()}"
        )
        self.command = command
        self.exit_code = exit_code
        self.stderr = stderr


class CommandTimeout(Exception):
    """A command was not done within its ``deadline``; it was stopped, or
    left behind where it could not be.

    Not a ``CommandError``: that is a command that ran and failed, which
    callers often take in their stride (no such file, not installed). A host
    that does not answer is a different matter and must not pass for it."""

    def __init__(self, command: str, where: str = ""):
        super().__init__(f"no answer in time{f' from {where}' if where else ''}: {command}")
        self.command = command


class CommandCancelled(BaseException):
    """The runner was told to stop (``Runner.abort``): a cancelled job.

    Not an ``Exception``, like ``KeyboardInterrupt``: the code between a
    command and the job that was cancelled catches errors per host and
    carries on (the next host, a rollback, another round of waiting), and a
    cancel must end all of that."""

    def __init__(self, command: str):
        super().__init__(f"cancelled: {command}")
        self.command = command


# When the commands this thread runs must be done (time.monotonic()), if at all
_DEADLINE: ContextVar[Optional[float]] = ContextVar("clabfleet_deadline", default=None)

CONNECT_TIMEOUT = 15   # seconds to reach a host over SSH (TCP, banner, login each)
KEEPALIVE = 15         # seconds between SSH keepalives on an idle connection
DEAD_PEER_TIMEOUT = 30  # seconds without any answer before a connection counts as dead
POLL = 0.05            # seconds between looks at a running command's output


@contextlib.contextmanager
def deadline(seconds: float):
    """The commands this thread runs inside the block must be done, all
    together, within ``seconds``: one that is not raises ``CommandTimeout``.
    For reads that must not hang on a host that stopped answering (the GUI's
    polls, a deploy's planning). Without it a command may take as long as it
    takes, as a deploy does. An enclosing deadline that ends sooner stays."""
    end = time.monotonic() + seconds
    outer = _DEADLINE.get()
    token = _DEADLINE.set(end if outer is None else min(outer, end))
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def time_left() -> Optional[float]:
    """Seconds until this thread's deadline (0 when it has passed); None without one."""
    end = _DEADLINE.get()
    return None if end is None else max(0.0, end - time.monotonic())


class _Lines:
    """Bytes in, whole lines out to a callback as they complete."""

    def __init__(self, on_output: OutputCallback):
        self._on_output, self._partial, self.lines = on_output, b"", []

    def feed(self, data: bytes) -> None:
        *whole, self._partial = (self._partial + data).split(b"\n")
        for raw in whole:
            self._emit(raw)

    def finish(self) -> str:
        if self._partial:
            self._emit(self._partial)
            self._partial = b""
        return "".join(line + "\n" for line in self.lines)

    def _emit(self, raw: bytes) -> None:
        line = raw.decode(errors="replace")
        self.lines.append(line)
        self._on_output(line)


class PrivilegeError(Exception):
    """containerlab needs root on a host and did not get it: clabfleet was
    not told to use sudo, or sudo wants a password."""


# How containerlab refuses a command that needs root
NEEDS_ROOT = "requires root privileges"
# How sudo refuses when it would have to ask for a password and may not
# (`sudo -n`) or cannot (no terminal)
SUDO_NEEDS_PASSWORD = ("a password is required", "a terminal is required")
SUDO_HELP = ("Unlock sudo first (`sudo -v` in the terminal clabfleet runs in; it lasts "
             "a few minutes), or let sudo run containerlab without a password "
             "(see docs/troubleshooting.md)")


def local_root_problem(sudo: bool) -> Optional[str]:
    """Why containerlab will not get root on this machine, or None if it
    should: what a deploy would run into, known before anyone tries one.

    With ``sudo``: sudo would ask for a password, which a GUI job cannot
    type. It is asked about containerlab itself, as a sudo rule may allow
    that one command without a password and nothing else. Without:
    containerlab is not installed setuid (where its own ``clab_admins``
    group decides), so it needs sudo."""
    path = shutil.which("containerlab")
    if os.geteuid() == 0 or not path:
        return None
    if sudo:
        try:
            res = subprocess.run(["sudo", "-n", "containerlab", "version"],
                                 capture_output=True, text=True)
        except OSError:
            return "--sudo was given, but there is no sudo on this machine"
        if res.returncode != 0 and any(t in res.stderr + res.stdout for t in SUDO_NEEDS_PASSWORD):
            return f"sudo asks for a password here, so deploys will fail. {SUDO_HELP}"
        return None
    if os.stat(path).st_mode & stat.S_ISUID:
        return None
    return ("containerlab needs root here, so deploys will fail. Start clabfleet with --sudo "
            "(or CLAB_SUDO=1)")


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


class Runner:
    """Base class for command runners."""

    name: str = ""

    def __init__(self, sudo: bool = False):
        self.sudo = sudo
        self.cancelled = False  # set by abort(): nothing more runs through this runner
        # False: use `sudo -n` so a missing NOPASSWD rule fails fast instead
        # of waiting on a password prompt nobody can see (e.g. the GUI)
        self.interactive_sudo = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def run(
        self,
        args: list[str],
        cwd: Optional[str] = None,
        check: bool = True,
        sudo: Optional[bool] = None,
        on_output: Optional[OutputCallback] = None,
    ) -> CommandResult:
        """Run a command.

        With ``on_output``, stdout and stderr are merged and passed to the
        callback line by line as they arrive; the result then carries the
        combined output in both ``stdout`` and ``stderr``.
        """
        raise NotImplementedError

    def containerlab(
        self,
        args: list[str],
        cwd: Optional[str] = None,
        check: bool = True,
        on_output: Optional[OutputCallback] = None,
    ) -> CommandResult:
        """Run a containerlab subcommand (with sudo if the host needs it).

        Without sudo set, a command containerlab refuses for lack of root
        raises ``PrivilegeError``, which says what to do, in place of
        containerlab's own error. So does sudo refusing for want of a
        password. sudo is never used unless asked for."""
        cmd = ["containerlab", *args]
        result = self.run(cmd, cwd=cwd, check=False, sudo=self.sudo, on_output=on_output)
        if self.sudo and result.exit_code != 0 and any(
                text in result.stderr + result.stdout for text in SUDO_NEEDS_PASSWORD):
            raise PrivilegeError(f"sudo asks for a password on {self.name or 'this host'}, "
                                 f"and nobody is there to type it. {SUDO_HELP}")
        if not self.sudo and result.exit_code != 0 and NEEDS_ROOT in result.stdout + result.stderr:
            raise PrivilegeError(
                f"containerlab needs root on {self.name or 'this host'}. Run clabfleet with --sudo "
                "(or CLAB_SUDO=1; in a cluster file: sudo: true for the host), "
                "or as a user containerlab accepts")
        if check and result.exit_code != 0:
            raise CommandError(shlex.join(cmd), result.exit_code, result.stderr)
        return result

    def abort(self) -> None:
        """Stop what is running through this runner and refuse anything
        more (a cancelled job). A command that cannot be stopped, such as
        one on a remote host or under sudo, is left to finish on its own."""
        self.cancelled = True
        self.close()

    def _interrupted(self, command: str) -> None:
        """Raise if this runner was aborted or the thread's deadline has passed."""
        if self.cancelled:
            raise CommandCancelled(command)
        if time_left() == 0:
            raise CommandTimeout(command, self.name)

    def makedirs(self, path: str) -> None:
        raise NotImplementedError

    def put_file(self, local_path: Path, remote_path: str) -> None:
        raise NotImplementedError

    def write_text(self, remote_path: str, text: str) -> None:
        raise NotImplementedError

    def exists(self, path: str) -> bool:
        return self.run(["test", "-e", path], check=False, sudo=False).exit_code == 0

    def remove_tree(self, path: str) -> None:
        # No sudo: `containerlab destroy --cleanup` has already removed the
        # root-owned clab-<lab>/ directory; what's left was written by us.
        result = self.run(["rm", "-rf", "--", path], check=False, sudo=False)
        if result.exit_code != 0:
            logger.warning("Could not remove %s on %s: %s",
                           path, self.name, result.stderr.strip())

    def close(self) -> None:
        pass


class LocalRunner(Runner):
    """Run commands on this machine."""

    name = "localhost"

    def run(self, args, cwd=None, check=True, sudo=None, on_output=None):
        use_sudo = self.sudo if sudo is None else sudo
        sudo_cmd = ["sudo"] if self.interactive_sudo else ["sudo", "-n"]
        cmd = (sudo_cmd if use_sudo else []) + list(args)
        text = shlex.join(cmd)
        logger.debug("local$ %s (cwd=%s)", text, cwd or ".")
        self._interrupted(text)
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT if on_output else subprocess.PIPE)
        except FileNotFoundError:
            # Same as a shell's "command not found"
            result = CommandResult(127, "", f"{cmd[0]}: command not found")
        else:
            result = self._collect(proc, text, on_output)
        if check and result.exit_code != 0:
            raise CommandError(text, result.exit_code, result.stderr)
        return result

    def _collect(self, proc: subprocess.Popen, text: str, on_output) -> CommandResult:
        """Read a process's output until it ends, looking up between reads
        for the deadline and for ``abort``."""
        lines = _Lines(on_output) if on_output else None
        chunks = {proc.stdout: [], proc.stderr: []}
        try:
            with selectors.DefaultSelector() as waiting:
                for stream in (proc.stdout, proc.stderr):
                    if stream is not None:
                        waiting.register(stream, selectors.EVENT_READ)
                while waiting.get_map():
                    self._interrupted(text)
                    left = time_left()
                    for key, _ in waiting.select(0.5 if left is None else min(0.5, left)):
                        data = os.read(key.fd, 65536)
                        if not data:
                            waiting.unregister(key.fileobj)
                        elif lines:
                            lines.feed(data)
                        else:
                            chunks[key.fileobj].append(data)
            while proc.poll() is None:
                self._interrupted(text)
                time.sleep(POLL)
        except (CommandTimeout, CommandCancelled):
            self._stop(proc)
            raise
        finally:
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        if lines:
            output = lines.finish()
            return CommandResult(proc.returncode, output, output)
        return CommandResult(proc.returncode, b"".join(chunks[proc.stdout]).decode(errors="replace"),
                             b"".join(chunks[proc.stderr]).decode(errors="replace"))

    @staticmethod
    def _stop(proc: subprocess.Popen) -> None:
        """End a process we stopped waiting for. One started through sudo
        belongs to root and may not be ours to signal: it is left to finish."""
        try:
            proc.terminate()
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(2)
        except (PermissionError, ProcessLookupError, subprocess.TimeoutExpired):
            logger.warning("Could not stop %s (pid %s); leaving it", shlex.join(proc.args), proc.pid)

    def makedirs(self, path):
        Path(path).mkdir(parents=True, exist_ok=True)

    def put_file(self, local_path, remote_path):
        dest = Path(remote_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if Path(local_path).resolve() != dest.resolve():
            shutil.copy2(local_path, dest)

    def write_text(self, remote_path, text):
        dest = Path(remote_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)


# How SSHRunner treats a lab host's SSH host key: "accept-new" trusts and
# remembers a key it has never seen (like OpenSSH's StrictHostKeyChecking
# accept-new); "strict" only accepts keys already in a known_hosts file. Both
# refuse a key that differs from the remembered one.
HOST_KEY_POLICIES = ("accept-new", "strict")
DEFAULT_KNOWN_HOSTS = "~/.clabfleet/known_hosts"


def _remember_new_keys(path: Path):
    """paramiko policy that accepts an unknown host key and appends it to ``path``."""
    import paramiko

    class RememberNewKeys(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            client.get_host_keys().add(hostname, key.get_name(), key)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a") as fh:
                fh.write(f"{hostname} {key.get_name()} {key.get_base64()}\n")
            logger.warning("New SSH host key for %s (%s %s) saved to %s", hostname,
                           key.get_name(), getattr(key, "fingerprint", ""), path)

    return RememberNewKeys()


class SSHRunner(Runner):
    """Run commands on a remote host over SSH.

    Relative paths are resolved against the SSH user's home directory, both
    for commands (the shell starts there) and for SFTP transfers.
    """

    def __init__(
        self,
        host: str,
        username: Optional[str] = None,
        port: int = 22,
        key_file: Optional[str] = None,
        password: Optional[str] = None,
        sudo: bool = False,
        name: Optional[str] = None,
        host_key_policy: str = "accept-new",
        known_hosts: Optional[str] = None,
    ):
        super().__init__(sudo=sudo)
        if host_key_policy not in HOST_KEY_POLICIES:
            raise ValueError(f"host_key_policy must be one of {', '.join(HOST_KEY_POLICIES)}")
        self.host_key_policy = host_key_policy
        self.known_hosts = Path(known_hosts or DEFAULT_KNOWN_HOSTS).expanduser()
        self.host = host
        self.username = username
        self.port = port
        self.key_file = key_file
        self.password = password
        self.name = name or host
        self._ssh = None
        self._sftp = None
        self._connecting = threading.Lock()  # one connection, however many threads ask first

    def client(self):
        """The underlying paramiko SSHClient (connects on first use)."""
        return self._client()

    def _client(self):
        with self._connecting:
            if self.cancelled:
                raise CommandCancelled(f"ssh {self.name}")
            if self._ssh is not None:
                transport = self._ssh.get_transport()
                if transport is not None and transport.is_active():
                    return self._ssh
                logger.info("SSH connection to %s was lost; connecting again", self.name)
                self._drop()
            self._ssh = self._connect()
            return self._ssh

    def _connect(self):
        import paramiko

        ssh = paramiko.SSHClient()
        ssh.load_system_host_keys()
        if self.known_hosts.exists():
            ssh.load_host_keys(str(self.known_hosts))
        if self.host_key_policy == "strict":
            ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            ssh.set_missing_host_key_policy(_remember_new_keys(self.known_hosts))
        left = time_left()
        wait = CONNECT_TIMEOUT if left is None else max(1.0, min(CONNECT_TIMEOUT, left))
        kwargs = {"hostname": self.host, "port": self.port, "timeout": wait,
                  "banner_timeout": wait, "auth_timeout": wait}
        if self.username:
            kwargs["username"] = self.username
        if self.key_file:
            kwargs["key_filename"] = str(Path(self.key_file).expanduser())
        if self.password:
            kwargs["password"] = self.password
        logger.info("Opening SSH connection to %s (%s)", self.name, self.host)
        ssh.connect(**kwargs)
        # A host that goes away without a word (power, network) must not hold
        # a command or a file copy forever: keepalives keep data in flight on
        # an idle connection, and the kernel gives up on data that is not
        # acknowledged within DEAD_PEER_TIMEOUT
        transport = ssh.get_transport()
        if transport is not None:
            transport.set_keepalive(KEEPALIVE)
            sock = getattr(transport, "sock", None)
            if hasattr(socket, "TCP_USER_TIMEOUT") and hasattr(sock, "setsockopt"):
                with contextlib.suppress(OSError):
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT,
                                    DEAD_PEER_TIMEOUT * 1000)
        return ssh

    def _sftp_client(self):
        if self._sftp is None:
            self._sftp = self._client().open_sftp()
        return self._sftp

    def run(self, args, cwd=None, check=True, sudo=None, on_output=None):
        use_sudo = self.sudo if sudo is None else sudo
        # -n: never prompt for a password; remote sudo must be NOPASSWD
        cmd = shlex.join((["sudo", "-n"] if use_sudo else []) + list(args))
        if cwd:
            cmd = f"cd {shlex.quote(cwd)} && {cmd}"
        text = f"[{self.name}] {cmd}"
        logger.debug("%s$ %s", self.name, cmd)
        self._interrupted(text)
        left = time_left()
        channel = self._client().get_transport().open_session(
            timeout=CONNECT_TIMEOUT if left is None else max(1.0, min(CONNECT_TIMEOUT, left)))
        try:
            if on_output:
                channel.set_combine_stderr(True)
            channel.exec_command(cmd)
            result = self._collect(channel, text, on_output)
        finally:
            channel.close()
        if check and result.exit_code != 0:
            raise CommandError(text, result.exit_code, result.stderr)
        return result

    def _collect(self, channel, text: str, on_output) -> CommandResult:
        """Read a command's output as it comes, on both streams, until it
        ends. (Waiting for the exit status first would never return for a
        command that says more than the channel's window holds.) Between
        reads, look up for the deadline and for ``abort``."""
        lines = _Lines(on_output) if on_output else None
        out, err = [], []
        while True:
            read = False
            if channel.recv_ready():
                data = channel.recv(65536)
                read = bool(data)
                lines.feed(data) if lines else out.append(data)
            if channel.recv_stderr_ready():
                data = channel.recv_stderr(65536)
                read = read or bool(data)
                err.append(data)
            if read:
                continue
            if channel.exit_status_ready() or channel.closed:
                # Done, and what it said has been read
                if not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                continue
            self._interrupted(text)
            time.sleep(POLL)
        self._interrupted(text)  # an abort closes the connection under the command
        exit_code = channel.recv_exit_status() if channel.exit_status_ready() else -1
        if exit_code == -1:  # closed with no word of how the command ended
            raise CommandError(text, 255, f"the connection to {self.name} was lost")
        if lines:
            output = lines.finish()
            return CommandResult(exit_code, output, output)
        return CommandResult(exit_code, b"".join(out).decode(errors="replace"),
                             b"".join(err).decode(errors="replace"))

    def makedirs(self, path):
        sftp = self._sftp_client()
        current = ""
        for part in path.split("/"):
            if not part:
                current = "/"
                continue
            current = posixpath.join(current, part) if current else part
            try:
                sftp.stat(current)
            except FileNotFoundError:
                sftp.mkdir(current)

    def put_file(self, local_path, remote_path):
        self.makedirs(posixpath.dirname(remote_path) or ".")
        logger.debug("Uploading %s → %s:%s", local_path, self.name, remote_path)
        self._sftp_client().put(str(local_path), remote_path)

    def write_text(self, remote_path, text):
        self.makedirs(posixpath.dirname(remote_path) or ".")
        with self._sftp_client().open(remote_path, "w") as fh:
            fh.write(text)

    def close(self):
        self._drop()

    def _drop(self) -> None:
        for conn in (self._sftp, self._ssh):
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        self._sftp = None
        self._ssh = None
