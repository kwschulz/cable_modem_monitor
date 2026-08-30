"""Orchestration logging adapter — single-emission event dispatcher.

``log_event(logger, event)`` formats and emits one log line for any
``OrchestratorEvent``. Components never call ``_logger`` directly.
See LOGGING_SPEC.md for the level policy and event inventory.
"""

from __future__ import annotations

import contextvars
import logging
from typing import TYPE_CHECKING

from .auth_stop import auth_stop_advice

if TYPE_CHECKING:
    from .events import OrchestratorEvent

# Maximum body length included in AuthFailed log lines.
_AUTH_BODY_MAX = 500

# Set by capture_events() in tests to collect events instead of logging them.
# None means no capture is active.
_capture_list: contextvars.ContextVar[list | None] = contextvars.ContextVar("_capture_list", default=None)


def log_event(logger: logging.Logger, event: OrchestratorEvent) -> None:
    """Format and emit one log line for the given event."""
    # If a test capture context is active, collect the event instead.
    captured = _capture_list.get()
    if captured is not None:
        captured.append(event)
        return
    logger.log(event.level, _format(event))


def _format(event: OrchestratorEvent) -> str:  # noqa: PLR0911, C901
    """Return the formatted log message for an event."""
    from .events import (
        ActionCompleted,
        ActionConnectionLost,
        ActionFailed,
        ActionPreFetchCompleted,
        ActionPreFetchFailed,
        ActionStarted,
        AuthCircuitBreakerOpen,
        AuthFailed,
        AuthLockoutDetected,
        AuthSucceeded,
        CircuitBreakerPollingBlocked,
        CollectionComplete,
        ConnectionFailedDuringLoad,
        ConnectivityBackoffActive,
        ConnectivityBackoffCleared,
        ConnectivityBackoffReset,
        ConnectivityFailureDetected,
        CounterReset,
        HealthBackoffCleared,
        HealthRecoveryDetected,
        HealthStatusReport,
        HnapConnectionFailed,
        HnapLoadError,
        HnapSessionExpired,
        HttpStatusError,
        LogoutExecuted,
        LogoutFailed,
        ParseError,
        PostLoginFetchFailed,
        RecoveryObserverException,
        RecoveryWindowClosed,
        RecoveryWindowOpened,
        ResourceDecodeError,
        ResourceFetched,
        ResourceLoadError,
        RestartCommandFailed,
        RestartCommandSent,
        SessionCleared,
        SessionRetryFailed,
        SessionRetryStarted,
        SessionRetrySucceeded,
        SessionReused,
        StaleSessionRecoveryDisabled,
        StatusTransition,
        StubPageDetected,
        SystemInfoFieldsChanged,
        ZeroChannelsNoSystemInfo,
    )

    # --- connectivity ---

    if isinstance(event, ConnectivityFailureDetected):
        return (
            f"Connection failure [{event.model}] — unreachable"
            f" (streak: {event.streak}, backoff: {event.backoff_polls} polls)"
        )

    if isinstance(event, ConnectivityBackoffActive):
        return f"Connectivity backoff active [{event.model}] ({event.polls_remaining} remaining), skipping poll"

    if isinstance(event, ConnectivityBackoffCleared):
        return f"Connectivity backoff cleared [{event.model}], retrying"

    if isinstance(event, ConnectivityBackoffReset):
        return f"Connectivity backoff reset [{event.model}] — next poll will attempt connection"

    # --- auth ---

    if isinstance(event, AuthSucceeded):
        msg = f"Auth succeeded [{event.model}] — strategy: {event.strategy}, status={event.status_code}"
        # Only strategies that advertise a reuse page carry a landing path;
        # rendering an empty one on every other strategy would read as a fault.
        if event.response_url:
            msg += f", landed: {event.response_url}"
        return msg

    if isinstance(event, AuthFailed):
        if event.method is None:
            # Connection error, so there is no HTTP response and the modem
            # judged no credential. Naming this "Auth failed" sent users to
            # their password for what is a reachability problem (#200); the
            # ConnectivityFailureDetected line that follows carries the
            # streak and backoff.
            return f"Connection failed during auth [{event.model}] strategy={event.strategy} — {event.error}"
        body = event.response_body or ""
        if len(body) > _AUTH_BODY_MAX:
            body = body[:_AUTH_BODY_MAX] + "... (truncated)"
        # The reason leads, as it does on the connection-error branch
        # above: the wire detail below is evidence for it, not a
        # substitute. A criterion mismatch names both sides here and
        # nowhere else the user looks (#189).
        return (
            f"Auth failed [{event.model}] strategy={event.strategy} — {event.error}"
            f"\n  request: {event.method} {event.url}"
            f"\n  response: {event.status_code} {event.content_type}"
            f"\n  body: {body}"
        )

    if isinstance(event, AuthLockoutDetected):
        return (
            f"Auth lockout [{event.model}] — firmware anti-brute-force triggered,"
            f" stopping immediately (streak: {event.streak})"
        )

    if isinstance(event, AuthCircuitBreakerOpen):
        # The remedy comes from one table so a new trip cause cannot be
        # right here and wrong on the next surface; see auth_stop.py.
        remedy = auth_stop_advice(event.signal, event.status_code).remedy
        if event.status_code == 404:
            # Endpoint absence is not a credential rejection — the
            # device at this address has no login page (wrong device,
            # or the modem's web layer is unavailable).
            return (
                f"Auth circuit breaker OPEN [{event.model}] — login endpoint not found"
                " (HTTP 404: wrong device at this address, or modem web interface"
                f" unavailable). Polling stopped. {remedy}"
            )
        return (
            f"Auth circuit breaker OPEN [{event.model}] — {event.streak} consecutive"
            f" auth failures. Polling stopped. {remedy}"
        )

    if isinstance(event, CircuitBreakerPollingBlocked):
        remedy = auth_stop_advice(event.signal, event.status_code).remedy
        if event.status_code == 404:
            # Preserve the trip reason on every blocked poll — after a
            # 404 trip, credentials are not the fix.
            return (
                f"Circuit breaker OPEN [{event.model}] — login endpoint not found"
                f" (HTTP 404). Polling stopped. {remedy}"
            )
        return f"Circuit breaker OPEN [{event.model}] — polling stopped. {remedy}"

    if isinstance(event, StaleSessionRecoveryDisabled):
        return (
            f"Recovered stale-session streak reached threshold [{event.model}]"
            f" — disabling session reuse for this runtime"
            f" ({event.streak} consecutive recoveries)"
        )

    # --- session ---

    if isinstance(event, SessionReused):
        return f"Session reused [{event.model}]"

    if isinstance(event, SessionCleared):
        return f"Session cleared [{event.model}]"

    if isinstance(event, LogoutExecuted):
        return f"Logout executed [{event.model}]"

    if isinstance(event, LogoutFailed):
        return f"Logout failed [{event.model}] — {event.reason}"

    if isinstance(event, PostLoginFetchFailed):
        detail = f"HTTP {event.status_code}" if event.status_code is not None else "no response"
        return f"Post-login fetch failed [{event.model}] — {event.path}: {detail} {event.reason}".rstrip()

    if isinstance(event, HnapSessionExpired):
        return f"HNAP session expired [{event.model}] — HTTP {event.status_code}"

    if isinstance(event, StubPageDetected):
        return (
            f"Stub page detected [{event.model}] — {event.path}:"
            f" {event.anchors_found}/{event.anchors_expected} anchors found"
        )

    if isinstance(event, SessionRetryStarted):
        return f"{event.signal_name} [{event.model}] — clearing session and retrying once in same poll"

    if isinstance(event, SessionRetrySucceeded):
        return f"{event.signal_name} recovered [{event.model}] — fresh login succeeded in same poll"

    if isinstance(event, SessionRetryFailed):
        return (
            f"{event.signal_name} [{event.model}] — retry failed,"
            f" reporting auth_failed (streak: {event.streak}/{event.threshold})"
        )

    # --- probe / health ---

    if isinstance(event, HealthStatusReport):
        return f"Health check [{event.model}]: {event.status} — {event.detail}"

    if isinstance(event, HealthRecoveryDetected):
        return f"Health recovered [{event.model}] — was {event.previous_status}"

    if isinstance(event, HealthBackoffCleared):
        return f"Health recovery detected [{event.model}] — clearing connectivity backoff"

    # --- collection / parsing ---

    if isinstance(event, CollectionComplete):
        return (
            f"Collection complete [{event.model}]"
            f" — DS: {event.ds_count}, US: {event.us_count}"
            f" ({event.elapsed_ms:.0f}ms)"
        )

    if isinstance(event, ParseError):
        return f"Parse error [{event.model}] — {event.reason}"

    if isinstance(event, ResourceLoadError):
        return f"Resource load error [{event.model}] — {event.path}: {event.reason}"

    if isinstance(event, HttpStatusError):
        summary = f"HTTP {event.status_code} [{event.model}] — {event.path}: {event.reason}"
        if not (event.request_line or event.response_body):
            return summary
        return (
            f"{summary}"
            f"\n  request: {event.request_line}"
            f"\n  response: {event.status_code} {event.content_type}"
            f"\n  body: {event.response_body}"
        )

    if isinstance(event, ConnectionFailedDuringLoad):
        return f"Connection failed during load [{event.model}] — {event.path}: {event.reason}"

    if isinstance(event, HnapConnectionFailed):
        return f"HNAP connection failed [{event.model}] — {event.reason}"

    if isinstance(event, HnapLoadError):
        return f"HNAP load error [{event.model}] — {event.reason}"

    if isinstance(event, ZeroChannelsNoSystemInfo):
        return f"Zero channels and no system_info [{event.model}] — cannot confirm parser health"

    if isinstance(event, SystemInfoFieldsChanged):
        parts = []
        if event.lost:
            parts.append(f"lost: {', '.join(sorted(event.lost))}")
        if event.gained:
            parts.append(f"gained: {', '.join(sorted(event.gained))}")
        return f"system_info fields changed [{event.model}] — {'; '.join(parts)}"

    if isinstance(event, StatusTransition):
        return f"Status transition [{event.model}]: {event.from_status} → {event.to_status}"

    if isinstance(event, CounterReset):
        return (
            f"Counter reset detected [{event.model}]"
            f" — corrected: {event.prev_corrected}→{event.cur_corrected},"
            f" uncorrected: {event.prev_uncorrected}→{event.cur_uncorrected}"
        )

    # --- restart / recovery ---

    if isinstance(event, RestartCommandSent):
        return f"Restart command sent [{event.model}] — session cleared ({event.elapsed_seconds:.1f}s)"

    if isinstance(event, RestartCommandFailed):
        return f"Restart command failed [{event.model}] — {event.reason}"

    if isinstance(event, RecoveryWindowOpened):
        return f"Recovery window open [{event.model}] — reason: {event.reason}"

    if isinstance(event, RecoveryWindowClosed):
        return (
            f"Recovery window closed [{event.model}]"
            f" — elapsed: {event.elapsed_seconds:.0f}s,"
            f" last snapshot docsis: {event.last_docsis_status}"
        )

    if isinstance(event, RecoveryObserverException):
        return f"Recovery observer exception [{event.model}] — {event.exc_type}"

    # --- actions ---

    if isinstance(event, ActionStarted):
        return f"Action started [{event.model}] — {event.transport}/{event.action_name}"

    if isinstance(event, ActionCompleted):
        return f"Action completed [{event.model}] — {event.transport}/{event.action_name}: {event.result}"

    if isinstance(event, ActionConnectionLost):
        return f"Action connection lost [{event.model}] — {event.transport}/{event.action_name}"

    if isinstance(event, ActionFailed):
        return f"Action failed [{event.model}] — {event.transport}/{event.action_name}: {event.reason}"

    if isinstance(event, ActionPreFetchCompleted):
        keys = f"{event.key_count} keys" if event.key_count is not None else "no keys"
        fallback = f", fallback: {event.fallback_endpoint}" if event.fallback_endpoint is not None else ""
        return f"Action pre-fetch completed [{event.model}] — {event.transport}/{event.action_name}: {keys}{fallback}"

    if isinstance(event, ActionPreFetchFailed):
        suffix = (
            f" (continuing with fallback: {event.fallback_endpoint})" if event.fallback_endpoint is not None else ""
        )
        return (
            f"Action pre-fetch failed [{event.model}] — {event.transport}/{event.action_name}: {event.reason}{suffix}"
        )

    # --- resource loading ---

    if isinstance(event, ResourceFetched):
        return (
            f"Resource fetched [{event.model}] — {event.path}"
            f" ({event.status_code}, {event.size_bytes}B, {event.elapsed_ms:.0f}ms)"
        )

    if isinstance(event, ResourceDecodeError):
        return f"Resource decode error [{event.model}] — {event.path}: {event.fmt}: {event.reason}"

    # Defensive — all event types must be handled above.
    return repr(event)
