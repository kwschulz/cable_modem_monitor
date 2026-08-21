"""Tests for the enrich_metadata MCP tool.

Three use cases:
1. New onboarding — analysis only, infers defaults
2. MVP review — analysis + user_input, merges and reports gaps
3. Status upgrade — existing_config + new metadata, detects conflicts

Plus contract tests that guard against silent data corruption:
- Round-trip preservation: existing config fields survive enrichment
- enrich → generate contract: metadata is self-contained for generate_config
"""

from __future__ import annotations

from typing import Any

import pytest
from solentlabs.cable_modem_monitor_catalog_tools.enrich_metadata import enrich_metadata
from solentlabs.cable_modem_monitor_catalog_tools.generate_config import generate_config

# ---------------------------------------------------------------------------
# Minimal analysis dicts for test cases
# ---------------------------------------------------------------------------


def _minimal_analysis(
    *,
    transport: str = "http",
    default_host: str = "",
    has_ofdm: bool = False,
) -> dict[str, Any]:
    """Build a minimal analysis dict for testing."""
    sections: dict[str, Any] = {
        "downstream": {
            "format": "table",
            "resource": "/status.html",
            "channel_type": {"fixed": "ofdm" if has_ofdm else "qam"},
            "mappings": [],
        },
    }
    result: dict[str, Any] = {
        "transport": transport,
        "sections": sections,
    }
    if default_host:
        result["default_host"] = default_host
    return result


# ---------------------------------------------------------------------------
# Use case 1: New onboarding — analysis only
# ---------------------------------------------------------------------------


class TestNewOnboarding:
    """New modem with only analysis available."""

    def test_infers_default_host(self) -> None:
        """Infers default_host from analysis."""
        analysis = _minimal_analysis(default_host="10.0.0.1")
        result = enrich_metadata(analysis)

        assert result.metadata["default_host"] == "10.0.0.1"
        assert "default_host" in result.inferred

    def test_default_host_fallback(self) -> None:
        """Falls back to 192.168.100.1 when analysis has no host."""
        analysis = _minimal_analysis()
        result = enrich_metadata(analysis)

        assert result.metadata["default_host"] == "192.168.100.1"
        assert "default_host" in result.inferred

    def test_infers_docsis_31_from_ofdm(self) -> None:
        """OFDM channels → DOCSIS 3.1."""
        analysis = _minimal_analysis(has_ofdm=True)
        result = enrich_metadata(analysis)

        assert result.metadata["hardware"]["docsis_version"] == "3.1"
        assert "hardware.docsis_version" in result.inferred

    def test_infers_docsis_30_without_ofdm(self) -> None:
        """QAM-only channels → DOCSIS 3.0."""
        analysis = _minimal_analysis(has_ofdm=False)
        result = enrich_metadata(analysis)

        assert result.metadata["hardware"]["docsis_version"] == "3.0"
        assert "hardware.docsis_version" in result.inferred

    def test_docsis_not_inferred_without_channels(self) -> None:
        """No channel sections at all: no inference, reported missing.

        Absence of OFDM only means 3.0 when channels were observed; an
        unprovisioned modem has none, and "3.0" would be a guess.
        """
        analysis = _minimal_analysis()
        analysis["sections"] = None
        result = enrich_metadata(analysis)

        assert "docsis_version" not in result.metadata.get("hardware", {})
        assert "hardware.docsis_version" not in result.inferred
        assert "hardware.docsis_version" in result.missing

    def test_infers_status_awaiting_verification(self) -> None:
        """New modem defaults to awaiting_verification status."""
        analysis = _minimal_analysis()
        result = enrich_metadata(analysis)

        assert result.metadata["status"] == "awaiting_verification"
        assert "status" in result.inferred

    def test_missing_manufacturer_model(self) -> None:
        """Reports manufacturer and model as missing."""
        analysis = _minimal_analysis()
        result = enrich_metadata(analysis)

        assert "manufacturer" in result.missing
        assert "model" in result.missing

    def test_to_dict_serialization(self) -> None:
        """Result serializes to expected dict structure."""
        analysis = _minimal_analysis()
        result = enrich_metadata(analysis)
        d = result.to_dict()

        assert "metadata" in d
        assert "inferred" in d
        assert "missing" in d
        assert "warnings" in d


# ---------------------------------------------------------------------------
# Use case 2: MVP review — analysis + user_input
# ---------------------------------------------------------------------------


class TestMVPReview:
    """Analysis + partial user input."""

    def test_user_input_fills_identity(self) -> None:
        """User provides manufacturer and model — no longer missing."""
        analysis = _minimal_analysis()
        user_input = {"manufacturer": "Solent Labs", "model": "T100"}
        result = enrich_metadata(analysis, user_input=user_input)

        assert result.metadata["manufacturer"] == "Solent Labs"
        assert result.metadata["model"] == "T100"
        assert "manufacturer" not in result.missing
        assert "model" not in result.missing

    def test_user_input_overrides_inferred(self) -> None:
        """User-provided default_host overrides inferred value."""
        analysis = _minimal_analysis(default_host="10.0.0.1")
        user_input = {"default_host": "192.168.0.1"}
        result = enrich_metadata(analysis, user_input=user_input)

        assert result.metadata["default_host"] == "192.168.0.1"

    def test_user_hardware_merges(self) -> None:
        """User hardware dict merges with inferred hardware."""
        analysis = _minimal_analysis(has_ofdm=True)
        user_input = {"hardware": {"chipset": "BCM33XX"}}
        result = enrich_metadata(analysis, user_input=user_input)

        hw = result.metadata["hardware"]
        assert hw["docsis_version"] == "3.1"
        assert hw["chipset"] == "BCM33XX"

    def test_verified_status_reports_missing(self) -> None:
        """Verified status reports attribution, isps, and release_date as missing."""
        analysis = _minimal_analysis()
        user_input = {
            "manufacturer": "Solent Labs",
            "model": "T100",
            "status": "confirmed",
        }
        result = enrich_metadata(analysis, user_input=user_input)

        assert "attribution" in result.missing
        assert "isps" in result.missing
        assert "hardware.release_date" in result.missing

    def test_release_date_present_not_missing(self) -> None:
        """A provided release_date is not reported missing."""
        analysis = _minimal_analysis()
        user_input = {
            "manufacturer": "Solent Labs",
            "model": "T100",
            "status": "confirmed",
            "hardware": {"release_date": "2024"},
        }
        result = enrich_metadata(analysis, user_input=user_input)

        assert "hardware.release_date" not in result.missing


# ---------------------------------------------------------------------------
# Use case 3: Status upgrade — existing_config + new metadata
# ---------------------------------------------------------------------------


class TestStatusUpgrade:
    """Existing modem config with new metadata for upgrade."""

    def test_existing_config_preserved(self) -> None:
        """Existing config fields carry over."""
        analysis = _minimal_analysis()
        existing = {
            "manufacturer": "Solent Labs",
            "model": "T100",
            "default_host": "192.168.100.1",
            "hardware": {"docsis_version": "3.0"},
            "status": "awaiting_verification",
        }
        result = enrich_metadata(analysis, existing_config=existing)

        assert result.metadata["manufacturer"] == "Solent Labs"
        assert result.metadata["model"] == "T100"
        assert result.metadata["hardware"]["docsis_version"] == "3.0"

    def test_conflict_detected(self) -> None:
        """Warns when existing default_host differs from inferred."""
        analysis = _minimal_analysis(default_host="10.0.0.1")
        existing = {
            "manufacturer": "Solent Labs",
            "model": "T100",
            "default_host": "192.168.100.1",
            "status": "awaiting_verification",
        }
        # User overrides to use the analysis host
        user_input = {"default_host": "10.0.0.1"}
        result = enrich_metadata(
            analysis,
            existing_config=existing,
            user_input=user_input,
        )

        assert any("default_host" in w for w in result.warnings)

    def test_no_conflict_when_same(self) -> None:
        """No warning when existing and enriched values match."""
        analysis = _minimal_analysis(default_host="192.168.100.1")
        existing = {
            "manufacturer": "Solent Labs",
            "model": "T100",
            "default_host": "192.168.100.1",
            "status": "awaiting_verification",
        }
        result = enrich_metadata(analysis, existing_config=existing)

        host_warnings = [w for w in result.warnings if "default_host" in w]
        assert len(host_warnings) == 0

    def test_existing_docsis_not_overridden(self) -> None:
        """Existing docsis_version is not overridden by inference."""
        analysis = _minimal_analysis(has_ofdm=True)
        existing = {
            "manufacturer": "Solent Labs",
            "model": "T100",
            "default_host": "192.168.100.1",
            "hardware": {"docsis_version": "3.0"},
            "status": "awaiting_verification",
        }
        result = enrich_metadata(analysis, existing_config=existing)

        # Existing 3.0 preserved even though analysis has OFDM
        assert result.metadata["hardware"]["docsis_version"] == "3.0"
        assert "hardware.docsis_version" not in result.inferred


# ---------------------------------------------------------------------------
# DOCSIS inference edge cases
# ---------------------------------------------------------------------------


class TestDocsisInference:
    """Edge cases for DOCSIS version inference."""

    def test_channel_type_field_without_ofdm(self) -> None:
        """channel_type field in mappings but QAM-only → 3.0."""
        analysis: dict[str, Any] = {
            "transport": "http",
            "sections": {
                "downstream": {
                    "format": "table",
                    "channel_type": {"fixed": "qam"},
                    "mappings": [
                        {"field": "channel_type", "type": "string"},
                        {"field": "frequency", "type": "integer"},
                    ],
                },
            },
        }
        result = enrich_metadata(analysis)
        assert result.metadata["hardware"]["docsis_version"] == "3.0"

    def test_channel_type_map_with_ofdm(self) -> None:
        """channel_type map containing OFDM values → 3.1."""
        analysis: dict[str, Any] = {
            "transport": "http",
            "sections": {
                "downstream": {
                    "format": "table",
                    "channel_type": {
                        "field": "modulation",
                        "map": {"QAM256": "qam", "OFDM PLC": "ofdm"},
                    },
                    "mappings": [],
                },
            },
        }
        result = enrich_metadata(analysis)
        assert result.metadata["hardware"]["docsis_version"] == "3.1"

    def test_ofdma_upstream(self) -> None:
        """OFDMA upstream → 3.1."""
        analysis: dict[str, Any] = {
            "transport": "http",
            "sections": {
                "downstream": {"format": "table", "channel_type": {"fixed": "qam"}, "mappings": []},
                "upstream": {"format": "table", "channel_type": {"fixed": "ofdma"}, "mappings": []},
            },
        }
        result = enrich_metadata(analysis)
        assert result.metadata["hardware"]["docsis_version"] == "3.1"

    def test_no_sections(self) -> None:
        """No sections → no inference; the field is reported missing."""
        analysis: dict[str, Any] = {"transport": "http"}
        result = enrich_metadata(analysis)
        assert "docsis_version" not in result.metadata.get("hardware", {})
        assert "hardware.docsis_version" in result.missing


# ---------------------------------------------------------------------------
# Transport flow — metadata must be self-contained
# ---------------------------------------------------------------------------


class TestTransportFlow:
    """Transport must flow through metadata for generate_config."""

    def test_transport_inferred_from_analysis(self) -> None:
        """Transport is inferred from analysis into metadata."""
        analysis = _minimal_analysis(transport="hnap")
        result = enrich_metadata(analysis)

        assert result.metadata["transport"] == "hnap"
        assert "transport" in result.inferred

    def test_transport_preserved_from_existing_config(self) -> None:
        """Existing config transport is not overridden by inference."""
        analysis = _minimal_analysis(transport="http")
        existing = {
            "manufacturer": "Solent Labs",
            "model": "T100",
            "transport": "hnap",
            "default_host": "192.168.100.1",
            "status": "awaiting_verification",
        }
        result = enrich_metadata(analysis, existing_config=existing)

        assert result.metadata["transport"] == "hnap"
        assert "transport" not in result.inferred

    def test_transport_conflict_detected(self) -> None:
        """Warns when user overrides transport to differ from existing."""
        analysis = _minimal_analysis(transport="http")
        existing = {
            "manufacturer": "Solent Labs",
            "model": "T100",
            "transport": "hnap",
            "default_host": "192.168.100.1",
            "status": "awaiting_verification",
        }
        user_input = {"transport": "http"}
        result = enrich_metadata(
            analysis,
            existing_config=existing,
            user_input=user_input,
        )

        transport_warnings = [w for w in result.warnings if "transport" in w]
        assert len(transport_warnings) == 1
        assert result.metadata["transport"] == "http"

    def test_empty_analysis_with_existing_config(self) -> None:
        """Status upgrade with empty analysis preserves existing transport."""
        existing = {
            "manufacturer": "Solent Labs",
            "model": "T100",
            "transport": "hnap",
            "default_host": "192.168.100.1",
            "status": "awaiting_verification",
        }
        result = enrich_metadata(analysis={}, existing_config=existing)

        assert result.metadata["transport"] == "hnap"


# ---------------------------------------------------------------------------
# Contract test 1: Round-trip preservation
#
# Every field in a complete existing config must survive enrichment
# unchanged. If _extract_metadata_from_config drops a field, this test
# breaks — preventing silent data loss on status upgrades.
# ---------------------------------------------------------------------------

# A complete modem.yaml config with every metadata field populated.
# This is the "known good" input — enrichment must preserve all of it.
_COMPLETE_CONFIG: dict[str, Any] = {
    "manufacturer": "Solent Labs",
    "model": "T100",
    "model_aliases": ["T100v2"],
    "brands": ["SolentLabs"],
    "transport": "hnap",
    "default_host": "10.0.0.1",
    "hardware": {"docsis_version": "3.1", "chipset": "BCM33XX"},
    "status": "awaiting_verification",
    "sources": {"issue": "#42"},
    "attribution": {"contributors": [{"github": "tester", "contribution": "HAR"}]},
    "isps": ["Comcast", "Spectrum"],
    "notes": "Dual-stack IPv6 supported",
    "references": {"documentation": "https://example.com/t100"},
    "timeout": 15,
}

# Every field that generate_config reads from the metadata dict.
# If generate_config._add_identity or _add_metadata_fields starts reading
# a new field from metadata, add it here — the test will catch the gap.
#
# fmt: off
_METADATA_FIELDS_FOR_GENERATE_CONFIG: list[str] = [
    # _add_identity
    "manufacturer",
    "model",
    "model_aliases",
    "brands",
    "transport",
    "default_host",
    # _add_metadata_fields
    "hardware",
    "timeout",
    "status",
    "sources",
    "attribution",
    "isps",
    "notes",
    "references",
]
# fmt: on


class TestRoundTripPreservation:
    """Existing config fields must survive enrichment unchanged.

    Guards against _extract_metadata_from_config silently dropping fields.
    """

    def test_all_fields_preserved(self) -> None:
        """Every field in a complete config survives enrichment."""
        # Empty analysis — pure status upgrade, no inferences should override
        result = enrich_metadata(analysis={}, existing_config=_COMPLETE_CONFIG)

        for key, expected in _COMPLETE_CONFIG.items():
            assert key in result.metadata, f"Field {key!r} dropped by enrichment"
            assert (
                result.metadata[key] == expected
            ), f"Field {key!r} changed: expected {expected!r}, got {result.metadata[key]!r}"

    def test_extraction_covers_generate_config_fields(self) -> None:
        """_extract_metadata_from_config extracts every field generate_config reads."""
        result = enrich_metadata(analysis={}, existing_config=_COMPLETE_CONFIG)

        for field in _METADATA_FIELDS_FOR_GENERATE_CONFIG:
            assert field in result.metadata, (
                f"Field {field!r} is read by generate_config but not "
                f"extracted by enrich_metadata — silent default will occur"
            )


# ---------------------------------------------------------------------------
# Contract test 2: enrich_metadata → generate_config end-to-end
#
# Runs both tools in sequence and verifies the output modem dict has the
# correct values — not defaults. If generate_config silently falls back
# to a default for a field that enrich_metadata should have provided,
# this test catches it.
#
# Table-driven: each row is a scenario with inputs and expected YAML
# substrings that MUST appear (and optionally substrings that must NOT).
# ---------------------------------------------------------------------------

# ┌──────────────────┬──────────┬────────────┬──────────────┬──────────────┐
# │ scenario         │ analysis │ existing   │ must_contain │ must_not     │
# ├──────────────────┼──────────┼────────────┼──────────────┼──────────────┤
# │ new onboarding   │ hnap     │ —          │ hnap, host   │ http         │
# │ status upgrade   │ empty    │ hnap modem │ hnap         │ http         │
# │ complete config  │ empty    │ full       │ all fields   │ —            │
# └──────────────────┴──────────┴────────────┴──────────────┴──────────────┘

_EnrichGenerateCase = tuple[
    str,  # description
    dict[str, Any],  # analysis
    dict[str, Any] | None,  # existing_config
    dict[str, Any] | None,  # user_input
    list[str],  # must_contain
    list[str],  # must_not_contain
]

# fmt: off
_ENRICH_GENERATE_CASES: list[_EnrichGenerateCase] = [
    # (description, analysis, existing_config, user_input, must_contain, must_not_contain)
    (
        "new onboarding — identity fields not defaulted",
        _minimal_analysis(transport="hnap", default_host="10.0.0.1"),
        None,
        {"manufacturer": "Solent Labs", "model": "T100"},
        ["manufacturer: Solent Labs", "model: T100", "transport: hnap", "default_host: 10.0.0.1"],
        ["transport: http"],
    ),
    (
        "status upgrade — HNAP transport preserved",
        {},
        {
            "manufacturer": "Solent Labs", "model": "T100", "transport": "hnap",
            "default_host": "192.168.100.1", "status": "awaiting_verification",
            "hardware": {"docsis_version": "3.1"},
        },
        {
            "status": "confirmed",
            "attribution": {"contributors": [{"github": "t", "contribution": "t"}]},
            "isps": ["ISP"],
        },
        ["transport: hnap"],
        ["transport: http"],
    ),
    (
        "complete config — all metadata fields flow through",
        {},
        _COMPLETE_CONFIG,
        None,
        [
            "manufacturer: Solent Labs", "model: T100", "transport: hnap",
            "default_host: 10.0.0.1", "docsis_version: '3.1'", "timeout: 15",
            "Comcast",
        ],
        [],
    ),
]
# fmt: on


@pytest.mark.parametrize(
    "desc,analysis,existing,user_input,must_contain,must_not_contain",
    _ENRICH_GENERATE_CASES,
    ids=[c[0] for c in _ENRICH_GENERATE_CASES],
)
def test_enrich_to_generate_contract(
    desc: str,
    analysis: dict[str, Any],
    existing: dict[str, Any] | None,
    user_input: dict[str, Any] | None,
    must_contain: list[str],
    must_not_contain: list[str],
) -> None:
    """enrich → generate: output YAML has correct values, not defaults."""
    enriched = enrich_metadata(analysis, existing_config=existing, user_input=user_input)
    result = generate_config(analysis=analysis, metadata=enriched.metadata)

    for expected in must_contain:
        assert expected in result.modem_yaml, f"{desc}: expected {expected!r} in modem_yaml"
    for forbidden in must_not_contain:
        assert forbidden not in result.modem_yaml, f"{desc}: {forbidden!r} should not be in modem_yaml"
