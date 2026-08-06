"""Find symbols the specs assert that do not exist in the codebase.

Complements the earlier doc-reference audit (anchors, UC refs, links,
paths) with the class it never covered: function, method, class and
constant names. A spec naming a callable that no .py defines misdirects
anyone -- human or AI -- who greps for it.

Candidates are deliberately narrow. Illustrative snippets legitimately
invent locals and parameters, so only references that assert a real
attribute or definition are collected.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
from collections import defaultdict
from pathlib import Path

ROOT = Path(
    subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip()
)

SPEC_GLOBS = [
    "packages/cable_modem_monitor_core/docs/*.md",
    "packages/cable_modem_monitor_catalog_tools/docs/*.md",
    "custom_components/cable_modem_monitor/docs/*.md",
    "docs/*.md",
]

# self._foo(  /  self.FOO  -- asserts an attribute on a real class
RE_SELF_CALL = re.compile(r"\bself\.(_?[a-z][a-z0-9_]*)\s*\(")
RE_SELF_CONST = re.compile(r"\bself\.([A-Z][A-Z0-9_]{2,})\b")
# def foo(  -- asserts a definition
RE_DEF = re.compile(r"^\s*(?:async\s+)?def\s+([a-z_][a-z0-9_]*)\s*\(", re.M)
# class Foo  -- asserts a class
RE_CLASS = re.compile(r"^\s*class\s+([A-Z][A-Za-z0-9_]*)", re.M)
# `foo()` or `Foo.bar()` in inline code spans
RE_INLINE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\(\)`")

# Names that are Python builtins, stdlib, or third-party -- not ours to define.
# fmt: off
IGNORE = {
    "int", "str", "dict", "list", "float", "bool", "set", "len", "min", "max",
    "sum", "any", "all", "print", "range", "super", "isinstance", "getattr",
    "setattr", "hasattr", "open", "sorted", "enumerate", "zip", "type", "repr",
    "format", "append", "get", "items", "keys", "values", "join", "split",
    "strip", "lower", "upper", "replace", "startswith", "endswith", "copy",
    "update", "pop", "extend", "insert", "remove", "count", "index", "sort",
    "model_validate", "model_dump", "dataclass", "field", "property",
    "now", "utcnow", "isoformat", "monotonic", "time", "sleep", "loads",
    "dumps", "match", "search", "findall", "sub", "compile", "group",
}
# fmt: on


def py_corpus() -> str:
    """Every tracked .py file's text, concatenated."""
    files = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    parts = []
    for f in files:
        # A tracked path can be absent from the work tree; skip it rather
        # than fail the sweep.
        with contextlib.suppress(OSError):
            parts.append((ROOT / f).read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(parts)


def python_blocks(text: str) -> list[tuple[int, str]]:
    """(start_line, body) for each ```python fenced block."""
    blocks, cur, start = [], None, 0
    for i, line in enumerate(text.splitlines(), 1):
        if cur is None:
            if re.match(r"^\s*```py(thon)?\s*$", line):
                cur, start = [], i
        elif re.match(r"^\s*```\s*$", line):
            blocks.append((start, "\n".join(cur)))
            cur = None
        else:
            cur.append(line)
    return blocks


def known_symbols(corpus: str) -> set[str]:
    """Every name the codebase defines or assigns."""
    names = set(re.findall(r"^\s*(?:async\s+)?def\s+([a-z_][a-z0-9_]*)", corpus, re.M))
    names |= set(re.findall(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)", corpus, re.M))
    # attributes and constants assigned anywhere
    names |= set(re.findall(r"\bself\.(_?[A-Za-z][A-Za-z0-9_]*)\s*[:=]", corpus))
    names |= set(re.findall(r"^\s*([A-Z][A-Z0-9_]{2,})\s*[:=]", corpus, re.M))
    return names


def spec_candidates(text: str) -> list[tuple[str, int, str]]:
    """(name, line, kind) for every symbol one spec file asserts."""
    cands: list[tuple[str, int, str]] = []

    for start, body in python_blocks(text):
        for rx, kind in (
            (RE_SELF_CALL, "self-method"),
            (RE_SELF_CONST, "self-const"),
            (RE_DEF, "def"),
            (RE_CLASS, "class"),
        ):
            for m in rx.finditer(body):
                line = start + body[: m.start()].count("\n") + 1
                cands.append((m.group(1), line, kind))

    for i, line in enumerate(text.splitlines(), 1):
        for m in RE_INLINE.finditer(line):
            name = m.group(1).split(".")[-1]
            cands.append((name, i, "inline"))

    return cands


def report(findings: dict[str, list[tuple[str, int, str]]]) -> None:
    """Print each spec's undefined symbols, first mention only."""
    total = sum(len(v) for v in findings.values())
    print(f"Spec files scanned: {sum(len(list(ROOT.glob(g))) for g in SPEC_GLOBS)}")
    print(f"Symbols asserted but not defined in any tracked .py: {total}\n")
    for rel in sorted(findings):
        print(f"{rel}")
        seen = set()
        for name, line, kind in sorted(findings[rel], key=lambda x: x[1]):
            if name in seen:
                continue
            seen.add(name)
            print(f"  :{line:<6} {kind:<12} {name}")
        print()


def main() -> None:
    known = known_symbols(py_corpus())
    findings: dict[str, list[tuple[str, int, str]]] = defaultdict(list)

    for pattern in SPEC_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            rel = str(path.relative_to(ROOT))
            text = path.read_text(encoding="utf-8", errors="ignore")
            for name, line, kind in spec_candidates(text):
                if name in IGNORE or name in known:
                    continue
                findings[rel].append((name, line, kind))

    report(findings)


if __name__ == "__main__":
    main()
