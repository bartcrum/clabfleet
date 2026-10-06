# clabfleet

Deploy and tear down [containerlab](https://containerlab.dev) topologies on
one host — or spread a large topology across a **cluster of hosts** with
automatic, resource-aware placement and VXLAN links between hosts. You can
also generate a topology from a live production network.

Topology files are **standard containerlab files** (`*.clab.yml`). Anything
containerlab accepts works here, and every file in `topologies/` still
deploys with plain `containerlab deploy`.

![The web GUI's rack view of the EVPN MLAG lab: one rack for the host, a device per node with its ports and link LEDs, and a cable per link coloured by its role](https://raw.githubusercontent.com/bartcrum/clabfleet/main/docs/images/rack-view.png)

*The [web GUI](docs/gui.md)'s rack view of `topologies/evpn_mlag.clab.yml`,
running on one host.*

More screens: [a tour of the GUI](docs/screenshots.md).

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

## Install

clabfleet runs on Linux: on the machine that runs the labs, or on one that
reaches the lab hosts over SSH. On Windows, use WSL 2
([below](#on-windows-wsl-2)).

**1. Docker and containerlab**, on every machine that will run labs:

```bash
# Docker Engine: https://docs.docker.com/engine/install/
sudo usermod -aG docker "$USER"     # then log out and back in
bash -c "$(curl -sL https://get.containerlab.dev)"

docker run --rm hello-world         # both must work without sudo
containerlab version
```

**2. clabfleet.** It needs Python 3.11 or newer (`python3 --version`);
Ubuntu 24.04 and Debian 12 have it.

```bash
pipx install "clabfleet[gui] @ git+https://github.com/bartcrum/clabfleet"
clabfleet --version
```

No pipx? `sudo apt install pipx && pipx ensurepath` on Debian and Ubuntu,
then open a new terminal. Or use a virtual environment:

```bash
python3 -m venv ~/.venvs/clabfleet
~/.venvs/clabfleet/bin/pip install "clabfleet[gui] @ git+https://github.com/bartcrum/clabfleet"
echo 'export PATH="$HOME/.venvs/clabfleet/bin:$PATH"' >> ~/.bashrc    # then open a new terminal
```

A plain `pip install` outside a virtual environment is refused on current
Debian, Ubuntu and Arch ("externally-managed-environment"). `[gui]` is the
web GUI; add `ldap` for LDAP / Active Directory logins and `napalm` for
the import from a live network, as in `clabfleet[gui,ldap]`.

**3. Root for containerlab.** containerlab needs root to wire a lab up.
`--sudo` runs it through sudo, which must not ask for a password in the
middle of a job: run `sudo -v` first, as below, or set it up once so that
it never asks, see
[First-time setup](docs/troubleshooting.md#first-time-setup-root-for-containerlab).

**4. A first lab.** This one uses a public image, so there is nothing to
import:

```bash
mkdir -p ~/labs && cd ~/labs
clabfleet new ring --kind linux -o ring.clab.yml    # four Linux routers in a ring
sudo -v && clabfleet --sudo deploy ring.clab.yml --pull --wait
clabfleet --sudo inspect ring.clab.yml

sudo -v && clabfleet --sudo gui     # http://localhost:8650; Ctrl+C stops it
clabfleet --sudo destroy ring.clab.yml
```

The GUI prints its address and, the first time, a password for the user
`admin`, which you change at the first login.

Labs of Arista cEOS or Cisco IOL nodes need their images, which are not
public: download or build them and load them into Docker on each lab
host. clabfleet checks that a lab's images are there before it deploys
([Node images](docs/cli.md#node-images)). The files in `topologies/` are
such labs; get them with a clone of this repository.

To work on clabfleet itself: `git clone https://github.com/bartcrum/clabfleet`,
then `pip install -e ".[gui,dev]"` in a virtual environment.
[CHANGELOG.md](CHANGELOG.md) says what each release changed.

### On Windows (WSL 2)

clabfleet runs inside a WSL 2 Linux distribution; you use the GUI from
your Windows browser. These steps follow Microsoft's and containerlab's
instructions and have not been tried by the maintainers on Windows yet:
please report what does not match.

1. **WSL 2 with Ubuntu 24.04.** In PowerShell as administrator:

   ```powershell
   wsl --install -d Ubuntu-24.04
   wsl -l -v          # VERSION must be 2; WSL 1 cannot run Docker
   ```

2. **systemd**, which Docker's service needs. In Ubuntu, `systemctl
   is-system-running` should answer `running` or `degraded`. If it does
   not, put these two lines in `/etc/wsl.conf` and run `wsl --shutdown` in
   PowerShell, then open Ubuntu again:

   ```ini
   [boot]
   systemd=true
   ```

3. **Follow steps 1 to 4 above inside Ubuntu.** Install Docker Engine in
   Ubuntu itself, as containerlab's guide for WSL recommends, rather than
   using Docker Desktop.
4. **Keep labs in the Linux home** (`~/labs`), not under `/mnt/c/...`:
   Windows folders are slow and do not keep Linux file permissions.
5. **The GUI:** start it with `clabfleet --sudo gui --no-browser` and open
   `http://localhost:8650` in your Windows browser.
6. **Memory:** WSL 2 gives Linux half of the machine's memory by default.
   For bigger labs raise it in `%UserProfile%\.wslconfig`, then
   `wsl --shutdown`:

   ```ini
   [wsl2]
   memory=16GB
   ```

7. **After Windows restarts** or `wsl --shutdown`, a lab that was running
   is half up and has to be recreated, see
   [After a reboot the lab is half up](docs/troubleshooting.md#after-a-reboot-the-lab-is-half-up).
   Node images you downloaded in Windows are under `/mnt/c/Users/<you>/Downloads`.

## Everyday commands

```bash
clabfleet --sudo deploy lab.clab.yml      # runs containerlab against the file in place;
                                          # the lab directory clab-<name>/ is created next to it
clabfleet --sudo inspect lab.clab.yml     # what is running?
clabfleet --sudo save lab.clab.yml        # save running configs into the lab directory
clabfleet --sudo stop lab.clab.yml        # save the configs and remove the containers;
                                          # the next deploy starts from the saved configs
clabfleet --sudo destroy lab.clab.yml     # tear it down (--keep-lab-dir keeps saved configs)
```

Leave `--sudo` out where containerlab needs no sudo (way A of the
First-time setup); clabfleet never uses sudo unless asked. For lab hosts
reached over SSH and for clusters, see [Single remote host](docs/cli.md#single-remote-host)
and [Multi-host cluster deployment](docs/cluster.md).

These print a short summary; add `--json` for the full result, for scripts.
`CLAB_SUDO=1` in the environment does what `--sudo` does.

## Documentation

| Guide | What it covers |
|-------|----------------|
| [Command line](docs/cli.md) | Waiting for nodes to boot, running commands on nodes, config snapshots and diffs, packet capture, validation, the routing view, path trace, lab templates, node images, a remote host, environment variables |
| [A tour of the GUI](docs/screenshots.md) | Screenshots: the diagram, rack view, routing, path trace, terminals, hosts, the drawing builder, logging in |
| [Web GUI](docs/gui.md) | The diagram and rack view, terminals, captures, editing topologies, users and roles, OIDC and LDAP / Active Directory logins, sessions |
| [Multi-host cluster deployment](docs/cluster.md) | The cluster inventory, checking hosts, placement strategies and pinning, how links between hosts work |
| [Import from live network](docs/import-live.md) | Device sources, kinds and images, interface names, sanitising configs, re-syncing |
| [First-time setup and troubleshooting](docs/troubleshooting.md) | Root for containerlab without password prompts, failed deploys, a host that stopped answering, a lab after a reboot, lost changes after Redeploy, lost admin password, directory logins |
| [Development](docs/development.md) | Running the tests, project structure |
| [Releasing](docs/releasing.md) | Cutting a release: version, changelog, tag, PyPI |

## Example topologies

| File | Description |
|------|-------------|
| `topologies/three_router_triangle.clab.yml` | 3x Cisco IOL routers in a full mesh with OSPF |
| `topologies/spine_leaf.clab.yml` | 2-spine 4-leaf fabric with BGP (Arista cEOS) |
| `topologies/large_campus.clab.yml` | 8-node IOL/IOL-L2 campus with placement labels for multi-host |
| `topologies/evpn_fabric.clab.yml` | 2-spine 4-leaf EVPN/VXLAN fabric (Arista cEOS): eBGP underlay, EVPN overlay, L2 and L3 VNIs, 4 Linux hosts |
| `topologies/evpn_mlag.clab.yml` | The same fabric with the leaves as two MLAG pairs (peer-links, one shared VTEP per pair) and 4 hosts dual-homed with LACP bonds |
| `topologies/cluster.yaml` | 3-host cluster inventory |
| `topologies/live_devices_example.yaml` | Device inventory for live network export |

## License

MIT — see [LICENSE](LICENSE).
