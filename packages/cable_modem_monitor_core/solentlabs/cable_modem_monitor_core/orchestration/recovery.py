"""Recovery — polling-cadence window around modem disruptions.

The modem is in *recovery* when it isn't operating normally and we
believe it may return. Recovery is a cadence concern only: it opens
a bounded window so consumers can switch to a faster poll rate, and
closes the window when the window duration expires. It does not
produce UX state, short-circuit polls, or publish a synthetic
"recovering" label — the snapshot stream always reflects modem
truth.

Three triggers enter the same window:

- ``restart()`` calls :py:meth:`Recovery.begin` after dispatching the
  reboot command.
- The orchestrator's connectivity policy engages on N consecutive
  poll failures — surfaced through :py:meth:`Recovery.evaluate_failure`.
- A 2-of-3 vote over (counter reset, uptime drop, transitional
  docsis) on a successful poll — see
  :py:meth:`Recovery._check_reboot_signals`.

See ORCHESTRATION_SPEC.md § Recovery for the full contract and
ARCHITECTURE_DECISIONS.md § Recovery Architecture for rationale.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from .events import RecoveryObserverException, RecoveryWindowClosed, RecoveryWindowOpened
from .logging import log_event
from .signals import CollectorSignal

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..models.modem_config.config import ModemConfig
    from .models import ModemResult

_logger = logging.getLogger(__name__)

# docsis_status values that indicate the modem is still ranging /
# partially locked — used by the reboot-signal vote. Module-private
# because nothing outside Recovery needs to read it.
_TRANSITIONAL_DOCSIS: frozenset[str] = frozenset({"Denied", "not_locked", "partial_lock"})


class Recovery:
    """Bounded polling-cadence window triggered by modem disruptions.

    Owns:

    - Window state (``active``, ``_started_at``, ``_reason``).
    - Reboot-signal history (``_prev_counters``, ``_prev_uptime``).
    - A thread-safe state-change observer invoked on False→True and
      True→False transitions, plus on :py:meth:`begin` re-entry so
      HA's cadence listener can refresh its immediate-refresh kick.

    The orchestrator calls :py:meth:`tick` on every poll (to advance
    the clock), :py:meth:`evaluate_snapshot` on success, and
    :py:meth:`evaluate_failure` on failure. :py:meth:`begin` is the
    only public entry that always (re)starts the window.
    """

    # Duration of an aggressive-polling window after any recovery
    # trigger. Covers the longest observed DOCSIS 3.1 ranging time
    # with headroom. Class-attribute (not a private module constant)
    # to match the AUTH_FAILURE_THRESHOLD pattern on Orchestrator /
    # SignalPolicy — tunable, discoverable, and test-friendly.
    WINDOW_SECONDS: int = 180

    def __init__(
        self,
        modem_config: ModemConfig,
        *,
        on_state_change: Callable[[], None] | None = None,
    ) -> None:
        self._modem_config = modem_config
        self._on_state_change = on_state_change

        # Window state — the core public answer to "is the modem in
        # a recovery window right now?"
        self._active: bool = False
        self._started_at: float | None = None
        self._reason: str = ""

        # Reboot-signal history (updated every evaluate_snapshot call)
        # so the 2-of-3 vote has baselines to compare against. None
        # until the first successful poll establishes a baseline.
        self._prev_counters: tuple[int, int] | None = None
        self._prev_uptime: int | None = None

        # Last-observed docsis_status from a successful poll. Two uses:
        #   (1) the window-close log line — reported as
        #       "last snapshot docsis" (not "final status"): during
        #       a long outage evaluate_snapshot never runs, so this
        #       reflects the last *successful* poll's value, which
        #       may be stale relative to window-close time.
        #   (2) edge detection for the ``transitional_docsis`` signal —
        #       that signal fires only when docsis just *entered* a
        #       transitional state, so we need the previous reading to
        #       compare against. The initial "unknown" counts as
        #       non-transitional for edge purposes, so a modem that's
        #       already partial_lock on the first poll fires once and
        #       then stays quiet on subsequent polls.
        self._last_docsis_status: str = "unknown"

    @property
    def active(self) -> bool:
        """Whether a recovery window is currently open."""
        return self._active

    def begin(self, reason: str) -> None:
        """Open a recovery window, or re-open an existing one.

        Always (re)starts the window — elapsed clock resets, reason
        is updated. Only ``restart()`` calls this directly; internal
        triggers use their own no-op-when-active logic.

        Fires the state-change observer on False→True transitions and
        on re-entry while already active (HA's cadence listener is
        idempotent and benefits from the refresh kick).
        """
        # Unconditional (re)entry: clock resets, reason updates.
        # Callers passing re-entry (a second restart press) want a
        # fresh full window, not an extension of the old one.
        self._active = True
        self._started_at = time.monotonic()
        self._reason = reason
        log_event(
            _logger,
            RecoveryWindowOpened(
                model=self._modem_config.model,
                reason=reason,
                window_seconds=float(self.WINDOW_SECONDS),
            ),
        )
        # Fire even on re-entry so HA's cadence listener gets a kick
        # to refresh async_request_refresh timing.
        self._fire_observer()

    def evaluate_snapshot(self, modem_data: dict[str, Any]) -> None:
        """Run the reboot-signal check on a successful poll.

        When the 2-of-3 vote fires and no window is active, enters a
        window with ``reason="reboot_signals:<matched>"``. Always
        refreshes reboot-signal history regardless of trigger
        outcome so the next call has current baselines.
        """
        # Tolerant unwrap — system_info absence or wrong type is
        # valid (some parsers don't produce it); we just can't vote.
        system_info: dict[str, Any] = {}
        raw_system_info = modem_data.get("system_info")
        if isinstance(raw_system_info, dict):
            system_info = raw_system_info

        docsis_status = str(system_info.get("docsis_status", "")).strip()

        # Capture the previous docsis BEFORE updating — the
        # transitional_docsis signal is edge-triggered and needs to
        # know what state we were in last poll.
        prev_docsis_status = self._last_docsis_status
        if docsis_status:
            self._last_docsis_status = docsis_status

        # Pull the two numeric signals once; _check_reboot_signals
        # compares them against the stored baselines.
        current_counters = self._extract_counters(system_info)
        current_uptime = self._extract_uptime(system_info)

        # Reboot-signal vote — only runs when no window is already
        # open. Internal triggers must not extend an existing window.
        if not self._active:
            reason = self._check_reboot_signals(
                current_counters=current_counters,
                current_uptime=current_uptime,
                docsis_status=docsis_status,
                prev_docsis_status=prev_docsis_status,
            )
            if reason is not None:
                self._enter_from_internal(reason)

        # History update runs on every call, window open or not —
        # otherwise a window that spans one poll would leave the
        # baseline stale and miss the next reboot.
        self._prev_counters = current_counters
        self._prev_uptime = current_uptime

    def evaluate_failure(self, result: ModemResult) -> None:
        """React to a failed poll.

        Enters a window on ``CONNECTIVITY`` failures when no window is
        already open. Other failure signals (AUTH_FAILED, PARSE_ERROR,
        LOAD_*) are no-ops — those are not "modem is rebooting"
        signals.
        """
        # No-op during an active window (internal triggers never
        # extend). A restart-triggered window running concurrently
        # with an observed outage should keep its original reason.
        if self._active:
            return
        if result.signal is CollectorSignal.CONNECTIVITY:
            self._enter_from_internal("connectivity_outage")

    def tick(self) -> None:
        """Advance the window clock.

        Called by the orchestrator on every poll. When the deadline
        has passed, clears window state and fires the observer on the
        True→False transition.
        """
        # Fast path — nothing to do when the window is closed.
        if not self._active or self._started_at is None:
            return

        # Deadline check. Note: the window runs to completion on
        # purpose; we don't exit early on "looks operational now"
        # because that re-introduces inference (see
        # ARCHITECTURE_DECISIONS.md § Exit).
        elapsed = time.monotonic() - self._started_at
        if elapsed < self.WINDOW_SECONDS:
            return

        # "last snapshot docsis" rather than "final status" — this is
        # the last value seen on a *successful* poll, which may be
        # stale if the window spanned a long outage with no successful
        # polls. The current modem state lives in the snapshot stream,
        # not here.
        log_event(
            _logger,
            RecoveryWindowClosed(
                model=self._modem_config.model,
                elapsed_seconds=elapsed,
                last_docsis_status=self._last_docsis_status,
            ),
        )
        self._active = False
        self._started_at = None
        self._reason = ""
        self._fire_observer()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _enter_from_internal(self, reason: str) -> None:
        """Enter a window from an internal trigger (no-op when active)."""
        if self._active:
            return
        self._active = True
        self._started_at = time.monotonic()
        self._reason = reason
        log_event(
            _logger,
            RecoveryWindowOpened(
                model=self._modem_config.model,
                reason=reason,
                window_seconds=float(self.WINDOW_SECONDS),
            ),
        )
        self._fire_observer()

    def _check_reboot_signals(
        self,
        *,
        current_counters: tuple[int, int] | None,
        current_uptime: int | None,
        docsis_status: str,
        prev_docsis_status: str,
    ) -> str | None:
        """2-of-3 vote over reboot-indicator signals.

        Signals:

        - ``counter_reset`` — ``total_corrected`` or
          ``total_uncorrected`` dropped below the previous poll.
        - ``uptime_drop`` — ``system_uptime`` decreased.
        - ``transitional_docsis`` — ``docsis_status`` just *entered*
          a ranging-like state (Denied / not_locked / partial_lock)
          from a stable state (Operational or unknown). Edge-triggered:
          a modem chronically stuck in partial_lock does NOT fire this
          signal every poll — only on entry.

        Returns the ``"reboot_signals:a+b"`` reason tag when two or
        more signals fire, else ``None``. A single signal is not
        enough — false positives from stats-clear firmware commands
        or clock drift are common.
        """
        fired: list[str] = []

        # Signal 1: error-counter reset. A reboot zeroes totals; a
        # drop in either counter is a strong boot indicator.
        if (
            self._prev_counters is not None
            and current_counters is not None
            and (current_counters[0] < self._prev_counters[0] or current_counters[1] < self._prev_counters[1])
        ):
            fired.append("counter_reset")

        # Signal 2: uptime drop. Only meaningful when the modem
        # reports numeric uptime — _extract_uptime returns None for
        # free-form strings (e.g. "17d 0h 51m"), suppressing this
        # signal rather than guessing.
        if self._prev_uptime is not None and current_uptime is not None and current_uptime < self._prev_uptime:
            fired.append("uptime_drop")

        # Signal 3: transitional DOCSIS state — edge-triggered.
        # Level-triggered (fire every poll while transitional) would
        # false-positive on chronically-unlocked modems when paired
        # with a benign event like a user-initiated stats clear. We
        # only care about the *transition into* a transitional state,
        # which is what actually accompanies a reboot.
        if docsis_status in _TRANSITIONAL_DOCSIS and prev_docsis_status not in _TRANSITIONAL_DOCSIS:
            fired.append("transitional_docsis")

        # Threshold vote (2-of-3). Single signals have too many
        # benign causes (stats clear, clock skew, intermittent
        # lock) — requiring two cuts false positives dramatically.
        if len(fired) >= 2:
            return "reboot_signals:" + "+".join(fired)
        return None

    @staticmethod
    def _extract_counters(system_info: dict[str, Any]) -> tuple[int, int] | None:
        """Read ``total_corrected`` + ``total_uncorrected`` from system_info.

        Returns ``None`` when either field is absent or not coercible.
        """
        corrected = system_info.get("total_corrected")
        uncorrected = system_info.get("total_uncorrected")
        if corrected is None or uncorrected is None:
            return None
        try:
            return int(corrected), int(uncorrected)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_uptime(system_info: dict[str, Any]) -> int | None:
        """Read ``system_uptime`` and coerce to seconds.

        Accepts integer-like strings (e.g. ``"1471890"``). Returns
        ``None`` for absent, non-numeric, or free-form uptime strings
        — without a numeric baseline, we cannot compute a drop.
        """
        raw = system_info.get("system_uptime")
        if raw is None:
            return None
        if isinstance(raw, int):
            return raw
        text = str(raw).strip()
        if not text.isdigit():
            return None
        return int(text)

    def _fire_observer(self) -> None:
        """Invoke the state-change callback, swallowing exceptions.

        The callback may run on the poll thread. Exceptions from
        consumer code must not propagate into orchestration logic.
        """
        if self._on_state_change is None:
            return
        try:
            self._on_state_change()
        except Exception as exc:  # noqa: BLE001
            log_event(
                _logger,
                RecoveryObserverException(
                    model=self._modem_config.model,
                    exc_type=type(exc).__name__,
                ),
            )
