"""Tests for modem.yaml Pydantic model.

Valid and invalid configs are stored as JSON fixtures in
tests/models/fixtures/modem_config/{valid,invalid}/.

Valid fixtures are complete modem.yaml-shaped dicts that must parse
without error. Invalid fixtures have `_config` (the bad input) and
`_expected_error` (the regex match for the expected ValidationError).

Behavioral tests verify specific field values, defaults, and access
patterns by loading named fixtures and checking parsed results.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from solentlabs.cable_modem_monitor_core.models.modem_config import ModemConfig
from solentlabs.cable_modem_monitor_core.models.modem_config.actions import CbnAction, HttpAction
from solentlabs.cable_modem_monitor_core.models.modem_config.session import SessionConfig

from tests._helpers import collect_fixtures, load_fixture

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "modem_config"
VALID_DIR = FIXTURES_DIR / "valid"
INVALID_DIR = FIXTURES_DIR / "invalid"


def _load(name: str) -> ModemConfig:
    """Load and parse a valid modem config fixture by name."""
    return ModemConfig.model_validate(load_fixture(VALID_DIR / name))


# ---------------------------------------------------------------------------
# Valid configs — each fixture file must parse without error
# ---------------------------------------------------------------------------

VALID_FIXTURES = collect_fixtures(VALID_DIR)


@pytest.mark.parametrize(
    "fixture_path",
    VALID_FIXTURES,
    ids=[f.stem for f in VALID_FIXTURES],
)
def test_valid_modem_config(fixture_path: Path):
    """Valid fixture parses without error."""
    config = ModemConfig.model_validate(load_fixture(fixture_path))
    assert config.manufacturer
    assert config.model
    assert config.transport in ("http", "hnap", "cbn")


# ---------------------------------------------------------------------------
# Invalid configs — each fixture file must raise ValidationError
# ---------------------------------------------------------------------------

INVALID_FIXTURES = collect_fixtures(INVALID_DIR)


@pytest.mark.parametrize(
    "fixture_path",
    INVALID_FIXTURES,
    ids=[f.stem for f in INVALID_FIXTURES],
)
def test_invalid_modem_config(fixture_path: Path):
    """Invalid fixture raises ValidationError with expected message."""
    raw = load_fixture(fixture_path)
    with pytest.raises(ValidationError, match=raw["_expected_error"]):
        ModemConfig.model_validate(raw["_config"])


# ---------------------------------------------------------------------------
# Behavioral: field access (table-driven where possible)
# ---------------------------------------------------------------------------

# ┌──────────────────────────┬─────────────────────────────┬────────────────┐
# │ fixture                  │ attribute path              │ expected       │
# ├──────────────────────────┼─────────────────────────────┼────────────────┤
# │ auth_none.json           │ timeout                     │ 10             │
# │ with_optional_identity   │ timeout                     │ 20             │
# │ with_optional_identity   │ model_aliases               │ ["T1100v2"]    │
# │ with_optional_identity   │ brands                      │ ["TestBrand"]  │
# │ with_optional_identity   │ notes                       │ "Test notes"   │
# │ auth_form.json           │ auth.encoding               │ "base64"       │
# │ auth_url_token.json      │ auth.ajax_login             │ True           │
# │ auth_url_token.json      │ auth.token_prefix           │ "ct_"          │
# │ auth_form_pbkdf2.json    │ auth.pbkdf2_iterations      │ 1000           │
# │ auth_form_pbkdf2.json    │ session.post_login_endpoi.. │ ["/establi..]  │
# │ auth_form_sjcl.json      │ session.post_login_endpoi.. │ [] (default)   │
# │ auth_form_nonce.json     │ auth.nonce_length           │ 8              │
# │ auth_basic.json          │ auth.challenge_cookie       │ False          │
# │ auth_basic_challenge_..  │ auth.challenge_cookie       │ True           │
# │ auth_hnap.json           │ auth.hmac_algorithm         │ "md5"          │
# │ auth_hnap_sha256.json    │ auth.hmac_algorithm         │ "sha256"       │
# │ health_config.json       │ health.http_probe           │ False          │
# │ health_config.json       │ health.supports_head        │ False          │
# │ health_config.json       │ health.supports_icmp        │ False          │
# └──────────────────────────┴─────────────────────────────┴────────────────┘

# fmt: off
FIELD_ACCESS_CASES = [
    # (fixture,                          attr_path,                 expected)
    ("auth_none.json",                   "timeout",                 10),
    ("with_optional_identity.json",      "timeout",                 20),
    ("with_optional_identity.json",      "model_aliases",           ["T1100v2"]),
    ("with_optional_identity.json",      "brands",                  ["TestBrand"]),
    ("with_optional_identity.json",      "notes",                   "Test notes"),
    ("auth_form.json",                   "auth.encoding",           "base64"),
    ("auth_url_token.json",              "auth.ajax_login",         True),
    ("auth_url_token.json",              "auth.token_prefix",       "ct_"),
    ("auth_form_pbkdf2.json",            "auth.pbkdf2_iterations",  1000),
    ("auth_form_pbkdf2.json",            "auth.csrf_header",        "X-CSRF-TOKEN"),
    ("auth_form_pbkdf2.json",            "session.post_login_endpoints", ["/establish.html"]),
    ("auth_form_sjcl.json",              "session.post_login_endpoints", []),
    ("auth_form_nonce.json",             "auth.nonce_length",       8),
    ("auth_form_nonce.json",             "auth.success_prefix",     "Url:"),
    ("auth_basic.json",                  "auth.challenge_cookie",   False),
    ("auth_basic_challenge_cookie.json", "auth.challenge_cookie",   True),
    ("auth_hnap.json",                   "auth.hmac_algorithm",     "md5"),
    ("auth_hnap_sha256.json",            "auth.hmac_algorithm",     "sha256"),
    ("auth_form_pbkdf2.json",            "auth.login_success",      {"error": "ok"}),
    ("auth_form_pbkdf2.json",            "auth.login_busy",         {"message": "MSG_LOGIN_150"}),
    ("health_config.json",               "health.http_probe",       False),
    ("health_config.json",               "health.supports_head",    False),
    ("health_config.json",               "health.supports_icmp",    False),
    ("auth_form_cbn.json",               "auth.strategy",           "form_cbn"),
    ("auth_form_cbn.json",               "auth.login_fun",          15),
    ("auth_form_cbn.json",               "auth.getter_endpoint",    "/xml/getter.xml"),
    ("auth_form_cbn.json",               "auth.setter_endpoint",    "/xml/setter.xml"),
    ("auth_form_cbn.json",               "auth.session_cookie_name", "sessionToken"),
    ("auth_form_cbn.json",               "auth.username_value",     "NULL"),
]
# fmt: on


def _resolve_attr(obj: object, path: str) -> object:
    """Resolve a dotted attribute path like 'auth.encoding'."""
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


@pytest.mark.parametrize(
    "fixture,attr_path,expected",
    FIELD_ACCESS_CASES,
    ids=[f"{c[0].removesuffix('.json')}:{c[1]}" for c in FIELD_ACCESS_CASES],
)
def test_field_access(fixture, attr_path, expected):
    """Parsed config field matches expected value."""
    config = _load(fixture)
    assert _resolve_attr(config, attr_path) == expected


# ---------------------------------------------------------------------------
# Behavioral: multi-field relationships (not table-driven)
# ---------------------------------------------------------------------------


class TestRelationships:
    """Tests for multi-field relationships that don't fit a flat table."""

    def test_references_issues_and_prs(self):
        """References section contains both issues and PRs."""
        config = _load("with_optional_identity.json")
        assert config.references is not None
        assert config.references.issues == ["ref-1", "ref-2"]
        assert config.references.prs == ["ref-3"]

    def test_sources_freeform(self):
        """Sources is a freeform string dict."""
        config = _load("with_optional_identity.json")
        assert config.sources["auth_config"] == "#42"

    def test_unsupported_omits_auth_and_hardware(self):
        """Unsupported status allows omitting auth and hardware."""
        config = _load("status_unsupported.json")
        assert config.auth is None
        assert config.hardware is None

    def test_health_defaults_when_omitted(self):
        """Health section omitted defaults all probes to True."""
        config = _load("auth_none.json")
        assert config.health is None

    def test_health_explicit_values(self):
        """Health section with explicit values overrides defaults."""
        config = _load("health_config.json")
        assert config.health is not None
        assert config.health.http_probe is False
        assert config.health.supports_head is False
        assert config.health.supports_icmp is False

    def test_form_logout_action(self):
        """Form auth with logout action configured."""
        config = _load("auth_form.json")
        assert config.actions is not None
        assert config.actions.logout is not None
        assert isinstance(config.actions.logout, HttpAction)
        assert config.actions.logout.endpoint == "/logout.asp"
        assert config.actions.logout.requires_session is False

    def test_cbn_transport_with_actions(self):
        """CBN transport with cbn-typed actions."""
        config = _load("auth_form_cbn.json")
        assert config.transport == "cbn"
        assert config.actions is not None
        assert isinstance(config.actions.logout, CbnAction)
        assert config.actions.logout.fun == 16
        assert isinstance(config.actions.restart, CbnAction)
        assert config.actions.restart.fun == 8


# Each row: (input headers, base_url, expected output, description-id)
_RESOLVED_HEADERS_CASES: list[tuple[dict[str, str], str, dict[str, str], str]] = [
    (
        {"Referer": "{base_url}/", "Origin": "{base_url}"},
        "http://192.168.0.1",
        {"Referer": "http://192.168.0.1/", "Origin": "http://192.168.0.1"},
        "substitutes {base_url}",
    ),
    (
        {"X-Requested-With": "XMLHttpRequest"},
        "http://192.168.0.1",
        {"X-Requested-With": "XMLHttpRequest"},
        "passes through static values",
    ),
    ({}, "http://x", {}, "empty headers"),
]


@pytest.mark.parametrize(
    "headers,base_url,expected,desc",
    _RESOLVED_HEADERS_CASES,
    ids=[c[3] for c in _RESOLVED_HEADERS_CASES],
)
def test_session_resolved_headers(headers: dict[str, str], base_url: str, expected: dict[str, str], desc: str) -> None:
    """SessionConfig.resolved_headers substitutes {base_url}; static values pass through."""
    assert SessionConfig(headers=headers).resolved_headers(base_url=base_url) == expected
