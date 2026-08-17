"""Login POST query-shape enforcement — routes builder and server rejection.

A capture whose login POSTs carry a query string (a dynamic form action
like ``/goform/login?id=NNN``) is a statement about the request the
firmware accepts. A bare-path login POST must not silently match that
route — same one-directional honesty rule as ``unrecorded_body_keys``,
one field over. Param *names* are compared, never values: the id changes
per page load, so byte equality against a stale capture would fail
correct code.

TEST DATA TABLES
================
Shape-collection cases live in ``fixtures/login_query_shapes_*.json``,
one file per case: ``login_path`` and ``_entries`` in,
``expected_shapes`` out (a list of param-name lists — JSON for a set of
frozensets). Adding a case = adding a file. The enforcement tests load
the same fixtures as server scaffolding.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import requests
from solentlabs.cable_modem_monitor_core.models.modem_config import ModemConfig
from solentlabs.cable_modem_monitor_core.test_harness.routes import build_login_query_shapes
from solentlabs.cable_modem_monitor_core.test_harness.server import HARMockServer

from tests._helpers import load_fixture

FIXTURES_DIR = Path(__file__).parent / "fixtures"

SHAPE_FIXTURES = sorted(FIXTURES_DIR.glob("login_query_shapes_*.json"))


def _entries_from(name: str) -> list[dict[str, Any]]:
    """Load a shape fixture's HAR entries for server scaffolding."""
    return list(load_fixture(FIXTURES_DIR / name)["_entries"])


def _make_config(data: dict[str, Any]) -> ModemConfig:
    """Validate a raw modem config dict, filling required identity fields."""
    from solentlabs.cable_modem_monitor_core.config_loader import validate_modem_config

    defaults = {
        "manufacturer": "Solent Labs",
        "model": "T100",
        "transport": "http",
        "default_host": "192.168.100.1",
        "status": "unsupported",
    }
    return validate_modem_config({**defaults, **data})


class TestBuildLoginQueryShapes:
    """Shape collection from captured login POSTs, one fixture per case."""

    @pytest.mark.parametrize("fixture_path", SHAPE_FIXTURES, ids=lambda p: p.stem)
    def test_shapes(self, fixture_path: Path) -> None:
        data = load_fixture(fixture_path)
        expected = frozenset(frozenset(shape) for shape in data["expected_shapes"])
        assert build_login_query_shapes(data["_entries"], data["login_path"]) == expected


class TestLoginQueryShapeEnforcement:
    """Server rejects login POSTs whose query shape the capture never recorded."""

    @pytest.fixture()
    def form_config(self) -> ModemConfig:
        return _make_config({"auth": {"strategy": "form", "action": "/goform/login", "cookie_name": "s"}})

    def test_bare_post_rejected_when_capture_has_query(self, form_config: ModemConfig) -> None:
        """A bare login POST against a ?id= capture fails as dishonest."""
        entries = _entries_from("login_query_shapes_dynamic_id.json")
        with HARMockServer(entries, modem_config=form_config) as server:
            resp = requests.post(f"{server.base_url}/goform/login", timeout=5)
        assert resp.status_code == 500
        assert "query" in resp.text

    def test_same_param_names_match_despite_different_value(self, form_config: ModemConfig) -> None:
        """The id changes per page load — names match, values need not."""
        entries = _entries_from("login_query_shapes_dynamic_id.json")
        with HARMockServer(entries, modem_config=form_config) as server:
            resp = requests.post(
                f"{server.base_url}/goform/login?id=999",
                timeout=5,
                allow_redirects=False,
            )
        assert resp.status_code == 302

    def test_bare_post_still_matches_bare_capture(self, form_config: ModemConfig) -> None:
        """Static-action modems are untouched: bare capture, bare POST."""
        entries = _entries_from("login_query_shapes_bare_login.json")
        with HARMockServer(entries, modem_config=form_config) as server:
            resp = requests.post(
                f"{server.base_url}/goform/login",
                timeout=5,
                allow_redirects=False,
            )
        assert resp.status_code == 302

    def test_data_page_tier3_fallback_unchanged(self, form_config: ModemConfig) -> None:
        """Incidental captured query on a GET data page still serves a bare GET."""
        entries = _entries_from("login_query_shapes_incidental_query_data_page.json")
        with HARMockServer(entries, modem_config=form_config) as server:
            requests.post(f"{server.base_url}/goform/login", timeout=5, allow_redirects=False)
            resp = requests.get(f"{server.base_url}/status.htm", timeout=5)
        assert resp.status_code == 200
