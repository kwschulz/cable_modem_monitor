"""Migrate config entry data from v1 to v2.

v1 entries store parser-era keys (``detected_manufacturer``,
``detected_modem``, ``parser_name``, ``auth_*`` fields).  v2 entries
use catalog-based keys (``manufacturer``, ``model``, ``modem_dir``,
``variant``).

The critical step is resolving v1 display names to a catalog
directory path (``modem_dir``).  This requires walking the catalog
at migration time.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlparse

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from solentlabs.cable_modem_monitor_catalog import CATALOG_PATH
from solentlabs.cable_modem_monitor_core.catalog_manager import list_modems, list_variants
from solentlabs.cable_modem_monitor_core.config_loader import load_modem_config

from ..const import DEFAULT_HEALTH_CHECK_INTERVAL

_LOGGER = logging.getLogger(__name__)

# unique_id suffixes of v1-era entities that v2 (3.14) never creates.
# v3.13 minted a per-poll System Uptime sensor (3.14 derives uptime from
# Last Boot Time at display time, #178) and a Capture HTML button (HAR
# intake replaced it). Removed here so 3.13 upgraders don't inherit
# permanently-unavailable orphans. v2 entries created on 3.14 betas
# clear theirs via remove-and-re-add (see CHANGELOG).
V1_DISCONTINUED_UNIQUE_ID_SUFFIXES = (
    "_cable_modem_system_uptime",
    "_capture_modem_data_button",
)

# The restart button kept its v1 unique_id, so it is only an orphan on
# modems whose yaml declares no restart action; 3.14 gates creation on
# that declaration (button.py) while v3.13 created the button always.
V1_RESTART_BUTTON_SUFFIX = "_restart_button"

# v1 keys that do not exist in v2 and must be removed.
V1_STALE_KEYS = frozenset(
    {
        "parser_name",
        "detected_manufacturer",
        "detected_modem",
        "modem_choice",
        "working_url",
        "parser_selected_at",
        "docsis_version",
        "actual_model",
        "auth_strategy",
        "auth_form_config",
        "auth_hnap_config",
        "auth_url_token_config",
        "auth_discovery_status",
        "auth_discovery_failed",
        "auth_discovery_error",
        "auth_type",
        "auth_captured_response",
    }
)


async def async_migrate(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Migrate a v1 config entry to v2 format.

    Returns True on success, False if catalog resolution fails
    (user must reconfigure through the setup wizard).
    """
    old_data = dict(entry.data)
    _LOGGER.debug("v1 entry data keys: %s", sorted(old_data.keys()))

    # --- Extract v1 fields ---
    detected_mfr = old_data.get("detected_manufacturer", "")
    detected_modem = old_data.get("detected_modem", "")
    model = extract_model(detected_modem, detected_mfr)
    protocol = derive_protocol(old_data)

    # --- Resolve to catalog directory (sync I/O → executor) ---
    resolved = await hass.async_add_executor_job(resolve_modem_dir, detected_mfr, model)
    if resolved is None:
        _LOGGER.error(
            "Cannot resolve v1 modem to catalog: "
            "manufacturer=%r, model=%r (from detected_modem=%r). "
            "Please reconfigure the integration.",
            detected_mfr,
            model,
            detected_modem,
        )
        return False

    # --- Resolve variant from v1 auth strategy ---
    v1_auth = old_data.get("auth_strategy", "")
    variant = await hass.async_add_executor_job(resolve_variant, resolved.modem_dir, v1_auth, resolved.sibling_dirs)

    # --- Build v2 data (only v2 keys, no leftovers) ---
    # Use canonical catalog values, not raw v1 strings, so downstream
    # exact-match lookups (options flow, diagnostics) succeed.
    new_data: dict[str, Any] = {
        "manufacturer": resolved.manufacturer,
        "model": resolved.model,
        "modem_dir": resolved.modem_dir,
        "variant": variant,
        "user_selected_modem": detected_modem or old_data.get("modem_choice", ""),
        "entity_prefix": old_data.get("entity_prefix", "none"),
        "channel_identity": "id",  # preserve v1 DCID-based entity naming
        "host": old_data.get("host", ""),
        "username": old_data.get("username", ""),
        "password": old_data.get("password", ""),
        "protocol": protocol,
        "legacy_ssl": old_data.get("legacy_ssl", False),
        # Optimistic defaults — runtime health probing corrects false positives.
        # sensor.py gates sensor creation on these keys (default False when absent),
        # so omitting them suppresses Ping/HTTP Latency sensors post-migration.
        "supports_icmp": True,
        "supports_head": True,
        "scan_interval": old_data.get("scan_interval", 600),
        "health_check_interval": DEFAULT_HEALTH_CHECK_INTERVAL,
    }

    hass.config_entries.async_update_entry(entry, data=new_data, version=2)

    # --- Drop registry rows for entities v2 no longer creates ---
    discontinued: tuple[str, ...] = V1_DISCONTINUED_UNIQUE_ID_SUFFIXES
    if not await hass.async_add_executor_job(modem_declares_restart, resolved.modem_dir, variant):
        discontinued += (V1_RESTART_BUTTON_SUFFIX,)

    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.unique_id.endswith(discontinued):
            _LOGGER.info("Removing discontinued v1 entity %s", reg_entry.entity_id)
            registry.async_remove(reg_entry.entity_id)

    _LOGGER.info(
        "Migrated entry %s to v2: manufacturer=%s, model=%s, modem_dir=%s",
        entry.entry_id,
        resolved.manufacturer,
        resolved.model,
        resolved.modem_dir,
    )
    return True


# ------------------------------------------------------------------
# Pure helpers — testable without HA
# ------------------------------------------------------------------


def extract_model(detected_modem: str, manufacturer: str) -> str:
    """Extract model name from v1 display name.

    v1 stores ``detected_modem`` as ``"{manufacturer} {model}"``
    (e.g., ``"ARRIS SB8200"``).  Strip the manufacturer prefix to
    get the bare model string.
    """
    if manufacturer and detected_modem.startswith(manufacturer):
        return detected_modem[len(manufacturer) :].strip()
    # Fallback: take everything after the last space
    parts = detected_modem.rsplit(" ", 1)
    return parts[-1] if parts else detected_modem


def derive_protocol(data: dict[str, Any]) -> str:
    """Derive protocol from v1 entry data.

    Priority:
        1. Parse scheme from ``working_url`` if present.
        2. ``legacy_ssl`` is True → ``"https"``.
        3. Default ``"http"``.
    """
    working_url = data.get("working_url", "")
    if working_url:
        try:
            parsed = urlparse(working_url)
            if parsed.scheme in ("http", "https"):
                return str(parsed.scheme)
        except Exception:
            pass

    if data.get("legacy_ssl"):
        return "https"

    return "http"


class ResolvedModem(NamedTuple):
    """Result of resolving a v1 modem to its catalog entry.

    Contains canonical catalog values, not the raw v1 strings.
    """

    modem_dir: str
    manufacturer: str
    model: str
    sibling_dirs: list[Path]


def resolve_modem_dir(manufacturer: str, model: str) -> ResolvedModem | None:
    """Resolve v1 manufacturer + model to a catalog entry.

    Walks the catalog and tries three match strategies:

        1. Exact manufacturer + model (case-insensitive).
        2. Exact manufacturer + model alias (case-insensitive).
        3. Model-only match — if exactly one catalog modem has this
           model name or alias regardless of manufacturer.  Handles
           cases where the manufacturer string changed between
           versions (e.g., ``"Arris/CommScope"`` → ``"Arris"``).

    Returns:
        ``ResolvedModem`` with canonical catalog values,
        or ``None`` if no match found.
    """
    summaries = list_modems(CATALOG_PATH)
    mfr_lower = manufacturer.lower()
    model_lower = model.lower()

    # Pass 1: exact manufacturer + model
    for s in summaries:
        if s.manufacturer.lower() == mfr_lower and s.model.lower() == model_lower:
            return ResolvedModem(_relative_dir(s.path), s.manufacturer, s.model, s.sibling_dirs)

    # Pass 2: exact manufacturer + model alias
    for s in summaries:
        if s.manufacturer.lower() == mfr_lower and _has_alias(s, model_lower):
            return ResolvedModem(_relative_dir(s.path), s.manufacturer, s.model, s.sibling_dirs)

    # Pass 3: model-only (handles manufacturer renames)
    matches = [s for s in summaries if s.model.lower() == model_lower or _has_alias(s, model_lower)]
    if len(matches) == 1:
        s = matches[0]
        return ResolvedModem(_relative_dir(s.path), s.manufacturer, s.model, s.sibling_dirs)

    return None


def resolve_variant(
    modem_dir_relative: str,
    v1_auth_strategy: str,
    sibling_dirs: list[Path] | None = None,
) -> str | None:
    """Resolve the correct variant name from the v1 auth strategy.

    Walks the variant list for the resolved modem directory and
    matches the v1 ``auth_strategy`` field to find the correct
    named variant.

    Single-variant modems always return ``None`` (default).
    Multi-variant modems return the variant name whose auth strategy
    matches the v1 entry, falling back to ``None`` if no match.

    Args:
        modem_dir_relative: Catalog-relative modem directory path
            (e.g., ``"netgear/cm1200"``).
        v1_auth_strategy: The ``auth_strategy`` value from the v1
            config entry (e.g., ``"basic"``, ``"none"``, ``""``).

    Returns:
        Variant name string (e.g., ``"basic"``), or ``None`` for
        the default variant.
    """
    full_path = CATALOG_PATH / modem_dir_relative
    variants = list_variants(full_path, sibling_dirs=sibling_dirs)

    if len(variants) <= 1:
        return None

    if not v1_auth_strategy:
        return None

    for v in variants:
        if v.auth_strategy == v1_auth_strategy:
            return v.name

    _LOGGER.warning(
        "No variant matches v1 auth_strategy %r in %s — using default",
        v1_auth_strategy,
        modem_dir_relative,
    )
    return None


def modem_declares_restart(modem_dir_relative: str, variant: str | None) -> bool:
    """Whether the resolved catalog entry declares a restart action."""
    # Mirrors Orchestrator.supports_restart, which is what button.py
    # gates creation on.  An unreadable config returns True: never
    # delete a user's registry row on the strength of a failed read.
    modem_yaml = CATALOG_PATH / modem_dir_relative / (f"modem-{variant}.yaml" if variant else "modem.yaml")
    try:
        actions = load_modem_config(modem_yaml).actions
    except Exception:
        _LOGGER.warning("Could not read %s — keeping the Restart button", modem_yaml)
        return True
    return actions is not None and actions.restart is not None


def _has_alias(summary: Any, model_lower: str) -> bool:
    """Check if a modem summary has a matching model alias."""
    return any(alias.lower() == model_lower for alias in summary.model_aliases)


def _relative_dir(path: Path) -> str:
    """Return catalog-relative directory path as a string."""
    return str(path.relative_to(CATALOG_PATH))
