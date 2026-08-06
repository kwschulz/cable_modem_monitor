"""Data models for the orchestration layer.

Result types, diagnostics, and health info dataclasses. These are the
contracts between orchestration components and consumers.

See ORCHESTRATION_SPEC.md Data Models section.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .signals import (
    CollectorSignal,
    ConnectionStatus,
    HealthStatus,
)

if TYPE_CHECKING:
    from .event_payload import SnapshotEventPayload


@dataclass
class ModemIdentity:
    """Static modem metadata from modem.yaml.

    Populated once at config load time. Consumers use this for
    display and device registration. The model field comes from
    the modem.yaml config — if the modem reports a different model
    in system_info at runtime, that value is available in
    modem_data.system_info and takes precedence for display.

    Attributes:
        manufacturer: Modem manufacturer (e.g., "Arris", "Netgear").
        model: Model name from modem.yaml (e.g., "SB8200").
        docsis_version: DOCSIS version (e.g., "3.1"). None if unknown.
        release_date: Release date string (e.g., "2020"). None if unknown.
        status: Verification status — "confirmed", "awaiting_verification",
            or "unsupported".
    """

    manufacturer: str
    model: str
    docsis_version: str | None = None
    release_date: str | None = None
    status: str = "awaiting_verification"


@dataclass
class ModemResult:
    """Result of a single data collection attempt.

    success=True means the collection pipeline ran without errors.
    It does NOT mean channels were found -- a modem without cable
    signal returns success=True with empty channel lists. The
    orchestrator interprets the data and derives connection status.

    Attributes:
        success: Whether the collection pipeline completed without error.
        modem_data: Parsed channel and system_info data. Present on
            success (may have empty channel lists). None on failure.
        signal: Failure classification when success is False.
            Always OK when success is True.
        error: Human-readable error detail for logging/diagnostics.
            Empty string on success.
        auth_status_code: HTTP status of the failed login response
            (AUTH_FAILED only, when the modem answered) — lets the
            circuit breaker message distinguish endpoint-not-found
            from credential rejection.
    """

    success: bool
    modem_data: dict[str, Any] | None = None
    signal: CollectorSignal = CollectorSignal.OK
    error: str = ""
    auth_status_code: int | None = None


@dataclass
class ResourceFetch:
    """Timing and size for a single resource fetch.

    Captured by the ResourceLoader during data collection. One entry
    per resource page (e.g., /status.html, /connection.html).

    Attributes:
        path: Resource path (e.g., "/status.html").
        duration_ms: Fetch time in milliseconds.
        size_bytes: Response body size in bytes.
        status_code: HTTP response status code (e.g., 200, 401).
        content_type: Response Content-Type header value. Empty string
            when the header is absent or not applicable.
    """

    path: str
    duration_ms: float
    size_bytes: int
    status_code: int = 0
    content_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for diagnostics output."""
        return {
            "path": self.path,
            "duration_ms": self.duration_ms,
            "size_bytes": self.size_bytes,
            "status_code": self.status_code,
            "content_type": self.content_type,
        }


@dataclass
class HealthInfo:
    """Result of a health probe cycle.

    Only contains actual probe measurements. The HealthMonitor considers
    collection evidence internally when deriving health_status, but does
    not fabricate probe results -- None means "not measured."

    Attributes:
        health_status: Derived status from probe combination and
            collection evidence.
        icmp_latency_ms: Round-trip time in milliseconds. None if
            ICMP failed, not supported, or not attempted.
        tcp_latency_ms: TCP handshake time in milliseconds to the
            modem's web port. Probes L4 reachability and the modem's
            TCP listen/accept path. None if the TCP probe failed or
            was not attempted.
        http_latency_ms: HTTP server response time in milliseconds,
            excluding TCP connection setup overhead. Populated only
            on modems where ``supports_head=True`` (HEAD bypasses the
            handler and gives a clean unimodal signal). None on
            GET-only modems, HTTP failure, or when suppressed by
            collection evidence.
    """

    health_status: HealthStatus
    icmp_latency_ms: float | None = None
    tcp_latency_ms: float | None = None
    http_latency_ms: float | None = None


@dataclass
class ModemSnapshot:
    """Point-in-time snapshot of everything the orchestrator knows.

    Top-level result consumers receive from get_modem_data(). Combines
    the ModemDataCollector output, health probe output, and
    orchestrator-derived fields into a single structure.

    Attributes:
        connection_status: Derived from collector signal and data.
        docsis_status: Read from system_info["docsis_status"] (parser-
            provided or orchestrator-enriched from lock_status fields).
            Falls back to "unknown" when the field is absent.
        modem_data: Parsed channel and system_info data. None on
            collection failure. Channel counts and aggregate fields
            (e.g., total_corrected) are computed by the parser
            coordinator — consumers read them from system_info
            regardless of whether the modem reported them natively.
        health_info: Health probe results. None if no health monitor.
        collector_signal: Raw signal from the collector (for diagnostics).
        error: Human-readable error summary.
        stats_last_reset: Timestamp when error counters were detected
            to have reset (decreased between polls). Used as a proxy
            for last boot time when the modem lacks native uptime.
            None until a reset is detected.
    """

    connection_status: ConnectionStatus
    docsis_status: str
    modem_data: dict[str, Any] | None = None
    health_info: HealthInfo | None = None
    collector_signal: CollectorSignal = CollectorSignal.OK
    error: str = ""
    stats_last_reset: datetime | None = None

    def to_event_payload(self) -> SnapshotEventPayload:
        """Build the consumer event payload from this snapshot."""
        from .event_payload import (
            SCHEMA_VERSION,
            HealthInfoPayload,
            ModemDataPayload,
            SnapshotEventPayload,
        )

        health_info = None
        if self.health_info:
            health_info = HealthInfoPayload(
                health_status=self.health_info.health_status.value,
                icmp_latency_ms=self.health_info.icmp_latency_ms,
                tcp_latency_ms=self.health_info.tcp_latency_ms,
                http_latency_ms=self.health_info.http_latency_ms,
            )

        modem_data = None
        if self.modem_data:
            # docsis_status lives at the top level of SnapshotEventPayload;
            # strip it from system_info to avoid duplication in the emitted payload.
            payload_data = dict(self.modem_data)
            if "system_info" in payload_data and "docsis_status" in payload_data["system_info"]:
                payload_data["system_info"] = {
                    k: v for k, v in payload_data["system_info"].items() if k != "docsis_status"
                }
            modem_data = ModemDataPayload.model_validate(payload_data)

        return SnapshotEventPayload(
            schema_version=SCHEMA_VERSION,
            connection_status=self.connection_status.value,
            docsis_status=self.docsis_status,
            collector_signal=self.collector_signal.value,
            error=self.error,
            stats_last_reset=self.stats_last_reset.isoformat() if self.stats_last_reset else None,
            health_info=health_info,
            modem_data=modem_data,
        )


@dataclass
class OrchestratorDiagnostics:
    """Read-only snapshot of operational diagnostics.

    Returned by diagnostics(). No side effects. Safe to call at any time.

    Attributes:
        poll_duration: Wall-clock time of last get_modem_data() call
            in seconds. None if never polled.
        auth_failure_streak: Current consecutive auth-related failure
            count. 0 when healthy.
        circuit_breaker_open: Whether polling is stopped due to
            persistent auth failures.
        session_is_valid: Current session state from the collector.
        auth_strategy: Auth strategy name from modem config (e.g.,
            "form", "hnap", "none"). Empty string if unknown.
        connectivity_streak: Consecutive connectivity failures. 0 when
            reachable.
        connectivity_backoff_remaining: Polls to skip before next
            connection attempt. 0 when no backoff active.
        stale_session_recovery_streak: Consecutive recovered stale-
            session events. Increments when a LOAD_AUTH same-poll retry
            succeeds and resets on an intervening normal success or
            unrecovered failure.
        session_reuse_disabled: Whether the orchestrator has stopped
            attempting cached-session reuse for the rest of this
            runtime after repeated consecutive stale-session recoveries.
        resource_fetches: Per-resource timing and size from the last
            successful collection.
        last_poll_at: ISO 8601 wall-clock timestamp (UTC) of the last
            ``get_modem_data()`` call. None if never polled.
        last_stub_body: Response body snippets from the last
            LOAD_INTEGRITY event, keyed by resource path. Empty dict
            if no stub-page failure has occurred. Retained until the
            next LOAD_INTEGRITY event — survives successful polls so
            it is present in bug-report diagnostics downloads even after
            the modem recovers. Full body stored; no truncation.
        system_info_fields_missing: Mapped system_info fields no
            configured source produced on the most recent parse.
            Snapshot semantics — recomputed per parse, a healed field
            clears. See PARSING_SPEC § Field Outcomes.
        system_info_fields_failed: Conversion-rejected raw values
            (truncated), keyed by field name. Retained for the runtime
            once recorded so intermittent failures survive into
            diagnostics downloads. Diagnostics-only; never feeds
            signals or policy.
    """

    poll_duration: float | None
    auth_failure_streak: int
    circuit_breaker_open: bool
    session_is_valid: bool
    auth_strategy: str = ""
    connectivity_streak: int = 0
    connectivity_backoff_remaining: int = 0
    stale_session_recovery_streak: int = 0
    session_reuse_disabled: bool = False
    resource_fetches: list[ResourceFetch] = field(default_factory=list)
    last_poll_at: str | None = None
    last_stub_body: dict[str, str] = field(default_factory=dict)
    system_info_fields_missing: list[str] = field(default_factory=list)
    system_info_fields_failed: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for diagnostics output."""
        return {
            "poll_duration": self.poll_duration,
            "auth_failure_streak": self.auth_failure_streak,
            "circuit_breaker_open": self.circuit_breaker_open,
            "session_is_valid": self.session_is_valid,
            "auth_strategy": self.auth_strategy,
            "connectivity_streak": self.connectivity_streak,
            "connectivity_backoff_remaining": self.connectivity_backoff_remaining,
            "stale_session_recovery_streak": self.stale_session_recovery_streak,
            "session_reuse_disabled": self.session_reuse_disabled,
            "resource_fetches": [f.to_dict() for f in self.resource_fetches],
            "last_poll_at": self.last_poll_at,
            "last_stub_body": self.last_stub_body,
            "system_info_fields_missing": self.system_info_fields_missing,
            "system_info_fields_failed": self.system_info_fields_failed,
        }


@dataclass
class RestartResult:
    """Result of dispatching a modem restart command.

    Reflects only whether the command itself was delivered. Does NOT
    report whether the modem actually rebooted or came back — that
    would require observation after the fact, which ``run_restart``
    deliberately doesn't do. Consumers watch the ``ModemSnapshot``
    stream through normal polling to see what actually happened.

    Attributes:
        success: True iff authentication succeeded, the action
            executor reported success, and the session was cleared
            without raising. An executor that ran and was refused is
            a failure.
        elapsed_seconds: Wall time of the ``run_restart`` call.
            Typically a few seconds (auth + POST + session clear).
        error: Structured error token. Empty on success.
            ``"command_failed"`` on any dispatch failure — no other
            tokens are emitted. What specifically refused the command
            is carried by the ``RestartCommandFailed`` log line, not
            here; that line is what a user pastes into an issue.
    """

    success: bool
    elapsed_seconds: float
    error: str = ""
