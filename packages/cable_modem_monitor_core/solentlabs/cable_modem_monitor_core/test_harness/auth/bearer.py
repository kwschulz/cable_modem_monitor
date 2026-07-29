"""Bearer token auth handler.

Serves a token from the login endpoint and requires it back as
``Authorization: Bearer <token>`` on subsequent requests. The login
response is shaped from the config's ``token_path`` so the real
``BearerAuthManager`` extracts the token by the same walk it uses
against hardware.

Answers ``201 Created`` — the status the Sagemcom F3896LG firmware
returns for token creation (issue #185).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from ..routes import RouteEntry, normalize_path
from .base import AuthHandler, extract_action_config

if TYPE_CHECKING:
    from ...models.modem_config import ModemConfig

_logger = logging.getLogger(__name__)

_MOCK_TOKEN = "mock-bearer-token"


def _nest(token_path: str, token: str) -> dict[str, Any]:
    """Build the response body that ``token_path`` walks down to reach the token."""
    keys = token_path.split(".")
    body: dict[str, Any] = {keys[-1]: token}
    for key in reversed(keys[:-1]):
        body = {key: body}
    return body


class BearerAuthHandler(AuthHandler):
    """Issues a bearer token at the login endpoint and enforces it thereafter."""

    def __init__(self, login_path: str, token_path: str, restart_path: str, restart_method: str) -> None:
        self._login_path = normalize_path(login_path)
        self._token_path = token_path
        self._restart_path = normalize_path(restart_path) if restart_path else ""
        self._restart_method = restart_method

    def is_login_request(self, method: str, path: str) -> bool:
        """Check if this is a POST to the login endpoint."""
        return method == "POST" and normalize_path(path) == self._login_path

    def handle_login(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> RouteEntry | None:
        """Return 201 with the token nested at token_path."""
        if not self.is_login_request(method, path):
            return None

        _logger.debug("Mock server: bearer login accepted at %s", path)
        return RouteEntry(
            status=201,
            headers=[("Content-Type", "application/json")],
            body=json.dumps(_nest(self._token_path, _MOCK_TOKEN)),
        )

    def is_authenticated(self, headers: dict[str, str]) -> bool:
        """Require the issued token back on the Authorization header."""
        return headers.get("authorization", "") == f"Bearer {_MOCK_TOKEN}"

    def get_challenge_response(self) -> RouteEntry:
        """Return 401 for requests arriving without the bearer token."""
        return RouteEntry(status=401, headers=[], body="Unauthorized")

    def is_restart_request(self, method: str, path: str) -> bool:
        """Check if this request targets the restart endpoint."""
        if not self._restart_path:
            return False
        return method == self._restart_method and normalize_path(path) == self._restart_path

    def handle_restart(self) -> RouteEntry:
        """Accept restart — the modem is rebooting, so the token dies with it."""
        _logger.debug("Mock server: restart accepted — bearer token invalidated")
        return RouteEntry(status=200, headers=[], body="OK")


def create_handler(
    modem_config: ModemConfig,
    har_entries: list[dict[str, Any]] | None = None,
) -> BearerAuthHandler:
    """Entry point for dynamic auth handler dispatch."""
    from ...models.modem_config.auth import BearerAuth

    auth = modem_config.auth
    assert isinstance(auth, BearerAuth)
    actions = extract_action_config(modem_config)
    return BearerAuthHandler(
        login_path=auth.login_endpoint,
        token_path=auth.token_path,
        restart_path=actions.restart_path,
        restart_method=actions.restart_method,
    )
