"""Plain-text report of a routing view, for ``clabfleet routing``."""

from typing import Optional

PROTOCOLS = ("ospf", "bgp", "evpn")


def _end(e: dict) -> str:
    iface = f" {e['iface']}" if e.get("iface") else ""
    ip = f" ({e['ip']})" if e.get("ip") else ""
    return f"{e['node']}{iface}{ip}"


def _live(entry: Optional[dict]) -> str:
    """``  [up]`` / ``  [DOWN: Active]`` after a line, when live state is shown."""
    if entry is None:
        return ""
    state = entry["state"]
    if state == "up":
        return "  [up]"
    detail = f": {entry['detail']}" if entry.get("detail") else ""
    return f"  [{state.upper() if state == 'down' else state}{detail}]"


def _uptime(seconds: Optional[int]) -> str:
    if seconds is None:
        return ""
    d, rest = divmod(int(seconds), 86400)
    h, rest = divmod(rest, 3600)
    m, s = divmod(rest, 60)
    return f"{d}d{h:02}h" if d else f"{h:02}:{m:02}:{s:02}"


def format_report(view: dict, protocols=PROTOCOLS, live: Optional[dict] = None) -> str:
    out: list[str] = []
    lv = live or {}
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
            w(f"    {_end(a['a'])} <-> {_end(a['b'])}  {area}"
              f"{_live(lv.get('ospf', {}).get(a['id'])) if live else ''}")
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
            entry = lv.get("bgp", {}).get(s["id"]) if live else None
            drift = entry.get("drift") if entry else None
            if entry and entry["state"] == "up":
                a_side = next(iter(entry["families"].values()))["a"]
                flags.append(f"up {_uptime(a_side.get('uptime'))}".strip())
                entry = None
            w(f"    {_end(s['a'])} <-> {_end(s['b'])}  {', '.join(flags)}{_live(entry)}")
            if drift:
                w(f"      drift: {drift}")
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
        if live:
            w("  VXLAN tunnels:")
            for t in evpn["tunnels"]:
                w(f"    {t['a']['node']} ({t['a']['ip']}) <-> {t['b']['node']} ({t['b']['ip']})  "
                  f"VNI {', '.join(map(str, t['vnis']))}{_live(lv.get('vxlan', {}).get(t['id']))}")
        w("")

    if live:
        _live_extras(w, lv, shown)

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


def _live_extras(w, lv: dict, shown: list[str]) -> None:
    """Live summary, nodes that could not be asked, and what runs unintended."""
    counts = []
    for key, label in (("ospf", "OSPF adjacencies"), ("bgp", "BGP sessions"), ("vxlan", "VXLAN tunnels")):
        c = (lv.get("summary") or {}).get(key) or {}
        if c and (key in shown or (key == "vxlan" and "evpn" in shown)):
            counts.append(f"{label}: " + ", ".join(f"{n} {s}" for s, n in sorted(c.items())))
    if counts:
        w("Live: " + "; ".join(counts))
    for host, err in sorted((lv.get("errors") or {}).items()):
        w(f"  host {host}: {err}")
    for node, n in sorted((lv.get("nodes") or {}).items()):
        for topic, err in sorted(n["errors"].items()):
            w(f"  {node}: {topic or 'all'}: {err}")
    extra = lv.get("extra") or {}
    rows = [("OSPF", e, f"neighbor {e['router_id']} on {e['iface']}") for e in extra.get("ospf", [])
            if "ospf" in shown]
    rows += [("BGP", e, f"neighbor {e['ip']} AS {e['asn']}") for e in extra.get("bgp", []) if "bgp" in shown]
    rows += [("EVPN", e, f"neighbor {e['ip']} AS {e['asn']}") for e in extra.get("evpn", [])
             if "evpn" in shown or "bgp" in shown]
    if rows:
        w("Running but not in the startup configs:")
        for proto, e, what in rows:
            peer = f" ({e['peer']})" if e.get("peer") else ""
            w(f"  {proto} {e['node']}: {what}{peer}  [{e['detail'] or e['state']}]")
    w("")
