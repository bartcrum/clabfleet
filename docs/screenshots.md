# A tour of the GUI

Screens of the web GUI, taken from a running lab: the EVPN MLAG example
(`topologies/evpn_mlag.clab.yml`, six Arista cEOS switches and four Linux
hosts) and the campus example. [Web GUI](gui.md) describes each part.

## Start page

The topologies in the workspace, each with a thumbnail and its state, and
ways to start a new lab: a blank canvas or a template.

![The start page: a card per topology with a thumbnail of its nodes and links, and cards to start a new lab from a blank canvas or a template](images/start-page.png)

## Diagram and inspector

The lab as a diagram with live node and link state. Selecting a node opens
the inspector: its state, CPU and memory, and every interface with its
address, its peer, its live state and what runs on it.

![The diagram of the EVPN MLAG lab with Leaf-1 selected; the inspector lists its interfaces with addresses, peers, link state and the BGP, MLAG and VXLAN roles of each](images/diagram-inspector.png)

The light theme (there is also a high-contrast one):

![The same diagram in the light theme, with Spine-1 selected](images/diagram-light.png)

## Rack view

Each host as a rack, each node a device with its ports and link LEDs, and
a cable per link coloured by its role.

![The rack view: one rack for the host with the two spines, four leaves and four hosts, and cables between their ports](images/racks.png)

## Routing

The routing design read from the nodes' startup configs, one view per
protocol, with the live state of the running lab over it.

EVPN: the route servers, the VTEPs, and the VXLAN tunnels between VTEPs
that share a VNI. The card shows a VTEP's VNIs with their RDs and RTs.

![The Routing tab's EVPN view in Live mode: sessions to the spines in green, VXLAN tunnels between the leaf pairs, and Leaf-1's VNIs in the inspector](images/routing-evpn.png)

BGP: routers grouped by AS, sessions with their address families and
state.

![The Routing tab's BGP view in Live mode with Spine-1 selected](images/routing-bgp.png)

OSPF, on a lab that is not deployed: the design alone, with what does not
add up between the configs.

![The Routing tab's OSPF view of the campus lab with Dist-1 selected; the inspector lists its OSPF interfaces and two notes about hellos sent to routers that run no OSPF](images/routing-ospf.png)

## Path trace

Where traffic from one node to another goes, hop by hop, read from the
nodes' own tables: every equal-cost branch, and VXLAN between VTEPs. The
path is drawn on the diagram.

![A trace from Host-1 to Host-4: the hops with their routes in the Trace panel, the links taken, and the path highlighted on the diagram with VXLAN hops as dashed arcs](images/trace.png)

## Terminals

A node's CLI, shell or logs in a tab, next to the diagram.

![A CLI tab on Spine-1 showing its BGP EVPN summary, under the diagram with a traced path](images/terminal.png)

## Hosts

Each lab host with its memory in use, the vCPU counted for its running
labs, and the labs on it.

![The Hosts page: one host with a memory bar, a vCPU bar and the EVPN MLAG lab running on it](images/hosts.png)

## Drawing a lab

Edit mode on the diagram: drag kinds onto the canvas, link nodes by
dragging between them, generate their configs.

![The campus lab in edit mode, with the palette of kinds to drag onto the canvas and the bar to generate configs, preview the YAML, discard or save](images/builder.png)

## Logging in

Named users with operator and viewer roles, and logins through OpenID
Connect or LDAP / Active Directory. (The provider and directory named on
this screen are stand-ins.)

![The login page with a single sign-on button and the name and password form](images/login.png)
