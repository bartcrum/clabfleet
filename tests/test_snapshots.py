import json
import time

import pytest
import yaml

from clabfleet import cli, deployer, snapshots
from clabfleet.cluster import ClusterConfig, HostInfo
from clabfleet.runner import CommandResult, Runner
from clabfleet.snapshots import (
    SnapshotError,
    Snapshotter,
    diff_lab,
    list_snapshots,
    read_file,
    startup_config,
    unified_diff,
)
from clabfleet.topology import load_topology

STARTUP = "hostname leaf1\n!\ninterface Ethernet1\n   no switchport\n!\n"

TOPO = {
    "name": "lab",
    "topology": {
        "kinds": {"arista_ceos": {"image": "ceos"}},
        "nodes": {
            "spine1": {"kind": "arista_ceos", "startup-config": "configs/__clabNodeName__.cfg"},
            "leaf1": {"kind": "ceos", "startup-config": STARTUP},
            "r1": {"kind": "cisco_iol", "image": "iol"},
            "srv": {"kind": "linux", "image": "alpine"},
            "br": {"kind": "bridge"},
        },
    },
}


def _eos(hostname, extra="", stamp="Fri Oct  2 00:47:01 2026"):
    return (f"! Startup-config last modified at {stamp} by root\n"
            f"! device: {hostname} (cEOSLab)\n!\nhostname {hostname}\n!\n{extra}end\n")


class FakeHost(Runner):
    """A lab host: answers `containerlab save` and `cat` of saved configs."""

    def __init__(self, files=None, root_only=(), unreachable=False):
        super().__init__()
        self.files = dict(files or {})
        self.root_only = set(root_only)
        self.unreachable = unreachable
        self.calls = []

    def run(self, args, cwd=None, check=True, sudo=None, on_output=None):
        self.calls.append((list(args), bool(sudo)))
        if self.unreachable:
            raise OSError("no route to host")
        if args[0] == "containerlab":
            if on_output:
                on_output("INFO Saved configuration")
            return CommandResult(0, "", "")
        if args[0] == "test":  # exists()
            return CommandResult(0, "", "")
        assert args[:2] == ["cat", "--"]
        path = args[2]
        if path not in self.files:
            return CommandResult(1, "", f"cat: {path}: No such file or directory\n")
        if path in self.root_only and not sudo:
            return CommandResult(1, "", f"cat: {path}: Permission denied\n")
        return CommandResult(0, self.files[path], "")


@pytest.fixture
def topo_file(tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "spine1.cfg").write_text("hostname spine1\n!\n")
    path = tmp_path / "lab.clab.yml"
    path.write_text(yaml.safe_dump(TOPO, sort_keys=False))
    return path


def _local_files(tmp_path, spine="", leaf=""):
    lab = tmp_path / "clab-lab"
    return {
        f"{lab}/spine1/flash/startup-config": _eos("spine1", spine),
        f"{lab}/leaf1/flash/startup-config": _eos("leaf1", leaf),
    }


def _patch_hosts(monkeypatch, hosts):
    factory = lambda host: hosts[host.name]  # noqa: E731
    monkeypatch.setattr(snapshots, "create_runner", factory)
    monkeypatch.setattr(deployer, "create_runner", factory)


def _local():
    return ClusterConfig(hosts=[HostInfo("localhost")])


def test_snapshot_saves_then_copies_configs(tmp_path, topo_file, monkeypatch):
    host = FakeHost(_local_files(tmp_path))
    _patch_hosts(monkeypatch, {"localhost": host})
    output = []

    out = Snapshotter(_local(), on_output=output.append).take(topo_file, name="first")

    assert host.calls[0] == (["containerlab", "save", "-t", "lab.clab.yml"], False)
    assert output == ["INFO Saved configuration"]
    assert out["hosts"]["localhost"]["status"] == "ok"
    path = tmp_path / "snapshots" / "lab" / "first"
    assert out["path"] == str(path)
    assert sorted(p.name for p in path.iterdir()) == ["leaf1.cfg", "snapshot.json", "spine1.cfg"]
    assert (path / "spine1.cfg").read_text() == _eos("spine1")
    meta = json.loads((path / "snapshot.json").read_text())
    assert meta["lab"] == "lab" and meta["saved"] is True
    assert meta["nodes"]["leaf1"] == {
        "host": "localhost", "kind": "ceos", "file": "leaf1.cfg",
        "source": f"{tmp_path}/clab-lab/leaf1/flash/startup-config",
    }
    # bridges are left out; kinds without a readable saved config are skipped
    assert meta["skipped"] == {
        "r1": "IOL saves to its binary NVRAM file",
        "srv": "kind 'linux' has no saved config",
    }


def test_snapshot_without_save_and_default_name(tmp_path, topo_file, monkeypatch):
    host = FakeHost(_local_files(tmp_path))
    _patch_hosts(monkeypatch, {"localhost": host})
    a = Snapshotter(_local()).take(topo_file, save=False, nodes=["spine*"],
                                   directory=tmp_path / "elsewhere")
    b = Snapshotter(_local()).take(topo_file, save=False, nodes=["spine*"],
                                   directory=tmp_path / "elsewhere")
    assert all(args[0] == "cat" for args, _ in host.calls)
    assert a["hosts"] == {}
    assert list(a["nodes"]) == ["spine1"] and a["skipped"] == {}
    assert a["path"].startswith(str(tmp_path / "elsewhere" / "lab"))
    assert len(a["snapshot"]) == len("20261001T194212Z") and a["snapshot"].endswith("Z")
    # two snapshots in the same second do not collide
    assert b["snapshot"] != a["snapshot"]


def test_snapshot_reads_remote_hosts_from_the_placement_record(tmp_path, topo_file, monkeypatch):
    cluster = ClusterConfig(hosts=[HostInfo("h1", "10.0.0.1"), HostInfo("h2", "10.0.0.2", sudo=True)])
    remote = "clabfleet/lab/clab-lab"
    h1 = FakeHost({f"{remote}/spine1/flash/startup-config": _eos("spine1")})
    h2 = FakeHost({f"{remote}/leaf1/flash/startup-config": _eos("leaf1")},
                  root_only={f"{remote}/leaf1/flash/startup-config"})
    _patch_hosts(monkeypatch, {"h1": h1, "h2": h2})
    (topo_file.parent / "lab.placement.json").write_text(json.dumps({
        "lab": "lab", "hosts": {"h1": {}, "h2": {}},
        "nodes": {"spine1": "h1", "leaf1": "h2", "r1": "h1", "srv": "h2"},
    }))

    out = Snapshotter(cluster).take(topo_file, save=False)

    assert out["nodes"]["spine1"]["host"] == "h1"
    assert out["nodes"]["leaf1"]["host"] == "h2"
    # each node is only looked for on its own host; h2 needed sudo to read it
    assert [c for c in h1.calls if c[0][0] == "cat"] == [
        (["cat", "--", f"{remote}/spine1/flash/startup-config"], False)]
    assert [c[1] for c in h2.calls] == [False, True]


def test_snapshot_without_record_searches_hosts(tmp_path, topo_file, monkeypatch):
    cluster = ClusterConfig(hosts=[HostInfo("h1", "10.0.0.1"), HostInfo("h2", "10.0.0.2")])
    remote = "clabfleet/lab/clab-lab"
    h1 = FakeHost(unreachable=True)
    h2 = FakeHost({f"{remote}/leaf1/flash/startup-config": _eos("leaf1")})
    _patch_hosts(monkeypatch, {"h1": h1, "h2": h2})

    out = Snapshotter(cluster).take(topo_file, save=False)

    assert list(out["nodes"]) == ["leaf1"]
    assert out["nodes"]["leaf1"]["host"] == "h2"
    reason = out["skipped"]["spine1"]
    assert "h1: unreachable: no route to host" in reason
    assert "h2: no saved config" in reason


def test_snapshot_with_nothing_captured_fails(tmp_path, topo_file, monkeypatch):
    _patch_hosts(monkeypatch, {"localhost": FakeHost()})
    with pytest.raises(SnapshotError, match="No configs captured.*spine1: no saved config"):
        Snapshotter(_local()).take(topo_file, save=False)
    assert not (tmp_path / "snapshots").exists()


def test_snapshot_names(tmp_path, topo_file, monkeypatch):
    _patch_hosts(monkeypatch, {"localhost": FakeHost(_local_files(tmp_path))})
    for bad in ("latest", "startup", "../x", ".hidden", "a b"):
        with pytest.raises(SnapshotError, match="Invalid snapshot name"):
            Snapshotter(_local()).take(topo_file, save=False, name=bad)
    Snapshotter(_local()).take(topo_file, save=False, name="before")
    with pytest.raises(SnapshotError, match="already exists"):
        Snapshotter(_local()).take(topo_file, save=False, name="before")


def test_read_file_only_retries_with_sudo_on_permission_denied():
    host = FakeHost({"/a": "x"}, root_only={"/a"})
    assert read_file(host, "/a", host_sudo=False) == (None, "cat: /a: Permission denied")
    assert read_file(host, "/a", host_sudo=True) == ("x", "")
    host.calls.clear()
    assert read_file(host, "/b", host_sudo=True)[0] is None
    assert len(host.calls) == 1


# ----------------------------------------------------------------------
# Listing and diffs
# ----------------------------------------------------------------------

def _take(topo_file, monkeypatch, name, spine="", leaf="", stamp="x", nodes=None):
    files = {
        f"{topo_file.parent}/clab-lab/spine1/flash/startup-config": _eos("spine1", spine, stamp),
        f"{topo_file.parent}/clab-lab/leaf1/flash/startup-config": _eos("leaf1", leaf, stamp),
    }
    _patch_hosts(monkeypatch, {"localhost": FakeHost(files)})
    out = Snapshotter(_local()).take(topo_file, save=False, name=name, nodes=nodes)
    time.sleep(0.001)
    return out


def _backdate(topo_file, name, taken_at):
    meta_path = topo_file.parent / "snapshots" / "lab" / name / "snapshot.json"
    meta = json.loads(meta_path.read_text())
    meta["taken_at"] = taken_at
    meta_path.write_text(json.dumps(meta))


def test_list_snapshots_oldest_first(topo_file, monkeypatch):
    topo = load_topology(topo_file)
    assert list_snapshots(topo) == []
    _take(topo_file, monkeypatch, "b")
    _take(topo_file, monkeypatch, "a")
    _backdate(topo_file, "b", "2026-01-01T00:00:00+00:00")
    _backdate(topo_file, "a", "2026-01-02T00:00:00+00:00")
    (topo_file.parent / "snapshots" / "lab" / "junk").mkdir()
    assert [s["name"] for s in list_snapshots(topo)] == ["b", "a"]


def test_diff_ignores_volatile_lines():
    old, new = _eos("s", stamp="Mon"), _eos("s", stamp="Tue")
    assert unified_diff(old, new, "a", "b", "arista_ceos") == ""
    assert unified_diff(old, new, "a", "b", "ceos") == ""
    assert "-! Startup-config last modified at Mon" in unified_diff(old, new, "a", "b", "linux")


def test_diff_against_previous_and_named(topo_file, monkeypatch):
    with pytest.raises(SnapshotError, match="no snapshots yet"):
        diff_lab(topo_file)
    _take(topo_file, monkeypatch, "one", stamp="Mon")
    with pytest.raises(SnapshotError, match="nothing before it"):
        diff_lab(topo_file)
    _take(topo_file, monkeypatch, "two", stamp="Tue")
    _backdate(topo_file, "one", "2026-01-01T00:00:00+00:00")
    _backdate(topo_file, "two", "2026-01-02T00:00:00+00:00")

    out = diff_lab(topo_file)
    assert (out["from"], out["against"], out["changed"]) == ("two", "one", 0)
    assert [(r["node"], r["status"]) for r in out["nodes"]] == [("spine1", "same"), ("leaf1", "same")]

    _take(topo_file, monkeypatch, "three", spine="interface Loopback0\n!\n", stamp="Wed")
    _backdate(topo_file, "three", "2026-01-03T00:00:00+00:00")
    out = diff_lab(topo_file, nodes=["spine*"])
    assert out["changed"] == 1
    assert out["nodes"][0]["diff"] == (
        "--- two/spine1\n+++ three/spine1\n@@ -2,4 +2,6 @@\n"
        " !\n hostname spine1\n !\n+interface Loopback0\n+!\n end\n"
    )
    # --from an older one against a later one or "latest"
    out = diff_lab(topo_file, from_snapshot="one", against="latest")
    assert (out["from"], out["against"], out["changed"]) == ("one", "three", 1)
    with pytest.raises(SnapshotError, match="no snapshot 'nope'"):
        diff_lab(topo_file, against="nope")
    with pytest.raises(ValueError, match="No node of lab 'lab' matches"):
        diff_lab(topo_file, nodes=["nope*"])


def test_diff_added_and_removed_nodes(topo_file, monkeypatch):
    _take(topo_file, monkeypatch, "one", nodes=["spine1"])
    _take(topo_file, monkeypatch, "two", nodes=["leaf1"])
    _backdate(topo_file, "one", "2026-01-01T00:00:00+00:00")
    _backdate(topo_file, "two", "2026-01-02T00:00:00+00:00")
    out = diff_lab(topo_file)
    status = {r["node"]: r["status"] for r in out["nodes"]}
    assert status == {"spine1": "removed", "leaf1": "added"}
    assert out["changed"] == 2
    leaf = next(r for r in out["nodes"] if r["node"] == "leaf1")
    assert leaf["diff"].startswith("--- /dev/null\n+++ two/leaf1\n")


def test_diff_against_startup(topo_file, monkeypatch):
    _take(topo_file, monkeypatch, "one")
    out = diff_lab(topo_file, against="startup")
    assert out["against"] == "startup"
    by_node = {r["node"]: r for r in out["nodes"]}
    # file startup-config (with __clabNodeName__) and inline startup-config
    assert by_node["spine1"]["status"] == "changed"
    assert "--- startup/spine1\n+++ one/spine1\n" in by_node["spine1"]["diff"]
    assert "+! device: spine1 (cEOSLab)" in by_node["spine1"]["diff"]
    assert "-   no switchport" in by_node["leaf1"]["diff"]
    assert "! Startup-config last modified" not in by_node["leaf1"]["diff"]

    # nodes the snapshot skipped or with no startup-config are reported, not diffed
    out = diff_lab(topo_file, against="startup", nodes=["r1", "srv"])
    assert [(r["node"], r["status"]) for r in out["nodes"]] == [("r1", "skipped"), ("srv", "skipped")]
    assert out["nodes"][0]["reason"] == "no startup-config in the topology"
    assert out["changed"] == 0


def test_startup_config_sources(tmp_path, topo_file):
    topo = load_topology(topo_file)
    assert startup_config(topo, "leaf1") == (STARTUP, "")
    assert startup_config(topo, "spine1") == ("hostname spine1\n!\n", "")
    topo.nodes["spine1"]["startup-config"] = "https://example.com/x.cfg"
    assert "is a URL" in startup_config(topo, "spine1")[1]
    topo.nodes["spine1"]["startup-config"] = "__clabDir__/x.cfg"
    assert "containerlab path" in startup_config(topo, "spine1")[1]
    topo.nodes["spine1"]["startup-config"] = "missing.cfg"
    assert "cannot read startup-config" in startup_config(topo, "spine1")[1]
    topo.nodes["spine1"]["startup-config"] = str(tmp_path / "configs" / "spine1.cfg")
    assert startup_config(topo, "spine1")[0] == "hostname spine1\n!\n"


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def test_cli_snapshot_list_and_diff_exit_codes(tmp_path, topo_file, monkeypatch, capsys):
    files = _local_files(tmp_path)
    _patch_hosts(monkeypatch, {"localhost": FakeHost(files)})
    assert cli.main(["diff", str(topo_file)]) == 2
    assert "no snapshots yet" in capsys.readouterr().err

    assert cli.main(["snapshot", str(topo_file), "--name", "one"]) == 0
    out = capsys.readouterr().out
    assert "Snapshot one of lab 'lab'" in out
    assert "spine1               spine1.cfg" in out
    assert "srv                  skipped: kind 'linux' has no saved config" in out

    files[f"{tmp_path}/clab-lab/leaf1/flash/startup-config"] = _eos("leaf1", "ip routing\n!\n")
    _patch_hosts(monkeypatch, {"localhost": FakeHost(files)})
    assert cli.main(["snapshot", str(topo_file), "--no-save", "--name", "two"]) == 0
    _backdate(topo_file, "one", "2026-01-01T00:00:00+00:00")
    _backdate(topo_file, "two", "2026-01-02T00:00:00+00:00")
    capsys.readouterr()

    assert cli.main(["snapshot", str(topo_file), "--list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines] == ["one", "two"]
    assert "2 nodes, 2 skipped" in lines[0]

    assert cli.main(["diff", str(topo_file)]) == 1
    captured = capsys.readouterr()
    assert "+ip routing" in captured.out
    assert "two vs one: 1 of 2 nodes differ" in captured.err
    assert cli.main(["diff", str(topo_file), "--nodes", "spine1"]) == 0
    assert cli.main(["diff", str(topo_file), "--against", "nope"]) == 2
    capsys.readouterr()

    assert cli.main(["diff", str(topo_file), "--against", "startup", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["against"] == "startup"
    assert {r["node"]: r["status"] for r in data["nodes"]} == {
        "spine1": "changed", "leaf1": "changed"}


def test_cli_snapshot_fails_when_save_fails(tmp_path, topo_file, monkeypatch, capsys):
    class SaveFails(FakeHost):
        def run(self, args, cwd=None, check=True, sudo=None, on_output=None):
            if args[0] == "containerlab":
                return CommandResult(1, "", "boom")
            return super().run(args, cwd, check, sudo, on_output)

    _patch_hosts(monkeypatch, {"localhost": SaveFails(_local_files(tmp_path))})
    assert cli.main(["snapshot", str(topo_file), "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["hosts"]["localhost"]["error"] == "boom"
    assert set(out["nodes"]) == {"spine1", "leaf1"}  # captured anyway


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------

def test_gui_snapshot_job_and_node_diff(tmp_path, topo_file, monkeypatch):
    pytest.importorskip("aiohttp")
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from clabfleet.gui import server
    from clabfleet.gui.state import JobManager, Workspace

    _patch_hosts(monkeypatch, {"localhost": FakeHost(_local_files(tmp_path))})
    ws = Workspace(_local(), [tmp_path])
    jobs = JobManager(ws)
    job = jobs.start("snapshot", "lab.clab.yml")
    for _ in range(200):
        if job.status != "running":
            break
        time.sleep(0.02)
    assert job.status == "ok", job.lines
    assert "INFO Saved configuration" in job.lines
    assert any(line.startswith("» snapshot ") for line in job.lines)
    assert any("Captured spine1 from localhost" in line for line in job.lines)

    result = ws.node_diff("lab.clab.yml", "leaf1", "startup")
    assert result["status"] == "changed" and result["against"] == "startup"
    with pytest.raises(ValueError):
        ws.node_diff("lab.clab.yml", "leaf1", "latest")

    async def scenario():
        app = server.create_app(ws, "tok")
        async with TestClient(TestServer(app)) as client:
            await client.get("/?token=tok", allow_redirects=False)
            resp = await client.get("/api/diff/lab.clab.yml", params={"node": "spine1"})
            assert resp.status == 200
            assert (await resp.json())["node"] == "spine1"
            resp = await client.get("/api/diff/lab.clab.yml",
                                    params={"node": "spine1", "against": "previous"})
            assert resp.status == 400
            assert "nothing before it" in await resp.text()
            resp = await client.get("/api/diff/nope.clab.yml", params={"node": "x"})
            assert resp.status == 404

    asyncio.run(scenario())
