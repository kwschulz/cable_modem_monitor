"""The auth/action models are the single source for the constraint tables.

Two guarantees:

- Every auth model self-describes completely (`display_name`,
  `transport`, `stateless`) and the registry matches the discriminated
  union, so no strategy can exist outside the derivation.
- The tables published in ARCHITECTURE.md and MODEM_YAML_SPEC.md equal
  what the models render. This is the freshness gate for
  ``scripts/generate_constraint_tables.py``; it is why omitting a
  strategy from a spec table (as `bearer` was) now fails the suite
  instead of surviving until someone reads both files.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any, get_args

import pytest
from solentlabs.cable_modem_monitor_core.models.modem_config.actions import (
    ActionConfig,
    get_action_type_rows,
)
from solentlabs.cable_modem_monitor_core.models.modem_config.auth import (
    CBN_AUTH_STRATEGIES,
    HNAP_AUTH_STRATEGIES,
    HTTP_AUTH_STRATEGIES,
    AuthConfig,
    get_auth_strategy_rows,
    get_strategy_display_labels,
    get_transport_strategy_sets,
)
from solentlabs.cable_modem_monitor_core.models.parser_config.config import (
    ALL_FORMAT_MODELS,
)
from solentlabs.cable_modem_monitor_core.models.parser_config.format_registry import (
    format_tags_for_transport,
)

# The generator lives in the package's scripts/ directory, not as an
# installed module, so load it by file path. Same pattern as the
# catalog index generator's tests.
_GENERATOR = Path(__file__).resolve().parents[2] / "scripts" / "generate_constraint_tables.py"
_spec = importlib.util.spec_from_file_location("generate_constraint_tables", _GENERATOR)
assert _spec is not None and _spec.loader is not None
_generator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_generator)


def _union_tags(annotated: Any) -> set[str]:
    """Return the discriminator tags of an Annotated discriminated union."""
    union = get_args(annotated)[0]
    return {get_args(member)[1].tag for member in get_args(union)}


# ---------------------------------------------------------------------------
# The models describe themselves completely
# ---------------------------------------------------------------------------

AUTH_ROWS = get_auth_strategy_rows()


@pytest.mark.parametrize("row", AUTH_ROWS, ids=[r.strategy for r in AUTH_ROWS])
def test_auth_model_self_describes(row: Any) -> None:
    """Every strategy declares a display name, a transport, and statelessness."""
    assert row.display_name
    assert row.transport
    assert isinstance(row.stateless, bool)


def test_auth_registry_matches_union() -> None:
    """A strategy outside ``_AUTH_MODELS`` would be invisible to every derivation."""
    assert {row.strategy for row in AUTH_ROWS} == _union_tags(AuthConfig)


def test_action_registry_matches_union() -> None:
    """Same guarantee for action types, which supply the action column."""
    assert {row.action_type for row in get_action_type_rows()} == _union_tags(ActionConfig)


def test_only_none_and_basic_are_stateless() -> None:
    """Statelessness gates login-page detection — pin the two strategies that have it.

    `none` sends no credential and `basic` re-sends it on every request,
    so neither holds a session that can expire mid-poll. Any other
    strategy claiming statelessness would silently disable detection.
    """
    assert {row.strategy for row in AUTH_ROWS if row.stateless} == {"basic", "none"}


def test_transport_sets_partition_the_strategies() -> None:
    """Every strategy lands in exactly one transport's validation set."""
    sets = get_transport_strategy_sets()
    assert sets == {
        "cbn": CBN_AUTH_STRATEGIES,
        "hnap": HNAP_AUTH_STRATEGIES,
        "http": HTTP_AUTH_STRATEGIES,
    }
    counted = sum(len(strategies) for strategies in sets.values())
    assert counted == len(AUTH_ROWS)


def test_display_labels_cover_every_strategy() -> None:
    """Config flow's variant dropdown derives from this — a gap renders a blank label."""
    assert get_strategy_display_labels() == {row.strategy: row.display_name for row in AUTH_ROWS}


# ---------------------------------------------------------------------------
# The specs publish what the models render
# ---------------------------------------------------------------------------

REGION_IDS = sorted(_generator.REGIONS)


@pytest.mark.parametrize("region", REGION_IDS)
def test_generated_region_is_present(region: str) -> None:
    """Each doc still carries its BEGIN/END markers."""
    path, _ = _generator.REGIONS[region]
    assert _generator._region_pattern(region).search(path.read_text(encoding="utf-8"))


def test_published_tables_match_the_models() -> None:
    """Regenerate with scripts/generate_constraint_tables.py when this fails."""
    for path, expected in _generator.render_all().items():
        assert path.read_text(encoding="utf-8") == expected, f"{path.name} constraint table is stale"


# The three-axis Mermaid diagram enumerates the same values by hand; a
# generated region inside a diagram node would be unreadable, so gate the
# contents instead. This is the one copy of the constraint with no
# generator behind it.
DIAGRAM_NODES = [
    ("HTA", lambda: HTTP_AUTH_STRATEGIES),
    ("HTF", lambda: format_tags_for_transport("http", ALL_FORMAT_MODELS)),
]


@pytest.mark.parametrize("node,expected", DIAGRAM_NODES, ids=[n for n, _ in DIAGRAM_NODES])
def test_architecture_diagram_lists_every_value(node: str, expected: Any) -> None:
    """The HTTP axis diagram must name every strategy and format it allows."""
    text = _generator.ARCHITECTURE.read_text(encoding="utf-8")
    match = re.search(rf'{node}\["(.*?)"\]', text)
    assert match, f"no {node} node in the ARCHITECTURE diagram"
    missing = [v for v in sorted(expected()) if not re.search(rf"\b{re.escape(v)}\b", match.group(1))]
    assert not missing, f"{node} node omits {missing}"
