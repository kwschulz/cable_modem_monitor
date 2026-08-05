"""Tests for test case discovery from modem directory structure.

Uses ``tmp_path`` to build minimal directory trees and verify
config resolution logic. Real catalog validation deferred to Step 8.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from solentlabs.cable_modem_monitor_core.test_harness.discovery import (
    discover_modem_tests,
    discover_restart_tests,
)

from tests._helpers import load_fixture

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

_PIPELINE_FIXTURES = Path(__file__).parent.parent / "fixtures" / "pipeline"

_MODEM_YAML = (_PIPELINE_FIXTURES / "modem.yaml").read_text()
_MODEM_WITH_ACTIONS_YAML = (_PIPELINE_FIXTURES / "modem_with_restart.yaml").read_text()
_PARSER_YAML = (_PIPELINE_FIXTURES / "modem.yaml").read_text()  # content irrelevant
_MINIMAL_HAR: dict = load_fixture(_PIPELINE_FIXTURES / "har_minimal.json")
_MINIMAL_GOLDEN: dict = load_fixture(_PIPELINE_FIXTURES / "golden_empty.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_modem_dir(
    base: Path,
    *,
    mfr: str = "solentlabs",
    model: str = "t100",
    modem_yaml: str | None = _MODEM_YAML,
    parser_yaml: str | None = _PARSER_YAML,
    parser_py: str | None = None,
    hars: dict[str, dict] | None = None,
    goldens: dict[str, dict] | None = None,
    variant_yamls: dict[str, str] | None = None,
) -> Path:
    """Build a modem directory tree under ``base/mfr/model/``.

    Args:
        base: Root modems directory.
        mfr: Manufacturer directory name.
        model: Model directory name.
        modem_yaml: Content for ``modem.yaml``. Use ``None`` to skip.
        parser_yaml: Content for ``parser.yaml``. Use ``None`` to skip.
        parser_py: Content for ``parser.py``. Use ``None`` to skip.
        hars: Map of stem -> HAR dict for ``test_data/{stem}.har``.
        goldens: Map of stem -> golden dict for ``test_data/{stem}.expected.json``.
        variant_yamls: Map of name -> content for ``modem-{name}.yaml``.

    Returns:
        Path to the modem directory.
    """
    modem_dir = base / mfr / model
    tests_dir = modem_dir / "test_data"
    tests_dir.mkdir(parents=True)

    if modem_yaml is not None:
        (modem_dir / "modem.yaml").write_text(modem_yaml)
    if parser_yaml is not None:
        (modem_dir / "parser.yaml").write_text(parser_yaml)
    if parser_py is not None:
        (modem_dir / "parser.py").write_text(parser_py)

    if variant_yamls:
        for name, content in variant_yamls.items():
            (modem_dir / f"modem-{name}.yaml").write_text(content)

    if hars is None:
        hars = {"modem": _MINIMAL_HAR}
    for stem, har_data in hars.items():
        (tests_dir / f"{stem}.har").write_text(json.dumps(har_data))

    if goldens is None:
        goldens = {"modem": _MINIMAL_GOLDEN}
    for stem, golden_data in goldens.items():
        (tests_dir / f"{stem}.expected.json").write_text(json.dumps(golden_data))

    return modem_dir


# ---------------------------------------------------------------------------
# Test: Single modem directory (MCP use case)
# ---------------------------------------------------------------------------


class TestSingleModemDir:
    """Discovery when pointed at a single modem directory."""

    def test_discovers_default_har(self, tmp_path: Path) -> None:
        """Discovers modem.har -> modem.yaml pairing."""
        modems = tmp_path / "modems"
        _build_modem_dir(modems)
        modem_dir = modems / "solentlabs" / "t100"

        cases = discover_modem_tests(modem_dir)

        assert len(cases) == 1
        case = cases[0]
        assert case.har_path == modem_dir / "test_data" / "modem.har"
        assert case.golden_path == modem_dir / "test_data" / "modem.expected.json"
        assert case.modem_config_path == modem_dir / "modem.yaml"
        assert case.parser_config_path == modem_dir / "parser.yaml"
        assert case.parser_py_path is None

    def test_discovers_parser_py(self, tmp_path: Path) -> None:
        """Discovers parser.py when present alongside parser.yaml."""
        modems = tmp_path / "modems"
        _build_modem_dir(
            modems,
            parser_py="class PostProcessor:\n    pass\n",
        )
        modem_dir = modems / "solentlabs" / "t100"

        cases = discover_modem_tests(modem_dir)

        assert len(cases) == 1
        assert cases[0].parser_py_path == modem_dir / "parser.py"
        assert cases[0].parser_config_path == modem_dir / "parser.yaml"

    def test_parser_py_only(self, tmp_path: Path) -> None:
        """Discovers modem with parser.py but no parser.yaml."""
        modems = tmp_path / "modems"
        _build_modem_dir(
            modems,
            parser_yaml=None,
            parser_py="class PostProcessor:\n    pass\n",
        )
        modem_dir = modems / "solentlabs" / "t100"

        cases = discover_modem_tests(modem_dir)

        assert len(cases) == 1
        assert cases[0].parser_config_path is None
        assert cases[0].parser_py_path == modem_dir / "parser.py"

    def test_missing_golden_file_still_discovered(self, tmp_path: Path) -> None:
        """HAR without golden file is still returned as a test case."""
        modems = tmp_path / "modems"
        _build_modem_dir(modems, goldens={})
        modem_dir = modems / "solentlabs" / "t100"

        cases = discover_modem_tests(modem_dir)

        assert len(cases) == 1
        case = cases[0]
        # golden_path is set but file does not exist
        assert case.golden_path == modem_dir / "test_data" / "modem.expected.json"
        assert not case.golden_path.exists()


# ---------------------------------------------------------------------------
# Test: Config resolution
# ---------------------------------------------------------------------------


class TestConfigResolution:
    """Config resolution per MODEM_DIRECTORY_SPEC."""

    def test_variant_har_uses_variant_yaml(self, tmp_path: Path) -> None:
        """modem-basic.har resolves to modem-basic.yaml."""
        modems = tmp_path / "modems"
        _build_modem_dir(
            modems,
            hars={"modem": _MINIMAL_HAR, "modem-basic": _MINIMAL_HAR},
            goldens={"modem": _MINIMAL_GOLDEN, "modem-basic": _MINIMAL_GOLDEN},
            variant_yamls={"basic": _MODEM_YAML},
        )
        modem_dir = modems / "solentlabs" / "t100"

        cases = discover_modem_tests(modem_dir)

        by_name = {c.name.split("/")[-1]: c for c in cases}
        assert by_name["modem"].modem_config_path == modem_dir / "modem.yaml"
        assert by_name["modem-basic"].modem_config_path == modem_dir / "modem-basic.yaml"

    def test_variant_har_falls_back_to_default(self, tmp_path: Path) -> None:
        """modem-compat.har with no modem-compat.yaml falls back to modem.yaml."""
        modems = tmp_path / "modems"
        _build_modem_dir(
            modems,
            hars={"modem": _MINIMAL_HAR, "modem-compat": _MINIMAL_HAR},
            goldens={"modem": _MINIMAL_GOLDEN, "modem-compat": _MINIMAL_GOLDEN},
        )
        modem_dir = modems / "solentlabs" / "t100"

        cases = discover_modem_tests(modem_dir)

        by_name = {c.name.split("/")[-1]: c for c in cases}
        assert by_name["modem-compat"].modem_config_path == modem_dir / "modem.yaml"

    def test_no_modem_yaml_skips(self, tmp_path: Path) -> None:
        """HAR with no resolvable modem config is skipped."""
        modems = tmp_path / "modems"
        _build_modem_dir(
            modems,
            modem_yaml=None,
            hars={"modem-orphan": _MINIMAL_HAR},
            goldens={"modem-orphan": _MINIMAL_GOLDEN},
        )
        modem_dir = modems / "solentlabs" / "t100"

        cases = discover_modem_tests(modem_dir)

        assert len(cases) == 0


# ---------------------------------------------------------------------------
# Test: Full tree discovery (Catalog / CI use case)
# ---------------------------------------------------------------------------


class TestFullTreeDiscovery:
    """Discovery when pointed at the modems root."""

    def test_discovers_multiple_modems(self, tmp_path: Path) -> None:
        """Finds test cases across multiple modem directories."""
        modems = tmp_path / "modems"
        _build_modem_dir(modems, mfr="acme", model="a100")
        _build_modem_dir(modems, mfr="acme", model="b200")

        cases = discover_modem_tests(modems)

        assert len(cases) == 2
        names = {c.name for c in cases}
        assert "acme/a100/modem" in names
        assert "acme/b200/modem" in names

    def test_skips_dirs_without_tests(self, tmp_path: Path) -> None:
        """Modem directories without tests/ are silently skipped."""
        modems = tmp_path / "modems"
        _build_modem_dir(modems, mfr="acme", model="a100")
        # Create a dir with no tests/
        incomplete = modems / "acme" / "broken"
        incomplete.mkdir(parents=True)
        (incomplete / "modem.yaml").write_text(_MODEM_YAML)
        (incomplete / "parser.yaml").write_text(_PARSER_YAML)

        cases = discover_modem_tests(modems)

        assert len(cases) == 1
        assert cases[0].name == "acme/a100/modem"

    def test_skips_dirs_without_parser(self, tmp_path: Path) -> None:
        """Modem directories with no parser.yaml or parser.py are skipped."""
        modems = tmp_path / "modems"
        _build_modem_dir(modems, mfr="acme", model="a100")
        _build_modem_dir(
            modems,
            mfr="acme",
            model="noparser",
            parser_yaml=None,
            parser_py=None,
        )

        cases = discover_modem_tests(modems)

        assert len(cases) == 1
        assert cases[0].name == "acme/a100/modem"

    def test_empty_directory(self, tmp_path: Path) -> None:
        """Empty modems directory returns no cases."""
        modems = tmp_path / "modems"
        modems.mkdir()

        cases = discover_modem_tests(modems)

        assert cases == []

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        """Nonexistent path returns no cases."""
        cases = discover_modem_tests(tmp_path / "does_not_exist")

        assert cases == []

    def test_cases_sorted_by_name(self, tmp_path: Path) -> None:
        """Returned cases are sorted alphabetically by name."""
        modems = tmp_path / "modems"
        _build_modem_dir(modems, mfr="zebra", model="z900")
        _build_modem_dir(modems, mfr="acme", model="a100")

        cases = discover_modem_tests(modems)

        assert cases[0].name < cases[1].name


# ---------------------------------------------------------------------------
# Test: ModemTestCase is frozen
# ---------------------------------------------------------------------------


class TestModemTestCaseImmutable:
    """ModemTestCase is a frozen dataclass."""

    def test_frozen(self, tmp_path: Path) -> None:
        """Cannot mutate attributes after creation."""
        modems = tmp_path / "modems"
        _build_modem_dir(modems)
        modem_dir = modems / "solentlabs" / "t100"
        cases = discover_modem_tests(modem_dir)

        with pytest.raises(AttributeError):
            cases[0].name = "hacked"  # type: ignore[misc]  # frozen dataclass — assignment raises AttributeError at runtime


# ---------------------------------------------------------------------------
# Test: Config resolution — Tier 3 fallback (no-dash stem)
# ---------------------------------------------------------------------------


class TestConfigResolutionTier3:
    """resolve_modem_config Tier 3: stems with no dashes fall through to modem.yaml."""

    def test_no_dash_stem_falls_back_to_modem_yaml(self, tmp_path: Path) -> None:
        """A stem with no dashes that has no exact yaml falls through to modem.yaml."""
        modems = tmp_path / "modems"
        _build_modem_dir(
            modems,
            hars={"firmware": _MINIMAL_HAR},
            goldens={"firmware": _MINIMAL_GOLDEN},
        )
        modem_dir = modems / "solentlabs" / "t100"

        cases = discover_modem_tests(modem_dir)

        assert len(cases) == 1
        assert cases[0].modem_config_path == modem_dir / "modem.yaml"


# ---------------------------------------------------------------------------
# Test: discover_restart_tests
# ---------------------------------------------------------------------------


class TestRestartDiscovery:
    """discover_restart_tests finds restart action test cases."""

    def test_discovers_combined_capture_when_actions_declared(self, tmp_path: Path) -> None:
        """actions.restart declared -> restart replays modem.har."""
        modems = tmp_path / "modems"
        _build_modem_dir(
            modems,
            modem_yaml=_MODEM_WITH_ACTIONS_YAML,
        )
        modem_dir = modems / "solentlabs" / "t100"

        cases = discover_restart_tests(modems)

        assert len(cases) == 1
        assert cases[0].har_path == modem_dir / "test_data" / "modem.har"
        assert cases[0].modem_config_path == modem_dir / "modem.yaml"

    def test_skips_when_no_actions(self, tmp_path: Path) -> None:
        """modem.yaml has no actions -> skipped."""
        modems = tmp_path / "modems"
        _build_modem_dir(modems, modem_yaml=_MODEM_YAML)

        cases = discover_restart_tests(modems)

        assert cases == []

    def test_skips_when_modem_har_missing(self, tmp_path: Path) -> None:
        """actions.restart declared but no modem.har -> skipped."""
        modems = tmp_path / "modems"
        _build_modem_dir(
            modems,
            modem_yaml=_MODEM_WITH_ACTIONS_YAML,
            hars={},
            goldens={},
        )

        cases = discover_restart_tests(modems)

        assert cases == []

    def test_skips_when_no_modem_yaml(self, tmp_path: Path) -> None:
        """No modem.yaml in directory -> skipped."""
        modems = tmp_path / "modems"
        _build_modem_dir(
            modems,
            modem_yaml=None,
            hars={"modem": _MINIMAL_HAR},
            goldens={},
        )

        cases = discover_restart_tests(modems)

        assert cases == []

    def test_single_modem_directory(self, tmp_path: Path) -> None:
        """discover_restart_tests(modem_dir) works when pointed at a single modem dir."""
        modems = tmp_path / "modems"
        _build_modem_dir(
            modems,
            mfr="acme",
            model="r100",
            modem_yaml=_MODEM_WITH_ACTIONS_YAML,
        )
        modem_dir = modems / "acme" / "r100"

        cases = discover_restart_tests(modem_dir)

        assert len(cases) == 1

    def test_multiple_modems(self, tmp_path: Path) -> None:
        """discover_restart_tests finds cases across multiple modem directories."""
        modems = tmp_path / "modems"
        _build_modem_dir(
            modems,
            mfr="acme",
            model="a100",
            modem_yaml=_MODEM_WITH_ACTIONS_YAML,
        )
        _build_modem_dir(
            modems,
            mfr="acme",
            model="b200",
            modem_yaml=_MODEM_WITH_ACTIONS_YAML,
        )

        cases = discover_restart_tests(modems)

        assert len(cases) == 2
        names = {c.name for c in cases}
        assert "acme/a100/restart" in names
        assert "acme/b200/restart" in names

    def test_name_format(self, tmp_path: Path) -> None:
        """Test case name is {mfr}/{model}/restart."""
        modems = tmp_path / "modems"
        _build_modem_dir(
            modems,
            mfr="acme",
            model="r100",
            modem_yaml=_MODEM_WITH_ACTIONS_YAML,
        )

        cases = discover_restart_tests(modems)

        assert cases[0].name == "acme/r100/restart"

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        """Nonexistent path returns no cases."""
        cases = discover_restart_tests(tmp_path / "does_not_exist")

        assert cases == []

    def test_empty_directory(self, tmp_path: Path) -> None:
        """Empty modems directory returns no cases."""
        modems = tmp_path / "modems"
        modems.mkdir()

        cases = discover_restart_tests(modems)

        assert cases == []

    def test_cases_sorted_by_name(self, tmp_path: Path) -> None:
        """Returned cases are sorted alphabetically by name."""
        modems = tmp_path / "modems"
        _build_modem_dir(
            modems,
            mfr="zebra",
            model="z900",
            modem_yaml=_MODEM_WITH_ACTIONS_YAML,
        )
        _build_modem_dir(
            modems,
            mfr="acme",
            model="a100",
            modem_yaml=_MODEM_WITH_ACTIONS_YAML,
        )

        cases = discover_restart_tests(modems)

        assert cases[0].name < cases[1].name

    def test_restart_test_case_is_frozen(self, tmp_path: Path) -> None:
        """RestartTestCase is a frozen dataclass."""
        modems = tmp_path / "modems"
        _build_modem_dir(
            modems,
            modem_yaml=_MODEM_WITH_ACTIONS_YAML,
        )

        cases = discover_restart_tests(modems)

        with pytest.raises(AttributeError):
            cases[0].name = "hacked"  # type: ignore[misc]  # frozen dataclass — assignment raises AttributeError at runtime
