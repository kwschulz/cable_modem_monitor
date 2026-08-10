"""Tests for the trip-reason table behind every stopped-modem message.

Asserted directly rather than through a surface, because a surface can
be right while the table is wrong.
"""

from __future__ import annotations

import pytest
from solentlabs.cable_modem_monitor_core.orchestration.auth_stop import auth_stop_advice
from solentlabs.cable_modem_monitor_core.orchestration.signals import CollectorSignal

# ┌────────────────────┬────────┬───────────────────────────────────────┐
# │ signal             │ status │ what the user is told to do           │
# ├────────────────────┼────────┼───────────────────────────────────────┤
# │ AUTH_FAILED        │ 404    │ reload — the endpoint is absent       │
# │ AUTH_LOCKOUT       │ None   │ wait, then reload                     │
# │ AUTH_FAILED        │ 401    │ reconfigure credentials               │
# │ LOAD_AUTH          │ None   │ reconfigure credentials               │
# │ LOAD_INTEGRITY     │ None   │ reconfigure credentials               │
# │ None (never seen)  │ None   │ reconfigure credentials               │
# └────────────────────┴────────┴───────────────────────────────────────┘
#
ENDPOINT = ("login endpoint not found", "Reload the integration to retry.")
LOCKOUT = (
    "modem locked out further logins",
    "Wait for the modem to clear the lockout, then reload the integration.",
)
CREDENTIALS = ("credentials rejected", "Reconfigure credentials to resume.")

# fmt: off
ADVICE_CASES = [
    (CollectorSignal.AUTH_FAILED,    404,  ENDPOINT,    "absent endpoint"),
    (CollectorSignal.AUTH_LOCKOUT,   None, LOCKOUT,     "firmware lockout"),
    (CollectorSignal.AUTH_FAILED,    401,  CREDENTIALS, "credential verdict"),
    (CollectorSignal.LOAD_AUTH,      None, CREDENTIALS, "six refused sessions"),
    (CollectorSignal.LOAD_INTEGRITY, None, CREDENTIALS, "six stub pages"),
    (None,                           None, CREDENTIALS, "no trip recorded"),
]
# fmt: on


@pytest.mark.parametrize(
    "signal,status_code,expected,desc",
    ADVICE_CASES,
    ids=[c[3] for c in ADVICE_CASES],
)
def test_advice_for_every_trip_reason(signal, status_code, expected, desc) -> None:
    """Each trip reason maps to one cause and one remedy."""
    advice = auth_stop_advice(signal, status_code)
    assert (advice.cause, advice.remedy) == expected
