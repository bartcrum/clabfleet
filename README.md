# clabfleet

Deploy and tear down [containerlab](https://containerlab.dev) topologies on
one host — or spread a large topology across a **cluster of hosts** with
automatic, resource-aware placement and VXLAN links between hosts. You can
also generate a topology from a live production network.

Topology files are **standard containerlab files** (`*.clab.yml`). Anything
containerlab accepts works here, and every file in `topologies/` still
deploys with plain `containerlab deploy`.

## Features

- **Deploy / destroy / save / inspect** labs locally or on a remote host over SSH
- **Multi-host clusters** — spread nodes across several containerlab servers;
  links between hosts become `vxlan-stitch` links that containerlab creates
  and removes itself
- **Placement strategies** — bin-pack, spread, or resource-based
- **Host pinning & tag affinity** — via ordinary node `labels`
- **Import from live network** — connects to real devices via NAPALM (device
  list from YAML, NetBox, Nautobot or Ansible), pulls running configs and LLDP
  neighbours, and writes a matching containerlab topology: interface names
  mapped per kind, images from rules, optional config sanitising, and
  re-syncs that report changes instead of overwriting
- **Image pre-flight** — fail before anything is created when a node's
  image is missing on its host, or pull it with `--pull`
- **Dry-run** — see the placement plan and the per-host topology files
  without deploying
- **Exec** — run a command on all or some nodes of a lab at once, through
  each kind's CLI, SSH or a shell
- **Lab templates** — generate a spine-leaf, ring or campus lab with
  addressing and routing configs for cEOS, IOL or plain Linux nodes
- **Config snapshots** — copy saved node configs into dated local folders
  and diff them against each other or the topology's `startup-config`
- **Capture** — `tcpdump` on any node interface as a live decode or a pcap
  you can pipe into Wireshark, from the CLI or by clicking a link in the GUI
- **Validate** — check topology files (and placement labels against a
  cluster) without touching any host, e.g. in CI
- **Routing view** — the OSPF areas and adjacencies, BGP sessions and EVPN
  overlay (VTEPs, VNIs, VXLAN tunnels) a lab is built to run, read from its
  startup configs, with inconsistencies between nodes flagged
- **Web GUI** — browse topologies, see live node state on a diagram,
  deploy/destroy with live output, and open CLI/shell/SSH terminals to nodes
  in the browser

## Requirements

- Python 3.11+
- On every lab host: Docker and containerlab
  (`bash -c "$(curl -sL https://get.containerlab.dev)"`)
- Remote hosts: SSH access (key-based recommended). containerlab needs root,
  so either set `sudo: true` (with passwordless sudo for containerlab) or
  add the SSH user to the `clab_admins` group.
- Node images (e.g. Cisco IOL, cEOS) are not public — build or import them on
  each host that will run those nodes.

## Quick start

```bash
pip install -e .

# Deploy on this machine (runs containerlab against the file in place;
# the lab directory clab-<name>/ is created next to it)
clabfleet --sudo deploy topologies/three_router_triangle.clab.yml

# What's running?
clabfleet --sudo inspect topologies/three_router_triangle.clab.yml

# Save running configs into the lab directory
clabfleet --sudo save topologies/three_router_triangle.clab.yml

# Tear it down (add --keep-lab-dir to keep saved configs)
clabfleet --sudo destroy topologies/three_router_triangle.clab.yml
```

### Wait for nodes to boot

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

### Run a command on every node

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

### Snapshot and diff configs

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

### Capture packets

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

### Check a topology before deploying

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

### Show the routing design

```bash
clabfleet routing topologies/evpn_fabric.clab.yml
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
  share a VNI.
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
not running or not readable). Up BGP sessions show their uptime and
prefixes received. Neighbours that run but are not in the startup configs
are listed, and so are sessions configured on one side in the startup
configs but on both sides on the routers: both mean the running config
has drifted, for example after changes on the CLI.

| Kind | How | Commands |
|------|-----|----------|
| Arista cEOS | one `docker exec`, JSON output | `show ip ospf neighbor vrf all`, `show ip bgp summary vrf all`, `show bgp evpn summary`, `show vxlan vtep` |
| Cisco IOL, CSR1000v, Catalyst 8000v | SSH to the management address (as `exec`; password from `CLAB_NODE_PASSWORD`, default `admin`) | `show ip ospf neighbor`, `show ip bgp summary`, `show bgp l2vpn evpn summary`, `show nve peers` |

A node is only asked about the protocols its startup config uses. Other
kinds show their sessions as unknown.

### Generate a lab from a template

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

### Node images

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

### Single remote host

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

## Web GUI

```bash
pip install -e ".[gui]"     # aiohttp, plus ruamel.yaml for saving edits

cd ~/Work/clabfleet
clabfleet --sudo gui                     # this machine
clabfleet gui --cluster topologies/cluster.yaml   # all cluster hosts
```

It prints its address (`http://localhost:8650/`); log in there. The first
time, the users file `~/.clabfleet/users.yaml` is created with one user,
**admin**, and a random password. The GUI prints it at every start until
it is changed, and keeps it in `~/.clabfleet/initial-admin-password`
(mode 0600; deleted once the password is changed). The first login asks
for a new password (12 characters or more) and allows nothing else until
it is set. Stop the GUI with
Ctrl+C. `--single-token` skips users altogether: the GUI prints a random
token link instead, opens it in your browser, and whoever has the token is
an operator.

- **Look:** the **◐ System / ☀ Light / ☾ Dark / ◑ High contrast** button
  in the top bar picks the colour theme (remembered per browser); high
  contrast is for projectors and bright rooms. A node or session pulses
  once when its state changes (not with reduced motion). Status is shown by
  shape as well as colour: a hollow ring is not deployed, a filled disc
  (with a check on the diagram) running, a half ring booting, a diamond
  not running, a square an error. Hover or select a node to fade
  everything but it, its links and its neighbours. Each node shows a
  device glyph (router, switch, host, firewall) chosen from its name, then
  its kind, with its status on the glyph's corner. Zoom with the wheel, the
  −, 100 %, + and Fit buttons at the bottom right, or `+`, `-`, `0` (fit),
  `1` (actual size) and the arrow keys once the canvas has focus; below
  60 % interface names and labels hide so the shape stays readable. From
  the keyboard, Tab into a canvas lands on a node: the arrow keys move to
  the nearest node that way (connected ones first), Enter selects it, `t`
  opens its terminal, Escape leaves the nodes so the arrows pan (`n` goes
  back). Screen readers hear the node they are on, job results, and
  problems as they appear and clear. Text and status colours meet WCAG AA
  contrast in both themes. With
  no lab selected, the start page lists the topologies and ways to start a
  new lab. Destroy and Redeploy
  ask in a dialog that says what will be removed; labs of 10 or more
  nodes ask you to type the lab name.
- **Sidebar:** every `*.clab.yml` under the current directory (or each
  `--dir`), with live state, plus any other labs running on your hosts.
  Filter it with the box at the top; deployed labs come first, with a ring
  showing how many nodes run, and labs are grouped by folder when there
  are several. « collapses it to a rail of state dots.
- **Command palette:** Ctrl+K (⌘K) or the Ctrl K button: type to jump to
  a lab, tab or node, open a node's CLI, shell, logs or config drift, run
  Deploy, Save configs and the other lab actions, switch the theme, and
  more. Arrow keys and Enter pick.
- **Events:** the Events tab (next to Health) lists what changed between
  live reads while the lab was open, such as a BGP session or a link going
  down and coming back, with the time, so a flap is still visible after it
  recovered; a strip shows the last 30 minutes at a glance. Links are
  recorded while the Diagram reads them, sessions, adjacencies and tunnels
  while the Routing tab is Live or the Health panel is open. Click a row
  to jump to it.
- **Export:** Export on the Diagram and Routing tabs saves the view as SVG
  or PNG, light or dark, for documents and slides.
- **Freshness:** live views say how old their data is ("live: read 4 s
  ago"). When it stops updating (a host or the GUI's server not
  answering), the label says so and the live colours turn grey instead of
  passing for the present.
- **Diagram:** nodes coloured by state (amber while booting), interface names on links, the host
  each node runs on (multi-host), and cross-host VXLAN links highlighted.
  Drag nodes to arrange them, scroll to zoom, double-click a node to open
  its terminal. Positions are remembered in the browser. **Save layout**
  writes them into the topology file as `graph-posX`/`graph-posY` node
  labels, so the layout travels with the file. Only those labels change:
  comments, ordering, quoting and indentation are kept.
- **Builder:** draw a lab instead of writing YAML. **+ New lab** in the
  sidebar starts one, blank (one node) or from a `clabfleet new` template.
  **Edit** on the Diagram tab (operators) opens a palette: drag a kind
  (Arista cEOS, Cisco IOL, Linux host) onto the canvas to add a node, and
  drag from a node's ● handle to another node to link them, with the next
  free port of each kind (`eth1`, `Ethernet0/1`, ...). Click a node or a
  link to rename it, change its kind or image, or change its ports; Delete
  removes the selection. The drawing is a draft: **Preview YAML** shows
  what it would write, **Save** writes it, **Discard** drops it. Saving
  changes only what differs in the file, so existing configs, comments and
  links of other forms (host, macvlan, ...) stay; a renamed node's
  `hostname` line follows, and a new node of a kind with no image in the
  file gets the kind's default image. **Generate configs…** addresses the
  drawing (a /31 per link, a /32 loopback per node, from pools you can
  change) and writes startup-configs: OSPF area 0, or eBGP with one AS per
  router, for cEOS and IOL; Linux hosts get their addresses and a default
  route through the router they are cabled to, which announces their
  subnet. It says first which existing configs it replaces; nothing is
  written until Save.
- **Inspector:** clicking a node (on the Diagram, the Nodes table or the
  Routing tab) or a link opens its details in a panel on the right, which
  narrows the canvas instead of covering it. Drag its left edge to resize
  it; on narrow screens it opens from the bottom. A node shows its state,
  CPU and memory, its interfaces with their peers and live state (click
  one to select the link), the protocols it runs (click one to see it on
  the Routing tab), and its terminal and Config diff buttons. A link
  shows the state of each end and the packet capture form.
- **Live link and node state:** while a deployed lab is open, links with
  an end down are drawn red and dashed. The link's tooltip says which
  end is down: **admin down** is the end that was shut, **no carrier** is
  the far side of it. A thin bar in each node shows its CPU use, full at
  one busy core. Hover a node, or click it, for CPU and memory. The GUI
  reads each node's interface state from `/sys/class/net` with one
  `docker exec` per node, and CPU and memory with one `docker stats` per
  host. It does this in the background, about every 5 to 10 seconds and
  only for labs someone has open, so many browser tabs do not add load.
  Topologies may name interfaces the kind's way (`Ethernet1` on cEOS,
  `Ethernet0/1` on IOL, `ethernet-1/1` on SR Linux). For VM-based kinds
  the state is that of the container's link to the VM, so a port shut
  inside the VM still shows as up. Links whose state cannot be read keep
  their normal colour.
- **Routing:** the `clabfleet routing` view as a diagram, on the same
  node positions as the Diagram tab. Pick OSPF, BGP or EVPN at the top.
  OSPF colours adjacencies by area (and shades each area when there are
  several); BGP shades each AS and draws IPv4 and EVPN sessions, dashed for
  iBGP and red for sessions configured on one side only; EVPN shows the
  EVPN sessions (control plane) and the VXLAN tunnels between VTEPs (data
  plane), either or both, for all VNIs or one. Click a node for its
  router-id, interfaces, sessions or VNIs, or an edge for both ends.
  The button at the top right counts the problems found in the configs
  and opens them in the Health panel. Nodes without the protocol are
  dimmed. **Cabling** shows the physical links faintly behind.
  Switch to **Live** on a deployed lab to colour every adjacency, session
  and tunnel by its running state (as `clabfleet routing --live`): green
  up, red down, amber partly up, grey not known, and blue dotted for
  neighbours running but not in the startup config. Cards show uptime and
  prefix counts, and sessions that are down join the problem list. The
  GUI asks the nodes in the background about every 10 seconds, only for
  labs someone has open on this tab or in the Health panel.
- **YAML editor:** edit the topology file in the YAML tab. Problems are
  listed as you type, using the same checks as `clabfleet validate`.
  Save with the button or Ctrl+S. Text that is not a loadable topology
  cannot be saved, but other errors, such as a startup config file that
  does not exist yet, do not block saving. Saving is refused while a job
  runs for the lab, or if the file changed on disk since you opened it.
- **Health:** the Health tab next to Activity lists everything wrong
  with the open lab: validation errors and warnings in the saved file,
  unreachable hosts, nodes not running or booting for more than five
  minutes, links down, routing config problems, and (while the panel is
  open) sessions, adjacencies and tunnels down, drift and neighbours not
  in the startup config. Filter by severity or source; click a row to
  select its node, link or session. The header's problem count opens it.
- **Lab header:** under the lab's name a status strip shows the nodes
  running (or ready, while booting), the hosts used, and the OSPF and BGP
  sessions up once the Routing tab has read them in Live mode. Click a
  stat to open the tab it comes from. The ⧉ button copies the topology
  file's path.
- **Deploy** while the lab is not deployed; **Redeploy / Save configs /
  Snapshot** once it is, and **Destroy** in the ⋯ menu, with containerlab's
  output streamed into the Activity panel. Different labs can run jobs at the
  same time, up to four, with one job per lab. Pick any job, running or
  past, from the Activity panel's list to see its output, how long it
  took, and the time each host took. The last 50 jobs are kept in
  `.clabfleet/jobs/` under the first workspace directory, so they survive
  a restart of the GUI.
- **Drift:** problems that come from a running config differing from the
  startup config (a session up that the startup config lacks, a neighbour
  not in it) offer **Config diff**, which reads the node's running config
  now and diffs it against its startup config (on cEOS with EOS's own
  `show running-config diffs`), and **Save configs** to keep the change.
- **Config diff:** the inspector's button opens a tab with the node's
  config in the latest snapshot against its `startup-config` or the
  previous snapshot
- **Terminals** in tabs at the bottom:
  - **CLI**: the node's own CLI via `docker exec` (`Cli` on cEOS, `sr_cli`
    on SR Linux, `cli` on cRPD)
  - **Shell**: a shell inside the container
  - **Logs**: follows the container's log (`docker logs --follow`, last
    2000 lines). It also works for a container that has stopped, which
    helps when a VM-based node fails to boot
  - **SSH**: `ssh` to the node's management IP. This is the CLI for VM-based
    kinds such as Cisco IOL. It needs a login on the node: containerlab's
    default configs create `admin`/`admin`, but your own `startup-config`
    must include a user

  Closing a CLI or Shell tab also ends what it started inside the
  container (the shell, its children and anything else in its session);
  killing the local `docker exec` alone would leave them running. Open
  tabs are limited to 16 per user and 64 in all (`--max-user-sessions N`,
  `--max-sessions N`), and captures (live tabs and downloads together) to
  4 per user and 8 in all; a tab over the limit says so. A browser that
  stops reading a tab's output for 30 seconds is disconnected, and the
  command's output is not read meanwhile, so a stuck tab cannot fill the
  GUI's memory.
- **Packet capture:** click a link in the diagram, pick which end to
  capture on, and optionally set a BPF filter, a packet count and a time
  limit (60 seconds by default). **Live** decodes packets in a tab at the
  bottom; press Ctrl+C there or close the tab to stop. **Download .pcap**
  captures to a file for Wireshark; **Stop & save** ends it early and keeps
  what was captured. Every GUI capture has a time limit: up to 10 minutes
  for a download, which also stops at 200 MB, and 30 minutes for a live
  tab. tcpdump is stopped inside the container when the tab closes, the
  download is cancelled or the GUI stops. See
  [Capture packets](#capture-packets) for how it reaches the node.
- Nodes on remote hosts are reached over the host's SSH connection, so the
  SSH user there needs Docker access (the `docker` group)

Security: terminals are shell access, so the GUI listens on `127.0.0.1`
only and every request needs a login. Requests from other websites are
refused. Use `--bind` with care, and set the admin password before you
do: until then anyone who has the initial password can log in as admin and
choose the password. See [Login and sessions](#login-and-sessions) below.

### Several users on a shared lab server

To let several people reach one GUI remotely, give each a named login
and serve it over TLS:

```bash
clabfleet user add alice                  # operator: everything; asks for a password
clabfleet user add bob --role viewer      # read-only
clabfleet user add ci --token             # a login token instead of a password
clabfleet user passwd alice               # new password, ends alice's sessions
clabfleet user rotate ci                  # new token, ends ci's sessions
clabfleet user list
clabfleet user remove bob                 # ends bob's sessions

clabfleet --sudo gui --bind 0.0.0.0 --tls-cert cert.pem --tls-key key.pem
```

- **Users file:** `~/.clabfleet/users.yaml` (mode 0600; another file with
  `--users FILE` on both `gui` and `user`; only the default one is created
  with admin and a random password when missing). Passwords are stored as scrypt hashes
  with a random salt, tokens as SHA-256 hashes. `user add` and `user
  passwd` ask for the password twice (`--password-stdin` reads one line
  from stdin, for scripts); `user add --token` and `user rotate` print a
  token once, with a login link (`--url https://lab.example.com:8650`
  sets its address). `user list` shows how each user logs in. Changes
  apply to a running GUI right away. A users file that
  other users could change (group/world-writable, owned by someone else,
  or in such a directory) is refused and nobody can log in until it is
  fixed.
- **Users in the GUI:** operators manage users with **Users** in the top
  bar: add an operator or viewer with a temporary password (shown once,
  with a Copy button; the user chooses their own at the first login) or
  a login token, change roles, reset a password or token (their sessions
  end), and remove users. Nobody can change their own role or remove
  themselves, and the last operator cannot be demoted or removed. All of
  it goes to the audit log (`user_added`, `user_role_changed`,
  `user_login_reset`, `user_removed`).
- **Login:** each user logs in at `https://<server>:8650/` with their name
  and password (or opens their token link), once per browser: the login
  lasts up to 30 days and survives GUI restarts. **Password** in the top
  bar changes your own password and logs out your other browsers. See
  [Login and sessions](#login-and-sessions). The header shows who you are
  and your role.
- **Roles:**
  - `operator`: deploy, redeploy, save and destroy, terminals, YAML edits
    and layout saves
  - `viewer`: topologies, diagrams, YAML, node state, job output and node
    logs (the Logs tab) of the labs defined by the workspace's topologies.
    Viewers cannot see config diffs (configs hold password hashes), open
    terminals or follow logs of other labs on the hosts, and topology
    files that are symlinks to somewhere outside the workspace are not
    shown at all. Controls they cannot use are hidden. The server
    refuses everything else with 403. Any route that is not a plain GET
    is operator-only unless the code marks it otherwise, so new features
    are protected by default.
- **Audit log:** `audit.jsonl` next to the users file (or `--audit-log
  FILE`; in single-token mode only with `--audit-log`). One JSON object
  per line with `ts`, `user`, `role`, `remote`, `event` and `details`.
  Events: `login` (method: password or token), `login_failed` (with the
  user name tried), `login_throttled` (once per address or user name and
  minute after 5 failed logins), `logout`, `password_changed`,
  `password_change_failed`, the `user_*` events above, `denied`, `job_started`
  (action, topology, options), `job_finished` (status, seconds),
  `topology_saved`, `positions_saved`, `terminal_opened` and
  `terminal_closed` (lab, node, mode, host, seconds, exit code),
  `capture_started`, `capture_finished` and `session_revoked` (an open
  terminal or capture ended because its login no longer holds). Jobs also
  record who started them, shown in the Activity panel.

  ```json
  {"ts": "2026-10-02T00:51:40.197+00:00", "user": "alice", "role": "operator", "remote": "10.1.2.3", "event": "terminal_opened", "details": {"lab": "spine-leaf-fabric", "node": "Spine-1", "mode": "cli", "host": "localhost"}}
  ```
- **TLS:** `--tls-cert` and `--tls-key` take PEM files. For a quick test,
  a self-signed pair:
  `openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem -days 365 -subj "/CN=$(hostname)" -addext "subjectAltName=DNS:$(hostname)"`.
  `--bind` to anything but a loopback address without TLS is refused;
  `--insecure-http` overrides that with a warning, for trusted networks
  only. Behind a reverse proxy that terminates TLS, keep the GUI on
  `127.0.0.1` and pass `--public-url https://lab.example.com` so the
  origin check and secure cookies match the address browsers use. Over
  HTTPS the GUI sends `Strict-Transport-Security` (one year), so browsers
  will then use HTTPS for every port of that host name.

### Login and sessions

- Named users log in with their name and password. Checking a password
  takes about a quarter of a second (scrypt), the same for names that do
  not exist, so failures do not tell which names are real. Passwords need
  12 characters or more. A user marked to change their password (the
  first admin) can only change it: the server refuses every other
  request from that login until it is done.
- Login links put the token after `#` (`/#token=...`). Browsers do not
  send that part to the server, so it stays out of access logs and proxy
  logs; the page posts the token to `/login` and removes it from the
  address bar. Without a link, the page asks for the token. Old
  `/?token=...` links still work (they are redirected to the `#` form)
  but are deprecated: the token in them reaches the server's URL. The
  access log (`-v`) never shows query strings.
- `/login` only accepts JSON from the GUI's own origin, so another website
  cannot log your browser in. A login link of a different user does not
  replace your session unless you confirm the switch.
- A login gives the browser a random session id in a cookie (HttpOnly,
  SameSite=Strict; over HTTPS Secure with the `__Host-` prefix). The
  password or token itself is never stored in the browser. The cookie name includes the
  GUI's port, because browsers send a host's cookies to all of its ports.
- Logins survive a GUI restart on the same port, and closing the browser:
  open your link once per browser. The sessions are kept in
  `gui-sessions-<port>.json` next to the users file (or in
  `~/.clabfleet/` without one), mode 0600, and only as SHA-256 hashes of
  the session ids, so reading the file does not let anyone in. Like the
  users file, it is not used if other users could change it. In
  single-token mode each start prints a new token, but browsers already
  logged in stay logged in; delete the file to log every browser out.
- A session ends after 7 days idle, 30 days after login, on **Log out**,
  when the user's password or token changes, or when the user is
  removed. Each user keeps at most 20 sessions; another login ends the
  oldest.
- After 5 failed logins from one address within a minute, logins from it
  are refused (429) for the rest of that minute; likewise after 5 wrong
  passwords for one user name, from any address (so one account can be
  locked out for a minute at a time by someone guessing at it). Behind a
  reverse proxy every client shares the proxy's address.
- Responses carry a Content-Security-Policy (no inline scripts, no
  framing) and `Cache-Control: no-store` for everything but static files.

Security notes: **operators are effectively root on the lab hosts.** That
is by design: containerlab runs as root, and an operator can edit and
deploy a topology that mounts any host path or runs privileged
containers. They can also open shells on every node and, through
`docker exec`, act as the account the GUI runs as on each lab host. Make
only trusted people operators; `viewer` is the role for anyone else.
Tokens are bearer secrets: anyone with a login link is that user until you
rotate it. Passwords are only as strong as people make them; for a team,
plan to put the GUI behind your directory (LDAP / Active Directory, or
OAuth / OIDC through a reverse proxy) rather than rely on them.
Terminals and captures that are already open are closed
within a few seconds when their user is removed, rotated, demoted from
operator or logs out.

For the CLI and Shell buttons, your user must be able to run `docker`
(member of the `docker` group, in a session started after you were added).

## Multi-host cluster deployment

When one server doesn't have enough CPU/RAM, spread the topology across
several.

### 1. Define your cluster

```yaml
# cluster.yaml
cluster:
  link_type: "vxlan-stitch"   # vxlan-stitch | vxlan
  vni_base: 1000              # lowest VNI; each cross-host link gets its own
  dst_port: 14789             # VXLAN UDP port — must be open between hosts
  mtu: 1450                   # leave room for the 50-byte VXLAN overhead

hosts:
  - name: "clab-1"
    host: "192.168.1.101"     # SSH address, also the VXLAN endpoint
    ssh_user: "netops"
    ssh_key: "~/.ssh/id_ed25519"
    sudo: true
    max_cpu: 16               # optional — defaults to nproc
    max_ram: 65536            # optional, MB — defaults to MemAvailable
    tags: ["core"]

  - name: "clab-2"
    host: "192.168.1.102"
    vtep_ip: "10.10.10.2"     # optional — separate underlay address for VXLAN
    ssh_user: "netops"
    ssh_key: "~/.ssh/id_ed25519"
    sudo: true
    tags: ["access"]
```

A host may be `localhost` (runs locally, no SSH). If any link crosses to or
from it, set its `vtep_ip` to an address the other hosts can reach.

### 2. Check the hosts

```bash
clabfleet status --cluster topologies/cluster.yaml
clabfleet status --cluster topologies/cluster.yaml --check-links
```

`--check-links` tests every pair of hosts in both directions. It pings
the other host's VXLAN address, then runs a short UDP listener on the VXLAN
port there and sends it tagged datagrams. The UDP test catches a firewall
that drops VXLAN while ping still works. The probes use `ping` and
`python3` on the hosts. A probe that cannot run is reported as not tested
rather than failed.

The same check runs automatically before deploying a lab with cross-host
links, for the host pairs that share links, and stops the deploy before
anything is created. Turn it off with `--skip-link-check`. On RHEL and
other firewalld hosts, open the port to your other lab hosts:

```bash
sudo firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source address=192.168.1.0/24 port port=14789 protocol=udp accept'
sudo firewall-cmd --reload
```

### 3. Deploy across the cluster

```bash
# Preview placement and write the per-host topology files
clabfleet deploy topologies/large_campus.clab.yml \
    --cluster topologies/cluster.yaml --dry-run --output-dir /tmp/campus

# Deploy
clabfleet -v deploy topologies/large_campus.clab.yml \
    --cluster topologies/cluster.yaml --strategy bin-pack

# Destroy on all hosts
clabfleet destroy topologies/large_campus.clab.yml \
    --cluster topologies/cluster.yaml
```

If a host fails during deploy, the others are left running so you can
look at what went wrong, and the output's `status` is `partial`. With
`--rollback`, clabfleet stops at the first failing host and destroys the
lab on every host it reached, including the failed one, which may hold
partly created nodes. The status is then `rolled-back`, or
`rollback-failed` if a host could not be cleaned up. The GUI has the same
option as a "Roll back on failure" checkbox in the lab header's ⋯ menu.

Each deploy writes `<lab>.placement.json` next to the topology file. It
records the host of every node, the VNI range, the cross-host links and
the cluster file used. `destroy`, `save`, `inspect` and `exec` then only
contact the hosts listed there, so an unrelated host being down does not
get in the way. A clean `destroy` removes the file. Without it, those
commands try every host in the cluster, as before. The web GUI uses the
record to show each node's host and the VNI of each cross-host link even
when it cannot reach a host.

### 4. Control node placement

Placement hints are node labels, so they work anywhere labels do (`defaults`,
`kinds`, `groups`, nodes). containerlab itself ignores them.

```yaml
topology:
  groups:
    access:
      labels:
        lab.host-tags: access       # prefer hosts tagged "access"
  nodes:
    Core-1:
      kind: cisco_iol
      labels:
        lab.host: clab-1            # pin to a specific host
    Access-1:
      kind: cisco_iol
      type: L2
      group: access
    Server-1:
      kind: linux
      labels:
        lab.cpu: "2"                # override the placement estimate
        lab.ram: "2048"             # (MB)
```

| Label | Meaning |
|-------|---------|
| `lab.host` | Pin the node to this cluster host |
| `lab.host-tags` | Comma-separated tags; prefer hosts with any of them |
| `lab.cpu` / `lab.ram` | vCPU / MB to reserve when placing the node |

Without `lab.cpu`/`lab.ram`, placement uses the node's `cpu`/`memory`
limits, then a per-kind estimate (e.g. cEOS 1 vCPU / 2 GB, IOL 0.5 / 512 MB).

Labs already running on a host count against it. Their nodes are
estimated the same way, from their `lab.cpu`/`lab.ram` labels or per-kind
estimates, and that CPU is reserved before placing the new lab. Their RAM
is reserved only when the inventory sets `max_ram`: a probed host's
`MemAvailable` already reflects what running labs use. `clabfleet status`
lists the running labs on each host with these estimates.

### Placement strategies

| Strategy | Behaviour |
|----------|-----------|
| `bin-pack` | Fill each host before moving to the next, keeping neighbours together. Fewest cross-host links. **(default)** |
| `spread` | Distribute nodes evenly across hosts. When hosts are equally loaded, a node joins the host where most of its neighbours already are. |
| `resource` | Always pick the host with the most free resources. |

### How cross-host links work

Each host gets a containerlab topology with only its own nodes. A link
whose ends land on different hosts is replaced by a `vxlan-stitch` link on
each side, pointing at the other host and sharing a VNI:

```
Host clab-1                              Host clab-2
┌────────────────────┐                  ┌────────────────────┐
│ Core-1             │                  │             Dist-1 │
│  Ethernet0/2 ──────┤                  ├────── Ethernet0/1  │
│      vxlan-stitch  │── VNI 1000 ──────│  vxlan-stitch      │
│ remote: clab-2     │   UDP 14789      │  remote: clab-1    │
└────────────────────┘                  └────────────────────┘
```

containerlab builds the VXLAN tunnels on deploy and removes them on destroy.
The nodes see an ordinary point-to-point link.

VNIs are unique across the cluster, so several labs can share it. Before
deploying, clabfleet reads the VNIs that other labs use from their lab
directories on each host (`~/clabfleet/<lab>/`), and gives this lab the
first free block at or above `vni_base`. A lab destroyed with
`--keep-lab-dir` keeps its VNIs reserved until its directory is removed.
The chosen range is shown as `vni_range` in the deploy output.

## Import from live network

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

### Device sources

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

### Kinds and images

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

### Interface names

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

### Sanitising configs

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

### Neighbours outside the inventory

`--include-neighbours` adds LLDP neighbours that are not in the inventory
(servers, devices you cannot log in to, ...) as `linux` nodes
(`--neighbour-image`, default `alpine:3`), named after their LLDP system
name or chassis ID, with their links. The report marks them
`neighbour: true`.

### Re-sync

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

## Example topologies

| File | Description |
|------|-------------|
| `topologies/three_router_triangle.clab.yml` | 3x Cisco IOL routers in a full mesh with OSPF |
| `topologies/spine_leaf.clab.yml` | 2-spine 4-leaf fabric with BGP (Arista cEOS) |
| `topologies/large_campus.clab.yml` | 8-node IOL/IOL-L2 campus with placement labels for multi-host |
| `topologies/evpn_fabric.clab.yml` | 2-spine 4-leaf EVPN/VXLAN fabric (Arista cEOS): eBGP underlay, EVPN overlay, L2 and L3 VNIs, 4 Linux hosts |
| `topologies/cluster.yaml` | 3-host cluster inventory |
| `topologies/live_devices_example.yaml` | Device inventory for live network export |

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

## Development

Tests run automatically on every pull request (GitHub Actions, Python 3.11, 3.12, 3.13 and 3.14).

```bash
pip install -e ".[dev]"
pytest
```

## Project structure

```
clabfleet/
  __init__.py      # Package init
  topology.py      # Load containerlab topologies, inheritance, placement hints
  cluster.py       # Cluster inventory and host probing
  placement.py     # Resource-aware node placement engine
  deployer.py      # Deploy/destroy/save/inspect; split topology per host
  runner.py        # Run commands locally or over SSH
  exporter.py      # Build a topology from live devices (NAPALM)
  inventory.py     # Device lists: YAML, NetBox, Nautobot, Ansible; kind/image rules
  ifmap.py         # Map device interface names to each kind's naming
  sanitise.py      # Strip secrets and management addressing from configs
  resync.py        # Diff and apply a re-import against an existing topology
  execute.py       # Run a command on lab nodes (clabfleet exec)
  snapshots.py     # Config snapshots and diffs (clabfleet snapshot/diff)
  capture.py       # tcpdump on node interfaces (clabfleet capture, GUI)
  nodes.py         # Per-kind CLI/SSH access, terminal commands, inspect parsing
  validate.py      # Topology checks (clabfleet validate)
  templates.py     # Lab templates (clabfleet new)
  linkcheck.py     # Ping and UDP checks between cluster hosts
  readiness.py     # Is a node's CLI/SSH up yet (deploy --wait, GUI)
  routing/         # OSPF/BGP/EVPN views from startup configs (clabfleet routing, GUI)
  cli.py           # CLI entrypoint
  gui/
    server.py      # aiohttp app: API, auth middleware, terminal websockets
    auth.py        # Users file, roles, audit log
    state.py       # Topology discovery, running labs, deploy/destroy jobs
    terminals.py   # Local pty, SSH-channel and live capture sessions
    sessions.py    # Open terminal/capture sessions: limits, revocation
    captures.py    # GUI packet captures: limits, pcap downloads
    editing.py     # Format-preserving saves of topology files
    static/        # Web UI (vanilla JS; xterm.js bundled in vendor/)
topologies/        # Example topologies and cluster inventory
tests/             # pytest suite
```

## License

MIT — see [LICENSE](LICENSE).
