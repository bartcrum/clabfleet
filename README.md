# EVE-NG Lab Automation

Automate deployment and teardown of EVE-NG lab topologies. Define your network
in YAML — or pull it from a running EVE-NG lab / live production network — and
deploy it with a single command. Supports **multi-host clusters** for large
topologies that exceed a single server's capacity.

## Features

- **Deploy** topologies from version-controlled YAML files
- **Multi-host clusters** — spread nodes across multiple EVE-NG servers with
  automatic resource-aware placement and GRE/VXLAN tunnels for cross-host links
- **Teardown** labs cleanly (stop, wipe, delete) — single or multi-host
- **Export** existing EVE-NG labs to reusable YAML
- **Import from live network** — connects to real devices via NAPALM, pulls
  running configs and LLDP/CDP neighbors, generates a matching topology
- **Placement strategies** — bin-pack, spread, or resource-based node placement
- **Host pinning & tag affinity** — control which nodes land on which servers
- **Point-to-point link shorthand** — auto-creates bridge networks
- **Startup configs** — inline in YAML or loaded from external files
- **Dry-run mode** — validate topology and see placement plan without deploying

## Quick start

```bash
pip install -e .

# Set connection details (or use CLI flags)
export EVE_NG_HOST=192.168.1.100
export EVE_NG_USER=admin
export EVE_NG_PASS=eve

# Deploy a topology
eve-ng-automator -v deploy topologies/three_router_triangle.yaml --start

# Check server status
eve-ng-automator status

# List available templates/images
eve-ng-automator list-templates

# Export a running lab to YAML
eve-ng-automator export /three-router-triangle -o exported.yaml

# Tear it down
eve-ng-automator teardown /three-router-triangle
```

## Multi-host cluster deployment

When a single EVE-NG server doesn't have enough CPU/RAM, spread the topology
across multiple servers.

### 1. Define your cluster

Create a cluster inventory listing your EVE-NG servers and their capacity:

```yaml
# cluster.yaml
cluster:
  tunnel_mode: "gre"           # gre | vxlan
  tunnel_pnet: "pnet9"         # cloud interface for inter-host tunnels

hosts:
  - name: "eve-1"
    host: "192.168.1.101"
    username: "admin"
    password: "eve"
    max_cpu: 16
    max_ram: 65536             # MB
    tags: ["core"]

  - name: "eve-2"
    host: "192.168.1.102"
    username: "admin"
    password: "eve"
    max_cpu: 16
    max_ram: 65536
    tags: ["distribution"]

  - name: "eve-3"
    host: "192.168.1.103"
    username: "admin"
    password: "eve"
    max_cpu: 8
    max_ram: 32768
    tags: ["access"]
```

### 2. Check cluster status

```bash
eve-ng-automator cluster-status topologies/cluster.yaml
```

### 3. Deploy across the cluster

```bash
# Auto-place nodes based on resources (bin-pack minimises cross-host links)
eve-ng-automator deploy topologies/large_campus.yaml \
    --cluster topologies/cluster.yaml \
    --strategy bin-pack --start -vv

# Preview placement without deploying
eve-ng-automator deploy topologies/large_campus.yaml \
    --cluster topologies/cluster.yaml --dry-run

# Tear down from all hosts
eve-ng-automator teardown topologies/large_campus.yaml \
    --cluster topologies/cluster.yaml
```

### 4. Control node placement

In your topology YAML, you can pin nodes to specific hosts or use tag affinity:

```yaml
nodes:
  - name: "Core-1"
    template: "csr1000v"
    host: "eve-1"              # pin to a specific host

  - name: "Access-1"
    template: "iosvl2"
    host_tags: ["access"]      # prefer hosts tagged "access"

  - name: "Server-1"
    template: "linux"
    # no host/host_tags → auto-placed by the placement engine
```

### Placement strategies

| Strategy | Behaviour |
|----------|-----------|
| `bin-pack` | Fill each host before moving to the next. Minimises cross-host tunnels. **(default)** |
| `spread` | Distribute nodes evenly across hosts. Balanced load. |
| `resource` | Always pick the host with the most free resources. |

### How cross-host links work

When two connected nodes land on different servers, the automator:

1. Creates a GRE (or VXLAN) tunnel between the two EVE-NG hosts via SSH
2. Bridges the tunnel into a `pnet` (cloud) network on each host
3. Connects the node interfaces to that cloud network

This is transparent — the nodes see a normal L2 link. The tunnel is torn down
automatically on teardown.

```
Host eve-1                          Host eve-2
┌──────────────────┐                ┌──────────────────┐
│  Core-1          │                │  Dist-1          │
│   Gi2 ───────────┤                ├─────────── Gi0/0 │
│                  │                │                  │
│  pnet9 ──────────┼── GRE tunnel ──┼────────── pnet9  │
└──────────────────┘                └──────────────────┘
```

## Topology YAML format

```yaml
lab:
  name: "my-lab"
  description: "Lab description"
  author: "netops"
  path: "/"

nodes:
  - name: "R1"
    template: "vios"                  # EVE-NG template name
    image: "vios-adventerprisek9..."  # specific image (optional)
    type: "qemu"                      # qemu | iol | dynamips | docker
    ethernet: 4                       # number of ethernet interfaces
    ram: 512
    cpu: 1
    host: "eve-1"                     # pin to cluster host (optional)
    host_tags: ["core"]               # tag affinity (optional)
    startup_config: |                 # inline config
      hostname R1
      ...
    # or: startup_config_file: "configs/R1.cfg"

networks:
  - name: "Mgmt"
    type: "pnet1"                     # bridge, ovs, pnet0-pnet9

links:
  # Explicit network reference
  - node: "R1"
    interface: "Gi0/0"
    network: "Mgmt"

  # Point-to-point shorthand (auto-creates a bridge)
  - endpoints:
      - node: "R1"
        interface: "Gi0/1"
      - node: "R2"
        interface: "Gi0/1"
```

## Import from live network

Create a device inventory YAML and export the topology:

```bash
# Install NAPALM support
pip install -e ".[napalm]"

# Export from production devices
eve-ng-automator export-live topologies/live_devices_example.yaml \
    -o topologies/prod_mirror.yaml --lab-name "prod-mirror"

# Deploy the mirror into EVE-NG (single host)
eve-ng-automator deploy topologies/prod_mirror.yaml --start

# Or deploy across a cluster if it's large
eve-ng-automator deploy topologies/prod_mirror.yaml \
    --cluster topologies/cluster.yaml --start
```

See `topologies/live_devices_example.yaml` for the device inventory format.

## Example topologies

| File | Description |
|------|-------------|
| `topologies/three_router_triangle.yaml` | 3x IOSv routers in full mesh with OSPF |
| `topologies/spine_leaf.yaml` | 2-spine 4-leaf fabric with BGP (Arista vEOS) |
| `topologies/large_campus.yaml` | 9-node campus network for multi-host deployment |
| `topologies/cluster.yaml` | 3-server cluster inventory |
| `topologies/live_devices_example.yaml` | Device inventory for live network export |

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `EVE_NG_HOST` | — | EVE-NG server hostname or IP (single-host mode) |
| `EVE_NG_USER` | `admin` | API username |
| `EVE_NG_PASS` | `eve` | API password |
| `EVE_NG_PORT` | `443` | API port |

## Project structure

```
eve_ng_automator/
  __init__.py          # Package init
  api_client.py        # EVE-NG REST API client
  topology_schema.py   # YAML schema, validation, interface resolution
  deployer.py          # Build labs from topology definitions (single host)
  distributed.py       # Multi-host deployment orchestrator
  cluster.py           # Cluster inventory and host management
  placement.py         # Resource-aware node placement engine
  interconnect.py      # Cross-host GRE/VXLAN tunnel manager
  exporter.py          # Export labs from EVE-NG or live networks
  teardown.py          # Clean teardown of labs
  cli.py               # CLI entrypoint
topologies/            # Example topology and cluster YAML files
```
