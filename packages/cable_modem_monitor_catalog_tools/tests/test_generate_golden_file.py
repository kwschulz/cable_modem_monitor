"""Tests for generate_golden_file MCP tool.

Fixture-driven integration tests: each fixture contains a HAR dict,
parser.yaml content, and expected golden file output.

Adding a test case = drop a JSON file in fixtures/generate_golden_file/.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from solentlabs.cable_modem_monitor_catalog_tools.generate_golden_file import (
    generate_golden_file,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "generate_golden_file"
VALID_FIXTURES = sorted((FIXTURES_DIR / "valid").glob("*.json"))
INVALID_FIXTURES = sorted((FIXTURES_DIR / "invalid").glob("*.json"))
MINIMAL_PARSER_YAML = (FIXTURES_DIR / "minimal_parser.yaml").read_text()


def _load_fixture(path: Path) -> dict[str, Any]:
    """Load a JSON fixture file."""
    return dict(json.loads(path.read_text()))


# --- Valid fixtures: full golden file comparison ---


@pytest.mark.parametrize(
    "fixture_path",
    VALID_FIXTURES,
    ids=[f.stem for f in VALID_FIXTURES],
)
def test_valid_golden_file(fixture_path: Path, tmp_path: Path) -> None:
    """Generate golden file from HAR and verify against expected output."""
    data = _load_fixture(fixture_path)

    har_file = tmp_path / "modem.har"
    har_file.write_text(json.dumps(data["_har"]))

    result = generate_golden_file(
        har_path=str(har_file),
        parser_yaml_content=data["_parser_yaml"],
    )

    assert not result.errors, f"Unexpected errors: {result.errors}"
    assert result.channel_counts == data["_expected_channel_counts"]
    assert result.system_info_fields == data["_expected_system_info_fields"]
    assert result.missing_system_info_fields == data["_expected_missing_system_info_fields"]
    assert result.golden_file == data["_expected_golden_file"]
    assert (
        result.golden_file_json == json.dumps(result.golden_file, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )


# --- Invalid fixtures: expected errors ---


@pytest.mark.parametrize(
    "fixture_path",
    INVALID_FIXTURES,
    ids=[f.stem for f in INVALID_FIXTURES],
)
def test_invalid_golden_file(fixture_path: Path, tmp_path: Path) -> None:
    """Generate golden file from bad input and verify error message."""
    data = _load_fixture(fixture_path)

    har_file = tmp_path / "modem.har"
    har_file.write_text(json.dumps(data["_har"]))

    result = generate_golden_file(
        har_path=str(har_file),
        parser_yaml_content=data["_parser_yaml"],
    )

    expected_msg = data["_expected_error_contains"]
    assert any(
        expected_msg in e for e in result.errors
    ), f"Expected error containing '{expected_msg}', got: {result.errors}"


# --- Inline tests for cases that need temp file manipulation ---


class TestFileErrors:
    """Tests requiring invalid file paths (can't be expressed as fixtures)."""

    def test_invalid_parser_yaml(self, tmp_path: Path) -> None:
        """Invalid parser.yaml content returns errors."""
        har_file = tmp_path / "modem.har"
        har_file.write_text('{"log": {"entries": []}}')

        result = generate_golden_file(
            har_path=str(har_file),
            parser_yaml_content="not: valid: yaml: [",
        )
        assert len(result.errors) > 0

    @pytest.mark.parametrize("parser_yaml", [None, ""], ids=["none", "empty"])
    def test_empty_parser_yaml(self, tmp_path: Path, parser_yaml: str | None) -> None:
        """No parser (analyze found no data sections) gets a legible error, not a traceback."""
        har_file = tmp_path / "modem.har"
        har_file.write_text('{"log": {"entries": []}}')

        result = generate_golden_file(
            har_path=str(har_file),
            parser_yaml_content=parser_yaml,
        )
        assert result.golden_file == {}
        assert len(result.errors) == 1
        assert "no data sections" in result.errors[0]
        assert "NoneType" not in result.errors[0]

    def test_missing_har_file(self, tmp_path: Path) -> None:
        """Missing HAR file returns errors."""
        result = generate_golden_file(
            har_path=str(tmp_path / "nonexistent.har"),
            parser_yaml_content=MINIMAL_PARSER_YAML,
        )
        assert len(result.errors) > 0
