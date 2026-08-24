"""Form-nonce auth handler for the HAR mock server.

Extends ``FormAuthHandler`` to serve the login page HTML on GET
and accept credential POSTs.  The test harness runner GETs the
login page to detect credential encoding (plain vs b64-packed)
before creating the auth manager — mirroring the HA config flow's
pre-fetch against a real modem.

Credentials are not validated — any POST to the login path is
accepted.  Real credential validation lives in the auth managers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..routes import RouteEntry, normalize_path
from .base import extract_action_config
from .form import FormAuthHandler

if TYPE_CHECKING:
    from ...models.modem_config import ModemConfig

_logger = logging.getLogger(__name__)


class FormNonceAuthHandler(FormAuthHandler):
    """Form-nonce auth handler that serves the login page on GET.

    Extends :class:`FormAuthHandler` to intercept GET requests to the
    login path and return stored login page HTML.  This mirrors the
    real modem behaviour where GET ``/cgi-bin/adv_pwd_cgi`` returns
    the login form and POST submits credentials.

    Args:
        login_path: Login endpoint path (``auth.action``).
        login_page_html: HTML body to serve on GET to ``login_path``.
        cookie_name: Session cookie name (empty for IP-based).
    """

    def __init__(
        self,
        login_path: str,
        login_page_html: str,
        cookie_name: str = "",
    ) -> None:
        super().__init__(
            login_path=login_path,
            cookie_name=cookie_name,
            login_page_html=login_page_html,
        )

    def is_login_request(self, method: str, path: str) -> bool:
        """GET to login page is also a login-flow request."""
        if method == "GET" and normalize_path(path) == self._login_path:
            return True
        return super().is_login_request(method, path)

    def handle_login(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> RouteEntry | None:
        """Serve login page on GET, accept credentials on POST.

        GET returns the stored login page HTML (for encoding detection).
        POST delegates to parent (sets authenticated flag, returns
        None so the route table response is used).
        """
        if method == "GET" and normalize_path(path) == self._login_path:
            _logger.debug("Mock server: serving login page for pre-fetch")
            return RouteEntry(
                status=200,
                headers=[("Content-Type", "text/html")],
                body=self._login_page_html,
            )

        # POST — delegate to parent (accepts login, returns None).
        return super().handle_login(method, path, body, headers)


def create_handler(
    modem_config: ModemConfig,
    har_entries: list[dict[str, Any]] | None = None,
) -> FormNonceAuthHandler:
    """Entry point for dynamic auth handler dispatch."""
    from ...models.modem_config.auth import FormNonceAuth
    from ..routes import extract_har_response_text

    auth = modem_config.auth
    assert isinstance(auth, FormNonceAuth)

    login_path = auth.action
    action_cfg = extract_action_config(modem_config)

    login_page_html = ""
    if har_entries:
        login_page_html = extract_har_response_text(har_entries, "GET", login_path)

    return FormNonceAuthHandler(
        login_path=login_path,
        login_page_html=login_page_html,
        cookie_name=action_cfg.cookie_name,
    )
