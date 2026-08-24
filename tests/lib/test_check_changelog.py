"""Tests for scripts/check_changelog.py.

CHANGELOG.md is the binding signal for the release version bump — the
section header under ``## [Unreleased]`` decides whether a release is a
patch or a minor, and ``scripts/release.py`` promotes the block
verbatim. A structure checker that silently passes therefore does not
produce a messy file, it produces a wrong version number. Nothing
tested this checker until now.

Coverage breakdown per docs/CODE_REVIEW.md § Test File Standards:
- ``validate`` violation detection — table-driven inline, one row per
  documented rule.
- Guard cases (fenced code, historical prose, prerelease ordering) —
  each paired with a live counterpart asserting the same rule still
  fires outside the guard, so a checker that has stopped working fails
  the suite instead of passing quietly.
- ``touched_lines`` diff-scoping — behavioural, with git stubbed.

The repo's own CHANGELOG carries 70 historical violations by design
(entries published before these rules existed use free-form sections),
so the real-file regression asserts the *Unreleased* block is clean
rather than the whole file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixture_helpers import load_script

_mod = load_script("scripts/check_changelog.py")

_CHANGELOG = Path(__file__).resolve().parents[2] / "CHANGELOG.md"

_HEADER = [
    "# Changelog",
    "",
    "## [Unreleased]",
    "",
    "### Fixed",
    "",
    "- A fix.",
    "",
]


def _messages(lines: list[str]) -> list[str]:
    return [message for _, message, _ in _mod.validate(lines)]


# ---------------------------------------------------------------------------
# Violations — each documented rule must actually fire
# ---------------------------------------------------------------------------

VIOLATIONS = [
    pytest.param(
        [*_HEADER, "## [Unreleased]", ""],
        "duplicate [Unreleased]",
        id="duplicate-unreleased",
    ),
    pytest.param(
        ["# Changelog", "", "## [1.0.0] - 2026-01-01", "", "### Fixed", "", "- x.", "", "## [Unreleased]", ""],
        "[Unreleased] must be the first version heading",
        id="unreleased-not-first",
    ),
    pytest.param(
        ["# Changelog", "", "## [1.0.0] - 2026-01-01", "", "### Fixed", "", "- x.", ""],
        "missing [Unreleased]",
        id="missing-unreleased",
    ),
    pytest.param(
        [*_HEADER, "## [1.0.0] - 2026-13-45", "", "### Fixed", "", "- x.", ""],
        "invalid date in heading",
        id="invalid-date",
    ),
    pytest.param(
        [
            *_HEADER,
            "## [1.0.0] - 2026-01-02",
            "",
            "### Fixed",
            "",
            "- x.",
            "",
            "## [1.0.0] - 2026-01-01",
            "",
            "### Fixed",
            "",
            "- y.",
            "",
        ],
        "already listed at line",
        id="duplicate-version",
    ),
    pytest.param(
        [
            *_HEADER,
            "## [1.0.0] - 2026-01-01",
            "",
            "### Fixed",
            "",
            "- x.",
            "",
            "## [2.0.0] - 2026-02-01",
            "",
            "### Fixed",
            "",
            "- y.",
            "",
        ],
        "is not below preceding",
        id="versions-ascending",
    ),
    pytest.param(
        [*_HEADER, "## [1.0.0] - 2026-01-01", "", "### Notes", "", "- x.", ""],
        "unknown section 'Notes'",
        id="unknown-section",
    ),
    pytest.param(
        [*_HEADER, "## [1.0.0] - 2026-01-01", "", "### Fixed", "", "- x.", "", "### Fixed", "", "- y.", ""],
        "repeated within the same version",
        id="repeated-section",
    ),
    pytest.param(
        [*_HEADER, "## [1.0.0] - 2026-01-01", "", "Loose prose with no section.", ""],
        "body text must be under a ### section",
        id="body-outside-section",
    ),
    pytest.param(
        [*_HEADER, "## [1.0.0] - 2026-01-01", "", "### Fixed", "", "- Ships P12 milestone.", ""],
        "roadmap P-number is not allowed",
        id="roadmap-p-number",
    ),
    pytest.param(
        [*_HEADER, "## [1.0.0] - 2026-01-01", "", "### Fixed", "", "- TODO write this up.", ""],
        "placeholder token",
        id="placeholder-token",
    ),
    pytest.param(
        [*_HEADER, "## [1.0] - 2026-01-01", "", "### Fixed", "", "- x.", ""],
        "malformed version heading",
        id="malformed-version-heading",
    ),
]


@pytest.mark.parametrize(("lines", "expected"), VIOLATIONS)
def test_violation_is_reported(lines: list[str], expected: str) -> None:
    """Every documented structural rule produces a violation."""
    messages = _messages(lines)
    assert any(expected in message for message in messages), f"expected {expected!r}, got {messages}"


# ---------------------------------------------------------------------------
# Valid structures — no false positives
# ---------------------------------------------------------------------------


def test_well_formed_changelog_is_clean() -> None:
    """A minimal correct changelog produces no violations."""
    lines = [
        *_HEADER,
        "## [1.1.0] - 2026-02-01",
        "",
        "### Added",
        "",
        "- Something new.",
        "",
        "### Fixed",
        "",
        "- Something fixed.",
        "",
        "## [1.0.0] - 2026-01-01",
        "",
        "### Security",
        "",
        "- Something hardened.",
        "",
    ]
    assert _messages(lines) == []


def test_prerelease_versions_order_correctly() -> None:
    """release < rc < beta < alpha, descending, is a valid sequence."""
    lines = [*_HEADER]
    for version, date in [
        ("1.0.0", "2026-05-01"),
        ("1.0.0-rc.1", "2026-04-01"),
        ("1.0.0-beta.2", "2026-03-01"),
        ("1.0.0-beta.1", "2026-02-01"),
        ("1.0.0-alpha.1", "2026-01-01"),
    ]:
        lines += [f"## [{version}] - {date}", "", "### Fixed", "", "- x.", ""]
    assert _messages(lines) == []


def test_prerelease_ordering_guard_still_catches_a_reversal() -> None:
    """Live counterpart: ordering is genuinely checked, not waved through."""
    lines = [
        *_HEADER,
        "## [1.0.0-beta.1] - 2026-01-01",
        "",
        "### Fixed",
        "",
        "- x.",
        "",
        "## [1.0.0-beta.2] - 2026-02-01",
        "",
        "### Fixed",
        "",
        "- y.",
        "",
    ]
    assert any("is not below preceding" in m for m in _messages(lines))


# ---------------------------------------------------------------------------
# Fenced code — guarded, with live counterparts
# ---------------------------------------------------------------------------


def test_fenced_content_is_ignored() -> None:
    """Headings and tokens inside a code fence are documentation, not structure."""
    lines = [
        *_HEADER,
        "```markdown",
        "## [Unreleased]",
        "### Notes",
        "- TODO example placeholder",
        "- Ships P12 milestone",
        "```",
        "",
    ]
    assert _messages(lines) == []


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param("### Notes", "unknown section 'Notes'", id="section"),
        pytest.param("- TODO example placeholder", "placeholder token", id="placeholder"),
        pytest.param("- Ships P12 milestone", "roadmap P-number is not allowed", id="p-number"),
        pytest.param("## [Unreleased]", "duplicate [Unreleased]", id="unreleased"),
    ],
)
def test_fence_guard_does_not_swallow_unfenced_violations(line: str, expected: str) -> None:
    """Live counterpart to the fence guard.

    The same content outside a fence must still be reported — otherwise
    a fence-tracking bug would silently disable the whole checker.
    """
    lines = [*_HEADER, "## [1.0.0] - 2026-01-01", "", "### Fixed", "", line, ""]
    assert any(expected in message for message in _messages(lines))


def test_unclosed_fence_is_reported() -> None:
    """A stray fence must not silently disable the rest of the file.

    Fenced content is skipped as documentation, so an unclosed fence
    suppresses every rule below it — including the rest of the entry the
    author is adding. The violation is reported rather than the file
    passing as clean.
    """
    lines = [*_HEADER, "```", "### Notes", "- TODO placeholder", ""]
    assert any("unclosed code fence" in message for message in _messages(lines))


def test_unclosed_fence_is_reported_at_its_opening_line() -> None:
    """Anchored to the opening line so the default diff scope catches it.

    Reported at line 0 it would need the file-level plumbing that
    ``missing [Unreleased]`` uses; anchored to the fence it lands on a
    line the author just touched.
    """
    lines = [*_HEADER, "```", "- TODO placeholder", ""]
    fence_line = lines.index("```") + 1
    reported = [lineno for lineno, message, _ in _mod.validate(lines) if "unclosed code fence" in message]
    assert reported == [fence_line]


def test_balanced_fences_are_still_clean() -> None:
    """Live counterpart: the new check does not fire on a closed fence."""
    lines = [*_HEADER, "```markdown", "### Notes", "- TODO placeholder", "```", ""]
    assert _messages(lines) == []


# ---------------------------------------------------------------------------
# Real-file regression
# ---------------------------------------------------------------------------


def test_unreleased_block_of_real_changelog_is_clean() -> None:
    """The block the release flow promotes must validate.

    Scoped to Unreleased: entries published before these rules existed
    use free-form sections and are read for context only.
    """
    lines = _CHANGELOG.read_text(encoding="utf-8").splitlines()
    start = lines.index(_mod._UNRELEASED)
    end = next(
        (i for i in range(start + 1, len(lines)) if _mod._VERSION_HEADING.match(lines[i])),
        len(lines),
    )
    block = ["# Changelog", "", *lines[start:end]]
    assert _messages(block) == []


def test_real_changelog_sections_are_known_in_the_unreleased_block() -> None:
    """The release flow reads the section name; an unknown one breaks the bump."""
    lines = _CHANGELOG.read_text(encoding="utf-8").splitlines()
    start = lines.index(_mod._UNRELEASED)
    end = next(
        (i for i in range(start + 1, len(lines)) if _mod._VERSION_HEADING.match(lines[i])),
        len(lines),
    )
    # An empty Unreleased block is the normal state between a release bump and
    # the next entry, so only the names are asserted, never their presence.
    sections = [line[4:] for line in lines[start:end] if line.startswith("### ")]
    assert all(name in _mod._SECTIONS for name in sections), sections


# ---------------------------------------------------------------------------
# touched_lines — diff scoping
# ---------------------------------------------------------------------------


def test_touched_lines_parses_added_hunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """An added block maps to its line range."""
    monkeypatch.setattr(_mod, "_resolve_base", lambda base: base)
    monkeypatch.setattr(
        _mod,
        "_run_git",
        lambda args: "abc123\n" if args[0] == "merge-base" else "@@ -10,0 +11,3 @@\n@@ -20,1 +23,1 @@\n",
    )
    assert _mod.touched_lines(Path("CHANGELOG.md"), "origin/main") == {11, 12, 13, 23}


def test_touched_lines_covers_the_neighbor_of_a_pure_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    """A removed ### heading leaves bullets stranded under a version.

    The deletion has no added lines of its own, so the surviving
    neighbors must be in scope or the stranded bullets go unreported.
    """
    monkeypatch.setattr(_mod, "_resolve_base", lambda base: base)
    monkeypatch.setattr(
        _mod,
        "_run_git",
        lambda args: "abc123\n" if args[0] == "merge-base" else "@@ -30,2 +29,0 @@\n",
    )
    assert _mod.touched_lines(Path("CHANGELOG.md"), "origin/main") == {29, 30}
