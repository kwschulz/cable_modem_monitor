"""Tests for HAR mock server — SJCL AES-CCM auth integration.

Runs the real ``FormSjclAuthManager`` against ``HARMockServer`` with
actual HTTP, validating the full two-phase auth flow: GET login page
for JS crypto variables, POST AES-CCM encrypted credentials, decrypt
response to extract CSRF nonce, and access gated data pages.

The mock server's test password is ``"pw"`` (defined in
``FormSjclAuthHandler._TEST_PASSWORD``).  The auth manager must derive
the same AES key the mock server uses — if the salt encoding drifts,
decryption fails and these tests catch it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import requests
from solentlabs.cable_modem_monitor_core.auth.form_sjcl import (
    FormSjclAuthManager,
)
from solentlabs.cable_modem_monitor_core.test_harness.auth.form_sjcl import (
    FormSjclAuthHandler,
)
from solentlabs.cable_modem_monitor_core.test_harness.server import HARMockServer

from tests._helpers import load_fixture

FIXTURES_DIR = Path(__file__).parent / "fixtures"

_TEST_PASSWORD = FormSjclAuthHandler._TEST_PASSWORD


def _load_entries(name: str) -> list[dict[str, Any]]:
    """Load HAR entries from a fixture file."""
    data = load_fixture(FIXTURES_DIR / name)
    return list(data["_entries"])


def _make_config(data: dict[str, Any]) -> Any:
    """Validate a raw modem config dict into a ModemConfig instance."""
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


_SJCL_AUTH_CONFIG: dict[str, Any] = {
    "auth": {
        "strategy": "form_sjcl",
        "login_page": "/",
        "login_endpoint": "/api/login",
        "session_validation_endpoint": "/php/ajaxSet_Session.php",
        "pbkdf2_iterations": 1000,
        "pbkdf2_key_length": 128,
        "ccm_tag_length": 16,
        "encrypt_aad": "loginPassword",
        "decrypt_aad": "nonce",
        "csrf_header": "csrfNonce",
        "cookie_name": "PHPSESSID",
    },
}


class TestHARMockServerSjclAuth:
    """Integration tests: real FormSjclAuthManager against HARMockServer.

    Exercises the full SJCL flow over real HTTP: GET login page for
    JS crypto variables, POST AES-CCM encrypted credentials, decrypt
    the response nonce, and verify data page access.
    """

    @pytest.fixture()
    def entries(self) -> list[dict[str, Any]]:
        """Load no-auth entries as data page fixtures."""
        return _load_entries("har_entries_no_auth.json")

    @pytest.fixture()
    def config(self) -> Any:
        """Modem config with SJCL auth."""
        return _make_config(_SJCL_AUTH_CONFIG)

    def test_data_pages_require_auth(self, entries: list[dict[str, Any]], config: Any) -> None:
        """Data pages return 401 before login."""
        with HARMockServer(entries, modem_config=config) as server:
            resp = requests.get(f"{server.base_url}/status.html")
            assert resp.status_code == 401

    def test_full_sjcl_flow(self, entries: list[dict[str, Any]], config: Any) -> None:
        """Full SJCL flow: GET page vars, POST encrypted, data accessible."""
        with HARMockServer(entries, modem_config=config) as server:
            session = requests.Session()
            manager = FormSjclAuthManager(config.auth)

            result = manager.authenticate(
                session,
                server.base_url,
                "admin",
                _TEST_PASSWORD,
            )

            assert result.success is True
            assert "csrfNonce" in session.headers

            # Data page now accessible
            data_resp = session.get(f"{server.base_url}/status.html")
            assert data_resp.status_code == 200

    def test_wrong_password_decryption_fails(self, entries: list[dict[str, Any]], config: Any) -> None:
        """Wrong password derives wrong key; response decryption fails."""
        with HARMockServer(entries, modem_config=config) as server:
            session = requests.Session()
            manager = FormSjclAuthManager(config.auth)

            result = manager.authenticate(
                session,
                server.base_url,
                "admin",
                "wrong_password",
            )

            assert result.success is False
            assert "decryption failed" in result.error.lower()
