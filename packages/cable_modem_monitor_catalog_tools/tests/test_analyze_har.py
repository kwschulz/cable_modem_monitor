"""Integration tests for analyze_har orchestrator.

Tests the full pipeline: HAR file → Phases 1-4 → AnalysisResult.
Uses fixture-driven tests for valid analysis and hard stop detection.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from solentlabs.cable_modem_monitor_catalog import CATALOG_PATH
from solentlabs.cable_modem_monitor_catalog_tools.analyze_har import (
    AnalysisResult,
    analyze_har,
)
from tests._helpers import collect_fixtures, load_fixture, write_har

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "analyze_har"
VALID_DIR = FIXTURES_DIR / "valid"
INVALID_DIR = FIXTURES_DIR / "invalid"

VALID_FIXTURES = collect_fixtures(VALID_DIR)
INVALID_FIXTURES = collect_fixtures(INVALID_DIR)


# =====================================================================
# Valid fixture tests — fixture-driven
# =====================================================================


@pytest.mark.parametrize(
    "fixture_path",
    VALID_FIXTURES,
    ids=[f.stem for f in VALID_FIXTURES],
)
def test_valid_analysis_transport(fixture_path: Path, tmp_path: Path) -> None:
    """Correct transport detected for each valid fixture."""
    data = load_fixture(fixture_path)
    har_file = write_har(tmp_path, data["_har"])
    result = analyze_har(har_file)
    assert result.transport.transport == data["_expected_transport"]


@pytest.mark.parametrize(
    "fixture_path",
    VALID_FIXTURES,
    ids=[f.stem for f in VALID_FIXTURES],
)
def test_valid_analysis_auth_strategy(fixture_path: Path, tmp_path: Path) -> None:
    """Correct auth strategy detected for each valid fixture."""
    data = load_fixture(fixture_path)
    har_file = write_har(tmp_path, data["_har"])
    result = analyze_har(har_file)
    assert result.auth.strategy == data["_expected_auth_strategy"]
    assert result.auth.confidence == data.get("_expected_auth_confidence", "high")


@pytest.mark.parametrize(
    "fixture_path",
    VALID_FIXTURES,
    ids=[f.stem for f in VALID_FIXTURES],
)
def test_valid_analysis_session(fixture_path: Path, tmp_path: Path) -> None:
    """Correct session cookie detected for each valid fixture."""
    data = load_fixture(fixture_path)
    har_file = write_har(tmp_path, data["_har"])
    result = analyze_har(har_file)
    assert result.session.cookie_name == data["_expected_session_cookie"]


@pytest.mark.parametrize(
    "fixture_path",
    VALID_FIXTURES,
    ids=[f.stem for f in VALID_FIXTURES],
)
def test_valid_analysis_actions(fixture_path: Path, tmp_path: Path) -> None:
    """Correct actions detected for each valid fixture."""
    data = load_fixture(fixture_path)
    har_file = write_har(tmp_path, data["_har"])
    result = analyze_har(har_file)

    expected_logout = data["_expected_logout"]
    expected_restart = data["_expected_restart"]

    if expected_logout is None:
        assert result.actions.logout is None
    else:
        assert result.actions.logout is not None
        assert result.actions.logout.type == expected_logout["type"]
        assert result.actions.logout.method == expected_logout["method"]
        assert result.actions.logout.endpoint == expected_logout["endpoint"]

    if expected_restart is None:
        assert result.actions.restart is None
    else:
        assert result.actions.restart is not None
        assert result.actions.restart.type == expected_restart["type"]
        assert result.actions.restart.method == expected_restart["method"]
        assert result.actions.restart.endpoint == expected_restart["endpoint"]


@pytest.mark.parametrize(
    "fixture_path",
    VALID_FIXTURES,
    ids=[f.stem for f in VALID_FIXTURES],
)
def test_valid_analysis_no_hard_stops(fixture_path: Path, tmp_path: Path) -> None:
    """Valid fixtures produce no hard stops (except HNAP stub)."""
    data = load_fixture(fixture_path)
    har_file = write_har(tmp_path, data["_har"])
    result = analyze_har(har_file)

    # HNAP fixtures get a hard stop from the format_hnap stub (not yet implemented)
    if data["_expected_transport"] == "hnap":
        non_hnap_stops = [hs for hs in result.hard_stops if "HNAP format detection is not yet implemented" not in hs]
        assert non_hnap_stops == []
    else:
        assert result.hard_stops == []


# =====================================================================
# Auth field extraction tests — fixture-driven
# =====================================================================


@pytest.mark.parametrize(
    "fixture_path",
    [f for f in VALID_FIXTURES if "_expected_auth_fields" in json.loads(f.read_text())],
    ids=[f.stem for f in VALID_FIXTURES if "_expected_auth_fields" in json.loads(f.read_text())],
)
def test_valid_analysis_auth_fields(fixture_path: Path, tmp_path: Path) -> None:
    """Auth fields extracted correctly for fixtures that specify them."""
    data = load_fixture(fixture_path)
    har_file = write_har(tmp_path, data["_har"])
    result = analyze_har(har_file)

    expected_fields = data["_expected_auth_fields"]
    for key, value in expected_fields.items():
        assert key in result.auth.fields, f"Missing auth field: {key}"
        assert (
            result.auth.fields[key] == value
        ), f"Auth field {key}: expected {value!r}, got {result.auth.fields[key]!r}"


# =====================================================================
# Invalid fixture tests — hard stop detection
# =====================================================================


@pytest.mark.parametrize(
    "fixture_path",
    INVALID_FIXTURES,
    ids=[f.stem for f in INVALID_FIXTURES],
)
def test_invalid_analysis_hard_stop(fixture_path: Path, tmp_path: Path) -> None:
    """Invalid fixtures produce the expected hard stop."""
    data = load_fixture(fixture_path)
    har_file = write_har(tmp_path, data["_har"])
    result = analyze_har(har_file)
    assert result.hard_stops, "Expected at least one hard stop"
    expected_msg = data["_expected_hard_stop"]
    assert any(expected_msg in hs for hs in result.hard_stops)


# =====================================================================
# Error handling tests — inline
# =====================================================================


class TestAnalyzeHarErrors:
    """Error handling for bad inputs."""

    def test_file_not_found(self, tmp_path: Path) -> None:
        """Missing file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            analyze_har(tmp_path / "nonexistent.har")

    def test_invalid_json(self, tmp_path: Path) -> None:
        """Invalid JSON raises ValueError."""
        bad_file = tmp_path / "bad.har"
        bad_file.write_text("not json")
        with pytest.raises(ValueError, match="Invalid JSON"):
            analyze_har(bad_file)

    def test_empty_entries(self, tmp_path: Path) -> None:
        """HAR with no entries raises ValueError."""
        empty_har = tmp_path / "empty.har"
        empty_har.write_text(json.dumps({"log": {"entries": []}}))
        with pytest.raises(ValueError, match="no entries"):
            analyze_har(empty_har)

    def test_missing_log(self, tmp_path: Path) -> None:
        """HAR without log.entries raises ValueError."""
        no_log = tmp_path / "nolog.har"
        no_log.write_text(json.dumps({"version": "1.0"}))
        with pytest.raises(ValueError, match="no entries"):
            analyze_har(no_log)


# =====================================================================
# Serialization test — inline
# =====================================================================


class TestAnalysisResultSerialization:
    """AnalysisResult.to_dict() matches spec output contract."""

    def test_to_dict_structure(self, tmp_path: Path) -> None:
        """Serialized output has all expected top-level keys."""
        # Use simplest fixture
        data = load_fixture(VALID_DIR / "auth_none.json")
        har_file = write_har(tmp_path, data["_har"])
        result = analyze_har(har_file)
        d = result.to_dict()

        assert "transport" in d
        assert "confidence" in d
        assert "auth" in d
        assert "session" in d
        assert "actions" in d
        assert "sections" in d
        assert "warnings" in d
        assert "hard_stops" in d
        assert "unread_resources" in d

    def test_to_dict_types(self, tmp_path: Path) -> None:
        """Serialized values have correct types."""
        data = load_fixture(VALID_DIR / "auth_none.json")
        har_file = write_har(tmp_path, data["_har"])
        result = analyze_har(har_file)
        d = result.to_dict()

        assert isinstance(d["transport"], str)
        assert isinstance(d["confidence"], str)
        assert isinstance(d["auth"], dict)
        assert isinstance(d["session"], dict)
        assert isinstance(d["actions"], dict)
        # sections is None or a dict depending on whether data pages exist
        assert d["sections"] is None or isinstance(d["sections"], dict)
        assert isinstance(d["warnings"], list)
        assert isinstance(d["hard_stops"], list)
        assert isinstance(d["unread_resources"], list)


# =====================================================================
# Shared auth/action endpoint: committed sercomm/dm1000 HAR
# =====================================================================

_DM1000_HAR = CATALOG_PATH / "sercomm" / "dm1000" / "test_data" / "modem.har"


@pytest.fixture(scope="module")
def dm1000_result() -> AnalysisResult:
    """Analysis of the committed dm1000 HAR, where login and reboot both POST /setup.cgi."""
    return analyze_har(_DM1000_HAR)


class TestSharedAuthEndpoint:
    """The credential-shaped POST is the login, not the most recent POST to the auth endpoint."""

    def test_login_post_selected(self, dm1000_result: AnalysisResult) -> None:
        """Auth fields come from the credential POST, not the later reboot POST."""
        auth = dm1000_result.auth
        assert auth.strategy == "form"
        assert auth.confidence == "high"
        assert auth.fields["action"] == "/setup.cgi"
        assert auth.fields["username_field"] == "login_user"
        assert auth.fields["password_field"] == "pws"

    def test_login_page_and_encoding_detected(self, dm1000_result: AnalysisResult) -> None:
        """Login page found via its relative form action; encoding from its base64encode() call."""
        auth = dm1000_result.auth
        assert auth.fields["login_page"] == "/login.html"
        assert auth.fields["encoding"] == "base64"

    def test_hidden_fields_are_not_an_action_payload(self, dm1000_result: AnalysisResult) -> None:
        """No reboot payload and no credential-shaped fields in hidden_fields."""
        hidden = dm1000_result.auth.fields["hidden_fields"]
        assert hidden.get("todo") != "reboot"
        assert "passwd" not in hidden
        assert "cur_passwd" not in hidden

    def test_dropped_credential_fields_are_flagged(self, dm1000_result: AnalysisResult) -> None:
        """Surplus credential-shaped fields are surfaced for manual review, not silently dropped."""
        assert any("cur_passwd" in w for w in dm1000_result.warnings)

    def test_no_fabricated_source_inferred_actions(self, dm1000_result: AnalysisResult) -> None:
        """No action endpoints invented from UI labels or image paths in page source."""
        assert dm1000_result.actions.logout is None
        assert dm1000_result.actions.restart is None


class TestNoDataSections:
    """A capture with no parseable data pages says so, loudly.

    An unprovisioned modem serves placeholder pages, so auth analyzes
    cleanly while sections come back empty; without a warning the
    contributor first learns of it as a crash three tools later.
    """

    def test_empty_sections_warns(self, tmp_path: Path) -> None:
        """Auth-only capture gets an explicit no-data-sections warning."""
        har = {
            "log": {
                "entries": [
                    {
                        "request": {"method": "GET", "url": "http://host/", "headers": []},
                        "response": {
                            "status": 200,
                            "headers": [],
                            "content": {
                                "mimeType": "text/html",
                                "text": '<form action="/goform/Login">'
                                '<input type="text" name="loginName">'
                                '<input type="password" name="loginPassword"></form>',
                            },
                        },
                    },
                    {
                        "request": {
                            "method": "POST",
                            "url": "http://host/goform/Login",
                            "headers": [],
                            "postData": {
                                "mimeType": "application/x-www-form-urlencoded",
                                "params": [
                                    {"name": "loginName", "value": "admin"},
                                    {"name": "loginPassword", "value": "secret"},
                                ],
                            },
                        },
                        "response": {
                            "status": 302,
                            "headers": [{"name": "Location", "value": "http://host/index.htm"}],
                            "content": {"size": 0, "text": ""},
                        },
                    },
                ]
            }
        }
        har_file = write_har(tmp_path, har)
        result = analyze_har(har_file)
        assert result.sections is None
        assert any("no parseable data sections" in w for w in result.warnings)
