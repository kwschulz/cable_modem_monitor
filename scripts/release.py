#!/usr/bin/env python3
"""Release preparation script for Cable Modem Monitor.

Updates version strings across all files and verifies consistency.
Does NOT stage, commit, push, tag, or create releases — the developer
handles all git operations.

Steps:
1. Validate version format
2. Check git working directory is clean
3. Run `make validate-ci` (full CI mirror — lint, format, type-check,
   tests, intake regression, PII check, catalog README freshness)
4. Verify translations/en.json matches strings.json
5. Update version in all required files:
   - custom_components/cable_modem_monitor/manifest.json
   - custom_components/cable_modem_monitor/const.py
   - tests/components/test_version_and_startup.py
   - packages/cable_modem_monitor_core/pyproject.toml
   - packages/cable_modem_monitor_catalog/pyproject.toml
6. Update CHANGELOG.md
7. Verify all version files are consistent
8. Print changed files and suggested next steps

Usage:
    python scripts/release.py 3.5.2                    # Full release prep
    python scripts/release.py 3.5.2 --skip-changelog   # Don't update changelog
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def print_error(msg: str) -> None:
    """Print error message in red."""
    print(f"\033[91m✗ {msg}\033[0m", file=sys.stderr)


def print_success(msg: str) -> None:
    """Print success message in green."""
    print(f"\033[92m✓ {msg}\033[0m")


def print_info(msg: str) -> None:
    """Print info message in blue."""
    print(f"\033[94mℹ {msg}\033[0m")


def print_warning(msg: str) -> None:
    """Print warning message in yellow."""
    print(f"\033[93m⚠ {msg}\033[0m")


def validate_version(version: str) -> bool:
    """Validate that version follows semantic versioning (X.Y.Z or X.Y.Z-beta.N)."""
    pattern = r"^\d+\.\d+\.\d+(-beta\.\d+)?$"
    return bool(re.match(pattern, version))


def get_repo_root() -> Path:
    """Get the repository root directory."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(result.stdout.strip())
    except subprocess.CalledProcessError as e:
        print_error(f"Failed to get repository root: {e}")
        sys.exit(1)


def check_git_clean() -> bool:
    """Check if git working directory is clean."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        return not result.stdout.strip()
    except subprocess.CalledProcessError:
        return False


def update_manifest(repo_root: Path, version: str) -> bool:
    """Update version in manifest.json."""
    manifest_path = repo_root / "custom_components" / "cable_modem_monitor" / "manifest.json"

    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)

        old_version = manifest.get("version", "unknown")
        manifest["version"] = version

        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
            f.write("\n")  # Add trailing newline

        print_success(f"Updated manifest.json: {old_version} → {version}")
        return True
    except Exception as e:
        print_error(f"Failed to update manifest.json: {e}")
        return False


def update_manifest_requirements(repo_root: Path, version: str) -> bool:
    """Update Core and Catalog version pins in manifest.json requirements."""
    manifest_path = repo_root / "custom_components" / "cable_modem_monitor" / "manifest.json"

    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)

        requirements = manifest.get("requirements", [])
        updated = []
        packages_found = 0
        for req in requirements:
            if req.startswith("solentlabs-cable-modem-monitor-core=="):
                updated.append(f"solentlabs-cable-modem-monitor-core=={version}")
                packages_found += 1
            elif req.startswith("solentlabs-cable-modem-monitor-catalog=="):
                updated.append(f"solentlabs-cable-modem-monitor-catalog=={version}")
                packages_found += 1
            else:
                updated.append(req)

        if packages_found != 2:
            print_error(f"Expected 2 package pins in manifest requirements, found {packages_found}")
            return False

        manifest["requirements"] = updated

        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
            f.write("\n")

        print_success(f"Updated manifest.json requirements pins → {version}")
        return True
    except Exception as e:
        print_error(f"Failed to update manifest requirements: {e}")
        return False


def update_const_py(repo_root: Path, version: str) -> bool:
    """Update VERSION constant in const.py."""
    const_path = repo_root / "custom_components" / "cable_modem_monitor" / "const.py"

    try:
        with open(const_path, encoding="utf-8") as f:
            content = f.read()

        # Find the current version
        version_match = re.search(r'VERSION = "([^"]+)"', content)
        if not version_match:
            print_error("Could not find VERSION constant in const.py")
            return False

        old_version = version_match.group(1)

        # Replace the version
        new_content = re.sub(r'VERSION = "[^"]+"', f'VERSION = "{version}"', content, count=1)

        with open(const_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        print_success(f"Updated const.py: {old_version} → {version}")
        return True
    except Exception as e:
        print_error(f"Failed to update const.py: {e}")
        return False


def update_version_test(repo_root: Path, version: str) -> bool:
    """Update version test in test_version_and_startup.py."""
    test_path = repo_root / "tests" / "components" / "test_version_and_startup.py"

    try:
        with open(test_path, encoding="utf-8") as f:
            content = f.read()

        # Find the current version in the test
        version_match = re.search(r'assert VERSION == "([^"]+)"', content)
        if not version_match:
            print_error("Could not find version assertion in test file")
            return False

        old_version = version_match.group(1)

        # Replace the version
        new_content = re.sub(
            r'assert VERSION == "[^"]+"',
            f'assert VERSION == "{version}"',
            content,
            count=1,
        )

        with open(test_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        print_success(f"Updated test_version_and_startup.py: {old_version} → {version}")
        return True
    except Exception as e:
        print_error(f"Failed to update version test: {e}")
        return False


def update_package_versions(repo_root: Path, version: str) -> bool:
    """Update version in Core and Catalog pyproject.toml files.

    Catalog Tools is intentionally excluded — it is a repo-only internal
    authoring pipeline, never published to PyPI, and its version is not
    coupled to Core/Catalog releases.
    """
    package_dirs = [
        repo_root / "packages" / "cable_modem_monitor_core",
        repo_root / "packages" / "cable_modem_monitor_catalog",
    ]

    all_ok = True
    for pkg_dir in package_dirs:
        pyproject_path = pkg_dir / "pyproject.toml"
        if not pyproject_path.is_file():
            print_warning(f"Not found: {pyproject_path}")
            continue

        try:
            content = pyproject_path.read_text(encoding="utf-8")
            version_match = re.search(r'^version = "([^"]+)"', content, re.MULTILINE)
            if not version_match:
                print_error(f"No version field in {pyproject_path}")
                all_ok = False
                continue

            old_version = version_match.group(1)
            new_content = re.sub(
                r'^version = "[^"]+"',
                f'version = "{version}"',
                content,
                count=1,
                flags=re.MULTILINE,
            )
            pyproject_path.write_text(new_content, encoding="utf-8")
            print_success(f"Updated {pkg_dir.name}/pyproject.toml: {old_version} → {version}")
        except Exception as e:
            print_error(f"Failed to update {pyproject_path}: {e}")
            all_ok = False

    return all_ok


def update_catalog_core_dependency(repo_root: Path, version: str) -> bool:
    """Update Catalog's dependency pin on Core in pyproject.toml.

    Core and Catalog are always released in lock-step, so the dependency
    must be an exact pin (==) rather than a lower-bound (>=).  This also
    avoids uv's refusal to resolve transitive pre-release dependencies
    when HA installs the packages without --prerelease=allow.
    """
    pyproject_path = repo_root / "packages" / "cable_modem_monitor_catalog" / "pyproject.toml"

    try:
        content = pyproject_path.read_text(encoding="utf-8")
        new_content, count = re.subn(
            r'"solentlabs-cable-modem-monitor-core==[^"]+"',
            f'"solentlabs-cable-modem-monitor-core=={version}"',
            content,
        )
        if count != 1:
            print_error(f"Expected 1 core dependency pin in catalog pyproject.toml, found {count}")
            return False

        pyproject_path.write_text(new_content, encoding="utf-8")
        print_success(f"Updated catalog→core dependency pin → =={version}")
        return True
    except Exception as e:
        print_error(f"Failed to update catalog core dependency: {e}")
        return False


def update_changelog(repo_root: Path, version: str) -> bool:
    """Update CHANGELOG.md to move Unreleased to new version."""
    changelog_path = repo_root / "CHANGELOG.md"

    try:
        with open(changelog_path, encoding="utf-8") as f:
            content = f.read()

        # Check if there's an Unreleased section
        if "## [Unreleased]" not in content:
            print_warning("No [Unreleased] section found in CHANGELOG.md")
            return True

        # Get today's date
        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")

        # Replace ## [Unreleased] with ## [version] - date and add new [Unreleased]
        new_content = content.replace(
            "## [Unreleased]",
            f"## [Unreleased]\n\n## [{version}] - {today}",
            1,
        )

        with open(changelog_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        print_success(f"Updated CHANGELOG.md for version {version}")
        return True
    except Exception as e:
        print_error(f"Failed to update CHANGELOG.md: {e}")
        return False


def show_changed_files(version: str) -> None:
    """Show the files that were updated and suggested next steps."""
    files = [
        "custom_components/cable_modem_monitor/manifest.json",
        "custom_components/cable_modem_monitor/const.py",
        "tests/components/test_version_and_startup.py",
        "packages/cable_modem_monitor_core/pyproject.toml",
        "packages/cable_modem_monitor_catalog/pyproject.toml",
        "CHANGELOG.md",
    ]

    # Check if translations were updated
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--", "custom_components/cable_modem_monitor/translations/en.json"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.stdout.strip():
            files.append("custom_components/cable_modem_monitor/translations/en.json")
    except subprocess.CalledProcessError:
        pass

    print_info("Files updated:")
    for f in files:
        print(f"  {f}")

    print_info(f"Suggested commit message: chore: bump version to {version}")
    print_info("After committing and merging to main:")
    print_info("  git checkout main && git pull")
    print_info(f"  git tag v{version}")
    print_info(f"  git push origin v{version}")
    print_info("")
    print_info("Wait for PyPI publish before restarting HA:")
    print_info("  gh run watch $(gh run list -w 'Publish to PyPI' --json databaseId -q '.[0].databaseId')")


def run_validate_ci(repo_root: Path) -> bool:
    """Run the full local CI mirror (`make validate-ci`).

    `make validate-ci` is the canonical local stand-in for the CI Tests
    workflow surface (lint + format + type-check + tests + intake
    regression + PII check + catalog README freshness). Calling it here
    means a release is gated on the same checks CI will run, so version
    bumps can't silently land on top of regressions introduced earlier
    in the cycle.
    """
    try:
        print_info("Running make validate-ci (full CI mirror)...")
        subprocess.run(
            ["make", "validate-ci"],
            cwd=repo_root,
            check=True,
        )
        print_success("Full CI validation passed")
        return True
    except subprocess.CalledProcessError:
        print_error("CI validation failed! Fix issues before releasing.")
        return False


def revalidate_rewritten_files(repo_root: Path) -> bool:
    """Re-check the files this script just rewrote.

    `run_validate_ci` above ran against the pre-bump tree, so nothing has
    yet read the promoted CHANGELOG. Without this, CI on the version-bump
    commit is the first check to see it.
    """
    steps = [
        ("CHANGELOG structure", ["make", "changelog-check"]),
        (
            "CHANGELOG tests",
            [sys.executable, "-m", "pytest", "tests/lib/test_check_changelog.py", "-q", "--no-header"],
        ),
    ]
    for label, command in steps:
        try:
            print_info(f"Re-checking {label} against the rewritten files...")
            subprocess.run(command, cwd=repo_root, check=True)
        except subprocess.CalledProcessError:
            print_error(f"{label} failed on the rewritten files.")
            return False
    print_success("Rewritten files pass their checks")
    return True


def _generate_catalog_index(repo_root: Path) -> None:
    """Regenerate the v3.14 catalog package README.md."""
    catalog_script = repo_root / "packages" / "cable_modem_monitor_catalog" / "scripts" / "generate_catalog_index.py"
    if not catalog_script.exists():
        return
    try:
        import subprocess

        result = subprocess.run(
            [sys.executable, str(catalog_script)],
            capture_output=True,
            text=True,
            cwd=repo_root,
        )
        if result.returncode != 0:
            print_warning(f"Catalog index warning: {result.stderr}")
        elif result.stdout:
            print_success(result.stdout.strip())
    except Exception as e:
        print_warning(f"Could not generate catalog index: {e}")


def verify_translations(repo_root: Path) -> bool:
    """Verify translations/en.json matches strings.json."""
    try:
        strings_path = repo_root / "custom_components" / "cable_modem_monitor" / "strings.json"
        translations_path = repo_root / "custom_components" / "cable_modem_monitor" / "translations" / "en.json"

        # Read both files
        with open(strings_path, encoding="utf-8") as f:
            strings_content = json.load(f)

        if not translations_path.exists():
            print_warning("translations/en.json not found, creating from strings.json...")
            translations_path.parent.mkdir(parents=True, exist_ok=True)
            with open(translations_path, "w", encoding="utf-8") as f:
                json.dump(strings_content, f, indent=2)
                f.write("\n")
            print_success("Created translations/en.json")
            return True

        with open(translations_path, encoding="utf-8") as f:
            translations_content = json.load(f)

        if strings_content != translations_content:
            print_warning("translations/en.json differs from strings.json, updating...")
            with open(translations_path, "w", encoding="utf-8") as f:
                json.dump(strings_content, f, indent=2)
                f.write("\n")
            print_success("Updated translations/en.json")
        else:
            print_success("translations/en.json matches strings.json")

        return True
    except Exception as e:
        print_error(f"Failed to verify translations: {e}")
        return False


def validate_release_preconditions(version: str, repo_root: Path) -> None:
    """Validate all preconditions for creating a release."""
    # Validate version format
    if not validate_version(version):
        print_error(
            f"Invalid version format: {version}. " "Must be X.Y.Z or X.Y.Z-beta.N " "(e.g., 3.5.1, 3.5.1-beta.1)"
        )
        sys.exit(1)

    # Check if pre-commit is installed
    try:
        result = subprocess.run(
            ["git", "config", "--get", "core.hooksPath"],
            capture_output=True,
            text=True,
            check=False,
        )
        hooks_path = result.stdout.strip() or ".git/hooks"
        pre_commit_hook = (
            repo_root / hooks_path / "pre-commit" if not hooks_path.startswith("/") else Path(hooks_path) / "pre-commit"
        )

        if not pre_commit_hook.exists():
            print_warning("Pre-commit hooks not installed!")
            print_warning("This may cause CI failures due to formatting/linting issues.")
            print_warning("Install with: .venv/bin/pre-commit install")
            print_warning("Continuing anyway...")
    except Exception as e:
        print_warning(f"Could not check pre-commit installation: {e}")

    # Check git status
    if not check_git_clean():
        print_error("Git working directory is not clean. Commit or stash changes first.")
        sys.exit(1)

    # Check if version tag already exists
    try:
        result = subprocess.run(
            ["git", "tag", "-l", f"v{version}"],
            capture_output=True,
            text=True,
            check=True,
        )
        if result.stdout.strip():
            print_error(f"Tag v{version} already exists!")
            sys.exit(1)
    except subprocess.CalledProcessError:
        pass


def update_all_files(repo_root: Path, version: str, skip_changelog: bool) -> None:
    """Update all version files."""
    success = True
    success = update_manifest(repo_root, version) and success
    success = update_manifest_requirements(repo_root, version) and success
    success = update_const_py(repo_root, version) and success
    success = update_version_test(repo_root, version) and success
    success = update_package_versions(repo_root, version) and success
    success = update_catalog_core_dependency(repo_root, version) and success

    if not skip_changelog:
        success = update_changelog(repo_root, version) and success

    if not success:
        print_error("Failed to update all files")
        sys.exit(1)


def _check_file_contains(path: Path, label: str, needle: str) -> bool:
    """Check that a file contains an expected string."""
    try:
        content = path.read_text(encoding="utf-8")
        if needle not in content:
            print_error(f"{label} version mismatch: expected '{needle}'")
            return False
        print_success(f"{label} version correct")
        return True
    except Exception as e:
        print_error(f"Failed to read {label}: {e}")
        return False


def _verify_dependency_pins(pkg: Path, version: str) -> bool:
    """Check cross-package dependency pins in pyproject files.

    Catalog pins Core with `==`. Catalog Tools is a repo-only internal
    package and uses `>=` ranges instead — not checked here.
    """
    core_pin = f'"solentlabs-cable-modem-monitor-core=={version}"'
    return _check_file_contains(
        pkg / "cable_modem_monitor_catalog" / "pyproject.toml",
        "catalog→core dependency",
        core_pin,
    )


def verify_version_consistency(repo_root: Path, version: str) -> bool:
    """Verify that all version files have been updated correctly.

    This prevents the CI error where tag version doesn't match manifest.json.
    """
    import json

    print_info("Verifying version consistency across all files...")

    all_correct = True

    # Check manifest.json (JSON structure, not substring)
    manifest_path = repo_root / "custom_components" / "cable_modem_monitor" / "manifest.json"
    manifest = None
    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
            manifest_version = manifest.get("version", "")
            if manifest_version != version:
                print_error(f"manifest.json version mismatch: expected {version}, got {manifest_version}")
                all_correct = False
            else:
                print_success("manifest.json version correct")
    except Exception as e:
        print_error(f"Failed to read manifest.json: {e}")
        all_correct = False

    # Check manifest requirements pins
    if manifest is not None:
        for pkg_name in [
            "solentlabs-cable-modem-monitor-core",
            "solentlabs-cable-modem-monitor-catalog",
        ]:
            expected_pin = f"{pkg_name}=={version}"
            if expected_pin not in manifest.get("requirements", []):
                print_error(f"manifest.json requirements missing: {expected_pin}")
                all_correct = False
            else:
                print_success(f"manifest.json requirement pin correct: {pkg_name}")

    # Check text-based version files
    cc = repo_root / "custom_components" / "cable_modem_monitor"
    pkg = repo_root / "packages"
    ver_assert = f'assert VERSION == "{version}"'
    ver_field = f'version = "{version}"'
    checks = [
        (cc / "const.py", "const.py", f'VERSION = "{version}"'),
        (repo_root / "tests" / "components" / "test_version_and_startup.py", "version test", ver_assert),
        (pkg / "cable_modem_monitor_core" / "pyproject.toml", "core/pyproject.toml", ver_field),
        (pkg / "cable_modem_monitor_catalog" / "pyproject.toml", "catalog/pyproject.toml", ver_field),
    ]
    for path, label, needle in checks:
        if not _check_file_contains(path, label, needle):
            all_correct = False

    # Check cross-package dependency pins
    if not _verify_dependency_pins(pkg, version):
        all_correct = False

    if all_correct:
        print_success("All version files are consistent!")
        return True
    print_error("Version consistency check failed!")
    print_error("This would cause CI to fail with: 'Tag version does not match manifest.json version'")
    return False


def _run_release(args: argparse.Namespace, repo_root: Path) -> None:
    """Execute the release workflow steps."""
    version = args.version

    print_info(f"Starting release process for version {version}")
    print_info(f"Repository root: {repo_root}")

    # Validate preconditions
    validate_release_preconditions(version, repo_root)

    # Full CI mirror against the pre-bump tree; rewritten files re-checked below
    _exit_on_failure(run_validate_ci(repo_root))

    # Verify translations
    _exit_on_failure(verify_translations(repo_root))

    # Update all version files
    update_all_files(repo_root, version, args.skip_changelog)

    # Regenerate catalog README from modem.yaml files
    _generate_catalog_index(repo_root)

    # Re-check what was just rewritten; validate-ci above saw the pre-bump tree
    _exit_on_failure(revalidate_rewritten_files(repo_root))

    # Verify version consistency (prevents CI tag/manifest mismatch error)
    _exit_on_failure(verify_version_consistency(repo_root, version))

    # Show what changed — developer stages and commits
    show_changed_files(version)

    print_success(f"\nVersion {version} prepared. Review, stage, and commit.")


def _exit_on_failure(success: bool) -> None:
    """Exit if the step failed."""
    if not success:
        sys.exit(1)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Automate Cable Modem Monitor releases")
    parser.add_argument("version", nargs="?", help="Version to release (e.g., 3.5.1)")
    parser.add_argument(
        "--skip-changelog",
        action="store_true",
        help="Skip updating CHANGELOG.md",
    )

    args = parser.parse_args()
    repo_root = get_repo_root()

    # Release mode requires version
    if not args.version:
        parser.error("version is required for release")

    _run_release(args, repo_root)


if __name__ == "__main__":
    main()
