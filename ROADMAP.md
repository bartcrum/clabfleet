# Roadmap

Candidate features for clabfleet, grouped by theme and ordered into phases.
Each item names the gap in the current code that motivates it and the
modules it would touch. Sizes are rough: **S** is an afternoon, **M** a
few days, **L** a week or more.

Phases are a suggested order: phase 1 fixes real problems and adds the
most-used commands, phase 2 deepens the cluster and GUI, phase 3 is the
longer tail. Items marked **Done** are implemented and documented in the
README; everything else is a proposal, not committed work.

## Phase 1 — foundations and daily use (done)

### 1.1 Per-lab VNI allocation (bug) — S

**Done.** VNIs used by other labs are read from the hosts' lab
directories before splitting; see "How cross-host links work" in the README.

`split_topology()` in `clabfleet/deployer.py` restarts VNI numbering at
`cluster.vni_base` on every deploy. Two labs on the same cluster therefore
get the same VNIs and the second deploy clashes with the first.

- Allocate VNIs from a per-cluster registry stored next to the cluster
  file (or under `~/.clabfleet/`), recording which lab holds which range.
- Alternative with no state: derive a per-lab offset from a hash of the
  lab name and check for overlap against running labs via `inspect`.
- Release the range on `destroy`.

### 1.2 Placement engine tests — S

**Done.** `tests/test_placement.py`.

`clabfleet/placement.py` has no dedicated test module; only
`tests/test_deployer.py` builds a `PlacementPlan` by hand. Add
`tests/test_placement.py` covering pinning, tag affinity with and without
a match, resource exhaustion errors, and the three strategies on a small
topology with fake `HostInfo` objects.

### 1.3 `clabfleet validate` — S

**Done.** Missing `startup-config`, `license` and `env-files` files are
errors, since containerlab cannot deploy without them; it also flags an
interface used by two links.

`load_topology()` already checks names, groups, kinds and link endpoints.
Expose it as a subcommand that never touches a host, and extend it to warn on:

- referenced files that do not exist (today a warning in the log only)
- kinds with no CLI/SSH access mode known to the GUI
- `lab.host` labels naming hosts absent from the `--cluster` file
- `lab.host-tags` that match no host

Touches `clabfleet/cli.py`, `clabfleet/topology.py`.

### 1.4 `clabfleet exec` — M

**Done.** Adds an `ssh` mode for VM-based kinds such as IOL, tunnelled
through the host's SSH connection. The access tables now live in
`clabfleet/nodes.py`.

There is no way to run a command on nodes. Add
`clabfleet exec <topology> "<command>" [--nodes glob] [--mode cli|shell]`
that fans out over `docker exec` on each node's host, reusing `KIND_CLI`
from `clabfleet/gui/state.py` (move it to a shared module). Print per-node
output with a host prefix in multi-host mode, and support `--json`.

Touches `clabfleet/cli.py`, `clabfleet/deployer.py`, `clabfleet/runner.py`.

### 1.5 Pre-flight image check — S

**Done.** Also adds `--skip-image-check`. Public images that containerlab
would pull on its own now need `--pull` or a manual `docker pull`.

Deploys fail late when an image is missing on a host. Before deploying,
run `docker image inspect` for every image on its target host and report
the gaps in one message. Add `--pull` to fetch them first.

Touches `clabfleet/deployer.py` (new step before `_deploy_on_host`).

### 1.6 Persist the placement plan — M

**Done.** `exec` uses the record too.

Nothing records which host each node landed on; `destroy`, `save` and
`inspect` probe every host for a lab directory instead. Write
`<lab>.placement.json` next to the topology on deploy, with the host per
node, the VNI range and the cluster file used. Read it on later commands
and in the GUI so the diagram shows the real assignment rather than a
recomputed one.

Touches `clabfleet/deployer.py`, `clabfleet/gui/state.py`,
`clabfleet/gui/static/app.js`.

## Phase 2 — cluster robustness and GUI depth (done)

### 2.1 Account for labs already running — M

**Done.** CPU of other running labs is always reserved. Their RAM is
reserved only when the inventory sets `max_ram`, because a probed
`MemAvailable` already reflects it. `status` lists running labs per host.

Placement uses `nproc` and `MemAvailable`, so CPU reserved by labs already
running is invisible. In `probe_host_resources()` (or a new step in
`LabDeployer._plan`), sum the per-kind resource estimates of containers
returned by `inspect --all` and reserve them before placing new nodes.

### 2.2 Cross-host connectivity check — S

**Done.** The UDP probe uses a short `python3` listener on the
destination. Probes that cannot run are reported as not tested.

`status` prints VTEP addresses but never tests them. Before a multi-host
deploy, run a reachability probe between every pair of VTEPs (ICMP, and a
UDP probe on `dst_port` where possible) and fail early with a clear
message. Add `clabfleet status --check-links`.

Touches `clabfleet/cluster.py`, `clabfleet/cli.py`.

### 2.3 Rollback on partial failure — M

**Done.** Opt-in with `--rollback` or the GUI checkbox. The failing host
is rolled back too. Summaries carry a `status`.

If the second host fails mid-deploy, the first is left running with
dangling VXLAN endpoints. Add `deploy --rollback` that destroys what
already deployed when any host fails, and record partial state in the
summary so the GUI can show it.

### 2.4 Readiness wait — M

**Done.** Docker health checks count first. The GUI probes in the
background so the page never waits on a booting node.

containerlab returns when containers start, but IOL and cEOS need a
minute to boot. Add a per-kind readiness probe (SSH port open, or the
kind's CLI answering `show version`) and `deploy --wait [--timeout]`.
Expose the state in the GUI as `booting` versus `ready`.

Touches `clabfleet/deployer.py`, `clabfleet/gui/state.py`,
`clabfleet/gui/static/app.js`.

### 2.5 Link-aware spread strategy — S

**Done.**

`_pick_host()` with `spread` ignores adjacency entirely. Apply the
bin-pack locality score as a tiebreaker among the least-loaded hosts, so
balanced placements still minimise cross-host links.

### 2.6 YAML editing in the browser — M

**Done.** Plain text editor. Positions are saved with ruamel.yaml so
comments and formatting are kept.

The YAML tab is read-only and node positions live only in browser local
storage. Add an editor with save, validation feedback from 1.3, and write
dragged positions back as `graph-posX` / `graph-posY` labels so layouts
travel with the file.

Touches `clabfleet/gui/server.py` (PUT endpoint), `clabfleet/gui/state.py`,
`clabfleet/gui/static/`.

### 2.7 Node logs tab — S

**Done.** Also works for stopped containers.

`docker logs --follow` per node over the existing terminal websocket,
useful for boot problems on VM-based kinds.

### 2.8 Job history and parallel jobs — M

**Done.** Up to four jobs at once, one per lab, last 50 kept. Planning
runs under a lock with in-flight deploys counted, so parallel deploys do
not share VNIs or overcommit hosts.

`JobManager` allows one job at a time and forgets everything on restart.
Keep a history file under the workspace, allow concurrent jobs on
different labs, and show elapsed time per host in the Activity panel.

## Phase 3 — longer tail

### 3.1 Config snapshots and diffs — M

**Done.** cEOS, SR Linux and cRPD; IOL saves to binary NVRAM and is
skipped. Also a Snapshot job and a per-node diff tab in the GUI.

`save` writes configs into the lab directory but nothing reads them back.
Add `snapshot` (pull configs to a local dated folder) and `diff` against
the previous snapshot or the committed `startup-config`.

### 3.2 Lab templates — M

**Done.** Templates `spine-leaf`, `ring` and `campus` for `arista_ceos`,
`cisco_iol` and `linux`, with /31 links and per-tier loopbacks. No GUI
action yet. The `linux` kind was tested on a live deploy. The cEOS and IOL
configs are only validated.

`clabfleet new spine-leaf --spines 2 --leaves 4 --kind arista_ceos` that
generates nodes, links and startup configs from a small template set.

### 3.3 Live link and node state in the diagram — M

**Done.** Interface state is read from `/sys/class/net` rather than
`ip link`, so it works in any container with `sh`. Probes run in the
background only for labs open in a browser, at most once per lab every
5 seconds however many tabs are open. Served at `/api/live/<topology>`.

Nodes are coloured by container state only. Poll `ip link` inside
containers to show links down, and `docker stats` for CPU and memory per
node.

### 3.4 Packet capture from the diagram — L

**Done.** Also `clabfleet capture` on the CLI. Nodes without `tcpdump` get
a helper container in their network namespace. GUI captures always have a
time limit, and tcpdump is killed inside the container when they end.

Click a link, pick a side, and stream `tcpdump` from the container's
network namespace to a download or a live decode. Fits the terminal
websocket plumbing in `clabfleet/gui/terminals.py`.

### 3.5 Cluster view page — S

A hosts page with per-host capacity bars, running labs, and which labs
span hosts, built on `Workspace.host_status()`.

### 3.6 Multi-user and remote access — L

**Done.** `clabfleet user add/list/remove/rotate` manages a users file
with operator and viewer roles; only token hashes are stored. Viewers are
read-only by default deny: any route that is not a plain GET needs an
operator unless marked otherwise. Logins, jobs, edits and terminal
sessions go to a JSON Lines audit log. `--tls-cert`/`--tls-key` serve
HTTPS, and a non-loopback `--bind` without TLS is refused unless
`--insecure-http` is given. Without a users file the GUI keeps its
single-token mode.

The GUI is single-token and localhost only. For a shared jump host: named
users with per-user tokens, an audit log of terminal sessions and
actions, and TLS.

### 3.7 Live network import improvements

**Done.** All six items below. Tested against recorded NAPALM data and
fake NetBox/Nautobot APIs only; no real devices were involved.

The exporter in `clabfleet/exporter.py` produces a topology that usually
needs hand edits before it deploys.

- **Interface name mapping — M.** **Done.** `clabfleet/ifmap.py`: per-kind
  naming for IOL, cEOS, N9Kv, SR Linux, XRd, XRv9k, vJunos/vSRX, C8000v,
  SR OS and linux; 1:1 where the port exists, else the next free port in a
  deterministic order, within port limits. Links and configs both, with a
  per-node report. Per-kind rewrite tables (for example
  `GigabitEthernet0/0/1` → `Ethernet0/1` for IOL) applied to both links
  and the saved configs.
- **Config sanitising — M.** **Done.** `clabfleet/sanitise.py`, IOS-like
  dialects and Junos; adds an admin/admin login, reports counts only.
  `--sanitise` strips or replaces password hashes, SNMP communities, AAA
  servers and management addresses.
- **Image mapping — S.** **Done.** `images:` (and `kinds:`) rules in the
  inventory. Map platform and version to a real image via the inventory
  file instead of emitting `REPLACE-ME/<kind>:latest`.
- **Incremental re-sync — M.** **Done.** `clabfleet/resync.py`: report only
  by default, `--apply` edits in place keeping hand edits, `--prune` to
  delete. Diff the new LLDP graph against the existing topology and report
  added, removed and changed nodes and links instead of overwriting.
- **Neighbour-only devices — S.** **Done.** `--include-neighbours`.
  Optionally add LLDP neighbours that are not in the inventory as
  placeholder `linux` nodes.
- **Other inventory sources — M.** **Done.** `clabfleet/inventory.py`:
  NetBox and Nautobot REST (stdlib only) and Ansible YAML/INI inventories.
  Accept a NetBox/Nautobot query or an Ansible inventory as the device list.

## Not planned

- Replacing containerlab's own VXLAN handling with a custom overlay.
- A full topology designer (drag nodes and links from a palette); YAML
  editing in 2.6 covers the need with far less code.
