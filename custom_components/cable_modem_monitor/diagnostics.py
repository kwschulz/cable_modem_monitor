"""Diagnostics support for Cable Modem Monitor.

Implements HA's diagnostics download.  Combines Core's
``OrchestratorDiagnostics`` with HA-side context (PII checklist,
sanitized logs, channel dump, coordinator state).

Security:
    All output is sanitized to remove sensitive information —
    credentials, private IPs, and file paths.  Users are warned to
    manually verify before sharing.

See HA_ADAPTER_SPEC.md § Diagnostics Platform.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from solentlabs.cable_modem_monitor_core.models.field_registry import (
    canonicalize_channel_keys,
)

from .const import (
    CONF_CHANNEL_IDENTITY,
    CONF_CREDENTIAL_ENCODING,
    CONF_LEGACY_SSL,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_PROTOCOL,
    CONF_SUPPORTS_HEAD,
    CONF_SUPPORTS_ICMP,
    CONF_VARIANT,
    VERSION,
)
from .coordinator import CableModemConfigEntry
from .core.log_buffer import (
    get_log_entries,
    sanitize_log_message,
    strip_logger_prefix,
)

_LOGGER = logging.getLogger(__name__)

# Log retrieval limits
LOG_TAIL_BYTES = 100_000  # ~100 KB from log file tail (~500 lines)
MAX_LOG_RECORDS = 150


# ------------------------------------------------------------------
# Sanitization
# ------------------------------------------------------------------


# ------------------------------------------------------------------
# Log retrieval (multi-source fallback)
# ------------------------------------------------------------------


def _create_log_entry(
    timestamp: float | str,
    level: str,
    logger: str,
    message: str,
) -> dict[str, Any]:
    """Create a sanitized log entry dictionary."""
    return {
        "timestamp": timestamp,
        "level": level,
        "logger": strip_logger_prefix(str(logger)),
        "message": sanitize_log_message(str(message)),
    }


def _no_logs_available_entry() -> list[dict[str, Any]]:
    """Return a placeholder when no logs are available."""
    return [
        {
            "timestamp": time.time(),
            "level": "INFO",
            "logger": "diagnostics",
            "message": (
                "No logs captured yet. The integration's log buffer may "
                "not have been initialized (happens on first setup). Try "
                "reloading the integration and capturing diagnostics again."
            ),
        }
    ]


def _get_logs_from_system_log_handler(
    handler: Any,
) -> list[dict[str, Any]]:
    """Extract logs from system_log handler.records (modern HA)."""
    logs: list[dict[str, Any]] = []
    if not hasattr(handler, "records"):
        return logs
    for record in handler.records:
        if "cable_modem_monitor" in record.name:
            logs.append(
                _create_log_entry(
                    timestamp=record.created,
                    level=record.levelname,
                    logger=record.name,
                    message=record.getMessage(),
                )
            )
    return logs


def _get_logs_from_system_log_direct(
    system_log: Any,
) -> list[dict[str, Any]]:
    """Extract logs from system_log.records (older HA versions)."""
    logs: list[dict[str, Any]] = []
    if not hasattr(system_log, "records"):
        return logs
    for record in system_log.records:
        entry = _parse_legacy_record(record)
        if entry:
            logs.append(entry)
    return logs


def _parse_legacy_record(record: Any) -> dict[str, Any] | None:
    """Parse a single record from system_log.records (legacy format)."""
    try:
        if hasattr(record, "name") and hasattr(record, "message"):
            if "cable_modem_monitor" not in str(record.name):
                return None
            level = getattr(record, "level", "ERROR")
            if isinstance(level, int):
                level = logging.getLevelName(level)
            timestamp = getattr(record, "timestamp", None) or getattr(record, "created", None)
            return _create_log_entry(
                timestamp=timestamp if timestamp is not None else time.time(),
                level=str(level),
                logger=str(record.name),
                message=str(record.message),
            )
        if hasattr(record, "getMessage"):
            if "cable_modem_monitor" not in record.name:
                return None
            return _create_log_entry(
                timestamp=record.created,
                level=record.levelname,
                logger=record.name,
                message=record.getMessage(),
            )
        if isinstance(record, tuple) and len(record) >= 4:
            logger_name = record[0]
            if "cable_modem_monitor" not in str(logger_name):
                return None
            timestamp = record[1] if len(record) > 1 else time.time()
            level = record[2] if len(record) > 2 else "ERROR"
            message = record[3] if len(record) > 3 else "Unknown"
            if isinstance(level, int):
                level = logging.getLevelName(level)
            return _create_log_entry(
                timestamp=timestamp,
                level=str(level),
                logger=str(logger_name),
                message=str(message),
            )
    except (IndexError, AttributeError) as e:
        _LOGGER.debug("Error parsing log record: %s", e)
    return None


def _get_logs_from_file(log_file_path: Path) -> list[dict[str, Any]]:
    """Read logs from home-assistant.log file tail."""
    if not log_file_path.exists():
        return _no_logs_available_entry()

    with open(log_file_path, "rb") as f:
        f.seek(0, 2)
        file_size = f.tell()
        read_size = min(LOG_TAIL_BYTES, file_size)
        f.seek(max(0, file_size - read_size))
        tail_data = f.read().decode("utf-8", errors="ignore")
        lines = tail_data.split("\n")

    log_pattern = re.compile(
        r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+"
        r"(\w+)\s+"
        r"\([^)]+\)\s+"
        r"\[(?:custom_components\.cable_modem_monitor"
        r"|solentlabs\.cable_modem_monitor_core)\.?([^\]]*)\]\s+"
        r"(.+)$"
    )

    logs: list[dict[str, Any]] = []
    for line in lines:
        match = log_pattern.match(line)
        if match:
            timestamp_str, level, logger, message = match.groups()
            logs.append(
                _create_log_entry(
                    timestamp=timestamp_str,
                    level=level,
                    logger=logger if logger else "__init__",
                    message=message,
                )
            )
    return logs


def _get_recent_logs(
    hass: HomeAssistant,
    max_records: int = MAX_LOG_RECORDS,
) -> list[dict[str, Any]]:
    """Get recent logs using multi-source fallback.

    1. Internal log buffer (INFO+, most reliable)
    2. system_log integration (WARNING/ERROR only)
    3. home-assistant.log file (if present)
    """
    # Method 1: Our own log buffer
    logs = get_log_entries(hass)
    if logs:
        return logs[-max_records:]

    # Method 2: system_log integration
    try:
        if "system_log" in hass.data:
            system_log = hass.data["system_log"]
            if hasattr(system_log, "handler"):
                logs = _get_logs_from_system_log_handler(system_log.handler)
                if logs:
                    return logs[-max_records:]
            logs = _get_logs_from_system_log_direct(system_log)
            if logs:
                return logs[-max_records:]
    except Exception as err:
        _LOGGER.warning(
            "Could not retrieve logs from system_log: %s",
            err,
            exc_info=True,
        )

    # Method 3: Log file (removed in HA 2025.11+ by default)
    try:
        log_file_path = Path(hass.config.path("home-assistant.log"))
        logs = _get_logs_from_file(log_file_path)
        if logs:
            return logs[-max_records:]
    except Exception as err:
        _LOGGER.warning("Failed to read logs from file: %s", err)

    return _no_logs_available_entry()


# ------------------------------------------------------------------
# Diagnostics builder
# ------------------------------------------------------------------


def _has_credentials(entry_data: Mapping[str, Any]) -> bool:
    """Whether the entry carries any credential at all."""
    # OR, not AND: password-only modems (auth.username_field: "") store
    # no username, so an AND test reports "no credentials" on a working
    # entry. Matches Core's test in orchestrator._log_poll_context.
    return bool(entry_data.get("username") or entry_data.get("password"))


def _build_diagnostics_dict(
    hass: HomeAssistant,
    entry: CableModemConfigEntry,
) -> dict[str, Any]:
    """Build the full diagnostics dictionary.

    Combines Core's OrchestratorDiagnostics with HA-side context.
    Runs in executor (log file I/O).
    """
    runtime = entry.runtime_data
    data_coord = runtime.data_coordinator
    snapshot = data_coord.data
    modem_data = snapshot.modem_data if snapshot else None
    system_info = (modem_data or {}).get("system_info", {})

    # Core diagnostics (no I/O, reads memory state)
    core_diag = runtime.orchestrator.diagnostics()

    diagnostics: dict[str, Any] = {
        "_solentlabs": {
            "tool": "cable_modem_monitor/diagnostics",
            "version": VERSION,
            "captured_at": datetime.now().isoformat(),
            "note": ("Captured with Solent Labs Cable Modem Monitor diagnostics"),
        },
        "_review_before_sharing": {
            "warning": (
                "Automated sanitization is best-effort, not foolproof. "
                "Modem manufacturers store data in unpredictable formats. "
                "Please verify your credentials are not present before "
                "sharing."
            ),
            "checklist": [
                "Search this file for your WiFi network name (SSID)",
                "Search this file for your WiFi password",
                "Search this file for your router admin password",
                "Check that public IPs show as ***PUBLIC_IP***",
            ],
            "how_to_search": ("Use Ctrl+F (Cmd+F on Mac) in your text editor"),
            "if_you_find_credentials": (
                "Replace them with ***REDACTED*** and note it in your " "GitHub issue so we can improve the sanitizer."
            ),
            "documentation": ("https://github.com/solentlabs/cable_modem_monitor" "/blob/main/docs/MODEM_REQUEST.md"),
        },
        "config_entry": {
            "title": entry.title,
            "host": entry.data.get("host"),
            "manufacturer": entry.data.get(CONF_MANUFACTURER),
            "model": entry.data.get(CONF_MODEL),
            "variant": entry.data.get(CONF_VARIANT),
            "protocol": entry.data.get(CONF_PROTOCOL, "http"),
            "legacy_ssl": entry.data.get(CONF_LEGACY_SSL, False),
            "has_credentials": _has_credentials(entry.data),
            "supports_icmp": entry.data.get(CONF_SUPPORTS_ICMP, False),
            "supports_head": entry.data.get(CONF_SUPPORTS_HEAD, False),
            "credential_encoding": entry.data.get(CONF_CREDENTIAL_ENCODING, "plain"),
            "channel_identity": entry.data.get(CONF_CHANNEL_IDENTITY, "id"),
        },
        "core_diagnostics": core_diag.to_dict(),
        "data_coordinator": {
            "last_update_success": data_coord.last_update_success,
            "update_interval": str(data_coord.update_interval),
        },
    }

    # Health coordinator (conditional)
    if runtime.health_coordinator is not None:
        health_coord = runtime.health_coordinator
        diagnostics["health_coordinator"] = {
            "last_update_success": health_coord.last_update_success,
            "update_interval": str(health_coord.update_interval),
        }

    # Modem data summary
    if snapshot:
        # Prefer health coordinator data (updated every 30s) over the
        # data coordinator snapshot (updated every 10m).
        health_info = None
        if runtime.health_coordinator is not None and runtime.health_coordinator.data is not None:
            health_info = runtime.health_coordinator.data
        if health_info is None:
            health_info = snapshot.health_info
        diagnostics["modem_data"] = {
            "connection_status": snapshot.connection_status.value,
            "collector_signal": snapshot.collector_signal.value,
            "health_status": (health_info.health_status.value if health_info else "none"),
            "icmp_latency_ms": (health_info.icmp_latency_ms if health_info else None),
            "tcp_latency_ms": (health_info.tcp_latency_ms if health_info else None),
            "http_latency_ms": (health_info.http_latency_ms if health_info else None),
            "error": snapshot.error or "",
        }
    else:
        diagnostics["modem_data"] = {"note": "No snapshot available"}

    # Full system_info pass-through — single source of truth for modem
    # identity, counters, and status.  See HA_ADAPTER_SPEC § system_info.
    if modem_data:
        diagnostics["system_info"] = system_info

    # Channel dump — parsers emit sparse dicts (only fields the modem
    # produces), already validated against field_registry by
    # validate_modem_data. Keys normalized to canonical order for
    # readability; see FIELD_REGISTRY § Canonical channel key order.
    if modem_data:
        diagnostics["downstream_channels"] = [canonicalize_channel_keys(ch) for ch in modem_data.get("downstream", [])]
        diagnostics["upstream_channels"] = [canonicalize_channel_keys(ch) for ch in modem_data.get("upstream", [])]
    else:
        diagnostics["downstream_channels"] = []
        diagnostics["upstream_channels"] = []

    # Last error from coordinator
    if data_coord.last_exception:
        exc_type = type(data_coord.last_exception).__name__
        exc_msg = sanitize_log_message(str(data_coord.last_exception))
        if len(exc_msg) > 200:
            exc_msg = exc_msg[:200] + "... (truncated)"
        diagnostics["last_error"] = {
            "type": exc_type,
            "message": exc_msg,
        }

    # Recent logs
    try:
        recent_logs = _get_recent_logs(hass, max_records=MAX_LOG_RECORDS)
        diagnostics["recent_logs"] = {
            "count": len(recent_logs),
            "logs": recent_logs,
        }
    except Exception as err:
        _LOGGER.warning("Failed to retrieve recent logs for diagnostics: %s", err)
        diagnostics["recent_logs"] = {
            "note": "Unable to retrieve recent logs",
            "error": str(err),
        }

    return diagnostics


# ------------------------------------------------------------------
# HA entry point
# ------------------------------------------------------------------


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: CableModemConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    if not hasattr(entry, "runtime_data") or entry.runtime_data is None:
        return {
            "error": ("Integration not fully initialized — " "runtime_data not available"),
            "config_entry": {
                "title": entry.title,
                "entry_id": entry.entry_id,
                "state": str(entry.state),
            },
        }

    return await hass.async_add_executor_job(_build_diagnostics_dict, hass, entry)
