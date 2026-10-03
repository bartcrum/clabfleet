# Multi-host cluster deployment

When one server doesn't have enough CPU/RAM, spread the topology across
several.

## 1. Define your cluster

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

## 2. Check the hosts

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

## 3. Deploy across the cluster

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

## 4. Control node placement

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

## Placement strategies

| Strategy | Behaviour |
|----------|-----------|
| `bin-pack` | Fill each host before moving to the next, keeping neighbours together. Fewest cross-host links. **(default)** |
| `spread` | Distribute nodes evenly across hosts. When hosts are equally loaded, a node joins the host where most of its neighbours already are. |
| `resource` | Always pick the host with the most free resources. |

## How cross-host links work

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
