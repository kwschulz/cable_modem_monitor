"""Phase 6 - System info detection.

Detects system_info sources across multiple pages and formats:
html_fields (label/id based), javascript, json. Builds a multi-source
configuration matching the parser.yaml system_info schema.

Per docs/ONBOARDING_SPEC.md Phase 6 and docs/PARSING_SPEC.md system_info section.
"""

from __future__ import annotations

from typing import Any

from ..format.types import DetectedLabelPair, PageAnalysis
from ..types import FleetPatterns
from .field_shape import FieldShapeVocabulary, field_shape
from .service_flows import detect_service_flow_aggregates
from .types import (
    SystemInfoDetail,
    SystemInfoFieldDetail,
    SystemInfoSourceDetail,
)

# -----------------------------------------------------------------------
# System info label-to-field mapping (ONBOARDING_SPEC Phase 6)
# -----------------------------------------------------------------------

# Maps lowercase label text -> (canonical_field, tier)
_LABEL_FIELD_MAP: dict[str, tuple[str, int]] = {
    # Tier 1 canonical
    "system up time": ("system_uptime", 1),
    "uptime": ("system_uptime", 1),
    "system uptime": ("system_uptime", 1),
    "software version": ("software_version", 1),
    "firmware version": ("software_version", 1),
    "sw version": ("software_version", 1),
    "hardware version": ("hardware_version", 1),
    "hw version": ("hardware_version", 1),
    "model": ("hardware_version", 1),
    "network access": ("docsis_status", 1),
    "cable modem status": ("docsis_status", 1),
    # Tier 2 registered
    "boot status": ("boot_status", 2),
    "boot state": ("boot_status", 2),
    "docsis version": ("docsis_version", 2),
    "temperature": ("temperature", 2),
    # Identity PII (serial number, MAC) intentionally not mapped — no CMM
    # consumer; see SYSTEM_INFO_SPEC § Tiered Sensor Model.
}

# ID-based label mapping (element ids commonly used for system info)
_ID_FIELD_MAP: dict[str, tuple[str, int]] = {
    "systemuptime": ("system_uptime", 1),
    "firmwareversion": ("software_version", 1),
    "softwareversion": ("software_version", 1),
    "hardwareversion": ("hardware_version", 1),
    "networkaccess": ("docsis_status", 1),
    "bootstate": ("boot_status", 2),
    "docsisversion": ("docsis_version", 2),
}

# JSON key mapping for system info
_JSON_SYSINFO_MAP: dict[str, tuple[str, int]] = {
    "uptime": ("system_uptime", 1),
    "systemuptime": ("system_uptime", 1),
    "system_uptime": ("system_uptime", 1),
    "firmwareversion": ("software_version", 1),
    "firmware_version": ("software_version", 1),
    "softwareversion": ("software_version", 1),
    "software_version": ("software_version", 1),
    "hardwareversion": ("hardware_version", 1),
    "hardware_version": ("hardware_version", 1),
    "model": ("hardware_version", 1),
    "networkaccess": ("docsis_status", 1),
    "network_access": ("docsis_status", 1),
    "status": ("docsis_status", 1),
    "bootstatus": ("boot_status", 2),
    "boot_status": ("boot_status", 2),
    "docsisversion": ("docsis_version", 2),
    "docsis_version": ("docsis_version", 2),
}


# -----------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------


def detect_system_info(
    pages: list[PageAnalysis],
    warnings: list[str],
    *,
    fleet: FleetPatterns | None = None,
) -> SystemInfoDetail | None:
    """Detect system_info sources across all analyzed pages.

    Scans label-value pairs, JS functions, and JSON data for system info
    fields. When ``fleet`` is provided, fleet-derived label mappings
    augment Core's hardcoded baseline.

    Returns a multi-source SystemInfoDetail or None if nothing found.
    """
    label_map, id_map, json_map = _build_merged_maps(fleet)
    vocabulary = FieldShapeVocabulary.from_fleet(fleet)
    sources: list[SystemInfoSourceDetail] = []

    for page in pages:
        _detect_page_system_info(page, label_map, id_map, json_map, sources, vocabulary)

    if not sources:
        return None

    return SystemInfoDetail(sources=sources)


def _build_merged_maps(
    fleet: FleetPatterns | None,
) -> tuple[
    dict[str, tuple[str, int]],
    dict[str, tuple[str, int]],
    dict[str, tuple[str, int]],
]:
    """Build merged label, ID, and JSON key maps from baseline + fleet."""
    label_map = dict(_LABEL_FIELD_MAP)
    id_map = dict(_ID_FIELD_MAP)
    json_map = dict(_JSON_SYSINFO_MAP)

    if fleet:
        for src, tgt in (
            (fleet.system_info_labels, label_map),
            (fleet.system_info_ids, id_map),
            (fleet.system_info_json_keys, json_map),
        ):
            for key, mapping in src.items():
                if key not in tgt:
                    tgt[key] = mapping

    return label_map, id_map, json_map


def _detect_page_system_info(
    page: PageAnalysis,
    label_map: dict[str, tuple[str, int]],
    id_map: dict[str, tuple[str, int]],
    json_map: dict[str, tuple[str, int]],
    sources: list[SystemInfoSourceDetail],
    vocabulary: FieldShapeVocabulary | None = None,
) -> None:
    """Detect system_info sources from a single page."""
    vocabulary = vocabulary or FieldShapeVocabulary()
    # html_fields: label-value pairs
    if page.label_pairs:
        fields = _match_label_pairs(page.label_pairs, label_map, id_map, vocabulary)
        if fields:
            sources.append(
                SystemInfoSourceDetail(
                    format="html_fields",
                    resource=page.resource,
                    fields=fields,
                )
            )

    # javascript: non-directional JS functions may contain system
    # info labels. Directional functions (ds/us in name) belong to
    # channel sections and are skipped here.
    for js_func in page.js_functions:
        if _is_directional_js(js_func.name):
            continue
        fields = _match_js_system_info(js_func.values)
        if fields:
            sources.append(
                SystemInfoSourceDetail(
                    format="javascript",
                    resource=page.resource,
                    fields=fields,
                )
            )

    # json: JSON data with system info keys. A service flow resource
    # carries no scalar system_info fields, only the per-flow array, so
    # aggregates alone are enough to make the source worth emitting.
    if page.json_data is not None:
        fields = _match_json_system_info(page.json_data, json_map, vocabulary)
        aggregates = detect_service_flow_aggregates(page.json_data)
        if fields or aggregates:
            sources.append(
                SystemInfoSourceDetail(
                    format="json",
                    resource=page.resource,
                    fields=fields,
                    child_aggregates=aggregates,
                )
            )


# -----------------------------------------------------------------------
# HTML label-value matching
# -----------------------------------------------------------------------


def _match_label_pairs(
    pairs: list[DetectedLabelPair],
    label_map: dict[str, tuple[str, int]],
    id_map: dict[str, tuple[str, int]],
    vocabulary: FieldShapeVocabulary | None = None,
) -> list[SystemInfoFieldDetail]:
    """Match detected label-value pairs to system info fields."""
    vocabulary = vocabulary or FieldShapeVocabulary()
    fields: list[SystemInfoFieldDetail] = []
    seen_fields: set[str] = set()

    for pair in pairs:
        field_name, _tier = _match_label(pair.label, pair.selector_type, label_map, id_map)
        if not field_name or field_name in seen_fields:
            continue

        seen_fields.add(field_name)
        field_type, field_format, field_map = field_shape(field_name, pair.value, vocabulary)
        fields.append(
            SystemInfoFieldDetail(
                field=field_name,
                type=field_type,
                format=field_format,
                map=field_map,
                selector_type=pair.selector_type,
                selector_value=pair.selector_value,
            )
        )

    return fields


def _match_label(
    label: str,
    selector_type: str,
    label_map: dict[str, tuple[str, int]] | None = None,
    id_map: dict[str, tuple[str, int]] | None = None,
) -> tuple[str, int]:
    """Match a label or id to a system info field name.

    Args:
        label: Raw label text from the page.
        selector_type: ``"id"`` or ``"label"`` / ``"css_pattern"``.
        label_map: Label lookup map. Defaults to ``_LABEL_FIELD_MAP``.
        id_map: ID lookup map. Defaults to ``_ID_FIELD_MAP``.

    Returns:
        ``(field_name, tier)`` or ``("", 0)``.
    """
    if label_map is None:
        label_map = _LABEL_FIELD_MAP
    if id_map is None:
        id_map = _ID_FIELD_MAP

    normalized = label.strip().lower().rstrip(":")

    if selector_type == "id":
        if normalized in id_map:
            return id_map[normalized]
        # Fall through to label-based
        normalized_no_caps = normalized.replace("_", " ")
        if normalized_no_caps in label_map:
            return label_map[normalized_no_caps]
    else:
        if normalized in label_map:
            return label_map[normalized]

    return "", 0


# -----------------------------------------------------------------------
# JavaScript direction filter
# -----------------------------------------------------------------------

_DIRECTIONAL_KEYWORDS: frozenset[str] = frozenset({"ds", "us", "downstream", "upstream"})


def _is_directional_js(name: str) -> bool:
    """Return True if the JS function name indicates channel direction.

    Directional functions (e.g., InitDsTableTagValue, InitUsTableTagValue)
    belong to channel sections. Non-directional functions are candidates
    for system_info detection.
    """
    lower = name.lower()
    return any(kw in lower for kw in _DIRECTIONAL_KEYWORDS)


# -----------------------------------------------------------------------
# JavaScript system info matching
# -----------------------------------------------------------------------


def _match_js_system_info(
    values: list[str],
) -> list[SystemInfoFieldDetail]:
    """Match JS delimited values to system info fields.

    JS system info functions typically have key-value pairs or
    positional fields. This is a best-effort heuristic.
    """
    fields: list[SystemInfoFieldDetail] = []
    seen: set[str] = set()

    for value in values:
        stripped = value.strip()
        if not stripped:
            continue

        # Check if value looks like a system info label
        field_name, _tier = _match_label(stripped, "label")
        if field_name and field_name not in seen:
            seen.add(field_name)
            fields.append(
                SystemInfoFieldDetail(
                    field=field_name,
                    type="string",
                    source=stripped,
                )
            )

    return fields


# -----------------------------------------------------------------------
# JSON system info matching
# -----------------------------------------------------------------------


def _match_json_system_info(
    data: dict[str, Any],
    json_map: dict[str, tuple[str, int]] | None = None,
    vocabulary: FieldShapeVocabulary | None = None,
) -> list[SystemInfoFieldDetail]:
    """Match JSON keys to system info fields."""
    vocabulary = vocabulary or FieldShapeVocabulary()
    if json_map is None:
        json_map = _JSON_SYSINFO_MAP

    fields: list[SystemInfoFieldDetail] = []
    seen: set[str] = set()

    _walk_json_for_sysinfo(data, json_map, fields, seen, "", vocabulary)

    return fields


def _walk_json_for_sysinfo(
    data: dict[str, Any],
    json_map: dict[str, tuple[str, int]],
    fields: list[SystemInfoFieldDetail],
    seen: set[str],
    prefix: str,
    vocabulary: FieldShapeVocabulary,
) -> None:
    """Recursively walk JSON looking for system info fields."""
    for key, value in data.items():
        normalized = key.strip().lower()

        if normalized in json_map:
            field_name, _tier = json_map[normalized]
            if field_name not in seen and isinstance(value, str | int | float):
                seen.add(field_name)
                field_type, field_format, field_map = field_shape(field_name, value, vocabulary)
                fields.append(
                    SystemInfoFieldDetail(
                        field=field_name,
                        type=field_type,
                        format=field_format,
                        map=field_map,
                        # Core looks the key up literally inside the
                        # container named by path. Emitting
                        # "container.key" as the key matches nothing and
                        # fails silently.
                        source=key,
                        path=prefix,
                    )
                )

        # Recurse into nested dicts (but not lists)
        if isinstance(value, dict):
            child_prefix = f"{prefix}.{key}" if prefix else key
            _walk_json_for_sysinfo(value, json_map, fields, seen, child_prefix, vocabulary)
