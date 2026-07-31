"""Pipeline runner — execute a modem test case end-to-end.

Two entry points:

``run_modem_test``
    Extraction-only path — manually wires auth -> load -> parse ->
    filter against a HAR mock server. Core unit tests use this to
    validate the extraction pipeline in isolation.

``run_modem_test_orchestrated``
    Full orchestration path — creates a ``ModemDataCollector`` and
    ``Orchestrator``, calls ``get_modem_data()``, and compares the
    snapshot's modem_data against the golden file. Catalog regression
    tests use this to exercise session lifecycle, logout, signal
    classification, and status derivation alongside extraction.

Both share the same config loading, golden file comparison, and
``TestResult`` format.

See catalog_tools' ONBOARDING_SPEC.md § Test Execution Flow.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import requests

from ..auth.base import AuthResult
from ..auth.factory import create_auth_manager
from ..config_loader import load_modem_config, load_parser_config
from ..fetch_list import collect_fetch_targets
from ..har import load_har_json
from ..loaders.hnap import HNAPLoader
from ..loaders.http import HTTPResourceLoader
from ..orchestration.factory import create_orchestrator
from ..orchestration.signals import ConnectionStatus
from ..parsers.coordinator import ModemParserCoordinator
from ..post_processor import load_post_processor
from .discovery import ModemTestCase, RestartTestCase
from .golden_file import ComparisonResult, compare_golden_file
from .server import HARMockServer

_logger = logging.getLogger(__name__)


@dataclass
class TestResult:
    """Result of running a single modem test case.

    Attributes:
        test_name: Human-readable test ID from ``ModemTestCase.name``.
        passed: ``True`` only if pipeline succeeded AND golden file
            matches.
        error: Pipeline error message if the test could not run
            (auth failure, resource 404, import error). Empty string
            when the pipeline completed.
        comparison: Golden file comparison result. ``None`` if the
            pipeline errored before reaching comparison.
    """

    test_name: str
    passed: bool
    error: str = ""
    comparison: ComparisonResult | None = None


@dataclass
class ActionTestResult:
    """Result of a restart action test case.

    Attributes:
        test_name: Human-readable test ID from ``RestartTestCase.name``.
        passed: ``True`` only if the action pipeline completed successfully.
        error: Error message if the action failed. Empty string on pass.
    """

    test_name: str
    passed: bool
    error: str = ""


def run_modem_restart_test(test_case: RestartTestCase) -> ActionTestResult:
    """Run the restart action pipeline for a single test case against a HAR mock server.

    Loads the modem config and restart HAR, starts a ``HARMockServer``,
    creates a collector, and dispatches the restart action via
    ``execute_action``. Passes if ``ActionResult.success`` is True.

    Args:
        test_case: Discovered restart test case with all file paths resolved.

    Returns:
        ``ActionTestResult`` with pass/fail and error detail.
        Never raises — all pipeline errors are captured in the result.
    """
    try:
        modem_config = load_modem_config(test_case.modem_config_path)
    except Exception as e:
        return ActionTestResult(test_name=test_case.name, passed=False, error=f"Config load failed: {e}")

    if modem_config.actions is None or modem_config.actions.restart is None:
        return ActionTestResult(
            test_name=test_case.name,
            passed=False,
            error="No actions.restart in modem config",
        )

    try:
        har_data = load_har_json(test_case.har_path)
        entries = har_data["log"]["entries"]
    except Exception as e:
        return ActionTestResult(test_name=test_case.name, passed=False, error=f"Failed to load HAR: {e}")

    try:
        from ..orchestration.actions import execute_action
        from ..orchestration.collector import ModemDataCollector

        with HARMockServer(entries, modem_config=modem_config) as server:
            collector = ModemDataCollector(
                modem_config=modem_config,
                parser_config=None,
                post_processor=None,
                base_url=server.base_url,
                username="admin",
                password="pw",
            )
            auth_result = collector.authenticate()
            if not auth_result.success:
                return ActionTestResult(
                    test_name=test_case.name,
                    passed=False,
                    error=f"Auth failed: {auth_result.error}",
                )
            result = execute_action(collector, modem_config, modem_config.actions.restart)
    except Exception as e:
        return ActionTestResult(test_name=test_case.name, passed=False, error=f"Action execution error: {e}")

    if not result.success:
        return ActionTestResult(
            test_name=test_case.name, passed=False, error=result.message or "Action returned failure"
        )

    # The HTTP executor reports success for any response it received, so
    # ``success`` alone cannot tell a restart the modem accepted from one
    # it 404'd. The status code it already records can. HNAP validates its
    # own SOAP result and records no status, so this skips it.
    status = result.details.get("status_code")
    if status is not None and not 200 <= int(status) < 400:
        return ActionTestResult(
            test_name=test_case.name,
            passed=False,
            error=f"Restart request answered {status} — the modem never recorded this exchange",
        )

    return ActionTestResult(test_name=test_case.name, passed=True)


def run_modem_test(test_case: ModemTestCase) -> TestResult:
    """Run the extraction pipeline for a single test case.

    Manually wires auth -> load -> parse -> filter against a HAR
    mock server. Core unit tests use this to validate extraction
    in isolation.

    Args:
        test_case: Discovered test case with all file paths resolved.

    Returns:
        ``TestResult`` with pass/fail, error detail, or golden file diff.
        Never raises — all pipeline errors are captured in the result.
    """
    loaded = _load_test_case(test_case)
    if isinstance(loaded, TestResult):
        return loaded

    entries, expected, modem_config, parser_config, post_processor = loaded

    try:
        actual = _run_pipeline(
            entries=entries,
            modem_config=modem_config,
            parser_config=parser_config,
            post_processor=post_processor,
        )
    except Exception as e:
        return TestResult(
            test_name=test_case.name,
            passed=False,
            error=f"Pipeline error: {e}",
        )

    return _compare_and_record(test_case, actual, expected)


def run_modem_test_orchestrated(test_case: ModemTestCase) -> TestResult:
    """Run the full orchestrator cycle for a single test case.

    Creates a ``ModemDataCollector`` and ``Orchestrator``, calls
    ``get_modem_data()``, and compares the snapshot's modem_data
    against the golden file. Exercises session lifecycle, logout,
    signal classification, and status derivation.

    Args:
        test_case: Discovered test case with all file paths resolved.

    Returns:
        ``TestResult`` with pass/fail, error detail, or golden file diff.
        Never raises — all pipeline errors are captured in the result.
    """
    loaded = _load_test_case(test_case)
    if isinstance(loaded, TestResult):
        return loaded

    entries, expected, modem_config, parser_config, post_processor = loaded

    try:
        actual = _run_orchestrated(
            entries=entries,
            modem_config=modem_config,
            parser_config=parser_config,
            post_processor=post_processor,
        )
    except Exception as e:
        return TestResult(
            test_name=test_case.name,
            passed=False,
            error=f"Orchestrator error: {e}",
        )

    return _compare_and_record(test_case, actual, expected)


def _compare_and_record(
    test_case: ModemTestCase,
    actual: dict[str, Any],
    expected: dict[str, Any],
) -> TestResult:
    """Write actual output, compare against golden file, clean up on pass."""
    actual_path = test_case.har_path.with_suffix(".actual.json")
    actual_path.write_text(
        json.dumps(actual, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    comparison = compare_golden_file(actual, expected)

    # Only keep actual file when there's drift
    if comparison.passed and actual_path.exists():
        actual_path.unlink()

    return TestResult(
        test_name=test_case.name,
        passed=comparison.passed,
        comparison=comparison,
    )


def _load_test_case(
    test_case: ModemTestCase,
) -> TestResult | tuple[list[dict[str, Any]], dict[str, Any], Any, Any, Any]:
    """Load and validate all inputs for a modem test case.

    Returns either a ``TestResult`` on load error or a tuple of
    (entries, expected, modem_config, parser_config, post_processor)
    on success. Shared by both ``run_modem_test`` and
    ``run_modem_test_orchestrated``.
    """
    name = test_case.name

    # Check golden file existence
    if not test_case.golden_path.is_file():
        return TestResult(
            test_name=name,
            passed=False,
            error=f"Golden file not found: {test_case.golden_path}",
        )

    # Load HAR
    try:
        har_data = load_har_json(test_case.har_path)
        entries = har_data["log"]["entries"]
    except Exception as e:
        return TestResult(
            test_name=name,
            passed=False,
            error=f"Failed to load HAR: {e}",
        )

    # Load golden file
    try:
        expected = json.loads(
            test_case.golden_path.read_text(encoding="utf-8"),
        )
    except Exception as e:
        return TestResult(
            test_name=name,
            passed=False,
            error=f"Failed to load golden file: {e}",
        )

    # Load modem config
    try:
        modem_config = load_modem_config(test_case.modem_config_path)
    except Exception as e:
        return TestResult(
            test_name=name,
            passed=False,
            error=f"Failed to load modem config: {e}",
        )

    # Load parser config (optional — parser.py-only is valid)
    parser_config = None
    if test_case.parser_config_path is not None:
        try:
            parser_config = load_parser_config(test_case.parser_config_path)
        except Exception as e:
            return TestResult(
                test_name=name,
                passed=False,
                error=f"Failed to load parser config: {e}",
            )

    # Load post-processor (optional)
    post_processor = None
    if test_case.parser_py_path is not None:
        try:
            post_processor = load_post_processor(test_case.parser_py_path)
        except Exception as e:
            return TestResult(
                test_name=name,
                passed=False,
                error=f"Failed to load parser.py: {e}",
            )

    return entries, expected, modem_config, parser_config, post_processor


def _detect_form_nonce_encoding(
    modem_config: Any,
    base_url: str,
) -> None:
    """Pre-fetch the login page from the mock server and set encoding.

    Mirrors the config flow detection path: GETs the login page,
    analyzes the form structure, and sets ``credential_encoding``
    and ``credential_field`` on the config.  This exercises the
    mock server's ``FormNonceAuthHandler`` GET route — the same
    pattern the HA config flow uses against a real modem.

    No-op for non-form_nonce auth strategies.
    """
    from ..auth.form_nonce import _analyze_login_form
    from ..models.modem_config.auth import FormNonceAuth

    if not isinstance(modem_config.auth, FormNonceAuth):
        return

    auth = modem_config.auth
    login_url = f"{base_url}{auth.action}"

    response = requests.get(login_url, timeout=5)
    if not response.text:
        return

    detection = _analyze_login_form(
        response.text,
        auth.username_field,
        auth.nonce_field,
    )
    auth.credential_encoding = detection.encoding
    auth.credential_field = detection.credential_field


def _run_pipeline(
    *,
    entries: list[dict[str, Any]],
    modem_config: Any,
    parser_config: Any,
    post_processor: Any,
) -> dict[str, Any]:
    """Run the full extraction pipeline against a mock server.

    Args:
        entries: HAR log entries for the mock server.
        modem_config: Validated ``ModemConfig`` instance.
        parser_config: Validated ``ParserConfig`` instance (or ``None``).
        post_processor: ``PostProcessor`` instance (or ``None``).

    Returns:
        Extracted ``ModemData`` dict.

    Raises:
        Exception: On any pipeline failure (auth, fetch, parse).
    """
    with HARMockServer(entries, modem_config=modem_config) as server:
        base_url = server.base_url

        # Detect encoding via mock server GET (mirrors config flow)
        _detect_form_nonce_encoding(modem_config, base_url)

        session = requests.Session()

        # Configure session
        auth_manager = create_auth_manager(modem_config)
        session_headers: dict[str, str] = {}
        if modem_config.session and modem_config.session.headers:
            session_headers = modem_config.session.resolved_headers(base_url=base_url)
        auth_manager.configure_session(
            session,
            session_headers,
        )

        # Authenticate
        auth_result: AuthResult = auth_manager.authenticate(
            session,
            base_url,
            username="admin",
            password="pw",
            timeout=modem_config.timeout,
        )
        if not auth_result.success:
            raise RuntimeError(f"Auth failed: {auth_result.error}")

        # Fetch resources
        if parser_config is None:
            raise RuntimeError(
                "Modem requires custom parser.py — " "parser.yaml alone insufficient for resource loading",
            )

        if modem_config.transport == "hnap":
            # HNAP: batched SOAP request, no per-page fetching
            hmac_algorithm = "md5"
            if hasattr(modem_config.auth, "hmac_algorithm"):
                hmac_algorithm = modem_config.auth.hmac_algorithm
            hnap_loader = HNAPLoader(
                session=session,
                base_url=base_url,
                private_key=auth_result.auth_context.private_key,
                hmac_algorithm=hmac_algorithm,
                timeout=modem_config.timeout,
            )
            resources = hnap_loader.fetch(parser_config)
        else:
            # HTTP: per-page fetching
            targets = collect_fetch_targets(parser_config, post_processor)
            # Prefer body-derived token from auth_context; fall back to cookie
            url_token = ""
            token_prefix = getattr(modem_config.auth, "token_prefix", "")
            if token_prefix:
                if auth_result.auth_context.url_token:
                    url_token = auth_result.auth_context.url_token
                else:
                    cookie_name = getattr(modem_config.auth, "cookie_name", "")
                    if cookie_name:
                        url_token = session.cookies.get(cookie_name, "") or ""

            loader = HTTPResourceLoader(
                session=session,
                base_url=base_url,
                timeout=modem_config.timeout,
                url_token=url_token,
                token_prefix=token_prefix,
            )
            resources = loader.fetch(targets, auth_result)

        # Parse — discard diagnostics, this harness only surfaces the
        # extracted data; downstream callers (CLI, golden-file generator)
        # consume the dict alone. Stub-page detection lives in the
        # production collector path, not in the test harness.
        coordinator = ModemParserCoordinator(parser_config, post_processor)
        data, _ = coordinator.parse(resources)
        return data


def _run_orchestrated(
    *,
    entries: list[dict[str, Any]],
    modem_config: Any,
    parser_config: Any,
    post_processor: Any,
) -> dict[str, Any]:
    """Run a full orchestrator cycle against a mock server.

    Creates a ``ModemDataCollector`` and ``Orchestrator``, calls
    ``get_modem_data()``, validates the snapshot status, and returns
    the extracted modem_data dict for golden file comparison.

    Args:
        entries: HAR log entries for the mock server.
        modem_config: Validated ``ModemConfig`` instance.
        parser_config: Validated ``ParserConfig`` instance (or ``None``).
        post_processor: ``PostProcessor`` instance (or ``None``).

    Returns:
        Extracted ``ModemData`` dict.

    Raises:
        RuntimeError: If collection failed or status is not ONLINE.
    """
    with HARMockServer(entries, modem_config=modem_config) as server:
        # Detect encoding via mock server GET (mirrors config flow)
        _detect_form_nonce_encoding(modem_config, server.base_url)

        orchestrator, _, _ = create_orchestrator(
            modem_config=modem_config,
            parser_config=parser_config,
            post_processor=post_processor,
            base_url=server.base_url,
            username="admin",
            password="pw",
            supports_icmp=False,
            http_probe=False,
        )
        snapshot = orchestrator.get_modem_data()

        if snapshot.modem_data is None:
            raise RuntimeError(f"Collection failed: {snapshot.connection_status.value}" f" — {snapshot.error}")

        if snapshot.connection_status != ConnectionStatus.ONLINE:
            raise RuntimeError(f"Expected ONLINE, got {snapshot.connection_status.value}")

        _assert_logout_succeeded(modem_config, server)

        return snapshot.modem_data


def _assert_logout_succeeded(modem_config: Any, server: HARMockServer) -> None:
    """Fail the replay if a declared logout did not come back successfully.

    A poll ends by firing ``actions.logout`` and Core deliberately
    ignores the outcome — logout is best-effort at both call sites per
    ORCHESTRATION_SPEC. That makes the harness the only place a logout
    which never worked can be noticed, so the check lives here rather
    than in the collector.
    """
    if modem_config.actions is None or modem_config.actions.logout is None:
        return

    status = server.auth_handler.served_actions.get("logout")
    if status is None:
        # Two ways to get here, both real failures: the request Core sent
        # did not match the declared endpoint, or the harness refused it
        # before dispatch (an unresolved placeholder answers 500). The
        # harness log carries which.
        raise RuntimeError(
            "Declared logout was never dispatched — the request Core sent did not match "
            "the declared endpoint, or the harness refused it (see the harness log)"
        )
    if not 200 <= status < 400:
        raise RuntimeError(f"Logout answered {status} — the modem never recorded this exchange")
