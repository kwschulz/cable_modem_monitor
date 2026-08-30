"""Every CBN login refusal, from the wire response to the breaker.

The three layers this crosses are each already tested against a mocked
neighbour: ``tests/auth/test_form_cbn.py`` drives the manager and asserts
the ``AuthResult``, ``test_collector.py`` fabricates an ``AuthResult`` and
asserts the signal, ``test_policy.py`` feeds a signal and asserts the
breaker. Nothing joined them for ``form_cbn``, and that is the seam this
defect lived in --- the manager returned a failed ``AuthResult`` for every
body without ``"successful"``, the collector read it as a credential
verdict, and the breaker stopped polling on the first occurrence. Every
test at every layer passed.

So these cases start at the response the modem actually sends and end at
what the user experiences: signal, connection status, breaker, streak.
A regression at any one of the three layers fails here.

Mirrors ``test_hnap_login_outcomes.py``, which covers the same seam for
the HNAP ``RELOAD`` token.

Deliberately negative-only. The success path needs resource loading and
parsing mocked to reach its end, which is a different test with a
different subject; ``TestSuccessfulLogin`` in ``tests/auth/test_form_cbn.py``
and the catalog replay suite cover it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from solentlabs.cable_modem_monitor_core.models.modem_config.auth import FormCbnAuth
from solentlabs.cable_modem_monitor_core.orchestration.collector import ModemDataCollector
from solentlabs.cable_modem_monitor_core.orchestration.models import ConnectionStatus
from solentlabs.cable_modem_monitor_core.orchestration.policy import SignalPolicy
from solentlabs.cable_modem_monitor_core.orchestration.signals import CollectorSignal

_BASE_URL = "http://192.0.2.1"


def _cbn_config() -> Any:
    """Minimal ModemConfig for a form_cbn entry, with no actions declared."""
    config = MagicMock()
    config.transport = "cbn"
    config.timeout = 10
    config.model = "T200"
    config.auth = FormCbnAuth(strategy="form_cbn")
    # No logout action: keeps the teardown paths out of a test about login
    # classification. Both shipping CBN entries declare one, but it runs
    # after a successful poll, and every case here fails auth.
    config.actions = None
    return config


def _response(text: str, url: str, method: str) -> MagicMock:
    """A 200 response carrying *text*.

    ``.text`` and ``.headers`` must be real values: the collector scrubs the
    password out of the body and reads Content-Type when it builds the
    AuthFailed event, and a MagicMock for either reaches the event model.
    """
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.url = url
    resp.request.method = method
    resp.headers = {"Content-Type": "text/html"}
    resp.text = text
    return resp


def _poll(login_body: str) -> tuple[CollectorSignal, ConnectionStatus, SignalPolicy]:
    """Run one poll whose login answers *login_body*, then apply policy."""
    collector = ModemDataCollector(_cbn_config(), None, None, _BASE_URL, "admin", "pw")
    policy = SignalPolicy(collector, model="T200")
    return (*_apply(collector, policy, login_body), policy)


def _apply(
    collector: ModemDataCollector,
    policy: SignalPolicy,
    login_body: str,
) -> tuple[CollectorSignal, ConnectionStatus]:
    """Drive one login round through *collector* and *policy*."""

    def fake_get(url: str, **kwargs: Any) -> MagicMock:
        # The login page GET is what hands the strategy its sessionToken;
        # without the cookie the manager fails before reading any token.
        collector._session.cookies.set("sessionToken", "tok")
        return _response("<html>login</html>", f"{_BASE_URL}/common_page/login.html", "GET")

    with (
        patch.object(collector._session, "get", side_effect=fake_get),
        patch.object(
            collector._session,
            "post",
            return_value=_response(login_body, f"{_BASE_URL}/xml/setter.xml", "POST"),
        ),
    ):
        result = collector.execute()

    return result.signal, policy.apply(result)


# What the CH7465MT firmware can answer a login with, and what each must do
# to the poll. Tokens come from the captured firmware JS (AUTH_CBN_SPEC.md
# § Login Token Vocabulary); the last row is what happens off the end of
# that list, which is the firmware's own ShowPasswordError() branch.
#
# The two restart rows are the regression guard: they are the only refusals
# that must leave the breaker closed, because they are the only ones the
# firmware itself answers by restarting the login rather than by telling
# the user something is wrong.
#
# ┌──────────────────┬────────────────────┬──────────────┬────────┬────────┐
# │ login body       │ signal             │ status       │ breaker│ streak │
# ├──────────────────┼────────────────────┼──────────────┼────────┼────────┤
# │ lockedout        │ AUTH_LOCKOUT       │ AUTH_FAILED  │ open   │ 1      │
# │ cbnAccessDenied  │ AUTH_LOCKOUT       │ AUTH_FAILED  │ open   │ 1      │
# │ cbnLogin         │ AUTH_UNAVAILABLE   │ UNREACHABLE  │ closed │ 0      │
# │ cbnFirstInstall  │ AUTH_UNAVAILABLE   │ UNREACHABLE  │ closed │ 0      │
# │ cbnBlockContent  │ AUTH_FAILED        │ AUTH_FAILED  │ open   │ 1      │
# │ idloginincorrect │ AUTH_FAILED        │ AUTH_FAILED  │ open   │ 1      │
# └──────────────────┴────────────────────┴──────────────┴────────┴────────┘
#
# The cbnBlockContent row records the behaviour that ships, not the
# behaviour that is necessarily right: the firmware sends that token to
# Blocked-content.html rather than to its password-error branch, so calling
# it a credential verdict is a choice made without evidence of what the
# token means (AUTH_CBN_SPEC.md § Known Gaps). Changing it is a decision,
# and this row is where that decision would show up.
#
# fmt: off
LOGIN_REFUSAL_CASES = [
    # (login_body,        signal,                            status,                       breaker, streak)
    ("lockedout",         CollectorSignal.AUTH_LOCKOUT,      ConnectionStatus.AUTH_FAILED, True,  1),
    ("cbnAccessDenied",   CollectorSignal.AUTH_LOCKOUT,      ConnectionStatus.AUTH_FAILED, True,  1),
    ("cbnLogin",          CollectorSignal.AUTH_UNAVAILABLE,  ConnectionStatus.UNREACHABLE, False, 0),
    ("cbnFirstInstall",   CollectorSignal.AUTH_UNAVAILABLE,  ConnectionStatus.UNREACHABLE, False, 0),
    ("cbnBlockContent",   CollectorSignal.AUTH_FAILED,       ConnectionStatus.AUTH_FAILED, True,  1),
    ("idloginincorrect",  CollectorSignal.AUTH_FAILED,       ConnectionStatus.AUTH_FAILED, True,  1),
]
# fmt: on


@pytest.mark.parametrize(
    ("login_body", "signal", "status", "breaker", "streak"),
    LOGIN_REFUSAL_CASES,
    ids=[c[0] for c in LOGIN_REFUSAL_CASES],
)
def test_login_refusal_outcome(
    login_body: str,
    signal: CollectorSignal,
    status: ConnectionStatus,
    breaker: bool,
    streak: int,
) -> None:
    """A refused login produces its defined signal, status, and breaker state."""
    actual_signal, actual_status, policy = _poll(login_body)

    assert actual_signal is signal
    assert actual_status is status
    assert policy.circuit_open is breaker
    assert policy.auth_failure_streak == streak


@pytest.mark.parametrize("login_body", ["cbnLogin", "cbnFirstInstall"])
def test_restart_never_trips_however_often_it_repeats(login_body: str) -> None:
    """Polling must outlive a modem that keeps asking for a restart.

    A threshold here would only postpone the wrong answer, which is the
    argument UC-87a already makes for a 5xx. Ten times the auth threshold
    is well past any streak the breaker reads.
    """
    collector = ModemDataCollector(_cbn_config(), None, None, _BASE_URL, "admin", "pw")
    policy = SignalPolicy(collector, model="T200")

    for _ in range(policy._threshold * 10):
        _apply(collector, policy, login_body)

    assert policy.circuit_open is False
    assert policy.auth_failure_streak == 0


def test_restart_does_not_clear_the_session() -> None:
    """Nothing is wrong with the session, so nothing should discard it.

    Clearing here would drop the sessionToken the next login derives its
    AES key from, forcing a fresh login page fetch every poll on firmware
    that counts login attempts toward its own lockout.
    """
    collector = ModemDataCollector(_cbn_config(), None, None, _BASE_URL, "admin", "pw")
    policy = SignalPolicy(collector, model="T200")

    with patch.object(collector, "clear_session") as mock_clear:
        _apply(collector, policy, "cbnLogin")

    mock_clear.assert_not_called()


def test_rejected_credential_still_stops_polling() -> None:
    """The fix must not let a real bad password poll forever.

    The firmware's own handler falls through to ShowPasswordError() for any
    body it does not recognise, so an unknown token stays a credential
    verdict; this pins that reading.
    """
    _, status, policy = _poll("idloginincorrect")

    assert status is ConnectionStatus.AUTH_FAILED
    assert policy.circuit_open is True
    assert policy.circuit_trip_signal is CollectorSignal.AUTH_FAILED


def test_lockout_is_distinguishable_from_a_rejected_credential() -> None:
    """Lockout and wrong-password both stop polling, but report differently.

    The breaker records its trip reason, so the blocked polls that follow
    tell the user to wait rather than to re-enter a password (#117).
    """
    _, status, policy = _poll("lockedout")

    assert status is ConnectionStatus.AUTH_FAILED
    assert policy.circuit_trip_signal is CollectorSignal.AUTH_LOCKOUT
