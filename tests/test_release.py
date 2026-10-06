"""What a release relies on: one version, the changelog, the packaged files."""

import re
import tomllib
from pathlib import Path

import clabfleet
from clabfleet import cli

ROOT = Path(__file__).resolve().parent.parent


def test_version_is_written_once_and_printed(capsys):
    assert re.fullmatch(r"\d+\.\d+\.\d+", clabfleet.__version__)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    # pyproject.toml reads it from the package rather than repeating it
    assert "version" not in project["project"] and "version" in project["project"]["dynamic"]
    assert project["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "clabfleet.__version__"}
    try:
        cli.main(["--version"])
    except SystemExit as stop:
        assert stop.code == 0
    assert capsys.readouterr().out.strip() == f"clabfleet {clabfleet.__version__}"


def test_changelog_has_the_current_version():
    """The release workflow takes its notes from this section."""
    sections = re.findall(r"^## (\S+)$", (ROOT / "CHANGELOG.md").read_text(), re.M)
    assert sections and sections[0] == clabfleet.__version__


def test_every_gui_file_is_packaged():
    """A file in a folder that package-data does not name would be left
    out of the wheel, and the GUI would break only once installed."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    patterns = project["tool"]["setuptools"]["package-data"]["clabfleet.gui"]
    gui = ROOT / "clabfleet" / "gui"
    packaged = {p for pattern in patterns for p in gui.glob(pattern) if p.is_file()}
    present = {p for p in (gui / "static").rglob("*") if p.is_file()}
    assert present and present == packaged
