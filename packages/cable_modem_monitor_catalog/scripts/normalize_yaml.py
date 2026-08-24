#!/usr/bin/env python3
"""Normalize YAML key ordering across the catalog fleet.

Reorders keys in modem.yaml and parser.yaml files to match the
canonical ordering derived from the Pydantic model field definitions.
Uses ruamel.yaml for round-trip fidelity: comments, quoting style,
and scalar style (``|`` blocks) are preserved.

Key-ordering logic is imported from Catalog Tools' generate_config
module (which derives top-level orders from Core's ``ModemConfig`` /
``ParserConfig`` field definitions, plus hardcoded presentation
choices for nested contexts that span discriminated unions).

This script is Catalog hygiene tooling — operates on data in this
package — but it requires Catalog Tools to be installed in the dev
environment because that's where the ordering logic lives.

Run from the repo root::

    .venv/bin/python packages/cable_modem_monitor_catalog/scripts/normalize_yaml.py

Dry-run (show which files would change)::

    .venv/bin/python packages/cable_modem_monitor_catalog/scripts/normalize_yaml.py --dry-run
"""

from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from solentlabs.cable_modem_monitor_catalog_tools.generate_config.validation import (
    CONTEXT_ORDERS,
    MODEM_KEY_ORDER,
    PARSER_KEY_ORDER,
    detect_context,
)

# -----------------------------------------------------------------------
# Reordering with comment preservation
# -----------------------------------------------------------------------


def _reorder_commented_map(
    cm: CommentedMap,
    key_order: list[str] | None = None,
) -> CommentedMap:
    """Reorder a CommentedMap while preserving comments.

    Args:
        cm: The map to reorder.
        key_order: Explicit top-level order, or None for context detection.
    """
    if key_order:
        leading = key_order
    else:
        ctx = detect_context(cm)
        leading = CONTEXT_ORDERS.get(ctx, []) if ctx else []

    ordered_keys: list[str] = []
    for key in leading:
        if key in cm:
            ordered_keys.append(key)
    for key in sorted(cm):
        if key not in ordered_keys:
            ordered_keys.append(key)

    new_cm = CommentedMap()

    # Preserve top-of-map comments
    if hasattr(cm, "ca") and cm.ca and cm.ca.comment:
        new_cm.ca.comment = cm.ca.comment

    for key in ordered_keys:
        new_cm[key] = _reorder_value(cm[key])
        # Preserve per-key comments (end-of-line, before-key, etc.)
        if hasattr(cm, "ca") and key in cm.ca.items:
            new_cm.ca.items[key] = cm.ca.items[key]

    return new_cm


def _reorder_value(v: object) -> object:
    """Recursively reorder nested structures."""
    if isinstance(v, CommentedMap):
        return _reorder_commented_map(v)
    if isinstance(v, dict):
        return _reorder_commented_map(CommentedMap(v))
    if isinstance(v, list):
        return [_reorder_value(item) for item in v]
    return v


# -----------------------------------------------------------------------
# File processing
# -----------------------------------------------------------------------


def normalize_file(
    path: Path,
    key_order: list[str],
    *,
    dry_run: bool = False,
) -> bool:
    """Normalize a single YAML file. Returns True if changed."""
    yml = YAML()
    yml.preserve_quotes = True

    original = path.read_text()
    data = yml.load(original)
    if data is None:
        return False

    normalized = _reorder_commented_map(data, key_order)

    buf = StringIO()
    yml.dump(normalized, buf)
    output = buf.getvalue()

    if original == output:
        return False

    if not dry_run:
        path.write_text(output)
    return True


def main() -> int:
    """Normalize all modem.yaml and parser.yaml files in the catalog."""
    dry_run = "--dry-run" in sys.argv

    root = Path("packages/cable_modem_monitor_catalog/solentlabs/cable_modem_monitor_catalog/modems")
    if not root.exists():
        print("Error: catalog modems directory not found")
        return 1

    prefix = "[dry-run] " if dry_run else ""

    modem_changed = 0
    modem_files = sorted(root.rglob("modem*.yaml"))
    for f in modem_files:
        if normalize_file(f, MODEM_KEY_ORDER, dry_run=dry_run):
            modem_changed += 1
            print(f"  {prefix}modem: {f.relative_to(root)}")

    parser_changed = 0
    parser_files = sorted(root.rglob("parser.yaml"))
    for f in parser_files:
        if normalize_file(f, PARSER_KEY_ORDER, dry_run=dry_run):
            parser_changed += 1
            print(f"  {prefix}parser: {f.relative_to(root)}")

    print(f"\nmodem: {modem_changed}/{len(modem_files)} {'would change' if dry_run else 'changed'}")
    print(f"parser: {parser_changed}/{len(parser_files)} {'would change' if dry_run else 'changed'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
