"""Orchestration log event dataclasses.

All log output from the orchestration layer flows through typed events.
See LOGGING_SPEC.md for the full inventory, level policy, and test pattern.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import IntEnum


class EventLevel(IntEnum):
    """Log level for orchestration events.

    Values match ``logging.*`` constants — passable directly to ``_logger.log()``.
    """

    DEBUG = logging.DEBUG  # 10
    INFO = logging.INFO  # 20
    WARNING = logging.WARNING  # 30
    ERROR = logging.ERROR  # 40


# ---------------------------------------------------------------------------
# Phase: connectivity
# ---------------------------------------------------------------------------


@dataclass
class ConnectivityFailureDetected:
    """First connectivity failure in a streak; backoff set."""

    model: str
    streak: int
    backoff_polls: int
    level: EventLevel = field(default=EventLevel.WARNING, init=False)


@dataclass
class ConnectivityBackoffActive:
    """Poll skipped — still inside connectivity backoff window."""

    model: str
    polls_remaining: int
    level: EventLevel = field(default=EventLevel.INFO, init=False)


@dataclass
class ConnectivityBackoffCleared:
    """Connectivity backoff counter reached zero; polling resumes."""

    model: str
    level: EventLevel = field(default=EventLevel.INFO, init=False)


@dataclass
class ConnectivityBackoffReset:
    """User-triggered manual refresh cleared an active connectivity backoff."""

    model: str
    level: EventLevel = field(default=EventLevel.INFO, init=False)


# ---------------------------------------------------------------------------
# Phase: auth
# ---------------------------------------------------------------------------


@dataclass
class AuthSucceeded:
    """Authentication completed. Level is caller-determined."""

    model: str
    strategy: str
    status_code: int  # response.status_code if response is not None else 0
    level: EventLevel  # caller-determined: INFO on first poll, DEBUG on reuse


@dataclass
class AuthFailed:
    """Authentication failed.

    Response-related fields are None when auth failed with a connection
    error (no HTTP response). Callers strip the query string from url and
    scrub the password from response_body before constructing this event.
    """

    model: str
    strategy: str
    error: str
    method: str | None
    url: str | None
    status_code: int | None
    content_type: str | None
    response_body: str | None
    level: EventLevel = field(default=EventLevel.WARNING, init=False)


@dataclass
class AuthLockoutDetected:
    """Firmware anti-brute-force lockout detected."""

    model: str
    streak: int
    level: EventLevel = field(default=EventLevel.WARNING, init=False)


@dataclass
class AuthCircuitBreakerOpen:
    """Auth circuit breaker opened; polling stopped."""

    model: str
    streak: int
    status_code: int | None = None
    level: EventLevel = field(default=EventLevel.ERROR, init=False)


@dataclass
class CircuitBreakerPollingBlocked:
    """Poll skipped — circuit breaker is open; status_code preserves the trip reason."""

    model: str
    status_code: int | None = None
    level: EventLevel = field(default=EventLevel.ERROR, init=False)


@dataclass
class StaleSessionRecoveryDisabled:
    """Stale-session recovery streak hit threshold; session reuse disabled for this runtime."""

    model: str
    streak: int
    level: EventLevel = field(default=EventLevel.INFO, init=False)


# ---------------------------------------------------------------------------
# Phase: session
# ---------------------------------------------------------------------------


@dataclass
class SessionReused:
    """Prior session cookie reused without re-authenticating."""

    model: str
    level: EventLevel = field(default=EventLevel.DEBUG, init=False)


@dataclass
class SessionCleared:
    """Session cookie cleared; next poll will re-authenticate."""

    model: str
    level: EventLevel = field(default=EventLevel.DEBUG, init=False)


@dataclass
class LogoutExecuted:
    """Logout action sent to modem."""

    model: str
    level: EventLevel = field(default=EventLevel.DEBUG, init=False)


@dataclass
class LogoutFailed:
    """Logout action failed."""

    model: str
    reason: str
    level: EventLevel = field(default=EventLevel.WARNING, init=False)


@dataclass
class PostLoginFetchFailed:
    """A declared post-login endpoint did not answer 2xx; collection continues.

    status_code is None when the request raised before a response
    arrived (connection error or timeout).
    """

    model: str
    path: str
    status_code: int | None
    reason: str
    level: EventLevel = field(default=EventLevel.WARNING, init=False)


@dataclass
class HnapSessionExpired:
    """HNAP HTTP error on a reused session — session likely expired server-side."""

    model: str
    status_code: int
    level: EventLevel = field(default=EventLevel.WARNING, init=False)


@dataclass
class StubPageDetected:
    """0 of N expected parser anchors found — stub or login page served at data URL."""

    model: str
    path: str
    anchors_found: int
    anchors_expected: int
    level: EventLevel = field(default=EventLevel.WARNING, init=False)


@dataclass
class SessionRetryStarted:
    """Single-poll session retry started for a LOAD_AUTH or LOAD_INTEGRITY signal."""

    model: str
    signal_name: str  # "LOAD_AUTH" or "LOAD_INTEGRITY"
    level: EventLevel = field(default=EventLevel.INFO, init=False)


@dataclass
class SessionRetrySucceeded:
    """Single-poll session retry succeeded — fresh login obtained in same poll."""

    model: str
    signal_name: str  # "LOAD_AUTH" or "LOAD_INTEGRITY"
    level: EventLevel = field(default=EventLevel.INFO, init=False)


@dataclass
class SessionRetryFailed:
    """Single-poll retry failed — policy recording signal as auth failure.

    Fires when policy receives a LOAD_AUTH or LOAD_INTEGRITY signal,
    meaning the orchestrator's same-poll retry already failed.
    """

    model: str
    signal_name: str  # "LOAD_AUTH" or "LOAD_INTEGRITY"
    streak: int
    threshold: int
    level: EventLevel = field(default=EventLevel.INFO, init=False)


# ---------------------------------------------------------------------------
# Phase: probe / health
# ---------------------------------------------------------------------------


@dataclass
class HealthStatusReport:
    """Health status derived from probes. Level is internally computed.

    WARNING on transition to DEGRADED or UNRESPONSIVE; INFO on any other
    status change; DEBUG on steady-state (no change).
    """

    model: str
    status: str
    changed: bool
    detail: str
    level: EventLevel = field(init=False)

    def __post_init__(self) -> None:
        if self.changed and self.status in ("degraded", "unresponsive"):
            self.level = EventLevel.WARNING
        elif self.changed:
            self.level = EventLevel.INFO
        else:
            self.level = EventLevel.DEBUG


@dataclass
class HealthRecoveryDetected:
    """Modem recovered from a degraded or unresponsive state."""

    model: str
    previous_status: str
    level: EventLevel = field(default=EventLevel.INFO, init=False)


@dataclass
class HealthBackoffCleared:
    """Health probe confirmed modem reachable — connectivity backoff cleared."""

    model: str
    level: EventLevel = field(default=EventLevel.INFO, init=False)


# ---------------------------------------------------------------------------
# Phase: collection / parsing
# ---------------------------------------------------------------------------


@dataclass
class CollectionComplete:
    """Parse pipeline completed. Level is caller-determined."""

    model: str
    ds_count: int
    us_count: int
    elapsed_ms: float
    level: EventLevel  # caller-determined: INFO on first poll, DEBUG on steady-state


@dataclass
class ParseError:
    """Parser error on a resource."""

    model: str
    reason: str
    level: EventLevel = field(default=EventLevel.WARNING, init=False)


@dataclass
class ResourceLoadError:
    """Resource could not be loaded."""

    model: str
    path: str
    status_code: int | None
    reason: str
    level: EventLevel = field(default=EventLevel.WARNING, init=False)


@dataclass
class HttpStatusError:
    """HTTP 4xx/5xx on a resource; wire-detail fields are 401/403-only."""

    model: str
    path: str
    status_code: int | None
    reason: str
    request_line: str = ""
    content_type: str = ""
    response_body: str = ""
    level: EventLevel = field(default=EventLevel.WARNING, init=False)


@dataclass
class ConnectionFailedDuringLoad:
    """Connection dropped mid-load."""

    model: str
    path: str
    status_code: int | None
    reason: str
    level: EventLevel = field(default=EventLevel.WARNING, init=False)


@dataclass
class HnapConnectionFailed:
    """HNAP connection failed during collection."""

    model: str
    reason: str
    level: EventLevel = field(default=EventLevel.WARNING, init=False)


@dataclass
class HnapLoadError:
    """HNAP load error during collection."""

    model: str
    reason: str
    level: EventLevel = field(default=EventLevel.WARNING, init=False)


@dataclass
class ZeroChannelsNoSystemInfo:
    """Zero channels and no system_info — cannot confirm parser health."""

    model: str
    level: EventLevel = field(default=EventLevel.WARNING, init=False)


@dataclass
class SystemInfoFieldsChanged:
    """system_info field set changed between polls — possible firmware update.

    Only fires when the parser-level field set changes (docsis_status is
    included; orchestrator-derived rate_* fields are excluded).
    """

    model: str
    gained: frozenset[str]
    lost: frozenset[str]
    level: EventLevel = field(default=EventLevel.WARNING, init=False)


@dataclass
class StatusTransition:
    """Connection status changed between polls."""

    model: str
    from_status: str
    to_status: str
    level: EventLevel = field(default=EventLevel.INFO, init=False)


@dataclass
class CounterReset:
    """Error counters dropped between polls — modem rebooted or stats cleared."""

    model: str
    prev_corrected: int
    cur_corrected: int
    prev_uncorrected: int
    cur_uncorrected: int
    level: EventLevel = field(default=EventLevel.INFO, init=False)


# ---------------------------------------------------------------------------
# Phase: restart / recovery
# ---------------------------------------------------------------------------


@dataclass
class RestartCommandSent:
    """Restart command dispatched and session cleared."""

    model: str
    elapsed_seconds: float
    level: EventLevel = field(default=EventLevel.INFO, init=False)


@dataclass
class RestartCommandFailed:
    """Restart command failed — auth or dispatch error."""

    model: str
    reason: str
    level: EventLevel = field(default=EventLevel.ERROR, init=False)


@dataclass
class RecoveryWindowOpened:
    """Recovery window started."""

    model: str
    reason: str
    window_seconds: float
    level: EventLevel = field(default=EventLevel.INFO, init=False)


@dataclass
class RecoveryWindowClosed:
    """Recovery window ended."""

    model: str
    elapsed_seconds: float
    last_docsis_status: str
    level: EventLevel = field(default=EventLevel.INFO, init=False)


@dataclass
class RecoveryObserverException:
    """Unhandled exception in a recovery observer callback."""

    model: str
    exc_type: str
    level: EventLevel = field(default=EventLevel.ERROR, init=False)


# ---------------------------------------------------------------------------
# Phase: actions (http exchange)
# ---------------------------------------------------------------------------


@dataclass
class ActionStarted:
    """Action dispatched. Level is caller-determined."""

    model: str
    transport: str  # "hnap" | "http" | "cbn"
    action_name: str
    level: EventLevel  # caller-determined


@dataclass
class ActionCompleted:
    """Response received on success path. Level is caller-determined."""

    model: str
    transport: str  # "hnap" | "http" | "cbn"
    action_name: str
    status_code: int | None
    result: str
    level: EventLevel  # caller-determined


@dataclass
class ActionConnectionLost:
    """Connection dropped during action — expected during modem restart. Level is caller-determined."""

    model: str
    transport: str  # "hnap" | "http" | "cbn"
    action_name: str
    level: EventLevel  # caller-determined


@dataclass
class ActionFailed:
    """Bad response format, unexpected result, or request error."""

    model: str
    transport: str  # "hnap" | "http" | "cbn"
    action_name: str
    reason: str
    level: EventLevel = field(default=EventLevel.WARNING, init=False)


@dataclass
class ActionPreFetchCompleted:
    """Pre-fetch returned data. Level is caller-determined.

    fallback_endpoint is non-None when keyword extraction failed and the
    action continues with the configured static fallback.
    """

    model: str
    transport: str  # "hnap" | "http" | "cbn"
    action_name: str
    key_count: int | None
    fallback_endpoint: str | None
    level: EventLevel  # caller-determined


@dataclass
class ActionPreFetchFailed:
    """Pre-fetch connection error or bad response.

    When fallback_endpoint is non-None, keyword extraction failed but the
    action continues with the static fallback. When None, the action will fail.
    """

    model: str
    transport: str  # "hnap" | "http" | "cbn"
    action_name: str
    reason: str
    fallback_endpoint: str | None
    level: EventLevel = field(default=EventLevel.WARNING, init=False)


# ---------------------------------------------------------------------------
# Phase: resource loading (http exchange)
# ---------------------------------------------------------------------------


@dataclass
class ResourceFetched:
    """Data page fetched during collection."""

    model: str
    path: str
    status_code: int
    size_bytes: int
    elapsed_ms: float
    level: EventLevel = field(default=EventLevel.DEBUG, init=False)


@dataclass
class ResourceDecodeError:
    """Response could not be decoded."""

    model: str
    path: str
    fmt: str
    reason: str
    level: EventLevel = field(default=EventLevel.WARNING, init=False)


# ---------------------------------------------------------------------------
# Union type — all orchestration events
# ---------------------------------------------------------------------------

type OrchestratorEvent = (
    ConnectivityFailureDetected
    | ConnectivityBackoffActive
    | ConnectivityBackoffCleared
    | ConnectivityBackoffReset
    | AuthSucceeded
    | AuthFailed
    | AuthLockoutDetected
    | AuthCircuitBreakerOpen
    | CircuitBreakerPollingBlocked
    | StaleSessionRecoveryDisabled
    | SessionReused
    | SessionCleared
    | LogoutExecuted
    | LogoutFailed
    | PostLoginFetchFailed
    | HnapSessionExpired
    | StubPageDetected
    | SessionRetryStarted
    | SessionRetrySucceeded
    | SessionRetryFailed
    | HealthStatusReport
    | HealthRecoveryDetected
    | HealthBackoffCleared
    | CollectionComplete
    | ParseError
    | ResourceLoadError
    | HttpStatusError
    | ConnectionFailedDuringLoad
    | HnapConnectionFailed
    | HnapLoadError
    | ZeroChannelsNoSystemInfo
    | SystemInfoFieldsChanged
    | StatusTransition
    | CounterReset
    | RestartCommandSent
    | RestartCommandFailed
    | RecoveryWindowOpened
    | RecoveryWindowClosed
    | RecoveryObserverException
    | ActionStarted
    | ActionCompleted
    | ActionConnectionLost
    | ActionFailed
    | ActionPreFetchCompleted
    | ActionPreFetchFailed
    | ResourceFetched
    | ResourceDecodeError
)
