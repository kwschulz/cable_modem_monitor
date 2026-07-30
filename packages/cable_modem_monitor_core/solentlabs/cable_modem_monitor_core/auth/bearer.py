"""Bearer token auth manager (RFC 6750). Not OAuth 2.0; tokens are opaque strings. See MODEM_YAML_SPEC.md § bearer."""

from __future__ import annotations

import logging
from typing import Any

import requests

from ..models.modem_config.auth import BearerAuth
from .base import AuthContext, AuthResult, BaseAuthManager

_logger = logging.getLogger(__name__)


class BearerAuthManager(BaseAuthManager):
    """POSTs credentials as JSON, extracts token via ``token_path``, injects Authorization: Bearer."""

    def __init__(self, config: BearerAuth) -> None:
        self._config = config

    def authenticate(
        self,
        session: requests.Session,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: int = 10,
        log_level: int = logging.DEBUG,
    ) -> AuthResult:
        config = self._config
        login_url = f"{base_url}{config.login_endpoint}"

        _logger.log(log_level, "Bearer login: POST %s", config.login_endpoint)

        # Password-only firmwares send no username key at all; an empty
        # username_field reproduces that body exactly.
        credentials: dict[str, str] = {"password": password}
        if config.username_field:
            credentials = {config.username_field: username, "password": password}

        response = session.post(login_url, json=credentials, timeout=timeout)

        # Token creation legitimately answers 201; any 2xx that carries the
        # token is a successful login.
        if not 200 <= response.status_code < 300:
            return AuthResult(
                success=False,
                error=f"Login returned HTTP {response.status_code}",
                response=response,
            )

        try:
            body: Any = response.json()
        except (ValueError, requests.exceptions.JSONDecodeError):
            return AuthResult(
                success=False,
                error="Login response is not valid JSON",
                response=response,
            )

        token = _extract_token(body, config.token_path)
        if token is None:
            return AuthResult(
                success=False,
                error=f"token_path '{config.token_path}' not found in login response",
                response=response,
            )

        session.headers["Authorization"] = f"Bearer {token}"
        _logger.log(log_level, "Bearer token obtained via %s", config.token_path)

        # Both values reach action endpoints as {auth:token} / {auth:user_id}.
        # An unresolvable user_id_path is not a login failure; only actions
        # that name the placeholder care, and they degrade to a literal path.
        user_id = _extract_user_id(body, config.user_id_path) if config.user_id_path else ""
        if config.user_id_path and not user_id:
            _logger.log(
                log_level,
                "Bearer user_id_path '%s' did not resolve in the login response",
                config.user_id_path,
            )

        return AuthResult(success=True, auth_context=AuthContext(token=token, user_id=user_id))

    def headers(self) -> frozenset[str]:
        """Headers this strategy puts on the wire."""
        return frozenset({"authorization", "cookie"})


def _walk_path(body: Any, path: str) -> Any:
    """Walk a dot-separated path through a JSON dict; return the value at the leaf or None."""
    current: Any = body
    for key in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def _extract_token(body: Any, token_path: str) -> str | None:
    """Walk a dot-separated path through a JSON dict; return the string value or None."""
    current = _walk_path(body, token_path)
    if not isinstance(current, str):
        return None
    return current


def _extract_user_id(body: Any, user_id_path: str) -> str:
    """Walk a dot-separated path to a user identifier; numbers are stored as their string form."""
    # Observed firmwares return the id as a JSON number (F3896LG: userId 3),
    # so unlike the token this accepts int as well as str. bool is an int
    # subclass and is never an identifier; reject it explicitly.
    current = _walk_path(body, user_id_path)
    if isinstance(current, str):
        return current
    if isinstance(current, int) and not isinstance(current, bool):
        return str(current)
    return ""


def create_manager(config: BearerAuth) -> BearerAuthManager:
    """Entry point for dynamic auth factory dispatch."""
    return BearerAuthManager(config)
