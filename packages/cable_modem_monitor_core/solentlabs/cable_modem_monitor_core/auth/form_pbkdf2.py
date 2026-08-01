"""Multi-round-trip PBKDF2 challenge-response authentication manager.

See MODEM_YAML_SPEC.md ``form_pbkdf2`` strategy.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import requests

from ..models.modem_config.auth import FormPbkdf2Auth
from .base import AuthFailureMode, AuthResult, BaseAuthManager
from .response import parse_json_dict, post_form

_logger = logging.getLogger(__name__)


class FormPbkdf2AuthManager(BaseAuthManager):
    """Multi-round-trip PBKDF2 challenge-response auth.

    The client requests server-provided salts, derives a key via
    PBKDF2, and submits the derived hash. Supports CSRF token
    acquisition from a separate init endpoint.

    Args:
        config: Validated ``FormPbkdf2Auth`` config from modem.yaml.
    """

    def __init__(self, config: FormPbkdf2Auth) -> None:
        self._config = config

    def auth_failure_mode(self) -> AuthFailureMode:
        """Login is verified: a wrong password fails the login_success check."""
        # Proven by test_auth_failure_modes — the mock server rejects a
        # derived-key mismatch, so a later 401 is not the credential.
        return AuthFailureMode.SESSION_REJECTED

    def headers(self) -> frozenset[str]:
        names = {"cookie"}
        if self._config.csrf_header:
            names.add(self._config.csrf_header.lower())
        return frozenset(names)

    def authenticate(
        self,
        session: requests.Session,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: int = 10,
        log_level: int = logging.DEBUG,
    ) -> AuthResult:
        """Execute the PBKDF2 challenge-response login flow.

        Steps:
            1. Fetch CSRF token if ``csrf_init_endpoint`` is configured.
            2. POST salt trigger to get server salts.
            3. Derive key via PBKDF2 with server salt.
            4. Optionally double-hash with ``saltwebui``.
            5. POST derived hash to complete login.

        Args:
            session: Session to configure with auth state.
            base_url: Modem base URL.
            username: Username credential.
            password: Password credential.
            timeout: Per-request timeout in seconds.

        Returns:
            AuthResult with login response.
        """
        config = self._config
        login_url = f"{base_url}{config.login_endpoint}"

        # Step 1: Fetch CSRF token
        csrf_token = ""
        if config.csrf_init_endpoint:
            csrf_token = _fetch_csrf_token(
                session,
                f"{base_url}{config.csrf_init_endpoint}",
                config.csrf_header,
                timeout,
            )
            if csrf_token and config.csrf_header:
                session.headers[config.csrf_header] = csrf_token

        # Step 2: Request server salts
        salt_result = _request_salts(session, login_url, username, config.salt_trigger, timeout)
        if isinstance(salt_result, AuthResult):
            return salt_result
        salt_json = salt_result

        # Step 3–4: Derive key (with optional double-hash)
        derived = _derive_key(password, salt_json["salt"], config.pbkdf2_iterations, config.pbkdf2_key_length)
        if config.double_hash:
            salt_webui = salt_json.get("saltwebui", salt_json["salt"])
            derived = _derive_key(derived, salt_webui, config.pbkdf2_iterations, config.pbkdf2_key_length)

        # Step 5: Login with derived hash
        login_result = _submit_login(session, login_url, username, derived, timeout, login_success=config.login_success)
        if isinstance(login_result, AuthResult):
            return login_result
        response = login_result

        _logger.log(
            log_level,
            "PBKDF2 login succeeded: status=%d, cookies=%s",
            response.status_code,
            list(session.cookies.keys()),
        )

        return AuthResult(
            success=True,
            response=response,
            response_url=config.login_endpoint,
        )


def _fetch_csrf_token(
    session: requests.Session,
    url: str,
    header_name: str,
    timeout: int,
) -> str:
    """Fetch a CSRF token from the init endpoint.

    Tries JSON response body first, then response headers.
    """
    try:
        resp = session.get(url, timeout=timeout)
    except requests.RequestException as e:
        if isinstance(e, requests.ConnectionError | requests.Timeout):
            raise
        _logger.debug("CSRF init endpoint unreachable: %s", url)
        return ""

    # Try JSON body (e.g., {"token": "..."})
    try:
        data = resp.json()
        if isinstance(data, dict):
            for key in ("token", "csrf", "csrfToken"):
                if key in data:
                    return str(data[key])
    except ValueError:
        pass

    # Try response header
    if header_name:
        token = resp.headers.get(header_name, "")
        if token:
            return str(token)

    return ""


def _request_salts(
    session: requests.Session,
    login_url: str,
    username: str,
    salt_trigger: str,
    timeout: int,
) -> dict[str, str] | AuthResult:
    """POST the salt trigger and return the salt JSON dict.

    Returns AuthResult on failure, dict on success.
    """
    salt_data = {"username": username, "password": salt_trigger}
    result = post_form(
        session,
        login_url,
        salt_data,
        timeout,
        context="Salt response",
    )
    if isinstance(result, AuthResult):
        return result
    response, salt_json = result

    if not salt_json.get("salt"):
        return AuthResult(success=False, error="No salt in server response", response=response)

    return salt_json


def _submit_login(
    session: requests.Session,
    login_url: str,
    username: str,
    derived_key: str,
    timeout: int,
    *,
    login_success: dict[str, Any] | None = None,
) -> requests.Response | AuthResult:
    """POST the derived key and validate the login response.

    Returns AuthResult on failure, Response on success.
    """
    login_data = {"username": username, "password": derived_key}
    try:
        response = session.post(login_url, data=login_data, timeout=timeout)
    except requests.RequestException as e:
        if isinstance(e, requests.ConnectionError | requests.Timeout):
            raise
        return AuthResult(success=False, error=f"Login POST failed: {type(e).__name__}: {e}")

    # Check HTTP-level failure before attempting JSON parse —
    # a 401 with an HTML body should not trigger a JSON parse error.
    if response.status_code == 401:
        return AuthResult(success=False, error="Login returned 401 Unauthorized", response=response)

    result_json = parse_json_dict(response, context="Login response")
    if isinstance(result_json, AuthResult):
        return result_json

    if login_success:
        # Firmware signals success via a specific key-value pair (e.g. {"error": "ok"}).
        # Fail if any required field is absent or has a different value.
        if not all(result_json.get(k) == v for k, v in login_success.items()):
            return AuthResult(
                success=False,
                error=f"Login rejected: {result_json.get('message', 'unknown error')}",
                response=response,
            )
    else:
        # Default: any truthy "error" field means login failed.
        if result_json.get("error"):
            return AuthResult(
                success=False,
                error=f"Login rejected: {result_json.get('message', 'unknown error')}",
                response=response,
            )

    return response


def _derive_key(
    password: str,
    salt: str,
    iterations: int,
    key_length_bits: int,
) -> str:
    """Derive a key using PBKDF2-HMAC-SHA256.

    Returns the derived key as a hex string.
    """
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
        dklen=key_length_bits // 8,
    )
    return dk.hex()


def create_manager(config: FormPbkdf2Auth) -> FormPbkdf2AuthManager:
    """Entry point for dynamic auth factory dispatch."""
    return FormPbkdf2AuthManager(config)
