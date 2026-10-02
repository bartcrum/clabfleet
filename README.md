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
- **Import from live network** — connects to real devices via NAPALM, pulls
  running configs and LLDP neighbours, writes a matching containerlab topology
- **Image pre-flight** — fail before anything is created when a node's
  image is missing on its host, or pull it with `--pull`
- **Dry-run** — see the placement plan and the per-host topology files
  without deploying
- **Exec** — run a command on all or some nodes of a lab at once, through
  each kind's CLI, SSH or a shell
- **Capture** — `tcpdump` on any node interface as a live decode or a pcap
  you can pipe into Wireshark, from the CLI or by clicking a link in the GUI
- **Validate** — check topology files (and placement labels against a
  cluster) without touching any host, e.g. in CI
- **Web GUI** — browse topologies, see live node state on a diagram,
  deploy/destroy with live output, and open CLI/shell/SSH terminals to nodes
  in the browser

## Requirements

- Python 3.10+
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
for cRPD. Use `--user` and `--password`, or set `CLAB_NODE_PASSWORD`, if
your startup configs create other logins. Words after the topology are
joined with spaces, as `ssh` does, so quote the command or put it after
`--` when it has options of its own. Like the GUI terminals, the CLI and
shell modes need Docker access on the host. If Docker refuses and the
host uses `sudo`, clabfleet retries with `sudo`.

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
`nicolaka/netshoot` by default, pulled on first use; set `--helper-image`
or `CLAB_CAPTURE_IMAGE` to use another image with `tcpdump`, and `--via
node|helper` to force either way.

Stopping the local `docker` client does not stop a process inside a
container, so clabfleet always stops captures explicitly: it kills the
node's `tcpdump` by PID, or removes the helper container. With
`--duration`, the node's `tcpdump` also runs under `timeout` (when the node
has it), so it ends even if clabfleet is killed outright. Like `exec`,
captures need Docker access on the host, with the same `sudo` retry.

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

## Web GUI

```bash
pip install -e ".[gui]"     # aiohttp, plus ruamel.yaml for saving edits

cd ~/Work/clabfleet
clabfleet --sudo gui                     # this machine
clabfleet gui --cluster topologies/cluster.yaml   # all cluster hosts
```

It opens your browser at a `http://localhost:8650/?token=...` link (also
printed in the terminal). Stop it with Ctrl+C.

- **Sidebar:** every `*.clab.yml` under the current directory (or each
  `--dir`), with live state, plus any other labs running on your hosts
- **Diagram:** nodes coloured by state (amber while booting), interface names on links, the host
  each node runs on (multi-host), and cross-host VXLAN links highlighted.
  Drag nodes to arrange them, scroll to zoom, double-click a node to open
  its terminal. Positions are remembered in the browser. **Save layout**
  writes them into the topology file as `graph-posX`/`graph-posY` node
  labels, so the layout travels with the file. Only those labels change:
  comments, ordering, quoting and indentation are kept.
- **YAML editor:** edit the topology file in the YAML tab. Problems are
  listed as you type, using the same checks as `clabfleet validate`.
  Save with the button or Ctrl+S. Text that is not a loadable topology
  cannot be saved, but other errors, such as a startup config file that
  does not exist yet, do not block saving. Saving is refused while a job
  runs for the lab, or if the file changed on disk since you opened it.
- **Deploy / Redeploy / Save configs / Destroy** with containerlab's output
  streamed into the Activity panel. Different labs can run jobs at the
  same time, up to four, with one job per lab. Pick any job, running or
  past, from the Activity panel's list to see its output, how long it
  took, and the time each host took. The last 50 jobs are kept in
  `.clabfleet/jobs/` under the first workspace directory, so they survive
  a restart of the GUI.
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
only and needs the random token from its start-up URL. Requests from other
websites are refused. Use `--bind` with care.

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
option as a "Roll back on failure" checkbox next to Deploy.

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

clabfleet export-live topologies/live_devices_example.yaml \
    -o imported/prod_mirror.clab.yml --lab-name prod-mirror

clabfleet --sudo deploy imported/prod_mirror.clab.yml
```

Each device becomes a node (kind from the NAPALM platform unless you set
`kind`) with its running config saved to `configs/<node>.cfg`. Each LLDP
adjacency between two inventoried devices becomes a link. Check that the
interface names match what each containerlab kind accepts before deploying.
See `topologies/live_devices_example.yaml` for the inventory format.

## Example topologies

| File | Description |
|------|-------------|
| `topologies/three_router_triangle.clab.yml` | 3x Cisco IOL routers in a full mesh with OSPF |
| `topologies/spine_leaf.clab.yml` | 2-spine 4-leaf fabric with BGP (Arista cEOS) |
| `topologies/large_campus.clab.yml` | 8-node IOL/IOL-L2 campus with placement labels for multi-host |
| `topologies/cluster.yaml` | 3-host cluster inventory |
| `topologies/live_devices_example.yaml` | Device inventory for live network export |

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAB_HOST` | `localhost` | Lab host for single-host commands |
| `CLAB_SSH_USER` | your SSH config | SSH username |
| `CLAB_SSH_KEY` | SSH agent / defaults | SSH private key |
| `CLAB_SSH_PASS` | — | SSH password (prefer keys) |
| `CLAB_SUDO` | off | Run containerlab with sudo (`1` to enable). The GUI uses `sudo -n`, so sudo for containerlab must not need a password |
| `CLAB_CAPTURE_IMAGE` | `nicolaka/netshoot:latest` | Image with `tcpdump` for capturing on nodes that have none |

## Development

Tests run automatically on every pull request (GitHub Actions, Python 3.10, 3.12 and 3.14).

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
  execute.py       # Run a command on lab nodes (clabfleet exec)
  capture.py       # tcpdump on node interfaces (clabfleet capture, GUI)
  nodes.py         # Per-kind CLI/SSH access, terminal commands, inspect parsing
  validate.py      # Topology checks (clabfleet validate)
  linkcheck.py     # Ping and UDP checks between cluster hosts
  readiness.py     # Is a node's CLI/SSH up yet (deploy --wait, GUI)
  cli.py           # CLI entrypoint
  gui/
    server.py      # aiohttp app: API, auth, terminal websockets
    state.py       # Topology discovery, running labs, deploy/destroy jobs
    terminals.py   # Local pty, SSH-channel and live capture sessions
    captures.py    # GUI packet captures: limits, pcap downloads
    editing.py     # Format-preserving saves of topology files
    static/        # Web UI (vanilla JS; xterm.js bundled in vendor/)
topologies/        # Example topologies and cluster inventory
tests/             # pytest suite
```

## License

MIT — see [LICENSE](LICENSE).
