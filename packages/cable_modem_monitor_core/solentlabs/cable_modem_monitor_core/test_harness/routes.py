"""Route builder — HAR entries to route table.

Pure data transformation. No HTTP, no auth, no network.
"""

from __future__ import annotations

import base64
import contextlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


@dataclass
class RouteEntry:
    """A single route in the mock server.

    Attributes:
        status: HTTP status code to return.
        headers: Response headers as list of (name, value) tuples.
        body: Response body text.
    """

    status: int
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: str = ""


def build_routes(
    har_entries: list[dict[str, Any]],
) -> dict[tuple[str, str], RouteEntry]:
    """Build route table from HAR entries.

    Each entry becomes a route keyed by ``(method, normalized_path)``.
    For duplicate keys, the last successful (status 200) response wins.
    Non-200 responses are stored only if no 200 exists for that route.

    Args:
        har_entries: List of HAR ``log.entries`` dicts.

    Returns:
        Route table mapping ``(method, path)`` to response.
    """
    routes: dict[tuple[str, str], RouteEntry] = {}

    for entry in har_entries:
        request = entry.get("request", {})
        response = entry.get("response", {})

        method = request.get("method", "GET").upper()
        url = request.get("url", "")
        parsed = urlparse(url)
        path = normalize_path(parsed.path)
        if not path:
            continue

        # Include query string in route key when present, so
        # endpoints like /setup.cgi?todo=X resolve independently.
        route_path = path
        if parsed.query:
            route_path = f"{path}?{parsed.query}"

        status = response.get("status", 0)
        # A non-positive status is har-capture's marker for a request
        # that never received one — the modem tore the connection down,
        # or the page navigated away mid-flight. There is no captured
        # response to replay, so the entry must not become a route;
        # serving it would put a status line on the wire that the modem
        # never sent. One entry in the fleet is like this: the SB8200's
        # logout GET, which used to replay as a literal -1.
        if status <= 0:
            continue

        headers = _extract_headers(response)
        body = _extract_body(response)

        key = (method, route_path)
        existing = routes.get(key)

        # Prefer 200 responses; for non-200, only store if no entry yet
        if existing is None or status == 200:
            routes[key] = RouteEntry(status=status, headers=headers, body=body)

    return routes


def normalize_path(path: str) -> str:
    """Normalize a URL path for consistent route matching.

    Ensures a leading slash. Trailing slashes are preserved.
    """
    if not path:
        return ""
    if not path.startswith("/"):
        path = "/" + path
    return path


def extract_har_response_text(
    entries: list[dict[str, Any]],
    method: str,
    path: str,
) -> str:
    """Find the first HAR entry matching *method* and *path*, return its body text.

    Args:
        entries: HAR ``log.entries`` list.
        method: HTTP method to match (e.g. ``"GET"``).
        path: URL path to match (normalized before comparison).

    Returns:
        Response body text, or empty string if no match.
    """
    norm = normalize_path(path)
    method_upper = method.upper()
    for entry in entries:
        req = entry.get("request", {})
        if req.get("method", "").upper() != method_upper:
            continue
        entry_path = normalize_path(urlparse(req.get("url", "")).path)
        if entry_path == norm:
            content = entry.get("response", {}).get("content", {})
            text: str = str(content.get("text", ""))
            if content.get("encoding") == "base64" and text:
                with contextlib.suppress(Exception):
                    text = base64.b64decode(text).decode("utf-8", errors="replace")
            return text
    return ""


def _extract_headers(response: dict[str, Any]) -> list[tuple[str, str]]:
    """Extract response headers from a HAR response dict.

    Strips Content-Length headers because HAR redaction may change
    body length without updating the header value.
    """
    headers: list[tuple[str, str]] = []
    for h in response.get("headers", []):
        name = h.get("name", "")
        value = h.get("value", "")
        if name and name.lower() != "content-length":
            headers.append((name, value))
    return headers


def _extract_body(response: dict[str, Any]) -> str:
    """Extract response body text from a HAR response dict.

    Decodes ``content.encoding: "base64"`` per the HAR 1.2 spec —
    the encoding field describes how the HAR recorder stored the
    body in the JSON file, not how the modem transmitted it.
    """
    content = response.get("content", {})
    text = str(content.get("text", ""))
    if content.get("encoding") == "base64" and text:
        with contextlib.suppress(Exception):
            text = base64.b64decode(text).decode("utf-8", errors="replace")
    return text
