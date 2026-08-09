"""Tests for config_flow_helpers — validation pipeline and encoding detection.

Tests the _run_validation() function directly (sync, no HA dependency).
All Core I/O is mocked: detect_protocol, config loaders, ModemDataCollector.

Pipeline behaviour: protocol detection observes the modem's TLS via
TCP probe + handshake; auth runs exactly once; a structured rejection
is surfaced to the user (UC-86).
Pre-fetch encoding detection — connectivity vs non-connectivity error handling.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from solentlabs.cable_modem_monitor_core.auth.base import AuthFailureMode
from solentlabs.cable_modem_monitor_core.catalog_manager import (
    ModemSummary,
    VariantInfo,
)
from solentlabs.cable_modem_monitor_core.connectivity import ConnectivityResult
from solentlabs.cable_modem_monitor_core.orchestration.models import ModemResult
from solentlabs.cable_modem_monitor_core.orchestration.signals import (
    CollectorSignal,
)

from custom_components.cable_modem_monitor.config_flow_helpers import (
    _POST_LOGIN_SIGNALS,
    _SIGNAL_ERROR_MAP,
    _attempt_validation,
    _detect_and_inject_form_nonce_encoding,
    _post_login_error_key,
    _raise_validation_failure,
    _run_validation,
    build_model_display_name,
    build_model_options,
    classify_error,
    default_health_check_interval,
    detect_probes,
    filter_by_manufacturer,
    format_variant_label,
    format_variant_labels,
    get_manufacturers,
    restart_requires_credentials,
)

# =====================================================================
# Helpers
# =====================================================================

_MODULE = "custom_components.cable_modem_monitor.config_flow_helpers"


def _ok_result() -> ModemResult:
    """Successful collection result."""
    return ModemResult(
        success=True,
        signal=CollectorSignal.OK,
        modem_data={"downstream": [], "upstream": []},
    )


def _auth_failed_result(error: str = "HNAP challenge response is not valid JSON") -> ModemResult:
    """Auth failure result (wrong protocol, bad credentials, etc.)."""
    return ModemResult(
        success=False,
        signal=CollectorSignal.AUTH_FAILED,
        error=error,
    )


def _load_auth_result() -> ModemResult:
    """LOAD_AUTH failure — 401/403 on data page after auth."""
    return ModemResult(
        success=False,
        signal=CollectorSignal.LOAD_AUTH,
        error="HTTP 401 on /status.html",
    )


def _connectivity_result() -> ModemResult:
    """Connectivity failure — modem unreachable."""
    return ModemResult(
        success=False,
        signal=CollectorSignal.CONNECTIVITY,
        error="Connection refused",
    )


def _parse_error_result() -> ModemResult:
    """Parse error — modem responded but data is malformed."""
    return ModemResult(
        success=False,
        signal=CollectorSignal.PARSE_ERROR,
        error="Unexpected HTML structure",
    )


def _setup_modem_dir(tmp_path: Path) -> Path:
    """Create a minimal modem directory with required files."""
    modem_dir = tmp_path / "test_mfr" / "test_model"
    modem_dir.mkdir(parents=True)
    (modem_dir / "modem.yaml").touch()
    (modem_dir / "parser.yaml").touch()
    (modem_dir / "parser.py").touch()
    return modem_dir


# =====================================================================
# Pure-function helpers — format_variant_label
# =====================================================================

# hw_version is intentionally NOT shown in the label (#124): it does not
# determine the auth contract and misled contributors. The cases below vary
# hw_version to prove it never changes the output — only auth strategy, the
# variant name, and confirmation status do.
# fmt: off
# ┌───────────────┬──────┬───────────┬────────────────────────┬──────────────────────────────┬────────────────────────┐
# │ auth_strategy │ name │ hw_version│ status                 │ expected                     │ description            │
# ├───────────────┼──────┼───────────┼────────────────────────┼──────────────────────────────┼────────────────────────┤
# │ "none"        │ None │ None      │ "confirmed"            │ "No Authentication"          │ default_no_auth        │
# │ "basic"       │ None │ None      │ "confirmed"            │ "Basic Authentication"       │ default_basic          │
# │ "form_nonce"  │ None │ None      │ "confirmed"            │ "Form Login (Nonce)"         │ default_nonce          │
# │ "url_token"   │ "v7" │ None      │ "confirmed"            │ "URL Token (v7)"             │ name_qualifier         │
# │ "unknown_x"   │ None │ None      │ "confirmed"            │ "unknown_x"                  │ unlisted_strategy      │
# │ "url_token"   │ None │ "v5"      │ "confirmed"            │ "URL Token"                  │ hw_version_ignored     │
# │ "url_token"   │ "v7" │ "v7"      │ "confirmed"            │ "URL Token (v7)"             │ name_eq_hw_shown       │
# │ "hnap"        │ None │ "v6"      │ "confirmed"            │ "HNAP"                       │ hw_version_ignored_hnap│
# │ "form_cbn"    │ None │ None      │ "confirmed"            │ "Form Login CBN"             │ cbn_no_name            │
# │ "none"        │ None │ None      │ "awaiting_verification"│ "No Authentication *"        │ unconfirmed_no_auth    │
# │ "url_token"   │ None │ "v5"      │ "awaiting_verification"│ "URL Token *"                │ unconfirmed_no_name    │
# │ "url_token"   │ "v7" │ "v7"      │ "awaiting_verification"│ "URL Token (v7) *"           │ unconfirmed_named      │
# └───────────────┴──────┴───────────┴────────────────────────┴──────────────────────────────┴────────────────────────┘
#
VARIANT_LABEL_CASES = [
    ("none",       None,  None,  "confirmed",             "No Authentication",          "default_no_auth"),
    ("basic",      None,  None,  "confirmed",             "Basic Authentication",       "default_basic"),
    ("form_nonce", None,  None,  "confirmed",             "Form Login (Nonce)",         "default_nonce"),
    ("url_token",  "v7",  None,  "confirmed",             "URL Token (v7)",             "name_qualifier"),
    ("unknown_x",  None,  None,  "confirmed",             "unknown_x",                 "unlisted_strategy"),
    ("url_token",  None,  "v5",  "confirmed",             "URL Token",                  "hw_version_ignored"),
    ("url_token",  "v7",  "v7",  "confirmed",             "URL Token (v7)",             "name_eq_hw_shown"),
    ("hnap",       None,  "v6",  "confirmed",             "HNAP",                       "hw_version_ignored_hnap"),
    ("form_cbn",   None,  None,  "confirmed",             "Form Login CBN",            "cbn_no_name"),
    # Unconfirmed variants — star appended
    ("none",       None,  None,  "awaiting_verification", "No Authentication *",        "unconfirmed_no_auth"),
    ("url_token",  None,  "v5",  "awaiting_verification", "URL Token *",                "unconfirmed_no_name"),
    ("url_token",  "v7",  "v7",  "awaiting_verification", "URL Token (v7) *",           "unconfirmed_named"),
]
# fmt: on


@pytest.mark.parametrize(
    "auth_strategy,name,hw_version,status,expected,desc",
    VARIANT_LABEL_CASES,
    ids=[c[5] for c in VARIANT_LABEL_CASES],
)
def test_format_variant_label(auth_strategy, name, hw_version, status, expected, desc):
    """format_variant_label builds the label from auth strategy, variant name, and status — never hw_version."""
    variant = VariantInfo(name=name, auth_strategy=auth_strategy, hw_version=hw_version, status=status)
    assert format_variant_label(variant) == expected


def test_format_variant_labels_disambiguates_only_on_collision():
    """hw_version is added back only when two variants would render identically (e.g. S33 generations)."""
    variants = [
        VariantInfo(name=None, auth_strategy="hnap", hw_version=None, status="confirmed"),
        VariantInfo(name=None, auth_strategy="hnap", hw_version="v2", status="confirmed"),
        VariantInfo(name=None, auth_strategy="hnap", hw_version="v3", status="confirmed"),
    ]
    assert format_variant_labels(variants) == ["HNAP", "HNAP (v2)", "HNAP (v3)"]


def test_format_variant_labels_leaves_unique_labels_alone():
    """When base labels already differ, hw_version stays hidden even if present (e.g. SB8200 by auth method)."""
    variants = [
        VariantInfo(name="basic", auth_strategy="url_token", hw_version="v7", status="confirmed"),
        VariantInfo(name="cookie", auth_strategy="url_token", hw_version="v7", status="awaiting_verification"),
    ]
    assert format_variant_labels(variants) == ["URL Token (basic)", "URL Token (cookie) *"]


def test_format_variant_labels_disambiguation_preserves_unconfirmed_marker():
    """The hw_version tiebreaker is inserted before the trailing ``*`` marker."""
    variants = [
        VariantInfo(name=None, auth_strategy="hnap", hw_version="v2", status="awaiting_verification"),
        VariantInfo(name=None, auth_strategy="hnap", hw_version="v3", status="awaiting_verification"),
    ]
    assert format_variant_labels(variants) == ["HNAP (v2) *", "HNAP (v3) *"]


# =====================================================================
# Pure-function helpers — get_manufacturers / filter_by_manufacturer
# =====================================================================


def test_get_manufacturers_normalizes_and_deduplicates():
    """Case variations consolidated into single title-case entry."""
    summaries = [
        ModemSummary(manufacturer="ARRIS", model="SB8200", path=Path("/fake")),
        ModemSummary(manufacturer="Arris", model="SB6183", path=Path("/fake")),
        ModemSummary(manufacturer="netgear", model="CM1100", path=Path("/fake")),
    ]
    assert get_manufacturers(summaries) == ["Arris", "Netgear"]


def test_get_manufacturers_preserves_mixed_case():
    """Deliberate mixed case survives display normalization (CommScope, not Commscope)."""
    summaries = [
        ModemSummary(manufacturer="CommScope", model="G54", path=Path("/fake")),
        ModemSummary(manufacturer="ARRIS", model="SB8200", path=Path("/fake")),
    ]
    assert get_manufacturers(summaries) == ["Arris", "CommScope"]


def test_get_manufacturers_includes_brands():
    """Dropdown is the union of manufacturer and brand names (ARCHITECTURE_DECISIONS § Config Flow)."""
    summaries = [
        ModemSummary(manufacturer="CommScope", model="G54", brands=["Arris"], path=Path("/fake")),
        ModemSummary(manufacturer="Arris", model="S33", brands=["Surfboard"], path=Path("/fake")),
    ]
    assert get_manufacturers(summaries) == ["Arris", "CommScope", "Surfboard"]


def test_get_manufacturers_merges_brand_case_variants():
    """Case variants of one brand collapse to a single dropdown entry."""
    summaries = [
        ModemSummary(manufacturer="Arris", model="S33", brands=["Surfboard"], path=Path("/fake")),
        ModemSummary(manufacturer="Arris", model="SB6183", brands=["SURFboard"], path=Path("/fake")),
    ]
    result = get_manufacturers(summaries)
    assert len(result) == 2
    assert [r.lower() for r in result] == ["arris", "surfboard"]


def test_get_manufacturers_empty():
    """Empty summaries returns empty list."""
    assert get_manufacturers([]) == []


def test_filter_by_manufacturer_case_insensitive():
    """Matches normalized manufacturer name across case variations."""
    summaries = [
        ModemSummary(manufacturer="ARRIS", model="SB8200", path=Path("/fake")),
        ModemSummary(manufacturer="Netgear", model="CM1100", path=Path("/fake")),
        ModemSummary(manufacturer="arris", model="SB6183", path=Path("/fake")),
    ]
    result = filter_by_manufacturer(summaries, "Arris")
    assert len(result) == 2
    assert all(r.manufacturer.lower() == "arris" for r in result)


def test_filter_by_manufacturer_no_match():
    """No match returns empty list."""
    summaries = [ModemSummary(manufacturer="Arris", model="SB8200", path=Path("/fake"))]
    assert filter_by_manufacturer(summaries, "Motorola") == []


def test_filter_by_manufacturer_matches_brands():
    """A brand-bucket selection surfaces modems carrying that brand (G54 under Arris)."""
    summaries = [
        ModemSummary(manufacturer="CommScope", model="G54", brands=["Arris"], path=Path("/fake")),
        ModemSummary(manufacturer="Arris", model="S33", brands=["Surfboard"], path=Path("/fake")),
        ModemSummary(manufacturer="Netgear", model="CM1100", path=Path("/fake")),
    ]
    result = filter_by_manufacturer(summaries, "Arris")
    assert {s.model for s in result} == {"G54", "S33"}
    assert {s.model for s in filter_by_manufacturer(summaries, "Surfboard")} == {"S33"}


# =====================================================================
# Pure-function helpers — build_model_display_name
# =====================================================================

# Status column: ✓ = "confirmed", * = "awaiting_verification".
# ┌──────────────┬──────────┬─────────────────────┬───────────────┬───┬──────────────────────────────────────────────┐
# │ manufacturer │ model    │ aliases             │ brands        │ st│ expected                                     │
# ├──────────────┼──────────┼─────────────────────┼───────────────┼───┼──────────────────────────────────────────────┤
# │ "ARRIS"      │ "SB8200" │ []                  │ []            │ ✓ │ "Arris SB8200"                               │
# │ "Motorola"   │ "MB8611" │ ["MB8612"]          │ []            │ ✓ │ "Motorola MB8611 (MB8612)"                   │
# │ "netgear"    │ "CM1100" │ []                  │ []            │ * │ "Netgear CM1100 *"                           │
# │ "ARRIS"      │ "CM820B" │ ["Zoom 5370"]       │ []            │ * │ "Arris CM820B (Zoom 5370) *"                 │
# │ "CommScope"  │ "G54"    │ []                  │ ["Arris"]     │ * │ "CommScope G54 (Arris) *"                    │
# │ "Arris"      │ "S33"    │ []                  │ ["Surfboard"] │ ✓ │ "Arris S33 (Surfboard)"                      │
# │ "ARRIS"      │ "SB6141" │ ["Motorola SB6141"] │ ["SURFboard"] │ ✓ │ "Arris SB6141 (Motorola SB6141, SURFboard)"  │
# └──────────────┴──────────┴─────────────────────┴───────────────┴───┴──────────────────────────────────────────────┘
#
# The parenthetical shows alternate user-facing names — model_aliases ∪
# brands (ARCHITECTURE_DECISIONS § Config Flow). Mixed-case manufacturer
# styling (CommScope) is preserved; single-case styling is title-cased.
#
# fmt: off
DISPLAY_NAME_CASES = [
    ("ARRIS", "SB8200", [], [], "confirmed",
     "Arris SB8200", "basic"),
    ("Motorola", "MB8611", ["MB8612"], [], "confirmed",
     "Motorola MB8611 (MB8612)", "with_alias"),
    ("netgear", "CM1100", [], [], "awaiting_verification",
     "Netgear CM1100 *", "unverified"),
    ("ARRIS", "CM820B", ["Zoom 5370"], [], "awaiting_verification",
     "Arris CM820B (Zoom 5370) *", "alias_and_star"),
    ("CommScope", "G54", [], ["Arris"], "awaiting_verification",
     "CommScope G54 (Arris) *", "brand_only"),
    ("Arris", "S33", [], ["Surfboard"], "confirmed",
     "Arris S33 (Surfboard)", "brand_confirmed"),
    ("ARRIS", "SB6141", ["Motorola SB6141"], ["SURFboard"], "confirmed",
     "Arris SB6141 (Motorola SB6141, SURFboard)", "alias_and_brand"),
]
# fmt: on


@pytest.mark.parametrize(
    "manufacturer,model,aliases,brands,status,expected,desc",
    DISPLAY_NAME_CASES,
    ids=[c[6] for c in DISPLAY_NAME_CASES],
)
def test_build_model_display_name(manufacturer, model, aliases, brands, status, expected, desc):
    """build_model_display_name formats manufacturer, model, aliases ∪ brands, status."""
    summary = ModemSummary(
        manufacturer=manufacturer,
        model=model,
        model_aliases=aliases,
        brands=brands,
        status=status,
        path=Path("/fake"),
    )
    assert build_model_display_name(summary) == expected


# Bucket-contextual labels: the lead name matches the filter the user
# chose. Brand bucket → brand leads; the parenthetical lists aliases
# and other brands, adding the manufacturer-composed name only when no
# alias anchors the entry. Manufacturer bucket / All → static label.
# ┌────────────────┬───────────────┬───────────────────────────────────────────────────────┬──────────────────┐
# │ modem          │ bucket        │ expected                                              │ description      │
# ├────────────────┼───────────────┼───────────────────────────────────────────────────────┼──────────────────┤
# │ G54            │ "Arris"       │ "Arris G54 (CommScope G54) *"                         │ brand_lead       │
# │ G54            │ "CommScope"   │ "CommScope G54 (Arris) *"                             │ mfr_bucket       │
# │ G54            │ None (All)    │ "CommScope G54 (Arris) *"                             │ all_view         │
# │ G54            │ "arris"       │ "Arris G54 (CommScope G54) *"                         │ case_insensitive │
# │ SB6141         │ "Motorola"    │ "Motorola SB6141 (Arris SB6141, SURFboard)"           │ other_brand_kept │
# │ S33            │ "SURFboard"   │ "SURFboard S33 (Arris S33)"                           │ brand_lead_conf  │
# │ XB6            │ "Xfinity"     │ "Xfinity XB6 (CGM4140COM) *"                          │ alias_anchored   │
# │ XB7            │ "Xfinity"     │ "Xfinity XB7 (CGM4331COM, Panoramic Wifi)"            │ alias_and_others │
# └────────────────┴───────────────┴───────────────────────────────────────────────────────┴──────────────────┘
_G54 = ("CommScope", "G54", [], ["Arris"], "awaiting_verification")
_SB6141 = ("ARRIS", "SB6141", [], ["SURFboard", "Motorola"], "confirmed")
_S33 = ("Arris", "S33", [], ["SURFboard"], "confirmed")
_XB6 = ("Technicolor", "XB6", ["CGM4140COM"], ["Xfinity"], "awaiting_verification")
_XB7 = ("Technicolor", "XB7", ["CGM4331COM"], ["Xfinity", "Panoramic Wifi"], "confirmed")

# fmt: off
BUCKET_LABEL_CASES = [
    (_G54,    "Arris",     "Arris G54 (CommScope G54) *",                "brand_lead"),
    (_G54,    "CommScope", "CommScope G54 (Arris) *",                    "mfr_bucket"),
    (_G54,    None,        "CommScope G54 (Arris) *",                    "all_view"),
    (_G54,    "arris",     "Arris G54 (CommScope G54) *",                "case_insensitive"),
    (_SB6141, "Motorola",  "Motorola SB6141 (Arris SB6141, SURFboard)",  "other_brand_kept"),
    (_S33,    "SURFboard", "SURFboard S33 (Arris S33)",                  "brand_lead_confirmed"),
    (_XB6,    "Xfinity",   "Xfinity XB6 (CGM4140COM) *",                 "alias_anchored"),
    (_XB7,    "Xfinity",   "Xfinity XB7 (CGM4331COM, Panoramic Wifi)",   "alias_and_other_brand"),
]
# fmt: on


@pytest.mark.parametrize(
    "modem,bucket,expected,desc",
    BUCKET_LABEL_CASES,
    ids=[c[3] for c in BUCKET_LABEL_CASES],
)
def test_build_model_display_name_bucket_contextual(modem, bucket, expected, desc):
    """Within a brand bucket the brand leads; manufacturer bucket and All use the static label."""
    manufacturer, model, aliases, brands, status = modem
    summary = ModemSummary(
        manufacturer=manufacturer,
        model=model,
        model_aliases=aliases,
        brands=brands,
        status=status,
        path=Path("/fake"),
    )
    assert build_model_display_name(summary, bucket=bucket) == expected


def test_build_model_options_all_view_lists_row_per_name():
    """All view lists one row per user-facing name — the G54 appears under both Arris and CommScope."""
    summaries = [
        ModemSummary(
            manufacturer="CommScope",
            model="G54",
            brands=["Arris"],
            status="awaiting_verification",
            path=Path("/fake"),
        ),
        ModemSummary(manufacturer="Netgear", model="CM1100", status="confirmed", path=Path("/fake")),
    ]
    options = build_model_options(summaries, None)
    labels = [label for _, label in options]
    assert "Arris G54 (CommScope G54) *" in labels
    assert "CommScope G54 (Arris) *" in labels
    assert "Netgear CM1100" in labels
    assert labels == sorted(labels, key=str.lower)  # both G54 rows land at their own alphabet spot
    # Brand rows carry a "|{brand}" value suffix; stripping it resolves to the same modem.
    values = dict(zip(labels, [v for v, _ in options], strict=True))
    assert values["CommScope G54 (Arris) *"] == "CommScope/G54"
    assert values["Arris G54 (CommScope G54) *"] == "CommScope/G54|Arris"
    assert values["Arris G54 (CommScope G54) *"].split("|", 1)[0] == "CommScope/G54"


def test_build_model_options_bucket_single_row():
    """A bucket view lists each modem once, with the bucket-contextual label."""
    summaries = [
        ModemSummary(
            manufacturer="CommScope",
            model="G54",
            brands=["Arris"],
            status="awaiting_verification",
            path=Path("/fake"),
        ),
        ModemSummary(manufacturer="Arris", model="S33", brands=["SURFboard"], status="confirmed", path=Path("/fake")),
    ]
    options = build_model_options(summaries, "Arris")
    assert options == [
        ("CommScope/G54", "Arris G54 (CommScope G54) *"),
        ("Arris/S33", "Arris S33 (SURFboard)"),
    ]


# =====================================================================
# Pure-function helpers — classify_error
# =====================================================================

# ┌───────────────────┬────────────────────┬───────────────────┐
# │ signal            │ expected key       │ description       │
# ├───────────────────┼────────────────────┼───────────────────┤
# │ CONNECTIVITY      │ "cannot_connect"   │ connectivity      │
# │ AUTH_FAILED       │ "invalid_auth"     │ auth_failed       │
# │ AUTH_UNAVAILABLE  │ "modem_busy"       │ auth_unavailable  │
# │ AUTH_LOCKOUT      │ "invalid_auth"     │ auth_lockout      │
# │ LOAD_ERROR        │ "cannot_connect"   │ load_error        │
# │ LOAD_AUTH         │ "invalid_auth"     │ load_auth         │
# │ LOAD_INTEGRITY    │ "invalid_auth"     │ load_integrity    │
# │ PARSE_ERROR       │ "parse_failed"     │ parse_error       │
# │ OK                │ "unknown"          │ unmapped          │
# │ None              │ "unknown"          │ no_signal         │
# └───────────────────┴────────────────────┴───────────────────┘
#
# AUTH_UNAVAILABLE is the one auth-phase signal that must not read as a
# credential problem: the modem declined to serve the login rather than
# judging it (UC-87a). Its row was missing here while the mapping existed,
# so the behaviour was implemented and unasserted.
#
# fmt: off
CLASSIFY_ERROR_CASES = [
    (CollectorSignal.CONNECTIVITY,      "cannot_connect",   "connectivity"),
    (CollectorSignal.AUTH_FAILED,       "invalid_auth",     "auth_failed"),
    (CollectorSignal.AUTH_UNAVAILABLE,  "modem_busy",       "auth_unavailable"),
    (CollectorSignal.AUTH_LOCKOUT,      "invalid_auth",     "auth_lockout"),
    (CollectorSignal.LOAD_ERROR,        "cannot_connect",   "load_error"),
    (CollectorSignal.LOAD_AUTH,         "invalid_auth",     "load_auth"),
    (CollectorSignal.LOAD_INTEGRITY,    "invalid_auth",     "load_integrity"),
    (CollectorSignal.PARSE_ERROR,       "parse_failed",     "parse_error"),
    (CollectorSignal.OK,                "unknown",          "unmapped"),
    (None,                              "unknown",          "no_signal"),
]
# fmt: on


@pytest.mark.parametrize(
    "signal,expected,desc",
    CLASSIFY_ERROR_CASES,
    ids=[c[2] for c in CLASSIFY_ERROR_CASES],
)
def test_classify_error(signal, expected, desc):
    """classify_error maps CollectorSignal to strings.json error key."""
    assert classify_error("some error", signal) == expected


# =====================================================================
# detect_probes
# =====================================================================


class TestDetectProbes:
    """Test health probe detection with mocked ICMP/HTTP."""

    @patch(f"{_MODULE}.test_http_head", return_value=True)
    @patch(f"{_MODULE}.test_icmp", return_value=True)
    def test_both_probes_succeed(self, mock_icmp, mock_head):
        """Both ICMP and HEAD succeed."""
        config = MagicMock()
        config.health.supports_head = True
        result = detect_probes("192.168.100.1", "http://192.168.100.1", config)
        assert result == {"supports_icmp": True, "supports_head": True}

    @patch(f"{_MODULE}.test_http_head", return_value=True)
    @patch(f"{_MODULE}.test_icmp", return_value=False)
    def test_icmp_fails(self, mock_icmp, mock_head):
        """ICMP blocked but HEAD succeeds."""
        config = MagicMock()
        config.health.supports_head = True
        result = detect_probes("192.168.100.1", "http://192.168.100.1", config)
        assert result == {"supports_icmp": False, "supports_head": True}

    @patch(f"{_MODULE}.test_http_head")
    @patch(f"{_MODULE}.test_icmp", return_value=True)
    def test_head_skipped_when_modem_rejects(self, mock_icmp, mock_head):
        """HEAD not tested when modem.yaml says supports_head=False."""
        config = MagicMock()
        config.health.supports_head = False
        result = detect_probes("192.168.100.1", "http://192.168.100.1", config)
        assert result == {"supports_icmp": True, "supports_head": False}
        mock_head.assert_not_called()

    @patch(f"{_MODULE}.test_http_head", return_value=False)
    @patch(f"{_MODULE}.test_icmp", return_value=True)
    def test_no_health_config(self, mock_icmp, mock_head):
        """No health section — HEAD tested normally."""
        config = MagicMock()
        config.health = None
        result = detect_probes("192.168.100.1", "http://192.168.100.1", config)
        assert result == {"supports_icmp": True, "supports_head": False}

    @patch(f"{_MODULE}.test_http_head", return_value=True)
    @patch(f"{_MODULE}.test_icmp", return_value=True)
    def test_legacy_ssl_forwarded(self, mock_icmp, mock_head):
        """legacy_ssl kwarg forwarded to test_http_head."""
        config = MagicMock()
        config.health.supports_head = True
        detect_probes("192.168.100.1", "https://192.168.100.1", config, legacy_ssl=True)
        mock_head.assert_called_once_with("https://192.168.100.1", legacy_ssl=True)


# =====================================================================
# default_health_check_interval — single default cadence
# =====================================================================


@pytest.mark.parametrize(
    "supports_icmp,supports_head,desc",
    [
        (True, True, "icmp_and_head"),
        (True, False, "icmp_only"),
        (False, True, "head_only"),
        (False, False, "get_only"),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_default_health_check_interval(supports_icmp, supports_head, desc):
    """Single 30s default applies regardless of probe capabilities.

    All probes (ICMP, TCP, HEAD) are lightweight and the GET fallback
    is no longer used at fast cadence, so the per-capability cadence
    differentiation is gone.
    """
    assert default_health_check_interval(supports_icmp, supports_head) == 30


# =====================================================================
# Variant path — modem-{variant}.yaml
# =====================================================================


class TestVariantPath:
    """Verify _run_validation loads modem-{variant}.yaml when variant is set."""

    @patch(f"{_MODULE}.detect_probes")
    @patch(f"{_MODULE}._attempt_validation")
    @patch(f"{_MODULE}.load_post_processor")
    @patch(f"{_MODULE}.load_parser_config")
    @patch(f"{_MODULE}.load_modem_config")
    @patch(f"{_MODULE}.detect_protocol")
    def test_variant_loads_variant_yaml(
        self,
        mock_detect,
        mock_load_modem,
        mock_load_parser,
        mock_load_post,
        mock_collector_cls,
        mock_probes,
        tmp_path,
    ):
        """variant='v2' loads modem-v2.yaml instead of modem.yaml."""
        modem_dir = tmp_path / "test_mfr" / "test_model"
        modem_dir.mkdir(parents=True)
        (modem_dir / "modem-v2.yaml").touch()
        (modem_dir / "parser.yaml").touch()
        (modem_dir / "parser.py").touch()

        mock_detect.return_value = ConnectivityResult(success=True, protocol="http", working_url="http://192.168.100.1")
        mock_load_modem.return_value = MagicMock()
        mock_load_parser.return_value = MagicMock()
        mock_load_post.return_value = MagicMock()
        mock_probes.return_value = {"supports_icmp": True, "supports_head": True}

        mock_collector_cls.return_value = _ok_result()

        _run_validation("192.168.100.1", None, "admin", "pw", modem_dir, "v2")

        mock_load_modem.assert_called_once_with(modem_dir / "modem-v2.yaml")


# =====================================================================
# Auth runs exactly once — no retry chain
# =====================================================================
#
# UC-86: a structured login rejection means stop. The pipeline never
# retries auth across protocols, never retries with weakened ciphers,
# and surfaces the first error directly. detect_protocol's TCP probe
# + TLS handshake choose the transport up front.
#
# ┌────────────────────────────────────┬───────────────────┬─────────────────────────┐
# │ scenario                           │ collector result  │ expected outcome        │
# ├────────────────────────────────────┼───────────────────┼─────────────────────────┤
# │ auto-detected http + auth ok       │ _ok_result()      │ http persisted, no exc  │
# │ auto-detected http + auth fail     │ _auth_failed      │ PermissionError         │
# │ auto-detected http + load_auth     │ _load_auth        │ PermissionError         │
# │ auto-detected http + connectivity  │ _connectivity     │ RuntimeError            │
# │ auto-detected http + parse error   │ _parse_error      │ RuntimeError            │
# │ user-specified http + auth fail    │ _auth_failed      │ PermissionError         │
# │ auto-detected https (modern SSL)   │ _ok_result()      │ https / legacy=False    │
# │ auto-detected https (needs SECLEVEL=0) │ _ok_result()  │ https / legacy=True     │
# └────────────────────────────────────┴───────────────────┴─────────────────────────┘

_SINGLE_ATTEMPT_CASE = tuple[
    str | None,  # user_protocol
    str,  # detected_protocol
    bool,  # detected_legacy_ssl
    ModemResult,  # collector result
    type[Exception] | None,  # expected exception
    str | None,  # expected stored protocol
    bool | None,  # expected stored legacy_ssl
    str,  # description
]

# fmt: off
SINGLE_ATTEMPT_CASES: list[_SINGLE_ATTEMPT_CASE] = [
    (None,   "http",  False, _ok_result(),
     None, "http",  False,
     "auto HTTP + ok"),
    (None,   "http",  False, _auth_failed_result(),
     PermissionError, None, None,
     "auto HTTP + AUTH_FAILED -> PermissionError"),
    (None,   "http",  False, _load_auth_result(),
     PermissionError, None, None,
     "auto HTTP + LOAD_AUTH -> PermissionError"),
    (None,   "http",  False, _connectivity_result(),
     RuntimeError, None, None,
     "auto HTTP + CONNECTIVITY -> RuntimeError"),
    (None,   "http",  False, _parse_error_result(),
     RuntimeError, None, None,
     "auto HTTP + PARSE_ERROR -> RuntimeError"),
    ("http", "http",  False, _auth_failed_result(),
     PermissionError, None, None,
     "user HTTP + AUTH_FAILED -> PermissionError"),
    (None,   "https", False, _ok_result(),
     None, "https", False,
     "auto HTTPS modern -> persisted"),
    (None,   "https", True,  _ok_result(),
     None, "https", True,
     "auto HTTPS legacy -> persisted"),
]
# fmt: on


@pytest.mark.parametrize(
    "user_protocol, detected_protocol, detected_legacy_ssl, collector_result, "
    "expected_exception, expected_protocol, expected_legacy_ssl, description",
    SINGLE_ATTEMPT_CASES,
    ids=[c[-1] for c in SINGLE_ATTEMPT_CASES],
)
class TestSingleAttempt:
    """UC-86: pipeline runs auth once and surfaces the result directly."""

    @patch(f"{_MODULE}.detect_probes")
    @patch(f"{_MODULE}._attempt_validation")
    @patch(f"{_MODULE}.load_post_processor")
    @patch(f"{_MODULE}.load_parser_config")
    @patch(f"{_MODULE}.load_modem_config")
    @patch(f"{_MODULE}.detect_protocol")
    def test_single_attempt_outcome(
        self,
        mock_detect: MagicMock,
        mock_load_modem: MagicMock,
        mock_load_parser: MagicMock,
        mock_load_post: MagicMock,
        mock_attempt: MagicMock,
        mock_probes: MagicMock,
        tmp_path: Path,
        user_protocol: str | None,
        detected_protocol: str,
        detected_legacy_ssl: bool,
        collector_result: ModemResult,
        expected_exception: type[Exception] | None,
        expected_protocol: str | None,
        expected_legacy_ssl: bool | None,
        description: str,
    ) -> None:
        """Each scenario triggers _attempt_validation once and surfaces directly."""
        modem_dir = _setup_modem_dir(tmp_path)

        mock_detect.return_value = ConnectivityResult(
            success=True,
            protocol=detected_protocol,
            legacy_ssl=detected_legacy_ssl,
            working_url=f"{detected_protocol}://192.168.100.1",
        )
        mock_load_modem.return_value = MagicMock()
        mock_load_parser.return_value = MagicMock()
        mock_load_post.return_value = MagicMock()
        mock_probes.return_value = {"supports_icmp": True, "supports_head": True}
        mock_attempt.return_value = collector_result

        if expected_exception is not None:
            with pytest.raises(expected_exception):
                _run_validation(
                    host="192.168.100.1",
                    protocol=user_protocol,
                    username="admin",
                    password="password",
                    modem_dir=modem_dir,
                    variant=None,
                )
        else:
            result = _run_validation(
                host="192.168.100.1",
                protocol=user_protocol,
                username="admin",
                password="password",
                modem_dir=modem_dir,
                variant=None,
            )
            assert result["protocol"] == expected_protocol
            assert result["legacy_ssl"] == expected_legacy_ssl

        assert mock_attempt.call_count == 1


class TestSingleAttemptCollectorArgs:
    """Verify the single auth attempt receives the detected transport."""

    @patch(f"{_MODULE}.detect_probes")
    @patch(f"{_MODULE}._attempt_validation")
    @patch(f"{_MODULE}.load_post_processor")
    @patch(f"{_MODULE}.load_parser_config")
    @patch(f"{_MODULE}.load_modem_config")
    @patch(f"{_MODULE}.detect_protocol")
    def test_https_legacy_forwarded_to_attempt(
        self,
        mock_detect: MagicMock,
        mock_load_modem: MagicMock,
        mock_load_parser: MagicMock,
        mock_load_post: MagicMock,
        mock_attempt: MagicMock,
        mock_probes: MagicMock,
        tmp_path: Path,
    ) -> None:
        """legacy_ssl=True forwarded to _attempt_validation when SECLEVEL=0 is required."""
        modem_dir = _setup_modem_dir(tmp_path)
        mock_detect.return_value = ConnectivityResult(
            success=True,
            protocol="https",
            legacy_ssl=True,
            working_url="https://192.168.100.1",
        )
        mock_load_modem.return_value = MagicMock()
        mock_load_parser.return_value = MagicMock()
        mock_load_post.return_value = MagicMock()
        mock_probes.return_value = {"supports_icmp": True, "supports_head": True}
        mock_attempt.return_value = _ok_result()

        result = _run_validation("192.168.100.1", None, "admin", "pw", modem_dir, None)

        assert mock_attempt.call_count == 1
        kwargs = mock_attempt.call_args.kwargs
        assert kwargs["base_url"] == "https://192.168.100.1"
        assert kwargs["legacy_ssl"] is True
        assert result["protocol"] == "https"
        assert result["legacy_ssl"] is True

    @patch(f"{_MODULE}.detect_probes")
    @patch(f"{_MODULE}._attempt_validation")
    @patch(f"{_MODULE}.load_post_processor")
    @patch(f"{_MODULE}.load_parser_config")
    @patch(f"{_MODULE}.load_modem_config")
    @patch(f"{_MODULE}.detect_protocol")
    def test_health_probes_use_detected_protocol(
        self,
        mock_detect: MagicMock,
        mock_load_modem: MagicMock,
        mock_load_parser: MagicMock,
        mock_load_post: MagicMock,
        mock_attempt: MagicMock,
        mock_probes: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Health probes run against the same transport detect_protocol picked."""
        modem_dir = _setup_modem_dir(tmp_path)
        mock_detect.return_value = ConnectivityResult(
            success=True,
            protocol="https",
            legacy_ssl=False,
            working_url="https://192.168.100.1",
        )
        mock_load_modem.return_value = MagicMock()
        mock_load_parser.return_value = MagicMock()
        mock_load_post.return_value = MagicMock()
        mock_probes.return_value = {"supports_icmp": True, "supports_head": True}
        mock_attempt.return_value = _ok_result()

        _run_validation("192.168.100.1", None, "admin", "pw", modem_dir, None)

        probe_call = mock_probes.call_args
        assert probe_call.args[1] == "https://192.168.100.1"
        assert probe_call.kwargs["legacy_ssl"] is False


# =====================================================================
# End-to-end auth-failure log — real Core, real HARMockServer
# =====================================================================


class TestAuthFailureDetailLog:
    """End-to-end: HA glue → real collector → auth-failure WARNING.

    Other tests in this file mock the validation primitives, so the
    real ``ModemDataCollector`` is never exercised. This class
    runs the full path against a ``HARMockServer`` that returns
    401, asserting:

    - ``PermissionError`` with the right error-key reaches the HA
      layer (form UI gets ``invalid_auth``).
    - The collector emits one sanitized ``WARNING`` carrying the
      modem's response — strategy name, request line, status,
      Content-Type, and a body snippet with the user's password
      replaced by ``[REDACTED]``.

    Regression guard for the auth-capture teardown: if a future
    refactor removes the failure-detail log or breaks the
    HA→Core→logger path, this test fails before ship.
    """

    @staticmethod
    def _write_form_auth_modem_yaml(tmp_path: Path) -> Path:
        """Write a minimal valid form-auth modem.yaml.

        No parser.yaml / parser.py — auth failure short-circuits
        before parsing runs, so this is enough to exercise the
        failure-log path.
        """
        modem_dir = tmp_path / "solent_labs" / "t100"
        modem_dir.mkdir(parents=True)
        (modem_dir / "modem.yaml").write_text(
            "manufacturer: Solent Labs\n"
            "model: T100\n"
            "transport: http\n"
            "default_host: 192.168.100.1\n"
            "status: unsupported\n"
            "auth:\n"
            "  strategy: form\n"
            "  action: /login\n"
        )
        return modem_dir

    @patch(f"{_MODULE}.detect_probes")
    def test_auth_failure_logs_wire_detail_and_raises_permission_error(
        self,
        mock_probes: MagicMock,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """401 on the auth POST → ``PermissionError`` + one WARNING with detail.

        Passes ``protocol="http"`` to skip auto-detection — the TCP
        probe and TLS handshake aren't under test here, and the
        mock-server host isn't reachable on :443.
        """
        import logging

        from solentlabs.cable_modem_monitor_core.test_harness import HARMockServer

        modem_dir = self._write_form_auth_modem_yaml(tmp_path)

        entries = [
            {
                "request": {"method": "POST", "url": "http://192.168.100.1/login"},
                "response": {
                    "status": 401,
                    "headers": [{"name": "Content-Type", "value": "text/plain"}],
                    "content": {"text": "unauthorized"},
                },
            }
        ]
        # Skip real probe I/O — ICMP/HEAD aren't under test here.
        mock_probes.return_value = {"supports_icmp": False, "supports_head": False}

        with (
            caplog.at_level(
                logging.WARNING,
                logger="solentlabs.cable_modem_monitor_core.orchestration.collector",
            ),
            HARMockServer(entries) as server,
        ):
            # ``base_url`` is built as ``f"{protocol}://{host}"``,
            # so pass the mock server's "127.0.0.1:PORT" as host.
            netloc = server.base_url.split("://", 1)[1]

            with pytest.raises(PermissionError) as excinfo:
                _run_validation(
                    host=netloc,
                    protocol="http",
                    username="admin",
                    password="wrong",
                    modem_dir=modem_dir,
                    variant=None,
                )

        # Error-key classification reaches the HA layer.
        assert str(excinfo.value).startswith("auth_error:invalid_auth:")

        # Single WARNING from the collector carrying the failure detail.
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_records, "expected a WARNING log for the auth failure"
        msg = warning_records[0].getMessage()
        assert "Auth failed" in msg
        assert "strategy=form" in msg
        assert "/login" in msg
        assert "401" in msg


# =====================================================================
# Pre-fetch encoding detection — _detect_and_inject_form_nonce_encoding
# =====================================================================


class TestDetectAndInjectFormNonceEncoding:
    """Verify pre-fetch behavior for form_nonce encoding detection."""

    def _form_nonce_config(self) -> MagicMock:
        """Build a MagicMock that passes the isinstance(auth, FormNonceAuth) check."""
        from solentlabs.cable_modem_monitor_core.models.modem_config.auth import (
            FormNonceAuth,
        )

        auth = FormNonceAuth(
            strategy="form_nonce",
            action="/login",
            nonce_field="ar_nonce",
        )
        config = MagicMock()
        config.auth = auth
        return config

    @patch("solentlabs.cable_modem_monitor_core.connectivity.create_session")
    def test_connection_error_raises(self, mock_create_session):
        """ConnectionError from requests propagates as builtins.ConnectionError."""
        import requests

        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("Connection refused")
        mock_create_session.return_value = session

        with pytest.raises(ConnectionError, match="Connection refused"):
            _detect_and_inject_form_nonce_encoding("http://192.168.100.1", self._form_nonce_config())

    @patch("solentlabs.cable_modem_monitor_core.connectivity.create_session")
    def test_timeout_raises(self, mock_create_session):
        """Timeout from requests propagates as builtins.ConnectionError."""
        import requests

        session = MagicMock()
        session.get.side_effect = requests.Timeout("Read timed out")
        mock_create_session.return_value = session

        with pytest.raises(ConnectionError, match="Read timed out"):
            _detect_and_inject_form_nonce_encoding("http://192.168.100.1", self._form_nonce_config())

    @patch("solentlabs.cable_modem_monitor_core.connectivity.create_session")
    def test_non_connectivity_error_falls_back_to_plain(self, mock_create_session):
        """Non-connectivity errors (e.g. bad HTML) fall back to plain encoding."""
        session = MagicMock()
        session.get.side_effect = ValueError("Unexpected response")
        mock_create_session.return_value = session

        encoding, field = _detect_and_inject_form_nonce_encoding("http://192.168.100.1", self._form_nonce_config())
        assert encoding == "plain"
        assert field == ""

    def test_non_form_nonce_skips(self):
        """Non-form_nonce auth returns defaults without any network call."""
        config = MagicMock()
        config.auth = MagicMock()  # Not a FormNonceAuth instance

        encoding, field = _detect_and_inject_form_nonce_encoding("http://192.168.100.1", config)
        assert encoding == "plain"
        assert field == ""


# =====================================================================
# _raise_validation_failure — plain PermissionError / RuntimeError
# =====================================================================


class TestRaiseValidationFailure:
    """``_raise_validation_failure`` maps a failed ModemResult to the right exception."""

    @staticmethod
    def _auth_signals() -> tuple[CollectorSignal, ...]:
        """Match the tuple used inside ``_run_validation``."""
        return (
            CollectorSignal.AUTH_FAILED,
            CollectorSignal.AUTH_LOCKOUT,
            CollectorSignal.LOAD_AUTH,
        )

    def test_auth_signal_raises_permission_error(self) -> None:
        """AUTH_FAILED signal → ``PermissionError`` with ``auth_error:`` prefix."""
        from custom_components.cable_modem_monitor.config_flow_helpers import (
            _raise_validation_failure,
        )

        with pytest.raises(PermissionError, match=r"^auth_error:"):
            _raise_validation_failure(_auth_failed_result(), self._auth_signals())

    def test_non_auth_signal_raises_runtime_error(self) -> None:
        """PARSE_ERROR signal → ``RuntimeError`` with ``collection_error:`` prefix."""
        from custom_components.cable_modem_monitor.config_flow_helpers import (
            _raise_validation_failure,
        )

        with pytest.raises(RuntimeError, match=r"^collection_error:"):
            _raise_validation_failure(_parse_error_result(), self._auth_signals())


# =====================================================================
# restart_requires_credentials
# =====================================================================

_RRC_MODULE = "custom_components.cable_modem_monitor.config_flow_helpers"


def _make_restart_action(*, has_action_auth: bool) -> MagicMock:
    """Build a mock HttpAction with or without action_auth."""
    from solentlabs.cable_modem_monitor_core.models.modem_config.actions import HttpAction

    action = MagicMock(spec=HttpAction)
    action.action_auth = MagicMock() if has_action_auth else None
    return action


def _make_modem_config(
    *,
    has_actions: bool = True,
    has_restart: bool = True,
    restart_has_action_auth: bool = False,
    restart_is_http: bool = True,
) -> MagicMock:
    """Build a mock ModemConfig for restart_requires_credentials tests."""
    from solentlabs.cable_modem_monitor_core.models.modem_config.actions import HnapAction, HttpAction

    config = MagicMock()

    if not has_actions:
        config.actions = None
        return config

    config.actions = MagicMock()

    if not has_restart:
        config.actions.restart = None
        return config

    if restart_is_http:
        action = MagicMock(spec=HttpAction)
        action.action_auth = MagicMock() if restart_has_action_auth else None
    else:
        action = MagicMock(spec=HnapAction)

    config.actions.restart = action
    return config


class TestRestartRequiresCredentials:
    """restart_requires_credentials returns True only when action_auth is set on restart."""

    @patch(f"{_RRC_MODULE}.load_modem_config")
    def test_returns_true_when_action_auth_set(self, mock_load: MagicMock, tmp_path: Path) -> None:
        """True when restart HttpAction has action_auth configured."""
        mock_load.return_value = _make_modem_config(has_restart=True, restart_has_action_auth=True)

        assert restart_requires_credentials(tmp_path, None) is True
        mock_load.assert_called_once_with(tmp_path / "modem.yaml")

    @patch(f"{_RRC_MODULE}.load_modem_config")
    def test_returns_false_when_no_actions(self, mock_load: MagicMock, tmp_path: Path) -> None:
        """False when modem.yaml has no actions block."""
        mock_load.return_value = _make_modem_config(has_actions=False)

        assert restart_requires_credentials(tmp_path, None) is False

    @patch(f"{_RRC_MODULE}.load_modem_config")
    def test_returns_false_when_no_restart(self, mock_load: MagicMock, tmp_path: Path) -> None:
        """False when actions block exists but restart is None."""
        mock_load.return_value = _make_modem_config(has_restart=False)

        assert restart_requires_credentials(tmp_path, None) is False

    @patch(f"{_RRC_MODULE}.load_modem_config")
    def test_returns_false_when_restart_has_no_action_auth(self, mock_load: MagicMock, tmp_path: Path) -> None:
        """False when restart action exists but action_auth is None."""
        mock_load.return_value = _make_modem_config(has_restart=True, restart_has_action_auth=False)

        assert restart_requires_credentials(tmp_path, None) is False

    @patch(f"{_RRC_MODULE}.load_modem_config")
    def test_returns_false_for_non_http_restart(self, mock_load: MagicMock, tmp_path: Path) -> None:
        """False when restart action is not an HttpAction (HNAP, CBN)."""
        mock_load.return_value = _make_modem_config(has_restart=True, restart_is_http=False)

        assert restart_requires_credentials(tmp_path, None) is False

    @patch(f"{_RRC_MODULE}.load_modem_config")
    def test_variant_yaml_loaded(self, mock_load: MagicMock, tmp_path: Path) -> None:
        """variant='v2' causes modem-v2.yaml path to be passed to load_modem_config."""
        mock_load.return_value = _make_modem_config(has_restart=True, restart_has_action_auth=True)

        assert restart_requires_credentials(tmp_path, "v2") is True
        mock_load.assert_called_once_with(tmp_path / "modem-v2.yaml")

    @patch(f"{_RRC_MODULE}.load_modem_config")
    def test_load_failure_returns_false(self, mock_load: MagicMock, tmp_path: Path) -> None:
        """Any load/parse failure returns False (no crash)."""
        mock_load.side_effect = FileNotFoundError("no such file")

        assert restart_requires_credentials(tmp_path, None) is False

    @patch(f"{_RRC_MODULE}.load_modem_config")
    def test_default_yaml_not_used_when_variant_given(self, mock_load: MagicMock, tmp_path: Path) -> None:
        """When variant is specified, modem.yaml path is NOT passed — modem-v2.yaml is."""
        mock_load.return_value = _make_modem_config(has_restart=True, restart_has_action_auth=False)

        restart_requires_credentials(tmp_path, "v2")

        mock_load.assert_called_once_with(tmp_path / "modem-v2.yaml")


# ---------------------------------------------------------------------------
# Session release after validation (#120)
# ---------------------------------------------------------------------------

# Single-session firmware holds one slot. Validation that walks away without
# releasing it strands that slot; the user's browser and our next attempt
# then collide with a session we abandoned.


class TestValidationReleasesSession:
    """_attempt_validation must close the collector on every outcome."""

    # fmt: off
    _CASES = [
        # (signal,                       success, description)
        (CollectorSignal.OK,             True,    "successful validation"),
        (CollectorSignal.LOAD_AUTH,      False,   "401 after successful auth"),
        (CollectorSignal.LOAD_INTEGRITY, False,   "stub page in place of data"),
        (CollectorSignal.AUTH_FAILED,    False,   "credentials rejected"),
        (CollectorSignal.PARSE_ERROR,    False,   "parse failure"),
    ]
    # fmt: on

    @pytest.mark.parametrize(("signal", "success", "description"), _CASES)
    @patch(f"{_MODULE}.create_collector")
    def test_collector_closed(
        self,
        mock_create: MagicMock,
        signal: CollectorSignal,
        success: bool,
        description: str,
    ) -> None:
        """Every validation outcome releases the server-side session."""
        collector = MagicMock()
        collector.execute.return_value = ModemResult(success=success, signal=signal, modem_data={})
        mock_create.return_value = collector

        _attempt_validation(
            modem_config=MagicMock(),
            parser_config=MagicMock(),
            post_processor=None,
            base_url="http://192.168.100.1",
            username="admin",
            password="secret",
            legacy_ssl=False,
        )

        collector.close.assert_called_once()

    @patch(f"{_MODULE}.create_collector")
    def test_collector_closed_when_execute_raises(self, mock_create: MagicMock) -> None:
        """An exception mid-collection must not strand the session either."""
        collector = MagicMock()
        collector.execute.side_effect = RuntimeError("connection reset")
        mock_create.return_value = collector

        with pytest.raises(RuntimeError):
            _attempt_validation(
                modem_config=MagicMock(),
                parser_config=MagicMock(),
                post_processor=None,
                base_url="http://192.168.100.1",
                username="admin",
                password="secret",
                legacy_ssl=False,
            )

        collector.close.assert_called_once()


# ---------------------------------------------------------------------------
# LOAD_AUTH error text (#120)
# ---------------------------------------------------------------------------

# A 401 on a data page follows a successful login, so it is not a credential
# problem. LOAD_AUTH stays in auth_signals, which keeps the raised exception
# type stable for callers; only the message the user reads changes.


class TestPostLoginKeyIsStrategyDependent:
    """A post-login 401 only means "session refused" if the login was verified.

    basic auth never validates a password, and form auth with no
    ``success:`` criterion accepts any response under HTTP 400, so for
    those a 401 on the data page is most often the bad password
    surfacing late. Reporting it as "your password is fine" is what
    beta.17 got wrong. The discriminator is not the strategy name:
    ``form`` answers differently depending on whether the entry
    declares a criterion.
    """

    # fmt: off
    _CASES = [
        # (strategy,     auth kwargs,                        expected key,       description)
        ("basic",        {},                                 "invalid_auth",     "never validates"),
        ("form",         {"action": "/login.htm"},           "invalid_auth",     "no criterion, accepts any 2xx/3xx"),
        ("form",         {"action": "/login.htm",
                          "success": {
                              "redirect": "/index.htm"}},    "session_rejected", "criterion checks the landing"),
        ("form_pbkdf2",  {"login_endpoint": "/l",
                          "pbkdf2_iterations": 1000,
                          "pbkdf2_key_length": 128},         "session_rejected", "verifies via login_success"),
        ("hnap",         {"hmac_algorithm": "md5"},          "session_rejected", "verifies via LoginResult"),
    ]
    # fmt: on

    @staticmethod
    def _config(strategy: str, **auth: Any) -> Any:
        from pydantic import TypeAdapter
        from solentlabs.cable_modem_monitor_core.models.modem_config.auth import AuthConfig

        config = MagicMock()
        config.auth = TypeAdapter(AuthConfig).validate_python({"strategy": strategy, **auth})
        return config

    @pytest.mark.parametrize(
        ("strategy", "auth", "expected", "description"), _CASES, ids=[f"{c[0]}-{c[3]}" for c in _CASES]
    )
    @pytest.mark.parametrize("signal", [CollectorSignal.LOAD_AUTH, CollectorSignal.LOAD_INTEGRITY])
    def test_key_follows_auth_strategy(
        self,
        signal: CollectorSignal,
        strategy: str,
        auth: dict[str, Any],
        expected: str,
        description: str,
    ) -> None:
        """The displayed key is chosen by whether the strategy verifies its login."""
        assert classify_error(None, signal, self._config(strategy, **auth)) == expected

    def test_defaults_to_invalid_auth_without_config(self) -> None:
        """With no modem_config to ask, fall back to the conservative key."""
        assert classify_error(None, CollectorSignal.LOAD_AUTH) == "invalid_auth"

    def test_load_auth_still_raises_permission_error(self) -> None:
        """The error key rides on PermissionError, the auth-signal exception type."""
        auth_signals = (
            CollectorSignal.AUTH_FAILED,
            CollectorSignal.AUTH_LOCKOUT,
            CollectorSignal.LOAD_AUTH,
        )
        result = ModemResult(success=False, signal=CollectorSignal.LOAD_AUTH, error="401 on /data")

        with pytest.raises(PermissionError, match="invalid_auth"):
            _raise_validation_failure(result, auth_signals)


# ---------------------------------------------------------------------------
# Signal-to-message coverage
# ---------------------------------------------------------------------------

# An unmapped signal falls through to "unknown" ("Unexpected error, check the
# logs"), which is what LOAD_INTEGRITY did until it was mapped. These two
# assertions close both ends: every signal reaches a key, every key renders.


class TestSignalErrorMapCoverage:
    """No signal may reach the user as "unknown", and no key may be missing."""

    def test_every_signal_is_mapped(self) -> None:
        """Only OK is exempt — it never reaches the error path."""
        unmapped = {sig for sig in CollectorSignal if sig is not CollectorSignal.OK} - set(_SIGNAL_ERROR_MAP)

        assert not unmapped, f"CollectorSignal members with no strings.json key: {unmapped}"

    def test_every_mapped_key_exists_in_strings_json(self) -> None:
        """Every key classify_error can return must render in the HA form."""
        strings = json.loads(
            (
                Path(__file__).resolve().parents[2] / "custom_components" / "cable_modem_monitor" / "strings.json"
            ).read_text()
        )
        declared = set(strings["config"]["error"])
        produced = set(_SIGNAL_ERROR_MAP.values()) | {"unknown"}

        assert produced <= declared, f"missing from strings.json: {produced - declared}"


# ---------------------------------------------------------------------------
# Spec-to-code gate — CONFIG_FLOW_SPEC § Step 4 vs the mapping it documents
# ---------------------------------------------------------------------------

# The spec table and the code dict are written by different hands and drift
# apart silently: LOAD_INTEGRITY reached users as "unknown" that way. Neither
# side is authoritative on its own, so the gate asserts they agree in both
# directions rather than picking a winner.
#
# Deliberately not gated: that a use case cites a test. A citation is
# satisfied by typing a number into a docstring, which is how UC-10's number
# came to sit in a test asserting UC-10's opposite. Gate what cannot be faked.

_SPEC_PATH = (
    Path(__file__).resolve().parents[2] / "custom_components" / "cable_modem_monitor" / "docs" / "CONFIG_FLOW_SPEC.md"
)


def _step4_tables() -> tuple[dict[str, str], dict[str, str]]:
    """Parse § Step 4's signal table and auth-failure-mode table."""
    text = _SPEC_PATH.read_text(encoding="utf-8")
    start = text.index("### Step 4: Validate")
    # Ends at the next heading of the same level or higher. Search from the
    # end of Step 4's own heading line, or "^" matches that heading itself.
    body = text.index("\n", start) + 1
    nxt = re.search(r"^#{1,3} ", text[body:], re.M)
    section = text[body : body + nxt.start()] if nxt else text[body:]

    signals: dict[str, str] = {}
    modes: dict[str, str] = {}
    target: dict[str, str] | None = None

    for line in section.splitlines():
        if not line.startswith("|"):
            # Any non-table line ends the current table, so prose between
            # two tables cannot leak rows from one into the other.
            target = None
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells[0] == "CollectorSignal":
            target = signals
        elif cells[0] == "`auth_failure_mode()`":
            target = modes
        elif target is not None and set(cells[0]) - set("-: "):
            target[cells[0]] = cells[1]

    assert signals, "§ Step 4 signal table not found — did the heading move?"
    assert modes, "§ Step 4 auth_failure_mode table not found"
    return signals, modes


class TestSpecTableMatchesCode:
    """CONFIG_FLOW_SPEC § Step 4 documents the mapping; the code implements it."""

    def test_signal_table_agrees_with_signal_error_map(self) -> None:
        """Every documented signal maps as documented, and none is undocumented."""
        spec_signals, _ = _step4_tables()

        documented: dict[CollectorSignal, str] = {}
        strategy_dependent: set[CollectorSignal] = set()
        for raw_signal, raw_key in spec_signals.items():
            signal = CollectorSignal[raw_signal.strip("`")]
            if "strategy-dependent" in raw_key:
                strategy_dependent.add(signal)
            else:
                documented[signal] = raw_key.strip("`")

        assert strategy_dependent == _POST_LOGIN_SIGNALS, (
            "signals the spec defers to the auth strategy must be exactly the ones "
            f"the code refines: spec={strategy_dependent}, code={_POST_LOGIN_SIGNALS}"
        )
        in_spec = set(documented) | strategy_dependent
        assert in_spec == set(_SIGNAL_ERROR_MAP), (
            "spec and code disagree on which signals have a key: "
            f"spec-only={in_spec - set(_SIGNAL_ERROR_MAP)}, "
            f"code-only={set(_SIGNAL_ERROR_MAP) - in_spec}"
        )
        mismatched = {
            signal: (key, _SIGNAL_ERROR_MAP[signal])
            for signal, key in documented.items()
            if _SIGNAL_ERROR_MAP[signal] != key
        }
        assert not mismatched, f"signal: (spec key, code key) disagree: {mismatched}"

    def test_mode_table_agrees_with_post_login_error_key(self) -> None:
        """Every AuthFailureMode is documented, and produces the documented key."""
        _, spec_modes = _step4_tables()

        documented = {
            AuthFailureMode[raw_mode.split("`")[1]]: raw_key.strip("`") for raw_mode, raw_key in spec_modes.items()
        }

        assert set(documented) == set(AuthFailureMode), (
            "every mode must be documented — an undocumented one silently takes "
            f"the invalid_auth fallback: {set(AuthFailureMode) - set(documented)}"
        )
        for mode, key in documented.items():
            with patch(f"{_MODULE}.create_auth_manager") as make_manager:
                make_manager.return_value.auth_failure_mode.return_value = mode
                produced = _post_login_error_key(MagicMock())
            assert produced == key, f"{mode.name}: spec says {key!r}, code produced {produced!r}"
