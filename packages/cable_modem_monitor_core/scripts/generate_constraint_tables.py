#!/usr/bin/env python3
"""Render the transport constraint tables published in Core's specs.

The auth models, parser-format models, and action models are the
single authoritative source for the transport → auth → format →
action constraint. This script renders that source into the marked
regions of ARCHITECTURE.md and MODEM_YAML_SPEC.md so a published
table cannot drift from the code that enforces it.

Freshness is gated by ``tests/models/test_constraint_tables.py``,
which runs in the Core suite. There is no separate CI job.

Usage:
    python scripts/generate_constraint_tables.py            # rewrite in place
    python scripts/generate_constraint_tables.py --check    # exit 1 if stale
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

from solentlabs.cable_modem_monitor_core.models.modem_config.actions import (
    get_action_type_rows,
)
from solentlabs.cable_modem_monitor_core.models.modem_config.auth import (
    get_auth_strategy_rows,
    get_transport_strategy_sets,
)
from solentlabs.cable_modem_monitor_core.models.parser_config.config import (
    ALL_FORMAT_MODELS,
)
from solentlabs.cable_modem_monitor_core.models.parser_config.format_registry import (
    format_tags_for_transport,
)

DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"
ARCHITECTURE = DOCS_DIR / "ARCHITECTURE.md"
MODEM_YAML_SPEC = DOCS_DIR / "MODEM_YAML_SPEC.md"

# Prose columns, not derivable from any model. A transport missing an
# entry here fails generation rather than rendering a blank cell, so
# adding a transport cannot silently ship an incomplete table.
_TRANSPORT_PROSE: dict[str, dict[str, str]] = {
    "cbn": {
        "loader": "`CBNLoader` → `Element`",
        "session": "cookie (rotating sessionToken + stable SID)",
    },
    "hnap": {
        "loader": "`HNAPLoader` → `dict`",
        "session": "implicit (uid cookie + HNAP_AUTH header)",
    },
    "http": {
        "loader": "`HTTPResourceLoader` → `BeautifulSoup` or `dict`",
        "session": "stateless, cookie, CSRF, or url_token",
    },
}


def _code_list(values: Iterable[str]) -> str:
    """Render literals as a comma-separated, alphabetically sorted code-span list."""
    return ", ".join(f"`{v}`" for v in sorted(values))


def _transports() -> list[str]:
    """Return every transport that has at least one auth strategy, sorted."""
    return sorted(get_transport_strategy_sets())


def _prose(transport: str, column: str) -> str:
    """Return the hand-written cell for a transport, or fail loudly."""
    try:
        return _TRANSPORT_PROSE[transport][column]
    except KeyError:
        raise SystemExit(
            f"transport '{transport}' has no '{column}' entry in _TRANSPORT_PROSE "
            f"({Path(__file__).name}) — add one before regenerating"
        ) from None


def _action_cell(transport: str) -> str:
    """Return the action-type cell for a transport.

    An action type is valid for exactly the transport it is named
    after, so a transport with no matching action model is a gap in
    the models, not in this script.
    """
    for row in get_action_type_rows():
        if row.action_type == transport:
            if row.supports_action_auth:
                return f"`{row.action_type}` (optional `action_auth`)"
            return f"`{row.action_type}`"
    raise SystemExit(f"transport '{transport}' has no matching action model")


def _formats_cell(transport: str) -> str:
    """Return the valid-formats cell for a transport."""
    return _code_list(format_tags_for_transport(transport, ALL_FORMAT_MODELS))


def render_constraint_summary() -> str:
    """ARCHITECTURE.md § Constraint Summary."""
    lines = [
        "| Transport | Loader | Valid auth | Valid formats | Valid action types |",
        "|-----------|--------|------------|---------------|--------------------|",
    ]
    sets = get_transport_strategy_sets()
    for transport in _transports():
        lines.append(
            f"| `{transport}` | {_prose(transport, 'loader')} "
            f"| {_code_list(sets[transport])} "
            f"| {_formats_cell(transport)} "
            f"| {_action_cell(transport)} |"
        )
    return "\n".join(lines)


def render_auth_strategies() -> str:
    """ARCHITECTURE.md § Auth Manager strategy table."""
    lines = [
        "| Strategy | Transport | Stateless? |",
        "|----------|-----------|:----------:|",
    ]
    for row in get_auth_strategy_rows():
        stateless = "Yes" if row.stateless else "No"
        lines.append(f"| `{row.strategy}` | `{row.transport}` | {stateless} |")
    return "\n".join(lines)


def render_yaml_constraints() -> str:
    """MODEM_YAML_SPEC.md § Transport constraints."""
    lines = [
        "| Transport | Valid auth strategies | Valid session | Valid formats | Valid action types |",
        "|-----------|-----------------------|---------------|---------------|--------------------|",
    ]
    sets = get_transport_strategy_sets()
    for transport in _transports():
        lines.append(
            f"| `{transport}` | {_code_list(sets[transport])} "
            f"| {_prose(transport, 'session')} "
            f"| {_formats_cell(transport)} "
            f"| {_action_cell(transport)} |"
        )
    return "\n".join(lines)


REGIONS: dict[str, tuple[Path, Callable[[], str]]] = {
    "constraint-summary": (ARCHITECTURE, render_constraint_summary),
    "auth-strategies": (ARCHITECTURE, render_auth_strategies),
    "yaml-constraints": (MODEM_YAML_SPEC, render_yaml_constraints),
}


def _region_pattern(region: str) -> re.Pattern[str]:
    """Match a BEGIN/END marker pair and everything between it."""
    return re.compile(
        rf"(<!-- BEGIN GENERATED: {re.escape(region)}[^>]*-->\n).*?(\n<!-- END GENERATED: {re.escape(region)} -->)",
        re.DOTALL,
    )


def replace_region(text: str, region: str, body: str) -> str:
    """Return ``text`` with the marked region's contents replaced by ``body``."""
    pattern = _region_pattern(region)
    if not pattern.search(text):
        raise SystemExit(f"no '{region}' generated region found — markers missing or malformed")
    # Lambda replacement, not a template string: the rendered body is
    # literal text and must not be scanned for backreference escapes.
    return pattern.sub(lambda m: m.group(1) + body + m.group(2), text)


def render_all() -> dict[Path, str]:
    """Return ``{doc_path: full expected text}`` for every doc with regions."""
    updated: dict[Path, str] = {}
    for region, (path, renderer) in REGIONS.items():
        text = updated.get(path, path.read_text(encoding="utf-8"))
        updated[path] = replace_region(text, region, renderer())
    return updated


def main() -> int:
    """Rewrite the generated regions, or report staleness under --check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if any doc is stale")
    args = parser.parse_args()

    stale: list[Path] = []
    for path, expected in render_all().items():
        if path.read_text(encoding="utf-8") == expected:
            continue
        if args.check:
            stale.append(path)
        else:
            path.write_text(expected, encoding="utf-8")
            print(f"updated {path.relative_to(DOCS_DIR.parent)}")

    if stale:
        for path in stale:
            print(f"stale constraint table: {path}", file=sys.stderr)
        print(
            "run: python packages/cable_modem_monitor_core/scripts/generate_constraint_tables.py",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
