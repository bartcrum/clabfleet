import socket
import threading
import time

from clabfleet import cli, readiness
from clabfleet.cluster import ClusterConfig, HostInfo
from clabfleet.gui.state import Workspace
from clabfleet.readiness import ReadinessCache, check_ready, wait_until_ready
from clabfleet.runner import CommandResult, LocalRunner, Runner


def _c(kind, status="Up 1 minute", state="running", ipv4="172.20.20.5", name="clab-l-n"):
    return {"id": "abc", "lab": "l", "node": "n", "container": name, "kind": kind,
            "state": state, "status": status, "ipv4": ipv4}


class Probe(Runner):
    """Answers readiness probes: docker exec exits cli_rc, SSH banner prints banner."""

    def __init__(self, cli_rc=0, banner="SSH-", ssh_rc=0):
        super().__init__()
        self.cli_rc, self.banner, self.ssh_rc = cli_rc, banner, ssh_rc
        self.calls = []

    def run(self, args, cwd=None, check=True, sudo=None, on_output=None):
        self.calls.append(list(args))
        if "docker" in args:
            return CommandResult(self.cli_rc, "", "")
        return CommandResult(self.ssh_rc, self.banner, "")


def test_check_ready_by_kind_and_health():
    assert check_ready(Probe(), _c("linux", state="exited")) == (False, "exited")
    assert check_ready(Probe(), _c("cisco_iol", "Up 2 minutes (healthy)")) == (True, "healthy")
    assert check_ready(Probe(), _c("cisco_xrv9k", "Up 9 s (health: starting)"))[0] is False
    assert check_ready(Probe(), _c("cisco_xrv9k", "Up 9 s (unhealthy)"))[0] is False
    assert check_ready(Probe(), _c("linux")) == (True, "running")

    runner = Probe(cli_rc=0)
    assert check_ready(runner, _c("arista_ceos")) == (True, "CLI answers")
    assert runner.calls[0] == ["timeout", "15", "docker", "exec", "clab-l-n",
                               "Cli", "-p", "15", "-c", "show version"]
    assert check_ready(Probe(cli_rc=1), _c("arista_ceos")) == (False, "CLI not up yet")

    assert check_ready(Probe(banner="SSH-"), _c("cisco_iol")) == (True, "SSH answers")
    assert check_ready(Probe(banner="", ssh_rc=124), _c("cisco_iol")) == (False, "SSH not up yet")
    assert check_ready(Probe(), _c("cisco_iol", ipv4="")) == (False, "no management address yet")
    assert check_ready(Probe(banner="", ssh_rc=127), _c("cisco_iol"))[1].startswith("cannot probe")


def test_ssh_banner_probe_with_bash(monkeypatch):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    monkeypatch.setattr(readiness, "SSH_PORT", srv.getsockname()[1])

    def serve():
        conn, _ = srv.accept()
        conn.sendall(b"SSH-2.0-test\r\n")
        conn.close()
    threading.Thread(target=serve, daemon=True).start()
    try:
        assert check_ready(LocalRunner(), _c("cisco_iol", ipv4="127.0.0.1"), timeout=5) == (
            True, "SSH answers")
    finally:
        srv.close()
    # Nothing listening any more: not ready, no exception
    assert check_ready(LocalRunner(), _c("cisco_iol", ipv4="127.0.0.1"), timeout=5)[0] is False


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_wait_until_ready_tracks_progress():
    clock = FakeClock()
    ready_at = {"a": 0, "b": 10, "c": 20}
    polled = []

    def poll(pending):
        polled.append(list(pending))
        return {n: (clock.now >= ready_at[n], "booting") for n in pending if n != "c" or clock.now > 5}

    result = wait_until_ready(poll, ["a", "b", "c"], timeout=60, interval=5,
                              clock=clock, sleep=clock.sleep)
    assert result == {"ready": True, "seconds": 20.0, "pending": {}}
    assert polled[0] == ["a", "b", "c"] and polled[1] == ["b", "c"]


def test_wait_until_ready_times_out():
    clock = FakeClock()
    result = wait_until_ready(lambda pending: {"a": (True, "ok"), "b": (False, "SSH not up yet")},
                              ["a", "b", "c"], timeout=12, interval=5,
                              clock=clock, sleep=clock.sleep)
    assert result["ready"] is False and result["seconds"] == 12.0
    assert result["pending"] == {"b": "SSH not up yet", "c": "not created yet"}


def test_cache_rechecks_only_not_ready_and_one_probe_at_a_time():
    clock = FakeClock()
    cache = ReadinessCache(recheck=10, clock=clock)
    assert cache.get("k") == (None, True)
    assert cache.claim("k") and not cache.claim("k")
    cache.store("k", False, "booting")
    assert cache.get("k") == ((False, "booting"), False)
    clock.now = 10
    assert cache.get("k") == ((False, "booting"), True)
    cache.store("k", True, "CLI answers")
    clock.now = 1000
    assert cache.get("k") == ((True, "CLI answers"), False)
    cache.prune({"other"})
    assert cache.get("k") == (None, True)


def test_gui_runtime_reports_readiness_from_background_probes(tmp_path, monkeypatch):
    import json

    class Host(Probe):
        def run(self, args, cwd=None, check=True, sudo=None, on_output=None):
            if args[0] == "containerlab":
                return CommandResult(0, json.dumps({"l": [
                    {"Id": "1", "Names": ["clab-l-sw"], "State": "running", "Status": "Up",
                     "Labels": {"containerlab": "l", "clab-node-name": "sw",
                                "clab-node-kind": "arista_ceos"}},
                    {"Id": "2", "Names": ["clab-l-pc"], "State": "running", "Status": "Up",
                     "Labels": {"containerlab": "l", "clab-node-name": "pc",
                                "clab-node-kind": "linux"}},
                ]}), "")
            return super().run(args, cwd, check, sudo, on_output)

    host = Host(cli_rc=1)
    ws = Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])
    monkeypatch.setattr(ws, "runner", lambda h: host)

    def states():
        containers = ws.runtime(max_age=0)[0].containers  # not the cached result
        return {c["node"]: (c["ready"], c["ready_detail"]) for c in containers}

    assert states() == {"sw": (None, "checking"), "pc": (None, "checking")}
    for _ in range(100):
        if states()["sw"][0] is not None and states()["pc"][0] is not None:
            break
        time.sleep(0.02)
    assert states() == {"sw": (False, "CLI not up yet"), "pc": (True, "running")}
    ws.close()


def test_cli_wait_flags_and_exit_code(monkeypatch, tmp_path):
    from clabfleet.deployer import LabDeployer
    seen = {}
    outcome = {"ready": True}

    def deploy(self, path, **kw):
        seen.update(kw)
        return {"hosts": {"localhost": {"status": "deployed"}}, "readiness": dict(outcome)}

    monkeypatch.setattr(LabDeployer, "deploy", deploy)
    topo = tmp_path / "t.clab.yml"
    topo.write_text("name: t\ntopology:\n  nodes:\n    a: {kind: linux}\n")
    assert cli.main(["deploy", str(topo), "--wait", "--wait-timeout", "30"]) == 0
    assert seen["wait"] is True and seen["wait_timeout"] == 30
    outcome["ready"] = False
    assert cli.main(["deploy", str(topo), "--wait"]) == 1


def test_deploy_wait_covers_deployed_nodes_only(monkeypatch, tmp_path):
    from clabfleet import deployer
    from clabfleet.deployer import LabDeployer
    from clabfleet.placement import NodePlacement, PlacementPlan

    topo = tmp_path / "t.clab.yml"
    topo.write_text("name: t\ntopology:\n  nodes:\n    a: {kind: linux}\n    br: {kind: bridge}\n"
                    "    b: {kind: arista_ceos}\n")

    class Quiet(Runner):
        def run(self, *a, **kw):
            return CommandResult(0, "", "")

    def plan(self, t, strategy):
        p = PlacementPlan()
        p.placements += [NodePlacement("a", "h1", 1, 1), NodePlacement("br", "h1", 0, 0),
                         NodePlacement("b", "h2", 1, 1)]
        return p

    def deploy_on_host(self, host, *a):
        if host.name == "h2":
            raise RuntimeError("boom")
        return {"status": "deployed"}

    inspected = {"t": [{"Names": ["clab-t-a"], "State": "running",
                        "Labels": {"containerlab": "t", "clab-node-name": "a",
                                   "clab-node-kind": "linux"}}]}
    monkeypatch.setattr(deployer, "create_runner", lambda host: Quiet())
    monkeypatch.setattr(deployer, "scan_used_vnis", lambda r, h: {})
    monkeypatch.setattr(deployer, "inspect_all", lambda runner: inspected)
    monkeypatch.setattr(LabDeployer, "_plan", plan)
    monkeypatch.setattr(LabDeployer, "_deploy_on_host", deploy_on_host)

    cluster = ClusterConfig(hosts=[HostInfo("h1", "10.0.0.1"), HostInfo("h2", "10.0.0.2")])
    summary = LabDeployer(cluster).deploy(topo, check_images=False, check_connectivity=False,
                                          wait=True, wait_timeout=30)
    assert summary["status"] == "partial"
    assert summary["readiness"]["ready"] is True  # only "a": bridge skipped, h2 failed

    summary = LabDeployer(cluster).deploy(topo, check_images=False, check_connectivity=False,
                                          rollback=True, wait=True)
    assert "readiness" not in summary  # rolled back: nothing to wait for
