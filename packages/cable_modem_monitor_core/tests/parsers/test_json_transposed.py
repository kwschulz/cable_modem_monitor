"""Tests for JSONTransposedParser.

Fixture-driven tests with synthesized JSON data. Each fixture contains
a JSON response, a JSONTransposedSection config, and expected channel
output. No modem-specific references.

Adding a test case = drop a JSON file in fixtures/json_transposed/valid/.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from solentlabs.cable_modem_monitor_core.models.parser_config.json_transposed import (
    JSONTransposedSection,
)
from solentlabs.cable_modem_monitor_core.parsers.formats.json_transposed import (
    JSONTransposedParser,
    transpose_indexed_rows,
)

from tests._helpers import collect_fixtures, load_fixture

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "json_transposed"
VALID_FIXTURES = collect_fixtures(FIXTURES_DIR / "valid")


@pytest.mark.parametrize(
    "fixture_path",
    VALID_FIXTURES,
    ids=[f.stem for f in VALID_FIXTURES],
)
def test_extraction(fixture_path: Path) -> None:
    """Parse JSON data and verify extracted channels match expected."""
    data = load_fixture(fixture_path)

    resource_key = data["_resource"]
    json_data = data.get("_json")
    resources: dict[str, Any] = {} if json_data is None else {resource_key: json_data}

    section_config = JSONTransposedSection.model_validate(data["_config"])
    parser = JSONTransposedParser(section_config)

    result = parser.parse(resources)
    expected = data["_expected"]

    assert result == expected, (
        f"Mismatch for {fixture_path.stem}:\n" f"  actual:   {result}\n" f"  expected: {expected}"
    )


# ---------------------------------------------------------------------------
# transpose_indexed_rows — public helper for parser.py PostProcessors
# ---------------------------------------------------------------------------

# (label, rows, kwargs, expected)
TRANSPOSE_CASES: list[tuple[str, list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]] = [
    (
        "two columns three named rows",
        [
            {"name": "CH", "index1": "0", "index2": "1"},
            {"name": "Power", "index1": "ON", "index2": "OFF"},
        ],
        {},
        [
            {"CH": "0", "Power": "ON"},
            {"CH": "1", "Power": "OFF"},
        ],
    ),
    (
        "string values are stripped",
        [
            {"name": "CH", "index1": " 0 "},
            {"name": "STATE", "index1": " OPERATE"},
        ],
        {},
        [{"CH": "0", "STATE": "OPERATE"}],
    ),
    (
        "non-string values pass through unchanged",
        [
            {"name": "CH", "index1": 7},
            {"name": "Power", "index1": True},
        ],
        {},
        [{"CH": 7, "Power": True}],
    ),
    (
        "empty rows yields empty list",
        [],
        {},
        [],
    ),
    (
        "no indexN columns yields empty list",
        [{"name": "CH", "value": "0"}],
        {},
        [],
    ),
    (
        "rows missing the name field are skipped",
        [
            {"name": "CH", "index1": "0"},
            {"index1": "ignore"},
            {"name": "", "index1": "skip"},
            {"name": 42, "index1": "non-string-name-skipped"},
        ],
        {},
        [{"CH": "0"}],
    ),
    (
        "custom name_field and index_prefix",
        [
            {"metric": "CH", "col1": "0", "col2": "1"},
            {"metric": "Freq", "col1": "10", "col2": "20"},
        ],
        {"name_field": "metric", "index_prefix": "col"},
        [
            {"CH": "0", "Freq": "10"},
            {"CH": "1", "Freq": "20"},
        ],
    ),
]


@pytest.mark.parametrize(
    ("label", "rows", "kwargs", "expected"),
    TRANSPOSE_CASES,
    ids=[c[0] for c in TRANSPOSE_CASES],
)
def test_transpose_indexed_rows(
    label: str,
    rows: list[dict[str, Any]],
    kwargs: dict[str, Any],
    expected: list[dict[str, Any]],
) -> None:
    """transpose_indexed_rows pivots rows into per-column dicts."""
    assert transpose_indexed_rows(rows, **kwargs) == expected


# (label, config_kwargs, error_match)
INVALID_SECTION_CASES: list[tuple[str, dict[str, Any], str]] = [
    (
        "unknown section-level field rejected",
        {
            "format": "json_transposed",
            "resource": "/api/upstream",
            "fields": [{"label": "CH", "field": "channel_id", "type": "integer"}],
            "unknown": "rejected",
        },
        "Extra inputs are not permitted",
    ),
    (
        "unknown row-mapping field rejected",
        {
            "format": "json_transposed",
            "resource": "/api/upstream",
            "fields": [
                {"label": "CH", "field": "channel_id", "type": "integer", "extra": "x"},
            ],
        },
        "Extra inputs are not permitted",
    ),
    (
        "missing resource rejected",
        {
            "format": "json_transposed",
            "fields": [{"label": "CH", "field": "channel_id", "type": "integer"}],
        },
        "Field required",
    ),
    (
        "missing fields list rejected",
        {"format": "json_transposed", "resource": "/api/upstream"},
        "Field required",
    ),
    (
        "wrong format literal rejected",
        {
            "format": "json",
            "resource": "/api/upstream",
            "fields": [{"label": "CH", "field": "channel_id", "type": "integer"}],
        },
        "Input should be 'json_transposed'",
    ),
    (
        "invalid mapping type rejected",
        {
            "format": "json_transposed",
            "resource": "/api/upstream",
            "fields": [{"label": "CH", "field": "channel_id", "type": "not_a_type"}],
        },
        "invalid field type",
    ),
]


@pytest.mark.parametrize(
    ("label", "config_kwargs", "error_match"),
    INVALID_SECTION_CASES,
    ids=[c[0] for c in INVALID_SECTION_CASES],
)
def test_section_validation_rejects(
    label: str,
    config_kwargs: dict[str, Any],
    error_match: str,
) -> None:
    """JSONTransposedSection validator rejects invalid configs."""
    with pytest.raises(ValidationError, match=error_match):
        JSONTransposedSection.model_validate(config_kwargs)
