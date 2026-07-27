"""Config flow helpers — I/O and data transformation for the setup wizard.

Wraps Core APIs (catalog browsing, connectivity probing, data
collection) in ``hass.async_add_executor_job()`` and provides
HA-specific helpers for building form schemas and classifying errors.

All blocking I/O runs in an executor thread.  This module never
blocks the HA event loop.
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from solentlabs.cable_modem_monitor_catalog import CATALOG_PATH
from solentlabs.cable_modem_monitor_core.auth.base import AuthFailureMode
from solentlabs.cable_modem_monitor_core.auth.factory import create_auth_manager
from solentlabs.cable_modem_monitor_core.catalog_manager import (
    ModemSummary,
    VariantInfo,
    list_modems,
    list_variants,
)
from solentlabs.cable_modem_monitor_core.config_loader import (
    load_modem_config,
    load_parser_config,
)
from solentlabs.cable_modem_monitor_core.connectivity import (
    ConnectivityResult,
    detect_protocol,
    test_http_head,
    test_icmp,
)
from solentlabs.cable_modem_monitor_core.models.modem_config import ModemConfig
from solentlabs.cable_modem_monitor_core.models.modem_config.auth import (
    get_strategy_display_labels,
)
from solentlabs.cable_modem_monitor_core.orchestration import (
    create_collector,
)
from solentlabs.cable_modem_monitor_core.orchestration.models import ModemResult
from solentlabs.cable_modem_monitor_core.orchestration.signals import (
    CollectorSignal,
)
from solentlabs.cable_modem_monitor_core.post_processor import (
    load_post_processor,
)

from .const import (
    DEFAULT_HEALTH_CHECK_INTERVAL,
)
from .lib.host_validation import parse_host_input

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(f"{__package__}.config_flow")


# ---------------------------------------------------------------------------
# Auth strategy display labels — derived from auth model ClassVars
# ---------------------------------------------------------------------------

AUTH_STRATEGY_LABELS: dict[str, str] = get_strategy_display_labels()


def format_variant_label(variant: VariantInfo) -> str:
    """Build a human-readable dropdown label: auth strategy + variant name qualifier + ``*`` if unconfirmed."""
    # Hardware version is deliberately not shown: it does not determine the
    # auth contract and misled contributors into picking the wrong variant
    # (#124). The variant name is the meaningful qualifier.
    label = AUTH_STRATEGY_LABELS.get(variant.auth_strategy, variant.auth_strategy)
    if variant.name:
        label = f"{label} ({variant.name})"
    if variant.status != "confirmed":
        label = f"{label} *"
    return label


def format_variant_labels(variants: list[VariantInfo]) -> list[str]:
    """Picker labels for a variant set, adding hw_version only to break ties.

    The base label hides hw_version (#124). But some modems have variants that
    differ *only* by hardware revision (e.g. the Arris S33 generations), where
    dropping it would make two options identical. For those — and only those —
    the hw_version is appended as a tiebreaker.
    """
    base = [format_variant_label(v) for v in variants]
    collisions = {label for label, count in Counter(base).items() if count > 1}
    out: list[str] = []
    for variant, label in zip(variants, base, strict=True):
        if label in collisions and variant.hw_version:
            # Insert the hw_version qualifier before any trailing "*" marker.
            if label.endswith(" *"):
                out.append(f"{label[:-2]} ({variant.hw_version}) *")
            else:
                out.append(f"{label} ({variant.hw_version})")
        else:
            out.append(label)
    return out


# ---------------------------------------------------------------------------
# Catalog browsing (async wrappers around Core sync API)
# ---------------------------------------------------------------------------


async def load_modem_catalog(hass: HomeAssistant) -> list[ModemSummary]:
    """Load all modem summaries from the catalog package (runs in executor)."""
    return await hass.async_add_executor_job(list_modems, CATALOG_PATH)


async def load_variant_list(
    hass: HomeAssistant,
    modem_dir: Path,
    sibling_dirs: list[Path] | None = None,
) -> list[VariantInfo]:
    """Load variant info for a modem directory, including sibling transports (runs in executor)."""
    return await hass.async_add_executor_job(list_variants, modem_dir, sibling_dirs)


def _normalize_manufacturer(name: str) -> str:
    """Normalize a manufacturer or brand name for display."""
    # modem.yaml stores names as styled in the wild (ARRIS, CommScope, SURFboard);
    # normalization is presentation-only — raw catalog values are preserved.
    # Deliberate mixed case passes through untouched: .title() would mangle
    # "CommScope" into "Commscope". Only single-case names get title-cased.
    if name.islower() or name.isupper():
        return name.title()
    return name


def get_manufacturers(summaries: list[ModemSummary]) -> list[str]:
    """Sorted manufacturer ∪ brand names for the Step 1a dropdown (ARCHITECTURE_DECISIONS § Config Flow)."""
    # Case variants of one name collapse to a single entry; the
    # lexicographically smallest normalized form wins so the pick is
    # deterministic ("SURFboard" over "Surfboard").
    buckets: dict[str, str] = {}
    for s in summaries:
        for name in (s.manufacturer, *(s.brands or [])):
            normalized = _normalize_manufacturer(name)
            key = normalized.lower()
            if key not in buckets or normalized < buckets[key]:
                buckets[key] = normalized
    return sorted(buckets.values())


def filter_by_manufacturer(
    summaries: list[ModemSummary],
    manufacturer: str,
) -> list[ModemSummary]:
    """Filter summaries to one dropdown bucket — matches manufacturer or any brand (case-insensitive)."""
    target = manufacturer.lower()
    return [
        s for s in summaries if s.manufacturer.lower() == target or any(b.lower() == target for b in s.brands or [])
    ]


def build_model_display_name(summary: ModemSummary, bucket: str | None = None) -> str:
    """Build the bucket-contextual ``{Lead} {Model} (alternates) *`` label (see CONFIG_FLOW_SPEC § Step 1)."""
    mfr = _normalize_manufacturer(summary.manufacturer)
    brands = summary.brands or []

    # The lead always matches the filter the user chose: browsing a brand
    # bucket leads with that brand, and the manufacturer-composed name
    # moves into the parenthetical (ARCHITECTURE_DECISIONS § Config Flow).
    lead_brand = None
    if bucket is not None and bucket.lower() != summary.manufacturer.lower():
        lead_brand = next((b for b in brands if b.lower() == bucket.lower()), None)

    if lead_brand is not None:
        parts = [_normalize_manufacturer(lead_brand), summary.model]
        # Aliases (sticker codes, rebadges) anchor the device; the
        # manufacturer-composed name is only worth a slot when no alias
        # does that job (e.g. the G54, whose only alternate is the
        # CommScope listing name).
        alternates = [*(summary.model_aliases or [])]
        if not alternates:
            alternates.append(f"{mfr} {summary.model}")
        alternates.extend(b for b in brands if b != lead_brand)
    else:
        parts = [mfr, summary.model]
        # Parenthetical carries alternate user-facing names only — never
        # firmware-internal codes (ARCHITECTURE_DECISIONS § Config Flow).
        alternates = [*(summary.model_aliases or []), *brands]

    if alternates:
        parts.append(f"({', '.join(alternates)})")

    if summary.status != "confirmed":
        parts.append("*")

    return " ".join(parts)


def build_model_options(
    summaries: list[ModemSummary],
    bucket: str | None,
) -> list[tuple[str, str]]:
    """Step 1b dropdown ``(value, label)`` pairs — the All view lists one row per user-facing name."""
    if bucket is not None:
        return [
            (f"{s.manufacturer}/{s.model}", build_model_display_name(s, bucket=bucket))
            for s in filter_by_manufacturer(summaries, bucket)
        ]

    # All view: a modem branded under multiple names gets one row per
    # name, each with that name leading, so an alphabetical scan finds
    # it under any name the user knows. Brand rows suffix the value
    # with "|{brand}" to stay unique; the handler strips the suffix.
    rows: list[tuple[str, str]] = []
    for s in summaries:
        rows.append((f"{s.manufacturer}/{s.model}", build_model_display_name(s)))
        for brand in s.brands or []:
            if brand.lower() != s.manufacturer.lower():
                rows.append((f"{s.manufacturer}/{s.model}|{brand}", build_model_display_name(s, bucket=brand)))
    return sorted(rows, key=lambda row: row[1].lower())


def restart_requires_credentials(modem_dir: Path, variant: str | None) -> bool:
    """True when restart needs per-action auth despite top-level ``auth.strategy: none``."""
    from solentlabs.cable_modem_monitor_core.models.modem_config.actions import (
        HttpAction,
    )

    try:
        modem_yaml = (modem_dir / f"modem-{variant}.yaml") if variant else (modem_dir / "modem.yaml")
        modem_config = load_modem_config(modem_yaml)
    except Exception:  # noqa: BLE001
        return False

    if modem_config.actions is None or modem_config.actions.restart is None:
        return False

    return isinstance(modem_config.actions.restart, HttpAction) and modem_config.actions.restart.action_auth is not None


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

# Maps collector signals to strings.json error keys.  These keys are
# looked up by HA's frontend in strings.json / translations/*.json.

_SIGNAL_ERROR_MAP: dict[CollectorSignal, str] = {
    CollectorSignal.CONNECTIVITY: "cannot_connect",
    CollectorSignal.AUTH_FAILED: "invalid_auth",
    CollectorSignal.AUTH_LOCKOUT: "invalid_auth",
    CollectorSignal.LOAD_ERROR: "cannot_connect",
    CollectorSignal.LOAD_AUTH: "invalid_auth",
    CollectorSignal.LOAD_INTEGRITY: "invalid_auth",
    CollectorSignal.PARSE_ERROR: "parse_failed",
}

# Signals raised after authenticate() reported success. Their entries
# above are the conservative answer; _post_login_error_key refines them
# when a modem_config is available to ask.
_POST_LOGIN_SIGNALS = frozenset({CollectorSignal.LOAD_AUTH, CollectorSignal.LOAD_INTEGRITY})


def _post_login_error_key(modem_config: Any) -> str:
    """Pick the error key for a post-login refusal, per auth strategy."""
    # "Login succeeded, so the password is fine" is only true if the
    # strategy verified it. basic auth never validates, and plain form
    # auth accepts any response under HTTP 400, so for those the refusal
    # is usually the bad password surfacing late (#120 regression).
    try:
        mode = create_auth_manager(modem_config).auth_failure_mode()
    except Exception:  # noqa: BLE001 - never let message selection break the flow
        return "invalid_auth"
    return "session_rejected" if mode is AuthFailureMode.SESSION_REJECTED else "invalid_auth"


def classify_error(
    error: str | None,
    signal: CollectorSignal | None = None,
    modem_config: Any = None,
) -> str:
    """Map a CollectorSignal to a strings.json error key for HA form display."""
    if signal is None:
        return "unknown"
    if signal in _POST_LOGIN_SIGNALS and modem_config is not None:
        return _post_login_error_key(modem_config)
    return _SIGNAL_ERROR_MAP.get(signal, "unknown")


# ---------------------------------------------------------------------------
# Probe detection (shared by validation pipeline and Reset Entities)
# ---------------------------------------------------------------------------


def default_health_check_interval(supports_icmp: bool, supports_head: bool) -> int:
    """Return the single default health-check interval."""
    # Both args accepted for API compatibility; per-capability cadence
    # differentiation was removed when HEAD probes became lightweight.
    return DEFAULT_HEALTH_CHECK_INTERVAL


def detect_probes(
    host: str,
    base_url: str,
    modem_config: ModemConfig,
    *,
    legacy_ssl: bool = False,
) -> dict[str, bool]:
    """Test ICMP and HTTP HEAD, using modem.yaml ``health.supports_head`` as a ceiling."""
    health_cfg = modem_config.health
    supports_icmp = test_icmp(host)
    if health_cfg and not health_cfg.supports_head:
        supports_head = False
    else:
        supports_head = test_http_head(base_url, legacy_ssl=legacy_ssl)
    return {"supports_icmp": supports_icmp, "supports_head": supports_head}


# ---------------------------------------------------------------------------
# Validation pipeline
# ---------------------------------------------------------------------------


def _detect_and_inject_form_nonce_encoding(
    base_url: str,
    modem_config: ModemConfig,
    *,
    legacy_ssl: bool = False,
) -> tuple[str, str]:
    """Pre-fetch the login page for form_nonce auth and detect credential encoding; no-op for other strategies."""
    # Raises ConnectionError on connectivity failure — caller must surface it
    # rather than proceeding to a doomed auth attempt.
    # Falls back to ("plain", "") for non-connectivity errors (malformed HTML, etc.).
    from solentlabs.cable_modem_monitor_core.models.modem_config.auth import (
        FormNonceAuth,
    )

    if not isinstance(modem_config.auth, FormNonceAuth):
        return ("plain", "")

    import requests as req_lib
    from solentlabs.cable_modem_monitor_core.auth.form_nonce import (
        _analyze_login_form,
    )
    from solentlabs.cable_modem_monitor_core.connectivity import create_session

    auth = modem_config.auth
    login_url = f"{base_url}{auth.action}"

    try:
        session = create_session(legacy_ssl=legacy_ssl)
        response = session.get(login_url, timeout=10)
    except (req_lib.ConnectionError, req_lib.Timeout) as exc:
        # Modem unreachable or unresponsive — no point proceeding to validation
        _LOGGER.info("Login page unreachable during validation (%s): %s", login_url, exc)
        raise ConnectionError(str(exc)) from exc
    except Exception as exc:
        _LOGGER.debug(
            "Login page pre-fetch failed during validation, using plain encoding: %s",
            exc,
        )
        return ("plain", "")

    detection = _analyze_login_form(
        response.text,
        auth.username_field,
        auth.nonce_field,
    )

    if detection.encoding != "plain":
        _LOGGER.info(
            "Credential encoding detected: %s (field=%r)",
            detection.encoding,
            detection.credential_field,
        )

    # Inject into config so the collector uses the correct encoding
    auth.credential_encoding = detection.encoding
    auth.credential_field = detection.credential_field

    return (detection.encoding, detection.credential_field)


def _raise_validation_failure(
    result: ModemResult,
    auth_signals: tuple[CollectorSignal, ...],
    modem_config: Any = None,
) -> None:
    """Raise ``PermissionError`` for auth signals or ``RuntimeError`` for collection signals."""
    # Wire-level detail is already at WARNING from the collector; ERROR here
    # carries only the signal/key summary for the HA form UI.
    _LOGGER.error("Validation failed: signal=%s, error=%s", result.signal, result.error)
    error_key = classify_error(result.error, result.signal, modem_config)
    if result.signal in auth_signals:
        raise PermissionError(f"auth_error:{error_key}:{result.error}")
    raise RuntimeError(f"collection_error:{error_key}:{result.error}")


def _attempt_validation(
    *,
    modem_config: ModemConfig,
    parser_config: Any,
    post_processor: Any,
    base_url: str,
    username: str,
    password: str,
    legacy_ssl: bool,
) -> ModemResult:
    """Run one validation attempt, always releasing the server-side session."""
    # Shared by initial setup, reauth, and options-flow re-validation —
    # no per-flow variation; the collector's sanitized WARNING covers all paths.
    collector = create_collector(
        modem_config=modem_config,
        parser_config=parser_config,
        post_processor=post_processor,
        base_url=base_url,
        username=username,
        password=password,
        legacy_ssl=legacy_ssl,
    )
    try:
        return collector.execute()
    finally:
        # execute() only reaches its logout phase on success, so a failed
        # attempt would strand the session on single-session firmware and
        # collide with the user's next try.
        collector.close()


def _run_validation(
    host: str,
    protocol: str | None,
    username: str,
    password: str,
    modem_dir: Path,
    variant: str | None,
) -> dict[str, Any]:
    """Full validation pipeline (sync — runs in executor): protocol detect → load config → collect → probe discovery."""
    # -- Step 1: Protocol detection -------------------------------------------
    # Probe both ports; let detect_protocol's TLS handshake observe
    # whether the modem speaks legacy TLS. User-specified protocols
    # restrict the probe to that transport but still drive the same
    # legacy_ssl observation when HTTPS is chosen.
    probe_input = f"{protocol}://{host}" if protocol else host
    conn: ConnectivityResult = detect_protocol(probe_input)
    if not conn.success:
        raise ConnectionError(conn.error or f"Cannot connect to {host}")
    base_url = conn.working_url or f"http://{host}"
    protocol = conn.protocol or "http"
    legacy_ssl = conn.legacy_ssl
    _LOGGER.info("Protocol detected: %s (legacy_ssl=%s)", protocol, legacy_ssl)

    # -- Step 2: Load config from catalog -------------------------------------
    modem_yaml = (modem_dir / f"modem-{variant}.yaml") if variant else (modem_dir / "modem.yaml")
    parser_yaml = modem_dir / "parser.yaml"
    parser_py = modem_dir / "parser.py"

    modem_config = load_modem_config(modem_yaml)
    parser_config = load_parser_config(parser_yaml) if parser_yaml.exists() else None
    post_processor = load_post_processor(parser_py) if parser_py.exists() else None

    # -- Step 2a: Detect form_nonce credential encoding ----------------------
    credential_encoding, credential_field = _detect_and_inject_form_nonce_encoding(
        base_url, modem_config, legacy_ssl=legacy_ssl
    )

    # -- Step 3: Test data collection -----------------------------------------
    # Single attempt against the chosen transport. UC-86: if the modem
    # rejects credentials, surface the real error immediately — never
    # retry, since retries on single-session firmware would collide
    # with our own previous attempt and obscure the original failure.
    auth_signals = (CollectorSignal.AUTH_FAILED, CollectorSignal.AUTH_LOCKOUT, CollectorSignal.LOAD_AUTH)
    result = _attempt_validation(
        modem_config=modem_config,
        parser_config=parser_config,
        post_processor=post_processor,
        base_url=base_url,
        username=username,
        password=password,
        legacy_ssl=legacy_ssl,
    )

    if not result.success:
        _raise_validation_failure(result, auth_signals, modem_config)

    _LOGGER.info("Validation succeeded: %d data keys", len(result.modem_data or {}))

    # -- Step 4: Health-probe discovery ---------------------------------------
    probes = detect_probes(host, base_url, modem_config, legacy_ssl=legacy_ssl)
    supports_icmp = probes["supports_icmp"]
    supports_head = probes["supports_head"]

    # -- Step 5: Build result dict --------------------------------------------
    return {
        "protocol": protocol,
        "legacy_ssl": legacy_ssl,
        "supports_icmp": supports_icmp,
        "supports_head": supports_head,
        "credential_encoding": credential_encoding,
        "credential_field": credential_field,
    }


async def validate_connection(
    hass: HomeAssistant,
    *,
    host: str,
    username: str,
    password: str,
    modem_dir: Path,
    variant: str | None,
) -> dict[str, Any]:
    """Async wrapper around ``_run_validation`` — decomposes the raw host input and runs in an executor thread."""
    hostname, user_protocol = parse_host_input(host)

    return await hass.async_add_executor_job(
        _run_validation,
        hostname,
        user_protocol,
        username,
        password,
        modem_dir,
        variant,
    )
