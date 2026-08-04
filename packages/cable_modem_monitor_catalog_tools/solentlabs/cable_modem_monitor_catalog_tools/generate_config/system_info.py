"""System info transformation — analysis system_info to parser.yaml format.

Normalizes HNAP and JSON system_info structures from the analysis
pipeline into the format expected by the parser config model.

Per ONBOARDING_SPEC.md ``generate_config`` tool contract.
"""

from __future__ import annotations

from typing import Any

from .mappings import normalize_type


def transform_system_info(system_info: dict[str, Any]) -> dict[str, Any]:
    """Transform analysis system_info into parser.yaml format.

    The analysis pipeline produces system_info with format-specific
    structures that don't match the parser config model exactly.
    This normalizes them.
    """
    sources = system_info.get("sources", [])
    transformed_sources = []

    for source in sources:
        fmt = source.get("format", "")
        if fmt == "hnap":
            transformed_sources.append(_transform_hnap_system_info(source))
        elif fmt == "json":
            transformed_sources.append(_transform_json_system_info(source))
        else:
            # html_fields, javascript — pass through
            transformed_sources.append(source)

    return {"sources": transformed_sources}


def _transform_hnap_system_info(source: dict[str, Any]) -> dict[str, Any]:
    """Transform HNAP system_info source.

    Analysis produces fields as a dict ``{hnap_key: canonical_field}``.
    Model expects a list of ``{source, field, type}`` dicts.
    """
    result: dict[str, Any] = {
        "format": "hnap",
        "response_key": source.get("response_key", ""),
    }

    fields = source.get("fields", {})
    if isinstance(fields, dict):
        result["fields"] = [
            {"source": hnap_key, "field": canonical, "type": "string"} for hnap_key, canonical in fields.items()
        ]
    else:
        result["fields"] = fields

    return result


def _transform_json_system_info(source: dict[str, Any]) -> dict[str, Any]:
    """Transform JSON system_info source.

    Analysis may produce field mappings with ``source`` key.
    Model expects ``key`` instead.
    """
    result: dict[str, Any] = {
        "format": "json",
        "resource": source.get("resource", ""),
    }
    if source.get("encoding"):
        result["encoding"] = source["encoding"]

    fields = source.get("fields", [])
    transformed_fields = []
    for f in fields:
        tf: dict[str, Any] = {
            "key": f.get("key") or f.get("source", ""),
            "field": f["field"],
            "type": normalize_type(f.get("type", "string")),
        }
        if f.get("path"):
            tf["path"] = f["path"]
        if f.get("format"):
            tf["format"] = f["format"]
        if f.get("map"):
            tf["map"] = f["map"]
        transformed_fields.append(tf)

    result["fields"] = transformed_fields

    # The analysis layer already emits the model's key names for these,
    # so only the type needs normalizing.
    if source.get("child_aggregates"):
        result["child_aggregates"] = [
            {**agg, "type": normalize_type(agg.get("type", "integer"))} for agg in source["child_aggregates"]
        ]

    return result
