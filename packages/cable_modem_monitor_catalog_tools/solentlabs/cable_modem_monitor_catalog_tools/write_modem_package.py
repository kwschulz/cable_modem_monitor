"""Write Modem Package Tool — MCP tool.

Writes the standard catalog directory structure for a modem package:
modem.yaml, parser.yaml, optional parser.py, HAR copy, and golden file.

Supports modem variants via ``variant_name``: when set, test data files
use ``modem-{variant_name}`` prefix (e.g., ``modem-v2.har``). Config
resolution in the test harness falls back to ``modem.yaml`` when no
variant-specific config exists.

Produces files on disk so that ``run_tests`` can exercise the pipeline.
Does not overwrite existing files — reports them as skipped.

Per ONBOARDING_SPEC.md ``write_modem_package`` tool contract.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from solentlabs.cable_modem_monitor_core.validation.parser_sandbox import validate_parser_sandbox

from .validation.fixture_integrity import check_login_page_in_har


@dataclass
class WriteResult:
    """Result of writing a modem package.

    Attributes:
        modem_dir: Path written to (for ``run_tests``).
        files_written: Files created or updated.
        files_skipped: Files that already existed (not overwritten).
        errors: Error messages (e.g., sandbox violations).
    """

    modem_dir: str = ""
    files_written: list[str] = field(default_factory=list)
    files_skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """Serialize to a plain dict for MCP tool output."""
        return {
            "modem_dir": self.modem_dir,
            "files_written": self.files_written,
            "files_skipped": self.files_skipped,
            "errors": self.errors,
        }


def write_modem_package(
    output_dir: str,
    modem_yaml: str,
    parser_yaml: str | None,
    golden_file: dict[str, object],
    har_path: str,
    parser_py: str | None = None,
    variant_name: str | None = None,
) -> WriteResult:
    """Write the standard catalog directory structure for a modem.

    Creates::

        {output_dir}/
        ├── modem.yaml
        ├── parser.yaml
        ├── parser.py              (if provided)
        └── test_data/
            ├── modem.har          (copied from har_path)
            └── modem.expected.json

    With ``variant_name="v2"``, test data uses variant prefix::

        {output_dir}/
        └── test_data/
            ├── modem-v2.har
            └── modem-v2.expected.json

    Existing files are not overwritten — they appear in
    ``files_skipped`` instead.

    Args:
        output_dir: Target directory (e.g., ``catalog/modems/{manufacturer}/{model}/``).
        modem_yaml: YAML string for modem.yaml.
        parser_yaml: YAML string for parser.yaml.
        golden_file: Golden file dict to serialize as JSON.
        har_path: Path to the source HAR file to copy.
        parser_py: Optional Python source for parser.py.
        variant_name: Optional variant suffix. When set, test data
            files use ``modem-{variant_name}`` prefix. Config files
            (modem.yaml, parser.yaml) are shared across variants.

    Returns:
        ``WriteResult`` with paths written and skipped.
    """
    result = WriteResult()

    # Refuse before touching disk: a partial package (modem.yaml written,
    # then a crash) looks half-onboarded and confuses every later step.
    if not parser_yaml:
        result.errors.append(
            "parser.yaml is empty: a catalog package needs a parser, and "
            "analyze_har found no data sections in this capture (see its "
            "warnings). Nothing was written."
        )
        return result

    # Sandbox check — parser.py must be a "pure parser"
    if parser_py is not None and (violations := validate_parser_sandbox(parser_py)):
        result.errors = [f"parser.py: {v}" for v in violations]
        return result

    # HAR/config consistency check — login_page must be present in the fixture
    auth_issue = _check_har_login_page(modem_yaml, har_path)
    if auth_issue:
        result.errors.append(auth_issue)
        return result

    out = Path(output_dir)
    test_data = out / "test_data"

    # Create directories
    out.mkdir(parents=True, exist_ok=True)
    test_data.mkdir(parents=True, exist_ok=True)
    result.modem_dir = str(out)

    # Write config files (shared across variants)
    _write_text(out / "modem.yaml", modem_yaml, result)
    _write_text(out / "parser.yaml", parser_yaml, result)

    if parser_py is not None:
        _write_text(out / "parser.py", parser_py, result)

    # Test data file prefix: "modem" or "modem-{variant}"
    prefix = f"modem-{variant_name}" if variant_name else "modem"

    # Write golden file
    golden_path = test_data / f"{prefix}.expected.json"
    golden_content = json.dumps(golden_file, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    _write_text(golden_path, golden_content, result)

    # Copy HAR file
    har_dest = test_data / f"{prefix}.har"
    _copy_file(Path(har_path), har_dest, result)

    return result


def _write_text(path: Path, content: str, result: WriteResult) -> None:
    """Write text content to path, skipping if file exists."""
    if path.is_file():
        result.files_skipped.append(str(path))
        return
    path.write_text(content, encoding="utf-8")
    result.files_written.append(str(path))


def _copy_file(src: Path, dest: Path, result: WriteResult) -> None:
    """Copy a file, skipping if destination exists."""
    if dest.is_file():
        result.files_skipped.append(str(dest))
        return
    if not src.is_file():
        result.files_skipped.append(f"{dest} (source not found: {src})")
        return
    shutil.copy2(src, dest)
    result.files_written.append(str(dest))


def _check_har_login_page(modem_yaml: str, har_path: str) -> str:
    """Return an issue string if the HAR fixture is inconsistent with login_page.

    Only checks ``form`` auth strategy with a non-empty ``login_page``.
    Returns empty string when the check is not applicable or passes.
    """
    try:
        config: Any = yaml.safe_load(modem_yaml)
    except yaml.YAMLError:
        return ""

    if not isinstance(config, dict):
        return ""

    auth = config.get("auth", {})
    if not isinstance(auth, dict):
        return ""
    if auth.get("strategy") != "form":
        return ""

    login_page: str = auth.get("login_page", "") or ""
    action: str = auth.get("action", "") or ""
    if not login_page or not action:
        return ""

    har_file = Path(har_path)
    if not har_file.is_file():
        return ""

    try:
        import gzip as _gzip

        opener = _gzip.open if har_file.suffix == ".gz" else open
        with opener(har_file) as f:
            har_data: Any = json.load(f)
        entries: list[Any] = har_data.get("log", {}).get("entries", [])
    except Exception:
        return ""

    issue = check_login_page_in_har(entries, login_page, action)
    if issue:
        return f"HAR fixture: {issue}"
    return ""
