"""XML system_info parser.

Extracts system_info fields from XML responses by navigating to a
named root element and reading sub-element text content.

See PARSING_SPEC.md XMLParser section (system_info).
"""

from __future__ import annotations

import logging
from typing import Any
from xml.etree.ElementTree import Element

from ...models.parser_config.system_info import XMLChildAggregate, XMLSystemInfoSource
from ..diagnostics import record_failed_field
from ..type_conversion import convert_value

_logger = logging.getLogger(__name__)


class XMLSystemInfoParser:
    """Parse system_info fields from an XML Element.

    Args:
        config: Validated ``XMLSystemInfoSource`` from parser.yaml.
    """

    def __init__(self, config: XMLSystemInfoSource) -> None:
        self._config = config
        # Conversion-rejected raw values from the most recent parse —
        # PARSING_SPEC § Field Outcomes.
        self.failed_fields: dict[str, str] = {}

    def parse(self, resources: dict[str, Any]) -> dict[str, Any]:
        """Extract system_info fields from the resource dict.

        Args:
            resources: Dict keyed by fun parameter string, values are
                ``Element`` objects from the CBN loader.

        Returns:
            Dict of canonical field names to values.
        """
        root = resources.get(self._config.resource)
        if root is None:
            _logger.debug(
                "XML resource '%s' not found in resources",
                self._config.resource,
            )
            return {}

        if not isinstance(root, Element):
            _logger.debug(
                "XML resource '%s' is not an Element (got %s)",
                self._config.resource,
                type(root).__name__,
            )
            return {}

        # Find the named root element
        container = root.find(self._config.root_element)
        if container is None:
            if root.tag == self._config.root_element:
                container = root
            else:
                _logger.debug(
                    "XML root element '%s' not found",
                    self._config.root_element,
                )
                return {}

        result = self._extract_fields(container)

        # Extract child element aggregates
        for agg in self._config.child_aggregates:
            value = _child_aggregate_max(container, agg)
            if value is not None:
                result[agg.field] = value

        return result

    def _extract_fields(self, container: Element) -> dict[str, Any]:
        """Extract mapped fields, recording conversion rejections."""
        result: dict[str, Any] = {}
        self.failed_fields = {}
        for field_map in self._config.fields:
            sub = container.find(field_map.source)
            if sub is None or sub.text is None:
                continue
            raw_value = sub.text.strip()
            value = convert_value(
                raw_value,
                field_map.type,
                map_config=field_map.map,
                input_format=field_map.format,
                scale=field_map.scale,
            )
            if value is not None:
                result[field_map.field] = value
            elif raw_value:
                record_failed_field(self.failed_fields, field_map.field, raw_value)
        return result


def _child_aggregate_max(container: Element, agg: XMLChildAggregate) -> Any:
    """Compute max of a sub-element value across filtered child elements.

    Iterates all ``agg.child_element`` children of ``container``,
    keeps those matching ``agg.filter`` key-value pairs, and returns
    the max of ``agg.max`` sub-element values after type conversion.
    Filter text runs through ``agg.map`` first, so filters compare
    normalized values rather than wire spellings.
    """
    best: int | float | None = None

    for child in container.findall(agg.child_element):
        # Apply filter: all key-value pairs must match
        if not all(_apply_map(child.findtext(key, "").strip(), agg.map) == value for key, value in agg.filter.items()):
            continue

        raw = child.findtext(agg.max, "")
        if not raw or not raw.strip():
            continue

        converted = convert_value(raw.strip(), agg.type, scale=agg.scale)
        if converted is not None and isinstance(converted, int | float) and (best is None or converted > best):
            best = converted

    return best


def _apply_map(value: str, map_config: dict[str, str] | None) -> str:
    """Normalize a raw filter value, same exact-match semantics as convert_value."""
    if map_config is None:
        return value
    return map_config.get(value, value)
