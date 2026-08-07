"""Tests for Phase 6: uptime format inference.

A format is only emitted when it demonstrably parses the observed value,
so these cases assert against Core's own converter rather than against
the shape of the format string.
"""

from __future__ import annotations

import pytest
from solentlabs.cable_modem_monitor_catalog_tools.analysis.mapping.uptime_format import (
    detect_uptime_format,
)
from solentlabs.cable_modem_monitor_core.parsers.type_conversion import convert_value

# The fleet's committed formats, most specific first, as scan_fleet orders them.
CANDIDATES = [
    "D: {days} H: {hours} M: {minutes} S: {seconds}",
    "{days} day(s) {hours}h:{minutes}m:{seconds}s",
    "{days} days {hours}h:{minutes}m:{seconds}s",
    "{days}day(s){hours}h:{minutes}m:{seconds}s",
    "[{days} days ]{hours}:{minutes}:{seconds}",
    "{days} days {hours}:{minutes}:{seconds}",
    "{days} d: {hours} h: {minutes} m",
    "{hours}h:{minutes}m:{seconds}s",
    "{hours}:{minutes}:{seconds}",
    "seconds",
]

# fmt: off
FORMAT_CASES = [
    # (raw value,                        expected format,                                desc)
    (574,                                "seconds",                                      "integer seconds"),
    ("1471890",                          "seconds",                                      "numeric string seconds"),
    ("17 days 00h:51m:30s",              "{days} days {hours}h:{minutes}m:{seconds}s",   "days with unit letters"),
    ("D: 39 H: 06 M: 24 S: 26",          "D: {days} H: {hours} M: {minutes} S: {seconds}", "labelled segments"),
    ("10 days 01:41:16",                 "[{days} days ]{hours}:{minutes}:{seconds}",    "optional days present"),
]
# fmt: on


@pytest.mark.parametrize("raw,expected,desc", FORMAT_CASES, ids=[c[2] for c in FORMAT_CASES])
def test_detects_format(raw: object, expected: str, desc: str) -> None:
    """The chosen format is the fleet candidate that parses the observed value."""
    assert detect_uptime_format(raw, CANDIDATES) == expected, desc


@pytest.mark.parametrize("raw,expected,desc", FORMAT_CASES, ids=[c[2] for c in FORMAT_CASES])
def test_detected_format_actually_parses(raw: object, expected: str, desc: str) -> None:
    """Whatever is chosen must produce a value in Core, not just look plausible."""
    fmt = detect_uptime_format(raw, CANDIDATES)
    assert convert_value(str(raw), "uptime", input_format=fmt) is not None, desc


# fmt: off
NO_FORMAT_CASES = [
    # (raw value,           desc)
    ("",                    "empty value"),
    ("   ",                 "whitespace only"),
    ("not an uptime",       "unparseable text"),
]
# fmt: on


@pytest.mark.parametrize("raw,desc", NO_FORMAT_CASES, ids=[c[1] for c in NO_FORMAT_CASES])
def test_returns_empty_when_nothing_parses(raw: str, desc: str) -> None:
    """No candidate parsing means no format, so the caller can fall back to string."""
    assert detect_uptime_format(raw, CANDIDATES) == "", desc


def test_no_candidates_still_detects_raw_seconds() -> None:
    """The seconds preset is Core's, not the fleet's, so it needs no candidate list."""
    assert detect_uptime_format(574, []) == "seconds"
