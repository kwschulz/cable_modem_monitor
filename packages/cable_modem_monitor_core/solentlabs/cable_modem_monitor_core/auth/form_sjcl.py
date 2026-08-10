"""SJCL AES-CCM encrypted form authentication manager.

SJCL (Stanford JavaScript Crypto Library) is a JS crypto library used
by some modem firmwares (e.g., Arris Touchstone gateways) to encrypt
credentials client-side before POSTing.  This manager implements the
Python equivalent: credentials are encrypted with AES-CCM using a
PBKDF2-derived key, and the server's login response is also encrypted
and must be decrypted to extract the CSRF nonce.

Requires the ``cryptography`` package (install Core with ``[sjcl]``).

Auth flow
---------
1. **GET login page** — parse JS variables: ``myIv``, ``mySalt``,
   ``currentSessionId`` from the embedded ``<script>`` block.
2. **Derive AES key** — ``PBKDF2(password, salt, iterations, key_len)``.
3. **Encrypt credentials** — AES-CCM encrypt
   ``{"Password": "<pw>", "Nonce": "<sessionId>"}`` with AAD from
   config (default ``"loginPassword"``).
4. **POST login** — send ``{"EncryptData": "<hex>", "Name": "<user>",
   "AuthData": "<encrypt_aad>"}``.
5. **Decrypt response** — the ``encryptData`` field in the JSON
   response is AES-CCM ciphertext.  Decrypt with AAD from config
   (default ``"nonce"``) to extract the CSRF nonce.
6. **POST session validation** — if ``session_validation_endpoint``
   is configured, POST with the ``csrfNonce`` header to finalize.

See MODEM_YAML_SPEC.md ``form_sjcl`` strategy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

import requests

from ..models.modem_config.auth import FormSjclAuth
from .base import AuthResult, BaseAuthManager
from .response import post_json

_logger = logging.getLogger(__name__)

# Only capture the three JS variables we need from the login page.
_WANTED_VARS = frozenset({"myIv", "mySalt", "currentSessionId"})

_VAR_RE = re.compile(
    r"(?:var\s+)?(\w+)\s*=\s*'([^']*)'",
)


class FormSjclAuthManager(BaseAuthManager):
    """SJCL AES-CCM encrypted form auth.

    The client encrypts credentials with AES in CCM mode using a
    PBKDF2-derived key.  The server response is also encrypted —
    must decrypt to extract the CSRF nonce.

    Args:
        config: Validated ``FormSjclAuth`` config from modem.yaml.
    """

    def __init__(self, config: FormSjclAuth) -> None:
        self._config = config

    def headers(self) -> frozenset[str]:
        names = {"cookie"}
        if self._config.csrf_header:
            names.add(self._config.csrf_header.lower())
        return frozenset(names)

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
        """Execute the SJCL AES-CCM login flow.

        Args:
            session: Session to configure with auth state.
            base_url: Modem base URL.
            username: Username credential.
            password: Password credential.
            timeout: Per-request timeout in seconds.

        Returns:
            AuthResult with login response.
        """
        # Lazy import — only needed when this strategy is active.
        aesccm_cls = _import_aesccm()
        if isinstance(aesccm_cls, AuthResult):
            return aesccm_cls

        config = self._config
        page_url = f"{base_url}{config.login_page}"
        login_url = f"{base_url}{config.login_endpoint}"

        # Step 1: GET login page, extract JS variables
        page_vars = _fetch_page_vars(session, page_url, timeout)
        if isinstance(page_vars, AuthResult):
            return page_vars

        # _fetch_page_vars has already rejected a page missing either of
        # the two required variables, so it can attach the response.
        iv_hex = page_vars["myIv"]
        salt = page_vars["mySalt"]
        session_id = page_vars.get("currentSessionId", "")

        iv_result = _validate_iv(iv_hex)
        if isinstance(iv_result, AuthResult):
            return iv_result
        iv_bytes = iv_result

        # Step 2: Derive AES key via PBKDF2
        key = _derive_key(
            password,
            salt,
            config.pbkdf2_iterations,
            config.pbkdf2_key_length,
        )

        # Step 3: Encrypt credentials
        plaintext = json.dumps({"Password": password, "Nonce": session_id})

        cipher = aesccm_cls(key, tag_length=config.ccm_tag_length)
        encrypted = cipher.encrypt(
            iv_bytes,
            plaintext.encode("utf-8"),
            config.encrypt_aad.encode("utf-8"),
        )

        # Step 4: POST login and check status
        login_payload = {
            "EncryptData": encrypted.hex(),
            "Name": username,
            "AuthData": config.encrypt_aad,
        }
        submit_result = _submit_login(
            session,
            login_url,
            login_payload,
            config.login_endpoint,
            timeout,
        )
        if isinstance(submit_result, AuthResult):
            return submit_result
        login_response, login_json = submit_result

        # Step 5: Decrypt response to extract CSRF nonce
        enc_data_hex = login_json.get("encryptData", "")
        if enc_data_hex and config.csrf_header:
            nonce_result = _decrypt_csrf_nonce(
                cipher,
                iv_bytes,
                enc_data_hex,
                config.decrypt_aad,
            )
            if isinstance(nonce_result, AuthResult):
                return nonce_result
            session.headers[config.csrf_header] = nonce_result
            _logger.debug("CSRF nonce set: %s", config.csrf_header)

        # Step 6: Session validation (optional)
        if config.session_validation_endpoint:
            validation_url = f"{base_url}{config.session_validation_endpoint}"
            val_result = _validate_session(session, validation_url, timeout)
            if isinstance(val_result, AuthResult):
                return val_result

        _logger.log(
            log_level,
            "SJCL login succeeded: cookies=%s",
            list(session.cookies.keys()),
        )

        return AuthResult(
            success=True,
            response=login_response,
            response_url=config.login_endpoint,
        )


def _import_aesccm() -> Any | AuthResult:
    """Import AESCCM class from cryptography, or return error.

    Single import point — avoids repeated lazy imports in encrypt
    and decrypt helpers.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESCCM

        return AESCCM
    except ImportError:
        return AuthResult(
            success=False,
            error=(
                "cryptography package required for form_sjcl auth. "
                "Install with: pip install "
                "solentlabs-cable-modem-monitor-core[sjcl]"
            ),
        )


def _validate_iv(iv_hex: str) -> bytes | AuthResult:
    """Validate and decode IV hex string for AES-CCM.

    AES-CCM nonce must be 7-13 bytes per RFC 3610.
    """
    try:
        iv_bytes = bytes.fromhex(iv_hex)
    except ValueError:
        return AuthResult(
            success=False,
            error=f"myIv is not valid hex: {iv_hex!r}",
        )
    if not 7 <= len(iv_bytes) <= 13:
        return AuthResult(
            success=False,
            error=f"myIv decoded to {len(iv_bytes)} bytes, " "AES-CCM nonce must be 7-13 bytes",
        )
    return iv_bytes


def _decrypt_csrf_nonce(
    cipher: Any,
    iv: bytes,
    enc_hex: str,
    aad_str: str,
) -> str | AuthResult:
    """Decrypt the CSRF nonce from the login response.

    Returns the nonce string, or AuthResult on failure.
    """
    try:
        enc_bytes = bytes.fromhex(enc_hex)
    except ValueError:
        return AuthResult(
            success=False,
            error="encryptData in login response is not valid hex",
        )
    try:
        nonce_bytes = cipher.decrypt(iv, enc_bytes, aad_str.encode("utf-8"))
    except Exception:
        return AuthResult(
            success=False,
            error="AES-CCM decryption failed (wrong password or corrupted data)",
        )
    return str(nonce_bytes.decode("utf-8"))


def _fetch_page_vars(
    session: requests.Session,
    url: str,
    timeout: int,
) -> dict[str, str] | AuthResult:
    """GET the login page and extract the ``myIv``/``mySalt``/``currentSessionId`` assignments."""
    try:
        resp = session.get(url, timeout=timeout)
    except requests.RequestException as e:
        if isinstance(e, requests.ConnectionError | requests.Timeout):
            raise
        return AuthResult(success=False, error=f"Login page fetch failed: {type(e).__name__}: {e}")

    # An HTTP error is a failed login whatever the body holds, and the
    # response has to come home with it. The collector reads the attached
    # status to tell a modem declining to serve (AUTH_UNAVAILABLE, UC-87a)
    # from a rejected credential; with nothing attached a 503 read as a
    # wrong password and tripped the breaker on the first poll.
    if resp.status_code >= 400:
        return AuthResult(
            success=False,
            error=f"Login page returned HTTP {resp.status_code}",
            response=resp,
        )

    variables: dict[str, str] = {}
    for match in _VAR_RE.finditer(resp.text):
        name = match.group(1)
        if name in _WANTED_VARS:
            variables[name] = match.group(2)

    # Answered under 400, but not with the login page this strategy expects:
    # wrong device at the address, or firmware that moved the variables. The
    # body is the only thing that distinguishes those, so it rides along.
    if not variables.get("myIv") or not variables.get("mySalt"):
        return AuthResult(
            success=False,
            error="Login page missing myIv or mySalt JS variables",
            response=resp,
        )

    return variables


def _derive_key(
    password: str,
    salt_hex: str,
    iterations: int,
    key_length_bits: int,
) -> bytes:
    """Derive an AES key using PBKDF2-HMAC-SHA256.

    Args:
        password: Plaintext password (UTF-8 encoded for PBKDF2).
        salt_hex: Salt as a hex string from the login page's
            ``mySalt`` JS variable.  Hex-decoded to binary bytes
            to match SJCL's ``sjcl.codec.hex.toBits(salt)``
            before calling ``sjcl.misc.pbkdf2()``.
        iterations: PBKDF2 iteration count.
        key_length_bits: Desired key length in bits.

    Returns:
        Raw key bytes (not hex).
    """
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        iterations,
        dklen=key_length_bits // 8,
    )


def _submit_login(
    session: requests.Session,
    url: str,
    payload: dict[str, Any],
    endpoint: str,
    timeout: int,
) -> tuple[requests.Response, dict[str, Any]] | AuthResult:
    """POST the encrypted login payload and validate p_status.

    Returns (response, json_body) on success, AuthResult on failure.
    """
    result = post_json(session, url, payload, timeout)
    if isinstance(result, AuthResult):
        return result
    response, body = result

    status = body.get("p_status", "")
    if status not in ("AdminMatch", "Match"):
        return AuthResult(
            success=False,
            error=f"Login rejected: p_status={status!r}",
            response=response,
            response_url=endpoint,
        )
    return response, body


def _validate_session(
    session: requests.Session,
    url: str,
    timeout: int,
) -> None | AuthResult:
    """POST an empty session validation request.

    The browser sends an empty POST (no JSON body) with the
    csrfNonce header already on the session.  The modem returns
    200 to confirm.

    Returns ``None`` on success, ``AuthResult`` on failure.
    """
    try:
        resp = session.post(url, timeout=timeout)
    except requests.RequestException as e:
        if isinstance(e, requests.ConnectionError | requests.Timeout):
            raise
        return AuthResult(
            success=False,
            error=f"Session validation POST failed: {type(e).__name__}: {e}",
        )
    if resp.status_code != 200:
        return AuthResult(
            success=False,
            error=(f"Session validation failed " f"(status {resp.status_code})"),
            response=resp,
        )
    return None


def create_manager(config: FormSjclAuth) -> FormSjclAuthManager:
    """Entry point for dynamic auth factory dispatch."""
    return FormSjclAuthManager(config)
