# Changelog

What changed in each release of clabfleet. Versions follow
[semantic versioning](https://semver.org/); while the version is below
1.0, a minor release may change behaviour.

## 0.2.0

Not released yet. The first release with a version tag and a package.

### Deploying labs

- Deploy, destroy, stop, save and inspect containerlab topologies on this
  machine, a remote host over SSH, or a cluster of hosts, with links
  between hosts as VXLAN.
- Placement by strategy (bin-pack, spread), pinning and host tags, counting
  the labs that already run; placement is recorded next to the topology.
- Checks before a deploy: missing images (`--pull` to fetch them), links
  between hosts, the topology itself (`clabfleet validate`).
- `--wait` for nodes to boot, `--rollback` when a host fails, and a clear
  message when containerlab cannot get root.

### Working with a running lab

- `exec` on many nodes, config snapshots and diffs, packet capture.
- The routing design read from startup configs (OSPF, BGP, EVPN, MLAG),
  and its live state on running nodes.
- `clabfleet trace`: the path traffic takes, hop by hop, from the nodes'
  own tables, through VRFs, equal-cost paths and VXLAN.

### Building labs

- Templates (`clabfleet new`): spine-leaf, ring and campus.
- Import from a live network (`export-live`): NAPALM, NetBox, Nautobot and
  Ansible inventories, interface name mapping, config sanitising, re-sync.

### Web GUI

- Diagram and rack view with live link and node state, terminals, logs,
  captures, a YAML editor and a drawing builder.
- Routing, Health, Events, Run and Trace panels; a Hosts page with each
  host's capacity and labs.
- Named users with operator and viewer roles, an audit log, TLS, and
  logins through OpenID Connect or LDAP / Active Directory.

### Security

- A trace destination can no longer carry a command to a node. Before, a
  viewer could run commands inside a running Linux node through the
  trace's destination field.
