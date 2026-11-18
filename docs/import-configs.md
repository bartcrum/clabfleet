# Import from config files (Arista AVD)

Build a lab from EOS config files alone, such as the
`intended/configs/*.cfg` that [Arista AVD](https://avd.arista.com) writes.
No device is contacted: each file becomes a cEOS node with the file as its
startup config, and the links are worked out from the configs.

```bash
clabfleet import-configs avd/intended/configs/ \
    -o labs/dc1/dc1.clab.yml --lab-name dc1

clabfleet validate labs/dc1/dc1.clab.yml
clabfleet routing labs/dc1/dc1.clab.yml      # the BGP/EVPN design, read from the configs
clabfleet --sudo deploy labs/dc1/dc1.clab.yml
```

In the [web GUI](gui.md): **Import** in the sidebar (or the *Import
configs* card on the start page), drop the files or a folder of them on
the dialog, name the lab, **Import**. The lab is written to
`<lab>/<lab>.clab.yml` in the first workspace directory, in a folder of
its own, and the dialog lists what the import had to say before it opens
the lab. An existing folder of that name is never overwritten. Importing
needs the operator role.

Give `import-configs` files, directories (their `*.cfg`, `*.conf` and
`*.txt` files), or both. A node is named after the `hostname` in its
config, else after the file. Next to the topology it writes
`configs/<node>.cfg` and `<name>.import-report.yaml`; without `-o` it
prints the topology with the configs inline. It refuses to overwrite an
existing output file without `--force`.

## How links are found

There is no LLDP to read, so links come from the configs:

1. **Interface descriptions.** AVD describes every fabric port with its
   peer and the peer's port: `P2P_dc1-spine1_Ethernet1`,
   `MLAG_dc1-leaf1b_Ethernet3`, `L2_dc1-leaf1c_Ethernet1` (AVD 4 and
   older: `P2P_LINK_TO_DC1-SPINE1_Ethernet1`). A description on an
   Ethernet port counts when it holds **the hostname of another imported
   config, a separator, and an Ethernet port name**, whatever else is
   around it and in any letter case, so customised descriptions work as
   long as they keep those two. `to dc1-spine1 Et1` and
   `uplink: DC1-SPINE1:Ethernet1` are read the same way.
2. **Addresses**, as a cross-check and a fallback. The two ends of a
   routed link share a /31 or /30. A described link whose two addresses
   are in different subnets is kept, with a warning. Two routed ports that
   share a /31 or /30 and have no usable description become a link too.

When the two ends disagree:

| What the configs say | Result |
|----------------------|--------|
| Both ends name each other | Link |
| One end names the other, whose port has no description naming a device | Link, with a warning |
| One end names a port that the other config describes as facing a third device | No link, with a warning |
| The description names a device whose config was not given (a server, or a switch you left out) | No link; the port is listed under `ports_without_peer` in the report |
| Three or more ports in one /31 or /30 | No link from the addresses, with a warning |

Servers and other endpoints are left out: their switch ports stay in the
configs, with nothing plugged in. Port-channels need nothing special,
since their member Ethernet ports carry the descriptions.

The report lists every link with where it came from (`descriptions`,
`description` for one end only, or `addresses`) and its subnet when both
ends are in one.

## What is changed in the configs

- **Sanitising is on by default**, as `export-live --sanitise` does it
  ([what it removes and replaces](import-live.md#sanitising-configs)):
  local users, AAA and SNMP secrets and management addresses go, protocol
  keys become `lab-key` on both ends so sessions still come up, and an
  `admin`/`admin` login is added. If a config still looks like it holds a
  secret afterwards, nothing is written and the node, line and kind of
  secret are listed; `--allow-residual` (GUI: *Import anyway*) writes the
  configs all the same. `--no-sanitise` (GUI: untick *Sanitise the
  configs*) saves the configs as they are. Either way the configs, the
  topology and the report are written readable by their owner only.
- **The management interface is renamed `Management0`**, the one a cEOS
  node has: AVD configs for hardware switches use `Management1`, which
  cEOS would not find. Every mention is renamed (the stanza,
  `source-interface`, NTP, TerminAttr). `--keep-management-name` leaves it
  alone, for when you mount your own `EosIntfMapping.json`.
- **Interface names in links** follow containerlab's cEOS naming:
  `Ethernet1` is `eth1`, `Ethernet49/1` is `eth49_1`. The configs keep
  their names. `--keep-interface-names` turns that off.

Every node gets `--image` (default: the cEOS image `clabfleet new` uses).
cEOS images are not public: load yours into Docker on the lab hosts, and
give its name with `--image` or in the GUI dialog.

## Limits

- EOS configs only.
- Tested against the configs of AVD's own `single-dc-l3ls` example and
  hand-written ones in that style. The resulting labs have not been
  booted on cEOS yet: expect a few hardware-only lines (transceiver,
  platform or speed settings) that cEOS rejects at boot without harm, and
  tell us what else you run into.
- Links whose configs give no hint (no description naming the peer, no
  shared /31 or /30, for example IPv6-only or unnumbered links described
  by hand) are not found. Add them in the GUI builder or the YAML.
- A lab is imported once: running the import again does not merge into an
  existing lab the way `export-live` re-syncs do.
