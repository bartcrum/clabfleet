"""Recorded NAPALM data for four devices and a fake ``napalm`` module.

core-rtr-01 (IOS) -- core-rtr-02 (IOS)    Gi0/0/1 both ends
core-rtr-01 Gi0/0/2 -- dist-sw-01 Ethernet1
core-rtr-01 Te1/1 -- dist-sw-02 Ethernet49/1
core-rtr-02 Gi0/0/2 -- dist-sw-01 Ethernet2
dist-sw-01 Ethernet49/1 -- dist-sw-02 Ethernet50/1
dist-sw-01 Ethernet10 -- server-01 (not in the inventory)
dist-sw-01 Management1 -- oob-sw (management, never a link)
"""

import copy
import sys
import types
from pathlib import Path

CONFIGS = Path(__file__).parent / "fixtures" / "live" / "configs"


def _n(system, port, chassis="00:00:00:00:00:01"):
    return {"remote_system_name": system, "remote_port": port,
            "remote_chassis_id": chassis, "remote_port_description": ""}


SMALL_IOS = """hostname core-rtr-02
!
username netops privilege 15 secret 9 $9$R2SECRET0901$abc
!
interface GigabitEthernet0/0/1
 description to core-rtr-01
 ip address 10.0.12.2 255.255.255.252
!
interface GigabitEthernet0/0/2
 description to dist-sw-01
 ip address 10.0.15.1 255.255.255.252
!
snmp-server community R2COMM0902 RO
end
"""
SMALL_EOS = """hostname dist-sw-02
username admin privilege 15 role network-admin secret sha512 $6$SW2HASH0903$x
interface Ethernet49/1
   no switchport
   ip address 10.0.14.2/30
interface Ethernet50/1
   no switchport
   ip address 10.0.23.0/31
end
"""

DEVICES = {
    "core-rtr-01.example.com": {
        "facts": {"hostname": "core-rtr-01", "fqdn": "core-rtr-01.example.com",
                  "vendor": "Cisco", "model": "C8300-1N1S-6T", "os_version": "17.12.1a"},
        "config": (CONFIGS / "ios.cfg").read_text(),
        "lldp": {
            "Gi0/0/1": [_n("core-rtr-02.example.com", "GigabitEthernet0/0/1")],
            "GigabitEthernet0/0/2": [_n("dist-sw-01", "Ethernet1")],
            "TenGigabitEthernet1/1": [_n("dist-sw-02.example.com", "Ethernet49/1")],
        },
    },
    "core-rtr-02.example.com": {
        "facts": {"hostname": "core-rtr-02", "fqdn": "core-rtr-02.example.com",
                  "vendor": "Cisco", "model": "C8300-1N1S-6T", "os_version": "17.9.4"},
        "config": SMALL_IOS,
        "lldp": {
            "GigabitEthernet0/0/1": [_n("core-rtr-01.example.com", "Gi0/0/1")],
            "GigabitEthernet0/0/2": [_n("dist-sw-01.example.com", "Ethernet2")],
        },
    },
    "dist-sw-01.example.com": {
        "facts": {"hostname": "dist-sw-01", "fqdn": "dist-sw-01.example.com",
                  "vendor": "Arista", "model": "DCS-7050SX3-48YC8", "os_version": "4.32.0F"},
        "config": (CONFIGS / "eos.cfg").read_text(),
        "lldp": {
            "Ethernet1": [_n("core-rtr-01.example.com", "Gi0/0/2")],
            "Ethernet2": [_n("core-rtr-02", "Gi0/0/2")],
            "Ethernet49/1": [_n("dist-sw-02", "Ethernet50/1")],
            "Ethernet10": [_n("server-01.example.com", "ens3", "52:54:00:aa:bb:cc")],
            "Management1": [_n("oob-sw", "Gi1/0/1")],
        },
    },
    "dist-sw-02.example.com": {
        "facts": {"hostname": "dist-sw-02", "fqdn": "dist-sw-02.example.com",
                  "vendor": "Arista", "model": "DCS-7050SX3-48YC8", "os_version": "4.31.2F"},
        "config": SMALL_EOS,
        "lldp": {
            "Ethernet49/1": [_n("core-rtr-01", "TenGigabitEthernet1/1")],
            "Ethernet50/1": [_n("dist-sw-01.example.com", "Ethernet49/1")],
        },
    },
}

INVENTORY = [
    {"hostname": host, "platform": "ios" if "rtr" in host else "eos",
     "username": "netops", "password": "PROD-PASSWORD-0999"}
    for host in DEVICES
]


class FakeDevice:
    opened: list = []

    def __init__(self, hostname, username, password, optional_args=None, data=None):
        self.hostname = hostname
        self.password = password
        self.data = data

    def open(self):
        if self.data is None:
            raise ConnectionError(f"cannot reach {self.hostname}")
        FakeDevice.opened.append(self.hostname)

    def close(self):
        pass

    def get_facts(self):
        return copy.deepcopy(self.data["facts"])

    def get_config(self):
        return {"running": self.data["config"], "startup": "", "candidate": ""}

    def get_lldp_neighbors_detail(self):
        return copy.deepcopy(self.data["lldp"])


def install(monkeypatch, devices=None):
    """Make ``import napalm`` return a fake driven by ``devices`` (default DEVICES)."""
    devices = DEVICES if devices is None else devices
    FakeDevice.opened = []

    def get_network_driver(platform):
        def driver(hostname, username, password, optional_args=None):
            return FakeDevice(hostname, username, password, optional_args,
                              devices.get(hostname))
        return driver

    module = types.ModuleType("napalm")
    module.get_network_driver = get_network_driver
    monkeypatch.setitem(sys.modules, "napalm", module)
    return module
