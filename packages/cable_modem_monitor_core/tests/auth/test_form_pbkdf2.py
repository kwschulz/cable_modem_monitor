"""Tests for FormPbkdf2AuthManager."""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests
from solentlabs.cable_modem_monitor_core.auth.form_pbkdf2 import (
    FormPbkdf2AuthManager,
    _derive_key,
    _fetch_csrf_token,
)
from solentlabs.cable_modem_monitor_core.models.modem_config.auth import (
    FormPbkdf2Auth,
)
from solentlabs.cable_modem_monitor_core.test_harness import HARMockServer

from .conftest import load_auth_fixture


class TestDeriveKey:
    """PBKDF2 key derivation utility."""

    def test_basic_derivation(self) -> None:
        """Derives a hex key from password and salt."""
        result = _derive_key("password", "salt", 1000, 128)
        # 128 bits = 16 bytes = 32 hex chars
        assert len(result) == 32
        assert all(c in "0123456789abcdef" for c in result)

    def test_deterministic(self) -> None:
        """Same inputs produce same output."""
        a = _derive_key("password", "salt", 1000, 128)
        b = _derive_key("password", "salt", 1000, 128)
        assert a == b

    def test_different_salt_different_key(self) -> None:
        """Different salt produces different key."""
        a = _derive_key("password", "salt1", 1000, 128)
        b = _derive_key("password", "salt2", 1000, 128)
        assert a != b

    def test_matches_hashlib(self) -> None:
        """Matches hashlib.pbkdf2_hmac directly."""
        expected = hashlib.pbkdf2_hmac("sha256", b"pass", b"salt", 1000, dklen=16).hex()
        result = _derive_key("pass", "salt", 1000, 128)
        assert result == expected


class TestFetchCsrfToken:
    """CSRF token fetch from init endpoint."""

    def test_json_body_token(self) -> None:
        """Extracts CSRF token from JSON response body."""
        entries, _ = load_auth_fixture("har_csrf_json_body.json")
        with HARMockServer(entries) as server:
            session = requests.Session()
            token = _fetch_csrf_token(
                session,
                f"{server.base_url}/api/v1/session/init_page",
                "X-CSRF-TOKEN",
                10,
            )
            assert token == "csrf_abc123"

    def test_header_token(self) -> None:
        """Extracts CSRF token from response header."""
        entries, _ = load_auth_fixture("har_csrf_header.json")
        with HARMockServer(entries) as server:
            session = requests.Session()
            token = _fetch_csrf_token(
                session,
                f"{server.base_url}/api/init",
                "X-CSRF-TOKEN",
                10,
            )
            assert token == "csrf_from_header"

    def test_unreachable_endpoint_propagates(self) -> None:
        """ConnectionError propagates for collector to classify as CONNECTIVITY."""
        session = requests.Session()
        with (
            patch.object(session, "get", side_effect=requests.ConnectionError("refused")),
            pytest.raises(requests.ConnectionError),
        ):
            _fetch_csrf_token(
                session,
                "http://127.0.0.1:1/unreachable",
                "X-CSRF-TOKEN",
                1,
            )

    def test_non_connection_error_returns_empty(self) -> None:
        """Non-connectivity RequestException returns empty token."""
        session = requests.Session()
        with patch.object(session, "get", side_effect=requests.RequestException("other")):
            token = _fetch_csrf_token(
                session,
                "http://192.168.100.1/api/init",
                "X-CSRF-TOKEN",
                10,
            )
            assert token == ""

    def test_non_json_no_header(self) -> None:
        """Returns empty string when no token found."""
        entries, _ = load_auth_fixture("har_csrf_no_token.json")
        with HARMockServer(entries) as server:
            session = requests.Session()
            token = _fetch_csrf_token(
                session,
                f"{server.base_url}/api/init",
                "X-CSRF-TOKEN",
                10,
            )
            assert token == ""


# ---------------------------------------------------------------------------
# Failure-scenario setup helpers — each configures session.post mocks so a
# single parameterized test can drive the full failure surface.
# ---------------------------------------------------------------------------


def _mock_post(session: requests.Session, *responses: Any) -> None:
    """Wire ``session.post`` to return responses in order (or raise exceptions).

    A single argument becomes ``return_value``; multiple become ``side_effect``
    so each successive POST yields the next item. Exception instances raise.
    """
    if len(responses) == 1:
        session.post = MagicMock(return_value=responses[0])  # type: ignore[assignment]  # monkey-patch on real Session; mypy can't model attribute replacement
    else:
        session.post = MagicMock(side_effect=list(responses))  # type: ignore[assignment]  # multi-response variant of the same monkey-patch


def _resp(*, json_value: Any = None, json_error: bool = False, status_code: int = 200) -> MagicMock:
    """Build a MagicMock response with the requested JSON behavior."""
    resp = MagicMock()
    resp.status_code = status_code
    if json_error:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = json_value
    return resp


def _setup_salt_invalid_json(session: requests.Session) -> None:
    _mock_post(session, _resp(json_error=True))


def _setup_no_salt_field(session: requests.Session) -> None:
    _mock_post(session, _resp(json_value={"no_salt": True}))


def _setup_salt_not_dict_string(session: requests.Session) -> None:
    _mock_post(session, _resp(json_value="not a dict"))


def _setup_salt_not_dict_list(session: requests.Session) -> None:
    _mock_post(session, _resp(json_value=[1, 2, 3]))


def _setup_salt_not_dict_int(session: requests.Session) -> None:
    _mock_post(session, _resp(json_value=42))


def _setup_login_401(session: requests.Session) -> None:
    salt = _resp(json_value={"salt": "s", "saltwebui": "sw"})
    login = _resp(json_error=True, status_code=401)
    _mock_post(session, salt, login)


def _setup_login_error_json(session: requests.Session) -> None:
    salt = _resp(json_value={"salt": "s"})
    login = _resp(json_value={"error": True, "message": "invalid password"})
    _mock_post(session, salt, login)


def _setup_login_error_ok_no_login_success(session: requests.Session) -> None:
    salt = _resp(json_value={"salt": "s"})
    login = _resp(json_value={"error": "ok", "message": "MSG_LOGIN_1"})
    _mock_post(session, salt, login)


def _setup_login_not_dict_string(session: requests.Session) -> None:
    salt = _resp(json_value={"salt": "s"})
    login = _resp(json_value="ok")
    _mock_post(session, salt, login)


def _setup_login_not_dict_list(session: requests.Session) -> None:
    salt = _resp(json_value={"salt": "s"})
    login = _resp(json_value=["error"])
    _mock_post(session, salt, login)


def _setup_login_not_dict_int(session: requests.Session) -> None:
    salt = _resp(json_value={"salt": "s"})
    login = _resp(json_value=42)
    _mock_post(session, salt, login)


def _setup_login_request_exception(session: requests.Session) -> None:
    salt = _resp(json_value={"salt": "s"})
    _mock_post(session, salt, requests.RequestException("redirects"))


# ┌──────────────────────────┬──────────────────────────────┬─────────────────────────┬───────────────────┐
# │ description              │ setup_fn                     │ expected_error          │ expects_response  │
# ├──────────────────────────┼──────────────────────────────┼─────────────────────────┼───────────────────┤
# │ salt_not_json            │ salt response not JSON       │ "json"                  │ True              │
# │ no_salt_in_response      │ salt body lacks "salt" field │ "salt"                  │ True              │
# │ salt_not_dict_*          │ salt body is non-dict JSON   │ "expected json object"  │ True              │
# │ login_401                │ login returns 401            │ "401"                   │ True              │
# │ login_error_in_json      │ login JSON has error flag    │ "invalid password"      │ True              │
# │ login_not_dict_*         │ login body is non-dict JSON  │ "expected json object"  │ True              │
# │ login_request_error      │ login raises RequestException│ "Login POST failed"     │ False             │
# └──────────────────────────┴──────────────────────────────┴─────────────────────────┴───────────────────┘
# expects_response: True when a Response was in scope at failure (collector's
# _log_auth_failure_detail can dump request/response detail). False when the
# failure fires before any response object exists.
#
# fmt: off
_FAILURE_CASES = [
    # (description,            setup_fn,                       expected_error,         expects_response)
    ("salt_not_json",          _setup_salt_invalid_json,       "json",                 True),
    ("no_salt_in_response",    _setup_no_salt_field,           "salt",                 True),
    ("salt_not_dict_string",   _setup_salt_not_dict_string,    "expected json object", True),
    ("salt_not_dict_list",     _setup_salt_not_dict_list,      "expected json object", True),
    ("salt_not_dict_int",      _setup_salt_not_dict_int,       "expected json object", True),
    ("login_401",              _setup_login_401,               "401",                  True),
    ("login_error_in_json",    _setup_login_error_json,        "invalid password",     True),
    ("login_not_dict_string",  _setup_login_not_dict_string,   "expected json object", True),
    ("login_not_dict_list",    _setup_login_not_dict_list,     "expected json object", True),
    ("login_not_dict_int",     _setup_login_not_dict_int,      "expected json object", True),
    ("login_request_error",    _setup_login_request_exception, "Login POST failed",    False),
    # Without login_success set, {"error": "ok"} is truthy and must fail.
    ("login_error_ok_no_login_success", _setup_login_error_ok_no_login_success, "MSG_LOGIN_1", True),
]
# fmt: on


class TestFormPbkdf2AuthManager:
    """FormPbkdf2AuthManager multi-round-trip auth.

    PBKDF2 auth makes two POSTs to the same endpoint (salt request,
    then login). The mock server returns the same response for both.
    Use unittest.mock for tests that need different responses per call.
    """

    def _make_config(self, **overrides: Any) -> FormPbkdf2Auth:
        """Build a FormPbkdf2Auth config with defaults."""
        defaults: dict[str, Any] = {
            "strategy": "form_pbkdf2",
            "login_endpoint": "/api/v1/session/login",
            "pbkdf2_iterations": 1000,
            "pbkdf2_key_length": 128,
        }
        defaults.update(overrides)
        return FormPbkdf2Auth.model_validate(defaults)

    def test_successful_login(self, session: requests.Session) -> None:
        """Full PBKDF2 login flow succeeds."""
        config = self._make_config(double_hash=False)
        manager = FormPbkdf2AuthManager(config)
        manager.configure_session(session, {})

        with patch.object(session, "post") as mock_post:
            salt_resp = MagicMock()
            salt_resp.json.return_value = {"salt": "s", "saltwebui": "sw"}
            login_resp = MagicMock()
            login_resp.status_code = 200
            login_resp.json.return_value = {"error": False}
            mock_post.side_effect = [salt_resp, login_resp]

            result = manager.authenticate(session, "http://192.168.100.1", "admin", "pw")
            assert result.success is True
            assert mock_post.call_count == 2

    def test_wrong_password_fails_login(self, session: requests.Session) -> None:
        """A wrong password fails the login_success check, not a later data fetch.

        This is what earns form_pbkdf2 its SESSION_REJECTED failure
        mode: because the credential is verified here, a 401 arriving
        afterwards is genuinely session-side. See
        test_auth_failure_modes.py.
        """
        config = self._make_config(double_hash=False, login_success={"error": "ok"})
        manager = FormPbkdf2AuthManager(config)
        manager.configure_session(session, {})

        with patch.object(session, "post") as mock_post:
            salt_resp = MagicMock()
            salt_resp.json.return_value = {"salt": "s", "saltwebui": "sw"}
            login_resp = MagicMock()
            login_resp.status_code = 200
            login_resp.json.return_value = {"error": "LoginIncorrect"}
            mock_post.side_effect = [salt_resp, login_resp]

            result = manager.authenticate(session, "http://192.168.100.1", "admin", "wrong")

            assert result.success is False

    def test_double_hash(self, session: requests.Session) -> None:
        """Double hash mode derives key twice."""
        config = self._make_config(double_hash=True)
        manager = FormPbkdf2AuthManager(config)
        manager.configure_session(session, {})

        with patch.object(session, "post") as mock_post:
            salt_resp = MagicMock()
            salt_resp.json.return_value = {
                "salt": "salt1",
                "saltwebui": "salt2",
            }
            login_resp = MagicMock()
            login_resp.status_code = 200
            login_resp.json.return_value = {"error": False}
            mock_post.side_effect = [salt_resp, login_resp]

            result = manager.authenticate(session, "http://192.168.100.1", "admin", "pw")
            assert result.success is True

    def test_with_csrf_endpoint(self, session: requests.Session) -> None:
        """CSRF token is fetched and set on session headers."""
        entries, _ = load_auth_fixture("har_csrf_json_body.json")

        with HARMockServer(entries) as server:
            config = self._make_config(
                csrf_init_endpoint="/api/v1/session/init_page",
                csrf_header="X-CSRF-TOKEN",
            )
            manager = FormPbkdf2AuthManager(config)
            manager.configure_session(session, {})

            with patch.object(session, "post") as mock_post:
                salt_resp = MagicMock()
                salt_resp.json.return_value = {"salt": "s"}
                login_resp = MagicMock()
                login_resp.status_code = 200
                login_resp.json.return_value = {"error": False}
                mock_post.side_effect = [salt_resp, login_resp]

                result = manager.authenticate(session, server.base_url, "admin", "pw")
            assert result.success is True
            assert session.headers.get("X-CSRF-TOKEN") == "csrf_abc123"

    def test_login_success_dict_matches_response(self, session: requests.Session) -> None:
        """login_success={error: ok} accepts {"error": "ok"} as a successful login."""
        config = self._make_config(double_hash=False, login_success={"error": "ok"})
        manager = FormPbkdf2AuthManager(config)
        manager.configure_session(session, {})

        with patch.object(session, "post") as mock_post:
            salt_resp = MagicMock()
            salt_resp.json.return_value = {"salt": "s", "saltwebui": "sw"}
            login_resp = MagicMock()
            login_resp.status_code = 200
            login_resp.json.return_value = {
                "error": "ok",
                "message": "MSG_LOGIN_1",
                "data": {"user": "admin", "uid": "1"},
            }
            mock_post.side_effect = [salt_resp, login_resp]

            result = manager.authenticate(session, "http://192.168.0.1", "admin", "pw")
            assert result.success is True

    def test_login_success_dict_mismatch_fails(self, session: requests.Session) -> None:
        """login_success={error: ok} does not suppress a different error string."""
        config = self._make_config(double_hash=False, login_success={"error": "ok"})
        manager = FormPbkdf2AuthManager(config)
        manager.configure_session(session, {})

        with patch.object(session, "post") as mock_post:
            salt_resp = MagicMock()
            salt_resp.json.return_value = {"salt": "s"}
            login_resp = MagicMock()
            login_resp.status_code = 200
            login_resp.json.return_value = {"error": "session_limit", "message": "MSG_LOGIN_150"}
            mock_post.side_effect = [salt_resp, login_resp]

            result = manager.authenticate(session, "http://192.168.0.1", "admin", "pw")
            assert result.success is False
            assert "MSG_LOGIN_150" in result.error

    def test_salt_request_connection_error_propagates(self, session: requests.Session) -> None:
        """ConnectionError on salt request propagates for collector."""
        config = self._make_config()
        manager = FormPbkdf2AuthManager(config)
        manager.configure_session(session, {})

        with (
            patch.object(session, "post", side_effect=requests.ConnectionError("refused")),
            pytest.raises(requests.ConnectionError),
        ):
            manager.authenticate(session, "http://127.0.0.1:1", "admin", "pw")

    def test_login_post_connection_error_propagates(self, session: requests.Session) -> None:
        """ConnectionError on login POST propagates for collector."""
        config = self._make_config(double_hash=False)
        manager = FormPbkdf2AuthManager(config)
        manager.configure_session(session, {})

        with (
            patch.object(session, "post") as mock_post,
            pytest.raises(requests.ConnectionError),
        ):
            salt_resp = MagicMock()
            salt_resp.json.return_value = {"salt": "s"}
            mock_post.side_effect = [
                salt_resp,
                requests.ConnectionError("connection lost"),
            ]

            manager.authenticate(session, "http://192.168.100.1", "admin", "pw")

    @pytest.mark.parametrize(
        "desc,setup_fn,expected_error,expects_response",
        _FAILURE_CASES,
        ids=[c[0] for c in _FAILURE_CASES],
    )
    def test_failure_scenario(
        self,
        session: requests.Session,
        desc: str,
        setup_fn: Any,
        expected_error: str,
        expects_response: bool,
    ) -> None:
        """Auth failure produces expected error and response state.

        Drives the full failure surface from a single table — see
        ``_FAILURE_CASES`` at module top. Adding a new failure mode
        means a row + setup helper, not a new test method.
        """
        config = self._make_config(double_hash=False)
        manager = FormPbkdf2AuthManager(config)
        manager.configure_session(session, {})

        setup_fn(session)
        result = manager.authenticate(session, "http://192.168.100.1", "admin", "pw")

        assert result.success is False
        assert expected_error.lower() in result.error.lower()
        assert (result.response is not None) is expects_response
