"""Tests for JSONSystemInfoParser.

Fixture-driven tests with synthesized JSON data. Each fixture contains
a JSON response, a JSONSystemInfoSource config, and expected system_info
output. No modem-specific references.

Adding a test case = drop a JSON file in fixtures/json_system_info/valid/.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from solentlabs.cable_modem_monitor_core.models.parser_config.system_info import (
    JSONSystemInfoSource,
)
from solentlabs.cable_modem_monitor_core.parsers.formats.json_system_info import (
    JSONSystemInfoParser,
)

from tests._helpers import collect_fixtures, load_fixture

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "json_system_info"
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
def test_extraction(fixture_path: Path) -> None:
    """Parse JSON data and verify extracted system_info matches expected."""
    data = load_fixture(fixture_path)

    resource_key = data["_resource"]
    resources = _build_resources(data.get("_json"), resource_key)

    source_config = JSONSystemInfoSource(**data["_config"])
    parser = JSONSystemInfoParser(source_config)

    result = parser.parse(resources)
    expected = data["_expected"]

    assert result == expected, (
        f"Mismatch for {fixture_path.stem}:\n" f"  actual:   {result}\n" f"  expected: {expected}"
    )
