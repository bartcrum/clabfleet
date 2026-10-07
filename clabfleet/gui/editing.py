"""Edit topology files from the GUI without disturbing their formatting.

Saving node positions goes through ruamel.yaml's round-trip mode, which
keeps comments, key order, quoting and indentation, so only the
``graph-posX`` / ``graph-posY`` labels change in the file.
"""

import hashlib
import io
import re
import os
import tempfile
from pathlib import Path
from typing import Optional

from ..topology import LABEL_SPARE, is_spare_link

POS_X, POS_Y = "graph-posX", "graph-posY"


class EditConflict(Exception):
    """The file changed on disk since the editor loaded it."""


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _yaml_for(text: str):
    """A round-trip YAML instance whose output matches the file's indentation."""
    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.util import load_yaml_guess_indent
    except ImportError as exc:
        raise RuntimeError(
            "Saving positions needs ruamel.yaml: pip install 'clabfleet[gui]'"
        ) from exc
    _, seq_indent, dash_offset = load_yaml_guess_indent(text)
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096  # never re-wrap long lines
    if dash_offset is None:  # no block sequences to learn from
        mapping = seq_indent or 2
        yaml.indent(mapping=mapping, sequence=mapping + 2, offset=2)
    else:
        yaml.indent(mapping=seq_indent - dash_offset, sequence=seq_indent, offset=dash_offset)
    return yaml


def set_positions(text: str, positions: dict[str, tuple[float, float]]) -> str:
    """Return ``text`` with graph-posX/graph-posY labels set for the given nodes.

    Unknown node names are ignored. Values are whole numbers, as strings
    (containerlab labels are strings).
    """
    from ruamel.yaml.comments import CommentedMap

    yaml = _yaml_for(text)
    data = yaml.load(text)
    nodes = data["topology"]["nodes"]
    for name, (x, y) in positions.items():
        if name not in nodes:
            continue
        node = nodes[name]
        if node is None:
            node = nodes[name] = CommentedMap()
        labels = node.get("labels")
        if labels is None:
            labels = node["labels"] = CommentedMap()
        labels[POS_X] = str(round(x))
        labels[POS_Y] = str(round(y))
    out = io.StringIO()
    yaml.dump(data, out)
    return out.getvalue()


def replace_file(path: Path, text: str, mode: Optional[int] = None) -> None:
    """Atomically replace ``path`` with ``text``: readers see the old file or
    the new one, never half of it. The new file is 0600 unless ``mode`` says
    otherwise."""
    # mkstemp: a fresh 0600 file, so nothing planted in the directory
    # can redirect or pre-open the write
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_if_unchanged(path: Path, text: str, base_hash: str) -> None:
    """Atomically replace ``path`` with ``text`` unless it changed since ``base_hash``."""
    current = path.read_text()
    if base_hash and text_hash(current) != base_hash:
        raise EditConflict(
            f"{path.name} changed on disk since it was opened; revert to load the new version"
        )
    replace_file(path, text, mode=path.stat().st_mode & 0o777)


# --- The topology builder: a drawn graph back into the file ---

SPECIAL_PREFIXES = ("host", "mgmt-net", "macvlan", "vxlan", "vxlan-stitch", "dummy", "bridge", "ovs-bridge")


def _simple_link(link) -> Optional[tuple[str, str]]:
    """(a, b) for a plain ``endpoints: ["n1:if", "n2:if"]`` link between two
    lab nodes; None for any other form, which the builder leaves alone."""
    if not isinstance(link, dict) or link.get("type") is not None:
        return None
    ends = link.get("endpoints")
    if not isinstance(ends, list) or len(ends) != 2 or not all(isinstance(e, str) for e in ends):
        return None
    if any(e.partition(":")[0] in SPECIAL_PREFIXES for e in ends):
        return None
    return str(ends[0]), str(ends[1])


def _link_nodes(link) -> set[str]:
    """Every node a link of any form names."""
    names = set()
    if not isinstance(link, dict):
        return names
    for ep in link.get("endpoints") or []:
        if isinstance(ep, str):
            names.add(ep.partition(":")[0])
        elif isinstance(ep, dict) and ep.get("node"):
            names.add(str(ep["node"]))
    ep = link.get("endpoint")
    if isinstance(ep, dict) and ep.get("node"):
        names.add(str(ep["node"]))
    return names


def _rename_in_link(link, old: str, new: str) -> None:
    eps = link.get("endpoints") if isinstance(link, dict) else None
    for i, ep in enumerate(eps or []):
        if isinstance(ep, str) and ep.partition(":")[0] == old:
            eps[i] = type(ep)(new + ":" + ep.partition(":")[2])  # keeps the quoting
        elif isinstance(ep, dict) and ep.get("node") == old:
            ep["node"] = new
    ep = link.get("endpoint") if isinstance(link, dict) else None
    if isinstance(ep, dict) and ep.get("node") == old:
        ep["node"] = new


# --- Spare ports: ports with nothing plugged in, and cables between them ---

def _links_of(data):
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    topo = data.get("topology") if isinstance(data, dict) else None
    if not isinstance(topo, dict):
        raise ValueError("the file has no 'topology:' section")
    if topo.get("links") is None:
        topo["links"] = CommentedSeq()
    return topo["links"], CommentedMap, CommentedSeq


def _spare_index(links, node: str, iface: str) -> Optional[int]:
    """Where the spare port ``node:iface`` is in ``links``, if it is one."""
    for i, link in enumerate(links):
        if is_spare_link(link):
            ep = link.get("endpoint") or {}
            if ep.get("node") == node and ep.get("interface") == iface:
                return i
    return None


def add_spare_ports(text: str, node: str, ifaces: list[str]) -> str:
    """``text`` with a spare port (a labelled dummy link) for each of
    ``ifaces`` of ``node``, after the other links."""
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString

    yaml = _yaml_for(text)
    data = yaml.load(text)
    links, CommentedMap, _ = _links_of(data)
    for iface in ifaces:
        endpoint = CommentedMap([("node", node), ("interface", iface)])
        endpoint.fa.set_flow_style()
        labels = CommentedMap([(LABEL_SPARE, DoubleQuotedScalarString("true"))])
        labels.fa.set_flow_style()
        links.append(CommentedMap([("type", "dummy"), ("endpoint", endpoint), ("labels", labels)]))
    out = io.StringIO()
    yaml.dump(data, out)
    return out.getvalue()


def cable_spare_ports(text: str, a: tuple[str, str], b: tuple[str, str]) -> str:
    """``text`` with a link between the spare ports ``a`` and ``b`` (each
    (node, interface)) in place of the two. ValueError if either is not a
    spare port of the file."""
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString

    if a == b:
        raise ValueError("A port cannot be cabled to itself")
    yaml = _yaml_for(text)
    data = yaml.load(text)
    links, CommentedMap, CommentedSeq = _links_of(data)
    for node, iface in (a, b):
        if _spare_index(links, node, iface) is None:
            raise ValueError(f"{node}:{iface} is not a spare port")
    for node, iface in (a, b):  # looked up again: the first removal moves the second
        del links[_spare_index(links, node, iface)]
    ends = CommentedSeq([DoubleQuotedScalarString(f"{a[0]}:{a[1]}"),
                         DoubleQuotedScalarString(f"{b[0]}:{b[1]}")])
    ends.fa.set_flow_style()
    links.append(CommentedMap([("endpoints", ends)]))
    out = io.StringIO()
    yaml.dump(data, out)
    return out.getvalue()


def uncable_ports(text: str, a: tuple[str, str], b: tuple[str, str]) -> str:
    """``text`` with the link between ``a`` and ``b`` (each (node,
    interface)) replaced by two spare ports. Only for a plain link between
    two lab nodes (``endpoints: ["n1:if", "n2:if"]``): one that carries more
    (addresses, a type, variables) has things to lose, and is left to the
    editor. ValueError if there is no such link."""
    yaml = _yaml_for(text)
    data = yaml.load(text)
    links, CommentedMap, _ = _links_of(data)
    wanted = {f"{a[0]}:{a[1]}", f"{b[0]}:{b[1]}"}
    found = None
    for i, link in enumerate(links):
        ends = link.get("endpoints") if isinstance(link, dict) else None
        if isinstance(ends, list) and len(ends) == 2 and {str(e) for e in ends} == wanted:
            found = i
            break
    if found is None or len(wanted) != 2:
        raise ValueError(f"There is no cable between {a[0]}:{a[1]} and {b[0]}:{b[1]}")
    if _simple_link(links[found]) is None or set(links[found]) - {"endpoints"}:
        raise ValueError(f"The link between {a[0]}:{a[1]} and {b[0]}:{b[1]} carries more than "
                         "its two ends (see the YAML): edit the file to remove it")
    del links[found]
    out = io.StringIO()
    yaml.dump(data, out)
    text = out.getvalue()
    for node, iface in (a, b):
        text = add_spare_ports(text, node, [iface])
    return text


def apply_graph(text: str, graph: dict, default_images: Optional[dict] = None) -> str:
    """Return ``text`` changed to match a graph drawn in the GUI builder.

    ``graph``: ``{"nodes": [{"name", "kind", "image"?, "pos"?, "rename_from"?,
    "config"?}], "links": [{"a": "node:iface", "b": "node:iface"}]}``.

    Only what differs is touched: nodes missing from the graph are removed
    (with every link naming them), new ones added, kind / image / position
    and generated configs (``config``: ``{"startup-config": text}`` or
    ``{"exec": [...]}``) set, renames applied to the node and its links.
    Plain node-to-node links follow the graph; links of other forms (host,
    macvlan, ...) are kept as they are. Comments, order and everything
    else in the file stay.

    A renamed node's ``hostname <old>`` line in an inline startup-config is
    renamed too. A node whose kind has no image here (none on the node, none
    under ``kinds``) gets ``default_images[kind]``, so it can deploy.
    """
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString, LiteralScalarString

    yaml = _yaml_for(text)
    data = yaml.load(text)
    topo = data.setdefault("topology", CommentedMap())
    if topo.get("nodes") is None:
        topo["nodes"] = CommentedMap()
    nodes = topo["nodes"]
    if topo.get("links") is None:
        topo["links"] = CommentedSeq()
    links = topo["links"]
    kinds = topo.get("kinds") or {}

    wanted = graph.get("nodes") or []
    names = [str(n["name"]) for n in wanted]
    if len(set(names)) != len(names):
        raise ValueError("Node names must be unique")

    # Renames first, keeping the node's place in the file
    for n in wanted:
        old, new = n.get("rename_from"), str(n["name"])
        if old and old != new and old in nodes:
            if new in nodes:
                raise ValueError(f"Cannot rename {old} to {new}: {new} exists")
            items = list(nodes.items())
            for key, _ in items:
                del nodes[key]
            for key, value in items:
                nodes[new if key == old else key] = value
            for link in links:
                _rename_in_link(link, old, new)
            config = (nodes[new] or {}).get("startup-config")
            if isinstance(config, str) and "\n" in config:
                renamed = re.sub(rf"(?m)^hostname {re.escape(old)}$", f"hostname {new}", config)
                if renamed != config:
                    nodes[new]["startup-config"] = LiteralScalarString(renamed)

    # Removed nodes, and every link naming them
    gone = [k for k in nodes if k not in set(names)]
    for k in gone:
        del nodes[k]
    for i in reversed(range(len(links))):
        if _link_nodes(links[i]) & set(gone):
            del links[i]

    for n in wanted:
        name, kind = str(n["name"]), str(n.get("kind") or "")
        node = nodes.get(name)
        if node is None:
            node = nodes[name] = CommentedMap()
        if kind and node.get("kind") != kind:
            node["kind"] = kind
        image = str(n.get("image") or "")
        default_image = ((kinds.get(node.get("kind")) or {}).get("image") or "")
        if image and image == default_image:
            node.pop("image", None)  # the kind's image covers it
        elif image and image != node.get("image"):
            node["image"] = image
        if (not node.get("image") and not (kinds.get(node.get("kind")) or {}).get("image")
                and (default_images or {}).get(node.get("kind"))):
            node["image"] = default_images[node["kind"]]
        pos = n.get("pos")
        if pos:
            labels = node.get("labels")
            if labels is None:
                labels = node["labels"] = CommentedMap()
            labels[POS_X] = str(round(float(pos[0])))
            labels[POS_Y] = str(round(float(pos[1])))
        for key, value in (n.get("config") or {}).items():
            if key == "startup-config":
                node[key] = LiteralScalarString(str(value))
            elif key == "exec":
                node[key] = [str(c) for c in value]
            else:
                raise ValueError(f"Unsupported config key {key}")

    # Plain links: follow the graph
    want = []
    for link in graph.get("links") or []:
        a, b = str(link["a"]), str(link["b"])
        for ep in (a, b):
            if ep.partition(":")[0] not in nodes or not ep.partition(":")[2]:
                raise ValueError(f"Link endpoint {ep} is not node:interface of a node")
        want.append((a, b))
    want_keys = {frozenset(w) for w in want}
    have = set()
    for i in reversed(range(len(links))):
        pair = _simple_link(links[i])
        if pair is None:
            continue
        if frozenset(pair) not in want_keys:
            del links[i]
        else:
            have.add(frozenset(pair))
    for a, b in want:
        if frozenset((a, b)) in have:
            continue
        ends = CommentedSeq([DoubleQuotedScalarString(a), DoubleQuotedScalarString(b)])
        ends.fa.set_flow_style()
        links.append(CommentedMap([("endpoints", ends)]))
        have.add(frozenset((a, b)))

    out = io.StringIO()
    yaml.dump(data, out)
    return out.getvalue()
