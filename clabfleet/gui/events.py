"""State changes seen in a lab's live reads: the GUI's event timeline.

The live probes read a lab every few seconds while someone has it open
(links and nodes on the Diagram, protocols on the Routing tab in Live mode
or in the Health panel). Comparing each read with the one before gives the
changes: "BGP Spine-1 ↔ Leaf-2 up → down at 14:02:11", so a session that
flapped is still visible after it came back.

Nothing is recorded for the first read of a lab or a failed read; the log
lives in memory and keeps the last ``KEEP`` changes per lab.
"""

import threading
from collections import deque
from typing import Optional

KEEP = 500


class EventLog:
    def __init__(self, keep: int = KEEP):
        self.keep = keep
        self._events: dict[str, deque] = {}
        self._last: dict[tuple[str, str], dict[str, str]] = {}  # (lab id, source) -> key -> state
        self._lock = threading.Lock()

    def observe(self, topo_id: str, source: str, items: dict[str, dict], now: float) -> list[dict]:
        """Record what changed since the last read from ``source`` ("links"
        or "protocols"). ``items``: key -> {"state", "label", "kind", "id",
        "detail"?}. Returns the new events."""
        new = []
        with self._lock:
            last = self._last.get((topo_id, source))
            if last is not None:
                for key, item in items.items():
                    before = last.get(key)
                    if before is not None and before != item["state"]:
                        new.append({"t": now, "kind": item["kind"], "id": item["id"],
                                    "label": item["label"], "from": before, "to": item["state"],
                                    "detail": item.get("detail", "")})
            self._last[(topo_id, source)] = {k: v["state"] for k, v in items.items()}
            if new:
                log = self._events.setdefault(topo_id, deque(maxlen=self.keep))
                log.extend(new)
        return new

    def events(self, topo_id: str, since: Optional[float] = None) -> list[dict]:
        """The lab's recorded changes, oldest first (after ``since``)."""
        with self._lock:
            events = list(self._events.get(topo_id, ()))
        return [e for e in events if since is None or e["t"] > since]


def link_items(snapshot: dict) -> dict[str, dict]:
    """Links of a live snapshot as event items."""
    items = {}
    for link_id, link in (snapshot.get("links") or {}).items():
        a, b = link.get("a") or {}, link.get("b") or {}
        ends = [f"{e['node']}:{e['iface']}" for e in (a, b) if e.get("node")]
        down = [f"{e['node']}:{e['iface']} {e.get('detail', '')}".strip()
                for e in (a, b) if e.get("state") == "down"]
        items[f"link:{link_id}"] = {"kind": "link", "id": link_id, "state": link.get("state", ""),
                                    "label": " ↔ ".join(ends), "detail": ", ".join(down)}
    return items


def protocol_items(snapshot: dict, view: dict) -> dict[str, dict]:
    """OSPF adjacencies, BGP sessions and VXLAN tunnels of a protocol
    snapshot as event items, named after their ends in ``view``."""
    ends = {}
    for a in (view.get("ospf") or {}).get("adjacencies") or []:
        ends[a["id"]] = ("ospf", "OSPF", a["a"]["node"], a["b"]["node"])
    for s in (view.get("bgp") or {}).get("sessions") or []:
        ends[s["id"]] = ("bgp", "BGP", s["a"]["node"], s["b"]["node"])
    for t in (view.get("evpn") or {}).get("tunnels") or []:
        ends[t["id"]] = ("evpn", "VXLAN", t["a"]["node"], t["b"]["node"])
    items = {}
    for key in ("ospf", "bgp", "vxlan"):
        for edge_id, entry in (snapshot.get(key) or {}).items():
            if edge_id not in ends:
                continue
            proto, tag, a, b = ends[edge_id]
            items[f"{key}:{edge_id}"] = {
                "kind": proto, "id": edge_id, "state": entry.get("state", ""),
                "label": f"{tag} {a.replace('ext:', '')} ↔ {b.replace('ext:', '')}",
                "detail": entry.get("detail", ""),
            }
    return items
