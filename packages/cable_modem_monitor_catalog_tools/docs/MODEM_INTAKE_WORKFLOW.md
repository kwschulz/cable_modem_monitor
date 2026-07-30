# Modem Intake Workflow

Onboard a new cable modem from a HAR capture to a tested catalog entry.
If the modem uses patterns Core already knows, onboarding is fully
automated. If the modem uses something new, the pipeline stops with
a clear report of what Core needs to support.

> **The HAR capture is the only authoritative input.** Every config
> decision traces to wire evidence in the capture. Without a complete
> HAR — including the authentication flow — the pipeline cannot run
> and a parser cannot be built. Recapture is always the answer to a
> bad HAR; there is no workaround.

<!-- -->

> **Authoritative spec:** [ONBOARDING_SPEC.md](ONBOARDING_SPEC.md)
> covers tool contracts, decision trees, validation rules, worked examples,
> and error handling in full detail. This document is the runnable workflow.

## Audience

This walkthrough is for anyone with a HAR capture who wants to produce
a draft catalog entry — whether that's the modem owner working on their
own hardware, or a contributor helping triage someone else's submission.

The pipeline tooling is plain Python, but the judgment work — format
detection on ambiguous HTML, metadata enrichment, test failure
diagnosis, modem config shaping — realistically benefits from an AI
assistant. This project itself was built with [Claude Code](https://claude.com/claude-code).
If you have access to a similar AI tool, treat it as the expected
helper for the judgment steps; if not, expect those steps to take
more reading and iteration against the specs.

> Throughout this doc: when you're triaging someone else's HAR, anything
> that asks "you" to confirm a value routes back to the original filer.
> When you're working on your own modem, you confirm with yourself.

## Prerequisites

- Repo cloned and the dev environment working (`make validate` green).
  See [docs/setup/GETTING_STARTED.md](../../../docs/setup/GETTING_STARTED.md)
  for one-time setup. Standard setup installs Core, Catalog, and
  catalog_tools together in editable mode.
- A `.sanitized.har` file. Capture is done with
  [solentlabs/har-capture](https://github.com/solentlabs/har-capture),
  this project's own tool — which means it's editable when a modem
  needs special handling (pre-flight headers, custom URL filters,
  non-standard auth flows). PRs to extend `har-capture` itself are
  welcome. For the standard capture walkthrough, see
  [docs/MODEM_REQUEST.md](../../../docs/MODEM_REQUEST.md). If a HAR
  has cookies on the first request and no auth flow, recapture in
  incognito/private browsing — the pipeline will reject it.

## HAR Captures Are Immutable Evidence

A HAR represents actual hardware we do not own or control. Never
edit one — not to fix malformed HTML, normalize endpoints, or tidy
payloads. Broken markup in a capture is firmware behavior the parser
must handle, not noise to clean: correcting bad tags in a HAR once
caused a modem that really returns bad tags to be processed
incorrectly. The only sanctioned transformation is PII value
sanitization at intake, which replaces values and never structure.

### Assembled fixtures

Not every fixture is a wire capture. Before har-capture existed,
entries were routinely assembled from user-supplied HTML and
diagnostics (`creator` values like `fixtures_to_har (synthetic)`,
`synthetic`, and `composite (fixture + har-capture)` across the
fleet record this). The practice is legitimate; this section
codifies the rules it must follow.

An assembled fixture is not an edited capture — it is a new artifact
that must declare itself:

- `log.creator.name` identifies the fixture as assembled (the fleet
  convention: synthetic fixtures self-identify in `creator`).
- `log.comment` in the HAR states how it was assembled, names the
  sources and the evidence issue, and lists everything not observed
  on the target unit.
- The `modem.yaml` `notes:` block repeats the provenance and says the
  fixture is replaced when a real full-session capture arrives.
- The assembly is only valid when it round-trips: run the parser over
  the assembled HAR and diff the output against the evidence — zero
  mismatches required.

**Partial evidence.** Sometimes no full session exists: a contributor
can share diagnostics or page source but not run a capture, a sanitizer
destroyed the response bodies, or only some endpoints were recorded. A
fixture built from what exists is legitimate — it is what makes the
entry testable — but no capture tool produced it, so it has no
`log.creator`. The audit renders these as `generated`.

Declaration rules still apply: name the sources, list what was not
observed, round-trip the parser against the evidence. Never retro-label
by editing the HAR, since real captured bytes may be mixed in. A
full-session capture replaces the fixture whenever one arrives.

Two further forms are distinguished by evidence basis and status:

**Reconstruction (sibling template).** When no HAR exists but a
sibling model's live parse output proves structural compatibility (a
diagnostics capture showing the sibling's parser fully extracting
channels on the new hardware), the sibling's HAR is the structural
template and every parsed value is substituted with the real values
observed in the diagnostics. `log.comment` lists every unobserved
template-filler field (e.g. lock status, symbol rates); the
round-trip diffs against the diagnostics. Because the structure was
never observed on the target hardware, the entry ships as
`status: awaiting_verification`, never `confirmed`. First use:
Technicolor XB8 (CGM4981COM), issue #101, reconstructed from the XB7
fixture plus a v3.12.0 diagnostics capture.

**Hybrid (same device, maintainer-declared).** When the evidence for
one physical device is spread across multiple real captures — e.g.,
an early capture with intact data endpoints and a later capture of
the same unit that alone covers new pages or actions but whose data
responses were destroyed by a sanitizer bug — the most complete
session is the template and the bodies it lost are substituted from
the other referenced capture(s). Every byte comes from a referenced
capture; nothing is fabricated, and no values are edited beyond the
sanctioned PII value redaction. `log.comment` lists every
transplanted entry and states the same-device and firmware
continuity evidence (device identity fields, byte-identical
firmware-served assets, the evidence issue's history); the
round-trip must reproduce the prior golden exactly, adding only
fields the newly covered pages enable. Because every byte is a real
observation of the specific unit, the entry keeps its status — it
continues to rest on its own verification evidence. First use:
Sercomm DM1000, issue #92 — the 2026-06-18 session capture as
template, data endpoint bodies from the 2026-01-05 capture of the
same unit.

## Inputs

You provide one of:

- A HAR file path (local) — most common when you're working on your own modem.
- A GitHub issue number with an attached HAR — when triaging a submission.
- A modem manufacturer + model name — looks up an existing HAR in the catalog (useful for re-running the pipeline on a known-good HAR, e.g. after a Core change).

## Pipeline Flow

```text
validate_har -> scan_fleet -> analyze_har(fleet) -> [check for gaps]
    -> enrich_metadata -> generate_config(fleet) -> generate_golden_file
    -> write_modem_package -> run_tests -> [fix failures] -> show changes
```

Two outcomes:

- **Clean pipeline (no gaps):** produces catalog files
- **Gaps detected:** reports what Core doesn't support and stops

## Step 1: Obtain HAR

If you have a local file (your own capture, or one downloaded from an
issue), use the path directly.

If you're triaging a submitted issue:

```bash
gh issue view <number> --json body,comments
```

Extract the HAR attachment URL from the body and download it to a temp
directory.

## Step 2: Validate HAR

```python
from solentlabs.cable_modem_monitor_catalog_tools.validate_har import validate_har
result = validate_har(har_path)
```

If `result.valid is False`: stop and address the issues before going
further. Validation catches structural problems and missing auth flows
early — there is no point scanning the fleet or running analysis on a
bad HAR. Common fix: HAR was captured against an existing session
(post-auth). Recapture in incognito/private browsing.

## Step 3: Scan Fleet Patterns

```python
from solentlabs.cable_modem_monitor_catalog import CATALOG_PATH
from solentlabs.cable_modem_monitor_catalog_tools.fleet_scanner import scan_fleet

fleet = scan_fleet(CATALOG_PATH)
```

Fleet patterns augment Core's baseline detection with proven patterns
from existing modems (selector directions, system_info labels, aggregate
fields). Pass `fleet` to both `analyze_har` and `generate_config`.

## Step 4: Analyze HAR

```python
from solentlabs.cable_modem_monitor_catalog_tools.analyze_har import analyze_har
result = analyze_har(har_path, fleet=fleet)
analysis = result.to_dict()
```

Check three outputs:

1. **`hard_stops`** — if non-empty, report and stop
2. **`warnings`** — note for later, don't stop
3. **`core_gaps`** — if present, report and stop (Step 5)

Report what was detected:

- Transport: `{analysis["transport"]}`
- Auth: `{analysis["auth"]["strategy"]}` (confidence: `{analysis["auth"]["confidence"]}`)
- Actions: logout={observed/source_inferred/none}, restart={observed/source_inferred/none}
  - `observed` — request appeared in HAR traffic (highest confidence)
  - `source_inferred` — endpoint referenced in captured page source or matches a
    working family-member modem in the catalog (add to config; flag for contributor
    confirmation that the endpoint works and, separately, whether a Cookie header
    appears in the logout request — `requires_session: true/false` is behavioral
    and cannot be inferred from the HAR alone)
  - `none` — not found by either method; request from the contributor before adding config
- Sections: list formats and channel counts

For source_inferred actions found at a `$.ajax({...})` call site, the
method and data params are extracted from the call site itself (see
ONBOARDING_SPEC § Source-Inferred Call-Site Extraction). Params that
resolve — `{cookie:<name>}` directives and string literals — need no
manual work. A warning naming an unresolved param (a value computed in
page script) marks the one remaining judgment step: read the quoted
expression in the page source and resolve the value by hand. Never
invent a value the page source doesn't support; if it can't be traced,
leave the param out and ask the contributor.

Two rules govern this layer:

- **Patterns come from confirmed modems only.** New URL regexes in
  `action_patterns.json` and new call-shape support in the extractor
  are authored from hardware-confirmed modems. Unconfirmed intakes
  consume patterns; they never contribute them. Credential field
  names need no authoring at all: every committed `modem.yaml`
  `password_field` feeds detection automatically (see ONBOARDING_SPEC
  § Phase 2).
- **Every extraction rule must fire on at least one committed fleet
  HAR.** A proposed rule with zero fleet matches is dead weight built
  for a hypothesis — reject it. `make intake-regression` grades
  detected actions against committed modem.yaml files and ratchets
  them against the fleet baseline (see INTAKE_PIPELINE.md § Intake
  Pipeline Regression) — an extractor change must improve grades or
  leave them unchanged.

If `auth.confidence` is not `high`, or `warnings` is non-empty for the auth
entry, verify the detected strategy against the HAR before proceeding. Pull
`analysis["auth"]["fields"]` and cross-check:

| Strategy | Key signal in the HAR |
|----------|-----------------------|
| `form` | Login is a POST with plain form fields. If `login_page` is set, a pre-fetch GET precedes the POST and hidden fields appear in the POST body. Check `encoding` — `base64` if the password is base64-encoded in the POST body, `plain` if it appears verbatim. |
| `form_nonce` | Auth response body starts with `Url:` (success) or `Error:` (failure) — no HTTP redirect. The POST body contains a short random numeric value alongside credentials; `nonce_field` should match its field name. |
| `form_pbkdf2` | A preliminary request fires before credentials are submitted and the response contains a salt value. `pbkdf2_iterations` and `pbkdf2_key_length` should match values visible in that exchange. |
| `form_sjcl` | The credential POST body is an encrypted SJCL JSON blob, not plain form fields. `encrypt_aad` and `decrypt_aad` should match the AAD strings in the login JS. |

If the detected strategy or any extracted field looks wrong, correct
`analysis["auth"]` before calling `generate_config` — don't patch the
generated YAML after the fact.

## Step 5: Check for Core Gaps

If `analysis["core_gaps"]` is present, the modem uses a pattern Core
doesn't support yet. **Stop config generation.** Report:

1. What was successfully detected (transport, session, etc.)
2. For each gap:
   - **Category**: what kind of pattern is missing
   - **Summary**: human-readable description
   - **Evidence**: wire data from the HAR
3. Suggest next steps:
   - `unmatched_login`: new login URL pattern needed in `auth_patterns.json`,
     or a new auth strategy needed in Core
   - `auth_unknown`: new auth strategy needed in Core
   - `unmatched_restart` / `unmatched_logout`: new action URL pattern needed
     in `action_patterns.json`

Format the report so it can be pasted into a GitHub issue for a
development effort. Do NOT try to resolve gaps by patching the
analysis dict.

If no gaps: proceed to Step 6.

## Step 6: Enrich Metadata

```python
from solentlabs.cable_modem_monitor_catalog_tools.enrich_metadata import enrich_metadata
enrich_result = enrich_metadata(analysis, existing_config=None, user_input=user_metadata)
metadata = enrich_result.metadata
```

For each item in `enrich_result.missing`:

- **hardware.chipset**: web search `"{manufacturer} {model} chipset"`
- **hardware.docsis_version**: check if OFDM channels detected (= 3.1), else 3.0.
  Never conclude 4.0 from channel data alone — D4.0 reuses 3.1's OFDM/OFDMA
  channel types (see MODEM_YAML_SPEC § Hardware for the sourced reference);
  `"4.0"` requires a hardware source — chipset or manufacturer specs
- **hardware.release_date**: web search the launch announcement
  (manufacturer press release, ISP rollout coverage). An FCC grant or
  dated manufacturer manual is an acceptable proxy — label it as such
  in `sources.release_date`. Feeds the catalog README timeline;
  entries without it are silently omitted from that rendering.
- **isps**: web search `"{model} compatible ISPs"`
- **default_host**: usually 192.168.100.1 for DOCSIS modems
- **brands**: user-visible box branding. Check firmware brand fields in
  the HAR (e.g. `customer`) and retail listings. Entries become
  manufacturer-dropdown buckets and must carry a source.
- **model_aliases**: alternate user-facing names only — rebadges,
  regional variants, sticker codes, each with a source. Firmware
  `product`/platform codes stay in the HAR as evidence; they never go
  in aliases (this is how the G54 got bad aliases, see #72).

`manufacturer` is stored as the firmware reports it and determines the
catalog directory; box branding that differs goes in `brands`. Full
research guidance: ONBOARDING_SPEC.md § Fields to research.

Confirm any values you can't find rather than guessing.

## Step 7: Generate Config

```python
from solentlabs.cable_modem_monitor_catalog_tools.generate_config import generate_config
result = generate_config(analysis, metadata, fleet=fleet)
```

If `result.validation.valid is False`:

- Read the errors
- Fix the analysis dict or metadata
- Retry

Review the generated YAML before proceeding.

## Step 8: Generate Golden File + Write Package

```python
from solentlabs.cable_modem_monitor_catalog_tools.generate_golden_file import generate_golden_file
golden = generate_golden_file(str(har_path), result.parser_yaml)
```

Report channel counts for sanity check:

- Downstream: N channels, fields: [list]
- Upstream: N channels, fields: [list]
- System info: [list of fields]

If `result.missing_system_info_fields` is non-empty, inspect the HAR for
those fields before proceeding. Search the response bodies for the field
name or a likely label (e.g., "uptime", "Up Time") and add the mapping to
the parser.yaml. A parser that ships without a Tier-1 field will fail
`verify_diagnostics` at confirmation time.

`generate_golden_file` runs the parser coordinator, not the
orchestrator, so its output omits every orchestrator-derived field —
`rate_corrected` / `rate_uncorrected`, signal classification, status
derivation. Use it for the channel-count and field sanity check above.
The committed golden comes from the orchestrated replay in Step 9,
which is what CI compares against.

Then write the catalog package:

```python
from solentlabs.cable_modem_monitor_catalog_tools.write_modem_package import write_modem_package
write_result = write_modem_package(output_dir, ...)
```

See [ONBOARDING_SPEC.md](ONBOARDING_SPEC.md) for the full
`write_modem_package` signature.

## Step 9: Run Tests

The authoritative check is the catalog suite — it replays the HAR
through the full orchestrator, which is what CI runs:

```bash
.venv/bin/python -m pytest packages/cable_modem_monitor_catalog/tests/ \
  --no-header -q -k "<model>"
```

On a golden mismatch the replay writes `modem.actual.json` next to the
HAR. Once the diff is what you intend, promote it to
`modem.expected.json` and re-run.

`catalog_tools.run_tests(modem_dir)` exists as an MCP tool and returns
the same structured diff, but it calls the non-orchestrated pipeline,
so a modem whose golden carries orchestrator-derived fields fails there
while passing CI. Diagnose with it if you like; decide with pytest.

If tests fail, diagnose from the structured diff:

- **Auth failure**: check modem.yaml auth fields against HAR login flow
- **404 on resource**: check parser.yaml resource paths vs HAR URLs
- **Empty channels**: check column indices/field offsets
- **Golden mismatch**: compare field-by-field diff

Fix the config, re-run. Loop until green.

The new modem is picked up automatically — discovery walks the catalog
tree, so there is no index or baseline to update. `make
intake-regression` simply includes it in the accuracy report on the
next run.

## Step 10: Regenerate Catalog README

Run the generator to keep the catalog index current:

```bash
python3 packages/cable_modem_monitor_catalog/scripts/generate_catalog_index.py
```

Stage `README.md` and `CATALOG_AUDIT.md` alongside the catalog files —
CI gates on README freshness and a stale README will fail the PR.

## Step 11: Show Changes

Run `git status` to see all files created or modified, and
`git diff --stat` for a summary. Do NOT commit or push automatically —
staging and commits are yours to make.

## Step 12: Open a Pull Request

Once tests are green and the diff looks right:

1. Create a branch, stage the new catalog files, commit with a clear
   message (e.g. `Add catalog entry for {manufacturer} {model}`).
2. Open a PR against `main` (or the active release branch). Reference
   the originating issue with `Related to #N` — never `Fixes #N`
   (see [CONTRIBUTING.md § Issue Closing Policy](../../../CONTRIBUTING.md#issue-closing-policy)).
3. Include in the PR description: the verdict from the HAR audit (if
   you ran one), channel counts from Step 8, and any unresolved
   warnings from `analyze_har`.

A maintainer reviews and merges. The modem lands in the catalog with
`status: awaiting_verification`. The Confirmation Phase below closes
the loop once a contributor reports the parser working on real
hardware.

## Confirmation Phase

After the catalog entry ships in a release, the contributor who filed
the original request (or any user with matching hardware) eventually
reports back from a real install. When their diagnostics show the
parser working, the maintainer captures that evidence as a
`*.verified.json` fixture and flips the `modem.yaml` status to
`confirmed`. This phase is maintainer-side; the contributor's only
job is to share their HA diagnostics download.

### Step 13: Receive Hardware Confirmation

Trigger: the originating issue gets a comment with a config-entry
diagnostics JSON attached and a positive report (channels populated,
no errors, "it works").

Open the diagnostics JSON and sanity-check:

- `data.modem_data.error` is empty
- `data.data_coordinator.last_update_success` is `true`
- `data.downstream_channels` and `data.upstream_channels` have the
  expected counts and locked entries with full fields (frequency,
  power, snr, corrected/uncorrected where applicable)
- `data.system_info` populated (docsis_status, system_uptime,
  hardware_version, software_version, total_corrected,
  total_uncorrected)
- `data.config_entry.variant` matches the variant the contributor
  used (relevant for multi-variant modems — see Gotchas)

If anything is missing or wrong, treat it as another iteration: ask
clarifying questions, ship a patch alpha, and wait for fresh
diagnostics. Do NOT confirm partial wins.

### Step 14: Build verified.json

The verified.json is a faithful copy of the diagnostics `data`
section, with integration-side noise stripped and provenance metadata
prepended. It is **not** a curated subset — keep all modem-side data
verbatim so future schema work can diff against real hardware output.

Strip these top-level keys from `data`:

- `home_assistant`
- `custom_components`
- `integration_manifest`
- `setup_times`
- `_solentlabs`
- `_review_before_sharing`
- `recent_logs`

Prepend at the top of the resulting object:

```json
{
  "verified_at": "YYYY-MM-DD",
  "version": "<release tag, e.g. 3.14.0-beta.1>",
  ...
}
```

Render channel arrays compact — one channel object per line:

```json
"downstream_channels": [
  {"lock_status": "locked", "channel_type": "qam", "channel_id": 17, ...},
  {"lock_status": "locked", "channel_type": "qam", "channel_id": 18, ...},
  ...
]
```

This is the convention used by the majority of existing verified.json
files. Keep dict bodies indented normally; only the channel-array
elements are compact.

Save to: `packages/cable_modem_monitor_catalog/.../<manufacturer>/<model>/test_data/<variant>.verified.json`.

For single-variant modems, the file is `modem.verified.json`. For
multi-variant modems, name it after the variant the contributor used
(e.g. `modem-basic.verified.json`).

### Step 15: Flip Status

Edit `modem.yaml` (or the variant-specific YAML):

```yaml
status: awaiting_verification  →  status: confirmed
```

For multi-variant modems, **only flip the variant the contributor
verified.** A confirmation on one variant does not transfer to the
others — each variant exercises a different transport/auth path and
must be verified independently.

### Step 15a: Run Catalog Tests

Run the full catalog test suite before committing:

```bash
.venv/bin/python -m pytest packages/cable_modem_monitor_catalog/tests/ --no-header -q
```

This is ordinary pre-commit verification, not a promotion gate. Every
test here already ran on the commit that added the entry:
`test_modem_golden_spec_conformance` covers every modem regardless of
`status:`, so flipping to `confirmed` activates nothing new. A failure
means this step's edits broke something, not that latent drift surfaced.

Fix any failures before proceeding to Step 16. If the golden needs
updating after a parser fix, promote the `modem.actual.json` the
failing replay writes next to the HAR and re-run until clean.

### Step 16: Commit and Reply

Stage the two files and commit with this message shape:

```text
feat(catalog): mark <Make> <Model> [(<variant>)] as confirmed

Verified by @<github-handle> on <release-tag> hardware (HW <ver>,
SW <ver>, <uptime or other distinguishing detail>, <DS> DS + <US> US
locked, all signals nominal).

Refreshes <variant>.verified.json from their diagnostics; flips
<modem.yaml | modem-<variant>.yaml> status awaiting_verification ->
confirmed. <Note any sibling variants that remain awaiting_verification
and what would be needed to confirm them.>

Related to #<issue>
```

Reply on the originating issue with a short, personal close: thank
the contributor, point them at the `Cable Modem Monitor: Generate
Dashboard` action under **Developer Tools > Actions**, and invite a
fresh issue if anything looks off later. Then close the issue.

## Confirmation Gotchas

These came out of real confirmations and are easy to miss on a first
pass.

### Channel-array format

Compact (one channel per line) is the convention. If you generate a
verified.json with `json.dumps(..., indent=2)` you'll get the
expanded form by default — reformat the channel arrays before
committing. The compact form keeps the diff readable when fixture
data churns and matches every other file in the catalog.

### verified.json is a point-in-time snapshot

Each `verified.json` reflects what the integration emitted at the
moment of confirmation, against the integration version cited in the
file's `version` field. The integration's diagnostics shape evolves
over time (fields get added, renamed, restructured) — that's normal,
and historical fixtures don't need to track those changes.

**Match the current diagnostics shape on the file you're writing
now.** Don't normalize older files retroactively. If a future
diagnostics change makes a historical fixture genuinely obsolete,
re-confirmation is the answer, not in-place editing.

### Catalog directory vs self-reported model

A modem's catalog directory name doesn't always match the model
string the firmware reports in diagnostics. For example, the modem
may self-report as `S33` while the catalog directory is `s33v2` to
distinguish hardware revisions. The `verify_diagnostics` CLI accepts
`--manufacturer` / `--model` overrides to bridge this gap.

### Multi-variant modems

A modem with `modem.yaml` plus one or more `modem-<variant>.yaml`
files (e.g. `modem-basic.yaml` for a Basic Auth variant of the same
hardware) has independent test_data and independent status per
variant. Each variant is its own confirmation surface:

- One contributor's confirmation only validates the variant their
  config_entry shows in `data.config_entry.variant`
- Sibling variants stay `awaiting_verification` until a different
  contributor (or the same contributor in different conditions)
  exercises that path
- The commit message must name which variant was confirmed and call
  out which siblings remain unverified

### Diagnostics partial wins

A diagnostics JSON can show "most things working" — channels
populated, latency healthy — while one or two `system_info` fields
are still null. That's not a confirmation; that's an alpha cycle.
Confirm only when the full system_info block is populated and there
are no errors in `modem_data`.

## Key Rules

1. **HAR is the authority.** Two tiers of wire evidence are valid:
   - **Observed** — the request appears in HAR traffic. Use directly, full confidence.
   - **Source-inferred** — the endpoint is referenced in captured page source AND/OR
     confirmed by a working family-member modem in the catalog. Add to config and note
     as inferred; the pipeline marks these automatically via `source: source_inferred`.
   If neither condition is met, request the information from the contributor. Do not guess.
2. **Known patterns are automated.** If the pipeline can classify
   everything, config generation is deterministic. No LLM reasoning
   needed for the common case.
3. **Unknown patterns are Core gaps.** If the pipeline can't classify
   something, stop and report. Don't try to resolve it -- that's a
   development effort, not an intake task.
4. **Only the catalog changes.** Onboarding a new modem should only add
   files to `packages/cable_modem_monitor_catalog/`. If Core changes
   are needed, that's a gap to flag.
5. **Iterate on test failures.** Expect the first run to fail. Diagnose,
   fix, re-run. This is normal.
6. **Never commit automatically.** Show changes; stage them yourself.
7. **Alias vs separate entry.** Each model a user would purchase by name
   gets its own catalog directory — but a separate entry requires its
   own HAR evidence; until a capture exists, a rebadge is recorded as a
   sourced alias on the evidenced entry. Aliases hold alternate
   user-facing names only (rebadges, sticker codes); firmware-internal
   codes belong in neither aliases nor brands. Box branding goes in
   `brands` and becomes a manufacturer-dropdown bucket.
   See [MODEM_YAML_SPEC.md](../../cable_modem_monitor_core/docs/MODEM_YAML_SPEC.md) § Aliases vs Separate Entries.
8. **Consolidate issue resources.** When a HAR is incomplete (e.g.,
   missing XHR due to Playwright `networkidle` bug), extract data from
   screenshots, embedded HTML/JS, and user confirmations before building
   the fixture. Note provenance in `modem.yaml` sources.
9. **Logout is an operational requirement — confirm it works.** Logout prevents
   stale server-side sessions from blocking re-authentication. For every modem
   with `actions.logout` configured, ask the contributor to confirm the endpoint
   responds (any 2xx or redirect) after a successful login. Set
   `requires_session: true` when the captured logout request carried the
   credential its auth strategy uses: a `Cookie` header for cookie-based
   strategies, an `Authorization` header for `bearer`. Set it to `false`
   (the default) when the request carried no credential at all. Judge by the
   credential, not by the presence of any cookie — the F3896LG logout carries
   a `sidebar_state` UI cookie that authenticates nothing, while the header is
   what the endpoint actually needs.
10. **Fixture values trace to observed artifacts.** Every value written into a
    golden file or verified.json must come from a real artifact — the
    contributor's diagnostics, a HAR, page source, or a posted screenshot. Never
    invent a value to fill a gap or make a fixture look complete (no plausible
    channel reading, no placeholder uptime, no "typical" SNR). A missing field
    stays null, or the entry isn't confirmed yet (see Diagnostics partial wins).
    A fixture that looks correct but holds a hand-filled value is wrong even when
    the value happens to be right — the next contributor's real data won't match.
