"""What to tell a user when the auth circuit breaker has stopped polling.

One table, three callers: the blocked-poll snapshot error takes the
cause, the two circuit-breaker log events take the remedy. A new trip
cause is a row here, not an edit at every surface.

Contract: ORCHESTRATION_SPEC.md § Auth Circuit Breaker, "Trip reason is
preserved".
"""

from __future__ import annotations

from dataclasses import dataclass

from .signals import CollectorSignal


@dataclass(frozen=True)
class AuthStopAdvice:
    """Why polling stopped, and what resumes it."""

    cause: str
    remedy: str


def auth_stop_advice(
    signal: CollectorSignal | None,
    status_code: int | None,
) -> AuthStopAdvice:
    """Map a circuit-breaker trip reason to what the user is told."""
    # A 404 is checked first because it is the one cause carried as a
    # status rather than a signal: the login endpoint is absent, so no
    # credential was ever judged (UC-87b).
    if status_code == 404:
        return AuthStopAdvice(
            cause="login endpoint not found",
            remedy="Reload the integration to retry.",
        )
    # Waiting alone never resumes polling — the breaker clears only by
    # orchestrator reconstruction — so the remedy names both steps.
    if signal is CollectorSignal.AUTH_LOCKOUT:
        return AuthStopAdvice(
            cause="modem locked out further logins",
            remedy="Wait for the modem to clear the lockout, then reload the integration.",
        )
    # The default covers a rejected credential (UC-87) and the two
    # threshold trips (UC-13, UC-19a). It is the commonest cause, not a
    # fallback for causes nobody has modelled — add a row above instead.
    return AuthStopAdvice(
        cause="credentials rejected",
        remedy="Reconfigure credentials to resume.",
    )
