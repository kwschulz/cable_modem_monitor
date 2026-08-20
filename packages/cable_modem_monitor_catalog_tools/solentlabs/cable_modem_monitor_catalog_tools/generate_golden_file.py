"""generate_golden_file MCP tool.

Reads HAR response bodies and uses the ModemParserCoordinator to extract
ModemData. The coordinator is the single extraction path — the same
engine used by the live pipeline and test harness.

See ONBOARDING_SPEC.md generate_golden_file section.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from solentlabs.cable_modem_monitor_core.config_loader import validate_parser_config
from solentlabs.cable_modem_monitor_core.har import build_resource_dict
from solentlabs.cable_modem_monitor_core.models.field_registry import SYSTEM_INFO_FIELDS
from solentlabs.cable_modem_monitor_core.parsers.coordinator import ModemParserCoordinator


@dataclass
class GenerateGoldenFileResult:
    """Result from generate_golden_file.

    Attributes:
        golden_file: The extracted ModemData dict.
        golden_file_json: Canonical JSON serialization (sort_keys=True).
            Write this string directly to modem.expected.json — never
            re-serialize the dict, which loses the ordering guarantee.
        channel_counts: Downstream and upstream channel counts.
        system_info_fields: Field names present in system_info.
        errors: Any errors encountered during extraction.
    """

    golden_file: dict[str, Any]
    golden_file_json: str = ""
    channel_counts: dict[str, int] = field(default_factory=dict)
    system_info_fields: list[str] = field(default_factory=list)
    missing_system_info_fields: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def generate_golden_file(
    har_path: str,
    parser_yaml_content: str | None,
) -> GenerateGoldenFileResult:
    """Generate a golden file from HAR response bodies.

    Loads the HAR, builds a resource dict from response bodies, and
    runs the coordinator to extract ModemData.

    Args:
        har_path: Path to the HAR file.
        parser_yaml_content: parser.yaml content as a YAML string.

    Returns:
        Result with golden_file dict, channel counts, and any errors.
    """
    errors: list[str] = []

    # generate_config returns parser_yaml None when analyze_har found no
    # data sections; name that state instead of crashing on the load.
    if not parser_yaml_content:
        return GenerateGoldenFileResult(
            golden_file={},
            errors=[
                "parser.yaml is empty: analyze_har found no data sections, so "
                "there is no parser to run. Recapture with the modem's data "
                "pages populated, or skip golden generation for an auth-only "
                "entry."
            ],
        )

    # Load and validate parser.yaml
    try:
        parser_config = _load_parser_yaml(parser_yaml_content)
    except Exception as e:
        return GenerateGoldenFileResult(
            golden_file={},
            errors=[f"Invalid parser.yaml: {e}"],
        )

    # Load HAR and build resource dict
    try:
        resources = build_resource_dict(har_path)
    except Exception as e:
        return GenerateGoldenFileResult(
            golden_file={},
            errors=[f"Failed to load HAR: {e}"],
        )

    if not resources:
        errors.append("No resources found in HAR")

    # Extract via coordinator (single extraction path).
    # Discard ParseDiagnostics — the golden-file generator surfaces only
    # the data dict; stub-page detection is a runtime concern owned by
    # the production collector.
    coordinator = ModemParserCoordinator(parser_config)
    golden, _ = coordinator.parse(resources)

    # Build result
    downstream = golden.get("downstream", [])
    upstream = golden.get("upstream", [])
    system_info = golden.get("system_info", {})

    channel_counts = {
        "downstream": len(downstream),
        "upstream": len(upstream),
    }

    system_info_fields = sorted(system_info.keys()) if system_info else []
    missing_system_info_fields = sorted(SYSTEM_INFO_FIELDS - set(system_info_fields))

    return GenerateGoldenFileResult(
        golden_file=golden,
        golden_file_json=json.dumps(golden, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        channel_counts=channel_counts,
        system_info_fields=system_info_fields,
        missing_system_info_fields=missing_system_info_fields,
        errors=errors,
    )


def _load_parser_yaml(content: str) -> Any:
    """Parse and validate parser.yaml content.

    Returns the validated ParserConfig model.
    """
    import yaml

    data = yaml.safe_load(content)
    if not isinstance(data, dict):
        raise ValueError("parser.yaml must be a YAML mapping")
    return validate_parser_config(data)
