"""Tests for JSSystemInfoParser.

Fixture-driven tests with synthesized HTML snippets. Each fixture contains
an HTML page with JS functions, a JSSystemInfoSource config, and expected
system_info output. No modem-specific references.

Adding a test case = drop a JSON file in fixtures/js_system_info/valid/.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from solentlabs.cable_modem_monitor_core.models.parser_config.system_info import (
    JSSystemInfoSource,
)
from solentlabs.cable_modem_monitor_core.parsers.formats.js_system_info import (
    JSSystemInfoParser,
)

from tests._helpers import collect_fixtures, load_fixture

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "js_system_info"
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
    """Parse JS system_info and verify extracted fields match expected."""
    data = load_fixture(fixture_path)

    resource_key = data["_resource"]
    resources = _build_resources(data.get("_html"), resource_key)

    config = JSSystemInfoSource(**data["_config"])
    parser = JSSystemInfoParser(config)

    result = parser.parse(resources)
    expected = data["_expected"]

    assert result == expected, (
        f"Mismatch for {fixture_path.stem}:\n" f"  actual:   {result}\n" f"  expected: {expected}"
    )
