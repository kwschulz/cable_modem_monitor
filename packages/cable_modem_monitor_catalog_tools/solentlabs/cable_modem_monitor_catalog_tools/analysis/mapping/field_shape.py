"""Phase 6 - system_info field type, format, and map.

Two Tier-1 fields are normalized rather than passed through, and the
rule is the same whichever format detected them. It lives here so the
html_fields, json, and hnap detectors share one implementation.

Per PARSING_SPEC.md Uptime Normalization and SYSTEM_INFO_SPEC.md
Canonical values.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..types import FleetPatterns
from .docsis_status import detect_docsis_status_map
from .uptime_format import detect_uptime_format


@dataclass(frozen=True)
class FieldShapeVocabulary:
    """Fleet-derived vocabularies that decide a field's type, format, and map."""

    uptime_formats: Sequence[str] = ()
    docsis_status_success_values: frozenset[str] = frozenset()

    @classmethod
    def from_fleet(cls, fleet: FleetPatterns | None) -> FieldShapeVocabulary:
        """Build from fleet patterns, or empty when the fleet is unavailable."""
        if fleet is None:
            return cls()
        return cls(
            uptime_formats=list(fleet.uptime_formats),
            docsis_status_success_values=frozenset(fleet.docsis_status_success_values),
        )


def field_shape(
    field_name: str,
    raw_value: object,
    vocabulary: FieldShapeVocabulary,
) -> tuple[str, str, dict[str, str]]:
    """Return the (type, format, map) a detected system_info field should carry.

    system_uptime declares type uptime plus the format naming its raw
    shape. docsis_status maps vendor success spellings to the canonical
    "Operational". Both fall back to an unadorned string when the
    observed value is not recognized, since a format that parses nothing
    or a map that matches nothing would produce no value and no error.
    """
    if field_name == "system_uptime":
        fmt = detect_uptime_format(raw_value, vocabulary.uptime_formats)
        return ("uptime", fmt, {}) if fmt else ("string", "", {})

    if field_name == "docsis_status":
        return "string", "", detect_docsis_status_map(raw_value, vocabulary.docsis_status_success_values)

    return "string", "", {}
