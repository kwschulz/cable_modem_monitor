"""Tests for ModemDataCollector edge cases.

Covers connection failures during auth, HNAP resource loading,
HNAP logout, missing parser config, auth context updates, and
URL token extraction.

These complement test_collector.py which covers signal classification
and basic session lifecycle.
"""

from __future__ import annotations

from typing import Any, Literal
from unittest.mock import MagicMock, patch

import pytest
import requests
from solentlabs.cable_modem_monitor_core.auth.base import AuthContext, AuthResult
from solentlabs.cable_modem_monitor_core.models.modem_config.actions import (
    HnapAction,
)
from solentlabs.cable_modem_monitor_core.models.modem_config.auth import (
    BasicAuth,
    HnapAuth,
    NoneAuth,
)
from solentlabs.cable_modem_monitor_core.models.modem_config.session import SessionConfig
from solentlabs.cable_modem_monitor_core.orchestration.collector import (
    ModemDataCollector,
)
from solentlabs.cable_modem_monitor_core.orchestration.signals import (
    CollectorSignal,
)
from solentlabs.cable_modem_monitor_core.parsers.diagnostics import ParseDiagnostics

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_config(
    *,
    auth_type: str = "none",
    transport: str = "http",
    cookie_name: str = "",
    token_prefix: str = "",
    logout_action: Any = None,
    timeout: int = 10,
    hmac_algorithm: Literal["md5", "sha256"] = "md5",
) -> Any:
    """Build a minimal ModemConfig-like object for testing."""
    config = MagicMock()
    config.transport = transport
    config.timeout = timeout

    if auth_type == "none":
        config.auth = NoneAuth(strategy="none")
    elif auth_type == "basic":
        config.auth = BasicAuth(strategy="basic")
    elif auth_type == "hnap":
        config.auth = HnapAuth(strategy="hnap", hmac_algorithm=hmac_algorithm)
    else:
        config.auth = MagicMock()
        config.auth.strategy = auth_type

    # cookie_name and token_prefix live on auth (auth owns the cookie).
    # Only set on auth types that support them (not NoneAuth/HnapAuth).
    if hasattr(config.auth, "cookie_name") or isinstance(config.auth, MagicMock):
        config.auth.cookie_name = cookie_name
    if hasattr(config.auth, "token_prefix") or isinstance(config.auth, MagicMock):
        config.auth.token_prefix = token_prefix

    config.session = MagicMock()
    config.session.headers = {}
    config.session.query_params = {}
    config.session.post_login_endpoints = []

    if logout_action is not None:
        config.actions = MagicMock()
        config.actions.logout = logout_action
    else:
        config.actions = None

    return config


# ------------------------------------------------------------------
# Tests — session headers from modem config
# ------------------------------------------------------------------


class TestSessionHeaders:
    """Collector passes session headers to auth manager."""

    def test_session_headers_applied(self) -> None:
        """Session headers from modem_config are passed to configure_session."""
        config = MagicMock()
        config.transport = "http"
        config.timeout = 10
        config.auth = NoneAuth(strategy="none")
        config.session = SessionConfig(headers={"X-Custom": "value", "Referer": "{base_url}/"})
        config.actions = None

        collector = ModemDataCollector(config, None, None, "http://localhost", "", "")
        assert collector._session.headers.get("X-Custom") == "value"
        assert collector._session.headers.get("Referer") == "http://localhost/"


# ------------------------------------------------------------------
# Tests — connection failure during auth
# ------------------------------------------------------------------


class TestConnectionFailureDuringAuth:
    """ConnectionError/Timeout during authenticate → CONNECTIVITY signal."""

    def test_connection_error_during_auth(self) -> None:
        """requests.ConnectionError during auth → CONNECTIVITY."""
        config = _make_config(auth_type="basic")
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")

        with patch.object(
            collector,
            "authenticate",
            side_effect=requests.ConnectionError("refused"),
        ):
            result = collector.execute()

        assert result.success is False
        assert result.signal == CollectorSignal.CONNECTIVITY

    def test_timeout_during_auth(self) -> None:
        """requests.Timeout during auth → CONNECTIVITY."""
        config = _make_config(auth_type="basic")
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")

        with patch.object(
            collector,
            "authenticate",
            side_effect=requests.Timeout("timed out"),
        ):
            result = collector.execute()

        assert result.success is False
        assert result.signal == CollectorSignal.CONNECTIVITY


# ------------------------------------------------------------------
# Tests — auth context updates
# ------------------------------------------------------------------


class TestAuthContextUpdate:
    """Auth context is stored on successful authentication."""

    def test_auth_context_saved_on_success(self) -> None:
        """Successful auth stores auth_context from result."""
        config = _make_config(auth_type="basic")
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")

        mock_context = AuthContext(private_key="test_key")
        mock_result = AuthResult(success=True, auth_context=mock_context)

        empty_modem_data = {"downstream": [], "upstream": [], "system_info": {}}
        with (
            patch.object(collector._auth_manager, "authenticate", return_value=mock_result),
            patch.object(collector, "_load_resources", return_value=({}, [])),
            patch.object(
                collector,
                "_parse",
                return_value=(empty_modem_data, ParseDiagnostics()),
            ),
        ):
            result = collector.execute()

        assert result.success is True
        assert collector._auth_context is mock_context


# ------------------------------------------------------------------
# Tests — missing parser config
# ------------------------------------------------------------------


class TestMissingParserConfig:
    """parser_config=None raises RuntimeError in _load_resources."""

    def test_load_resources_no_parser_config(self) -> None:
        """_load_resources raises when parser_config is None."""
        config = _make_config(auth_type="none")
        collector = ModemDataCollector(config, None, None, "http://localhost", "", "")

        mock_result = AuthResult(success=True)
        with pytest.raises(RuntimeError, match="custom parser.py"):
            collector._load_resources(mock_result)

    def test_parse_no_coordinator(self) -> None:
        """_parse raises when coordinator is None (no parser_config)."""
        config = _make_config(auth_type="none")
        collector = ModemDataCollector(config, None, None, "http://localhost", "", "")

        with pytest.raises(RuntimeError, match="No parser coordinator"):
            collector._parse({})


# ------------------------------------------------------------------
# Tests — HNAP resource loading
# ------------------------------------------------------------------


class TestHnapResourceLoading:
    """HNAP transport uses HNAPLoader with correct algorithm."""

    def test_hnap_loader_created_with_md5(self) -> None:
        """HNAP loader uses md5 algorithm from config."""
        config = _make_config(auth_type="hnap", transport="hnap", hmac_algorithm="md5")
        parser_config = MagicMock()
        collector = ModemDataCollector(config, parser_config, None, "http://localhost", "", "pw")

        # Set auth context as if authenticated
        collector._auth_context = AuthContext(private_key="test_private_key")

        with patch("solentlabs.cable_modem_monitor_core.loaders.hnap.HNAPLoader") as mock_loader_cls:
            mock_loader = MagicMock()
            mock_loader.fetch.return_value = {"hnap_data": "ok"}
            mock_loader.resource_fetches = [("/HNAP1/", 50.0, 1024, 200, "text/xml")]
            mock_loader_cls.return_value = mock_loader

            resources, fetches = collector._load_hnap_resources()

        mock_loader_cls.assert_called_once_with(
            session=collector._session,
            base_url="http://localhost",
            private_key="test_private_key",
            hmac_algorithm="md5",
            timeout=10,
            headers=frozenset({"cookie", "hnap_auth"}),
        )
        assert resources == {"hnap_data": "ok"}
        assert len(fetches) == 1
        assert fetches[0].path == "/HNAP1/"

    def test_hnap_loader_with_sha256(self) -> None:
        """HNAP loader uses sha256 when config specifies it."""
        config = _make_config(auth_type="hnap", transport="hnap", hmac_algorithm="sha256")
        parser_config = MagicMock()
        collector = ModemDataCollector(config, parser_config, None, "http://localhost", "", "pw")
        collector._auth_context = AuthContext(private_key="key")

        with patch("solentlabs.cable_modem_monitor_core.loaders.hnap.HNAPLoader") as mock_loader_cls:
            mock_loader_cls.return_value.fetch.return_value = {}
            mock_loader_cls.return_value.resource_fetches = []
            collector._load_hnap_resources()

        assert mock_loader_cls.call_args[1]["hmac_algorithm"] == "sha256"

    def test_hnap_loader_no_auth_context(self) -> None:
        """HNAP loader uses empty private key when no auth context."""
        config = _make_config(auth_type="hnap", transport="hnap")
        parser_config = MagicMock()
        collector = ModemDataCollector(config, parser_config, None, "http://localhost", "", "pw")
        # auth_context is None by default

        with patch("solentlabs.cable_modem_monitor_core.loaders.hnap.HNAPLoader") as mock_loader_cls:
            mock_loader_cls.return_value.fetch.return_value = {}
            mock_loader_cls.return_value.resource_fetches = []
            collector._load_hnap_resources()

        assert mock_loader_cls.call_args[1]["private_key"] == ""


# ------------------------------------------------------------------
# Tests — HNAP logout
# ------------------------------------------------------------------


class TestHnapLogout:
    """HNAP logout action dispatches via execute_action."""

    def test_hnap_logout_called(self) -> None:
        """HNAP logout action dispatches via execute_action."""
        logout_action = HnapAction(
            type="hnap",
            action_name="Logout",
        )
        config = _make_config(
            auth_type="hnap",
            transport="hnap",
            cookie_name="uid",
            logout_action=logout_action,
        )
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "pw")
        collector._auth_context = AuthContext(private_key="test_key")

        modem_data: dict[str, Any] = {"downstream": [], "upstream": [], "system_info": {}}
        with (
            patch.object(collector, "authenticate", return_value=MagicMock(success=True)),
            patch.object(collector, "_load_resources", return_value=({}, [])),
            patch.object(collector, "_parse", return_value=(modem_data, ParseDiagnostics())),
            patch("solentlabs.cable_modem_monitor_core.orchestration.collector.execute_action") as mock_action,
        ):
            result = collector.execute()

        assert result.success is True
        mock_action.assert_called_once()
        # Verify the logout action was passed through
        call_args = mock_action.call_args[0]
        assert call_args[0] is collector
        assert call_args[2] is logout_action


# ------------------------------------------------------------------
# Tests — URL token extraction
# ------------------------------------------------------------------


class TestUrlTokenExtraction:
    """URL token extraction from session cookie."""

    def test_url_token_from_cookie(self) -> None:
        """token_prefix + cookie_name → url_token extracted."""
        config = _make_config(
            auth_type="form",
            cookie_name="auth_token",
            token_prefix="?token=",
        )
        parser_config = MagicMock()
        collector = ModemDataCollector(config, parser_config, None, "http://localhost", "", "")

        # Set cookie in session
        collector._session.cookies.set("auth_token", "abc123")

        mock_result = AuthResult(success=True)
        with patch("solentlabs.cable_modem_monitor_core.orchestration.collector.HTTPResourceLoader") as mock_loader_cls:
            mock_loader = MagicMock()
            mock_loader.fetch.return_value = {}
            mock_loader.resource_fetches = []
            mock_loader_cls.return_value = mock_loader

            with patch(
                "solentlabs.cable_modem_monitor_core.orchestration.collector.collect_fetch_targets", return_value=[]
            ):
                collector._load_http_resources(mock_result)

        # Verify HTTPResourceLoader was called with url_token
        assert mock_loader_cls.call_args[1]["url_token"] == "abc123"
        assert mock_loader_cls.call_args[1]["token_prefix"] == "?token="

    def test_no_url_token_without_prefix(self) -> None:
        """No token_prefix → url_token is empty."""
        config = _make_config(
            auth_type="form",
            cookie_name="auth_token",
            token_prefix="",
        )
        parser_config = MagicMock()
        collector = ModemDataCollector(config, parser_config, None, "http://localhost", "", "")
        collector._session.cookies.set("auth_token", "abc123")

        mock_result = AuthResult(success=True)
        with patch("solentlabs.cable_modem_monitor_core.orchestration.collector.HTTPResourceLoader") as mock_loader_cls:
            mock_loader = MagicMock()
            mock_loader.fetch.return_value = {}
            mock_loader.resource_fetches = []
            mock_loader_cls.return_value = mock_loader

            with patch(
                "solentlabs.cable_modem_monitor_core.orchestration.collector.collect_fetch_targets", return_value=[]
            ):
                collector._load_http_resources(mock_result)

        assert mock_loader_cls.call_args[1]["url_token"] == ""


# ------------------------------------------------------------------
# Tests — HNAP session_is_valid
# ------------------------------------------------------------------


class TestHnapSessionValidity:
    """session_is_valid checks for HNAP transport."""

    def test_hnap_valid_with_cookie_and_key(self) -> None:
        """HNAP session valid when uid cookie and private key present."""
        config = _make_config(auth_type="hnap", transport="hnap")
        collector = ModemDataCollector(config, None, None, "http://localhost", "", "pw")
        collector._auth_context = AuthContext(private_key="some_key")
        collector._session.cookies.set("uid", "session123")
        assert collector.session_is_valid is True

    def test_hnap_invalid_without_cookie(self) -> None:
        """HNAP session invalid without uid cookie."""
        config = _make_config(auth_type="hnap", transport="hnap")
        collector = ModemDataCollector(config, None, None, "http://localhost", "", "pw")
        collector._auth_context = AuthContext(private_key="some_key")
        assert collector.session_is_valid is False

    def test_url_token_valid(self) -> None:
        """Session valid when auth context has url_token."""
        config = _make_config(auth_type="none")
        collector = ModemDataCollector(config, None, None, "http://localhost", "", "")
        collector._auth_context = AuthContext(url_token="token123")
        assert collector.session_is_valid is True


# ------------------------------------------------------------------
# Tests — session reuse flag tracking
# ------------------------------------------------------------------


class TestSessionReusedFlag:
    """_session_reused tracks whether authenticate() reused the session."""

    def test_reused_when_session_valid(self) -> None:
        """authenticate() sets _session_reused=True when session is valid."""
        config = _make_config(auth_type="hnap", transport="hnap")
        collector = ModemDataCollector(config, None, None, "http://localhost", "", "pw")
        # Simulate authenticated HNAP session
        collector._auth_context = AuthContext(private_key="some_key")
        collector._session.cookies.set("uid", "session123")
        collector._last_auth_result = AuthResult(success=True)

        collector.authenticate()
        assert collector._session_reused is True

    def test_not_reused_when_fresh_login(self) -> None:
        """authenticate() sets _session_reused=False on fresh login."""
        config = _make_config(auth_type="hnap", transport="hnap")
        collector = ModemDataCollector(config, None, None, "http://localhost", "", "pw")
        # No auth context — session is not valid

        with patch.object(
            collector._auth_manager,
            "authenticate",
            return_value=AuthResult(
                success=True,
                auth_context=AuthContext(private_key="new_key"),
            ),
        ):
            collector.authenticate()
        assert collector._session_reused is False

    def test_initially_false(self) -> None:
        """_session_reused defaults to False before any authenticate() call."""
        config = _make_config(auth_type="hnap", transport="hnap")
        collector = ModemDataCollector(config, None, None, "http://localhost", "", "pw")
        assert collector._session_reused is False


# ------------------------------------------------------------------
# Tests — auth success logging uses status_code, not Response truthiness
# ------------------------------------------------------------------


class TestAuthSuccessLogging:
    """Auth succeeded log shows actual status code, not 0.

    requests.Response.__bool__() returns False for status >= 400,
    so ``if result.response`` would mask the real status code as 0.
    The collector must use ``result.response is not None`` instead.
    """

    def test_error_status_logged_correctly(self, caplog: pytest.LogCaptureFixture) -> None:
        """Auth result with 4xx response logs actual status code, not 0."""
        config = _make_config(auth_type="basic")
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "pw")

        error_response = requests.Response()
        error_response.status_code = 403
        error_response._content = b"Forbidden"

        with patch.object(
            collector._auth_manager,
            "authenticate",
            return_value=AuthResult(
                success=True,
                response=error_response,
                response_url="/login",
            ),
        ):
            import logging

            with caplog.at_level(logging.DEBUG):
                collector.authenticate()

        auth_log = [r for r in caplog.records if "Auth succeeded" in r.message]
        assert len(auth_log) == 1
        assert "status=403" in auth_log[0].message
        assert "status=0" not in auth_log[0].message

    def test_none_response_logs_zero(self, caplog: pytest.LogCaptureFixture) -> None:
        """Auth result with no response object logs status=0."""
        config = _make_config(auth_type="basic")
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "pw")

        with patch.object(
            collector._auth_manager,
            "authenticate",
            return_value=AuthResult(success=True),
        ):
            import logging

            with caplog.at_level(logging.DEBUG):
                collector.authenticate()

        auth_log = [r for r in caplog.records if "Auth succeeded" in r.message]
        assert len(auth_log) == 1
        assert "status=0" in auth_log[0].message
