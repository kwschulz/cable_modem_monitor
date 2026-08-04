"""Shared utilities for HAR entry inspection.

Generic HAR reading utilities — no domain-specific knowledge about
modem auth patterns or login endpoints. Domain-specific constants
live in the modules that use them (auth_flow.py, protocol_signals.py).
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Issue prefixes — structured markers for validation results
# ---------------------------------------------------------------------------

HARD_STOP_PREFIX = "HARD STOP:"
WARNING_PREFIX = "WARNING:"

# ---------------------------------------------------------------------------
# Static resource extensions — excluded from data page detection
# ---------------------------------------------------------------------------

STATIC_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".css",
        ".js",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".svg",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
    }
)


# ---------------------------------------------------------------------------
# HAR entry inspection functions
# ---------------------------------------------------------------------------


def lower_headers(req_or_resp: dict[str, Any]) -> dict[str, str]:
    """Build lowercase header name -> value dict from a request or response."""
    return {h["name"].lower(): h["value"] for h in req_or_resp.get("headers", [])}


def is_hnap_request(url: str, req_headers: dict[str, str]) -> bool:
    """Check if a request has HNAP protocol markers."""
    return "/HNAP1/" in url or "hnap_auth" in req_headers or "soapaction" in req_headers


def has_set_cookie(resp: dict[str, Any]) -> bool:
    """Check if a response sets a cookie (via cookies array or Set-Cookie header)."""
    if resp.get("cookies"):
        return True
    return any(h["name"].lower() == "set-cookie" for h in resp.get("headers", []))


def has_content(response: dict[str, Any]) -> bool:
    """Check if a response has non-trivial content."""
    content = response.get("content", {})
    size = content.get("size", 0)
    text = content.get("text", "")
    return size > 0 or bool(text)


def content_type_of(response: dict[str, Any]) -> str:
    """Extract a response's Content-Type, lowercased and stripped of parameters."""
    ct = lower_headers(response).get("content-type", "")
    if not ct:
        ct = response.get("content", {}).get("mimeType", "")
    return ct.lower().split(";")[0].strip()


def is_static_resource(url: str) -> bool:
    """Check if a URL is a static resource (CSS, JS, image, font)."""
    path = url.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0]
    return any(path.lower().endswith(ext) for ext in STATIC_EXTENSIONS)


def path_from_url(url: str) -> str:
    """Extract path from a full or relative URL.

    Handles ``http://host/path``, ``/path``, and ``/path?query`` forms.
    Query strings and fragments are stripped.
    """
    if "://" in url:
        after_scheme = url.split("://", 1)[1]
        slash_idx = after_scheme.find("/")
        if slash_idx >= 0:
            return after_scheme[slash_idx:].split("?", 1)[0]
        return "/"
    return url.split("?", 1)[0]


def parse_form_params(post_data: dict[str, Any]) -> dict[str, str]:
    """Parse form parameters from HAR postData.

    Handles both the ``params`` array and ``text`` with URL-encoded data.
    """
    result: dict[str, str] = {}

    # Prefer structured params
    params = post_data.get("params", [])
    if params:
        for p in params:
            result[p.get("name", "")] = p.get("value", "")
        return result

    # Fall back to parsing text
    text = post_data.get("text", "")
    if text and "=" in text:
        for pair in text.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                result[k] = v

    return result
