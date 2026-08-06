"""Tests for Phase 6: DOCSIS service flow detection.

Covers array shape (wrapped vs flat), direction vocabulary (canonical
words vs mapped wire spellings), metric typing, and the negative cases
that must produce no aggregates.
"""

from __future__ import annotations

from typing import Any

import pytest

# analysis.format must initialize before analysis.mapping; importing a
# mapping submodule first trips the packages' import cycle.
from solentlabs.cable_modem_monitor_catalog_tools.analysis import format as _format  # noqa: F401
from solentlabs.cable_modem_monitor_catalog_tools.analysis.mapping.service_flows import (
    detect_service_flow_aggregates,
)
from solentlabs.cable_modem_monitor_core.models.parser_config.system_info import JSONSystemInfoSource
from solentlabs.cable_modem_monitor_core.parsers.formats.json_system_info import JSONSystemInfoParser

RESOURCE = "/api/serviceflows"

# =====================================================================
# Body builders
# =====================================================================


def _wrapped(*flows: dict[str, Any]) -> dict[str, Any]:
    """Body whose array items nest each flow under a wrapper key."""
    return {"serviceFlows": [{"serviceFlow": f} for f in flows]}


def _flat(*flows: dict[str, Any]) -> dict[str, Any]:
    """Body whose array items are the flows themselves."""
    return {"serviceFlows": list(flows)}


DS_FLOW = {"direction": "downstream", "maxTrafficRate": 856000000, "maxTrafficBurst": 42600}
US_FLOW = {"direction": "upstream", "maxTrafficRate": 74900000, "maxTrafficBurst": 42600}


# =====================================================================
# Detection - table-driven
# =====================================================================

BOTH_DIRECTIONS = [
    "provisioned_speed_down",
    "provisioned_speed_up",
    "provisioned_burst_down",
    "provisioned_burst_up",
]

# fmt: off
DETECTION_CASES = [
    # (body,                                                              expected_fields,      desc)
    (_wrapped(DS_FLOW, US_FLOW),
     BOTH_DIRECTIONS,                                                     "wrapped items, both directions"),
    (_flat(DS_FLOW, US_FLOW),
     BOTH_DIRECTIONS,                                                     "flat items, both directions"),
    (_wrapped(DS_FLOW),
     ["provisioned_speed_down", "provisioned_burst_down"],                "downstream only"),
    (_wrapped({"direction": "upstream", "maxTrafficRate": 1}),
     ["provisioned_speed_up"],                                            "rate without burst"),
    (_wrapped({"direction": "downstream", "pMaxTrafficRate": 1, "pMaxTrafficBurst": 2}),
     ["provisioned_speed_down", "provisioned_burst_down"],                "p-prefixed metric spelling"),
    (_wrapped({"DIRECTION": "Downstream", "MaxTrafficRate": 1}),
     ["provisioned_speed_down"],                                          "case-insensitive vocabulary"),
    ({"data": {"flows": [{"direction": "downstream", "maxTrafficRate": 1}]}},
     ["provisioned_speed_down"],                                          "nested array path"),
]
# fmt: on


@pytest.mark.parametrize("body,expected_fields,desc", DETECTION_CASES, ids=[c[2] for c in DETECTION_CASES])
def test_detects_expected_fields(body: dict[str, Any], expected_fields: list[str], desc: str) -> None:
    """Each (metric, observed direction) pair produces one aggregate, in stable order."""
    aggregates = detect_service_flow_aggregates(body)
    assert [a.field for a in aggregates] == expected_fields, desc


# =====================================================================
# Negative cases - table-driven
# =====================================================================

# fmt: off
NO_DETECTION_CASES = [
    # (body,                                                              desc)
    ({},                                                                  "empty body"),
    ({"serviceFlows": []},                                                "empty array"),
    ({"channels": [{"channelId": 1, "frequency": 500000000}]},            "channel array, no service flow keys"),
    (_wrapped({"maxTrafficRate": 1, "maxTrafficBurst": 2}),               "metrics without a direction key"),
    (_wrapped({"direction": "downstream", "scheduleType": "undefined"}),  "direction without a provisioned metric"),
    (_wrapped({"direction": "sideways", "maxTrafficRate": 1}),            "unrecognized direction spelling is skipped"),
]
# fmt: on


@pytest.mark.parametrize("body,desc", NO_DETECTION_CASES, ids=[c[1] for c in NO_DETECTION_CASES])
def test_produces_no_aggregates(body: dict[str, Any], desc: str) -> None:
    """Shapes that are not service flows must produce nothing, never a guess."""
    assert detect_service_flow_aggregates(body) == [], desc


# =====================================================================
# Emitted aggregate contents
# =====================================================================


def test_wrapper_key_becomes_item_path() -> None:
    """A uniform single-key wrapper is reported as item_path, not left in array_path."""
    aggregates = detect_service_flow_aggregates(_wrapped(DS_FLOW))
    assert aggregates[0].array_path == "serviceFlows"
    assert aggregates[0].item_path == "serviceFlow"


def test_flat_items_have_no_item_path() -> None:
    """An unwrapped array leaves item_path empty."""
    assert detect_service_flow_aggregates(_flat(DS_FLOW))[0].item_path == ""


def test_canonical_directions_need_no_map() -> None:
    """Firmware already spelling the canonical words emits no normalization map."""
    for aggregate in detect_service_flow_aggregates(_wrapped(DS_FLOW, US_FLOW)):
        assert aggregate.map == {}


def test_numeric_direction_codes_normalize_in_map() -> None:
    """Non-canonical spellings normalize in the aggregate's map, never in Core."""
    body = _wrapped(
        {"direction": "1", "maxTrafficRate": 10},
        {"direction": "2", "maxTrafficRate": 20},
    )
    aggregates = detect_service_flow_aggregates(body)
    assert [a.field for a in aggregates] == ["provisioned_speed_down", "provisioned_speed_up"]
    for aggregate in aggregates:
        assert aggregate.map == {"1": "downstream", "2": "upstream"}


def test_filter_values_are_strings() -> None:
    """Core rejects a numeric filter rule at config load; these compare as text."""
    body = _wrapped({"direction": 1, "maxTrafficRate": 10})
    for aggregate in detect_service_flow_aggregates(body):
        assert aggregate.filter == {"direction": "downstream"}
        for value in aggregate.filter.values():
            assert isinstance(value, str)


def test_filter_uses_observed_key_casing() -> None:
    """The filter key must match the wire, not the normalized lookup form."""
    aggregates = detect_service_flow_aggregates(_wrapped({"Direction": "downstream", "maxTrafficRate": 1}))
    assert aggregates[0].filter == {"Direction": "downstream"}


def test_max_uses_observed_key_casing() -> None:
    """The max key must match the wire so Core can look it up."""
    aggregates = detect_service_flow_aggregates(_wrapped({"direction": "downstream", "MAXTRAFFICRATE": 1}))
    assert aggregates[0].max == "MAXTRAFFICRATE"


def test_integer_metrics_type_as_integer() -> None:
    """Whole-number provisioned values carry no scale and type as integer."""
    assert detect_service_flow_aggregates(_wrapped(DS_FLOW))[0].type == "integer"


def test_fractional_metrics_type_as_float() -> None:
    """A fractional observed value types the aggregate as float."""
    body = _wrapped({"direction": "downstream", "maxTrafficRate": 1.5})
    assert detect_service_flow_aggregates(body)[0].type == "float"


# =====================================================================
# Round trip through Core - table-driven
# =====================================================================
#
# Field names alone do not prove a generated config works: Core compares
# filter rules byte for byte, so a wrong direction spelling matches no
# items and produces no value without erroring. These cases feed the
# detector's own output to the real parser and assert values come back.

# fmt: off
ROUND_TRIP_CASES = [
    # (wire direction words,          desc)
    (("downstream", "upstream"),      "canonical lowercase spelling"),
    (("Downstream", "Upstream"),      "capitalized spelling"),
    (("DOWNSTREAM", "UPSTREAM"),      "uppercase spelling"),
    (("1", "2"),                      "numeric-coded direction"),
]
# fmt: on


@pytest.mark.parametrize("directions,desc", ROUND_TRIP_CASES, ids=[c[1] for c in ROUND_TRIP_CASES])
def test_generated_aggregates_resolve_in_core(directions: tuple[str, str], desc: str) -> None:
    """Whatever the wire spelling, the emitted config must produce values in Core."""
    down, up = directions
    body = _wrapped(
        {"direction": down, "maxTrafficRate": 856000000},
        {"direction": up, "maxTrafficRate": 74900000},
    )

    aggregates = detect_service_flow_aggregates(body)
    source = JSONSystemInfoSource.model_validate(
        {
            "format": "json",
            "resource": RESOURCE,
            "fields": [],
            "child_aggregates": [a.to_dict() for a in aggregates],
        }
    )

    produced = JSONSystemInfoParser(source).parse({RESOURCE: body})
    assert produced == {
        "provisioned_speed_down": 856000000,
        "provisioned_speed_up": 74900000,
    }, desc


def test_serializes_to_the_parser_yaml_shape() -> None:
    """to_dict emits the model's key names, ready for parser.yaml."""
    aggregate = detect_service_flow_aggregates(_wrapped(DS_FLOW))[0]
    assert aggregate.to_dict() == {
        "array_path": "serviceFlows",
        "item_path": "serviceFlow",
        "filter": {"direction": "downstream"},
        "max": "maxTrafficRate",
        "field": "provisioned_speed_down",
        "type": "integer",
    }


def test_one_aggregate_per_metric_stem() -> None:
    """Two accepted spellings of one metric must not both write the same field."""
    body = _wrapped({"direction": "downstream", "maxTrafficRate": 10, "pMaxTrafficRate": 20})
    fields = [a.field for a in detect_service_flow_aggregates(body)]
    assert fields == ["provisioned_speed_down"]
