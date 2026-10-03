from pathlib import Path

import pytest

from clabfleet.routing import routing_view
from clabfleet.routing.parse import parse_config
from clabfleet.routing.report import format_report
from clabfleet.topology import load_topology

LAB = Path(__file__).resolve().parent.parent / "topologies" / "evpn_mlag.clab.yml"


def view_of(tmp_path, *edits):
    """The MLAG lab's view, after replacing text in it: (old, new[, count])."""
    text = LAB.read_text()
    for old, new, *count in edits:
        assert old in text, old
        text = text.replace(old, new, *count)
    path = tmp_path / "lab.clab.yml"
    path.write_text(text)
    return routing_view(load_topology(path))


def messages(view, protocol=None):
    return [p["message"] for p in view["problems"] if protocol in (None, p["protocol"])]


def test_parse_mlag():
    cfg = parse_config("""interface Ethernet3
   channel-group 10 mode active
interface Ethernet4
   channel-group 10 mode active
interface Port-Channel5
   switchport access vlan 10
   mlag 5
mlag configuration
   domain-id POD-A
   local-interface Vlan4094
   peer-address 10.254.0.1
   peer-address heartbeat 172.20.20.3
   peer-link Port-Channel10
""")
    m = cfg.mlag
    assert (m.domain_id, m.local_interface, m.peer_address, m.peer_link) == \
        ("POD-A", "Vlan4094", "10.254.0.1", "Port-channel10")
    assert cfg.port_channel_members("Port-channel10") == ["Ethernet3", "Ethernet4"]
    po = cfg.interface("Port-Channel5")
    assert (po.mlag_id, po.access_vlan) == (5, 10)


def test_mlag_lab():
    view = routing_view(load_topology(LAB))
    assert view["problems"] == []  # the shared VTEP addresses are not duplicates
    pairs = view["mlag"]["pairs"]
    assert [(p["domain"], p["nodes"], p["vtep"]) for p in pairs] == [
        ("POD-A", ["Leaf-1", "Leaf-2"], "10.255.2.12"), ("POD-B", ["Leaf-3", "Leaf-4"], "10.255.2.34")]
    assert len(pairs[0]["peer_links"]) == 2
    assert [(p["mlag"], p["vlan"], p["hosts"]) for p in pairs[0]["ports"]] == [(5, 10, ["Host-1"]), (6, 20, ["Host-2"])]
    assert view["mlag"]["unpaired"] == []
    # One VTEP per pair: tunnels only between the pairs
    evpn = view["evpn"]
    assert sorted((t["a"]["node"], t["b"]["node"]) for t in evpn["tunnels"]) == [
        ("Leaf-1", "Leaf-3"), ("Leaf-1", "Leaf-4"), ("Leaf-2", "Leaf-3"), ("Leaf-2", "Leaf-4")]
    assert evpn["vteps"]["Leaf-1"]["mlag_peer"] == "Leaf-2"
    # The iBGP session over the peer-link VLAN is found too
    assert any(s["type"] == "ibgp" and {s["a"]["node"], s["b"]["node"]} == {"Leaf-1", "Leaf-2"}
               for s in view["bgp"]["sessions"])
    report = format_report(view)
    assert "POD-A: Leaf-1 (10.254.0.0) <-> Leaf-2 (10.254.0.1)  peer-link Port-channel10 (2 links), VTEP 10.255.2.12" in report
    assert "mlag 5    Port-channel5 / Port-channel5  VLAN 10 -> Host-1" in report


@pytest.mark.parametrize("edits, expected", [
    # Leaf-2 in another domain
    ([("domain-id POD-A\n           local-interface Vlan4094\n           peer-address 10.254.0.0",
       "domain-id POD-X\n           local-interface Vlan4094\n           peer-address 10.254.0.0")],
     "MLAG peers Leaf-1 and Leaf-2 have different domain-ids (POD-A, POD-X)"),
    # Leaf-1 points at an address nobody has
    ([("peer-address 10.254.0.1", "peer-address 10.254.0.9")],
     "Leaf-1 MLAG peer-address 10.254.0.9 is not configured on any node"),
    # A dual-homed port on one side only
    ([("   switchport access vlan 10\n           mlag 5", "   switchport access vlan 10", 1)],
     "mlag 5 is configured on Leaf-2 only, not on Leaf-1"),
    # Different VLANs on the two halves of a port
    ([("   switchport access vlan 10\n           mlag 5", "   switchport access vlan 30\n           mlag 5", 1)],
     "mlag 5: VLAN 30 on Leaf-1, 10 on Leaf-2"),
    # The pair's VTEPs on different addresses
    ([("description VTEP source, shared with Leaf-1 (MLAG pair A)\n           ip address 10.255.2.12/32",
       "description VTEP source, shared with Leaf-1 (MLAG pair A)\n           ip address 10.255.2.99/32")],
     "MLAG peers Leaf-1 and Leaf-2 are VTEPs with different source addresses (10.255.2.12, 10.255.2.99); they should share one"),
    # The peer-link's members cabled elsewhere
    ([('- endpoints: ["Leaf-1:eth3", "Leaf-2:eth3"]', '- endpoints: ["Leaf-1:eth3", "Leaf-3:eth7"]'),
      ('- endpoints: ["Leaf-1:eth4", "Leaf-2:eth4"]', '- endpoints: ["Leaf-1:eth4", "Leaf-3:eth8"]')],
     "Leaf-1 peer-link Port-channel10 has no link to Leaf-2"),
])
def test_mlag_problems(tmp_path, edits, expected):
    assert expected in messages(view_of(tmp_path, *edits), "mlag")


def test_shared_address_outside_a_pair_is_still_a_duplicate(tmp_path):
    # Leaf-3 takes pair A's VTEP address: not its MLAG peer, so a duplicate
    view = view_of(tmp_path, ("ip address 10.255.2.34/32", "ip address 10.255.2.12/32", 1))
    assert any(m.startswith("10.255.2.12 is configured on") for m in messages(view, "ip"))


def show_mlag(state="active", neg="connected", link="up", sanity="consistent", full=2, partial=0):
    import json
    return json.dumps({
        "domainId": "POD-A", "localInterface": "Vlan4094", "localIntfStatus": "up",
        "peerLink": "Port-Channel10", "peerLinkStatus": link, "peerAddress": "10.254.0.1",
        "configSanity": sanity, "state": state, "negStatus": neg, "reloadDelay": 300,
        "mlagPorts": {"Disabled": 0, "Configured": 0, "Inactive": 0, "Active-partial": partial, "Active-full": full},
        "portsErrdisabled": False})


def test_mlag_live():
    from clabfleet.routing.live import eos_script, overlay, parse_outputs, wanted_topics

    view = routing_view(load_topology(LAB))
    assert "mlag" in wanted_topics(view, "Leaf-1") and "mlag" not in wanted_topics(view, "Spine-1")
    assert "show mlag | json" in eos_script(["mlag"])
    running = {n: True for n in ("Leaf-1", "Leaf-2", "Leaf-3", "Leaf-4")}

    def pair_state(a_text, b_text, c_text=show_mlag(), d_text=show_mlag()):
        states = {n: parse_outputs("eos", {"mlag": t}, 0) for n, t in
                  zip(running, (a_text, b_text, c_text, d_text))}
        return overlay(view, states, running)

    live = pair_state(show_mlag(), show_mlag())
    assert live["mlag"]["mlag0"]["state"] == "up" and live["summary"]["mlag"] == {"up": 2}
    st = pair_state(show_mlag(partial=1, full=1), show_mlag(partial=1, full=1))["mlag"]["mlag0"]
    assert st["state"] == "partial" and st["detail"] == "1 dual-homed port up on one side only"
    st = pair_state(show_mlag(state="inactive", neg="connecting", link="down"), show_mlag())["mlag"]["mlag0"]
    assert st["state"] == "down" and st["detail"] == "state inactive, peer connecting, peer-link down"
    st = pair_state(show_mlag(sanity="inconsistent"), show_mlag())["mlag"]["mlag0"]
    assert st["detail"] == "config inconsistent"
    # A half that is not running: the pair's state is not known from it
    live = overlay(view, {"Leaf-1": parse_outputs("eos", {"mlag": show_mlag()}, 0)}, {"Leaf-1": True})
    assert live["mlag"]["mlag0"]["b"] == {"state": "unknown", "detail": "not running"}


def test_mlag_events():
    from clabfleet.gui.events import protocol_items

    view = routing_view(load_topology(LAB))
    items = protocol_items({"mlag": {"mlag1": {"state": "down", "detail": "peer-link down"}}}, view)
    assert items["mlag:mlag1"]["label"] == "MLAG Leaf-3 ↔ Leaf-4" and items["mlag:mlag1"]["kind"] == "mlag"
