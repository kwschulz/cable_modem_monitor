"""Tests for the services module — YAML builders, channel helpers, and service handlers.

Most functions are pure (no I/O), tested via table-driven patterns.
Service handler tests mock runtime_data and HA infrastructure.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.cable_modem_monitor.const import DOMAIN
from custom_components.cable_modem_monitor.coordinator import (
    CableModemRuntimeData,
)
from custom_components.cable_modem_monitor.dev_tools import (
    _add_channel_graphs,
    _build_channel_graph_yaml,
    _build_channel_lookup,
    _build_error_graphs_yaml,
    _build_latency_graph_yaml,
    _build_restart_button_card_yaml,
    _build_status_card_yaml,
    _find_loaded_entry,
    _format_channel_label,
    _format_title_with_type,
    _get_channel_info_id_mode,
    _get_dashboard_titles,
    _get_entity_prefix,
    _group_by_type,
    _migrate_statistics,
    _plan_stat_renames_to_id,
    _plan_stat_renames_to_number,
    _resolve_config_entry_for_device,
    _unique_types,
    create_convert_channel_identity_handler,
    create_generate_dashboard_handler,
    create_orphaned_statistics_handler,
)
from custom_components.cable_modem_monitor.services import (
    _find_loaded_entries,
    _resolve_target_entries,
    async_register_services,
    async_request_modem_refresh,
    create_request_health_check_handler,
    create_request_refresh_handler,
)

# -----------------------------------------------------------------------
# _get_channel_info
# -----------------------------------------------------------------------

# ┌──────────────────────────────┬──────────────┬─────────────────────────────┬────────────────────────────┐
# │ input channels               │ default_type │ expected output             │ description                │
# ├──────────────────────────────┼──────────────┼─────────────────────────────┼────────────────────────────┤
# │ typed + id                   │ "qam"        │ sorted by (type, id)        │ explicit channel_type+id   │
# │ no channel_type              │ "qam"        │ uses default_type           │ falls back to default      │
# │ string channel_id            │ "qam"        │ parsed to int               │ str→int conversion         │
# │ invalid string channel_id    │ "qam"        │ uses index+1 fallback       │ non-numeric fallback       │
# │ empty list                   │ "qam"        │ empty list                  │ no channels                │
# └──────────────────────────────┴──────────────┴─────────────────────────────┴────────────────────────────┘
#
# fmt: off
CHANNEL_INFO_CASES: list[tuple[list[dict[str, Any]], str, list[tuple[str, int]], str]] = [
    (
        [{"channel_type": "ofdm", "channel_id": 2}, {"channel_type": "qam", "channel_id": 1}],
        "qam",
        [("ofdm", 2), ("qam", 1)],
        "sorted_by_type_and_id",
    ),
    (
        [{"channel_id": 5}],
        "qam",
        [("qam", 5)],
        "default_type_used",
    ),
    (
        [{"channel_type": "qam", "channel_id": "3"}],
        "qam",
        [("qam", 3)],
        "string_id_parsed",
    ),
    (
        [{"channel_type": "qam", "channel_id": "abc"}],
        "qam",
        [("qam", 1)],
        "invalid_string_id_fallback",
    ),
    (
        [],
        "qam",
        [],
        "empty_list",
    ),
]
# fmt: on


@pytest.mark.parametrize(
    "channels,default_type,expected,desc",
    CHANNEL_INFO_CASES,
    ids=[c[3] for c in CHANNEL_INFO_CASES],
)
def test_get_channel_info(channels, default_type, expected, desc):
    """_get_channel_info_id_mode extracts and sorts channel tuples."""
    assert _get_channel_info_id_mode(channels, default_type) == expected


# -----------------------------------------------------------------------
# _group_by_type / _unique_types
# -----------------------------------------------------------------------


def test_group_by_type():
    """Channels grouped by type."""
    info = [("qam", 1), ("ofdm", 33), ("qam", 2)]
    grouped = _group_by_type(info)
    assert set(grouped.keys()) == {"qam", "ofdm"}
    assert grouped["qam"] == [("qam", 1), ("qam", 2)]
    assert grouped["ofdm"] == [("ofdm", 33)]


def test_unique_types():
    """Unique channel types extracted."""
    info = [("qam", 1), ("ofdm", 33), ("qam", 2)]
    assert _unique_types(info) == {"qam", "ofdm"}


# -----------------------------------------------------------------------
# _format_channel_label
# -----------------------------------------------------------------------

# fmt: off
LABEL_CASES = [
    ("qam", 1, "full",    "QAM Ch 1",  "full_format"),
    ("qam", 1, "id_only", "Ch 1",      "id_only_format"),
    ("qam", 1, "type_id", "QAM 1",     "type_id_format"),
]
# fmt: on


@pytest.mark.parametrize("ch_type,ch_id,fmt,expected,desc", LABEL_CASES, ids=[c[4] for c in LABEL_CASES])
def test_format_channel_label(ch_type: str, ch_id: int, fmt: str, expected: str, desc: str):
    """Channel labels formatted per style option."""
    assert _format_channel_label(ch_type, ch_id, fmt) == expected


# -----------------------------------------------------------------------
# _format_title_with_type
# -----------------------------------------------------------------------

# fmt: off
TITLE_CASES = [
    ("Downstream Power Levels (dBmV)", "qam",   False, "Downstream QAM Power Levels (dBmV)", "long_ds"),
    ("Upstream Power Levels (dBmV)",   "atdma", False, "Upstream ATDMA Power Levels (dBmV)", "long_us"),
    ("DS Power (dBmV)",                "qam",   True,  "DS QAM Power (dBmV)",                "short_ds"),
    ("US Power (dBmV)",                "atdma", True,  "US ATDMA Power (dBmV)",              "short_us"),
    ("Downstream Power",               None,    False, "Downstream Power",                   "no_type"),
    ("Other Title",                    "qam",   False, "Other Title",                        "no_prefix_match"),
]
# fmt: on


@pytest.mark.parametrize("base,ch_type,short,expected,desc", TITLE_CASES, ids=[c[4] for c in TITLE_CASES])
def test_format_title_with_type(base: str, ch_type: str | None, short: bool, expected: str, desc: str):
    """Title formatting with channel type insertion."""
    assert _format_title_with_type(base, ch_type, short) == expected


# -----------------------------------------------------------------------
# _get_dashboard_titles
# -----------------------------------------------------------------------


def test_dashboard_titles_short():
    """Short titles use abbreviated prefixes."""
    titles = _get_dashboard_titles(short_titles=True)
    assert titles["ds_power"] == "DS Power (dBmV)"
    assert titles["us_power"] == "US Power (dBmV)"


def test_dashboard_titles_long():
    """Long titles use full direction names."""
    titles = _get_dashboard_titles(short_titles=False)
    assert titles["ds_power"] == "Downstream Power Levels (dBmV)"
    assert titles["us_power"] == "Upstream Power Levels (dBmV)"


# -----------------------------------------------------------------------
# YAML builders
# -----------------------------------------------------------------------


def test_build_status_card_yaml_full():
    """Status card includes counts (stable summary values) and excludes rates.

    Rate sensors exist as HA entities but are intentionally omitted from
    the status entities row — point-in-time MEASUREMENT values jitter
    poll-to-poll and don't belong in a stable summary. The rate trend
    is available via the opt-in `include_error_rates` history graph.
    """
    system_info = {
        "software_version": "1.0",
        "system_uptime": "12345",
        "total_corrected": 100,
        "total_uncorrected": 0,
        "rate_corrected": 5.0,
        "rate_uncorrected": 0.0,
    }
    lines = _build_status_card_yaml(
        "cable_modem",
        system_info,
        has_icmp=True,
        has_head=True,
    )
    yaml = "\n".join(lines)
    assert "sensor.cable_modem_status" in yaml
    assert "sensor.cable_modem_ping_latency" in yaml
    assert "sensor.cable_modem_tcp_latency" in yaml
    assert "sensor.cable_modem_http_latency" in yaml
    assert "sensor.cable_modem_software_version" in yaml
    assert "sensor.cable_modem_system_uptime" not in yaml
    assert "sensor.cable_modem_last_boot_time" in yaml
    assert "format: relative" in yaml
    assert "sensor.cable_modem_total_corrected_errors" in yaml
    # Rates are intentionally NOT in the status entities row even when
    # the fields are present in system_info.
    assert "rate_corrected_errors" not in yaml
    assert "rate_uncorrected_errors" not in yaml
    # Restart lives in its own button card, not the status entities row.
    assert "restart_modem" not in yaml


def test_build_status_card_yaml_minimal():
    """Status card omits entities when modem data is sparse and HEAD unsupported."""
    system_info = {}
    lines = _build_status_card_yaml(
        "cable_modem",
        system_info,
        has_icmp=False,
        has_head=False,
    )
    yaml = "\n".join(lines)
    assert "sensor.cable_modem_status" in yaml
    assert "sensor.cable_modem_tcp_latency" in yaml
    assert "ds_channel_count" in yaml
    assert "ping_latency" not in yaml
    assert "http_latency" not in yaml
    assert "software_version" not in yaml
    assert "system_uptime" not in yaml
    assert "last_boot_time" not in yaml
    assert "total_corrected_errors" not in yaml


def test_build_status_card_yaml_passthrough_fields():
    """Unknown system_info fields appear via the dynamic pass-through loop."""
    system_info = {
        "ds_power_status": "Good",
        "ds_snr_status": "Good",
        "us_power_status": "Good",
        "ds_partial_service": "No",
        # Consumed fields must not be duplicated by the loop.
        "software_version": "1.0",
        "total_corrected": 100,
        "total_uncorrected": 0,
        "rate_corrected": 5.0,
    }
    lines = _build_status_card_yaml("cable_modem", system_info, has_icmp=False, has_head=False)
    yaml = "\n".join(lines)
    assert "sensor.cable_modem_ds_power_status" in yaml
    assert "sensor.cable_modem_ds_snr_status" in yaml
    assert "sensor.cable_modem_us_power_status" in yaml
    assert "sensor.cable_modem_ds_partial_service" in yaml
    # Consumed/explicit fields must not be duplicated.
    assert yaml.count("software_version") == 1
    assert "rate_corrected" not in yaml


# Fields that were previously routed through explicit helper functions but now
# reach the card via the generic passthrough loop.
#
# ┌──────────────────────────────┬───────────────────────────────────────────┐
# │ field                        │ expected entity-id substring              │
# └──────────────────────────────┴───────────────────────────────────────────┘
_PASSTHROUGH_FORMERLY_EXPLICIT_CASES = [
    # docsis_status is absent here: it moved back to an explicit row
    # (see test_status_card_default_exclusions).
    ("cpu_speed", {"cpu_speed": "1GHz"}, "sensor.modem_cpu_speed"),
    ("memory_total", {"memory_total": 1024}, "sensor.modem_memory_total"),
    ("memory_free", {"memory_free": 512}, "sensor.modem_memory_free"),
    ("provisioned_speed_down", {"provisioned_speed_down": "1Gbps"}, "sensor.modem_provisioned_speed_down"),
    ("provisioned_speed_up", {"provisioned_speed_up": "100Mbps"}, "sensor.modem_provisioned_speed_up"),
    ("provisioned_burst_down", {"provisioned_burst_down": "x"}, "sensor.modem_provisioned_burst_down"),
    ("provisioned_burst_up", {"provisioned_burst_up": "x"}, "sensor.modem_provisioned_burst_up"),
]


@pytest.mark.parametrize(
    "system_info,expected_entity",
    [(c[1], c[2]) for c in _PASSTHROUGH_FORMERLY_EXPLICIT_CASES],
    ids=[c[0] for c in _PASSTHROUGH_FORMERLY_EXPLICIT_CASES],
)
def test_passthrough_formerly_explicit_fields(system_info: dict[str, Any], expected_entity: str) -> None:
    """Fields that were once explicitly placed now flow through the passthrough loop.

    Exclusion is disabled here — this test asserts loop reach, not the
    default exclusion policy (covered below).
    """
    lines = _build_status_card_yaml("modem", system_info, has_icmp=False, has_head=False, exclude_fields=frozenset())
    assert expected_entity in "\n".join(lines)


def test_status_card_display_only_fields_never_appear():
    """Display-only fields (no sensors exist) stay off the card even unexcluded."""
    system_info = {
        "docsis_status": "Operational",
        "hardware_version": "V1.0",
        "model_name": "TPS-2000",
        "software_version": "1.0",
        "ds_power_status": "Good",
    }
    lines = _build_status_card_yaml("modem", system_info, has_icmp=False, has_head=False)
    yaml = "\n".join(lines)
    assert "hardware_version" not in yaml
    assert "model_name" not in yaml
    assert "sensor.modem_ds_power_status" in yaml
    # DOCSIS status is an attribute row off the Status sensor (the
    # standalone sensor was retired, #178) — no standalone entity.
    assert "sensor.modem_docsis_status" not in yaml
    assert lines.count("        attribute: docsis_status") == 1
    assert "name: DOCSIS Status" in yaml
    # Positioned before Software Version, at its historical spot.
    docsis_idx = lines.index("        attribute: docsis_status")
    sw_idx = lines.index("      - entity: sensor.modem_software_version")
    assert docsis_idx < sw_idx


def test_status_card_exclude_override():
    """A caller-supplied exclusion list drops the named pass-through rows."""
    system_info = {
        "docsis_status": "Operational",
        "ds_power_status": "Good",
        "dhcp_status": "Ready",
    }
    yaml = "\n".join(
        _build_status_card_yaml(
            "modem",
            system_info,
            has_icmp=False,
            has_head=False,
            exclude_fields=frozenset({"ds_power_status"}),
        )
    )
    assert "attribute: docsis_status" in yaml
    assert "sensor.modem_dhcp_status" in yaml
    assert "ds_power_status" not in yaml


def test_status_card_exclude_drops_docsis_row():
    """Excluding docsis_status removes the explicit DOCSIS Status row."""
    system_info = {"docsis_status": "Operational"}
    yaml = "\n".join(
        _build_status_card_yaml(
            "modem",
            system_info,
            has_icmp=False,
            has_head=False,
            exclude_fields=frozenset({"docsis_status"}),
        )
    )
    assert "docsis_status" not in yaml


def test_build_restart_button_card_yaml():
    """Restart button is a dedicated `button` card so confirmation always fires."""
    lines = _build_restart_button_card_yaml("cable_modem")
    yaml = "\n".join(lines)
    assert "type: button" in yaml
    assert "entity: button.cable_modem_restart_modem" in yaml
    # Entities-card row tap_actions get bypassed by the button widget,
    # so we use call-service + confirmation on a standalone button card.
    assert "action: call-service" in yaml
    assert "service: button.press" in yaml
    assert "entity_id: button.cable_modem_restart_modem" in yaml
    assert "confirmation:" in yaml
    assert "This will restart your modem" in yaml


def test_build_channel_graph_yaml():
    """Channel graph card has correct entity IDs and labels."""
    info = [("qam", 1), ("qam", 2)]
    lines = _build_channel_graph_yaml(
        "DS Power",
        24,
        info,
        "sensor.cm_ds_{ch_type}_ch_{ch_id}_power",
        "full",
    )
    yaml = "\n".join(lines)
    assert "title: DS Power" in yaml
    assert "hours_to_show: 24" in yaml
    assert "sensor.cm_ds_qam_ch_1_power" in yaml
    assert "sensor.cm_ds_qam_ch_2_power" in yaml


def test_build_error_graphs_yaml_counts_only_by_default():
    """Default: counts only — rate graphs are opt-in via include_rates."""
    titles = _get_dashboard_titles(False)
    lines = _build_error_graphs_yaml("cm", titles)
    yaml = "\n".join(lines)
    assert "sensor.cm_total_corrected_errors" in yaml
    assert "sensor.cm_total_uncorrected_errors" in yaml
    assert "rate_corrected_errors" not in yaml
    assert "rate_uncorrected_errors" not in yaml


def test_build_error_graphs_yaml_with_rates_opt_in():
    """include_rates=True appends rate history graphs after counts."""
    titles = _get_dashboard_titles(False)
    lines = _build_error_graphs_yaml("cm", titles, include_rates=True)
    yaml = "\n".join(lines)
    assert "sensor.cm_total_corrected_errors" in yaml
    assert "sensor.cm_total_uncorrected_errors" in yaml
    assert "sensor.cm_rate_corrected_errors" in yaml
    assert "sensor.cm_rate_uncorrected_errors" in yaml


def test_build_latency_graph_yaml_with_icmp_and_head():
    """Latency graph includes Ping, TCP, and HTTP HEAD when all available."""
    lines = _build_latency_graph_yaml("cm", has_icmp=True, has_head=True)
    yaml = "\n".join(lines)
    assert "sensor.cm_ping_latency" in yaml
    assert "sensor.cm_tcp_latency" in yaml
    assert "sensor.cm_http_latency" in yaml


def test_build_latency_graph_yaml_no_icmp_no_head():
    """Latency graph omits Ping and HTTP when unavailable; TCP always present."""
    lines = _build_latency_graph_yaml("cm", has_icmp=False, has_head=False)
    yaml = "\n".join(lines)
    assert "ping_latency" not in yaml
    assert "sensor.cm_tcp_latency" in yaml
    assert "http_latency" not in yaml


def test_build_latency_graph_yaml_icmp_only():
    """Latency graph: ICMP supported but HEAD not — no HTTP line."""
    lines = _build_latency_graph_yaml("cm", has_icmp=True, has_head=False)
    yaml = "\n".join(lines)
    assert "sensor.cm_ping_latency" in yaml
    assert "sensor.cm_tcp_latency" in yaml
    assert "http_latency" not in yaml


# -----------------------------------------------------------------------
# _add_channel_graphs (integration of builders + grouping)
# -----------------------------------------------------------------------


def test_add_channel_graphs_by_direction_single_type():
    """By-direction grouping with single type inserts type in title."""
    parts: list[str] = []
    info = [("qam", 1), ("qam", 2)]
    _add_channel_graphs(
        parts,
        info,
        "Downstream Power Levels (dBmV)",
        "sensor.cm_ds_{ch_type}_ch_{ch_id}_power",
        24,
        "auto",
        "by_direction",
        False,
    )
    yaml = "\n".join(parts)
    assert "QAM" in yaml  # type inserted in title
    assert "sensor.cm_ds_qam_ch_1_power" in yaml


def test_add_channel_graphs_by_type():
    """By-type grouping creates separate cards per type."""
    parts: list[str] = []
    info = [("qam", 1), ("ofdm", 33)]
    _add_channel_graphs(
        parts,
        info,
        "DS Power",
        "sensor.cm_ds_{ch_type}_ch_{ch_id}_power",
        24,
        "auto",
        "by_type",
        False,
    )
    yaml = "\n".join(parts)
    # Two separate title cards
    assert yaml.count("title:") == 2


def test_add_channel_graphs_empty():
    """Empty channel list produces no output."""
    parts: list[str] = []
    _add_channel_graphs(
        parts,
        [],
        "DS Power",
        "sensor.cm_ds_{ch_type}_ch_{ch_id}_power",
        24,
        "auto",
        "by_direction",
        False,
    )
    assert parts == []


# -----------------------------------------------------------------------
# Helpers for service handler tests
# -----------------------------------------------------------------------


def _make_mock_entry(
    runtime_data: CableModemRuntimeData,
    entry_id: str = "test_entry",
) -> MagicMock:
    """Create a mock config entry with runtime_data and domain."""
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.domain = DOMAIN
    entry.runtime_data = runtime_data
    return entry


def _make_mock_call(
    data: dict[str, Any] | None = None,
    device_id: str | list[str] | None = None,
) -> MagicMock:
    """Create a mock ServiceCall with optional data fields."""
    call = MagicMock()
    call.data = dict(data) if data else {}
    if device_id is not None:
        call.data["device_id"] = device_id
    return call


# -----------------------------------------------------------------------
# async_request_modem_refresh (shared helper)
# -----------------------------------------------------------------------


# ┌──────────────────────┬──────────────────────────────────────────────────────┬────────────────┐
# │ health_coordinator   │ expected calls                                       │ description    │
# ├──────────────────────┼──────────────────────────────────────────────────────┼────────────────┤
# │ mock coordinator     │ reset_connectivity, health.refresh, data.refresh     │ with health    │
# │ None                 │ reset_connectivity, data.refresh                     │ without health │
# └──────────────────────┴──────────────────────────────────────────────────────┴────────────────┘
#
# fmt: off
REFRESH_HELPER_CASES = [
    (True,  "with_health"),
    (False, "without_health"),
]
# fmt: on


@pytest.mark.parametrize(
    "has_health,desc",
    REFRESH_HELPER_CASES,
    ids=[c[1] for c in REFRESH_HELPER_CASES],
)
async def test_async_request_modem_refresh(
    has_health: bool,
    desc: str,
    mock_orchestrator: MagicMock,
    mock_data_coordinator: MagicMock,
    mock_health_coordinator: MagicMock,
    mock_runtime_data: CableModemRuntimeData,
) -> None:
    """Shared helper resets backoff, optionally refreshes health, then data."""
    mock_data_coordinator.async_request_refresh = AsyncMock()

    if has_health:
        mock_health_coordinator.async_request_refresh = AsyncMock()
    else:
        mock_runtime_data.health_coordinator = None

    await async_request_modem_refresh(mock_runtime_data)

    mock_orchestrator.reset_connectivity.assert_called_once()
    mock_data_coordinator.async_request_refresh.assert_awaited_once()

    if has_health:
        mock_health_coordinator.async_request_refresh.assert_awaited_once()


async def test_refresh_helper_calls_health_before_data(
    mock_runtime_data: CableModemRuntimeData,
) -> None:
    """Health coordinator refreshed before data coordinator."""
    call_order: list[str] = []

    data_coord: MagicMock = mock_runtime_data.data_coordinator  # type: ignore[assignment]
    health_coord: MagicMock = mock_runtime_data.health_coordinator  # type: ignore[assignment]

    async def record_health() -> None:
        call_order.append("health")

    async def record_data() -> None:
        call_order.append("data")

    health_coord.async_request_refresh = record_health
    data_coord.async_request_refresh = record_data

    await async_request_modem_refresh(mock_runtime_data)

    assert call_order == ["health", "data"]


# -----------------------------------------------------------------------
# _find_loaded_entries
# -----------------------------------------------------------------------


def test_find_loaded_entries() -> None:
    """Returns only entries with non-None runtime_data."""
    loaded = MagicMock()
    loaded.runtime_data = MagicMock()

    unloaded = MagicMock(spec=[])  # no runtime_data attribute

    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [loaded, unloaded]

    result = _find_loaded_entries(hass)

    hass.config_entries.async_entries.assert_called_once_with(DOMAIN)
    assert result == [loaded]


# -----------------------------------------------------------------------
# _resolve_config_entry_for_device
# -----------------------------------------------------------------------

# ┌─────────────────┬──────────────────────┬────────────────────┬──────────────────┐
# │ device exists?  │ config entry state   │ expected result    │ description      │
# ├─────────────────┼──────────────────────┼────────────────────┼──────────────────┤
# │ yes             │ loaded, our domain   │ the entry          │ valid_device     │
# │ no              │ —                    │ None               │ unknown_device   │
# │ yes             │ loaded, other domain │ None               │ wrong_domain     │
# │ yes             │ no runtime_data      │ None               │ not_loaded       │
# └─────────────────┴──────────────────────┴────────────────────┴──────────────────┘
#
# fmt: off
RESOLVE_DEVICE_CASES = [
    ("dev_1", True,  DOMAIN,  True,  True,  "valid_device"),
    ("dev_2", False, None,    None,  False, "unknown_device"),
    ("dev_3", True,  "other", True,  False, "wrong_domain"),
    ("dev_4", True,  DOMAIN,  False, False, "not_loaded"),
]
# fmt: on


@pytest.mark.parametrize(
    "device_id,device_exists,domain,has_runtime,expect_found,desc",
    RESOLVE_DEVICE_CASES,
    ids=[c[5] for c in RESOLVE_DEVICE_CASES],
)
@patch("custom_components.cable_modem_monitor.dev_tools.dr")
def test_resolve_config_entry_for_device(
    mock_dr: MagicMock,
    device_id: str,
    device_exists: bool,
    domain: str | None,
    has_runtime: bool | None,
    expect_found: bool,
    desc: str,
) -> None:
    """Device ID resolves to config entry only when valid."""
    hass = MagicMock()
    mock_registry = MagicMock()
    mock_dr.async_get.return_value = mock_registry

    if device_exists:
        device = MagicMock()
        device.config_entries = {"entry_1"}
        mock_registry.async_get.return_value = device

        entry = MagicMock()
        entry.domain = domain
        if has_runtime:
            entry.runtime_data = MagicMock()
        else:
            entry.runtime_data = None
        hass.config_entries.async_get_entry.return_value = entry
    else:
        mock_registry.async_get.return_value = None

    result = _resolve_config_entry_for_device(hass, device_id)

    if expect_found:
        assert result is not None
    else:
        assert result is None


# -----------------------------------------------------------------------
# _resolve_target_entries
# -----------------------------------------------------------------------


@patch("custom_components.cable_modem_monitor.dev_tools.dr")
def test_resolve_target_with_device_id(mock_dr: MagicMock) -> None:
    """Resolves device_id to matching config entry."""
    hass = MagicMock()
    entry = MagicMock()
    entry.domain = DOMAIN
    entry.runtime_data = MagicMock()

    mock_registry = MagicMock()
    mock_dr.async_get.return_value = mock_registry
    device = MagicMock()
    device.config_entries = {"entry_1"}
    mock_registry.async_get.return_value = device
    hass.config_entries.async_get_entry.return_value = entry

    call = _make_mock_call(device_id="dev_abc")
    result = _resolve_target_entries(hass, call)

    assert result == [entry]


def test_resolve_target_no_device_id_returns_all_loaded() -> None:
    """Falls back to all loaded entries when no device_id in call."""
    loaded = MagicMock()
    loaded.runtime_data = MagicMock()

    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [loaded]

    call = _make_mock_call(device_id=None)
    result = _resolve_target_entries(hass, call)

    assert result == [loaded]


@patch("custom_components.cable_modem_monitor.dev_tools.dr")
def test_resolve_target_unknown_device_returns_empty(mock_dr: MagicMock) -> None:
    """Unknown device_id returns empty list, not fallback."""
    hass = MagicMock()
    mock_registry = MagicMock()
    mock_dr.async_get.return_value = mock_registry
    mock_registry.async_get.return_value = None

    call = _make_mock_call(device_id="nonexistent")
    result = _resolve_target_entries(hass, call)

    assert result == []


@patch("custom_components.cable_modem_monitor.dev_tools.dr")
def test_resolve_target_string_device_id(mock_dr: MagicMock) -> None:
    """Single string device_id is handled (not just list)."""
    hass = MagicMock()
    entry = MagicMock()
    entry.domain = DOMAIN
    entry.runtime_data = MagicMock()

    mock_registry = MagicMock()
    mock_dr.async_get.return_value = mock_registry
    device = MagicMock()
    device.config_entries = {"entry_1"}
    mock_registry.async_get.return_value = device
    hass.config_entries.async_get_entry.return_value = entry

    call = _make_mock_call(device_id="single_id")
    result = _resolve_target_entries(hass, call)

    assert len(result) == 1


# -----------------------------------------------------------------------
# request_refresh service handler
# -----------------------------------------------------------------------


async def test_request_refresh_triggers_refresh(
    mock_orchestrator: MagicMock,
    mock_data_coordinator: MagicMock,
    mock_health_coordinator: MagicMock,
    mock_runtime_data: CableModemRuntimeData,
) -> None:
    """Handler calls async_request_modem_refresh for resolved entry."""
    mock_data_coordinator.async_request_refresh = AsyncMock()
    mock_health_coordinator.async_request_refresh = AsyncMock()

    entry = _make_mock_entry(mock_runtime_data)
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [entry]

    handler = create_request_refresh_handler(hass)
    call = _make_mock_call()

    await handler(call)

    mock_orchestrator.reset_connectivity.assert_called_once()
    mock_data_coordinator.async_request_refresh.assert_awaited_once()


async def test_request_refresh_no_entries_raises() -> None:
    """No target entry is a user-fixable condition, so the action raises."""
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []

    handler = create_request_refresh_handler(hass)
    call = _make_mock_call()

    with pytest.raises(ServiceValidationError, match="No loaded cable modem"):
        await handler(call)


# -----------------------------------------------------------------------
# request_health_check service handler
# -----------------------------------------------------------------------


async def test_request_health_check_triggers_probe(
    mock_health_coordinator: MagicMock,
    mock_runtime_data: CableModemRuntimeData,
) -> None:
    """Handler refreshes health coordinator when enabled."""
    mock_health_coordinator.async_request_refresh = AsyncMock()

    entry = _make_mock_entry(mock_runtime_data)
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [entry]

    handler = create_request_health_check_handler(hass)
    call = _make_mock_call()

    await handler(call)

    mock_health_coordinator.async_request_refresh.assert_awaited_once()


async def test_request_health_check_no_health(
    mock_runtime_data: CableModemRuntimeData,
) -> None:
    """Handler skips when health monitoring is disabled."""
    mock_runtime_data.health_coordinator = None

    entry = _make_mock_entry(mock_runtime_data)
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [entry]

    handler = create_request_health_check_handler(hass)
    call = _make_mock_call()

    # Should not raise — logs warning and returns
    await handler(call)


async def test_request_health_check_no_entries_raises() -> None:
    """No target entry is a user-fixable condition, so the action raises."""
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []

    handler = create_request_health_check_handler(hass)
    call = _make_mock_call()

    with pytest.raises(ServiceValidationError, match="No loaded cable modem"):
        await handler(call)


# -----------------------------------------------------------------------
# _get_entity_prefix
# -----------------------------------------------------------------------


def test_get_entity_prefix() -> None:
    """Entity prefix derived from device name via slugification."""
    entry = MagicMock()
    entry.data = {"entity_prefix": "none", "host": "192.168.100.1"}
    entry.runtime_data.modem_identity.model = "TPS-2000"

    assert _get_entity_prefix(entry) == "cable_modem"


# -----------------------------------------------------------------------
# _find_loaded_entry (singular)
# -----------------------------------------------------------------------


def test_find_loaded_entry_found() -> None:
    """Returns first loaded entry."""
    entry = MagicMock()
    entry.runtime_data = MagicMock()

    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [entry]

    assert _find_loaded_entry(hass) is entry


def test_find_loaded_entry_none() -> None:
    """Returns None when no loaded entries exist."""
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []

    assert _find_loaded_entry(hass) is None


# -----------------------------------------------------------------------
# _add_channel_graphs — multi-type by_direction
# -----------------------------------------------------------------------


def test_add_channel_graphs_multi_type_by_direction() -> None:
    """Multi-type by-direction uses base title and full labels."""
    parts: list[str] = []
    info = [("qam", 1), ("ofdm", 33)]
    _add_channel_graphs(
        parts,
        info,
        "Downstream Power Levels (dBmV)",
        "sensor.cm_ds_{ch_type}_ch_{ch_id}_power",
        24,
        "auto",
        "by_direction",
        False,
    )
    yaml = "\n".join(parts)
    # Multi-type uses base title (no type prefix)
    assert "Downstream Power Levels (dBmV)" in yaml
    # Auto label becomes "full" for multi-type
    assert "QAM Ch 1" in yaml
    assert "OFDM Ch 33" in yaml


# -----------------------------------------------------------------------
# generate_dashboard service handler
# -----------------------------------------------------------------------


def test_generate_dashboard_handler(
    mock_runtime_data: CableModemRuntimeData,
) -> None:
    """Default dashboard: status card with counts, count graphs, no rate graphs.

    Rate sensors exist as HA entities (capability-gated in sensor.py) but
    do not auto-generate into the dashboard. Opt-in via include_error_rates.
    """
    entry = _make_mock_entry(mock_runtime_data)
    entry.data = {
        "entity_prefix": "none",
        "host": "192.168.100.1",
        "supports_icmp": True,
        "supports_head": True,
    }

    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [entry]

    handler = create_generate_dashboard_handler(hass)
    call = MagicMock()
    call.data = {
        "include_downstream_power": True,
        "include_downstream_snr": False,
        "include_downstream_frequency": False,
        "include_upstream_power": True,
        "include_upstream_frequency": False,
        "include_errors": True,
        "include_latency": True,
        "include_status_card": True,
        "graph_hours": 24,
        "short_titles": False,
        "channel_label": "auto",
        "channel_grouping": "by_direction",
    }

    result = handler(call)

    yaml = result["yaml"]
    assert "Cable Modem Dashboard" in yaml
    assert "sensor.cable_modem_status" in yaml
    assert "sensor.cable_modem_ds_qam_ch_1_power" in yaml
    assert "sensor.cable_modem_ds_ofdm_ch_2_power" in yaml
    assert "sensor.cable_modem_us_atdma_ch_1_power" in yaml
    assert "sensor.cable_modem_total_corrected_errors" in yaml
    # Rates are off by default — not in status card, not in graphs.
    assert "rate_corrected_errors" not in yaml
    assert "rate_uncorrected_errors" not in yaml
    assert "sensor.cable_modem_tcp_latency" in yaml
    assert "sensor.cable_modem_http_latency" in yaml
    assert "entity: button.cable_modem_restart_modem" in yaml


def test_generate_dashboard_handler_with_error_rates_opt_in(
    mock_runtime_data: CableModemRuntimeData,
) -> None:
    """include_error_rates=True adds rate history graphs (still no entities row)."""
    entry = _make_mock_entry(mock_runtime_data)
    entry.data = {
        "entity_prefix": "none",
        "host": "192.168.100.1",
        "supports_icmp": True,
        "supports_head": True,
    }

    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [entry]

    handler = create_generate_dashboard_handler(hass)
    call = MagicMock()
    call.data = {
        "include_errors": True,
        "include_error_rates": True,
        "include_status_card": True,
    }

    yaml = handler(call)["yaml"]
    # Rate graphs appear (opt-in honored).
    assert "sensor.cable_modem_rate_corrected_errors" in yaml
    assert "sensor.cable_modem_rate_uncorrected_errors" in yaml
    # But the status entities row still excludes rate entities — the
    # entities-row exclusion is unconditional regardless of opt-in.
    status_card_lines: list[str] = []
    for line in yaml.split("\n"):
        if line.startswith("  - type: history-graph"):
            break
        status_card_lines.append(line)
    status_card = "\n".join(status_card_lines)
    assert "rate_corrected_errors" not in status_card
    assert "rate_uncorrected_errors" not in status_card


def test_generate_dashboard_handler_no_restart_support(
    mock_runtime_data: CableModemRuntimeData,
) -> None:
    """Dashboard omits the restart button when the orchestrator reports no restart support."""
    mock_runtime_data.orchestrator.supports_restart = False  # type: ignore[misc]

    entry = _make_mock_entry(mock_runtime_data)
    entry.data = {"entity_prefix": "none", "host": "192.168.100.1"}

    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [entry]

    handler = create_generate_dashboard_handler(hass)
    call = MagicMock()
    call.data = {}

    yaml = handler(call)["yaml"]
    assert "button.cable_modem_restart_modem" not in yaml
    assert "This will restart your modem" not in yaml


def test_generate_dashboard_number_mode_without_lock_status(
    mock_runtime_data: CableModemRuntimeData,
) -> None:
    """Channels that report no lock_status still reach the dashboard in NUMBER mode.

    Some modems never emit the field. Core leaves those channels alone and
    every downstream consumer treats them as locked
    (CHANNEL_IDENTIFICATION_SPEC.md section 6). Guards the whole service
    path, not just the helper: the reported symptom was a dashboard whose
    card list came back with no channel graphs at all.
    """
    from custom_components.cable_modem_monitor.const import (
        CONF_CHANNEL_IDENTITY,
        ChannelIdentity,
    )

    snapshot = mock_runtime_data.data_coordinator.data
    modem_data = snapshot.modem_data
    assert modem_data is not None
    without_lock_status = {
        **modem_data,
        "downstream": [{k: v for k, v in ch.items() if k != "lock_status"} for ch in modem_data["downstream"]],
        "upstream": [{k: v for k, v in ch.items() if k != "lock_status"} for ch in modem_data["upstream"]],
    }
    mock_runtime_data.data_coordinator.data = replace(snapshot, modem_data=without_lock_status)

    entry = _make_mock_entry(mock_runtime_data)
    entry.data = {
        "entity_prefix": "none",
        "host": "192.168.100.1",
        CONF_CHANNEL_IDENTITY: ChannelIdentity.NUMBER,
    }

    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [entry]

    handler = create_generate_dashboard_handler(hass)
    call = MagicMock()
    call.data = {
        "include_downstream_power": True,
        "include_upstream_power": True,
        "include_status_card": True,
        "channel_grouping": "by_direction",
    }

    yaml = handler(call)["yaml"]

    # Position-mode entity ids carry no channel type.
    assert "sensor.cable_modem_ds_ch_1_power" in yaml
    assert "sensor.cable_modem_ds_ch_2_power" in yaml
    assert "sensor.cable_modem_us_ch_1_power" in yaml


def test_generate_dashboard_no_entry_raises() -> None:
    """Raises rather than smuggling an error string back as YAML."""
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []

    handler = create_generate_dashboard_handler(hass)
    call = MagicMock()
    call.data = {}

    with pytest.raises(ServiceValidationError, match="No cable modem configured"):
        handler(call)


def test_generate_dashboard_no_snapshot_raises(
    mock_runtime_data: CableModemRuntimeData,
) -> None:
    """Raises rather than smuggling an error string back as YAML."""
    mock_runtime_data.data_coordinator.data = None  # type: ignore[assignment]

    entry = _make_mock_entry(mock_runtime_data)
    entry.data = {"entity_prefix": "none", "host": "192.168.100.1"}

    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [entry]

    handler = create_generate_dashboard_handler(hass)
    call = MagicMock()
    call.data = {}

    with pytest.raises(ServiceValidationError, match="modem must be online"):
        handler(call)


# -----------------------------------------------------------------------
# Service registration / unregistration
# -----------------------------------------------------------------------


def test_register_services() -> None:
    """Registers all three services."""
    hass = MagicMock()
    async_register_services(hass)

    assert hass.services.async_register.call_count == 5
    registered = {call.args[1] for call in hass.services.async_register.call_args_list}
    assert registered == {
        "generate_dashboard",
        "request_refresh",
        "request_health_check",
        "convert_channel_identity",
        "orphaned_statistics",
    }


# -----------------------------------------------------------------------
# Stat rename planning (_plan_stat_renames_to_number / _plan_stat_renames_to_id)
# -----------------------------------------------------------------------

CHANNELS_DIR = Path(__file__).parent.parent / "fixtures" / "channels"


def _load_fixture(name: str) -> Any:
    return json.loads((CHANNELS_DIR / name).read_text())


def _channels() -> dict[str, list[dict[str, Any]]]:
    """Build channel lookup from fixtures (ds_locked + us_locked)."""
    return _build_channel_lookup(
        {
            "downstream": _load_fixture("ds_locked.json"),
            "upstream": _load_fixture("us_locked.json"),
        }
    )


_TO_NUMBER_CASES = _load_fixture("stat_renames_to_number.json")
_TO_ID_CASES = _load_fixture("stat_renames_to_id.json")


class TestPlanStatRenamesToNumber:
    """_plan_stat_renames_to_number: id-mode stats → number-mode."""

    CHANNELS = _channels()

    @pytest.mark.parametrize(
        "case",
        _TO_NUMBER_CASES,
        ids=[c["id"] for c in _TO_NUMBER_CASES],
    )
    def test_plan(self, case: dict[str, Any]) -> None:
        result = _plan_stat_renames_to_number(
            case["stat_ids"],
            self.CHANNELS,
            case["prefix"],
        )
        expected = [tuple(pair) for pair in case["expected"]]
        assert result == expected


class TestPlanStatRenamesToId:
    """_plan_stat_renames_to_id: number-mode stats → id-mode."""

    CHANNELS = _channels()

    @pytest.mark.parametrize(
        "case",
        _TO_ID_CASES,
        ids=[c["id"] for c in _TO_ID_CASES],
    )
    def test_plan(self, case: dict[str, Any]) -> None:
        result = _plan_stat_renames_to_id(
            case["stat_ids"],
            self.CHANNELS,
            case["prefix"],
        )
        expected = [tuple(pair) for pair in case["expected"]]
        assert result == expected


# -----------------------------------------------------------------------
# _migrate_statistics — recorder I/O helper
# -----------------------------------------------------------------------


def test_migrate_statistics_clears_then_renames() -> None:
    """Each rename pair queues a clear on the new id, then an update from old to new."""
    hass = MagicMock()
    recorder = MagicMock()

    renames = [
        ("sensor.cable_modem_ds_qam_ch_1_power", "sensor.cable_modem_ds_ch_1_power"),
        ("sensor.cable_modem_ds_qam_ch_2_power", "sensor.cable_modem_ds_ch_2_power"),
    ]

    with patch("homeassistant.helpers.recorder.get_instance", return_value=recorder) as mock_get:
        result = _migrate_statistics(hass, renames)

    assert result == 2
    mock_get.assert_called_once_with(hass)

    # Two clear calls (one per new_id) and two update calls (one per pair).
    assert recorder.async_clear_statistics.call_count == 2
    assert recorder.async_update_statistics_metadata.call_count == 2

    recorder.async_clear_statistics.assert_any_call(["sensor.cable_modem_ds_ch_1_power"])
    recorder.async_clear_statistics.assert_any_call(["sensor.cable_modem_ds_ch_2_power"])
    recorder.async_update_statistics_metadata.assert_any_call(
        "sensor.cable_modem_ds_qam_ch_1_power",
        new_statistic_id="sensor.cable_modem_ds_ch_1_power",
    )


def test_migrate_statistics_empty_list_returns_zero() -> None:
    """Empty rename list still resolves the recorder but issues no calls."""
    hass = MagicMock()
    recorder = MagicMock()

    with patch("homeassistant.helpers.recorder.get_instance", return_value=recorder):
        result = _migrate_statistics(hass, [])

    assert result == 0
    recorder.async_clear_statistics.assert_not_called()
    recorder.async_update_statistics_metadata.assert_not_called()


# -----------------------------------------------------------------------
# create_convert_channel_identity_handler — full handler dispatch
# -----------------------------------------------------------------------


def _make_convert_runtime(mock_runtime_data, target_mode: str = "id"):
    """Configure runtime + entry data for a convert_channel_identity test."""
    from solentlabs.cable_modem_monitor_core.orchestration.models import ModemSnapshot
    from solentlabs.cable_modem_monitor_core.orchestration.signals import (
        ConnectionStatus,
        DocsisStatus,
    )

    snapshot = ModemSnapshot(
        connection_status=ConnectionStatus.ONLINE,
        docsis_status=DocsisStatus.OPERATIONAL,
        modem_data={
            "downstream": [
                {"channel_id": 1, "channel_number": 1, "channel_type": "qam"},
                {"channel_id": 2, "channel_number": 2, "channel_type": "qam"},
            ],
            "upstream": [
                {"channel_id": 1, "channel_number": 1, "channel_type": "atdma"},
            ],
            "system_info": {
                "downstream_channel_count": 2,
                "upstream_channel_count": 1,
            },
        },
    )
    mock_runtime_data.data_coordinator.data = snapshot

    entry = _make_mock_entry(mock_runtime_data)
    entry.data = {
        "channel_identity": target_mode,
        "entity_prefix": "none",
        "host": "192.168.100.1",
    }
    return entry


async def test_convert_no_entry_raises(mock_runtime_data) -> None:
    """No loaded entry → handler raises, recorder untouched."""
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []

    handler = create_convert_channel_identity_handler(hass)
    call = _make_mock_call()

    with (
        patch(
            "homeassistant.components.recorder.statistics.async_list_statistic_ids",
            new_callable=AsyncMock,
        ) as mock_list,
        pytest.raises(ServiceValidationError, match="No cable modem configured"),
    ):
        await handler(call)

    mock_list.assert_not_called()


async def test_convert_no_modem_data_raises(mock_runtime_data) -> None:
    """Snapshot is None (modem offline) → handler raises, recorder untouched."""
    mock_runtime_data.data_coordinator.data = None
    entry = _make_mock_entry(mock_runtime_data)
    entry.data = {"channel_identity": "id", "entity_prefix": "none"}
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [entry]

    handler = create_convert_channel_identity_handler(hass)
    call = _make_mock_call()

    with (
        patch(
            "homeassistant.components.recorder.statistics.async_list_statistic_ids",
            new_callable=AsyncMock,
        ) as mock_list,
        pytest.raises(ServiceValidationError, match="modem must be online"),
    ):
        await handler(call)

    mock_list.assert_not_called()


async def test_convert_no_renames_returns_info(mock_runtime_data) -> None:
    """No matching opposite-mode stats → 'already in X mode' info, no migration."""
    entry = _make_convert_runtime(mock_runtime_data, target_mode="id")
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [entry]

    # Recorder reports stats unrelated to this modem
    with (
        patch(
            "homeassistant.components.recorder.statistics.async_list_statistic_ids",
            new_callable=AsyncMock,
            return_value=[
                {"statistic_id": "sensor.thermostat_temperature"},
                {"statistic_id": "sensor.kitchen_humidity"},
            ],
        ),
        patch("homeassistant.helpers.recorder.get_instance") as mock_get,
    ):
        handler = create_convert_channel_identity_handler(hass)
        result = await handler(_make_mock_call())

    assert result["renamed"] == 0
    assert "already in id mode" in result["info"]
    mock_get.assert_not_called()


async def test_convert_to_id_mode_migrates(mock_runtime_data) -> None:
    """Number-mode stats present + target=id → migrates, schedules reload."""
    entry = _make_convert_runtime(mock_runtime_data, target_mode="id")
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [entry]

    recorder = MagicMock()
    # Number-mode stats that should be renamed to id-mode
    number_mode_stats = [
        {"statistic_id": "sensor.cable_modem_ds_ch_1_power"},
        {"statistic_id": "sensor.cable_modem_ds_ch_2_power"},
    ]

    with (
        patch(
            "homeassistant.components.recorder.statistics.async_list_statistic_ids",
            new_callable=AsyncMock,
            return_value=number_mode_stats,
        ),
        patch("homeassistant.helpers.recorder.get_instance", return_value=recorder),
    ):
        handler = create_convert_channel_identity_handler(hass)
        result = await handler(_make_mock_call())

    assert result["mode"] == "id"
    assert result["renamed"] >= 1
    # Reload was scheduled
    hass.async_create_task.assert_called_once()


async def test_convert_to_number_mode_migrates(mock_runtime_data) -> None:
    """ID-mode stats present + target=number → migrates via _plan_stat_renames_to_number."""
    entry = _make_convert_runtime(mock_runtime_data, target_mode="number")
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [entry]

    recorder = MagicMock()
    id_mode_stats = [
        {"statistic_id": "sensor.cable_modem_ds_qam_ch_1_power"},
        {"statistic_id": "sensor.cable_modem_ds_qam_ch_2_power"},
    ]

    with (
        patch(
            "homeassistant.components.recorder.statistics.async_list_statistic_ids",
            new_callable=AsyncMock,
            return_value=id_mode_stats,
        ),
        patch("homeassistant.helpers.recorder.get_instance", return_value=recorder),
    ):
        handler = create_convert_channel_identity_handler(hass)
        result = await handler(_make_mock_call())

    assert result["mode"] == "number"
    assert result["renamed"] >= 1
    hass.async_create_task.assert_called_once()


async def test_convert_with_device_id_resolves_entry(mock_runtime_data) -> None:
    """device_id supplied → handler uses _resolve_config_entry_for_device, not _find_loaded_entry."""
    entry = _make_convert_runtime(mock_runtime_data, target_mode="id")
    hass = MagicMock()

    with (
        patch(
            "custom_components.cable_modem_monitor.dev_tools._resolve_config_entry_for_device",
            return_value=entry,
        ) as mock_resolve,
        patch(
            "homeassistant.components.recorder.statistics.async_list_statistic_ids",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        handler = create_convert_channel_identity_handler(hass)
        result = await handler(_make_mock_call(device_id="dev_abc"))

    mock_resolve.assert_called_once_with(hass, "dev_abc")
    # No stats matched → "no migration needed" info path
    assert result["renamed"] == 0


# -----------------------------------------------------------------------
# Internal yaml-builder branches not covered elsewhere
# -----------------------------------------------------------------------


def test_get_channel_info_number_mode_filters_unlocked() -> None:
    """_get_channel_info_number_mode returns only locked channels, sorted."""
    from custom_components.cable_modem_monitor.dev_tools import (
        _get_channel_info_number_mode,
    )

    channels = [
        {"channel_number": 2, "lock_status": "locked"},
        {"channel_number": 1, "lock_status": "locked"},
        {"channel_number": 3, "lock_status": "not_locked"},
    ]
    result = _get_channel_info_number_mode(channels)
    # sorted by channel_number, only locked ones, with empty type string
    assert result == [("", 1), ("", 2)]


def test_get_channel_info_number_mode_treats_missing_lock_status_as_locked() -> None:
    """Missing lock_status is treated as locked per CHANNEL_IDENTIFICATION_SPEC §6."""
    from custom_components.cable_modem_monitor.dev_tools import (
        _get_channel_info_number_mode,
    )

    channels = [
        {"channel_number": 2},
        {"channel_number": 1, "power": -5.0},
        {"channel_number": 3, "lock_status": None},
        {"channel_number": 4, "lock_status": "not_locked"},
    ]
    result = _get_channel_info_number_mode(channels)
    # Missing and explicit-None lock_status included; not_locked excluded
    assert result == [("", 1), ("", 2), ("", 3)]


def test_get_channel_info_number_mode_dispatched_when_identity_is_number() -> None:
    """_get_channel_info routes to _get_channel_info_number_mode when in NUMBER mode."""
    from custom_components.cable_modem_monitor.const import ChannelIdentity
    from custom_components.cable_modem_monitor.dev_tools import _get_channel_info

    modem_data = {
        "downstream": [{"channel_number": 1, "lock_status": "locked"}],
        "upstream": [{"channel_number": 1, "lock_status": "locked"}],
    }
    ds, us = _get_channel_info(modem_data, ChannelIdentity.NUMBER)
    # Empty type string is the position-mode signal
    assert ds == [("", 1)]
    assert us == [("", 1)]


def test_format_channel_label_position_mode() -> None:
    """Empty ch_type → 'Ch <n>' with no type prefix."""
    from custom_components.cable_modem_monitor.dev_tools import _format_channel_label

    assert _format_channel_label("", 5, "full") == "Ch 5"
    # The format param is ignored in position mode
    assert _format_channel_label("", 5, "type_id") == "Ch 5"


def test_build_channel_graph_defs_number_mode_omits_channel_type() -> None:
    """NUMBER-mode entity patterns omit the {ch_type} segment."""
    from custom_components.cable_modem_monitor.const import ChannelIdentity
    from custom_components.cable_modem_monitor.dev_tools import (
        _build_channel_graph_defs,
    )

    defs = _build_channel_graph_defs(
        entity_prefix="modem",
        identity_mode=ChannelIdentity.NUMBER,
        downstream_info=[("", 1)],
        upstream_info=[("", 1)],
    )
    patterns = [pattern for (_key, _info, _title, pattern) in defs]
    # Every pattern uses the number-mode shape: sensor.{prefix}_{dir}_ch_{ch_id}_{metric}
    for p in patterns:
        assert "{ch_type}" not in p
        assert "_ch_{ch_id}" in p


# create_orphaned_statistics_handler
# -----------------------------------------------------------------------


def _make_mock_entry_minimal(mock_runtime_data):
    """Config entry with entry_id, no snapshot required."""
    entry = _make_mock_entry(mock_runtime_data)
    entry.entry_id = "test_entry_id"
    return entry


async def test_list_orphaned_no_entry_raises() -> None:
    """No loaded entry → handler raises, recorder untouched."""
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []
    handler = create_orphaned_statistics_handler(hass)
    call = _make_mock_call()

    with (
        patch(
            "homeassistant.components.recorder.statistics.async_list_statistic_ids",
            new_callable=AsyncMock,
        ) as mock_list,
        pytest.raises(ServiceValidationError, match="No cable modem configured"),
    ):
        await handler(call)

    mock_list.assert_not_called()


async def test_list_orphaned_no_stats_returns_none_found(mock_runtime_data) -> None:
    """No statistics exist for this prefix → count 0."""
    entry = _make_mock_entry_minimal(mock_runtime_data)
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [entry]
    handler = create_orphaned_statistics_handler(hass)
    call = _make_mock_call()

    with patch(
        "homeassistant.components.recorder.statistics.async_list_statistic_ids",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await handler(call)

    assert result["count"] == 0
    assert "No statistics found" in result["yaml"]


# ┌──────────────────────────────────────┬──────────┬──────────────────────────────────────────────────────┐
# │ scenario                             │ expected │ description                                          │
# ├──────────────────────────────────────┼──────────┼──────────────────────────────────────────────────────┤
# │ all stats have registered entities   │ 0        │ nothing to report                                    │
# │ one stat has no entity               │ 1        │ single orphan in yaml                                │
# │ multiple orphaned                    │ 3        │ all appear, sorted alphabetically                    │
# │ id-mode orphans, position active     │ 2        │ mode-switch scenario                                 │
# │ different prefix entirely excluded   │ 0        │ other integration not counted                        │
# │ prefix boundary — no underscore      │ 0        │ sensor.{prefix}extra excluded (no _ separator)       │
# │ mix: active + orphaned + other pfx   │ 2        │ only orphaned from current prefix in yaml            │
# └──────────────────────────────────────┴──────────┴──────────────────────────────────────────────────────┘
#
# Each case defined as:
#   active_suffixes   — suffixes that ARE registered (f"sensor.{prefix}_{suffix}")
#   orphaned_suffixes — suffixes in stats but NOT registered
#   other_stats       — full stat_ids from unrelated prefixes (excluded by prefix filter)
#   expected_count    — int
#   desc              — test id
#
# fmt: off
LIST_ORPHANED_CASES: list[tuple[list[str], list[str], list[str], int, str]] = [
    (
        ["ds_ch_1_power", "ds_ch_2_power"],
        [],
        [],
        0,
        "all_active",
    ),
    (
        ["ds_ch_1_power"],
        ["ds_qam_ch_21_power"],
        [],
        1,
        "single_orphan",
    ),
    (
        ["ds_ch_1_power"],
        ["ds_qam_ch_21_power", "ds_qam_ch_21_snr", "ds_qam_ch_21_corrected"],
        [],
        3,
        "multiple_orphaned_sorted",
    ),
    (
        ["ds_ch_1_power", "ds_ch_2_power"],      # position-mode active
        ["ds_qam_ch_21_power", "ds_qam_ch_21_snr"],  # id-mode orphans from mode switch
        [],
        2,
        "id_mode_orphans_position_active",
    ),
    (
        ["ds_ch_1_power"],
        [],
        ["sensor.other_integration_sensor", "sensor.totally_different_ds_ch_1_power"],
        0,
        "different_prefix_excluded",
    ),
    (
        ["ds_ch_1_power"],
        [],
        [],
        0,
        "prefix_boundary_no_underscore",
        # sensor.{prefix}extra (no _ after prefix) must NOT be matched —
        # verified by the handler's startswith(f"sensor.{prefix}_") filter
    ),
    (
        ["ds_ch_1_power", "us_ch_1_power"],
        ["ds_qam_ch_21_power", "ds_ofdm_ch_33_snr"],
        ["sensor.other_thing_ds_ch_1_power", "sensor.unrelated_sensor"],
        2,
        "mix_active_orphaned_other_prefix",
    ),
]
# fmt: on


@pytest.mark.parametrize(
    "active_suffixes,orphaned_suffixes,other_stats,expected_count,desc",
    LIST_ORPHANED_CASES,
    ids=[c[4] for c in LIST_ORPHANED_CASES],
)
async def test_list_orphaned_filtering(
    mock_runtime_data,
    active_suffixes: list[str],
    orphaned_suffixes: list[str],
    other_stats: list[str],
    expected_count: int,
    desc: str,
) -> None:
    """Orphan detection correctly filters by prefix, registered entities, and sort order."""
    entry = _make_mock_entry_minimal(mock_runtime_data)
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [entry]
    handler = create_orphaned_statistics_handler(hass)
    call = _make_mock_call()

    prefix = _get_entity_prefix(entry)
    stat_ids = (
        [{"statistic_id": f"sensor.{prefix}_{s}"} for s in active_suffixes + orphaned_suffixes]
        + [{"statistic_id": s} for s in other_stats]
        # prefix-boundary probe: a stat that starts with the prefix but lacks the _ separator
        + [{"statistic_id": f"sensor.{prefix}extra_suffix"}]
    )
    registered = [MagicMock(entity_id=f"sensor.{prefix}_{s}") for s in active_suffixes]

    with (
        patch(
            "homeassistant.components.recorder.statistics.async_list_statistic_ids",
            new_callable=AsyncMock,
            return_value=stat_ids,
        ),
        patch("homeassistant.helpers.entity_registry.async_get") as mock_er,
    ):
        mock_er.return_value.entities.get_entries_for_config_entry_id.return_value = registered
        result = await handler(call)

    assert result["count"] == expected_count

    if expected_count == 0:
        assert "action: recorder.purge_entities" not in result["yaml"]
    else:
        # Preview output is a comment block — not a runnable YAML action.
        assert "action: recorder.purge_entities" not in result["yaml"]
        assert "execute: true" in result["yaml"]
        assert "WARNING" in result["yaml"]
        # orphaned entries appear in preview
        for suffix in orphaned_suffixes:
            assert f"sensor.{prefix}_{suffix}" in result["yaml"]
        # active entities excluded
        for suffix in active_suffixes:
            assert f"sensor.{prefix}_{suffix}" not in result["yaml"]
        # other-prefix stats excluded
        for stat in other_stats:
            assert stat not in result["yaml"]
        # prefix boundary: no-underscore stat excluded
        assert f"sensor.{prefix}extra_suffix" not in result["yaml"]


@pytest.mark.asyncio
async def test_list_orphaned_execute_clears_statistics(mock_runtime_data) -> None:
    """execute=True calls recorder async_clear_statistics in batches and returns purge count."""
    entry = _make_mock_entry_minimal(mock_runtime_data)
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [entry]
    handler = create_orphaned_statistics_handler(hass)
    call = _make_mock_call({"execute": True})

    prefix = _get_entity_prefix(entry)
    # 150 orphaned stats — spans two internal batches of 100
    orphaned_ids = sorted(f"sensor.{prefix}_ch_{i:03d}_power" for i in range(150))
    stat_ids = [{"statistic_id": sid} for sid in orphaned_ids]

    cleared_batches: list[list[str]] = []
    # call_soon_threadsafe is mocked — make it invoke the callback immediately
    hass.loop.call_soon_threadsafe.side_effect = lambda fn: fn()

    def fake_async_clear_statistics(ids: list[str], *, on_done=None) -> None:
        cleared_batches.append(list(ids))
        if on_done:
            hass.loop.call_soon_threadsafe(on_done)

    mock_recorder = MagicMock()
    mock_recorder.async_clear_statistics.side_effect = fake_async_clear_statistics

    with (
        patch(
            "homeassistant.components.recorder.statistics.async_list_statistic_ids",
            new_callable=AsyncMock,
            return_value=stat_ids,
        ),
        patch("homeassistant.helpers.entity_registry.async_get") as mock_er,
        patch(
            "custom_components.cable_modem_monitor.dev_tools.get_instance",
            return_value=mock_recorder,
        ),
    ):
        mock_er.return_value.entities.get_entries_for_config_entry_id.return_value = []
        result = await handler(call)

    assert result["count"] == 150
    assert "Purged 150" in result["yaml"]
    # Two batches: first 100, then 50
    assert len(cleared_batches) == 2
    assert len(cleared_batches[0]) == 100
    assert len(cleared_batches[1]) == 50
    assert cleared_batches[0] + cleared_batches[1] == orphaned_ids


@pytest.mark.asyncio
async def test_list_orphaned_preview_large_list_shows_execute_hint(mock_runtime_data) -> None:
    """Preview of a large list mentions execute: true and shows partial entity list."""
    entry = _make_mock_entry_minimal(mock_runtime_data)
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [entry]
    handler = create_orphaned_statistics_handler(hass)
    call = _make_mock_call()

    prefix = _get_entity_prefix(entry)
    orphaned_ids = [f"sensor.{prefix}_ch_{i:03d}_power" for i in range(150)]
    stat_ids = [{"statistic_id": sid} for sid in orphaned_ids]

    with (
        patch(
            "homeassistant.components.recorder.statistics.async_list_statistic_ids",
            new_callable=AsyncMock,
            return_value=stat_ids,
        ),
        patch("homeassistant.helpers.entity_registry.async_get") as mock_er,
    ):
        mock_er.return_value.entities.get_entries_for_config_entry_id.return_value = []
        result = await handler(call)

    assert result["count"] == 150
    assert "execute: true" in result["yaml"]
    assert "action: recorder.purge_entities" not in result["yaml"]
    # all 150 entities appear in the preview
    for sid in orphaned_ids:
        assert sid in result["yaml"]
    # output is comment lines only — safe to display, nothing executes
    non_comment_lines = [ln for ln in result["yaml"].splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert non_comment_lines == []
