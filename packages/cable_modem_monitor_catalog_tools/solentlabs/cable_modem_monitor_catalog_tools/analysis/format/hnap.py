"""Phase 5-6 — HNAP format detection and field mapping.

Detects delimited string format, extracts response_keys, delimiters,
and positional field mappings from HNAP SOAP JSON responses.

Per docs/ONBOARDING_SPEC.md Phase 5 (HNAP transport).
"""

from __future__ import annotations

import re
from typing import Any

from solentlabs.cable_modem_monitor_core.har import merge_hnap_har_responses

from ...validation.har_utils import WARNING_PREFIX
from ..mapping.field_shape import FieldShapeVocabulary, field_shape
from ..types import FleetPatterns

# HNAP response keys that indicate downstream/upstream channel data.
_DS_PATTERNS = re.compile(r"downstream|dschannel", re.IGNORECASE)
_US_PATTERNS = re.compile(r"upstream|uschannel", re.IGNORECASE)

# Common HNAP record delimiters in order of prevalence.
_CANDIDATE_RECORD_DELIMITERS = ["|+|", "|-|", "||"]

# HNAP channel type values that indicate technology types.
_CHANNEL_TYPE_MAP_VALUES = {
    "QAM256",
    "256QAM",
    "QAM64",
    "QAM16",
    "OFDM",
    "OFDM PLC",
    "SC-QAM",
    "ATDMA",
    "OFDMA",
}

# Fields known to be numeric (for type inference from positional values).
_FREQUENCY_PATTERN = re.compile(r"^\d{5,}$")
_SMALL_FLOAT_PATTERN = re.compile(r"^-?\d+\.?\d*$")
_INTEGER_PATTERN = re.compile(r"^\d+$")
_LOCK_PATTERN = re.compile(r"^(Locked|Not Locked|Unlocked)$", re.IGNORECASE)


def _augment_from_fleet(fleet: FleetPatterns | None) -> None:
    """Merge fleet-derived delimiters and channel types into module sets."""
    if not fleet:
        return
    for d in fleet.delimiters:
        if d not in _CANDIDATE_RECORD_DELIMITERS:
            _CANDIDATE_RECORD_DELIMITERS.append(d)
    _CHANNEL_TYPE_MAP_VALUES.update(fleet.channel_type_values)


def detect_hnap_sections(
    entries: list[dict[str, Any]],
    warnings: list[str],
    hard_stops: list[str],
    *,
    fleet: FleetPatterns | None = None,
) -> dict[str, Any]:
    """Detect HNAP format and field mappings from HAR entries.

    Scans ``GetMultipleHNAPs`` response bodies for delimited string
    data, detects record/field delimiters, infers positional field
    mappings, and identifies system_info sources.

    When ``fleet`` is provided, fleet-derived delimiters and channel
    type values augment the baseline detection sets.

    Args:
        entries: HAR ``log.entries`` list.
        warnings: Mutable list to append warnings to.
        hard_stops: Mutable list to append hard stops to.
        fleet: Optional fleet patterns for augmented detection.

    Returns:
        Sections dict with downstream, upstream, and system_info keys.
    """
    _augment_from_fleet(fleet)
    hnap_responses = _collect_hnap_responses(entries)
    if not hnap_responses:
        warnings.append(
            f"{WARNING_PREFIX} No GetMultipleHNAPs responses found in HAR. "
            "Cannot detect HNAP format or field mappings.",
        )
        return {}

    sections: dict[str, Any] = {}
    system_info_sources: list[dict[str, Any]] = []
    vocabulary = FieldShapeVocabulary.from_fleet(fleet)

    for response_key, response_data in hnap_responses.items():
        if not isinstance(response_data, dict):
            continue

        # Find the data key that holds delimited channel strings
        channel_result = _detect_channel_data(
            response_key,
            response_data,
            fleet=fleet,
        )

        if channel_result is not None:
            direction = _direction_from_response_key(response_key)
            if direction != "unknown" and direction not in sections:
                sections[direction] = channel_result
        else:
            # No delimited data — potential system_info source
            si_source = _detect_system_info_source(
                response_key,
                response_data,
                vocabulary,
            )
            if si_source is not None:
                system_info_sources.append(si_source)

    if system_info_sources:
        sections["system_info"] = {
            "sources": system_info_sources,
        }

    if not sections:
        warnings.append(
            f"{WARNING_PREFIX} HNAP responses found but no channel data "
            "or system_info detected. Manual review required.",
        )

    return sections


def _collect_hnap_responses(
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Extract all HNAP action responses from HAR entries.

    Delegates to the shared ``merge_hnap_har_responses`` utility.
    """
    return merge_hnap_har_responses(entries)


def _detect_channel_data(
    response_key: str,
    response_data: dict[str, Any],
    fleet: FleetPatterns | None = None,
) -> dict[str, Any] | None:
    """Detect delimited channel data in an HNAP action response.

    Looks for string values containing record delimiters. If found,
    analyzes the delimited structure and builds a section config.

    When ``fleet`` contains a layout for ``response_key``, uses the
    proven field positions from committed configs instead of heuristic
    inference — this handles fields like ``channel_number`` that look
    like row counters, and large error counters that would otherwise be
    misclassified as ``frequency`` or ``symbol_rate``.

    Returns section dict or None if no delimited data found.
    """
    for data_key, value in response_data.items():
        if not isinstance(value, str) or len(value) < 10:
            continue

        # Detect record delimiter
        record_delim = _detect_record_delimiter(value)
        if record_delim is None:
            continue

        # Split into records
        records = [r for r in value.split(record_delim) if r.strip()]
        if len(records) < 2:
            continue

        # Detect field delimiter from first record
        field_delim = _detect_field_delimiter(records[0])
        if field_delim is None:
            continue

        # Parse all records for field mapping and channel_type detection.
        # HNAP channel data is small (typically <100 records); sampling
        # can miss OFDM channels at the tail of the list.
        sample_records = [r.split(field_delim) for r in records]

        # Fleet-based layout: use proven field positions from committed configs
        if fleet and response_key in fleet.hnap_response_layouts:
            mappings = _mappings_from_hnap_fleet(fleet.hnap_response_layouts[response_key])
        else:
            mappings = _infer_field_mappings(sample_records)

        if not mappings:
            continue

        section: dict[str, Any] = {
            "format": "hnap",
            "response_key": response_key,
            "data_key": data_key,
            "record_delimiter": record_delim,
            "field_delimiter": field_delim,
            "mappings": mappings,
        }

        # Detect channel type
        channel_type = _detect_channel_type(sample_records, mappings)
        if channel_type is not None:
            section["channel_type"] = channel_type

        # Detect filter (skip row counter = 0 placeholder channels)
        filter_rule = _detect_filter(sample_records, mappings)
        if filter_rule is not None:
            section["filter"] = filter_rule

        return section

    return None


def _detect_record_delimiter(value: str) -> str | None:
    """Detect the record delimiter in a delimited string."""
    for delim in _CANDIDATE_RECORD_DELIMITERS:
        if delim in value:
            parts = value.split(delim)
            if len(parts) >= 2:
                return delim
    return None


def _detect_field_delimiter(record: str) -> str | None:
    """Detect the field delimiter within a single record."""
    # Caret is the standard HNAP field delimiter
    if "^" in record:
        parts = record.split("^")
        if len(parts) >= 3:
            return "^"
    # Comma fallback
    if "," in record:
        parts = record.split(",")
        if len(parts) >= 3:
            return ","
    return None


def _mappings_from_hnap_fleet(layout: dict[str, Any]) -> list[dict[str, Any]]:
    """Build field mappings from a fleet-proven HNAP response layout.

    Uses committed field position definitions instead of heuristic
    inference.  Channel_type map is still derived from actual HAR data
    by ``_detect_channel_type``.
    """
    result = []
    for f in layout.get("fields", []):
        if not isinstance(f, dict) or "field" not in f or "index" not in f:
            continue
        result.append(
            {
                "field": f["field"],
                "type": f.get("type", "string"),
                "index": f["index"],
            }
        )
    return result


def _infer_field_mappings(
    sample_records: list[list[str]],
) -> list[dict[str, Any]]:
    """Infer field names and types from sample record values.

    Two-pass approach:
    1. Identify definitive fields (row counter, lock_status, channel_type,
       frequency/large integers). Row counters are skipped entirely.
    2. Assign remaining numeric fields by DOCSIS convention — first
       unassigned small integer becomes channel_id, then power/SNR by
       value range, then corrected/uncorrected for remaining integers.
    """
    if not sample_records:
        return []

    field_count = len(sample_records[0])

    # Collect samples per position
    position_samples = _collect_position_samples(sample_records, field_count)

    # Pass 1 + 1.5: Identify definitive fields, skip positions, resolve large ints
    skip_indices, definitive = _run_pass1(position_samples)

    # Pass 2: Assign remaining numeric fields by DOCSIS convention
    assigned_fields = {m["field"] for m in definitive.values()}
    pass2_state = _Pass2State(
        channel_id_assigned="channel_id" in assigned_fields,
        frequency_assigned="frequency" in assigned_fields,
    )

    mappings: list[dict[str, Any]] = []
    for idx in range(field_count):
        if idx in skip_indices:
            continue
        if idx in definitive:
            mappings.append(definitive[idx])
            continue

        samples = position_samples[idx]
        mapping = _assign_pass2_field(idx, samples, pass2_state)
        if mapping is not None:
            mappings.append(mapping)

    return mappings


def _collect_position_samples(
    sample_records: list[list[str]],
    field_count: int,
) -> list[list[str]]:
    """Collect non-empty stripped samples for each field position."""
    position_samples: list[list[str]] = []
    for idx in range(field_count):
        samples = [r[idx].strip() for r in sample_records if idx < len(r) and r[idx].strip()]
        position_samples.append(samples)
    return position_samples


def _run_pass1(
    position_samples: list[list[str]],
) -> tuple[set[int], dict[int, dict[str, Any]]]:
    """Pass 1: identify definitive fields and skip positions.

    Also runs pass 1.5 to resolve provisional ``_large_int`` fields.

    Row counters (sequential 1..N integers) are promoted to channel_number
    only when the record contains other non-counter positions. If every
    position looks like a row counter the data is degenerate and all
    counters are skipped, preserving the empty-mappings return.

    Returns:
        Tuple of (skip_indices, definitive_mappings).
    """
    skip_indices: set[int] = set()
    definitive: dict[int, dict[str, Any]] = {}
    pass1_fields: set[str] = set()

    # Pre-scan: determine whether ALL non-empty positions are row counters.
    # If so, the record has no real data and counters stay as skips.
    non_empty = [s for s in position_samples if s and not all(v == "" for v in s)]
    all_counters = non_empty and all(_is_row_counter(s) for s in non_empty)

    for idx, samples in enumerate(position_samples):
        if not samples or all(s == "" for s in samples):
            skip_indices.add(idx)
            continue
        if _is_row_counter(samples):
            if not all_counters and "channel_number" not in pass1_fields:
                # Sequential 1..N in a record with real data fields →
                # channel_number, not a display row counter.
                definitive[idx] = {"field": "channel_number", "type": "integer", "index": idx}
                pass1_fields.add("channel_number")
            else:
                # Degenerate all-counters record, or a subsequent counter after
                # channel_number was already claimed — skip so pass 2 ignores it.
                skip_indices.add(idx)
            continue
        result = _classify_definitive(idx, samples, pass1_fields)
        if result is not None:
            definitive[idx] = result
            pass1_fields.add(result["field"])

    # Pass 1.5: Resolve provisional _large_int fields
    _resolve_large_integers(definitive)
    return skip_indices, definitive


class _Pass2State:
    """Mutable state for pass 2 numeric field assignment."""

    # Remaining numeric fields in standard DOCSIS order.
    _REMAINING_FIELDS = ["power", "snr", "corrected", "uncorrected"]

    def __init__(self, channel_id_assigned: bool, frequency_assigned: bool) -> None:
        self.channel_id_assigned = channel_id_assigned
        self.frequency_assigned = frequency_assigned
        self.remaining_idx = 0

    def next_remaining_field(self) -> tuple[str, str] | None:
        """Return (field_name, field_type) for the next standard field, or None."""
        if self.remaining_idx >= len(self._REMAINING_FIELDS):
            return None
        name = self._REMAINING_FIELDS[self.remaining_idx]
        ftype = "float" if name in ("power", "snr") else "int"
        return name, ftype

    def advance_remaining(self, field_name: str) -> None:
        """Advance remaining index past the assigned field."""
        if field_name in self._REMAINING_FIELDS:
            self.remaining_idx = self._REMAINING_FIELDS.index(field_name) + 1


def _assign_pass2_field(
    idx: int,
    samples: list[str],
    state: _Pass2State,
) -> dict[str, Any] | None:
    """Assign a single position in pass 2.

    Returns a mapping dict or None if the position is empty.
    Updates ``state`` when a field is assigned.
    """
    if not all(_SMALL_FLOAT_PATTERN.match(s) for s in samples):
        return {"field": f"field_{idx}", "type": "string", "index": idx}

    result = _classify_remaining_numeric(
        idx,
        samples,
        state.channel_id_assigned,
        state.frequency_assigned,
        state,
    )
    if result is not None:
        if result["field"] == "channel_id":
            state.channel_id_assigned = True
        else:
            state.advance_remaining(result["field"])
    return result


def _resolve_large_integers(
    definitive: dict[int, dict[str, Any]],
) -> None:
    """Resolve provisional ``_large_int`` fields to frequency or symbol_rate.

    When only one large-integer position exists, it is ``frequency``.
    When two exist, the one with larger max values is ``frequency``
    and the smaller is ``symbol_rate`` (DOCSIS upstream convention:
    frequencies are typically higher than symbol rates/channel widths).
    """
    large_int_indices = [idx for idx, m in definitive.items() if m["field"] == "_large_int"]

    if len(large_int_indices) == 1:
        definitive[large_int_indices[0]]["field"] = "frequency"
        definitive[large_int_indices[0]].pop("_max_val", None)
    elif len(large_int_indices) >= 2:
        # Sort by max value — larger = frequency, smaller = symbol_rate
        sorted_indices = sorted(
            large_int_indices,
            key=lambda i: definitive[i].get("_max_val", 0),
        )
        for idx in sorted_indices[:-1]:
            definitive[idx]["field"] = "symbol_rate"
            definitive[idx].pop("_max_val", None)
        definitive[sorted_indices[-1]]["field"] = "frequency"
        definitive[sorted_indices[-1]].pop("_max_val", None)


def _classify_definitive(
    index: int,
    samples: list[str],
    assigned_fields: set[str],
) -> dict[str, Any] | None:
    """Pass 1: classify fields with unambiguous signal patterns.

    Returns a mapping dict for: lock_status, channel_type,
    frequency, symbol_rate (large integers). Returns None if the
    field cannot be definitively classified.

    Uses ``assigned_fields`` to avoid duplicate assignment — e.g.,
    if ``frequency`` is already assigned, the next large-integer
    position becomes ``symbol_rate`` instead.
    """
    # Lock status
    if all(_LOCK_PATTERN.match(s) for s in samples):
        return {"field": "lock_status", "type": "lock_status", "index": index}

    # Channel type (non-numeric strings matching known types)
    if any(s in _CHANNEL_TYPE_MAP_VALUES for s in samples):
        return {"field": "channel_type", "type": "string", "index": index}

    # Filter placeholder zeros (unlocked/inactive channels report 0
    # for frequency, symbol_rate, etc.)
    non_zero = [s for s in samples if s not in ("0", "0.0")]
    if not non_zero:
        return None

    # Large integers (>= 100k, <= 2B): frequency or symbol_rate.
    # Capped at 2 GHz — cumulative error counters can exceed this and
    # must not be misclassified as frequency/symbol_rate.
    # Deferred to pass 1.5 (_resolve_large_integers) when multiple
    # large-integer positions exist. Return a provisional classification.
    if all(_FREQUENCY_PATTERN.match(s) for s in non_zero):
        max_val = max(int(s) for s in non_zero)
        if 100_000 <= max_val <= 2_000_000_000:
            return {
                "field": "_large_int",
                "type": "frequency",
                "index": index,
                "_max_val": max_val,
            }

    # MHz-range floats: some HNAP modems report frequency in MHz
    # (e.g., 543.0) instead of Hz. Cable frequencies range 54–1218 MHz,
    # so all non-zero values >= 100.0 distinguishes from power/SNR.
    # Only triggered when values contain actual decimal points — pure
    # integers are handled by the integer path above.
    if any("." in s for s in non_zero) and all(_SMALL_FLOAT_PATTERN.match(s) for s in non_zero):
        float_vals = [float(s) for s in non_zero]
        if all(v >= 100.0 for v in float_vals):
            max_val = int(max(float_vals) * 1_000_000)
            return {
                "field": "_large_int",
                "type": "frequency",
                "index": index,
                "_max_val": max_val,
            }

    return None


def _classify_remaining_numeric(
    index: int,
    samples: list[str],
    channel_id_assigned: bool,
    frequency_assigned: bool,
    state: _Pass2State,
) -> dict[str, Any] | None:
    """Pass 2: classify remaining numeric fields by DOCSIS convention.

    Assignment order: channel_id (first unassigned small non-sequential
    integer), then remaining fields in standard DOCSIS order
    (power, snr, corrected, uncorrected).
    """
    values = [float(s) for s in samples]
    max_val = max(values)
    all_int = all(_INTEGER_PATTERN.match(s) for s in samples)

    # First unassigned small integer with non-zero, non-sequential → channel_id
    if not channel_id_assigned and all_int:
        int_vals = [int(s) for s in samples]
        if not _is_sequential_from_one(int_vals) and max(int_vals) < 1000 and any(v > 0 for v in int_vals):
            return {"field": "channel_id", "type": "int", "index": index}

    # Large values that aren't frequency → symbol_rate
    if max_val >= 100_000 and not frequency_assigned:
        return {"field": "symbol_rate", "type": "frequency", "index": index}

    # Assign from remaining standard DOCSIS field order
    # (power, snr, corrected, uncorrected)
    next_field = state.next_remaining_field()
    if next_field is not None:
        field_name, field_type = next_field
        return {"field": field_name, "type": field_type, "index": index}

    return {"field": f"field_{index}", "type": "int", "index": index}


def _detect_channel_type(
    sample_records: list[list[str]],
    mappings: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Detect channel type configuration from sample data.

    When a field maps to ``channel_type``, always returns a field-based
    map with normalized values. This ensures ``generate_config`` can
    produce an inline ``map:`` on the field mapping — the form the
    parser expects at runtime.
    """
    ct_mapping = None
    for m in mappings:
        if m["field"] == "channel_type":
            ct_mapping = m
            break

    if ct_mapping is None:
        return None

    idx = ct_mapping["index"]
    type_values = set()
    for record in sample_records:
        if idx < len(record) and record[idx].strip():
            type_values.add(record[idx].strip())

    if not type_values:
        return {"fixed": "unknown"}

    # Build normalized map from observed values
    type_map: dict[str, str] = {}
    for val in sorted(type_values):
        type_map[val] = _normalize_channel_type(val)

    return {"source": "field", "index": idx, "map": type_map}


def _normalize_channel_type(value: str) -> str:
    """Normalize a channel type string to the canonical form."""
    lower = value.lower().strip()
    if "ofdma" in lower:
        return "ofdma"
    if "ofdm" in lower:
        return "ofdm"
    if "atdma" in lower:
        return "atdma"
    if lower in ("sc-qam", "qam256", "256qam", "qam64", "qam16"):
        return "qam"
    return lower


def _detect_filter(
    sample_records: list[list[str]],
    mappings: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Detect filter rules for placeholder channels (channel_id == 0).

    Only generates the filter when channel_id=0 records are genuinely
    empty slots (no channel attempting to lock). Records with explicit
    "Not Locked" or "Unlocked" status are real DOCSIS channels — they
    report channel_id=0 because no channel ID was assigned, but they
    represent real RF measurement points and must not be filtered.
    """
    channel_id_idx: int | None = None
    lock_status_idx: int | None = None
    for m in mappings:
        if m["field"] == "channel_id":
            channel_id_idx = m["index"]
        elif m["field"] == "lock_status":
            lock_status_idx = m["index"]

    if channel_id_idx is None:
        return None

    zero_id_records = [r for r in sample_records if channel_id_idx < len(r) and r[channel_id_idx].strip() == "0"]
    if not zero_id_records:
        return None

    # If any channel_id=0 record has explicit "Not Locked" status, these
    # are real unlocked channels — don't filter them.
    if lock_status_idx is not None:
        for record in zero_id_records:
            if lock_status_idx < len(record):
                ls_val = record[lock_status_idx].strip().lower()
                if "not locked" in ls_val or "unlocked" in ls_val:
                    return None

    return {"channel_id": {"not": 0}}


def _detect_system_info_source(
    response_key: str,
    response_data: dict[str, Any],
    vocabulary: FieldShapeVocabulary,
) -> dict[str, Any] | None:
    """Detect system_info fields in an HNAP action response.

    Looks for flat key-value responses with string values that
    contain useful system information (firmware, model, uptime).
    """
    fields: list[dict[str, Any]] = []

    for key, value in response_data.items():
        if not isinstance(value, str) or not value:
            continue

        canonical = _map_system_info_key(key)
        if canonical is None:
            continue

        # Same normalization the html_fields and json detectors apply:
        # system_uptime and docsis_status are canonical fields, not
        # passthrough strings. The response value is the evidence.
        field_type, field_format, field_map = field_shape(canonical, value, vocabulary)
        field_def: dict[str, Any] = {"source": key, "field": canonical, "type": field_type}
        if field_format:
            field_def["format"] = field_format
        if field_map:
            field_def["map"] = field_map
        fields.append(field_def)

    if not fields:
        return None

    return {
        "format": "hnap",
        "response_key": response_key,
        "fields": fields,
    }


def _map_system_info_key(key: str) -> str | None:
    """Map an HNAP response key to a canonical system_info field name."""
    lower = key.lower()

    if "firmwareversion" in lower or "softwareversion" in lower:
        return "software_version"
    if "modelname" in lower or "softwaremodelname" in lower:
        return "model_name"
    if "systemuptime" in lower or "uptime" in lower:
        return "system_uptime"
    if "networkaccess" in lower:
        return "docsis_status"
    # mac_address / serial_number intentionally not mapped — identity PII
    # with no CMM consumer. See SYSTEM_INFO_SPEC § Tiered Sensor Model.
    if "systemtime" in lower or "cursystemtime" in lower:
        return "system_time"
    if "internetconnection" in lower:
        return "docsis_status"

    return None


def _direction_from_response_key(response_key: str) -> str:
    """Infer downstream/upstream from the HNAP response key name."""
    if _DS_PATTERNS.search(response_key):
        return "downstream"
    if _US_PATTERNS.search(response_key):
        return "upstream"
    return "unknown"


def _is_row_counter(samples: list[str]) -> bool:
    """Check if samples represent a sequential row counter (1, 2, 3, ...)."""
    if not all(_INTEGER_PATTERN.match(s) for s in samples):
        return False
    values = [int(s) for s in samples]
    return _is_sequential_from_one(values)


def _is_sequential_from_one(values: list[int]) -> bool:
    """Check if integer values are sequential starting from 1."""
    if not values or values[0] != 1:
        return False
    return values == list(range(1, len(values) + 1))
