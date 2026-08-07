"""Step 2: Auth flow validation.

Checks the first request to determine if the HAR captured a pre-auth
flow (login visible) or is post-auth only (browser had existing session),
and that a login answering a redirect went somewhere the capture recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from ..analysis.auth.patterns import (
    get_login_url_patterns,
    get_session_cookie_indicators,
    has_credential_fields,
)
from .har_utils import (
    HARD_STOP_PREFIX,
    WARNING_PREFIX,
    has_set_cookie,
    is_hnap_request,
    lower_headers,
    path_from_url,
)

# Redirect chains longer than this are a capture artefact, not a login flow.
_MAX_REDIRECT_HOPS = 5

# Domain-specific: modem session cookie name indicators.
# Loaded from auth_patterns.json — single source of truth.
_SESSION_COOKIE_INDICATORS: frozenset[str] = get_session_cookie_indicators()

# Login endpoint patterns (shared via auth_patterns.json)
_LOGIN_URL_PATTERNS: tuple[str, ...] = get_login_url_patterns()


@dataclass
class AuthArtifacts:
    """Auth-related signals found across HAR entries."""

    any: bool = False
    login_post: bool = False
    hnap: bool = False


def validate_auth_flow(entries: list[dict[str, Any]], issues: list[str]) -> bool:
    """Check whether the HAR contains an auth flow. Returns True if detected.

    Appends HARD STOP issues for: session cookies on first request,
    Authorization header on first request (post-auth HAR).
    """
    first_req = entries[0]["request"]
    first_resp = entries[0]["response"]
    first_status = first_resp.get("status", 0)

    # First request carries session cookies -> post-auth HAR
    session_cookie_names = _get_session_cookie_names(first_req.get("cookies", []))
    if session_cookie_names:
        issues.append(
            f"{HARD_STOP_PREFIX} First request carries session cookies "
            f"({', '.join(session_cookie_names)}) — browser had existing session. "
            f"Please recapture using incognito/private browsing."
        )
        return False

    # 401/403 -> pre-auth captured, auth challenge visible
    if first_status in (401, 403):
        return True

    # 301/302 -> redirect to login page
    if first_status in (301, 302):
        return True

    # 200 -> could be no-auth, login page, or post-auth
    if first_status == 200:
        return _classify_first_200(entries, issues)

    # Other statuses (500, etc.) — unusual, not auth flow
    return False


def validate_auth_redirect_landing(entries: list[dict[str, Any]], issues: list[str]) -> None:
    """Warn when a login redirect points at a page the capture never recorded.

    Core posts the login with ``allow_redirects=True`` and evaluates
    ``auth.success.redirect`` against where it lands, so the landing page
    is part of the auth flow, not a page the capture may skip. Without it
    the entry cannot be replay-tested: the mock server answers the
    redirect 404 and the login reads as failed. A warning rather than a
    hard stop, because the data pages may all be present and the entry
    still parses; what is lost is auth replay coverage.
    """
    responses = _responses_by_path(entries)

    for entry in entries:
        request = entry["request"]
        if request.get("method") != "POST" or not _is_login_url(request.get("url", "")):
            continue
        if not has_credential_fields(request.get("postData", {})):
            continue
        _walk_redirect_chain(entry, responses, issues)


def _walk_redirect_chain(
    login_entry: dict[str, Any],
    responses: dict[str, dict[str, Any]],
    issues: list[str],
) -> None:
    """Follow a login's redirects through the capture, warning at the first gap."""
    url = login_entry["request"].get("url", "")
    response = login_entry["response"]

    for _ in range(_MAX_REDIRECT_HOPS):
        if not 300 <= response.get("status", 0) < 400:
            return  # Landed on a captured response.

        location = lower_headers(response).get("location", "")
        if not location:
            return  # Redirect without a target; nothing to check.

        # Location may be relative ("at_a_glance.jst") or absolute
        # ("/at_a_glance.jst"); both appear in the fleet's captures.
        url = urljoin(url, location)
        path = path_from_url(url)

        landing = responses.get(path)
        if landing is None:
            issues.append(
                f"{WARNING_PREFIX} Login redirects to {path}, which the capture "
                f"does not contain. Auth cannot be replay-tested from this HAR — "
                f"recapture, following the redirect to the page the modem lands on."
            )
            return
        response = landing


def _responses_by_path(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index responses by request path, keeping the first response per path."""
    by_path: dict[str, dict[str, Any]] = {}
    for entry in entries:
        path = path_from_url(entry["request"].get("url", ""))
        by_path.setdefault(path, entry["response"])
    return by_path


def _classify_first_200(entries: list[dict[str, Any]], issues: list[str]) -> bool:
    """Classify a 200 first response as login page, no-auth, or post-auth."""
    artifacts = _scan_auth_artifacts(entries)

    if not artifacts.any:
        return False

    if artifacts.login_post or artifacts.hnap:
        return True

    # Has auth artifacts but no clear login flow — check for post-auth
    if _has_authorization_on_first_request(entries):
        issues.append(
            f"{HARD_STOP_PREFIX} First request carries Authorization header — "
            "browser had existing session. Please recapture using "
            "incognito/private browsing."
        )
        return False

    return True


def _scan_auth_artifacts(entries: list[dict[str, Any]]) -> AuthArtifacts:
    """Scan all entries for auth-related artifacts."""
    artifacts = AuthArtifacts()

    for i, entry in enumerate(entries):
        req = entry["request"]
        resp = entry["response"]
        method = req.get("method", "")
        url = req.get("url", "")
        req_hdrs = lower_headers(req)

        if is_hnap_request(url, req_hdrs):
            artifacts.hnap = artifacts.any = True

        # A POST without credential-shaped fields is an action posted to the
        # auth endpoint, not a login; a HAR with no login must not report
        # an auth flow.
        if method == "POST" and _is_login_url(url) and has_credential_fields(req.get("postData", {})):
            artifacts.login_post = artifacts.any = True

        if "authorization" in req_hdrs:
            artifacts.any = True

        if i > 0 and has_set_cookie(resp):
            artifacts.any = True

    return artifacts


def _has_authorization_on_first_request(entries: list[dict[str, Any]]) -> bool:
    """Check if the first request has an Authorization header (post-auth HAR)."""
    all_200 = all(e["response"].get("status") == 200 for e in entries)
    if not all_200:
        return False
    return "authorization" in lower_headers(entries[0]["request"])


def _is_login_url(url: str) -> bool:
    """Check if a URL matches known modem login endpoint patterns."""
    lower = url.lower()
    return any(p in lower for p in _LOGIN_URL_PATTERNS)


def _get_session_cookie_names(cookies: list[dict[str, Any]]) -> list[str]:
    """Return cookie names that suggest a pre-existing session."""
    found = []
    for cookie in cookies:
        name = cookie.get("name", "").lower()
        if any(ind in name for ind in _SESSION_COOKIE_INDICATORS):
            found.append(cookie.get("name", ""))
    return found
