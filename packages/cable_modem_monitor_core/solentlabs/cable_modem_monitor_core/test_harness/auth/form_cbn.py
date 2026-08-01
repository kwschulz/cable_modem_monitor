"""CBN AES-256-CBC authentication handler.

Speaks the Compal Broadband Networks encrypted login protocol: serves
a login page with a deterministic ``sessionToken`` cookie, then accepts
an AES-256-CBC encrypted password POST and returns a success response
with a rotating session token and SID.

CBN-specific: login, logout, and restart all POST to the same setter
endpoint, discriminated by the ``fun=N`` parameter in the body. This
handler routes setter POSTs via body inspection in ``handle_login``.

Data requests (POST to getter endpoint) are served via
``get_route_override``, which dispatches by ``fun=N`` from the body.
This bypasses the route table (which can't differentiate POST requests
to the same URL). Each response includes a rotated session token.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ..routes import RouteEntry, normalize_path
from .form import FormAuthHandler

if TYPE_CHECKING:
    from ...models.modem_config import ModemConfig

_logger = logging.getLogger(__name__)

_FUN_RE = re.compile(rb"fun=(\d+)")
_FUN_TEXT_RE = re.compile(r"fun=(\d+)")


class FormCbnAuthHandler(FormAuthHandler):
    """CBN AES-256-CBC auth handler — speaks the encrypted login protocol.

    The real ``FormCbnAuthManager`` does a multi-phase login:

    1. GET login page → receive ``sessionToken`` cookie.
    2. Encrypt password via ``compal_encrypt(pw, sessionToken)``.
    3. POST to setter endpoint with ``fun=<login_fun>``.
    4. Check response for ``"successful"`` and extract ``SID``.

    This handler serves a login page with a known session token,
    accepts any encrypted password POST, and returns a success
    response with SID. Credentials are not validated — real
    credential validation lives in the auth managers.

    CBN-specific: All setter endpoint POSTs (login, logout, restart)
    share the same URL, discriminated by ``fun=N`` in the POST body.
    This handler routes them via body inspection in ``handle_login``.

    Data requests to the getter endpoint are served via
    ``get_route_override``, which builds a ``fun→response`` lookup
    from HAR entries.

    Args:
        login_page_path: Path the auth manager GETs for the login page.
        setter_endpoint: Path for all setter POSTs (login, logout, restart).
        getter_endpoint: Path for data GET POSTs (fun=N → XML response).
        session_cookie_name: Name of the rotating session token cookie.
        login_fun: ``fun`` parameter value for login POST.
        logout_fun: ``fun`` parameter value for logout POST (None if no logout).
        restart_fun: ``fun`` parameter value for restart POST (None if no restart).
        har_entries: HAR ``log.entries`` list for building getter responses.
    """

    _TEST_PASSWORD = "pw"
    _INITIAL_SESSION_TOKEN = "mock-cbn-session-token-0000"
    _TEST_SID = "12345"

    def __init__(
        self,
        login_page_path: str,
        setter_endpoint: str,
        getter_endpoint: str,
        session_cookie_name: str,
        login_fun: int,
        logout_fun: int | None = None,
        restart_fun: int | None = None,
        har_entries: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            login_path=setter_endpoint,
            cookie_name="",  # Session tracked by server flag, not cookie
        )
        self._login_page_path = normalize_path(login_page_path)
        self._setter_endpoint = normalize_path(setter_endpoint)
        self._getter_endpoint = normalize_path(getter_endpoint)
        self._session_cookie_name = session_cookie_name
        self._login_fun = login_fun
        self._logout_fun = logout_fun
        self._restart_fun = restart_fun
        self._token_counter = 0

        # Build fun→response lookup from HAR entries for getter dispatch.
        self._getter_responses = _build_getter_responses(
            har_entries or [],
            self._getter_endpoint,
        )

    @property
    def current_token(self) -> str:
        """The most recently issued session token (deterministic)."""
        if self._token_counter == 0:
            return self._INITIAL_SESSION_TOKEN
        return f"mock-cbn-token-{self._token_counter:04d}"

    def _next_token(self) -> str:
        """Generate the next rotating session token."""
        self._token_counter += 1
        return self.current_token

    def is_login_request(self, method: str, path: str) -> bool:
        """CBN login: GET login page or any POST to setter endpoint.

        All setter POSTs are routed here so ``handle_login`` can
        inspect the body and dispatch by ``fun`` parameter.
        """
        norm = normalize_path(path)
        if method == "GET" and norm == self._login_page_path:
            return True
        return method == "POST" and norm == self._setter_endpoint

    def handle_login(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> RouteEntry | None:
        """Dispatch by method and ``fun`` parameter.

        GET login page → serve page with ``sessionToken`` cookie.
        POST setter with ``fun=login`` → accept login, return SID.
        POST setter with ``fun=logout`` → clear session.
        POST setter with ``fun=restart`` → clear session.
        Other → ``None`` (fall through to route table).
        """
        norm = normalize_path(path)

        if method == "GET" and norm == self._login_page_path:
            return self._login_page_response()

        if method == "POST" and norm == self._setter_endpoint:
            fun = self._parse_fun(body)
            if fun == self._login_fun:
                return self._handle_login_post()
            if fun is not None and fun == self._logout_fun:
                return self._handle_logout_post()
            if fun is not None and fun == self._restart_fun:
                return self._handle_restart_post()

        return None

    def _login_page_response(self) -> RouteEntry:
        """Serve login page HTML with initial ``sessionToken`` cookie."""
        html = "<html><body>CBN Login</body></html>"
        return RouteEntry(
            status=200,
            headers=[
                ("Content-Type", "text/html"),
                (
                    "Set-Cookie",
                    f"{self._session_cookie_name}={self._INITIAL_SESSION_TOKEN}; Path=/",
                ),
            ],
            body=html,
        )

    def _handle_login_post(self) -> RouteEntry:
        """Accept login POST and return success with SID."""
        self._authenticated = True
        new_token = self._next_token()
        _logger.debug("Mock server: CBN login accepted, SID=%s", self._TEST_SID)
        return RouteEntry(
            status=200,
            headers=[
                ("Content-Type", "text/xml"),
                (
                    "Set-Cookie",
                    f"{self._session_cookie_name}={new_token}; Path=/",
                ),
            ],
            body=f"successful SID={self._TEST_SID}",
        )

    def _handle_logout_post(self) -> RouteEntry:
        """Handle logout — clear session."""
        self._authenticated = False
        _logger.debug("Mock server: CBN logout — session cleared")
        # Recorded here rather than by the server: CBN addresses every
        # action at one setter endpoint and discriminates by ``fun``, so
        # the server's path matching never sees a logout.
        self.record_action("logout", 200)
        return RouteEntry(status=200, headers=[], body="OK")

    def _handle_restart_post(self) -> RouteEntry:
        """Handle restart — clear session."""
        self._authenticated = False
        _logger.debug("Mock server: CBN restart accepted — session cleared")
        self.record_action("restart", 200)
        return RouteEntry(status=200, headers=[], body="OK")

    def get_route_override(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> RouteEntry | None:
        """Serve CBN getter responses by ``fun`` parameter.

        All CBN data requests POST to the same getter endpoint with
        varying ``fun=N`` body parameters. The standard route table
        can't differentiate these, so this override dispatches by
        body content. Each response includes a rotated session token.
        """
        if method != "POST" or normalize_path(path) != self._getter_endpoint:
            return None

        fun = self._parse_fun(body)
        if fun is None or fun not in self._getter_responses:
            return None

        base = self._getter_responses[fun]
        new_token = self._next_token()
        response_headers = list(base.headers)
        response_headers.append(
            (
                "Set-Cookie",
                f"{self._session_cookie_name}={new_token}; Path=/",
            )
        )
        return RouteEntry(
            status=base.status,
            headers=response_headers,
            body=base.body,
        )

    @staticmethod
    def _parse_fun(body: bytes) -> int | None:
        """Extract ``fun=N`` from URL-encoded POST body."""
        match = _FUN_RE.search(body)
        return int(match.group(1)) if match else None


def _build_getter_responses(
    har_entries: list[dict[str, Any]],
    getter_endpoint: str,
) -> dict[int, RouteEntry]:
    """Build ``fun→response`` lookup from HAR getter entries.

    Filters HAR entries for POST requests to the getter endpoint,
    extracts the ``fun=N`` parameter from the request body, and maps
    it to the response. For duplicate fun values, the last 200-status
    response wins (matching ``build_routes`` behavior).

    Response headers are preserved except Content-Length (HAR redaction
    may have changed body size) and Set-Cookie (handler adds its own
    rotating token).
    """
    responses: dict[int, RouteEntry] = {}

    for entry in har_entries:
        request = entry.get("request", {})
        response = entry.get("response", {})

        if request.get("method", "").upper() != "POST":
            continue

        url = request.get("url", "")
        path = normalize_path(urlparse(url).path)
        if path != getter_endpoint:
            continue

        # Extract fun from request body
        post_data = request.get("postData", {})
        body_text = post_data.get("text", "")
        match = _FUN_TEXT_RE.search(body_text)
        if not match:
            continue
        fun = int(match.group(1))

        # Extract response
        status = response.get("status", 0)
        body = str(response.get("content", {}).get("text", ""))
        resp_headers: list[tuple[str, str]] = []
        for h in response.get("headers", []):
            name = h.get("name", "")
            lower = name.lower()
            if lower in ("content-length", "set-cookie"):
                continue
            if name:
                resp_headers.append((name, h.get("value", "")))

        # Prefer 200 responses; for non-200, only store if no entry yet
        existing = responses.get(fun)
        if existing is None or status == 200:
            responses[fun] = RouteEntry(
                status=status,
                headers=resp_headers,
                body=body,
            )

    _logger.debug(
        "CBN mock: built getter lookup with %d fun values: %s",
        len(responses),
        sorted(responses.keys()),
    )
    return responses


def create_handler(
    modem_config: ModemConfig,
    har_entries: list[dict[str, Any]] | None = None,
) -> FormCbnAuthHandler:
    """Entry point for dynamic auth handler dispatch."""
    from ...models.modem_config.actions import CbnAction
    from ...models.modem_config.auth import FormCbnAuth

    auth = modem_config.auth
    assert isinstance(auth, FormCbnAuth)

    logout_fun: int | None = None
    restart_fun: int | None = None
    if modem_config.actions:
        if modem_config.actions.logout and isinstance(modem_config.actions.logout, CbnAction):
            logout_fun = modem_config.actions.logout.fun
        if modem_config.actions.restart and isinstance(modem_config.actions.restart, CbnAction):
            restart_fun = modem_config.actions.restart.fun

    return FormCbnAuthHandler(
        login_page_path=auth.login_page,
        setter_endpoint=auth.setter_endpoint,
        getter_endpoint=auth.getter_endpoint,
        session_cookie_name=auth.session_cookie_name,
        login_fun=auth.login_fun,
        logout_fun=logout_fun,
        restart_fun=restart_fun,
        har_entries=har_entries,
    )
