"""Parser registries — type-to-callable dispatch tables.

Maps parser.yaml section config types to parser callables. Seven channel
format types and six system info source types are registered.

Channel parsers: ``(section, resources) -> tuple[list[dict], AnchorCount]``
System info parsers: ``(source, resources) -> tuple[dict, AnchorCount]``

The ``AnchorCount`` reports how many of the section/source's declared
extraction targets (JS function names, JSON variables, etc.) the parser
actually located in the response body. Used by the coordinator to
detect stub-page responses (see PARSING_SPEC.md § Parser Diagnostics
and ORCHESTRATION_USE_CASES.md § UC-19a).

The ``CHANNEL_PARSERS`` and ``SYSINFO_PARSERS`` dicts derive from
the central format-model lists (``CHANNEL_SECTION_MODELS`` and
``SYSTEM_INFO_SOURCE_MODELS``) by joining each model with its wrapper
in the per-tag tables below. Adding a format means:

1. Define the model with its ``format_tag``/``decode_kind``/
   ``transports`` ClassVars and append it to the appropriate model
   list in ``models/parser_config/``.
2. Define the wrapper here and add an entry to
   ``_CHANNEL_WRAPPERS_BY_TAG`` (or the sysinfo equivalent).

See PARSING_SPEC.md Parser Registry section.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ..models.parser_config.config import CHANNEL_SECTION_MODELS
from ..models.parser_config.javascript import JSEmbeddedSection
from ..models.parser_config.js_json import JSJsonSection
from ..models.parser_config.json_format import JSONSection
from ..models.parser_config.json_transposed import JSONTransposedSection
from ..models.parser_config.system_info import (
    SYSTEM_INFO_SOURCE_MODELS,
    HNAPSystemInfoSource,
    HTMLFieldsSource,
    JSONSystemInfoSource,
    JSSystemInfoSource,
    JSVarsSystemInfoSource,
    XMLSystemInfoSource,
)
from ..models.parser_config.table import HTMLTableSection
from ..models.parser_config.transposed import HTMLTableTransposedSection
from ..models.parser_config.xml_format import XMLSection
from .diagnostics import AnchorCount
from .formats.hnap import HNAPParser
from .formats.hnap_fields import HNAPFieldsParser
from .formats.html_fields import HTMLFieldsParser
from .formats.html_table import HTMLTableParser
from .formats.html_table_transposed import HTMLTableTransposedParser
from .formats.js_embedded import JSEmbeddedParser
from .formats.js_json_parser import JSJsonParser
from .formats.js_system_info import JSSystemInfoParser
from .formats.js_vars import JSVarsParser
from .formats.json_parser import JSONParser
from .formats.json_system_info import JSONSystemInfoParser
from .formats.json_transposed import JSONTransposedParser
from .formats.xml_parser import XMLChannelParser
from .formats.xml_system_info import XMLSystemInfoParser
from .table_selector import find_table

# ---------------------------------------------------------------------------
# Anchor presence detection
# ---------------------------------------------------------------------------

# Trivially-fulfilled count for formats that don't yet do anchor counting.
# Treated as "1 of 1" so the format participates in per-resource aggregation
# without ever flagging stub-detection. Formats opt in by replacing this
# with a real count when they observe the same failure shape #151 hit on
# JS-format parsers.
# Opted in: javascript (#151), javascript_json, table (issue #104).
# Remaining: table_transposed, xml, hnap (HNAP failures are caught as
# LOAD_AUTH before reaching the parser, so LOAD_INTEGRITY is redundant).
# Formats without per-anchor counting still get presence accounting for
# every path they declare — the coordinator covers the ones whose paths
# hang off tables/arrays rather than the section.
_ANCHOR_TRIVIAL = AnchorCount(expected=1, fulfilled=1)


def _count_js_function_anchors(soup: Any, function_names: list[str]) -> AnchorCount:
    """Count how many JS function declarations are present in soup.

    Substring presence check — accurate enough for stub detection.
    A false positive (function name appearing in a comment) is rare
    and at worst suppresses one stub detection; a false negative
    (declaration syntax we don't recognize) is not observed in any
    catalog modem.
    """
    if soup is None:
        return AnchorCount(expected=len(function_names), fulfilled=0)
    text = str(soup) if not isinstance(soup, str) else soup
    fulfilled = sum(1 for name in function_names if re.search(rf"function\s+{re.escape(name)}\s*\(", text))
    return AnchorCount(expected=len(function_names), fulfilled=fulfilled)


def _count_js_variable_anchors(soup: Any, variable_names: list[str]) -> AnchorCount:
    """Count how many JS variable assignments are present in soup."""
    if soup is None:
        return AnchorCount(expected=len(variable_names), fulfilled=0)
    text = str(soup) if not isinstance(soup, str) else soup
    fulfilled = sum(1 for name in variable_names if re.search(rf"{re.escape(name)}\s*=", text))
    return AnchorCount(expected=len(variable_names), fulfilled=fulfilled)


def resource_present(resources: dict[str, Any], resource: str) -> AnchorCount:
    """Trivially-fulfilled count gated on resource presence.

    Used by formats that don't do per-anchor counting yet, and by the
    coordinator for declared paths no format parser counted. Returns
    fulfilled=0 if the resource itself is missing — that's a clear
    signal even without per-anchor introspection.
    """
    if resources.get(resource) is None:
        return AnchorCount(expected=1, fulfilled=0)
    return _ANCHOR_TRIVIAL


# ---------------------------------------------------------------------------
# Channel parser registry
# ---------------------------------------------------------------------------


def _parse_hnap_channels(
    section: Any,
    resources: dict[str, Any],
) -> tuple[list[dict[str, Any]], AnchorCount]:
    """Parse channels from an HNAP section.

    HNAP doesn't fit the URL-keyed resource model — it's a SOAP batch.
    Stub-page detection (UC-19a) is HTML-specific. HNAP failures are
    caught earlier as LOAD_AUTH (stale session) or LOAD_ERROR (HTTP),
    so the wrapper reports trivially fulfilled.
    """
    hnap_parser = HNAPParser(section)
    channels = hnap_parser.parse(resources)
    if not isinstance(channels, list):
        channels = []
    return channels, _ANCHOR_TRIVIAL


def _parse_html_table_channels(
    section: HTMLTableSection,
    resources: dict[str, Any],
) -> tuple[list[dict[str, Any]], AnchorCount]:
    """Parse channels from HTML table section(s) with merge_by support."""
    primary_channels: list[dict[str, Any]] = []
    companion_tables: list[tuple[list[dict[str, Any]], list[str]]] = []

    soup = resources.get(section.resource)
    fulfilled = 0

    for table_def in section.tables:
        parser = HTMLTableParser(section.resource, table_def)
        channels = parser.parse(resources)
        if not isinstance(channels, list):
            continue

        # A stub page (JS redirect on session expiry) has soup but no tables —
        # fulfilled stays 0, triggering LOAD_INTEGRITY per UC-19a.
        if soup is not None and find_table(soup, table_def.selector) is not None:
            fulfilled += 1

        if table_def.merge_by is not None:
            companion_tables.append((channels, table_def.merge_by))
        else:
            primary_channels.extend(channels)

    for companion_channels, merge_by in companion_tables:
        _merge_channels(primary_channels, companion_channels, merge_by)

    # Auto-assign channel_number from 1-based row position when not
    # already mapped by parser.yaml.  See CHANNEL_IDENTIFICATION_SPEC §10.
    for idx, channel in enumerate(primary_channels, start=1):
        if "channel_number" not in channel:
            channel["channel_number"] = idx

    return primary_channels, AnchorCount(expected=len(section.tables), fulfilled=fulfilled)


def _parse_transposed_channels(
    section: HTMLTableTransposedSection,
    resources: dict[str, Any],
) -> tuple[list[dict[str, Any]], AnchorCount]:
    """Parse channels from transposed HTML table section(s) with merge_by support."""
    # Normalize flat form to tables list
    if section.tables is not None:
        tables = section.tables
    else:
        from ..models.parser_config.transposed import TransposedTableDefinition

        assert section.selector is not None and section.rows is not None
        tables = [
            TransposedTableDefinition(
                selector=section.selector,
                rows=section.rows,
                channel_type=section.channel_type,
            )
        ]

    primary_channels: list[dict[str, Any]] = []
    companion_tables: list[tuple[list[dict[str, Any]], list[str]]] = []

    for table_def in tables:
        parser = HTMLTableTransposedParser(section.resource, table_def)
        channels = parser.parse(resources)
        if not isinstance(channels, list):
            continue

        if table_def.merge_by is not None:
            companion_tables.append((channels, table_def.merge_by))
        else:
            primary_channels.extend(channels)

    for companion_channels, merge_by in companion_tables:
        _merge_channels(primary_channels, companion_channels, merge_by)

    # Auto-assign channel_number from 1-based row position when not
    # already mapped by parser.yaml.  See CHANNEL_IDENTIFICATION_SPEC §10.
    for idx, channel in enumerate(primary_channels, start=1):
        if "channel_number" not in channel:
            channel["channel_number"] = idx

    return primary_channels, resource_present(resources, section.resource)


def _parse_js_embedded_channels(
    section: JSEmbeddedSection,
    resources: dict[str, Any],
) -> tuple[list[dict[str, Any]], AnchorCount]:
    """Parse channels from JS-embedded section with unified channel_number.

    Concatenates function outputs in declaration order and assigns unified
    1-based ``channel_number`` across the combined list. Emits
    ``source_channel_number`` when the per-function position differs from
    the unified number.  See CHANNEL_IDENTIFICATION_SPEC §10.

    Anchor count: each declared ``functions[].name`` is one expected
    anchor. Fulfilled when the function declaration is present in the
    soup (substring/regex check on the response body) — distinct from
    "function returned channels," which conflates "function found,
    yielded zero rows" (UC-04) with "function not found" (UC-19a).
    """
    function_results: list[list[dict[str, Any]]] = []
    for func in section.functions:
        parser = JSEmbeddedParser(section.resource, func)
        result = parser.parse(resources)
        if isinstance(result, list):
            function_results.append(result)

    channels: list[dict[str, Any]] = []
    unified = 1
    for func_channels in function_results:
        for func_pos, channel in enumerate(func_channels, start=1):
            channel["channel_number"] = unified
            if func_pos != unified:
                channel["source_channel_number"] = func_pos
            unified += 1
            channels.append(channel)

    soup = resources.get(section.resource)
    anchors = _count_js_function_anchors(soup, [f.name for f in section.functions])
    return channels, anchors


def _parse_json_channels(
    section: JSONSection,
    resources: dict[str, Any],
) -> tuple[list[dict[str, Any]], AnchorCount]:
    """Parse channels from a JSON API section.

    JSONSection supports two shapes (see ``models/parser_config/
    json_format.py``):
    - flat: section-level ``resource`` + ``fields``
    - arrays: list of ``arrays[].resource`` + per-array fields

    Anchor accounting is per-shape — the flat path counts the single
    section resource; the arrays path counts each declared array's
    resource. Stub detection on JSON modems isn't yet observed in the
    field, so each path reports trivially fulfilled when its resources
    are present.
    """
    parser = JSONParser(section)
    channels = parser.parse(resources)
    if not isinstance(channels, list):
        channels = []

    # Auto-assign channel_number from 1-based row position when not
    # already mapped by parser.yaml.  See CHANNEL_IDENTIFICATION_SPEC §10.
    for idx, channel in enumerate(channels, start=1):
        if "channel_number" not in channel:
            channel["channel_number"] = idx

    if section.arrays:
        # Multi-array shape: each array may have its own resource, or
        # share the section-level resource (e.g., a single endpoint that
        # returns a mixed list filtered by channel_type). Per-array
        # resource takes precedence; fall back to section-level.
        # See JSONSection model — arrays' resource defaults to "" and
        # validation requires either flat `resource` or per-array resources.
        expected = len(section.arrays)
        fulfilled = sum(1 for arr in section.arrays if resources.get(arr.resource or section.resource) is not None)
        return channels, AnchorCount(expected=expected, fulfilled=fulfilled)
    return channels, resource_present(resources, section.resource)


def _parse_js_json_channels(
    section: JSJsonSection,
    resources: dict[str, Any],
) -> tuple[list[dict[str, Any]], AnchorCount]:
    """Parse channels from a js_json section — JSON arrays in JS variables.

    Anchor count: the section's ``variable`` is one expected anchor.
    Fulfilled when the variable assignment is present in the soup.
    See PARSING_SPEC.md § Parser Diagnostics, FORMAT_JAVASCRIPT_SPEC.md
    § Failure modes.
    """
    parser = JSJsonParser(section)
    channels = parser.parse(resources)
    if not isinstance(channels, list):
        channels = []

    # Auto-assign channel_number from 1-based row position when not
    # already mapped by parser.yaml.  See CHANNEL_IDENTIFICATION_SPEC §10.
    for idx, channel in enumerate(channels, start=1):
        if "channel_number" not in channel:
            channel["channel_number"] = idx

    soup = resources.get(section.resource)
    anchors = _count_js_variable_anchors(soup, [section.variable])
    return channels, anchors


def _parse_xml_channels(
    section: XMLSection,
    resources: dict[str, Any],
) -> tuple[list[dict[str, Any]], AnchorCount]:
    """Parse channels from an XML section.

    XMLSection has multiple ``tables[].resource`` rather than a single
    section-level resource. For now, treat as trivially fulfilled —
    the cbn transport hasn't exhibited the stub-page failure shape that
    drives UC-19a; opt in if the same pattern surfaces.
    """
    parser = XMLChannelParser(section)
    channels = parser.parse(resources)
    if not isinstance(channels, list):
        channels = []

    # Auto-assign channel_number from 1-based row position when not
    # already mapped by parser.yaml.  See CHANNEL_IDENTIFICATION_SPEC §10.
    for idx, channel in enumerate(channels, start=1):
        if "channel_number" not in channel:
            channel["channel_number"] = idx

    return channels, _ANCHOR_TRIVIAL


def _parse_json_transposed_channels(
    section: JSONTransposedSection,
    resources: dict[str, Any],
) -> tuple[list[dict[str, Any]], AnchorCount]:
    """Parse channels from a JSONTransposedParser section."""
    parser = JSONTransposedParser(section)
    channels = parser.parse(resources)
    if not isinstance(channels, list):
        channels = []

    # Auto-assign channel_number from 1-based row position when not
    # already mapped by parser.yaml.  See CHANNEL_IDENTIFICATION_SPEC §10.
    for idx, channel in enumerate(channels, start=1):
        if "channel_number" not in channel:
            channel["channel_number"] = idx

    return channels, resource_present(resources, section.resource)


# Wrappers keyed by format_tag. Combined with CHANNEL_SECTION_MODELS
# below to build CHANNEL_PARSERS — preserves locality (wrapper lives
# next to its peers) while removing the duplicated model→callable
# table.
_CHANNEL_WRAPPERS_BY_TAG: dict[str, Callable[..., tuple[list[dict[str, Any]], AnchorCount]]] = {
    "table": _parse_html_table_channels,
    "table_transposed": _parse_transposed_channels,
    "javascript": _parse_js_embedded_channels,
    "javascript_json": _parse_js_json_channels,
    "hnap": _parse_hnap_channels,
    "json": _parse_json_channels,
    "json_transposed": _parse_json_transposed_channels,
    "xml": _parse_xml_channels,
}

# Maps section config type -> parser callable(section, resources) ->
# (list[dict], AnchorCount). Built by joining the central model list
# with the wrapper table — a missing wrapper for a registered model
# raises at import time.
CHANNEL_PARSERS: dict[type, Callable[..., tuple[list[dict[str, Any]], AnchorCount]]] = {
    model: _CHANNEL_WRAPPERS_BY_TAG[model.format_tag] for model in CHANNEL_SECTION_MODELS
}


# ---------------------------------------------------------------------------
# System info source registry
# ---------------------------------------------------------------------------


def _parse_html_fields_sysinfo(
    source: HTMLFieldsSource,
    resources: dict[str, Any],
) -> tuple[dict[str, Any], AnchorCount, dict[str, str]]:
    """Parse system_info from HTML label/value pairs."""
    html_si = HTMLFieldsParser(source)
    result = html_si.parse(resources)
    if not isinstance(result, dict):
        result = {}
    return result, resource_present(resources, source.resource), html_si.failed_fields


def _parse_hnap_sysinfo(
    source: HNAPSystemInfoSource,
    resources: dict[str, Any],
) -> tuple[dict[str, Any], AnchorCount, dict[str, str]]:
    """Parse system_info from HNAP response fields.

    See note on ``_parse_hnap_channels``: HNAP is not subject to the
    HTML stub-page failure mode that drives UC-19a.
    """
    hnap_si = HNAPFieldsParser(source)
    result = hnap_si.parse(resources)
    if not isinstance(result, dict):
        result = {}
    return result, _ANCHOR_TRIVIAL, hnap_si.failed_fields


def _parse_js_sysinfo(
    source: JSSystemInfoSource,
    resources: dict[str, Any],
) -> tuple[dict[str, Any], AnchorCount, dict[str, str]]:
    """Parse system_info from JS-embedded tagValueList variables.

    Anchor count: each non-empty ``functions[].name`` is one expected
    anchor (function declaration). Functions with empty ``name`` look
    for ``tagValueList`` at top-level script scope and don't have a
    countable function anchor — they contribute to ``_resource_present``
    instead.
    """
    js_si = JSSystemInfoParser(source)
    result = js_si.parse(resources)
    if not isinstance(result, dict):
        result = {}

    soup = resources.get(source.resource)
    named_funcs = [f.name for f in source.functions if f.name]
    if named_funcs:
        anchors = _count_js_function_anchors(soup, named_funcs)
    else:
        anchors = resource_present(resources, source.resource)
    return result, anchors, js_si.failed_fields


def _parse_js_vars_sysinfo(
    source: JSVarsSystemInfoSource,
    resources: dict[str, Any],
) -> tuple[dict[str, Any], AnchorCount, dict[str, str]]:
    """Parse system_info from JS variable assignments.

    Anchor count: each ``fields[].source`` (JS variable name) is one
    expected anchor. Fulfilled when the variable assignment is present
    in the soup.
    """
    js_vars_si = JSVarsParser(source)
    result = js_vars_si.parse(resources)
    if not isinstance(result, dict):
        result = {}

    soup = resources.get(source.resource)
    var_names = [f.source for f in source.fields]
    if var_names:
        anchors = _count_js_variable_anchors(soup, var_names)
    else:
        anchors = resource_present(resources, source.resource)
    return result, anchors, js_vars_si.failed_fields


def _parse_json_sysinfo(
    source: JSONSystemInfoSource,
    resources: dict[str, Any],
) -> tuple[dict[str, Any], AnchorCount, dict[str, str]]:
    """Parse system_info from a JSON API response."""
    json_si = JSONSystemInfoParser(source)
    result = json_si.parse(resources)
    if not isinstance(result, dict):
        result = {}
    return result, resource_present(resources, source.resource), json_si.failed_fields


def _parse_xml_sysinfo(
    source: XMLSystemInfoSource,
    resources: dict[str, Any],
) -> tuple[dict[str, Any], AnchorCount, dict[str, str]]:
    """Parse system_info from XML element fields."""
    xml_si = XMLSystemInfoParser(source)
    result = xml_si.parse(resources)
    if not isinstance(result, dict):
        result = {}
    return result, resource_present(resources, source.resource), xml_si.failed_fields


# Wrappers keyed by format_tag. Combined with SYSTEM_INFO_SOURCE_MODELS
# below to build SYSINFO_PARSERS.
_SYSINFO_WRAPPERS_BY_TAG: dict[str, Callable[..., tuple[dict[str, Any], AnchorCount, dict[str, str]]]] = {
    "html_fields": _parse_html_fields_sysinfo,
    "hnap": _parse_hnap_sysinfo,
    "javascript": _parse_js_sysinfo,
    "javascript_vars": _parse_js_vars_sysinfo,
    "json": _parse_json_sysinfo,
    "xml": _parse_xml_sysinfo,
}

# Maps source config type -> parser callable(source, resources) ->
# (dict, AnchorCount, failed_fields). The third element carries
# conversion-rejected raw values — PARSING_SPEC § Field Outcomes.
SYSINFO_PARSERS: dict[type, Callable[..., tuple[dict[str, Any], AnchorCount, dict[str, str]]]] = {
    model: _SYSINFO_WRAPPERS_BY_TAG[model.format_tag] for model in SYSTEM_INFO_SOURCE_MODELS
}


# ---------------------------------------------------------------------------
# Merge utility (used by table and transposed factory functions)
# ---------------------------------------------------------------------------


def _merge_channels(
    primary: list[dict[str, Any]],
    merge_table: list[dict[str, Any]],
    merge_by: list[str],
) -> None:
    """Merge fields from a companion table into primary channels.

    Builds a lookup by the declared key fields, then enriches primary
    channels. Primary always wins on field conflicts.

    Per PARSING_SPEC.md Companion Tables (merge_by) section.
    """
    merge_map: dict[tuple[Any, ...], dict[str, Any]] = {}
    for ch in merge_table:
        key = tuple(ch.get(field) for field in merge_by)
        merge_map[key] = ch

    for ch in primary:
        key = tuple(ch.get(field) for field in merge_by)
        extra = merge_map.get(key, {})
        for field, value in extra.items():
            if field not in ch:
                ch[field] = value
