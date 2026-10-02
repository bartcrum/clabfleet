import http.server
import io
import json
import threading
import urllib.error
import urllib.parse

import pytest
import yaml

from clabfleet import inventory
from clabfleet.inventory import (
    InventoryError,
    ansible_devices,
    detect_format,
    first_match,
    load_inventory,
    parse_rules,
)

TOKEN = "nbtoken-SECRET-0001"


class FakeAPI:
    """Stands in for urllib's urlopen: serves canned JSON pages by path."""

    def __init__(self, pages: dict, status: int = 200):
        self.pages = pages      # path → list of result pages
        self.status = status
        self.requests = []

    def __call__(self, req, timeout=None, context=None):
        self.requests.append(req)
        if self.status != 200:
            raise urllib.error.HTTPError(req.full_url, self.status, "Forbidden", {}, None)
        url = urllib.parse.urlsplit(req.full_url)
        query = urllib.parse.parse_qs(url.query)
        pages = self.pages.get(url.path, [[]])
        page = int(query.get("page", ["0"])[0])
        nxt = None
        if page + 1 < len(pages):
            nxt = f"{url.scheme}://{url.netloc}{url.path}?{url.query}&page={page + 1}"
        body = json.dumps({"count": sum(map(len, pages)), "next": nxt,
                           "results": pages[page]}).encode()
        return io.BytesIO(body)

    def queries(self, path):
        return [urllib.parse.parse_qs(urllib.parse.urlsplit(r.full_url).query)
                for r in self.requests if urllib.parse.urlsplit(r.full_url).path == path]


def _nb_device(name, ip, platform, model="DCS-7050", role="leaf"):
    return {
        "id": hash(name) % 1000, "name": name,
        "primary_ip": {"address": f"{ip}/24"} if ip else None,
        "platform": {"slug": platform, "name": platform} if platform else None,
        "device_type": {"model": model, "manufacturer": {"name": "Arista", "slug": "arista"}},
        "role": {"slug": role, "name": role.title()},
        "site": {"slug": "dc1", "name": "DC1"},
        "tags": [{"slug": "lab-import", "name": "lab-import"}],
    }


@pytest.fixture
def env(monkeypatch):
    values = {"NETBOX_TOKEN": TOKEN, "NAUTOBOT_TOKEN": TOKEN,
              "CLABFLEET_DEVICE_USERNAME": "netops", "CLABFLEET_DEVICE_PASSWORD": "pw"}
    return values


def test_netbox_devices_paginates_filters_and_maps_platforms(env, monkeypatch, caplog):
    api = FakeAPI({
        "/api/dcim/platforms/": [[{"slug": "cisco-xe", "napalm_driver": "ios"},
                                  {"slug": "arista-eos", "napalm_driver": ""}]],
        "/api/dcim/devices/": [
            [_nb_device("leaf1", "10.0.0.1", "arista-eos"),
             _nb_device("rtr1", "10.0.0.2", "cisco-xe", model="C8300")],
            [_nb_device("odd1", None, "vendor-x"),
             _nb_device("leaf2", None, "my-eos-platform")],
        ],
    })
    monkeypatch.setattr(inventory, "urlopen", api)
    inv = load_inventory(netbox="https://netbox.example.com/",
                         filters={"site": "dc1", "role": ["leaf", "border"], "tag": "lab-import"},
                         env=env)
    assert [d["name"] for d in inv.devices] == ["leaf1", "rtr1", "leaf2"]
    leaf1, rtr1, leaf2 = inv.devices
    assert leaf1["hostname"] == "10.0.0.1" and leaf1["platform"] == "eos"   # guessed
    assert rtr1["platform"] == "ios"                                        # napalm_driver
    assert leaf2["hostname"] == "leaf2"                                     # no primary IP
    assert leaf1["model"] == "DCS-7050" and leaf1["vendor"] == "Arista"
    assert leaf1["role"] == "leaf" and leaf1["site"] == "dc1"
    assert leaf1["username"] == "netops" and leaf1["password"] == "pw"
    assert any("odd1" in w and "platform_map" in w for w in inv.warnings)
    assert inv.source == "netbox"

    for req in api.requests:
        assert req.get_header("Authorization") == f"Token {TOKEN}"
        assert req.get_header("Accept") == "application/json"
    first, second = api.queries("/api/dcim/devices/")
    assert first["site"] == ["dc1"] and first["role"] == ["leaf", "border"]
    assert first["tag"] == ["lab-import"] and first["limit"] == [str(inventory.PAGE_SIZE)]
    assert second["page"] == ["1"]
    assert TOKEN not in caplog.text


def test_netbox_platform_map_from_inventory_file(tmp_path, env, monkeypatch):
    api = FakeAPI({"/api/dcim/devices/": [[_nb_device("fw1", "10.0.0.9", "vendor-x")]]})
    monkeypatch.setattr(inventory, "urlopen", api)
    path = tmp_path / "inv.yaml"
    path.write_text(yaml.safe_dump({
        "source": {"type": "netbox", "url": "https://nb", "token_env": "MY_NB",
                   "filters": {"site": "dc2"}, "platform_map": {"vendor-x": "fortios"}},
        "defaults": {"username": "u", "password": "p"},
    }))
    inv = load_inventory(path, filters={"role": "fw"}, env={"MY_NB": "t0k"})
    assert inv.devices[0]["platform"] == "fortios"
    q = api.queries("/api/dcim/devices/")[0]
    assert q["site"] == ["dc2"] and q["role"] == ["fw"]
    assert api.requests[0].get_header("Authorization") == "Token t0k"


def test_netbox_errors(env, monkeypatch):
    monkeypatch.setattr(inventory, "urlopen", FakeAPI({}, status=403))
    with pytest.raises(InventoryError) as exc:
        load_inventory(netbox="https://nb", env=env)
    assert "HTTP 403" in str(exc.value) and "token" in str(exc.value)
    assert TOKEN not in str(exc.value)

    def unreachable(req, **kw):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(inventory, "urlopen", unreachable)
    with pytest.raises(InventoryError, match="cannot reach https://nb/api/dcim/platforms/"):
        load_inventory(netbox="https://nb", env=env)

    monkeypatch.setattr(inventory, "urlopen", lambda req, **kw: io.BytesIO(b"<html>"))
    with pytest.raises(InventoryError, match="invalid JSON"):
        load_inventory(netbox="https://nb", env=env)

    with pytest.raises(InventoryError, match="NETBOX_TOKEN"):
        load_inventory(netbox="https://nb", env={})


def test_nautobot_devices(env, monkeypatch):
    api = FakeAPI({"/api/dcim/devices/": [[
        {"name": "spine1", "primary_ip4": {"address": "10.1.0.1/32"},
         "platform": {"name": "Arista EOS", "network_driver": "arista_eos",
                      "network_driver_mappings": {"napalm": "eos"}},
         "device_type": {"model": "7280R3", "manufacturer": {"id": "x", "url": "u"}},
         "role": {"name": "spine"}, "location": {"name": "DC1"}, "tags": []},
        {"name": "rtr1", "primary_ip4": None, "primary_ip": {"address": "10.1.0.2/32"},
         "platform": {"name": "IOS router", "network_driver": "cisco_ios",
                      "napalm_driver": ""},
         "device_type": {"model": "ISR4451"}, "role": {"name": "edge"}},
        {"name": "fw1", "platform": None},
    ]]})
    monkeypatch.setattr(inventory, "urlopen", api)
    inv = load_inventory(nautobot="https://nautobot.example.com",
                         filters={"location": "DC1", "role": "spine"}, env=env)
    spine, rtr = inv.devices
    assert spine["platform"] == "eos" and spine["hostname"] == "10.1.0.1"
    assert spine["role"] == "spine" and spine["site"] == "DC1" and spine["model"] == "7280R3"
    assert "vendor" not in spine           # manufacturer only a reference at depth=1
    assert rtr["platform"] == "ios" and rtr["hostname"] == "10.1.0.2"
    assert any("fw1" in w for w in inv.warnings)
    q = api.queries("/api/dcim/devices/")[0]
    assert q["depth"] == ["1"] and q["location"] == ["DC1"] and q["role"] == ["spine"]


ANSIBLE_YAML = """
all:
  vars:
    ansible_user: netops
    ansible_password: from-all
  children:
    routers:
      vars:
        ansible_network_os: cisco.ios.ios
        ansible_password: from-routers
      hosts:
        rtr1:
          ansible_host: 10.0.0.1
        rtr2:
          ansible_host: 10.0.0.2
          ansible_password: from-host
    switches:
      vars:
        ansible_network_os: arista.eos.eos
      children:
        leaves:
          hosts:
            leaf1:
              ansible_host: 10.0.1.1
              ansible_port: 2222
              clab_image: ceos:4.32.0F
            leaf2:
              ansible_password: !vault |
                $ANSIBLE_VAULT;1.1;AES256
                3132
    servers:
      hosts:
        web1:
          ansible_host: 10.0.9.9
"""


def test_ansible_yaml_inheritance(tmp_path):
    path = tmp_path / "hosts.yml"
    path.write_text(ANSIBLE_YAML)
    (tmp_path / "group_vars").mkdir()
    (tmp_path / "group_vars" / "leaves.yml").write_text("ansible_user: leafadmin\n")
    (tmp_path / "host_vars").mkdir()
    (tmp_path / "host_vars" / "rtr1.yaml").write_text("ansible_become_password: en4ble\n")
    warnings = []
    devices = {d["name"]: d for d in ansible_devices(path, warnings=warnings)}
    assert set(devices) == {"rtr1", "rtr2", "leaf1", "leaf2"}
    assert devices["rtr1"] == {
        "hostname": "10.0.0.1", "name": "rtr1", "platform": "ios", "groups": ["routers"],
        "username": "netops", "password": "from-routers",
        "optional_args": {"secret": "en4ble"},
    }
    assert devices["rtr2"]["password"] == "from-host"
    leaf1 = devices["leaf1"]
    assert leaf1["platform"] == "eos" and leaf1["username"] == "leafadmin"
    assert leaf1["optional_args"] == {"port": 2222}
    assert leaf1["image"] == "ceos:4.32.0F"
    assert leaf1["groups"] == ["leaves", "switches"]
    assert devices["leaf2"]["hostname"] == "leaf2"
    assert "password" not in devices["leaf2"]   # vaulted: comes from the environment
    assert any("leaf2" in w and "vault" in w for w in warnings)
    assert any("web1" in w for w in warnings)


def test_ansible_group_filter(tmp_path):
    path = tmp_path / "hosts.yml"
    path.write_text(ANSIBLE_YAML)
    names = [d["name"] for d in ansible_devices(path, {"group": "switches"})]
    assert names == ["leaf1", "leaf2"]
    with pytest.raises(InventoryError, match="only filter on group"):
        ansible_devices(path, {"site": "dc1"})


ANSIBLE_INI = """
# production
edge0 ansible_host=192.0.2.1 ansible_network_os=junos

[spines]
spine[1:2].dc1 ansible_network_os=eos

[leaves]
leaf[01:03] ansible_network_os=nxos
leaf-x ansible_host="10.9.9.9" ansible_network_os=cisco.nxos.nxos ansible_password='p w'

[dc1:children]
spines
leaves

[dc1:vars]
ansible_user=admin
ansible_password=dcpass
ansible_port=22
"""


def test_ansible_ini(tmp_path):
    path = tmp_path / "hosts"
    path.write_text(ANSIBLE_INI)
    assert detect_format(path) == "ansible"
    devices = {d["name"]: d for d in ansible_devices(path)}
    assert sorted(devices) == ["edge0", "leaf-x", "leaf01", "leaf02", "leaf03",
                               "spine1.dc1", "spine2.dc1"]
    assert devices["edge0"]["platform"] == "junos" and devices["edge0"]["hostname"] == "192.0.2.1"
    assert "username" not in devices["edge0"]           # not in dc1
    assert devices["spine1.dc1"]["platform"] == "eos"
    assert devices["spine1.dc1"]["groups"] == ["dc1", "spines"]
    assert devices["leaf02"]["platform"] == "nxos_ssh"
    assert devices["leaf02"]["username"] == "admin"
    assert devices["leaf02"]["optional_args"] == {"port": 22}
    assert devices["leaf-x"]["hostname"] == "10.9.9.9"
    assert devices["leaf-x"]["password"] == "p w"
    assert [d["name"] for d in ansible_devices(path, {"group": "spines"})] == \
        ["spine1.dc1", "spine2.dc1"]


def test_load_inventory_with_ansible_and_env_credentials(tmp_path):
    path = tmp_path / "hosts.ini"
    path.write_text(ANSIBLE_INI)
    inv = load_inventory(path, env={"CLABFLEET_DEVICE_USERNAME": "envuser",
                                    "CLABFLEET_DEVICE_PASSWORD": "envpw"})
    edge = next(d for d in inv.devices if d["name"] == "edge0")
    assert edge["username"] == "envuser" and edge["password"] == "envpw"
    leaf = next(d for d in inv.devices if d["name"] == "leaf01")
    assert leaf["username"] == "admin"      # inventory wins over the environment
    assert inv.source == "ansible"


def test_native_inventory_with_ansible_source_and_rules(tmp_path):
    (tmp_path / "hosts.ini").write_text(ANSIBLE_INI)
    path = tmp_path / "inv.yaml"
    path.write_text(yaml.safe_dump({
        "source": {"type": "ansible", "path": "hosts.ini", "filters": {"group": "spines"}},
        "kinds": [{"platform": "eos", "kind": "arista_ceos"}],
        "images": [{"kind": "arista_ceos", "image": "ceos:4.32.0F"}],
    }))
    inv = load_inventory(path, env={})
    assert [d["name"] for d in inv.devices] == ["spine1.dc1", "spine2.dc1"]
    assert inv.kind_rules[0].values == {"kind": "arista_ceos"}
    assert inv.image_rules[0].values == {"image": "ceos:4.32.0F"}


def test_native_inventory_defaults_and_password_env(tmp_path):
    path = tmp_path / "inv.yaml"
    path.write_text(yaml.safe_dump({
        "defaults": {"username": "netops", "password_env": "PROD_PW",
                     "optional_args": {"transport": "ssh"}},
        "devices": [
            {"hostname": "r1", "platform": "ios"},
            {"hostname": "r2", "platform": "eos", "password": "own",
             "optional_args": {"port": 443}},
        ],
    }))
    inv = load_inventory(path, env={"PROD_PW": "secret-from-env"})
    r1, r2 = inv.devices
    assert r1["password"] == "secret-from-env" and r1["username"] == "netops"
    assert r1["optional_args"] == {"transport": "ssh"}
    assert r2["password"] == "own"
    assert r2["optional_args"] == {"transport": "ssh", "port": 443}
    assert "password_env" not in r1


def test_missing_credentials_are_reported_without_values(tmp_path):
    path = tmp_path / "inv.yaml"
    path.write_text(yaml.safe_dump({"devices": [
        {"hostname": "r1", "platform": "ios", "username": "u", "password": "hunter2"},
        {"hostname": "r2", "platform": "ios"},
    ]}))
    with pytest.raises(InventoryError) as exc:
        load_inventory(path, env={})
    assert "r2: missing username, password" in str(exc.value)
    assert "hunter2" not in str(exc.value)


def test_plain_list_inventory_still_works(tmp_path):
    path = tmp_path / "inv.yaml"
    path.write_text(yaml.safe_dump([{"hostname": "r1", "platform": "ios",
                                     "username": "u", "password": "p"}]))
    assert detect_format(path) == "native"
    assert load_inventory(path, env={}).devices[0]["hostname"] == "r1"


def test_source_conflicts(tmp_path):
    with pytest.raises(InventoryError, match="only one"):
        load_inventory(netbox="https://a", nautobot="https://b", env={})
    path = tmp_path / "inv.yaml"
    path.write_text("devices: []\n")
    with pytest.raises(InventoryError, match="--filter needs"):
        load_inventory(path, filters={"site": "x"}, env={})


def test_example_inventory_loads():
    from pathlib import Path
    path = Path(__file__).parent.parent / "topologies" / "live_devices_example.yaml"
    inv = load_inventory(path, env={"PROD_NET_PASSWORD": "x"})
    assert len(inv.devices) == 5
    assert all(d["password"] == "x" for d in inv.devices)
    assert inv.devices[4]["username"] == "fwadmin"
    assert inv.kind_rules[0].values == {"kind": "cisco_iol", "type": "L2"}
    assert first_match(inv.image_rules, {"kind": "cisco_iol", "type": "L2"}).index == 1
    assert first_match(inv.image_rules, {"kind": "cisco_iol", "version": "17.12.2"}).index == 2
    assert first_match(inv.image_rules, {"kind": "cisco_iol", "version": "15.9"}).index == 3


def test_rules_match_whole_values_case_insensitively():
    rules = parse_rules([
        {"platform": "ios", "model": "C9[23]00.*", "kind": "cisco_iol", "type": "L2"},
        {"platform": "ios", "kind": "cisco_iol"},
        {"group": "spines", "kind": "arista_ceos"},
        {"tag": "clab-.*", "kind": "linux"},
    ], "kinds", ("kind", "type"))
    assert first_match(rules, {"platform": "eos", "tag": ["prod", "clab-host"]}).index == 4
    assert first_match(rules, {"platform": "ios", "model": "c9300-48P"}).index == 1
    assert first_match(rules, {"platform": "IOS", "model": "ISR4451"}).index == 2
    assert first_match(rules, {"platform": "iosxr"}) is None        # no partial match
    assert first_match(rules, {"platform": "eos", "group": ["dc1", "spines"]}).index == 3
    assert first_match(rules, {"platform": "eos", "model": None}) is None


def test_rule_errors():
    with pytest.raises(InventoryError, match="needs 'image'"):
        parse_rules([{"kind": "cisco_iol"}], "images", ("image", "kind"))
    with pytest.raises(InventoryError, match="unknown field 'colour'"):
        parse_rules([{"colour": "red", "image": "x"}], "images", ("image", "kind"))
    with pytest.raises(InventoryError, match="bad regex"):
        parse_rules([{"model": "(", "image": "x"}], "images", ("image", "kind"))
    # In images:, kind is something to match on
    rule = parse_rules([{"kind": "cisco_iol", "image": "iol:1"}], "images", ("image", "kind"))[0]
    assert rule.matches({"kind": "cisco_iol"}) and not rule.matches({"kind": "linux"})


# --- token and device-address safety ------------------------------------------

LOCAL_PORTS = range(8731, 8736)


class _Recorder(http.server.BaseHTTPRequestHandler):
    """Answers every GET with ``self.server.reply(handler)``; records the requests."""

    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("Authorization")))
        status, headers, body = self.server.reply(self)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def servers(monkeypatch):
    """Start local HTTP servers on 127.0.0.1 (ports 8731-8735); yields a factory."""
    monkeypatch.setenv("no_proxy", "*")
    started = []

    def start(reply):
        for port in LOCAL_PORTS:
            try:
                srv = http.server.HTTPServer(("127.0.0.1", port), _Recorder)
            except OSError:
                continue
            srv.seen, srv.reply = [], reply
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            started.append(srv)
            return srv
        pytest.skip("no free port in 8731-8735")

    yield start
    for srv in started:
        srv.shutdown()
        srv.server_close()


def _json(data):
    return 200, {"Content-Type": "application/json"}, json.dumps(data).encode()


def test_redirects_are_refused_and_never_get_the_token(servers):
    other = servers(lambda h: _json({"next": None, "results": [{"stolen": True}]}))
    target = f"http://localhost:{other.server_port}/api/dcim/devices/"
    nb = servers(lambda h: (302, {"Location": target}, b""))
    client = inventory.RestClient(f"http://127.0.0.1:{nb.server_port}", TOKEN, allow_http=True)
    with pytest.raises(InventoryError, match="HTTP 302.*redirects are not followed") as exc:
        client.get_all("/api/dcim/devices/")
    assert TOKEN not in str(exc.value)
    assert nb.seen and nb.seen[0][1] == f"Token {TOKEN}"
    assert other.seen == []                    # the token never reached the other host


def test_real_opener_fetches_pages_from_the_base_url(servers):
    def reply(handler):
        if "page=2" in handler.path:
            return _json({"next": None, "results": [{"id": 2}]})
        # A proxy-terminated NetBox links to http:// and its internal port
        return _json({"next": "http://127.0.0.1:1/api/x/?limit=200&page=2",
                      "results": [{"id": 1}]})
    nb = servers(reply)
    client = inventory.RestClient(f"http://127.0.0.1:{nb.server_port}/", TOKEN,
                                  allow_http=True)
    assert client.get_all("/api/x/") == [{"id": 1}, {"id": 2}]
    assert [p for p, _ in nb.seen] == ["/api/x/?limit=200", "/api/x/?limit=200&page=2"]


@pytest.mark.parametrize("nxt", [
    "https://evil.example.com/api/dcim/devices/?page=2",
    "https://netbox.example.com@evil.example.com/api/dcim/devices/?page=2",
    "file:///etc/passwd",
    "ftp://netbox.example.com/x",
])
def test_pagination_links_to_other_hosts_are_refused(env, monkeypatch, nxt):
    requests = []

    def opener(req, **kw):
        requests.append(req.full_url)
        return io.BytesIO(json.dumps({"next": nxt, "results": []}).encode())
    monkeypatch.setattr(inventory, "urlopen", opener)
    with pytest.raises(InventoryError, match="refusing pagination link"):
        load_inventory(netbox="https://netbox.example.com", env=env)
    assert len(requests) == 1


def test_pagination_link_downgrade_is_rewritten_to_the_base_url(monkeypatch):
    requests = []

    def opener(req, **kw):
        requests.append(req.full_url)
        nxt = None if "page=2" in req.full_url else \
            "http://NetBox.example.com:8080/api/dcim/devices/?limit=200&page=2"
        return io.BytesIO(json.dumps({"next": nxt, "results": [1]}).encode())
    monkeypatch.setattr(inventory, "urlopen", opener)
    client = inventory.RestClient("https://netbox.example.com:8443", TOKEN)
    assert client.get_all("/api/dcim/devices/") == [1, 1]
    assert requests[1] == "https://netbox.example.com:8443/api/dcim/devices/?limit=200&page=2"


def test_plain_http_needs_opt_in_and_warns(env, monkeypatch):
    with pytest.raises(InventoryError, match="unencrypted.*allow-http"):
        load_inventory(netbox="http://netbox.example.com", env=env)
    with pytest.raises(InventoryError, match="not an http"):
        load_inventory(netbox="file:///etc/passwd", env=env)
    with pytest.raises(InventoryError, match="not in the URL"):
        load_inventory(netbox="https://user:pw@netbox.example.com", env=env)
    monkeypatch.setattr(inventory, "urlopen", FakeAPI({}))
    inv = load_inventory(netbox="http://netbox.example.com", env=env, allow_http=True)
    assert any("unencrypted" in w for w in inv.warnings)
    assert load_inventory(netbox="https://netbox.example.com", env=env).warnings == []


def test_verify_tls_false_and_allow_http_in_the_file_warn(tmp_path, env, monkeypatch):
    monkeypatch.setattr(inventory, "urlopen", FakeAPI({}))
    path = tmp_path / "inv.yaml"
    path.write_text(yaml.safe_dump({"source": {"type": "netbox", "url": "https://nb",
                                               "verify_tls": False}}))
    inv = load_inventory(path, env=env)
    assert any("verification is off" in w for w in inv.warnings)
    path.write_text(yaml.safe_dump({"source": {"type": "netbox", "url": "http://nb",
                                               "allow_http": True}}))
    assert any("unencrypted" in w for w in load_inventory(path, env=env).warnings)


def test_allowed_networks(tmp_path, monkeypatch):
    resolved = {"rtr.example.com": ["10.1.0.5"], "evil.example.com": ["10.1.0.6", "203.0.113.7"]}
    monkeypatch.setattr(inventory, "resolve", lambda host: resolved.get(host, []))
    path = tmp_path / "inv.yaml"
    path.write_text(yaml.safe_dump({
        "allowed_networks": ["10.1.0.0/16"],
        "defaults": {"username": "u", "password": "p", "platform": "ios"},
        "devices": [{"hostname": h} for h in ("10.1.2.3", "198.51.100.9", "rtr.example.com",
                                              "evil.example.com", "gone.example.com",
                                              "fd00::1")],
    }))
    inv = load_inventory(path, env={})
    assert [d["hostname"] for d in inv.devices] == ["10.1.2.3", "rtr.example.com"]
    (warning,) = inv.warnings
    assert "left out 4 device(s)" in warning and "198.51.100.9" in warning
    assert "203.0.113.7" in warning and "does not resolve" in warning
    # --allowed-network adds to the file's list
    inv = load_inventory(path, env={}, allowed_networks=["198.51.100.0/24", "fd00::/8"])
    assert len(inv.devices) == 4
    with pytest.raises(InventoryError, match="not a network"):
        load_inventory(path, env={}, allowed_networks=["10.0.0.300/8"])


def test_allowed_networks_stop_a_repointed_netbox_device(env, monkeypatch):
    api = FakeAPI({"/api/dcim/devices/": [[_nb_device("leaf1", "10.0.0.1", "arista-eos"),
                                            _nb_device("leaf2", "203.0.113.66", "arista-eos")]]})
    monkeypatch.setattr(inventory, "urlopen", api)
    inv = load_inventory(netbox="https://nb", env=env, allowed_networks=["10.0.0.0/8"])
    assert [d["name"] for d in inv.devices] == ["leaf1"]
    assert any("leaf2" in w and "203.0.113.66" in w for w in inv.warnings)


def test_host_ranges_are_capped_and_checked():
    assert inventory._expand_range("h[1:3]") == ["h1", "h2", "h3"]
    with pytest.raises(InventoryError, match="too large"):
        inventory._expand_range("h[0:999999999]")
    with pytest.raises(InventoryError, match="too large"):
        inventory._expand_range("h[0:99]x[0:99]")
    with pytest.raises(InventoryError, match="bad host range"):
        inventory._expand_range("h[1:a]")


def test_device_strings_are_printable(env, monkeypatch):
    api = FakeAPI({"/api/dcim/devices/": [[_nb_device("x\x1b]0;pwned\x07", "10.0.0.1",
                                                       "vendor-\x1b[2J")]]})
    monkeypatch.setattr(inventory, "urlopen", api)
    inv = load_inventory(netbox="https://nb", env=env)
    (warning,) = inv.warnings
    assert "\x1b" not in warning and "\x07" not in warning
    assert r"x\x1b]0;pwned\x07" in warning
    assert inventory.printable("café ‮") == "café \\u202e"
