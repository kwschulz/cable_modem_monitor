"""Tests for scripts/check_suppression_discipline.py.

Coverage breakdown per docs/CODE_REVIEW.md § Test File Standards:
- Comment-token scan logic — table-driven inline (Table-driven tests,
  Schema tests use fixtures).
- Diff-parser shapes — fixture-driven JSON bundles
  (tests/lib/fixtures/suppression_diffs/*.json) so adding a case is
  adding one file (Schema tests use fixtures, No inline data blobs).
  Mirrors the parser-coordinator fixture pattern.
- File-level scan, audit, and main() modes — behavioural with tmp_path
  files and mocked subprocess (no real git invocation needed).

The scanner uses Python's tokenize module so that suppression text
appearing inside docstrings or string literals is correctly ignored
— only real source-level COMMENT tokens are flagged. Tests below
exercise that distinction directly.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from tests.fixture_helpers import collect_fixtures, load_fixture, load_script

_mod = load_script("scripts/check_suppression_discipline.py")


_FIXTURES = Path(__file__).parent / "fixtures" / "suppression_diffs"


# ---------------------------------------------------------------------------
# _scan_comment — table-driven (per docs/CODE_REVIEW.md § Test File Standards)
# ---------------------------------------------------------------------------

# Inputs are COMMENT-token text (the leading `#` plus everything after it
# up to end-of-line, as Python's tokenizer presents it).
#
# fmt: off
_COMMENT_CASES = [
    # description, comment_text, flagged
    ("plain comment, no suppression",            "# explanation",                                  False),
    ("type: ignore bare",                        "# type: ignore",                                 True),
    ("type: ignore[code] no rationale",          "# type: ignore[arg-type]",                       True),
    ("type: ignore[code] with rationale",        "# type: ignore[arg-type]  # mock dynamic attr",  False),
    ("type: ignore plus free text",              "# type: ignore[arg-type] free-text only",        True),
    ("pyright: ignore[code] no rationale",       "# pyright: ignore[reportFoo]",                   True),
    ("pyright: ignore[code] with rationale",     "# pyright: ignore[reportFoo]  # known limit",    False),
    ("bare ruff/flake suppression",              "# noq" "a",                                      True),
    ("ruff/flake suppression with code",         "# noq" "a: B008",                                False),
]
# fmt: on


@pytest.mark.parametrize("desc,comment_text,flagged", _COMMENT_CASES, ids=[c[0] for c in _COMMENT_CASES])
def test_scan_comment(desc: str, comment_text: str, flagged: bool) -> None:
    """Comment-text scan flags unjustified suppressions, passes everything else."""
    result = _mod._scan_comment(comment_text)
    assert (result is not None) is flagged


# ---------------------------------------------------------------------------
# _has_justification — edge cases (inline, behavioural)
# ---------------------------------------------------------------------------


def test_has_justification_rejects_only_whitespace_after() -> None:
    """`# type: ignore[code]   ` (trailing spaces only) is not justified."""
    text = "# type: ignore[arg-type]   "
    pattern = _mod._SUPPRESSION_PATTERNS[0][1]
    match = pattern.search(text)
    assert match is not None
    assert _mod._has_justification(text, match) is False


def test_has_justification_rejects_double_hash_with_no_text() -> None:
    """`# type: ignore[code]  #` is empty second comment, not justified."""
    text = "# type: ignore[arg-type]  #"
    pattern = _mod._SUPPRESSION_PATTERNS[0][1]
    match = pattern.search(text)
    assert match is not None
    assert _mod._has_justification(text, match) is False


def test_has_justification_rejects_free_text_without_hash() -> None:
    """Free text after the suppression must use a `#` to be a comment."""
    text = "# type: ignore[arg-type] free text"
    pattern = _mod._SUPPRESSION_PATTERNS[0][1]
    match = pattern.search(text)
    assert match is not None
    assert _mod._has_justification(text, match) is False


# ---------------------------------------------------------------------------
# _parse_diff_added_lines — fixture-driven (per docs/CODE_REVIEW.md § Test File Standards)
# ---------------------------------------------------------------------------

_DIFF_FIXTURES = collect_fixtures(_FIXTURES)


@pytest.mark.parametrize("fixture_path", _DIFF_FIXTURES, ids=[p.stem for p in _DIFF_FIXTURES])
def test_parse_diff_added_lines(fixture_path: Path) -> None:
    """Each fixture is a single JSON bundle: ``_diff`` (input) plus ``_expected``
    (output). Adding a case is adding one file. Mirrors the parser-coordinator
    fixture pattern under ``tests/parsers/fixtures/coordinator/valid/``."""
    fixture = load_fixture(fixture_path)
    expected_tuples = [tuple(item) for item in fixture["_expected"]]

    actual = _mod._parse_diff_added_lines(fixture["_diff"])

    assert (
        actual == expected_tuples
    ), f"Mismatch for {fixture_path.stem}:\n  actual:   {actual}\n  expected: {expected_tuples}"


# ---------------------------------------------------------------------------
# Suppression patterns themselves — sanity check the regex
# ---------------------------------------------------------------------------


def test_pattern_distinguishes_bare_from_coded() -> None:
    """The bare ruff/flake pattern must NOT match the code-specific form."""
    bare_pattern = _mod._SUPPRESSION_PATTERNS[2][1]
    assert isinstance(bare_pattern, re.Pattern)
    assert bare_pattern.search("# noq" "a")
    assert not bare_pattern.search("# noq" "a: B008")
    assert not bare_pattern.search("# noq" "a:B008")


# ---------------------------------------------------------------------------
# _file_violations — tokenize-based, distinguishes comments from strings
# ---------------------------------------------------------------------------


def _write_py(tmp_path: Path, name: str, content: str) -> Path:
    """Helper: write a Python file under tmp_path and return its path."""
    p = tmp_path / name
    p.write_text(content)
    return p


def test_file_violations_flags_real_comment_suppression(tmp_path: Path) -> None:
    """A real source-level comment suppression is flagged."""
    path = _write_py(tmp_path, "a.py", "x = 1  # type: ignore[arg-type]\n")
    violations = _mod._file_violations(path)
    assert len(violations) == 1
    line, rule, match = violations[0]
    assert line == 1
    assert rule == "type: ignore"
    assert match.group(0).startswith("# type: ignore")


def test_file_violations_passes_justified_comment(tmp_path: Path) -> None:
    """A justified comment suppression is not flagged."""
    path = _write_py(
        tmp_path,
        "a.py",
        "x = 1  # type: ignore[arg-type]  # rationale here\n",
    )
    assert _mod._file_violations(path) == []


def test_file_violations_skips_string_literal(tmp_path: Path) -> None:
    """Suppression-shaped text inside a string literal is NOT a comment;
    tokenize correctly classifies it as STRING and the scanner skips it."""
    path = _write_py(tmp_path, "a.py", 'x = "code # type: ignore[arg-type]"\n')
    assert _mod._file_violations(path) == []


def test_file_violations_skips_docstring(tmp_path: Path) -> None:
    """Suppression text inside a docstring is part of a STRING token,
    not a COMMENT token, and is correctly skipped."""
    path = _write_py(
        tmp_path,
        "a.py",
        '"""Module docstring mentioning # type: ignore as documentation."""\n',
    )
    assert _mod._file_violations(path) == []


def test_file_violations_skips_test_fixture_text(tmp_path: Path) -> None:
    """A test file containing suppression text inside a list-of-strings
    fixture (the exact pattern this scanner's own tests use) is not
    flagged because the text lives inside STRING tokens."""
    content = """
CASES = [
    ("description", "x = 1  # type: ignore[arg-type]", True),
    ("another", "y = 2  # type: ignore[no-any-return]", False),
]
"""
    path = _write_py(tmp_path, "a.py", content)
    assert _mod._file_violations(path) == []


def test_file_violations_handles_unreadable_file(tmp_path: Path) -> None:
    """A path that does not exist returns no violations (graceful)."""
    missing = tmp_path / "nope.py"
    assert _mod._file_violations(missing) == []


def test_file_violations_handles_syntax_error(tmp_path: Path) -> None:
    """A file that fails to tokenize returns no violations (skip rather
    than crash the gate)."""
    path = _write_py(tmp_path, "broken.py", "def f(:\n  pass\n")
    # Should not raise; returns empty list.
    assert _mod._file_violations(path) == []


# ---------------------------------------------------------------------------
# _scan_added — diff-driven scanning across files
# ---------------------------------------------------------------------------


def test_scan_added_filters_by_added_lines(tmp_path: Path, monkeypatch) -> None:
    """Only suppressions on added lines are flagged; pre-existing
    suppressions on unchanged lines are grandfathered."""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "a.py"
    path.write_text("old_existing = 1  # type: ignore[arg-type]\n" "newly_added = 2  # type: ignore[arg-type]\n")
    # Diff says line 2 was added. Line 1 is pre-existing.
    added = [("a.py", 2, "newly_added = 2  # type: ignore[arg-type]")]
    violations = _mod._scan_added(added)
    assert len(violations) == 1
    assert "a.py:2" in violations[0]


def test_scan_added_returns_empty_for_clean(tmp_path: Path, monkeypatch) -> None:
    """No additions means no violations, even if file has existing suppressions."""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "a.py"
    path.write_text("x = 1  # type: ignore[arg-type]\n")
    assert _mod._scan_added([]) == []


def test_scan_added_skips_missing_file(tmp_path: Path, monkeypatch) -> None:
    """A file path in the diff that doesn't exist on disk is skipped silently."""
    monkeypatch.chdir(tmp_path)
    added = [("ghost.py", 1, "x = 1  # type: ignore")]
    assert _mod._scan_added(added) == []


# ---------------------------------------------------------------------------
# _scan_all_tracked — audit mode, mocked subprocess + tmp_path files
# ---------------------------------------------------------------------------


def test_scan_all_tracked_finds_violations(tmp_path: Path, monkeypatch) -> None:
    """Audit mode tokenizes every tracked .py and flags real-comment suppressions."""
    good = tmp_path / "good.py"
    bad = tmp_path / "bad.py"
    good.write_text("x = 1  # type: ignore[arg-type]  # rationale\n")
    bad.write_text("y = 2  # type: ignore[arg-type]\n")

    def fake_check_output(cmd: list[str], **kwargs: object) -> str:
        return f"{good}\n{bad}\n"

    monkeypatch.setattr(_mod.subprocess, "check_output", fake_check_output)
    violations = _mod._scan_all_tracked()
    assert len(violations) == 1
    assert str(bad) in violations[0]


def test_scan_all_tracked_skips_missing_file(monkeypatch) -> None:
    """A path in `git ls-files` output that doesn't exist on disk is skipped."""
    missing = "/nonexistent/path.py"

    def fake_check_output(cmd: list[str], **kwargs: object) -> str:
        return f"{missing}\n"

    monkeypatch.setattr(_mod.subprocess, "check_output", fake_check_output)
    assert _mod._scan_all_tracked() == []


# ---------------------------------------------------------------------------
# main() — argument parsing and exit codes
# ---------------------------------------------------------------------------


def test_main_audit_mode_returns_1_on_violations(monkeypatch, capsys) -> None:
    """`--all` exits 1 when violations exist, prints to stderr."""
    monkeypatch.setattr(_mod, "_scan_all_tracked", lambda: ["fake.py:1: violation"])
    monkeypatch.setattr(sys, "argv", ["script", "--all"])
    rc = _mod.main()
    assert rc == 1
    err = capsys.readouterr().err
    assert "1 violation(s)" in err
    assert "fake.py:1" in err


def test_main_audit_mode_returns_0_when_clean(monkeypatch) -> None:
    monkeypatch.setattr(_mod, "_scan_all_tracked", list)
    monkeypatch.setattr(sys, "argv", ["script", "--all"])
    assert _mod.main() == 0


def test_main_branch_mode_invokes_diff_against_base(monkeypatch) -> None:
    """`--branch BASE` uses git diff BASE...HEAD."""
    captured: list[list[str]] = []

    def fake_added(args: list[str]) -> list:
        captured.append(args)
        return []

    monkeypatch.setattr(_mod, "_git_added_lines", fake_added)
    monkeypatch.setattr(sys, "argv", ["script", "--branch", "origin/main"])
    rc = _mod.main()
    assert rc == 0
    assert captured == [["origin/main...HEAD"]]


def test_main_default_mode_uses_staged(monkeypatch) -> None:
    """No args = scans staged diff (for pre-commit)."""
    captured: list[list[str]] = []

    def fake_added(args: list[str]) -> list:
        captured.append(args)
        return []

    monkeypatch.setattr(_mod, "_git_added_lines", fake_added)
    monkeypatch.setattr(sys, "argv", ["script"])
    rc = _mod.main()
    assert rc == 0
    assert captured == [["--cached"]]


# ---------------------------------------------------------------------------
# _git_added_lines — error handling
# ---------------------------------------------------------------------------


def test_git_added_lines_exits_on_failure(monkeypatch) -> None:
    """A failing git diff invocation exits with code 2 (invocation error)."""

    def fake_check_output(*args: object, **kwargs: object) -> str:
        raise _mod.subprocess.CalledProcessError(returncode=128, cmd="git diff")

    monkeypatch.setattr(_mod.subprocess, "check_output", fake_check_output)

    with pytest.raises(SystemExit) as exc_info:
        _mod._git_added_lines(["--cached"])
    assert exc_info.value.code == 2
