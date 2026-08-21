"""Tests for the ``run_restart`` one-shot command.

Covers the procedure defined in ORCHESTRATION_SPEC.md § Restart
Action: authentication → action execution → session clear → recovery
window. The only emitted error token is ``"command_failed"``.

Use case coverage:
- UC-40: Restart dispatches and opens a recovery window.
- UC-42: Back-to-back restarts are allowed by Core (serialization
  is the consumer's responsibility; see HA_ADAPTER_SPEC § Operation
  Mutex).
- UC-44: ``actions.restart`` absent → ``RestartNotSupportedError``.
- UC-45: Dispatch bypasses the auth circuit breaker (tested in
  test_orchestrator.py).
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from solentlabs.cable_modem_monitor_core.auth.base import LoginLockoutError
from solentlabs.cable_modem_monitor_core.models.modem_config.actions import (
    HttpAction,
)
from solentlabs.cable_modem_monitor_core.orchestration.recovery import Recovery
from solentlabs.cable_modem_monitor_core.orchestration.restart import (
    RestartNotSupportedError,
    run_restart,
)


def _config(*, has_restart: bool = True) -> MagicMock:
    """Build a mock ModemConfig."""
    config = MagicMock()
    config.model = "T100"
    config.timeout = 10
    if has_restart:
        config.actions.restart = HttpAction(
            type="http",
            method="POST",
            endpoint="/restart.htm",
            params={"restart": "1"},
        )
        config.actions.logout = None
    else:
        config.actions = None
    return config


def _collector(auth_success: bool = True) -> MagicMock:
    """Build a mock ModemDataCollector with cooperative defaults."""
    collector = MagicMock()
    collector._session = MagicMock()
    collector._base_url = "http://192.168.100.1"
    collector.authenticate.return_value.success = auth_success
    collector.authenticate.return_value.error = "" if auth_success else "wrong"
    return collector


def _recovery(config: MagicMock) -> Recovery:
    """Build a real Recovery instance bound to the mock config."""
    return Recovery(modem_config=config)


def _make_fresh_session_success(token: str = "tok") -> MagicMock:
    """Fresh session mock that completes login and the action request successfully."""
    fresh = MagicMock()
    fresh.headers = {}
    login_resp = MagicMock()
    login_resp.status_code = 200
    login_resp.json.return_value = {"created": {"token": token}}
    action_resp = MagicMock()
    action_resp.status_code = 200
    fresh.post.return_value = login_resp
    fresh.request.return_value = action_resp
    return fresh


# ------------------------------------------------------------------
# Not-supported guard
# ------------------------------------------------------------------


def test_raises_when_restart_action_absent() -> None:
    config = _config(has_restart=False)
    collector = _collector()
    recovery = _recovery(config)

    with pytest.raises(RestartNotSupportedError):
        run_restart(collector, config, recovery)


# ------------------------------------------------------------------
# Success path
# ------------------------------------------------------------------


def test_success_opens_recovery_window() -> None:
    config = _config()
    collector = _collector()
    recovery = _recovery(config)

    result = run_restart(collector, config, recovery)

    assert result.success is True
    assert result.error == ""
    assert result.elapsed_seconds >= 0
    assert recovery.active is True


def test_success_clears_session_once() -> None:
    config = _config()
    collector = _collector()
    recovery = _recovery(config)

    run_restart(collector, config, recovery)

    # One clear_session call — between action execution and recovery.begin.
    assert collector.clear_session.call_count == 1


def test_success_authenticates_and_executes_action() -> None:
    config = _config()
    collector = _collector()
    recovery = _recovery(config)

    run_restart(collector, config, recovery)

    collector.authenticate.assert_called_once()
    collector._session.request.assert_called_once()


# ------------------------------------------------------------------
# Failure paths — error token must be "command_failed" only
# ------------------------------------------------------------------


def test_auth_failure_returns_command_failed() -> None:
    config = _config()
    collector = _collector(auth_success=False)
    recovery = _recovery(config)

    result = run_restart(collector, config, recovery)

    assert result.success is False
    assert result.error == "command_failed"
    assert recovery.active is False


def test_auth_lockout_returns_command_failed() -> None:
    """HNAP firmware lockout raises rather than returning; restart still
    reports the single ``command_failed`` token (#117)."""
    config = _config()
    collector = _collector()
    collector.authenticate.side_effect = LoginLockoutError("LOCKUP")
    recovery = _recovery(config)

    result = run_restart(collector, config, recovery)

    assert result.success is False
    assert result.error == "command_failed"
    assert recovery.active is False


def test_action_exception_returns_command_failed() -> None:
    config = _config()
    collector = _collector()
    collector._session.request.side_effect = RuntimeError("boom")
    recovery = _recovery(config)

    result = run_restart(collector, config, recovery)

    assert result.success is False
    assert result.error == "command_failed"
    assert recovery.active is False


def test_clear_session_exception_returns_command_failed() -> None:
    config = _config()
    collector = _collector()
    collector.clear_session.side_effect = RuntimeError("broken")
    recovery = _recovery(config)

    result = run_restart(collector, config, recovery)

    assert result.success is False
    assert result.error == "command_failed"
    assert recovery.active is False


# ------------------------------------------------------------------
# Re-entrancy — UC-42 retired behavior
# ------------------------------------------------------------------


def test_restart_while_recovery_active_is_allowed() -> None:
    """Core does not arbitrate. A second restart during a recovery
    window dispatches normally — the caller may want a retry.
    """
    config = _config()
    collector = _collector()
    recovery = _recovery(config)

    result_first = run_restart(collector, config, recovery)
    assert result_first.success is True
    assert recovery.active is True

    # Window is open; caller presses restart again — must not raise.
    result_second = run_restart(collector, config, recovery)
    assert result_second.success is True
    assert recovery.active is True


# ------------------------------------------------------------------
# Per-action auth path (action_auth on HttpAction)
# ------------------------------------------------------------------


def _config_with_action_auth() -> MagicMock:
    """Build a mock ModemConfig with per-action auth on restart."""
    from solentlabs.cable_modem_monitor_core.models.modem_config.auth import BearerAuth

    config = MagicMock()
    config.model = "Hub5"
    config.timeout = 10
    config.actions.restart = HttpAction(
        type="http",
        method="POST",
        endpoint="/rest/v1/system/reboot",
        json_body={"reboot": {"enable": True}},
        action_auth=BearerAuth(
            strategy="bearer",
            login_endpoint="/rest/v1/user/login",
            token_path="created.token",
        ),
    )
    config.actions.logout = None
    return config


_ACTION_AUTH_PATCH = "solentlabs.cable_modem_monitor_core.orchestration.actions.create_session"


def test_action_auth_skips_collector_authenticate() -> None:
    """When action_auth is set, collector.authenticate() is never called."""
    config = _config_with_action_auth()
    collector = _collector()
    recovery = _recovery(config)

    with patch(_ACTION_AUTH_PATCH, return_value=_make_fresh_session_success()):
        result = run_restart(collector, config, recovery)

    assert result.success is True
    collector.authenticate.assert_not_called()


def test_action_auth_still_clears_collector_session() -> None:
    """Session clear still happens even when action_auth skips collector.authenticate."""
    config = _config_with_action_auth()
    collector = _collector()
    recovery = _recovery(config)

    with patch(_ACTION_AUTH_PATCH, return_value=_make_fresh_session_success()):
        run_restart(collector, config, recovery)

    collector.clear_session.assert_called_once()


def test_action_auth_opens_recovery_window() -> None:
    """Per-action auth path still opens a recovery window after success."""
    config = _config_with_action_auth()
    collector = _collector()
    recovery = _recovery(config)

    with patch(_ACTION_AUTH_PATCH, return_value=_make_fresh_session_success()):
        result = run_restart(collector, config, recovery)

    assert result.success is True
    assert recovery.active is True


def test_no_action_auth_calls_collector_authenticate() -> None:
    """Without action_auth, collector.authenticate() is called as normal."""
    config = _config()
    collector = _collector()
    recovery = _recovery(config)

    result = run_restart(collector, config, recovery)

    assert result.success is True
    collector.authenticate.assert_called_once()


# ------------------------------------------------------------------
# A rejected action is a failed restart (issue #82)
# ------------------------------------------------------------------


def _make_fresh_session_login_rejected() -> MagicMock:
    """Fresh session mock whose per-action login is refused."""
    fresh = MagicMock()
    fresh.headers = {}
    login_resp = MagicMock()
    login_resp.status_code = 401
    fresh.post.return_value = login_resp
    return fresh


def test_action_auth_failure_returns_command_failed() -> None:
    """A refused per-action login never sends the reboot, so the restart failed."""
    config = _config_with_action_auth()
    collector = _collector()
    recovery = _recovery(config)

    with patch(_ACTION_AUTH_PATCH, return_value=_make_fresh_session_login_rejected()):
        result = run_restart(collector, config, recovery)

    assert result.success is False
    assert result.error == "command_failed"
    assert recovery.active is False


def test_action_auth_failure_logs_what_refused_it(caplog: pytest.LogCaptureFixture) -> None:
    """The log line is the whole diagnostic path — ``error`` is only a token.

    A user reporting a failed restart pastes this line, so it has to
    name the layer that refused the command. ``command_failed`` alone
    tells them, and us, nothing (#82).
    """
    config = _config_with_action_auth()
    collector = _collector()
    recovery = _recovery(config)

    with (
        patch(_ACTION_AUTH_PATCH, return_value=_make_fresh_session_login_rejected()),
        caplog.at_level(logging.ERROR),
    ):
        run_restart(collector, config, recovery)

    assert "Restart command failed [Hub5]" in caplog.text
    assert "Per-action auth failed" in caplog.text
    assert "401" in caplog.text


def test_action_auth_failure_leaves_monitoring_session_intact() -> None:
    """No reboot was dispatched, so there is no stale-cookie risk to clear against."""
    config = _config_with_action_auth()
    collector = _collector()
    recovery = _recovery(config)

    with patch(_ACTION_AUTH_PATCH, return_value=_make_fresh_session_login_rejected()):
        run_restart(collector, config, recovery)

    collector.clear_session.assert_not_called()


def _collector_refusing_the_command(status: int = 401) -> MagicMock:
    """Collector whose session answers the action request with a refusal."""
    collector = _collector()
    rejected = MagicMock()
    rejected.status_code = status
    rejected.ok = False
    collector._session.request.return_value = rejected
    return collector


def test_rejected_restart_request_returns_command_failed() -> None:
    """The modem answering 401 to the reboot POST is a failed restart."""
    config = _config()
    collector = _collector_refusing_the_command()
    recovery = _recovery(config)

    result = run_restart(collector, config, recovery)

    assert result.success is False
    assert result.error == "command_failed"
    assert recovery.active is False


def test_rejected_restart_request_logs_the_status(caplog: pytest.LogCaptureFixture) -> None:
    """The refusing status reaches the log, where a user can report it."""
    config = _config()
    collector = _collector_refusing_the_command(403)
    recovery = _recovery(config)

    with caplog.at_level(logging.ERROR):
        run_restart(collector, config, recovery)

    assert "Restart command failed [T100]" in caplog.text
    assert "403" in caplog.text
