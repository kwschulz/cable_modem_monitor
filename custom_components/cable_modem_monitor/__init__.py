"""The Cable Modem Monitor integration.

Home Assistant adapter layer for monitoring cable modem signal quality
and health.  Consumes Core (orchestration, parsing, auth) and Catalog
(modem configs, parsers) packages — all modem-specific logic lives
there.

Entry points:
    async_setup_entry: Called by HA when integration is configured.
    async_unload_entry: Called when integration is removed or reloaded.

See Also:
    HA_ADAPTER_SPEC.md for the full startup/unload specification.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import timedelta
from importlib.metadata import version as pkg_version
from typing import Any

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from solentlabs.cable_modem_monitor_catalog import CATALOG_PATH
from solentlabs.cable_modem_monitor_core.config_loader import (
    load_modem_config,
    load_parser_config,
)
from solentlabs.cable_modem_monitor_core.orchestration import (
    HealthMonitor,
    Orchestrator,
    apply_credential_encoding,
    create_orchestrator,
)
from solentlabs.cable_modem_monitor_core.orchestration.models import (
    HealthInfo,
    ModemIdentity,
    ModemSnapshot,
)
from solentlabs.cable_modem_monitor_core.orchestration.signals import (
    CollectorSignal,
    ConnectionStatus,
    HealthStatus,
)
from solentlabs.cable_modem_monitor_core.post_processor import (
    load_post_processor,
)

from .channel_bond_notifier import (
    ChannelTotals,
    evaluate,
    format_change_message,
    format_onboarding_message,
)
from .channel_bond_storage import (
    BondState,
    async_load_bond_state,
    async_remove_bond_state,
    async_save_bond_state,
)
from .const import (
    CONF_CHANNEL_IDENTITY,
    CONF_CHANNEL_ONBOARDING_ELIGIBLE,
    CONF_CREDENTIAL_ENCODING,
    CONF_CREDENTIAL_FIELD,
    CONF_ENTITY_PREFIX,
    CONF_HEALTH_CHECK_INTERVAL,
    CONF_LEGACY_SSL,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_MODEM_DIR,
    CONF_PROTOCOL,
    CONF_SCAN_INTERVAL,
    CONF_SUPPORTS_HEAD,
    CONF_SUPPORTS_ICMP,
    CONF_VARIANT,
    DEFAULT_HEALTH_CHECK_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PLATFORMS,
    VERSION,
    ChannelIdentity,
)
from .coordinator import CableModemConfigEntry, CableModemRuntimeData
from .core.log_buffer import setup_log_buffer
from .lib.utils import get_device_name
from .mapping_manager import ChannelMap, build_channel_map
from .migrations import async_run_migrations
from .recovery_adapter import attach_recovery_cadence_listener
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

# Must match ConfigFlow.VERSION in config_flow.py
_CURRENT_VERSION = 2


def _local_dev_suffix() -> str:
    """Startup-log marker for local dev installs (CMM_LOCAL_DEV env var)."""
    # The manifest version only bumps at release time, so a bind-mounted
    # dev tree otherwise logs itself as the previous beta and reads as a
    # released install. docker-compose.test.yml sets the variable; HACS
    # installs never have it.
    return " (local dev)" if os.getenv("CMM_LOCAL_DEV") else ""


def _get_package_versions() -> str:
    """Build package version string for the startup log.

    Uses importlib.metadata which does file I/O — call from executor.
    """
    parts = []
    for pkg, label in (
        ("solentlabs-cable-modem-monitor-core", "core"),
        ("solentlabs-cable-modem-monitor-catalog", "catalog"),
    ):
        try:
            parts.append(f"{label}: v{pkg_version(pkg)}")
        except Exception:  # noqa: BLE001
            parts.append(f"{label}: not installed")
    return ", ".join(parts)


async def _check_channel_bond_change(
    hass: HomeAssistant,
    entry: CableModemConfigEntry,
    snapshot: ModemSnapshot,
    orchestrator: Orchestrator,
    model: str,
) -> None:
    """Detect channel-bond total changes and fire the appropriate notification.

    Silent on the first post-upgrade poll (no retroactive onboarding) and
    while a recovery window is open (transient count flux is expected).
    Persists the baseline to a dedicated ``Store`` — not entry data —
    so baseline updates don't trip the integration's update listener.
    """
    modem_data = snapshot.modem_data
    if not modem_data:
        return
    system_info = modem_data.get("system_info", {})
    ds = system_info.get("downstream_channel_count")
    us = system_info.get("upstream_channel_count")
    if not isinstance(ds, int) or not isinstance(us, int):
        return

    current = ChannelTotals(downstream=ds, upstream=us)
    stored = await async_load_bond_state(hass, entry.entry_id)
    onboarding_eligible = bool(entry.data.get(CONF_CHANNEL_ONBOARDING_ELIGIBLE, False))

    action = evaluate(
        current=current,
        stored=stored,
        onboarding_eligible=onboarding_eligible,
        recovery_active=orchestrator.recovery_active,
    )

    if action == "none":
        return

    new_state = BondState(baseline_downstream=current.downstream, baseline_upstream=current.upstream)
    await async_save_bond_state(hass, entry.entry_id, new_state)

    if action == "silent_init":
        return

    if action == "onboarding":
        title = "Cable Modem Monitor: Modem online"
        message = format_onboarding_message(model=model, current=current)
        notification_id = f"cable_modem_monitor_onboarding_{entry.entry_id}"
    else:
        # "change" — evaluate only returns this when stored is not None.
        assert stored is not None
        title = "Cable Modem Monitor: Channel bond changed"
        message = format_change_message(model=model, prior=stored, current=current)
        notification_id = f"cable_modem_monitor_channel_change_{entry.entry_id}"

    await hass.services.async_call(
        "persistent_notification",
        "create",
        {"title": title, "message": message, "notification_id": notification_id},
    )


def _rebuild_channel_map(
    entry: ConfigEntry,
    snapshot: ModemSnapshot,
    identity_mode: ChannelIdentity,
) -> None:
    """Rebuild channel map on runtime_data after a poll.

    No-op when runtime_data is not yet set (first poll, before Step 9)
    or when the snapshot has no modem_data.
    """
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None or snapshot.modem_data is None:
        return
    runtime.channel_map = build_channel_map(
        snapshot.modem_data.get("downstream", []),
        snapshot.modem_data.get("upstream", []),
        identity_mode,
    )


async def _announce_auth_stop(
    hass: HomeAssistant,
    entry: ConfigEntry,
    snapshot: ModemSnapshot,
    orchestrator: Orchestrator,
    model: str,
    lockout_reported: list[bool],
) -> None:
    """Tell the user why polling stopped when the auth breaker opens."""
    # Reauth is HA's surface for a credential lockout: a
    # "Reauthentication required" notification with the fix form.
    # https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/reauthentication-flow/
    # Contract: HA_ADAPTER_SPEC.md § Reauth Flow, UC-81, UC-81a, UC-87.
    # Core integrations trigger this by raising ConfigEntryAuthFailed,
    # but that also flips every entity unavailable; our Status sensor is
    # the sole outage announcer (#178), so we call the API directly.
    if snapshot.connection_status is not ConnectionStatus.AUTH_FAILED:
        return
    # Breaker open — not a lone AUTH_FAILED poll — is the trigger:
    # definitive credential rejections trip it immediately (UC-87),
    # stale-session failures only after 6 in a row (UC-81), so
    # transient flakes never interrupt the user.
    diagnostics = orchestrator.diagnostics()
    if not diagnostics.circuit_breaker_open:
        return
    # SignalPolicy.apply collapses four signals into AUTH_FAILED, and
    # the blocked polls that follow carry no signal at all, so the
    # latched trip reason is the only thing that tells the routes apart
    # (UC-81a).
    if diagnostics.circuit_trip_signal is CollectorSignal.AUTH_LOCKOUT:
        await _notify_auth_lockout(hass, entry, model, lockout_reported)
        return
    # HA dedupes inside async_start_reauth too; this guard keeps the
    # WARNING to one line per lockout instead of one per poll.
    if any(entry.async_get_active_flows(hass, {SOURCE_REAUTH})):
        return
    _LOGGER.warning(
        "Auth circuit breaker open [%s] — starting reauthentication flow",
        model,
    )
    entry.async_start_reauth(hass)


async def _notify_auth_lockout(
    hass: HomeAssistant,
    entry: ConfigEntry,
    model: str,
    lockout_reported: list[bool],
) -> None:
    """Notify about a firmware lockout without inviting a login attempt."""
    # Never the reauth form: submitting it calls validate_connection, a
    # real login, and the config flow is stateless by design (UC-86), so
    # the open breaker does not stop it. Prompting a locked-out user
    # invites hand-fed attempts at a modem that locked itself to stop
    # them (#117). The message says nothing about credentials for the
    # same reason.
    if lockout_reported[0]:
        return
    _LOGGER.warning(
        "Auth circuit breaker open [%s] — firmware lockout, not prompting for credentials",
        model,
    )
    # Waiting alone never resumes polling: the breaker clears only by
    # orchestrator reconstruction (ORCHESTRATION_SPEC § Auth Circuit
    # Breaker, "Recovery paths"), so the reload is part of the remedy.
    await hass.services.async_call(
        "persistent_notification",
        "create",
        {
            "title": "Cable Modem Monitor: Modem locked out logins",
            "message": (
                f"{model} reported a login lockout, so polling has stopped. "
                f"Wait for the modem to clear it, or power cycle the modem, "
                f"then reload the integration to resume polling. If it keeps "
                f"happening, download diagnostics (Settings > Devices & "
                f"Services > Cable Modem Monitor > ...) and open an issue."
            ),
            "notification_id": f"cable_modem_monitor_auth_lockout_{entry.entry_id}",
        },
    )
    # Latch only after the notification landed — a failed call must be
    # retried on the next poll, not swallowed into permanent silence.
    lockout_reported[0] = True


def _log_availability_transition(
    snapshot: ModemSnapshot,
    model: str,
    reported_unavailable: list[bool],
) -> None:
    """Log one line per availability edge rather than one per poll."""
    # The coordinator never raises (Core returns snapshots carrying an
    # error field instead), so HA's built-in once-per-transition
    # unavailable logging never engages and we track the edge ourselves.
    if snapshot.error:
        if not reported_unavailable[0]:
            reported_unavailable[0] = True
            _LOGGER.warning(
                "Modem unavailable [%s] — %s",
                model,
                snapshot.connection_status.value,
            )
    elif reported_unavailable[0]:
        reported_unavailable[0] = False
        _LOGGER.info("Modem available again [%s]", model)


def _build_snapshot_payload(snapshot: ModemSnapshot) -> dict[str, Any]:
    """Build the event bus payload from a ModemSnapshot.

    Full snapshot — PII stripping is the consumer's responsibility (CMMT).
    """
    return snapshot.to_event_payload().model_dump()


async def async_migrate_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Migrate a config entry to the current version.

    Called by HA when entry.version < ConfigFlow.VERSION.  Delegates
    to the migration registry which chains handlers in sequence.
    """
    _LOGGER.info(
        "Migrating config entry %s from version %d to %d",
        entry.entry_id,
        entry.version,
        _CURRENT_VERSION,
    )
    return await async_run_migrations(hass, entry, _CURRENT_VERSION)


def _attach_health_sync_listeners(
    hass: HomeAssistant,
    health_coordinator: DataUpdateCoordinator[HealthInfo],
    data_coordinator: DataUpdateCoordinator[ModemSnapshot],
    model: str,
) -> None:
    """Keep the health and data coordinators from outvoting each other with stale state.

    Two directions, one shared piece of state:

    Health → data: when health transitions from data-path-down to
    data-path-up (``HealthStatus.data_path_up``), schedules an
    immediate data poll so recovery latency is bounded by the
    health-check interval rather than the scan interval (~10m). The
    edge is a boolean derived per reading, not a pair of enumerated
    states — enumerating transitions missed DEGRADED (2026-06-29) and
    then a transitional ICMP_BLOCKED that laundered the down state
    (2026-07-12). Up→up steps (RESPONSIVE ↔ ICMP_BLOCKED) never fire,
    so ping-blocking setups get no spurious polls. The Core
    orchestrator independently clears connectivity backoff on
    data-path-up health — this listener just ensures the poll happens
    promptly rather than waiting for the next scan.

    Data → health: when a poll succeeds while health still reads
    UNRESPONSIVE or DEGRADED, schedules an immediate health refresh. A
    completed collection is live proof the data path is up; without
    the refresh, the stale probe result tops the Status cascade for up
    to a full health interval, displaying "Unresponsive" over a modem
    that is actively serving polls (UC-59a's principle — stale
    evidence must not outvote a live signal — in reverse). The refresh
    is cheap: the fresh collection evidence puts the TCP/HEAD skip
    gate up, so it is an ICMP-only probe.

    The shared flag: that health refresh flips down → RESPONSIVE,
    which would trip the health → data direction into forcing a poll
    seconds after the successful poll that started the exchange — a
    redundant login on session-limited modems. The data listener marks
    the recovery as poll-proven; the next probe result consumes the
    mark, suppressing exactly that one forced poll.
    """
    # Start "up" so the first successful health check is not
    # misinterpreted as a recovery — avoids a spurious immediate poll
    # right after the initial data fetch.
    previous_up: list[bool] = [True]
    # True while the current down state has been contradicted by a
    # successful poll — the recovery is already proven, data is fresh.
    poll_proven: list[bool] = [False]

    @callback
    def _on_health_update() -> None:
        if health_coordinator.data is None:
            return
        current_up = health_coordinator.data.health_status.data_path_up
        was_up = previous_up[0]
        previous_up[0] = current_up
        # Any live probe result supersedes the poll-proven mark; it may
        # suppress only the recovery edge of the probe that follows it.
        proven = poll_proven[0]
        poll_proven[0] = False
        if not was_up and current_up:
            if proven:
                _LOGGER.debug(
                    "Health recovery [%s] — poll already succeeded, skipping forced poll",
                    model,
                )
                return
            _LOGGER.info("Health recovery [%s] — scheduling immediate poll", model)
            hass.async_create_task(data_coordinator.async_request_refresh())

    @callback
    def _on_data_update() -> None:
        snapshot = data_coordinator.data
        if snapshot is None or health_coordinator.data is None:
            return
        if snapshot.connection_status is not ConnectionStatus.ONLINE:
            return
        # Only a data-path-down claim is contradicted by a successful
        # poll. UNKNOWN is excluded — probes are not applicable, so a
        # refresh adds no information and would fire on every poll for
        # probe-less configurations.
        health_status = health_coordinator.data.health_status
        if health_status.data_path_up or health_status is HealthStatus.UNKNOWN:
            return
        poll_proven[0] = True
        _LOGGER.info(
            "Health refresh [%s] — successful poll contradicts stale %s reading",
            model,
            health_coordinator.data.health_status.value,
        )
        hass.async_create_task(health_coordinator.async_request_refresh())

    health_coordinator.async_add_listener(_on_health_update)
    data_coordinator.async_add_listener(_on_data_update)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register integration-global services at component load.

    Bronze action-setup: services are registered once here, not per
    config entry, so they exist even before any entry is set up (and a
    re-added entry keeps them). Handlers resolve their target config
    entry at call time. See HA_ADAPTER_SPEC.md § Services.
    """
    async_register_services(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CableModemConfigEntry,
) -> bool:
    """Set up Cable Modem Monitor from a config entry.

    Follows the startup sequence defined in HA_ADAPTER_SPEC.md.
    Steps 1-5 (config loading, Core component creation) run in an
    executor thread because they involve file I/O.
    """
    pkg_versions = await hass.async_add_executor_job(_get_package_versions)
    _LOGGER.info(
        "Cable Modem Monitor v%s%s starting [%s %s] — %s",
        VERSION,
        _local_dev_suffix(),
        entry.data.get(CONF_MANUFACTURER, ""),
        entry.data.get(CONF_MODEL, ""),
        pkg_versions,
    )

    # Initialize log buffer before any Core calls so auth, parsing,
    # action, and health logs are captured from the first poll.
    setup_log_buffer(hass)

    # Resolve effective polling intervals (options override data)
    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL,
        entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )
    health_check_interval = entry.options.get(
        CONF_HEALTH_CHECK_INTERVAL,
        entry.data.get(CONF_HEALTH_CHECK_INTERVAL, DEFAULT_HEALTH_CHECK_INTERVAL),
    )

    # Steps 1-5: Load config and create Core components (sync I/O)
    try:
        orchestrator, health_monitor, modem_identity = await hass.async_add_executor_job(
            _create_core_components, entry.data
        )
    except Exception as err:
        # A missing or corrupt catalog config is a permanent
        # misconfiguration — HA's retry won't fix it — so raise
        # ConfigEntryError (not ConfigEntryNotReady). The modem being
        # unreachable is a different case: the first poll never raises
        # and setup proceeds with deferred entities (see § Data Coordinator).
        _LOGGER.exception("Failed to load modem configuration from catalog")
        raise ConfigEntryError(f"Failed to load modem configuration from catalog: {err}") from err

    # Step 6: Create data DataUpdateCoordinator
    host = entry.data[CONF_HOST]
    model = entry.data.get(CONF_MODEL, host)
    coordinator_label = f"{model} ({host})" if model != host else host

    identity_mode = ChannelIdentity(entry.data.get(CONF_CHANNEL_IDENTITY, ChannelIdentity.ID))

    # Mutable cells so the edge survives across polls (Silver
    # log-when-unavailable); see _log_availability_transition. The
    # lockout cell resets on entry reload, which is also the only thing
    # that clears the breaker, so it tracks it exactly.
    reported_unavailable = [False]
    lockout_reported = [False]

    async def _async_update_data() -> ModemSnapshot:
        snapshot = await hass.async_add_executor_job(orchestrator.get_modem_data)
        _log_availability_transition(snapshot, model, reported_unavailable)
        await _announce_auth_stop(hass, entry, snapshot, orchestrator, model, lockout_reported)
        _rebuild_channel_map(entry, snapshot, identity_mode)
        await _check_channel_bond_change(hass, entry, snapshot, orchestrator, model)
        hass.bus.async_fire(
            "cable_modem_monitor_data_updated",
            _build_snapshot_payload(snapshot),
        )
        return snapshot

    data_coordinator = DataUpdateCoordinator[ModemSnapshot](
        hass,
        _LOGGER,
        name=f"Cable Modem {coordinator_label}",
        update_method=_async_update_data,
        update_interval=(timedelta(seconds=scan_interval) if scan_interval > 0 else None),
        config_entry=entry,
    )

    # Step 6a: Install the recovery cadence listener — switches the
    # data coordinator's interval to 30s while a recovery window is
    # open, and back to the configured interval when the window
    # closes. Single point of HA-side contact for Core's recovery
    # observer; other modules don't subscribe.
    attach_recovery_cadence_listener(hass, entry, orchestrator, data_coordinator)

    # Step 7: Create health DataUpdateCoordinator (conditional)
    health_coordinator: DataUpdateCoordinator[HealthInfo] | None = None
    if health_monitor is not None:

        async def _async_update_health() -> HealthInfo:
            return await hass.async_add_executor_job(health_monitor.ping)

        health_coordinator = DataUpdateCoordinator[HealthInfo](
            hass,
            _LOGGER,
            name=f"Cable Modem {coordinator_label} Health",
            update_method=_async_update_health,
            update_interval=(timedelta(seconds=health_check_interval) if health_check_interval > 0 else None),
            config_entry=entry,
        )

    # Step 7b: Health sync listeners — immediate poll on health recovery,
    # immediate health refresh when a successful poll contradicts stale health
    if health_coordinator is not None:
        _attach_health_sync_listeners(hass, health_coordinator, data_coordinator, model)

    # Step 8: First poll (always runs, even when polling is disabled)
    await data_coordinator.async_config_entry_first_refresh()

    # Log first-poll result at HA level (Core logs parse details under solentlabs.*)
    snapshot = data_coordinator.data
    if snapshot and snapshot.modem_data:
        _LOGGER.info("First poll complete [%s] — data received", model)
    else:
        status = snapshot.connection_status.value if snapshot else "unknown"
        _LOGGER.info("First poll complete [%s] — no data (%s)", model, status)

    if health_coordinator is not None:
        await health_coordinator.async_config_entry_first_refresh()
        _LOGGER.info("Health monitoring started [%s] — %s", model, _format_interval(health_check_interval))

    # Step 9: Store runtime data (with initial channel map from first poll)
    modem_data = snapshot.modem_data if snapshot else None
    initial_channel_map = (
        build_channel_map(
            modem_data.get("downstream", []),
            modem_data.get("upstream", []),
            identity_mode,
        )
        if modem_data
        else ChannelMap()
    )

    entry.runtime_data = CableModemRuntimeData(
        data_coordinator=data_coordinator,
        health_coordinator=health_coordinator,
        orchestrator=orchestrator,
        health_monitor=health_monitor,
        modem_identity=modem_identity,
        channel_map=initial_channel_map,
    )

    # Step 10: Forward platform setup
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Step 11: Update device registry
    _update_device_registry(hass, entry)

    # Services are registered in async_setup (integration-global), not
    # here — see § Services.

    # Log operational summary — after this, per-poll details are DEBUG only
    _log_operational_summary(scan_interval, health_check_interval, model)

    # Register update listener (options flow changes trigger reload)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


def _format_interval(seconds: int) -> str:
    """Format a polling interval as a human-readable string."""
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs:
        parts.append(f"{secs}s")
    return f"every {' '.join(parts)}" if parts else "every 0s"


def _log_operational_summary(
    scan_interval: int,
    health_check_interval: int,
    model: str,
) -> None:
    """Log a one-time summary after startup completes.

    Tells the user the polling cadence and how to enable detailed
    logging. After this message, per-poll details are DEBUG only.
    """
    poll_msg = _format_interval(scan_interval) if scan_interval > 0 else "manual only"
    health_msg = _format_interval(health_check_interval) if health_check_interval > 0 else "disabled"

    _LOGGER.info(
        "Initialized [%s] — Polling %s, health checks %s. Enable debug logging for per-poll details.",
        model,
        poll_msg,
        health_msg,
    )


async def async_unload_entry(
    hass: HomeAssistant,
    entry: CableModemConfigEntry,
) -> bool:
    """Unload a config entry.

    Unloads platforms; ``runtime_data`` is auto-cleaned by HA. There
    is no long-running work to cancel — ``orchestrator.restart()`` is
    one-shot and returns in a few seconds.
    """
    model = entry.data.get("model", "unknown")

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        # Log out any live modem session and release the socket pool now
        # rather than leaving a lock / lingering to GC — matters on reload
        # so the fresh orchestrator doesn't collide with a dying one. Both
        # steps are blocking network/socket work, hence the executor.
        await hass.async_add_executor_job(entry.runtime_data.orchestrator.close)

    # Services are integration-global (registered in async_setup); they
    # outlive the entry and are not unregistered here.

    _LOGGER.info("Unloaded [%s]", model)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up per-entry Store state when the config entry is deleted.

    Called by HA after ``async_unload_entry``. Entry data and options are
    managed by HA; Store payloads (e.g. the channel-bond baseline) are not.
    """
    await async_remove_bond_state(hass, entry.entry_id)


async def _async_update_listener(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Reload integration when options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)


# ------------------------------------------------------------------
# Sync helpers — run in executor (file I/O)
# ------------------------------------------------------------------


def _create_core_components(
    data: Mapping[str, Any],
) -> tuple[Orchestrator, HealthMonitor | None, ModemIdentity]:
    """Load modem config and create Core components.

    Implements startup Steps 1-5 from HA_ADAPTER_SPEC.md.  Runs in an
    executor thread because config loading reads YAML files from the
    catalog package.

    Config loading stays here (catalog path is HA-specific).  Assembly
    delegates to the Core factory.

    Args:
        data: Config entry data (user selections + validation results).
    """
    # Step 1: Load configs from catalog
    modem_dir = CATALOG_PATH / data[CONF_MODEM_DIR]
    variant = data.get(CONF_VARIANT)

    modem_yaml = modem_dir / f"modem-{variant}.yaml" if variant else modem_dir / "modem.yaml"
    parser_yaml = modem_dir / "parser.yaml"
    parser_py = modem_dir / "parser.py"

    modem_config = load_modem_config(modem_yaml)
    parser_config = load_parser_config(parser_yaml) if parser_yaml.exists() else None
    post_processor = load_post_processor(parser_py) if parser_py.exists() else None

    # Step 1a: Inject credential encoding (Core concern)
    apply_credential_encoding(
        modem_config,
        credential_encoding=data.get(CONF_CREDENTIAL_ENCODING, "plain"),
        credential_field=data.get(CONF_CREDENTIAL_FIELD, ""),
    )

    # Steps 2-5: Delegate assembly to Core factory
    protocol = data.get(CONF_PROTOCOL, "http")
    host = data[CONF_HOST]
    base_url = f"{protocol}://{host}"

    # Resolve health probe defaults from modem.yaml, apply config entry overrides
    health_cfg = modem_config.health
    http_probe = health_cfg.http_probe if health_cfg else True
    default_icmp = health_cfg.supports_icmp if health_cfg else True
    default_head = health_cfg.supports_head if health_cfg else True

    return create_orchestrator(
        modem_config=modem_config,
        parser_config=parser_config,
        post_processor=post_processor,
        base_url=base_url,
        username=data.get(CONF_USERNAME, ""),
        password=data.get(CONF_PASSWORD, ""),
        legacy_ssl=data.get(CONF_LEGACY_SSL, False),
        supports_icmp=data.get(CONF_SUPPORTS_ICMP, default_icmp),
        supports_head=data.get(CONF_SUPPORTS_HEAD, default_head),
        http_probe=http_probe,
        model=modem_config.model,
    )


# ------------------------------------------------------------------
# Device registry
# ------------------------------------------------------------------


def _update_device_registry(
    hass: HomeAssistant,
    entry: CableModemConfigEntry,
) -> None:
    """Register or update the HA device for this config entry.

    Device name depends on the entity_prefix setting chosen during
    config flow.  See ENTITY_MODEL_SPEC.md Device Model section.
    """
    data = entry.data
    identity = entry.runtime_data.modem_identity

    device_name = get_device_name(
        data.get(CONF_ENTITY_PREFIX, "default"),
        model=identity.model,
        host=data.get(CONF_HOST, ""),
    )

    protocol = data.get(CONF_PROTOCOL, "http")
    host = data[CONF_HOST]

    # Version identity lives on the device card, not in the entity list
    # (#178). software_version also stays a sensor — the sensor is the
    # firmware-change timeline; this field is display-only and refreshes
    # at entry setup. Empty when the first poll returned no data.
    snapshot = entry.runtime_data.data_coordinator.data
    system_info: dict[str, Any] = {}
    if snapshot is not None and snapshot.modem_data is not None:
        system_info = snapshot.modem_data.get("system_info", {})

    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=device_name,
        manufacturer=identity.manufacturer,
        model=identity.model,
        configuration_url=f"{protocol}://{host}",
        sw_version=system_info.get("software_version"),
        hw_version=system_info.get("hardware_version"),
    )
