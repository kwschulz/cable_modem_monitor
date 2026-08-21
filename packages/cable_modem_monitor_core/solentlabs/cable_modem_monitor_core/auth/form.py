"""HTML form POST authentication manager.

See MODEM_YAML_SPEC.md ``form`` strategy.
"""

from __future__ import annotations

import base64
import logging
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from ..models.modem_config.auth import FormAuth
from .base import AuthFailureMode, AuthResult, BaseAuthManager

_logger = logging.getLogger(__name__)


class FormAuthManager(BaseAuthManager):
    """HTML form POST login.

    POSTs credentials to a configured endpoint and evaluates the
    response for success. Supports password encoding (plain or base64),
    hidden form fields, login page pre-fetch, a POST URL read off the
    pre-fetched form (``action_source: login_page``), and success
    detection via redirect URL or response body indicator.

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
            1. Pre-fetch login page if configured (to get cookies/nonces,
               and the form action when ``action_source: login_page``).
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
        login_url = f"{base_url}{config.action}"
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

            # Read the POST URL off the form every login: firmware that
            # publishes a per-page-load token in the action (#189) rejects
            # a stale or missing one. Re-read here, never cached, for the
            # same reason the hidden fields are.
            if config.action_source == "login_page":
                published = _login_action_from_page(
                    prefetch_response.text,
                    config.form_selector,
                    prefetch_response.url,
                )
                if published is not None:
                    login_url = published
                else:
                    # Falling back keeps a modem the static URL satisfies
                    # working; ERROR because a declared source that stopped
                    # resolving is a config defect that has to surface.
                    _logger.error(
                        "Login form action not found on %s (form_selector=%r); " "falling back to configured action %s",
                        config.login_page,
                        config.form_selector,
                        config.action,
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


def _parse_login_page(html: str) -> BeautifulSoup | None:
    """Parse the pre-fetched page; ``None`` when there is nothing to read."""
    if not html:
        return None
    try:
        return BeautifulSoup(html, "html.parser")
    except Exception:
        _logger.debug("Failed to parse login page HTML", exc_info=True)
        return None


def _login_form_scope(soup: BeautifulSoup, form_selector: str) -> Tag | BeautifulSoup:
    """The element the login form reads from: selector match, else first form, else the page."""
    if form_selector:
        match = soup.select_one(form_selector)
        if match is not None:
            return match
    form = soup.find("form")
    return form if form is not None else soup


def _login_action_from_page(html: str, form_selector: str, page_url: str) -> str | None:
    """Resolve the login form's ``action`` against the page URL; ``None`` when it cannot be read.

    No form, a selector matching nothing (or not a form), or a form
    without an action all answer ``None``. A selector miss does not
    fall back to the first form here: the declared form is gone, and
    posting to whatever form remains is a guess. Resolution follows the
    browser: a relative ``setup.cgi`` on ``/cgi-bin/login.html`` posts
    to ``/cgi-bin/setup.cgi``.
    """
    soup = _parse_login_page(html)
    if soup is None:
        return None
    form = soup.select_one(form_selector) if form_selector else soup.find("form")
    if not isinstance(form, Tag) or form.name != "form":
        return None
    action = form.get("action")
    if not isinstance(action, str) or not action.strip():
        return None
    return urljoin(page_url, action.strip())


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
    soup = _parse_login_page(html)
    if soup is None:
        return {}

    scope = _login_form_scope(soup, form_selector)

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
