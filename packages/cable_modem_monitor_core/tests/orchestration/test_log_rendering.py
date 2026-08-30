"""Rendering tests for the orchestration log adapter.

Covers the wording of lines a user reads to diagnose their own modem,
where the leading phrase is what sends them to the right remedy. The
level policy and event inventory live in LOGGING_SPEC.md.
"""

from __future__ import annotations

import logging

import pytest
from solentlabs.cable_modem_monitor_core.orchestration.events import AuthFailed
from solentlabs.cable_modem_monitor_core.orchestration.logging import log_event


def _unreachable_auth_failure() -> AuthFailed:
    # method is None is how the collector signals "no HTTP response":
    # the modem was never reached, so it judged no credential.
    return AuthFailed(
        model="MB7621",
        strategy="form",
        error="ConnectTimeout: Connection to 192.168.100.1 timed out.",
        method=None,
        url=None,
        status_code=None,
        content_type=None,
        response_body=None,
    )


def test_unreachable_modem_is_not_reported_as_an_auth_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.auth.render")
    with caplog.at_level(logging.WARNING, logger="test.auth.render"):
        log_event(logger, _unreachable_auth_failure())

    line = caplog.text
    # The remedy for an unreachable modem is not a new password, so the
    # line must not lead the user to their credentials (#200).
    assert "Auth failed" not in line
    assert "Connection failed during auth" in line


def test_connection_failure_line_keeps_model_strategy_and_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.auth.render")
    with caplog.at_level(logging.WARNING, logger="test.auth.render"):
        log_event(logger, _unreachable_auth_failure())

    line = caplog.text
    assert "[MB7621]" in line
    assert "strategy=form" in line
    assert "ConnectTimeout" in line


def test_a_judged_credential_still_reads_as_an_auth_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The response branch is unchanged: the modem answered, so "Auth
    # failed" is the accurate phrase and the wire detail follows it.
    event = AuthFailed(
        model="MB7621",
        strategy="form",
        error="Login redirect mismatch",
        method="POST",
        url="http://192.168.100.1/goform/login",
        status_code=401,
        content_type="text/html",
        response_body="denied",
    )
    logger = logging.getLogger("test.auth.render")
    with caplog.at_level(logging.WARNING, logger="test.auth.render"):
        log_event(logger, event)

    assert "Auth failed [MB7621] strategy=form" in caplog.text


def test_log_parser_matches_what_the_adapter_emits(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from solentlabs.cable_modem_monitor_core.analysis.log_parser import CORE_PATTERNS

    logger = logging.getLogger("test.auth.render")
    with caplog.at_level(logging.WARNING, logger="test.auth.render"):
        log_event(logger, _unreachable_auth_failure())

    rendered = caplog.records[0].getMessage()
    line = f"2026-08-30 15:17:32.095 WARNING collector {rendered}"
    match = CORE_PATTERNS["auth_fail"].search(line)
    assert match is not None, "log_parser cannot read the line the adapter emits"
    assert match.group(2) == "MB7621"
