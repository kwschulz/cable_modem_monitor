#!/usr/bin/env python3
"""Validate Core and Catalog declared dependencies against HA's package constraints.

Home Assistant pins specific package versions in package_constraints.txt. Library
packages that declare floors above HA's pins will fail to install at runtime even
though they install fine in a standalone environment (e.g., the beta.4 incident
where requests>=2.34.2 and pyyaml>=6.0.3 both exceeded HA's pins).

Exit codes:
  0  Every declared dependency satisfies HA's pins
  1  At least one declared floor conflicts with an HA pin
  2  Cannot check — homeassistant is not installed, so there are no
     constraints to compare against

2 is deliberately not 0. A gate has three outcomes and only two exit
codes are ever reached by accident: collapsing "couldn't check" into
"passed" makes an unverified run indistinguishable from a verified one,
and nothing downstream ever notices, because a gate that wrongly passes
generates no signal.
"""

import importlib.util
import re
import sys
import tomllib
from pathlib import Path

try:
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version
except ImportError:
    print("  ERROR: 'packaging' library not found. Install requirements-dev.txt first.")
    sys.exit(1)

ROOT = Path(__file__).parent.parent

_INTERNAL_PREFIX = "solentlabs-"


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]", "-", name).lower()


def _find_ha_constraints() -> Path | None:
    spec = importlib.util.find_spec("homeassistant")
    if not spec or not spec.submodule_search_locations:
        return None
    ha_dir = Path(list(spec.submodule_search_locations)[0])
    path = ha_dir / "package_constraints.txt"
    return path if path.exists() else None


def _parse_ha_constraints(path: Path) -> dict[str, str]:
    """Return {normalized_name: pinned_version} for all exact-pinned packages."""
    pins: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;]+)", line)
        if m:
            pins[_normalize(m.group(1))] = m.group(2)
    return pins


def _parse_pyproject_deps(path: Path) -> list[tuple[str, str]]:
    """Return [(normalized_name, specifier_string)] from [project.dependencies]."""
    if not path.exists():
        return []
    data = tomllib.loads(path.read_text())
    result = []
    for dep in data.get("project", {}).get("dependencies", []):
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*(.*?)(?:\s*;.*)?$", dep.strip())
        if not m:
            continue
        name = _normalize(m.group(1))
        if name.startswith(_INTERNAL_PREFIX):
            continue
        spec = m.group(2).strip()
        result.append((name, spec))
    return result


def main() -> int:
    constraints_path = _find_ha_constraints()
    if not constraints_path:
        print(
            "  ERROR: homeassistant is not installed, so its package constraints"
            " cannot be read — this check verified nothing.",
            file=sys.stderr,
        )
        print("  Fix: pip install homeassistant (CI installs it for this job).", file=sys.stderr)
        return 2

    ha_pins = _parse_ha_constraints(constraints_path)

    pyprojects = [
        ROOT / "packages" / "cable_modem_monitor_core" / "pyproject.toml",
        ROOT / "packages" / "cable_modem_monitor_catalog" / "pyproject.toml",
    ]

    conflicts: list[str] = []

    for pyproject in pyprojects:
        pkg_label = pyproject.parent.name
        for name, spec_str in _parse_pyproject_deps(pyproject):
            if name not in ha_pins or not spec_str:
                continue
            ha_version = ha_pins[name]
            try:
                if Version(ha_version) not in SpecifierSet(spec_str):
                    conflicts.append(
                        f"  {pkg_label}: {name}{spec_str} — "
                        f"HA pins {name}=={ha_version}, which does not satisfy"
                        f" {spec_str}"
                    )
            except Exception as exc:
                conflicts.append(f"  {pkg_label}: could not parse {name}{spec_str}: {exc}")

    if conflicts:
        noun = "conflict" if len(conflicts) == 1 else "conflicts"
        print(f"  ❌ {len(conflicts)} HA compatibility {noun} found:")
        for c in conflicts:
            print(c)
        print(
            "  Fix: lower the floor in packages/cable_modem_monitor_core/pyproject.toml" " to a version HA can satisfy."
        )
        return 1

    print("  ✅ All declared dependencies satisfy HA's package constraints.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
