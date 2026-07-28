"""Helpers that build and PII-sanitize auth-failure events."""

from __future__ import annotations

from typing import Any, Final
from urllib.parse import urlparse, urlunparse

import requests

from ..auth.base import AuthFailureMode
from .events import AuthFailed, HttpStatusError

# Maximum response body characters stored in auth-failure logs (log-line budget).
_FAILURE_BODY_SNIPPET_MAX: Final[int] = 500
_REDACTED: Final[str] = "[REDACTED]"


_HINTS: Final[dict[AuthFailureMode, str]] = {
    AuthFailureMode.NOT_CONFIGURED: "modem requires authentication (check config)",
    AuthFailureMode.CREDENTIALS_SUSPECT: "credentials rejected",
    AuthFailureMode.SESSION_REJECTED: "session expired",
}


def _auth_failure_hint(auth_manager: Any) -> str:
    """Return a context-appropriate hint for a 401/403 during resource loading."""
    # The strategy owns this, not an isinstance chain here: the answer
    # depends on whether its login flow verifies the credential, which
    # only the strategy knows. See BaseAuthManager.auth_failure_mode.
    return _HINTS[auth_manager.auth_failure_mode()]


def _build_http_status_error_event(
    model: str,
    path: str,
    status_code: int | None,
    reason: str,
    request_line: str,
    content_type: str,
    response_body: str,
    password: str,
) -> HttpStatusError:
    """Build a sanitized HttpStatusError carrying 401/403 wire detail."""
    body = _scrub_password(response_body[:_FAILURE_BODY_SNIPPET_MAX], password)
    if len(response_body) > _FAILURE_BODY_SNIPPET_MAX:
        body = body + "... (truncated)"

    return HttpStatusError(
        model=model,
        path=path,
        status_code=status_code,
        reason=reason,
        request_line=request_line,
        content_type=content_type,
        response_body=body,
    )


def _should_detect_login_pages(modem_config: Any) -> bool:
    """Determine if login page detection should be enabled.

    Derived from the strategy's own ClassVars, not an isinstance
    chain: a stateless strategy holds no session that can expire, and
    only the HTTP loader can substitute a login page for a data page.
    See ARCHITECTURE.md § Auth Manager for the published table.
    """
    auth = modem_config.auth
    if auth is None:
        return False
    return not auth.stateless and auth.transport == "http"


# ---------------------------------------------------------------------------
# Auth-failure event builder
# ---------------------------------------------------------------------------


def _build_auth_failed_event(
    model: str,
    strategy: str,
    response: requests.Response | None,
    error: str,
    password: str,
) -> AuthFailed:
    """Build a sanitized AuthFailed event from raw auth result data."""
    if response is None:
        return AuthFailed(
            model=model,
            strategy=strategy,
            error=error,
            method=None,
            url=None,
            status_code=None,
            content_type=None,
            response_body=None,
        )

    method = response.request.method if response.request else "?"
    url = _strip_url_query(response.url)
    status = response.status_code
    content_type = response.headers.get("Content-Type", "")
    body_snippet = _scrub_password(response.text[:_FAILURE_BODY_SNIPPET_MAX], password)
    if len(response.text) > _FAILURE_BODY_SNIPPET_MAX:
        body_snippet = body_snippet + "... (truncated)"

    return AuthFailed(
        model=model,
        strategy=strategy,
        error=error,
        method=method,
        url=url,
        status_code=status,
        content_type=content_type,
        response_body=body_snippet,
    )


def _strip_url_query(url: str) -> str:
    """Replace any URL query string with ``?<redacted>``.

    URL-token strategies put base64-encoded credentials directly in
    the query (``?<base64(user:password)>``). Stripping the query
    wholesale is conservative — non-credential queries lose
    visibility, but they're rare in modem auth and not worth the
    field-name guessing it would take to differentiate.
    """
    parsed = urlparse(url)
    if not parsed.query:
        return url
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "<redacted>", parsed.fragment))


def _strategy_name(modem_config: Any) -> str:
    """Return the auth strategy name for the failure log (e.g., ``"form"``)."""
    auth = getattr(modem_config, "auth", None)
    return getattr(auth, "strategy", "none") if auth is not None else "none"


def _scrub_password(text: str, password: str) -> str:
    """Replace literal occurrences of the user's password with ``[REDACTED]``.

    Catches the common cases:

    - Modem error pages that echo the submitted password back.
    - Form-encoded request bodies that some modems include in their
      response (rare but observed).

    Does not attempt to scrub derived forms (PBKDF2 hashes, encrypted
    blobs) — those are protocol-shaped credentials, not the user's
    secret, and reversing them isn't trivial enough to leak useful
    info from a body snippet.
    """
    if not password:
        return text
    return text.replace(password, _REDACTED)
