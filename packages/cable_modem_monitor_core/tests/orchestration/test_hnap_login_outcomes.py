"""Every HNAP login refusal, from the wire response to the breaker.

The three layers this crosses are each already tested against a mocked
neighbour: ``tests/auth/test_hnap.py`` drives the manager and asserts the
``AuthResult``, ``test_collector.py`` fabricates an ``AuthResult`` and
asserts the signal, ``test_policy.py`` feeds a signal and asserts the
breaker. Nothing joined them, and #201 lived in that seam --- the manager
returned a failed ``AuthResult`` for ``LoginResult: "RELOAD"``, the
collector read it as a credential verdict, and the breaker stopped polling
on the first occurrence. Every test at every layer passed.

So these cases start at the response the modem actually sends and end at
what the user experiences: signal, connection status, breaker, streak.
A regression at any one of the three layers fails here.

Deliberately negative-only. The success path needs resource loading and
parsing mocked to reach its end, which is a different test with a
different subject; ``TestSuccessfulLogin`` in ``tests/auth/test_hnap.py``
and the catalog replay suite cover it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from solentlabs.cable_modem_monitor_core.models.modem_config.auth import HnapAuth
from solentlabs.cable_modem_monitor_core.orchestration.collector import ModemDataCollector
from solentlabs.cable_modem_monitor_core.orchestration.models import ConnectionStatus
from solentlabs.cable_modem_monitor_core.orchestration.policy import SignalPolicy
from solentlabs.cable_modem_monitor_core.orchestration.signals import CollectorSignal

_CHALLENGE = "test_challenge_abc"
_PUBLIC_KEY = "test_public_key_xyz"
_COOKIE = "test_uid_cookie"


def _hnap_config() -> Any:
    """Minimal ModemConfig for an hnap entry, with no actions declared."""
    config = MagicMock()
    config.transport = "hnap"
    config.timeout = 10
    config.model = "S33"
    config.auth = HnapAuth(strategy="hnap", hmac_algorithm="sha256")
    # No logout action, matching every arris/s33* entry. Keeps the
    # teardown paths out of a test about login classification.
    config.actions = None
    return config


def _response(payload: dict[str, Any], text: str) -> MagicMock:
    """A 200 response carrying *payload* from .json() and *text* from .text."""
    # .text and .headers must be real values: the collector scrubs the
    # password out of the body and reads Content-Type when it builds the
    # AuthFailed event, and a MagicMock for either reaches the event model.
    resp = MagicMock()
    resp.status_code = 200
    resp.url = "http://192.0.2.1/HNAP1/"
    resp.request.method = "POST"
    resp.headers = {"Content-Type": "text/html"}
    resp.text = text
    resp.json.return_value = payload
    return resp


def _challenge_response() -> MagicMock:
    """Phase 1 answer: a well-formed challenge the manager can derive from."""
    payload = {
        "LoginResponse": {
            "Challenge": _CHALLENGE,
            "PublicKey": _PUBLIC_KEY,
            "Cookie": _COOKIE,
            "LoginResult": "OK",
        },
    }
    return _response(payload, '{"LoginResponse": {"LoginResult": "OK"}}')


def _login_response(login_result: str | None) -> MagicMock:
    """Phase 2 answer carrying *login_result*, or no LoginResult at all."""
    inner = {} if login_result is None else {"LoginResult": login_result}
    return _response(
        {"LoginResponse": inner},
        f'{{"LoginResponse": {{"LoginResult": "{login_result}"}}}}',
    )


def _poll(login_result: str | None) -> tuple[CollectorSignal, ConnectionStatus, SignalPolicy]:
    """Run one poll whose login answers *login_result*, then apply policy."""
    collector = ModemDataCollector(_hnap_config(), None, None, "http://192.0.2.1", "admin", "pw")
    with patch.object(collector._session, "post") as mock_post:
        mock_post.side_effect = [_challenge_response(), _login_response(login_result)]
        result = collector.execute()

    policy = SignalPolicy(collector, model="S33")
    return result.signal, policy.apply(result), policy


# What the S33v3 firmware can answer a login with, and what each must do to
# the poll. Tokens come from Login.js (AUTH_HNAP_SPEC.md § Firmware
# Assumptions); the last two rows are what happens off the end of that list.
#
# The RELOAD row is the regression guard for #201: it is the only refusal
# that must leave the breaker closed, because it is the only one the
# firmware itself answers by retrying rather than by telling the user
# something is wrong.
#
# ┌──────────────┬────────────────────┬──────────────┬────────┬────────┐
# │ LoginResult  │ signal             │ status       │ breaker│ streak │
# ├──────────────┼────────────────────┼──────────────┼────────┼────────┤
# │ FAILED       │ AUTH_FAILED        │ AUTH_FAILED  │ open   │ 1      │
# │ LOCKUP       │ AUTH_LOCKOUT       │ AUTH_FAILED  │ open   │ 1      │
# │ REBOOT       │ AUTH_LOCKOUT       │ AUTH_FAILED  │ open   │ 1      │
# │ RELOAD       │ AUTH_UNAVAILABLE   │ UNREACHABLE  │ closed │ 0      │
# │ NOT_A_TOKEN  │ AUTH_FAILED        │ AUTH_FAILED  │ open   │ 1      │
# │ (absent)     │ AUTH_FAILED        │ AUTH_FAILED  │ open   │ 1      │
# └──────────────┴────────────────────┴──────────────┴────────┴────────┘
#
# The absent row is a body with no LoginResult at all, which the manager
# reads as "" and routes to unexpected-result. It is recorded here as the
# behaviour that ships, not as the behaviour that is necessarily right: an
# unreadable answer is being called a credential verdict. Changing it is a
# decision, and this row is where that decision would show up.
#
# fmt: off
LOGIN_REFUSAL_CASES = [
    # (login_result,  signal,                            status,                       breaker, streak)
    ("FAILED",        CollectorSignal.AUTH_FAILED,       ConnectionStatus.AUTH_FAILED, True,  1),
    ("LOCKUP",        CollectorSignal.AUTH_LOCKOUT,      ConnectionStatus.AUTH_FAILED, True,  1),
    ("REBOOT",        CollectorSignal.AUTH_LOCKOUT,      ConnectionStatus.AUTH_FAILED, True,  1),
    ("RELOAD",        CollectorSignal.AUTH_UNAVAILABLE,  ConnectionStatus.UNREACHABLE, False, 0),
    ("NOT_A_TOKEN",   CollectorSignal.AUTH_FAILED,       ConnectionStatus.AUTH_FAILED, True,  1),
    (None,            CollectorSignal.AUTH_FAILED,       ConnectionStatus.AUTH_FAILED, True,  1),
]
# fmt: on


@pytest.mark.parametrize(
    ("login_result", "signal", "status", "breaker", "streak"),
    LOGIN_REFUSAL_CASES,
    ids=[c[0] or "absent" for c in LOGIN_REFUSAL_CASES],
)
def test_login_refusal_outcome(
    login_result: str | None,
    signal: CollectorSignal,
    status: ConnectionStatus,
    breaker: bool,
    streak: int,
) -> None:
    """A refused login produces its defined signal, status, and breaker state."""
    actual_signal, actual_status, policy = _poll(login_result)

    assert actual_signal is signal
    assert actual_status is status
    assert policy.circuit_open is breaker
    assert policy.auth_failure_streak == streak


def test_reload_never_trips_however_often_it_repeats() -> None:
    """#201: polling must outlive a modem that keeps asking for a restart.

    A threshold here would only postpone the wrong answer, which is the
    argument UC-87a already makes for a 5xx. Ten times the auth threshold
    is well past any streak the breaker reads.
    """
    collector = ModemDataCollector(_hnap_config(), None, None, "http://192.0.2.1", "admin", "pw")
    policy = SignalPolicy(collector, model="S33")

    for _ in range(policy._threshold * 10):
        with patch.object(collector._session, "post") as mock_post:
            mock_post.side_effect = [_challenge_response(), _login_response("RELOAD")]
            policy.apply(collector.execute())

    assert policy.circuit_open is False
    assert policy.auth_failure_streak == 0


def test_reload_does_not_clear_the_session() -> None:
    """Nothing is wrong with the session, so nothing should discard it.

    Clearing here would force a fresh challenge every poll on firmware
    whose anti-brute-force counts login attempts (§ Lockout behaviour).
    """
    collector = ModemDataCollector(_hnap_config(), None, None, "http://192.0.2.1", "admin", "pw")
    policy = SignalPolicy(collector, model="S33")

    with (
        patch.object(collector._session, "post") as mock_post,
        patch.object(collector, "clear_session") as mock_clear,
    ):
        mock_post.side_effect = [_challenge_response(), _login_response("RELOAD")]
        policy.apply(collector.execute())

    mock_clear.assert_not_called()


def test_rejected_credential_still_stops_polling() -> None:
    """The #201 fix must not let a real bad password poll forever.

    Login.js branches FAILED and RELOAD in the same equality chain, so the
    firmware never uses one to mean the other; this pins that reading.
    Jordan's 2026-08-29 capture has both on the wire from one session.
    """
    _, status, policy = _poll("FAILED")

    assert status is ConnectionStatus.AUTH_FAILED
    assert policy.circuit_open is True
    assert policy.circuit_trip_signal is CollectorSignal.AUTH_FAILED
