"""Signal and status enums for the orchestration layer.

CollectorSignal classifies infrastructure failures from a data
collection attempt. Status enums represent derived state from
collection outcomes and health probes.

See ORCHESTRATION_SPEC.md for the full signal catalog and status
derivation rules.
"""

from __future__ import annotations

from enum import Enum, StrEnum


class CollectorSignal(Enum):
    """Signals from a data collection attempt.

    These represent infrastructure failures -- the collection pipeline
    could not complete. The orchestrator maps them to policy decisions.

    Notably absent: zero channels. Empty data is a valid collection
    result (signal=OK with empty channel lists), not a failure. The
    orchestrator interprets empty data using session state and modem
    context.
    """

    OK = "ok"
    AUTH_FAILED = "auth_failed"
    AUTH_UNAVAILABLE = "auth_unavailable"  # 5xx or a declared busy body; not a credential verdict
    AUTH_LOCKOUT = "auth_lockout"
    CONNECTIVITY = "connectivity"
    LOAD_ERROR = "load_error"
    LOAD_AUTH = "load_auth"
    LOAD_INTEGRITY = "load_integrity"
    PARSE_ERROR = "parse_error"


class ConnectionStatus(Enum):
    """Modem connection status derived from poll outcome."""

    ONLINE = "online"
    AUTH_FAILED = "auth_failed"
    PARSER_ISSUE = "parser_issue"
    UNREACHABLE = "unreachable"
    NO_SIGNAL = "no_signal"


class DocsisStatus(StrEnum):
    """Well-known DOCSIS status values.

    ``enrich_docsis_status`` writes one of these into ``system_info``
    when the parser does not provide ``docsis_status``.  StrEnum members
    compare equal to their string values, so
    ``docsis == DocsisStatus.OPERATIONAL`` works whether *docsis* is an
    enum member or a plain ``"Operational"`` string.

    ``OPERATIONAL`` uses title-case ``"Operational"`` to match the
    canonical ``system_info`` value (see SYSTEM_INFO_SPEC § Canonical
    Values).  Other values are orchestrator-specific labels.
    """

    OPERATIONAL = "Operational"
    PARTIAL_LOCK = "partial_lock"
    NOT_LOCKED = "not_locked"
    UNKNOWN = "unknown"


class HealthStatus(Enum):
    """Modem health derived from probe results."""

    RESPONSIVE = "responsive"
    DEGRADED = "degraded"
    ICMP_BLOCKED = "icmp_blocked"
    UNRESPONSIVE = "unresponsive"
    UNKNOWN = "unknown"

    @property
    def data_path_up(self) -> bool:
        """True when this reading proves TCP — the data path — is usable."""
        # ICMP_BLOCKED counts as proof: since the ICMP contradiction
        # override (UC-59a), every ICMP_BLOCKED reading is confirmed by
        # a live TCP pass, never assumed from stale collection evidence.
        # UNKNOWN is not evidence in either direction and reads as down.
        return self in (HealthStatus.RESPONSIVE, HealthStatus.ICMP_BLOCKED)
