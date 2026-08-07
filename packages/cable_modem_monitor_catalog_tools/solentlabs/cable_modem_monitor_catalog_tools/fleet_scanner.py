"""Fleet scanner — fleet pattern extraction and catalog health audit.

Two responsibilities:

**Pattern extraction** (``scan_fleet``): reads all ``parser.yaml`` files
and builds a ``FleetPatterns`` instance used by Core's analyzer to augment
baseline detection with proven patterns from the existing catalog.

**Catalog health audit** (``audit_fleet_auth``): sweeps all ``modem.yaml``
files and checks that form-auth fixtures contain the login page responses
the runtime auth manager needs. Used by the intake pipeline regression
script to surface fixture gaps without blocking CI.

Usage::

    from cable_modem_monitor_catalog import CATALOG_PATH
    from cable_modem_monitor_catalog_tools.fleet_scanner import scan_fleet

    fleet = scan_fleet(CATALOG_PATH)
    result = analyze_har(har_path, fleet=fleet)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from solentlabs.cable_modem_monitor_catalog_tools.analysis.types import FleetPatterns
from solentlabs.cable_modem_monitor_catalog_tools.validation.fixture_integrity import check_login_page_in_har


def scan_fleet(catalog_path: Path) -> FleetPatterns:
    """Scan all parser.yaml files and build fleet patterns.

    Args:
        catalog_path: Root of the modem catalog directory
            (``modems/{manufacturer}/{model}/``).

    Returns:
        ``FleetPatterns`` populated from the fleet's proven configs.
    """
    selector_directions: dict[str, str] = {}
    system_info_labels: dict[str, tuple[str, int]] = {}
    system_info_ids: dict[str, tuple[str, int]] = {}
    system_info_json_keys: dict[str, tuple[str, int]] = {}
    delimiters: set[str] = set()
    channel_type_values: set[str] = set()
    aggregate_fields: list[tuple[str, str]] = []
    seen_aggregates: set[tuple[str, str]] = set()
    js_function_layouts: dict[str, dict[str, Any]] = {}
    hnap_response_layouts: dict[str, dict[str, Any]] = {}
    uptime_formats: set[str] = set()
    docsis_status_values: set[str] = set()

    for parser_path in sorted(catalog_path.rglob("parser.yaml")):
        try:
            data = yaml.safe_load(parser_path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            continue

        if not isinstance(data, dict):
            continue

        _extract_selectors(data, selector_directions)
        _extract_system_info_labels(data, system_info_labels)
        _extract_system_info_ids(data, system_info_ids)
        _extract_system_info_json_keys(data, system_info_json_keys)
        _extract_delimiters(data, delimiters)
        _extract_channel_type_values(data, channel_type_values)
        _extract_aggregates(data, aggregate_fields, seen_aggregates)
        _extract_js_function_layouts(data, js_function_layouts)
        _extract_hnap_response_layouts(data, hnap_response_layouts)
        _extract_uptime_formats(data, uptime_formats)
        _extract_docsis_status_values(data, docsis_status_values)

    return FleetPatterns(
        selector_directions=selector_directions,
        system_info_labels=system_info_labels,
        system_info_ids=system_info_ids,
        system_info_json_keys=system_info_json_keys,
        delimiters=delimiters,
        channel_type_values=channel_type_values,
        aggregate_fields=aggregate_fields,
        js_function_layouts=js_function_layouts,
        hnap_response_layouts=hnap_response_layouts,
        # Longest first: a more specific pattern must be tried before a
        # shorter one that would also match a prefix of the same value.
        uptime_formats=sorted(uptime_formats, key=lambda f: (-len(f), f)),
        docsis_status_success_values=docsis_status_values,
    )


# ---------------------------------------------------------------------------
# Fleet auth audit
# ---------------------------------------------------------------------------


@dataclass
class AuthAuditIssue:
    """A login_page fixture-integrity issue found in the catalog.

    Attributes:
        modem: Relative modem path (e.g. ``netgear/cm1100``).
        har: Name of the HAR fixture file (e.g. ``modem.har``).
        issue: Human-readable description of what's wrong.
    """

    modem: str
    har: str
    issue: str


def audit_fleet_auth(catalog_path: Path) -> list[AuthAuditIssue]:
    """Scan form-auth fixtures for the login-flow responses replay needs.

    Two checks per ``test_data/*.har``:

    - With ``login_page`` and ``action`` set, the fixture must hold a GET
      response for the login page with a non-empty HTML body referencing
      the action.
    - A login answering a redirect must land on a page the fixture holds,
      which is what ``auth.success.redirect`` is evaluated against.

    Both surface gaps the intake pipeline would have prevented but manual
    fixture assembly can introduce — the same checks enforced at intake
    time by ``write_modem_package`` and the HAR validation gate.

    Args:
        catalog_path: Root of the modem catalog directory.

    Returns:
        List of ``AuthAuditIssue`` entries.  Empty list means no issues.
    """
    issues: list[AuthAuditIssue] = []

    for modem_yaml_path in sorted(catalog_path.rglob("modem.yaml")):
        try:
            config: Any = yaml.safe_load(modem_yaml_path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            continue

        if not isinstance(config, dict):
            continue

        auth = config.get("auth", {})
        if not isinstance(auth, dict):
            continue
        if auth.get("strategy") != "form":
            continue

        login_page: str = auth.get("login_page", "") or ""
        action: str = auth.get("action", "") or ""

        modem_dir = modem_yaml_path.parent
        test_data = modem_dir / "test_data"
        modem_rel = str(modem_dir.relative_to(catalog_path))

        for har_path in sorted(test_data.glob("*.har")):
            try:
                with open(har_path, encoding="utf-8") as f:
                    har_data: Any = json.load(f)
                entries: list[Any] = har_data.get("log", {}).get("entries", [])
            except (OSError, json.JSONDecodeError):
                continue

            if login_page and action:
                issue = check_login_page_in_har(entries, login_page, action)
                if issue:
                    issues.append(AuthAuditIssue(modem=modem_rel, har=har_path.name, issue=issue))

            issues.extend(_redirect_landing_issues(entries, modem_rel, har_path.name))

    return issues


def _redirect_landing_issues(
    entries: list[Any],
    modem_rel: str,
    har_name: str,
) -> list[AuthAuditIssue]:
    """Run the HAR gate's redirect-landing check over one committed fixture."""
    # Same function the intake gate runs, so a contributor's capture and a
    # committed fixture are judged by one implementation.
    from .validation.auth_flow import validate_auth_redirect_landing
    from .validation.har_utils import WARNING_PREFIX

    warnings: list[str] = []
    validate_auth_redirect_landing(entries, warnings)
    return [
        AuthAuditIssue(modem=modem_rel, har=har_name, issue=w.removeprefix(WARNING_PREFIX).strip()) for w in warnings
    ]


# -----------------------------------------------------------------------
# Selector → direction
# -----------------------------------------------------------------------


def _extract_selectors(
    data: dict[str, object],
    result: dict[str, str],
) -> None:
    """Extract selector.match → direction mappings from parser.yaml."""
    for direction in ("downstream", "upstream"):
        section = data.get(direction)
        if not isinstance(section, dict):
            continue

        _add_selector(section.get("selector"), direction, result)

        tables = section.get("tables")
        if isinstance(tables, list):
            for table in tables:
                if isinstance(table, dict):
                    _add_selector(table.get("selector"), direction, result)


def _add_selector(
    selector: object,
    direction: str,
    result: dict[str, str],
) -> None:
    """Add a single selector match to the result map."""
    if not isinstance(selector, dict):
        return
    match = selector.get("match")
    if isinstance(match, str) and match.strip():
        result[match.strip().lower()] = direction


# -----------------------------------------------------------------------
# System info: labels, IDs, JSON keys
# -----------------------------------------------------------------------


def _extract_system_info_labels(
    data: dict[str, object],
    result: dict[str, tuple[str, int]],
) -> None:
    """Extract label → (field, tier) from system_info ``label`` fields."""
    for field_def in _iter_system_info_fields(data):
        label = field_def.get("label")
        field_name = field_def.get("field")
        if isinstance(label, str) and isinstance(field_name, str) and label.strip() and field_name.strip():
            normalized = label.strip().lower()
            if normalized not in result:
                result[normalized] = (field_name.strip(), 1)


def _extract_uptime_formats(
    data: dict[str, object],
    result: set[str],
) -> None:
    """Collect ``format`` strings from the fleet's ``type: uptime`` fields."""
    for field_def in _iter_system_info_fields(data):
        if field_def.get("type") != "uptime":
            continue
        fmt = field_def.get("format")
        if isinstance(fmt, str) and fmt.strip():
            result.add(fmt.strip())


def _extract_docsis_status_values(
    data: dict[str, object],
    result: set[str],
) -> None:
    """Collect raw docsis_status spellings the fleet maps to "Operational".

    Only the canonical success target is harvested. Error and
    in-progress mappings must not teach intake to flatten a real fault
    into "Operational".
    """
    for field_def in _iter_system_info_fields(data):
        if field_def.get("field") != "docsis_status":
            continue
        mapping = field_def.get("map")
        if not isinstance(mapping, dict):
            continue
        for raw, canonical in mapping.items():
            if isinstance(raw, str) and canonical == "Operational" and raw.strip():
                result.add(raw.strip().lower())


def _extract_system_info_ids(
    data: dict[str, object],
    result: dict[str, tuple[str, int]],
) -> None:
    """Extract element ID → (field, tier) from system_info ``id`` fields."""
    for field_def in _iter_system_info_fields(data):
        css_id = field_def.get("id")
        field_name = field_def.get("field")
        if isinstance(css_id, str) and isinstance(field_name, str) and css_id.strip() and field_name.strip():
            normalized = css_id.strip().lower()
            if normalized not in result:
                result[normalized] = (field_name.strip(), 1)


def _extract_system_info_json_keys(
    data: dict[str, object],
    result: dict[str, tuple[str, int]],
) -> None:
    """Extract JSON key → (field, tier) from system_info ``key`` fields."""
    for field_def in _iter_system_info_fields(data):
        key = field_def.get("key")
        field_name = field_def.get("field")
        if isinstance(key, str) and isinstance(field_name, str) and key.strip() and field_name.strip():
            normalized = key.strip().lower()
            if normalized not in result:
                result[normalized] = (field_name.strip(), 1)


def _iter_system_info_fields(
    data: dict[str, object],
) -> list[dict[str, object]]:
    """Iterate over all field dicts in system_info sources."""
    system_info = data.get("system_info")
    if not isinstance(system_info, dict):
        return []

    sources = system_info.get("sources")
    if not isinstance(sources, list):
        return []

    fields: list[dict[str, object]] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        fields.extend(_dict_items(source.get("fields")))
        # javascript sources nest their fields one level deeper, under
        # each function. Skipping them hides every field they declare.
        for function in _dict_items(source.get("functions")):
            fields.extend(_dict_items(function.get("fields")))
    return fields


def _dict_items(value: object) -> list[dict[str, object]]:
    """Return the dict entries of a list, or empty when it is not one."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


# -----------------------------------------------------------------------
# Delimiters
# -----------------------------------------------------------------------


def _extract_delimiters(
    data: dict[str, object],
    result: set[str],
) -> None:
    """Extract record delimiters from HNAP and JavaScript configs."""
    for direction in ("downstream", "upstream"):
        section = data.get(direction)
        if not isinstance(section, dict):
            continue

        # Section-level delimiter (HNAP/JS)
        _add_delimiter(section.get("delimiter"), result)

        # Per-function delimiters (JS)
        functions = section.get("functions")
        if isinstance(functions, list):
            for func in functions:
                if isinstance(func, dict):
                    _add_delimiter(func.get("delimiter"), result)


def _add_delimiter(value: object, result: set[str]) -> None:
    """Add a non-empty string delimiter to the result set."""
    if isinstance(value, str) and value.strip():
        result.add(value.strip())


# -----------------------------------------------------------------------
# Channel type values
# -----------------------------------------------------------------------


def _extract_channel_type_values(
    data: dict[str, object],
    result: set[str],
) -> None:
    """Extract channel type map keys from downstream/upstream sections."""
    for direction in ("downstream", "upstream"):
        section = data.get(direction)
        if not isinstance(section, dict):
            continue

        # Section-level channel_type.map
        _add_channel_type_map(section.get("channel_type"), result)

        # Per-table channel_type.map
        tables = section.get("tables")
        if isinstance(tables, list):
            for table in tables:
                if isinstance(table, dict):
                    _add_channel_type_map(table.get("channel_type"), result)


def _add_channel_type_map(ct: object, result: set[str]) -> None:
    """Add channel type map keys to the result set."""
    if not isinstance(ct, dict):
        return
    ct_map = ct.get("map")
    if isinstance(ct_map, dict):
        for key in ct_map:
            if isinstance(key, str) and key.strip():
                result.add(key.strip())


# -----------------------------------------------------------------------
# Aggregates
# -----------------------------------------------------------------------


def _extract_aggregates(
    data: dict[str, object],
    result: list[tuple[str, str]],
    seen: set[tuple[str, str]],
) -> None:
    """Extract (source_field, aggregate_name) pairs from aggregate sections."""
    aggregate = data.get("aggregate")
    if not isinstance(aggregate, dict):
        return

    for agg_name, agg_def in aggregate.items():
        if not isinstance(agg_def, dict):
            continue
        source_field = agg_def.get("sum")
        if isinstance(source_field, str) and source_field.strip():
            pair = (source_field.strip(), agg_name.strip())
            if pair not in seen:
                seen.add(pair)
                result.append(pair)


# -----------------------------------------------------------------------
# JS function layouts
# -----------------------------------------------------------------------


def _extract_js_function_layouts(
    data: dict[str, object],
    result: dict[str, Any],
) -> None:
    """Extract JS function field layouts from javascript-format sections.

    Populates ``result`` with ``function_name → function_dict`` entries
    from the fleet's committed ``functions[]`` lists.  Only functions with
    a non-empty name and at least one field entry are stored.  First
    occurrence wins — the fleet is expected to be consistent across modems
    using the same firmware.
    """
    for direction in ("downstream", "upstream"):
        section = data.get(direction)
        if not isinstance(section, dict):
            continue
        if section.get("format") != "javascript":
            continue

        functions = section.get("functions")
        if not isinstance(functions, list):
            continue

        for func in functions:
            if not isinstance(func, dict):
                continue
            name = func.get("name", "")
            if not isinstance(name, str) or not name.strip():
                continue
            fields = func.get("fields")
            if not isinstance(fields, list) or not fields:
                continue
            if name not in result:
                result[name] = func


# -----------------------------------------------------------------------
# HNAP response layouts
# -----------------------------------------------------------------------


def _extract_hnap_response_layouts(
    data: dict[str, object],
    result: dict[str, Any],
) -> None:
    """Extract HNAP response_key → field layout from hnap-format sections.

    Populates ``result`` with ``response_key → layout`` entries from the
    fleet's committed ``fields[]`` lists.  Only stores entries with a
    non-empty name and at least one field.  First occurrence wins — the
    fleet is expected to be consistent across modems using the same
    firmware.
    """
    for direction in ("downstream", "upstream"):
        section = data.get(direction)
        if not isinstance(section, dict):
            continue
        if section.get("format") != "hnap":
            continue
        response_key = section.get("response_key", "")
        if not isinstance(response_key, str) or not response_key.strip():
            continue
        if response_key in result:
            continue
        fields = section.get("fields")
        if not isinstance(fields, list) or not fields:
            continue
        result[response_key] = {
            "data_key": section.get("data_key", ""),
            "record_delimiter": section.get("record_delimiter", "|+|"),
            "field_delimiter": section.get("field_delimiter", "^"),
            "fields": fields,
        }
