"""Base auth handler — no authentication required.

Provides the ``AuthHandler`` base class used by all auth strategies.
When used directly, all requests are considered authenticated (no-auth
mode for ``auth: none`` modems).

Also provides ``ActionConfig``, the declared logout/restart endpoints
the factory hands every handler so action matching is identical across
auth strategies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from ..routes import RouteEntry, normalize_path

if TYPE_CHECKING:
    from ...models.modem_config import ModemConfig


class ActionConfig(NamedTuple):
    """Shared action fields extracted from modem config.

    Carries the cookie name plus the method and path of each declared
    ``type: http`` action. Other action types (``hnap``, ``cbn``) are
    dispatched by their own handlers and leave these paths empty.
    """

    cookie_name: str
    logout_method: str
    logout_path: str
    restart_method: str
    restart_path: str


def extract_action_config(modem_config: ModemConfig) -> ActionConfig:
    """Extract shared action fields from modem config.

    Reads session cookie name and action endpoints (logout, restart)
    from the config.
    """
    from ...models.modem_config.actions import HttpAction

    cookie_name = getattr(modem_config.auth, "cookie_name", "")
    logout_path = ""
    logout_method = "GET"
    restart_path = ""
    restart_method = "POST"
    if modem_config.actions:
        if modem_config.actions.logout and isinstance(modem_config.actions.logout, HttpAction):
            logout_path = modem_config.actions.logout.endpoint
            logout_method = modem_config.actions.logout.method
        if modem_config.actions.restart and isinstance(modem_config.actions.restart, HttpAction):
            restart_path = modem_config.actions.restart.endpoint
            restart_method = modem_config.actions.restart.method
    return ActionConfig(
        cookie_name=cookie_name,
        logout_method=logout_method,
        logout_path=logout_path,
        restart_method=restart_method,
        restart_path=restart_path,
    )


def _segments(path: str) -> tuple[str, ...]:
    """Split a normalized path into comparable segments."""
    return tuple(s for s in normalize_path(path).split("/") if s)


def _template_match(template: tuple[str, ...], actual: tuple[str, ...]) -> bool:
    """Match a declared endpoint against a request path, ``{auth:…}`` segments wild.

    An endpoint may address the session in the URL
    (``/rest/v1/user/{auth:user_id}/token/{auth:token}``), so the
    segments Core interpolates at action time cannot be compared
    literally. Everything else must match exactly.
    """
    if len(template) != len(actual):
        return False
    return all(
        ("{" in expected and "}" in expected) or expected == got for expected, got in zip(template, actual, strict=True)
    )


class AuthHandler:
    """Base auth handler — no authentication required.

    Used for ``auth: none`` modems. All requests are considered
    authenticated.

    Action matching lives here rather than in each strategy. It is
    driven entirely by the declared ``actions:`` block, so a modem's
    logout and restart are recognised the same way whatever its auth
    strategy. Handlers that previously matched for themselves diverged:
    ``basic`` and ``none`` never matched at all, so a declared restart
    fell through to the route table, 404'd, and passed anyway.
    """

    def __init__(self) -> None:
        self._actions = ActionConfig("", "GET", "", "POST", "")
        self.served_actions: dict[str, int] = {}

    def configure_actions(self, actions: ActionConfig) -> None:
        """Set the declared action endpoints this handler answers."""
        self._actions = actions

    def record_action(self, kind: str, status: int) -> None:
        """Record the status served for a dispatched action.

        Read back by the test runner: the orchestrated replay fires
        logout on every successful poll and Core deliberately ignores
        the result (ORCHESTRATION_SPEC — logout is best-effort at both
        call sites), so the harness is the only place that can notice
        a logout that did not work.
        """
        self.served_actions[kind] = status

    def is_login_request(self, method: str, path: str) -> bool:
        """Check if this request targets the login endpoint."""
        return False

    def handle_login(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> RouteEntry | None:
        """Handle a login request. Returns response or None to pass through."""
        return None

    def get_challenge_response(self) -> RouteEntry:
        """Return the 401 challenge response for unauthenticated requests."""
        return RouteEntry(status=401, headers=[], body="Unauthorized")

    def is_authenticated(self, headers: dict[str, str]) -> bool:
        """Check if the request is authenticated."""
        return True

    def set_authenticated(self) -> dict[str, str]:
        """Mark session as authenticated. Returns headers to add to response."""
        return {}

    def is_logout_request(self, method: str, path: str) -> bool:
        """Check if this request targets the declared logout endpoint."""
        if not self._actions.logout_path or method != self._actions.logout_method.upper():
            return False
        return _template_match(_segments(self._actions.logout_path), _segments(path))

    def handle_logout(self) -> RouteEntry:
        """Handle a logout request. Clears session state."""
        return RouteEntry(status=200, headers=[], body="OK")

    def is_restart_request(self, method: str, path: str) -> bool:
        """Check if this request targets the declared restart endpoint."""
        if not self._actions.restart_path or method != self._actions.restart_method.upper():
            return False
        return _template_match(_segments(self._actions.restart_path), _segments(path))

    def handle_restart(self) -> RouteEntry:
        """Handle a restart request. Returns 200 and clears session."""
        return RouteEntry(status=200, headers=[], body="OK")

    def get_route_override(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> RouteEntry | None:
        """Override the route table for this request.

        Returns a ``RouteEntry`` to bypass the route table, or ``None``
        to use the standard route table lookup. Used by HNAP to serve
        merged data responses from a single endpoint.
        """
        return None
