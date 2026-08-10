"""Tests for orchestration/events.py and orchestration/logging.py.

Covers:
- EventLevel values match stdlib logging constants
- Fixed-level events carry the correct level
- HealthStatusReport computes its level from status and changed
- Caller-determined events accept the level as an init parameter
- log_event() calls logger.log() with the event's level and a non-empty string
- capture_events() collects event objects without calling the logger
- assert_event_emitted() passes and fails correctly
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from solentlabs.cable_modem_monitor_core.orchestration.events import (
    AuthCircuitBreakerOpen,
    AuthFailed,
    AuthSucceeded,
    CircuitBreakerPollingBlocked,
    CollectionComplete,
    ConnectivityFailureDetected,
    EventLevel,
    HealthRecoveryDetected,
    HealthStatusReport,
    RecoveryWindowOpened,
    ResourceFetched,
    ZeroChannelsNoSystemInfo,
)
from solentlabs.cable_modem_monitor_core.orchestration.logging import log_event
from solentlabs.cable_modem_monitor_core.orchestration.signals import CollectorSignal

from .event_capture import assert_event_emitted, capture_events

# ---------------------------------------------------------------------------
# EventLevel
# ---------------------------------------------------------------------------


def test_event_level_values_match_stdlib():
    assert EventLevel.DEBUG == logging.DEBUG
    assert EventLevel.INFO == logging.INFO
    assert EventLevel.WARNING == logging.WARNING
    assert EventLevel.ERROR == logging.ERROR


# ---------------------------------------------------------------------------
# Fixed-level events
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "expected_level"),
    [
        (ConnectivityFailureDetected(model="SB8200", streak=1, backoff_polls=2), EventLevel.WARNING),
        (
            AuthFailed(
                model="SB8200",
                strategy="form",
                error="401",
                method=None,
                url=None,
                status_code=None,
                content_type=None,
                response_body=None,
            ),
            EventLevel.WARNING,
        ),
        (AuthCircuitBreakerOpen(model="SB8200", streak=5, signal=CollectorSignal.AUTH_FAILED), EventLevel.ERROR),
        (ZeroChannelsNoSystemInfo(model="SB8200"), EventLevel.WARNING),
        (
            ResourceFetched(model="SB8200", path="/status.html", status_code=200, size_bytes=1024, elapsed_ms=42.0),
            EventLevel.DEBUG,
        ),
        (HealthRecoveryDetected(model="SB8200", previous_status="degraded"), EventLevel.INFO),
        (RecoveryWindowOpened(model="SB8200", reason="restart_command", window_seconds=300.0), EventLevel.INFO),
    ],
)
def test_fixed_level_events(event, expected_level):
    assert event.level == expected_level


# ---------------------------------------------------------------------------
# HealthStatusReport — internally-computed level
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "changed", "expected_level"),
    [
        ("degraded", True, EventLevel.WARNING),
        ("unresponsive", True, EventLevel.WARNING),
        ("healthy", True, EventLevel.INFO),
        ("degraded", False, EventLevel.DEBUG),
        ("unresponsive", False, EventLevel.DEBUG),
        ("healthy", False, EventLevel.DEBUG),
    ],
)
def test_health_status_report_level(status, changed, expected_level):
    event = HealthStatusReport(model="SB8200", status=status, changed=changed, detail="")
    assert event.level == expected_level


# ---------------------------------------------------------------------------
# Caller-determined events
# ---------------------------------------------------------------------------


def test_caller_determined_level_accepted():
    event = AuthSucceeded(
        model="SB8200",
        strategy="form",
        status_code=200,
        response_url="/status.html",
        level=EventLevel.INFO,
    )
    assert event.level == EventLevel.INFO


@pytest.mark.parametrize("level", [EventLevel.INFO, EventLevel.DEBUG])
def test_collection_complete_accepts_either_level(level):
    """INFO for the first collection of a collector's lifetime, DEBUG for every one after."""
    event = CollectionComplete(model="SB8200", ds_count=32, us_count=8, elapsed_ms=120.0, level=level)
    assert event.level == level


# ---------------------------------------------------------------------------
# log_event() — adapter
# ---------------------------------------------------------------------------


def test_log_event_calls_logger_log():
    logger = MagicMock(spec=logging.Logger)
    event = ZeroChannelsNoSystemInfo(model="SB8200")
    log_event(logger, event)
    logger.log.assert_called_once()
    level_arg, msg_arg = logger.log.call_args.args
    assert level_arg == EventLevel.WARNING
    assert isinstance(msg_arg, str)
    assert msg_arg  # non-empty


def test_log_event_level_matches_event():
    logger = MagicMock(spec=logging.Logger)
    event = AuthCircuitBreakerOpen(model="SB8200", streak=5, signal=CollectorSignal.AUTH_FAILED)
    log_event(logger, event)
    level_arg, _ = logger.log.call_args.args
    assert level_arg == EventLevel.ERROR


def test_circuit_breaker_message_default():
    """Without a status code the breaker message points at credentials."""
    logger = MagicMock(spec=logging.Logger)
    log_event(logger, AuthCircuitBreakerOpen(model="SB8200", streak=1, signal=CollectorSignal.AUTH_FAILED))
    _, msg = logger.log.call_args.args
    assert "Reconfigure credentials" in msg


def test_circuit_breaker_message_endpoint_not_found():
    """HTTP 404 on login is endpoint absence, not credential rejection — wrong device or modem unavailable."""
    logger = MagicMock(spec=logging.Logger)
    log_event(
        logger,
        AuthCircuitBreakerOpen(model="SB8200", streak=1, signal=CollectorSignal.AUTH_FAILED, status_code=404),
    )
    _, msg = logger.log.call_args.args
    assert "login endpoint not found" in msg
    assert "Reconfigure credentials" not in msg


def test_polling_blocked_message_default():
    """Steady-state blocked message points at credentials when the trip was credential rejection."""
    logger = MagicMock(spec=logging.Logger)
    log_event(logger, CircuitBreakerPollingBlocked(model="SB8200", signal=CollectorSignal.AUTH_FAILED))
    _, msg = logger.log.call_args.args
    assert "Reconfigure credentials" in msg


def test_polling_blocked_message_endpoint_not_found():
    """Steady-state blocked message preserves the 404 trip reason — credentials are not the fix."""
    logger = MagicMock(spec=logging.Logger)
    log_event(
        logger,
        CircuitBreakerPollingBlocked(model="SB8200", signal=CollectorSignal.AUTH_FAILED, status_code=404),
    )
    _, msg = logger.log.call_args.args
    assert "login endpoint not found" in msg
    assert "Reconfigure credentials" not in msg


# Both events take their remedy from auth_stop_advice, so a new trip
# cause cannot be right on one surface and wrong on the other. This is
# the pair that drifted: the 404 wording was added to one of them first
# and had to be backfilled into the other (423bf9de, then 0160be2a).
@pytest.mark.parametrize(
    "event",
    [
        AuthCircuitBreakerOpen(model="SB8200", streak=1, signal=CollectorSignal.AUTH_LOCKOUT),
        CircuitBreakerPollingBlocked(model="SB8200", signal=CollectorSignal.AUTH_LOCKOUT),
    ],
    ids=["breaker opened", "poll blocked"],
)
def test_lockout_never_advises_reconfiguring_credentials(event):
    """A lockout clears by waiting and reloading, never by a new password."""
    logger = MagicMock(spec=logging.Logger)
    log_event(logger, event)
    _, msg = logger.log.call_args.args
    assert "Wait for the modem to clear the lockout" in msg
    assert "credentials" not in msg.lower()


@pytest.mark.parametrize(
    ("response_url", "expected_tail"),
    [
        ("/status.html", ", landed: /status.html"),
        ("/login.html", ", landed: /login.html"),
        ("", ""),  # strategies that advertise no reuse page say nothing
    ],
)
def test_auth_succeeded_message_reports_landing_path(response_url, expected_tail):
    """A form login that lands back on the login page is the only visible sign it was refused."""
    logger = MagicMock(spec=logging.Logger)
    log_event(
        logger,
        AuthSucceeded(
            model="SB8200",
            strategy="form",
            status_code=200,
            response_url=response_url,
            level=EventLevel.INFO,
        ),
    )
    _, msg = logger.log.call_args.args
    assert msg == f"Auth succeeded [SB8200] — strategy: form, status=200{expected_tail}"


def test_log_event_message_contains_model():
    logger = MagicMock(spec=logging.Logger)
    event = ZeroChannelsNoSystemInfo(model="HUB5")
    log_event(logger, event)
    _, msg_arg = logger.log.call_args.args
    assert "HUB5" in msg_arg


def test_auth_failed_connection_error_format():
    """Connection-error path (method=None) produces a single-line message."""
    logger = MagicMock(spec=logging.Logger)
    event = AuthFailed(
        model="SB8200",
        strategy="form",
        error="Connection refused",
        method=None,
        url=None,
        status_code=None,
        content_type=None,
        response_body=None,
    )
    log_event(logger, event)
    _, msg = logger.log.call_args.args
    assert "\n" not in msg
    assert "Connection refused" in msg


def test_auth_failed_response_format():
    """Response path (method set) produces a multi-line message with wire detail."""
    logger = MagicMock(spec=logging.Logger)
    event = AuthFailed(
        model="SB8200",
        strategy="form",
        error="401",
        method="POST",
        url="http://192.168.100.1/login",
        status_code=401,
        content_type="text/html",
        response_body="<html>Unauthorized</html>",
    )
    log_event(logger, event)
    _, msg = logger.log.call_args.args
    assert "POST" in msg
    assert "401" in msg
    assert "text/html" in msg


def test_auth_failed_response_format_states_why_it_failed():
    """The reason is the diagnosis; the wire detail is only the evidence for it.

    A criterion mismatch names both sides. Dropping ``error`` left the
    landing path in the message as a bare request URL, with nothing
    saying it was the mismatch or what was expected (#189).
    """
    logger = MagicMock(spec=logging.Logger)
    event = AuthFailed(
        model="SB8200",
        strategy="form",
        error="Login redirect mismatch: expected path containing '/index.htm', got '/ErrorMsg.htm'",
        method="POST",
        url="http://192.168.100.1/goform/Login",
        status_code=200,
        content_type="text/html",
        response_body="<html>Error</html>",
    )
    log_event(logger, event)
    _, msg = logger.log.call_args.args
    assert "expected path containing '/index.htm'" in msg
    assert "got '/ErrorMsg.htm'" in msg


# ---------------------------------------------------------------------------
# capture_events() + assert_event_emitted()
# ---------------------------------------------------------------------------


def test_capture_events_collects_events():
    def _emit():
        logger = MagicMock(spec=logging.Logger)
        log_event(logger, ZeroChannelsNoSystemInfo(model="SB8200"))

    with capture_events() as events:
        _emit()

    assert len(events) == 1
    assert isinstance(events[0], ZeroChannelsNoSystemInfo)


def test_capture_events_does_not_call_logger():
    logger = MagicMock(spec=logging.Logger)
    with capture_events():
        log_event(logger, ZeroChannelsNoSystemInfo(model="SB8200"))
    logger.log.assert_not_called()


def test_assert_event_emitted_passes():
    with capture_events() as events:
        log_event(MagicMock(), ZeroChannelsNoSystemInfo(model="SB8200"))
    assert_event_emitted(events, ZeroChannelsNoSystemInfo, model="SB8200")


def test_assert_event_emitted_fails_wrong_type():
    with capture_events() as events:
        log_event(MagicMock(), ZeroChannelsNoSystemInfo(model="SB8200"))
    with pytest.raises(AssertionError, match="AuthFailed"):
        assert_event_emitted(events, AuthFailed)


def test_assert_event_emitted_fails_wrong_field():
    with capture_events() as events:
        log_event(MagicMock(), ZeroChannelsNoSystemInfo(model="SB8200"))
    with pytest.raises(AssertionError):
        assert_event_emitted(events, ZeroChannelsNoSystemInfo, model="HUB5")


def test_assert_event_emitted_no_fields_passes():
    with capture_events() as events:
        log_event(MagicMock(), ZeroChannelsNoSystemInfo(model="SB8200"))
    assert_event_emitted(events, ZeroChannelsNoSystemInfo)
