"""Catalog test configuration — auto-discover modem test cases.

Uses Core's ``discover_modem_tests`` to walk the catalog's modem
directory tree and parametrize tests from HAR + golden file pairs.
Also discovers all modem.yaml files for schema validation.
Enables socket access for HAR mock server replay.

Adding a modem = adding files to its directory. No test code changes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from solentlabs.cable_modem_monitor_core.test_harness import discover_modem_tests, discover_restart_tests

# Catalog modems root: solentlabs/cable_modem_monitor_catalog/modems/
CATALOG_MODEMS_PATH = Path(__file__).parent.parent / "solentlabs" / "cable_modem_monitor_catalog" / "modems"

# Replay cases red until FormAuth grows action_source (#189): these
# captures' login POSTs carry a dynamic ?id= the fleet code does not
# yet send, and the harness now refuses the bare-path substitute.
# strict=True makes each marker fail the moment the fix works, forcing
# its removal — the xfail set cannot silently outlive the bug.
_KNOWN_DYNAMIC_ACTION_CASES = {
    "netgear/cm2050v/modem",
    "netgear/cm2050v/modem-https",
    "netgear/cm3000/modem",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark known dynamic-action replay cases xfail — the replay test only.

    The marker cannot ride on the ``modem_test_case`` param: other tests
    share that fixture and pass, and a strict marker turns their pass
    into an XPASS failure.
    """
    for item in items:
        if getattr(item, "originalname", "") != "test_modem_har_replay":
            continue
        # callspec exists on parametrized Function items only; pytest.Item
        # does not declare it, so read it the same guarded way as originalname.
        callspec = getattr(item, "callspec", None)
        if callspec is not None and callspec.id in _KNOWN_DYNAMIC_ACTION_CASES:
            item.add_marker(
                pytest.mark.xfail(
                    reason="#189: login POST needs the form action's dynamic ?id=",
                    strict=True,
                )
            )


@pytest.fixture(autouse=True)
def _allow_sockets(socket_enabled: None) -> None:  # noqa: ARG001
    """Enable sockets for HAR mock server replay.

    The ``socket_enabled`` fixture is provided by pytest-socket
    and re-enables socket operations for the test.
    """


def _discover_modem_yamls() -> list[Path]:
    """Find all modem*.yaml files in the catalog."""
    return sorted(CATALOG_MODEMS_PATH.rglob("modem*.yaml"))


def _discover_config_pairs() -> list[tuple[Path, Path]]:
    """Pair every modem*.yaml with the parser.yaml in its directory.

    Variants share one parser.yaml, so each variant yields its own pair.
    Entries with no parser.yaml (``status: unsupported``) have nothing
    cross-file to check and are omitted.
    """
    pairs = [(p, p.parent / "parser.yaml") for p in _discover_modem_yamls()]
    return [(modem, parser) for modem, parser in pairs if parser.is_file()]


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrize tests from modem directory discovery."""
    if "modem_test_case" in metafunc.fixturenames:
        cases = discover_modem_tests(CATALOG_MODEMS_PATH)
        metafunc.parametrize("modem_test_case", cases, ids=lambda c: c.name)
    if "modem_yaml_path" in metafunc.fixturenames:
        paths = _discover_modem_yamls()
        metafunc.parametrize("modem_yaml_path", paths, ids=lambda p: str(p.relative_to(CATALOG_MODEMS_PATH)))
    if "config_pair" in metafunc.fixturenames:
        pairs = _discover_config_pairs()
        metafunc.parametrize("config_pair", pairs, ids=lambda p: str(p[0].relative_to(CATALOG_MODEMS_PATH)))
    if "restart_test_case" in metafunc.fixturenames:
        cases = discover_restart_tests(CATALOG_MODEMS_PATH)
        metafunc.parametrize("restart_test_case", cases, ids=lambda c: c.name)
