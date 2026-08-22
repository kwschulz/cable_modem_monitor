#!/usr/bin/env python3
"""Structural validator for CHANGELOG.md.

CHANGELOG.md is written by the maintainer at release time and promoted
verbatim by scripts/release.py, which renames ``## [Unreleased]`` to the
new version. Nothing else checks its shape, so a malformed file would
ship. This validates the Keep a Changelog structure the release flow
depends on (see docs/reference/RELEASING.md § CHANGELOG ownership):

- ``## [Unreleased]`` is present exactly once and is the first version heading
- every other version heading is ``## [X.Y.Z] - YYYY-MM-DD`` (optional
  ``-alpha.N`` / ``-beta.N`` / ``-rc.N``) with a real date, versions are
  strictly descending, and no version appears twice
- ``###`` headings are only the six Keep a Changelog sections, each at
  most once per version
- body text sits under a ``###`` section, never directly under a version
- no roadmap ``P<n>`` identifiers (CLAUDE.md § No P-numbers in public
  artifacts) and no TODO / TBD / FIXME / XXX placeholders

Only lines this branch touches are reported. Entries published before
these rules existed use free-form sections and prose under the version
heading; they are release notes that already shipped, so they are read
for context (version order, duplicate detection) but never flagged.
``--all`` reports on the whole file.

Deletions are covered by the surviving neighbor line, so removing a
``###`` heading still flags the bullets left under a version heading.
Uncommitted edits count: the diff is working tree against branch point.

Exit codes:
  0  Structure valid
  1  At least one violation (each printed as path:line: message)
  2  Invocation error (file missing, base ref does not resolve)
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

_SECTIONS = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
_PRERELEASE_RANK = {"alpha": 0, "beta": 1, "rc": 2}
_RELEASE_RANK = 3

_UNRELEASED = "## [Unreleased]"
_VERSION_HEADING = re.compile(
    r"^## \[(?P<version>\d+\.\d+\.\d+(?:-(?P<pre>alpha|beta|rc)\.(?P<pre_n>\d+))?)\]" r" - (?P<date>\d{4}-\d{2}-\d{2})$"
)
_SECTION_HEADING = re.compile(r"^### (?P<name>.*)$")
_ROADMAP_ID = re.compile(r"\bP\d{1,3}\b")
_PLACEHOLDER = re.compile(r"\b(?:TODO|TBD|FIXME|XXX)\b")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")

# (line, message, other_line). other_line is the second line of a
# two-line violation (order, duplicate); the violation is in scope when
# either line was touched, so adding a heading above an existing one
# still reports.
Problem = tuple[int, str, int | None]


def _version_key(match: re.Match[str]) -> tuple[int, ...]:
    major, minor, patch = (int(p) for p in match["version"].split("-")[0].split("."))
    if match["pre"] is None:
        return (major, minor, patch, _RELEASE_RANK, 0)
    return (major, minor, patch, _PRERELEASE_RANK[match["pre"]], int(match["pre_n"]))


class _Validator:
    """Line-by-line state machine over the changelog."""

    def __init__(self) -> None:
        self.problems: list[Problem] = []
        self.seen_unreleased = False
        self.seen_any_version = False
        self.seen_versions: dict[str, int] = {}
        self.prev_key: tuple[int, ...] | None = None
        self.prev_version = ""
        self.prev_lineno = 0
        self.current_section: str | None = None
        self.sections_in_block: dict[str, int] = {}

    def _start_block(self) -> None:
        self.current_section = None
        self.sections_in_block = {}
        self.seen_any_version = True

    def _add(self, lineno: int, message: str, other: int | None = None) -> None:
        self.problems.append((lineno, message, other))

    def _unreleased(self, lineno: int) -> None:
        if self.seen_unreleased:
            self._add(lineno, "duplicate [Unreleased] heading")
        elif self.seen_any_version:
            self._add(lineno, "[Unreleased] must be the first version heading")
        self.seen_unreleased = True
        self._start_block()

    def _version(self, lineno: int, match: re.Match[str]) -> None:
        version = match["version"]
        try:
            datetime.date.fromisoformat(match["date"])
        except ValueError:
            self._add(lineno, f"invalid date in heading: {match['date']}")
        if version in self.seen_versions:
            first = self.seen_versions[version]
            self._add(lineno, f"version {version} already listed at line {first}", first)
        self.seen_versions[version] = lineno
        key = _version_key(match)
        if self.prev_key is not None and key >= self.prev_key:
            self._add(lineno, f"version {version} is not below preceding {self.prev_version}", self.prev_lineno)
        self.prev_key, self.prev_version, self.prev_lineno = key, version, lineno
        if not self.seen_unreleased:
            self._add(lineno, "[Unreleased] heading must precede all version headings")
        self._start_block()

    def _section(self, lineno: int, name: str) -> None:
        if name not in _SECTIONS:
            self._add(lineno, f"unknown section '{name}' (expected one of {', '.join(_SECTIONS)})")
        elif name in self.sections_in_block:
            self._add(lineno, f"section '{name}' repeated within the same version", self.sections_in_block[name])
        self.sections_in_block[name] = lineno
        self.current_section = name

    def _body(self, lineno: int, line: str) -> None:
        if self.seen_any_version and self.current_section is None and line.strip():
            self._add(lineno, "body text must be under a ### section, not directly under a version")
        if _ROADMAP_ID.search(line):
            self._add(lineno, "roadmap P-number is not allowed in a public artifact")
        if _PLACEHOLDER.search(line):
            self._add(lineno, "placeholder token (TODO/TBD/FIXME/XXX)")

    def feed(self, lineno: int, line: str) -> None:
        if line == _UNRELEASED:
            self._unreleased(lineno)
        elif version_match := _VERSION_HEADING.match(line):
            self._version(lineno, version_match)
        elif line.startswith("## "):
            self._add(lineno, f"malformed version heading: {line}")
            self._start_block()
        elif section_match := _SECTION_HEADING.match(line):
            self._section(lineno, section_match["name"])
        else:
            self._body(lineno, line)

    def finish(self) -> list[Problem]:
        if not self.seen_unreleased:
            self._add(0, "missing [Unreleased] heading")
        return self.problems


def validate(lines: list[str]) -> list[Problem]:
    """Return (line_number, message) violations for the whole file."""
    validator = _Validator()
    in_fence = False
    for lineno, line in enumerate(lines, start=1):
        if line.startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            validator.feed(lineno, line)
    return validator.finish()


def _run_git(args: list[str]) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(f"error: git {' '.join(args)} failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(2)
    return result.stdout


def _resolve_base(base: str) -> str:
    """Return base if it resolves, falling back origin/main -> main."""
    for candidate in (base, "main") if base == "origin/main" else (base,):
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", candidate],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            return candidate
    print(f"error: base ref '{base}' does not resolve", file=sys.stderr)
    sys.exit(2)


def touched_lines(path: Path, base: str) -> set[int]:
    """Line numbers this branch adds, plus the neighbor surviving a deletion.

    Diffs the working tree against the branch point, so an edit counts
    before it is committed and `make validate-ci` sees what CI will see.
    """
    merge_base = _run_git(["merge-base", _resolve_base(base), "HEAD"]).strip()
    diff = _run_git(["diff", "--unified=0", merge_base, "--", str(path)])
    touched: set[int] = set()
    for line in diff.splitlines():
        if hunk := _HUNK.match(line):
            start = int(hunk["start"])
            count = 1 if hunk["count"] is None else int(hunk["count"])
            # count == 0 is a pure deletion: `start` is the line before the
            # removed block, so flag it and the line that closed over the gap.
            touched.update(range(start, start + count) if count else (start, start + 1))
    return touched


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate CHANGELOG.md structure")
    parser.add_argument(
        "path",
        nargs="?",
        default=Path(__file__).resolve().parent.parent / "CHANGELOG.md",
        type=Path,
    )
    parser.add_argument("--base", default="origin/main", help="branch point to diff against (default: origin/main)")
    parser.add_argument("--all", action="store_true", help="report on the whole file, not just touched lines")
    args = parser.parse_args()
    if not args.path.is_file():
        print(f"error: {args.path} not found", file=sys.stderr)
        return 2

    problems = validate(args.path.read_text(encoding="utf-8").splitlines())
    scope = "whole file"
    if not args.all:
        touched = touched_lines(args.path, args.base)
        # Line 0 is the file-level "missing [Unreleased]"; it applies
        # whenever this branch touched the file at all.
        problems = [p for p in problems if p[0] in touched or p[2] in touched or (p[0] == 0 and touched)]
        scope = f"{len(touched)} line(s) changed vs {args.base}"

    for lineno, message, _ in problems:
        print(f"{args.path}:{lineno}: {message}")
    if problems:
        print(f"\n{len(problems)} CHANGELOG.md structure violation(s)")
        return 1
    print(f"CHANGELOG.md structure OK ({scope})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
