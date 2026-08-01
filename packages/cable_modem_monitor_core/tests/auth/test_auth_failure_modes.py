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
# │ form          │ CREDENTIALS_SUSPECT  │ accepts any response under HTTP 400│
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


def _manager_for(strategy: str) -> Any:
    """Build the auth manager for a strategy from its minimal config."""
    auth = TypeAdapter(AuthConfig).validate_python({"strategy": strategy, **_MINIMAL_AUTH[strategy]})
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

    def do_POST(self) -> None:  # noqa: N802
        body = b"<html><form><input name='password'></form></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        """Same 200 response as POST — the login pre-fetch also succeeds."""
        self.do_POST()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence per-request server logging."""


@pytest.fixture()
def always_ok_server() -> Any:
    """Serve HTTP 200 for everything, the common bad-password response."""
    server = HTTPServer(("127.0.0.1", 0), _AlwaysOkHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


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
