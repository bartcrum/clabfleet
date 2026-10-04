"""Diagram annotations: sticky notes and labelled boxes ("DC1", "tenant A")
drawn on a lab's diagram.

They are kept next to the topology file, in ``<file>.notes.json``, rather
than in it: containerlab checks the topology against its schema, and a box
belongs to no node. The file travels with the topology (copy both).
"""

import json
import math
from pathlib import Path

from .editing import replace_file

MAX_ITEMS = 200       # notes and boxes, each
MAX_TEXT = 500        # characters in a note or a box label
MAX_COORD = 1e6
MAX_SIZE = 1e5
COLORS = 8            # the --c0 .. --c7 palette
SUFFIX = ".notes.json"


def empty() -> dict:
    return {"notes": [], "boxes": []}


def path_for(topology: Path) -> Path:
    return topology.with_name(topology.name + SUFFIX)


def _num(value, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} is out of range")
    return round(value, 1)


def _text(value, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    if len(value) > MAX_TEXT:
        raise ValueError(f"{name} is longer than {MAX_TEXT} characters")
    return value


def _id(value, seen: set) -> str:
    if not isinstance(value, str) or not value or len(value) > 40 or value in seen:
        raise ValueError("each annotation needs its own short id")
    seen.add(value)
    return value


def clean(data) -> dict:
    """The annotations in ``data``, checked; ValueError if anything is off."""
    if not isinstance(data, dict):
        raise ValueError("Expected {notes: [...], boxes: [...]}")
    notes, boxes = data.get("notes", []), data.get("boxes", [])
    if not isinstance(notes, list) or not isinstance(boxes, list):
        raise ValueError("notes and boxes must be lists")
    if len(notes) > MAX_ITEMS or len(boxes) > MAX_ITEMS:
        raise ValueError(f"At most {MAX_ITEMS} notes and {MAX_ITEMS} boxes")
    seen: set = set()
    out = empty()
    for n in notes:
        if not isinstance(n, dict):
            raise ValueError("A note must be an object")
        out["notes"].append({
            "id": _id(n.get("id"), seen),
            "x": _num(n.get("x"), "x", -MAX_COORD, MAX_COORD),
            "y": _num(n.get("y"), "y", -MAX_COORD, MAX_COORD),
            "text": _text(n.get("text", ""), "A note"),
        })
    for b in boxes:
        if not isinstance(b, dict):
            raise ValueError("A box must be an object")
        color = b.get("color", 0)
        if isinstance(color, bool) or not isinstance(color, int) or not 0 <= color < COLORS:
            raise ValueError(f"color must be 0 to {COLORS - 1}")
        out["boxes"].append({
            "id": _id(b.get("id"), seen),
            "x": _num(b.get("x"), "x", -MAX_COORD, MAX_COORD),
            "y": _num(b.get("y"), "y", -MAX_COORD, MAX_COORD),
            "w": _num(b.get("w"), "width", 20, MAX_SIZE),
            "h": _num(b.get("h"), "height", 20, MAX_SIZE),
            "label": _text(b.get("label", ""), "A box label"),
            "color": color,
        })
    return out


def load(topology: Path) -> dict:
    """The lab's annotations; none if the file is missing or unreadable."""
    try:
        return clean(json.loads(path_for(topology).read_text()))
    except (OSError, ValueError):
        return empty()


def save(topology: Path, data) -> dict:
    """Check and write the annotations (atomically); no file when empty."""
    cleaned = clean(data)
    target = path_for(topology)
    if not cleaned["notes"] and not cleaned["boxes"]:
        target.unlink(missing_ok=True)
        return cleaned
    replace_file(target, json.dumps(cleaned, indent=1) + "\n")
    return cleaned
