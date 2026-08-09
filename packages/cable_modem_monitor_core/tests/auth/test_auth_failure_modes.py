"""Every auth strategy declares how to read a post-login 401.

``BaseAuthManager.auth_failure_mode`` decides whether a 401 arriving
after a "successful" login means the session was refused or the
password was wrong all along. The default is pessimistic; overriding
to ``SESSION_REJECTED`` is a claim that the strategy rejects a bad
password at login time.

That claim is paid for by a bad-password test living with the strategy
it describes:

- ``form_pbkdf2`` — ``test_form_pbkdf2.py::TestFormPbkdf2AuthManager::
  test_wrong_password_fails_login``
- ``hnap`` — ``test_hnap.py::test_login_failure``
- ``form`` **with success criteria** — below, in both directions per
  criterion: ``test_form_with_success_criteria_rejects_bad_password`` and
  ``test_form_success_criteria_decide_both_ways``

The strategies that cannot tell get the inverse test here, pinning the
fact that a wrong password sails through login. That is precisely why
their post-login 401 must not be reported as "your password is fine".

See ARCHITECTURE_DECISIONS.md "Post-login 401 is read per auth
strategy".
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
import requests
from pydantic import TypeAdapter
from solentlabs.cable_modem_monitor_core.auth import create_auth_manager
from solentlabs.cable_modem_monitor_core.auth.base import AuthFailureMode
from solentlabs.cable_modem_monitor_core.models.modem_config import ModemConfig
from solentlabs.cable_modem_monitor_core.models.modem_config.auth import (
    AuthConfig,
    get_strategy_display_labels,
)
from solentlabs.cable_modem_monitor_core.orchestration.auth_failure import (
    _auth_failure_hint,
)

# ┌───────────────┬──────────────────────┬────────────────────────────────────┐
# │ strategy      │ declared mode        │ why                                │
# ├───────────────┼──────────────────────┼────────────────────────────────────┤
# │ none          │ NOT_CONFIGURED       │ no credentials exist               │
# │ basic         │ CREDENTIALS_SUSPECT  │ never validates; the 401 is the tell│
# │ form          │ CREDENTIALS_SUSPECT  │ no success criteria: nothing checked│
# │ form_nonce    │ CREDENTIALS_SUSPECT  │ checks, but unproven — see below   │
# │ url_token     │ CREDENTIALS_SUSPECT  │ checks, but unproven — see below   │
# │ form_cbn      │ CREDENTIALS_SUSPECT  │ checks, but unproven — see below   │
# │ form_sjcl     │ CREDENTIALS_SUSPECT  │ checks, but unproven — see below   │
# │ bearer        │ CREDENTIALS_SUSPECT  │ checks, but unproven — see below   │
# │ form_pbkdf2   │ SESSION_REJECTED     │ proven — login_success mismatch    │
# │ hnap          │ SESSION_REJECTED     │ proven — LoginResult mismatch      │
# └───────────────┴──────────────────────┴────────────────────────────────────┘
#
# "unproven" is not a judgement about the strategy. The mock harness
# cannot yet simulate a rejected login for it ("Credentials are not
# validated" in test_harness/auth/), so the claim cannot be paid for.
# Adding that rejection, plus a bad-password test, is the price of
# flipping one of these to SESSION_REJECTED.
#
# fmt: off
DECLARED_MODES: dict[str, AuthFailureMode] = {
    "none":        AuthFailureMode.NOT_CONFIGURED,
    "basic":       AuthFailureMode.CREDENTIALS_SUSPECT,
    "form":        AuthFailureMode.CREDENTIALS_SUSPECT,
    "form_nonce":  AuthFailureMode.CREDENTIALS_SUSPECT,
    "url_token":   AuthFailureMode.CREDENTIALS_SUSPECT,
    "form_cbn":    AuthFailureMode.CREDENTIALS_SUSPECT,
    "form_sjcl":   AuthFailureMode.CREDENTIALS_SUSPECT,
    "bearer":      AuthFailureMode.CREDENTIALS_SUSPECT,
    "form_pbkdf2": AuthFailureMode.SESSION_REJECTED,
    "hnap":        AuthFailureMode.SESSION_REJECTED,
}

# Smallest config that validates for each strategy (required fields only).
_MINIMAL_AUTH: dict[str, dict[str, Any]] = {
    "none":        {},
    "basic":       {},
    "form":        {"action": "/login.htm"},
    "form_nonce":  {"action": "/login.htm", "nonce_field": "nonce"},
    "url_token":   {"login_page": "/login.html"},
    "form_cbn":    {},
    "form_sjcl":   {"login_endpoint": "/login", "pbkdf2_iterations": 1000, "pbkdf2_key_length": 128},
    "bearer":      {"login_endpoint": "/login", "token_path": "token"},
    "form_pbkdf2": {"login_endpoint": "/login", "pbkdf2_iterations": 1000, "pbkdf2_key_length": 128},
    "hnap":        {"hmac_algorithm": "md5"},
}
# fmt: on


# The rows above describe each strategy at its minimal config. `form` is
# the one whose answer depends on that config: naming a `success`
# criterion makes it verify the credential at login time, so a form modem
# with one reads its post-login 401 as SESSION_REJECTED. A block naming
# no criterion cannot occur — FormSuccess rejects it. See
# test_form_with_success_criteria_rejects_bad_password.


def _manager_for(strategy: str, **overrides: Any) -> Any:
    """Build the auth manager for a strategy from its minimal config, plus any overrides."""
    auth = TypeAdapter(AuthConfig).validate_python({"strategy": strategy, **_MINIMAL_AUTH[strategy], **overrides})
    # create_auth_manager only reads .auth; a full ModemConfig would add
    # a dozen unrelated required fields to every row of _MINIMAL_AUTH.
    # ModemConfig is named unquoted so the import is a live reference: a
    # string cast reads as an unused import to CodeQL (py/unused-import),
    # and moving it under TYPE_CHECKING relocates that finding rather
    # than resolving it.
    return create_auth_manager(cast(ModemConfig, SimpleNamespace(auth=auth)))


# ---------------------------------------------------------------------------
# Completeness — the forcing function
# ---------------------------------------------------------------------------


def test_every_strategy_declares_a_mode() -> None:
    """A new auth strategy fails here until someone decides how its 401 reads.

    The base class supplies a safe default so a missed declaration is
    merely unspecific rather than wrong; this test stops it being
    missed silently.
    """
    undeclared = set(get_strategy_display_labels()) - set(DECLARED_MODES)

    assert not undeclared, (
        f"auth strategies with no declared failure mode: {sorted(undeclared)}. "
        f"Add a row to DECLARED_MODES, and if it claims SESSION_REJECTED, a "
        f"bad-password test proving the login is verified."
    )


def test_no_stale_rows() -> None:
    """A removed strategy must not leave a row behind claiming coverage."""
    stale = set(DECLARED_MODES) - set(get_strategy_display_labels())

    assert not stale, f"DECLARED_MODES rows for strategies that no longer exist: {sorted(stale)}"


@pytest.mark.parametrize("strategy", sorted(DECLARED_MODES), ids=sorted(DECLARED_MODES))
def test_declared_mode_matches_implementation(strategy: str) -> None:
    """The manager reports the mode this module declares for it."""
    assert _manager_for(strategy).auth_failure_mode() is DECLARED_MODES[strategy]


# ---------------------------------------------------------------------------
# The inverse fact — these strategies cannot tell at login time
# ---------------------------------------------------------------------------

# Regression guard for the beta.17 defect: a bad password on one of
# these produced "the login worked, so this is not a username or
# password problem". If either assertion flips, the strategy gained
# real failure detection and may now claim SESSION_REJECTED.


class _AlwaysOkHandler(BaseHTTPRequestHandler):
    """Modem that re-renders its login page with HTTP 200 on a bad password."""

    status = 200

    def do_POST(self) -> None:  # noqa: N802
        body = b"<html><form><input name='password'></form></html>"
        self.send_response(self.status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        """Same response as POST — the login pre-fetch behaves identically."""
        self.do_POST()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence per-request server logging."""


class _Always401Handler(_AlwaysOkHandler):
    """Modem that refuses the login outright instead of re-rendering the page."""

    status = 401


class _Always503Handler(_AlwaysOkHandler):
    """Modem declining to serve the login — busy session slot, or mid-reboot."""

    status = 503


def _serve(handler: type[BaseHTTPRequestHandler]) -> Any:
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture()
def always_ok_server() -> Any:
    """Serve HTTP 200 for everything, the common bad-password response."""
    yield from _serve(_AlwaysOkHandler)


@pytest.fixture()
def always_401_server() -> Any:
    """Serve HTTP 401 for everything, the unambiguous login refusal."""
    yield from _serve(_Always401Handler)


@pytest.fixture()
def always_503_server() -> Any:
    """Serve HTTP 503 for everything, the modem declining to serve a login."""
    yield from _serve(_Always503Handler)


# ---------------------------------------------------------------------------
# One rule — a login answering >= 400 failed, and brings the response home
# ---------------------------------------------------------------------------

# Two ratified decisions meet here, both in ARCHITECTURE_DECISIONS.md:
# § Auth-failure detail via single WARNING log — "Auth managers must
# include the requests.Response on their failure AuthResult" — and § How
# to add an auth strategy — "If the strategy pre-fetches a login page,
# use the response... Discarding the response body is a bug."
#
# Attaching is not only about the log line. The collector reads the
# attached response's status to tell AUTH_UNAVAILABLE from AUTH_FAILED
# (UC-87a), so a strategy that drops it forces a 5xx to read as a
# rejected credential and trips the breaker on the first poll.
#
# The threshold is >= 400, not != 200: form_nonce and form_cbn post with
# allow_redirects=False, and basic's challenge probe is a GET with the
# same, so a 302 is a normal answer on all three.

# Declared exceptions at their minimal config — these send no login
# request, so nothing can answer it 503. Their 5xx arrives later, on the
# data fetch, as LOAD_ERROR. `basic` stops being an exception once
# challenge_cookie is configured; see the test below the parametrized one.
_NO_LOGIN_REQUEST = {
    "none": "sends no credential and issues no login request",
    "basic": "sets session.auth and returns; the credential rides on the data fetch",
}


@pytest.mark.parametrize("strategy", sorted(DECLARED_MODES), ids=sorted(DECLARED_MODES))
def test_login_5xx_fails_and_attaches_response(strategy: str, always_503_server: str) -> None:
    """Every strategy that issues a login reports a 5xx as failure, with the response."""
    result = _manager_for(strategy).authenticate(requests.Session(), always_503_server, "admin", "pw")

    if strategy in _NO_LOGIN_REQUEST:
        assert result.success is True, f"{strategy} {_NO_LOGIN_REQUEST[strategy]}"
        return

    assert result.success is False, f"{strategy} reported success for a login answered 503"
    assert result.response is not None, (
        f"{strategy} failed without attaching the response — the collector cannot "
        f"tell AUTH_UNAVAILABLE from AUTH_FAILED, so the user is told their password is wrong"
    )
    assert result.response.status_code == 503


def test_basic_challenge_probe_status_is_not_read(always_503_server: str) -> None:
    """Pinned, not endorsed: `basic` with a challenge probe ignores what the probe answers.

    The row above exempts `basic` because at its minimal config it sends
    nothing. With ``challenge_cookie: true`` it does send a
    credential-bearing ``GET /`` and still returns success whatever comes
    back. Two catalog entries depend on that: `netgear/c7000v2` and
    `netgear/cm1200`'s basic variant both record the 401 from this probe
    as what sets ``XSRF_TOKEN``, so on those modems the refusal is the
    mechanism and a status guard would break the auth it protects.

    Whether that expected 401 can be told apart from a genuine 5xx is an
    open question needing evidence no capture holds. This pins today's
    answer so changing it is a decision rather than a drift.
    """
    session = requests.Session()
    manager = _manager_for("basic", challenge_cookie=True)

    with patch.object(session, "get", wraps=session.get) as probe:
        result = manager.authenticate(session, always_503_server, "admin", "pw")

    # Assert the probe fired and carried the credential; without this the
    # test would still pass if the probe were removed entirely.
    assert probe.call_count == 1, "the challenge probe must actually be sent"
    assert session.auth == ("admin", "pw"), "the probe carries the credential"
    assert probe.call_args.args[0].endswith("/"), "the probe targets the modem root"

    assert result.success is True, "a 503 on the challenge probe is not currently read"
    assert result.response is None


# ---------------------------------------------------------------------------
# The log hint Core renders from the mode
# ---------------------------------------------------------------------------

# ┌───────────────┬──────────────────────────────────────────┐
# │ strategy      │ expected hint substring                  │
# ├───────────────┼──────────────────────────────────────────┤
# │ none          │ "modem requires authentication"          │
# │ basic         │ "credentials rejected"                   │
# │ form          │ "credentials rejected"                   │
# │ form_pbkdf2   │ "session expired"                        │
# │ hnap          │ "session expired"                        │
# └───────────────┴──────────────────────────────────────────┘
#
# form reads "credentials rejected" deliberately. It used to fall
# through to "session expired", which asserted a verification that
# never happened.
#
# fmt: off
_HINT_CASES = [
    ("none",        "modem requires authentication"),
    ("basic",       "credentials rejected"),
    ("form",        "credentials rejected"),
    ("form_pbkdf2", "session expired"),
    ("hnap",        "session expired"),
]
# fmt: on


@pytest.mark.parametrize(("strategy", "expected"), _HINT_CASES, ids=[c[0] for c in _HINT_CASES])
def test_auth_failure_hint(strategy: str, expected: str) -> None:
    """Core's 401 log hint follows the strategy's declared mode."""
    assert expected in _auth_failure_hint(_manager_for(strategy))


def test_basic_accepts_any_password() -> None:
    """basic auth attaches credentials without ever validating them."""
    manager = _manager_for("basic")

    result = manager.authenticate(requests.Session(), "http://192.168.100.1", "admin", "wrong")

    assert result.success is True
    assert manager.auth_failure_mode() is AuthFailureMode.CREDENTIALS_SUSPECT


def test_form_accepts_any_non_error_response(always_ok_server: str) -> None:
    """form auth with no success criteria reads a re-rendered login page as success."""
    manager = _manager_for("form")

    result = manager.authenticate(requests.Session(), always_ok_server, "admin", "wrong")

    assert result.success is True
    assert manager.auth_failure_mode() is AuthFailureMode.CREDENTIALS_SUSPECT


def test_form_with_success_criteria_rejects_bad_password(always_ok_server: str) -> None:
    """form auth declaring success criteria fails the same login the plain config accepts.

    Same server, same wrong password, opposite verdict: the criteria are
    what turn an unverified login into a verified one. That is the claim
    ARCHITECTURE_DECISIONS.md requires before a strategy may report
    SESSION_REJECTED, so a 401 arriving later is genuinely session-side.
    """
    manager = _manager_for("form", success={"indicator": "Welcome"})

    result = manager.authenticate(requests.Session(), always_ok_server, "admin", "wrong")

    assert result.success is False
    assert manager.auth_failure_mode() is AuthFailureMode.SESSION_REJECTED


# Rejecting is only half the claim. A criterion that refused every login
# would satisfy the test above while breaking every good password, so each
# branch is exercised in both directions against the same server. `redirect`
# is the branch that matters in the field: all 9 catalog entries declaring
# `success` use it and none uses `indicator`.
#
# fmt: off
_CRITERIA_CASES = [
    # (criteria,                          accepted, why)
    ({"redirect": "/login.htm"},          True,     "landed on the path the criterion names"),
    ({"redirect": "/DocsisStatus.htm"},   False,    "never left the login page"),
    ({"indicator": "<form"},              True,     "body carries the indicator"),
    ({"indicator": "Welcome"},            False,    "re-rendered login page lacks it"),
]
# fmt: on


@pytest.mark.parametrize(
    ("criteria", "accepted", "why"),
    _CRITERIA_CASES,
    ids=[f"{next(iter(c[0]))}-{'accept' if c[1] else 'reject'}" for c in _CRITERIA_CASES],
)
def test_form_success_criteria_decide_both_ways(
    always_ok_server: str,
    criteria: dict[str, str],
    accepted: bool,
    why: str,
) -> None:
    """Each success criterion accepts and rejects, so SESSION_REJECTED is earned, not blanket."""
    manager = _manager_for("form", success=criteria)

    result = manager.authenticate(requests.Session(), always_ok_server, "admin", "pw")

    assert result.success is accepted, why


# A `success` block naming no criterion would check nothing while reading as
# verification, so FormSuccess rejects it at load time. That guarantee is what
# lets auth_failure_mode() treat "success is not None" as "a criterion is
# named". Coverage lives with the schema:
# tests/models/fixtures/modem_config/invalid/form_success_{no,blank}_criterion.json


def test_form_http_error_fails_login_whatever_the_criteria(always_401_server: str) -> None:
    """A 4xx login response is a failed login even when `success` is declared.

    The HTTP-error guard used to live on the no-criteria branch only, so
    declaring `success` silently dropped it. The criterion here is one the
    401 body satisfies, which is what makes this discriminating: before the
    fix the status went unchecked, the indicator matched, and a 401 login
    refusal was reported as a success. Declaring criteria must narrow what
    counts as success, never widen it.
    """
    manager = _manager_for("form", success={"indicator": "<form"})

    result = manager.authenticate(requests.Session(), always_401_server, "admin", "wrong")

    assert result.success is False
    assert "401" in result.error
