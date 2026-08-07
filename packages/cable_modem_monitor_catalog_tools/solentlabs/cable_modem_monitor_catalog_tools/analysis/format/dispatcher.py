"""Phase 5-6 dispatcher - format detection and section assembly.

Routes to transport-specific modules (http / hnap) for format
classification, then delegates to mapping and mapping.system_info
for Phase 6 extraction. Assembles the ``sections`` output dict.

Per docs/ONBOARDING_SPEC.md Phases 5-6.
"""

from __future__ import annotations

import posixpath
from typing import Any

from ...validation.har_utils import WARNING_PREFIX
from ..mapping.channel_detection import detect_channel_type_fixed
from ..mapping.system_info import detect_system_info
from ..types import FleetPatterns
from .hnap import detect_hnap_sections
from .http import (
    analyze_page,
    classify_page_format,
    identify_data_pages,
)
from .table_analysis import (
    detect_row_start,
    detect_table_direction,
    detect_table_selector,
    is_channel_table,
)
from .types import PageAnalysis

# extract_section_mappings is imported inside the _assemble_* helpers
# below, not here. format and mapping are mutually dependent packages:
# mapping/__init__ pulls mapping.dispatcher, which pulls
# format.table_analysis, which initializes this package. A module-level
# "from ..mapping import ..." would run while mapping/__init__ is still
# executing and the name is unbound, so importing any analysis.mapping
# leaf module raises ImportError. Submodule imports above are safe;
# only the from-package import has to be deferred.


def detect_sections(
    entries: list[dict[str, Any]],
    transport: str,
    warnings: list[str],
    hard_stops: list[str],
    *,
    fleet: FleetPatterns | None = None,
) -> dict[str, Any]:
    """Run Phases 5-6: format detection, field mapping, section assembly.

    Args:
        entries: HAR ``log.entries`` list.
        transport: Detected transport ("http" or "hnap").
        warnings: Mutable list to append warnings to.
        hard_stops: Mutable list to append hard stops to.
        fleet: Optional fleet patterns for augmented detection.

    Returns:
        Sections dict with downstream, upstream, and system_info keys.
        Each section uses format-agnostic ``mappings``.
    """
    if transport == "hnap":
        return detect_hnap_sections(entries, warnings, hard_stops, fleet=fleet)

    return _detect_http_sections(entries, warnings, hard_stops, fleet=fleet)


def _detect_http_sections(
    entries: list[dict[str, Any]],
    warnings: list[str],
    hard_stops: list[str],
    *,
    fleet: FleetPatterns | None = None,
) -> dict[str, Any]:
    """Detect HTTP format sections from data pages.

    Identifies data pages, classifies format per page, extracts field
    mappings for channel sections, and detects system_info sources.
    """
    data_pages = identify_data_pages(entries)
    if not data_pages:
        warnings.append(f"{WARNING_PREFIX} No data pages found in HAR. Cannot detect format or field mappings.")
        return {}

    # Phase 5: Analyze each data page
    page_analyses: list[PageAnalysis] = []
    for entry in data_pages:
        page = analyze_page(entry)
        page_analyses.append(page)

    # Phase 5-6: Assemble channel sections from table/JS/JSON pages
    sections: dict[str, Any] = {}
    _assemble_channel_sections(page_analyses, sections, warnings, fleet=fleet)

    # Phase 6: Detect system_info sources
    system_info = detect_system_info(page_analyses, warnings, fleet=fleet)
    if system_info:
        sections["system_info"] = system_info.to_dict()

    return sections


def _assemble_channel_sections(
    pages: list[PageAnalysis],
    sections: dict[str, Any],
    warnings: list[str],
    *,
    fleet: FleetPatterns | None = None,
) -> None:
    """Assemble downstream and upstream sections from page analyses."""
    for page in pages:
        fmt = classify_page_format(page)

        if fmt == "json":
            _assemble_json_sections(page, sections, warnings)
        elif fmt == "javascript_json":
            _assemble_js_json_sections(page, sections, warnings)
        elif fmt == "javascript":
            # For pages that have both JavaScript functions and HTML
            # channel tables, try the tables first. Tables provide more
            # complete and field-labelled channel data (DOCSIS 3.1 modems
            # like the Netgear CM1100 expose both a JS tagValueList and
            # full-detail HTML tables on the same page). The "first wins"
            # guard in _assemble_table_sections and _assemble_js_sections
            # ensures JS only fills sections not already populated by tables.
            if page.tables:
                _assemble_table_sections(page, "table", sections, warnings, fleet=fleet)
            _assemble_js_sections(page, sections, warnings, fleet=fleet)
        elif fmt in ("table", "table_transposed"):
            _assemble_table_sections(page, fmt, sections, warnings, fleet=fleet)


def _assemble_table_sections(
    page: PageAnalysis,
    fmt: str,
    sections: dict[str, Any],
    warnings: list[str],
    *,
    fleet: FleetPatterns | None = None,
) -> None:
    """Assemble channel sections from HTML table pages."""
    from ..mapping import extract_section_mappings

    for table in page.tables:
        # Only consider tables that contain channel data — skip
        # layout, navigation, and provisioning status tables.
        if not is_channel_table(table):
            continue

        direction = detect_table_direction(table, fleet=fleet)
        if direction == "unknown":
            warnings.append(
                f"{WARNING_PREFIX} Cannot determine direction for table "
                f"on {page.resource} (index {table.table_index}). "
                "Manual review required."
            )
            continue

        # Skip if section already populated (first table wins)
        if direction in sections:
            continue

        selector = detect_table_selector(table, all_tables=page.tables)
        row_start = detect_row_start(table)

        # Phase 6: Extract field mappings
        section = extract_section_mappings(
            fmt=fmt,
            table=table,
            resource=page.resource,
            direction=direction,
            warnings=warnings,
        )

        if section is None:
            continue

        section.selector = selector
        section.row_start = row_start
        sections[direction] = section.to_dict()


def _assemble_js_sections(
    page: PageAnalysis,
    sections: dict[str, Any],
    warnings: list[str],
    *,
    fleet: FleetPatterns | None = None,
) -> None:
    """Assemble channel sections from JavaScript-embedded data.

    Collects all JS functions per direction.  When the primary (first)
    function for a direction is processed, subsequent functions are
    appended as ``additional_js_functions`` on the section dict when a
    fleet layout exists for them — enabling DOCSIS 3.1 modems that expose
    separate QAM and OFDM functions to emit both in a single section.
    """
    from ..mapping import extract_section_mappings

    for js_func in page.js_functions:
        # Infer direction from function name
        direction = _direction_from_js_name(js_func.name)
        if direction == "unknown":
            warnings.append(
                f"{WARNING_PREFIX} Cannot determine direction for JS function '{js_func.name}' on {page.resource}."
            )
            continue

        if direction in sections:
            # Additional function for an already-populated direction —
            # append via fleet layout when available (e.g., OFDM after QAM).
            if fleet and js_func.name in fleet.js_function_layouts:
                existing = sections[direction]
                additional = existing.setdefault("additional_js_functions", [])
                additional.append(fleet.js_function_layouts[js_func.name])
            continue

        section = extract_section_mappings(
            fmt="javascript",
            js_function=js_func,
            resource=page.resource,
            direction=direction,
            warnings=warnings,
            fleet=fleet,
        )

        if section is None:
            continue

        sections[direction] = section.to_dict()


def _assemble_js_json_sections(
    page: PageAnalysis,
    sections: dict[str, Any],
    warnings: list[str],
) -> None:
    """Assemble channel sections from JS-embedded JSON arrays.

    Each detected variable (e.g., ``json_dsData``, ``json_usData``)
    becomes a section.  Direction is inferred from the variable name
    or from the JSON key structure.
    """
    from ...validation.har_utils import WARNING_PREFIX
    from ..mapping import extract_section_mappings

    for js_var in page.js_json_variables:
        # Wrap as dict so extract_section_mappings can find the array
        json_data = {"_raw": js_var.data}

        section = extract_section_mappings(
            fmt="json",
            json_data=json_data,
            resource=page.resource,
            warnings=warnings,
        )

        if section is None:
            continue

        # Override format to javascript_json in the output
        section.format = "javascript_json"
        section.variable = js_var.name

        # Infer direction from variable name, then resource path
        direction = _direction_from_js_name(js_var.name)
        if direction == "unknown":
            direction = _direction_from_resource(page.resource)
        if direction == "unknown":
            direction = _direction_from_json(json_data)

        if direction == "unknown":
            warnings.append(
                f"{WARNING_PREFIX} Cannot determine direction for JS JSON variable "
                f"'{js_var.name}' on {page.resource}. Manual review required."
            )
            continue

        if direction not in sections:
            if section.channel_type is None:
                section.channel_type = detect_channel_type_fixed(direction)
            sections[direction] = section.to_dict()


def _assemble_json_sections(
    page: PageAnalysis,
    sections: dict[str, Any],
    warnings: list[str],
) -> None:
    """Assemble channel sections from JSON API responses."""
    from ..mapping import extract_section_mappings

    if page.json_data is None:
        return

    section = extract_section_mappings(
        fmt="json",
        json_data=page.json_data,
        resource=page.resource,
        warnings=warnings,
    )

    if section is None:
        return

    # Infer direction from resource path or JSON structure
    direction = _direction_from_resource(page.resource)
    if direction == "unknown":
        direction = _direction_from_json(page.json_data)

    if direction == "unknown":
        warnings.append(
            f"{WARNING_PREFIX} Cannot determine direction for JSON data on {page.resource}. Manual review required."
        )
        return

    if direction not in sections:
        # DOCSIS 3.0 JSON APIs rarely embed channel type; apply fixed
        # fallback when no channelType key was found in the JSON data.
        if section.channel_type is None:
            section.channel_type = detect_channel_type_fixed(direction)
        sections[direction] = section.to_dict()


def _direction_from_js_name(name: str) -> str:
    """Infer downstream/upstream from JS function name."""
    lower = name.lower()
    if "ds" in lower or "downstream" in lower:
        return "downstream"
    if "us" in lower or "upstream" in lower:
        return "upstream"
    return "unknown"


def _direction_from_resource(resource: str) -> str:
    """Infer downstream/upstream from resource path.

    Checks full keywords first, then DOCSIS abbreviation prefixes
    (``ds``/``us``) in path segments and filenames — handles URLs like
    ``/data/dsinfo.asp`` or ``/data/usinfo.asp`` that omit the full word.
    """
    lower = resource.lower()
    if "downstream" in lower:
        return "downstream"
    if "upstream" in lower:
        return "upstream"

    # Check path segments for ds/us prefixes (e.g. /data/dsinfo.asp)
    segments = [posixpath.splitext(s)[0] for s in lower.split("/") if s]
    for seg in segments:
        if _has_direction_prefix(seg, ("ds",)):
            return "downstream"
        if _has_direction_prefix(seg, ("us",)):
            return "upstream"

    return "unknown"


def _direction_from_json(data: dict[str, Any]) -> str:
    """Infer downstream/upstream from JSON structure keys.

    Scans top-level and nested keys to handle modems like the G54
    that nest channels under ``docsis.dschannel``, ``docsis.uschannel``.
    """
    if _scan_keys_for_direction(data, "downstream", ("ds",)):
        return "downstream"
    if _scan_keys_for_direction(data, "upstream", ("us",)):
        return "upstream"
    return "unknown"


def _scan_keys_for_direction(
    data: dict[str, Any],
    full_name: str,
    prefixes: tuple[str, ...],
    depth: int = 0,
) -> bool:
    """Recursively scan JSON keys for direction indicators."""
    if depth > 3:
        return False
    for key, value in data.items():
        lower = key.lower()
        if full_name in lower:
            return True
        if _has_direction_prefix(lower, prefixes):
            return True
        if isinstance(value, dict) and _scan_keys_for_direction(value, full_name, prefixes, depth + 1):
            return True
    return False


# Common English words starting with "us" or "ds" that are NOT
# direction indicators.  Checked before prefix matching to avoid
# false positives like "user" → upstream.
_NON_DIRECTION_WORDS = frozenset(
    {
        "user",
        "username",
        "usage",
        "used",
        "usable",
        "usual",
        "usually",
        "dst",
    }
)


def _has_direction_prefix(key: str, prefixes: tuple[str, ...]) -> bool:
    """Check if a key starts with a direction prefix.

    Matches DOCSIS compound words like ``dschannel``, ``ds_power``,
    ``dsChannel`` but NOT common English words like ``user``,
    ``username``, ``usage`` (which start with ``us`` but are not
    direction indicators).
    """
    if key in _NON_DIRECTION_WORDS:
        return False
    return any(key.startswith(p) and len(key) > len(p) for p in prefixes)
