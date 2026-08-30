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

5. Extract SID from response body:
   Regex: SID=(\d+)
   Error if not found after successful login

6. Set SID cookie on session:
   Cookie name from sid_cookie_name (default: "SID")
   Domain MUST match modem hostname (required for IP-based access)
   Both sessionToken (rotating) and SID (stable) are needed for
   subsequent authenticated requests
```

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
