"""Metadata Enrichment Tool — MCP tool.

Bridges ``analyze_har`` and ``generate_config``. Infers metadata from
analysis output, merges with user-provided or existing config metadata,
and reports what was inferred, what's still missing, and any conflicts.

Three use cases:
1. **New onboarding** — analysis only, infer defaults.
2. **MVP review** — analysis + user_input, merge and report gaps.
3. **Status upgrade** — existing_config + new metadata, detect conflicts.

Per ONBOARDING_SPEC.md ``enrich_metadata`` tool contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EnrichMetadataResult:
    """Result of metadata enrichment.

    Attributes:
        metadata: Complete metadata dict ready for ``generate_config``.
        inferred: Field names filled by inference from analysis.
        missing: Field names still needed for the target status.
        warnings: Conflicts between existing and inferred values.
    """

    metadata: dict[str, Any] = field(default_factory=dict)
    inferred: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for MCP tool output."""
        return {
            "metadata": self.metadata,
            "inferred": self.inferred,
            "missing": self.missing,
            "warnings": self.warnings,
        }


def enrich_metadata(
    analysis: dict[str, Any],
    existing_config: dict[str, Any] | None = None,
    user_input: dict[str, Any] | None = None,
) -> EnrichMetadataResult:
    """Enrich metadata from analysis output and optional inputs.

    Priority order (highest wins): user_input > existing_config > inferred.

    Args:
        analysis: Dict from ``AnalysisResult.to_dict()`` — contains
            transport, sections, host information.
        existing_config: Existing modem.yaml dict (for status upgrades).
        user_input: Caller-provided overrides (manufacturer, model, etc.).

    Returns:
        ``EnrichMetadataResult`` with complete metadata and gap report.
    """
    result = EnrichMetadataResult()
    metadata: dict[str, Any] = {}

    # Start with existing config (base layer)
    if existing_config:
        metadata = _extract_metadata_from_config(existing_config)

    # Apply inferences from analysis
    _apply_inferences(metadata, analysis, result)

    # Apply user input (overrides everything)
    if user_input:
        _apply_user_input(metadata, user_input, result)

    # Detect conflicts between existing and inferred
    if existing_config:
        _detect_conflicts(metadata, existing_config, result)

    # Check for missing required fields
    _check_missing(metadata, result)

    result.metadata = metadata
    return result


def _extract_metadata_from_config(config: dict[str, Any]) -> dict[str, Any]:
    """Extract metadata fields from an existing modem.yaml config dict."""
    metadata: dict[str, Any] = {}
    # Direct metadata fields
    for key in (
        "manufacturer",
        "model",
        "model_aliases",
        "brands",
        "transport",
        "default_host",
        "hardware",
        "status",
        "sources",
        "attribution",
        "isps",
        "notes",
        "references",
        "gaps",
        "timeout",
    ):
        if key in config:
            metadata[key] = config[key]
    return metadata


def _apply_inferences(
    metadata: dict[str, Any],
    analysis: dict[str, Any],
    result: EnrichMetadataResult,
) -> None:
    """Infer metadata fields from analysis output.

    Only sets fields not already present in metadata.
    """
    # transport — from analysis
    if "transport" not in metadata:
        transport = analysis.get("transport", "")
        if transport:
            metadata["transport"] = transport
            result.inferred.append("transport")

    # default_host — from analysis request URLs
    if "default_host" not in metadata:
        host = analysis.get("default_host", "")
        if host:
            metadata["default_host"] = host
            result.inferred.append("default_host")
        else:
            metadata["default_host"] = "192.168.100.1"
            result.inferred.append("default_host")

    # hardware.docsis_version: OFDM/OFDMA channels mean 3.1, else 3.0;
    # no channel sections at all means no inference (stays in `missing`)
    if "hardware" not in metadata:
        metadata["hardware"] = {}
    hw = metadata["hardware"]
    if isinstance(hw, dict) and "docsis_version" not in hw:
        docsis = _infer_docsis_version(analysis)
        if docsis:
            hw["docsis_version"] = docsis
            result.inferred.append("hardware.docsis_version")

    # status — default to awaiting_verification for new modems
    if "status" not in metadata:
        metadata["status"] = "awaiting_verification"
        result.inferred.append("status")


_OFDM_TYPES: frozenset[str] = frozenset({"ofdm", "ofdma"})


def _infer_docsis_version(analysis: dict[str, Any]) -> str:
    """Infer DOCSIS version from channel types in analysis sections.

    OFDM or OFDMA channels → DOCSIS 3.1, otherwise 3.0.
    Checks both fixed types and mapped values — the presence of a
    channel_type field alone is not sufficient (a DOCSIS 3.0 modem
    can have a channel_type column with only QAM/ATDMA values).
    Never infers "4.0": D4.0 uses the same channel types as 3.1, so
    wire data can't distinguish them here — 3.1 is the conservative
    floor, and "4.0" is set by hand from hardware sources (see
    MODEM_YAML_SPEC § Hardware; first case: technicolor/xb10).
    """
    sections = analysis.get("sections") or {}
    # Absence of OFDM only means 3.0 when channels were observed; an
    # unprovisioned modem has no channel sections, and "3.0" would be
    # a guess. Empty string = cannot infer.
    if not any(sections.get(name) for name in ("downstream", "upstream")):
        return ""
    for section_name in ("downstream", "upstream"):
        section = sections.get(section_name) or {}
        channel_type = section.get("channel_type", {})
        if isinstance(channel_type, dict):
            fixed = channel_type.get("fixed", "")
            if fixed.lower() in _OFDM_TYPES:
                return "3.1"
            type_map = channel_type.get("map", {})
            if any(v.lower() in _OFDM_TYPES for v in type_map.values()):
                return "3.1"
    return "3.0"


def _apply_user_input(
    metadata: dict[str, Any],
    user_input: dict[str, Any],
    result: EnrichMetadataResult,
) -> None:
    """Apply user-provided overrides to metadata.

    User input always wins. Does not track as inferred.
    """
    for key, value in user_input.items():
        if key == "hardware" and isinstance(value, dict):
            # Merge hardware dict rather than replacing
            existing_hw = metadata.get("hardware", {})
            if isinstance(existing_hw, dict):
                existing_hw.update(value)
                metadata["hardware"] = existing_hw
            else:
                metadata["hardware"] = value
        else:
            metadata[key] = value


def _detect_conflicts(
    metadata: dict[str, Any],
    existing_config: dict[str, Any],
    result: EnrichMetadataResult,
) -> None:
    """Detect conflicts between enriched metadata and existing config."""
    for key in ("transport", "default_host", "status"):
        existing_val = existing_config.get(key)
        enriched_val = metadata.get(key)
        if existing_val and enriched_val and existing_val != enriched_val:
            result.warnings.append(f"existing {key} {existing_val!r} differs from enriched value {enriched_val!r}")


def _check_missing(
    metadata: dict[str, Any],
    result: EnrichMetadataResult,
) -> None:
    """Check for fields still missing based on target status.

    All statuses need: manufacturer, model, auth (from analysis).
    ``confirmed`` / ``awaiting_verification`` also needs:
    hardware (docsis_version, release_date), attribution, isps.
    """
    # Always required
    if not metadata.get("manufacturer"):
        result.missing.append("manufacturer")
    if not metadata.get("model"):
        result.missing.append("model")

    status = metadata.get("status", "awaiting_verification")
    if status in ("confirmed", "awaiting_verification"):
        hw = metadata.get("hardware") or {}
        if not isinstance(hw, dict) or not hw.get("docsis_version"):
            result.missing.append("hardware.docsis_version")
        # release_date feeds the catalog README timeline — entries
        # without it are silently omitted from that rendering
        if not isinstance(hw, dict) or not hw.get("release_date"):
            result.missing.append("hardware.release_date")
        if not metadata.get("attribution"):
            result.missing.append("attribution")
        if not metadata.get("isps"):
            result.missing.append("isps")
