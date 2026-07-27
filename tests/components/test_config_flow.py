"""Tests for the config flow — wizard steps, options flow, reauth, helpers.

Mocks: load_modem_catalog, load_variant_list, validate_connection.
The config flow is tested through HA's flow machinery.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from solentlabs.cable_modem_monitor_core.orchestration.models import (
    ModemIdentity,
    ModemSnapshot,
)
from solentlabs.cable_modem_monitor_core.orchestration.signals import (
    CollectorSignal,
    ConnectionStatus,
    DocsisStatus,
)

from custom_components.cable_modem_monitor.config_flow import (
    CableModemMonitorConfigFlow,
    _build_prefix_options,
    _duration_to_seconds,
    _seconds_to_duration,
    _ValidationProgress,
)
from custom_components.cable_modem_monitor.const import DOMAIN, EntityPrefix

from .conftest import (
    FAKE_CATALOG,
    MOCK_ENTRY_DATA,
    MOCK_MODEM_DATA,
    MOCK_MULTI_VARIANTS,
    MOCK_SINGLE_VARIANT,
    MOCK_SUMMARIES,
    MOCK_VALIDATION_RESULT,
)

_PATCH_CATALOG_PATH = "custom_components.cable_modem_monitor.config_flow.CATALOG_PATH"

# All tests need the integration to be loadable
pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


# -----------------------------------------------------------------------
# Duration helpers — pure functions
# -----------------------------------------------------------------------

# ┌──────────┬───────────────────────────────────┬──────────────────┐
# │ seconds  │ expected dict                      │ description      │
# ├──────────┼───────────────────────────────────┼──────────────────┤
# │ 600      │ {hours: 0, minutes: 10, seconds: 0} │ 10 minutes     │
# │ 3661     │ {hours: 1, minutes: 1, seconds: 1}  │ mixed          │
# │ 0        │ {hours: 0, minutes: 0, seconds: 0}  │ zero           │
# │ 30       │ {hours: 0, minutes: 0, seconds: 30}│ seconds only    │
# └──────────┴───────────────────────────────────┴──────────────────┘
#
# fmt: off
DURATION_CASES = [
    (600,  {"hours": 0, "minutes": 10, "seconds": 0},  "10_minutes"),
    (3661, {"hours": 1, "minutes": 1,  "seconds": 1},  "mixed"),
    (0,    {"hours": 0, "minutes": 0,  "seconds": 0},  "zero"),
    (30,   {"hours": 0, "minutes": 0,  "seconds": 30}, "seconds_only"),
]
# fmt: on


@pytest.mark.parametrize("seconds,expected,desc", DURATION_CASES, ids=[c[2] for c in DURATION_CASES])
def test_seconds_to_duration(seconds, expected, desc):
    """_seconds_to_duration converts correctly."""
    assert _seconds_to_duration(seconds) == expected


# fmt: off
SECONDS_CASES = [
    ({"hours": 1, "minutes": 2, "seconds": 3}, 3723, "dict_full"),
    ({"minutes": 5},                            300,  "dict_partial"),
    (42,                                        42,   "raw_int"),
]
# fmt: on


@pytest.mark.parametrize("duration,expected,desc", SECONDS_CASES, ids=[c[2] for c in SECONDS_CASES])
def test_duration_to_seconds(duration, expected, desc):
    """_duration_to_seconds handles both dict and raw int."""
    assert _duration_to_seconds(duration) == expected


# -----------------------------------------------------------------------
# _build_prefix_options
# -----------------------------------------------------------------------


def test_prefix_options_first_entry():
    """First entry gets Default option."""
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []
    options = _build_prefix_options(hass)
    values = [o["value"] for o in options]
    assert "none" in values  # "Default" option


def test_prefix_options_none_in_use():
    """When 'none' prefix already used, Default is removed."""
    hass = MagicMock()
    existing = MagicMock()
    existing.data = {"entity_prefix": "none"}
    hass.config_entries.async_entries.return_value = [existing]
    options = _build_prefix_options(hass)
    values = [o["value"] for o in options]
    assert "none" not in values
    assert "model" in values
    assert "ip" in values


# -----------------------------------------------------------------------
# _ValidationProgress
# -----------------------------------------------------------------------


def test_validation_progress_lifecycle():
    """Progress tracks task state and collects results."""
    progress = _ValidationProgress()
    assert not progress.is_running()
    assert progress.result is None
    assert progress.error is None

    progress.reset()
    assert progress.task is None
    assert progress.error_key == "unknown"


async def test_validation_progress_collect_no_task():
    """Collect returns False when no task was started."""
    progress = _ValidationProgress()
    assert await progress.collect() is False


async def test_validation_progress_runtime_error():
    """RuntimeError extracts error key from colon-delimited message."""
    progress = _ValidationProgress()

    async def _raise():
        raise RuntimeError("error_type:modem_locked:Too many attempts")

    loop = asyncio.get_event_loop()
    progress.task = loop.create_task(_raise())
    await asyncio.sleep(0)  # let task complete

    assert await progress.collect() is False
    assert progress.error_key == "modem_locked"
    assert progress.task is None


async def test_validation_progress_unexpected_error():
    """Generic exceptions default to 'unknown' error key."""
    progress = _ValidationProgress()

    async def _raise():
        raise ValueError("something unexpected")

    loop = asyncio.get_event_loop()
    progress.task = loop.create_task(_raise())
    await asyncio.sleep(0)

    assert await progress.collect() is False
    assert progress.error_key == "unknown"
    assert isinstance(progress.error, ValueError)


async def test_validation_progress_connection_error():
    """ConnectionError maps to network_unreachable."""
    progress = _ValidationProgress()

    async def _raise():
        raise ConnectionError("Modem unreachable")

    loop = asyncio.get_event_loop()
    progress.task = loop.create_task(_raise())
    await asyncio.sleep(0)

    assert await progress.collect() is False
    assert progress.error_key == "network_unreachable"
    assert isinstance(progress.error, ConnectionError)


async def test_validation_progress_permission_error():
    """PermissionError extracts error key from colon-delimited format."""
    progress = _ValidationProgress()

    async def _raise():
        raise PermissionError("auth_error:invalid_auth:Bad password")

    loop = asyncio.get_event_loop()
    progress.task = loop.create_task(_raise())
    await asyncio.sleep(0)

    assert await progress.collect() is False
    assert progress.error_key == "invalid_auth"
    assert isinstance(progress.error, PermissionError)


# ┌──────────────────┬───────────┬────────────────┬─────────────────────────┐
# │ exception type   │ message   │ expected key   │ description             │
# ├──────────────────┼───────────┼────────────────┼─────────────────────────┤
# │ PermissionError  │ "denied"  │ invalid_auth   │ auth-shaped fallback    │
# │ RuntimeError     │ "boom"    │ unknown        │ generic fallback        │
# └──────────────────┴───────────┴────────────────┴─────────────────────────┘
#
# fmt: off
UNCLASSIFIED_ERROR_CASES = [
    # (exc_type,       message,  expected_key,   desc)
    (PermissionError, "denied", "invalid_auth", "bare_permission_error"),
    (RuntimeError,    "boom",   "unknown",      "bare_runtime_error"),
]
# fmt: on


@pytest.mark.parametrize(
    "exc_type,message,expected_key,desc",
    UNCLASSIFIED_ERROR_CASES,
    ids=[c[3] for c in UNCLASSIFIED_ERROR_CASES],
)
async def test_validation_progress_unclassified_error(exc_type, message, expected_key, desc):
    """An exception carrying no encoded key falls back by exception type."""
    # These reach the handler without the "{kind}:{key}:{msg}" encoding,
    # e.g. protocol detection failing ahead of the collector run.
    progress = _ValidationProgress()

    async def _raise():
        raise exc_type(message)

    loop = asyncio.get_event_loop()
    progress.task = loop.create_task(_raise())
    await asyncio.sleep(0)

    assert await progress.collect() is False
    assert progress.error_key == expected_key


# -----------------------------------------------------------------------
# Config flow — Step 1a: Manufacturer selection
# -----------------------------------------------------------------------


async def test_step_user_shows_form(hass: HomeAssistant):
    """Step 1a shows manufacturer dropdown."""
    with patch(
        "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
        return_value=MOCK_SUMMARIES,
    ):
        result: Any = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_step_user_selects_manufacturer(hass: HomeAssistant):
    """Selecting a manufacturer advances to model step."""
    with patch(
        "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
        return_value=MOCK_SUMMARIES,
    ):
        result: Any = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"manufacturer": "Solent Labs"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "model"


# -----------------------------------------------------------------------
# Config flow — Step 1b: Model selection
# -----------------------------------------------------------------------


async def test_step_model_selects_model_single_variant(hass: HomeAssistant):
    """Selecting a single-variant modem skips variant step."""
    with (
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_variant_list",
            return_value=MOCK_SINGLE_VARIANT,
        ),
        patch(_PATCH_CATALOG_PATH, FAKE_CATALOG),
    ):
        # Step 1a — init flow
        result: Any = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        # Step 1a — select "All" manufacturers
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"manufacturer": "__all__"},
        )
        # Step 1b — select model (single variant → skip variant step)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"model": "Solent Labs/TPS-2000", "entity_prefix": "none"},
        )

    # Single-variant modem skips step 2, goes straight to connection
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "connection"


# -----------------------------------------------------------------------
# Config flow — Step 2: Variant selection (multi-variant)
# -----------------------------------------------------------------------


async def test_step_variant_shown_for_multi_variant(hass: HomeAssistant):
    """Variant step shown when modem has multiple variants."""
    with (
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_variant_list",
            return_value=MOCK_MULTI_VARIANTS,
        ),
        patch(_PATCH_CATALOG_PATH, FAKE_CATALOG),
    ):
        # Step 1a → Step 1b
        result: Any = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"manufacturer": "__all__"},
        )
        # Step 1b — select multi-variant modem (TPS-3000 has basic + form_nonce)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"model": "Solent Labs/TPS-3000", "entity_prefix": "none"},
        )

    # Multi-variant modem shows step 2
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "variant"


async def test_step_variant_advances_to_connection(hass: HomeAssistant):
    """Selecting a variant advances to connection step."""
    with (
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_variant_list",
            return_value=MOCK_MULTI_VARIANTS,
        ),
        patch(_PATCH_CATALOG_PATH, FAKE_CATALOG),
    ):
        # Steps 1a → 1b → variant form
        result: Any = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"manufacturer": "__all__"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"model": "Solent Labs/TPS-3000", "entity_prefix": "none"},
        )
        # Step 2 — select variant (composite key: {rel_dir}/{name|__default__})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"variant": "solentlabs/tps-3000/v2"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "connection"


# -----------------------------------------------------------------------
# Config flow — Step 3: Connection details
# -----------------------------------------------------------------------


async def test_step_connection_shows_form(hass: HomeAssistant):
    """Connection step shows host and credential fields."""
    with (
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_variant_list",
            return_value=MOCK_SINGLE_VARIANT,
        ),
        patch(_PATCH_CATALOG_PATH, FAKE_CATALOG),
    ):
        # Steps 1a → 1b → connection form (single variant, skips step 2)
        result: Any = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"manufacturer": "__all__"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"model": "Solent Labs/TPS-2000", "entity_prefix": "none"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "connection"


# -----------------------------------------------------------------------
# Config flow — Step 3: no blocking I/O while building the connection schema
# -----------------------------------------------------------------------


async def test_build_connection_schema_does_no_file_io(hass: HomeAssistant):
    """_build_connection_schema must not read modem.yaml on the event loop.

    The restart-credentials check reads modem.yaml; HA flagged that read as a
    blocking call inside the event loop during the connection step. The read is
    now resolved off the loop at variant selection, so building the schema must
    not call restart_requires_credentials at all.
    """
    # Typed Any: the @HANDLERS.register decorator erases the subclass type to
    # ConfigFlow, so the flow's own attributes aren't visible to the type checker.
    flow: Any = CableModemMonitorConfigFlow()
    flow.hass = hass
    # Simulate a resolved none-auth single-variant selection.
    flow._variants = MOCK_SINGLE_VARIANT
    flow._selected_variant = None
    flow._selected_summary = MOCK_SUMMARIES[0]
    flow._selected_modem_dir = MOCK_SUMMARIES[0].path

    with patch(
        "custom_components.cable_modem_monitor.config_flow.restart_requires_credentials",
        side_effect=AssertionError("must not read modem.yaml on the event loop"),
    ):
        schema = flow._build_connection_schema()

    assert schema is not None


# -----------------------------------------------------------------------
# Config flow — Step 3: Connection shows the selected variant (#176)
# -----------------------------------------------------------------------


async def test_connection_form_shows_selected_variant(hass: HomeAssistant):
    """Connection form surfaces the chosen variant so the user can see what they picked.

    #176, change 1: a multi-variant modem hides which variant is active once
    the picker closes. The connection form now carries the variant's human
    label in description_placeholders.
    """
    with (
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_variant_list",
            return_value=MOCK_MULTI_VARIANTS,
        ),
        patch(_PATCH_CATALOG_PATH, FAKE_CATALOG),
    ):
        result: Any = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"manufacturer": "__all__"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"model": "Solent Labs/TPS-3000", "entity_prefix": "none"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"variant": "solentlabs/tps-3000/v2"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "connection"
    # The selected variant's label is shown; the v2 qualifier ties it to the pick.
    assert "v2" in result["description_placeholders"]["variant"]


async def test_single_variant_connection_still_populates_variant(hass: HomeAssistant):
    """Single-variant modems also populate the variant placeholder.

    The connection string references {variant}; if a single-variant flow left
    it unset the string would fail to render. The label is set even when the
    picker step is skipped.
    """
    with (
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_variant_list",
            return_value=MOCK_SINGLE_VARIANT,
        ),
        patch(_PATCH_CATALOG_PATH, FAKE_CATALOG),
    ):
        result: Any = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"manufacturer": "__all__"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"model": "Solent Labs/TPS-2000", "entity_prefix": "none"},
        )

    assert result["step_id"] == "connection"
    assert result["description_placeholders"]["variant"]


# -----------------------------------------------------------------------
# Config flow — Step 4: Validation + entry creation
# -----------------------------------------------------------------------


async def test_full_flow_creates_entry(hass: HomeAssistant):
    """Complete flow through validation creates config entry."""
    with (
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_variant_list",
            return_value=MOCK_SINGLE_VARIANT,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.validate_connection",
            return_value=MOCK_VALIDATION_RESULT,
        ),
        patch(
            "custom_components.cable_modem_monitor.async_setup_entry",
            return_value=True,
        ),
        patch(_PATCH_CATALOG_PATH, FAKE_CATALOG),
    ):
        # Step 1a → Step 1b → Step 3 (single variant, skip step 2)
        result: Any = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"manufacturer": "__all__"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"model": "Solent Labs/TPS-2000", "entity_prefix": "none"},
        )
        # Step 3 → connection details (triggers step 4 validation)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"host": "192.168.100.1"},
        )

        # Step 4 — drive through progress spinner states.
        # Mock validate_connection resolves instantly, so HA may skip
        # the spinner or condense the steps.
        while result["type"] in (FlowResultType.SHOW_PROGRESS, FlowResultType.SHOW_PROGRESS_DONE):
            result = await hass.config_entries.flow.async_configure(result["flow_id"])

    # Successful validation creates config entry
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Solent Labs TPS-2000 (192.168.100.1)"
    assert result["data"]["protocol"] == "http"
    assert result["data"]["manufacturer"] == "Solent Labs"
    assert result["data"]["channel_identity"] == "number"
    # ICMP + HEAD both detected — standard 30s default.
    assert result["data"]["health_check_interval"] == 30


async def test_full_flow_sibling_variant_uses_sibling_modem_dir(hass: HomeAssistant):
    """Config entry modem_dir reflects the sibling directory when a sibling variant is chosen.

    This is the core correctness guarantee of the cross-directory grouping feature:
    if the user selects HNAP (from sb8200-hnap/) rather than URL Token (from sb8200/),
    the stored modem_dir must point to sb8200-hnap/, not sb8200/.
    """
    from solentlabs.cable_modem_monitor_core.catalog_manager import ModemSummary, VariantInfo

    sibling_dir = FAKE_CATALOG / "solentlabs" / "tps-3000-hnap"
    primary_dir = FAKE_CATALOG / "solentlabs" / "tps-3000"

    # Summary with a sibling — simulates grouped catalog entry
    summary_with_sibling = ModemSummary(
        manufacturer="Solent Labs",
        model="TPS-3000",
        default_host="192.168.100.1",
        auth_strategy="url_token",
        status="confirmed",
        path=primary_dir,
        sibling_dirs=[sibling_dir],
    )

    # Two variants: one from primary dir (url_token), one from sibling dir (hnap)
    multi_transport_variants = [
        VariantInfo(name=None, auth_strategy="url_token", path=primary_dir / "modem.yaml"),
        VariantInfo(name=None, auth_strategy="hnap", path=sibling_dir / "modem.yaml"),
    ]

    # Composite key for the sibling's default variant
    sibling_key = "solentlabs/tps-3000-hnap/__default__"

    with (
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=[summary_with_sibling],
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_variant_list",
            return_value=multi_transport_variants,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.validate_connection",
            return_value=MOCK_VALIDATION_RESULT,
        ),
        patch(
            "custom_components.cable_modem_monitor.async_setup_entry",
            return_value=True,
        ),
        patch(_PATCH_CATALOG_PATH, FAKE_CATALOG),
    ):
        result: Any = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"manufacturer": "__all__"},
        )
        # Step 1b — select TPS-3000 (has two transport variants)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"model": "Solent Labs/TPS-3000", "entity_prefix": "none"},
        )
        # Step 2 — select the sibling HNAP variant
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"variant": sibling_key},
        )
        # Step 3 — connection details
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"host": "192.168.100.1"},
        )
        while result["type"] in (FlowResultType.SHOW_PROGRESS, FlowResultType.SHOW_PROGRESS_DONE):
            result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # modem_dir must point to the sibling directory, not the primary summary path
    assert result["data"]["modem_dir"] == "solentlabs/tps-3000-hnap"
    assert result["data"]["variant"] is None  # default variant within that dir


async def test_full_flow_get_only_modem_uses_default_cadence(hass: HomeAssistant):
    """GET-only modems (no ICMP, no HEAD) use the same default cadence.

    The per-capability cadence differentiation was removed when the HTTP GET
    probe was replaced with TCP connect — TCP and ICMP probes are lightweight,
    so a single 30s default applies regardless of HEAD support.
    """
    get_only_validation = {
        **MOCK_VALIDATION_RESULT,
        "supports_icmp": False,
        "supports_head": False,
    }

    with (
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_variant_list",
            return_value=MOCK_SINGLE_VARIANT,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.validate_connection",
            return_value=get_only_validation,
        ),
        patch(
            "custom_components.cable_modem_monitor.async_setup_entry",
            return_value=True,
        ),
        patch(_PATCH_CATALOG_PATH, FAKE_CATALOG),
    ):
        result: Any = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"manufacturer": "__all__"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"model": "Solent Labs/TPS-2000", "entity_prefix": "none"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"host": "192.168.100.1"},
        )
        while result["type"] in (FlowResultType.SHOW_PROGRESS, FlowResultType.SHOW_PROGRESS_DONE):
            result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["health_check_interval"] == 30


# -----------------------------------------------------------------------
# Config flow — Validation errors
# -----------------------------------------------------------------------


async def test_validation_connection_error_shows_form(hass: HomeAssistant):
    """Connection error during validation returns to connection form."""
    with (
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_variant_list",
            return_value=MOCK_SINGLE_VARIANT,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.validate_connection",
            side_effect=ConnectionError("Modem unreachable"),
        ),
        patch(_PATCH_CATALOG_PATH, FAKE_CATALOG),
    ):
        # Steps 1a → 1b → 3 → 4 (validation raises ConnectionError)
        result: Any = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"manufacturer": "__all__"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"model": "Solent Labs/TPS-2000", "entity_prefix": "none"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"host": "192.168.100.1"},
        )

        # Drive through progress states after validation failure
        while result["type"] in (FlowResultType.SHOW_PROGRESS, FlowResultType.SHOW_PROGRESS_DONE):
            result = await hass.config_entries.flow.async_configure(result["flow_id"])

    # Connection error returns to step 3 with error banner
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "connection"
    assert "base" in result.get("errors", {})


async def test_validation_auth_error(hass: HomeAssistant):
    """Auth error during validation shows error on connection form."""
    with (
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_variant_list",
            return_value=MOCK_SINGLE_VARIANT,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.validate_connection",
            side_effect=PermissionError("auth_error:invalid_auth:Bad password"),
        ),
        patch(_PATCH_CATALOG_PATH, FAKE_CATALOG),
    ):
        # Steps 1a → 1b → 3 → 4 (validation raises PermissionError)
        result: Any = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"manufacturer": "__all__"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"model": "Solent Labs/TPS-2000", "entity_prefix": "none"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"host": "192.168.100.1"},
        )

        # Drive through progress states after auth failure
        while result["type"] in (FlowResultType.SHOW_PROGRESS, FlowResultType.SHOW_PROGRESS_DONE):
            result = await hass.config_entries.flow.async_configure(result["flow_id"])

    # Auth error returns to step 3 with specific error key
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "connection"
    assert result["errors"]["base"] == "invalid_auth"


# -----------------------------------------------------------------------
# Config flow — Step 4: Failure recovery on the connection form (#176)
# -----------------------------------------------------------------------


def _schema_fields(result: Any) -> set[str]:
    """Field names present in a form result's data schema."""
    return {str(key) for key in result["data_schema"].schema}


async def _drive_multi_variant_to_failure(hass: HomeAssistant, *, connection: dict[str, Any]) -> Any:
    """Run a multi-variant flow (variant v2) through a failed validation; return the error form.

    Assumes the caller holds the load_modem_catalog / load_variant_list /
    validate_connection patches open.
    """
    result: Any = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"manufacturer": "__all__"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"model": "Solent Labs/TPS-3000", "entity_prefix": "none"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"variant": "solentlabs/tps-3000/v2"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=connection)
    while result["type"] in (FlowResultType.SHOW_PROGRESS, FlowResultType.SHOW_PROGRESS_DONE):
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    return result


async def test_multi_variant_failure_shows_error_form_with_variant_switch(hass: HomeAssistant):
    """A multi-variant failure returns to the connection form with the error and an inline variant switch (#176)."""
    with (
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_variant_list",
            return_value=MOCK_MULTI_VARIANTS,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.validate_connection",
            side_effect=PermissionError("auth_error:invalid_auth:Bad password"),
        ),
        patch(_PATCH_CATALOG_PATH, FAKE_CATALOG),
    ):
        result = await _drive_multi_variant_to_failure(hass, connection={"host": "192.168.100.1"})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "connection"
    # The full, translated reason is shown via the error banner (not a hand-rolled string).
    assert result["errors"]["base"] == "invalid_auth"
    # The form offers an inline variant switch.
    assert "variant" in _schema_fields(result)


async def test_single_variant_failure_has_no_variant_switch(hass: HomeAssistant):
    """A single-variant failure shows the error form without a variant field (nothing to switch to)."""
    with (
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_variant_list",
            return_value=MOCK_SINGLE_VARIANT,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.validate_connection",
            side_effect=ConnectionError("unreachable"),
        ),
        patch(_PATCH_CATALOG_PATH, FAKE_CATALOG),
    ):
        result: Any = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"manufacturer": "__all__"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"model": "Solent Labs/TPS-2000", "entity_prefix": "none"}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"host": "192.168.100.1"})
        while result["type"] in (FlowResultType.SHOW_PROGRESS, FlowResultType.SHOW_PROGRESS_DONE):
            result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result["step_id"] == "connection"
    assert result["errors"]["base"] == "network_unreachable"
    assert "variant" not in _schema_fields(result)


async def test_failure_form_preserves_entered_connection(hass: HomeAssistant):
    """The error form keeps the host and credentials already typed (#176)."""
    with (
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_variant_list",
            return_value=MOCK_MULTI_VARIANTS,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.validate_connection",
            side_effect=PermissionError("auth_error:invalid_auth:Bad password"),
        ),
        patch(_PATCH_CATALOG_PATH, FAKE_CATALOG),
    ):
        result = await _drive_multi_variant_to_failure(
            hass, connection={"host": "10.0.0.5", "username": "myuser", "password": "secret"}
        )

    assert result["step_id"] == "connection"
    applied = result["data_schema"]({})
    assert applied["host"] == "10.0.0.5"
    assert applied["username"] == "myuser"


async def test_switch_variant_on_failure_form_revalidates(hass: HomeAssistant):
    """Switching variant on the error form re-runs validation with the new variant (#176)."""
    with (
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_variant_list",
            return_value=MOCK_MULTI_VARIANTS,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.validate_connection",
            side_effect=[PermissionError("auth_error:invalid_auth:Bad password"), MOCK_VALIDATION_RESULT],
        ),
        patch("custom_components.cable_modem_monitor.async_setup_entry", return_value=True),
        patch(_PATCH_CATALOG_PATH, FAKE_CATALOG),
    ):
        result: Any = await _drive_multi_variant_to_failure(hass, connection={"host": "192.168.100.1"})
        assert result["step_id"] == "connection"
        # Switch to the default variant and resubmit; second validation succeeds.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"variant": "solentlabs/tps-3000/__default__", "host": "192.168.100.1"},
        )
        while result["type"] in (FlowResultType.SHOW_PROGRESS, FlowResultType.SHOW_PROGRESS_DONE):
            result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The switched-to default variant (name=None) is what got recorded.
    assert result["data"]["variant"] is None


async def test_switch_to_credentialed_variant_rerenders_before_validating(hass: HomeAssistant):
    """Switching from a no-auth variant to one needing credentials re-renders the form first (#176).

    HA forms are static, so the credential fields only appear on a re-render. The
    flow must show them before validating, not fail a credential-less attempt first.
    """
    from solentlabs.cable_modem_monitor_core.catalog_manager import VariantInfo

    base_dir = FAKE_CATALOG / "solentlabs" / "tps-3000"
    variants = [
        VariantInfo(name=None, auth_strategy="none", path=base_dir / "modem.yaml"),
        VariantInfo(name="secure", auth_strategy="basic", path=base_dir / "modem-secure.yaml"),
    ]
    with (
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_variant_list",
            return_value=variants,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.validate_connection",
            side_effect=ConnectionError("unreachable"),
        ) as mock_validate,
        patch(_PATCH_CATALOG_PATH, FAKE_CATALOG),
    ):
        result: Any = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"manufacturer": "__all__"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"model": "Solent Labs/TPS-3000", "entity_prefix": "none"}
        )
        # Pick the no-auth variant first; its connection form has no credentials.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"variant": "solentlabs/tps-3000/__default__"}
        )
        assert "username" not in _schema_fields(result)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"host": "192.168.100.1"})
        while result["type"] in (FlowResultType.SHOW_PROGRESS, FlowResultType.SHOW_PROGRESS_DONE):
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["step_id"] == "connection"
        assert mock_validate.call_count == 1
        # Switch to the credentialed variant: form must re-render with the password
        # field, without running a second validation.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"variant": "solentlabs/tps-3000/secure", "host": "192.168.100.1"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "connection"
    assert "username" in _schema_fields(result)
    assert "password" in _schema_fields(result)
    assert mock_validate.call_count == 1  # no re-validation triggered by the switch


# -----------------------------------------------------------------------
# Options flow
# -----------------------------------------------------------------------


def _options_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create a MockConfigEntry for options flow tests."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data=MOCK_ENTRY_DATA,
        options={},
    )
    entry.add_to_hass(hass)
    return entry


async def test_options_step_init_shows_form(hass: HomeAssistant):
    """Options init step shows form with current values."""
    entry = _options_entry(hass)

    with patch("custom_components.cable_modem_monitor.async_setup_entry", return_value=True):
        await hass.config_entries.async_setup(entry.entry_id)

    result: Any = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"


async def test_options_full_flow_success(hass: HomeAssistant):
    """Complete options flow validates and creates entry."""
    entry = _options_entry(hass)

    with (
        patch("custom_components.cable_modem_monitor.async_setup_entry", return_value=True),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.validate_connection",
            return_value=MOCK_VALIDATION_RESULT,
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)

        result: Any = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                "host": "192.168.100.1",
                "username": "admin",
                "password": "newpass",
                "scan_interval": {"hours": 0, "minutes": 5, "seconds": 0},
                "health_check_interval": {"hours": 0, "minutes": 1, "seconds": 0},
            },
        )

        # Drive through progress spinner
        while result["type"] in (FlowResultType.SHOW_PROGRESS, FlowResultType.SHOW_PROGRESS_DONE):
            result = await hass.config_entries.options.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["scan_interval"] == 300
    assert result["data"]["health_check_interval"] == 60


async def test_options_flow_validation_failure(hass: HomeAssistant):
    """Validation error in options flow shows error form."""
    entry = _options_entry(hass)

    with (
        patch("custom_components.cable_modem_monitor.async_setup_entry", return_value=True),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.validate_connection",
            side_effect=ConnectionError("Modem unreachable"),
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)

        result: Any = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                "host": "192.168.100.1",
                "username": "admin",
                "password": "pass",
                "scan_interval": {"hours": 0, "minutes": 10, "seconds": 0},
                "health_check_interval": {"hours": 0, "minutes": 0, "seconds": 30},
            },
        )

        while result["type"] in (FlowResultType.SHOW_PROGRESS, FlowResultType.SHOW_PROGRESS_DONE):
            result = await hass.config_entries.options.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["errors"]["base"] == "network_unreachable"


async def test_options_password_preserved(hass: HomeAssistant):
    """Blank password in options preserves existing password from entry data."""
    entry = _options_entry(hass)

    with (
        patch("custom_components.cable_modem_monitor.async_setup_entry", return_value=True),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.validate_connection",
            return_value=MOCK_VALIDATION_RESULT,
        ) as mock_validate,
    ):
        await hass.config_entries.async_setup(entry.entry_id)

        result: Any = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                "host": "192.168.100.1",
                "username": "admin",
                "password": "",  # blank — should preserve "password" from entry
                "scan_interval": {"hours": 0, "minutes": 10, "seconds": 0},
                "health_check_interval": {"hours": 0, "minutes": 0, "seconds": 30},
            },
        )

        while result["type"] in (FlowResultType.SHOW_PROGRESS, FlowResultType.SHOW_PROGRESS_DONE):
            result = await hass.config_entries.options.async_configure(result["flow_id"])

    # Validate was called with the original password, not blank
    call_kwargs = mock_validate.call_args
    assert call_kwargs.kwargs["password"] == "password"


# -----------------------------------------------------------------------
# Reauth flow
# -----------------------------------------------------------------------


async def test_reauth_shows_form(hass: HomeAssistant):
    """Reauth flow shows credential form."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data=MOCK_ENTRY_DATA,
        unique_id="192.168.100.1",
    )
    entry.add_to_hass(hass)

    with patch("custom_components.cable_modem_monitor.async_setup_entry", return_value=True):
        await hass.config_entries.async_setup(entry.entry_id)

    result: Any = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"


async def test_reauth_success(hass: HomeAssistant):
    """Successful reauth updates entry, reloads it, and aborts with reauth_successful."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data=MOCK_ENTRY_DATA,
        unique_id="192.168.100.1",
    )
    entry.add_to_hass(hass)
    # setup is mocked, so runtime_data is never populated; the real
    # async_unload_entry on reload needs orchestrator.close() to exist.
    entry.runtime_data = MagicMock()

    with (
        patch("custom_components.cable_modem_monitor.async_setup_entry", return_value=True) as mock_setup,
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.validate_connection",
            return_value=MOCK_VALIDATION_RESULT,
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)

        result: Any = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "host": "192.168.100.1",
                "username": "admin",
                "password": "newpassword",
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "newpassword"
    # The reload is what delivers the auth reset — a fresh orchestrator
    # is built from the updated entry (UC-16). Setup ran once when the
    # entry was added, once more on the reauth-triggered reload.
    assert mock_setup.call_count == 2


async def test_reauth_failure_shows_error(hass: HomeAssistant):
    """Failed reauth re-shows form with invalid_auth error."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data=MOCK_ENTRY_DATA,
        unique_id="192.168.100.1",
    )
    entry.add_to_hass(hass)

    with (
        patch("custom_components.cable_modem_monitor.async_setup_entry", return_value=True),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.validate_connection",
            side_effect=PermissionError("auth_error:invalid_auth:Bad password"),
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)

        result: Any = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "host": "192.168.100.1",
                "username": "admin",
                "password": "wrongpassword",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"]["base"] == "invalid_auth"


async def test_reauth_surfaces_session_rejection_not_bad_credentials(hass: HomeAssistant):
    """A post-login refusal keeps its own key instead of reading as a bad password (#120).

    A LOAD_AUTH streak opens the breaker and prompts for reauth, so a user
    whose credentials are correct lands on this form; telling them to check
    their password sends them after one the modem already accepted.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data=MOCK_ENTRY_DATA,
        unique_id="192.168.100.1",
    )
    entry.add_to_hass(hass)

    with (
        patch("custom_components.cable_modem_monitor.async_setup_entry", return_value=True),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.validate_connection",
            side_effect=PermissionError("auth_error:session_rejected:401 on /api/v1/sta_docsis_status"),
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)

        result: Any = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "host": "192.168.100.1",
                "username": "admin",
                "password": "correcthorse",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"]["base"] == "session_rejected"


async def test_reauth_connection_error_shows_network_unreachable(hass: HomeAssistant):
    """A pre-classification ConnectionError keeps its own key, not the auth fallback."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data=MOCK_ENTRY_DATA,
        unique_id="192.168.100.1",
    )
    entry.add_to_hass(hass)

    with (
        patch("custom_components.cable_modem_monitor.async_setup_entry", return_value=True),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.validate_connection",
            side_effect=ConnectionError("Cannot connect to 192.168.100.1"),
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)

        result: Any = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "host": "192.168.100.1",
                "username": "admin",
                "password": "correcthorse",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"]["base"] == "network_unreachable"


async def test_reauth_full_loop_recovers_to_online(hass: HomeAssistant):
    """UC-81 end to end: breaker-open lockout → reauth flow → reload → ONLINE."""
    # The one test that runs the REAL async_setup_entry — twice: the
    # initial setup and the reload triggered by reauth success.
    # _create_core_components hands the first setup a locked-out
    # orchestrator and the second a healthy one, standing in for
    # "the user fixed the password."
    identity = ModemIdentity(
        manufacturer="Solent Labs",
        model="TPS-2000",
        docsis_version="3.0",
        release_date="2024",
        status="confirmed",
    )
    locked_snapshot = ModemSnapshot(
        connection_status=ConnectionStatus.AUTH_FAILED,
        docsis_status=DocsisStatus.UNKNOWN,
        modem_data=None,
        collector_signal=CollectorSignal.AUTH_FAILED,
        error="auth failed",
    )
    online_snapshot = ModemSnapshot(
        connection_status=ConnectionStatus.ONLINE,
        docsis_status=DocsisStatus.OPERATIONAL,
        modem_data=MOCK_MODEM_DATA,
        collector_signal=CollectorSignal.OK,
    )

    locked_orch = MagicMock()
    locked_orch.supports_restart = False
    locked_orch.get_modem_data.return_value = locked_snapshot
    locked_orch.diagnostics.return_value.circuit_breaker_open = True

    healthy_orch = MagicMock()
    healthy_orch.supports_restart = False
    healthy_orch.get_modem_data.return_value = online_snapshot
    healthy_orch.diagnostics.return_value.circuit_breaker_open = False

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data=MOCK_ENTRY_DATA,
        unique_id="192.168.100.1",
    )
    entry.add_to_hass(hass)

    # Keyed on the received entry data, not a call-count sequence:
    # old password → locked out, new → healthy. Reauth success can
    # reload more than once; keying on data stays correct regardless.
    def _fake_components(data):
        if data.get("password") == "newpassword":
            return (healthy_orch, None, identity)
        return (locked_orch, None, identity)

    with (
        patch(
            "custom_components.cable_modem_monitor._create_core_components",
            side_effect=_fake_components,
        ),
        # setup_log_buffer attaches handlers to process-global loggers;
        # patch it out so this real-setup test stays hermetic (the log
        # buffer is orthogonal to the reauth path under test).
        patch("custom_components.cable_modem_monitor.setup_log_buffer"),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.validate_connection",
            return_value=MOCK_VALIDATION_RESULT,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # The locked-out first poll started HA's reauth flow.
        flows = [
            f
            for f in hass.config_entries.flow.async_progress()
            if f["handler"] == DOMAIN and f.get("context", {}).get("source") == config_entries.SOURCE_REAUTH
        ]
        assert len(flows) == 1

        result: Any = await hass.config_entries.flow.async_configure(
            flows[0]["flow_id"],
            user_input={
                "host": "192.168.100.1",
                "username": "admin",
                "password": "newpassword",
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "newpassword"
    # The reload rebuilt Core components from the updated entry; the
    # fresh orchestrator's poll came back ONLINE — recovery complete.
    assert entry.runtime_data.data_coordinator.data.connection_status is ConnectionStatus.ONLINE


# -----------------------------------------------------------------------
# Edge cases
# -----------------------------------------------------------------------


async def test_duplicate_entity_prefix_aborts(hass: HomeAssistant):
    """A new entry with the same entity_prefix as an existing entry aborts.

    Deduplication keys on entity_prefix because the prefix controls
    every entity_id this integration creates — two entries claiming
    the same prefix would collide regardless of host or model.
    """
    existing = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data=MOCK_ENTRY_DATA,
        unique_id=str(EntityPrefix.MODEL),
    )
    existing.add_to_hass(hass)

    with (
        patch("custom_components.cable_modem_monitor.async_setup_entry", return_value=True),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_variant_list",
            return_value=MOCK_SINGLE_VARIANT,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.validate_connection",
            return_value=MOCK_VALIDATION_RESULT,
        ),
        patch(_PATCH_CATALOG_PATH, FAKE_CATALOG),
    ):
        await hass.config_entries.async_setup(existing.entry_id)

        result: Any = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"manufacturer": "__all__"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"model": "Solent Labs/TPS-2000", "entity_prefix": str(EntityPrefix.MODEL)},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"host": "192.168.100.1"},
        )

        while result["type"] in (FlowResultType.SHOW_PROGRESS, FlowResultType.SHOW_PROGRESS_DONE):
            result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_different_entity_prefix_at_same_host_succeeds(hass: HomeAssistant):
    """Same hostname with a different entity_prefix is allowed.

    Supports the swap-modem and multi-view workflows (e.g. testing a
    different modem on the same default IP, or running two dashboards
    against the same modem with different entity prefixes).
    """
    existing = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data=MOCK_ENTRY_DATA,
        unique_id="other_prefix",  # different from what the new flow will use
    )
    existing.add_to_hass(hass)

    with (
        patch("custom_components.cable_modem_monitor.async_setup_entry", return_value=True),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_modem_catalog",
            return_value=MOCK_SUMMARIES,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.load_variant_list",
            return_value=MOCK_SINGLE_VARIANT,
        ),
        patch(
            "custom_components.cable_modem_monitor.config_flow.validate_connection",
            return_value=MOCK_VALIDATION_RESULT,
        ),
        patch(_PATCH_CATALOG_PATH, FAKE_CATALOG),
    ):
        await hass.config_entries.async_setup(existing.entry_id)

        result: Any = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"manufacturer": "__all__"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"model": "Solent Labs/TPS-2000", "entity_prefix": "model"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"host": "192.168.100.1"},
        )

        while result["type"] in (FlowResultType.SHOW_PROGRESS, FlowResultType.SHOW_PROGRESS_DONE):
            result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
