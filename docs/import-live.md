# Import from live network

```bash
pip install -e ".[napalm]"

export PROD_NET_PASSWORD=...          # read via password_env in the inventory
clabfleet export-live topologies/live_devices_example.yaml \
    -o imported/prod_mirror.clab.yml --lab-name prod-mirror --sanitise

clabfleet validate imported/prod_mirror.clab.yml
clabfleet --sudo deploy imported/prod_mirror.clab.yml
```

Each device becomes a node with its running config saved to
`configs/<node>.cfg`. Each LLDP adjacency between two inventoried devices
becomes a link. Next to the topology, `<name>.import-report.yaml` lists,
per node, every interface rename, where the image came from and what
`--sanitise` changed (counts only, never values). Without `-o` the
topology is printed with the configs inline.

Without `--sanitise` the configs are saved as they are, with the
network's passwords, keys and SNMP communities, and export-live says so
on stderr. Either way `configs/` is created readable by you only (0700),
and the configs, the topology and the report are written 0600.

## Device sources

| Source | How |
|--------|-----|
| clabfleet YAML | `export-live inventory.yaml` with a `devices:` list; see `topologies/live_devices_example.yaml` |
| NetBox | `--netbox URL` (token in `NETBOX_TOKEN`), or `source: {type: netbox, url: ...}` in the YAML |
| Nautobot | `--nautobot URL` (token in `NAUTOBOT_TOKEN`), or `source: {type: nautobot, ...}` |
| Ansible | `export-live hosts.ini` (or `hosts.yml`), `--ansible FILE`, or `source: {type: ansible, path: ...}` |

- **NetBox / Nautobot:** `--filter KEY=VALUE` (repeatable; the same key
  twice means either value) is passed to `/api/dcim/devices/`, e.g.
  `--filter site=dc1 --filter role=leaf --filter tag=lab-import`
  (Nautobot: `location=`). All pages are fetched. Each device is reached on
  its primary IP (else its name). The NAPALM driver comes from
  `source.platform_map` (platform slug, or name on Nautobot → driver), else
  the platform's NAPALM driver field, else a guess from the platform name
  (`ios`, `eos`, `nxos`, `iosxr`, `junos`, ...); devices with no driver are
  skipped with a warning. Use `--token-env VAR` or `source.token_env` for
  another token variable. The token is only sent to the URL you give:
  it must be `https://` (a plain `http://` URL needs `--allow-http` or
  `source.allow_http: true`, and warns), redirects are not followed, and
  pagination links to another host are refused (links to `http://` on the
  same host, as a NetBox behind a TLS proxy returns, are fetched from your
  URL). `source.verify_tls: false` turns certificate checks off, with a
  warning.
- **Ansible:** YAML or INI static inventories, including `[group:vars]`,
  `[group:children]`, host ranges such as `leaf[01:04]`, and `group_vars/`
  and `host_vars/` next to the file. Variables merge like Ansible's (`all`,
  then parent groups, then child groups, then the host). Uses
  `ansible_host`, `ansible_user`, `ansible_password` (or `ansible_ssh_pass`),
  `ansible_port`, `ansible_become_password` (NAPALM's enable secret) and
  `ansible_network_os` (`cisco.ios.ios` → `ios`, `arista.eos.eos` → `eos`,
  `cisco.nxos.nxos` → `nxos_ssh`, ...; `napalm_platform` overrides it).
  Optional `clab_kind`, `clab_type` and `clab_image` host variables set the
  node directly. `--filter group=NAME` keeps one group. Vault-encrypted
  values are not decrypted.
- **Credentials:** the device entry, then `defaults:` in the YAML, then the
  variable named by `password_env` / `username_env`, then
  `CLABFLEET_DEVICE_USERNAME` / `CLABFLEET_DEVICE_PASSWORD`. Passwords and
  tokens are never logged or written to the output.
- **Allowed networks:** the device login usually works on every device,
  so whoever can edit a device's address (in NetBox, say) could point it
  at a machine of theirs and collect the password. `allowed_networks:`
  in the inventory (a list of CIDRs), or `--allowed-network CIDR`
  (repeatable), leaves out every device whose address, or any address its
  name resolves to, is outside them, and lists them in a warning.
- **Device identity:** NAPALM's drivers do not all check who they talk
  to. For `ios`, `iosxr` and `nxos_ssh` (SSH through Netmiko) export-live
  sets `system_host_keys: true`, so a device in your `~/.ssh/known_hosts`
  whose host key changed is refused; add `ssh_strict: true` to
  `optional_args` to refuse unknown devices too (connect once with `ssh`
  first). The `eos` https transport and the `junos` NETCONF driver do not
  verify the device by default: run imports from a management network you
  trust and use `allowed_networks`. See `topologies/live_devices_example.yaml`.

## Kinds and images

A node's kind is the device's `kind`, else the first matching `kinds:`
rule in the inventory, else the platform default (`ios` → `cisco_iol`,
`eos` → `arista_ceos`, `nxos` → `cisco_n9kv`, `junos` →
`juniper_vjunosrouter`, ...). Its image is the device's `image`, else the
first matching `images:` rule, else `REPLACE-ME/<kind>:latest` with a
warning. Rules match regexes against the whole value, ignoring case, on
`platform`, `vendor`, `model`, `version` (from the device's NAPALM facts),
`role`, `site`, `tag` (NetBox/Nautobot), `group` (Ansible), `hostname`, `name`, and
for images also `kind` and `type`:

```yaml
kinds:
  - {platform: ios, model: "C9[23]00.*", kind: cisco_iol, type: L2}
images:
  - {kind: cisco_iol, version: '17\.12\..*', image: "vrnetlab/cisco_iol:17.12.01"}
  - {kind: arista_ceos, image: "ceos:4.32.0F"}
```

## Interface names

Interface names are mapped to the names each kind accepts, in the links
and in the saved configs (`interface` stanzas and references such as
`passive-interface` or `source-interface`; descriptions are left alone).
Abbreviations are expanded first (`Gi0/1`, `Te1/1`, `Et49/1`, ...), so
both ends of an adjacency agree.

| Kind | Topology name | Config name | 1:1 when the device has |
|------|---------------|-------------|-------------------------|
| `cisco_iol` | `Ethernet0/1` … `Ethernet15/3` (63 ports; `0/0` is management) | same | an Ethernet port whose last two numbers fit, e.g. `Gi0/0/2` → `Ethernet0/2` |
| `arista_ceos` | `eth1`, `eth49_1` | `Ethernet1`, `Ethernet49/1` | `EthernetN`, `EthernetN/M` |
| `cisco_n9kv` | `eth5` | `Ethernet1/5` | `Ethernet1/N` |
| `nokia_srlinux` | `e1-3` | `ethernet-1/3` | `ethernet-1/N` |
| `cisco_xrd` | `Gi0-0-0-2` | `GigabitEthernet0/0/0/2` | any `…0/0/0/N` port |
| `cisco_xrv9k` | `eth3` | `GigabitEthernet0/0/0/2` | any `…0/0/0/N` port |
| `juniper_vjunos*`, `juniper_vsrx` | `eth4` | `ge-0/0/3` (vJunosEvolved `et-`) | `ge-`/`xe-`/`et-0/0/N` (vJunos router/switch: 10 ports) |
| `cisco_c8000v`, `cisco_csr1000v` | `eth2` | `GigabitEthernet3` | `GigabitEthernetN` |
| `nokia_sros` | `eth4` | `1/1/4` | `1/1/N` |
| `linux` and others | `eth1`, `eth2`, … | not rewritten | `EthernetN` |

Other interfaces get the lowest free port: link interfaces first, then
the other physical interfaces in the config, each in natural order, so the
result does not depend on the order devices report them in. Management
ports and logical interfaces (loopbacks, VLANs, port-channels, tunnels,
subinterfaces) are never mapped. Interfaces beyond a kind's port count
are listed as dropped in the report: their links are left out and their
IOS-style config stanzas removed. `--keep-interface-names` turns mapping
off; names must then be 1-64 letters, digits and `_ . / : -`, and links
with other names (LLDP data comes from the neighbour) are left out with a
warning. A port is only ever used by one link: when the LLDP data of the
two ends disagrees, the second link is left out with a warning.

## Sanitising configs

`--sanitise` cleans every saved config, for IOS/IOS-XE, IOS-XR, NX-OS, EOS
and Junos (with it, configs of other platforms are not saved at all):

- **Removed:** `enable secret/password`, all `username` lines, `aaa ...`,
  TACACS+/RADIUS servers and keys, `snmp-server community/user/host`,
  `snmp mib community-map`, `crypto pki` / `crypto ca` trustpoints and
  certificate chains, `key config-key`, PEM private keys, banners,
  comments, `snmp-server location/contact`. Junos: `tacplus-server`,
  `radius-server`, `authentication-order`, SNMP communities, SNMPv3,
  location and contact, SSH public keys, local certificates, login
  messages, comments.
- **Replaced with `lab-key`:** OSPF/OSPFv3/IS-IS/BGP/HSRP/VRRP/GLBP/NHRP/
  PIM/BFD authentication keys (hex keys get a hex placeholder of the right
  length), key-chain `key-string`, NTP authentication keys, ISAKMP, IKEv2
  and EzVPN pre-shared keys, WPA PSKs, passwords in URLs
  (`tftp://user:...@host`), `event manager environment` values,
  `--token`/`--password` options of EOS daemons, line and other
  `password`/`secret` values, Junos `$9$`/`$8$` secrets and every quoted
  value of a statement Junos marks `## SECRET-DATA`. Both ends get the
  same value, so authenticated adjacencies still form. Descriptions and
  remarks that mention a password, key or community lose their text.
- **Management:** addresses on management interfaces (`Management*`,
  `mgmt0`, `fxp0`, `em0`, or any interface in a management VRF), static
  routes in the management VRF or via the management subnet, and the
  Junos management routing instance are removed; `--mgmt-address dhcp`
  turns the addresses into DHCP instead, `keep` leaves them.
- **Login:** a placeholder user (`--lab-user` / `--lab-password`, default
  `admin`/`admin`) is added after `hostname` so the node stays reachable.
  Junos keeps its users, but every `encrypted-password` becomes the hash of
  `admin@123` (the vrnetlab default).
- **Report:** a sanitised import report leaves out the devices' addresses
  and the LLDP names of neighbour placeholders.

Sanitising is best effort: configs keep secrets in more places than any
list of commands covers. So every sanitised config is then checked for
anything that still looks like one (a `password`, `key`, `secret`,
`community`, `key-string` ... followed by something other than the
placeholder, crypt hashes such as `$1$` or `$9$`, private keys, passwords
in URLs, `SECRET-DATA` marks, long hex keys). If any config is flagged,
nothing is written and export-live lists the node, line and kind of
secret (never the value); fix or check those lines, or add
`--allow-residual` to write the configs anyway (the report keeps the
findings). Review the configs before you share them. Node names are the
devices' hostnames and placeholders are named after their LLDP system
names: these are not anonymised.

A re-sync with `--apply` or `--overwrite` over a sanitised import must
sanitise again: without `--sanitise` it is refused, unless you pass
`--no-sanitise` to write the configs as they are.

## Neighbours outside the inventory

`--include-neighbours` adds LLDP neighbours that are not in the inventory
(servers, devices you cannot log in to, ...) as `linux` nodes
(`--neighbour-image`, default `alpine:3`), named after their LLDP system
name or chassis ID, with their links. The report marks them
`neighbour: true`.

## Re-sync

Running `export-live` again with the same `-o` file does not overwrite
it. It reports what changed since the last import:

```
Changes since the last import of imported/prod_mirror.clab.yml:
- node server-01
~ node core-rtr-02: startup config changed
+ link core-rtr-02:Ethernet0/3 -- dist-sw-02:eth51_1
~ link dist-sw-01:eth49_1 -- dist-sw-02:eth50_1 -> dist-sw-01:eth48_1 -- dist-sw-02:eth50_1

Nothing written. Re-run with --apply to update the topology (add --prune to delete what is gone), or --overwrite to replace it.
```

`--json` prints the same as JSON. `--apply` edits the file in place:
comments, order, GUI positions and other labels, and nodes and links you
added by hand all stay. New nodes and links are added, moved links get
their new interfaces, and changed nodes get their new kind/type/image (an
image you set by hand is never replaced by a placeholder) and config. A
node whose `startup-config` you pointed at another file keeps it; the new
config goes to `configs/<node>.cfg` for you to compare. Removed nodes and
links are only deleted with `--prune`, and only nodes an import created
count as removed. Interface assignments from the last report are reused,
so a new port does not renumber the others. Lines that change on every
fetch (such as `! Last configuration change`) are not a config change.
`--overwrite` replaces the file with a fresh import.
