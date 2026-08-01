"""No-auth manager — data endpoints are publicly accessible.

See MODEM_YAML_SPEC.md ``none`` strategy.
"""

from __future__ import annotations

import logging

import requests

from ..models.modem_config.auth import NoneAuth
from .base import AuthFailureMode, AuthResult, BaseAuthManager


class NoneAuthManager(BaseAuthManager):
    """No authentication required.

    All data endpoints are publicly accessible. The session is used
    as-is with no credentials attached.
    """

    def auth_failure_mode(self) -> AuthFailureMode:
        """No credentials exist, so a 401 means the catalog entry is wrong."""
        return AuthFailureMode.NOT_CONFIGURED

    def authenticate(
        self,
        session: requests.Session,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: int = 10,  # noqa: ARG002
        log_level: int = logging.DEBUG,  # noqa: ARG002
    ) -> AuthResult:
        """No-op — always succeeds."""
        return AuthResult(success=True)


def create_manager(config: NoneAuth) -> NoneAuthManager:
    """Entry point for dynamic auth factory dispatch."""
    return NoneAuthManager()
