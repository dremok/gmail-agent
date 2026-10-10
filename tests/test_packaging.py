"""pyproject.toml ranges, requirements.txt pins and uv.lock must describe the same dependencies."""

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent


def requirements(lines: list[str]) -> dict[str, Requirement]:
    reqs = [Requirement(line) for line in lines if line.strip() and not line.startswith("#")]
    return {canonicalize_name(r.name): r for r in reqs}


def test_requirements_pin_the_locked_versions_inside_the_declared_ranges():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    declared = requirements(project["dependencies"])
    pinned = requirements((ROOT / "requirements.txt").read_text().splitlines())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = {canonicalize_name(p["name"]): p.get("version") for p in lock["package"]}

    assert pinned.keys() == declared.keys()
    for name, pin in pinned.items():
        [spec] = pin.specifier
        assert spec.operator == "==", name
        assert spec.version == locked[name], name
        assert declared[name].specifier.contains(spec.version), name
        assert pin.extras == declared[name].extras, name
        assert all(s.operator != "==" for s in declared[name].specifier), name
