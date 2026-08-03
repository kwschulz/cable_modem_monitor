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


def _collector() -> MagicMock:
    """Build a mock ModemDataCollector."""
    return MagicMock()


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


# UC-87a: a login answering 5xx means the modem declined to serve the
# request, not that it judged the credential. It must never reach a
# credential prompt at any repetition count, because some causes (ISP
# customer care holding the session) are outside the user's control.
class TestAuthUnavailable:
    """AUTH_UNAVAILABLE is a wait, not a verdict."""

    @staticmethod
    def _unavailable() -> ModemResult:
        return ModemResult(
            success=False,
            signal=CollectorSignal.AUTH_UNAVAILABLE,
            error="Login returned HTTP 503",
            auth_status_code=503,
        )

    def test_reports_unreachable(self) -> None:
        """The modem answered, but declined; that is not an auth failure."""
        policy = SignalPolicy(_collector())
        assert policy.apply(self._unavailable()) == ConnectionStatus.UNREACHABLE

    def test_does_not_touch_the_auth_streak(self) -> None:
        """A busy modem must not accumulate toward a credential verdict."""
        policy = SignalPolicy(_collector())
        policy.apply(self._unavailable())
        assert policy.auth_failure_streak == 0

    def test_never_trips_the_breaker(self) -> None:
        """Not at threshold, not at ten times threshold."""
        policy = SignalPolicy(_collector())
        for _ in range(policy._threshold * 10):
            policy.apply(self._unavailable())
        assert policy.circuit_open is False
        assert policy.auth_failure_streak == 0

    def test_does_not_clear_the_session(self) -> None:
        """Nothing is wrong with the session; only the modem is busy."""
        collector = _collector()
        policy = SignalPolicy(collector)
        policy.apply(self._unavailable())
        collector.clear_session.assert_not_called()


class TestAuthFailedTripsImmediately:
    """UC-87 and UC-87b: a credential verdict or an absent endpoint stops at once."""

    @staticmethod
    def _auth_failed(status: int) -> ModemResult:
        return ModemResult(
            success=False,
            signal=CollectorSignal.AUTH_FAILED,
            error=f"Login returned HTTP {status}",
            auth_status_code=status,
        )

    def test_credentials_rejected_trips_on_first_failure(self) -> None:
        """UC-87 is unchanged: 401 is a verdict on the credential."""
        policy = SignalPolicy(_collector())
        policy.apply(self._auth_failed(401))
        assert policy.circuit_open is True

    def test_absent_endpoint_trips_and_records_status(self) -> None:
        """UC-87b: the trip carries 404 so the blocked poll names the endpoint."""
        policy = SignalPolicy(_collector())
        policy.apply(self._auth_failed(404))
        assert policy.circuit_open is True
        assert policy.circuit_trip_status_code == 404
