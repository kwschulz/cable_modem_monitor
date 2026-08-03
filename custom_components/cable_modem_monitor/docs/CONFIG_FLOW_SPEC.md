# Config Flow Specification

The config flow is the user-facing setup wizard in Home Assistant. It guides
the user from "I want to monitor my modem" to a working integration entry
with authenticated polling.

**Design principles:**

- Progressive disclosure — each step narrows the next
- Dropdown-first — manufacturer then model, no search index needed at ~25 modems
- Validate once — a single connectivity + auth + parse check at the end

---

## Steps

```text
┌─────────────────────────────────────────────────┐
│ Step 1a: Select manufacturer                    │
│   [Technicolor           ▼]                     │
│                                                 │
│   Options: "All", plus one per manufacturer     │
│   in the catalog. "All" shows every modem.      │
│                                                 │
│   Source: Core's list_modems(catalog_path)   │
│   returns all modem summaries. HA filters       │
│   client-side by manufacturer.                  │
└─────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────┐
│ Step 1b: Select model                           │
│   [CGM4981COM (XB7)  DOCSIS 3.1           ▼]   │
│                                                 │
│   Filtered to models from the selected          │
│   manufacturer, or all models if "All".         │
│   Display: {manufacturer} {model}  DOCSIS X.Y   │
└─────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────┐
│ Step 2: Variant  (skipped if single-variant)    │
│   [URL Token (Comcast firmware) ▼]              │
│                                                 │
│   Source: modem-{variant}.yaml files in the     │
│   selected model directory.                     │
│   Shows: variant description, ISPs, auth type.  │
└─────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────┐
│ Step 3: Connection                              │
│   Host: [192.168.100.1]                         │
│   Username: [admin]          (if auth requires) │
│   Password: [••••••••]       (if auth requires) │
│                                                 │
│   Host default from modem.yaml default_host.    │
│   Credential fields shown/hidden based on       │
│   variant's auth strategy.                      │
└─────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────┐
│ Step 4: Validate  (progress spinner)            │
│   "Connecting to modem..."                      │
│                                                 │
│   1. Test connectivity (HTTP/HTTPS)             │
│   2. Authenticate with variant's auth config    │
│   3. Fetch + parse one page to confirm          │
│   4. Detect health probe support (ICMP, HEAD)   │
│                                                 │
│   On success → create entry                     │
│   On failure → show error, return to step 3     │
└─────────────────────────────────────────────────┘
```

---

## Step Details

### Step 1: Find Your Modem

Two sub-steps that narrow the selection progressively:

**Step 1a — Select manufacturer.** A dropdown lists every name a user
might identify the maker by: the union of `manufacturer:` values and
`brands:` entries across the catalog, plus an "All" option that shows
every modem. A modem branded differently from its recorded manufacturer
(e.g., the CommScope-made G54 sold under the Arris brand) appears under
both names while remaining one catalog record. At ~25 modems the full
list is still usable, so "All" is a practical default for users who
aren't sure of the manufacturer.

**Step 1b — Select model.** A second dropdown filtered to models from the
selected manufacturer (or all models if "All" was chosen). This step
also includes the entity prefix selector (`none`, `model`, `ip`) —
see ENTITY_MODEL_SPEC § Entity Prefix for naming behavior.

**Source:** Core's `list_modems(catalog_path)` returns all modem
summaries. The HA config flow filters client-side by manufacturer
selection. No search index is needed at the current catalog size (~25
modems).

**Display format:** `{manufacturer} {model}` with DOCSIS version and
verification status. Alternate user-facing names — `model_aliases` ∪
`brands` — shown in parentheses. Labels are bucket-contextual: when the
user filtered by a brand bucket, the line leads with that brand and the
parenthetical lists aliases and other brands (`Xfinity XB6
(CGM4140COM)` under Xfinity), adding the manufacturer-composed name
only when no alias anchors the entry (`Arris G54 (CommScope G54)`
under Arris; `CommScope G54 (Arris)` under CommScope or All). The lead
always matches the filter the user chose.

```text
Arris SB8200                    DOCSIS 3.1
Arris SB8200 (Surfboard)        DOCSIS 3.1
Technicolor CGM4981COM (XB7)    DOCSIS 3.1
Netgear CM1200                  DOCSIS 3.1  *
```

**Verification rollup:** The `*` on a model reflects the aggregate status across
all variant files in the model's primary directory and any sibling directories
(see § Known Gap). If at least one variant is `confirmed`, the model shows no
`*`. If every variant is unconfirmed, the model shows `*`. This means a user
whose variant is confirmed will not see `*` on their model just because another
variant is newly added and awaiting verification.

**Help link:** Step description includes a link to the auto-generated
modem index in the GitHub repo (e.g., `docs/SUPPORTED_MODEMS.md`). This
file is regenerated by CI on merge to main — if the directory structure
changed, CI commits the updated index; if not, no commit. Always in sync,
no contributor setup required.

**Physical vs logical:** The catalog is organized by manufacturer on disk
(`modems/{mfr}/{model}/`). The dropdown presents a logical view — users
see brands and aliases without needing to know the directory structure.

### Step 2: Variant (conditional)

**Shown when:** The selected model entry has more than one variant. Variants
come from two sources: multiple `modem-{variant}.yaml` files within the primary
directory, and sibling directories that share the same `(manufacturer, model)`
identity under a different transport. All are flattened into a single list.

**Skipped when:** Exactly one variant exists across the primary directory and
any siblings. Flow proceeds directly to Step 3 with that config.

**Source:** `list_variants(modem_dir, sibling_dirs)` in the catalog manager.
Scans all directories, returns a combined flat list sorted with default variants
first.

**Display format:** Auth strategy label with hardware version qualifier when set.
Hardware version is shown in parentheses — users can verify it against the sticker
on the modem. Unconfirmed variants are marked with `*`.

```text
URL Token
URL Token (v7)
URL Token (v7) *
HNAP (v6)
Form Login CBN *
```

The variant label comes from fields in each variant's YAML:

- `auth.strategy` → mapped to user-facing label via `AUTH_STRATEGY_LABELS`
- `hardware.hw_version` → shown in parentheses when present
- variant name (from filename stem) → shown in parentheses when present
- `status` → `*` appended when `status != "confirmed"`

**Composite variant keys:** To prevent collisions when multiple directories each
contribute a default variant (`name=None`), the dropdown value is
`"{rel_dir}/{name|__default__}"`. The selected directory is stored as
`_selected_modem_dir` and used for all downstream steps.

### Step 3: Connection

**Fields shown depend on the variant's auth strategy:**

| Auth strategy | Fields shown |
|---------------|-------------|
| `none` | Host only |
| `basic` | Host, Username, Password |
| `bearer` | Host, Username, Password |
| `form` | Host, Username, Password |
| `form_cbn` | Host, Username, Password |
| `form_nonce` | Host, Username, Password |
| `form_pbkdf2` | Host, Username, Password |
| `form_sjcl` | Host, Username, Password |
| `url_token` | Host, Username, Password |
| `hnap` | Host, Username, Password |

**Exception — `none` with per-action auth:** when the top-level strategy
is `none` but the restart action has `action_auth` set, credential fields
are still shown so the user can supply credentials for the restart action.

The credential-visibility check loads the selected modem's variant YAML
synchronously at form-build time. The `restart_requires_credentials(modem_dir, variant)`
helper in `config_flow_helpers.py` performs this check.

**Host default:** From `default_host` in the selected modem/variant YAML
(typically `192.168.100.1`).

**Host input accepts IP, hostname, or full URL.** Address-bar pastes
are tolerated: any path, query, or fragment is discarded, with or
without a scheme (`https://192.168.100.1/cgi-bin/luci/` → host
`192.168.100.1`, protocol `https`). Only the protocol and host[:port]
are ever kept —
data-page paths come from the catalog, never from the user. If the
user enters a bare IP (e.g., `192.168.100.1`), protocol detection
probes both :80 and :443 automatically in Step 4. If the user includes
a protocol prefix (e.g., `https://192.168.100.1`), only that port is
probed — no fallback to the other transport. This gives users an
escape hatch when automatic detection picks the wrong protocol.

**Entity prefix is selected in Step 1.** Options: `none`, `model`, `ip`.
`none` is only available if no other instance is already using it.
See ENTITY_MODEL_SPEC.md Entity Prefix section for naming behavior.

### Step 4: Validate

Runs in an executor thread to avoid blocking the HA event loop.

**Validation pipeline:**

1. **Protocol detection** — if the user entered a bare IP/hostname
   (no `http://` or `https://` prefix), TCP-probe ports 80 and 443.
   When 443 accepts a connection *and* completes a TLS handshake,
   prefer HTTPS — modems that expose both ports almost always intend
   HTTPS for authenticated traffic. Standard Python SSL is tried
   first; `SECLEVEL=0` is the fallback only when standard fails.
   `legacy_ssl=True` when standard SSL fails but `SECLEVEL=0`
   succeeds, or when the negotiated TLS version is TLS 1.1 or older
   — both require `LegacySSLAdapter` at runtime. When 443 is closed
   or both handshakes fail, fall back to HTTP if port 80 is open.
   The probe never sends an HTTP request — only TCP connects plus,
   on 443, TLS handshakes. Records `working_url`, `protocol`, and
   `legacy_ssl` for runtime. If the user explicitly included a
   protocol prefix, respect it and probe only that transport.
2. **Authenticate + Parse** — load modem config from the catalog, create
   a `ModemDataCollector`, and run `execute()`. This single call
   authenticates with the variant's auth strategy, fetches data pages,
   and runs the parser. Confirm non-empty output. Authentication
   runs exactly once: a structured rejection (UC-86) is surfaced
   directly to the user; protocol-retry loops are explicitly avoided
   because they collide with single-session firmware and obscure the
   real failure. The collector is closed in a `finally` for the same
   reason — `execute()` only reaches its own logout phase on the success
   path, so a failed attempt would otherwise strand the server-side
   session and collide with the user's next try.
3. **Health probes** — test ICMP ping and HTTP HEAD support for the
   health monitoring pipeline.

**Implementation — Core API call sequence:**

```text
config_flow_helpers.validate_connection(hass, host, user, pass, modem_dir, variant)
 │
 ├─ parse_host_input(host)                    [lib/host_validation]
 │   → (hostname, user_protocol | None)
 │
 └─ _run_validation(...)                      [executor thread]
      │
      ├─ 1. detect_protocol(hostname)         [Core connectivity]
      │     TCP probe :80 and :443; TLS handshake on :443 —
      │     standard Python SSL first, SECLEVEL=0 fallback;
      │     legacy_ssl=True when standard fails or version ≤ TLS 1.1
      │     → ConnectivityResult(protocol, legacy_ssl, working_url)
      │
      ├─ 2. load_modem_config(modem.yaml)     [Core config_loader]
      │     load_parser_config(parser.yaml)
      │     load_post_processor(parser.py)
      │
      ├─ 3. ModemDataCollector(legacy_ssl=...)  [Core orchestration]
      │     .execute()
      │     Auth → Fetch pages → Parse → Logout (success path only)
      │     → ModemResult(success, modem_data, signal, error)
      │     Single attempt — no retry chain (UC-86)
      │     .close() in finally — releases the session on every outcome
      │
      ├─ 4. test_icmp(hostname)               [Core connectivity]
      │     → bool (supports ICMP ping)
      │
      ├─ 5. test_http_head(url, legacy_ssl)   [Core connectivity]
      │     → bool (supports HTTP HEAD)
      │
      └─ 6. Return results for config entry
            {protocol, legacy_ssl, supports_icmp, supports_head}
```

**Error classification from `ModemResult.signal`:**

| CollectorSignal | strings.json key | User message |
|----------------|-------------------|-------------|
| `CONNECTIVITY` | `cannot_connect` | Modem not responding |
| `AUTH_FAILED` | `invalid_auth` | Login failed |
| `AUTH_UNAVAILABLE` | `modem_busy` | Modem declined the login (5xx); not a credential problem (UC-87a) |
| `AUTH_LOCKOUT` | `invalid_auth` | Login failed |
| `LOAD_ERROR` | `cannot_connect` | Modem not responding |
| `LOAD_AUTH` | strategy-dependent, see below | |
| `LOAD_INTEGRITY` | strategy-dependent, see below | |
| `PARSE_ERROR` | `parse_failed` | Connected but couldn't read data |

Every signal except `OK` is mapped. An unmapped signal falls through to
`unknown` ("Unexpected error, check the logs"), which tells the user nothing;
`test_every_signal_is_mapped` fails when a new `CollectorSignal` lands without
a key.

`LOAD_AUTH` and `LOAD_INTEGRITY` are read through the auth strategy, because
a 401 after a "successful" login only means the session was refused if the
strategy actually verified the login:

| `auth_failure_mode()` | strings.json key | User message |
|-----------------------|------------------|--------------|
| `CREDENTIALS_SUSPECT` (default) | `invalid_auth` | Login failed, check credentials |
| `SESSION_REJECTED` | `session_rejected` | Logged in, but the modem refused the data request |
| `NOT_CONFIGURED` | `invalid_auth` | Login failed, check credentials |

`basic` never validates a credential and plain `form` accepts any response
under HTTP 400, so for those a post-login 401 is most often the bad password
surfacing late. Authority and the proven-not-assumed rule:
ARCHITECTURE_DECISIONS.md § Post-login 401 is read per auth strategy.

`auth_signals` is config-flow-local: it selects `PermissionError` over
`RuntimeError`, and both carry the same encoded error key, so membership
changes nothing a user sees. HA's reauth prompt is started on the runtime path
instead (`_start_reauth_on_lockout` — `ConnectionStatus.AUTH_FAILED` plus an
open circuit breaker).

Reauth matters most here. An already configured modem that starts refusing
data trips the auth streak, opens the breaker, and prompts for
reauthentication, so a user with correct credentials arrives at
`reauth_confirm`. Both that handler and `_ValidationProgress.collect` resolve
the displayed key through `_validation_error_key`, rather than assuming an
auth-shaped exception means bad credentials.

**Protocol detection failure** (before `ModemDataCollector` runs):

| Condition | strings.json key | User message |
|-----------|-------------------|-------------|
| No HTTP/HTTPS response | `network_unreachable` | Can't reach modem |

**On success:** Create the config entry and proceed to dashboard.
Protocol, legacy_ssl, supports_icmp, and supports_head are persisted
once and reused at every runtime poll — no re-discovery.

**On failure:** Classify the error and return to Step 3 with a message:

| Error | Message | Guidance |
|-------|---------|----------|
| Network unreachable | Can't reach modem | Check IP, check you're on the modem's network |
| Modem not responding | Connection refused | Check IP, try HTTP vs HTTPS |
| Auth failed | Login failed | Check username/password, try logging in via browser |
| Parse failed | Connected but couldn't read data | Modem may need different variant selection |

---

## Config Entry

After successful validation, these fields are stored in the HA config
entry. The config entry is thin — it stores user selections and derived
connection info. Everything else (auth config, page URLs, parser class,
session config) is loaded from the catalog package at startup using the
`manufacturer/model/variant` tuple.

```yaml
# User selections
manufacturer: "arris"              # Step 1 (canonical, from catalog)
model: "sb8200"                    # Step 1 (canonical, from catalog)
user_selected_modem: "Arris SB8200 (Surfboard)"  # Step 1 (display name user selected)
entity_prefix: "model"             # Step 1 (none|model|ip)
variant: "url-token"               # Step 2 (null if single-variant)

# Connection
host: "192.168.100.1"              # Step 3
username: "admin"                  # Step 3 (null if no-auth)
password: "encrypted"              # Step 3 (null if no-auth)

# Derived during validation (Step 4)
protocol: "https"                  # Detected or user-specified
legacy_ssl: false                  # True if legacy ciphers required
supports_icmp: true
supports_head: true

# Polling configuration (options flow)
scan_interval: 600                 # seconds (10 min default), or 0 for disabled
health_check_interval: 30          # seconds (30s default), or 0 for disabled
```

**Thin entry, runtime lookup.** The parser code is in the catalog package
and can't be stored in the config entry — the package is already a hard
runtime dependency. So auth config, page URLs, session config, and parser
class are all loaded from the package at startup. This means modem config
fixes (corrected page URLs, updated auth params) apply automatically on
restart without reconfiguration.

**Variant resolution at startup:** The loader resolves the
`manufacturer/model/variant` tuple to a config file:

- `variant: "url-token"` → loads `modem-url-token.yaml`
- `variant: null` → loads `modem.yaml`

**`modem.yaml` is the default variant.** When a single-variant modem is
later split into multiple variants, the variant matching the original
behavior stays as `modem.yaml`. New variants get `modem-{name}.yaml`
suffixes. Existing config entries with `variant: null` continue to load
`modem.yaml` — no migration needed. The constraint: when splitting, never
delete or rename `modem.yaml`. Rename the new behavior, not the existing
one.

---

## Reconfiguration (Options Flow)

Users can reconfigure via Settings → Integrations → Cable Modem Monitor →
Configure.

**What can change:**

- Host, username, password (without re-selecting modem)
- Data poll interval (30s–86400s, or disabled for manual-only)
- Health check interval (10s–86400s, or disabled)
- Full reconfiguration (restart from Step 1)

**What cannot change without reconfiguration:**

- Manufacturer, model, variant — these determine the parser and auth
  config. Changing them means starting over.

**Polling modes.** Data and health intervals are independently
configurable, including disabled. See
[HA_ADAPTER_SPEC.md](HA_ADAPTER_SPEC.md#polling-modes) for the
complete matrix and interval limits.

---

## Reauthentication Flow

When the modem rejects credentials 6 times consecutively, the
integration triggers HA's native reauth flow. The user re-enters
credentials via `async_step_reauth` (reusing the Step 3 connection
form). Validation runs identically to Step 4.

See [HA_ADAPTER_SPEC.md](HA_ADAPTER_SPEC.md#reauth-flow) for the
full orchestrator state machine and circuit breaker behavior.

---

## Performance Characteristics

| Step | I/O | Scale factor | Typical cost |
|------|-----|-------------|-------------|
| Find modem | Read identity fields from all modem*.yaml | O(total modems) | ~25 files, <100ms |
| Variant | Read modem-{variant}.yaml in one model dir | O(variants per model) | 2-3 files, <5ms |
| Connection | None (form only) | O(1) | 0ms |
| Validate | HTTP requests to modem | O(pages) | 1-5s (network bound) |

All YAML reads run in an executor thread. No event loop blocking.

The "Find modem" step reads all modem.yaml files to search identity fields.
At ~25 modems this is negligible. If the catalog grows significantly, Core
can generate a build-time index during catalog package build — no changes
to the config flow API needed.
