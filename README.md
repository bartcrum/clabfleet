# EVE-NG Lab Automation

Automate deployment and teardown of EVE-NG lab topologies. Define your network
in YAML — or pull it from a running EVE-NG lab / live production network — and
deploy it with a single command.

## Features

- **Deploy** topologies from version-controlled YAML files
- **Teardown** labs cleanly (stop, wipe, delete)
- **Export** existing EVE-NG labs to reusable YAML
- **Import from live network** — connects to real devices via NAPALM, pulls
  running configs and LLDP/CDP neighbors, generates a matching topology
- **Point-to-point link shorthand** — auto-creates bridge networks
- **Startup configs** — inline in YAML or loaded from external files
- **Dry-run mode** — validate topology without touching EVE-NG

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
    ethernet: 4                       # number of ethernet interfaces
    ram: 512
    cpu: 1
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

# Deploy the mirror into EVE-NG
eve-ng-automator deploy topologies/prod_mirror.yaml --start
```

See `topologies/live_devices_example.yaml` for the device inventory format.

## Example topologies

| File | Description |
|------|-------------|
| `topologies/three_router_triangle.yaml` | 3x IOSv routers in full mesh with OSPF |
| `topologies/spine_leaf.yaml` | 2-spine 4-leaf fabric with BGP (Arista vEOS) |
| `topologies/live_devices_example.yaml` | Device inventory for live network export |

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `EVE_NG_HOST` | — | EVE-NG server hostname or IP |
| `EVE_NG_USER` | `admin` | API username |
| `EVE_NG_PASS` | `eve` | API password |
| `EVE_NG_PORT` | `443` | API port |

## Project structure

```
eve_ng_automator/
  __init__.py          # Package init
  api_client.py        # EVE-NG REST API client
  topology_schema.py   # YAML schema, validation, interface resolution
  deployer.py          # Build labs from topology definitions
  exporter.py          # Export labs from EVE-NG or live networks
  teardown.py          # Clean teardown of labs
  cli.py               # CLI entrypoint
topologies/            # Example topology YAML files
```
