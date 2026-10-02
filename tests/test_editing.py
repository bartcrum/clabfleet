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
    apply_graph,
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
            await client.post("/login", json={"token": "tok"})
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


# --- the topology builder ---------------------------------------------------------

BUILDER_TOPO = """\
# A small lab
name: small

topology:
  kinds:
    arista_ceos:
      image: ceos:4.35.6M
  nodes:
    # the routers
    R1:
      kind: arista_ceos
      startup-config: |
        hostname R1
    R2:
      kind: arista_ceos
    H1:
      kind: linux
      image: alpine:3.20
  links:
    - endpoints: ["R1:eth1", "R2:eth1"]   # core link
    - endpoints: ["R2:eth2", "H1:eth1"]
    - endpoints: ["R1:eth9", "host:r1-tap"]
"""


def _graph(text):
    """The graph the GUI would send for ``text`` unchanged."""
    d = yaml.safe_load(text)["topology"]
    nodes = [{"name": k, "kind": v["kind"], "image": v.get("image", "")}
             for k, v in d["nodes"].items()]
    links = [{"a": e[0], "b": e[1]} for e in (l["endpoints"] for l in d["links"])
             if not any(x.startswith("host:") for x in e)]
    return {"nodes": nodes, "links": links}


def test_apply_graph_unchanged_is_a_no_op():
    assert apply_graph(BUILDER_TOPO, _graph(BUILDER_TOPO)) == BUILDER_TOPO
    real = (TOPOLOGIES / "spine_leaf.clab.yml").read_text()
    assert apply_graph(real, _graph(real)) == real


def test_apply_graph_adds_removes_and_renames():
    g = _graph(BUILDER_TOPO)
    # Add a router linked to R1, drop H1, rename R2 to Core-2, move R1
    g["nodes"] = [n for n in g["nodes"] if n["name"] != "H1"]
    g["nodes"][1].update(name="Core-2", rename_from="R2")
    g["nodes"][0]["pos"] = [100.4, -20]
    g["nodes"].append({"name": "R3", "kind": "cisco_iol", "image": "vrnetlab/cisco_iol:17.12.01",
                       "pos": [0, 160]})
    g["links"] = [{"a": "R1:eth1", "b": "Core-2:eth1"}, {"a": "R1:eth2", "b": "R3:Ethernet0/1"}]
    out = apply_graph(BUILDER_TOPO, g)
    d = yaml.safe_load(out)["topology"]
    assert list(d["nodes"]) == ["R1", "Core-2", "R3"]  # renamed in place
    assert d["nodes"]["R1"]["startup-config"] == "hostname R1\n"  # configs kept
    assert d["nodes"]["R1"]["labels"] == {"graph-posX": "100", "graph-posY": "-20"}
    assert d["nodes"]["R3"] == {"kind": "cisco_iol", "image": "vrnetlab/cisco_iol:17.12.01",
                                "labels": {"graph-posX": "0", "graph-posY": "160"}}
    assert [l["endpoints"] for l in d["links"]] == [
        ["R1:eth1", "Core-2:eth1"],      # renamed end
        ["R1:eth9", "host:r1-tap"],      # not a node-to-node link: kept
        ["R1:eth2", "R3:Ethernet0/1"],   # added
    ]
    # Comments stay, the new link is written like the others
    assert "# the routers" in out and "# core link" in out and "# A small lab" in out
    assert '- endpoints: ["R1:eth2", "R3:Ethernet0/1"]' in out


def test_apply_graph_removing_a_node_removes_its_links_of_every_form():
    g = _graph(BUILDER_TOPO)
    g["nodes"] = [n for n in g["nodes"] if n["name"] != "R1"]
    g["links"] = [l for l in g["links"] if not l["a"].startswith("R1:")]
    d = yaml.safe_load(apply_graph(BUILDER_TOPO, g))["topology"]
    assert list(d["nodes"]) == ["R2", "H1"]
    assert [l["endpoints"] for l in d["links"]] == [["R2:eth2", "H1:eth1"]]


def test_apply_graph_sets_generated_configs_and_images():
    g = _graph(BUILDER_TOPO)
    g["nodes"][1]["config"] = {"startup-config": "hostname R2\n!\nip routing\n"}
    g["nodes"][2]["config"] = {"exec": ["ip addr add 10.0.0.1/31 dev eth1"]}
    g["nodes"][1]["image"] = "ceos:4.36.0F"
    out = apply_graph(BUILDER_TOPO, g)
    d = yaml.safe_load(out)["topology"]["nodes"]
    assert d["R2"]["startup-config"] == "hostname R2\n!\nip routing\n"
    assert "startup-config: |" in out.split("R2:")[1]  # a readable block, not a quoted line
    assert d["R2"]["image"] == "ceos:4.36.0F"
    assert d["H1"]["exec"] == ["ip addr add 10.0.0.1/31 dev eth1"]
    # Back to the kind's default image: the node's own image line goes
    g["nodes"][1]["image"] = "ceos:4.35.6M"
    assert "image" not in yaml.safe_load(apply_graph(out, g))["topology"]["nodes"]["R2"]


@pytest.mark.parametrize("graph, error", [
    ({"nodes": [{"name": "R1", "kind": "x"}, {"name": "R1", "kind": "x"}]}, "unique"),
    ({"nodes": [{"name": "R1", "kind": "arista_ceos"}], "links": [{"a": "R1:eth1", "b": "Nope:eth1"}]},
     "not node:interface"),
    ({"nodes": [{"name": "R1", "kind": "arista_ceos"}, {"name": "R2", "kind": "arista_ceos"},
                {"name": "H1", "kind": "linux"}],
      "links": [{"a": "R1:eth1", "b": "R2"}]}, "not node:interface"),
])
def test_apply_graph_refuses_bad_graphs(graph, error):
    with pytest.raises(ValueError, match=error):
        apply_graph(BUILDER_TOPO, graph)


def test_builder_endpoints(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    monkeypatch.setattr(Workspace, "runtime", lambda self: [])

    async def scenario():
        app = server.create_app(ws, "tok")
        async with TestClient(TestServer(app)) as client:
            await client.post("/login", json={"token": "tok"})
            info = await (await client.get("/api/builder")).json()
            assert info["kinds"]["cisco_iol"]["ports"][:2] == ["Ethernet0/1", "Ethernet0/2"]
            assert info["kinds"]["arista_ceos"]["image"] and "spine-leaf" in info["templates"]

            detail = await (await client.get("/api/topologies/t.clab.yml")).json()
            graph = {"nodes": [{"name": "a", "kind": "linux"}, {"name": "b", "kind": "linux"}],
                     "links": [{"a": "a:eth1", "b": "b:eth1"}]}
            # A dry run shows the YAML and writes nothing
            resp = await client.put("/api/graph/t.clab.yml", json={
                "graph": graph, "base_hash": detail["hash"], "dry_run": True})
            preview = await resp.json()
            assert resp.status == 200 and preview["validation"]["loadable"]
            assert '"a:eth1", "b:eth1"' in preview["yaml"] and "# web" in preview["yaml"]
            assert (tmp_path / "t.clab.yml").read_text() == TOPO
            # Saving writes it, keeping the comment
            resp = await client.put("/api/graph/t.clab.yml", json={
                "graph": graph, "base_hash": detail["hash"]})
            saved = (await resp.json())["detail"]
            assert [n["name"] for n in saved["nodes"]] == ["a", "b"] and len(saved["links"]) == 1
            assert "# web" in (tmp_path / "t.clab.yml").read_text()
            # Stale, bad and unloadable drawings are refused
            resp = await client.put("/api/graph/t.clab.yml", json={"graph": graph,
                                                                 "base_hash": detail["hash"]})
            assert resp.status == 409
            bad = {"nodes": [{"name": "a", "kind": "linux"}], "links": [{"a": "a:eth1", "b": "z:eth1"}]}
            resp = await client.put("/api/graph/t.clab.yml", json={"graph": bad, "base_hash": ""})
            assert resp.status == 400
            resp = await client.put("/api/graph/t.clab.yml", json={"graph": {"nodes": []},
                                                                 "base_hash": ""})
            assert resp.status == 400 and "no nodes" in (await resp.text()).lower()

            # New labs: from a template, or one node to start from
            resp = await client.post("/api/topologies", json={
                "file": "fabric", "name": "fabric", "kind": "arista_ceos",
                "template": "spine-leaf", "params": {"spines": 1, "leaves": 2}})
            assert resp.status == 200 and (await resp.json())["id"] == "fabric.clab.yml"
            fabric = await (await client.get("/api/topologies/fabric.clab.yml")).json()
            assert len(fabric["nodes"]) == 3 and len(fabric["links"]) == 2
            resp = await client.post("/api/topologies", json={
                "file": "mine.clab.yml", "name": "mine", "kind": "linux"})
            mine = await (await client.get(f"/api/topologies/{(await resp.json())['id']}")).json()
            assert [n["name"] for n in mine["nodes"]] == ["Host-1"]
            for body, status in [({"file": "fabric", "name": "x", "kind": "linux"}, 409),
                                 ({"file": "../up", "name": "x", "kind": "linux"}, 400),
                                 ({"file": "ok", "name": "bad name!", "kind": "linux"}, 400),
                                 ({"file": "ok", "name": "x", "kind": "junos"}, 400),
                                 ({"file": "ok", "name": "x", "kind": "linux",
                                   "template": "nope"}, 400)]:
                assert (await client.post("/api/topologies", json=body)).status == status, body
            assert not (tmp_path / "ok.clab.yml").exists()

    asyncio.run(scenario())


def test_apply_graph_renames_the_hostname_and_fills_missing_images():
    g = _graph(BUILDER_TOPO)
    g["nodes"][0].update(name="Core-1", rename_from="R1")
    g["links"] = [{"a": "Core-1:eth1", "b": "R2:eth1"}, {"a": "R2:eth2", "b": "H1:eth1"}]
    g["nodes"].append({"name": "H2", "kind": "linux"})
    out = apply_graph(BUILDER_TOPO, g, {"linux": "alpine:3.20", "arista_ceos": "ceos:x"})
    nodes = yaml.safe_load(out)["topology"]["nodes"]
    assert nodes["Core-1"]["startup-config"] == "hostname Core-1\n"
    assert "startup-config: |" in out
    assert nodes["H2"]["image"] == "alpine:3.20"   # linux has no image under kinds here
    assert "image" not in nodes["R2"]              # arista_ceos has one under kinds
