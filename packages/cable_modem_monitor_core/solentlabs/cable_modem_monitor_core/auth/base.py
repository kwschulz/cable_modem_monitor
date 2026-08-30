"""Base auth manager and result types.

Auth managers execute login flows and prepare a ``requests.Session``
for resource loading. Each strategy is driven by modem.yaml config.

See MODEM_YAML_SPEC.md Auth section and RESOURCE_LOADING_SPEC.md.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from enum import StrEnum

import requests


class AuthFailureMode(StrEnum):
    """How to read a 401/403 that arrives *after* authenticate() succeeded.

    The answer depends on whether the strategy actually verified the
    login. A strategy that cannot tell a bad password from a good one
    reports success regardless, so a later 401 is very often the
    rejected credential surfacing late.
    """

    NOT_CONFIGURED = "not_configured"
    CREDENTIALS_SUSPECT = "credentials_suspect"
    SESSION_REJECTED = "session_rejected"


class LoginLockoutError(Exception):
    """Firmware anti-brute-force triggered.

    Raised by an auth strategy when the modem reports that it is
    refusing logins to protect itself rather than judging the
    credential; each strategy's spec names the responses that mean it.
    The orchestrator catches this and applies backoff policy. Defined
    here rather than in orchestration so the auth layer can raise it
    without importing upward.
    """


@dataclass
class AuthContext:
    """Typed downstream state from auth managers.

    Each auth strategy populates the fields it produces; the runner
    reads them by attribute based on ``modem_config.transport``, and
    action endpoints reference them as ``{auth:...}`` placeholders.

    Attributes:
        url_token: Session token for URL query string auth (``url_token`` strategy).
        private_key: HMAC signing key for HNAP requests (``hnap`` strategy).
        token: Session token from the login response (``bearer`` strategy).
        user_id: Account identifier from the login response (``bearer`` strategy).
    """

    url_token: str = ""
    private_key: str = ""
    token: str = ""
    user_id: str = ""


@dataclass
class AuthResult:
    """Result of an authentication attempt.

    Attributes:
        success: Whether authentication succeeded.
        error: Error message on failure.
        auth_context: Typed downstream state from the auth manager.
            See :class:`AuthContext` for available fields.
        response: Login response object. Used for auth response reuse
            — the loader skips re-fetching if the login response landed
            on a data page. Also set on failure (any branch where the
            modem returned a response) so the collector can render
            sanitized failure detail.
        response_url: URL path the login response corresponds to.
            May differ from the login URL if a redirect occurred.
        busy: The modem declined to serve the login without judging
            the credential, under a 2xx the status rule cannot read;
            each strategy's spec names the responses that mean it. The
            collector classifies it ``AUTH_UNAVAILABLE``, as it does a
            5xx (UC-87a). Only meaningful with ``success=False``.

    **Reuse contract — load-bearing.** ``response`` and ``response_url``
    advertise an auth-response-reuse opportunity to the loader, which
    decodes ``response.text`` as if it were a fetched data page. They
    MUST NOT be set when:

    - The login response body is an opaque artefact (e.g., a session
      token string returned for downstream URL injection).
    - The login response is otherwise not a parser-consumable data page
      for any path in the fetch list.

    Violating this contract causes the loader to surface the auth
    artefact as the data page and skip the real fetch — silently
    producing empty results. See RESOURCE_LOADING_SPEC.md § Auth
    Response Reuse and MODEM_YAML_SPEC.md § ``url_token``.
    Regression: SB8200 #81.

    On the failure path (``success=False``), ``response`` may be set
    so the collector can log sanitized wire detail; ``response_url``
    is irrelevant there because the loader is not invoked.
    """

    success: bool
    error: str = ""
    auth_context: AuthContext = field(default_factory=AuthContext)
    response: requests.Response | None = None
    response_url: str = ""
    busy: bool = False


class BaseAuthManager(abc.ABC):
    """Abstract base for auth managers.

    Auth managers authenticate against a modem's web interface and
    prepare a ``requests.Session`` for subsequent resource loading.

    The session is passed by reference — implementations add cookies,
    auth headers, or other credentials to it. After ``authenticate()``
    succeeds, the session is ready for the loader to use.
    """

    @abc.abstractmethod
    def authenticate(
        self,
        session: requests.Session,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: int = 10,
        log_level: int = logging.DEBUG,
    ) -> AuthResult:
        """Authenticate and prepare the session.

        Args:
            session: ``requests.Session`` to configure with auth state.
            base_url: Modem base URL (e.g., ``http://192.168.100.1``).
            username: Username credential.
            password: Password credential.
            timeout: Per-request timeout in seconds from modem.yaml.
            log_level: Log level for non-error messages. Config flow
                uses INFO for visibility; polling uses DEBUG to avoid
                log noise.

        Returns:
            AuthResult with success flag and optional login response.
        """

    def configure_session(
        self,
        session: requests.Session,
        session_headers: dict[str, str],
    ) -> None:
        """Apply session-wide configuration from modem.yaml.

        Sets static headers (e.g., ``X-Requested-With``) on the
        session. Called once before ``authenticate()``.

        Args:
            session: Session to configure.
            session_headers: Headers from ``session.headers`` in modem.yaml.
        """
        session.headers.update(session_headers)

    def headers(self) -> frozenset[str]:
        """Lowercase names of headers this strategy puts on the wire.

        Loader diagnostics treat these as redaction targets in failure
        logs (so logs confirm presence without leaking token values).
        Each strategy declares the headers IT introduces; ``cookie`` is
        included by default because almost every auth strategy ends up
        with a session cookie on the wire.
        """
        return frozenset({"cookie"})

    def auth_failure_mode(self) -> AuthFailureMode:
        """How a post-login 401/403 should be read for this strategy.

        The default is deliberately pessimistic: assume the credential
        is suspect. Overriding to ``SESSION_REJECTED`` is a claim that
        this strategy rejects a bad password at login time, and that
        claim must be backed by a mock-server test proving it — see
        ARCHITECTURE_DECISIONS.md "Post-login 401 is read per auth
        strategy". A strategy that inherits this default produces a
        message that is merely less specific; one that overrides it
        without proof tells users their working password is fine when
        it is not.
        """
        return AuthFailureMode.CREDENTIALS_SUSPECT
