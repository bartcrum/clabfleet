import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from clabfleet.runner import (
    CommandCancelled, CommandError, CommandResult, CommandTimeout, LocalRunner, PrivilegeError,
    Runner, SSHRunner, deadline, time_left,
)

sys.path.insert(0, str(Path(__file__).parent))  # ssh_server.py, next to this file


@pytest.fixture
def ssh(tmp_path):
    """(server, a runner connected to it): real SSH over a local socket."""
    pytest.importorskip("paramiko")
    from ssh_server import SSHServer

    with SSHServer() as server:
        runner = SSHRunner("127.0.0.1", port=server.port, username="lab", password="lab",
                           name="h1", known_hosts=str(tmp_path / "known_hosts"))
        yield server, runner
        runner.close()


def test_ssh_run_returns_output_and_exit_code(ssh):
    _, runner = ssh
    result = runner.run(["sh", "-c", "echo out; echo err >&2; exit 3"], check=False)
    assert (result.exit_code, result.stdout, result.stderr) == (3, "out\n", "err\n")
    with pytest.raises(CommandError) as exc:
        runner.run(["sh", "-c", "echo boom >&2; exit 2"])
    assert exc.value.exit_code == 2 and "boom" in exc.value.stderr and "[h1]" in exc.value.command
    assert runner.run(["pwd"], cwd="/tmp").stdout == "/tmp\n"


def test_ssh_streaming_run_returns_result(ssh):
    _, runner = ssh
    seen = []
    result = runner.run(["sh", "-c", "echo one; echo two >&2; sleep 0.3; printf three"],
                        on_output=seen.append)
    assert sorted(seen) == ["one", "three", "two"]  # both streams, line by line
    assert result.exit_code == 0 and result.stdout == result.stderr
    with pytest.raises(CommandError) as exc:
        runner.run(["sh", "-c", "echo boom; exit 2"], on_output=lambda line: None)
    assert exc.value.exit_code == 2 and "boom" in exc.value.stderr


def test_ssh_long_output_on_both_streams_does_not_hang(ssh):
    """More than the channel's window holds: waiting for the exit status
    before reading, as the runner once did, never returns."""
    _, runner = ssh
    megabytes = "head -c 3000000 /dev/zero | tr '\\0' x"
    with deadline(30):
        result = runner.run(["sh", "-c", f"{megabytes}; {megabytes} >&2"])
    assert (len(result.stdout), len(result.stderr)) == (3_000_000, 3_000_000)


def test_ssh_command_past_its_deadline_is_given_up(ssh):
    _, runner = ssh
    started = time.monotonic()
    with pytest.raises(CommandTimeout) as exc:
        with deadline(1):
            runner.run(["sleep", "30"])
    assert time.monotonic() - started < 5 and "no answer in time from h1" in str(exc.value)
    # Nothing starts once the time is up, and the runner works again after
    with deadline(0.01):
        time.sleep(0.02)
        with pytest.raises(CommandTimeout):
            runner.run(["true"])
    assert runner.run(["echo", "still here"]).stdout == "still here\n"
    # Without a deadline a command takes what it takes
    assert runner.run(["sh", "-c", "sleep 1.2; echo done"]).stdout == "done\n"


def test_ssh_abort_stops_a_running_command_and_refuses_more(ssh):
    _, runner = ssh
    threading.Timer(0.5, runner.abort).start()
    started = time.monotonic()
    with pytest.raises(CommandCancelled):
        runner.run(["sleep", "30"], on_output=lambda line: None)
    assert time.monotonic() - started < 5
    with pytest.raises(CommandCancelled):
        runner.run(["true"])
    with pytest.raises(CommandCancelled):
        runner.client()


def test_ssh_lost_connection_fails_the_command_and_the_next_one_reconnects(ssh):
    server, runner = ssh
    assert runner.run(["echo", "hi"]).stdout == "hi\n"
    threading.Timer(0.5, server.drop_connections).start()
    started = time.monotonic()
    with pytest.raises(Exception) as exc:  # CommandError, or paramiko's own word for it
        runner.run(["sleep", "30"])
    assert time.monotonic() - started < 10, exc.value
    assert runner.run(["echo", "back"]).stdout == "back\n"
    assert server.connections == 2


def test_ssh_first_use_from_many_threads_makes_one_connection(ssh):
    server, runner = ssh
    with ThreadPoolExecutor(max_workers=8) as pool:
        outputs = list(pool.map(lambda i: runner.run(["echo", str(i)]).stdout, range(8)))
    assert outputs == [f"{i}\n" for i in range(8)]
    assert server.connections == 1


def test_local_run_deadline_and_abort():
    runner = LocalRunner()
    started = time.monotonic()
    with pytest.raises(CommandTimeout):
        with deadline(1):
            runner.run(["sleep", "30"])
    assert time.monotonic() - started < 5
    result = runner.run(["sh", "-c", "echo out; echo err >&2; exit 3"], check=False)
    assert (result.exit_code, result.stdout, result.stderr) == (3, "out\n", "err\n")
    seen = []
    assert runner.run(["sh", "-c", "printf 'a\\nb'"], on_output=seen.append).stdout == "a\nb\n"
    assert seen == ["a", "b"]
    assert runner.run(["no-such-program-here"], check=False).exit_code == 127

    threading.Timer(0.5, runner.abort).start()
    started = time.monotonic()
    with pytest.raises(CommandCancelled):
        runner.run(["sleep", "30"], on_output=lambda line: None)
    assert time.monotonic() - started < 5
    with pytest.raises(CommandCancelled):
        runner.run(["true"])


def test_deadlines_nest_to_the_nearest_end():
    assert time_left() is None
    with deadline(60):
        assert 59 < time_left() <= 60
        with deadline(5):
            assert 4 < time_left() <= 5
            with deadline(600):  # an outer deadline that ends sooner stays
                assert time_left() <= 5
        assert time_left() > 50
    assert time_left() is None


class RecordingClient:
    """Stands in for paramiko.SSHClient: records the host key setup, never connects."""

    instances: list = []

    def __init__(self):
        import paramiko
        self.host_keys = paramiko.HostKeys()
        self.loaded, self.policy, self.connected = [], None, None
        RecordingClient.instances.append(self)

    def load_system_host_keys(self):
        pass

    def load_host_keys(self, path):
        self.loaded.append(path)

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def get_host_keys(self):
        return self.host_keys

    def connect(self, **kwargs):
        self.connected = kwargs

    def get_transport(self):
        return None

    def _log(self, level, msg):  # used by paramiko's RejectPolicy
        pass


def _host_key():
    import paramiko
    return paramiko.RSAKey.generate(1024)


def test_accept_new_saves_unknown_host_keys(tmp_path, monkeypatch):
    paramiko = pytest.importorskip("paramiko")
    monkeypatch.setattr(paramiko, "SSHClient", RecordingClient)
    known = tmp_path / "dir" / "known_hosts"
    runner = SSHRunner("10.0.0.1", name="h1", known_hosts=str(known))
    client = runner.client()
    assert client.loaded == []  # no file yet
    key = _host_key()
    client.policy.missing_host_key(client, "[10.0.0.1]:2222", key)

    assert known.stat().st_mode & 0o777 == 0o600
    assert known.parent.stat().st_mode & 0o777 == 0o700
    saved = paramiko.HostKeys(str(known))
    assert saved.lookup("[10.0.0.1]:2222")[key.get_name()] == key
    assert client.get_host_keys().lookup("[10.0.0.1]:2222")[key.get_name()] == key

    # The next connection loads the remembered keys
    SSHRunner("10.0.0.1", known_hosts=str(known)).client()
    assert RecordingClient.instances[-1].loaded == [str(known)]


def test_strict_rejects_unknown_host_keys(tmp_path, monkeypatch):
    paramiko = pytest.importorskip("paramiko")
    monkeypatch.setattr(paramiko, "SSHClient", RecordingClient)
    known = tmp_path / "known_hosts"
    client = SSHRunner("10.0.0.1", host_key_policy="strict", known_hosts=str(known)).client()
    with pytest.raises(paramiko.SSHException):
        client.policy.missing_host_key(client, "10.0.0.1", _host_key())
    assert not known.exists()
    with pytest.raises(ValueError, match="host_key_policy"):
        SSHRunner("10.0.0.1", host_key_policy="trust-all")


class _RootedHost(Runner):
    """A host where containerlab refuses what needs root, unless run through sudo."""

    name = "lab-a"

    def __init__(self, sudo: bool = False):
        super().__init__(sudo=sudo)
        self.calls = []

    def run(self, args, cwd=None, check=True, sudo=None, on_output=None):
        self.calls.append((list(args), bool(sudo)))
        if not sudo and args[1] != "inspect":
            return CommandResult(1, "", "ERROR\n  This containerlab command requires root privileges "
                                        "or root via SUID to run, effective UID: 1000 SUID: 1000.")
        if args[1] == "broken":
            return CommandResult(1, "", "no such command")
        return CommandResult(0, "ok", "")


def test_containerlab_says_what_to_do_when_it_needs_root():
    host = _RootedHost()
    for check in (True, False):
        with pytest.raises(PrivilegeError) as exc:
            host.containerlab(["destroy", "-t", "x.clab.yml"], check=check)
        assert "needs root on lab-a" in str(exc.value) and "--sudo" in str(exc.value)
        assert "requires root privileges" not in str(exc.value)
    # sudo is never used unless asked for
    assert host.calls == [(["containerlab", "destroy", "-t", "x.clab.yml"], False)] * 2


def test_containerlab_is_otherwise_untouched():
    host = _RootedHost()
    assert host.containerlab(["inspect", "--all"]).stdout == "ok"  # works without root
    asked = _RootedHost(sudo=True)
    assert asked.containerlab(["destroy", "-t", "x.clab.yml"]).stdout == "ok"
    assert asked.calls == [(["containerlab", "destroy", "-t", "x.clab.yml"], True)]
    # Any other failure is the command's own, as before
    assert asked.containerlab(["broken"], check=False).exit_code == 1
    with pytest.raises(CommandError):
        asked.containerlab(["broken"])


class _LockedSudo(Runner):
    """A host where sudo wants a password and may not ask for it."""

    name = "lab-a"

    def run(self, args, cwd=None, check=True, sudo=None, on_output=None):
        return CommandResult(1, "", "sudo: a password is required")


def test_containerlab_says_what_to_do_when_sudo_wants_a_password():
    with pytest.raises(PrivilegeError) as exc:
        _LockedSudo(sudo=True).containerlab(["deploy", "-t", "x.clab.yml"])
    assert "sudo asks for a password on lab-a" in str(exc.value)
    assert "sudo -v" in str(exc.value) and "docs/troubleshooting.md" in str(exc.value)
    # Not asked to use sudo: its messages are not ours to explain
    with pytest.raises(CommandError):
        _LockedSudo().containerlab(["deploy", "-t", "x.clab.yml"])


def test_local_root_problem(tmp_path, monkeypatch):
    from clabfleet import runner

    def sudo_answers(code, stderr=""):
        def run(cmd, **kwargs):
            # containerlab itself: a sudo rule may allow it and nothing else
            assert cmd == ["sudo", "-n", "containerlab", "version"]
            return subprocess.CompletedProcess(cmd, code, "", stderr)
        monkeypatch.setattr(runner.subprocess, "run", run)

    binary = tmp_path / "containerlab"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(runner.shutil, "which", lambda name: str(binary))
    monkeypatch.setattr(runner.os, "geteuid", lambda: 1000)

    # --sudo: fine while sudo needs no password
    sudo_answers(0)
    assert runner.local_root_problem(True) is None
    sudo_answers(1, "sudo: a password is required\n")
    assert "sudo asks for a password here" in runner.local_root_problem(True)
    # containerlab failing for a reason of its own is not sudo's doing
    sudo_answers(1, "Error: something else\n")
    assert runner.local_root_problem(True) is None
    # No --sudo: containerlab must be setuid to get root on its own
    binary.chmod(0o755)
    assert "Start clabfleet with --sudo" in runner.local_root_problem(False)
    binary.chmod(0o4755)
    assert runner.local_root_problem(False) is None
    # Nothing to say without containerlab, or as root
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    assert runner.local_root_problem(False) is None
    monkeypatch.setattr(runner.os, "geteuid", lambda: 0)
    assert runner.local_root_problem(True) is None


def test_gui_warns_at_start_when_deploys_would_fail(tmp_path, monkeypatch, capsys):
    pytest.importorskip("aiohttp")
    from clabfleet import cli
    from clabfleet.gui import auth, server

    monkeypatch.setattr(auth, "DEFAULT_USERS_FILE", tmp_path / "home" / "users.yaml")
    monkeypatch.setattr(server, "run", lambda *a, **kw: None)
    asked = []
    monkeypatch.setattr(cli, "local_root_problem",
                        lambda sudo: asked.append(sudo) or ("no root" if sudo else None))
    base = ["gui", "--dir", str(tmp_path), "--no-browser"]
    assert cli.main(["--sudo", *base]) == 0  # it still starts: looking needs no root
    assert "WARNING: no root" in capsys.readouterr().err
    assert cli.main(base) == 0
    assert "WARNING" not in capsys.readouterr().err
    assert asked == [True, False]
