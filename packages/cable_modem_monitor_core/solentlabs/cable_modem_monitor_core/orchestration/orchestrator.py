"""Orchestrator — policy engine for modem data collection.

Coordinates ModemDataCollector, HealthMonitor, and Recovery.
Delegates signal policy to SignalPolicy, status derivation to pure
functions, and restart dispatch to :func:`run_restart`.

Consumers call get_modem_data() when they want data. The orchestrator
applies backoff and lockout protection regardless of why it was called.
No scheduling or threads — consumers manage their own cadence.

See ORCHESTRATION_SPEC.md for interface contracts and
RUNTIME_POLLING_SPEC.md for behavioral rules.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .auth_stop import auth_stop_advice
from .events import (
    CircuitBreakerPollingBlocked,
    ConnectivityBackoffReset,
    CounterReset,
    HealthBackoffCleared,
    SessionRetryStarted,
    SessionRetrySucceeded,
    StatusTransition,
    SystemInfoFieldsChanged,
)
from .logging import log_event
from .models import ModemSnapshot, OrchestratorDiagnostics, RestartResult
from .policy import SignalPolicy
from .recovery import Recovery
from .restart import RestartNotSupportedError, run_restart
from .signals import (
    CollectorSignal,
    ConnectionStatus,
    DocsisStatus,
)
from .status import derive_connection_status, enrich_docsis_status

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..models.modem_config.config import ModemConfig
    from .collector import ModemDataCollector
    from .models import HealthInfo, ModemResult
    from .modem_health import HealthMonitor

_logger = logging.getLogger(__name__)

# Re-export so existing imports (tests, HA) keep working.
__all__ = ["Orchestrator", "RestartNotSupportedError"]


@dataclass(frozen=True)
class _ErrorRateBaseline:
    """Prior-poll state for inter-poll error rate computation (#164).

    Always updated and cleared as a unit — see _update_error_stats.
    """

    corrected: int
    uncorrected: int
    monotonic: float


class Orchestrator:
    """Policy engine for modem data collection.

    Coordinates the collector with backoff, circuit breaker, and status
    derivation. Exposes a synchronous API — consumers wrap it for their
    platform's scheduling model.

    Args:
        collector: ModemDataCollector instance (reused across polls).
        health_monitor: Optional health probe monitor. None if the
            modem doesn't support ICMP or HTTP HEAD probes.
        modem_config: Parsed modem.yaml config. Used for identity
            (model) and actions (restart, logout).
    """

    AUTH_FAILURE_THRESHOLD: int = 6

    def __init__(
        self,
        collector: ModemDataCollector,
        health_monitor: HealthMonitor | None,
        modem_config: ModemConfig,
    ) -> None:
        self._collector = collector
        self._health_monitor = health_monitor
        self._modem_config = modem_config

        # Policy — signal→policy mapping, auth circuit breaker, and
        # connectivity backoff all live in SignalPolicy.
        self._policy = SignalPolicy(collector, self.AUTH_FAILURE_THRESHOLD, model=modem_config.model)

        # Recovery — owns the polling-cadence window triggered by
        # restart, connectivity outage, or the reboot-signal vote.
        # 2C wires tick/evaluate_snapshot/evaluate_failure into the
        # collection flow; here we only construct it so run_restart
        # has a destination for recovery.begin().
        self._recovery = Recovery(collector=collector, modem_config=modem_config)

        # Last connection status — used to detect UNREACHABLE→ONLINE
        # transitions and emit a single INFO log line.
        self._last_status: ConnectionStatus | None = None

        # First-poll verbose logging — INFO on first poll (every
        # orchestrator reconstruction counts), DEBUG on steady-state
        self._first_poll_complete: bool = False

        # Counter-reset detection (#110) and inter-poll error rates (#164).
        # Both use the same prior-poll baseline; see _update_error_stats.
        self._prev_error_baseline: _ErrorRateBaseline | None = None
        self._stats_last_reset: datetime | None = None

        # Field-set change detection (P25) — parser-level system_info keys
        # after enrich_docsis_status, before _update_error_stats.
        self._prev_system_info_fields: frozenset[str] | None = None

        # Diagnostics state
        self._last_poll_duration: float | None = None
        self._last_poll_at: str | None = None

        # Monotonic timestamp of the last CONNECTIVITY failure. Used
        # by the "health recovery clears connectivity backoff"
        # shortcut in _execute_poll() to avoid trusting a cached
        # data-path-up reading that pre-dates the outage. The health
        # coordinator runs on its own (slower) cadence, so between
        # a modem going down and the next health probe, latest
        # would otherwise report a stale up status.
        self._last_connectivity_failure_at: float | None = None

    def get_modem_data(self) -> ModemSnapshot:
        """Execute a data collection cycle.

        Sequence:
        1. Check circuit breaker — if open, return AUTH_FAILED
        2. Check backoff — if active, decrement and return AUTH_FAILED
        3. Run ModemDataCollector
        4. If collection failed, apply signal policy
        5. On success: reset streak, derive statuses
        6. Detect state transitions
        7. Return ModemSnapshot

        Returns:
            ModemSnapshot with modem data, health info, and derived
            status fields.
        """
        start = time.monotonic()
        self._last_poll_at = datetime.now(UTC).isoformat()

        try:
            snapshot = self._execute_poll()
        finally:
            self._last_poll_duration = time.monotonic() - start

        return snapshot

    def restart(self) -> RestartResult:
        """Send the restart command. One-shot; does not wait.

        Bypasses the auth circuit breaker — the user pressed the
        button, so a recent auth failure should not block the
        restart. Returns quickly (2–5 s): authenticate, dispatch the
        reboot command, clear the session, trigger a recovery window,
        return.

        Does NOT observe the reboot. The snapshot stream surfaces
        real modem state (UNREACHABLE → ranging → ONLINE) as normal
        polling runs at recovery cadence.

        Returns:
            RestartResult — ``success`` is True iff the command
            dispatched cleanly; ``error="command_failed"`` otherwise.

        Raises:
            RestartNotSupportedError: If modem has no restart action.
        """
        return run_restart(self._collector, self._modem_config, self._recovery)

    def reset_connectivity(self) -> None:
        """Reset connectivity backoff for immediate retry.

        Called when the user requests a manual refresh. Clears the
        connectivity backoff so the next get_modem_data() attempts
        a real connection regardless of prior failures.
        """
        was_backing_off = self._policy.connectivity_streak > 0
        self._policy.reset_connectivity()
        if was_backing_off:
            log_event(_logger, ConnectivityBackoffReset(model=self._modem_config.model))

    def close(self) -> None:
        """Release held resources — logs out any live session, closes the collector's HTTP session."""
        self._collector.close()

    def diagnostics(self) -> OrchestratorDiagnostics:
        """Return a read-only snapshot of operational diagnostics.

        No side effects — safe to call at any time.
        """
        auth = self._modem_config.auth
        return OrchestratorDiagnostics(
            poll_duration=self._last_poll_duration,
            auth_failure_streak=self._policy.auth_failure_streak,
            circuit_breaker_open=self._policy.circuit_open,
            circuit_trip_signal=self._policy.circuit_trip_signal,
            session_is_valid=self._collector.session_is_valid,
            auth_strategy=auth.strategy if auth else "",
            connectivity_streak=self._policy.connectivity_streak,
            connectivity_backoff_remaining=self._policy.connectivity_backoff_remaining,
            stale_session_recovery_streak=self._policy.stale_session_recovery_streak,
            session_reuse_disabled=self._policy.session_reuse_disabled,
            resource_fetches=self._collector.last_resource_fetches,
            last_poll_at=self._last_poll_at,
            last_stub_body=self._collector.last_stub_bodies,
            system_info_fields_missing=self._collector.last_system_info_fields_missing,
            system_info_fields_failed=self._collector.system_info_fields_failed,
        )

    @property
    def status(self) -> ConnectionStatus:
        """Current connection status from the last get_modem_data() call.

        Returns UNREACHABLE if never polled.
        """
        if self._last_status is None:
            return ConnectionStatus.UNREACHABLE
        return self._last_status

    @property
    def supports_restart(self) -> bool:
        """Whether the modem declares a restart action in modem.yaml."""
        actions = self._modem_config.actions
        return actions is not None and actions.restart is not None

    @property
    def recovery_active(self) -> bool:
        """Whether a recovery window is currently open.

        True while the :class:`Recovery` module is running an
        aggressive-polling window — triggered by ``restart()``, a
        connectivity outage, or the reboot-signal vote.

        Consumers read this to decide whether to poll at recovery
        cadence. HA subscribes to the observer (see
        :meth:`set_recovery_observer`) for immediate notification
        rather than polling this flag.

        Thread-safe: a boolean read is atomic under the GIL.
        """
        return self._recovery.active

    def set_recovery_observer(self, observer: Callable[[], None] | None) -> None:
        """Register a callback fired on ``recovery_active`` transitions.

        Invoked from the poll thread on both False→True and
        True→False transitions (plus on :meth:`Recovery.begin`
        re-entry while already active — useful for kicking HA's
        ``async_request_refresh`` timing).

        The callback must be thread-safe. HA implementations
        typically hop to the event loop via ``dispatcher_send``
        (which internally uses ``call_soon_threadsafe``).

        Pass ``None`` to clear a previously registered observer.
        """
        # The Recovery instance owns observer dispatch — here we
        # just install the callable. Exceptions raised by the
        # observer are swallowed inside Recovery so they can't
        # break orchestration on the poll thread.
        self._recovery._on_state_change = observer

    # ------------------------------------------------------------------
    # Internal — poll execution
    # ------------------------------------------------------------------

    def _execute_poll(self) -> ModemSnapshot:
        """Run the collection pipeline with policy checks."""
        # Recovery tick — advance the window clock and fire the
        # True→False observer if the deadline just passed. Runs
        # before any short-circuit so the window always closes
        # promptly even when polling is blocked (circuit open,
        # backoff active). Cheap — constant-time no-op when idle.
        self._recovery.tick()

        # Circuit breaker
        if self._policy.circuit_open:
            trip_status_code = self._policy.circuit_trip_status_code
            trip_signal = self._policy.circuit_trip_signal
            log_event(
                _logger,
                CircuitBreakerPollingBlocked(
                    model=self._modem_config.model,
                    status_code=trip_status_code,
                    signal=trip_signal,
                ),
            )
            # Cause and remedy come from one table so this snapshot and
            # the log line above cannot disagree; see auth_stop.py.
            advice = auth_stop_advice(trip_signal, trip_status_code)
            return self._make_snapshot(
                ConnectionStatus.AUTH_FAILED,
                DocsisStatus.UNKNOWN,
                error=f"Circuit breaker open — {advice.cause}",
            )

        # Health recovery — clear connectivity backoff if the modem
        # is proven reachable. ICMP_BLOCKED qualifies: since UC-59a it
        # always carries a live TCP pass, so the data path is proven up
        # even when ping is filtered. The freshness gate matters: the
        # health coordinator is on a slower cadence than the data
        # coordinator during a recovery window, so ``latest`` may still
        # hold a pre-outage reading. Only trust it if the probe ran
        # AFTER our last observed connectivity failure.
        if (
            self._health_monitor is not None
            and self._policy.connectivity_backoff_remaining > 0
            and self._health_monitor.latest.health_status.data_path_up
            and self._is_health_probe_fresh()
        ):
            log_event(_logger, HealthBackoffCleared(model=self._modem_config.model))
            self._policy.reset_connectivity()

        # Connectivity backoff
        if self._policy.check_connectivity_backoff():
            return self._make_snapshot(
                ConnectionStatus.UNREACHABLE,
                DocsisStatus.UNKNOWN,
                error="Connectivity backoff active",
            )

        # Log poll context — INFO on first poll, DEBUG on steady-state
        self._log_poll_context()

        if not self._policy.should_attempt_session_reuse() and self._collector.session_is_valid:
            _logger.debug(
                "Session reuse disabled [%s] — clearing session before poll",
                self._modem_config.model,
            )
            self._collector.clear_session()

        # Notify health monitor — avoids redundant HTTP probe during collection
        if self._health_monitor is not None:
            self._health_monitor.record_collection_start()

        collection_success = False
        load_auth_recovered = False
        try:
            result = self._collector.execute()
            collection_success = result.success

            if not result.success and result.signal in (
                CollectorSignal.LOAD_AUTH,
                CollectorSignal.LOAD_INTEGRITY,
            ):
                result = self._retry_load_auth_once(result.signal)
                collection_success = result.success
                load_auth_recovered = result.success

            if not result.success:
                self._policy.reset_stale_session_recovery_streak()
                return self._handle_failure(result)

            if not load_auth_recovered:
                self._policy.reset_stale_session_recovery_streak()

            self._first_poll_complete = True
            return self._handle_success(result)
        finally:
            if self._health_monitor is not None:
                self._health_monitor.record_collection_end(collection_success)

    def _retry_load_auth_once(self, signal: CollectorSignal) -> ModemResult:
        """Retry one auth-integrity failure immediately with a fresh session.

        Handles both LOAD_AUTH (session expiry, stale auth state) and
        LOAD_INTEGRITY (stub-page response, UC-19a). Both indicate the
        current session is unable to retrieve real data; clearing it
        and re-authenticating in the same poll smooths over the failure
        without waiting for the next scheduled cycle.
        """
        signal_name = signal.value.upper()
        log_event(_logger, SessionRetryStarted(model=self._modem_config.model, signal_name=signal_name))
        self._collector.attempt_logout_before_retry()
        self._collector.clear_session()

        retry_result = self._collector.execute()
        if retry_result.success:
            self._policy.record_stale_session_recovery()
            log_event(_logger, SessionRetrySucceeded(model=self._modem_config.model, signal_name=signal_name))

        return retry_result

    def _handle_failure(self, result: ModemResult) -> ModemSnapshot:
        """Apply signal policy for a failed collection."""
        status = self._policy.apply(result)

        # Freshness watermark — remember when we last saw
        # connectivity actually fail so the health-recovery
        # shortcut in _execute_poll() can distinguish a genuine
        # health-recovery probe from a stale pre-outage reading.
        if result.signal is CollectorSignal.CONNECTIVITY:
            self._last_connectivity_failure_at = time.monotonic()

        # Recovery hook — on CONNECTIVITY failures, Recovery opens a
        # window so HA drops to the faster cadence. Other failure
        # signals (AUTH_FAILED, PARSE_ERROR, LOAD_*) are no-ops
        # inside evaluate_failure — those aren't "modem is
        # rebooting" signals.
        self._recovery.evaluate_failure(result)

        self._detect_transition(status)
        return self._make_snapshot(
            status,
            DocsisStatus.UNKNOWN,
            collector_signal=result.signal,
            error=result.error,
        )

    def _handle_success(self, result: ModemResult) -> ModemSnapshot:
        """Process a successful collection result."""
        self._policy.clear_streak()

        modem_data = result.modem_data
        assert modem_data is not None  # guaranteed by success=True

        # Derive statuses
        connection_status = derive_connection_status(modem_data, model=self._modem_config.model)
        enrich_docsis_status(modem_data)
        docsis_status = modem_data.get("system_info", {}).get("docsis_status", DocsisStatus.UNKNOWN)

        # Field-set change detection (P25) — snapshot after enrich_docsis_status
        # so docsis_status is stable, but before _update_error_stats so
        # orchestrator-derived rate_* fields don't appear in the diff.
        current_fields = frozenset(modem_data.get("system_info", {}))
        if self._prev_system_info_fields is not None and current_fields != self._prev_system_info_fields:
            log_event(
                _logger,
                SystemInfoFieldsChanged(
                    model=self._modem_config.model,
                    gained=current_fields - self._prev_system_info_fields,
                    lost=self._prev_system_info_fields - current_fields,
                ),
            )
        self._prev_system_info_fields = current_fields

        # Counter-reset detection (#110) and per-minute error rates
        # (#164) — both derived from one prior-state read of the
        # SC-QAM error totals. Reset detection updates orchestrator
        # state only; it does NOT feed the reboot-signal vote
        # (that's Recovery's own history). Rate fields are written
        # into modem_data["system_info"] for snapshot consumers.
        self._update_error_stats(modem_data)

        # Recovery hook — runs the reboot-signal vote (may open a
        # window) and always refreshes Recovery's own baselines.
        # When a window is already open, evaluate_snapshot only
        # updates history; the window ticks to completion via
        # tick() at the top of the next poll.
        self._recovery.evaluate_snapshot(modem_data)

        # Read latest health info
        health_info: HealthInfo | None = None
        if self._health_monitor is not None:
            health_info = self._health_monitor.latest

        self._detect_transition(connection_status)

        return self._make_snapshot(
            connection_status,
            docsis_status,
            modem_data=modem_data,
            health_info=health_info,
            collector_signal=CollectorSignal.OK,
            stats_last_reset=self._stats_last_reset,
        )

    # ------------------------------------------------------------------
    # Internal — health freshness
    # ------------------------------------------------------------------

    def _is_health_probe_fresh(self) -> bool:
        """Whether the latest health probe happened after the last
        observed CONNECTIVITY failure.

        Used to gate the "health recovery clears connectivity backoff"
        shortcut so we don't trust a cached data-path-up reading from
        before a modem outage. Returns True when either:

        - No connectivity failure has been observed yet (nothing to
          invalidate against), or
        - A health probe has run since the last failure.
        """
        if self._last_connectivity_failure_at is None:
            return True
        if self._health_monitor is None:
            return False
        probe_at = self._health_monitor.latest_probe_at
        if probe_at is None:
            return False
        return probe_at > self._last_connectivity_failure_at

    # ------------------------------------------------------------------
    # Internal — transition detection
    # ------------------------------------------------------------------

    def _detect_transition(self, new_status: ConnectionStatus) -> None:
        """Log status transitions for diagnostics."""
        old_status = self._last_status
        self._last_status = new_status

        if old_status is not None and old_status != new_status:
            log_event(
                _logger,
                StatusTransition(
                    model=self._modem_config.model,
                    from_status=old_status.value,
                    to_status=new_status.value,
                ),
            )

    # ------------------------------------------------------------------
    # Internal — first-poll verbose logging
    # ------------------------------------------------------------------

    def _log_poll_context(self) -> None:
        """Log auth context before poll. INFO on first poll, DEBUG after."""
        log = _logger.info if not self._first_poll_complete else _logger.debug

        auth = self._modem_config.auth
        strategy = type(auth).__name__ if auth is not None else "none"
        has_creds = bool(self._collector._username or self._collector._password)

        session = "active" if self._collector.session_is_valid else "none"

        log(
            "Poll [%s] — auth: %s, url: %s, credentials: %s, session: %s",
            self._modem_config.model,
            strategy,
            self._collector._base_url,
            "yes" if has_creds else "no",
            session,
        )

    # ------------------------------------------------------------------
    # Internal — counter-reset detection (#110) and error rates (#164)
    # ------------------------------------------------------------------

    def _update_error_stats(self, modem_data: dict[str, Any]) -> None:
        """Detect error counter resets and compute per-minute error rates.

        Reads ``system_info["total_corrected"]`` /
        ``system_info["total_uncorrected"]`` — the parser coordinator's
        aggregate output, scoped per the modem's ``parser.yaml``
        ``aggregate`` declaration (``downstream.qam`` on DOCSIS 3.1,
        ``downstream`` on DOCSIS 3.0-only). The orchestrator must not
        re-derive these from raw channels: doing so would bypass the
        YAML scope and pull in OFDM codeword counters, which are not
        comparable to SC-QAM counters and have asynchronous per-profile
        discontinuities (see
        [PARSING_SPEC.md § Aggregate](../docs/PARSING_SPEC.md#aggregate-derived-system_info-fields)).

        Two derivations share one prior-state read of the totals:

        - **Reset detection (#110):** a decrease in either total
          records ``stats_last_reset`` as a proxy for last boot time.
        - **Error rates (#164):** ``rate_corrected`` / ``rate_uncorrected``
          (errors/min) are written into ``modem_data["system_info"]``
          from the inter-poll delta divided by monotonic elapsed time.

        Each rate field is decided independently. Two ways a rate can
        be reported on a given poll:

        - **Zero floor:** a zero total means a zero rate by definition
          (no errors → no rate of errors). This is true regardless of
          poll number, baseline state, or clock state.
        - **Inter-poll delta:** requires a prior baseline and a
          positive monotonic elapsed time. Omitted on a counter reset
          (the interval spans a discontinuity).

        Otherwise the rate field is omitted (HA renders ``unknown``).
        Prior state is held in ``_prev_error_baseline`` (an
        ``_ErrorRateBaseline`` — corrected, uncorrected, monotonic),
        updated atomically each poll.
        """
        system_info = modem_data.setdefault("system_info", {})
        raw_corrected = system_info.get("total_corrected")
        raw_uncorrected = system_info.get("total_uncorrected")
        if raw_corrected is None or raw_uncorrected is None:
            return  # modem has no SC-QAM aggregate — no rates derivable

        try:
            cur_corrected = int(raw_corrected)
            cur_uncorrected = int(raw_uncorrected)
        except (TypeError, ValueError):
            return  # aggregate present but uncoercible — defensive guard

        current_monotonic = time.monotonic()
        prev = self._prev_error_baseline
        self._prev_error_baseline = _ErrorRateBaseline(cur_corrected, cur_uncorrected, current_monotonic)

        # Zero floor: cur == 0 means rate == 0 by definition. Applies
        # even on the first poll, after a reset, or under bad clock
        # state — no inter-poll baseline is needed to know that zero
        # errors implies zero rate.
        if cur_corrected == 0:
            system_info["rate_corrected"] = 0.0
        if cur_uncorrected == 0:
            system_info["rate_uncorrected"] = 0.0

        if prev is None:
            return  # first poll — no inter-poll delta for non-zero totals

        if cur_corrected < prev.corrected or cur_uncorrected < prev.uncorrected:
            self._stats_last_reset = datetime.now(UTC)
            log_event(
                _logger,
                CounterReset(
                    model=self._modem_config.model,
                    prev_corrected=prev.corrected,
                    cur_corrected=cur_corrected,
                    prev_uncorrected=prev.uncorrected,
                    cur_uncorrected=cur_uncorrected,
                ),
            )
            return  # interval spans a discontinuity — no inter-poll delta

        delta_seconds = current_monotonic - prev.monotonic
        if delta_seconds <= 0:
            return  # clock skew or paused VM — no inter-poll delta

        # Inter-poll delta for non-zero totals. (Counters at zero
        # were already handled by the zero floor above.)
        if cur_corrected > 0:
            system_info["rate_corrected"] = (cur_corrected - prev.corrected) / delta_seconds * 60
        if cur_uncorrected > 0:
            system_info["rate_uncorrected"] = (cur_uncorrected - prev.uncorrected) / delta_seconds * 60

    # ------------------------------------------------------------------
    # Internal — snapshot construction
    # ------------------------------------------------------------------

    def _make_snapshot(
        self,
        connection_status: ConnectionStatus,
        docsis_status: str,
        *,
        modem_data: dict[str, Any] | None = None,
        health_info: HealthInfo | None = None,
        collector_signal: CollectorSignal = CollectorSignal.OK,
        error: str = "",
        stats_last_reset: datetime | None = None,
    ) -> ModemSnapshot:
        """Build a ModemSnapshot with defaults."""
        return ModemSnapshot(
            connection_status=connection_status,
            docsis_status=docsis_status,
            modem_data=modem_data,
            health_info=health_info,
            collector_signal=collector_signal,
            error=error,
            stats_last_reset=stats_last_reset,
        )
