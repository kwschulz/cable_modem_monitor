"""Tests for HTMLTableParser.

Fixture-driven tests with synthesized HTML snippets. Each fixture contains
HTML, table config, and expected channel output. No modem-specific references.

Adding a test case = drop a JSON file in fixtures/html_table/valid/.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from solentlabs.cable_modem_monitor_core.models.parser_config.table import (
    TableDefinition,
)
from solentlabs.cable_modem_monitor_core.parsers.formats.html_table import HTMLTableParser

from tests._helpers import collect_fixtures, load_fixture

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "html_table"
VALID_FIXTURES = collect_fixtures(FIXTURES_DIR / "valid")


def _build_resources(html: str | None, resource_key: str) -> dict[str, BeautifulSoup]:
    """Build a resource dict. Returns empty dict if html is None."""
    if html is None:
        return {}
    return {resource_key: BeautifulSoup(html, "html.parser")}


@pytest.mark.parametrize(
    "fixture_path",
    VALID_FIXTURES,
    ids=[f.stem for f in VALID_FIXTURES],
)
def test_extraction(fixture_path: Path) -> None:
    """Parse HTML table and verify extracted channels match expected."""
    data = load_fixture(fixture_path)

    resource_key = data["_resource"]
    resources = _build_resources(data.get("_html"), resource_key)

    table_def = TableDefinition(**data["_config"])
    parser = HTMLTableParser(resource_key, table_def)

    result = parser.parse(resources)
    expected = data["_expected"]

    assert result == expected, (
        f"Mismatch for {fixture_path.stem}:\n" f"  actual:   {result}\n" f"  expected: {expected}"
    )
