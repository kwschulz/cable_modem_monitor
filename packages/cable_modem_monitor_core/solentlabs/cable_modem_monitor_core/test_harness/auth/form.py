"""Form-based authentication handler.

Validates that a POST is made to the configured login endpoint.
Tracks session state via a server-side flag (works for both
cookie-based and IP-based sessions). Serves the captured login page
at ``auth.login_page`` before any session exists, as the firmware does.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..routes import RouteEntry, normalize_path
from .base import AuthHandler, extract_action_config

if TYPE_CHECKING:
    from ...models.modem_config import ModemConfig

_logger = logging.getLogger(__name__)


class FormAuthHandler(AuthHandler):
    """Form-based authentication handler.

    Validates that a POST is made to the configured login endpoint.
    Tracks session state via a server-side flag (works for both
    cookie-based and IP-based modems — in tests all traffic is
    localhost).

    Credentials are not validated — any POST to the login path is
    accepted. Real credential validation lives in the auth managers
    (``auth/``). This handler only gates access by session state.

    Args:
        login_path: The login endpoint path (from modem.yaml ``auth.action``).
        cookie_name: Session cookie name if cookie-based (from
            modem.yaml ``auth.cookie_name``). Empty for IP-based.
        login_page: Page Core pre-fetches before the POST (from
            modem.yaml ``auth.login_page``). Empty when none is declared.
        login_page_html: Captured body of that page. Empty when the
            capture never recorded it, in which case the page stays
            behind the challenge like any other route.

    Logout and restart endpoints are not passed here — the base class
    matches them from the declared actions block for every strategy.
    """

    _SESSION_TOKEN = "mock-session-token"

    def __init__(
        self,
        login_path: str,
        cookie_name: str = "",
        *,
        login_page: str = "",
        login_page_html: str = "",
    ) -> None:
        super().__init__()
        self._login_path = normalize_path(login_path)
        self._cookie_name = cookie_name
        self._login_page = normalize_path(login_page) if login_page else ""
        self._login_page_html = login_page_html
        self._authenticated = False

    def is_login_request(self, method: str, path: str) -> bool:
        """POST to the login endpoint, or GET of a captured login page."""
        if method == "POST" and normalize_path(path) == self._login_path:
            return True
        return self._is_login_page_get(method, path)

    def _is_login_page_get(self, method: str, path: str) -> bool:
        """True for a GET of the declared login page when the capture holds it."""
        return bool(self._login_page_html) and method == "GET" and normalize_path(path) == self._login_page

    def handle_login(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> RouteEntry | None:
        """Serve the login page on GET; accept the login POST and set session state.

        The page is served body-only, its framing synthesized. The
        captured headers carry the firmware's pre-auth cookie (Netgear
        sets XSRF_TOKEN on first contact), and a cookie by that name is
        what ``is_authenticated`` reads as a session, so replaying them
        would open every data page without a login POST.

        The POST returns None so the server falls through to the route
        table and serves the HAR response verbatim. A missing login entry
        in the HAR produces a 404, which fails the test — this is
        intentional. HAR fixtures must include the full auth flow;
        validate_har enforces this at intake time.
        """
        if self._is_login_page_get(method, path):
            _logger.debug("Mock server: serving captured login page %s", path)
            return RouteEntry(
                status=200,
                headers=[("Content-Type", "text/html")],
                body=self._login_page_html,
            )

        if not self.is_login_request(method, path):
            return None

        self._authenticated = True
        _logger.debug("Mock server: login accepted at %s", path)
        return None

    def is_authenticated(self, headers: dict[str, str]) -> bool:
        """Check session state."""
        if self._authenticated:
            return True

        if self._cookie_name:
            cookie_header = headers.get("cookie", "")
            if self._cookie_name in cookie_header:
                self._authenticated = True
                return True

        return False

    def set_authenticated(self) -> dict[str, str]:
        """Return Set-Cookie header if cookie-based session."""
        if self._cookie_name:
            return {"Set-Cookie": f"{self._cookie_name}={self._SESSION_TOKEN}; Path=/"}
        return {}

    def handle_logout(self) -> RouteEntry:
        """Clear session state on logout."""
        self._authenticated = False
        _logger.debug("Mock server: logout — session cleared")
        return RouteEntry(status=200, headers=[], body="OK")

    def handle_restart(self) -> RouteEntry:
        """Accept restart and clear session (modem is rebooting)."""
        self._authenticated = False
        _logger.debug("Mock server: restart accepted — session cleared")
        return RouteEntry(status=200, headers=[], body="OK")


def create_handler(
    modem_config: ModemConfig,
    har_entries: list[dict[str, Any]] | None = None,
) -> FormAuthHandler:
    """Entry point for dynamic auth handler dispatch."""
    from ..routes import extract_har_response_text

    auth = modem_config.auth
    login_path = getattr(auth, "action", "") or getattr(auth, "login_endpoint", "")
    login_page = getattr(auth, "login_page", "") or ""
    # The first captured GET of the page is the pre-auth one; a later
    # authenticated visit may answer with a dashboard instead.
    login_page_html = extract_har_response_text(har_entries, "GET", login_page) if har_entries and login_page else ""
    return FormAuthHandler(
        login_path=login_path,
        cookie_name=extract_action_config(modem_config).cookie_name,
        login_page=login_page,
        login_page_html=login_page_html,
    )
