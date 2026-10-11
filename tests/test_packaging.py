"""pyproject.toml ranges, requirements.txt pins and uv.lock must describe the same dependencies."""

import json
import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from gmail_agent import __version__

ROOT = Path(__file__).resolve().parent.parent


def requirements(lines: list[str]) -> dict[str, Requirement]:
    reqs = [Requirement(line) for line in lines if line.strip() and not line.startswith("#")]
    return {canonicalize_name(r.name): r for r in reqs}


def test_requirements_pin_the_locked_versions_inside_the_declared_ranges():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    declared = requirements(project["dependencies"])
    pinned = requirements((ROOT / "requirements.txt").read_text().splitlines())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked: dict[str, set[str]] = {}
    for p in lock["package"]:  # a package can be locked at one version per platform
        locked.setdefault(canonicalize_name(p["name"]), set()).add(p.get("version"))

    assert pinned.keys() == declared.keys()
    for name, pin in pinned.items():
        [spec] = pin.specifier
        assert spec.operator == "==", name
        assert spec.version in locked[name], name
        assert declared[name].specifier.contains(spec.version), name
        assert pin.extras == declared[name].extras, name
        assert pin.marker == declared[name].marker, name
        assert all(s.operator != "==" for s in declared[name].specifier), name


def test_intel_macs_get_a_cryptography_with_a_wheel():
    """cryptography 49 publishes macOS wheels for arm64 only, so Intel Macs are held at 48."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    crypto = requirements(project["dependencies"])["cryptography"]
    intel = {"sys_platform": "darwin", "platform_machine": "x86_64"}
    arm = {"sys_platform": "darwin", "platform_machine": "arm64"}
    assert crypto.marker.evaluate(intel)
    assert not crypto.marker.evaluate(arm)
    assert not crypto.specifier.contains("49.0.0")
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    versions = {p["version"] for p in lock["package"] if p["name"] == "cryptography"}
    assert any(not crypto.specifier.contains(v) for v in versions), "others got the cap too"


def test_one_version_everywhere():
    """pyproject.toml holds the version; the installed metadata, __version__, both versions in
    server.json and the Claude Code plugin with its pinned server must match it (the release
    workflow checks the tag too)."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    server = json.loads((ROOT / "server.json").read_text())
    [package] = server["packages"]
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert __version__ == project["version"]  # stale? run `uv sync`
    assert server["version"] == package["version"] == project["version"]
    assert package["identifier"] == project["name"]
    assert plugin["version"] == project["version"]
    assert plugin["mcpServers"]["gmail"]["args"] == [
        f"{project['name']}=={project['version']}",
        "mcp",
    ]


def test_server_json_fits_the_registry():
    server = json.loads((ROOT / "server.json").read_text())
    readme = (ROOT / "README.md").read_text()
    assert f"<!-- mcp-name: {server['name']} -->" in readme  # PyPI ownership proof
    assert len(server["description"]) <= 100
    assert re.fullmatch(r"[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+", server["name"])
    assert server["packages"][0]["packageArguments"] == [{"type": "positional", "value": "mcp"}]
