"""HAR Validation Gate — MCP tool.

Orchestrates four-step validation on a HAR file before analysis proceeds:
1. Structural validation (valid JSON, non-empty entries, request/response)
2. Auth flow validation (first request check, session cookie check,
   login redirect lands on a captured page)
3. Protocol signal scanning (transport hints, auth hints)
4. Response integrity (bodies stripped/replaced after capture)

Per ONBOARDING_SPEC.md HAR Validation Gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .validation.auth_flow import validate_auth_flow, validate_auth_redirect_landing
from .validation.har_utils import HARD_STOP_PREFIX
from .validation.protocol_signals import identify_transport_and_auth
from .validation.response_integrity import validate_response_integrity
from .validation.structural import validate_structure


@dataclass
class ValidationResult:
    """Result of HAR validation."""

    valid: bool
    issues: list[str] = field(default_factory=list)
    auth_flow_detected: bool = False
    transport_hints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for MCP tool output."""
        return {
            "valid": self.valid,
            "issues": self.issues,
            "auth_flow_detected": self.auth_flow_detected,
            "transport_hints": self.transport_hints,
        }


def validate_har(har_path: str | Path) -> ValidationResult:
    """Run the HAR Validation Gate.

    Args:
        har_path: Path to a .har file.

    Returns:
        ValidationResult with pass/fail, issues, and transport hints.
    """
    har_path = Path(har_path)
    issues: list[str] = []
    transport_hints: list[str] = []

    # Step 1: Structural validation
    har_data = validate_structure(har_path, issues)
    if har_data is None:
        return ValidationResult(valid=False, issues=issues)

    entries = har_data["log"]["entries"]

    # Step 2: Auth flow validation
    auth_flow_detected = validate_auth_flow(entries, issues)
    validate_auth_redirect_landing(entries, issues)

    # Step 3: Protocol signal scanning
    identify_transport_and_auth(entries, issues, transport_hints)

    # Step 4: Response body integrity
    validate_response_integrity(entries, issues)

    valid = not any(issue.startswith(HARD_STOP_PREFIX) for issue in issues)
    return ValidationResult(
        valid=valid,
        issues=issues,
        auth_flow_detected=auth_flow_detected,
        transport_hints=transport_hints,
    )
