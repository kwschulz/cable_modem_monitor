"""SJCL AES-CCM authentication handler.

Speaks the two-phase SJCL crypto protocol: serves a login page with
deterministic JS variables, then returns a properly encrypted success
response that the real auth manager can decrypt.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..routes import RouteEntry, normalize_path
from .base import extract_action_config
from .form import FormAuthHandler

if TYPE_CHECKING:
    from ...models.modem_config import ModemConfig

_logger = logging.getLogger(__name__)


class FormSjclAuthHandler(FormAuthHandler):
    """SJCL AES-CCM auth handler — speaks the crypto protocol.

    The real ``FormSjclAuthManager`` does a two-phase login: GET the
    login page to extract JS crypto variables (IV, salt, session ID),
    then POST AES-CCM encrypted credentials. The response contains an
    encrypted CSRF nonce that the manager must decrypt.

    This handler serves a login page with known/deterministic JS
    variables and returns a properly encrypted success response that
    the real auth manager can decrypt using the test password.

    Credentials are not validated — any encrypted POST is accepted.
    Real credential validation lives in the auth managers.

    Args:
        login_page_path: Path the auth manager GETs for JS variables.
        login_endpoint: Path the auth manager POSTs encrypted credentials to.
        pbkdf2_iterations: PBKDF2 iteration count from modem.yaml.
        pbkdf2_key_length: PBKDF2 key length in bits from modem.yaml.
        ccm_tag_length: AES-CCM tag length in bytes from modem.yaml.
        decrypt_aad: AAD string for response decryption from modem.yaml.
        csrf_header: CSRF header name. Empty if no CSRF nonce exchange.
        cookie_name: Session cookie name (empty for IP-based).
        session_validation_endpoint: POST endpoint for session finalization
            (empty if no validation step).
    """

    _TEST_PASSWORD = "pw"
    _TEST_IV_HEX = "aabbccddeeff00"  # 7 bytes — minimum for AES-CCM
    _TEST_SALT = "1122334455667788"
    _TEST_SESSION_ID = "mock_session_123"
    _TEST_CSRF_NONCE = "mock_csrf_nonce"

    def __init__(
        self,
        login_page_path: str,
        login_endpoint: str,
        pbkdf2_iterations: int,
        pbkdf2_key_length: int,
        ccm_tag_length: int,
        decrypt_aad: str,
        csrf_header: str,
        cookie_name: str = "",
        session_validation_endpoint: str = "",
    ) -> None:
        super().__init__(
            login_path=login_endpoint,
            cookie_name=cookie_name,
        )
        self._login_page_path = normalize_path(login_page_path)
        self._pbkdf2_iterations = pbkdf2_iterations
        self._pbkdf2_key_length = pbkdf2_key_length
        self._ccm_tag_length = ccm_tag_length
        self._decrypt_aad = decrypt_aad
        self._csrf_header = csrf_header
        self._session_validation_path = (
            normalize_path(session_validation_endpoint) if session_validation_endpoint else ""
        )

    def is_login_request(self, method: str, path: str) -> bool:
        """SJCL login is two-phase: GET login page then POST credentials."""
        if method == "GET" and normalize_path(path) == self._login_page_path:
            return True
        return super().is_login_request(method, path)

    def handle_login(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> RouteEntry | None:
        """Serve login page (GET) or accept credentials (POST).

        GET to login page returns HTML with JS crypto variables.
        POST to login endpoint returns encrypted success response.
        """
        if method == "GET" and normalize_path(path) == self._login_page_path:
            return self._login_page_response()

        if not super().is_login_request(method, path):
            return None

        self._authenticated = True
        _logger.debug("Mock server: SJCL login accepted at %s", path)
        return self._login_success_response()

    def get_route_override(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> RouteEntry | None:
        """Return 200 empty body for session validation endpoint."""
        if self._session_validation_path and method == "POST" and normalize_path(path) == self._session_validation_path:
            return RouteEntry(status=200, headers=[], body="")
        return None

    def _login_page_response(self) -> RouteEntry:
        """Generate login page HTML with JS crypto variables."""
        html = (
            "<html><head><script>"
            f"var myIv='{self._TEST_IV_HEX}';"
            f"var mySalt='{self._TEST_SALT}';"
            f"var currentSessionId='{self._TEST_SESSION_ID}';"
            "</script></head><body></body></html>"
        )
        return RouteEntry(status=200, headers=[("Content-Type", "text/html")], body=html)

    def _login_success_response(self) -> RouteEntry:
        """Generate encrypted success response.

        Derives the AES key from the test password and known salt,
        then encrypts a CSRF nonce (if csrf_header is configured)
        so the auth manager's decryption succeeds end-to-end.
        """
        import hashlib
        import json

        response_data: dict[str, str] = {"p_status": "Match"}

        if self._csrf_header:
            from cryptography.hazmat.primitives.ciphers.aead import AESCCM

            key = hashlib.pbkdf2_hmac(
                "sha256",
                self._TEST_PASSWORD.encode("utf-8"),
                bytes.fromhex(self._TEST_SALT),
                self._pbkdf2_iterations,
                dklen=self._pbkdf2_key_length // 8,
            )
            cipher = AESCCM(key, tag_length=self._ccm_tag_length)
            iv_bytes = bytes.fromhex(self._TEST_IV_HEX)
            encrypted = cipher.encrypt(
                iv_bytes,
                self._TEST_CSRF_NONCE.encode("utf-8"),
                self._decrypt_aad.encode("utf-8"),
            )
            response_data["encryptData"] = encrypted.hex()

        body = json.dumps(response_data)
        response_headers: list[tuple[str, str]] = [("Content-Type", "application/json")]

        if self._cookie_name:
            response_headers.append(("Set-Cookie", f"{self._cookie_name}={self._SESSION_TOKEN}; Path=/"))

        return RouteEntry(status=200, headers=response_headers, body=body)


def create_handler(
    modem_config: ModemConfig,
    har_entries: list[dict[str, Any]] | None = None,
) -> FormSjclAuthHandler:
    """Entry point for dynamic auth handler dispatch."""
    from ...models.modem_config.auth import FormSjclAuth

    auth = modem_config.auth
    assert isinstance(auth, FormSjclAuth)

    action_cfg = extract_action_config(modem_config)
    return FormSjclAuthHandler(
        login_page_path=auth.login_page,
        login_endpoint=auth.login_endpoint,
        pbkdf2_iterations=auth.pbkdf2_iterations,
        pbkdf2_key_length=auth.pbkdf2_key_length,
        ccm_tag_length=auth.ccm_tag_length,
        decrypt_aad=auth.decrypt_aad,
        csrf_header=auth.csrf_header,
        cookie_name=action_cfg.cookie_name,
        session_validation_endpoint=auth.session_validation_endpoint,
    )
