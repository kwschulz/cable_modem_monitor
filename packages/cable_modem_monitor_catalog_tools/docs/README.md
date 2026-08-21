# Catalog Tools Documentation

Documentation for the catalog authoring lifecycle. Catalog Tools
covers both ends of a modem's life in the catalog: **intake**
(turning a HAR capture into modem.yaml + parser.yaml + test_data +
golden files) and **confirmation** (turning a contributor's HA
diagnostics into a `verified.json` fixture and flipping the
modem's status from `awaiting_verification` to `confirmed`). Open
to contributors with hardware and (typically) AI assistance for
the judgment layer.

For the integration's core specs, see the
[Core documentation](../../cable_modem_monitor_core/docs/).
For the modem data catalog itself, see the
[Catalog documentation](../../cable_modem_monitor_catalog/docs/).
Doc-authoring rules — one home per rule, current contract only, how to
cite another doc — are repo-wide and live in
[Project docs § Doc Authoring](../../../docs/README.md#doc-authoring).

| Document | Covers |
| -------- | ------ |
| [ONBOARDING_SPEC.md](ONBOARDING_SPEC.md) | Intake pipeline specification — contracts between stages, validation rules |
| [INTAKE_PIPELINE.md](INTAKE_PIPELINE.md) | Pipeline overview — who does what, extension points, fleet patterns |
| [MODEM_INTAKE_WORKFLOW.md](MODEM_INTAKE_WORKFLOW.md) | Runnable workflow — intake (Steps 1–11) and confirmation (Steps 12–15) |

## Why this package is separate

See `../../cable_modem_monitor_core/docs/ARCHITECTURE_DECISIONS.md`
section "catalog_tools is a developer accelerator, never a runtime
dep" for the rationale. In short: Core + Catalog is the minimum
required surface. Catalog Tools helps authors reach a working
configuration faster — it is never installed by Home Assistant.

**Operational test:** deleting this package directory must leave
Core + Catalog + HA fully functional.

## Dependency policy

Catalog Tools is `Private :: Do Not Upload` — only contributors
running intake install it, never end users. Contributor-onboarding
friction is load-bearing (catalog growth depends on community
contributions; every install hurdle counts), so dependency choices
are made deliberately.

### Decisions

1. **No `lxml` dependency.** Rejected after evaluation against the
   contributor-onboarding cost.
2. **HTML parsing uses HTML5-aware regex.** Pattern + accepted
   blind spots documented in
   `analysis/js_endpoints.py` module docstring. Not `bs4`.
3. **If HTML5 compliance ever becomes load-bearing, prefer
   `html5lib` over `lxml`.** Pure Python, no C extension.

### Why no `lxml`

- C extension. Wheels cover major platforms but exotic ones
  compile from source — every extra dep is one more install step
  that can fail.
- The "use bs4" reflex is misleading: `bs4 + html.parser` (stdlib
  backend) has the same HTML5 end-tag blind spots as a regex.
  Only `bs4 + lxml` (or `+ html5lib`) is HTML5-correct. Adding
  `bs4` without an HTML5-correct backend is strictly worse than
  the regex (more code, same defect).
- The pipeline's value proposition is "drop a HAR, get a working
  catalog entry." Every dep counts against that.

### Revisit if

A real modem intake fails because the regex's accepted blind
spots — unterminated `<script>` blocks, literal `</script>` in JS
strings, CDATA wrapping, HTML-commented scripts — actually break
extraction. Until then, the warnings are advisory and the impact
of a miss is low. See `analysis/js_endpoints.py` module docstring
for the full list.

### Scope

This is a catalog_tools decision. Core has its own evaluation to
make if/when an HTML5-parsing concern arises there.
