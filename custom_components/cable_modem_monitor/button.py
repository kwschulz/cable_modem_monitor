"""Button platform for Cable Modem Monitor.

Provides action buttons for modem management:

    RestartModemButton     Restart modem with post-restart monitoring
    UpdateModemDataButton  Trigger immediate data + health refresh
    ResetEntitiesButton    Clear entity registry and reload

Buttons use plain ButtonEntity (not CoordinatorEntity) — they trigger
actions rather than consuming coordinator data.

See ENTITY_MODEL_SPEC.md § Buttons and HA_ADAPTER_SPEC.md § Restart
Lifecycle.
"""

from __future__ import annotations

import functools
import logging
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager

from homeassistant.components.button import ButtonEntity
from homeassistant.const import CONF_HOST, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from solentlabs.cable_modem_monitor_catalog import CATALOG_PATH
from solentlabs.cable_modem_monitor_core.config_loader import load_modem_config
from solentlabs.cable_modem_monitor_core.orchestration.models import (
    RestartResult,
)

from .config_flow_helpers import detect_probes
from .const import (
    CONF_ENTITY_PREFIX,
    CONF_LEGACY_SSL,
    CONF_MODEM_DIR,
    CONF_SUPPORTS_HEAD,
    CONF_SUPPORTS_ICMP,
    CONF_VARIANT,
    DOMAIN,
)
from .coordinator import ActiveOperation, CableModemConfigEntry
from .lib.utils import get_device_name
from .services import async_request_modem_refresh

_LOGGER = logging.getLogger(__name__)

# Buttons do no per-entity I/O — no throttling needed.
PARALLEL_UPDATES = 0

# Notification IDs (single ID per action so updates replace previous)
_NOTIFY_RESTART = "cable_modem_restart"
_NOTIFY_RESET = "cable_modem_reset"
_NOTIFY_UPDATE = "cable_modem_update"


# ------------------------------------------------------------------
# Operation mutex
# ------------------------------------------------------------------


class OperationInProgressError(HomeAssistantError):
    """Raised when a destructive button operation is already running."""

    def __init__(self, active: ActiveOperation) -> None:
        """Store which operation holds the mutex."""
        super().__init__(f"operation '{active}' already in progress")
        self.active: ActiveOperation = active


class OperationUnavailableError(HomeAssistantError):
    """Raised when runtime_data is gone; the entry is unloading."""


@contextmanager
def hold_active_operation(
    entry: CableModemConfigEntry,
    op: ActiveOperation,
) -> Iterator[None]:
    """Hold the destructive-operation mutex for the handler's duration."""
    # See HA_ADAPTER_SPEC.md § Operation Mutex. Sync contextmanager on
    # purpose: set/clear is synchronous, the body is where awaits happen.
    runtime = entry.runtime_data
    if runtime is None:
        raise OperationUnavailableError("runtime_data unavailable; entry is unloading")
    if runtime.active_operation is not None:
        raise OperationInProgressError(runtime.active_operation)
    runtime.active_operation = op
    try:
        yield
    finally:
        # Re-read; the entry may have unloaded or reloaded during the body.
        runtime = entry.runtime_data
        if runtime is not None:
            runtime.active_operation = None


# ------------------------------------------------------------------
# Base class
# ------------------------------------------------------------------


class _ButtonBase(ButtonEntity):
    """Base class for modem buttons.

    Uses plain ButtonEntity — buttons trigger actions, they don't
    consume coordinator data.  Device linkage via identifiers.
    """

    _attr_has_entity_name = True

    def __init__(self, entry: CableModemConfigEntry) -> None:
        """Initialize the button with config entry."""
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=get_device_name(
                entry.data.get(CONF_ENTITY_PREFIX, "default"),
                model=entry.runtime_data.modem_identity.model,
                host=entry.data.get("host", ""),
            ),
        )

    async def _notify(
        self,
        title: str,
        message: str,
        notification_id: str,
    ) -> None:
        """Send a persistent notification."""
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": title,
                "message": message,
                "notification_id": notification_id,
            },
        )


# ------------------------------------------------------------------
# Platform setup
# ------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CableModemConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Cable Modem Monitor button entities."""
    orchestrator = entry.runtime_data.orchestrator

    entities: list[ButtonEntity] = [
        UpdateModemDataButton(entry),
        ResetEntitiesButton(entry),
    ]

    if orchestrator.supports_restart:
        entities.append(RestartModemButton(entry))

    async_add_entities(entities)


# ------------------------------------------------------------------
# Button entities
# ------------------------------------------------------------------


class RestartModemButton(_ButtonBase):
    """Button to dispatch a modem restart command.

    Only created when modem.yaml declares actions.restart. Calls
    ``orchestrator.restart()`` on an executor thread — a one-shot
    command that returns in 2–5 seconds after authenticating,
    sending the reboot instruction, and triggering a recovery
    window.

    See HA_ADAPTER_SPEC.md § Restart Lifecycle.
    """

    def __init__(self, entry: CableModemConfigEntry) -> None:
        """Initialize the restart button."""
        super().__init__(entry)
        self._attr_name = "Restart Modem"
        self._attr_unique_id = f"{entry.entry_id}_restart_button"
        self._attr_icon = "mdi:restart"

    async def async_press(self) -> None:
        """Handle the button press — dispatch the restart command."""
        runtime = self._entry.runtime_data
        orchestrator = runtime.orchestrator
        model = runtime.modem_identity.model

        # No state writes in this handler: a ButtonEntity's state is its
        # last-pressed timestamp, and every write during a press renders
        # in the logbook as another "Pressed" entry attributed to the
        # pressing user. The mutex alone refuses overlapping presses;
        # no ``recovery_active`` check. A user who sees a flakey modem
        # after a restart is allowed to retry once this press finishes.
        try:
            with hold_active_operation(self._entry, "restart"):
                _LOGGER.info("Modem restart initiated [%s]", model)
                result: RestartResult = await self.hass.async_add_executor_job(orchestrator.restart)
        except OperationInProgressError as err:
            _LOGGER.warning(
                "Operation '%s' already in progress [%s] — ignoring restart press",
                err.active,
                model,
            )
            return

        if result.success:
            _LOGGER.info("Restart command sent [%s] in %.1fs", model, result.elapsed_seconds)
            await self._notify(
                f"Restart Command Sent [{model}]",
                f"{model} restart command dispatched in {result.elapsed_seconds:.1f} seconds.",
                _NOTIFY_RESTART,
            )
        else:
            _LOGGER.warning("Modem restart failed [%s]: %s", model, result.error)
            await self._notify(
                f"Modem Restart Failed [{model}]",
                f"{model} restart did not dispatch: {result.error}",
                _NOTIFY_RESTART,
            )

        # Trigger an immediate refresh so the dashboard reacts to the
        # post-reboot state (typically UNREACHABLE while the modem
        # comes back).
        await runtime.data_coordinator.async_request_refresh()

        # Surface the failure to the caller once state has been refreshed.
        # The notification above stays as the persistent record; this is
        # what an automation or the UI actually sees as the action result.
        if not result.success:
            raise HomeAssistantError(f"{model} restart did not dispatch: {result.error}")


class UpdateModemDataButton(_ButtonBase):
    """Button to manually trigger modem data update.

    Triggers health probe first, then data collection — so the
    snapshot includes fresh health info.
    """

    def __init__(self, entry: CableModemConfigEntry) -> None:
        """Initialize the update data button."""
        super().__init__(entry)
        self._attr_name = "Update Modem Data"
        self._attr_unique_id = f"{entry.entry_id}_update_data_button"
        self._attr_icon = "mdi:update"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_press(self) -> None:
        """Handle the button press — refresh health then data."""
        runtime = self._entry.runtime_data
        model = runtime.modem_identity.model

        # Refuse while a destructive operation runs; never sets the
        # mutex itself (a refresh is not destructive).
        if runtime.active_operation is not None:
            _LOGGER.warning(
                "Operation '%s' already in progress [%s] — ignoring update press",
                runtime.active_operation,
                model,
            )
            return

        _LOGGER.info("Manual data update triggered [%s]", model)

        await async_request_modem_refresh(runtime)

        success = runtime.data_coordinator.last_update_success
        if not success:
            _LOGGER.warning("Manual data update failed [%s]", model)


class ResetEntitiesButton(_ButtonBase):
    """Button to reset all entities and reload integration.

    Re-detects probe capabilities (ICMP, HTTP HEAD), removes entities
    from the registry, then reloads the integration to recreate them.
    Unique IDs are preserved so entity_ids, history, and automations
    survive the reset.
    """

    def __init__(self, entry: CableModemConfigEntry) -> None:
        """Initialize the reset entities button."""
        super().__init__(entry)
        self._attr_name = "Reset Entities"
        self._attr_unique_id = f"{entry.entry_id}_reset_entities_button"
        self._attr_icon = "mdi:refresh"
        self._attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        """Handle button press — re-detect probes, remove entities, reload."""
        model = self._entry.runtime_data.modem_identity.model

        # A second press while the first is still tearing down entities
        # would fire coordinator updates against unloaded entities
        # (UC-80). Same no-state-writes rule as the restart button.
        try:
            with hold_active_operation(self._entry, "reset"):
                await self._reset_entities(model)
        except OperationInProgressError as err:
            _LOGGER.warning(
                "Operation '%s' already in progress [%s] — ignoring reset press",
                err.active,
                model,
            )
            return

    async def _reset_entities(self, model: str) -> None:
        """Re-detect probes, remove entities, reload, notify."""
        # Re-detect probe capabilities
        updated = await self._redetect_probes()

        # Remove all entities
        entity_reg = er.async_get(self.hass)
        entries_to_remove = [
            entity_entry
            for entity_entry in entity_reg.entities.values()
            if (entity_entry.platform == DOMAIN and entity_entry.config_entry_id == self._entry.entry_id)
        ]
        entities_to_remove = [entry.entity_id for entry in entries_to_remove]

        # Breakdown by platform so the count reconciles with each
        # platform's own "Created N entities" log on re-init.
        breakdown = Counter(entry.domain for entry in entries_to_remove)
        breakdown_str = ", ".join(f"{n} {domain}" for domain, n in sorted(breakdown.items()))
        _LOGGER.info(
            "Removing %d entities for reset [%s]: %s",
            len(entities_to_remove),
            model,
            breakdown_str,
        )
        for entity_id in entities_to_remove:
            entity_reg.async_remove(entity_id)

        # Update config entry with new probe results and reload
        if updated:
            self.hass.config_entries.async_update_entry(
                self._entry,
                data={**self._entry.data, **updated},
            )

        await self.hass.config_entries.async_reload(self._entry.entry_id)

        probe_msg = ""
        if updated:
            probe_msg = (
                f"\n\nProbe re-detection: ICMP={'yes' if updated.get(CONF_SUPPORTS_ICMP) else 'no'}, "
                f"HEAD={'yes' if updated.get(CONF_SUPPORTS_HEAD) else 'no'}"
            )

        _LOGGER.info("Entity reset complete [%s] — removed %d entities and reloaded", model, len(entities_to_remove))

        await self._notify(
            "Entity Reset Complete",
            f"Removed {len(entities_to_remove)} entities and reloaded the integration.{probe_msg}",
            _NOTIFY_RESET,
        )

    async def _redetect_probes(self) -> dict[str, bool] | None:
        """Re-detect ICMP and HTTP HEAD support.

        Uses the shared ``detect_probes()`` helper (same logic as the
        config flow validation pipeline). Returns updated config dict
        or None if modem is unreachable.
        """
        data = self._entry.data
        host = data[CONF_HOST]
        protocol = data.get("protocol", "http")
        base_url = f"{protocol}://{host}"
        legacy_ssl = data.get(CONF_LEGACY_SSL, False)

        # Load modem.yaml for health config defaults
        modem_dir = CATALOG_PATH / data[CONF_MODEM_DIR]
        variant = data.get(CONF_VARIANT)
        modem_yaml = modem_dir / f"modem-{variant}.yaml" if variant else modem_dir / "modem.yaml"

        try:
            modem_config = await self.hass.async_add_executor_job(load_modem_config, modem_yaml)
        except Exception:
            _LOGGER.warning("Could not load modem config for probe re-detection")
            return None

        try:
            probes = await self.hass.async_add_executor_job(
                functools.partial(detect_probes, host, base_url, modem_config, legacy_ssl=legacy_ssl)
            )
        except Exception:
            _LOGGER.warning("Probe re-detection failed — modem may be unreachable")
            return None

        _LOGGER.info(
            "Probe re-detection: supports_icmp=%s, supports_head=%s",
            probes["supports_icmp"],
            probes["supports_head"],
        )
        return {
            CONF_SUPPORTS_ICMP: probes["supports_icmp"],
            CONF_SUPPORTS_HEAD: probes["supports_head"],
        }
