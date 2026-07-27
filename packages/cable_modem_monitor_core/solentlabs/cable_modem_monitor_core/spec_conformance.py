"""Spec-conformance validation for ModemData / golden fixtures.

Enforces the value contracts in PARSING_SPEC.md § Channel Field Contracts
by Type. This module is the source of truth for the catalog's
spec-conformance gate AND for the intake pipeline's auto-generation of
canonicalizing ``map:`` blocks (see PARSING_SPEC.md § Canonical
modulation values).

Consumed by:
- ``packages/cable_modem_monitor_catalog/tests/test_modems.py`` —
  validates every committed ``modem.expected.json``.
- ``cable_modem_monitor_catalog_tools.analysis.mapping`` — when
  generating parser.yaml from a HAR, the intake calls
  ``canonicalize_modulation`` on observed column values and emits a
  ``map:`` block whenever any observed value isn't already canonical.

Single source of truth for canonical modulation form prevents the
intake/validator drift loop where each new modem reintroduced
non-canonical values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

ALLOWED_CHANNEL_TYPES: Final = frozenset({"qam", "ofdm", "atdma", "ofdma"})
ALLOWED_LOCK_STATUSES: Final = frozenset({"locked", "not_locked"})
OFDM_STRIPPED_FIELDS: Final = frozenset({"is_ofdm", "symbol_rate"})

# PARSING_SPEC.md § Canonical modulation values.
# The enumeration is the DOCSIS-defined modulation orders, not a fleet
# observation: QPSK plus 8/16/32/64/128/256-QAM (DOCSIS 3.0 SC-QAM) and
# 512/1024/2048/4096-QAM, with optional 8192/16384-QAM (DOCSIS 3.1 OFDM).
# A genuinely new valid order means extending this set with a spec
# citation, never silently dropping it.
# Source: CableLabs CM-SP-PHYv3.0 / CM-SP-PHYv3.1
# (https://www.cablelabs.com/specifications).
CANONICAL_MODULATIONS: Final = frozenset(
    {
        "QPSK",
        "QAM8",
        "QAM16",
        "QAM32",
        "QAM64",
        "QAM128",
        "QAM256",
        "QAM512",
        "QAM1024",
        "QAM2048",
        "QAM4096",
        "QAM8192",
        "QAM16384",
    }
)


def canonicalize_modulation(raw: str) -> str | None:
    """Return canonical modulation form for a raw modem-published string.

    Recognizes QAM constellation values in any common surface form
    (``256QAM``, ``QAM256``, ``256-QAM``, ``256 QAM``, ``256qam``,
    ``qam_256``) and ``QPSK``. Returns ``None`` for strings that aren't
    modulation values at all (channel-type restatements like ``OFDM`` /
    ``ATDMA``, profile IDs, IUC lists, ``Other``, empty/whitespace).

    Used by:
    - Intake to auto-build ``map:`` blocks: any observed value where
      ``canonicalize_modulation(v) != v`` becomes a map entry.
    - Validator to check that committed goldens carry only canonical
      values.
    """
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    if stripped in CANONICAL_MODULATIONS:
        return stripped

    # Strip case + separators to detect QAM<N> / <N>QAM patterns.
    # Whitespace, hyphen, and underscore are the surface-form variants
    # observed in real fleet data; nothing else gets stripped.
    normalized = stripped.replace("-", "").replace("_", "").replace(" ", "").upper()
    if normalized == "QPSK":
        return "QPSK"
    if normalized.startswith("QAM"):
        suffix = normalized[3:]
    elif normalized.endswith("QAM"):
        suffix = normalized[:-3]
    else:
        return None
    candidate = f"QAM{suffix}"
    return candidate if candidate in CANONICAL_MODULATIONS else None


def derive_channel_type_from_modulation(modulation: str | None, direction: str) -> str | None:
    """Derive canonical ``channel_type`` from a channel's modulation value.

    Universal rule (per PARSING_SPEC § Channel Field Contracts):
    - Canonical QAM constellations and QPSK map to ``"qam"`` on
      downstream and ``"atdma"`` on upstream.
    - ``"OFDMA"`` (and variants like ``"OFDM PLC"`` containing OFDMA)
      maps to ``"ofdma"``.
    - ``"OFDM"`` (and ``"OFDM PLC"``) maps to ``"ofdm"``.
    - Anything else returns ``None``.

    Used by the coordinator for sections configured with
    ``channel_type: { derive: from_modulation }`` — replaces hand-coded
    per-modem ``map:`` blocks that enumerate every constellation.
    """
    if not modulation:
        return None
    if canonicalize_modulation(modulation) is not None:
        return "qam" if direction == "downstream" else "atdma"
    upper = modulation.upper()
    if "OFDMA" in upper:
        return "ofdma"
    if "OFDM" in upper:
        return "ofdm"
    return None


def build_modulation_canonicalization_map(observed: set[str]) -> dict[str, str]:
    """Return ``raw → canonical`` entries for non-canonical observed values.

    Values already in canonical form are skipped (no identity-mapping
    noise in the emitted ``map:`` block). Values with no canonical form
    (channel-type strings, IUC lists, etc.) are also skipped — those
    aren't drift to fix at the parser level; they're either channel-type
    sentinels handled by the channel_type derivation or pollution that
    needs to be filtered at the field-extraction level.
    """
    out: dict[str, str] = {}
    for raw in sorted(observed):
        canonical = canonicalize_modulation(raw)
        if canonical is not None and canonical != raw:
            out[raw] = canonical
    return out


@dataclass(frozen=True)
class Violation:
    """One spec-conformance failure.

    The ``(modem, path, rule)`` triple uniquely identifies a violation
    for baseline matching.
    """

    modem: str
    path: str
    rule: str
    value: Any
    message: str

    def fingerprint(self) -> tuple[str, str, str]:
        return (self.modem, self.path, self.rule)


def validate_modem_data(data: dict[str, Any], modem: str) -> list[Violation]:
    """Validate a parsed ``modem.expected.json`` against PARSING_SPEC contracts.

    Args:
        data: Parsed golden fixture (top-level ``downstream`` / ``upstream``
            channel lists, plus ``system_info`` etc.).
        modem: Identifier for the modem (e.g., ``arris/sb6183``). Used to
            tag violations and match against the baseline.

    Returns:
        List of violations. Empty list = conformant.
    """
    violations: list[Violation] = []
    for direction in ("downstream", "upstream"):
        channels = data.get(direction) or []
        for idx, channel in enumerate(channels):
            path_prefix = f"{direction}[{idx}]"
            violations.extend(_validate_channel(channel, modem, path_prefix))
    return violations


def _validate_channel(channel: dict[str, Any], modem: str, path_prefix: str) -> list[Violation]:
    out: list[Violation] = []

    lock_status = channel.get("lock_status")

    # The unlocked-channel nulling rule strips every key except
    # channel_number and lock_status, channel_type included, so the
    # identity requirement cannot apply to an unlocked channel.
    # PARSING_SPEC.md § Field publication is per-modem.
    channel_type = channel.get("channel_type")
    if lock_status != "not_locked" and channel_type not in ALLOWED_CHANNEL_TYPES:
        out.append(
            Violation(
                modem=modem,
                path=f"{path_prefix}.channel_type",
                rule="channel_type_enum",
                value=channel_type,
                message=(f"channel_type must be one of {sorted(ALLOWED_CHANNEL_TYPES)}; got {channel_type!r}"),
            )
        )

    if lock_status is not None and lock_status not in ALLOWED_LOCK_STATUSES:
        out.append(
            Violation(
                modem=modem,
                path=f"{path_prefix}.lock_status",
                rule="lock_status_enum",
                value=lock_status,
                message=(f"lock_status must be one of {sorted(ALLOWED_LOCK_STATUSES)}; got {lock_status!r}"),
            )
        )

    if "modulation" in channel:
        modulation = channel["modulation"]
        if modulation is not None and modulation not in CANONICAL_MODULATIONS:
            out.append(
                Violation(
                    modem=modem,
                    path=f"{path_prefix}.modulation",
                    rule="modulation_canonical",
                    value=modulation,
                    message=(f"modulation must be one of {sorted(CANONICAL_MODULATIONS)}; got {modulation!r}"),
                )
            )

    # OFDM/OFDMA stripping rule (PARSING_SPEC § Fields stripped from
    # OFDM/OFDMA output).
    if channel_type in ("ofdm", "ofdma"):
        for field in OFDM_STRIPPED_FIELDS:
            if field in channel:
                out.append(
                    Violation(
                        modem=modem,
                        path=f"{path_prefix}.{field}",
                        rule="ofdm_stripped_field",
                        value=channel[field],
                        message=(f"{field!r} is not part of the OFDM/OFDMA contract and must be removed by the parser"),
                    )
                )

    return out
