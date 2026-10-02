"""Compare a fresh live-network import with an existing topology file.

``clabfleet export-live -o lab.clab.yml`` on a file that already exists
does not overwrite it. It reports what changed since the last import:

- nodes added or removed, and nodes whose kind, type, image or startup
  config differ
- links added or removed, and links between the same two nodes that moved
  to other interfaces

and only changes the file with ``--apply``. Applying edits the YAML in
place (comments, order, labels such as GUI positions, hand-added nodes
and links all stay) and updates only what the import owns:

- new nodes and links are added; changed links get their new endpoints
- a changed node gets its new kind/type/image (a placeholder image never
  replaces a real one) and its config file is rewritten if the node still
  points at ``configs/<node>.cfg``; a node whose ``startup-config`` was
  changed by hand keeps it, and the new config is saved next to it
- removed nodes and links are only deleted with ``--prune``

Only nodes recorded in the previous import report count as removed, so
nodes added by hand are never reported or pruned. Without a report, every
node in the file is treated as imported.
"""

import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .exporter import (
    CONFIG_DIR,
    PLACEHOLDER_IMAGE,
    ImportResult,
    load_report,
    write_configs,
    write_report,
)
from .topology import load_topology

# Config lines that change on every fetch without a real change
VOLATILE_LINE = re.compile(
    r"^(! Last configuration change|! NVRAM config last updated|! No configuration change"
    r"|!Time:|!Running configuration last done|## Last commit|! Command: |!Command: )"
)
NODE_FIELDS = ("kind", "type", "image")

LinkKey = tuple[str, str]  # sorted ("node:iface", "node:iface")


@dataclass
class SyncDiff:
    added_nodes: list[str] = field(default_factory=list)
    removed_nodes: list[str] = field(default_factory=list)
    changed_nodes: dict[str, dict] = field(default_factory=dict)  # node → {field: [old, new]}
    added_links: list[LinkKey] = field(default_factory=list)
    removed_links: list[LinkKey] = field(default_factory=list)
    changed_links: list[tuple[LinkKey, LinkKey]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added_nodes or self.removed_nodes or self.changed_nodes
                    or self.added_links or self.removed_links or self.changed_links)

    def as_dict(self) -> dict:
        return {
            "nodes": {
                "added": self.added_nodes,
                "removed": self.removed_nodes,
                "changed": self.changed_nodes,
            },
            "links": {
                "added": [list(k) for k in self.added_links],
                "removed": [list(k) for k in self.removed_links],
                "changed": [{"old": list(o), "new": list(n)} for o, n in self.changed_links],
            },
        }

    def as_text(self) -> str:
        lines = []
        for name in self.added_nodes:
            lines.append(f"+ node {name}")
        for name in self.removed_nodes:
            lines.append(f"- node {name}")
        for name, changes in self.changed_nodes.items():
            parts = []
            for key, (old, new) in changes.items():
                parts.append("startup config changed" if key == "startup-config"
                             else f"{key} {old} -> {new}")
            lines.append(f"~ node {name}: {'; '.join(parts)}")
        for key in self.added_links:
            lines.append(f"+ link {_fmt(key)}")
        for key in self.removed_links:
            lines.append(f"- link {_fmt(key)}")
        for old, new in self.changed_links:
            lines.append(f"~ link {_fmt(old)} -> {_fmt(new)}")
        return "\n".join(lines) if lines else "No changes."


def _fmt(key: LinkKey) -> str:
    return " -- ".join(key)


def _link_key(a: str, b: str) -> LinkKey:
    return tuple(sorted((a, b)))  # type: ignore[return-value]


def _pair(key: LinkKey) -> tuple[str, ...]:
    return tuple(sorted(ep.split(":", 1)[0] for ep in key))


def _strip_volatile(text: str) -> str:
    return "\n".join(ln.rstrip() for ln in text.splitlines()
                     if not VOLATILE_LINE.match(ln)).strip()


def diff_topology(existing: str | Path, result: ImportResult) -> SyncDiff:
    """What applying ``result`` to the topology file ``existing`` would change."""
    existing = Path(existing)
    topo = load_topology(existing)
    previous = load_report(existing)
    imported_before = set((previous.get("nodes") or {}).keys()) or set(topo.nodes)
    new_nodes = result.topology["topology"]["nodes"]
    diff = SyncDiff()

    diff.added_nodes = [n for n in new_nodes if n not in topo.nodes]
    diff.removed_nodes = [n for n in topo.nodes if n not in new_nodes and n in imported_before]

    for name, new in new_nodes.items():
        if name not in topo.nodes:
            continue
        old = topo.effective_node(name)
        changes = {}
        for key in NODE_FIELDS:
            if new.get(key) == old.get(key):
                continue
            if key == "image" and new.get(key) == PLACEHOLDER_IMAGE.format(kind=new["kind"]):
                continue  # keep an image set by hand
            if key not in new and key != "image":
                continue  # nothing imported for it (e.g. type set by hand)
            changes[key] = [old.get(key), new.get(key)]
        new_cfg = _new_config(name, new, result)
        old_cfg = _old_config(name, old, topo.base_dir)
        if new_cfg is not None and (old_cfg is None
                                    or _strip_volatile(old_cfg) != _strip_volatile(new_cfg)):
            changes["startup-config"] = [old.get("startup-config"), new.get("startup-config")]
        if changes:
            diff.changed_nodes[name] = changes

    old_links = _existing_links(topo)
    new_links = [_link_key(*link["endpoints"]) for link in result.topology["topology"]["links"]]
    added = [k for k in new_links if k not in old_links]
    removed = [k for k in old_links
               if k not in new_links and all(n in imported_before for n in _pair(k))]
    for old_key in list(removed):
        match = next((k for k in added if _pair(k) == _pair(old_key)), None)
        if match:
            diff.changed_links.append((old_key, match))
            removed.remove(old_key)
            added.remove(match)
    diff.added_links, diff.removed_links = added, removed
    return diff


def _new_config(name: str, node: dict, result: ImportResult) -> Optional[str]:
    if name in result.configs:
        return result.configs[name]
    value = node.get("startup-config")
    return value if isinstance(value, str) and "\n" in value else None


def _old_config(name: str, node: dict, base_dir: Path) -> Optional[str]:
    """The config the last import saved for a node.

    For a node pointed at another file by hand, that is the import's own
    ``configs/<node>.cfg`` if it is there, so hand edits are not a change.
    """
    value = node.get("startup-config")
    if not isinstance(value, str):
        return None
    if "\n" in value:
        return value
    own = base_dir / CONFIG_DIR / f"{name}.cfg"
    path = own if own.is_file() else base_dir / value
    return path.read_text() if path.is_file() else None


def _existing_links(topo) -> list[LinkKey]:
    keys = []
    for link in topo.links:
        eps = [ep for ep in link.endpoints if ep.node in topo.nodes and ep.interface]
        if len(eps) == 2:
            keys.append(_link_key(f"{eps[0].node}:{eps[0].interface}",
                                  f"{eps[1].node}:{eps[1].interface}"))
    return keys


# --- Apply --------------------------------------------------------------------

def apply_diff(existing: str | Path, result: ImportResult, diff: SyncDiff,
               prune: bool = False) -> list[str]:
    """Apply ``diff`` to the topology file in place; returns what was done."""
    existing = Path(existing)
    text = existing.read_text()
    loader = _RoundTrip(text)
    data = loader.data
    nodes = data["topology"]["nodes"]
    if data["topology"].get("links") is None:
        data["topology"]["links"] = []
    links = data["topology"]["links"]
    new_nodes = result.topology["topology"]["nodes"]
    base_dir = existing.parent
    done: list[str] = []
    configs_to_write: set[str] = set()

    for name in diff.added_nodes:
        nodes[name] = dict(new_nodes[name])
        configs_to_write.add(name)
        done.append(f"added node {name}")

    for name, changes in diff.changed_nodes.items():
        node = nodes[name]
        if node is None:
            node = nodes[name] = {}
        updated = [key for key in NODE_FIELDS if key in changes]
        for key in updated:
            node[key] = changes[key][1]
        if "startup-config" in changes:
            current = node.get("startup-config")
            default_ref = f"{CONFIG_DIR}/{name}.cfg"
            inline = isinstance(current, str) and "\n" in current
            if inline:  # keep it inline
                node["startup-config"] = result.configs.get(
                    name, new_nodes[name].get("startup-config"))
                updated.append("startup-config")
            elif current in (None, default_ref):
                node["startup-config"] = default_ref
                configs_to_write.add(name)
                updated.append("startup-config")
            else:
                configs_to_write.add(name)
                done.append(f"{name}: kept hand-set startup-config {current}; "
                            f"new config saved to {default_ref}")
        if updated:
            done.append(f"updated node {name} ({', '.join(updated)})")

    for old, new in diff.changed_links:
        for link in links:
            if _raw_link_key(link) == old:
                _set_endpoints(link, new)
                done.append(f"moved link {_fmt(old)} -> {_fmt(new)}")
                break

    for key in diff.added_links:
        links.append({"endpoints": list(key)})
        done.append(f"added link {_fmt(key)}")

    if prune:
        for key in diff.removed_links:
            for i, link in enumerate(links):
                if _raw_link_key(link) == key:
                    del links[i]
                    done.append(f"removed link {_fmt(key)}")
                    break
        for name in diff.removed_nodes:
            del nodes[name]
            for i in reversed(range(len(links))):
                if name in _raw_link_nodes(links[i]):
                    del links[i]
            done.append(f"removed node {name}")

    if not prune:
        # Kept nodes stay "imported", so a later --prune can still remove them
        previous = load_report(existing).get("nodes") or {}
        for name in diff.removed_nodes:
            result.report["nodes"][name] = {**(previous.get(name) or {}), "gone": True}
    write_configs(result, base_dir, nodes=configs_to_write)
    _atomic_write(existing, loader.dump())
    write_report(result, existing)
    return done


def _raw_link_nodes(link) -> list[str]:
    eps = link.get("endpoints") or []
    out = []
    for ep in eps:
        if isinstance(ep, str):
            out.append(ep.split(":", 1)[0])
        elif isinstance(ep, dict):
            out.append(ep.get("node"))
    return out


def _raw_link_key(link) -> Optional[LinkKey]:
    eps = link.get("endpoints") or []
    names = []
    for ep in eps:
        if isinstance(ep, str):
            names.append(ep)
        elif isinstance(ep, dict) and ep.get("node") and ep.get("interface"):
            names.append(f"{ep['node']}:{ep['interface']}")
    return _link_key(*names) if len(names) == 2 else None


def _set_endpoints(link, key: LinkKey) -> None:
    eps = link["endpoints"]
    by_node = {ep.split(":", 1)[0]: ep for ep in key}
    for i, ep in enumerate(eps):
        if isinstance(ep, str):
            eps[i] = by_node.get(ep.split(":", 1)[0], ep)
        elif isinstance(ep, dict) and ep.get("node") in by_node:
            ep["interface"] = by_node[ep["node"]].split(":", 1)[1]


class _RoundTrip:
    """Load/dump YAML keeping comments and formatting (ruamel.yaml if installed)."""

    def __init__(self, text: str):
        try:
            from .gui.editing import _yaml_for
            self._yaml = _yaml_for(text)
        except RuntimeError:
            self._yaml = None
        if self._yaml is not None:
            self.data = self._yaml.load(text)
        else:
            self.data = yaml.safe_load(text)

    def dump(self) -> str:
        if self._yaml is None:
            from .topology import dump_yaml
            return dump_yaml(self.data)
        from ruamel.yaml.scalarstring import LiteralScalarString
        _literal_configs(self.data, LiteralScalarString)
        out = io.StringIO()
        self._yaml.dump(self.data, out)
        return out.getvalue()


def _literal_configs(data, literal) -> None:
    """Write multi-line strings (inline configs) as ``|`` blocks."""
    for node in (data["topology"].get("nodes") or {}).values():
        if node and isinstance(node.get("startup-config"), str) \
                and "\n" in node["startup-config"]:
            node["startup-config"] = literal(node["startup-config"])


def _atomic_write(path: Path, text: str) -> None:
    from .gui.editing import write_if_unchanged
    write_if_unchanged(path, text, base_hash="")
