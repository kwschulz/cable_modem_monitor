"""Tests for XMLSystemInfoParser.

Fixture-driven tests with synthesized XML data. Each fixture contains
an XML string, an XMLSystemInfoSource config, and expected system_info.

Adding a test case = drop a JSON file in fixtures/xml_system_info/valid/.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import defusedxml.ElementTree as DefusedET
import pytest
from solentlabs.cable_modem_monitor_core.models.parser_config.system_info import (
    XMLSystemInfoSource,
)
from solentlabs.cable_modem_monitor_core.parsers.formats.xml_system_info import (
    XMLSystemInfoParser,
)

from tests._helpers import collect_fixtures, load_fixture

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "xml_system_info"
VALID_FIXTURES = collect_fixtures(FIXTURES_DIR / "valid")


def _build_resources(
    xml_str: str | None,
    resource_key: str,
    resource_value: Any = None,
) -> dict[str, Any]:
    """Build a resource dict with parsed XML Element.

    Fixture contract:
    - ``_xml: "<...>"`` → parses to an ``Element`` under ``_resource``.
    - ``_xml: null`` with no ``_resource_value`` → empty dict (the
      parser's "resource missing" branch).
    - ``_xml: null`` with ``_resource_value: <x>`` → ``{_resource: x}``
      for testing the parser's non-Element type-check branch.
    """
    if resource_value is not None:
        return {resource_key: resource_value}
    if xml_str is None:
        return {}
    return {resource_key: DefusedET.fromstring(xml_str)}


@pytest.mark.parametrize(
    "fixture_path",
    VALID_FIXTURES,
    ids=[f.stem for f in VALID_FIXTURES],
)
def test_extraction(fixture_path: Path) -> None:
    """Parse XML system_info and verify extracted fields match expected."""
    data = load_fixture(fixture_path)
    config = XMLSystemInfoSource.model_validate(data["_config"])
    resources = _build_resources(
        data["_xml"],
        data["_resource"],
        data.get("_resource_value"),
    )
    expected = data["_expected"]

    parser = XMLSystemInfoParser(config)
    result = parser.parse(resources)

    assert result == expected
