# `form_sjcl` --- SJCL AES-CCM Encrypted Auth

## Overview

SJCL (Stanford JavaScript Crypto Library) AES-CCM strategy. Encrypts
credentials client-side with AES in CCM mode using a PBKDF2-derived key.
Server response is also encrypted --- must decrypt to extract CSRF nonce.
Requires the `cryptography` package
(`pip install solentlabs-cable-modem-monitor-core[sjcl]`).

## Crypto Library

This section documents what SJCL standardizes. It is the implementation
authority --- if code deviates from these encoding rules, code is wrong.

SJCL is an open-source JavaScript crypto library with a stable API. Any
modem using it gets the same encoding rules:

- **Salt**: Hex string on the login page. SJCL's wrapper calls
  `sjcl.codec.hex.toBits(salt)` --- hex-decoded to binary bytes before
  PBKDF2. NOT UTF-8 encoded.
- **IV**: Hex string on the login page. Hex-decoded to binary bytes.
  Must be 7--13 bytes per RFC 3610 (AES-CCM nonce).
- **Password**: UTF-8 encoded for PBKDF2. SJCL internally calls
  `sjcl.codec.utf8String.toBits(password)`.
- **AAD (Additional Authenticated Data)**: UTF-8 string. SJCL
  triple-converts it (`utf8String.toBits` -> `hex.fromBits` ->
  `hex.toBits`) which is effectively a no-op --- the result is the
  UTF-8 bytes.
- **Key derivation**: PBKDF2-HMAC-SHA256 (RFC 8018). Output is raw key
  bytes used directly as AES key.
- **Encryption**: AES-CCM (NIST SP 800-38C). Authenticated encryption
  with associated data.

These encoding rules are universal to SJCL, not specific to any modem
firmware.

## Auth Flow

Step-by-step with explicit encoding at every boundary:

```text
1. GET login page (login_page)
   Status >= 400 fails the login, with the response attached so the
   collector can classify it (a 5xx is a busy modem, not a credential).
   Parse JS variables: myIv (hex), mySalt (hex), currentSessionId
   Note: myIv and mySalt are required. A page answering under 400
   without them fails the login and attaches the page, whose body is
   the only thing separating a wrong device from moved variables.

2. Derive AES key:
   PBKDF2-HMAC-SHA256(password.utf8, hex_decode(mySalt), iterations, key_len)
   Note: SJCL's sjclPbkdf2() calls sjcl.codec.hex.toBits(salt)
   before sjcl.misc.pbkdf2(). The IV is also hex-decoded.

3. Encrypt credentials:
   plaintext = JSON.serialize({"Password": "<pw>", "Nonce": "<sessionId>"})
   Note: browser builds this via string concatenation with spaces after
   ":" and ",".  Python json.dumps() default (no separators arg) matches.
   ciphertext = AES-CCM(key, hex_decode(myIv), plaintext.utf8, aad=encrypt_aad.utf8)

4. POST login (login_endpoint):
   {"EncryptData": hex(ciphertext), "Name": "<user>", "AuthData": "<encrypt_aad>"}
   Response: {"p_status": "AdminMatch"|"Match", "encryptData": "<hex>"}
   Sets session cookie (auth.cookie_name)

5. Decrypt response:
   AES-CCM decrypt hex_decode(encryptData) with aad=decrypt_aad.utf8
   Result: CSRF nonce (UTF-8 string)
   Set csrf_header on session for subsequent requests

6. POST session validation (session_validation_endpoint, optional):
   Empty POST (no body) with csrf_header -> finalizes session
   Accepts HTTP 200 as success regardless of response body
```

## Firmware Assumptions

What's hardcoded in `auth/form_sjcl.py` that is specific to the Arris
Touchstone firmware family, not inherent to SJCL:

| Assumption | Value | Source | Risk if variant differs |
|---|---|---|---|
| JS variable names | `myIv`, `mySalt`, `currentSessionId` | `base_95x.js` | Different firmware may use different variable names |
| POST field names | `EncryptData`, `Name`, `AuthData` | `base_95x.js` login() function | Other vendors may use different field names |
| Plaintext structure | `{"Password": "<pw>", "Nonce": "<sessionId>"}` | `base_95x.js` login() function | JSON keys are firmware-specific |
| Response encrypted field | `encryptData` (lowercase 'e') | HAR response from ajaxSet_Password.php | Field name is firmware-specific |
| Success field and values | `p_status` in (`"AdminMatch"`, `"Match"`) | HAR response, `base_95x.js` loginPasswordChk() | Success detection is firmware-specific |
| PBKDF2 hash algorithm | SHA-256 | SJCL default | Universal for SJCL but hardcoded in code |

When a second SJCL modem appears with different wire format, the
refactoring point is these assumptions --- extract to config or a
separate wire-format handler.

## Config Reference

See [MODEM_YAML_SPEC.md](MODEM_YAML_SPEC.md#form_sjcl) for the complete
field table.

Fields that map to **crypto library** (SJCL-level):

- `pbkdf2_iterations`, `pbkdf2_key_length`, `ccm_tag_length` --- crypto
  parameters

Fields that map to **firmware** (Arris-level):

- `encrypt_aad`, `decrypt_aad` --- AAD strings are firmware-specific
  choices
- `login_page`, `login_endpoint`, `session_validation_endpoint` --- URL
  paths
- `csrf_header`, `cookie_name` --- header/cookie names

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
| `sjclCrypto.js` | Salt and IV encoding rules |
| `base_95x.js` | Wire format, JS variable names, and POST field names |

## Platform Notes

Entries on this platform share an identical auth flow. Firmware in this
family carries model flags suggesting sibling hardware variants exist,
so a capture from one gateway may not represent the whole line.

Which entries use this strategy is catalog data, not spec content.
Query it:

```python
from solentlabs.cable_modem_monitor_catalog import CATALOG_PATH
from solentlabs.cable_modem_monitor_core.catalog_manager import list_modems

[m for m in list_modems(CATALOG_PATH) if m.auth_strategy == "form_sjcl"]
```

Each `ModemSummary` carries `manufacturer`, `model`, `status`,
`transport`, and `sibling_dirs` for entries sharing one model identity.

## Known Gaps

- **Single modem**: No wire format variation has been observed. All
  firmware assumptions are derived from one modem.
- **`encryptflag` not checked**: The login page has an `encryptflag` JS
  variable (`'true'` in all captures). The code always encrypts --- if a
  firmware version has `encryptflag = 'false'`, the strategy would fail.
- **Pre-auth `csrfNonce` header**: The browser's `$.ajaxSetup` sends
  `csrfNonce: "undefined"` on every AJAX request before login. Declared
  as a static header in `session.headers` in modem.yaml. The auth
  strategy overwrites it with the decrypted nonce after login.
- **Session cleanup not tested**: The browser calls `logout.php` via
  `doSessionClean()` before login. The `csrfNonce` header fix resolves
  the 471 error, but session cleanup may still matter for stale-session
  rejection on modems with single-session enforcement.
