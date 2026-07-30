"""HTTP action executor — pre-fetch, form-action extraction, main request.

Handles HTTP transport actions (logout, restart) defined in modem.yaml.
Supports dynamic endpoint extraction from pre-fetch pages via
form-action keyword matching.

Architecture decision: ``endpoint_pattern`` is a keyword/substring,
not a regex.  The executor wraps it in a form-action regex internally.
This follows the same pattern as auth strategies — core provides the
extraction behaviour, modem.yaml supplies the parameter.  If a future
modem needs non-form extraction (JavaScript variable, meta tag), add
an ``extraction_mode`` field to ``HttpAction`` and a new strategy here.

See MODEM_YAML_SPEC.md Actions section and ORCHESTRATION_SPEC.md.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import requests
import requests.cookies

from ..events import (
    ActionCompleted,
    ActionConnectionLost,
    ActionPreFetchCompleted,
    ActionPreFetchFailed,
    ActionStarted,
    EventLevel,
)
from ..logging import log_event
from .base import ActionResult

if TYPE_CHECKING:
    from ...auth.base import AuthContext
    from ...models.modem_config.actions import HttpAction

_logger = logging.getLogger(__name__)


def execute_http_action(
    session: requests.Session,
    base_url: str,
    action: HttpAction,
    timeout: int = 10,
    *,
    log_level: int = logging.INFO,
    model: str = "",
    query_params: dict[str, str] | None = None,
    auth_context: AuthContext | None = None,
) -> ActionResult:
    """Execute an HTTP action (logout, restart); connection errors on restart are treated as success."""
    stripped_base = base_url.rstrip("/")
    level = EventLevel(log_level)
    action_name = action.method

    # Phase 1 + 2: Pre-fetch and endpoint extraction
    endpoint = _resolve_endpoint(
        session, stripped_base, action, timeout, log_level=log_level, model=model, action_name=action_name
    )
    if endpoint is None:
        return ActionResult(
            success=False,
            message=(
                f"Endpoint extraction failed for keyword "
                f'"{action.endpoint_pattern}" and no static endpoint '
                f"fallback configured"
            ),
        )

    # Phase 3 + 4: Param interpolation and main request
    if "{auth:" in endpoint:
        endpoint = _interpolate_auth_endpoint(endpoint, auth_context)
    url = f"{stripped_base}{endpoint}"
    if query_params:
        sep = "&" if "?" in url else "?"
        qs = "&".join(f"{k}={v}" for k, v in query_params.items())
        url = f"{url}{sep}{qs}"
    headers = dict(action.headers) if action.headers else None

    log_event(_logger, ActionStarted(model=model, transport="http", action_name=action_name, level=level))

    try:
        if action.json_body is not None:
            resp = session.request(
                action.method,
                url,
                json=action.json_body,
                headers=headers,
                timeout=timeout,
            )
        else:
            raw_params = dict(action.params) if action.params else None
            data: dict[str, str] | None = (
                _interpolate_cookie_params(raw_params, session.cookies)
                if raw_params and any("{cookie:" in v for v in raw_params.values())
                else raw_params
            )
            resp = session.request(
                action.method,
                url,
                data=data,
                headers=headers,
                timeout=timeout,
            )
        log_event(
            _logger,
            ActionCompleted(
                model=model,
                transport="http",
                action_name=action_name,
                status_code=resp.status_code,
                result="ok" if resp.ok else "error",
                level=level,
            ),
        )
        return ActionResult(
            success=True,
            message=f"Action completed with status {resp.status_code}",
            details={"status_code": resp.status_code},
        )
    except (requests.ConnectionError, requests.Timeout):
        log_event(_logger, ActionConnectionLost(model=model, transport="http", action_name=action_name, level=level))
        return ActionResult(
            success=True,
            message="Action sent (connection lost — expected for restart)",
        )


def _resolve_endpoint(
    session: requests.Session,
    base_url: str,
    action: HttpAction,
    timeout: int,
    *,
    log_level: int = logging.INFO,
    model: str = "",
    action_name: str = "",
) -> str | None:
    """Return the resolved action endpoint; ``None`` if keyword extraction fails with no static fallback."""
    if not action.pre_fetch_url:
        return action.endpoint

    level = EventLevel(log_level)
    pre_url = f"{base_url}{action.pre_fetch_url}"

    # Phase 1: Pre-fetch (start log dropped — no ActionPreFetchStarted event)
    try:
        pre_resp = session.get(pre_url, timeout=timeout)
        # Response bytes log dropped — absorbed into downstream pre-fetch events
    except (requests.ConnectionError, requests.Timeout):
        # Pre-fetch may only be for session state; try static endpoint
        fallback = action.endpoint or None
        log_event(
            _logger,
            ActionPreFetchFailed(
                model=model,
                transport="http",
                action_name=action_name,
                reason="connection_lost",
                fallback_endpoint=fallback,
            ),
        )
        return fallback

    # Phase 2: Endpoint extraction (only if keyword is configured)
    if action.endpoint_pattern:
        extracted = _extract_form_action(pre_resp.text, action.endpoint_pattern)
        if extracted:
            log_event(
                _logger,
                ActionPreFetchCompleted(
                    model=model,
                    transport="http",
                    action_name=action_name,
                    key_count=1,
                    fallback_endpoint=None,
                    level=level,
                ),
            )
            return extracted

        # Extraction failed — use fallback or fail
        if action.endpoint:
            log_event(
                _logger,
                ActionPreFetchCompleted(
                    model=model,
                    transport="http",
                    action_name=action_name,
                    key_count=0,
                    fallback_endpoint=action.endpoint,
                    level=level,
                ),
            )
            return action.endpoint

        log_event(
            _logger,
            ActionPreFetchFailed(
                model=model,
                transport="http",
                action_name=action_name,
                reason=f'keyword "{action.endpoint_pattern}" not found in any form action',
                fallback_endpoint=None,
            ),
        )
        return None

    return action.endpoint


def _extract_form_action(page_html: str, keyword: str) -> str | None:
    """Return the first ``<form action>`` URL containing ``keyword``, or ``None``."""
    if not page_html:
        return None

    pattern = re.compile(
        r"<form[^>]*\saction=[\"']" r"([^\"']*" + re.escape(keyword) + r"[^\"']*)" r"[\"']",
        re.IGNORECASE,
    )
    match = pattern.search(page_html)
    if match:
        return match.group(1)
    return None


_COOKIE_PLACEHOLDER_RE = re.compile(r"\{cookie:([^}]+)\}")


def _interpolate_cookie_params(
    params: dict[str, str],
    cookies: requests.cookies.RequestsCookieJar,
) -> dict[str, str]:
    """Replace ``{cookie:name}`` placeholders in params with values from the session jar.

    Absent cookies are left as the literal placeholder string.
    """

    def _resolve(m: re.Match[str]) -> str:
        val = cookies.get(m.group(1))
        return m.group(0) if val is None else val

    return {k: _COOKIE_PLACEHOLDER_RE.sub(_resolve, v) for k, v in params.items()}


_AUTH_PLACEHOLDER_RE = re.compile(r"\{auth:([^}]+)\}")


def _interpolate_auth_endpoint(endpoint: str, auth_context: AuthContext | None) -> str:
    """Replace ``{auth:token}`` / ``{auth:user_id}`` in an endpoint with the authenticated session's values.

    Unresolved keys are left as the literal placeholder, matching
    ``{cookie:name}``. A fixed key set, not a template language;
    see MODEM_YAML_SPEC.md § Auth-value placeholders.
    """
    values = {"token": auth_context.token, "user_id": auth_context.user_id} if auth_context is not None else {}

    def _resolve(m: re.Match[str]) -> str:
        return values.get(m.group(1)) or m.group(0)

    return _AUTH_PLACEHOLDER_RE.sub(_resolve, endpoint)
