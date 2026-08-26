"""Tests for __init__.py — startup helpers, lifecycle, and logging.

TEST DATA TABLES
================
This module uses table-driven tests. Tables are defined at the top
of the file with ASCII box-drawing comments for readability.

_create_core_components tests mock at the Core/Catalog I/O boundary
(load_modem_config, load_parser_config, load_post_processor) and verify
wiring logic only. Catalog validity is tested in the catalog test suite.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from solentlabs.cable_modem_monitor_core.orchestration.models import (
    ModemIdentity,
    ModemSnapshot,
)
from solentlabs.cable_modem_monitor_core.orchestration.signals import (
    CollectorSignal,
    ConnectionStatus,
    DocsisStatus,
)

from custom_components.cable_modem_monitor import (
    _announce_auth_stop,
    _async_update_listener,
    _check_channel_bond_change,
    _create_core_components,
    _get_package_versions,
    _log_operational_summary,
    _update_device_registry,
    async_migrate_entry,
    async_remove_entry,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.cable_modem_monitor.const import PLATFORMS
from custom_components.cable_modem_monitor.coordinator import CableModemRuntimeData
from custom_components.cable_modem_monitor.migrations import async_run_migrations

from .conftest import MOCK_ENTRY_DATA, STUB_MODEM_CONFIG, _make_stub_modem_config

# -----------------------------------------------------------------------
# _get_package_versions — pure function (mocked importlib)
# -----------------------------------------------------------------------


def test_get_package_versions_installed():
    """Returns formatted version string when packages are installed."""
    with patch(
        "custom_components.cable_modem_monitor.pkg_version",
        return_value="1.0.0",
    ):
        result = _get_package_versions()

    assert result == "core: v1.0.0, catalog: v1.0.0"


def test_get_package_versions_not_installed():
    """Returns 'not installed' when package lookup fails."""
    with patch(
        "custom_components.cable_modem_monitor.pkg_version",
        side_effect=Exception("not found"),
    ):
        result = _get_package_versions()

    assert result == "core: not installed, catalog: not installed"


# -----------------------------------------------------------------------
# _log_operational_summary — pure function (logging output)
# -----------------------------------------------------------------------

# ┌──────────────┬───────────────────────┬──────────────┬──────────────┐
# │ scan_interval│ health_check_interval │ poll_msg     │ health_msg   │
# ├──────────────┼───────────────────────┼──────────────┼──────────────┤
# │ 600          │ 30                    │ every 10m    │ every 30s    │
# │ 30           │ 0                     │ every 30s    │ disabled     │
# │ 0            │ 60                    │ manual only  │ every 60s    │
# │ 0            │ 0                     │ manual only  │ disabled     │
# └──────────────┴───────────────────────┴──────────────┴──────────────┘
#
# fmt: off
SUMMARY_CASES = [
    (600, 30, "every 10m",   "every 30s", "minutes_with_health"),
    (30,  0,  "every 30s",   "disabled",  "seconds_no_health"),
    (0,   60, "manual only", "every 1m",  "manual_with_health"),
    (0,   0,  "manual only", "disabled",  "manual_no_health"),
]
# fmt: on


@pytest.mark.parametrize(
    "scan,health,expected_poll,expected_health,desc",
    SUMMARY_CASES,
    ids=[c[4] for c in SUMMARY_CASES],
)
def test_log_operational_summary(scan, health, expected_poll, expected_health, desc, caplog):
    """Operational summary formats intervals correctly."""
    with caplog.at_level(logging.INFO, logger="custom_components.cable_modem_monitor"):
        _log_operational_summary(scan, health, "TPS-2000")

    assert expected_poll in caplog.text
    assert expected_health in caplog.text


# -----------------------------------------------------------------------
# async_migrate_entry
# -----------------------------------------------------------------------


async def test_async_migrate_entry_delegates():
    """Migration delegates to async_run_migrations with correct version."""
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test_id"
    entry.version = 1

    with patch(
        "custom_components.cable_modem_monitor.async_run_migrations",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_migrate:
        result = await async_migrate_entry(hass, entry)

    assert result is True
    mock_migrate.assert_awaited_once_with(hass, entry, 2)


# -----------------------------------------------------------------------
# async_setup — integration-global service registration
# -----------------------------------------------------------------------


async def test_async_setup_registers_services():
    """async_setup registers integration-global services (Bronze action-setup).

    Services are registered once at component load, not per config
    entry, so they exist even before any entry is configured. Handlers
    resolve their target config entry at call time.
    """
    hass = MagicMock()

    with patch("custom_components.cable_modem_monitor.async_register_services") as mock_reg:
        result = await async_setup(hass, {})

    assert result is True
    mock_reg.assert_called_once_with(hass)


# -----------------------------------------------------------------------
# async_unload_entry
# -----------------------------------------------------------------------


async def test_unload_entry_basic():
    """Unload succeeds and returns True.

    Services are integration-global (registered in async_setup) and are
    deliberately not unregistered on entry unload — they outlive the
    entry so a re-added entry still has them.
    """
    hass = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    hass.async_add_executor_job = AsyncMock()
    hass.config_entries.async_entries.return_value = [MagicMock()]  # not last

    entry = MagicMock()

    result = await async_unload_entry(hass, entry)

    assert result is True
    hass.config_entries.async_unload_platforms.assert_awaited_once_with(entry, PLATFORMS)


async def test_unload_entry_closes_orchestrator():
    """Unload closes the orchestrator's session in the executor."""
    hass = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    hass.config_entries.async_entries.return_value = []

    async def _run_executor(func, *args):
        return func(*args)

    hass.async_add_executor_job = _run_executor

    entry = MagicMock()

    await async_unload_entry(hass, entry)

    entry.runtime_data.orchestrator.close.assert_called_once_with()


async def test_unload_entry_skips_close_when_platform_unload_fails():
    """A failed platform unload leaves the session untouched."""
    hass = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)
    hass.async_add_executor_job = AsyncMock()

    entry = MagicMock()

    result = await async_unload_entry(hass, entry)

    assert result is False
    hass.async_add_executor_job.assert_not_awaited()


# -----------------------------------------------------------------------
# async_setup_entry — failure path
# -----------------------------------------------------------------------


async def test_setup_entry_catalog_failure():
    """Setup raises ConfigEntryError when catalog loading fails.

    Bronze test-before-setup: a missing/corrupt catalog config is a
    permanent misconfiguration, so raise ConfigEntryError rather than
    returning False (which HA logs as a bare "failed to set up").
    """
    hass = MagicMock()

    call_count = 0

    async def _mock_executor_job(func, *args):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: _get_package_versions
            return "core: v1.0.0, catalog: v1.0.0"
        # Second call: _create_core_components — raises
        raise FileNotFoundError("modem.yaml not found")

    hass.async_add_executor_job = _mock_executor_job

    entry = MagicMock()
    entry.data = {"host": "192.168.100.1", "manufacturer": "Solent Labs", "model": "TPS-2000"}
    entry.options = {}

    with (
        patch("custom_components.cable_modem_monitor.setup_log_buffer"),
        pytest.raises(ConfigEntryError, match="catalog"),
    ):
        await async_setup_entry(hass, entry)


# -----------------------------------------------------------------------
# _create_core_components — wiring tests (mocked Core/Catalog I/O)
# -----------------------------------------------------------------------

# Shared patch targets for _create_core_components tests
_PATCH_LOAD_MODEM = "custom_components.cable_modem_monitor.load_modem_config"
_PATCH_LOAD_PARSER = "custom_components.cable_modem_monitor.load_parser_config"
_PATCH_LOAD_POST = "custom_components.cable_modem_monitor.load_post_processor"


def test_wiring_loads_config_and_returns_tuple():
    """Happy path: loads config, returns (orchestrator, health_monitor, identity)."""
    with (
        patch(_PATCH_LOAD_MODEM, return_value=STUB_MODEM_CONFIG),
        patch(_PATCH_LOAD_PARSER, return_value=None),
        patch(_PATCH_LOAD_POST, return_value=None),
        patch("pathlib.Path.exists", return_value=False),
    ):
        orchestrator, health_monitor, identity = _create_core_components(MOCK_ENTRY_DATA)

    assert orchestrator is not None
    assert health_monitor is not None
    assert identity.manufacturer == "Solent Labs"
    assert identity.model == "TPS-2000"
    assert identity.status == "confirmed"


def test_wiring_variant_uses_variant_yaml():
    """Variant in entry data loads modem-{variant}.yaml."""
    data = {**MOCK_ENTRY_DATA, "variant": "form-nonce"}

    with (
        patch(_PATCH_LOAD_MODEM, return_value=STUB_MODEM_CONFIG) as mock_load,
        patch(_PATCH_LOAD_PARSER, return_value=None),
        patch(_PATCH_LOAD_POST, return_value=None),
        patch("pathlib.Path.exists", return_value=False),
    ):
        _create_core_components(data)

    called_path = mock_load.call_args[0][0]
    assert called_path.name == "modem-form-nonce.yaml"


def test_wiring_parser_loaded_when_present():
    """Parser config and post-processor loaded when files exist."""
    with (
        patch(_PATCH_LOAD_MODEM, return_value=STUB_MODEM_CONFIG),
        patch(_PATCH_LOAD_PARSER, return_value=MagicMock()) as mock_parser,
        patch(_PATCH_LOAD_POST, return_value=MagicMock()) as mock_post,
        patch("pathlib.Path.exists", return_value=True),
    ):
        _create_core_components(MOCK_ENTRY_DATA)

    mock_parser.assert_called_once()
    mock_post.assert_called_once()


def test_wiring_no_parser_files():
    """Neither parser config nor post-processor loaded when files are absent."""
    with (
        patch(_PATCH_LOAD_MODEM, return_value=STUB_MODEM_CONFIG),
        patch(_PATCH_LOAD_PARSER) as mock_parser,
        patch(_PATCH_LOAD_POST) as mock_post,
        patch("pathlib.Path.exists", return_value=False),
    ):
        _create_core_components(MOCK_ENTRY_DATA)

    mock_parser.assert_not_called()
    mock_post.assert_not_called()


def test_wiring_passes_entry_credentials_to_factory():
    """Entry-data credentials reach create_orchestrator on every (re)build."""
    # Credential changes are delivered by entry reload (UC-16): the
    # rebuilt orchestrator must be constructed from the UPDATED entry
    # data, or the clean slate the reload promises never happens.
    data = {**MOCK_ENTRY_DATA, "username": "admin", "password": "newpassword"}

    with (
        patch(_PATCH_LOAD_MODEM, return_value=STUB_MODEM_CONFIG),
        patch(_PATCH_LOAD_PARSER, return_value=None),
        patch(_PATCH_LOAD_POST, return_value=None),
        patch("pathlib.Path.exists", return_value=False),
        patch("custom_components.cable_modem_monitor.create_orchestrator") as mock_factory,
    ):
        _create_core_components(data)

    kwargs = mock_factory.call_args.kwargs
    assert kwargs["username"] == "admin"
    assert kwargs["password"] == "newpassword"


# ┌──────────────┬────────────┬─────────────────────┬───────────────────┐
# │ supports_icmp│ http_probe │ health_monitor      │ description       │
# ├──────────────┼────────────┼─────────────────────┼───────────────────┤
# │ True         │ True       │ created             │ both probes       │
# │ True         │ False      │ created             │ icmp only         │
# │ False        │ True       │ created             │ http only         │
# │ False        │ False      │ None                │ no probes         │
# └──────────────┴────────────┴─────────────────────┴───────────────────┘
#
# fmt: off
HEALTH_MONITOR_CASES = [
    (True,  True,  True,  "both_probes"),
    (True,  False, True,  "icmp_only"),
    (False, True,  True,  "http_only"),
    (False, False, False, "no_probes"),
]
# fmt: on


@pytest.mark.parametrize(
    "supports_icmp,http_probe,expect_created,desc",
    HEALTH_MONITOR_CASES,
    ids=[c[3] for c in HEALTH_MONITOR_CASES],
)
def test_health_monitor_conditional(supports_icmp, http_probe, expect_created, desc):
    """Health monitor created only when at least one probe is enabled."""
    stub = _make_stub_modem_config(
        health_http_probe=http_probe,
        health_supports_icmp=supports_icmp,
    )
    data = {**MOCK_ENTRY_DATA, "supports_icmp": supports_icmp}

    with (
        patch(_PATCH_LOAD_MODEM, return_value=stub),
        patch(_PATCH_LOAD_PARSER, return_value=None),
        patch(_PATCH_LOAD_POST, return_value=None),
        patch("pathlib.Path.exists", return_value=False),
    ):
        _, health_monitor, _ = _create_core_components(data)

    if expect_created:
        assert health_monitor is not None
    else:
        assert health_monitor is None


def test_health_monitor_no_health_config():
    """When modem config has no health section, defaults create health monitor."""
    stub = _make_stub_modem_config(health_config=False)

    with (
        patch(_PATCH_LOAD_MODEM, return_value=stub),
        patch(_PATCH_LOAD_PARSER, return_value=None),
        patch(_PATCH_LOAD_POST, return_value=None),
        patch("pathlib.Path.exists", return_value=False),
    ):
        _, health_monitor, _ = _create_core_components(MOCK_ENTRY_DATA)

    # health=None → defaults to http_probe=True, supports_icmp=True
    assert health_monitor is not None


def test_identity_without_hardware():
    """When hardware is None, identity has None for version fields."""
    stub = _make_stub_modem_config(hardware=False)

    with (
        patch(_PATCH_LOAD_MODEM, return_value=stub),
        patch(_PATCH_LOAD_PARSER, return_value=None),
        patch(_PATCH_LOAD_POST, return_value=None),
        patch("pathlib.Path.exists", return_value=False),
    ):
        _, _, identity = _create_core_components(MOCK_ENTRY_DATA)

    assert identity.docsis_version is None
    assert identity.release_date is None
    assert identity.manufacturer == "Solent Labs"


def test_config_load_error_propagates():
    """FileNotFoundError from load_modem_config propagates to caller."""
    with (
        patch(_PATCH_LOAD_MODEM, side_effect=FileNotFoundError("not found")),
        pytest.raises(FileNotFoundError),
    ):
        _create_core_components(MOCK_ENTRY_DATA)


# -----------------------------------------------------------------------
# async_run_migrations — error paths
# -----------------------------------------------------------------------


async def test_migration_exception_returns_false():
    """Migration that raises returns False."""

    async def _failing(hass, entry):
        raise ValueError("corrupt entry")

    entry = MagicMock()
    entry.version = 1
    entry.entry_id = "test"

    with patch.dict(
        "custom_components.cable_modem_monitor.migrations.MIGRATIONS",
        {2: _failing},
        clear=True,
    ):
        result = await async_run_migrations(MagicMock(), entry, 2)

    assert result is False


async def test_migration_returning_false_stops_chain():
    """Migration returning False halts further migrations."""
    calls = []

    async def _fail(hass, entry):
        calls.append(2)
        return False

    async def _should_not_run(hass, entry):
        calls.append(3)
        return True

    entry = MagicMock()
    entry.version = 1
    entry.entry_id = "test"

    with patch.dict(
        "custom_components.cable_modem_monitor.migrations.MIGRATIONS",
        {2: _fail, 3: _should_not_run},
        clear=True,
    ):
        result = await async_run_migrations(MagicMock(), entry, 3)

    assert result is False
    assert calls == [2]  # v3 migration never ran


# -----------------------------------------------------------------------
# async_setup_entry — happy path
# -----------------------------------------------------------------------


async def test_setup_entry_happy_path():
    """Full startup sequence succeeds and stores runtime data."""
    hass = MagicMock()

    mock_orch = MagicMock()
    mock_orch.supports_restart = True
    mock_health_mon = MagicMock()
    mock_identity = ModemIdentity(
        manufacturer="Solent Labs",
        model="TPS-2000",
        docsis_version="3.0",
        release_date="2024",
        status="confirmed",
    )

    call_count = 0

    async def _mock_executor(func, *args):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "core: v1.0.0, catalog: v1.0.0"
        return (mock_orch, mock_health_mon, mock_identity)

    hass.async_add_executor_job = _mock_executor
    hass.config_entries.async_forward_entry_setups = AsyncMock()

    entry = MagicMock()
    entry.data = MOCK_ENTRY_DATA
    entry.options = {}
    entry.entry_id = "test_123"
    entry.title = "Solent Labs TPS-2000"

    mock_data_coord = MagicMock()
    mock_data_coord.async_config_entry_first_refresh = AsyncMock()
    mock_health_coord = MagicMock()
    mock_health_coord.async_config_entry_first_refresh = AsyncMock()

    # DataUpdateCoordinator[Type](...) → subscript returns a callable mock
    mock_duc = MagicMock()
    mock_duc.__getitem__.return_value.side_effect = [
        mock_data_coord,
        mock_health_coord,
    ]

    with (
        patch("custom_components.cable_modem_monitor.setup_log_buffer"),
        patch(
            "custom_components.cable_modem_monitor.DataUpdateCoordinator",
            mock_duc,
        ),
        patch("custom_components.cable_modem_monitor._update_device_registry"),
        patch("custom_components.cable_modem_monitor.attach_recovery_cadence_listener") as mock_attach,
    ):
        result = await async_setup_entry(hass, entry)

    assert result is True
    assert isinstance(entry.runtime_data, CableModemRuntimeData)
    assert entry.runtime_data.data_coordinator is mock_data_coord
    assert entry.runtime_data.health_coordinator is mock_health_coord
    assert entry.runtime_data.orchestrator is mock_orch
    mock_data_coord.async_config_entry_first_refresh.assert_awaited_once()
    mock_health_coord.async_config_entry_first_refresh.assert_awaited_once()
    hass.config_entries.async_forward_entry_setups.assert_awaited_once()
    # Recovery cadence listener is attached exactly once.
    mock_attach.assert_called_once_with(hass, entry, mock_orch, mock_data_coord)


# -----------------------------------------------------------------------
# _update_device_registry
# -----------------------------------------------------------------------


def _make_registry_entry(modem_data):
    """Build the mock entry + identity for device registry tests."""
    entry = MagicMock()
    entry.data = MOCK_ENTRY_DATA
    entry.entry_id = "test_123"
    entry.runtime_data = MagicMock()
    entry.runtime_data.modem_identity = ModemIdentity(
        manufacturer="Solent Labs",
        model="TPS-2000",
        docsis_version="3.0",
        release_date="2024",
        status="confirmed",
    )
    entry.runtime_data.data_coordinator.data.modem_data = modem_data
    return entry


def test_update_device_registry():
    """Device registry updated with modem identity and version fields."""
    hass = MagicMock()
    entry = _make_registry_entry({"system_info": {"software_version": "1.2.3", "hardware_version": "V1.0"}})
    mock_registry = MagicMock()

    with patch(
        "custom_components.cable_modem_monitor.dr.async_get",
        return_value=mock_registry,
    ):
        _update_device_registry(hass, entry)

    mock_registry.async_get_or_create.assert_called_once()
    kwargs = mock_registry.async_get_or_create.call_args.kwargs
    assert kwargs["manufacturer"] == "Solent Labs"
    assert kwargs["model"] == "TPS-2000"
    assert kwargs["configuration_url"] == "http://192.168.100.1"
    assert kwargs["sw_version"] == "1.2.3"
    assert kwargs["hw_version"] == "V1.0"


# Device model split: `model` is the catalog name the user bought, `model_id`
# is the manufacturer identifier the modem reports for the same box. The
# reported value must never take over `model` — on the Technicolor entries it
# is an OEM board code (XB7 reports "CGM4331COM"), and promoting it would
# replace a name users recognize with one they do not.
# fmt: off
DEVICE_MODEL_CASES = [
    # (system_info,                 expected_model, expected_model_id, id)
    ({"model_name": "CM3200A"},     "TPS-2000",     "CM3200A",   "reported-differs"),
    ({"model_name": "CGM4331COM"},  "TPS-2000",     "CGM4331COM", "oem-board-code"),
    ({"model_name": "TPS-2000"},    "TPS-2000",     "TPS-2000",  "reported-matches"),
    ({},                            "TPS-2000",     None,        "no-model-name"),
    ({"model_name": ""},            "TPS-2000",     None,        "empty-model-name"),
    ({"model_name": None},          "TPS-2000",     None,        "null-model-name"),
]
# fmt: on


@pytest.mark.parametrize(
    "system_info,expected_model,expected_model_id",
    [(c[0], c[1], c[2]) for c in DEVICE_MODEL_CASES],
    ids=[c[3] for c in DEVICE_MODEL_CASES],
)
def test_update_device_registry_model(system_info, expected_model, expected_model_id):
    """Catalog model stays on `model`; the reported one lands on `model_id`."""
    hass = MagicMock()
    entry = _make_registry_entry({"system_info": system_info})
    mock_registry = MagicMock()

    with patch(
        "custom_components.cable_modem_monitor.dr.async_get",
        return_value=mock_registry,
    ):
        _update_device_registry(hass, entry)

    kwargs = mock_registry.async_get_or_create.call_args.kwargs
    assert kwargs["model"] == expected_model
    assert kwargs["model_id"] == expected_model_id


def test_update_device_registry_no_modem_data():
    """Version fields stay empty when the first poll returned no data."""
    hass = MagicMock()
    entry = _make_registry_entry(None)
    mock_registry = MagicMock()

    with patch(
        "custom_components.cable_modem_monitor.dr.async_get",
        return_value=mock_registry,
    ):
        _update_device_registry(hass, entry)

    kwargs = mock_registry.async_get_or_create.call_args.kwargs
    assert kwargs["model"] == "TPS-2000"
    assert kwargs["model_id"] is None
    assert kwargs["sw_version"] is None
    assert kwargs["hw_version"] is None


# -----------------------------------------------------------------------
# _async_update_listener
# -----------------------------------------------------------------------


async def test_update_listener_reloads():
    """Update listener triggers config entry reload."""
    hass = MagicMock()
    hass.config_entries.async_reload = AsyncMock()
    entry = MagicMock()
    entry.entry_id = "test_123"

    await _async_update_listener(hass, entry)

    hass.config_entries.async_reload.assert_awaited_once_with("test_123")


# -----------------------------------------------------------------------
# _check_channel_bond_change — onboarding + totals change detection
# -----------------------------------------------------------------------


def _make_snapshot(downstream_count: int | None = 24, upstream_count: int | None = 4):
    """Build a minimal snapshot carrying system_info channel counts."""
    from solentlabs.cable_modem_monitor_core.orchestration.models import ModemSnapshot
    from solentlabs.cable_modem_monitor_core.orchestration.signals import (
        CollectorSignal,
        ConnectionStatus,
        DocsisStatus,
    )

    system_info: dict[str, object] = {}
    if downstream_count is not None:
        system_info["downstream_channel_count"] = downstream_count
    if upstream_count is not None:
        system_info["upstream_channel_count"] = upstream_count

    return ModemSnapshot(
        connection_status=ConnectionStatus.ONLINE,
        docsis_status=DocsisStatus.OPERATIONAL,
        modem_data={"system_info": system_info, "downstream": [], "upstream": []},
        collector_signal=CollectorSignal.OK,
    )


def _make_bond_test_harness(
    *,
    entry_data: dict,
    stored_state=None,
    recovery_active: bool = False,
    snapshot=None,
):
    """Build (hass, entry, orchestrator, snapshot) for bond-change tests.

    Patches the Store helpers at the import site so ``async_load_bond_state``
    returns ``stored_state`` and ``async_save_bond_state`` is observable.
    """
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    entry = MagicMock()
    entry.entry_id = "entry_abc"
    entry.data = entry_data
    orchestrator = MagicMock()
    orchestrator.recovery_active = recovery_active
    return hass, entry, orchestrator, snapshot or _make_snapshot()


async def test_channel_bond_fresh_setup_fires_onboarding():
    """First successful poll on a fresh-setup entry fires the onboarding notification."""
    entry_data = {"channel_onboarding_eligible": True}
    hass, entry, orchestrator, snapshot = _make_bond_test_harness(entry_data=entry_data)

    with (
        patch(
            "custom_components.cable_modem_monitor.async_load_bond_state",
            AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.cable_modem_monitor.async_save_bond_state",
            AsyncMock(),
        ) as mock_save,
    ):
        await _check_channel_bond_change(hass, entry, snapshot, orchestrator, "TPS-2000")

    # Baseline persisted via Store, not entry data.
    hass.config_entries.async_update_entry.assert_not_called()
    mock_save.assert_awaited_once()
    saved_state = mock_save.call_args.args[2]
    assert saved_state.baseline_downstream == 24
    assert saved_state.baseline_upstream == 4

    hass.services.async_call.assert_awaited_once()
    call_args = hass.services.async_call.call_args
    assert call_args.args[0] == "persistent_notification"
    assert call_args.args[1] == "create"
    payload = call_args.args[2]
    assert payload["notification_id"] == "cable_modem_monitor_onboarding_entry_abc"
    assert "TPS-2000" in payload["message"]
    assert "generate_dashboard" in payload["message"]


async def test_channel_bond_upgraded_entry_silent_init():
    """Entry without the eligibility flag (upgraded) baselines silently."""
    entry_data: dict = {}  # no channel_onboarding_eligible key — upgraded entry
    hass, entry, orchestrator, snapshot = _make_bond_test_harness(entry_data=entry_data)

    with (
        patch(
            "custom_components.cable_modem_monitor.async_load_bond_state",
            AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.cable_modem_monitor.async_save_bond_state",
            AsyncMock(),
        ) as mock_save,
    ):
        await _check_channel_bond_change(hass, entry, snapshot, orchestrator, "TPS-2000")

    hass.config_entries.async_update_entry.assert_not_called()
    mock_save.assert_awaited_once()
    assert mock_save.call_args.args[2].baseline_downstream == 24
    hass.services.async_call.assert_not_called()


async def test_channel_bond_change_fires_notification():
    """Totals differing from baseline fire the change notification."""
    from custom_components.cable_modem_monitor.channel_bond_storage import BondState

    entry_data = {"channel_onboarding_eligible": True}
    hass, entry, orchestrator, snapshot = _make_bond_test_harness(
        entry_data=entry_data,
        snapshot=_make_snapshot(downstream_count=23, upstream_count=4),
    )
    prior = BondState(baseline_downstream=24, baseline_upstream=4)

    with (
        patch(
            "custom_components.cable_modem_monitor.async_load_bond_state",
            AsyncMock(return_value=prior),
        ),
        patch(
            "custom_components.cable_modem_monitor.async_save_bond_state",
            AsyncMock(),
        ) as mock_save,
    ):
        await _check_channel_bond_change(hass, entry, snapshot, orchestrator, "TPS-2000")

    hass.config_entries.async_update_entry.assert_not_called()
    assert mock_save.call_args.args[2].baseline_downstream == 23

    payload = hass.services.async_call.call_args.args[2]
    assert payload["notification_id"] == "cable_modem_monitor_channel_change_entry_abc"
    assert "downstream 24 → 23" in payload["message"]


async def test_channel_bond_steady_counts_no_op():
    """No change means no notification, no Store write, no entry-data write."""
    from custom_components.cable_modem_monitor.channel_bond_storage import BondState

    entry_data = {"channel_onboarding_eligible": True}
    hass, entry, orchestrator, snapshot = _make_bond_test_harness(entry_data=entry_data)
    stored = BondState(baseline_downstream=24, baseline_upstream=4)

    with (
        patch(
            "custom_components.cable_modem_monitor.async_load_bond_state",
            AsyncMock(return_value=stored),
        ),
        patch(
            "custom_components.cable_modem_monitor.async_save_bond_state",
            AsyncMock(),
        ) as mock_save,
    ):
        await _check_channel_bond_change(hass, entry, snapshot, orchestrator, "TPS-2000")

    hass.config_entries.async_update_entry.assert_not_called()
    mock_save.assert_not_awaited()
    hass.services.async_call.assert_not_called()


async def test_channel_bond_recovery_suppresses_change():
    """Count mismatch during a recovery window: no notification, no Store write."""
    from custom_components.cable_modem_monitor.channel_bond_storage import BondState

    entry_data = {"channel_onboarding_eligible": True}
    hass, entry, orchestrator, snapshot = _make_bond_test_harness(
        entry_data=entry_data,
        recovery_active=True,
        snapshot=_make_snapshot(downstream_count=0, upstream_count=0),
    )
    stored = BondState(baseline_downstream=24, baseline_upstream=4)

    with (
        patch(
            "custom_components.cable_modem_monitor.async_load_bond_state",
            AsyncMock(return_value=stored),
        ),
        patch(
            "custom_components.cable_modem_monitor.async_save_bond_state",
            AsyncMock(),
        ) as mock_save,
    ):
        await _check_channel_bond_change(hass, entry, snapshot, orchestrator, "TPS-2000")

    hass.config_entries.async_update_entry.assert_not_called()
    mock_save.assert_not_awaited()
    hass.services.async_call.assert_not_called()


async def test_channel_bond_missing_snapshot_data_no_op():
    """Snapshots without system_info counts are ignored before Store is touched."""
    entry_data = {"channel_onboarding_eligible": True}
    snapshot = _make_snapshot(downstream_count=None, upstream_count=None)
    hass, entry, orchestrator, _ = _make_bond_test_harness(entry_data=entry_data, snapshot=snapshot)

    with (
        patch(
            "custom_components.cable_modem_monitor.async_load_bond_state",
            AsyncMock(return_value=None),
        ) as mock_load,
        patch(
            "custom_components.cable_modem_monitor.async_save_bond_state",
            AsyncMock(),
        ) as mock_save,
    ):
        await _check_channel_bond_change(hass, entry, snapshot, orchestrator, "TPS-2000")

    mock_load.assert_not_awaited()
    mock_save.assert_not_awaited()
    hass.config_entries.async_update_entry.assert_not_called()
    hass.services.async_call.assert_not_called()


async def test_async_remove_entry_clears_bond_store():
    """HA's entry-removal hook cleans up the channel-bond Store for the entry."""
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "entry_abc"

    with patch(
        "custom_components.cable_modem_monitor.async_remove_bond_state",
        AsyncMock(),
    ) as mock_remove:
        await async_remove_entry(hass, entry)

    mock_remove.assert_awaited_once_with(hass, "entry_abc")


# -----------------------------------------------------------------------
# _check_channel_bond_change — early-out branches
# -----------------------------------------------------------------------


async def test_channel_bond_no_modem_data_no_op():
    """Snapshot with modem_data=None exits before touching the bond store."""
    from solentlabs.cable_modem_monitor_core.orchestration.models import ModemSnapshot
    from solentlabs.cable_modem_monitor_core.orchestration.signals import (
        CollectorSignal,
        ConnectionStatus,
        DocsisStatus,
    )

    snapshot = ModemSnapshot(
        connection_status=ConnectionStatus.UNREACHABLE,
        docsis_status=DocsisStatus.NOT_LOCKED,
        modem_data=None,
        collector_signal=CollectorSignal.OK,
    )
    hass, entry, orchestrator, _ = _make_bond_test_harness(
        entry_data={"channel_onboarding_eligible": True}, snapshot=snapshot
    )

    with patch(
        "custom_components.cable_modem_monitor.async_load_bond_state",
        AsyncMock(),
    ) as mock_load:
        await _check_channel_bond_change(hass, entry, snapshot, orchestrator, "TPS-2000")

    mock_load.assert_not_awaited()
    hass.services.async_call.assert_not_called()


# -----------------------------------------------------------------------
# _rebuild_channel_map — runtime presence and snapshot data branches
# -----------------------------------------------------------------------


def _rebuild_inputs(*, with_runtime: bool, with_modem_data: bool) -> tuple[Any, Any, Any]:
    """Build (entry, snapshot, identity) inputs for _rebuild_channel_map tests."""
    from solentlabs.cable_modem_monitor_core.orchestration.models import ModemSnapshot
    from solentlabs.cable_modem_monitor_core.orchestration.signals import (
        ConnectionStatus,
        DocsisStatus,
    )

    from custom_components.cable_modem_monitor.const import ChannelIdentity

    entry: Any = MagicMock()
    if with_runtime:
        runtime = MagicMock()
        runtime.channel_map = None
        entry.runtime_data = runtime
    else:
        # Use a minimal object with no runtime_data attribute (covers the
        # pre-Step-9 case where _rebuild_channel_map's getattr-fallback fires).
        class _Bare:
            pass

        entry = _Bare()

    snapshot = ModemSnapshot(
        connection_status=ConnectionStatus.ONLINE,
        docsis_status=DocsisStatus.OPERATIONAL,
        modem_data=({"downstream": [{"channel_id": 1}], "upstream": [{"channel_id": 1}]} if with_modem_data else None),
    )
    return entry, snapshot, ChannelIdentity.ID


def test_rebuild_channel_map_no_runtime_data_no_op():
    """Pre-Step-9 entry without runtime_data returns silently."""
    from custom_components.cable_modem_monitor import _rebuild_channel_map

    entry, snapshot, identity = _rebuild_inputs(with_runtime=False, with_modem_data=True)

    # Should not raise; nothing to assert beyond no exception (entry has no
    # runtime_data, so the function returns at the early-out).
    _rebuild_channel_map(entry, snapshot, identity)


def test_rebuild_channel_map_no_modem_data_no_op():
    """Snapshot without modem_data leaves runtime.channel_map untouched."""
    from custom_components.cable_modem_monitor import _rebuild_channel_map

    entry, snapshot, identity = _rebuild_inputs(with_runtime=True, with_modem_data=False)

    _rebuild_channel_map(entry, snapshot, identity)

    # channel_map was untouched (still the sentinel value we set)
    assert entry.runtime_data.channel_map is None


def test_rebuild_channel_map_builds_map():
    """With runtime + modem_data, channel_map is rebuilt from snapshot."""
    from custom_components.cable_modem_monitor import _rebuild_channel_map

    entry, snapshot, identity = _rebuild_inputs(with_runtime=True, with_modem_data=True)

    _rebuild_channel_map(entry, snapshot, identity)

    # channel_map was reassigned to a fresh ChannelMap built from the snapshot.
    assert entry.runtime_data.channel_map is not None
    assert entry.runtime_data.channel_map is not None  # not the sentinel


# -----------------------------------------------------------------------
# _attach_health_sync_listeners — recovery-edge detection (health → data)
# -----------------------------------------------------------------------


def _health_recovery_inputs():
    """Wire up listener inputs and return (hass, health_coord, data_coord, listener_fn)."""
    from solentlabs.cable_modem_monitor_core.orchestration.signals import (
        ConnectionStatus,
        HealthStatus,
    )

    from custom_components.cable_modem_monitor import _attach_health_sync_listeners

    hass = MagicMock()
    health_coord = MagicMock()
    health_coord.data = MagicMock()
    health_coord.data.health_status = HealthStatus.RESPONSIVE
    health_coord.async_request_refresh = AsyncMock()
    data_coord = MagicMock()
    data_coord.data = MagicMock()
    data_coord.data.connection_status = ConnectionStatus.ONLINE
    data_coord.async_request_refresh = AsyncMock()

    _attach_health_sync_listeners(hass, health_coord, data_coord, "TPS-2000")
    listener_fn = health_coord.async_add_listener.call_args[0][0]
    return hass, health_coord, data_coord, listener_fn


def test_health_recovery_triggers_refresh_on_unresponsive_to_responsive():
    """Transition UNRESPONSIVE -> RESPONSIVE schedules an immediate poll."""
    from solentlabs.cable_modem_monitor_core.orchestration.signals import HealthStatus

    hass, health_coord, data_coord, listener_fn = _health_recovery_inputs()

    # Tick 1: UNRESPONSIVE (sets previous to UNRESPONSIVE)
    health_coord.data.health_status = HealthStatus.UNRESPONSIVE
    listener_fn()
    hass.async_create_task.assert_not_called()

    # Tick 2: RESPONSIVE (recovery detected)
    health_coord.data.health_status = HealthStatus.RESPONSIVE
    listener_fn()
    hass.async_create_task.assert_called_once()


def test_health_recovery_skips_when_data_none():
    """Listener exits silently when health_coordinator.data is None."""
    hass, health_coord, _, listener_fn = _health_recovery_inputs()

    health_coord.data = None
    listener_fn()

    hass.async_create_task.assert_not_called()


def test_health_recovery_no_op_in_steady_responsive():
    """RESPONSIVE -> RESPONSIVE is not treated as recovery."""
    from solentlabs.cable_modem_monitor_core.orchestration.signals import HealthStatus

    hass, health_coord, _, listener_fn = _health_recovery_inputs()

    health_coord.data.health_status = HealthStatus.RESPONSIVE
    listener_fn()
    listener_fn()

    hass.async_create_task.assert_not_called()


def test_health_recovery_triggers_refresh_on_degraded_to_responsive():
    """Transition DEGRADED -> RESPONSIVE schedules an immediate poll.

    DEGRADED means ICMP ok but TCP (the data path) is down — the exact
    state a modem passes through while its web UI warms up after a
    reboot. Recovery through DEGRADED must trigger the prompt poll just
    like UNRESPONSIVE; otherwise the data path waits for the next slow
    scan (the #170-adjacent post-reboot latency this fixes).
    """
    from solentlabs.cable_modem_monitor_core.orchestration.signals import HealthStatus

    hass, health_coord, data_coord, listener_fn = _health_recovery_inputs()

    # Tick 1: DEGRADED (TCP/data path down)
    health_coord.data.health_status = HealthStatus.DEGRADED
    listener_fn()
    hass.async_create_task.assert_not_called()

    # Tick 2: RESPONSIVE — data path recovered, schedule the poll
    health_coord.data.health_status = HealthStatus.RESPONSIVE
    listener_fn()
    hass.async_create_task.assert_called_once()


def test_health_recovery_no_refresh_on_icmp_blocked_to_responsive():
    """ICMP_BLOCKED -> RESPONSIVE is NOT a data-path recovery.

    ICMP_BLOCKED means TCP (the data path) was already up — the data
    poll would have succeeded throughout — so ICMP starting to answer
    is not a recovery worth a forced poll. Excluding it avoids spurious
    polls on modems that routinely block ping.
    """
    from solentlabs.cable_modem_monitor_core.orchestration.signals import HealthStatus

    hass, health_coord, _, listener_fn = _health_recovery_inputs()

    health_coord.data.health_status = HealthStatus.ICMP_BLOCKED
    listener_fn()
    health_coord.data.health_status = HealthStatus.RESPONSIVE
    listener_fn()

    hass.async_create_task.assert_not_called()


@pytest.mark.parametrize("down_state", ["UNRESPONSIVE", "DEGRADED"])
def test_health_recovery_triggers_refresh_on_down_to_icmp_blocked(down_state):
    """Transition from a down state to ICMP_BLOCKED IS a data-path recovery.

    Since the ICMP contradiction override (UC-59a), every ICMP_BLOCKED
    reading carries a live TCP pass — the data path is proven up, so
    the forced poll must fire just as it does for RESPONSIVE.
    """
    from solentlabs.cable_modem_monitor_core.orchestration.signals import HealthStatus

    hass, health_coord, _, listener_fn = _health_recovery_inputs()

    health_coord.data.health_status = HealthStatus[down_state]
    listener_fn()
    hass.async_create_task.assert_not_called()

    health_coord.data.health_status = HealthStatus.ICMP_BLOCKED
    listener_fn()
    hass.async_create_task.assert_called_once()


def test_health_recovery_fires_once_through_icmp_blocked():
    """UNRESPONSIVE -> ICMP_BLOCKED -> RESPONSIVE fires exactly one poll.

    The transitional ICMP_BLOCKED reading must not launder the down
    state (observed live 2026-07-12: the old state-enumeration edge
    missed this path and left Status at Unreachable for a full scan
    interval), and the subsequent up -> up step must not double-fire.
    """
    from solentlabs.cable_modem_monitor_core.orchestration.signals import HealthStatus

    hass, health_coord, _, listener_fn = _health_recovery_inputs()

    health_coord.data.health_status = HealthStatus.UNRESPONSIVE
    listener_fn()
    health_coord.data.health_status = HealthStatus.ICMP_BLOCKED
    listener_fn()
    health_coord.data.health_status = HealthStatus.RESPONSIVE
    listener_fn()

    hass.async_create_task.assert_called_once()


def test_health_recovery_no_refresh_on_responsive_to_icmp_blocked():
    """RESPONSIVE -> ICMP_BLOCKED is up -> up — no forced poll.

    A modem whose network starts filtering ping has not lost its data
    path; firing here would spuriously poll ping-blocking setups.
    """
    from solentlabs.cable_modem_monitor_core.orchestration.signals import HealthStatus

    hass, health_coord, _, listener_fn = _health_recovery_inputs()

    health_coord.data.health_status = HealthStatus.ICMP_BLOCKED
    listener_fn()

    hass.async_create_task.assert_not_called()


# -----------------------------------------------------------------------
# _attach_health_sync_listeners — stale-health contradiction (data → health)
# -----------------------------------------------------------------------


def _health_sync_inputs():
    """Wire up both listeners; return (hass, health_coord, data_coord, health_fn, data_fn)."""
    hass, health_coord, data_coord, health_fn = _health_recovery_inputs()
    data_fn = data_coord.async_add_listener.call_args[0][0]
    return hass, health_coord, data_coord, health_fn, data_fn


@pytest.mark.parametrize(
    "stale_state",
    ["UNRESPONSIVE", "DEGRADED"],
)
def test_poll_success_refreshes_stale_down_health(stale_state):
    """A successful poll while health reads data-path-down refreshes health.

    A completed collection (auth + fetches) is live proof the modem is
    responsive; a stale UNRESPONSIVE/DEGRADED probe result must not keep
    masking it in the Status cascade until the next scheduled probe —
    UC-59a's principle applied in the reverse direction.
    """
    from solentlabs.cable_modem_monitor_core.orchestration.signals import HealthStatus

    _, health_coord, _, _, data_fn = _health_sync_inputs()

    health_coord.data.health_status = HealthStatus[stale_state]
    data_fn()

    health_coord.async_request_refresh.assert_called_once()


def test_poll_failure_does_not_refresh_health():
    """A failed poll (UNREACHABLE snapshot) never triggers a health refresh."""
    from solentlabs.cable_modem_monitor_core.orchestration.signals import (
        ConnectionStatus,
        HealthStatus,
    )

    _, health_coord, data_coord, _, data_fn = _health_sync_inputs()

    health_coord.data.health_status = HealthStatus.UNRESPONSIVE
    data_coord.data.connection_status = ConnectionStatus.UNREACHABLE
    data_fn()

    health_coord.async_request_refresh.assert_not_called()


def test_poll_success_with_healthy_probe_no_refresh():
    """Steady state — poll success while health is RESPONSIVE is a no-op."""
    _, health_coord, _, _, data_fn = _health_sync_inputs()

    data_fn()

    health_coord.async_request_refresh.assert_not_called()


@pytest.mark.parametrize(
    "stale_state",
    ["UNKNOWN", "ICMP_BLOCKED"],
)
def test_poll_success_ignores_non_data_path_states(stale_state):
    """UNKNOWN (probes not applicable) and ICMP_BLOCKED (TCP already up)
    are not contradicted by a successful poll — no refresh, no flag churn."""
    from solentlabs.cable_modem_monitor_core.orchestration.signals import HealthStatus

    _, health_coord, _, _, data_fn = _health_sync_inputs()

    health_coord.data.health_status = HealthStatus[stale_state]
    data_fn()

    health_coord.async_request_refresh.assert_not_called()


def test_poll_success_skips_when_coordinator_data_none():
    """Listener exits silently when either coordinator has no data yet."""
    from solentlabs.cable_modem_monitor_core.orchestration.signals import HealthStatus

    _, health_coord, data_coord, _, data_fn = _health_sync_inputs()

    health_coord.data.health_status = HealthStatus.UNRESPONSIVE
    data_coord.data = None
    data_fn()
    health_coord.async_request_refresh.assert_not_called()

    data_coord.data = MagicMock()
    health_coord.data = None
    data_fn()
    health_coord.async_request_refresh.assert_not_called()


def test_poll_confirmed_recovery_suppresses_forced_poll():
    """A recovery proven by a successful poll must not force another poll.

    Sequence: health goes UNRESPONSIVE → a scheduled poll succeeds (data
    listener refreshes health) → that refresh reads RESPONSIVE. The
    recovery edge fires, but the evidence WAS a successful poll seconds
    ago — a forced poll would be a redundant login on session-limited
    modems.
    """
    from solentlabs.cable_modem_monitor_core.orchestration.signals import HealthStatus

    _, health_coord, data_coord, health_fn, data_fn = _health_sync_inputs()

    health_coord.data.health_status = HealthStatus.UNRESPONSIVE
    health_fn()
    data_fn()  # poll succeeded while health stale — sets the confirmed flag
    health_coord.data.health_status = HealthStatus.RESPONSIVE
    health_fn()

    data_coord.async_request_refresh.assert_not_called()


def test_confirmed_flag_cleared_by_next_down_probe():
    """The confirmed flag survives only until the next probe result.

    If the health refresh still reads down (modem dipped again after the
    successful poll), the flag is consumed; a later genuine recovery edge
    must fire the forced poll as usual.
    """
    from solentlabs.cable_modem_monitor_core.orchestration.signals import HealthStatus

    _, health_coord, data_coord, health_fn, data_fn = _health_sync_inputs()

    health_coord.data.health_status = HealthStatus.UNRESPONSIVE
    health_fn()
    data_fn()  # flag set
    health_fn()  # refresh still reads UNRESPONSIVE — flag consumed
    health_coord.data.health_status = HealthStatus.RESPONSIVE
    health_fn()  # genuine recovery, flag no longer set

    data_coord.async_request_refresh.assert_called_once()


# -----------------------------------------------------------------------
# _format_interval — hours branch
# -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (3600, "every 1h"),
        (3660, "every 1h 1m"),
        (3661, "every 1h 1m 1s"),
        (7200, "every 2h"),
    ],
    ids=["1h_exact", "1h_1m", "1h_1m_1s", "2h_exact"],
)
def test_format_interval_includes_hours(seconds, expected, caplog):
    """_format_interval emits an hours component when seconds >= 3600."""
    with caplog.at_level(logging.INFO, logger="custom_components.cable_modem_monitor"):
        _log_operational_summary(seconds, 30, "TPS-2000")

    assert expected in caplog.text


# -----------------------------------------------------------------------
# _announce_auth_stop — auth circuit breaker → reauth prompt, or a
# lockout notification when the prompt would be harmful (UC-81, UC-81a)
# -----------------------------------------------------------------------


def _reauth_inputs(
    *,
    status: ConnectionStatus = ConnectionStatus.AUTH_FAILED,
    breaker_open: bool = True,
    trip_signal: CollectorSignal | None = CollectorSignal.AUTH_FAILED,
    active_flows: tuple[Any, ...] = (),
) -> tuple[MagicMock, MagicMock, ModemSnapshot, MagicMock]:
    """Build (hass, entry, snapshot, orchestrator) for reauth trigger tests."""
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    entry = MagicMock()
    entry.async_get_active_flows.return_value = list(active_flows)
    orchestrator = MagicMock()
    diagnostics = orchestrator.diagnostics.return_value
    diagnostics.circuit_breaker_open = breaker_open
    # The trip signal is what discriminates the four routes to the
    # prompt; a bare MagicMock here would silently take the reauth
    # branch for every one of them.
    diagnostics.circuit_trip_signal = trip_signal
    snapshot = ModemSnapshot(
        connection_status=status,
        docsis_status=DocsisStatus.UNKNOWN,
        modem_data=None,
        collector_signal=CollectorSignal.AUTH_FAILED,
        error="auth failed",
    )
    return hass, entry, snapshot, orchestrator


async def test_reauth_started_when_breaker_open():
    """AUTH_FAILED with the breaker open starts HA's reauth flow."""
    hass, entry, snapshot, orchestrator = _reauth_inputs()

    await _announce_auth_stop(hass, entry, snapshot, orchestrator, "TPS-2000", [False])

    entry.async_start_reauth.assert_called_once_with(hass)
    hass.services.async_call.assert_not_called()


async def test_no_reauth_on_transient_auth_failure():
    """A single AUTH_FAILED poll (breaker still closed) does not prompt.

    The breaker debounces single-session collisions and flaky logins;
    only a definitive lockout should interrupt the user.
    """
    hass, entry, snapshot, orchestrator = _reauth_inputs(breaker_open=False)

    await _announce_auth_stop(hass, entry, snapshot, orchestrator, "TPS-2000", [False])

    entry.async_start_reauth.assert_not_called()


async def test_no_reauth_when_online():
    """A healthy poll never starts reauth, whatever the breaker says."""
    hass, entry, snapshot, orchestrator = _reauth_inputs(status=ConnectionStatus.ONLINE)

    await _announce_auth_stop(hass, entry, snapshot, orchestrator, "TPS-2000", [False])

    entry.async_start_reauth.assert_not_called()


async def test_no_duplicate_reauth_while_flow_active():
    """An in-progress reauth flow suppresses re-triggering on later polls."""
    hass, entry, snapshot, orchestrator = _reauth_inputs(active_flows=("flow",))

    await _announce_auth_stop(hass, entry, snapshot, orchestrator, "TPS-2000", [False])

    entry.async_start_reauth.assert_not_called()


# UC-81a decision 2. Resubmitting the reauth form calls
# validate_connection, a real login, and the config flow is stateless by
# design (UC-86) so the open breaker does not stop it. Prompting a
# locked-out user invites hand-fed attempts at a modem that locked
# itself to stop them (#117).
class TestFirmwareLockoutDoesNotPromptForCredentials:
    """A lockout gets a notification instead of the reauth form."""

    @staticmethod
    def _lockout() -> tuple[MagicMock, MagicMock, ModemSnapshot, MagicMock]:
        return _reauth_inputs(trip_signal=CollectorSignal.AUTH_LOCKOUT)

    async def test_lockout_never_starts_reauth(self):
        hass, entry, snapshot, orchestrator = self._lockout()

        await _announce_auth_stop(hass, entry, snapshot, orchestrator, "TPS-2000", [False])

        entry.async_start_reauth.assert_not_called()

    async def test_lockout_notifies_with_the_remedy_and_never_the_password(self):
        """Waiting then reloading is the remedy; credentials are not."""
        hass, entry, snapshot, orchestrator = self._lockout()

        await _announce_auth_stop(hass, entry, snapshot, orchestrator, "TPS-2000", [False])

        domain, service, data = hass.services.async_call.call_args.args
        assert (domain, service) == ("persistent_notification", "create")
        assert "TPS-2000" in data["message"]
        assert "reload the integration" in data["message"]
        assert "diagnostics" in data["message"]
        assert "password" not in data["message"].lower()
        assert "credential" not in data["message"].lower()

    async def test_notification_fires_once_not_every_poll(self):
        """A dismissed notification must not be resurrected on the next poll."""
        hass, entry, snapshot, orchestrator = self._lockout()
        reported = [False]

        for _ in range(3):
            await _announce_auth_stop(hass, entry, snapshot, orchestrator, "TPS-2000", reported)

        assert hass.services.async_call.await_count == 1

    async def test_a_failed_notification_is_retried_next_poll(self):
        """Latching on a call that never landed would be permanent silence."""
        hass, entry, snapshot, orchestrator = self._lockout()
        hass.services.async_call.side_effect = [HomeAssistantError("boom"), None]
        reported = [False]

        with pytest.raises(HomeAssistantError):
            await _announce_auth_stop(hass, entry, snapshot, orchestrator, "TPS-2000", reported)
        await _announce_auth_stop(hass, entry, snapshot, orchestrator, "TPS-2000", reported)

        assert hass.services.async_call.await_count == 2


async def test_update_data_triggers_reauth_on_breaker_open():
    """The coordinator update path wires the reauth trigger end to end."""
    hass = MagicMock()

    mock_orch = MagicMock()
    mock_orch.supports_restart = False
    mock_orch.diagnostics.return_value.circuit_breaker_open = True
    mock_orch.diagnostics.return_value.circuit_trip_signal = CollectorSignal.AUTH_FAILED
    mock_identity = ModemIdentity(
        manufacturer="Solent Labs",
        model="TPS-2000",
        docsis_version="3.0",
        release_date="2024",
        status="confirmed",
    )
    snapshot = ModemSnapshot(
        connection_status=ConnectionStatus.AUTH_FAILED,
        docsis_status=DocsisStatus.UNKNOWN,
        modem_data=None,
        collector_signal=CollectorSignal.AUTH_FAILED,
        error="auth failed",
    )
    mock_orch.get_modem_data = MagicMock()

    async def _mock_executor(func, *args):
        if func is mock_orch.get_modem_data:
            return snapshot
        if not args:
            return "core: v1.0.0, catalog: v1.0.0"
        # _create_core_components — health_monitor None keeps the test
        # to a single (data) coordinator.
        return (mock_orch, None, mock_identity)

    hass.async_add_executor_job = _mock_executor
    hass.config_entries.async_forward_entry_setups = AsyncMock()

    entry = MagicMock()
    entry.data = MOCK_ENTRY_DATA
    entry.options = {}
    entry.entry_id = "test_123"
    entry.async_get_active_flows.return_value = []

    mock_data_coord = MagicMock()
    mock_data_coord.async_config_entry_first_refresh = AsyncMock()
    mock_duc = MagicMock()
    mock_duc.__getitem__.return_value.side_effect = [mock_data_coord]

    with (
        patch("custom_components.cable_modem_monitor.setup_log_buffer"),
        patch("custom_components.cable_modem_monitor.DataUpdateCoordinator", mock_duc),
        patch("custom_components.cable_modem_monitor._update_device_registry"),
        patch("custom_components.cable_modem_monitor.attach_recovery_cadence_listener"),
    ):
        assert await async_setup_entry(hass, entry) is True
        update_method = mock_duc.__getitem__.return_value.call_args_list[0].kwargs["update_method"]
        result = await update_method()

    assert result is snapshot
    entry.async_start_reauth.assert_called_once_with(hass)


async def test_unavailable_logged_once_per_transition(caplog):
    """Silver log-when-unavailable: one line per edge, not per poll."""
    hass = MagicMock()

    mock_orch = MagicMock()
    mock_orch.supports_restart = False
    mock_orch.diagnostics.return_value.circuit_breaker_open = False
    mock_identity = ModemIdentity(
        manufacturer="Solent Labs",
        model="TPS-2000",
        docsis_version="3.0",
        release_date="2024",
        status="confirmed",
    )

    def _snapshot(*, ok: bool) -> ModemSnapshot:
        if ok:
            return ModemSnapshot(
                connection_status=ConnectionStatus.ONLINE,
                docsis_status=DocsisStatus.UNKNOWN,
                modem_data={"system_info": {}},
                collector_signal=CollectorSignal.OK,
                error="",
            )
        return ModemSnapshot(
            connection_status=ConnectionStatus.UNREACHABLE,
            docsis_status=DocsisStatus.UNKNOWN,
            modem_data=None,
            collector_signal=CollectorSignal.CONNECTIVITY,
            error="unreachable",
        )

    # down, down, down, up, up, down — two outages, one recovery.
    sequence = [
        _snapshot(ok=False),
        _snapshot(ok=False),
        _snapshot(ok=False),
        _snapshot(ok=True),
        _snapshot(ok=True),
        _snapshot(ok=False),
    ]
    pending = list(sequence)
    mock_orch.get_modem_data = MagicMock()

    async def _mock_executor(func, *args):
        if func is mock_orch.get_modem_data:
            return pending.pop(0)
        if not args:
            return "core: v1.0.0, catalog: v1.0.0"
        return (mock_orch, None, mock_identity)

    hass.async_add_executor_job = _mock_executor
    hass.config_entries.async_forward_entry_setups = AsyncMock()

    entry = MagicMock()
    entry.data = MOCK_ENTRY_DATA
    entry.options = {}
    entry.entry_id = "test_123"
    entry.async_get_active_flows.return_value = []

    mock_data_coord = MagicMock()
    mock_data_coord.async_config_entry_first_refresh = AsyncMock()
    mock_duc = MagicMock()
    mock_duc.__getitem__.return_value.side_effect = [mock_data_coord]

    with (
        patch("custom_components.cable_modem_monitor.setup_log_buffer"),
        patch("custom_components.cable_modem_monitor.DataUpdateCoordinator", mock_duc),
        patch("custom_components.cable_modem_monitor._update_device_registry"),
        patch("custom_components.cable_modem_monitor.attach_recovery_cadence_listener"),
    ):
        assert await async_setup_entry(hass, entry) is True
        update_method = mock_duc.__getitem__.return_value.call_args_list[0].kwargs["update_method"]
        with caplog.at_level(logging.INFO, logger="custom_components.cable_modem_monitor"):
            for _ in sequence:
                await update_method()

    # Three consecutive failures produce one "unavailable" line, not three;
    # the later outage produces a second one after the recovery edge.
    assert caplog.text.count("Modem unavailable") == 2
    assert caplog.text.count("Modem available again") == 1
