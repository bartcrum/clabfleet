"""Command runners — execute containerlab on a lab host.

A lab host is either the local machine (commands run via subprocess) or a
remote Linux server reached over SSH (commands run via paramiko, files are
copied with SFTP). Both expose the same small interface so the deployer does
not care where containerlab actually runs.
"""

import logging
import posixpath
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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
    ) -> CommandResult:
        raise NotImplementedError

    def containerlab(
        self, args: list[str], cwd: Optional[str] = None, check: bool = True
    ) -> CommandResult:
        """Run a containerlab subcommand (with sudo if the host needs it)."""
        return self.run(["containerlab", *args], cwd=cwd, check=check, sudo=self.sudo)

    def makedirs(self, path: str) -> None:
        raise NotImplementedError

    def put_file(self, local_path: Path, remote_path: str) -> None:
        raise NotImplementedError

    def write_text(self, remote_path: str, text: str) -> None:
        raise NotImplementedError

    def exists(self, path: str) -> bool:
        return self.run(["test", "-e", path], check=False, sudo=False).exit_code == 0

    def remove_tree(self, path: str) -> None:
        # Lab directories created by containerlab are root-owned
        self.run(["rm", "-rf", "--", path], check=False, sudo=self.sudo)

    def close(self) -> None:
        pass


class LocalRunner(Runner):
    """Run commands on this machine."""

    name = "localhost"

    def run(self, args, cwd=None, check=True, sudo=None):
        use_sudo = self.sudo if sudo is None else sudo
        cmd = (["sudo"] if use_sudo else []) + list(args)
        logger.debug("local$ %s (cwd=%s)", shlex.join(cmd), cwd or ".")
        try:
            proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
            result = CommandResult(proc.returncode, proc.stdout, proc.stderr)
        except FileNotFoundError:
            # Same as a shell's "command not found"
            result = CommandResult(127, "", f"{cmd[0]}: command not found")
        if check and result.exit_code != 0:
            raise CommandError(shlex.join(cmd), result.exit_code, result.stderr)
        return result

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
    ):
        super().__init__(sudo=sudo)
        self.host = host
        self.username = username
        self.port = port
        self.key_file = key_file
        self.password = password
        self.name = name or host
        self._ssh = None
        self._sftp = None

    def _client(self):
        if self._ssh is None:
            import paramiko

            ssh = paramiko.SSHClient()
            ssh.load_system_host_keys()
            ssh.set_missing_host_key_policy(paramiko.WarningPolicy())
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

    def run(self, args, cwd=None, check=True, sudo=None):
        use_sudo = self.sudo if sudo is None else sudo
        # -n: never prompt for a password; remote sudo must be NOPASSWD
        cmd = shlex.join((["sudo", "-n"] if use_sudo else []) + list(args))
        if cwd:
            cmd = f"cd {shlex.quote(cwd)} && {cmd}"
        logger.debug("%s$ %s", self.name, cmd)
        _, stdout, stderr = self._client().exec_command(cmd)
        exit_code = stdout.channel.recv_exit_status()
        result = CommandResult(
            exit_code, stdout.read().decode(), stderr.read().decode()
        )
        if check and exit_code != 0:
            raise CommandError(f"[{self.name}] {cmd}", exit_code, result.stderr)
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
