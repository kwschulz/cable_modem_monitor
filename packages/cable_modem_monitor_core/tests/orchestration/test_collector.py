"""Tests for ModemDataCollector.

Covers signal classification, session lifecycle, logout, and the
loader's login-page handling. Uses the HAR mock server for
integration tests. The gate that decides *whether* detection runs
lives in ``auth_failure.py`` and is covered by
``test_auth_failure.py``.

Use case coverage (collector level):
- UC-01: First poll — fresh login
- UC-02: Subsequent poll — session reuse
- UC-04: Zero channels with system_info
- UC-06: Single-session modem — logout after poll; logout before auth retry
- UC-17: LOAD_AUTH — 401 on data page
- UC-19: Login page detection
- UC-30: Connection refused — CONNECTIVITY
- UC-33: Parser error — PARSE_ERROR
"""

from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests
from solentlabs.cable_modem_monitor_core.auth.base import AuthContext, AuthResult
from solentlabs.cable_modem_monitor_core.fetch_list import ResourceTarget
from solentlabs.cable_modem_monitor_core.loaders.hnap import HNAPLoadError
from solentlabs.cable_modem_monitor_core.loaders.http import (
    HTTPResourceLoader,
    LoginPageDetectedError,
    ResourceLoadError,
)
from solentlabs.cable_modem_monitor_core.models.modem_config.actions import (
    CbnAction,
    HttpAction,
)
from solentlabs.cable_modem_monitor_core.models.modem_config.auth import (
    BasicAuth,
    NoneAuth,
)
from solentlabs.cable_modem_monitor_core.orchestration.actions.base import ActionResult
from solentlabs.cable_modem_monitor_core.orchestration.collector import (
    LoginLockoutError,
    ModemDataCollector,
)
from solentlabs.cable_modem_monitor_core.orchestration.signals import (
    CollectorSignal,
)
from solentlabs.cable_modem_monitor_core.parsers.diagnostics import (
    AnchorCount,
    ParseDiagnostics,
)

from tests._helpers import load_fixture

# ------------------------------------------------------------------
# Helpers — minimal modem configs as plain objects
# ------------------------------------------------------------------


def _make_config(
    *,
    auth_type: str = "none",
    transport: str = "http",
    cookie_name: str = "",
    logout_endpoint: str = "",
    requires_session: bool = False,
    logout_action: HttpAction | CbnAction | None = None,
    timeout: int = 10,
    post_login_endpoints: list[str] | None = None,
    query_params: dict[str, str] | None = None,
) -> Any:
    """Build a minimal ModemConfig-like object for testing.

    Uses MagicMock to simulate the Pydantic model without needing
    full validation. Only the fields the collector reads are set.
    Pass ``logout_action`` to supply a pre-built action (overrides
    ``logout_endpoint``/``requires_session``).
    """
    config = MagicMock()
    config.transport = transport
    config.timeout = timeout

    # Auth
    if auth_type == "none":
        config.auth = NoneAuth(strategy="none")
    elif auth_type == "form":
        config.auth = MagicMock()
        config.auth.strategy = "form"
        config.auth.action = "/login.htm"
        config.auth.username_field = "username"
        config.auth.password_field = "password"
    elif auth_type == "basic":
        config.auth = BasicAuth(strategy="basic")
    else:
        config.auth = MagicMock()
        config.auth.strategy = auth_type

    # cookie_name lives on auth (auth owns the cookie it produces).
    # NoneAuth and HnapAuth don't have cookie_name — only set on mocks.
    if hasattr(config.auth, "cookie_name") or isinstance(config.auth, MagicMock):
        config.auth.cookie_name = cookie_name

    # Session (lifecycle only: headers, query_params, post-login calls)
    config.session = MagicMock()
    config.session.headers = {}
    config.session.query_params = query_params or {}
    config.session.post_login_endpoints = post_login_endpoints or []

    # Actions — logout_action wins; fall back to building HttpAction from endpoint.
    if logout_action is not None:
        config.actions = MagicMock()
        config.actions.logout = logout_action
    elif logout_endpoint:
        config.actions = MagicMock()
        config.actions.logout = HttpAction(
            type="http",
            method="GET",
            endpoint=logout_endpoint,
            requires_session=requires_session,
        )
    else:
        config.actions = None

    return config


# ------------------------------------------------------------------
# Simple mock server for collector tests
# ------------------------------------------------------------------


class _SimpleHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for collector tests."""

    def do_GET(self) -> None:  # noqa: N802
        """Serve configured responses."""
        server: _SimpleServer = self.server  # type: ignore[assignment]
        path = self.path.split("?")[0]
        # Record the full request target, query string included, so tests
        # can assert session.query_params actually reach the wire.
        server.requested_paths.append(self.path)

        response = server.responses.get(path)
        if response is None:
            self.send_response(404)
            self.end_headers()
            return

        status, body = response
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress logging."""


class _SimpleServer(HTTPServer):
    """Test HTTP server with configurable responses."""

    def __init__(self, responses: dict[str, tuple[int, str]]) -> None:
        self.responses = responses
        # GET targets in arrival order — lets tests assert request sequencing.
        self.requested_paths: list[str] = []
        super().__init__(("127.0.0.1", 0), _SimpleHandler)

    @property
    def base_url(self) -> str:
        """Server base URL."""
        return f"http://127.0.0.1:{self.server_address[1]}"

    def __enter__(self) -> _SimpleServer:
        """Start server in background thread."""
        self._thread = Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        """Stop server."""
        self.shutdown()
        self._thread.join(timeout=5)
        self.server_close()


# ------------------------------------------------------------------
# HTML test data — named constants, not inline in test methods
# ------------------------------------------------------------------

_LOGIN_PAGE_HTML = '<html><form><input type="password" name="pw"></form></html>'
_DATA_PAGE_HTML = "<html><table><tr><td>data</td></tr></table></html>"


# ------------------------------------------------------------------
# Tests — signal classification (table-driven)
# ------------------------------------------------------------------


def _run_collector_with_failure(
    *,
    auth_side_effect: Any = None,
    auth_return: Any = None,
    load_side_effect: Any = None,
    parse_side_effect: Any = None,
) -> Any:
    """Create a collector and execute with controlled failures.

    Patches each pipeline phase. Phases after the failing one are
    patched with success stubs so the failure is isolated.
    """
    config = _make_config(auth_type="none")
    collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")

    auth_patch = (
        patch.object(collector, "authenticate", side_effect=auth_side_effect)
        if auth_side_effect
        else patch.object(
            collector,
            "authenticate",
            return_value=auth_return or MagicMock(success=True),
        )
    )
    load_patch = (
        patch.object(collector, "_load_resources", side_effect=load_side_effect)
        if load_side_effect
        else patch.object(collector, "_load_resources", return_value=({}, []))
    )
    parse_patch = (
        patch.object(collector, "_parse", side_effect=parse_side_effect)
        if parse_side_effect
        else patch.object(
            collector,
            "_parse",
            return_value=({"downstream": [], "upstream": [], "system_info": {}}, ParseDiagnostics()),
        )
    )

    with auth_patch, load_patch, parse_patch:
        return collector.execute()


# ┌──────────┬──────────────────────────┬────────────────┬─────────────────────┐
# │ phase    │ side_effect              │ signal         │ description         │
# ├──────────┼──────────────────────────┼────────────────┼─────────────────────┤
# │ auth     │ AuthResult(False)        │ AUTH_FAILED    │ wrong credentials   │
# │ auth     │ LoginLockoutError        │ AUTH_LOCKOUT   │ firmware lockout    │
# │ load     │ requests.ConnectionError │ CONNECTIVITY   │ connection refused  │
# │ load     │ ResourceLoadError(401)   │ LOAD_AUTH      │ stale session       │
# │ load     │ ResourceLoadError(500)   │ LOAD_ERROR     │ server error        │
# │ load     │ LoginPageDetectedError   │ LOAD_AUTH      │ login page detected │
# │ parse    │ ValueError               │ PARSE_ERROR    │ malformed response  │
# └──────────┴──────────────────────────┴────────────────┴─────────────────────┘
_AUTH_FAIL = {"auth_return": AuthResult(success=False, error="wrong password")}
_AUTH_LOCKOUT = {"auth_side_effect": LoginLockoutError("LOCKUP")}
_LOAD_CONN = {"load_side_effect": requests.ConnectionError("refused")}
_LOAD_401 = {
    "load_side_effect": ResourceLoadError("HTTP 401", status_code=401, path="/d.htm"),
}
_LOAD_500 = {
    "load_side_effect": ResourceLoadError("HTTP 500", status_code=500, path="/d.htm"),
}
_LOAD_LOGIN = {"load_side_effect": LoginPageDetectedError("/d.htm")}
_PARSE_ERR = {"parse_side_effect": ValueError("bad HTML")}

# fmt: off
SIGNAL_CLASSIFICATION_CASES = [
    # (kwargs,       expected_signal,              description)
    (_AUTH_FAIL,     CollectorSignal.AUTH_FAILED,   "wrong credentials"),
    (_AUTH_LOCKOUT,  CollectorSignal.AUTH_LOCKOUT,  "firmware lockout"),
    (_LOAD_CONN,     CollectorSignal.CONNECTIVITY,  "connection refused"),
    (_LOAD_401,      CollectorSignal.LOAD_AUTH,     "stale session (401)"),
    (_LOAD_500,      CollectorSignal.LOAD_ERROR,    "server error (500)"),
    (_LOAD_LOGIN,    CollectorSignal.LOAD_AUTH,     "login page detected"),
    (_PARSE_ERR,     CollectorSignal.PARSE_ERROR,   "malformed response"),
]
# fmt: on


@pytest.mark.parametrize(
    "kwargs,expected_signal,desc",
    SIGNAL_CLASSIFICATION_CASES,
    ids=[c[2] for c in SIGNAL_CLASSIFICATION_CASES],
)
def test_signal_classification(kwargs: dict[str, Any], expected_signal: CollectorSignal, desc: str) -> None:
    """execute() classifies pipeline failures into correct signals."""
    result = _run_collector_with_failure(**kwargs)
    assert result.signal == expected_signal
    assert result.success is False


# ------------------------------------------------------------------
# Tests — HNAP signal classification (table-driven, UC-21/UC-22)
# ------------------------------------------------------------------


def _make_hnap_load_error(
    *,
    status_code: int | None = None,
    cause: Exception | None = None,
) -> HNAPLoadError:
    """Build an HNAPLoadError with controlled attributes."""
    msg = f"HNAP request returned HTTP {status_code}" if status_code else "HNAP request failed"
    err = HNAPLoadError(msg, status_code=status_code)
    if cause is not None:
        err.__cause__ = cause
    return err


def _run_hnap_collector_with_failure(
    *,
    load_error: HNAPLoadError,
    session_reused: bool,
) -> Any:
    """Create an HNAP collector and execute with a controlled HNAP failure.

    Sets _session_reused to simulate whether authenticate() short-circuited.
    Patches _load_resources to raise the given HNAPLoadError.
    """
    config = _make_config(auth_type="hnap", transport="hnap")
    collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "pw")

    with (
        patch.object(
            collector,
            "authenticate",
            return_value=MagicMock(success=True),
        ),
        patch.object(collector, "_load_resources", side_effect=load_error),
        patch.object(
            collector,
            "_parse",
            return_value=({"downstream": [], "upstream": [], "system_info": {}}, ParseDiagnostics()),
        ),
    ):
        collector._session_reused = session_reused
        return collector.execute()


# ┌───────────────────────────────────┬──────────────┬────────────────┬───────────────────────────────┐
# │ HNAPLoadError                     │ sess_reused  │ expected       │ description                   │
# ├───────────────────────────────────┼──────────────┼────────────────┼───────────────────────────────┤
# │ status_code=None + ConnError      │ True         │ CONNECTIVITY   │ UC-30: connection refused      │
# │ status_code=None + Timeout        │ True         │ CONNECTIVITY   │ UC-31: timeout                 │
# │ status_code=401, reused           │ True         │ LOAD_AUTH      │ UC-21: stale session (401)     │
# │ status_code=404, reused           │ True         │ LOAD_AUTH      │ UC-21: S33 stale session (404) │
# │ status_code=500, reused           │ True         │ LOAD_AUTH      │ UC-21: server-side expiry      │
# │ status_code=401, fresh            │ False        │ LOAD_ERROR     │ UC-22: fresh session error     │
# │ status_code=500, fresh            │ False        │ LOAD_ERROR     │ UC-22: fresh session error     │
# │ status_code=None + ValueError     │ True         │ LOAD_ERROR     │ JSON parse error               │
# │ status_code=None + ValueError     │ False        │ LOAD_ERROR     │ JSON parse error (fresh)       │
# └───────────────────────────────────┴──────────────┴────────────────┴───────────────────────────────┘
#
_HNAP_CONN = _make_hnap_load_error(cause=requests.ConnectionError("refused"))
_HNAP_TIMEOUT = _make_hnap_load_error(cause=requests.Timeout("timed out"))
_HNAP_401 = _make_hnap_load_error(status_code=401)
_HNAP_404 = _make_hnap_load_error(status_code=404)
_HNAP_500 = _make_hnap_load_error(status_code=500)
_HNAP_JSON = _make_hnap_load_error(cause=ValueError("No JSON"))

# fmt: off
HNAP_SIGNAL_CASES = [
    # (load_error,  reused, expected,                     description)
    (_HNAP_CONN,    True,   CollectorSignal.CONNECTIVITY, "UC-30: conn refused"),
    (_HNAP_TIMEOUT, True,   CollectorSignal.CONNECTIVITY, "UC-31: timeout"),
    (_HNAP_401,     True,   CollectorSignal.LOAD_AUTH,    "UC-21: stale (401)"),
    (_HNAP_404,     True,   CollectorSignal.LOAD_AUTH,    "UC-21: stale (404)"),
    (_HNAP_500,     True,   CollectorSignal.LOAD_AUTH,    "UC-21: stale (500)"),
    (_HNAP_401,     False,  CollectorSignal.LOAD_ERROR,   "UC-22: fresh 401"),
    (_HNAP_500,     False,  CollectorSignal.LOAD_ERROR,   "UC-22: fresh 500"),
    (_HNAP_JSON,    True,   CollectorSignal.LOAD_ERROR,   "JSON parse (reused)"),
    (_HNAP_JSON,    False,  CollectorSignal.LOAD_ERROR,   "JSON parse (fresh)"),
]
# fmt: on


@pytest.mark.parametrize(
    "load_error,session_reused,expected_signal,desc",
    HNAP_SIGNAL_CASES,
    ids=[c[3] for c in HNAP_SIGNAL_CASES],
)
def test_hnap_signal_classification(
    load_error: HNAPLoadError,
    session_reused: bool,
    expected_signal: CollectorSignal,
    desc: str,
) -> None:
    """HNAP errors route to correct signal based on status code + session reuse (UC-21/UC-22)."""
    result = _run_hnap_collector_with_failure(load_error=load_error, session_reused=session_reused)
    assert result.signal == expected_signal
    assert result.success is False


# ------------------------------------------------------------------
# Tests — session lifecycle (behavioral, inline)
# ------------------------------------------------------------------


class TestSessionIsValid:
    """session_is_valid property checks."""

    def test_no_auth_always_valid(self) -> None:
        """No-auth modems are always valid."""
        config = _make_config(auth_type="none")
        collector = ModemDataCollector(config, None, None, "http://localhost", "", "")
        assert collector.session_is_valid is True

    def test_basic_auth_invalid_before_first_auth(self) -> None:
        """Basic auth requires authenticate() to set session.auth."""
        config = _make_config(auth_type="basic")
        collector = ModemDataCollector(config, None, None, "http://localhost", "", "")
        assert collector.session_is_valid is False

    def test_form_auth_invalid_before_login(self) -> None:
        """Form auth is invalid before first authenticate()."""
        config = _make_config(auth_type="form", cookie_name="sid")
        collector = ModemDataCollector(config, None, None, "http://localhost", "", "")
        assert collector.session_is_valid is False

    def test_clear_session_resets(self) -> None:
        """clear_session() invalidates the session."""
        config = _make_config(auth_type="form", cookie_name="sid")
        collector = ModemDataCollector(config, None, None, "http://localhost", "", "")
        # Simulate authenticated state
        collector._auth_context = MagicMock(url_token="", private_key="")
        collector._session.cookies.set("sid", "abc123")
        assert collector.session_is_valid is True

        collector.clear_session()
        assert collector.session_is_valid is False
        assert collector._auth_context is None

    def test_close_closes_underlying_session(self) -> None:
        """close() releases the requests.Session and its socket pool."""
        config = _make_config(auth_type="none")
        collector = ModemDataCollector(config, None, None, "http://localhost", "", "")

        with patch.object(collector.session, "close") as mock_close:
            collector.close()

        mock_close.assert_called_once_with()

    def test_close_logs_out_live_session_before_closing(self) -> None:
        """A live session is logged out (release the server-side lock) then closed."""
        config = _make_config(auth_type="form", cookie_name="sid")
        collector = ModemDataCollector(config, None, None, "http://localhost", "", "")
        collector._auth_context = MagicMock(url_token="", private_key="")
        collector._session.cookies.set("sid", "abc123")
        assert collector.session_is_valid is True

        manager = MagicMock()
        with (
            patch.object(collector, "_best_effort_logout") as mock_logout,
            patch.object(collector.session, "close") as mock_close,
        ):
            manager.attach_mock(mock_logout, "logout")
            manager.attach_mock(mock_close, "close")
            collector.close()

        # Logout must precede the socket close — the session is needed to log out.
        assert [c[0] for c in manager.mock_calls] == ["logout", "close"]

    def test_close_skips_logout_without_live_session(self) -> None:
        """No live session (never authenticated) → close the socket, no logout."""
        config = _make_config(auth_type="form", cookie_name="sid")
        collector = ModemDataCollector(config, None, None, "http://localhost", "", "")
        assert collector.session_is_valid is False

        with (
            patch.object(collector, "_best_effort_logout") as mock_logout,
            patch.object(collector.session, "close") as mock_close,
        ):
            collector.close()

        mock_logout.assert_not_called()
        mock_close.assert_called_once_with()


# ------------------------------------------------------------------
# Tests — successful collection (behavioral, inline)
# ------------------------------------------------------------------


class TestSuccessfulCollection:
    """execute() returns OK signal with modem data on success."""

    def test_ok_with_channels(self) -> None:
        """Successful parse returns OK with modem data."""
        config = _make_config(auth_type="none")
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")
        modem_data = {
            "downstream": [{"channel_id": 1}],
            "upstream": [{"channel_id": 1}],
            "system_info": {},
        }
        with (
            patch.object(
                collector,
                "authenticate",
                return_value=MagicMock(success=True),
            ),
            patch.object(
                collector,
                "_load_resources",
                return_value=({"data": "ok"}, []),
            ),
            patch.object(
                collector,
                "_parse",
                return_value=(modem_data, ParseDiagnostics()),
            ),
        ):
            result = collector.execute()

        assert result.success is True
        assert result.signal == CollectorSignal.OK
        assert result.modem_data is not None
        assert len(result.modem_data["downstream"]) == 1

    def test_ok_with_zero_channels(self) -> None:
        """Zero channels is a valid success (UC-04)."""
        config = _make_config(auth_type="none")
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")
        modem_data = {
            "downstream": [],
            "upstream": [],
            "system_info": {"firmware": "1.0"},
        }
        with (
            patch.object(
                collector,
                "authenticate",
                return_value=MagicMock(success=True),
            ),
            patch.object(
                collector,
                "_load_resources",
                return_value=({"data": "ok"}, []),
            ),
            patch.object(
                collector,
                "_parse",
                return_value=(modem_data, ParseDiagnostics()),
            ),
        ):
            result = collector.execute()

        assert result.success is True
        assert result.signal == CollectorSignal.OK
        assert result.modem_data is not None
        assert result.modem_data["downstream"] == []


class TestPostLoginEndpoints:
    """session.post_login_endpoints — lifecycle GETs issued after a fresh login."""

    # Recorded into the server's path log by the stubbed load phase, so a
    # single sequence proves the endpoints precede the data fetch.
    _LOAD_MARKER = "<load>"

    _ESTABLISH = "/establish.html"
    _MENU = "/menu.html"

    @staticmethod
    def _make_collector(
        server: _SimpleServer,
        endpoints: list[str],
        query_params: dict[str, str] | None = None,
    ) -> ModemDataCollector:
        """Collector against ``server`` whose login always succeeds."""
        config = _make_config(
            auth_type="form",
            cookie_name="SID",
            post_login_endpoints=endpoints,
            query_params=query_params,
        )
        collector = ModemDataCollector(config, MagicMock(), None, server.base_url, "user", "pw")

        def _login(session: Any, *_args: Any, **_kwargs: Any) -> AuthResult:
            # Set the cookie the real login would — session_is_valid keys
            # off it, so the second execute() takes the reuse path.
            session.cookies.set("SID", "token")
            return AuthResult(success=True, auth_context=AuthContext())

        stub = MagicMock(side_effect=_login)
        collector._auth_manager.authenticate = stub  # type: ignore[method-assign]  # stub must outlive this helper
        return collector

    def _execute(self, collector: ModemDataCollector, server: _SimpleServer) -> Any:
        """Run execute() with the load and parse phases stubbed."""

        def _load(_auth_result: Any) -> tuple[dict[str, Any], list[Any]]:
            server.requested_paths.append(self._LOAD_MARKER)
            return {"data": "ok"}, []

        parsed = ({"downstream": [], "upstream": [], "system_info": {}}, ParseDiagnostics())
        with (
            patch.object(collector, "_load_resources", side_effect=_load),
            patch.object(collector, "_parse", return_value=parsed),
        ):
            return collector.execute()

    def test_endpoints_fetched_in_order_before_data(self) -> None:
        """Declared endpoints are GET in order, ahead of the data fetch."""
        responses = {self._ESTABLISH: (200, "{}"), self._MENU: (200, "{}")}
        with _SimpleServer(responses) as server:
            collector = self._make_collector(server, [self._ESTABLISH, self._MENU])
            result = self._execute(collector, server)

            assert result.success is True
            assert server.requested_paths == [self._ESTABLISH, self._MENU, self._LOAD_MARKER]

    def test_nothing_fetched_when_unconfigured(self) -> None:
        """No extra request is made when no endpoint is declared."""
        with _SimpleServer({}) as server:
            collector = self._make_collector(server, [])
            result = self._execute(collector, server)

            assert result.success is True
            assert server.requested_paths == [self._LOAD_MARKER]

    def test_non_2xx_warns_without_failing_auth(self, caplog: pytest.LogCaptureFixture) -> None:
        """A failed post-login GET logs a WARNING and collection continues.

        Login already succeeded — reporting AUTH_FAILED here would tell
        the user their working credentials are wrong (#120).
        """
        with _SimpleServer({}) as server:  # every path 404s
            collector = self._make_collector(server, [self._ESTABLISH])
            with caplog.at_level(logging.WARNING):
                result = self._execute(collector, server)

            assert result.success is True
            assert result.signal == CollectorSignal.OK
            assert self._ESTABLISH in caplog.text
            assert "404" in caplog.text

    def test_transport_error_warns_without_failing_auth(self, caplog: pytest.LogCaptureFixture) -> None:
        """A connection error on a post-login GET is best-effort, like logout."""
        with _SimpleServer({}) as server:
            collector = self._make_collector(server, [self._ESTABLISH])
            with (
                caplog.at_level(logging.WARNING),
                patch.object(collector._session, "get", side_effect=requests.ConnectionError("refused")),
            ):
                result = self._execute(collector, server)

            assert result.success is True
            assert self._ESTABLISH in caplog.text

    def test_not_refetched_on_session_reuse(self) -> None:
        """Endpoints fire on a fresh login only, not on a reused session."""
        with _SimpleServer({self._ESTABLISH: (200, "{}")}) as server:
            collector = self._make_collector(server, [self._ESTABLISH])
            self._execute(collector, server)
            self._execute(collector, server)

            assert server.requested_paths.count(self._ESTABLISH) == 1

    def test_fetched_by_bare_authenticate(self) -> None:
        """A fresh login fetches them even when no collection follows.

        restart.py authenticates and dispatches its action without ever
        entering execute(), so firmware that needs the call to treat a
        session as established needs it on that path too.
        """
        with _SimpleServer({self._ESTABLISH: (200, "{}")}) as server:
            collector = self._make_collector(server, [self._ESTABLISH])

            collector.authenticate()

            assert server.requested_paths == [self._ESTABLISH]

    def test_carries_session_query_params(self) -> None:
        """Declared session.query_params ride along, as on every other fetch."""
        with _SimpleServer({self._ESTABLISH: (200, "{}")}) as server:
            collector = self._make_collector(server, [self._ESTABLISH], query_params={"_n": "42"})

            collector.authenticate()

            assert server.requested_paths == [f"{self._ESTABLISH}?_n=42"]


class TestPostProcessorResourcesFetch:
    """parser.py resources declarations reach the HTTP loader's fetch list."""

    def test_declared_resources_included_in_http_fetch(self) -> None:
        """Paths declared on the PostProcessor are fetched."""
        from solentlabs.cable_modem_monitor_core.models.parser_config import (
            ParserConfig,
        )

        fixture = Path(__file__).parent.parent / "models" / "fixtures" / "parser_config" / "valid" / "table_single.json"
        parser_config = ParserConfig.model_validate(load_fixture(fixture))

        class PostProcessor:
            resources = {"/extra.json": "json"}

        config = _make_config(auth_type="none")
        collector = ModemDataCollector(config, parser_config, PostProcessor(), "http://localhost", "", "")

        with patch("solentlabs.cable_modem_monitor_core.orchestration.collector.HTTPResourceLoader") as loader_cls:
            loader = loader_cls.return_value
            loader.fetch.return_value = {}
            loader.decode_errors = []
            loader.resource_fetches = []
            collector._load_http_resources(MagicMock())

        targets = loader.fetch.call_args[0][0]
        paths = {t.path for t in targets}
        assert "/extra.json" in paths
        assert len(paths) == 2


# ------------------------------------------------------------------
# Tests — UC-19a stub-page detection (LOAD_INTEGRITY signal)
# ------------------------------------------------------------------


class TestStubPageDetection:
    """execute() emits LOAD_INTEGRITY when parser found 0 of N expected anchors.

    See ORCHESTRATION_USE_CASES.md § UC-19a.
    """

    def test_zero_fulfillment_emits_load_integrity(self) -> None:
        """0 of N anchors fulfilled on a single resource → LOAD_INTEGRITY."""
        config = _make_config(auth_type="none")
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")
        modem_data = {"downstream": [], "upstream": [], "system_info": {"model": "T100"}}
        diagnostics = ParseDiagnostics(by_resource={"/status.html": AnchorCount(expected=4, fulfilled=0)})
        with (
            patch.object(collector, "authenticate", return_value=MagicMock(success=True)),
            patch.object(collector, "_load_resources", return_value=({"data": "ok"}, [])),
            patch.object(collector, "_parse", return_value=(modem_data, diagnostics)),
        ):
            result = collector.execute()

        assert result.success is False
        assert result.signal == CollectorSignal.LOAD_INTEGRITY
        assert "/status.html" in result.error

    def test_full_fulfillment_emits_ok(self) -> None:
        """All anchors fulfilled (even with zero channels) → OK, not LOAD_INTEGRITY (UC-04)."""
        config = _make_config(auth_type="none")
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")
        modem_data = {"downstream": [], "upstream": [], "system_info": {"firmware": "1.0"}}
        diagnostics = ParseDiagnostics(by_resource={"/status.html": AnchorCount(expected=4, fulfilled=4)})
        with (
            patch.object(collector, "authenticate", return_value=MagicMock(success=True)),
            patch.object(collector, "_load_resources", return_value=({"data": "ok"}, [])),
            patch.object(collector, "_parse", return_value=(modem_data, diagnostics)),
        ):
            result = collector.execute()

        assert result.success is True
        assert result.signal == CollectorSignal.OK

    def test_partial_fulfillment_emits_ok(self) -> None:
        """Partial fulfillment is firmware-variant territory, not stub — OK."""
        config = _make_config(auth_type="none")
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")
        modem_data = {"downstream": [{"channel_id": 1}], "upstream": [], "system_info": {}}
        diagnostics = ParseDiagnostics(by_resource={"/status.html": AnchorCount(expected=4, fulfilled=2)})
        with (
            patch.object(collector, "authenticate", return_value=MagicMock(success=True)),
            patch.object(collector, "_load_resources", return_value=({"data": "ok"}, [])),
            patch.object(collector, "_parse", return_value=(modem_data, diagnostics)),
        ):
            result = collector.execute()

        assert result.success is True
        assert result.signal == CollectorSignal.OK

    def test_zero_on_one_of_many_resources_emits_load_integrity(self) -> None:
        """One stub resource among others → still LOAD_INTEGRITY."""
        config = _make_config(auth_type="none")
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")
        modem_data = {"downstream": [], "upstream": [], "system_info": {}}
        diagnostics = ParseDiagnostics(
            by_resource={
                "/data.html": AnchorCount(expected=2, fulfilled=2),
                "/router.html": AnchorCount(expected=4, fulfilled=0),
            }
        )
        with (
            patch.object(collector, "authenticate", return_value=MagicMock(success=True)),
            patch.object(collector, "_load_resources", return_value=({"data": "ok"}, [])),
            patch.object(collector, "_parse", return_value=(modem_data, diagnostics)),
        ):
            result = collector.execute()

        assert result.success is False
        assert result.signal == CollectorSignal.LOAD_INTEGRITY
        assert "/router.html" in result.error
        # Resource with full fulfillment must NOT appear in error
        assert "/data.html" not in result.error


# ------------------------------------------------------------------
# Tests — logout (behavioral, inline)
# ------------------------------------------------------------------


class TestLogout:
    """Logout execution for single-session modems (UC-06)."""

    def test_logout_called_when_configured(self) -> None:
        """Logout action fires after successful collection."""
        config = _make_config(
            auth_type="none",
            cookie_name="sid",
            logout_endpoint="/logout",
        )
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")
        modem_data = {"downstream": [{"channel_id": 1}], "upstream": [], "system_info": {}}

        with (
            patch.object(
                collector,
                "authenticate",
                return_value=MagicMock(success=True),
            ),
            patch.object(
                collector,
                "_load_resources",
                return_value=({}, []),
            ),
            patch.object(
                collector,
                "_parse",
                return_value=(modem_data, ParseDiagnostics()),
            ),
            patch("solentlabs.cable_modem_monitor_core.orchestration.collector.execute_action") as mock_action,
        ):
            result = collector.execute()

        assert result.success is True
        mock_action.assert_called_once()

    def test_logout_failure_does_not_affect_result(self) -> None:
        """Logout is best-effort — failure doesn't change success."""
        config = _make_config(
            auth_type="none",
            cookie_name="sid",
            logout_endpoint="/logout",
        )
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")
        modem_data = {"downstream": [{"channel_id": 1}], "upstream": [], "system_info": {}}

        with (
            patch.object(
                collector,
                "authenticate",
                return_value=MagicMock(success=True),
            ),
            patch.object(
                collector,
                "_load_resources",
                return_value=({}, []),
            ),
            patch.object(
                collector,
                "_parse",
                return_value=(modem_data, ParseDiagnostics()),
            ),
            patch(
                "solentlabs.cable_modem_monitor_core.orchestration.collector.execute_action",
                side_effect=RuntimeError("logout failed"),
            ),
        ):
            result = collector.execute()

        assert result.success is True

    @pytest.mark.parametrize(
        "action_result, session_kept",
        [
            # Clearing after an accepted logout: without it the collector
            # reuses a dead session on the next poll and hits the login
            # page instead of the data page (LOAD_AUTH signal).
            pytest.param(
                ActionResult(success=True, message="Action completed with status 200"),
                False,
                id="accepted",
            ),
            # Keeping it after a refused one: the modem still holds the
            # session, and dropping our cookie orphans it for good. On
            # firmware that permits one login at a time that is one
            # orphaned session per poll, then lockout.
            pytest.param(
                ActionResult(success=False, message="Action refused with status 403"),
                True,
                id="refused",
            ),
        ],
    )
    def test_logout_result_decides_session_clearing(
        self,
        action_result: ActionResult,
        session_kept: bool,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The local session is cleared only when the modem accepted the logout."""
        config = _make_config(
            auth_type="form",
            cookie_name="",
            logout_endpoint="/logout",
        )
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")
        modem_data: dict[str, Any] = {"downstream": [{"channel_id": 1}], "upstream": [], "system_info": {}}

        # Simulate a successful first poll with logout
        auth_result = MagicMock(success=True, auth_context=AuthContext())
        with (
            patch.object(collector, "authenticate", return_value=auth_result),
            patch.object(collector, "_load_resources", return_value=({}, [])),
            patch.object(collector, "_parse", return_value=(modem_data, ParseDiagnostics())),
            patch(
                "solentlabs.cable_modem_monitor_core.orchestration.collector.execute_action",
                return_value=action_result,
            ),
            caplog.at_level(logging.WARNING),
        ):
            # Set auth context as if authentication succeeded
            collector._auth_context = AuthContext()
            collector._last_auth_result = auth_result

            result = collector.execute()

        assert result.success is True
        assert collector.session_is_valid is session_kept
        assert (collector._auth_context is not None) is session_kept
        if session_kept:
            assert "Logout failed" in caplog.text
            assert action_result.message in caplog.text
        else:
            assert "Logout failed" not in caplog.text


# ------------------------------------------------------------------
# Tests — attempt_logout_before_retry (behavioral, inline)
# ------------------------------------------------------------------


_HTTP_LOGOUT_DEFAULT = HttpAction(type="http", method="GET", endpoint="/logout")
_HTTP_LOGOUT_REQUIRES_SESSION = HttpAction(type="http", method="GET", endpoint="/logout", requires_session=True)
_CBN_LOGOUT = CbnAction(type="cbn", fun=16)


# Session states the requires_session guard has to tell apart, as
# (auth_type, cookie_name, set_cookie). The guard reads session_is_valid,
# not the cookie jar: "header" is a live bearer session with an empty jar,
# which a cookie test would have read as no session at all (#185).
_SESSION_STATES: dict[str, tuple[str, str, bool]] = {
    "no_session": ("form", "PHPSESSID", False),
    "cookie_session": ("form", "PHPSESSID", True),
    "header_session": ("bearer", "", False),
}


@pytest.mark.parametrize(
    "logout_action, session_state, expected_fires",
    [
        pytest.param(None, "cookie_session", False, id="no_logout_action"),
        pytest.param(_HTTP_LOGOUT_DEFAULT, "no_session", True, id="http_default_no_session"),
        pytest.param(_HTTP_LOGOUT_DEFAULT, "cookie_session", True, id="http_default_cookie_session"),
        pytest.param(_HTTP_LOGOUT_REQUIRES_SESSION, "no_session", False, id="http_requires_session_guard_fires"),
        pytest.param(_HTTP_LOGOUT_REQUIRES_SESSION, "cookie_session", True, id="http_requires_session_cookie"),
        pytest.param(_HTTP_LOGOUT_REQUIRES_SESSION, "header_session", True, id="http_requires_session_header"),
        # CBN embeds the session token by protocol, so the isinstance guard
        # never fires and logout proceeds regardless of local session state.
        pytest.param(_CBN_LOGOUT, "no_session", True, id="cbn_no_session"),
        pytest.param(_CBN_LOGOUT, "cookie_session", True, id="cbn_cookie_session"),
    ],
)
def test_attempt_logout_before_retry_matrix(
    logout_action: HttpAction | CbnAction | None,
    session_state: str,
    expected_fires: bool,
) -> None:
    """Table-driven guard matrix for attempt_logout_before_retry."""
    auth_type, cookie_name, set_cookie = _SESSION_STATES[session_state]
    config = _make_config(auth_type=auth_type, cookie_name=cookie_name, logout_action=logout_action)
    collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")
    collector._auth_context = AuthContext()
    if set_cookie:
        collector._session.cookies.set(cookie_name, "abc123")

    with patch("solentlabs.cable_modem_monitor_core.orchestration.collector.execute_action") as mock_action:
        collector.attempt_logout_before_retry()

    if expected_fires:
        mock_action.assert_called_once()
    else:
        mock_action.assert_not_called()


class TestAttemptLogoutBeforeRetry:
    """Side-effect and error-handling tests for attempt_logout_before_retry."""

    def test_swallows_execute_action_exception(self) -> None:
        """Exceptions from execute_action are silently swallowed."""
        config = _make_config(logout_endpoint="/logout")
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")

        with patch(
            "solentlabs.cable_modem_monitor_core.orchestration.collector.execute_action",
            side_effect=RuntimeError("connection reset"),
        ):
            collector.attempt_logout_before_retry()  # must not raise

    def test_does_not_clear_session(self) -> None:
        """Does not call clear_session — that is the orchestrator's responsibility."""
        config = _make_config(logout_endpoint="/logout")
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")

        with (
            patch("solentlabs.cable_modem_monitor_core.orchestration.collector.execute_action"),
            patch.object(collector, "clear_session") as mock_clear,
        ):
            collector.attempt_logout_before_retry()

        mock_clear.assert_not_called()


def _login_failure(status: int | None) -> dict[str, Any]:
    """Build an auth_return for a login the modem answered with *status*."""
    response = MagicMock()
    response.status_code = status
    response.url = "http://localhost/rest/v1/user/login"
    response.request.method = "POST"
    response.headers = {"Content-Type": "application/json"}
    response.text = f'{{"status":{status},"message":"Service Unavailable"}}'
    return {
        "auth_return": AuthResult(
            success=False,
            error=f"Login returned HTTP {status}",
            response=response if status is not None else None,
        )
    }


# ┌────────┬─────────────────────┬──────────────────────────────────────┐
# │ status │ expected signal     │ why                                  │
# ├────────┼─────────────────────┼──────────────────────────────────────┤
# │ 401    │ AUTH_FAILED         │ credential examined and rejected     │
# │ 403    │ AUTH_FAILED         │ credential examined and rejected     │
# │ 404    │ AUTH_FAILED         │ login endpoint absent (UC-87b)       │
# │ 500    │ AUTH_UNAVAILABLE    │ modem declined to serve (UC-87a)     │
# │ 502    │ AUTH_UNAVAILABLE    │ modem declined to serve              │
# │ 503    │ AUTH_UNAVAILABLE    │ session slot busy on the F3896LG     │
# │ 504    │ AUTH_UNAVAILABLE    │ modem declined to serve              │
# │ none   │ AUTH_FAILED         │ no response to inspect               │
# └────────┴─────────────────────┴──────────────────────────────────────┘
#
# fmt: off
LOGIN_STATUS_CASES = [
    (401,  CollectorSignal.AUTH_FAILED,      "401-rejected"),
    (403,  CollectorSignal.AUTH_FAILED,      "403-rejected"),
    (404,  CollectorSignal.AUTH_FAILED,      "404-absent-endpoint"),
    (500,  CollectorSignal.AUTH_UNAVAILABLE, "500-declined"),
    (502,  CollectorSignal.AUTH_UNAVAILABLE, "502-declined"),
    (503,  CollectorSignal.AUTH_UNAVAILABLE, "503-busy"),
    (504,  CollectorSignal.AUTH_UNAVAILABLE, "504-declined"),
    (None, CollectorSignal.AUTH_FAILED,      "no-response"),
]
# fmt: on


@pytest.mark.parametrize(
    ("status", "expected"),
    [(c[0], c[1]) for c in LOGIN_STATUS_CASES],
    ids=[c[2] for c in LOGIN_STATUS_CASES],
)
def test_login_failure_signal_by_status(status: int | None, expected: CollectorSignal) -> None:
    """A 5xx login is the modem declining to serve, not a credential verdict (UC-87a)."""
    result = _run_collector_with_failure(**_login_failure(status))
    assert result.signal is expected
    assert result.auth_status_code == status


# ------------------------------------------------------------------
# Tests — login page detection (behavioral, inline — server-based)
# ------------------------------------------------------------------


class TestSessionCreation:
    """Verify ModemDataCollector creates session via create_session().

    The collector must use connectivity.create_session() — not bare
    requests.Session() — so that HTTPS modems with self-signed certs
    get verify=False, and legacy-SSL modems get the LegacySSLAdapter.

    HealthMonitor already does this correctly (modem_health.py).

    Use case coverage:
    - UC-82: HTTPS modem with self-signed certificate
    - UC-83: HTTPS modem with legacy SSL firmware
    """

    def test_session_created_via_factory(self) -> None:
        """Default construction uses create_session(legacy_ssl=False).

        Regression: bare requests.Session() has verify=True, which
        breaks HTTPS modems with self-signed certificates (UC-82).
        """
        config = _make_config(auth_type="none")
        with patch(
            "solentlabs.cable_modem_monitor_core.orchestration.collector.create_session",
        ) as mock_cs:
            mock_session = MagicMock(spec=requests.Session)
            mock_session.headers = {}
            mock_cs.return_value = mock_session

            ModemDataCollector(config, None, None, "http://localhost", "", "")

            mock_cs.assert_called_once_with(legacy_ssl=False)

    def test_legacy_ssl_forwarded(self) -> None:
        """legacy_ssl=True is forwarded to create_session() (UC-83).

        HTTPS modems with old firmware need LegacySSLAdapter mounted
        for cipher negotiation to succeed.
        """
        config = _make_config(auth_type="none")
        with patch(
            "solentlabs.cable_modem_monitor_core.orchestration.collector.create_session",
        ) as mock_cs:
            mock_session = MagicMock(spec=requests.Session)
            mock_session.headers = {}
            mock_cs.return_value = mock_session

            ModemDataCollector(
                config,
                None,
                None,
                "https://192.168.100.1",
                "",
                "",
                legacy_ssl=True,
            )

            mock_cs.assert_called_once_with(legacy_ssl=True)


class TestLoginPageDetection:
    """Login page detection in HTTPResourceLoader."""

    def test_login_page_raises(self) -> None:
        """HTML with password input raises LoginPageDetectedError."""
        responses = {"/data.htm": (200, _LOGIN_PAGE_HTML)}

        with _SimpleServer(responses) as server:
            session = requests.Session()
            loader = HTTPResourceLoader(
                session=session,
                base_url=server.base_url,
                detect_login_pages=True,
            )
            targets = [ResourceTarget(path="/data.htm", format="table", encoding="")]

            with pytest.raises(LoginPageDetectedError) as exc_info:
                loader.fetch(targets)

            assert exc_info.value.path == "/data.htm"

    def test_normal_page_passes(self) -> None:
        """HTML without password input passes through normally."""
        responses = {"/data.htm": (200, _DATA_PAGE_HTML)}

        with _SimpleServer(responses) as server:
            session = requests.Session()
            loader = HTTPResourceLoader(
                session=session,
                base_url=server.base_url,
                detect_login_pages=True,
            )
            targets = [ResourceTarget(path="/data.htm", format="table", encoding="")]

            resources = loader.fetch(targets)
            assert "/data.htm" in resources

    def test_detection_disabled_passes_login_page(self) -> None:
        """With detection disabled, login page is treated as data."""
        responses = {"/data.htm": (200, _LOGIN_PAGE_HTML)}

        with _SimpleServer(responses) as server:
            session = requests.Session()
            loader = HTTPResourceLoader(
                session=session,
                base_url=server.base_url,
                detect_login_pages=False,
            )
            targets = [ResourceTarget(path="/data.htm", format="table", encoding="")]

            # Should NOT raise — detection is disabled
            resources = loader.fetch(targets)
            assert "/data.htm" in resources


# ------------------------------------------------------------------
# Tests — ResourceLoadError attributes (table-driven)
# ------------------------------------------------------------------

# ┌────────┬──────────────────────┬─────────────────────────┐
# │ status │ body                 │ description             │
# ├────────┼──────────────────────┼─────────────────────────┤
# │ 401    │ "Unauthorized"       │ stale session           │
# │ 500    │ "Server Error"       │ server error            │
# └────────┴──────────────────────┴─────────────────────────┘
#
# fmt: off
RESOURCE_LOAD_ERROR_CASES = [
    # (status, body,             description)
    (401,      "Unauthorized",   "stale session (401)"),
    (500,      "Server Error",   "server error (500)"),
]
# fmt: on


@pytest.mark.parametrize(
    "status,body,desc",
    RESOURCE_LOAD_ERROR_CASES,
    ids=[c[2] for c in RESOURCE_LOAD_ERROR_CASES],
)
def test_resource_load_error_attributes(status: int, body: str, desc: str) -> None:
    """ResourceLoadError carries status_code and path from HTTP errors."""
    responses = {"/data.htm": (status, body)}

    with _SimpleServer(responses) as server:
        session = requests.Session()
        loader = HTTPResourceLoader(session=session, base_url=server.base_url)
        targets = [ResourceTarget(path="/data.htm", format="table", encoding="")]

        with pytest.raises(ResourceLoadError) as exc_info:
            loader.fetch(targets)

        assert exc_info.value.status_code == status
        assert exc_info.value.path == "/data.htm"


# ------------------------------------------------------------------
# Tests — mock server logout (behavioral, inline)
# ------------------------------------------------------------------


class TestMockServerLogout:
    """Mock server auth handler logout support."""

    def test_form_handler_logout_clears_session(self) -> None:
        """FormAuthHandler clears session on logout request."""
        from solentlabs.cable_modem_monitor_core.test_harness.auth import (
            FormAuthHandler,
        )
        from solentlabs.cable_modem_monitor_core.test_harness.auth.base import ActionConfig

        handler = FormAuthHandler(login_path="/login.htm", cookie_name="sid")
        handler.configure_actions(
            ActionConfig(
                cookie_name="sid",
                logout_method="GET",
                logout_path="/logout",
                restart_method="POST",
                restart_path="",
            )
        )

        # Authenticate
        handler.handle_login("POST", "/login.htm", b"", {})
        assert handler.is_authenticated({}) is True

        # Logout
        assert handler.is_logout_request("GET", "/logout") is True
        handler.handle_logout()
        assert handler.is_authenticated({}) is False

    def test_base_handler_no_logout(self) -> None:
        """Base AuthHandler has no logout endpoint."""
        from solentlabs.cable_modem_monitor_core.test_harness.auth import AuthHandler

        handler = AuthHandler()
        assert handler.is_logout_request("GET", "/logout") is False

    def test_form_handler_no_logout_path(self) -> None:
        """FormAuthHandler without logout_path returns False."""
        from solentlabs.cable_modem_monitor_core.test_harness.auth import (
            FormAuthHandler,
        )

        handler = FormAuthHandler(login_path="/login.htm")
        assert handler.is_logout_request("GET", "/logout") is False


# ------------------------------------------------------------------
# Tests — session_is_valid edge cases
# ------------------------------------------------------------------


class TestSessionIsValidEdgeCases:
    """Cover session_is_valid branches not reached by main tests."""

    def test_none_auth_config_no_context(self) -> None:
        """auth=None with no auth context returns True."""
        config = _make_config(auth_type="none")
        config.auth = None
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")
        assert collector.session_is_valid is True

    def test_fallthrough_returns_true(self) -> None:
        """Non-HNAP, non-cookie, non-url-token auth returns True."""
        config = _make_config(auth_type="basic")
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "pw")
        # Simulate having authenticated (sets auth_context)
        collector._auth_context = AuthContext(private_key="")
        assert collector.session_is_valid is True


# ------------------------------------------------------------------
# Tests — last_resource_fetches property
# ------------------------------------------------------------------


class TestResourceFetchesProperty:
    """Verify last_resource_fetches surfaces loader timing."""

    def test_empty_before_first_poll(self) -> None:
        """resource_fetches is empty before any collection."""
        config = _make_config(auth_type="none")
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")
        assert collector.last_resource_fetches == []

    def test_populated_after_successful_collection(self) -> None:
        """resource_fetches populated after execute()."""
        from solentlabs.cable_modem_monitor_core.orchestration.models import (
            ResourceFetch,
        )

        config = _make_config(auth_type="none")
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")

        mock_fetches = [ResourceFetch("/status.html", 500.0, 12000)]
        with (
            patch.object(collector, "authenticate", return_value=MagicMock(success=True)),
            patch.object(collector, "_load_resources", return_value=({}, mock_fetches)),
            patch.object(
                collector,
                "_parse",
                return_value=({"downstream": [], "upstream": [], "system_info": {}}, ParseDiagnostics()),
            ),
        ):
            result = collector.execute()

        assert result.success is True
        assert len(collector.last_resource_fetches) == 1
        assert collector.last_resource_fetches[0].path == "/status.html"


class TestSystemInfoFieldOutcomes:
    """Collector exposure of field outcomes (PARSING_SPEC § Field Outcomes).

    missing: snapshot of the most recent parse. failed: retained for
    the runtime once recorded (stub-body retention rationale).
    """

    def _execute_with_diagnostics(self, collector: ModemDataCollector, diagnostics: ParseDiagnostics) -> None:
        modem_data = {"downstream": [], "upstream": [], "system_info": {"model": "T100"}}
        with (
            patch.object(collector, "authenticate", return_value=MagicMock(success=True)),
            patch.object(collector, "_load_resources", return_value=({"data": "ok"}, [])),
            patch.object(collector, "_parse", return_value=(modem_data, diagnostics)),
        ):
            collector.execute()

    def test_missing_is_last_parse_snapshot(self) -> None:
        """missing reflects the most recent parse; a healed field clears."""
        config = _make_config(auth_type="none")
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")

        self._execute_with_diagnostics(collector, ParseDiagnostics(system_info_fields_missing=["system_uptime"]))
        assert collector.last_system_info_fields_missing == ["system_uptime"]

        self._execute_with_diagnostics(collector, ParseDiagnostics())
        assert collector.last_system_info_fields_missing == []

    def test_failed_retained_across_polls(self) -> None:
        """failed entries survive later healthy parses for the runtime."""
        config = _make_config(auth_type="none")
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")

        self._execute_with_diagnostics(
            collector,
            ParseDiagnostics(system_info_fields_failed={"system_uptime": "01/17/2026 14:52:10"}),
        )
        self._execute_with_diagnostics(collector, ParseDiagnostics())

        assert collector.system_info_fields_failed == {"system_uptime": "01/17/2026 14:52:10"}

    def test_failed_property_returns_copy(self) -> None:
        """A held failed dict must not change when later polls record more failures."""
        config = _make_config(auth_type="none")
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")

        self._execute_with_diagnostics(
            collector,
            ParseDiagnostics(system_info_fields_failed={"system_uptime": "01/17/2026 14:52:10"}),
        )
        held = collector.system_info_fields_failed
        self._execute_with_diagnostics(
            collector,
            ParseDiagnostics(system_info_fields_failed={"docsis_status": "garbage"}),
        )

        assert held == {"system_uptime": "01/17/2026 14:52:10"}

    def test_outcomes_empty_before_any_poll(self) -> None:
        """Fresh collector exposes empty outcome channels."""
        config = _make_config(auth_type="none")
        collector = ModemDataCollector(config, MagicMock(), None, "http://localhost", "", "")

        assert collector.last_system_info_fields_missing == []
        assert collector.system_info_fields_failed == {}
