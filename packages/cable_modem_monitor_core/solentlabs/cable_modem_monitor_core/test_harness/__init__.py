"""Test harness — HAR replay server, golden file comparison, and pipeline runner.

Two use cases share the same ``HARMockServer`` component:

1. **Automated catalog regression testing** — ``run_modem_test()`` /
   ``run_modem_test_orchestrated()`` start an ephemeral server per test,
   run the pipeline, and compare output against golden files.

2. **Manual integration testing** — ``python -m solentlabs.cable_modem_monitor_core.test_harness``
   starts a persistent server for testing against a real HA instance.

See ARCHITECTURE.md § Test Harness for the replay and pass-criterion
rules; catalog_tools' ONBOARDING_SPEC.md § Testing for how intake
drives it.

Public API for Catalog's pytest integration::

    from solentlabs.cable_modem_monitor_core.test_harness import (
        discover_modem_tests,
        run_modem_test,
        run_modem_test_orchestrated,
    )
"""

from __future__ import annotations

from .discovery import (
    ModemTestCase,
    RestartTestCase,
    discover_modem_tests,
    discover_restart_tests,
    resolve_modem_config,
)
from .golden_file import ComparisonResult, compare_golden_file
from .loader import ServerConfig, load_server_from_modem_dir
from .runner import ActionTestResult, TestResult, run_modem_restart_test, run_modem_test, run_modem_test_orchestrated
from .server import HARMockServer

__all__ = [
    "ActionTestResult",
    "ComparisonResult",
    "HARMockServer",
    "ModemTestCase",
    "RestartTestCase",
    "ServerConfig",
    "TestResult",
    "compare_golden_file",
    "discover_modem_tests",
    "discover_restart_tests",
    "load_server_from_modem_dir",
    "resolve_modem_config",
    "run_modem_restart_test",
    "run_modem_test",
    "run_modem_test_orchestrated",
]
