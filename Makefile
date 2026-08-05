.PHONY: help setup test test-quick test-simple clean lint lint-fix fix-imports lint-all type-check format format-check check validate validate-ci validate-host intake-regression catalog-field-sweep pii-check spell-check catalog-readme-check suppression-check ha-compat-check install-hooks docker-start docker-stop docker-restart docker-logs docker-status docker-clean docker-shell

# Pin tool invocations to the project venv so that subprocesses
# without venv on PATH (release.py shelling out, fresh clones, CI
# subshells) get the project-pinned versions instead of whatever
# the system happens to have. The `test:` target already venv-
# resolves via scripts/dev/run_tests_local.sh; this keeps lint,
# type-check, and format consistent with that.
VENV_BIN := .venv/bin

# Default target - show help
help:
	@echo "Cable Modem Monitor - Available Commands"
	@echo ""
	@echo "Development:"
	@echo "  make setup       - Create or repair the dev environment (venv + deps)"
	@echo "  make test        - Run full test suite with coverage (creates venv)"
	@echo "  make test-quick  - Quick test run (assumes venv exists)"
	@echo "  make test-simple - Simple test without venv (global install)"
	@echo "  make clean       - Remove test artifacts and cache files"
	@echo ""
	@echo "Code Quality:"
	@echo "  make lint         - Run ruff linter"
	@echo "  make lint-fix     - Run ruff linter and auto-fix issues"
	@echo "  make fix-imports  - Fix import sorting with Ruff"
	@echo "  make lint-all     - Run all linters (ruff, mypy, security)"
	@echo "  make type-check   - Run mypy type checker"
	@echo "  make format       - Format code with black"
	@echo "  make format-check - Check code formatting without modifying"
	@echo "  make check        - Run all code quality checks (lint, format, type)"
	@echo "  make quick-check  - Quick checks (lint + format, skip type-check)"
	@echo "  make validate-host - Cross-platform validation (auto-installs tools)"
	@echo "  make validate-ci   - Full CI-like validation (lint + tests + ha-compat)"
	@echo "  make spell-check   - Spell check catalog modem YAML files (requires Node.js)"
	@echo "  make install-hooks - Install optional pre-push hook (runs validate-ci)"
	@echo ""
	@echo "Catalog Reports (informational, never gates):"
	@echo "  make intake-regression    - How well intake reproduces committed configs"
	@echo "  make catalog-field-sweep  - Captured registry fields no entry maps"
	@echo ""
	@echo "Docker Development:"
	@echo "  make docker-start   - Start Home Assistant dev environment"
	@echo "  make docker-stop    - Stop the dev environment"
	@echo "  make docker-restart - Restart the dev environment"
	@echo "  make docker-logs    - Show container logs (follow mode)"
	@echo "  make docker-status  - Show container status"
	@echo "  make docker-shell   - Open a shell in the container"
	@echo "  make docker-clean   - Remove container and all test data"
	@echo ""
	@echo "Maintenance:"
	@echo "For more details, see scripts/README.md"

# Create or repair the dev environment (venv + all dependencies)
setup:
	@bash scripts/dev/setup_env.sh

# Run full test suite with coverage
test:
	@bash scripts/dev/run_tests_local.sh

# Quick test (assumes venv setup)
test-quick:
	@bash scripts/dev/quick_test.sh

# Simple test without venv
test-simple:
	@bash scripts/dev/test_simple.sh

# Clean test artifacts
clean:
	@python3 scripts/dev/cleanup_test_artifacts.py

# Run linter
lint:
	@echo "Running Ruff linter..."
	@$(VENV_BIN)/ruff check .

# Run linter with auto-fix
lint-fix:
	@echo "Running Ruff linter with auto-fix..."
	@$(VENV_BIN)/ruff check --fix .

# Type checking
type-check:
	@echo "Running mypy type checker..."
	@$(VENV_BIN)/mypy .

# Format code
format:
	@echo "Formatting code with Black..."
	@$(VENV_BIN)/black .

# Check code formatting without modifying
format-check:
	@echo "Checking code formatting..."
	@$(VENV_BIN)/black --check .

# Run all code quality checks
check: lint format-check type-check
	@echo "✅ All code quality checks passed!"

# Quick check (lint + format only, skip type-check for speed)
quick-check: lint format-check
	@echo "✅ Quick quality checks passed!"

# Run all linters (comprehensive)
lint-all: lint type-check
	@echo "Running security linting..."
	@if command -v bandit >/dev/null 2>&1; then \
		bandit -c .bandit -r . ; \
	else \
		echo "⚠️  Bandit not installed. Install with: pip install -r requirements-security.txt"; \
	fi
	@echo "✅ All linting checks completed!"

# Quick pre-commit validation (fast) - requires venv
validate:
	@echo "🔍 Running quick validation..."
	@$(MAKE) quick-check
	@$(MAKE) test-quick
	@echo "✅ Validation passed! Safe to commit."

# Full CI validation (thorough) - requires venv.
# Mirrors the CI Tests workflow surface so a green local run guarantees
# a green CI run. Skipped vs. CI: HACS hassfest (uses an external
# GitHub Action) and version-check (release.py covers that separately).
# Skipped vs. CI: HACS validation (.github/workflows/validate.yaml uses
# hacs/action@main, which runs in a GitHub-hosted Docker context with
# external network checks against home-assistant/brands and HACS APIs;
# not reasonably reproducible locally — same exception class as hassfest).
validate-ci: check test intake-regression pii-check spell-check catalog-readme-check suppression-check ha-compat-check autoclose-check link-check
	@echo "✅ Full CI validation passed!"
	@echo "🔍 Checking declared dependencies for available updates..."
	@$(VENV_BIN)/python scripts/check_owned_deps.py

# Intake pipeline accuracy report — mirrors CI test-packages step.
# Computes fleet onboarding accuracy fresh from the catalog every run
# (report, not a gate). Trend is tracked via the timestamped scorecard
# artifact in CI; per-modem parse correctness is gated by the golden
# replay tests.
intake-regression:
	@echo "🔍 Running intake pipeline accuracy report..."
	@$(VENV_BIN)/python packages/cable_modem_monitor_catalog_tools/scripts/intake_pipeline_regression.py

# Catalog field sweep — registry fields a committed HAR carries that the
# entry never maps. Deliberately NOT a validate-ci dependency and not
# mirrored in CI: the finding set only moves when a HAR, a parser.yaml,
# or the registry vocabulary changes, and every hit needs a human
# decision. Run it on catalog change and before a release cut.
catalog-field-sweep:
	@echo "🔍 Sweeping the catalog for captured-but-unmapped registry fields..."
	@$(VENV_BIN)/python packages/cable_modem_monitor_catalog_tools/scripts/catalog_field_sweep.py

# Fixture PII / credential scan — mirrors CI pii-check job.
pii-check:
	@echo "🔍 Scanning fixtures for PII..."
	@$(VENV_BIN)/python packages/cable_modem_monitor_catalog/scripts/check_fixture_pii.py

# Spell check for catalog modem files — mirrors CI spell-check job. Requires Node.js (npx).
# Scoped to catalog modem YAML; broader codebase (Python, docs) not yet audited.
spell-check:
	@echo "🔤 Running spell check on catalog modem files..."
	@npx --yes cspell@10 --config cspell.config.yaml \
		"packages/cable_modem_monitor_catalog/solentlabs/cable_modem_monitor_catalog/modems/**/*.yaml" \
		--no-progress

# Suppression-discipline scan — mirrors CI suppression-check job.
# Scans every commit on this branch since origin/main for unjustified
# `# type: ignore` / `# pyright: ignore` / bare `# noqa` patterns.
# Matches CI's diff scope (--branch origin/main) so local validation
# catches what CI would. See CLAUDE.md § Code Discipline.
suppression-check:
	@echo "🔍 Scanning for unjustified suppressions..."
	@$(VENV_BIN)/python scripts/check_suppression_discipline.py --branch origin/main

# HA dependency compatibility — mirrors CI ha-compat-check job.
# Validates that Core/Catalog declared dep floors are satisfiable under
# HA's package_constraints.txt. Catches cases where a deps-bump sets a
# floor above what HA pins (e.g., requests or pyyaml). Exit non-zero = gate.
ha-compat-check:
	@echo "🔍 Checking Core/Catalog deps against HA package constraints..."
	@$(VENV_BIN)/python scripts/check_ha_compat.py

# Catalog README + audit freshness — mirrors CI catalog-readme job.
catalog-readme-check:
	@echo "🔍 Checking catalog README is up to date..."
	@$(VENV_BIN)/python packages/cable_modem_monitor_catalog/scripts/generate_catalog_index.py --print > /tmp/catalog_readme.md
	@if ! diff -q packages/cable_modem_monitor_catalog/README.md /tmp/catalog_readme.md > /dev/null 2>&1; then \
		echo "❌ Catalog README is out of date."; \
		echo "   Run: python packages/cable_modem_monitor_catalog/scripts/generate_catalog_index.py"; \
		diff --unified packages/cable_modem_monitor_catalog/README.md /tmp/catalog_readme.md || true; \
		exit 1; \
	fi
	@echo "✅ Catalog README is up to date"
	@echo "🔍 Checking catalog audit is up to date..."
	@$(VENV_BIN)/python packages/cable_modem_monitor_catalog/scripts/generate_catalog_index.py --print-audit > /tmp/catalog_audit.md
	@if ! diff -q packages/cable_modem_monitor_catalog/CATALOG_AUDIT.md /tmp/catalog_audit.md > /dev/null 2>&1; then \
		echo "❌ Catalog audit is out of date."; \
		echo "   Run: python packages/cable_modem_monitor_catalog/scripts/generate_catalog_index.py"; \
		diff --unified packages/cable_modem_monitor_catalog/CATALOG_AUDIT.md /tmp/catalog_audit.md || true; \
		exit 1; \
	fi
	@echo "✅ Catalog audit is up to date"

# Auto-close keyword scan — mirrors CI autoclose-check job. Scans commit
# bodies on this branch (origin/main..HEAD) for GitHub auto-close
# keywords plus an issue ref, which would close issues on merge. See
# CLAUDE.md § PR and Issue Conventions.
autoclose-check:
	@echo "🔍 Scanning commit bodies for auto-close keywords..."
	@$(VENV_BIN)/python scripts/check_auto_close_keywords.py --base origin/main

# Markdown link check — mirrors CI link-check job. Validates that intra-repo
# relative and repo-absolute links resolve, and that the HACS-rendered root
# README uses absolute URLs. Offline and deterministic. See CLAUDE.md
# § Two READMEs — GitHub vs HACS.
link-check:
	@echo "🔗 Checking intra-repo Markdown links..."
	@$(VENV_BIN)/python scripts/check_markdown_links.py

# Install optional pre-push hook that runs `make validate-ci` before push.
# Opt-in per developer — CI is the authoritative gate. To bypass once:
#   git push --no-verify
install-hooks:
	@HOOKS_DIR="$$(git rev-parse --git-path hooks)"; \
	cp scripts/hooks/pre-push "$$HOOKS_DIR/pre-push"; \
	chmod +x "$$HOOKS_DIR/pre-push"; \
	echo "✅ Installed pre-push hook → $$HOOKS_DIR/pre-push"

# Cross-platform validation (auto-installs tools, works without venv)
validate-host:
	@python scripts/dev/validate.py

# Docker development environment
docker-start:
	@python3 scripts/dev/ha-sync-run.py

docker-stop:
	@docker compose -f docker-compose.test.yml down

docker-restart:
	@docker restart ha-cable-modem-test

docker-logs:
	@docker logs -f ha-cable-modem-test

docker-status:
	@docker ps -a --filter name=ha-cable-modem-test

docker-shell:
	@docker exec -it ha-cable-modem-test bash

docker-clean:
	@docker compose -f docker-compose.test.yml down -v && echo "Volumes removed"
