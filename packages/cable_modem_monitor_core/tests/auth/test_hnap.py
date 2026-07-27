"""Tests for HnapAuthManager — HNAP HMAC challenge-response."""

from __future__ import annotations

import hashlib
import hmac
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any, Literal
from unittest.mock import MagicMock, patch

import pytest
import requests
from solentlabs.cable_modem_monitor_core.auth.base import LoginLockoutError
from solentlabs.cable_modem_monitor_core.auth.hnap import HnapAuthManager
from solentlabs.cable_modem_monitor_core.models.modem_config.auth import HnapAuth
from solentlabs.cable_modem_monitor_core.protocol.hnap import (
    HNAP_NAMESPACE,
    compute_auth_header,
    hmac_hex,
)


def _make_manager(hmac_algorithm: Literal["md5", "sha256"] = "md5") -> HnapAuthManager:
    """Create an HnapAuthManager with the given algorithm."""
    config = HnapAuth(strategy="hnap", hmac_algorithm=hmac_algorithm)
    return HnapAuthManager(config)


def _hmac_md5(key: str, message: str) -> str:
    """Compute HMAC-MD5 matching the auth manager's output."""
    return (
        hmac.new(
            key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.md5,
        )
        .hexdigest()
        .upper()
    )


# --- Mock HNAP Server ---


class _HNAPHandler(BaseHTTPRequestHandler):
    """Minimal HNAP mock server for auth testing."""

    challenge = "test_challenge_abc"
    public_key = "test_public_key_xyz"
    cookie = "test_uid_cookie"
    password = "pw"
    login_result = "OK"

    def do_POST(self) -> None:  # noqa: N802
        """Handle HNAP POST requests."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        data = json.loads(body) if body else {}

        login = data.get("Login", {})
        action = login.get("Action", "")

        if action == "request":
            self._handle_challenge()
        elif action == "login":
            self._handle_login(login)
        else:
            self._respond(400, {"error": "unknown action"})

    def _handle_challenge(self) -> None:
        """Return challenge, public key, and cookie."""
        self._respond(
            200,
            {
                "LoginResponse": {
                    "Challenge": self.challenge,
                    "PublicKey": self.public_key,
                    "Cookie": self.cookie,
                    "LoginResult": "OK",
                },
            },
        )

    def _handle_login(self, login: dict[str, Any]) -> None:
        """Validate login credentials and return result."""
        expected_private_key = _hmac_md5(
            self.public_key + self.password,
            self.challenge,
        )
        expected_login_password = _hmac_md5(
            expected_private_key,
            self.challenge,
        )

        if login.get("LoginPassword") == expected_login_password:
            self._respond(
                200,
                {"LoginResponse": {"LoginResult": self.__class__.login_result}},
            )
        else:
            self._respond(
                200,
                {"LoginResponse": {"LoginResult": "FAILED"}},
            )

    def _respond(self, status: int, data: dict) -> None:
        """Send JSON response."""
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress server log output during tests."""


@pytest.fixture()
def hnap_server():
    """Start a mock HNAP server and yield its base URL."""
    _HNAPHandler.login_result = "OK"
    server = HTTPServer(("127.0.0.1", 0), _HNAPHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


class TestSuccessfulLogin:
    """Test successful HNAP authentication."""

    def test_login_succeeds(self, hnap_server: str) -> None:
        """Full challenge-response login produces AuthResult with private key."""
        manager = _make_manager()
        session = requests.Session()

        result = manager.authenticate(session, hnap_server, "admin", "pw")

        assert result.success is True
        assert result.error == ""
        assert result.auth_context.private_key != ""

    def test_private_key_is_correct(self, hnap_server: str) -> None:
        """Derived private key matches expected HMAC computation."""
        manager = _make_manager()
        session = requests.Session()

        result = manager.authenticate(session, hnap_server, "admin", "pw")

        expected = _hmac_md5(
            _HNAPHandler.public_key + "pw",
            _HNAPHandler.challenge,
        )
        assert result.auth_context.private_key == expected

    def test_uid_cookie_set(self, hnap_server: str) -> None:
        """Session has uid cookie after successful login."""
        manager = _make_manager()
        session = requests.Session()

        manager.authenticate(session, hnap_server, "admin", "pw")

        assert session.cookies.get("uid") == _HNAPHandler.cookie

    def test_private_key_cookie_set(self, hnap_server: str) -> None:
        """Session has PrivateKey cookie after successful login.

        Both uid and PrivateKey are HNAP protocol-level cookies set
        by Login.js. Some firmware returns HTTP 500 on data requests
        when the PrivateKey cookie is missing.
        """
        manager = _make_manager()
        session = requests.Session()

        result = manager.authenticate(session, hnap_server, "admin", "pw")

        assert session.cookies.get("PrivateKey") == result.auth_context.private_key


# ┌──────────────────┬──────────┬─────────────────────────────────┬───────────────────────────┐
# │ login_result     │ password │ expected_error_substring        │ description               │
# ├──────────────────┼──────────┼─────────────────────────────────┼───────────────────────────┤
# │ "OK"             │ wrong    │ incorrect username or password  │ wrong password            │
# └──────────────────┴──────────┴─────────────────────────────────┴───────────────────────────┘
#
# LOCKUP/REBOOT are not failure *results* — they raise LoginLockoutError
# so the orchestrator can distinguish firmware lockout from a rejected
# credential (#117). Covered separately below.
#
# fmt: off
LOGIN_FAILURE_CASES = [
    # (login_result, password,    error_substring,               description)
    ("OK",           "wrong_pwd", "incorrect username or password", "wrong password"),
]
# fmt: on


@pytest.mark.parametrize(
    "login_result,password,error_substring,desc",
    LOGIN_FAILURE_CASES,
    ids=[c[3] for c in LOGIN_FAILURE_CASES],
)
def test_login_failure(
    hnap_server: str,
    login_result: str,
    password: str,
    error_substring: str,
    desc: str,
) -> None:
    """Verify login failure cases produce correct error messages."""
    _HNAPHandler.login_result = login_result
    manager = _make_manager()
    session = requests.Session()

    result = manager.authenticate(session, hnap_server, "admin", password)

    assert result.success is False
    assert error_substring in result.error
    assert result.response is not None


@pytest.mark.parametrize("login_result", ["LOCKUP", "REBOOT"])
def test_firmware_lockout_raises(hnap_server: str, login_result: str) -> None:
    """LOCKUP/REBOOT raise rather than returning a failed AuthResult (#117)."""
    _HNAPHandler.login_result = login_result
    manager = _make_manager()

    with pytest.raises(LoginLockoutError, match=login_result):
        manager.authenticate(requests.Session(), hnap_server, "admin", "pw")


# ---------------------------------------------------------------------------
# Failure-scenario setup helpers — drive the full HNAP failure surface from
# a single parameterized test. Each helper configures session.post via mock
# so a single call yields the desired response (or raises an exception).
# ---------------------------------------------------------------------------


def _challenge_ok() -> MagicMock:
    """Valid challenge response with all required LoginResponse fields."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "LoginResponse": {
            "Challenge": _HNAPHandler.challenge,
            "PublicKey": _HNAPHandler.public_key,
            "Cookie": _HNAPHandler.cookie,
            "LoginResult": "OK",
        },
    }
    return resp


def _resp_with_json(json_value: Any) -> MagicMock:
    """Build a status-200 response that returns ``json_value`` from .json()."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = json_value
    return resp


def _setup_challenge_request_exception(mock_post: MagicMock) -> None:
    mock_post.side_effect = requests.RequestException("redirects")


def _setup_challenge_not_dict_string(mock_post: MagicMock) -> None:
    mock_post.return_value = _resp_with_json("not a dict")


def _setup_challenge_not_dict_list(mock_post: MagicMock) -> None:
    mock_post.return_value = _resp_with_json([1, 2, 3])


def _setup_challenge_not_dict_int(mock_post: MagicMock) -> None:
    mock_post.return_value = _resp_with_json(42)


def _setup_challenge_missing_fields(mock_post: MagicMock) -> None:
    """LoginResponse is a dict but lacks Challenge/PublicKey/Cookie."""
    mock_post.return_value = _resp_with_json({"LoginResponse": {}})


def _setup_login_request_exception(mock_post: MagicMock) -> None:
    mock_post.side_effect = [_challenge_ok(), requests.RequestException("redirects")]


def _setup_login_not_dict_string(mock_post: MagicMock) -> None:
    mock_post.side_effect = [_challenge_ok(), _resp_with_json("not a dict")]


def _setup_login_not_dict_list(mock_post: MagicMock) -> None:
    mock_post.side_effect = [_challenge_ok(), _resp_with_json([1, 2, 3])]


def _setup_login_not_dict_int(mock_post: MagicMock) -> None:
    mock_post.side_effect = [_challenge_ok(), _resp_with_json(42)]


def _setup_login_unexpected_result(mock_post: MagicMock) -> None:
    """LoginResult is dict-shaped but value isn't OK/OK_CHANGED/FAILED/LOCKUP/REBOOT."""
    mock_post.side_effect = [
        _challenge_ok(),
        _resp_with_json({"LoginResponse": {"LoginResult": "UNKNOWN_STATUS"}}),
    ]


# ┌────────────────────────────────┬────────────────────────────────────┬────────────────────────┬──────────────────┐
# │ description                    │ setup_fn                           │ expected_error         │ expects_response │
# ├────────────────────────────────┼────────────────────────────────────┼────────────────────────┼──────────────────┤
# │ challenge_request_exception    │ non-connectivity RequestException  │ "challenge request"    │ False            │
# │ challenge_not_dict_*           │ challenge body is non-dict JSON    │ "expected json object" │ True             │
# │ challenge_missing_fields       │ challenge dict lacks required      │ "missing required"     │ True             │
# │ login_request_exception        │ non-connectivity on login POST     │ "login request failed" │ False            │
# │ login_not_dict_*               │ login body is non-dict JSON        │ "expected json object" │ True             │
# │ login_unexpected_result        │ LoginResult value is unrecognized  │ "unexpected result"    │ True             │
# └────────────────────────────────┴────────────────────────────────────┴────────────────────────┴──────────────────┘
# expects_response: True when a Response was in scope at failure (collector's
# _log_auth_failure_detail can dump request/response detail). False when the
# failure fires before any response object exists (RequestException paths).
#
# fmt: off
_FAILURE_CASES = [
    # (description,                     setup_fn,                          expected_error,          expects_response)
    ("challenge_request_exception",     _setup_challenge_request_exception, "challenge request",     False),
    ("challenge_not_dict_string",       _setup_challenge_not_dict_string,   "expected json object",  True),
    ("challenge_not_dict_list",         _setup_challenge_not_dict_list,     "expected json object",  True),
    ("challenge_not_dict_int",          _setup_challenge_not_dict_int,      "expected json object",  True),
    ("challenge_missing_fields",        _setup_challenge_missing_fields,    "missing required",      True),
    ("login_request_exception",         _setup_login_request_exception,     "login request failed",  False),
    ("login_not_dict_string",           _setup_login_not_dict_string,       "expected json object",  True),
    ("login_not_dict_list",             _setup_login_not_dict_list,         "expected json object",  True),
    ("login_not_dict_int",              _setup_login_not_dict_int,          "expected json object",  True),
    ("login_unexpected_result",         _setup_login_unexpected_result,     "unexpected result",     True),
]
# fmt: on


@pytest.mark.parametrize(
    "desc,setup_fn,expected_error,expects_response",
    _FAILURE_CASES,
    ids=[c[0] for c in _FAILURE_CASES],
)
def test_failure_scenario(
    desc: str,
    setup_fn: Any,
    expected_error: str,
    expects_response: bool,
) -> None:
    """Auth failure produces expected error and response state.

    Drives the full HNAP failure surface from ``_FAILURE_CASES``. Adding a
    new failure mode means a row + setup helper, not a new test method.
    The hnap_server-based ``test_login_failure`` covers FAILED/LOCKUP/REBOOT
    via the real crypto round-trip; this table covers the parse-failure,
    RequestException, and unexpected-result paths via mocks.
    """
    manager = _make_manager()
    session = requests.Session()

    with patch.object(session, "post") as mock_post:
        setup_fn(mock_post)
        result = manager.authenticate(session, "http://192.168.100.1", "admin", "pw")

    assert result.success is False
    assert expected_error.lower() in result.error.lower()
    assert (result.response is not None) is expects_response


def test_connection_refused() -> None:
    """ConnectionError on challenge POST propagates for collector to classify."""
    manager = _make_manager()
    session = requests.Session()

    with pytest.raises(requests.ConnectionError):
        manager.authenticate(session, "http://127.0.0.1:1", "admin", "pw")


def test_login_connection_error_propagates() -> None:
    """ConnectionError on login POST (after challenge succeeds) propagates."""
    manager = _make_manager()
    session = requests.Session()

    with patch.object(session, "post") as mock_post:
        mock_post.side_effect = [_challenge_ok(), requests.ConnectionError("lost mid-flow")]
        with pytest.raises(requests.ConnectionError):
            manager.authenticate(session, "http://192.168.100.1", "admin", "pw")


# ┌─────────────┬──────────┬──────────────────────────┐
# │ algorithm   │ hex_len  │ description              │
# ├─────────────┼──────────┼──────────────────────────┤
# │ md5         │ 32       │ MD5 produces 32 hex      │
# │ sha256      │ 64       │ SHA256 produces 64 hex   │
# └─────────────┴──────────┴──────────────────────────┘
#
# fmt: off
HMAC_ALGORITHM_CASES = [
    # (algorithm, hex_len, description)
    ("md5",    32, "MD5 produces 32 hex chars"),
    ("sha256", 64, "SHA256 produces 64 hex chars"),
]
# fmt: on


@pytest.mark.parametrize(
    "algorithm,hex_len,desc",
    HMAC_ALGORITHM_CASES,
    ids=[c[2] for c in HMAC_ALGORITHM_CASES],
)
def test_hmac_algorithm_output(algorithm: str, hex_len: int, desc: str) -> None:
    """Verify HMAC algorithm produces correct output length."""
    result = hmac_hex("test_key", "test_message", algorithm=algorithm)
    assert len(result) == hex_len
    assert result == result.upper()


class TestAuthHeader:
    """Test HNAP_AUTH header computation."""

    @patch("solentlabs.cable_modem_monitor_core.protocol.hnap.time")
    def test_header_format(self, mock_time: Any) -> None:
        """HNAP_AUTH header has correct format: 'HMAC_HEX TIMESTAMP'."""
        mock_time.time.return_value = 1708960420.646

        header = compute_auth_header("withoutloginkey", "Login")

        parts = header.split(" ")
        assert len(parts) == 2
        hmac_part, timestamp = parts
        assert len(hmac_part) == 32
        assert hmac_part == hmac_part.upper()
        assert timestamp.isdigit()

    @patch("solentlabs.cable_modem_monitor_core.protocol.hnap.time")
    def test_timestamp_modulo(self, mock_time: Any) -> None:
        """Timestamp uses modulo 2_000_000_000_000."""
        mock_time.time.return_value = 2_500_000_000.123

        header = compute_auth_header("key", "Action")

        timestamp = int(header.split(" ")[1])
        expected = 2_500_000_000_123 % 2_000_000_000_000
        assert timestamp == expected

    @patch("solentlabs.cable_modem_monitor_core.protocol.hnap.time")
    def test_hmac_includes_quoted_namespace(self, mock_time: Any) -> None:
        """HMAC message includes quoted SOAP namespace URI."""
        mock_time.time.return_value = 1.0

        header = compute_auth_header("test_key", "Login")

        timestamp = "1000"
        soap_uri = f'"{HNAP_NAMESPACE}Login"'
        expected_hmac = _hmac_md5("test_key", timestamp + soap_uri)
        assert header == f"{expected_hmac} {timestamp}"
