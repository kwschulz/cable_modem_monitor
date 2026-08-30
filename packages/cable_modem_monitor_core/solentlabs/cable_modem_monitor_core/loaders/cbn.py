"""CBN (Compal Broadband Networks) XML POST resource loader.

Fetches data from Compal modem firmware via POST to a ``getter_endpoint``
with ``fun=N`` parameters. Each response is XML parsed with defusedxml.

Key constraints:
- Sequential execution — the server rotates ``sessionToken`` on every
  response, so requests must be serialized.
- Token must be the first POST body parameter.
- Logout is handled by the collector via ``actions.logout`` config,
  not by the loader (avoids double-logout).

A fetch failure is surfaced, never skipped: connection and timeout
errors propagate for the collector to read as ``CONNECTIVITY``, and a
non-2xx raises ``ResourceLoadError`` carrying its status. Only a body
that will not decode is skipped, which is the case that behaviour was
built for.

See RESOURCE_LOADING_SPEC.md CBN XML POST Loading section.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from xml.etree.ElementTree import Element, ParseError

import defusedxml.ElementTree as DefusedET
import requests

from ..fetch_list import ResourceTarget
from .diagnostics import describe_request
from .http import ResourceLoadError

_logger = logging.getLogger(__name__)


class CBNLoader:
    """Fetch resources via CBN XML POST API.

    Args:
        session: Authenticated ``requests.Session`` with session cookies.
        base_url: Modem base URL (e.g., ``http://192.168.0.1``).
        getter_endpoint: URL path for data POST (e.g., ``/xml/getter.xml``).
        session_cookie_name: Cookie carrying the rotating session token.
        timeout: Per-request timeout in seconds.
        model: Modem model name for log messages.
    """

    def __init__(
        self,
        session: requests.Session,
        base_url: str,
        getter_endpoint: str,
        session_cookie_name: str,
        timeout: int,
        model: str,
        headers: frozenset[str] = frozenset(),
    ) -> None:
        self._session = session
        self._base_url = base_url
        self._getter_url = f"{base_url}{getter_endpoint}"
        self._cookie_name = session_cookie_name
        self._timeout = timeout
        self._model = model
        self._headers = headers
        self.resource_fetches: list[tuple[str, float, int, int, str]] = []

    def fetch(
        self,
        targets: list[ResourceTarget],
        auth_result: Any = None,
    ) -> dict[str, Any]:
        """Fetch all targets and return the resource dict.

        Each target is fetched sequentially. Logout is NOT done here —
        the collector handles it via ``_execute_logout_if_needed()``
        using the ``actions.logout`` config.

        Args:
            targets: Resource targets from ``collect_fetch_targets()``.
            auth_result: Unused (present for interface compatibility).

        Returns:
            Dict keyed by ``fun`` parameter string, values are
            ``defusedxml.ElementTree.Element`` objects.
        """
        resources: dict[str, Any] = {}
        self.resource_fetches = []

        for target in targets:
            element = self._fetch_one(target.path)
            if element is not None:
                resources[target.path] = element

        return resources

    def _fetch_one(self, fun: str) -> Element | None:
        """Fetch one resource, returning None only when its body will not decode."""
        token = self._session.cookies.get(self._cookie_name) or ""
        post_body = f"token={token}&fun={fun}"

        start = time.monotonic()
        try:
            response = self._session.post(
                self._getter_url,
                data=post_body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            # An unreachable or unresponsive modem is CONNECTIVITY, not a
            # short resource dict. Swallowing it here dropped the key, the
            # coordinator counted 0/N anchors, and the poll ended as
            # LOAD_INTEGRITY — auth streak, AUTH_FAILED, reauth prompt for a
            # modem that judged nothing (#200). RESOURCE_LOADING_SPEC
            # § Error Signals: the loader surfaces, policy decides.
            if isinstance(exc, requests.ConnectionError | requests.Timeout):
                raise
            # Everything else becomes LOAD_ERROR, the same conversion
            # loaders/http.py makes. A CBN-specific error type would need
            # its own collector branch to be caught at all, and the status
            # is the only thing that branch would read.
            raise ResourceLoadError(
                f"Failed to fetch fun={fun}: {type(exc).__name__}: {exc}",
                path=fun,
            ) from exc

        elapsed_ms = (time.monotonic() - start) * 1000

        if not response.ok:
            # Carries the status out so the collector can tell a stale
            # session (401/403 -> LOAD_AUTH) from the modem declining to
            # serve (-> LOAD_ERROR, no auth streak). Skipping the target
            # routed both to LOAD_INTEGRITY, which counts toward the streak
            # a 5xx must not touch. UC-32, and the all-or-nothing rule.
            raise ResourceLoadError(
                f"HTTP {response.status_code} on fun={fun}",
                status_code=response.status_code,
                path=fun,
                request_line=describe_request(response.request, headers=self._headers),
                response_body=response.text,
                content_type=response.headers.get("Content-Type", ""),
            )

        _logger.debug(
            "CBN resource loaded: fun=%s [%s] (%.0fms, %d bytes)",
            fun,
            self._model,
            elapsed_ms,
            len(response.content),
        )
        content_type = response.headers.get("Content-Type", "")
        self.resource_fetches.append(
            (
                fun,
                round(elapsed_ms, 1),
                len(response.content),
                response.status_code,
                content_type,
            )
        )

        try:
            element: Element = DefusedET.fromstring(response.text)
            return element
        except ParseError:
            _logger.warning(
                "CBN malformed XML for fun=%s [%s]",
                fun,
                self._model,
            )
            return None
