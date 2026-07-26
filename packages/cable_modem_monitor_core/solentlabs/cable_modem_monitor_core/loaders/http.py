"""HTTP resource loader — fetch pages and build resource dict.

Uses an authenticated ``requests.Session`` to fetch data pages from a
modem. Each response is decoded based on the format declared in
parser.yaml (HTML → BeautifulSoup, JSON → dict).

See RESOURCE_LOADING_SPEC.md.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

import requests
from bs4 import BeautifulSoup

from ..auth.base import AuthResult
from ..models.parser_config.config import ALL_FORMAT_MODELS
from ..models.parser_config.format_registry import lookup_decode_kind
from .diagnostics import describe_request
from .fetch_list import ResourceTarget
from .html_normalize import normalize_html

_logger = logging.getLogger(__name__)


def _decode_kind(fmt: str) -> str:
    """Return the loader's decode_kind for a format tag, or empty string."""
    kind = lookup_decode_kind(fmt, ALL_FORMAT_MODELS)
    return kind or ""


class HTTPResourceLoader:
    """Fetch resources from a modem's HTTP interface.

    Given an authenticated session and a list of resource targets
    (derived from parser.yaml), fetches each page and decodes the
    response based on format. Deduplicates by path. Reuses the auth
    response if it landed on a data page.

    Args:
        session: Authenticated ``requests.Session``.
        base_url: Modem base URL (e.g., ``http://192.168.100.1``).
        timeout: Per-request timeout in seconds.
        url_token: URL token for ``url_token`` auth (empty for other
            strategies).
        token_prefix: Token prefix from ``auth.token_prefix``
            (e.g., ``ct_``).
        detect_login_pages: When True, check HTML responses for login
            page indicators (``<input type="password">``). Raises
            ``LoginPageDetectedError`` if detected. Enable for
            form-based auth strategies where the modem silently serves
            a login page at data URLs when the session expires.
    """

    def __init__(
        self,
        session: requests.Session,
        base_url: str,
        timeout: int = 10,
        url_token: str = "",
        token_prefix: str = "",
        detect_login_pages: bool = False,
        model: str = "",
        query_params: dict[str, str] | None = None,
        headers: frozenset[str] = frozenset(),
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._url_token = url_token
        self._token_prefix = token_prefix
        self._detect_login_pages = detect_login_pages
        self._model = model
        self._query_params = query_params or {}
        self._headers = headers
        self.resource_fetches: list[tuple[str, float, int, int, str]] = []
        self.decode_errors: list[tuple[str, str, str]] = []  # (path, fmt, reason)

    def fetch(
        self,
        targets: list[ResourceTarget],
        auth_result: AuthResult | None = None,
    ) -> dict[str, Any]:
        """Fetch all resource targets and return the resource dict.

        Args:
            targets: List of resources to fetch (from
                ``collect_fetch_targets``).
            auth_result: Auth result for response reuse. If the login
                response landed on a page in the target list, its body
                is reused instead of re-fetching.

        Returns:
            Resource dict keyed by URL path. Values are
            ``BeautifulSoup`` for HTML formats, ``dict`` for
            structured formats.

        Raises:
            ResourceLoadError: If a required page cannot be fetched.
        """
        resources: dict[str, Any] = {}
        self.resource_fetches = []
        self.decode_errors = []

        # Check for auth response reuse
        reuse_path = ""
        reuse_response: requests.Response | None = None
        if auth_result and auth_result.response is not None and auth_result.response_url:
            reuse_path = auth_result.response_url
            reuse_response = auth_result.response

        for target in targets:
            # Auth response reuse — skip fetch if login landed here
            if reuse_path and reuse_response is not None and target.path == reuse_path:
                _logger.debug(
                    "Reusing auth response for %s [%s]",
                    target.path,
                    self._model,
                )
                self._store_decoded(resources, target.path, reuse_response.text, target.format, target.encoding)
                continue

            # Fetch the page
            url = self._build_url(target.path)
            start = time.monotonic()
            try:
                response = self._session.get(url, timeout=self._timeout)
            except requests.RequestException as e:
                raise ResourceLoadError(
                    f"Failed to fetch {target.path}: {type(e).__name__}: {e}",
                ) from e
            except ValueError as e:
                # A malformed request component (e.g. a non-header-safe session
                # cookie value) makes http.client.putheader raise a bare
                # ValueError, which is not a RequestException and would escape
                # as an unhandled stack trace for what is an expected auth
                # failure. Convert it to a handled load error. See UC-19b.
                raise ResourceLoadError(
                    f"Malformed request fetching {target.path} (likely a non-token session credential): {e}",
                    path=target.path,
                ) from e
            elapsed_ms = (time.monotonic() - start) * 1000

            _logger.debug(
                "Fetched %s [%s]: %d (%d bytes, %.0fms)",
                target.path,
                self._model,
                response.status_code,
                len(response.content),
                elapsed_ms,
            )
            content_type = response.headers.get("Content-Type", "")
            self.resource_fetches.append(
                (
                    target.path,
                    round(elapsed_ms, 1),
                    len(response.content),
                    response.status_code,
                    content_type,
                )
            )

            if response.status_code in (401, 403):
                raise ResourceLoadError(
                    f"HTTP {response.status_code} on {target.path}",
                    status_code=response.status_code,
                    path=target.path,
                    request_line=describe_request(response.request, headers=self._headers),
                    response_body=response.text,
                    content_type=content_type,
                )

            if response.status_code >= 400:
                raise ResourceLoadError(
                    f"HTTP {response.status_code} fetching {target.path}"
                    f"\n  request: {describe_request(response.request, headers=self._headers)}",
                    status_code=response.status_code,
                    path=target.path,
                )

            # Login page detection — data pages should never contain
            # a password input field. If one is present, the modem
            # silently served a login page instead of data (session
            # expired with HTTP 200).
            if (
                self._detect_login_pages
                and response.status_code == 200
                and _decode_kind(target.format) == "html"
                and _is_login_page(response.text)
            ):
                _logger.warning(
                    "Data page %s appears to be a login page" " — session: cookies=%s basic_auth=%s",
                    target.path,
                    list(self._session.cookies.keys()),
                    self._session.auth is not None,
                )
                raise LoginPageDetectedError(target.path)

            self._store_decoded(resources, target.path, response.text, target.format, target.encoding)

        return resources

    def _store_decoded(
        self,
        resources: dict[str, Any],
        path: str,
        text: str,
        fmt: str,
        encoding: str,
    ) -> None:
        """Decode text and store result, or record decode error."""
        decoded, reason = _decode_response(text, fmt, encoding)
        if decoded is not None:
            resources[path] = decoded
        elif reason is not None:
            self.decode_errors.append((path, fmt, reason))

    def _build_url(self, path: str) -> str:
        """Build the full URL for a resource path.

        Appends URL token if configured (for ``url_token`` auth),
        then any session-level query parameters from modem.yaml.
        """
        url = f"{self._base_url}{path}"
        if self._url_token and self._token_prefix:
            url = f"{url}?{self._token_prefix}{self._url_token}"
        if self._query_params:
            sep = "&" if "?" in url else "?"
            qs = "&".join(f"{k}={v}" for k, v in self._query_params.items())
            url = f"{url}{sep}{qs}"
        return url


class ResourceLoadError(Exception):
    """A resource could not be fetched from the modem.

    Attributes:
        status_code: HTTP status code if the error was an HTTP response.
            None for connection/timeout errors.
        path: Resource path that failed (e.g., "/status.html").
        request_line: Sanitized description of the request we sent.
        response_body: Raw response body; the caller scrubs it before logging.
        content_type: Response Content-Type, for reading the body correctly.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        path: str = "",
        request_line: str = "",
        response_body: str = "",
        content_type: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.path = path
        # Fields, not message text: the collector rebuilds the message and is
        # the layer that sanitizes these (LOGGING_SPEC.md).
        self.request_line = request_line
        self.response_body = response_body
        self.content_type = content_type


class LoginPageDetectedError(ResourceLoadError):
    """Data page response contains login form indicators.

    The modem served a login page at a data URL -- the session has
    expired silently (HTTP 200, but body is a login form instead of
    data). Maps to CollectorSignal.LOAD_AUTH.
    """

    def __init__(self, path: str) -> None:
        super().__init__(
            f"Data page {path} appears to be a login page",
            status_code=200,
            path=path,
        )


def _decode_response(
    text: str,
    fmt: str,
    encoding: str,
) -> tuple[Any, str | None]:
    """Decode a response body based on format and encoding.

    Returns ``(value, None)`` on success, ``(None, reason)`` on decode
    failure, ``(None, None)`` for an empty body.  Callers check reason
    to distinguish a real decode error from a silently-empty response.
    """
    if not text:
        return None, None

    # Handle base64 encoding on response body
    if encoding == "base64":
        try:
            text = base64.b64decode(text).decode("utf-8", errors="replace")
        except Exception:
            return None, "base64 decode failed"

    kind = _decode_kind(fmt)

    if kind == "html":
        return BeautifulSoup(normalize_html(text), "html.parser"), None

    if kind == "json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None, "invalid JSON"
        if not isinstance(data, dict):
            return {"_raw": data}, None
        return data, None

    if kind == "xml":
        return None, "XML format not yet supported"

    _logger.warning("Unknown format '%s', returning as BeautifulSoup", fmt)
    return BeautifulSoup(normalize_html(text), "html.parser"), None


def _is_login_page(text: str) -> bool:
    """Check if an HTML response contains login form indicators.

    Data pages from parser.yaml (status, connection, channel info)
    do not contain password input fields. Login pages always do.
    This invariant enables login page detection without
    modem-specific configuration.

    See RESOURCE_LOADING_SPEC.md Login Page Detection section.
    """
    lower = text.lower()
    return 'type="password"' in lower or "type='password'" in lower
