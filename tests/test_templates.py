import ipaddress
import re

import pytest
import yaml

from clabfleet import cli
from clabfleet.templates import KINDS, TEMPLATES, TemplateError, generate, render
from clabfleet.topology import load_topology, topology_from_dict
from clabfleet.validate import validate_topology

SIZES = {
    "spine-leaf": {"spines": 2, "leaves": 5},   # leaves > 4 exercise IOL slot 1
    "ring": {"nodes": 5},
    "campus": {"core": 2, "dist": 3, "access": 5},
}


def _addresses(data: dict, kind: str) -> dict[tuple[str, str], ipaddress.IPv4Interface]:
    """(node, interface as used in links) → address, read back from the rendered configs."""
    result = {}
    for name, node in data["topology"]["nodes"].items():
        if kind == "linux":
            for cmd in node["exec"]:
                addr, dev = re.fullmatch(r"ip addr add (\S+) dev (\S+)", cmd).groups()
                result[(name, dev)] = ipaddress.IPv4Interface(addr)
            continue
        iface = None
        for line in node["startup-config"].splitlines():
            if line.startswith("interface "):
                iface = line.split()[1]
            elif line.startswith(" ip address ") and iface:
                parts = line.split()[2:]
                addr = parts[0] if len(parts) == 1 else f"{parts[0]}/{parts[1]}"
                if kind == "arista_ceos":
                    iface = iface.replace("Ethernet", "eth")
                result[(name, iface)] = ipaddress.IPv4Interface(addr)
    return result


@pytest.mark.parametrize("kind", list(KINDS))
@pytest.mark.parametrize("template", list(TEMPLATES))
def test_every_template_and_kind_validates(tmp_path, template, kind):
    params = SIZES[template]
    data = generate(template, params, kind=kind)
    path = tmp_path / "lab.clab.yml"
    path.write_text(render(data, template, params, kind))

    report = validate_topology(path)
    assert report.ok and not report.warnings, (report.errors, report.warnings)
    topo = load_topology(path)
    assert topo.name == TEMPLATES[template].default_name
    assert all(link.is_p2p for link in topo.links)
    assert topo.data["topology"]["kinds"][kind]["image"] == KINDS[kind].image
    assert path.read_text().startswith(f"# {TEMPLATES[template].description} ({kind})")

    addrs = _addresses(data, kind)
    link_addrs = [a for (_, iface), a in addrs.items() if a.network.prefixlen == 31]
    assert len(link_addrs) == 2 * len(topo.links)
    every_ip = [a.ip for a in addrs.values()]
    assert len(every_ip) == len(set(every_ip)), "duplicate IP"
    for link in topo.links:
        a, b = (addrs[(ep.node, ep.interface)] for ep in link.endpoints)
        assert a.network == b.network and a.network.prefixlen == 31
        assert a.ip != b.ip


def test_spine_leaf_bgp_and_naming():
    data = generate("spine-leaf", {"spines": 2, "leaves": 3}, kind="arista_ceos")
    nodes = data["topology"]["nodes"]
    assert list(nodes) == ["Spine-1", "Spine-2", "Leaf-1", "Leaf-2", "Leaf-3"]
    assert data["topology"]["links"][0] == {"endpoints": ["Spine-1:eth1", "Leaf-1:eth1"]}
    assert data["topology"]["links"][3] == {"endpoints": ["Spine-2:eth1", "Leaf-1:eth2"]}

    spine = nodes["Spine-1"]["startup-config"]
    assert "username admin privilege 15 secret admin" in spine
    assert "interface Loopback0\n ip address 10.255.0.1/32" in spine
    assert "interface Ethernet1\n description to-Leaf-1\n no switchport\n" \
           " ip address 10.0.0.0/31" in spine
    assert "router bgp 65000" in spine
    assert " neighbor 10.0.0.1 remote-as 65001" in spine
    assert " neighbor 10.0.0.5 remote-as 65003" in spine

    leaf = nodes["Leaf-2"]["startup-config"]
    assert "interface Loopback0\n ip address 10.255.1.2/32" in leaf
    assert "router bgp 65002" in leaf
    assert " maximum-paths 2" in leaf
    assert " neighbor 10.0.0.2 remote-as 65000" in leaf  # Spine-1 end of link 1
    assert " network 10.255.1.2/32" in leaf


def test_iol_interface_names_and_syntax():
    data = generate("campus", {"core": 2, "dist": 1, "access": 4}, kind="cisco_iol",
                    image="my/iol:1")
    topo = data["topology"]
    assert topo["kinds"] == {"cisco_iol": {"image": "my/iol:1"}}
    # Dist-1: two core uplinks, then four access ports → 0/1-0/3, 1/0-1/2
    dist_ports = [ep.split(":")[1] for link in topo["links"] for ep in link["endpoints"]
                  if ep.startswith("Dist-1:")]
    assert dist_ports == ["Ethernet0/1", "Ethernet0/2", "Ethernet0/3",
                          "Ethernet1/0", "Ethernet1/1", "Ethernet1/2"]
    cfg = topo["nodes"]["Dist-1"]["startup-config"]
    assert "interface Ethernet1/0\n description to-Access-2\n" in cfg
    assert " ip address 10.255.1.1 255.255.255.255" in cfg
    assert " ip address 10.0.0.3 255.255.255.254" in cfg
    assert " network 10.0.0.2 0.0.0.1 area 0" in cfg
    assert cfg.rstrip().endswith("end")


def test_campus_spreads_access_over_dist():
    data = generate("campus", {"core": 1, "dist": 2, "access": 4}, kind="linux")
    uplinks = [tuple(ep.split(":")[0] for ep in link["endpoints"])
               for link in data["topology"]["links"] if "Access" in link["endpoints"][1]]
    assert uplinks == [("Dist-1", "Access-1"), ("Dist-1", "Access-2"),
                       ("Dist-2", "Access-3"), ("Dist-2", "Access-4")]


def test_ring_closes_and_uses_custom_subnets():
    data = generate("ring", {"nodes": 3}, kind="linux", name="tri",
                    link_subnet="192.168.0.0/24", loopback_subnet="172.16.0.0/16")
    assert data["name"] == "tri"
    links = [link["endpoints"] for link in data["topology"]["links"]]
    assert links == [["R1:eth1", "R2:eth1"], ["R2:eth2", "R3:eth1"], ["R3:eth2", "R1:eth2"]]
    assert data["topology"]["nodes"]["R1"]["exec"] == [
        "ip addr add 172.16.0.1/32 dev lo",
        "ip addr add 192.168.0.0/31 dev eth1",
        "ip addr add 192.168.0.5/31 dev eth2",
    ]
    ceos = generate("ring", {"nodes": 3})["topology"]["nodes"]["R2"]["startup-config"]
    assert " ip ospf network point-to-point" in ceos
    assert " network 10.0.0.2/31 area 0.0.0.0" in ceos


@pytest.mark.parametrize("args, message", [
    (("mesh", {}), "Unknown template 'mesh'"),
    (("ring", {"nodes": 2}), "--nodes must be between 3 and"),
    (("ring", {"spines": 2}), "has no option(s) spines"),
    (("ring", {}, "juniper_crpd"), "Unsupported kind"),
])
def test_bad_parameters(args, message):
    with pytest.raises(TemplateError, match=re.escape(message)):
        generate(*args)


def test_bad_subnets():
    with pytest.raises(TemplateError, match="too small for this many links"):
        generate("spine-leaf", {"spines": 2, "leaves": 4}, link_subnet="10.0.0.0/29")
    with pytest.raises(TemplateError, match="overlaps"):
        generate("ring", link_subnet="10.0.0.0/8", loopback_subnet="10.255.0.0/16")
    with pytest.raises(TemplateError, match="Invalid link subnet"):
        generate("ring", link_subnet="10.0.0.1/24")
    with pytest.raises(TemplateError, match="too small for 2 tiers"):
        generate("spine-leaf", loopback_subnet="10.255.0.0/24")


def test_cli_new_writes_and_refuses_overwrite(tmp_path, capsys):
    out = tmp_path / "labs" / "fabric.clab.yml"
    argv = ["new", "spine-leaf", "--spines", "1", "--leaves", "2", "--kind", "linux",
            "--name", "fab", "-o", str(out)]
    assert cli.main(argv) == 0
    assert "lab 'fab', 3 linux nodes, 2 links" in capsys.readouterr().out
    text = out.read_text()
    assert "#   clabfleet new spine-leaf --spines 1 --leaves 2 --asn 65000 --kind linux" in text
    assert topology_from_dict(yaml.safe_load(text)).name == "fab"

    out.write_text("keep me")
    assert cli.main(argv) == 1
    assert "already exists" in capsys.readouterr().err
    assert out.read_text() == "keep me"
    assert cli.main(argv + ["--force"]) == 0
    assert out.read_text() == text


def test_cli_new_prints_and_lists(capsys):
    assert cli.main(["new", "ring", "--nodes", "3", "--kind", "cisco_iol"]) == 0
    data = yaml.safe_load(capsys.readouterr().out)
    assert list(data["topology"]["nodes"]) == ["R1", "R2", "R3"]

    assert cli.main(["new", "--list"]) == 0
    listing = capsys.readouterr().out
    for name in (*TEMPLATES, *KINDS, "--spines", "--access"):
        assert name in listing
    assert cli.main(["new"]) == 2


# --- configs for a graph drawn in the GUI builder ------------------------------------

from clabfleet.templates import generate_configs, port_index  # noqa: E402

DRAWN_NODES = {"R1": "arista_ceos", "R2": "cisco_iol", "R3": "arista_ceos", "H1": "linux",
               "Sw": "nokia_srlinux"}
DRAWN_LINKS = [("R1", "eth1", "R2", "Ethernet0/1"), ("R2", "Ethernet0/2", "R3", "eth1"),
               ("R1", "eth3", "R3", "eth2"), ("R1", "eth4", "H1", "eth1"),
               ("R3", "eth5", "Sw", "e1-1")]


def test_port_index_inverts_the_namers():
    assert port_index("cisco_iol", "Ethernet1/2") == 6 and port_index("cisco_iol", "eth1") is None
    assert port_index("arista_ceos", "eth12") == 12 and port_index("linux", "Ethernet1") is None
    for kind, spec in KINDS.items():
        assert all(port_index(kind, spec.interface(i)[0]) == i for i in range(1, 20))


def test_drawn_graph_gets_working_bgp_configs(tmp_path):
    configs, skipped = generate_configs(DRAWN_NODES, DRAWN_LINKS, "bgp", asn=65100)
    assert skipped == ["Sw"] and set(configs) == {"R1", "R2", "R3", "H1"}
    r1 = configs["R1"]["startup-config"]
    # Its own AS, sessions only with routers, the host's subnet announced
    assert "router bgp 65100" in r1 and "interface Ethernet3\n" in r1 and "interface Ethernet4\n" in r1
    assert r1.count("remote-as") == 2 and "remote-as 65101" in r1 and "remote-as 65102" in r1
    h1 = configs["H1"]["exec"]
    gw = next(c.split()[-1] for c in h1 if c.startswith("ip route replace default via"))
    host_net = next(line.split()[1] for line in r1.splitlines() if line.startswith(" network ")
                    and not line.endswith("/32"))
    assert ipaddress.ip_address(gw) in ipaddress.ip_network(host_net)
    assert "router bgp 65101" in configs["R2"]["startup-config"]  # IOL

    # Written into a topology with the builder, the routing view finds it all paired
    from clabfleet.gui.editing import apply_graph
    from clabfleet.routing import routing_view
    from clabfleet.topology import load_topology
    text = "name: drawn\ntopology:\n  nodes:\n" + "".join(
        f"    {n}: {{kind: {k}, image: img}}\n" for n, k in DRAWN_NODES.items()) + "  links: []\n"
    graph = {"nodes": [{"name": n, "kind": k, **({"config": configs[n]} if n in configs else {})}
                       for n, k in DRAWN_NODES.items()],
             "links": [{"a": f"{a}:{ai}", "b": f"{b}:{bi}"} for a, ai, b, bi in DRAWN_LINKS]}
    (tmp_path / "drawn.clab.yml").write_text(apply_graph(text, graph))
    view = routing_view(load_topology(tmp_path / "drawn.clab.yml"))
    sessions = view["bgp"]["sessions"]
    assert len(sessions) == 3 and all(s["configured"] == "both" for s in sessions)
    assert view["problems"] == []


def test_drawn_graph_gets_working_ospf_configs(tmp_path):
    configs, _ = generate_configs(DRAWN_NODES, DRAWN_LINKS, "ospf")
    r1 = configs["R1"]["startup-config"]
    assert "router ospf 1" in r1 and r1.count(" area 0.0.0.0") == 4  # loopback + 3 links
    assert "network 10.255.1.1" not in r1  # hosts sit in the next loopback tier
    assert configs["H1"]["exec"][0] == "ip addr add 10.255.1.1/32 dev lo"


@pytest.mark.parametrize("links, error", [
    ([("R1", "Ethernet0/1", "R2", "Ethernet0/1")], "R1: Ethernet0/1 is not a arista_ceos data port"),
    ([("R1", "eth1", "R2", "Ethernet0/1"), ("R1", "eth1", "H1", "eth1")], "used by two links"),
])
def test_drawn_graph_errors(links, error):
    with pytest.raises(TemplateError, match=error):
        generate_configs(DRAWN_NODES, links, "bgp")
    with pytest.raises(TemplateError, match="ospf or bgp"):
        generate_configs(DRAWN_NODES, [], "rip")
