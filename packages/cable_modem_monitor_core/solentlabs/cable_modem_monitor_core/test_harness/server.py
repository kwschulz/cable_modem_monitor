"""HTTP server — thin layer composing routes and auth.

Supports two lifecycle modes: ephemeral (context manager for automated
tests) and persistent (``serve_forever()`` for manual integration testing).

See ARCHITECTURE.md § Test Harness for the replay-fidelity rules this
server implements.
"""

from __future__ import annotations

import gzip
import logging
import re
import threading
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

if TYPE_CHECKING:
    from ..models.modem_config import ModemConfig

from .auth import create_auth_handler
from .routes import build_routes, normalize_path

_logger = logging.getLogger(__name__)

# The two placeholder namespaces MODEM_YAML_SPEC defines. Matched
# against the path only — a captured query string may legitimately
# carry braces (an XB10 entry passes JSON in one).
_UNRESOLVED_PLACEHOLDER_RE = re.compile(r"\{(?:auth|cookie):[^}/]+\}")


class _MockHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the mock server.

    Dispatches requests through the auth layer and route table.
    """

    # N802: Method names are dictated by BaseHTTPRequestHandler — the
    # stdlib dispatches by looking for methods named exactly do_GET,
    # do_POST, etc.  Renaming to snake_case would break dispatch.

    @property
    def _mock_server(self) -> HARMockServer:
        """The owning HARMockServer, narrowed from the stdlib's BaseServer typing."""
        server = self.server
        if not isinstance(server, HARMockServer):
            raise TypeError(f"handler requires HARMockServer, got {type(server).__name__}")
        return server

    def do_GET(self) -> None:  # noqa: N802
        """Handle GET requests."""
        self._handle_request("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        """Handle HEAD requests — same as GET but without response body."""
        self._handle_request("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        """Handle POST requests."""
        self._handle_request("POST")

    def do_PUT(self) -> None:  # noqa: N802
        """Handle PUT requests."""
        self._handle_request("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        """Handle DELETE requests — REST firmwares end a session with one."""
        self._handle_request("DELETE")

    def _handle_request(self, method: str) -> None:
        """Dispatch a request through auth then routes."""
        server = self._mock_server
        self._is_head = method == "HEAD"
        # HEAD uses GET routes for lookup
        lookup_method = "GET" if self._is_head else method
        parsed = urlparse(self.path)
        path = normalize_path(parsed.path)
        # Include query string for route lookup so endpoints like
        # /setup.cgi?todo=X resolve independently.
        route_path = f"{path}?{parsed.query}" if parsed.query else path
        headers = {k.lower(): v for k, v in self.headers.items()}

        body = b""
        if method in ("POST", "PUT", "DELETE"):
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)

        method = lookup_method
        auth = server.auth_handler

        # A placeholder that survived to the wire means Core could not
        # resolve it, so the request targets a path the modem never had.
        # Routing it would hide the bug behind whatever the lookup does
        # next; the ZG's logout reached the modem as a literal
        # {auth:user_id} for exactly this reason.
        unresolved = _UNRESOLVED_PLACEHOLDER_RE.search(unquote(path))
        if unresolved:
            self._fail_request(f"unresolved placeholder in request path: {unresolved.group(0)}")
            return

        # Login request — handle auth and serve response
        if auth.is_login_request(method, path):
            self._handle_login(server, method, path, route_path, body, headers)
            return

        # Logout / restart — the capture answers when it has the
        # exchange; the handler contributes the session side effect
        # either way (clearing state, invalidating a token).
        for kind, matches, handle in (
            ("logout", auth.is_logout_request, auth.handle_logout),
            ("restart", auth.is_restart_request, auth.handle_restart),
        ):
            if not matches(method, path):
                continue
            synthesized = handle()
            captured = _find_route(
                server.routes,
                method,
                path,
                route_path,
                login_page=server.login_page,
                token_prefix=server.token_prefix,
            )
            response = captured if captured is not None else synthesized
            auth.record_action(kind, response.status)
            self._send_response(response.status, response.headers, response.body)
            return

        # Non-login request — check auth
        if not auth.is_authenticated(headers):
            challenge = auth.get_challenge_response()
            self._send_response(
                challenge.status,
                challenge.headers,
                challenge.body,
            )
            return

        # Auth handler route override (HNAP merged data response)
        override = auth.get_route_override(method, path, body, headers)
        if override is not None:
            self._send_response(override.status, override.headers, override.body)
            return

        # Serve from route table
        route = _find_route(
            server.routes,
            method,
            path,
            route_path,
            login_page=server.login_page,
            token_prefix=server.token_prefix,
        )
        if route is None:
            # Trimmed captures rarely include these side-effect calls, so
            # synthesize a response only when the route table has none.
            if path in server.post_login_endpoints:
                self._send_response(200, [("Content-Type", "application/json")], '{"error": "ok"}')
                return
            self._send_response(404, [], "Not Found")
            return

        self._send_response(route.status, route.headers, route.body)

    def _handle_login(
        self,
        server: HARMockServer,
        method: str,
        path: str,
        route_path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> None:
        """Handle a login request through auth then route table.

        If ``handle_login`` returns ``None``, the request is not a login
        attempt (e.g. HNAP data request on POST /HNAP1/). In that case,
        fall through to the authenticated request path: auth check,
        route override, then route table.
        """
        auth = server.auth_handler
        login_response = auth.handle_login(method, path, body, headers)
        if login_response is not None:
            self._send_response(
                login_response.status,
                login_response.headers,
                login_response.body,
            )
            return

        # Not a login — fall through to authenticated request handling.
        # This path is used by HNAP when is_login_request matches all
        # POST /HNAP1/ but handle_login returns None for data requests.
        if not auth.is_authenticated(headers):
            self._send_response(401, [], "Unauthorized")
            return

        override = auth.get_route_override(method, path, body, headers)
        if override is not None:
            self._send_response(override.status, override.headers, override.body)
            return

        # Form auth fallthrough — route table + set_authenticated
        route = _find_route(
            server.routes,
            method,
            path,
            route_path,
            login_page=server.login_page,
            token_prefix=server.token_prefix,
        )
        if route is None:
            self._send_response(404, [], "Not Found")
            return
        extra_headers = auth.set_authenticated()
        response_headers = list(route.headers)
        for name, value in extra_headers.items():
            response_headers.append((name, value))
        self._send_response(route.status, response_headers, route.body)

    def _fail_request(self, message: str) -> None:
        """Answer 500 with a diagnostic — the harness cannot honestly serve this request."""
        payload = message.encode("utf-8")
        self.send_response(500)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if not getattr(self, "_is_head", False):
            self.wfile.write(payload)

    def _send_response(
        self,
        status: int,
        headers: list[tuple[str, str]],
        body: str,
    ) -> None:
        """Send a wire-framed HTTP response. HEAD requests get headers only."""
        server = self._mock_server
        try:
            out_headers, payload = _frame_wire_response(headers, body, server.base_url)
        except _UnsupportedFramingError as exc:
            # Fail loudly rather than serve bytes that contradict the headers.
            self._fail_request(str(exc))
            return
        self.send_response(status)
        for name, value in out_headers:
            self.send_header(name, value)
        self.end_headers()
        if payload and not getattr(self, "_is_head", False):
            self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default stderr logging."""
        _logger.debug(format, *args)


def _find_route(
    routes: dict[tuple[str, str], Any],
    method: str,
    path: str,
    route_path: str,
    login_page: str = "",
    token_prefix: str = "",
) -> Any:
    """Look up a route by method and path, with query-string fallback.

    Three-tier lookup:

    1. Exact match ``(method, route_path)`` — query string included.
    2. Path-only ``(method, path)`` — request has query, route doesn't.
    3. Scan for route whose path portion matches — route has query
       (e.g. HAR captured ``?status=1``), request doesn't.

    Tiers 1-2 handle query-string-specific routes (e.g.
    ``/setup.cgi?todo=X``) and dynamic URL token suffixes.
    Tier 3 handles HARs that captured incidental query params
    (e.g. ``?status=1``) that aren't part of the resource path.

    **Login-page disambiguation.** When ``login_page`` is set and the
    request targets that path with a query string, the request is a
    login GET — unless ``token_prefix`` is set and the query starts
    with it, in which case it's a token-suffixed data fetch. Login
    GETs match against captured login HAR entries (path with query)
    only; they MUST NOT fall back to a bare-path data entry, which
    would silently substitute the data page for the login response
    and mask auth-side bugs end-to-end. Data fetches use the normal
    Tier 1-3 lookup. Regression: SB8200 #81.
    """
    has_query = route_path != path

    # Tier 1: exact match
    route = routes.get((method, route_path))
    if route is not None:
        return route

    if _is_login_get(path, has_query, route_path, login_page, token_prefix):
        return _find_login_entry(routes, method, path, token_prefix)

    # Tier 2: request has query, route stored without
    if has_query:
        route = routes.get((method, path))
        if route is not None:
            return route

    # Tier 3: route has query, request doesn't — scan by path prefix
    for (m, rp), r in routes.items():
        if m == method and rp.split("?", 1)[0] == path:
            return r

    return None


def _is_login_get(
    path: str,
    has_query: bool,
    route_path: str,
    login_page: str,
    token_prefix: str,
) -> bool:
    """True if the request is a login GET at ``login_page``."""
    if not login_page or path != normalize_path(login_page) or not has_query:
        return False
    # Data fetch with token suffix at login_page is not a login GET.
    query = route_path.split("?", 1)[1]
    return not (token_prefix and query.startswith(token_prefix))


def _find_login_entry(
    routes: dict[tuple[str, str], Any],
    method: str,
    path: str,
    token_prefix: str,
) -> Any:
    """Return the captured login HAR entry for ``path``, or ``None``.

    Picks the first route whose key has the same path and a non-empty
    query string. Skips data-shape entries (query starts with
    ``token_prefix``) so SB8200-style fixtures with both login and
    token-suffixed data entries at the same path don't cross-route.
    """
    for (m, rp), r in routes.items():
        if m != method:
            continue
        entry_path, _, entry_query = rp.partition("?")
        if entry_path != path or not entry_query:
            continue
        if token_prefix and entry_query.startswith(token_prefix):
            continue
        return r
    return None


class _UnsupportedFramingError(Exception):
    """Raised when a captured framing header cannot be reconstructed on the wire."""


def _frame_wire_response(
    headers: list[tuple[str, str]],
    body: str,
    origin: str,
) -> tuple[list[tuple[str, str]], bytes]:
    """Re-frame a decoded HAR body so the captured headers stay true on the wire.

    A HAR stores the decoded response body while keeping the original
    headers. Serving both verbatim promises framing (chunked, gzip) the
    bytes don't have, so the client errors; this re-applies the framing
    the headers declare. Absolute Location targets are rewritten to the
    harness origin so redirects stay inside the harness.
    """
    out, encoding, chunked = _transform_headers(headers, origin)
    payload = _encode_payload(body.encode("utf-8"), encoding)
    if chunked:
        payload = _chunk_encode(payload)
    else:
        out.append(("Content-Length", str(len(payload))))
    return out, payload


def _transform_headers(
    headers: list[tuple[str, str]],
    origin: str,
) -> tuple[list[tuple[str, str]], str, bool]:
    """Filter and rewrite captured headers; returns (headers, content_encoding, chunked)."""
    out: list[tuple[str, str]] = []
    encoding = ""
    chunked = False
    for name, value in headers:
        lower = name.lower()
        if lower == "content-length":
            # Recomputed by the caller; captured values drift after redaction.
            continue
        if lower == "content-encoding":
            encoding = value.strip().lower()
        elif lower == "transfer-encoding":
            if value.strip().lower() != "chunked":
                raise _UnsupportedFramingError(f"unsupported Transfer-Encoding: {value}")
            chunked = True
        out.append((name, _rewrite_location(value, origin) if lower == "location" else value))
    return out, encoding, chunked


def _encode_payload(payload: bytes, encoding: str) -> bytes:
    """Apply the declared Content-Encoding to the payload bytes."""
    if encoding in ("gzip", "x-gzip"):
        return gzip.compress(payload)
    if encoding == "deflate":
        return zlib.compress(payload)
    if encoding and encoding != "identity":
        raise _UnsupportedFramingError(f"unsupported Content-Encoding: {encoding}")
    return payload


def _chunk_encode(payload: bytes) -> bytes:
    """Wrap payload as a single HTTP chunk plus terminator."""
    if not payload:
        return b"0\r\n\r\n"
    return f"{len(payload):X}\r\n".encode("ascii") + payload + b"\r\n0\r\n\r\n"


def _rewrite_location(value: str, origin: str) -> str:
    """Point an absolute redirect at the harness origin, keeping path and query."""
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return value
    rebuilt = f"{origin}{parsed.path or '/'}"
    if parsed.query:
        rebuilt += f"?{parsed.query}"
    return rebuilt


class HARMockServer(HTTPServer):
    """Auth-aware HAR replay HTTP server.

    Two usage modes share the same server:

    **Automated testing** — context manager, ephemeral port::

        with HARMockServer(entries, modem_config=config) as server:
            base_url = server.base_url
            # ... run pipeline against base_url ...

    **Manual integration testing** — persistent server on a fixed port::

        server = HARMockServer(entries, modem_config=config,
                               host="0.0.0.0", port=8080)
        server.serve_forever()  # blocks until interrupted

    Args:
        har_entries: HAR ``log.entries`` list.
        modem_config: Validated ``ModemConfig`` for auth handler creation.
            None for no auth.
        host: Bind address. Defaults to ``127.0.0.1`` (localhost only).
            Use ``0.0.0.0`` to accept connections from other hosts.
        port: Bind port. Defaults to ``0`` (OS-assigned ephemeral port).
            Use a fixed port (e.g., ``8080``) for manual testing.
    """

    def __init__(
        self,
        har_entries: list[dict[str, Any]],
        modem_config: ModemConfig | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.routes = build_routes(har_entries)
        self.auth_handler = create_auth_handler(modem_config, har_entries)
        self.login_page = _extract_login_page(modem_config)
        self.token_prefix = _extract_token_prefix(modem_config)
        self.post_login_endpoints = _extract_post_login_endpoints(modem_config)
        self._thread: threading.Thread | None = None

        super().__init__((host, port), _MockHandler)

    @property
    def base_url(self) -> str:
        """Base URL of the running server (e.g., ``http://127.0.0.1:54321``)."""
        host = str(self.server_address[0])
        port = self.server_address[1]
        return f"http://{host}:{port}"

    def __enter__(self) -> HARMockServer:
        """Start the server in a background thread."""
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        """Stop the server and wait for the thread to finish."""
        self.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.server_close()


def _extract_login_page(modem_config: ModemConfig | None) -> str:
    """Return the ``auth.login_page`` if the strategy declares one."""
    if modem_config is None or modem_config.auth is None:
        return ""
    return getattr(modem_config.auth, "login_page", "") or ""


def _extract_token_prefix(modem_config: ModemConfig | None) -> str:
    """Return ``auth.token_prefix`` (url_token strategy) if declared."""
    if modem_config is None or modem_config.auth is None:
        return ""
    return getattr(modem_config.auth, "token_prefix", "") or ""


def _extract_post_login_endpoints(modem_config: ModemConfig | None) -> frozenset[str]:
    """Return normalized ``session.post_login_endpoints`` paths."""
    if modem_config is None or modem_config.session is None:
        return frozenset()
    return frozenset(normalize_path(p) for p in modem_config.session.post_login_endpoints)
