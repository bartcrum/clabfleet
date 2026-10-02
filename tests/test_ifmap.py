import random

import pytest

from clabfleet import ifmap
from clabfleet.ifmap import (
    canonical,
    config_interfaces,
    identity_map,
    is_logical,
    is_management,
    is_physical,
    map_interfaces,
    rewrite_config,
)


@pytest.mark.parametrize("name,expected", [
    ("Gi0/0/1", "GigabitEthernet0/0/1"),
    ("gi0/1", "GigabitEthernet0/1"),
    ("Giga0/1", "GigabitEthernet0/1"),
    ("GigabitEthernet 0/1", "GigabitEthernet0/1"),
    ("Te1/1", "TenGigabitEthernet1/1"),
    ("Twe1/0/1", "TwentyFiveGigE1/0/1"),
    ("Fo1/0/49", "FortyGigabitEthernet1/0/49"),
    ("Hu0/0/0/1", "HundredGigE0/0/0/1"),
    ("Fa0/1", "FastEthernet0/1"),
    ("Et49/1", "Ethernet49/1"),
    ("Eth1/5", "Ethernet1/5"),
    ("Po10", "Port-channel10"),
    ("Gi0/1.100", "GigabitEthernet0/1.100"),
    ("ge-0/0/3", "ge-0/0/3"),
    ("ethernet-1/2", "ethernet-1/2"),
    ("1/1/3", "1/1/3"),
    ("aabb.cc00.0100", "aabb.cc00.0100"),
])
def test_canonical_expands_abbreviations(name, expected):
    assert canonical(name) == expected


def test_interface_classes():
    for name in ("Management1", "mgmt0", "Ma1", "fxp0", "em0", "me0"):
        assert is_management(name) and not is_physical(name), name
    for name in ("Loopback0", "Vlan10", "Po1", "Gi0/1.100", "ae0", "Bundle-Ether1", "irb"):
        assert not is_physical(name), name
    assert is_logical("Vlan10") and is_logical("Gi0/1.100")
    # Unrecognised port IDs (Linux names, MACs) are usable as link ends
    assert not is_logical("ens3") and not is_logical("aabb.cc00.0100")
    for name in ("Gi0/0/1", "Ethernet49/1", "xe-0/0/1", "ethernet-1/1", "1/1/1"):
        assert is_physical(name), name


def test_iol_native_then_sequential():
    m = map_interfaces("cisco_iol", ["Gi0/0/1", "GigabitEthernet0/0/2", "Te1/1", "Gi0/0",
                                     "Gi1/0/24"])
    assert m.report() == {
        "GigabitEthernet0/0": "Ethernet0/3",      # 0/0 is IOL's management port
        "GigabitEthernet0/0/1": "Ethernet0/1",    # last two numbers as slot/port
        "GigabitEthernet0/0/2": "Ethernet0/2",
        "GigabitEthernet1/0/24": "Ethernet1/0",   # port 24 does not exist on IOL
        "TenGigabitEthernet1/1": "Ethernet1/1",
    }
    assert m.config_names["GigabitEthernet0/0/1"] == "Ethernet0/1"


def test_native_collision_falls_back_to_next_free_port():
    m = map_interfaces("cisco_iol", ["Gi0/0/1", "Te0/0/1"])
    assert m.endpoint("Gi0/0/1") == "Ethernet0/1"
    assert m.endpoint("Te0/0/1") == "Ethernet0/2"


def test_mapping_is_deterministic_whatever_the_input_order():
    names = [f"GigabitEthernet1/0/{i}" for i in range(1, 30)] + ["Te1/1/1", "Gi0/0/2"]
    first = map_interfaces("cisco_iol", names).report()
    for _ in range(5):
        shuffled = names[:]
        random.shuffle(shuffled)
        assert map_interfaces("cisco_iol", shuffled).report() == first


def test_link_interfaces_get_ports_before_config_only_ones():
    m = map_interfaces("cisco_iol", ["Gi1/0/40"], config_ifaces=["Gi1/0/1", "Gi1/0/2"])
    assert m.endpoint("Gi1/0/40") == "Ethernet0/1"
    assert m.endpoint("Gi1/0/2") == "Ethernet0/2"   # native
    assert m.endpoint("Gi1/0/1") == "Ethernet0/3"   # its native port went to the link


def test_port_limit_drops_the_rest():
    names = [f"GigabitEthernet1/0/{i}" for i in range(1, 71)]
    m = map_interfaces("cisco_iol", names)
    assert len(m.endpoints) == 63
    assert m.dropped == [f"GigabitEthernet1/0/{i}" for i in range(64, 71)]
    assert "Ethernet0/0" not in m.endpoints.values()
    assert m.endpoint("Gi1/0/63") == "Ethernet15/3"


def test_ceos_keeps_arista_numbering():
    m = map_interfaces("arista_ceos", ["Ethernet1", "Et49/1", "Gi0/1", "Ethernet2"])
    assert m.report() == {"Ethernet1": "eth1", "Ethernet2": "eth2", "Ethernet49/1": "eth49_1",
                          "GigabitEthernet0/1": "eth3"}
    assert m.config_names["Ethernet49/1"] == "Ethernet49/1"
    assert m.config_names["GigabitEthernet0/1"] == "Ethernet3"
    assert ifmap.naming_for("ceos") is ifmap.naming_for("arista_ceos")


@pytest.mark.parametrize("kind,original,endpoint,config", [
    ("cisco_n9kv", "Ethernet1/5", "eth5", "Ethernet1/5"),
    ("cisco_n9kv", "Ethernet2/1", "eth1", "Ethernet1/1"),
    ("nokia_srlinux", "ethernet-1/3", "e1-3", "ethernet-1/3"),
    ("nokia_srlinux", "Gi0/1", "e1-1", "ethernet-1/1"),
    ("cisco_xrd", "GigabitEthernet0/0/0/2", "Gi0-0-0-2", "GigabitEthernet0/0/0/2"),
    ("cisco_xrd", "Hu0/0/0/1", "Gi0-0-0-1", "GigabitEthernet0/0/0/1"),
    ("cisco_xrv9k", "Gi0/0/0/2", "eth3", "GigabitEthernet0/0/0/2"),
    ("juniper_vjunosrouter", "ge-0/0/3", "eth4", "ge-0/0/3"),
    ("juniper_vjunosrouter", "xe-0/0/1", "eth2", "ge-0/0/1"),
    ("juniper_vjunosevolved", "et-0/0/0", "eth1", "et-0/0/0"),
    ("cisco_c8000v", "GigabitEthernet3", "eth2", "GigabitEthernet3"),
    ("cisco_csr1000v", "Gi0/0/1", "eth1", "GigabitEthernet2"),
    ("nokia_sros", "1/1/4", "eth4", "1/1/4"),
    ("linux", "Ethernet3", "eth3", "eth3"),
    ("linux", "ens3", "eth1", "eth1"),
    ("some_unknown_kind", "Gi0/1", "eth1", "eth1"),
])
def test_kind_naming(kind, original, endpoint, config):
    m = map_interfaces(kind, [original])
    assert m.endpoint(original) == endpoint
    assert m.config_names[canonical(original)] == config


def test_vjunos_port_limit():
    m = map_interfaces("juniper_vjunosrouter", [f"ge-0/0/{i}" for i in range(12)])
    assert m.endpoint("ge-0/0/9") == "eth10"
    assert m.dropped == ["ge-0/0/10", "ge-0/0/11"]


def test_management_and_logical_interfaces_are_not_mapped():
    m = map_interfaces("arista_ceos", ["Management1", "Ethernet1"],
                       config_ifaces=["Loopback0", "Vlan10", "Port-Channel1", "Ethernet1.10"])
    assert m.report() == {"Ethernet1": "eth1"}


def test_pinned_assignments_survive_new_interfaces():
    first = map_interfaces("cisco_iol", ["Gi1/0/5", "Gi1/0/9"])
    assert first.report() == {"GigabitEthernet1/0/5": "Ethernet0/1",
                              "GigabitEthernet1/0/9": "Ethernet0/2"}
    # A new port that sorts first would shift everything without the pins
    second = map_interfaces("cisco_iol", ["Gi1/0/1", "Gi1/0/5", "Gi1/0/9"],
                            pinned=first.report())
    assert second.endpoint("Gi1/0/5") == "Ethernet0/1"
    assert second.endpoint("Gi1/0/9") == "Ethernet0/2"
    assert second.endpoint("Gi1/0/1") == "Ethernet0/3"
    # Pins that are invalid for the kind or no longer present are ignored
    third = map_interfaces("cisco_iol", ["Gi1/0/1"],
                           pinned={"Gi1/0/1": "Ethernet99/9", "Gi1/0/7": "Ethernet0/1"})
    assert third.endpoint("Gi1/0/1") == "Ethernet0/1"


def test_identity_map_keeps_names():
    m = identity_map("cisco_iol", ["Gi0/0/1"])
    assert m.endpoint("GigabitEthernet0/0/1") == "Gi0/0/1"
    assert rewrite_config("interface Gi0/0/1\n", m) == "interface Gi0/0/1\n"


IOS = """hostname r1
interface GigabitEthernet0/0/1
 description uplink to Gi0/0/2 on core
 ip address 10.0.0.1 255.255.255.0
interface GigabitEthernet0/0/1.100
 encapsulation dot1Q 100
interface GigabitEthernet0/0/10
 shutdown
interface GigabitEthernet0/0/2
 shutdown
interface TenGigabitEthernet1/1
 channel-group 1 mode active
router ospf 1
 passive-interface Gi0/0/2
 passive-interface GigabitEthernet0/0/10
ip route 0.0.0.0 0.0.0.0 GigabitEthernet0/0/1 10.0.0.254
ip flow-export source Gi0/0/1
"""


def test_rewrite_config_renames_everywhere_in_one_pass():
    m = map_interfaces("cisco_iol", ["Gi0/0/1", "Gi0/0/2", "Te1/1"],
                       config_ifaces=config_interfaces(IOS))
    # Gi0/0/10 → next free port; Gi0/0/2 → Ethernet0/2 is not renamed again
    assert m.endpoint("Gi0/0/10") == "Ethernet0/3"
    out = rewrite_config(IOS, m)
    assert "interface Ethernet0/1\n" in out
    assert "interface Ethernet0/1.100\n" in out
    assert "interface Ethernet0/3\n" in out
    assert "interface Ethernet1/1\n" in out
    assert " passive-interface Ethernet0/2\n" in out
    assert " passive-interface Ethernet0/3\n" in out
    assert "0.0.0.0 Ethernet0/1 10.0.0.254" in out
    assert "ip flow-export source Ethernet0/1" in out
    # Descriptions are free text and are left alone
    assert " description uplink to Gi0/0/2 on core\n" in out
    assert "GigabitEthernet" not in out.replace("Gi0/0/2 on core", "")


def test_rewrite_does_not_touch_longer_names_or_numbers():
    m = map_interfaces("arista_ceos", ["Gi0/1"])
    text = ("interface Gi0/1\ninterface Gi0/10\ninterface Gi0/1/0\n"
            "ip address 10.0.0.1/24\nvlan 0/1\nntp server 1.1.1.1\n")
    out = rewrite_config(text, m)
    assert out.splitlines() == ["interface Ethernet1", "interface Gi0/10", "interface Gi0/1/0",
                                "ip address 10.0.0.1/24", "vlan 0/1", "ntp server 1.1.1.1"]


def test_rewrite_drops_stanzas_of_dropped_interfaces():
    names = [f"Gi1/0/{i}" for i in range(1, 66)]
    text = "".join(f"interface GigabitEthernet1/0/{i}\n shutdown\n" for i in range(1, 66))
    text += "router ospf 1\n network 0.0.0.0 255.255.255.255 area 0\n"
    m = map_interfaces("cisco_iol", names, config_interfaces(text))
    out = rewrite_config(text, m)
    assert out.count("interface ") == 63
    assert "GigabitEthernet1/0/64" not in out and "GigabitEthernet1/0/65" not in out
    assert out.endswith("router ospf 1\n network 0.0.0.0 255.255.255.255 area 0\n")


def test_rewrite_junos_config():
    text = """interfaces {
    xe-0/0/1 {
        description "to ge-0/0/1 of peer";
        unit 0 {
            family inet {
                address 10.0.0.1/30;
            }
        }
    }
}
protocols {
    ospf {
        area 0.0.0.0 {
            interface xe-0/0/1.0;
        }
    }
}
"""
    assert config_interfaces(text) == ["xe-0/0/1"]
    m = map_interfaces("juniper_vjunosrouter", ["xe-0/0/1"], config_interfaces(text))
    out = rewrite_config(text, m)
    assert "    ge-0/0/1 {" in out
    assert "interface ge-0/0/1.0;" in out
    assert "xe-0/0/1" not in out


def test_config_interfaces_set_style():
    text = "set interfaces ge-0/0/1 unit 0 family inet address 10.0.0.1/30\n" \
           "set interfaces ge-0/0/1 description x\nset interfaces fxp0 unit 0\n"
    assert config_interfaces(text) == ["ge-0/0/1", "fxp0"]


def test_unsupported_kind_leaves_config_alone():
    m = map_interfaces("linux", ["Gi0/1"])
    assert rewrite_config("interface Gi0/1\n", m) == "interface Gi0/1\n"
