"""Tests for FormSjclAuthHandler — SJCL AES-CCM mock handler.

Covers the two-phase login flow (GET login page + POST credentials),
AES-CCM response encryption, cookie handling, and parent class
delegation.
"""

from __future__ import annotations

import hashlib
import json

from solentlabs.cable_modem_monitor_core.test_harness.auth.base import ActionConfig
from solentlabs.cable_modem_monitor_core.test_harness.auth.form_sjcl import (
    FormSjclAuthHandler,
)


def _make_handler(
    *,
    csrf_header: str = "X-CSRF-TOKEN",
    cookie_name: str = "",
    logout_path: str = "",
    restart_path: str = "",
) -> FormSjclAuthHandler:
    """Build a handler with deterministic test parameters."""
    handler = FormSjclAuthHandler(
        login_page_path="/login.htm",
        login_endpoint="/api/login",
        pbkdf2_iterations=1000,
        pbkdf2_key_length=128,
        ccm_tag_length=8,
        decrypt_aad="nonce",
        csrf_header=csrf_header,
        cookie_name=cookie_name,
    )
    handler.configure_actions(
        ActionConfig(
            cookie_name=cookie_name,
            logout_method="GET",
            logout_path=logout_path,
            restart_method="POST",
            restart_path=restart_path,
        )
    )
    return handler


# ------------------------------------------------------------------
# Tests — is_login_request (two-phase detection)
# ------------------------------------------------------------------


class TestIsLoginRequest:
    """SJCL login detection — GET login page + POST credentials."""

    def test_get_login_page_is_login(self) -> None:
        """GET to login page path is a login request."""
        handler = _make_handler()
        assert handler.is_login_request("GET", "/login.htm") is True

    def test_post_login_endpoint_is_login(self) -> None:
        """POST to login endpoint is a login request (delegates to parent)."""
        handler = _make_handler()
        assert handler.is_login_request("POST", "/api/login") is True

    def test_get_other_path_not_login(self) -> None:
        """GET to a non-login path is not a login request."""
        handler = _make_handler()
        assert handler.is_login_request("GET", "/data.htm") is False

    def test_post_other_path_not_login(self) -> None:
        """POST to a non-login path is not a login request."""
        handler = _make_handler()
        assert handler.is_login_request("POST", "/data.htm") is False


# ------------------------------------------------------------------
# Tests — handle_login GET (login page with JS variables)
# ------------------------------------------------------------------


class TestHandleLoginGet:
    """GET login page returns HTML with JS crypto variables."""

    def test_login_page_returns_html(self) -> None:
        """GET to login page returns 200 with HTML body."""
        handler = _make_handler()
        response = handler.handle_login("GET", "/login.htm", b"", {})
        assert response is not None
        assert response.status == 200
        assert any(h[0] == "Content-Type" and "text/html" in h[1] for h in response.headers)

    def test_login_page_contains_iv(self) -> None:
        """Login page HTML contains the test IV."""
        handler = _make_handler()
        response = handler.handle_login("GET", "/login.htm", b"", {})
        assert response is not None
        assert FormSjclAuthHandler._TEST_IV_HEX in response.body

    def test_login_page_contains_salt(self) -> None:
        """Login page HTML contains the test salt."""
        handler = _make_handler()
        response = handler.handle_login("GET", "/login.htm", b"", {})
        assert response is not None
        assert FormSjclAuthHandler._TEST_SALT in response.body

    def test_login_page_contains_session_id(self) -> None:
        """Login page HTML contains the test session ID."""
        handler = _make_handler()
        response = handler.handle_login("GET", "/login.htm", b"", {})
        assert response is not None
        assert FormSjclAuthHandler._TEST_SESSION_ID in response.body


# ------------------------------------------------------------------
# Tests — handle_login POST (encrypted success response)
# ------------------------------------------------------------------


class TestHandleLoginPost:
    """POST credentials returns encrypted success response."""

    def test_post_login_sets_authenticated(self) -> None:
        """POST to login endpoint marks handler as authenticated."""
        handler = _make_handler()
        handler.handle_login("POST", "/api/login", b"encrypted_data", {})
        assert handler.is_authenticated({}) is True

    def test_post_login_returns_json(self) -> None:
        """POST login returns JSON with p_status=Match."""
        handler = _make_handler()
        response = handler.handle_login("POST", "/api/login", b"data", {})
        assert response is not None
        body = json.loads(response.body)
        assert body["p_status"] == "Match"

    def test_post_login_with_csrf_returns_encrypted_data(self) -> None:
        """With csrf_header configured, response includes encryptData."""
        handler = _make_handler(csrf_header="X-CSRF-TOKEN")
        response = handler.handle_login("POST", "/api/login", b"data", {})
        assert response is not None
        body = json.loads(response.body)
        assert "encryptData" in body
        assert len(body["encryptData"]) > 0

    def test_post_login_without_csrf_no_encrypted_data(self) -> None:
        """Without csrf_header, response has no encryptData."""
        handler = _make_handler(csrf_header="")
        response = handler.handle_login("POST", "/api/login", b"data", {})
        assert response is not None
        body = json.loads(response.body)
        assert "encryptData" not in body

    def test_encrypted_data_is_decryptable(self) -> None:
        """Encrypted CSRF nonce can be decrypted with the same key."""
        from cryptography.hazmat.primitives.ciphers.aead import AESCCM

        handler = _make_handler(csrf_header="X-CSRF-TOKEN")
        response = handler.handle_login("POST", "/api/login", b"data", {})
        assert response is not None
        body = json.loads(response.body)

        # Derive the same key the handler used
        key = hashlib.pbkdf2_hmac(
            "sha256",
            FormSjclAuthHandler._TEST_PASSWORD.encode("utf-8"),
            bytes.fromhex(FormSjclAuthHandler._TEST_SALT),
            1000,
            dklen=128 // 8,
        )
        cipher = AESCCM(key, tag_length=8)
        iv_bytes = bytes.fromhex(FormSjclAuthHandler._TEST_IV_HEX)
        plaintext = cipher.decrypt(
            iv_bytes,
            bytes.fromhex(body["encryptData"]),
            b"nonce",
        )
        assert plaintext.decode("utf-8") == FormSjclAuthHandler._TEST_CSRF_NONCE


# ------------------------------------------------------------------
# Tests — cookie handling
# ------------------------------------------------------------------


class TestCookieHandling:
    """Set-Cookie header for cookie-based sessions."""

    def test_cookie_in_login_response(self) -> None:
        """Login response includes Set-Cookie when cookie_name configured."""
        handler = _make_handler(cookie_name="session_id")
        response = handler.handle_login("POST", "/api/login", b"data", {})
        assert response is not None
        cookie_headers = [h for h in response.headers if h[0] == "Set-Cookie"]
        assert len(cookie_headers) == 1
        assert "session_id=" in cookie_headers[0][1]

    def test_no_cookie_without_cookie_name(self) -> None:
        """No Set-Cookie header when cookie_name is empty (IP-based)."""
        handler = _make_handler(cookie_name="")
        response = handler.handle_login("POST", "/api/login", b"data", {})
        assert response is not None
        cookie_headers = [h for h in response.headers if h[0] == "Set-Cookie"]
        assert len(cookie_headers) == 0


# ------------------------------------------------------------------
# Tests — non-login request passthrough
# ------------------------------------------------------------------


class TestNonLoginPassthrough:
    """handle_login returns None for non-login requests."""

    def test_get_non_login_path_returns_none(self) -> None:
        """GET to non-login path returns None (passthrough)."""
        handler = _make_handler()
        result = handler.handle_login("GET", "/data.htm", b"", {})
        assert result is None

    def test_post_non_login_path_returns_none(self) -> None:
        """POST to non-login path returns None."""
        handler = _make_handler()
        result = handler.handle_login("POST", "/other", b"", {})
        assert result is None


# ------------------------------------------------------------------
# Tests — inherited behavior (logout, restart)
# ------------------------------------------------------------------


class TestInheritedBehavior:
    """Verify FormAuthHandler parent behavior works through SJCL handler."""

    def test_logout_clears_session(self) -> None:
        """Logout inherited from FormAuthHandler clears auth state."""
        handler = _make_handler(logout_path="/logout")
        handler.handle_login("POST", "/api/login", b"data", {})
        assert handler.is_authenticated({}) is True

        assert handler.is_logout_request("GET", "/logout") is True
        handler.handle_logout()
        assert handler.is_authenticated({}) is False

    def test_restart_clears_session(self) -> None:
        """Restart inherited from FormAuthHandler clears auth state."""
        handler = _make_handler(restart_path="/restart")
        handler.handle_login("POST", "/api/login", b"data", {})
        assert handler.is_authenticated({}) is True

        assert handler.is_restart_request("POST", "/restart") is True
        handler.handle_restart()
        assert handler.is_authenticated({}) is False
