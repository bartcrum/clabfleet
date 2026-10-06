# First-time setup and troubleshooting

Commands here are run from the folder you cloned clabfleet into, and
`topologies/<lab>.clab.yml` stands for your topology file.

## First-time setup: root for containerlab

containerlab needs root to create a lab's network links, so Deploy,
Redeploy, Stop, Save and Destroy need it too. Looking at labs does not.
clabfleet never uses sudo unless you ask it to (`--sudo` or `CLAB_SUDO=1`),
and the GUI cannot type a sudo password for you. Choose one of these once
per lab host:

| Way | Start clabfleet with | Good for |
|---|---|---|
| A. containerlab's own group | no `--sudo` | A machine where containerlab was installed with its installer or packages |
| B. sudo without a password, for containerlab only | `--sudo` | Any machine; a GUI that runs for days or as a service |
| C. Unlock sudo before you start | `--sudo` | Trying things out |

**A. containerlab's group.** containerlab's installer makes its binary
setuid and lets members of the `clab_admins` group use it without sudo:

```bash
ls -l "$(command -v containerlab)"       # setuid looks like -rwsr-xr-x ... root
sudo usermod -aG clab_admins "$USER"     # then log out and back in
```

Some distribution packages install it without setuid (`-rwxr-xr-x`); then
use B.

**B. A sudo rule for containerlab.**

```bash
echo "$USER ALL=(root) NOPASSWD: $(command -v containerlab)" | sudo tee /etc/sudoers.d/clabfleet
sudo chmod 440 /etc/sudoers.d/clabfleet
sudo visudo -cf /etc/sudoers.d/clabfleet  # must say "parsed OK"
sudo -n containerlab version              # must not ask for a password
```

Then start with `clabfleet --sudo gui`, or put `export CLAB_SUDO=1` in your
shell profile. Two other things fall back to sudo and are not covered by
this rule: reading a saved config that only root can read (snapshots and
diffs), and `docker` if your user is not in the `docker` group. Add your
user to the `docker` group for the second.

**C. Unlock sudo first.**

```bash
sudo -v && clabfleet --sudo gui
```

sudo stays unlocked for a few minutes after its last use (15 by default).
After that a deploy fails with "sudo asks for a password": stop the GUI
with Ctrl+C and run the line again. Logins survive the restart.

With A and B, anyone who may run containerlab is effectively root on that
host: a topology can mount any path or run privileged containers. The
same holds for every operator of the GUI.

`clabfleet gui` checks this when it starts and prints a warning if deploys
would fail, so you find out before the first click:

```
WARNING: containerlab needs root here, so deploys will fail. Start clabfleet with --sudo (or CLAB_SUDO=1)
WARNING: sudo asks for a password here, so deploys will fail. Unlock sudo first (...)
```

## Troubleshooting

### Deploy fails: "containerlab needs root on ..."

clabfleet was started without `--sudo` and containerlab is not set up for
your user. Use one of the three ways above. On a cluster host, set `sudo:
true` for it in the cluster file, or add the SSH user to `clab_admins`
there.

### Deploy fails: "sudo asks for a password on ..."

clabfleet was started with `--sudo`, but sudo wants a password and nobody
can type it into a GUI job. Either sudo was never unlocked in that
terminal, or it has locked again since (way C times out). Set up A or B
to be rid of it, or stop the GUI, run `sudo -v` and start it again.

### A lab host stopped answering

The GUI asks every host for its labs every few seconds. A host that is
switched off, unreachable, or so busy that it does not answer within 20
seconds shows its error in the top bar and on the Hosts page ("no answer
within 20 seconds", or SSH's own words), and its labs show as not known.
The rest of the GUI carries on: the other hosts are asked at the same
time, and a host that is down is not asked again on every refresh but
tried in the background every 30 seconds until it answers, when it comes
back by itself.

The machine the GUI itself runs on is never treated as down. If it is too
busy to answer one of those checks in time (during a heavy deploy, say),
that one refresh shows "no answer within 20 seconds" and the next one
asks again; terminals and everything else keep working meanwhile.

An SSH connection to a host that goes away without a word (power,
network) is noticed after about half a minute and made anew on the next
use.

A deploy that needs a host which does not answer fails while planning
("Host ... unreachable: no answer in time"), after at most two minutes,
and does not hold up deploys of other labs. A job that is stuck further
on, waiting for a command on such a host, can be ended with **Cancel
job** in the Activity panel: the GUI stops waiting and the lab is free
for another job. Cancel undoes nothing. What containerlab had started is
left as it is, and a command already running on a host may still finish
there, so look at the lab afterwards and Destroy or Redeploy it.

### After a reboot the lab is half up

After the lab host restarts, the GUI shows some nodes running and others
exited, and nothing can reach anything. Docker brought back the
containers that restart by themselves, but a lab's links do not survive a
reboot, and the other nodes stay down. Starting the containers by hand
does not bring the links back. Recreate the lab.

Keeping the configs last saved on the nodes (Save configs, or `write` on
the node):

```bash
clabfleet --sudo destroy topologies/<lab>.clab.yml --keep-lab-dir
clabfleet --sudo deploy topologies/<lab>.clab.yml --wait
```

From the startup configs in the topology file: **Redeploy** in the GUI, or

```bash
clabfleet --sudo deploy topologies/<lab>.clab.yml --reconfigure --wait
```

**Stop** in the GUI does not help here: it saves the running configs
first, which exited nodes cannot do, and then removes nothing.

Before a planned reboot, **Stop** the lab (`clabfleet --sudo stop ...`):
it saves the configs and removes the containers, and **Deploy from saved
configs** brings it back afterwards.

### Redeploy lost my changes on the nodes

**Redeploy** recreates every node from the topology's startup configs and
discards what was configured on the nodes since. To restart a lab and
keep its configs, use **Stop** and then **Deploy from saved configs**. To
keep a copy before a Redeploy, take a **Snapshot**
([Snapshot and diff configs](cli.md#snapshot-and-diff-configs)).

### The CLI and Shell buttons fail with a Docker permission error

Your user must be able to run `docker`: add it to the `docker` group
(`sudo usermod -aG docker "$USER"`) and log out and back in, then restart
the GUI.

### I lost the admin password

Until it is changed, the first password is printed at every start and
kept in `~/.clabfleet/initial-admin-password`. After that, set a new one
on the machine the GUI runs on:

```bash
clabfleet user passwd admin
```

### The login page has no "Log in with ..." button, or my directory account is refused

Directory logins only exist once `~/.clabfleet/directory.yaml` is there
(or `--directory FILE` is given) and the GUI has been restarted. Check
the file and the role a user would get:

```bash
clabfleet directory check
clabfleet directory check --user alice    # LDAP
```

"... is in no group that may use this GUI" means the login was right and
none of the user's groups is listed under `roles:`. See
[Logging in through your directory](gui.md#logging-in-through-your-directory).
