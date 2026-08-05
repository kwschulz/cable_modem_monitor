#!/usr/bin/env python3
"""Intake pipeline accuracy tracker.

Tests the catalog_tools intake pipeline by treating each catalog HAR
as a fresh submission. For each modem:

1. Run HAR through validate_har -> analyze_har -> generate_config
2. Run generate_golden_file with the generated parser.yaml
3. Compare generated golden file against committed golden file
4. Compute field-level accuracy (matching / total committed fields)
5. Grade detected actions against the committed modem.yaml actions

Reports fleet-wide accuracy percentage and per-action grades so
onboarding capability and consistency can be tracked over time. This is
a report, not a gate: it computes accuracy fresh from the catalog every
run and, in CI, writes a GitHub step summary plus a timestamped JSON
scorecard artifact (the durable trend record). Correctness of each
modem's parse is gated independently by the golden replay tests.

Usage:
    python .../intake_pipeline_regression.py
    python .../intake_pipeline_regression.py --modem arris/sb8200
    python .../intake_pipeline_regression.py -v
    python .../intake_pipeline_regression.py --scorecard scorecard.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml
from solentlabs.cable_modem_monitor_catalog_tools.analysis.actions.grading import grade_actions
from solentlabs.cable_modem_monitor_catalog_tools.analysis.auth.grading import grade_auth
from solentlabs.cable_modem_monitor_catalog_tools.analysis.types import FleetPatterns
from solentlabs.cable_modem_monitor_catalog_tools.grading import GRADE_SEVERITY
from solentlabs.cable_modem_monitor_catalog_tools.regression import (
    ModemResult,
    build_scorecard,
    fleet_accuracy,
    result_status,
)
from solentlabs.cable_modem_monitor_core.test_harness import resolve_modem_config

CATALOG_ROOT = (
    Path(__file__).resolve().parents[2] / "cable_modem_monitor_catalog/solentlabs/cable_modem_monitor_catalog/modems"
)

CONFIG_FILES = ("modem.yaml", "parser.yaml", "parser.py")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_modems(
    filter_modem: str | None = None,
) -> list[tuple[str, Path, Path]]:
    """Discover catalog modems with HAR files."""
    results = []
    for mfr_dir in sorted(CATALOG_ROOT.iterdir()):
        if not mfr_dir.is_dir():
            continue
        for model_dir in sorted(mfr_dir.iterdir()):
            if not model_dir.is_dir():
                continue
            modem_id = f"{mfr_dir.name}/{model_dir.name}"
            if filter_modem and filter_modem != modem_id:
                continue
            test_data = model_dir / "test_data"
            if not test_data.exists():
                continue
            for har in sorted(test_data.glob("*.har")):
                results.append((modem_id, har, model_dir))
    return results


def _read_har_intake_info(har_path: Path) -> tuple[str, str]:
    """Return (intake_status, intake_reason) from log._solentlabs, or ('', '') if absent."""
    try:
        sl = json.loads(har_path.read_text()).get("log", {}).get("_solentlabs") or {}
        return sl.get("intake_status", ""), sl.get("intake_reason", "")
    except Exception:
        return "", ""


# ---------------------------------------------------------------------------
# File backup / restore
# ---------------------------------------------------------------------------


def _backup_files(modem_dir: Path) -> dict[str, bytes | None]:
    """Save original config files."""
    backups: dict[str, bytes | None] = {}
    for name in CONFIG_FILES:
        path = modem_dir / name
        backups[name] = path.read_bytes() if path.exists() else None
    return backups


def _restore_files(modem_dir: Path, backups: dict[str, bytes | None]) -> None:
    """Restore original config files from backup."""
    for name, content in backups.items():
        path = modem_dir / name
        if content is not None:
            path.write_bytes(content)
        elif path.exists():
            path.unlink()


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------


def _diff_channel_list(
    section: str,
    gen_list: list[Any],
    com_list: list[Any],
) -> list[str]:
    """Diff two lists of channel dicts."""
    diffs: list[str] = []
    if len(gen_list) != len(com_list):
        diffs.append(f"{section}: channel count {len(gen_list)} vs committed {len(com_list)}")
        return diffs
    for i, (g, c) in enumerate(zip(gen_list, com_list, strict=True)):
        if not isinstance(g, dict) or not isinstance(c, dict):
            continue
        for key in sorted(set(g.keys()) | set(c.keys())):
            if g.get(key) != c.get(key):
                diffs.append(f"{section}[{i}].{key}: {g.get(key)!r} vs {c.get(key)!r}")
    return diffs


def _diff_section(
    section: str,
    gen_val: Any,
    com_val: Any,
) -> list[str]:
    """Diff a single section (list, dict, or missing)."""
    if isinstance(gen_val, list) and isinstance(com_val, list):
        return _diff_channel_list(section, gen_val, com_val)

    if isinstance(gen_val, dict) and isinstance(com_val, dict):
        diffs: list[str] = []
        for key in sorted(set(gen_val.keys()) | set(com_val.keys())):
            if gen_val.get(key) != com_val.get(key):
                diffs.append(f"{section}.{key}: {gen_val.get(key)!r} vs {com_val.get(key)!r}")
        return diffs

    if gen_val is None and com_val is not None:
        return [f"{section}: missing in generated"]
    if gen_val is not None and com_val is None:
        return [f"{section}: extra in generated"]
    if gen_val != com_val:
        return [f"{section}: type mismatch"]
    return []


def _diff_golden_files(
    generated: dict[str, Any],
    committed: dict[str, Any],
) -> list[str]:
    """Compare generated golden file against committed golden file."""
    diffs: list[str] = []
    for section in ("downstream", "upstream", "system_info"):
        diffs.extend(_diff_section(section, generated.get(section), committed.get(section)))
    return diffs


# ---------------------------------------------------------------------------
# Field-level accuracy counting
# ---------------------------------------------------------------------------


def _count_fields(golden: dict[str, Any]) -> int:
    """Count total leaf fields in a golden file."""
    count = 0
    for section in ("downstream", "upstream"):
        for ch in golden.get(section, []):
            if isinstance(ch, dict):
                count += len(ch)
    si = golden.get("system_info")
    if isinstance(si, dict):
        count += len(si)
    return count


def _count_matching_fields(
    generated: dict[str, Any],
    committed: dict[str, Any],
) -> int:
    """Count fields in committed that are correctly reproduced in generated."""
    matching = 0
    for section in ("downstream", "upstream"):
        gen_list = generated.get(section, [])
        com_list = committed.get(section, [])
        for i, com_ch in enumerate(com_list):
            if not isinstance(com_ch, dict):
                continue
            gen_ch = gen_list[i] if i < len(gen_list) and isinstance(gen_list[i], dict) else {}
            for key, val in com_ch.items():
                if gen_ch.get(key) == val:
                    matching += 1
    gen_si = generated.get("system_info") or {}
    com_si = committed.get("system_info") or {}
    if isinstance(com_si, dict) and isinstance(gen_si, dict):
        for key, val in com_si.items():
            if gen_si.get(key) == val:
                matching += 1
    return matching


def _diff_config_files(
    generated_yaml: str | None,
    committed_path: Path,
    label: str,
) -> list[str]:
    """Compare generated config YAML against committed config."""
    if not committed_path.exists():
        return []

    committed = yaml.safe_load(committed_path.read_text())
    generated = yaml.safe_load(generated_yaml) if generated_yaml else None

    if generated is None and committed is not None:
        return [f"{label}: pipeline produced nothing, committed exists"]
    if generated is not None and committed is None:
        return [f"{label}: pipeline produced content, committed is empty"]
    if generated == committed:
        return []

    diffs: list[str] = []
    gen_keys = set(generated.keys()) if generated else set()
    com_keys = set(committed.keys()) if committed else set()
    missing = com_keys - gen_keys
    extra = gen_keys - com_keys
    if missing:
        diffs.append(f"{label}: missing keys {sorted(missing)}")
    if extra:
        diffs.append(f"{label}: extra keys {sorted(extra)}")
    for key in sorted(gen_keys & com_keys):
        if generated[key] != committed[key]:  # type: ignore[index]
            diffs.append(f"{label}.{key}: differs")
    return diffs


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------


def _run_validate(har_path: Path, result: ModemResult) -> bool:
    """Run validate_har. Returns True if passed."""
    from solentlabs.cable_modem_monitor_catalog_tools.validate_har import (
        validate_har,
    )

    val = validate_har(str(har_path))
    if not val.valid:
        result.stage_failed = "validate_har"
        result.error = "; ".join(val.issues)
        return False
    return True


def _run_analyze(
    har_path: Path,
    result: ModemResult,
    fleet: FleetPatterns | None = None,
) -> dict[str, Any] | None:
    """Run analyze_har. Returns analysis dict or None on failure."""
    from solentlabs.cable_modem_monitor_catalog_tools.analyze_har import (
        analyze_har,
    )

    analysis = analyze_har(str(har_path), fleet=fleet)
    if analysis.hard_stops:
        result.stage_failed = "analyze_har"
        result.error = "; ".join(analysis.hard_stops)
        return None
    return analysis.to_dict()


def _run_generate(
    analysis_data: dict[str, Any],
    modem_dir: Path,
    result: ModemResult,
    fleet: FleetPatterns | None = None,
) -> tuple[str | None, str | None]:
    """Run generate_config. Returns (modem_yaml, parser_yaml) or (None, None)."""
    from solentlabs.cable_modem_monitor_catalog_tools.enrich_metadata import (
        enrich_metadata,
    )
    from solentlabs.cable_modem_monitor_catalog_tools.generate_config import (
        generate_config,
    )

    user_meta = _extract_metadata(modem_dir)
    enriched = enrich_metadata(analysis_data, user_input=user_meta)
    config = generate_config(analysis_data, enriched.metadata, fleet=fleet)
    if config.validation and not config.validation.valid:
        # attribution and isps are catalog-completeness requirements — a freshly
        # generated config legitimately lacks them (both require human input).
        # Filter them out before deciding whether to block the regression run.
        pipeline_errors = [
            e for e in config.validation.errors if "requires attribution" not in e and "requires isps" not in e
        ]
        if pipeline_errors:
            result.stage_failed = "generate_config"
            result.error = "; ".join(pipeline_errors)
            return None, None
    return config.modem_yaml, config.parser_yaml


def _run_golden_comparison(
    har_path: Path,
    parser_yaml: str,
    modem_dir: Path,
    result: ModemResult,
) -> None:
    """Overwrite configs, generate golden file, compare, restore."""
    from solentlabs.cable_modem_monitor_catalog_tools.generate_golden_file import (
        generate_golden_file,
    )

    golden_result = generate_golden_file(str(har_path), parser_yaml)
    if golden_result.errors:
        result.stage_failed = "generate_golden_file"
        result.error = "; ".join(golden_result.errors)
        return

    result.channel_counts = golden_result.channel_counts

    stem = har_path.stem
    expected_path = har_path.parent / f"{stem}.expected.json"

    if not expected_path.exists():
        if golden_result.golden_file:
            result.golden_diffs = ["no committed golden file to compare"]
        return

    committed = json.loads(expected_path.read_text())
    generated = golden_result.golden_file or {}
    result.total_fields = _count_fields(committed)
    result.matching_fields = _count_matching_fields(generated, committed)
    result.golden_diffs = _diff_golden_files(generated, committed)


def _extract_metadata(modem_dir: Path) -> dict[str, Any]:
    """Extract identity metadata from committed modem.yaml."""
    modem_yaml = modem_dir / "modem.yaml"
    if not modem_yaml.exists():
        return {}
    with open(modem_yaml) as f:
        cfg = yaml.safe_load(f) or {}
    return {
        "manufacturer": cfg.get("manufacturer", ""),
        "model": cfg.get("model", ""),
    }


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


def _run_pipeline(
    har_path: Path,
    modem_dir: Path,
    committed_path: Path | None,
    result: ModemResult,
    fleet: FleetPatterns | None,
) -> None:
    """Run the pipeline stages, stopping at the first one that fails."""
    if not _run_validate(har_path, result):
        return

    analysis_data = _run_analyze(har_path, result, fleet=fleet)
    if analysis_data is None:
        return

    committed = _grade_actions_stage(result, analysis_data, committed_path)

    modem_yaml, parser_yaml = _run_generate(analysis_data, modem_dir, result, fleet=fleet)
    if modem_yaml is None:
        return

    _grade_auth_stage(result, modem_yaml, committed)

    # Config diffs (informational)
    if committed_path is not None:
        result.config_diffs.extend(_diff_config_files(modem_yaml, committed_path, committed_path.name))
    result.config_diffs.extend(_diff_config_files(parser_yaml, modem_dir / "parser.yaml", "parser.yaml"))

    # Golden file comparison
    if parser_yaml:
        _run_golden_comparison(har_path, parser_yaml, modem_dir, result)
    else:
        result.stage_failed = "generate_golden_file"
        result.error = "no parser.yaml generated"


def run_modem(
    modem_id: str,
    har_path: Path,
    modem_dir: Path,
    verbose: bool = False,
    fleet: FleetPatterns | None = None,
) -> ModemResult:
    """Run the full pipeline regression for one modem HAR."""
    result = ModemResult(modem=modem_id, har_file=har_path.name)

    # A modem directory may hold several HARs, each capturing a different
    # auth variant with its own modem-<variant>.yaml. Grading every HAR
    # against the base modem.yaml compares a capture to a config that does
    # not describe it. Core's test harness already owns this pairing rule.
    committed_path = resolve_modem_config(har_path.stem, modem_dir)

    try:
        _run_pipeline(har_path, modem_dir, committed_path, result, fleet)
    except Exception as e:
        result.stage_failed = result.stage_failed or "unknown"
        result.error = str(e)

    # Pipeline failures: count committed golden file fields for accuracy.
    # The stages live in a helper so an early return there cannot skip this.
    # It used to sit after a try block the stages returned out of directly,
    # so a HAR failing at validate_har or analyze_har contributed no
    # denominator at all and left the accuracy pool instead of scoring zero,
    # which quietly inflated the fleet percentage.
    if result.total_fields == 0 and result.stage_failed:
        stem = har_path.stem
        expected_path = har_path.parent / f"{stem}.expected.json"
        if expected_path.exists():
            result.total_fields = _count_fields(json.loads(expected_path.read_text()))

    if verbose:
        _print_result(result)

    return result


def _grade_actions_stage(
    result: ModemResult,
    analysis_data: dict[str, Any],
    committed_path: Path | None,
) -> dict[str, Any]:
    """Grade detected actions against the committed config; returns it.

    Runs right after analysis so actions are graded even when a later
    stage fails.
    """
    if committed_path is None or not committed_path.exists():
        return {}
    committed: dict[str, Any] = yaml.safe_load(committed_path.read_text()) or {}
    result.grades["actions"] = grade_actions(analysis_data.get("actions"), committed.get("actions"))
    return committed


def _grade_auth_stage(
    result: ModemResult,
    modem_yaml: str,
    committed: dict[str, Any],
) -> None:
    """Grade the generated auth block against the committed config.

    Unlike actions (graded from analysis output), auth is graded from
    the generated config, so it requires generation to succeed.
    """
    if not committed:
        return
    generated_auth = (yaml.safe_load(modem_yaml) or {}).get("auth")
    result.grades["auth"] = grade_auth(generated_auth, committed.get("auth"))


def _print_result(result: ModemResult) -> None:
    """Print verbose output for a single modem result."""
    pct = f"{result.accuracy_pct:5.1f}%" if result.total_fields else "  n/a"

    if result.stage_failed:
        print(f"    FAIL  {pct}  {result.stage_failed}: {result.error}")
        _print_result_actions(result)
        return

    ds = result.channel_counts.get("downstream", 0)
    us = result.channel_counts.get("upstream", 0)
    if result.golden_diffs:
        print(f"    DRIFT {pct}  ds={ds} us={us}: {len(result.golden_diffs)} diffs")
        for d in result.golden_diffs[:5]:
            print(f"      {d}")
        if len(result.golden_diffs) > 5:
            print(f"      ... and {len(result.golden_diffs) - 5} more")
    else:
        print(f"    OK    {pct}  ds={ds} us={us}: golden file matches")

    if result.config_diffs:
        print(f"    CONFIG diffs: {len(result.config_diffs)}")
        for d in result.config_diffs[:3]:
            print(f"      {d}")
    _print_result_actions(result)


def _print_result_actions(result: ModemResult) -> None:
    """Print per-item grades for one modem result, all dimensions."""
    for dim, grades in sorted(result.grades.items()):
        if not grades:
            continue
        summary = "  ".join(f"{item}={grade.status}" for item, grade in sorted(grades.items()))
        print(f"    {dim.upper()} {summary}")
        for item, grade in sorted(grades.items()):
            if grade.status != "match" and grade.detail:
                print(f"      {item}: {grade.detail}")


def _print_auth_audit(catalog_root: Path) -> None:
    """Run auth fixture audit and print any issues found."""
    from solentlabs.cable_modem_monitor_catalog_tools.fleet_scanner import audit_fleet_auth

    issues = audit_fleet_auth(catalog_root)
    if not issues:
        return
    print("AUTH FIXTURE ISSUES (report only — verify against hardware):")
    for issue in issues:
        print(f"  {issue.modem} ({issue.har}): {issue.issue}")
    print()


def _print_incomplete_section(incomplete: list[tuple[str, Path, str, str]]) -> None:
    """Print the incomplete HARs block."""
    if not incomplete:
        return
    print("INCOMPLETE HARS (excluded from accuracy — replace with clean capture):")
    for modem_id, har_path, status, reason in incomplete:
        suffix = f" — {reason}" if reason else ""
        print(f"  {modem_id} ({har_path.name}): {status}{suffix}")
    print()


def _print_failures_section(failed_stage: list[ModemResult]) -> None:
    """Print the pipeline failures block."""
    if not failed_stage:
        return
    print("PIPELINE FAILURES:")
    for r in failed_stage:
        pct = f"  ({r.accuracy_pct:5.1f}%)" if r.total_fields else ""
        print(f"  {r.modem} ({r.har_file}):{pct}  {r.stage_failed} — {r.error}")
    print()


def _print_drift_section(drifted: list[ModemResult]) -> None:
    """Print the golden file drift block."""
    if not drifted:
        return
    print("GOLDEN FILE DRIFT:")
    for r in sorted(drifted, key=lambda r: r.accuracy_pct):
        fld = f"{r.matching_fields}/{r.total_fields}" if r.total_fields else "n/a"
        print(f"  {r.modem} ({r.har_file}):  {r.accuracy_pct:5.1f}% ({fld} fields)")
        for d in r.golden_diffs[:3]:
            print(f"    {d}")
        if len(r.golden_diffs) > 3:
            print(f"    ... and {len(r.golden_diffs) - 3} more")
    print()


def _print_passed_section(passed: list[ModemResult]) -> None:
    """Print the clean modems block."""
    if not passed:
        return
    print(f"CLEAN ({len(passed)}):")
    for r in passed:
        ds = r.channel_counts.get("downstream", 0)
        us = r.channel_counts.get("upstream", 0)
        print(f"  {r.modem}: ds={ds} us={us}")
    print()


def _print_summary(
    results: list[ModemResult],
    incomplete: list[tuple[str, Path, str, str]] | None = None,
) -> None:
    """Print the final summary with accuracy metrics."""
    passed = [r for r in results if r.passed]
    failed_stage = [r for r in results if r.stage_failed]
    drifted = [r for r in results if r.golden_diffs and not r.stage_failed]

    matching_fields, total_fields, fleet_pct = fleet_accuracy(results)
    pipeline_passed = len(results) - len(failed_stage)
    incomplete = incomplete or []

    print(f"\n{'=' * 60}")
    print(f"FLEET ACCURACY: {fleet_pct:.1f}%  ({matching_fields} / {total_fields} fields)")
    print(
        f"  Pipeline pass rate: {pipeline_passed}/{len(results)} HARs"
        f"  |  Clean: {len(passed)}"
        f"  Drift: {len(drifted)}"
        f"  Failed: {len(failed_stage)}" + (f"  Skipped: {len(incomplete)}" if incomplete else "")
    )
    print(f"{'=' * 60}\n")

    _print_incomplete_section(incomplete)
    _print_failures_section(failed_stage)
    _print_drift_section(drifted)
    _print_grades_sections(results)
    _print_passed_section(passed)


def _print_grades_sections(results: list[ModemResult]) -> None:
    """Fleet-wide capability grading per dimension: tally plus every non-match."""
    for dim in sorted({dim for r in results for dim in r.grades}):
        graded = [(r, item, grade) for r in results for item, grade in sorted(r.grades.get(dim, {}).items())]
        if not graded:
            continue
        tally = Counter(grade.status for _, _, grade in graded)
        counts = "  ".join(f"{status}: {tally.get(status, 0)}" for status in GRADE_SEVERITY)
        print(f"{dim.upper()} — pipeline-detected vs committed ({len(graded)} graded):")
        print(f"  {counts}")
        for r, item, grade in graded:
            if grade.status != "match":
                detail = f" — {grade.detail}" if grade.detail else ""
                print(f"  {r.modem} ({r.har_file}) {item}: {grade.status}{detail}")
        print()


# ---------------------------------------------------------------------------
# Scorecard and CI summary
# ---------------------------------------------------------------------------


def _write_scorecard(path: Path, results: list[ModemResult]) -> None:
    """Write JSON scorecard to disk."""
    scorecard = build_scorecard(results)
    path.write_text(json.dumps(scorecard, indent=2) + "\n")
    print(f"Scorecard written to {path}")


def _write_step_summary(results: list[ModemResult]) -> None:
    """Write GitHub Actions step summary if running in CI."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    matching, total, fleet_pct = fleet_accuracy(results)
    failed_count = sum(1 for r in results if r.stage_failed)
    clean_count = sum(1 for r in results if r.passed)
    drift_count = sum(1 for r in results if r.golden_diffs and not r.stage_failed)

    lines = [
        f"## Intake Pipeline Accuracy: **{fleet_pct:.1f}%**",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Fleet accuracy | {fleet_pct:.1f}% ({matching}/{total} fields) |",
        f"| Pipeline pass rate | {len(results) - failed_count}/{len(results)} HARs |",
        f"| Clean | {clean_count} |",
        f"| Drift | {drift_count} |",
        f"| Pipeline failures | {failed_count} |",
        "",
        "<details>",
        "<summary>Per-modem breakdown</summary>",
        "",
        "| Modem | HAR | Status | Accuracy | Fields |",
        "|-------|-----|--------|----------|--------|",
    ]
    for r in sorted(results, key=lambda r: r.accuracy_pct):
        status = result_status(r)
        fld = f"{r.matching_fields}/{r.total_fields}" if r.total_fields else "-"
        pct = f"{r.accuracy_pct:.1f}%" if r.total_fields else "-"
        lines.append(f"| {r.modem} | {r.har_file} | {status} | {pct} | {fld} |")
    lines += ["", "</details>", ""]

    with open(summary_path, "a") as f:
        f.write("\n".join(lines))


def main() -> None:
    """Run the regression sweep."""
    parser = argparse.ArgumentParser(description="Intake pipeline regression — accuracy tracking")
    parser.add_argument("--modem", help="Run only this modem (e.g., arris/sb8200)")
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show per-modem details",
    )
    parser.add_argument(
        "--scorecard",
        type=Path,
        metavar="PATH",
        help="Write JSON scorecard for trend tracking",
    )
    args = parser.parse_args()

    modems = discover_modems(args.modem)
    if not modems:
        print("No modems found.")
        sys.exit(1)

    # Partition: HARs flagged as incomplete skip the pipeline entirely.
    incomplete: list[tuple[str, Path, str, str]] = []
    runnable: list[tuple[str, Path, Path]] = []
    for modem_id, har_path, modem_dir in modems:
        status, reason = _read_har_intake_info(har_path)
        if status:
            incomplete.append((modem_id, har_path, status, reason))
        else:
            runnable.append((modem_id, har_path, modem_dir))

    # Scan fleet patterns once — feeds into analyze_har and generate_config
    from solentlabs.cable_modem_monitor_catalog_tools.fleet_scanner import scan_fleet

    fleet = scan_fleet(CATALOG_ROOT)
    print(
        f"Fleet patterns: {len(fleet.selector_directions)} selectors, "
        f"{len(fleet.system_info_labels)} labels, "
        f"{len(fleet.aggregate_fields)} aggregates"
    )

    print(f"Running pipeline regression on {len(runnable)} HAR files...")
    if incomplete:
        print(f"  ({len(incomplete)} HAR(s) skipped — incomplete capture)\n")
    else:
        print()

    results: list[ModemResult] = []
    for modem_id, har_path, modem_dir in runnable:
        print(f"  {modem_id} ({har_path.name})")
        r = run_modem(modem_id, har_path, modem_dir, verbose=args.verbose, fleet=fleet)
        results.append(r)

    _print_summary(results, incomplete=incomplete)
    _print_auth_audit(CATALOG_ROOT)
    _write_step_summary(results)

    if args.scorecard:
        _write_scorecard(args.scorecard, results)


if __name__ == "__main__":
    main()
