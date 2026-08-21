# `form_pbkdf2` -- PBKDF2 Challenge-Response Auth

## Overview

Multi-round-trip PBKDF2 challenge-response. Client requests server-provided salts, derives a key via PBKDF2-HMAC-SHA256, and submits the derived hash. Optionally double-hashes with a second salt. No encryption of the payload -- the derived hash is sent as plaintext hex in a form-encoded POST. No external crypto dependencies (uses Python's `hashlib`).

## Crypto Library

This section documents what SJCL's PBKDF2 standardizes. It is the
implementation authority -- if code deviates from these encoding rules,
code is wrong.

These modems use the SJCL JavaScript library for PBKDF2 computation, but unlike `form_sjcl`, the salt is passed as a plain string (not hex-decoded). The key difference:

- **Salt**: Plain string from JSON response. UTF-8 encoded for PBKDF2. The modem's `login.js` passes the salt string directly to `sjcl.misc.pbkdf2()` which internally converts strings via `sjcl.codec.utf8String.toBits()`. This is NOT hex-decoded -- contrast with `form_sjcl` where the wrapper explicitly calls `sjcl.codec.hex.toBits(salt)`.
- **Password**: UTF-8 encoded for first PBKDF2 round. For double-hash, the hex output of the first round is UTF-8 encoded as the "password" for the second round.
- **Key derivation**: PBKDF2-HMAC-SHA256 (RFC 8018). Output is hex-encoded and sent as plaintext.
- **No encryption**: The derived hash is sent in a plaintext form-encoded POST. The security relies on the hash being non-reversible, not on transport encryption.

## Auth Flow

```text
1. Fetch CSRF token (optional, if csrf_init_endpoint configured):
   GET csrf_init_endpoint
   Extract token from JSON body ("token"|"csrf"|"csrfToken" keys)
   or from response header (csrf_header name)
   Set on session: session.headers[csrf_header] = token

2. Request server salts (form-encoded):
   POST login_endpoint  username=<user>&password=<salt_trigger>
   Content-Type: application/x-www-form-urlencoded
   Response: {"salt": "<salt_string>", "saltwebui": "<saltwebui_string>"}
   Salt values are plain strings, NOT hex-encoded.

3. Derive key (first hash):
   PBKDF2-HMAC-SHA256(password.utf8, salt.utf8, iterations, key_len)
   Output: hex string

4. Double-hash (if double_hash: true):
   PBKDF2-HMAC-SHA256(derived_hex.utf8, saltwebui.utf8, iterations, key_len)
   Output: hex string
   If saltwebui missing from response, falls back to salt.

5. POST login (form-encoded):
   POST login_endpoint  username=<user>&password=<derived_hex>
   Content-Type: application/x-www-form-urlencoded
   Busy:    HTTP != 401 AND login_busy dict matches response (checked first; AuthResult.busy)
   Success: HTTP != 401 AND ("error" field absent/falsy, OR login_success dict matches response)
   Sets PHPSESSID cookie

6. Subsequent requests:
   GETs use PHPSESSID cookie only
   POSTs include PHPSESSID + csrf_header
```

## Firmware Assumptions

What's hardcoded in `auth/form_pbkdf2.py` that is specific to the Technicolor REST platform, not inherent to PBKDF2:

| Assumption | Value | Source | Risk if variant differs |
|---|---|---|---|
| POST field names | `"username"`, `"password"` | Technicolor login.js | Other vendors may use different field names |
| Salt response fields | `"salt"`, `"saltwebui"` | Technicolor login.js, HAR | Different API may use different key names |
| Salt trigger value | default `"seeksalthash"` | Technicolor login.js | Other firmware may use different trigger |
| Success criteria | HTTP != 401 AND (`login_success` dict matches response, OR `"error"` is falsy/absent) | HAR response analysis; `{"error":"ok"}` confirmed on success | Configurable via `login_success` in modem.yaml |
| Busy criteria | `login_busy` dict matches response, checked before success | Technicolor login.js branches on `message == "MSG_LOGIN_150"` (#120) | Configurable via `login_busy` in modem.yaml; unset means no busy detection |
| Error detail field | `"message"` key in error response | HAR response analysis | Firmware-specific |
| Form-encoded POST | `application/x-www-form-urlencoded` | HAR request headers, login.js jQuery `$.ajax({data})` | All known Technicolor REST modems use form-encoded |
| Same endpoint for salt and login | Single `login_endpoint` for both rounds | Technicolor API design | Other designs may separate these |

## Config Reference

See [MODEM_YAML_SPEC.md](MODEM_YAML_SPEC.md#form_pbkdf2) for the complete field table.

Fields that map to **crypto library** (PBKDF2-level):

- `pbkdf2_iterations`, `pbkdf2_key_length` -- crypto parameters
- `double_hash` -- whether to apply the second derivation round

Fields that map to **firmware** (Technicolor-level):

- `salt_trigger` -- the magic string that triggers salt response
- `login_endpoint` -- URL path
- `csrf_init_endpoint`, `csrf_header` -- CSRF mechanism
- `cookie_name` -- session cookie name

## Evidence Base

A protocol claim in this spec is evidence-backed when it traces to
firmware JavaScript recorded in a catalog capture, or to behaviour
observed on the wire where firmware source does not document it. The
firmware sources below establish the crypto envelope; the assumptions
table cites its own evidence per row. The captures
themselves are catalog data -- derive them with the query under
Platform Notes rather than listing them here.

| Firmware source | Establishes |
|---|---|
| `login.js` | PBKDF2 parameters, salt encoding, and the salt fallback branch |

## Platform Notes

Entries on this platform share the same Technicolor REST API surface
(`/api/v1/session/`) and an identical auth flow.

Which entries use this strategy is catalog data, not spec content.
Query it:

```python
from solentlabs.cable_modem_monitor_catalog import CATALOG_PATH
from solentlabs.cable_modem_monitor_core.catalog_manager import list_modems

[m for m in list_modems(CATALOG_PATH) if m.auth_strategy == "form_pbkdf2"]
```

Each `ModemSummary` carries `manufacturer`, `model`, `status`,
`transport`, and `sibling_dirs` for entries sharing one model identity.

## Known Gaps

- **Salt encoding contrast**: The salt is UTF-8 encoded (plain string), NOT hex-decoded. This is the opposite of `form_sjcl`. Both use SJCL's PBKDF2, but the SJCL wrapper functions handle the salt differently. The `form_pbkdf2` modems pass the salt string directly to `sjcl.misc.pbkdf2()` which UTF-8 encodes it internally. The `form_sjcl` modems pass through `sjclPbkdf2()` which hex-decodes first.
- **`salt == "none"` fallback**: The modems' login.js contains a branch that skips PBKDF2 and sends the plaintext password when the salt is `"none"` (first-time setup, factory reset). This branch is documented in ARCHITECTURE.md but not implemented in the strategy code.
- **PBKDF2 params partially redacted**: One HAR capture has `***REDACTED***` for some PBKDF2 constants. Values confirmed from the other HAR's login.js.
- **Second POST not captured in HAR**: The salt request is captured but the hashed-password POST was executed by JavaScript and not recorded. Auth flow is inferred from login.js analysis.
- **Neither modem confirmed**: No real-world validation yet.
