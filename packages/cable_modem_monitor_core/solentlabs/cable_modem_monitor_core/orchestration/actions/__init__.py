"""Action execution — single dispatch for logout and restart commands.

Dispatches modem-side actions to transport-scoped executors based on
the action type (HTTP, HNAP, or CBN).  Both the collector (logout) and
orchestrator (restart) use ``execute_action()`` as the single entry
point.

See MODEM_YAML_SPEC.md Actions section and ORCHESTRATION_SPEC.md.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...connectivity import create_session
from .base import ActionResult
from .cbn_action import execute_cbn_action
from .hnap_action import execute_hnap_action
from .http_action import execute_http_action

if TYPE_CHECKING:
    from ...auth.base import AuthContext
    from ...models.modem_config.actions import CbnAction, HnapAction, HttpAction
    from ...models.modem_config.config import ModemConfig
    from ..collector import ModemDataCollector

_logger = logging.getLogger(__name__)


def execute_action(
    collector: ModemDataCollector,
    modem_config: ModemConfig,
    action: HttpAction | HnapAction | CbnAction,
    *,
    log_level: int = logging.INFO,
) -> ActionResult:
    """Execute an action using the collector's session.

    Single dispatch point for all modem-side actions.  Extracts
    session, base URL, and transport credentials from the collector and
    dispatches to the appropriate transport-scoped executor.

    Args:
        collector: Active collector with authenticated session.
        modem_config: Modem configuration for timeout and auth fields.
        action: Action config from modem.yaml (logout or restart).
        log_level: Log level for action messages. Use ``logging.DEBUG``
            for routine operations (logout) to reduce noise.

    Returns:
        ActionResult with success status and details.
    """
    from ...models.modem_config.actions import CbnAction, HnapAction, HttpAction

    model = modem_config.model

    if isinstance(action, HttpAction):
        query_params: dict[str, str] = {}
        if modem_config.session and modem_config.session.query_params:
            query_params = dict(modem_config.session.query_params)
        session = collector._session
        # {auth:...} placeholders resolve from the session that sends the
        # request, so a per-action session brings its own context.
        auth_context: AuthContext | None = collector._auth_context
        if action.action_auth is not None:
            from ...auth.factory import create_auth_manager_for_action

            fresh = create_session()
            manager = create_auth_manager_for_action(action.action_auth)
            auth_result = manager.authenticate(
                fresh,
                collector._base_url,
                collector._username,
                collector._password,
                timeout=modem_config.timeout,
                log_level=log_level,
            )
            if not auth_result.success:
                return ActionResult(
                    success=False,
                    message=f"Per-action auth failed: {auth_result.error}",
                )
            session = fresh
            auth_context = auth_result.auth_context
        return execute_http_action(
            session,
            collector._base_url,
            action,
            timeout=modem_config.timeout,
            log_level=log_level,
            model=model,
            query_params=query_params or None,
            auth_context=auth_context,
        )

    if isinstance(action, HnapAction):
        private_key = ""
        if collector._auth_context:
            private_key = collector._auth_context.private_key
        hmac_algorithm = getattr(modem_config.auth, "hmac_algorithm", "md5")
        return execute_hnap_action(
            collector._session,
            collector._base_url,
            action,
            private_key=private_key,
            hmac_algorithm=hmac_algorithm,
            timeout=modem_config.timeout,
            log_level=log_level,
            model=model,
        )

    if isinstance(action, CbnAction):
        setter_endpoint = getattr(modem_config.auth, "setter_endpoint", "/xml/setter.xml")
        session_cookie_name = getattr(modem_config.auth, "session_cookie_name", "sessionToken")
        return execute_cbn_action(
            collector._session,
            collector._base_url,
            action,
            setter_endpoint=setter_endpoint,
            session_cookie_name=session_cookie_name,
            timeout=modem_config.timeout,
            log_level=log_level,
            model=model,
        )

    _logger.warning("Unknown action type [%s]: %s", model, type(action).__name__)
    return ActionResult(
        success=False,
        message=f"Unknown action type: {type(action).__name__}",
    )


__all__ = [
    "ActionResult",
    "execute_action",
    "execute_cbn_action",
    "execute_hnap_action",
    "execute_http_action",
]
