"""Catalog modem tests — schema validation, HAR replay, spec conformance.

Auto-parametrized by conftest.py:
- ``modem_yaml_path``: every modem*.yaml validates through Pydantic
- ``modem_test_case``: HAR + golden file pairs run full orchestrator cycle
  AND validate the committed golden against PARSING_SPEC contracts

No modem-specific test code here. Adding a modem = adding files.

The spec-conformance gate enforces PARSING_SPEC field contracts on every
modem regardless of ``status:``. A new entry must be conformant on the
commit that adds it.
"""

from __future__ import annotations

import json
from pathlib import Path

from solentlabs.cable_modem_monitor_core.config_loader import load_modem_config
from solentlabs.cable_modem_monitor_core.spec_conformance import validate_modem_data
from solentlabs.cable_modem_monitor_core.test_harness import (
    ModemTestCase,
    RestartTestCase,
    run_modem_restart_test,
    run_modem_test_orchestrated,
)


def test_modem_yaml_schema(modem_yaml_path: Path) -> None:
    """Every modem.yaml in the catalog passes Pydantic schema validation."""
    load_modem_config(modem_yaml_path)


def test_modem_har_replay(modem_test_case: ModemTestCase) -> None:
    """Each modem's HAR replay produces expected output via orchestrator."""
    result = run_modem_test_orchestrated(modem_test_case)
    assert result.passed, (
        f"{result.test_name}: {result.error}" if result.error else f"{result.test_name}: golden file mismatch"
    )


def test_modem_golden_spec_conformance(modem_test_case: ModemTestCase) -> None:
    """Every modem's golden conforms to PARSING_SPEC field contracts.

    Runs regardless of ``status:``. The gate used to skip anything not
    yet ``confirmed``, which let drift accumulate unseen in onboarding
    entries and then surface as a surprise failure at promotion time,
    mid-confirmation with a contributor waiting (see #111). Catching it
    on the commit that introduces it is the whole point.
    """
    if not modem_test_case.golden_path.is_file():
        return

    data = json.loads(modem_test_case.golden_path.read_text(encoding="utf-8"))
    violations = validate_modem_data(data, modem=modem_test_case.name)

    if violations:
        details = "\n".join(f"  - {v.path} ({v.rule}): {v.value!r} — {v.message}" for v in violations)
        msg = (
            f"{modem_test_case.name}: {len(violations)} spec-conformance "
            f"violation(s):\n{details}\n\n"
            f"Fix the parser and regenerate the golden. Downgrading "
            f"modem.yaml status no longer exempts an entry."
        )
        raise AssertionError(msg)


def test_modem_restart_action(restart_test_case: RestartTestCase) -> None:
    """Each modem with a restart HAR fixture exercises the action pipeline against a mock server.

    Adding restart action coverage for a modem = declaring actions.restart
    with the restart click captured in test_data/modem.har (one HAR per
    variant). No test code changes needed.
    """
    result = run_modem_restart_test(restart_test_case)
    assert result.passed, f"{result.test_name}: {result.error}"
