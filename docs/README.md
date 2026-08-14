# Project Documentation

Guides and references that span the full project. For package-specific
specs, see the indexes in each package's `docs/` directory.

## Guides

| Document | Audience | Covers |
|----------|----------|--------|
| [CODE_REVIEW.md](CODE_REVIEW.md) | Contributors | Coding standards, test patterns, naming |
| [MODEM_REQUEST.md](MODEM_REQUEST.md) | Contributors | How to submit a modem request with HAR capture |
| [EXAMPLES.md](EXAMPLES.md) | Contributors | Usage examples |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Users | Common issues and solutions |
| [TRANSLATION_GUIDE.md](TRANSLATION_GUIDE.md) | Translators | Localization process |
| [ATTRIBUTION.md](ATTRIBUTION.md) | All | Third-party acknowledgments |

## Reference

| Document | Covers |
|----------|--------|
| [reference/RELEASING.md](reference/RELEASING.md) | Release process (beta, stable) |
| [reference/LINTING.md](reference/LINTING.md) | Linter configuration and rules |
| [`.github/codeql/README.md`](../.github/codeql/README.md) | CodeQL configuration, suppressed-rule rationales, sibling-repo workflow |
| [reference/CODEQL_TESTING_GUIDE.md](reference/CODEQL_TESTING_GUIDE.md) | CodeQL test patterns |

## Setup

| Document | Covers |
|----------|--------|
| [setup/GETTING_STARTED.md](setup/GETTING_STARTED.md) | Setup, daily workflow, troubleshooting (single canonical path: VS Code dev container, WSL2 on Windows) |

## Doc Authoring

Applies to every doc in this repo — project guides, package specs,
workflow docs, and `ARCHITECTURE_DECISIONS.md`.

- One home per rule. A fact stated in two docs will drift; state it
  once and link from everywhere else.
- Docs state the current contract only — no version history, no
  "previously" narrative, no record of what a design replaced. Git
  and the commit body carry that.
- Quote code only as far as the contract requires (public signature,
  schema); implementation detail stays in code.
- A decision enters `ARCHITECTURE_DECISIONS.md` only if it constrains
  future work; alternatives merely considered are omitted. A rejected
  approach is recorded as the constraint it creates, not as the story
  of its rejection.
- Cite as `FILE.md § Section Name` with a relative link. The doc that
  owns a rule is the link target; consumers point to it with at most
  one sentence of local context.

## Package Specs (separate indexes)

| Index | Scope |
|-------|-------|
| [Core specs](../packages/cable_modem_monitor_core/docs/README.md) | Architecture, auth, parsing, orchestration |
| [Catalog docs](../packages/cable_modem_monitor_catalog/docs/README.md) | Modem data, mock server |
| [Catalog Tools docs](../packages/cable_modem_monitor_catalog_tools/docs/README.md) | Intake pipeline, onboarding spec, runnable workflow |
| [HA specs](../custom_components/cable_modem_monitor/docs/README.md) | Config flow, entities, adapter wiring |
