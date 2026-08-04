"""Phase 6 - uptime format inference.

``system_uptime`` is a normalized field, not a passthrough string: Core
declares ``type: uptime`` plus a ``format`` naming the raw shape, and
converts to ``"N days HHh:MMm:SSs"``. Intake has to pick that format,
and the observed value decides it. Each candidate is tried against
Core's own converter, so a format is only emitted when it demonstrably
parses the value in the capture.

Per PARSING_SPEC.md Uptime Normalization.
"""

from __future__ import annotations

from collections.abc import Sequence

from solentlabs.cable_modem_monitor_core.parsers.type_conversion import convert_value

# Preset for raw integer seconds, per PARSING_SPEC.
_SECONDS = "seconds"


def detect_uptime_format(raw: object, candidates: Sequence[str]) -> str:
    """Return the format that parses this raw uptime value, or empty if none do.

    ``candidates`` come from the fleet's committed configs, most specific
    first.
    """
    text = str(raw).strip()
    if not text:
        return ""

    if text.isdigit():
        return _SECONDS

    for candidate in candidates:
        if candidate == _SECONDS:
            continue
        if convert_value(text, "uptime", input_format=candidate) is not None:
            return candidate

    return ""
