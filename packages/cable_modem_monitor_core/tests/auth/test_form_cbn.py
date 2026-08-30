"""Tests for FormCbnAuthManager.

Table-driven failure scenarios. Mock HTTP responses simulate the modem.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests
from solentlabs.cable_modem_monitor_core.auth.form_cbn import FormCbnAuthManager
from solentlabs.cable_modem_monitor_core.models.modem_config.auth import FormCbnAuth


def _make_config(**overrides: Any) -> FormCbnAuth:
    """Build a FormCbnAuth config with defaults."""
    defaults: dict[str, Any] = {
        "strategy": "form_cbn",
        "login_page": "/common_page/login.html",
        "getter_endpoint": "/xml/getter.xml",
        "setter_endpoint": "/xml/setter.xml",
        "session_cookie_name": "sessionToken",
        "sid_cookie_name": "SID",
        "username_value": "NULL",
        "login_fun": 15,
    }
    defaults.update(overrides)
    return FormCbnAuth.model_validate(defaults)


def _mock_response(
    status_code: int = 200,
    text: str = "",
    ok: bool | None = None,
) -> MagicMock:
    """Build a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = text
    resp.ok = ok if ok is not None else (200 <= status_code < 400)
    return resp


# ---------------------------------------------------------------------------
# Successful login
# ---------------------------------------------------------------------------


class TestSuccessfulLogin:
    """Successful CBN auth flow."""

    def test_full_login_flow(self, session: requests.Session) -> None:
        """Full login: GET login page -> encrypt -> POST setter -> SID set."""
        config = _make_config()
        manager = FormCbnAuthManager(config)
        token = "test_session_token_123"

        login_page_resp = _mock_response(text="<html>login</html>")
        login_post_resp = _mock_response(text="successful SID=12345")

        def mock_get(url: str, **kwargs: Any) -> MagicMock:
            session.cookies.set("sessionToken", token)
            return login_page_resp

        def mock_post(url: str, **kwargs: Any) -> MagicMock:
            return login_post_resp

        session.get = mock_get  # type: ignore[assignment]
        session.post = mock_post  # type: ignore[assignment]

        result = manager.authenticate(session, "http://192.168.0.1", "admin", "password123")

        assert result.success is True
        assert session.cookies.get("SID") == "12345"

    def test_custom_login_fun(self, session: requests.Session) -> None:
        """Custom login_fun value is used in POST body."""
        config = _make_config(login_fun=20)
        manager = FormCbnAuthManager(config)

        session.cookies.set("sessionToken", "tok")
        login_page_resp = _mock_response(text="<html>login</html>")
        login_post_resp = _mock_response(text="successful SID=999")

        captured_data: list[str] = []

        def mock_get(url: str, **kwargs: Any) -> MagicMock:
            return login_page_resp

        def mock_post(url: str, data: str = "", **kwargs: Any) -> MagicMock:
            captured_data.append(data)
            return login_post_resp

        session.get = mock_get  # type: ignore[assignment]
        session.post = mock_post  # type: ignore[assignment]

        result = manager.authenticate(session, "http://192.168.0.1", "admin", "pw")

        assert result.success is True
        assert "fun=20" in captured_data[0]


# ---------------------------------------------------------------------------
# Failure scenarios — table-driven
# ---------------------------------------------------------------------------

# ┌────────────────────────────┬────────────────────────────────────────────┐
# │ scenario                   │ expected error substring                   │
# ├────────────────────────────┼────────────────────────────────────────────┤
# │ login page HTTP error      │ "Login page returned HTTP 500"             │
# │ missing session cookie     │ "did not set 'sessionToken' cookie"        │
# │ login body not successful  │ "Login failed: idloginincorrect"           │
# │ no SID in response         │ "SID not found in response"                │
# │ login POST 302 redirect    │ "Login POST returned HTTP 302"             │
# └────────────────────────────┴────────────────────────────────────────────┘


def _setup_login_page_error(session: requests.Session, status_code: int) -> None:
    """Configure session to return error on login page GET."""
    resp = _mock_response(status_code=status_code, text="error")
    session.get = lambda *a, **k: resp  # type: ignore[assignment]


def _setup_missing_cookie(session: requests.Session) -> None:
    """Configure session: login page OK but no sessionToken cookie."""
    resp = _mock_response(text="<html>login</html>")
    session.get = lambda *a, **k: resp  # type: ignore[assignment]


def _setup_post_failure(session: requests.Session) -> None:
    """Configure session: login page OK, cookie set, POST raises."""
    resp = _mock_response(text="<html>login</html>")

    def mock_get(url: str, **kwargs: Any) -> MagicMock:
        session.cookies.set("sessionToken", "tok")
        return resp

    session.get = mock_get  # type: ignore[assignment]
    session.post = MagicMock(side_effect=requests.ConnectionError("refused"))  # type: ignore[assignment]


def _setup_login_rejected(session: requests.Session) -> None:
    """Configure session: login POST returns 'idloginincorrect'."""
    page_resp = _mock_response(text="<html>login</html>")
    post_resp = _mock_response(text="idloginincorrect")

    def mock_get(url: str, **kwargs: Any) -> MagicMock:
        session.cookies.set("sessionToken", "tok")
        return page_resp

    session.get = mock_get  # type: ignore[assignment]
    session.post = lambda *a, **k: post_resp  # type: ignore[assignment]


def _setup_no_sid(session: requests.Session) -> None:
    """Configure session: login successful but no SID in body."""
    page_resp = _mock_response(text="<html>login</html>")
    post_resp = _mock_response(text="successful but no session id")

    def mock_get(url: str, **kwargs: Any) -> MagicMock:
        session.cookies.set("sessionToken", "tok")
        return page_resp

    session.get = mock_get  # type: ignore[assignment]
    session.post = lambda *a, **k: post_resp  # type: ignore[assignment]


def _setup_network_error(session: requests.Session) -> None:
    """Configure session: login page GET raises ConnectionError."""
    session.get = MagicMock(side_effect=requests.ConnectionError("unreachable"))  # type: ignore[assignment]


def _setup_login_post_redirect(session: requests.Session) -> None:
    """Configure session: login POST returns 302 (redirect to login page).

    A 302 redirect back to the login page contains "successful" in JS
    templates, which would cause a false-positive without the
    ``allow_redirects=False`` + status code check.
    """
    page_resp = _mock_response(text="<html>login</html>")
    post_resp = _mock_response(status_code=302, text="successful redirect")

    def mock_get(url: str, **kwargs: Any) -> MagicMock:
        session.cookies.set("sessionToken", "tok")
        return page_resp

    session.get = mock_get  # type: ignore[assignment]
    session.post = lambda *a, **k: post_resp  # type: ignore[assignment]


# fmt: off
# Connectivity failures are absent by design: they raise rather than
# returning an AuthResult, so they live in CONNECTIVITY_SITES below.
FAILURE_CASES = [
    # (description, setup_fn, expected_error_substring, expects_response)
    # expects_response: True when a Response was in scope at failure
    # (collector's _log_auth_failure_detail can dump request/response
    # detail). False for connection/network errors that fired before
    # any response object existed.
    ("login_page_http_error",   _setup_login_page_error,    "Login page returned HTTP 500",                 True),
    ("missing_session_cookie",  _setup_missing_cookie,      "did not set 'sessionToken' cookie",            True),
    ("login_body_rejected",     _setup_login_rejected,      "Login failed: idloginincorrect",               True),
    ("no_sid_in_response",      _setup_no_sid,              "SID not found in response",                    True),
    ("login_post_302_redirect",  _setup_login_post_redirect, "Login POST returned HTTP 302",                True),
]
# fmt: on


@pytest.mark.parametrize(
    "desc,setup_fn,expected_error,expects_response",
    FAILURE_CASES,
    ids=[c[0] for c in FAILURE_CASES],
)
def test_failure_scenario(
    session: requests.Session,
    desc: str,
    setup_fn: Any,
    expected_error: str,
    expects_response: bool,
) -> None:
    """Auth failure produces expected error message and response state."""
    config = _make_config()
    manager = FormCbnAuthManager(config)

    if desc == "login_page_http_error":
        setup_fn(session, 500)
    else:
        setup_fn(session)

    result = manager.authenticate(session, "http://192.168.0.1", "admin", "password123")

    assert result.success is False
    assert expected_error in result.error
    assert (result.response is not None) is expects_response


# ---------------------------------------------------------------------------
# Crypto dependency missing
# ---------------------------------------------------------------------------


class TestCryptoDependencyMissing:
    """Missing cryptography package returns auth error, not exception."""

    def test_import_error(self, session: requests.Session) -> None:
        """ImportError from compal_encrypt returns AuthResult error."""
        config = _make_config()
        manager = FormCbnAuthManager(config)

        page_resp = _mock_response(text="<html>login</html>")

        def mock_get(url: str, **kwargs: Any) -> MagicMock:
            session.cookies.set("sessionToken", "tok")
            return page_resp

        session.get = mock_get  # type: ignore[assignment]

        with patch(
            "solentlabs.cable_modem_monitor_core.auth.form_cbn.compal_encrypt",
            side_effect=ImportError("no cryptography"),
        ):
            result = manager.authenticate(session, "http://192.168.0.1", "admin", "pw")

        assert result.success is False
        assert "no cryptography" in result.error


# ---------------------------------------------------------------------------
# Token in POST body
# ---------------------------------------------------------------------------


class TestPostBody:
    """Token must be first parameter in POST body."""

    def test_token_is_first_param(self, session: requests.Session) -> None:
        """POST body starts with 'token='."""
        config = _make_config()
        manager = FormCbnAuthManager(config)
        token = "my_session_token"

        page_resp = _mock_response(text="<html>login</html>")
        post_resp = _mock_response(text="successful SID=1")

        captured_data: list[str] = []

        def mock_get(url: str, **kwargs: Any) -> MagicMock:
            session.cookies.set("sessionToken", token)
            return page_resp

        def mock_post(url: str, data: str = "", **kwargs: Any) -> MagicMock:
            captured_data.append(data)
            return post_resp

        session.get = mock_get  # type: ignore[assignment]
        session.post = mock_post  # type: ignore[assignment]

        manager.authenticate(session, "http://192.168.0.1", "", "pw")

        assert captured_data[0].startswith(f"token={token}")


# ---------------------------------------------------------------------------
# Connectivity is not a credential verdict (#200)
# ---------------------------------------------------------------------------

# form_cbn was the only strategy converting an unreachable modem into
# AuthResult(success=False, response=None). The collector reads that as
# status=None -> AUTH_FAILED, which trips the circuit breaker on the first
# occurrence: polling stops and the user is sent to reconfigure a password
# that was never wrong. UC-30/UC-31 want CONNECTIVITY, which backs off and
# recovers unattended.
#
# Both request sites need their own row. The cross-strategy gate in
# test_auth_failure_modes.py only ever reaches the first one, because auth
# never gets past a login page it cannot fetch.
#
# fmt: off
CONNECTIVITY_SITES = [
    # (description,      setup_fn,             raised)
    ("login_page_get",   _setup_network_error, requests.ConnectionError),
    ("login_post",       _setup_post_failure,  requests.ConnectionError),
]
# fmt: on


@pytest.mark.parametrize(
    "desc,setup_fn,raised",
    CONNECTIVITY_SITES,
    ids=[c[0] for c in CONNECTIVITY_SITES],
)
def test_connectivity_propagates_from_every_request_site(
    session: requests.Session,
    desc: str,
    setup_fn: Any,
    raised: type[Exception],
) -> None:
    """A network failure at either request leaves authenticate() unconverted."""
    setup_fn(session)
    manager = FormCbnAuthManager(FormCbnAuth(strategy="form_cbn"))

    with pytest.raises(raised):
        manager.authenticate(session, "http://192.168.100.1", "admin", "pw")
