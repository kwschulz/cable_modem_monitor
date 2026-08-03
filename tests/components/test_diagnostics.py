"""Tests for the diagnostics module.

Tests log retrieval helpers (pure functions) and the async entry point.
The full diagnostics builder is tested with mock runtime_data.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.cable_modem_monitor.diagnostics import (
    _create_log_entry,
    _get_logs_from_file,
    _get_logs_from_system_log_direct,
    _get_logs_from_system_log_handler,
    _get_recent_logs,
    _no_logs_available_entry,
    _parse_legacy_record,
    async_get_config_entry_diagnostics,
)

from .conftest import MOCK_ENTRY_DATA

# -----------------------------------------------------------------------
# Module-level test data
# -----------------------------------------------------------------------

SAMPLE_LOG_FILE_CONTENT = (
    "2025-03-30 12:00:00.123 INFO (MainThread) "
    "[custom_components.cable_modem_monitor.sensor] Poll OK\n"
    "2025-03-30 12:00:01.456 DEBUG (MainThread) "
    "[homeassistant.core] Other line\n"
    "2025-03-30 12:00:02.789 WARNING (MainThread) "
    "[solentlabs.cable_modem_monitor_core.auth] Auth timeout\n"
)

SAMPLE_LOG_BUFFER_ENTRY: list[dict[str, Any]] = [
    {"timestamp": 1.0, "level": "INFO", "logger": "test", "message": "ok"},
]

# -----------------------------------------------------------------------
# Pure helpers
# -----------------------------------------------------------------------


def test_create_log_entry():
    """Log entries are sanitized and structured."""
    entry = _create_log_entry(
        1700000000.0,
        "INFO",
        "custom_components.cable_modem_monitor.sensor",
        "Poll OK",
    )
    assert entry["timestamp"] == 1700000000.0
    assert entry["level"] == "INFO"
    assert entry["logger"] == "sensor"
    assert entry["message"] == "Poll OK"


def test_no_logs_available_entry():
    """Placeholder entry returned when no logs captured."""
    entries = _no_logs_available_entry()
    assert len(entries) == 1
    assert entries[0]["level"] == "INFO"
    assert "No logs captured" in entries[0]["message"]


# -----------------------------------------------------------------------
# System log handler extraction
# -----------------------------------------------------------------------


def test_get_logs_from_system_log_handler_filters():
    """Only cable_modem_monitor records extracted from system_log handler."""
    record_ours = MagicMock()
    record_ours.name = "custom_components.cable_modem_monitor.sensor"
    record_ours.created = 1700000000.0
    record_ours.levelname = "INFO"
    record_ours.getMessage.return_value = "Poll OK"

    record_other = MagicMock()
    record_other.name = "homeassistant.core"
    record_other.created = 1700000001.0
    record_other.levelname = "DEBUG"
    record_other.getMessage.return_value = "Other"

    handler = MagicMock()
    handler.records = [record_ours, record_other]

    logs = _get_logs_from_system_log_handler(handler)
    assert len(logs) == 1
    assert logs[0]["logger"] == "sensor"


def test_get_logs_from_system_log_handler_no_records():
    """Handler without records attribute returns empty."""
    handler = MagicMock(spec=[])
    logs = _get_logs_from_system_log_handler(handler)
    assert logs == []


# -----------------------------------------------------------------------
# System log direct extraction (legacy HA)
# -----------------------------------------------------------------------


def test_get_logs_from_system_log_direct_filters():
    """Only cable_modem_monitor records extracted from system_log.records."""
    record_ours = MagicMock()
    record_ours.name = "custom_components.cable_modem_monitor"
    record_ours.message = "Auth failed"
    record_ours.level = "ERROR"
    record_ours.timestamp = 1700000000.0
    del record_ours.getMessage

    record_other = MagicMock()
    record_other.name = "homeassistant.core"
    record_other.message = "Other"
    record_other.level = "INFO"
    record_other.timestamp = 1700000001.0
    del record_other.getMessage

    system_log = MagicMock()
    system_log.records = [record_ours, record_other]

    logs = _get_logs_from_system_log_direct(system_log)
    assert len(logs) == 1
    assert logs[0]["level"] == "ERROR"


def test_get_logs_from_system_log_direct_no_records():
    """System log without records attribute returns empty."""
    system_log = MagicMock(spec=[])
    logs = _get_logs_from_system_log_direct(system_log)
    assert logs == []


# -----------------------------------------------------------------------
# Legacy record parsing
# -----------------------------------------------------------------------


def test_parse_legacy_record_with_name_and_message():
    """Legacy record with name + message attributes."""
    record = MagicMock()
    record.name = "custom_components.cable_modem_monitor"
    record.message = "Auth failed"
    record.level = "ERROR"
    record.timestamp = 1700000000.0
    # No getMessage — the first branch checks hasattr(name) + hasattr(message)
    del record.getMessage

    entry = _parse_legacy_record(record)
    assert entry is not None
    assert entry["level"] == "ERROR"


def test_parse_legacy_record_non_matching():
    """Record from unrelated logger returns None."""
    record = MagicMock()
    record.name = "homeassistant.core"
    record.message = "Other"
    del record.getMessage

    entry = _parse_legacy_record(record)
    assert entry is None


def test_parse_legacy_record_tuple_format():
    """Legacy tuple record (logger, timestamp, level, message)."""
    record = ("custom_components.cable_modem_monitor", 1700000000.0, "WARNING", "Timeout")
    entry = _parse_legacy_record(record)
    assert entry is not None
    assert entry["level"] == "WARNING"
    assert "Timeout" in entry["message"]


def test_parse_legacy_record_tuple_non_matching():
    """Tuple record from unrelated logger returns None."""
    record = ("homeassistant.core", 1700000000.0, "INFO", "Other")
    assert _parse_legacy_record(record) is None


def test_parse_legacy_record_with_get_message():
    """Legacy record with getMessage() method (logging.LogRecord-like)."""
    record = MagicMock(spec=logging.LogRecord)
    record.name = "cable_modem_monitor"
    record.created = 1700000000.0
    record.levelname = "INFO"
    record.getMessage.return_value = "Startup complete"
    # Ensure first branch doesn't match by removing 'message' attr
    del record.message

    entry = _parse_legacy_record(record)
    assert entry is not None
    assert entry["message"] == "Startup complete"


def test_parse_legacy_record_getmessage_non_matching():
    """Record with getMessage() but wrong logger returns None."""
    record = MagicMock(spec=logging.LogRecord)
    record.name = "homeassistant.core"
    record.created = 1700000000.0
    record.levelname = "INFO"
    record.getMessage.return_value = "Other"
    del record.message

    assert _parse_legacy_record(record) is None


def test_parse_legacy_record_integer_level():
    """Record with integer level is converted via getLevelName."""
    record = MagicMock()
    record.name = "custom_components.cable_modem_monitor"
    record.message = "Auth failed"
    record.level = 40  # logging.ERROR
    record.timestamp = 1700000000.0
    del record.getMessage

    entry = _parse_legacy_record(record)
    assert entry is not None
    assert entry["level"] == "ERROR"


def test_parse_legacy_record_tuple_integer_level():
    """Tuple record with integer level is converted to name."""
    record = ("custom_components.cable_modem_monitor", 1700000000.0, 30, "Warning")
    entry = _parse_legacy_record(record)
    assert entry is not None
    assert entry["level"] == "WARNING"


def test_parse_legacy_record_attribute_error():
    """Record with getMessage but broken .name triggers except handler."""
    record = MagicMock(spec=[])
    record.getMessage = lambda: "test"
    type(record).name = property(lambda self: (_ for _ in ()).throw(AttributeError("no name")))

    assert _parse_legacy_record(record) is None


# -----------------------------------------------------------------------
# Log file reading
# -----------------------------------------------------------------------


def test_get_logs_from_file_missing(tmp_path: Path):
    """Missing log file returns no-logs placeholder."""
    logs = _get_logs_from_file(tmp_path / "nonexistent.log")
    assert len(logs) == 1
    assert "No logs captured" in logs[0]["message"]


def test_get_logs_from_file_with_matching_lines(tmp_path: Path):
    """Log file lines matching our logger are extracted."""
    log_file = tmp_path / "home-assistant.log"
    log_file.write_text(SAMPLE_LOG_FILE_CONTENT)
    logs = _get_logs_from_file(log_file)
    assert len(logs) == 2
    assert logs[0]["level"] == "INFO"
    assert logs[1]["level"] == "WARNING"


# -----------------------------------------------------------------------
# _get_recent_logs (multi-source fallback)
# -----------------------------------------------------------------------


def test_get_recent_logs_from_buffer():
    """Log buffer (method 1) is preferred source."""
    hass = MagicMock()
    with patch(
        "custom_components.cable_modem_monitor.diagnostics.get_log_entries",
        return_value=SAMPLE_LOG_BUFFER_ENTRY,
    ):
        logs = _get_recent_logs(hass, max_records=10)
    assert logs == SAMPLE_LOG_BUFFER_ENTRY


def test_get_recent_logs_falls_through_to_placeholder():
    """When all sources fail, placeholder is returned."""
    hass = MagicMock()
    hass.data = {}
    hass.config.path.return_value = "/nonexistent/home-assistant.log"
    with patch(
        "custom_components.cable_modem_monitor.diagnostics.get_log_entries",
        return_value=[],
    ):
        logs = _get_recent_logs(hass, max_records=10)
    assert len(logs) == 1
    assert "No logs captured" in logs[0]["message"]


def test_get_recent_logs_system_log_handler_fallback():
    """Falls back to system_log handler when log buffer is empty."""
    hass = MagicMock()

    record = MagicMock()
    record.name = "custom_components.cable_modem_monitor.sensor"
    record.created = 1700000000.0
    record.levelname = "WARNING"
    record.getMessage.return_value = "Timeout"

    handler = MagicMock()
    handler.records = [record]

    system_log = MagicMock()
    system_log.handler = handler
    hass.data = {"system_log": system_log}

    with patch(
        "custom_components.cable_modem_monitor.diagnostics.get_log_entries",
        return_value=[],
    ):
        logs = _get_recent_logs(hass, max_records=10)

    assert len(logs) == 1
    assert logs[0]["level"] == "WARNING"


def test_get_recent_logs_system_log_direct_fallback():
    """Falls back to system_log.records when handler returns empty."""
    hass = MagicMock()

    record = MagicMock()
    record.name = "custom_components.cable_modem_monitor"
    record.message = "Auth failed"
    record.level = "ERROR"
    record.timestamp = 1700000000.0
    del record.getMessage

    handler = MagicMock(spec=[])  # no records attribute
    system_log = MagicMock()
    system_log.handler = handler
    system_log.records = [record]
    hass.data = {"system_log": system_log}

    with patch(
        "custom_components.cable_modem_monitor.diagnostics.get_log_entries",
        return_value=[],
    ):
        logs = _get_recent_logs(hass, max_records=10)

    assert len(logs) == 1
    assert logs[0]["level"] == "ERROR"


def test_get_recent_logs_log_file_exception():
    """Exception reading log file falls through to placeholder."""
    hass = MagicMock()
    hass.data = {}
    hass.config.path.side_effect = RuntimeError("config error")

    with patch(
        "custom_components.cable_modem_monitor.diagnostics.get_log_entries",
        return_value=[],
    ):
        logs = _get_recent_logs(hass, max_records=10)

    assert len(logs) == 1
    assert "No logs captured" in logs[0]["message"]


def test_get_recent_logs_system_log_exception():
    """Exception accessing system_log is caught and falls through."""
    hass = MagicMock()
    hass.data = {"system_log": MagicMock(handler=property(lambda s: 1 / 0))}
    type(hass.data["system_log"]).handler = property(lambda self: (_ for _ in ()).throw(RuntimeError("broken")))
    hass.config.path.return_value = "/nonexistent/home-assistant.log"

    with patch(
        "custom_components.cable_modem_monitor.diagnostics.get_log_entries",
        return_value=[],
    ):
        logs = _get_recent_logs(hass, max_records=10)

    assert len(logs) == 1
    assert "No logs captured" in logs[0]["message"]


# -----------------------------------------------------------------------
# async_get_config_entry_diagnostics — entry point
# -----------------------------------------------------------------------


async def test_diagnostics_not_initialized(hass: MagicMock):
    """Returns error dict when runtime_data is not set."""
    entry = MagicMock()
    entry.runtime_data = None
    entry.title = "Test Modem"
    entry.entry_id = "test_id"
    entry.state = "not_loaded"

    result = await async_get_config_entry_diagnostics(hass, entry)
    assert "error" in result
    assert "not fully initialized" in result["error"]


async def test_diagnostics_delegates_to_builder(mock_runtime_data):
    """Entry point delegates to _build_diagnostics_dict in executor."""
    hass = MagicMock()
    entry = MagicMock()
    entry.runtime_data = mock_runtime_data
    entry.data = MOCK_ENTRY_DATA
    entry.title = "Solent Labs TPS-2000"
    entry.entry_id = "test_123"

    # async_add_executor_job should call _build_diagnostics_dict
    async def fake_executor(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = fake_executor

    with (
        patch(
            "custom_components.cable_modem_monitor.diagnostics.get_log_entries",
            return_value=[],
        ),
        patch(
            "custom_components.cable_modem_monitor.diagnostics._get_logs_from_file",
            return_value=[],
        ),
    ):
        result = await async_get_config_entry_diagnostics(hass, entry)

    assert "config_entry" in result
    assert result["config_entry"]["model"] == "TPS-2000"
    assert "core_diagnostics" in result
    assert "modem_data" in result
    assert result["modem_data"]["connection_status"] == "online"
    # modem_data contains only evaluated state — no system_info fields
    assert "total_corrected" not in result["modem_data"]
    assert "software_version" not in result["modem_data"]
    assert "docsis_status" not in result["modem_data"]
    # Full system_info pass-through (single source of truth)
    assert "system_info" in result
    assert result["system_info"]["software_version"] == "4502.9.016"
    assert result["system_info"]["total_corrected"] == 150
    assert result["system_info"]["total_uncorrected"] == 3


# -----------------------------------------------------------------------
# Diagnostics builder — edge cases
# -----------------------------------------------------------------------


async def test_diagnostics_no_snapshot(mock_runtime_data):
    """Builder handles missing snapshot gracefully."""
    hass = MagicMock()
    entry = MagicMock()
    entry.runtime_data = mock_runtime_data
    entry.data = MOCK_ENTRY_DATA
    entry.title = "Solent Labs TPS-2000"
    entry.entry_id = "test_123"

    mock_runtime_data.data_coordinator.data = None

    async def fake_executor(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = fake_executor

    with patch(
        "custom_components.cable_modem_monitor.diagnostics.get_log_entries",
        return_value=SAMPLE_LOG_BUFFER_ENTRY,
    ):
        result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["modem_data"]["note"] == "No snapshot available"
    assert result["downstream_channels"] == []
    assert result["upstream_channels"] == []


async def test_diagnostics_last_exception_truncated(mock_runtime_data):
    """Builder truncates long exception messages."""
    hass = MagicMock()
    entry = MagicMock()
    entry.runtime_data = mock_runtime_data
    entry.data = MOCK_ENTRY_DATA
    entry.title = "Solent Labs TPS-2000"
    entry.entry_id = "test_123"

    mock_runtime_data.data_coordinator.last_exception = RuntimeError("x" * 300)

    async def fake_executor(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = fake_executor

    with patch(
        "custom_components.cable_modem_monitor.diagnostics.get_log_entries",
        return_value=SAMPLE_LOG_BUFFER_ENTRY,
    ):
        result = await async_get_config_entry_diagnostics(hass, entry)

    assert "last_error" in result
    assert result["last_error"]["type"] == "RuntimeError"
    assert "truncated" in result["last_error"]["message"]


# ┌────────────┬────────────┬──────────┬──────────────────────────────────┐
# │ username   │ password   │ expected │ description                      │
# ├────────────┼────────────┼──────────┼──────────────────────────────────┤
# │ "admin"    │ "secret"   │ True     │ both supplied                    │
# │ ""         │ "secret"   │ True     │ password-only modem (#185)       │
# │ "admin"    │ ""         │ True     │ username-only                    │
# │ ""         │ ""         │ False    │ open modem, no auth              │
# │ absent     │ absent     │ False    │ keys missing entirely            │
# └────────────┴────────────┴──────────┴──────────────────────────────────┘
#
# fmt: off
HAS_CREDENTIALS_CASES = [
    # (entry_overrides,                          expected, id)
    ({"username": "admin", "password": "secret"}, True,    "both"),
    ({"username": "",      "password": "secret"}, True,    "password-only"),
    ({"username": "admin", "password": ""},       True,    "username-only"),
    ({"username": "",      "password": ""},       False,   "neither"),
    ({},                                          False,   "keys-absent"),
]
# fmt: on


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [(c[0], c[1]) for c in HAS_CREDENTIALS_CASES],
    ids=[c[2] for c in HAS_CREDENTIALS_CASES],
)
async def test_diagnostics_has_credentials(mock_runtime_data, overrides, expected):
    """has_credentials tracks Core's OR test, so password-only entries report True."""
    hass = MagicMock()
    entry = MagicMock()
    entry.runtime_data = mock_runtime_data
    entry.data = {k: v for k, v in MOCK_ENTRY_DATA.items() if k not in ("username", "password")}
    entry.data.update(overrides)
    entry.title = "Solent Labs TPS-2000"
    entry.entry_id = "test_123"

    async def fake_executor(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = fake_executor

    with patch(
        "custom_components.cable_modem_monitor.diagnostics.get_log_entries",
        return_value=SAMPLE_LOG_BUFFER_ENTRY,
    ):
        result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["config_entry"]["has_credentials"] is expected


async def test_diagnostics_health_coord_data_none(mock_runtime_data):
    """Falls back to snapshot health_info when health coordinator has no data."""
    hass = MagicMock()
    entry = MagicMock()
    entry.runtime_data = mock_runtime_data
    entry.data = MOCK_ENTRY_DATA
    entry.title = "Solent Labs TPS-2000"
    entry.entry_id = "test_123"

    mock_runtime_data.health_coordinator.data = None

    async def fake_executor(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = fake_executor

    with patch(
        "custom_components.cable_modem_monitor.diagnostics.get_log_entries",
        return_value=SAMPLE_LOG_BUFFER_ENTRY,
    ):
        result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["modem_data"]["health_status"] == "responsive"


async def test_diagnostics_log_retrieval_exception(mock_runtime_data):
    """Builder catches log retrieval failure and includes error note."""
    hass = MagicMock()
    entry = MagicMock()
    entry.runtime_data = mock_runtime_data
    entry.data = MOCK_ENTRY_DATA
    entry.title = "Solent Labs TPS-2000"
    entry.entry_id = "test_123"

    async def fake_executor(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = fake_executor

    with patch(
        "custom_components.cable_modem_monitor.diagnostics._get_recent_logs",
        side_effect=RuntimeError("log crash"),
    ):
        result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["recent_logs"]["note"] == "Unable to retrieve recent logs"
    assert "log crash" in result["recent_logs"]["error"]
