"""Shared test helpers for the Home Assistant integration tests.

Deliberately mirrors ``packages/cable_modem_monitor_core/tests/_helpers.py``
rather than importing it. Core is a standalone PyPI library whose test
package is not shipped, and both roots contain a package named ``tests``,
so an import would resolve differently depending on pytest's rootdir.

Named ``fixture_helpers`` rather than ``_helpers`` for that same reason:
catalog_tools puts Core's package root on its pythonpath and imports
``tests._helpers``, so a module of that name here shadows Core's and
breaks resolution for anything Core exports that this file does not.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]


def collect_fixtures(directory: Path) -> list[Path]:
    """Collect all JSON fixture files from a directory, sorted by name."""
    return sorted(directory.glob("*.json"))


def load_fixture(path: Path) -> dict[str, Any]:
    """Load a JSON fixture file and return the parsed dict."""
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def load_script(relative_path: str) -> ModuleType:
    """Import a script from ``scripts/`` by path and return the module.

    ``scripts/`` is not an importable package, and several script
    filenames are not valid module names (``check-docker.py``), so gate
    scripts cannot be imported normally. The module name registered in
    ``sys.modules`` is derived from the filename with hyphens folded to
    underscores.

    Args:
        relative_path: Path to the script, relative to the repo root
            (e.g. ``"scripts/check_changelog.py"``)

    Returns:
        The executed module

    Raises:
        FileNotFoundError: The script does not exist — a renamed or moved
            script otherwise surfaces as an opaque spec error.
    """
    script = _REPO_ROOT / relative_path
    if not script.is_file():
        raise FileNotFoundError(f"script not found: {script}")

    name = script.stem.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, script)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {script}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
