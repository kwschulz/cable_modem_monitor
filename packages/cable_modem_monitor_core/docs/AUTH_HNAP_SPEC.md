# `hnap` --- HNAP HMAC Challenge-Response Auth

## Overview

HNAP (Home Network Administration Protocol) HMAC challenge-response
authentication. The client sends a `Login` SOAP-over-JSON action with
`Action: "request"`, the modem returns a per-session `Challenge`,
`PublicKey`, and `Cookie`. The client derives a `PrivateKey` via
HMAC(`PublicKey + password`, `Challenge`) and a `LoginPassword` via
HMAC(`PrivateKey`, `Challenge`), then sends a second `Login` action
with `Action: "login"` carrying the derived `LoginPassword`. After
login, every subsequent HNAP action is signed with an `HNAP_AUTH`
header computed from the `PrivateKey`, the action name, and the
current timestamp.

HNAP is a distinct transport, not a form-auth variant. Endpoint
(`/HNAP1/`), content type (JSON), SOAP namespace
(`http://purenetworks.com/HNAP1/`), session cookies (`uid`,
`PrivateKey`), and auth header name (`HNAP_AUTH`) are fixed by the
protocol --- only the HMAC algorithm varies across firmware. No
external crypto dependencies (uses Python's `hashlib` / `hmac`).

## Crypto Library

This section documents what the HNAP protocol standardises. It is the
implementation authority --- if code deviates from these encoding
rules, code is wrong. The shared primitives live in
`protocol/hnap.py` and are consumed by `auth/hnap.py`,
`loaders/hnap.py`, and `orchestration/actions/hnap_action.py`.

HNAP defines one keyed-hash primitive, two derivation rules, and one
per-request header format:

- **HMAC output encoding**: `hmac_hex(key, message, algorithm)`
  returns the HMAC digest as **uppercase hex**. Both the reference
  JavaScript (`hmac_md5.js`, `hmac_sha256.js`) and firmware expect
  uppercase; lowercase hex causes the modem to reject the login even
  when the underlying digest is correct.
- **Key/message encoding**: both `key` and `message` are UTF-8
  encoded before HMAC. Strings come straight from the challenge
  response (already JSON-decoded UTF-8) and the user's password.
- **HMAC algorithm**: `md5` or `sha256`, selected per modem via
  `auth.hmac_algorithm`. The algorithm applies to every HMAC in the
  flow (pre-auth header, `PrivateKey` derivation, `LoginPassword`
  derivation, post-auth headers) --- it is never mixed within a
  session.
- **`PrivateKey` derivation**:
  `hmac_hex(key = PublicKey + password, message = Challenge, algorithm)`.
  The `PublicKey` from the challenge response is prepended to the
  user's password with no separator --- the concatenation is then
  UTF-8 encoded as the HMAC key.
- **`LoginPassword` derivation**:
  `hmac_hex(key = PrivateKey, message = Challenge, algorithm)`. The
  derived hash is what the modem compares against --- the user's
  raw password never leaves the client.
- **`HNAP_AUTH` header format**:
  `"<HMAC_HEX> <TIMESTAMP>"` (hex, single space, timestamp).
  - `HMAC_HEX = hmac_hex(key = signing_key, message = TIMESTAMP + soapActionURI, algorithm)`
  - `signing_key` is the literal string `"withoutloginkey"` for the
    pre-auth challenge request, and the derived `PrivateKey` for
    every request after login.
  - `soapActionURI` is the SOAPAction header value **including its
    enclosing double-quotes**:
    `'"http://purenetworks.com/HNAP1/Login"'`. The quotes are part
    of the HMAC input --- stripping them produces a valid-looking but
    rejected header.
  - `TIMESTAMP = floor(time_ms) mod 2_000_000_000_000`. The modulo
    matches the firmware's 32-bit integer handling (from
    `SOAPAction.js`: `Math.floor(Date.now()) % 2000000000000`) and
    is rendered as a decimal string.

These rules are universal across HNAP firmware. Any modem identifying
itself as HNAP implements this same crypto envelope; firmware
differences show up as action name variations, not crypto changes.

## Auth Flow

Step-by-step with explicit encoding at every boundary. All requests
POST to `{base_url}/HNAP1/`:

```text
1. Send challenge request (Login with Action="request"):
   POST /HNAP1/
     Content-Type: application/json; charset=utf-8
     SOAPAction: "http://purenetworks.com/HNAP1/Login"
     HNAP_AUTH: hmac_hex(
       key="withoutloginkey",
       message=TIMESTAMP + '"http://purenetworks.com/HNAP1/Login"',
       algorithm=hmac_algorithm,
     ) + " " + TIMESTAMP
     Body (JSON):
       {"Login": {
          "Action": "request",
          "Username": "<user>",
          "LoginPassword": "",
          "Captcha": "",
          "PrivateLogin": "LoginPassword"
       }}

   Response (JSON):
     {"LoginResponse": {
        "Challenge":  "<hex string>",
        "PublicKey":  "<hex string>",
        "Cookie":     "<session uid>",
        "LoginResult": "OK"
     }}

   Any of Challenge / PublicKey / Cookie missing -> auth fails before
   phase 2; the collector surfaces AUTH_FAILED with the missing-field
   detail.

2. Derive keys from challenge response:
   PrivateKey    = hmac_hex(PublicKey + password, Challenge, algorithm)
   LoginPassword = hmac_hex(PrivateKey,           Challenge, algorithm)

3. Install session cookies BEFORE the login POST:
   uid        = <Cookie from step 1>    (path=/)
   PrivateKey = <derived PrivateKey>    (path=/)

   Both cookies are HNAP protocol-level state. Login.js sets them
   via:
     $.cookie('uid', obj.Cookie, { path: '/' });
     $.cookie('PrivateKey', PrivateKey, { path: '/' });
   Some firmware returns HTTP 500 on subsequent data requests when
   the PrivateKey cookie is missing --- set both even though the
   auth header alone would satisfy the HMAC check.

4. Send login request (Login with Action="login"):
   POST /HNAP1/
     Content-Type: application/json; charset=utf-8
     SOAPAction: "http://purenetworks.com/HNAP1/Login"
     HNAP_AUTH: hmac_hex(
       key=PrivateKey,
       message=TIMESTAMP + '"http://purenetworks.com/HNAP1/Login"',
       algorithm=hmac_algorithm,
     ) + " " + TIMESTAMP
     Cookie: uid=<Cookie>; PrivateKey=<PrivateKey>
     Body (JSON):
       {"Login": {
          "Action": "login",
          "Username": "<user>",
          "LoginPassword": "<derived LoginPassword>",
          "Captcha": "",
          "PrivateLogin": "LoginPassword"
       }}

   Response (JSON):
     {"LoginResponse": {"LoginResult": "<status>"}}

5. Classify LoginResult:
   "OK" | "OK_CHANGED"  -> authenticated; return AuthResult(
                            success=True,
                            auth_context=AuthContext(private_key=PrivateKey),
                          )
   "FAILED"              -> wrong username or password (AUTH_FAILED)
   "LOCKUP" | "REBOOT"   -> firmware anti-brute-force triggered
                            (raises LoginLockoutError, which the
                             collector maps to AUTH_LOCKOUT;
                             see § Lockout behaviour below)
   "RELOAD"              -> session state stale, restart the login
                            (AuthResult busy=True, which the collector
                             maps to AUTH_UNAVAILABLE; see § Restart
                             the login below)
   anything else         -> unexpected protocol state (AUTH_FAILED)

6. Subsequent requests (resource loads, action executors):
   Each HNAP call re-computes HNAP_AUTH with:
     key     = PrivateKey   (from AuthContext, also present as cookie)
     message = TIMESTAMP + '"http://purenetworks.com/HNAP1/<Action>"'
   Cookies uid + PrivateKey are carried automatically by the
   requests.Session jar. See loaders/hnap.py and
   orchestration/actions/hnap_action.py for consumers.
```

### Lockout behaviour

HNAP firmware implements its own anti-brute-force throttle. After a
firmware-defined number of consecutive wrong-password attempts, the
login response changes from `"FAILED"` to `"LOCKUP"` (temporary
lock, modem rejects further attempts for a cool-down window) or
`"REBOOT"` (some firmware forces a device restart to clear the
lock).

`HnapAuthManager._login_with_credentials` detects these values and
raises `LoginLockoutError` directly --- the only exception any auth
strategy raises past the collector's own error handling.
`ModemDataCollector` catches it and returns
`CollectorSignal.AUTH_LOCKOUT`; the orchestrator never sees the
exception. `SignalPolicy` trips the circuit breaker on the first
occurrence, so polling stops outright.

There is no lockout backoff and no login suppression window. A modem
refusing logins to protect itself gains nothing from a slower retry,
so the policy is one attempt then stop rather than a cool-down timer
(ARCHITECTURE_DECISIONS.md § Session reuse across polls). Session
reuse is the primary defence against ever reaching lockout; the
circuit breaker is the safety net.

HNAP keeps a distinct exit path (`LoginLockoutError` ->
`AUTH_LOCKOUT`) rather than collapsing into generic `AUTH_FAILED`, and
that path now earns its keep downstream. Both signals still trip the
breaker on the first occurrence, but from there they diverge:

- The breaker records `AUTH_LOCKOUT` as its trip reason, so the blocked
  polls that follow report the lockout rather than advising a
  credential change, and a diagnostics download taken after polling
  stopped can tell a lockout from a wrong password
  (ORCHESTRATION_SPEC.md § Auth Circuit Breaker, "Trip reason is
  preserved").
- The HA adapter does not raise its reauth prompt on this route.
  Submitting that form is a real login attempt, which is the one thing
  a locked-out modem must not receive; it fires a notification instead
  (HA_ADAPTER_SPEC.md § Reauth Flow, UC-81a).

A modem protecting itself is not a wrong password, and no surface says
it is. The remedy is to wait out the cool-down or power cycle, then
reload the integration --- the breaker never times out on its own
(§ Auth Circuit Breaker, "Recovery paths").

`_SIGNAL_ERROR_MAP` still maps `AUTH_LOCKOUT` to `invalid_auth`
(`config_flow_helpers.py`). That map feeds the config flow's error slot,
reached only when a user is already typing credentials into a form, and
the runtime route no longer sends them there. See ORCHESTRATION_SPEC.md
§ Exceptions and § Auth Circuit Breaker.

### Restart the login

`RELOAD` is the firmware asking the client to start the login over.
`Login.js` handles it by navigating to `/Login.html` with no alert ---
the only branch that neither blames the credential nor reports a lock.
The token appears on the S33v3 firmware line (`AT01.01.*`); the older
Arris HNAP captures have no such branch.

The manager returns `AuthResult(success=False, busy=True)`, which the
collector classifies as `AUTH_UNAVAILABLE`: the modem declined to serve
this login without judging the credential, so polling continues at
normal cadence and the condition clears on its own (UC-87a). This is
the same treatment a 5xx login gets, reached from a protocol token
rather than a status code.

Reading it as a credential verdict is what #201 was: `AUTH_FAILED`
trips the breaker on the first occurrence, so one `RELOAD` stopped
polling outright and told the user to reconfigure a password that was
never wrong, while their browser recovered from the identical response
by doing what the firmware asked.

`busy` is set in the strategy, not declared per entry: HNAP's token set
is fixed by the protocol, so there is nothing for a catalog entry to
declare (§ Config Reference). `form_cbn` sets it the same way and for
the same reason (AUTH_CBN_SPEC.md § Login Token Vocabulary). The
structural difference is with `form_pbkdf2`, whose busy body is entry
data (`login_busy`).

## Firmware Assumptions

What's hardcoded in `protocol/hnap.py` and `auth/hnap.py` that is
specific to HNAP firmware family, not inherent to HMAC:

| Assumption | Value | Source | Risk if variant differs |
|---|---|---|---|
| SOAP namespace | `http://purenetworks.com/HNAP1/` | Login.js, SOAPAction.js | Universal across known HNAP firmware --- fixed by protocol |
| Endpoint | `/HNAP1/` | Login.js, SOAPAction.js | Universal across known HNAP firmware --- fixed by protocol |
| Pre-auth signing key | literal string `"withoutloginkey"` | Login.js challenge request | Firmware rejects the challenge if anything else is used |
| Login action body keys | `Action`, `Username`, `LoginPassword`, `Captcha`, `PrivateLogin` | Login.js | Known HNAP firmware (Motorola, Arris Surfboard) all use this shape |
| `PrivateLogin` value | `"LoginPassword"` | Login.js | Selects the `LoginPassword` credential field; variant firmware could declare a different field name |
| Challenge response fields | `Challenge`, `PublicKey`, `Cookie`, `LoginResult` inside `LoginResponse` | Login.js, HAR captures | Missing any of the three credential fields aborts auth |
| `uid` cookie name | `uid` | Login.js (`$.cookie('uid', ...)`) | Hardcoded; firmware variant could pick a different cookie name |
| `PrivateKey` cookie name | `PrivateKey` | Login.js (`$.cookie('PrivateKey', ...)`) | Needed to avoid HTTP 500 on data requests on some firmware |
| `LoginResult` success values | `OK`, `OK_CHANGED` | Login.js, observed HAR | Other values currently treated as protocol-unexpected |
| `LoginResult` lockout values | `LOCKUP`, `REBOOT` | Login.js (`LoginResult == "LOCKUP"` branch) | Variant firmware could emit other lockout tokens --- would fall into `unexpected result` |
| `LoginResult` restart value | `RELOAD` | Login.js (`LoginResult == "RELOAD"` branch, S33v3 firmware line) | Present only on the AT01.01.* line; older Arris HNAP firmware omits the branch |
| Timestamp modulo | `2_000_000_000_000` | SOAPAction.js (`Math.floor(Date.now()) % 2000000000000`) | Matches 32-bit integer handling in firmware |
| HNAP_AUTH header format | `"<HEX> <TIMESTAMP>"` (uppercase hex, single space) | SOAPAction.js | Lowercase hex or alternate separators are rejected |
| HMAC algorithm family | `md5` or `sha256` only | HnapAuth model Literal | No other algorithms observed; variant firmware would need a new Literal value |

HNAP auth is fully constrained by the transport. When a new HNAP
firmware appears with different success values, body keys, or
lockout tokens, the refactoring point is this table --- extract to
config or a variant handler rather than threading conditionals
through `protocol/hnap.py`.

## Config Reference

See [MODEM_YAML_SPEC.md](MODEM_YAML_SPEC.md#hnap) for the complete
field table.

Fields that map to **crypto library** (HNAP-level):

- `hmac_algorithm` --- `md5` or `sha256`. Sole required HNAP auth
  field; chosen per modem from HAR evidence (firmware either calls
  `hmac_md5.js` or `hmac_sha256.js`).

Fields that map to **firmware** (protocol-level):

- None. HNAP's endpoint, SOAP namespace, session cookies, and
  header names are fixed by the protocol. Modem-specific behaviour
  (per-model action names, restart mechanism) belongs in
  `actions` --- not in the `auth` block.

A consequence of this constraint: a `session` block on a modem with
`auth.strategy: hnap` is rejected as a config error. The implicit
HNAP session (uid + PrivateKey cookies, HNAP_AUTH header) is the
only valid session mechanism. See
[MODEM_YAML_SPEC.md § Session / Transport compatibility](MODEM_YAML_SPEC.md#session).

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
| `Login.js` | Challenge/login request shape and cookie setup |
| `SOAPAction.js` | HNAP_AUTH header format and timestamp modulo |
| `hmac_md5.js` / `hmac_sha256.js` | Uppercase hex HMAC output |

## Platform Notes

Every HNAP modem shares the same auth envelope, parser logic, and auth
flow. Entries diverge on exactly two axes, both config-driven:

- **HMAC algorithm** --- signing uses MD5 or SHA256, selected per entry
  by `auth.hmac_algorithm`, because firmware lines differ.
- **Action names** --- the `actions` block carries the vendor's naming
  (`GetMoto*`, `GetCustomer*` / `SetArrisConfigurationInfo`).

Which entries use which is catalog data, not spec content. Query it:

```python
from solentlabs.cable_modem_monitor_catalog import CATALOG_PATH
from solentlabs.cable_modem_monitor_core.catalog_manager import list_modems

[m for m in list_modems(CATALOG_PATH) if m.auth_strategy == "hnap"]
```

Each `ModemSummary` carries `manufacturer`, `model`, `status`,
`transport`, and `sibling_dirs` for entries sharing one model identity.

## Known Gaps

- **No captcha path**: `Captcha: ""` is sent unconditionally. The
  `Login.js` reference code has a captcha branch for some firmware
  variants; no captured HAR has exercised it and the strategy has
  no config surface to populate a captcha value.
- **Token coverage beyond the five observed**: `OK`, `OK_CHANGED`,
  `FAILED`, `LOCKUP`, `REBOOT` and `RELOAD` are the tokens the captured
  `Login.js` files branch on. Any other value falls into the generic
  `unexpected result` path and is surfaced as `AUTH_FAILED`, which trips
  the breaker on the first occurrence. That default is deliberately
  pessimistic, and it was wrong once: `RELOAD` sat unhandled in the
  S33v3 capture for seven weeks and stopped a working integration
  (#201). The catalog gate `test_hnap_login_result_coverage.py` now
  reads the tokens back out of every HNAP entry's captured `Login.js`
  and fails when one is unhandled, so a new firmware line cannot repeat
  it. Firmware that emits a token without shipping the branch in
  `Login.js` remains invisible.
- **Timestamp collision within a session**: HNAP_AUTH uses a
  millisecond timestamp modulo `2_000_000_000_000`. Two HMAC
  computations in the same millisecond produce identical
  `HMAC_HEX TIMESTAMP` pairs. This is a theoretical concern only
  --- our loader serialises HNAP calls and no observed modem
  rejects identical auth headers as replays --- but it's worth
  noting for future parallel-loader work.
- **Anti-replay behaviour not characterised**: No firmware has been
  observed rejecting a reused `HNAP_AUTH` header. The timestamp is
  in the HMAC but it is unclear whether the firmware validates it
  against a freshness window or only uses it as HMAC salt.
- **MD5 security**: part of the HNAP fleet uses HMAC-MD5 (derive which
  from `auth.hmac_algorithm`).
  HMAC-MD5 has no known practical break but MD5 itself is
  cryptographically weak. This is a firmware choice, not a bug in
  our implementation --- `hmac_algorithm` is dictated by the
  firmware's JavaScript, not chosen by us.
