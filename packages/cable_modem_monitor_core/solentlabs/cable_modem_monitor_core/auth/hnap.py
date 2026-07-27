"""HNAP HMAC challenge-response authentication.

Implements the HNAP login protocol: request a challenge from the modem,
derive a private key via HMAC, then authenticate with the derived
credentials. All subsequent requests are signed with ``HNAP_AUTH``
headers using the private key.

Protocol details derived from HAR evidence (Login.js, SOAPAction.js).

See MODEM_YAML_SPEC.md ``hnap`` strategy and RUNTIME_POLLING_SPEC.md
for lockout handling.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import requests

from ..protocol.hnap import (
    HNAP_ENDPOINT,
    HNAP_NAMESPACE,
    compute_auth_header,
    hmac_hex,
)
from .base import AuthContext, AuthResult, BaseAuthManager, LoginLockoutError
from .response import parse_json_dict

if TYPE_CHECKING:
    from ..models.modem_config.auth import HnapAuth

_logger = logging.getLogger(__name__)


# Pre-auth signing key used for the initial challenge request,
# before the private key is derived from the challenge response.
_PRE_AUTH_KEY = "withoutloginkey"


class HnapAuthManager(BaseAuthManager):
    """HNAP HMAC challenge-response authentication.

    Two-phase login:
    1. Send ``Login`` action with ``Action: "request"`` to get
       ``Challenge``, ``PublicKey``, and ``Cookie`` from the modem.
    2. Derive ``PrivateKey`` and ``LoginPassword`` via HMAC, then
       send ``Login`` action with ``Action: "login"`` and the
       computed credentials.

    The derived ``PrivateKey`` is returned in ``AuthResult`` for
    the HNAP loader to sign subsequent data requests.

    Args:
        config: Validated ``HnapAuth`` config from modem.yaml.
    """

    def __init__(self, config: HnapAuth) -> None:
        self._hmac_algorithm = config.hmac_algorithm

    def headers(self) -> frozenset[str]:
        # HNAP_AUTH carries an HMAC signed with the per-session PrivateKey.
        return frozenset({"cookie", "hnap_auth"})

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
        """Execute the HNAP challenge-response login flow.

        Args:
            session: ``requests.Session`` to configure with cookies.
            base_url: Modem base URL (e.g., ``http://192.168.100.1``).
            username: Username credential (typically ``"admin"``).
            password: Password credential.
            timeout: Per-request timeout in seconds.
            log_level: Log level for non-error messages.

        Returns:
            ``AuthResult`` with ``auth_context.private_key`` on
            success, or error detail on failure (including lockout
            detection).
        """
        url = f"{base_url.rstrip('/')}{HNAP_ENDPOINT}"

        # Phase 1: Request challenge
        challenge_result = self._request_challenge(
            session,
            url,
            username,
            timeout=timeout,
        )
        if challenge_result is not None:
            return challenge_result  # Error occurred

        # Phase 2: Compute credentials and login
        return self._login_with_credentials(
            session,
            url,
            username,
            password,
            timeout=timeout,
        )

    def _request_challenge(
        self,
        session: requests.Session,
        url: str,
        username: str,
        *,
        timeout: int,
    ) -> AuthResult | None:
        """Send the challenge request (phase 1).

        Returns ``None`` on success (challenge data stored on instance),
        or an ``AuthResult`` with error detail on failure.
        """
        body = {
            "Login": {
                "Action": "request",
                "Username": username,
                "LoginPassword": "",
                "Captcha": "",
                "PrivateLogin": "LoginPassword",
            },
        }

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "SOAPAction": f'"{HNAP_NAMESPACE}Login"',
            "HNAP_AUTH": compute_auth_header(
                _PRE_AUTH_KEY,
                "Login",
                self._hmac_algorithm,
            ),
        }

        try:
            response = session.post(
                url,
                json=body,
                headers=headers,
                timeout=timeout,
            )
        except requests.RequestException as e:
            if isinstance(e, requests.ConnectionError | requests.Timeout):
                raise
            return AuthResult(
                success=False,
                error=f"HNAP challenge request failed: {type(e).__name__}: {e}",
            )

        data = parse_json_dict(
            response,
            context="HNAP challenge response",
        )
        if isinstance(data, AuthResult):
            return data

        login_response = data.get("LoginResponse", {})
        challenge = login_response.get("Challenge")
        public_key = login_response.get("PublicKey")
        cookie = login_response.get("Cookie")

        if not challenge or not public_key or not cookie:
            return AuthResult(
                success=False,
                error=(
                    "HNAP challenge response missing required fields "
                    f"(Challenge={challenge!r}, PublicKey={public_key!r}, "
                    f"Cookie={cookie!r})"
                ),
                response=response,
            )

        # Store challenge data for phase 2
        self._challenge = challenge
        self._public_key = public_key
        self._cookie = cookie

        _logger.debug("HNAP challenge received")
        return None

    def _login_with_credentials(
        self,
        session: requests.Session,
        url: str,
        username: str,
        password: str,
        *,
        timeout: int,
    ) -> AuthResult:
        """Compute credentials and send the login request (phase 2)."""
        # Derive keys from challenge
        private_key = hmac_hex(
            key=self._public_key + password,
            message=self._challenge,
            algorithm=self._hmac_algorithm,
        )
        login_password = hmac_hex(
            key=private_key,
            message=self._challenge,
            algorithm=self._hmac_algorithm,
        )

        # Set session cookies from challenge response.
        # Login.js sets both cookies after deriving the private key:
        #   $.cookie('uid', obj.Cookie, { path: '/' });
        #   $.cookie('PrivateKey', PrivateKey, { path: '/' });
        # Both are HNAP protocol-level cookies — some firmware returns
        # HTTP 500 on data requests when the PrivateKey cookie is missing.
        session.cookies.set("uid", self._cookie)
        session.cookies.set("PrivateKey", private_key)

        body = {
            "Login": {
                "Action": "login",
                "Username": username,
                "LoginPassword": login_password,
                "Captcha": "",
                "PrivateLogin": "LoginPassword",
            },
        }

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "SOAPAction": f'"{HNAP_NAMESPACE}Login"',
            "HNAP_AUTH": compute_auth_header(
                private_key,
                "Login",
                self._hmac_algorithm,
            ),
        }

        try:
            response = session.post(
                url,
                json=body,
                headers=headers,
                timeout=timeout,
            )
        except requests.RequestException as e:
            if isinstance(e, requests.ConnectionError | requests.Timeout):
                raise
            return AuthResult(
                success=False,
                error=f"HNAP login request failed: {type(e).__name__}: {e}",
            )

        data = parse_json_dict(
            response,
            context="HNAP login response",
        )
        if isinstance(data, AuthResult):
            return data

        login_response = data.get("LoginResponse", {})
        login_result = login_response.get("LoginResult", "")

        if login_result in ("LOCKUP", "REBOOT"):
            # Distinct from a rejected credential (#117): the modem is
            # refusing logins to protect itself, and retrying is what
            # reboots it. The orchestrator needs to tell the two apart
            # to report the right remedy.
            raise LoginLockoutError(f"HNAP firmware anti-brute-force triggered: LoginResult={login_result}")

        if login_result == "FAILED":
            return AuthResult(
                success=False,
                error="HNAP login failed: incorrect username or password",
                response=response,
            )

        if login_result not in ("OK", "OK_CHANGED"):
            return AuthResult(
                success=False,
                error=f"HNAP login unexpected result: {login_result!r}",
                response=response,
            )

        return AuthResult(
            success=True,
            auth_context=AuthContext(private_key=private_key),
        )


def create_manager(config: HnapAuth) -> HnapAuthManager:
    """Entry point for dynamic auth factory dispatch."""
    return HnapAuthManager(config)
