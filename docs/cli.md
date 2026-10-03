# Command line

Everything `clabfleet` does on one host, beyond the deploy, inspect, save and
destroy shown in the [README](../README.md#quick-start). For several hosts see
[Multi-host cluster deployment](cluster.md); for the browser, the [Web GUI](gui.md).

## Wait for nodes to boot

containerlab returns once containers start, but network OSes take
longer: cEOS needs about a minute, VM-based kinds several. With `--wait`,
`deploy` waits until every node is ready, printing progress, for up to
`--wait-timeout` seconds (default 900). It exits with code 1 if some
nodes are still not ready, and lists them in the output's `readiness`.

```bash
clabfleet deploy topologies/spine_leaf.clab.yml --wait
```

A node is ready when its Docker health check reports healthy, which
vrnetlab images have. Without a health check, a kind with a CLI through
`docker exec` (cEOS, SR Linux, cRPD) must answer `show version`, and a
kind reached over SSH, such as IOL, must answer on port 22 of its
management address. Other kinds are ready once running. The GUI uses the
same probes and shows nodes as **booting** in amber until they are ready.

## Run a command on every node

```bash
clabfleet exec topologies/spine_leaf.clab.yml "show ip bgp summary"
clabfleet exec topologies/spine_leaf.clab.yml --nodes 'leaf*' "show version"
clabfleet exec topologies/three_router_triangle.clab.yml "show ip ospf neighbor"
clabfleet exec lab.clab.yml --mode shell -- ip -br addr
```

Nodes run in parallel and each node's output is printed under its name.
The exit code is 1 if any node failed. Add `--json` for machine-readable
output. Each node is reached the best way its kind allows (`--mode auto`):

| Mode | Used for | How |
|------|----------|-----|
| `cli` | cEOS, SR Linux, cRPD | The node's CLI through `docker exec` |
| `ssh` | VM-based kinds such as Cisco IOL | SSH to the management address, tunnelled through the host's SSH connection for remote hosts |
| `shell` | Everything else | `sh -c` inside the container |

SSH mode logs in with containerlab's default `admin`/`admin`, or `root`
for cRPD. Use `--user` with `CLAB_NODE_PASSWORD` or `--ask-password` (a
prompt) if your startup configs create other logins. `--password PW` works
too, but other users on the machine can see it in `ps` and it stays in your
shell history. Words after the topology are
joined with spaces, as `ssh` does, so quote the command or put it after
`--` when it has options of its own. Like the GUI terminals, the CLI and
shell modes need Docker access on the host. If Docker refuses and the
host uses `sudo`, clabfleet retries with `sudo`.

## Snapshot and diff configs

```bash
clabfleet --sudo snapshot topologies/spine_leaf.clab.yml
clabfleet --sudo snapshot topologies/spine_leaf.clab.yml --nodes 'leaf*' --name before-bgp
clabfleet snapshot topologies/spine_leaf.clab.yml --list

clabfleet diff topologies/spine_leaf.clab.yml                     # latest vs the one before
clabfleet diff topologies/spine_leaf.clab.yml --against startup   # latest vs the topology
clabfleet diff topologies/spine_leaf.clab.yml --from before-bgp --against latest
```

`snapshot` runs `save` (skip it with `--no-save`), then copies each
node's saved config from the lab directory into a local folder:

```
topologies/snapshots/spine-leaf-fabric/20261002T004701Z/
  snapshot.json     # when, and each node's host, kind and source file
  Spine-1.cfg
  Leaf-1.cfg
  ...
```

Snapshots go to `snapshots/<lab>/` next to the topology, or
`DIR/<lab>/` with `--dir`, and are named after the UTC time unless you
give `--name`. Configs are read with `cat` on each node's host, from the
placement record, so nodes on remote hosts work too. If the file is not
readable and the host uses `sudo`, clabfleet retries with `sudo`.
Snapshots hold whole device configs (password hashes, keys), so their
folders are created mode 0700 and the files 0600.

| Kind | Saved config |
|------|--------------|
| cEOS | `clab-<lab>/<node>/flash/startup-config` → `<node>.cfg` |
| SR Linux | `clab-<lab>/<node>/config/config.json` → `<node>.json` |
| cRPD | `clab-<lab>/<node>/config/juniper.conf` → `<node>.conf` |

Other kinds are listed as skipped. Cisco IOL saves into its binary NVRAM
file, which a snapshot cannot read, and `linux` nodes have nothing to
save.

`diff` prints a unified diff per node of a snapshot (`--from`, default
the latest) against `previous` (the snapshot before it, the default),
`startup` (the node's `startup-config` in the topology, inline or a
file inside the topology's folder; a path that leads outside it, also
through a symlink, is skipped), `latest` or a snapshot name. Lines that change on every save, such
as cEOS's `! Startup-config last modified at` comment, are ignored. Expect
a diff against `startup` even without changes: the saved config is the
whole running config, including defaults and the management interface
containerlab sets up. The exit code is 0 with no differences, 1 with
differences and 2 on errors. Add `--json` for machine-readable output.

## Capture packets

```bash
# Live decode, BGP only, 5 packets
clabfleet capture topologies/spine_leaf.clab.yml Spine-1:eth1 -f 'tcp port 179' -c 5

# pcap file, stopped after 60 seconds
clabfleet capture topologies/spine_leaf.clab.yml Leaf-1:eth1 -w leaf1.pcap --duration 60

# Straight into Wireshark
clabfleet capture topologies/spine_leaf.clab.yml Spine-1:eth1 -w - | wireshark -k -i -
```

`capture` runs `tcpdump` in the node's network namespace on whichever host
the node runs on. Without `-w` it prints a live text decode; with `-w FILE`
(or `-w -` for stdout) it writes a pcap. `-f` takes a BPF filter, `-c` a
packet count, `--duration` a time limit in seconds and `--snaplen` the bytes
kept per packet. Without a limit it runs until Ctrl+C.

The interface must be one the node uses in the topology's links, or `eth0`
(management). Nodes with their own `tcpdump`, such as cEOS, use it through
`docker exec`. For nodes without one, such as `alpine`, clabfleet starts a
throwaway helper container that shares the node's network namespace
(`docker run --net container:<node>`). The helper image is
`nicolaka/netshoot`, pinned by digest, pulled on first use; set
`--helper-image` or `CLAB_CAPTURE_IMAGE` to use another image with
`tcpdump` and `sh`, and `--via node|helper` to force either way. The
helper runs with `--log-driver none` (so captured traffic is not copied
into Docker's log on the host) and modest limits (`--pids-limit 64
--memory 256m`).

Stopping the local `docker` client does not stop a process inside a
container, so clabfleet always stops captures explicitly: it kills the
node's `tcpdump` by PID, or removes the helper container. With
`--duration`, `tcpdump` also runs under `timeout` (when the node or helper
image has it), so it ends even if clabfleet is killed outright. A helper
with a duration is labelled with the time it will have stopped by
(`clabfleet.capture.expires`); when the GUI starts, it removes helpers
more than a minute past that time on every host. That leaves the captures
of other running GUIs and of `clabfleet capture` alone, and never touches
helpers without a duration. Like `exec`, captures need Docker access on
the host, with the same `sudo` retry.

## Check a topology before deploying

```bash
clabfleet validate topologies/*.clab.yml
clabfleet validate topologies/large_campus.clab.yml --cluster topologies/cluster.yaml
```

`validate` never contacts a host. It reports:

- **Errors** (exit code 1): files that do not load, links to unknown
  nodes, an interface used by two links, a missing `startup-config`,
  `license` or `env-files` file, and with `--cluster`, nodes pinned with
  `lab.host` to a host the cluster does not have.
- **Warnings**: a missing bind-mount source, files outside the topology
  directory (not copied to remote hosts), kinds clabfleet has no placement
  estimate or CLI access for, and with `--cluster`, `lab.host-tags` no host
  matches and cluster hosts without a VXLAN address.

Add `--strict` to fail on warnings as well.

## Show the routing design

```bash
clabfleet routing topologies/evpn_fabric.clab.yml
clabfleet routing topologies/evpn_mlag.clab.yml --protocol mlag
clabfleet routing topologies/large_campus.clab.yml --protocol ospf
clabfleet routing topologies/spine_leaf.clab.yml --json
```

`routing` reads the nodes' startup configs (inline or files next to the
topology) and prints the intended protocol design, without contacting a
host:

- **OSPF:** router-ids (configured, or derived the way IOS and EOS do),
  areas, ABRs, and an adjacency for every pair of OSPF interfaces in one
  subnet, with costs. Areas come from `network` statements (wildcard or
  prefix, the most specific wins) or from `ip ospf area` on the interface.
- **BGP:** each neighbour statement matched to the node that owns the
  address and paired with the other side's statement, so a session shows
  as eBGP or iBGP, with its address families (IPv4, EVPN), peer groups,
  and whether it runs over a cable or between loopbacks. Peers outside
  the lab are listed as external.
- **EVPN:** VTEPs and their source address, L2 VNIs (VLAN) and L3 VNIs
  (VRF) with their RD and route targets, the BGP sessions that carry the
  EVPN address family, and a VXLAN tunnel between every two VTEPs that
  share a VNI. The two halves of an MLAG pair are one VTEP: no tunnel
  between them, and their shared source address is not a duplicate.
- **MLAG** (EOS `mlag configuration`): which leaves pair up (each one's
  peer-address is the other's local interface), the peer-link and the
  cables in it, the dual-homed ports (`mlag <n>` on a port-channel) with
  their VLAN and the hosts behind them, and the shared VTEP. Problems: a
  peer-address nobody has or that does not point back, different
  domain-ids, a peer-link not cabled to the peer, an `mlag` id or VLAN on
  one side only, and a pair whose VTEPs use different addresses.
- **Problems:** a session configured on one side only, a `remote-as` that
  is not the peer's AS, loopback peering without `update-source` or
  `ebgp-multihop`, address families that differ between the two sides,
  OSPF area or network type mismatches, a cable whose ends are not in one
  subnet or run OSPF on one end only, duplicate addresses (anycast
  gateways excepted), VTEPs without an EVPN session, VNIs on a single VTEP
  and route targets that do not match.

Configs in IOS style are read: Cisco IOS, IOS-XE and NX-OS, and Arista
EOS. Other formats (Junos, SR Linux) are listed as not read.

Add `--live` (and `--cluster` for a multi-host lab) to also ask the
running nodes, with read-only `show` commands, and compare:

```bash
clabfleet routing topologies/spine_leaf.clab.yml --live
```

Every adjacency, session and VXLAN tunnel gets a state: **up**, **down**
(with the reason: the BGP state such as `Active`, the OSPF state such as
`EXSTART`, a neighbour missing from the running config, a remote VTEP not
learned), **partial** (some address families down) or **unknown** (node
not running or not readable). An MLAG pair is up when both halves are
active and connected with the peer-link up and the config consistent,
and partly up while a dual-homed port is up on one side only. Up BGP sessions show their uptime and
prefixes received. Neighbours that run but are not in the startup configs
are listed, and so are sessions configured on one side in the startup
configs but on both sides on the routers: both mean the running config
has drifted, for example after changes on the CLI.

| Kind | How | Commands |
|------|-----|----------|
| Arista cEOS | one `docker exec`, JSON output | `show ip ospf neighbor vrf all`, `show ip bgp summary vrf all`, `show bgp evpn summary`, `show vxlan vtep`, `show mlag` |
| Cisco IOL, CSR1000v, Catalyst 8000v | SSH to the management address (as `exec`; password from `CLAB_NODE_PASSWORD`, default `admin`) | `show ip ospf neighbor`, `show ip bgp summary`, `show bgp l2vpn evpn summary`, `show nve peers` |

A node is only asked about the protocols its startup config uses. Other
kinds show their sessions as unknown.

## Generate a lab from a template

```bash
clabfleet new --list
clabfleet new spine-leaf --spines 2 --leaves 4 -o labs/fabric.clab.yml
clabfleet new ring --nodes 5 --kind cisco_iol -o labs/ring.clab.yml
clabfleet new campus --core 2 --dist 2 --access 6 --kind linux -o labs/campus.clab.yml
```

`new` writes a complete topology: nodes, links and a startup config for
every node. Without `-o` it prints the YAML. It will not overwrite an
existing file unless you add `--force`.

| Template | Options (default) | Routing |
|----------|-------------------|---------|
| `spine-leaf` | `--spines` (2), `--leaves` (4), `--asn` (65000) | eBGP: spines share `--asn`, leaf *n* uses `--asn` + *n*, leaves use ECMP over the spines |
| `ring` | `--nodes` (4, at least 3) | OSPF area 0 |
| `campus` | `--core` (2), `--dist` (2), `--access` (4) | OSPF area 0. Cores are fully meshed, every distribution node links to every core, and access nodes are split over the distribution nodes in blocks with one uplink each |

Every template takes the same common options:

- `--kind`: `arista_ceos` (default), `cisco_iol` or `linux`. cEOS configs
  create the `admin`/`admin` login that SSH needs. IOL links start at
  `Ethernet0/1`, after the management port, and continue `0/2`, `0/3`,
  `1/0` and so on. `linux` nodes (default image `alpine:3.20`, deploy with
  `--pull`) get their addresses through `exec` commands but run no
  routing, so each node reaches only its direct neighbours.
- `--image` replaces the kind's default image (`--list` shows them).
  `--name` sets the lab name, which defaults to the template name.
- `--link-subnet` (default `10.0.0.0/16`): every link gets the next /31
  from it, and the first node of the link, such as the spine or the core,
  gets the lower address.
- `--loopback-subnet` (default `10.255.0.0/16`): each node gets a /32
  loopback, with one /24 per tier. For example spines are `10.255.0.n` and
  leaves `10.255.1.n`.

The cEOS and IOL configs use the same syntax as the example topologies,
and the tests check every template and kind with `validate`. Only `linux`
labs have been deployed and pinged so far; the cEOS and IOL configs have
not been booted.

## Node images

Before deploying, clabfleet checks that every node's image exists on the
host the node is placed on, and stops with one message listing what is
missing per host. Nothing is created until the check passes.

- `--pull` runs `docker pull` for missing images first, then fails only
  on the ones that could not be pulled. Public images such as `alpine`
  need this, or a manual `docker pull`, because the check runs before
  containerlab would pull them itself.
- `--skip-image-check` turns the check off.
- Nodes with `image-pull-policy: always` are not checked, since
  containerlab pulls those on every deploy.
- If Docker cannot be reached on a host, even with `sudo` when the host
  uses it, the check is skipped there with a warning.

## Single remote host

```bash
export CLAB_HOST=192.168.1.101
export CLAB_SSH_USER=netops
export CLAB_SSH_KEY=~/.ssh/id_ed25519

clabfleet status
clabfleet --sudo deploy topologies/spine_leaf.clab.yml
clabfleet --sudo destroy topologies/spine_leaf.clab.yml
```

The topology and every file it references (startup configs, licenses,
bind-mount sources, env files) are copied to `~/clabfleet/<lab>/` on the
host, and containerlab runs there. `destroy` removes that directory.

The first connection to a host remembers its SSH host key in
`~/.clabfleet/known_hosts` (keys in `~/.ssh/known_hosts` count too), and
later connections refuse a host whose key has changed, like OpenSSH's
`StrictHostKeyChecking accept-new`. With `--host-key-policy strict` (or
`host_key_policy: strict` per host or under `cluster:` in a cluster file)
only already-known keys are accepted; add them with `ssh-keyscan` or one
manual `ssh` first.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAB_HOST` | `localhost` | Lab host for single-host commands |
| `CLAB_SSH_USER` | your SSH config | SSH username |
| `CLAB_SSH_KEY` | SSH agent / defaults | SSH private key |
| `CLAB_SSH_PASS` | — | SSH password (prefer keys) |
| `CLAB_HOST_KEY_POLICY` | `accept-new` | SSH host key checking for `--host`: `accept-new` or `strict` |
| `CLAB_NODE_PASSWORD` | `admin` | `exec`: SSH password on the lab nodes |
| `CLAB_SUDO` | off | Run containerlab with sudo (`1` to enable). The GUI uses `sudo -n`, so sudo for containerlab must not need a password |
| `CLAB_CAPTURE_IMAGE` | `nicolaka/netshoot@sha256:…` (pinned) | Image with `tcpdump` for capturing on nodes that have none |
| `CLABFLEET_DEVICE_USERNAME` / `CLABFLEET_DEVICE_PASSWORD` | — | `export-live`: device login when the inventory gives none |
| `NETBOX_TOKEN` / `NAUTOBOT_TOKEN` | — | `export-live`: API token for `--netbox` / `--nautobot` (sent only to that URL, over HTTPS) |
