"""import-configs: a topology from config files alone (clabfleet.cfgimport).

The configs are written the way Arista AVD 5 writes them: fabric ports
described as ``<TYPE>_<peer>_<peer port>``, /31 addresses on routed links.
"""

import asyncio
import stat

import pytest
import yaml

from clabfleet import cli
from clabfleet.cfgimport import ConfigImportError, hostname_of, import_configs, infer_links
from clabfleet.exporter import ExportOptions, ResidualSecretsError, report_path
from clabfleet.sanitise import SanitiseOptions
from clabfleet.topology import topology_from_dict

SECRET = "$6$Enl0WfE32Fthwyi$PRODHASH0001"


def _config(hostname, ports, extra=""):
    """An EOS config: ``ports`` is [(name, description, address or None)]."""
    lines = [
        "! Command: show running-config",
        f"username arista privilege 15 role network-admin secret sha512 {SECRET}",
        "!",
        f"hostname {hostname}",
        "!",
        "vrf instance MGMT",
        "!",
    ]
    for name, description, address in ports:
        lines.append(f"interface {name}")
        if description:
            lines.append(f"   description {description}")
        lines.append("   no shutdown")
        if address:
            lines += ["   no switchport", f"   ip address {address}"]
        lines.append("!")
    lines += [
        "interface Management1",
        "   description OOB_MANAGEMENT",
        "   vrf MGMT",
        "   ip address 172.16.1.10/24",
        "!",
        "ip route vrf MGMT 0.0.0.0/0 172.16.1.1",
        "ntp local-interface vrf MGMT Management1",
        "!",
    ]
    return "\n".join(lines) + "\n" + extra + "end\n"


def _fabric():
    """Two spines, an MLAG pair of leaves and an L2 leaf below them."""
    return {
        "dc1-spine1.cfg": _config("dc1-spine1", [
            ("Ethernet1", "P2P_dc1-leaf1a_Ethernet1", "10.255.255.0/31"),
            ("Ethernet2", "P2P_dc1-leaf1b_Ethernet1", "10.255.255.4/31"),
        ]),
        "dc1-spine2.cfg": _config("dc1-spine2", [
            ("Ethernet1", "P2P_dc1-leaf1a_Ethernet2", "10.255.255.2/31"),
            ("Ethernet2", "P2P_dc1-leaf1b_Ethernet2", "10.255.255.6/31"),
        ]),
        "dc1-leaf1a.cfg": _config("dc1-leaf1a", [
            ("Port-Channel3", "MLAG_dc1-leaf1b_Port-Channel3", None),
            ("Ethernet1", "P2P_dc1-spine1_Ethernet1", "10.255.255.1/31"),
            ("Ethernet2", "P2P_dc1-spine2_Ethernet1", "10.255.255.3/31"),
            ("Ethernet3", "MLAG_dc1-leaf1b_Ethernet3", None),
            ("Ethernet4", "MLAG_dc1-leaf1b_Ethernet4", None),
            ("Ethernet5", "SERVER_dc1-leaf1-server1_PCI1", None),
            ("Ethernet8", "L2_dc1-leaf1c_Ethernet1", None),
            ("Vlan4094", "MLAG", "10.255.1.64/31"),
        ]),
        "dc1-leaf1b.cfg": _config("dc1-leaf1b", [
            ("Port-Channel3", "MLAG_dc1-leaf1a_Port-Channel3", None),
            ("Ethernet1", "P2P_dc1-spine1_Ethernet2", "10.255.255.5/31"),
            ("Ethernet2", "P2P_dc1-spine2_Ethernet2", "10.255.255.7/31"),
            ("Ethernet3", "MLAG_dc1-leaf1a_Ethernet3", None),
            ("Ethernet4", "MLAG_dc1-leaf1a_Ethernet4", None),
            ("Ethernet5", "SERVER_dc1-leaf1-server1_PCI2", None),
            ("Ethernet8", "L2_dc1-leaf1c_Ethernet2", None),
            ("Vlan4094", "MLAG", "10.255.1.65/31"),
        ]),
        "dc1-leaf1c.cfg": _config("dc1-leaf1c", [
            ("Port-Channel1", "L2_DC1_L3_LEAF1_Port-Channel8", None),
            ("Ethernet1", "L2_dc1-leaf1a_Ethernet8", None),
            ("Ethernet2", "L2_dc1-leaf1b_Ethernet8", None),
            ("Ethernet5", "SERVER_dc1-leaf1-server1_iLO", None),
        ]),
    }


FABRIC_LINKS = sorted(tuple(sorted(pair)) for pair in [
    ("dc1-leaf1a:eth1", "dc1-spine1:eth1"),
    ("dc1-leaf1a:eth2", "dc1-spine2:eth1"),
    ("dc1-leaf1b:eth1", "dc1-spine1:eth2"),
    ("dc1-leaf1b:eth2", "dc1-spine2:eth2"),
    ("dc1-leaf1a:eth3", "dc1-leaf1b:eth3"),
    ("dc1-leaf1a:eth4", "dc1-leaf1b:eth4"),
    ("dc1-leaf1a:eth8", "dc1-leaf1c:eth1"),
    ("dc1-leaf1b:eth8", "dc1-leaf1c:eth2"),
])


def _links(topo):
    return sorted(tuple(sorted(link["endpoints"])) for link in topo["topology"]["links"])


def _pairs(links):
    return sorted(tuple(sorted([f"{k.a}:{k.a_if}", f"{k.b}:{k.b_if}"])) for k in links)


def _sanitised(**kwargs):
    return ExportOptions(lab_name="avd", sanitise=SanitiseOptions(), **kwargs)


def test_avd_fabric_becomes_a_lab():
    result = import_configs(_fabric(), _sanitised(), image="ceos:4.35.6M")

    nodes = result.topology["topology"]["nodes"]
    assert sorted(nodes) == ["dc1-leaf1a", "dc1-leaf1b", "dc1-leaf1c", "dc1-spine1", "dc1-spine2"]
    assert nodes["dc1-leaf1a"] == {"kind": "arista_ceos", "image": "ceos:4.35.6M",
                                   "startup-config": "configs/dc1-leaf1a.cfg"}
    assert _links(result.topology) == FABRIC_LINKS
    topology_from_dict(result.topology)     # a topology containerlab would take

    # Every link came from both descriptions; routed ones name their subnet
    assert {link["from"] for link in result.report["links"]} == {"descriptions"}
    first = result.report["links"][0]
    assert first == {"endpoints": ["dc1-leaf1a:Ethernet1", "dc1-spine1:Ethernet1"],
                     "from": "descriptions", "subnet": "10.255.255.0/31"}
    # Server ports get no link and no node; only a count is reported
    assert result.warnings == []
    assert "ports_without_peer" not in result.report

    # Sanitised, and the management port is the one cEOS has
    config = result.configs["dc1-leaf1a"]
    assert SECRET not in config and "172.16.1.10" not in config
    assert "interface Management0" in config and "Management1" not in config
    assert "description P2P_dc1-spine1_Ethernet1" in config
    entry = result.report["nodes"]["dc1-leaf1a"]
    assert entry["management_interface"] == "Management1 renamed Management0"
    assert entry["file"] == "dc1-leaf1a.cfg" and entry["image_from"] == "given"
    assert "source" not in entry
    assert result.report["source"] == "config files" and result.report["sanitised"] is True


def test_management_name_and_secrets_can_be_kept():
    result = import_configs(_fabric(), ExportOptions(lab_name="avd"), image="ceos:x",
                            rename_management=False)
    config = result.configs["dc1-spine1"]
    assert "interface Management1" in config and SECRET in config
    assert "management_interface" not in result.report["nodes"]["dc1-spine1"]


def test_older_avd_descriptions_and_other_separators():
    links, unmatched, warnings = infer_links({
        "DC1-SPINE1": _config("DC1-SPINE1", [
            ("Ethernet1", "P2P_LINK_TO_DC1-LEAF1A_Ethernet1", "172.31.255.0/31"),
            ("Ethernet2", "to dc1-leaf2a Et49/1", None),
        ]),
        "DC1-LEAF1A": _config("DC1-LEAF1A", [
            ("Ethernet1", "P2P_LINK_TO_DC1-SPINE1_Ethernet1", "172.31.255.1/31"),
        ]),
        "dc1-leaf2a": _config("dc1-leaf2a", [
            ("Ethernet49/1", "uplink: DC1-SPINE1:Ethernet2", None),
        ]),
    })
    assert _pairs(links) == [("DC1-LEAF1A:Ethernet1", "DC1-SPINE1:Ethernet1"),
                             ("DC1-SPINE1:Ethernet2", "dc1-leaf2a:Ethernet49/1")]
    assert not unmatched and not warnings


def test_a_hostname_inside_a_longer_one_is_not_a_match():
    links, unmatched, _ = infer_links({
        "leaf1": _config("leaf1", [("Ethernet1", "P2P_spine1_Ethernet1", None)]),
        "leaf10": _config("leaf10", [("Ethernet1", "P2P_spine1_Ethernet10", None)]),
        "spine1": _config("spine1", [
            ("Ethernet1", "P2P_leaf1_Ethernet1", None),
            ("Ethernet10", "P2P_leaf10_Ethernet1", None),
            ("Ethernet11", "P2P_dc2-leaf1_Ethernet1", None),     # another site's leaf1
        ]),
    })
    assert _pairs(links) == [("leaf10:Ethernet1", "spine1:Ethernet10"),
                             ("leaf1:Ethernet1", "spine1:Ethernet1")]
    assert unmatched == [{"port": "spine1:Ethernet11", "description": "P2P_dc2-leaf1_Ethernet1"}]


def test_links_from_addresses_when_descriptions_say_nothing():
    links, _, warnings = infer_links({
        "r1": _config("r1", [("Ethernet1", "", "10.0.0.0/31"), ("Ethernet2", "core", "10.0.0.5/30"),
                             ("Ethernet3", "lan", "192.168.1.1/24")]),
        "r2": _config("r2", [("Ethernet7", "", "10.0.0.1/31"), ("Ethernet3", "lan", "192.168.1.2/24")]),
        "r3": _config("r3", [("Ethernet1", "", "10.0.0.6/30")]),
    })
    assert [(k.source, k.subnet) for k in links] == [("addresses", "10.0.0.0/31"),
                                                     ("addresses", "10.0.0.4/30")]
    assert _pairs(links) == [("r1:Ethernet1", "r2:Ethernet7"), ("r1:Ethernet2", "r3:Ethernet1")]
    assert warnings == []       # a shared /24 is a LAN, not a link


def test_disagreements_are_reported():
    links, unmatched, warnings = infer_links({
        "a": _config("a", [
            ("Ethernet1", "P2P_b_Ethernet1", "10.0.0.0/31"),     # addresses disagree
            ("Ethernet2", "P2P_b_Ethernet2", None),              # b's end is not described
            ("Ethernet3", "P2P_b_Ethernet3", None),              # b's end faces c
            ("Ethernet4", "P2P_b_Ethernet9", None),              # b has no such port
            ("Ethernet5", "P2P_missing-switch_Ethernet1", None),
        ]),
        "b": _config("b", [
            ("Ethernet1", "P2P_a_Ethernet1", "10.0.9.1/31"),
            ("Ethernet2", "", None),
            ("Ethernet3", "P2P_c_Ethernet1", None),
        ]),
        "c": _config("c", [("Ethernet1", "P2P_b_Ethernet3", None)]),
    })
    assert _pairs(links) == [("a:Ethernet1", "b:Ethernet1"), ("a:Ethernet2", "b:Ethernet2"),
                             ("a:Ethernet4", "b:Ethernet9"), ("b:Ethernet3", "c:Ethernet1")]
    assert {k.source for k in links if "Ethernet2" in k.a_if or "Ethernet4" in k.a_if} == {"description"}
    text = "\n".join(warnings)
    assert "a:Ethernet1 -- b:Ethernet1 is in the descriptions, but the addresses" in text
    assert "a:Ethernet2 -- b:Ethernet2 comes from the description of a:Ethernet2 only" in text
    assert "a:Ethernet3 -- b:Ethernet3 left out: b:Ethernet3 is described as facing c:Ethernet1" in text
    assert "b has no Ethernet9 in its config" in text
    assert "1 port(s) face a device whose config was not given" in text
    assert unmatched == [{"port": "a:Ethernet5", "description": "P2P_missing-switch_Ethernet1"}]


def test_duplicate_subnet_makes_no_link():
    links, _, warnings = infer_links({
        name: _config(name, [("Ethernet1", "", f"10.0.0.{i}/30")])
        for i, name in enumerate(("a", "b", "c"), start=1)})
    assert links == []
    assert any("10.0.0.0/30 is on" in w for w in warnings)


def test_hostname_comes_from_the_config_then_the_file_name():
    assert hostname_of("x/y/whatever.cfg", "!\nhostname dc1-leaf1a\n") == "dc1-leaf1a"
    assert hostname_of("x/y/dc1-leaf9.CFG", "interface Ethernet1\n") == "dc1-leaf9"
    assert hostname_of("C:\\configs\\sw1.txt", "interface Ethernet1\n") == "sw1"


@pytest.mark.parametrize("files, message", [
    ({}, "no config files"),
    ({"a.cfg": "hostname a\n", "copy/a.cfg": "hostname a\n"}, "both the config of 'a'"),
    ({"notes.txt": "remember the milk\n"}, "does not look like a device config"),
    ({"blob.cfg": "hostname a\x00\n"}, "not a text file"),
])
def test_bad_uploads_are_refused(files, message):
    with pytest.raises(ConfigImportError, match=message):
        import_configs(files)


def test_no_links_at_all_is_a_warning():
    result = import_configs({"a.cfg": _config("a", [("Ethernet1", "", None)]),
                             "b.cfg": _config("b", [("Ethernet1", "", None)])},
                            ExportOptions(lab_name="x"), image="ceos:x")
    assert result.topology["topology"]["links"] == []
    assert any("no links found" in w for w in result.warnings)


def test_residual_secrets_stop_the_import():
    files = _fabric()
    files["dc1-spine1.cfg"] = files["dc1-spine1.cfg"].replace(
        "end\n", "some new-feature shared-secret 7 0123456789ABCDEF0123\nend\n")
    try:
        import_configs(files, _sanitised(), image="ceos:x")
    except ResidualSecretsError as exc:
        assert list(exc.findings) == ["dc1-spine1"]
    else:   # the sanitiser handled the line itself: nothing left to refuse
        return
    result = import_configs(files, _sanitised(allow_residual=True), image="ceos:x")
    assert any("may still hold secrets" in w for w in result.warnings)


# --- CLI ----------------------------------------------------------------------

def _write(tmp_path, files):
    src = tmp_path / "intended" / "configs"
    src.mkdir(parents=True)
    for name, text in files.items():
        (src / name).write_text(text)
    (src / "README.md").write_text("not a config\n")
    return src


def test_cli_end_to_end(tmp_path, capsys):
    src = _write(tmp_path, _fabric())
    out = tmp_path / "lab" / "dc1.clab.yml"

    assert cli.main(["import-configs", str(src), "-o", str(out), "--lab-name", "dc1"]) == 0
    captured = capsys.readouterr()
    assert "(5 nodes, 8 links)" in captured.out
    assert "warning" not in captured.err.lower()

    text = out.read_text()
    assert text.startswith("# Imported from config files (5 nodes) by clabfleet.")
    topo = yaml.safe_load(text)
    assert topo["name"] == "dc1" and _links(topo) == FABRIC_LINKS
    assert all(n["image"].startswith("ceos:") for n in topo["topology"]["nodes"].values())
    assert cli.main(["validate", str(out)]) == 0

    configs = sorted(p.name for p in (out.parent / "configs").iterdir())
    assert configs == ["dc1-leaf1a.cfg", "dc1-leaf1b.cfg", "dc1-leaf1c.cfg",
                       "dc1-spine1.cfg", "dc1-spine2.cfg"]
    for path in [out, report_path(out), *(out.parent / "configs").iterdir()]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert SECRET not in path.read_text()
    report = yaml.safe_load(report_path(out).read_text())
    assert report["sanitised"] is True and len(report["links"]) == 8
    assert report_path(out).read_text().startswith("# Written by clabfleet import-configs")

    # Never over an existing file, unless told to
    assert cli.main(["import-configs", str(src), "-o", str(out)]) == 1
    assert "use --force" in capsys.readouterr().err
    assert cli.main(["import-configs", str(src), "-o", str(out), "--force",
                     "--no-sanitise", "--keep-management-name", "--image", "ceos:4.32.0F"]) == 0
    assert "--no-sanitise" in capsys.readouterr().err
    kept = (out.parent / "configs" / "dc1-spine1.cfg").read_text()
    assert SECRET in kept and "interface Management1" in kept
    assert yaml.safe_load(out.read_text())["topology"]["nodes"]["dc1-spine1"]["image"] == "ceos:4.32.0F"


def test_cli_prints_inline_and_takes_single_files(tmp_path, capsys):
    src = _write(tmp_path, _fabric())
    assert cli.main(["import-configs", str(src / "dc1-spine1.cfg"),
                     str(src / "dc1-leaf1a.cfg")]) == 0
    captured = capsys.readouterr()
    topo = yaml.safe_load(captured.out)
    assert _links(topo) == [("dc1-leaf1a:eth1", "dc1-spine1:eth1")]
    assert "hostname dc1-spine1" in topo["topology"]["nodes"]["dc1-spine1"]["startup-config"]
    # The leaf's other fabric ports face switches that were not given
    assert "port(s) face a device whose config was not given" in captured.err

    assert cli.main(["import-configs", str(tmp_path / "nope")]) == 1
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli.main(["import-configs", str(empty)]) == 1


# --- GUI ----------------------------------------------------------------------

def test_gui_upload_builds_a_lab_in_its_own_folder(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp")
    pytest.importorskip("ruamel.yaml")
    from aiohttp.test_utils import TestClient, TestServer

    from clabfleet.cluster import ClusterConfig, HostInfo
    from clabfleet.gui import server
    from clabfleet.gui.state import Workspace

    ws = Workspace(ClusterConfig(hosts=[HostInfo("localhost")]), [tmp_path])
    monkeypatch.setattr(Workspace, "runtime", lambda self: [])
    uploads = [{"name": name, "text": text} for name, text in _fabric().items()]

    async def scenario():
        app = server.create_app(ws, "secret-token")
        async with TestClient(TestServer(app)) as client:
            assert (await client.post("/api/import-configs",
                                      json={"name": "dc1", "files": uploads})).status == 401
            assert (await client.post("/login", json={"token": "secret-token"})).status == 200

            resp = await client.post("/api/import-configs", json={"name": "dc1", "files": uploads})
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            assert body == {"id": "dc1/dc1.clab.yml", "nodes": 5, "links": 8, "warnings": [],
                            "sanitised": True}
            state = await (await client.get("/api/state")).json()
            assert "dc1/dc1.clab.yml" in [t["id"] for t in state["topologies"]]
            detail = await (await client.get("/api/topologies/dc1/dc1.clab.yml")).json()
            assert len(detail["nodes"]) == 5 and len(detail["links"]) == 8

            # The same name again, a bad name, bad files
            again = await client.post("/api/import-configs", json={"name": "dc1", "files": uploads})
            assert again.status == 409
            for payload in ({"name": "../x", "files": uploads}, {"name": "x", "files": []},
                            {"name": "x", "files": [{"name": "a.cfg"}]},
                            {"name": "x", "files": [{"name": "a.txt", "text": "hello\n"}]},
                            {"name": "x", "files": uploads, "image": "bad image"}):
                resp = await client.post("/api/import-configs", json=payload)
                assert resp.status == 400, payload["name"]
            assert not (tmp_path / "x").exists()

            raw = await client.post("/api/import-configs",
                                    json={"name": "raw", "files": uploads, "sanitise": False})
            assert (await raw.json())["sanitised"] is False

    asyncio.run(scenario())
    lab = tmp_path / "dc1"
    assert _links(yaml.safe_load((lab / "dc1.clab.yml").read_text())) == FABRIC_LINKS
    config = (lab / "configs" / "dc1-leaf1a.cfg").read_text()
    assert SECRET not in config and "interface Management0" in config
    assert SECRET in (tmp_path / "raw" / "configs" / "dc1-leaf1a.cfg").read_text()
