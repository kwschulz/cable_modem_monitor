"""Parse Core logger lines into structured analysis events.

Handles only ``solentlabs.cable_modem_monitor_core.*`` logger output.
HA-specific patterns (startup, fetch_complete, health_recovery, etc.)
are handled by the HA analysis layer.

Pattern source of truth — ORCHESTRATION_SPEC.md logging contracts:

- **ModemDataCollector § Logging Contract**: ``parse_complete``,
  ``auth_fail``, ``auth_success`` — collection outcomes and auth
  lifecycle.
- **Orchestrator § Logging Contract**: ``poll_start``,
  ``status_transition``, ``connectivity_fail``, ``backoff_clear`` —
  polling state machine.
- **HealthMonitor § Logging Contract**: ``health_responsive``,
  ``health_unresponsive``, ``health_degraded`` — probe results.

If a log format changes in the spec, update the corresponding
``CORE_PATTERNS`` entry here and the test fixtures in
``tests/fixtures/analysis/``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime

from .models import (
    BackoffEvent,
    CoreAnalysis,
    HealthEvent,
    PollEvent,
    RecoveryEvent,
)

# ---------------------------------------------------------------------------
# Timestamp format shared across all Core log lines
# ---------------------------------------------------------------------------

_TS = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})"


def parse_ts(ts_str: str) -> datetime:
    """Parse a Core log timestamp string into a datetime.

    Args:
        ts_str: Timestamp in ``YYYY-MM-DD HH:MM:SS.mmm`` format.
    """
    return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S.%f")


# ---------------------------------------------------------------------------
# Core patterns — solentlabs.cable_modem_monitor_core.* loggers
# ---------------------------------------------------------------------------

CORE_PATTERNS: dict[str, re.Pattern[str]] = {
    "poll_start": re.compile(_TS + r" .+Poll \[(.+?)\] — auth: (\w+), .+session: (\w+)"),
    "auth_success": re.compile(_TS + r" .+Auth succeeded \[(.+?)\] — strategy: (\w+), status=(\d+)(?:, landed: (.+))?"),
    "auth_fail": re.compile(_TS + r" .+Connection failed during auth \[(.+?)\]"),
    "parse_complete": re.compile(_TS + r" .+Parse complete \[(.+?)\]: (\d+) DS, (\d+) US"),
    "health_responsive": re.compile(_TS + r" .+Health check \[(.+?)\]: responsive \((.+?)\)"),
    "health_unresponsive": re.compile(_TS + r" .+Health check \[(.+?)\]: unresponsive"),
    "health_degraded": re.compile(_TS + r" .+Health check \[(.+?)\]: degraded"),
    "status_transition": re.compile(_TS + r" .+Status transition \[(.+?)\]: (.+)"),
    "connectivity_fail": re.compile(
        _TS + r" .+Connection failure \[(.+?)\] — unreachable " + r"\(streak: (\d+), backoff: (\d+)"
    ),
    "backoff_clear": re.compile(_TS + r" .+Health recovery detected \[(.+?)\] — clearing connectivity backoff"),
}


# ---------------------------------------------------------------------------
# Line handlers
# ---------------------------------------------------------------------------


def _handle_poll(line: str, analysis: CoreAnalysis) -> bool:
    """Try poll-related patterns.  Return True if matched."""
    m = CORE_PATTERNS["parse_complete"].search(line)
    if m:
        analysis.polls.append(
            PollEvent(
                timestamp=parse_ts(m.group(1)),
                model=m.group(2),
                duration_s=0.0,
                success=True,
                ds_channels=int(m.group(3)),
                us_channels=int(m.group(4)),
            )
        )
        return True

    m = CORE_PATTERNS["auth_fail"].search(line)
    if m:
        analysis.polls.append(
            PollEvent(
                timestamp=parse_ts(m.group(1)),
                model=m.group(2),
                duration_s=0.0,
                success=False,
            )
        )
        return True

    return False


def _handle_health(line: str, analysis: CoreAnalysis) -> bool:
    """Try health-check patterns.  Return True if matched."""
    m = CORE_PATTERNS["health_responsive"].search(line)
    if m:
        detail = m.group(3)
        icmp_ms = 0.0
        tcp_ms = 0.0
        icmp_match = re.search(r"ICMP ([\d.]+)ms", detail)
        if icmp_match:
            icmp_ms = float(icmp_match.group(1))
        tcp_match = re.search(r"TCP ([\d.]+)ms", detail)
        if tcp_match:
            tcp_ms = float(tcp_match.group(1))
        analysis.health_checks.append(
            HealthEvent(
                timestamp=parse_ts(m.group(1)),
                model=m.group(2),
                status="responsive",
                icmp_ms=icmp_ms,
                tcp_ms=tcp_ms,
            )
        )
        return True

    m = CORE_PATTERNS["health_unresponsive"].search(line)
    if m:
        analysis.health_checks.append(
            HealthEvent(
                timestamp=parse_ts(m.group(1)),
                model=m.group(2),
                status="unresponsive",
            )
        )
        return True

    m = CORE_PATTERNS["health_degraded"].search(line)
    if m:
        analysis.health_checks.append(
            HealthEvent(
                timestamp=parse_ts(m.group(1)),
                model=m.group(2),
                status="degraded",
            )
        )
        return True

    return False


def _handle_connectivity(line: str, analysis: CoreAnalysis) -> bool:
    """Try connectivity failure, backoff, and transition patterns."""
    m = CORE_PATTERNS["backoff_clear"].search(line)
    if m:
        analysis.recoveries.append(
            RecoveryEvent(
                timestamp=parse_ts(m.group(1)),
                model=m.group(2),
                transition="backoff_cleared",
            )
        )
        return True

    m = CORE_PATTERNS["connectivity_fail"].search(line)
    if m:
        analysis.backoffs.append(
            BackoffEvent(
                timestamp=parse_ts(m.group(1)),
                model=m.group(2),
                streak=int(m.group(3)),
                backoff=int(m.group(4)),
            )
        )
        return True

    m = CORE_PATTERNS["status_transition"].search(line)
    if m:
        analysis.transitions.append((parse_ts(m.group(1)), m.group(3)))
        return True

    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_core_logs(lines: Iterable[str]) -> CoreAnalysis:
    """Parse Core logger lines into a :class:`CoreAnalysis`.

    Only matches ``solentlabs.cable_modem_monitor_core.*`` log patterns.
    HA-specific lines (startup, fetch_complete, health_recovery, etc.)
    are silently skipped.

    Args:
        lines: Log lines to parse (strings, not file objects).

    Returns:
        Populated CoreAnalysis with all matched events.
    """
    analysis = CoreAnalysis()

    for line in lines:
        if _handle_poll(line, analysis):
            continue
        if _handle_health(line, analysis):
            continue
        _handle_connectivity(line, analysis)

    return analysis
