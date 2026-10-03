import asyncio
import json

import pytest

from clabfleet.gui import annotations

pytest.importorskip("aiohttp")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from clabfleet.cluster import ClusterConfig, HostInfo  # noqa: E402
from clabfleet.gui import server  # noqa: E402
from clabfleet.gui.auth import AuditLog, UserStore  # noqa: E402
from clabfleet.gui.state import Workspace  # noqa: E402

GOOD = {"notes": [{"id": "n1", "x": 10, "y": -5.25, "text": "Spines run EOS 4.32"}],
        "boxes": [{"id": "b1", "x": 0, "y": 0, "w": 300, "h": 200, "label": "DC1", "color": 3}]}


def test_clean_and_files(tmp_path):
    topo = tmp_path / "lab.clab.yml"
    topo.write_text("name: lab\n")
    assert annotations.load(topo) == annotations.empty()
    saved = annotations.save(topo, GOOD)
    assert saved["notes"][0]["y"] == -5.2  # rounded to 0.1
    assert annotations.path_for(topo).name == "lab.clab.yml.notes.json"
    assert annotations.load(topo) == saved
    # Nothing left: the file goes
    annotations.save(topo, {"notes": [], "boxes": []})
    assert not annotations.path_for(topo).exists()
    # A broken file reads as none
    annotations.path_for(topo).write_text("{nope")
    assert annotations.load(topo) == annotations.empty()

    bad = [
        [],
        {"notes": "x"},
        {"notes": [{"id": "a", "x": "1", "y": 0}]},
        {"notes": [{"id": "a", "x": float("nan"), "y": 0}]},
        {"notes": [{"id": "a", "x": 0, "y": 0, "text": "x" * 501}]},
        {"notes": [{"id": "a", "x": 0, "y": 0}, {"id": "a", "x": 0, "y": 0}]},
        {"boxes": [{"id": "b", "x": 0, "y": 0, "w": 5, "h": 100}]},
        {"boxes": [{"id": "b", "x": 0, "y": 0, "w": 50, "h": 100, "color": 8}]},
        {"boxes": [{"id": "b", "x": 0, "y": 0, "w": 50, "h": 100, "color": True}]},
        {"notes": [{"id": str(i), "x": 0, "y": 0} for i in range(annotations.MAX_ITEMS + 1)]},
    ]
    for data in bad:
        with pytest.raises(ValueError):
            annotations.clean(data)


def test_annotations_api(tmp_path):
    (tmp_path / "t.clab.yml").write_text("name: t\ntopology:\n  nodes:\n    R1: {kind: linux, image: a}\n")
    ws = Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])
    users = UserStore(tmp_path / "users.yaml")
    op, viewer = users.add("op"), users.add("vic", "viewer")
    audit_path = tmp_path / "audit.jsonl"

    async def scenario():
        app = server.create_app(ws, users=users, audit=AuditLog(audit_path))
        async with TestClient(TestServer(app)) as client:
            await client.post("/login", json={"token": viewer})
            assert (await client.put("/api/annotations/t.clab.yml", json=GOOD)).status == 403
            await client.post("/logout")
            await client.post("/login", json={"token": op})
            resp = await client.put("/api/annotations/t.clab.yml", json=GOOD)
            assert resp.status == 200 and (await resp.json())["boxes"][0]["label"] == "DC1"
            detail = await (await client.get("/api/topologies/t.clab.yml")).json()
            assert detail["annotations"]["notes"][0]["text"] == "Spines run EOS 4.32"
            assert (await client.put("/api/annotations/t.clab.yml", json={"notes": 1})).status == 400
            assert (await client.put("/api/annotations/nope.clab.yml", json=GOOD)).status == 404

    asyncio.run(scenario())
    # The topology itself is untouched, and is still the only lab
    assert (tmp_path / "t.clab.yml").read_text().startswith("name: t\n")
    assert [t["id"] for t in ws.topologies()] == ["t.clab.yml"]
    events = [json.loads(line) for line in audit_path.read_text().splitlines() if "annotations_saved" in line]
    assert events[0]["details"] == {"topology": "t.clab.yml", "notes": 1, "boxes": 1}
