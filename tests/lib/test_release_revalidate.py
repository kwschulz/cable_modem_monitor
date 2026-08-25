"""Tests for ``revalidate_rewritten_files`` in scripts/release.py.

``release.py`` runs the full CI mirror before it rewrites the version
files and promotes ``[Unreleased]``, so that run inspects the pre-bump
tree. ``revalidate_rewritten_files`` is what closes the gap. It is a
gate, and a gate that stops running does not report an error, it reports
success, so the tests below assert the commands actually ran rather than
only that the return value is ``True``.

Coverage per docs/CODE_REVIEW.md § Gate Scripts Require Tests:
- Both checks run, in order, against the repo root.
- Each step failing independently fails the gate.
- A live counterpart for the pass case, so a stubbed-out gate that always
  returns ``True`` fails the suite.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.fixture_helpers import load_script

_mod = load_script("scripts/release.py")

_REPO_ROOT = Path("/repo")


class _Recorder:
    """Stands in for ``subprocess.run``, recording calls and failing chosen ones."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self._fail_on = fail_on

    def __call__(self, command: list[str], **kwargs: object) -> object:
        self.calls.append(command)
        if self._fail_on is not None and self._fail_on in " ".join(command):
            raise subprocess.CalledProcessError(1, command)
        return object()


def _run(monkeypatch: pytest.MonkeyPatch, fail_on: str | None = None) -> tuple[bool, _Recorder]:
    recorder = _Recorder(fail_on)
    monkeypatch.setattr(_mod.subprocess, "run", recorder)
    return _mod.revalidate_rewritten_files(_REPO_ROOT), recorder


def test_passes_when_both_checks_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clean rewritten files return True."""
    ok, _ = _run(monkeypatch)
    assert ok is True


def test_runs_the_changelog_structure_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The make target actually runs — a gate that stops running is the failure mode."""
    _, recorder = _run(monkeypatch)
    assert ["make", "changelog-check"] in recorder.calls


def test_runs_the_changelog_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test file that reads the real CHANGELOG is re-run after the rewrite."""
    _, recorder = _run(monkeypatch)
    pytest_calls = [call for call in recorder.calls if "pytest" in call]
    assert len(pytest_calls) == 1
    assert "tests/lib/test_check_changelog.py" in pytest_calls[0]


def test_both_checks_run_and_structure_comes_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two checks, structure before tests, so the cheap one reports first."""
    _, recorder = _run(monkeypatch)
    assert len(recorder.calls) == 2
    assert recorder.calls[0] == ["make", "changelog-check"]


def test_runs_against_the_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both commands are cwd-anchored to the repo, not to the caller's directory."""
    seen: list[object] = []

    def recorder(command: list[str], **kwargs: object) -> object:
        seen.append(kwargs.get("cwd"))
        return object()

    monkeypatch.setattr(_mod.subprocess, "run", recorder)
    _mod.revalidate_rewritten_files(_REPO_ROOT)
    assert seen == [_REPO_ROOT, _REPO_ROOT]


@pytest.mark.parametrize(
    ("fail_on", "expected_calls"),
    [
        pytest.param("changelog-check", 1, id="structure-gate-fails"),
        pytest.param("pytest", 2, id="changelog-tests-fail"),
    ],
)
def test_a_failing_step_fails_the_gate(monkeypatch: pytest.MonkeyPatch, fail_on: str, expected_calls: int) -> None:
    """Either step failing returns False, and the run stops there."""
    ok, recorder = _run(monkeypatch, fail_on=fail_on)
    assert ok is False
    assert len(recorder.calls) == expected_calls
