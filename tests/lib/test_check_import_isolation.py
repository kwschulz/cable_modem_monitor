"""Tests for scripts/check_import_isolation.py.

The scanner catches import cycles that only surface on a leaf-first
import. A cycle between two packages is invisible while something always
imports the "safe" side first — the suite stays green and any third
party importing the leaf directly gets ImportError. Both cycles this
repo has had (analysis.actions/auth, analysis.format/mapping) had that
shape, and the second is what this script was written for.

Coverage breakdown per docs/CODE_REVIEW.md § Test File Standards:
- ``_module_name`` and ``_modules_for`` path handling — table-driven and
  behavioural over tmp_path trees.
- ``_purge`` and ``main`` — behavioural, against a throwaway namespace
  package so the real solentlabs tree is never imported or evicted.
- Guard cases (paths outside the roots, non-Python files) carry live
  counterparts per § Gate Scripts Require Tests.

The full sweep over all 197 shipping modules takes ~32s and already runs
in validate-ci and the pre-push hook; repeating it here would triple the
unit suite's runtime to re-prove what the gate itself proves on every
push. The regression below asserts discovery still finds the tree, which
is the part that silently breaks if a package moves.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.fixture_helpers import load_script

_mod = load_script("scripts/check_import_isolation.py")

_NS = "faux_isolation_ns"


# ---------------------------------------------------------------------------
# _module_name
# ---------------------------------------------------------------------------

# fmt: off
MODULE_NAMES = [
    pytest.param("solentlabs/pkg/thing.py",          "solentlabs.pkg.thing", id="module"),
    pytest.param("solentlabs/pkg/__init__.py",       "solentlabs.pkg",       id="package-init"),
    pytest.param("solentlabs/pkg/sub/deep/leaf.py",  "solentlabs.pkg.sub.deep.leaf", id="nested"),
    pytest.param("solentlabs/__init__.py",           "solentlabs",           id="namespace-root"),
]
# fmt: on


@pytest.mark.parametrize(("relative", "expected"), MODULE_NAMES)
def test_module_name(relative: str, expected: str, tmp_path: Path) -> None:
    """A source path maps to the dotted name a consumer would import."""
    root = tmp_path / "solentlabs"
    assert _mod._module_name(tmp_path / relative, root) == expected


# ---------------------------------------------------------------------------
# _modules_for — path filtering
# ---------------------------------------------------------------------------


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A single throwaway package root, standing in for the real ones."""
    root = tmp_path / "pkg" / _NS
    (root / "sub").mkdir(parents=True)
    monkeypatch.setattr(_mod, "PACKAGE_ROOTS", (root,))
    return root


def test_path_inside_a_root_is_included(roots: Path) -> None:
    (roots / "thing.py").write_text("", encoding="utf-8")
    assert _mod._modules_for([str(roots / "thing.py")]) == [f"{_NS}.thing"]


def test_path_outside_the_roots_is_skipped(roots: Path, tmp_path: Path) -> None:
    """Editing an unrelated file must not drag it into the sweep."""
    stray = tmp_path / "elsewhere.py"
    stray.write_text("", encoding="utf-8")
    assert _mod._modules_for([str(stray)]) == []


def test_non_python_files_are_skipped(roots: Path) -> None:
    (roots / "data.yaml").write_text("", encoding="utf-8")
    assert _mod._modules_for([str(roots / "data.yaml")]) == []


def test_filters_do_not_swallow_a_real_module(roots: Path, tmp_path: Path) -> None:
    """Live counterpart: with a stray and a non-Python file alongside it,
    the genuine module is still selected."""
    (roots / "thing.py").write_text("", encoding="utf-8")
    (roots / "data.yaml").write_text("", encoding="utf-8")
    stray = tmp_path / "elsewhere.py"
    stray.write_text("", encoding="utf-8")
    selected = _mod._modules_for([str(stray), str(roots / "data.yaml"), str(roots / "thing.py")])
    assert selected == [f"{_NS}.thing"]


def test_duplicate_paths_collapse_and_sort(roots: Path) -> None:
    for name in ("b.py", "a.py"):
        (roots / name).write_text("", encoding="utf-8")
    paths = [str(roots / "b.py"), str(roots / "a.py"), str(roots / "b.py")]
    assert _mod._modules_for(paths) == [f"{_NS}.a", f"{_NS}.b"]


# ---------------------------------------------------------------------------
# _purge — cold-import guarantee
# ---------------------------------------------------------------------------


def test_purge_drops_only_the_target_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cold-import guarantee: the tree goes, everything else stays.

    Without this the scanner would inherit whatever a previous import
    left cached, which is precisely the ordering that hides a cycle.
    """
    monkeypatch.setattr(_mod, "NAMESPACE", _NS)
    monkeypatch.setitem(sys.modules, _NS, object())
    monkeypatch.setitem(sys.modules, f"{_NS}.deep.leaf", object())
    monkeypatch.setitem(sys.modules, f"{_NS}_lookalike", object())

    _mod._purge()

    assert _NS not in sys.modules
    assert f"{_NS}.deep.leaf" not in sys.modules
    # Prefix match alone would evict this; the split on "." is what saves it.
    assert f"{_NS}_lookalike" in sys.modules


# ---------------------------------------------------------------------------
# main — the gate itself
# ---------------------------------------------------------------------------


@pytest.fixture
def package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build an importable throwaway package and point the scanner at it."""

    def _build(modules: dict[str, str]) -> None:
        root = tmp_path / "pkg" / _NS
        root.mkdir(parents=True)
        (root / "__init__.py").write_text("", encoding="utf-8")
        for name, body in modules.items():
            (root / f"{name}.py").write_text(body, encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path / "pkg"))
        monkeypatch.setattr(_mod, "PACKAGE_ROOTS", (root,))
        monkeypatch.setattr(_mod, "NAMESPACE", _NS)

    return _build


def test_importable_modules_pass(package, monkeypatch: pytest.MonkeyPatch) -> None:
    package({"good": "VALUE = 1\n"})
    monkeypatch.setattr(sys, "argv", ["check_import_isolation.py"])
    assert _mod.main() == 0


def test_module_that_fails_to_import_is_reported(
    package,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The cycle's shape: importing the leaf on its own raises."""
    package({"good": "VALUE = 1\n", "broken": "raise ImportError('cannot import name X')\n"})
    monkeypatch.setattr(sys, "argv", ["check_import_isolation.py"])

    assert _mod.main() == 1

    out = capsys.readouterr().out
    assert "broken" in out
    assert "cannot be imported on their own" in out


def test_explicit_paths_narrow_the_sweep(package, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Given paths, only those are checked — the pre-commit fast path.

    The broken module exists but is not named, so the run passes.
    """
    package({"good": "VALUE = 1\n", "broken": "raise ImportError('boom')\n"})
    target = str(tmp_path / "pkg" / _NS / "good.py")
    monkeypatch.setattr(sys, "argv", ["check_import_isolation.py", target])
    assert _mod.main() == 0


def test_naming_the_broken_module_still_fails(package, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Live counterpart: narrowing does not mean never failing."""
    package({"broken": "raise ImportError('boom')\n"})
    target = str(tmp_path / "pkg" / _NS / "broken.py")
    monkeypatch.setattr(sys, "argv", ["check_import_isolation.py", target])
    assert _mod.main() == 1


def test_no_matching_paths_is_a_pass(package, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A commit touching nothing in the roots has nothing to check."""
    package({"good": "VALUE = 1\n"})
    stray = tmp_path / "elsewhere.py"
    stray.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["check_import_isolation.py", str(stray)])
    assert _mod.main() == 0


# ---------------------------------------------------------------------------
# Real-repo regression
# ---------------------------------------------------------------------------


def test_discovery_finds_the_shipping_tree() -> None:
    """Discovery must keep resolving after a package move or rename.

    The import sweep itself runs in validate-ci and the pre-push hook;
    what fails silently here is discovery returning nothing, which would
    make the gate pass by checking zero modules.
    """
    modules = _mod._all_modules()
    assert len(modules) > 100, f"expected the full shipping tree, found {len(modules)}"
    assert all(name.startswith("solentlabs") for name in modules)
