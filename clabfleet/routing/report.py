"""Plain-text report of a routing view, for ``clabfleet routing``."""

PROTOCOLS = ("ospf", "bgp", "evpn")


def _end(e: dict) -> str:
    iface = f" {e['iface']}" if e.get("iface") else ""
    ip = f" ({e['ip']})" if e.get("ip") else ""
    return f"{e['node']}{iface}{ip}"


def format_report(view: dict, protocols=PROTOCOLS) -> str:
    out: list[str] = []
    w = out.append
    shown = [p for p in protocols if view.get(p)]
    if not shown:
        w("No OSPF, BGP or EVPN configuration found.")

    if "ospf" in shown:
        ospf = view["ospf"]
        w(f"OSPF  (areas: {', '.join(ospf['areas'])})")
        for name, n in sorted(ospf["nodes"].items()):
            rid = n["router_id"] + ("" if n["router_id_configured"] else " (derived)")
            abr = "  ABR" if n["abr"] else ""
            w(f"  {name:<16} router-id {rid:<22} area {', '.join(n['areas'])}{abr}")
        w("  adjacencies:")
        for a in ospf["adjacencies"]:
            area = f"area {a['area']}" if a["area"] is not None else "AREA MISMATCH"
            w(f"    {_end(a['a'])} <-> {_end(a['b'])}  {area}")
        w("")

    if "bgp" in shown:
        bgp = view["bgp"]
        w(f"BGP  (AS: {', '.join(bgp['asns'])})")
        for name, n in sorted(bgp["nodes"].items()):
            rid = n["router_id"] + ("" if n["router_id_configured"] else " (derived)")
            w(f"  {name:<16} AS {n['asn']:<10} router-id {rid}")
        w("  sessions:")
        for s in bgp["sessions"]:
            flags = [s["type"], "/".join(s["families"]) or "no AFI"]
            if s["multihop"]:
                flags.append("multihop")
            if s["configured"] != "both":
                flags.append(s["configured"])
            if s["external"]:
                flags.append(f"external AS {s['b']['asn']}")
            w(f"    {_end(s['a'])} <-> {_end(s['b'])}  {', '.join(flags)}")
        w("")

    if "evpn" in shown:
        evpn = view["evpn"]
        w("EVPN")
        for name, v in sorted(evpn["vteps"].items()):
            w(f"  VTEP {name:<16} {v['ip'] or '?':<16} source {v['source_interface'] or '?'}")
        w("  VNIs:")
        for v in evpn["vnis"]:
            what = (f"VLAN {', '.join(map(str, v['vlans']))}" if v["type"] == "l2"
                    else f"VRF {', '.join(v['vrfs'])}")
            w(f"    {v['vni']:<8} {v['type'].upper()} {what:<16} on {', '.join(v['members'])}")
        w(f"  EVPN sessions: {len(evpn['sessions'])}, VXLAN tunnels: {len(evpn['tunnels'])}")
        w("")

    problems = [p for p in view["problems"] if p["protocol"] in shown or p["protocol"] == "ip"]
    if problems:
        w("Problems:")
        for p in problems:
            w(f"  {p['severity']}: [{p['protocol']}] {p['message']}")
    # Nodes without any config (hosts, mostly) are not worth listing
    unparsed = [u for u in view["unparsed"] if u["reason"] != "no startup-config"]
    if unparsed:
        w("Configs not read:")
        for u in unparsed:
            w(f"  {u['node']}: {u['reason']}")
    return "\n".join(out).rstrip() + "\n"
