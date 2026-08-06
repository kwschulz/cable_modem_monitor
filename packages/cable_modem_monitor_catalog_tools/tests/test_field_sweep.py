"""Tests for the catalog field sweep.

Covers the two halves independently — what a committed entry counts as
extracted, and what a capture is read to carry — then the gap between
them. Inline configs and bodies throughout: the sweep is generic over
modems, so a fixture would only add indirection.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from solentlabs.cable_modem_monitor_catalog_tools.analysis.field_sweep import (
    captured_fields,
    extracted_fields,
    sweep_entry,
)


def _entry(body: str, content_type: str = "application/json", *, encoding: str = "") -> dict[str, Any]:
    """Build a minimal 200 HAR entry carrying one response body."""
    content: dict[str, Any] = {"text": body}
    if encoding:
        content["encoding"] = encoding
    return {
        "request": {"url": "https://192.168.100.1/data"},
        "response": {
            "status": 200,
            "headers": [{"name": "Content-Type", "value": content_type}],
            "content": content,
        },
    }


# =====================================================================
# What the committed entry extracts
# =====================================================================


class TestExtractedFields:
    """Every parser.yaml form that populates a field must be recognized."""

    @pytest.mark.parametrize(
        ("section", "expected"),
        [
            pytest.param(
                {"fields": [{"key": "a", "field": "power", "type": "float"}]},
                {"power"},
                id="field-line",
            ),
            pytest.param(
                {"channel_type": {"fixed": "qam"}},
                {"channel_type"},
                id="channel-type-fixed",
            ),
            pytest.param(
                {"channel_type": {"field": "modulation", "map": {"QAM256": "qam"}}},
                {"channel_type"},
                id="channel-type-derived-does-not-claim-its-source",
            ),
            pytest.param(
                {"fixed_fields": {"lock_status": "locked"}},
                {"lock_status"},
                id="fixed-fields",
            ),
            pytest.param(
                {"tables": [{"lock_status": {"all_of": ["IsLocked"]}}]},
                {"lock_status"},
                id="xml-lock-status-derivation",
            ),
            pytest.param(
                {"filter": {"channel_type": {"not": 0}}},
                set(),
                id="filter-names-without-populating",
            ),
            pytest.param(
                {"arrays": [{"fields": [{"field": "snr", "type": "float"}]}]},
                {"snr"},
                id="nested-under-arrays",
            ),
        ],
    )
    def test_channel_declaration_forms(self, section: dict[str, Any], expected: set[str]) -> None:
        """Each declaration form contributes exactly the field it populates."""
        channel, _ = extracted_fields({"downstream": section}, None)
        assert channel == expected

    def test_upstream_and_downstream_are_one_channel_namespace(self) -> None:
        """A channel field counts as extracted when either direction populates it."""
        config = {
            "downstream": {"fields": [{"field": "snr", "type": "float"}]},
            "upstream": {"fields": [{"field": "symbol_rate", "type": "integer"}]},
        }
        channel, _ = extracted_fields(config, None)
        assert channel == {"snr", "symbol_rate"}

    def test_aggregate_and_computed_name_their_outputs(self) -> None:
        """Derived system_info blocks name their output as the mapping key."""
        config = {
            "aggregate": {"total_corrected": {"sum": "corrected", "channels": "downstream"}},
            "computed": {"memory_used": {"operation": "percent_used", "inputs": {}}},
        }
        _, system = extracted_fields(config, None)
        assert {"total_corrected", "memory_used"} <= system

    def test_golden_covers_post_processor_output(self) -> None:
        """A field only a parser.py writes still counts as extracted."""
        golden = {
            "downstream": [{"channel_id": 1, "channel_width": 96_000_000}],
            "system_info": {"model_name": "example"},
        }
        channel, system = extracted_fields({}, golden)
        assert "channel_width" in channel
        assert "model_name" in system


# =====================================================================
# What the capture carries
# =====================================================================


class TestCapturedFields:
    """Key collection is shape-aware: array context is channel context."""

    def test_json_array_keys_are_channel_context(self) -> None:
        """Keys inside an array of objects resolve against the channel vocabulary."""
        body = json.dumps({"channels": [{"channelId": 1, "rxMer": 40.1}]})
        channel, _ = captured_fields([_entry(body)])
        assert channel["channel_id"] == {"channelId"}
        assert channel["snr"] == {"rxMer"}

    def test_json_scalar_keys_are_system_info_context(self) -> None:
        """Keys outside any array resolve against the system_info vocabulary."""
        body = json.dumps({"info": {"softwareVersion": "1.0", "docsisVersion": "3.1"}})
        _, system = captured_fields([_entry(body)])
        assert system["software_version"] == {"softwareVersion"}
        assert system["docsis_version"] == {"docsisVersion"}

    def test_top_level_json_array_is_channel_context(self) -> None:
        """A body that is a bare array is still read as channel data."""
        body = json.dumps([{"channelId": 1}])
        channel, _ = captured_fields([_entry(body)])
        assert channel["channel_id"] == {"channelId"}

    def test_service_flow_shape_reports_directional_fields(self) -> None:
        """Provisioned maxima are recognized by array shape, not by a single key."""
        body = json.dumps(
            {
                "serviceFlows": [
                    {"direction": "downstream", "maxTrafficRate": 1},
                    {"direction": "upstream", "maxTrafficRate": 2},
                ]
            }
        )
        _, system = captured_fields([_entry(body)])
        assert {"provisioned_speed_down", "provisioned_speed_up"} <= set(system)

    def test_xml_repeated_siblings_are_channel_context(self) -> None:
        """Everything below a repeated element stays channel context."""
        body = (
            '<?xml version="1.0"?><table>'
            "<row><channelId>1</channelId><snr>40</snr></row>"
            "<row><channelId>2</channelId><snr>41</snr></row>"
            "</table>"
        )
        channel, system = captured_fields([_entry(body, "text/xml")])
        assert channel["channel_id"] == {"channelId"}
        assert channel["snr"] == {"snr"}
        assert not system

    def test_xml_singleton_elements_are_system_info_context(self) -> None:
        """A non-repeated element carries page-level data, not channel data."""
        body = '<?xml version="1.0"?><status><docsisVersion>3.1</docsisVersion></status>'
        _, system = captured_fields([_entry(body, "text/xml")])
        assert system["docsis_version"] == {"docsisVersion"}

    def test_html_channel_table_headers_are_collected(self) -> None:
        """Headers of a recognized channel table resolve to channel fields."""
        body = (
            "<html><body><table>"
            "<tr><th>Channel ID</th><th>Symbol Rate</th></tr>"
            "<tr><td>1</td><td>5120</td></tr>"
            "</table></body></html>"
        )
        channel, _ = captured_fields([_entry(body, "text/html")])
        assert channel["channel_id"] == {"Channel ID"}
        assert channel["symbol_rate"] == {"Symbol Rate"}

    def test_html_non_channel_table_is_skipped(self) -> None:
        """A table with no channel vocabulary contributes no channel keys."""
        body = (
            "<html><body><table>"
            "<tr><th>Interface</th><th>Bytes Sent</th></tr>"
            "<tr><td>eth0</td><td>12</td></tr>"
            "</table></body></html>"
        )
        channel, _ = captured_fields([_entry(body, "text/html")])
        assert not channel

    def test_html_single_match_table_is_skipped(self) -> None:
        """A boot-progress table matching only "Status" is not channel data."""
        body = (
            "<html><body><table>"
            "<tr><th>Task</th><th>Status</th></tr>"
            "<tr><td>DOCSIS Ranging</td><td>Done</td></tr>"
            "</table></body></html>"
        )
        channel, _ = captured_fields([_entry(body, "text/html")])
        assert "lock_status" not in channel

    def test_js_embedded_json_array_is_channel_context(self) -> None:
        """A JSON array assigned to a script variable carries channel keys."""
        body = '<html><body><script>var ds = [{"channelId": "1", "rxMer": "40"}];</script></body></html>'
        channel, _ = captured_fields([_entry(body, "text/html")])
        assert channel["channel_id"] == {"channelId"}

    def test_malformed_xml_is_skipped(self) -> None:
        """An unparseable XML body yields no keys rather than raising."""
        channel, system = captured_fields([_entry("<?xml version='1.0'?><open>", "text/xml")])
        assert not channel
        assert not system

    def test_base64_body_is_decoded(self) -> None:
        """A base64-encoded response is read, not skipped as opaque text."""
        body = base64.b64encode(json.dumps({"info": {"softwareVersion": "1.0"}}).encode()).decode()
        _, system = captured_fields([_entry(body, encoding="base64")])
        assert system["software_version"] == {"softwareVersion"}

    @pytest.mark.parametrize(
        ("entry", "reason"),
        [
            pytest.param(
                {"request": {"url": "https://h/x"}, "response": {"status": 401, "content": {"text": "{}"}}},
                "non-200",
                id="non-200",
            ),
            pytest.param(
                _entry("", "application/json"),
                "empty body",
                id="empty-body",
            ),
            pytest.param(
                {
                    "request": {"url": "https://h/app.js"},
                    "response": {
                        "status": 200,
                        "headers": [{"name": "Content-Type", "value": "application/json"}],
                        "content": {"text": '{"channels": [{"channelId": 1}]}'},
                    },
                },
                "static resource",
                id="static-resource",
            ),
        ],
    )
    def test_ignored_responses(self, entry: dict[str, Any], reason: str) -> None:
        """Responses that cannot carry catalog data yield no keys."""
        channel, system = captured_fields([entry])
        assert not channel, reason
        assert not system, reason


# =====================================================================
# The gap
# =====================================================================


class TestSweepEntry:
    """End to end: a captured registry field the entry never populates."""

    def test_reports_unmapped_field(self) -> None:
        """A captured channel field absent from the config is a finding."""
        body = json.dumps({"channels": [{"channelId": 1, "rxMer": 40.1}]})
        config = {"downstream": {"fields": [{"key": "channelId", "field": "channel_id", "type": "integer"}]}}
        gaps = sweep_entry(config, None, [_entry(body)])
        assert [(g.section, g.field, g.capture_keys) for g in gaps] == [("channel", "snr", ("rxMer",))]

    def test_quiet_when_field_is_mapped(self) -> None:
        """A field the config populates produces no finding."""
        body = json.dumps({"channels": [{"channelId": 1}]})
        config = {"downstream": {"fields": [{"key": "channelId", "field": "channel_id", "type": "integer"}]}}
        assert sweep_entry(config, None, [_entry(body)]) == []

    def test_quiet_when_only_the_golden_shows_the_field(self) -> None:
        """A post-processor field is extracted even with no config line for it."""
        body = json.dumps({"channels": [{"channelId": 1, "rxMer": 40.1}]})
        golden = {"downstream": [{"channel_id": 1, "snr": 40.1}]}
        assert sweep_entry({}, golden, [_entry(body)]) == []

    def test_finding_serializes_for_json_output(self) -> None:
        """A finding round-trips to the plain dict the --json report emits."""
        body = json.dumps({"channels": [{"rxMer": 40.1}]})
        (gap,) = sweep_entry({}, None, [_entry(body)])
        assert gap.to_dict() == {"section": "channel", "field": "snr", "capture_keys": ["rxMer"]}

    def test_unregistered_keys_are_never_reported(self) -> None:
        """A key with no canonical home is out of scope, not a finding."""
        body = json.dumps({"info": {"serialNumber": "X", "macAddress": "Y"}})
        assert sweep_entry({}, None, [_entry(body)]) == []
