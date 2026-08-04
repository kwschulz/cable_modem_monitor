"""Cross-file consistency checks between modem.yaml and parser.yaml.

Validates constraints that span both config files — transport vs format
compatibility, and aggregate field name collisions with system_info.

Individual file validation is handled by Pydantic model validators
(transport constraints, auth-session-action consistency, required
fields by status). This module handles what neither model can validate
alone.
"""

from __future__ import annotations

from enum import StrEnum

from ..models.modem_config import ModemConfig
from ..models.parser_config import ParserConfig
from ..models.parser_config.config import ALL_FORMAT_MODELS
from ..models.parser_config.format_registry import format_tags_for_transport
from ..models.parser_config.system_info import (
    JSONSystemInfoSource,
    JSSystemInfoSource,
    SystemInfoSection,
    XMLSystemInfoSource,
)

# Formats valid per transport — derived from the central registry.
# Adding a format with new transport coverage updates this without
# editing this file. See MODEM_YAML_SPEC.md transport-format table.
_VALID_FORMATS: dict[str, frozenset[str]] = {
    transport: format_tags_for_transport(transport, ALL_FORMAT_MODELS) for transport in ("cbn", "hnap", "http")
}


def validate_cross_file(modem: ModemConfig, parser: ParserConfig) -> list[str]:
    """Run cross-file consistency checks.

    Args:
        modem: Validated ModemConfig instance.
        parser: Validated ParserConfig instance.

    Returns:
        List of error messages. Empty list means all checks passed.
    """
    errors: list[str] = []
    _check_transport_format(modem, parser, errors)
    _check_aggregate_collisions(modem, parser, errors)
    _check_provisioned_speed_direction(parser, errors)
    return errors


def _check_transport_format(modem: ModemConfig, parser: ParserConfig, errors: list[str]) -> None:
    """Validate that parser.yaml formats are compatible with modem.yaml transport."""
    valid_formats = _VALID_FORMATS.get(modem.transport, frozenset())
    section_formats = _collect_section_formats(parser)

    for section_name, fmt in section_formats:
        if fmt not in valid_formats:
            if fmt == "xml":
                errors.append(
                    f"XML format in section '{section_name}' is not yet " "supported — no parser implementation exists"
                )
            else:
                errors.append(
                    f"transport '{modem.transport}' does not support format "
                    f"'{fmt}' in section '{section_name}' — "
                    f"valid formats: {sorted(valid_formats)}"
                )


def _check_aggregate_collisions(modem: ModemConfig, parser: ParserConfig, errors: list[str]) -> None:
    """Validate that aggregate field names don't collide with system_info fields."""
    if not parser.aggregate or parser.system_info is None:
        return

    system_info_fields = _collect_system_info_fields(parser.system_info)
    for agg_name in parser.aggregate:
        if agg_name in system_info_fields:
            errors.append(f"aggregate field '{agg_name}' collides with system_info " f"field — one source per field")


def _collect_section_formats(parser: ParserConfig) -> list[tuple[str, str]]:
    """Collect (section_name, format) pairs from parser config.

    Returns format values for downstream, upstream, and system_info
    source formats.
    """
    results: list[tuple[str, str]] = []

    if parser.downstream is not None:
        results.append(("downstream", parser.downstream.format))

    if parser.upstream is not None:
        results.append(("upstream", parser.upstream.format))

    if parser.system_info is not None:
        for source in parser.system_info.sources:
            results.append(("system_info", source.format))

    return results


def _collect_system_info_fields(section: SystemInfoSection) -> set[str]:
    """Collect all field names from system_info sources.

    Handles all source types: html_fields, hnap, json have a ``fields``
    list directly. javascript sources have ``functions`` containing
    ``fields`` lists.
    """
    fields: set[str] = set()

    for source in section.sources:
        if isinstance(source, JSSystemInfoSource):
            for func in source.functions:
                for js_field in func.fields:
                    fields.add(js_field.field)
        else:
            for mapping in source.fields:
                fields.add(mapping.field)

    return fields


class _DocsisDirection(StrEnum):
    """Canonical service flow direction vocabulary.

    DOCS-IF3-MIB's ``IfDirection`` textual convention defines both the
    words and the numeric codes ``downstream(1)``/``upstream(2)``. The
    words are canonical here because a swap is visible on the page;
    a swap of ``"1"`` and ``"2"`` is not.
    """

    DOWNSTREAM = "downstream"
    UPSTREAM = "upstream"


# Provisioned speed/burst field names and their expected direction.
_DIRECTION_FIELDS: dict[str, _DocsisDirection] = {
    "provisioned_speed_down": _DocsisDirection.DOWNSTREAM,
    "provisioned_speed_up": _DocsisDirection.UPSTREAM,
    "provisioned_burst_down": _DocsisDirection.DOWNSTREAM,
    "provisioned_burst_up": _DocsisDirection.UPSTREAM,
}


def _check_provisioned_speed_direction(parser: ParserConfig, errors: list[str]) -> None:
    """Validate that provisioned speed child_aggregates filter on the right direction.

    Filters compare against the canonical vocabulary from DOCS-IF3-MIB's
    ``IfDirection`` textual convention; other wire spellings (the
    numeric codes, mixed case) are normalized in the aggregate's
    ``map``. A swap produces plausible-looking but inverted speed
    values that are easy to miss in review.
    """
    if parser.system_info is None:
        return

    for source in parser.system_info.sources:
        if not isinstance(source, JSONSystemInfoSource | XMLSystemInfoSource):
            continue
        for agg in source.child_aggregates:
            expected = _DIRECTION_FIELDS.get(agg.field)
            if expected is None:
                continue
            actual = agg.filter.get("direction")
            # JSON filters also allow {"not": ...} rules; only a plain
            # equality filter carries a direction to check.
            if isinstance(actual, str) and actual != expected:
                errors.append(
                    f"child_aggregate '{agg.field}' filters on direction "
                    f"'{actual}' but must filter on '{expected}' — values "
                    f"will be swapped. Normalize other wire spellings in 'map'."
                )
