import asyncio
import difflib
import shutil
from pathlib import Path

import pytest
import yaml

pytest.importorskip("aiohttp")
pytest.importorskip("ruamel.yaml")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from clabfleet.cluster import ClusterConfig, HostInfo  # noqa: E402
from clabfleet.gui import server, state  # noqa: E402
from clabfleet.gui.editing import (  # noqa: E402
    EditConflict,
    set_positions,
    text_hash,
    write_if_unchanged,
)
from clabfleet.gui.state import UnloadableTopology, Workspace  # noqa: E402

TOPOLOGIES = Path(__file__).parent.parent / "topologies"


def test_set_positions_only_adds_labels_and_keeps_comments():
    text = (TOPOLOGIES / "spine_leaf.clab.yml").read_text()
    out = set_positions(text, {"Spine-1": (100.4, -20), "Leaf-1": (0, 80.6), "ghost": (1, 1)})
    added = [line for line in difflib.ndiff(text.splitlines(), out.splitlines())
             if line.startswith(("+ ", "- "))]
    assert all(line.startswith("+ ") for line in added)  # nothing removed or reworded
    assert len(added) == 6  # labels: + two positions, for two nodes
    data = yaml.safe_load(out)
    assert data["topology"]["nodes"]["Spine-1"]["labels"] == {"graph-posX": "100", "graph-posY": "-20"}
    assert data["topology"]["nodes"]["Leaf-1"]["labels"]["graph-posY"] == "81"
    # Saving the same positions again changes nothing
    assert set_positions(out, {"Spine-1": (100, -20), "Leaf-1": (0, 81)}) == out


def test_set_positions_handles_flow_and_empty_nodes():
    text = ("# lab\nname: t\ntopology:\n  nodes:\n    a: {kind: linux, image: alpine}\n"
            "    b:\n    c:\n      kind: linux\n      labels:\n        role: x  # keep\n")
    out = set_positions(text, {"a": (1, 2), "b": (3, 4), "c": (5, 6)})
    nodes = yaml.safe_load(out)["topology"]["nodes"]
    assert nodes["a"]["labels"] == {"graph-posX": "1", "graph-posY": "2"}
    assert nodes["b"] == {"labels": {"graph-posX": "3", "graph-posY": "4"}}
    assert nodes["c"]["labels"] == {"role": "x", "graph-posX": "5", "graph-posY": "6"}
    assert out.startswith("# lab\n") and "role: x  # keep" in out


def test_write_if_unchanged(tmp_path):
    path = tmp_path / "t.clab.yml"
    path.write_text("name: t\n")
    path.chmod(0o640)
    write_if_unchanged(path, "name: u\n", text_hash("name: t\n"))
    assert path.read_text() == "name: u\n"
    assert path.stat().st_mode & 0o777 == 0o640
    with pytest.raises(EditConflict, match="changed on disk"):
        write_if_unchanged(path, "name: v\n", text_hash("name: t\n"))
    assert path.read_text() == "name: u\n"
    assert [p.name for p in tmp_path.iterdir()] == ["t.clab.yml"]  # no temp files left


TOPO = "name: t\ntopology:\n  nodes:\n    a: {kind: linux, image: alpine}  # web\n"


def _ws(tmp_path):
    (tmp_path / "t.clab.yml").write_text(TOPO)
    ws = Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])
    ws.topologies()
    return ws


def test_workspace_save_and_validate(tmp_path):
    ws = _ws(tmp_path)
    detail = ws.topology_detail("t.clab.yml")
    assert detail["hash"] == text_hash(TOPO)

    report = ws.validate_yaml("t.clab.yml", "name: t\ntopology:\n  nodes:\n    a: {kind: linux\n")
    assert not report["loadable"] and report["errors"][0].startswith("YAML syntax error at line")

    with pytest.raises(UnloadableTopology):
        ws.save_yaml("t.clab.yml", "name: t\n", detail["hash"])
    assert (tmp_path / "t.clab.yml").read_text() == TOPO

    new = TOPO + "    b: {kind: linux, startup-config: missing.cfg}\n"
    result = ws.save_yaml("t.clab.yml", new, detail["hash"])  # errors do not block saving
    assert (tmp_path / "t.clab.yml").read_text() == new
    assert result["validation"]["errors"] == ["node 'b' startup-config 'missing.cfg' does not exist"]
    assert [n["name"] for n in result["detail"]["nodes"]] == ["a", "b"]

    with pytest.raises(EditConflict):
        ws.save_yaml("t.clab.yml", TOPO, detail["hash"])  # stale hash

    out = ws.save_positions("t.clab.yml", {"a": [10, 20]}, result["detail"]["hash"])
    assert out["nodes"][0]["pos"] == [10.0, 20.0]
    assert "# web" in (tmp_path / "t.clab.yml").read_text()
    with pytest.raises(ValueError):
        ws.save_positions("t.clab.yml", {"a": ["x", 1]}, "")
    with pytest.raises(ValueError):
        ws.save_positions("t.clab.yml", {"a": [float("inf"), 1]}, "")


def test_edit_endpoints(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    monkeypatch.setattr(Workspace, "runtime", lambda self: [])

    async def scenario():
        app = server.create_app(ws, "tok")
        async with TestClient(TestServer(app)) as client:
            await client.get("/?token=tok", allow_redirects=False)
            detail = await (await client.get("/api/topologies/t.clab.yml")).json()

            resp = await client.post("/api/validate/t.clab.yml", json={"yaml": "name: [t\n"})
            assert (await resp.json())["loadable"] is False

            resp = await client.put("/api/topologies/t.clab.yml", json={"yaml": "nope: 1\n",
                                                                        "base_hash": detail["hash"]})
            assert resp.status == 400
            assert "name" in (await resp.json())["validation"]["errors"][0]

            new = TOPO.replace("alpine", "alpine:3")
            resp = await client.put("/api/topologies/t.clab.yml",
                                    json={"yaml": new, "base_hash": detail["hash"]})
            assert resp.status == 200
            saved = (await resp.json())["detail"]
            assert saved["nodes"][0]["image"] == "alpine:3"

            resp = await client.put("/api/topologies/t.clab.yml",
                                    json={"yaml": TOPO, "base_hash": detail["hash"]})
            assert resp.status == 409  # stale

            resp = await client.put("/api/positions/t.clab.yml",
                                    json={"positions": {"a": [5, 6]}, "base_hash": saved["hash"]})
            assert resp.status == 200
            assert (await resp.json())["nodes"][0]["pos"] == [5.0, 6.0]

            # Refused while a job runs for the lab
            app[server.JOBS].jobs["j"] = state.Job("j", "deploy", "t.clab.yml", lab="t")
            resp = await client.put("/api/topologies/t.clab.yml", json={"yaml": TOPO, "base_hash": ""})
            assert resp.status == 409 and "job is running" in await resp.text()
            resp = await client.put("/api/positions/t.clab.yml", json={"positions": {}})
            assert resp.status == 409

            assert (await client.put("/api/topologies/nope.clab.yml", json={"yaml": TOPO})).status == 404
            resp = await client.put("/api/topologies/t.clab.yml", json={"yaml": TOPO},
                                    headers={"Origin": "http://evil.example"})
            assert resp.status == 403

    asyncio.run(scenario())


def test_editing_example_copies(tmp_path):
    # Every example topology round-trips through a position save without
    # losing comments or changing other lines
    for src in TOPOLOGIES.glob("*.clab.yml"):
        path = tmp_path / src.name
        shutil.copy(src, path)
        text = path.read_text()
        first = next(iter(yaml.safe_load(text)["topology"]["nodes"]))
        out = set_positions(text, {first: (1, 2)})
        removed = [line for line in difflib.ndiff(text.splitlines(), out.splitlines())
                   if line.startswith("- ")]
        assert removed == [], src.name
