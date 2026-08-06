"""Tests for the unread-resource report.

Two concerns: the subtraction (an endpoint the generated config reads must
not be reported) and the redaction (no value from any response body may
reach the output). The #185 HAR is the live case for both — it fetched a
service flow resource nothing read, and it answers with MAC addresses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from solentlabs.cable_modem_monitor_catalog import CATALOG_PATH
from solentlabs.cable_modem_monitor_catalog_tools.analysis.actions.types import ActionDetail, ActionsDetail
from solentlabs.cable_modem_monitor_catalog_tools.analysis.auth.types import AuthDetail
from solentlabs.cable_modem_monitor_catalog_tools.analysis.unread_resources import (
    UnreadResource,
    detect_unread_resources,
)
from solentlabs.cable_modem_monitor_catalog_tools.analyze_har import AnalysisResult, analyze_har
from solentlabs.cable_modem_monitor_catalog_tools.fleet_scanner import scan_fleet
from solentlabs.cable_modem_monitor_core.har import load_har_json

_ZG_HAR = CATALOG_PATH / "sagemcom" / "f3896lg-zg" / "test_data" / "modem.har"

# Every leaf a skeleton may carry. Anything else is a leaked value.
_TYPE_NAMES = frozenset({"null", "bool", "int", "float", "str"})


@pytest.fixture(scope="module")
def zg_result() -> AnalysisResult:
    """Analysis of the committed F3896LG-ZG HAR (issue #185)."""
    return analyze_har(_ZG_HAR, fleet=scan_fleet(CATALOG_PATH))


@pytest.fixture(scope="module")
def zg_unread(zg_result: AnalysisResult) -> dict[str, UnreadResource]:
    """The ZG report keyed by path."""
    return {resource.path: resource for resource in zg_result.unread_resources}


# =====================================================================
# Subtraction — what the config reads is not reported
# =====================================================================


class TestSubtraction:
    """Resources the generated config consumes are absent from the report."""

    def test_eventlog_is_reported(self, zg_unread: dict[str, UnreadResource]) -> None:
        """Core has no event-log concept, so the endpoint is genuinely unread."""
        assert "/rest/v1/cablemodem/eventlog" in zg_unread

    def test_eventlog_skeleton(self, zg_unread: dict[str, UnreadResource]) -> None:
        """The event log's shape reaches the LLM as keys and types."""
        assert zg_unread["/rest/v1/cablemodem/eventlog"].shape == {
            "eventlog": [{"priority": "str", "time": "str", "message": "str"}]
        }

    def test_service_flows_are_not_reported(self, zg_unread: dict[str, UnreadResource]) -> None:
        """Intake now generates the service flow source, so it is consumed."""
        assert "/rest/v1/cablemodem/serviceflows" not in zg_unread

    @pytest.mark.parametrize(
        "path",
        [
            "/rest/v1/cablemodem/state_",
            "/rest/v1/cablemodem/downstream",
            "/rest/v1/cablemodem/upstream",
            "/rest/v1/system/info",
        ],
    )
    def test_mapped_resources_are_not_reported(self, zg_unread: dict[str, UnreadResource], path: str) -> None:
        """The four long-standing parser resources are read."""
        assert path not in zg_unread

    def test_login_endpoint_is_not_reported(self, zg_unread: dict[str, UnreadResource]) -> None:
        """The auth login endpoint is consumed, not unread."""
        assert "/rest/v1/user/login" not in zg_unread

    def test_non_2xx_is_not_reported(self, zg_unread: dict[str, UnreadResource]) -> None:
        """The capture's 403 and 503 are not candidates."""
        assert "/rest/v1/system/ui/cloudui" not in zg_unread
        assert "/rest/v1/wifi/capabilities" not in zg_unread

    def test_static_assets_are_not_reported(self, zg_unread: dict[str, UnreadResource]) -> None:
        """The CSS and JS bundles never appear."""
        assert not [path for path in zg_unread if path.endswith((".js", ".css"))]

    def test_report_is_not_a_gate(self, zg_result: AnalysisResult) -> None:
        """Unread resources produce no hard stop and no core gap."""
        assert zg_result.unread_resources
        assert zg_result.hard_stops == []
        assert zg_result.core_gaps == []

    def test_always_serialized(self, zg_result: AnalysisResult) -> None:
        """The field rides in the MCP contract alongside warnings."""
        assert isinstance(zg_result.to_dict()["unread_resources"], list)


class TestPlaceholderEndpoints:
    """A runtime-templated endpoint never equals the captured path."""

    def test_logout_placeholders_match_captured_path(self) -> None:
        """DELETE /rest/v1/user/3/token/abc is the config's {auth:...} logout."""
        entries = _json_entry("http://192.168.100.1/rest/v1/user/3/token/abc", {"ok": True})
        actions = ActionsDetail(logout=_logout("/rest/v1/user/{auth:user_id}/token/{auth:token}"))
        assert detect_unread_resources(entries, None, AuthDetail(strategy="none"), actions, "http") == []

    def test_placeholder_segment_count_must_match(self) -> None:
        """A shorter path is not swallowed by a templated endpoint."""
        entries = _json_entry("http://192.168.100.1/rest/v1/user/3", {"ok": True})
        actions = ActionsDetail(logout=_logout("/rest/v1/user/{auth:user_id}/token/{auth:token}"))
        unread = detect_unread_resources(entries, None, AuthDetail(strategy="none"), actions, "http")
        assert [resource.path for resource in unread] == ["/rest/v1/user/3"]


# =====================================================================
# Redaction — no response body value reaches the output
# =====================================================================


def _shape_keys(node: Any) -> list[str]:
    """Every dict key in a skeleton."""
    if isinstance(node, dict):
        return [key for k, v in node.items() for key in [k, *_shape_keys(v)]]
    if isinstance(node, list):
        return [key for value in node for key in _shape_keys(value)]
    return []


def _shape_leaves(node: Any) -> list[Any]:
    """Every non-container value in a skeleton."""
    if isinstance(node, dict):
        return [leaf for value in node.values() for leaf in _shape_leaves(value)]
    if isinstance(node, list):
        return [leaf for value in node for leaf in _shape_leaves(value)]
    return [node]


def _body_scalars(node: Any) -> list[Any]:
    """Every scalar value in a response body; keys excluded."""
    if isinstance(node, dict):
        return [scalar for value in node.values() for scalar in _body_scalars(value)]
    if isinstance(node, list):
        return [scalar for value in node for scalar in _body_scalars(value)]
    return [node]


def _wire_values_by_path(har_path: Path) -> dict[str, set[str]]:
    """Every scalar value each path answered with, as strings."""
    by_path: dict[str, set[str]] = {}
    for entry in load_har_json(har_path)["log"]["entries"]:
        text = entry.get("response", {}).get("content", {}).get("text", "") or ""
        try:
            body = json.loads(text)
        except ValueError:
            continue
        url = entry.get("request", {}).get("url", "")
        path = url[url.find("/", url.find("://") + 3) :].split("?", 1)[0] if "://" in url else url
        values = by_path.setdefault(path, set())
        values.update(str(scalar) for scalar in _body_scalars(body) if scalar is not None)
    return by_path


class TestNoValuesLeak:
    """Keys and types only. Values are where MACs and serial numbers live."""

    def test_every_leaf_is_a_type_name(self, zg_result: AnalysisResult) -> None:
        """No skeleton leaf is anything but a type name or a union of them."""
        for resource in zg_result.unread_resources:
            for leaf in _shape_leaves(resource.shape):
                assert set(str(leaf).split("|")) <= _TYPE_NAMES, f"{resource.path}: {leaf!r}"

    def test_no_entry_carries_a_value_from_its_own_body(self, zg_result: AnalysisResult) -> None:
        """Nothing an entry emits — key, leaf, path — appears as a value in the body it came from.

        Scoped per endpoint on purpose. A key colliding with an unrelated
        value on some other endpoint says nothing; a key that equals a
        value in its own body is how a map keyed by MAC or serial number
        would leak.
        """
        wire = _wire_values_by_path(_ZG_HAR)
        for resource in zg_result.unread_resources:
            emitted = {resource.path, resource.content_type, str(resource.status)}
            emitted.update(_shape_keys(resource.shape))
            emitted.update(str(leaf) for leaf in _shape_leaves(resource.shape))
            leaked = (emitted - _TYPE_NAMES) & wire[resource.path]
            assert not leaked, f"{resource.path} leaked {sorted(leaked)}"

    def test_mac_address_key_survives_without_its_value(self, zg_unread: dict[str, UnreadResource]) -> None:
        """The provisioning endpoint reports that it carries a MAC, not which one."""
        shape = zg_unread["/rest/v1/system/gateway/provisioning"].shape
        assert shape["provisioning"]["macAddress"] == "str"


# =====================================================================
# Shape reduction — inline synthetic bodies
# =====================================================================


def _logout(endpoint: str) -> ActionDetail:
    """A minimal HTTP logout action carrying only an endpoint."""
    return ActionDetail(type="http", method="DELETE", endpoint=endpoint)


def _json_entry(url: str, body: Any) -> list[dict[str, Any]]:
    """One HAR entry answering 200 with a JSON body."""
    return [
        {
            "request": {"method": "GET", "url": url},
            "response": {
                "status": 200,
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "content": {"mimeType": "application/json", "text": json.dumps(body)},
            },
        }
    ]


def _shape_of_body(body: Any) -> Any:
    """Run one synthetic JSON response through the reporter and return its skeleton."""
    entries = _json_entry("http://192.168.100.1/data.json", body)
    unread = detect_unread_resources(entries, None, AuthDetail(strategy="none"), ActionsDetail(), "http")
    assert len(unread) == 1
    return unread[0].shape


class TestShapeReduction:
    """Nested shape is preserved; heterogeneous arrays merge to a union."""

    def test_nested_object_in_array(self) -> None:
        """serviceFlows[].serviceFlow.{...} keeps both levels."""
        body = {"serviceFlows": [{"serviceFlow": {"direction": "downstream", "maxTrafficRate": 856000000}}]}
        flow = {"serviceFlow": {"direction": "str", "maxTrafficRate": "int"}}
        assert _shape_of_body(body) == {"serviceFlows": [flow]}

    def test_heterogeneous_array_keys_union(self) -> None:
        """A key only the second variant carries still reaches the LLM."""
        body = {"channels": [{"id": 1}, {"id": 2, "fftType": "4K"}]}
        assert _shape_of_body(body) == {"channels": [{"id": "int", "fftType": "str"}]}

    def test_conflicting_types_union(self) -> None:
        """A key typed differently across items reports both types."""
        body = {"channels": [{"power": None}, {"power": 1.5}]}
        assert _shape_of_body(body) == {"channels": [{"power": "float|null"}]}

    def test_empty_array(self) -> None:
        """An empty array is reported as such, not guessed at."""
        assert _shape_of_body({"channels": []}) == {"channels": []}

    def test_scalar_array(self) -> None:
        """An array of scalars reports its element type."""
        assert _shape_of_body({"languages": ["en", "nl"]}) == {"languages": ["str"]}

    def test_top_level_array(self) -> None:
        """A body that is itself an array still reduces."""
        assert _shape_of_body([{"id": 1}]) == [{"id": "int"}]

    def test_bool_is_not_int(self) -> None:
        """bool is checked before int, which it subclasses in Python."""
        assert _shape_of_body({"enable": True}) == {"enable": "bool"}
