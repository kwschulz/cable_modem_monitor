"""Tests for HAR mock server — routes, HTTP auth handlers, and server.

Route builder, normalize_path, auth factory, HTTP handler behavioral
tests, and HTTP server integration. No modem-specific references.

HNAP auth handler and HNAP server integration tests live in
``test_har_mock_server_hnap.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import requests
from solentlabs.cable_modem_monitor_core.test_harness.auth import (
    AuthHandler,
    BasicAuthHandler,
    FormAuthHandler,
    HnapAuthHandler,
    create_auth_handler,
)
from solentlabs.cable_modem_monitor_core.test_harness.routes import (
    RouteEntry,
    build_routes,
    normalize_path,
)
from solentlabs.cable_modem_monitor_core.test_harness.server import HARMockServer, _find_route

from tests._helpers import load_fixture

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_entries(name: str) -> list[dict[str, Any]]:
    """Load HAR entries from a fixture file."""
    data = load_fixture(FIXTURES_DIR / name)
    return list(data["_entries"])


def _make_config(data: dict[str, Any]) -> Any:
    """Validate a raw modem config dict into a ModemConfig instance.

    Fills in required identity fields if missing so tests only need
    to specify the auth/session fields under test.
    """
    from solentlabs.cable_modem_monitor_core.config_loader import validate_modem_config

    defaults = {
        "manufacturer": "Solent Labs",
        "model": "T100",
        "transport": "http",
        "default_host": "192.168.100.1",
        "status": "unsupported",
        "auth": {"strategy": "none"},
    }
    return validate_modem_config({**defaults, **data})


# ---------------------------------------------------------------------------
# Layer 1: Route builder tests (fixture-driven)
# ---------------------------------------------------------------------------


class TestBuildRoutes:
    """Tests for build_routes."""

    def test_basic_route_building(self) -> None:
        """HAR entries produce correct route table."""
        entries = _load_entries("har_entries_no_auth.json")
        routes = build_routes(entries)

        assert ("GET", "/status.html") in routes
        assert ("GET", "/info.html") in routes
        assert routes[("GET", "/status.html")].body == "<html>DS data</html>"
        assert routes[("GET", "/status.html")].status == 200

    def test_post_route(self) -> None:
        """POST entries are routed separately from GET."""
        entries = _load_entries("har_entries_form_auth.json")
        routes = build_routes(entries)

        assert ("POST", "/goform/login") in routes
        assert ("GET", "/status.html") in routes

    def test_duplicate_path_200_wins(self) -> None:
        """For duplicate paths, last 200 response wins."""
        entries = _load_entries("har_entries_duplicate_path.json")
        routes = build_routes(entries)

        assert routes[("GET", "/status.html")].status == 200
        assert routes[("GET", "/status.html")].body == "<html>data</html>"

    def test_non_200_stored_when_no_200(self) -> None:
        """Non-200 response is stored when no 200 exists."""
        entries = _load_entries("har_entries_404_only.json")
        routes = build_routes(entries)

        assert routes[("GET", "/missing.html")].status == 404

    def test_empty_entries(self) -> None:
        """Empty HAR entries produce empty routes."""
        assert build_routes([]) == {}

    def test_entry_without_url(self) -> None:
        """Entries with empty URL are skipped."""
        entries = _load_entries("har_entries_empty_url.json")
        assert build_routes(entries) == {}

    def test_response_headers_preserved(self) -> None:
        """Response headers from HAR are preserved in route."""
        entries = _load_entries("har_entries_form_auth.json")
        routes = build_routes(entries)
        headers = routes[("POST", "/goform/login")].headers

        assert ("Set-Cookie", "session=abc123; Path=/") in headers


# ┌──────────────┬──────────────────────┬──────────┐
# │ input        │ expected             │ desc     │
# ├──────────────┼──────────────────────┼──────────┤
# │ /status.html │ /status.html         │ standard │
# │ status.html  │ /status.html         │ no slash │
# │ ""           │ ""                   │ empty    │
# │ /api/data/   │ /api/data/           │ trailing │
# └──────────────┴──────────────────────┴──────────┘
#
# fmt: off
NORMALIZE_PATH_CASES = [
    # (input,         expected,       description)
    ("/status.html",  "/status.html", "standard path"),
    ("status.html",   "/status.html", "missing leading slash"),
    ("",              "",             "empty path"),
    ("/api/data/",    "/api/data/",   "trailing slash preserved"),
]
# fmt: on


@pytest.mark.parametrize("input_path,expected,desc", NORMALIZE_PATH_CASES)
def test_normalize_path(input_path: str, expected: str, desc: str) -> None:
    """Path normalization: {desc}."""
    assert normalize_path(input_path) == expected


# ---------------------------------------------------------------------------
# _find_route — login-page disambiguation (regression: SB8200 #81)
#
# When a modem's login_page shares a path with parser-fetched data
# (e.g., SB8200 logs in at /cmconnectionstatus.html and the parser
# fetches /cmconnectionstatus.html), the harness must not collapse a
# login GET onto the bare-path data entry. Otherwise the auth manager
# receives the data page as the login response, masking the real
# behavior end-to-end.
# ---------------------------------------------------------------------------


def _route(body: str) -> RouteEntry:
    return RouteEntry(status=200, headers=[], body=body)


class TestFindRouteLoginDisambiguation:
    """Login-page-aware routing for url_token / form modems."""

    def test_login_get_does_not_collapse_to_bare_data_entry(self) -> None:
        """Login GET with query MUST NOT match a bare-path data entry.

        SB8200-shape HAR: login GET (with query, empty body) plus a
        bare-path data entry (the page fetched after auth). A live
        login GET with a different sanitized credential previously
        fell through to the bare data entry via Tier 2 — handing the
        data page back as the login response. With login_page set,
        the route lookup must return the captured login entry only.
        """
        routes = {
            ("GET", "/cmconnectionstatus.html?login_AAAA"): _route("token_value"),
            ("GET", "/cmconnectionstatus.html"): _route("<html>Downstream Bonded Channels</html>"),
        }

        result = _find_route(
            routes,
            method="GET",
            path="/cmconnectionstatus.html",
            route_path="/cmconnectionstatus.html?login_BBBB",
            login_page="/cmconnectionstatus.html",
            token_prefix="ct_",
        )

        assert result is not None
        assert result.body == "token_value"

    def test_token_suffixed_data_fetch_uses_bare_data_entry(self) -> None:
        """Data fetches with ``?ct_<token>`` use the bare-path entry.

        This is the normal Tier 2 fallback for url_token data fetches
        — it must keep working even when the data path equals the
        login page.
        """
        routes = {
            ("GET", "/cmconnectionstatus.html?login_AAAA"): _route("token_value"),
            ("GET", "/cmconnectionstatus.html"): _route("<html>Downstream Bonded Channels</html>"),
        }

        result = _find_route(
            routes,
            method="GET",
            path="/cmconnectionstatus.html",
            route_path="/cmconnectionstatus.html?ct_dynamic_token",
            login_page="/cmconnectionstatus.html",
            token_prefix="ct_",
        )

        assert result is not None
        assert "Downstream Bonded Channels" in result.body

    def test_login_get_with_no_login_entry_returns_none(self) -> None:
        """Login GET with no captured login entry returns None.

        Don't silently substitute the bare data entry — that's the
        bug we're guarding against. A missing login HAR entry should
        surface as a 404 so the test author notices.
        """
        routes = {
            ("GET", "/cmconnectionstatus.html"): _route("<html>data</html>"),
        }

        result = _find_route(
            routes,
            method="GET",
            path="/cmconnectionstatus.html",
            route_path="/cmconnectionstatus.html?login_AAAA",
            login_page="/cmconnectionstatus.html",
            token_prefix="ct_",
        )

        assert result is None

    def test_login_get_without_token_prefix_picks_query_entry(self) -> None:
        """Bare-base64 variant (no token_prefix): any query entry matches login."""
        routes = {
            ("GET", "/cmconnectionstatus.html?YWRtaW46c2FuaXRpemVk"): _route(""),
            ("GET", "/cmconnectionstatus.html"): _route("<html>data</html>"),
        }

        result = _find_route(
            routes,
            method="GET",
            path="/cmconnectionstatus.html",
            route_path="/cmconnectionstatus.html?YWRtaW46bGl2ZQ==",
            login_page="/cmconnectionstatus.html",
            token_prefix="",
        )

        assert result is not None
        assert result.body == ""

    def test_no_login_page_uses_default_tier_lookup(self) -> None:
        """Without login_page set, behavior is unchanged (Tier 2 active)."""
        routes = {
            ("GET", "/data.html"): _route("data"),
        }

        result = _find_route(
            routes,
            method="GET",
            path="/data.html",
            route_path="/data.html?token=abc",
            login_page="",
        )

        assert result is not None
        assert result.body == "data"


# ---------------------------------------------------------------------------
# Layer 2: Auth handler tests (table-driven)
# ---------------------------------------------------------------------------


AUTH_FACTORY_CASES = [
    (None, AuthHandler, FormAuthHandler, "none config"),
    (
        _make_config({"transport": "http"}),
        AuthHandler,
        FormAuthHandler,
        "missing auth key",
    ),
    (
        _make_config({"auth": {"strategy": "none"}}),
        AuthHandler,
        FormAuthHandler,
        "explicit none",
    ),
    (
        _make_config({"auth": {"strategy": "basic"}}),
        BasicAuthHandler,
        None,
        "basic auth",
    ),
    (
        _make_config(
            {
                "auth": {"strategy": "form", "action": "/login", "cookie_name": "s"},
            }
        ),
        FormAuthHandler,
        None,
        "form auth",
    ),
    (
        _make_config({"auth": {"strategy": "form", "action": "/login"}}),
        FormAuthHandler,
        None,
        "form no session",
    ),
    (
        _make_config(
            {
                "auth": {"strategy": "form", "action": "/login"},
                "actions": {
                    "logout": {"type": "http", "method": "GET", "endpoint": "/logout"},
                    "restart": {"type": "http", "method": "POST", "endpoint": "/restart"},
                },
            }
        ),
        FormAuthHandler,
        None,
        "form with actions",
    ),
    (
        _make_config({"transport": "hnap", "auth": {"strategy": "hnap", "hmac_algorithm": "md5"}}),
        HnapAuthHandler,
        None,
        "hnap auth",
    ),
]


@pytest.mark.parametrize("config,expected_type,not_type,desc", AUTH_FACTORY_CASES)
def test_create_auth_handler(
    config: Any,
    expected_type: type,
    not_type: type | None,
    desc: str,
) -> None:
    """Auth factory creates correct handler: {desc}."""
    handler = create_auth_handler(config)
    assert isinstance(handler, expected_type)
    if not_type is not None:
        assert not isinstance(handler, not_type)


class TestAuthHandlerNone:
    """Behavioral tests for the base AuthHandler (no auth)."""

    def test_always_authenticated(self) -> None:
        """No-auth handler always reports authenticated."""
        handler = AuthHandler()
        assert handler.is_authenticated({})

    def test_not_login_request(self) -> None:
        """No-auth handler never identifies a login request."""
        assert not AuthHandler().is_login_request("POST", "/login")

    def test_handle_login_returns_none(self) -> None:
        """No-auth handler returns None from handle_login."""
        assert AuthHandler().handle_login("POST", "/login", b"", {}) is None

    def test_set_authenticated_empty(self) -> None:
        """No-auth handler returns no extra headers."""
        assert AuthHandler().set_authenticated() == {}

    def test_not_restart_request(self) -> None:
        """No-auth handler never identifies a restart request."""
        assert not AuthHandler().is_restart_request("POST", "/restart")

    def test_handle_restart_returns_200(self) -> None:
        """No-auth handler returns 200 from handle_restart."""
        response = AuthHandler().handle_restart()
        assert response.status == 200


class TestFormAuthHandler:
    """Behavioral tests for FormAuthHandler."""

    def test_login_request_detection(self) -> None:
        """POST to login path is detected as login request."""
        handler = FormAuthHandler("/goform/login")
        assert handler.is_login_request("POST", "/goform/login")
        assert not handler.is_login_request("GET", "/goform/login")
        assert not handler.is_login_request("POST", "/other")

    def test_unauthenticated_by_default(self) -> None:
        """New handler starts unauthenticated."""
        assert not FormAuthHandler("/goform/login").is_authenticated({})

    def test_login_sets_authenticated(self) -> None:
        """Successful login sets authenticated state."""
        handler = FormAuthHandler("/goform/login")
        handler.handle_login("POST", "/goform/login", b"user=admin", {})
        assert handler.is_authenticated({})

    def test_cookie_session_headers(self) -> None:
        """Cookie-based session sets Set-Cookie header."""
        handler = FormAuthHandler("/goform/login", cookie_name="session")
        extra = handler.set_authenticated()
        assert "Set-Cookie" in extra
        assert "session=" in extra["Set-Cookie"]

    def test_cookie_authenticates_request(self) -> None:
        """Cookie in request headers authenticates the request."""
        handler = FormAuthHandler("/goform/login", cookie_name="session")
        assert not handler.is_authenticated({})
        assert handler.is_authenticated({"cookie": "session=abc123"})

    def test_ip_session_no_cookie(self) -> None:
        """IP-based session (no cookie_name) returns no Set-Cookie."""
        assert FormAuthHandler("/goform/login").set_authenticated() == {}

    def test_restart_not_detected_without_config(self) -> None:
        """No restart path configured — restart not detected."""
        handler = FormAuthHandler("/goform/login")
        assert not handler.is_restart_request("POST", "/goform/restart")

    def test_restart_request_detection(self) -> None:
        """POST to restart path is detected as restart request."""
        handler = FormAuthHandler("/goform/login", restart_path="/goform/restart")
        assert handler.is_restart_request("POST", "/goform/restart")
        assert not handler.is_restart_request("GET", "/goform/restart")
        assert not handler.is_restart_request("POST", "/other")

    def test_restart_custom_method(self) -> None:
        """Restart with non-POST method (e.g. GET) is detected."""
        handler = FormAuthHandler(
            "/goform/login",
            restart_path="/api/restart",
            restart_method="GET",
        )
        assert handler.is_restart_request("GET", "/api/restart")
        assert not handler.is_restart_request("POST", "/api/restart")

    def test_restart_clears_session(self) -> None:
        """Restart clears session state."""
        handler = FormAuthHandler("/goform/login", restart_path="/goform/restart")
        handler.handle_login("POST", "/goform/login", b"user=admin", {})
        assert handler.is_authenticated({})

        response = handler.handle_restart()
        assert response.status == 200
        assert not handler.is_authenticated({})

    def test_logout_clears_session(self) -> None:
        """Logout clears session state (parity with restart)."""
        handler = FormAuthHandler("/goform/login", logout_path="/goform/logout")
        handler.handle_login("POST", "/goform/login", b"user=admin", {})
        assert handler.is_authenticated({})

        response = handler.handle_logout()
        assert response.status == 200
        assert not handler.is_authenticated({})


class TestBasicAuthHandler:
    """Behavioral tests for BasicAuthHandler."""

    def test_authenticated_with_basic_header(self) -> None:
        """Request with Basic auth header is authenticated."""
        assert BasicAuthHandler().is_authenticated({"authorization": "Basic dXNlcjpwYXNz"})

    def test_unauthenticated_without_header(self) -> None:
        """Request without auth header is not authenticated."""
        assert not BasicAuthHandler().is_authenticated({})

    def test_wrong_scheme(self) -> None:
        """Non-Basic auth scheme is not authenticated."""
        assert not BasicAuthHandler().is_authenticated({"authorization": "Bearer token123"})

    def test_challenge_response_default(self) -> None:
        """Default challenge has WWW-Authenticate but no Set-Cookie."""
        handler = BasicAuthHandler()
        challenge = handler.get_challenge_response()
        assert challenge.status == 401
        header_names = [h[0] for h in challenge.headers]
        assert "WWW-Authenticate" in header_names
        assert "Set-Cookie" not in header_names

    def test_challenge_response_with_cookie(self) -> None:
        """challenge_cookie=True adds Set-Cookie to 401 response."""
        handler = BasicAuthHandler(challenge_cookie=True, cookie_name="XSRF_TOKEN")
        challenge = handler.get_challenge_response()
        assert challenge.status == 401
        header_dict = dict(challenge.headers)
        assert header_dict["WWW-Authenticate"] == 'Basic realm="modem"'
        assert "XSRF_TOKEN=mock-challenge" in header_dict["Set-Cookie"]

    def test_challenge_response_cookie_without_name(self) -> None:
        """challenge_cookie=True but empty cookie_name omits Set-Cookie."""
        handler = BasicAuthHandler(challenge_cookie=True, cookie_name="")
        challenge = handler.get_challenge_response()
        header_names = [h[0] for h in challenge.headers]
        assert "Set-Cookie" not in header_names


# ---------------------------------------------------------------------------
# Layer 3: HTTP server integration tests (fixture-driven)
# ---------------------------------------------------------------------------


class TestHARMockServerNoAuth:
    """Integration tests for mock server with no auth."""

    @pytest.fixture()
    def entries(self) -> list[dict[str, Any]]:
        """Load no-auth HAR entries from fixture."""
        return _load_entries("har_entries_no_auth.json")

    def test_serves_har_responses(self, entries: list[dict[str, Any]]) -> None:
        """Mock server serves recorded HAR responses."""
        with HARMockServer(entries) as server:
            resp = requests.get(f"{server.base_url}/status.html")
            assert resp.status_code == 200
            assert resp.text == "<html>DS data</html>"

    def test_multiple_pages(self, entries: list[dict[str, Any]]) -> None:
        """Mock server serves multiple pages."""
        with HARMockServer(entries) as server:
            resp1 = requests.get(f"{server.base_url}/status.html")
            resp2 = requests.get(f"{server.base_url}/info.html")
            assert resp1.status_code == 200
            assert resp2.status_code == 200
            assert "System info" in resp2.text

    def test_head_returns_status_no_body(self, entries: list[dict[str, Any]]) -> None:
        """HEAD returns 200 with no body — used by connectivity probes."""
        with HARMockServer(entries) as server:
            resp = requests.head(f"{server.base_url}/status.html")
            assert resp.status_code == 200
            assert resp.text == ""

    def test_404_for_unknown_path(self, entries: list[dict[str, Any]]) -> None:
        """Unknown paths return 404."""
        with HARMockServer(entries) as server:
            resp = requests.get(f"{server.base_url}/unknown.html")
            assert resp.status_code == 404

    def test_base_url_format(self, entries: list[dict[str, Any]]) -> None:
        """base_url is http://127.0.0.1:<port>."""
        with HARMockServer(entries) as server:
            assert server.base_url.startswith("http://127.0.0.1:")

    def test_custom_port(self, entries: list[dict[str, Any]]) -> None:
        """Server binds to a specific port when requested."""
        with HARMockServer(entries, port=0) as server:
            # Ephemeral port should be non-zero after binding
            port = server.server_address[1]
            assert port > 0
            assert server.base_url == f"http://127.0.0.1:{port}"

    def test_custom_host_and_port(self, entries: list[dict[str, Any]]) -> None:
        """Server binds to custom host and port."""
        with HARMockServer(entries, host="127.0.0.1", port=0) as server:
            resp = requests.get(f"{server.base_url}/status.html")
            assert resp.status_code == 200
            assert resp.text == "<html>DS data</html>"


class TestHARMockServerFormAuth:
    """Integration tests for mock server with form auth."""

    @pytest.fixture()
    def entries(self) -> list[dict[str, Any]]:
        """Load form-auth HAR entries from fixture."""
        return _load_entries("har_entries_form_auth.json")

    def test_data_pages_require_auth(self, entries: list[dict[str, Any]]) -> None:
        """Data pages return 401 before login."""
        config = _make_config({"auth": {"strategy": "form", "action": "/goform/login"}})
        with HARMockServer(entries, modem_config=config) as server:
            resp = requests.get(f"{server.base_url}/status.html")
            assert resp.status_code == 401

    def test_login_then_data(self, entries: list[dict[str, Any]]) -> None:
        """Login followed by data page request succeeds."""
        config = _make_config({"auth": {"strategy": "form", "action": "/goform/login"}})
        with HARMockServer(entries, modem_config=config) as server:
            login_resp = requests.post(
                f"{server.base_url}/goform/login",
                data="username=admin&password=secret",
            )
            assert login_resp.status_code == 200

            data_resp = requests.get(f"{server.base_url}/status.html")
            assert data_resp.status_code == 200
            assert data_resp.text == "<html>DS data</html>"

    def test_cookie_session(self, entries: list[dict[str, Any]]) -> None:
        """Cookie-based session allows auth via cookie header."""
        config = _make_config(
            {
                "auth": {"strategy": "form", "action": "/goform/login", "cookie_name": "session"},
            }
        )
        with HARMockServer(entries, modem_config=config) as server:
            requests.post(
                f"{server.base_url}/goform/login",
                data="username=admin&password=secret",
            )
            session = requests.Session()
            session.cookies.set("session", "mock-session-token")
            data_resp = session.get(f"{server.base_url}/status.html")
            assert data_resp.status_code == 200


class TestAuthFactoryRestartWiring:
    """Verify create_auth_handler wires restart config correctly."""

    def test_form_auth_with_restart_action(self) -> None:
        """Factory passes restart endpoint and method to FormAuthHandler."""
        config = _make_config(
            {
                "auth": {"strategy": "form", "action": "/goform/login"},
                "actions": {
                    "restart": {"type": "http", "method": "POST", "endpoint": "/goform/restart"},
                },
            }
        )
        handler = create_auth_handler(config)
        assert isinstance(handler, FormAuthHandler)
        assert handler.is_restart_request("POST", "/goform/restart")

    def test_form_auth_with_get_restart(self) -> None:
        """Factory respects non-POST restart method."""
        config = _make_config(
            {
                "auth": {"strategy": "form", "action": "/goform/login"},
                "actions": {
                    "restart": {"type": "http", "method": "GET", "endpoint": "/api/restart"},
                },
            }
        )
        handler = create_auth_handler(config)
        assert isinstance(handler, FormAuthHandler)
        assert handler.is_restart_request("GET", "/api/restart")
        assert not handler.is_restart_request("POST", "/api/restart")

    def test_form_auth_without_actions(self) -> None:
        """Factory without actions — restart not detected."""
        config = _make_config({"auth": {"strategy": "form", "action": "/goform/login"}})
        handler = create_auth_handler(config)
        assert isinstance(handler, FormAuthHandler)
        assert not handler.is_restart_request("POST", "/goform/restart")


class TestHARMockServerBasicAuth:
    """Integration tests for mock server with basic auth."""

    @pytest.fixture()
    def entries(self) -> list[dict[str, Any]]:
        """Load no-auth HAR entries (basic auth doesn't need login entries)."""
        return _load_entries("har_entries_no_auth.json")

    def test_unauthenticated_returns_401(self, entries: list[dict[str, Any]]) -> None:
        """Request without auth header returns 401."""
        config = _make_config({"auth": {"strategy": "basic"}})
        with HARMockServer(entries, modem_config=config) as server:
            resp = requests.get(f"{server.base_url}/status.html")
            assert resp.status_code == 401

    def test_basic_auth_succeeds(self, entries: list[dict[str, Any]]) -> None:
        """Request with Basic auth header succeeds."""
        config = _make_config({"auth": {"strategy": "basic"}})
        with HARMockServer(entries, modem_config=config) as server:
            resp = requests.get(
                f"{server.base_url}/status.html",
                auth=("admin", "pw"),
            )
            assert resp.status_code == 200


class TestHARMockServerActions:
    """Integration tests for mock server with logout and restart actions."""

    @pytest.fixture()
    def entries(self) -> list[dict[str, Any]]:
        """Load form-auth HAR entries with actions."""
        return _load_entries("har_entries_form_auth_actions.json")

    @pytest.fixture()
    def config(self) -> Any:
        """Config with form auth, logout, and restart actions."""
        return _make_config(
            {
                "auth": {"strategy": "form", "action": "/goform/login", "cookie_name": "session"},
                "session": {},
                "actions": {
                    "logout": {"type": "http", "method": "GET", "endpoint": "/goform/logout"},
                    "restart": {"type": "http", "method": "POST", "endpoint": "/goform/restart"},
                },
            }
        )

    def test_login_data_logout_cycle(self, entries: list[dict[str, Any]], config: Any) -> None:
        """Full cycle: login → fetch data → logout → data returns 401."""
        with HARMockServer(entries, modem_config=config) as server:
            # Login
            requests.post(f"{server.base_url}/goform/login", data="user=admin")

            # Fetch data
            resp = requests.get(f"{server.base_url}/status.html")
            assert resp.status_code == 200

            # Logout
            resp = requests.get(f"{server.base_url}/goform/logout")
            assert resp.status_code == 200

            # Data should now require auth again
            resp = requests.get(f"{server.base_url}/status.html")
            assert resp.status_code == 401

    def test_login_data_restart_cycle(self, entries: list[dict[str, Any]], config: Any) -> None:
        """Full cycle: login → fetch data → restart → data returns 401."""
        with HARMockServer(entries, modem_config=config) as server:
            # Login
            requests.post(f"{server.base_url}/goform/login", data="user=admin")

            # Fetch data
            resp = requests.get(f"{server.base_url}/status.html")
            assert resp.status_code == 200

            # Restart
            resp = requests.post(f"{server.base_url}/goform/restart", data="restart=1")
            assert resp.status_code == 200

            # Session cleared — data requires auth again
            resp = requests.get(f"{server.base_url}/status.html")
            assert resp.status_code == 401

    def test_restart_then_relogin(self, entries: list[dict[str, Any]], config: Any) -> None:
        """After restart, a fresh login restores access."""
        with HARMockServer(entries, modem_config=config) as server:
            # Login → restart → session cleared
            requests.post(f"{server.base_url}/goform/login", data="user=admin")
            requests.post(f"{server.base_url}/goform/restart", data="restart=1")
            resp = requests.get(f"{server.base_url}/status.html")
            assert resp.status_code == 401

            # Re-login → data accessible
            requests.post(f"{server.base_url}/goform/login", data="user=admin")
            resp = requests.get(f"{server.base_url}/status.html")
            assert resp.status_code == 200

    def test_restart_without_auth_still_accepted(self, entries: list[dict[str, Any]], config: Any) -> None:
        """Restart endpoint is dispatched before auth gating."""
        with HARMockServer(entries, modem_config=config) as server:
            # No login — restart still accepted (modem restart
            # endpoints typically don't require auth in the mock
            # since the real auth is handled by the collector)
            resp = requests.post(f"{server.base_url}/goform/restart", data="restart=1")
            assert resp.status_code == 200
