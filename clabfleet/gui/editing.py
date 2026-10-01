"""Edit topology files from the GUI without disturbing their formatting.

Saving node positions goes through ruamel.yaml's round-trip mode, which
keeps comments, key order, quoting and indentation, so only the
``graph-posX`` / ``graph-posY`` labels change in the file.
"""

import hashlib
import io
import os
import tempfile
from pathlib import Path

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


def write_if_unchanged(path: Path, text: str, base_hash: str) -> None:
    """Atomically replace ``path`` with ``text`` unless it changed since ``base_hash``."""
    current = path.read_text()
    if base_hash and text_hash(current) != base_hash:
        raise EditConflict(
            f"{path.name} changed on disk since it was opened; revert to load the new version"
        )
    mode = path.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
