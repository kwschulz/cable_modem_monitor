#!/usr/bin/env python3
"""Pre-commit hook to check for PII in test fixtures.

Scans HTML/HTM/JSON fixture files for potential personally identifiable information:
- MAC addresses (not anonymized with XX:XX:XX:XX:XX:XX)
- IP addresses (only 192.168.100.x allowed in fixtures)
- Serial numbers
- WiFi SSIDs and credentials
- Session tokens and CSRF tokens
- Email addresses
- Other sensitive patterns

Also validates HAR files for the same patterns.

Reference data (safe values, allowlists) is loaded from
``data/pii_safe_values.json`` so it can be reused by other tools.

Exit codes:
- 0: No PII found
- 1: Potential PII detected (review required)
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import yaml
from har_capture.patterns import load_allowlist
from har_capture.sanitization import check_for_pii

_LFS_POINTER_PREFIX = "version https://git-lfs.github.com/spec/v1"

# ---------------------------------------------------------------------------
# Load reference data
# ---------------------------------------------------------------------------

_DATA_DIR = Path(__file__).parent / "data"


def _load_safe_values() -> dict[str, list[str]]:
    """Load safe-value reference data from the JSON sidecar file.

    Returns a dict mapping section name → list of values.
    """
    path = _DATA_DIR / "pii_safe_values.json"
    raw = json.loads(path.read_text())
    return {key: section["values"] for key, section in raw.items() if isinstance(section, dict) and "values" in section}


_SAFE = _load_safe_values()

# ---------------------------------------------------------------------------
# Catalog-driven custom PII patterns
# ---------------------------------------------------------------------------
#
# The catalog declares each modem's credential field name in
# modem.yaml (auth.password_field). har-capture's universal
# sensitive-field patterns catch most of these (anything matching
# ``password|passwd|pwd|\bpass``), but quirky names like Hitron
# CODA56's ``pws`` slip through. Build a custom-patterns JSON from
# the catalog declarations so check_for_pii flags fixture commits
# that contain those quirky fields with non-redacted values.
#
# Same source-of-truth principle as the runtime auth-failure log
# scrubber in Core: the catalog owns the field-name knowledge; the
# PII gate consumes that ownership.

_CATALOG_MODEMS = Path(__file__).parent.parent / "solentlabs" / "cable_modem_monitor_catalog" / "modems"

# Field names already covered by har-capture's defaults — anything
# matching these patterns can be omitted from our custom regex to
# avoid duplicate findings.
_DEFAULT_COVERAGE_RE = re.compile(
    r"password|passwd|pwd|\bpass|secret|token|credential",
    re.IGNORECASE,
)


def _collect_uncovered_credential_fields() -> list[str]:
    """Return catalog-declared password_field names not covered by har-capture defaults.

    Walks every ``modem*.yaml`` in the catalog, extracts
    ``auth.password_field`` (string or list-of-strings — both
    shapes appear in the catalog), and returns the set of names
    that har-capture's universal patterns would NOT match.
    """
    fields: set[str] = set()
    if not _CATALOG_MODEMS.is_dir():
        return []
    for yml in _CATALOG_MODEMS.rglob("modem*.yaml"):
        try:
            data = yaml.safe_load(yml.read_text()) or {}
        except yaml.YAMLError:
            continue
        auth = (data.get("auth") or {}) if isinstance(data, dict) else {}
        pf = auth.get("password_field") if isinstance(auth, dict) else None
        if isinstance(pf, str) and pf:
            fields.add(pf)
        elif isinstance(pf, list):
            for item in pf:
                if isinstance(item, str) and item:
                    fields.add(item)
    return sorted(f for f in fields if not _DEFAULT_COVERAGE_RE.search(f))


def _build_catalog_pii_patterns_file() -> str | None:
    """Generate a custom_patterns JSON for ``check_for_pii``.

    Returns the path to the generated JSON file, or ``None`` if
    the catalog has no credential fields beyond what har-capture
    already covers (in which case the caller passes ``None`` to
    ``check_for_pii`` and uses defaults only).
    """
    uncovered = _collect_uncovered_credential_fields()
    if not uncovered:
        return None

    # Match form-urlencoded (``field=value``), JSON (``"field": "value"``),
    # and YAML (``field: value``). Allows optional quoting around the
    # field name. Requires at least 4 chars in the value to filter out
    # empty/short placeholders.
    field_alt = "|".join(re.escape(f) for f in uncovered)
    pattern_def = {
        "patterns": {
            "catalog_credential_field": {
                "regex": rf"['\"]?\b({field_alt})\b['\"]?\s*[=:]\s*['\"]?([^\s,&'\"]{{4,}})['\"]?",
                "flags": ["IGNORECASE"],
            }
        }
    }

    out = Path(tempfile.gettempdir()) / "cmm_catalog_pii_patterns.json"
    out.write_text(json.dumps(pattern_def, indent=2))
    return str(out)


_CATALOG_PII_PATTERNS = _build_catalog_pii_patterns_file()

# Build a simple set of known allowlisted values from har-capture
_allowlist = load_allowlist()
PII_ALLOWLIST = set(_allowlist.get("static_placeholders", {}).get("values", []))
for prefix in _allowlist.get("hash_prefixes", {}).get("values", []):
    PII_ALLOWLIST.add(prefix.rstrip("_"))

# ---------------------------------------------------------------------------
# Reference data (from JSON)
# ---------------------------------------------------------------------------

SAFE_MACS: set[str] = set(_SAFE["safe_macs"])
SAFE_IP_PREFIXES: tuple[str, ...] = tuple(_SAFE["safe_ip_prefixes"])
SAFE_IP_VALUES: set[str] = set(_SAFE.get("safe_ip_values", []))
_SAFE_IPV6_PREFIXES: tuple[str, ...] = tuple(_SAFE["safe_ipv6_prefixes"])
SAFE_EMAIL_VALUES: set[str] = {v.lower() for v in _SAFE.get("safe_email_values", [])}
SAFE_SSID_VALUES: set[str] = set(_SAFE["safe_ssids"])
TAGVALUE_SAFE_PATTERNS: set[str] = set(_SAFE["safe_tagvalue_patterns"])
_SAFE_SERIAL_PREFIXES: tuple[str, ...] = tuple(_SAFE["safe_serial_prefixes"])
_CODE_INDICATORS: tuple[str, ...] = tuple(_SAFE["code_indicators"])
_SAFE_CREDENTIAL_PLACEHOLDERS: set[str] = {v.lower() for v in _SAFE.get("safe_credential_placeholders", [])}

# Known safe values (har-capture allowlist + common placeholders +
# documented public credential placeholders from the catalog).
SAFE_VALUES = set(PII_ALLOWLIST) | {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff", "0.0.0.0"} | _SAFE_CREDENTIAL_PLACEHOLDERS

# ---------------------------------------------------------------------------
# Regex patterns (logic — stays in code)
# ---------------------------------------------------------------------------

WIFI_CRED_PATTERN = re.compile(
    r"var\s+tagValueList\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)

MOTO_PASSWORD_PATTERN = re.compile(
    r"var\s+Current(?:Pw|Password)[A-Za-z]*\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)

SSID_PATTERNS = [
    re.compile(r'class="[^"]*ssidValue[^"]*"[^>]*>([^<]+)<', re.IGNORECASE),
    re.compile(r'"ssid"\s*:\s*"([^"]+)"', re.IGNORECASE),
    re.compile(r'"SSID"\s*:\s*"([^"]+)"', re.IGNORECASE),
    re.compile(r'"networkName"\s*:\s*"([^"]+)"', re.IGNORECASE),
]

SESSION_TOKEN_PATTERNS = [
    re.compile(r'"sessionid"\s*:\s*"([a-f0-9]{16,})"', re.IGNORECASE),
    re.compile(r'"token"\s*:\s*"([a-f0-9]{16,})"', re.IGNORECASE),
    re.compile(r'"csrf[_-]?token"\s*:\s*"([^"]+)"', re.IGNORECASE),
    re.compile(r'"auth[_-]?token"\s*:\s*"([^"]+)"', re.IGNORECASE),
]

IP_PATTERN = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")

# JS/code identifier: starts with s/S, all word characters (SNRLevel, SnmpLog, etc.)
_JS_IDENT = re.compile(r"^[sS]\w+$")


# ---------------------------------------------------------------------------
# Safe-value checks
# ---------------------------------------------------------------------------


def is_safe_ip(ip: str) -> bool:
    """Check if an IP address is safe (private or documentation range)."""
    return any(ip.startswith(prefix) for prefix in SAFE_IP_PREFIXES)


def is_safe_mac(mac: str) -> bool:
    """Check if a MAC address is safe (zeroed, broadcast, or placeholder)."""
    mac_lower = mac.lower()
    if mac_lower in SAFE_MACS or mac_lower == "xx:xx:xx:xx:xx:xx":
        return True
    # har-capture format-preserving hashes produce 02:xx:xx:xx:xx:xx
    # (locally-administered bit set)
    return mac_lower.startswith("02:")


def _is_safe_ip_finding(match: str) -> bool:
    """Check if an IP finding is a known false positive."""
    if is_safe_ip(match) or match in SAFE_IP_VALUES:
        return True
    octets = match.split(".")
    if len(octets) != 4 or not all(o.isdigit() for o in octets):
        return False
    # DOCSIS/firmware version numbers (all octets < 10)
    if all(int(o) < 10 for o in octets):
        return True
    # Browser/app version strings: N.0.0.0 (e.g. Chrome/148.0.0.0).
    # Real public IPs never appear as host-zero network addresses in traffic.
    return all(o == "0" for o in octets[1:])


def _is_safe_serial_finding(match: str) -> bool:
    """Check if a serial_number finding is a known false positive."""
    if any(match.startswith(p) for p in _SAFE_SERIAL_PREFIXES):
        return True
    if _JS_IDENT.match(match) or match in ("snapshot", "snippet"):
        return True
    return any(ind in match for ind in (*_CODE_INDICATORS, "/", ":"))


def _has_code_indicators(match: str) -> bool:
    """Check if a match contains JS/code patterns."""
    return any(ind in match for ind in _CODE_INDICATORS)


def _is_safe_catalog_credential_match(match: str) -> bool:
    """True if a catalog_credential_field match holds a known placeholder.

    Match shape: ``<field>=<value>`` or ``<field>: <value>``. Safe only
    when the value portion is a known redaction placeholder. No
    code-indicator check here: the bare match is just the field+value
    pair with no surrounding source context to inspect.
    """
    value_match = re.search(r"[=:]\s*['\"]?([^'\"\s,&]+)", match)
    if not value_match:
        return False
    value = value_match.group(1).strip()
    return value.startswith("***") or value == "[REDACTED]" or value.lower() in SAFE_VALUES


def _is_safe_session_token_or_account_id(match: str) -> bool:
    return _has_code_indicators(match) or " " in match


def _is_safe_password_field(match: str) -> bool:
    return _has_code_indicators(match) or match == "password=password"


def _is_safe_ipv6(match: str) -> bool:
    match_lower = match.lower()
    if any(match_lower.startswith(p) for p in _SAFE_IPV6_PREFIXES):
        return True
    return len(match) <= 5


def _is_safe_private_ip(match: str) -> bool:
    # SAFE_IP_VALUES holds exact firmware-embedded addresses; the public-IP
    # handler already consults it, and a private example address is no less
    # safe for being RFC1918.
    return is_safe_ip(match) or match.startswith("10.") or match in SAFE_IP_VALUES


# Dispatch from pattern name → safe-finding handler. Adding a new
# pattern category means adding a row here, not extending the if/elif
# chain (which would push complexity back over the C901 threshold).
_SAFE_FINDING_DISPATCH: dict[str, Callable[[str], bool]] = {
    "public_ip": _is_safe_ip_finding,
    "mac_address": is_safe_mac,
    "private_ip": _is_safe_private_ip,
    "serial_number": _is_safe_serial_finding,
    "password_field": _is_safe_password_field,
    "catalog_credential_field": _is_safe_catalog_credential_match,
    "session_token": _is_safe_session_token_or_account_id,
    "account_id": _is_safe_session_token_or_account_id,
    "ipv6": _is_safe_ipv6,
    # Open-source library author credits embedded in modem firmware JS.
    "email": lambda m: m.lower() in SAFE_EMAIL_VALUES,
    # Cable modem fixtures never contain credit card numbers. Long float
    # fractions (e.g. tcp_latency_ms) pattern-match card numbers.
    "credit_card_visa": lambda _: True,
    "credit_card_mastercard": lambda _: True,
}


def _is_safe_finding(finding: dict[str, str]) -> bool:
    """Filter known false positives from har-capture check_for_pii."""
    pattern = finding["pattern"]
    match = finding["match"]

    if match.lower() in SAFE_VALUES:
        return True

    handler = _SAFE_FINDING_DISPATCH.get(pattern)
    return handler(match) if handler else False


# ---------------------------------------------------------------------------
# File-type checkers
# ---------------------------------------------------------------------------


def check_motorola_passwords(content: str, filepath: Path) -> list[str]:
    """Check for Motorola JavaScript password variables.

    Looks for var CurrentPwAdmin = 'value' or var CurrentPwUser = 'value' patterns.
    """
    issues = []

    for match in MOTO_PASSWORD_PATTERN.finditer(content):
        password = match.group(1)
        if password.startswith("***"):
            continue
        issues.append(f"  Motorola password variable found: '{password}'")

    return issues


def check_tagvaluelist_credentials(content: str, filepath: Path) -> list[str]:
    """Check tagValueList for potential WiFi credentials.

    Looks for alphanumeric strings 8+ chars that could be passwords/SSIDs.
    """
    issues = []

    for match in WIFI_CRED_PATTERN.finditer(content):
        values_str = match.group(1)
        values = values_str.split("|")

        for i, val in enumerate(values):
            val_stripped = val.strip()
            val_lower = val_stripped.lower()

            if (
                len(val_stripped) >= 8
                and re.match(r"^[a-zA-Z0-9]+$", val_stripped)
                and val_lower not in TAGVALUE_SAFE_PATTERNS
                and not re.match(r"^\d+$", val_stripped)
                and not val_stripped.startswith("***")
                and not re.match(r"^V\d", val_stripped)
                and not val_stripped.endswith("Hz")
                and not val_stripped.endswith("dB")
                and not val_stripped.endswith("dBmV")
                and "Ksym" not in val_stripped
            ):
                issues.append(f"  Potential WiFi credential in tagValueList position {i}: '{val_stripped}'")

    return issues


def check_ssids(content: str, filepath: Path) -> list[str]:
    """Check for WiFi SSIDs that haven't been redacted."""
    issues = []

    for pattern in SSID_PATTERNS:
        for match in pattern.finditer(content):
            ssid = match.group(1).strip()
            ssid_lower = ssid.lower()

            if ssid_lower in SAFE_SSID_VALUES or ssid.startswith("***"):
                continue
            if not ssid or ssid.isspace():
                continue

            issues.append(f"  WiFi SSID found: '{ssid}'")

    return issues


def check_session_tokens(content: str, filepath: Path) -> list[str]:
    """Check for session tokens and CSRF tokens that should be redacted."""
    issues = []

    for pattern in SESSION_TOKEN_PATTERNS:
        for match in pattern.finditer(content):
            token = match.group(1)

            if token.startswith("***"):
                continue

            issues.append(f"  Session/auth token found: '{token[:20]}...'")

    return issues


def check_ips_in_content(content: str, filepath: Path) -> list[str]:
    """Check for IP addresses that aren't in the safe list."""
    issues = []
    seen_ips: set[str] = set()

    for match in IP_PATTERN.finditer(content):
        ip = match.group(1)

        if ip in seen_ips:
            continue
        seen_ips.add(ip)

        if is_safe_ip(ip) or ip in SAFE_IP_VALUES:
            continue

        octets = ip.split(".")
        if not all(0 <= int(o) <= 255 for o in octets):
            continue
        if any(len(o) > 1 and o.startswith("0") for o in octets):
            continue
        if all(int(o) < 10 for o in octets):
            continue
        issues.append(f"  Non-allowed IP address: {ip}")

    return issues


def check_html_file(filepath: Path) -> list[str]:
    """Check a single HTML file for PII."""
    issues = []
    content = filepath.read_text(errors="ignore")

    findings = check_for_pii(content, str(filepath), custom_patterns=_CATALOG_PII_PATTERNS)
    for finding in findings:
        if not _is_safe_finding(finding):
            issues.append(f"  {finding['pattern']}: {finding['match']} (line {finding['line']})")

    issues.extend(check_tagvaluelist_credentials(content, filepath))
    issues.extend(check_motorola_passwords(content, filepath))
    issues.extend(check_ssids(content, filepath))
    issues.extend(check_session_tokens(content, filepath))
    issues.extend(check_ips_in_content(content, filepath))

    return issues


def check_json_file(filepath: Path) -> list[str]:
    """Check a single JSON file for PII."""
    issues = []

    try:
        content = filepath.read_text(errors="ignore")
    except Exception as e:
        return [f"  Failed to read file: {e}"]

    issues.extend(check_ssids(content, filepath))
    issues.extend(check_session_tokens(content, filepath))
    issues.extend(check_ips_in_content(content, filepath))

    findings = check_for_pii(content, str(filepath), custom_patterns=_CATALOG_PII_PATTERNS)
    for finding in findings:
        if not _is_safe_finding(finding):
            issues.append(f"  {finding['pattern']}: {finding['match']} (line {finding['line']})")

    return issues


# A DOCSIS device certificate is issued to the modem's MAC, so a TLS
# capture records it as the cert subject. Neither existing pass sees it:
# har-capture's sanitizer covers cookies, form fields, headers and bodies,
# and the extract_text walk below only visits text/value/content keys.
_CERT_IDENTIFIER_PATTERNS = (
    re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$"),  # colon or dash MAC
    re.compile(r"^[0-9A-Fa-f]{12}$"),  # bare hex, the form CableLabs subjects use
)


def _is_device_identifier(value: str) -> bool:
    """True when a cert subject reads as a per-device id rather than a hostname."""
    # An all-X placeholder is the redacted form, not a finding.
    if set(value.upper()) <= {"X", ":", "-"}:
        return False
    return any(pattern.match(value) for pattern in _CERT_IDENTIFIER_PATTERNS)


def check_cert_identifiers(har_data: dict | list) -> list[str]:
    """Flag TLS certificate fields carrying a per-device identifier."""
    issues: list[str] = []

    def walk(obj: dict | list) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in ("subjectName", "issuer") and isinstance(value, str):
                    if _is_device_identifier(value):
                        issues.append(f"  cert_identifier: {value} (in _securityDetails.{key})")
                else:
                    walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(har_data)
    return issues


def check_har_file(filepath: Path) -> list[str]:  # noqa: C901
    """Check a HAR file for PII."""
    issues = []

    try:
        content = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return [f"  Failed to read HAR file: {e}"]

    if content.startswith(_LFS_POINTER_PREFIX):
        return [f"  {filepath.name} is a Git LFS pointer — run: git lfs pull"]

    try:
        har_data = json.loads(content)
    except json.JSONDecodeError as e:
        return [f"  Failed to parse HAR file: {e}"]

    def extract_text(obj: dict | list | str, path: str = "") -> None:  # noqa: C901
        if isinstance(obj, dict):
            for key, value in obj.items():
                new_path = f"{path}.{key}" if path else key
                if key in ("text", "value", "content") and isinstance(value, str):
                    findings = check_for_pii(
                        value,
                        f"{filepath}:{new_path}",
                        custom_patterns=_CATALOG_PII_PATTERNS,
                    )
                    for finding in findings:
                        if not _is_safe_finding(finding):
                            issues.append(f"  {finding['pattern']}: {finding['match']} (in {new_path})")
                    if "tagValueList" in value:
                        tagvalue_issues = check_tagvaluelist_credentials(value, filepath)
                        for issue in tagvalue_issues:
                            issues.append(f"{issue} (in {new_path})")
                else:
                    extract_text(value, new_path)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                extract_text(item, f"{path}[{i}]")

    extract_text(har_data)
    issues.extend(check_cert_identifiers(har_data))
    return issues


# ---------------------------------------------------------------------------
# Directory scanning and metadata checks
# ---------------------------------------------------------------------------


def check_metadata_exists(fixture_dir: Path) -> bool:
    """Check if metadata.yaml exists in fixture directory or parent.

    Extended subdirectories inherit metadata from parent fixture directory.
    """
    if (fixture_dir / "metadata.yaml").exists():
        return True
    return bool(fixture_dir.parent and (fixture_dir.parent / "metadata.yaml").exists())


def _report_issues(path: Path, issues: list[str]) -> bool:
    """Print PII findings for a file. Returns True if issues found."""
    if not issues:
        return False
    print(f"\n  Potential PII in {path}:")
    for issue in issues:
        print(issue)
    return True


_CHECKER = {
    ".html": check_html_file,
    ".htm": check_html_file,
    ".json": check_json_file,
    ".har": check_har_file,
}


def _scan_directory(root: Path) -> tuple[int, int]:
    """Scan a directory tree for PII in fixture files.

    Returns (files_checked, exit_code).
    """
    exit_code = 0
    checked_dirs: set[Path] = set()
    files_checked = 0

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix == ".json" and path.name == "state.json":
            continue

        checker = _CHECKER.get(suffix)
        if checker is None:
            continue

        files_checked += 1
        if _report_issues(path, checker(path)):
            exit_code = 1

        if suffix in (".html", ".htm"):
            fixture_dir = path.parent
            if fixture_dir not in checked_dirs:
                checked_dirs.add(fixture_dir)
                if not check_metadata_exists(fixture_dir):
                    print(f"\n  Missing metadata.yaml in {fixture_dir}")
                    exit_code = 1

    return files_checked, exit_code


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Run PII checks on catalog fixture files.

    Invoked from repo root by pre-commit and CI. Path is relative
    to the repo root.
    """
    fixture_root = Path("packages/cable_modem_monitor_catalog/solentlabs/cable_modem_monitor_catalog/modems")

    if not fixture_root.exists():
        print("PII check: catalog modems directory not found")
        return 0

    files_checked, exit_code = _scan_directory(fixture_root)

    if files_checked == 0:
        print("PII check: no fixture files found")
        return 0

    if exit_code == 0:
        print(f"PII check: {files_checked} fixture files clean")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
