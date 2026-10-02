"""export-live end to end, with NAPALM faked (see tests/live_fakes.py)."""

import copy
import json

import pytest
import yaml

import live_fakes
from clabfleet import cli
from clabfleet.exporter import (
    DeviceData,
    ExportError,
    ExportOptions,
    build_topology,
    collect_devices,
    export_from_live_network,
    report_path,
)
from clabfleet.inventory import parse_rules
from clabfleet.validate import validate_topology
from test_sanitise import SECRET_MARKER


@pytest.fixture
def napalm(monkeypatch):
    return live_fakes.install(monkeypatch)


def _inventory(tmp_path, **extra):
    data = {
        "defaults": {"username": "netops", "password": "PROD-PASSWORD-0999"},
        "devices": [{"hostname": d["hostname"], "platform": d["platform"]}
                    for d in live_fakes.INVENTORY],
        **extra,
    }
    path = tmp_path / "inventory.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def _links(topo):
    return sorted(tuple(sorted(link["endpoints"])) for link in topo["topology"]["links"])


EXPECTED_LINKS = sorted(tuple(sorted(pair)) for pair in [
    ("core-rtr-01:Ethernet0/1", "core-rtr-02:Ethernet0/1"),
    ("core-rtr-01:Ethernet0/2", "dist-sw-01:eth1"),
    ("core-rtr-01:Ethernet1/1", "dist-sw-02:eth49_1"),
    ("core-rtr-02:Ethernet0/2", "dist-sw-01:eth2"),
    ("dist-sw-01:eth49_1", "dist-sw-02:eth50_1"),
])
IMAGES = [
    {"kind": "cisco_iol", "version": r"17\.12\..*", "image": "vrnetlab/cisco_iol:17.12.01"},
    {"kind": "cisco_iol", "image": "vrnetlab/cisco_iol:17.9.1"},
    {"vendor": "arista", "version": r"4\.32\..*", "image": "ceos:4.32.0F"},
]


def test_end_to_end_cli(tmp_path, napalm, capsys):
    inv = _inventory(tmp_path, images=IMAGES)
    out = tmp_path / "lab" / "prod.clab.yml"
    rc = cli.main(["export-live", str(inv), "-o", str(out), "--lab-name", "prod",
                   "--sanitise", "--include-neighbours", "--neighbour-image", "alpine:3.20"])
    assert rc == 0
    stdout, stderr = capsys.readouterr()
    assert "Wrote" in stdout and "5 nodes, 6 links" in stdout
    assert "no image for dist-sw-02" in stderr

    report = validate_topology(out)
    assert report.ok and not report.warnings, (report.errors, report.warnings)

    topo = yaml.safe_load(out.read_text())
    nodes = topo["topology"]["nodes"]
    assert topo["name"] == "prod"
    assert list(nodes) == ["core-rtr-01", "core-rtr-02", "dist-sw-01", "dist-sw-02", "server-01"]
    assert nodes["core-rtr-01"] == {"kind": "cisco_iol", "image": "vrnetlab/cisco_iol:17.12.01",
                                    "startup-config": "configs/core-rtr-01.cfg"}
    assert nodes["core-rtr-02"]["image"] == "vrnetlab/cisco_iol:17.9.1"
    assert nodes["dist-sw-01"]["image"] == "ceos:4.32.0F"
    assert nodes["dist-sw-02"]["image"] == "REPLACE-ME/arista_ceos:latest"
    assert nodes["server-01"] == {"kind": "linux", "image": "alpine:3.20"}
    assert "oob-sw" not in nodes                       # seen on a management port only
    assert _links(topo) == sorted(EXPECTED_LINKS + [("dist-sw-01:eth10", "server-01:eth1")])

    # Configs: interfaces renamed, secrets gone, placeholder login added
    r1 = (out.parent / "configs" / "core-rtr-01.cfg").read_text()
    assert "interface Ethernet0/1\n" in r1 and "interface Ethernet1/1\n" in r1
    assert "GigabitEthernet0/0/1" not in r1
    assert "username admin privilege 15 secret 0 admin" in r1
    for cfg in (out.parent / "configs").iterdir():
        assert not SECRET_MARKER.findall(cfg.read_text()), cfg.name

    # Nothing anywhere holds the device login
    for path in out.parent.rglob("*"):
        if path.is_file():
            assert "PROD-PASSWORD" not in path.read_text(), path

    rep = yaml.safe_load(report_path(out).read_text())
    assert rep["nodes"]["core-rtr-01"]["interfaces"]["TenGigabitEthernet1/1"] == "Ethernet1/1"
    assert rep["nodes"]["core-rtr-01"]["image_from"] == "images rule #1"
    assert rep["nodes"]["core-rtr-02"]["image_from"] == "images rule #2"
    assert rep["nodes"]["dist-sw-02"]["image_from"] == "placeholder"
    assert rep["nodes"]["core-rtr-01"]["sanitised"]["changes"]["snmp"] == 4
    assert rep["nodes"]["server-01"]["neighbour"] is True


def test_stdout_without_output_file(tmp_path, napalm, capsys):
    assert cli.main(["export-live", str(_inventory(tmp_path))]) == 0
    topo = yaml.safe_load(capsys.readouterr().out)
    cfg = topo["topology"]["nodes"]["dist-sw-01"]["startup-config"]
    assert "interface Ethernet49/1" in cfg and "PROD-PASSWORD" not in cfg
    assert _links(topo) == EXPECTED_LINKS
    assert "server-01" not in topo["topology"]["nodes"]


def test_export_from_live_network_api_is_unchanged(tmp_path, napalm):
    topo = export_from_live_network(live_fakes.INVENTORY, output_file=tmp_path / "x.clab.yml",
                                    lab_name="api", kind_map={"eos": "ceos"})
    assert topo["name"] == "api"
    assert topo["topology"]["nodes"]["dist-sw-01"]["kind"] == "ceos"
    assert (tmp_path / "configs" / "core-rtr-01.cfg").exists()
    assert (tmp_path / "x.import-report.yaml").exists()


def test_unreachable_devices_are_skipped(napalm):
    devices = live_fakes.INVENTORY + [{"hostname": "gone.example.com", "platform": "ios",
                                       "username": "u", "password": "p"}]
    collected = collect_devices(devices)
    assert [d.spec["hostname"] for d in collected] == list(live_fakes.DEVICES)
    assert all("password" not in d.spec for d in collected)


def test_missing_napalm(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "napalm", None)
    with pytest.raises(ExportError, match="NAPALM is required"):
        collect_devices(live_fakes.INVENTORY)


def _collected(**overrides):
    out = []
    for spec in live_fakes.INVENTORY:
        data = copy.deepcopy(live_fakes.DEVICES[spec["hostname"]])
        s = {k: v for k, v in spec.items() if k not in ("username", "password")}
        s.update(overrides.get(data["facts"]["hostname"], {}))
        out.append(DeviceData(s, data["facts"], data["config"], data["lldp"]))
    return out


def test_image_and_kind_rule_precedence():
    kinds = parse_rules([{"model": "DCS-7050.*", "version": r"4\.31\..*", "kind": "arista_ceos",
                          "type": "ceos-lab"},
                         {"platform": "ios", "kind": "cisco_c8000v"}], "kinds", ("kind", "type"))
    images = parse_rules([{"kind": "cisco_c8000v", "image": "c8000v:rule"},
                          {"kind": "cisco_iol", "image": "iol:never-first"}],
                         "images", ("image", "kind"))
    collected = _collected(**{"core-rtr-02": {"image": "c8000v:pinned", "kind": "cisco_iol"}})
    result = build_topology(collected, ExportOptions(kind_rules=kinds, image_rules=images))
    nodes = result.topology["topology"]["nodes"]
    assert nodes["core-rtr-01"]["kind"] == "cisco_c8000v"         # kinds rule
    assert nodes["core-rtr-01"]["image"] == "c8000v:rule"
    assert nodes["core-rtr-02"]["kind"] == "cisco_iol"            # device entry wins
    assert nodes["core-rtr-02"]["image"] == "c8000v:pinned"
    assert nodes["dist-sw-02"]["type"] == "ceos-lab"              # first rule, with type
    assert "type" not in nodes["dist-sw-01"]                      # 4.32 → platform default
    assert nodes["dist-sw-01"]["image"] == "REPLACE-ME/arista_ceos:latest"
    assert result.report["nodes"]["core-rtr-01"]["kind_rule"] == 2
    # c8000v naming: GigabitEthernet0/0/1 has no 1:1 port → eth1, eth2, ...
    assert ("core-rtr-01:eth1", "core-rtr-02:Ethernet0/1") in _links(result.topology)


def test_keep_interface_names():
    result = build_topology(_collected(), ExportOptions(map_interfaces=False, inline_configs=True))
    links = _links(result.topology)
    assert ("core-rtr-01:Gi0/0/1", "core-rtr-02:GigabitEthernet0/0/1") in links
    cfg = result.topology["topology"]["nodes"]["core-rtr-01"]["startup-config"]
    assert "interface GigabitEthernet0/0/1" in cfg


def test_neighbour_placeholders_merge_and_use_chassis_id():
    collected = _collected()
    lldp = collected[2].lldp      # dist-sw-01
    lldp["Ethernet11"] = [{"remote_system_name": "server-01", "remote_port": "ens4"}]
    lldp["Ethernet12"] = [{"remote_system_name": "", "remote_port": "",
                           "remote_chassis_id": "52:54:00:12:34:56"}]
    result = build_topology(collected, ExportOptions(include_neighbours=True))
    nodes = result.topology["topology"]["nodes"]
    placeholders = [n for n, v in nodes.items() if v["kind"] == "linux"]
    assert placeholders == ["server-01", "52-54-00-12-34-56"]
    links = _links(result.topology)
    assert ("dist-sw-01:eth10", "server-01:eth1") in links
    assert ("dist-sw-01:eth11", "server-01:eth2") in links
    assert ("52-54-00-12-34-56:eth1", "dist-sw-01:eth12") in links
    # Without the flag they are left out
    result = build_topology(_collected(), ExportOptions())
    assert "server-01" not in result.topology["topology"]["nodes"]


def test_sanitise_drops_configs_of_unsupported_platforms():
    collected = _collected(**{"dist-sw-02": {"platform": "fortios"}})
    from clabfleet.sanitise import SanitiseOptions
    result = build_topology(collected, ExportOptions(sanitise=SanitiseOptions()))
    assert "startup-config" not in result.topology["topology"]["nodes"]["dist-sw-02"]
    assert "dist-sw-02" not in result.configs
    assert any("sanitising is not supported" in w for w in result.warnings)


def test_inconsistent_lldp_never_reuses_an_interface():
    collected = _collected()
    # dist-sw-01 now sees dist-sw-02 on another port, dist-sw-02's entry is stale
    lldp = collected[2].lldp
    lldp["Ethernet48/1"] = lldp.pop("Ethernet49/1")
    result = build_topology(collected, ExportOptions())
    used = [ep for link in result.topology["topology"]["links"] for ep in link["endpoints"]]
    assert len(used) == len(set(used))
    assert any("already used" in w for w in result.warnings)


def test_interface_limit_leaves_links_out():
    collected = _collected()
    lldp = collected[0].lldp      # core-rtr-01 → 70 extra IOL ports to a neighbour
    for i in range(1, 71):
        lldp[f"GigabitEthernet2/0/{i}"] = [{"remote_system_name": "big", "remote_port": f"p{i}"}]
    result = build_topology(collected, ExportOptions(include_neighbours=True))
    assert len(result.report["nodes"]["core-rtr-01"]["dropped_interfaces"]) > 0
    assert any("left out" in w for w in result.warnings)
    used = [ep for link in result.topology["topology"]["links"] for ep in link["endpoints"]
            if ep.startswith("core-rtr-01:")]
    assert len(used) == len(set(used)) == 63


# --- re-sync ------------------------------------------------------------------

def _first_import(tmp_path, capsys, *extra):
    inv = _inventory(tmp_path, images=IMAGES)
    out = tmp_path / "prod.clab.yml"
    assert cli.main(["export-live", str(inv), "-o", str(out), "--include-neighbours",
                     *extra]) == 0
    capsys.readouterr()
    return inv, out


def _hand_edit(out):
    text = out.read_text()
    text = text.replace("    core-rtr-01:\n",
                        "    # the core\n    core-rtr-01:\n"
                        "      labels:\n        graph-posX: '120'\n        graph-posY: '80'\n", 1)
    text = text.replace("image: REPLACE-ME/arista_ceos:latest", "image: ceos:hand-picked")
    text = text.replace("startup-config: configs/dist-sw-02.cfg",
                        "startup-config: configs/custom-sw2.cfg")
    text = text.replace("  links:\n", "    client:\n      kind: linux\n      image: alpine:3\n"
                                      "  links:\n  - endpoints: [client:eth1, server-01:eth5]\n")
    out.write_text(text)
    (out.parent / "configs" / "custom-sw2.cfg").write_text("hostname dist-sw-02\n! by hand\n")
    return out.read_text()


def _changed_network():
    devices = copy.deepcopy(live_fakes.DEVICES)
    r2 = devices["core-rtr-02.example.com"]
    r2["config"] = r2["config"].replace("snmp-server", "ip domain name example.com\nsnmp-server")
    r2["lldp"]["GigabitEthernet0/0/3"] = [live_fakes._n("dist-sw-02", "Ethernet51/1")]
    sw1 = devices["dist-sw-01.example.com"]
    sw1["lldp"]["Ethernet48/1"] = sw1["lldp"].pop("Ethernet49/1")
    del sw1["lldp"]["Ethernet10"]                          # server-01 is gone
    sw2 = devices["dist-sw-02.example.com"]
    sw2["lldp"]["Ethernet51/1"] = [live_fakes._n("core-rtr-02", "Gi0/0/3")]
    sw2["lldp"]["Ethernet50/1"] = [live_fakes._n("dist-sw-01", "Ethernet48/1")]
    sw2["config"] += "! new line\n"
    # Only a timestamp changes on core-rtr-01: not a change
    r1 = devices["core-rtr-01.example.com"]
    r1["config"] = r1["config"].replace("10:12:01", "11:00:00")
    return devices


def test_resync_reports_without_writing(tmp_path, monkeypatch, capsys):
    live_fakes.install(monkeypatch)
    inv, out = _first_import(tmp_path, capsys)
    before = _hand_edit(out)
    cfg_before = (tmp_path / "configs" / "core-rtr-02.cfg").read_text()
    report_before = report_path(out).read_text()

    live_fakes.install(monkeypatch, _changed_network())
    assert cli.main(["export-live", str(inv), "-o", str(out), "--include-neighbours"]) == 0
    text = capsys.readouterr().out
    assert "+ link core-rtr-02:Ethernet0/3 -- dist-sw-02:eth51_1" in text
    assert "~ link dist-sw-01:eth49_1 -- dist-sw-02:eth50_1 -> " \
           "dist-sw-01:eth48_1 -- dist-sw-02:eth50_1" in text
    assert "- node server-01" in text
    assert "- link dist-sw-01:eth10 -- server-01:eth1" in text
    assert "~ node core-rtr-02: startup config changed" in text
    assert "~ node dist-sw-02: startup config changed" in text
    assert "core-rtr-01" not in text            # timestamp-only config change
    assert "client" not in text                 # hand-added nodes are not imported ones
    assert "dist-sw-02: image" not in text      # placeholder never replaces a hand-set image
    assert "Nothing written" in text
    assert out.read_text() == before
    assert (tmp_path / "configs" / "core-rtr-02.cfg").read_text() == cfg_before
    assert report_path(out).read_text() == report_before

    assert cli.main(["export-live", str(inv), "-o", str(out), "--include-neighbours",
                     "--json"]) == 0
    diff = json.loads(capsys.readouterr().out)
    assert diff["applied"] is False
    assert diff["nodes"] == {"added": [], "removed": ["server-01"],
                             "changed": {"core-rtr-02": {"startup-config": [
                                 "configs/core-rtr-02.cfg", "configs/core-rtr-02.cfg"]},
                                 "dist-sw-02": {"startup-config": [
                                     "configs/custom-sw2.cfg", "configs/dist-sw-02.cfg"]}}}
    assert diff["links"]["added"] == [["core-rtr-02:Ethernet0/3", "dist-sw-02:eth51_1"]]
    assert diff["links"]["changed"] == [{"old": ["dist-sw-01:eth49_1", "dist-sw-02:eth50_1"],
                                         "new": ["dist-sw-01:eth48_1", "dist-sw-02:eth50_1"]}]
    assert out.read_text() == before


def test_resync_apply_keeps_hand_edits(tmp_path, monkeypatch, capsys):
    live_fakes.install(monkeypatch)
    inv, out = _first_import(tmp_path, capsys)
    _hand_edit(out)
    live_fakes.install(monkeypatch, _changed_network())

    assert cli.main(["export-live", str(inv), "-o", str(out), "--include-neighbours",
                     "--apply"]) == 0
    printed = capsys.readouterr().out
    assert "kept hand-set startup-config configs/custom-sw2.cfg" in printed
    assert "removed nodes/links kept; use --prune" in printed
    text = out.read_text()
    assert "    # the core\n" in text and "graph-posX: '120'" in text
    assert text.startswith("# Imported from live network")
    topo = yaml.safe_load(text)
    nodes = topo["topology"]["nodes"]
    assert nodes["dist-sw-02"]["image"] == "ceos:hand-picked"
    assert nodes["dist-sw-02"]["startup-config"] == "configs/custom-sw2.cfg"
    assert (tmp_path / "configs" / "custom-sw2.cfg").read_text().endswith("! by hand\n")
    assert (tmp_path / "configs" / "dist-sw-02.cfg").read_text().endswith("! new line\n")
    assert "ip domain name" in (tmp_path / "configs" / "core-rtr-02.cfg").read_text()
    assert "client" in nodes and "server-01" in nodes     # no --prune
    links = _links(topo)
    assert ("client:eth1", "server-01:eth5") in links
    assert ("core-rtr-02:Ethernet0/3", "dist-sw-02:eth51_1") in links
    assert ("dist-sw-01:eth48_1", "dist-sw-02:eth50_1") in links
    assert ("dist-sw-01:eth49_1", "dist-sw-02:eth50_1") not in links
    report = validate_topology(out)
    assert report.ok, report.errors

    # Prune removes what is gone, but not hand-added nodes
    assert cli.main(["export-live", str(inv), "-o", str(out), "--include-neighbours",
                     "--apply", "--prune"]) == 0
    printed = capsys.readouterr().out
    assert "- node server-01" in printed and "removed node server-01" in printed
    topo = yaml.safe_load(out.read_text())
    assert "server-01" not in topo["topology"]["nodes"]
    assert "client" in topo["topology"]["nodes"]
    assert not any("server-01" in ep for link in topo["topology"]["links"]
                   for ep in link["endpoints"])
    assert validate_topology(out).ok

    # Nothing left to do
    assert cli.main(["export-live", str(inv), "-o", str(out), "--include-neighbours"]) == 0
    assert "No changes." in capsys.readouterr().out


def test_resync_keeps_interface_assignments(tmp_path, monkeypatch, capsys):
    live_fakes.install(monkeypatch)
    inv, out = _first_import(tmp_path, capsys)
    devices = copy.deepcopy(live_fakes.DEVICES)
    # A new port that sorts before the existing ones on core-rtr-01
    devices["core-rtr-01.example.com"]["lldp"]["Gi0/0/0"] = [
        live_fakes._n("core-rtr-02", "Gi0/0/7")]
    live_fakes.install(monkeypatch, devices)
    assert cli.main(["export-live", str(inv), "-o", str(out), "--include-neighbours",
                     "--json"]) == 0
    diff = json.loads(capsys.readouterr().out)
    assert diff["links"]["changed"] == [] and diff["links"]["removed"] == []
    assert len(diff["links"]["added"]) == 1


def test_overwrite_and_flag_errors(tmp_path, monkeypatch, capsys):
    live_fakes.install(monkeypatch)
    inv = _inventory(tmp_path)
    out = tmp_path / "prod.clab.yml"
    assert cli.main(["export-live", str(inv), "-o", str(out), "--apply"]) == 1
    assert "re-syncing an existing" in capsys.readouterr().err
    assert cli.main(["export-live", str(inv), "-o", str(out)]) == 0
    out.write_text(out.read_text().replace("cisco_iol", "linux"))
    assert cli.main(["export-live", str(inv), "-o", str(out), "--overwrite"]) == 0
    assert "cisco_iol" in out.read_text()
    assert cli.main(["export-live", str(inv), "-o", str(out), "--prune"]) == 1
    assert cli.main(["export-live"]) == 1
    assert "give an inventory" in capsys.readouterr().err
    assert cli.main(["export-live", str(inv), "--filter", "nonsense"]) == 1


def test_resync_apply_without_ruamel_falls_back_to_pyyaml(tmp_path, monkeypatch, capsys):
    from clabfleet.gui import editing

    live_fakes.install(monkeypatch)
    inv, out = _first_import(tmp_path, capsys)
    live_fakes.install(monkeypatch, _changed_network())

    def no_ruamel(text):
        raise RuntimeError("no ruamel")
    monkeypatch.setattr(editing, "_yaml_for", no_ruamel)
    assert cli.main(["export-live", str(inv), "-o", str(out), "--include-neighbours",
                     "--apply"]) == 0
    topo = yaml.safe_load(out.read_text())
    assert ("core-rtr-02:Ethernet0/3", "dist-sw-02:eth51_1") in _links(topo)
    assert validate_topology(out).ok


def test_cli_with_netbox_source(tmp_path, monkeypatch, capsys):
    from clabfleet import inventory
    from test_inventory import FakeAPI, _nb_device

    devices = {}
    results = []
    for i, (host, data) in enumerate(live_fakes.DEVICES.items(), 1):
        ip = f"10.0.0.{i}"
        devices[ip] = data
        platform = "cisco-ios-xe" if "rtr" in host else "arista-eos"
        results.append(_nb_device(data["facts"]["hostname"], ip, platform))
    live_fakes.install(monkeypatch, devices)
    api = FakeAPI({"/api/dcim/devices/": [results[:2], results[2:]]})
    monkeypatch.setattr(inventory, "urlopen", api)
    monkeypatch.setenv("NETBOX_TOKEN", "tok")
    monkeypatch.setenv("CLABFLEET_DEVICE_USERNAME", "netops")
    monkeypatch.setenv("CLABFLEET_DEVICE_PASSWORD", "pw")
    out = tmp_path / "nb.clab.yml"
    assert cli.main(["export-live", "--netbox", "https://netbox.example.com", "-o", str(out),
                     "--filter", "site=dc1", "--filter", "role=leaf",
                     "--filter", "role=spine"]) == 0
    query = api.queries("/api/dcim/devices/")[0]
    assert query["site"] == ["dc1"] and query["role"] == ["leaf", "spine"]
    topo = yaml.safe_load(out.read_text())
    assert _links(topo) == EXPECTED_LINKS
    assert sorted(live_fakes.FakeDevice.opened) == sorted(devices)
    assert validate_topology(out).ok


# --- keeping production data where it belongs -----------------------------------

def _mode(path):
    return path.stat().st_mode & 0o777


def test_files_are_private_and_raw_configs_warn(tmp_path, napalm, capsys):
    inv = _inventory(tmp_path)
    out = tmp_path / "lab" / "prod.clab.yml"
    (tmp_path / "lab" / "configs").mkdir(parents=True)
    old = tmp_path / "lab" / "configs" / "core-rtr-01.cfg"
    old.write_text("stale\n")
    old.chmod(0o644)
    assert cli.main(["export-live", str(inv), "-o", str(out)]) == 0
    assert "WARNING: --sanitise not given" in capsys.readouterr().err
    assert _mode(out.parent / "configs") == 0o700
    for path in [out, report_path(out), *(out.parent / "configs").iterdir()]:
        assert _mode(path) == 0o600, path
    assert "stale" not in old.read_text()

    assert cli.main(["export-live", str(inv), "-o", str(out), "--sanitise", "--overwrite"]) == 0
    assert "--sanitise not given" not in capsys.readouterr().err
    assert cli.main(["export-live", str(inv)]) == 0        # inline configs on stdout
    assert "--sanitise not given" in capsys.readouterr().err


def test_resync_keeps_a_sanitised_lab_sanitised(tmp_path, monkeypatch, capsys):
    live_fakes.install(monkeypatch)
    inv, out = _first_import(tmp_path, capsys, "--sanitise")
    live_fakes.install(monkeypatch, _changed_network())
    for flag in ("--apply", "--overwrite"):
        assert cli.main(["export-live", str(inv), "-o", str(out), flag]) == 1
        assert "was sanitised" in capsys.readouterr().err
    assert "PROD" not in (tmp_path / "configs" / "core-rtr-02.cfg").read_text()
    # Reporting alone writes nothing, so it needs no flag
    assert cli.main(["export-live", str(inv), "-o", str(out)]) == 0
    assert cli.main(["export-live", str(inv), "-o", str(out), "--include-neighbours",
                     "--apply", "--sanitise"]) == 0
    assert not SECRET_MARKER.findall((tmp_path / "configs" / "core-rtr-02.cfg").read_text())
    assert _mode(out) == 0o600
    capsys.readouterr()
    assert cli.main(["export-live", str(inv), "-o", str(out), "--apply", "--no-sanitise",
                     "--include-neighbours"]) == 0
    assert "R2SECRET" in (tmp_path / "configs" / "core-rtr-02.cfg").read_text()
    assert cli.main(["export-live", str(inv), "-o", str(out), "--sanitise",
                     "--no-sanitise"]) == 1


def test_residual_secrets_stop_the_export(tmp_path, monkeypatch, capsys):
    devices = copy.deepcopy(live_fakes.DEVICES)
    r2 = devices["core-rtr-02.example.com"]
    r2["config"] = r2["config"].replace(
        "end\n", "interface Gi9\n ip frobnicate authentication text PLANTED0001\nend\n")
    live_fakes.install(monkeypatch, devices)
    inv = _inventory(tmp_path)
    out = tmp_path / "prod.clab.yml"
    assert cli.main(["export-live", str(inv), "-o", str(out), "--sanitise"]) == 1
    err = capsys.readouterr().err
    assert "still look like they hold secrets" in err
    assert "core-rtr-02: line" in err and "(authentication)" in err
    assert "PLANTED" not in err
    assert not out.exists() and not (tmp_path / "configs").exists()

    assert cli.main(["export-live", str(inv), "-o", str(out), "--sanitise",
                     "--allow-residual"]) == 0
    assert "core-rtr-02: config may still hold secrets" in capsys.readouterr().err
    rep = yaml.safe_load(report_path(out).read_text())
    assert rep["nodes"]["core-rtr-02"]["sanitised"]["residual"][0]["category"] == \
        "authentication"
    assert "PLANTED" not in report_path(out).read_text()


def test_sanitised_report_leaves_out_device_addresses():
    from clabfleet.sanitise import SanitiseOptions
    result = build_topology(_collected(), ExportOptions(sanitise=SanitiseOptions(),
                                                        include_neighbours=True))
    assert result.report["sanitised"] is True
    for name, entry in result.report["nodes"].items():
        assert "source" not in entry, name
    assert "example.com" not in yaml.safe_dump(result.report)
    result = build_topology(_collected(), ExportOptions(include_neighbours=True))
    assert result.report["sanitised"] is False
    assert result.report["nodes"]["core-rtr-01"]["source"] == "core-rtr-01.example.com"
    assert result.report["nodes"]["server-01"]["source"] == "server-01.example.com"


def test_kept_interface_names_are_checked_and_warnings_escaped():
    collected = _collected()
    lldp = collected[2].lldp      # dist-sw-01
    lldp["Ethernet11"] = [{"remote_system_name": "core-rtr-02",
                           "remote_port": "Gi0/0/9\x1b]0;owned\x07"}]
    lldp["Ethernet12"] = [{"remote_system_name": "core-rtr-02", "remote_port": "x" * 65}]
    result = build_topology(collected, ExportOptions(map_interfaces=False))
    eps = [ep for link in result.topology["topology"]["links"] for ep in link["endpoints"]]
    assert not any("Gi0/0/9" in ep or "xxx" in ep for ep in eps)
    bad = [w for w in result.warnings if "not a usable interface name" in w]
    assert len(bad) == 2
    assert all("\x1b" not in w and "\x07" not in w for w in result.warnings)
    assert any(r"\x1b]0;owned\x07" in w for w in bad)
    assert ("core-rtr-01:Gi0/0/1", "core-rtr-02:GigabitEthernet0/0/1") in _links(result.topology)


def test_empty_lldp_names_never_match_a_device():
    collected = _collected()
    collected[1].facts["fqdn"] = ".example.com"     # core-rtr-02 without a hostname part
    collected[2].lldp["Ethernet11"] = [{"remote_system_name": "", "remote_port": "Gi9"}]
    result = build_topology(collected, ExportOptions())
    assert _links(result.topology) == EXPECTED_LINKS


@pytest.mark.parametrize("hostname,expected", [
    ("-x", "x"), ("_core", "core"), (".example.com", "node"), ("---", "node"),
    ("rtr 1.example.com", "rtr-1"), ("Leaf_01", "Leaf_01"),
])
def test_node_names_start_with_a_letter_or_digit(hostname, expected):
    import re
    from clabfleet.exporter import _node_name
    assert _node_name(hostname) == expected
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", _node_name(hostname))


def test_ssh_drivers_check_known_host_keys(monkeypatch):
    seen = {}

    def get_network_driver(platform):
        def driver(hostname, username, password, optional_args=None):
            seen[hostname] = optional_args
            raise ConnectionError("not reachable")
        return driver
    import sys
    import types
    module = types.ModuleType("napalm")
    module.get_network_driver = get_network_driver
    monkeypatch.setitem(sys.modules, "napalm", module)
    collect_devices([
        {"hostname": "a", "platform": "ios", "username": "u", "password": "p"},
        {"hostname": "b", "platform": "nxos_ssh", "username": "u", "password": "p",
         "optional_args": {"system_host_keys": False, "port": 2222}},
        {"hostname": "c", "platform": "eos", "username": "u", "password": "p"},
    ])
    assert seen["a"] == {"system_host_keys": True}
    assert seen["b"] == {"system_host_keys": False, "port": 2222}   # inventory wins
    assert seen["c"] == {}
