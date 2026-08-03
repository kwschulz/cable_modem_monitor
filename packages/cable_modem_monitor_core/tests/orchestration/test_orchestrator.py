"""Tests for Orchestrator policy engine.

Covers signal→policy mapping, circuit breaker, backoff, status
derivation, transition detection, restart dispatch, Recovery
wiring, and diagnostics. Tests mock the collector — no
HTTP traffic.

Use case coverage (orchestrator level):
- UC-01: First poll — fresh login
- UC-02: Subsequent poll — session reuse
- UC-03: On-demand refresh (same API)
- UC-04: Zero channels with system_info — NO_SIGNAL
- UC-05: Zero channels without system_info — NO_SIGNAL with warning
- UC-07: DOCSIS status derivation (table-driven)
- UC-10: Wrong credentials — single failure
- UC-11: Transient auth failure — streak resets on success
- UC-12: Firmware lockout — AUTH_LOCKOUT trips circuit immediately
- UC-13: LOAD_AUTH — threshold circuit breaker, session issues self-correct
- UC-14: Circuit breaker trip — credential rejection
- UC-15: Circuit breaker blocks polling
- UC-17: LOAD_AUTH — 401 on data page
- UC-18: LOAD_AUTH — self-correcting stale session
- UC-19: Login page detection → LOAD_AUTH
- UC-20: Password changed after months of success
- UC-30: Connection refused — UNREACHABLE
- UC-31: HTTP timeout — UNREACHABLE
- UC-32: HTTP 5xx on data page — UNREACHABLE
- UC-33: Parser error — PARSER_ISSUE
- UC-34: Status transition — unreachable → online
- UC-35: All-or-nothing page loading
- UC-36: Health recovery clears connectivity backoff
- UC-40: Restart dispatches and opens a recovery window
- UC-43: Polls during a recovery window are unaffected
- UC-44: Restart not supported
- UC-45: Restart bypasses circuit breaker
- UC-49: Connectivity outage engages the recovery window
- UC-60: Diagnostics snapshot
- UC-88: Reboot-signal vote opens a recovery window
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from solentlabs.cable_modem_monitor_core.orchestration.models import (
    ModemResult,
    ModemSnapshot,
)
from solentlabs.cable_modem_monitor_core.orchestration.orchestrator import (
    Orchestrator,
    RestartNotSupportedError,
)
from solentlabs.cable_modem_monitor_core.orchestration.signals import (
    CollectorSignal,
    ConnectionStatus,
    DocsisStatus,
)

# ------------------------------------------------------------------
# Helpers — mock collector and config
# ------------------------------------------------------------------


def _make_modem_data(
    *,
    downstream: list[dict[str, Any]] | None = None,
    upstream: list[dict[str, Any]] | None = None,
    system_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal ModemData dict."""
    return {
        "downstream": downstream or [],
        "upstream": upstream or [],
        "system_info": system_info or {},
    }


def _make_channels(
    ds_count: int = 24,
    us_count: int = 4,
    *,
    lock_status: str | None = "locked",
) -> dict[str, Any]:
    """Build ModemData with channel lists."""
    downstream = []
    for i in range(ds_count):
        ch: dict[str, Any] = {"channel_id": i + 1, "frequency": 600 + i * 6}
        if lock_status is not None:
            ch["lock_status"] = lock_status
        downstream.append(ch)

    upstream = [{"channel_id": i + 1, "frequency": 30 + i * 6} for i in range(us_count)]

    return _make_modem_data(
        downstream=downstream,
        upstream=upstream,
        system_info={"firmware": "1.0"},
    )


def _ok_result(
    modem_data: dict[str, Any] | None = None,
) -> ModemResult:
    """Build a successful ModemResult."""
    if modem_data is None:
        modem_data = _make_channels()
    return ModemResult(success=True, modem_data=modem_data)


def _fail_result(
    signal: CollectorSignal,
    error: str = "",
) -> ModemResult:
    """Build a failed ModemResult."""
    return ModemResult(success=False, signal=signal, error=error)


def _mock_collector(
    results: list[ModemResult] | ModemResult | None = None,
    *,
    single_session: bool = False,
) -> MagicMock:
    """Build a mock ModemDataCollector.

    Args:
        results: Single result or list of results for sequential
            execute() calls. Defaults to a single OK result.
        single_session: Whether the firmware enforces one session.
    """
    collector = MagicMock()
    collector.session_is_valid = True
    # Set explicitly: a bare MagicMock attribute is truthy, which would
    # silently route AUTH_FAILED down the single-session threshold path.
    collector.is_single_session = single_session
    collector._session = MagicMock()
    collector._base_url = "http://192.168.100.1"
    collector._auth_context = None

    if results is None:
        collector.execute.return_value = _ok_result()
    elif isinstance(results, list):
        collector.execute.side_effect = results
    else:
        collector.execute.return_value = results

    return collector


def _mock_config(
    *,
    has_restart: bool = False,
) -> MagicMock:
    """Build a minimal ModemConfig-like object."""
    config = MagicMock()
    config.timeout = 10
    config.model = "T100"

    # Actions
    if has_restart:
        from solentlabs.cable_modem_monitor_core.models.modem_config.actions import (
            HttpAction,
        )

        config.actions.restart = HttpAction(
            type="http",
            method="POST",
            endpoint="/restart.htm",
            params={"restart": "1"},
        )
    else:
        config.actions = None

    return config


def _make_orchestrator(
    collector: MagicMock | None = None,
    config: MagicMock | None = None,
    health_monitor: Any | None = None,
) -> Orchestrator:
    """Build an Orchestrator with mock dependencies."""
    if collector is None:
        collector = _mock_collector()
    if config is None:
        config = _mock_config()
    return Orchestrator(
        collector=collector,
        health_monitor=health_monitor,
        modem_config=config,
    )


# ==================================================================
# Normal Operations
# ==================================================================


class TestFirstPoll:
    """UC-01: First poll — fresh login."""

    def test_returns_online_with_channels(self) -> None:
        orch = _make_orchestrator()
        snapshot = orch.get_modem_data()

        assert snapshot.connection_status == ConnectionStatus.ONLINE
        assert snapshot.docsis_status == DocsisStatus.OPERATIONAL
        assert snapshot.modem_data is not None
        assert len(snapshot.modem_data["downstream"]) == 24
        assert len(snapshot.modem_data["upstream"]) == 4
        assert snapshot.collector_signal == CollectorSignal.OK
        assert snapshot.error == ""

    def test_status_property_updated(self) -> None:
        orch = _make_orchestrator()
        orch.get_modem_data()

        assert orch.status == ConnectionStatus.ONLINE

    def test_diagnostics_after_first_poll(self) -> None:
        orch = _make_orchestrator()
        orch.get_modem_data()

        m = orch.diagnostics()
        assert m.auth_failure_streak == 0
        assert m.circuit_breaker_open is False
        assert m.session_is_valid is True
        assert m.poll_duration is not None
        assert m.poll_duration >= 0
        assert m.last_poll_at is not None


class TestSessionReuse:
    """UC-02: Subsequent poll — session reuse."""

    def test_no_extra_auth_on_second_poll(self) -> None:
        collector = _mock_collector([_ok_result(), _ok_result()])
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()
        orch.get_modem_data()

        assert collector.execute.call_count == 2
        assert orch.status == ConnectionStatus.ONLINE


class TestOnDemandRefresh:
    """UC-03: On-demand refresh uses same API."""

    def test_circuit_blocks_manual_calls(self) -> None:
        collector = _mock_collector(_fail_result(CollectorSignal.AUTH_FAILED))
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()  # AUTH_FAILED → circuit open
        snapshot = orch.get_modem_data()  # manual call — circuit blocks

        assert snapshot.connection_status == ConnectionStatus.AUTH_FAILED
        assert collector.execute.call_count == 1


# ==================================================================
# Zero Channels
# ==================================================================


class TestZeroChannels:
    """UC-04/05: Zero channels derivation."""

    def test_zero_channels_with_system_info(self) -> None:
        """UC-04: system_info present → NO_SIGNAL."""
        data = _make_modem_data(system_info={"firmware": "1.0"})
        collector = _mock_collector(_ok_result(data))
        orch = _make_orchestrator(collector=collector)

        snapshot = orch.get_modem_data()

        assert snapshot.connection_status == ConnectionStatus.NO_SIGNAL
        assert snapshot.modem_data is not None
        assert snapshot.modem_data["downstream"] == []
        assert snapshot.collector_signal == CollectorSignal.OK

    def test_zero_channels_without_system_info(self, caplog: Any) -> None:
        """UC-05: no system_info → NO_SIGNAL with warning."""
        data = _make_modem_data()
        collector = _mock_collector(_ok_result(data))
        orch = _make_orchestrator(collector=collector)

        with caplog.at_level(logging.WARNING):
            snapshot = orch.get_modem_data()

        assert snapshot.connection_status == ConnectionStatus.NO_SIGNAL
        assert "Zero channels and no system_info" in caplog.text

    def test_zero_channels_does_not_increment_streak(self) -> None:
        """UC-04: Zero channels is valid data, not an auth failure."""
        data = _make_modem_data(system_info={"firmware": "1.0"})
        collector = _mock_collector(_ok_result(data))
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()

        assert orch.diagnostics().auth_failure_streak == 0


# ==================================================================
# DOCSIS Status Derivation — UC-07
# ==================================================================


# ┌──────────────────────────────┬────────┬──────────────────┬──────────────────────┐
# │ DS lock_status values        │ US cnt │ Expected         │ system_info override │
# ├──────────────────────────────┼────────┼──────────────────┼──────────────────────┤
# │ All "locked"                 │ > 0    │ OPERATIONAL      │ —                    │
# │ All "locked"                 │ 0      │ PARTIAL_LOCK     │ —                    │
# │ Some "locked", some not      │ > 0    │ PARTIAL_LOCK     │ —                    │
# │ None "locked"                │ > 0    │ NOT_LOCKED       │ —                    │
# │ No DS channels               │ any    │ UNKNOWN(absent)  │ —                    │
# │ lock_status absent           │ > 0    │ UNKNOWN(absent)  │ —                    │
# │ lock_status absent           │ > 0    │ raw string       │ docsis_status        │
# │ lock_status absent           │ > 0    │ raw string       │ docsis_status (lc)   │
# │ lock_status absent           │ > 0    │ raw string       │ non-operational      │
# └──────────────────────────────┴────────┴──────────────────┴──────────────────────┘
#
# fmt: off
DOCSIS_STATUS_CASES = [
    # (ds_channels,                      us_count, expected,               description,                system_info)
    ([{"lock_status": "locked"}] * 4,    2,        DocsisStatus.OPERATIONAL,  "all locked + upstream",    None),
    ([{"lock_status": "locked"}] * 4,    0,        DocsisStatus.PARTIAL_LOCK, "all locked + no upstream", None),
    ([{"lock_status": "locked"},
      {"lock_status": "not_locked"}],    2,        DocsisStatus.PARTIAL_LOCK, "some locked",              None),
    ([{"lock_status": "not_locked"}] * 3, 2,       DocsisStatus.NOT_LOCKED,   "none locked",              None),
    ([],                                 2,        DocsisStatus.UNKNOWN,      "no DS channels",           None),
    ([{"frequency": 600}] * 3,          2,        DocsisStatus.UNKNOWN,      "no lock_status field",     None),
    ([{"frequency": 600}] * 3,          2,        "OPERATIONAL",             "fallback: docsis_status",
     {"docsis_status": "OPERATIONAL"}),
    ([{"frequency": 600}] * 3,          2,        "Operational",             "fallback: docsis_status case insensitive",
     {"docsis_status": "Operational"}),
    ([{"frequency": 600}] * 3,          2,        "Not Synchronized",        "fallback: non-operational",
     {"docsis_status": "Not Synchronized"}),
]
# fmt: on


@pytest.mark.parametrize(
    "ds_channels,us_count,expected,desc,system_info_override",
    DOCSIS_STATUS_CASES,
    ids=[c[3] for c in DOCSIS_STATUS_CASES],
)
def test_docsis_status_derivation(
    ds_channels: list[dict[str, Any]],
    us_count: int,
    expected: str,
    desc: str,
    system_info_override: dict[str, Any] | None,
) -> None:
    """UC-07: DOCSIS status from lock_status fields."""
    upstream = [{"channel_id": i} for i in range(us_count)]
    data = _make_modem_data(
        downstream=ds_channels,
        upstream=upstream,
        system_info=system_info_override or {"firmware": "1.0"},
    )
    collector = _mock_collector(_ok_result(data))
    orch = _make_orchestrator(collector=collector)

    snapshot = orch.get_modem_data()

    assert snapshot.docsis_status == expected


# ------------------------------------------------------------------
# enrich_docsis_status — unit-level (system_info mutation contract)
# ------------------------------------------------------------------

_US1 = [{"channel_id": 1}]
_LOCKED = {"lock_status": "locked"}
_UNLOCKED = {"lock_status": "not_locked"}
_NO_LOCK = {"frequency": 600}

# fmt: off
ENRICH_CASES = [
    # (ds,                          us,   sysinfo,                       expected,       id)
    # Derivable from lock_status
    ([_LOCKED] * 3,                 _US1,  {},                           "Operational",  "all-locked+us"),
    ([_LOCKED] * 3,                 [],    {},                           "partial_lock", "all-locked-no-us"),
    ([_LOCKED, _UNLOCKED],          _US1,  {},                           "partial_lock", "some-locked"),
    ([_UNLOCKED] * 2,               _US1,  {},                           "not_locked",   "none-locked"),
    # Not derivable — field stays absent
    ([],                            _US1,  {},                           None,           "no-ds"),
    ([_NO_LOCK] * 2,                _US1,  {},                           None,           "no-lock-status"),
    # Parser provided — not overwritten
    ([_LOCKED] * 3,                 _US1,  {"docsis_status": "Allowed"}, "Allowed",      "parser-wins"),
    ([_NO_LOCK] * 2,                _US1,  {"docsis_status": "Ranging"}, "Ranging",      "parser-no-lock"),
]
# fmt: on


@pytest.mark.parametrize(
    "ds_channels,upstream,system_info,expected_docsis,desc",
    ENRICH_CASES,
    ids=[c[4] for c in ENRICH_CASES],
)
def test_enrich_docsis_status(
    ds_channels: list[dict[str, Any]],
    upstream: list[dict[str, Any]],
    system_info: dict[str, Any],
    expected_docsis: str | None,
    desc: str,
) -> None:
    """enrich_docsis_status writes to system_info or leaves it absent."""
    from solentlabs.cable_modem_monitor_core.orchestration.status import enrich_docsis_status

    modem_data: dict[str, Any] = {
        "downstream": ds_channels,
        "upstream": upstream,
        "system_info": dict(system_info),
    }

    enrich_docsis_status(modem_data)

    if expected_docsis is None:
        assert "docsis_status" not in modem_data["system_info"]
    else:
        assert modem_data["system_info"]["docsis_status"] == expected_docsis


# ==================================================================
# Auth Failures — UC-10 through UC-20
# ==================================================================


class TestAuthFailure:
    """UC-10/UC-87: Wrong credentials — circuit trips immediately."""

    def test_single_auth_failure_trips_circuit(self) -> None:
        collector = _mock_collector(_fail_result(CollectorSignal.AUTH_FAILED, "wrong password"))
        orch = _make_orchestrator(collector=collector)

        snapshot = orch.get_modem_data()

        assert snapshot.connection_status == ConnectionStatus.AUTH_FAILED
        assert orch.diagnostics().auth_failure_streak == 1
        assert orch.diagnostics().circuit_breaker_open is True
        assert snapshot.modem_data is None


class TestStreakReset:
    """UC-11: LOAD_AUTH self-corrects — streak resets on success."""

    def test_success_resets_streak(self) -> None:
        collector = _mock_collector(
            [
                _fail_result(CollectorSignal.LOAD_AUTH),
                _ok_result(),
            ]
        )
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()  # LOAD_AUTH retried in same poll, success keeps streak at 0
        assert orch.diagnostics().auth_failure_streak == 0
        assert orch.diagnostics().circuit_breaker_open is False


class TestLockout:
    """UC-12: Firmware lockout — AUTH_LOCKOUT trips circuit immediately."""

    def test_lockout_trips_circuit(self) -> None:
        collector = _mock_collector(_fail_result(CollectorSignal.AUTH_LOCKOUT))
        orch = _make_orchestrator(collector=collector)

        snapshot = orch.get_modem_data()

        assert snapshot.connection_status == ConnectionStatus.AUTH_FAILED
        assert orch.diagnostics().auth_failure_streak == 1
        assert orch.diagnostics().circuit_breaker_open is True
        assert collector.execute.call_count == 1

    def test_lockout_stops_polling(self) -> None:
        """AUTH_LOCKOUT → circuit open → no further collection."""
        collector = _mock_collector(_fail_result(CollectorSignal.AUTH_LOCKOUT))
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()  # lockout → circuit open
        collector.execute.reset_mock()

        snapshot = orch.get_modem_data()  # circuit blocks collection

        assert snapshot.connection_status == ConnectionStatus.AUTH_FAILED
        collector.execute.assert_not_called()


class TestCircuitBreaker:
    """UC-14/15/87: Circuit breaker trip and blocking."""

    def test_auth_failed_trips_immediately(self) -> None:
        """UC-87: First AUTH_FAILED → circuit open. One attempt, stop."""
        collector = _mock_collector(_fail_result(CollectorSignal.AUTH_FAILED))
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()

        assert orch.diagnostics().circuit_breaker_open is True
        assert orch.diagnostics().auth_failure_streak == 1
        assert collector.execute.call_count == 1

    def test_circuit_blocks_polling(self) -> None:
        """UC-15: Open circuit → no collection."""
        collector = _mock_collector(_fail_result(CollectorSignal.AUTH_FAILED))
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()  # AUTH_FAILED → circuit open
        collector.execute.reset_mock()

        snapshot = orch.get_modem_data()

        assert snapshot.connection_status == ConnectionStatus.AUTH_FAILED
        collector.execute.assert_not_called()

    def test_lockout_trips_immediately(self) -> None:
        """AUTH_LOCKOUT trips circuit on first occurrence."""
        collector = _mock_collector(_fail_result(CollectorSignal.AUTH_LOCKOUT))
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()

        assert orch.diagnostics().circuit_breaker_open is True
        assert orch.diagnostics().auth_failure_streak == 1

    def test_load_auth_uses_threshold(self) -> None:
        """LOAD_AUTH is a session issue — circuit trips at threshold, not immediately."""
        results = [_fail_result(CollectorSignal.LOAD_AUTH) for _ in range(10)]
        collector = _mock_collector(results)
        orch = _make_orchestrator(collector=collector)

        for _ in range(5):
            orch.get_modem_data()

        assert orch.diagnostics().circuit_breaker_open is False
        assert orch.diagnostics().auth_failure_streak == 5


class TestCircuitBreakerTripReason:
    """Blocked-poll errors preserve the trip reason — 404 trips are not credential problems."""

    def test_blocked_error_names_endpoint_not_found_for_404_trip(self) -> None:
        result = ModemResult(
            success=False,
            signal=CollectorSignal.AUTH_FAILED,
            error="login endpoint 404",
            auth_status_code=404,
        )
        collector = _mock_collector(result)
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()  # 404 trip
        snapshot = orch.get_modem_data()  # blocked

        assert "login endpoint not found" in snapshot.error
        assert "credentials" not in snapshot.error

    def test_blocked_error_names_credentials_for_credential_trip(self) -> None:
        collector = _mock_collector(_fail_result(CollectorSignal.AUTH_FAILED))
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()  # credential trip
        snapshot = orch.get_modem_data()  # blocked

        assert "reconfigure credentials" in snapshot.error


class TestLoadAuth:
    """UC-17/18/19: LOAD_AUTH signal handling."""

    def test_load_auth_clears_session(self) -> None:
        """UC-17: LOAD_AUTH clears session and increments streak on retry failure."""
        collector = _mock_collector(_fail_result(CollectorSignal.LOAD_AUTH))
        orch = _make_orchestrator(collector=collector)

        snapshot = orch.get_modem_data()

        assert snapshot.connection_status == ConnectionStatus.AUTH_FAILED
        assert orch.diagnostics().auth_failure_streak == 1
        assert collector.execute.call_count == 2
        collector.clear_session.assert_called()

    def test_load_auth_recovers_in_same_poll(self) -> None:
        """UC-18: LOAD_AUTH retries once immediately and returns success."""
        collector = _mock_collector(
            [
                _fail_result(CollectorSignal.LOAD_AUTH),
                _ok_result(),
            ]
        )
        orch = _make_orchestrator(collector=collector)

        snapshot = orch.get_modem_data()

        assert snapshot.connection_status == ConnectionStatus.ONLINE
        assert orch.diagnostics().auth_failure_streak == 0
        assert collector.execute.call_count == 2
        collector.clear_session.assert_called_once()

    def test_load_auth_self_corrects(self) -> None:
        """UC-18: a later successful poll leaves the streak clear."""
        collector = _mock_collector(
            [
                _fail_result(CollectorSignal.LOAD_AUTH),
                _ok_result(),
                _ok_result(),
            ]
        )
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()  # LOAD_AUTH retried in same poll → success
        snapshot = orch.get_modem_data()  # steady-state success

        assert snapshot.connection_status == ConnectionStatus.ONLINE
        assert orch.diagnostics().auth_failure_streak == 0

    def test_load_auth_escalates_to_circuit(self) -> None:
        """Persistent LOAD_AUTH eventually trips circuit breaker."""
        results = [_fail_result(CollectorSignal.LOAD_AUTH) for _ in range(12)]
        collector = _mock_collector(results)
        orch = _make_orchestrator(collector=collector)

        for _ in range(6):
            orch.get_modem_data()

        assert orch.diagnostics().circuit_breaker_open is True

    def test_load_auth_recovery_streak_resets_on_normal_success(self) -> None:
        """A normal poll breaks the consecutive stale-session recovery streak."""
        collector = _mock_collector(
            [
                _fail_result(CollectorSignal.LOAD_AUTH),
                _ok_result(),
                _ok_result(),
                _fail_result(CollectorSignal.LOAD_AUTH),
                _ok_result(),
            ]
        )
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()
        diag = orch.diagnostics()
        assert diag.stale_session_recovery_streak == 1
        assert diag.session_reuse_disabled is False

        orch.get_modem_data()
        diag = orch.diagnostics()
        assert diag.stale_session_recovery_streak == 0
        assert diag.session_reuse_disabled is False

        orch.get_modem_data()
        diag = orch.diagnostics()
        assert diag.stale_session_recovery_streak == 1
        assert diag.session_reuse_disabled is False

    def test_load_auth_calls_attempt_logout_before_retry(self) -> None:
        """LOAD_AUTH calls attempt_logout_before_retry before clear_session on retry."""
        collector = _mock_collector(_fail_result(CollectorSignal.LOAD_AUTH))
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()

        collector.attempt_logout_before_retry.assert_called_once()
        method_names = [c[0] for c in collector.mock_calls]
        assert method_names.index("attempt_logout_before_retry") < method_names.index("clear_session")


class TestLoadIntegrity:
    """UC-19a: LOAD_INTEGRITY signal handling — stub-page recovery (issue #151)."""

    def test_load_integrity_clears_session_and_increments_streak(self) -> None:
        """Persistent stub response → clears session, streak=1, status=AUTH_FAILED."""
        collector = _mock_collector(_fail_result(CollectorSignal.LOAD_INTEGRITY))
        orch = _make_orchestrator(collector=collector)

        snapshot = orch.get_modem_data()

        assert snapshot.connection_status == ConnectionStatus.AUTH_FAILED
        assert orch.diagnostics().auth_failure_streak == 1
        # Same-poll retry — collector executed twice
        assert collector.execute.call_count == 2
        collector.clear_session.assert_called()

    def test_load_integrity_recovers_in_same_poll(self) -> None:
        """First poll stub → cleared session → second poll real data → ONLINE."""
        collector = _mock_collector(
            [
                _fail_result(CollectorSignal.LOAD_INTEGRITY),
                _ok_result(),
            ]
        )
        orch = _make_orchestrator(collector=collector)

        snapshot = orch.get_modem_data()

        # Recovers within the same poll, no AUTH_FAILED surfaced
        assert snapshot.connection_status == ConnectionStatus.ONLINE
        assert orch.diagnostics().auth_failure_streak == 0
        assert collector.execute.call_count == 2
        collector.clear_session.assert_called_once()

    def test_load_integrity_escalates_to_circuit_breaker(self) -> None:
        """Sustained stub responses eventually trip the circuit breaker."""
        results = [_fail_result(CollectorSignal.LOAD_INTEGRITY) for _ in range(12)]
        collector = _mock_collector(results)
        orch = _make_orchestrator(collector=collector)

        for _ in range(6):
            orch.get_modem_data()

        assert orch.diagnostics().circuit_breaker_open is True

    def test_load_integrity_calls_attempt_logout_before_retry(self) -> None:
        """LOAD_INTEGRITY also calls attempt_logout_before_retry before clear_session."""
        collector = _mock_collector(_fail_result(CollectorSignal.LOAD_INTEGRITY))
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()

        collector.attempt_logout_before_retry.assert_called_once()
        method_names = [c[0] for c in collector.mock_calls]
        assert method_names.index("attempt_logout_before_retry") < method_names.index("clear_session")


class TestLoadAuthAdaptiveReuse:
    """UC-18 follow-on: stale-session recovery streak adapts session reuse."""

    def test_load_auth_disables_reuse_after_two_consecutive_recoveries(self) -> None:
        """Two consecutive stale-session recoveries disable reuse for this runtime."""
        collector = _mock_collector(
            [
                _fail_result(CollectorSignal.LOAD_AUTH),
                _ok_result(),
                _fail_result(CollectorSignal.LOAD_AUTH),
                _ok_result(),
                _ok_result(),
            ]
        )
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()
        diag = orch.diagnostics()
        assert diag.stale_session_recovery_streak == 1
        assert diag.session_reuse_disabled is False

        orch.get_modem_data()
        diag = orch.diagnostics()
        assert diag.stale_session_recovery_streak == 2
        assert diag.session_reuse_disabled is True

        collector.clear_session.reset_mock()
        orch.get_modem_data()

        assert collector.execute.call_count == 5
        collector.clear_session.assert_called_once()


class TestPasswordChanged:
    """UC-87: Password changed — circuit trips on first AUTH_FAILED."""

    def test_immediate_stop(self) -> None:
        """AUTH_FAILED → circuit open. One attempt, no escalation."""
        collector = _mock_collector(_fail_result(CollectorSignal.AUTH_FAILED))
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()  # wrong password → circuit trips

        assert orch.diagnostics().circuit_breaker_open is True
        assert orch.diagnostics().auth_failure_streak == 1
        assert collector.execute.call_count == 1

        # Subsequent polls are blocked — no more login attempts
        collector.execute.reset_mock()
        orch.get_modem_data()
        collector.execute.assert_not_called()


# ==================================================================
# Signal → Policy Mapping (table-driven)
# ==================================================================


# ┌─────────────────┬─────────────────┬───────────────┬────────┐
# │ Signal          │ Expected status │ Streak change │ Descr  │
# ├─────────────────┼─────────────────┼───────────────┼────────┤
# │ AUTH_FAILED     │ AUTH_FAILED     │ +1            │        │
# │ AUTH_LOCKOUT    │ AUTH_FAILED     │ +1            │        │
# │ LOAD_AUTH       │ AUTH_FAILED     │ +1            │        │
# │ CONNECTIVITY    │ UNREACHABLE     │ 0             │        │
# │ LOAD_ERROR      │ UNREACHABLE     │ 0             │        │
# │ PARSE_ERROR     │ PARSER_ISSUE    │ 0             │        │
# └─────────────────┴─────────────────┴───────────────┴────────┘
#
# fmt: off
SIGNAL_POLICY_CASES = [
    # (signal,                           expected_status,               streak_delta, description)
    (CollectorSignal.AUTH_FAILED,        ConnectionStatus.AUTH_FAILED,  1,            "auth failed"),
    (CollectorSignal.AUTH_LOCKOUT,       ConnectionStatus.AUTH_FAILED,  1,            "auth lockout"),
    (CollectorSignal.LOAD_AUTH,          ConnectionStatus.AUTH_FAILED,  1,            "load auth 401/403"),
    (CollectorSignal.CONNECTIVITY,       ConnectionStatus.UNREACHABLE,  0,            "connection refused"),
    (CollectorSignal.LOAD_ERROR,         ConnectionStatus.UNREACHABLE,  0,            "http 5xx"),
    (CollectorSignal.PARSE_ERROR,        ConnectionStatus.PARSER_ISSUE, 0,            "parse error"),
]
# fmt: on


@pytest.mark.parametrize(
    "signal,expected_status,streak_delta,desc",
    SIGNAL_POLICY_CASES,
    ids=[c[3] for c in SIGNAL_POLICY_CASES],
)
def test_signal_policy_mapping(
    signal: CollectorSignal,
    expected_status: ConnectionStatus,
    streak_delta: int,
    desc: str,
) -> None:
    """Each signal maps to the correct status and streak behavior."""
    collector = _mock_collector(_fail_result(signal))
    orch = _make_orchestrator(collector=collector)

    snapshot = orch.get_modem_data()

    assert snapshot.connection_status == expected_status
    assert orch.diagnostics().auth_failure_streak == streak_delta


# ==================================================================
# Connectivity Failures — UC-30 through UC-35
# ==================================================================


class TestConnectivityFailures:
    """UC-30/31/32: Connectivity, load, and parse errors."""

    def test_first_connectivity_failure(self) -> None:
        """UC-30: First connectivity failure returns UNREACHABLE."""
        collector = _mock_collector(_fail_result(CollectorSignal.CONNECTIVITY))
        orch = _make_orchestrator(collector=collector)

        snapshot = orch.get_modem_data()

        assert snapshot.connection_status == ConnectionStatus.UNREACHABLE
        assert orch.diagnostics().auth_failure_streak == 0
        assert orch.diagnostics().connectivity_streak == 1

    def test_connectivity_backoff_retries_on_clear(self) -> None:
        """First connectivity failure sets backoff=1; next poll clears and retries."""
        collector = _mock_collector(
            [
                _fail_result(CollectorSignal.CONNECTIVITY),
                _ok_result(),  # used when backoff clears
            ]
        )
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()  # CONNECTIVITY, backoff=1
        assert orch.diagnostics().connectivity_backoff_remaining == 1

        s2 = orch.get_modem_data()  # backoff clears (1→0) → retry succeeds
        assert s2.connection_status == ConnectionStatus.ONLINE
        assert collector.execute.call_count == 2

    def test_connectivity_backoff_exponential(self) -> None:
        """Backoff grows: 1, 2, 4 with exponential pattern."""
        collector = _mock_collector()
        collector.execute.return_value = _fail_result(CollectorSignal.CONNECTIVITY)
        orch = _make_orchestrator(collector=collector)

        # Failure 1: backoff=1
        orch.get_modem_data()
        assert orch.diagnostics().connectivity_backoff_remaining == 1

        # Backoff=1 clears immediately → Failure 2: backoff=2
        orch.get_modem_data()
        assert orch.diagnostics().connectivity_backoff_remaining == 2
        assert orch.diagnostics().connectivity_streak == 2

        # Skip 1 poll (2→1), then clears → Failure 3: backoff=4
        orch.get_modem_data()  # skip
        orch.get_modem_data()  # clears → execute → fail
        assert orch.diagnostics().connectivity_backoff_remaining == 4
        assert orch.diagnostics().connectivity_streak == 3

    def test_connectivity_backoff_caps_at_max(self) -> None:
        """Backoff caps at max_connectivity_backoff (default 6)."""
        collector = _mock_collector()
        collector.execute.return_value = _fail_result(CollectorSignal.CONNECTIVITY)
        orch = _make_orchestrator(collector=collector)

        # Drive to streak=4 where 2^(4-1)=8, capped at 6.
        # With backoff=N, N-1 polls are skipped before the next execute.
        # streak=1(bo=1) → clears → streak=2(bo=2) → skip 1 → clears →
        # streak=3(bo=4) → skip 3 → clears → streak=4(bo=6, capped)
        for _ in range(8):
            orch.get_modem_data()

        assert orch.diagnostics().connectivity_streak == 4
        assert orch.diagnostics().connectivity_backoff_remaining == 6

    def test_success_resets_connectivity(self) -> None:
        """Successful poll clears connectivity streak and backoff."""
        collector = _mock_collector(
            [
                _fail_result(CollectorSignal.CONNECTIVITY),
                _fail_result(CollectorSignal.CONNECTIVITY),  # backoff=1 clears → execute
                _ok_result(),  # backoff=2: skip 1, then clears → execute
            ]
        )
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()  # streak=1, backoff=1
        orch.get_modem_data()  # backoff clears → streak=2, backoff=2
        orch.get_modem_data()  # skip (2→1)
        orch.get_modem_data()  # backoff clears → success

        assert orch.diagnostics().connectivity_streak == 0
        assert orch.diagnostics().connectivity_backoff_remaining == 0

    def test_reset_connectivity_clears_state(self) -> None:
        """reset_connectivity() clears backoff for immediate retry."""
        collector = _mock_collector(
            [
                _fail_result(CollectorSignal.CONNECTIVITY),
                _ok_result(),
            ]
        )
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()  # streak=1, backoff=1
        assert orch.diagnostics().connectivity_backoff_remaining == 1

        orch.reset_connectivity()
        assert orch.diagnostics().connectivity_streak == 0
        assert orch.diagnostics().connectivity_backoff_remaining == 0

        # Next poll runs immediately
        s = orch.get_modem_data()
        assert s.connection_status == ConnectionStatus.ONLINE
        assert collector.execute.call_count == 2

    def test_reset_connectivity_noop_when_not_backing_off(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """reset_connectivity() is silent when there is no active backoff."""
        collector = _mock_collector([_ok_result()])
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()  # streak=0
        assert orch.diagnostics().connectivity_streak == 0

        with caplog.at_level(logging.INFO):
            orch.reset_connectivity()

        assert "Connectivity backoff reset" not in caplog.text
        assert orch.diagnostics().connectivity_streak == 0

    def test_close_delegates_to_collector(self) -> None:
        """close() releases the collector's session."""
        collector = _mock_collector([_ok_result()])
        orch = _make_orchestrator(collector=collector)

        orch.close()

        collector.close.assert_called_once_with()

    def test_non_connectivity_failure_clears_connectivity(self) -> None:
        """Auth failure after connectivity outage clears connectivity backoff."""
        collector = _mock_collector()
        orch = _make_orchestrator(collector=collector)

        # Build up connectivity backoff
        collector.execute.return_value = _fail_result(CollectorSignal.CONNECTIVITY)
        orch.get_modem_data()  # streak=1, backoff=1

        # Modem comes back with auth error — backoff=1 clears, execute runs
        collector.execute.return_value = _fail_result(CollectorSignal.AUTH_FAILED)
        orch.get_modem_data()

        assert orch.diagnostics().connectivity_streak == 0
        assert orch.diagnostics().connectivity_backoff_remaining == 0
        assert orch.diagnostics().auth_failure_streak == 1

    def test_load_error_no_streak(self) -> None:
        """UC-32: HTTP 5xx → UNREACHABLE, no auth streak increment."""
        collector = _mock_collector(_fail_result(CollectorSignal.LOAD_ERROR))
        orch = _make_orchestrator(collector=collector)

        snapshot = orch.get_modem_data()

        assert snapshot.connection_status == ConnectionStatus.UNREACHABLE
        assert orch.diagnostics().auth_failure_streak == 0

    def test_parse_error(self) -> None:
        """UC-33: Parser error → PARSER_ISSUE."""
        collector = _mock_collector(_fail_result(CollectorSignal.PARSE_ERROR, "unexpected format"))
        orch = _make_orchestrator(collector=collector)

        snapshot = orch.get_modem_data()

        assert snapshot.connection_status == ConnectionStatus.PARSER_ISSUE
        assert orch.diagnostics().auth_failure_streak == 0


class TestStatusTransition:
    """UC-34: Status transition detection."""

    def test_unreachable_to_online_logged(self, caplog: Any) -> None:
        """Transition from UNREACHABLE to ONLINE is logged."""
        collector = _mock_collector(
            [
                _fail_result(CollectorSignal.CONNECTIVITY),
                _ok_result(),
            ]
        )
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()  # UNREACHABLE, backoff=1

        with caplog.at_level(logging.INFO):
            orch.get_modem_data()  # backoff clears → ONLINE

        assert "Status transition [T100]: unreachable" in caplog.text
        assert "online" in caplog.text

    def test_online_to_unreachable_logged(self, caplog: Any) -> None:
        """Transition from ONLINE to UNREACHABLE is logged."""
        collector = _mock_collector(
            [
                _ok_result(),
                _fail_result(CollectorSignal.CONNECTIVITY),
            ]
        )
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()  # ONLINE

        with caplog.at_level(logging.INFO):
            orch.get_modem_data()  # UNREACHABLE

        assert "Status transition [T100]: online" in caplog.text
        assert "unreachable" in caplog.text

    def test_no_transition_on_same_status(self, caplog: Any) -> None:
        """No transition log when status stays the same."""
        collector = _mock_collector([_ok_result(), _ok_result()])
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()

        with caplog.at_level(logging.INFO):
            orch.get_modem_data()

        assert "Status transition" not in caplog.text


# ==================================================================
# Restart — UC-42/43/44/45
# ==================================================================


class TestRestart:
    """Orchestrator.restart() delegates to run_restart and returns quickly.

    Detailed run_restart behavior is covered in
    ``tests/orchestration/test_restart.py``. These tests confirm the
    orchestrator wires the collector, config, and Recovery instance
    through correctly and that button-gated behaviors (circuit
    breaker bypass, streak isolation) still hold.
    """

    def test_restart_not_supported(self) -> None:
        """UC-44: No actions.restart → RestartNotSupportedError."""
        orch = _make_orchestrator(config=_mock_config(has_restart=False))

        with pytest.raises(RestartNotSupportedError):
            orch.restart()

    def test_restart_opens_recovery_window(self) -> None:
        """UC-40: Successful restart dispatch opens a recovery window."""
        config = _mock_config(has_restart=True)
        orch = _make_orchestrator(config=config)

        assert orch._recovery.active is False

        result = orch.restart()

        assert result.success is True
        assert result.error == ""
        assert orch._recovery.active is True

    def test_restart_clears_session(self) -> None:
        """Session is cleared exactly once after a successful command."""
        config = _mock_config(has_restart=True)
        collector = _mock_collector()
        orch = _make_orchestrator(collector=collector, config=config)

        orch.restart()

        assert collector.clear_session.call_count == 1

    def test_restart_command_failed_on_action_exception(self) -> None:
        """Action executor raising yields error=command_failed."""
        config = _mock_config(has_restart=True)
        collector = _mock_collector()
        collector._session.request.side_effect = RuntimeError("action failed")
        orch = _make_orchestrator(collector=collector, config=config)

        result = orch.restart()

        assert result.success is False
        assert result.error == "command_failed"
        # Recovery window is NOT entered when the command failed.
        assert orch._recovery.active is False

    def test_restart_bypasses_circuit_breaker(self) -> None:
        """UC-45: Restart executes even when circuit breaker is open."""
        config = _mock_config(has_restart=True)
        collector = _mock_collector()
        orch = _make_orchestrator(collector=collector, config=config)

        # Trip circuit breaker via AUTH_FAILED (immediate)
        collector.execute.return_value = _fail_result(CollectorSignal.AUTH_FAILED, "wrong password")
        orch.get_modem_data()

        assert orch.diagnostics().circuit_breaker_open is True

        # Polling is blocked
        snap = orch.get_modem_data()
        assert snap.connection_status == ConnectionStatus.AUTH_FAILED

        # Restart still dispatches — circuit breaker is not consulted.
        result = orch.restart()

        assert result.success is True

    def test_restart_auth_failure_does_not_increment_streak(self) -> None:
        """UC-45: Auth failure during restart does not affect polling streak."""
        config = _mock_config(has_restart=True)
        collector = _mock_collector()
        orch = _make_orchestrator(collector=collector, config=config)

        # One auth failure during normal polling
        collector.execute.return_value = _fail_result(CollectorSignal.AUTH_FAILED, "wrong password")
        orch.get_modem_data()
        streak_before = orch.diagnostics().auth_failure_streak
        assert streak_before == 1

        # Restart action fails — run_restart catches and returns command_failed
        collector._session.request.side_effect = RuntimeError("auth error")
        orch.restart()

        # Streak is unchanged — restart dispatch failures are not polling failures.
        assert orch.diagnostics().auth_failure_streak == streak_before


# ==================================================================
# Unplanned Restart — UC-49
# ==================================================================


class TestUnplannedRestart:
    """UC-49: Modem restarted externally — recovery through normal polling."""

    def test_outage_and_recovery_sequence(self) -> None:
        """ONLINE → UNREACHABLE (with backoff) → LOAD_AUTH retry → ONLINE."""
        collector = _mock_collector()
        orch = _make_orchestrator(collector=collector)

        # Poll 1: Normal — ONLINE
        collector.execute.return_value = _ok_result()
        snap = orch.get_modem_data()
        assert snap.connection_status == ConnectionStatus.ONLINE

        # Poll 2: Modem down — UNREACHABLE, backoff=1
        collector.execute.return_value = _fail_result(CollectorSignal.CONNECTIVITY, "Connection refused")
        snap = orch.get_modem_data()
        assert snap.connection_status == ConnectionStatus.UNREACHABLE

        # Poll 3: Backoff clears, modem back but stale session — retry succeeds same poll
        collector.execute.side_effect = [
            _fail_result(CollectorSignal.LOAD_AUTH, "401 on data page"),
            _ok_result(),
        ]
        snap = orch.get_modem_data()
        assert snap.connection_status == ConnectionStatus.ONLINE
        collector.clear_session.assert_called()
        # Connectivity cleared because modem responded
        assert orch.diagnostics().connectivity_streak == 0

    def test_connectivity_never_trips_circuit_breaker(self) -> None:
        """CONNECTIVITY failures never trip the auth circuit breaker."""
        collector = _mock_collector()
        orch = _make_orchestrator(collector=collector)

        # Drive many connectivity failures through backoff cycles
        collector.execute.return_value = _fail_result(CollectorSignal.CONNECTIVITY)
        for _ in range(50):
            orch.get_modem_data()

        assert orch.diagnostics().auth_failure_streak == 0
        assert not orch.diagnostics().circuit_breaker_open

    def test_stale_session_self_corrects(self, caplog: pytest.LogCaptureFixture) -> None:
        """LOAD_AUTH clears session and recovers within the same poll."""
        collector = _mock_collector()
        orch = _make_orchestrator(collector=collector)

        # Normal operation
        collector.execute.return_value = _ok_result()
        orch.get_modem_data()

        # Stale session detected
        collector.execute.side_effect = [
            _fail_result(CollectorSignal.LOAD_AUTH, "401 on data page"),
            _ok_result(),
        ]
        with caplog.at_level(logging.INFO):
            snap = orch.get_modem_data()

        assert "LOAD_AUTH" in caplog.text
        collector.clear_session.assert_called_once()
        assert snap.connection_status == ConnectionStatus.ONLINE


# ==================================================================
# Recovery wiring — UC-40, UC-43, UC-49, UC-88
# ==================================================================


class TestRecoveryWiring:
    """Orchestrator wires Recovery's tick / evaluate_* / observer.

    Detailed Recovery behavior lives in ``test_recovery.py``; these
    tests confirm the orchestrator calls into Recovery at the right
    points in the collection flow and exposes the expected public
    surface.
    """

    def test_recovery_active_delegates_to_recovery_module(self) -> None:
        """``recovery_active`` reads through to the Recovery instance."""
        orch = _make_orchestrator()

        assert orch.recovery_active is False

        # Simulate an open window by calling into the real Recovery.
        orch._recovery.begin("restart_command")

        assert orch.recovery_active is True

    def test_set_recovery_observer_registers_callback(self) -> None:
        """Observer installed via orchestrator fires on Recovery transitions."""
        orch = _make_orchestrator()
        calls: list[None] = []

        orch.set_recovery_observer(lambda: calls.append(None))

        orch._recovery.begin("restart_command")

        assert len(calls) == 1

    def test_set_recovery_observer_none_clears_callback(self) -> None:
        """Passing None removes a previously-registered observer."""
        orch = _make_orchestrator()
        calls: list[None] = []
        orch.set_recovery_observer(lambda: calls.append(None))

        # Fire once to prove it's wired.
        orch._recovery.begin("restart_command")
        assert len(calls) == 1

        orch.set_recovery_observer(None)

        # Re-entry — observer cleared, so no additional calls.
        orch._recovery.begin("restart_command")
        assert len(calls) == 1

    def test_connectivity_failure_opens_recovery_window(self) -> None:
        """UC-49: A CONNECTIVITY failure engages Recovery via evaluate_failure."""
        collector = _mock_collector(_fail_result(CollectorSignal.CONNECTIVITY, "refused"))
        orch = _make_orchestrator(collector=collector)

        assert orch.recovery_active is False

        orch.get_modem_data()

        assert orch.recovery_active is True

    def test_non_connectivity_failure_does_not_open_window(self) -> None:
        """AUTH_FAILED / PARSE_ERROR / LOAD_* do not trigger a recovery window."""
        collector = _mock_collector(_fail_result(CollectorSignal.AUTH_FAILED, "bad"))
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()

        assert orch.recovery_active is False

    def test_success_updates_recovery_history(self) -> None:
        """UC-88 setup: successful polls feed Recovery's reboot-signal baselines."""
        data = _make_modem_data(
            system_info={
                "total_corrected": 100,
                "total_uncorrected": 5,
                "system_uptime": "1000",
            }
        )
        collector = _mock_collector(_ok_result(data))
        orch = _make_orchestrator(collector=collector)

        assert orch._recovery._prev_counters is None
        assert orch._recovery._prev_uptime is None

        orch.get_modem_data()

        # evaluate_snapshot refreshed the baselines.
        assert orch._recovery._prev_counters == (100, 5)
        assert orch._recovery._prev_uptime == 1000

    def test_reboot_signal_vote_opens_window_on_second_poll(self) -> None:
        """UC-88: 2-of-3 signals across two successful polls → window open."""
        # Baseline poll — high counters, high uptime, Operational.
        data1 = _make_modem_data(
            system_info={
                "total_corrected": 500,
                "total_uncorrected": 20,
                "system_uptime": "5000",
                "docsis_status": "Operational",
            }
        )
        # Post-reboot poll — counters reset + uptime drop (2 signals).
        data2 = _make_modem_data(
            system_info={
                "total_corrected": 0,
                "total_uncorrected": 0,
                "system_uptime": "30",
                "docsis_status": "Operational",
            }
        )
        collector = _mock_collector([_ok_result(data1), _ok_result(data2)])
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()  # establishes baseline
        assert orch.recovery_active is False

        orch.get_modem_data()  # post-reboot — vote fires

        assert orch.recovery_active is True

    def test_tick_closes_window_on_subsequent_poll(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Recovery.tick() runs at the top of every poll, closing expired windows."""
        from solentlabs.cable_modem_monitor_core.orchestration import recovery as rec_module

        # Drive monotonic time from a controlled clock.
        clock = {"now": 1000.0}
        monkeypatch.setattr(rec_module.time, "monotonic", lambda: clock["now"])

        orch = _make_orchestrator()
        orch._recovery.begin("restart_command")
        assert orch.recovery_active is True

        # Advance past the window deadline; the next poll should
        # observe the close via tick().
        clock["now"] = 1000.0 + orch._recovery.WINDOW_SECONDS + 1

        orch.get_modem_data()

        assert orch.recovery_active is False


# ==================================================================
# Diagnostics — UC-60
# ==================================================================


class TestDiagnostics:
    """UC-60: Diagnostics snapshot."""

    def test_diagnostics_before_any_poll(self) -> None:
        """Diagnostics available before first poll."""
        orch = _make_orchestrator()
        m = orch.diagnostics()

        assert m.poll_duration is None
        assert m.auth_failure_streak == 0
        assert m.circuit_breaker_open is False
        assert m.connectivity_streak == 0
        assert m.connectivity_backoff_remaining == 0
        assert m.last_poll_at is None

    def test_diagnostics_after_successful_poll(self) -> None:
        """Diagnostics reflect latest poll."""
        orch = _make_orchestrator()
        orch.get_modem_data()
        m = orch.diagnostics()

        assert m.poll_duration is not None
        assert m.poll_duration >= 0
        assert m.auth_failure_streak == 0
        assert m.circuit_breaker_open is False
        assert m.session_is_valid is True
        assert m.last_poll_at is not None

    def test_diagnostics_after_failure(self) -> None:
        """Diagnostics track auth failure streak."""
        collector = _mock_collector(_fail_result(CollectorSignal.LOAD_AUTH))
        orch = _make_orchestrator(collector=collector)
        orch.get_modem_data()
        m = orch.diagnostics()

        assert m.auth_failure_streak == 1
        assert m.circuit_breaker_open is False
        assert m.poll_duration is not None

    def test_diagnostics_no_side_effects(self) -> None:
        """Calling diagnostics() has no side effects."""
        orch = _make_orchestrator()
        m1 = orch.diagnostics()
        m2 = orch.diagnostics()

        assert m1.auth_failure_streak == m2.auth_failure_streak
        assert m1.circuit_breaker_open == m2.circuit_breaker_open

    def test_diagnostics_include_field_outcomes(self) -> None:
        """The collector's field outcomes land on the diagnostics snapshot."""
        collector = _mock_collector()
        collector.last_system_info_fields_missing = ["system_uptime"]
        collector.system_info_fields_failed = {"docsis_status": "garbage"}
        orch = _make_orchestrator(collector=collector)

        snapshot = orch.diagnostics()

        assert snapshot.system_info_fields_missing == ["system_uptime"]
        assert snapshot.system_info_fields_failed == {"docsis_status": "garbage"}

    def test_diagnostics_available_with_circuit_open(self) -> None:
        """Diagnostics work even when circuit breaker is open."""
        collector = _mock_collector(_fail_result(CollectorSignal.AUTH_FAILED))
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()  # AUTH_FAILED → circuit open

        m = orch.diagnostics()
        assert m.circuit_breaker_open is True
        assert m.auth_failure_streak == 1


# ==================================================================
# Status property
# ==================================================================


class TestStatusProperty:
    """Orchestrator.status property."""

    def test_default_before_poll(self) -> None:
        """Status is UNREACHABLE before first poll."""
        orch = _make_orchestrator()
        assert orch.status == ConnectionStatus.UNREACHABLE

    def test_reflects_last_poll(self) -> None:
        """Status reflects the last poll result."""
        collector = _mock_collector(
            [
                _ok_result(),
                _fail_result(CollectorSignal.CONNECTIVITY),
            ]
        )
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()
        assert orch.status == ConnectionStatus.ONLINE

        orch.get_modem_data()
        assert orch.status == ConnectionStatus.UNREACHABLE  # type: ignore[comparison-overlap]


# ==================================================================
# Health monitor integration
# ==================================================================


class TestHealthMonitorIntegration:
    """Health monitor read on successful collection."""

    def test_reads_latest_health_info(self) -> None:
        """Snapshot includes health_info from health monitor."""
        from solentlabs.cable_modem_monitor_core.orchestration.models import (
            HealthInfo,
        )
        from solentlabs.cable_modem_monitor_core.orchestration.signals import (
            HealthStatus,
        )

        health_info = HealthInfo(
            health_status=HealthStatus.RESPONSIVE,
            icmp_latency_ms=5.0,
        )
        health_monitor = MagicMock()
        health_monitor.latest = health_info
        orch = _make_orchestrator(health_monitor=health_monitor)

        snapshot = orch.get_modem_data()

        assert snapshot.health_info is health_info

    def test_no_health_monitor_returns_none(self) -> None:
        """No health monitor → health_info is None."""
        orch = _make_orchestrator(health_monitor=None)

        snapshot = orch.get_modem_data()

        assert snapshot.health_info is None

    def test_no_health_info_on_failure(self) -> None:
        """Failed collection still reads health_info (probes are independent)."""
        health_monitor = MagicMock()
        health_monitor.latest = None
        collector = _mock_collector(_fail_result(CollectorSignal.CONNECTIVITY))
        orch = _make_orchestrator(collector=collector, health_monitor=health_monitor)

        snapshot = orch.get_modem_data()

        assert snapshot.health_info is None


# ==================================================================
# Counter-Reset Detection (#110)
# ==================================================================


def _make_channels_with_errors(
    corrected: int = 100,
    uncorrected: int = 10,
) -> dict[str, Any]:
    """Build ModemData with SC-QAM error totals in ``system_info``.

    ``corrected`` / ``uncorrected`` are the aggregate totals (what the
    parser coordinator would emit), not per-channel values.
    """
    ds = [
        {
            "channel_id": i + 1,
            "frequency": 600 + i * 6,
            "channel_type": "qam",
            "lock_status": "locked",
        }
        for i in range(2)
    ]
    return _make_modem_data(
        downstream=ds,
        upstream=[{"channel_id": 3, "frequency": 30}],
        system_info={
            "firmware": "1.0",
            "total_corrected": corrected,
            "total_uncorrected": uncorrected,
        },
    )


class TestCounterResetDetection:
    """Counter-reset detection for last boot time proxy (#110)."""

    def test_first_poll_no_reset(self) -> None:
        """First poll stores totals but does not detect a reset."""
        data = _make_channels_with_errors(corrected=100, uncorrected=10)
        collector = _mock_collector(_ok_result(data))
        orch = _make_orchestrator(collector=collector)

        snapshot = orch.get_modem_data()

        assert snapshot.stats_last_reset is None

    def test_steady_state_no_reset(self) -> None:
        """Increasing counters do not trigger reset."""
        data1 = _make_channels_with_errors(corrected=100, uncorrected=10)
        data2 = _make_channels_with_errors(corrected=200, uncorrected=20)
        collector = _mock_collector([_ok_result(data1), _ok_result(data2)])
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()
        snapshot = orch.get_modem_data()

        assert snapshot.stats_last_reset is None

    def test_corrected_decrease_triggers_reset(self) -> None:
        """Corrected counter decrease → reset detected."""
        data1 = _make_channels_with_errors(corrected=500, uncorrected=50)
        data2 = _make_channels_with_errors(corrected=0, uncorrected=0)
        collector = _mock_collector([_ok_result(data1), _ok_result(data2)])
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()
        snapshot = orch.get_modem_data()

        assert snapshot.stats_last_reset is not None

    def test_uncorrected_decrease_triggers_reset(self) -> None:
        """Uncorrected counter decrease → reset detected."""
        data1 = _make_channels_with_errors(corrected=100, uncorrected=50)
        data2 = _make_channels_with_errors(corrected=200, uncorrected=0)
        collector = _mock_collector([_ok_result(data1), _ok_result(data2)])
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()
        snapshot = orch.get_modem_data()

        assert snapshot.stats_last_reset is not None

    def test_reset_timestamp_persists(self) -> None:
        """Once set, stats_last_reset stays on subsequent snapshots."""
        data1 = _make_channels_with_errors(corrected=500, uncorrected=50)
        data2 = _make_channels_with_errors(corrected=0, uncorrected=0)
        data3 = _make_channels_with_errors(corrected=10, uncorrected=1)
        collector = _mock_collector([_ok_result(data1), _ok_result(data2), _ok_result(data3)])
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()
        orch.get_modem_data()  # reset detected
        snapshot = orch.get_modem_data()  # counters increasing again

        assert snapshot.stats_last_reset is not None

    def test_no_error_counters_no_detection(self) -> None:
        """Modems without error counters get no detection."""
        data = _make_channels()  # no corrected/uncorrected fields
        collector = _mock_collector([_ok_result(data), _ok_result(data)])
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()
        snapshot = orch.get_modem_data()

        assert snapshot.stats_last_reset is None

    def test_reset_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Counter reset is logged at INFO level."""
        data1 = _make_channels_with_errors(corrected=500, uncorrected=50)
        data2 = _make_channels_with_errors(corrected=0, uncorrected=0)
        collector = _mock_collector([_ok_result(data1), _ok_result(data2)])
        orch = _make_orchestrator(collector=collector)

        with caplog.at_level(logging.INFO):
            orch.get_modem_data()
            orch.get_modem_data()

        assert any("Counter reset detected" in r.message for r in caplog.records)


# ==================================================================
# system_info field-set change detection (P25)
# ==================================================================


class TestSystemInfoFieldsChanged:
    """Detects when parser-level system_info fields appear or disappear."""

    def test_first_poll_no_event(self, caplog: pytest.LogCaptureFixture) -> None:
        """First poll sets baseline; no event fired."""
        data = _make_modem_data(system_info={"firmware": "1.0", "hardware_version": "2"})
        collector = _mock_collector(_ok_result(data))
        orch = _make_orchestrator(collector=collector)

        with caplog.at_level(logging.WARNING):
            orch.get_modem_data()

        assert not any("system_info fields changed" in r.message for r in caplog.records)

    def test_steady_state_no_event(self, caplog: pytest.LogCaptureFixture) -> None:
        """Same field set across polls — no event."""
        data = _make_modem_data(system_info={"firmware": "1.0", "hardware_version": "2"})
        collector = _mock_collector([_ok_result(data), _ok_result(data)])
        orch = _make_orchestrator(collector=collector)

        with caplog.at_level(logging.WARNING):
            orch.get_modem_data()
            orch.get_modem_data()

        assert not any("system_info fields changed" in r.message for r in caplog.records)

    def test_lost_field_fires_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Field disappears → WARNING with lost field name."""
        data1 = _make_modem_data(system_info={"firmware": "1.0", "boot_status": "operational"})
        data2 = _make_modem_data(system_info={"firmware": "1.0"})
        collector = _mock_collector([_ok_result(data1), _ok_result(data2)])
        orch = _make_orchestrator(collector=collector)

        with caplog.at_level(logging.WARNING):
            orch.get_modem_data()
            orch.get_modem_data()

        assert any("system_info fields changed" in r.message and "lost" in r.message for r in caplog.records)
        assert any("boot_status" in r.message for r in caplog.records)

    def test_gained_field_fires_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """New field appears → WARNING with gained field name."""
        data1 = _make_modem_data(system_info={"firmware": "1.0"})
        data2 = _make_modem_data(system_info={"firmware": "1.0", "boot_status": "operational"})
        collector = _mock_collector([_ok_result(data1), _ok_result(data2)])
        orch = _make_orchestrator(collector=collector)

        with caplog.at_level(logging.WARNING):
            orch.get_modem_data()
            orch.get_modem_data()

        assert any("system_info fields changed" in r.message and "gained" in r.message for r in caplog.records)
        assert any("boot_status" in r.message for r in caplog.records)

    def test_rate_fields_excluded_from_diff(self, caplog: pytest.LogCaptureFixture) -> None:
        """Orchestrator-computed rate_* fields do not trigger a change event."""
        data1 = _make_modem_data(system_info={"firmware": "1.0", "total_corrected": 100, "total_uncorrected": 10})
        data2 = _make_modem_data(system_info={"firmware": "1.0", "total_corrected": 200, "total_uncorrected": 20})
        collector = _mock_collector([_ok_result(data1), _ok_result(data2)])
        orch = _make_orchestrator(collector=collector)

        with caplog.at_level(logging.WARNING):
            orch.get_modem_data()
            orch.get_modem_data()

        # rate_corrected / rate_uncorrected are added by _update_error_stats
        # after the field-set snapshot — they must not appear in the diff.
        assert not any("system_info fields changed" in r.message for r in caplog.records)


# ==================================================================
# Error Rate Computation (#164)
# ==================================================================


def _data_with_totals(corrected: int, uncorrected: int) -> dict[str, Any]:
    """Build modem_data carrying SC-QAM error totals in ``system_info``.

    The orchestrator reads ``system_info["total_corrected"]`` /
    ``total_uncorrected`` — the parser coordinator's aggregate output,
    scoped to SC-QAM per the YAML ``aggregate`` declaration. Tests
    feed totals at the same layer the parser would, not as raw
    per-channel fields.
    """
    return _make_modem_data(
        downstream=[
            {
                "channel_id": 1,
                "frequency": 600,
                "channel_type": "qam",
                "lock_status": "locked",
                "corrected": corrected,
                "uncorrected": uncorrected,
            }
        ],
        upstream=[{"channel_id": 1, "frequency": 30}],
        system_info={
            "firmware": "1.0",
            "total_corrected": corrected,
            "total_uncorrected": uncorrected,
        },
    )


def _patch_orch_monotonic(monkeypatch: pytest.MonkeyPatch, values: list[float]) -> None:
    """Patch time.monotonic() to return the given values in sequence.

    After the list is exhausted, the last value repeats. Time is a global
    module attribute, so unrelated callers (pytest fixtures, recovery,
    health monitor) also see the patch — clamping prevents StopIteration
    from incidental calls outside the orchestrator's poll path.
    """
    from solentlabs.cable_modem_monitor_core.orchestration import orchestrator as orch_mod

    state = {"idx": 0}

    def fake_monotonic() -> float:
        idx = state["idx"]
        state["idx"] = idx + 1
        return values[idx] if idx < len(values) else values[-1]

    monkeypatch.setattr(orch_mod.time, "monotonic", fake_monotonic)


def _run_polls(
    monkeypatch: pytest.MonkeyPatch,
    monotonic: list[float],
    *poll_data: dict[str, Any],
) -> ModemSnapshot:
    """Patch the clock, build a collector from successive poll dicts, run
    N polls, return the final snapshot.

    Each test's "interesting" inputs (monotonic sequence + poll data) stay
    visible at the call site; orchestrator + collector wiring is hidden.

    Args:
        monotonic: Values returned by ``time.monotonic()`` in order
            (last value clamps if exhausted). Three calls per
            ``get_modem_data()`` roundtrip — see ``_patch_orch_monotonic``.
        poll_data: One ``modem_data`` dict per scheduled poll, in order.
    """
    _patch_orch_monotonic(monkeypatch, monotonic)
    collector = _mock_collector([_ok_result(d) for d in poll_data])
    orch = _make_orchestrator(collector=collector)
    snapshot: ModemSnapshot | None = None
    for _ in poll_data:
        snapshot = orch.get_modem_data()
    assert snapshot is not None, "poll_data must contain at least one entry"
    return snapshot


class TestErrorRateComputation:
    """Per-minute error rate fields in system_info (#164)."""

    # Table-driven: each row is a "compute rate" scenario between two
    # successful polls. Expected rate = delta_counts / delta_seconds * 60.
    # Both counters share the same delta_seconds; we test corrected and
    # uncorrected in the same row to exercise the dual computation.
    @pytest.mark.parametrize(
        ("prev_c", "prev_u", "cur_c", "cur_u", "delta_s", "expected_c", "expected_u"),
        [
            pytest.param(100, 10, 300, 30, 60.0, 200.0, 20.0, id="standard_60s_interval"),
            pytest.param(100, 0, 340, 0, 120.0, 120.0, 0.0, id="scan_interval_120s_uncorrected_flat"),
            pytest.param(10, 0, 11, 0, 600.0, 0.1, 0.0, id="fractional_long_interval"),
            pytest.param(500, 50, 500, 50, 30.0, 0.0, 0.0, id="zero_delta_emits_zero_signal"),
            pytest.param(0, 0, 6, 0, 60.0, 6.0, 0.0, id="counter_starts_at_zero"),
        ],
    )
    def test_rate_computation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        prev_c: int,
        prev_u: int,
        cur_c: int,
        cur_u: int,
        delta_s: float,
        expected_c: float,
        expected_u: float,
    ) -> None:
        """Second poll emits rate = delta_counts / delta_seconds * 60.

        Rate uses monotonic elapsed time, not configured scan_interval —
        the orchestrator has no knowledge of HA scheduling. Zero deltas
        emit 0.0 explicitly (real signal, not absence of data).
        """
        snapshot = _run_polls(
            monkeypatch,
            [0.0, 0.0, 0.0, delta_s, delta_s, delta_s],
            _data_with_totals(prev_c, prev_u),
            _data_with_totals(cur_c, cur_u),
        )

        assert snapshot.modem_data is not None
        system_info = snapshot.modem_data["system_info"]
        assert system_info["rate_corrected"] == pytest.approx(expected_c)
        assert system_info["rate_uncorrected"] == pytest.approx(expected_u)

    # Zero-floor rule: cur == 0 implies rate == 0 by definition.
    # Each counter is decided independently. Table covers first-poll
    # partials and after-reset-to-zero.
    @pytest.mark.parametrize(
        ("cur_c", "cur_u", "expected_c", "expected_u"),
        [
            pytest.param(0, 0, 0.0, 0.0, id="both_zero_emit_zero_rates"),
            pytest.param(0, 5, 0.0, None, id="only_corrected_zero"),
            pytest.param(5, 0, None, 0.0, id="only_uncorrected_zero"),
        ],
    )
    def test_first_poll_zero_floor_per_counter(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cur_c: int,
        cur_u: int,
        expected_c: float | None,
        expected_u: float | None,
    ) -> None:
        """First poll with a zero total emits rate=0 for that counter.

        Zero floor applies independently per counter: a counter at zero
        implies a zero rate (no errors → no rate of errors), regardless
        of poll number. A non-zero counter without a baseline stays
        absent (unknown).
        """
        snapshot = _run_polls(
            monkeypatch,
            [0.0, 0.0, 0.0],
            _data_with_totals(cur_c, cur_u),
        )

        assert snapshot.modem_data is not None
        system_info = snapshot.modem_data["system_info"]
        if expected_c is None:
            assert "rate_corrected" not in system_info
        else:
            assert system_info["rate_corrected"] == pytest.approx(expected_c)
        if expected_u is None:
            assert "rate_uncorrected" not in system_info
        else:
            assert system_info["rate_uncorrected"] == pytest.approx(expected_u)

    # Reset table: prev_c/prev_u → cur_c/cur_u after a decrease in at least
    # one counter.  expected_c/expected_u are the rate field outcomes:
    #   float  → field present with that value (zero floor fired)
    #   None   → field absent (inter-poll delta short-circuited by reset)
    #
    # Zero floor and reset detection are evaluated independently per
    # counter, so mixed outcomes (one present, one absent) are valid.
    @pytest.mark.parametrize(
        ("prev_c", "prev_u", "cur_c", "cur_u", "expected_c", "expected_u"),
        [
            pytest.param(500, 50, 10, 1, None, None, id="both_decrease_nonzero"),
            pytest.param(500, 50, 0, 0, 0.0, 0.0, id="both_reset_to_zero"),
            pytest.param(500, 50, 10, 50, None, None, id="only_corrected_decreases"),
            pytest.param(500, 50, 500, 1, None, None, id="only_uncorrected_decreases"),
            pytest.param(500, 50, 0, 50, 0.0, None, id="corrected_to_zero_uncorrected_stable"),
            pytest.param(500, 50, 500, 0, None, 0.0, id="corrected_stable_uncorrected_to_zero"),
        ],
    )
    def test_counter_reset_scenarios(
        self,
        monkeypatch: pytest.MonkeyPatch,
        prev_c: int,
        prev_u: int,
        cur_c: int,
        cur_u: int,
        expected_c: float | None,
        expected_u: float | None,
    ) -> None:
        """Counter decrease in either field triggers reset detection.

        Reset detection fires when ``cur < prev`` on either counter,
        recording ``stats_last_reset`` and short-circuiting the
        inter-poll delta for both counters.  The zero floor runs first
        and is independent: a counter that lands at zero emits 0.0
        even when the reset short-circuit fires.
        """
        snapshot = _run_polls(
            monkeypatch,
            [0.0, 0.0, 0.0, 60.0, 60.0, 60.0],
            _data_with_totals(prev_c, prev_u),
            _data_with_totals(cur_c, cur_u),
        )

        assert snapshot.modem_data is not None
        system_info = snapshot.modem_data["system_info"]
        if expected_c is None:
            assert "rate_corrected" not in system_info
        else:
            assert system_info["rate_corrected"] == pytest.approx(expected_c)
        if expected_u is None:
            assert "rate_uncorrected" not in system_info
        else:
            assert system_info["rate_uncorrected"] == pytest.approx(expected_u)
        assert snapshot.stats_last_reset is not None

    def test_first_poll_nonzero_omits_rate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """First poll with non-zero totals: no prior baseline, rate absent.

        Complement to ``test_first_poll_zero_floor_per_counter`` — the two
        partition first-poll behavior on whether each total is zero.
        """
        snapshot = _run_polls(
            monkeypatch,
            [0.0, 0.0, 0.0],
            _data_with_totals(100, 10),
        )

        assert snapshot.modem_data is not None
        system_info = snapshot.modem_data["system_info"]
        assert "rate_corrected" not in system_info
        assert "rate_uncorrected" not in system_info

    def test_nonpositive_delta_seconds_omits_rate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Monotonic clock returning equal values → defensively omit rate.

        Clock skew, paused VM, or container sleep can produce a
        non-positive elapsed time; emitting a rate from that would
        produce a divide-by-zero or negative-rate spike.
        """
        snapshot = _run_polls(
            monkeypatch,
            [0.0] * 6,
            _data_with_totals(100, 10),
            _data_with_totals(200, 20),
        )

        assert snapshot.modem_data is not None
        system_info = snapshot.modem_data["system_info"]
        assert "rate_corrected" not in system_info
        assert "rate_uncorrected" not in system_info

    def test_no_error_counters_no_rate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Modems without an SC-QAM error aggregate expose no rate fields.

        ``_update_error_stats`` reads ``system_info["total_corrected"]``
        / ``total_uncorrected`` (the parser coordinator's aggregate
        output). When either is absent, the method short-circuits
        without writing rate fields or updating prior-state.
        """
        snapshot = _run_polls(
            monkeypatch,
            [0.0, 0.0, 0.0, 60.0, 60.0, 60.0],
            _make_channels(),
            _make_channels(),  # no corrected/uncorrected fields on either poll
        )

        assert snapshot.modem_data is not None
        system_info = snapshot.modem_data["system_info"]
        assert "rate_corrected" not in system_info
        assert "rate_uncorrected" not in system_info

    def test_multi_poll_continuity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Rate computation continues correctly across three successful polls.

        Pins the state-update invariant: ``_prev_error_baseline`` must be
        advanced after each poll so the third poll's rate is based on the
        second poll's baseline, not the first poll's.

        Poll 1: 100/10 at t=0     → no rate (no baseline)
        Poll 2: 200/20 at t=60s   → rate = (100,10)/60*60 = 100/min, 10/min
        Poll 3: 320/24 at t=120s  → rate = (120,4)/60*60 = 120/min, 4/min
                                    (NOT based on poll 1, which would give
                                    (220,14)/120*60 = 110/min, 7/min)
        """
        # Run polls 1 and 2 to set up, then assert on poll 2 and poll 3.
        # _run_polls only returns the last snapshot, so we run twice.
        _patch_orch_monotonic(
            monkeypatch,
            [0.0, 0.0, 0.0, 60.0, 60.0, 60.0, 120.0, 120.0, 120.0],
        )
        collector = _mock_collector(
            [
                _ok_result(_data_with_totals(100, 10)),
                _ok_result(_data_with_totals(200, 20)),
                _ok_result(_data_with_totals(320, 24)),
            ]
        )
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()
        snap2 = orch.get_modem_data()
        snap3 = orch.get_modem_data()

        assert snap2.modem_data is not None
        assert snap2.modem_data["system_info"]["rate_corrected"] == pytest.approx(100.0)
        assert snap2.modem_data["system_info"]["rate_uncorrected"] == pytest.approx(10.0)

        assert snap3.modem_data is not None
        assert snap3.modem_data["system_info"]["rate_corrected"] == pytest.approx(120.0)
        assert snap3.modem_data["system_info"]["rate_uncorrected"] == pytest.approx(4.0)

    def test_rate_resumes_after_reset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """After a reset poll, rate resumes on the next poll using the
        post-reset value as the new baseline.

        Poll 1: 500/50 at t=0     → no rate (no baseline)
        Poll 2: 10/1  at t=60s    → reset detected, rate OMITTED; baseline
                                    advances to (10, 1) at t=60
        Poll 3: 70/7  at t=120s   → rate = (60, 6)/60*60 = 60/min, 6/min
                                    (uses post-reset baseline, not pre-reset)
        """
        # Need intermediate snapshots (poll 2 asserts reset state) so we
        # drive the orchestrator directly here rather than via _run_polls.
        _patch_orch_monotonic(
            monkeypatch,
            [0.0, 0.0, 0.0, 60.0, 60.0, 60.0, 120.0, 120.0, 120.0],
        )
        collector = _mock_collector(
            [
                _ok_result(_data_with_totals(500, 50)),
                _ok_result(_data_with_totals(10, 1)),
                _ok_result(_data_with_totals(70, 7)),
            ]
        )
        orch = _make_orchestrator(collector=collector)

        orch.get_modem_data()
        snap_reset = orch.get_modem_data()
        snap_after = orch.get_modem_data()

        assert snap_reset.modem_data is not None
        reset_info = snap_reset.modem_data["system_info"]
        assert "rate_corrected" not in reset_info
        assert "rate_uncorrected" not in reset_info
        assert snap_reset.stats_last_reset is not None

        assert snap_after.modem_data is not None
        after_info = snap_after.modem_data["system_info"]
        assert after_info["rate_corrected"] == pytest.approx(60.0)
        assert after_info["rate_uncorrected"] == pytest.approx(6.0)

    def test_ofdm_channels_excluded_from_rate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression for #164: OFDM channel error counts must not contaminate rates.

        The parser coordinator scopes ``total_corrected`` /
        ``total_uncorrected`` to SC-QAM via the YAML ``aggregate``
        declaration. The orchestrator must source from those
        ``system_info`` fields rather than re-summing
        ``corrected``/``uncorrected`` across every channel in
        ``downstream`` — the earlier implementation did the latter,
        which silently pulled in OFDM codewords. OFDM and SC-QAM
        codeword counters use different FEC chains (LDPC+BCH vs
        Reed-Solomon) and OFDM has asynchronous per-profile
        discontinuities; the two are not comparable.

        This test makes the bypass observable: OFDM channels carry
        large ``corrected`` / ``uncorrected`` values that would
        dominate any unscoped sum, but the rate must match the
        ``system_info`` totals (SC-QAM only).
        """

        def with_qam_aggregate_and_ofdm_noise(total_c: int, total_u: int, ofdm_c: int, ofdm_u: int) -> dict[str, Any]:
            return _make_modem_data(
                downstream=[
                    {
                        "channel_id": 1,
                        "channel_type": "qam",
                        "lock_status": "locked",
                        "corrected": total_c,
                        "uncorrected": total_u,
                    },
                    {
                        "channel_id": 33,
                        "channel_type": "ofdm",
                        "lock_status": "locked",
                        "corrected": ofdm_c,
                        "uncorrected": ofdm_u,
                    },
                ],
                system_info={
                    "firmware": "1.0",
                    "total_corrected": total_c,
                    "total_uncorrected": total_u,
                },
            )

        snapshot = _run_polls(
            monkeypatch,
            [0.0, 0.0, 0.0, 60.0, 60.0, 60.0],
            with_qam_aggregate_and_ofdm_noise(100, 10, 999_999, 999_999),
            with_qam_aggregate_and_ofdm_noise(200, 20, 999_999_999, 999_999_999),
        )

        assert snapshot.modem_data is not None
        system_info = snapshot.modem_data["system_info"]
        # Rate reflects SC-QAM aggregate delta (100/60s, 10/60s × 60),
        # not the OFDM channel's much larger counter delta.
        assert system_info["rate_corrected"] == pytest.approx(100.0)
        assert system_info["rate_uncorrected"] == pytest.approx(10.0)


# ==================================================================
# Health Recovery Clears Connectivity Backoff
# ==================================================================


def _make_health_monitor(status_value: str) -> MagicMock:
    """Build a mock HealthMonitor with the given health status.

    ``latest_probe_at`` defaults to ``float("inf")`` so the
    orchestrator's freshness check in the health-recovery-clears-
    backoff shortcut always treats the mock reading as recent.
    Stale-probe tests override this explicitly.

    Args:
        status_value: HealthStatus enum value string (e.g., "responsive").
    """
    from solentlabs.cable_modem_monitor_core.orchestration.models import HealthInfo
    from solentlabs.cable_modem_monitor_core.orchestration.signals import HealthStatus

    health_status = HealthStatus(status_value)
    monitor = MagicMock()
    monitor.latest = HealthInfo(health_status=health_status)
    monitor.latest_probe_at = float("inf")
    return monitor


# Table-driven: each row is (health_status_value, has_monitor, expect_clear, description)
#
# ┌──────────────────┬─────────────┬──────────────┬─────────────────────────────┐
# │ health_status    │ has_monitor │ expect_clear │ description                 │
# ├──────────────────┼─────────────┼──────────────┼─────────────────────────────┤
# │ responsive       │ yes         │ yes          │ recovery clears backoff     │
# │ unresponsive     │ yes         │ no           │ still down                  │
# │ degraded         │ yes         │ no           │ HTTP probe failing          │
# │ icmp_blocked     │ yes         │ yes          │ TCP verified up (UC-59a)    │
# │ unknown          │ yes         │ no           │ no probe data               │
# │ (none)           │ no          │ no           │ no health monitor           │
# └──────────────────┴─────────────┴──────────────┴─────────────────────────────┘
#
# icmp_blocked clears since the ICMP contradiction override (UC-59a):
# every ICMP_BLOCKED reading is confirmed by a live TCP pass, never
# assumed from stale collection evidence — the "conservative for v1"
# rationale no longer applies. Observed live 2026-07-12: the old
# behavior left a reachable modem in backoff for a full scan interval
# after an outage resolved through icmp_blocked.
#
# fmt: off
HEALTH_RECOVERY_CASES = [
    ("responsive",   True,  True,  "recovery_clears_backoff"),
    ("unresponsive", True,  False, "still_down"),
    ("degraded",     True,  False, "http_probe_failing"),
    ("icmp_blocked", True,  True,  "tcp_verified_up_uc59a"),
    ("unknown",      True,  False, "no_probe_data"),
    ("responsive",   False, False, "no_health_monitor"),
]
# fmt: on


class TestHealthRecoveryClearsBackoff:
    """UC-36: Health recovery clears connectivity backoff.

    When the health monitor reports a data-path-up status (RESPONSIVE
    or ICMP_BLOCKED) while connectivity backoff is active, the
    orchestrator clears the backoff and the poll proceeds immediately.
    """

    @pytest.mark.parametrize(
        "health_status, has_monitor, expect_clear, desc",
        HEALTH_RECOVERY_CASES,
        ids=[c[3] for c in HEALTH_RECOVERY_CASES],
    )
    def test_health_recovery_during_backoff(
        self,
        health_status: str,
        has_monitor: bool,
        expect_clear: bool,
        desc: str,
    ) -> None:
        """Backoff is cleared only when health proves the data path up."""
        from solentlabs.cable_modem_monitor_core.orchestration.models import HealthInfo
        from solentlabs.cable_modem_monitor_core.orchestration.signals import HealthStatus

        # Build collector: two failures to reach backoff=2, then success
        collector = _mock_collector(
            [
                _fail_result(CollectorSignal.CONNECTIVITY),
                _fail_result(CollectorSignal.CONNECTIVITY),
                _ok_result(),
            ]
        )
        # Start unresponsive so health doesn't interfere while building backoff
        health_monitor = _make_health_monitor("unresponsive") if has_monitor else None
        orch = _make_orchestrator(collector=collector, health_monitor=health_monitor)

        # Poll 1: CONNECTIVITY → streak=1, backoff=1
        orch.get_modem_data()
        # Poll 2: backoff=1 clears → CONNECTIVITY → streak=2, backoff=2
        orch.get_modem_data()
        assert orch.diagnostics().connectivity_backoff_remaining == 2

        # Switch to the parameterised health status for the test poll
        if health_monitor is not None:
            health_monitor.latest = HealthInfo(
                health_status=HealthStatus(health_status),
            )

        # Poll 3: health recovery may clear backoff before the check
        snapshot = orch.get_modem_data()

        if expect_clear:
            # Health cleared the backoff → collector ran → success
            assert snapshot.connection_status == ConnectionStatus.ONLINE
            assert orch.diagnostics().connectivity_streak == 0
            assert orch.diagnostics().connectivity_backoff_remaining == 0
            assert collector.execute.call_count == 3
        else:
            # Backoff was not cleared → poll skipped (2→1)
            assert snapshot.connection_status == ConnectionStatus.UNREACHABLE
            assert collector.execute.call_count == 2

    def test_health_responsive_no_backoff_is_noop(self) -> None:
        """RESPONSIVE health with no active backoff does not affect normal flow."""
        health_monitor = _make_health_monitor("responsive")
        collector = _mock_collector([_ok_result()])
        orch = _make_orchestrator(collector=collector, health_monitor=health_monitor)

        snapshot = orch.get_modem_data()

        assert snapshot.connection_status == ConnectionStatus.ONLINE
        assert orch.diagnostics().connectivity_streak == 0
        assert orch.diagnostics().connectivity_backoff_remaining == 0
        assert collector.execute.call_count == 1

    def test_health_recovery_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Health-triggered backoff clear is logged at INFO level."""
        from solentlabs.cable_modem_monitor_core.orchestration.models import HealthInfo
        from solentlabs.cable_modem_monitor_core.orchestration.signals import HealthStatus

        collector = _mock_collector(
            [
                _fail_result(CollectorSignal.CONNECTIVITY),
                _fail_result(CollectorSignal.CONNECTIVITY),
                _ok_result(),
            ]
        )
        # Start unresponsive while building backoff
        health_monitor = _make_health_monitor("unresponsive")
        orch = _make_orchestrator(collector=collector, health_monitor=health_monitor)

        orch.get_modem_data()  # streak=1, backoff=1
        orch.get_modem_data()  # backoff clears → streak=2, backoff=2

        # Switch to responsive for the recovery poll
        health_monitor.latest = HealthInfo(health_status=HealthStatus.RESPONSIVE)

        with caplog.at_level(logging.INFO):
            orch.get_modem_data()  # health recovery clears backoff=2

        assert any("Health recovery" in r.message for r in caplog.records)

    def test_health_recovery_clears_deep_backoff(self) -> None:
        """Health recovery clears even a high-streak backoff (streak=4, backoff=6)."""
        collector = _mock_collector()
        collector.execute.return_value = _fail_result(CollectorSignal.CONNECTIVITY)
        health_monitor = _make_health_monitor("unresponsive")
        orch = _make_orchestrator(collector=collector, health_monitor=health_monitor)

        # Drive to streak=4, backoff=6 (capped from 8):
        # streak=1(bo=1) → clears → streak=2(bo=2) → skip 1 → clears →
        # streak=3(bo=4) → skip 3 → clears → streak=4(bo=6)
        for _ in range(8):
            orch.get_modem_data()

        assert orch.diagnostics().connectivity_backoff_remaining == 6
        assert orch.diagnostics().connectivity_streak == 4

        # Now health recovers — switch to RESPONSIVE and provide success result
        from solentlabs.cable_modem_monitor_core.orchestration.models import HealthInfo
        from solentlabs.cable_modem_monitor_core.orchestration.signals import HealthStatus

        health_monitor.latest = HealthInfo(health_status=HealthStatus.RESPONSIVE)
        collector.execute.return_value = _ok_result()

        snapshot = orch.get_modem_data()

        assert snapshot.connection_status == ConnectionStatus.ONLINE
        assert orch.diagnostics().connectivity_streak == 0
        assert orch.diagnostics().connectivity_backoff_remaining == 0

    def test_stale_responsive_probe_does_not_clear_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cached RESPONSIVE from before the outage must NOT clear backoff.

        Regression guard for the MB7621 hardware observation at
        10:38:53 — the health coordinator runs on a slower cadence
        than the data coordinator during a recovery window, so
        ``health_monitor.latest`` can report pre-outage RESPONSIVE
        while the modem is actually down. The orchestrator's
        freshness check must ignore a probe whose timestamp pre-
        dates the last observed CONNECTIVITY failure.

        Time is mocked across every poll: ``time.monotonic`` is
        host-dependent (seconds since boot) and would otherwise
        return small values on freshly booted CI runners, breaking
        the freshness comparison against the literal probe time.
        """
        import solentlabs.cable_modem_monitor_core.orchestration.orchestrator as orch_module
        from solentlabs.cable_modem_monitor_core.orchestration.models import HealthInfo
        from solentlabs.cable_modem_monitor_core.orchestration.signals import HealthStatus

        # Probe ran at t=100 with RESPONSIVE. Any connectivity
        # failure observed after that is "newer" than the probe.
        health_monitor = MagicMock()
        health_monitor.latest = HealthInfo(health_status=HealthStatus.RESPONSIVE)
        health_monitor.latest_probe_at = 100.0

        collector = _mock_collector()
        collector.execute.return_value = _fail_result(CollectorSignal.CONNECTIVITY)
        orch = _make_orchestrator(collector=collector, health_monitor=health_monitor)

        # Pin orchestrator time to t=200.0 for every poll. The
        # exact value doesn't matter — only that it's > 100.0
        # (the probe time) so the freshness check correctly
        # classifies the cached probe as stale.
        monkeypatch.setattr(orch_module.time, "monotonic", lambda: 200.0)

        # Poll 1: connectivity failure → backoff=1
        orch.get_modem_data()
        assert orch.diagnostics().connectivity_backoff_remaining == 1

        # Poll 2: backoff was 1, decrements to 0, poll runs, fails → backoff=2
        orch.get_modem_data()
        assert orch.diagnostics().connectivity_backoff_remaining == 2

        # Poll 3: backoff>0, health is cached RESPONSIVE, but
        # latest_probe_at (100.0) < _last_connectivity_failure_at
        # (200.0). Shortcut must stay closed — backoff decrements
        # to 1 and the poll is skipped (UNREACHABLE without
        # invoking the collector).
        call_count_before = collector.execute.call_count
        snapshot = orch.get_modem_data()

        assert snapshot.connection_status == ConnectionStatus.UNREACHABLE
        assert collector.execute.call_count == call_count_before
        # Backoff ticked down normally (2 → 1), not cleared.
        assert orch.diagnostics().connectivity_backoff_remaining == 1

    def test_fresh_responsive_probe_still_clears_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A probe that ran AFTER the last failure correctly clears backoff.

        Complement to the stale-probe test: confirms the freshness
        gate doesn't accidentally break the legitimate case. ``time``
        is mocked across every poll for the same host-determinism
        reason as the stale-probe test.
        """
        import solentlabs.cable_modem_monitor_core.orchestration.orchestrator as orch_module
        from solentlabs.cable_modem_monitor_core.orchestration.models import HealthInfo
        from solentlabs.cable_modem_monitor_core.orchestration.signals import HealthStatus

        health_monitor = MagicMock()
        # Initial reading — stale (pre-outage).
        health_monitor.latest = HealthInfo(health_status=HealthStatus.UNRESPONSIVE)
        health_monitor.latest_probe_at = 100.0

        collector = _mock_collector()
        collector.execute.return_value = _fail_result(CollectorSignal.CONNECTIVITY)
        orch = _make_orchestrator(collector=collector, health_monitor=health_monitor)

        # Pin orchestrator time to t=200.0 — failures recorded here
        # land between the stale probe (100.0) and the fresh probe
        # (300.0) introduced below.
        monkeypatch.setattr(orch_module.time, "monotonic", lambda: 200.0)

        # Two failures to build backoff.
        orch.get_modem_data()
        orch.get_modem_data()

        assert orch.diagnostics().connectivity_backoff_remaining == 2

        # A FRESH probe runs (t=300, after the failures at t=200)
        # and reports RESPONSIVE. Next poll should clear backoff.
        health_monitor.latest = HealthInfo(health_status=HealthStatus.RESPONSIVE)
        health_monitor.latest_probe_at = 300.0
        collector.execute.return_value = _ok_result()

        snapshot = orch.get_modem_data()

        assert snapshot.connection_status == ConnectionStatus.ONLINE
        assert orch.diagnostics().connectivity_streak == 0
        assert orch.diagnostics().connectivity_backoff_remaining == 0


# fmt: off
# (failure_at, has_monitor, probe_at, expected, desc)
HEALTH_PROBE_FRESHNESS_CASES: list[tuple[float | None, bool, float | None, bool, str]] = [
    (None,  True,  100.0, True,  "no_failure_yet_with_probe"),
    (None,  False, None,  True,  "no_failure_yet_no_monitor"),
    (200.0, False, None,  False, "failure_but_no_monitor"),
    (200.0, True,  None,  False, "failure_but_no_probe_yet"),
    (200.0, True,  100.0, False, "probe_older_than_failure"),
    (200.0, True,  200.0, False, "probe_equal_to_failure"),
    (200.0, True,  300.0, True,  "probe_newer_than_failure"),
]
# fmt: on


class TestHealthProbeFreshness:
    """Pure-unit coverage for ``Orchestrator._is_health_probe_fresh``.

    The freshness gate protects the "health recovery clears backoff"
    shortcut from a stale cached RESPONSIVE probe. The logic has a
    small branch matrix driven by three inputs:

    - ``_last_connectivity_failure_at`` (None → nothing to gate against)
    - ``_health_monitor`` (None → cannot verify freshness)
    - ``health_monitor.latest_probe_at`` (None → no probe yet)

    Integration coverage via :class:`TestHealthRecoveryClearsBackoff`
    exercises the orchestrator-level behaviour; this table pins the
    helper's branch outcomes directly.
    """

    @pytest.mark.parametrize(
        "failure_at, has_monitor, probe_at, expected, desc",
        HEALTH_PROBE_FRESHNESS_CASES,
        ids=[c[4] for c in HEALTH_PROBE_FRESHNESS_CASES],
    )
    def test_freshness_branches(
        self,
        failure_at: float | None,
        has_monitor: bool,
        probe_at: float | None,
        expected: bool,
        desc: str,
    ) -> None:
        """Each row in the matrix returns the documented outcome."""
        if has_monitor:
            health_monitor: Any = MagicMock()
            health_monitor.latest_probe_at = probe_at
        else:
            health_monitor = None

        orch = _make_orchestrator(health_monitor=health_monitor)
        orch._last_connectivity_failure_at = failure_at

        assert orch._is_health_probe_fresh() is expected
