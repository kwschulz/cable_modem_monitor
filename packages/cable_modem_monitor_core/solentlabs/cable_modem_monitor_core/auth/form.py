"""HTML form POST authentication manager.

See MODEM_YAML_SPEC.md ``form`` strategy.
"""

from __future__ import annotations

import base64
import logging
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, Tag

from ..models.modem_config.auth import FormAuth
from .base import AuthFailureMode, AuthResult, BaseAuthManager

_logger = logging.getLogger(__name__)


class FormAuthManager(BaseAuthManager):
    """HTML form POST login.

    POSTs credentials to a configured endpoint and evaluates the
    response for success. Supports password encoding (plain or base64),
    hidden form fields, login page pre-fetch, and success detection
    via redirect URL or response body indicator.

    Args:
        config: Validated ``FormAuth`` config from modem.yaml.
    """

    def __init__(self, config: FormAuth) -> None:
        self._config = config

    def auth_failure_mode(self) -> AuthFailureMode:
        """Login is verified only when success criteria are declared."""
        # Proven by test_auth_failure_modes — with a criterion set, a modem
        # re-rendering its login page fails the check, so a later 401 is not
        # the credential. FormSuccess rejects a block naming no criterion, so
        # a present block always checks something; without that guarantee this
        # would claim a verification that never ran.
        if self._config.success is not None:
            return AuthFailureMode.SESSION_REJECTED
        return super().auth_failure_mode()

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
        """Execute the form login flow.

        Steps:
            1. Pre-fetch login page if configured (to get cookies/nonces).
            2. Build form data with credentials and hidden fields.
            3. POST to the login endpoint.
            4. Evaluate success via redirect or indicator.

        Args:
            session: Session to configure with auth state.
            base_url: Modem base URL.
            username: Username credential.
            password: Password credential.
            timeout: Per-request timeout in seconds.

        Returns:
            AuthResult with login response for auth response reuse.
        """
        config = self._config

        # Step 1: Pre-fetch login page if configured (for cookies/nonces)
        discovered_fields: dict[str, str] = {}
        if config.login_page:
            try:
                prefetch_response = session.get(
                    f"{base_url}{config.login_page}",
                    timeout=timeout,
                )
            except requests.RequestException as e:
                if isinstance(e, requests.ConnectionError | requests.Timeout):
                    raise
                return AuthResult(
                    success=False,
                    error=f"Login page pre-fetch failed: {type(e).__name__}: {e}",
                )

            # Read hidden fields from the login form
            discovered_fields = _discover_hidden_fields(
                prefetch_response.text,
                config.form_selector,
            )

        # Step 2: Build form data
        # Merge order: discovered fields <- hidden_fields <- credentials
        encoded_password = _encode_password(password, config.encoding)
        form_data: dict[str, str] = {}
        form_data.update(discovered_fields)
        form_data.update(config.hidden_fields)
        form_data[config.username_field] = username
        for field_name in config.password_field:
            form_data[field_name] = encoded_password

        # Step 3: POST to login endpoint with Referer header.
        # Some modem firmware rejects login POSTs without a matching
        # Referer header (defensive measure from v3.13, HAR evidence
        # shows 60% of modems send it, none reject it).
        login_url = f"{base_url}{config.action}"
        try:
            response = session.request(
                config.method,
                login_url,
                data=form_data,
                headers={"Referer": base_url},
                allow_redirects=True,
                timeout=timeout,
            )
        except requests.RequestException as e:
            if isinstance(e, requests.ConnectionError | requests.Timeout):
                raise
            return AuthResult(
                success=False,
                error=f"Login POST failed: {type(e).__name__}: {e}",
            )

        # Step 4: Evaluate success
        error = _check_success(config, response)
        if error:
            return AuthResult(success=False, error=error, response=response)

        response_path = urlparse(response.url).path if response.url else ""

        return AuthResult(
            success=True,
            response=response,
            response_url=response_path,
        )


def _encode_password(password: str, encoding: str) -> str:
    """Encode the password per the configured encoding."""
    if encoding == "base64":
        return base64.b64encode(password.encode("utf-8")).decode("ascii")
    return password


def _check_success(config: FormAuth, response: requests.Response) -> str:
    """Check if the login response indicates success.

    Returns an error message on failure, empty string on success.
    """
    # An HTTP error is a failed login whatever the criteria say. This guard
    # used to sit on the no-criteria branch alone, so declaring `success`
    # dropped it and an empty block turned a 401 refusal into a success.
    # Criteria must narrow what counts as success, never widen it.
    if response.status_code >= 400:
        return f"Login returned HTTP {response.status_code}"

    if config.success is None:
        # No explicit success criteria — accept any non-error response
        return ""

    if config.success.redirect:
        response_path = urlparse(response.url).path if response.url else ""
        if config.success.redirect not in response_path:
            return (
                f"Login redirect mismatch: expected path containing "
                f"'{config.success.redirect}', got '{response_path}'"
            )

    if config.success.indicator and config.success.indicator not in response.text:
        return f"Login success indicator '{config.success.indicator}' not found in response body"

    return ""


def _discover_hidden_fields(html: str, form_selector: str) -> dict[str, str]:
    """Read ``<input type="hidden">`` fields from the login form.

    Part of the auth handshake: discovers hidden fields (CSRF tokens,
    mode flags, etc.) that the form expects to be submitted alongside
    credentials. Only collects ``type="hidden"`` inputs — not text,
    password, or other input types.

    Args:
        html: Raw HTML string from the login page pre-fetch.
        form_selector: CSS selector to identify the login form.
            If empty, uses the first ``<form>`` found, or falls back
            to page-level hidden inputs.

    Returns:
        Dict of field name to value. Empty dict on any failure.
    """
    if not html:
        return {}

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        _logger.debug("Failed to parse login page HTML", exc_info=True)
        return {}

    scope: Tag | BeautifulSoup = soup
    if form_selector:
        match = soup.select_one(form_selector)
        if match is not None:
            scope = match
    if scope is soup:
        form = soup.find("form")
        if form is not None:
            scope = form

    fields: dict[str, str] = {}
    for inp in scope.find_all("input", attrs={"type": "hidden"}):
        name = inp.get("name")
        if isinstance(name, str) and name:
            value = inp.get("value", "")
            fields[name] = value if isinstance(value, str) else ""

    if fields:
        _logger.debug("Discovered %d hidden field(s) from login form", len(fields))

    return fields


def create_manager(config: FormAuth) -> FormAuthManager:
    """Entry point for dynamic auth factory dispatch."""
    return FormAuthManager(config)
