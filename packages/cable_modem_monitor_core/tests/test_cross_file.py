"""Tests for cross-file consistency validation.

Each fixture has ``_modem`` and ``_parser`` dicts that are independently
valid Pydantic configs. The cross-file check validates constraints that
span both files.

Valid fixtures pass with no errors. Invalid fixtures produce errors
matching ``_expected_error``.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from solentlabs.cable_modem_monitor_core.models.modem_config import ModemConfig
from solentlabs.cable_modem_monitor_core.models.parser_config import ParserConfig
from solentlabs.cable_modem_monitor_core.validation.cross_file import (
    validate_cross_file,
)

from tests._helpers import collect_fixtures, load_fixture

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "cross_file"
VALID_DIR = FIXTURES_DIR / "valid"
INVALID_DIR = FIXTURES_DIR / "invalid"
WARNING_DIR = FIXTURES_DIR / "warning"


# ---------------------------------------------------------------------------
# Valid configs — cross-file checks pass
# ---------------------------------------------------------------------------

VALID_FIXTURES = collect_fixtures(VALID_DIR)


@pytest.mark.parametrize(
    "fixture_path",
    VALID_FIXTURES,
    ids=[f.stem for f in VALID_FIXTURES],
)
def test_valid_cross_file(fixture_path: Path) -> None:
    """Valid fixture pair passes cross-file validation."""
    fixture = load_fixture(fixture_path)
    modem = ModemConfig.model_validate(fixture["_modem"])
    parser = ParserConfig.model_validate(fixture["_parser"])
    errors = validate_cross_file(modem, parser)
    assert errors == [], f"Unexpected errors: {errors}"


# ---------------------------------------------------------------------------
# Invalid configs — cross-file checks catch errors
# ---------------------------------------------------------------------------

INVALID_FIXTURES = collect_fixtures(INVALID_DIR)


@pytest.mark.parametrize(
    "fixture_path",
    INVALID_FIXTURES,
    ids=[f.stem for f in INVALID_FIXTURES],
)
def test_invalid_cross_file(fixture_path: Path) -> None:
    """Invalid fixture pair produces expected cross-file error."""
    fixture = load_fixture(fixture_path)
    modem = ModemConfig.model_validate(fixture["_modem"])
    parser = ParserConfig.model_validate(fixture["_parser"])
    expected_error = fixture["_expected_error"]

    errors = validate_cross_file(modem, parser)
    assert errors, "Expected cross-file errors but got none"
    combined = " | ".join(errors)
    assert re.search(expected_error, combined), f"Expected error matching '{expected_error}', got: {errors}"


# ---------------------------------------------------------------------------
# XML format hard stop (uses MagicMock — can't be expressed as fixture)
# ---------------------------------------------------------------------------


class TestXmlFormatHardStop:
    """XML format produces explicit unsupported error."""

    def test_xml_format_error_message(self) -> None:
        """XML format produces 'not yet supported' rather than generic format error."""
        modem = MagicMock(spec=ModemConfig)
        modem.transport = "http"

        parser = MagicMock(spec=ParserConfig)
        parser.downstream = MagicMock()
        parser.downstream.format = "xml"
        parser.upstream = None
        parser.system_info = None
        parser.aggregate = {}

        errors = validate_cross_file(modem, parser)
        assert len(errors) == 1
        assert "XML format" in errors[0]
        assert "not yet supported" in errors[0]


# ---------------------------------------------------------------------------
# auth:none validation warnings — fixture-driven
# ---------------------------------------------------------------------------

WARNING_FIXTURES = collect_fixtures(WARNING_DIR) if WARNING_DIR.is_dir() else []


@pytest.mark.parametrize(
    "fixture_path",
    WARNING_FIXTURES,
    ids=[f.stem for f in WARNING_FIXTURES],
)
def test_auth_none_session_cookie_warning(fixture_path: Path) -> None:
    """ModemConfig validation warnings (fixture-driven)."""
    fixture = load_fixture(fixture_path)
    expected_warning = fixture.get("_expected_warning")

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        ModemConfig.model_validate(fixture["_config"])

        auth_warnings = [x for x in w if "auth:none" in str(x.message)]
        if expected_warning:
            assert len(auth_warnings) == 1, f"Expected warning matching {expected_warning!r}"
            assert expected_warning in str(auth_warnings[0].message)
        else:
            assert len(auth_warnings) == 0, f"Unexpected warnings: {auth_warnings}"
