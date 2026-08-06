#!/usr/bin/env python3
"""Fail when a shipping module cannot be imported on its own.

Catches import cycles that only surface on a leaf-first import. A cycle
between two packages is invisible when something always imports the
"safe" side first, so the suite stays green while any consumer importing
the leaf directly gets ImportError. Both cycles this repo has had
(analysis.actions/auth, analysis.format/mapping) had that shape.

Each module is imported into a fresh interpreter state: the package tree
is purged from sys.modules first, so import order is the module's own,
not whatever a previous import left cached.

With no arguments, sweeps every shipping module (~30s, for validate-ci).
Given paths, checks only those (fast enough for pre-commit). Scoping to
staged files is sound for catching a cycle as it lands: introducing one
means editing a module inside it.
"""

from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Published packages only. These are what third parties import directly,
# so leaf-first import is a real access pattern, not a hypothetical.
NAMESPACE = "solentlabs"
PACKAGE_ROOTS = (
    REPO_ROOT / "packages" / "cable_modem_monitor_core" / NAMESPACE,
    REPO_ROOT / "packages" / "cable_modem_monitor_catalog_tools" / NAMESPACE,
)


def _module_name(path: Path, root: Path) -> str:
    """Dotted module name for a source path under a package root."""
    parts = list(path.relative_to(root.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _all_modules() -> list[str]:
    """Every importable module under the published package roots."""
    return [_module_name(p, root) for root in PACKAGE_ROOTS for p in sorted(root.rglob("*.py"))]


def _modules_for(paths: list[str]) -> list[str]:
    """Module names for the given paths, skipping anything outside the roots."""
    names: list[str] = []
    for raw in paths:
        path = Path(raw).resolve()
        for root in PACKAGE_ROOTS:
            if path.is_relative_to(root) and path.suffix == ".py":
                names.append(_module_name(path, root))
                break
    return sorted(set(names))


def _purge() -> None:
    """Drop the namespace from sys.modules so the next import runs cold."""
    for name in [n for n in sys.modules if n.split(".")[0] == NAMESPACE]:
        del sys.modules[name]


def main() -> int:
    args = sys.argv[1:]
    modules = _modules_for(args) if args else _all_modules()
    if not modules:
        return 0

    scope = f"{len(modules)} changed" if args else f"all {len(modules)} shipping"
    print(f"🔍 Checking {scope} module(s) import standalone...")
    failures: list[tuple[str, str]] = []

    for name in modules:
        _purge()
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 - any import-time failure is a defect
            # Last frame is where it actually broke; the rest is the
            # import chain, which is the useful part for a cycle.
            failures.append((name, traceback.format_exc(limit=-1).strip()))

    if failures:
        print(f"\n❌ {len(failures)} module(s) cannot be imported on their own:\n")
        for name, tb in failures:
            print(f"  {name}")
            for line in tb.splitlines():
                print(f"      {line}")
            print()
        print("An import cycle is the usual cause. Defer the from-package")
        print("import into the function that uses it, or move the shared")
        print("symbol to a module both sides can import.")
        return 1

    print(f"✅ {len(modules)} module(s) import standalone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
