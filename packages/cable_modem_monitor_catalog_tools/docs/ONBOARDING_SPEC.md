# Modem Onboarding Specification

An MCP server that provides structured tools for modem onboarding —
from HAR analysis through config generation, testing, and commit.

**Who uses this:** Contributors submit HAR captures via GitHub issues —
no AI tooling required. The MCP server is a maintainer tool for
processing those submissions. Any MCP-compatible AI client can
orchestrate the tools (Claude Code, Gemini CLI, Cursor, Cline, or
any other MCP client). Manual config creation also works — the specs
are the authority, not the MCP.

**Architecture:** MCP tools handle deterministic steps (HAR parsing,
transport detection, config generation, validation, test execution).
The LLM handles judgment calls (ambiguous HTML formats, metadata web
search, test failure diagnosis). The user handles approval (golden file
review, commit authorization).

**Design principles:**

- HAR is the single source of truth — every config decision traces to wire evidence
- Config constraints (transport, auth, format) form the decision framework
- Deterministic logic lives in MCP tools, not in prompts — repeatable and testable
- Ambiguity is a hard stop, not a guess — flag for human review
- Metadata not in the HAR is filled via web search, not left as TODOs
- Generated config must pass Pydantic build-time validation, plus an
  alias-and-brand check: firmware-internal codes (underscore-shaped,
  e.g. `G54_COMMSCOPE`) are rejected from `model_aliases` and `brands`
- The golden file is the human review checkpoint — not the config itself
- All modems — new and existing — go through the same workflow from HAR. Every modem package is built from scratch

## Contents

| Section | What it covers |
|---------|----------------|
| [Inputs](#inputs) | What the onboarding process requires |
| [Outputs](#outputs) | Artifacts generated (modem.yaml, parser.yaml, etc.) |
| [Workflow](#workflow) | End-to-end onboarding flow (capture → analysis → testing → review) |
| [HAR Validation Gate](#har-validation-gate) | Four-step HAR quality check before proceeding |
| [Decision Tree](#decision-tree) | 7-phase detection: transport, auth, session, format, fields |
| [parser.py Decision](#parserpy-decision) | When code post-processing is needed vs config-only |
| [Error Handling](#error-handling) | Hard stops, warnings, confidence annotations |
| [MCP Tools](#mcp-tools) | Tool contracts: validate_har, analyze_har, enrich_metadata, generate_config, etc. |
| [Architecture](#architecture) | Package ownership, fleet patterns, Core vs Catalog |
| [Testing](#testing) | HARMockServer, golden files, static validation, test discovery |
| [Transport Constraint Reference](#transport-constraint-reference) | Auth/session/format valid per transport |
| [Examples](#examples) | Worked examples: HTTP form, HNAP, HTTP JSON |
| [Assumptions and Limitations](#assumptions-and-limitations) | Scope boundaries and known gaps |

---

## Inputs

| Input | Type | Required | Description |
|-------|------|:--------:|-------------|
| HAR file | `.har` file path | yes | Capture from [har-capture](https://github.com/solentlabs/har-capture) — records the full HTTP conversation (auth flows, API calls, page content) with built-in PII redaction |
| Manufacturer | string | yes | Modem manufacturer as the firmware reports it (e.g., "Arris", "CommScope") — determines the catalog directory. Box branding that differs goes in `brands`, not here. |
| Model | string | yes | Model identifier (e.g., "SB8200", "MB7621") |
| GitHub issue | integer | yes | Issue number for `references` and change tracking |
| Contributor | string | yes | GitHub username for `attribution` |
| Default host | string | no | Modem IP if not `192.168.100.1` |
| Known transport | string | no | Override transport detection if contributor provides it |

**What about hardware metadata?** Fields like `hardware.chipset`,
`hardware.docsis_version`, `brands`, `model_aliases`, and `isps` are
not in the HAR. The `enrich_metadata` tool infers what it can from the
analysis (e.g., OFDM/OFDMA channels → DOCSIS 3.1, request hosts →
default_host) and reports what's still missing. The LLM fills remaining
gaps via web search rather than leaving them as TODOs or asking the
contributor.

---

## Outputs

All files are written to `modems/{manufacturer}/{model}/` in the Catalog
package, following MODEM_DIRECTORY_SPEC.md.

| Output | File | Always generated |
|--------|------|:----------------:|
| Modem config | `modem.yaml` | yes |
| Parser config | `parser.yaml` | yes |
| Parser code | `parser.py` | only if declarative extraction is insufficient |
| Golden file | `test_data/modem.expected.json` | yes |
| HAR copy | `test_data/modem.har` | yes (sanitized copy) |

---

## Workflow

### Full onboarding flow

```text
┌─────────────────────────────────────────────────────────┐
│ HAR Capture (user + har-capture)                        │
│ https://github.com/solentlabs/har-capture               │
├─────────────────────────────────────────────────────────┤
│ 1. User runs har-capture against modem IP               │
│ 2. har-capture drives the browser: login, navigate,     │
│    capture all HTTP traffic, close session              │
│ 3. Built-in PII redaction scrubs credentials, MACs,     │
│    serial numbers from the capture                      │
│ 4. User reviews HAR if needed                           │
└────────────────────────┬────────────────────────────────┘
                         ↓ sanitized .har file
┌─────────────────────────────────────────────────────────┐
│ Onboarding (LLM + MCP tools)                            │
├─────────────────────────────────────────────────────────┤
│ 5.  LLM calls validate_har                              │
│ 6.  LLM calls scan_fleet (Catalog) → FleetPatterns      │
│ 7.  LLM calls analyze_har(fleet=fleet)                  │
│     ├── hard_stops? → present to user, resolve          │
│     └── ambiguous HTML format? → LLM reads HAR          │
│         response bodies, picks format                   │
│ 8.  LLM calls enrich_metadata (with analysis +          │
│     manufacturer + model)                               │
│     ├── reviews inferred fields                         │
│     ├── missing fields? → LLM does web search           │
│     │   (chipset, brands, ISPs, model aliases)          │
│     └── warnings? → LLM resolves conflicts              │
│ 9.  LLM calls generate_config(fleet=fleet) (with        │
│     analysis + enriched metadata) — validates before    │
│     returning                                           │
│     ├── validation errors? → LLM fixes, retries         │
│     └── valid → continue                                │
│ 10. LLM calls generate_golden_file                      │
│     └── sanity checks channel counts with user          │
│ 11. LLM calls write_modem_package (configs + golden     │
│     file + HAR → catalog directory)                     │
│ 12. LLM calls run_tests                                 │
│     ├── failures? → LLM diagnoses, fixes config,        │
│     │   re-runs (loop until green)                      │
│     └── pass → continue                                 │
│ 13. LLM commits and pushes → CI runs tests again        │
└────────────────────────┬────────────────────────────────┘
                         ↓ PR ready
┌─────────────────────────────────────────────────────────┐
│ Review and Deploy                                       │
├─────────────────────────────────────────────────────────┤
│ 14. Admin reviews PR changes                            │
│ 15. PR merged → config available on next deployment     │
└─────────────────────────────────────────────────────────┘
```

### What lives where

| Responsibility | Package | Owner |
|----------------|---------|-------|
| HAR capture + PII redaction | [har-capture](https://github.com/solentlabs/har-capture) | User |
| HAR validation | Core (MCP: `validate_har`) | Tool |
| Fleet pattern scanning | Catalog (`fleet_scanner.scan_fleet`) | Tool |
| Transport / auth / format detection | Core (MCP: `analyze_har`) | Tool |
| Ambiguity resolution | — | LLM → User |
| Metadata inference + gap detection | Core (MCP: `enrich_metadata`) | Tool |
| Metadata gaps (web search) | — | LLM (web search) |
| Config generation + validation | Core (MCP: `generate_config`) | Tool |
| Golden file generation | Core (MCP: `generate_golden_file`) | Tool |
| File placement | Core (MCP: `write_modem_package`) | Tool |
| End-to-end testing | Core (harness) + MCP: `run_tests` | Tool |
| Test failure diagnosis + fixes | — | LLM |
| Commit + push | — | LLM → User authorization |
| CI test execution | Core (harness) via Catalog pytest | CI |
| PR review + merge | — | Admin |

### Why MCP, not a prompt-based workflow

The onboarding process is mostly deterministic: parse HAR structure,
match patterns to config fields, validate schemas, run tests. These
steps should be **code, not prompts** — repeatable, testable, and not
dependent on LLM reasoning for correctness.

A prompt-based approach would embed the entire decision tree in natural
language, re-derived by the LLM every invocation. An MCP server encodes
the deterministic parts in Python, exposing structured tools that any
MCP-compatible AI client can orchestrate. The LLM adds value where
judgment is needed: resolving ambiguous HTML formats, searching the
web for metadata, diagnosing test failures, and interacting with the
user.

| | Prompt-based | MCP |
|---|---|---|
| Decision tree | Re-derived from spec each time | Encoded in Python, tested |
| HAR parsing | LLM reads JSON, hopes to count correctly | Code parses deterministically |
| Pydantic validation | LLM calls validator, interprets output | Tool returns structured errors |
| Test execution | LLM runs pytest, parses terminal output | Tool returns structured results |
| Repeatability | Varies by invocation | Deterministic for same input |
| Testability | Cannot unit test a prompt | Standard Python tests |

The MCP server and test harness both live in Core. Consumers include
any MCP-compatible AI client (Claude Code, Gemini CLI, Cursor, etc.)
and Catalog's CI (via pytest). Both are internal to this project.

---

## HAR Validation Gate

**Before any analysis, validate the HAR.** This is a hard stop — if
validation fails, report the gap and do not proceed.

### Step 1: Structural validation

- HAR file parses as valid JSON
- `log.entries` array is non-empty
- Entries have `request` and `response` objects
- **Sanitization check:** HAR contains the "Sanitized by har-capture"
  marker. If missing, emit a warning and require manual verification
  of PII removal (MACs, IPs, Serial Numbers, Passwords) before
  committing to `test_data/`.

### Step 2: Auth flow validation

Check the **first request** in the HAR:

| First request evidence | Interpretation | Action |
|------------------------|---------------|--------|
| Returns 200 with data/dashboard content | Post-auth only — no login flow captured | **HARD STOP**: Request pre-auth HAR. Instruct: "use incognito/private browsing" or "clear cookies first." |
| Returns 401/403 | Pre-auth captured, auth challenge visible | Continue |
| Returns HTML login page (form, no data) | Pre-auth captured, form-based auth | Continue |
| First request has `Cookie` header with session value | Browser had existing session | **HARD STOP**: Request fresh HAR |
| Returns 301/302 redirect to login page | Pre-auth captured | Continue |

### Step 3: Auth mechanism identification

| Wire evidence | Auth mechanism |
|--------------|----------------|
| `WWW-Authenticate: Basic` response header | `basic` auth |
| `WWW-Authenticate: Digest` response header | Digest auth — **unsupported, flag** |
| POST to login endpoint with credential-shaped form body | `form` (or variant — continue analysis) |
| `HNAP_AUTH` header on any request | `hnap` auth |
| URL contains base64-encoded credentials | `url_token` auth |
| JSON POST with SJCL AES-CCM encrypted payload (`EncryptData`) | `form_sjcl` auth |
| Form-encoded POST with salt/PBKDF2 flow | `form_pbkdf2` auth |
| No auth headers, no login, immediate 200 with data | `none` auth |

**Common mistake — 401 does not mean Basic Auth.** Many modems return
401 for any unauthenticated request regardless of their actual auth
mechanism (form-based, HNAP, etc.). The `WWW-Authenticate` response
header is what identifies the mechanism. Without it, do not assume
`basic` in modem.yaml.

**Cross-validation: session config must match auth type.** HTTP Basic
Auth is stateless — no cookies, no sessions, no logout. If the
modem.yaml has session config (cookies, logout endpoints) but declares
`basic` auth, the auth type is wrong.

**If auth mechanism cannot be determined:** HARD STOP. Report what was
observed and what's missing. Do not guess.

### Exception: `none` auth modems

If the HAR shows no login flow and all requests return 200 with data
content, this is valid for `auth: none` modems. The "post-auth only"
hard stop does not apply when there is no auth to capture.

### Step 4: Response body integrity

Detect responses whose body was replaced or stripped **after** capture —
over-sanitization that destroys the payload the parser needs. The signal
is tool-agnostic: a recorded `content.size` that greatly exceeds the actual
`content.text` length means the body shrank between capture and now. The
common cause is a sanitizer that collapses a whole structured payload (e.g.
base64-encoded JSON) down to a single redaction token; the field names and
shape are not PII and must survive, so this is a defect, not expected
sanitization.

| Wire evidence | Action |
|--------------|--------|
| A data response's `content.size` dwarfs its body length (large recorded size, tiny non-empty body) | **HARD STOP**: the HAR is over-sanitized and carries no usable data. Recapture with a fixed sanitizer. |

Empty bodies (`text: ""`) are the "no body captured" case and are not
flagged here. Static assets and non-2xx responses are excluded. One flagged
data response condemns the HAR — there is no point analyzing a capture whose
payloads are gone.

**How to distinguish `none` auth from post-auth HAR:**

- `none` auth: No `Cookie`, `Authorization`, or session headers on any
  request. Consistent 200 responses. No login endpoints in the URL
  history.
- Post-auth HAR: Requests carry session cookies, auth headers, or
  tokens. The session was established before capture started.

---

## Decision Tree

The decision tree is hierarchical: transport first (HNAP constrains
everything; HTTP opens independent axes), then auth, then session, then
actions, then format, then field mappings, then metadata enrichment via
web search.

### Phase 1: Transport Detection

Examine all HAR entries. Transport is determined by the **data transport
mechanism**, not the login UI.

```text
For each entry in HAR:
  Check request headers and URL:
    ├── Request URL is /HNAP1/ ?
    │   └── YES → transport: hnap
    │
    ├── Request has SOAPAction header?
    │   └── YES → transport: hnap
    │
    ├── Request has HNAP_AUTH header?
    │   └── YES → transport: hnap
    │
    └── None of the above → transport: http
```

**HNAP detection is unambiguous.** The `/HNAP1/` endpoint, `SOAPAction`
header, or `HNAP_AUTH` header are protocol markers with no false
positives.

**Everything non-HNAP is `http`.** This includes modems with HTML pages,
JSON APIs, or any combination. The data format (HTML tables, JSON
responses) is a separate axis — detected in Phase 5.

### Phase 2: Auth Strategy Detection

Auth detection depends on transport — the constraint table limits valid
strategies.

**The login POST is selected by credential-shaped field names, not
recency alone.** A form POST to an auth-pattern URL counts as a login
only when its body carries a password-shaped field name — names, not
values, because the sanitizer redacts values. Among credential POSTs
the latest wins, so a retried login yields the successful attempt. An
action POST sharing the login endpoint (Sercomm DM1000: `/setup.cgi`
serves both login and reboot) is never picked, and a HAR with no
credential POST reports no auth flow.

Password-shaped means either of:

- a curated substring from `password_field_indicators` in
  `auth_patterns.json` — these generalize to firmware never seen
  before;
- an exact `password_field` name declared by any committed
  `modem.yaml` — committing a modem teaches the detector its field
  name with no separate pattern-maintenance step. Exact names never
  generalize; only curated substrings do.

One caveat follows from the catalog-derived half: the intake
regression treats each committed HAR as a fresh submission, but a
modem whose field name matches no curated substring is recognized via
its own committed config, so it grades better there than a genuinely
novel submission would.

#### HNAP transport

Auth is always `hnap`. The only variable is `hmac_algorithm`:

| Evidence | Algorithm |
|----------|-----------|
| HNAP_AUTH hash is 32 hex chars (128 bits) | `md5` |
| HNAP_AUTH hash is 64 hex chars (256 bits) | `sha256` |
| Cannot determine from HAR alone | Flag — check model documentation or contributor info |

**Note:** The HNAP_AUTH hash length in the HAR may be ambiguous if the
HAR was captured post-auth (hash computed client-side). If the HAR
includes the Login action response, the `Challenge` and `PublicKey`
fields confirm the protocol but not the algorithm. Default to `md5`
(most common) and flag for verification.

#### HTTP transport

```text
Check HAR entries for login flow:
  ├── No login flow, all 200s, no session artifacts
  │   └── strategy: none
  │
  ├── POST to /goform/*, /cgi-bin/*, or similar with credential-shaped form fields
  │   ├── Response is text with "Url:" / "Error:" prefixes?
  │   │   └── strategy: form_nonce
  │   │       Extract: action, username_field, password_field,
  │   │                nonce_field, nonce_length,
  │   │                success_prefix, error_prefix
  │   │
  │   ├── Response is redirect (302) or contains success indicator?
  │   │   └── strategy: form
  │   │       Extract: action, username_field, password_field,
  │   │                encoding (check for base64), hidden_fields,
  │   │                success.redirect or success.indicator
  │   │
  │   └── Ambiguous response format → flag for human review
  │
  ├── POST with JSON body containing credentials
  │   ├── Login page has SJCL JS variables (myIv, mySalt, encryptflag)?
  │   │   POST body contains EncryptData field?
  │   │   └── strategy: form_sjcl
  │   │       Extract: login_page, login_endpoint,
  │   │                session_validation_endpoint,
  │   │                csrf_header, encrypt_aad, decrypt_aad
  │   │       (pbkdf2_iterations, pbkdf2_key_length, ccm_tag_length
  │   │        extracted from JS or set to SJCL defaults)
  │   │
  │   ├── Multi-request salt/challenge flow?
  │   │   └── strategy: form_pbkdf2
  │   │       Extract: login_endpoint, salt_trigger,
  │   │                pbkdf2_iterations, pbkdf2_key_length,
  │   │                double_hash, csrf_init_endpoint, csrf_header,
  │   │                login_success (see below)
  │   │
  │   └── Single POST login?
  │       └── strategy: form
  │           Extract: action, username_field, password_field
  │
  ├── URL contains base64-encoded credentials (login_<base64>)
  │   └── strategy: url_token
  │       Extract: login_page, login_prefix, success_indicator,
  │                ajax_login (X-Requested-With header present?)
  │
  ├── 401 response with WWW-Authenticate: Basic
  │   └── strategy: basic
  │       Extract: challenge_cookie (retry with Set-Cookie?)
  │
  └── Cannot determine → HARD STOP
```

#### `form_pbkdf2` — detecting `login_success`

Most firmware signals a failed login via a truthy `error` field in the
response JSON. Some firmware (e.g., the Technicolor REST platform) instead
uses `"error": "ok"` on success — treating `error` as a general status
field rather than a strict failure indicator. The default check
("no truthy `error` = success") breaks for these modems.

**Detection rule:** Inspect the response body of the *login* POST (the
second POST to `login_endpoint`, where the password field carries the
derived hash — not the salt-trigger POST). Parse the response JSON.

- If a field is present with a value that is non-null, non-false, and reads
  as a sentinel (short string like `"ok"`, integer `0` meaning "no error",
  boolean `true`) rather than an error description, emit `login_success`
  with that key-value pair.
- If the response has no `error` field, or `error` is null/false/absent,
  omit `login_success` — the default behaviour handles it.

**Examples:**

```yaml
# Technicolor CGA6444VF — {"error": "ok", "message": "MSG_LOGIN_1", ...}
login_success:
  error: "ok"

# Hypothetical — {"result": 0, ...} where 0 means no error
login_success:
  result: 0
```

Values may be string, integer, or boolean — matched by equality against
the parsed JSON response. If the login response body is absent from the
HAR or encrypted, omit `login_success` and flag for contributor
verification.

### Phase 3: Session Detection

Examine post-login requests for session artifacts:

| Evidence | Config |
|----------|--------|
| `Set-Cookie` header after login with named cookie | `auth.cookie_name: "<name>"` (auth owns the cookie it produces) |
| Same cookie sent on all subsequent requests | Confirms cookie-based session |
| Cookie in post-login requests but never in a `Set-Cookie` response | `auth.inject_credential_cookie: true` + `auth.cookie_name: "<name>"` (url_token only) — firmware sets the credential cookie client-side via JavaScript; Core replicates with `btoa(user:pass)` |
| No cookies on any request | Stateless — no cookie_name needed |
| Logout endpoint visible in HAR | `actions.logout` (see Phase 4) |
| `X-Requested-With: XMLHttpRequest` on data requests | `session.headers: { X-Requested-With: "XMLHttpRequest" }` |
| Token in URL query string on data requests | `auth.token_prefix: "<prefix>"` (url_token strategy only) |
| CSRF token header on POST requests | Belongs to auth strategy config, not session |

**Single-session detection:** Cannot be determined from HAR alone. If
the modem rejects concurrent sessions, the HAR won't show it (only one
session was active). If a logout flow is visible in the HAR, emit
`actions.logout`; single-session semantics follow automatically.
Set `requires_session: true` when the logout endpoint requires a valid
session cookie; leave it `false` (default) for unauthenticated logout
endpoints.

**HNAP transport:** Session is implicit (`uid` + `PrivateKey` cookies,
`HNAP_AUTH` header). Do not emit a `session` block.

### Phase 4: Action Detection

Scan HAR for logout and restart flows:

#### Logout

| Evidence | Config |
|----------|--------|
| GET to `/logout*` after data pages | `actions.logout: { type: http, method: GET, endpoint: "<path>" }` |
| POST to `/goform/logout*` or `/api/*/logout` | `actions.logout: { type: http, method: POST, endpoint: "<path>", params: {...} }` |
| POST with pre-fetch page (extract dynamic endpoint) | Add `pre_fetch_url` and `endpoint_pattern` |
| HNAP action with logout/session-end semantics | `actions.logout: { type: hnap, action_name: "<name>" }` |
| No logout visible in HAR | Omit `actions.logout`. Note in the generated YAML that logout behavior could not be confirmed from the HAR. |

#### Restart

| Evidence | Config |
|----------|--------|
| POST to reboot/restart endpoint with params | `actions.restart: { type: http, method: POST, endpoint: "<path>", params: {...} }` |
| HNAP SetConfiguration action with reboot param | `actions.restart: { type: hnap, action_name: "<name>", params: {...} }` |
| No restart visible in HAR | Omit `actions.restart`. This is common — most HAR captures don't include a restart. |

**Restart is rarely in the HAR.** Most contributors capture status pages,
not admin operations. Omitting restart is normal and correct.

#### Source-Inferred Call-Site Extraction

When no request in HAR traffic matches an action pattern, scan captured
response bodies instead. Three passes, in order:

1. **`$.ajax({...})` call sites** (`analysis/actions/callsite.py`) — parse
   the options object for `type`, `url`, and `data`. A `url` matching an
   action pattern yields the endpoint, the method (from `type:`), and
   data params. This is the only call shape observed at action endpoints
   in fleet HARs (XB10 restart, S33-family logout). `$.post(url, {...})`
   appears in fleet page source but never at an action endpoint — it is
   deliberately unsupported until a fleet HAR shows one.
2. **`<form action>` URLs** — a captured page's form action matching an
   action pattern yields the endpoint and method (attribute values may
   be quoted or bare). A query string on the form action is a
   per-session dynamic token (Netgear `?id=`), so the config gets the
   Core pre-fetch shape: bare endpoint as fallback, `endpoint_pattern`
   keyword, `pre_fetch_url` pointing at the page that embeds the live
   URL. Form input values are rewritten by page script at submit time
   (`advButtonClick` sets `buttonSelect` after page load), so params
   are never extracted from form HTML — a warning names the form's
   fields for the manual step.
3. **Bare quoted strings** — the legacy scan; yields endpoint only, with
   the method guessed from the action name (GET for logout, POST for
   restart). Only strings that read as request paths qualify: a bare
   word (`"Reboot"`) is a UI label, and `..` segments or static assets
   (`"../i/Logout_Normal.png"`) are page furniture — matching them
   would fabricate endpoints the firmware never exposes. The same
   action-name prior applies in pass 1 when a call site has no
   `type:` field.

**Traffic-observed actions get pre-fetch from form evidence too.** When
an observed action's endpoint equals a captured page's form action, that
page is emitted as `pre_fetch_url` (with `endpoint_pattern` only when
the form action carries a query token). The older keyword-overlap
heuristic remains warning-only — it suggests, never emits.

**Param resolution rules (pass 1), in precedence order:**

| Param | Resolution |
|-------|-----------|
| Name matches a cookie name observed in this HAR's `Set-Cookie` headers | `{cookie:<name>}` directive (double-submit CSRF shape). Wins even over a literal value — the literal is the captured session's token, not a reusable one. |
| Value is a plain string literal | Emitted verbatim. |
| Value is a computed expression or identifier | Dropped, with a warning naming the param and quoting the expression so the manual step has the evidence in front of it. |

A `data:` payload that is a bare identifier (not an object literal)
yields no params and a warning naming the identifier.

**Every value comes from the contributor's own HAR.** Confirmed modems
contribute the *patterns* (URL regexes in `action_patterns.json`, the
call shapes this extractor parses); they never contribute values.
Cookie names are read from this capture's `Set-Cookie` headers — name
matching, not value matching, because the HAR sanitizer rewrites cookie
values but not inline JS literals.

#### Credential Param Classification

After extracting action params, classify each param as a credential or
an action trigger.  This applies to both HTTP and HNAP actions.

**Detection heuristics (either triggers classification):**

| Signal | Example |
|--------|---------|
| Field name contains a credential keyword (`password`, `passwd`, `pwd`, `secret`, `token`) | `OldPassword`, `csrf_token` |
| Value matches HAR sanitizer pattern (`FIELD_[hex]`, `PASS_[hex]`) | `FIELD_7e8cc9ae`, `PASS_16811255` |

**Behaviour:**

- Params whose value is a `{cookie:<name>}` directive are exempt from
  classification — the directive instructs Core to echo a session
  cookie at execution time; it is config, not a captured credential
  value. Without this exemption the name heuristic would blank
  `csrfp_token`-style params the call-site extractor just resolved.
- Detected credential values are replaced with empty strings in the
  generated config.  The sanitized values are artifacts — they are
  neither the real credential nor a useful placeholder.
- The field names are recorded in `credential_params` on the
  `ActionDetail` so the MCP tool output annotates which params were
  credentials vs action triggers.
- All form fields are preserved in `params` — the browser submits the
  whole form, and the server may require them to be present even if
  their values are not validated for the action.

### Phase 5: Format Detection

Format is constrained by transport:

| Transport | Format detection |
|-----------|-----------------|
| `hnap` | Always `hnap`. See HNAP format detection below. |
| `http` | Inspect data page responses — see below. JSON responses use `json` (or `json_transposed` for `name`+`indexN` pivot shapes); HTML responses use `table`, `table_transposed`, `javascript`, `javascript_json`, or `html_fields`. |

#### HNAP format detection

HNAP format is always `hnap`, but the analysis tool must extract
structural details from HAR response bodies:

1. **Collect HNAP responses** — merge all `GetMultipleHNAPs` response
   bodies from HAR entries (excluding Login SOAPAction entries)
2. **Identify channel data** — for each action response, look for
   string values containing record delimiters (`|+|`, `|-|`, `||`)
3. **Detect delimiters** — record delimiter from the string, field
   delimiter (typically `^`) from the first record
4. **Infer field mappings** — two-pass positional classification:
   - Pass 1: identify definitive fields (lock_status by text pattern,
     channel_type by known values, frequency/symbol_rate by magnitude)
   - Pass 1.5: resolve large-integer ambiguity (larger max values →
     frequency, smaller → symbol_rate)
   - Pass 2: assign remaining numeric fields by DOCSIS convention
     (channel_id first, then power, snr, corrected, uncorrected)
5. **Detect channel type** — if a field has multiple distinct values
   matching known types (QAM256, OFDM PLC, SC-QAM, OFDMA), generate
   a `channel_type.map` config
6. **Identify system_info sources** — action responses with flat
   key-value pairs (no delimiters) that contain firmware, model, or
   uptime fields

**Direction inference:** `response_key` names containing "Downstream"
or "DSChannel" → downstream. "Upstream" or "USChannel" → upstream.

See [HNAPParser](../../cable_modem_monitor_core/docs/FORMAT_HNAP_SPEC.md#hnapparser) for the validated
record layout and parser.yaml example.

#### HTTP format detection

For each data page in the HAR, examine the response body:

**Pre-processing (before format dispatch):**

1. **HAR base64 decoding.** If `content.encoding == "base64"` on the
   HAR response, decode the body first. Some modems (e.g., dm1000)
   produce base64-encoded responses that browsers record verbatim.
   This is a HAR artifact, distinct from parser.yaml `encoding: base64`
   (which is a runtime loader concern).

2. **Content-Type sniffing.** Some modems serve JSON with
   `Content-Type: text/html` (e.g., coda56) or misspelled headers
   (e.g., `applation/json`). Before the Content-Type dispatch:
   - If the header contains `json` (any spelling), treat as JSON.
   - Otherwise, if the body starts with `{` or `[`, attempt JSON
     parse. If valid, treat as JSON. If invalid, fall through to the
     Content-Type-based branch (HTML parsing still runs).
   - Top-level JSON arrays are wrapped as `{"_raw": [...]}` to match
     the runtime loader convention.

```text
Response body analysis (sniff-then-Content-Type):
  ├── Body is valid JSON (regardless of Content-Type)?
  │   └── format: json
  │       Top-level arrays wrapped as {"_raw": [...]}
  │       Detect: array_path and field key names from JSON structure
  │
  ├── application/xml or text/xml ?
  │   └── format: xml — not yet supported, flag for human review
  │
  ├── text/html (and not valid JSON)?
  │   ├── Contains <table> elements with channel data?
  │   │   ├── Rows are channels (each row = one channel)?
  │   │   │   └── format: table
  │   │   │
  │   │   ├── Rows are metrics (each row = one field, columns = channels)?
  │   │   │   └── format: table_transposed
  │   │   │       Indicator: first column has labels like "Channel ID",
  │   │   │       "Frequency", "Power Level"; subsequent columns have values
  │   │   │
  │   │   └── Cannot determine orientation → examine header row and data rows
  │   │
  │   ├── Contains <script> with JS variable assignments holding JSON arrays?
  │   │   └── format: javascript_json
  │   │       Indicators: variableName = [{...}, ...]; in <script> block,
  │   │       array of dicts with 2+ keys (channel objects)
  │   │
  │   ├── Contains <script> with function bodies containing delimited data?
  │   │   └── format: javascript
  │   │       Indicators: function name like "Init*TagValue", pipe-delimited
  │   │       strings, tagValueList variable
  │   │
  │   ├── Contains labeled fields (label: value pairs) without tables?
  │   │   └── format: html_fields (system_info only)
  │   │
  │   └── Mixed content → per-section format (common: table for channels,
  │       html_fields or javascript for system_info)
  │
  └── Ambiguous → flag for human review
```

**JSON direction inference.** After format classification, JSON pages
need direction assignment (downstream vs upstream). The pipeline scans:

1. Resource path (e.g., `/dsinfo.asp` → downstream)
2. Top-level JSON keys (e.g., `{"downstream": [...]}`)
3. Nested keys up to 3 levels deep (e.g., `{"docsis": {"dschannel": [...]}}`)

Short-form prefixes `ds`/`us` are matched on nested keys to handle
modems like the G54 that use `dschannel`/`uschannel`.

**Per-section format is the norm.** A single modem often uses `table`
for downstream/upstream and `html_fields` for system_info, because
channel data naturally appears in tables while system info appears as
label-value pairs.

#### JavaScript function classification

A single HTML page may contain multiple JS functions with delimited
data. The analyzer classifies each function by direction:

- **Directional** — function name contains `ds`, `us`, `downstream`,
  or `upstream` (case-insensitive). These are channel data functions
  and are routed to downstream/upstream section assembly.
- **Non-directional** — everything else. These are candidates for
  system_info detection. The analyzer checks their delimited values
  against the system_info label map. Functions with matching values
  produce system_info sources; functions with no matches are flagged
  as warnings for the LLM layer to inspect.

This direction-based classification avoids false negatives from name
patterns. Generic function names (e.g., no directional keyword) that
contain system_info fields would be missed by a name-matching approach.

### Phase 6: Field Mapping Extraction

This is the most labor-intensive phase. `analyze_har` must map source field
names/positions to canonical output fields.

#### Canonical output fields

**Channel sections (downstream, upstream):**

| Canonical field | Type | Required | Description |
|----------------|------|:--------:|-------------|
| `channel_id` | integer | yes | DOCSIS channel identifier |
| `frequency` | frequency | yes | Channel frequency (normalized to Hz) |
| `power` | float | yes | Signal power level |
| `snr` | float | downstream only | Signal-to-noise ratio / MER |
| `corrected` | integer | no | Correctable codeword errors |
| `uncorrected` | integer | no | Uncorrectable codeword errors |
| `modulation` | string | no | Modulation type |
| `lock_status` | string | no | Channel lock status |
| `symbol_rate` | integer | upstream only | Symbol rate |
| `channel_type` | string | derived | `qam`/`ofdm` (downstream), `atdma`/`ofdma` (upstream) |

**System info (Tier 1 canonical):**

| Canonical field | Type | Common |
|----------------|------|:------:|
| `system_uptime` | string | yes |
| `software_version` | string | yes |
| `hardware_version` | string | yes |
| `docsis_status` | string | sometimes |

**System info (Tier 2 registered — see FIELD_REGISTRY):**

| Registered field | Type | Common |
|-----------------|------|:------:|
| `boot_status` | string | sometimes |
| `docsis_version` | string | sometimes |

System info fields are open-ended — extract whatever the modem provides,
**except identity PII**: `serial_number` and `mac_address` are deliberately
not mapped (no CMM consumer; PII risk). The intake mapping skips them so
they never enter the pipeline. See
[Three-tier field mapping](#three-tier-field-mapping) below and
SYSTEM_INFO_SPEC § Tiered Sensor Model.

#### Three-tier field mapping

Field mapping follows the three-tier system defined in FIELD_REGISTRY.
The analysis tool must map ALL detected fields, not just canonical ones:

1. **Tier 1 (canonical):** Recognized source headers/labels map to
   canonical field names. Core validates these.
2. **Tier 2 (registered):** Recognized source headers/labels map to
   registered field names (see FIELD_REGISTRY). Standardized across
   3+ modems.
3. **Tier 3 (unregistered):** Unrecognized headers/labels are converted
   to `snake_case(source_text)` with default type `string`. These are
   modem-specific pass-throughs that may graduate to Tier 2 when 3+
   modems expose the same field.

**Do not skip unrecognized fields.** The graduation path (Tier 3 to
Tier 2) only works if fields are captured in the first place.

#### Format-specific mapping

**`table` format:** Map column indices to canonical fields by examining
the table header row. Column headers like "Frequency", "Power Level",
"SNR/MER" map to canonical names.

| Source header (common variants) | Canonical field | Tier |
|-------------------------------|----------------|:----:|
| "Channel ID", "Channel", "Ch" | `channel_id` | 1 |
| "Frequency", "Freq" | `frequency` | 1 |
| "Power", "Power Level", "Pwr" | `power` | 1 |
| "SNR", "SNR/MER", "MER", "Signal to Noise" | `snr` | 1 |
| "Corrected", "Correctable", "Total Correctable Codewords" | `corrected` | 1 |
| "Uncorrected", "Uncorrectable", "Total Uncorrectable Codewords" | `uncorrected` | 1 |
| "Modulation", "Mod" | `modulation` | 1 |
| "Lock Status", "Status" | `lock_status` | 1 |
| "Symbol Rate", "Symb. Rate" | `symbol_rate` | 1 |
| Anything else | `snake_case(header_text)` | 3 |

**`table_transposed` format:** Map row labels to canonical fields. Same
label-to-field mapping as above, but rows are labels instead of columns.

**`javascript` format:** Examine JS function bodies to determine:

- Function name (regex target)
- Delimiter character
- Fields per channel (count values between delimiters)
- Field offsets within each channel record (requires examining actual
  data to identify which offset contains frequency, power, etc.)

**`hnap` format:** Examine HNAP response JSON to determine:

- `response_key` (action response wrapper key)
- `data_key` (field containing delimited channel data)
- `record_delimiter` and `field_delimiter` (split the delimited string)
- Field indices within each record (same approach as JS — examine
  actual data to identify positions)

**`javascript_json` format:** JS variable assignments containing JSON
arrays of channel objects. Direction is inferred from the variable
name (e.g., `json_dsData` → downstream). Mapping extraction reuses
the JSON key→field pipeline. The `variable` name is captured in the
section output for config generation.

**`json` format:** Examine JSON response structure to determine:

- `array_path` (dot-notation path to channel array)
- JSON key names → canonical field names
- `fallback_key` if the modem uses non-standard key names

#### Table-to-section association

For HTML table formats (`table`, `table_transposed`), the analysis must
determine whether each detected table belongs to `downstream` or
`upstream`. Use a three-strategy cascade — try each in order until a
match is found:

| Strategy | Where to look | Example |
|----------|--------------|---------|
| 1. Inside table | `<th colspan>` title row containing "Downstream" or "Upstream" | `<th colspan="8">Downstream Bonded Channels</th>` |
| 2. Before table | Preceding heading or text element (`<h1>`-`<h6>`, `<b>`, `<td>`) containing "Downstream" or "Upstream" | `<h4>Downstream</h4>` before `<table>` |
| 3. First cell | First `<th>` or `<td>` in the first row containing "Downstream" or "Upstream" | Transposed tables: `<th>Downstream</th>` |

Secondary signal: table `id` attributes (`dsTable`, `usTable`,
`dsOfdmTable`, `usOfdmaTable`).

If no direction can be determined, flag for human review. Every modem
in the HAR corpus uses "Downstream"/"Upstream" as full keywords.

#### Row start detection

The analysis must determine where data rows begin in each table. This
becomes `row_start` in parser.yaml (via `generate_config`).

Scan rows from the top. Count rows until the first row containing
actual valid data — non-empty cells with numeric values, not all zeros,
not all dashes. The row index is the `row_start` value reported in the
analysis output.

Title rows (`<th colspan>`) and header rows (cells matching known field
labels) are counted as non-data rows.

#### Table selector detection

The analysis must choose a selector strategy for each detected table.
Parser.yaml supports 4 selector types. Auto-select using this priority:

| Priority | Type | When to use |
|:--------:|------|-------------|
| 1 | `id` | Table has an `id` attribute |
| 2 | `header_text` | A nearby heading or title row uniquely identifies the table |
| 3 | `css` | A CSS class uniquely identifies the table |
| 4 | `nth` | Fallback — 0-based table index on the page (fragile) |

Higher priority selectors are more robust across firmware updates.
The selector is configurable in parser.yaml — maintainers can override
the auto-detected choice.

#### Channel type detection

Examine the data for channel type values and build an explicit map.
Map only values observed in the HAR. Do not speculatively add values
for the DOCSIS version or modem family — unrecognized values produce
a warning at runtime, which surfaces new values for a future config
update.

| Evidence | Config |
|----------|--------|
| Modulation column contains "QAM256", "Other" etc. | `channel_type: { field: modulation, map: { "QAM256": "qam", "Other": "ofdm" } }` |
| Separate tables for QAM and OFDM | `channel_type: { fixed: "qam" }` and `channel_type: { fixed: "ofdm" }` |
| HNAP channel type field | `channel_type: { index: N, map: { "SC-QAM": "qam", "OFDM": "ofdm" } }` |
| JSON `channelType` field | `channel_type: { key: "channelType", map: { "sc-qam": "qam", "ofdm": "ofdm" } }` |
| DOCSIS 3.0 modem (no OFDM) | `channel_type: { fixed: "qam" }` for downstream, `{ fixed: "atdma" }` for upstream |

#### Unit detection

Examine data values for unit suffixes:

| Value pattern | Config |
|--------------|--------|
| `"507000000 Hz"`, `"507 MHz"` | `type: frequency` (auto-detects Hz vs MHz) |
| `"3.2 dBmV"` | `type: float, unit: "dBmV"` |
| `"-15.3 dB"` | `type: float, unit: "dB"` |
| Plain numbers without units | No `unit` field needed |

#### Filter detection

Examine the data for placeholder/invalid rows:

| Evidence | Config |
|----------|--------|
| Rows with "Not Locked" or "Unlocked" status | `filter: { lock_status: "Locked" }` |
| HNAP channels with channel_id 0 | `filter: { channel_id: { not: 0 } }` |
| Rows with all-zero values | `filter: { frequency: { not: 0 } }` |
| DOCSIS 3.1 modem, legacy downstream channel array contains rows with `modulation: "UNSUPPORTED"` (or `"unsupported"`) | `filter: { modulation: { not: "UNSUPPORTED" } }` — see note below |

**DOCSIS 3.1 OFDM shadows in the legacy SC-QAM array.** DOCSIS 3.1
OSSI requires every downstream channel — SC-QAM and OFDM — to be
enumerable in the legacy `docsIfDownChannelTable`. OFDM channels
appear there with modulation reported as `"unsupported"` (the legacy
modulation enum has no OFDM value); the rich OFDM data is exposed
in a separate `docsIf31CmDsOfdmChanTable`. Many vendor web APIs
mirror this shape — a "downstream channels" endpoint returns both
real SC-QAM rows and OFDM-shadow rows with `modulation: UNSUPPORTED`,
while a separate "OFDM channels" endpoint carries the canonical OFDM
data. When parser.yaml extracts SC-QAM from the legacy array, add
`filter: { modulation: { not: "UNSUPPORTED" } }` so the shadows
don't double-count alongside the OFDM-array extraction. The shadow
rows' `frequency` is typically a CMTS-assigned channel placement
value, not the active subcarrier band edge — different concept from
the canonical `frequency` field (see `core/docs/FIELD_REGISTRY.md`
§ `frequency` semantics).

### Post-Analysis: JS Endpoint Discovery

After Phase 6, the pipeline scans all JavaScript content in the HAR
for server endpoint references (AJAX calls, fetch targets) and diffs
them against the captured request URLs. Endpoints referenced in JS
but absent from HAR requests are surfaced as advisory warnings.

**Why:** Browser captures only record requests that fire during the
session. Modem firmware JS may reference endpoints that only fire
under specific conditions (stale session state, keepalive timers,
conditional UI paths). These endpoints can be critical to the auth
or session flow but invisible in the HAR.

**What it scans:**

- Response bodies of `.js` file entries
- Inline `<script>` blocks in HTML pages

**What it matches:**

- `$.ajax()`, `$.post()`, `$.get()` (jQuery)
- `fetch()` (Fetch API)
- `.open()` (XMLHttpRequest)
- `createServerRecord()` (Arris firmware)

Only static URL string literals are matched. Variable references
(`$.ajax({url: varName})`) are ignored to avoid false positives.

**Output:** Advisory warnings (not hard stops). The maintainer
investigates whether the uncaptured endpoint is relevant to the
modem's auth, session, or data flow.

### Post-Analysis: Request Requirements

After JS endpoint discovery, the pipeline scans data-fetch entries
for query parameters that appear on every request, indicating a
session-level requirement the modem firmware imposes on all AJAX
calls.

**Why:** Request-side patterns are invisible in response analysis.
The TG3442DE requires `_n=<random>` on every AJAX request — the
server returns 400 without it. The parameter was on every data-fetch
entry in the HAR from day one but went undetected for five alpha
iterations (issue #86) because the pipeline only analyzed responses.

**Detection rule:**

1. Identify data-fetch entries using `identify_data_pages()` (same
   filter as Phase 5: status 200, non-static, has content, data
   Content-Type).
2. Require at least 2 data-fetch entries — with a single entry,
   session-level cannot be distinguished from endpoint-specific.
3. Extract query parameters from each entry's request URL.
4. Exclude entries with no query string — navigation pages and
   initial page loads carry no information about session-level
   parameters.
5. A parameter present on **every** data-fetch entry that carries
   a query string is a session requirement. Parameters appearing
   on fewer entries are endpoint-specific and ignored.
6. Filter known non-session parameters: jQuery's `cache: false`
   adds `_=<timestamp>` to every AJAX request. The bare `_` key
   is always excluded.
7. Filter auth-managed token params: if Phase 3 detected a
   `token_prefix` (url_token auth), query params whose name
   starts with that prefix are auth-owned, not session-owned.

**Output:** Detected parameters are added to `session.query_params`
in the analysis result. The config generator emits them in the
session block of modem.yaml. A warning is appended noting the
detected parameters for maintainer review.

**HNAP:** Skipped. HNAP uses SOAP-over-HTTP; query parameters are
not part of the session contract.

### Phase 7: Metadata Enrichment

Hardware metadata, branding, and ISP information are not present in the
HAR. The `enrich_metadata` MCP tool handles the inferrable subset:
`default_host` from HAR request URLs, `hardware.docsis_version` from
OFDM/OFDMA channel presence. It reports what was inferred, what's still
missing, and any conflicts with existing config. The LLM fills remaining
gaps by searching the web using the manufacturer and model as search terms.

#### Fields to research

| Field | Search strategy | Fallback |
|-------|----------------|----------|
| `hardware.docsis_version` | Search "{manufacturer} {model} specifications" or FCC filing. Also infer from HAR: if OFDM/OFDMA channels are present in data pages, the modem is at least DOCSIS 3.1. DOCSIS 4.0 cannot be inferred from wire data — it reuses 3.1's OFDM/OFDMA channel types ([Averna, DOCSIS 4.0 Overview](https://insight.averna.com/en/resources/blog/the-flavors-of-docsis-4-0)) — so set `"4.0"` only from hardware sources (chipset, manufacturer specs). | Infer from channel data if possible; flag if ambiguous |
| `hardware.chipset` | Search "{manufacturer} {model} chipset" or "{model} teardown". FCC filings, iFixit teardowns, and DSLReports forums are common sources. | Omit — chipset is optional |
| `hardware.release_date` | Search "{manufacturer} {model} release" or "{model} launch". Prefer a manufacturer press release or ISP rollout announcement; an FCC grant date or dated manufacturer manual is an acceptable proxy when labeled as such in `sources.release_date`. Feeds the catalog README timeline — entries without it are omitted from that rendering. | Omit and note the gap — never guess a year |
| `brands` | User-visible brand names from the box or retail listings (e.g., SB8200 → "Surfboard", the CommScope-made G54 → "Arris"). Also check firmware brand fields in the HAR (e.g., `customer`). Brands become manufacturer-dropdown choices and appear in the model line's parenthetical, so entries must be names users actually see, and must be sourced. | Omit if no branding found |
| `model_aliases` | Alternate user-facing model names only — rebadges, regional variants, and sticker codes users encounter. Shown in the model line's parenthetical. Firmware-internal `product`/platform codes do NOT belong here — they stay in the HAR as evidence. If the name is a different product users would purchase and search for, create a separate catalog entry instead. See `MODEM_YAML_SPEC.md` § Aliases vs Separate Entries. | Omit if no aliases found |
| `isps` | Search "{model} ISP" or "{model} compatible". Also check the GitHub issue — contributors often mention their ISP. | `["Various"]` if unknown |
| `default_host` | Most cable modems use `192.168.100.1`. Some (Compal, some gateways) use `10.0.0.1` or `192.168.0.1`. Check modem documentation if contributor didn't provide it. | `"192.168.100.1"` |

#### Evidence sources (ranked by reliability)

1. **HAR content** — model name in HTML title/headers, DOCSIS version
   from channel types (OFDM = 3.1)
2. **GitHub issue** — contributor often mentions ISP, firmware, variant
3. **Manufacturer product page** — authoritative for specs
4. **FCC filing** — chipset, sometimes DOCSIS version
5. **DSLReports / modem forums** — ISP compatibility, variant info
6. **Amazon/retail listings** — brand name, marketing model name

#### Source tracking

Every researched field should have a corresponding entry in
`sources` so future maintainers know where the data came from:

```yaml
hardware:
  docsis_version: "3.1"
  chipset: "Broadcom BCM3390"

sources:
  chipset: "FCC filing"
  docsis_version: "OFDM channels in HAR + manufacturer spec sheet"
```

If a field cannot be determined from any source, omit it rather than
guessing. `chipset` is optional. `docsis_version` can usually be
inferred from channel data (OFDM present → 3.1, no OFDM → 3.0).

---

## parser.py Decision

parser.py is the escape hatch for modem-specific quirks that can't be
expressed in parser.yaml. The tools should **prefer parser.yaml** and
only generate parser.py when necessary.

### When parser.py IS needed

| Situation | Why parser.yaml can't handle it |
|-----------|-------------------------------|
| Complex uptime string parsing (e.g., "3 days 2h 15m" → seconds) | Requires procedural string logic |
| Restart window filtering (zero power/SNR during reboot) | Requires runtime state |
| Frequency range → center conversion | Arithmetic on extracted values |
| Non-standard data layout that doesn't fit any format strategy | Extraction logic too complex for config |
| Conditional field presence (field exists only on some firmware) | Config doesn't support conditional logic |

### When parser.py is NOT needed

| Situation | parser.yaml solution |
|-----------|---------------------|
| Multi-table field merging | `merge_by` on companion table |
| Different column layouts per channel type | Multiple `tables[]` entries |
| Unit stripping | `unit` field on column/row mapping |
| OFDM vs QAM detection | `channel_type.map` |
| Label-based system info extraction | `html_fields` format with `label` selector |
| CSS-targeted system info extraction | `html_fields` format with `css` selector |
| Attribute-encoded values (e.g., CSS class status) | `html_fields` `css` selector with `attribute` field |

#### CSS-encoded status values

Some modems render status indicators as CSS classes or data attributes
rather than visible text. For example, a provisioning status table may
use Bootstrap classes (`class="success"`) with glyphicon icons instead
of displaying the word "Online". The automated Phase 6 label detection
(which matches visible text via regex) will miss these fields because
there is no text content to match against.

These values are configured in parser.yaml using `html_fields` with a
`css` selector and the `attribute` field:

```yaml
html_fields:
  - field: provisioning_status
    css: "td.provisioning-result span"
    attribute: "class"
    pattern: "label-(?P<value>\\w+)"
```

The `css` selector targets the element, `attribute` reads the named
attribute (instead of text content), and the optional `pattern`
extracts the meaningful portion via a named capture group.

This pattern appears in modems like the CGA2121, where provisioning
tables use CSS classes to indicate pass/fail status. During onboarding,
these fields start as Tier 3 (passthrough) since they are
modem-specific. They can be elevated to Tier 2 when the same pattern
appears across multiple modems. See
[SYSTEM_INFO_SPEC.md](../../cable_modem_monitor_core/docs/SYSTEM_INFO_SPEC.md) for full `html_fields` semantics.

### parser.py contract

If generated, parser.py must follow the post-processing contract from
[PARSING_SPEC.md](../../cable_modem_monitor_core/docs/PARSING_SPEC.md):

```python
class PostProcessor:
    """Post-processor for {Manufacturer} {Model}."""

    def parse_downstream(
        self,
        channels: list[dict],
        resources: dict[str, Any],
    ) -> list[dict]:
        """Modify downstream channels after BaseParser extraction."""
        # ... modem-specific logic
        return channels

    def parse_upstream(
        self,
        channels: list[dict],
        resources: dict[str, Any],
    ) -> list[dict]:
        """Modify upstream channels after BaseParser extraction."""
        # ... modem-specific logic
        return channels

    def parse_system_info(
        self,
        system_info: dict[str, Any],
        resources: dict[str, Any],
    ) -> dict[str, Any]:
        """Modify system info after BaseParser extraction."""
        # ... modem-specific logic
        return system_info
```

Only implement hooks for sections that need post-processing. The
coordinator skips missing hooks.

---

## Error Handling

### Hard stops (do not proceed)

| Condition | Message |
|-----------|---------|
| HAR is post-auth only (no login flow for authenticated modem) | "HAR appears to be post-auth only — first request returns 200 with data content and session cookies are present. Please recapture using incognito/private browsing." |
| Auth mechanism cannot be determined | "Cannot determine auth mechanism from HAR. Observed: [list evidence]. Missing: [what's needed]. Please provide additional information or a more complete HAR capture." |
| Transport ambiguous | "Cannot determine transport. HNAP markers (HNAP1 URL, SOAPAction, HNAP_AUTH header) were not found, but some data responses are ambiguous. Please confirm the modem's data transport mechanism." |
| HAR has no data pages (only login flow) | "HAR contains login flow but no data page responses. Please recapture including navigation to the modem's status/signal pages after login." |

### Warnings (proceed with flag)

| Condition | Message |
|-----------|---------|
| No logout flow in HAR | "No logout endpoint observed in HAR. If this modem has single-session limits, a logout action will be needed." |
| HMAC algorithm uncertain (HNAP) | "HNAP HMAC algorithm cannot be confirmed from HAR. Defaulting to `md5`. Verify with contributor." |
| Restart not in HAR | "No restart flow observed in HAR. `actions.restart` omitted. Can be added later from modem documentation." |
| parser.py generated | "parser.py was generated for: [reasons]. Review the post-processing logic for correctness." |

### Confidence annotations

The generated modem.yaml should include comments marking fields with
low confidence:

```yaml
auth:
  strategy: form
  action: "/goform/login"          # from HAR: POST observed at this endpoint
  username_field: "loginUsername"   # from HAR: form field name in POST body
  password_field: "loginPassword"  # from HAR: form field name in POST body
  encoding: base64                 # from HAR: password value appears base64-encoded

```

---

## MCP Tools

The onboarding MCP server lives in Core and exposes structured tools
that the LLM orchestrates. Each tool has defined inputs/outputs and is
independently testable.

### `validate_har`

Runs the HAR validation gate (see above). Returns structured result:
pass/fail, detected issues, and diagnostic messages.

**Input:** HAR file path
**Output:** `{ valid: bool, issues: [], auth_flow_detected: bool, transport_hints: [] }`

### `analyze_har`

The core analysis tool. Runs Phases 1–6 of the decision tree:
transport detection, auth strategy detection, session detection, action
detection, format detection, and field mapping extraction.

**Input:** HAR file path + optional ``fleet`` (``FleetPatterns`` from Catalog scanner)
**Output:** Structured analysis result:

```json
{
  "transport": "http",
  "confidence": "high",
  "auth": {
    "strategy": "form",
    "fields": { "action": "/goform/login", "...": "..." },
    "confidence": "high"
  },
  "session": {
    "cookie_name": "session",
    "headers": {},
    "query_params": {}
  },
  "actions": { "logout": { "...": "..." }, "restart": null },
  "sections": {
    "downstream": {
      "format": "table",
      "resource": "/status.html",
      "mappings": [
        { "index": 0, "field": "channel_id", "type": "integer" },
        { "index": 1, "field": "frequency", "type": "frequency", "unit": "Hz" },
        { "index": 2, "field": "power", "type": "float", "unit": "dBmV" }
      ],
      "selector": { "type": "header_text", "match": "Downstream Bonded Channels" },
      "row_start": 2,
      "channel_type": { "fixed": "qam" },
      "filter": { "lock_status": "locked" },
      "channel_count": 24
    },
    "upstream": {
      "format": "table",
      "resource": "/status.html",
      "mappings": [
        { "index": 0, "field": "channel_id", "type": "integer" },
        { "index": 1, "field": "frequency", "type": "frequency" }
      ],
      "selector": { "type": "header_text", "match": "Upstream Bonded Channels" },
      "row_start": 2,
      "channel_type": { "fixed": "atdma" },
      "channel_count": 4
    },
    "system_info": {
      "sources": [
        {
          "format": "html_fields",
          "resource": "/info.html",
          "fields": [
            { "label": "System Up Time", "field": "system_uptime", "type": "string" }
          ]
        }
      ]
    }
  },
  "warnings": [],
  "hard_stops": [],
  "core_gaps": [
    {
      "phase": "auth",
      "category": "unmatched_login",
      "summary": "Form POST to /custom/endpoint has credential fields but URL not in login patterns",
      "evidence": { "endpoint": "/custom/endpoint", "method": "POST" }
    }
  ]
}
```

Returns `hard_stops` if transport or auth is ambiguous — the LLM presents
these to the user for resolution before proceeding.

**Core gaps** indicate patterns the pipeline detected but Core cannot yet
handle. When `core_gaps` is non-empty, config generation should not
proceed — the modem needs a development effort to extend Core's pattern
set. Each gap has:

- `phase` / `category`: identifies the pipeline phase and gap type
- `summary`: human-readable description of the gap
- `evidence`: structured wire data from the HAR for diagnosis

The `/modem-intake` skill reports gaps to the user and stops. The gap
report contains enough detail to file a GitHub issue for a development
effort. Categories:

| Category | Phase | Evidence | What Core needs |
|----------|-------|----------|-----------------|
| `unmatched_login` | auth | POST endpoint + credential fields | New URL pattern in `auth_patterns.json` or new auth strategy |
| `auth_unknown` | auth | Signal flags + description | New auth strategy implementation |
| `unmatched_restart` | actions | POST endpoint + action-like params | New URL pattern in `action_patterns.json` |
| `unmatched_logout` | actions | POST endpoint + action-like params | New URL pattern in `action_patterns.json` |

Well-known modems with standard patterns produce zero core gaps.
Novel modems produce gaps that require development before onboarding.

### `enrich_metadata`

Bridges `analyze_har` and `generate_config`. Contributors should only need
to provide manufacturer + model — the rest is either inferrable from the
HAR analysis or has sensible defaults. Separating enrichment from config
assembly keeps `generate_config` doing one thing and gives contributors
clear guidance on what's missing.

**Use cases:**

1. **Self-service contributor:** Runs har-capture, processes HAR through the
   pipeline, submits a PR. Without enrichment, modem.yaml is thin — missing
   docsis_version, default_host, or hardware metadata. `enrich_metadata`
   tells them exactly what was inferred and what still needs filling in,
   turning cryptic validation failures into actionable guidance.

2. **Maintainer workflow:** Downloads a contributor's HAR, runs the pipeline
   with an LLM. The LLM calls `enrich_metadata` for structured
   `inferred` / `missing` / `warnings` output, fills gaps via web search,
   passes complete metadata to `generate_config`.

3. **Status upgrade:** Existing modem moves from `awaiting_verification` → `confirmed`.
   Tool merges new metadata (ISPs, attribution) with existing config.

**Input:** Analysis result + optional existing config + optional user input
**Output:**

```json
{
  "metadata": { "...": "complete metadata dict for generate_config" },
  "inferred": ["default_host", "hardware.docsis_version"],
  "missing": ["hardware.chipset", "isps"],
  "warnings": ["existing default_host 10.0.0.1 differs from HAR host 192.168.100.1"]
}
```

**Inferences from analysis:**

- `default_host` — most common host in HAR request URLs
- `hardware.docsis_version` — OFDM/OFDMA channels in analysis → 3.1, else 3.0
  (never 4.0 — indistinguishable from 3.1 on the wire; hand-set from hardware sources)
- `transport` — from analysis
- `status` — defaults to `awaiting_verification` for new, unchanged for existing

### `generate_config`

Takes the analysis result (from `analyze_har`) plus enriched metadata
(from `enrich_metadata`) and produces modem.yaml and parser.yaml content.
Runs Pydantic validation and cross-file consistency checks before returning.

**Input:** Analysis result + metadata (manufacturer, model, hardware, brands, etc.) + optional ``fleet`` (``FleetPatterns`` from Catalog scanner)
**Output:** `{ modem_yaml: str, parser_yaml: str, parser_py: str | null, validation: { valid: bool, errors: [] } }`

When ``fleet`` is provided, ``build_parser_dict`` uses fleet aggregate
patterns to augment auto-generated aggregate fields.

Does **not** write files — returns content for the LLM to review and
place. If validation fails, returns errors so the LLM can fix and retry.

### `generate_golden_file`

Reads the HAR response bodies directly and applies the parser.yaml
config to extract `ModemData`. This is the same extraction logic the
pipeline uses, but against HAR content rather than a live server.

**Input:** HAR file path + parser.yaml content
**Output:** `{ golden_file: dict, golden_file_json: str, channel_counts: { downstream: int, upstream: int }, system_info_fields: [str], missing_system_info_fields: [str] }`

`golden_file_json` is the canonical serialization of `golden_file` (`sort_keys=True`, `indent=2`, `ensure_ascii=False`). Always write this string directly to `modem.expected.json` — never re-serialize `golden_file` yourself, which loses the ordering guarantee.

The channel counts and field lists are returned separately so the LLM can
sanity-check before writing ("Found 16 downstream, 4 upstream, system
info has uptime + firmware version — does that look right?").
`missing_system_info_fields` is the diff of `SYSTEM_INFO_FIELDS` (the four
Tier-1 fields: `docsis_status`, `hardware_version`, `software_version`,
`system_uptime`) against what the parser actually extracted. A non-empty list
means the parser is missing a registry field — inspect the HAR for that data
before proceeding. This is an advisory warning, not a hard stop; some modems
genuinely don't expose all four fields.

### `run_tests`

Invokes Core's test harness for a specific modem directory. This is
the same harness that Catalog's pytest suite uses — the MCP tool just
provides a structured interface to it.

**Input:** Modem directory path (e.g., `modems/motorola/mb7621`)
**Output:** `{ passed: bool, failures: [{ test: str, expected: any, actual: any, diff: str }] }`

### `write_modem_package`

Writes pipeline output to the catalog modem directory. The pipeline
produces configs and golden files in memory, but `run_tests` needs files
on disk. This tool writes directly to the catalog folder — that's the
destination anyway.

**Input:** Output directory, modem.yaml string, parser.yaml string,
golden file dict, HAR file path, optional parser.py string
**Output:**

```json
{
  "modem_dir": "modems/motorola/mb7621",
  "files_written": ["modem.yaml", "parser.yaml", "test_data/modem.har", "test_data/modem.expected.json"],
  "files_skipped": [],
  "errors": []
}
```

**Validation gate — login_page fixture consistency:** Before writing any
files, the tool checks that form-auth modems with `login_page` configured
have a usable login page response in the HAR fixture. If the GET entry for
`login_page` is missing, has an empty body, or its HTML does not reference
`auth.action`, the tool returns an error in `errors` and writes nothing.
This prevents a class of silent failures where the mock server test passes
(it does not validate CSRF tokens) while real hardware authentication fails
because `_discover_hidden_fields()` returned empty. The check only applies
to `strategy: form` with a non-empty `login_page` — all other strategies
are unaffected.

**Creates standard catalog structure:**

```text
{output_dir}/
├── modem.yaml
├── parser.yaml
├── parser.py              (if provided)
└── test_data/
    ├── modem.har          (copied from har_path)
    └── modem.expected.json
```

Existing files are not overwritten — reported in `files_skipped` so the
LLM can decide whether to force-replace or investigate.

### `validate_config`

Standalone validation — runs Pydantic schema + cross-file consistency
against existing files on disk. Useful for re-validation after manual
edits.

**Input:** Modem directory path
**Output:** `{ valid: bool, errors: [] }`

### Tool boundaries

| Responsibility | MCP tool | LLM | User |
|----------------|----------|--------|------|
| HAR structural validation | `validate_har` | | |
| Transport detection | `analyze_har` | | |
| Auth detection (unambiguous) | `analyze_har` | | |
| Auth detection (ambiguous) | `analyze_har` returns hard_stop | Presents options | Decides |
| Format detection (HNAP) | `analyze_har` | | |
| Format detection (HTTP — ambiguous) | `analyze_har` returns candidates | Reads response bodies, picks format | |
| Field mapping extraction | `analyze_har` | | |
| Metadata inference + gap detection | `enrich_metadata` | Reviews inferred/missing | |
| Metadata gaps (web search) | | Web search for missing fields | Verifies |
| Config generation | `generate_config` | Provides enriched metadata | |
| Golden file generation | `generate_golden_file` | Sanity checks counts | Reviews |
| File placement | `write_modem_package` | | |
| End-to-end testing | `run_tests` | Diagnoses failures | |
| Test failure fixes | | Edits config | Approves |
| Commit + push | | Uses existing git tools | Authorizes |

---

## Architecture

### Package Ownership

Both the MCP server and the test harness live in **Core**. Catalog
provides modem data; Core provides the infrastructure to analyze,
validate, and test it.

```text
Core (solentlabs-cable-modem-monitor-core)
├── Pipeline: auth → load → parse
├── MCP server: onboarding tools (analyze, generate, validate)
├── Test harness: HARMockServer, golden file comparison
└── Pydantic validation models (dev dependency)

Catalog (solentlabs-cable-modem-monitor-catalog)
├── modems/{mfr}/{model}/modem.yaml
├── modems/{mfr}/{model}/parser.yaml
├── modems/{mfr}/{model}/parser.py          (optional)
└── modems/{mfr}/{model}/test_data/
    ├── modem.har
    └── modem.expected.json
```

**Why Core owns both:** The MCP tools exercise Core's pipeline logic
(auth strategies, resource loaders, parser coordinator). The test
harness validates that the pipeline produces correct output from HAR
input. Both are consumers of Core's internals. Catalog just points
Core's harness at its modem directories — no test logic in Catalog.

**Dependency direction is preserved:** Catalog depends on Core. Core
has no knowledge of specific modems. The test harness accepts a modem
directory path and discovers the files it needs.

### Catalog Extension: Fleet-Augmented Analysis

Core's ``analyze_har`` works from first principles — hardcoded keyword
patterns, field registries, and direction heuristics. The Catalog
extends this with fleet-derived patterns learned from the 35+ existing
``parser.yaml`` files.

**Architecture:** Core defines the contract (``FleetPatterns``
dataclass). Catalog populates it by scanning the fleet. Core's
analyzer accepts it as an optional parameter and merges fleet patterns
into its baseline detection.

```text
Core defines:
  FleetPatterns (analysis/types.py)
    selector_directions: dict[str, str]           # selector text → direction
    system_info_labels: dict[str, tuple[str,int]] # label text → (field, tier)
    system_info_ids: dict[str, tuple[str,int]]    # CSS ID → (field, tier)
    system_info_json_keys: dict[str, tuple[str,int]] # JSON key → (field, tier)
    delimiters: set[str]                          # record delimiters (HNAP/JS)
    channel_type_values: set[str]                 # modulation type strings
    aggregate_fields: list[tuple[str,str]]        # (source_field, agg_name)

  analyze_har(har_path, fleet=None) → AnalysisResult
  generate_config(analysis, metadata, *, fleet=None) → GenerateConfigResult

Catalog provides:
  fleet_scanner.scan_fleet(CATALOG_PATH) → FleetPatterns
  trial_parser.trial_parse(har_path, parser_yaml) → TrialResult
```

**What fleet patterns augment:**

| Phase | Baseline (Core) | Fleet augmentation (Catalog) |
|-------|-----------------|------------------------------|
| Table direction | Keyword matching ("downstream", "upstream") | Selector text from proven configs ("Signal Status (Codewords)" → downstream) |
| System info labels | 17 hardcoded label→field mappings | Labels, CSS IDs, and JSON keys learned from fleet ("firmware name" → firmware_name) |
| Aggregate fields | Hardcoded (source_field, agg_name) pairs | Additional aggregate patterns from fleet parser.yaml files |

**Merge rules:** Fleet patterns augment, not override. Core's baseline
maps apply first. Fleet adds entries only for labels/selectors that
Core's baseline does not cover. This means a new modem gets the
benefit of every previous modem's config without any manual registry
maintenance.

**Trial parser:** After analysis, the trial parser feeds HAR response
bodies through Core's ``ModemParserCoordinator`` with a candidate
``parser.yaml`` to validate that selectors find the right tables and
field mappings extract non-empty values. This catches configuration
errors before committing.

---

## Testing

Core provides a reusable test harness that takes a modem directory
and runs the full pipeline against an `HARMockServer` — a local HTTP
server that replays HAR-captured responses with auth simulation. The
harness is consumed by:

- The MCP `run_tests` tool (during onboarding)
- Catalog's pytest suite (during CI)
- Developers running tests locally
- The standalone serve command for manual HA integration testing:
  `python -m solentlabs.cable_modem_monitor_core.test_harness <modem_dir>`

### HARMockServer

Builds a local HTTP server from HAR entries. Each entry becomes a
route that replays the recorded response.

```text
HAR entry:
  request:  { method: GET, url: "/MotoConnection.asp", headers: [...] }
  response: { status: 200, headers: [...], content: { text: "<html>..." } }

      ↓ becomes

Mock route:
  GET /MotoConnection.asp → 200, <html>...
```

**Auth-aware replay:** The server handles auth flows statefully,
not just serves responses by URL:

1. **Login endpoint** returns success response only when credentials
   match the HAR's recorded login request body
2. **Session cookie** is set on successful login and required on
   subsequent requests
3. **Data pages** return 401/redirect if no valid session, 200 with
   recorded content if session is valid
4. **HNAP** validates full `HNAP_AUTH` HMAC signatures on all requests
   (login phases and data requests). The mock uses deterministic
   challenge values, so the expected private key and login password are
   pre-computed. This catches HMAC computation bugs (wrong key, wrong
   message format, wrong timestamp modulo), not just header presence.
   The mock also merges all `GetMultipleHNAPs` HAR responses into a
   single combined response — the HNAP loader sends one batched request,
   but HAR captures contain multiple separate calls

This is necessary because the pipeline under test performs real auth
— if the server ignores auth, we're not testing the auth config.

**Stateless fallback:** For `auth: none` modems, the server simply
maps URL paths to responses with no session tracking.

**Auth coverage:** All nine auth strategies have dedicated mock handlers
including full PBKDF2 multi-round challenge-response, SJCL AES-CCM
crypto validation, and CBN AES-256-CBC encrypted login.

### Golden File Comparison

After the pipeline produces `ModemData`, the harness compares it
against the golden file (`test_data/modem.expected.json`).

**Comparison rules:**

- Deep equality on the full `ModemData` dict
- Downstream and upstream channel lists are order-sensitive (channel
  order should be deterministic from the same input)
- System info fields are compared as a flat dict
- On failure, output a structured diff showing exactly which fields
  diverged, with both expected and actual values

**Diff format example:**

```text
FAIL: modems/motorola/mb7621
  downstream[3].frequency:
    expected: 507000000
    actual:   507
    (likely missing Hz normalization)

  upstream: expected 4 channels, got 3
    missing: channel_id=4 (frequency=37700000)
    (likely filter excluding valid channel)
```

### Test Discovery

The harness discovers test cases from the Catalog directory structure:

```text
For each modems/{mfr}/{model}/test_data/ directory:
  For each *.har file:
    1. Find matching *.expected.json (same stem)
    2. Find matching modem*.yaml (see config resolution below)
    3. Find parser.yaml (or parser.py) in parent directory
    4. Register as a test case
```

**Config resolution** (from MODEM_DIRECTORY_SPEC.md):

- `modem.har` → uses `modem.yaml`
- `modem-{name}.har` → look for `modem-{name}.yaml`; if not found,
  fall back to `modem.yaml`

### Test Execution Flow

```text
discover test case:
  modem.har + modem.expected.json + modem.yaml + parser.yaml
    ↓
build HARMockServer from modem.har
    ↓
load modem config (modem.yaml → auth strategy, session, actions)
load parser config (parser.yaml → extraction mappings)
    ↓
run pipeline against HARMockServer:
  1. Auth strategy authenticates (login flow)
  2. Resource loader fetches pages declared in parser.yaml
  3. ModemParserCoordinator extracts ModemData
  4. parser.py post-processing (if present)
    ↓
compare output against modem.expected.json
    ↓
pass / fail with structured diff
```

### Catalog's pytest integration

Catalog's test suite is thin — it just points Core's harness at its
modem directories:

```python
# packages/cable_modem_monitor_catalog/tests/conftest.py
from solentlabs.cable_modem_monitor_core.test_harness import discover_modem_tests

# Auto-discover all modem test cases from the catalog
pytest_plugins = []

def pytest_generate_tests(metafunc):
    """Parametrize tests from modem directory discovery."""
    if "modem_test_case" in metafunc.fixturenames:
        cases = discover_modem_tests(CATALOG_MODEMS_PATH)
        metafunc.parametrize("modem_test_case", cases, ids=lambda c: c.name)
```

```python
# packages/cable_modem_monitor_catalog/tests/test_modems.py
from solentlabs.cable_modem_monitor_core.test_harness import run_modem_test

def test_modem_har_replay(modem_test_case):
    """Each modem's HAR replay produces expected output."""
    result = run_modem_test(modem_test_case)
    assert result.passed, result.diff
```

No modem-specific test code in Catalog. Adding a modem means adding
files to its directory — the test is automatic.

---

### Static validation (inside `generate_config`)

The `generate_config` tool validates before returning:

- All required fields present
- Transport constraint table satisfied (valid auth strategies, formats,
  action types for the declared transport)
- Auth-session-action consistency rules (e.g., HNAP + explicit session
  block is an error; `requires_session` only valid on `type: http`)
- Field types and selector types are valid
- Every `resource` in parser.yaml appears as a request URL in the HAR
- Every `response_key` (HNAP) corresponds to an action response key
  in the HAR
- Column indices / field offsets don't exceed actual data dimensions

If validation fails, `generate_config` returns errors — the LLM fixes
and retries. Invalid config is never written to disk.

### End-to-end testing (via `run_tests`)

After all artifacts are placed in the modem directory, the LLM calls
`run_tests` which invokes Core's test harness:

1. `HARMockServer` built from `test_data/modem.har` (auth-aware)
2. Full pipeline runs: auth → load → parse
3. Output compared against `test_data/modem.expected.json`
4. Structured diff returned on failure

| Failure | Root cause | Fix |
|---------|-----------|-----|
| Auth fails (401, no session) | modem.yaml auth config doesn't match HAR login flow | Revisit auth fields — check form fields, endpoint, encoding |
| Resource load fails (404) | parser.yaml `resource` path doesn't match HAR URLs | Check URL paths — trailing slashes, case sensitivity |
| Parser returns empty channels | Column indices or field offsets are wrong | Re-examine HAR response content — count columns/fields |
| Output doesn't match golden file | Field mapping mismatch, filter too aggressive, or golden file wrong | Compare diff — fix config or update golden file |
| HNAP batch request fails | Action names from `response_key` don't match HAR | Verify `response_key` values against HAR response keys |

**Expect iteration.** Test failures are normal during onboarding.
The LLM diagnoses the failure from the diff, fixes the config, and
re-runs `run_tests`. This loop continues until tests pass.

---

## Transport Constraint Reference

Reproduced from [MODEM_YAML_SPEC.md](../../cable_modem_monitor_core/docs/MODEM_YAML_SPEC.md#validation-rules) for quick reference during analysis:

| Transport | Valid auth strategies | Valid formats | Valid action types |
|-----------|---------------------|---------------|-------------------|
| `http` | `none`, `basic`, `form`, `form_nonce`, `url_token`, `form_pbkdf2`, `form_sjcl` | `table`, `table_transposed`, `html_fields`, `javascript`, `javascript_json`, `json`, `json_transposed`, `xml` | `http` |
| `hnap` | `hnap` | `hnap` | `hnap` |

---

## Examples

### Example 1: HTML table modem with form auth

**HAR evidence:**

- First request: GET `/` → 200 with HTML login form
- POST `/goform/login` with `loginUsername=admin&loginPassword=<base64>`
- 302 redirect to `/home.asp`
- Subsequent GETs to `/connection.asp` → 200 with HTML tables
- Set-Cookie: `session=<value>`
- GET `/logout.asp` at end of capture

**Generated modem.yaml:**

```yaml
manufacturer: "{Manufacturer}"
model: "{Model}"
transport: http
default_host: "192.168.100.1"

auth:
  strategy: form
  action: "/goform/login"
  username_field: "loginUsername"
  password_field: "loginPassword"
  encoding: base64
  cookie_name: "session"
  success:
    redirect: "/home.asp"

actions:
  logout:
    type: http
    method: GET
    endpoint: "/logout.asp"
    requires_session: false

hardware:
  docsis_version: "3.0"

status: awaiting_verification
```

**Generated parser.yaml (abbreviated):**

```yaml
downstream:
  format: table
  resource: "/connection.asp"
  tables:
    - selector:
        type: css
        match: "table.data-table"
      row_start: 1
      columns:
        - index: 0
          field: channel_id
          type: integer
        # ... remaining columns from table header analysis

system_info:
  sources:
    - format: html_fields
      resource: "/home.asp"
      fields:
        - label: "System Up Time"
          field: system_uptime
          type: string
```

### Example 2: HNAP modem

Validated against S33v2 HAR (26 DS + 5 US channels).

**HAR evidence:**

- First request: GET `/` → HTML page loading HNAP JavaScript
  (Login.js, SOAPAction.js, hmac_md5.js)
- POST `/HNAP1/` with `HNAP_AUTH` header, SOAPAction: Login
- Login response: JSON with Challenge, PublicKey, Cookie
- Data requests: POST `/HNAP1/` with SOAPAction: GetMultipleHNAPs
- Data response: JSON with `GetMultipleHNAPsResponse` containing
  action responses with delimiter-separated channel data
- DS channel data: `"1^Locked^QAM256^24^567000000^3^41^0^0^|+|..."`
- US channel data: `"1^Locked^SC-QAM^1^6400000^38400000^47.0^|+|..."`

**Generated modem.yaml:**

```yaml
manufacturer: "{Manufacturer}"
model: "{Model}"
transport: hnap
default_host: "192.168.100.1"

auth:
  strategy: hnap
  hmac_algorithm: md5  # from HNAP_AUTH hash length (32 hex chars)

hardware:
  docsis_version: "3.1"

status: awaiting_verification
```

**Generated parser.yaml (abbreviated):**

```yaml
downstream:
  format: hnap
  response_key: "GetCustomerStatusDownstreamChannelInfoResponse"
  data_key: "CustomerConnDownstreamChannel"
  record_delimiter: "|+|"
  field_delimiter: "^"
  channels:
    - { index: 1, field: lock_status, type: string }
    - { index: 2, field: channel_type, type: string }
    - { index: 3, field: channel_id, type: integer }
    - { index: 4, field: frequency, type: frequency }
    - { index: 5, field: power, type: float }
    - { index: 6, field: snr, type: float }
    - { index: 7, field: corrected, type: integer }
    - { index: 8, field: uncorrected, type: integer }
  channel_type:
    index: 2
    map:
      "QAM256": "qam"
      "OFDM PLC": "ofdm"

upstream:
  format: hnap
  response_key: "GetCustomerStatusUpstreamChannelInfoResponse"
  data_key: "CustomerConnUpstreamChannel"
  record_delimiter: "|+|"
  field_delimiter: "^"
  channels:
    - { index: 1, field: lock_status, type: string }
    - { index: 2, field: channel_type, type: string }
    - { index: 3, field: channel_id, type: integer }
    - { index: 4, field: symbol_rate, type: frequency }
    - { index: 5, field: frequency, type: frequency }
    - { index: 6, field: power, type: float }
  channel_type:
    index: 2
    map:
      "SC-QAM": "qam"
      "OFDMA": "ofdma"

system_info:
  sources:
    - format: hnap
      response_key: "GetCustomerStatusConnectionInfoResponse"
      fields:
        - { source: CustomerConnSystemUpTime, field: system_uptime, type: string }
        - { source: StatusSoftwareModelName, field: model_name, type: string }
    - format: hnap
      response_key: "GetArrisDeviceStatusResponse"
      fields:
        - { source: FirmwareVersion, field: firmware_version, type: string }
```

### Example 3: HTTP modem with JSON API and no auth

**HAR evidence:**

- First request: GET `/` → 200 with HTML page (modem info, no login)
- GET `/api/v1/downstream` → 200, `application/json`
- Response: `{"downstream": {"channels": [{"channelId": 1, ...}]}}`

**Generated modem.yaml:**

```yaml
manufacturer: "{Manufacturer}"
model: "{Model}"
transport: http
default_host: "192.168.100.1"

auth:
  strategy: none

hardware:
  docsis_version: "3.1"

status: awaiting_verification
```

**Generated parser.yaml (abbreviated):**

```yaml
downstream:
  format: json
  resource: "/api/v1/downstream"
  array_path: "downstream.channels"
  channels:
    - key: "channelId"
      field: channel_id
      type: integer
    - key: "frequency"
      field: frequency
      type: integer
    # ...
```

---

## Assumptions and Limitations

1. **One HAR = one transport.** The tools do not support mixed-transport
   modems (no known examples exist).

2. **DOCSIS version inference is value-based.** The tools check actual
   `channel_type` map values for OFDM/OFDMA — not just the presence of
   a `channel_type` field. A DOCSIS 3.0 modem with a `channel_type`
   column (QAM/ATDMA only) correctly returns "3.0". Human should verify.

3. **HMAC algorithm detection is best-effort.** HNAP hash length
   heuristic works for MD5 (32 hex) vs SHA256 (64 hex) but can't
   distinguish other algorithms. No other algorithms are currently
   known in the modem landscape.

5. **Restart actions are almost never in HAR captures.** Contributors
   capture status pages, not admin operations. Restart config is
   typically added from modem documentation or code inspection, not
   from HAR analysis.

6. **Password encoding detection is heuristic.** Two signals (first
   match wins): (1) POST body — if the password value matches
   `[A-Za-z0-9+/=]+` and decodes to printable UTF-8, flag as
   `encoding: base64`. (2) Login page JavaScript — if the page
   preceding the login POST contains a base64 `keyStr` constant or
   `btoa()` call applied to the password field, flag as `encoding:
   base64`. Otherwise default to `plain`.

7. **The tools target the current spec.** All generated configs conform
   to [MODEM_YAML_SPEC.md](../../cable_modem_monitor_core/docs/MODEM_YAML_SPEC.md) and [PARSING_SPEC.md](../../cable_modem_monitor_core/docs/PARSING_SPEC.md) schemas.

8. **JS endpoint discovery is regex-based.** Only static URL string
   literals in AJAX/fetch calls are detected. Dynamically constructed
   URLs (`baseUrl + path`, template literals) are not detected.
