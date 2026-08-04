"""JSONSystemInfoParser — extract system_info from JSON API responses.

Same extraction pattern as JSONParser but produces a flat dict of
system_info fields instead of a channel list. Each field mapping
specifies a key to extract from the JSON response, with optional
dot-notation path navigation.

See PARSING_SPEC.md System Info section (json source).
"""

from __future__ import annotations

import logging
from typing import Any

from ...models.parser_config.system_info import JSONChildAggregate, JSONSystemInfoSource
from ..base import BaseParser
from ..diagnostics import record_failed_field
from ..filter import passes_filter
from ..type_conversion import convert_value
from .json_parser import _navigate_path

_logger = logging.getLogger(__name__)


class JSONSystemInfoParser(BaseParser):
    """Extract system_info from a JSON API response.

    Each instance handles one ``JSONSystemInfoSource`` config, which
    declares a resource and a list of key→field mappings.

    Args:
        config: Validated ``JSONSystemInfoSource`` from parser.yaml.
    """

    def __init__(self, config: JSONSystemInfoSource) -> None:
        self._config = config
        # Conversion-rejected raw values from the most recent parse —
        # PARSING_SPEC § Field Outcomes.
        self.failed_fields: dict[str, str] = {}

    def parse(self, resources: dict[str, Any]) -> dict[str, str]:
        """Extract system_info fields from the configured JSON resource.

        Args:
            resources: Resource dict (path -> parsed JSON dict).

        Returns:
            Dict of system_info field names to string values.
        """
        data = resources.get(self._config.resource)
        if data is None:
            _logger.warning("Resource '%s' not found", self._config.resource)
            return {}

        if not isinstance(data, dict):
            _logger.warning(
                "Resource '%s' is not a dict (got %s)",
                self._config.resource,
                type(data).__name__,
            )
            return {}

        self.failed_fields = {}
        result = self._extract_fields(self._field_source(data))

        # child_aggregates navigate from the whole resource, not from
        # the source-level array_path element the fields read.
        for agg in self._config.child_aggregates:
            value = _child_aggregate_max(data, agg)
            if value is not None:
                result[agg.field] = str(value)

        return result

    def _field_source(self, data: dict[str, Any]) -> dict[str, Any]:
        """Resolve the object field mappings read from, honoring array_path.

        Navigates array_path and takes the first element (same concept
        as the channel parser's array_path).
        """
        if not self._config.array_path:
            return data

        array = _navigate_path(data, self._config.array_path)
        if not isinstance(array, list) or not array:
            _logger.warning(
                "array_path '%s' did not resolve to a non-empty list",
                self._config.array_path,
            )
            return {}
        return array[0] if isinstance(array[0], dict) else {}

    def _extract_fields(self, data: dict[str, Any]) -> dict[str, str]:
        """Extract mapped fields, recording conversion rejections."""
        result: dict[str, str] = {}

        for field_def in self._config.fields:
            # Navigate optional per-field path before key lookup
            target = data
            if field_def.path:
                target = _navigate_path(data, field_def.path)
                if target is None or not isinstance(target, dict):
                    _logger.debug(
                        "Path '%s' not found for field '%s'",
                        field_def.path,
                        field_def.field,
                    )
                    continue

            raw_value = target.get(field_def.key)
            if raw_value is None:
                continue

            converted = convert_value(
                raw_value,
                field_def.type,
                map_config=field_def.map,
                input_format=field_def.format,
                scale=field_def.scale,
            )
            if converted is not None:
                result[field_def.field] = str(converted)
            else:
                record_failed_field(self.failed_fields, field_def.field, raw_value)

        return result


def _child_aggregate_max(data: dict[str, Any], agg: JSONChildAggregate) -> int | float | None:
    """Compute max of a key across filtered items of a JSON array.

    Same order as ``_extract_from_array``: extract, convert (``map``
    applies inside ``convert_value``), filter, then aggregate. Filters
    therefore see normalized values, not wire spellings.
    """
    array = _navigate_path(data, agg.array_path)
    if not isinstance(array, list):
        _logger.warning(
            "child_aggregate '%s': array_path '%s' did not resolve to a list",
            agg.field,
            agg.array_path,
        )
        return None

    best: int | float | None = None

    for entry in array:
        item = _navigate_path(entry, agg.item_path) if agg.item_path else entry
        if not isinstance(item, dict):
            continue

        if not passes_filter(_filter_values(item, agg), agg.filter):
            continue

        raw = item.get(agg.max)
        if raw is None:
            continue

        converted = convert_value(raw, agg.type, scale=agg.scale)
        if converted is not None and isinstance(converted, int | float) and (best is None or converted > best):
            best = converted

    return best


def _filter_values(item: dict[str, Any], agg: JSONChildAggregate) -> dict[str, Any]:
    """Extract and normalize the filter keys of one array item.

    Absent keys are left out so ``passes_filter`` sees ``None`` and
    rejects the item, rather than matching a stringified ``None``.
    """
    values: dict[str, Any] = {}
    for key in agg.filter:
        raw = item.get(key)
        if raw is None:
            continue
        values[key] = convert_value(raw, "string", map_config=agg.map)
    return values
