"""Config flow for Cable Modem Monitor integration.

Four-step setup wizard that guides the user from "I want to monitor
my modem" to a working integration entry:

    Step 1a  — Select manufacturer (or "All")
    Step 1b  — Select model + entity prefix
    Step 2   — Select variant  (skipped for single-variant modems)
    Step 3   — Enter connection details (host, credentials)
    Step 4   — Validate  (progress spinner)

Plus:
    Options flow  — change host, credentials, prefix, intervals
    Reauth flow   — re-enter credentials when circuit breaker opens

See CONFIG_FLOW_SPEC.md for the full specification.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from solentlabs.cable_modem_monitor_catalog import CATALOG_PATH
from solentlabs.cable_modem_monitor_core.catalog_manager import (
    ModemSummary,
    VariantInfo,
)

from .config_flow_helpers import (
    build_model_display_name,
    build_model_options,
    default_health_check_interval,
    format_variant_labels,
    get_manufacturers,
    load_modem_catalog,
    load_variant_list,
    restart_requires_credentials,
    validate_connection,
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
    CONF_USER_SELECTED_MODEM,
    CONF_VARIANT,
    DEFAULT_HEALTH_CHECK_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ChannelIdentity,
    EntityPrefix,
)
from .lib.host_validation import parse_host_input

_LOGGER = logging.getLogger(__name__)

# Sentinel value for the "All manufacturers" option
_ALL_MANUFACTURERS = "__all__"

# Sentinel value for the default variant (modem.yaml, name=None).
# HA's SelectSelector rejects empty-string option values.
_DEFAULT_VARIANT = "__default__"

# Cooloff window before options-flow validation re-auths the modem.
# Some modems (e.g. MB7621) only allow one logged-in session and
# silently invalidate older sessions when overlapping auths arrive.
# Wait until no collection is in flight and a quiet window has
# elapsed since the last successful collection.
_COOLOFF_QUIET_SECONDS = 5.0
_COOLOFF_POLL_SECONDS = 0.5
_COOLOFF_TIMEOUT_SECONDS = 30.0


# =============================================================================
# Validation progress helper
# =============================================================================


def _validation_error_key(exc: Exception) -> str:
    """Map a validation exception to its strings.json error key."""
    # _raise_validation_failure encodes the classified key as
    # "{kind}:{key}:{message}". The per-type fallbacks cover exceptions
    # raised before classification ran, e.g. protocol detection.
    if isinstance(exc, ConnectionError):
        return "network_unreachable"
    parts = str(exc).split(":", 2)
    if len(parts) >= 2:
        return parts[1]
    return "invalid_auth" if isinstance(exc, PermissionError) else "unknown"


class _ValidationProgress:
    """Manages async validation state for HA's progress-spinner pattern.

    HA config flows show a spinner while a background task runs.  This
    helper encapsulates the task lifecycle, result caching, and error
    handling.
    """

    def __init__(self) -> None:
        """Initialize with empty state."""
        self.task: asyncio.Task[dict[str, Any]] | None = None
        self.result: dict[str, Any] | None = None
        self.error: Exception | None = None
        self.error_key: str = "unknown"

    def start(
        self,
        hass: HomeAssistant,
        coro: Any,
    ) -> None:
        """Start the validation coroutine as a background task."""
        self.task = hass.async_create_task(coro)

    def is_running(self) -> bool:
        """Return True if the task is still in progress."""
        return self.task is not None and not self.task.done()

    async def collect(self) -> bool:
        """Await the task and store the outcome.

        Returns:
            True if validation succeeded.
        """
        if self.task is None:
            return False

        try:
            self.result = await self.task
            return True
        except (ConnectionError, PermissionError, RuntimeError) as exc:
            self.error = exc
            self.error_key = _validation_error_key(exc)
        except Exception as exc:
            _LOGGER.exception("Unexpected validation error")
            self.error = exc
            self.error_key = "unknown"
        finally:
            self.task = None

        return False

    def reset(self) -> None:
        """Clear all state for the next attempt."""
        self.task = None
        self.result = None
        self.error = None
        self.error_key = "unknown"


# =============================================================================
# Main config flow — 4-step wizard
# =============================================================================


@config_entries.HANDLERS.register(DOMAIN)
class CableModemMonitorConfigFlow(config_entries.ConfigFlow):
    """Handle initial setup flow for Cable Modem Monitor.

    State is carried across steps via instance attributes.  Each step
    narrows the user's selection until validation confirms everything
    works.
    """

    VERSION = 2

    def __init__(self) -> None:
        """Initialize config flow state."""
        self._summaries: list[ModemSummary] = []
        self._selected_manufacturer: str = _ALL_MANUFACTURERS
        self._selected_summary: ModemSummary | None = None
        self._variants: list[VariantInfo] = []
        self._selected_variant: str | None = None
        # Human label of the selected variant, shown on the connection form
        # so the user can see which variant they are configuring (#176).
        self._selected_variant_label: str = ""
        # Whether the selected variant's restart action needs per-action
        # credentials despite top-level auth "none". Resolved off the event
        # loop at variant selection (reads modem.yaml); read by
        # _build_connection_schema, which must not do I/O.
        self._restart_needs_credentials: bool = False
        # Directory of the selected variant — may differ from summary.path
        # when a sibling transport directory was chosen in Step 2.
        self._selected_modem_dir: Path | None = None
        self._progress = _ValidationProgress()
        # Carried from connection step for retry on error
        self._connection_input: dict[str, Any] | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> OptionsFlowHandler:
        """Return the options flow handler."""
        return OptionsFlowHandler()

    # -----------------------------------------------------------------
    # Step 1a: Select manufacturer
    # -----------------------------------------------------------------

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Step 1a — Select manufacturer."""
        if not self._summaries:
            self._summaries = await load_modem_catalog(self.hass)

        if user_input is not None:
            self._selected_manufacturer = user_input["manufacturer"]
            return await self.async_step_model()

        manufacturers = get_manufacturers(self._summaries)
        options = [
            selector.SelectOptionDict(value=_ALL_MANUFACTURERS, label="All"),
        ] + [selector.SelectOptionDict(value=m, label=m) for m in manufacturers]

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("manufacturer"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    # -----------------------------------------------------------------
    # Step 1b: Select model + entity prefix
    # -----------------------------------------------------------------

    async def async_step_model(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Step 1b — Select model and entity prefix."""
        if user_input is not None:
            # Find the selected summary by model key. Brand rows in the
            # All view suffix the value with "|{brand}" — strip it; both
            # rows resolve to the same modem.
            model_key = user_input["model"].split("|", 1)[0]
            for s in self._summaries:
                if f"{s.manufacturer}/{s.model}" == model_key:
                    self._selected_summary = s
                    break

            if self._selected_summary is None:
                return self.async_abort(reason="unknown_model")

            # Check for variants — includes sibling transport directories
            self._variants = await load_variant_list(
                self.hass,
                self._selected_summary.path,
                self._selected_summary.sibling_dirs or None,
            )

            # Store entity prefix and channel identity for later
            self._connection_input = {
                CONF_ENTITY_PREFIX: user_input.get(CONF_ENTITY_PREFIX, EntityPrefix.NONE),
                CONF_CHANNEL_IDENTITY: user_input.get(CONF_CHANNEL_IDENTITY, ChannelIdentity.NUMBER),
            }

            if len(self._variants) > 1:
                return await self.async_step_variant()

            # Single variant — skip Step 2
            if self._variants:
                await self._apply_variant(self._variants[0])
            else:
                self._selected_modem_dir = self._selected_summary.path
            return await self.async_step_connection()

        # Build model dropdown — All lists one row per user-facing name
        bucket = None if self._selected_manufacturer == _ALL_MANUFACTURERS else self._selected_manufacturer
        model_options = [
            selector.SelectOptionDict(value=value, label=label)
            for value, label in build_model_options(self._summaries, bucket)
        ]

        # Entity prefix options
        prefix_options = _build_prefix_options(self.hass)

        # Channel identity options
        identity_options = [
            selector.SelectOptionDict(value=ChannelIdentity.NUMBER, label="Channel Number (recommended)"),
            selector.SelectOptionDict(value=ChannelIdentity.ID, label="Channel ID (DOCSIS)"),
        ]

        # Show selected manufacturer in the step description.
        # "All" becomes empty so the description reads naturally:
        # "Choose a modem model" instead of "Choose a All modem model."
        if self._selected_manufacturer == _ALL_MANUFACTURERS:
            mfr_display = ""
        else:
            mfr_display = f"{self._selected_manufacturer} "

        return self.async_show_form(
            step_id="model",
            description_placeholders={"manufacturer": mfr_display},
            data_schema=vol.Schema(
                {
                    vol.Required("model"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=model_options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_ENTITY_PREFIX,
                        default=prefix_options[0]["value"],
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=prefix_options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_CHANNEL_IDENTITY,
                        default=ChannelIdentity.NUMBER,
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=identity_options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    # -----------------------------------------------------------------
    # Step 2: Select variant (conditional)
    # -----------------------------------------------------------------

    async def async_step_variant(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Step 2 — Select variant (only shown for multi-variant modems)."""
        if user_input is not None:
            selected_v = self._find_variant(user_input["variant"])
            if selected_v is None:
                return self.async_abort(reason="unknown_variant")
            await self._apply_variant(selected_v)
            return await self.async_step_connection()

        return self.async_show_form(
            step_id="variant",
            data_schema=vol.Schema(
                {
                    vol.Required("variant"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=self._variant_options(),
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    def _variant_key(self, variant: VariantInfo) -> str:
        """Composite dropdown value ``{rel_dir}/{name|__default__}`` — unique across sibling dirs."""
        rel = str(variant.path.parent.relative_to(CATALOG_PATH))
        return f"{rel}/{variant.name or _DEFAULT_VARIANT}"

    def _variant_options(self) -> list[selector.SelectOptionDict]:
        """Picker options with disambiguated labels (hw_version shown only to break ties)."""
        labels = format_variant_labels(self._variants)
        return [
            selector.SelectOptionDict(value=self._variant_key(v), label=label)
            for v, label in zip(self._variants, labels, strict=True)
        ]

    def _find_variant(self, key: str) -> VariantInfo | None:
        """Resolve a composite variant key back to its VariantInfo."""
        return next((v for v in self._variants if self._variant_key(v) == key), None)

    def _current_variant(self) -> VariantInfo | None:
        """The VariantInfo currently selected, matched by directory and name."""
        return next(
            (
                v
                for v in self._variants
                if v.path.parent == self._selected_modem_dir and v.name == self._selected_variant
            ),
            None,
        )

    async def _apply_variant(self, variant: VariantInfo) -> None:
        """Record the chosen variant + its disambiguated label, and resolve restart creds off-loop."""
        self._selected_variant = variant.name
        self._selected_modem_dir = variant.path.parent
        labels = format_variant_labels(self._variants)
        self._selected_variant_label = next(lbl for v, lbl in zip(self._variants, labels, strict=True) if v is variant)
        await self._resolve_restart_credentials()

    # -----------------------------------------------------------------
    # Step 3: Connection details
    # -----------------------------------------------------------------

    async def async_step_connection(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Step 3 — Enter host and credentials."""
        if user_input is not None:
            # The retry form (connection_with_errors) may carry an inline variant
            # switch (#176). Apply it before validating; it is recorded as the
            # selected variant, not stored in the connection input.
            rerender = False
            raw_variant = user_input.pop("variant", None)
            if raw_variant is not None:
                switched = self._find_variant(raw_variant)
                current = self._current_variant()
                if switched is not None and (
                    current is None or self._variant_key(switched) != self._variant_key(current)
                ):
                    before_creds = self._shows_credentials()
                    await self._apply_variant(switched)
                    # If the new variant exposes credential fields the previous
                    # form lacked, re-render so the user can fill them before we
                    # validate. HA forms are static — the field set only updates
                    # on a re-render, not when the dropdown changes.
                    rerender = self._shows_credentials() and not before_creds
            # Merge with entity prefix and channel identity from Step 1b
            if self._connection_input:
                user_input[CONF_ENTITY_PREFIX] = self._connection_input.get(CONF_ENTITY_PREFIX, EntityPrefix.NONE)
                user_input[CONF_CHANNEL_IDENTITY] = self._connection_input.get(
                    CONF_CHANNEL_IDENTITY,
                    ChannelIdentity.NUMBER,
                )
            self._connection_input = user_input
            if rerender:
                return self._show_connection_form(include_variant=True)
            return await self.async_step_validate()

        return self._show_connection_form()

    def _show_connection_form(
        self,
        *,
        errors: dict[str, str] | None = None,
        include_variant: bool = False,
    ) -> config_entries.ConfigFlowResult:
        """Render the connection form, pre-filling anything already entered (#176)."""
        return self.async_show_form(
            step_id="connection",
            data_schema=self._build_connection_schema(defaults=self._connection_input, include_variant=include_variant),
            errors=errors,
            description_placeholders={"variant": self._selected_variant_label},
        )

    def _shows_credentials(self) -> bool:
        """True if the current variant's connection form shows username/password fields."""
        return self._get_selected_auth_strategy() != "none" or self._restart_needs_credentials

    def _build_connection_schema(
        self,
        defaults: dict[str, Any] | None = None,
        *,
        include_variant: bool = False,
    ) -> vol.Schema:
        """Build the connection form schema based on auth strategy."""
        summary = self._selected_summary
        d = defaults or {}
        default_host = d.get(CONF_HOST, summary.default_host if summary else "192.168.100.1")

        # Determine auth strategy from selected variant
        auth_strategy = self._get_selected_auth_strategy()

        fields: dict[Any, Any] = {}

        # On the retry form, offer an inline variant switch so the user can
        # correct a wrong pick without restarting (#176). Only when there's a
        # choice to make (more than one variant).
        if include_variant and len(self._variants) > 1:
            current = self._current_variant()
            variant_field = (
                vol.Required("variant", default=self._variant_key(current)) if current else vol.Required("variant")
            )
            fields[variant_field] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=self._variant_options(),
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )

        fields[vol.Required(CONF_HOST, default=default_host)] = str

        if auth_strategy != "none":
            fields[vol.Optional(CONF_USERNAME, default=d.get(CONF_USERNAME, ""))] = str
            fields[vol.Optional(CONF_PASSWORD, default=d.get(CONF_PASSWORD, ""))] = str
        elif self._restart_needs_credentials:
            # auth_strategy == "none", but the restart action needs per-action
            # auth — show credentials so the user can enable restart from the
            # HA UI. The flag is resolved off the event loop at variant
            # selection (the modem.yaml read must not block the loop).
            fields[vol.Optional(CONF_USERNAME, default=d.get(CONF_USERNAME, ""))] = str
            fields[vol.Optional(CONF_PASSWORD, default=d.get(CONF_PASSWORD, ""))] = str

        return vol.Schema(fields)

    async def _resolve_restart_credentials(self) -> None:
        """Resolve, off the event loop, whether the variant's restart needs per-action auth."""
        try:
            modem_dir = self._get_selected_modem_dir()
        except ValueError:
            self._restart_needs_credentials = False
            return
        self._restart_needs_credentials = await self.hass.async_add_executor_job(
            restart_requires_credentials,
            modem_dir,
            self._selected_variant,
        )

    def _get_selected_modem_dir(self) -> Path:
        """Return the modem directory for the currently selected variant.

        Uses ``_selected_modem_dir`` when set (Step 2 result), which may
        differ from ``summary.path`` when a sibling transport directory was
        chosen. Falls back to ``summary.path`` before Step 2 runs.
        """
        if self._selected_modem_dir is not None:
            return self._selected_modem_dir
        if self._selected_summary is not None:
            return self._selected_summary.path
        raise ValueError("No modem directory selected")

    def _get_selected_auth_strategy(self) -> str:
        """Return the auth strategy for the currently selected variant."""
        if self._selected_modem_dir is not None:
            # Match by both directory and name — needed when multiple
            # sibling directories contribute name=None variants.
            for v in self._variants:
                if v.path.parent == self._selected_modem_dir and v.name == self._selected_variant:
                    return v.auth_strategy
        elif self._selected_variant is not None:
            for v in self._variants:
                if v.name == self._selected_variant:
                    return v.auth_strategy
        elif self._variants:
            # Default variant (first in list)
            return self._variants[0].auth_strategy
        elif self._selected_summary:
            return self._selected_summary.auth_strategy
        return "none"

    # -----------------------------------------------------------------
    # Step 4: Validate (progress spinner)
    # -----------------------------------------------------------------

    async def async_step_validate(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Step 4 — Validate connectivity, auth, and parsing."""
        if not self._progress.task:
            if not self._connection_input or not self._selected_summary:
                return self.async_abort(reason="missing_input")

            self._progress.start(
                self.hass,
                validate_connection(
                    self.hass,
                    host=self._connection_input[CONF_HOST],
                    username=self._connection_input.get(CONF_USERNAME, ""),
                    password=self._connection_input.get(CONF_PASSWORD, ""),
                    modem_dir=self._get_selected_modem_dir(),
                    variant=self._selected_variant,
                ),
            )

        if self._progress.is_running():
            return self.async_show_progress(
                step_id="validate",
                progress_action="validate",
                progress_task=self._progress.task,
            )

        success = await self._progress.collect()
        if success:
            return self.async_show_progress_done(next_step_id="validate_success")
        return self.async_show_progress_done(next_step_id="connection_with_errors")

    async def async_step_validate_success(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Create config entry after successful validation."""
        if not self._connection_input or not self._selected_summary or not self._progress.result:
            return self.async_abort(reason="missing_input")

        summary = self._selected_summary
        validation = self._progress.result
        conn = self._connection_input
        self._progress.reset()

        # Deduplicate by entity_prefix — the prefix is by design the
        # per-entry disambiguator (it controls every entity_id this
        # integration creates), so two entries claiming the same
        # prefix would collide regardless of host or model. Any other
        # combination — different prefix at same host, same prefix
        # across host changes, etc. — is the user's intent and
        # should not be blocked.
        hostname, _ = parse_host_input(conn[CONF_HOST])
        entity_prefix = conn.get(CONF_ENTITY_PREFIX, EntityPrefix.NONE)
        await self.async_set_unique_id(str(entity_prefix))
        self._abort_if_unique_id_configured()

        display_name = build_model_display_name(summary)
        title = f"{summary.manufacturer} {summary.model} ({hostname})"

        modem_dir = str(self._get_selected_modem_dir().relative_to(CATALOG_PATH))

        entry_data: dict[str, Any] = {
            # User selections (Steps 1-2)
            CONF_MANUFACTURER: summary.manufacturer,
            CONF_MODEL: summary.model,
            CONF_VARIANT: self._selected_variant,
            CONF_USER_SELECTED_MODEM: display_name,
            CONF_ENTITY_PREFIX: conn.get(CONF_ENTITY_PREFIX, EntityPrefix.NONE),
            CONF_CHANNEL_IDENTITY: conn.get(CONF_CHANNEL_IDENTITY, ChannelIdentity.NUMBER),
            CONF_MODEM_DIR: modem_dir,
            # Connection (Step 3)
            CONF_HOST: hostname,
            CONF_USERNAME: conn.get(CONF_USERNAME, ""),
            CONF_PASSWORD: conn.get(CONF_PASSWORD, ""),
            # Derived during validation (Step 4)
            CONF_PROTOCOL: validation["protocol"],
            CONF_LEGACY_SSL: validation.get("legacy_ssl", False),
            CONF_SUPPORTS_ICMP: validation["supports_icmp"],
            CONF_SUPPORTS_HEAD: validation["supports_head"],
            CONF_CREDENTIAL_ENCODING: validation.get("credential_encoding", "plain"),
            CONF_CREDENTIAL_FIELD: validation.get("credential_field", ""),
            # Polling defaults — health interval adapts to probe capability
            CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
            CONF_HEALTH_CHECK_INTERVAL: default_health_check_interval(
                validation["supports_icmp"],
                validation["supports_head"],
            ),
            # Marks fresh setups as eligible for the one-time channel-bond
            # onboarding notification. Never mutated after create so it
            # doesn't trip the update listener. Upgraded entries lack the
            # key entirely and skip onboarding (silent init instead).
            CONF_CHANNEL_ONBOARDING_ELIGIBLE: True,
        }

        return self.async_create_entry(title=title, data=entry_data)

    async def async_step_connection_with_errors(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Re-show the connection step with the validation error.

        For multi-variant modems the form includes an inline variant switch so
        the user can correct a wrong pick without restarting (#176). The error
        banner carries the full, translated reason (strings.json config.error).
        """
        errors = {"base": self._progress.error_key}
        self._progress.reset()
        return self._show_connection_form(errors=errors, include_variant=True)

    # -----------------------------------------------------------------
    # Reauth flow
    # -----------------------------------------------------------------

    async def async_step_reauth(
        self,
        entry_data: dict[str, Any],
    ) -> config_entries.ConfigFlowResult:
        """Handle reauth triggered by circuit breaker."""
        # Store the entry data so reauth_confirm can access it
        self._connection_input = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Show connection form for reauthentication."""
        entry_id = self.context.get("entry_id")
        if entry_id is None:
            return self.async_abort(reason="reauth_failed")
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            return self.async_abort(reason="reauth_failed")

        if user_input is not None:
            # Load summary to get modem_dir
            summaries = await load_modem_catalog(self.hass)
            mfr = entry.data[CONF_MANUFACTURER]
            model = entry.data[CONF_MODEL]
            summary = next(
                (s for s in summaries if s.manufacturer == mfr and s.model == model),
                None,
            )
            if summary is None:
                return self.async_abort(reason="reauth_failed")

            modem_dir = CATALOG_PATH / entry.data[CONF_MODEM_DIR] if CONF_MODEM_DIR in entry.data else summary.path
            try:
                result = await validate_connection(
                    self.hass,
                    host=user_input[CONF_HOST],
                    username=user_input.get(CONF_USERNAME, ""),
                    password=user_input.get(CONF_PASSWORD, ""),
                    modem_dir=modem_dir,
                    variant=entry.data.get(CONF_VARIANT),
                )
            except (ConnectionError, PermissionError, RuntimeError) as exc:
                _LOGGER.error("Reauth validation failed: %s", exc)
                # Reauth is where a session-rejection lands for an already
                # configured modem, so it must show the classified key —
                # "check your password" is wrong when login succeeded (#120).
                return self.async_show_form(
                    step_id="reauth_confirm",
                    data_schema=_build_reauth_schema(entry.data),
                    errors={"base": _validation_error_key(exc)},
                )

            # Update entry with new credentials + validation results
            hostname, _ = parse_host_input(user_input[CONF_HOST])
            updated = {
                **entry.data,
                CONF_HOST: hostname,
                CONF_USERNAME: user_input.get(CONF_USERNAME, ""),
                CONF_PASSWORD: user_input.get(CONF_PASSWORD, ""),
                CONF_PROTOCOL: result["protocol"],
                CONF_LEGACY_SSL: result.get("legacy_ssl", False),
                CONF_SUPPORTS_ICMP: result["supports_icmp"],
                CONF_SUPPORTS_HEAD: result["supports_head"],
                CONF_CREDENTIAL_ENCODING: result.get("credential_encoding", "plain"),
                CONF_CREDENTIAL_FIELD: result.get("credential_field", ""),
            }
            self.hass.config_entries.async_update_entry(entry, data=updated)
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_build_reauth_schema(entry.data),
        )


# =============================================================================
# Duration helpers
# =============================================================================


def _seconds_to_duration(seconds: int) -> dict[str, int]:
    """Convert seconds to a duration dict for DurationSelector."""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return {"hours": hours, "minutes": minutes, "seconds": secs}


def _duration_to_seconds(duration: dict[str, int] | int) -> int:
    """Convert a DurationSelector dict to seconds.

    Accepts raw int for backward compatibility with existing entries.
    """
    if isinstance(duration, int):
        return duration
    return duration.get("hours", 0) * 3600 + duration.get("minutes", 0) * 60 + duration.get("seconds", 0)


# =============================================================================
# Options flow
# =============================================================================


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle reconfiguration of connection params and intervals."""

    def __init__(self) -> None:
        """Initialize options flow state."""
        self._progress = _ValidationProgress()
        self._user_input: dict[str, Any] | None = None

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Show the options form."""
        if user_input is not None:
            # Preserve password if user left it blank
            if not user_input.get(CONF_PASSWORD):
                user_input[CONF_PASSWORD] = self.config_entry.data.get(CONF_PASSWORD, "")
            self._user_input = user_input
            return await self.async_step_options_validate()

        entry = self.config_entry
        data = entry.data
        options = entry.options

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST,
                        default=data.get(CONF_HOST, "192.168.100.1"),
                    ): str,
                    vol.Optional(
                        CONF_USERNAME,
                        default=data.get(CONF_USERNAME, ""),
                    ): str,
                    vol.Optional(CONF_PASSWORD, default=""): str,
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=_seconds_to_duration(
                            options.get(
                                CONF_SCAN_INTERVAL,
                                data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                            )
                        ),
                    ): selector.DurationSelector(selector.DurationSelectorConfig(enable_day=False)),
                    vol.Required(
                        CONF_HEALTH_CHECK_INTERVAL,
                        default=_seconds_to_duration(
                            options.get(
                                CONF_HEALTH_CHECK_INTERVAL,
                                data.get(
                                    CONF_HEALTH_CHECK_INTERVAL,
                                    DEFAULT_HEALTH_CHECK_INTERVAL,
                                ),
                            )
                        ),
                    ): selector.DurationSelector(selector.DurationSelectorConfig(enable_day=False)),
                }
            ),
        )

    async def async_step_options_validate(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Validate options changes with progress spinner."""
        if not self._progress.task:
            if not self._user_input:
                return self.async_abort(reason="missing_input")

            entry = self.config_entry
            summaries = await load_modem_catalog(self.hass)
            mfr = entry.data[CONF_MANUFACTURER]
            model = entry.data[CONF_MODEL]
            summary = next(
                (s for s in summaries if s.manufacturer == mfr and s.model == model),
                None,
            )
            if summary is None:
                return self.async_abort(reason="unknown_model")

            self._progress.start(
                self.hass,
                self._validate_with_cooloff(
                    entry,
                    host=self._user_input[CONF_HOST],
                    username=self._user_input.get(CONF_USERNAME, ""),
                    password=self._user_input.get(CONF_PASSWORD, ""),
                    modem_dir=CATALOG_PATH / entry.data[CONF_MODEM_DIR],
                    variant=entry.data.get(CONF_VARIANT),
                ),
            )

        if self._progress.is_running():
            return self.async_show_progress(
                step_id="options_validate",
                progress_action="validate",
                progress_task=self._progress.task,
            )

        success = await self._progress.collect()
        if success:
            return self.async_show_progress_done(next_step_id="options_success")
        return self.async_show_progress_done(next_step_id="options_with_errors")

    async def _validate_with_cooloff(
        self,
        entry: config_entries.ConfigEntry,
        *,
        host: str,
        username: str,
        password: str,
        modem_dir: Any,
        variant: str | None,
    ) -> dict[str, Any]:
        """Wait for the modem to be quiet, then run validation.

        Some modems (e.g. Motorola MB7621) only allow one logged-in
        session at a time. Overlapping auth attempts — e.g. options-
        flow validation racing with a regular poll or a recent manual
        refresh — cause the modem to silently invalidate one session
        and serve subsequent requests as the login page, surfacing
        as a spurious "auth failed" error to the user.

        Read collection state from the HealthMonitor (notified by the
        orchestrator) and wait until no collection is active and at
        least ``_COOLOFF_QUIET_SECONDS`` has elapsed since the last
        successful collection.
        """
        await self._wait_for_quiet_modem(entry)
        return await validate_connection(
            self.hass,
            host=host,
            username=username,
            password=password,
            modem_dir=modem_dir,
            variant=variant,
        )

    async def _wait_for_quiet_modem(
        self,
        entry: config_entries.ConfigEntry,
    ) -> None:
        """Block until the modem is quiet enough for a fresh auth."""
        runtime = getattr(entry, "runtime_data", None)
        health = getattr(runtime, "health_monitor", None) if runtime else None
        if health is None:
            return

        deadline = time.monotonic() + _COOLOFF_TIMEOUT_SECONDS
        waited = False
        while time.monotonic() < deadline:
            if health.collection_active:
                await asyncio.sleep(_COOLOFF_POLL_SECONDS)
                waited = True
                continue

            last_success = health.last_collection_success_at
            if last_success is None:
                break

            elapsed = time.monotonic() - last_success
            if elapsed >= _COOLOFF_QUIET_SECONDS:
                break

            await asyncio.sleep(_COOLOFF_QUIET_SECONDS - elapsed)
            waited = True

        if waited:
            _LOGGER.debug("Options-flow validation: waited for modem to settle")

    async def async_step_options_success(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Apply validated options changes."""
        if not self._user_input or not self._progress.result:
            return self.async_abort(reason="missing_input")

        inp = self._user_input
        validation = self._progress.result
        self._progress.reset()

        hostname, _ = parse_host_input(inp[CONF_HOST])
        entry = self.config_entry

        # Update entry.data with new connection info
        updated_data = {
            **entry.data,
            CONF_HOST: hostname,
            CONF_USERNAME: inp.get(CONF_USERNAME, ""),
            CONF_PASSWORD: inp.get(CONF_PASSWORD, ""),
            CONF_PROTOCOL: validation["protocol"],
            CONF_LEGACY_SSL: validation.get("legacy_ssl", False),
            CONF_SUPPORTS_ICMP: validation["supports_icmp"],
            CONF_SUPPORTS_HEAD: validation["supports_head"],
            CONF_CREDENTIAL_ENCODING: validation.get("credential_encoding", "plain"),
            CONF_CREDENTIAL_FIELD: validation.get("credential_field", ""),
        }

        mfr = entry.data[CONF_MANUFACTURER]
        model = entry.data[CONF_MODEL]
        title = f"{mfr} {model} ({hostname})"

        self.hass.config_entries.async_update_entry(entry, title=title, data=updated_data)

        # Store intervals in options as int seconds (triggers reload via update listener)
        scan = _duration_to_seconds(inp.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        health = _duration_to_seconds(inp.get(CONF_HEALTH_CHECK_INTERVAL, DEFAULT_HEALTH_CHECK_INTERVAL))
        return self.async_create_entry(
            title="",
            data={
                CONF_SCAN_INTERVAL: scan,
                CONF_HEALTH_CHECK_INTERVAL: health,
            },
        )

    async def async_step_options_with_errors(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Re-show options form with error message."""
        errors = {"base": self._progress.error_key}
        saved = self._user_input or {}
        self._progress.reset()
        self._user_input = None

        entry = self.config_entry
        data = entry.data

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST,
                        default=saved.get(CONF_HOST, data.get(CONF_HOST, "")),
                    ): str,
                    vol.Optional(
                        CONF_USERNAME,
                        default=saved.get(CONF_USERNAME, data.get(CONF_USERNAME, "")),
                    ): str,
                    vol.Optional(CONF_PASSWORD, default=""): str,
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=_seconds_to_duration(
                            _duration_to_seconds(saved.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
                        ),
                    ): selector.DurationSelector(selector.DurationSelectorConfig(enable_day=False)),
                    vol.Required(
                        CONF_HEALTH_CHECK_INTERVAL,
                        default=_seconds_to_duration(
                            _duration_to_seconds(saved.get(CONF_HEALTH_CHECK_INTERVAL, DEFAULT_HEALTH_CHECK_INTERVAL))
                        ),
                    ): selector.DurationSelector(selector.DurationSelectorConfig(enable_day=False)),
                }
            ),
            errors=errors,
        )


# =============================================================================
# Shared helpers
# =============================================================================


def _build_prefix_options(hass: HomeAssistant) -> list[selector.SelectOptionDict]:
    """Build entity prefix dropdown based on existing entries.

    ``none`` (Default) is only available when no other entry already uses it.
    """
    existing = hass.config_entries.async_entries(DOMAIN)
    none_in_use = any(e.data.get(CONF_ENTITY_PREFIX) == EntityPrefix.NONE for e in existing)

    if none_in_use or existing:
        return [
            selector.SelectOptionDict(value=EntityPrefix.MODEL, label="Model"),
            selector.SelectOptionDict(value=EntityPrefix.IP, label="IP Address"),
        ]

    return [
        selector.SelectOptionDict(value=EntityPrefix.NONE, label="Default"),
        selector.SelectOptionDict(value=EntityPrefix.MODEL, label="Model"),
        selector.SelectOptionDict(value=EntityPrefix.IP, label="IP Address"),
    ]


def _build_reauth_schema(
    data: dict[str, Any] | Mapping[str, Any],
) -> vol.Schema:
    """Build the reauth form schema (host + credentials)."""
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=data.get(CONF_HOST, "")): str,
            vol.Optional(CONF_USERNAME, default=data.get(CONF_USERNAME, "")): str,
            vol.Optional(CONF_PASSWORD, default=""): str,
        }
    )
