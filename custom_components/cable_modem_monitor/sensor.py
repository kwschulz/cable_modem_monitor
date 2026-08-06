"""Sensor platform for Cable Modem Monitor.

Creates Home Assistant sensor entities from Core's ModemSnapshot and
HealthInfo types.  All modem-specific logic lives in Core — this module
is pure HA presentation.

Entity types:
    - Status: 10-level priority cascade over connection/health/DOCSIS
    - Modem Info: static metadata from ModemIdentity
    - System: channel counts, error totals, software version, uptime
    - Per-channel: power, SNR, frequency, corrected/uncorrected
    - LAN stats: bytes, packets, errors, drops per interface
    - Health: ICMP and HTTP latency (from health coordinator)

See ENTITY_MODEL_SPEC.md for the full entity catalog.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from datetime import datetime, timedelta
from typing import Any, NamedTuple

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import dt as dt_util
from solentlabs.cable_modem_monitor_core.orchestration.models import (
    HealthInfo,
    ModemSnapshot,
)
from solentlabs.cable_modem_monitor_core.orchestration.signals import (
    ConnectionStatus,
    DocsisStatus,
    HealthStatus,
)

from .const import (
    CONF_CHANNEL_IDENTITY,
    CONF_ENTITY_PREFIX,
    CONF_SUPPORTS_HEAD,
    CONF_SUPPORTS_ICMP,
    CONSUMED_SYSTEM_INFO_FIELDS,
    DISPLAY_ONLY_SYSTEM_INFO_FIELDS,
    DOMAIN,
    ChannelIdentity,
)
from .coordinator import CableModemConfigEntry
from .lib.utils import get_device_name, parse_uptime_to_seconds
from .mapping_manager import SlotKey, build_channel_map

_LOGGER = logging.getLogger(__name__)

# Coordinator-backed entities do no per-entity I/O — no throttling needed.
PARALLEL_UPDATES = 0


# ------------------------------------------------------------------
# Status cascade
# ------------------------------------------------------------------


_DOCSIS_DISPLAY: dict[str, str] = {
    DocsisStatus.NOT_LOCKED: "Not Locked",
    DocsisStatus.PARTIAL_LOCK: "Partial Lock",
}


def _compute_display_status(
    connection: ConnectionStatus,
    health: HealthStatus | None,
    docsis: str,
) -> str:
    """Apply the 10-level priority cascade to derive display state.

    Pure lookup — no business logic.  Core derives the three input
    values; this function maps them to a human-readable string.

    See ENTITY_MODEL_SPEC.md § Status Sensor.
    """
    effective_health = health or HealthStatus.UNKNOWN

    if effective_health == HealthStatus.UNRESPONSIVE:
        return "Unresponsive"
    if connection == ConnectionStatus.UNREACHABLE:
        return "Unreachable"
    if connection == ConnectionStatus.AUTH_FAILED:
        return "Auth Failed"
    if effective_health == HealthStatus.DEGRADED:
        return "Degraded"
    if connection == ConnectionStatus.PARSER_ISSUE:
        return "Parser Error"
    if connection == ConnectionStatus.NO_SIGNAL:
        return "No Signal"
    docsis_label = _DOCSIS_DISPLAY.get(docsis)
    if docsis_label:
        return docsis_label
    if effective_health == HealthStatus.ICMP_BLOCKED:
        return "ICMP Blocked"
    if docsis in (DocsisStatus.OPERATIONAL, DocsisStatus.UNKNOWN):
        return "Operational"
    return docsis.replace("_", " ").title()


_DIAGNOSIS_MAP: dict[HealthStatus, str] = {
    HealthStatus.RESPONSIVE: "Modem is responsive to health probes",
    HealthStatus.DEGRADED: ("Modem responds to ICMP but HTTP is failing" " — web server may be hung"),
    HealthStatus.ICMP_BLOCKED: ("HTTP works but ICMP is blocked" " — network may filter ping"),
    HealthStatus.UNRESPONSIVE: ("Modem is not responding to any health probes"),
}


def _derive_diagnosis(health: HealthStatus | None) -> str:
    """Derive a human-readable diagnosis from health status."""
    if health is None:
        return ""
    return _DIAGNOSIS_MAP.get(health, "")


# ------------------------------------------------------------------
# Channel metric definitions (table-driven entity creation)
# ------------------------------------------------------------------

# Aliases for table readability
_M = SensorStateClass.MEASUREMENT
_TI = SensorStateClass.TOTAL_INCREASING
_FREQ = SensorDeviceClass.FREQUENCY
_DSIZE = SensorDeviceClass.DATA_SIZE
_DRATE = SensorDeviceClass.DATA_RATE

# Each tuple: (field, name_suffix, unit, device_class, state_class, icon, value_type)
# fmt: off
_DS_METRICS = [
    ("power",       "Power",       "dBmV", None,  _M,  "mdi:signal",        float),
    ("snr",         "SNR",         "dB",   None,  _M,  "mdi:signal-variant", float),
    ("frequency",   "Frequency",   "Hz",   _FREQ, _M,  "mdi:sine-wave",     int),
    ("corrected",   "Corrected",   None,   None,  _TI, "mdi:check-circle",  int),
    ("uncorrected", "Uncorrected", None,   None,  _TI, "mdi:alert-circle",  int),
]

_US_METRICS = [
    ("power",     "Power",     "dBmV", None,  _M, "mdi:signal",    float),
    ("frequency", "Frequency", "Hz",   _FREQ, _M, "mdi:sine-wave", int),
]

_LAN_METRICS = [
    ("received_bytes",      "Received Bytes",      _DSIZE, "B"),
    ("received_packets",    "Received Packets",    None,   None),
    ("received_errors",     "Received Errors",     None,   None),
    ("received_drops",      "Received Drops",      None,   None),
    ("transmitted_bytes",   "Transmitted Bytes",   _DSIZE, "B"),
    ("transmitted_packets", "Transmitted Packets", None,   None),
    ("transmitted_errors",  "Transmitted Errors",  None,   None),
    ("transmitted_drops",   "Transmitted Drops",   None,   None),
]
# fmt: on

# Power and SNR are always created for downstream; power for upstream.
# Other metrics are created only when the field is present on the channel.
_DS_ALWAYS_FIELDS = frozenset(("power", "snr"))
_US_ALWAYS_FIELDS = frozenset(("power",))


class _FieldUnitMeta(NamedTuple):
    """HA unit metadata for a continuous system_info field."""

    native_unit: str
    device_class: SensorDeviceClass | None
    state_class: SensorStateClass | None
    suggested_unit: str | None = None


# Units and device classes for dynamic system_info fields.
# Fields not listed here display as unitless strings.
_SYSTEM_INFO_FIELD_UNITS: dict[str, _FieldUnitMeta] = {
    # HA applies no default scaling to DATA_RATE, so the raw bit/s these
    # store renders as "110100480 bit/s" without a suggested unit.
    "provisioned_speed_down": _FieldUnitMeta("bit/s", _DRATE, _M, suggested_unit="Mbit/s"),
    "provisioned_speed_up": _FieldUnitMeta("bit/s", _DRATE, _M, suggested_unit="Mbit/s"),
    "provisioned_burst_down": _FieldUnitMeta("B", _DSIZE, _M),
    "provisioned_burst_up": _FieldUnitMeta("B", _DSIZE, _M),
}

# Abbreviations uppercased in humanized field names.
_SYSINFO_ABBREVIATIONS = frozenset(
    {
        "ds",
        "us",
        "dhcp",
        "tftp",
        "ip",
        "mac",
        "snr",
        "fft",
        "ofdm",
    }
)


def _humanize_field_name(field: str) -> str:
    """Convert snake_case field to Title Case, uppercasing known abbreviations.

    Examples: ``ds_scanning_status`` → ``DS Scanning Status``,
    ``dhcp_status`` → ``DHCP Status``.
    """
    return " ".join(w.upper() if w in _SYSINFO_ABBREVIATIONS else w.title() for w in field.split("_"))


# ------------------------------------------------------------------
# Base classes
# ------------------------------------------------------------------


class ModemSensorBase(
    CoordinatorEntity[DataUpdateCoordinator[ModemSnapshot]],
    SensorEntity,
):
    """Base class for sensors that read from the data coordinator."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[ModemSnapshot],
        entry: CableModemConfigEntry,
    ) -> None:
        """Initialize with data coordinator and config entry."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=get_device_name(
                entry.data.get(CONF_ENTITY_PREFIX, "default"),
                model=entry.runtime_data.modem_identity.model,
                host=entry.data.get("host", ""),
            ),
        )

    @property
    def _snapshot(self) -> ModemSnapshot:
        """Current snapshot from the data coordinator."""
        return self.coordinator.data

    @property
    def available(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Available when coordinator succeeds and modem_data is present."""
        return self.coordinator.last_update_success and self._snapshot.modem_data is not None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Invalidate cached properties before writing state."""
        d: dict[str, Any] = self.__dict__  # type: ignore[assignment]
        d.pop("native_value", None)
        d.pop("extra_state_attributes", None)
        super()._handle_coordinator_update()


class _StickyAvailabilityMixin:
    """Always available while the coordinator is alive.

    Non-continuous data sensors (no unit/state_class) drive the HA
    logbook, so a modem outage's ``-> Unavailable`` transition logs a
    spurious line. Staying available — and holding the last value (see
    _HoldsLastValueMixin) — keeps them silent so the Status sensor is
    the sole outage announcer (#178). See ENTITY_MODEL_SPEC § Availability.
    """

    coordinator: DataUpdateCoordinator[ModemSnapshot]

    @property
    def available(self) -> bool:
        """Available whenever the coordinator's last update succeeded."""
        return self.coordinator.last_update_success


class _HoldsLastValueMixin:
    """Holds the last non-None native value across transient data gaps."""

    _held_value: Any = None

    def _hold_last(self, current: Any) -> Any:
        """Return current, or the last non-None value when current is None."""
        if current is not None:
            self._held_value = current
        return self._held_value


class HealthSensorBase(  # pyright: ignore[reportIncompatibleVariableOverride]
    CoordinatorEntity[DataUpdateCoordinator[HealthInfo]],
    SensorEntity,
):
    """Base class for sensors that read from the health coordinator."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[HealthInfo],
        entry: CableModemConfigEntry,
    ) -> None:
        """Initialize with health coordinator and config entry."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=get_device_name(
                entry.data.get(CONF_ENTITY_PREFIX, "default"),
                model=entry.runtime_data.modem_identity.model,
                host=entry.data.get("host", ""),
            ),
        )

    @property
    def _health_info(self) -> HealthInfo:
        """Current health info from the health coordinator."""
        return self.coordinator.data

    @callback
    def _handle_coordinator_update(self) -> None:
        """Invalidate cached properties before writing state."""
        d: dict[str, Any] = self.__dict__  # type: ignore[assignment]
        d.pop("native_value", None)
        super()._handle_coordinator_update()


# ------------------------------------------------------------------
# System sensors
# ------------------------------------------------------------------


class ModemStatusSensor(ModemSensorBase):
    """Unified status sensor with 10-level priority cascade.

    Listens to both the data coordinator (10min poll) and the health
    coordinator (30s probes) so status reflects health changes in
    near-real-time.

    See ENTITY_MODEL_SPEC.md § Status Sensor.
    """

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[ModemSnapshot],
        entry: CableModemConfigEntry,
    ) -> None:
        """Initialize the status sensor."""
        super().__init__(coordinator, entry)
        self._attr_name = "Status"
        self._attr_unique_id = f"{entry.entry_id}_cable_modem_status"
        self._attr_icon = "mdi:router-network"
        self._health_coordinator: DataUpdateCoordinator[HealthInfo] | None = entry.runtime_data.health_coordinator

    async def async_added_to_hass(self) -> None:
        """Subscribe to both data and health coordinators."""
        await super().async_added_to_hass()
        if self._health_coordinator is not None:
            self.async_on_remove(
                self._health_coordinator.async_add_listener(
                    self._handle_health_update,
                )
            )

    @callback
    def _handle_health_update(self) -> None:
        """Re-render when health coordinator updates."""
        d: dict[str, Any] = self.__dict__  # type: ignore[assignment]
        d.pop("native_value", None)
        d.pop("extra_state_attributes", None)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Status sensor is always available to show current state."""
        return self.coordinator.last_update_success

    @property
    def _latest_health_status(self) -> HealthStatus | None:
        """Return the freshest health status from either source.

        Prefers the health coordinator (30s updates) over the snapshot
        (10min updates) so status reflects health changes promptly.
        """
        if self._health_coordinator is not None and self._health_coordinator.data is not None:
            health_info: HealthInfo = self._health_coordinator.data
            return health_info.health_status
        snapshot = self._snapshot
        return snapshot.health_info.health_status if snapshot.health_info else None

    @functools.cached_property
    def native_value(self) -> str:
        """Return display state from the priority cascade."""
        snapshot = self._snapshot
        return _compute_display_status(
            snapshot.connection_status,
            self._latest_health_status,
            snapshot.docsis_status,
        )

    @functools.cached_property
    def extra_state_attributes(self) -> dict[str, str]:
        """Return connection, health, DOCSIS status and diagnosis."""
        snapshot = self._snapshot
        health_status = self._latest_health_status
        return {
            "connection_status": snapshot.connection_status.value,
            "health_status": (health_status.value if health_status else "unknown"),
            "docsis_status": snapshot.docsis_status,
            "diagnosis": _derive_diagnosis(health_status),
        }


class ModemInfoSensor(ModemSensorBase):
    """Static modem metadata from ModemIdentity.

    State is the detected model.  Attributes pass through all
    ModemIdentity fields.  Always available — reads from runtime_data,
    not live poll data.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[ModemSnapshot],
        entry: CableModemConfigEntry,
    ) -> None:
        """Initialize the modem info sensor."""
        super().__init__(coordinator, entry)
        self._attr_name = "Modem Info"
        self._attr_unique_id = f"{entry.entry_id}_cable_modem_info"
        self._attr_icon = "mdi:information-outline"

    @property
    def available(self) -> bool:
        """Always available — reads static identity, not live data."""
        return self.coordinator.last_update_success

    @functools.cached_property
    def native_value(self) -> str:
        """Return the detected model name."""
        return self._entry.runtime_data.modem_identity.model

    @functools.cached_property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Return ModemIdentity fields as attributes."""
        identity = self._entry.runtime_data.modem_identity
        return {
            "manufacturer": identity.manufacturer,
            "model": identity.model,
            "release_date": identity.release_date,
            "docsis_version": identity.docsis_version,
            "status": identity.status,
        }


# ------------------------------------------------------------------
# System info sensors
# ------------------------------------------------------------------


class _SystemInfoSensor(ModemSensorBase):
    """Base for sensors that read from modem_data system_info."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def _get_system_info(self) -> dict[str, Any]:
        """Return system_info dict, or empty dict if unavailable."""
        modem_data = self._snapshot.modem_data
        if modem_data is None:
            return {}
        return modem_data.get("system_info", {})  # type: ignore[no-any-return]


class ModemChannelCountSensor(_SystemInfoSensor):
    """Channel count sensor (downstream or upstream)."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[ModemSnapshot],
        entry: CableModemConfigEntry,
        *,
        direction: str,
    ) -> None:
        """Initialize the channel count sensor."""
        super().__init__(coordinator, entry)
        label = "DS" if direction == "downstream" else "US"
        self._field = f"{direction}_channel_count"
        self._attr_name = f"{label} Channel Count"
        self._attr_unique_id = f"{entry.entry_id}_cable_modem_{direction}_channel_count"
        self._attr_icon = "mdi:numeric"
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @functools.cached_property
    def native_value(self) -> int | None:
        """Return the channel count."""
        value = self._get_system_info().get(self._field)
        return int(value) if value is not None else None


class ModemErrorTotalSensor(_SystemInfoSensor):
    """Total corrected or uncorrected error sensor."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[ModemSnapshot],
        entry: CableModemConfigEntry,
        *,
        error_type: str,
    ) -> None:
        """Initialize the error total sensor."""
        super().__init__(coordinator, entry)
        self._field = f"total_{error_type}"
        label = error_type.title()
        self._attr_name = f"Total {label} Errors"
        self._attr_unique_id = f"{entry.entry_id}_cable_modem_total_{error_type}"
        self._attr_icon = "mdi:alert-circle-check" if error_type == "corrected" else "mdi:alert-circle"
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING

    @functools.cached_property
    def native_value(self) -> int | None:
        """Return the error total."""
        value = self._get_system_info().get(self._field)
        return int(value) if value is not None else None


class ModemErrorRateSensor(_SystemInfoSensor):
    """Per-minute corrected or uncorrected error rate sensor (#164)."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[ModemSnapshot],
        entry: CableModemConfigEntry,
        *,
        error_type: str,
    ) -> None:
        """Initialize the error rate sensor."""
        super().__init__(coordinator, entry)
        self._field = f"rate_{error_type}"
        label = error_type.title()
        self._attr_name = f"Rate {label} Errors"
        self._attr_unique_id = f"{entry.entry_id}_cable_modem_rate_{error_type}"
        self._attr_icon = "mdi:alert-circle-check" if error_type == "corrected" else "mdi:alert-circle"
        self._attr_native_unit_of_measurement = "errors/min"
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @functools.cached_property
    def native_value(self) -> float | None:
        """Return the error rate (errors/min)."""
        value = self._get_system_info().get(self._field)
        return float(value) if value is not None else None


class ModemSoftwareVersionSensor(_StickyAvailabilityMixin, _HoldsLastValueMixin, _SystemInfoSensor):
    """Software version sensor — sticky, so an outage doesn't log; a firmware push still does (#178)."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[ModemSnapshot],
        entry: CableModemConfigEntry,
    ) -> None:
        """Initialize the software version sensor."""
        super().__init__(coordinator, entry)
        self._attr_name = "Software Version"
        self._attr_unique_id = f"{entry.entry_id}_cable_modem_software_version"
        self._attr_icon = "mdi:information-outline"

    @functools.cached_property
    def native_value(self) -> str | None:
        """Return the software version string, held across data gaps."""
        return self._hold_last(self._get_system_info().get("software_version"))


# now() - uptime jitters by poll timing and uptime reporting granularity;
# a real reboot moves the boot time by the entire prior uptime. 5 minutes
# sits far above the jitter and far below any reboot delta (#178).
_BOOT_TIME_JITTER_TOLERANCE = timedelta(minutes=5)


class ModemLastBootTimeSensor(  # pyright: ignore[reportIncompatibleVariableOverride]  # rationale: the mixin's `available` property intentionally overrides Entity's attribute; combining it with RestoreSensor in the MRO raises the variance complaint at the class level, matching HealthSensorBase
    _StickyAvailabilityMixin,
    _SystemInfoSensor,
    RestoreSensor,
):
    """Last boot time (derived from system uptime or counter reset).

    Priority: native system_uptime from modem > counter-reset detection
    from the orchestrator. Counter-reset provides a proxy for modems
    that don't report uptime (see #110).

    Sticky availability only (#178): _stable_boot_time already holds
    the value, so the value-holding mixin isn't needed.
    """

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[ModemSnapshot],
        entry: CableModemConfigEntry,
    ) -> None:
        """Initialize the last boot time sensor."""
        super().__init__(coordinator, entry)
        self._attr_name = "Last Boot Time"
        self._attr_unique_id = f"{entry.entry_id}_cable_modem_last_boot_time"
        self._attr_icon = "mdi:restart"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._stable_boot_time: datetime | None = None
        self._last_uptime_seconds: int | None = None

    async def async_added_to_hass(self) -> None:
        """Seed the held boot time from the last recorded state (HA restart)."""
        await super().async_added_to_hass()
        data = await self.async_get_last_sensor_data()
        if data is not None:
            restored: Any = data.native_value
            if isinstance(restored, str):
                restored = dt_util.parse_datetime(restored)
            if isinstance(restored, datetime):
                self._stable_boot_time = restored

    @functools.cached_property
    def native_value(self) -> datetime | None:
        """Return last boot time, held steady across poll-timing jitter."""
        candidate: datetime | None = None
        uptime_rebooted = False
        uptime_str = self._get_system_info().get("system_uptime")
        if uptime_str:
            uptime_seconds = parse_uptime_to_seconds(uptime_str)
            if uptime_seconds is not None:
                candidate = dt_util.now() - timedelta(seconds=uptime_seconds)
                # Uptime is monotonic within a boot — a decrease is
                # reboot evidence even when the resulting timestamps
                # land inside the jitter tolerance (double reboot
                # within the deadband).
                uptime_rebooted = self._last_uptime_seconds is not None and uptime_seconds < self._last_uptime_seconds
                self._last_uptime_seconds = uptime_seconds
        if candidate is None:
            candidate = self._snapshot.stats_last_reset
        if candidate is None:
            return self._stable_boot_time
        # Only reboot evidence replaces the held value: an uptime
        # decrease, or a shift beyond the tolerance — otherwise every
        # poll writes a slightly different timestamp to the recorder.
        if (
            self._stable_boot_time is None
            or uptime_rebooted
            or abs(candidate - self._stable_boot_time) > _BOOT_TIME_JITTER_TOLERANCE
        ):
            self._stable_boot_time = candidate
        return self._stable_boot_time


class SystemInfoFieldSensor(_HoldsLastValueMixin, _SystemInfoSensor):
    """Dynamic sensor for Tier 3 system_info fields.

    Created for each system_info field not consumed by a dedicated
    sensor class.  Parameterized by field name — one class handles
    all pass-through fields.  Value is returned as-is with no type
    conversion.

    Sticky for non-continuous fields (#178); continuous fields (those
    in _SYSTEM_INFO_FIELD_UNITS) keep their graph-meaningful gaps.

    See ENTITY_MODEL_SPEC.md § Field Pass-Through.
    """

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[ModemSnapshot],
        entry: CableModemConfigEntry,
        *,
        field: str,
    ) -> None:
        """Initialize with the system_info field name."""
        super().__init__(coordinator, entry)
        self._field = field
        self._attr_name = _humanize_field_name(field)
        self._attr_unique_id = f"{entry.entry_id}_cable_modem_{field}"
        self._attr_icon = "mdi:information-outline"
        unit_meta = _SYSTEM_INFO_FIELD_UNITS.get(field)
        self._continuous = unit_meta is not None
        if unit_meta is not None:
            self._attr_native_unit_of_measurement = unit_meta.native_unit
            self._attr_device_class = unit_meta.device_class
            self._attr_state_class = unit_meta.state_class
            self._attr_suggested_unit_of_measurement = unit_meta.suggested_unit

    @property
    def available(self) -> bool:
        """Sticky for non-continuous fields; continuous fields keep graph gaps."""
        if self._continuous:
            return super().available  # ModemSensorBase: requires modem_data
        return self.coordinator.last_update_success

    @functools.cached_property
    def native_value(self) -> str | int | float | None:
        """Return the field value as-is; held across gaps when non-continuous."""
        current = self._get_system_info().get(self._field)
        return current if self._continuous else self._hold_last(current)


# ------------------------------------------------------------------
# Per-channel sensors
# ------------------------------------------------------------------


class ChannelSensor(ModemSensorBase):
    """Generic channel metric sensor.

    Parameterized by direction, slot key, identity mode, and metric
    definition.  One instance per channel per metric.  All non-metric
    fields from the channel dict flow as extra_state_attributes
    (tier 2/3 pass-through).

    The identity mode determines entity naming (a single ``if``):
    - Position mode: ``DS Ch 1 Power`` / ``ds_ch_1_power``
    - ID mode: ``DS QAM Ch 29 Power`` / ``ds_qam_ch_29_power``
    """

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[ModemSnapshot],
        entry: CableModemConfigEntry,
        *,
        direction: str,
        slot_key: SlotKey,
        identity_mode: ChannelIdentity,
        field: str,
        name_suffix: str,
        unit: str | None,
        device_class: SensorDeviceClass | None,
        state_class: SensorStateClass,
        icon: str,
        value_type: type,
    ) -> None:
        """Initialize the channel sensor."""
        super().__init__(coordinator, entry)
        self._direction = direction
        self._slot_key = slot_key
        self._identity_mode = identity_mode
        self._field = field
        self._value_type = value_type

        prefix = "DS" if direction == "downstream" else "US"
        dir_code = "ds" if direction == "downstream" else "us"

        if identity_mode == ChannelIdentity.NUMBER:
            # Position mode: no channel type in name/id
            self._attr_name = f"{prefix} Ch {slot_key} {name_suffix}"
            self._attr_unique_id = f"{entry.entry_id}_cable_modem_{dir_code}_ch_{slot_key}_{field}"
        else:
            # ID mode: (channel_type, channel_id) tuple
            ch_type, ch_id = slot_key  # type: ignore[misc]
            self._attr_name = f"{prefix} {ch_type.upper()} Ch {ch_id} {name_suffix}"
            self._attr_unique_id = f"{entry.entry_id}_cable_modem_{dir_code}_{ch_type}_ch_{ch_id}_{field}"

        self._attr_icon = icon
        self._attr_state_class = state_class
        if unit:
            self._attr_native_unit_of_measurement = unit
        if device_class:
            self._attr_device_class = device_class

    def _find_channel(self) -> dict[str, Any] | None:
        """Find this channel via pre-built slot maps on runtime_data.

        Slot maps are rebuilt once per poll in the coordinator update
        callback (see ``__init__._async_update_data``).  Sensors do an
        O(1) dict lookup here.
        """
        if self._snapshot.modem_data is None:
            return None
        channel_map = self._entry.runtime_data.channel_map
        direction_map = channel_map.downstream if self._direction == "downstream" else channel_map.upstream
        return direction_map.get(self._slot_key)

    @functools.cached_property
    def native_value(self) -> float | int | None:
        """Return the channel metric value."""
        ch = self._find_channel()
        if ch is None:
            return None
        value = ch.get(self._field)
        if value is None:
            return None
        return self._value_type(value)  # type: ignore[no-any-return]

    @functools.cached_property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return all non-metric fields as attributes.

        Both channel_number and channel_id are always present
        regardless of identity mode.
        """
        ch = self._find_channel()
        if ch is None:
            return {}
        return {key: value for key, value in ch.items() if key != self._field}


# ------------------------------------------------------------------
# LAN statistics sensors
# ------------------------------------------------------------------


class LanStatsSensor(ModemSensorBase):
    """LAN statistics sensor for a specific interface and metric."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[ModemSnapshot],
        entry: CableModemConfigEntry,
        *,
        interface: str,
        field: str,
        name_suffix: str,
        device_class: SensorDeviceClass | None,
        unit: str | None,
    ) -> None:
        """Initialize the LAN stats sensor."""
        super().__init__(coordinator, entry)
        self._interface = interface
        self._field = field
        self._attr_name = f"LAN {interface} {name_suffix}"
        self._attr_unique_id = f"{entry.entry_id}_cable_modem_lan_{interface}_{field}"
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        if device_class:
            self._attr_device_class = device_class
        if unit:
            self._attr_native_unit_of_measurement = unit

    @functools.cached_property
    def native_value(self) -> int | None:
        """Return the LAN stats value."""
        modem_data = self._snapshot.modem_data
        if modem_data is None:
            return None
        lan_stats = modem_data.get("lan_stats", {})
        iface_data = lan_stats.get(self._interface)
        if iface_data is None:
            return None
        value = iface_data.get(self._field)
        return int(value) if value is not None else None


# ------------------------------------------------------------------
# Health sensors
# ------------------------------------------------------------------


class PingLatencySensor(HealthSensorBase):
    """ICMP ping latency sensor.

    Only created when supports_icmp is True.  Reads from the health
    coordinator, independent of data collection timing.
    """

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[HealthInfo],
        entry: CableModemConfigEntry,
    ) -> None:
        """Initialize the ping latency sensor."""
        super().__init__(coordinator, entry)
        self._attr_name = "Ping Latency"
        self._attr_unique_id = f"{entry.entry_id}_cable_modem_ping_latency"
        self._attr_native_unit_of_measurement = "ms"
        self._attr_icon = "mdi:speedometer"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._last_value: int | None = None

    @functools.cached_property
    def native_value(self) -> int | None:
        """Return ping latency in milliseconds.

        Caches last known value to avoid flicker when a probe
        intermittently returns None.
        """
        latency = self._health_info.icmp_latency_ms
        if latency is not None:
            self._last_value = int(round(latency))
        return self._last_value


class TcpLatencySensor(HealthSensorBase):
    """TCP latency sensor — modem L4 reachability.

    Created whenever the health coordinator runs the TCP probe (i.e.
    HTTP probe is enabled in modem.yaml). Independent of HEAD support;
    GET-only modems still get this clean L4 signal.

    Caches last known value to avoid flicker when the TCP probe
    intermittently fails.
    """

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[HealthInfo],
        entry: CableModemConfigEntry,
    ) -> None:
        """Initialize the TCP latency sensor."""
        super().__init__(coordinator, entry)
        self._attr_name = "TCP Latency"
        self._attr_unique_id = f"{entry.entry_id}_cable_modem_tcp_latency"
        self._attr_native_unit_of_measurement = "ms"
        self._attr_icon = "mdi:transit-connection-variant"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._last_value: int | None = None

    @functools.cached_property
    def native_value(self) -> int | None:
        """Return TCP handshake latency in milliseconds.

        Caches last known value to avoid flicker when a probe
        intermittently returns None.
        """
        latency = self._health_info.tcp_latency_ms
        if latency is not None:
            self._last_value = int(round(latency))
        return self._last_value


class HttpLatencySensor(HealthSensorBase):
    """HTTP HEAD latency sensor — modem application-layer responsiveness.

    Only created when the modem advertises ``supports_head=True``. HEAD
    bypasses the modem's CGI handler and gives a clean unimodal latency
    signal. Modems without HEAD support skip this sensor entirely
    because the GET fallback is bimodal (cold vs warm cache paths) and
    would corrupt the metric.

    Caches last known value to avoid flicker when the HTTP probe
    intermittently fails.
    """

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[HealthInfo],
        entry: CableModemConfigEntry,
    ) -> None:
        """Initialize the HTTP HEAD latency sensor."""
        super().__init__(coordinator, entry)
        self._attr_name = "HTTP Latency"
        self._attr_unique_id = f"{entry.entry_id}_cable_modem_http_latency"
        self._attr_native_unit_of_measurement = "ms"
        self._attr_icon = "mdi:web-clock"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._last_value: int | None = None

    @functools.cached_property
    def native_value(self) -> int | None:
        """Return HTTP HEAD latency in milliseconds (server response time).

        Caches last known value to avoid flicker when a probe
        intermittently returns None.
        """
        latency = self._health_info.http_latency_ms
        if latency is not None:
            self._last_value = int(round(latency))
        return self._last_value


# ------------------------------------------------------------------
# Platform setup
# ------------------------------------------------------------------


def _create_channel_sensors(
    data_coord: DataUpdateCoordinator[ModemSnapshot],
    entry: CableModemConfigEntry,
    modem_data: dict[str, Any],
) -> list[SensorEntity]:
    """Create per-channel sensor entities from first poll data.

    Uses the mapping manager to build slot maps based on the user's
    channel identity mode.  Position mode creates entities for all
    positions (including unlocked); ID mode creates only for locked
    channels with valid keys.
    """
    identity_mode = ChannelIdentity(entry.data.get(CONF_CHANNEL_IDENTITY, ChannelIdentity.ID))
    slots = build_channel_map(
        modem_data.get("downstream", []),
        modem_data.get("upstream", []),
        identity_mode,
    )

    entities: list[SensorEntity] = []

    # Downstream
    for slot_key, ch in slots.downstream.items():
        for field, name, unit, dev_cls, state_cls, icon, val_type in _DS_METRICS:
            if field in _DS_ALWAYS_FIELDS or field in ch:
                entities.append(
                    ChannelSensor(
                        data_coord,
                        entry,
                        direction="downstream",
                        slot_key=slot_key,
                        identity_mode=identity_mode,
                        field=field,
                        name_suffix=name,
                        unit=unit,
                        device_class=dev_cls,
                        state_class=state_cls,
                        icon=icon,
                        value_type=val_type,
                    )
                )

    # Upstream
    for slot_key, ch in slots.upstream.items():
        for field, name, unit, dev_cls, state_cls, icon, val_type in _US_METRICS:
            if field in _US_ALWAYS_FIELDS or field in ch:
                entities.append(
                    ChannelSensor(
                        data_coord,
                        entry,
                        direction="upstream",
                        slot_key=slot_key,
                        identity_mode=identity_mode,
                        field=field,
                        name_suffix=name,
                        unit=unit,
                        device_class=dev_cls,
                        state_class=state_cls,
                        icon=icon,
                        value_type=val_type,
                    )
                )

    return entities


def _create_lan_sensors(
    data_coord: DataUpdateCoordinator[ModemSnapshot],
    entry: CableModemConfigEntry,
    modem_data: dict[str, Any],
) -> list[SensorEntity]:
    """Create LAN statistics sensors from first poll data."""
    entities: list[SensorEntity] = []
    lan_stats = modem_data.get("lan_stats", {})

    for interface in lan_stats:
        for field, name, dev_cls, unit in _LAN_METRICS:
            entities.append(
                LanStatsSensor(
                    data_coord,
                    entry,
                    interface=interface,
                    field=field,
                    name_suffix=name,
                    device_class=dev_cls,
                    unit=unit,
                )
            )

    return entities


def _create_data_dependent_entities(
    data_coord: DataUpdateCoordinator[ModemSnapshot],
    entry: CableModemConfigEntry,
    modem_data: dict[str, Any],
) -> list[SensorEntity]:
    """Create entities that require modem poll data.

    Called from async_setup_entry on the happy path (first poll
    succeeded) and from the deferred creation listener when the modem
    comes online after a failed first poll (UC-84).
    """
    entities: list[SensorEntity] = []
    system_info = modem_data.get("system_info", {})

    # Channel counts (always computed by parser coordinator)
    entities.append(ModemChannelCountSensor(data_coord, entry, direction="downstream"))
    entities.append(ModemChannelCountSensor(data_coord, entry, direction="upstream"))

    # Error totals and rates (gated by SC-QAM capability — `total_*`
    # presence indicates the modem reports SC-QAM error counters. Rate
    # sensors share the same gate because the orchestrator deliberately
    # omits rate_* on the first poll, across counter resets, and when
    # monotonic elapsed time is non-positive; gating rate creation on
    # rate_* presence would prevent the sensor from ever materializing
    # (HA's data-dependent entity creation is one-shot at first poll).
    # The rate sensor reads None on polls where the orchestrator omits
    # the field — HA renders that as `unknown`.
    if "total_corrected" in system_info:
        entities.append(ModemErrorTotalSensor(data_coord, entry, error_type="corrected"))
        entities.append(ModemErrorRateSensor(data_coord, entry, error_type="corrected"))
    if "total_uncorrected" in system_info:
        entities.append(ModemErrorTotalSensor(data_coord, entry, error_type="uncorrected"))
        entities.append(ModemErrorRateSensor(data_coord, entry, error_type="uncorrected"))

    # Software version (gated by field presence). system_uptime gets no
    # sensor of its own — it feeds Last Boot Time below, and uptime is
    # rendered at display time as now − last_boot (#178).
    if "software_version" in system_info:
        entities.append(ModemSoftwareVersionSensor(data_coord, entry))

    # Last boot time — from native uptime OR counter-reset detection.
    # Gate mirrors the error-total sensors: SC-QAM aggregate presence
    # means the orchestrator can detect counter resets and proxy last
    # boot time from them.
    has_qam_error_counters = "total_corrected" in system_info or "total_uncorrected" in system_info
    if "system_uptime" in system_info or has_qam_error_counters:
        entities.append(ModemLastBootTimeSensor(data_coord, entry))

    # Per-channel sensors
    entities.extend(_create_channel_sensors(data_coord, entry, modem_data))

    # LAN stats sensors
    entities.extend(_create_lan_sensors(data_coord, entry, modem_data))

    # Tier 3 dynamic system_info sensors (pass-through)
    for field in sorted(system_info):
        if field in CONSUMED_SYSTEM_INFO_FIELDS or field in DISPLAY_ONLY_SYSTEM_INFO_FIELDS:
            continue
        entities.append(SystemInfoFieldSensor(data_coord, entry, field=field))

    return entities


def _register_deferred_entity_creation(
    data_coord: DataUpdateCoordinator[ModemSnapshot],
    entry: CableModemConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register a one-shot listener to create data-dependent entities.

    When the first poll returns modem_data=None (modem unreachable at
    startup), data-dependent entities cannot be created because they
    require channel IDs and field presence from the poll data.  This
    registers a coordinator listener that fires on each update and
    creates entities on the first update with modem_data present.

    See UC-84 in ORCHESTRATION_USE_CASES.md and HA_ADAPTER_SPEC
    § Deferred Entity Creation.
    """

    @callback
    def _on_first_data() -> None:
        """Create data-dependent entities on first successful poll."""
        snapshot: ModemSnapshot | None = data_coord.data
        modem_data = snapshot.modem_data if snapshot else None
        if modem_data is None:
            return

        unsub()

        new_entities = _create_data_dependent_entities(data_coord, entry, modem_data)
        _LOGGER.info(
            "Deferred data sensors created [%s] — %d data-dependent entities",
            entry.runtime_data.modem_identity.model,
            len(new_entities),
        )
        async_add_entities(new_entities)

        # Guarantee initial state for deferred entities. async_add_entities
        # schedules entity registration as an eager-start task. By the time
        # entities register their coordinator listeners, the triggering
        # coordinator update has already completed. Firing the listeners
        # again (after the registration delay) fires _handle_coordinator_update()
        # on all entities including the newly registered ones, populating
        # state from current data without a new modem poll.
        #
        # async_update_listeners() is the right primitive here:
        # async_set_updated_data() also unschedules and reschedules the
        # refresh timer (resetting the regular poll cadence) and emits a
        # "Manually updated" DEBUG line that misrepresents the intent
        # (we didn't update anything, we're re-fanning current data).
        async def _async_ensure_initial_state() -> None:
            await asyncio.sleep(1)
            data_coord.async_update_listeners()

        data_coord.hass.async_create_task(
            _async_ensure_initial_state(),
            "cable_modem_deferred_entity_state",
        )

    unsub = data_coord.async_add_listener(_on_first_data)
    entry.async_on_unload(unsub)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CableModemConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Cable Modem Monitor sensor entities."""
    runtime = entry.runtime_data
    data_coord = runtime.data_coordinator
    health_coord = runtime.health_coordinator
    snapshot = data_coord.data

    entities: list[SensorEntity] = []

    # -- Always created --
    entities.append(ModemStatusSensor(data_coord, entry))
    entities.append(ModemInfoSensor(data_coord, entry))

    # -- Health sensors (from health coordinator) --
    if health_coord is not None:
        entities.append(TcpLatencySensor(health_coord, entry))
        if entry.data.get(CONF_SUPPORTS_ICMP, False):
            entities.append(PingLatencySensor(health_coord, entry))
        if entry.data.get(CONF_SUPPORTS_HEAD, False):
            entities.append(HttpLatencySensor(health_coord, entry))

    # -- Data-dependent sensors (require modem_data from first poll) --
    modem_data = snapshot.modem_data if snapshot else None
    if modem_data is None:
        _LOGGER.info(
            "No modem data from first poll [%s] — deferring data sensors",
            runtime.modem_identity.model,
        )
        async_add_entities(entities)
        _register_deferred_entity_creation(data_coord, entry, async_add_entities)
        return

    entities.extend(_create_data_dependent_entities(data_coord, entry, modem_data))

    _LOGGER.info("Created %d sensor entities [%s]", len(entities), runtime.modem_identity.model)
    async_add_entities(entities)
