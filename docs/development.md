# Development

Tests run automatically on every pull request (GitHub Actions, Python 3.11, 3.12, 3.13 and 3.14).

```bash
pip install -e ".[dev]"
pytest
```

# Project structure

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
  routing/         # OSPF/BGP/EVPN views from startup configs (clabfleet routing, GUI),
                   # live state, EVPN routes and path trace from running nodes
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
