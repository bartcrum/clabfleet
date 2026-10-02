import re
from pathlib import Path

import pytest

from clabfleet.sanitise import (
    JUNOS_LAB_HASH,
    SanitiseOptions,
    residual_secrets,
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
        "ntp": 1, "other": 2, "mgmt": 2, "comments": 1, "info": 1,
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
                 " key 1", "snmp-server enable traps",
                 "router bgp 65000", " ip address 10.0.12.1 255.255.255.252",
                 "vrf definition Mgmt-vrf", " transport input ssh"):
        assert keep in lines, keep
    # Whole blocks removed: certificate data, AAA groups, TACACS servers
    assert "CERTDATA" not in out and "quit" not in out
    assert "aaa" not in out and "tacacs" not in out.lower() and "radius" not in out
    assert "ip route vrf Mgmt-vrf" not in out
    # Production details: location, comments naming who changed what
    assert "snmp-server location" not in out and "by netops" not in out


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
    assert "community" not in out and "location DC1;" not in out
    assert "address 10.0.31.1/30;" in out          # data-plane address kept
    assert out.count("{") == out.count("}")         # blocks stay balanced
    assert report.counts == {"users": 3, "aaa": 2, "ntp": 1, "keys": 2, "snmp": 1, "mgmt": 1,
                             "comments": 1, "info": 1}
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


# Secrets that earlier versions let through. Each holds LEAK<n>, or a hex key.
LEAKS = [
    ("eos", "interface Ethernet1\n   ip ospf message-digest-key 1 sha256 7 LEAK01\n"),
    ("eos", "interface Vlan10\n   vrrp 1 peer authentication text LEAK02\n"),
    ("eos", "daemon agent\n   exec /usr/bin/agent --token LEAK03 --verbose\n   no shutdown\n"),
    ("eos", "SNMP-SERVER COMMUNITY LEAK04 RO\n"),
    ("ios", "interface Gi1\n standby 1 authentication md5 key-string 7 LEAK05 timeout 30\n"),
    ("ios", "interface Tunnel0\n ip nhrp authentication LEAK06\n"),
    ("ios", "interface Gi2\n glbp 1 authentication text LEAK07\n"),
    ("ios", "interface Gi3\n ipv6 ospf authentication ipsec spi 256 sha1 "
            "0123456789ABCDEF0123456789ABCDEF01234567\n"),
    ("ios", "interface Gi4\n ospfv3 authentication ipsec spi 500 md5 7 LEAK09\n"),
    ("ios", "interface Gi5\n ospfv3 encryption ipsec spi 300 esp aes-cbc 128 "
            "0123456789ABCDEF0123456789ABCDEF sha1 0123456789ABCDEF0123456789ABCDEF01234567\n"),
    ("ios", "crypto isakmp client configuration group EZ\n key LEAK11\n pool P\n"),
    ("ios", "crypto ipsec client ezvpn E\n group G key LEAK12\n"),
    ("ios", "crypto ikev2 profile P\n authentication local pre-share key LEAK13\n"),
    ("ios", "router ospf 1\n area 1 virtual-link 10.0.0.2 message-digest-key 1 md5 LEAK14\n"
            " area 2 virtual-link 10.0.0.3 authentication-key LEAK15\n"),
    ("ios", "snmp mib community-map LEAK16 engineid 0102\n"),
    ("ios", "dot11 ssid LAB\n wpa-psk ascii 0 LEAK17\n"),
    ("ios", "wlan W 1 W\n security wpa psk set-key ascii 0 LEAK18\n"),
    ("ios", "key chain K\n key 1\n  key-string LEAK19 with trailing words\n"),
    ("ios", "archive\n path tftp://backup:LEAK20@10.0.0.9/cfg\n"),
    ("ios", "event manager environment API_TOKEN LEAK21\n"),
    ("ios", "banner motd ^C\nWiFi password is LEAK22\n^C\nhostname r1\n"),
    ("ios", "! old password was LEAK23\nhostname r1\n"),
    ("ios", "interface Gi6\n description vty password LEAK24\n"),
    ("iosxr", "router ospf 1\n area 0\n  interface Gi0/0/0/0\n"
              "   message-digest-key 1 md5 encrypted LEAK25\n"),
    ("iosxr", "router isis 1\n lsp-password hmac-md5 encrypted LEAK26\n"
              " interface Gi0/0/0/1\n  hello-password hmac-md5 encrypted LEAK27\n"),
    ("iosxr", "router hsrp\n interface Gi0/0/0/2\n  address-family ipv4\n   hsrp 1\n"
              "    authentication LEAK28\n"),
    ("iosxr", "banner motd ;secret LEAK29;\nhostname xr\n"),
    ("nxos", "interface Vlan10\n  hsrp 1\n    authentication text LEAK30\n"
             "  vrrp 2\n    authentication text LEAK31\n"),
    ("nxos", "interface Ethernet1/1\n  bfd authentication keyed-sha1 key-id 1 hex-key LEAK32\n"),
    ("nxos", "interface Ethernet1/2\n  ip pim hello-authentication ah-md5 3 LEAK33\n"),
    ("eos", "banner login\nroot password LEAK34\nEOF\nhostname sw\n"),
    ("junos", 'protocols {\n    ospf {\n        area 0 {\n            interface ge-0/0/0 {\n'
              '                authentication {\n                    simple-password '
              '"$8$LEAK35"; ## SECRET-DATA\n                }\n            }\n        }\n'
              '    }\n}\n'),
    ("junos", 'security {\n    certificates {\n        local {\n            c1 {\n'
              '                "-----BEGIN RSA PRIVATE KEY-----\nLEAK36\n'
              '-----END RSA PRIVATE KEY-----\n-----BEGIN CERTIFICATE-----\nMIIB\n'
              '-----END CERTIFICATE-----\n"; ## SECRET-DATA\n            }\n        }\n    }\n}\n'),
    ("junos", 'protocols {\n    isis { level 2 { authentication-key "$9$LEAK37"; } }\n}\n'),
    ("junos", 'protocols {\n    rsvp {\n        interface ge-0/0/1 {\n'
              '            new-auth-thing "LEAK38"; ## SECRET-DATA\n        }\n    }\n}\n'),
    ("junos", 'system {\n    login {\n        message "the root password is LEAK39";\n    }\n}\n'),
    ("junos", 'set protocols ospf area 0 interface ge-0/0/0 authentication simple-password '
              '"$8$LEAK40"\n'),
    ("junos", 'set system services ssh "LEAK41\nstill LEAK41"  ## SECRET-DATA\n'),
]


@pytest.mark.parametrize("platform,text", LEAKS, ids=lambda v: v[:30] if "\n" in v else v)
def test_leaked_secrets_are_removed(platform, text):
    out, report = sanitise_config(text, platform)
    assert "LEAK" not in out and "0123456789ABCDEF" not in out, out
    assert residual_secrets(out) == [], out
    assert "LEAK" not in str(report.as_dict())


def _leak(snippet):
    return next(text for _, text in LEAKS if snippet in text)


def test_replacements_keep_the_rest_of_the_line():
    out, _ = sanitise_config(_leak("LEAK05"), "ios", SanitiseOptions(user=""))
    assert " standby 1 authentication md5 key-string lab-key timeout 30" in out
    out, _ = sanitise_config(_leak("LEAK20"), "ios", SanitiseOptions(user=""))
    assert " path tftp://backup:lab-key@10.0.0.9/cfg" in out
    out, _ = sanitise_config(_leak("spi 256"), "ios", SanitiseOptions(user=""))
    hex_key = "lab-key".encode().hex()
    assert f"sha1 {(hex_key * 3)[:40]}" in out           # still a valid 40-digit key
    out, _ = sanitise_config(_leak("LEAK03"), "eos", SanitiseOptions(user=""))
    assert "exec /usr/bin/agent --token lab-key --verbose" in out
    out, _ = sanitise_config(_leak("LEAK37"), "junos")       # single-line nested blocks
    assert out == ('protocols {\n    isis {\n        level 2 {\n'
                   '            authentication-key "lab-key";\n        }\n    }\n}\n')


def test_residual_check_flags_what_sanitising_missed():
    planted = ("hostname r1\n"
               "interface Gi1\n"
               " ip frobnicate authentication text PLANTED1\n"
               " widget-password PLANTED2\n"
               "crypto vault enc $1$PLANTED3\n"
               "archive url https://u:PLANTED4@backup\n"
               "-----BEGIN EC PRIVATE KEY-----\n"
               "snmp-server community PLANTED5 RO\n"
               "ospfv3 authentication ipsec spi 300 md5 0123456789ABCDEF0123456789ABCDEF\n")
    found = residual_secrets(planted)
    assert found == [(3, "authentication"), (4, "widget-password"), (5, "crypt-hash"),
                     (6, "url-credentials"), (7, "private-key"), (8, "community"),
                     (9, "hex-key"), (9, "spi")]
    assert "PLANTED" not in str(found)
    # The placeholders inserted while sanitising are not flagged
    assert residual_secrets("username admin password admin\n key-string lab-key\n"
                            f' encrypted-password "{JUNOS_LAB_HASH}";\n') == []
    assert residual_secrets(" key-string s3cr3t\n", SanitiseOptions(key="s3cr3t")) == []


@pytest.mark.parametrize("name,platform", [
    ("ios.cfg", "ios"), ("eos.cfg", "eos"), ("nxos.cfg", "nxos_ssh"),
    ("junos.cfg", "junos"), ("junos_set.cfg", "junos"),
])
@pytest.mark.parametrize("mgmt", ["remove", "dhcp", "keep"])
def test_residual_check_passes_sanitised_fixtures(name, platform, mgmt):
    text = (CONFIGS / name).read_text()
    assert residual_secrets(text)               # the raw config is flagged
    out, _ = sanitise_config(text, platform, SanitiseOptions(mgmt=mgmt))
    assert residual_secrets(out) == []


def test_free_text_that_mentions_secrets_is_cleared():
    out, _ = sanitise_config("interface Gi1\n description uplink to core\n"
                             "ip access-list extended A\n 10 remark psk is xyz\n", "ios",
                             SanitiseOptions(user=""))
    assert " description uplink to core" in out
    assert " 10 remark (removed by clabfleet)" in out


def test_banners_are_removed():
    text = ("hostname r1\nbanner exec ^C\nline one\nline two ^C\nbanner login #one line#\n"
            "banner motd ^CAuthorised use only^C\ninterface Gi1\n shutdown\n")
    out, report = sanitise_config(text, "ios", SanitiseOptions(user=""))
    assert out == "hostname r1\ninterface Gi1\n shutdown\n"
    assert report.counts["info"] == 3


def test_mgmt_routes_via_the_mgmt_subnet():
    ios = ("hostname r1\ninterface GigabitEthernet0/0\n vrf forwarding Mgmt-vrf\n"
           " ip address 192.0.2.10 255.255.255.0\n!\n"
           "ip route 198.51.100.0 255.255.255.0 192.0.2.1\nip route 10.0.0.0 255.0.0.0 10.0.12.2\n")
    out, _ = sanitise_config(ios, "ios", SanitiseOptions(user=""))
    assert "192.0.2.1" not in out and "ip route 10.0.0.0 255.0.0.0 10.0.12.2" in out
    junos = ("system {\n    management-instance;\n}\ninterfaces {\n    fxp0 {\n        unit 0 {\n"
             "            family inet {\n                address 192.0.2.41/24;\n            }\n"
             "        }\n    }\n}\nrouting-options {\n    static {\n"
             "        route 198.51.100.0/24 next-hop 192.0.2.1;\n"
             "        route 0.0.0.0/0 {\n            next-hop 192.0.2.1;\n"
             "            no-readvertise;\n        }\n"
             "        route 10.0.0.0/8 next-hop 10.0.31.2;\n    }\n}\n"
             "routing-instances {\n    mgmt_junos {\n        routing-options {\n"
             "            static {\n                route 0.0.0.0/0 next-hop 192.0.2.1;\n"
             "            }\n        }\n    }\n}\n")
    out, report = sanitise_config(junos, "junos")
    assert "192.0.2." not in out and "management-instance" not in out
    assert "mgmt_junos" not in out and "no-readvertise" not in out
    assert "route 10.0.0.0/8 next-hop 10.0.31.2;" in out
    assert out.count("{") == out.count("}")
    out, _ = sanitise_config(junos, "junos", SanitiseOptions(mgmt="keep"))
    assert "mgmt_junos" in out and "route 0.0.0.0/0 {" in out
    out, _ = sanitise_config(junos.replace("system {\n    management-instance;\n}\n", "")
                             .replace("routing-options {\n    static", "routing-options {\n"
                                      "    static"), "junos", SanitiseOptions(mgmt="dhcp"))
    assert out.count("dhcp;") == 1


def test_junos_long_lines_are_linear():
    import time
    text = "system {\n    host-name a" + " " * 200_000 + "b;\n}\n"
    start = time.monotonic()
    sanitise_config(text, "junos")
    residual_secrets(text)
    assert time.monotonic() - start < 2
