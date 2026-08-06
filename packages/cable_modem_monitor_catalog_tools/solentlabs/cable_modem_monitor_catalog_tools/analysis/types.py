"""Pipeline-wide types: gap detection and fleet enrichment.

``CoreGap`` is the shared type used across all phases when the pipeline
encounters a pattern that Core does not yet support. Gaps halt the
intake process and provide wire evidence for a development effort.

``FleetPatterns`` is the extension point for catalog-level enrichment.
Core defines the shape; Catalog populates it by scanning the fleet's
``parser.yaml`` files.

Phase-local result types live with their phase:
- Phase 2 auth: ``auth/types.py``
- Phase 4 actions: ``actions/types.py``
- Phase 5 format: ``format/types.py``
- Phase 6 mapping: ``mapping/types.py``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# -----------------------------------------------------------------------
# Fleet enrichment extension point
# -----------------------------------------------------------------------


@dataclass
class FleetPatterns:
    """Extension point for catalog-level fleet enrichment.

    Core defines this contract. Catalog populates it by scanning all
    ``parser.yaml`` files in the modem fleet. Core's analyzer accepts
    it optionally — without it, baseline hardcoded maps apply. With it,
    fleet-derived patterns augment detection.

    Attributes:
        selector_directions: Normalized selector/title text mapped to
            ``"downstream"`` or ``"upstream"``. Built from the fleet's
            ``downstream.selector.match`` and ``upstream.selector.match``
            values.
        system_info_labels: Normalized label text mapped to
            ``(canonical_field, tier)``. Built from all
            ``system_info.sources[].fields[].label`` entries across
            the fleet.
        system_info_ids: Normalized element IDs mapped to
            ``(canonical_field, tier)``. Built from CSS ``id``-based
            selectors in the fleet's system_info sources.
        system_info_json_keys: Normalized JSON keys mapped to
            ``(canonical_field, tier)``. Built from ``key`` fields in
            JSON-format system_info sources across the fleet.
        delimiters: Record/value delimiters observed in the fleet's
            HNAP and JavaScript parser configs.
        channel_type_values: Modulation/channel type strings observed
            in the fleet's ``channel_type.map`` values.
        aggregate_fields: ``(source_field, aggregate_name)`` pairs
            observed in the fleet's ``aggregate`` sections.
        js_function_layouts: JavaScript function name mapped to the
            committed field layout (offsets, types, units). First
            occurrence per function name wins.
        hnap_response_layouts: HNAP response key mapped to the
            committed field layout (positions, types). First occurrence
            per response key wins.
        uptime_formats: ``format`` strings observed on the fleet's
            ``type: uptime`` fields, longest first. Candidates for
            matching a newly observed raw uptime value.
        docsis_status_success_values: Lowercased raw spellings the fleet
            maps to the canonical ``"Operational"``. Error and
            in-progress states are deliberately excluded, per
            SYSTEM_INFO_SPEC Diagnostic Pass-Through.
    """

    selector_directions: dict[str, str] = field(default_factory=dict)
    system_info_labels: dict[str, tuple[str, int]] = field(default_factory=dict)
    system_info_ids: dict[str, tuple[str, int]] = field(default_factory=dict)
    system_info_json_keys: dict[str, tuple[str, int]] = field(default_factory=dict)
    delimiters: set[str] = field(default_factory=set)
    channel_type_values: set[str] = field(default_factory=set)
    aggregate_fields: list[tuple[str, str]] = field(default_factory=list)
    js_function_layouts: dict[str, dict[str, Any]] = field(default_factory=dict)
    hnap_response_layouts: dict[str, dict[str, Any]] = field(default_factory=dict)
    uptime_formats: list[str] = field(default_factory=list)
    docsis_status_success_values: set[str] = field(default_factory=set)


# -----------------------------------------------------------------------
# Pipeline-wide: Core gap detection
# -----------------------------------------------------------------------


@dataclass
class CoreGap:
    """A pattern the pipeline detected but Core cannot yet handle.

    When the intake pipeline encounters an auth mechanism, action
    endpoint, session pattern, or data format that doesn't match any
    known Core pattern, it creates a ``CoreGap`` with structured wire
    evidence. This halts config generation and provides enough detail
    to file a development issue or extend Core's pattern set.

    Attributes:
        phase: Pipeline phase that produced this gap.
        category: Machine-readable key (e.g., ``unmatched_login``).
        summary: Human-readable one-liner describing the gap.
        evidence: Structured wire data from the HAR for diagnosis.
    """

    phase: str
    category: str
    summary: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for MCP tool output."""
        return {
            "phase": self.phase,
            "category": self.category,
            "summary": self.summary,
            "evidence": self.evidence,
        }
