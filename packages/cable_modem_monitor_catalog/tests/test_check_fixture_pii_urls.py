"""Tests for URL-surface PII scanning in ``check_fixture_pii``.

A HAR request URL carries no field name, so the strict sanitization
rules have nothing to anchor on: a credential that appears *only* in a
path segment or query string survives sanitization entirely. This is the
gap har-capture 0.11.1's propagation sweep cannot close — it replaces
verbatim copies of values already redacted elsewhere, and a
URL-only secret has no copy to propagate from. Detection here is
therefore the catalog gate's job, not the sanitizer's.

Every guard case (a value that must NOT be flagged) is paired with a
live counterpart asserting the rule fires on the same surface. Without
that pairing a guard passes when the detector is simply broken.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "check_fixture_pii.py"


def _load_module() -> Any:
    """Import the pre-commit hook script by path.

    ``scripts/`` is not an importable package, so the hook is loaded
    directly rather than through the package namespace.

    Deliberately not ``tests.fixture_helpers.load_script``, which the
    root-level ``tests/lib/`` suites share: this package is its own
    pytest rootdir, so ``tests`` resolves to *this* directory and the
    root helper is unreachable from here. Importing it would also
    shadow Core's ``tests`` package, which is the collision
    fixture_helpers' own docstring documents.
    """
    spec = importlib.util.spec_from_file_location("check_fixture_pii", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_fixture_pii"] = module
    spec.loader.exec_module(module)
    return module


pii = _load_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _har_with_url(url: str, query_string: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Minimal single-entry HAR carrying one request URL."""
    return {
        "log": {
            "version": "1.2",
            "creator": {"name": "test"},
            "entries": [
                {
                    "request": {
                        "method": "GET",
                        "url": url,
                        "queryString": query_string or [],
                        "headers": [],
                        "cookies": [],
                    },
                    "response": {"status": 200, "headers": [], "cookies": [], "content": {"text": ""}},
                }
            ],
        }
    }


# ---------------------------------------------------------------------------
# Detection — values that MUST be flagged
# ---------------------------------------------------------------------------

# Fake credentials throughout. The base64 blobs decode to admin:<fake>.
LEAKY_URLS = [
    pytest.param(
        "https://192.168.100.1/cmconnectionstatus.html?YWRtaW46RmFrZVBhc3My",
        "base64 credential as a bare query key (SB8200 wire shape)",
        id="base64-credential-bare-query-key",
    ),
    pytest.param(
        "https://192.168.100.1/login.html?credential=YWRtaW46RmFrZVBhc3My",
        "base64 credential as a query value",
        id="base64-credential-query-value",
    ),
    pytest.param(
        "https://192.168.100.1/login_YWRtaW46RmFrZVBhc3My",
        "base64 credential embedded in a path segment with a prefix",
        id="base64-credential-path-prefix",
    ),
    pytest.param(
        "https://192.168.100.1/rest/v1/user/3/token/a3f9c2e18b7d4056af12ce9930bb77e1",
        "opaque session token in a path segment (F3896LG-ZG shape)",
        id="opaque-token-path-segment",
    ),
]


@pytest.mark.parametrize(("url", "why"), LEAKY_URLS)
def test_url_secret_is_flagged(url: str, why: str, tmp_path: Path) -> None:
    """A secret reachable only through the URL surface must be reported."""
    har = tmp_path / "leak.har"
    har.write_text(json.dumps(_har_with_url(url)), encoding="utf-8")

    issues = pii.check_har_file(har)

    assert issues, f"expected a finding — {why}: {url}"


def test_credential_in_query_string_name_is_flagged(tmp_path: Path) -> None:
    """A bare query key lands in ``queryString[].name``, not ``.value``.

    The leaked SB8200 credential sat in exactly this position, so a
    scanner that reads only query *values* still misses it.
    """
    har = tmp_path / "leak.har"
    har.write_text(
        json.dumps(
            _har_with_url(
                "https://192.168.100.1/cmconnectionstatus.html?YWRtaW46RmFrZVBhc3My",
                query_string=[{"name": "YWRtaW46RmFrZVBhc3My", "value": ""}],
            )
        ),
        encoding="utf-8",
    )

    issues = pii.check_har_file(har)

    assert issues, "credential in queryString[].name must be reported"


# ---------------------------------------------------------------------------
# Guards — values that must NOT be flagged, each with a live counterpart
# ---------------------------------------------------------------------------

SAFE_URLS = [
    pytest.param("https://192.168.100.1/jquery-2.0.3.min.js", id="asset-js"),
    pytest.param("https://192.168.100.1/jquery-ui-1.8.21.custom.min.js", id="asset-js-versioned"),
    pytest.param("https://192.168.100.1/fonts/TeleNeo-Regular.woff2", id="asset-font"),
    pytest.param("https://192.168.100.1/img/329136_2024-09-30_master_Home_M.jpg", id="asset-image"),
    pytest.param("https://192.168.100.1/customerID.txt?_=1772950969655", id="cache-buster"),
    # The sanctioned format-preserving placeholder: base64 of admin:sanitized.
    pytest.param("https://192.168.100.1/cmconnectionstatus.html?YWRtaW46c2FuaXRpemVk", id="sanitized-placeholder"),
    pytest.param("https://192.168.100.1/login_YWRtaW46c2FuaXRpemVk", id="sanitized-placeholder-prefixed"),
    pytest.param("https://192.168.100.1/rest/v1/user/3/token/[REDACTED]", id="redacted-placeholder"),
]


@pytest.mark.parametrize("url", SAFE_URLS)
def test_safe_url_is_not_flagged(url: str, tmp_path: Path) -> None:
    """Assets, cache busters and redaction placeholders are not findings."""
    har = tmp_path / "clean.har"
    har.write_text(json.dumps(_har_with_url(url)), encoding="utf-8")

    issues = pii.check_har_file(har)

    assert not issues, f"false positive on {url}: {issues}"


def test_asset_extension_guard_does_not_swallow_a_real_secret() -> None:
    """Live counterpart to the asset guard.

    The guard skips names ending in an asset extension. If it were
    implemented as a substring test, a token merely *containing* such a
    string would be skipped too — so assert the detector still fires.
    """
    assert pii.find_url_secrets("https://192.168.100.1/a3f9c2e18b7d4056af12ce9930bb77e1.js.token")


def test_placeholder_guard_does_not_swallow_a_real_credential() -> None:
    """Live counterpart to the placeholder guard.

    ``admin:sanitized`` is allowlisted by its decoded password half, not
    by the base64 blob, so a different credential for the same user must
    still be reported.
    """
    assert not pii.find_url_secrets("https://192.168.100.1/x?YWRtaW46c2FuaXRpemVk")
    assert pii.find_url_secrets("https://192.168.100.1/x?YWRtaW46RmFrZVBhc3My")


# ---------------------------------------------------------------------------
# Fleet regression — the committed corpus must stay clean
# ---------------------------------------------------------------------------

_CATALOG_MODEMS = Path(__file__).parent.parent / "solentlabs" / "cable_modem_monitor_catalog" / "modems"


def _catalog_hars() -> list[Path]:
    return sorted(_CATALOG_MODEMS.rglob("*.har"))


@pytest.mark.parametrize("har_path", _catalog_hars(), ids=lambda p: p.name)
def test_committed_fixture_has_no_url_secrets(har_path: Path) -> None:
    """No committed catalog HAR may carry a secret on its URL surface."""
    data = json.loads(har_path.read_text(encoding="utf-8"))
    findings: list[str] = []
    for entry in data.get("log", {}).get("entries", []):
        request = entry.get("request", {})
        findings.extend(pii.find_url_secrets(request.get("url", ""), request.get("queryString")))

    assert not findings, f"{har_path.name} carries URL secrets: {findings}"
