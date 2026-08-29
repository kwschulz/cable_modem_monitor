"""Fleet gate: every ``LoginResult`` token an HNAP firmware emits is handled.

AUTH_HNAP_SPEC.md § Evidence Base makes the firmware JavaScript the
authority for protocol claims, and § Firmware Assumptions cites ``Login.js``
as the source for the ``LoginResult`` value set. Nothing enforced the
citation. The S33v3's ``Login.js`` branches on a fifth token, ``RELOAD``,
which sat in that entry's committed HAR from 2026-07-10 while
``auth/hnap.py`` routed it to "unexpected result" — an ``AUTH_FAILED`` that
trips the circuit breaker on its first occurrence and stops polling until
the user reconfigures credentials that were never wrong (#201).

The gate reads the tokens back out of each entry's captured ``Login.js``
and asserts the auth manager knows them, so the next firmware line that
adds one fails here instead of in the field.

Scope note: a HAR that captured no ``Login.js`` contributes nothing. That
is most of the fleet, so ``test_extractor_finds_tokens`` pairs the gate —
without it, an extractor that silently matched nothing would pass every
entry.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from solentlabs.cable_modem_monitor_core.auth.hnap import HANDLED_LOGIN_RESULTS
from solentlabs.cable_modem_monitor_core.config_loader import load_modem_config
from solentlabs.cable_modem_monitor_core.har import load_har_json

# Login.js dispatches on the token with a strict equality chain:
#     if(obj2.LoginResult == "FAILED") ... else if(obj2.LoginResult == "RELOAD")
_LOGIN_RESULT_COMPARISON = re.compile(r'LoginResult\s*==\s*"([A-Za-z_]+)"')


def _login_result_tokens(modem_dir: Path) -> set[str]:
    """Tokens the entry's captured Login.js compares LoginResult against."""
    test_data = modem_dir / "test_data"
    if not test_data.is_dir():
        return set()

    tokens: set[str] = set()
    for har_path in sorted(test_data.glob("*.har")):
        har = load_har_json(har_path)
        for entry in har["log"]["entries"]:
            if "Login.js" not in entry["request"]["url"]:
                continue
            body = (entry.get("response", {}).get("content") or {}).get("text") or ""
            tokens |= set(_LOGIN_RESULT_COMPARISON.findall(body))
    return tokens


def test_login_results_are_handled(modem_yaml_path: Path) -> None:
    """No HNAP entry's firmware emits a LoginResult the auth manager ignores."""
    config = load_modem_config(modem_yaml_path)
    if config.auth is None or config.auth.strategy != "hnap":
        pytest.skip("not an hnap entry")

    tokens = _login_result_tokens(modem_yaml_path.parent)
    if not tokens:
        pytest.skip("no Login.js in this entry's captures")

    unhandled = tokens - HANDLED_LOGIN_RESULTS
    assert not unhandled, (
        f"{modem_yaml_path.parent.name}: Login.js branches on {sorted(unhandled)}, "
        f"which auth/hnap.py routes to 'unexpected result' → AUTH_FAILED. "
        f"Read the firmware's own handling for each, then add it to the right "
        f"set in auth/hnap.py and a row to LOGIN_RESULT_CASES in "
        f"tests/auth/test_hnap.py."
    )


def test_extractor_finds_tokens() -> None:
    """The extractor reads real tokens, so a skip means absent, not broken."""
    catalog = Path(__file__).parent.parent / "solentlabs" / "cable_modem_monitor_catalog" / "modems"
    found: set[str] = set()
    for modem_yaml in sorted(catalog.rglob("modem*.yaml")):
        config = load_modem_config(modem_yaml)
        if config.auth is not None and config.auth.strategy == "hnap":
            found |= _login_result_tokens(modem_yaml.parent)

    # RELOAD is the token this gate exists for and only the S33v3 line emits
    # it; if it stops being found, the extractor broke, not the firmware.
    assert {"FAILED", "LOCKUP", "REBOOT", "OK_CHANGED", "RELOAD"} <= found
