import pytest

from clabfleet.runner import CommandError, SSHRunner


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
