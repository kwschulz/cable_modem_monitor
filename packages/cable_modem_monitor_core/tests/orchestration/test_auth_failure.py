"""Tests for the login-page-detection gate in ``auth_failure``.

The gate is derived, not hand-written: it reads each strategy's own
``stateless`` and ``transport`` ClassVars. These tests pin the answer
for every strategy in the registry, so adding one cannot silently
inherit a default.

Use case coverage:
- UC-19: Login page detection (whether it runs; the loader behaviour
  once it does is covered in test_collector.py)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from solentlabs.cable_modem_monitor_core.models.modem_config.auth import (
    BasicAuth,
    BearerAuth,
    FormAuth,
    FormCbnAuth,
    FormNonceAuth,
    FormPbkdf2Auth,
    FormSjclAuth,
    HnapAuth,
    NoneAuth,
    UrlTokenAuth,
    get_auth_strategy_rows,
)
from solentlabs.cable_modem_monitor_core.orchestration.auth_failure import (
    _should_detect_login_pages,
)

# Real auth models, not mocks: the gate reads the strategy's own
# `stateless` and `transport` ClassVars, so a MagicMock would answer
# truthy for both and prove nothing.
AUTH_MODELS_BY_STRATEGY: dict[str, Any] = {
    "basic": BasicAuth(strategy="basic"),
    "bearer": BearerAuth(strategy="bearer", login_endpoint="/login", token_path="token"),
    "form": FormAuth(strategy="form", action="/login.htm"),
    "form_cbn": FormCbnAuth(strategy="form_cbn"),
    "form_nonce": FormNonceAuth(strategy="form_nonce", action="/login", nonce_field="nonce"),
    "form_pbkdf2": FormPbkdf2Auth(
        strategy="form_pbkdf2", login_endpoint="/login", pbkdf2_iterations=1000, pbkdf2_key_length=128
    ),
    "form_sjcl": FormSjclAuth(
        strategy="form_sjcl", login_endpoint="/login", pbkdf2_iterations=1000, pbkdf2_key_length=128
    ),
    "hnap": HnapAuth(strategy="hnap", hmac_algorithm="md5"),
    "none": NoneAuth(strategy="none"),
    "url_token": UrlTokenAuth(strategy="url_token", login_page="/login"),
}


def _config_with(auth: Any) -> Any:
    """Build a minimal ModemConfig-like object carrying one auth strategy."""
    config = MagicMock()
    config.auth = auth
    return config


# ┌───────────────┬──────────┬─────────────────────────────────────┐
# │ strategy      │ expected │ why                                 │
# ├───────────────┼──────────┼─────────────────────────────────────┤
# │ "basic"       │ False    │ stateless, credential per request   │
# │ "bearer"      │ True     │ token session over HTTP             │
# │ "form"        │ True     │ cookie session over HTTP            │
# │ "form_cbn"    │ False    │ cbn transport, CBNLoader path       │
# │ "form_nonce"  │ True     │ cookie session over HTTP            │
# │ "form_pbkdf2" │ True     │ cookie session over HTTP            │
# │ "form_sjcl"   │ True     │ cookie session over HTTP            │
# │ "hnap"        │ False    │ hnap transport, HNAPLoader path     │
# │ "none"        │ False    │ stateless, no credential at all     │
# │ "url_token"   │ True     │ token session over HTTP             │
# └───────────────┴──────────┴─────────────────────────────────────┘
#
# fmt: off
LOGIN_PAGE_DETECTION_CASES = [
    # (strategy,     expected)
    ("basic",        False),
    ("bearer",       True),
    ("form",         True),
    ("form_cbn",     False),
    ("form_nonce",   True),
    ("form_pbkdf2",  True),
    ("form_sjcl",    True),
    ("hnap",         False),
    ("none",         False),
    ("url_token",    True),
]
# fmt: on


def test_every_strategy_has_a_case() -> None:
    """A new auth strategy must land in the table above, not default silently."""
    registered = {row.strategy for row in get_auth_strategy_rows()}
    assert {case[0] for case in LOGIN_PAGE_DETECTION_CASES} == registered
    assert set(AUTH_MODELS_BY_STRATEGY) == registered


@pytest.mark.parametrize(
    "strategy,expected",
    LOGIN_PAGE_DETECTION_CASES,
    ids=[case[0] for case in LOGIN_PAGE_DETECTION_CASES],
)
def test_should_detect_login_pages(strategy: str, expected: bool) -> None:
    """Detection runs for stateful HTTP strategies and nothing else."""
    config = _config_with(AUTH_MODELS_BY_STRATEGY[strategy])
    assert _should_detect_login_pages(config) is expected


def test_should_detect_login_pages_without_auth_config() -> None:
    """An unauthenticated config has no strategy to ask, so detection stays off."""
    assert _should_detect_login_pages(_config_with(None)) is False
