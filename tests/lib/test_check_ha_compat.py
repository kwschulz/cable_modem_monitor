"""Tests for scripts/check_ha_compat.py.

Home Assistant pins exact package versions in package_constraints.txt. A
library floor above one of those pins installs fine standalone and fails
at runtime inside HA — the beta.4 incident, where requests>=2.34.2 and
pyyaml>=6.0.3 both exceeded HA's pins. This script is the gate that
catches that class before release.

It was added in the same commit that lowered those floors
("fix(deps): lower Core floors to HA-compatible versions; add HA compat
gate") and had no tests until now, so nothing proved it still rejects
what it was written to reject.

Coverage breakdown per docs/CODE_REVIEW.md § Test File Standards:
- ``_normalize`` and the two parsers — table-driven and behavioural over
  tmp_path files.
- ``main`` — behavioural, with ROOT and the constraints lookup pointed at
  a throwaway tree.
- Guard cases (non-pinned lines, internal solentlabs- deps, absent
  homeassistant) each carry a live counterpart per § Gate Scripts
  Require Tests, so a gate that has stopped rejecting fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixture_helpers import load_script

_mod = load_script("scripts/check_ha_compat.py")


# ---------------------------------------------------------------------------
# _normalize
# ---------------------------------------------------------------------------

# fmt: off
NAMES = [
    pytest.param("requests",      "requests",     id="already-normal"),
    pytest.param("PyYAML",        "pyyaml",       id="case-folded"),
    pytest.param("typing_extensions", "typing-extensions", id="underscore"),
    pytest.param("ruamel.yaml", "ruamel-yaml", id="dot"),
    pytest.param("Foo_Bar.Baz",   "foo-bar-baz",  id="mixed"),
]
# fmt: on


@pytest.mark.parametrize(("raw", "expected"), NAMES)
def test_normalize(raw: str, expected: str) -> None:
    """PEP 503 name normalization, so pyproject and HA spellings compare."""
    assert _mod._normalize(raw) == expected


# ---------------------------------------------------------------------------
# _parse_ha_constraints
# ---------------------------------------------------------------------------


def _constraints(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "package_constraints.txt"
    path.write_text(body, encoding="utf-8")
    return path


def test_exact_pins_are_captured(tmp_path: Path) -> None:
    pins = _mod._parse_ha_constraints(_constraints(tmp_path, "requests==2.32.3\nPyYAML==6.0.2\n"))
    assert pins == {"requests": "2.32.3", "pyyaml": "6.0.2"}


def test_comments_and_blank_lines_are_ignored(tmp_path: Path) -> None:
    body = "# Automatically generated\n\n   \nrequests==2.32.3\n"
    assert _mod._parse_ha_constraints(_constraints(tmp_path, body)) == {"requests": "2.32.3"}


def test_environment_marker_is_stripped_from_the_version(tmp_path: Path) -> None:
    body = 'requests==2.32.3; python_version < "3.13"\n'
    assert _mod._parse_ha_constraints(_constraints(tmp_path, body)) == {"requests": "2.32.3"}


def test_non_exact_lines_are_skipped(tmp_path: Path) -> None:
    """Only ``==`` pins constrain; a floor in HA's file is not a pin."""
    body = "requests>=2.32.3\nurllib3<3\n"
    assert _mod._parse_ha_constraints(_constraints(tmp_path, body)) == {}


def test_exact_pin_guard_still_captures_the_pinned_form(tmp_path: Path) -> None:
    """Live counterpart: the same package pinned with == is captured."""
    assert _mod._parse_ha_constraints(_constraints(tmp_path, "requests==2.32.3\n")) == {"requests": "2.32.3"}


# ---------------------------------------------------------------------------
# _parse_pyproject_deps
# ---------------------------------------------------------------------------


def _pyproject(tmp_path: Path, deps: list[str]) -> Path:
    rendered = ", ".join(f'"{d}"' for d in deps)
    path = tmp_path / "pyproject.toml"
    path.write_text(f'[project]\nname = "x"\nversion = "1"\ndependencies = [{rendered}]\n', encoding="utf-8")
    return path


def test_declared_dependencies_are_returned(tmp_path: Path) -> None:
    assert _mod._parse_pyproject_deps(_pyproject(tmp_path, ["requests>=2.32.0", "PyYAML>=6.0"])) == [
        ("requests", ">=2.32.0"),
        ("pyyaml", ">=6.0"),
    ]


def test_internal_packages_are_skipped(tmp_path: Path) -> None:
    """Our own packages are not on PyPI's constraint surface."""
    deps = ["solentlabs-cable-modem-monitor-core==3.14.0", "requests>=2.32.0"]
    assert _mod._parse_pyproject_deps(_pyproject(tmp_path, deps)) == [("requests", ">=2.32.0")]


def test_internal_skip_does_not_swallow_third_party_deps(tmp_path: Path) -> None:
    """Live counterpart: only the solentlabs- prefix is dropped."""
    deps = ["solentlabs-core==1.0", "solent-adjacent>=2.0"]
    assert _mod._parse_pyproject_deps(_pyproject(tmp_path, deps)) == [("solent-adjacent", ">=2.0")]


def test_dependency_without_a_specifier_yields_an_empty_spec(tmp_path: Path) -> None:
    assert _mod._parse_pyproject_deps(_pyproject(tmp_path, ["requests"])) == [("requests", "")]


def test_missing_pyproject_is_not_an_error(tmp_path: Path) -> None:
    assert _mod._parse_pyproject_deps(tmp_path / "absent.toml") == []


# ---------------------------------------------------------------------------
# main — the gate itself
# ---------------------------------------------------------------------------


@pytest.fixture
def gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the script at a throwaway package tree and constraints file."""

    def _build(core_deps: list[str], ha_body: str, catalog_deps: list[str] | None = None) -> int:
        for pkg, deps in (
            ("cable_modem_monitor_core", core_deps),
            ("cable_modem_monitor_catalog", catalog_deps or []),
        ):
            directory = tmp_path / "packages" / pkg
            directory.mkdir(parents=True)
            _pyproject(directory, deps)

        constraints = _constraints(tmp_path, ha_body)
        monkeypatch.setattr(_mod, "ROOT", tmp_path)
        monkeypatch.setattr(_mod, "_find_ha_constraints", lambda: constraints)
        return _mod.main()

    return _build


def test_floor_above_an_ha_pin_is_rejected(gate, capsys: pytest.CaptureFixture[str]) -> None:
    """The beta.4 shape: requests>=2.34.2 against HA's requests==2.32.3."""
    assert gate(["requests>=2.34.2"], "requests==2.32.3\n") == 1
    out = capsys.readouterr().out
    assert "HA compatibility" in out
    assert "requests" in out


def test_floor_that_ha_satisfies_is_accepted(gate) -> None:
    """Live counterpart: the same dependency at a compatible floor passes."""
    assert gate(["requests>=2.32.0"], "requests==2.32.3\n") == 0


def test_dependency_ha_does_not_pin_is_ignored(gate) -> None:
    assert gate(["some-unpinned-lib>=99.0"], "requests==2.32.3\n") == 0


def test_dependency_without_a_specifier_is_ignored(gate) -> None:
    """No floor declared means nothing to conflict with."""
    assert gate(["requests"], "requests==2.32.3\n") == 0


def test_catalog_dependencies_are_checked_too(gate) -> None:
    """Both pyprojects are scanned, not just Core."""
    assert gate([], "requests==2.32.3\n", catalog_deps=["requests>=2.34.2"]) == 1


def test_unparseable_specifier_is_reported_rather_than_swallowed(gate, capsys: pytest.CaptureFixture[str]) -> None:
    assert gate(["requests>>>2.0"], "requests==2.32.3\n") == 1
    assert "could not parse" in capsys.readouterr().out


def test_absent_homeassistant_skips_the_gate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Documented, not endorsed: with no HA installed the gate returns 0.

    The check cannot run without HA's constraints file, but the exit code
    is indistinguishable from a real pass, so a local run in an
    environment missing HA reports success having verified nothing. CI
    installs homeassistant explicitly (tests.yml § HA Dependency
    Compatibility), which is what keeps this path from mattering there.
    """
    monkeypatch.setattr(_mod, "_find_ha_constraints", lambda: None)
    assert _mod.main() == 0
    assert "skipping" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Real-repo regression
# ---------------------------------------------------------------------------


def test_repo_dependencies_satisfy_ha_constraints() -> None:
    """What make ha-compat-check runs; skipped when HA is absent."""
    if _mod._find_ha_constraints() is None:
        pytest.skip("homeassistant not installed in this environment")
    assert _mod.main() == 0
