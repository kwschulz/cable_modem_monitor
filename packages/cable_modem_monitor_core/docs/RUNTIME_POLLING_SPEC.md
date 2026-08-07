# Runtime Polling Specification

After setup, the integration polls the modem on a user-configured cadence
(default 10 minutes). The orchestrator is a scheduler and policy engine
that delegates to three specialized components — each independently
testable with a clear input→output contract.

**Design principles:**

- Protocol layers signal conditions; the orchestrator owns all policy
- Sessions persist across polls — re-login is the exception, not the rule
- Auth circuit breaker stops polling after persistent auth failures
- Health monitoring runs independently from data polling

---

## Components

```text
Orchestrator (scheduler + policy)
 ├─ ModemDataCollector  — one poll cycle → ModemData | signal
 ├─ HealthMonitor       — probe cycle → HealthInfo
 ├─ Recovery            — aggressive-poll window; cadence signal to consumer
 └─ restart()           — one-shot: auth → action → clear session → trigger recovery
```

**ModemDataCollector** executes a single poll cycle — the auth → load →
parse sequence. It owns no scheduling or retry policy; it runs once and
returns `ModemData` on success or a signal on failure. The orchestrator
decides whether to retry, backoff, or report.

```text
┌────────────────────────────────────────────────────────────┐
│ ModemDataCollector                                         │
│                                                            │
│  ┌──────────────┐    ┌─────────────────┐    ┌───────────┐  │
│  │ Auth Manager │───►│ Resource Loader │───►│  Parser   │  │
│  │              │    │                 │    │           │  │
│  │ session      │    │ fetch pages     │    │ resources │  │
│  │ lifecycle    │    │ build resource  │    │ → data    │  │
│  │ reuse/retry  │    │ dict            │    │           │  │
│  └──────────────┘    └─────────────────┘    └───────────┘  │
│                                                            │
│  → ModemData (channels, system info, DOCSIS lock state)    │
└────────────────────────────────────────────────────────────┘
```

**Auth Manager** owns the `requests.Session` and its lifecycle:

- Authenticates using the strategy and config from modem.yaml
- Reuses valid sessions across polls (avoids re-login every cycle)
- Validates session state before each poll using strategy-specific
  checks: HNAP verifies `uid` cookie + private key (the private key is
  also set as a `PrivateKey` cookie); cookie-based strategies verify
  `auth.cookie_name` is present in the cookie jar;
  basic and none are stateless (always valid)
- Raises `LoginLockoutError` on firmware lockout; tracks no lockout
  state of its own
- Executes `actions.logout` for single-session modems after each poll

**Resource Loader** uses the authenticated session to fetch data:

- Collects resource paths from parser.yaml at startup
- Dispatches by transport: HTTP → format-dependent (`BeautifulSoup` or `dict`),
  HNAP → JSON from SOAP response
- Builds the resource dict the parser expects
- Injects session tokens into URLs for URL token auth modems

See `RESOURCE_LOADING_SPEC.md` for the full resource dict contract,
loader behavior per transport, and URL construction details.

**Parser** transforms resources into structured data:

- Receives the resource dict, returns channels and system info
- Pure function — no session, no HTTP, no auth awareness
- Instantiated once from the catalog package at startup, reused every poll

**Orchestrator** owns scheduling, policy, and error recovery:

- Invokes `ModemDataCollector` for each poll cycle
- Trips the circuit breaker on lockout and on rejected credentials;
  applies exponential backoff on connectivity failure
- Coordinates `HealthMonitor` alongside collection
- Hands each snapshot to `Recovery` for heuristic evaluation and
  window management (see ORCHESTRATION_SPEC § Recovery)
- Exposes `restart()` as a one-shot command that triggers a recovery
  window (see ORCHESTRATION_SPEC § Restart Action)
- Returns status codes: `online`, `auth_failed`, `parser_issue`,
  `unreachable`, `no_signal`

---

## Poll Cycle

```text
Each poll:
 1. Orchestrator: circuit breaker open? → return auth_failed.
 2. Health recovery: if health monitor reports RESPONSIVE and
    connectivity backoff > 0, clear backoff and streak.
 3. Orchestrator: connectivity backoff active? → decrement. If still
    > 0 after decrement, return unreachable. If cleared to 0, proceed.
 4. Orchestrator invokes ModemDataCollector:
    a. Auth Manager: session valid? → reuse. Expired? → login.
    b. Resource Loader: fetch all pages using authenticated session.
       Build resource dict.
    c. Parser: ModemParserCoordinator.parse(resources) → channels + system info.
    d. Auth Manager: logout if single-session modem.
 5. Orchestrator: check ModemDataCollector result.
    Success → reset auth failure streak, reset connectivity state, derive status.
    Auth failure (AUTH_FAILED/AUTH_LOCKOUT) → trip circuit breaker immediately.
    Auth failure (LOAD_AUTH) → increment streak, trip at threshold.
    Connectivity failure → increment connectivity streak, set exponential backoff.
    Other failure → apply signal policy.
```

---

## Status Derivation

The orchestrator derives status fields after each poll:

**`connection_status`** — from pipeline outcome (snapshot field):

| Condition | Value |
|-----------|-------|
| Channels present | `online` |
| Zero channels, parser fulfilled all expected anchors | `no_signal` |
| Zero channels, parser fulfilled some anchors (no `system_info`) | `no_signal` (with diagnostic warning) |
| Zero channels, parser fulfilled **0 of N** expected anchors (stub response) | `auth_failed` via `LOAD_INTEGRITY` (see UC-19a) |
| Auth failure / lockout | `auth_failed` |
| Connection error / timeout | `unreachable` |

**`docsis_status`** — enriched into `system_info` (same pattern as
error totals and channel counts). The parser provides it when the
modem exposes a native value; the orchestrator fills it in when
absent. `snapshot.docsis_status` reads from the enriched
`system_info["docsis_status"]`.

*Parser provides `docsis_status`:* YAML `map` entries normalize
vendor values to the canonical `"Operational"` (see
SYSTEM_INFO_SPEC § Canonical Values). Non-mapped values pass through
as raw diagnostic strings (e.g., `"Ranging"`). The orchestrator does
not overwrite a parser-provided value.

*Parser does not provide `docsis_status`:* The orchestrator derives
it from downstream channel `lock_status` fields and writes it into
`system_info`. If the data needed for derivation is not available
(no downstream channels, or channels lack `lock_status`), the field
stays absent — same sparse-dict rule as other system_info fields.
No sensor is created when the field is absent.

Rows are evaluated top to bottom; the first match wins.

| Condition | Value |
|-----------|-------|
| No DS channels | *(absent — cannot derive)* |
| Channels lack `lock_status` | *(absent — cannot derive)* |
| No DS channel locked | `"not_locked"` |
| All DS locked AND upstream present | `"Operational"` |
| Anything else locked-but-incomplete — some DS locked, or all DS locked with no upstream | `"partial_lock"` |

The platform adapter composes `connection_status`, `docsis_status`,
and `health_status` (from the health pipeline) into a display state
via a priority cascade. See
[ENTITY_MODEL_SPEC.md](../../../custom_components/cable_modem_monitor/docs/ENTITY_MODEL_SPEC.md#status-sensor)
for the HA implementation's cascade rules.

---

## Derived Fields

The parser coordinator enriches `system_info` with fields derived
from parsed channel data. These appear in `modem_data.system_info`
before the orchestrator sees the data.

**Channel counts** (always computed by coordinator):

- `downstream_channel_count` — `len(downstream)`, always present
- `upstream_channel_count` — `len(upstream)`, always present
- If the parser maps native channel counts from the modem's web UI,
  the native value takes precedence.

**Aggregate fields** (declared in parser.yaml `aggregate` section):

- e.g., `total_corrected` — `sum(corrected)` across scoped channels
- Scope can be a direction (`downstream`) or type-qualified
  (`downstream.qam`, `downstream.ofdm`)
- Only computed when declared — parsers without an `aggregate` section
  produce no aggregate fields
- Modems with native totals in their web UI map them as `system_info`
  fields in parser.yaml instead
- Consumers read from `system_info` regardless of source

See [PARSING_SPEC.md](PARSING_SPEC.md#aggregate-derived-system_info-fields)
for the full schema.

---

## Design Rules

1. **All-or-nothing page loading.** If any page fetch fails, the entire
   poll fails. Partial data masks root causes — a missing page could mean
   wrong URL, changed firmware, or auth redirect. Log which page failed,
   the error type, and HTTP status if available. Previous `ModemData`
   persists in platform output until the next successful poll.

2. **Exponential backoff on connectivity failures.** When the modem is
   unreachable, connectivity backoff avoids wasting polls on timeouts.
   The backoff counter is set to `min(2^(streak-1), 6)` and decremented
   each poll cycle. The poll proceeds when the counter reaches 0
   (so counter=N skips N-1 polls): first failure retries immediately,
   then skip 1, 3, up to 5 polls. Any successful poll or non-
   connectivity failure (auth error, parse error) resets the
   connectivity state. User-initiated refresh (Update Modem Data
   button) calls `reset_connectivity()` to bypass backoff and attempt
   immediately. Connectivity failures never count
   toward the auth circuit breaker.

2a. **A login answering 5xx is not a credential verdict.** The auth
   path already refuses to read a transport failure as a rejected
   password (`ConnectionError` and `Timeout` on auth abort the poll and
   report `unreachable`). The same reasoning extends to HTTP status:
   5xx means the modem declined to serve the request, not that it
   examined the credential and rejected it. These emit
   `AUTH_UNAVAILABLE`, never touch the auth streak, and never reach a
   credential prompt no matter how many polls in a row they occur —
   some causes (ISP customer care holding the session) are outside the
   user's control entirely, so escalating to a reauth form only offers
   a remedy that cannot work. See UC-87a.

3. **No within-poll retries.** Every failure mode waits for the next
   scheduled poll cycle. This is inherent rate limiting — even at the
   minimum 30-second cadence, the modem gets breathing room between
   attempts.

4. **Lockout stops polling outright.** `LoginLockoutError` trips the
   circuit breaker on the first occurrence — polling stops entirely
   and the user must reconfigure credentials to resume. There is no
   lockout backoff window: a modem refusing logins to protect itself
   gains nothing from a slower retry, and each further attempt is what
   the firmware counts. Session reuse is the primary defense; the
   circuit breaker is the safety net. (Evidence: the `Login.js` in the
   S33/S33v2/S33v3 and SB8200-HNAP captures branches on
   `LoginResult == "LOCKUP"` with "Max number of login attempts
   reached"; #117 reported an S33v2 rebooting under polling that
   re-logged in every cycle. No `LOCKUP` response has itself been
   captured — see `AUTH_HNAP_SPEC.md` § Known Gaps. Full walkthrough:
   `ORCHESTRATION_SPEC.md` § Auth Circuit Breaker.)

5. **Auth strategies must validate success.** A 200 OK response does not
   mean authentication succeeded. Each strategy validates that the
   expected session artifacts were established (cookies set, token
   returned, expected redirect). If validation fails, the strategy
   returns `AuthResult.FAILURE` with diagnostic details — it does not
   assume success. Auth failures are the #1 onboarding issue; accurate
   signaling is critical.

6. **Each poll is independent.** The orchestrator does not distinguish
   "transient" from "permanent" failure. If the modem responds, it's
   `online`. If it doesn't, it's `unreachable`. Cross-poll state is
   limited to: auth backoff, connectivity backoff, and session. The
   `unreachable → online`
   transition is logged with session state for diagnostics (see Modem
   Restart, Unplanned restart).

---

## Signal and Policy Separation

Protocol layers **signal conditions**. The orchestrator **owns policy**.

A protocol layer (auth manager, resource loader, parser) translates raw
responses into meaningful signals — success, failure, lockout, zero
channels, timeout. It never decides what to do about those signals. The
orchestrator receives signals and applies policy — retry, backoff,
suppress, report.

```text
┌──────────────────────────────────────────────────────────┐
│  Orchestrator                                            │
│                                                          │
│  Owns: backoff, circuit breaker, session lifecycle,      │
│        error reporting                                   │
│                                                          │
│  Receives signals, decides actions                       │
├──────────────────────────────────────────────────────────┤
│  Protocol Layers                                         │
│                                                          │
│  Auth Manager    → signals: success, failure, lockout    │
│  Resource Loader → signals: status codes, timeouts       │
│  Parser          → signals: channels found, parse error  │
│                                                          │
│  Translates responses, never decides retry/backoff       │
└──────────────────────────────────────────────────────────┘
```

**Signal mechanism:** Protocol layers use exceptions for conditions that
callers must not ignore (e.g., `LoginLockoutError` for firmware
anti-brute-force), and return values for expected outcomes (success, wrong
password). Policy state like backoff counters lives exclusively on the
orchestrator.

| Layer | Signals (raises/returns) | Never decides |
|-------|--------------------------|---------------|
| Auth Manager | `LoginLockoutError`, `AuthResult` | Whether to retry |
| Resource Loader | Status codes, `Timeout`, `ConnectionError` | Retry count |
| Parser | `ModemData`, parse errors | Whether to fall back |

**Why this matters:** When a layer both signals a condition and decides what
to do about it, callers inherit hidden policy they can't override. Keeping
signal and policy separate means the orchestrator has full visibility into
what happened and full control over what to do next.

**Example — firmware lockout:** The auth manager's HNAP strategy receives
`LoginResult: "LOCKUP"` and raises `LoginLockoutError`. It does not track
lockout state or decide what happens next — that's policy. The collector
catches the exception and returns `AUTH_LOCKOUT`; `SignalPolicy` trips the
circuit breaker. The auth manager stays stateless with respect to lockout;
the orchestrator owns the recovery strategy, and could switch it to a
backoff without the strategy changing.

---

## Session Lifecycle

Sessions persist across polls. The auth manager decides per-poll whether
to reuse or refresh.

**Session reuse** — HNAP modems have firmware anti-brute-force that
can lock out or reboot the modem after repeated
login attempts. The auth manager reuses the session (cookies + HNAP
private key) as long as it's valid. This is critical — logging in every
poll triggers the lockout.

**Adaptive reuse disable** — some firmware keeps the local session
looking valid while the server has already expired it, which produces a
same-poll `LOAD_AUTH` recovery loop on every reused-session poll. After
2 consecutive recovered stale-session events, the orchestrator stops
attempting session reuse for the rest of that process lifetime and
starts each poll with a fresh login. An intervening normal successful
poll resets the recovery streak. This state is runtime-only —
orchestrator reconstruction (entry reload or process restart)
re-enables reuse.

Session reuse strategy is intentionally not exposed as a per-modem
yaml field. Per CLAUDE.md's "no per-modem recovery tuning" principle,
all reuse-strategy adaptation lives in core via the runtime streak
counter — generic across modems, self-tuning, and restart-resetting.
The streak is signal-agnostic: it counts any same-poll `LOAD_AUTH`
recovery, including reboot-induced stale sessions. False positives
are benign — forced fresh login is harmless. Note that this is
orthogonal to `actions.logout`, which controls *session lifecycle*
(logout-after-poll for single-session modems), not *reuse strategy*.

**Auth circuit breaker** — auth failures stop polling, on one of two
trip modes. Rejected credentials (`AUTH_FAILED`) and firmware lockout
(`AUTH_LOCKOUT`) trip on the first occurrence: the modem answered, and
it said no. Session-shaped failures (`LOAD_AUTH`, `LOAD_INTEGRITY`) may
self-correct, so they accumulate and trip at 6 consecutive failures.
Either way polling stops entirely. The client (HA) triggers a reauth
flow — the user must reconfigure credentials to resume. Manual refresh
deliberately does not bypass the breaker (see `ORCHESTRATION_SPEC.md`
§ Auth Circuit Breaker, "No manual bypass").

**Single-session logout** — modems with `actions.logout` configured
allow only one authenticated session. Logout fires in two places:

1. **After each successful poll** — frees the session so users can
   access the modem's web UI between polls. The local session is
   cleared only when the modem accepted the logout. A refused one
   (non-2xx) leaves the cookie in place: the server-side session is
   still live and that cookie is the only handle on it, so dropping it
   orphans one session per poll and locks out single-login firmware.
2. **Before a same-poll auth retry** — when `LOAD_AUTH` or
   `LOAD_INTEGRITY` fires, the orchestrator calls logout before
   clearing the stale local session and retrying. This recovers from
   a crash or unclean restart where the previous session was never
   released: since single-session firmware logout endpoints do not
   require credentials, the call succeeds even with no cookie. Both
   logouts are best-effort in that failure does not block the
   subsequent poll or retry; only the local session state turns on the
   answer.

The integration cannot clear another client's session (it doesn't have
their cookie). If another client holds the session when login is
attempted and the auth retry's logout doesn't free it, the strategy
returns `AuthResult.FAILURE`.

---

## State Across Polls

| State | Owner | Purpose | Lifetime |
|-------|-------|---------|----------|
| `session` (cookies) | Auth Manager | Avoid re-login every poll | Until expired or cleared |
| HNAP private key | Auth Manager | SOAP request signing + `PrivateKey` cookie | Until session expires |
| Session token | Auth Manager | URL token injection | Until session expires |
| Auth failure streak | Orchestrator | Circuit breaker threshold tracking | Reset on successful collection |
| Circuit open flag | Orchestrator | Stops polling on persistent auth failure | Cleared by orchestrator reconstruction (reauth → entry reload) |
| Stale-session recovery streak | Orchestrator | Tracks consecutive recovered `LOAD_AUTH` same-poll retries | Reset by an intervening normal success or orchestrator reconstruction |
| Session reuse disabled flag | Orchestrator | Forces fresh auth on each poll after repeated consecutive stale-session recoveries | Reset by orchestrator reconstruction (entry reload / process restart) |
| Connectivity streak | Orchestrator | Tracks consecutive unreachable failures | Reset on success, non-connectivity failure, reset_connectivity(), or health recovery |
| Connectivity backoff | Orchestrator | Exponential backoff: min(2^(streak-1), 6) | Decremented each poll, cleared by reset_connectivity() or health recovery |
| Last poll status | Orchestrator | Detect status transitions (e.g., unreachable → online) | Updated each poll |
| Parser instance | Orchestrator | Skip re-instantiation | Integration lifetime |
| Working URL | Orchestrator | Protocol (HTTP/HTTPS) | Integration lifetime |

---

## Health Pipeline

Monitors modem availability independently from data polling. Runs on
its own cadence (e.g., 30s) — typically faster than the data poll
(e.g., 10m). The client (platform adapter) schedules health checks; the
orchestrator reads the latest result during `get_modem_data()` without
triggering a new probe.

If the health check detects "unresponsive" between data polls, health
sensors update immediately — no waiting for the next data poll. This
gives faster outage detection, especially for unplanned reboots.

Health checks and data collection run independently on their own
cadences — neither pipeline suppresses the other. However, health
status feeds back into the connectivity backoff policy: when the
orchestrator detects that health is `RESPONSIVE` while a connectivity
backoff is active, it clears the backoff immediately. This prevents
the system from skipping polls against a modem that is proven
reachable. If the subsequent poll fails, the backoff re-engages
normally.

The platform adapter may additionally schedule an immediate data
poll when it detects a health recovery transition (e.g.,
`UNRESPONSIVE` → `RESPONSIVE`), reducing recovery latency from up
to one `scan_interval` to one `health_check_interval`.

See `ORCHESTRATION_SPEC.md` § HealthMonitor for the probe API and
status derivation matrix.

`health_status` is one of the three inputs to the Status sensor's
priority cascade — see
[ENTITY_MODEL_SPEC.md](../../../custom_components/cable_modem_monitor/docs/ENTITY_MODEL_SPEC.md#status-sensor).

---

## Modem Restart

Two paths lead to a modem restart. Both need session recovery and channel
stabilization, but they differ in who initiates and who monitors.

### Planned restart (user-triggered)

The user presses a "Restart Modem" button in Home Assistant. The
orchestrator sends the restart command via the action layer, then a
restart monitor takes over the polling loop temporarily.

```text
Button press
 ├─ Authenticate (fresh session for the restart command)
 ├─ Execute restart action (HNAP SOAP / HTML form POST / REST)
 │   └─ Connection drop during request = success (modem is rebooting)
 └─ Restart monitor:
    ├─ Clear auth cache (old session is dead)
    ├─ Switch to fast polling (e.g., 10s)
    ├─ Phase 1: wait for modem to respond (HTTP returns any response)
    ├─ Phase 2: wait for channel sync (stable counts for 3+ polls)
    ├─ Send notification (success / warning / timeout)
    └─ Restore original polling interval
```

The action layer is transport-specific — `actions.restart` in modem.yaml
declares the type (`http` or `hnap`) and the action
factory creates the right implementation. The orchestrator doesn't know
how the restart command is sent, only whether it succeeded.

The restart button is only available if the modem declares
`actions.restart` in modem.yaml. Modems without it show the button as
unavailable.

### Unplanned restart (external)

ISP-initiated restarts, power outages, firmware updates. No button press,
no restart monitor — the polling loop discovers the outage on its own.

```text
Normal poll
 ├─ Auth manager: session valid? → yes (stale cookies, modem doesn't know)
 ├─ Resource loader: fetch pages → connection refused / timeout
 └─ Orchestrator: status = unreachable

... modem reboots ...

Next poll
 ├─ Auth manager: session valid? → yes (stale cookies still in memory)
 ├─ Resource loader: fetch pages with stale session
 │   ├─ Case A: modem rejects → LOAD_AUTH → clear session → same-poll retry
 │   └─ Case B: modem accepts (IP-based, or ignores stale cookies) → success
 ├─ Parser: channels found (Case B)
 └─ Orchestrator: log transition with session state for diagnostics
```

If the stale session is rejected (Case A), `LOAD_AUTH` signal handling
clears the session and retries once in the same poll with a fresh
login, so a modem that is back up recovers without waiting for the
next cycle. No proactive cache clear is needed.

The orchestrator logs the `unreachable → online` transition. Session
state is already reported in the poll log (`session: new` vs
`session: reused`).

Optional recovery enhancements (may be added based on real-world data):

- Switch to fast polling during the outage window
- Wait for channel counts to stabilize before reporting `online`
- Notify the user ("Modem restarted externally")

These would make unplanned restart recovery more predictable — the
same two-phase pattern (wait for response, wait for channel sync) applies
regardless of who initiated the restart.

---

## Error Recovery

The orchestrator does not use a state machine. Each poll is an independent
pass through the linear pipeline (auth → load → parse) with a defined
policy response for every signal. No failure mode requires tracking state
beyond what is already specified (session, backoff counter, last status).

### Signal Catalog

Every signal a protocol layer can emit and the orchestrator's policy:

| Signal | Source | Orchestrator Policy | Status |
|--------|--------|-------------------|--------|
| `AuthResult.SUCCESS` | Auth Manager | Proceed to loading | `online` (if parse succeeds) |
| `AuthResult.FAILURE`, login answered 401/403/404 | Auth Manager | Trip circuit breaker immediately | `auth_failed` |
| `AuthResult.FAILURE`, login answered 5xx | Auth Manager | Abort poll, no auth streak, no breaker | `unreachable` |
| `LoginLockoutError` | Auth Manager | Trip circuit breaker immediately | `auth_failed` |
| `ConnectionError` on auth | Auth Manager (propagated) | Abort poll, connectivity backoff | `unreachable` |
| `Timeout` on auth | Auth Manager (propagated) | Abort poll, connectivity backoff | `unreachable` |
| Any page `Timeout` / `ConnectionError` | Resource Loader | Abort poll (all-or-nothing), connectivity backoff | `unreachable` |
| HTTP 401/403 on data page | Resource Loader | Clear session, increment auth streak | `auth_failed` |
| HTTP 5xx on data page | Resource Loader | Abort poll | `unreachable` |
| Channels found | Parser | Build response, reset auth streak | `online` |
| Zero channels, all expected anchors fulfilled | Parser | Derive status from system_info | `no_signal` |
| Zero channels, **0 of N** expected anchors fulfilled | Parser Coordinator | Clear session, increment auth streak (`LOAD_INTEGRITY`) | `auth_failed` |

### Diagnostics for Remote Troubleshooting

When a poll fails with `auth_failed` or `parser_issue`, the orchestrator
captures structured diagnostic data for the user to share in a bug report:

- **Auth diagnostics:** which strategy was used, HTTP status code,
  whether expected session artifacts (cookies, tokens, redirects) were
  present, response size
- **Loader diagnostics:** which page(s) were fetched, HTTP status per
  page, response sizes, whether any response resembles a login page
  (presence of password form elements, login-related titles)
- **Parser diagnostics:** which extraction step failed, what the parser
  expected vs. what it found, number of channels extracted (if partial)
- **Modem identity:** manufacturer, model, variant, firmware version
  (if known from system_info)

Expected-vs-found capture is not gated on poll failure. A successful
poll can still under-deliver fields parser.yaml maps — a firmware
variant that omits a field, or a value in an unhandled format. These
surface as system_info field outcomes (`PARSING_SPEC § Field
Outcomes`) in the same diagnostics download: missing fields as a
list, conversion failures with the raw value that needs a catalog
fix. Background: #98, where a silently absent uptime field was
indistinguishable from a malformed one without a contributor
round-trip.

This data is exposed via HA's integration diagnostics download (JSON).
The user downloads the file and attaches it to a GitHub issue — no
log scraping required.
