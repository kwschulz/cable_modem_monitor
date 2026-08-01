"""Tests for HTMLTableTransposedParser.

Fixture-driven tests with synthesized HTML snippets. Each fixture contains
HTML, transposed table config, and expected channel output. No modem-specific
references.

Adding a test case = drop a JSON file in fixtures/html_table_transposed/valid/.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from solentlabs.cable_modem_monitor_core.loaders.html_normalize import normalize_html
from solentlabs.cable_modem_monitor_core.models.parser_config.transposed import (
    TransposedTableDefinition,
)
from solentlabs.cable_modem_monitor_core.parsers.formats.html_table_transposed import (
    HTMLTableTransposedParser,
)

from tests._helpers import collect_fixtures, load_fixture

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "html_table_transposed"
VALID_FIXTURES = collect_fixtures(FIXTURES_DIR / "valid")


def _build_resources(html: str | None, resource_key: str, *, normalize: bool = False) -> dict[str, BeautifulSoup]:
    """Build a resource dict. Returns empty dict if html is None."""
    if html is None:
        return {}
    if normalize:
        html = normalize_html(html)
    return {resource_key: BeautifulSoup(html, "html.parser")}


@pytest.mark.parametrize(
    "fixture_path",
    VALID_FIXTURES,
    ids=[f.stem for f in VALID_FIXTURES],
)
def test_extraction(fixture_path: Path) -> None:
    """Parse transposed HTML table and verify extracted channels match expected."""
    data = load_fixture(fixture_path)

    resource_key = data["_resource"]
    # Allow fixtures to specify a different key for the resource dict
    resource_dict_key = data.get("_resource_key", resource_key)
    resources = _build_resources(data.get("_html"), resource_dict_key, normalize=data.get("_normalize", False))

    table_def = TransposedTableDefinition(**data["_config"])
    parser = HTMLTableTransposedParser(resource_key, table_def)

    result = parser.parse(resources)
    expected = data["_expected"]

    assert result == expected, (
        f"Mismatch for {fixture_path.stem}:\n" f"  actual:   {result}\n" f"  expected: {expected}"
    )
