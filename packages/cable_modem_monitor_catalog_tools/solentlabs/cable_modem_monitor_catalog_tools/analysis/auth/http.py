"""Phase 2 - HTTP auth strategy detection.

Implements the HTTP branch of the ONBOARDING_SPEC Phase 2 decision tree.
Walks: none -> basic -> url_token -> form_sjcl -> form_pbkdf2 ->
form_nonce -> form -> hard stop.

Per docs/ONBOARDING_SPEC.md Phase 2 (HTTP transport).
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any, NamedTuple
from urllib.parse import urljoin, urlsplit

from ...validation.har_utils import (
    HARD_STOP_PREFIX,
    WARNING_PREFIX,
    has_set_cookie,
    lower_headers,
    parse_form_params,
    path_from_url,
)
from ..types import CoreGap
from .patterns import (
    get_login_url_patterns,
    get_nonce_error_prefix,
    get_nonce_success_prefix,
    get_pbkdf2_salt_triggers,
    get_sjcl_page_variables,
    get_sjcl_post_fields,
    has_credential_fields,
    is_password_field_name,
)
from .types import AuthDetail

# ---------------------------------------------------------------------------
# Auth patterns (loaded from auth_patterns.json)
# ---------------------------------------------------------------------------

_LOGIN_URL_PATTERNS: tuple[str, ...] = get_login_url_patterns()
_NONCE_SUCCESS_PREFIX: str = get_nonce_success_prefix()
_NONCE_ERROR_PREFIX: str = get_nonce_error_prefix()
_PBKDF2_SALT_TRIGGERS: tuple[str, ...] = get_pbkdf2_salt_triggers()
_SJCL_PAGE_VARS: tuple[str, ...] = get_sjcl_page_variables()
_SJCL_POST_FIELDS: tuple[str, ...] = get_sjcl_post_fields()

# A url_token credential rides in the query string as login_<base64(user:pass)>.
# The marker must be followed by an actual token and must not be matched
# anywhere in the URL: /cgi-bin/login_cgi is a script name and
# /Admin_Login_Lock.txt is a status file, and both used to register as
# url_token auth, outranking the correct strategy and emitting a bogus
# login_page.
_URL_TOKEN_QUERY = re.compile(r"(login(?:_|%5f))([A-Za-z0-9+/=%]{4,})", re.IGNORECASE)
_BASE64_CHARS = re.compile(r"^[A-Za-z0-9+/=]{4,}$")
# Bare base64 credential: base64(user:pass) as a query param name with empty value
_BARE_BASE64_CREDENTIAL = re.compile(r"^[A-Za-z0-9+/]{8,}={0,2}$")


# ---------------------------------------------------------------------------
# HTTP auth decision tree
# ---------------------------------------------------------------------------


def detect_http_auth(
    entries: list[dict[str, Any]],
    warnings: list[str],
    hard_stops: list[str],
    core_gaps: list[CoreGap] | None = None,
) -> AuthDetail:
    """Walk the HTTP auth decision tree.

    Order: none -> basic -> url_token -> form_sjcl -> form_pbkdf2 ->
    form_nonce -> form -> hard stop.

    Args:
        entries: HAR ``log.entries`` list.
        warnings: Mutable list to append warnings to.
        hard_stops: Mutable list to append hard stops to.
        core_gaps: Mutable list to append core gap items to.

    Returns:
        AuthDetail with strategy, extracted fields, and confidence.
    """
    if core_gaps is None:
        core_gaps = []

    signals = _collect_http_signals(entries)
    _flag_unmatched_logins(signals, core_gaps)

    # No auth signals at all -> none
    if not signals.has_any_auth_signal:
        return AuthDetail(strategy="none", confidence="high")

    # 401 + WWW-Authenticate: Digest -> HARD STOP (unsupported)
    if signals.digest_challenge:
        hard_stops.append(
            f"{HARD_STOP_PREFIX} WWW-Authenticate: Digest detected "
            f"(observed: {signals.digest_www_authenticate!r}). "
            "Digest auth is not yet supported. "
            "See ONBOARDING_SPEC Phase 2 for supported auth strategies."
        )
        return AuthDetail(strategy="digest", confidence="high")

    # 401 + WWW-Authenticate: Basic -> basic
    if signals.basic_challenge:
        return _extract_basic(entries, signals)

    # URL token pattern -> url_token
    if signals.url_token_entry is not None:
        return _extract_url_token(entries, signals, warnings)

    # SJCL AES-CCM encrypted login -> form_sjcl (must check before pbkdf2)
    if signals.sjcl_login_entry is not None:
        return _extract_form_sjcl(entries, signals)

    # JSON POST with PBKDF2 salt flow -> form_pbkdf2
    if signals.pbkdf2_entries:
        return _extract_form_pbkdf2(signals)

    # Form POST to login endpoint
    if signals.form_post_entry is not None:
        # Check for nonce-style response
        if signals.form_nonce_entry is not None:
            return _extract_form_nonce(signals)
        # Standard form auth
        return _extract_form(entries, signals, warnings)

    # Auth signals detected but no strategy matched -> HARD STOP + evidence
    hard_stops.append(
        f"{HARD_STOP_PREFIX} Cannot determine auth mechanism. "
        f"Observed signals: {signals.describe()}. "
        "Manual review required - check HAR for login flow details."
    )
    core_gaps.append(
        CoreGap(
            phase="auth",
            category="auth_unknown",
            summary=f"Auth signals detected but no strategy matched: {signals.describe()}",
            evidence={
                "has_401": signals.has_401,
                "has_authorization_header": signals.has_authorization_header,
                "has_form_post": signals.form_post_entry is not None,
                "has_set_cookie_after_login": signals.has_set_cookie_after_login,
                "signals_description": signals.describe(),
            },
        )
    )
    return AuthDetail(strategy="unknown", confidence="low")


# ---------------------------------------------------------------------------
# HTTP signal collection
# ---------------------------------------------------------------------------


@dataclass
class _HttpAuthSignals:
    """Collected auth signals from HTTP HAR entries."""

    has_any_auth_signal: bool = False
    digest_challenge: bool = False
    digest_www_authenticate: str = ""
    basic_challenge: bool = False
    basic_challenge_cookie: bool = False
    url_token_entry: dict[str, Any] | None = None
    url_token_login_prefix: str = ""
    url_token_login_page: str = ""
    form_post_entry: dict[str, Any] | None = None
    form_nonce_entry: dict[str, Any] | None = None
    sjcl_login_entry: dict[str, Any] | None = None
    sjcl_login_page_html: str = ""
    pbkdf2_entries: list[dict[str, Any]] = field(default_factory=list)
    has_401: bool = False
    has_302_after_post: bool = False
    has_authorization_header: bool = False
    has_set_cookie_after_login: bool = False
    unmatched_credential_posts: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """Describe what signals were found, for hard stop messages."""
        parts: list[str] = []
        if self.digest_challenge:
            parts.append("WWW-Authenticate: Digest")
        if self.has_401:
            parts.append("401 response")
        if self.has_authorization_header:
            parts.append("Authorization header")
        if self.form_post_entry is not None:
            url = self.form_post_entry["request"].get("url", "")
            parts.append(f"POST to {path_from_url(url)}")
        if self.has_set_cookie_after_login:
            parts.append("Set-Cookie after login")
        return ", ".join(parts) if parts else "ambiguous auth artifacts"


def _flag_unmatched_logins(signals: _HttpAuthSignals, core_gaps: list[CoreGap]) -> None:
    """Flag credential POSTs to unrecognized endpoints as core gaps.

    Only relevant when no login URL was matched — if the pipeline already
    found a login via URL matching, extra credential POSTs are non-auth
    forms (e.g., restart forms with password fields).
    """
    if signals.form_post_entry is not None or not signals.unmatched_credential_posts:
        return
    for endpoint in signals.unmatched_credential_posts:
        core_gaps.append(
            CoreGap(
                phase="auth",
                category="unmatched_login",
                summary=f"Form POST to {endpoint} has credential fields but URL not in login patterns",
                evidence={"endpoint": endpoint, "method": "POST"},
            )
        )


def _collect_http_signals(entries: list[dict[str, Any]]) -> _HttpAuthSignals:
    """Scan all entries and collect auth-related signals."""
    signals = _HttpAuthSignals()

    for entry in entries:
        _check_entry_auth_signals(entry, entries, signals)

    # If SJCL login detected, find the login page with JS variables.
    if signals.sjcl_login_entry is not None:
        signals.sjcl_login_page_html = _find_sjcl_login_page(entries, signals.sjcl_login_entry)

    return signals


def _check_entry_auth_signals(
    entry: dict[str, Any],
    all_entries: list[dict[str, Any]],
    signals: _HttpAuthSignals,
) -> None:
    """Check a single HAR entry for auth-related signals."""
    req = entry["request"]
    resp = entry["response"]
    url = req.get("url", "")
    method = req.get("method", "")
    status = resp.get("status", 0)
    req_hdrs = lower_headers(req)
    resp_hdrs = lower_headers(resp)

    # Authorization header on any request
    if "authorization" in req_hdrs:
        signals.has_authorization_header = True
        signals.has_any_auth_signal = True

    # 401 + WWW-Authenticate
    if status == 401:
        signals.has_401 = True
        signals.has_any_auth_signal = True
        www_auth = resp_hdrs.get("www-authenticate", "")
        scheme = _parse_auth_scheme(www_auth)
        if scheme == "digest":
            signals.digest_challenge = True
            signals.digest_www_authenticate = www_auth
        elif scheme == "basic":
            signals.basic_challenge = True
            signals.basic_challenge_cookie = _has_challenge_cookie_retry(all_entries, entry)

    # URL token pattern: login_<base64> or bare base64 credential in URL
    _check_url_token_signals(url, req, entry, signals)

    # POST requests
    if method == "POST":
        _check_post_signals(entry, req, resp, url, status, signals)

    # Set-Cookie on non-first entry after a login-like POST
    if has_set_cookie(resp) and signals.form_post_entry is not None:
        signals.has_set_cookie_after_login = True
        signals.has_any_auth_signal = True


def _check_url_token_signals(
    url: str,
    req: dict[str, Any],
    entry: dict[str, Any],
    signals: _HttpAuthSignals,
) -> None:
    """Check for URL token auth: login_<base64> prefix or bare base64 credential."""
    token_match = _extract_url_token_parts(url)
    if token_match is not None:
        signals.url_token_entry = entry
        signals.url_token_login_prefix = token_match[0]
        signals.url_token_login_page = token_match[1]
        signals.has_any_auth_signal = True
    elif signals.url_token_entry is None:
        # Bare base64 fallback: query param name is base64(user:pass)
        bare = _detect_bare_base64_credential(req)
        if bare is None:
            bare = _detect_basic_credential_in_query(req)
        if bare is not None:
            signals.url_token_entry = entry
            signals.url_token_login_prefix = ""
            signals.url_token_login_page = bare
            signals.has_any_auth_signal = True


def _check_post_signals(
    entry: dict[str, Any],
    req: dict[str, Any],
    resp: dict[str, Any],
    url: str,
    status: int,
    signals: _HttpAuthSignals,
) -> None:
    """Check POST request for auth signals (JSON/form)."""
    post_data = req.get("postData", {})
    mime = post_data.get("mimeType", "").lower()

    # JSON POST - check for SJCL or PBKDF2
    if "json" in mime:
        text = post_data.get("text", "")

        # SJCL: POST body contains EncryptData/AuthData fields
        if _is_login_url(url) and any(f in text for f in _SJCL_POST_FIELDS):
            signals.sjcl_login_entry = entry
            signals.has_any_auth_signal = True
        else:
            is_salt = any(trigger in text.lower() for trigger in _PBKDF2_SALT_TRIGGERS)
            if is_salt or _is_login_url(url):
                signals.pbkdf2_entries.append(entry)
                signals.has_any_auth_signal = True

    # Form POST to login-like endpoint. An action POST can share the login
    # endpoint (DM1000: /setup.cgi serves login and reboot), so only a
    # credential-shaped body marks the login. Among credential POSTs the
    # latest wins; a retried login yields the successful attempt.
    if "form" in mime or "x-www-form-urlencoded" in mime:
        if _is_login_url(url):
            if has_credential_fields(post_data):
                signals.form_post_entry = entry
                signals.has_any_auth_signal = True

                # Check response for nonce-style text prefixes
                resp_text = resp.get("content", {}).get("text", "")
                if resp_text and (
                    resp_text.strip().startswith(_NONCE_SUCCESS_PREFIX)
                    or resp_text.strip().startswith(_NONCE_ERROR_PREFIX)
                ):
                    signals.form_nonce_entry = entry

                # 302 redirect after POST
                if status in (301, 302):
                    signals.has_302_after_post = True
        elif has_credential_fields(post_data):
            signals.unmatched_credential_posts.append(path_from_url(url))


# ---------------------------------------------------------------------------
# Strategy extraction helpers
# ---------------------------------------------------------------------------


def _extract_basic(entries: list[dict[str, Any]], signals: _HttpAuthSignals) -> AuthDetail:
    """Extract basic auth fields."""
    return AuthDetail(
        strategy="basic",
        fields={"challenge_cookie": signals.basic_challenge_cookie},
        confidence="high",
    )


def _collect_server_cookie_names(entries: list[dict[str, Any]]) -> set[str]:
    """Return all cookie names issued via Set-Cookie across all HAR responses."""
    names: set[str] = set()
    for entry in entries:
        resp = entry["response"]
        for header in resp.get("headers", []):
            if header["name"].lower() == "set-cookie":
                name = header["value"].split("=", 1)[0].strip()
                if name:
                    names.add(name)
        for cookie in resp.get("cookies", []):
            name = cookie.get("name", "")
            if name:
                names.add(name)
    return names


def _detect_client_side_cookie(
    entries: list[dict[str, Any]],
    auth_entry: dict[str, Any],
) -> str:
    """Return name of first cookie in post-login requests never issued via Set-Cookie.

    Detects the client-side credential cookie injection pattern: firmware
    authenticates and returns a session token in the response body; browser JS
    sets it as a credential cookie (createCookie("credential", result)).
    Returns the cookie name, or empty string if not detected.
    """
    server_cookies = _collect_server_cookie_names(entries)
    past_auth = False
    for entry in entries:
        if entry is auth_entry:
            past_auth = True
            continue
        if not past_auth:
            continue
        for cookie in entry["request"].get("cookies", []):
            name: str = cookie.get("name", "")
            if name and name not in server_cookies:
                return name
    return ""


def _extract_url_token(
    entries: list[dict[str, Any]],
    signals: _HttpAuthSignals,
    warnings: list[str],
) -> AuthDetail:
    """Extract url_token auth fields."""
    entry = signals.url_token_entry
    assert entry is not None  # guaranteed by caller
    req = entry["request"]
    req_hdrs = lower_headers(req)
    resp = entry["response"]

    ajax_login = "x-requested-with" in req_hdrs

    resp_text = resp.get("content", {}).get("text", "")

    # Empty auth response body: HAR sanitizers strip the response body, so an
    # empty body here may mean the body was redacted, not that it was empty.
    # The pattern: firmware returns a server-issued token in the auth body;
    # browser JS sets it as a credential cookie (createCookie("credential", result)).
    inject_credential_cookie = False
    detected_cookie_name = ""
    if not resp_text:
        warnings.append(
            f"{WARNING_PREFIX} url_token auth response body is empty — "
            "body may have been redacted by har-capture sanitizer. "
            "Check whether post-login requests carry a credential cookie "
            "that was not set via Set-Cookie (client-side cookie injection pattern)."
        )
        detected_cookie_name = _detect_client_side_cookie(entries, entry)
        if detected_cookie_name:
            inject_credential_cookie = True
            warnings.append(
                f"{WARNING_PREFIX} cookie '{detected_cookie_name}' appears in "
                "post-login requests but was never issued via Set-Cookie — "
                "client-side credential cookie injection detected. "
                "Setting inject_credential_cookie: true (confidence: low — "
                "verify the auth response body contains the credential value; "
                "it is typically a server-issued token, not btoa(user:pass))."
            )

    fields: dict[str, Any] = {
        "login_page": signals.url_token_login_page,
        "login_prefix": signals.url_token_login_prefix,
        "ajax_login": ajax_login,
        "success_indicator": "",
    }
    if inject_credential_cookie:
        fields["inject_credential_cookie"] = True
        fields["cookie_name"] = detected_cookie_name

    return AuthDetail(
        strategy="url_token",
        fields=fields,
        confidence="high",
    )


def _find_sjcl_login_page(
    entries: list[dict[str, Any]],
    sjcl_post_entry: dict[str, Any],
) -> str:
    """Find the login page HTML containing SJCL JS variables.

    Scans GET responses before the SJCL POST for pages containing
    ``myIv`` or ``mySalt`` variable assignments.
    """
    for entry in entries:
        if entry is sjcl_post_entry:
            break
        req = entry["request"]
        if req.get("method", "") != "GET":
            continue
        content = entry["response"].get("content", {})
        text: str = content.get("text", "")
        if not text:
            continue
        # Check for SJCL JS variables on the page
        if any(var in text for var in _SJCL_PAGE_VARS):
            return text
    return ""


def _find_sjcl_login_page_path(
    entries: list[dict[str, Any]],
    login_post_entry: dict[str, Any],
) -> str:
    """Find the login page path from GET responses before the login POST.

    Prefers HTML pages over JS files — the auth manager fetches the
    HTML page, and the SJCL variables may be inline or in a linked script.
    """
    login_page = "/"
    login_page_is_html = False
    for e in entries:
        if e is login_post_entry:
            break
        if e["request"].get("method") != "GET":
            continue
        content = e["response"].get("content", {})
        text = content.get("text", "")
        if not text or not any(var in text for var in _SJCL_PAGE_VARS):
            continue
        mime = content.get("mimeType", "")
        is_html = "html" in mime
        if is_html or not login_page_is_html:
            login_page = path_from_url(e["request"].get("url", "/"))
            login_page_is_html = is_html
            if is_html:
                break
    return login_page


def _find_sjcl_session_validation(
    entries: list[dict[str, Any]],
    login_post_entry: dict[str, Any],
    csrf_header: str,
) -> str:
    """Find the session validation endpoint after the login POST.

    Looks for the first POST after login that carries the CSRF header
    with a non-``"undefined"`` value.
    """
    past_login = False
    for e in entries:
        if e is login_post_entry:
            past_login = True
            continue
        if not past_login or e["request"].get("method") != "POST":
            continue
        if not csrf_header:
            continue
        e_hdrs = {h["name"].lower(): h["value"] for h in e["request"].get("headers", [])}
        val = e_hdrs.get(csrf_header.lower(), "")
        if val and val != "undefined":
            return path_from_url(e["request"].get("url", ""))
    return ""


def _extract_sjcl_encrypt_aad(post_text: str) -> str:
    """Extract the encrypt AAD from the login POST body's AuthData field."""
    import json

    try:
        body = json.loads(post_text)
        if isinstance(body, dict) and "AuthData" in body:
            return str(body["AuthData"])
    except (ValueError, TypeError):
        pass
    return "loginPassword"  # SJCL default


def _extract_form_sjcl(
    entries: list[dict[str, Any]],
    signals: _HttpAuthSignals,
) -> AuthDetail:
    """Extract form_sjcl auth fields from SJCL AES-CCM login flow."""
    entry = signals.sjcl_login_entry
    assert entry is not None  # guaranteed by caller
    req = entry["request"]
    url = req.get("url", "")
    post_text = req.get("postData", {}).get("text", "")

    login_page = _find_sjcl_login_page_path(entries, entry)
    encrypt_aad = _extract_sjcl_encrypt_aad(post_text)

    # Extract CSRF header from the POST's request headers
    csrf_header = ""
    for h in req.get("headers", []):
        name_lower = h["name"].lower()
        if "csrf" in name_lower or "nonce" in name_lower:
            csrf_header = h["name"]
            break

    session_validation = _find_sjcl_session_validation(entries, entry, csrf_header)
    confidence = "high" if signals.sjcl_login_page_html else "medium"

    return AuthDetail(
        strategy="form_sjcl",
        fields={
            "login_page": login_page,
            "login_endpoint": path_from_url(url),
            "session_validation_endpoint": session_validation,
            "csrf_header": csrf_header,
            "encrypt_aad": encrypt_aad,
            "decrypt_aad": "nonce",
        },
        confidence=confidence,
    )


def _extract_form_pbkdf2(signals: _HttpAuthSignals) -> AuthDetail:
    """Extract form_pbkdf2 auth fields from salt/challenge flow."""
    if not signals.pbkdf2_entries:
        return AuthDetail(strategy="form_pbkdf2", confidence="medium")

    # The first PBKDF2 entry is typically the salt request
    first_entry = signals.pbkdf2_entries[0]
    req = first_entry["request"]
    url = req.get("url", "")

    # Extract CSRF header if present
    csrf_header = ""
    req_hdrs_lower = lower_headers(req)
    for header_name in ("x-csrf-token", "csrf-token", "x-xsrf-token"):
        if header_name in req_hdrs_lower:
            csrf_header = header_name.upper().replace("-", "_")
            # Preserve original casing - check raw headers
            for h in req.get("headers", []):
                if h["name"].lower() == header_name:
                    csrf_header = h["name"]
                    break
            break

    # Detect CSRF init endpoint (request before the login POST)
    csrf_init_endpoint = ""

    fields: dict[str, Any] = {
        "login_endpoint": path_from_url(url),
        "csrf_header": csrf_header,
        "csrf_init_endpoint": csrf_init_endpoint,
    }

    # Try to extract PBKDF2 params from response JSON
    resp = first_entry["response"]
    resp_text = resp.get("content", {}).get("text", "")
    if resp_text:
        pbkdf2_params = _extract_pbkdf2_params_from_response(resp_text)
        fields.update(pbkdf2_params)

    return AuthDetail(
        strategy="form_pbkdf2",
        fields=fields,
        confidence="medium",
    )


def _extract_form_nonce(signals: _HttpAuthSignals) -> AuthDetail:
    """Extract form_nonce auth fields."""
    entry = signals.form_nonce_entry
    assert entry is not None  # guaranteed by caller
    req = entry["request"]
    url = req.get("url", "")
    post_data = req.get("postData", {})

    # Extract form params
    params = parse_form_params(post_data)

    # Identify field names from POST params
    nonce_field = ""
    username_field = "username"
    password_field = "password"
    for name in params:
        lower = name.lower()
        if lower in ("username", "user"):
            username_field = name
        elif lower in ("password", "pass"):
            password_field = name
        else:
            nonce_field = name

    # Detect success/error prefixes from response
    success_prefix = _NONCE_SUCCESS_PREFIX
    error_prefix = _NONCE_ERROR_PREFIX

    fields: dict[str, Any] = {
        "action": path_from_url(url),
        "nonce_field": nonce_field,
        "success_prefix": success_prefix,
        "error_prefix": error_prefix,
    }
    if username_field != "username":
        fields["username_field"] = username_field
    if password_field != "password":
        fields["password_field"] = password_field

    return AuthDetail(
        strategy="form_nonce",
        fields=fields,
        confidence="high",
    )


def _extract_form(
    entries: list[dict[str, Any]],
    signals: _HttpAuthSignals,
    warnings: list[str],
) -> AuthDetail:
    """Extract standard form auth fields.

    When a login page GET precedes the form POST in the HAR, emits
    ``login_page`` so the runtime pre-fetches it for cookies and
    hidden-field discovery. Hidden fields that the runtime would
    discover are filtered out — only overrides remain. A
    ``form_selector`` is emitted when the login page has multiple
    ``<form>`` elements.
    """
    from .form_discovery import detect_form_selector, extract_hidden_fields

    entry = signals.form_post_entry
    assert entry is not None  # guaranteed by caller
    req = entry["request"]
    resp = entry["response"]
    url = req.get("url", "")
    post_path = path_from_url(url)
    status = resp.get("status", 0)
    post_data = req.get("postData", {})

    # Parse form fields
    params = parse_form_params(post_data)
    username_field, password_field, hidden_fields = classify_form_fields(params)

    # classify_form_fields drops surplus credential-shaped fields from
    # hidden_fields; surface them for the manual step.
    dropped = [n for n in params if n not in hidden_fields and n not in (username_field, password_field)]
    if dropped:
        warnings.append(
            f"{WARNING_PREFIX} form auth: credential-shaped fields {dropped} omitted "
            "from hidden_fields. If the firmware requires them in the login POST, "
            "add them by hand from the login page source."
        )

    # Detect login page from HAR (GET before POST with matching action)
    login_page_info = _find_login_page(entries, entry)
    login_page = login_page_info.path
    login_page_html = login_page_info.html

    # Detect encoding: check POST value, then fall back to login page JS
    encoding = detect_encoding(params, password_field, login_page_html)

    # When login_page found, the runtime discovers hidden fields from the
    # HTML form automatically. Filter hidden_fields to only keep overrides
    # (fields not discoverable, or with values different from the HTML).
    form_selector = ""
    if login_page_html:
        form_selector = detect_form_selector(login_page_html, post_path)
        discoverable = extract_hidden_fields(login_page_html, form_selector)
        hidden_fields = {
            name: value
            for name, value in hidden_fields.items()
            if name not in discoverable or discoverable[name] != value
        }

    # Detect success indicator
    success: dict[str, str] = {}
    if status in (301, 302):
        # Redirect on success
        location = ""
        for h in resp.get("headers", []):
            if h["name"].lower() == "location":
                location = h["value"]
                break
        if location:
            success["redirect"] = path_from_url(location)

    return AuthDetail(
        strategy="form",
        fields={
            "action": post_path,
            "method": "POST",
            "username_field": username_field,
            "password_field": password_field,
            "encoding": encoding,
            "hidden_fields": hidden_fields,
            "login_page": login_page,
            "form_selector": form_selector,
            "success": success,
        },
        confidence="high",
    )


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def _parse_auth_scheme(www_authenticate: str) -> str:
    """Extract the auth scheme token from a WWW-Authenticate header.

    Per RFC 7235, the scheme is the first whitespace-delimited token.
    Returns the lowercase scheme string, or empty string if absent.
    """
    token = www_authenticate.strip().split(None, 1)[0] if www_authenticate.strip() else ""
    return token.lower()


def _is_login_url(url: str) -> bool:
    """Check if a URL matches known login endpoint patterns."""
    lower = url.lower()
    return any(p in lower for p in _LOGIN_URL_PATTERNS)


def classify_form_fields(
    params: dict[str, str],
) -> tuple[str, str, dict[str, str]]:
    """Classify form fields into username, password, and hidden fields.

    Returns:
        Tuple of (username_field, password_field, hidden_fields).
    """
    username_field = ""
    password_field = ""
    hidden_fields: dict[str, str] = {}

    username_indicators = ("username", "user", "login")

    for name, value in params.items():
        lower_name = name.lower()
        # Password checked first: "loginPassword" matches both lists, and
        # password is the more specific signal. First match wins on each
        # axis, since credential inputs precede auxiliary fields like
        # cur_passwd. Surplus credential-shaped fields never become
        # hidden_fields; their captured values are credentials, not form
        # constants.
        if is_password_field_name(name):
            password_field = password_field or name
        elif any(ind in lower_name for ind in username_indicators):
            username_field = username_field or name
        else:
            hidden_fields[name] = value

    # Default field names if not identified
    if not username_field:
        username_field = "username"
    if not password_field:
        password_field = "password"

    return username_field, password_field, hidden_fields


def detect_encoding(
    params: dict[str, str],
    password_field: str,
    login_page_html: str = "",
) -> str:
    """Detect password encoding from POST values or login page JavaScript.

    Two heuristics (first match wins):
    1. POST body: password value looks like valid base64.
    2. Login page HTML: JavaScript encodes the password before submission
       (e.g., ``isEncryptPswd = 1`` with a base64 ``encode()`` function,
       or ``btoa()`` applied to the password field).
    """
    # Heuristic 1: POST body value
    pwd_value = params.get(password_field, "")
    if pwd_value and _BASE64_CHARS.match(pwd_value) and len(pwd_value) >= 4:
        try:
            decoded = base64.b64decode(pwd_value, validate=True)
            decoded.decode("utf-8")
            return "base64"
        except Exception:
            pass

    # Heuristic 2: Login page JavaScript
    if login_page_html and _has_js_base64_encoding(login_page_html):
        return "base64"

    return "plain"


# Base64 keyStr — the exact 65-character alphabet string used by
# modem firmware JavaScript encoders.  No false positives: this
# literal only appears in base64 implementations.
_BASE64_KEYSTR = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="

_JS_BTOA_PATTERN = re.compile(r"btoa\s*\(", re.IGNORECASE)

# Stock base64.js firmware calls base64encode() at submit time; the keyStr
# lives in that separate file, so the login page HTML shows only the call.
_JS_BASE64_FN_PATTERN = re.compile(r"base64encode\s*\(", re.IGNORECASE)


def _has_js_base64_encoding(html: str) -> bool:
    """Check if login page JavaScript encodes the password in base64."""
    if _BASE64_KEYSTR in html:
        return True
    if _JS_BASE64_FN_PATTERN.search(html):
        return True
    return bool(_JS_BTOA_PATTERN.search(html))


class _LoginPageInfo(NamedTuple):
    """Login page path and HTML body discovered from the HAR."""

    path: str
    html: str


# Form actions may be quoted or bare, absolute or relative to the page.
_FORM_ACTION_PATTERN = re.compile(
    r"<form[^>]*\saction=(?:[\"']([^\"']+)[\"']|([^\s>\"']+))",
    re.IGNORECASE,
)


def _page_posts_to(text: str, page_url: str, post_path: str) -> bool:
    """Check if a page references the login path or has a form action resolving to it."""
    if post_path in text:
        return True
    # A relative form action never contains the absolute post path;
    # resolve it against the page URL.
    for match in _FORM_ACTION_PATTERN.finditer(text):
        action = match.group(1) or match.group(2) or ""
        if action and path_from_url(urljoin(page_url, action)) == post_path:
            return True
    return False


def _find_login_page(
    entries: list[dict[str, Any]],
    form_post_entry: dict[str, Any],
) -> _LoginPageInfo:
    """Find the login page that served the login form.

    Scans entries before the form POST for a GET response containing
    an HTML form whose action matches the POST URL.

    Returns:
        ``_LoginPageInfo`` with path and HTML. Both are empty strings
        when no matching login page is found.
    """
    post_url = form_post_entry["request"].get("url", "")
    post_path = path_from_url(post_url)

    for entry in entries:
        if entry is form_post_entry:
            break
        req = entry["request"]
        if req.get("method", "") != "GET":
            continue
        content = entry["response"].get("content", {})
        text: str = content.get("text", "")
        if not text:
            continue
        mime: str = content.get("mimeType", "")
        if "html" not in mime:
            continue
        # Check if this page contains a form posting to the login URL
        if _page_posts_to(text, req.get("url", ""), post_path):
            page_path = path_from_url(req.get("url", ""))
            return _LoginPageInfo(path=page_path, html=text)

    return _LoginPageInfo(path="", html="")


def _has_challenge_cookie_retry(entries: list[dict[str, Any]], challenge_entry: dict[str, Any]) -> bool:
    """Check if a 401 challenge is followed by a retry with Set-Cookie.

    Some modems return a challenge cookie on the initial 401 that must
    be included in the retry.
    """
    found_challenge = False
    for entry in entries:
        if entry is challenge_entry:
            found_challenge = True
            # Check if the 401 response sets a cookie
            if has_set_cookie(entry["response"]):
                continue
            return False
        if found_challenge:
            # Next request after challenge - check if it retries same URL
            challenge_url = challenge_entry["request"].get("url", "")
            retry_url = entry["request"].get("url", "")
            if path_from_url(challenge_url) == path_from_url(retry_url):
                return True
            break
    return False


def _extract_url_token_parts(url: str) -> tuple[str, str] | None:
    """Extract login prefix and page path from a URL token login URL.

    Returns:
        Tuple of (login_prefix, login_page) or None if not a token URL.
    """
    # Query only: the token is a credential passed as a query parameter, so a
    # path segment that merely contains the word "login" is not one.
    query = urlsplit(url).query
    if not query:
        return None
    match = _URL_TOKEN_QUERY.search(query)
    if match is None:
        return None
    # Normalize: login%5f -> login_
    prefix = match.group(1).replace("%5f", "_").replace("%5F", "_")
    # The login page is the path without query string
    return prefix, path_from_url(url)


def _detect_bare_base64_credential(req: dict[str, Any]) -> str | None:
    """Detect bare base64 credential token in a query parameter.

    Some modems (e.g. SB8200 HW v7) pass base64(user:pass) as a query
    parameter name with an empty value, without any ``login_`` prefix.

    Returns:
        The login page path if a bare base64 credential is found, else None.
    """
    for param in req.get("queryString", []):
        name = param.get("name", "")
        value = param.get("value", "")
        # Bare credential: name is base64, value is empty
        if value or not _BARE_BASE64_CREDENTIAL.match(name):
            continue
        try:
            decoded = base64.b64decode(name).decode("utf-8", errors="replace")
        except Exception:
            continue
        # Credential format: user:pass (must have exactly one colon)
        if ":" in decoded and decoded.count(":") == 1:
            return path_from_url(req.get("url", ""))
    return None


def _detect_basic_credential_in_query(req: dict[str, Any]) -> str | None:
    """Detect a url_token login whose credential also rides in an Authorization header.

    Returns:
        The login page path if header and query carry the same credential, else None.
    """
    # The credential is sent twice, as a bare query parameter and as the Basic
    # header value, so _detect_bare_base64_credential's decode test is the
    # natural check. It cannot fire on a sanitized capture: har-capture
    # replaces the credential with an opaque placeholder, and it assigns the
    # URL and the header *different* placeholders, so the two copies cannot be
    # compared either. What survives sanitizing is the shape. Pairing a Basic
    # header with a valueless query parameter occurs once in the whole
    # catalog, on this login, so the shape alone identifies it.
    has_basic = any(
        header["name"].lower() == "authorization" and header.get("value", "").strip().lower().startswith("basic")
        for header in req.get("headers", [])
    )
    if not has_basic:
        return None

    for param in req.get("queryString", []):
        # Bare credential: the whole parameter is the token, with no value.
        if param.get("name") and not param.get("value"):
            return path_from_url(req.get("url", ""))
    return None


def _extract_pbkdf2_params_from_response(resp_text: str) -> dict[str, Any]:
    """Try to extract PBKDF2 parameters from a JSON response body.

    Looks for salt, iterations, and key length fields in the response.
    Returns extracted params as a dict (may be partial or empty).
    """
    import json

    params: dict[str, Any] = {}
    try:
        data = json.loads(resp_text)
    except (json.JSONDecodeError, TypeError):
        return params

    if not isinstance(data, dict):
        return params

    # Common field names for PBKDF2 params
    for key, config_key in [
        ("iterations", "pbkdf2_iterations"),
        ("iter", "pbkdf2_iterations"),
        ("keyLength", "pbkdf2_key_length"),
        ("key_length", "pbkdf2_key_length"),
        ("salt", "salt_value"),
    ]:
        if key in data:
            params[config_key] = data[key]

    return params
