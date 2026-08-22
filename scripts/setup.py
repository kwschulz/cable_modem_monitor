#!/usr/bin/env python3
# Cable Modem Monitor - Automated Development Environment Setup (Python)
# This script consolidates the setup logic for all platforms.

import os
import platform
import re
import shutil
import subprocess
import sys

# ========================================
# Platform-agnostic Console Output
# ========================================
# Use plain ASCII for maximum compatibility across all platforms
# ANSI color codes work on Windows 10+, macOS, Linux, Chrome OS

# ANSI color codes (work on all modern terminals)
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
CYAN = "\033[0;36m"
NC = "\033[0m"  # No Color


def print_step(message):
    """Print step message with arrow (ASCII-only)."""
    print(f"{CYAN}>{NC} {message}")


def print_success(message):
    """Print success message with checkmark (ASCII-only)."""
    print(f"{GREEN}OK{NC} {message}")


def print_error(message):
    """Print error message with X (ASCII-only)."""
    print(f"{RED}X{NC} {message}", file=sys.stderr)


def print_warning(message):
    """Print warning message with ! (ASCII-only)."""
    print(f"{YELLOW}!{NC} {message}")


def run_command(command, quiet=False):
    """Runs a command and returns its output."""
    try:
        process = subprocess.run(
            command,
            shell=True,
            check=True,
            capture_output=True,
            text=True,
        )
        if not quiet:
            print(process.stdout)
        return process.stdout.strip()
    except subprocess.CalledProcessError as e:
        if not quiet:
            print_error(f"Command failed: {command}")
            print(e.stderr, file=sys.stderr)
        raise


def is_privacy_safe_email(email: str) -> bool:
    """Check if an email is a privacy-safe (noreply) address."""
    if not email:
        return False
    email = email.lower()
    return (
        email.endswith("@users.noreply.github.com") or email == "noreply@anthropic.com" or email == "noreply@github.com"
    )


def get_required_python_version():
    """Parse pyproject.toml to get the required Python version."""
    if not os.path.exists("pyproject.toml"):
        return (3, 11)  # Default fallback

    with open("pyproject.toml") as f:
        content = f.read()

    # Match requires-python = ">=3.12" or ">=3.12,<3.13" etc.
    match = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)', content)
    if match:
        return (int(match.group(1)), int(match.group(2)))
    return (3, 11)  # Default fallback


def get_debian_version():
    """Get Debian/Ubuntu version info if applicable."""
    try:
        with open("/etc/os-release") as f:
            content = f.read()
        version_match = re.search(r"VERSION_CODENAME=(\w+)", content)
        id_match = re.search(r"^ID=(\w+)", content, re.MULTILINE)
        if version_match and id_match:
            return (id_match.group(1), version_match.group(1))
    except FileNotFoundError:
        pass
    return (None, None)


def print_python_install_instructions(required_major, required_minor):
    """Print platform-specific Python installation instructions."""
    distro, codename = get_debian_version()
    is_chromeos = os.path.exists("/dev/.cros_milestone") or "chromeos" in platform.platform().lower()

    print("")
    print(f"  Python {required_major}.{required_minor} is required but not installed.")
    print("")

    if distro == "debian" and codename == "bookworm":
        print("  Debian 12 (bookworm) ships with Python 3.11. To install Python 3.12:")
        print("")
        print("  Option 1: Use pyenv (recommended)")
        print("    curl https://pyenv.run | bash")
        print("    # Follow the instructions to add pyenv to your shell")
        print("    pyenv install 3.12")
        print("    pyenv local 3.12")
        print("")
        print("  Option 2: Build from source")
        print("    sudo apt install build-essential libssl-dev zlib1g-dev \\")
        print("      libbz2-dev libreadline-dev libsqlite3-dev libffi-dev \\")
        print("      liblzma-dev libncurses-dev")
        print("    wget https://www.python.org/ftp/python/3.12.0/Python-3.12.0.tgz")
        print("    tar xzf Python-3.12.0.tgz && cd Python-3.12.0")
        print("    ./configure --enable-optimizations")
        print("    make -j$(nproc) && sudo make altinstall")
        print("")
        if is_chromeos:
            print("  Note: Chrome OS Flex uses Debian bookworm in Crostini.")
            print("  pyenv works well in this environment.")
            print("")
    elif distro == "ubuntu":
        print("  Ubuntu - install from deadsnakes PPA:")
        print("")
        print("    sudo apt install software-properties-common")
        print("    sudo add-apt-repository ppa:deadsnakes/ppa")
        print("    sudo apt update")
        py_ver = f"{required_major}.{required_minor}"
        print(f"    sudo apt install python{py_ver} python{py_ver}-venv")
        print("")
    elif platform.system() == "Darwin":
        print("  macOS - install with Homebrew:")
        print("")
        print(f"    brew install python@{required_major}.{required_minor}")
        print("")
    elif platform.system() == "Windows":
        print("  Windows - use WSL2 for development (native Windows not supported)")
        print("")
        print("    See docs/setup/GETTING_STARTED.md for instructions")
        print("")
    else:
        print("  Install Python 3.12 using your system's package manager or pyenv:")
        print("")
        print("    curl https://pyenv.run | bash")
        print("    pyenv install 3.12")
        print("")


def configure_git_email_privacy():
    """Check and configure git email for privacy protection."""
    try:
        current_email = run_command("git config user.email", quiet=True)
    except Exception:
        current_email = ""

    if is_privacy_safe_email(current_email):
        print_success(f"Git email is privacy-safe: {current_email}")
        return

    if current_email:
        print_warning(f"Personal email detected: {current_email}")
    else:
        print_warning("No git email configured")

    print("")
    print("  To protect your privacy, this project recommends using GitHub's noreply email.")
    print("  Personal emails in git history are permanently public.")
    print("")
    print("  Setup instructions:")
    print("    1. Go to: https://github.com/settings/emails")
    print("    2. Check 'Keep my email addresses private'")
    print("    3. Copy your noreply email (e.g., 12345+user@users.noreply.github.com)")
    print("")

    # Try to prompt for email (works in interactive terminals)
    try:
        new_email = input("  Paste your GitHub noreply email (or press Enter to skip): ").strip()
        if new_email:
            if is_privacy_safe_email(new_email):
                run_command(f'git config user.email "{new_email}"', quiet=True)
                print_success(f"Git email configured: {new_email}")
            else:
                print_warning("That doesn't look like a noreply email, skipping")
                print("  You can configure it later with: ./scripts/dev/setup-git-email.sh")
        else:
            print_warning("Skipped - you can configure later with: ./scripts/dev/setup-git-email.sh")
    except (EOFError, KeyboardInterrupt):
        # Non-interactive mode (CI, piped input, etc.)
        print_warning("Non-interactive mode - configure later with: ./scripts/dev/setup-git-email.sh")


# ========================================
# Main Setup Logic
# ========================================
def main():  # noqa: C901
    print("")
    print("==========================================")
    print("Cable Modem Monitor - Development Setup")
    print("==========================================")
    print("")

    # 1. Check if in project root
    print_step("Checking if in project root directory...")
    if not os.path.exists("custom_components/cable_modem_monitor/__init__.py"):
        print_error("Not in project root directory")
        print("\nPlease run this script from the cable_modem_monitor/ directory:")
        print("  cd /path/to/cable_modem_monitor")
        print("  python3 scripts/setup.py\n")
        sys.exit(1)
    print_success("Running from project root")
    print("")

    # 2. Check Python version against pyproject.toml requirement
    print_step("Checking Python version...")
    current_major, current_minor = sys.version_info[:2]
    required_major, required_minor = get_required_python_version()

    if current_major == required_major and current_minor >= required_minor:
        print_success(f"Python {current_major}.{current_minor} found (requirement: {required_major}.{required_minor})")
    else:
        print_error(f"Python {current_major}.{current_minor} found, but {required_major}.{required_minor} required")
        print_python_install_instructions(required_major, required_minor)
        sys.exit(1)
    print("")

    # 3. Check for venv module
    print_step("Checking for venv module...")
    import importlib.util

    if importlib.util.find_spec("venv") is not None:
        print_success("venv module available")
    else:
        print_error("venv module not installed")
        print("\nInstall it with:")
        print(f"  Linux/macOS: sudo apt install python{current_major}.{current_minor}-venv")
        print("  Windows: venv is included with Python\n")
        sys.exit(1)
    print("")

    # 4. Clean up old venv
    if os.path.isdir("venv"):
        print_warning("Found venv/ directory, removing it in favor of .venv/")
        shutil.rmtree("venv")
        print_success("Removed venv/ directory")
        print("")

    # 5. Create virtual environment
    pip_cmd = os.path.join(".venv", "bin", "pip")
    precommit_cmd = os.path.join(".venv", "bin", "pre-commit")
    python_cmd = os.path.join(".venv", "bin", "python")

    print_step("Creating virtual environment...")
    if os.path.exists(".venv") and os.path.exists(pip_cmd):
        print_success("Virtual environment already exists")
    elif os.path.exists(".venv"):
        print_warning("Virtual environment is incomplete, recreating...")
        try:
            shutil.rmtree(".venv")
            print_success("Removed incomplete venv")
        except OSError:
            print_error("Cannot remove .venv (files are locked)")
            print("")
            print("This happens when VS Code or another process is using the venv.")
            print("")
            print("Solutions:")
            print("  1. Close VS Code")
            print("  2. Run: rm -rf .venv")
            print("  3. Run this setup again")
            print("")
            sys.exit(1)
        run_command(f"{sys.executable} -m venv .venv")
        print_success("Virtual environment created")
    else:
        run_command(f"{sys.executable} -m venv .venv")
        print_success("Virtual environment created")
    print("")

    # 6. Upgrade pip
    print_step("Upgrading pip...")
    try:
        run_command(f"{pip_cmd} install --upgrade pip", quiet=True)
        print_success("pip ready")
    except Exception:
        print_warning("pip upgrade skipped (will work next run)")
    print("")

    # 7. Install dependencies
    print_step("Installing development dependencies...")
    print("  (This may take a few minutes...)")
    if os.path.exists("requirements-dev.txt"):
        run_command(f"{pip_cmd} install --quiet -r requirements-dev.txt")
        print_success("Development dependencies installed from requirements-dev.txt")
    else:
        print_warning("requirements-dev.txt not found, using fallback installation")
        # Install packages manually (less ideal)
        packages = [
            "homeassistant>=2024.1.0",
            "beautifulsoup4",
            "lxml",
            "pytest",
            "pytest-cov",
            "pytest-asyncio",
            "pytest-mock",
            "pytest-homeassistant-custom-component",
            "ruff",
            "black",
            "pre-commit",
            "pylint",
            "mypy",
            "types-requests",
            "bandit",
            "defusedxml",
            "freezegun",
            "responses",
            "pytest-socket",
        ]
        run_command(f"{pip_cmd} install --quiet {' '.join(packages)}")
        run_command(f"{pip_cmd} install --quiet --upgrade requests aiohttp")
        print_success("Development dependencies installed")
    print("")

    # 8. Install the local packages editable
    # Without these, `solentlabs.*` imports resolve to whatever pip pulled
    # from PyPI instead of this checkout, so tests run against the published
    # release and fail in ways that look like broken code. setup_env.sh
    # (`make setup`) does the same three installs; keep both paths in step.
    print_step("Installing solentlabs packages (editable)...")
    for package in (
        "packages/cable_modem_monitor_core",
        "packages/cable_modem_monitor_catalog",
        "packages/cable_modem_monitor_catalog_tools",
    ):
        run_command(f"{pip_cmd} install --quiet -e {package}")
    print_success("solentlabs packages installed")
    print("")

    # 9. Install Playwright for HAR capture (optional)
    print_step("Installing Playwright for modem traffic capture...")
    try:
        run_command(f"{pip_cmd} install --quiet playwright", quiet=True)
        # Use Firefox - only needs libasound2 vs Chromium's 14+ deps
        run_command(f"{python_cmd} -m playwright install firefox", quiet=True)
        print_success("Playwright and Firefox browser installed")

        # Install browser system dependencies (requires sudo on Linux)
        if platform.system() == "Linux":
            print_step("Installing browser dependency (libasound2)...")
            try:
                # Firefox only needs libasound2 - much simpler than Chromium
                result = subprocess.run(
                    ["sudo", "apt-get", "install", "-y", "libasound2t64"],
                    capture_output=False,
                )
                if result.returncode == 0:
                    print_success("Browser dependency installed")
                else:
                    print_warning("Run manually: sudo apt-get install -y libasound2t64")
            except Exception:
                print_warning("Run manually: sudo apt-get install -y libasound2t64")
    except Exception as e:
        print_warning(f"Playwright installation skipped: {e}")
        print("  Run manually: pip install playwright && playwright install firefox")
    print("")

    # 10. Install pre-commit hooks
    print_step("Setting up pre-commit hooks...")
    try:
        run_command(f"{pip_cmd} show pre-commit", quiet=True)
        # Install pre-commit, commit-msg, and pre-push hooks
        # pre-commit: format/lint staged files
        # commit-msg: validate commit message format
        # pre-push: full project validation (ruff check . && pytest)
        hook_types = "--hook-type pre-commit --hook-type commit-msg --hook-type pre-push"
        run_command(f"{precommit_cmd} install --install-hooks {hook_types}", quiet=True)
        print_success("Pre-commit hooks installed (pre-commit + commit-msg + pre-push)")
    except Exception as e:
        print_warning(f"Pre-commit hook installation failed: {e}")
        print("  Run manually: pre-commit install --hook-type pre-commit --hook-type commit-msg")
    print("")

    # 11. Check Docker
    print_step("Checking Docker...")
    if shutil.which("docker"):
        try:
            docker_version = run_command("docker --version", quiet=True).split(" ")[2].strip(",")
            run_command("docker ps", quiet=True)
            print_success(f"Docker {docker_version} is running")
        except Exception:
            print_warning("Docker installed but not running")
            print("  Start Docker Desktop to use dev containers")
    else:
        print_warning("Docker not installed")
        print("  Optional: Install Docker Desktop for containerized development")
    print("")

    # 12. Check Git LFS
    print_step("Checking Git LFS...")
    try:
        run_command("git lfs version", quiet=True)
        print_success("Git LFS is installed")

        # Configure LFS diff driver so HAR diffs show actual content
        # instead of opaque LFS pointer changes (oid/size)
        try:
            current_textconv = run_command("git config diff.lfs.textconv", quiet=True)
        except Exception:
            current_textconv = ""
        if current_textconv != "cat":
            run_command("git config diff.lfs.textconv cat", quiet=True)
            print_success("Git LFS diff driver configured (HAR diffs show content)")
        else:
            print_success("Git LFS diff driver already configured")
    except Exception:
        print_warning("Git LFS not installed — HAR test fixtures won't download")
        print("  Install: https://git-lfs.com/")
        print("  Then:    git lfs install && git lfs pull")
    print("")

    # 13. Configure git email privacy
    print_step("Checking git email privacy...")
    configure_git_email_privacy()
    print("")

    # 14. Check VS Code extensions
    print_step("Checking VS Code...")
    if shutil.which("code"):
        try:
            code_version = run_command("code --version", quiet=True).splitlines()[0]
            print_success(f"VS Code {code_version} installed")

            # Check for required extensions
            extensions = run_command("code --list-extensions", quiet=True)
            required_extensions = {
                "ms-python.python": "Python extension",
                "ms-vscode-remote.remote-containers": "Dev Containers extension",
            }

            missing_extensions = []
            for ext_id, _ext_name in required_extensions.items():
                if ext_id not in extensions:
                    missing_extensions.append(ext_id)

            if missing_extensions:
                print_warning("Some recommended VS Code extensions are missing:")
                for ext in missing_extensions:
                    print(f"    - {ext}")
                print("")
                print("  Install them with:")
                for ext in missing_extensions:
                    print(f"    code --install-extension {ext}")
            else:
                print_success("All recommended VS Code extensions installed")
        except Exception:
            print_warning("Could not check VS Code extensions")
    else:
        print_warning("VS Code not installed")
        print("  Optional: Install VS Code for better development experience")
    print("")

    # 15. Run a quick test
    print_step("Running quick test to verify setup...")
    try:
        run_command(f"{python_cmd} -m pytest tests/parsers/netgear/test_cm600.py::test_fixtures_exist -q", quiet=True)
        print_success("Tests can run successfully")
    except Exception:
        print_warning("Test execution had issues (may need additional setup)")
    print("")

    # Final message
    print("==========================================")
    print("Setup Complete!")
    print("==========================================")
    print("")
    print_success("Your development environment is ready!")
    print("")
    print("What's installed:")
    print("  • Python virtual environment (.venv/)")
    print("  • All development dependencies")
    print("  • Pre-commit hooks (format/lint on commit, email privacy check)")
    print("  • Pre-push hooks (full validation before push)")
    print("  • Code formatters and linters")
    print("  • Git email privacy protection")
    print("")
    print("Next steps:")
    print("")
    print(f"  {CYAN}1. Run tests:{NC}")
    print("     make test")
    print("     # or: .venv/bin/pytest tests/")
    print("")
    print(f"  {CYAN}2. Run code quality checks:{NC}")
    print("     make lint")
    print("     make format")
    print("")
    print(f"  {CYAN}3. Start Docker development environment:{NC}")
    print("     make docker-start")
    print("     # Then open http://localhost:8123")
    print("")
    print(f"  {CYAN}4. Open in VS Code:{NC}")
    print("     code .")
    print("     # Press F1 -> 'Dev Containers: Reopen in Container'")
    print("")
    print(f"  {CYAN}5. Verify your setup:{NC}")
    print("     ./scripts/verify-setup.sh")
    print("")
    print("Documentation:")
    print("  • Getting Started: docs/setup/GETTING_STARTED.md")
    print("  • Contributing:    CONTRIBUTING.md")
    print("  • Architecture:    packages/cable_modem_monitor_core/docs/ARCHITECTURE.md")
    print("")
    print("Happy coding! 🚀")
    print("")


if __name__ == "__main__":
    main()
