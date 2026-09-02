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
from solentlabs.cable_modem_monitor_core.test_harness.auth.base import ActionConfig
from solentlabs.cable_modem_monitor_core.test_harness.routes import (
    RouteEntry,
    build_routes,
    normalize_path,
)
from solentlabs.cable_modem_monitor_core.test_harness.server import (
    _UNRESOLVED_PLACEHOLDER_RE,
    HARMockServer,
    _find_route,
)

from tests._helpers import load_fixture

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_entries(name: str) -> list[dict[str, Any]]:
    """Load HAR entries from a fixture file."""
    data = load_fixture(FIXTURES_DIR / name)
    return list(data["_entries"])


def _with_actions(
    handler: AuthHandler,
    *,
    logout_path: str = "",
    logout_method: str = "GET",
    restart_path: str = "",
    restart_method: str = "POST",
) -> Any:
    """Configure a directly-constructed handler's action endpoints.

    The factory does this from modem.yaml; unit tests that build a
    handler without one configure it here.
    """
    handler.configure_actions(
        ActionConfig(
            cookie_name="",
            logout_method=logout_method,
            logout_path=logout_path,
            restart_method=restart_method,
            restart_path=restart_path,
        )
    )
    return handler


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

    def test_later_exchange_wins_among_non_200(self) -> None:
        """Two non-200 answers to one key: the later exchange is the route.

        The contributor capture steps put the deliberate wrong-password
        login before the real one, so a bare-path firmware records two
        302s under the same key and the first is the refusal.
        """
        entries = [
            {
                "request": {"method": "POST", "url": "http://192.168.100.1/goform/login"},
                "response": {"status": 302, "headers": [{"name": "Location", "value": "/Login.htm"}], "content": {}},
            },
            {
                "request": {"method": "POST", "url": "http://192.168.100.1/goform/login"},
                "response": {"status": 302, "headers": [{"name": "Location", "value": "/index.htm"}], "content": {}},
            },
        ]
        assert build_routes(entries)[("POST", "/goform/login")].headers == [("Location", "/index.htm")]

    def test_200_is_not_displaced_by_a_later_non_200(self) -> None:
        """A later failure never replaces a captured success for the same key."""
        entries = [
            {
                "request": {"method": "GET", "url": "http://192.168.100.1/status.html"},
                "response": {"status": 200, "headers": [], "content": {"text": "<html>data</html>"}},
            },
            {
                "request": {"method": "GET", "url": "http://192.168.100.1/status.html"},
                "response": {"status": 500, "headers": [], "content": {"text": "boom"}},
            },
        ]
        assert build_routes(entries)[("GET", "/status.html")].status == 200

    def test_login_post_keeps_the_last_exchange_over_a_200(self) -> None:
        """On the login path a 200 refusal does not outrank a later redirect.

        The XB8 re-renders the login form with 200 when the password is
        wrong and answers the accepted login with a 302 (#194). The
        status preference would replay the refusal as the login.
        """
        entries = [
            {
                "request": {"method": "POST", "url": "http://192.168.100.1/check.jst"},
                "response": {"status": 200, "headers": [], "content": {"text": "<html>login form</html>"}},
            },
            {
                "request": {"method": "POST", "url": "http://192.168.100.1/check.jst"},
                "response": {
                    "status": 302,
                    "headers": [{"name": "location", "value": "at_a_glance.jst"}],
                    "content": {},
                },
            },
        ]
        routes = build_routes(entries, login_path="/check.jst")
        assert routes[("POST", "/check.jst")].status == 302

    def test_login_exemption_does_not_reach_other_paths(self) -> None:
        """Naming a login path leaves the status preference on every other key."""
        entries = [
            {
                "request": {"method": "GET", "url": "http://192.168.100.1/status.html"},
                "response": {"status": 200, "headers": [], "content": {"text": "<html>data</html>"}},
            },
            {
                "request": {"method": "GET", "url": "http://192.168.100.1/status.html"},
                "response": {"status": 401, "headers": [], "content": {"text": "Unauthorized"}},
            },
        ]
        routes = build_routes(entries, login_path="/check.jst")
        assert routes[("GET", "/status.html")].status == 200

    def test_login_exemption_does_not_reach_another_post(self) -> None:
        """A POST to any other path keeps the status preference."""
        entries = [
            {
                "request": {"method": "POST", "url": "http://192.168.100.1/goform/reboot"},
                "response": {"status": 200, "headers": [], "content": {"text": "ok"}},
            },
            {
                "request": {"method": "POST", "url": "http://192.168.100.1/goform/reboot"},
                "response": {"status": 500, "headers": [], "content": {"text": "boom"}},
            },
        ]
        routes = build_routes(entries, login_path="/check.jst")
        assert routes[("POST", "/goform/reboot")].status == 200

    def test_entry_without_a_response_is_not_a_route(self) -> None:
        """A request that never got a response cannot be replayed as one.

        har-capture records status -1 when the modem tore the
        connection down mid-request. Routing it served ``-1`` as an
        HTTP status; the request must fall through instead.
        """
        entries = [
            {
                "request": {"method": "GET", "url": "http://192.168.100.1/logout.html"},
                "response": {"status": -1, "headers": [], "content": {"text": ""}},
            }
        ]
        assert build_routes(entries) == {}

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
        handler = _with_actions(FormAuthHandler("/goform/login"), restart_path="/goform/restart")
        assert handler.is_restart_request("POST", "/goform/restart")
        assert not handler.is_restart_request("GET", "/goform/restart")
        assert not handler.is_restart_request("POST", "/other")

    def test_restart_custom_method(self) -> None:
        """Restart with non-POST method (e.g. GET) is detected."""
        handler = _with_actions(
            FormAuthHandler("/goform/login"),
            restart_path="/api/restart",
            restart_method="GET",
        )
        assert handler.is_restart_request("GET", "/api/restart")
        assert not handler.is_restart_request("POST", "/api/restart")

    def test_restart_clears_session(self) -> None:
        """Restart clears session state."""
        handler = _with_actions(FormAuthHandler("/goform/login"), restart_path="/goform/restart")
        handler.handle_login("POST", "/goform/login", b"user=admin", {})
        assert handler.is_authenticated({})

        response = handler.handle_restart()
        assert response.status == 200
        assert not handler.is_authenticated({})

    def test_logout_clears_session(self) -> None:
        """Logout clears session state (parity with restart)."""
        handler = _with_actions(FormAuthHandler("/goform/login"), logout_path="/goform/logout")
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

    def test_declared_post_login_endpoint_answered_when_absent_from_har(
        self,
        entries: list[dict[str, Any]],
    ) -> None:
        """A declared post-login path missing from the capture gets a synthesized 200.

        Trimmed HARs routinely omit these side-effect calls, which would
        otherwise 404 the replay for a modem that works in the field.
        """
        config = _make_config({"session": {"post_login_endpoints": ["/establish.html"]}})
        with HARMockServer(entries, modem_config=config) as server:
            resp = requests.get(f"{server.base_url}/establish.html")
            assert resp.status_code == 200

    def test_har_entry_wins_over_synthesized_post_login_response(
        self,
        entries: list[dict[str, Any]],
    ) -> None:
        """A captured response for a declared path is served, not the synthetic one."""
        config = _make_config({"session": {"post_login_endpoints": ["/status.html"]}})
        with HARMockServer(entries, modem_config=config) as server:
            resp = requests.get(f"{server.base_url}/status.html")
            assert resp.text == "<html>DS data</html>"


class TestHARMockServerActionFidelity:
    """Logout and restart answer from the capture, and can fail.

    The harness used to intercept every action before route lookup and
    answer it from a per-strategy handler, so a captured action response
    was unreachable and no action request could fail. Regression: a
    bearer logout went out as an unresolved ``{auth:user_id}`` path,
    got 501 from a server with no ``do_DELETE``, and passed.
    """

    @pytest.fixture()
    def entries(self) -> list[dict[str, Any]]:
        """Load bearer HAR entries with a captured DELETE logout."""
        return _load_entries("har_entries_bearer_actions.json")

    @pytest.fixture()
    def config(self) -> Any:
        """Bearer config whose logout addresses the session in the URL."""
        return _make_config(
            {
                "auth": {
                    "strategy": "bearer",
                    "login_endpoint": "/rest/v1/user/login",
                    "token_path": "created.token",
                    "user_id_path": "created.userId",
                },
                "actions": {
                    "logout": {
                        "type": "http",
                        "method": "DELETE",
                        "endpoint": "/rest/v1/user/{auth:user_id}/token/{auth:token}",
                        "requires_session": True,
                    },
                },
            }
        )

    def test_delete_is_dispatched(self, entries: list[dict[str, Any]], config: Any) -> None:
        """A declared DELETE reaches the dispatcher instead of the stdlib's 501."""
        with HARMockServer(entries, modem_config=config) as server:
            resp = requests.delete(f"{server.base_url}/rest/v1/user/7/token/CAPTURED_TOKEN")
            assert resp.status_code != 501

    def test_captured_action_response_wins_over_handler(
        self,
        entries: list[dict[str, Any]],
        config: Any,
    ) -> None:
        """The capture's 204 is served, not the handler's synthesized 200."""
        with HARMockServer(entries, modem_config=config) as server:
            resp = requests.delete(f"{server.base_url}/rest/v1/user/7/token/CAPTURED_TOKEN")
            assert resp.status_code == 204

    def test_unresolved_placeholder_fails_loudly(
        self,
        entries: list[dict[str, Any]],
        config: Any,
    ) -> None:
        """A request still carrying ``{auth:…}`` is a Core bug, not a route miss.

        Percent-encoded by ``requests`` on the way out, so the check has
        to unquote before looking.
        """
        with HARMockServer(entries, modem_config=config) as server:
            resp = requests.delete(f"{server.base_url}/rest/v1/user/{{auth:user_id}}/token/CAPTURED_TOKEN")
            assert resp.status_code == 500
            assert "auth:user_id" in resp.text

    def test_placeholder_body_does_not_span_a_nested_brace(self) -> None:
        """`{` is excluded from the placeholder body, which keeps the match linear.

        Allowing it makes the scan quadratic: on a path of repeated
        `{auth:` with no closing brace, every start position runs to the
        end of the string before failing (py/polynomial-redos). No real
        placeholder nests a brace, so the exclusion costs nothing —
        asserted here so the character class cannot quietly regress.
        """
        assert _UNRESOLVED_PLACEHOLDER_RE.search("/v1/{auth:user_id}/t") is not None
        assert _UNRESOLVED_PLACEHOLDER_RE.search("/v1/{cookie:sessionToken}") is not None
        assert _UNRESOLVED_PLACEHOLDER_RE.search("/v1/user/7/token/abc") is None

        # The pathological shape: opener repeated, never closed.
        assert _UNRESOLVED_PLACEHOLDER_RE.search("{auth:" * 64) is None

        # A real placeholder buried in junk is still caught, but the match
        # starts at the inner brace rather than spanning from the outer one.
        nested = _UNRESOLVED_PLACEHOLDER_RE.search("{auth:{auth:x}")
        assert nested is not None
        assert nested.group(0) == "{auth:x}"

    def test_action_served_status_is_recorded(
        self,
        entries: list[dict[str, Any]],
        config: Any,
    ) -> None:
        """The server records what it answered, so a caller can assert on it."""
        with HARMockServer(entries, modem_config=config) as server:
            requests.delete(f"{server.base_url}/rest/v1/user/7/token/CAPTURED_TOKEN")
            assert server.auth_handler.served_actions["logout"] == 204

    def test_action_dispatched_for_strategy_without_its_own_matcher(self) -> None:
        """Restart is matched from config for every strategy, not per-handler.

        ``basic`` never implemented restart matching, so a declared
        restart fell through to the route table and 404'd while the
        action test still passed.
        """
        entries = _load_entries("har_entries_no_auth.json")
        config = _make_config(
            {
                "auth": {"strategy": "basic"},
                "actions": {
                    "restart": {"type": "http", "method": "POST", "endpoint": "/goform/reboot"},
                },
            }
        )
        with HARMockServer(entries, modem_config=config) as server:
            resp = requests.post(f"{server.base_url}/goform/reboot", data="x=1")
            assert resp.status_code == 200
            assert server.auth_handler.served_actions["restart"] == 200


class TestHARMockServerBearerCaptureSeeding:
    """The bearer handler issues the captured token, not a synthetic one.

    A logout that addresses the session in its URL only replays if the
    token Core carries is the token the captured logout path contains.
    Enforcement compares against what the client sends, derived from the
    login response body — never the captured ``Authorization`` header,
    which har-capture sanitizes independently of the body.
    """

    @pytest.fixture()
    def entries(self) -> list[dict[str, Any]]:
        """Load bearer HAR entries with a captured login response."""
        return _load_entries("har_entries_bearer_actions.json")

    @pytest.fixture()
    def config(self) -> Any:
        """Bearer config matching the captured login response shape."""
        return _make_config(
            {
                "auth": {
                    "strategy": "bearer",
                    "login_endpoint": "/rest/v1/user/login",
                    "token_path": "created.token",
                    "user_id_path": "created.userId",
                },
            }
        )

    def test_login_serves_the_captured_response(self, entries: list[dict[str, Any]], config: Any) -> None:
        """The captured login body reaches the client, userId included."""
        with HARMockServer(entries, modem_config=config) as server:
            resp = requests.post(f"{server.base_url}/rest/v1/user/login", json={"password": "pw"})
            assert resp.status_code == 201
            assert resp.json() == {"created": {"token": "CAPTURED_TOKEN", "userId": 7}}

    def test_captured_token_is_enforced(self, entries: list[dict[str, Any]], config: Any) -> None:
        """Data requests need the captured token back on the Authorization header."""
        with HARMockServer(entries, modem_config=config) as server:
            ok = requests.get(
                f"{server.base_url}/rest/v1/data",
                headers={"Authorization": "Bearer CAPTURED_TOKEN"},
            )
            assert ok.status_code == 200

    def test_synthetic_token_is_rejected(self, entries: list[dict[str, Any]], config: Any) -> None:
        """The old synthetic token no longer opens the session."""
        with HARMockServer(entries, modem_config=config) as server:
            resp = requests.get(
                f"{server.base_url}/rest/v1/data",
                headers={"Authorization": "Bearer mock-bearer-token"},
            )
            assert resp.status_code == 401

    def test_falls_back_to_synthetic_token_without_a_capture(self, config: Any) -> None:
        """A capture with no login response still yields a working login."""
        entries = _load_entries("har_entries_no_auth.json")
        with HARMockServer(entries, modem_config=config) as server:
            resp = requests.post(f"{server.base_url}/rest/v1/user/login", json={"password": "pw"})
            assert resp.status_code == 201
            assert resp.json()["created"]["token"]


class TestHARMockServerRequestBodyShape:
    """A JSON key the capture never carried is refused, not routed.

    Routes and action matching key on method and path, so nothing
    compared what Core sent against what the capture recorded. A
    synthesized fixture written to match Core's own request therefore
    certified Core against itself: the F3896LG login carried a
    ``username`` key for as long as ``BearerAuthManager`` sent one, and
    every replay passed (#82).
    """

    @pytest.fixture()
    def entries(self) -> list[dict[str, Any]]:
        """Bearer entries whose captured login body is password-only."""
        return _load_entries("har_entries_bearer_actions.json")

    @pytest.fixture()
    def config(self) -> Any:
        """Bearer config matching the captured login."""
        return _make_config(
            {
                "auth": {
                    "strategy": "bearer",
                    "login_endpoint": "/rest/v1/user/login",
                    "token_path": "created.token",
                },
            }
        )

    def test_captured_shape_is_served(self, entries: list[dict[str, Any]], config: Any) -> None:
        """The body the capture recorded replays normally."""
        with HARMockServer(entries, modem_config=config) as server:
            resp = requests.post(f"{server.base_url}/rest/v1/user/login", json={"password": "pw"})
            assert resp.status_code == 201

    def test_invented_key_fails_loudly(self, entries: list[dict[str, Any]], config: Any) -> None:
        """The exact #82 defect: a username key the firmware was never sent."""
        with HARMockServer(entries, modem_config=config) as server:
            resp = requests.post(
                f"{server.base_url}/rest/v1/user/login",
                json={"username": "admin", "password": "pw"},
            )
            assert resp.status_code == 500
            assert "username" in resp.text

    def test_subset_of_captured_keys_is_allowed(self, entries: list[dict[str, Any]], config: Any) -> None:
        """Sending fewer keys than the capture is routine, not a defect."""
        with HARMockServer(entries, modem_config=config) as server:
            resp = requests.post(f"{server.base_url}/rest/v1/user/login", json={})
            assert resp.status_code == 201

    def test_form_encoded_body_is_not_compared(self) -> None:
        """Form posts carry browser-only fields; key comparison says nothing.

        The fixture's captured login body is ``username=admin&password=secret``
        — a real form post, so the endpoint must stay unindexed. Pointing
        this at a capture with no POST at all would pass whatever the
        exclusion did.
        """
        entries = _load_entries("har_entries_form_auth.json")
        config = _make_config({"auth": {"strategy": "form", "action": "/goform/login"}})
        with HARMockServer(entries, modem_config=config) as server:
            assert ("POST", "/goform/login") not in server.json_body_keys
            resp = requests.post(f"{server.base_url}/goform/login", json={"invented": "1"})
            assert resp.status_code != 500


class TestHARMockServerWireFraming:
    """Wire re-framing: replayed headers must be true on the wire.

    A HAR stores the decoded response body while keeping the original
    headers, so a faithful replay must re-frame the body to match what
    the headers promise (re-chunk, re-compress) and rewrite absolute
    redirect targets to the harness origin. Regression: the TM1602A
    fixture had Transfer-Encoding deleted to mask this gap.
    """

    @pytest.fixture()
    def entries(self) -> list[dict[str, Any]]:
        """Load wire-framing HAR entries from fixture."""
        return _load_entries("har_entries_wire_framing.json")

    def test_chunked_response_decodes_intact(self, entries: list[dict[str, Any]]) -> None:
        """A chunked-marked body is re-chunked on the wire and decodes cleanly."""
        with HARMockServer(entries) as server:
            resp = requests.get(f"{server.base_url}/chunked.html")
            assert resp.status_code == 200
            assert resp.text == "<html>chunked data</html>"
            assert resp.headers["Transfer-Encoding"] == "chunked"
            assert "Content-Length" not in resp.headers

    def test_gzip_response_decodes_intact(self, entries: list[dict[str, Any]]) -> None:
        """A gzip-marked body is re-compressed on the wire and decodes cleanly."""
        with HARMockServer(entries) as server:
            resp = requests.get(f"{server.base_url}/gzipped.html")
            assert resp.status_code == 200
            assert resp.text == "<html>gzipped data</html>"
            assert resp.headers["Content-Encoding"] == "gzip"

    def test_chunked_gzip_combination(self, entries: list[dict[str, Any]]) -> None:
        """Content-Encoding applies before Transfer-Encoding chunking."""
        with HARMockServer(entries) as server:
            resp = requests.get(f"{server.base_url}/chunked-gzip.html")
            assert resp.status_code == 200
            assert resp.text == "<html>both encodings</html>"

    def test_absolute_location_rewritten_to_harness_origin(self, entries: list[dict[str, Any]]) -> None:
        """An absolute Location pointing at the captured host targets the harness."""
        with HARMockServer(entries) as server:
            resp = requests.get(f"{server.base_url}/", allow_redirects=False)
            assert resp.status_code == 302
            assert resp.headers["Location"] == f"{server.base_url}/index.htm"

    def test_absolute_redirect_can_be_followed(self, entries: list[dict[str, Any]]) -> None:
        """Following a rewritten redirect stays inside the harness."""
        with HARMockServer(entries) as server:
            resp = requests.get(f"{server.base_url}/", timeout=5)
            assert resp.status_code == 200
            assert resp.text == "<html>index</html>"

    def test_relative_location_untouched(self, entries: list[dict[str, Any]]) -> None:
        """A relative Location is replayed as captured."""
        with HARMockServer(entries) as server:
            resp = requests.get(f"{server.base_url}/relative-redirect.html", allow_redirects=False)
            assert resp.headers["Location"] == "/index.htm"

    def test_unsupported_content_encoding_fails_loudly(self, entries: list[dict[str, Any]]) -> None:
        """An encoding the harness cannot reconstruct returns 500, not a silent lie."""
        with HARMockServer(entries) as server:
            resp = requests.get(f"{server.base_url}/brotli.html")
            assert resp.status_code == 500
            assert "br" in resp.text

    def test_plain_response_gets_content_length(self, entries: list[dict[str, Any]]) -> None:
        """Unframed responses carry an accurate Content-Length."""
        with HARMockServer(entries) as server:
            resp = requests.get(f"{server.base_url}/index.htm")
            assert resp.headers["Content-Length"] == str(len("<html>index</html>"))


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


class TestHARMockServerFormLoginPage:
    """The form simulator serves the captured login page before any session exists.

    Real firmware hands an unauthenticated client its login page; that
    page is where Core reads hidden fields and a dynamic form action.
    Answering the pre-fetch with the 401 challenge instead left replay
    unable to certify anything read off the page (#189).
    """

    _FORM = {"strategy": "form", "action": "/goform/login", "login_page": "/", "cookie_name": "session"}

    @pytest.fixture()
    def entries(self) -> list[dict[str, Any]]:
        return _load_entries("har_entries_form_login_page.json")

    def test_login_page_served_pre_auth(self, entries: list[dict[str, Any]]) -> None:
        """GET login_page answers 200 with the captured body, no session required."""
        with HARMockServer(entries, modem_config=_make_config({"auth": self._FORM})) as server:
            resp = requests.get(f"{server.base_url}/")
        assert resp.status_code == 200
        assert "/goform/login?id=111" in resp.text

    def test_captured_headers_are_not_served(self, entries: list[dict[str, Any]]) -> None:
        """Only the body is replayed: the firmware's pre-auth cookie must not read as a session."""
        with HARMockServer(entries, modem_config=_make_config({"auth": self._FORM})) as server:
            session = requests.Session()
            page = session.get(f"{server.base_url}/")
            data = session.get(f"{server.base_url}/status.html")
        assert "Set-Cookie" not in page.headers
        assert data.status_code == 401

    def test_data_pages_still_challenged(self, entries: list[dict[str, Any]]) -> None:
        """Serving the login page opens nothing else."""
        with HARMockServer(entries, modem_config=_make_config({"auth": self._FORM})) as server:
            assert requests.get(f"{server.base_url}/status.html").status_code == 401

    def test_undeclared_login_page_is_challenged(self, entries: list[dict[str, Any]]) -> None:
        """Without login_page in config the page is a data page like any other."""
        config = _make_config({"auth": {"strategy": "form", "action": "/goform/login"}})
        with HARMockServer(entries, modem_config=config) as server:
            assert requests.get(f"{server.base_url}/").status_code == 401

    def test_uncaptured_login_page_is_challenged(self) -> None:
        """A declared page the capture never recorded is not invented."""
        entries = _load_entries("har_entries_form_auth.json")
        with HARMockServer(entries, modem_config=_make_config({"auth": self._FORM})) as server:
            assert requests.get(f"{server.base_url}/").status_code == 401

    def test_accepted_login_is_the_later_capture(self, entries: list[dict[str, Any]]) -> None:
        """Refused-then-accepted logins under distinct ?id= keys replay the accepted one."""
        with HARMockServer(entries, modem_config=_make_config({"auth": self._FORM})) as server:
            session = requests.Session()
            login = session.post(f"{server.base_url}/goform/login?id=999", allow_redirects=False)
            assert login.status_code == 302
            assert login.headers["Location"].endswith("/index.htm")
            assert session.get(f"{server.base_url}/status.html").status_code == 200


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
