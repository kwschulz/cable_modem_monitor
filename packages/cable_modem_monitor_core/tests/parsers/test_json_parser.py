"""Tests for JSONParser.

Fixture-driven tests with synthesized JSON data. Each fixture contains
a JSON response, a JSONSection config, and expected channel output.
Optionally a fixture may declare ``_expected_warnings``: a list of
substrings that must each appear in at least one WARN-level log
record. An empty list asserts no WARNs were emitted (useful for
sparse-data sentinels). No modem-specific references.

Adding a test case = drop a JSON file in fixtures/json_parser/valid/.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from solentlabs.cable_modem_monitor_core.models.parser_config.json_format import (
    JSONSection,
)
from solentlabs.cable_modem_monitor_core.parsers.formats.json_parser import (
    JSONParser,
    _navigate_path,
)

from tests._helpers import collect_fixtures, load_fixture

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "json_parser"
VALID_FIXTURES = collect_fixtures(FIXTURES_DIR / "valid")


def _build_resources(
    json_data: Any,
    resource_key: str,
) -> dict[str, Any]:
    """Build a resource dict. Returns empty dict if json_data is None."""
    if json_data is None:
        return {}
    return {resource_key: json_data}


@pytest.mark.parametrize(
    "fixture_path",
    VALID_FIXTURES,
    ids=[f.stem for f in VALID_FIXTURES],
)
def test_extraction(fixture_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Parse JSON data and verify extracted channels match expected.

    When the fixture declares ``_expected_warnings``, also check that
    every listed substring appears in at least one WARN-level record
    (or that no WARNs fired, when the list is empty).
    """
    data = load_fixture(fixture_path)

    # Multi-resource fixtures use _resources dict; single-resource use _resource + _json
    if "_resources" in data:
        resources: dict[str, Any] = data["_resources"]
    else:
        resource_key = data["_resource"]
        resources = _build_resources(data.get("_json"), resource_key)

    section_config = JSONSection.model_validate(data["_config"])
    parser = JSONParser(section_config)

    with caplog.at_level(logging.WARNING):
        result = parser.parse(resources)
    expected = data["_expected"]

    assert result == expected, (
        f"Mismatch for {fixture_path.stem}:\n" f"  actual:   {result}\n" f"  expected: {expected}"
    )

    if "_expected_warnings" in data:
        warn_records = [r for r in caplog.records if r.levelname == "WARNING"]
        warn_messages = [r.message for r in warn_records]
        if not data["_expected_warnings"]:
            assert not warn_records, f"{fixture_path.stem}: expected no WARNs, got {warn_messages}"
        else:
            for substring in data["_expected_warnings"]:
                assert any(
                    substring in m for m in warn_messages
                ), f"{fixture_path.stem}: expected WARN containing {substring!r}, got {warn_messages}"


NAVIGATE_PATH_CASES = [
    ("dict key", {"a": {"b": 1}}, "a.b", 1),
    ("nested dict", {"a": {"b": {"c": 2}}}, "a.b.c", 2),
    ("single key", {"x": 42}, "x", 42),
    ("missing key", {"a": 1}, "b", None),
    ("missing nested key", {"a": {"b": 1}}, "a.c", None),
    ("list index", {"_raw": [{"id": 1}, {"id": 2}]}, "_raw.0", {"id": 1}),
    ("list second element", {"_raw": [{"id": 1}, {"id": 2}]}, "_raw.1", {"id": 2}),
    ("list then key", {"_raw": [{"id": 1}]}, "_raw.0.id", 1),
    ("index out of bounds", {"_raw": [{"id": 1}]}, "_raw.5", None),
    ("index on dict", {"a": {"b": 1}}, "a.0", None),
    ("key on list", {"a": [1, 2]}, "a.x", None),
]


@pytest.mark.parametrize(
    ("label", "data", "path", "expected"),
    NAVIGATE_PATH_CASES,
    ids=[c[0] for c in NAVIGATE_PATH_CASES],
)
def test_navigate_path(label: str, data: Any, path: str, expected: Any) -> None:
    """_navigate_path handles dict keys and list indices."""
    assert _navigate_path(data, path) == expected
