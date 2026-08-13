"""Tests for the Recovery module.

Covers window entry/exit behavior, the 2-of-3 reboot-signal vote,
observer firing on transitions, and history tracking across
successive ``evaluate_snapshot`` calls.

Use case coverage:
- UC-40: Restart button opens a window via ``begin("restart_command")``.
- UC-43: Successful polls during a window don't short-circuit; the
  window ticks to completion.
- UC-49: Connectivity failure engages the window.
- UC-88: Reboot-signal check opens a window on 2-of-3 match.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from solentlabs.cable_modem_monitor_core.orchestration.models import ModemResult
from solentlabs.cable_modem_monitor_core.orchestration.recovery import Recovery
from solentlabs.cable_modem_monitor_core.orchestration.signals import (
    CollectorSignal,
)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _modem_data(
    *,
    total_corrected: int | None = None,
    total_uncorrected: int | None = None,
    system_uptime: int | str | None = None,
    docsis_status: str | None = None,
) -> dict[str, Any]:
    """Build a minimal modem_data dict with the fields recovery reads."""
    system_info: dict[str, Any] = {}
    if total_corrected is not None:
        system_info["total_corrected"] = total_corrected
    if total_uncorrected is not None:
        system_info["total_uncorrected"] = total_uncorrected
    if system_uptime is not None:
        system_info["system_uptime"] = system_uptime
    if docsis_status is not None:
        system_info["docsis_status"] = docsis_status
    return {
        "downstream": [],
        "upstream": [],
        "system_info": system_info,
    }


def _make_recovery(
    on_state_change: Any = None,
) -> Recovery:
    """Build a Recovery instance with a mocked config."""
    config = MagicMock()
    config.model = "T100"
    return Recovery(
        modem_config=config,
        on_state_change=on_state_change,
    )


def _failure(signal: CollectorSignal) -> ModemResult:
    """Build a failed ModemResult with the given signal."""
    return ModemResult(success=False, signal=signal, error="test")


# ------------------------------------------------------------------
# begin()
# ------------------------------------------------------------------


def test_begin_opens_window_and_fires_observer() -> None:
    observer = MagicMock()
    recovery = _make_recovery(on_state_change=observer)

    assert recovery.active is False

    recovery.begin("restart_command")

    assert recovery.active is True
    observer.assert_called_once()


def test_begin_re_entry_fires_observer_again() -> None:
    observer = MagicMock()
    recovery = _make_recovery(on_state_change=observer)

    recovery.begin("restart_command")
    recovery.begin("restart_command")

    assert recovery.active is True
    assert observer.call_count == 2


def test_begin_resets_started_clock_on_re_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr(
        "solentlabs.cable_modem_monitor_core.orchestration.recovery.time.monotonic",
        lambda: clock["now"],
    )
    recovery = _make_recovery()

    recovery.begin("restart_command")
    clock["now"] = 160.0
    recovery.begin("restart_command")

    # The internal started_at advanced to 160 — otherwise the window
    # would expire 60 seconds early relative to the second call.
    assert recovery._started_at == 160.0


# ------------------------------------------------------------------
# tick()
# ------------------------------------------------------------------


def test_tick_noop_when_inactive() -> None:
    observer = MagicMock()
    recovery = _make_recovery(on_state_change=observer)

    recovery.tick()

    observer.assert_not_called()


def test_tick_closes_window_after_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr(
        "solentlabs.cable_modem_monitor_core.orchestration.recovery.time.monotonic",
        lambda: clock["now"],
    )
    observer = MagicMock()
    recovery = _make_recovery(on_state_change=observer)

    recovery.begin("restart_command")
    assert observer.call_count == 1
    recovery.tick()  # still inside window at t=100
    clock["now"] = 100.0 + Recovery.WINDOW_SECONDS + 1
    recovery.tick()  # past deadline

    assert recovery.active is False
    assert observer.call_count == 2


def test_tick_does_not_fire_observer_before_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr(
        "solentlabs.cable_modem_monitor_core.orchestration.recovery.time.monotonic",
        lambda: clock["now"],
    )
    observer = MagicMock()
    recovery = _make_recovery(on_state_change=observer)

    recovery.begin("restart_command")
    observer.reset_mock()

    clock["now"] = 100.0 + Recovery.WINDOW_SECONDS - 1
    recovery.tick()

    assert recovery.active is True
    observer.assert_not_called()


# ------------------------------------------------------------------
# evaluate_failure()
# ------------------------------------------------------------------


def test_evaluate_failure_enters_window_on_connectivity() -> None:
    observer = MagicMock()
    recovery = _make_recovery(on_state_change=observer)

    recovery.evaluate_failure(_failure(CollectorSignal.CONNECTIVITY))

    assert recovery.active is True
    observer.assert_called_once()


def test_evaluate_failure_is_noop_when_already_active() -> None:
    observer = MagicMock()
    recovery = _make_recovery(on_state_change=observer)

    recovery.begin("restart_command")
    observer.reset_mock()

    recovery.evaluate_failure(_failure(CollectorSignal.CONNECTIVITY))

    observer.assert_not_called()


@pytest.mark.parametrize(
    "signal",
    [
        CollectorSignal.AUTH_FAILED,
        CollectorSignal.AUTH_LOCKOUT,
        CollectorSignal.LOAD_ERROR,
        CollectorSignal.LOAD_AUTH,
        CollectorSignal.PARSE_ERROR,
    ],
)
def test_evaluate_failure_noop_for_non_connectivity(
    signal: CollectorSignal,
) -> None:
    recovery = _make_recovery()

    recovery.evaluate_failure(_failure(signal))

    assert recovery.active is False


# ------------------------------------------------------------------
# evaluate_snapshot() — reboot-signal vote
# ------------------------------------------------------------------


def test_snapshot_first_call_updates_history_without_firing() -> None:
    observer = MagicMock()
    recovery = _make_recovery(on_state_change=observer)

    recovery.evaluate_snapshot(
        _modem_data(
            total_corrected=100,
            total_uncorrected=5,
            system_uptime=1000,
            docsis_status="Operational",
        )
    )

    # No previous baseline means no signals fire on the first call.
    assert recovery.active is False
    observer.assert_not_called()


def test_single_signal_does_not_trigger_window() -> None:
    recovery = _make_recovery()
    # Establish baseline.
    recovery.evaluate_snapshot(
        _modem_data(
            total_corrected=100,
            total_uncorrected=5,
            system_uptime=1000,
            docsis_status="Operational",
        )
    )
    # Only the counter-reset signal fires.
    recovery.evaluate_snapshot(
        _modem_data(
            total_corrected=50,  # dropped
            total_uncorrected=5,
            system_uptime=1500,
            docsis_status="Operational",
        )
    )

    assert recovery.active is False


def test_two_of_three_signals_open_window() -> None:
    observer = MagicMock()
    recovery = _make_recovery(on_state_change=observer)

    recovery.evaluate_snapshot(
        _modem_data(
            total_corrected=100,
            total_uncorrected=5,
            system_uptime=1000,
            docsis_status="Operational",
        )
    )
    recovery.evaluate_snapshot(
        _modem_data(
            total_corrected=0,  # counter reset
            total_uncorrected=0,  # counter reset
            system_uptime=10,  # uptime drop
            docsis_status="Operational",
        )
    )

    assert recovery.active is True
    observer.assert_called_once()


def test_transitional_docsis_counts_as_a_signal() -> None:
    recovery = _make_recovery()

    recovery.evaluate_snapshot(
        _modem_data(
            total_corrected=100,
            total_uncorrected=5,
            system_uptime=1000,
            docsis_status="Operational",
        )
    )
    recovery.evaluate_snapshot(
        _modem_data(
            total_corrected=100,  # no change
            total_uncorrected=5,  # no change
            system_uptime=10,  # uptime drop
            docsis_status="not_locked",  # transitional — Operational→not_locked edge
        )
    )

    assert recovery.active is True


def test_chronic_partial_lock_plus_counter_reset_does_not_trigger() -> None:
    """Edge-triggered transitional_docsis suppresses the classic false positive.

    A modem stuck in partial_lock (bad ISP signal, not a reboot)
    combined with a user clearing counters via the web UI used to
    trip the 2-of-3 vote under level-triggered semantics. With edge
    triggering, transitional_docsis only fires once (on first
    observation) and then stays quiet — so a later counter-clear
    sees just 1 signal and the vote correctly doesn't open a window.
    """
    recovery = _make_recovery()

    # Poll 1: modem is already stuck in partial_lock. On first
    # observation the edge fires (prev = "unknown" is non-transitional),
    # but no counter/uptime baseline exists yet so the vote is 1-of-3.
    recovery.evaluate_snapshot(
        _modem_data(
            total_corrected=500,
            total_uncorrected=20,
            system_uptime=5000,
            docsis_status="partial_lock",
        )
    )
    assert recovery.active is False

    # Poll 2: still partial_lock (chronic). No new edge — signal
    # silent. Counters/uptime unchanged.
    recovery.evaluate_snapshot(
        _modem_data(
            total_corrected=500,
            total_uncorrected=20,
            system_uptime=5060,
            docsis_status="partial_lock",
        )
    )
    assert recovery.active is False

    # Poll 3: user clears error counters via the modem's web UI.
    # counter_reset fires; transitional_docsis does NOT (still
    # partial_lock, no new edge); uptime_drop does NOT. 1-of-3 —
    # vote correctly doesn't trigger.
    recovery.evaluate_snapshot(
        _modem_data(
            total_corrected=0,
            total_uncorrected=0,
            system_uptime=5120,
            docsis_status="partial_lock",
        )
    )

    assert recovery.active is False


def test_repeated_partial_lock_does_not_re_fire_transitional() -> None:
    """Transitional_docsis alone cannot drive a window on a stuck modem.

    Even combined with uptime unchanged and counters unchanged, a
    modem chronically in partial_lock must never trip the window on
    its own after the initial edge.
    """
    recovery = _make_recovery()

    # Establish baseline in partial_lock — first poll sees the edge.
    recovery.evaluate_snapshot(
        _modem_data(
            total_corrected=100,
            total_uncorrected=5,
            system_uptime=1000,
            docsis_status="partial_lock",
        )
    )

    # Ten more polls, all partial_lock, stable counters and uptime.
    # The edge has already fired and won't fire again; other signals
    # are silent; the vote stays at zero. Window never opens.
    for i in range(10):
        recovery.evaluate_snapshot(
            _modem_data(
                total_corrected=100,
                total_uncorrected=5,
                system_uptime=1000 + i * 60,
                docsis_status="partial_lock",
            )
        )
        assert recovery.active is False, f"window opened on chronic-lock poll {i}"


def test_snapshot_history_updates_even_while_active() -> None:
    recovery = _make_recovery()
    recovery.begin("restart_command")

    recovery.evaluate_snapshot(
        _modem_data(
            total_corrected=500,
            total_uncorrected=10,
            system_uptime=200,
            docsis_status="Operational",
        )
    )

    assert recovery._prev_counters == (500, 10)
    assert recovery._prev_uptime == 200


def test_snapshot_accepts_string_uptime() -> None:
    recovery = _make_recovery()

    recovery.evaluate_snapshot(
        _modem_data(
            total_corrected=1,
            total_uncorrected=1,
            system_uptime="1000",
            docsis_status="Operational",
        )
    )
    recovery.evaluate_snapshot(
        _modem_data(
            total_corrected=0,  # counter reset
            total_uncorrected=0,  # counter reset
            system_uptime="500",  # uptime drop (numeric)
            docsis_status="Operational",
        )
    )

    assert recovery.active is True


def test_snapshot_ignores_non_numeric_uptime() -> None:
    recovery = _make_recovery()

    recovery.evaluate_snapshot(
        _modem_data(
            total_corrected=1,
            total_uncorrected=1,
            system_uptime="17d 0h 51m 30s",
            docsis_status="Operational",
        )
    )
    # With uptime non-numeric, only counter + docsis signals count.
    recovery.evaluate_snapshot(
        _modem_data(
            total_corrected=0,  # counter reset
            total_uncorrected=0,  # counter reset
            system_uptime="0d 0h 1m 0s",  # non-numeric — no uptime signal
            docsis_status="Operational",  # not transitional
        )
    )

    # Only one signal (counter_reset) — not enough.
    assert recovery.active is False


def test_snapshot_noop_when_active() -> None:
    observer = MagicMock()
    recovery = _make_recovery(on_state_change=observer)

    recovery.begin("restart_command")
    observer.reset_mock()

    # Establish baseline so the vote could otherwise trigger.
    recovery.evaluate_snapshot(
        _modem_data(
            total_corrected=100,
            total_uncorrected=5,
            system_uptime=1000,
            docsis_status="Operational",
        )
    )
    recovery.evaluate_snapshot(
        _modem_data(
            total_corrected=0,
            total_uncorrected=0,
            system_uptime=10,
            docsis_status="not_locked",
        )
    )

    # Window stays in its existing state; evaluate_snapshot doesn't
    # fire the observer while already active.
    assert recovery.active is True
    observer.assert_not_called()


# ------------------------------------------------------------------
# Observer safety
# ------------------------------------------------------------------


def test_observer_exceptions_do_not_propagate() -> None:
    def boom() -> None:
        raise RuntimeError("listener exploded")

    recovery = _make_recovery(on_state_change=boom)

    # Must not raise.
    recovery.begin("restart_command")

    assert recovery.active is True
