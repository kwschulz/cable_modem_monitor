"""Tests for HAR mock server — HNAP auth handler and integration.

HnapAuthHandler behavioral tests and HNAP server integration tests
(full login flow + data request using real auth manager and loader).

HTTP auth handler and server tests live in ``test_har_mock_server.py``.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests
from solentlabs.cable_modem_monitor_core.auth.hnap import HnapAuthManager
from solentlabs.cable_modem_monitor_core.loaders.hnap import HNAPLoader
from solentlabs.cable_modem_monitor_core.models.modem_config.auth import HnapAuth
from solentlabs.cable_modem_monitor_core.test_harness.auth import (
    HnapAuthHandler,
)
from solentlabs.cable_modem_monitor_core.test_harness.server import HARMockServer

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
# Layer 2: HNAP auth handler tests
# ---------------------------------------------------------------------------


class TestHnapAuthHandler:
    """Behavioral tests for HnapAuthHandler."""

    @pytest.fixture()
    def entries(self) -> list[dict[str, Any]]:
        """Load HNAP HAR entries from fixture."""
        return _load_entries("har_entries_hnap_auth.json")

    @pytest.fixture()
    def handler(self, entries: list[dict[str, Any]]) -> HnapAuthHandler:
        """Create an HNAP handler with MD5 algorithm."""
        return HnapAuthHandler(hmac_algorithm="md5", har_entries=entries)

    def test_login_request_before_auth(self, handler: HnapAuthHandler) -> None:
        """POST /HNAP1/ is a login request before authentication."""
        assert handler.is_login_request("POST", "/HNAP1/")

    def test_not_login_request_wrong_method(self, handler: HnapAuthHandler) -> None:
        """GET /HNAP1/ is not a login request."""
        assert not handler.is_login_request("GET", "/HNAP1/")

    def test_not_login_request_wrong_path(self, handler: HnapAuthHandler) -> None:
        """POST to other path is not a login request."""
        assert not handler.is_login_request("POST", "/other")

    def test_unauthenticated_by_default(self, handler: HnapAuthHandler) -> None:
        """New handler starts unauthenticated."""
        assert not handler.is_authenticated({})

    def test_challenge_response_structure(self, handler: HnapAuthHandler) -> None:
        """Phase 1 returns challenge, public key, and cookie."""
        # Compute valid pre-auth HNAP_AUTH header
        timestamp = "1234567890"
        soap_action = '"http://purenetworks.com/HNAP1/Login"'
        auth_hash = (
            hmac_mod.new(
                b"withoutloginkey",
                (timestamp + soap_action).encode("utf-8"),
                hashlib.md5,
            )
            .hexdigest()
            .upper()
        )

        body = json.dumps(
            {"Login": {"Action": "request", "Username": "admin"}},
        ).encode("utf-8")
        headers = {
            "hnap_auth": f"{auth_hash} {timestamp}",
            "soapaction": soap_action,
        }

        response = handler.handle_login("POST", "/HNAP1/", body, headers)
        assert response is not None
        assert response.status == 200

        data = json.loads(response.body)
        login_resp = data["LoginResponse"]
        assert login_resp["LoginResult"] == "OK"
        assert "Challenge" in login_resp
        assert "PublicKey" in login_resp
        assert "Cookie" in login_resp

    def test_invalid_json_body(self, handler: HnapAuthHandler) -> None:
        """Invalid JSON body with Login SOAPAction returns error."""
        response = handler.handle_login(
            "POST",
            "/HNAP1/",
            b"not json",
            {"soapaction": '"http://purenetworks.com/HNAP1/Login"'},
        )
        assert response is not None
        assert "ERROR" in response.body

    def test_non_login_soapaction_returns_none(
        self,
        handler: HnapAuthHandler,
    ) -> None:
        """POST without Login SOAPAction returns None (delegates to server)."""
        response = handler.handle_login(
            "POST",
            "/HNAP1/",
            b"{}",
            {"soapaction": '"http://purenetworks.com/HNAP1/GetMultipleHNAPs"'},
        )
        assert response is None

    def test_merged_response_contains_all_actions(
        self,
        entries: list[dict[str, Any]],
    ) -> None:
        """Merged response includes actions from all GetMultipleHNAPs entries."""
        handler = HnapAuthHandler(hmac_algorithm="md5", har_entries=entries)
        override = handler.get_route_override("POST", "/HNAP1/", b"", {})
        assert override is not None
        data = json.loads(override.body)
        hnap_resp = data["GetMultipleHNAPsResponse"]

        # From fixture entry 3 (channel data)
        assert "GetStatusDownstreamResponse" in hnap_resp
        assert "GetStatusUpstreamResponse" in hnap_resp
        # From fixture entry 4 (device status)
        assert "GetDeviceStatusResponse" in hnap_resp

    def test_merged_response_excludes_login(
        self,
        entries: list[dict[str, Any]],
    ) -> None:
        """Login entries are excluded from the merged data response."""
        handler = HnapAuthHandler(hmac_algorithm="md5", har_entries=entries)
        override = handler.get_route_override("POST", "/HNAP1/", b"", {})
        assert override is not None
        data = json.loads(override.body)
        hnap_resp = data["GetMultipleHNAPsResponse"]
        assert "LoginResponse" not in hnap_resp

    def test_route_override_non_hnap_path(
        self,
        handler: HnapAuthHandler,
    ) -> None:
        """Route override returns None for non-HNAP paths."""
        assert handler.get_route_override("GET", "/status.html", b"", {}) is None

    def test_sha256_algorithm(self, entries: list[dict[str, Any]]) -> None:
        """SHA256 algorithm is accepted for handler construction."""
        handler = HnapAuthHandler(hmac_algorithm="sha256", har_entries=entries)
        assert handler._hmac_algorithm == "sha256"


# ---------------------------------------------------------------------------
# Layer 3: HNAP server integration tests
# ---------------------------------------------------------------------------


class TestHARMockServerHnapAuth:
    """Integration tests for mock server with HNAP auth.

    Tests the full HNAP workflow: challenge -> login -> data request,
    exercising the real auth manager against the mock server.
    """

    @pytest.fixture()
    def entries(self) -> list[dict[str, Any]]:
        """Load HNAP HAR entries from fixture."""
        return _load_entries("har_entries_hnap_auth.json")

    def test_unauthenticated_returns_401(
        self,
        entries: list[dict[str, Any]],
    ) -> None:
        """Data request without HNAP auth returns 401."""
        config = _make_config({"transport": "hnap", "auth": {"strategy": "hnap", "hmac_algorithm": "md5"}})
        with HARMockServer(entries, modem_config=config) as server:
            resp = requests.post(f"{server.base_url}/HNAP1/")
            assert resp.status_code == 401

    def test_full_hnap_login_flow(
        self,
        entries: list[dict[str, Any]],
    ) -> None:
        """Full HNAP challenge-response login using real auth manager."""
        config = _make_config({"transport": "hnap", "auth": {"strategy": "hnap", "hmac_algorithm": "md5"}})
        auth_config = HnapAuth(strategy="hnap", hmac_algorithm="md5")
        auth_manager = HnapAuthManager(auth_config)

        with HARMockServer(entries, modem_config=config) as server:
            session = requests.Session()
            result = auth_manager.authenticate(
                session,
                server.base_url,
                username="admin",
                password="pw",
            )
            assert result.success, f"Auth failed: {result.error}"
            assert result.auth_context.private_key

    def test_data_request_after_login(
        self,
        entries: list[dict[str, Any]],
    ) -> None:
        """Authenticated data request returns merged HNAP response."""
        config = _make_config({"transport": "hnap", "auth": {"strategy": "hnap", "hmac_algorithm": "md5"}})
        auth_config = HnapAuth(strategy="hnap", hmac_algorithm="md5")
        auth_manager = HnapAuthManager(auth_config)

        with HARMockServer(entries, modem_config=config) as server:
            session = requests.Session()
            result = auth_manager.authenticate(
                session,
                server.base_url,
                username="admin",
                password="pw",
            )
            assert result.success

            # Build a minimal parser config with HNAP response keys
            mock_config = MagicMock()
            mock_config.downstream = MagicMock()
            mock_config.downstream.response_key = "GetStatusDownstreamResponse"
            mock_config.upstream = MagicMock()
            mock_config.upstream.response_key = "GetStatusUpstreamResponse"
            mock_config.system_info = None

            loader = HNAPLoader(
                session=session,
                base_url=server.base_url,
                private_key=result.auth_context.private_key,
                hmac_algorithm="md5",
            )
            resources = loader.fetch(mock_config)

            assert "hnap_response" in resources
            hnap_resp = resources["hnap_response"]
            assert "GetStatusDownstreamResponse" in hnap_resp
            assert "GetStatusUpstreamResponse" in hnap_resp
            # From second GetMultipleHNAPs entry (merged)
            assert "GetDeviceStatusResponse" in hnap_resp

    def test_reauth_after_prior_session(
        self,
        entries: list[dict[str, Any]],
    ) -> None:
        """New session can re-authenticate after prior session completed.

        Regression test: config flow validation logs in (session A),
        then orchestrator creates a new session (B) and must be able
        to log in again despite the harness already being authenticated.
        """
        config = _make_config({"transport": "hnap", "auth": {"strategy": "hnap", "hmac_algorithm": "md5"}})
        auth_config = HnapAuth(strategy="hnap", hmac_algorithm="md5")

        with HARMockServer(entries, modem_config=config) as server:
            # Session A — config flow validation
            session_a = requests.Session()
            manager_a = HnapAuthManager(auth_config)
            result_a = manager_a.authenticate(
                session_a,
                server.base_url,
                username="admin",
                password="pw",
            )
            assert result_a.success, f"Session A failed: {result_a.error}"

            # Session B — orchestrator poll (fresh session)
            session_b = requests.Session()
            manager_b = HnapAuthManager(auth_config)
            result_b = manager_b.authenticate(
                session_b,
                server.base_url,
                username="admin",
                password="pw",
            )
            assert result_b.success, f"Session B failed: {result_b.error}"
            assert result_b.auth_context.private_key

    def test_orchestrated_reauth_after_prior_session(
        self,
        entries: list[dict[str, Any]],
    ) -> None:
        """Full orchestrator cycle succeeds after a prior session on same server.

        Regression test for the HA dogfood scenario: config flow
        validation runs a full orchestrator collect (session A), then
        the integration starts and runs another collect (session B)
        against the same persistent harness. Both must succeed.
        """
        from solentlabs.cable_modem_monitor_core.config_loader import (
            load_parser_config,
        )
        from solentlabs.cable_modem_monitor_core.orchestration.collector import (
            ModemDataCollector,
        )

        config = _make_config({"transport": "hnap", "auth": {"strategy": "hnap", "hmac_algorithm": "md5"}})
        parser_config = load_parser_config(FIXTURES_DIR / "parser_hnap_minimal.yaml")

        with HARMockServer(entries, modem_config=config) as server:
            # Session A — config flow validation collect
            collector_a = ModemDataCollector(
                modem_config=config,
                parser_config=parser_config,
                post_processor=None,
                base_url=server.base_url,
                username="admin",
                password="pw",
            )
            result_a = collector_a.execute()
            assert result_a.success, f"Session A failed: {result_a.error}"
            assert result_a.modem_data is not None

            # Session B — orchestrator poll (fresh collector, fresh session)
            collector_b = ModemDataCollector(
                modem_config=config,
                parser_config=parser_config,
                post_processor=None,
                base_url=server.base_url,
                username="admin",
                password="pw",
            )
            result_b = collector_b.execute()
            assert result_b.success, f"Session B failed: {result_b.error}"
            assert result_b.modem_data is not None
            assert len(result_b.modem_data.get("downstream", [])) > 0
