---
name: ha-safe-deps-bump
description: Bump drifted dependencies to latest while holding HA-constrained runtime deps within Home Assistant's package_constraints. Use when check_owned_deps or the validate-ci footer reports outdated declared dependencies.
---

<!-- Master copy: skills/ha-safe-deps-bump.md — edit there, not in .claude/skills/ -->

# HA-Safe Dependency Bump Skill

> **Invocation note**: Project-local skills in `skills/` are not registered as Skill tool
> targets — `Skill("ha-safe-deps-bump")` will return "Unknown skill". Read this file and
> execute the steps directly. This is a Claude Code limitation, not a config gap.

Update declared dependencies that have drifted behind their latest
releases, **without** raising a floor past what Home Assistant allows.
Version drift is an expected part of the lifecycle and we do update it,
but the runtime deps HA also installs are pegged: pushing a floor past
HA's `package_constraints.txt` fails `ha-compat-check` in CI and breaks
the HACS/HA install on deploy (the beta.4 incident: `requests` and
`pyyaml` exceeded HA's pins).

## When to Use

- The `make validate-ci` footer or `scripts/check_owned_deps.py` reports
  declared dependencies with newer versions available.
- A periodic lifecycle dependency refresh.
- NOT for security patches — Dependabot is scoped to security-only and
  handles those (see the dependabot strategy). This skill is the
  non-security lifecycle bump.

## Key Rule — what may move and what may not

Categorize each drifted package by **where it is declared**:

- **HA-constrained → HOLD by default.** Third-party runtime deps in the
  *published* packages' `[project].dependencies`:
  - `packages/cable_modem_monitor_core/pyproject.toml`
  - `packages/cable_modem_monitor_catalog/pyproject.toml`

  These ship to PyPI and HA installs them. Their floors are deliberately
  conservative for compatibility breadth. Do not raise a floor just
  because a newer release exists — only on a deliberate need, and only
  within HA's ceiling (proven by `ha-compat-check`).
- **Free → BUMP.** Everything HA never installs:
  - `requirements-dev.txt`, `requirements-security.txt`, `tests/requirements.txt`
  - root `pyproject.toml` (dev / optional extras)
  - `packages/cable_modem_monitor_catalog_tools/pyproject.toml` (never installed by HA)

## Workflow

### 1. Survey the drift

```bash
.venv/bin/python scripts/check_owned_deps.py
# same source data, machine-readable:
.venv/bin/python -m pip list --outdated --format=json
```

Build the list of `(package, installed, latest)` for our declared deps.

### 2. Split into free vs HA-constrained

Grep each drifted package name to find its declaration file and bucket it
per the Key Rule. If a package is declared in both a published
`pyproject.toml` runtime list and a dev file, treat it as HA-constrained.

### 3. Decide whether each free package is even a file change

Compare the **declared spec** (not the installed version) to the latest.
The drift report (`pip list --outdated`) measures the local venv, not
our declarations, so most "drift" on this floor-based project is just a
stale venv:

- Floor already permits the latest (e.g. `black>=26.5.1`, latest
  `26.5.1`) → **nothing to commit.** CI and users install fresh and
  already get the latest; only the local `.venv` is behind. Sync it with
  `pip install -U <pkg>` if desired — no tracked change.
- Spec **excludes** the latest — a cap (`~=`, `<`) or pin (`==`) → this
  is the only case that needs a file edit, and caps are usually
  deliberate (`har-capture~=0.9.0`, `types-requests<2.33.0`). Confirm the
  reason in the nearby comment before touching it.

The build is the gate: notify when old, update only if it stays green.
Floored deps already track latest in CI, so they are effectively updated
on every run; the local action is `pip install -U` to match. For held
caps and pins, periodically try moving the spec forward and adopt the
new value only if `validate-ci` (and `ha-compat-check` for runtime deps)
stays green. Take major-version jumps one at a time (e.g. `pytest` 8→9);
several pins are governed by `pytest-homeassistant-custom-component`,
not us, and stay where the harness puts them.

### 4. Verify, and back off breakage

```bash
make validate-ci
```

- Green → keep the batch.
- Red → the bump itself broke the build (new `black`/`ruff` reformat or
  rule, a `pytest` major, a changed lint default). Isolate the offender:
  revert that one package, re-run, and record it as **HELD** with the
  reason. Never force a broken bump through.

### 5. HA-constrained set (report by default)

List the held HA-constrained deps with their current floor. If a bump is
genuinely wanted, raise the floor and prove it stays inside HA's ceiling:

```bash
make ha-compat-check
```

Keep only if green; revert if red. Default outcome is "held, newer
available, floor intentionally conservative."

### 6. Commit

A standalone `chore(deps)` commit, never folded into feature work. Body
carries a summary table:

```text
| Package | Old | New | Action | Reason |
|---------|-----|-----|--------|--------|
| black   | 26.3.1 | 26.5.1 | bumped | dev tooling |
| requests| 2.32.3 | (2.34.2) | held | HA-constrained floor |
```

Then the normal flow: show changed files, let the developer stage,
commit, and `make validate-ci` before push.

## Notes

- The HA-compat policy is CLAUDE.md § Pre-Push Verification;
  `scripts/check_ha_compat.py` / `make ha-compat-check` enforce that every
  declared floor is satisfiable under HA's `package_constraints.txt`.
- `scripts/check_owned_deps.py` reports only packages we declare directly,
  not the transitive HA / test-harness tree.
- The riskiest bumps are the formatters and test runners (`black`, `ruff`,
  `pytest*`), because a new version changes what passes `validate-ci`, not
  what HA allows. Those are exactly where step 4's back-off matters.
