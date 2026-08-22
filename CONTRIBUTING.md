# Contributing to Cable Modem Monitor

Thank you for your interest in contributing! This document provides guidelines for contributing to the project.

## Ways to Contribute

- 🐛 Report bugs via [GitHub Issues](https://github.com/solentlabs/cable_modem_monitor/issues)
- 💡 Suggest features or improvements
- 📝 Improve documentation
- 🧪 Add support for additional modem models
- 🔧 Submit bug fixes or enhancements
- 🌍 Help translate the integration (see [Translations](#translations) below)

---

## Before You File

The encouraged contribution path is **expanding modem support** — the catalog grows through community contributions because the maintainer can't acquire every modem. See [Adding Modem Support](#adding-modem-support) for the catalog intake pipeline.

- **Bug reports** — file directly via the bug template.
- **Modem support requests** — file directly via the modem-request template.
- **Adding modem support yourself** — use the catalog intake pipeline — Claude Code: `/modem-intake`; other AI tools: load [`skills/modem-intake.md`](skills/modem-intake.md) as context. See [Adding Modem Support](#adding-modem-support).
- **New features, sensors, architecture changes** — start a [Discussion](https://github.com/solentlabs/cable_modem_monitor/discussions/new?category=ideas), not an Issue. Issues are for features whose shape is already clear; Discussions are for shaping the idea. This avoids the situation where a contributor invests time in a full design that doesn't fit the project's direction.
- **Core code changes (`packages/cable_modem_monitor_core/`)** — start a Discussion regardless of size. Core is the shared substrate every supported modem depends on; a regression there breaks every modem at once. The bar is correspondingly high (regression tests, golden files, real-modem evidence).
- **Refactors that touch more than two files** — start as a Discussion. Small, scoped fixes outside Core can go straight to PR.

### Why scope-before-code matters

Review is the bottleneck on any project, solo or team. Generation cost has dropped to near-zero with AI assistance; review cost is still bounded by human reading speed and judgment, with reviewer attention degrading measurably past 60–90 minutes per session ([Sadowski et al., *Modern Code Review: A Case Study at Google*, FSE 2018](https://sback.it/publications/icse2018seip.pdf)). The only way to keep the project from breaking under that asymmetry is to align on scope before code is written.

This discipline predates AI. Rust requires an [RFC](https://github.com/rust-lang/rfcs) for substantial changes; Python uses [PEPs](https://peps.python.org/pep-0001/); Kubernetes uses [KEPs](https://github.com/kubernetes/enhancements); Home Assistant routes architectural changes through a dedicated [architecture repo](https://github.com/home-assistant/architecture). The cost of misaligned implementation has always exceeded the cost of a design discussion. AI just makes skipping the step more tempting, and more expensive when it happens.

---

## What Happens After You File

Review path depends on which surface the PR touches:

- **Catalog PR** (new modem entry, HAR fixtures, parser.yaml) — reviewed against intake-pipeline standards. This is the encouraged path; usually the fastest to merge.
- **Core PR** (`packages/cable_modem_monitor_core/`) — heavy scrutiny. Regression tests, golden files, and real-modem evidence are expected. Core PRs without that bar may be closed or held until the evidence is in. The asymmetric bar reflects asymmetric blast radius — Core breakage affects every supported modem at once.
- **HA adapter PR** (`custom_components/`) — standard review for behavior changes; bug fixes without behavior change move faster.
- **Docs PR** — standard review.
- **PR that builds on a prior Discussion** — the Discussion is the design agreement; review focuses on the implementation matching it.
- **PR that introduces net-new direction without a prior Discussion** — likely to be closed in favor of starting a Discussion. This isn't a judgment on the work — it's a sequencing call. Direction proposals belong in Discussions where they can be shaped collaboratively before code is written. Once the direction is agreed, a follow-up PR is welcome.
- **Issue that should have been a Discussion** — converted to a Discussion (GitHub supports this), or you'll be asked to open one. No need to refile.

PR titles and bodies are not edited by the maintainer — any feedback comes in review or close comments.

---

## Adding Modem Support

There are two paths, depending on what you want to do:

- **Requesting support for your modem** — see
  [docs/MODEM_REQUEST.md](./docs/MODEM_REQUEST.md). Capture a HAR,
  screen it for PII, file a request. A maintainer or contributor builds
  the parser from there; you verify it works on your hardware.
- **Want to implement the catalog entry yourself** — the intake pipeline
  takes a HAR and produces `modem.yaml`, `parser.yaml`, and golden files.
  AI assistance helps with the judgment steps but is not required. Start
  with [AI-Assisted Catalog Contribution](#ai-assisted-catalog-contribution)
  below, then follow
  [MODEM_INTAKE_WORKFLOW.md](packages/cable_modem_monitor_catalog_tools/docs/MODEM_INTAKE_WORKFLOW.md)
  for the full walkthrough.

Either path is valuable. The catalog grows through community contributions
because the maintainer can't acquire every modem.

If the intake pipeline stops with a **Core gap** — a pattern the pipeline
can't classify — the gap report is the contribution. Paste it into your
issue. You do not need to implement Core changes to resolve it; that is a
separate development effort.

---

## Development Environment

See the [Getting Started Guide](./docs/setup/GETTING_STARTED.md) for
environment setup. Come back here once you can run `make validate`.

### Format, Lint, Test

Day-to-day, the workflow runs through VS Code tasks
(`Ctrl+Shift+P` → `Tasks: Run Task`):

- **🎨 Format Code** — auto-format on demand
- **🧪 Test: All** — full test suite (Core, Catalog, HA)
- **⚡ Test: Quick** — fast feedback during development

Linting runs continuously in the **PROBLEMS** tab via the Ruff, mypy,
and Pyright extensions — keep it at zero.

For terminal use, `make format` / `make check` / `make test` cover the
same ground. Pre-commit hooks check formatting, linting, type-check,
and PII at commit time. See [Submitting Changes](#submitting-changes)
below for the pre-push gate.

### Test on Local HA

VS Code tasks bring up a local Home Assistant container with the
integration bind-mounted:

- **🚀 HA: Start** — start HA at <http://localhost:8123>
- **🚀 HA: Start (Debug)** — same, with `custom_components.cable_modem_monitor`
  at DEBUG log level
- **📋 HA: View Logs** — tail the HA log
- **⏹️ HA: Stop** — shut down the container

To test a PR, `gh pr checkout NNN` then run **🚀 HA: Start**.

### The Contributor Loop

For catalog contributions (the primary path):

1. Open the project in VS Code (WSL2)
2. Use the intake pipeline with AI assistance to produce `modem.yaml`,
   `parser.yaml`, and golden files — see
   [AI-Assisted Catalog Contribution](#ai-assisted-catalog-contribution)
3. Start the local HA container (**🚀 HA: Start**) and verify the
   integration loads and sensors appear
4. Run `make validate-ci` — green means the regression suite has
   validated the output
5. Submit the PR

Packaging and release are handled by the maintainer. A contributor's job
ends at step 5.

### Core Changes

Core changes follow the same local environment and the same `make
validate-ci` gate — no special setup required beyond what catalog
contributors already have. The difference is scope control, not tooling.

Core is the shared substrate every supported modem depends on. A
regression there breaks all of them at once. Before writing any Core code:
start a Discussion, agree on scope, get a green light. PRs that skip this
step will be closed regardless of code quality.

Once a Core PR is submitted with passing tests and golden files, hardware
sign-off comes from whoever has the affected modem — the contributor, the
original reporter, or a community tester via the `needs-testing` label
flow. The maintainer reviews code; hardware is the community's domain.

## Project Architecture

The codebase is split into three layers:

| Package | Path | Responsibility |
|---------|------|----------------|
| **Core** | `packages/cable_modem_monitor_core/` | Auth, HTTP loading, parsing, orchestration, test harness. Platform-agnostic — no HA imports. |
| **Catalog** | `packages/cable_modem_monitor_catalog/` | Modem configs (`modem.yaml`), parsers (`parser.yaml` / `parser.py`), HAR fixtures and golden files. |
| **HA Adapter** | `custom_components/cable_modem_monitor/` | Config flow, sensors, services, coordinators. Thin wrapper that imports from Core and Catalog. |

Core and Catalog are published to PyPI as standalone packages. The HA
adapter declares them as dependencies in `manifest.json`.

**Specs** (authoritative design docs):

- Core: [`packages/cable_modem_monitor_core/docs/`](packages/cable_modem_monitor_core/docs/) — architecture, auth, parsing, orchestration, onboarding
- HA: [`custom_components/cable_modem_monitor/docs/`](custom_components/cable_modem_monitor/docs/) — config flow, entities, adapter wiring

**Tests:**

- Core + Catalog: `pytest` in each package's `tests/` directory (not from repo root)
- HA integration: `pytest` at repo root (`tests/`)

## AI-Assisted Contribution

AI assistance — Claude Code, Cursor, Copilot, or similar — can lower the bar to contributing significantly, but it amplifies the review-capacity asymmetry described in [Why scope-before-code matters](#why-scope-before-code-matters). The rules below apply to all AI-assisted contributions to this repository.

### Disclosure

Disclose AI use on your first PR or issue. One sentence — which tool, what role it played. This isn't a barrier; it's calibration.

### Read CONTRIBUTING.md alongside CLAUDE.md

Most coding assistants auto-load project-instruction files (`CLAUDE.md` for Claude Code, `.cursorrules` for Cursor, `.github/copilot-instructions.md` for Copilot) but do **not** auto-load `CONTRIBUTING.md`. `CLAUDE.md` is a thin behavioral guide that points to the authoritative docs (`docs/CODE_REVIEW.md`, `packages/cable_modem_monitor_core/docs/ARCHITECTURE.md`, `packages/cable_modem_monitor_core/docs/MODEM_YAML_SPEC.md`) — it is not a substitute for the contribution-flow rules here. Add `CONTRIBUTING.md` to your AI's context explicitly when working on this repository — otherwise your tool will produce work that follows the code rules but skips the process rules (PRs that should have been Discussions, Core changes that bypass scope review, refactors spanning many files without prior alignment).

### Know the entity model boundary

This integration exposes what modems report — channel data, signal levels,
system info — not computed summaries or derived analysis built on top of
it. Features that add interpretation (signal health scores, quality grades,
anomaly detection) cross the entity model boundary and require a Discussion
before any code or spec work. Without that Discussion, the PR will be
closed.

The boundary is intentional and documented in
[ARCHITECTURE_DECISIONS.md](packages/cable_modem_monitor_core/docs/ARCHITECTURE_DECISIONS.md).
When your AI proposes a feature that interprets or scores modem data, that
is the signal to open a Discussion rather than continue coding.

### One thread, one ask

Multi-topic comments and PRs get redirected. If a comment carries three unrelated asks (a bug fix + a doc critique + a queued feature), they go in three separate threads. Pre-committing future output ("I'll also be writing X next") is discouraged — it pre-loads scope without alignment.

### Read every diff line-by-line before committing

Accepting AI-generated changes without reading them produces classic failure modes: merge-conflict markers leaking into commits, specs rewritten to match in-flight code instead of the other way around, multiple unrelated concerns bundled into one PR. Disclosure is not a substitute for inspection.

### Review pace and volume

> Project parameter — adjust for your context.

I review on weekends, with ~2-week turnarounds typical. Multiple open PRs from one contributor are sequenced, not parallelized — one active work item at a time. This isn't gatekeeping; it reflects the review-capacity reality cited above.

### Templated redirect

When a contribution skips the process rules, the response is a short canned redirect, not a per-PR negotiation:

> Thanks for the contribution. This needs scope discussion before code — please open a Discussion, and we can scope it together there. Closing this PR for now; reopen or refile after the Discussion lands.

Depersonalized by design. Same message every time, applied consistently to everyone.

## AI-Assisted Catalog Contribution

The general rules in [AI-Assisted Contribution](#ai-assisted-contribution) apply; this section adds catalog-specific guidance.

The maintainer doesn't have access to most of the modems in the
catalog. Catalog growth depends on contributors who have the hardware
and on AI tools that lower the bar to analyzing captures and proposing
entries. This section is for that audience.

If you have AI access (Claude, ChatGPT, or similar with file
attachment support), you can do significantly more than file a request:

- **Audit your own HAR** for completeness before submitting (prompt
  in the next section).
- **Run the intake pipeline** on a HAR — yours or one attached to a
  stalled issue — and open a PR with the resulting catalog entry. See
  [MODEM_INTAKE_WORKFLOW.md](packages/cable_modem_monitor_catalog_tools/docs/MODEM_INTAKE_WORKFLOW.md).
- **Comment on stalled issues** with HAR audit results so other
  contributors can pick them up.
- **Analyze logs** from bug reports to identify patterns.

### Auditing a HAR for completeness

The most common reason a HAR is unusable is that it was recorded
against an already-logged-in browser session — the auth flow is
missing. Attach the `.sanitized.har` to your AI assistant and run this
prompt:

````text
You are auditing a HAR capture from a cable modem's web interface.
The capture will be used to build a parser, so it needs to contain
the full HTTP conversation including any login flow.

Please inspect the attached HAR and answer these questions in order.
Output a fenced markdown block I can paste into a GitHub issue.

1. Total number of entries (requests) in the HAR.
2. Is there evidence of authentication in the capture? Look for any
   of: a POST request whose URL contains "login", "auth", "session",
   "hnap", or "admin"; a POST carrying form-encoded credentials in
   the body; or any request carrying an `Authorization` header
   (which is how HTTP Basic Auth modems present credentials). List
   what you find (URL + method + which signal). Note: HNAP-style
   modems POST to `/HNAP1/` for normal operations too, so listing
   all of them is fine — presence is what matters.
3. Does the FIRST request to the modem carry a Cookie header? On
   its own this is a hint, not proof — but combined with the
   *absence* of any authentication evidence in question 2, it
   strongly suggests the capture was recorded against an already
   logged-in session.
4. How many distinct data-bearing responses are present? Count
   responses whose Content-Type starts with "text/html",
   "application/json", "application/xml", or "text/xml". List the
   paths.
5. Do any responses look truncated or empty (response body size
   under 200 bytes)? List them — small responses are sometimes
   legitimate (status pings, redirects), so this is an FYI, not a
   failure on its own.
6. Overall verdict: PASS / RECAPTURE NEEDED / UNCERTAIN, with a
   one-sentence reason.

Format the output exactly like this:

```
## HAR audit

- Entries: <N>
- Authentication evidence: <list or "none found">
- First-request cookie: <yes/no>
- Data-bearing responses: <count> — <paths>
- Small responses (FYI): <list or "none">
- Verdict: <PASS | RECAPTURE NEEDED | UNCERTAIN> — <reason>
```
````

**PASS** → continue to the intake pipeline. **RECAPTURE NEEDED** →
re-run har-capture and make sure the recording covers a full login
flow (not just navigation after the modem already had a session).
**UNCERTAIN** → submit anyway with the audit output included.

A capture with no authentication evidence *and* a Cookie header on the
first request is almost certainly post-auth and won't produce a working
parser.

### The intake pipeline

The `cable_modem_monitor_catalog_tools` package contains the intake
pipeline — a deterministic toolchain that validates HAR captures,
detects auth strategy, classifies response formats, generates
`modem.yaml` and `parser.yaml`, and produces golden files.

- [Intake Pipeline overview](packages/cable_modem_monitor_catalog_tools/docs/INTAKE_PIPELINE.md)
- [Modem Intake Workflow](packages/cable_modem_monitor_catalog_tools/docs/MODEM_INTAKE_WORKFLOW.md)
- [Onboarding Spec](packages/cable_modem_monitor_catalog_tools/docs/ONBOARDING_SPEC.md)

The catalog tools package is never installed by Home Assistant — it's
a developer accelerator for catalog growth, open to contributors with
hardware. The pipeline mechanics are plain Python; the judgment work
(format detection on ambiguous HTML, metadata enrichment, test failure
diagnosis) realistically benefits from AI assistance — that's what
this section's audience brings to the table.

Modem configurations live in the catalog package
(`packages/cable_modem_monitor_catalog/`). Each modem has a
`modem.yaml`, `parser.yaml`, and `test_data/` directory with a HAR
capture and golden file.

## Code Style

Style, type-safety patterns, and test conventions are documented in
[docs/CODE_REVIEW.md](docs/CODE_REVIEW.md). Ruff and Black enforce
formatting automatically — run `make format` or let pre-commit handle it.

## Submitting Changes

Standard fork → branch → PR flow.

**Inner loop**: `make test` (or VS Code's **🧪 Test: All** task) for
quick iteration. Runs the package and integration test suites — same
pytest CI runs, but it's a *subset* of the full CI gate.

**Pre-push gate**: `make validate-ci`. This is the canonical local
mirror of the CI Tests workflow — lint, format, type-check, tests,
intake regression, PII check, and catalog README freshness. If
`make validate-ci` is green, CI will be green. Pre-push hooks run a
subset, so failures still surface, but `make validate-ci` is the
definitive local check.

`scripts/release.py` runs `make validate-ci` automatically as part of
every version bump, so a release can never silently land on top of
regressions that accumulated since the last bump.

PRs that touch a modem (parser, catalog entry) should follow the
[AI-Assisted Catalog Contribution](#ai-assisted-catalog-contribution)
flow above. Reference issues with `Related to #X` or `Addresses #X` —
never `Fixes #X` (see [Issue Closing Policy](#issue-closing-policy)).
Don't add a CHANGELOG.md entry; the maintainer writes it at release
(see [RELEASING.md § CHANGELOG ownership](docs/reference/RELEASING.md#changelog-ownership)).

### Issue Closing Policy

**No issue is ever auto-closed.** Don't use `Fixes #X`, `Closes #X`,
or `Resolves #X` in PR bodies or commit messages — GitHub auto-closes
from any commit message in a merge, including buried references. Use
`Related to #X` or `Addresses #X` instead.

Closing requires either an artifact proving the fix worked, or a
deliberate manual close by the maintainer (when the reporter has left
the conversation — not automatic).

- **Bug or defect** — artifact is the reporter confirming the fix on
  their hardware.
- **Modem support request** — closing requires:
  - **From the user:** sanitized HAR capture (`modem.har`) and
    verified diagnostics (`modem.verified.json`)
  - **Derived from the HAR:** `modem.yaml`, `parser.yaml`, optionally
    `parser.py`, and `modem.expected.json`
  - **Gate:** all of the above pass the regression test suite before
    the branch merges
- **Capability gap on a confirmed modem** (missing uptime, untested
  action, etc.) — the durable record is the `gaps:` list in the
  modem's `modem.yaml`, rendered in
  [CATALOG_AUDIT.md](packages/cable_modem_monitor_catalog/CATALOG_AUDIT.md)
  as "Confirmed with Gaps." An issue tracking a gap stays open while
  there's an active conversation; when it goes cold, close it with a
  pointer to the audit table — the gap record survives the closure.
  Renewed interest gets a fresh issue (ideally opened by the
  interested contributor), and the gap's `issue:` URL is updated to
  match.

### Commit Message Format

Use clear, descriptive commit messages:

```text
Add support for Arris TG1682G modem

- Added HTML parser for Arris status page format
- Created test fixtures from real modem output
- Updated documentation with supported models
- All existing tests still pass
```

## Issue Labels

State labels (`needs-triage`, `in-development`, `needs-testing`,
`needs-data`, `backlog`) are mutually exclusive — exactly one
applies. Same for `release:vX.Y` labels. Everything else stacks.

- `needs-triage` — auto-applied; replaced with a real state on first read
- `in-development` — code being written or in an unreleased branch
- `needs-testing` — released and installable; user can verify now
- `needs-data` — waiting on user for HAR / diagnostics / clarification
- `backlog` — acknowledged, not actively prioritized

`needs-testing` is not applied until the code is released. A parser on
an unreleased branch is `in-development` even when complete. Apply
`release:vX.Y` when the work is committed to that release (branch cut,
or merged); all `release:vX.Y` descriptions read "Tagged for vX.Y."

When a user confirms a modem parser works on real hardware, the modem
is promoted per the
[promotion procedure in MODEM_DIRECTORY_SPEC.md](packages/cable_modem_monitor_core/docs/MODEM_DIRECTORY_SPEC.md#promotion-procedure).

## Release Process

Maintainers handle releases following semantic versioning. See
[Release Process](docs/reference/RELEASING.md) for the full workflow
including the `release.py` script and step-by-step instructions.

## Code of Conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Attribution

When adding a parser informed by external code or research, document
the source in the parser docstring and add an entry to
[docs/ATTRIBUTION.md](docs/ATTRIBUTION.md). For how to phrase external
influence honestly — especially when AI tools were involved — see
[Attribution Standards](docs/ATTRIBUTION.md#attribution-standards).

Data contributors, testers, and external references are credited in
ATTRIBUTION.md.

---

## Questions?

- 🐛 Report issues via [GitHub Issues](https://github.com/solentlabs/cable_modem_monitor/issues)
- 📧 Contact maintainers via GitHub

## Resources

- [Home Assistant Developer Docs](https://developers.home-assistant.io/)
- [HACS Documentation](https://hacs.xyz/)
- [Python asyncio](https://docs.python.org/3/library/asyncio.html)
- [pytest Documentation](https://docs.pytest.org/)

---

## Translations

> **📖 See [Translation Guide](docs/TRANSLATION_GUIDE.md)** for complete instructions.

**12 languages supported:** English, German, Dutch, French, Chinese, Italian, Spanish, Polish, Swedish, Russian, Portuguese (Brazil), Ukrainian

**To add a language:** Copy `translations/en.json` → `translations/XX.json`, translate values (not keys), submit PR.

Thank you for contributing to Cable Modem Monitor!
