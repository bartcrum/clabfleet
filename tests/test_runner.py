import subprocess

import pytest

from clabfleet.runner import CommandError, CommandResult, PrivilegeError, Runner, SSHRunner


class FakeChannel:
    def __init__(self, exit_code):
        self.exit_code = exit_code

    def set_combine_stderr(self, combine):
        pass

    def recv_exit_status(self):
        return self.exit_code


class FakeStdout:
    def __init__(self, lines, exit_code):
        self.lines = lines
        self.channel = FakeChannel(exit_code)

    def __iter__(self):
        return iter(self.lines)


class FakeClient:
    def __init__(self, lines, exit_code):
        self.lines, self.exit_code = lines, exit_code

    def exec_command(self, cmd):
        return None, FakeStdout(self.lines, self.exit_code), None


def _runner(monkeypatch, lines, exit_code):
    runner = SSHRunner("10.0.0.1", name="h1")
    monkeypatch.setattr(runner, "_client", lambda: FakeClient(lines, exit_code))
    return runner


def test_ssh_streaming_run_returns_result(monkeypatch):
    seen = []
    runner = _runner(monkeypatch, ["one\n", "two\n"], 0)
    result = runner.run(["containerlab", "deploy"], check=True, on_output=seen.append)
    assert seen == ["one", "two"]
    assert (result.exit_code, result.stdout) == (0, "one\ntwo\n")


def test_ssh_streaming_run_checks_exit_code(monkeypatch):
    runner = _runner(monkeypatch, ["boom\n"], 2)
    with pytest.raises(CommandError) as exc:
        runner.run(["containerlab", "deploy"], check=True, on_output=lambda line: None)
    assert exc.value.exit_code == 2 and "boom" in exc.value.stderr

    result = runner.run(["containerlab", "deploy"], check=False, on_output=lambda line: None)
    assert result.exit_code == 2


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
