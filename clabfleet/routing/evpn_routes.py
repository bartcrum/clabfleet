"""EVPN routes a VTEP has: the hosts (type-2 MAC/IP) and prefixes (type-5)
learned on each VNI, and from which VTEP, from EOS's
``show bgp evpn route-type mac-ip detail | json`` and
``show bgp evpn route-type ip-prefix ipv4 detail | json``.
"""

import json


def _vni(label) -> object:
    value = (label or {}).get("value") if isinstance(label, dict) else label
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def parse_eos_evpn_routes(mac_ip_json: str, prefix_json: str, vteps: dict[str, str]) -> list[dict]:
    """One row per route (best path): {"type": "mac-ip"|"ip-prefix", "vni",
    "l3_vni", "mac", "ip", "prefix", "vtep", "from": node or "", "local"}.
    ``vteps`` maps VTEP address to lab node."""
    rows = []
    for text, kind in ((mac_ip_json, "mac-ip"), (prefix_json, "ip-prefix")):
        routes = (json.loads(text) if text.strip() else {}).get("evpnRoutes") or {}
        for key, route in routes.items():
            paths = route.get("evpnRoutePaths") or []
            if not paths:
                continue
            path = next((p for p in paths if (p.get("routeType") or {}).get("active")), paths[0])
            detail = path.get("routeDetail") or {}
            words = key.split()
            # "RD: <rd> mac-ip <mac> [<ip>]" / "RD: <rd> ip-prefix <prefix>"
            after = words[words.index(kind) + 1:] if kind in words else []
            vtep = path.get("nextHop") or ""
            rows.append({
                "type": kind,
                "vni": _vni(detail.get("label")),
                "l3_vni": _vni(detail.get("l3Label")) if detail.get("l3Label") else None,
                "mac": after[0] if kind == "mac-ip" and after else "",
                "ip": after[1] if kind == "mac-ip" and len(after) > 1 else "",
                "prefix": after[0] if kind == "ip-prefix" and after else "",
                "vtep": vtep,
                "from": vteps.get(vtep, ""),
                "local": not vtep,
            })
    rows.sort(key=lambda r: (str(r["vni"]), r["type"], r["local"] is False, r["mac"] or r["prefix"], r["ip"]))
    return rows
