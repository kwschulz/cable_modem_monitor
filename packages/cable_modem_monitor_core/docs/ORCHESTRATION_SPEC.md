# Orchestration Specification

Interface contracts for the orchestration layer — the runtime components
that schedule, execute, and recover from poll cycles. These components
live in Core (`solentlabs.cable_modem_monitor_core`) and are
platform-agnostic. Clients (Home Assistant, CLI tools, containers) wrap
these components; they do not reimplement them.

**Relationship to other specs:**

- `RUNTIME_POLLING_SPEC.md` — behavior (what happens when)
- `ARCHITECTURE.md` — system design (how components fit together)
- This spec — contracts (method signatures, result types, signal types)

**Design principles:**

- Core's primary API is synchronous (`requests`-based I/O)
- Core logs and returns results; consumers present them
- Protocol layers signal conditions; the orchestrator owns all policy
- All state is memory-only (lost on process restart)
- The collector reports what it found; the orchestrator interprets it

---

## ModemDataCollector

Executes a single data collection: authenticate → load resources → parse →
post-parse filter → logout. Runs once, returns a result. Owns no
scheduling, retry, or backoff policy.

The collector's job is to accurately report what it found. Zero channels
is valid data — a modem without a cable connection still has a working
web interface. The orchestrator decides what zero channels *means*.

### Public API

```python
class ModemDataCollector:
    def __init__(
        self,
        modem_config: ModemConfig,
        parser_config: ParserConfig | None,
        post_processor: PostProcessor | None,
        base_url: str,
        username: str,
        password: str,
        *,
        legacy_ssl: bool = False,
    ) -> None:
        """Initialize collector with modem configuration.

        Creates the auth manager, resource loader, and parser coordinator
        from the provided config. The collector is reusable across polls —
        the auth manager maintains session state between calls.

        The HTTP session is created via ``create_session()`` from
        ``connectivity.py``, which sets ``verify=False`` (cable modems
        use self-signed certificates) and optionally mounts the
        ``LegacySSLAdapter`` for old firmware requiring SECLEVEL=0
        ciphers.

        The per-request HTTP timeout comes from modem_config.timeout
        (modem.yaml ``timeout`` field, default 10s). This applies to
        every HTTP request the collector makes — auth, resource loading,
        and logout. Slow modems override this in their modem.yaml.

        Args:
            modem_config: Parsed modem.yaml config. Includes timeout,
                auth strategy, session config, and action definitions.
            parser_config: Parsed parser.yaml config. None if parser.py
                handles all extraction.
            post_processor: Optional PostProcessor from parser.py.
            base_url: Modem URL (e.g., "http://192.168.100.1").
            username: Login credential.
            password: Login credential.
            legacy_ssl: Whether HTTPS requires legacy (SECLEVEL=0)
                ciphers. Discovered during setup by detect_protocol().
                Passed to create_session(). Defaults to False.
        """

    def execute(self) -> ModemResult:
        """Execute one data collection.

        Sequence:
        1. Auth Manager: validate session → reuse or authenticate
           (a fresh login also GETs each session.post_login_endpoints
            path, best-effort — see Post-login endpoints below)
        2. Resource Loader: fetch all resources (all-or-nothing)
        3. Parser: extract channels + system_info → ModemData
        4. Post-parse filter: apply restart-window filter if configured
        5. Logout: execute actions.logout if single-session modem

        Returns:
            ModemResult with modem data or failure signal.
        """

    @property
    def session_is_valid(self) -> bool:
        """Whether the Auth Manager believes the current session is usable.

        Strategy-specific local check: HNAP verifies uid cookie +
        private key (the private key is also set as a PrivateKey
        cookie); cookie-based strategies verify the session cookie
        (``auth.cookie_name``) is present; basic and none are always
        valid.

        This is a local check — the server may have expired the session
        even if this returns True. Used for diagnostics and by clients
        that want to inspect session state without triggering a poll.
        """

    def clear_session(self) -> None:
        """Invalidate the current session.

        Called by the orchestrator when it has external evidence that
        the session is dead: LOAD_AUTH signal (HTTP 401/403 on data
        page, or HNAP HTTP error on reused session) or connectivity
        transition (unreachable → responsive).
        """

    def attempt_logout_before_retry(self) -> None:
        """Best-effort logout before a same-poll auth retry on single-session firmware.

        Called by the orchestrator immediately before clear_session() when
        LOAD_AUTH or LOAD_INTEGRITY triggers a retry. Releases any active
        server-side session so the subsequent re-authentication can succeed.
        Does not inspect or clear session state — that is clear_session()'s
        responsibility. Failure is silently ignored; the retry proceeds
        regardless.

        No-op unless ``actions.logout`` is configured. With
        ``requires_session: false`` the call always proceeds — such endpoints
        clear a server-side session without credentials (confirmed on
        SB8200 v6). With ``requires_session: true`` the call is skipped when
        ``session_is_valid`` is False, because the credential the endpoint
        needs is already gone. The test is session validity, not cookie
        presence: header-authenticated strategies (``bearer``) hold a live
        session with an empty cookie jar.
        """
```

### Result Type

```python
@dataclass
class ModemResult:
    """Result of a single data collection attempt.

    success=True means the collection pipeline ran without errors.
    It does NOT mean channels were found — a modem without cable
    signal returns success=True with empty channel lists. The
    orchestrator interprets the data and derives connection status.

    Attributes:
        success: Whether the collection pipeline completed without error.
        modem_data: Parsed channel and system_info data. Present on
            success (may have empty channel lists). None on failure.
        signal: Failure classification when success is False.
            Always OK when success is True.
        error: Human-readable error detail for logging/diagnostics.
            Empty string on success.
        auth_status_code: HTTP status of the failed login response
            (AUTH_FAILED only, when the modem answered) — lets the
            circuit breaker message distinguish endpoint-not-found
            from credential rejection.
    """

    success: bool
    modem_data: ModemData | None = None
    signal: CollectorSignal = CollectorSignal.OK
    error: str = ""
    auth_status_code: int | None = None
```

### Signal Catalog

```python
class CollectorSignal(Enum):
    """Signals from a data collection attempt.

    These represent infrastructure failures — the collection pipeline
    could not complete. The orchestrator maps them to policy decisions.

    Notably absent: zero channels. Empty data is a valid collection
    result (signal=OK with empty channel lists), not a failure. The
    orchestrator interprets empty data using session state and modem
    context.
    """

    OK = "ok"                          # Collection completed — modem_data is populated
    AUTH_FAILED = "auth_failed"        # Wrong credentials or strategy mismatch
    AUTH_LOCKOUT = "auth_lockout"      # Firmware anti-brute-force triggered
    CONNECTIVITY = "connectivity"      # Connection refused, timeout, DNS failure
    LOAD_ERROR = "load_error"          # HTTP error on data page (5xx, 404)
    LOAD_AUTH = "load_auth"            # Session expired: HTTP 401/403, or HNAP HTTP error on reused session
    LOAD_INTEGRITY = "load_integrity"  # HTTP 200 stub: parser found 0 of N expected anchors (login-page detection false negative)
    PARSE_ERROR = "parse_error"        # Parser exception (malformed response)
```

### Signal → Policy Mapping (Orchestrator reference)

| Signal | Orchestrator Policy |
|--------|-------------------|
| `OK` | Reset auth streak, derive statuses, return `ModemSnapshot` (see § Connection Status Derivation) |
| `AUTH_FAILED` | Trip circuit breaker immediately, report `auth_failed` |
| `AUTH_LOCKOUT` | Trip circuit breaker immediately, report `auth_failed` |
| `CONNECTIVITY` | Abort, report `unreachable`, apply connectivity backoff |
| `LOAD_ERROR` | Abort, report `unreachable` |
| `LOAD_AUTH` | For single-session modems (`actions.logout` configured): attempt logout (best-effort; skipped if `requires_session: true` and the session is not valid) before clearing session. Then clear session, retry once in same poll, increment auth streak if retry fails, report `auth_failed` (see UC-17, UC-18) |
| `LOAD_INTEGRITY` | Same as `LOAD_AUTH` — for single-session modems, attempt logout (best-effort) before clearing session. Clear session, retry once in same poll, increment auth streak if retry fails, report `auth_failed` (see UC-19a) |
| `PARSE_ERROR` | Abort, report `parser_issue` |

### State Ownership

| State | Owner | Lifetime |
|-------|-------|----------|
| `requests.Session` (cookies, headers, verify=False) | Auth Manager (inside ModemDataCollector), created via `create_session(legacy_ssl=...)` | Until `clear_session()` or process exit |
| HNAP private key | Auth Manager | Until session cleared (also set as `PrivateKey` cookie) |
| URL token | Auth Manager | Until session cleared |
| Parser coordinator instance | ModemDataCollector | Collector lifetime (reused across polls) |
| `session_is_valid` check | Auth Manager (inside ModemDataCollector) | Evaluated on each `execute()` call |

### Logging Contract

ModemDataCollector logs detail at the point of failure *before*
classifying it into a signal. The signal is for the orchestrator to act
on. The log record is for humans troubleshooting. Both always happen.

**All log lines include `[MODEL]`** for multi-modem disambiguation —
model context is carried on each event dataclass and rendered by the
`log_event()` adapter. See
[LOGGING_SPEC.md](LOGGING_SPEC.md) for the event taxonomy and level
policy.

Example — HTTP 401 on a data page:

- **Log** (WARNING): `"HTTP 401 on /status.html [MODEL] — session likely expired"`
- **Signal**: `CollectorSignal.LOAD_AUTH`
- **ModemResult.error**: `"401 on /status.html — session likely expired"`

Example — HNAP 404 on reused session:

- **Log** (WARNING): `"HNAP HTTP 404 on reused session [MODEL] — session likely expired"`
- **Signal**: `CollectorSignal.LOAD_AUTH`
- **ModemResult.error**: `"HNAP HTTP 404 — session expired"`

Example — stub response (login-page detection false negative):

- **Log** (WARNING): `"Stub response on /status.html [MODEL] — 0 of 4 expected parser anchors found, treating as session integrity failure"`
- **Signal**: `CollectorSignal.LOAD_INTEGRITY`
- **ModemResult.error**: `"0 of 4 expected anchors on /status.html — stub response"`

Example — successful collection with no channels:

- **Log** (INFO): `"Parse complete [MODEL]: 0 DS, 0 US channels"`
- **Signal**: `CollectorSignal.OK`
- **ModemResult.modem_data**: `{downstream: [], upstream: [], system_info: {...}}`

**Log level tiers:**

| Tier | Level | When | Purpose |
|------|-------|------|---------|
| Pulse | INFO first poll, DEBUG after | Successful poll summaries | `"Parse complete [MODEL]: 24 DS, 4 US"` — visible at INFO for first-poll confirmation, then DEBUG in steady-state to keep success-path logs quiet |
| Auth/resource | INFO first poll, DEBUG after | Steady-state noise reduction | Auth strategy, session state, resource loading. Visible at INFO for first-poll diagnostics, drops to DEBUG after to avoid flooding multi-modem logs |
| Failures | WARNING/ERROR always | Never demoted | Auth failures, connectivity errors, parse errors. Always visible regardless of poll count |
| Wire data | DEBUG always | Troubleshooting only | Request/response details, parsing internals |

Status transitions and adaptive-reuse state changes stay at INFO even
after the first poll. These are operator-relevant events, not
steady-state heartbeat logs.

### Auth Log Level

Auth managers accept an optional `log_level` parameter (default
`logging.DEBUG`) used for their own internal narration during
`authenticate()` — which login URL they're hitting, nonce extraction,
session-cookie acceptance, etc. Callers leave the default in place
during normal polling and config-flow validation, keeping
steady-state logs quiet.

When auth fails, the collector emits a single sanitized ``WARNING``
log carrying the modem's response (see § Auth-Failure Detail Log
below). That line lands in HA's default log view without requiring
the user to enable DEBUG, which is the diagnostic surface a
maintainer needs when helping a stuck-setup user.

This mirrors the existing `log_level` pattern used by action execution
(both HTTP and HNAP actions). Auth managers use the level for all
non-error log calls during `authenticate()`. Errors and warnings are
always logged regardless of `log_level`.

### Post-login endpoints

`authenticate()` owns this, not `execute()`. On the fresh-login branch,
after a successful result and before returning, the collector GETs each
`session.post_login_endpoints` path in order. Responses are discarded.
The calls carry `session.query_params` like every other fetch.

Placing it in `authenticate()` rather than in `execute()`'s sequence is
what makes it reach the restart path: `restart.py` calls
`collector.authenticate()` and dispatches its action without ever
entering `execute()`. Firmware that requires the call to establish a
session requires it there too.

Best-effort by design. A non-2xx response or a transport error emits
`PostLoginFetchFailed` at WARNING and proceeds. Login already
succeeded, so failing the collection here would report bad credentials
for a working password; a genuinely required call surfaces anyway as
the data fetch's own 401 alongside this warning. Config surface and
evidence bar: MODEM_YAML_SPEC.md § Post-login endpoints.

### Auth-Failure Detail Log

When the collector's auth phase fails, it emits one sanitized
``WARNING`` log carrying the modem's response. That single line is
the diagnostic surface for stuck-setup users — it lands in HA's
default log view without requiring DEBUG to be enabled, which is
what shortens the round-trip on issues like #86, #104, #120.

```text
Auth failed [MODEL] strategy=form
  request: POST http://192.168.100.1/login?<redacted>
  response: 401 text/html
  body: <truncated 500-char snippet, with the user's password replaced by [REDACTED]>
```

The log fires from the collector's existing failure path, so
initial setup, reauth, options-flow re-validation, and steady-state
polling all surface the same detail. The auth circuit breaker
bounds volume during persistent failure.

**Sanitization** is local to the collector and minimal:

- The user's literal password is replaced with ``[REDACTED]`` if it
  appears in the body snippet.
- URL query strings are stripped wholesale (``?<redacted>``) — some
  strategies (Arris ``url_token``) put credentials in the query.
- Body snippet is truncated to 500 characters.

Derived credential forms (PBKDF2 hashes, encrypted blobs) are left
intact — they're protocol-shaped, not the user's secret, and the
maintainer needs them to confirm the strategy ran.

Auth managers must include the ``requests.Response`` on their
failure ``AuthResult`` so the collector can render the detail.

See ARCHITECTURE_DECISIONS.md § "Auth-failure detail via single
WARNING log" for the design rationale (replaces an earlier session-
adapter capture mechanism that was over-engineered for the goal).

#### AuthResult Reuse Contract (success path)

On the **success** path, ``AuthResult.response`` and
``AuthResult.response_url`` advertise an auth-response-reuse
opportunity to the loader. The loader decodes ``response.text`` as
the data page for ``response_url`` and skips re-fetching that path.

These fields MUST be set only when the login response body is itself
a parser-consumable data page for ``response_url``. Strategies that
return opaque artefacts (session tokens in the body, empty bodies,
non-data-page redirect landings) MUST leave both fields unset.
Violating this contract surfaces the auth artefact as the parsed
data page and silently produces empty results.

The contract is canonically documented in the ``AuthResult``
docstring (``auth/base.py``) and replicated in
``RESOURCE_LOADING_SPEC.md`` § Auth Response Reuse. Each auth
strategy unit-tests both branches (positive: data-page advertises
reuse; negative: artefact branches do not). Adding a new auth
strategy requires both tests. Regression: SB8200 #81.

### Exceptions

ModemDataCollector does **not** raise exceptions to the orchestrator. All
failure modes are captured in `ModemResult.signal`. Internally, the
collector catches exceptions from auth, loader, and parser, logs them,
classifies them into the appropriate signal, and returns a result.

The one exception: `LoginLockoutError` is raised by HNAP auth strategies
when firmware anti-brute-force triggers. ModemDataCollector catches it and
returns `CollectorSignal.AUTH_LOCKOUT`. The orchestrator never sees the
exception directly.

---

## Component Factory

`orchestration.factory` owns the YAML-to-running-components path.
Consumers supply *what* (loaded configs, credentials, protocol
settings), Core handles *how* (credential encoding, collector
creation, health monitor assembly, identity extraction).

```python
def apply_credential_encoding(
    modem_config, credential_encoding="plain", credential_field="",
) -> None:
    """Inject form_nonce encoding. No-op for other strategies."""

def create_collector(
    modem_config, parser_config, post_processor,
    base_url, username="", password="", *, legacy_ssl=False,
) -> ModemDataCollector:
    """Single-shot collector. Used by all HA config-flow paths
    (initial setup, reauth, options-flow re-validation) and by the
    test harness."""

def create_orchestrator(
    modem_config, parser_config, post_processor,
    base_url, username="", password="", *, legacy_ssl=False,
    supports_icmp=True, supports_head=True, http_probe=True,
    model="",
) -> tuple[Orchestrator, HealthMonitor | None, ModemIdentity]:
    """Full orchestration graph for runtime polling."""
```

**Consumers:**

- **HA adapter:** Loads configs from catalog (HA-specific path),
  resolves health probe defaults vs config entry overrides, calls
  `create_orchestrator()`. Config loading stays in the adapter;
  assembly delegates to Core.
- **Config flow:** Calls `create_collector(...).execute()` for all
  validation paths (initial setup, reauth, options re-validation).
  Auth-failure detail is logged from the collector — no per-flow
  variation. See § Auth-Failure Detail Log.
- **Test harness:** Calls `create_orchestrator()` with
  `supports_icmp=False, http_probe=False` (no real modem to probe).

---

## Orchestrator

Policy engine. Coordinates ModemDataCollector and HealthMonitor.
Exposes the restart action (see § Restart Action) and surfaces
recovery state (see § Recovery). Owns all backoff and policy
decisions. Interprets collection results using session state and
modem context. Exposes a synchronous API — consumers wrap it for
their platform's scheduling model (HA's DataUpdateCoordinator, a CLI
loop, etc.).

The orchestrator does not own scheduling or threads. Consumers call
`get_modem_data()` when they want data and `ping()` on the health
monitor when they want a health check. The orchestrator applies the
same backoff and lockout protection regardless of why it was called.

See `ORCHESTRATION_USE_CASES.md` for end-to-end scenario walkthroughs
and test case derivations.

### Scheduling and Manual Triggers

Consumers own scheduling for both data collection and health checks.
Both can also be triggered manually from the UI:

| Operation | Scheduled | Manual trigger | API call |
|-----------|-----------|---------------|----------|
| Data collection | Consumer timer (e.g., 10m) | "Update" button in HA | `orchestrator.get_modem_data()` |
| Health check | Consumer timer (e.g., 30s) | Included in "Update" button refresh | `health_monitor.ping()` |
| Restart | N/A | "Restart Modem" button in HA | `orchestrator.restart()` |

The API methods are identical for scheduled and manual calls — the
orchestrator doesn't know or care why it was called. Backoff and
circuit breaker protection apply equally to both. `restart()` is a
one-shot command; the post-reboot window is handled generically by
the recovery module (§ Recovery).

**Interval limits:**

| Interval | Min | Max | Default | Rationale |
|----------|-----|-----|---------|-----------|
| Data collection | 30s | 24h | 10m | Min protects HNAP modems from anti-brute-force. Max prevents excessively stale data. |
| Health check | 10s | 24h | 30s | Min avoids wasteful probing. Max matches data collection. Both intervals are independently configurable or disabled — see HA_ADAPTER_SPEC Polling Modes. |

The consumer enforces these limits in its configuration UI. Core
does not validate intervals — it processes one call at a time
whenever the consumer calls it.

### Public API

```python
class Orchestrator:
    def __init__(
        self,
        collector: ModemDataCollector,
        health_monitor: HealthMonitor | None,
        modem_config: ModemConfig,
    ) -> None:
        """Initialize orchestrator with its components.

        Args:
            collector: ModemDataCollector instance (reused across polls).
            health_monitor: Optional health probe monitor. None if the
                modem doesn't support ICMP or HTTP HEAD probes.
            modem_config: Parsed modem.yaml config. Used for
                identity (model) and actions (logout, restart).
        """

    def get_modem_data(self) -> ModemSnapshot:
        """Execute a data collection cycle.

        Sequence:
        1. Check circuit breaker — if open, return AUTH_FAILED
        2. Health recovery check — if connectivity backoff is active
           and HealthMonitor reports a data-path-up status
           (``HealthStatus.data_path_up``: RESPONSIVE or ICMP_BLOCKED,
           the latter proven by a live TCP pass since UC-59a), clear
           the backoff (modem is proven reachable, no reason to keep
           skipping)
        3. Check connectivity backoff — decrement counter. If still > 0
           after decrement, return UNREACHABLE. If counter reached 0,
           backoff is cleared and collection proceeds.
        4. Signal HealthMonitor: record_collection_start()
        5. Run ModemDataCollector
        6. Signal HealthMonitor: record_collection_end(success)
        7. If collection failed, apply signal → policy mapping
           (connectivity failures may trigger a recovery window)
        8. On success, derive connection_status from data
        9. Enrich system_info.docsis_status if absent (from lock_status)
        10. Hand the snapshot to the recovery module
            (runs the reboot-signal check, may enter or exit a window)
        11. Read latest HealthInfo from HealthMonitor (if present)
        12. Detect state transitions (unreachable → online)
        13. Return combined result

        Polls always run the full cycle and return whatever the modem
        reports. There is no short-circuit for in-flight restarts —
        restart is a one-shot command, not a stateful procedure. If a
        scheduled poll happens to land while the modem is rebooting,
        it surfaces UNREACHABLE honestly and the recovery module
        accelerates subsequent polls until the modem answers again.

        Steps 4 and 6 notify the HealthMonitor of collection activity
        so it can skip the HTTP probe when a collection is active or
        recently succeeded (see HealthMonitor § Collection Evidence).
        The end signal is in a finally block — it always runs.

        The HealthMonitor runs on its own cadence. The orchestrator
        reads the latest health result for the snapshot (step 11) and
        for backoff decisions (step 2), but does not trigger a new
        probe.

        The caller decides when to poll (schedule, service call,
        automation). The orchestrator applies the same backoff and
        lockout protection regardless. The recovery module may
        switch the consumer's poll cadence via the recovery signal
        (see § Recovery).

        Returns:
            ModemSnapshot with modem data, health info, and derived
            status fields.
        """

    def restart(self) -> RestartResult:
        """Send the restart command. One-shot; does not wait.

        Restart is a user-initiated action — it bypasses the auth
        circuit breaker. If the circuit is open due to bad credentials,
        the user can still restart the modem. Auth failure during
        restart does NOT increment the orchestrator's auth failure
        streak.

        Procedure (see § Restart Action for the full contract):

        1. Authenticate against the modem.
        2. Execute the ``actions.restart`` executor.
        3. Clear the collector session (forces fresh auth on the
           next poll — some firmware invalidates sessions after a
           reboot command).
        4. Trigger the recovery module so subsequent polls run at
           recovery cadence (see § Recovery).
        5. Return.

        The call returns as soon as the command has been dispatched
        — typically in a few seconds. It does NOT observe the reboot
        itself, does NOT poll until the modem comes back, and does
        NOT report whether the modem actually restarted. The dashboard
        reflects reality through normal polling at recovery cadence:
        UNREACHABLE while the modem is down, transitional docsis
        states while it ranges, Operational once it's back.

        Callable at any time. If the modem is already in a recovery
        window or is still rebooting from a prior restart, the call
        proceeds normally — authentication may fail (returning
        ``error="command_failed"``), or the command may dispatch and
        re-trigger the modem's reboot sequence. That's the caller's
        intent; Core does not arbitrate. Consumers serialize
        concurrent presses via their own button mutex (see
        HA_ADAPTER_SPEC § Operation Mutex).

        Only available if modem.yaml declares ``actions.restart``.

        Returns:
            RestartResult with success flag (True iff the command
            dispatched cleanly) and an error token.

        Raises:
            RestartNotSupportedError: If modem has no restart action.
        """

    def reset_connectivity(self) -> None:
        """Reset connectivity backoff for immediate retry.

        Called when the user requests a manual refresh (Update Modem
        Data button). Clears connectivity streak and backoff so the
        next get_modem_data() attempts a real connection regardless
        of prior connectivity failures. Deliberately does NOT clear
        the auth circuit breaker — see § Auth Circuit Breaker,
        "No manual bypass."
        """

    def close(self) -> None:
        """Release held resources — log out any live session, close the HTTP session.

        Called by the consumer when discarding the orchestrator (e.g.
        HA entry reload). If a session is still live (a session-reuse
        modem at reload), best-effort logs it out first so single-session
        firmware isn't left holding a lock; the logout is guarded (skipped
        when no session), suppressed, and bounded by the action timeout so
        teardown can't hang on an unreachable modem. Then releases the
        session's pooled sockets rather than leaving them to GC.
        Per-poll-logout modems have already cleared their session, so the
        logout is a no-op for them. The orchestrator is single-use after
        close().
        """

    def diagnostics(self) -> OrchestratorDiagnostics:
        """Return a read-only snapshot of operational diagnostics.

        No side effects — safe to call at any time, including when the
        circuit breaker is open or a restart is in progress.

        Returns:
            OrchestratorDiagnostics with current operational state.
        """

    @property
    def status(self) -> ConnectionStatus:
        """Current connection status from the last get_modem_data() call.

        Clients read this for display without triggering a new collection.
        """

    @property
    def recovery_active(self) -> bool:
        """Whether a recovery window is currently open.

        True while the recovery module is running an aggressive-poll
        window. Set by any of:

        - ``restart()`` after the command dispatches,
        - connectivity backoff engaging on observed poll failures,
        - the reboot-signal check matching a successful poll (see
          § Reboot-Signal Trigger).

        Consumers use this to switch the data poll cadence to the
        recovery interval (HA uses the ``recovery_state_signal``
        dispatcher to react promptly — see HA_ADAPTER_SPEC
        § Recovery Adapter).

        Thread-safe: boolean read is atomic under the GIL.
        """

    def set_recovery_observer(
        self, observer: Callable[[], None] | None
    ) -> None:
        """Register a callback fired when ``recovery_active`` flips.

        Invoked from the poll thread on both the False→True and
        True→False transitions. The callback must be thread-safe —
        HA implementations typically hop to the event loop via
        ``dispatcher_send`` (which internally calls
        ``call_soon_threadsafe``).

        Core doesn't know or care what the observer does; it just
        invokes the callable. This keeps the Core→HA coupling one-
        directional (Core signals state, HA decides UX response).
        """

    @property
    def supports_restart(self) -> bool:
        """Whether modem.yaml declares ``actions.restart``.

        False means ``restart()`` will raise
        ``RestartNotSupportedError``.
        """
```

### Data Models

```python
@dataclass
class ModemIdentity:
    """Static modem metadata from modem.yaml.

    Populated once at config load time. Consumers use this for
    display and device registration. The model field comes from
    the modem.yaml config — if the modem reports a different model
    in system_info at runtime, that value is available in
    modem_data.system_info and takes precedence for display.

    Attributes:
        manufacturer: Modem manufacturer (e.g., "Arris", "Netgear").
        model: Model name from modem.yaml (e.g., "SB8200").
        docsis_version: DOCSIS version (e.g., "3.1"). None if unknown.
        release_date: Release date string (e.g., "2020"). None if unknown.
        status: Verification status — "confirmed", "awaiting_verification",
            or "unsupported".
    """

    manufacturer: str
    model: str
    docsis_version: str | None = None
    release_date: str | None = None
    status: str = "awaiting_verification"


@dataclass
class ModemSummary:
    """Lightweight modem summary for catalog browsing.

    Returned by list_modems(). Contains enough for config flow
    display and filtering — not the full modem config.

    Attributes:
        manufacturer: Modem manufacturer.
        model: Model name.
        model_aliases: Alternative model names (e.g., "Surfboard").
        brands: ISP brand names (e.g., "Xfinity").
        docsis_version: DOCSIS version string. None if unknown.
        status: Verification status.
        default_host: Default IP for this modem (e.g., "192.168.100.1").
        auth_strategy: Auth strategy of the default variant. For
            multi-variant modems, the config flow loads variant-specific
            auth strategy in Step 2.
        path: Filesystem path to the modem directory in the catalog.
    """

    manufacturer: str
    model: str
    model_aliases: list[str]
    brands: list[str]
    docsis_version: str | None
    status: str
    default_host: str
    auth_strategy: str
    path: Path
```

### Catalog Manager

```python
def list_modems(catalog_path: Path) -> list[ModemSummary]:
    """Walk the catalog and return summaries of all modems.

    Reads identity fields from each modem.yaml (and modem-{variant}.yaml)
    in the catalog directory. Returns a flat list suitable for config flow
    dropdowns, search, or filtering.

    The consumer owns the UI pattern — dropdowns, typeahead, flat list.
    This API returns everything; the consumer filters client-side.

    Args:
        catalog_path: Root of the catalog modems directory.

    Returns:
        List of ModemSummary, one per modem (default variant).
    """
```

### Result Types

```python
@dataclass
class ModemSnapshot:
    """Point-in-time snapshot of everything the orchestrator knows.

    This is the top-level result consumers receive from
    get_modem_data(). It combines the ModemDataCollector output,
    health probe output, and orchestrator-derived status fields
    into a single immutable structure.

    Attributes:
        connection_status: Derived from collector signal and data.
        docsis_status: Read from system_info["docsis_status"] (parser-
            provided or orchestrator-enriched from lock_status fields).
            Falls back to "unknown" when the field is absent.
        modem_data: Parsed channel and system_info data. None on
            collection failure. Present (possibly with empty channels)
            on successful collection. Channel counts and aggregate
            fields (e.g., total_corrected) are computed by the parser
            coordinator — consumers read them from system_info
            regardless of whether the modem reported them natively.
        health_info: Health probe results. None if no health monitor.
        collector_signal: Raw signal from the collector (for diagnostics).
        error: Human-readable error summary.
    """

    connection_status: ConnectionStatus
    docsis_status: str
    modem_data: ModemData | None = None
    health_info: HealthInfo | None = None
    collector_signal: CollectorSignal = CollectorSignal.OK
    error: str = ""


@dataclass
class ResourceFetch:
    """Timing and size for a single resource fetch.

    Captured by the ResourceLoader during data collection. One entry
    per resource page (e.g., /status.html, /connection.html).

    Resource fetch metrics are distinct from health probe latency:

    - Health probe latency (ICMP, TCP): baseline
      responsiveness — "how quickly does the modem respond to a
      lightweight request?"
    - Resource fetch timing: data page performance — "how long do
      actual data pages take to load?" Affected by content
      generation, CGI scripts, page size, firmware load.

    Consumers aggregate these however they need (min/max/avg for
    trend monitoring, per-resource for diagnostics).

    Attributes:
        path: Resource path (e.g., "/status.html").
        duration_ms: Fetch time in milliseconds.
        size_bytes: Response body size in bytes.
        status_code: HTTP response status code (e.g., 200, 401).
        content_type: Response Content-Type header value. Empty string
            when the header is absent or not applicable (e.g., HNAP
            batch responses where the Content-Type is always JSON).
    """

    path: str
    duration_ms: float
    size_bytes: int
    status_code: int = 0
    content_type: str = ""


@dataclass
class OrchestratorDiagnostics:
    """Read-only snapshot of operational diagnostics.

    Returned by diagnostics(). No side effects. Safe to call at any time.

    Attributes:
        poll_duration: Wall-clock time of last get_modem_data() call
            in seconds. None if never polled.
        auth_failure_streak: Current consecutive auth-related failure
            count. 0 when healthy.
        circuit_breaker_open: Whether polling is stopped due to
            persistent auth failures.
        session_is_valid: Current session state from the collector.
        auth_strategy: Auth strategy name from modem config (e.g.,
            "form", "hnap", "none"). Empty string if unknown.
        connectivity_streak: Consecutive connectivity failures. 0 when
            reachable.
        connectivity_backoff_remaining: Polls to skip before next
            connection attempt. 0 when no backoff active.
        stale_session_recovery_streak: Consecutive recovered stale-
            session events. Increments when a LOAD_AUTH same-poll
            retry succeeds and resets on an intervening normal success
            or unrecovered failure.
        session_reuse_disabled: Whether the orchestrator has disabled
            cached-session reuse for the rest of this runtime after
            repeated consecutive stale-session recoveries.
        resource_fetches: Per-resource timing and size from the last
            successful collection. Empty list if never polled or
            collection failed before resource loading. Consumers
            compute aggregates (min/max/avg) from this raw data.
        last_poll_at: ISO 8601 wall-clock timestamp (UTC) of the last
            ``get_modem_data()`` call. None if never polled.
        last_stub_body: Response body snippets from the last
            LOAD_INTEGRITY event, keyed by resource path. Empty dict
            if no stub-page failure has occurred. Retained across
            successful polls so it is present in user-shared diagnostics
            downloads even after the modem recovers. Full body stored;
            no truncation (stub pages are small, and the full body is
            the diagnostic signal).
        system_info_fields_missing: Field names parser.yaml maps in
            system_info whose source key appeared in no configured
            source's response on the most recent completed parse.
            Snapshot semantics (recomputed per parse, like
            resource_fetches): a persistent catalog/firmware mismatch
            is always visible; a healed field clears.
            See PARSING_SPEC § Field Outcomes.
        system_info_fields_failed: Mapping of field name to the raw
            value (truncated) that type conversion rejected. Retained
            for the rest of the runtime once recorded (stub-body
            retention rationale: an intermittent failure must survive
            into a diagnostics download taken after a healthy poll).
            The raw value is the repair datum for fixing the catalog
            format string. Only fields parser.yaml explicitly maps are
            captured. Diagnostics-only; never feeds signals or policy.

    Note: auth-failure wire detail is not stored on this dataclass.
    The collector emits a single sanitized ``WARNING`` log when
    auth fails (see § Auth-Failure Detail Log), which is the
    diagnostic surface; no separate structured type is exposed.
    """

    poll_duration: float | None
    auth_failure_streak: int
    circuit_breaker_open: bool
    session_is_valid: bool
    auth_strategy: str = ""
    connectivity_streak: int = 0
    connectivity_backoff_remaining: int = 0
    stale_session_recovery_streak: int = 0
    session_reuse_disabled: bool = False
    resource_fetches: list[ResourceFetch] = field(default_factory=list)
    last_poll_at: str | None = None
    last_stub_body: dict[str, str] = field(default_factory=dict)
    system_info_fields_missing: list[str] = field(default_factory=list)
    system_info_fields_failed: dict[str, str] = field(default_factory=dict)


class ConnectionStatus(Enum):
    """Modem connection status derived from poll outcome.

    Every value is a direct observation from the poll — either data
    was received (ONLINE, NO_SIGNAL), or the modem rejected us
    (AUTH_FAILED), or the poll failed (UNREACHABLE), or parsing
    failed (PARSER_ISSUE). There is no speculative "Restarting"
    status; during a restart or other outage the snapshot honestly
    reports whatever the modem is reporting (typically UNREACHABLE
    during the reboot, then transitional docsis states as it ranges,
    then ONLINE).
    """

    ONLINE = "online"
    AUTH_FAILED = "auth_failed"
    PARSER_ISSUE = "parser_issue"
    UNREACHABLE = "unreachable"
    NO_SIGNAL = "no_signal"

# Note: The "Degraded" display state in the HA Status sensor cascade
# comes from HealthStatus.DEGRADED (ICMP responds, HTTP fails), which
# is a health probe signal. ConnectionStatus has no DEGRADED value.


class DocsisStatus(StrEnum):
    """Well-known DOCSIS status values.

    OPERATIONAL matches the canonical system_info value.
    """

    OPERATIONAL = "Operational"
    PARTIAL_LOCK = "partial_lock"
    NOT_LOCKED = "not_locked"
    UNKNOWN = "unknown"


@dataclass
class RestartResult:
    """Result of dispatching a modem restart command.

    Reflects only whether the command itself was delivered to the
    modem. Does NOT report whether the modem actually rebooted or
    came back — that would require observation after the fact, which
    ``restart()`` deliberately doesn't do. Consumers watch the
    ``ModemSnapshot`` stream through normal polling to see what
    actually happened.

    Attributes:
        success: True iff authentication succeeded, the action
            executor ran, and the session was cleared without raising.
            False on any failure during the command dispatch itself.
        elapsed_seconds: Wall time of the ``restart()`` call. Typically
            a few seconds (auth + POST + session clear).
        error: Structured error token. Empty on success. On failure:

            * ``"command_failed"`` — authentication raised, the action
              executor raised, or the session clear raised.

            No other error tokens are emitted. ``restart()`` does not
            time out, cannot be cancelled, and does not observe the
            reboot.
    """

    success: bool
    elapsed_seconds: float
    error: str = ""
```

### Collection Flow

The `get_modem_data()` method runs the collector and derives status
from the result. The orchestrator deals with signal policy and status
derivation — no retry logic.

```python
def get_modem_data(self) -> ModemSnapshot:
    if self._circuit_open:
        return ModemSnapshot(connection_status=ConnectionStatus.AUTH_FAILED, ...)

    # Health recovery — clear connectivity backoff if proven reachable
    # (data_path_up: RESPONSIVE or ICMP_BLOCKED — live TCP pass per UC-59a)
    if (self._health_monitor and self._connectivity_backoff > 0
            and self._health_monitor.latest.health_status.data_path_up):
        self._policy.reset_connectivity()

    # Connectivity backoff — decrement; skip only while counter > 0
    if self._connectivity_backoff > 0:
        self._connectivity_backoff -= 1
        if self._connectivity_backoff > 0:
            return ModemSnapshot(connection_status=ConnectionStatus.UNREACHABLE, ...)
        # Counter reached 0 — backoff cleared, proceed to collect

    result = self._collector.execute()

    if not result.success:
        status = self._apply_signal_policy(result)
        # Policy may ask the recovery module to enter a window
        # (e.g., connectivity backoff engaged).
        self._recovery.evaluate_failure(result)
        return ModemSnapshot(connection_status=status, ...)

    # Success — reset auth failure streak
    self._auth_failure_streak = 0

    status = self._derive_connection_status(result.modem_data)
    self._enrich_docsis_status(result.modem_data)  # fills system_info if absent
    docsis_status = (result.modem_data or {}).get("system_info", {}).get("docsis_status", "unknown")

    # Hand the snapshot to the recovery module. It runs the reboot-
    # signal check (possibly entering a window), and — if a window is
    # already open — decides whether this snapshot indicates the
    # modem is back (it never exits the window early; the module
    # runs the window to completion and then restores normal
    # cadence).
    self._recovery.evaluate_snapshot(result.modem_data)

    health_info = self._health_monitor.latest if self._health_monitor else None
    # ... transition detection ...
    return ModemSnapshot(connection_status=status, docsis_status=docsis_status, health_info=health_info, ...)
```

### Connection Status Derivation

Pure mapping from channel data and system_info to connection status.
No side effects, no retry logic.

`system_info` is an opportunistic signal — like ICMP or HEAD, use it
when available, don't rely on it when absent. If `system_info` has
data, the parser is working and the zero-channel result is meaningful.
If `system_info` is empty, the result is ambiguous (parser may not
extract system_info, or may have matched nothing).

```python
def _derive_connection_status(self, modem_data: ModemData) -> ConnectionStatus:
    """Derive connection status from a successful collection.

    Called when result.success is True. Maps channel data and
    system_info to a ConnectionStatus value.
    """
    has_channels = (
        len(modem_data["downstream"]) > 0
        or len(modem_data["upstream"]) > 0
    )

    if has_channels:
        return ConnectionStatus.ONLINE

    # Zero channels — use system_info as a parser health signal.
    system_info = modem_data.get("system_info", {})

    if system_info:
        # Parser extracted system_info — it's working. Zero channels
        # means the modem genuinely has no signal.
        return ConnectionStatus.NO_SIGNAL

    # system_info is empty AND zero channels. Ambiguous — could be
    # no signal on a parser that doesn't extract system_info, or
    # a parser mismatch where nothing matched at all. Default to
    # no_signal but log a diagnostic warning suggesting the user
    # verify their modem model configuration.
    logger.warning(
        "Zero channels and no system_info — cannot confirm parser "
        "health. Verify modem model matches the configured parser."
    )
    return ConnectionStatus.NO_SIGNAL
```

### Signal → Policy Implementation

```python
def _apply_signal_policy(self, result: ModemResult) -> ConnectionStatus:
    """Map a collector failure signal to connection status with side effects.

    Called only when result.success is False. Successful results go
    through _derive_connection_status() instead.

    Non-connectivity failures clear connectivity backoff — any response
    from the modem proves the network path works.
    """
    # Any non-connectivity failure means the modem responded
    if result.signal != CollectorSignal.CONNECTIVITY:
        self._connectivity_streak = 0
        self._connectivity_backoff = 0

    match result.signal:
        case CollectorSignal.AUTH_FAILED:
            self._auth_failure_streak += 1
            self._circuit_open = True  # immediate — credentials rejected
            return ConnectionStatus.AUTH_FAILED

        case CollectorSignal.AUTH_LOCKOUT:
            self._auth_failure_streak += 1
            self._circuit_open = True  # immediate — firmware anti-brute-force
            return ConnectionStatus.AUTH_FAILED

        case CollectorSignal.CONNECTIVITY:
            self._connectivity_streak += 1
            self._connectivity_backoff = min(
                2 ** (self._connectivity_streak - 1),
                self._max_connectivity_backoff,
            )
            return ConnectionStatus.UNREACHABLE

        case CollectorSignal.LOAD_ERROR:
            return ConnectionStatus.UNREACHABLE

        case CollectorSignal.LOAD_AUTH:
            # Data page returned 401/403. Auth didn't grant data
            # access — wrong strategy, stale session, or firmware
            # quirk. Clear session so the next poll starts fresh.
            # No retry: HA-triggered reboots are handled by restart(),
            # config issues need user intervention.
            self._auth_failure_streak += 1
            self._collector.clear_session()
            if self._auth_failure_streak >= self.AUTH_FAILURE_THRESHOLD:
                self._circuit_open = True
            return ConnectionStatus.AUTH_FAILED

        case CollectorSignal.PARSE_ERROR:
            return ConnectionStatus.PARSER_ISSUE
```

### Auth Circuit Breaker

Prevents login attempts with known-bad credentials. Without this,
wrong credentials cause repeated login failures — on HNAP modems
with `REBOOT` anti-brute-force, even a single extra attempt can
cause a device restart.

The circuit breaker has two trip modes:

- **Immediate** (AUTH_FAILED, AUTH_LOCKOUT): The modem explicitly
  rejected credentials. Retrying with the same password is pointless.
  Circuit trips on the first occurrence. One attempt, stop.
- **Threshold** (LOAD_AUTH): Session issue, not credential rejection.
  Data page returned 401/403 after successful login. Self-corrects
  when session is refreshed (UC-18). Circuit trips only after
  `AUTH_FAILURE_THRESHOLD` (default: 6) consecutive failures.

The streak counter resets to 0 on any successful collection.

```python
AUTH_FAILURE_THRESHOLD: int = 6  # applies to LOAD_AUTH only
```

**Recovery paths.** An open circuit blocks all polling indefinitely;
it never times out. It clears only by orchestrator reconstruction:
credentials are constructor-bound (there is no setter), so credential
reconfiguration always means the consumer rebuilds the orchestrator —
an HA config-entry reload after the reauth flow, or a client
restart. A fresh orchestrator starts with a closed circuit and every
auth-related field at its constructor default.

**No manual bypass — a considered decision, not an omission.**
Manual refresh (`reset_connectivity()`, the HA Update Modem Data
button, the `request_refresh` service) deliberately cannot clear the
breaker. The refresh path is an automation surface — HA users wire
`request_refresh` into automations ("ping fails → refresh") — so a
breaker bypass there would let a retry loop post known-bad
credentials at automation frequency, defeating the breaker's
purpose (on HNAP firmware with `REBOOT` anti-brute-force, each
attempt can restart the modem). The known trip causes that
self-resolve without a credential fix (dev instance started on the
wrong network, modem web UI temporarily down) recover via reload,
which is acceptable for their frequency. Revisit only with field
evidence of latched breakers that a reload cannot reasonably cover.

**Trip reason is preserved.** The policy records the HTTP status
that tripped the breaker (`circuit_trip_status_code`). Every
subsequent blocked poll logs advice matching the trip cause: a 404
trip says "login endpoint not found — reload the integration to
retry," not "reconfigure credentials."

#### Use Cases

**Password changed after months of success (UC-87):**
The modem works fine, then the user changes the password on the
modem's web UI (or ISP firmware resets it). Session reuse continues
until the session expires. Then:

```text
Poll N:   session expired → Auth Manager re-authenticates → wrong password
          → AUTH_FAILED → circuit OPEN, polling stops
Poll N+1: circuit blocks — no collection
```

**Root cause detection via logs:**

```text
INFO  Poll N: "Auth failed — wrong credentials or strategy mismatch (streak: 1)"
ERROR Poll N: "Auth circuit breaker OPEN — credentials rejected. Polling stopped. Reconfigure credentials to resume."
```

**Firmware changes auth mechanism:**
Same pattern as password change. The auth strategy in modem.yaml no
longer matches the modem's actual mechanism. AUTH_FAILED on first
attempt, circuit trips immediately. User sees error, opens a GitHub
issue or reconfigures.

**Transient auth failure:**
Modem is busy, temporarily rejects a login.

```text
Poll N:   AUTH_FAILED (streak: 1)
Poll N+1: collection succeeds (streak: 0) — circuit stays closed
```

Streak resets on success. Transient failures never reach the threshold.

**LOAD_AUTH on a data page:**
401/403 on a data page after auth appeared to succeed.

```text
Poll N:   LOAD_AUTH (streak: 1), session cleared
Poll N+1: fresh login → collection succeeds (streak: 0)
```

**LOAD_AUTH on a data page (single-session firmware):**
Single-session firmware enforces one active admin session. If the session was
not released (HA restart, crash, integration reload), the stale session blocks
new logins. The firmware's logout endpoint does not require authentication —
the browser itself erases the credential cookie before calling it.

```text
Poll N:   LOAD_AUTH (streak: 1), logout attempted (best-effort), session cleared
Poll N+1: fresh login → collection succeeds (streak: 0)
```

Applies when `actions.logout` is configured.
Logout is best-effort: failure does not block the retry and is not counted.

**LOAD_AUTH on HNAP (stale session):**
Server-side session expiry on HNAP modems with firmware session timeouts.
The firmware may return a non-standard HTTP code (404, 500) instead of 401.

```text
Poll N:   LOAD_AUTH (streak: 1), session cleared  [HNAP HTTP 404 on reused session]
Poll N+1: fresh login → collection succeeds (streak: 0)
```

If persistent (modem always returns errors on data pages), the streak
grows and the circuit trips — same as wrong credentials.

### State Ownership

| State | Purpose | Lifetime |
|-------|---------|----------|
| Login backoff counter | Anti-brute-force suppression | Decremented each get_modem_data(), cleared by orchestrator reconstruction |
| Auth failure streak | Circuit breaker — consecutive auth-related failures | Reset on successful collection, cleared by orchestrator reconstruction |
| Circuit open flag | Stops collection when streak reaches threshold | Set when tripped, cleared by orchestrator reconstruction |
| Circuit trip status code | Keeps blocked-poll advice matched to the trip cause (404 vs credentials) | Set on immediate trip, cleared by orchestrator reconstruction |
| Connectivity streak | Tracks consecutive CONNECTIVITY failures | Reset on success, non-connectivity failure, or reset_connectivity() |
| Connectivity backoff | Exponential backoff for unreachable modem: min(2^(streak-1), 6) | Decremented each get_modem_data(), cleared by reset_connectivity() |
| Recovery window state | Aggressive poll cadence, session preservation during the window | Owned by the recovery module; see § Recovery |
| Last connection status | Transition detection (unreachable → online) | Updated each get_modem_data() |
| Last poll timestamp | Metrics (poll duration, cadence tracking) | Updated each get_modem_data() |
| ModemDataCollector instance | Reuse session across polls | Orchestrator lifetime |
| HealthMonitor instance | Reuse probe config | Orchestrator lifetime |

### Derived Fields

The orchestrator computes these after a successful collection:

**Connection status** — from data interpretation (see above).

**DOCSIS status** — from downstream channel `lock_status` fields:

| Condition | Value |
|-----------|-------|
| All DS `lock_status == "locked"` AND upstream present | `operational` |
| Some DS locked | `partial_lock` |
| No DS locked | `not_locked` |
| No DS channels (no signal) | `not_locked` |
| No `lock_status` field on channels | `unknown` |

**Channel counts and aggregate `total_*` fields** are computed by the
parser coordinator, not the orchestrator. By the time the orchestrator
receives `modem_data`, `system_info` already contains channel counts
and any declared aggregate fields. See
[PARSING_SPEC.md](PARSING_SPEC.md#aggregate-derived-system_info-fields).

**Error rates (`rate_corrected`, `rate_uncorrected`)** — the
orchestrator computes per-minute error rates and writes them into
`modem_data.system_info` before snapshot construction. Elapsed time is
measured with `time.monotonic()` so a changed `scan_interval`, clock
skew, or paused VM cannot poison the value. These are the first
orchestrator-derived `system_info` fields and are distinct from
parser-coordinator-derived fields (aggregate, computed): those are
stateless single-poll transformations; rates are stateful inter-poll
computations.

**Each rate field is decided independently per counter.** There are
two ways a rate field can be reported on a given poll:

- **Zero floor.** When the current total is `0`, the rate is `0.0` by
  definition — no errors observed means no rate of errors. This is
  true regardless of poll number, baseline state, or clock state. A
  counter at zero produces a zero rate on the first poll, after a
  counter reset that lands at zero, or under any clock anomaly.
- **Inter-poll delta.** For a non-zero current total, the rate is
  `Δ count / Δ seconds × 60`. Requires a prior baseline and a
  positive monotonic elapsed time.

Otherwise the rate field is omitted from `system_info` (HA renders
`unknown`). The two counters can land in different states on the same
poll — for example, `total_corrected = 0` and `total_uncorrected = 5`
on the first poll produces `rate_corrected = 0.0` (zero floor) and
omits `rate_uncorrected` (no baseline yet).

| Field | When emitted | Value |
|-------|--------------|-------|
| `rate_corrected` | `total_corrected == 0` | `0.0` |
| `rate_corrected` | baseline + positive Δt + no reset on this counter | `Δ total_corrected / Δ seconds × 60` |
| `rate_uncorrected` | `total_uncorrected == 0` | `0.0` |
| `rate_uncorrected` | baseline + positive Δt + no reset on this counter | `Δ total_uncorrected / Δ seconds × 60` |

**Omission cases** (field absent from `system_info` for the
corresponding counter):

- First poll since orchestrator construction, with a non-zero
  current total (no prior baseline).
- A counter reset is detected on this poll — *either* total
  decreased, which marks the interval as spanning a modem reboot or
  stats wipe. Both counters omit the inter-poll delta; counters at
  zero post-reset still emit `0.0` via the zero floor.
- `Δ seconds <= 0` (defensive guard against clock skew or paused VM),
  and the current total is non-zero.
- The modem has no SC-QAM error counters at all
  (`total_corrected` / `total_uncorrected` absent from `system_info`).

Zero-delta counts (counter stable across an interval) produce `0.0`
via the inter-poll formula, matching the zero-floor result. Scope is
inherited from the aggregate totals: SC-QAM only. OFDM rates are out
of scope; see
[PARSING_SPEC.md § Aggregate](PARSING_SPEC.md#aggregate-derived-system_info-fields)
for the MIB-cited boundary rule.

### Logging Contract

Every orchestrator log line includes `[MODEL]` for multi-modem
disambiguation — model context is carried on event dataclasses and
rendered by `log_event()`. See [LOGGING_SPEC.md](LOGGING_SPEC.md).
Policy decisions are logged with enough context to identify root cause
without reading code. The auth failure streak count appears in every
auth-related log so the progression is visible in a linear log scan.

**Steady-state success-path logs are DEBUG.** All success-path
orchestration logs — auth, resource loading, session state, parse
completion — fire at INFO on the first poll and drop to DEBUG after.
The first poll lands first-install diagnostics in the default log
view without requiring DEBUG; subsequent polls go quiet to avoid
flooding multi-modem logs.

**Operator-relevant transitions stay at INFO** regardless of poll
count: status transitions, adaptive-reuse state changes, counter
resets, recovery events, modem reboots. These are not steady-state
heartbeat logs — they reflect changes the user benefits from seeing.

**Liveness confirmation without DEBUG:** users have two paths to
verify the integration is polling without enabling DEBUG logging:

1. **Integration UI** — Settings → Devices & Services → Cable Modem
   Monitor → Download Diagnostics. The JSON dump includes
   `last_poll_at`, streak counters, reuse state, and channel counts.
2. **Settings change** — temporarily enable DEBUG logging via
   Settings → System → Logs to surface per-poll detail.

**First-poll trigger** — the "first poll INFO" mode fires on:
fresh install, reconfigure or reauth (entry reload), and HA server
start/restart — every path is an orchestrator instance recreation.
Modem reboot is not in this list — it gets its own one-line
INFO event ("Counter reset detected …") and does not trigger verbose
first-poll output.

**Auth lifecycle:**

- INFO (first poll) / DEBUG (after): `"Poll [MODEL] — auth: FormAuth, url: ..., credentials: yes, session: none"`
- WARNING: `"Auth lockout [MODEL] — firmware anti-brute-force triggered, suppressing login for 3 polls (streak: 3/6)"`
- ERROR: `"Circuit breaker OPEN [MODEL] — polling stopped. Reconfigure credentials to resume."`
- ERROR (404 trip): `"Circuit breaker OPEN [MODEL] — login endpoint not found (HTTP 404). Polling stopped. Reload the integration to retry."`

**Backoff and circuit breaker:**

- INFO: `"Backoff active [MODEL] (2 remaining), skipping collection"`
- INFO: `"Backoff cleared [MODEL], resuming"`

**Collection outcomes:**

- INFO (first poll) / DEBUG (after): `"Parse complete [MODEL]: 24 DS, 4 US channels"`
- WARNING: `"Poll failed [MODEL] — signal: connectivity, error: ..."`
- INFO: `"Counter reset detected [MODEL] — corrected: 1000→0, uncorrected: 50→0"` (modem reboot — operator-relevant transition, never demoted)

**State transitions:**

- INFO: `"Status transition [MODEL]: unreachable → online"`

**Restart and Recovery:**

See § Restart Action and § Recovery for log lines. The restart
action logs the command dispatch only (one INFO line on success,
one ERROR on failure). The recovery module logs window
entry/exit and cadence transitions.

State transitions and policy decisions are the orchestrator's unique
contribution — these are the logs that tell the story of what happened
across polls.

---

## HealthMonitor

Lightweight health probe that runs alongside data collection.
Determines whether the modem is network-reachable without triggering
a full authentication and parse cycle. The probes serve the same
goal — check modem health with the minimum possible impact.

The orchestrator notifies the HealthMonitor when data collections
start and end. TCP and HEAD probes are skipped when a collection is
active (avoids contention on the modem's web server) or recently
succeeded (redundant — the collection already proved L4/HTTP
reachability). A failing ICMP probe overrides the skip for TCP —
see Collection Evidence § ICMP contradiction override below.

### Probe Strategy

Three independent probes, each testing a different layer of the
modem's stack:

| Probe | Layer | What it proves | Affects status? |
|-------|-------|---------------|-----------------|
| ICMP ping | Network (L3) | IP stack responds | Yes |
| TCP connect | Transport (L4) | TCP stack accepts connections | Yes |
| HTTP HEAD | Application | Web server responds without invoking the handler | No (latency-only) |

**Probe order:** ICMP first (if supported), then HEAD (if supported
and HTTP probe enabled), then TCP (if HTTP probe enabled). HEAD runs
before TCP so the modem's web server gets an uncontested connection
— embedded modems are often single-threaded and degrade if a TCP
probe is still being cleaned up.

**Status uses ICMP + TCP only.** HEAD timing is a latency-only
signal that populates `http_latency_ms` when available; HEAD failure
does not change status. Application-layer issues that don't break
TCP listening surface via the next slow-poll instead.

**TCP/HEAD timing split:** When both run, the HEAD probe records
total elapsed time (which includes its own TCP handshake because the
connection pool is always cold between probes). The dedicated TCP
probe measures the handshake separately. Subtracting yields the
modem's pure server response time, stored in `http_latency_ms`.

- `tcp_latency_ms` in `HealthInfo` is the dedicated TCP handshake
  measurement — the L4 reachability signal.
- `http_latency_ms` is the **server response time** (HEAD elapsed
  minus TCP handshake) — populated only when HEAD ran successfully.

**HEAD-or-skip, no GET fallback:** HEAD bypasses the CGI handler on
properly-implementing webservers, giving a clean unimodal latency
signal. When `supports_head=False`, the HEAD probe is **skipped
entirely** — a fallback to GET is intentionally avoided because GET
timing is bimodal on most embedded modems (cold compute path vs warm
cached path) and would corrupt the metric. GET-only modems still
get TCP latency for L4 reachability and ICMP for L3.

**The HEAD probe is a connectivity check, not a content check.**
Any response — 200, 302, 401 — means the modem's web server is
alive. Redirects are not followed (`allow_redirects=False`); a 3xx
is as valid a sign of life as a 200. The probe uses a pre-configured
session with `verify=False` and optional legacy SSL ciphers — the
same SSL knowledge that the setup flow discovered.

**ICMP availability:** Some networks block ICMP. When
`supports_icmp=False`, the ICMP probe is skipped entirely.

### Probe Discovery

`supports_icmp`, `supports_head`, and `legacy_ssl` are **discovered
during setup**, not user-configured. The consumer's setup flow (HA
config flow, CLI init) runs a connectivity check against the modem
and passes the results to the HealthMonitor constructor.

Discovery sequence (run once during setup):

1. **Protocol** — `detect_protocol()` TCP-probes ports 80 and 443
   and, when 443 is open, performs a TLS handshake using a
   broad-cipher (`SECLEVEL=0`) context. HTTPS is preferred whenever
   the handshake completes; `legacy_ssl` is set from the negotiated
   TLS version (TLSv1.1 or older → True). Determines `protocol` and
   `legacy_ssl` without sending any HTTP requests.
2. **ICMP** — ping the modem IP. Success → `supports_icmp=True`.
   Timeout or error → `supports_icmp=False` (network blocks ICMP).
3. **HTTP HEAD** — HEAD request to `base_url`. Normal response
   (2xx, 3xx) → `supports_head=True`. 405 Method Not Allowed or
   unexpected behavior → `supports_head=False` (modem rejects HEAD,
   so the HEAD probe is skipped at runtime — no GET fallback).

The user never sees or configures these flags. The setup flow
tests what works and passes the results through. The HealthMonitor
receives `legacy_ssl` so its HTTP probe session matches the SSL
configuration that worked during setup.

**Rediscovery:** Capabilities are stable — ICMP blocking is a
network characteristic, HEAD support is a firmware characteristic.
Neither changes between polls. Rediscovery only happens on
integration reconfiguration (HA options flow) or re-setup.

**Fragile modem override:** For modems where even GET health probes
carry risk (e.g., S33v2 firmware that crashes under HTTP load),
modem.yaml can declare `health.http_probe: false` to disable HTTP
health probes entirely. When disabled, only ICMP runs (if
supported). The health monitor does not generate its own HTTP
traffic between collections.

### Probe Configurations

Modem and network capabilities determine which probes are available.
Status derivation uses ICMP (L3) and TCP (L4) — the modem-load HEAD
signal is latency-only and does not affect status:

**ICMP + TCP (full visibility)**
Both reachability probes run. All four health states are
distinguishable. This is the default configuration.

| ICMP | TCP | Status | Meaning |
|------|-----|--------|---------|
| pass | pass | `responsive` | Modem is healthy |
| pass | fail | `degraded` | Network OK, modem L4 stack not accepting |
| fail | pass | `icmp_blocked` | Modem reachable at L4, network blocks ICMP |
| fail | fail | `unresponsive` | Modem is down |

**TCP only** (`supports_icmp=False`)
Network blocks ICMP. Only the L4 layer is tested. Cannot distinguish
"modem reachable but ICMP blocked" from "fully healthy" — but
that's fine, because ICMP is blocked on this network regardless of
modem state.

| ICMP | TCP | Status | Meaning |
|------|-----|--------|---------|
| N/A | pass | `responsive` | Modem L4 stack responds |
| N/A | fail | `unresponsive` | Modem L4 stack down — modem may be down |

Lost visibility: cannot detect `degraded` (ICMP would distinguish
"network reachable but TCP listen unhappy" from "completely down").

**ICMP only** (`http_probe=False`)
Modem.yaml sets `health.http_probe: false` for fragile modems where
even TCP/HEAD probes between collections carry risk (e.g., S33v2
firmware that crashes under additional HTTP load). All non-ICMP
probes are permanently disabled. Only ICMP runs between data
collections.

| ICMP | TCP | Status | Meaning |
|------|-----|--------|---------|
| pass | (disabled) | `responsive` | Network OK, modem reachable |
| fail | (disabled) | `unresponsive` | ICMP blocked, no L4 probe |

Lost visibility: if the collector fails AND ICMP is the only probe,
we can only detect network-layer outages. Application-layer health
comes from the next data collection attempt.

**Neither probe** (`supports_icmp=False` + `http_probe=False`)
Edge case — ICMP blocked by network AND HTTP probes disabled for
fragile modem. Health status is always UNKNOWN. The only health
signal comes from data collection outcomes. This configuration
should be rare — if ICMP is blocked and HTTP is disabled, the
health monitor provides no value and the consumer may choose not
to schedule health checks at all.

| ICMP | TCP | Status |
|------|-----|--------|
| N/A | N/A | `unknown` |

**HEAD latency signal availability** is orthogonal to status:
`http_latency_ms` is populated when `supports_head=True` AND the
HEAD probe ran successfully (no skip, HEAD didn't fail). On modems
with `supports_head=False`, HEAD is never run and `http_latency_ms`
is always None — by design, since the only way to populate it would
be a fallback to GET, which produces bimodal corrupted data.

### Public API

```python
class HealthMonitor:
    def __init__(
        self,
        base_url: str,
        supports_icmp: bool = True,
        supports_head: bool = True,
        http_probe: bool = True,
        legacy_ssl: bool = False,
        timeout: int = 5,
    ) -> None:
        """Initialize health monitor with probe configuration.

        The supports_icmp, supports_head, and legacy_ssl flags are
        discovered during setup (see Probe Discovery), not user-
        configured. The http_probe flag comes from modem.yaml for
        known-fragile modems.

        Creates a pre-configured requests.Session with verify=False
        and optional legacy SSL ciphers. The HTTP probe uses this
        session with allow_redirects=False — any response means the
        modem is alive.

        Args:
            base_url: Modem URL for HTTP probe (e.g., "http://192.168.100.1").
            supports_icmp: Whether ICMP ping works on this network.
                Discovered during setup — False if the network blocks
                ICMP.
            supports_head: Whether the modem handles HTTP HEAD correctly.
                Discovered during setup — False if the modem returns
                405 or unexpected responses to HEAD. When False, the
                HEAD probe is skipped entirely (no GET fallback —
                GET timing is bimodal and would corrupt the metric).
            http_probe: Whether to run HTTP health probes at all. When
                False, only ICMP probes run (if supported). Defaults
                to True. Set to False via modem.yaml health.http_probe
                for fragile modems where HTTP traffic between
                collections risks crashes.
            legacy_ssl: Whether HTTPS requires legacy (SECLEVEL=0)
                ciphers. Discovered during setup by detect_protocol().
                Passed through to create_session() for the HTTP probe.
            timeout: Per-probe timeout in seconds.
        """

    def record_collection_start(self) -> None:
        """Signal that a data collection cycle is starting.

        Called by the orchestrator before collector.execute().
        While active, ping() skips the TCP and HEAD probes to
        avoid contention on the modem's web server.
        """

    def record_collection_end(self, success: bool) -> None:
        """Signal that a data collection cycle has ended.

        Called by the orchestrator after collector.execute()
        completes (success or failure). A successful collection
        is recorded so the next ping() can skip the redundant
        TCP and HEAD probes.
        """

    def ping(self) -> HealthInfo:
        """Run health probes and return results.

        Runs enabled probes and returns a combined result:
        1. ICMP ping (if supports_icmp) — network-layer check
        2. HTTP HEAD (if supports_head AND http_probe AND no
           collection evidence suppresses it) — latency only
        3. TCP connect (if http_probe AND no collection evidence
           suppresses it) — L4 reachability check

        TCP and HEAD share the skip gate — both are skipped when a
        data collection is active or recently succeeded, since the
        collection already proves L4/HTTP reachability. When
        skipped, collection evidence substitutes for the TCP probe
        in status derivation but tcp_latency_ms / http_latency_ms
        stay None (not measured, not fabricated).

        Exception: a failing ICMP probe forces the TCP probe past
        the skip gate — stale collection evidence must not outvote
        a live probe. HEAD stays skipped. See Collection Evidence
        § ICMP contradiction override.

        Returns:
            HealthInfo with probe results and derived status.
        """

    @property
    def latest(self) -> HealthInfo:
        """Most recent health probe result.

        Returns the result from the last ping() call. The
        orchestrator reads this during get_modem_data() to include
        health data in ModemSnapshot without triggering a new probe.

        Returns a default HealthInfo(health_status=UNKNOWN) if
        ping() has never been called.
        """
```

### Result Type

```python
@dataclass
class HealthInfo:
    """Result of a health probe cycle.

    Only contains actual probe measurements. The HealthMonitor
    considers collection evidence internally when deriving
    health_status, but does not fabricate probe results — None
    means "not measured."

    Attributes:
        health_status: Derived status from ICMP + TCP results and
            collection evidence.
        icmp_latency_ms: Round-trip time in milliseconds. None if
            ICMP failed, not supported, or not attempted.
        tcp_latency_ms: TCP handshake time in milliseconds to the
            modem's web port. The L4 reachability signal. None if
            the TCP probe failed or was not attempted.
        http_latency_ms: HTTP server response time in milliseconds,
            excluding TCP connection setup overhead. Populated only
            on modems where supports_head=True (HEAD bypasses the
            handler and gives a clean unimodal signal). None on
            GET-only modems, HEAD failure, or when suppressed by
            collection evidence.
    """

    health_status: HealthStatus
    icmp_latency_ms: float | None = None
    tcp_latency_ms: float | None = None
    http_latency_ms: float | None = None


class HealthStatus(Enum):
    """Modem health derived from ICMP + TCP probe results."""

    RESPONSIVE = "responsive"        # ICMP and TCP both pass
    DEGRADED = "degraded"            # ICMP works, TCP fails (modem L4 stack issue)
    ICMP_BLOCKED = "icmp_blocked"    # TCP works, ICMP fails (network blocks ICMP)
    UNRESPONSIVE = "unresponsive"    # Neither responds (modem is down)
    UNKNOWN = "unknown"              # No probes enabled or run
```

### Status Derivation

See Probe Configurations above for the full derivation tables per
configuration. The general rule:

- **ICMP pass + TCP pass** → modem is up
- **ICMP pass + TCP fail** → modem reachable at L3 but L4 stack not accepting
- **ICMP fail + TCP pass** → modem reachable, network blocks ICMP
- **Both fail** → modem is down
- **N/A** (probe not run) → that layer isn't tested, derive from what's available

HEAD timing is **not** part of status derivation. HEAD failure on a
HEAD-capable modem is logged but does not degrade the status — slow
poll catches application-layer issues. This keeps the status signal
purely about reachability.

### Collection Evidence

A successful data collection is stronger evidence of modem liveness
than any health probe — it authenticates, fetches pages, and parses
data. The HealthMonitor uses this to avoid redundant or contentious
HTTP probes.

**Mechanism:** The orchestrator calls `record_collection_start()`
before the collector runs and `record_collection_end(success)` in
a finally block after it completes. The HealthMonitor tracks this
via two fields:

- `_collection_active` (bool) — True between start and end signals.
- `_last_collection_success` (monotonic timestamp) — set when a
  collection succeeds.

**TCP/HEAD probe suppression:** `ping()` skips both TCP and HEAD
probes when:

1. **Collection is active** — the modem is already handling HTTP
   traffic from the data poll. Running additional probes would
   compete for the modem's web server and produce misleadingly slow
   latency values.
2. **Collection succeeded since the previous `ping()`** — L4/HTTP
   reachability was already proven. The evidence is consumed once:
   the first `ping()` after a successful collection skips both TCP
   and HEAD; the next `ping()` runs them normally.

**Baseline guarantee:** The very first `ping()` call never skips
TCP/HEAD, regardless of collection state. Consumers (e.g., HA
sensor entities) need at least one real measurement to establish
baseline values.

**Status derivation with evidence:** When TCP/HEAD are skipped,
collection evidence substitutes as `tcp_ok=True` in the status
derivation matrix. The status reflects that the modem is L4-
reachable, but `tcp_latency_ms` and `http_latency_ms` stay `None`
(not measured).

**ICMP contradiction override (UC-59a):** Collection evidence is a
statement about the past — the modem was reachable when the poll
ran. A failing ICMP probe is a current observation that contradicts
it: the modem may have gone down since. When ICMP fails while the
skip gate is engaged, `ping()` runs the TCP probe anyway and feeds
its real result into status derivation instead of substituting
`tcp_ok=True`. This distinguishes a genuinely ICMP-filtered network
(`icmp_blocked`, confirmed by a live TCP pass) from an outage that
began just after a successful poll (`unresponsive`). Without the
override, any outage starting within one health interval of a
successful collection reports a false `icmp_blocked` — and logs a
spurious Status Activity line — until the evidence expires. HEAD
stays skipped: it is latency-only and cannot affect status. The
override applies to both skip reasons; it never bypasses the
config-level `http_probe=False` disable, and the forced TCP result
populates `tcp_latency_ms` when the connect succeeds.

| ICMP | TCP probe | Collection evidence | Status |
|------|-----------|-------------------|--------|
| pass | skipped | active or recent success | `responsive` |
| fail | forced — pass | active or recent success | `icmp_blocked` |
| fail | forced — fail | active or recent success | `unresponsive` |
| N/A | skipped | active or recent success | `responsive` |

**ICMP always runs.** It is lightweight (no web server involvement)
and provides network-layer visibility regardless of collection
activity.

**Logging:** The log detail shows ICMP, TCP, and HEAD timing
separately when probes run:

```text
Health check [MODEL]: responsive (ICMP 1.5ms, TCP 1.8ms, HTTP HEAD 4.3ms, 0 bytes)
```

On GET-only modems (`supports_head=False`), the HEAD entry is
omitted (only ICMP and TCP appear). When TCP/HEAD are skipped, the
log shows the skip reason:

```text
Health check [MODEL]: responsive (ICMP 1.5ms, TCP/HEAD skipped (collection active))
Health check [MODEL]: responsive (ICMP 1.5ms, TCP/HEAD skipped (recent collection))
```

`collection active` means the probes were skipped to avoid contention
during an in-progress collection. `recent collection` means a
collection succeeded since the last ping, making them redundant.

When the ICMP contradiction override forces the TCP probe, the log
shows the real TCP result alongside the skip reason:

```text
Health check [MODEL]: unresponsive (ICMP timeout, TCP timeout, HEAD skipped (recent collection; TCP forced by ICMP failure))
```

### State Ownership

The consumer is responsible for scheduling `ping()` calls at the
configured health check interval. No session, no cookies, no auth.
The orchestrator notifies the HealthMonitor of collection activity
via `record_collection_start()` / `record_collection_end()`.

| State | Purpose | Lifetime |
|-------|---------|----------|
| Last probe result | `latest` property — read by orchestrator during get_modem_data() | Updated each `ping()` call |
| supports_head | Gates whether the HEAD probe runs (no GET fallback) | HealthMonitor lifetime |
| Collection active | Suppresses TCP/HEAD probes during data poll | Between start/end signals |
| Last collection success | Suppresses TCP/HEAD probes after successful poll | Updated on successful collection end |
| Last ping time | Determines if collection evidence is fresh | Updated at end of each `ping()` call |

### Consumer Entity Guidance

HealthInfo fields are `None` when a probe is disabled or not
supported. Consumers should **not create UI elements for probes
that will never produce data**. A sensor that is permanently
None/Unknown is confusing and clutters the interface.

The consumer knows the probe configuration (it passed the flags to
the HealthMonitor constructor) and should use it to decide which
entities to register:

| Configuration | Entities to create |
|--------------|-------------------|
| ICMP + HTTP | ICMP latency, HTTP latency, health status |
| ICMP only (`http_probe=False`) | ICMP latency, health status |
| HTTP only (`supports_icmp=False`) | HTTP latency, health status |
| Neither | No health entities (or health_status=UNKNOWN only) |

**When no probes are useful:** Pass `health_monitor=None` to the
Orchestrator. `ModemSnapshot.health_info` will always be `None`,
and the consumer skips all health entities. This is cleaner than
creating a HealthMonitor that produces only UNKNOWN.

### Logging Contract

All health log lines include `[MODEL]`. Log levels are transition-
based — the same status at the same level would flood logs every 30s:

| Event | Level | Example |
|-------|-------|---------|
| Transition to responsive (recovery) | INFO | `"Health check [MODEL]: responsive (ICMP 3ms, TCP 2ms)"` |
| Transition to degraded | WARNING | `"Health check [MODEL]: degraded (ICMP 2ms, TCP timeout)"` |
| Transition to unresponsive | WARNING | `"Health check [MODEL]: unresponsive (ICMP timeout, TCP timeout)"` |
| TCP/HEAD skipped (collection evidence) | DEBUG | `"Health check [MODEL]: responsive (ICMP 1.5ms, TCP/HEAD skipped (collection active\|recent collection))"` |
| First check (UNKNOWN → any) | INFO or WARNING | Depending on the target status |
| Steady-state (no change) | DEBUG | Same format, but only visible with debug logging enabled |

This ensures a single WARNING on transition to a bad state, then
silence until recovery (INFO) or further degradation (WARNING).
Routine "still responsive" checks produce no visible output at
default log levels.

---

## Restart Action

`Orchestrator.restart()` is a one-shot command. It sends the reboot
instruction to the modem and returns. It does **not** wait for the
modem to come back, probe for liveness, or watch DOCSIS ranging.
Post-reboot polling cadence is handled generically by the recovery
module (§ Recovery).

The implementation lives in `orchestration/restart.py` as a thin
module-level function `run_restart(collector, modem_config,
recovery)`, called by `Orchestrator.restart()`.

Procedure:

1. Raise `RestartNotSupportedError` if `actions.restart` is None.
2. Authenticate against the modem — **unless** `actions.restart` is an
   `HttpAction` with `action_auth` set (per-action auth). When
   `action_auth` is present, `execute_action` creates a fresh session,
   authenticates, and executes the action on it;
   `collector.authenticate()` is skipped entirely.
3. Execute the `actions.restart` executor (`HTTP` or `HNAP` — see
   § Action Executors).
4. Clear the collector session (forces fresh auth on the next poll;
   avoids the MB7621-class stale-cookie failure mode).
5. Call `recovery.begin(reason="restart_command")` so subsequent
   polls run at recovery cadence. The call returns immediately; the
   recovery module owns what happens next.
6. Return a `RestartResult` — success iff steps 2–5 completed
   without raising.

**Per-action auth (`action_auth` on `HttpAction`):** when
`actions.restart.action_auth` is set, `execute_action` creates a
fresh `requests.Session`, authenticates using the configured strategy,
and executes the action on that temporary session. The collector's
monitoring session is not needed and `collector.authenticate()` (step
2) is skipped. Any auth strategy is valid in `action_auth`.

Typical call duration: 2–5 seconds (auth + POST + session clear).
The caller does not block on the reboot itself.

`restart()` does not refuse based on recovery state. A user who
sees a flakey modem after a restart may want to try again; Core
lets them. The command either dispatches (possibly re-rebooting
an already-rebooting modem, which is the caller's intent) or
fails cleanly with `error="command_failed"` if the modem isn't
reachable. Serialization of rapid button presses is the consumer's
responsibility — HA uses a short-lived mutex (see § Operation
Mutex in HA_ADAPTER_SPEC).

**Why the session clear:** some firmware (observed on MB7621)
invalidates the session during the reboot itself, but from our side
the session object still looks valid. The next poll would reuse a
dead cookie, get served a login page, and trip LOAD_AUTH. Clearing
here forces the post-restart poll to authenticate fresh. Small cost
(one extra auth), guaranteed-clean handoff.

**Bypasses the auth circuit breaker.** The user is asking for a
restart; blocking that because credentials were recently wrong
doesn't help them.

**UX gating is the consumer's responsibility.** Core provides no
gate. HA disables the restart button via its own `active_operation`
mutex for the duration of each press (seconds). It does NOT gate
on `recovery_active` — a user watching a flakey modem after a
restart is allowed to try again. See HA_ADAPTER_SPEC § Operation
Mutex.

### Logging Contract

Every line includes `[MODEL]`. Two lines total — the command is
one-shot.

- INFO: `"Restart command sent [MODEL] — session cleared (0.4s)"`
- ERROR: `"Restart command failed [MODEL]: <exc>"`

---

## Recovery

The modem is in **recovery** when it isn't operating normally and
we believe it may return. Recovery is purely a *polling-cadence
concern* — it adjusts how the consumer's poll cadence behaves for a
bounded window. It does **not** produce UX state, does **not**
short-circuit polls, and does **not** compete with the collector.

The `ModemSnapshot` always reflects what the modem actually reported
on the last poll (Unreachable, transitional docsis, Operational).
Consumers render snapshot truth, not a synthetic "Recovering" label.

Recovery lives in `orchestration/recovery.py` as a small module
wired into the orchestrator at construction. The orchestrator
delegates to it on every poll and on command dispatch; other modules
(collector, parser, signals, HA adapter) don't know it exists.

### Triggers

Three ways to enter a recovery window:

1. **Command dispatched** — `restart()` calls `recovery.begin(reason="restart_command")`
   after the command lands.
2. **Observed outage** — the orchestrator's connectivity policy
   engages (N consecutive poll failures, see § Signal → Policy). On
   engage, it calls `recovery.begin(reason="connectivity_outage")`.
3. **Reboot-signal check** — `recovery.evaluate_snapshot(modem_data)`
   runs on every successful poll and consults the 2-of-3 signal
   rule (see § Reboot-Signal Trigger). If the rule fires, it enters
   a window with `reason="reboot_signals:<matched>"`.

All three reach the same code path. The reason string is diagnostic
only; it affects log lines, not policy.

### Behavior during a window

While `recovery.active` is True:

- The recovery module fires its observer on the False→True
  transition so consumers can switch to the recovery cadence
  immediately (HA drops `coordinator.update_interval` to its
  recovery-adapter cadence; see HA_ADAPTER_SPEC § Recovery Adapter).
- Polls run normally — no short-circuit, no special guard. The
  orchestrator doesn't know it's in a recovery window when it
  returns a snapshot.
- The collector preserves its session across polls (implemented via
  `skip_logout=True` on `collector.execute()`). Rapid polling
  without logout + re-auth avoids hammering firmware anti-brute-force
  thresholds.
- If the session dies mid-window (LOAD_AUTH observed), the normal
  signal policy kicks in — session is cleared, next poll
  re-authenticates fresh. The window continues.

### Exit

The window runs to completion — it always lasts
`_RECOVERY_WINDOW_SECONDS`. Early exit was considered and rejected:
it would re-introduce inference ("this snapshot looks operational,
clear the window") and add no measurable benefit — a few extra fast
polls at the tail of a window cost nothing.

On expiry:

- Recovery fires its observer on the True→False transition so
  consumers can restore normal cadence.
- `recovery.active` returns False.
- The next poll runs at normal cadence.

The snapshot at that point is whatever the modem is currently
reporting — Operational, Unreachable, Denied, whatever. Consumers
render it honestly.

**Re-entry during a window** depends on the trigger path:

- **Explicit `begin()` call** (from `restart()`) — the window is
  re-started. The elapsed clock resets, the reason is updated, the
  observer fires again (False→True is a no-op on the
  already-True flag, but HA's listener is idempotent — it sets
  the cadence to the fast value either way). Rationale: a user
  who re-pressed restart wants a fresh observation window.
- **`evaluate_snapshot()` / `evaluate_failure()`** (internal
  triggers from the reboot-signal check and connectivity) — no-op while
  `active` is True. The running window's reason and timer are
  preserved. Rationale: automated signals shouldn't keep extending
  a window indefinitely.

### Timing

Recovery timing is **generic, not per-modem**. Bench-tuning per
modem was tried and removed; DOCSIS ranging variance is already
handled adaptively by polling through the window at a short
cadence.

| Constant | Default | Purpose |
|----------|---------|---------|
| `Recovery.WINDOW_SECONDS` | `180` | Duration of an aggressive-polling window. Covers the longest observed DOCSIS 3.1 ranging time with headroom. |

The constant is a class attribute on `Recovery` — matching the
`AUTH_FAILURE_THRESHOLD` pattern on `Orchestrator` and `SignalPolicy`
— so tests and future tuning have a named public handle without
reaching across a module-private boundary.

The inter-poll interval during a window is **not** a Core concern —
Core doesn't own scheduling. HA sets its coordinator's
`update_interval` to a fast cadence when the recovery observer
fires (see HA_ADAPTER_SPEC § Recovery Adapter); Core just exposes
`recovery_active` and fires the observer.

Future versions may expose the window duration as a user-configurable
option. Currently a class constant.

### Reboot-Signal Trigger

A simple 2-of-3 vote on signals from a successful poll. Implemented
as a private method `_check_reboot_signals(modem_data)` on the
`Recovery` class — not a separate module. The state it needs
(previous counter totals, previous uptime reading, previous docsis
status) lives on `Recovery` alongside the window state.

Signals:

- **Counter reset** — current `total_corrected` or `total_uncorrected`
  dropped below the previous poll's value.
- **Uptime drop** — modem-reported `system_uptime` decreased from
  the previous poll. Only evaluated when the modem exposes the
  field.
- **Transitional docsis** — `docsis_status` just *entered* a
  ranging-like state (Denied, not_locked, partial_lock) from a
  stable state (Operational, or the initial "unknown"). This signal
  is **edge-triggered**, not level-triggered: a modem chronically
  stuck in partial_lock fires the signal once on first observation
  and then stays quiet on subsequent polls. Level triggering was
  tried and rejected — it false-positived on chronically-unlocked
  modems when paired with a benign event like a user clearing error
  counters via the modem's web UI. Edge triggering aligns the
  signal with what actually accompanies a reboot (entry to a
  transitional state from stable), at the cost of missing reboots
  of modems that were already stuck and stay stuck through the
  reboot — an edge-of-edge case covered by the other two signals
  and by the commanded-restart and connectivity-outage paths.

Rule: if **two or more** signals are True, `evaluate_snapshot`
opens a recovery window with reason
`"reboot_signals:<matched_signals>"` (e.g.
`"reboot_signals:counter_reset+transitional_docsis"`). A single
signal does NOT trigger — false positives are easy from firmware
stats-clear commands, clock drift, signal issues, etc.

This is a threshold vote, not inference. No weights, no
probabilities, no judgment. Even if all three match and the modem
didn't actually reboot, the only consequence is polling faster for
`_RECOVERY_WINDOW_SECONDS`. No UX fiction, no misleading labels,
no coordinator timeouts.

Modems that don't expose the relevant fields simply don't trigger
via this path. That's fine — commanded restarts and observed
outages still enter recovery through their own paths.

### Public API

```python
class Recovery:
    def __init__(
        self,
        collector: ModemDataCollector,
        modem_config: ModemConfig,
        *,
        on_state_change: Callable[[], None] | None = None,
    ) -> None:
        """Initialize the recovery module.

        Args:
            collector: Used to pass ``skip_logout=True`` during window
                polls (the orchestrator controls the actual call; the
                recovery module only sets a flag the orchestrator
                reads).
            modem_config: Read for model name (logging) and for any
                future per-modem recovery opt-out.
            on_state_change: Callback fired on False→True and True→False
                transitions. Runs on the poll thread; callbacks must
                be thread-safe. None disables notification.
        """

    @property
    def active(self) -> bool:
        """True while a recovery window is open."""

    def begin(self, reason: str) -> None:
        """Open a recovery window, or re-open an existing one.

        Always (re)starts the window — elapsed clock resets, reason
        is updated. Called directly by ``restart()`` (the user
        pressed restart, they want fresh fast-poll coverage).
        Internal triggers (`evaluate_snapshot`, `evaluate_failure`)
        must NOT call this while ``active`` is True — they have
        their own no-op-when-active logic.

        Fires the state-change observer — on a False→True transition,
        and again on re-entry so HA's cadence listener can refresh
        the ``async_request_refresh()`` kick.

        Args:
            reason: Diagnostic tag (``"restart_command"``,
                ``"connectivity_outage"``, ``"reboot_signals:<matched>"``).
                Used in log lines only.
        """

    def evaluate_snapshot(self, modem_data: ModemData) -> None:
        """Run the reboot-signal check on a successful poll.

        Called by the orchestrator after every successful poll.
        Calls the private ``_check_reboot_signals`` (see § Reboot-
        Signal Trigger) against the snapshot and the accumulated
        history. Enters a window if the check returns a non-None
        reason and no window is already active.

        Always updates the history (previous counters, last uptime)
        regardless of trigger outcome so the next call has current
        baselines.
        """

    def evaluate_failure(self, result: ModemResult) -> None:
        """React to a failed poll.

        Called by the orchestrator on poll failures. Enters a window
        when the connectivity policy has just engaged (the orchestrator
        informs recovery; recovery does not read policy state
        directly). No-op on non-connectivity failures.
        """

    def tick(self) -> None:
        """Advance the window clock.

        Called by the orchestrator on every poll (before or after
        the poll itself — timing isn't load-bearing). Checks whether
        the window's deadline has passed; if so, clears state and
        fires the observer on the True→False transition.
        """
```

### Logging Contract

Every line includes `[MODEL]`. One line per state change; no
per-poll noise.

- INFO: `"Recovery window open [MODEL] — reason: restart_command"`
- INFO: `"Recovery window open [MODEL] — reason: reboot_signals:counter_reset+transitional_docsis"`
- INFO: `"Recovery window closed [MODEL] — elapsed: 182s, last snapshot docsis: Operational"` (the docsis value is the last one captured on a successful poll; during an outage that spanned the window, no successful polls means this value can be stale relative to window-close time)

### Scope Guardrails (UC sources)

| UC | Covers |
|----|--------|
| UC-40 | Restart button: command dispatches, returns quickly, recovery window opens |
| UC-42 | (retired) — Core no longer refuses based on recovery state; HA mutex serializes rapid button presses |
| UC-43 | `get_modem_data()` during a recovery window behaves normally — no short-circuit |
| UC-44 | Raises `RestartNotSupportedError` when `actions.restart` is absent |
| UC-45 | Restart bypasses circuit breaker |
| UC-46 | (retired; no response-timeout phase in the new model) |
| UC-78 | Data sensors go Unavailable only when the snapshot's ``modem_data`` is None |
| UC-88 | Reboot-signal check matches on a scheduled poll → recovery window opens |

---

## Action Executors

Transport-scoped executors for modem-side actions (logout, restart).
Located in `orchestration/actions/`.

### Single Dispatch

`execute_action(collector, modem_config, action)` is the single entry
point for all action execution. Both the collector (logout) and
orchestrator (restart) call it. The function extracts session, base
URL, and HNAP credentials from the collector and dispatches to the
appropriate transport-scoped executor based on the action's type
discriminator (`http` or `hnap`).

### HTTP Executor

`http_action.execute_http_action()` handles standard HTTP actions.

Phases:

1. **Pre-fetch** (optional): GET `pre_fetch_url` to establish session
   state (CSRF tokens, nonces).
2. **Endpoint extraction** (optional): find a `<form>` whose `action`
   attribute contains the `endpoint_pattern` keyword. Core-provided
   extraction — the keyword is not a regex.
3. **Param interpolation**: replace `{cookie:name}` placeholders in
   `params` values with the corresponding cookie from the session jar.
   If the named cookie is absent the placeholder is sent as-is.
4. **Main request**: send the action request to the resolved endpoint.

Connection errors and timeouts are treated as success (the modem is
rebooting during restart). Returns `ActionResult`.

### HNAP Executor

`hnap_action.execute_hnap_action()` handles HNAP SOAP actions.

Phases:

1. **Pre-fetch** (optional): call `pre_fetch_action` HNAP action to
   retrieve current config values.
2. **Interpolation**: replace `${var:default}` placeholders in `params`
   with values from the pre-fetch response.
3. **Main request**: HMAC-sign and send a SOAP POST to `/HNAP1/`.
4. **Response validation**: check `response_key`, `result_key`, and
   `success_value` from the action config.

Connection errors during the main request are treated as success.
HMAC signing uses shared primitives from `protocol.hnap`.
Returns `ActionResult`.

### ActionResult

All executors return `ActionResult(success, message, details)`.
Callers may use or ignore it — restart is fire-and-forget today,
but the result is available for diagnostics and future recovery
decisions.

---

## Event Taxonomy

See [`LOGGING_SPEC.md`](LOGGING_SPEC.md).
