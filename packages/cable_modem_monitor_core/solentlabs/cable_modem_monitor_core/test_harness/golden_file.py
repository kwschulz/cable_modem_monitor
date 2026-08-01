"""Golden file comparison with structured diffs.

Compares ModemData pipeline output against a validated golden file
(``tests/modem.expected.json``). The golden file is the regression
checkpoint — first run: user validates output is correct; every
subsequent run: pipeline output must match that validated snapshot.

See catalog_tools' ONBOARDING_SPEC.md § Golden File Comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FieldDiff:
    """A single field-level difference between expected and actual."""

    path: str
    expected: Any
    actual: Any
    hint: str = ""


@dataclass
class ComparisonResult:
    """Result of comparing pipeline output against a golden file.

    Attributes:
        passed: True if output matches expected exactly.
        diffs: List of field-level differences (empty when passed).
        diff_text: Human-readable summary of all differences.
    """

    passed: bool
    diffs: list[FieldDiff] = field(default_factory=list)
    diff_text: str = ""


def compare_golden_file(
    actual: dict[str, Any],
    expected: dict[str, Any],
) -> ComparisonResult:
    """Compare pipeline output against a golden file.

    Deep equality on the full ModemData dict. Downstream and upstream
    channel lists are order-sensitive. System info is compared as a
    flat dict.

    Args:
        actual: ModemData dict from the pipeline.
        expected: ModemData dict from the golden file.

    Returns:
        ComparisonResult with pass/fail and structured diffs.
    """
    diffs: list[FieldDiff] = []

    for section_name in ("downstream", "upstream"):
        _compare_channel_section(
            section_name,
            actual.get(section_name, []),
            expected.get(section_name, []),
            diffs,
        )

    _compare_system_info(
        actual.get("system_info", {}),
        expected.get("system_info", {}),
        diffs,
    )

    passed = len(diffs) == 0
    diff_text = _format_diffs(diffs) if diffs else ""

    return ComparisonResult(passed=passed, diffs=diffs, diff_text=diff_text)


def _compare_channel_section(
    section_name: str,
    actual: list[dict[str, Any]],
    expected: list[dict[str, Any]],
    diffs: list[FieldDiff],
) -> None:
    """Compare channel lists (order-sensitive)."""
    if len(actual) != len(expected):
        diff = FieldDiff(
            path=section_name,
            expected=f"{len(expected)} channels",
            actual=f"{len(actual)} channels",
        )
        # Identify missing or extra channels by channel_id
        expected_ids: set[int] = {ch["channel_id"] for ch in expected if "channel_id" in ch}
        actual_ids: set[int] = {ch["channel_id"] for ch in actual if "channel_id" in ch}
        missing = expected_ids - actual_ids
        extra = actual_ids - expected_ids
        hints = []
        if missing:
            hints.append(f"missing channel_ids: {sorted(missing)}")
        if extra:
            hints.append(f"extra channel_ids: {sorted(extra)}")
        if hints:
            diff.hint = "; ".join(hints)
        diffs.append(diff)

    # Compare channels up to the shorter list
    for i, (act_ch, exp_ch) in enumerate(zip(actual, expected, strict=False)):
        _compare_channel(f"{section_name}[{i}]", act_ch, exp_ch, diffs)


def _compare_channel(
    prefix: str,
    actual: dict[str, Any],
    expected: dict[str, Any],
    diffs: list[FieldDiff],
) -> None:
    """Compare a single channel dict."""
    all_keys = sorted(set(actual) | set(expected))
    for key in all_keys:
        path = f"{prefix}.{key}"
        if key not in expected:
            diffs.append(FieldDiff(path=path, expected="<absent>", actual=actual[key]))
        elif key not in actual:
            diffs.append(FieldDiff(path=path, expected=expected[key], actual="<absent>"))
        elif actual[key] != expected[key]:
            diff = FieldDiff(path=path, expected=expected[key], actual=actual[key])
            diff.hint = _hint_for_field(key, actual[key], expected[key])
            diffs.append(diff)


def _compare_system_info(
    actual: dict[str, str],
    expected: dict[str, str],
    diffs: list[FieldDiff],
) -> None:
    """Compare system_info as a flat dict."""
    if not actual and not expected:
        return

    if not expected and actual:
        diffs.append(
            FieldDiff(
                path="system_info",
                expected="<absent>",
                actual=f"{len(actual)} fields",
            )
        )
        return

    if expected and not actual:
        diffs.append(
            FieldDiff(
                path="system_info",
                expected=f"{len(expected)} fields",
                actual="<absent>",
            )
        )
        return

    all_keys = sorted(set(actual) | set(expected))
    for key in all_keys:
        path = f"system_info.{key}"
        if key not in expected:
            diffs.append(FieldDiff(path=path, expected="<absent>", actual=actual[key]))
        elif key not in actual:
            diffs.append(FieldDiff(path=path, expected=expected[key], actual="<absent>"))
        elif actual[key] != expected[key]:
            diffs.append(FieldDiff(path=path, expected=expected[key], actual=actual[key]))


def _hint_for_field(field: str, actual: Any, expected: Any) -> str:
    """Generate a diagnostic hint for a field mismatch."""
    if field == "frequency" and isinstance(actual, int | float) and isinstance(expected, int | float):
        ratio = actual / expected if expected != 0 else 0
        if abs(ratio - 1e-6) < 1e-3:
            return "likely missing Hz normalization (value is in MHz)"
        if abs(ratio - 1e6) < 1e3:
            return "likely double Hz normalization"
    if field == "channel_id":
        if isinstance(actual, str) and isinstance(expected, int):
            return "channel_id is string, expected int"
        if isinstance(actual, int) and isinstance(expected, str):
            return "channel_id is int, expected string"
    return ""


def _format_diffs(diffs: list[FieldDiff]) -> str:
    """Format diffs as human-readable text."""
    lines: list[str] = []
    for d in diffs:
        lines.append(f"  {d.path}:")
        lines.append(f"    expected: {d.expected}")
        lines.append(f"    actual:   {d.actual}")
        if d.hint:
            lines.append(f"    ({d.hint})")
    return "\n".join(lines)
