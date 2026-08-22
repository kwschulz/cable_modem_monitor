"""Dev tool implementations for Cable Modem Monitor.

Contains the dashboard YAML generator and channel identity converter —
the two developer-facing tools whose implementations are decoupled from
service registration wiring.  Service registration lives in services.py,
which imports the handler factories from here.

See HA_ADAPTER_SPEC.md § Services.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from typing import Any

import homeassistant.helpers.device_registry as dr
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import slugify as ha_slugify

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
from .lib.utils import get_device_name

_LOGGER = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Device target resolution (dev-tool variant — single-entry lookups)
# ------------------------------------------------------------------


def _find_loaded_entry(
    hass: HomeAssistant,
) -> CableModemConfigEntry | None:
    """Find the first loaded config entry for our domain."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if hasattr(entry, "runtime_data") and entry.runtime_data is not None:
            return entry
    return None


def _resolve_config_entry_for_device(
    hass: HomeAssistant,
    device_id: str,
) -> CableModemConfigEntry | None:
    """Resolve a device_id to a loaded config entry for our domain."""
    registry = dr.async_get(hass)
    device = registry.async_get(device_id)
    if device is None:
        return None

    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if (
            entry is not None
            and entry.domain == DOMAIN
            and hasattr(entry, "runtime_data")
            and entry.runtime_data is not None
        ):
            return entry  # type: ignore[return-value]  # rationale: async_get_entry returns ConfigEntry | None; the domain + runtime_data guards above confirm this is our typed entry

    return None


# ------------------------------------------------------------------
# Entity ID prefix — matches _update_device_registry in __init__.py
# ------------------------------------------------------------------


def _get_entity_prefix(entry: CableModemConfigEntry) -> str:
    """Derive the entity ID prefix from config entry settings.

    Must match the device_name logic in get_device_name() so
    generated entity IDs match what HA actually creates.
    """
    data = entry.data
    device_name = get_device_name(
        data.get(CONF_ENTITY_PREFIX, "default"),
        model=entry.runtime_data.modem_identity.model,
        host=data.get(CONF_HOST, ""),
    )
    return str(ha_slugify(device_name))


# ------------------------------------------------------------------
# Channel helpers — read v3.14 ModemSnapshot data directly
# ------------------------------------------------------------------


def _get_channel_info_id_mode(
    channels: list[dict[str, Any]],
    default_type: str,
) -> list[tuple[str, int]]:
    """Extract (channel_type, channel_id) pairs for ID mode dashboards.

    Args:
        channels: Channel dicts from modem_data.
        default_type: Fallback type when channel_type is absent.
    """
    result: list[tuple[str, int]] = []
    for idx, ch in enumerate(channels):
        ch_type = ch.get("channel_type", default_type)
        ch_id = ch.get("channel_id", idx + 1)
        if isinstance(ch_id, str):
            try:
                ch_id = int(ch_id)
            except ValueError:
                ch_id = idx + 1
        result.append((ch_type, ch_id))
    return sorted(result)


def _get_channel_info_number_mode(
    channels: list[dict[str, Any]],
) -> list[tuple[str, int]]:
    """Extract channel numbers for position mode dashboards.

    Returns ("", channel_number) tuples so downstream functions
    keep the same signature.  The empty type string signals position
    mode to label/pattern formatters.
    """
    result: list[tuple[str, int]] = []
    for idx, ch in enumerate(channels):
        ch_num = ch.get("channel_number", idx + 1)
        lock_status = ch.get("lock_status")
        # Missing lock_status is a supported Core condition (some modems,
        # e.g. hitron/coda56, don't report it) and must be treated as
        # locked per CHANNEL_IDENTIFICATION_SPEC §6.
        if lock_status is None or lock_status == "locked":
            result.append(("", ch_num))
    return sorted(result, key=lambda x: x[1])


def _get_channel_info(
    modem_data: dict[str, Any],
    identity_mode: ChannelIdentity,
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Extract channel info for both directions based on identity mode."""
    if identity_mode == ChannelIdentity.NUMBER:
        ds = _get_channel_info_number_mode(modem_data.get("downstream", []))
        us = _get_channel_info_number_mode(modem_data.get("upstream", []))
    else:
        ds = _get_channel_info_id_mode(modem_data.get("downstream", []), "qam")
        us = _get_channel_info_id_mode(modem_data.get("upstream", []), "atdma")
    return ds, us


def _group_by_type(
    channel_info: list[tuple[str, int]],
) -> dict[str, list[tuple[str, int]]]:
    """Group channel info tuples by channel type."""
    grouped: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for ch_type, ch_id in channel_info:
        grouped[ch_type].append((ch_type, ch_id))
    return dict(grouped)


def _unique_types(channel_info: list[tuple[str, int]]) -> set[str]:
    """Get unique channel types from channel info."""
    return {ch_type for ch_type, _ in channel_info}


# ------------------------------------------------------------------
# YAML builders
# ------------------------------------------------------------------

# Default fields left off the generated status card. Empty since the
# identity strings (hardware_version, model_name) stopped minting
# sensors entirely (DISPLAY_ONLY_SYSTEM_INFO_FIELDS); the option
# remains for users to drop rows, including the explicit
# docsis_status row below.
STATUS_CARD_DEFAULT_EXCLUDE: tuple[str, ...] = ()


def _get_dashboard_titles(short_titles: bool) -> dict[str, str]:
    """Get dashboard card titles based on user preference."""
    if short_titles:
        return {
            "ds_power": "DS Power (dBmV)",
            "ds_snr": "DS SNR (dB)",
            "ds_freq": "DS Frequency (MHz)",
            "us_power": "US Power (dBmV)",
            "us_freq": "US Frequency (MHz)",
            "corrected": "Corrected Errors",
            "uncorrected": "Uncorrected Errors",
            "corrected_rate": "Corrected Error Rate",
            "uncorrected_rate": "Uncorrected Error Rate",
        }
    return {
        "ds_power": "Downstream Power Levels (dBmV)",
        "ds_snr": "Downstream Signal-to-Noise Ratio (dB)",
        "ds_freq": "Downstream Frequency (MHz)",
        "us_power": "Upstream Power Levels (dBmV)",
        "us_freq": "Upstream Frequency (MHz)",
        "corrected": "Corrected Errors (Total)",
        "uncorrected": "Uncorrected Errors (Total)",
        "corrected_rate": "Corrected Error Rate (errors/min)",
        "uncorrected_rate": "Uncorrected Error Rate (errors/min)",
    }


def _format_title_with_type(
    base_title: str,
    channel_type: str | None,
    short_titles: bool,
) -> str:
    """Format a title with optional channel type prefix.

    For single-type directions, puts the type in the title so labels
    can omit it (e.g., "Downstream QAM Power Levels (dBmV)").
    """
    if not channel_type:
        return base_title
    type_upper = channel_type.upper()
    if short_titles:
        if base_title.startswith("DS "):
            return f"DS {type_upper} {base_title[3:]}"
        if base_title.startswith("US "):
            return f"US {type_upper} {base_title[3:]}"
    else:
        if base_title.startswith("Downstream "):
            return f"Downstream {type_upper} {base_title[11:]}"
        if base_title.startswith("Upstream "):
            return f"Upstream {type_upper} {base_title[9:]}"
    return base_title


def _format_channel_label(
    ch_type: str,
    ch_id: int,
    label_format: str,
) -> str:
    """Format a channel label for the dashboard.

    Args:
        ch_type: Channel type (e.g., "qam", "ofdm").
            Empty string for position mode.
        ch_id: Channel ID or channel number.
        label_format: One of "full", "id_only", "type_id".
    """
    if not ch_type:
        # Position mode — no type in label
        return f"Ch {ch_id}"
    if label_format == "id_only":
        return f"Ch {ch_id}"
    if label_format == "type_id":
        return f"{ch_type.upper()} {ch_id}"
    return f"{ch_type.upper()} Ch {ch_id}"


def _build_status_card_yaml(
    entity_prefix: str,
    system_info: dict[str, Any],
    *,
    has_icmp: bool,
    has_head: bool,
    exclude_fields: frozenset[str] | None = None,
) -> list[str]:
    """Build YAML for the status entities card.

    Only includes entities for fields the modem actually provides.
    Channel counts and status are always present; everything else is
    gated on system_info field presence (matching sensor.py gating).

    Args:
        entity_prefix: Entity ID prefix (e.g., "cable_modem").
        system_info: The system_info dict from modem data.
        has_icmp: Whether ICMP ping latency entity exists.
        has_head: Whether the HTTP HEAD latency entity exists
            (only created on supports_head=True modems).
        exclude_fields: Pass-through fields to omit from the card;
            defaults to STATUS_CARD_DEFAULT_EXCLUDE.
    """
    if exclude_fields is None:
        exclude_fields = frozenset(STATUS_CARD_DEFAULT_EXCLUDE)
    lines = [
        "  - type: entities",
        "    title: Cable Modem Status",
        "    entities:",
        f"      - entity: sensor.{entity_prefix}_status",
        "        name: Status",
        "        tap_action:",
        "          action: call-service",
        "          service: button.press",
        "          target:",
        f"            entity_id: button.{entity_prefix}_update_modem_data",
    ]
    if has_icmp:
        lines.append(f"      - entity: sensor.{entity_prefix}_ping_latency")
        lines.append("        name: Ping")
    lines.extend(
        [
            f"      - entity: sensor.{entity_prefix}_tcp_latency",
            "        name: TCP",
            "        icon: mdi:transit-connection-variant",
        ]
    )
    if has_head:
        lines.extend(
            [
                f"      - entity: sensor.{entity_prefix}_http_latency",
                "        name: HTTP",
                "        icon: mdi:speedometer",
            ]
        )
    # DOCSIS status renders as an `attribute` row off the Status sensor
    # (the standalone sensor was retired, #178) at its historical
    # position (the pre-beta.12 "Modem Status" spot). The row shows the
    # raw value (e.g. `operational`); prettifying needs a template card.
    if "docsis_status" in system_info and "docsis_status" not in exclude_fields:
        lines.append("      - type: attribute")
        lines.append(f"        entity: sensor.{entity_prefix}_status")
        lines.append("        attribute: docsis_status")
        lines.append("        name: DOCSIS Status")
    if "software_version" in system_info:
        lines.append(f"      - entity: sensor.{entity_prefix}_software_version")
        lines.append("        name: Software Version")
    # Uptime is a display-time calculation, not a sensor (#178): both
    # rows render Last Boot Time — relative ("5 days ago") for uptime,
    # absolute for the boot instant. Gate mirrors sensor.py's Last
    # Boot Time creation gate.
    if "system_uptime" in system_info or "total_corrected" in system_info or "total_uncorrected" in system_info:
        lines.extend(
            [
                f"      - entity: sensor.{entity_prefix}_last_boot_time",
                "        name: Uptime",
                "        format: relative",
                f"      - entity: sensor.{entity_prefix}_last_boot_time",
                "        name: Last Boot",
                "        format: datetime",
            ]
        )
    lines.extend(
        [
            f"      - entity: sensor.{entity_prefix}_ds_channel_count",
            "        name: Downstream Channel Count",
            f"      - entity: sensor.{entity_prefix}_us_channel_count",
            "        name: Upstream Channel Count",
        ]
    )
    # Error totals only — the rate sensors exist as HA entities but
    # are intentionally omitted from the status entities row. Rates
    # are point-in-time MEASUREMENT values that jitter poll-to-poll;
    # the summary row is for stable identity/state, the rate trend
    # belongs in a history graph (see include_error_rates).
    if "total_corrected" in system_info:
        lines.extend(
            [
                f"      - entity: sensor.{entity_prefix}_total_corrected_errors",
                "        name: Total Corrected Errors",
                f"      - entity: sensor.{entity_prefix}_total_uncorrected_errors",
                "        name: Total Uncorrected Errors",
            ]
        )
    for field in sorted(system_info):
        if (
            field in CONSUMED_SYSTEM_INFO_FIELDS  # includes docsis_status (attribute row above)
            or field in DISPLAY_ONLY_SYSTEM_INFO_FIELDS
            or field in exclude_fields
        ):
            continue
        lines.append(f"      - entity: sensor.{entity_prefix}_{field}")
        lines.append(f"        name: {field.replace('_', ' ').title()}")
    lines.append("    show_header_toggle: false")
    lines.append("    state_color: false")
    return lines


def _build_restart_button_card_yaml(entity_prefix: str) -> list[str]:
    """Build YAML for the standalone restart button card.

    A dedicated `button` card is used instead of an entities-row so the
    confirmation dialog reliably guards every tap. In an entities card,
    clicking the button widget bypasses the row's tap_action.
    """
    return [
        "  - type: button",
        f"    entity: button.{entity_prefix}_restart_modem",
        "    name: Restart Modem",
        "    icon: mdi:restart",
        "    show_state: false",
        "    tap_action:",
        "      action: call-service",
        "      service: button.press",
        "      target:",
        f"        entity_id: button.{entity_prefix}_restart_modem",
        "      confirmation:",
        "        text: This will restart your modem and temporarily disconnect your internet.",
    ]


def _build_channel_graph_yaml(
    title: str,
    hours: int,
    channel_info: list[tuple[str, int]],
    entity_pattern: str,
    channel_label: str,
) -> list[str]:
    """Build YAML for a channel history graph card."""
    yaml_parts = [
        "  - type: history-graph",
        f"    title: {title}",
        f"    hours_to_show: {hours}",
        "    entities:",
    ]
    for ch_type, ch_id in channel_info:
        entity_id = entity_pattern.format(ch_type=ch_type, ch_id=ch_id)
        label = _format_channel_label(ch_type, ch_id, channel_label)
        yaml_parts.append(f"      - entity: {entity_id}")
        yaml_parts.append(f"        name: {label}")
    return yaml_parts


def _build_error_graphs_yaml(
    entity_prefix: str,
    titles: dict[str, str],
    *,
    include_rates: bool = False,
) -> list[str]:
    """Build YAML for error history graphs.

    Count graphs are always emitted (caller gates this function on
    ``total_corrected`` presence). Rate graphs are opt-in via
    ``include_rates`` — rates are MEASUREMENT values that jitter
    poll-to-poll, so they're off by default; users who want a rate
    trend over time enable ``include_error_rates`` on the service call.
    """
    lines = [
        "  - type: history-graph",
        f"    title: {titles['corrected']}",
        "    hours_to_show: 168",
        "    entities:",
        f"      - entity: sensor.{entity_prefix}_total_corrected_errors",
        "        name: Corrected Error Count",
        "  - type: history-graph",
        f"    title: {titles['uncorrected']}",
        "    hours_to_show: 168",
        "    entities:",
        f"      - entity: sensor.{entity_prefix}_total_uncorrected_errors",
        "        name: Uncorrected Error Count",
    ]
    if include_rates:
        lines.extend(
            [
                "  - type: history-graph",
                f"    title: {titles['corrected_rate']}",
                "    hours_to_show: 168",
                "    entities:",
                f"      - entity: sensor.{entity_prefix}_rate_corrected_errors",
                "        name: Corrected Error Rate",
                "  - type: history-graph",
                f"    title: {titles['uncorrected_rate']}",
                "    hours_to_show: 168",
                "    entities:",
                f"      - entity: sensor.{entity_prefix}_rate_uncorrected_errors",
                "        name: Uncorrected Error Rate",
            ]
        )
    return lines


def _build_latency_graph_yaml(
    entity_prefix: str,
    *,
    has_icmp: bool,
    has_head: bool,
) -> list[str]:
    """Build YAML for the latency history graph.

    Always graphs TCP latency (the L4 reachability signal). Adds Ping
    and HTTP HEAD lines only when those entities exist for this modem.
    """
    lines = [
        "  - type: history-graph",
        "    title: Latency",
        "    hours_to_show: 6",
        "    entities:",
    ]
    if has_icmp:
        lines.append(f"      - entity: sensor.{entity_prefix}_ping_latency")
        lines.append("        name: Ping")
    lines.append(f"      - entity: sensor.{entity_prefix}_tcp_latency")
    lines.append("        name: TCP")
    if has_head:
        lines.append(f"      - entity: sensor.{entity_prefix}_http_latency")
        lines.append("        name: HTTP")
    return lines


def _add_channel_graphs(
    yaml_parts: list[str],
    channel_info: list[tuple[str, int]],
    base_title: str,
    entity_pattern: str,
    graph_hours: int,
    channel_label: str,
    channel_grouping: str,
    short_titles: bool,
) -> None:
    """Add channel graph cards based on grouping preference.

    Modifies *yaml_parts* in place.
    """
    if not channel_info:
        return

    types = _unique_types(channel_info)
    is_single_type = len(types) == 1

    if channel_grouping == "by_type":
        grouped = _group_by_type(channel_info)
        for ch_type in sorted(grouped):
            channels = grouped[ch_type]
            title = _format_title_with_type(base_title, ch_type, short_titles)
            effective_label = "id_only" if channel_label == "auto" else channel_label
            yaml_parts.extend(
                _build_channel_graph_yaml(
                    title,
                    graph_hours,
                    channels,
                    entity_pattern,
                    effective_label,
                )
            )
    else:
        if is_single_type:
            single_type = next(iter(types))
            title = _format_title_with_type(base_title, single_type, short_titles)
            effective_label = "id_only" if channel_label == "auto" else channel_label
        else:
            title = base_title
            effective_label = "full" if channel_label == "auto" else channel_label
        yaml_parts.extend(
            _build_channel_graph_yaml(
                title,
                graph_hours,
                channel_info,
                entity_pattern,
                effective_label,
            )
        )


def _build_channel_graph_defs(
    entity_prefix: str,
    identity_mode: ChannelIdentity,
    downstream_info: list[tuple[str, int]],
    upstream_info: list[tuple[str, int]],
) -> list[tuple[str, list[tuple[str, int]], str, str]]:
    """Build the channel graph definitions for the dashboard.

    Returns (opt_key, channel_info, title_key, entity_pattern) tuples.
    """
    p = entity_prefix
    if identity_mode == ChannelIdentity.NUMBER:
        ds = f"sensor.{p}_ds_ch_{{ch_id}}"
        us = f"sensor.{p}_us_ch_{{ch_id}}"
    else:
        ds = f"sensor.{p}_ds_{{ch_type}}_ch_{{ch_id}}"
        us = f"sensor.{p}_us_{{ch_type}}_ch_{{ch_id}}"
    # fmt: off
    return [
        ("ds_power", downstream_info, "ds_power", f"{ds}_power"),
        ("ds_snr",   downstream_info, "ds_snr",   f"{ds}_snr"),
        ("ds_freq",  downstream_info, "ds_freq",  f"{ds}_frequency"),
        ("us_power", upstream_info,   "us_power", f"{us}_power"),
        ("us_freq",  upstream_info,   "us_freq",  f"{us}_frequency"),
    ]
    # fmt: on


# ------------------------------------------------------------------
# Dashboard handler factory
# ------------------------------------------------------------------


def _resolve_dashboard_target(hass: HomeAssistant, call: ServiceCall) -> tuple[CableModemConfigEntry, dict[str, Any]]:
    """Resolve the config entry and modem data for a dashboard request."""
    device_id = call.data.get("device_id")
    entry = _resolve_config_entry_for_device(hass, device_id) if device_id else _find_loaded_entry(hass)
    if entry is None:
        raise ServiceValidationError("No cable modem configured")

    snapshot = entry.runtime_data.data_coordinator.data
    if snapshot is None or snapshot.modem_data is None:
        raise ServiceValidationError("No modem data available — the modem must be online")

    return entry, snapshot.modem_data


def create_generate_dashboard_handler(
    hass: HomeAssistant,
) -> Any:
    """Create the generate_dashboard service handler."""

    def handle_generate_dashboard(call: ServiceCall) -> dict[str, Any]:
        """Handle the generate_dashboard service call."""
        entry, modem_data = _resolve_dashboard_target(hass, call)

        entity_prefix = _get_entity_prefix(entry)
        has_icmp = bool(entry.data.get(CONF_SUPPORTS_ICMP, False))
        has_head = bool(entry.data.get(CONF_SUPPORTS_HEAD, False))
        has_restart = entry.runtime_data.orchestrator.supports_restart
        system_info = modem_data.get("system_info", {})

        opts = {
            "ds_power": call.data.get("include_downstream_power", True),
            "ds_snr": call.data.get("include_downstream_snr", True),
            "ds_freq": call.data.get("include_downstream_frequency", True),
            "us_power": call.data.get("include_upstream_power", True),
            "us_freq": call.data.get("include_upstream_frequency", True),
            "errors": call.data.get("include_errors", True),
            "error_rates": call.data.get("include_error_rates", False),
            "latency": call.data.get("include_latency", True),
            "status": call.data.get("include_status_card", True),
        }
        graph_hours = call.data.get("graph_hours", 24)
        short_titles = call.data.get("short_titles", True)
        titles = _get_dashboard_titles(short_titles)
        channel_label = call.data.get("channel_label", "auto")
        channel_grouping = call.data.get("channel_grouping", "by_direction")

        identity_mode = ChannelIdentity(entry.data.get(CONF_CHANNEL_IDENTITY, ChannelIdentity.ID))
        downstream_info, upstream_info = _get_channel_info(modem_data, identity_mode)

        yaml_parts = [
            "# Cable Modem Dashboard",
            "# Copy from here, paste into: Dashboard > Add Card > Manual",
            "type: vertical-stack",
            "cards:",
        ]

        if opts["status"]:
            yaml_parts.extend(
                _build_status_card_yaml(
                    entity_prefix,
                    system_info,
                    has_icmp=has_icmp,
                    has_head=has_head,
                    exclude_fields=frozenset(call.data.get("status_card_exclude", STATUS_CARD_DEFAULT_EXCLUDE)),
                )
            )

        if has_restart:
            yaml_parts.extend(_build_restart_button_card_yaml(entity_prefix))

        channel_graphs = _build_channel_graph_defs(
            entity_prefix,
            identity_mode,
            downstream_info,
            upstream_info,
        )

        for opt_key, ch_info, title_key, entity_pattern in channel_graphs:
            if opts[opt_key]:
                _add_channel_graphs(
                    yaml_parts,
                    ch_info,
                    titles[title_key],
                    entity_pattern,
                    graph_hours,
                    channel_label,
                    channel_grouping,
                    short_titles,
                )

        if opts["errors"] and "total_corrected" in system_info:
            yaml_parts.extend(
                _build_error_graphs_yaml(
                    entity_prefix,
                    titles,
                    include_rates=opts["error_rates"],
                )
            )

        if opts["latency"]:
            yaml_parts.extend(
                _build_latency_graph_yaml(
                    entity_prefix,
                    has_icmp=has_icmp,
                    has_head=has_head,
                )
            )

        _LOGGER.info(
            "Generated dashboard: %d DS channels, %d US channels, prefix=%s",
            len(downstream_info),
            len(upstream_info),
            entity_prefix,
        )
        return {"yaml": "\n".join(yaml_parts)}

    return handle_generate_dashboard


# ------------------------------------------------------------------
# Channel identity conversion
# ------------------------------------------------------------------


def _build_channel_lookup(
    modem_data: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Build direction → channel list lookup from modem data."""
    return {
        "ds": modem_data.get("downstream", []),
        "us": modem_data.get("upstream", []),
    }


# Statistic ID patterns per mode (entity_id format):
#   ID mode:       sensor.{prefix}_{ds|us}_{type}_ch_{id}_{metric}
#   Position mode: sensor.{prefix}_{ds|us}_ch_{num}_{metric}
_ID_MODE_STAT_ID = re.compile(r"^sensor\.(\w+)_(ds|us)_(\w+)_ch_(\d+)_(\w+)$")
_NUMBER_MODE_STAT_ID = re.compile(r"^sensor\.(\w+)_(ds|us)_ch_(\d+)_(\w+)$")


def _plan_stat_renames_to_number(
    stat_ids: list[str],
    channels: dict[str, list[dict[str, Any]]],
    prefix: str,
) -> list[tuple[str, str]]:
    """Plan statistic renames from id-mode to number-mode entity_ids."""
    lookup: dict[tuple[str, str, int], int] = {}
    for direction, ch_list in channels.items():
        for ch in ch_list:
            ch_type = ch.get("channel_type")
            ch_id = ch.get("channel_id")
            ch_num = ch.get("channel_number")
            if ch_type is not None and ch_id is not None and ch_num is not None:
                lookup[(direction, ch_type, ch_id)] = ch_num

    renames: list[tuple[str, str]] = []
    for stat_id in stat_ids:
        match = _ID_MODE_STAT_ID.match(stat_id)
        if not match:
            continue
        stat_prefix, direction, ch_type, ch_id_str, metric = match.groups()
        if stat_prefix != prefix:
            continue
        ch_num = lookup.get((direction, ch_type, int(ch_id_str)))
        if ch_num is None:
            continue
        new_id = f"sensor.{prefix}_{direction}_ch_{ch_num}_{metric}"
        renames.append((stat_id, new_id))
    return renames


def _plan_stat_renames_to_id(
    stat_ids: list[str],
    channels: dict[str, list[dict[str, Any]]],
    prefix: str,
) -> list[tuple[str, str]]:
    """Plan statistic renames from number-mode to id-mode entity_ids."""
    lookup: dict[tuple[str, int], tuple[str, int]] = {}
    for direction, ch_list in channels.items():
        for ch in ch_list:
            ch_type = ch.get("channel_type")
            ch_id = ch.get("channel_id")
            ch_num = ch.get("channel_number")
            if ch_type is not None and ch_id is not None and ch_num is not None:
                lookup[(direction, ch_num)] = (ch_type, ch_id)

    renames: list[tuple[str, str]] = []
    for stat_id in stat_ids:
        match = _NUMBER_MODE_STAT_ID.match(stat_id)
        if not match:
            continue
        stat_prefix, direction, ch_num_str, metric = match.groups()
        if stat_prefix != prefix:
            continue
        key = lookup.get((direction, int(ch_num_str)))
        if key is None:
            continue
        ch_type, ch_id = key
        new_id = f"sensor.{prefix}_{direction}_{ch_type}_ch_{ch_id}_{metric}"
        renames.append((stat_id, new_id))
    return renames


def _migrate_statistics(
    hass: HomeAssistant,
    stat_renames: list[tuple[str, str]],
) -> int:
    """Migrate recorder statistics from old entity_ids to new.

    Uses the recorder's task queue directly — no entity registry
    events, no race with running entities.  Both clear and rename
    tasks go through queue_task FIFO, so the clear for each target
    completes before the rename attempts to use that statistic_id.
    """
    from homeassistant.helpers.recorder import get_instance

    recorder = get_instance(hass)

    # Queue clear + rename pairs.  FIFO ordering guarantees the
    # clear runs before the rename for each target statistic_id.
    for old_stat_id, new_stat_id in stat_renames:
        recorder.async_clear_statistics([new_stat_id])
        recorder.async_update_statistics_metadata(
            old_stat_id,
            new_statistic_id=new_stat_id,
        )

    return len(stat_renames)


def create_convert_channel_identity_handler(
    hass: HomeAssistant,
) -> Any:
    """Create the convert_channel_identity service handler."""

    async def handle_convert(call: ServiceCall) -> dict[str, Any]:
        """Migrate recorder history to match the current channel mode.

        Searches recorder statistics for entries that use the opposite
        naming pattern (e.g. from a previous install).  Renames them
        to the current mode so historical data is preserved.
        """
        device_id = call.data.get("device_id")
        if device_id:
            entry = _resolve_config_entry_for_device(hass, device_id)
        else:
            entry = _find_loaded_entry(hass)
        if entry is None:
            raise ServiceValidationError("No cable modem configured")

        target_mode = ChannelIdentity(entry.data.get(CONF_CHANNEL_IDENTITY, ChannelIdentity.ID))

        snapshot = entry.runtime_data.data_coordinator.data
        if snapshot is None or snapshot.modem_data is None:
            raise ServiceValidationError("No modem data available — the modem must be online")

        channels = _build_channel_lookup(snapshot.modem_data)
        prefix = _get_entity_prefix(entry)

        # Query recorder for all statistic IDs.
        from homeassistant.components.recorder.statistics import (
            async_list_statistic_ids,
        )

        all_stats = await async_list_statistic_ids(hass)
        all_stat_ids = [s["statistic_id"] for s in all_stats]

        # Plan renames from opposite-mode stats to current mode.
        if target_mode == ChannelIdentity.NUMBER:
            stat_renames = _plan_stat_renames_to_number(all_stat_ids, channels, prefix)
        else:
            stat_renames = _plan_stat_renames_to_id(all_stat_ids, channels, prefix)

        if not stat_renames:
            return {
                "info": f"No statistics to migrate — already in {target_mode.value} mode",
                "renamed": 0,
            }

        migrated = _migrate_statistics(hass, stat_renames)
        model = entry.runtime_data.modem_identity.model
        _LOGGER.info(
            "Migrated statistics to %s mode [%s]: %d statistics renamed",
            target_mode.value,
            model,
            migrated,
        )

        # Reload so sensors pick up the migrated statistics.
        hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))
        return {"renamed": migrated, "mode": target_mode.value}

    return handle_convert


async def _clear_statistic_batch(hass: HomeAssistant, ids: list[str]) -> None:
    """Clear one batch of statistic IDs via the recorder and wait for completion."""
    batch_done = asyncio.Event()

    def _on_done() -> None:
        hass.loop.call_soon_threadsafe(batch_done.set)

    get_instance(hass).async_clear_statistics(ids, on_done=_on_done)
    async with asyncio.timeout(30):
        await batch_done.wait()


async def _purge_statistic_ids(hass: HomeAssistant, statistic_ids: list[str]) -> int:
    """Purge statistic IDs in batches of 100. Returns total count purged."""
    batch_size = 100
    purged = 0
    for i in range(0, len(statistic_ids), batch_size):
        await _clear_statistic_batch(hass, statistic_ids[i : i + batch_size])
        purged += len(statistic_ids[i : i + batch_size])
    return purged


def create_orphaned_statistics_handler(
    hass: HomeAssistant,
) -> Any:
    """Create the orphaned_statistics service handler."""

    async def handle_orphaned_statistics(call: ServiceCall) -> dict[str, Any]:
        """Find recorder statistics with no registered entity for this modem.

        Returns a YAML snippet for review, or purges directly when execute=True.
        """
        device_id = call.data.get("device_id")
        if device_id:
            entry = _resolve_config_entry_for_device(hass, device_id)
        else:
            entry = _find_loaded_entry(hass)
        if entry is None:
            raise ServiceValidationError("No cable modem configured")

        prefix = _get_entity_prefix(entry)

        from homeassistant.components.recorder.statistics import (
            async_list_statistic_ids,
        )
        from homeassistant.helpers import entity_registry as er

        all_stats = await async_list_statistic_ids(hass)
        our_stat_ids = {s["statistic_id"] for s in all_stats if s["statistic_id"].startswith(f"sensor.{prefix}_")}

        if not our_stat_ids:
            return {
                "yaml": f"# No statistics found for modem prefix '{prefix}'.",
                "count": 0,
            }

        registry = er.async_get(hass)
        registered_entity_ids = {e.entity_id for e in registry.entities.get_entries_for_config_entry_id(entry.entry_id)}

        orphaned = sorted(our_stat_ids - registered_entity_ids)

        if not orphaned:
            return {
                "yaml": (
                    f"# No orphaned statistics found for modem prefix '{prefix}'.\n"
                    f"# All {len(our_stat_ids)} statistics have registered entities."
                ),
                "count": 0,
            }

        execute = call.data.get("execute", False)
        total = len(orphaned)

        if execute:
            purged = await _purge_statistic_ids(hass, orphaned)
            _LOGGER.warning(
                "Purged %d orphaned statistics for prefix '%s'",
                purged,
                prefix,
            )
            return {
                "yaml": f"# Purged {purged} orphaned statistics for modem prefix '{prefix}'.",
                "count": purged,
            }

        _LOGGER.debug(
            "Orphaned statistics preview: %d found for prefix '%s'",
            total,
            prefix,
        )
        lines = [
            f"# Found {total} orphaned entity record(s) for modem prefix '{prefix}'.",
            "# These have no registered entity. They were likely left behind",
            "# by a mode switch, channel rebonding (ID mode), or a prefix change.",
            "#",
            "# To purge all of them: call this service again with execute: true.",
            "# WARNING: execute: true is permanent and cannot be undone.",
            "#",
            "# Orphaned entities (not a runnable action):",
            "#",
        ]
        for stat_id in orphaned:
            lines.append(f"# {stat_id}")

        return {"yaml": "\n".join(lines), "count": total}

    return handle_orphaned_statistics
