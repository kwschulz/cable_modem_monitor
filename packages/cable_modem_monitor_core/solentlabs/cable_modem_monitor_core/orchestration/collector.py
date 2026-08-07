"""ModemDataCollector — single data collection cycle.

Executes authenticate -> load resources -> parse -> post-parse filter
-> logout. Owns no scheduling, retry, or backoff policy. Runs once
and returns a ModemResult with signal classification.

See ORCHESTRATION_SPEC.md ModemDataCollector section.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any, Final

import requests

from ..auth.base import AuthContext, AuthResult, BaseAuthManager, LoginLockoutError
from ..auth.factory import create_auth_manager
from ..connectivity import create_session
from ..fetch_list import collect_fetch_targets
from ..loaders.hnap import HNAPLoadError
from ..loaders.http import (
    HTTPResourceLoader,
    LoginPageDetectedError,
    ResourceLoadError,
)
from ..models.modem_config.actions import HttpAction
from ..models.modem_config.auth import NoneAuth
from ..parsers.coordinator import ModemParserCoordinator
from ..parsers.diagnostics import ParseDiagnostics
from .actions import execute_action
from .auth_failure import (
    _auth_failure_hint,
    _build_auth_failed_event,
    _build_http_status_error_event,
    _should_detect_login_pages,
    _strategy_name,
)
from .events import (
    AuthSucceeded,
    CollectionComplete,
    ConnectionFailedDuringLoad,
    EventLevel,
    HnapConnectionFailed,
    HnapLoadError as HnapLoadErrorEvent,
    HnapSessionExpired,
    LogoutExecuted,
    LogoutFailed,
    ParseError,
    PostLoginFetchFailed,
    ResourceDecodeError,
    ResourceFetched,
    ResourceLoadError as ResourceLoadErrorEvent,
    SessionCleared,
    SessionReused,
    StubPageDetected,
)
from .logging import log_event
from .models import ModemResult, ResourceFetch
from .signals import CollectorSignal

_logger = logging.getLogger(__name__)
_LOGOUT_LOG_LEVEL: Final[int] = logging.DEBUG
_DEFAULT_AUTH_LOG_LEVEL: Final[int] = logging.DEBUG


class ModemDataCollector:
    """Execute a single data collection cycle."""

    def __init__(
        self,
        modem_config: Any,
        parser_config: Any,
        post_processor: Any,
        base_url: str,
        username: str,
        password: str,
        *,
        legacy_ssl: bool = False,
    ) -> None:
        self._modem_config = modem_config
        self._parser_config = parser_config
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._legacy_ssl = legacy_ssl

        # Auth manager and context
        self._auth_manager: BaseAuthManager = create_auth_manager(modem_config)
        self._auth_context: AuthContext | None = None
        self._last_auth_result: AuthResult | None = None
        self._session_reused: bool = False
        # First successful login of this collector's lifetime is the
        # setup-confirmation line; re-logins after it are steady state.
        # ORCHESTRATION_SPEC § Logging Contract, auth/resource tier.
        self._auth_success_logged: bool = False
        # Same tier for the collection that follows the login: the first
        # completed collection of this collector's lifetime confirms the
        # poll returned data, and every one after it is steady state.
        self._collection_complete_logged: bool = False

        # Persistent session — reused across execute() calls.
        # Created via create_session() so HTTPS modems with self-signed
        # certs get verify=False, and legacy firmware gets SECLEVEL=0.
        self._session = self._build_session()

        # Parser coordinator (reused across polls)
        self._coordinator: ModemParserCoordinator | None = None
        if parser_config is not None:
            self._coordinator = ModemParserCoordinator(parser_config, post_processor)

        # Kept for fetch-list derivation — parser.py declares the
        # resources its hooks read via `resources` (PARSING_SPEC).
        self._post_processor = post_processor

        # Login page detection — enable for form-based auth strategies
        self._detect_login_pages = _should_detect_login_pages(modem_config)

        # Per-resource timing from last successful collection
        self._last_resource_fetches: list[ResourceFetch] = []

        # Stub body from last LOAD_INTEGRITY failure — kept until next event
        self._last_stub_bodies: dict[str, str] = {}

        # system_info field outcomes (PARSING_SPEC § Field Outcomes).
        # missing: snapshot of the most recent parse. failed: retained
        # for the runtime so an intermittent conversion failure survives
        # into a diagnostics download taken after a healthy poll.
        self._last_sysinfo_missing: list[str] = []
        self._sysinfo_failed: dict[str, str] = {}

    def execute(self) -> ModemResult:
        """Execute one data collection."""
        start = time.monotonic()

        # Phase 1: Auth
        try:
            auth_result = self.authenticate()
        except LoginLockoutError as exc:
            return ModemResult(
                success=False,
                signal=CollectorSignal.AUTH_LOCKOUT,
                error=str(exc),
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            error_with_type = f"{type(exc).__name__}: {exc}"
            log_event(
                _logger,
                _build_auth_failed_event(
                    model=self._modem_config.model,
                    strategy=_strategy_name(self._modem_config),
                    response=None,
                    error=error_with_type,
                    password=self._password,
                ),
            )
            return ModemResult(
                success=False,
                signal=CollectorSignal.CONNECTIVITY,
                error=error_with_type,
            )

        if not auth_result.success:
            log_event(
                _logger,
                _build_auth_failed_event(
                    model=self._modem_config.model,
                    strategy=_strategy_name(self._modem_config),
                    response=auth_result.response,
                    error=auth_result.error,
                    password=self._password,
                ),
            )
            status = auth_result.response.status_code if auth_result.response is not None else None
            # A 5xx is the modem declining to serve the login, not a verdict
            # on the credential, so it must not reach the circuit breaker.
            # Mirrors the data path, where 401/403 is LOAD_AUTH and every
            # other status is LOAD_ERROR (RESOURCE_LOADING_SPEC). See UC-87a.
            signal = (
                CollectorSignal.AUTH_UNAVAILABLE
                if status is not None and 500 <= status < 600
                else CollectorSignal.AUTH_FAILED
            )
            return ModemResult(
                success=False,
                signal=signal,
                error=auth_result.error,
                auth_status_code=status,
            )

        # Phase 2: Load resources
        try:
            resources, fetches = self._load_resources(auth_result)
        except LoginPageDetectedError as exc:
            return ModemResult(
                success=False,
                signal=CollectorSignal.LOAD_AUTH,
                error=str(exc),
            )
        except HNAPLoadError as exc:
            return self._classify_hnap_error(exc)
        except ResourceLoadError as exc:
            if exc.status_code in (401, 403):
                hint = _auth_failure_hint(self._auth_manager)
                log_event(
                    _logger,
                    _build_http_status_error_event(
                        model=self._modem_config.model,
                        path=exc.path,
                        status_code=exc.status_code,
                        reason=hint,
                        request_line=exc.request_line,
                        content_type=exc.content_type,
                        response_body=exc.response_body,
                        password=self._password,
                    ),
                )
                return ModemResult(
                    success=False,
                    signal=CollectorSignal.LOAD_AUTH,
                    error=f"{exc.status_code} on {exc.path} — {hint}",
                )
            log_event(
                _logger,
                ResourceLoadErrorEvent(
                    model=self._modem_config.model,
                    path=exc.path,
                    status_code=exc.status_code,
                    reason=str(exc),
                ),
            )
            return ModemResult(
                success=False,
                signal=CollectorSignal.LOAD_ERROR,
                error=str(exc),
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            log_event(
                _logger,
                ConnectionFailedDuringLoad(
                    model=self._modem_config.model,
                    path="",
                    status_code=None,
                    reason=str(exc),
                ),
            )
            return ModemResult(
                success=False,
                signal=CollectorSignal.CONNECTIVITY,
                error=str(exc),
            )

        self._emit_resource_fetched_events(fetches)

        # Phase 3: Parse + stub-page integrity check (UC-19a)
        parse_outcome = self._run_parse_phase(resources)
        if isinstance(parse_outcome, ModemResult):
            return parse_outcome
        data = parse_outcome

        # Phase 4: Logout (best-effort, after successful collection)
        self._execute_logout_if_needed()

        self._last_resource_fetches = fetches

        ds_count = len(data.get("downstream", []))
        us_count = len(data.get("upstream", []))
        elapsed_ms = (time.monotonic() - start) * 1000
        log_event(
            _logger,
            CollectionComplete(
                model=self._modem_config.model,
                ds_count=ds_count,
                us_count=us_count,
                elapsed_ms=elapsed_ms,
                level=EventLevel.DEBUG if self._collection_complete_logged else EventLevel.INFO,
            ),
        )
        self._collection_complete_logged = True

        return ModemResult(
            success=True,
            modem_data=data,
            signal=CollectorSignal.OK,
        )

    @property
    def session_is_valid(self) -> bool:
        """Whether the Auth Manager believes the current session is usable.

        Strategy-specific local check: HNAP verifies uid cookie +
        private key (the private key is also set as a PrivateKey
        cookie by the auth manager); cookie-based strategies verify
        the session cookie is present; basic and none are always valid
        after first auth.

        This is a local check -- the server may have expired the session
        even if this returns True.
        """
        # Never authenticated — only NoneAuth can skip authenticate()
        # entirely. BasicAuth is stateless per-request but still needs
        # the initial authenticate() call to set session.auth.
        if self._auth_context is None:
            if self._modem_config.auth is None:
                return True
            return isinstance(self._modem_config.auth, NoneAuth)

        # HNAP: verify session cookies and private key
        if self._modem_config.transport == "hnap":
            has_uid = "uid" in self._session.cookies
            has_key = bool(self._auth_context.private_key)
            return has_uid and has_key

        # Cookie-based: verify session cookie (cookie_name is on auth config)
        cookie_name = getattr(self._modem_config.auth, "cookie_name", "")
        if cookie_name:
            return cookie_name in self._session.cookies

        # URL token: verify token exists
        if self._auth_context.url_token:
            return True

        # Already authenticated — assume valid until server rejects
        return True

    @property
    def last_resource_fetches(self) -> list[ResourceFetch]:
        """Per-resource timing from the last successful collection."""
        return self._last_resource_fetches

    @property
    def last_stub_bodies(self) -> dict[str, str]:
        """Response body snippets from the last LOAD_INTEGRITY event, keyed by resource path."""
        return self._last_stub_bodies

    @property
    def last_system_info_fields_missing(self) -> list[str]:
        """Mapped system_info fields no source produced on the most recent parse."""
        return self._last_sysinfo_missing

    @property
    def system_info_fields_failed(self) -> dict[str, str]:
        """Conversion-rejected raw values, retained for the runtime."""
        # Copy: unlike the sibling properties (reassigned per event),
        # this dict is mutated in place via update() across polls — a
        # handed-out reference would change under its consumer.
        return dict(self._sysinfo_failed)

    @property
    def session(self) -> requests.Session:
        """The underlying ``requests.Session`` used for auth and loading."""
        return self._session

    def clear_session(self) -> None:
        """Invalidate the current session."""
        self._session.cookies.clear()
        self._auth_context = None
        self._last_auth_result = None
        log_event(_logger, SessionCleared(model=self._modem_config.model))

    def close(self) -> None:
        """Log out any live session, then close the HTTP session."""
        # Release a live server-side session before dropping the socket, so
        # single-session firmware isn't left holding a lock when a consumer
        # discards this collector (e.g. an HA reload that was reusing the
        # session). Best-effort and bounded by the action's timeout, so
        # teardown can't hang on an unreachable modem. No-op for
        # per-poll-logout modems — their session is already cleared here.
        if self.session_is_valid:
            self._best_effort_logout()
        self._session.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_session(self) -> requests.Session:
        """Build the ``requests.Session`` for this modem's polling lifetime."""
        session = create_session(legacy_ssl=self._legacy_ssl)
        session_headers: dict[str, str] = {}
        if self._modem_config.session and self._modem_config.session.headers:
            session_headers = self._modem_config.session.resolved_headers(base_url=self._base_url)
        self._auth_manager.configure_session(session, session_headers)
        return session

    def authenticate(
        self,
        *,
        log_level: int = _DEFAULT_AUTH_LOG_LEVEL,
    ) -> AuthResult:
        """Authenticate the session if not already valid."""
        if self.session_is_valid:
            self._session_reused = True
            log_event(_logger, SessionReused(model=self._modem_config.model))
            return self._last_auth_result or AuthResult(success=True)

        self._session_reused = False
        _logger.debug("No active session [%s] — Authenticating", self._modem_config.model)
        result = self._auth_manager.authenticate(
            self._session,
            self._base_url,
            self._username,
            self._password,
            timeout=self._modem_config.timeout,
            log_level=log_level,
        )

        if result.success:
            self._auth_context = result.auth_context
            self._last_auth_result = result
            log_event(
                _logger,
                AuthSucceeded(
                    model=self._modem_config.model,
                    strategy=_strategy_name(self._modem_config),
                    status_code=result.response.status_code if result.response is not None else 0,
                    response_url=result.response_url,
                    level=EventLevel.DEBUG if self._auth_success_logged else EventLevel.INFO,
                ),
            )
            self._auth_success_logged = True
            # Part of establishing the login, so it belongs here rather
            # than in execute() — restart.py authenticates directly and
            # dispatches its action without ever reaching execute().
            self._fetch_post_login_endpoints()

        return result

    def _fetch_post_login_endpoints(self) -> None:
        """GET each declared post-login path in order; responses are discarded.

        Some SPA firmware treats the browser's first post-login call as
        part of establishing the session and rejects data requests until
        it has been made (#120). Best-effort by design: login already
        succeeded, so failing here would report bad credentials for a
        working password, and a genuinely required call shows up anyway
        as the data fetch's own 401 alongside this warning. A transient
        hiccup, or firmware moving the path, must not kill monitoring on
        a modem that was working.
        """
        # Reached only on the fresh-login branch of authenticate(), so
        # there is no reused-session case to guard against here.
        session_config = self._modem_config.session
        if session_config is None:
            return
        for path in session_config.post_login_endpoints:
            try:
                response = self._session.get(
                    self._post_login_url(path),
                    timeout=self._modem_config.timeout,
                )
            except requests.RequestException as exc:
                self._log_post_login_failure(path, None, f"{type(exc).__name__}: {exc}")
                continue
            if not 200 <= response.status_code < 300:
                self._log_post_login_failure(path, response.status_code, response.reason or "")

    def _post_login_url(self, path: str) -> str:
        """Build a post-login URL, carrying session-level query params like every other fetch."""
        url = f"{self._base_url}{path}"
        query_params = self._modem_config.session.query_params if self._modem_config.session else {}
        if query_params:
            sep = "&" if "?" in url else "?"
            qs = "&".join(f"{k}={v}" for k, v in query_params.items())
            url = f"{url}{sep}{qs}"
        return url

    def _log_post_login_failure(self, path: str, status_code: int | None, reason: str) -> None:
        """Emit the WARNING for a post-login endpoint that did not answer 2xx."""
        log_event(
            _logger,
            PostLoginFetchFailed(
                model=self._modem_config.model,
                path=path,
                status_code=status_code,
                reason=reason,
            ),
        )

    def _load_resources(
        self,
        auth_result: AuthResult,
    ) -> tuple[dict[str, Any], list[ResourceFetch]]:
        """Fetch all resources using the authenticated session."""
        if self._parser_config is None:
            raise RuntimeError("Modem requires custom parser.py — parser.yaml alone insufficient for resource loading")

        if self._modem_config.transport == "hnap":
            return self._load_hnap_resources()

        if self._modem_config.transport == "cbn":
            return self._load_cbn_resources()

        return self._load_http_resources(auth_result)

    def _load_http_resources(
        self,
        auth_result: AuthResult,
    ) -> tuple[dict[str, Any], list[ResourceFetch]]:
        """Fetch HTTP resources."""
        targets = collect_fetch_targets(self._parser_config, self._post_processor)

        # Prefer body-derived token from auth_context; fall back to cookie
        url_token = ""
        token_prefix = getattr(self._modem_config.auth, "token_prefix", "")
        if token_prefix:
            if self._auth_context and self._auth_context.url_token:
                url_token = self._auth_context.url_token
            else:
                cookie_name = getattr(self._modem_config.auth, "cookie_name", "")
                if cookie_name:
                    url_token = self._session.cookies.get(cookie_name, "") or ""

        query_params: dict[str, str] = {}
        if self._modem_config.session and self._modem_config.session.query_params:
            query_params = dict(self._modem_config.session.query_params)

        loader = HTTPResourceLoader(
            session=self._session,
            base_url=self._base_url,
            timeout=self._modem_config.timeout,
            url_token=url_token,
            token_prefix=token_prefix,
            detect_login_pages=self._detect_login_pages,
            model=self._modem_config.model,
            query_params=query_params,
            headers=self._auth_manager.headers(),
        )

        # On session reuse, don't pass auth_result — there's no
        # login response to reuse.
        effective_auth = auth_result if self._auth_context else None
        resources = loader.fetch(targets, effective_auth)
        for path, fmt, reason in loader.decode_errors:
            log_event(
                _logger,
                ResourceDecodeError(
                    model=self._modem_config.model,
                    path=path,
                    fmt=fmt,
                    reason=reason,
                ),
            )
        return resources, _to_resource_fetches(loader.resource_fetches)

    def _load_hnap_resources(self) -> tuple[dict[str, Any], list[ResourceFetch]]:
        """Fetch HNAP resources via batched SOAP request."""
        from ..loaders.hnap import HNAPLoader

        hmac_algorithm = "md5"
        if hasattr(self._modem_config.auth, "hmac_algorithm"):
            hmac_algorithm = self._modem_config.auth.hmac_algorithm

        private_key = ""
        if self._auth_context:
            private_key = self._auth_context.private_key

        loader = HNAPLoader(
            session=self._session,
            base_url=self._base_url,
            private_key=private_key,
            hmac_algorithm=hmac_algorithm,
            timeout=self._modem_config.timeout,
            headers=self._auth_manager.headers(),
        )
        resources = loader.fetch(self._parser_config)
        return resources, _to_resource_fetches(loader.resource_fetches)

    def _load_cbn_resources(self) -> tuple[dict[str, Any], list[ResourceFetch]]:
        """Fetch CBN resources via XML POST with fun parameters.

        Logout is NOT done by the loader — the collector handles it
        via ``_execute_logout_if_needed()`` using ``actions.logout``.
        """
        from ..loaders.cbn import CBNLoader
        from ..models.modem_config.auth import FormCbnAuth

        targets = collect_fetch_targets(self._parser_config, self._post_processor)

        auth = self._modem_config.auth
        assert isinstance(auth, FormCbnAuth)

        loader = CBNLoader(
            session=self._session,
            base_url=self._base_url,
            getter_endpoint=auth.getter_endpoint,
            session_cookie_name=auth.session_cookie_name,
            timeout=self._modem_config.timeout,
            model=self._modem_config.model,
            headers=self._auth_manager.headers(),
        )
        resources = loader.fetch(targets)
        return resources, _to_resource_fetches(loader.resource_fetches)

    def _classify_hnap_error(self, exc: HNAPLoadError) -> ModemResult:
        """Route an HNAP load failure to the correct signal."""
        cause = exc.__cause__

        # Connection/timeout — modem unreachable (UC-30/UC-31)
        if exc.status_code is None and isinstance(cause, requests.ConnectionError | requests.Timeout):
            log_event(_logger, HnapConnectionFailed(model=self._modem_config.model, reason=str(exc)))
            return ModemResult(
                success=False,
                signal=CollectorSignal.CONNECTIVITY,
                error=str(exc),
            )

        # HTTP error on reused session — stale (UC-21)
        if exc.status_code is not None and self._session_reused:
            log_event(
                _logger,
                HnapSessionExpired(
                    model=self._modem_config.model,
                    status_code=exc.status_code,
                ),
            )
            return ModemResult(
                success=False,
                signal=CollectorSignal.LOAD_AUTH,
                error=f"HNAP HTTP {exc.status_code} — session expired",
            )

        # Fresh session HTTP error or JSON parse — genuine problem (UC-22)
        log_event(_logger, HnapLoadErrorEvent(model=self._modem_config.model, reason=str(exc)))
        return ModemResult(
            success=False,
            signal=CollectorSignal.LOAD_ERROR,
            error=str(exc),
        )

    def _parse(self, resources: dict[str, Any]) -> tuple[dict[str, Any], ParseDiagnostics]:
        """Parse resources into ModemData with diagnostics."""
        if self._coordinator is None:
            raise RuntimeError("No parser coordinator configured")
        return self._coordinator.parse(resources)

    def _emit_resource_fetched_events(self, fetches: list[ResourceFetch]) -> None:
        """Emit one ResourceFetched event per successfully loaded page."""
        for f in fetches:
            log_event(
                _logger,
                ResourceFetched(
                    model=self._modem_config.model,
                    path=f.path,
                    status_code=f.status_code,
                    size_bytes=f.size_bytes,
                    elapsed_ms=f.duration_ms,
                ),
            )

    def _run_parse_phase(self, resources: dict[str, Any]) -> dict[str, Any] | ModemResult:
        """Run parse + stub-page integrity check."""
        try:
            data, diagnostics = self._parse(resources)
        except Exception as exc:
            log_event(_logger, ParseError(model=self._modem_config.model, reason=str(exc)))
            return ModemResult(
                success=False,
                signal=CollectorSignal.PARSE_ERROR,
                error=str(exc),
            )
        if diagnostics.has_zero_fulfillment:
            return self._build_load_integrity_result(diagnostics, resources)
        # Field outcomes are diagnostics-only — recorded, never a signal.
        # PARSING_SPEC § Field Outcomes.
        self._last_sysinfo_missing = list(diagnostics.system_info_fields_missing)
        self._sysinfo_failed.update(diagnostics.system_info_fields_failed)
        return data

    def _build_load_integrity_result(self, diagnostics: ParseDiagnostics, resources: dict[str, Any]) -> ModemResult:
        """Build a LOAD_INTEGRITY result when 0 of N expected anchors were found (UC-19a)."""
        affected = ", ".join(diagnostics.zero_fulfillment_resources)
        for path, c in diagnostics.by_resource.items():
            if c.expected > 0 and c.fulfilled == 0:
                log_event(
                    _logger,
                    StubPageDetected(
                        model=self._modem_config.model,
                        path=path,
                        anchors_found=c.fulfilled,
                        anchors_expected=c.expected,
                    ),
                )
        self._last_stub_bodies = {
            path: str(body)
            for path in diagnostics.zero_fulfillment_resources
            if (body := resources.get(path)) is not None
        }
        return ModemResult(
            success=False,
            signal=CollectorSignal.LOAD_INTEGRITY,
            error=f"0 of N expected anchors on {affected} — stub response",
        )

    def _execute_logout_if_needed(self) -> None:
        """Execute logout action for single-session modems."""
        actions = self._modem_config.actions
        if actions is None or actions.logout is None:
            return

        try:
            result = execute_action(self, self._modem_config, actions.logout, log_level=_LOGOUT_LOG_LEVEL)
        except Exception as exc:
            log_event(_logger, LogoutFailed(model=self._modem_config.model, reason=str(exc)))
            return

        # A refused status is not a transport error, so it never reaches
        # the except. Read the result: the session the modem refused to
        # end is still live, and our cookie is the only handle on it.
        # Clearing it here orphans one session per poll, which locks out
        # firmware that permits a single login.
        if not result.success:
            log_event(_logger, LogoutFailed(model=self._modem_config.model, reason=result.message))
            return

        log_event(_logger, LogoutExecuted(model=self._modem_config.model))
        # Session is dead server-side after logout — clear local
        # state so next poll re-authenticates instead of reusing
        # a stale session.
        self.clear_session()

    def attempt_logout_before_retry(self) -> None:
        """Best-effort logout before a same-poll auth retry on single-session firmware."""
        self._best_effort_logout()

    def _best_effort_logout(self) -> None:
        """Suppressed, guarded logout — releases an active server-side session.

        Shared by the same-poll retry path and close(); never raises.
        """
        actions = self._modem_config.actions
        if actions is None or actions.logout is None:
            return
        # requires_session=True means the endpoint needs a live session to
        # function. Skip the call when it is already gone; it would fail anyway.
        # Unauthenticated endpoints (False, the default) can clear any active
        # server-side session without credentials. The test is session validity,
        # not cookie presence: bearer holds a live session in the Authorization
        # header with an empty jar.
        if isinstance(actions.logout, HttpAction) and actions.logout.requires_session and not self.session_is_valid:
            return
        with contextlib.suppress(Exception):
            execute_action(self, self._modem_config, actions.logout, log_level=_LOGOUT_LOG_LEVEL)


def _to_resource_fetches(
    raw: list[tuple[str, float, int, int, str]],
) -> list[ResourceFetch]:
    """Convert loader timing tuples to ResourceFetch objects."""
    return [
        ResourceFetch(
            path=r[0],
            duration_ms=r[1],
            size_bytes=r[2],
            status_code=r[3],
            content_type=r[4],
        )
        for r in raw
    ]
