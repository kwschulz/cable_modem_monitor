"""Restart action — one-shot command dispatch.

``run_restart`` sends the reboot instruction, clears the collector
session, and triggers a recovery window. It does not wait for the
modem to come back, probe for liveness, or observe the reboot —
post-reboot polling cadence belongs to the recovery module.

See ORCHESTRATION_SPEC.md § Restart Action for the full contract.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from .actions import execute_action
from .events import RestartCommandFailed, RestartCommandSent
from .logging import log_event
from .models import RestartResult

if TYPE_CHECKING:
    from ..models.modem_config.config import ModemConfig
    from .collector import ModemDataCollector
    from .recovery import Recovery

_logger = logging.getLogger(__name__)


class RestartNotSupportedError(Exception):
    """Modem does not declare actions.restart in modem.yaml."""


def _has_action_auth(restart_action: object) -> bool:
    """Return True if the restart action is an HttpAction with action_auth set."""
    from ..models.modem_config.actions import HttpAction

    return isinstance(restart_action, HttpAction) and restart_action.action_auth is not None


def run_restart(
    collector: ModemDataCollector,
    modem_config: ModemConfig,
    recovery: Recovery,
) -> RestartResult:
    """Send the reboot command and trigger a recovery window.

    Procedure:

    1. Raise ``RestartNotSupportedError`` if ``actions.restart`` is
       None.
    2. Establish the monitoring session via ``collector.authenticate()``
       — unless ``actions.restart`` has ``action_auth`` set, in which
       case ``execute_action`` authenticates on a separate fresh session
       and the monitoring session is not needed for the restart command.
    3. Execute the restart action, and stop if it reports failure.
    4. Clear the collector session (forces fresh auth on the next
       poll — some firmware invalidates sessions after a reboot).
    5. Call ``recovery.begin("restart_command")`` so subsequent polls
       run at recovery cadence.
    6. Return a ``RestartResult``.

    Typical duration: 2–5 seconds. The caller does not block on the
    reboot itself. Any exception between steps 2 and 4, or a failed
    ``ActionResult`` from step 3, yields
    ``RestartResult(success=False, error="command_failed")`` — the
    only error token this function emits.
    """
    # Step 1 — capability guard. Buttons that can't exist as HA
    # entities still reach here via service calls and tests.
    actions = modem_config.actions
    if actions is None or actions.restart is None:
        raise RestartNotSupportedError("Modem does not declare actions.restart")

    start = time.monotonic()
    model = modem_config.model

    # Steps 2–4 are wrapped in one try/except. Any raise inside maps
    # to the single ``command_failed`` token; the reboot didn't
    # dispatch cleanly and the caller should see it as a failed
    # command, not as a nuanced taxonomy of why.
    try:
        # Step 2 — authenticate. Bypass the circuit breaker: the user
        # asked for a restart, so a recent bad-credentials streak
        # shouldn't block the command.
        #
        # Skip when action_auth is set — execute_action will authenticate
        # on a separate fresh session for the action. Establishing the
        # monitoring session here is unnecessary: it is not used for the
        # restart command (e.g., Hub 5 has no monitoring auth at all).
        if not _has_action_auth(actions.restart):
            auth_result = collector.authenticate()
            if not auth_result.success:
                elapsed = time.monotonic() - start
                log_event(
                    _logger,
                    RestartCommandFailed(model=model, reason=f"auth failed — {auth_result.error}"),
                )
                return RestartResult(
                    success=False,
                    elapsed_seconds=elapsed,
                    error="command_failed",
                )

        # Step 3 — execute the reboot action. Connection errors /
        # timeouts inside the HTTP executor are already swallowed
        # there (the modem IS rebooting during the POST); anything
        # that surfaces here is a genuine dispatch failure.
        #
        # A returned failure is one too: per-action auth can be refused,
        # or the modem can answer the command 401/404. Neither raises,
        # and neither rebooted anything (#82).
        action_result = execute_action(collector, modem_config, actions.restart)
        if not action_result.success:
            elapsed = time.monotonic() - start
            log_event(_logger, RestartCommandFailed(model=model, reason=action_result.message))
            # No command reached the modem, so the monitoring session is
            # still good and there is no reboot for recovery to watch.
            return RestartResult(
                success=False,
                elapsed_seconds=elapsed,
                error="command_failed",
            )

        # Step 4 — clear the session locally. MB7621-class firmware
        # invalidates sessions server-side during the reboot while
        # our cookie still looks valid; clearing now forces the next
        # poll to re-auth fresh instead of tripping LOAD_AUTH.
        collector.clear_session()
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - start
        log_event(_logger, RestartCommandFailed(model=model, reason=str(exc)))
        return RestartResult(
            success=False,
            elapsed_seconds=elapsed,
            error="command_failed",
        )

    elapsed = time.monotonic() - start
    log_event(_logger, RestartCommandSent(model=model, elapsed_seconds=elapsed))

    # Step 5 — hand off to Recovery. begin() fires the observer so
    # HA's cadence listener drops the data-coordinator interval;
    # from here on, post-reboot observation is polling's job.
    recovery.begin("restart_command")

    # Step 6 — report. success=True iff steps 2–4 all completed.
    return RestartResult(
        success=True,
        elapsed_seconds=elapsed,
    )
