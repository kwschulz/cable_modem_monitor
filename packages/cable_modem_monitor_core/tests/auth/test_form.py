"""Tests for FormAuthManager."""

from __future__ import annotations

import base64
import logging
from unittest.mock import patch

import pytest
import requests
from solentlabs.cable_modem_monitor_core.auth.form import (
    FormAuthManager,
    _check_success,
    _discover_hidden_fields,
    _encode_password,
    _login_action_from_page,
)
from solentlabs.cable_modem_monitor_core.models.modem_config.auth import (
    FormAuth,
    FormSuccess,
)
from solentlabs.cable_modem_monitor_core.test_harness import HARMockServer

from .conftest import load_auth_fixture


class TestEncodePassword:
    """Password encoding utility."""

    def test_plain_encoding(self) -> None:
        """Plain encoding returns password as-is."""
        assert _encode_password("secret", "plain") == "secret"

    def test_base64_encoding(self) -> None:
        """Base64 encoding returns base64-encoded password."""
        result = _encode_password("secret", "base64")
        assert result == base64.b64encode(b"secret").decode("ascii")

    def test_empty_password(self) -> None:
        """Empty password works for both encodings."""
        assert _encode_password("", "plain") == ""
        assert _encode_password("", "base64") == base64.b64encode(b"").decode()


class TestFormAuthManager:
    """FormAuthManager executes form POST login."""

    def test_basic_form_login(self, session: requests.Session) -> None:
        """Successful form login against mock server."""
        entries, modem_config = load_auth_fixture("har_form_login.json")

        with HARMockServer(entries, modem_config=modem_config) as server:
            config = FormAuth(
                strategy="form",
                action="/goform/login",
            )
            manager = FormAuthManager(config)
            manager.configure_session(session, {})

            result = manager.authenticate(session, server.base_url, "admin", "pw")
            assert result.success is True
            assert result.response is not None

    def test_base64_encoded_password(self, session: requests.Session) -> None:
        """Password is base64-encoded before POST."""
        entries, modem_config = load_auth_fixture("har_form_login.json")

        with HARMockServer(entries, modem_config=modem_config) as server:
            config = FormAuth(
                strategy="form",
                action="/goform/login",
                encoding="base64",
            )
            manager = FormAuthManager(config)
            manager.configure_session(session, {})

            result = manager.authenticate(session, server.base_url, "admin", "pw")
            assert result.success is True

    def test_password_field_list(self, session: requests.Session) -> None:
        """password_field as list sends encoded password to all fields."""
        entries, modem_config = load_auth_fixture("har_form_login.json")

        with HARMockServer(entries, modem_config=modem_config) as server:
            config = FormAuth(
                strategy="form",
                action="/goform/login",
                encoding="base64",
                password_field=["pws", "passwd"],
            )
            manager = FormAuthManager(config)
            manager.configure_session(session, {})

            result = manager.authenticate(session, server.base_url, "admin", "pw")
            assert result.success is True

    def test_password_field_string_normalized(self, session: requests.Session) -> None:
        """password_field as string is normalized to single-element list."""
        entries, modem_config = load_auth_fixture("har_form_login.json")

        with HARMockServer(entries, modem_config=modem_config) as server:
            config = FormAuth(
                strategy="form",
                action="/goform/login",
                password_field="mypasswd",  # type: ignore[arg-type]  # validator normalizes str→list
            )
            assert config.password_field == ["mypasswd"]
            manager = FormAuthManager(config)
            manager.configure_session(session, {})

            result = manager.authenticate(session, server.base_url, "admin", "pw")
            assert result.success is True

    def test_success_redirect_check(self, session: requests.Session) -> None:
        """Success check via redirect URL matching."""
        entries, modem_config = load_auth_fixture("har_form_login.json")

        with HARMockServer(entries, modem_config=modem_config) as server:
            config = FormAuth(
                strategy="form",
                action="/goform/login",
                success=FormSuccess(redirect="/goform/login"),
            )
            manager = FormAuthManager(config)
            manager.configure_session(session, {})

            result = manager.authenticate(session, server.base_url, "admin", "pw")
            # The mock server doesn't redirect, so the final URL is the
            # login URL itself -- which contains "/goform/login". The
            # action must be the endpoint the fixture serves: it used to
            # be "/login", which the server answers 401, and the redirect
            # check passed anyway because declaring `success` skipped the
            # HTTP-error guard.
            assert result.success is True

    def test_success_indicator_present(self, session: requests.Session) -> None:
        """Success check via response body indicator."""
        entries, modem_config = load_auth_fixture("har_form_login_with_indicator.json")

        with HARMockServer(entries, modem_config=modem_config) as server:
            config = FormAuth(
                strategy="form",
                action="/login",
                success=FormSuccess(indicator="Welcome"),
            )
            manager = FormAuthManager(config)
            manager.configure_session(session, {})

            result = manager.authenticate(session, server.base_url, "admin", "pw")
            assert result.success is True

    def test_success_indicator_missing(self, session: requests.Session) -> None:
        """Failure when success indicator is not in response."""
        entries, modem_config = load_auth_fixture("har_form_login_error.json")

        with HARMockServer(entries, modem_config=modem_config) as server:
            config = FormAuth(
                strategy="form",
                action="/login",
                success=FormSuccess(indicator="Welcome"),
            )
            manager = FormAuthManager(config)
            manager.configure_session(session, {})

            result = manager.authenticate(session, server.base_url, "admin", "pw")
            assert result.success is False
            assert "indicator" in result.error

    def test_response_url_captured(self, session: requests.Session) -> None:
        """Auth response URL is captured for response reuse."""
        entries, modem_config = load_auth_fixture("har_form_login.json")

        with HARMockServer(entries, modem_config=modem_config) as server:
            config = FormAuth(
                strategy="form",
                action="/goform/login",
            )
            manager = FormAuthManager(config)
            manager.configure_session(session, {})

            result = manager.authenticate(session, server.base_url, "admin", "pw")
            assert result.success is True
            assert result.response_url == "/goform/login"

    def test_login_with_indicator(self, session: requests.Session) -> None:
        """Login with success indicator in response body."""
        entries, modem_config = load_auth_fixture("har_form_login_with_indicator.json")

        with HARMockServer(entries, modem_config=modem_config) as server:
            config = FormAuth(strategy="form", action="/login")
            manager = FormAuthManager(config)
            manager.configure_session(session, {})

            result = manager.authenticate(session, server.base_url, "admin", "pw")
            assert result.success is True

    def test_401_no_success_criteria(self, session: requests.Session) -> None:
        """401 response with no success criteria returns auth failure."""
        entries, modem_config = load_auth_fixture("har_form_login_401.json")

        with HARMockServer(entries, modem_config=modem_config) as server:
            config = FormAuth(strategy="form", action="/goform/login")
            manager = FormAuthManager(config)
            manager.configure_session(session, {})

            result = manager.authenticate(session, server.base_url, "admin", "pw")
            assert result.success is False
            assert "401" in result.error

    def test_server_error_no_success_criteria(self, session: requests.Session) -> None:
        """500 response with no success criteria returns auth failure."""
        entries, modem_config = load_auth_fixture("har_form_login_500.json")

        with HARMockServer(entries, modem_config=modem_config) as server:
            config = FormAuth(strategy="form", action="/goform/login")
            manager = FormAuthManager(config)
            manager.configure_session(session, {})

            result = manager.authenticate(session, server.base_url, "admin", "pw")
            assert result.success is False
            assert "500" in result.error

    def test_redirect_mismatch(self, session: requests.Session) -> None:
        """Redirect mismatch returns auth failure with path details."""
        entries, modem_config = load_auth_fixture("har_form_login_redirect.json")

        with HARMockServer(entries, modem_config=modem_config) as server:
            config = FormAuth(
                strategy="form",
                action="/goform/login",
                success=FormSuccess(redirect="/dashboard"),
            )
            manager = FormAuthManager(config)
            manager.configure_session(session, {})

            result = manager.authenticate(session, server.base_url, "admin", "pw")
            assert result.success is False
            assert "redirect mismatch" in result.error.lower()
            assert "/dashboard" in result.error


class TestHiddenFieldsAndCredentialRouting:
    """Explicit hidden_fields and password_field list in form POST."""

    def test_hidden_fields_included_in_post(self, session: requests.Session) -> None:
        """Static hidden_fields from config are sent in the POST."""
        entries, modem_config = load_auth_fixture("har_form_login.json")

        with HARMockServer(entries, modem_config=modem_config) as server:
            config = FormAuth(
                strategy="form",
                action="/goform/login",
                hidden_fields={"todo": "login", "language": "en"},
            )
            manager = FormAuthManager(config)
            manager.configure_session(session, {})

            result = manager.authenticate(session, server.base_url, "admin", "pw")
            assert result.success is True

    def test_password_field_list_with_hidden_fields(self, session: requests.Session) -> None:
        """password_field list populates before hidden_fields are merged."""
        entries, modem_config = load_auth_fixture("har_form_login.json")

        with HARMockServer(entries, modem_config=modem_config) as server:
            config = FormAuth(
                strategy="form",
                action="/goform/login",
                encoding="base64",
                password_field=["pws", "passwd"],
                hidden_fields={"cur_passwd": ""},
            )
            manager = FormAuthManager(config)
            manager.configure_session(session, {})

            result = manager.authenticate(session, server.base_url, "admin", "secret")
            assert result.success is True

    def test_login_page_prefetch_for_cookies(self, session: requests.Session) -> None:
        """login_page pre-fetch establishes cookies without parsing HTML."""
        entries, modem_config = load_auth_fixture("har_form_login_with_hidden_fields.json")

        with HARMockServer(entries, modem_config=modem_config) as server:
            config = FormAuth(
                strategy="form",
                action="/goform/login",
                login_page="/login.html",
                hidden_fields={"csrf_token": "abc123", "mode": "login"},
            )
            manager = FormAuthManager(config)
            manager.configure_session(session, {})

            result = manager.authenticate(session, server.base_url, "admin", "pw")
            assert result.success is True


# ---------------------------------------------------------------------------
# Network error paths — table-driven
# ---------------------------------------------------------------------------

# ┌──────────────────────────┬─────────────────────────────┬──────────────────────────┐
# │ scenario                 │ config                      │ expected error fragment  │
# ├──────────────────────────┼─────────────────────────────┼──────────────────────────┤
# │ login page prefetch fail │ login_page="/login.html"    │ "pre-fetch failed"       │
# │ login POST fail          │ no login_page               │ "POST failed"            │
# └──────────────────────────┴─────────────────────────────┴──────────────────────────┘

# fmt: off
NETWORK_ERROR_CASES = [
    # (description,               login_page,    mock_method, expected_error)
    ("login_page_prefetch_fail",  "/login.html", "get",       "pre-fetch failed"),
    ("login_post_fail",           "",            "request",   "POST failed"),
]
# fmt: on


@pytest.mark.parametrize(
    "desc,login_page,mock_method,expected_error",
    NETWORK_ERROR_CASES,
    ids=[c[0] for c in NETWORK_ERROR_CASES],
)
def test_network_error_propagates(
    session: requests.Session,
    desc: str,
    login_page: str,
    mock_method: str,
    expected_error: str,
) -> None:
    """ConnectionError propagates for collector to classify as CONNECTIVITY."""
    config = FormAuth(
        strategy="form",
        action="/goform/login",
        login_page=login_page,
    )
    manager = FormAuthManager(config)
    manager.configure_session(session, {})

    with (
        patch.object(
            session,
            mock_method,
            side_effect=requests.ConnectionError("refused"),
        ),
        pytest.raises(requests.ConnectionError),
    ):
        manager.authenticate(
            session,
            "http://192.168.100.1",
            "admin",
            "password",
        )


# ---------------------------------------------------------------------------
# _check_success fallback boundary — table-driven
# ---------------------------------------------------------------------------
# When config.success is None (no explicit criteria), the fallback
# rejects any HTTP status >= 400.
#
# ┌────────┬─────────┬───────────────────────────────┐
# │ status │ accept? │ description                   │
# ├────────┼─────────┼───────────────────────────────┤
# │ 200    │ ✓       │ normal OK                     │
# │ 301    │ ✓       │ permanent redirect            │
# │ 302    │ ✓       │ found (login redirect)        │
# │ 399    │ ✓       │ boundary — last accepted      │
# │ 400    │ ✗       │ boundary — first rejected     │
# │ 401    │ ✗       │ unauthorized                  │
# │ 403    │ ✗       │ forbidden                     │
# │ 404    │ ✗       │ not found                     │
# │ 500    │ ✗       │ internal server error         │
# │ 503    │ ✗       │ service unavailable           │
# └────────┴─────────┴───────────────────────────────┘

# fmt: off
CHECK_SUCCESS_FALLBACK_CASES = [
    # (status, should_accept, description)
    (200,  True,  "normal_ok"),
    (301,  True,  "permanent_redirect"),
    (302,  True,  "found_redirect"),
    (399,  True,  "boundary_last_accepted"),
    (400,  False, "boundary_first_rejected"),
    (401,  False, "unauthorized"),
    (403,  False, "forbidden"),
    (404,  False, "not_found"),
    (500,  False, "internal_server_error"),
    (503,  False, "service_unavailable"),
]
# fmt: on


def _make_response(status: int) -> requests.Response:
    """Build a minimal Response with the given status code."""
    resp = requests.Response()
    resp.status_code = status
    resp._content = b""
    return resp


@pytest.mark.parametrize(
    "status,should_accept,desc",
    CHECK_SUCCESS_FALLBACK_CASES,
    ids=[c[2] for c in CHECK_SUCCESS_FALLBACK_CASES],
)
def test_check_success_fallback_boundary(
    status: int,
    should_accept: bool,
    desc: str,
) -> None:
    """_check_success with no criteria rejects status >= 400."""
    config = FormAuth(strategy="form", action="/login")
    response = _make_response(status)
    error = _check_success(config, response)

    if should_accept:
        assert error == "", f"Status {status} should be accepted, got: {error}"
    else:
        assert error != "", f"Status {status} should be rejected"
        assert str(status) in error


# ---------------------------------------------------------------------------
# Hidden field discovery — unit tests
# ---------------------------------------------------------------------------

# Named HTML constants (no inline data blobs in test methods — rule 18)
_LOGIN_FORM_WITH_CSRF = (
    "<html><body>"
    "<form action='/goform/login' method='POST'>"
    "<input type='text' name='username' value=''>"
    "<input type='password' name='password' value=''>"
    "<input type='hidden' name='webToken' value='tok-9876'>"
    "<input type='hidden' name='mode' value='login'>"
    "</form>"
    "</body></html>"
)

_TWO_FORMS_PAGE = (
    "<html><body>"
    "<form id='search'><input type='hidden' name='q' value='x'></form>"
    "<form id='login'><input type='hidden' name='tok' value='abc'></form>"
    "</body></html>"
)

_NO_FORMS_PAGE = "<html><body><p>No forms here</p></body></html>"


DISCOVER_HIDDEN_FIELDS_CASES = [
    pytest.param(
        _LOGIN_FORM_WITH_CSRF,
        "",
        {"webToken": "tok-9876", "mode": "login"},
        id="first_form_fallback",
    ),
    pytest.param(
        _LOGIN_FORM_WITH_CSRF,
        "form[action='/goform/login']",
        {"webToken": "tok-9876", "mode": "login"},
        id="css_selector_targets_form",
    ),
    pytest.param(
        _TWO_FORMS_PAGE,
        "#login",
        {"tok": "abc"},
        id="selector_picks_correct_form",
    ),
    pytest.param("", "", {}, id="empty_html"),
    pytest.param(_NO_FORMS_PAGE, "", {}, id="no_hidden_inputs"),
]


@pytest.mark.parametrize("html,selector,expected", DISCOVER_HIDDEN_FIELDS_CASES)
def test_discover_hidden_fields(
    html: str,
    selector: str,
    expected: dict[str, str],
) -> None:
    """_discover_hidden_fields reads only type=hidden inputs from the form."""
    assert _discover_hidden_fields(html, selector) == expected


# ---------------------------------------------------------------------------
# Hidden field discovery — merge behavior (integration)
# ---------------------------------------------------------------------------

_DISCOVER_PATCH = "solentlabs.cable_modem_monitor_core.auth.form._discover_hidden_fields"

MERGE_BEHAVIOR_CASES = [
    pytest.param(
        {},
        {"csrf_token": "abc123", "mode": "login"},
        {"csrf_token": "abc123", "mode": "login"},
        id="discovered_fields_included",
    ),
    pytest.param(
        {"mode": "override"},
        {"csrf_token": "abc123", "mode": "login"},
        {"csrf_token": "abc123", "mode": "override"},
        id="static_overrides_discovered",
    ),
    pytest.param(
        {},
        {},
        {},
        id="empty_discovery_no_effect",
    ),
    # Credentials are written last, so they win a key collision from either
    # source. A modem.yaml or a login page that happens to name a field
    # "username" must not be able to displace the user's credential.
    pytest.param(
        {"username": "decoy"},
        {"csrf_token": "abc123"},
        {"csrf_token": "abc123", "username": "admin"},
        id="credentials_override_static",
    ),
    pytest.param(
        {},
        {"password": "stale-from-page"},
        {"password": "password"},
        id="credentials_override_discovered",
    ),
]


@pytest.mark.parametrize("hidden_fields,discovered,expected_subset", MERGE_BEHAVIOR_CASES)
def test_hidden_field_merge_order(
    hidden_fields: dict[str, str],
    discovered: dict[str, str],
    expected_subset: dict[str, str],
    session: requests.Session,
) -> None:
    """Merge order: discovered (base) <- hidden_fields (override) <- credentials."""
    entries, modem_config = load_auth_fixture(
        "har_form_login_with_hidden_fields.json",
    )

    with HARMockServer(entries, modem_config=modem_config) as server:
        config = FormAuth(
            strategy="form",
            action="/goform/login",
            login_page="/login.html",
            hidden_fields=hidden_fields,
        )
        manager = FormAuthManager(config)
        manager.configure_session(session, {})

        with (
            patch(_DISCOVER_PATCH, return_value=discovered),
            patch.object(session, "request", wraps=session.request) as mock_req,
        ):
            result = manager.authenticate(
                session,
                server.base_url,
                "admin",
                "password",
            )

        assert result.success is True
        post_data = mock_req.call_args.kwargs.get("data", {})
        for key, value in expected_subset.items():
            assert post_data.get(key) == value, f"Expected {key}={value!r}, got {post_data.get(key)!r}"
        # Credentials always present regardless of discovered fields
        assert post_data.get("username") == "admin"
        assert post_data.get("password") == "password"


def test_no_login_page_skips_discovery(session: requests.Session) -> None:
    """Without login_page, no pre-fetch or field discovery occurs."""
    entries, modem_config = load_auth_fixture("har_form_login.json")

    with HARMockServer(entries, modem_config=modem_config) as server:
        config = FormAuth(
            strategy="form",
            action="/goform/login",
        )
        manager = FormAuthManager(config)
        manager.configure_session(session, {})

        with patch(_DISCOVER_PATCH) as mock_discover:
            result = manager.authenticate(
                session,
                server.base_url,
                "admin",
                "password",
            )

        assert result.success is True
        mock_discover.assert_not_called()


def test_discovery_reruns_on_every_attempt(session: requests.Session) -> None:
    """A CSRF token is re-read per login, so a second attempt never posts the first one."""
    # Discovery lives inside authenticate() rather than manager construction
    # precisely so single-use tokens stay current. Caching it would make the
    # second login of a runtime fail on firmware that rotates the token, and
    # the manager outlives the token by design.
    entries, modem_config = load_auth_fixture(
        "har_form_login_with_hidden_fields.json",
    )

    with HARMockServer(entries, modem_config=modem_config) as server:
        config = FormAuth(
            strategy="form",
            action="/goform/login",
            login_page="/login.html",
        )
        manager = FormAuthManager(config)
        manager.configure_session(session, {})

        rotating = [{"csrf_token": "first-token"}, {"csrf_token": "second-token"}]
        with (
            patch(_DISCOVER_PATCH, side_effect=rotating) as mock_discover,
            patch.object(session, "request", wraps=session.request) as mock_req,
        ):
            for _ in rotating:
                assert manager.authenticate(session, server.base_url, "admin", "password").success is True

        assert mock_discover.call_count == len(rotating)
        posted = [c.kwargs["data"]["csrf_token"] for c in mock_req.call_args_list if "data" in c.kwargs]
        assert posted == ["first-token", "second-token"], f"stale token reposted: {posted}"


# ---------------------------------------------------------------------------
# action_source: login_page — the POST URL is read off the pre-fetched page
# ---------------------------------------------------------------------------

_DYNAMIC_ACTION_PAGE = (
    "<html><body>"
    "<form name='loginform' method='POST' action='/goform/login?id=111'>"
    "<input type='hidden' name='loginName' value='admin'>"
    "</form>"
    "</body></html>"
)

_RELATIVE_ACTION_PAGE = "<html><body><form action='setup.cgi' method='POST'></form></body></html>"

_TWO_FORMS_WITH_ACTIONS = (
    "<html><body>"
    "<form id='search' action='/search'></form>"
    "<form id='login' action='/goform/login?id=222'></form>"
    "</body></html>"
)

_FORM_WITHOUT_ACTION = "<html><body><form name='loginform' method='POST'></form></body></html>"

_FORM_WITH_EMPTY_ACTION = "<html><body><form name='loginform' action=''></form></body></html>"

# ┌────────────────────────────┬────────────────┬─────────────────────────┬──────────────────────────────┐
# │ page                       │ selector       │ page url                │ resolved action              │
# ├────────────────────────────┼────────────────┼─────────────────────────┼──────────────────────────────┤
# │ dynamic action, first form │ ""             │ http://h/               │ http://h/goform/login?id=111 │
# │ dynamic action, selector   │ form[name=...] │ http://h/               │ http://h/goform/login?id=111 │
# │ two forms, selector picks  │ #login         │ http://h/               │ http://h/goform/login?id=222 │
# │ relative action            │ ""             │ http://h/cgi-bin/l.html │ http://h/cgi-bin/setup.cgi   │
# │ no form on page            │ ""             │ http://h/               │ None                         │
# │ selector matches nothing   │ #missing       │ http://h/               │ None                         │
# │ selector matches non-form  │ body           │ http://h/               │ None                         │
# │ form without action        │ ""             │ http://h/               │ None                         │
# │ form with empty action     │ ""             │ http://h/               │ None                         │
# │ empty page                 │ ""             │ http://h/               │ None                         │
# └────────────────────────────┴────────────────┴─────────────────────────┴──────────────────────────────┘

LOGIN_ACTION_CASES = [
    pytest.param(_DYNAMIC_ACTION_PAGE, "", "http://h/", "http://h/goform/login?id=111", id="first_form"),
    pytest.param(
        _DYNAMIC_ACTION_PAGE, "form[name='loginform']", "http://h/", "http://h/goform/login?id=111", id="selector"
    ),
    pytest.param(
        _TWO_FORMS_WITH_ACTIONS, "#login", "http://h/", "http://h/goform/login?id=222", id="selector_picks_form"
    ),
    pytest.param(_RELATIVE_ACTION_PAGE, "", "http://h/cgi-bin/login.html", "http://h/cgi-bin/setup.cgi", id="relative"),
    pytest.param(_NO_FORMS_PAGE, "", "http://h/", None, id="no_form"),
    pytest.param(_DYNAMIC_ACTION_PAGE, "#missing", "http://h/", None, id="selector_no_match"),
    pytest.param(_DYNAMIC_ACTION_PAGE, "body", "http://h/", None, id="selector_not_a_form"),
    pytest.param(_FORM_WITHOUT_ACTION, "", "http://h/", None, id="no_action_attribute"),
    pytest.param(_FORM_WITH_EMPTY_ACTION, "", "http://h/", None, id="empty_action"),
    pytest.param("", "", "http://h/", None, id="empty_page"),
]


@pytest.mark.parametrize("html,selector,page_url,expected", LOGIN_ACTION_CASES)
def test_login_action_from_page(html: str, selector: str, page_url: str, expected: str | None) -> None:
    """The form's action resolves against the page URL; anything short of a usable one is None."""
    assert _login_action_from_page(html, selector, page_url) == expected


def _dynamic_config(action_source: str = "login_page") -> FormAuth:
    return FormAuth(
        strategy="form",
        action="/goform/login",
        action_source=action_source,  # type: ignore[arg-type]  # both literals under test
        login_page="/",
        username_field="loginName",
        password_field="loginPassword",  # type: ignore[arg-type]  # validator normalizes str→list
        success=FormSuccess(redirect="/index.htm"),
    )


class TestActionSourceLoginPage:
    """End-to-end through the mock server: the POST goes where the page says."""

    def test_posts_to_the_action_read_from_the_page(self, session: requests.Session) -> None:
        """The per-page-load ?id= travels from the served page onto the login POST."""
        entries, modem_config = load_auth_fixture("har_form_login_dynamic_action.json")

        with HARMockServer(entries, modem_config=modem_config) as server:
            manager = FormAuthManager(_dynamic_config())
            manager.configure_session(session, {})
            with patch.object(session, "request", wraps=session.request) as mock_req:
                result = manager.authenticate(session, server.base_url, "admin", "pw")

        assert result.success is True, result.error
        assert mock_req.call_args.args[1] == f"{server.base_url}/goform/login?id=111"

    def test_config_source_posts_the_bare_action(self, session: requests.Session) -> None:
        """Left at config, the same firmware refuses the bare POST — the #189 failure, now visible in replay."""
        entries, modem_config = load_auth_fixture("har_form_login_dynamic_action.json")

        with HARMockServer(entries, modem_config=modem_config) as server:
            manager = FormAuthManager(_dynamic_config(action_source="config"))
            manager.configure_session(session, {})
            result = manager.authenticate(session, server.base_url, "admin", "pw")

        assert result.success is False
        assert "500" in result.error

    def test_refused_credentials_are_reported(self, session: requests.Session) -> None:
        """A 302 back to the login page is a refusal, told apart from success by the Location target alone."""
        entries, modem_config = load_auth_fixture("har_form_login_dynamic_action_refused.json")

        with HARMockServer(entries, modem_config=modem_config) as server:
            manager = FormAuthManager(_dynamic_config())
            manager.configure_session(session, {})
            result = manager.authenticate(session, server.base_url, "admin", "pw")

        assert result.success is False
        assert "redirect mismatch" in result.error.lower()

    def test_action_is_reread_on_every_login(self, session: requests.Session) -> None:
        """The id rotates per page serve, so a second login never reuses the first id."""
        entries, modem_config = load_auth_fixture("har_form_login_dynamic_action.json")
        served = [f"{{base}}/goform/login?id={n}" for n in (1, 2)]

        with HARMockServer(entries, modem_config=modem_config) as server:
            manager = FormAuthManager(_dynamic_config())
            manager.configure_session(session, {})
            rotating = [s.format(base=server.base_url) for s in served]
            with (
                patch(_ACTION_PATCH, side_effect=rotating) as mock_read,
                patch.object(session, "request", wraps=session.request) as mock_req,
            ):
                for _ in rotating:
                    assert manager.authenticate(session, server.base_url, "admin", "pw").success is True

        assert mock_read.call_count == len(rotating)
        posted = [c.args[1] for c in mock_req.call_args_list if c.args[0] == "POST"]
        assert posted == rotating, f"stale action reposted: {posted}"


_ACTION_PATCH = "solentlabs.cable_modem_monitor_core.auth.form._login_action_from_page"

# Each case is a page the source cannot resolve against. The login must
# still go to the configured action, and the defect must be logged at
# ERROR: a modem the static URL satisfies keeps working, and a declared
# source that stopped resolving never does so silently.
FALLBACK_CASES = [
    pytest.param(_NO_FORMS_PAGE, "", id="no_form"),
    pytest.param(_DYNAMIC_ACTION_PAGE, "#missing", id="selector_no_match"),
    pytest.param(_FORM_WITHOUT_ACTION, "", id="no_action_attribute"),
]


@pytest.mark.parametrize("page,selector", FALLBACK_CASES)
def test_unresolvable_source_falls_back_to_configured_action(
    page: str,
    selector: str,
    session: requests.Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ERROR plus fallback to ``action``; the login itself is not failed."""
    entries, modem_config = load_auth_fixture("har_form_login_with_hidden_fields.json")

    with HARMockServer(entries, modem_config=modem_config) as server:
        config = FormAuth(
            strategy="form",
            action="/goform/login",
            action_source="login_page",
            login_page="/login.html",
            form_selector=selector,
        )
        manager = FormAuthManager(config)
        manager.configure_session(session, {})

        prefetch = requests.Response()
        prefetch.status_code = 200
        prefetch._content = page.encode()
        prefetch.url = f"{server.base_url}/login.html"
        with (
            patch.object(session, "get", return_value=prefetch),
            patch.object(session, "request", wraps=session.request) as mock_req,
            caplog.at_level(logging.ERROR, logger="solentlabs.cable_modem_monitor_core.auth.form"),
        ):
            result = manager.authenticate(session, server.base_url, "admin", "pw")

    assert result.success is True, result.error
    assert mock_req.call_args.args[1] == f"{server.base_url}/goform/login"
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "/login.html" in errors[0].getMessage()
    assert "/goform/login" in errors[0].getMessage()


def test_config_source_never_reads_the_page(session: requests.Session, caplog: pytest.LogCaptureFixture) -> None:
    """Entries leaving action_source at config are unaffected: no read, no log, even with a form-less page."""
    entries, modem_config = load_auth_fixture("har_form_login_with_hidden_fields.json")

    with HARMockServer(entries, modem_config=modem_config) as server:
        config = FormAuth(strategy="form", action="/goform/login", login_page="/login.html")
        manager = FormAuthManager(config)
        manager.configure_session(session, {})
        with (
            patch(_ACTION_PATCH) as mock_read,
            caplog.at_level(logging.ERROR),
        ):
            result = manager.authenticate(session, server.base_url, "admin", "pw")

    assert result.success is True
    mock_read.assert_not_called()
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]
