# `form_cbn` --- CBN AES-256-CBC Encrypted Auth

## Overview

CBN (Compal Broadband Networks) AES-256-CBC encrypted form auth. Compal modem firmwares use the CryptoJS library (v3.1.2) to encrypt the password client-side. The AES key and IV are derived from a rotating session token cookie --- each HTTP response rotates the token via `Set-Cookie`. Login is a form-encoded POST to a `setter.xml` endpoint with `fun=N` parameters. Requires the `cryptography` package (`pip install solentlabs-cable-modem-monitor-core[cbn]`).

## Crypto Library

This section documents what CryptoJS v3.1.2 standardizes. It is the
implementation authority --- if code deviates from these encoding rules,
code is wrong.

CryptoJS v3.1.2 with AES-256-CBC. The key and IV are derived from the session token, not from a password-based KDF:

- **Key**: `SHA-256(sessionToken.utf8)` --- 32 bytes (AES-256 key)
- **IV**: `MD5(sessionToken.utf8)` --- 16 bytes (CBC block size). MD5 is required for protocol fidelity despite being cryptographically weak.
- **Padding**: PKCS7 (128-bit blocks)
- **Encryption format**: The ciphertext goes through a multi-step encoding:
  1. AES-256-CBC encrypt (with PKCS7 padding)
  2. Hex-encode the ciphertext
  3. Prepend `":"` character
  4. Base64-encode the result: `base64(":" + hex(ciphertext))`
- **Session token rotation**: Every HTTP response includes a new `Set-Cookie: sessionToken=<new_value>`. The next request must use the new token for both authentication and key derivation. The `requests.Session` cookie jar handles this automatically.

These encoding rules are derived from `encrypt_cryptoJS.js` in the modem firmware.

## Auth Flow

```text
1. GET login page (login_page, default: "/common_page/login.html")
   Receive initial sessionToken cookie
   Required: cookie must be present in response
   Error if missing: "Login page did not set 'sessionToken' cookie"

2. Derive AES key and IV from sessionToken:
   key = SHA256(sessionToken.utf8)  -> 32 bytes
   iv  = MD5(sessionToken.utf8)     -> 16 bytes

3. Encrypt password:
   padded    = PKCS7(password.utf8, block_size=128)
   encrypted = AES-256-CBC(key, iv, padded)
   encoded   = base64(":" + hex(encrypted))

4. POST login to setter_endpoint (default: "/xml/setter.xml"):
   Content-Type: application/x-www-form-urlencoded
   Body: token=<sessionToken>&fun=<login_fun>&Username=<username_value>&Password=<encoded>
   CRITICAL: token parameter MUST be first (firmware rejects other orderings)

   Success criteria:
   - HTTP status == 200 (exactly)
   - Response body contains "successful" (case-insensitive)
   - Response body matches regex SID=(\d+) to extract SID value

   A body without "successful" is not automatically a wrong password.
   See Login Token Vocabulary below.

5. Extract SID from response body:
   Regex: SID=(\d+)
   Error if not found after successful login

6. Set SID cookie on session:
   Cookie name from sid_cookie_name (default: "SID")
   Domain MUST match modem hostname (required for IP-based access)
   Both sessionToken (rotating) and SID (stable) are needed for
   subsequent authenticated requests
```

## Login Token Vocabulary

A login body without `"successful"` carries one of four other firmware
outcomes. Core read every one of them as a wrong password, which is
`AUTH_FAILED`: the circuit breaker trips on the first occurrence, so a
single lockout or restart response stopped polling and told the user to
reconfigure a credential the modem never judged.

| Body token | Firmware does | Core reports | Effect |
|---|---|---|---|
| `"successful"` | `index.html` | success | session established |
| `lockedout` | `Access-denied.html` | raises `LoginLockoutError` | `AUTH_LOCKOUT`, breaker trips |
| `cbnAccessDenied` | `Access-denied.html` | raises `LoginLockoutError` | `AUTH_LOCKOUT`, breaker trips |
| `cbnLogin` | `login.html` | `AuthResult(busy=True)` | `AUTH_UNAVAILABLE`, no streak, no breaker |
| `cbnFirstInstall` | `login.html` | `AuthResult(busy=True)` | `AUTH_UNAVAILABLE`, no streak, no breaker |
| `cbnBlockContent` | `Blocked-content.html` | rejected, token named | `AUTH_FAILED`, breaker trips |
| anything else | `ShowPasswordError()` | rejected | `AUTH_FAILED`, breaker trips |

### Restart the login

`cbnLogin` and `cbnFirstInstall` are the firmware asking the client to
start the login over. `common_api.js` handles both by navigating to
`login.html` with no alert --- the branch that neither blames the
credential nor reports a lock. The manager returns
`AuthResult(success=False, busy=True)`, which the collector classifies
`AUTH_UNAVAILABLE`: polling continues at normal cadence and the
condition clears on its own (UC-87a). Same treatment a 5xx login gets,
reached from a protocol token rather than a status code.

`busy` is set in the strategy, not declared per entry: the tokens come
from firmware shared across the CBN platform, not from entry config, so
a catalog entry has nothing to declare. `hnap` sets it the same way for
`RELOAD` (AUTH_HNAP_SPEC.md § Restart the login); `form_pbkdf2` differs,
its busy body being entry data (`login_busy`).

### Lockout

`lockedout` and `cbnAccessDenied` are firmware anti-brute-force, sent to
`Access-denied.html`. `form_cbn` raises `LoginLockoutError` rather than
returning a failed result, so the orchestrator can tell a self-protecting
modem from a rejected credential (#117): both stop polling, but the
breaker records the trip reason and the blocked polls that follow report
the lockout rather than advising a reconfigure.

### Matching and ordering

Token tests are substring matches. The firmware uses `response.match()`
for every token except `lockedout`, which `login.html` compares with
`==`; substring is used uniformly here because it is a superset and each
token is distinctive enough that no other body carries one.

Groups are checked by consequence --- lockout, then restart, then blocked
--- rather than in firmware source order, because the two captured
handlers disagree on order and the tokens are mutually exclusive in every
observed response.

The catalog gate `test_cbn_login_token_coverage.py` reads these tokens
back out of each entry's captured firmware JS and fails when one is
unhandled, so a new firmware line that adds a token fails a test rather
than a user's integration.

## Transport Failures

A connection error or timeout during either request is re-raised, not
converted to a failed login: the modem never answered, so it judged no
credential. The collector classifies it `CONNECTIVITY` (UC-30/UC-31),
which backs off and recovers unattended. Reporting one as a failed login
is `AUTH_FAILED`, which trips the circuit breaker on its first
occurrence and stops polling until the user reconfigures (#200).

The same rule governs the data path: `loaders/cbn.py` surfaces every
transport failure rather than omitting the resource, because an omitted
resource reaches the parse layer as a stub page and counts toward the
auth streak. Only a body that will not decode is skipped. See
RESOURCE_LOADING_SPEC.md § Error Signals.

## Firmware Assumptions

What's hardcoded in `auth/form_cbn.py` and `protocol/cbn.py` that is specific to Compal firmware, not inherent to CryptoJS:

| Assumption | Value | Source | Risk if variant differs |
|---|---|---|---|
| Encryption format | `base64(":" + hex(ciphertext))` | `encrypt_cryptoJS.js` | The colon prefix and encoding chain are firmware-specific |
| POST encoding | `application/x-www-form-urlencoded` (not JSON) | HAR request headers | Other CryptoJS modems may use JSON |
| Parameter order | `token` must be first parameter | Empirical testing | Not documented in firmware --- observed behavior |
| XML setter/getter pattern | `setter.xml` with `fun=N` dispatch | Compal firmware API design | Entirely Compal-specific |
| Success string | `"successful"` (case-insensitive) in response body | HAR response analysis | Other firmware may signal success differently |
| Lockout tokens | `lockedout`, `cbnAccessDenied` | `login.html` inline handler, `common_api.js` (`NoticeLogin`) | Variant firmware could emit other lockout tokens --- would fall into the wrong-password branch |
| Restart tokens | `cbnLogin`, `cbnFirstInstall` | `common_api.js` (`ajaxSetToDB`, `ajaxSetNoTokenUpdate`, `NoticeLogin`) | Variant firmware could emit other restart tokens --- would fall into the wrong-password branch |
| Blocked token | `cbnBlockContent` | `login.html` inline handler (`LoginFunc`) | Meaning unestablished; see Known Gaps |
| SID extraction | Regex `SID=(\d+)` from response body | HAR response analysis | SID format is firmware-specific |
| Username value | `"NULL"` (Compal single-password auth) or `"admin"` | modem.yaml config | Varies by modem model |
| Key derivation from cookie | SHA256 for key, MD5 for IV | `encrypt_cryptoJS.js` | CryptoJS supports other modes; this is a firmware choice |

## Config Reference

See [MODEM_YAML_SPEC.md](MODEM_YAML_SPEC.md#form_cbn) for the complete field table.

Fields that map to **crypto library** (CryptoJS-level):

- None --- all crypto parameters are hardcoded (AES-256-CBC, SHA256 key, MD5 IV, PKCS7). CryptoJS supports other configurations but this firmware uses fixed settings.

Fields that map to **firmware** (Compal-level):

- `login_page` --- URL to GET for initial session token
- `getter_endpoint`, `setter_endpoint` --- XML API endpoints
- `session_cookie_name`, `sid_cookie_name` --- cookie names
- `username_value` --- literal username string
- `login_fun` --- `fun` parameter value for login action

## Evidence Base

A protocol claim in this spec is evidence-backed when it traces to
firmware JavaScript recorded in a catalog capture, or to behaviour
observed on the wire where firmware source does not document it. The
firmware sources below establish the crypto envelope; the assumptions
table cites its own evidence per row. The captures
themselves are catalog data --- derive them with the query under
Platform Notes rather than listing them here.

| Firmware source | Establishes |
|---|---|
| `encrypt_cryptoJS.js` | Encryption format and key/IV derivation |
| `common_page/login.html` (inline `LoginFunc`) | Success test; `cbnBlockContent`, `lockedout`, `cbnAccessDenied` branches |
| `js/common_api.js` (`NoticeLogin`, `ajaxSetToDB`, `ajaxSetNoTokenUpdate`) | `cbnLogin`, `cbnFirstInstall`, `lockedout` branches |

Both token sources are the CH7465MT capture, the only catalog HAR that
records CBN firmware JS. See Known Gaps.

## Platform Notes

Entries on this platform share an identical auth flow. Branding is not a
reliable signal of platform: CBN/Compal firmware ships under other
vendors' names, so a modem branded by one manufacturer can run this
strategy. Catalog entries record the firmware variant.

Which entries use this strategy is catalog data, not spec content.
Query it:

```python
from solentlabs.cable_modem_monitor_catalog import CATALOG_PATH
from solentlabs.cable_modem_monitor_core.catalog_manager import list_modems

[m for m in list_modems(CATALOG_PATH) if m.auth_strategy == "form_cbn"]
```

Each `ModemSummary` carries `manufacturer`, `model`, `status`,
`transport`, and `sibling_dirs` for entries sharing one model identity.

## Known Gaps

- **Parameter order sensitivity**: The `token` parameter must be first in the form-encoded POST body. This was discovered empirically during strategy development --- the modem firmware rejects requests with different parameter ordering. This is not documented in the firmware source and may not apply to all firmware versions.
- **Rotating token timing**: The session token rotates on every response. If a request fails or times out, the token state may desynchronize. The `requests.Session` cookie jar tracks the latest token, but concurrent requests could race.
- **MD5 for IV derivation**: MD5 is cryptographically broken but required here for protocol fidelity. This is a firmware design choice, not a bug in our implementation.
- **`cbnBlockContent` meaning is unestablished**: the firmware routes it to `Blocked-content.html`, so the capture establishes only what it is *not* --- the password-error branch. Nothing in the capture says what condition produces it or whether waiting clears it. Core therefore claims no recovery and treats it as a rejection, naming the token in the error so a field occurrence is distinguishable from a real bad password. Marking it busy would assert a retry semantics the evidence does not support. A capture that shows the modem emitting it would settle the mapping.
- **The token vocabulary rests on one device**: `common_page/login.html` and `js/common_api.js` from the CH7465MT capture are the sole source. `arris/sb8200-cbn` ships a synthetic fixture with no firmware JS, so the fleet gate skips it and its firmware is unconfirmed against this vocabulary. The tokens are read as platform-wide because `common_api.js` is a shared library that branches on `_OperatorId` within one build, serving several operator deployments; that supports platform-wide, not cross-model. An unknown token falls into the wrong-password branch, which is the pre-existing behaviour, so an entry whose firmware differs is no worse off than before.
