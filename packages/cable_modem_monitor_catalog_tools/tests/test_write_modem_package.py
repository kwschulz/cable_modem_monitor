"""Tests for the write_modem_package MCP tool.

Covers: happy path, existing files skipped, missing HAR source,
optional parser.py, golden file formatting.

Fixture data lives in tests/fixtures/write_modem_package/.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from solentlabs.cable_modem_monitor_catalog_tools.write_modem_package import write_modem_package
from tests._helpers import load_fixture

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "write_modem_package"
_BASIC = load_fixture(_FIXTURES_DIR / "basic.json")
_FORM_AUTH = load_fixture(_FIXTURES_DIR / "form_auth.json")

_MODEM_YAML: str = _BASIC["modem_yaml"]
_PARSER_YAML: str = _BASIC["parser_yaml"]
_GOLDEN: dict[str, Any] = _BASIC["golden_file"]
_PARSER_PY: str = _BASIC["parser_py"]
_FORM_AUTH_YAML: str = _FORM_AUTH["modem_yaml"]
_LOGIN_PAGE_HTML: str = _FORM_AUTH["login_page_html"]


def _create_har(tmp_path: Path) -> Path:
    """Create a minimal HAR file and return its path."""
    har = tmp_path / "source.har"
    har.write_text('{"log": {"entries": []}}', encoding="utf-8")
    return har


# ---------------------------------------------------------------------------
# Input guards — refuse incomplete packages before touching disk
# ---------------------------------------------------------------------------


class TestEmptyParserGuard:
    """A missing parser.yaml refuses the write with nothing on disk.

    The generate stage returns parser_yaml None when analyze_har found
    no data sections; writing then must fail whole, not leave a partial
    package (modem.yaml written, then a crash on the parser write).
    """

    def test_none_parser_refused_before_writing(self, tmp_path: Path) -> None:
        """parser_yaml=None: legible error, no files, no directories."""
        har_path = _create_har(tmp_path)
        out_dir = tmp_path / "modems" / "solentlabs" / "t100"

        result = write_modem_package(
            output_dir=str(out_dir),
            modem_yaml=_MODEM_YAML,
            parser_yaml=None,
            golden_file=_GOLDEN,
            har_path=str(har_path),
        )

        assert result.errors and "no data sections" in result.errors[0]
        assert result.files_written == []
        assert not out_dir.exists()

    def test_empty_parser_refused_before_writing(self, tmp_path: Path) -> None:
        """parser_yaml='': same refusal as None."""
        har_path = _create_har(tmp_path)
        out_dir = tmp_path / "modems" / "solentlabs" / "t100"

        result = write_modem_package(
            output_dir=str(out_dir),
            modem_yaml=_MODEM_YAML,
            parser_yaml="",
            golden_file=_GOLDEN,
            har_path=str(har_path),
        )

        assert result.errors and "no data sections" in result.errors[0]
        assert result.files_written == []
        assert not out_dir.exists()


# ---------------------------------------------------------------------------
# Happy path — creates full directory structure
# ---------------------------------------------------------------------------


class TestHappyPath:
    """write_modem_package creates the standard catalog structure."""

    def test_creates_all_files(self, tmp_path: Path) -> None:
        """All expected files are created."""
        har_path = _create_har(tmp_path)
        out_dir = tmp_path / "modems" / "solentlabs" / "t100"

        result = write_modem_package(
            output_dir=str(out_dir),
            modem_yaml=_MODEM_YAML,
            parser_yaml=_PARSER_YAML,
            golden_file=_GOLDEN,
            har_path=str(har_path),
        )

        assert (out_dir / "modem.yaml").is_file()
        assert (out_dir / "parser.yaml").is_file()
        assert (out_dir / "test_data" / "modem.expected.json").is_file()
        assert (out_dir / "test_data" / "modem.har").is_file()
        assert len(result.files_written) == 4
        assert result.files_skipped == []
        assert result.modem_dir == str(out_dir)

    def test_modem_yaml_content(self, tmp_path: Path) -> None:
        """modem.yaml has the expected content."""
        har_path = _create_har(tmp_path)
        out_dir = tmp_path / "out"

        write_modem_package(
            output_dir=str(out_dir),
            modem_yaml=_MODEM_YAML,
            parser_yaml=_PARSER_YAML,
            golden_file=_GOLDEN,
            har_path=str(har_path),
        )

        assert (out_dir / "modem.yaml").read_text() == _MODEM_YAML

    def test_golden_file_formatted(self, tmp_path: Path) -> None:
        """Golden file is pretty-printed JSON with trailing newline."""
        har_path = _create_har(tmp_path)
        out_dir = tmp_path / "out"

        write_modem_package(
            output_dir=str(out_dir),
            modem_yaml=_MODEM_YAML,
            parser_yaml=_PARSER_YAML,
            golden_file=_GOLDEN,
            har_path=str(har_path),
        )

        content = (out_dir / "test_data" / "modem.expected.json").read_text()
        assert content.endswith("\n")
        parsed = json.loads(content)
        assert parsed == _GOLDEN

    def test_har_copied(self, tmp_path: Path) -> None:
        """HAR file is copied to test_data/modem.har."""
        har_path = _create_har(tmp_path)
        out_dir = tmp_path / "out"

        write_modem_package(
            output_dir=str(out_dir),
            modem_yaml=_MODEM_YAML,
            parser_yaml=_PARSER_YAML,
            golden_file=_GOLDEN,
            har_path=str(har_path),
        )

        copied = (out_dir / "test_data" / "modem.har").read_text()
        assert copied == har_path.read_text()


# ---------------------------------------------------------------------------
# Optional parser.py
# ---------------------------------------------------------------------------


class TestParserPy:
    """Optional parser.py file handling."""

    def test_parser_py_written(self, tmp_path: Path) -> None:
        """parser.py is written when provided."""
        har_path = _create_har(tmp_path)
        out_dir = tmp_path / "out"

        result = write_modem_package(
            output_dir=str(out_dir),
            modem_yaml=_MODEM_YAML,
            parser_yaml=_PARSER_YAML,
            golden_file=_GOLDEN,
            har_path=str(har_path),
            parser_py=_PARSER_PY,
        )

        assert (out_dir / "parser.py").is_file()
        assert (out_dir / "parser.py").read_text() == _PARSER_PY
        assert len(result.files_written) == 5

    def test_no_parser_py(self, tmp_path: Path) -> None:
        """No parser.py created when not provided."""
        har_path = _create_har(tmp_path)
        out_dir = tmp_path / "out"

        write_modem_package(
            output_dir=str(out_dir),
            modem_yaml=_MODEM_YAML,
            parser_yaml=_PARSER_YAML,
            golden_file=_GOLDEN,
            har_path=str(har_path),
        )

        assert not (out_dir / "parser.py").exists()


# ---------------------------------------------------------------------------
# Skip existing files
# ---------------------------------------------------------------------------


class TestSkipExisting:
    """Existing files are not overwritten."""

    def test_existing_modem_yaml_skipped(self, tmp_path: Path) -> None:
        """Pre-existing modem.yaml is skipped, not overwritten."""
        har_path = _create_har(tmp_path)
        out_dir = tmp_path / "out"
        out_dir.mkdir(parents=True)
        existing = out_dir / "modem.yaml"
        existing.write_text("original content")

        result = write_modem_package(
            output_dir=str(out_dir),
            modem_yaml=_MODEM_YAML,
            parser_yaml=_PARSER_YAML,
            golden_file=_GOLDEN,
            har_path=str(har_path),
        )

        assert existing.read_text() == "original content"
        assert str(existing) in result.files_skipped

    def test_existing_har_skipped(self, tmp_path: Path) -> None:
        """Pre-existing modem.har is skipped."""
        har_path = _create_har(tmp_path)
        out_dir = tmp_path / "out"
        test_data = out_dir / "test_data"
        test_data.mkdir(parents=True)
        existing_har = test_data / "modem.har"
        existing_har.write_text("original har")

        result = write_modem_package(
            output_dir=str(out_dir),
            modem_yaml=_MODEM_YAML,
            parser_yaml=_PARSER_YAML,
            golden_file=_GOLDEN,
            har_path=str(har_path),
        )

        assert existing_har.read_text() == "original har"
        assert str(existing_har) in result.files_skipped


# ---------------------------------------------------------------------------
# Missing HAR source
# ---------------------------------------------------------------------------


class TestMissingHar:
    """Missing HAR source file handling."""

    def test_missing_har_source(self, tmp_path: Path) -> None:
        """Missing HAR source is reported in files_skipped."""
        out_dir = tmp_path / "out"

        result = write_modem_package(
            output_dir=str(out_dir),
            modem_yaml=_MODEM_YAML,
            parser_yaml=_PARSER_YAML,
            golden_file=_GOLDEN,
            har_path="/nonexistent/modem.har",
        )

        assert any("source not found" in s for s in result.files_skipped)
        assert not (out_dir / "test_data" / "modem.har").exists()


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    """WriteResult serialization."""

    def test_to_dict(self, tmp_path: Path) -> None:
        """to_dict returns expected structure."""
        har_path = _create_har(tmp_path)
        out_dir = tmp_path / "out"

        result = write_modem_package(
            output_dir=str(out_dir),
            modem_yaml=_MODEM_YAML,
            parser_yaml=_PARSER_YAML,
            golden_file=_GOLDEN,
            har_path=str(har_path),
        )
        d = result.to_dict()

        assert "modem_dir" in d
        assert "files_written" in d
        assert "files_skipped" in d


# ---------------------------------------------------------------------------
# Variant support
# ---------------------------------------------------------------------------


class TestVariant:
    """Modem variant naming support."""

    def test_variant_file_names(self, tmp_path: Path) -> None:
        """Variant uses modem-{name} prefix for test data files."""
        har_path = _create_har(tmp_path)
        out_dir = tmp_path / "out"

        write_modem_package(
            output_dir=str(out_dir),
            modem_yaml=_MODEM_YAML,
            parser_yaml=_PARSER_YAML,
            golden_file=_GOLDEN,
            har_path=str(har_path),
            variant_name="v2",
        )

        assert (out_dir / "test_data" / "modem-v2.har").is_file()
        assert (out_dir / "test_data" / "modem-v2.expected.json").is_file()
        # Config files are shared — no variant suffix
        assert (out_dir / "modem.yaml").is_file()
        assert (out_dir / "parser.yaml").is_file()

    def test_variant_does_not_write_default_har(self, tmp_path: Path) -> None:
        """Variant does not create modem.har — only modem-{name}.har."""
        har_path = _create_har(tmp_path)
        out_dir = tmp_path / "out"

        write_modem_package(
            output_dir=str(out_dir),
            modem_yaml=_MODEM_YAML,
            parser_yaml=_PARSER_YAML,
            golden_file=_GOLDEN,
            har_path=str(har_path),
            variant_name="v2",
        )

        assert not (out_dir / "test_data" / "modem.har").exists()
        assert not (out_dir / "test_data" / "modem.expected.json").exists()


# ---------------------------------------------------------------------------
# login_page / HAR fixture consistency check
# ---------------------------------------------------------------------------


def _create_form_auth_har(tmp_path: Path, login_page_body: str = _LOGIN_PAGE_HTML) -> Path:
    """Create a HAR with a login page GET and login POST."""
    har = tmp_path / "source.har"
    har.write_text(
        json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "request": {"method": "GET", "url": "http://192.168.100.1/LoginPage.asp"},
                            "response": {
                                "status": 200,
                                "headers": [],
                                "content": {"mimeType": "text/html", "text": login_page_body},
                            },
                        },
                        {
                            "request": {
                                "method": "POST",
                                "url": "http://192.168.100.1/goform/Login",
                                "postData": {
                                    "mimeType": "application/x-www-form-urlencoded",
                                    "text": "user=a&pass=b&token=abc",
                                },
                            },
                            "response": {
                                "status": 302,
                                "headers": [{"name": "Location", "value": "/Index.asp"}],
                                "content": {},
                            },
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    return har


class TestLoginPageValidation:
    """HAR fixture must contain the login page response when login_page is configured."""

    def test_valid_har_passes(self, tmp_path: Path) -> None:
        """Form auth HAR with correct login page entry is accepted."""
        har_path = _create_form_auth_har(tmp_path)
        out_dir = tmp_path / "out"

        result = write_modem_package(
            output_dir=str(out_dir),
            modem_yaml=_FORM_AUTH_YAML,
            parser_yaml=_PARSER_YAML,
            golden_file=_GOLDEN,
            har_path=str(har_path),
        )

        assert result.errors == []
        assert (out_dir / "test_data" / "modem.har").is_file()

    def test_missing_login_page_entry_blocked(self, tmp_path: Path) -> None:
        """Form auth HAR missing the login page GET entry is rejected."""
        har = tmp_path / "no_login_page.har"
        har.write_text(
            json.dumps(
                {
                    "log": {
                        "entries": [
                            {
                                "request": {"method": "POST", "url": "http://192.168.100.1/goform/Login"},
                                "response": {"status": 302, "headers": [], "content": {}},
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        out_dir = tmp_path / "out"

        result = write_modem_package(
            output_dir=str(out_dir),
            modem_yaml=_FORM_AUTH_YAML,
            parser_yaml=_PARSER_YAML,
            golden_file=_GOLDEN,
            har_path=str(har),
        )

        assert result.errors
        assert "no GET entry" in result.errors[0]
        assert not (out_dir / "test_data" / "modem.har").exists()

    def test_empty_login_page_body_blocked(self, tmp_path: Path) -> None:
        """Form auth HAR with login page entry but empty body is rejected."""
        har_path = _create_form_auth_har(tmp_path, login_page_body="")
        out_dir = tmp_path / "out"

        result = write_modem_package(
            output_dir=str(out_dir),
            modem_yaml=_FORM_AUTH_YAML,
            parser_yaml=_PARSER_YAML,
            golden_file=_GOLDEN,
            har_path=str(har_path),
        )

        assert result.errors
        assert "empty" in result.errors[0]

    def test_wrong_login_page_action_blocked(self, tmp_path: Path) -> None:
        """Login page HTML that doesn't reference auth.action is rejected."""
        wrong_html = "<html><body><form action='/different/endpoint'></form></body></html>"
        har_path = _create_form_auth_har(tmp_path, login_page_body=wrong_html)
        out_dir = tmp_path / "out"

        result = write_modem_package(
            output_dir=str(out_dir),
            modem_yaml=_FORM_AUTH_YAML,
            parser_yaml=_PARSER_YAML,
            golden_file=_GOLDEN,
            har_path=str(har_path),
        )

        assert result.errors
        assert "does not reference" in result.errors[0]

    def test_non_form_auth_skips_check(self, tmp_path: Path) -> None:
        """Non-form auth strategies skip the login_page check."""
        har_path = _create_har(tmp_path)
        out_dir = tmp_path / "out"
        basic_yaml = "manufacturer: T\nmodel: T\nauth:\n  strategy: basic\n"

        result = write_modem_package(
            output_dir=str(out_dir),
            modem_yaml=basic_yaml,
            parser_yaml=_PARSER_YAML,
            golden_file=_GOLDEN,
            har_path=str(har_path),
        )

        assert result.errors == []

    def test_form_auth_no_login_page_skips_check(self, tmp_path: Path) -> None:
        """Form auth without login_page set skips the check."""
        har_path = _create_har(tmp_path)
        out_dir = tmp_path / "out"
        no_prefetch_yaml = (
            "manufacturer: T\nmodel: T\nauth:\n"
            "  strategy: form\n  action: /login\n"
            "  username_field: u\n  password_field: p\n"
        )

        result = write_modem_package(
            output_dir=str(out_dir),
            modem_yaml=no_prefetch_yaml,
            parser_yaml=_PARSER_YAML,
            golden_file=_GOLDEN,
            har_path=str(har_path),
        )

        assert result.errors == []
