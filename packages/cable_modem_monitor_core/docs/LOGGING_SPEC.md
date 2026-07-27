# Event Taxonomy

All log output from the orchestration layer flows through a single typed
event union. Components construct the appropriate event and pass it to
`log_event()` in `orchestration/logging.py`. The adapter owns level
routing and message formatting; components never call `_logger` directly.

Events are plain `@dataclass` types. The adapter dispatches via
`isinstance` — no discriminator field. Each event carries `level:
EventLevel` (see below) and `model: str` for multi-modem disambiguation.

## EventLevel

```python
class EventLevel(IntEnum):
    DEBUG   = logging.DEBUG    # 10
    INFO    = logging.INFO     # 20
    WARNING = logging.WARNING  # 30
    ERROR   = logging.ERROR    # 40
```

`IntEnum` — values are ints, passable directly to `_logger.log()`. No
mapping layer.

## Adapter contract

```python
# orchestration/logging.py
def log_event(logger: logging.Logger, event: OrchestratorEvent) -> None:
    ...
```

Single emission: one event, one log line, immediately. `OrchestratorEvent`
is the union type of all event dataclasses.

## Level policy

Most events carry a fixed level set as a dataclass default (`init=False`).
Events marked **caller-determined** take `level: EventLevel` as a required
init parameter — callers pass `EventLevel.DEBUG` for routine steady-state
operations and `EventLevel.INFO` for first-poll confirmation. This
preserves the first-poll-INFO / steady-state-DEBUG behaviour without
per-call-site level selection.

## Event inventory

### Phase: connectivity

| Event | Level | When |
|---|---|---|
| `ConnectivityFailureDetected` | WARNING | First failure in a connectivity streak; backoff set |
| `ConnectivityBackoffActive` | INFO | Subsequent poll skipped — still in backoff |
| `ConnectivityBackoffCleared` | INFO | Backoff counter reached zero; polling resumes |
| `ConnectivityBackoffReset` | INFO | User-triggered manual refresh cleared an active backoff |

Fields — `ConnectivityFailureDetected`: `model`, `streak: int`, `backoff_polls: int`
Fields — `ConnectivityBackoffActive`: `model`, `polls_remaining: int`
Fields — `ConnectivityBackoffCleared`: `model`
Fields — `ConnectivityBackoffReset`: `model`

### Phase: auth

| Event | Level | When |
|---|---|---|
| `AuthSucceeded` | caller-determined | Auth completed |
| `AuthFailed` | WARNING | Auth failed |
| `AuthLockoutDetected` | WARNING | Lockout detected |
| `AuthCircuitBreakerOpen` | ERROR | Circuit breaker opened |
| `CircuitBreakerPollingBlocked` | ERROR | Per-poll guard — breaker is open; credentials must be reconfigured |
| `StaleSessionRecoveryDisabled` | INFO | Stale-session recovery streak hit threshold; session reuse disabled for this runtime |

Fields — `AuthSucceeded`: `model`, `strategy: str`, `status_code: int` (0 when no response)
Fields — `AuthFailed`: `model`, `strategy: str`, `error: str`, `method: str | None`,
`url: str | None`, `status_code: int | None`, `content_type: str | None`,
`response_body: str | None`
Fields — `AuthLockoutDetected`: `model`, `streak: int`
Fields — `AuthCircuitBreakerOpen`: `model`, `streak: int`,
`status_code: int | None` — HTTP status of the failed login response
when the modem answered. A 404 renders the endpoint-not-found message
(wrong device at the address, or modem web interface unavailable)
instead of the credentials one; the stop behavior is identical either
way — retrying would keep posting credentials at an unknown device.
Fields — `CircuitBreakerPollingBlocked`: `model`
Fields — `StaleSessionRecoveryDisabled`: `model`, `streak: int`

Response-related fields on `AuthFailed` are `None` when auth failed with a
connection error (no HTTP response). Sanitization happens at event
construction time (not in the adapter) — the component knows the password
and strips the query string from `url` and scrubs the password from
`response_body` before creating the event.

### Phase: session

| Event | Level | When |
|---|---|---|
| `SessionReused` | DEBUG | Prior session reused |
| `SessionCleared` | DEBUG | Session cleared |
| `LogoutExecuted` | DEBUG | Logout action sent |
| `LogoutFailed` | WARNING | Logout action failed |
| `PostLoginFetchFailed` | WARNING | A `session.post_login_endpoints` path did not answer 2xx; collection continues |
| `HnapSessionExpired` | WARNING | HNAP HTTP error on a reused session — session likely expired |
| `StubPageDetected` | WARNING | 0 of N expected parser anchors found — stub/login page served at data URL |

Fields — `LogoutFailed`: `model`, `reason: str`
Fields — `PostLoginFetchFailed`: `model`, `path: str`,
`status_code: int | None` (None on a transport error), `reason: str`
Fields — `HnapSessionExpired`: `model`, `status_code: int`
Fields — `StubPageDetected`: `model`, `path: str`, `anchors_found: int`,
`anchors_expected: int`

| Event | Level | When |
|---|---|---|
| `SessionRetryStarted` | INFO | Single-poll session retry started for LOAD_AUTH or LOAD_INTEGRITY |
| `SessionRetrySucceeded` | INFO | Retry succeeded — fresh login obtained in same poll |
| `SessionRetryFailed` | INFO | Retry failed — policy recording signal as auth failure |

Fields — `SessionRetryStarted` / `SessionRetrySucceeded`: `model`, `signal_name: str`
Fields — `SessionRetryFailed`: `model`, `signal_name: str`, `streak: int`, `threshold: int`

### Phase: probe / health

| Event | Level | When |
|---|---|---|
| `HealthStatusReport` | internally-determined¹ | Health status derived from probes |
| `HealthRecoveryDetected` | INFO | Modem recovered from degraded state |

Individual probe failures (ICMP subprocess timeout, OS error, HTTP HEAD
failure) are direct `_logger.debug` calls — implementation detail
below the aggregate status signal. When probes fail consistently, the
status transitions to DEGRADED or UNRESPONSIVE and `HealthStatusReport`
fires at WARNING, which is the user-visible signal.

¹ `HealthStatusReport` computes its own level: WARNING on transition to
DEGRADED or UNRESPONSIVE, INFO on any other status change, DEBUG on
steady-state (no change).

Fields — `HealthStatusReport`: `model`, `status: str`, `changed: bool`,
`detail: str`
Fields — `HealthRecoveryDetected`: `model`, `previous_status: str`

| Event | Level | When |
|---|---|---|
| `HealthBackoffCleared` | INFO | Health probe confirmed modem reachable — connectivity backoff cleared |

Fields — `HealthBackoffCleared`: `model`

### Phase: collection / parsing

| Event | Level | When |
|---|---|---|
| `CollectionComplete` | caller-determined | Parse pipeline completed |
| `ParseError` | WARNING | Parser error on a resource |
| `ResourceLoadError` | WARNING | Resource could not be loaded |
| `HttpStatusError` | WARNING | HTTP 4xx/5xx on a resource |
| `ConnectionFailedDuringLoad` | WARNING | Connection dropped mid-load |
| `HnapConnectionFailed` | WARNING | HNAP connection failed |
| `HnapLoadError` | WARNING | HNAP load error |
| `ZeroChannelsNoSystemInfo` | WARNING | Zero channels and no system_info |

Fields — `CollectionComplete`: `model`, `ds_count: int`, `us_count: int`, `elapsed_ms: float`
Fields — `ParseError`: `model`, `reason: str`
Fields — `ResourceLoadError` / `HttpStatusError` / `ConnectionFailedDuringLoad`:
`model`, `path: str`, `status_code: int | None`, `reason: str`
Fields — `HttpStatusError` also carries `request_line: str`, `content_type: str`,
`response_body: str`, populated for 401/403 only and empty otherwise.
Fields — `HnapConnectionFailed` / `HnapLoadError`: `model`, `reason: str`

A 401/403 during load renders multi-line, mirroring `AuthFailed` — summary,
then `request:` (method, URL, and headers sent, with sensitive values masked
as `<set, len=N>`), `response:`, and `body:`. A bare status line cannot tell a
rejected session from a missing header or cookie. The collector constructs the
event because it is the only layer that knows the password: it scrubs
`response_body` and truncates it to the log-line budget first.

| Event | Level | When |
|---|---|---|
| `StatusTransition` | INFO | Connection status changed between polls |
| `CounterReset` | INFO | Error counters dropped — modem rebooted or stats cleared |

Fields — `StatusTransition`: `model`, `from_status: str`, `to_status: str`
Fields — `CounterReset`: `model`, `prev_corrected: int`, `cur_corrected: int`, `prev_uncorrected: int`, `cur_uncorrected: int`

### Phase: restart / recovery

| Event | Level | When |
|---|---|---|
| `RestartCommandSent` | INFO | Restart command dispatched and session cleared |
| `RestartCommandFailed` | ERROR | Restart command failed |
| `RecoveryWindowOpened` | INFO | Recovery window started |
| `RecoveryWindowClosed` | INFO | Recovery window ended |
| `RecoveryObserverException` | ERROR | Unhandled exception in recovery observer |

Fields — `RestartCommandSent`: `model`, `elapsed_seconds: float`
Fields — `RestartCommandFailed`: `model`, `reason: str`
Fields — `RecoveryWindowOpened`: `model`, `reason: str`, `window_seconds: float`
Fields — `RecoveryWindowClosed`: `model`, `elapsed_seconds: float`, `last_docsis_status: str`
Fields — `RecoveryObserverException`: `model`, `exc_type: str`

### Phase: actions (http exchange)

| Event | Level | When |
|---|---|---|
| `ActionStarted` | caller-determined | Action dispatched (HNAP / HTTP / CBN) |
| `ActionCompleted` | caller-determined | Response received, success path |
| `ActionConnectionLost` | caller-determined | Connection dropped — expected during restart |
| `ActionFailed` | WARNING | Bad response format, unexpected result, request error |
| `ActionPreFetchCompleted` | caller-determined | Pre-fetch returned data |
| `ActionPreFetchFailed` | WARNING | Pre-fetch connection error or bad response |

Fields — `ActionStarted` / `ActionCompleted` / `ActionConnectionLost`:
`model`, `transport: str` (`"hnap"` / `"http"` / `"cbn"`), `action_name: str`
Fields — `ActionCompleted`: adds `status_code: int | None`, `result: str`
Fields — `ActionFailed`: `model`, `transport: str`, `action_name: str`, `reason: str`
Fields — `ActionPreFetchFailed`: `model`, `transport: str`, `action_name: str`,
`reason: str`, `fallback_endpoint: str | None`
Fields — `ActionPreFetchCompleted`: `model`, `transport: str`,
`action_name: str`, `key_count: int | None`, `fallback_endpoint: str | None`

`ActionPreFetchFailed.fallback_endpoint` — non-`None` means extraction
failed but action continues with the static fallback endpoint.
`None` means the action is about to fail (`ActionFailed` follows).

### Phase: resource loading (http exchange)

| Event | Level | When |
|---|---|---|
| `ResourceFetched` | DEBUG | Data page fetched during collection |
| `ResourceDecodeError` | WARNING | Response could not be decoded |

Fields — `ResourceFetched`: `model`, `path: str`, `status_code: int`,
`size_bytes: int`, `elapsed_ms: float`
Fields — `ResourceDecodeError`: `model`, `path: str`, `fmt: str`, `reason: str`

`ResourceFetched` is emitted from `orchestration/collector.py` once per
page after `_load_resources()` succeeds. `ResourceDecodeError` is emitted
from `_load_http_resources()` after `HTTPResourceLoader.fetch()` returns —
the loader accumulates `decode_errors: list[tuple[str, str, str]]`
(path, fmt, reason) during the fetch; the collector emits one event per
entry. Both sit in the collector rather than in `loaders/http.py` to
avoid a circular import.

The following loader calls remain as direct `_logger` calls: auth-response
reuse (DEBUG, low signal), login-page detection (fires immediately before
raising `LoginPageDetectedError` — the collection layer owns the signal),
and the unknown-format fallback warning in `_decode_response` (the loader
returns BeautifulSoup rather than failing, so no `ResourceDecodeError` is
appropriate).

## Intentional exceptions — direct `_logger` calls

The following calls bypass `log_event()` by design. Each has a documented
reason; adding an event type for them would not improve diagnostics.

| File | Message pattern | Reason |
|------|----------------|--------|
| `orchestration/orchestrator.py` | `"Session reuse disabled [%s]..."` (DEBUG) | Low-signal pre-poll detail. |
| `orchestration/orchestrator.py` | `"Poll [%s] — auth: %s, url: %s..."` (INFO/DEBUG) | Verbose first-poll context log. INFO on first poll, DEBUG after. No event needed — it summarises static config, not a runtime signal. |
| `orchestration/policy.py` | `"Unknown signal: %s"` (WARNING) | Defensive guard that should never fire. |
| `orchestration/collector.py` | `"No active session [%s] — Authenticating"` (DEBUG) | Pre-auth lifecycle noise. `SessionReused` fires on the reuse path; this fires on the non-reuse path as a single debug line. |
| `orchestration/actions/hnap_action.py` | `"Interpolated %s: ${%s} → ..."` (DEBUG ×2) | Variable interpolation detail for action payload construction. |
| `orchestration/modem_health.py` | ICMP/HTTP probe debug calls (DEBUG ×4) | Individual probe failures below the aggregate `HealthStatusReport` signal. Documented above in § probe/health. |

## Test pattern

Tests use a `capture_events()` fixture that intercepts events before
they reach `_logger`. Tests assert on the typed event, not on formatted
strings:

```python
with capture_events() as events:
    # ... trigger code ...
assert_event_emitted(events, AuthFailed, model="SB8200")
```

This decouples tests from adapter format choices. Wording changes in the
adapter do not break tests; substance changes (wrong event type, wrong
model, wrong level) do.
