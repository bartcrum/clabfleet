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
