"""Fleet gate: every login token a CBN firmware emits is handled.

AUTH_CBN_SPEC.md § Evidence Base makes the firmware JavaScript the
authority for protocol claims. ``auth/form_cbn.py`` read any login body
without ``"successful"`` as a wrong password, so the four tokens the
firmware routes elsewhere -- two lockout, two restart-the-login -- each
produced an ``AUTH_FAILED`` that trips the circuit breaker on its first
occurrence, stopping polling until the user reconfigures credentials the
modem never judged.

The gate reads the tokens back out of each entry's captured firmware JS
and asserts the auth manager knows them, so the next firmware line that
adds one fails here instead of in the field.

Scope note: the CBN vocabulary rests on a single capture (CH7465MT).
``arris/sb8200-cbn`` ships a synthetic fixture with no firmware JS and
contributes nothing, so ``test_extractor_finds_tokens`` pairs the gate --
without it, an extractor that silently matched nothing would pass the
whole fleet. Those two entries are the entire CBN fleet.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from solentlabs.cable_modem_monitor_core.auth.form_cbn import HANDLED_LOGIN_TOKENS
from solentlabs.cable_modem_monitor_core.config_loader import load_modem_config
from solentlabs.cable_modem_monitor_core.har import load_har_json

# The firmware tests the login response two ways, both scoped to the
# response variable:
#     if(response.match("cbnBlockContent"))
#     else if(response == "lockedout" || response.match("cbnAccessDenied"))
# The scoping is load-bearing. A bare `.match("` matches 23 strings in the
# CH7465MT capture, most of them jQuery selectors and paths -- including
# $("#cbnLogin"), where cbnLogin is a CSS element id rather than a token.
_RESPONSE_TOKEN = re.compile(r'response\s*(?:\.\s*match\s*\(\s*|==\s*)"([^"]+)"')

# Noise the unscoped pattern would pull in. Asserted against so a widened
# regex fails loudly instead of quietly inflating the token set.
_KNOWN_NOISE = frozenset({"index.html", "HomePage.html", "level2", "level3arrow", "json", "mac", "error"})


def _login_token_set(modem_dir: Path) -> set[str]:
    """Tokens the entry's captured firmware JS compares the login response against.

    Scans every captured body rather than named files: the CH7465MT
    handler is split across common_page/login.html and js/common_api.js,
    and a variant could move it again. The response scoping, not the
    filename, is what keeps unrelated matches out.
    """
    test_data = modem_dir / "test_data"
    if not test_data.is_dir():
        return set()

    tokens: set[str] = set()
    for har_path in sorted(test_data.glob("*.har")):
        har = load_har_json(har_path)
        for entry in har["log"]["entries"]:
            body = (entry.get("response", {}).get("content") or {}).get("text") or ""
            tokens |= set(_RESPONSE_TOKEN.findall(body))
    return tokens


def _is_cbn(modem_yaml_path: Path) -> bool:
    """Whether the entry authenticates with the form_cbn strategy."""
    config = load_modem_config(modem_yaml_path)
    return config.auth is not None and config.auth.strategy == "form_cbn"


def test_login_tokens_are_handled(modem_yaml_path: Path) -> None:
    """No CBN entry's firmware emits a login token the auth manager ignores."""
    if not _is_cbn(modem_yaml_path):
        pytest.skip("not a form_cbn entry")

    tokens = _login_token_set(modem_yaml_path.parent)
    if not tokens:
        pytest.skip("no CBN firmware JS in this entry's captures")

    # "successful" is the success test, not a branch token, and the manager
    # checks it before any of these.
    unhandled = tokens - HANDLED_LOGIN_TOKENS - {"successful"}
    assert not unhandled, (
        f"{modem_yaml_path.parent.name}: firmware JS branches on {sorted(unhandled)}, "
        f"which auth/form_cbn.py routes to the wrong-password branch -> AUTH_FAILED. "
        f"Read the firmware's own handling for each, then add it to the right "
        f"set in auth/form_cbn.py and a row to LOGIN_TOKEN_CASES in "
        f"tests/auth/test_form_cbn.py."
    )


def test_extractor_finds_tokens() -> None:
    """The extractor reads real tokens, so a skip means absent, not broken."""
    catalog = Path(__file__).parent.parent / "solentlabs" / "cable_modem_monitor_catalog" / "modems"
    found: set[str] = set()
    for modem_yaml in sorted(catalog.rglob("modem*.yaml")):
        if _is_cbn(modem_yaml):
            found |= _login_token_set(modem_yaml.parent)

    # The CH7465MT capture is the sole source of the CBN vocabulary; if
    # these stop being found, the extractor broke, not the firmware.
    assert {"lockedout", "cbnAccessDenied", "cbnLogin", "cbnFirstInstall", "cbnBlockContent"} <= found

    # Scoping regression: these appear in the same capture behind a bare
    # `.match("` and must never reach the token set.
    assert not (found & _KNOWN_NOISE)
