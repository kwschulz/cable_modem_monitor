#!/usr/bin/env python3
"""Catalog field sweep — registry fields the catalog captured and never mapped.

Walks every committed catalog entry that has a HAR, reads the capture
for keys that resolve to a registry field, and reports the ones the
entry's own parser.yaml and golden file never populate.

A report, not a gate. Each hit is a candidate a human decides on: the
field may be genuinely absent from the modem's data model, redundant
with one already mapped, or a real mapping gap. Nothing here is wired
automatically.

Scope and blind spots are in
``analysis/field_sweep.py`` and INTAKE_PIPELINE.md § Catalog Field Sweep.

Usage:
    python .../catalog_field_sweep.py
    python .../catalog_field_sweep.py --modem arris/sb8200
    python .../catalog_field_sweep.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml
from solentlabs.cable_modem_monitor_catalog_tools.analysis.field_sweep import FieldGap, sweep_entry
from solentlabs.cable_modem_monitor_core.test_harness import ModemTestCase, discover_modem_tests

CATALOG_MODEMS = (
    Path(__file__).resolve().parents[2] / "cable_modem_monitor_catalog/solentlabs/cable_modem_monitor_catalog/modems"
)


def _load_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON artifact, or None when it is absent or unreadable."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def sweep_case(case: ModemTestCase) -> list[FieldGap]:
    """Sweep one discovered test case against its committed config."""
    if case.parser_config_path is None:
        return []
    parser_config = yaml.safe_load(case.parser_config_path.read_text(encoding="utf-8")) or {}
    har = _load_json(case.har_path)
    if har is None:
        return []
    entries = har.get("log", {}).get("entries", [])
    return sweep_entry(parser_config, _load_json(case.golden_path), entries)


def _model_of(case_name: str) -> str:
    """Reduce a test-case name to the entry that owns the parser.yaml."""
    return "/".join(case_name.split("/")[:2])


def _print_report(results: list[tuple[str, list[FieldGap]]]) -> None:
    """Print per-entry findings and the fleet-wide field ranking."""
    per_field_entries: Counter[str] = Counter()
    per_field_models: dict[str, set[str]] = {}
    total = 0

    for name, gaps in results:
        if not gaps:
            continue
        print(f"\n{name}")
        for gap in gaps:
            keys = ", ".join(gap.capture_keys)
            print(f"  {gap.section:<11} {gap.field:<22} captured as: {keys}")
            per_field_entries[gap.field] += 1
            per_field_models.setdefault(gap.field, set()).add(_model_of(name))
            total += 1

    models = {_model_of(name) for name, _ in results}
    affected = {_model_of(name) for name, gaps in results if gaps}
    print(f"\n{total} findings across {len(affected)} of {len(models)} swept entries ({len(results)} captures)")

    if not per_field_models:
        return
    # Ranked by entries affected, not captures: one parser.yaml is one
    # decision, and a model with five HAR variants would otherwise
    # outrank a field missing on five different modems.
    print("\nMost-missed registry fields:")
    for field, owners in sorted(per_field_models.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        label = "entry " if len(owners) == 1 else "entries"
        print(f"  {len(owners):>3} {label} ({per_field_entries[field]} captures)  {field}: {', '.join(sorted(owners))}")


def main() -> int:
    """Run the sweep over the committed catalog."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modem", help="Limit to one entry, e.g. arris/sb8200")
    parser.add_argument("--json", action="store_true", help="Emit findings as JSON")
    args = parser.parse_args()

    cases = discover_modem_tests(CATALOG_MODEMS)
    if args.modem:
        cases = [c for c in cases if c.name.startswith(f"{args.modem}/")]
        if not cases:
            print(f"No catalog entry matches {args.modem}", file=sys.stderr)
            return 1

    results = [(case.name, sweep_case(case)) for case in cases]

    if args.json:
        payload = {name: [gap.to_dict() for gap in gaps] for name, gaps in results if gaps}
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        _print_report(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
