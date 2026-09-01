# Release Process

This document describes how to create a new release of Cable Modem Monitor.

## Overview

Releases follow a PR-based workflow:

1. **Develop on feature branch** → Run release script (prepares version bump)
2. **Create/update PR** → CI validates, review changes
3. **Merge to main** → Creates clean history
4. **Tag main** → Triggers GitHub release workflow

## Key Rules

- **CI runs on push. Publishing runs on tag push.** Don't skip the tag.
- **Never manually edit version numbers.** Use `scripts/release.py`.
- **The working tree carries the last tag's version until a bump.**
  `release.py` updates the version files only at release-prep time, so
  during development the manifest, startup banner, and downloaded
  diagnostics all report the previous tag's number (e.g. `beta.14`
  while `beta.15` work is in flight, untagged). This is expected. The
  startup banner is still the tell: it appends `(local dev)` whenever
  the `CMM_LOCAL_DEV` env var is set (`docker-compose.test.yml` sets
  it for the local harness), so a dev-tree install self-identifies in
  the log regardless of the version it reports. Whether a *specific*
  change is live is confirmed by behavior, not the number.
- **Never run `gh release create` manually.** Let CI handle it.
- **Dogfood on local HA** before stable releases (maintainer launches,
  not automated).

## Step-by-Step Release

### 1. Prepare the Release (on feature branch)

`make validate-ci` ends with `scripts/check_owned_deps.py`. It is
informational and always exits 0: it compares the local venv against
PyPI, so a package it lists may already be permitted by our declared
floor. Read it, and open a separate deps commit if something we declare
is genuinely behind. It does not gate a release.

```bash
# Ensure you're on your feature branch with all changes committed
git checkout feature/your-branch
git status  # Should be clean

# Run the release script
.venv/bin/python scripts/release.py 3.14.0
```

The release script will:

- Validate version format and clean working directory
- Run tests and code quality checks (pytest, ruff, black, mypy)
- Verify translations are in sync
- Update version in manifest.json, const.py, pyproject.toml files, and test assertions
- Move `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD` in CHANGELOG.md
- Print changed files and suggested next steps

The script does **not** stage, commit, tag, push, or create releases —
the developer handles all git operations.

### 2. Stage, Commit, and Push

```bash
# Review changed files, stage, and commit
git diff
git add <changed-files>
git commit -m "chore: bump version to 3.14.0"

# Push your feature branch
git push origin feature/your-branch

# Create PR if it doesn't exist, or it will update the existing one
gh pr create --title "feat: v3.14.0 - Your Release Title" --body "..."
# Or update existing PR via GitHub UI
```

### 3. Review and Merge

- Wait for CI to pass
- Review the changes, including the PR **description**: `make
  autoclose-check` scans commit bodies only, so a `Fixes #N` in the body
  is the one auto-close vector nothing catches
- Merge with a **merge commit, never a squash**. **Where tags live**
  tags the merge commit, and a squash produces none; a squash also
  rewrites the head SHAs GitHub uses to mark a PR merged, which costs a
  fork contributor their merged badge and authorship
- `require-pr-approval` cannot be satisfied by the author, so this is a
  `gh pr merge --merge --admin` in practice. Confirm every check is
  green first: the admin flag overrides required status checks too, not
  just the review requirement

### 4. Tag Main and Push

After the PR is merged:

Tag the merge commit by SHA. Don't `git checkout main` first: local
`main` never moves in this workflow (see **Where tags live**), so a
checkout-then-tag risks tagging a stale commit, and it disturbs the
working tree for no gain.

```bash
git fetch origin

# The merge commit the PR produced, straight from GitHub
MERGE_SHA=$(gh api repos/solentlabs/cable_modem_monitor/pulls/204 -q .merge_commit_sha)

# Confirm it is what main points at before tagging it
git rev-parse origin/main

git tag -a v3.14.0 "$MERGE_SHA" -m "Cable Modem Monitor v3.14.0

- Highlight 1
- Highlight 2
- See CHANGELOG.md for details"

# Push the tag (triggers release workflow)
git push origin v3.14.0
```

### 5. Verify Release

The tag push triggers `.github/workflows/release.yml` which:

- Creates a GitHub Release, `prerelease` for a beta tag
- Builds the release body from the matching `## [X.Y.Z]` section of
  `CHANGELOG.md`, **not** from the tag message. The tag message is for
  readers of `git`; what users see is the changelog section, so that
  section has to be right before the tag is pushed.
- Attaches the `cable_modem_monitor.zip` asset HACS installs from

Verify at: <https://github.com/solentlabs/cable_modem_monitor/releases>

## Release Tiers

Two tiers ship: beta and stable. Both publish to PyPI and create
GitHub Releases with a zip asset. Beta tags carry the GitHub
`prerelease` flag; stable tags don't.

| Tier   | Tag pattern       | PyPI      | GitHub Release          | HACS visibility                   |
|--------|-------------------|-----------|-------------------------|-----------------------------------|
| Beta   | `v*.*.*-beta.*`   | Published | Pre-release + zip asset | Manual install via version picker |
| Stable | `v*.*.*` (no `-`) | Published | Release + zip asset     | Default (auto-update offer)       |

Betas install manually via HACS → integration → Redownload → "Need
a different version?" → Release. There is no auto-update path on
betas — by design, each beta is a deliberate per-version install.

**Where tags live:**

All tags live on `main`, beta and stable alike. A release is cut on a
short-lived branch off current `main` (`feature/vX.Y.Z-beta.N` for a
beta), opened as a PR, gated by required-status-checks + commitlint,
then merged and tagged on the merge commit. Delete the branch after.

Patch releases may carry a beta when the fix is behavior-changing
enough to want field testing first. Earlier patch lines (3.8.1 through
3.8.6, 3.9.1, 3.9.2, 3.10.2, 3.12.1, 3.13.1) shipped straight to
stable; that is precedent, not a rule.

- `require-pr-approval` can't be self-satisfied (GitHub never counts the
  author's own approval), so the maintainer merges with admin bypass.
- A branch off current `main` can't fall behind. If one drifts, rebase
  it onto `main`, don't cherry-pick.
- **"Current `main`" means `origin/main`, not the local branch.** PRs
  merge on GitHub, and `git fetch` advances only the remote-tracking
  ref, so local `main` never moves unless it is checked out and pulled.
  In this workflow it never is. Cut from the fetched ref:

  ```bash
  git fetch origin
  git checkout -b feature/vX.Y.Z-beta.N origin/main
  ```

  To bring local `main` current without checking it out, run
  `git fetch origin main:main`. It fast-forwards, and refuses rather
  than rewrites if the branch has diverged.

This replaced a long-lived beta-line branch that fell behind `main` and
let commits dodge the PR gates until they surfaced all at once (the
cause of the beta.11 commitlint rewrite and force-push).

### `hacs.json` configuration

```json
{
  "name": "Cable Modem Monitor",
  "render_readme": true,
  "homeassistant": "2024.12.0",
  "hacs": "2.0.0",
  "zip_release": true,
  "filename": "cable_modem_monitor.zip",
  "hide_default_branch": true
}
```

`zip_release: true` and `hide_default_branch: true` are paired. With
both set, HACS distributes only the zip assets attached to GitHub
Releases and hides the default-branch option from the version
selector entirely. Users can't accidentally try to install from a
branch ref, which would 404 (see "Why branch tracking is unsupported"
below).

### Why branch tracking is unsupported

HACS reads `hacs.json` from the **default branch**, not from the
branch a user is tracking. When `zip_release: true` is set on the
default branch, HACS expects a zip asset on every install path
including branch refs. Branch refs have no GitHub Release object, so
HACS constructs a zip URL that returns 404 — by design, not a bug.

Confirmed by HACS maintainer in
[hacs/integration#3513](https://github.com/hacs/integration/issues/3513)
(closed 2024-02-16):

> *"This is correct. It should not be possible. When a repository
> uses release assets (`zip_release`), only those assets are valid."*
> — ludeeus, HACS maintainer
>
> *"Ask the author if you need a development version. If that was
> possible before, that was a bug."*

`hide_default_branch: true` is the maintainer-recommended companion:
HACS hides the branch dropdown from the version selector entirely so
users don't see the broken path.

For developers who do need to run from a branch (rare — typically
only the maintainer testing an unreleased commit), install outside
HACS: clone the repo, symlink `custom_components/cable_modem_monitor`
into the HA config dir, and `pip install -e` the Core and Catalog
packages.

References:

- [HACS docs — Publish: Integration](https://hacs.xyz/docs/publish/integration/) — overall integration publishing requirements
- [HACS docs — Publish: General](https://hacs.xyz/docs/publish/start/) — `zip_release` field documented as "only supported for integrations"
- [hacs/integration#3513](https://github.com/hacs/integration/issues/3513) — definitive answer on `zip_release` + branch tracking incompatibility

### Rollback safety

`zip_release: true` means HACS expects a `cable_modem_monitor.zip`
asset on every release a user might roll back to. Older stable
releases (v3.13.x and earlier) lack this asset because the zip
wasn't built before the beta cut. Without a backfill of those older
releases, rollback to a pre-beta release fails with a HACS
asset-not-found error. Forward-only updates work fine.

## Version Numbering

We follow [Semantic Versioning](https://semver.org/):

- **MAJOR** (X.0.0): Breaking changes
- **MINOR** (0.X.0): New features, backward compatible
- **PATCH** (0.0.X): Bug fixes, backward compatible

### Version pinning in manifest.json

`manifest.json` pins both PyPI packages with exact `==` specifiers.
This is deliberate — HA only checks whether the installed version
satisfies the specifier; it never queries PyPI for newer versions
within a range. A `>=` pin would leave users permanently on the
version installed at first setup. The `==` pin forces HA to upgrade
when the manifest changes.

All three packages are released in lock-step. `scripts/release.py`
bumps all version files atomically. Independent versioning is not
supported — even a catalog-only change (new modem config) triggers a
full release, because the user needs a HACS update (new manifest pin)
to receive the new catalog version.

## Package Architecture

### What ships where

| Package | Delivery | Contents |
|---------|----------|----------|
| HACS zip (`cable_modem_monitor.zip`) | GitHub release asset | HA adapter: config flow, coordinator, sensors, buttons, services, translations, icons |
| Core (`solentlabs-cable-modem-monitor-core`) | PyPI wheel | Auth, parsers, orchestration, loaders, protocol, MCP tools |
| Catalog (`solentlabs-cable-modem-monitor-catalog`) | PyPI wheel | modem.yaml, parser.yaml for each supported modem |

The HACS zip contains only runtime files — `docs/` is excluded from
the zip build. Spec files stay in the repo for contributors but don't
ship to users.

### Install flow

1. HACS downloads `cable_modem_monitor.zip` from the GitHub release
2. HACS extracts the zip into `custom_components/cable_modem_monitor/`
3. HA reads `manifest.json` → sees `requirements` pins
4. HA pip-installs Core and Catalog from PyPI (exact `==` pins)
5. Integration loads

### Manifest loggers

`manifest.json` declares `loggers` for both PyPI packages:

```json
"loggers": [
    "solentlabs.cable_modem_monitor_core",
    "solentlabs.cable_modem_monitor_catalog"
]
```

This tells HA to include Core and Catalog log output when a user
enables debug logging for the integration. Without this field, only
the `custom_components.cable_modem_monitor` logger is captured.

## Branching and Merging

### Applying a fix to multiple feature branches: rebase, don't cherry-pick

When the same fix needs to land on multiple in-flight feature branches
(e.g., v3.11.0 and v3.12.0):

1. Commit the fix to the base branch first (e.g., v3.11.0).
2. Rebase the child branch onto the updated parent:
   `git checkout feature/v3.12.0 && git rebase feature/v3.11.0`
3. Force push the rebased branch: `git push --force-with-lease`

Cherry-picking creates duplicate commits with different SHAs; when the
branches eventually merge to main, you get duplicate history or merge
conflicts. The exception is backporting to *unrelated* branches (e.g.,
a hotfix into an older release line) — cherry-pick is fine there.

### Release branches → main: merge commits, not squash

- Preserves release branch history.
- Child branches rebase cleanly without "skipped commit" noise.
- Shows clear release boundaries in `git log`.

### Beta → main merges are contributor-onboarding-driven

Whether to open a beta-to-main merge PR after each beta tag turns
on **contributor-onboarding pressure**, not a blanket "always
merge" rule. The default GitHub view of the repo shows `main`;
contributors landing on the catalog README, contributor docs, or
intake tooling see whatever is on `main`. When the canonical view
is stale relative to current pipeline reality, contributors start
on outdated docs and friction accumulates.

**Merge a given beta to main when:** the beta contains
contributor-facing doc or tool changes that should be visible at
the canonical GitHub URL. Examples:

- Catalog README updates (new confirmed modems, chipset metadata,
  badge changes)
- `cable_modem_monitor_catalog_tools/` workflow changes
  (intake/confirmation tooling, MCP tool additions)
- Contributor-onboarding docs (CONTRIBUTING, INTAKE_PIPELINE,
  MODEM_INTAKE_WORKFLOW, AUTHORING guides)
- Dev tooling that contributors use (`make` targets, pre-commit
  hooks, local CI mirror commands)

**Defer the merge when:** the beta is mostly runtime-code changes,
internal refactors, or test/spec updates with no user- or
contributor-facing surface change. The merge can wait until the
next beta with contributor-facing changes, or until stable cut.

**HACS note:** `main` being on a beta version string is acceptable
here. HACS users who haven't opted into the beta channel install
the latest *stable tag*, not the default branch — so a beta version
string on `main` doesn't break stable installs. Beta-channel users
opt in separately. Don't argue against the merge on HACS grounds.

**Original driver:** the v3.14 architecture moved to `main` early
because contributors were starting on the wrong branch — a major
operational headache. With v3.14 on `main`, that acute pressure is
reduced; later beta merges become more situational.

### Small single-feature/fix PRs: squash merge is fine

Creates one clean commit on main.

### Contributor PRs target the open beta branch

Contributors open PRs against `main` (GitHub's default); the maintainer
retargets each to the open `feature/vX.Y.Z-beta.N` branch with
`gh api repos/solentlabs/cable_modem_monitor/pulls/<n> -X PATCH -f base=<branch>`
(`gh pr edit --base` fails on a GitHub GraphQL deprecation). The beta
branch is then the only PR into `main`, so a release arrives as one
reviewed range.

### CHANGELOG ownership

Contributors don't write CHANGELOG entries. The maintainer reconciles
`[Unreleased]` against `git log <last-tag>..HEAD` before running
`release.py`, which promotes the section verbatim (see
[Release Checklist](#release-checklist)).

`changelog-check.yml` (required on `main`) enforces two things:

- `scripts/check_changelog.py` validates structure on every PR:
  `[Unreleased]` first, version headings well-formed and descending,
  Keep a Changelog sections plus the release-narrative ones (`Overview`,
  `Upgrade Notes`, `Highlights...`) only, no roadmap P-numbers or
  placeholders. Prose always sits under a `###` heading.
  It reports only on lines the branch touches, so entries published
  before these rules existed are read for context (version order,
  duplicate detection) but never flagged; `--all` covers the whole file.
  Local mirror: `make changelog-check`, part of `make validate-ci`.
- A PR into `main` that changes code under `custom_components/` or
  `packages/` must also change `CHANGELOG.md`. PRs into a beta branch
  are exempt; that is where contributor work lands.

## Hotfix Releases

For urgent fixes to a released version:

```bash
# Create hotfix branch from the release tag
git checkout -b hotfix/3.14.1 v3.14.0

# Make fixes, commit, then follow normal release process
.venv/bin/python scripts/release.py 3.14.1
# Stage and commit version bump, then push
git push origin hotfix/3.14.1
# Create PR, merge, then tag main
```

## Release Checklist

- [ ] All tests passing (`pytest tests/ -v`)
- [ ] Code quality checks passing (`ruff check`, `black --check`, `mypy`)
- [ ] CHANGELOG.md `[Unreleased]` reconciled against `git log <last-tag>..HEAD --oneline` — every user-facing commit has an entry (`release.py` promotes `[Unreleased]` verbatim; it does not generate entries)
- [ ] Version numbers consistent (manifest.json, const.py)
- [ ] PR reviewed and merged to main
- [ ] Tag created on main (not feature branch)
- [ ] GitHub Release created automatically
- [ ] Verify HACS picks up new version

## Troubleshooting

### "Tag already exists"

If you accidentally pushed a tag to the wrong branch:

```bash
# Delete remote tag
git push origin --delete v3.14.0

# Delete local tag
git tag -d v3.14.0

# Start fresh from the tagging step
```

### PR shows "Expected — Waiting for status to be reported" on required checks

The `require-status-checks` repository ruleset matches check names as
plain strings. If a workflow job is renamed, the ruleset doesn't know —
it just never sees the old name reported and the check hangs as
"Expected" forever.

Fix: update the ruleset to use the new job name. The current required
checks live in ruleset ID `10547747`. Update via:

```bash
gh api repos/solentlabs/cable_modem_monitor/rulesets/10547747 --method PUT --input <payload>
```

where `<payload>` is the full ruleset JSON with the corrected `context`
strings. See CLAUDE.md § "Adding a new CI job" for the rule that
prevents this drift.

### Release workflow didn't trigger

Ensure the tag follows the pattern `v*` (e.g., `v3.14.0`). Check `.github/workflows/release.yml` for the trigger configuration.

---

Last updated: 2026-08-24
