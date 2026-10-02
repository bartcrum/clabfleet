import re
from pathlib import Path

import pytest

from clabfleet.sanitise import (
    JUNOS_LAB_HASH,
    SanitiseOptions,
    sanitise_config,
)

CONFIGS = Path(__file__).parent / "fixtures" / "live" / "configs"
# Every secret in the fixtures carries a 4-digit marker: <WORD>0001 ... <WORD>0408
SECRET_MARKER = re.compile(r"[A-Z]+0[0-4]\d\d\b|\$[0-9]\$[A-Za-z]*0[0-4]\d\d")


def _sanitise(name, platform, **opts):
    text = (CONFIGS / name).read_text()
    out, report = sanitise_config(text, platform, SanitiseOptions(**opts))
    return text, out, report


@pytest.mark.parametrize("name,platform", [
    ("ios.cfg", "ios"), ("eos.cfg", "eos"), ("nxos.cfg", "nxos_ssh"),
    ("junos.cfg", "junos"), ("junos_set.cfg", "junos"),
])
@pytest.mark.parametrize("mgmt", ["remove", "dhcp"])
def test_no_secret_survives(name, platform, mgmt):
    text, out, report = _sanitise(name, platform, mgmt=mgmt)
    assert SECRET_MARKER.findall(text), "fixture lost its markers"
    assert SECRET_MARKER.findall(out) == []
    assert "192.0.2." not in out          # management addresses
    assert "198.51.100.2" not in out      # TACACS/RADIUS servers
    # The report counts changes but never echoes values
    assert not SECRET_MARKER.findall(str(report.as_dict()))


def test_ios_categories_and_kept_config():
    _, out, report = _sanitise("ios.cfg", "ios")
    assert report.counts == {
        "enable": 1, "users": 2, "aaa": 8, "snmp": 4, "crypto": 2, "keys": 7,
        "ntp": 1, "other": 2, "mgmt": 2,
    }
    lines = out.splitlines()
    # Placeholder login right after hostname
    assert lines[lines.index("hostname core-rtr-01") + 1] == \
        "username admin privilege 15 secret 0 admin"
    # Same placeholder on both ends keeps authenticated adjacencies working
    assert " ip ospf message-digest-key 1 md5 lab-key" in lines
    assert " ip ospf authentication-key lab-key" in lines
    assert " neighbor 10.0.12.2 password lab-key" in lines
    assert " standby 1 authentication md5 key-string lab-key" in lines
    assert "  key-string lab-key" in lines
    assert "crypto isakmp key lab-key address 203.0.113.9" in lines
    assert "ntp authentication-key 1 md5 lab-key" in lines
    # Not over-matched
    for keep in ("service password-encryption", " ip ospf authentication message-digest",
                 "ntp trusted-key 1", "ntp server 198.51.100.30 key 1", "key chain OSPF-KEYS",
                 " key 1", "snmp-server location DC1", "snmp-server enable traps",
                 "router bgp 65000", " ip address 10.0.12.1 255.255.255.252",
                 "vrf definition Mgmt-vrf", " transport input ssh"):
        assert keep in lines, keep
    # Whole blocks removed: certificate data, AAA groups, TACACS servers
    assert "CERTDATA" not in out and "quit" not in out
    assert "aaa" not in out and "tacacs" not in out.lower() and "radius" not in out
    assert "ip route vrf Mgmt-vrf" not in out


def test_mgmt_modes():
    _, out, _ = _sanitise("ios.cfg", "ios", mgmt="dhcp")
    assert "interface GigabitEthernet0/0\n description OOB\n vrf forwarding Mgmt-vrf\n" \
           " ip address dhcp\n" in out
    _, out, report = _sanitise("ios.cfg", "ios", mgmt="keep")
    assert " ip address 192.0.2.10 255.255.255.0" in out
    assert "ip route vrf Mgmt-vrf 0.0.0.0 0.0.0.0 192.0.2.1" in out
    _, out, _ = _sanitise("eos.cfg", "eos", mgmt="dhcp")
    assert "interface Management1\n   vrf MGMT\n   ip address dhcp\n" in out
    _, out, _ = _sanitise("nxos.cfg", "nxos", mgmt="remove")
    assert "interface mgmt0\n  vrf member management\n\n" in out
    assert "vrf context management\n\n" in out  # its static route is gone
    with pytest.raises(ValueError):
        sanitise_config("hostname x\n", "ios", SanitiseOptions(mgmt="bogus"))


def test_eos_and_nxos_logins():
    _, out, _ = _sanitise("eos.cfg", "eos", user="lab", password="lab123")
    assert "hostname dist-sw-01\nusername lab privilege 15 secret lab123\n" in out
    assert "sshkey" not in out and "aaa root" not in out
    _, out, report = _sanitise("nxos.cfg", "nxos_ssh")
    assert "no password strength-check\nusername admin password admin role network-admin" in out
    assert "feature tacacs+" not in out and "feature ospf" in out
    assert "    password lab-key" in out   # BGP neighbour password, NX-OS style
    assert report.counts["users"] == 2


def test_iosxr_login_block():
    text = ("hostname xr1\nusername ops\n group root-lr\n secret 10 $6$XRSECRET0501$x\n!\n"
            "router ospf 1\n area 0\n  interface Gi0/0/0/0\n   authentication-key encrypted "
            "XRKEY0502\n")
    out, report = sanitise_config(text, "iosxr")
    assert "XRSECRET" not in out and "XRKEY" not in out
    assert "username admin\n group root-lr\n group cisco-support\n secret 0 admin\n" in out
    assert "   authentication-key lab-key" in out


def test_junos_curly():
    _, out, report = _sanitise("junos.cfg", "junos")
    assert out.count(f'encrypted-password "{JUNOS_LAB_HASH}";') == 2
    assert 'authentication-key "lab-key";' in out
    assert 'md5 1 key "lab-key";' in out
    assert 'authentication-key 1 type md5 value "lab-key";' in out
    assert "ssh-rsa" not in out and "tacplus" not in out and "authentication-order" not in out
    assert "community" not in out and "location DC1;" in out
    assert "address 10.0.31.1/30;" in out          # data-plane address kept
    assert out.count("{") == out.count("}")         # blocks stay balanced
    assert report.counts == {"users": 3, "aaa": 2, "ntp": 1, "keys": 2, "snmp": 1, "mgmt": 1}
    assert "admin@123" in " ".join(report.notes)


def test_junos_mgmt_dhcp_and_set_style():
    _, out, _ = _sanitise("junos.cfg", "junos", mgmt="dhcp")
    assert "    fxp0 {\n        unit 0 {\n            family inet {\n                dhcp;\n" in out
    _, out, report = _sanitise("junos_set.cfg", "junos", mgmt="dhcp")
    assert out.splitlines() == [
        "set version 23.2R1.14",
        "set system host-name edge-02",
        f'set system root-authentication encrypted-password "{JUNOS_LAB_HASH}"',
        "set system login user netops class super-user",
        f'set system login user netops authentication encrypted-password "{JUNOS_LAB_HASH}"',
        "set interfaces ge-0/0/1 unit 0 family inet address 10.0.32.1/30",
        "set interfaces fxp0 unit 0 family inet dhcp",
        'set protocols bgp group ibgp authentication-key "lab-key"',
    ]


def test_unsupported_platform_returns_none():
    out, report = sanitise_config("config system admin\n set password ENC xyz\n", "fortios")
    assert out is None
    assert report.dialect == "unsupported"


def test_generic_rule_does_not_over_match():
    text = ("hostname r\nservice password-encryption\npassword encryption aes\n"
            "security passwords min-length 8\nno service password-recovery\n"
            "ip ssh version 2\nlogin block-for 60 attempts 3 within 30\n"
            "standby 1 authentication md5 key-chain HSRP-CHAIN\n"
            "ppp chap password 7 PPPSECRET0601\n")
    out, report = sanitise_config(text, "ios", SanitiseOptions(user=""))
    assert out.splitlines() == [
        "hostname r", "service password-encryption", "password encryption aes",
        "security passwords min-length 8", "no service password-recovery",
        "ip ssh version 2", "login block-for 60 attempts 3 within 30",
        "standby 1 authentication md5 key-chain HSRP-CHAIN",
        "ppp chap password lab-key",
    ]
    assert dict(report.counts) == {"other": 1}
