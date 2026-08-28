"""Constants for the Cable Modem Monitor integration."""

from __future__ import annotations

from enum import StrEnum

from homeassistant.const import Platform

# IMPORTANT: Do not edit VERSION manually!
# Use: python scripts/release.py <version>
VERSION = "3.14.0"

DOMAIN = "cable_modem_monitor"
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]

# Config entry keys — user selections (config flow Steps 1-3)
CONF_MANUFACTURER = "manufacturer"
CONF_MODEL = "model"
CONF_VARIANT = "variant"
CONF_USER_SELECTED_MODEM = "user_selected_modem"
CONF_ENTITY_PREFIX = "entity_prefix"
CONF_CHANNEL_IDENTITY = "channel_identity"
CONF_MODEM_DIR = "modem_dir"

# Config entry keys — derived during validation (config flow Step 4)
CONF_PROTOCOL = "protocol"
CONF_LEGACY_SSL = "legacy_ssl"
CONF_SUPPORTS_ICMP = "supports_icmp"
CONF_SUPPORTS_HEAD = "supports_head"
CONF_CREDENTIAL_ENCODING = "credential_encoding"
CONF_CREDENTIAL_FIELD = "credential_field"

# Config entry key — channel-bond onboarding eligibility.
# Set to ``True`` by the config flow on fresh setup; absent for
# upgraded entries that pre-date the channel-bond notifier. Never
# mutated after create, so it doesn't trip the entry update listener
# (which reloads the integration). Baseline totals themselves live in
# the dedicated Store (``channel_bond_storage``), not entry data.
CONF_CHANNEL_ONBOARDING_ELIGIBLE = "channel_onboarding_eligible"

# Polling configuration (options flow)
CONF_SCAN_INTERVAL = "scan_interval"
CONF_HEALTH_CHECK_INTERVAL = "health_check_interval"

# Defaults — data polling
DEFAULT_SCAN_INTERVAL = 600  # 10 minutes
MIN_SCAN_INTERVAL = 30
MAX_SCAN_INTERVAL = 86400  # 24 hours

# Defaults — health checks
DEFAULT_HEALTH_CHECK_INTERVAL = 30
MIN_HEALTH_CHECK_INTERVAL = 10
MAX_HEALTH_CHECK_INTERVAL = 86400  # 24 hours

# SSL certificate verification — disabled for consumer cable modems.
# Consumer modems universally use self-signed certificates on private LANs.
# Enabling verification would break 99%+ of installations.
VERIFY_SSL = False


# ---------------------------------------------------------------------------
# Value enums for config entry fields
# ---------------------------------------------------------------------------


class ChannelIdentity(StrEnum):
    """How per-channel entities are identified.

    NUMBER uses the modem's row position (stable across reboots).
    ID uses the CMTS-assigned Channel ID (DOCSIS-native, can change).

    See CHANNEL_IDENTIFICATION_SPEC.md § 5.
    """

    NUMBER = "number"
    ID = "id"


class EntityPrefix(StrEnum):
    """Entity ID prefix strategy for multi-modem disambiguation.

    NONE is the default for single-modem setups.
    MODEL and IP add a disambiguator for multi-modem setups.
    """

    NONE = "none"
    MODEL = "model"
    IP = "ip"


# system_info fields consumed by dedicated sensor classes.
# Dynamic SystemInfoFieldSensor (sensor.py) and the dashboard passthrough loop
# (dev_tools.py) both skip these — the dedicated classes own them.
# When a field graduates to a dedicated sensor, add it here.
# system_uptime has no sensor of its own — it feeds Last Boot Time.
# docsis_status has no sensor of its own either — the Status sensor
# carries it as display value and attribute (#178).
# fmt: off
CONSUMED_SYSTEM_INFO_FIELDS: frozenset[str] = frozenset({
    "software_version",
    "system_uptime",
    "docsis_status",
    "downstream_channel_count",
    "upstream_channel_count",
    "total_corrected",
    "total_uncorrected",
    "rate_corrected",
    "rate_uncorrected",
})
# fmt: on

# system_info fields captured for the data layer (snapshot, event payload,
# diagnostics) but never minted as entities (#178):
# - current_time: a per-poll wall-clock reading whose past states have no
#   diagnostic value — persisting it writes a recorder row every poll.
# - hardware_version, model_name: immutable identity strings — a sensor
#   whose state cannot change is device metadata, shown natively on the
#   device page (hw_version / model in the device registry).
DISPLAY_ONLY_SYSTEM_INFO_FIELDS: frozenset[str] = frozenset(
    {
        "current_time",
        "hardware_version",
        "model_name",
    }
)
