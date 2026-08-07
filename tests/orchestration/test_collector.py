"""Tests for orchestration/collector.py event emission."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import requests
from requests.cookies import RequestsCookieJar
from solentlabs.cable_modem_monitor_core.auth.base import AuthFailureMode
from solentlabs.cable_modem_monitor_core.orchestration.events import (
    AuthFailed,
    AuthSucceeded,
    CollectionComplete,
    ConnectionFailedDuringLoad,
    EventLevel,
    HnapConnectionFailed,
    HnapLoadError,
    HnapSessionExpired,
    HttpStatusError,
    LogoutExecuted,
    LogoutFailed,
    ParseError,
    ResourceDecodeError as ResourceDecodeErrorEvent,
    ResourceFetched,
    ResourceLoadError as ResourceLoadErrorEvent,
    SessionCleared,
    SessionReused,
    StubPageDetected,
)

from tests.fixture_helpers import load_fixture

from .event_capture import assert_event_emitted, capture_events

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_URL = "http://192.168.100.1"
_MODEL = "SB8200"


def _make_modem_config(
    *,
    model: str = _MODEL,
    transport: str = "http",
    auth=None,
    actions=None,
    session_config=None,
    timeout: int = 10,
) -> MagicMock:
    cfg = MagicMock()
    cfg.model = model
    cfg.transport = transport
    cfg.auth = auth if auth is not None else MagicMock(strategy="form", cookie_name="", token_prefix="")
    cfg.actions = actions
    cfg.session = session_config
    cfg.timeout = timeout
    return cfg


def _make_collector(modem_config=None):
    """Build a ModemDataCollector with all external I/O mocked out."""
    from solentlabs.cable_modem_monitor_core.orchestration.collector import ModemDataCollector

    if modem_config is None:
        modem_config = _make_modem_config()

    parser_config = MagicMock()
    post_processor = None

    with (
        patch("solentlabs.cable_modem_monitor_core.orchestration.collector.create_auth_manager") as mock_cam,
        patch("solentlabs.cable_modem_monitor_core.orchestration.collector.create_session") as mock_cs,
        patch("solentlabs.cable_modem_monitor_core.orchestration.collector.ModemParserCoordinator") as mock_coord,
    ):
        mock_cs.return_value = MagicMock(spec=requests.Session)
        auth_manager = MagicMock()
        # Real enum member, not a bare mock: the collector renders its
        # 401/403 hint from this, and the mode is a closed set.
        auth_manager.auth_failure_mode.return_value = AuthFailureMode.CREDENTIALS_SUSPECT
        mock_cam.return_value = auth_manager
        mock_coord.return_value = MagicMock()
        collector = ModemDataCollector(
            modem_config,
            parser_config,
            post_processor,
            _BASE_URL,
            username="admin",
            password="secret",
        )

    # requests.Session.cookies is an instance attr set in Session.__init__, so it's
    # absent from the spec'd mock. Set a real RequestsCookieJar so clear_session()
    # and cookie-presence checks work without AttributeError.
    collector._session.cookies = RequestsCookieJar()

    return collector


def _authenticated_collector():
    """Build a collector whose auth phase succeeds, so execute() reaches the load phase."""
    collector = _make_collector()
    auth_result = MagicMock(success=True, response=None, response_url=None, auth_context=MagicMock())
    cast(MagicMock, collector._auth_manager).authenticate.return_value = auth_result
    collector._auth_context = MagicMock()
    return collector


_FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# SessionReused
# ---------------------------------------------------------------------------


def test_session_reused_emitted_when_session_valid():
    collector = _make_collector()
    collector._auth_context = MagicMock()  # makes session_is_valid return True
    collector._last_auth_result = MagicMock(success=True)
    collector._modem_config.transport = "basic"
    collector._modem_config.auth = MagicMock(strategy="basic", cookie_name="")

    with capture_events() as events:
        collector.authenticate()

    assert_event_emitted(events, SessionReused, model=_MODEL)


# ---------------------------------------------------------------------------
# AuthSucceeded
# ---------------------------------------------------------------------------


def _fresh_login_collector(response_url: str = "/status.html"):
    """Collector whose authenticate() always takes the fresh-login branch.

    The cookie jar stays empty, so session_is_valid keeps returning False
    and repeat calls re-authenticate instead of short-circuiting on reuse.
    """
    collector = _make_collector()
    collector._auth_context = None
    collector._modem_config.auth = MagicMock(strategy="form", cookie_name="sessionid")
    collector._session.cookies = RequestsCookieJar()  # no session cookie → not valid

    resp = MagicMock()
    resp.status_code = 200
    resp.url = _BASE_URL
    auth_result = MagicMock()
    auth_result.success = True
    auth_result.response = resp
    auth_result.response_url = response_url
    auth_result.auth_context = MagicMock()

    cast(MagicMock, collector._auth_manager).authenticate.return_value = auth_result
    return collector


def _only_auth_succeeded(events) -> AuthSucceeded:
    return next(e for e in events if isinstance(e, AuthSucceeded))


def test_auth_succeeded_emitted_on_successful_auth():
    collector = _fresh_login_collector()

    with capture_events() as events:
        collector.authenticate()

    assert_event_emitted(events, AuthSucceeded, model=_MODEL)


def test_auth_succeeded_first_login_is_info():
    """First successful login is the setup-confirmation line (ORCHESTRATION_SPEC § Log Level Tiers)."""
    collector = _fresh_login_collector()

    with capture_events() as events:
        collector.authenticate()

    assert _only_auth_succeeded(events).level == EventLevel.INFO


def test_auth_succeeded_relogin_is_debug():
    """Every login after the first is steady state, so it drops to DEBUG."""
    collector = _fresh_login_collector()
    collector.authenticate()

    with capture_events() as events:
        collector.authenticate()

    assert _only_auth_succeeded(events).level == EventLevel.DEBUG


def test_auth_succeeded_carries_response_url():
    """response_url is the only field that tells an accepted form login from a refused one."""
    collector = _fresh_login_collector(response_url="/login.html")

    with capture_events() as events:
        collector.authenticate()

    assert _only_auth_succeeded(events).response_url == "/login.html"


# ---------------------------------------------------------------------------
# AuthFailed — connection error (no response)
# ---------------------------------------------------------------------------


def test_auth_failed_emitted_on_connection_error():
    collector = _make_collector()
    collector._auth_context = None
    collector._modem_config.auth = MagicMock(strategy="form", cookie_name="sessionid")
    collector._session.cookies = RequestsCookieJar()

    with (
        capture_events() as events,
        patch.object(
            collector._auth_manager,
            "authenticate",
            side_effect=requests.ConnectionError("refused"),
        ),
    ):
        result = collector.execute()

    assert result.success is False
    assert_event_emitted(events, AuthFailed, model=_MODEL)
    event = next(e for e in events if isinstance(e, AuthFailed))
    assert event.method is None
    assert event.url is None
    assert event.level == EventLevel.WARNING


# ---------------------------------------------------------------------------
# AuthFailed — bad response (auth strategy returned failure)
# ---------------------------------------------------------------------------


def test_auth_failed_emitted_with_response_fields():
    collector = _make_collector()
    collector._auth_context = None
    collector._modem_config.auth = MagicMock(strategy="form", cookie_name="sessionid")
    collector._session.cookies = RequestsCookieJar()

    mock_request = MagicMock()
    mock_request.method = "POST"
    mock_response = MagicMock()
    mock_response.request = mock_request
    mock_response.url = f"{_BASE_URL}/login"
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "text/html"}
    mock_response.text = "<html>Login failed</html>"

    auth_result = MagicMock()
    auth_result.success = False
    auth_result.response = mock_response
    auth_result.error = "wrong credentials"

    cast(MagicMock, collector._auth_manager).authenticate.return_value = auth_result

    with capture_events() as events:
        collector.execute()

    assert_event_emitted(events, AuthFailed, model=_MODEL)
    event = next(e for e in events if isinstance(e, AuthFailed))
    assert event.method == "POST"
    assert event.status_code == 200


# ---------------------------------------------------------------------------
# HttpStatusError (401/403 on resource)
# ---------------------------------------------------------------------------


def test_http_status_error_emitted_on_401():
    from solentlabs.cable_modem_monitor_core.loaders.http import ResourceLoadError as LoaderResourceLoadError

    collector = _authenticated_collector()

    with (
        capture_events() as events,
        patch.object(
            collector,
            "_load_resources",
            side_effect=LoaderResourceLoadError("401", status_code=401, path="/status.html"),
        ),
    ):
        result = collector.execute()

    assert result.success is False
    assert_event_emitted(events, HttpStatusError, model=_MODEL)
    event = next(e for e in events if isinstance(e, HttpStatusError))
    assert event.status_code == 401
    assert event.path == "/status.html"
    assert event.level == EventLevel.WARNING


def test_http_status_error_401_carries_wire_detail():
    """A 401 must surface the modem's response body and the request we sent (#120)."""
    from solentlabs.cable_modem_monitor_core.loaders.http import ResourceLoadError as LoaderResourceLoadError

    collector = _authenticated_collector()
    fixture = load_fixture(_FIXTURES / "http_401_wire_detail.json")
    expected = fixture["_expected"]

    with (
        capture_events() as events,
        patch.object(
            collector,
            "_load_resources",
            side_effect=LoaderResourceLoadError("401", **fixture["_load_error"]),
        ),
    ):
        collector.execute()

    event = next(e for e in events if isinstance(e, HttpStatusError))
    assert event.response_body == expected["response_body"]
    assert event.content_type == expected["content_type"]
    assert event.request_line == expected["request_line"]


def test_http_status_error_401_scrubs_password_from_body():
    """The 401 body is modem-controlled text; scrub credentials before logging."""
    from solentlabs.cable_modem_monitor_core.loaders.http import ResourceLoadError as LoaderResourceLoadError

    fixture = load_fixture(_FIXTURES / "http_401_body_echoes_password.json")
    collector = _authenticated_collector()
    collector._password = fixture["_password"]

    with (
        capture_events() as events,
        patch.object(
            collector,
            "_load_resources",
            side_effect=LoaderResourceLoadError("401", **fixture["_load_error"]),
        ),
    ):
        collector.execute()

    event = next(e for e in events if isinstance(e, HttpStatusError))
    assert event.response_body == fixture["_expected"]["response_body"]


# ---------------------------------------------------------------------------
# ResourceLoadError event (non-auth load failure)
# ---------------------------------------------------------------------------


def test_resource_load_error_event_emitted():
    from solentlabs.cable_modem_monitor_core.loaders.http import ResourceLoadError as LoaderResourceLoadError

    collector = _make_collector()
    collector._auth_context = MagicMock()

    with (
        capture_events() as events,
        patch.object(
            collector,
            "_load_resources",
            side_effect=LoaderResourceLoadError("timeout", status_code=None, path="/data.html"),
        ),
    ):
        result = collector.execute()

    assert result.success is False
    assert_event_emitted(events, ResourceLoadErrorEvent, model=_MODEL)
    event = next(e for e in events if isinstance(e, ResourceLoadErrorEvent))
    assert event.level == EventLevel.WARNING


# ---------------------------------------------------------------------------
# ConnectionFailedDuringLoad
# ---------------------------------------------------------------------------


def test_connection_failed_during_load_emitted():
    collector = _make_collector()
    collector._auth_context = MagicMock()

    with (
        capture_events() as events,
        patch.object(collector, "_load_resources", side_effect=requests.ConnectionError("refused")),
    ):
        result = collector.execute()

    assert result.success is False
    assert_event_emitted(events, ConnectionFailedDuringLoad, model=_MODEL)
    event = next(e for e in events if isinstance(e, ConnectionFailedDuringLoad))
    assert event.level == EventLevel.WARNING


# ---------------------------------------------------------------------------
# CollectionComplete
# ---------------------------------------------------------------------------


def test_collection_complete_emitted_on_success():
    collector = _make_collector()
    collector._auth_context = MagicMock()

    modem_data = {"downstream": [1, 2, 3], "upstream": [1, 2]}

    with (
        capture_events() as events,
        patch.object(collector, "_load_resources", return_value=(modem_data, [])),
        patch.object(collector, "_run_parse_phase", return_value=modem_data),
        patch.object(collector, "_execute_logout_if_needed"),
    ):
        result = collector.execute()

    assert result.success is True
    assert_event_emitted(events, CollectionComplete, model=_MODEL)
    event = next(e for e in events if isinstance(e, CollectionComplete))
    assert event.ds_count == 3
    assert event.us_count == 2
    assert event.elapsed_ms >= 0
    assert event.level == EventLevel.DEBUG


# ---------------------------------------------------------------------------
# HNAP errors
# ---------------------------------------------------------------------------


def test_hnap_connection_failed_emitted():
    from solentlabs.cable_modem_monitor_core.loaders.hnap import HNAPLoadError

    collector = _make_collector(_make_modem_config(transport="hnap"))
    collector._auth_context = MagicMock(private_key="key")
    collector._session.cookies = RequestsCookieJar()  # no uid → session_is_valid False → full auth path

    exc = HNAPLoadError("connection refused")
    exc.__cause__ = requests.ConnectionError("refused")

    with (
        capture_events() as events,
        patch.object(collector, "_load_resources", side_effect=exc),
    ):
        result = collector.execute()

    assert result.success is False
    assert_event_emitted(events, HnapConnectionFailed, model=_MODEL)


def test_hnap_session_expired_emitted_on_reused_session():
    from solentlabs.cable_modem_monitor_core.loaders.hnap import HNAPLoadError

    collector = _make_collector(_make_modem_config(transport="hnap"))
    collector._auth_context = MagicMock(private_key="key")
    # Give the session a uid cookie so session_is_valid returns True for HNAP,
    # causing authenticate() to short-circuit and leave _session_reused = True.
    uid_jar = RequestsCookieJar()
    uid_jar.set("uid", "test-uid")
    collector._session.cookies = uid_jar

    exc = HNAPLoadError("HTTP 401", status_code=401)
    exc.__cause__ = None

    with (
        capture_events() as events,
        patch.object(collector, "_load_resources", side_effect=exc),
    ):
        result = collector.execute()

    assert result.success is False
    assert_event_emitted(events, HnapSessionExpired, model=_MODEL)
    event = next(e for e in events if isinstance(e, HnapSessionExpired))
    assert event.status_code == 401


def test_hnap_load_error_emitted_on_fresh_session_error():
    from solentlabs.cable_modem_monitor_core.loaders.hnap import HNAPLoadError

    collector = _make_collector(_make_modem_config(transport="hnap"))
    collector._auth_context = MagicMock(private_key="key")
    # No uid cookie → session_is_valid False for HNAP → authenticate() goes through
    # full auth path → _session_reused = False → fresh-session error path in
    # _classify_hnap_error (status_code=200 not in (401, 403)).
    collector._session.cookies = RequestsCookieJar()

    exc = HNAPLoadError("bad JSON", status_code=200)
    exc.__cause__ = None

    with (
        capture_events() as events,
        patch.object(collector, "_load_resources", side_effect=exc),
    ):
        result = collector.execute()

    assert result.success is False
    assert_event_emitted(events, HnapLoadError, model=_MODEL)


# ---------------------------------------------------------------------------
# ParseError
# ---------------------------------------------------------------------------


def test_parse_error_emitted():
    collector = _make_collector()
    collector._auth_context = MagicMock()

    with (
        capture_events() as events,
        patch.object(collector, "_load_resources", return_value=({}, [])),
        patch.object(collector, "_parse", side_effect=ValueError("unexpected structure")),
    ):
        result = collector.execute()

    assert result.success is False
    assert_event_emitted(events, ParseError, model=_MODEL)
    event = next(e for e in events if isinstance(e, ParseError))
    assert "unexpected structure" in event.reason
    assert event.level == EventLevel.WARNING


# ---------------------------------------------------------------------------
# StubPageDetected
# ---------------------------------------------------------------------------


def test_stub_page_detected_emitted_on_zero_fulfillment():
    from solentlabs.cable_modem_monitor_core.parsers.diagnostics import AnchorCount, ParseDiagnostics

    collector = _make_collector()
    collector._auth_context = MagicMock()

    diagnostics = MagicMock(spec=ParseDiagnostics)
    diagnostics.has_zero_fulfillment = True
    diagnostics.zero_fulfillment_resources = ["/status.html"]
    diagnostics.by_resource = {"/status.html": AnchorCount(fulfilled=0, expected=5)}

    with (
        capture_events() as events,
        patch.object(collector, "_load_resources", return_value=({"status.html": MagicMock()}, [])),
        patch.object(collector, "_parse", return_value=({"downstream": []}, diagnostics)),
    ):
        result = collector.execute()

    assert result.success is False
    assert_event_emitted(events, StubPageDetected, model=_MODEL)
    event = next(e for e in events if isinstance(e, StubPageDetected))
    assert event.anchors_found == 0
    assert event.anchors_expected == 5
    assert event.level == EventLevel.WARNING


# ---------------------------------------------------------------------------
# Logout events
# ---------------------------------------------------------------------------


def _make_collector_with_logout(logout_action=True):
    cfg = _make_modem_config()
    cfg.session = MagicMock()
    if logout_action:
        cfg.actions = MagicMock()
        cfg.actions.logout = MagicMock()
    else:
        cfg.actions = None
    return _make_collector(cfg)


def test_logout_executed_emitted():
    collector = _make_collector_with_logout()
    collector._auth_context = MagicMock()

    modem_data = {"downstream": [], "upstream": []}

    with (
        capture_events() as events,
        patch.object(collector, "_load_resources", return_value=(modem_data, [])),
        patch.object(collector, "_run_parse_phase", return_value=modem_data),
        patch("solentlabs.cable_modem_monitor_core.orchestration.collector.execute_action"),
    ):
        collector.execute()

    assert_event_emitted(events, LogoutExecuted, model=_MODEL)
    assert any(isinstance(e, SessionCleared) for e in events)


def test_logout_failed_emitted_on_exception():
    collector = _make_collector_with_logout()
    collector._auth_context = MagicMock()

    modem_data = {"downstream": [], "upstream": []}

    with (
        capture_events() as events,
        patch.object(collector, "_load_resources", return_value=(modem_data, [])),
        patch.object(collector, "_run_parse_phase", return_value=modem_data),
        patch(
            "solentlabs.cable_modem_monitor_core.orchestration.collector.execute_action",
            side_effect=RuntimeError("action failed"),
        ),
    ):
        collector.execute()

    assert_event_emitted(events, LogoutFailed, model=_MODEL)
    assert not any(isinstance(e, LogoutExecuted) for e in events)
    event = next(e for e in events if isinstance(e, LogoutFailed))
    assert event.level == EventLevel.WARNING


# ---------------------------------------------------------------------------
# ResourceFetched
# ---------------------------------------------------------------------------


def test_resource_fetched_emitted_per_page():
    from solentlabs.cable_modem_monitor_core.orchestration.models import ResourceFetch

    collector = _make_collector()
    collector._auth_context = MagicMock()

    fetches = [
        ResourceFetch(path="/status.html", duration_ms=45.0, size_bytes=2048, status_code=200),
        ResourceFetch(path="/connection.html", duration_ms=32.0, size_bytes=1024, status_code=200),
    ]
    modem_data = {"downstream": [], "upstream": []}

    with (
        capture_events() as events,
        patch.object(collector, "_load_resources", return_value=(modem_data, fetches)),
        patch.object(collector, "_run_parse_phase", return_value=modem_data),
        patch.object(collector, "_execute_logout_if_needed"),
    ):
        result = collector.execute()

    assert result.success is True
    fetched = [e for e in events if isinstance(e, ResourceFetched)]
    assert len(fetched) == 2
    assert fetched[0].path == "/status.html"
    assert fetched[0].elapsed_ms == 45.0
    assert fetched[0].status_code == 200
    assert fetched[0].level == EventLevel.DEBUG


# ---------------------------------------------------------------------------
# ResourceDecodeError
# ---------------------------------------------------------------------------


def test_resource_decode_error_emitted():
    from solentlabs.cable_modem_monitor_core.auth.base import AuthResult

    collector = _make_collector()

    mock_loader = MagicMock()
    mock_loader.fetch.return_value = {}
    mock_loader.resource_fetches = []
    mock_loader.decode_errors = [("/status.html", "json", "invalid JSON")]

    with (
        capture_events() as events,
        patch(
            "solentlabs.cable_modem_monitor_core.orchestration.collector.HTTPResourceLoader",
            return_value=mock_loader,
        ),
        patch(
            "solentlabs.cable_modem_monitor_core.orchestration.collector.collect_fetch_targets",
            return_value=[],
        ),
    ):
        collector._load_http_resources(AuthResult(success=True))

    assert_event_emitted(events, ResourceDecodeErrorEvent, model=_MODEL)
    event = next(e for e in events if isinstance(e, ResourceDecodeErrorEvent))
    assert event.path == "/status.html"
    assert event.fmt == "json"
    assert event.reason == "invalid JSON"
    assert event.level == EventLevel.WARNING
