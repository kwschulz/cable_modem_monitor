"""A login redirect must land on a page the capture recorded.

Core posts the login with ``allow_redirects=True`` and evaluates
``auth.success.redirect`` against the landing URL, so a capture that stops
at the 3xx cannot replay the auth leg. Two committed fixtures shipped that
way and their replays passed anyway, because declaring success criteria
used to skip the HTTP-error check and a 404 at the matching path read as
a successful login.
"""

from __future__ import annotations

from typing import Any

import pytest
from solentlabs.cable_modem_monitor_catalog_tools.validation.auth_flow import (
    validate_auth_redirect_landing,
)

_HOST = "http://192.168.100.1"
_CREDENTIALS = {"params": [{"name": "username", "value": "admin"}, {"name": "password", "value": ""}]}


def _login(url: str, status: int, location: str | None = None) -> dict[str, Any]:
    """A credential-carrying POST to a login endpoint."""
    return _entry("POST", url, status, location, post_data=_CREDENTIALS)


def _entry(
    method: str,
    url: str,
    status: int,
    location: str | None = None,
    post_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = [{"name": "Location", "value": location}] if location else []
    return {
        "request": {"method": method, "url": url, "postData": post_data or {}},
        "response": {"status": status, "headers": headers},
    }


# ┌────────────────────────────┬──────────────────────────────────────────────┐
# │ case                       │ why                                          │
# ├────────────────────────────┼──────────────────────────────────────────────┤
# │ landing captured, absolute │ the xb7/xb8 Location form, capture complete  │
# │ landing captured, relative │ the xb6/xb10 Location form                   │
# │ landing absent             │ the gap — xb7 and xb8 as committed           │
# │ no redirect                │ login answered in place, nothing to follow   │
# │ redirect without Location  │ nothing to resolve; not our finding          │
# │ chain fully captured       │ multi-hop landing is still a landing         │
# │ chain breaks mid-way       │ the gap can be one hop in                    │
# │ non-login POST redirects   │ an action, not a login — must not fire       │
# └────────────────────────────┴──────────────────────────────────────────────┘

_CASES: list[tuple[str, list[dict[str, Any]], bool]] = [
    (
        "landing captured, absolute",
        [_login(f"{_HOST}/check.jst", 302, "/at_a_glance.jst"), _entry("GET", f"{_HOST}/at_a_glance.jst", 200)],
        False,
    ),
    (
        "landing captured, relative",
        [_login(f"{_HOST}/check.jst", 302, "at_a_glance.jst"), _entry("GET", f"{_HOST}/at_a_glance.jst", 200)],
        False,
    ),
    (
        "landing absent",
        [_login(f"{_HOST}/check.jst", 302, "/at_a_glance.jst")],
        True,
    ),
    (
        "no redirect",
        [_login(f"{_HOST}/login.cgi", 200)],
        False,
    ),
    (
        "redirect without Location",
        [_login(f"{_HOST}/check.jst", 302)],
        False,
    ),
    (
        "chain fully captured",
        [
            _login(f"{_HOST}/check.jst", 302, "/hop.jst"),
            _entry("GET", f"{_HOST}/hop.jst", 302, "/at_a_glance.jst"),
            _entry("GET", f"{_HOST}/at_a_glance.jst", 200),
        ],
        False,
    ),
    (
        "chain breaks mid-way",
        [
            _login(f"{_HOST}/check.jst", 302, "/hop.jst"),
            _entry("GET", f"{_HOST}/hop.jst", 302, "/at_a_glance.jst"),
        ],
        True,
    ),
    (
        "non-login POST redirects",
        [_entry("POST", f"{_HOST}/goform/restart", 302, "/gone.jst")],
        False,
    ),
]


@pytest.mark.parametrize(
    ("entries", "expect_warning"),
    [(c[1], c[2]) for c in _CASES],
    ids=[c[0] for c in _CASES],
)
def test_redirect_landing(entries: list[dict[str, Any]], expect_warning: bool) -> None:
    issues: list[str] = []

    validate_auth_redirect_landing(entries, issues)

    assert bool(issues) is expect_warning, issues


def test_warning_names_the_missing_path() -> None:
    """The message must say which page to recapture, not just that one is missing."""
    issues: list[str] = []

    validate_auth_redirect_landing([_login(f"{_HOST}/check.jst", 302, "/at_a_glance.jst")], issues)

    assert issues[0].startswith("WARNING:")
    assert "/at_a_glance.jst" in issues[0]


def test_redirect_loop_terminates() -> None:
    """A capture whose redirects cycle must not hang the gate."""
    issues: list[str] = []

    validate_auth_redirect_landing(
        [
            _login(f"{_HOST}/check.jst", 302, "/a.jst"),
            _entry("GET", f"{_HOST}/a.jst", 302, "/b.jst"),
            _entry("GET", f"{_HOST}/b.jst", 302, "/a.jst"),
        ],
        issues,
    )

    assert issues == []
