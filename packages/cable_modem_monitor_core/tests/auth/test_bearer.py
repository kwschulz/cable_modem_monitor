"""Tests for Bearer token auth strategy.

Covers: happy path (token extracted), missing token path, HTTP error,
bad JSON response, and interface compliance (headers method).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests
from solentlabs.cable_modem_monitor_core.auth.bearer import BearerAuthManager
from solentlabs.cable_modem_monitor_core.models.modem_config.auth import BearerAuth


def _config(
    login_endpoint: str = "/rest/v1/user/login",
    token_path: str = "created.token",
    username_field: str = "username",
) -> BearerAuth:
    return BearerAuth(
        strategy="bearer",
        login_endpoint=login_endpoint,
        token_path=token_path,
        username_field=username_field,
    )


def _session() -> MagicMock:
    session = MagicMock(spec=requests.Session)
    session.headers = {}
    return session


def _response(status_code: int, json_body: object | None = None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("not json")
    resp.text = text
    return resp


# =============================================================================
# Failure cases table
# =============================================================================
#
# ┌──────────────────────────┬─────────────┬──────────────────┬────────────────────────────┐
# │ case_id                  │ status_code │ json_body        │ expected_error_fragment    │
# ├──────────────────────────┼─────────────┼──────────────────┼────────────────────────────┤
# │ http-401                 │ 401         │ (non-JSON)       │ "401"                      │
# │ http-500                 │ 500         │ (non-JSON)       │ ""                         │
# │ missing-top-level-key    │ 200         │ {"other":"data"} │ "token_path"               │
# │ missing-intermediate-key │ 200         │ {"x":"y"}        │ ""                         │
# │ non-json-body            │ 200         │ (non-JSON)       │ "json"                     │
# └──────────────────────────┴─────────────┴──────────────────┴────────────────────────────┘
#
# json_body=None triggers ValueError on .json() (non-JSON response simulation).
# token_path is "created.token" except for the intermediate-key case ("a.b.c").
#
# fmt: off
_BEARER_FAILURE_CASES: list[tuple[int, object, str, str]] = [
    # (status_code, json_body,             token_path,       expected_error_fragment)
    (401, None,                             "created.token",  "401"),
    (500, None,                             "created.token",  ""),
    (200, {"other": "data"},               "created.token",  "token_path"),
    (200, {"something": "else"},           "a.b.c",          ""),
    (200, None,                             "created.token",  "json"),
]
# fmt: on


# ------------------------------------------------------------------
# Happy path
# ------------------------------------------------------------------


class TestBearerHappyPath:
    def test_posts_json_credentials_to_login_endpoint(self) -> None:
        """Login POST sends JSON body with username and password to login_endpoint."""
        session = _session()
        resp = _response(200, {"created": {"token": "abc123", "userLevel": "regular"}})
        session.post.return_value = resp

        manager = BearerAuthManager(_config())
        result = manager.authenticate(session, "http://192.168.100.1", "admin", "secret")

        assert result.success is True
        session.post.assert_called_once_with(
            "http://192.168.100.1/rest/v1/user/login",
            json={"username": "admin", "password": "secret"},
            timeout=10,
        )

    def test_extracts_token_from_nested_path(self) -> None:
        """Token extracted by walking dot-separated token_path."""
        session = _session()
        session.post.return_value = _response(
            200, {"created": {"token": "qwertyuiop1234567890", "userLevel": "regular", "userId": 3}}
        )

        manager = BearerAuthManager(_config(token_path="created.token"))
        result = manager.authenticate(session, "http://192.168.100.1", "ignored", "mypassword")

        assert result.success is True

    def test_injects_bearer_header_into_session(self) -> None:
        """Authorization: Bearer token injected into session headers on success."""
        session = _session()
        session.headers = {}
        session.post.return_value = _response(200, {"created": {"token": "tok123"}})

        manager = BearerAuthManager(_config())
        result = manager.authenticate(session, "http://192.168.100.1", "", "pass")

        assert result.success is True
        assert session.headers["Authorization"] == "Bearer tok123"

    def test_username_included_in_body(self) -> None:
        """Username is sent alongside password in the login body."""
        session = _session()
        session.post.return_value = _response(200, {"created": {"token": "t"}})

        manager = BearerAuthManager(_config())
        manager.authenticate(session, "http://192.168.100.1", "someuser", "pass")

        body = session.post.call_args[1]["json"]
        assert body["username"] == "someuser"
        assert body["password"] == "pass"

    # Sagemcom F3896LG answers a successful login with 201 Created, not 200.
    # Evidence: issue #185 HAR (Ziggo F3896LG-ZG), POST /rest/v1/user/login.
    @pytest.mark.parametrize("status_code", [200, 201, 202, 204])
    def test_any_2xx_is_success(self, status_code: int) -> None:
        """Any 2xx login response carrying the token succeeds."""
        session = _session()
        session.post.return_value = _response(status_code, {"created": {"token": "tok"}})

        manager = BearerAuthManager(_config())
        result = manager.authenticate(session, "http://192.168.100.1", "", "pass")

        assert result.success is True
        assert session.headers["Authorization"] == "Bearer tok"

    # Password-only firmware: both Virgin (issue #82 curl) and Ziggo (issue #185
    # HAR) post {"password": ...} with no username key.
    def test_username_omitted_when_username_field_empty(self) -> None:
        """Empty username_field drops the username key from the login body."""
        session = _session()
        session.post.return_value = _response(201, {"created": {"token": "t"}})

        manager = BearerAuthManager(_config(username_field=""))
        manager.authenticate(session, "http://192.168.100.1", "unused", "secret")

        assert session.post.call_args[1]["json"] == {"password": "secret"}

    def test_username_field_renames_the_key(self) -> None:
        """A non-default username_field names the credential key."""
        session = _session()
        session.post.return_value = _response(200, {"created": {"token": "t"}})

        manager = BearerAuthManager(_config(username_field="user"))
        manager.authenticate(session, "http://192.168.100.1", "admin", "pass")

        assert session.post.call_args[1]["json"] == {"user": "admin", "password": "pass"}

    def test_shallow_token_path(self) -> None:
        """Single-segment token_path extracts top-level key."""
        session = _session()
        session.post.return_value = _response(200, {"token": "flat_token"})

        manager = BearerAuthManager(_config(token_path="token"))
        result = manager.authenticate(session, "http://192.168.100.1", "", "pass")

        assert result.success is True
        assert session.headers["Authorization"] == "Bearer flat_token"


# ------------------------------------------------------------------
# Failure paths
# ------------------------------------------------------------------


class TestBearerFailures:
    @pytest.mark.parametrize(
        "status_code,json_body,token_path,expected_error_fragment",
        _BEARER_FAILURE_CASES,
        ids=["http-401", "http-500", "missing-top-level-key", "missing-intermediate-key", "non-json-body"],
    )
    def test_authenticate_failure(
        self,
        status_code: int,
        json_body: object | None,
        token_path: str,
        expected_error_fragment: str,
    ) -> None:
        """Non-200 status, missing token path, or non-JSON body returns failure."""
        session = _session()
        session.post.return_value = _response(status_code, json_body)
        manager = BearerAuthManager(_config(token_path=token_path))
        result = manager.authenticate(session, "http://192.168.100.1", "", "pass")
        assert result.success is False
        if expected_error_fragment:
            assert expected_error_fragment in result.error.lower()

    def test_connection_error_propagates(self) -> None:
        """ConnectionError from requests propagates (not swallowed)."""
        session = _session()
        session.post.side_effect = requests.ConnectionError("refused")

        manager = BearerAuthManager(_config())
        with pytest.raises(requests.ConnectionError):
            manager.authenticate(session, "http://192.168.100.1", "", "pass")


# ------------------------------------------------------------------
# Interface compliance
# ------------------------------------------------------------------


class TestBearerInterface:
    def test_headers_returns_authorization_and_cookie(self) -> None:
        """headers() includes 'authorization' and 'cookie'."""
        manager = BearerAuthManager(_config())
        h = manager.headers()

        assert "authorization" in h
        assert "cookie" in h

    def test_timeout_is_forwarded(self) -> None:
        """timeout parameter is forwarded to requests.post."""
        session = _session()
        session.post.return_value = _response(200, {"created": {"token": "t"}})

        manager = BearerAuthManager(_config())
        manager.authenticate(session, "http://192.168.100.1", "", "pass", timeout=30)

        assert session.post.call_args[1]["timeout"] == 30
