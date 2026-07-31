"""PBKDF2 multi-round challenge-response authentication handler.

Speaks the multi-round PBKDF2 protocol: optionally serves a CSRF
token, then serves deterministic salt values on the salt trigger
POST, and validates the derived PBKDF2 hash on the login POST.

Follows the same pattern as ``FormSjclAuthHandler`` — extends
``FormAuthHandler`` for session gating, overrides login handling
for the multi-phase protocol.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

from ..routes import RouteEntry, normalize_path
from .base import extract_action_config
from .form import FormAuthHandler

if TYPE_CHECKING:
    from ...models.modem_config import ModemConfig

_logger = logging.getLogger(__name__)


class FormPbkdf2AuthHandler(FormAuthHandler):
    """PBKDF2 challenge-response auth handler.

    The real ``FormPbkdf2AuthManager`` does a multi-round login:

    1. Optional GET to ``csrf_init_endpoint`` for a CSRF token.
    2. POST salt trigger to ``login_endpoint`` — server returns salts.
    3. Client derives PBKDF2 key from password + salt.
    4. POST derived hash to ``login_endpoint`` — server validates.

    This handler serves deterministic salt values and validates the
    derived key against the test password (``pw``), catching config
    bugs like wrong iterations, key length, or double_hash setting.

    Args:
        login_endpoint: Path for both salt trigger and login POSTs.
        salt_trigger: Body value that identifies a salt request
            (e.g., ``seeksalthash``).
        pbkdf2_iterations: PBKDF2 iteration count from modem.yaml.
        pbkdf2_key_length: PBKDF2 key length in bits from modem.yaml.
        double_hash: Whether to double-hash with ``saltwebui``.
        csrf_init_endpoint: GET endpoint for CSRF token (empty if none).
        csrf_header: CSRF header name (empty if none).
        cookie_name: Session cookie name (empty for IP-based).
        login_success: Key-value pairs the firmware returns on success
            (e.g., ``{"error": "ok"}``). When set, the mock success
            response body mirrors these pairs so the auth manager's
            ``login_success`` check passes. Empty means the default
            ``{"success": true}`` body is returned.
    """

    _TEST_PASSWORD = "pw"
    _TEST_SALT = "testpbkdf2salt1234"
    _TEST_SALT_WEBUI = "testpbkdf2saltwebui5678"
    _TEST_CSRF_TOKEN = "mock_csrf_token_pbkdf2"

    def __init__(
        self,
        login_endpoint: str,
        salt_trigger: str,
        pbkdf2_iterations: int,
        pbkdf2_key_length: int,
        double_hash: bool,
        csrf_init_endpoint: str = "",
        csrf_header: str = "",
        cookie_name: str = "",
        login_success: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            login_path=login_endpoint,
            cookie_name=cookie_name,
        )
        self._salt_trigger = salt_trigger
        self._pbkdf2_iterations = pbkdf2_iterations
        self._pbkdf2_key_length = pbkdf2_key_length
        self._double_hash = double_hash
        self._csrf_init_path = normalize_path(csrf_init_endpoint) if csrf_init_endpoint else ""
        self._csrf_header = csrf_header
        self._login_success = login_success or {}

        # Pre-compute expected derived key from test password + test salts
        self._expected_derived = self._compute_expected_key()

    def is_login_request(self, method: str, path: str) -> bool:
        """PBKDF2 login includes optional CSRF GET and two POST phases."""
        if self._csrf_init_path and method == "GET" and normalize_path(path) == self._csrf_init_path:
            return True
        return super().is_login_request(method, path)

    def handle_login(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> RouteEntry | None:
        """Route to the correct login phase.

        Phase 0 (GET csrf_init): return CSRF token.
        Phase 1 (POST salt trigger): return deterministic salts.
        Phase 2 (POST derived hash): validate and set session.
        """
        # Phase 0: CSRF init
        if self._csrf_init_path and method == "GET" and normalize_path(path) == self._csrf_init_path:
            return self._csrf_init_response()

        if not super().is_login_request(method, path):
            return None

        # Distinguish salt trigger from login by body content
        if self._is_salt_request(body):
            return self._salt_response()

        # Phase 2: login with derived hash
        if self._validate_login(body):
            self._authenticated = True
            _logger.debug("Mock server: PBKDF2 login accepted at %s", path)
            return self._login_success_response()

        _logger.debug("Mock server: PBKDF2 login rejected — derived key mismatch")
        return RouteEntry(
            status=200,
            headers=[("Content-Type", "application/json")],
            body=json.dumps({"error": "LoginIncorrect", "message": "Invalid credentials"}),
        )

    def _is_salt_request(self, body: bytes) -> bool:
        """Check if the POST body is a salt trigger request.

        Accepts both form-encoded (``password=seeksalthash``) and
        JSON (``{"password": "seeksalthash"}``) bodies.
        """
        password = self._extract_field(body, "password")
        return password == self._salt_trigger

    def _validate_login(self, body: bytes) -> bool:
        """Validate the derived PBKDF2 hash in the login POST body.

        Accepts both form-encoded and JSON bodies.
        """
        submitted = self._extract_field(body, "password")
        return submitted == self._expected_derived

    @staticmethod
    def _extract_field(body: bytes, field: str) -> str:
        """Extract a field value from a form-encoded or JSON body."""
        # Try JSON first
        try:
            data = json.loads(body)
            return str(data.get(field, ""))
        except (json.JSONDecodeError, AttributeError):
            pass
        # Fall back to form-encoded
        try:
            parsed = parse_qs(body.decode("utf-8"))
            values = parsed.get(field, [])
            return values[0] if values else ""
        except (UnicodeDecodeError, ValueError):
            return ""

    def _compute_expected_key(self) -> str:
        """Pre-compute the expected derived key from the test password."""
        derived = _derive_key(
            self._TEST_PASSWORD,
            self._TEST_SALT,
            self._pbkdf2_iterations,
            self._pbkdf2_key_length,
        )
        if self._double_hash:
            derived = _derive_key(
                derived,
                self._TEST_SALT_WEBUI,
                self._pbkdf2_iterations,
                self._pbkdf2_key_length,
            )
        return derived

    def _csrf_init_response(self) -> RouteEntry:
        """Return a CSRF token response."""
        response_headers: list[tuple[str, str]] = [("Content-Type", "application/json")]
        if self._csrf_header:
            response_headers.append((self._csrf_header, self._TEST_CSRF_TOKEN))
        body = json.dumps({"token": self._TEST_CSRF_TOKEN})
        return RouteEntry(status=200, headers=response_headers, body=body)

    def _salt_response(self) -> RouteEntry:
        """Return deterministic salt values."""
        body = json.dumps({"salt": self._TEST_SALT, "saltwebui": self._TEST_SALT_WEBUI})
        response_headers: list[tuple[str, str]] = [("Content-Type", "application/json")]
        if self._cookie_name:
            response_headers.append(("Set-Cookie", f"{self._cookie_name}={self._SESSION_TOKEN}; Path=/"))
        return RouteEntry(status=200, headers=response_headers, body=body)

    def _login_success_response(self) -> RouteEntry:
        """Return login success response.

        When login_success is configured, the response body mirrors those
        key-value pairs so the auth manager's success check passes.
        Otherwise returns a generic {"success": true} body.
        """
        response_headers: list[tuple[str, str]] = [("Content-Type", "application/json")]
        if self._cookie_name:
            response_headers.append(("Set-Cookie", f"{self._cookie_name}={self._SESSION_TOKEN}; Path=/"))
        body = json.dumps(self._login_success if self._login_success else {"success": True})
        return RouteEntry(status=200, headers=response_headers, body=body)


def _derive_key(password: str, salt: str, iterations: int, key_length_bits: int) -> str:
    """Derive a key using PBKDF2-HMAC-SHA256, returned as hex."""
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
        dklen=key_length_bits // 8,
    )
    return dk.hex()


def create_handler(
    modem_config: ModemConfig,
    har_entries: list[dict[str, Any]] | None = None,
) -> FormPbkdf2AuthHandler:
    """Entry point for dynamic auth handler dispatch."""
    from ...models.modem_config.auth import FormPbkdf2Auth

    auth = modem_config.auth
    assert isinstance(auth, FormPbkdf2Auth)

    action_cfg = extract_action_config(modem_config)
    return FormPbkdf2AuthHandler(
        login_endpoint=auth.login_endpoint,
        salt_trigger=auth.salt_trigger,
        pbkdf2_iterations=auth.pbkdf2_iterations,
        pbkdf2_key_length=auth.pbkdf2_key_length,
        double_hash=auth.double_hash,
        csrf_init_endpoint=auth.csrf_init_endpoint,
        csrf_header=auth.csrf_header,
        cookie_name=action_cfg.cookie_name,
        login_success=auth.login_success or None,
    )
