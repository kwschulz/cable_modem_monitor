"""Phase 6 result types for field mapping and system info.

Dataclasses for section configuration, field mappings, and
system_info detection output. Used by the mapping dispatcher,
system_info, and the format dispatcher.
"""

from __future__ import annotations

from dataclasses import (
    dataclass,
    field as dataclass_field,
)
from typing import Any


@dataclass
class FieldMapping:
    """A single field mapping from source position to canonical name.

    The locator key varies by format: ``index`` (table column),
    ``offset`` (javascript/hnap), ``key`` (json), ``label``
    (table_transposed row label).

    ``map`` carries optional value-canonicalization (raw → canonical
    form) emitted when the analysis layer detects non-canonical
    observed values for this field — e.g., modulation values like
    ``256QAM`` that need rewriting to ``QAM256`` per PARSING_SPEC.
    """

    field: str
    type: str
    tier: int = 3
    unit: str = ""
    index: int | None = None
    offset: int | None = None
    key: str = ""
    label: str = ""
    map: dict[str, str] = dataclass_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the sections output format."""
        result: dict[str, Any] = {"field": self.field, "type": self.type}
        if self.unit:
            result["unit"] = self.unit
        if self.index is not None:
            result["index"] = self.index
        if self.offset is not None:
            result["offset"] = self.offset
        if self.key:
            result["key"] = self.key
        if self.label:
            result["label"] = self.label
        if self.map:
            result["map"] = self.map
        return result

    @classmethod
    def find_by(cls, mappings: list[FieldMapping], field_name: str) -> FieldMapping | None:
        """Find a mapping by canonical field name."""
        for m in mappings:
            if m.field == field_name:
                return m
        return None


@dataclass
class SectionDetail:
    """Detected configuration for a single data section (DS/US).

    Format-agnostic ``mappings`` output.
    ``generate_config`` transforms this into parser.yaml.
    """

    format: str
    resource: str
    mappings: list[FieldMapping]
    selector: dict[str, str] = dataclass_field(default_factory=dict)
    row_start: int = 0
    channel_type: dict[str, Any] | None = None
    filter: dict[str, Any] | None = None
    channel_count: int = 0
    function_name: str = ""
    delimiter: str = ""
    fields_per_record: int = 0
    array_path: str = ""
    variable: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the sections output format."""
        result: dict[str, Any] = {
            "format": self.format,
            "resource": self.resource,
            "mappings": [m.to_dict() for m in self.mappings],
        }
        # Optional fields — only emitted when non-default.
        _optional: list[tuple[str, Any]] = [
            ("selector", self.selector),
            ("row_start", self.row_start),
            ("channel_type", self.channel_type),
            ("filter", self.filter),
            ("channel_count", self.channel_count),
            ("function_name", self.function_name),
            ("delimiter", self.delimiter),
            ("fields_per_record", self.fields_per_record),
            ("array_path", self.array_path),
            ("variable", self.variable),
        ]
        for key, value in _optional:
            if value:
                result[key] = value
        return result


@dataclass
class SystemInfoFieldDetail:
    """A detected system_info field."""

    field: str
    type: str = "string"
    selector_type: str = ""  # "label", "id", or "css_pattern"
    selector_value: str = ""
    source: str = ""  # HNAP/JSON source key
    pattern: str = ""
    # Container to navigate before the key lookup. Core resolves key
    # and path separately, so a nested field must split them rather
    # than carry one dotted string.
    path: str = ""
    # Raw input shape for types that require one, e.g. uptime.
    format: str = ""
    # Value normalization, e.g. a vendor docsis_status spelling to the
    # canonical "Operational".
    map: dict[str, str] = dataclass_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the sections output format."""
        result: dict[str, Any] = {"field": self.field, "type": self.type}
        if self.format:
            result["format"] = self.format
        if self.selector_type == "label":
            result["label"] = self.selector_value
        elif self.selector_type == "id":
            result["id"] = self.selector_value
        elif self.selector_type == "css_pattern":
            result["css"] = self.selector_value
        elif self.source:
            result["source"] = self.source
        if self.path:
            result["path"] = self.path
        if self.pattern:
            result["pattern"] = self.pattern
        if self.map:
            result["map"] = self.map
        return result


@dataclass
class ChildAggregateDetail:
    """A detected reduction over repeated items in a JSON array.

    ``filter`` values are strings by contract: Core normalizes these
    rules to text before comparing, so a numeric rule reads correctly
    and matches nothing. ``map`` carries wire spellings of the filter
    keys, keeping firmware vocabulary out of Core.
    """

    array_path: str
    filter: dict[str, str]
    max: str
    field: str
    type: str
    item_path: str = ""
    map: dict[str, str] = dataclass_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the sections output format."""
        result: dict[str, Any] = {"array_path": self.array_path}
        if self.item_path:
            result["item_path"] = self.item_path
        result["filter"] = self.filter
        if self.map:
            result["map"] = self.map
        result["max"] = self.max
        result["field"] = self.field
        result["type"] = self.type
        return result


@dataclass
class SystemInfoSourceDetail:
    """A detected system_info source page."""

    format: str
    resource: str
    fields: list[SystemInfoFieldDetail]
    response_key: str = ""
    child_aggregates: list[ChildAggregateDetail] = dataclass_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the sections output format."""
        result: dict[str, Any] = {
            "format": self.format,
            "resource": self.resource,
            "fields": [f.to_dict() for f in self.fields],
        }
        if self.response_key:
            result["response_key"] = self.response_key
        if self.child_aggregates:
            result["child_aggregates"] = [a.to_dict() for a in self.child_aggregates]
        return result


@dataclass
class SystemInfoDetail:
    """Detected system_info configuration with multi-source support."""

    sources: list[SystemInfoSourceDetail]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the sections output format."""
        return {"sources": [s.to_dict() for s in self.sources]}
