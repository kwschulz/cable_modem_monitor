"""Tests for orchestration/policy.py event emission."""

from __future__ import annotations

from unittest.mock import MagicMock

from solentlabs.cable_modem_monitor_core.orchestration.events import (
    AuthCircuitBreakerOpen,
    AuthLockoutDetected,
    ConnectivityBackoffActive,
    ConnectivityBackoffCleared,
    ConnectivityFailureDetected,
    EventLevel,
    StaleSessionRecoveryDisabled,
)
from solentlabs.cable_modem_monitor_core.orchestration.models import ModemResult
from solentlabs.cable_modem_monitor_core.orchestration.policy import SignalPolicy
from solentlabs.cable_modem_monitor_core.orchestration.signals import CollectorSignal

from .event_capture import assert_event_emitted, capture_events


def _make_policy(
    *,
    model: str = "SB8200",
    threshold: int = 3,
    max_backoff: int = 6,
    stale_threshold: int = 2,
    single_session: bool = False,
) -> SignalPolicy:
    collector = MagicMock()
    # Set explicitly: a bare MagicMock attribute is truthy, which would
    # silently route AUTH_FAILED down the single-session threshold path.
    collector.is_single_session = single_session
    return SignalPolicy(
        collector,
        auth_failure_threshold=threshold,
        max_connectivity_backoff=max_backoff,
        stale_recovery_threshold=stale_threshold,
        model=model,
    )


def _connectivity_result() -> ModemResult:
    return ModemResult(success=False, signal=CollectorSignal.CONNECTIVITY)


def _auth_failed_result() -> ModemResult:
    return ModemResult(success=False, signal=CollectorSignal.AUTH_FAILED)


def _auth_lockout_result() -> ModemResult:
    return ModemResult(success=False, signal=CollectorSignal.AUTH_LOCKOUT)


# ---------------------------------------------------------------------------
# ConnectivityFailureDetected
# ---------------------------------------------------------------------------


def test_connectivity_failure_detected_emitted():
    policy = _make_policy(model="HUB5")
    with capture_events() as events:
        policy.apply(_connectivity_result())
    assert_event_emitted(events, ConnectivityFailureDetected, model="HUB5")
    event = next(e for e in events if isinstance(e, ConnectivityFailureDetected))
    assert event.streak == 1
    assert event.backoff_polls >= 1
    assert event.level == EventLevel.WARNING


# ---------------------------------------------------------------------------
# ConnectivityBackoffActive + ConnectivityBackoffCleared
# ---------------------------------------------------------------------------


def test_connectivity_backoff_active_emitted():
    policy = _make_policy(max_backoff=4)
    # streak=1→backoff=1, streak=2→backoff=2; need backoff≥2 to emit Active (not Cleared)
    policy.apply(_connectivity_result())  # streak=1, backoff=1
    policy.apply(_connectivity_result())  # streak=2, backoff=2
    with capture_events() as events:
        still_active = policy.check_connectivity_backoff()  # backoff 2→1, Active
    assert still_active is True
    assert_event_emitted(events, ConnectivityBackoffActive, model="SB8200")
    event = next(e for e in events if isinstance(e, ConnectivityBackoffActive))
    assert event.polls_remaining >= 0
    assert event.level == EventLevel.INFO


def test_connectivity_backoff_cleared_emitted():
    policy = _make_policy(max_backoff=6)
    policy.apply(_connectivity_result())  # streak=1, backoff=1
    with capture_events() as events:
        still_active = policy.check_connectivity_backoff()
    # backoff=1 → decrements to 0 → emits Cleared
    assert still_active is False
    assert_event_emitted(events, ConnectivityBackoffCleared, model="SB8200")
    event = next(e for e in events if isinstance(e, ConnectivityBackoffCleared))
    assert event.level == EventLevel.INFO


# ---------------------------------------------------------------------------
# AuthLockoutDetected
# ---------------------------------------------------------------------------


def test_auth_lockout_detected_emitted():
    policy = _make_policy(model="CM8200")
    with capture_events() as events:
        policy.apply(_auth_lockout_result())
    assert_event_emitted(events, AuthLockoutDetected, model="CM8200")
    event = next(e for e in events if isinstance(e, AuthLockoutDetected))
    assert event.streak == 1
    assert event.level == EventLevel.WARNING


# ---------------------------------------------------------------------------
# AuthCircuitBreakerOpen — immediate trip (AUTH_FAILED / AUTH_LOCKOUT)
# ---------------------------------------------------------------------------


def test_circuit_breaker_open_on_auth_failed():
    policy = _make_policy(model="SB8200")
    with capture_events() as events:
        policy.apply(_auth_failed_result())
    assert_event_emitted(events, AuthCircuitBreakerOpen, model="SB8200")
    event = next(e for e in events if isinstance(e, AuthCircuitBreakerOpen))
    assert event.streak == 1
    assert event.level == EventLevel.ERROR


def test_circuit_breaker_event_carries_auth_status_code():
    """A login 404 reaches the breaker event so the message can say endpoint-not-found."""
    policy = _make_policy(model="MB7621")
    result = ModemResult(success=False, signal=CollectorSignal.AUTH_FAILED, auth_status_code=404)
    with capture_events() as events:
        policy.apply(result)
    event = next(e for e in events if isinstance(e, AuthCircuitBreakerOpen))
    assert event.status_code == 404


def test_circuit_breaker_open_on_auth_lockout():
    policy = _make_policy()
    with capture_events() as events:
        policy.apply(_auth_lockout_result())
    assert_event_emitted(events, AuthCircuitBreakerOpen)
    event = next(e for e in events if isinstance(e, AuthCircuitBreakerOpen))
    assert event.level == EventLevel.ERROR


# ---------------------------------------------------------------------------
# AUTH_FAILED on single-session firmware, threshold trip (UC-87a)
# ---------------------------------------------------------------------------


def test_single_session_auth_failed_does_not_open_breaker():
    """A refused login while the slot is occupied must not stop polling."""
    policy = _make_policy(model="F3896LG-ZG", single_session=True)
    with capture_events() as events:
        policy.apply(_auth_failed_result())
    assert not [e for e in events if isinstance(e, AuthCircuitBreakerOpen)]


def test_single_session_auth_failed_trips_at_threshold():
    """The breaker still opens if the slot never frees."""
    policy = _make_policy(model="F3896LG-ZG", threshold=2, single_session=True)
    with capture_events() as events:
        policy.apply(_auth_failed_result())  # streak=1, no trip
        policy.apply(_auth_failed_result())  # streak=2, trips
    assert_event_emitted(events, AuthCircuitBreakerOpen, model="F3896LG-ZG")


def test_single_session_login_404_still_trips_immediately():
    """An absent login endpoint is not a session conflict; stop at once."""
    # Retrying a 404 would keep posting credentials at an unknown device,
    # and the trip must carry the status so the blocked-poll message says
    # endpoint-not-found rather than reconfigure-credentials.
    policy = _make_policy(model="F3896LG-ZG", single_session=True)
    result = ModemResult(success=False, signal=CollectorSignal.AUTH_FAILED, auth_status_code=404)
    with capture_events() as events:
        policy.apply(result)
    assert policy.circuit_open is True
    assert policy.circuit_trip_status_code == 404
    event = next(e for e in events if isinstance(e, AuthCircuitBreakerOpen))
    assert event.status_code == 404


# ---------------------------------------------------------------------------
# AuthCircuitBreakerOpen — threshold trip (LOAD_AUTH)
# ---------------------------------------------------------------------------


def test_circuit_breaker_open_at_threshold_on_load_auth():
    policy = _make_policy(threshold=2)
    load_auth = ModemResult(success=False, signal=CollectorSignal.LOAD_AUTH)
    with capture_events() as events:
        policy.apply(load_auth)  # streak=1, no trip
        policy.apply(load_auth)  # streak=2, trips
    assert_event_emitted(events, AuthCircuitBreakerOpen)
    event = next(e for e in events if isinstance(e, AuthCircuitBreakerOpen))
    assert event.streak == 2


# ---------------------------------------------------------------------------
# StaleSessionRecoveryDisabled
# ---------------------------------------------------------------------------


def test_stale_session_recovery_disabled_emitted():
    policy = _make_policy(stale_threshold=2)
    with capture_events() as events:
        policy.record_stale_session_recovery()  # streak=1, no trip
        policy.record_stale_session_recovery()  # streak=2, disables
    assert_event_emitted(events, StaleSessionRecoveryDisabled, model="SB8200")
    event = next(e for e in events if isinstance(e, StaleSessionRecoveryDisabled))
    assert event.streak == 2
    assert event.level == EventLevel.INFO
