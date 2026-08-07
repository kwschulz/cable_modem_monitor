# modem.yaml Specification

modem.yaml is the configuration file for a cable modem. It declares
everything the system needs to connect, authenticate, and interact with
a modem's web interface — everything except parsing, which lives in
parser.yaml and parser.py.

Single-variant modems use `modem.yaml`. Multi-variant modems use
`modem-{variant}.yaml` files — one per firmware variant. All variants
in a directory share the same `parser.yaml` (and optional `parser.py`).

See [MODEM_DIRECTORY_SPEC.md](MODEM_DIRECTORY_SPEC.md) for the full
directory layout and multi-variant assembly contract.

## Principles

Three principles govern modem.yaml. Every field in this spec is shaped
by them, and any new field proposal must satisfy them.

### Modem Behavior Is Data-Driven

Core provides a fixed set of behaviours (auth strategies, session
patterns, action executors, parser formats, extraction modes). Each
modem selects from them via `modem.yaml`. No modem's configuration
requires code changes; no modem's configuration affects another.

When a new modem doesn't fit, the answer is either (a) it composes
from existing behaviours and the YAML just hasn't been written, or
(b) Core needs a new behaviour added — never (c) special-case logic
for that one modem.

### Config Fields Are Parameters, Not Implementations

Every field in `modem.yaml` is a parameter to a Core behaviour, not
a raw implementation. `auth.strategy: form` selects a strategy;
`endpoint_pattern: "RouterStatus"` supplies a keyword to a Core
extraction strategy.

Contributors provide *what*, Core handles *how*. If a config field
would require regex, code, or implementation knowledge from the
contributor, the abstraction is wrong — the field should be a
higher-level parameter that Core decodes, not a passthrough to an
implementation detail.

### Catalog Tools Intake Is the Onboarding Path

New modems are added through the Catalog Tools pipeline
(`/modem-intake` skill or the equivalent function calls), not by
hand-constructing files. The pipeline validates against this spec
end-to-end: HAR analysis → config generation → validation → trial
parse → test fixtures. Manual construction bypasses that validation
and is a recurring source of drift.

The pipeline is plain Python; AI assistance helps with the judgment
layer (which auth strategy fits, which fields map where). See
[ONBOARDING_SPEC.md](../../cable_modem_monitor_catalog_tools/docs/ONBOARDING_SPEC.md)
for the contract.

---

## Contents

| Section | What it covers |
|---------|----------------|
| [Principles](#principles) | The three rules every field obeys |
| [Schema Overview](#schema-overview) | Complete YAML skeleton with annotations |
| [Identity](#identity) | manufacturer, model, transport, default_host, aliases |
| [Auth](#auth) | 10 strategy types with full config examples |
| [Session](#session) | Cookie, single-session, SPA patterns |
| [Actions](#actions) | Restart and logout — http and hnap types |
| [Hardware](#hardware) | DOCSIS version, hw_version, firmware, chipset |
| [Timeout](#timeout) | Per-request override |
| [Health](#health) | Health probe configuration (fragile modems) |
| [Metadata](#metadata) | Status, attribution, sources, ISPs, notes |
| [Validation Rules](#validation-rules) | Transport constraints, required fields, consistency checks |
| [Multi-Variant Modems](#multi-variant-modems) | Naming, shared files, assembly |
| [Capabilities](#capabilities) | Implicit from parser output |
| [Complete Examples](#complete-examples) | 5 pattern-based modem configs |

---

## Schema Overview

> **Manufacturer is stored as the modem reports it / as the
> manufacturer styles their brand.** Don't pre-normalize case here.
> Display layers handle normalization for readability. The example
> below uses `ARRIS` because that's how the SB8200 self-identifies.

```yaml
# Identity
manufacturer: "ARRIS"
model: "SB8200"
model_aliases:                    # optional — alternate user-facing names; shown in parentheses
  - "CommScope SB8200"
brands:                           # optional — user-visible brand names; become manufacturer-dropdown choices
  - "Surfboard"
transport: http                    # http | hnap | cbn
default_host: "192.168.100.1"

# Auth
auth:
  strategy: url_token
  cookie_name: "sessionId"        # auth owns the cookie it produces
  token_prefix: "ct_"             # url_token only
  # ... strategy-specific fields

# Session (lifecycle only — independent from auth)
session:
  # ... session fields

# Actions (optional)
actions:
  restart:
    # ... action config
  logout:
    # ... action config

# Hardware
hardware:
  docsis_version: "3.1"
  chipset: "Broadcom BCM3390"

# Timeout (optional, default 10)
timeout: 15

# Health (optional, defaults are correct for most modems)
health:
  http_probe: false      # disable TCP/HEAD probes for fragile modems
  supports_head: false   # modem rejects HTTP HEAD (HEAD probe skipped, no fallback)
  supports_icmp: true    # hint; auto-detection overrides at setup

# Metadata
status: confirmed
sources:
  auth_config: "#81"
  chipset: "FCC filing"
attribution:
  contributors:
    - github: "https://github.com/kwschulz"
      contribution: "Initial HAR capture and fixtures"
isps:
  - "Comcast"
  - "Spectrum"
pii_fields:                  # optional — system_info keys with PII beyond global defaults
  - home_ssid
notes: |
  SB8200 HTTPS variant with URL token auth.
references:
  issues:
    - 42
    - 81
```

---

## Identity

| Field | Type | Required | Description |
|-------|------| :--------: |-------------|
| `manufacturer` | string | yes | Manufacturer name (e.g., "Arris", "Netgear", "Motorola") |
| `model` | string | yes | Model identifier (e.g., "SB8200", "CM1200") |
| `model_aliases` | list[string] | no | Alternate user-facing model names — rebadges, regional variants, sticker codes (e.g., `["CGM4140COM"]`). Shown in the model line's parenthetical. Firmware-internal codes do not belong here. See [Aliases vs Separate Entries](#aliases-vs-separate-entries). |
| `brands` | list[string] | no | User-visible brand names from the product/box (e.g., `["Surfboard"]`, or `["Arris"]` on CommScope-made hardware). Feed the config flow's manufacturer dropdown alongside `manufacturer` — a rebranded modem appears under both names while staying one record. Entries must be sourced. |
| `transport` | enum | yes | `http`, `hnap`, or `cbn` |
| `default_host` | string | yes | Default IP address (e.g., "192.168.100.1") |

`transport` identifies the transport protocol (`http`, `hnap`, or
`cbn`). For `http`, auth, session, and format are configured
independently. For `hnap` and `cbn`, the transport constrains
auth, format, and action types. See [Validation Rules](#validation-rules)
for details.

`default_host` is the pre-filled value in the config flow. Users can
override it during setup. Most cable modems use `192.168.100.1`.

### Aliases vs Separate Entries

Each model a user can purchase or identify by name gets its own catalog
directory and `modem.yaml`. The config flow shows each model as a
first-class entry — users should find their modem by the name on the
box, not reason about internal compatibility.

**Separate catalog entry** (own directory under `modems/{mfr}/{model}/`):

- Different product a user would purchase by name
- Different hardware revision of the same product line
- Different brand selling compatible hardware

Parser and config may be identical to another entry. That is an
implementation detail. The new entry's `references` section links to
compatible models. Having a standalone entry enables diagnostics
collection and golden file validation for that specific model.

A separate entry also requires its own evidence — a HAR capture in
`test_data/`. Until a capture exists for a rebadged or sibling
product, it is recorded as an alias on the evidenced entry — see
`model_aliases` below — and graduates to its own entry when evidence
arrives.

**model_aliases** (alternate user-facing names — shown in the model
line's parenthetical):

- Manufacturer rebrand of the same product (e.g., acquirer name)
- Alternate model numbers users encounter on boxes, device stickers,
  or ISP paperwork
- Marketing name variants for the same hardware

Firmware-internal identifiers (product codes, platform strings from
firmware responses) do not belong in `model_aliases` — they are
evidence in the HAR and candidates for a future detection block, not
names users recognize.

Aliases appear in the model line's parenthetical so users can match an
alternate name; the primary `model` name leads the label.

**brands** (user-visible names — a manufacturer-dropdown dimension):

Box-level brand names (`Surfboard`, or `Arris` on CommScope-made
hardware) belong in `brands`, not `model_aliases`. Each entry becomes a
choice in the config flow's manufacturer dropdown and also appears in
the model line's parenthetical, so the modem is findable under the name
on the box while remaining one catalog record. See
ARCHITECTURE_DECISIONS § Brand names as manufacturer-step choices.

---

## Auth

Auth declares how the system authenticates with the modem's web
interface. The `strategy` field selects the auth implementation; the
remaining fields are strategy-specific configuration.

```yaml
auth:
  strategy: form            # Discriminator — selects the auth class
  action: "/goform/login"   # Strategy-specific fields
  username_field: "loginUsername"
  password_field: "loginPassword"
  encoding: base64
```

Each modem.yaml (or modem-{variant}.yaml) has exactly one auth
strategy. Multi-variant modems that differ only in auth get separate
variant files.

### `none`

No authentication required. Data endpoints are publicly accessible.

```yaml
auth:
  strategy: none
```

No additional fields. This is the simplest case — modems that expose
their status pages without any login.

**Success detection:** Always succeeds — no login is attempted.

Evidence: modems with publicly accessible status pages (no login
required). Common on older DOCSIS 3.0 modems and some ISP-branded
devices that expose read-only status endpoints.

### `basic`

HTTP Basic Authentication. Credentials sent as `Authorization: Basic`
header on every request.

```yaml
auth:
  strategy: basic
  challenge_cookie: false
  cookie_name: ""
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `challenge_cookie` | bool | `false` | If `true`, retry with server-set cookie on initial 401. Some firmware returns a challenge cookie that must be included in the retry. |
| `cookie_name` | string | `""` | Session cookie produced by login. Empty = stateless. Auth owns the cookie it produces — see ARCHITECTURE_DECISIONS.md "Session is lifecycle, auth owns the cookie." |

**Success detection:** Always succeeds. Basic auth is per-request
(credentials sent on every request), so there is no login response
to evaluate. If `challenge_cookie` is enabled and the initial request
fails with a network error, that is reported as a failure.

Evidence: modems that return a `WWW-Authenticate: Basic` header on
401 responses. Some modems also require a challenge cookie on retry.

### `form`

HTML form POST login. The system POSTs credentials to the specified
endpoint and evaluates the response for success.

```yaml
auth:
  strategy: form
  action: "/goform/login"
  method: POST
  username_field: "loginUsername"
  password_field: "loginPassword"
  encoding: plain
  cookie_name: "SessionID"
  hidden_fields:
    webToken: ""
  success:
    redirect: "/MotoHome.asp"
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `action` | string | required | Form POST URL |
| `method` | string | `POST` | HTTP method |
| `username_field` | string | `"username"` | Form field name for username |
| `password_field` | string or list | `"password"` | Form field name(s) for password. All fields receive the same encoded password. Use a list when the modem POSTs the password to multiple form fields. |
| `encoding` | enum | `plain` | `plain` or `base64` — how the password is encoded before POST |
| `cookie_name` | string | `""` | Session cookie produced by login. Auth owns the cookie it produces — see ARCHITECTURE_DECISIONS.md. |
| `hidden_fields` | map | `{}` | Static overrides or supplements for form hidden fields. Values here take precedence over fields discovered from the `login_page` form. Use when the form's default value isn't what the modem expects, or to add fields not present in the HTML. |
| `login_page` | string | `""` | Page to fetch before login. Establishes session cookies and reads `<input type="hidden">` fields from the login form. Discovered hidden fields are included in the POST automatically. If empty, POST directly without pre-fetch. |
| `form_selector` | string | `""` | CSS selector to identify the login form on `login_page` for hidden-field discovery. If empty, uses the first `<form>` found. Also used by MCP intake at build time. |
| `success.redirect` | string | `""` | Expected redirect URL after successful login |
| `success.indicator` | string | `""` | String to match in response body to confirm success |

**Form POST body:** When `login_page` is set, the auth manager
pre-fetches the page and reads `<input type="hidden">` fields from
the login form. Merge order: discovered fields (base) ← `hidden_fields`
(override) ← credentials (override). This automatically supports
modems with dynamic CSRF tokens (e.g., server-generated `webToken`).
When `login_page` is empty, the POST body is built entirely from
config: `username_field`, `password_field` entries, and
`hidden_fields`.

**Success detection:** If `success` is provided, checks `redirect`
(path substring match) and/or `indicator` (body substring match).
Either may be omitted; if both are present, both must pass. A block
naming neither is a schema error, because it would check nothing while
reading as verification. If `success` is omitted entirely, any response
with HTTP status < 400 is treated as success.

An HTTP status of 400 or above always fails the login, whether or not
`success` is provided. Criteria narrow what counts as success; they
never widen it.

**A declared `success` block is a claim about the login.** It is what
makes `form` report a post-login 401 as `SESSION_REJECTED` rather than
`CREDENTIALS_SUSPECT` (ARCHITECTURE_DECISIONS.md § Post-login 401 is
read per auth strategy). Declare it only when the criterion genuinely
distinguishes an accepted login from a refused one on that modem.

**Which criterion to reach for.** `redirect` suits a modem whose login
answers 3xx and lands on a known page; every entry declaring `success`
today uses it. `indicator` is for a modem that answers the login **200
in place**, with no redirect to match — `cm3500b`, `coda56`, and
`dm1000` all behave that way, so `redirect` could never verify their
logins. Neither is retired for lack of current use.

**When to leave `success` omitted:** Many modems return 200 plus the
login page body when credentials are rejected (e.g., MB7621 returns
a 200 redirect chain ending at `/login.asp`). The loose check above
classifies this as auth success; the loader's `Data page X appears
to be a login page` detection then catches the mismatch on the next
fetch and surfaces it as `LOAD_AUTH`. That defense-in-depth is the
intended backstop and works correctly in practice.

Tightening via `success.redirect` to "fix" this class of modem
behavior is a rejected pattern. Post-auth landing URLs are
firmware-version-coupled — new firmware that lands on a different
page (firmware-update prompt, change-password flow, captive state)
breaks auth where it didn't before. A prior alpha cycle hit this
from the other direction: a `Login redirect mismatch: expected
path containing '/DocsisStatus.htm', got '/ErrorMsg.htm'` error
looked like a regression and a softening fix was drafted, but the
strict check was correctly identifying a real auth failure that
softening would have masked. The principle runs both directions —
don't tighten via redirect when the loose check is fine; don't
soften the redirect check when it's catching real failures.
Configure `success.redirect` only when the modem has a stable,
well-known post-auth landing path that doesn't drift across
firmware updates and the loose check produces an unacceptable
failure mode for that specific modem.

Evidence: modems with HTML login forms. Encoding, field names, and
success indicators vary by manufacturer. Both `goform` and `cgi-bin`
style endpoints are common.

### `form_nonce`

Form POST with a **client-generated nonce** — a random value
created fresh for each login request and sent alongside credentials
as a separate form field.

#### What is a nonce?

A **nonce** ("**n**umber used **once**") is a random value included
in a request to prevent replay attacks. If an attacker intercepts a
login POST, they cannot resubmit it later because the server
expects each request to carry a fresh, unique nonce. The concept
originates from cryptographic protocols and is formalized in
standards including:

- **RFC 7616** (HTTP Digest Authentication) — server-generated
  nonces in `WWW-Authenticate` challenges
- **RFC 6749 / RFC 9207** (OAuth 2.0) — `state` parameter as a
  CSRF/replay nonce
- **OWASP CSRF Prevention** — synchronizer tokens and
  double-submit cookies

Cable modem `form_nonce` auth is a simplified variant: the client
generates a random numeric string (like OAuth 1.0's `oauth_nonce`)
and includes it in the login POST. No server challenge is needed —
the nonce is purely client-originated.

#### Core pattern

Two properties distinguish `form_nonce` from `form`:

1. **Client-generated nonce** — a random value created by JavaScript
   before submission (not extracted from the server or a page).
   Contrast with server-generated tokens like CSRF `webToken` fields,
   which belong to the `form` strategy with `login_page` pre-fetch.
2. **Text-prefix response** — the server returns a plain-text body
   starting with `Url:` (success, followed by redirect path) or
   `Error:` (failure, followed by message). No HTTP redirect, no
   Set-Cookie on the login response itself.

These two properties are the **invariants** of the strategy. The
variable parts (field names, nonce length, credential encoding)
are configurable per modem. When a new modem matches both
invariants, it should use `form_nonce` with its own field names —
not a new strategy.

#### Distinguishing from similar patterns

| Pattern | Nonce source | Response evaluation | Strategy |
|---------|-------------|-------------------|----------|
| Plain form login | None | Redirect or cookie | `form` |
| Form with server token | Server (hidden field in login page) | Redirect or cookie | `form` with `login_page` |
| Form with client nonce | Client (JavaScript random) | Text prefix (`Url:` / `Error:`) | `form_nonce` |
| AJAX with client nonce + base64 | Client (JavaScript random) | Text prefix | `form_nonce` (same endpoint, different encoding) |

```yaml
auth:
  strategy: form_nonce
  action: "/cgi-bin/adv_pwd_cgi"
  username_field: "username"
  password_field: "password"
  nonce_field: "ar_nonce"
  nonce_length: 8
  cookie_name: "credential"
  success_prefix: "Url:"
  error_prefix: "Error:"
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `action` | string | required | Form POST URL |
| `username_field` | string | `"username"` | Form field name for the username credential |
| `password_field` | string | `"password"` | Form field name for the password credential |
| `nonce_field` | string | required | Form field name for the client-generated nonce |
| `nonce_length` | int | `8` | Length of the random nonce string (digits only) |
| `cookie_name` | string | `""` | Session cookie produced by login. Auth owns the cookie it produces — see ARCHITECTURE_DECISIONS.md. |
| `success_prefix` | string | `"Url:"` | Response body prefix indicating successful login |
| `error_prefix` | string | `"Error:"` | Response body prefix indicating failed login |
| `credential_encoding` | enum | `"plain"` | `"plain"` or `"b64_packed"`. Not set in modem.yaml — detected at setup time and stored in HA config entry. |
| `credential_field` | string | `""` | Hidden field name for packed credentials (e.g., `"arguments"`). Empty for plain encoding. Not set in modem.yaml. |

**Success detection:** Matches the response body text against
`success_prefix` (default `"Url:"`) and `error_prefix` (default
`"Error:"`). If the body starts with `error_prefix`, the login
failed and the text after the prefix is the error message. If it
starts with `success_prefix`, the login succeeded and the text
after the prefix is the redirect path. Both are configurable per
modem.

#### Login page pre-fetch and encoding detection

The credential encoding is detected at **setup time** (HA config
flow or test harness) by pre-fetching the login page (GET to the
`action` URL) and inspecting the form structure:

- **Plain encoding** — the form has `<input name="{username_field}">`
  and `<input name="{password_field}">`. Credentials are sent as
  separate form fields: `username=X&password=Y&nonce=Z`.
- **Base64-packed encoding** — the form has no named credential
  inputs but contains a hidden field (e.g., `<input name="arguments"
  type="hidden">`) with an empty value. Credentials are packed as
  `base64(encodeURIComponent("username=X:password=Y"))` into that
  field: `arguments=<base64>&nonce=Z`.

The detected encoding is stored in the HA config entry as
`credential_encoding` (`"plain"` or `"b64_packed"`) and
`credential_field` (the hidden field name, empty for plain). At
runtime, the auth manager reads these from the `FormNonceAuth`
config — no pre-fetch or detection occurs during polling.

Detection falls back to plain encoding on any parse failure
(backward compatible). No YAML config field is needed — the
encoding is per-installation (firmware-dependent), not per-modem.

The test harness detects encoding from HAR entries at test
execution time, using the same `_analyze_login_form()` function.

Evidence: observed in Arris SB6190 firmware 9.1.103AA65L (plain
form fields) and 9.1.103AA72 (base64-packed `arguments` field).
Both firmware variants share identical modem.yaml config — same
endpoint, same field names, same cookie, same response parsing.
The only difference is the login page form structure.

### `url_token`

Credentials encoded in the URL query string. The response sets a
session cookie; subsequent requests use a server-issued token in the
query string.

```yaml
auth:
  strategy: url_token
  login_page: "/cmconnectionstatus.html"
  login_prefix: "login_"
  success_indicator: "Downstream Bonded Channels"
  ajax_login: true
  auth_header_data: false
  cookie_name: "sessionId"
  token_prefix: "ct_"
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `login_page` | string | required | Page URL that accepts the token login |
| `login_prefix` | string | `""` | Prefix before base64 token in login URL. Empty = bare base64. |
| `success_indicator` | string | `""` | Dual-purpose field: auth success check AND response type discriminator. See below. |
| `ajax_login` | bool | `false` | Include `X-Requested-With: XMLHttpRequest` header on login |
| `auth_header_data` | bool | `false` | Include `Authorization: Basic` header on data requests |
| `cookie_name` | string | `""` | Session cookie produced by login. Auth owns the cookie it produces — see ARCHITECTURE_DECISIONS.md. |
| `token_prefix` | string | `""` | URL token prefix for subsequent data page requests (e.g., `ct_`). The token value is extracted by the auth manager from the login response body. |
| `inject_credential_cookie` | bool | `false` | After auth, set `cookie_name` to the auth response body (a server-issued session token). Use when the server returns a token in the auth response body and the firmware JS sets it as the credential cookie client-side (e.g., `createCookie("credential", result)` where `result` is the response body). Auth fails if the body is empty — this field requires a non-empty server response. |

**Success detection and response type discrimination:**

`success_indicator` serves two roles for `url_token`:

1. **Data page detection:** Response body **contains** `success_indicator`
   → the body is the data page itself (modem served data directly during
   login). Token is `None` — no URL injection needed. The response is
   passed to the loader as an auth response reuse candidate
   (`AuthResult.response` and `AuthResult.response_url` are set).
2. **Token extraction gate:** Response body **does not contain** indicator
   → the body is treated as a server-issued session token (typically
   20-40 chars alphanumeric). Extracted via `response.text.strip()` →
   `auth_context.url_token`. `AuthResult.response`/`response_url` MUST
   stay unset — see reuse contract below.
3. **Empty body fallback:** Body is empty → fall back to cookie via
   `cookie_name`. `AuthResult.response`/`response_url` MUST stay unset.
4. **Empty cookie fallback:** Cookie is empty → no token injection
   (loader attempts without). `AuthResult.response`/`response_url`
   MUST stay unset.
5. **Login-page body (not a token):** The login response is the login
   page itself — auth did not establish a session (single-session
   contention, silent redirect, expired credentials). A token is a
   single-line, header-safe value; a login page is multi-line HTML.
   When `inject_credential_cookie` is set, a body that is not
   header-safe is **not** injected as `cookie_name` — injecting it
   would corrupt the next request's headers. The credential cookie is
   left unset and the subsequent data fetch is classified by the
   loader's login-page detection as `LOAD_AUTH` (the existing
   self-correcting path — see `ORCHESTRATION_USE_CASES.md` UC-19b).
   This branch never raises and never reports a spurious success with
   a corrupt cookie. Regression: SB8200 inject variant #124 (rct —
   login-page body stuffed into the `credential` cookie crashed
   `http.client.putheader`).

The collector prefers `auth_context.url_token` (body-derived) over
cookie extraction when both are available. This ordering matters because
on some firmware variants (SB8200, Issue #81) the response body token
differs from the cookie value.

Without the `success_indicator` guard, the auth manager would use an
entire HTML data page as a URL parameter, silently breaking any
url_token variant where the login response returns data directly.

**Reuse contract — load-bearing for url_token.** The login URL and a
parser data page often share the same path (SB8200 logs in at
`/cmconnectionstatus.html` and parses the same path). Branches 2-4
above produce a non-data-page response at that path; advertising it
for loader reuse causes the loader to surface the auth artefact —
or empty body — as the parsed data page and skip the real fetch.
See `RESOURCE_LOADING_SPEC.md` § Auth Response Reuse and the
`AuthResult` docstring in `auth/base.py` for the full contract.
Regression: SB8200 #81 (v3.14.0-beta.2 dtaubert — 0/0 channels).

**Pre-login cookie clearing:** Before the login request, the auth
manager deletes any existing session cookie (`cookie_name`) from the
session. This matches the browser's `eraseCookie("sessionId")` call
before `$.ajax` login — without it, the modem rejects re-login
attempts with 401 because it sees an active session.

Evidence: modems that encode credentials in URL query strings and
issue session tokens for subsequent requests. Firmware variants of
the same model may use different login prefix values.

### `hnap`

HNAP HMAC challenge-response authentication. The system performs a
Login SOAP action, computes an HMAC signature from the challenge, and
signs all subsequent requests with `HNAP_AUTH` headers.

```yaml
auth:
  strategy: hnap
  hmac_algorithm: md5
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `hmac_algorithm` | enum | required | `md5` or `sha256` |

HNAP auth is fully constrained by the transport. The endpoint (`/HNAP1/`),
session mechanism (`uid` + `PrivateKey` cookies, `HNAP_AUTH` header), and
protocol are all fixed. Only the HMAC algorithm varies across modems.

**Success detection:** The HNAP login response is a JSON object with
a `LoginResult` field. Accepted values: `"OK"` or `"OK_CHANGED"`
(success). `"FAILED"` means wrong credentials. `"LOCKUP"` or
`"REBOOT"` mean the firmware's anti-brute-force mechanism has
triggered (temporary lockout or forced device restart). These are
protocol-fixed — not configurable per modem.

Evidence: modems using the HNAP protocol with HMAC challenge-response.
The HMAC algorithm (MD5 or SHA256) varies across models and hardware
revisions.

### `form_pbkdf2`

Multi-round-trip challenge-response using PBKDF2 key derivation.
See [AUTH_PBKDF2_SPEC.md](AUTH_PBKDF2_SPEC.md) for the full protocol,
encoding rules, and firmware assumptions.
The client requests server-provided salts, derives a key via PBKDF2,
and submits the derived hash. Structurally closer to HNAP's
challenge-response than to a simple form POST.

```yaml
auth:
  strategy: form_pbkdf2
  login_endpoint: "/api/v1/session/login"
  salt_trigger: "seeksalthash"
  pbkdf2_iterations: 1000
  pbkdf2_key_length: 128
  double_hash: true
  csrf_init_endpoint: "/api/v1/session/init_page"
  csrf_header: "X-CSRF-TOKEN"
  cookie_name: "PHPSESSID"

session:
  headers:
    X-Requested-With: "XMLHttpRequest"

actions:
  logout:
    type: http
    method: POST
    endpoint: "/api/v1/session/logout"
    requires_session: true
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `login_endpoint` | string | required | URL for both salt request and login POST |
| `salt_trigger` | string | `"seeksalthash"` | Password value that triggers salt response |
| `pbkdf2_iterations` | int | required | PBKDF2 iteration count |
| `pbkdf2_key_length` | int | required | Derived key length in bits |
| `double_hash` | bool | `true` | Hash twice: first with `salt`, then with `saltwebui` |
| `csrf_init_endpoint` | string | `""` | Endpoint to fetch a fresh CSRF token (e.g., `/api/v1/session/init_page`). Called before each POST that requires CSRF (login, logout). If empty, CSRF token is extracted from the login response and reused for all POSTs. |
| `csrf_header` | string | `""` | Header name for the CSRF token (e.g., `X-CSRF-TOKEN`). The `form_pbkdf2` strategy fetches the token value (via `csrf_init_endpoint` or login response) and attaches it as this header. Which requests carry the token and how the token is obtained are strategy-specific — other strategies that need CSRF may define their own fields. |
| `cookie_name` | string | `""` | Session cookie produced by login. Auth owns the cookie it produces — see ARCHITECTURE_DECISIONS.md. |
| `login_success` | dict | `{}` | When set, login is considered successful only when every key-value pair in this dict matches the response JSON. Values may be string, integer, or boolean — matched by equality against the parsed JSON response. Use when the firmware signals success with a specific field rather than absence of an error (e.g., Technicolor CGA6444VF returns `{"error": "ok", ...}` — set `login_success: {error: "ok"}`). |

Session-wide headers and logout are declared in their respective
sections. See [Session](#session) and [Actions](#actions).

**Success detection:** HTTP 401 is always treated as failure. If
`login_success` is set, login succeeds only when every key-value
pair in the dict matches the response JSON; any mismatch is treated
as failure (the `message` field provides the error detail). If
`login_success` is empty (the default), any truthy `"error"` field
in the response is treated as failure; absent or falsy is success.

Evidence: modems with JavaScript SPA interfaces that use PBKDF2
key derivation for login. Parameters are typically derived from the
modem's login.js source code in HAR captures.

### `form_sjcl`

See [AUTH_SJCL_SPEC.md](AUTH_SJCL_SPEC.md) for the full protocol,
encoding rules, and firmware assumptions.

SJCL (Stanford JavaScript Crypto Library) AES-CCM encrypted form
auth. Some modem firmwares use the SJCL JavaScript library to
encrypt credentials client-side with AES-CCM before POSTing. The
server response is also encrypted — must decrypt to extract the
CSRF nonce. Key is derived via PBKDF2 from the password and a
per-session salt provided by the server.

Requires the ``cryptography`` package. Install Core with the
``[sjcl]`` extra: ``pip install solentlabs-cable-modem-monitor-core[sjcl]``.

```yaml
auth:
  strategy: form_sjcl
  login_page: "/"
  login_endpoint: "/php/ajaxSet_Password.php"
  session_validation_endpoint: "/php/ajaxSet_Session.php"
  pbkdf2_iterations: 1000
  pbkdf2_key_length: 128
  ccm_tag_length: 16
  encrypt_aad: "loginPassword"
  decrypt_aad: "nonce"
  csrf_header: "csrfNonce"
  cookie_name: "PHPSESSID"

session:
  headers:
    X-Requested-With: "XMLHttpRequest"

actions:
  logout:
    type: http
    method: GET
    endpoint: "/php/logout.php"
    requires_session: true
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `login_page` | string | `"/"` | Page to GET for per-session IV, salt, and session ID (parsed from JS variables `myIv` (hex), `mySalt` (hex), `currentSessionId`) |
| `login_endpoint` | string | required | URL for the encrypted credential POST |
| `session_validation_endpoint` | string | `""` | URL for the post-login session validation POST. If empty, session validation is skipped. |
| `pbkdf2_iterations` | int | required | PBKDF2 iteration count for key derivation |
| `pbkdf2_key_length` | int | required | Derived key length in bits (128 for AES-128) |
| `ccm_tag_length` | int | `16` | AES-CCM authentication tag length in bytes |
| `encrypt_aad` | string | `"loginPassword"` | AAD (Additional Authenticated Data, UTF-8 encoded) for encrypting the login payload |
| `decrypt_aad` | string | `"nonce"` | AAD (UTF-8 encoded) for decrypting the server's nonce response |
| `csrf_header` | string | `""` | Header name for the CSRF nonce extracted from the decrypted login response. If empty, nonce decryption is skipped. |
| `cookie_name` | string | `""` | Session cookie produced by login. Auth owns the cookie it produces — see ARCHITECTURE_DECISIONS.md. |

**Auth flow (encoding boundaries):**

All hex values from JS variables are hex-decoded to binary bytes
before cryptographic operations. This matches SJCL's
`sjcl.codec.hex.toBits()` wrapper. String values (password, AAD)
are UTF-8 encoded.

1. **GET login page** — extract `myIv` (hex), `mySalt` (hex),
   `currentSessionId` from JS variable assignments in the response.
2. **Derive key** —
   `PBKDF2-HMAC-SHA256(password.utf8, hex_decode(mySalt), iterations, key_length)`.
3. **Encrypt** — AES-CCM encrypt
   `JSON({"Password": "<pw>", "Nonce": "<sessionId>"}).utf8`
   with the derived key, `hex_decode(myIv)`, and AAD
   `encrypt_aad.utf8`.
4. **POST login** — send
   `{"EncryptData": hex(ciphertext), "Name": "<user>", "AuthData": "<encrypt_aad>"}`.
   Server responds with
   `{"p_status": "AdminMatch"|"Match", "encryptData": "<hex>"}`.
5. **Decrypt nonce** — AES-CCM decrypt `hex_decode(encryptData)`
   with AAD `decrypt_aad.utf8` to extract the CSRF nonce.
6. **POST session validation** — if `session_validation_endpoint` is
   configured, POST with the `csrf_header` to finalize the session.

**Success detection:** The login response JSON `p_status` field must
be `"AdminMatch"` or `"Match"`. Any other value is treated as failure.

Evidence: Arris Touchstone gateway firmwares that embed the SJCL
library in their web interface. Constants are found in `base_95x.js`
or similar JS files in HAR captures.

### `bearer`

Bearer token auth for REST APIs (RFC 6750). The strategy POSTs a JSON
body to a login endpoint, extracts a token from the JSON response by
walking a dot-separated path, and injects
`Authorization: Bearer <token>` into the session headers for
subsequent requests.

The login request sends `{"username": "<username>", "password": "<password>"}` as the
JSON body. Set `username_field: ""` for password-only firmwares — the
username key is then omitted entirely rather than sent empty.

```yaml
auth:
  strategy: bearer
  login_endpoint: "/api/v1/login"
  token_path: "data.token"
```

| Field | Type | Required | Description |
|-------|------|:--------:|-------------|
| `strategy` | string | yes | Always `"bearer"` |
| `login_endpoint` | string | yes | Path to POST the JSON login body to |
| `token_path` | string | yes | Dot-separated JSON path to the token in the response (e.g., `"created.token"` extracts `response["created"]["token"]`) |
| `username_field` | string | no | Key carrying the username, default `"username"`. Empty string omits the username from the body — required for firmwares that authenticate on a password alone. |
| `user_id_path` | string | no | Dot-separated JSON path to a user identifier in the login response, resolved the same way as `token_path`. Set it only when an action endpoint needs the value. |

**Login request:**

```http
POST <base_url><login_endpoint>
Content-Type: application/json
{"username": "<username>", "password": "<password>"}
```

With `username_field: ""` the body is `{"password": "<password>"}`.

**Token extraction:** the `token_path` value is split on `.` and used
to walk the parsed JSON response. For example, `"created.token"` with
response `{"created": {"token": "abc", "userLevel": "regular"}}`
extracts `"abc"`. Returns an error if any key in the path is missing
or the response is not valid JSON.

**Downstream values:** the extracted token is also stored in
`AuthContext.token`, and `user_id_path` (when set) in
`AuthContext.user_id`. Action endpoints reference both as
`{auth:token}` and `{auth:user_id}` — see
[Auth-value placeholders](#auth-value-placeholders). Firmwares that end
a session by addressing it in the URL path need them; re-reading the
token out of the `Authorization` header would be indirect, and the user
id never reaches the header at all. A `user_id_path` resolving to a
number is stored as its string form. Missing or unresolvable
`user_id_path` leaves `AuthContext.user_id` empty and does not fail the
login.

**Success detection:** any 2xx carrying the token succeeds — token
creation legitimately answers `201 Created`. Non-2xx →
`AuthResult(success=False)`. Missing token path →
`AuthResult(success=False)`. Non-JSON response →
`AuthResult(success=False)`.

**Transport:** `http` only.

**Header injected:** `Authorization: Bearer <token>`. The strategy's
`headers()` method returns `frozenset({"authorization", "cookie"})`.

Evidence: Sagemcom F3896LG REST API, shipped by two Liberty Global
operators. On Virgin Media (Hub 5) the monitoring endpoints are public
and only the restart endpoint at `/rest/v1/system/reboot` needs a token
(issue #82). On Ziggo the capture shows the token on every request
(issue #185). Both firmwares authenticate on a password alone and
answer `POST /rest/v1/user/login` with `201`.

---

### `form_cbn`

See [AUTH_CBN_SPEC.md](AUTH_CBN_SPEC.md) for the full protocol,
encoding rules, and firmware assumptions.

CBN (Compal Broadband Networks) AES-256-CBC encrypted form auth.
Compal modem firmwares use the CryptoJS library to encrypt the
password client-side. The AES key and IV are derived from a rotating
session token cookie — each response rotates the token via
`Set-Cookie`. The login POST goes to a `setter.xml` endpoint with
`fun=N` parameters (same XML POST pattern used for data fetching).

Requires the ``cryptography`` package. Install Core with the
``[cbn]`` extra: ``pip install solentlabs-cable-modem-monitor-core[cbn]``.

```yaml
auth:
  strategy: form_cbn
  login_page: "/common_page/login.html"
  getter_endpoint: "/xml/getter.xml"
  setter_endpoint: "/xml/setter.xml"
  session_cookie_name: "sessionToken"
  sid_cookie_name: "SID"
  username_value: "NULL"
  login_fun: 15

session:
  headers:
    X-Requested-With: "XMLHttpRequest"

actions:
  logout:
    type: cbn
    fun: 16
  restart:
    type: cbn
    fun: 8
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `login_page` | string | `"/common_page/login.html"` | Page to GET for the initial `sessionToken` cookie |
| `getter_endpoint` | string | `"/xml/getter.xml"` | URL for XML POST data fetches (`fun=N` parameters) |
| `setter_endpoint` | string | `"/xml/setter.xml"` | URL for login, logout, and action POSTs |
| `session_cookie_name` | string | `"sessionToken"` | Cookie that carries the rotating session token. Rotates on every response. |
| `sid_cookie_name` | string | `"SID"` | Secondary session cookie set after login. Extracted from login response body. Stable for the session lifetime. |
| `username_value` | string | `"NULL"` | Username sent in the login POST. Compal modems use single-password auth — literal `"NULL"` is the default. |
| `login_fun` | int | `15` | `fun` parameter value for the login POST to `setter_endpoint` |

**Auth flow:**

1. **GET login page** — receive initial `sessionToken` cookie.
2. **Derive AES key and IV** — `key = SHA256(sessionToken)` (32 bytes),
   `iv = MD5(sessionToken)` (16 bytes).
3. **Encrypt password** — AES-256-CBC with PKCS7 padding. Output is
   `base64(":" + hex(ciphertext))`. This replicates the `CBN_Encrypt`
   function from Compal's `encrypt_cryptoJS.js`.
4. **POST login** — send to `setter_endpoint`:
   `token=<sessionToken>&fun=<login_fun>&Username=<username_value>&Password=<encrypted>`.
   **Token must be the first parameter** (modem firmware rejects other
   orderings).
5. **Check response** — HTTP status is always 200. Body contains
   `"successful"` on success or `"idloginincorrect"` on failure.
   Extract SID from response body via `SID=(\d+)` regex.
6. **Set SID cookie** — domain must match the modem hostname (not empty
   domain), otherwise `requests` won't send the cookie for IP addresses.

**Success detection:** Response body must contain `"successful"` AND
HTTP status must be 200. A 302 redirect to the login page also
contains `"successful"` in JS template strings — checking both status
and body prevents false positives.

**Lockout indicators:** Pre-login `getter.xml?fun=1` (GlobalSettings)
exposes `<LockedOut>` and `<AccessDenied>` fields. When `LockedOut`
is not `"Disable"`, the modem has temporarily locked the account
after too many failed attempts.

Evidence: Compal
[CH7465MT](../../cable_modem_monitor_catalog/solentlabs/cable_modem_monitor_catalog/modems/compal/ch7465mt/modem.yaml)
(Magenta AT / UPC / Ziggo / Virgin Media "Connect Box"). The
`CBN_Encrypt` function is in `encrypt_cryptoJS.js` which loads CryptoJS
v3.1.2 (`AES.js`, `sha256.js`, `md5.js`).

---

## Session

Session owns post-login lifecycle: concurrency limits and session-wide
HTTP headers. Auth strategies own login mechanics and cookie names —
the cookie is an output of the login flow, so auth owns it. See
ARCHITECTURE_DECISIONS.md "Session is lifecycle, auth owns the cookie."

How to end a session (logout) is declared in `actions.logout` — see
[Actions](#actions). Session declares the *lifecycle*; auth declares
the *credentials and cookies*; actions declare the *operations*.

For HNAP, session is implicit (always `uid` + `PrivateKey` cookies,
`HNAP_AUTH` header) — no session block needed.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `headers` | map | `{}` | Static headers added to all requests for this session (e.g., `X-Requested-With: XMLHttpRequest` for SPA-style modems). Header values support the `{base_url}` placeholder, which resolves to the modem's URL at session-build time — used for `Referer`/`Origin` headers that some modems validate against their own origin. Dynamic headers (CSRF tokens, HNAP signatures, auth tokens) are managed by auth strategies — each strategy defines its own fields for token acquisition and header injection. |
| `query_params` | map | `{}` | Static query parameters appended to every URL Core fetches for this session — data resources, `post_login_endpoints`, and actions alike (e.g., `_n: "12345"` for Arris firmware that requires a cache-buster nonce on AJAX requests). Not used for auth-managed tokens — those go through `auth.token_prefix`. |
| `post_login_endpoints` | list | `[]` | Paths to GET, in order, on every fresh login, ahead of any data request. See [Post-login endpoints](#post-login-endpoints). |

### Post-login endpoints

Some SPA firmware treats the browser's first post-login call as part of
establishing the session, and rejects data requests until it has been
made. Declare those paths and Core GETs them, in order, after a fresh
login and before the first data fetch:

```yaml
session:
  post_login_endpoints:
    - "/api/v1/session/menu"
```

Responses are discarded — this is lifecycle, not data. The calls belong
to login, so they run on every successful fresh authentication and
nothing runs on a reused session. That includes a restart action, which
authenticates without ever collecting data.

A non-2xx response or a transport error logs a WARNING and collection
continues. Login already succeeded, so failing here would report bad
credentials for a working password; if the call really was required,
the data fetch reports the failure itself with the warning naming the
missed call alongside it.

Evidence: a path the browser requests immediately after login that the
firmware rejects when unauthenticated. Cite the capture, and any
independent non-browser client that makes the same call — a headless
client has no UI to render, so it is strong evidence the call is
session establishment rather than chrome.

### Stateless

No session section needed (or `session: {}`). Each request is
independent — the auth manager attaches credentials per-request.
Common with `none` and `basic` auth.

### Cookie-based

Cookie-based modems declare `cookie_name` on the auth strategy (not
in session). The session section is only needed if the modem has
concurrency limits, explicit logout, or session-wide headers. After
successful auth, the modem sets the cookie declared in `auth.cookie_name`.
The session maintains this cookie across requests.

### Single-session modems

Some modems permit only one active session at a time. Firmware differs
in who loses when a second login arrives: some refuse the newcomer,
answering 5xx (see UC-87a); others accept it and evict the incumbent,
so it is the displaced client whose next request fails. Either way the
integration should hold the slot for as little time as it can.

Declare `actions.logout` on both. Core uses logout presence to drive
single-session behaviour:

```yaml
auth:
  strategy: form
  # ... strategy fields ...
  cookie_name: "session"

actions:
  logout:
    type: http
    method: GET
    endpoint: "/logout.asp"
    requires_session: false
```

When `actions.logout` is configured, logout fires in two places:

- **After each successful poll** — frees the session so the user can
  access the modem's web UI between polls.
- **Before a same-poll auth retry** — when `LOAD_AUTH` or
  `LOAD_INTEGRITY` fires, Core calls logout (best-effort) before
  clearing the stale session and retrying in the same poll. This
  recovers from a crash or unclean restart where the session was
  never released. Whether the call proceeds depends on `requires_session`
  (see below).

The integration cannot clear another client's session — it holds no
credential for one. On firmware that refuses the newcomer, a
third-party session the pre-retry logout cannot free means our login
is refused. Firmware that reports this as 5xx is classified
`AUTH_UNAVAILABLE` and reports `unreachable`, leaving polling alive so
recovery happens on its own when the other session ends (explicit
logout or modem-side timeout).
That classification is driven by the login's status code, not by
logout presence, so it applies to any firmware that answers this way.
See UC-87a.

See RUNTIME_POLLING_SPEC.md for the full session lifecycle.

Evidence for the refusing family: a second login attempt fails while an
existing session is active.

### SPA-style modems

Some modems have JavaScript SPA web interfaces where every request
uses `XMLHttpRequest`. Declare
session-wide headers to apply to all requests:

```yaml
session:
  headers:
    X-Requested-With: "XMLHttpRequest"
```

Dynamic headers like CSRF tokens are managed by the auth strategy,
not declared in `session.headers`. Each strategy defines its own
fields for token acquisition and header injection. See
[form_pbkdf2](#form_pbkdf2) for an example.

Action-level `headers` override session-level headers for a specific
action if needed.

---

## Actions

Optional section declaring modem-side actions. Two actions are
supported: `restart` (user-triggered) and `logout` (system-triggered).

The integration's purpose is read-only monitoring. Logout is session
cleanup — the auth manager ends its session so the user can access
the modem's web UI between polls. Restart is the sole state-changing
action, explicitly declared per-modem. No other modem commands are
supported — this is a security and terms-of-service boundary.

Both actions share the same schema with two type discriminators:
`http` for standard HTTP requests and `hnap` for HNAP SOAP-over-JSON.

**Logout call sites:** Core invokes `actions.logout` in two places:
after a successful poll (session always valid), and before a same-poll
auth retry (`attempt_logout_before_retry`). The retry call is guarded
by `requires_session` — if `true` and the session is not valid, the
call is skipped. See [Single-session modems](#single-session-modems)
for the full call-site semantics.

### Action schema — `type: http`

Standard HTTP request. Covers form-encoded POST, plain GET, and
empty-body POST patterns.

```yaml
actions:
  restart:
    type: http
    method: POST
    endpoint: "/goform/MotoSecurity"
    params:
      UserId: ""
      MotoSecurityAction: "1"
```

| Field | Type | Required | Description |
|-------|------| :--------: |-------------|
| `type` | enum | yes | `http`, `hnap`, or `cbn` |
| `method` | string | yes | HTTP method (`GET`, `POST`, etc.). No default — must be explicit. |
| `endpoint` | string | yes | URL path to send the request to. May contain `{auth:token}` / `{auth:user_id}` placeholders — see [Auth-value placeholders](#auth-value-placeholders). |
| `requires_session` | bool | `false` | *Logout only.* `false` = endpoint is unauthenticated and can clear any active server-side session without credentials. `true` = endpoint needs a live session; Core skips the pre-retry logout call when the session is not valid. |
| `params` | map | no | Form parameters. If present, body is `application/x-www-form-urlencoded`. Mutually exclusive with `json_body`. |
| `json_body` | map | no | JSON request body. If present, body is `application/json`. Mutually exclusive with `params`. Use for REST APIs that accept JSON. |
| `headers` | map | no | Per-action headers. Merged with session-level `headers` (action wins on conflict). |
| `pre_fetch_url` | string | no | URL to fetch before the action (establish session state or extract dynamic endpoint) |
| `endpoint_pattern` | string | no | Keyword to match within form action attributes on the pre-fetch page. Core wraps this in a form-action regex — not a raw regex. See Architecture Decision below. |
| `action_auth` | AuthConfig | no | Per-action auth config. When present, a fresh session is created, authenticated with the given strategy, the action is executed on that temporary session, and the session is discarded. The collector's session is untouched. Any auth strategy is valid. |

### Action schema — `type: hnap`

HNAP SOAP-over-JSON request. Endpoint (`/HNAP1/`), method (POST),
and content type (`application/json`) are fixed by protocol.

```yaml
actions:
  restart:
    type: hnap
    action_name: "SetArrisConfigurationInfo"
    pre_fetch_action: "GetArrisConfigurationInfo"
    params:
      Action: "reboot"
    response_key: "SetArrisConfigurationInfoResponse"
    result_key: "SetArrisConfigurationInfoResult"
    success_value: "OK"
```

| Field | Type | Required | Description |
|-------|------| :--------: |-------------|
| `type` | enum | yes | `http`, `hnap`, or `cbn` |
| `action_name` | string | yes | HNAP SOAP action to invoke |
| `pre_fetch_action` | string | no | Action to call first (extract current config for template vars) |
| `params` | map | no | SOAP parameters. Values with `${key:default}` are replaced with pre-fetch values. |
| `response_key` | string | no | Key in the SOAP response to check |
| `result_key` | string | no | Key within the response to match |
| `success_value` | string | no | Expected value indicating success |

### Action schema — `type: cbn`

XML POST parameterized request. Used by the `cbn` transport.
Actions are POST requests to the `setter_endpoint` (from auth config)
with a `fun=N` parameter identifying the action. The rotating session
token is included as the first POST body parameter.

```yaml
actions:
  restart:
    type: cbn
    fun: 8
  logout:
    type: cbn
    fun: 16
```

| Field | Type | Required | Description |
|-------|------| :--------: |-------------|
| `type` | enum | yes | `cbn` |
| `fun` | int | yes | `fun` parameter value for the setter POST (e.g., `8` for reboot, `16` for logout) |

The executor reads the current `sessionToken` cookie and POSTs
`token=<sessionToken>&fun=<value>` to `setter_endpoint`. Connection
errors during restart are treated as success (the modem is rebooting).

### Logout examples

**Simple GET logout:**

```yaml
actions:
  logout:
    type: http
    method: GET
    endpoint: "/logout.asp"
```

**Form POST with dynamic endpoint:**

```yaml
actions:
  logout:
    type: http
    method: POST
    endpoint: "/goform/logout"
    pre_fetch_url: "/Logout.htm"
    endpoint_pattern: "logout"
    params:
      nowTime: ""
```

**Empty-body POST with CSRF:**

The CSRF token is managed by the `form_pbkdf2` auth strategy
(`csrf_init_endpoint` + `csrf_header` in auth config). The action
config doesn't need CSRF awareness.

### Architecture Decision: endpoint_pattern

`endpoint_pattern` is a **keyword/substring**, not a regex. Core
provides form-action extraction as a built-in strategy — it wraps the
keyword in a `<form ... action="...keyword...">` regex internally.

**Why:** modem.yaml selects from core-provided behaviours and supplies
parameters. This follows the same pattern as auth strategies — the
modem declares *what* to find, core handles *how*. Contributors supply
a simple keyword, not a fragile regex.

**Extraction logic (in `orchestration/actions/http_action.py`):**

1. Fetch `pre_fetch_url`
2. Search for `<form ... action="...keyword...">` (case-insensitive)
3. If found → use the full action attribute value as the endpoint
4. If not found and `endpoint` is set → fallback with ERROR log
5. If not found and no `endpoint` → fail the action

**Logging on extraction failure:**

- ERROR with the keyword, fallback endpoint (if any), and a 500-char
  page content preview for diagnostics.

**Cookie-value params:** Some modems use the Double Submit Cookie CSRF
pattern — the POST body must echo a cookie value set by the server.
Use `{cookie:name}` placeholders in `params` values:

```yaml
params:
  csrfp_token: "{cookie:csrfp_token}"
```

Core resolves `{cookie:name}` from the session jar at action time.
Use `pre_fetch_url` to ensure the server has issued the cookie before
the action fires. If the named cookie is absent from the jar when the
action executes, the placeholder is left unresolved and the param value
is sent as the literal string `{cookie:name}` — callers should ensure
the cookie exists via `pre_fetch_url`.

### Auth-value placeholders

Some REST firmwares address the session itself in the URL. The
Sagemcom F3896LG ends one with
`DELETE /rest/v1/user/{userId}/token/{token}`, where both values come
from the login response body and neither is a cookie. Use `{auth:…}`
placeholders in an action `endpoint`:

```yaml
auth:
  strategy: bearer
  login_endpoint: "/rest/v1/user/login"
  token_path: "created.token"
  user_id_path: "created.userId"

actions:
  logout:
    type: http
    method: DELETE
    endpoint: "/rest/v1/user/{auth:user_id}/token/{auth:token}"
    requires_session: true
```

Two keys are defined, both resolved from `AuthContext` at action time:

| Placeholder | Source |
|-------------|--------|
| `{auth:token}` | `AuthContext.token` — the session token the auth strategy obtained |
| `{auth:user_id}` | `AuthContext.user_id` — the account identifier, when the strategy captures one |

An unresolved placeholder (empty `AuthContext` field, or no
authenticated session) leaves the literal `{auth:key}` text in the
endpoint, matching how `{cookie:name}` behaves in `params`. For a
logout that means the request goes to a nonsense path and fails
harmlessly; `requires_session: true` is what prevents the call being
attempted with no session at all.

**Scope:** placeholders resolve in `endpoint` only, not in `params`,
`json_body`, or `headers`. No modem has needed them there. `params`
has its own `{cookie:name}` resolution, which is unrelated and stays
as it is.

### Architecture Decision: a fixed key set, not a template language

`{auth:…}` accepts two known keys, not arbitrary expressions or paths
into the login response. Which values Core captures is decided in the
auth strategy (`token_path`, `user_id_path`); modem.yaml only names an
already-captured value. This is the same division as
`endpoint_pattern`: the config declares *what* to use, Core owns *how*
it is obtained.

**Why not `{response:created.userId}`?** That pushes JSON-path
evaluation into endpoint strings, makes every login response
permanently addressable, and turns a two-key substitution into a
template evaluator with its own failure modes. A third key, if one is
ever observed, is a field on `AuthContext` and a row in the table
above.

**Extensibility:** If a future modem needs non-form extraction (e.g.,
JavaScript variable), add an `extraction_mode` field to the schema and
a new core strategy. Don't build until needed.

```yaml
auth:
  strategy: form_pbkdf2
  csrf_init_endpoint: "/api/v1/session/init_page"
  csrf_header: "X-CSRF-TOKEN"
  cookie_name: "PHPSESSID"
  # ... other form_pbkdf2 fields

session:
  headers:
    X-Requested-With: "XMLHttpRequest"

actions:
  logout:
    type: http
    method: POST
    endpoint: "/api/v1/session/logout"
    requires_session: true
```

---

## Hardware

```yaml
hardware:
  docsis_version: "3.1"
  hw_version: "v6"         # optional — shown in variant dropdown; users can verify on modem sticker
  firmware: "TB01"         # optional — firmware family identifier (catalog metadata, not shown in UI)
  chipset: "Broadcom BCM3390"
  release_date: "2017"     # optional — year or YYYY-MM-DD
  end_of_life: "2024"      # optional — year or YYYY-MM-DD; omit if still current
```

| Field | Type | Required | Description |
|-------|------| :--------: |-------------|
| `docsis_version` | string | yes | DOCSIS specification version of the hardware ("3.0", "3.1", or "4.0"). Hardware capability, not provisioned mode — a 4.0 modem running on a 3.1 plant is still "4.0". Intake inference only ever assigns "3.0" or "3.1": DOCSIS 4.0 reuses 3.1's OFDM/OFDMA channel types ([Averna, DOCSIS 4.0 Overview](https://insight.averna.com/en/resources/blog/the-flavors-of-docsis-4-0) — "Like 3.1, DOCSIS 4.0 uses OFDM"), so wire data can't distinguish them; "4.0" is set by hand from hardware sources. |
| `hw_version` | string | no | Hardware version label shown in the variant dropdown (e.g., `"v6"`). Users can verify this against the sticker on the modem. |
| `firmware` | string | no | Firmware family identifier for catalog documentation (e.g., `"TB01"`). Not shown in the UI. |
| `chipset` | string | no | Modem chipset (informational) |
| `release_date` | string | no | Year or date the hardware was released (e.g., `"2017"` or `"2017-06"`). Used in the catalog timeline. |
| `end_of_life` | string | no | Year or date the hardware reached end-of-life. Omit if still current. |

---

## Timeout

```yaml
timeout: 15
```

Per-request timeout in seconds. Applies to each individual HTTP request
(page fetch, HNAP call), not to the total poll cycle. Default: 10
seconds.

Override for slow modems — some cable modems take 12-20 seconds to
render data pages, particularly those with older chipsets or HTTPS
endpoints.

---

## Health

```yaml
health:
  http_probe: false
  supports_head: false
  supports_icmp: true
```

Health check configuration. Controls how the HealthMonitor probes
this modem.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `http_probe` | bool | `true` | Whether TCP and HEAD health probes are enabled |
| `supports_head` | bool | `true` | Whether the modem handles HTTP HEAD correctly |
| `supports_icmp` | bool | `true` | Whether ICMP ping is expected to work |

When `http_probe` is `false`, the HealthMonitor skips both the TCP
and HEAD probes entirely — only ICMP runs (if supported). Use this
for fragile modems where every HTTP-stack interaction carries crash
risk (e.g., S33v2, #117).

When `supports_head` is `false`, the HealthMonitor **skips the HEAD
probe entirely** — there is no GET fallback. The TCP probe still
runs as the L4 reachability signal; HEAD is purely a latency-only
metric and skipping it on incompatible modems leaves the
`http_latency_ms` field None rather than corrupting it with bimodal
GET timing. Some modems return 405 or unexpected responses to HEAD
requests; this is a modem characteristic — set per-model in
modem.yaml.

`supports_icmp` is a network-dependent hint. Auto-detection during
setup overrides this default. User options override both. Useful for
ISP-managed modems known to block ICMP.

**Precedence:** modem.yaml provides defaults → auto-detection
overrides at setup → user options (highest priority).

Most modems omit this section — defaults are correct. Only declare it
when a modem is known to be fragile.

---

## Metadata

Metadata is required for all modems, including `unsupported` entries.
It documents what we know about a modem even before we can monitor it.

### Status

```yaml
status: confirmed
```

| Value | Meaning |
|-------|---------|
| `confirmed` | Full pipeline verified on real hardware (modem.verified.json present) |
| `awaiting_verification` | Parser written or placeholder entry exists, awaiting user data or confirmation. Default for new modems and for entries blocked on missing HAR captures. |
| `unsupported` | Modem cannot be monitored — no reachable channel-data endpoint (e.g., ISP firmware removed it, or the modem genuinely has no admin web interface). Reserved for permanent inability, not "we don't have data yet." |

The distinction matters: an `awaiting_verification` modem could become
`confirmed` once data lands. An `unsupported` modem can't — there's no
data path the integration could use, even if the user provided HAR
captures. If the channel data IS reachable but obscured by ISP firmware
quirks (hidden URL, login-gated endpoint), that's
`awaiting_verification`, not `unsupported`.

`unsupported` modems have identity and hardware fields only — no auth,
no pages, no parser. They document modems we know about but can't
support.

### Attribution

```yaml
attribution:
  contributors:
    - github: "https://github.com/kwschulz"
      contribution: "Initial HAR capture and parser"
    - github: "https://github.com/solentlabs"
      contribution: "HTTPS variant testing"
```

Credits to the community members who provided HAR captures, test
results, or parser contributions.

### Sources

```yaml
sources:
  auth_config: "#81, #124"
  chipset: "FCC filing"
  detection_hints: "HAR capture"
  release_date: "Amazon listing"
```

Provenance tracking — documents where each piece of configuration data
came from. Freeform strings referencing GitHub issues, HAR captures,
FCC filings, manufacturer documentation, or contributor reports. This
helps future maintainers understand why the config says what it says
and where to look if something seems wrong.

Common source fields: `auth_config`, `chipset`, `detection_hints`,
`release_date`. Any key is valid — use whatever describes the source.

### PII Fields

```yaml
pii_fields:
  - home_ssid
```

Declares `system_info` keys reported by this modem that contain
personally identifiable information beyond the global defaults.
Consumers (e.g., CMMT) strip the union of
`pii_fields_global.json` and any modem-specific `pii_fields`
before telemetry submission.

The global defaults
(`packages/cable_modem_monitor_catalog/scripts/data/pii_fields_global.json`)
list `mac_address` and `serial_number` as defensive defaults. CMM no
longer collects these (no parser extracts them; the intake mapping skips
them — see SYSTEM_INFO_SPEC § Tiered Sensor Model), so the list is a
safety net rather than an active strip target. Add `pii_fields` to a
modem's YAML only for fields not already in the global list. Omit the
key if the modem reports no PII beyond the global defaults.

Currently no modem in the catalog carries this key.

### ISPs and notes

```yaml
isps:
  - "Comcast"
  - "Spectrum"
notes: |
  HTTPS variant requires login. HTTP variant works without auth.
  See issue #81 for firmware differences.
references:
  issues:
    - 42
    - 81
  prs:
    - 57
```

### Gaps

Known capability gaps on an otherwise-verified entry. Status and
coverage are separate axes: `status: confirmed` certifies that the
*shipped* pipeline works on real hardware; `gaps` records what is
*not shipped yet* and exactly what evidence would close it.

A gap is a capability blocked on a **single specific obtainable
artifact** — almost always a HAR capture. This is the *build* axis:
we lack the wire evidence to implement the capability. Two things
that look like gaps are not:

- **Implemented but unverified on this variant's hardware** — that is
  the *verify* axis, which `status` and `notes` own. A reboot action
  mirrored from a sibling model and awaiting a hardware test is not a
  gap; no new artifact would build it, only confirm it.
- **A capability no artifact could ever close** — firmware that
  simply never exposes the data. That is a permanent limitation,
  documented in `notes`, not a gap (a `needs:` no capture can satisfy
  is a false promise to contributors).

If no obtainable artifact would close it, it is not a gap.

```yaml
gaps:
  - capability: system_uptime
    needs: "HAR capture that includes the status.html page"
    issue: "https://github.com/solentlabs/cable_modem_monitor/issues/92"
  - capability: reboot action
    needs: "HAR capture of the Reboot button click (advanced page)"
    issue: "https://github.com/solentlabs/cable_modem_monitor/issues/92"
```

| Field | Required | Description |
|-------|:--------:|-------------|
| `capability` | yes | The missing capability, named concretely (a canonical field, an action, a sensor) |
| `needs` | yes | The specific evidence that closes the gap — phrased so a contributor can act on it |
| `issue` | no | URL of the issue where the gap is being tracked or discussed |

The catalog index generator renders all gaps into a "Confirmed with
Gaps" table in `CATALOG_AUDIT.md`, making each row a self-contained
contribution task. The `notes` field keeps the narrative *why*; the
`gaps` list is the machine-readable roll-up. Remove the entry when
the capability lands.

---

## Validation Rules

### Transport constraints

The transport identifies the protocol. For HNAP and CBN, the
transport constrains auth, format, and action types. For HTTP, auth,
session, and format are configured independently, subject to the
[auth-session-action consistency](#auth-session-action-consistency)
rules below.

<!-- BEGIN GENERATED: yaml-constraints (from the auth, format, and action models; run packages/cable_modem_monitor_core/scripts/generate_constraint_tables.py) -->
| Transport | Valid auth strategies | Valid session | Valid formats | Valid action types |
|-----------|-----------------------|---------------|---------------|--------------------|
| `cbn` | `form_cbn` | cookie (rotating sessionToken + stable SID) | `xml` | `cbn` |
| `hnap` | `hnap` | implicit (uid cookie + HNAP_AUTH header) | `hnap` | `hnap` |
| `http` | `basic`, `bearer`, `form`, `form_nonce`, `form_pbkdf2`, `form_sjcl`, `none`, `url_token` | stateless, cookie, CSRF, or url_token | `html_fields`, `javascript`, `javascript_json`, `javascript_vars`, `json`, `json_transposed`, `table`, `table_transposed` | `http` (optional `action_auth`) |
<!-- END GENERATED: yaml-constraints -->

The format field in parser.yaml determines how the response is decoded.
See ARCHITECTURE.md Constraint Summary for the format-to-value-type
mapping.

Violations are rejected at both **build time** (Pydantic validation in
Catalog's dev-gate) and **load time** (`load_modem_config()` in Core)
with a clear error message — not at runtime with mysterious parsing
failures.

### Required fields by status

| Field | `confirmed` / `awaiting_verification` | `unsupported` |
|-------| :------------------------------------: | :-------------: |
| `manufacturer` | required | required |
| `model` | required | required |
| `transport` | required | required |
| `default_host` | required | required |
| `auth` | required | — |
| `session` | optional | — |
| `hardware` | required | optional |
| `status` | required | required |
| `attribution` | required | optional |
| `isps` | required | optional |

### Auth-session-action consistency

- `auth.strategy: none` + `auth.cookie_name` → N/A (`none` has no
  `cookie_name` field — cookie-tracking requires an auth strategy)
- `auth.strategy: hnap` + explicit `session` block → error (HNAP
  session is implicit, cannot be overridden)
- `actions.logout.requires_session: true` on a `type: cbn` action →
  schema error (`requires_session` is `HttpAction`-only; CBN logout
  always embeds the session token by protocol)

---

## Multi-Variant Modems

When a modem model has firmware variants that differ in auth, protocol,
or behavior, each variant gets its own yaml file:

```text
modems/arris/sb8200/
├── parser.yaml                 # Shared extraction config
├── parser.py                   # Shared code overrides
├── modem.yaml                  # auth: none (HTTP, default variant)
├── modem-url-token.yaml        # auth: url_token (login_ prefix, ct_ tokens)
├── modem-cookie.yaml           # auth: url_token (no prefix, cookie-only)
└── test_data/
    ├── modem.har
    ├── modem.expected.json
    ├── modem.verified.json              # Present when status: confirmed
    ├── modem-url-token.har
    ├── modem-url-token.expected.json
    ├── modem-url-token.verified.json    # Per-variant verification
    ├── modem-cookie.har
    └── modem-cookie.expected.json
```

### Rules

1. **One auth strategy per file.** No `auth.types` with multiple
   strategies — each variant file has one `auth.strategy`.
2. **Variant name in filename.** `modem-{variant}.yaml` where variant
   is a short descriptive name (`noauth`, `url-token`, `basic`).
3. **Shared parser.** All variants in a directory share `parser.yaml`
   and `parser.py`. If variants need different parsers, they belong in
   separate directories.
4. **Same transport.** All variants in a directory share a transport.
   Different transport = different directory.
5. **Per-variant metadata.** Status, attribution, ISPs, and references
   are per-variant. A variant can be `confirmed` while another is
   `awaiting_verification`.
6. **The variant filename is the persisted config key.** A config
   entry stores the variant name, and the integration re-resolves it
   at runtime as `modem-{variant}.yaml` (HA `__init__.py`). Editing a
   variant's contents — including cosmetic fields like `hw_version` —
   is safe for existing installs. Renaming, merging, or removing a
   variant file is not: it orphans every config entry bound to that
   name and requires a config-entry migration. This is why
   `hw_version` corrections happen in place rather than by reshaping
   the variant set.

### Single-variant modems

Most modems: one `modem.yaml` file. No variant suffix needed.

```text
modems/motorola/mb7621/
├── parser.yaml
├── modem.yaml
└── test_data/
    ├── modem.har
    ├── modem.expected.json
    └── modem.verified.json      # Present when status: confirmed
```

---

## Capabilities

Capabilities are **not declared in modem.yaml**. They are implicit
from parser.yaml and parser.py — the presence of a mapping IS the
capability declaration.

- Downstream channel mapping in parser.yaml → downstream capability
- `system_uptime` field in parser.yaml → system uptime capability
- `parse_downstream` override in parser.py → downstream capability

No separate capabilities list to maintain. No mapping = no entity.
See [PARSING_SPEC.md](PARSING_SPEC.md#capabilities-are-implicit) for
details.

---

## Complete Examples

### HTTP — no auth (simplest)

```yaml
manufacturer: "{Manufacturer}"
model: "{Model}"
brands:
  - "{BrandName}"
transport: http
default_host: "192.168.100.1"

auth:
  strategy: none

hardware:
  docsis_version: "3.0"

status: confirmed
sources:
  auth_config: "Community forum"
attribution:
  contributors:
    - github: "https://github.com/{contributor}"
      contribution: "Initial implementation"
isps:
  - "Various"
```

### HTTP — form auth with session

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

session:
  cookie_name: "session"

actions:
  logout:
    type: http
    method: GET
    endpoint: "/logout.asp"
    requires_session: false

hardware:
  docsis_version: "3.0"
  chipset: "Broadcom BCM3384"

status: confirmed
sources:
  auth_config: "HAR capture"
attribution:
  contributors:
    - github: "https://github.com/{contributor}"
      contribution: "Initial implementation"
isps:
  - "Various"
notes: |
  Single-session modem — must logout before new login.
```

### HNAP

```yaml
manufacturer: "{Manufacturer}"
model: "{Model}"
model_aliases:
  - "{AlternativeName}"
brands:
  - "{BrandName}"
transport: hnap
default_host: "192.168.100.1"

auth:
  strategy: hnap
  hmac_algorithm: md5

actions:
  restart:
    type: hnap
    action_name: "SetArrisConfigurationInfo"
    pre_fetch_action: "GetArrisConfigurationInfo"
    params:
      Action: "reboot"
    response_key: "SetArrisConfigurationInfoResponse"
    result_key: "SetArrisConfigurationInfoResult"
    success_value: "OK"

hardware:
  docsis_version: "3.1"
  chipset: "Broadcom BCM3390"

status: confirmed
sources:
  auth_config: "HAR capture, user testing"
attribution:
  contributors:
    - github: "https://github.com/{contributor}"
      contribution: "HAR capture and testing"
isps:
  - "{ISP}"
```

### HTTP — structured data (JSON API)

```yaml
manufacturer: "{Manufacturer}"
model: "{Model}"
model_aliases:
  - "{AlternativeName}"
transport: http
default_host: "192.168.100.1"

auth:
  strategy: none

hardware:
  docsis_version: "3.1"

status: confirmed
sources:
  auth_config: "HAR capture"
attribution:
  contributors:
    - github: "https://github.com/{contributor}"
      contribution: "REST API analysis"
isps:
  - "{ISP}"
```

### Unsupported (placeholder)

```yaml
manufacturer: "{Manufacturer}"
model: "{Model}"
transport: http
default_host: "10.0.0.1"

hardware:
  docsis_version: "3.1"

status: unsupported
notes: |
  Awaiting HAR capture from user.
references:
  issues:
    - 123
```
