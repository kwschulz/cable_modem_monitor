"""Tests for orchestration/recovery.py event emission."""

from __future__ import annotations

from unittest.mock import MagicMock

from solentlabs.cable_modem_monitor_core.orchestration.events import (
    RecoveryObserverException,
    RecoveryWindowClosed,
    RecoveryWindowOpened,
)
from solentlabs.cable_modem_monitor_core.orchestration.recovery import Recovery

from .event_capture import assert_event_emitted, capture_events


def _make_recovery(on_state_change=None) -> Recovery:
    modem_config = MagicMock()
    modem_config.model = "MB7621"
    return Recovery(modem_config, on_state_change=on_state_change)


# ---------------------------------------------------------------------------
# begin() — RecoveryWindowOpened
# ---------------------------------------------------------------------------


def test_begin_emits_recovery_window_opened():
    recovery = _make_recovery()
    with capture_events() as events:
        recovery.begin("restart_command")
    assert_event_emitted(events, RecoveryWindowOpened, model="MB7621", reason="restart_command")


def test_begin_window_seconds_matches_class_constant():
    recovery = _make_recovery()
    with capture_events() as events:
        recovery.begin("restart_command")
    event = next(e for e in events if isinstance(e, RecoveryWindowOpened))
    assert event.window_seconds == Recovery.WINDOW_SECONDS


# ---------------------------------------------------------------------------
# tick() — RecoveryWindowClosed
# ---------------------------------------------------------------------------


def test_tick_close_emits_recovery_window_closed(monkeypatch):
    recovery = _make_recovery()
    recovery.begin("restart_command")

    # Force the window to appear expired.
    monkeypatch.setattr(recovery, "_started_at", 0.0)
    monkeypatch.setattr(recovery, "WINDOW_SECONDS", 0)

    with capture_events() as events:
        recovery.tick()

    assert_event_emitted(events, RecoveryWindowClosed, model="MB7621")


def test_tick_no_event_when_window_open(monkeypatch):
    recovery = _make_recovery()
    recovery.begin("restart_command")

    # Window hasn't expired.
    with capture_events() as events:
        recovery.tick()

    assert not any(isinstance(e, RecoveryWindowClosed) for e in events)


# ---------------------------------------------------------------------------
# _enter_from_internal() — RecoveryWindowOpened
# ---------------------------------------------------------------------------


def test_evaluate_failure_emits_recovery_window_opened():
    from solentlabs.cable_modem_monitor_core.orchestration.models import ModemResult
    from solentlabs.cable_modem_monitor_core.orchestration.signals import CollectorSignal

    recovery = _make_recovery()
    result = ModemResult(success=False, signal=CollectorSignal.CONNECTIVITY)

    with capture_events() as events:
        recovery.evaluate_failure(result)

    assert_event_emitted(events, RecoveryWindowOpened, model="MB7621")
    event = next(e for e in events if isinstance(e, RecoveryWindowOpened))
    assert event.reason == "connectivity_outage"


# ---------------------------------------------------------------------------
# _fire_observer() — RecoveryObserverException
# ---------------------------------------------------------------------------


def test_observer_exception_emits_recovery_observer_exception():
    def _bad_observer():
        raise ValueError("observer boom")

    recovery = _make_recovery(on_state_change=_bad_observer)

    with capture_events() as events:
        recovery.begin("restart_command")

    assert_event_emitted(events, RecoveryObserverException, model="MB7621")
    event = next(e for e in events if isinstance(e, RecoveryObserverException))
    assert event.exc_type == "ValueError"
