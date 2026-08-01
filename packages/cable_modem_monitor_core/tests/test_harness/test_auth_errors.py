"""Tests for auth handler error paths and factory dispatch.

Covers HNAP handler error branches (invalid signatures, wrong
password, missing headers), auth factory dispatch for all strategy
types, and form handler cookie re-authentication.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
from pathlib import Path
from typing import Any, Literal
from unittest.mock import MagicMock

import pytest
from solentlabs.cable_modem_monitor_core.test_harness.auth.base import AuthHandler
from solentlabs.cable_modem_monitor_core.test_harness.auth.bearer import BearerAuthHandler
from solentlabs.cable_modem_monitor_core.test_harness.auth.factory import (
    create_auth_handler,
)
from solentlabs.cable_modem_monitor_core.test_harness.auth.form import (
    FormAuthHandler,
)
from solentlabs.cable_modem_monitor_core.test_harness.auth.form_sjcl import (
    FormSjclAuthHandler,
)
from solentlabs.cable_modem_monitor_core.test_harness.auth.hnap import (
    HnapAuthHandler,
)

from tests._helpers import load_fixture

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_entries(name: str) -> list[dict[str, Any]]:
    """Load HAR entries from a fixture file."""
    data = load_fixture(FIXTURES_DIR / name)
    return list(data["_entries"])


def _make_hnap_handler(algorithm: Literal["md5", "sha256"] = "md5") -> HnapAuthHandler:
    """Create an HNAP handler with fixture entries."""
    entries = _load_entries("har_entries_hnap_auth.json")
    return HnapAuthHandler(hmac_algorithm=algorithm, har_entries=entries)


def _hmac_hex(key: str, message: str, algorithm: Literal["md5", "sha256"] = "md5") -> str:
    """Compute HMAC and return uppercase hex digest."""
    digest = hashlib.sha256 if algorithm == "sha256" else hashlib.md5
    return (
        hmac_mod.new(
            key.encode("utf-8"),
            message.encode("utf-8"),
            digest,
        )
        .hexdigest()
        .upper()
    )


def _valid_phase1_headers(handler: HnapAuthHandler) -> dict[str, str]:
    """Build valid HNAP phase 1 headers."""
    timestamp = "12345"
    soap_action = '"http://purenetworks.com/HNAP1/Login"'
    pre_auth_key = "withoutloginkey"
    hmac_hash = _hmac_hex(pre_auth_key, timestamp + soap_action)
    return {
        "soapaction": soap_action,
        "hnap_auth": f"{hmac_hash} {timestamp}",
    }


# ------------------------------------------------------------------
# HNAP handler — error branches
# ------------------------------------------------------------------


class TestHnapNonLoginSoapAction:
    """Non-Login SOAPAction returns None to delegate to server."""

    def test_non_login_soap_action_returns_none(self) -> None:
        """HNAP request with non-Login SOAPAction → None (delegate)."""
        handler = _make_hnap_handler()
        response = handler.handle_login(
            "POST",
            "/HNAP1/",
            b'{"GetDeviceInfo": {}}',
            {"soapaction": '"http://purenetworks.com/HNAP1/GetDeviceInfo"'},
        )
        assert response is None


class TestHnapPhase1Errors:
    """Phase 1 (challenge request) — signature validation failures."""

    def test_invalid_signature_returns_failed(self) -> None:
        """Invalid HNAP_AUTH in phase 1 → LoginResult FAILED."""
        handler = _make_hnap_handler()
        body = json.dumps({"Login": {"Action": "request"}}).encode()
        headers = {
            "soapaction": '"http://purenetworks.com/HNAP1/Login"',
            "hnap_auth": "BADHASH 12345",
        }
        response = handler.handle_login("POST", "/HNAP1/", body, headers)
        assert response is not None
        data = json.loads(response.body)
        assert data["LoginResponse"]["LoginResult"] == "FAILED"

    def test_missing_hnap_auth_returns_failed(self) -> None:
        """Missing HNAP_AUTH header in phase 1 → FAILED."""
        handler = _make_hnap_handler()
        body = json.dumps({"Login": {"Action": "request"}}).encode()
        headers = {"soapaction": '"http://purenetworks.com/HNAP1/Login"'}
        response = handler.handle_login("POST", "/HNAP1/", body, headers)
        assert response is not None
        data = json.loads(response.body)
        assert data["LoginResponse"]["LoginResult"] == "FAILED"


class TestHnapPhase2Errors:
    """Phase 2 (login attempt) — credential validation failures."""

    def _do_phase1(self, handler: HnapAuthHandler) -> dict[str, Any]:
        """Complete phase 1 and return challenge data."""
        body = json.dumps({"Login": {"Action": "request"}}).encode()
        headers = _valid_phase1_headers(handler)
        response = handler.handle_login("POST", "/HNAP1/", body, headers)
        assert response is not None
        data: dict[str, Any] = json.loads(response.body)["LoginResponse"]
        return data

    def test_invalid_private_key_signature(self) -> None:
        """Phase 2 with wrong private key signature → FAILED."""
        handler = _make_hnap_handler()
        self._do_phase1(handler)  # complete phase 1 to enable phase 2

        body = json.dumps({"Login": {"Action": "login", "LoginPassword": "anything", "Captcha": ""}}).encode()
        timestamp = "12345"
        soap_action = '"http://purenetworks.com/HNAP1/Login"'
        headers = {
            "soapaction": soap_action,
            "hnap_auth": f"WRONGKEY {timestamp}",
        }
        response = handler.handle_login("POST", "/HNAP1/", body, headers)
        assert response is not None
        data = json.loads(response.body)
        assert data["LoginResponse"]["LoginResult"] == "FAILED"

    def test_wrong_password(self) -> None:
        """Phase 2 with correct signature but wrong LoginPassword → FAILED."""
        handler = _make_hnap_handler()
        challenge_data = self._do_phase1(handler)

        # Compute CORRECT private key (using real test password "pw")
        private_key = _hmac_hex(
            challenge_data["PublicKey"] + HnapAuthHandler._PASSWORD,
            challenge_data["Challenge"],
        )

        # Sign with correct private key — but send wrong LoginPassword
        timestamp = "12345"
        soap_action = '"http://purenetworks.com/HNAP1/Login"'
        hmac_hash = _hmac_hex(private_key, timestamp + soap_action)
        body = json.dumps({"Login": {"Action": "login", "LoginPassword": "wrong_hash", "Captcha": ""}}).encode()
        headers = {
            "soapaction": soap_action,
            "hnap_auth": f"{hmac_hash} {timestamp}",
        }
        response = handler.handle_login("POST", "/HNAP1/", body, headers)
        assert response is not None
        data = json.loads(response.body)
        assert data["LoginResponse"]["LoginResult"] == "FAILED"


class TestHnapUnknownAction:
    """Unknown Login action (not 'request' or 'login') returns ERROR."""

    def test_unknown_action_returns_error(self) -> None:
        """Login body with unknown Action → ERROR response."""
        handler = _make_hnap_handler()
        body = json.dumps({"Login": {"Action": "unknown"}}).encode()
        headers = {
            "soapaction": '"http://purenetworks.com/HNAP1/Login"',
            "hnap_auth": "HASH 12345",
        }
        response = handler.handle_login("POST", "/HNAP1/", body, headers)
        assert response is not None
        data = json.loads(response.body)
        assert data["LoginResponse"]["LoginResult"] == "ERROR"


class TestHnapValidationEdgeCases:
    """HNAP_AUTH header validation edge cases."""

    def test_malformed_hnap_auth_no_space(self) -> None:
        """HNAP_AUTH without space separator → signature validation fails."""
        handler = _make_hnap_handler()
        body = json.dumps({"Login": {"Action": "request"}}).encode()
        headers = {
            "soapaction": '"http://purenetworks.com/HNAP1/Login"',
            "hnap_auth": "NOSPACEHERE",
        }
        response = handler.handle_login("POST", "/HNAP1/", body, headers)
        assert response is not None
        data = json.loads(response.body)
        assert data["LoginResponse"]["LoginResult"] == "FAILED"

    def test_missing_soap_action_in_validation(self) -> None:
        """Missing SOAPAction in _validate_hnap_auth → signature validation fails."""
        handler = _make_hnap_handler()
        body = json.dumps({"Login": {"Action": "request"}}).encode()
        # Include "Login" in soapaction to pass the outer check,
        # but set it to something the HMAC won't match
        headers = {
            "soapaction": "Login",
            "hnap_auth": "HASH 12345",
        }
        response = handler.handle_login("POST", "/HNAP1/", body, headers)
        assert response is not None
        data = json.loads(response.body)
        assert data["LoginResponse"]["LoginResult"] == "FAILED"

    def test_validate_hnap_auth_missing_soapaction(self) -> None:
        """_validate_hnap_auth returns False when SOAPAction is empty."""
        handler = _make_hnap_handler()
        result = handler._validate_hnap_auth(
            {"hnap_auth": "HASH 12345", "soapaction": ""},
            "anykey",
        )
        assert result is False

    def test_is_authenticated_without_login(self) -> None:
        """is_authenticated returns False before login."""
        handler = _make_hnap_handler()
        assert handler.is_authenticated({}) is False

    def test_is_authenticated_missing_hnap_auth(self) -> None:
        """is_authenticated with missing HNAP_AUTH returns False."""
        handler = _make_hnap_handler()
        handler._authenticated = True
        assert handler.is_authenticated({}) is False


# ------------------------------------------------------------------
# Auth factory — dispatch for all strategy types
# ------------------------------------------------------------------


def _make_config(data: dict[str, Any]) -> Any:
    """Validate a raw modem config dict into a ModemConfig instance."""
    from solentlabs.cable_modem_monitor_core.config_loader import validate_modem_config

    defaults = {
        "manufacturer": "Solent Labs",
        "model": "T100",
        "transport": "http",
        "default_host": "192.168.100.1",
        "status": "unsupported",
        "auth": {"strategy": "none"},
    }
    return validate_modem_config({**defaults, **data})


class TestFactoryFormSjclDispatch:
    """create_auth_handler dispatches FormSjclAuth to FormSjclAuthHandler."""

    def test_form_sjcl_creates_sjcl_handler(self) -> None:
        """FormSjclAuth config → FormSjclAuthHandler instance."""
        config = _make_config(
            {
                "auth": {
                    "strategy": "form_sjcl",
                    "login_page": "/login.htm",
                    "login_endpoint": "/api/login",
                    "pbkdf2_iterations": 1000,
                    "pbkdf2_key_length": 128,
                    "ccm_tag_length": 8,
                    "decrypt_aad": "nonce",
                    "csrf_header": "X-CSRF",
                },
            }
        )
        handler = create_auth_handler(config)
        assert isinstance(handler, FormSjclAuthHandler)


class TestFactoryUrlTokenDispatch:
    """create_auth_handler dispatches UrlTokenAuth to no-auth handler."""

    def test_url_token_creates_base_handler(self) -> None:
        """UrlTokenAuth config → base AuthHandler (no gating needed)."""
        config = _make_config(
            {
                "auth": {
                    "strategy": "url_token",
                    "login_page": "/login.cgi",
                },
            }
        )
        handler = create_auth_handler(config)
        # URL token uses base handler (no auth gating)
        assert type(handler) is AuthHandler


class TestFactoryFallback:
    """create_auth_handler falls back to base handler for unknown types."""

    def test_none_modem_config(self) -> None:
        """None modem_config → base AuthHandler."""
        handler = create_auth_handler(None)
        assert type(handler) is AuthHandler

    def test_none_auth(self) -> None:
        """modem_config with auth=None → base AuthHandler."""
        config = MagicMock()
        config.auth = None
        handler = create_auth_handler(config)
        assert type(handler) is AuthHandler


class TestFactoryMissingHandler:
    """A strategy with no handler module raises rather than degrading."""

    def test_missing_handler_module_raises(self) -> None:
        """An unresolvable strategy must not silently degrade to no-auth."""
        config = _make_config(
            {
                "auth": {
                    "strategy": "bearer",
                    "login_endpoint": "/rest/v1/user/login",
                    "token_path": "created.token",
                },
            }
        )
        # Every shipped strategy has a handler module, so the missing-module
        # branch is only reachable by naming one that does not exist.
        config.auth.strategy = "no_such_strategy"  # type: ignore[assignment] # rationale: strategy is a Literal, so only an out-of-model value can reach the factory's ModuleNotFoundError branch
        with pytest.raises(ModuleNotFoundError, match="no_such_strategy"):
            create_auth_handler(config)


class TestBearerHandler:
    """BearerAuthHandler issues a token and then demands it back."""

    def _handler(self, token_path: str = "created.token") -> BearerAuthHandler:
        return BearerAuthHandler(
            login_path="/rest/v1/user/login",
            token_path=token_path,
        )

    def test_login_answers_201_with_token_nested_at_token_path(self) -> None:
        """Login returns 201 and a body the configured token_path walks."""
        handler = self._handler()
        response = handler.handle_login("POST", "/rest/v1/user/login", b"{}", {})

        assert response is not None
        assert response.status == 201
        assert json.loads(response.body)["created"]["token"]

    def test_shallow_token_path_nests_one_level(self) -> None:
        """A single-segment token_path produces a flat body."""
        handler = self._handler(token_path="token")
        response = handler.handle_login("POST", "/rest/v1/user/login", b"{}", {})

        assert response is not None
        assert list(json.loads(response.body)) == ["token"]

    def test_issued_token_is_required_on_later_requests(self) -> None:
        """Only the issued token authenticates; absent or wrong tokens do not."""
        handler = self._handler()
        response = handler.handle_login("POST", "/rest/v1/user/login", b"{}", {})
        assert response is not None
        token = json.loads(response.body)["created"]["token"]

        assert handler.is_authenticated({"authorization": f"Bearer {token}"}) is True
        assert handler.is_authenticated({"authorization": "Bearer wrong"}) is False
        assert handler.is_authenticated({}) is False

    def test_challenge_is_401(self) -> None:
        """Unauthenticated requests get a 401 challenge."""
        assert self._handler().get_challenge_response().status == 401


# ------------------------------------------------------------------
# Form handler — cookie re-authentication
# ------------------------------------------------------------------


class TestFormCookieReAuth:
    """FormAuthHandler re-authenticates via cookie header."""

    def test_cookie_re_authenticates(self) -> None:
        """Cookie in request headers re-authenticates the session."""
        handler = FormAuthHandler(login_path="/login.htm", cookie_name="session_id")
        assert handler.is_authenticated({}) is False

        # Simulate browser sending the session cookie
        headers = {"cookie": "session_id=abc123; other=val"}
        assert handler.is_authenticated(headers) is True
        # Subsequent calls should also be True (flag set)
        assert handler.is_authenticated({}) is True

    def test_wrong_cookie_does_not_authenticate(self) -> None:
        """Wrong cookie name does not re-authenticate."""
        handler = FormAuthHandler(login_path="/login.htm", cookie_name="session_id")
        headers = {"cookie": "wrong_cookie=abc123"}
        assert handler.is_authenticated(headers) is False

    def test_handle_login_non_login_path_returns_none(self) -> None:
        """handle_login returns None for requests to non-login paths."""
        handler = FormAuthHandler(login_path="/login.htm")
        result = handler.handle_login("GET", "/data.htm", b"", {})
        assert result is None


# ------------------------------------------------------------------
# Base handler — is_authenticated always True
# ------------------------------------------------------------------


class TestBaseHandlerDefaults:
    """Base AuthHandler default behavior for no-auth modems."""

    def test_is_authenticated_always_true(self) -> None:
        """Base handler always returns True (no-auth)."""
        handler = AuthHandler()
        assert handler.is_authenticated({}) is True
        assert handler.is_authenticated({"cookie": "some=val"}) is True

    def test_handle_logout_returns_ok(self) -> None:
        """Base handler handle_logout returns 200 OK."""
        handler = AuthHandler()
        response = handler.handle_logout()
        assert response.status == 200
