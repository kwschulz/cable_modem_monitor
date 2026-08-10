"""Tests for orchestration data models."""

from __future__ import annotations

from solentlabs.cable_modem_monitor_core.orchestration.models import (
    HealthInfo,
    ModemIdentity,
    ModemResult,
    ModemSnapshot,
    OrchestratorDiagnostics,
    ResourceFetch,
    RestartResult,
)
from solentlabs.cable_modem_monitor_core.orchestration.signals import (
    CollectorSignal,
    ConnectionStatus,
    DocsisStatus,
    HealthStatus,
)


class TestModemIdentity:
    """ModemIdentity dataclass construction and defaults."""

    def test_required_fields_only(self) -> None:
        """Identity with just manufacturer and model."""
        identity = ModemIdentity(manufacturer="Solent Labs", model="T100")
        assert identity.manufacturer == "Solent Labs"
        assert identity.model == "T100"
        assert identity.docsis_version is None
        assert identity.release_date is None
        assert identity.status == "awaiting_verification"

    def test_all_fields(self) -> None:
        """Identity with all optional fields populated."""
        identity = ModemIdentity(
            manufacturer="Solent Labs",
            model="T200",
            docsis_version="3.1",
            release_date="2023",
            status="confirmed",
        )
        assert identity.docsis_version == "3.1"
        assert identity.release_date == "2023"
        assert identity.status == "confirmed"


class TestModemResult:
    """ModemResult dataclass defaults and construction."""

    def test_success_defaults(self) -> None:
        """Successful result has OK signal and empty error."""
        result = ModemResult(success=True, modem_data={"downstream": []})
        assert result.signal == CollectorSignal.OK
        assert result.error == ""
        assert result.modem_data is not None

    def test_failure_defaults(self) -> None:
        """Failed result has None modem_data by default."""
        result = ModemResult(
            success=False,
            signal=CollectorSignal.AUTH_FAILED,
            error="wrong password",
        )
        assert result.modem_data is None
        assert result.error == "wrong password"

    def test_zero_channels_is_success(self) -> None:
        """Zero channels with system_info is a valid success result."""
        data = {"downstream": [], "upstream": [], "system_info": {"fw": "1.0"}}
        result = ModemResult(success=True, modem_data=data)
        assert result.success is True
        assert result.signal == CollectorSignal.OK


class TestModemSnapshot:
    """ModemSnapshot dataclass defaults and construction."""

    def test_minimal_snapshot(self) -> None:
        """Snapshot with only required fields."""
        snap = ModemSnapshot(
            connection_status=ConnectionStatus.ONLINE,
            docsis_status=DocsisStatus.OPERATIONAL,
        )
        assert snap.modem_data is None
        assert snap.health_info is None
        assert snap.collector_signal == CollectorSignal.OK
        assert snap.error == ""

    def test_full_snapshot(self) -> None:
        """Snapshot with all fields populated."""
        health = HealthInfo(health_status=HealthStatus.RESPONSIVE)
        snap = ModemSnapshot(
            connection_status=ConnectionStatus.ONLINE,
            docsis_status=DocsisStatus.OPERATIONAL,
            modem_data={"downstream": [{"channel_id": 1}]},
            health_info=health,
            collector_signal=CollectorSignal.OK,
        )
        assert snap.health_info is not None
        assert snap.modem_data is not None


class TestModemSnapshotToEventPayload:
    """ModemSnapshot.to_event_payload() serialization."""

    def test_top_level_fields(self) -> None:
        """Core status fields are present at the top level."""
        snap = ModemSnapshot(
            connection_status=ConnectionStatus.ONLINE,
            docsis_status=DocsisStatus.OPERATIONAL,
            collector_signal=CollectorSignal.OK,
            error="",
        )
        payload = snap.to_event_payload()
        assert payload.connection_status == "online"
        assert payload.docsis_status == "Operational"
        assert payload.collector_signal == "ok"
        assert payload.error == ""
        assert payload.modem_data is None
        assert payload.health_info is None

    def test_docsis_status_stripped_from_system_info(self) -> None:
        """docsis_status is removed from system_info in the emitted payload."""
        snap = ModemSnapshot(
            connection_status=ConnectionStatus.ONLINE,
            docsis_status=DocsisStatus.OPERATIONAL,
            modem_data={
                "downstream": [],
                "upstream": [],
                "system_info": {
                    "docsis_status": "Operational",
                    "system_uptime": "1 days 00h:00m:00s",
                },
            },
        )
        payload = snap.to_event_payload()
        assert payload.modem_data is not None
        assert "docsis_status" not in payload.modem_data.system_info
        assert payload.modem_data.system_info["system_uptime"] == "1 days 00h:00m:00s"

    def test_original_modem_data_not_mutated(self) -> None:
        """to_event_payload() does not modify the snapshot's modem_data dict."""
        system_info = {"docsis_status": "Operational", "system_uptime": "0 days"}
        snap = ModemSnapshot(
            connection_status=ConnectionStatus.ONLINE,
            docsis_status=DocsisStatus.OPERATIONAL,
            modem_data={"downstream": [], "upstream": [], "system_info": system_info},
        )
        snap.to_event_payload()
        assert "docsis_status" in system_info

    def test_failure_snapshot_no_modem_data(self) -> None:
        """Top-level docsis_status is present even when modem_data is null."""
        snap = ModemSnapshot(
            connection_status=ConnectionStatus.UNREACHABLE,
            docsis_status=DocsisStatus.UNKNOWN,
            collector_signal=CollectorSignal.CONNECTIVITY,
            error="timed out",
        )
        payload = snap.to_event_payload()
        assert payload.docsis_status == "unknown"
        assert payload.modem_data is None

    def test_system_info_without_docsis_status(self) -> None:
        """system_info that never had docsis_status is left unchanged."""
        snap = ModemSnapshot(
            connection_status=ConnectionStatus.ONLINE,
            docsis_status=DocsisStatus.OPERATIONAL,
            modem_data={
                "downstream": [],
                "upstream": [],
                "system_info": {"system_uptime": "2 days"},
            },
        )
        payload = snap.to_event_payload()
        assert payload.modem_data is not None
        assert payload.modem_data.system_info == {"system_uptime": "2 days"}


class TestResourceFetch:
    """ResourceFetch timing, size, and response metadata."""

    def test_construction(self) -> None:
        fetch = ResourceFetch(path="/status.html", duration_ms=800.0, size_bytes=12480)
        assert fetch.path == "/status.html"
        assert fetch.duration_ms == 800.0
        assert fetch.size_bytes == 12480
        assert fetch.status_code == 0
        assert fetch.content_type == ""

    def test_construction_with_response_metadata(self) -> None:
        """ResourceFetch carries HTTP status code and Content-Type."""
        fetch = ResourceFetch(
            path="/status.html",
            duration_ms=800.0,
            size_bytes=12480,
            status_code=200,
            content_type="text/html",
        )
        assert fetch.status_code == 200
        assert fetch.content_type == "text/html"

    def test_to_dict(self) -> None:
        """to_dict returns all fields as a plain dict."""
        fetch = ResourceFetch(
            path="/status.html",
            duration_ms=800.0,
            size_bytes=12480,
            status_code=200,
            content_type="text/html; charset=utf-8",
        )
        result = fetch.to_dict()
        assert result == {
            "path": "/status.html",
            "duration_ms": 800.0,
            "size_bytes": 12480,
            "status_code": 200,
            "content_type": "text/html; charset=utf-8",
        }

    def test_to_dict_defaults(self) -> None:
        """to_dict includes default status_code and content_type."""
        fetch = ResourceFetch(path="/status.html", duration_ms=800.0, size_bytes=12480)
        result = fetch.to_dict()
        assert result["status_code"] == 0
        assert result["content_type"] == ""


class TestHealthInfo:
    """HealthInfo probe results."""

    def test_defaults(self) -> None:
        """Latency fields default to None (not measured)."""
        info = HealthInfo(health_status=HealthStatus.UNKNOWN)
        assert info.icmp_latency_ms is None
        assert info.http_latency_ms is None

    def test_responsive_with_latency(self) -> None:
        info = HealthInfo(
            health_status=HealthStatus.RESPONSIVE,
            icmp_latency_ms=4.0,
            http_latency_ms=12.0,
        )
        assert info.icmp_latency_ms == 4.0
        assert info.http_latency_ms == 12.0


class TestOrchestratorDiagnostics:
    """OrchestratorDiagnostics snapshot."""

    def test_initial_state(self) -> None:
        """Metrics before any poll."""
        metrics = OrchestratorDiagnostics(
            poll_duration=None,
            auth_failure_streak=0,
            circuit_breaker_open=False,
            session_is_valid=False,
        )
        assert metrics.resource_fetches == []
        assert metrics.last_poll_at is None
        assert metrics.auth_strategy == ""

    def test_after_successful_poll(self) -> None:
        """Metrics after a successful collection."""
        fetches = [
            ResourceFetch("/status.html", 800.0, 12480),
            ResourceFetch("/info.html", 1200.0, 8192),
        ]
        metrics = OrchestratorDiagnostics(
            poll_duration=2.5,
            auth_failure_streak=0,
            circuit_breaker_open=False,
            session_is_valid=True,
            resource_fetches=fetches,
            last_poll_at="2026-05-01T12:00:00+00:00",
        )
        assert len(metrics.resource_fetches) == 2
        assert metrics.poll_duration == 2.5

    def test_to_dict_initial_state(self) -> None:
        """to_dict serializes all fields including empty resource_fetches."""
        metrics = OrchestratorDiagnostics(
            poll_duration=None,
            auth_failure_streak=0,
            circuit_breaker_open=False,
            session_is_valid=False,
        )
        result = metrics.to_dict()
        assert result == {
            "poll_duration": None,
            "auth_failure_streak": 0,
            "circuit_breaker_open": False,
            "circuit_trip_signal": None,
            "session_is_valid": False,
            "auth_strategy": "",
            "connectivity_streak": 0,
            "connectivity_backoff_remaining": 0,
            "stale_session_recovery_streak": 0,
            "session_reuse_disabled": False,
            "resource_fetches": [],
            "last_poll_at": None,
            "last_stub_body": {},
            "system_info_fields_missing": [],
            "system_info_fields_failed": {},
        }

    def test_to_dict_with_fetches(self) -> None:
        """to_dict serializes nested ResourceFetch objects."""
        fetches = [
            ResourceFetch("/status.html", 800.0, 12480, 200, "text/html"),
            ResourceFetch("/info.html", 1200.0, 8192, 200, "text/html"),
        ]
        metrics = OrchestratorDiagnostics(
            poll_duration=2.5,
            auth_failure_streak=1,
            circuit_breaker_open=False,
            session_is_valid=True,
            auth_strategy="form",
            connectivity_streak=3,
            connectivity_backoff_remaining=2,
            resource_fetches=fetches,
            last_poll_at="2026-05-01T12:00:00+00:00",
        )
        result = metrics.to_dict()
        assert result["poll_duration"] == 2.5
        assert result["auth_failure_streak"] == 1
        assert result["auth_strategy"] == "form"
        assert result["connectivity_streak"] == 3
        assert result["connectivity_backoff_remaining"] == 2
        assert result["last_poll_at"] == "2026-05-01T12:00:00+00:00"
        assert len(result["resource_fetches"]) == 2
        assert result["resource_fetches"][0] == {
            "path": "/status.html",
            "duration_ms": 800.0,
            "size_bytes": 12480,
            "status_code": 200,
            "content_type": "text/html",
        }


class TestRestartResult:
    """RestartResult dispatch outcome."""

    def test_success(self) -> None:
        result = RestartResult(
            success=True,
            elapsed_seconds=3.5,
        )
        assert result.success is True
        assert result.error == ""

    def test_command_failed(self) -> None:
        result = RestartResult(
            success=False,
            elapsed_seconds=2.1,
            error="command_failed",
        )
        assert not result.success
        assert result.error == "command_failed"
