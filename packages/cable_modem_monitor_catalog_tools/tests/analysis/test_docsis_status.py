"""Tests for Phase 6: docsis_status canonical mapping.

The asymmetry under test is the point: mapping a success spelling is
useful, mapping anything else would report a healthy modem during a
fault. See SYSTEM_INFO_SPEC Canonical values, Diagnostic Pass-Through.
"""

from __future__ import annotations

import pytest
from solentlabs.cable_modem_monitor_catalog_tools.analysis.mapping.docsis_status import (
    detect_docsis_status_map,
)

# Lowercased spellings, as scan_fleet harvests them from committed configs.
SUCCESS_VALUES = frozenset(
    {
        "allowed",
        "connected",
        "docsis_cm_stat_params_operational",
        "operational",
        "success",
        "telephony-reg complete",
    }
)

# fmt: off
MAPPED_CASES = [
    # (raw value,                 expected map,                                 desc)
    ("operational",               {"operational": "Operational"},               "lowercase success"),
    ("OPERATIONAL",               {"OPERATIONAL": "Operational"},               "uppercase success"),
    ("Allowed",                   {"Allowed": "Operational"},                   "vendor word"),
    ("Connected",                 {"Connected": "Operational"},                 "another vendor word"),
    ("Telephony-Reg Complete",    {"Telephony-Reg Complete": "Operational"},    "multi-word spelling"),
    ("  Allowed  ",               {"Allowed": "Operational"},                   "surrounding whitespace"),
]
# fmt: on


@pytest.mark.parametrize("raw,expected,desc", MAPPED_CASES, ids=[c[2] for c in MAPPED_CASES])
def test_maps_success_spellings(raw: str, expected: dict[str, str], desc: str) -> None:
    """A known success spelling normalizes, keeping the observed casing as the key."""
    assert detect_docsis_status_map(raw, SUCCESS_VALUES) == expected, desc


# fmt: off
UNMAPPED_CASES = [
    # (raw value,          desc)
    ("Ranging",            "in-progress state passes through"),
    ("Scanning",           "in-progress state passes through"),
    ("Access Denied",      "error state passes through"),
    ("Not Synchronized",   "explicit fault passes through"),
    ("1",                  "numeric not in the harvested vocabulary"),
    ("",                   "empty value"),
    ("   ",                "whitespace only"),
    ("Operational",        "already canonical needs no map"),
]
# fmt: on


@pytest.mark.parametrize("raw,desc", UNMAPPED_CASES, ids=[c[1] for c in UNMAPPED_CASES])
def test_leaves_everything_else_alone(raw: str, desc: str) -> None:
    """Anything not a known success spelling reaches the status sensor unchanged."""
    assert detect_docsis_status_map(raw, SUCCESS_VALUES) == {}, desc


def test_no_fleet_vocabulary_maps_nothing() -> None:
    """Without harvested spellings there is no basis to normalize, so nothing is."""
    assert detect_docsis_status_map("Allowed", frozenset()) == {}
