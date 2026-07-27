"""Tests for HTTPResourceLoader."""

from __future__ import annotations

import base64 as b64mod
from pathlib import Path
from typing import Any

import pytest
import requests
from bs4 import BeautifulSoup
from solentlabs.cable_modem_monitor_core.auth.base import AuthResult
from solentlabs.cable_modem_monitor_core.loaders.fetch_list import ResourceTarget
from solentlabs.cable_modem_monitor_core.loaders.http import (
    HTTPResourceLoader,
    LoginPageDetectedError,
    ResourceLoadError,
    _decode_response,
)
from solentlabs.cable_modem_monitor_core.test_harness import HARMockServer

from tests._helpers import load_fixture


def _build_entries(
    pages: dict[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    """Build HAR entries.

    Args:
        pages: Mapping of path to (content_type, body).
    """
    entries: list[dict[str, Any]] = []
    for path, (content_type, body) in pages.items():
        entries.append(
            {
                "request": {
                    "method": "GET",
                    "url": f"http://192.168.100.1{path}",
                },
                "response": {
                    "status": 200,
                    "headers": [
                        {"name": "Content-Type", "value": content_type},
                    ],
                    "content": {"text": body},
                },
            }
        )
    return entries


_FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# _decode_response — table-driven
# ---------------------------------------------------------------------------

# ┌──────────────────────────┬───────────────────┬──────────┬───────────┬──────────────────────────────────┐
# │ text                     │ format            │ encoding │ exp_type  │ description                      │
# ├──────────────────────────┼───────────────────┼──────────┼───────────┼──────────────────────────────────┤
# │ "<html>..."              │ table             │ ""       │ soup      │ HTML table                       │
# │ "<html>..."              │ table_transposed  │ ""       │ soup      │ transposed table                 │
# │ "<script>..."            │ javascript        │ ""       │ soup      │ JS embedded                      │
# │ "<script>..."            │ javascript_json   │ ""       │ soup      │ JS JSON                          │
# │ "<html>..."              │ html_fields       │ ""       │ soup      │ html_fields                      │
# │ '{"key":"val"}'          │ json              │ ""       │ dict      │ valid JSON dict                  │
# │ "not json"               │ json              │ ""       │ None      │ invalid JSON                     │
# │ "[1,2,3]"                │ json              │ ""       │ dict      │ non-dict JSON → _raw wrapper     │
# │ b64("<html>...")         │ table             │ base64   │ soup      │ base64-encoded body              │
# │ "!!!invalid"             │ table             │ base64   │ None      │ base64 decode failure            │
# │ ""                       │ table             │ ""       │ None      │ empty body                       │
# │ "<root/>"                │ xml               │ ""       │ None      │ xml is CBN-only, not decoded here│
# │ "<html>..."              │ unknown           │ ""       │ None      │ unregistered format rejected     │
# └──────────────────────────┴───────────────────┴──────────┴───────────┴──────────────────────────────────┘

_B64_HTML = b64mod.b64encode(b"<html><table></table></html>").decode()

# fmt: off
_DECODE_CASES: list[tuple[str, str, str, str | None, str]] = [
    # (text,                          format,             encoding, exp_type, description)
    ("<html><table></table></html>",  "table",            "",       "soup",   "HTML table"),
    ("<html></html>",                 "table_transposed", "",       "soup",   "transposed table"),
    ("<script>var x=1;</script>",     "javascript",       "",       "soup",   "JS embedded"),
    ("<script>var x=1;</script>",     "javascript_json",  "",       "soup",   "JS JSON"),
    ("<html><div>info</div></html>",  "html_fields",      "",       "soup",   "html_fields"),
    ('{"key": "value"}',              "json",             "",       "dict",   "valid JSON dict"),
    ("not json",                      "json",             "",       None,     "invalid JSON"),
    ("[1, 2, 3]",                     "json",             "",       "dict",   "non-dict JSON wrapped"),
    (_B64_HTML,                       "table",            "base64", "soup",   "base64-encoded body"),
    ("x",                             "table",            "base64", None,     "base64 decode failure"),
    ("",                              "table",            "",       None,     "empty body"),
    ("<root/>",                       "xml",              "",       None,     "xml is CBN-only"),
    ("<html></html>",                 "unknown",          "",       None,     "unregistered format rejected"),
]
# fmt: on


@pytest.mark.parametrize("text,fmt,encoding,exp_type,desc", _DECODE_CASES, ids=[c[4] for c in _DECODE_CASES])
def test_decode_response(text: str, fmt: str, encoding: str, exp_type: str | None, desc: str) -> None:
    """_decode_response: {desc}."""
    value, reason = _decode_response(text, fmt, encoding)
    if exp_type is None:
        assert value is None
    elif exp_type == "soup":
        assert isinstance(value, BeautifulSoup)
    elif exp_type == "dict":
        assert isinstance(value, dict)


class TestDecodeResponseBehaviors:
    """Behavioral assertions beyond type checking."""

    def test_json_dict_preserves_keys(self) -> None:
        """Valid JSON dict preserves original keys."""
        value, _ = _decode_response('{"key": "value"}', "json", "")
        assert value["key"] == "value"

    def test_json_non_dict_wrapped_in_raw(self) -> None:
        """Non-dict JSON is wrapped in _raw key."""
        value, _ = _decode_response("[1, 2, 3]", "json", "")
        assert value["_raw"] == [1, 2, 3]

    def test_failure_returns_reason(self) -> None:
        """Decode failures return a non-None reason string."""
        _, reason = _decode_response("not json", "json", "")
        assert reason == "invalid JSON"

    def test_empty_body_returns_none_reason(self) -> None:
        """Empty body returns (None, None) — not a decode error."""
        value, reason = _decode_response("", "table", "")
        assert value is None
        assert reason is None

    def test_base64_failure_returns_reason(self) -> None:
        """Bad base64 returns (None, reason)."""
        _, reason = _decode_response("!!!invalid", "table", "base64")
        assert reason == "base64 decode failed"

    def test_xml_returns_reason(self) -> None:
        """XML is CBN-only; the HTTP loader rejects it rather than decoding."""
        _, reason = _decode_response("<root/>", "xml", "")
        assert reason == "unsupported decode kind 'xml' for format 'xml'"

    def test_unregistered_format_returns_reason(self) -> None:
        """An unregistered format is rejected, never coerced to BeautifulSoup."""
        value, reason = _decode_response("<html></html>", "unknown", "")
        assert value is None
        assert reason == "unsupported decode kind '' for format 'unknown'"


class TestHTTPResourceLoader:
    """HTTPResourceLoader fetches pages from mock server."""

    def test_fetch_single_html_page(self) -> None:
        """Fetches a single HTML page as BeautifulSoup."""
        entries = _build_entries({"/status.html": ("text/html", "<html><table>Data</table></html>")})

        with HARMockServer(entries) as server:
            session = requests.Session()
            loader = HTTPResourceLoader(session, server.base_url, timeout=10)
            targets = [ResourceTarget(path="/status.html", format="table")]
            resources = loader.fetch(targets)

        assert "/status.html" in resources
        assert isinstance(resources["/status.html"], BeautifulSoup)

    def test_fetch_multiple_pages(self) -> None:
        """Fetches multiple pages in one call."""
        entries = _build_entries(
            {
                "/status.html": ("text/html", "<html>Status</html>"),
                "/info.html": ("text/html", "<html>Info</html>"),
            }
        )

        with HARMockServer(entries) as server:
            session = requests.Session()
            loader = HTTPResourceLoader(session, server.base_url, timeout=10)
            targets = [
                ResourceTarget(path="/status.html", format="table"),
                ResourceTarget(path="/info.html", format="html_fields"),
            ]
            resources = loader.fetch(targets)

        assert len(resources) == 2
        assert "/status.html" in resources
        assert "/info.html" in resources

    def test_fetch_json_page(self) -> None:
        """Fetches JSON page and returns dict."""
        entries = _build_entries({"/api/data": ("application/json", '{"channels": []}')})

        with HARMockServer(entries) as server:
            session = requests.Session()
            loader = HTTPResourceLoader(session, server.base_url, timeout=10)
            targets = [ResourceTarget(path="/api/data", format="json")]
            resources = loader.fetch(targets)

        assert "/api/data" in resources
        assert isinstance(resources["/api/data"], dict)
        assert resources["/api/data"]["channels"] == []

    def test_404_raises_error(self) -> None:
        """404 response raises ResourceLoadError."""
        entries = _build_entries({"/status.html": ("text/html", "<html>OK</html>")})

        with HARMockServer(entries) as server:
            session = requests.Session()
            loader = HTTPResourceLoader(session, server.base_url, timeout=10)
            targets = [ResourceTarget(path="/nonexistent.html", format="table")]
            with pytest.raises(ResourceLoadError, match="404"):
                loader.fetch(targets)

    def test_malformed_cookie_value_raises_clean_error(self) -> None:
        """A non-header-safe cookie value yields a clean ResourceLoadError.

        Backstop for UC-19b: if a credential cookie carrying CR/LF reaches the
        fetch, ``http.client.putheader`` raises a bare ``ValueError`` that
        escapes the loader's ``requests.RequestException`` handler as an
        unhandled stack trace. The loader must convert it to a handled
        ResourceLoadError instead. Regression: SB8200 inject variant #124 (rct).
        """
        entries = _build_entries({"/status.html": ("text/html", "<html>OK</html>")})

        with HARMockServer(entries) as server:
            session = requests.Session()
            session.cookies.set("credential", "<html>\nlogin\n</html>")
            loader = HTTPResourceLoader(session, server.base_url, timeout=10)
            targets = [ResourceTarget(path="/status.html", format="table")]
            with pytest.raises(ResourceLoadError):
                loader.fetch(targets)

    def test_auth_response_reuse(self) -> None:
        """Auth response is reused when its URL matches a target."""
        entries = _build_entries({"/status.html": ("text/html", "<html>Status</html>")})

        # Simulate auth response that landed on /status.html
        mock_response = requests.Response()
        mock_response.status_code = 200
        mock_response._content = b"<html>Auth Landing</html>"
        mock_response.url = "http://192.168.100.1/status.html"
        mock_response.encoding = "utf-8"

        auth_result = AuthResult(
            success=True,
            response=mock_response,
            response_url="/status.html",
        )

        with HARMockServer(entries) as server:
            session = requests.Session()
            loader = HTTPResourceLoader(session, server.base_url, timeout=10)
            targets = [ResourceTarget(path="/status.html", format="table")]
            resources = loader.fetch(targets, auth_result=auth_result)

        assert "/status.html" in resources
        # Should have the auth response content, not the server content
        soup = resources["/status.html"]
        assert "Auth Landing" in soup.get_text()

    def test_url_token_appended(self) -> None:
        """URL token is appended to request URLs."""
        entries = _build_entries({"/status.html": ("text/html", "<html>Data</html>")})

        with HARMockServer(entries) as server:
            session = requests.Session()
            loader = HTTPResourceLoader(
                session,
                server.base_url,
                timeout=10,
                url_token="abc123",
                token_prefix="ct_",
            )
            # This will fail because the mock server routes by path
            # without query string, so the fetch should still work
            targets = [ResourceTarget(path="/status.html", format="table")]
            resources = loader.fetch(targets)

        assert "/status.html" in resources

    def test_empty_targets_returns_empty_dict(self) -> None:
        """No targets produces empty resource dict."""
        entries = _build_entries({})

        with HARMockServer(entries) as server:
            session = requests.Session()
            loader = HTTPResourceLoader(session, server.base_url, timeout=10)
            resources = loader.fetch([])

        assert resources == {}

    def test_auth_response_reuse_only_for_matching_path(self) -> None:
        """Auth response is not reused for non-matching paths."""
        entries = _build_entries(
            {
                "/status.html": ("text/html", "<html>Server Status</html>"),
                "/info.html": ("text/html", "<html>Server Info</html>"),
            }
        )

        mock_response = requests.Response()
        mock_response.status_code = 200
        mock_response._content = b"<html>Auth Landing</html>"
        mock_response.url = "http://192.168.100.1/info.html"
        mock_response.encoding = "utf-8"

        auth_result = AuthResult(
            success=True,
            response=mock_response,
            response_url="/info.html",
        )

        with HARMockServer(entries) as server:
            session = requests.Session()
            loader = HTTPResourceLoader(session, server.base_url, timeout=10)
            targets = [
                ResourceTarget(path="/status.html", format="table"),
                ResourceTarget(path="/info.html", format="html_fields"),
            ]
            resources = loader.fetch(targets, auth_result=auth_result)

        # /status.html should come from server
        assert "Server Status" in resources["/status.html"].get_text()
        # /info.html should be reused from auth response
        assert "Auth Landing" in resources["/info.html"].get_text()

    def test_no_reuse_when_auth_result_has_no_response(self) -> None:
        """Loader fetches the data path when AuthResult does not advertise reuse.

        Negative pair to ``test_auth_response_reuse``. When the auth
        manager populates ``auth_context.url_token`` but does NOT set
        ``response``/``response_url`` (token-only body, not a data
        page — see RESOURCE_LOADING_SPEC.md § Auth Response Reuse),
        the loader must perform a real fetch with the token appended
        to the URL. Reusing a token-only auth response would surface
        the token string as the parsed data page.

        Regression: SB8200 #81. Pairs with
        test_token_branch_does_not_advertise_reuse in test_url_token.py.
        """
        from solentlabs.cable_modem_monitor_core.auth.base import AuthContext

        # Server returns the real data page for the token-suffixed URL
        entries = _build_entries({"/cmconnectionstatus.html": ("text/html", "<html>Downstream Bonded Channels</html>")})

        auth_result = AuthResult(
            success=True,
            auth_context=AuthContext(url_token="ct_token_value"),
            # response and response_url intentionally unset — token branch
        )

        with HARMockServer(entries) as server:
            session = requests.Session()
            loader = HTTPResourceLoader(
                session,
                server.base_url,
                timeout=10,
                url_token="ct_token_value",
                token_prefix="ct_",
            )
            targets = [ResourceTarget(path="/cmconnectionstatus.html", format="table")]
            resources = loader.fetch(targets, auth_result=auth_result)

        # Loader fetched the data page — token appended via url_token, not reused
        assert "Downstream Bonded Channels" in resources["/cmconnectionstatus.html"].get_text()
        # Verify the loader recorded the network fetch (not a reuse)
        paths_fetched = [r[0] for r in loader.resource_fetches]
        assert "/cmconnectionstatus.html" in paths_fetched

    def test_auth_response_reuse_with_error_status(self) -> None:
        """Auth response with error status is still eligible for reuse.

        requests.Response.__bool__() returns False for status >= 400.
        The reuse check must use ``is not None``, not truthiness.
        """
        entries = _build_entries({"/status.html": ("text/html", "<html>Server Status</html>")})

        # Response with 403 — bool(response) is False
        error_response = requests.Response()
        error_response.status_code = 403
        error_response._content = b"<html>Error Landing</html>"
        error_response.url = "http://192.168.100.1/status.html"
        error_response.encoding = "utf-8"

        auth_result = AuthResult(
            success=True,
            response=error_response,
            response_url="/status.html",
        )

        with HARMockServer(entries) as server:
            session = requests.Session()
            loader = HTTPResourceLoader(session, server.base_url, timeout=10)
            targets = [ResourceTarget(path="/status.html", format="table")]
            resources = loader.fetch(targets, auth_result=auth_result)

        # Should reuse the auth response (error content), not fetch from server
        assert "Error Landing" in resources["/status.html"].get_text()

    def test_connection_failure_raises_resource_load_error(self) -> None:
        """ConnectionError during fetch raises ResourceLoadError."""
        from unittest.mock import patch

        session = requests.Session()
        loader = HTTPResourceLoader(session, "http://127.0.0.1", timeout=1)
        targets = [ResourceTarget(path="/status.html", format="table")]
        with (
            patch.object(session, "get", side_effect=requests.ConnectionError("refused")),
            pytest.raises(ResourceLoadError, match="ConnectionError"),
        ):
            loader.fetch(targets)

    # Each row: (exception_class, exception_arg, expected_type_name).
    # Verifies the wrapped ResourceLoadError surfaces the underlying
    # requests exception class so log analysis can distinguish a
    # connection refusal from an SSL handshake failure at a glance.
    _FETCH_EXCEPTIONS = [
        (requests.ConnectionError, "refused", "ConnectionError"),
        (requests.Timeout, "timed out", "Timeout"),
        (requests.exceptions.SSLError, "handshake", "SSLError"),
        (requests.HTTPError, "bad response", "HTTPError"),
        (requests.exceptions.ChunkedEncodingError, "bad chunk", "ChunkedEncodingError"),
    ]

    @pytest.mark.parametrize(
        "exc_class,exc_arg,expected_type_name",
        _FETCH_EXCEPTIONS,
        ids=[c[2] for c in _FETCH_EXCEPTIONS],
    )
    def test_fetch_error_includes_exception_class_name(
        self,
        exc_class: type[Exception],
        exc_arg: str,
        expected_type_name: str,
    ) -> None:
        """ResourceLoadError message includes the underlying exception class."""
        from unittest.mock import patch

        session = requests.Session()
        loader = HTTPResourceLoader(session, "http://127.0.0.1", timeout=1)
        targets = [ResourceTarget(path="/status.html", format="table")]
        with (
            patch.object(session, "get", side_effect=exc_class(exc_arg)),
            pytest.raises(ResourceLoadError) as exc_info,
        ):
            loader.fetch(targets)
        assert expected_type_name in str(exc_info.value)

    def test_undecoded_response_skipped(self) -> None:
        """Page that decodes to None is excluded from resources."""
        entries = _build_entries({"/data.json": ("application/json", "not valid json")})

        with HARMockServer(entries) as server:
            session = requests.Session()
            loader = HTTPResourceLoader(session, server.base_url, timeout=10)
            targets = [ResourceTarget(path="/data.json", format="json")]
            resources = loader.fetch(targets)

        assert "/data.json" not in resources

    def test_login_page_detected(self) -> None:
        """Login page detection raises LoginPageDetectedError."""
        login_html = '<html><form><input type="password" name="pw"></form></html>'
        entries = _build_entries({"/status.html": ("text/html", login_html)})

        with HARMockServer(entries) as server:
            session = requests.Session()
            loader = HTTPResourceLoader(
                session,
                server.base_url,
                timeout=10,
                detect_login_pages=True,
            )
            targets = [ResourceTarget(path="/status.html", format="table")]
            with pytest.raises(LoginPageDetectedError):
                loader.fetch(targets)

    def test_stub_html_passes_loader_unchanged(self) -> None:
        """UC-19a: stub HTML without a password input is NOT classified as
        a login page — it passes through to the parser. Stub-page detection
        moved to the parser-coordinator layer (issue #151)."""
        stub_html = (
            "<html><head><title>Modem</title>"
            '<link rel="stylesheet" href="css/main.css">'
            '<script src="jquery.js"></script>'
            "</head><body><div>Loading...</div></body></html>"
        )
        entries = _build_entries({"/status.html": ("text/html", stub_html)})

        with HARMockServer(entries) as server:
            session = requests.Session()
            loader = HTTPResourceLoader(
                session,
                server.base_url,
                timeout=10,
                detect_login_pages=True,
            )
            targets = [ResourceTarget(path="/status.html", format="table")]
            resources = loader.fetch(targets)

        # Loader's job is HTTP-shape; it does not know parser anchors.
        # Stub passes through; the coordinator detects zero-fulfillment.
        assert "/status.html" in resources

    def test_401_raises_resource_load_error(self) -> None:
        """401 response raises ResourceLoadError with status code."""
        fixture = load_fixture(_FIXTURES / "http_401_plain.json")

        with HARMockServer(fixture["_entries"]) as server:
            session = requests.Session()
            loader = HTTPResourceLoader(session, server.base_url, timeout=10)
            targets = [ResourceTarget(path="/status.html", format="table")]
            with pytest.raises(ResourceLoadError, match="401"):
                loader.fetch(targets)

    def test_401_carries_response_body_and_request_line(self) -> None:
        """401 attaches the wire detail the collector needs to diagnose it (#120)."""
        fixture = load_fixture(_FIXTURES / "http_401_json_body.json")
        expected = fixture["_expected"]

        with HARMockServer(fixture["_entries"]) as server:
            session = requests.Session()
            session.headers.update({"X-Requested-With": "XMLHttpRequest"})
            loader = HTTPResourceLoader(session, server.base_url, timeout=10)
            targets = [ResourceTarget(path="/status.html", format="table")]
            with pytest.raises(ResourceLoadError) as exc_info:
                loader.fetch(targets)

        exc = exc_info.value
        assert exc.response_body == expected["response_body"]
        assert exc.content_type == expected["content_type"]
        assert "X-Requested-With=XMLHttpRequest" in exc.request_line
        assert "GET" in exc.request_line

    def test_query_params_appended(self) -> None:
        """Session query_params are appended to fetch URLs."""
        entries = _build_entries({"/status.html": ("text/html", "<html>Data</html>")})

        with HARMockServer(entries) as server:
            session = requests.Session()
            loader = HTTPResourceLoader(
                session,
                server.base_url,
                timeout=10,
                query_params={"_n": "12345"},
            )
            targets = [ResourceTarget(path="/status.html", format="table")]
            resources = loader.fetch(targets)

        assert "/status.html" in resources

    def test_query_params_with_url_token(self) -> None:
        """Query params combine with URL token in the request URL."""
        entries = _build_entries({"/status.html": ("text/html", "<html>Data</html>")})

        with HARMockServer(entries) as server:
            session = requests.Session()
            loader = HTTPResourceLoader(
                session,
                server.base_url,
                timeout=10,
                url_token="abc123",
                token_prefix="ct_",
                query_params={"_n": "99999"},
            )
            targets = [ResourceTarget(path="/status.html", format="table")]
            resources = loader.fetch(targets)

        assert "/status.html" in resources

    def test_resource_fetches_recorded(self) -> None:
        """Per-resource timing tuples are populated after fetch."""
        entries = _build_entries(
            {
                "/status.html": ("text/html", "<html>Status</html>"),
                "/info.html": ("text/html", "<html>Info</html>"),
            }
        )

        with HARMockServer(entries) as server:
            session = requests.Session()
            loader = HTTPResourceLoader(session, server.base_url, timeout=10)
            targets = [
                ResourceTarget(path="/status.html", format="table"),
                ResourceTarget(path="/info.html", format="html_fields"),
            ]
            loader.fetch(targets)

        assert len(loader.resource_fetches) == 2
        paths = [f[0] for f in loader.resource_fetches]
        assert "/status.html" in paths
        assert "/info.html" in paths
        # Each tuple is (path, duration_ms, size_bytes, status_code, content_type)
        for _path, duration_ms, size_bytes, status_code, content_type in loader.resource_fetches:
            assert duration_ms >= 0
            assert size_bytes > 0
            assert status_code == 200
            assert "text/html" in content_type
