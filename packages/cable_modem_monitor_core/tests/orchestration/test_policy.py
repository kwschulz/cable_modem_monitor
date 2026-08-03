"""Tests for SignalPolicy edge cases.

Covers PARSE_ERROR signal mapping and the defensive unknown-signal
fallback. These complement the policy tests in test_orchestrator.py
which cover auth failure streaks, backoff, and circuit breaker.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from solentlabs.cable_modem_monitor_core.orchestration.models import ModemResult
from solentlabs.cable_modem_monitor_core.orchestration.policy import SignalPolicy
from solentlabs.cable_modem_monitor_core.orchestration.signals import (
    CollectorSignal,
    ConnectionStatus,
)


def _collector(*, single_session: bool = False) -> MagicMock:
    """Build a mock collector with single-session state set explicitly."""
    # A bare MagicMock attribute is truthy, which would route AUTH_FAILED
    # down the single-session threshold path without anyone asking for it.
    collector = MagicMock()
    collector.is_single_session = single_session
    return collector


@pytest.fixture()
def policy() -> SignalPolicy:
    """Create a SignalPolicy with a mock collector."""
    return SignalPolicy(_collector())


# ┌──────────────────────┬──────────────────────┬──────────────────────┐
# │ signal               │ expected_status       │ description          │
# ├──────────────────────┼──────────────────────┼──────────────────────┤
# │ PARSE_ERROR          │ PARSER_ISSUE         │ parser failure       │
# │ CONNECTIVITY         │ UNREACHABLE          │ connection lost      │
# │ LOAD_ERROR           │ UNREACHABLE          │ server error         │
# └──────────────────────┴──────────────────────┴──────────────────────┘
#
# fmt: off
SIGNAL_MAPPING_CASES = [
    (CollectorSignal.PARSE_ERROR,  ConnectionStatus.PARSER_ISSUE, "parser failure → parser_issue"),
    (CollectorSignal.CONNECTIVITY, ConnectionStatus.UNREACHABLE,  "connection lost → unreachable"),
    (CollectorSignal.LOAD_ERROR,   ConnectionStatus.UNREACHABLE,  "server error → unreachable"),
]
# fmt: on


@pytest.mark.parametrize(
    "signal,expected_status,desc",
    SIGNAL_MAPPING_CASES,
    ids=[c[2] for c in SIGNAL_MAPPING_CASES],
)
def test_signal_to_status_mapping(
    policy: SignalPolicy,
    signal: CollectorSignal,
    expected_status: ConnectionStatus,
    desc: str,
) -> None:
    """Policy maps infrastructure signals to correct connection status."""
    result = ModemResult(success=False, signal=signal, error="test error")
    assert policy.apply(result) == expected_status


class TestParseErrorDoesNotAffectStreak:
    """PARSE_ERROR is not an auth failure — streak unchanged."""

    def test_streak_unchanged_on_parse_error(self, policy: SignalPolicy) -> None:
        """PARSE_ERROR does not increment auth failure streak."""
        result = ModemResult(
            success=False,
            signal=CollectorSignal.PARSE_ERROR,
            error="bad html",
        )
        policy.apply(result)
        assert policy.auth_failure_streak == 0

    def test_circuit_breaker_default(self, policy: SignalPolicy) -> None:
        """Circuit breaker starts closed."""
        assert policy.circuit_open is False


class TestLoadIntegrityTreatedLikeLoadAuth:
    """LOAD_INTEGRITY (UC-19a) gets the same recovery semantics as LOAD_AUTH."""

    def test_load_integrity_returns_auth_failed(self, policy: SignalPolicy) -> None:
        """LOAD_INTEGRITY surfaces as ConnectionStatus.AUTH_FAILED."""
        result = ModemResult(
            success=False,
            signal=CollectorSignal.LOAD_INTEGRITY,
            error="0 of 4 expected anchors on /status.html — stub response",
        )
        assert policy.apply(result) == ConnectionStatus.AUTH_FAILED

    def test_load_integrity_clears_session(self) -> None:
        """LOAD_INTEGRITY triggers session clear (same as LOAD_AUTH)."""
        collector = _collector()
        policy = SignalPolicy(collector)
        result = ModemResult(success=False, signal=CollectorSignal.LOAD_INTEGRITY)
        policy.apply(result)
        collector.clear_session.assert_called_once()

    def test_load_integrity_increments_auth_streak(self, policy: SignalPolicy) -> None:
        """LOAD_INTEGRITY counts toward the auth failure streak."""
        result = ModemResult(success=False, signal=CollectorSignal.LOAD_INTEGRITY)
        assert policy.auth_failure_streak == 0
        policy.apply(result)
        assert policy.auth_failure_streak == 1
        policy.apply(result)
        assert policy.auth_failure_streak == 2

    def test_load_integrity_trips_circuit_breaker_at_threshold(self, policy: SignalPolicy) -> None:
        """Sustained LOAD_INTEGRITY trips the circuit breaker like LOAD_AUTH does."""
        result = ModemResult(success=False, signal=CollectorSignal.LOAD_INTEGRITY)
        for _ in range(policy._threshold):
            policy.apply(result)
        assert policy.circuit_open is True

    def test_intervening_ok_resets_streak(self, policy: SignalPolicy) -> None:
        """A successful poll between LOAD_INTEGRITY events resets the streak."""
        load_integrity = ModemResult(success=False, signal=CollectorSignal.LOAD_INTEGRITY)
        policy.apply(load_integrity)
        assert policy.auth_failure_streak == 1
        policy.clear_streak()
        assert policy.auth_failure_streak == 0


# Single-session firmware refuses a login while another session holds the
# slot, so a user opening the modem's own web UI produces a refused login
# on perfectly good credentials. UC-87's immediate trip assumes no
# transient condition can make the same password work later; that premise
# is false here. See ORCHESTRATION_USE_CASES UC-87a.
class TestAuthFailedOnSingleSessionFirmware:
    """AUTH_FAILED takes the threshold path on single-session firmware."""

    @staticmethod
    def _auth_failed() -> ModemResult:
        return ModemResult(
            success=False,
            signal=CollectorSignal.AUTH_FAILED,
            error="login refused",
            auth_status_code=401,
        )

    def test_multi_session_still_trips_immediately(self) -> None:
        """UC-87 is unchanged for firmware that allows concurrent sessions."""
        policy = SignalPolicy(_collector(single_session=False))
        policy.apply(self._auth_failed())
        assert policy.circuit_open is True

    def test_single_session_does_not_trip_on_first_failure(self) -> None:
        """One refused login must not strand polling behind a reauth prompt."""
        policy = SignalPolicy(_collector(single_session=True))
        policy.apply(self._auth_failed())
        assert policy.circuit_open is False
        assert policy.auth_failure_streak == 1

    def test_single_session_trips_at_threshold(self) -> None:
        """A password genuinely changed on the modem still reaches reauth."""
        policy = SignalPolicy(_collector(single_session=True))
        for _ in range(policy._threshold):
            policy.apply(self._auth_failed())
        assert policy.circuit_open is True

    def test_single_session_status_is_still_auth_failed(self) -> None:
        """Status reporting is unchanged; only the trip rule differs."""
        policy = SignalPolicy(_collector(single_session=True))
        assert policy.apply(self._auth_failed()) == ConnectionStatus.AUTH_FAILED

    def test_released_slot_resets_the_streak(self) -> None:
        """The browser logging out lets the next poll succeed and clears the streak."""
        policy = SignalPolicy(_collector(single_session=True))
        policy.apply(self._auth_failed())
        policy.apply(self._auth_failed())
        assert policy.auth_failure_streak == 2
        policy.clear_streak()
        assert policy.auth_failure_streak == 0
        assert policy.circuit_open is False

    def test_lockout_still_trips_immediately_on_single_session(self) -> None:
        """Firmware anti-brute-force is a real lockout; threshold must not apply."""
        policy = SignalPolicy(_collector(single_session=True))
        policy.apply(ModemResult(success=False, signal=CollectorSignal.AUTH_LOCKOUT))
        assert policy.circuit_open is True

    def test_login_404_still_trips_immediately_on_single_session(self) -> None:
        """No session slot frees an absent login endpoint."""
        policy = SignalPolicy(_collector(single_session=True))
        result = ModemResult(
            success=False,
            signal=CollectorSignal.AUTH_FAILED,
            auth_status_code=404,
        )
        policy.apply(result)
        assert policy.circuit_open is True
        assert policy.circuit_trip_status_code == 404

    def test_threshold_trip_records_the_refusal_status(self) -> None:
        """circuit_trip_status_code must survive the threshold path."""
        policy = SignalPolicy(_collector(single_session=True))
        for _ in range(policy._threshold):
            policy.apply(self._auth_failed())
        assert policy.circuit_trip_status_code == 401
