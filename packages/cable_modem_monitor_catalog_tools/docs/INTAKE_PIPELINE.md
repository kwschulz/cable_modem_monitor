# Modem Intake Pipeline

How new modems go from a HAR capture to a tested catalog entry.

> **Authoritative spec:** [ONBOARDING_SPEC.md](ONBOARDING_SPEC.md)
> covers tool contracts, decision trees, validation rules, worked examples,
> and error handling in full detail. This document is an overview.

---

## Who Does What

| Role | What they do |
| ------ | ------------- |
| **HA user filing a request** | Captures a HAR with [har-capture](https://github.com/solentlabs/har-capture) and submits it via [modem request issue](https://github.com/solentlabs/cable_modem_monitor/issues/new?template=modem_request.yml). |
| **Catalog contributor** | Runs the intake pipeline on their own HAR or on a submitted one, produces a draft catalog entry, opens a PR. AI assistance (e.g., [Claude Code](https://claude.com/claude-code)) is the expected helper for the judgment steps. See [MODEM_INTAKE_WORKFLOW.md](MODEM_INTAKE_WORKFLOW.md). |
| **Maintainer** | Reviews and merges PRs, develops Core when a CoreGap is reported, ships releases. |
| **MCP tools** | Orchestration accelerator for runs driven through an AI agent. Handle deterministic steps — HAR parsing, pattern matching, config generation, validation, test execution. |
| **LLM** | Handles judgment calls — ambiguous HTML formats, metadata web search, test failure diagnosis. |

The pipeline tooling is plain Python, but the judgment layer realistically benefits from AI assistance. This project itself was built with Claude Code; that's the assumed contributor path. Manual config creation also works — the specs are the authority — but expect it to take more iteration.

---

## Pipeline Overview

```text
HAR file
    │
    ▼
validate_har ─── structural + auth flow checks
    │
    │             catalog/modems/
    │                  │
    ▼                  ▼
    │             scan_fleet ─── build FleetPatterns from proven configs
    │                  │
    ▼                  │
analyze_har(fleet) ◄───┘ transport, auth, session, actions, format, fields
    │
    ├── hard_stops? → stop, report to user
    ├── core_gaps? → stop, report what Core needs (see below)
    ├── unread_resources → key skeletons of the endpoints nothing read
    │
    ▼
enrich_metadata ─ infer defaults, detect missing fields
    │
    ├── missing fields? → LLM web search (chipset, DOCSIS version, ISPs)
    │
    ▼
generate_config(fleet) ─ modem.yaml + parser.yaml (Pydantic-validated)
    │
    ├── validation errors? → LLM fixes, retries
    │
    ▼
generate_golden_file ─ parse HAR through generated config
    │
    ▼
write_modem_package ── place all files in catalog directory
    │
    ▼
run_tests ─────── HAR replay → auth → load → parse → golden file diff
    │
    ├── failures? → LLM diagnoses, fixes config, re-runs
    │
    ▼
catalog entry ready for review
```

**Two outcomes:**

- **Known patterns:** Pipeline runs end-to-end, produces a tested catalog entry.
- **Unknown patterns:** Pipeline stops with a CoreGap report (see below).

---

## What's Deterministic vs. What's Judgment

The pipeline separates deterministic logic (repeatable, testable Python code) from LLM judgment (ambiguity resolution, web search, diagnosis).

| Step | Deterministic (Python function) | Judgment (LLM) |
| ------ | -------------------------- | ----------------- |
| HAR validation | Structural checks, auth flow detection — fail-fast gate | — |
| Fleet scan | `scan_fleet()` builds patterns from catalog parser.yaml files | — |
| Transport detection | HNAP marker scan, URL pattern matching | — |
| Auth detection | Pattern matching against `auth_patterns.json` | Intended: ambiguous cases presented for a decision. Today the detector returns one strategy and no alternatives — see [Detection Owes the LLM Evidence](#detection-owes-the-llm-evidence-not-a-verdict) |
| Format detection | HNAP: deterministic. HTTP: candidate list | HTTP: LLM reads response bodies, picks format |
| Field mapping | Column/field extraction from HAR content, service flow aggregate detection, fleet patterns augment direction and system_info label detection | — |
| Unread resources | Subtract every endpoint the config reads from the HAR's 2xx JSON endpoints; reduce each remainder to its key skeleton | Read the skeletons and decide whether anything there is worth mapping |
| Metadata enrichment | Inference from analysis + defaults | Web search for missing fields (chipset, ISPs) |
| Config generation | Pydantic validation, constraint checking | Fix validation errors and retry |
| Golden file generation | Parse HAR through config | Sanity-check channel counts |
| Testing | HAR replay, golden file diff | Diagnose failures, fix config |

See [ONBOARDING_SPEC.md § Tool boundaries](ONBOARDING_SPEC.md#tool-boundaries) for the full responsibility matrix.

---

## Detection Owes the LLM Evidence, Not a Verdict

Intake never aimed to detect every config deterministically. The goal is a
near-perfect config with the remaining gaps named, and the LLM is the layer
that gets there. A tool returning one answer with no alternatives does not
serve that judgment step, it removes it.

Two steps already work the intended way. HTTP format detection emits a
candidate list and the LLM picks by reading response bodies. Unread
resources emit key skeletons and the LLM decides what is worth mapping.
Neither gates, and both hand over evidence rather than a conclusion.

Auth and action detection do not. Both return a single answer,
`AuthDetail.confidence` is serialized and read by nothing, and a failure is
a hard stop rather than a shortlist. The cost is not missed capability, it
is **silent wrong confidence**: `sagemcom/f3896lg-zg` is reported
`form_pbkdf2` with no sign that its login response carries `created.token`,
the one fact that makes it `bearer`. Nothing downstream learns a decision
existed.

**The rule.** Where a detection is ambiguous, report the alternatives and
the wire evidence for each, not the winner alone. Where the capture cannot
settle it, say so and name what a better capture would show. Both are
successes: the first yields a config, the second yields a gap report, and
the mission counts them equally.

**The catalog is the strongest hint.** Committed configs already hold the
discriminating strings — `token_path: created.token`, `action_name:
SetArrisConfigurationInfo`, `todo: reboot`, `fun: 8`. Reporting "N committed
modems declare this shape, here they are" alongside the wire evidence gives
the LLM precedent it can read.

The scan is a hint source, not a detection source. Deriving *detection
itself* from committed configs would require deciding which `modem.yaml`
fields are recognition signal and which are runtime behaviour —
`action_name` is the former, `cookie_name` the latter — a judgment no scan
can make and the LLM makes for free. As a hint source the distinction stops
mattering. Note that
`scan_fleet()` reads only `parser.yaml`; auth and actions live in
`modem.yaml`, which pattern extraction never opens.

**What this measures.** Exact-match grading answers a question the mission
never asked. The fitting measure is whether the correct answer was among the
candidates offered, and whether un-inferable cases were flagged as gaps.
Until detection emits candidates there is nothing to score that way, so the
grades below stand as the interim proxy.

---

## CoreGap: When the Pipeline Stops

A **CoreGap** means the modem uses a pattern that Core doesn't support yet. The pipeline detects it, reports it, and stops. No guessing, no workarounds.

| Gap Category | What It Means | What Core Needs |
| ------------- | --------------- | ----------------- |
| `unmatched_login` | Login POST to an endpoint not in known patterns | New URL pattern in `auth_patterns.json`, or a new auth strategy |
| `auth_unknown` | Auth mechanism doesn't match any known strategy | New auth strategy implementation |
| `unmatched_restart` | Restart action to an unrecognized endpoint | New URL pattern in `action_patterns.json` |
| `unmatched_logout` | Logout action to an unrecognized endpoint | New URL pattern in `action_patterns.json` |

Well-known modems with standard patterns produce zero gaps. Novel modems produce gaps that require a development effort before onboarding can proceed.

**Gap categories are endpoint-level, and cover auth and actions only.** A
data endpoint the generator does not read produces no gap: intake writes a
`parser.yaml` that simply omits it, and the pipeline reports success. That
is how the service flow resource behind issue #185 was captured in the HAR,
fetched successfully, and left unread without anything flagging it. Detection
coverage for data endpoints is measured by intake accuracy, not gated by a
gap category, so a field the pipeline cannot generate shows up as a lower
percentage rather than a stop. The unread-resource report below is what makes
such an endpoint visible; it does not make it a gap.

When the pipeline stops on a gap, the report contains enough detail (phase, category, summary, wire evidence) to file a GitHub issue for the development work.

---

## Unread Resources: What Nothing Looked At

`analyze_har` also reports `unread_resources` — every 2xx JSON endpoint in
the HAR that no part of the generated config consumes. Endpoints reached as
a parser resource, as the auth login endpoint, or as an action are
subtracted; what remains is what nothing asked about.

Each entry carries the path, status, content type, and the response body's
**key skeleton with value types** — never values. Keys are what make the
judgment possible: an LLM recognizes `maxTrafficRate` as a provisioned rate
where `856000000` on its own says nothing. Values are where MAC addresses,
serial numbers, and boot filenames live, and this report flows into an LLM
context and often into a GitHub issue.

**This is not a gate.** Every HAR has unread endpoints — UI preferences,
language lists, LED settings — so a gate here would fail every intake. It
rides alongside `warnings`: always present, informational, never failing.
The pipeline does not classify what the endpoints contain or suggest
mappings; producing the shape is the whole job, and the judgment belongs to
the LLM reading it.

---

## Fleet Patterns (Layer 2)

The pipeline uses a three-layer detection model:

1. **Core baseline** — deterministic heuristics built into `analyze_har`
2. **Fleet patterns** — proven patterns extracted from existing catalog entries
3. **LLM gap-fill** — judgment calls for ambiguous cases

`scan_fleet()` reads all `parser.yaml` files in the catalog and builds a `FleetPatterns` instance containing selector-to-direction mappings, system_info label/ID/JSON-key mappings, delimiters, channel type values, aggregate field patterns, and uptime formats. This is passed to both `analyze_har(fleet=...)` and `generate_config(fleet=...)`.

Fleet patterns grow automatically as new modems are onboarded — each new parser.yaml enriches detection for future modems that share similar patterns.

---

## Data-Driven Extension Points

Three JSON pattern files control what the pipeline recognizes. Adding support for a new login URL, action endpoint, or service flow spelling is a data change, not a code change:

- **`auth_patterns.json`** — known login URL patterns and credential field names. When `analyze_har` sees a POST to a URL matching a pattern here, it classifies the auth strategy.

- **`action_patterns.json`** — known action URLs (logout, restart, reboot). When `analyze_har` sees POST requests matching patterns here, it maps them to modem actions. **URLs only**, which is the limit of this extension point rather than a gap in its data: a firmware that names the action anywhere else is invisible to it no matter what is added to the file. Three families do exactly that — HNAP in the `SOAPAction` header, CBN in the setter's `fun=` code, and form modems in a body parameter such as `todo=reboot`, all of them POSTing to one endpoint that serves every action. HNAP is the one with a code-side substitute: `detect_hnap_actions()` matches action names containing `logout`, `reboot`, `restart` or `reset`, so a vendor spelling like `SetArrisConfigurationInfo` is missed and no data change can reach it. Reading these is a detector capability question, not a pattern-file entry — and the cheaper answer is to report the `SOAPAction` names, `fun=` codes and body parameters a capture contains, with the committed configs that declare the same strings, rather than to encode every vendor spelling. See [Detection Owes the LLM Evidence](#detection-owes-the-llm-evidence-not-a-verdict).

- **`service_flow_patterns.json`** — the wire vocabulary of a service flow resource: which item keys name a direction, which name a provisioned maximum, and which direction spellings are not the canonical words. All are matched case-insensitively.

The files live in Catalog Tools (`solentlabs/cable_modem_monitor_catalog_tools/analysis/`). Extending them is the first step when a CoreGap is reported for an unmatched endpoint.

What stays in code is the output contract, not the wire vocabulary: the canonical direction words and the field names they produce (`provisioned_speed_down` and siblings) define registered fields, so they are not an extension point.

Two more vocabularies are not files at all. Uptime `format` strings and the `docsis_status` spellings meaning "Operational" are harvested from the fleet's committed configs by `scan_fleet()`, so onboarding a modem with a new uptime shape or a new vendor status word extends the candidate list for every modem after it.

---

## Where Things Live

| Artifact | Location |
| ---------- | ---------- |
| Pipeline tools (validate, analyze, enrich, generate, test) | `packages/cable_modem_monitor_catalog_tools/solentlabs/cable_modem_monitor_catalog_tools/` |
| Pattern files (auth, actions) | `.../catalog_tools/analysis/auth/` and `.../catalog_tools/analysis/actions/` |
| Pattern file (service flows) | `.../catalog_tools/analysis/mapping/service_flow_patterns.json` |
| Fleet scanner | `packages/cable_modem_monitor_catalog_tools/solentlabs/cable_modem_monitor_catalog_tools/fleet_scanner.py` |
| Intake pipeline regression (accuracy tracking + auth audit) | `packages/cable_modem_monitor_catalog_tools/scripts/intake_pipeline_regression.py` |
| Test harness (HAR replay, golden file comparison) | `packages/cable_modem_monitor_core/solentlabs/cable_modem_monitor_core/test_harness/` |
| Modem catalog entries (output) | `packages/cable_modem_monitor_catalog/solentlabs/cable_modem_monitor_catalog/modems/{manufacturer}/{model}/` |
| Authoritative spec | `packages/cable_modem_monitor_catalog_tools/docs/ONBOARDING_SPEC.md` |
| Runnable workflow | [MODEM_INTAKE_WORKFLOW.md](MODEM_INTAKE_WORKFLOW.md) |

---

---

## Intake Pipeline Regression

`scripts/intake_pipeline_regression.py` measures how well the pipeline
reproduces committed catalog configs when run against the same HAR as a
fresh submission. It is a **report, not a gate**: `make
intake-regression` (run by `validate-ci` and mirrored in CI) computes
fleet onboarding accuracy fresh from the catalog every run and never
fails the build on accuracy. The durable trend lives in the timestamped
scorecard artifact (`--scorecard`), and per-modem parse correctness is
gated independently by the golden replay tests. Findings indicate where
the intake pipeline — or the capture, to be resolved in har-capture —
can improve, not regressions in Core.

```bash
python packages/cable_modem_monitor_catalog_tools/scripts/intake_pipeline_regression.py
python packages/cable_modem_monitor_catalog_tools/scripts/intake_pipeline_regression.py --modem arris/sb8200 -v
python packages/cable_modem_monitor_catalog_tools/scripts/intake_pipeline_regression.py --scorecard scorecard.json
```

**What it reports:**

| Status | Meaning |
|--------|---------|
| `CLEAN` | Generated golden file matches committed golden file exactly |
| `DRIFT` | Pipeline ran but generated output differs from committed golden file |
| `FAILURE` | Pipeline stage failed (validate_har, analyze_har, generate_config) |

Fleet-wide **field accuracy** is reported as a percentage of committed
golden file fields correctly reproduced by the pipeline. This tracks
improvement over time as the pipeline gains new pattern recognition.

A HAR the pipeline cannot process scores **zero against its full field
count**, never dropping out of the denominator: a modem the pipeline
fails outright is its worst result, not an absent one, and excluding it
would raise the percentage. Only the `INCOMPLETE HARS` list is excluded,
because a capture that never recorded the flow measures the capture
rather than the pipeline.

**Which config a HAR is graded against.** A modem directory may hold
several HARs, each capturing a different auth variant alongside its own
`modem-<variant>.yaml`. Both grades resolve the committed config from
the HAR stem via Core's `resolve_modem_config()` — exact match, then
stem walk, then `modem.yaml` — the same rule the golden replay tests
use. Grading every HAR against the base `modem.yaml` instead compares a
capture to a config that does not describe it, and reports the
disagreement as a pipeline defect: before this was fixed, the four
SB8200 variants and the SB6190 nonce capture produced five auth
`mismatch` lines in which the detector and the catalog in fact agreed.

**Actions grading** compares pipeline-detected logout/restart actions
against the committed config per HAR
(`analysis/actions/grading.py`):

| Grade | Meaning |
|-------|---------|
| `match` | Type, identity (method + endpoint, or hnap action_name), and params all reproduced |
| `partial` | Identity matches; params differ, are missing, or json_body not produced |
| `pipeline_only` | Pipeline detected an action the catalog never adopted — candidate enrichment, or a false positive |
| `committed_only` | Committed action the pipeline cannot produce from the HAR (human-authored config, or action never fired during capture) |
| `mismatch` | Type, endpoint, method, or action_name disagree — investigate which side is wrong |

Human-authored fields a HAR cannot show (`pre_fetch_action`,
`action_auth`, `requires_session`, response keys) are out of grading
scope.

**A detected action is a candidate, not a finding.** `pipeline_only` is
the expected grade for most of them, and adopting one is a runtime
decision the capture cannot make. `actions.logout` is the clear case:
declaring it makes logout fire after every successful poll and before a
same-poll auth retry ([MODEM_YAML_SPEC.md § Single-session
modems](../../cable_modem_monitor_core/docs/MODEM_YAML_SPEC.md)). It is
the remedy for single-session firmware, evidenced by a second login
failing while one is active — which a capture of one session cannot
show. Seeing `GET /Logout.htm` means the user clicked logout while
capturing; that the endpoint exists is not a reason to call it every
poll. Restart is likewise gated by hardware confirmation, since the
catalog does not ship a reboot button nobody has pressed. Read the
`pipeline_only` list as a shortlist for a human, never as a diff to
apply.

**`committed_only` separates into two causes**, and which one applies is
decided by checking the HAR for the committed action rather than
assumed. Either the action never fired while capturing — the common
case, and nothing to fix in the pipeline — or it fired and was still not
produced, which means its intent was expressed outside the URL and
`action_patterns.json` could not see it (see [Data-Driven Extension
Points](#data-driven-extension-points)). A third, narrower case is a
committed endpoint no capture can yield: `sagemcom/f3896lg-zg` declares
its logout as `/rest/v1/user/{auth:user_id}/token/{auth:token}`, and the
generator has no template vocabulary to produce placeholders with.

**Auth grading** compares the pipeline-generated auth block against the
committed config (`analysis/auth/grading.py`), using the same grade
taxonomy on two items: `strategy` (the detected auth strategy — the
headline capability) and `fields` (everything else in the block —
endpoints, field names, cookie names, nested success criteria). Fields
are only graded when the strategy matches; comparing field layouts of
two different strategies is meaningless. Unlike actions (graded from
analysis output), auth is graded from the generated config, so it
requires generation to succeed.

**Auth strategy mismatches that are correct by design.** A `strategy:
mismatch` normally means one side is wrong, but eight standing lines are
none of them a catalog error. They are reported, never suppressed — the
report states what the pipeline can do, and hiding a known limit would
make it read as capability. Four causes:

| Cause | Modems |
|-------|--------|
| **No branch for the strategy.** The HTTP tree walks none → basic → url_token → form_sjcl → form_pbkdf2 → form_nonce → form. `form_cbn` and `bearer` are not in it, so it cannot emit them | `arris/sb8200-cbn`, `compal/ch7465mt` (`form_cbn`); `sagemcom/f3896lg-zg` (`bearer`) |
| **Capture carries no evidence.** The committed strategy is right about the hardware; the HAR cannot show it | `netgear/c7000v2`, `technicolor/tc4400` — committed `basic`, but zero 401 challenges and zero `Authorization` headers. `arris/tg3442de` — committed `form_sjcl`, but both login POST bodies are `{}`, so the SJCL fields the branch keys on are gone and the login URL falls through to the PBKDF2 bucket |
| **Action-scoped auth read as primary.** `auth: none` plus `actions.restart.action_auth: bearer` — the only login in the capture fired for the restart action, and the data path really is unauthenticated | `sagemcom/f3896lg-vmb` |
| **Credential shape the detector cannot name.** The login posts `arguments=<base64 of user:pass>`; the credential test is field-name based, so a generic `arguments` parameter reads as carrying no credentials | `arris/sb6190` (b64 variant) |

None of these is a catalog defect, and no entry above should be changed to
make a line turn green. Nor does closing them require teaching the detector
every shape: each one has wire evidence that would let a reader settle it —
`created.token` in a login response, `fun=` codes on a setter endpoint, a
login POST that precedes only a reboot. Reporting that evidence and the
matching catalog precedent is the fix, per [Detection Owes the LLM
Evidence](#detection-owes-the-llm-evidence-not-a-verdict). The two `basic`
lines have no evidence in the capture at all, and their correct outcome is a
gap report asking for a clean recapture.

**Auth fixture audit** runs at the end of every sweep. For each form-auth
modem with `login_page` configured, it verifies that the committed HAR
fixture contains a usable login page response. Issues are printed as
advisory — they indicate catalog gaps to investigate, not CI failures.
Hardware is required to confirm whether a fixture gap actually causes a
runtime problem.

**Trend tracking** is the scorecard (`--scorecard`), a timestamped,
commit-stamped JSON snapshot of fleet accuracy and per-modem grades.
CI uploads it as an artifact every run, so accuracy over time is read
from the scorecard history rather than a committed baseline. Adding a
modem needs no index or baseline update — discovery walks the catalog
tree and the new HAR is included automatically on the next run.

The reusable machinery (scorecard building, result classification)
lives in the unit-tested
`solentlabs/cable_modem_monitor_catalog_tools/regression/` package and
is generic over grade dimensions; the script supplies discovery,
pipeline stages, and printing. The shared grade taxonomy is
`solentlabs/cable_modem_monitor_catalog_tools/grading.py`.

---

## Catalog Field Sweep

`scripts/catalog_field_sweep.py` reads every committed entry's HAR for
keys that resolve to a registry field, and reports the ones that
entry's own `parser.yaml` and golden file never populate. Like the
regression above it is a **report, not a gate**, and nothing it finds
is wired automatically — each hit is a candidate a human decides on.

```bash
python packages/cable_modem_monitor_catalog_tools/scripts/catalog_field_sweep.py
python packages/cable_modem_monitor_catalog_tools/scripts/catalog_field_sweep.py --modem arris/sb8200
python packages/cable_modem_monitor_catalog_tools/scripts/catalog_field_sweep.py --json
```

**Against unread resources.** That report is endpoint-granular, has no
registry knowledge, and runs at intake on a HAR with no committed
config yet. It cannot see an endpoint that is read but read
incompletely: the SB8200's `/cmconnectionstatus.html` is a parser
resource, so it is never unread, and its `Boot State` row goes unmapped
in silence. This sweep is field-granular, registry-aware, and runs over
the already-committed catalog.

**Against the intake regression.** That compares a *generated* config
against the committed one — pipeline capability. This compares a
*capture* against the committed config — catalog coverage. A field the
generator misses but the committed config already has belongs to the
regression, not here.

**What counts as extracted** is the committed `parser.yaml` plus the
committed golden. A field reaches the output without a `field:` line
through `channel_type: {fixed: …}`, `fixed_fields`, the XML
`lock_status: {all_of: […]}` derivation, an `aggregate:` total, or a
`parser.py` post-processor. Reading the golden alongside the config
covers the last of those, which no static read of the YAML can see.

**Scope.** Only fields with a canonical home surface, and the
vocabulary is the shipped alias maps (`field_registry.json`,
`mapping.system_info`, `mapping.service_flows`). Serial numbers, MAC
addresses, boot filenames, event logs and MTA lines have no registry
field — they are new-schema questions gated by
[ARCHITECTURE_DECISIONS.md § Core Schema Model](../../cable_modem_monitor_core/docs/ARCHITECTURE_DECISIONS.md),
and several are PII-adjacent. Keys are printed, never values, for the
same reason as unread resources.

**Known blind spots**, all consequences of reading names off the wire:

| Blind spot | Why |
|------------|-----|
| HNAP and JS-embedded modems | Channel fields arrive positionally inside a delimited string; there are no keys to read |
| Direction | A channel key carries no direction, so a field counts as extracted when *either* downstream or upstream populates it |
| Unregistered spellings | A firmware key no alias map lists resolves to nothing, so the field it carries cannot be named |

The reusable logic lives in the unit-tested
`solentlabs/cable_modem_monitor_catalog_tools/analysis/field_sweep.py`;
the script supplies discovery and printing.

---

## Further Reading

- [ONBOARDING_SPEC.md](ONBOARDING_SPEC.md) — full tool contracts, decision tree (7 phases), validation rules, worked examples, error handling
- [MODEM_REQUEST.md](../../../docs/MODEM_REQUEST.md) — contributor guide for submitting HAR captures
- [MODEM_YAML_SPEC.md](../../cable_modem_monitor_core/docs/MODEM_YAML_SPEC.md) — modem config schema and transport constraints
- [PARSING_SPEC.md](../../cable_modem_monitor_core/docs/PARSING_SPEC.md) — parser config schema
