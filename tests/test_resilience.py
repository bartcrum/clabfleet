"""A slow or dead host must not hang the GUI: polls with a time limit and
no redialling, planning that gives up, and jobs that can be cancelled."""

import asyncio
import json
import threading
import time

import pytest

from clabfleet import deployer as deployer_mod
from clabfleet.cluster import ClusterConfig, HostInfo, RunnerPool
from clabfleet.deployer import DeploymentError, LabDeployer
from clabfleet.runner import CommandCancelled, LocalRunner

pytest.importorskip("aiohttp")

from clabfleet.gui import jobs as jobs_mod  # noqa: E402
from clabfleet.gui import state  # noqa: E402
from clabfleet.gui.jobs import JobManager  # noqa: E402
from clabfleet.gui.state import HostUnreachable, Workspace  # noqa: E402

TOPO = "name: t\ntopology:\n  nodes:\n    a: {kind: linux, image: alpine}\n"


def _local(name: str) -> LocalRunner:
    runner = LocalRunner()
    runner.name = name
    return runner


def _cluster(*names, workdir="clabfleet-test-workdir"):
    """Hosts that look remote; the tests give them local runners."""
    return ClusterConfig(hosts=[HostInfo(n, host=f"10.0.0.{i}", workdir=str(workdir))
                                for i, n in enumerate(names, 1)])


# ----------------------------------------------------------------------
# The GUI's polls
# ----------------------------------------------------------------------

@pytest.fixture
def cluster_ws(tmp_path, monkeypatch):
    """A workspace of three hosts where ``stuck`` names those that hang."""
    monkeypatch.setattr(state, "create_runner", lambda host: _local(host.name))
    monkeypatch.setattr(state, "HOST_POLL_TIMEOUT", 1)
    monkeypatch.setattr(state, "HOST_RETRY", 3600)
    stuck, asked = set(), []

    def inspect_all(runner):
        asked.append(runner.name)
        if runner.name in stuck:
            runner.run(["sleep", "30"])
        return {}

    monkeypatch.setattr(state, "inspect_all", inspect_all)
    monkeypatch.setattr(state, "probe_host_resources", lambda runner, host: (
        runner.run(["sleep", "30"] if runner.name in stuck else ["true"]) and {"cpus": 4}))
    monkeypatch.setattr(state, "containerlab_version", lambda runner: "0.79.0")
    ws = Workspace(_cluster("h1", "h2", "h3"), [tmp_path])
    ws.stuck, ws.asked = stuck, asked
    yield ws
    ws.close()


def test_a_host_that_hangs_costs_one_wait_then_nothing(cluster_ws):
    ws = cluster_ws
    ws.stuck.add("h2")
    started = time.monotonic()
    states = ws.runtime(max_age=0)
    took = time.monotonic() - started
    # The hosts were asked at once: about the one time limit, not three in a row
    assert 0.9 < took < 2.5
    assert [(s.name, s.ok) for s in states] == [("h1", True), ("h2", False), ("h3", True)]
    assert states[1].error == "no answer within 1 seconds"

    # From now on the host is not asked: its error comes back at once
    ws.asked.clear()
    started = time.monotonic()
    again = ws.runtime(max_age=0)
    assert time.monotonic() - started < 0.5
    assert again[1].error == "no answer within 1 seconds" and "h2" not in ws.asked
    assert sorted(ws.asked) == ["h1", "h3"]
    # ... to everything that wants its runner, so nothing else waits on it either
    with pytest.raises(HostUnreachable, match="no answer within 1 seconds"):
        ws.runner(ws.host("h2"))
    assert ws.runner(ws.host("h1")).name == "h1"


def test_a_host_that_comes_back_is_noticed_in_the_background(cluster_ws, monkeypatch):
    ws = cluster_ws
    ws.stuck.add("h2")
    ws.runtime(max_age=0)
    ws.stuck.clear()                      # the host is fine again
    monkeypatch.setattr(state, "HOST_RETRY", 0)
    started = time.monotonic()
    states = ws.runtime(max_age=0)        # this poll still gets the old answer, at once
    assert time.monotonic() - started < 0.5 and not states[1].ok
    deadline = time.monotonic() + 5
    while "h2" in ws._down and time.monotonic() < deadline:
        time.sleep(0.05)
    assert all(s.ok for s in ws.runtime(max_age=0))


def test_host_status_asks_hosts_at_once_too(cluster_ws):
    ws = cluster_ws
    ws.stuck.update({"h1", "h3"})
    started = time.monotonic()
    status = ws.host_status()
    assert time.monotonic() - started < 2.5   # two stuck hosts, one wait
    assert [(h["name"], h["ok"]) for h in status] == [("h1", False), ("h2", True), ("h3", False)]
    assert status[0]["error"] == "no answer within 1 seconds"
    assert status[1]["version"] == "0.79.0" and status[1]["cpus"] == 4


def test_this_machine_is_never_marked_down(tmp_path, monkeypatch):
    """A slow answer from the machine the GUI runs on (a busy Docker during
    a deploy) fails that poll and nothing else: there is no connection to
    lose, so terminals, live state and the next poll go on as usual."""
    monkeypatch.setattr(state, "HOST_POLL_TIMEOUT", 1)
    slow = [True]

    def inspect_all(runner):
        if slow[0]:
            runner.run(["sleep", "30"])
        return {}

    monkeypatch.setattr(state, "inspect_all", inspect_all)
    ws = Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])
    try:
        started = time.monotonic()
        (only,) = ws.runtime(max_age=0)
        assert time.monotonic() - started < 2.5   # the poll itself still has its limit
        assert not only.ok and only.error == "no answer within 1 seconds"
        # Not down: its runner is still handed out, and still works
        assert ws._down == {}
        assert ws.runner(ws.host("localhost")).run(["echo", "fine"]).stdout == "fine\n"
        # The next poll asks again, and gets its answer as soon as there is one
        slow[0] = False
        (only,) = ws.runtime(max_age=0)
        assert only.ok
    finally:
        ws.close()


def test_containerlab_saying_no_is_an_answer(cluster_ws, monkeypatch):
    """An error from containerlab is the host answering: it is not marked down."""
    ws = cluster_ws

    def inspect_all(runner):
        if runner.name == "h2":
            raise state.InspectError("containerlab: permission denied")
        return {}

    monkeypatch.setattr(state, "inspect_all", inspect_all)
    states = ws.runtime(max_age=0)
    assert states[1].error == "containerlab: permission denied" and "h2" not in ws._down


# ----------------------------------------------------------------------
# Planning a deploy
# ----------------------------------------------------------------------

def test_planning_gives_up_on_a_host_that_hangs_and_frees_the_lock(tmp_path, monkeypatch):
    class Hanging(LocalRunner):
        def run(self, args, **kwargs):
            return super().run(["sleep", "30"], **{k: v for k, v in kwargs.items() if k != "sudo"})

    monkeypatch.setattr(deployer_mod, "create_runner", lambda host: Hanging())
    monkeypatch.setattr(deployer_mod, "PLAN_TIMEOUT", 1)
    path = tmp_path / "t.clab.yml"
    path.write_text(TOPO)
    started = time.monotonic()
    with pytest.raises(DeploymentError, match="h1 .* unreachable: .*no answer in time"):
        # (a work directory of its own: were the plan to go on by mistake,
        # the "hosts" are this machine and would be written to)
        LabDeployer(_cluster("h1", "h2", workdir=tmp_path / "work")).deploy(path)
    assert not (tmp_path / "work").exists()
    assert time.monotonic() - started < 4
    # Another deploy can plan: the lock every deploy shares was let go
    assert deployer_mod._PLAN_LOCK.acquire(timeout=1)
    deployer_mod._PLAN_LOCK.release()


# ----------------------------------------------------------------------
# Cancelling a job
# ----------------------------------------------------------------------

class StuckDeployer:
    """A deployer whose deploy never ends by itself. Like the real one it
    catches errors per host and carries on, which a cancel must get past."""

    def __init__(self, cluster, on_output=None, interactive_sudo=True):
        self.cluster, self.on_output = cluster, on_output
        self._runners = RunnerPool(lambda host: _local(host.name), interactive_sudo)

    def deploy(self, path, reconfigure=False, rollback=False):
        self.on_output("deploying")
        for _ in range(3):  # this host, then "the next one", then "a rollback"
            for host in self.cluster.hosts:
                try:
                    self._runners.get(host).run(["sleep", "30"], on_output=self.on_output)
                except Exception:  # noqa: BLE001 - as the real deployer does per host
                    continue
        return {"hosts": {}}

    def destroy(self, path, cleanup=True):
        return {"hosts": {"localhost": {"status": "destroyed"}}}

    def abort(self):
        self._runners.abort()


def _jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_mod, "LabDeployer", StuckDeployer)
    (tmp_path / "t.clab.yml").write_text(TOPO)
    ws = Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])
    ws.topologies()
    return ws, JobManager(ws)


def _wait(job, seconds=8):
    end = time.monotonic() + seconds
    while job.status == "running" and time.monotonic() < end:
        time.sleep(0.05)
    return job.status


def test_cancelling_a_job_ends_it_and_frees_its_lab(tmp_path, monkeypatch):
    ws, jobs = _jobs(tmp_path, monkeypatch)
    job = jobs.start("deploy", "t.clab.yml", user="alice")
    time.sleep(0.5)
    with pytest.raises(RuntimeError, match="already running for lab 't'"):
        jobs.start("destroy", "t.clab.yml")

    started = time.monotonic()
    assert jobs.cancel(job.id, "bob") is job
    jobs.cancel(job.id, "carol")          # asking twice does no harm
    assert _wait(job) == "cancelled" and time.monotonic() - started < 5
    assert job.finished and sum("cancel requested by bob" in line for line in job.lines) == 1
    assert any("Nothing was undone" in line for line in job.lines) and job.lines[-1] == "✗ cancelled"
    # Its lab and its place are free again, and the history says what happened
    assert _wait(jobs.start("destroy", "t.clab.yml")) == "ok"
    saved = json.loads((tmp_path / ".clabfleet" / "jobs" / f"{job.id}.json").read_text())
    assert saved["status"] == "cancelled"

    with pytest.raises(ValueError, match="not running any more"):
        jobs.cancel(job.id)
    with pytest.raises(KeyError):
        jobs.cancel("nope")
    ws.close()


def test_a_runner_pool_that_was_aborted_refuses_new_hosts_too():
    pool = RunnerPool(lambda host: _local(host.name))
    first = pool.get(HostInfo("localhost"))
    pool.abort()
    assert first.cancelled
    with pytest.raises(CommandCancelled):
        pool.get(HostInfo("other", host="localhost")).run(["true"])
    assert not isinstance(CommandCancelled("x"), Exception)  # it gets past `except Exception`


def test_cancel_endpoint_is_for_operators_and_audited(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from clabfleet.gui import server
    from clabfleet.gui.auth import AuditLog, UserStore

    monkeypatch.setattr(jobs_mod, "LabDeployer", StuckDeployer)
    (tmp_path / "t.clab.yml").write_text(TOPO)
    ws = Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])
    users = UserStore(tmp_path / "users.yaml")
    operator, viewer = users.add("olga"), users.add("vic", "viewer")
    audit_path = tmp_path / "audit.jsonl"

    async def scenario():
        app = server.create_app(ws, users=users, audit=AuditLog(audit_path))
        async with TestClient(TestServer(app)) as client:
            await client.post("/login", json={"token": operator})
            job = await (await client.post("/api/jobs", json={
                "action": "deploy", "topology": "t.clab.yml"})).json()
            url = f"/api/jobs/{job['id']}/cancel"

            await client.post("/login", json={"token": viewer, "switch": True})
            assert (await client.post(url)).status == 403
            await client.post("/login", json={"token": operator, "switch": True})
            assert (await client.post("/api/jobs/nope/cancel")).status == 404
            assert (await client.post(url)).status == 200
            for _ in range(100):
                view = await (await client.get(f"/api/jobs/{job['id']}")).json()
                if view["status"] != "running":
                    break
                await asyncio.sleep(0.05)
            assert view["status"] == "cancelled"
            assert (await client.post(url)).status == 409   # no longer running

    asyncio.run(scenario())
    events = [json.loads(line) for line in audit_path.read_text().splitlines()]
    cancelled = [e for e in events if e["event"] == "job_cancelled"]
    assert [(e["user"], e["details"]["action"], e["details"]["lab"]) for e in cancelled] == [
        ("olga", "deploy", "t")]
    assert [e["details"]["status"] for e in events if e["event"] == "job_finished"] == ["cancelled"]


def test_a_job_cancelled_before_it_got_going_never_starts_its_command(tmp_path, monkeypatch):
    ws, jobs = _jobs(tmp_path, monkeypatch)
    gate = threading.Event()
    real_init = StuckDeployer.__init__

    def slow_init(self, *args, **kwargs):
        gate.wait(5)  # the job's thread has not made its deployer yet
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(StuckDeployer, "__init__", slow_init)
    job = jobs.start("deploy", "t.clab.yml")
    jobs.cancel(job.id, "bob")
    gate.set()
    assert _wait(job) == "cancelled"
    ws.close()
