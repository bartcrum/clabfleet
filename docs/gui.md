# Web GUI

```bash
pip install -e ".[gui]"     # aiohttp, plus ruamel.yaml for saving edits
pip install -e ".[gui,ldap]"  # with LDAP / Active Directory logins (ldap3)

clabfleet --sudo gui                     # this machine; topologies under the current folder
clabfleet --sudo gui --dir ~/labs        # topologies somewhere else
clabfleet gui --cluster topologies/cluster.yaml   # all cluster hosts
```

Deploying needs root for containerlab, and the GUI cannot type a sudo
password: set that up once, as in
[First-time setup](troubleshooting.md#first-time-setup-root-for-containerlab).
The GUI warns at start-up if deploys would fail. For anything else that
goes wrong, see [Troubleshooting](troubleshooting.md#troubleshooting).

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
  its kind, with its status on the glyph's corner, and under its name the
  vendor as a small badge with the product next to it (Arista · cEOS). Zoom with the wheel, the
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
  new lab (blank, or a template), each card with a thumbnail of its nodes
  and links (labs of up to 60 nodes). Destroy and Redeploy
  ask in a dialog that says what will be removed; labs of 10 or more
  nodes ask you to type the lab name.
- **Hosts page:** click a host in the top bar (or Ctrl+K, "Hosts page")
  for a card per lab host: whether it answers, its containerlab version,
  VTEP address and tags, and what is on it.
  - **Memory** is measured on the host: in use of total.
  - **vCPU** is what placement counts for the running lab nodes there (a
    node's `lab.cpu` label, else an estimate for its kind) of what the
    host may use (`max_cpu` in the cluster file, else its CPUs). It is
    not CPU load. The bar turns amber from 80 % and says "over capacity"
    past the limit.
  - **RAM for labs** is shown when the cluster file sets `max_ram`, which
    is when placement reserves running labs' RAM against it.
  - **Labs:** each lab with nodes on the host, how many of them run, what
    is counted for them, and the other hosts the lab is also on. The
    line at the top says how many labs span hosts. Click a lab to open
    it.

  The page follows the same reads as the rest of the GUI: hosts every 30
  seconds, labs every 5. A host that does not answer shows the error.
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
  to jump to it. As in Health, the button at the end of a row copies its
  line, with the full date and time in UTC, and **Copy all** copies the
  list, newest first.
- **Traffic:** links carrying traffic are drawn thicker (from 10 kb/s, by
  orders of magnitude), with the rate each way in the link's tooltip and
  inspector, from the interfaces' byte counters read with the link state.
- **What if:** on a running lab, an operator can shut and un-shut a link
  end (the interface goes down inside the node, as on a shut port), shut
  or restore all of a node's links, or freeze and resume a node (`docker
  pause`: its links stay up but it stops answering, like a hung box, so
  its neighbours time out). The inspector offers them on links and nodes;
  disruptive ones ask first, all are audited (`whatif`), and nothing is
  saved to any config. Watch the Routing tab in Live mode reconverge.
- **Trace:** the Trace tab follows traffic from a node to another node or
  an address through the running lab's tables, hop by hop: routes on cEOS
  (VRFs included), IOS kinds and Linux hosts, every equal-cost branch, and
  on an EVPN fabric the VXLAN hop to the VTEP (from the route, or for
  bridged traffic the MAC and VXLAN tables, the EVPN MAC/IP routes, or
  the VLAN's flood list). A port-channel or a Linux bond branches to its
  member links, and an MLAG pair's shared VTEP to both leaves. The path
  is highlighted on the Diagram, VXLAN hops as arcs. Routed traffic stays
  in the VRF it arrives in, and the trace is one way: it says nothing
  about the way back. It only reads;
  viewers can use it too. The same walk from a terminal: `clabfleet
  trace` ([Trace a path](cli.md#trace-a-path)).
- **Notes and boxes:** operators can put sticky notes and labelled boxes
  ("DC1", "tenant A") on the Diagram with Note and Box: drag to move, drag
  a box's corner to resize, double-click to edit, Delete to remove.
  Everyone sees them. They are kept next to the topology in
  `<file>.notes.json` (not in the topology, which containerlab checks
  against its schema), so copy both to move a lab.
- **Host lanes:** in a multi-host lab, Host lanes groups the nodes into one
  tinted lane per host, so the links between hosts (VXLAN) are the ones
  crossing lane borders. Turning it off puts the nodes back; Save layout
  keeps the lanes.
- **Rack view:** Racks on the Diagram draws each host as a rack with the
  nodes in its slots, one port per interface the topology uses, and each
  link as a cable between those ports. Links inside a host loop through the
  cable manager beside its rack; links between hosts run through the cable
  tray on top with their VNI. Cable colour is the link's role (fabric,
  parallel pair, host access, outside the lab), port LEDs its live state.
  Clicking, terminals and capture work as in the logical view; the choice is
  remembered in the browser.
- **Minimap:** labs of 16 nodes or more get a small map of the whole
  diagram above the zoom buttons; click or drag in it to move there.
- **Narrow screens:** on a tablet or phone the sidebar is a rail that opens
  over the page, the secondary lab actions move into the ⋯ menu, the
  inspector is a bottom sheet and an open dock takes the whole screen.
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
  CPU and memory, its interfaces in one table, the protocols it runs
  (click one to see it on the Routing tab), and its terminal and Config
  diff buttons. The interface table lists every link end with its peer
  and live state (click one to select the link) and, for a node whose
  startup config could be read, every configured interface (loopbacks,
  VLAN interfaces, port-channels and the VXLAN interface too) with its
  addresses and VRF. Under each interface is what runs on it: its OSPF
  area and adjacency, its BGP sessions, the VTEP and its source, the MLAG
  peer-link and ports, its port-channel and access VLAN, and "shut" if
  the config shuts it down. A session or adjacency is marked ✓ or ✕ from
  the live state, and a click shows it on the Routing tab. Addresses and
  protocols are the startup config's, as on the Routing tab; nodes
  without a readable config (Linux hosts) list their links only. A link
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
  node positions as the Diagram tab. Pick OSPF, BGP, EVPN or MLAG at the top.
  OSPF colours adjacencies by area (and shades each area when there are
  several); BGP shades each AS and draws IPv4 and EVPN sessions, dashed for
  iBGP and red for sessions configured on one side only; EVPN shows the
  EVPN sessions (control plane) and the VXLAN tunnels between VTEPs (data
  plane), either or both, for all VNIs or one. Click a node for its
  router-id, interfaces, sessions or VNIs, or an edge for both ends.
  MLAG shades each pair, draws its peer-link heavy and links each
  dual-homed host to both leaves; a leaf's card lists its ports.
  On a deployed lab a cEOS VTEP's card can also read what it has
  **learned**: the hosts (EVPN type-2 MAC/IP routes) and prefixes
  (type-5) on each VNI, and which VTEP each came from.
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
  To take a problem elsewhere (a ticket, a chat), the button at the end
  of its row copies that line, and **Copy all** copies the list as it is
  filtered, under a line naming the lab and the time. If the browser does
  not let the page copy, the text is shown, selected, to copy by hand.
- **Lab header:** under the lab's name a status strip shows the nodes
  running (or ready, while booting), the hosts used, and the OSPF and BGP
  sessions up once the Routing tab has read them in Live mode. Click a
  stat to open the tab it comes from. The ⧉ button copies the topology
  file's path.
- **Deploy** while the lab is not deployed; **Redeploy / Save configs /
  Snapshot / Stop** once it is, and **Destroy** in the ⋯ menu, with
  containerlab's output streamed into the Activity panel. **Stop** turns
  the lab down to free memory and disk: it saves every node's running
  config, then removes the containers and keeps the lab directory (a few
  MB), so the next Deploy starts from the saved configs. If saving fails
  on a host, nothing is removed. A stopped lab says so: its badge reads
  "stopped · configs saved 07:20", the button becomes **Deploy from saved
  configs**, and **Discard saved configs** in the ⋯ menu removes the lab
  directory, so the next Deploy starts from the topology again (shown for
  a lab that runs on this machine alone). **Destroy** also deletes the lab
  directory. `clabfleet stop` does the same from the command line. Above
  the output, Activity
  shows each host's steps (plan, images, deploy, links, ready) and how
  long each took; a finished job pops up with View and Open lab.
- **Run on nodes:** the Run tab (operators) runs one command on every node
  matching names or globs (`Leaf-*`), like `clabfleet exec`: the CLI on
  cEOS and SR Linux, SSH on VM kinds, else a shell. Show the outputs as a
  list, side by side, or as diffs against the first node, to spot the odd
  one out. Commands are audited (`exec`). Different labs can run jobs at the
  same time, up to four, with one job per lab. Pick any job, running or
  past, from the Activity panel's list to see its output, how long it
  took, and the time each host took. **Cancel job** (operators) ends a
  running job that is stuck, for instance on a host that stopped
  answering: the GUI stops waiting and the lab is free for another job.
  Nothing is undone, so look at the lab afterwards
  ([A lab host stopped answering](troubleshooting.md#a-lab-host-stopped-answering)).
  The last 50 jobs are kept in
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
  [Capture packets](cli.md#capture-packets) for how it reaches the node.
- Nodes on remote hosts are reached over the host's SSH connection, so the
  SSH user there needs Docker access (the `docker` group)

Security: terminals are shell access, so the GUI listens on `127.0.0.1`
only and every request needs a login. Requests from other websites are
refused. Use `--bind` with care, and set the admin password before you
do: until then anyone who has the initial password can log in as admin and
choose the password. See [Login and sessions](#login-and-sessions) below.

## Several users on a shared lab server

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
  Events: `login` (method: password, token, oidc or ldap; for a
  directory login also the groups that gave the role and the directory's
  id of the user), `login_failed` (with the
  user name tried), `login_throttled` (once per address or user name and
  minute after 5 failed logins), `logout`, `directory_login_ended` (an
  LDAP user is gone or has no role any more), `password_changed`,
  `password_change_failed`, the `user_*` events above, `denied`, `job_started`
  (action, topology, options), `job_cancelled`, `job_finished` (status, seconds),
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

## Logging in through your directory

People can also log in with the accounts they already have: through an
OAuth 2.0 / OpenID Connect provider (Microsoft Entra ID, AD FS, Okta,
Keycloak, Google), with their LDAP / Active Directory name and password,
or both. Their groups in the directory give them their role, so access is
managed there and nobody has to be added to the GUI.

Put the settings in `~/.clabfleet/directory.yaml` (or pass `--directory
FILE`) and restart the GUI. The file holds a client secret or a bind
password: keep it mode 0600. Like the users file, it is refused when other
users could change it.

```yaml
oidc:
  issuer: https://login.microsoftonline.com/<tenant-id>/v2.0
  client_id: 0b0c...
  client_secret_env: CLABFLEET_OIDC_SECRET   # or client_secret: ...
  label: Entra ID                  # the button says "Log in with Entra ID"
  roles:
    operator: [netlab-admins]      # values of the groups claim
    viewer: [netlab-readers]

ldap:
  url: ldaps://dc1.example.com
  bind_dn: CN=svc-clabfleet,OU=Service,DC=example,DC=com
  bind_password_env: CLABFLEET_LDAP_PASSWORD # or bind_password: ...
  user_base: DC=example,DC=com
  roles:
    operator: ["CN=NetLab Admins,OU=Groups,DC=example,DC=com"]
    viewer: ["CN=NetLab Readers,OU=Groups,DC=example,DC=com"]
```

```bash
clabfleet directory check                 # reads the file, reaches the provider and the LDAP server
clabfleet directory check --user alice    # LDAP: alice's entry, and the role her groups give her
```

- **Roles:** someone in an `operator` group is an operator, else someone
  in a `viewer` group is a viewer, and anyone else is refused, even with
  the right password. Operators are effectively root on the lab hosts
  (see the security notes below): map that role to a group whose
  membership is itself well guarded.
- **Local users stay.** The users file works as before next to the
  directory, so the admin can still log in when the directory is down,
  and token users (scripts) are unaffected. A directory user whose name
  is also a local user's is refused: the local user has that name. Remove
  the local user to let the directory one in. Directory users are not
  listed under **Users**; there is nothing to manage for them here.
- **OIDC:** register the GUI with the provider as a web application with
  the redirect URI `https://<server>:8650/login/oidc/callback` (printed
  at start-up; with `--public-url`, that address). The login page gets a
  **Log in with ...** button. The GUI uses the authorization code flow
  with PKCE, checks the ID token's signature against the provider's keys
  and its issuer, audience, expiry and nonce (with the PyJWT library;
  only public-key signatures are accepted), and only finishes a login
  in the browser that started it. A public client without a secret works
  too (leave `client_secret` out).

  | Setting | Default | |
  |---|---|---|
  | `issuer` | | The provider's issuer URL (https). Its endpoints are read from `<issuer>/.well-known/openid-configuration` |
  | `client_id`, `client_secret` or `client_secret_env` | | The application's id and secret; `_env` names an environment variable holding the secret |
  | `groups_claim` | `groups` | The claim with the user's groups: a list or one value. A path works too (`realm_access.roles` for Keycloak realm roles). If the ID token does not have it, the provider's userinfo endpoint is asked |
  | `username_claim` | `preferred_username` | The claim shown as the user's name (then `email`, then `sub`). The audit log also records `sub`, which never changes |
  | `scopes` | `openid profile email` | Add what your provider needs for the groups claim (often `groups`) |
  | `session_hours` | `12` | A login ends after this long; the next one goes through the provider again (usually without a prompt) |
  | `label` | `single sign-on` | The name on the login button |
  | `ca_file` | system CAs | CA certificate (PEM) for a provider with a private CA |

  Microsoft Entra ID sends group object ids in `groups` (add the groups
  claim under Token configuration), so `roles` lists ids, or define app
  roles and set `groups_claim: roles`. With more than 200 groups it
  leaves the claim out; have it send only the groups assigned to the
  application. Google has no groups claim: `groups_claim: email` with
  the addresses under `roles` works for a few people.

  The provider is not asked again during a session, so someone removed
  from a group keeps their role until the session ends (`session_hours`).
  **Log out** ends the GUI's session, not the provider's.
- **LDAP / Active Directory:** people type their directory name and
  password into the login form. The GUI finds the account with the bind
  account, binds as it with the password, and reads its groups. A name
  that belongs to a local user is checked locally only: a local password
  is never sent to the directory. LDAP needs one more package: `pip
  install 'clabfleet[ldap]'` (ldap3).

  | Setting | Default | |
  |---|---|---|
  | `url` | | `ldaps://host[:port]`, or `ldap://host[:port]`, which is upgraded with StartTLS. The server's certificate is verified |
  | `ca_file` | system CAs | CA certificate (PEM) of the domain's CA |
  | `start_tls` | `true` for `ldap://` | `false` sends passwords in clear text (a warning at start-up); for tests only |
  | `bind_dn`, `bind_password` or `bind_password_env` | anonymous | The account that looks users up. It needs to read users and their groups, nothing more |
  | `user_base` | | Where users are searched (the whole subtree) |
  | `user_filter` | Active Directory accounts by `sAMAccountName`, not disabled | The search for a user; `{username}` is the name typed, escaped. For another directory, e.g. `(&(objectClass=inetOrgPerson)(uid={username}))` |
  | `username_attribute` | `sAMAccountName` | The attribute shown as the user's name (`uid` elsewhere) |
  | `group_attribute` | `memberOf` | The attribute of the user that lists their groups' DNs |
  | `nested_groups` | `false` | Active Directory: also count groups reached through other groups (searched under `group_base`, default `user_base`) |
  | `recheck` | `300` | Seconds between checks that a logged-in user is still there with the same role |
  | `timeout` | `10` | Seconds to wait for the server |
  | `label` | `directory` | What the login form calls it ("your ... name and password") |

  `roles` takes the groups' full DNs (compared without case), so a group
  of the same name somewhere else in the tree does not count. A user's
  primary group (Domain Users) is not in `memberOf`.

  Every `recheck` seconds the GUI looks each logged-in user up again: a
  changed group changes their role, and someone disabled, deleted or out
  of every mapped group is logged out and their terminals close. If the
  directory does not answer, logins hold for up to an hour past their
  check and then end. Wrong passwords are limited like local ones (5 per
  minute per address and per name), which also keeps the GUI from locking
  directory accounts.
- **Changed settings** end the directory logins made with the old ones
  (a new secret, label or CA file does not). `--single-token` and
  `--directory` exclude each other.

## Login and sessions

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
let people log in through your directory instead (see
[Logging in through your directory](#logging-in-through-your-directory)):
OIDC brings the provider's multi-factor login with it.
Terminals and captures that are already open are closed
within a few seconds when their user is removed, rotated, demoted from
operator or logs out.

For the CLI and Shell buttons, your user must be able to run `docker`
(member of the `docker` group, in a session started after you were added).
