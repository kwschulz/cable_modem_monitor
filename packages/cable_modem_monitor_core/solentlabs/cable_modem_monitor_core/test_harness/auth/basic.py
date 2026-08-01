"""HTTP Basic Authentication handler.

Checks that an Authorization header with the Basic scheme is present.
Stateless — no session tracking.

When ``challenge_cookie`` is enabled, the 401 challenge response
includes a ``Set-Cookie`` header that the real ``BasicAuthManager``
captures and sends on subsequent requests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..routes import RouteEntry
from .base import AuthHandler

if TYPE_CHECKING:
    from ...models.modem_config import ModemConfig


class BasicAuthHandler(AuthHandler):
    """HTTP Basic Authentication handler.

    Checks that an Authorization header with the Basic scheme is
    present. Credentials are not validated — any Basic header is
    accepted. Real credential validation lives in the auth managers.
    Stateless — no session tracking.

    Args:
        challenge_cookie: Whether to set a cookie on 401 responses.
        cookie_name: Cookie name to set (e.g., ``"XSRF_TOKEN"``).
    """

    def __init__(
        self,
        *,
        challenge_cookie: bool = False,
        cookie_name: str = "",
    ) -> None:
        super().__init__()
        self._challenge_cookie = challenge_cookie
        self._cookie_name = cookie_name

    def get_challenge_response(self) -> RouteEntry:
        """Return 401 with optional Set-Cookie for challenge_cookie modems."""
        headers: list[tuple[str, str]] = [
            ("WWW-Authenticate", 'Basic realm="modem"'),
        ]
        if self._challenge_cookie and self._cookie_name:
            headers.append(
                ("Set-Cookie", f"{self._cookie_name}=mock-challenge; Path=/"),
            )
        return RouteEntry(status=401, headers=headers, body="Unauthorized")

    def is_authenticated(self, headers: dict[str, str]) -> bool:
        """Check for valid Basic auth header."""
        auth_header = headers.get("authorization", "")
        return auth_header.lower().startswith("basic ")


def create_handler(
    modem_config: ModemConfig,
    har_entries: list[dict[str, Any]] | None = None,
) -> BasicAuthHandler:
    """Entry point for dynamic auth handler dispatch."""
    from ...models.modem_config.auth import BasicAuth

    auth = modem_config.auth
    assert isinstance(auth, BasicAuth)
    return BasicAuthHandler(
        challenge_cookie=auth.challenge_cookie,
        cookie_name=auth.cookie_name,
    )
