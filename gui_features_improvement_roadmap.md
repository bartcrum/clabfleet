# GUI improvement roadmap

Design and UX ideas for the clabfleet web GUI, followed by feature ideas.
Pick items by number. Sizes: **S** is an afternoon, **M** a few days, **L**
a week or more.

Each design item starts with what the GUI does today (from the current
`clabfleet/gui/static/`), then the proposal.

---

## Part 1 — Design

### Where the GUI stands

The GUI is functional and consistent, but it reads as a developer tool more
than a designed product:

- **Text everywhere.** No icons on buttons, tabs, nodes or the sidebar; a
  router, a switch and a Linux host look identical on the canvas except for
  the small kind label.
- **Green does three jobs.** The accent green is the brand colour, the
  primary button colour (Deploy), and the "running / up" status colour. A
  green Deploy button next to a lab that is green because it runs reads as
  "go" when it is actually disabled.
- **Floating cards cover the canvas.** Node, link and routing cards, the
  problems list and the legend all float over the diagram in fixed corners
  and hide the nodes underneath.
- **Problems live in several places.** Validation errors in the YAML tab,
  routing problems behind a button in the Routing tab, down links as red
  lines in the Diagram, host errors in the console. There is no single
  "what is wrong with this lab" view.
- **Every action has equal weight.** Deploy, Redeploy, Save configs,
  Snapshot and Destroy sit in one row of same-sized buttons, and the
  destructive ones are confirmed with the browser's native `confirm()`.
- **Small text.** Twelve rules in `app.css` use 9–11 px text (interface
  labels, kinds, badges, legends), which gets hard to read when zoomed out
  and fails contrast on muted colours.
- **Theme follows the OS only**, with no manual switch, no reduced-motion
  handling and no screen-reader announcements for state changes.

### A. Visual language

**A1. Device icons on nodes — S**
Today: every node is the same rounded box with a name and a kind string.
Proposal: a small glyph per role (router, L3 switch, L2 switch, host,
firewall, route server) on the left of the box, chosen from the kind and
the tier the layout already detects (`TIERS` in `app.js`). Inline SVG
sprites bundled in `static/`, no external fonts. The kind moves to a
tooltip or second line; the vendor (Arista, Cisco, Nokia, Juniper) becomes
a small badge.

**A2. Separate brand, action and status colours — S**
**Done** (branch `design-quick-wins`).
Today: `--accent` green is used for all three.
Proposal: three token groups in `app.css`:
- *brand / interactive*: a neutral blue or teal for primary buttons,
  focus rings, selection
- *status*: `--ok`, `--warn`, `--bad`, `--unknown`, `--drift`, used only
  for state
- *categorical*: the existing `--c0..--c7` for areas, ASes and VNIs

Green then always means "healthy", and a primary button never looks like
a status.

**A3. Status never by colour alone — S**
**Done** (branch `design-quick-wins`).
Today: node state is a 4.5 px dot; link state is mostly colour (some
dashes). Red/green is the most common colour-blindness pair.
Proposal: pair every status colour with a shape or pattern: ✓ ring for
up, ✕ for down, ◐ for booting, ? for unknown; dashed and dotted edge
styles already exist for some states, so extend them to all. Make the
status dot larger (8 px) with an outline.

**A4. Icons on buttons and tabs — S**
Small icons next to the labels of Deploy (play), Destroy (trash), Save
configs (download), Snapshot (camera), Diagram / Nodes / Routing / YAML
tabs, Fit, Reload. Labels stay; icons speed up scanning. One SVG sprite.

**A5. Type scale and minimum sizes — S**
**Done** (branch `design-quick-wins`).
Define a scale (11 / 12 / 13 / 15 / 18 px) as tokens and raise the 9–10 px
labels to 11 px minimum. Interface labels on the canvas get a
level-of-detail rule (see C3) instead of shrinking.

### B. Layout and information architecture

**B1. Lab header as a status strip — S**
**Done** (branch `gui-structure`).
Today: name, a state badge, the file path and five equal buttons.
Proposal:
- A row of compact stats under the name: *6/6 running · 2 hosts ·
  8/8 sessions up · 0 problems*, each clickable (opens Nodes, Routing
  Live or the Health panel).
- One context-dependent primary button: **Deploy** when stopped,
  **Open terminal…** or nothing when running.
- Redeploy, Save configs and Snapshot in a secondary group; **Destroy**
  in an overflow menu (⋯), separated from the rest.
- The file path moves into a tooltip or a copy button.

**B2. Docked inspector instead of floating cards — M**
**Done** (branch `gui-structure`).
Today: node, link and routing cards float at top-right over the canvas.
Proposal: a resizable right-hand inspector panel, shared by Diagram and
Routing, that pushes the canvas instead of covering it. The selection
drives it, with sections:
- *Overview*: state, kind, image, host, mgmt IP, CPU/memory
- *Interfaces*: name, IP, link state, peer
- *Protocols*: OSPF / BGP / EVPN details (today's routing cards)
- *Actions*: open CLI / shell / logs, config diff, capture
Collapses to a thin rail when nothing is selected; becomes a bottom sheet
on narrow screens.

**B3. One Health panel — M**
**Done** (branch `gui-structure`).
Today: problems are spread across tabs and popovers.
Proposal: a *Health* tab in the bottom dock (next to Activity) that lists
everything wrong with the open lab, one row each, filterable by severity
and source: validation, routing config problems, live sessions down,
links down, drift, host errors, nodes booting too long. Clicking a row
selects the node or edge and switches to the right tab. The header stat
"N problems" (B1) opens it.

**B4. Sidebar with search, grouping and state — S**
Today: a flat list of topologies with path and "6/6".
Proposal: a search box; group by folder; running labs first with a
progress ring instead of "6/6"; a collapsed state showing only icons and
state dots, to give the canvas more room.

**B5. Activity as steps, not just a log — M**
Today: the Activity panel shows raw containerlab output.
Proposal: above the log, a stepper per host (plan → images → deploy →
links → ready) with durations, built from the log lines the job already
emits. The raw log stays one click away. Finished jobs raise a toast with
"View" and "Open lab" links.

### C. Canvas

**C1. Zoom controls and a minimap — S**
Today: wheel to zoom, Fit button.
Proposal: +, −, 100 % and Fit buttons grouped bottom-right, plus an
optional minimap for labs with more than ~15 nodes. Keyboard: `+`/`-`,
`0` to fit, arrows to pan.

**C2. Focus on hover and selection — S**
**Done** (branch `design-quick-wins`).
Hovering or selecting a node highlights its links and neighbours and dims
everything else to ~30 %. Selecting an edge highlights both ends. Makes
dense spine-leaf and EVPN views readable without filtering.

**C3. Level of detail — S**
Zoomed out: hide interface labels and secondary text, keep names and
status. Zoomed in: show interface labels, IPs, costs. Avoids the label
pile-up seen today on the BGP view of the spine-leaf fabric.

**C4. Hosts as swimlanes in multi-host labs — M**
Today: a `@host` badge per node and blue dashed cross-host links.
Proposal: an optional layout that groups nodes into one tinted lane per
host, with cross-host VXLAN links crossing lane borders. The same
group-tint style as the AS and area regions in the Routing tab, so the
visual language is shared.

**C5. Gentle motion for state changes — S**
A short pulse when a node or session changes state, edges fading between
colours, booting nodes breathing. All disabled under
`prefers-reduced-motion`.

### D. States and feedback

**D1. First-run and empty states — S**
Today: "Select a topology on the left."
Proposal: a welcome screen with three steps (pick or generate a lab,
deploy, open a terminal), the example topologies as cards with a mini
diagram, and the `clabfleet new` templates as "Start from a template".

**D2. Styled confirmation dialogs — S**
**Done** (branch `design-quick-wins`).
Today: native `confirm()` for Destroy, Redeploy, discarding edits and
switching users.
Proposal: an in-app modal that states the impact, e.g. *Destroy
spine-leaf-fabric: removes 6 containers on 1 host; configs not saved since
14:02 are lost*, with the destructive button in red and focus on Cancel.
Typing the lab name to confirm for labs with more than N nodes.

**D3. Loading, freshness and errors — S**
Skeleton placeholders while topologies and live state load; one
consistent "updated 4 s ago" freshness label for every live view; a clear
"stale" style when live data stops updating (host unreachable) instead of
silently keeping the last colours.

### E. Accessibility and responsiveness

**E1. Keyboard-navigable canvas — M**
Tab into the canvas, arrow keys move between connected nodes, Enter opens
the inspector, `t` opens the node's terminal. Nodes and edges get
`role` and `aria-label` text ("Leaf-1, arista_ceos, running, 2 BGP
sessions up").

**E2. Screen-reader announcements — S**
An `aria-live` region announcing job results and state changes ("BGP
session Spine-1 to Leaf-2 down").

**E3. Contrast pass — S**
Check every text and status token against WCAG AA in both themes; muted
10 px text on the dark panel is the likely failure today.

**E4. Narrow screens — M**
Collapsible sidebar, inspector as a bottom sheet, the action row as a
menu, the dock as a full-screen sheet. Useful on a tablet in a lab.

### F. Theming

**F1. Theme switch — S**
**Done** (branch `design-quick-wins`).
Light / dark / follow system, remembered per browser. The token set is
already split into `:root` and a light override, so this is mostly a
`data-theme` attribute and a menu entry.

**F2. High-contrast theme — S**
Thicker strokes, no tints, maximum-contrast status colours. Useful on
projectors for training sessions.

### Suggested design order

1. **Quick wins (S):** A2 colour roles, A3 status shapes, A5 type scale,
   D2 dialogs, F1 theme switch, C2 focus highlighting
2. **Structure (M):** B1 header strip, B2 docked inspector, B3 Health
   panel
3. **Polish:** A1 device icons, A4 button icons, C1/C3 canvas controls
   and level of detail, D1 first-run, E1–E3 accessibility
4. **Later:** C4 host swimlanes, B5 job stepper, E4 narrow screens,
   F2 high contrast

---

## Part 2 — Features

**Routing and protocols**

1. **Path trace — M.** Pick two nodes or a prefix and highlight the hops
   traffic takes (from `show ip route` / `traceroute`), ECMP paths as
   parallel highlights.
2. **Protocol event timeline — S–M.** The live poller already reads every
   10 s; record changes ("Spine-1↔Leaf-2 down 14:02:11, up 14:02:40") in
   a strip under the diagram so flaps are visible after the fact.
3. **EVPN MAC/IP per VNI — M.** Which hosts (type-2 / type-5 routes) are
   learned on each VNI and from which VTEP, in the VTEP's card. The part
   of roadmap 4.2 left for later.
4. **Drift → diff in one click — S.** A drift warning links to the config
   diff tab for that node, with Save configs next to it.

**Live state and traffic**

5. **Link utilisation — M.** Links drawn thicker or hotter by traffic
   rate (rx/tx bytes from `/sys/class/net`, which the live probe already
   reads). Pairs with packet capture.
6. **Failure "what-if" — M.** Shut / no-shut an interface or stop a node
   from the diagram (operator only, confirmed) and watch the Live routing
   view reconverge.

**Everyday use**

7. **Command palette (Ctrl+K) — S.** Jump to a node, open its CLI or
   logs, switch tabs, run Deploy and other actions from the keyboard.
8. **Run a command on many nodes — M.** `clabfleet exec` in the GUI:
   select nodes or a glob, run a command, compare outputs side by side or
   diffed.
9. **Node detail page — S.** Interfaces with config IPs, live state and
   the protocols on each, in one table. Fits the inspector (B2).
10. **New lab wizard — M.** The `clabfleet new` templates as a form with
    a live diagram preview before writing the file.

**Docs and sharing**

11. **Export a view — S.** Download the Diagram or Routing view as
    SVG / PNG, light or dark.
12. **Diagram annotations — S–M.** Sticky notes and labelled boxes
    ("DC1", "tenant A") saved as labels in the topology file, like
    positions.
