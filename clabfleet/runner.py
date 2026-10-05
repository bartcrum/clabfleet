"""Command runners — execute containerlab on a lab host.

A lab host is either the local machine (commands run via subprocess) or a
remote Linux server reached over SSH (commands run via paramiko, files are
copied with SFTP). Both expose the same small interface so the deployer does
not care where containerlab actually runs.
"""

import logging
import os
import posixpath
import shlex
import shutil
import stat
import subprocess
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
    type. Without: containerlab is not installed setuid (where its own
    ``clab_admins`` group decides), so it needs sudo."""
    if os.geteuid() == 0:
        return None
    if sudo:
        try:
            ok = subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0
        except OSError:
            return "--sudo was given, but there is no sudo on this machine"
        return None if ok else f"sudo asks for a password here, so deploys will fail. {SUDO_HELP}"
    path = shutil.which("containerlab")
    if not path or os.stat(path).st_mode & stat.S_ISUID:
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
        logger.debug("local$ %s (cwd=%s)", shlex.join(cmd), cwd or ".")
        try:
            if on_output:
                result = self._run_streaming(cmd, cwd, on_output)
            else:
                proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                result = CommandResult(proc.returncode, proc.stdout, proc.stderr)
        except FileNotFoundError:
            # Same as a shell's "command not found"
            result = CommandResult(127, "", f"{cmd[0]}: command not found")
        if check and result.exit_code != 0:
            raise CommandError(shlex.join(cmd), result.exit_code, result.stderr)
        return result

    @staticmethod
    def _run_streaming(cmd, cwd, on_output) -> CommandResult:
        lines = []
        with subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True) as proc:
            for line in proc.stdout:
                lines.append(line)
                on_output(line.rstrip("\n"))
        output = "".join(lines)
        return CommandResult(proc.returncode, output, output)

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

    def client(self):
        """The underlying paramiko SSHClient (connects on first use)."""
        return self._client()

    def _client(self):
        if self._ssh is None:
            import paramiko

            ssh = paramiko.SSHClient()
            ssh.load_system_host_keys()
            if self.known_hosts.exists():
                ssh.load_host_keys(str(self.known_hosts))
            if self.host_key_policy == "strict":
                ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
            else:
                ssh.set_missing_host_key_policy(_remember_new_keys(self.known_hosts))
            kwargs = {"hostname": self.host, "port": self.port, "timeout": 30}
            if self.username:
                kwargs["username"] = self.username
            if self.key_file:
                kwargs["key_filename"] = str(Path(self.key_file).expanduser())
            if self.password:
                kwargs["password"] = self.password
            logger.info("Opening SSH connection to %s (%s)", self.name, self.host)
            ssh.connect(**kwargs)
            self._ssh = ssh
        return self._ssh

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
        logger.debug("%s$ %s", self.name, cmd)
        _, stdout, stderr = self._client().exec_command(cmd)
        if on_output:
            stdout.channel.set_combine_stderr(True)
            lines = []
            for line in stdout:
                lines.append(line)
                on_output(line.rstrip("\n"))
            output = "".join(lines)
            result = CommandResult(stdout.channel.recv_exit_status(), output, output)
        else:
            exit_code = stdout.channel.recv_exit_status()
            result = CommandResult(
                exit_code, stdout.read().decode(), stderr.read().decode()
            )
        if check and result.exit_code != 0:
            raise CommandError(f"[{self.name}] {cmd}", result.exit_code, result.stderr)
        return result

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
        for conn in (self._sftp, self._ssh):
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        self._sftp = None
        self._ssh = None
