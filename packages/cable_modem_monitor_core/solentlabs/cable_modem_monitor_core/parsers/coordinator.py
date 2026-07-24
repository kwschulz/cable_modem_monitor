"""ModemParserCoordinator — orchestrates parser.yaml-driven extraction.

Reads parser.yaml config, dispatches to registered parsers per section,
applies parser.py post-processing hooks, enriches system_info with
derived fields (channel counts, aggregate sums), and assembles ModemData.
Surfaces per-resource ParseDiagnostics alongside ModemData so the
collector can detect stub-page responses (UC-19a).

Parser registries (type-to-callable dispatch tables) live in
``registries.py``.

See PARSING_SPEC.md ModemParserCoordinator and Aggregate sections.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, TypeVar

from ..models.parser_config.common import ChannelTypeDerive
from ..models.parser_config.config import ParserConfig
from ..spec_conformance import derive_channel_type_from_modulation
from .diagnostics import AnchorCount, ParseDiagnostics
from .registries import CHANNEL_PARSERS, SYSINFO_PARSERS

_T = TypeVar("_T")
_logger = logging.getLogger(__name__)

# Section names that contain channel data (list[dict] output).
_CHANNEL_SECTIONS = ("downstream", "upstream")

# Hook method names by section.
_HOOK_NAMES = {
    "downstream": "parse_downstream",
    "upstream": "parse_upstream",
    "system_info": "parse_system_info",
}


class ModemParserCoordinator:
    """Factory and orchestrator for parser.yaml-driven extraction.

    Creates BaseParser instances from parser.yaml config, runs them per
    section, applies merge_by for companion tables, invokes parser.py
    post-processing hooks, and assembles the final ModemData dict.

    Args:
        config: Validated ParserConfig from parser.yaml.
        post_processor: Optional parser.py post-processor instance.
            Duck-typed: checked for ``parse_downstream``,
            ``parse_upstream``, ``parse_system_info`` methods via hasattr.
    """

    def __init__(
        self,
        config: ParserConfig,
        post_processor: Any = None,
    ) -> None:
        self._config = config
        self._post_processor = post_processor

    def parse(self, resources: dict[str, Any]) -> tuple[dict[str, Any], ParseDiagnostics]:
        """Run the full extraction pipeline and assemble ModemData.

        Sequence: extract channels → extract system_info → apply hooks
        → enrich derived fields (channel counts, aggregate sums).

        Args:
            resources: Resource dict keyed by URL path. Values are
                format-dependent (BeautifulSoup for HTML, dict for JSON).

        Returns:
            Tuple of (ModemData, ParseDiagnostics). ModemData has
            downstream, upstream, and optional system_info, with
            derived fields merged into system_info. ParseDiagnostics
            reports per-resource expected vs. fulfilled anchor counts
            (see PARSING_SPEC.md § Parser Diagnostics).
        """
        result: dict[str, Any] = {}
        per_resource: dict[str, AnchorCount] = defaultdict(AnchorCount)

        for section_name in _CHANNEL_SECTIONS:
            channels, count, resource = self._extract_channel_section(section_name, resources)
            result[section_name] = channels
            if resource is not None:
                per_resource[resource] = per_resource[resource] + count

        # Apply direction-aware channel_type derivation for sections
        # configured with ``channel_type: { derive: from_modulation }``.
        # Format parsers no-op on ChannelTypeDerive because they don't
        # know whether they're processing the DS or US section; the
        # coordinator does, so derivation lives here.
        for section_name in _CHANNEL_SECTIONS:
            section = getattr(self._config, section_name, None)
            if _section_uses_channel_type_derive(section):
                _apply_derive_channel_type(result[section_name], section_name)

        # Null metrics on unlocked channels before aggregation.
        # See CHANNEL_IDENTIFICATION_SPEC.md §6.
        for section_name in _CHANNEL_SECTIONS:
            _null_unlocked_channels(result[section_name])

        # Strip fields that don't belong on OFDM/OFDMA channels.
        # See PARSING_SPEC.md § Output Contract.
        for section_name in _CHANNEL_SECTIONS:
            _strip_ofdm_fields(result[section_name])

        system_info, sysinfo_counts, sysinfo_failed = self._extract_system_info(resources)
        if system_info:
            result["system_info"] = system_info
        for resource, count in sysinfo_counts.items():
            per_resource[resource] = per_resource[resource] + count

        self._enrich_derived_fields(result)

        # Field outcomes — section-level, post-merge and post-hook.
        # Underscore-prefixed fields are internal hook intermediates
        # and excluded from the accounting.
        # See PARSING_SPEC.md § Field Outcomes (system_info).
        produced = set(result.get("system_info", {}))
        failed = {
            name: raw for name, raw in sysinfo_failed.items() if name not in produced and not name.startswith("_")
        }
        missing = sorted(self._configured_system_info_fields() - produced - set(failed))

        diagnostics = ParseDiagnostics(
            by_resource=dict(per_resource),
            system_info_fields_missing=missing,
            system_info_fields_failed=failed,
        )
        return result, diagnostics

    def _extract_channel_section(
        self,
        section_name: str,
        resources: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], AnchorCount, str | None]:
        """Extract channels for a single section.

        Dispatches to the channel parser registry by section config type.
        Returns (channels, anchor_count, resource_path). resource_path is
        None when the section is absent from parser.yaml.
        """
        section = getattr(self._config, section_name, None)
        if section is None:
            channels: list[dict[str, Any]] = []
            anchors = AnchorCount()
            resource_path: str | None = None
        else:
            parser_fn = CHANNEL_PARSERS.get(type(section))
            if parser_fn is None:
                raise NotImplementedError(f"{type(section).__name__} has no registered channel parser")

            channels, anchors = parser_fn(section, resources)
            # HNAP and arrays-mode JSON sections lack a single section-level
            # `resource` — they don't participate in per-resource stub
            # detection (the wrapper already aggregates per-array internally).
            resource_path = getattr(section, "resource", None) or None

        channels = self._apply_hook(section_name, channels, resources)
        # Post-hook numbering: format parsers auto-assign before hooks
        # run, so channels a hook appends (or builds for an unconfigured
        # section) would otherwise ship without the channel_number every
        # channel MUST carry. See CHANNEL_IDENTIFICATION_SPEC §10.
        if isinstance(channels, list):
            for idx, channel in enumerate(channels, start=1):
                if "channel_number" not in channel:
                    channel["channel_number"] = idx
        return channels, anchors, resource_path

    def _extract_system_info(
        self,
        resources: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, AnchorCount], dict[str, str]]:
        """Extract system_info from all configured sources.

        Dispatches to the system info source registry by source config type.
        Merges results with last-write-wins. Returns (system_info,
        per_resource_anchors, failed_fields). Per-resource anchors
        aggregate across all sources sharing a resource path;
        failed_fields carries conversion-rejected raw values across all
        sources (PARSING_SPEC § Field Outcomes).
        """
        per_resource: dict[str, AnchorCount] = defaultdict(AnchorCount)
        failed: dict[str, str] = {}

        section = self._config.system_info
        if section is None:
            return self._apply_hook("system_info", {}, resources), dict(per_resource), failed

        merged: dict[str, Any] = {}
        for source in section.sources:
            parser_fn = SYSINFO_PARSERS.get(type(source))
            if parser_fn is None:
                raise NotImplementedError(f"{type(source).__name__} has no registered system_info parser")
            data, anchors, source_failed = parser_fn(source, resources)
            merged.update(data)
            failed.update(source_failed)
            # HNAP sysinfo sources share the no-resource property of HNAP
            # channel sections — skip per-resource aggregation for them.
            # Empty-string resource paths (e.g., arrays-mode JSON) are
            # treated the same way.
            resource_path = getattr(source, "resource", None) or None
            if resource_path is not None:
                per_resource[resource_path] = per_resource[resource_path] + anchors

        return self._apply_hook("system_info", merged, resources), dict(per_resource), failed

    def _configured_system_info_fields(self) -> set[str]:
        """Field names parser.yaml maps across all system_info sources.

        JS sources nest field mappings under functions; every other
        format carries a flat ``fields`` list.
        """
        section = self._config.system_info
        if section is None:
            return set()

        configured: set[str] = set()
        for source in section.sources:
            functions = getattr(source, "functions", None)
            if functions is not None:
                for func in functions:
                    configured.update(f.field for f in func.fields)
            else:
                configured.update(f.field for f in getattr(source, "fields", []))
        return {name for name in configured if not name.startswith("_")}

    def _apply_hook(
        self,
        section_name: str,
        data: _T,
        resources: dict[str, Any],
    ) -> _T:
        """Invoke parser.py post-processing hook if present.

        The hook receives the extraction output and the full resource
        dict. Its return value replaces the extraction output.
        """
        if self._post_processor is None:
            return data

        hook_name = _HOOK_NAMES.get(section_name)
        if hook_name is None:
            return data

        hook = getattr(self._post_processor, hook_name, None)
        if hook is None:
            return data

        _logger.debug("Invoking parser.py hook: %s", hook_name)
        result: _T = hook(data, resources)
        return result

    def _enrich_derived_fields(self, data: dict[str, Any]) -> None:
        """Enrich system_info with channel counts, aggregates, and computed fields.

        Runs after all sections are extracted and hooks have run.
        Uses ``setdefault`` so native values from the parser take
        precedence over computed values.

        See PARSING_SPEC.md § Aggregate and § Computed sections.
        """
        system_info = data.setdefault("system_info", {})

        # Channel counts — locked channels only, native wins
        downstream = data.get("downstream", [])
        upstream = data.get("upstream", [])
        system_info.setdefault("downstream_channel_count", _count_locked(downstream))
        system_info.setdefault("upstream_channel_count", _count_locked(upstream))

        # Aggregate sums — from parser.yaml aggregate section
        for field_name, field_def in self._config.aggregate.items():
            if field_name in system_info:
                continue  # native mapping wins

            channels = _select_channels(data, field_def.channels)
            if not channels:
                continue

            total = _sum_field(channels, field_def.sum)
            if total is not None:
                system_info[field_name] = total

        # Computed fields — derived from other system_info fields
        for computed_name, computed_def in self._config.computed.items():
            if computed_name in system_info:
                continue  # native mapping wins

            value = _apply_computed(system_info, computed_def)
            if value is not None:
                system_info[computed_name] = value


# ---------------------------------------------------------------------------
# Unlocked channel nulling — see CHANNEL_IDENTIFICATION_SPEC.md §6
# ---------------------------------------------------------------------------

# Fields preserved on unlocked channels. Everything else is stripped.
_UNLOCKED_KEEP_FIELDS = frozenset({"channel_number", "lock_status"})


def _section_uses_channel_type_derive(section: Any) -> bool:
    """True if the section config carries a ``ChannelTypeDerive`` directive.

    Checks both section-level ``channel_type`` (json/javascript/hnap)
    and per-table ``channel_type`` (xml/html_table). One match anywhere
    flags the section.
    """
    if section is None:
        return False
    if isinstance(getattr(section, "channel_type", None), ChannelTypeDerive):
        return True
    tables = getattr(section, "tables", None)
    if tables:
        return any(isinstance(getattr(t, "channel_type", None), ChannelTypeDerive) for t in tables)
    return False


def _apply_derive_channel_type(channels: list[dict[str, Any]], direction: str) -> None:
    """Fill in ``channel_type`` per channel from modulation + direction.

    Skips channels that already have a non-empty ``channel_type`` —
    per-table derivation may set some channels' types directly while
    others fall through to this helper.
    """
    for ch in channels:
        existing = ch.get("channel_type")
        if existing:
            continue
        derived = derive_channel_type_from_modulation(ch.get("modulation"), direction)
        if derived is not None:
            ch["channel_type"] = derived


def _null_unlocked_channels(channels: list[dict[str, Any]]) -> None:
    """Strip metric fields from unlocked channels (in-place).

    Unlocked channels keep only ``channel_number`` and ``lock_status``.
    All other keys are removed. This prevents firmware noise
    (``-inf``, ``0``, ``"Unknown"``) from leaking into output.

    Channels without ``lock_status`` are left alone — some modems do
    not report lock status at all.
    """
    for channel in channels:
        lock_status = channel.get("lock_status")
        if lock_status is not None and lock_status != "locked":
            for key in list(channel):
                if key not in _UNLOCKED_KEEP_FIELDS:
                    del channel[key]


# ---------------------------------------------------------------------------
# OFDM field normalization — see PARSING_SPEC.md § Output Contract
# ---------------------------------------------------------------------------

# Fields always stripped from OFDM/OFDMA channels.
_OFDM_STRIP_FIELDS = frozenset({"is_ofdm", "symbol_rate"})

# Channel types subject to OFDM field stripping.
_OFDM_TYPES = frozenset({"ofdm", "ofdma"})

# Generic modulation labels that restate channel type rather than
# reporting an actual modulation scheme. Stripped from OFDM/OFDMA.
# Real PLC modulation values (e.g. "QAM4096") are preserved.
# See PARSING_SPEC.md § Output Contract.
_OFDM_GENERIC_MODULATION = frozenset({"Other", "OFDM", "OFDM PLC", "OFDMA"})


def _strip_ofdm_fields(channels: list[dict[str, Any]]) -> None:
    """Strip fields that don't belong on OFDM/OFDMA channels (in-place).

    ``is_ofdm`` is redundant with ``channel_type``. ``symbol_rate`` is
    not applicable to OFDM/OFDMA. Generic modulation labels that
    restate the channel type are stripped — real PLC modulation values
    (e.g. ``"QAM4096"``) are preserved.
    """
    for channel in channels:
        if channel.get("channel_type") in _OFDM_TYPES:
            for field in _OFDM_STRIP_FIELDS:
                channel.pop(field, None)
            mod = channel.get("modulation")
            if mod in _OFDM_GENERIC_MODULATION:
                del channel["modulation"]


def _count_locked(channels: list[dict[str, Any]]) -> int:
    """Count channels that are locked (bonded).

    Channels without ``lock_status`` are counted — they come from
    modems that don't report lock status, so all parsed channels are
    presumed active.
    """
    return sum(1 for ch in channels if ch.get("lock_status", "locked") == "locked")


# ---------------------------------------------------------------------------
# Aggregate helpers — pure functions, no state
# ---------------------------------------------------------------------------


def _select_channels(data: dict[str, Any], scope: str) -> list[dict[str, Any]]:
    """Select channels matching the given scope.

    Scope formats:
    - ``downstream`` — all downstream channels
    - ``upstream`` — all upstream channels
    - ``downstream.qam`` — downstream with ``channel_type == "qam"``
    - ``upstream.atdma`` — upstream with ``channel_type == "atdma"``

    Args:
        data: Parsed ModemData dict with ``downstream`` and ``upstream``.
        scope: Channel scope string from aggregate config.

    Returns:
        List of matching channel dicts.
    """
    parts = scope.split(".", 1)
    direction = parts[0]

    channels: list[dict[str, Any]] = data.get(direction, [])

    if len(parts) == 2:
        channel_type = parts[1]
        channels = [ch for ch in channels if ch.get("channel_type") == channel_type]

    return channels


def _sum_field(channels: list[dict[str, Any]], field_name: str) -> int | float | None:
    """Sum a numeric field across channels.

    Channels missing the field are skipped. Returns ``None`` if no
    channels have the field (avoids returning 0 for a genuinely
    absent field vs. a field that sums to 0).

    Args:
        channels: List of channel dicts.
        field_name: Field to sum.

    Returns:
        Sum of the field, or ``None`` if no channels have it.
    """
    total: int | float = 0
    found = False

    for ch in channels:
        value = ch.get(field_name)
        if value is not None:
            total += value
            found = True

    return total if found else None


def _parse_numeric(value: str) -> float | None:
    """Parse a numeric string, stripping trailing units if present.

    Handles plain numbers (``"233520"``) and values with units
    (``"524288 kB"``). Returns ``None`` if the value is not numeric.
    """
    stripped = value.strip()
    if not stripped:
        return None

    # Try the string as-is first (common case: plain number)
    try:
        return float(stripped)
    except ValueError:
        pass

    # Strip trailing non-numeric suffix (e.g., "524288 kB" → "524288")
    parts = stripped.split(None, 1)
    try:
        return float(parts[0])
    except (ValueError, IndexError):
        return None


def _apply_computed(
    system_info: dict[str, Any],
    computed_def: Any,
) -> float | str | None:
    """Dispatch a computed field operation.

    Resolves input field names from ``computed_def.inputs`` against
    ``system_info``, then applies the named operation.
    """
    if computed_def.operation == "percent_used":
        return _compute_percent_used(system_info, computed_def.inputs, computed_def.precision)
    if computed_def.operation == "combined_status":
        return _compute_combined_status(system_info, computed_def.inputs)
    return None  # pragma: no cover — Literal type prevents reaching here


def _compute_percent_used(
    system_info: dict[str, Any],
    inputs: dict[str, str],
    precision: int,
) -> float | None:
    """Compute usage percentage from total and free system_info fields.

    Expected inputs: ``total`` and ``free``, each mapping to a
    system_info field name.

    Returns ``None`` if either input field is missing or non-numeric,
    or if total is zero (avoids division by zero).
    """
    total_field = inputs.get("total", "")
    free_field = inputs.get("free", "")
    if not total_field or not free_field:
        return None

    raw_total = system_info.get(total_field)
    raw_free = system_info.get(free_field)
    if raw_total is None or raw_free is None:
        return None

    total = _parse_numeric(str(raw_total))
    free = _parse_numeric(str(raw_free))
    if total is None or free is None or total <= 0:
        return None

    return round((total - free) / total * 100, precision)


# Canonical operational value. Modem-specific raw values (e.g. "Complete",
# "Allowed", "Good") are normalized to "Operational" via YAML ``map``
# entries on each modem's parser.yaml system_info fields.
_OPERATIONAL_VALUES = frozenset({"operational"})


def _compute_combined_status(
    system_info: dict[str, Any],
    inputs: dict[str, str],
) -> str | None:
    """Synthesize a single status from multiple status fields.

    Returns ``"Operational"`` when all input fields are present and
    their values match a known-positive DOCSIS provisioning status.
    Returns ``None`` if any input field is missing.  Returns the first
    non-positive value as-is when at least one input is not positive
    (preserves the actual reported status for diagnostics).
    """
    if not inputs:
        return None

    values: list[str] = []
    for field_name in inputs.values():
        raw = system_info.get(field_name)
        if raw is None:
            return None
        values.append(str(raw))

    non_positive = [v for v in values if v.strip().lower() not in _OPERATIONAL_VALUES]
    if non_positive:
        return non_positive[0]

    return "Operational"
