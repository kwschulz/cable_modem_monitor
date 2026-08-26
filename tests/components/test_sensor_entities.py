"""Tests for sensor entity creation and value rendering.

Verifies sensor entity creation logic via _create_channel_sensors,
_create_lan_sensors, and _create_data_dependent_entities.  Entity
value rendering is tested with mock coordinator data.

Deferred entity creation tests verify UC-84: when the first poll
returns modem_data=None (modem unreachable at startup), data-dependent
entities are created on the first successful poll via a one-shot
coordinator listener.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from solentlabs.cable_modem_monitor_core.orchestration.models import (
    HealthInfo,
    ModemSnapshot,
)
from solentlabs.cable_modem_monitor_core.orchestration.signals import (
    ConnectionStatus,
    DocsisStatus,
    HealthStatus,
)

from custom_components.cable_modem_monitor.const import (
    CONF_CHANNEL_IDENTITY,
    ChannelIdentity,
)
from custom_components.cable_modem_monitor.mapping_manager import build_channel_map
from custom_components.cable_modem_monitor.sensor import (
    ChannelSensor,
    HttpLatencySensor,
    LanStatsSensor,
    ModemChannelCountSensor,
    ModemErrorTotalSensor,
    ModemInfoSensor,
    ModemLastBootTimeSensor,
    ModemSoftwareVersionSensor,
    ModemStatusSensor,
    PingLatencySensor,
    SystemInfoFieldSensor,
    TcpLatencySensor,
    _create_channel_sensors,
    _create_lan_sensors,
    _humanize_field_name,
)

from .conftest import MOCK_ENTRY_DATA, MOCK_MODEM_DATA

# -----------------------------------------------------------------------
# Module-level test data
# -----------------------------------------------------------------------

MODEM_DATA_WITH_LAN: dict[str, Any] = {
    **MOCK_MODEM_DATA,
    "lan_stats": {
        "eth0": {"received_bytes": 1000, "transmitted_bytes": 500},
    },
}

MODEM_DATA_LAN_SINGLE: dict[str, Any] = {
    **MOCK_MODEM_DATA,
    "lan_stats": {"eth0": {"received_bytes": 42000}},
}

# System_info with Tier 3 pass-through fields (not consumed by dedicated sensors)
MODEM_DATA_WITH_PASSTHROUGH: dict[str, Any] = {
    **MOCK_MODEM_DATA,
    "system_info": {
        **MOCK_MODEM_DATA["system_info"],
        "ds_scanning_status": "success",
        "us_ranging_status": "success",
        "dhcp_status": "success",
    },
}

# First-poll-shaped data: SC-QAM totals present (capability), rate
# fields absent (orchestrator omits rate_* on the first poll because
# there's no prior baseline). Used to verify capability gating
# creates rate sensors even when the rate fields aren't populated.
MODEM_DATA_RATE_FIELDS_ABSENT: dict[str, Any] = {
    "system_info": {
        "software_version": "1.0",
        "total_corrected": 100,
        "total_uncorrected": 5,
    },
    "downstream": [
        {
            "channel_number": 1,
            "channel_id": 1,
            "channel_type": "qam",
            "lock_status": "locked",
            "frequency": 555000000,
            "power": 2.5,
            "snr": 38.0,
        }
    ],
    "upstream": [],
}

# Channel shapes for the capability-gating table below.  A placeholder
# cell ("----") makes Core omit the key, so the channel arrives without
# it; an unlocked channel arrives with its metrics stripped.
_FULL: dict[str, Any] = {
    "channel_number": 1,
    "channel_id": 1,
    "channel_type": "qam",
    "lock_status": "locked",
    "frequency": 555000000,
    "power": 2.5,
    "snr": 38.0,
    "corrected": 100,
    "uncorrected": 2,
}
_GAPS: dict[str, Any] = {
    "channel_number": 2,
    "channel_id": 2,
    "channel_type": "qam",
    "lock_status": "locked",
    "frequency": 563000000,
}
_UNLOCKED: dict[str, Any] = {
    "channel_number": 3,
    "lock_status": "not_locked",
}
_NO_FEC: dict[str, Any] = {
    "channel_number": 1,
    "channel_id": 1,
    "channel_type": "qam",
    "lock_status": "locked",
    "frequency": 555000000,
    "power": 2.5,
    "snr": 38.0,
}

_ALL_METRICS = ["corrected", "frequency", "power", "snr", "uncorrected"]
_ALWAYS_ONLY = ["power", "snr"]
_NO_FEC_METRICS = ["frequency", "power", "snr"]

# fmt: off
CHANNEL_GATING_CASES = [
    # (channels,           identity mode,          slot key,   created,          description)
    ([_FULL, _GAPS],       ChannelIdentity.ID,     ("qam", 2), _ALL_METRICS,     "placeholder cells, id mode"),
    ([_FULL, _GAPS],       ChannelIdentity.NUMBER, 2,          _ALL_METRICS,     "placeholder cells, position mode"),
    ([_FULL, _UNLOCKED],   ChannelIdentity.NUMBER, 3,          _ALWAYS_ONLY,     "unlocked channel, always-set only"),
    ([_NO_FEC],            ChannelIdentity.NUMBER, 1,          _NO_FEC_METRICS,  "modem without FEC columns"),
]
# fmt: on


def _downstream_only(channels: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap a downstream channel list as modem_data."""
    return {
        "system_info": {"software_version": "1.0"},
        "downstream": channels,
        "upstream": [],
    }


# -----------------------------------------------------------------------
# _create_channel_sensors
# -----------------------------------------------------------------------


def _make_coord_and_entry(modem_data, runtime_data):
    """Create mock coordinator and entry for sensor construction."""
    coord = MagicMock()
    coord.data = ModemSnapshot(
        connection_status=ConnectionStatus.ONLINE,
        docsis_status=DocsisStatus.OPERATIONAL,
        modem_data=modem_data,
    )
    coord.last_update_success = True

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = MOCK_ENTRY_DATA
    entry.runtime_data = runtime_data
    return coord, entry


def test_create_channel_sensors_downstream(mock_runtime_data):
    """Downstream channels create power+SNR sensors (always) + published metrics."""
    coord, entry = _make_coord_and_entry(MOCK_MODEM_DATA, mock_runtime_data)
    sensors = _create_channel_sensors(coord, entry, MOCK_MODEM_DATA)

    # ID mode (from MOCK_ENTRY_DATA):
    # 2 DS channels × (power, snr always + frequency, corrected, uncorrected published) = 2 × 5 = 10
    # 1 US channel × (power always + frequency published) = 1 × 2 = 2
    # Total = 12
    assert len(sensors) == 12

    # Verify a downstream sensor has correct ID-mode naming
    ds_power = [s for s in sensors if "qam" in str(s._attr_unique_id) and "power" in str(s._attr_unique_id)]
    assert len(ds_power) == 1
    assert ds_power[0]._attr_name == "DS QAM Ch 1 Power"


def test_create_channel_sensors_position_mode(mock_runtime_data):
    """Position mode creates sensors keyed by channel_number (no type in name)."""
    entry_data = {**MOCK_ENTRY_DATA, CONF_CHANNEL_IDENTITY: ChannelIdentity.NUMBER}
    coord = MagicMock()
    coord.data = ModemSnapshot(
        connection_status=ConnectionStatus.ONLINE,
        docsis_status=DocsisStatus.OPERATIONAL,
        modem_data=MOCK_MODEM_DATA,
    )
    coord.last_update_success = True
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = entry_data
    entry.runtime_data = mock_runtime_data

    sensors = _create_channel_sensors(coord, entry, MOCK_MODEM_DATA)

    # Same count: 2 DS × 5 metrics + 1 US × 2 metrics = 12
    assert len(sensors) == 12

    # Position-mode naming: no channel type in name
    ds_power = [s for s in sensors if "_ds_ch_1_power" in str(s._attr_unique_id)]
    assert len(ds_power) == 1
    assert ds_power[0]._attr_name == "DS Ch 1 Power"


@pytest.mark.parametrize(
    "channels,identity_mode,slot_key,expected,desc",
    CHANNEL_GATING_CASES,
    ids=[case[4] for case in CHANNEL_GATING_CASES],
)
def test_create_channel_sensors_capability_gating(
    mock_runtime_data,
    channels,
    identity_mode,
    slot_key,
    expected,
    desc,
):
    """Metric creation follows what the direction publishes.

    Creation is one-shot, so a per-channel gate strands a channel whose
    cell held a placeholder.  See ENTITY_MODEL_SPEC.md, Capability
    Gating.
    """
    modem_data = _downstream_only(channels)
    coord, entry = _make_coord_and_entry(modem_data, mock_runtime_data)
    entry.data = {**MOCK_ENTRY_DATA, CONF_CHANNEL_IDENTITY: identity_mode}

    sensors = _create_channel_sensors(coord, entry, modem_data)

    created = sorted(s._field for s in sensors if isinstance(s, ChannelSensor) and s._slot_key == slot_key)
    assert created == expected, f"Failed: {desc}"


def test_channel_sensor_placeholder_cell_reads_none(mock_runtime_data):
    """A placeholder cell carries no value, so HA renders `unknown`."""
    modem_data = _downstream_only([_FULL, _GAPS])
    coord, entry = _make_coord_and_entry(modem_data, mock_runtime_data)
    mock_runtime_data.channel_map = build_channel_map(
        modem_data["downstream"],
        modem_data["upstream"],
        ChannelIdentity.ID,
    )

    sensors = _create_channel_sensors(coord, entry, modem_data)

    placeholder = [s for s in sensors if isinstance(s, ChannelSensor) and s._slot_key == ("qam", 2)]
    assert sorted(s._field for s in placeholder if s.native_value is None) == [
        "corrected",
        "power",
        "snr",
        "uncorrected",
    ]


def test_create_channel_sensors_empty_data(mock_runtime_data):
    """No channels produces no sensors."""
    coord, entry = _make_coord_and_entry({}, mock_runtime_data)
    sensors = _create_channel_sensors(coord, entry, {})
    assert sensors == []


def test_create_lan_sensors(mock_runtime_data):
    """LAN stats sensors created per interface per metric."""
    coord, entry = _make_coord_and_entry(MODEM_DATA_WITH_LAN, mock_runtime_data)
    sensors = _create_lan_sensors(coord, entry, MODEM_DATA_WITH_LAN)
    # 1 interface × 8 metrics = 8
    assert len(sensors) == 8


# -----------------------------------------------------------------------
# Sensor value rendering (with mock coordinator)
# -----------------------------------------------------------------------


def _make_sensor(sensor_cls, mock_runtime_data, modem_data=None, **kwargs):
    """Helper to construct a sensor with mock coordinator."""
    snapshot = ModemSnapshot(
        connection_status=ConnectionStatus.ONLINE,
        docsis_status=DocsisStatus.OPERATIONAL,
        modem_data=modem_data if modem_data is not None else MOCK_MODEM_DATA,
        health_info=HealthInfo(health_status=HealthStatus.RESPONSIVE, icmp_latency_ms=2.5, http_latency_ms=15.0),
    )
    coord = MagicMock()
    coord.data = snapshot
    coord.last_update_success = True

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = MOCK_ENTRY_DATA
    entry.runtime_data = mock_runtime_data

    return sensor_cls(coord, entry, **kwargs)


def test_channel_count_sensor_value(mock_runtime_data):
    """Channel count sensor reads from system_info."""
    sensor = _make_sensor(ModemChannelCountSensor, mock_runtime_data, direction="downstream")
    assert sensor.native_value == 2


def test_channel_count_sensor_missing(mock_runtime_data):
    """Channel count returns None when system_info missing."""
    sensor = _make_sensor(ModemChannelCountSensor, mock_runtime_data, modem_data={}, direction="downstream")
    assert sensor.native_value is None


def test_error_total_sensor_value(mock_runtime_data):
    """Error total reads from system_info."""
    sensor = _make_sensor(ModemErrorTotalSensor, mock_runtime_data, error_type="corrected")
    assert sensor.native_value == 150


@pytest.mark.parametrize("error_type", ["corrected", "uncorrected"])
def test_error_total_sensor_missing(mock_runtime_data, error_type):
    """Total sensor returns None when system_info lacks the field.

    Mirrors ``test_error_rate_sensor_value``'s field-absent cases.
    A modem with no SC-QAM error counters (OFDM-only, or before the
    parser aggregate runs) has no ``total_*`` field in system_info;
    the sensor reads None and HA renders ``unknown``.
    """
    sensor = _make_sensor(
        ModemErrorTotalSensor,
        mock_runtime_data,
        modem_data={"system_info": {}, "downstream": [], "upstream": []},
        error_type=error_type,
    )
    assert sensor.native_value is None


# Table-driven: each row exercises ModemErrorRateSensor.native_value
# against a custom system_info value, covering both error_types, both
# nominal and zero values, and the absent-field (None) case.
@pytest.mark.parametrize(
    ("error_type", "field_value", "expected"),
    [
        pytest.param("corrected", 42.0, 42.0, id="corrected_typical"),
        pytest.param("uncorrected", 0.5, 0.5, id="uncorrected_fractional"),
        pytest.param("corrected", 0.0, 0.0, id="corrected_zero_is_real_signal"),
        pytest.param("uncorrected", 0.0, 0.0, id="uncorrected_zero_is_real_signal"),
        pytest.param("corrected", None, None, id="corrected_field_absent"),
        pytest.param("uncorrected", None, None, id="uncorrected_field_absent"),
    ],
)
def test_error_rate_sensor_value(mock_runtime_data, error_type, field_value, expected):
    """Rate sensor reads from `rate_{error_type}` in system_info.

    Covers nominal values, zero (a real signal — "no new errors observed"),
    and field absent (orchestrator omitted on this poll → HA shows
    `unknown`).
    """
    from custom_components.cable_modem_monitor.sensor import ModemErrorRateSensor

    system_info = {} if field_value is None else {f"rate_{error_type}": field_value}
    modem_data = {"system_info": system_info, "downstream": [], "upstream": []}
    sensor = _make_sensor(
        ModemErrorRateSensor,
        mock_runtime_data,
        modem_data=modem_data,
        error_type=error_type,
    )
    assert sensor.native_value == expected


@pytest.mark.parametrize("error_type", ["corrected", "uncorrected"])
def test_error_rate_sensor_display_metadata(mock_runtime_data, error_type):
    """Rate sensor exposes errors/min as MEASUREMENT (point-in-time), 2dp."""
    from homeassistant.components.sensor import SensorStateClass

    from custom_components.cable_modem_monitor.sensor import ModemErrorRateSensor

    sensor = _make_sensor(ModemErrorRateSensor, mock_runtime_data, error_type=error_type)
    assert sensor._attr_native_unit_of_measurement == "errors/min"
    assert sensor._attr_state_class == SensorStateClass.MEASUREMENT
    # An inter-poll delta divided by elapsed time carries no meaningful
    # digits past this; HA renders the raw float without it.
    assert sensor.suggested_display_precision == 2


def test_software_version_sensor_value(mock_runtime_data):
    """Software version sensor returns version string."""
    sensor = _make_sensor(ModemSoftwareVersionSensor, mock_runtime_data)
    assert sensor.native_value == "4502.9.016"


def test_modem_info_sensor(mock_runtime_data):
    """Modem info shows model name and identity attributes."""
    sensor = _make_sensor(ModemInfoSensor, mock_runtime_data)
    assert sensor.native_value == "TPS-2000"
    attrs = sensor.extra_state_attributes
    assert attrs["manufacturer"] == "Solent Labs"
    assert attrs["status"] == "confirmed"


def test_modem_status_sensor_operational(mock_runtime_data):
    """Status sensor shows Operational when all good."""
    snapshot = ModemSnapshot(
        connection_status=ConnectionStatus.ONLINE,
        docsis_status=DocsisStatus.OPERATIONAL,
        modem_data=MOCK_MODEM_DATA,
        health_info=HealthInfo(health_status=HealthStatus.RESPONSIVE),
    )
    coord = MagicMock()
    coord.data = snapshot
    coord.last_update_success = True

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = MOCK_ENTRY_DATA
    entry.runtime_data = mock_runtime_data
    entry.runtime_data.health_coordinator = None  # No separate health coord

    sensor = ModemStatusSensor(coord, entry)
    assert sensor.native_value == "Operational"
    assert sensor.extra_state_attributes["connection_status"] == "online"


def test_channel_sensor_native_value_id_mode(mock_runtime_data):
    """Channel sensor reads correct field from channel data (ID mode)."""
    coord = MagicMock()
    coord.data = ModemSnapshot(
        connection_status=ConnectionStatus.ONLINE,
        docsis_status=DocsisStatus.OPERATIONAL,
        modem_data=MOCK_MODEM_DATA,
    )
    coord.last_update_success = True

    mock_runtime_data.channel_map = build_channel_map(
        MOCK_MODEM_DATA["downstream"],
        MOCK_MODEM_DATA["upstream"],
        ChannelIdentity.ID,
    )
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = MOCK_ENTRY_DATA
    entry.runtime_data = mock_runtime_data

    sensor = ChannelSensor(
        coord,
        entry,
        direction="downstream",
        slot_key=("qam", 1),
        identity_mode=ChannelIdentity.ID,
        field="power",
        name_suffix="Power",
        unit="dBmV",
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal",
        value_type=float,
    )
    assert sensor.native_value == 2.5
    attrs = sensor.extra_state_attributes
    assert attrs["channel_id"] == 1
    assert attrs["channel_type"] == "qam"
    # Non-metric fields pass through
    assert "modulation" in attrs


def test_channel_sensor_native_value_position_mode(mock_runtime_data):
    """Channel sensor reads correct field from channel data (position mode)."""
    coord = MagicMock()
    coord.data = ModemSnapshot(
        connection_status=ConnectionStatus.ONLINE,
        docsis_status=DocsisStatus.OPERATIONAL,
        modem_data=MOCK_MODEM_DATA,
    )
    coord.last_update_success = True

    mock_runtime_data.channel_map = build_channel_map(
        MOCK_MODEM_DATA["downstream"],
        MOCK_MODEM_DATA["upstream"],
        ChannelIdentity.NUMBER,
    )
    entry_data = {**MOCK_ENTRY_DATA, CONF_CHANNEL_IDENTITY: ChannelIdentity.NUMBER}
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = entry_data
    entry.runtime_data = mock_runtime_data

    sensor = ChannelSensor(
        coord,
        entry,
        direction="downstream",
        slot_key=1,
        identity_mode=ChannelIdentity.NUMBER,
        field="power",
        name_suffix="Power",
        unit="dBmV",
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal",
        value_type=float,
    )
    assert sensor.native_value == 2.5
    assert sensor._attr_name == "DS Ch 1 Power"
    assert sensor._attr_unique_id is not None
    assert "cable_modem_ds_ch_1_power" in sensor._attr_unique_id
    attrs = sensor.extra_state_attributes
    assert attrs["channel_id"] == 1
    assert attrs["channel_number"] == 1


def test_channel_sensor_not_found(mock_runtime_data):
    """Channel sensor returns None when channel not in data."""
    coord = MagicMock()
    coord.data = ModemSnapshot(
        connection_status=ConnectionStatus.ONLINE,
        docsis_status=DocsisStatus.OPERATIONAL,
        modem_data=MOCK_MODEM_DATA,
    )

    mock_runtime_data.channel_map = build_channel_map(
        MOCK_MODEM_DATA["downstream"],
        MOCK_MODEM_DATA["upstream"],
        ChannelIdentity.ID,
    )
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = MOCK_ENTRY_DATA
    entry.runtime_data = mock_runtime_data

    sensor = ChannelSensor(
        coord,
        entry,
        direction="downstream",
        slot_key=("qam", 999),  # doesn't exist
        identity_mode=ChannelIdentity.ID,
        field="power",
        name_suffix="Power",
        unit="dBmV",
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal",
        value_type=float,
    )
    assert sensor.native_value is None


def test_health_sensor_ping_latency(mock_runtime_data):
    """Ping latency sensor reads from health coordinator."""
    health_info = HealthInfo(
        health_status=HealthStatus.RESPONSIVE,
        icmp_latency_ms=3.7,
        http_latency_ms=12.0,
    )
    coord = MagicMock()
    coord.data = health_info
    coord.last_update_success = True

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = MOCK_ENTRY_DATA
    entry.runtime_data = mock_runtime_data

    sensor = PingLatencySensor(coord, entry)
    assert sensor.native_value == 4  # rounded from 3.7


def test_health_sensor_http_latency(mock_runtime_data):
    """HTTP latency sensor reads from health coordinator."""
    health_info = HealthInfo(
        health_status=HealthStatus.RESPONSIVE,
        icmp_latency_ms=2.5,
        http_latency_ms=14.8,
    )
    coord = MagicMock()
    coord.data = health_info
    coord.last_update_success = True

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = MOCK_ENTRY_DATA
    entry.runtime_data = mock_runtime_data

    sensor = HttpLatencySensor(coord, entry)
    assert sensor.native_value == 15  # rounded from 14.8


def test_health_sensor_tcp_latency(mock_runtime_data):
    """TCP latency sensor reads from health coordinator."""
    health_info = HealthInfo(
        health_status=HealthStatus.RESPONSIVE,
        icmp_latency_ms=2.5,
        tcp_latency_ms=1.6,
        http_latency_ms=14.8,
    )
    coord = MagicMock()
    coord.data = health_info
    coord.last_update_success = True

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = MOCK_ENTRY_DATA
    entry.runtime_data = mock_runtime_data

    sensor = TcpLatencySensor(coord, entry)
    assert sensor.native_value == 2  # rounded from 1.6


def test_ping_latency_caches_last_value(mock_runtime_data):
    """Ping latency sensor returns cached value when probe returns None."""
    coord = MagicMock()
    coord.last_update_success = True
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = MOCK_ENTRY_DATA
    entry.runtime_data = mock_runtime_data

    sensor = PingLatencySensor(coord, entry)

    # First update: real value
    coord.data = HealthInfo(
        health_status=HealthStatus.RESPONSIVE,
        icmp_latency_ms=4.2,
        http_latency_ms=100.0,
    )
    vars(sensor).pop("native_value", None)
    assert sensor.native_value == 4

    # Second update: probe returns None — sensor keeps cached value
    coord.data = HealthInfo(
        health_status=HealthStatus.RESPONSIVE,
        icmp_latency_ms=None,
        http_latency_ms=100.0,
    )
    vars(sensor).pop("native_value", None)
    assert sensor.native_value == 4

    # Third update: new value — sensor updates
    coord.data = HealthInfo(
        health_status=HealthStatus.RESPONSIVE,
        icmp_latency_ms=6.1,
        http_latency_ms=100.0,
    )
    vars(sensor).pop("native_value", None)
    assert sensor.native_value == 6


def test_http_latency_caches_last_value(mock_runtime_data):
    """HTTP latency sensor returns cached value when probe returns None."""
    coord = MagicMock()
    coord.last_update_success = True
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = MOCK_ENTRY_DATA
    entry.runtime_data = mock_runtime_data

    sensor = HttpLatencySensor(coord, entry)

    # First update: real value
    coord.data = HealthInfo(
        health_status=HealthStatus.RESPONSIVE,
        icmp_latency_ms=3.0,
        http_latency_ms=110.7,
    )
    vars(sensor).pop("native_value", None)
    assert sensor.native_value == 111

    # Second update: probe returns None — sensor keeps cached value
    coord.data = HealthInfo(
        health_status=HealthStatus.RESPONSIVE,
        icmp_latency_ms=3.0,
        http_latency_ms=None,
    )
    vars(sensor).pop("native_value", None)
    assert sensor.native_value == 111

    # Third update: new value — sensor updates
    coord.data = HealthInfo(
        health_status=HealthStatus.RESPONSIVE,
        icmp_latency_ms=3.0,
        http_latency_ms=95.2,
    )
    vars(sensor).pop("native_value", None)
    assert sensor.native_value == 95


def test_latency_sensors_return_none_when_never_measured(mock_runtime_data):
    """Latency sensors return None when no measurement has ever succeeded."""
    coord = MagicMock()
    coord.last_update_success = True
    coord.data = HealthInfo(
        health_status=HealthStatus.RESPONSIVE,
        icmp_latency_ms=None,
        http_latency_ms=None,
    )
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = MOCK_ENTRY_DATA
    entry.runtime_data = mock_runtime_data

    ping = PingLatencySensor(coord, entry)
    assert ping.native_value is None

    http = HttpLatencySensor(coord, entry)
    assert http.native_value is None


def test_lan_stats_sensor_value(mock_runtime_data):
    """LAN stats sensor reads interface data."""
    coord = MagicMock()
    coord.data = ModemSnapshot(
        connection_status=ConnectionStatus.ONLINE,
        docsis_status=DocsisStatus.OPERATIONAL,
        modem_data=MODEM_DATA_LAN_SINGLE,
    )
    coord.last_update_success = True

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = MOCK_ENTRY_DATA
    entry.runtime_data = mock_runtime_data

    sensor = LanStatsSensor(
        coord,
        entry,
        interface="eth0",
        field="received_bytes",
        name_suffix="Received Bytes",
        device_class=None,
        unit="B",
    )
    assert sensor.native_value == 42000


# -----------------------------------------------------------------------
# _humanize_field_name — pure function
# -----------------------------------------------------------------------

# ┌──────────────────────────┬──────────────────────────┬─────────────────────┐
# │ field                    │ expected                 │ description         │
# ├──────────────────────────┼──────────────────────────┼─────────────────────┤
# │ ds_scanning_status       │ DS Scanning Status       │ ds abbreviation     │
# │ us_ranging_status        │ US Ranging Status        │ us abbreviation     │
# │ dhcp_status              │ DHCP Status              │ dhcp abbreviation   │
# │ registration_status      │ Registration Status      │ no abbreviation     │
# │ software_version         │ Software Version         │ simple words        │
# │ tftp_status              │ TFTP Status              │ tftp abbreviation   │
# │ ip_address               │ IP Address               │ ip abbreviation     │
# │ snr_margin               │ SNR Margin               │ snr abbreviation    │
# └──────────────────────────┴──────────────────────────┴─────────────────────┘
#
# fmt: off
HUMANIZE_CASES = [
    ("ds_scanning_status",  "DS Scanning Status",  "ds_prefix"),
    ("us_ranging_status",   "US Ranging Status",   "us_prefix"),
    ("dhcp_status",         "DHCP Status",         "dhcp_abbreviation"),
    ("registration_status", "Registration Status", "no_abbreviation"),
    ("software_version",    "Software Version",    "simple_words"),
    ("tftp_status",         "TFTP Status",         "tftp_abbreviation"),
    ("ip_address",          "IP Address",          "ip_abbreviation"),
    ("snr_margin",          "SNR Margin",          "snr_abbreviation"),
]
# fmt: on


@pytest.mark.parametrize(
    "field,expected,desc",
    HUMANIZE_CASES,
    ids=[c[2] for c in HUMANIZE_CASES],
)
def test_humanize_field_name(field, expected, desc):
    """_humanize_field_name handles abbreviations and title case."""
    assert _humanize_field_name(field) == expected


# -----------------------------------------------------------------------
# SystemInfoFieldSensor — Tier 3 dynamic pass-through
# -----------------------------------------------------------------------

# ┌────────────────────────┬────────────────────────┬───────────┬──────────┬───────┬────────────┬───────────┐
# │ id                     │ field                  │ value     │ expected │ unit  │ device_cls │ suggested │
# ├────────────────────────┼────────────────────────┼───────────┼──────────┼───────┼────────────┼───────────┤
# │ string_value           │ ds_scanning_status     │ "success" │ success  │ None  │ None       │ None      │
# │ numeric_value          │ temperature            │ 42.5      │ 42.5     │ None  │ None       │ None      │
# │ missing_field          │ nonexistent            │ (absent)  │ None     │ None  │ None       │ None      │
# │ speed_down             │ provisioned_speed_down │ 110100480 │ 110…480  │ bit/s │ DATA_RATE  │ Mbit/s    │
# │ burst_down             │ provisioned_burst_down │ 412876    │ 412876   │ B     │ DATA_SIZE  │ None      │
# └────────────────────────┴────────────────────────┴───────────┴──────────┴───────┴────────────┴───────────┘
# Native unit stays bit/s: that is what Core stores. The suggested unit is
# display only, applied by HA at first entity registration.

_DATA_RATE = SensorDeviceClass.DATA_RATE
_DATA_SIZE = SensorDeviceClass.DATA_SIZE

_SYSINFO_FIELD_CASES = [
    ("string_value", "ds_scanning_status", {"ds_scanning_status": "success"}, "success", None, None, None),
    ("numeric_value", "temperature", {"temperature": 42.5}, 42.5, None, None, None),
    ("missing_field", "nonexistent", {}, None, None, None, None),
    (
        "speed_down",
        "provisioned_speed_down",
        {"provisioned_speed_down": 110100480},
        110100480,
        "bit/s",
        _DATA_RATE,
        "Mbit/s",
    ),
    ("burst_down", "provisioned_burst_down", {"provisioned_burst_down": 412876}, 412876, "B", _DATA_SIZE, None),
]


@pytest.mark.parametrize(
    (
        "case_id",
        "field",
        "system_info",
        "expected_value",
        "expected_unit",
        "expected_device_class",
        "expected_suggested_unit",
    ),
    _SYSINFO_FIELD_CASES,
    ids=[c[0] for c in _SYSINFO_FIELD_CASES],
)
def test_system_info_field_sensor(
    mock_runtime_data,
    case_id,
    field,
    system_info,
    expected_value,
    expected_unit,
    expected_device_class,
    expected_suggested_unit,
):
    """SystemInfoFieldSensor reads value and applies unit metadata when present."""
    modem_data: dict[str, Any] = {
        "system_info": system_info,
        "downstream": [],
        "upstream": [],
    }
    sensor = _make_sensor(
        SystemInfoFieldSensor,
        mock_runtime_data,
        modem_data=modem_data,
        field=field,
    )
    assert sensor.native_value == expected_value
    assert getattr(sensor, "_attr_native_unit_of_measurement", None) == expected_unit
    assert getattr(sensor, "_attr_device_class", None) == expected_device_class
    assert getattr(sensor, "_attr_suggested_unit_of_measurement", None) == expected_suggested_unit


# -----------------------------------------------------------------------
# _create_data_dependent_entities — extracted helper
# -----------------------------------------------------------------------


def test_create_data_dependent_entities(mock_runtime_data):
    """Helper creates system, channel, and LAN sensors from modem data."""
    from custom_components.cable_modem_monitor.sensor import (
        ModemErrorRateSensor,
        _create_data_dependent_entities,
    )

    coord, entry = _make_coord_and_entry(MOCK_MODEM_DATA, mock_runtime_data)
    entities = _create_data_dependent_entities(coord, entry, MOCK_MODEM_DATA)

    # MOCK_MODEM_DATA has:
    #   system_info with total_corrected, total_uncorrected, rate_corrected,
    #     rate_uncorrected, software_version, system_uptime
    #   2 DS channels (qam + ofdm), 1 US channel (atdma)
    #   No lan_stats
    #
    # Expected entities:
    #   2 channel counts (DS + US)
    #   2 error totals (corrected + uncorrected)
    #   2 error rates (corrected + uncorrected)
    #   1 software version
    #   1 last boot time (uptime present; no uptime sensor — display-derived)
    #   12 channel sensors (see test_create_channel_sensors_downstream)
    #   0 LAN sensors (no lan_stats)
    # Total = 20
    assert len(entities) == 20

    rate_sensors = [e for e in entities if isinstance(e, ModemErrorRateSensor)]
    assert len(rate_sensors) == 2


def test_create_data_dependent_entities_rate_omitted_first_poll(mock_runtime_data):
    """Rate sensors are created on first poll (gated by SC-QAM capability).

    The orchestrator omits ``rate_corrected`` / ``rate_uncorrected`` on the
    first successful poll (no prior baseline), but HA's data-dependent
    entity creation runs exactly once at first-data-available. Gating
    rate creation on ``rate_corrected`` presence would leave the sensors
    permanently absent. Capability gating (``total_corrected`` presence)
    is the fix: the sensor exists from poll 1, reads ``None`` until the
    orchestrator emits a rate on poll 2+.

    Fixture: ``MODEM_DATA_RATE_FIELDS_ABSENT`` (module-level constant).
    """
    from custom_components.cable_modem_monitor.sensor import (
        ModemErrorRateSensor,
        _create_data_dependent_entities,
    )

    coord, entry = _make_coord_and_entry(MODEM_DATA_RATE_FIELDS_ABSENT, mock_runtime_data)
    entities = _create_data_dependent_entities(coord, entry, MODEM_DATA_RATE_FIELDS_ABSENT)

    rate_sensors = [e for e in entities if isinstance(e, ModemErrorRateSensor)]
    assert len(rate_sensors) == 2
    # On a poll where the orchestrator omitted the rate field, the
    # sensor's native_value is None — HA renders this as `unknown`.
    for sensor in rate_sensors:
        assert sensor.native_value is None


def test_create_data_dependent_entities_no_channels(mock_runtime_data):
    """Helper with empty channel lists creates only system sensors."""
    from custom_components.cable_modem_monitor.sensor import (
        _create_data_dependent_entities,
    )

    minimal_data: dict[str, Any] = {
        "system_info": {"software_version": "1.0"},
        "downstream": [],
        "upstream": [],
    }
    coord, entry = _make_coord_and_entry(minimal_data, mock_runtime_data)
    entities = _create_data_dependent_entities(coord, entry, minimal_data)

    # 2 channel counts + 1 software version + 0 channels + 0 LAN = 3
    assert len(entities) == 3


def test_create_data_dependent_entities_with_passthrough(mock_runtime_data):
    """Tier 3 system_info fields produce dynamic SystemInfoFieldSensor instances."""
    from custom_components.cable_modem_monitor.sensor import (
        _create_data_dependent_entities,
    )

    coord, entry = _make_coord_and_entry(MODEM_DATA_WITH_PASSTHROUGH, mock_runtime_data)
    entities = _create_data_dependent_entities(coord, entry, MODEM_DATA_WITH_PASSTHROUGH)

    # Base case has 20 entities (see test_create_data_dependent_entities).
    # MODEM_DATA_WITH_PASSTHROUGH adds 3 Tier 3 fields → 20 + 3 = 23.
    assert len(entities) == 23

    passthrough = [e for e in entities if isinstance(e, SystemInfoFieldSensor)]
    assert len(passthrough) == 3
    passthrough_fields = {s._field for s in passthrough}
    assert passthrough_fields == {"ds_scanning_status", "us_ranging_status", "dhcp_status"}


def test_create_data_dependent_entities_passthrough_sorted(mock_runtime_data):
    """Tier 3 dynamic sensors are created in sorted field order."""
    from custom_components.cable_modem_monitor.sensor import (
        _create_data_dependent_entities,
    )

    coord, entry = _make_coord_and_entry(MODEM_DATA_WITH_PASSTHROUGH, mock_runtime_data)
    entities = _create_data_dependent_entities(coord, entry, MODEM_DATA_WITH_PASSTHROUGH)

    passthrough = [e for e in entities if isinstance(e, SystemInfoFieldSensor)]
    fields = [s._field for s in passthrough]
    assert fields == sorted(fields)


def test_create_data_dependent_entities_display_only_not_minted(mock_runtime_data):
    """Display-only fields never become pass-through sensors."""
    from custom_components.cable_modem_monitor.sensor import (
        _create_data_dependent_entities,
    )

    modem_data: dict[str, Any] = {
        **MOCK_MODEM_DATA,
        "system_info": {
            **MOCK_MODEM_DATA["system_info"],
            "current_time": "Thu Jan 01 12:00:00 2026",
            "hardware_version": "V1.0",
            "model_name": "TPS-2000",
        },
    }
    coord, entry = _make_coord_and_entry(modem_data, mock_runtime_data)
    entities = _create_data_dependent_entities(coord, entry, modem_data)

    passthrough_fields = {e._field for e in entities if isinstance(e, SystemInfoFieldSensor)}
    assert passthrough_fields.isdisjoint({"current_time", "hardware_version", "model_name"})


def test_docsis_status_not_minted_as_passthrough(mock_runtime_data):
    """docsis_status is consumed by the Status sensor — no standalone sensor (#178).

    A genuine pass-through field still mints alongside it.
    """
    from custom_components.cable_modem_monitor.sensor import (
        _create_data_dependent_entities,
    )

    modem_data: dict[str, Any] = {
        "system_info": {
            "software_version": "1.0",
            "docsis_status": "operational",
            "dhcp_status": "success",
        },
        "downstream": [],
        "upstream": [],
    }
    coord, entry = _make_coord_and_entry(modem_data, mock_runtime_data)
    entities = _create_data_dependent_entities(coord, entry, modem_data)

    passthrough_fields = {e._field for e in entities if isinstance(e, SystemInfoFieldSensor)}
    assert "docsis_status" not in passthrough_fields
    assert "dhcp_status" in passthrough_fields  # a real pass-through still mints


# -----------------------------------------------------------------------
# Sticky availability — non-continuous data sensors hold value on outage
# (#178). One modem outage should log one line (Status -> Unreachable),
# not a fan-out of `-> Unavailable` lines from every non-continuous sensor.
# -----------------------------------------------------------------------


def _outage_snapshot() -> ModemSnapshot:
    """A poll where the modem is unreachable: modem_data is None."""
    return ModemSnapshot(
        connection_status=ConnectionStatus.UNREACHABLE,
        docsis_status=DocsisStatus.UNKNOWN,
        modem_data=None,
    )


def test_software_version_sticky_holds_value_and_stays_available(mock_runtime_data):
    """Software Version holds its last value and stays available on outage."""
    sensor = _make_sensor(ModemSoftwareVersionSensor, mock_runtime_data)
    assert sensor.native_value == "4502.9.016"
    assert sensor.available is True

    # A subsequent poll finds the modem unreachable (modem_data=None).
    sensor.coordinator.data = _outage_snapshot()
    vars(sensor).pop("native_value", None)

    assert sensor.native_value == "4502.9.016"  # held → no state change → no log line
    assert sensor.available is True


def test_software_version_unavailable_on_coordinator_failure(mock_runtime_data):
    """A hard coordinator failure still takes the sticky sensor unavailable."""
    sensor = _make_sensor(ModemSoftwareVersionSensor, mock_runtime_data)
    sensor.coordinator.last_update_success = False
    assert sensor.available is False


def test_last_boot_time_stays_available_on_outage(mock_runtime_data):
    """Last Boot Time stays available on outage (value already holds itself)."""
    sensor = _make_sensor(ModemLastBootTimeSensor, mock_runtime_data)
    assert sensor.available is True
    sensor.coordinator.data = _outage_snapshot()
    assert sensor.available is True


def test_system_info_field_noncontinuous_is_sticky(mock_runtime_data):
    """A non-continuous pass-through field holds value and stays available."""
    modem_data: dict[str, Any] = {
        "system_info": {"ds_scanning_status": "success"},
        "downstream": [],
        "upstream": [],
    }
    sensor = _make_sensor(SystemInfoFieldSensor, mock_runtime_data, modem_data=modem_data, field="ds_scanning_status")
    assert sensor.native_value == "success"
    assert sensor.available is True

    sensor.coordinator.data = _outage_snapshot()
    vars(sensor).pop("native_value", None)

    assert sensor.native_value == "success"  # held
    assert sensor.available is True


def test_system_info_field_continuous_not_sticky(mock_runtime_data):
    """A continuous pass-through field (unit/state_class) keeps graph gaps."""
    modem_data: dict[str, Any] = {
        "system_info": {"provisioned_speed_down": 110100480},
        "downstream": [],
        "upstream": [],
    }
    sensor = _make_sensor(
        SystemInfoFieldSensor, mock_runtime_data, modem_data=modem_data, field="provisioned_speed_down"
    )
    assert sensor.native_value == 110100480
    assert sensor.available is True

    sensor.coordinator.data = _outage_snapshot()
    vars(sensor).pop("native_value", None)

    assert sensor.native_value is None  # no holding — the gap is graph signal
    assert sensor.available is False  # continuous → unavailable on outage


def test_continuous_data_sensor_unavailable_on_outage(mock_runtime_data):
    """Continuous data sensors are untouched — they go unavailable on outage."""
    sensor = _make_sensor(ModemChannelCountSensor, mock_runtime_data, direction="downstream")
    assert sensor.available is True
    sensor.coordinator.data = _outage_snapshot()
    assert sensor.available is False


# -----------------------------------------------------------------------
# Deferred entity creation (UC-84)
# -----------------------------------------------------------------------


def test_deferred_creation_on_first_data(mock_runtime_data):
    """Deferred listener creates data entities on first successful poll.

    UC-84 steps 7-9: modem comes back, listener fires, entities created,
    listener unsubscribes.
    """
    from custom_components.cable_modem_monitor.sensor import (
        _register_deferred_entity_creation,
    )

    # Coordinator starts with modem_data=None (unreachable)
    coord = MagicMock()
    coord.data = ModemSnapshot(
        connection_status=ConnectionStatus.UNREACHABLE,
        docsis_status=DocsisStatus.NOT_LOCKED,
        modem_data=None,
    )

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = MOCK_ENTRY_DATA
    entry.runtime_data = mock_runtime_data

    add_entities = MagicMock()

    # Register deferred creation
    _register_deferred_entity_creation(coord, entry, add_entities)

    # Listener should be registered
    coord.async_add_listener.assert_called_once()
    listener_fn = coord.async_add_listener.call_args[0][0]

    # entry.async_on_unload should be called with the unsub callable
    entry.async_on_unload.assert_called_once()

    # Simulate coordinator update with valid data
    coord.data = ModemSnapshot(
        connection_status=ConnectionStatus.ONLINE,
        docsis_status=DocsisStatus.OPERATIONAL,
        modem_data=MOCK_MODEM_DATA,
    )
    listener_fn()

    # Entities should be added
    add_entities.assert_called_once()
    created = add_entities.call_args[0][0]
    assert len(created) == 20  # Same as test_create_data_dependent_entities


def test_deferred_creation_noop_while_no_data(mock_runtime_data):
    """Deferred listener does nothing when modem_data is still None.

    UC-84 step 5: subsequent polls still unreachable, listener fires
    but takes no action.
    """
    from custom_components.cable_modem_monitor.sensor import (
        _register_deferred_entity_creation,
    )

    coord = MagicMock()
    coord.data = ModemSnapshot(
        connection_status=ConnectionStatus.UNREACHABLE,
        docsis_status=DocsisStatus.NOT_LOCKED,
        modem_data=None,
    )

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = MOCK_ENTRY_DATA
    entry.runtime_data = mock_runtime_data

    add_entities = MagicMock()

    _register_deferred_entity_creation(coord, entry, add_entities)
    listener_fn = coord.async_add_listener.call_args[0][0]

    # Simulate update — still no data
    listener_fn()

    # async_add_entities should NOT be called
    add_entities.assert_not_called()


# -----------------------------------------------------------------------
# Sensor availability — coordinator failure and absent modem data
# -----------------------------------------------------------------------

# fmt: off
# ┌──────────────────────────┬─────────────────────┬──────────────────────────────────────────┐
# │ sensor_cls               │ condition           │ description                              │
# ├──────────────────────────┼─────────────────────┼──────────────────────────────────────────┤
# │ ModemSoftwareVersionSensor│ coord failed       │ ModemSensorBase: last_update_success=False│
# │ ModemInfoSensor          │ coord failed        │ ModemSensorBase subclass: coord failed   │
# │ ModemStatusSensor        │ coord failed        │ own available property: last_update_success│
# └──────────────────────────┴─────────────────────┴──────────────────────────────────────────┘
SENSOR_COORD_FAILED_CASES = [
    (ModemSoftwareVersionSensor, {}, "software_version"),
    (ModemInfoSensor,            {}, "modem_info"),
    (ModemStatusSensor,          {}, "modem_status"),
]
# fmt: on


@pytest.mark.parametrize(
    "sensor_cls,kwargs,desc",
    SENSOR_COORD_FAILED_CASES,
    ids=[c[2] for c in SENSOR_COORD_FAILED_CASES],
)
def test_sensor_unavailable_when_coordinator_fails(sensor_cls, kwargs, desc, mock_runtime_data):
    """Sensor reports unavailable when last_update_success is False."""
    coord, entry = _make_coord_and_entry(MOCK_MODEM_DATA, mock_runtime_data)
    coord.last_update_success = False
    sensor = sensor_cls(coord, entry, **kwargs)
    assert sensor.available is False


def test_modem_sensor_unavailable_when_no_modem_data(mock_runtime_data):
    """A continuous ModemSensorBase subclass is unavailable when modem_data is None.

    Sticky sensors deliberately stay available here — see the
    sticky-availability tests above.
    """
    coord, entry = _make_coord_and_entry(None, mock_runtime_data)
    sensor = ModemChannelCountSensor(coord, entry, direction="downstream")
    assert sensor.available is False


# -----------------------------------------------------------------------
# _SystemInfoSensor — modem_data=None returns empty dict
# -----------------------------------------------------------------------


def test_system_info_sensor_returns_none_when_no_modem_data(mock_runtime_data):
    """Channel count returns None when snapshot.modem_data is None."""
    coord, entry = _make_coord_and_entry(None, mock_runtime_data)
    sensor = ModemChannelCountSensor(coord, entry, direction="downstream")
    assert sensor.native_value is None


# -----------------------------------------------------------------------
# ModemLastBootTimeSensor — stats_last_reset fallback
# -----------------------------------------------------------------------


def test_last_boot_time_falls_back_to_stats_last_reset(mock_runtime_data):
    """native_value returns stats_last_reset when system_uptime is absent."""
    from datetime import UTC, datetime

    reset_time = datetime(2026, 1, 15, 8, 0, 0, tzinfo=UTC)
    snapshot = ModemSnapshot(
        connection_status=ConnectionStatus.ONLINE,
        docsis_status=DocsisStatus.OPERATIONAL,
        modem_data={"system_info": {}, "downstream": [], "upstream": []},
        stats_last_reset=reset_time,
    )
    coord = MagicMock()
    coord.data = snapshot
    coord.last_update_success = True

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = MOCK_ENTRY_DATA
    entry.runtime_data = mock_runtime_data

    sensor = ModemLastBootTimeSensor(coord, entry)
    assert sensor.native_value == reset_time


def test_last_boot_time_returns_none_when_no_uptime_or_reset(mock_runtime_data):
    """native_value returns None when both system_uptime and stats_last_reset are absent."""
    snapshot = ModemSnapshot(
        connection_status=ConnectionStatus.ONLINE,
        docsis_status=DocsisStatus.OPERATIONAL,
        modem_data={"system_info": {}, "downstream": [], "upstream": []},
    )
    coord = MagicMock()
    coord.data = snapshot
    coord.last_update_success = True

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = MOCK_ENTRY_DATA
    entry.runtime_data = mock_runtime_data

    sensor = ModemLastBootTimeSensor(coord, entry)
    assert sensor.native_value is None


# -----------------------------------------------------------------------
# ModemLastBootTimeSensor — jitter tolerance (only real reboots move it)
# -----------------------------------------------------------------------


def _boot_time_poll(sensor, coord, uptime, stats_last_reset=None):
    """Advance the sensor one poll: swap the snapshot and invalidate the cache."""
    system_info = {} if uptime is None else {"system_uptime": uptime}
    coord.data = ModemSnapshot(
        connection_status=ConnectionStatus.ONLINE,
        docsis_status=DocsisStatus.OPERATIONAL,
        modem_data={"system_info": system_info, "downstream": [], "upstream": []},
        stats_last_reset=stats_last_reset,
    )
    sensor.__dict__.pop("native_value", None)
    return sensor.native_value


def test_last_boot_time_stable_across_poll_jitter(mock_runtime_data):
    """A few seconds of poll-timing jitter must not move the emitted boot time.

    Uptime keeps increasing between polls (it can never decrease
    without a reboot); jitter shows up as the computed timestamp
    wobbling because the increase doesn't exactly match wall time.
    """
    coord, entry = _make_coord_and_entry(MOCK_MODEM_DATA, mock_runtime_data)
    sensor = ModemLastBootTimeSensor(coord, entry)

    first = _boot_time_poll(sensor, coord, "86400")
    second = _boot_time_poll(sensor, coord, "86410")

    assert first is not None
    assert second == first


def test_last_boot_time_updates_on_real_reboot(mock_runtime_data):
    """A large uptime drop (real reboot) replaces the emitted boot time."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    coord, entry = _make_coord_and_entry(MOCK_MODEM_DATA, mock_runtime_data)
    sensor = ModemLastBootTimeSensor(coord, entry)

    first = _boot_time_poll(sensor, coord, "86400")
    second = _boot_time_poll(sensor, coord, "60")

    assert first is not None
    assert second is not None
    assert second != first
    assert second - first > timedelta(hours=23)
    assert abs(second - (dt_util.now() - timedelta(seconds=60))) < timedelta(minutes=1)


def test_last_boot_time_double_reboot_within_tolerance(mock_runtime_data):
    """An uptime decrease is reboot evidence even inside the jitter tolerance."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    coord, entry = _make_coord_and_entry(MOCK_MODEM_DATA, mock_runtime_data)
    sensor = ModemLastBootTimeSensor(coord, entry)

    _boot_time_poll(sensor, coord, "86400")
    first_reboot = _boot_time_poll(sensor, coord, "300")
    # Second reboot 4 minutes after the first: timestamps land inside
    # the tolerance, but uptime dropped 300 → 60.
    second_reboot = _boot_time_poll(sensor, coord, "60")

    assert first_reboot is not None
    assert second_reboot is not None
    assert second_reboot != first_reboot
    assert abs(second_reboot - (dt_util.now() - timedelta(seconds=60))) < timedelta(minutes=1)


def test_last_boot_time_keeps_restored_value_when_sources_absent(mock_runtime_data):
    """A restored boot time survives polls that carry no uptime or reset data."""
    from datetime import UTC, datetime

    coord, entry = _make_coord_and_entry(MOCK_MODEM_DATA, mock_runtime_data)
    sensor = ModemLastBootTimeSensor(coord, entry)
    restored = datetime(2026, 6, 1, 3, 0, 0, tzinfo=UTC)
    sensor._stable_boot_time = restored

    assert _boot_time_poll(sensor, coord, None) == restored


def test_last_boot_time_counter_reset_respects_tolerance(mock_runtime_data):
    """A stats_last_reset within tolerance of the held value does not rewrite it."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    coord, entry = _make_coord_and_entry(MOCK_MODEM_DATA, mock_runtime_data)
    sensor = ModemLastBootTimeSensor(coord, entry)

    held = dt_util.now() - timedelta(days=2)
    sensor._stable_boot_time = held
    nearby_reset = held + timedelta(seconds=30)

    assert _boot_time_poll(sensor, coord, None, stats_last_reset=nearby_reset) == held


# -----------------------------------------------------------------------
# ChannelSensor — modem_data=None paths
# -----------------------------------------------------------------------


def test_channel_sensor_native_value_none_when_no_modem_data(mock_runtime_data):
    """ChannelSensor.native_value is None when snapshot.modem_data is None."""
    coord, entry = _make_coord_and_entry(MOCK_MODEM_DATA, mock_runtime_data)
    sensors = _create_channel_sensors(coord, entry, MOCK_MODEM_DATA)
    assert sensors, "need at least one channel sensor for this test"

    sensor = sensors[0]
    # Replace coordinator data with a None-modem_data snapshot
    coord.data = ModemSnapshot(
        connection_status=ConnectionStatus.UNREACHABLE,
        docsis_status=DocsisStatus.NOT_LOCKED,
        modem_data=None,
    )
    assert sensor.native_value is None


def test_channel_sensor_attributes_empty_when_no_modem_data(mock_runtime_data):
    """ChannelSensor.extra_state_attributes is {} when snapshot.modem_data is None."""
    coord, entry = _make_coord_and_entry(MOCK_MODEM_DATA, mock_runtime_data)
    sensors = _create_channel_sensors(coord, entry, MOCK_MODEM_DATA)
    sensor = sensors[0]

    coord.data = ModemSnapshot(
        connection_status=ConnectionStatus.UNREACHABLE,
        docsis_status=DocsisStatus.NOT_LOCKED,
        modem_data=None,
    )
    assert sensor.extra_state_attributes == {}


# -----------------------------------------------------------------------
# LanStatsSensor — None paths
# -----------------------------------------------------------------------
# fmt: off
# ┌──────────────────┬────────────────────┬──────────────────────────────────────┐
# │ lan_stats        │ expected           │ description                          │
# ├──────────────────┼────────────────────┼──────────────────────────────────────┤
# │ absent key       │ None               │ interface not in lan_stats dict      │
# │ modem_data=None  │ None               │ snapshot has no modem_data           │
# └──────────────────┴────────────────────┴──────────────────────────────────────┘
# fmt: on


def _make_lan_sensor(modem_data, mock_runtime_data):
    """Build a LanStatsSensor wired to the given modem_data snapshot."""
    coord, entry = _make_coord_and_entry(modem_data, mock_runtime_data)
    return LanStatsSensor(
        coord,
        entry,
        interface="eth0",
        field="received_bytes",
        name_suffix="Received Bytes",
        device_class=None,
        unit=None,
    )


def test_lan_sensor_none_when_interface_absent(mock_runtime_data):
    """native_value is None when the interface key is absent from lan_stats."""
    modem_data = {**MOCK_MODEM_DATA, "lan_stats": {"eth1": {"received_bytes": 100}}}
    sensor = _make_lan_sensor(modem_data, mock_runtime_data)
    assert sensor.native_value is None


def test_lan_sensor_none_when_no_modem_data(mock_runtime_data):
    """native_value is None when snapshot.modem_data is None."""
    sensor = _make_lan_sensor(None, mock_runtime_data)
    assert sensor.native_value is None


def test_deferred_creation_cleanup_on_unload(mock_runtime_data):
    """Deferred listener is cleaned up when entry unloads.

    UC-84 assertion: if consumer unloads before modem recovers,
    listener is cleaned up.
    """
    from custom_components.cable_modem_monitor.sensor import (
        _register_deferred_entity_creation,
    )

    unsub_fn = MagicMock()

    coord = MagicMock()
    coord.data = ModemSnapshot(
        connection_status=ConnectionStatus.UNREACHABLE,
        docsis_status=DocsisStatus.NOT_LOCKED,
        modem_data=None,
    )
    coord.async_add_listener.return_value = unsub_fn

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = MOCK_ENTRY_DATA
    entry.runtime_data = mock_runtime_data

    add_entities = MagicMock()

    _register_deferred_entity_creation(coord, entry, add_entities)

    # The unsub callable should be registered for cleanup
    entry.async_on_unload.assert_called_once_with(unsub_fn)


# -----------------------------------------------------------------------
# Deferred entity re-notification (UC-84 step 9a)
# -----------------------------------------------------------------------


def test_deferred_creation_schedules_re_notification(mock_runtime_data):
    """Deferred listener schedules a re-notification task after entity creation.

    UC-84 step 9a: after async_add_entities, a delayed task is scheduled
    that calls async_set_updated_data to ensure deferred entities receive
    _handle_coordinator_update() after their coordinator listeners are
    registered.
    """
    from custom_components.cable_modem_monitor.sensor import (
        _register_deferred_entity_creation,
    )

    coord = MagicMock()
    coord.data = ModemSnapshot(
        connection_status=ConnectionStatus.UNREACHABLE,
        docsis_status=DocsisStatus.NOT_LOCKED,
        modem_data=None,
    )

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = MOCK_ENTRY_DATA
    entry.runtime_data = mock_runtime_data

    add_entities = MagicMock()

    _register_deferred_entity_creation(coord, entry, add_entities)
    listener_fn = coord.async_add_listener.call_args[0][0]

    # Simulate coordinator update with valid data
    coord.data = ModemSnapshot(
        connection_status=ConnectionStatus.ONLINE,
        docsis_status=DocsisStatus.OPERATIONAL,
        modem_data=MOCK_MODEM_DATA,
    )
    listener_fn()

    # Re-notification task should be scheduled
    coord.hass.async_create_task.assert_called_once()
    task_name = coord.hass.async_create_task.call_args[0][1]
    assert task_name == "cable_modem_deferred_entity_state"


@pytest.mark.asyncio
async def test_deferred_re_notification_fires_coordinator_listeners(
    mock_runtime_data,
):
    """Scheduled re-notification fans out to coordinator listeners.

    The coroutine body waits for entity registration to complete, then
    fires `async_update_listeners()` so newly-registered entities receive
    `_handle_coordinator_update()` against the current snapshot. Using
    `async_update_listeners()` (rather than `async_set_updated_data()`)
    avoids resetting the refresh timer and the misleading "Manually
    updated" DEBUG log — we are not updating data, only re-fanning it.
    """
    from unittest.mock import AsyncMock, patch

    from custom_components.cable_modem_monitor.sensor import (
        _register_deferred_entity_creation,
    )

    snapshot = ModemSnapshot(
        connection_status=ConnectionStatus.ONLINE,
        docsis_status=DocsisStatus.OPERATIONAL,
        modem_data=MOCK_MODEM_DATA,
    )

    coord = MagicMock()
    coord.data = ModemSnapshot(
        connection_status=ConnectionStatus.UNREACHABLE,
        docsis_status=DocsisStatus.NOT_LOCKED,
        modem_data=None,
    )

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = MOCK_ENTRY_DATA
    entry.runtime_data = mock_runtime_data

    add_entities = MagicMock()

    _register_deferred_entity_creation(coord, entry, add_entities)
    listener_fn = coord.async_add_listener.call_args[0][0]

    # Simulate data arrival
    coord.data = snapshot
    listener_fn()

    # Capture the coroutine passed to async_create_task
    coro = coord.hass.async_create_task.call_args[0][0]

    # Await the coroutine with sleep patched out
    with patch(
        "custom_components.cable_modem_monitor.sensor.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        _ = await coro

    coord.async_update_listeners.assert_called_once_with()
    coord.async_set_updated_data.assert_not_called()


def test_deferred_re_notification_not_scheduled_when_no_data(mock_runtime_data):
    """No re-notification when modem_data is still None.

    UC-84 step 5: listener fires but modem_data=None, so no entities
    are created and no re-notification is scheduled.
    """
    from custom_components.cable_modem_monitor.sensor import (
        _register_deferred_entity_creation,
    )

    coord = MagicMock()
    coord.data = ModemSnapshot(
        connection_status=ConnectionStatus.UNREACHABLE,
        docsis_status=DocsisStatus.NOT_LOCKED,
        modem_data=None,
    )

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = MOCK_ENTRY_DATA
    entry.runtime_data = mock_runtime_data

    add_entities = MagicMock()

    _register_deferred_entity_creation(coord, entry, add_entities)
    listener_fn = coord.async_add_listener.call_args[0][0]

    # Simulate update — still no data
    listener_fn()

    add_entities.assert_not_called()
    coord.hass.async_create_task.assert_not_called()


# -----------------------------------------------------------------------
# async_setup_entry — sensor platform wiring
# -----------------------------------------------------------------------


def _setup_entry_inputs(
    mock_runtime_data,
    *,
    modem_data: dict[str, Any] | None = MOCK_MODEM_DATA,
    has_health: bool = True,
    icmp: bool = True,
    head: bool = True,
):
    """Build (hass, entry, add_entities) for an async_setup_entry call."""
    snapshot = (
        ModemSnapshot(
            connection_status=ConnectionStatus.ONLINE,
            docsis_status=DocsisStatus.OPERATIONAL,
            modem_data=modem_data,
        )
        if modem_data is not None
        else ModemSnapshot(
            connection_status=ConnectionStatus.UNREACHABLE,
            docsis_status=DocsisStatus.NOT_LOCKED,
            modem_data=None,
        )
    )
    mock_runtime_data.data_coordinator.data = snapshot
    if not has_health:
        mock_runtime_data.health_coordinator = None

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {
        **MOCK_ENTRY_DATA,
        "supports_icmp": icmp,
        "supports_head": head,
    }
    entry.runtime_data = mock_runtime_data

    hass = MagicMock()
    add_entities = MagicMock()
    return hass, entry, add_entities


async def test_async_setup_entry_happy_path(mock_runtime_data) -> None:
    """All sensors created when modem_data and health_coord are present."""
    from custom_components.cable_modem_monitor.sensor import (
        HttpLatencySensor,
        ModemInfoSensor,
        ModemStatusSensor,
        PingLatencySensor,
        TcpLatencySensor,
        async_setup_entry,
    )

    hass, entry, add_entities = _setup_entry_inputs(mock_runtime_data)

    await async_setup_entry(hass, entry, add_entities)

    add_entities.assert_called_once()
    entities = add_entities.call_args[0][0]
    types = {type(e) for e in entities}

    # Always-created
    assert ModemStatusSensor in types
    assert ModemInfoSensor in types
    # Health sensors (ICMP + HEAD enabled)
    assert TcpLatencySensor in types
    assert PingLatencySensor in types
    assert HttpLatencySensor in types
    # Data-dependent entities present (channel sensors, system_info, etc.)
    assert len(entities) > 5


async def test_async_setup_entry_no_health_coord(mock_runtime_data) -> None:
    """No latency sensors created when health_coordinator is None."""
    from custom_components.cable_modem_monitor.sensor import (
        HttpLatencySensor,
        PingLatencySensor,
        TcpLatencySensor,
        async_setup_entry,
    )

    hass, entry, add_entities = _setup_entry_inputs(mock_runtime_data, has_health=False)

    await async_setup_entry(hass, entry, add_entities)

    add_entities.assert_called_once()
    entities = add_entities.call_args[0][0]
    types = {type(e) for e in entities}

    assert TcpLatencySensor not in types
    assert PingLatencySensor not in types
    assert HttpLatencySensor not in types


async def test_async_setup_entry_defers_when_no_modem_data(
    mock_runtime_data,
) -> None:
    """No modem_data on first poll defers data-dependent entities."""
    from unittest.mock import patch

    from custom_components.cable_modem_monitor.sensor import (
        ModemInfoSensor,
        ModemStatusSensor,
        async_setup_entry,
    )

    hass, entry, add_entities = _setup_entry_inputs(mock_runtime_data, modem_data=None)

    with patch("custom_components.cable_modem_monitor.sensor." "_register_deferred_entity_creation") as mock_register:
        await async_setup_entry(hass, entry, add_entities)

    # First call: only always-created + health sensors, no data-dependent
    add_entities.assert_called_once()
    entities = add_entities.call_args[0][0]
    types = {type(e) for e in entities}
    assert ModemStatusSensor in types
    assert ModemInfoSensor in types
    # No ChannelSensor (would only appear with modem_data)
    from custom_components.cable_modem_monitor.sensor import ChannelSensor

    assert ChannelSensor not in types

    # Deferred registration was set up
    mock_register.assert_called_once_with(mock_runtime_data.data_coordinator, entry, add_entities)
