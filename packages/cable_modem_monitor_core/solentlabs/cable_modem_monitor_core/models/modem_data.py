"""ModemData output contract.

TypedDict for the parser output shape (Option C from design). Parsers return
plain dicts matching this shape. Pydantic validators are separate — used by
MCP tools and test harness for optional runtime validation.

See PARSING_SPEC.md Output Contract for the authoritative definition.
"""

from __future__ import annotations

from typing import Any, Required, TypedDict

from pydantic import BaseModel, ConfigDict, model_validator

from .field_registry import (
    ALL_CHANNEL_TYPES,
    CHANNEL_REQUIRED_FIELDS,
    DOWNSTREAM_CHANNEL_TYPES,
    UPSTREAM_CHANNEL_TYPES,
)

# ---------------------------------------------------------------------------
# TypedDict contract (what parsers return)
# ---------------------------------------------------------------------------


class DownstreamChannel(TypedDict, total=False):
    """A single downstream channel."""

    channel_id: Required[int]
    channel_type: Required[str]
    frequency: int
    power: float
    snr: float
    lock_status: str
    modulation: str
    corrected: int
    uncorrected: int


class UpstreamChannel(TypedDict, total=False):
    """A single upstream channel."""

    channel_id: Required[int]
    channel_type: Required[str]
    frequency: int
    power: float
    lock_status: str
    modulation: str
    symbol_rate: int


class ModemData(TypedDict, total=False):
    """Parser output shape.

    downstream and upstream are lists of channel dicts with canonical fields
    plus arbitrary pass-through fields. system_info is a flat dict whose
    values carry their declared type, per PARSING_SPEC Output Contract.
    """

    downstream: Required[list[DownstreamChannel]]
    upstream: Required[list[UpstreamChannel]]
    system_info: dict[str, Any]


# ---------------------------------------------------------------------------
# Pydantic validators (for MCP tools / test harness)
# ---------------------------------------------------------------------------


class ChannelValidator(BaseModel):
    """Validates canonical fields on a single channel dict.

    extra="allow" permits pass-through fields (Tier 2/3).
    """

    model_config = ConfigDict(extra="allow")

    channel_id: int
    channel_type: str
    frequency: int | None = None
    power: float | None = None
    snr: float | None = None
    lock_status: str | None = None
    modulation: str | None = None
    corrected: int | None = None
    uncorrected: int | None = None
    symbol_rate: int | None = None

    @model_validator(mode="after")
    def validate_channel_type(self) -> ChannelValidator:
        """Ensure channel_type is a canonical value."""
        if self.channel_type not in ALL_CHANNEL_TYPES:
            raise ValueError(f"channel_type '{self.channel_type}' not in " f"{sorted(ALL_CHANNEL_TYPES)}")
        return self


def validate_modem_data(data: dict[str, Any]) -> list[str]:
    """Validate a ModemData dict. Returns list of error strings (empty = valid).

    Checks:
    - Required top-level keys present
    - Each channel has required fields (channel_id, channel_type)
    - channel_type values are canonical
    - Field types are correct for canonical fields
    """
    errors: list[str] = []

    if "downstream" not in data and "upstream" not in data:
        errors.append("ModemData must have at least 'downstream' or 'upstream'")
        return errors

    for section_name, valid_types in [
        ("downstream", DOWNSTREAM_CHANNEL_TYPES),
        ("upstream", UPSTREAM_CHANNEL_TYPES),
    ]:
        channels = data.get(section_name, [])
        if not isinstance(channels, list):
            errors.append(f"'{section_name}' must be a list")
            continue

        for i, channel in enumerate(channels):
            prefix = f"{section_name}[{i}]"

            for req_field in CHANNEL_REQUIRED_FIELDS:
                if req_field not in channel:
                    errors.append(f"{prefix}: missing required field '{req_field}'")

            ct = channel.get("channel_type")
            if ct is not None and ct not in valid_types:
                errors.append(
                    f"{prefix}: channel_type '{ct}' not valid for {section_name}, "
                    f"expected one of {sorted(valid_types)}"
                )

            cid = channel.get("channel_id")
            if cid is not None and not isinstance(cid, int):
                errors.append(f"{prefix}: channel_id must be int, got {type(cid).__name__}")

    system_info = data.get("system_info")
    if system_info is not None and not isinstance(system_info, dict):
        errors.append("'system_info' must be a dict")

    return errors
