"""Tests for HNAP format detection and field mapping.

Table-driven tests for internal helpers and fixture-driven
integration tests for detect_hnap_sections.

Adding an integration test case = drop a JSON file in
fixtures/format_hnap/.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from solentlabs.cable_modem_monitor_catalog_tools.analysis.format.hnap import (
    _assign_pass2_field,
    _classify_definitive,
    _classify_remaining_numeric,
    _detect_channel_data,
    _detect_channel_type,
    _detect_field_delimiter,
    _detect_filter,
    _detect_record_delimiter,
    _detect_system_info_source,
    _direction_from_response_key,
    _infer_field_mappings,
    _is_row_counter,
    _map_system_info_key,
    _normalize_channel_type,
    _Pass2State,
    _resolve_large_integers,
    detect_hnap_sections,
)
from solentlabs.cable_modem_monitor_catalog_tools.analysis.mapping.field_shape import FieldShapeVocabulary
from tests._helpers import collect_fixtures, load_fixture

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "format_hnap"
HNAP_FIXTURES = collect_fixtures(FIXTURES_DIR)


# =====================================================================
# detect_hnap_sections — fixture-driven integration
# =====================================================================


@pytest.mark.parametrize(
    "fixture_path",
    HNAP_FIXTURES,
    ids=[f.stem for f in HNAP_FIXTURES],
)
def test_detect_hnap_sections(fixture_path: Path) -> None:
    """Integration test: detect_hnap_sections produces expected output."""
    data = load_fixture(fixture_path)
    warnings: list[str] = []
    hard_stops: list[str] = []

    result = detect_hnap_sections(data["_entries"], warnings, hard_stops)

    if "_expected_sections" in data:
        assert result == data["_expected_sections"]

    if "_expected_warning" in data:
        assert any(data["_expected_warning"] in w for w in warnings)

    if "_expected_has_filter" in data:
        assert "downstream" in result
        assert "filter" in result["downstream"]

    if "_expected_has_downstream" in data:
        assert "downstream" in result

    if "_expected_has_system_info" in data:
        assert "system_info" in result
        assert len(result["system_info"]["sources"]) >= 1


# =====================================================================
# _normalize_channel_type — table-driven
# =====================================================================

# ┌──────────────┬──────────┬───────────────────────┐
# │ input        │ expected │ description           │
# ├──────────────┼──────────┼───────────────────────┤
# │ "OFDMA"      │ "ofdma"  │ OFDMA before OFDM     │
# │ "OFDM"       │ "ofdm"   │ plain OFDM            │
# │ "OFDM PLC"   │ "ofdm"   │ OFDM variant          │
# │ "ATDMA"      │ "atdma"  │ upstream modulation   │
# │ "SC-QAM"     │ "qam"    │ SC-QAM → qam          │
# │ "QAM256"     │ "qam"    │ QAM256 → qam          │
# │ "QAM64"      │ "qam"    │ QAM64 → qam           │
# │ "QAM16"      │ "qam"    │ QAM16 → qam           │
# │ "Other"      │ "other"  │ fallback → lowercase  │
# └──────────────┴──────────┴───────────────────────┘

# fmt: off
NORMALIZE_CHANNEL_TYPE_CASES = [
    # (input,       expected,  description)
    ("OFDMA",       "ofdma",   "ofdma_before_ofdm"),
    ("OFDM",        "ofdm",    "plain_ofdm"),
    ("OFDM PLC",    "ofdm",    "ofdm_variant"),
    ("ATDMA",       "atdma",   "upstream_modulation"),
    ("SC-QAM",      "qam",     "sc_qam"),
    ("QAM256",      "qam",     "qam256"),
    ("QAM64",       "qam",     "qam64"),
    ("QAM16",       "qam",     "qam16"),
    ("Other",       "other",   "fallback_lowercase"),
]
# fmt: on


@pytest.mark.parametrize(
    "value,expected,desc",
    NORMALIZE_CHANNEL_TYPE_CASES,
    ids=[c[2] for c in NORMALIZE_CHANNEL_TYPE_CASES],
)
def test_normalize_channel_type(value: str, expected: str, desc: str) -> None:
    """Channel type normalization maps all modulation variants."""
    assert _normalize_channel_type(value) == expected


# =====================================================================
# _map_system_info_key — table-driven
# =====================================================================

# ┌──────────────────────────┬──────────────────────┬────────────────────────┐
# │ input                    │ expected             │ description            │
# ├──────────────────────────┼──────────────────────┼────────────────────────┤
# │ "FirmwareVersion"        │ "software_version"   │ firmware match         │
# │ "SoftwareVersion"        │ "software_version"   │ software alias         │
# │ "ModelName"              │ "model_name"         │ model match            │
# │ "SoftwareModelName"      │ "model_name"         │ software model alias   │
# │ "SystemUptime"           │ "system_uptime"      │ uptime match           │
# │ "Uptime"                 │ "system_uptime"      │ short uptime alias     │
# │ "NetworkAccess"          │ "docsis_status"      │ network access         │
# │ "MacAddress"             │ None                 │ mac not mapped (PII)   │
# │ "SerialNumber"           │ None                 │ serial not mapped (PII)│
# │ "CurSystemTime"          │ "system_time"        │ system time variant    │
# │ "SystemTime"             │ "system_time"        │ system time            │
# │ "InternetConnection"     │ "docsis_status"      │ internet connection    │
# │ "UnknownField"           │ None                 │ no match               │
# └──────────────────────────┴──────────────────────┴────────────────────────┘

# fmt: off
SYSTEM_INFO_KEY_CASES = [
    # (input,                  expected,               description)
    ("FirmwareVersion",        "software_version",      "firmware"),
    ("SoftwareVersion",        "software_version",      "software_alias"),
    ("ModelName",              "model_name",            "model"),
    ("SoftwareModelName",      "model_name",            "software_model"),
    ("SystemUptime",           "system_uptime",         "system_uptime"),
    ("Uptime",                 "system_uptime",         "short_uptime"),
    ("NetworkAccess",          "docsis_status",         "network_access"),
    ("MacAddress",             None,                    "mac_not_mapped_pii"),
    ("SerialNumber",           None,                    "serial_not_mapped_pii"),
    ("CurSystemTime",          "system_time",           "cur_system_time"),
    ("SystemTime",             "system_time",           "system_time"),
    ("InternetConnection",     "docsis_status",         "internet_connection"),
    ("UnknownField",           None,                    "no_match"),
]
# fmt: on


@pytest.mark.parametrize(
    "key,expected,desc",
    SYSTEM_INFO_KEY_CASES,
    ids=[c[2] for c in SYSTEM_INFO_KEY_CASES],
)
def test_map_system_info_key(key: str, expected: str | None, desc: str) -> None:
    """System info key mapping covers all known patterns."""
    assert _map_system_info_key(key) == expected


# =====================================================================
# _direction_from_response_key — table-driven
# =====================================================================

# fmt: off
DIRECTION_CASES = [
    # (response_key,                        expected,      description)
    ("GetStatusDownstreamResponse",          "downstream",  "downstream"),
    ("GetStatusUpstreamResponse",            "upstream",    "upstream"),
    ("GetDsChannelResponse",                 "downstream",  "dschannel"),
    ("GetUsChannelResponse",                 "upstream",    "uschannel"),
    ("GetDeviceInfoResponse",                "unknown",     "no_direction"),
]
# fmt: on


@pytest.mark.parametrize(
    "key,expected,desc",
    DIRECTION_CASES,
    ids=[c[2] for c in DIRECTION_CASES],
)
def test_direction_from_response_key(key: str, expected: str, desc: str) -> None:
    """Response key direction detection covers all patterns."""
    assert _direction_from_response_key(key) == expected


# =====================================================================
# _detect_record_delimiter — table-driven
# =====================================================================

# fmt: off
RECORD_DELIM_CASES = [
    # (value,                                expected,  description)
    ("a^b^c|+|d^e^f",                        "|+|",     "pipe_plus_pipe"),
    ("a^b^c|-|d^e^f",                        "|-|",     "pipe_minus_pipe"),
    ("a^b^c||d^e^f",                         "||",      "double_pipe"),
    ("a^b^c,d^e^f",                          None,      "no_delimiter"),
]
# fmt: on


@pytest.mark.parametrize(
    "value,expected,desc",
    RECORD_DELIM_CASES,
    ids=[c[2] for c in RECORD_DELIM_CASES],
)
def test_detect_record_delimiter(value: str, expected: str | None, desc: str) -> None:
    """Record delimiter detection covers all candidates and None."""
    assert _detect_record_delimiter(value) == expected


# =====================================================================
# _detect_field_delimiter — table-driven
# =====================================================================

# fmt: off
FIELD_DELIM_CASES = [
    # (record,         expected,  description)
    ("a^b^c^d",        "^",       "caret"),
    ("a,b,c,d",        ",",       "comma_fallback"),
    ("a^b",            None,      "caret_too_few_parts"),
    ("a,b",            None,      "comma_too_few_parts"),
    ("abcd",           None,      "no_delimiter"),
]
# fmt: on


@pytest.mark.parametrize(
    "record,expected,desc",
    FIELD_DELIM_CASES,
    ids=[c[2] for c in FIELD_DELIM_CASES],
)
def test_detect_field_delimiter(record: str, expected: str | None, desc: str) -> None:
    """Field delimiter detection covers caret, comma, and none."""
    assert _detect_field_delimiter(record) == expected


# =====================================================================
# _is_row_counter — table-driven
# =====================================================================

# fmt: off
ROW_COUNTER_CASES = [
    # (samples,              expected,  description)
    (["1", "2", "3"],        True,      "sequential_from_1"),
    (["1"],                  True,      "single_element"),
    (["2", "3", "4"],        False,     "not_from_1"),
    (["1", "3", "5"],        False,     "non_sequential"),
    (["a", "b", "c"],        False,     "non_integer"),
    ([],                     False,     "empty"),
]
# fmt: on


@pytest.mark.parametrize(
    "samples,expected,desc",
    ROW_COUNTER_CASES,
    ids=[c[2] for c in ROW_COUNTER_CASES],
)
def test_is_row_counter(samples: list[str], expected: bool, desc: str) -> None:
    """Row counter detection for sequential integers from 1."""
    assert _is_row_counter(samples) == expected


# =====================================================================
# _infer_field_mappings — edge cases
# =====================================================================


class TestInferFieldMappings:
    """Field mapping inference edge cases."""

    def test_empty_records(self) -> None:
        """Empty sample records returns empty mappings."""
        assert _infer_field_mappings([]) == []

    def test_all_empty_values_skipped(self) -> None:
        """Positions with only empty strings are skipped."""
        records = [["", "Locked", "QAM256"], ["", "Locked", "QAM256"]]
        mappings = _infer_field_mappings(records)
        # First position is all-empty → skipped
        indices = [m["index"] for m in mappings]
        assert 0 not in indices

    def test_non_numeric_becomes_string(self) -> None:
        """Non-numeric values in pass 2 get string type."""
        records = [["Locked", "abc", "3.5"], ["Locked", "def", "4.0"]]
        mappings = _infer_field_mappings(records)
        string_fields = [m for m in mappings if m["type"] == "string" and "field_" in m["field"]]
        assert len(string_fields) >= 1

    def test_all_row_counters_returns_empty(self) -> None:
        """Records where every position is a row counter returns no mappings."""
        records = [["1"], ["2"], ["3"]]
        assert _infer_field_mappings(records) == []

    def test_mixed_empty_positions_skipped(self) -> None:
        """Positions that are empty in all samples after pass 1 are skipped."""
        records = [
            ["Locked", "", "567000000"],
            ["Locked", "", "573000000"],
        ]
        mappings = _infer_field_mappings(records)
        indices = [m["index"] for m in mappings]
        assert 1 not in indices


# =====================================================================
# _resolve_large_integers — unit tests
# =====================================================================


class TestResolveLargeIntegers:
    """Large integer resolution: frequency vs symbol_rate."""

    def test_single_large_int_becomes_frequency(self) -> None:
        """One large integer position resolves to frequency."""
        definitive: dict[int, dict[str, Any]] = {
            3: {"field": "_large_int", "type": "frequency", "index": 3, "_max_val": 567000000},
        }
        _resolve_large_integers(definitive)
        assert definitive[3]["field"] == "frequency"
        assert "_max_val" not in definitive[3]

    def test_two_large_ints_sorted_by_max(self) -> None:
        """Two large integers: larger max → frequency, smaller → symbol_rate."""
        definitive: dict[int, dict[str, Any]] = {
            3: {"field": "_large_int", "type": "frequency", "index": 3, "_max_val": 5120000},
            4: {"field": "_large_int", "type": "frequency", "index": 4, "_max_val": 567000000},
        }
        _resolve_large_integers(definitive)
        assert definitive[3]["field"] == "symbol_rate"
        assert definitive[4]["field"] == "frequency"
        assert "_max_val" not in definitive[3]
        assert "_max_val" not in definitive[4]

    def test_three_large_ints(self) -> None:
        """Three large integers: largest → frequency, others → symbol_rate."""
        definitive: dict[int, dict[str, Any]] = {
            2: {"field": "_large_int", "type": "frequency", "index": 2, "_max_val": 100000},
            3: {"field": "_large_int", "type": "frequency", "index": 3, "_max_val": 5120000},
            4: {"field": "_large_int", "type": "frequency", "index": 4, "_max_val": 567000000},
        }
        _resolve_large_integers(definitive)
        assert definitive[2]["field"] == "symbol_rate"
        assert definitive[3]["field"] == "symbol_rate"
        assert definitive[4]["field"] == "frequency"


# =====================================================================
# _classify_definitive — pass 1 classification
# =====================================================================


class TestClassifyDefinitive:
    """Pass 1: definitive field classification."""

    def test_lock_status(self) -> None:
        """Lock patterns classify as lock_status."""
        result = _classify_definitive(0, ["Locked", "Not Locked", "Locked"], set())
        assert result is not None
        assert result["field"] == "lock_status"

    def test_channel_type(self) -> None:
        """Known modulation values classify as channel_type."""
        result = _classify_definitive(0, ["QAM256", "OFDM", "QAM256"], set())
        assert result is not None
        assert result["field"] == "channel_type"

    def test_large_integer_provisional(self) -> None:
        """Large integers (>= 100k) get provisional _large_int classification."""
        result = _classify_definitive(0, ["567000000", "573000000"], set())
        assert result is not None
        assert result["field"] == "_large_int"
        assert result["_max_val"] == 573000000

    def test_small_values_not_definitive(self) -> None:
        """Small numeric values are not definitively classified."""
        result = _classify_definitive(0, ["3", "4", "5"], set())
        assert result is None


# =====================================================================
# _classify_remaining_numeric — pass 2
# =====================================================================


class TestClassifyRemainingNumeric:
    """Pass 2: remaining numeric field assignment."""

    def test_channel_id_assigned(self) -> None:
        """First small non-sequential integer becomes channel_id."""
        state = _Pass2State(channel_id_assigned=False, frequency_assigned=False)
        result = _classify_remaining_numeric(
            0,
            ["5", "6", "10"],
            channel_id_assigned=False,
            frequency_assigned=False,
            state=state,
        )
        assert result is not None
        assert result["field"] == "channel_id"

    def test_float_becomes_power(self) -> None:
        """Values with decimal points become power."""
        state = _Pass2State(channel_id_assigned=True, frequency_assigned=True)
        result = _classify_remaining_numeric(
            0,
            ["3.5", "4.0", "-1.2"],
            channel_id_assigned=True,
            frequency_assigned=True,
            state=state,
        )
        assert result is not None
        assert result["field"] == "power"

    def test_large_value_becomes_symbol_rate(self) -> None:
        """Large values when frequency not assigned become symbol_rate."""
        state = _Pass2State(channel_id_assigned=True, frequency_assigned=False)
        result = _classify_remaining_numeric(
            0,
            ["5120000", "5120000"],
            channel_id_assigned=True,
            frequency_assigned=False,
            state=state,
        )
        assert result is not None
        assert result["field"] == "symbol_rate"

    def test_remaining_standard_order(self) -> None:
        """Remaining fields assigned in standard DOCSIS order."""
        state = _Pass2State(channel_id_assigned=True, frequency_assigned=True)
        result = _classify_remaining_numeric(
            0,
            ["3", "4", "5"],
            channel_id_assigned=True,
            frequency_assigned=True,
            state=state,
        )
        assert result is not None
        assert result["field"] == "power"

    def test_exhausted_remaining_fields(self) -> None:
        """After all standard fields assigned, fallback to field_N."""
        state = _Pass2State(channel_id_assigned=True, frequency_assigned=True)
        state.remaining_idx = 4  # Past power, snr, corrected, uncorrected
        result = _classify_remaining_numeric(
            7,
            ["3", "4", "5"],
            channel_id_assigned=True,
            frequency_assigned=True,
            state=state,
        )
        assert result is not None
        assert result["field"] == "field_7"


# =====================================================================
# _assign_pass2_field — non-numeric fallback
# =====================================================================


class TestAssignPass2Field:
    """Pass 2 field assignment including non-numeric fallback."""

    def test_non_numeric_string_field(self) -> None:
        """Non-numeric values get field_N string type."""
        state = _Pass2State(channel_id_assigned=True, frequency_assigned=True)
        result = _assign_pass2_field(5, ["abc", "def"], state)
        assert result is not None
        assert result["field"] == "field_5"
        assert result["type"] == "string"


# =====================================================================
# _detect_channel_type — unit tests
# =====================================================================


class TestDetectChannelType:
    """Channel type detection from sample data."""

    def test_no_channel_type_field(self) -> None:
        """Returns None when no channel_type mapping exists."""
        mappings = [{"field": "channel_id", "type": "int", "index": 0}]
        result = _detect_channel_type([["5"]], mappings)
        assert result is None

    def test_single_value_field_map(self) -> None:
        """Single channel type value produces field map with normalized value."""
        mappings = [{"field": "channel_type", "type": "string", "index": 0}]
        records = [["QAM256"], ["QAM256"], ["QAM256"]]
        result = _detect_channel_type(records, mappings)
        assert result is not None
        assert result == {"source": "field", "index": 0, "map": {"QAM256": "qam"}}

    def test_multiple_values_map(self) -> None:
        """Multiple channel type values produce map config."""
        mappings = [{"field": "channel_type", "type": "string", "index": 0}]
        records = [["QAM256"], ["OFDM"], ["QAM256"]]
        result = _detect_channel_type(records, mappings)
        assert result is not None
        assert "map" in result
        assert result["map"]["QAM256"] == "qam"
        assert result["map"]["OFDM"] == "ofdm"


# =====================================================================
# _detect_filter — unit tests
# =====================================================================


class TestDetectFilter:
    """Filter detection for placeholder channels."""

    def test_channel_id_zero_detected(self) -> None:
        """Channel_id=0 in samples triggers filter rule."""
        mappings = [{"field": "channel_id", "type": "int", "index": 0}]
        records = [["5", "Locked"], ["0", "Locked"], ["6", "Locked"]]
        result = _detect_filter(records, mappings)
        assert result == {"channel_id": {"not": 0}}

    def test_no_zero_channel_id(self) -> None:
        """No channel_id=0 means no filter."""
        mappings = [{"field": "channel_id", "type": "int", "index": 0}]
        records = [["5", "Locked"], ["6", "Locked"]]
        result = _detect_filter(records, mappings)
        assert result is None

    def test_no_channel_id_mapping(self) -> None:
        """No channel_id field means no filter."""
        mappings = [{"field": "power", "type": "float", "index": 0}]
        records = [["3.5"], ["4.0"]]
        result = _detect_filter(records, mappings)
        assert result is None


# =====================================================================
# _detect_channel_data — edge cases (continue guards)
# =====================================================================

# ┌──────────────────────────────┬─────────────────────────────────────┬──────────────┐
# │ scenario                     │ data value                          │ expected     │
# ├──────────────────────────────┼─────────────────────────────────────┼──────────────┤
# │ no record delimiter          │ long string, no |+| |-| ||          │ None         │
# │ single record                │ one record only (no split)          │ None         │
# │ no field delimiter           │ records without ^ or ,              │ None         │
# │ no mappings                  │ records with only row counters      │ None         │
# └──────────────────────────────┴─────────────────────────────────────┴──────────────┘

# fmt: off
CHANNEL_DATA_EDGE_CASES = [
    # (description,              response_data,                                                     expected)
    ("no_record_delimiter",      {"Data": "this is a long string without any recognized delimiters here"},  None),
    ("single_record",            {"Data": "1^Locked^QAM256^5^567000000|+|"},                                None),
    ("no_field_delimiter",       {"Data": "abcdefghij|+|klmnopqrst"},                                      None),
    ("all_row_counters",         {"Data": "1^1^1|+|2^2^2|+|3^3^3"},                                         None),
]
# fmt: on


@pytest.mark.parametrize(
    "desc,response_data,expected",
    CHANNEL_DATA_EDGE_CASES,
    ids=[c[0] for c in CHANNEL_DATA_EDGE_CASES],
)
def test_detect_channel_data_edge(
    desc: str,
    response_data: dict[str, Any],
    expected: dict[str, Any] | None,
) -> None:
    """_detect_channel_data returns None for edge-case inputs."""
    result = _detect_channel_data("GetTestResponse", response_data)
    assert result is expected


# =====================================================================
# system_info field shape - table-driven
# =====================================================================
#
# HNAP detection lives apart from the html_fields and json detectors, so
# these assert it applies the same normalization rather than emitting
# passthrough strings.

VOCABULARY = FieldShapeVocabulary(
    uptime_formats=["{days} days {hours}h:{minutes}m:{seconds}s"],
    docsis_status_success_values=frozenset({"allowed", "connected"}),
)

# fmt: off
SHAPE_CASES = [
    # (hnap key, raw value, expected field def, desc)
    ("CustomerConnSystemUpTime", "17 days 00h:51m:30s",
     {"source": "CustomerConnSystemUpTime", "field": "system_uptime", "type": "uptime",
      "format": "{days} days {hours}h:{minutes}m:{seconds}s"},
     "uptime typed and formatted"),
    ("CustomerConnNetworkAccess", "Allowed",
     {"source": "CustomerConnNetworkAccess", "field": "docsis_status", "type": "string",
      "map": {"Allowed": "Operational"}},
     "docsis_status mapped"),
    ("InternetConnection", "Ranging",
     {"source": "InternetConnection", "field": "docsis_status", "type": "string"},
     "fault state passes through"),
    ("FirmwareVersion", "2.0.1",
     {"source": "FirmwareVersion", "field": "software_version", "type": "string"},
     "ordinary field unchanged"),
]
# fmt: on


@pytest.mark.parametrize("key,value,expected,desc", SHAPE_CASES, ids=[c[3] for c in SHAPE_CASES])
def test_system_info_field_shape(key: str, value: str, expected: dict[str, Any], desc: str) -> None:
    """HNAP fields carry the same type, format, and map as the other detectors."""
    source = _detect_system_info_source("GetDeviceInfoResponse", {key: value}, VOCABULARY)
    assert source is not None, desc
    assert source["fields"] == [expected], desc


def test_uptime_without_a_matching_format_stays_a_string() -> None:
    """An unparseable uptime must not ship a format that resolves nothing."""
    source = _detect_system_info_source(
        "GetDeviceInfoResponse",
        {"CustomerConnSystemUpTime": "who knows"},
        VOCABULARY,
    )
    assert source is not None
    assert source["fields"] == [{"source": "CustomerConnSystemUpTime", "field": "system_uptime", "type": "string"}]
