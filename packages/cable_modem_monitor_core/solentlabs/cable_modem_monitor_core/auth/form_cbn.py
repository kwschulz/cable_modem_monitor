"""CBN (Compal Broadband Networks) AES-256-CBC encrypted form auth manager.

Replicates the browser-side login flow from Compal modem firmware:

1. **GET login page** — receive ``sessionToken`` cookie.
2. **Encrypt password** — ``compal_encrypt(password, sessionToken)``
   using AES-256-CBC with key=SHA256(token), IV=MD5(token).
3. **POST login** — send to ``setter_endpoint`` with
   ``token=<tok>&fun=<login_fun>&Username=<username>&Password=<encrypted>``.
4. **Check response** — body contains ``"successful"`` and ``SID=<N>``.
   A body without it carries one of four other firmware outcomes rather
   than always meaning a wrong password; see ``_classify_login_failure``
   and AUTH_CBN_SPEC.md § Login Token Vocabulary.
5. **Set SID cookie** — extracted from response body, stored on session.

``ConnectionError`` and ``Timeout`` are re-raised for the collector to
classify as ``CONNECTIVITY`` (UC-30/UC-31). An unreachable modem has
judged no credential, and reporting one as a failed login trips the
circuit breaker on the first occurrence (#200).

Requires the ``cryptography`` package: install Core with ``[cbn]``.

See MODEM_YAML_SPEC.md ``form_cbn`` strategy.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from urllib.parse import urlparse

import requests

from ..models.modem_config.auth import FormCbnAuth
from ..protocol.cbn import compal_encrypt
from .base import AuthContext, AuthResult, BaseAuthManager, LoginLockoutError

_logger = logging.getLogger(__name__)

_SID_RE = re.compile(r"SID=(\d+)")

# Login body tokens, grouped by what the firmware means by each. The set is
# dictated by the CH7465MT capture, not chosen here: the inline handler in
# common_page/login.html and js/common_api.js. AUTH_CBN_SPEC.md § Login
# Token Vocabulary carries the reasoning per group, and the catalog gate
# test_cbn_login_token_coverage.py reads the tokens back out of each entry's
# captured firmware JS so a new firmware line cannot add one unnoticed.
_LOCKOUT_TOKENS = frozenset({"lockedout", "cbnAccessDenied"})
_RESTART_TOKENS = frozenset({"cbnLogin", "cbnFirstInstall"})
_BLOCKED_TOKENS = frozenset({"cbnBlockContent"})

HANDLED_LOGIN_TOKENS = _LOCKOUT_TOKENS | _RESTART_TOKENS | _BLOCKED_TOKENS


def _first_token(body: str, tokens: frozenset[str]) -> str:
    """The first token from ``tokens`` the login body carries, or ``""``."""
    # The firmware tests most tokens with response.match(), a substring
    # test; login.html compares "lockedout" with == instead. Substring is
    # used uniformly here because it is a superset and each token is
    # distinctive enough that no other body carries one.
    return next((token for token in sorted(tokens) if token in body), "")


def _classify_login_failure(body: str, response: requests.Response) -> AuthResult:
    """Map a login body carrying no "successful" onto the firmware's own outcome."""
    # Checked by consequence, not in firmware source order: the two captured
    # handlers disagree on order, and the tokens are mutually exclusive in
    # every observed response.
    token = _first_token(body, _LOCKOUT_TOKENS)
    if token:
        # Access-denied.html in the firmware. Distinct from a rejected
        # credential (#117): the modem is refusing logins to protect itself,
        # so the remedy is waiting, not a new password.
        raise LoginLockoutError(f"CBN firmware anti-brute-force triggered: {token}")

    token = _first_token(body, _RESTART_TOKENS)
    if token:
        # The firmware answers these by navigating back to login.html with no
        # message to the user: session state is stale, so start over. It is
        # not a verdict on the credential, and reading it as one trips the
        # breaker on the first occurrence and stops polling for good. busy
        # routes it to AUTH_UNAVAILABLE, which leaves polling running so the
        # condition clears (UC-87a).
        return AuthResult(
            success=False,
            busy=True,
            error=f"CBN login must be restarted: {token}",
            response=response,
        )

    token = _first_token(body, _BLOCKED_TOKENS)
    if token:
        # Blocked-content.html, which is not the password-error branch. What
        # the token means is unestablished in the capture, so Core claims no
        # recovery and treats it as a rejection, naming it so a field report
        # is distinguishable from a real bad password. AUTH_CBN_SPEC.md
        # § Known Gaps.
        return AuthResult(
            success=False,
            error=f"CBN login refused: {token}",
            response=response,
        )

    # ShowPasswordError() in the firmware: the credential was judged wrong.
    return AuthResult(
        success=False,
        error=f"Login failed: {body[:200]}",
        response=response,
    )


def _send(send: Callable[[], requests.Response], context: str) -> requests.Response | AuthResult:
    """Send a request, letting a connectivity failure through unconverted.

    ``ConnectionError`` and ``Timeout`` mean the modem never answered, so
    they belong to the collector as ``CONNECTIVITY`` (UC-30/UC-31).
    Everything else is a real answer this strategy can report on. Mirrors
    the contract ``auth/response.py`` states for the JSON strategies.
    """
    try:
        return send()
    except requests.RequestException as exc:
        if isinstance(exc, requests.ConnectionError | requests.Timeout):
            raise
        return AuthResult(success=False, error=f"{context}: {type(exc).__name__}: {exc}")


class FormCbnAuthManager(BaseAuthManager):
    """CBN AES-256-CBC encrypted form auth.

    Args:
        config: Validated ``FormCbnAuth`` config from modem.yaml.
    """

    def __init__(self, config: FormCbnAuth) -> None:
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
        """Execute the CBN encrypted login flow.

        Args:
            session: Session to configure with auth state.
            base_url: Modem base URL.
            username: Username credential (ignored — CBN uses username_value).
            password: Password credential.
            timeout: Per-request timeout in seconds.
            log_level: Logging level for non-error messages.

        Returns:
            AuthResult with success flag.
        """
        log = _logger.log
        config = self._config

        # Step 1: GET login page to receive sessionToken cookie
        login_page_url = f"{base_url}{config.login_page}"
        log(log_level, "CBN auth: fetching login page %s", login_page_url)
        outcome = _send(lambda: session.get(login_page_url, timeout=timeout), "Failed to fetch login page")
        if isinstance(outcome, AuthResult):
            return outcome
        response = outcome

        if not response.ok:
            return AuthResult(
                success=False,
                error=f"Login page returned HTTP {response.status_code}",
                response=response,
            )

        # Step 2: Read sessionToken from cookies
        session_token = session.cookies.get(config.session_cookie_name)
        if not session_token:
            return AuthResult(
                success=False,
                error=f"Login page did not set '{config.session_cookie_name}' cookie",
                response=response,
            )

        # Step 3: Encrypt password
        try:
            encrypted = compal_encrypt(password, session_token)
        except ImportError as exc:
            return AuthResult(success=False, error=str(exc))

        # Step 4: POST login — token must be first parameter
        setter_url = f"{base_url}{config.setter_endpoint}"
        post_body = (
            f"token={session_token}"
            f"&fun={config.login_fun}"
            f"&Username={config.username_value}"
            f"&Password={encrypted}"
        )
        log(log_level, "CBN auth: posting login to %s", setter_url)
        outcome = _send(
            lambda: session.post(
                setter_url,
                data=post_body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=timeout,
                allow_redirects=False,
            ),
            "Login POST failed",
        )
        if isinstance(outcome, AuthResult):
            return outcome
        login_response = outcome

        # Step 5: Check response for success + extract SID
        # Status must be 200 — a 302 redirect to the login page would
        # contain "successful" in JS templates, causing false-positive.
        if login_response.status_code != 200:
            return AuthResult(
                success=False,
                error=f"Login POST returned HTTP {login_response.status_code}",
                response=login_response,
            )

        body = login_response.text
        if "successful" not in body.lower():
            return _classify_login_failure(body, login_response)

        sid_match = _SID_RE.search(body)
        if not sid_match:
            return AuthResult(
                success=False,
                error="Login successful but SID not found in response",
                response=login_response,
            )

        # Step 6: Set SID cookie on session
        sid_value = sid_match.group(1)
        hostname = urlparse(base_url).hostname or ""
        session.cookies.set(config.sid_cookie_name, sid_value, domain=hostname)
        log(log_level, "CBN auth: SID=%s set on session", sid_value)

        return AuthResult(
            success=True,
            auth_context=AuthContext(),
            response=login_response,
            response_url=config.setter_endpoint,
        )


def create_manager(config: FormCbnAuth) -> FormCbnAuthManager:
    """Entry point for dynamic auth factory dispatch."""
    return FormCbnAuthManager(config)
