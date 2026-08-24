"""Tests for scripts/check_auto_close_keywords.py.

GitHub closes an issue when a merged commit body contains a closing
keyword directly followed by a reference — regardless of any qualifier
around it. "still need to confirm this fixes #12" closes #12 on merge.
The scanner exists to catch that phrasing before it merges.

The whole gate is one regex, so most of the value is in pinning both
sides of it: the phrasings that must be caught, and the ones that must
not be, since a pattern loose enough to flag "fix(core): handle #5 case"
would be turned off within a week.

Coverage breakdown per docs/CODE_REVIEW.md § Test File Standards:
- ``_PATTERN`` — table-driven, matching and non-matching cases.
- ``_commits``, ``_resolve_base`` and ``main`` — behavioural against a
  real throwaway git repository built in tmp_path, so no subprocess
  mocking is needed (§ Test Overrides Are a Code Smell).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.fixture_helpers import load_script

_mod = load_script("scripts/check_auto_close_keywords.py")


# ---------------------------------------------------------------------------
# _PATTERN — phrasings GitHub would act on
# ---------------------------------------------------------------------------

# fmt: off
CLOSES = [
    pytest.param("Fixes #12",                     id="fixes"),
    pytest.param("fixes #12",                     id="lowercase"),
    pytest.param("FIXES #12",                     id="uppercase"),
    pytest.param("Fixed #12",                     id="past-tense"),
    pytest.param("Fix #12",                       id="bare-stem"),
    pytest.param("Closes #12",                    id="closes"),
    pytest.param("closed #12",                    id="closed"),
    pytest.param("Resolves #12",                  id="resolves"),
    pytest.param("resolve #12",                   id="resolve"),
    pytest.param("Fixes: #12",                    id="colon"),
    pytest.param("Fixes   #12",                   id="extra-space"),
    pytest.param("Fixes\t#12",                    id="tab"),
    pytest.param("still need to confirm this fixes #12", id="qualified-still-closes"),
    pytest.param("Fixes owner/repo#12",           id="cross-repo"),
    pytest.param("Fixes https://github.com/owner/repo/issues/12", id="full-url"),
]

KEEPS_OPEN = [
    pytest.param("Related to #12",                id="related-to"),
    pytest.param("Addresses #12",                 id="addresses"),
    pytest.param("fix(core): handle #5 case",     id="conventional-subject"),
    pytest.param("fixes the #12 problem",         id="words-between"),
    pytest.param("prefix #12",                    id="keyword-inside-word"),
    pytest.param("affixes #12",                   id="keyword-as-suffix"),
    pytest.param("See #12",                       id="bare-reference"),
    pytest.param("Fixed the parser",              id="keyword-without-ref"),
    pytest.param("Fixes\n#12",                    id="ref-on-next-line"),
]
# fmt: on


@pytest.mark.parametrize("text", CLOSES)
def test_auto_close_phrasing_is_detected(text: str) -> None:
    """Anything GitHub would act on must be flagged."""
    assert _mod._PATTERN.search(text), f"expected a match in {text!r}"


@pytest.mark.parametrize("text", KEEPS_OPEN)
def test_safe_phrasing_is_not_flagged(text: str) -> None:
    """Live counterparts to the guards: no false positives.

    A scanner that flagged ordinary conventional-commit subjects would
    be disabled within a week, so these matter as much as the hits.
    """
    assert not _mod._PATTERN.search(text), f"unexpected match in {text!r}"


# ---------------------------------------------------------------------------
# Real git repository
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A throwaway repo whose base..HEAD holds the given commit messages."""

    def _build(messages: list[str]) -> str:
        _git(tmp_path, "init", "-q", "-b", "main")
        _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "initial")
        base = _git(tmp_path, "rev-parse", "HEAD")
        for message in messages:
            _git(tmp_path, "commit", "-q", "--allow-empty", "-m", message)
        monkeypatch.chdir(tmp_path)
        return base

    return _build


def test_commits_are_parsed_with_subject_and_body(repo) -> None:
    """The NUL/field-separated log format must survive multi-line bodies."""
    base = repo(["subject line\n\nbody line one\nbody line two"])
    commits = _mod._commits(base)
    assert len(commits) == 1
    _sha, subject, body = commits[0]
    assert subject == "subject line"
    assert "body line two" in body


def test_multiple_commits_are_all_returned(repo) -> None:
    base = repo(["first", "second", "third"])
    assert len(_mod._commits(base)) == 3


def test_no_commits_since_base_is_empty(repo) -> None:
    base = repo([])
    assert _mod._commits(base) == []


def test_resolve_base_falls_back_to_main(repo) -> None:
    """A fresh clone without origin/main still resolves."""
    repo([])
    assert _mod._resolve_base("origin/main") == "main"


def test_resolve_base_exits_on_an_unknown_ref(repo) -> None:
    repo([])
    with pytest.raises(SystemExit) as excinfo:
        _mod._resolve_base("no-such-ref")
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# main — the gate itself
# ---------------------------------------------------------------------------


def test_gate_fails_on_an_auto_close_body(repo, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    base = repo(["fix(core): handle timeout\n\nFixes #12"])
    monkeypatch.setattr("sys.argv", ["check_auto_close_keywords.py", "--base", base])

    assert _mod.main() == 1

    out = capsys.readouterr().out
    assert "auto-close phrase" in out
    assert "Related to #N" in out


def test_gate_passes_on_a_safe_body(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    """Live counterpart: the recommended phrasing is accepted."""
    base = repo(["fix(core): handle timeout\n\nRelated to #12"])
    monkeypatch.setattr("sys.argv", ["check_auto_close_keywords.py", "--base", base])
    assert _mod.main() == 0


def test_gate_scans_the_subject_line_too(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    """A keyword in the subject closes just as surely as one in the body."""
    base = repo(["Fixes #12"])
    monkeypatch.setattr("sys.argv", ["check_auto_close_keywords.py", "--base", base])
    assert _mod.main() == 1


def test_gate_reports_every_offending_commit(repo, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Not just the first — a branch can carry several."""
    base = repo(["a\n\nFixes #1", "b\n\nRelated to #2", "c\n\nCloses #3"])
    monkeypatch.setattr("sys.argv", ["check_auto_close_keywords.py", "--base", base])

    assert _mod.main() == 1

    out = capsys.readouterr().out
    assert "#1" in out
    assert "#3" in out
    assert "#2" not in out
