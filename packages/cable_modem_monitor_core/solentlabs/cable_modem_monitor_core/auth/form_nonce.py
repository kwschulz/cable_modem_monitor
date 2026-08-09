"""Form POST with client-generated nonce authentication manager.

See MODEM_YAML_SPEC.md ``form_nonce`` strategy.
"""

from __future__ import annotations

import base64
import logging
import secrets
import string
import urllib.parse
from dataclasses import dataclass
from typing import Literal

import requests
from bs4 import BeautifulSoup, Tag

from ..models.modem_config.auth import FormNonceAuth
from .base import AuthResult, BaseAuthManager

_logger = logging.getLogger(__name__)

# Characters that JavaScript's encodeURIComponent does NOT encode.
_JS_URI_SAFE = "-_.!~*'()"


_CredentialEncoding = Literal["plain", "b64_packed"]


@dataclass(frozen=True)
class _FormDetection:
    """Result of login page form structure analysis."""

    encoding: _CredentialEncoding
    credential_field: str  # empty for plain, field name for packed encodings


class FormNonceAuthManager(BaseAuthManager):
    """Form POST with client-generated nonce.

    Generates a random nonce and POSTs username, password, and nonce
    as form fields. The response is evaluated by text prefix (``Url:``
    for success, ``Error:`` for failure).

    When the login page form contains a hidden ``arguments``-style
    field instead of named credential inputs, credentials are packed
    as ``base64(encodeURIComponent("u=val:p=val"))`` into that field.
    Encoding is determined at setup time (HA config flow or test
    harness) and stored in ``FormNonceAuth.credential_encoding``.

    Args:
        config: Validated ``FormNonceAuth`` config.  The
            ``credential_encoding`` and ``credential_field`` fields
            are populated by the HA config flow (from a live login
            page pre-fetch) or by the test harness (from HAR entries).
            modem.yaml does not set them — they default to ``"plain"``.
    """

    def __init__(self, config: FormNonceAuth) -> None:
        self._config = config

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
        """Execute the nonce-based login flow.

        Steps:
            1. Generate a random numeric nonce.
            2. Build form data (plain fields or b64-packed).
            3. POST with ``X-Requested-With`` header.
            4. Parse the text-prefix response for success/error.

        Args:
            session: Session to configure with auth state.
            base_url: Modem base URL.
            username: Username credential.
            password: Password credential.
            timeout: Per-request timeout in seconds.
            log_level: Log level for non-error messages.

        Returns:
            AuthResult with login response.
        """
        config = self._config
        login_url = f"{base_url}{config.action}"
        encoding = config.credential_encoding

        # Step 1: Generate nonce (digits only, per spec)
        nonce = "".join(secrets.choice(string.digits) for _ in range(config.nonce_length))

        # Step 2: Build form data based on config encoding
        if encoding == "b64_packed":
            packed = _pack_b64_credentials(
                config.username_field,
                username,
                config.password_field,
                password,
            )
            form_data = {
                config.credential_field: packed,
                config.nonce_field: nonce,
            }
        else:
            form_data = {
                config.username_field: username,
                config.password_field: password,
                config.nonce_field: nonce,
            }

        # Step 3: POST with X-Requested-With (both firmware variants
        # send this via jQuery $.ajax — per-request, not session-wide).
        try:
            response = session.post(
                login_url,
                data=form_data,
                headers={"X-Requested-With": "XMLHttpRequest"},
                allow_redirects=False,
                timeout=timeout,
            )
        except requests.RequestException as e:
            if isinstance(e, requests.ConnectionError | requests.Timeout):
                raise
            return AuthResult(
                success=False,
                error=f"Nonce login POST failed: {type(e).__name__}: {e}",
            )

        # An HTTP error is a failed login before the body is worth reading:
        # this strategy judges by text prefix alone, so a 401 or 503 whose
        # body simply lacks the error prefix used to fall through as success.
        # The threshold is >= 400 rather than != 200 because this POST does
        # not follow redirects, so a 302 is a normal answer here.
        if response.status_code >= 400:
            return AuthResult(
                success=False,
                error=f"Login returned HTTP {response.status_code}",
                response=response,
            )

        # Step 4: Parse text-prefix response
        text = response.text.strip()

        if text.startswith(config.error_prefix):
            error_msg = text[len(config.error_prefix) :].strip()
            return AuthResult(
                success=False,
                error=f"Login rejected: {error_msg}",
                response=response,
            )

        redirect_hint = ""
        if text.startswith(config.success_prefix):
            redirect_hint = text[len(config.success_prefix) :].strip()

        _logger.log(
            log_level,
            "Nonce login succeeded: status=%d, redirect=%s, encoding=%s",
            response.status_code,
            redirect_hint,
            encoding,
        )

        # Do not populate response_url — the response body is the text
        # prefix ("Url:/path"), not the content of the redirect target.
        # The loader would incorrectly reuse this body as page content.
        return AuthResult(
            success=True,
            response=response,
        )


def _analyze_login_form(
    html: str,
    username_field: str,
    nonce_field: str,
    *,
    log_level: int = logging.DEBUG,
) -> _FormDetection:
    """Analyze a login form to determine credential encoding.

    Pure function — no I/O.

    Args:
        html: Raw HTML of the login page.
        username_field: Config field name for the username credential.
        nonce_field: Config field name for the nonce (excluded from
            packed-credential detection).
        log_level: Log level for diagnostic messages.

    Returns:
        Detection result.
    """
    _plain = _FormDetection(encoding="plain", credential_field="")

    if not html:
        return _plain

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        _logger.debug("Failed to parse login page HTML", exc_info=True)
        return _plain

    # Find the login form (first <form> on the page).
    form_tag = soup.find("form")
    if not isinstance(form_tag, Tag):
        _logger.debug("No <form> found in login page, using plain encoding")
        return _plain

    # Check: does the form have a named credential input?
    has_named_credential = form_tag.find("input", attrs={"name": username_field}) is not None
    if has_named_credential:
        _logger.log(
            log_level,
            "Login form has name=%r input — using plain credential encoding",
            username_field,
        )
        return _plain

    # No named credential input — look for a packed credential hidden
    # field: a hidden input with an empty value that isn't the nonce.
    credential_field = ""
    for inp in form_tag.find_all("input", attrs={"type": "hidden"}):
        name = inp.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name == nonce_field:
            continue
        value = inp.get("value", "")
        if not value:
            credential_field = name
            break

    if credential_field:
        _logger.log(
            log_level,
            "Login form has packed credential field %r — using b64 encoding",
            credential_field,
        )
        return _FormDetection(encoding="b64_packed", credential_field=credential_field)

    _logger.debug("No packed credential field found in login form, using plain encoding")
    return _plain


def _pack_b64_credentials(
    username_field: str,
    username: str,
    password_field: str,
    password: str,
) -> str:
    """Pack credentials as base64-encoded URI-component pairs.

    Reproduces the JavaScript pattern::

        Base64.encode(
            encodeURIComponent("username=" + user) + ":"
            + encodeURIComponent("password=" + pass)
        )

    Verified byte-for-byte against SB6190 firmware 9.1.103AA72 HAR.

    Args:
        username_field: Key name for username (e.g. ``"username"``).
        username: Username value.
        password_field: Key name for password (e.g. ``"password"``).
        password: Password value.

    Returns:
        Base64-encoded string for the packed form field.
    """
    part1 = urllib.parse.quote(f"{username_field}={username}", safe=_JS_URI_SAFE)
    part2 = urllib.parse.quote(f"{password_field}={password}", safe=_JS_URI_SAFE)
    return base64.b64encode(f"{part1}:{part2}".encode()).decode("ascii")


def create_manager(config: FormNonceAuth) -> FormNonceAuthManager:
    """Entry point for dynamic auth factory dispatch."""
    return FormNonceAuthManager(config)
