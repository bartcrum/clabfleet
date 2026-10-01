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
- **Dry-run** — see the placement plan and the per-host topology files
  without deploying
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
pip install -e ".[gui]"

cd ~/Work/clabfleet
clabfleet --sudo gui                     # this machine
clabfleet gui --cluster topologies/cluster.yaml   # all cluster hosts
```

It opens your browser at a `http://localhost:8650/?token=...` link (also
printed in the terminal). Stop it with Ctrl+C.

- **Sidebar:** every `*.clab.yml` under the current directory (or each
  `--dir`), with live state, plus any other labs running on your hosts
- **Diagram:** nodes coloured by state, interface names on links, the host
  each node runs on (multi-host), and cross-host VXLAN links highlighted.
  Drag nodes to arrange them (positions are remembered per topology),
  scroll to zoom, double-click a node to open its terminal
- **Deploy / Redeploy / Save configs / Destroy** with containerlab's output
  streamed into the Activity panel
- **Terminals** in tabs at the bottom:
  - **CLI**: the node's own CLI via `docker exec` (`Cli` on cEOS, `sr_cli`
    on SR Linux, `cli` on cRPD)
  - **Shell**: a shell inside the container
  - **SSH**: `ssh` to the node's management IP. This is the CLI for VM-based
    kinds such as Cisco IOL. It needs a login on the node: containerlab's
    default configs create `admin`/`admin`, but your own `startup-config`
    must include a user
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
  vni_base: 1000              # each cross-host link gets the next VNI
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

### Placement strategies

| Strategy | Behaviour |
|----------|-----------|
| `bin-pack` | Fill each host before moving to the next, keeping neighbours together. Fewest cross-host links. **(default)** |
| `spread` | Distribute nodes evenly across hosts. |
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
  cli.py           # CLI entrypoint
  gui/
    server.py      # aiohttp app: API, auth, terminal websockets
    state.py       # Topology discovery, running labs, deploy/destroy jobs
    terminals.py   # Local pty and SSH-channel terminal sessions
    static/        # Web UI (vanilla JS; xterm.js bundled in vendor/)
topologies/        # Example topologies and cluster inventory
tests/             # pytest suite
```

## License

MIT — see [LICENSE](LICENSE).
