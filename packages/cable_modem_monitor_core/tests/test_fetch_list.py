"""Tests for fetch list derivation from parser.yaml and parser.py.

TEST DATA TABLES
================
parser.py ``resources`` declaration cases are table-driven; tables are
defined at the top of the file with ASCII box-drawing comments.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from solentlabs.cable_modem_monitor_core.fetch_list import (
    ResourceTarget,
    collect_fetch_targets,
)
from solentlabs.cable_modem_monitor_core.models.parser_config import ParserConfig

from tests._helpers import load_fixture

FIXTURES_DIR = Path(__file__).parent / "models" / "fixtures" / "parser_config" / "valid"
LOCAL_FIXTURES_DIR = Path(__file__).parent / "fixtures"

# =============================================================================
# Test Data Tables
# =============================================================================

# ┌──────────────────────────────────┬───────────────────────────────────┬──────────────────────────────┐
# │ declared resources               │ expected (path, format) set       │ description                  │
# ├──────────────────────────────────┼───────────────────────────────────┼──────────────────────────────┤
# │ two new paths                    │ yaml target + both declared       │ hooks declare new paths      │
# │ yaml path, different format      │ yaml target only, yaml format     │ parser.yaml wins on overlap  │
# │ _ABSENT (no attribute)           │ yaml target only                  │ attribute is optional        │
# │ None post_processor              │ yaml target only                  │ no parser.py at all          │
# │ {}                               │ yaml target only                  │ empty declaration is a no-op │
# └──────────────────────────────────┴───────────────────────────────────┴──────────────────────────────┘
#
# The yaml fixture (table_single.json) maps /status.html with format "table".
_ABSENT = object()  # sentinel: PostProcessor defines no resources attribute
#
# fmt: off
POST_PROCESSOR_RESOURCES_CASES = [
    # (declared,                 expected_targets,            description)
    ({"/extra.json": "json", "/extra.html": "table"},
     {("/status.html", "table"), ("/extra.json", "json"), ("/extra.html", "table")},
     "hooks declare new paths"),
    ({"/status.html": "json"},   {("/status.html", "table")}, "parser.yaml wins on overlap"),
    (_ABSENT,                    {("/status.html", "table")}, "attribute is optional"),
    (None,                       {("/status.html", "table")}, "no parser.py at all"),
    ({},                         {("/status.html", "table")}, "empty declaration is a no-op"),
]

INVALID_RESOURCES_CASES = [
    # (declared,               description)
    (["/extra.json"],          "list instead of dict"),
    ({"/extra.json": 42},      "non-string format value"),
    ({3: "json"},              "non-string path key"),
]
# fmt: on


def _make_post_processor(declared: Any) -> Any:
    """Build a duck-typed PostProcessor for a table case."""
    if declared is None:
        return None
    if declared is _ABSENT:
        return object()
    return SimpleNamespace(resources=declared)


def _table_single_config() -> ParserConfig:
    """Load the shared single-table parser config fixture."""
    data = load_fixture(FIXTURES_DIR / "table_single.json")
    return ParserConfig.model_validate(data)


class TestCollectFetchTargets:
    """Fetch list derivation from ParserConfig."""

    def test_table_single_resource(self) -> None:
        """Single table section produces one resource target."""
        data = load_fixture(FIXTURES_DIR / "table_single.json")
        config = ParserConfig.model_validate(data)
        targets = collect_fetch_targets(config)

        assert len(targets) == 1
        assert targets[0].path == data["downstream"]["resource"]
        assert targets[0].format == "table"

    def test_downstream_and_upstream_same_resource(self) -> None:
        """Duplicate paths are deduplicated."""
        data = load_fixture(FIXTURES_DIR / "upstream_table.json")
        config = ParserConfig.model_validate(data)
        targets = collect_fetch_targets(config)

        paths = [t.path for t in targets]
        # Should have unique paths only
        assert len(paths) == len(set(paths))

    def test_system_info_resources_collected(self) -> None:
        """System info sources add to the fetch list."""
        data = load_fixture(FIXTURES_DIR / "system_info_html_fields.json")
        config = ParserConfig.model_validate(data)
        targets = collect_fetch_targets(config)

        formats = {t.format for t in targets}
        assert "html_fields" in formats

    def test_hnap_sections_skipped(self) -> None:
        """HNAP sections are excluded from HTTP fetch list."""
        data = load_fixture(FIXTURES_DIR / "hnap_downstream.json")
        config = ParserConfig.model_validate(data)
        targets = collect_fetch_targets(config)

        # HNAP sections have no resource paths -- skip entirely
        assert len(targets) == 0

    def test_javascript_format(self) -> None:
        """JavaScript sections produce resource targets."""
        data = load_fixture(FIXTURES_DIR / "javascript_single_function.json")
        config = ParserConfig.model_validate(data)
        targets = collect_fetch_targets(config)

        assert len(targets) >= 1
        formats = {t.format for t in targets}
        assert "javascript" in formats

    def test_json_format(self) -> None:
        """JSON sections produce resource targets."""
        data = load_fixture(FIXTURES_DIR / "json_downstream.json")
        config = ParserConfig.model_validate(data)
        targets = collect_fetch_targets(config)

        assert len(targets) >= 1
        formats = {t.format for t in targets}
        assert "json" in formats

    def test_mixed_formats_deduplicated(self) -> None:
        """Multiple sections with same path keep first format."""
        data = load_fixture(LOCAL_FIXTURES_DIR / "parser_config_shared_resource.json")
        config = ParserConfig.model_validate(data)
        targets = collect_fetch_targets(config)

        # Same resource path -- deduplicated to one target
        assert len(targets) == 1
        assert targets[0].path == "/status.html"
        assert targets[0].format == "table"

    def test_empty_config(self) -> None:
        """Config with no sections produces empty fetch list."""
        # system_info only, with HNAP source
        data = load_fixture(FIXTURES_DIR / "system_info_hnap.json")
        config = ParserConfig.model_validate(data)
        targets = collect_fetch_targets(config)

        # HNAP system info has no resource path
        assert len(targets) == 0

    def test_xml_multi_table_resources(self) -> None:
        """XML tables[] produces one target per unique resource."""
        data = load_fixture(FIXTURES_DIR / "xml_multi_table.json")
        config = ParserConfig.model_validate(data)
        targets = collect_fetch_targets(config)

        paths = sorted(t.path for t in targets)
        assert paths == ["10", "9"]
        assert all(t.format == "xml" for t in targets)

    def test_xml_single_table_resource(self) -> None:
        """XML section with one table produces one target."""
        data = load_fixture(FIXTURES_DIR / "xml_downstream.json")
        config = ParserConfig.model_validate(data)
        targets = collect_fetch_targets(config)

        assert len(targets) == 1
        assert targets[0].path == "10"
        assert targets[0].format == "xml"

    def test_json_per_array_resources(self) -> None:
        """JSON arrays with per-array resources produce one target per unique resource."""
        data = load_fixture(FIXTURES_DIR / "json_multi_resource_arrays.json")
        config = ParserConfig.model_validate(data)
        targets = collect_fetch_targets(config)

        paths = sorted(t.path for t in targets)
        assert paths == ["/api/ofdm", "/api/qam"]
        assert all(t.format == "json" for t in targets)
        assert all(t.encoding == "base64" for t in targets)

    def test_resource_target_is_frozen(self) -> None:
        """ResourceTarget is immutable."""
        target = ResourceTarget(path="/test.html", format="table")
        with pytest.raises(AttributeError):
            target.path = "/other.html"  # type: ignore[misc]


class TestPostProcessorResources:
    """parser.py resources declarations merged into the fetch list."""

    @pytest.mark.parametrize(
        "declared,expected_targets,desc",
        POST_PROCESSOR_RESOURCES_CASES,
        ids=[c[2] for c in POST_PROCESSOR_RESOURCES_CASES],
    )
    def test_declared_resources(self, declared: Any, expected_targets: set[tuple[str, str]], desc: str) -> None:
        """Declared paths are fetched; parser.yaml wins on overlap."""
        targets = collect_fetch_targets(_table_single_config(), _make_post_processor(declared))
        assert {(t.path, t.format) for t in targets} == expected_targets, f"Failed: {desc}"

    @pytest.mark.parametrize(
        "declared,desc",
        INVALID_RESOURCES_CASES,
        ids=[c[1] for c in INVALID_RESOURCES_CASES],
    )
    def test_invalid_declaration_fails_fast(self, declared: Any, desc: str) -> None:
        """A wrongly shaped resources declaration raises at startup."""
        with pytest.raises(TypeError, match="resources"):
            collect_fetch_targets(_table_single_config(), _make_post_processor(declared))
