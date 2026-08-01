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

import json
from pathlib import Path
from typing import Any


def collect_fixtures(directory: Path) -> list[Path]:
    """Collect all JSON fixture files from a directory, sorted by name."""
    return sorted(directory.glob("*.json"))


def load_fixture(path: Path) -> dict[str, Any]:
    """Load a JSON fixture file and return the parsed dict."""
    data: dict[str, Any] = json.loads(path.read_text())
    return data
