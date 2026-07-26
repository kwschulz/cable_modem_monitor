"""Tests for the pipeline runner.

Integration tests using ``tmp_path`` modem directories and real
mock servers. Covers: happy path (pass), golden file mismatch
(failure), missing golden file (error), auth error, and
PostProcessor pipeline integration.

Loader unit tests for ``load_post_processor`` in isolation live in
``tests/test_post_processor.py``.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
from solentlabs.cable_modem_monitor_core.test_harness.discovery import (
    ModemTestCase,
    RestartTestCase,
    discover_modem_tests,
)
from solentlabs.cable_modem_monitor_core.test_harness.runner import (
    run_modem_restart_test,
    run_modem_test,
    run_modem_test_orchestrated,
)

from tests._helpers import load_fixture

# ---------------------------------------------------------------------------
# Fixture paths — shared pipeline fixtures
# ---------------------------------------------------------------------------

_PIPELINE_FIXTURES = Path(__file__).parent.parent / "fixtures" / "pipeline"

_MODEM_YAML = (_PIPELINE_FIXTURES / "modem.yaml").read_text()
_MODEM_WITH_RESTART_YAML = (_PIPELINE_FIXTURES / "modem_with_restart.yaml").read_text()
_MODEM_FORM_AUTH_YAML = (_PIPELINE_FIXTURES / "modem_form_auth.yaml").read_text()
_MODEM_SESSION_HEADERS_YAML = (_PIPELINE_FIXTURES / "modem_session_headers.yaml").read_text()
_MODEM_URL_TOKEN_YAML = (_PIPELINE_FIXTURES / "modem_url_token.yaml").read_text()
_MODEM_HNAP_YAML = (_PIPELINE_FIXTURES / "modem_hnap.yaml").read_text()
_PARSER_YAML = (_PIPELINE_FIXTURES / "parser.yaml").read_text()
_PARSER_HNAP_YAML = (_PIPELINE_FIXTURES / "parser_hnap.yaml").read_text()
_HAR_DATA: dict[str, Any] = load_fixture(_PIPELINE_FIXTURES / "har_2ch.json")
_HAR_RESTART_DATA: dict[str, Any] = load_fixture(_PIPELINE_FIXTURES / "har_restart.json")
_HAR_HNAP_DATA: dict[str, Any] = load_fixture(_PIPELINE_FIXTURES / "har_hnap_2ch.json")
_GOLDEN_FILE: dict[str, Any] = load_fixture(_PIPELINE_FIXTURES / "golden_2ch.json")
_GOLDEN_HNAP_FILE: dict[str, Any] = load_fixture(_PIPELINE_FIXTURES / "golden_hnap_2ch.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_test_dir(
    tmp_path: Path,
    *,
    modem_yaml: str = _MODEM_YAML,
    parser_yaml: str | None = _PARSER_YAML,
    parser_py: str | None = None,
    har_data: dict[str, Any] = _HAR_DATA,
    golden: dict[str, Any] | None = _GOLDEN_FILE,
) -> ModemTestCase:
    """Build a modem dir, discover, and return the single test case."""
    modem_dir = tmp_path / "modems" / "solentlabs" / "t100"
    tests_dir = modem_dir / "test_data"
    tests_dir.mkdir(parents=True)

    (modem_dir / "modem.yaml").write_text(modem_yaml)
    if parser_yaml is not None:
        (modem_dir / "parser.yaml").write_text(parser_yaml)
    if parser_py is not None:
        (modem_dir / "parser.py").write_text(parser_py)

    (tests_dir / "modem.har").write_text(json.dumps(har_data))
    if golden is not None:
        (tests_dir / "modem.expected.json").write_text(json.dumps(golden))

    cases = discover_modem_tests(modem_dir)
    assert len(cases) == 1
    return cases[0]


# ---------------------------------------------------------------------------
# Happy path — pipeline passes
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Pipeline runs and golden file matches."""

    def test_pass(self, tmp_path: Path) -> None:
        """Full pipeline: mock server -> auth -> fetch -> parse -> compare."""
        case = _build_test_dir(tmp_path)

        result = run_modem_test(case)

        assert result.passed is True
        assert result.error == ""
        assert result.comparison is not None
        assert result.comparison.passed is True
        assert result.test_name == case.name


# ---------------------------------------------------------------------------
# Golden file failures (pipeline ran, output differs)
# ---------------------------------------------------------------------------


class TestGoldenFileMismatch:
    """Pipeline succeeds but output doesn't match golden file."""

    def test_wrong_frequency(self, tmp_path: Path) -> None:
        """Mismatched frequency produces a diff, not an error."""
        bad_golden = {
            "downstream": [
                {"channel_id": 1, "frequency": 999000000, "power": 2.5},
                {"channel_id": 2, "frequency": 513000000, "power": 2.6},
            ],
            "upstream": [],
        }
        case = _build_test_dir(tmp_path, golden=bad_golden)

        result = run_modem_test(case)

        assert result.passed is False
        assert result.error == ""
        assert result.comparison is not None
        assert result.comparison.passed is False
        assert len(result.comparison.diffs) > 0
        # Verify the diff points to the right field
        paths = [d.path for d in result.comparison.diffs]
        assert "downstream[0].frequency" in paths

    def test_wrong_channel_count(self, tmp_path: Path) -> None:
        """Different number of channels is a failure, not an error."""
        short_golden = {
            "downstream": [
                {"channel_id": 1, "frequency": 507000000, "power": 2.5},
            ],
            "upstream": [],
        }
        case = _build_test_dir(tmp_path, golden=short_golden)

        result = run_modem_test(case)

        assert result.passed is False
        assert result.error == ""
        assert result.comparison is not None
        assert result.comparison.passed is False


# ---------------------------------------------------------------------------
# Pipeline errors (test cannot run) — table-driven
# ---------------------------------------------------------------------------

# ┌─────────────────────┬─────────────────────────────┬─────────────────────────────┐
# │ scenario            │ bad input                   │ expected error fragment      │
# ├─────────────────────┼─────────────────────────────┼─────────────────────────────┤
# │ missing golden file │ golden=None                 │ "Golden file not found"     │
# │ invalid HAR         │ har_data={"not": "a har"}   │ "Failed to load HAR"        │
# │ invalid modem YAML  │ modem_yaml="not: valid:..." │ "Failed to load modem"      │
# │ invalid parser YAML │ parser_yaml="bad: config"   │ "Failed to load parser"     │
# └─────────────────────┴─────────────────────────────┴─────────────────────────────┘

# fmt: off
PIPELINE_ERROR_CASES = [
    # (description,            kwargs,                                          expected_error)
    ("missing golden file",    {"golden": None},                                "Golden file not found"),
    ("invalid HAR",            {"har_data": {"not": "a har"}},                  "Failed to load HAR"),
    ("invalid modem YAML",     {"modem_yaml": "not: valid: yaml: config"},      "Failed to load modem config"),
    ("invalid parser YAML",    {"parser_yaml": "bad: config"},                  "Failed to load parser config"),
]
# fmt: on


@pytest.mark.parametrize(
    "desc,kwargs,expected_error",
    PIPELINE_ERROR_CASES,
    ids=[c[0] for c in PIPELINE_ERROR_CASES],
)
def test_pipeline_error(
    tmp_path: Path,
    desc: str,
    kwargs: dict[str, Any],
    expected_error: str,
) -> None:
    """Pipeline errors return passed=False with structured error message."""
    case = _build_test_dir(tmp_path, **kwargs)

    result = run_modem_test(case)

    assert result.passed is False, f"{desc}: expected failure"
    assert expected_error in result.error, f"{desc}: error mismatch"


# ---------------------------------------------------------------------------
# PostProcessor pipeline integration
# ---------------------------------------------------------------------------


class TestPostProcessorPipelineIntegration:
    """PostProcessor hooks invoked through the runner pipeline.

    Loader unit tests for ``load_post_processor`` in isolation live in
    ``tests/test_post_processor.py``. The cases below exercise the
    runner's interaction with PostProcessor — golden file matching,
    error capture — not the loader itself.
    """

    def test_post_processor_integration(self, tmp_path: Path) -> None:
        """PostProcessor hooks are invoked during pipeline run."""
        # PostProcessor that adds a field to every channel
        pp_code = textwrap.dedent("""\
            class PostProcessor:
                \"\"\"Adds a marker field to downstream channels.\"\"\"

                def parse_downstream(self, channels, resources):
                    for ch in channels:
                        ch["custom_field"] = "added"
                    return channels
        """)
        # Golden file must include the custom field
        golden_with_custom = {
            "downstream": [
                {"channel_id": 1, "channel_number": 1, "frequency": 507000000, "power": 2.5, "custom_field": "added"},
                {"channel_id": 2, "channel_number": 2, "frequency": 513000000, "power": 2.6, "custom_field": "added"},
            ],
            "upstream": [],
            "system_info": {
                "downstream_channel_count": 2,
                "upstream_channel_count": 0,
            },
        }
        case = _build_test_dir(
            tmp_path,
            parser_py=pp_code,
            golden=golden_with_custom,
        )

        result = run_modem_test(case)

        assert result.passed is True
        assert result.error == ""


# ---------------------------------------------------------------------------
# Additional error paths — table-driven
# ---------------------------------------------------------------------------

# ┌──────────────────────────┬─────────────────────────────────────────┬────────────────────────┐
# │ scenario                 │ bad input                               │ expected error         │
# ├──────────────────────────┼─────────────────────────────────────────┼────────────────────────┤
# │ corrupt golden file JSON │ golden file with invalid JSON           │ "Failed to load golden"│
# │ parser.py syntax error   │ parser_py with broken syntax            │ "Failed to load parser"│
# │ pipeline exception       │ PostProcessor that raises               │ "Pipeline error"       │
# │ no parser.yaml           │ parser.py only, no parser.yaml          │ "Pipeline error"       │
# │ auth failure             │ form auth against non-login HAR         │ "Pipeline error"       │
# └──────────────────────────┴─────────────────────────────────────────┴────────────────────────┘


class TestCorruptGoldenFile:
    """Golden file exists but contains invalid JSON."""

    def test_corrupt_golden_json(self, tmp_path: Path) -> None:
        """Invalid JSON in golden file produces a load error."""
        case = _build_test_dir(tmp_path)
        # Overwrite the golden file with invalid JSON
        case.golden_path.write_text("not valid json {{{")

        result = run_modem_test(case)

        assert result.passed is False
        assert "Failed to load golden file" in result.error


class TestParserPyLoadError:
    """parser.py with syntax error prevents pipeline from running."""

    def test_syntax_error_in_parser_py(self, tmp_path: Path) -> None:
        """Syntax error in parser.py is captured as a load error."""
        pp_code = "def broken(\n"
        case = _build_test_dir(tmp_path, parser_py=pp_code)

        result = run_modem_test(case)

        assert result.passed is False
        assert "Failed to load parser.py" in result.error


class TestPipelineError:
    """Pipeline raises at runtime."""

    def test_post_processor_raises(self, tmp_path: Path) -> None:
        """PostProcessor that raises is captured as pipeline error."""
        pp_code = textwrap.dedent("""\
            class PostProcessor:
                \"\"\"Raises during parsing.\"\"\"

                def parse_downstream(self, channels, resources):
                    raise ValueError("deliberate test error")
        """)
        case = _build_test_dir(tmp_path, parser_py=pp_code)

        result = run_modem_test(case)

        assert result.passed is False
        assert "Pipeline error" in result.error

    def test_no_parser_yaml(self, tmp_path: Path) -> None:
        """parser.py without parser.yaml causes pipeline error."""
        pp_code = textwrap.dedent("""\
            class PostProcessor:
                \"\"\"Minimal PostProcessor.\"\"\"
                pass
        """)
        case = _build_test_dir(tmp_path, parser_yaml=None, parser_py=pp_code)

        result = run_modem_test(case)

        assert result.passed is False
        assert "Pipeline error" in result.error


class TestAuthFailure:
    """Auth strategy that fails against the mock server."""

    def test_form_auth_failure(self, tmp_path: Path) -> None:
        """Form auth fails when indicator not found in response."""
        case = _build_test_dir(tmp_path, modem_yaml=_MODEM_FORM_AUTH_YAML)

        result = run_modem_test(case)

        assert result.passed is False
        assert "Pipeline error" in result.error
        assert "Auth failed" in result.error


# ---------------------------------------------------------------------------
# Pipeline configuration paths — session wiring
# ---------------------------------------------------------------------------


class TestSessionHeaders:
    """Modem config with session headers passes them to the session."""

    def test_session_headers_applied(self, tmp_path: Path) -> None:
        """Pipeline runs successfully with session headers configured."""
        case = _build_test_dir(tmp_path, modem_yaml=_MODEM_SESSION_HEADERS_YAML)

        result = run_modem_test(case)

        assert result.passed is True
        assert result.error == ""


class TestUrlToken:
    """Modem config with URL token session configuration."""

    def test_url_token_session(self, tmp_path: Path) -> None:
        """Pipeline runs with URL token extraction configured."""
        case = _build_test_dir(tmp_path, modem_yaml=_MODEM_URL_TOKEN_YAML)

        result = run_modem_test(case)

        # Pipeline should succeed — no token cookie present means
        # empty url_token, which is valid (no token appended to URLs)
        assert result.passed is True
        assert result.error == ""


class TestHnapTransport:
    """HNAP transport path — batched SOAP request via HNAPLoader."""

    def test_hnap_pipeline(self, tmp_path: Path) -> None:
        """Full HNAP pipeline: mock server -> HNAP auth -> SOAP fetch -> parse."""
        case = _build_test_dir(
            tmp_path,
            modem_yaml=_MODEM_HNAP_YAML,
            parser_yaml=_PARSER_HNAP_YAML,
            har_data=_HAR_HNAP_DATA,
            golden=_GOLDEN_HNAP_FILE,
        )

        result = run_modem_test(case)

        assert result.passed is True, f"HNAP pipeline failed: {result.error}"
        assert result.error == ""


# ---------------------------------------------------------------------------
# Orchestrated runner — full cycle through Orchestrator.get_modem_data()
# ---------------------------------------------------------------------------


class TestOrchestratedHappyPath:
    """Orchestrated path: collector -> orchestrator -> golden file."""

    def test_pass(self, tmp_path: Path) -> None:
        """Full orchestrated cycle: mock server -> collector -> orchestrator -> compare."""
        case = _build_test_dir(tmp_path)

        result = run_modem_test_orchestrated(case)

        assert result.passed is True
        assert result.error == ""
        assert result.comparison is not None
        assert result.comparison.passed is True

    def test_golden_file_mismatch(self, tmp_path: Path) -> None:
        """Orchestrated path detects golden file drift."""
        bad_golden = {
            "downstream": [
                {"channel_id": 1, "frequency": 999000000, "power": 2.5},
                {"channel_id": 2, "frequency": 513000000, "power": 2.6},
            ],
            "upstream": [],
        }
        case = _build_test_dir(tmp_path, golden=bad_golden)

        result = run_modem_test_orchestrated(case)

        assert result.passed is False
        assert result.error == ""
        assert result.comparison is not None


class TestOrchestratedErrors:
    """Orchestrated path error handling."""

    def test_missing_golden_file(self, tmp_path: Path) -> None:
        """Missing golden file returns error before orchestration."""
        case = _build_test_dir(tmp_path, golden=None)

        result = run_modem_test_orchestrated(case)

        assert result.passed is False
        assert "Golden file not found" in result.error

    def test_auth_failure(self, tmp_path: Path) -> None:
        """Auth failure through orchestrator returns structured error."""
        case = _build_test_dir(tmp_path, modem_yaml=_MODEM_FORM_AUTH_YAML)

        result = run_modem_test_orchestrated(case)

        assert result.passed is False
        assert "Orchestrator error" in result.error

    def test_invalid_modem_config(self, tmp_path: Path) -> None:
        """Invalid modem config is caught before orchestration."""
        case = _build_test_dir(tmp_path, modem_yaml="not: valid: yaml: config")

        result = run_modem_test_orchestrated(case)

        assert result.passed is False
        assert "Failed to load modem config" in result.error


# ---------------------------------------------------------------------------
# Restart action runner
# ---------------------------------------------------------------------------


def _build_restart_case(
    tmp_path: Path,
    *,
    modem_yaml: str = _MODEM_WITH_RESTART_YAML,
    har_data: dict[str, Any] = _HAR_RESTART_DATA,
) -> RestartTestCase:
    """Build a minimal RestartTestCase under tmp_path."""
    modem_dir = tmp_path / "modems" / "solentlabs" / "t100"
    tests_dir = modem_dir / "test_data"
    tests_dir.mkdir(parents=True)
    modem_config_path = modem_dir / "modem.yaml"
    modem_config_path.write_text(modem_yaml)
    har_path = tests_dir / "modem.har"
    har_path.write_text(json.dumps(har_data))
    return RestartTestCase(
        name="solentlabs/t100/restart",
        modem_dir=modem_dir,
        har_path=har_path,
        modem_config_path=modem_config_path,
    )


# fmt: off
RESTART_ERROR_CASES = [
    # (description,           kwargs,                                        expected_error)
    ("config load failure",   {"modem_yaml": "not: valid: yaml: config"},   "Config load failed"),
    ("no actions.restart",    {"modem_yaml": _MODEM_YAML},                  "No actions.restart"),
    ("har load failure",      {"har_data": {"not": "a har"}},               "Failed to load HAR"),
]
# fmt: on


@pytest.mark.parametrize(
    "desc,kwargs,expected_error",
    RESTART_ERROR_CASES,
    ids=[c[0] for c in RESTART_ERROR_CASES],
)
def test_restart_error(
    tmp_path: Path,
    desc: str,
    kwargs: dict[str, Any],
    expected_error: str,
) -> None:
    """Restart pipeline errors return passed=False with structured error message."""
    case = _build_restart_case(tmp_path, **kwargs)

    result = run_modem_restart_test(case)

    assert result.passed is False, f"{desc}: expected failure"
    assert expected_error in result.error, f"{desc}: error mismatch"


class TestRestartAction:
    """run_modem_restart_test — happy path and auth failure."""

    def test_happy_path(self, tmp_path: Path) -> None:
        """Restart action against mock server succeeds."""
        case = _build_restart_case(tmp_path)

        result = run_modem_restart_test(case)

        assert result.passed is True
        assert result.error == ""
        assert result.test_name == case.name

    def test_auth_failure(self, tmp_path: Path) -> None:
        """Form auth failure before action dispatch returns structured error."""
        modem_yaml_with_form_and_restart = (
            "manufacturer: Solent Labs\n"
            "model: T100\n"
            "transport: http\n"
            "default_host: 192.168.100.1\n"
            "auth:\n"
            "  strategy: form\n"
            "  action: /login\n"
            "  success:\n"
            "    indicator: Welcome\n"
            "hardware:\n"
            "  docsis_version: '3.0'\n"
            "status: awaiting_verification\n"
            "attribution:\n"
            "  contributors:\n"
            "    - github: test-user\n"
            "      contribution: Initial capture\n"
            "isps:\n"
            "  - Various\n"
            "actions:\n"
            "  restart:\n"
            "    type: http\n"
            "    method: POST\n"
            "    endpoint: /restart\n"
        )
        case = _build_restart_case(tmp_path, modem_yaml=modem_yaml_with_form_and_restart)

        result = run_modem_restart_test(case)

        assert result.passed is False
        assert "Auth failed" in result.error
