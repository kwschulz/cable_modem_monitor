"""JSONParser — extract channel data from JSON API responses.

Modem REST APIs return channel data as JSON arrays within nested
objects. The ``array_path`` locates the channel array via dot-notation
path navigation. Each object in the array is mapped to a channel dict
using key→field mappings from parser.yaml.

Supports flat form (single array_path) or multi-array form (arrays list)
for modems with multiple channel arrays in one response.

Parameterized by a ``JSONSection`` from parser.yaml. The coordinator
creates one instance per JSON channel section.

See PARSING_SPEC.md JSONParser section.
"""

from __future__ import annotations

import logging
from typing import Any

from ...models.parser_config.common import (
    ChannelTypeConfig,
    ChannelTypeFixed,
    ChannelTypeMap,
    FilterValue,
    JsonChannelMapping,
)
from ...models.parser_config.json_format import JSONSection
from ..base import BaseParser
from ..filter import passes_filter
from ..type_conversion import convert_value

_logger = logging.getLogger(__name__)


def _navigate_path(data: Any, path: str) -> Any:
    """Navigate a dot-notation path within nested dicts and lists.

    Supports dict keys and numeric list indices::

        "downstream.channels"   → data["downstream"]["channels"]
        "_raw.0"                → data["_raw"][0]

    Args:
        data: Root structure to navigate.
        path: Dot-separated path (keys or integer indices).

    Returns:
        The value at the path, or ``None`` if any segment is missing.
    """
    current = data
    for key in path.split("."):
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, list) and key.isdigit():
            idx = int(key)
            current = current[idx] if idx < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


class JSONParser(BaseParser):
    """Extract channel data from a JSON API response.

    Handles both flat form (single array) and multi-array form.
    In multi-array form, channels from all arrays are concatenated.

    Args:
        config: Validated ``JSONSection`` from parser.yaml.
    """

    def __init__(self, config: JSONSection) -> None:
        self._config = config

    def parse(self, resources: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract channels from the configured JSON resource(s).

        Args:
            resources: Resource dict (path -> parsed JSON dict).

        Returns:
            List of channel dicts with converted field values.
        """
        if self._config.arrays is not None:
            return self._parse_multi_array(resources)

        data = _get_resource(resources, self._config.resource)
        if data is None:
            return []
        return self._parse_single_array(data)

    def _parse_single_array(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse channels from a single array_path (flat form)."""
        return _extract_from_array(
            data,
            self._config.array_path,
            self._config.fields or [],
            self._config.channel_type,
            self._config.fixed_fields,
            self._config.filter,
        )

    def _parse_multi_array(self, resources: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse channels from multiple arrays and concatenate results.

        Each array may specify its own resource endpoint. When absent,
        the section-level resource is used as the default.
        """
        channels: list[dict[str, Any]] = []
        for array_def in self._config.arrays or []:
            resource_key = array_def.resource or self._config.resource
            data = _get_resource(resources, resource_key)
            if data is None:
                continue
            channels.extend(
                _extract_from_array(
                    data,
                    array_def.array_path,
                    array_def.fields,
                    array_def.channel_type,
                    array_def.fixed_fields,
                    array_def.filter,
                )
            )
        return channels


def _get_resource(resources: dict[str, Any], key: str) -> dict[str, Any] | None:
    """Look up a resource by path, logging warnings on miss or wrong type."""
    data = resources.get(key)
    if data is None:
        _logger.warning("Resource '%s' not found", key)
        return None
    if not isinstance(data, dict):
        _logger.warning("Resource '%s' is not a dict (got %s)", key, type(data).__name__)
        return None
    return data


def _extract_from_array(
    data: dict[str, Any],
    array_path: str,
    mappings: list[JsonChannelMapping],
    channel_type: ChannelTypeConfig | None,
    fixed_fields: dict[str, str],
    filter_rules: dict[str, FilterValue],
) -> list[dict[str, Any]]:
    """Extract channels from a single JSON array.

    Shared by both flat and multi-array forms.
    """
    array = _navigate_path(data, array_path)
    if array is None:
        _logger.warning("Array path '%s' not found", array_path)
        return []

    if not isinstance(array, list):
        _logger.warning(
            "Value at '%s' is not a list (got %s)",
            array_path,
            type(array).__name__,
        )
        return []

    channels: list[dict[str, Any]] = []
    for item in array:
        if not isinstance(item, dict):
            continue

        channel = _extract_channel(item, mappings)
        if channel is None:
            continue

        _apply_channel_type(channel, channel_type)

        for field_name, field_value in fixed_fields.items():
            channel[field_name] = field_value

        if not passes_filter(channel, filter_rules):
            continue

        channels.append(channel)

    return channels


def _extract_channel(
    item: dict[str, Any],
    mappings: list[JsonChannelMapping],
) -> dict[str, Any] | None:
    """Extract field values from one JSON object by key.

    Tries the primary ``key`` first, then ``fallback_key`` if present.
    Returns ``None`` if no fields could be extracted.

    Raw values that are absent, whitespace-only, or listed in the
    mapping's ``null_values`` are silently skipped (sparse-data
    signal). Non-empty raw values that fail to coerce — after any
    declared ``separator``/``range_op`` transform — surface as WARN
    logs so catalog gaps don't go unnoticed when firmware introduces
    a novel shape.
    """
    channel: dict[str, Any] = {}

    for mapping in mappings:
        original_raw = item.get(mapping.key)

        # Try fallback key if primary is missing
        if original_raw is None and mapping.fallback_key:
            original_raw = item.get(mapping.fallback_key)

        if original_raw is None:
            continue
        if isinstance(original_raw, str) and not original_raw.strip():
            continue
        # Declared no-value sentinels are sparse data, same as absent or
        # whitespace-only; the cannot-coerce WARN below stays reserved
        # for shapes the catalog has not seen.
        if isinstance(original_raw, str) and original_raw.strip() in mapping.null_values:
            continue

        # Boolean truthy check: compare against declared truthy value
        if mapping.truthy is not None:
            channel[mapping.field] = original_raw == mapping.truthy
            continue

        raw_value, transform_warned = _apply_transform(original_raw, mapping)

        if raw_value is None:
            if not transform_warned:
                _logger.warning(
                    "Field '%s' (type=%s): transform produced no value for raw %r",
                    mapping.field,
                    mapping.type,
                    original_raw,
                )
            continue

        value = convert_value(
            raw_value,
            mapping.type,
            unit=mapping.unit,
            map_config=mapping.map,
            scale=mapping.scale,
            input_format=mapping.format,
        )

        if value is None:
            _logger.warning(
                "Field '%s' (type=%s): cannot coerce raw value %r — "
                "no extractor handled this shape. Declare a separator "
                "or range in parser.yaml if firmware uses a compound format.",
                mapping.field,
                mapping.type,
                original_raw,
            )
            continue

        channel[mapping.field] = value

    return channel if channel else None


def _apply_transform(
    raw_value: Any,
    mapping: JsonChannelMapping,
) -> tuple[Any, bool]:
    """Apply separator + optional range transform before type conversion.

    Returns ``(transformed_value, transform_warned)``. ``transform_warned``
    is True when the transform itself logged a WARN or intentionally
    suppressed silent-fail surfacing for an expected-skip case (e.g.
    ``range`` declared but raw isn't range-shaped — SC-QAM rows reading
    the same key as OFDM range rows).
    """
    if mapping.range == "span":
        # Only produce a value when raw is a string carrying the separator.
        # Mixed-shape tables (SC-QAM numeric Frequency in the same array as
        # OFDM range strings) land in the silent-skip branch.
        if mapping.separator and isinstance(raw_value, str) and mapping.separator in raw_value:
            parts = raw_value.split(mapping.separator)
            transformed = _compute_span(parts, raw_value, mapping.field)
            return transformed, transformed is None
        return None, True  # intentional silent skip

    if mapping.separator and isinstance(raw_value, str):
        parts = raw_value.split(mapping.separator)
        idx = mapping.separator_index
        return (parts[idx] if idx < len(parts) else raw_value), False

    return raw_value, False


def _compute_span(parts: list[str], original_raw: Any, field_name: str) -> str | None:
    """Compute ``last - first`` from separator-split parts.

    Used by ``range_op: span`` to derive a band span (e.g. OFDM
    ``channel_width``) from a band-edge string like ``"751~860"``.
    Returns the span as a string for downstream type coercion. Returns
    ``None`` and WARN-logs on shape surprises so the catalog author is
    notified rather than silently absorbing an unexpected value.
    """
    if len(parts) < 2:
        _logger.warning(
            "Field '%s': range_op span needs separator-split parts, " "got single value %r — firmware shape change?",
            field_name,
            original_raw,
        )
        return None
    try:
        low = float(parts[0].strip())
        high = float(parts[-1].strip())
    except ValueError:
        _logger.warning(
            "Field '%s': cannot parse range bounds in %r as numbers.",
            field_name,
            original_raw,
        )
        return None
    if high <= low:
        _logger.warning(
            "Field '%s': range bounds reversed or zero in %r (low=%s, high=%s).",
            field_name,
            original_raw,
            low,
            high,
        )
        return None
    return str(high - low)


def _apply_channel_type(
    channel: dict[str, Any],
    channel_type: ChannelTypeConfig | None,
) -> None:
    """Apply channel_type from config (fixed or field→map derivation)."""
    if "channel_type" in channel:
        return

    if channel_type is None:
        return

    if isinstance(channel_type, ChannelTypeFixed):
        channel["channel_type"] = channel_type.fixed
        return

    if isinstance(channel_type, ChannelTypeMap):
        raw_value = str(channel.get(channel_type.field, ""))
        if raw_value and raw_value in channel_type.map:
            channel["channel_type"] = channel_type.map[raw_value]
        elif raw_value:
            _logger.warning(
                "Unmapped channel_type value: '%s' (known: %s)",
                raw_value,
                list(channel_type.map.keys()),
            )
