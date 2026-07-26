"""Tests for table_selector — isolated selector logic.

Fixture-driven tests with synthesized HTML grounded in real modem patterns.
Each fixture contains HTML, a selector config, and expected match criteria.
No modem-specific references — patterns are named by structure, not model.

Adding a test case = drop a JSON file in fixtures/table_selector/.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from solentlabs.cable_modem_monitor_core.models.parser_config.common import (
    TableSelector,
)
from solentlabs.cable_modem_monitor_core.parsers.table_selector import find_table

from tests._helpers import collect_fixtures, load_fixture

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "table_selector"
FIXTURES = collect_fixtures(FIXTURES_DIR)


@pytest.mark.parametrize(
    "fixture_path",
    FIXTURES,
    ids=[f.stem for f in FIXTURES],
)
def test_find_table(fixture_path: Path) -> None:
    """Find table using configured selector and verify match criteria."""
    data = load_fixture(fixture_path)

    soup = BeautifulSoup(data["_html"], "html.parser")
    selector = TableSelector(**data["_selector"])

    result = find_table(soup, selector)

    if data.get("_expected_none"):
        assert result is None, f"Expected None for {fixture_path.stem}, got: {result}"
        return

    assert result is not None, f"Expected a table for {fixture_path.stem}, got None"
    assert result.name == data["_expected_tag"], f"Expected <{data['_expected_tag']}>, got <{result.name}>"

    # Verify we found the RIGHT table, not just any table
    if "_expected_has_text" in data:
        table_text = result.get_text()
        assert data["_expected_has_text"] in table_text, (
            f"Expected table to contain '{data['_expected_has_text']}' " f"for {fixture_path.stem}"
        )

    if "_expected_attr" in data:
        for attr_name, attr_value in data["_expected_attr"].items():
            actual = result.get(attr_name)
            assert actual == attr_value, (
                f"Expected {attr_name}={attr_value}, " f"got {attr_name}={actual} for {fixture_path.stem}"
            )
