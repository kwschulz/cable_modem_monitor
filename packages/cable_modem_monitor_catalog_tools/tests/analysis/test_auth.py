"""Tests for Phase 2: Auth strategy detection.

Strategy detection, field extraction, and edge case tests are
fixture-driven. Utility function tests (login URL, path, form params,
encoding) are table-driven since they test pure functions with scalar
inputs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from solentlabs.cable_modem_monitor_catalog_tools.analysis import AuthDetail
from solentlabs.cable_modem_monitor_catalog_tools.analysis.auth import detect_auth
from solentlabs.cable_modem_monitor_catalog_tools.analysis.auth.hnap import (
    _detect_hmac_algorithm,
)
from solentlabs.cable_modem_monitor_catalog_tools.analysis.auth.http import (
    _extract_form_pbkdf2,
    _extract_url_token_parts,
    _HttpAuthSignals,
    _is_login_url,
    _parse_auth_scheme,
    classify_form_fields,
    detect_encoding,
)
from solentlabs.cable_modem_monitor_catalog_tools.analysis.auth.patterns import (
    _fleet_password_field_names,
    get_fleet_password_field_names,
    has_credential_fields,
    is_password_field_name,
)
from solentlabs.cable_modem_monitor_catalog_tools.validation.har_utils import (
    parse_form_params,
    path_from_url,
)
from tests._helpers import collect_fixtures, load_fixture

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "auth"
VALID_DIR = FIXTURES_DIR / "valid"
INVALID_DIR = FIXTURES_DIR / "invalid"

VALID_FIXTURES = collect_fixtures(VALID_DIR)
INVALID_FIXTURES = collect_fixtures(INVALID_DIR)


# =====================================================================
# Strategy detection - fixture-driven
# =====================================================================


@pytest.mark.parametrize("fixture_path", VALID_FIXTURES, ids=[f.stem for f in VALID_FIXTURES])
def test_valid_auth_strategy(fixture_path: Path) -> None:
    """Correct strategy and confidence for each valid fixture."""
    data = load_fixture(fixture_path)
    warnings: list[str] = []
    hard_stops: list[str] = []
    result = detect_auth(data["_entries"], data["_transport"], warnings, hard_stops)
    assert result.strategy == data["_expected_strategy"]
    assert result.confidence == data["_expected_confidence"]
    assert not hard_stops


@pytest.mark.parametrize("fixture_path", INVALID_FIXTURES, ids=[f.stem for f in INVALID_FIXTURES])
def test_invalid_auth_hard_stop(fixture_path: Path) -> None:
    """Invalid fixtures produce expected strategy and hard stop."""
    data = load_fixture(fixture_path)
    warnings: list[str] = []
    hard_stops: list[str] = []
    result = detect_auth(data["_entries"], data["_transport"], warnings, hard_stops)
    assert result.strategy == data["_expected_strategy"]
    assert result.confidence == data["_expected_confidence"]
    assert any(data["_expected_hard_stop"] in hs for hs in hard_stops)


# =====================================================================
# Field extraction - fixture-driven
# =====================================================================


@pytest.mark.parametrize(
    "fixture_path",
    [f for f in VALID_FIXTURES if "_expected_fields" in load_fixture(f)],
    ids=[f.stem for f in VALID_FIXTURES if "_expected_fields" in load_fixture(f)],
)
def test_auth_field_extraction(fixture_path: Path) -> None:
    """Strategy-specific fields extracted correctly."""
    data = load_fixture(fixture_path)
    result = detect_auth(data["_entries"], data["_transport"], [], [])
    for key, value in data["_expected_fields"].items():
        assert key in result.fields, f"Missing auth field: {key}"
        assert result.fields[key] == value, f"Auth field {key}: expected {value!r}, got {result.fields[key]!r}"


# =====================================================================
# Warnings - fixture-driven
# =====================================================================


@pytest.mark.parametrize(
    "fixture_path",
    [f for f in VALID_FIXTURES if "_expected_warning" in load_fixture(f)],
    ids=[f.stem for f in VALID_FIXTURES if "_expected_warning" in load_fixture(f)],
)
def test_auth_expected_warnings(fixture_path: Path) -> None:
    """Fixtures with _expected_warning produce the expected warning."""
    data = load_fixture(fixture_path)
    warnings: list[str] = []
    detect_auth(data["_entries"], data["_transport"], warnings, [])
    expected = data["_expected_warning"]
    assert any(expected in w for w in warnings), f"Expected warning containing {expected!r}, got {warnings}"


# =====================================================================
# Edge cases - pure function behavioral tests (no HAR data)
# =====================================================================


class TestAuthUtilityEdgeCases:
    """Edge cases for auth utility functions (scalar inputs, no HAR dicts)."""

    def test_url_token_no_match_returns_none(self) -> None:
        """URL without login_ prefix returns no url_token match."""
        assert _extract_url_token_parts("/status.html?session=abc") is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://h/cmconnectionstatus.html?login_YWRtaW46cGFzcw==", ("login_", "/cmconnectionstatus.html")),
        ("http://h/status.html?login%5fYWRtaW46cGFzcw==", ("login_", "/status.html")),
        # The marker must carry a token, and must sit in the query string. A
        # path segment that merely spells "login_" is a script or file name.
        ("https://h/cgi-bin/login_cgi", None),
        ("https://h/Admin_Login_Lock.txt?_=1779637456025", None),
        ("https://h/login_page.html?x=1", None),
        ("http://h/status.html?login_", None),
    ],
    ids=[
        "query_token",
        "query_token_percent_encoded",
        "path_segment_login_cgi",
        "path_segment_admin_login_lock",
        "path_segment_with_query",
        "marker_without_token",
    ],
)
def test_url_token_parts_requires_token_in_query(url: str, expected: tuple[str, str] | None) -> None:
    """Only a query-string marker followed by a token is url_token auth."""
    assert _extract_url_token_parts(url) == expected


# =====================================================================
# Serialization - inline behavioral
# =====================================================================


class TestAuthDetailSerialization:
    """AuthDetail.to_dict() produces expected output."""

    def test_to_dict(self) -> None:
        """Serialization includes strategy, fields, and confidence."""
        detail = AuthDetail(strategy="form", fields={"action": "/login"}, confidence="high")
        d = detail.to_dict()
        assert d["strategy"] == "form"
        assert d["fields"]["action"] == "/login"
        assert d["confidence"] == "high"


# =====================================================================
# Auth scheme parsing - table-driven
# =====================================================================

# fmt: off
AUTH_SCHEME_CASES = [
    # (www_authenticate,                                       expected, desc)
    ('Basic realm="modem"',                                    "basic",  "basic with realm"),
    ('Digest realm="modem", nonce="abc", qop="auth"',          "digest", "digest with params"),
    ("Basic",                                                  "basic",  "bare basic"),
    ("Digest",                                                 "digest", "bare digest"),
    ("bearer token=xyz",                                       "bearer", "bearer token"),
    ("",                                                       "",       "empty header"),
    ("   Basic   ",                                            "basic",  "whitespace padded"),
    ("BASIC realm=modem",                                      "basic",  "uppercase basic"),
    ("DIGEST realm=modem",                                     "digest", "uppercase digest"),
]
# fmt: on


@pytest.mark.parametrize(
    "www_authenticate,expected,desc",
    AUTH_SCHEME_CASES,
    ids=[c[2] for c in AUTH_SCHEME_CASES],
)
def test_parse_auth_scheme(www_authenticate: str, expected: str, desc: str) -> None:
    """WWW-Authenticate header scheme parsed per RFC 7235."""
    assert _parse_auth_scheme(www_authenticate) == expected


# =====================================================================
# Utility function tests - table-driven
# =====================================================================

# ┌──────────────────────────────┬──────────┬──────────────────────────┐
# │ url                          │ expected │ description              │
# ├──────────────────────────────┼──────────┼──────────────────────────┤
# │ /goform/login                │ True     │ goform login endpoint    │
# │ /cgi-bin/auth                │ True     │ cgi-bin endpoint         │
# │ /api/v1/session/login        │ True     │ API session endpoint     │
# │ /LOGIN                       │ True     │ case insensitive         │
# │ /check.jst                   │ True     │ jst login page           │
# │ /goform/GenieLogin           │ True     │ genie login              │
# │ /goform/home                 │ True     │ goform home login        │
# │ /php/ajaxSet_Password.php    │ True     │ php ajax login           │
# │ /setup.cgi                   │ True     │ setup cgi login          │
# │ /status.html                 │ False    │ data page                │
# │ /info.html                   │ False    │ info page                │
# │ /                            │ False    │ root path                │
# └──────────────────────────────┴──────────┴──────────────────────────┘

# fmt: off
LOGIN_URL_CASES = [
    ("/goform/login",           True,  "goform login"),
    ("/cgi-bin/auth",           True,  "cgi-bin endpoint"),
    ("/api/v1/session/login",   True,  "API session endpoint"),
    ("/LOGIN",                  True,  "case insensitive"),
    ("/check.jst",              True,  "jst login page"),
    ("/goform/GenieLogin",      True,  "genie login"),
    ("/goform/home",            True,  "goform home login"),
    ("/php/ajaxSet_Password.php", True, "php ajax login"),
    ("/setup.cgi",              True,  "setup cgi login"),
    ("/status.html",            False, "data page"),
    ("/info.html",              False, "info page"),
    ("/",                       False, "root path"),
]
# fmt: on


@pytest.mark.parametrize(
    "url,expected,desc",
    LOGIN_URL_CASES,
    ids=[c[2] for c in LOGIN_URL_CASES],
)
def test_is_login_url(url: str, expected: bool, desc: str) -> None:
    """Login URL detection matches expected patterns."""
    assert _is_login_url(url) == expected


# ┌──────────────────────────────────────┬──────────────┬────────────┐
# │ url                                  │ expected     │ description│
# ├──────────────────────────────────────┼──────────────┼────────────┤
# │ http://host/path                     │ /path        │ full URL   │
# │ /relative/path                       │ /relative... │ relative   │
# │ http://host/path?query=1             │ /path        │ with query │
# │ /path?query=1                        │ /path        │ rel+query  │
# │ http://host/                         │ /            │ root       │
# │ http://host                          │ /            │ no path    │
# └──────────────────────────────────────┴──────────────┴────────────┘

# fmt: off
PATH_CASES = [
    ("http://host/path",           "/path",           "full URL"),
    ("/relative/path",             "/relative/path",  "relative"),
    ("http://host/path?query=1",   "/path",           "with query"),
    ("/path?query=1",              "/path",           "rel with query"),
    ("http://host/",               "/",               "root"),
    ("http://host",                "/",               "no path"),
]
# fmt: on


@pytest.mark.parametrize(
    "url,expected,desc",
    PATH_CASES,
    ids=[c[2] for c in PATH_CASES],
)
def test_path_from_url(url: str, expected: str, desc: str) -> None:
    """Path extraction from URL works for all forms."""
    assert path_from_url(url) == expected


class TestParseFormParams:
    """Form parameter parsing from HAR postData."""

    def test_from_params_array(self) -> None:
        """Parses structured params array."""
        post_data = {
            "params": [
                {"name": "user", "value": "admin"},
                {"name": "pass", "value": "secret"},
            ]
        }
        assert parse_form_params(post_data) == {
            "user": "admin",
            "pass": "secret",
        }

    def test_from_text(self) -> None:
        """Parses URL-encoded text fallback."""
        post_data = {"text": "user=admin&pass=secret"}
        assert parse_form_params(post_data) == {
            "user": "admin",
            "pass": "secret",
        }

    def test_empty(self) -> None:
        """Empty postData returns empty dict."""
        assert parse_form_params({}) == {}


class TestClassifyFormFields:
    """Form field classification into username, password, hidden."""

    def test_explicit_fields(self) -> None:
        """Identifies username and password fields by name."""
        params = {
            "loginUsername": "admin",
            "loginPassword": "pass",
            "webToken": "",
        }
        user, pwd, hidden = classify_form_fields(params)
        assert user == "loginUsername"
        assert pwd == "loginPassword"
        assert hidden == {"webToken": ""}

    def test_defaults(self) -> None:
        """Defaults to 'username' and 'password' when no match."""
        params = {"field1": "val1", "field2": "val2"}
        user, pwd, hidden = classify_form_fields(params)
        assert user == "username"
        assert pwd == "password"
        assert hidden == {"field1": "val1", "field2": "val2"}

    def test_pws_is_a_password_field(self) -> None:
        """Sercomm-style 'pws' field classified as the password."""
        params = {"login_user": "admin", "pws": "c3dvcmRmaXNo"}
        user, pwd, hidden = classify_form_fields(params)
        assert user == "login_user"
        assert pwd == "pws"
        assert hidden == {}

    def test_first_credential_field_wins(self) -> None:
        """First password-shaped field is the password; later ones are not demoted to hidden."""
        params = {
            "login_user": "admin",
            "pws": "c3dvcmRmaXNo",
            "todo": "login",
            "passwd": "c3dvcmRmaXNo",
            "cur_passwd": "",
        }
        user, pwd, hidden = classify_form_fields(params)
        assert user == "login_user"
        assert pwd == "pws"
        assert hidden == {"todo": "login"}


class TestHasCredentialFields:
    """Credential field detection in POST data."""

    def test_params_with_password(self) -> None:
        """Detects password field in structured params."""
        post_data = {"params": [{"name": "loginPassword", "value": "secret"}]}
        assert has_credential_fields(post_data) is True

    def test_params_without_credentials(self) -> None:
        """No credential fields in non-auth form."""
        post_data = {"params": [{"name": "action", "value": "reboot"}]}
        assert has_credential_fields(post_data) is False

    def test_text_with_password(self) -> None:
        """Detects password in URL-encoded text fallback."""
        post_data = {"text": "username=admin&password=secret"}
        assert has_credential_fields(post_data) is True

    def test_params_with_pws(self) -> None:
        """Detects Sercomm-style 'pws' password field."""
        post_data = {"params": [{"name": "pws", "value": "c3dvcmRmaXNo"}]}
        assert has_credential_fields(post_data) is True

    def test_empty(self) -> None:
        """Empty postData returns False."""
        assert has_credential_fields({}) is False


class TestFleetDerivedPasswordFields:
    """Committed catalog password_field names extend detection without a pattern edit."""

    @pytest.fixture()
    def novel_catalog(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Temp catalog with one modem whose password field matches no static substring."""
        from solentlabs import cable_modem_monitor_catalog as catalog_pkg

        modem_dir = tmp_path / "acme" / "a100"
        modem_dir.mkdir(parents=True)
        (modem_dir / "modem.yaml").write_text(
            "auth:\n  strategy: form\n  password_field: geheimcode\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(catalog_pkg, "CATALOG_PATH", tmp_path)
        _fleet_password_field_names.cache_clear()
        yield
        _fleet_password_field_names.cache_clear()

    def test_exact_catalog_name_recognized(self, novel_catalog: None) -> None:
        """A committed exact name is a password field."""
        assert is_password_field_name("geheimcode") is True

    def test_catalog_names_match_exactly_not_as_substrings(self, novel_catalog: None) -> None:
        """Catalog-derived names never generalize; only curated substrings do."""
        assert is_password_field_name("geheimcode2") is False

    def test_static_substrings_still_apply(self, novel_catalog: None) -> None:
        """Curated substrings work regardless of catalog contents."""
        assert is_password_field_name("loginPassword") is True

    def test_has_credential_fields_uses_catalog_names(self, novel_catalog: None) -> None:
        """The login discriminator accepts a POST keyed by a committed exact name."""
        post_data = {"params": [{"name": "geheimcode", "value": "secret"}]}
        assert has_credential_fields(post_data) is True

    def test_classify_uses_catalog_names(self, novel_catalog: None) -> None:
        """A committed exact name classifies as the password, never as a hidden field."""
        user, pwd, hidden = classify_form_fields({"user": "admin", "geheimcode": "secret"})
        assert pwd == "geheimcode"
        assert hidden == {}

    def test_real_catalog_supplies_names(self) -> None:
        """The real catalog scan yields the committed dm1000 name."""
        _fleet_password_field_names.cache_clear()
        assert "pws" in get_fleet_password_field_names()


# -----------------------------------------------------------------------
# Core gap detection — unmatched credential POSTs
# -----------------------------------------------------------------------
#
# ┌───────────────────────┬──────────┬───────────────────────────────┐
# │ url_path              │ gap?     │ description                   │
# ├───────────────────────┼──────────┼───────────────────────────────┤
# │ /custom/auth_endpoint │ True     │ unrecognized URL → core gap   │
# │ /goform/login         │ False    │ known pattern → no gap        │
# └───────────────────────┴──────────┴───────────────────────────────┘
#
# fmt: off
CREDENTIAL_GAP_CASES = [
    # (url_path,               expect_gap, description)
    ("/custom/auth_endpoint",  True,       "unrecognized URL produces core gap"),
    ("/goform/login",          False,      "known login pattern produces no gap"),
]
# fmt: on


def _build_credential_post_entry(url_path: str) -> dict:
    """Build a minimal HAR entry with a credential POST to the given path."""
    return {
        "request": {
            "method": "POST",
            "url": f"http://192.168.0.1{url_path}",
            "headers": [],
            "postData": {
                "mimeType": "application/x-www-form-urlencoded",
                "params": [
                    {"name": "username", "value": "admin"},
                    {"name": "password", "value": "secret"},
                ],
            },
        },
        "response": {"status": 200, "headers": []},
    }


@pytest.mark.parametrize("url_path,expect_gap,desc", CREDENTIAL_GAP_CASES, ids=[c[2] for c in CREDENTIAL_GAP_CASES])
def test_credential_post_gap_detection(url_path: str, expect_gap: bool, desc: str) -> None:
    """Credential POST to unrecognized URL produces a core gap."""
    from solentlabs.cable_modem_monitor_catalog_tools.analysis.types import CoreGap

    entries = [_build_credential_post_entry(url_path)]
    core_gaps: list[CoreGap] = []
    detect_auth(entries, "http", [], [], core_gaps)
    login_gaps = [g for g in core_gaps if g.category == "unmatched_login"]
    if expect_gap:
        assert len(login_gaps) == 1
        assert login_gaps[0].phase == "auth"
        assert login_gaps[0].evidence["endpoint"] == url_path
    else:
        assert len(login_gaps) == 0


class TestDetectEncoding:
    """Password encoding detection (base64 vs plain)."""

    def test_base64_value(self) -> None:
        """Valid base64-encoded password detected."""
        assert detect_encoding({"password": "YWRtaW4="}, "password") == "base64"

    def test_plain_value(self) -> None:
        """Plain text password detected."""
        assert detect_encoding({"password": "admin"}, "password") == "plain"

    def test_empty_value(self) -> None:
        """Empty password defaults to plain."""
        assert detect_encoding({"password": ""}, "password") == "plain"

    def test_missing_field(self) -> None:
        """Missing field defaults to plain."""
        assert detect_encoding({}, "password") == "plain"

    def test_base64encode_call_on_login_page(self) -> None:
        """Redacted POST value falls back to the login page's base64encode() call."""
        html = "<script>document.tF.passwd.value = base64encode(document.tF.pws.value);</script>"
        assert detect_encoding({"pws": "[REDACTED]"}, "pws", html) == "base64"


# =====================================================================
# _HttpAuthSignals.describe() - table-driven
# =====================================================================


class TestHttpAuthSignalsDescribe:
    """Tests for describe() method with various signal combinations."""

    def test_digest_challenge_signal(self) -> None:
        """Digest challenge shows in description."""
        signals = _HttpAuthSignals(digest_challenge=True, has_any_auth_signal=True)
        assert "WWW-Authenticate: Digest" in signals.describe()

    def test_401_signal(self) -> None:
        """401 response shows in description."""
        signals = _HttpAuthSignals(has_401=True, has_any_auth_signal=True)
        assert "401 response" in signals.describe()

    def test_authorization_header_signal(self) -> None:
        """Authorization header shows in description."""
        signals = _HttpAuthSignals(has_authorization_header=True, has_any_auth_signal=True)
        assert "Authorization header" in signals.describe()

    def test_form_post_signal(self) -> None:
        """Form POST shows endpoint path in description."""
        signals = _HttpAuthSignals(
            form_post_entry={
                "request": {"url": "http://192.168.100.1/goform/login", "method": "POST"},
                "response": {"status": 200},
            },
            has_any_auth_signal=True,
        )
        desc = signals.describe()
        assert "POST to /goform/login" in desc

    def test_set_cookie_signal(self) -> None:
        """Set-Cookie after login shows in description."""
        signals = _HttpAuthSignals(has_set_cookie_after_login=True, has_any_auth_signal=True)
        assert "Set-Cookie after login" in signals.describe()

    def test_no_signals_returns_ambiguous(self) -> None:
        """No specific signals returns ambiguous message."""
        signals = _HttpAuthSignals(has_any_auth_signal=True)
        assert signals.describe() == "ambiguous auth artifacts"


# =====================================================================
# HNAP auth edge cases
# =====================================================================


class TestHnapAuthEdgeCases:
    """Edge cases for HNAP auth detection."""

    def test_whitespace_only_hnap_auth_header(self) -> None:
        """Whitespace-only HNAP_AUTH header is treated as absent."""
        entries = [
            {
                "request": {
                    "url": "http://192.168.100.1/HNAP1/",
                    "method": "POST",
                    "headers": [{"name": "HNAP_AUTH", "value": "   "}],
                },
                "response": {"status": 200},
            }
        ]
        result = _detect_hmac_algorithm(entries)
        assert result is None


# =====================================================================
# form_pbkdf2 empty guard
# =====================================================================


class TestFormPbkdf2EmptyGuard:
    """Edge case: empty pbkdf2_entries returns minimal detail."""

    def test_empty_pbkdf2_entries(self) -> None:
        """Empty pbkdf2 entries returns medium confidence without fields."""
        signals = _HttpAuthSignals(pbkdf2_entries=[], has_any_auth_signal=True)
        result = _extract_form_pbkdf2(signals)
        assert result.strategy == "form_pbkdf2"
        assert result.confidence == "medium"
        assert not result.fields


# =====================================================================
# Dynamic form action (?id= query on the login POST)
# =====================================================================


def _dynamic_action_entries(*, with_login_page: bool = True) -> list[dict[str, Any]]:
    """Login POST whose URL carries a per-session query token."""
    login_page = {
        "request": {"method": "GET", "url": "http://host/", "headers": []},
        "response": {
            "status": 200,
            "headers": [],
            "content": {
                "mimeType": "text/html",
                "text": '<form action="/goform/Login?id=487192871">'
                '<input type="text" name="loginName">'
                '<input type="password" name="loginPassword"></form>',
            },
        },
    }
    login_post = {
        "request": {
            "method": "POST",
            "url": "http://host/goform/Login?id=487192871",
            "headers": [{"name": "Content-Type", "value": "application/x-www-form-urlencoded"}],
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
    }
    return [login_page, login_post] if with_login_page else [login_post]


class TestDynamicFormAction:
    """A query on the login POST is a per-session token, never config.

    The bare path stays in ``action``; whether ``action_source:
    login_page`` can be emitted depends on the installed Core's FormAuth
    (#189), so both capability states are pinned here via monkeypatch.
    """

    def test_emits_action_source_when_core_supports_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Core with action_source: emitted, confidence stays high."""
        from solentlabs.cable_modem_monitor_catalog_tools.analysis.auth import http as auth_http

        monkeypatch.setattr(auth_http, "_core_supports_action_source", lambda: True)
        warnings: list[str] = []
        result = detect_auth(_dynamic_action_entries(), "http", warnings, [], [])
        assert result.strategy == "form"
        assert result.fields["action"] == "/goform/Login"
        assert result.fields["action_source"] == "login_page"
        assert result.confidence == "high"

    def test_warns_and_downgrades_without_core_support(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Core without action_source: no field, loud warning, medium confidence."""
        from solentlabs.cable_modem_monitor_catalog_tools.analysis.auth import http as auth_http

        monkeypatch.setattr(auth_http, "_core_supports_action_source", lambda: False)
        warnings: list[str] = []
        result = detect_auth(_dynamic_action_entries(), "http", warnings, [], [])
        assert result.strategy == "form"
        assert result.fields["action"] == "/goform/Login"
        assert "action_source" not in result.fields
        assert result.confidence == "medium"
        assert any("query" in w and "action_source" in w for w in warnings)

    def test_warns_when_login_page_not_captured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """action_source needs login_page; without one the token is unreachable."""
        from solentlabs.cable_modem_monitor_catalog_tools.analysis.auth import http as auth_http

        monkeypatch.setattr(auth_http, "_core_supports_action_source", lambda: True)
        warnings: list[str] = []
        result = detect_auth(_dynamic_action_entries(with_login_page=False), "http", warnings, [], [])
        assert result.strategy == "form"
        assert "action_source" not in result.fields
        assert result.confidence == "medium"
        assert any("login page" in w.lower() for w in warnings)

    def test_static_action_unaffected(self) -> None:
        """A query-free login POST emits neither field nor warning."""
        entries = _dynamic_action_entries()
        for entry in entries:
            entry["request"]["url"] = entry["request"]["url"].split("?", 1)[0]
        content = entries[0]["response"]["content"]
        content["text"] = content["text"].replace("?id=487192871", "")
        warnings: list[str] = []
        result = detect_auth(entries, "http", warnings, [], [])
        assert result.strategy == "form"
        assert "action_source" not in result.fields
        assert result.confidence == "high"
        assert not any("action_source" in w for w in warnings)
