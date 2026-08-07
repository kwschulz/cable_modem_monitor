# Resource Loading Specification

Resource loaders fetch data from the modem and return a keyed dict for the
parser. The loader is the bridge between authentication and parsing — it
uses the authenticated session but owns no auth state, and it builds the
dict the parser consumes but extracts no data.

The fetch list is derived from parser.yaml — the orchestrator collects
`resource` paths (HTML/REST) or `response_key` values (HNAP) at startup —
merged with the paths parser.py declares via its `resources` attribute.

**Design principles:**

- Loaders are transport-specific — they know *how* to fetch, not *what* to extract
- The resource dict is the contract between loader and parser
- Loaders signal errors (status codes, timeouts); the orchestrator decides retry policy
- Pages are fetched once even when multiple semantic names share a path

---

## Resource Dict Contract

The resource dict is `dict[str, Any]` — keys identify resources, values are
the fetched content in a format-dependent type. Parsers access resources
by key and cast to the expected type.

### HTTP Transport — HTML Formats

Keys are the URL paths on the fetch list. Values are parsed HTML.

```python
{
    "/MotoConnection.asp": BeautifulSoup,
    "/MotoHome.asp": BeautifulSoup,
    "/MotoSwInfo.asp": BeautifulSoup,
}
```

- Keys are the path component only (no host, no query string).
  This is load-bearing: auth strategies that modify the URL (e.g.,
  `url_token` appending `?ct_<token>`) produce the same resource
  dict keys as unauthenticated variants. This enables multi-variant
  modems to share a single parser.yaml.
- Values are `BeautifulSoup` objects parsed from the response body
- One entry per unique path (see deduplication below)

### HTTP Transport — Structured Formats

Keys are the URL paths on the fetch list. Values are parsed
structured data (dict).

```python
{
    "/rest/v1/cablemodem/state_": dict,
    "/rest/v1/cablemodem/downstream": dict,
    "/rest/v1/cablemodem/upstream": dict,
}
```

- Same key convention as HTML formats — path only
- Values are `dict` from format-specific decoding (`json.loads()`, or `b64decode()` + `json.loads()`)
- The value type is always `dict` regardless of the wire format
- **Type enforcement:** If JSON decoding succeeds but the root value is not a
  `dict` (e.g., a JSON array, string, or scalar), the loader must treat it as a
  load error. HTTP loaders wrap non-dict values in `{"_raw": data}`. HNAP loaders
  raise `HNAPLoadError`. Code that consumes parsed JSON must use
  `isinstance(data, dict)` before `.get()` calls — modem firmware can return
  structurally valid JSON with unexpected types

### HNAP Transport

HNAP uses a single batched SOAP request, not per-page fetches. The resource
dict reflects this different transport.

```python
{
    "hnap_response": {
        "GetCustomerStatusDownstreamChannelInfoResponse": {...},
        "GetCustomerStatusUpstreamChannelInfoResponse": {...},
        "GetCustomerStatusStartupSequenceResponse": {...},
        ...
    },
}
```

- `hnap_response` — the full `GetMultipleHNAPsResponse` dict containing
  all action responses

**Action names vary by manufacturer** (e.g., `GetCustomer*`, `GetMoto*`). The
loader returns whatever actions the modem responds with — parsers
reference the action names via `response_key` in parser.yaml.

---

## Loader Behavior

### Page Fetching (HTTP Transport)

For each unique path on the fetch list:

1. Build the full URL: `{protocol}://{host}{path}`
2. Attach auth credentials to the request (strategy-specific):
   - **URL token** — append token as query parameter (e.g., `?ct_<token>`)
   - **Cookie-based** — session cookies are on the `requests.Session`
   - **Basic auth** — credentials are on the `requests.Session`
3. Send `GET` request with the modem's configured timeout
4. If `encoding: base64` is set on the section, decode first:
   `b64decode(response.text)` → raw text
5. Parse the response (format-dependent):
   - HTML formats: `normalize_html(text)` → `BeautifulSoup(..., "html.parser")`
   - `json`: `json.loads(text)`
6. Key the result by path (not by semantic name)

**HTML normalization:** Before BS4 parsing, `normalize_html()` fixes known
firmware malformations at the input boundary. The current rule rewrites
`<th>Label</td>` → `<th>Label</th>` — firmware that closes a `<th>` with
`</td>` instead of `</th>`, causing BS4 to nest sibling cells inside the
unclosed `<th>`. Applied here so parsers receive well-formed HTML without
needing per-parser workarounds.

**SSL handling:** If the config entry has `legacy_ssl: true` (detected
during validation), the loader configures the session for `SECLEVEL=0`
to support older modem firmware with weak TLS ciphers.

### HNAP Batching

HNAP modems expose all data through a single `/HNAP1/` endpoint via
SOAP-style POST requests. Instead of fetching pages individually, the
loader batches all actions into one `GetMultipleHNAPs` request.

1. Derive action names from parser.yaml `response_key` values (strip `Response` suffix)
2. Call `hnap_builder.call_multiple(session, base_url, actions)`
   - The builder handles HMAC signing (MD5 or SHA256 per modem config)
   - Single HTTP POST to `/HNAP1/` with `SOAPAction: GetMultipleHNAPs`
3. Parse the JSON response
4. Extract `GetMultipleHNAPsResponse` as the `hnap_response` dict
5. Individual action responses become keys within `hnap_response`

### HNAP Error Classification

HNAP errors during data fetching are classified by transport context:

| Condition | Signal | Rationale |
|-----------|--------|-----------|
| HTTP error + reused session | `LOAD_AUTH` | `/HNAP1/` always exists — HTTP error means server rejected the session |
| HTTP error + fresh session | `LOAD_ERROR` | Auth succeeded but server errored — genuine problem, not stale session |
| Connection error / timeout | `CONNECTIVITY` | Modem unreachable |
| Invalid JSON (HTTP 200) | `LOAD_ERROR` | Response arrived but is malformed |

This differs from HTTP transport, where 401/403 maps to `LOAD_AUTH`
and other status codes map to `LOAD_ERROR` regardless of session state.
HNAP firmware uses non-standard status codes for session rejection
(some return 404, others 500, rather than 401), so status code alone
cannot distinguish auth failure from server error. The session-reuse
context resolves the ambiguity.

### CBN XML POST Loading

The `cbn` transport uses a single HTTP endpoint
(`getter_endpoint` from auth config, typically `/xml/getter.xml`) for
all data fetches. Each data source is identified by a `fun=N` POST
body parameter rather than a URL path (`fun` = function code — the
firmware's internal dispatch ID, used as-is from the wire format).
The loader POSTs sequentially because the server rotates the session
token on every response.

**Fetch cycle:**

1. For each target in the fetch list:
   a. Read the current `sessionToken` from `session.cookies`
   b. POST to `getter_endpoint` with body
      `token=<sessionToken>&fun=<target.path>` — token **must be the
      first parameter** (modem firmware rejects other orderings)
   c. Parse the XML response with `defusedxml.ElementTree.fromstring()`
   d. Store the root `Element` in the resource dict keyed by `target.path`
2. Return the resource dict. The loader does **not** send logout —
   the collector handles logout via `actions.logout` config, consistent
   with the HTTP and HNAP transports (loader loads, collector manages
   lifecycle).

**Resource dict contract:**

```python
{
    "10": Element,   # downstream_table XML
    "11": Element,   # upstream_table XML
    "2":  Element,   # cm_system_info XML
    "1":  Element,   # GlobalSettings XML
    "144": Element,  # cmstatus XML
}
```

Keys are the `fun` parameter strings on the fetch list. Values are
`defusedxml.ElementTree.Element` objects
representing the parsed XML root.

**Token rotation:** The server sets a new `sessionToken` cookie via
`Set-Cookie` on every response. `requests.Session` handles this
automatically — the loader reads the cookie before each POST to
construct the `token` body parameter.

**Logout lifecycle:** Unlike HTTP (no mandatory cleanup) and HNAP
(session is implicit), `cbn` modems allow only one concurrent
session. Logout is handled by the collector via `actions.logout`
config — the same pattern as HTTP and HNAP. The loader is
responsible only for fetching data; the collector manages session
lifecycle (authenticate → load → parse → logout). See
`RUNTIME_POLLING_SPEC.md` § Session Lifecycle. Logout failure is
logged but does not fail the collection cycle.

**Error signals:** Same as HTTP — `ConnectionError`, `Timeout`,
non-200 status codes. Additionally, malformed XML responses are
logged and the target is skipped (parser receives no entry for that
fun value).

### Path Deduplication

Multiple sections in parser.yaml can reference the same URL path.
For example, both `downstream` and `upstream` sections may declare
`resource: "/cmSignalData.htm"`. The orchestrator collects unique paths
before passing the fetch list to the loader, so the page is fetched
once. The parser receives one dict entry for `"/cmSignalData.htm"` and
extracts both downstream and upstream data from the same parsed page.

### Auth Response Reuse

If the auth step's response already returned a data page (e.g., a
post-login redirect lands on a page in the fetch list), the loader
reuses that response instead of re-fetching. This avoids an extra HTTP
round-trip and is common with form auth modems that redirect to a
dashboard page after login.

**Contract — load-bearing.** Reuse keys on `AuthResult.response` and
`AuthResult.response_url`. Auth managers MUST populate these fields
only when the response body is itself a parser-consumable data page
for the path in `response_url`. Strategies that return opaque
artefacts (session tokens in the body, empty bodies, redirect
landings on non-data pages) MUST leave both fields unset; otherwise
the loader decodes the artefact as the data page and silently skips
the real fetch. See `auth/base.py` `AuthResult` docstring and
`MODEM_YAML_SPEC.md` § `url_token`. Regression: SB8200 #81.

The contract is unit-tested at the auth-strategy boundary (each
strategy's `test_*_branch_does_not_advertise_reuse`) and at the
loader boundary (`test_no_reuse_when_auth_result_has_no_response`).
Adding a new auth strategy with a non-data-page success path
requires both tests.

---

## Timeout

Each modem declares a `timeout` in modem.yaml (seconds). Core defines a
default (10 seconds) applied when the field is absent. Slow modems
override this — some cable modems take 12-20 seconds to render data pages.

The timeout applies per-request (each page fetch or HNAP call), not to
the entire loader operation.

---

## Fetch List Derivation

The orchestrator builds the fetch list at startup from two sources:

1. **parser.yaml** — every mapped section declares the resource it
   extracts from:
   - **HTTP:** Collects all unique `resource` paths from parser.yaml
     sections (downstream, upstream, system_info sources)
   - **HNAP:** Derives action names from parser.yaml `response_key`
     values (strip `Response` suffix) and batches them in a single
     `GetMultipleHNAPs` request
   - **CBN:** Collects all unique `resource` values (fun parameter
     strings) from parser.yaml sections and system_info sources
2. **parser.py `resources`** — the resources its hooks read,
   declared as a dict of path → format on the `PostProcessor`
   class. Applies to path-based transports (HTTP, CBN); not HNAP.
   Paths parser.yaml already maps are deduplicated, with
   parser.yaml's format winning.

See [PARSING_SPEC.md](PARSING_SPEC.md#fetch-list-derivation) for
details on both sources.

---

## URL Token Auth

URL token auth modems (e.g., modems with ISP-specific firmware) require
a session token appended to every data page URL. The token is extracted
from the login response body during authentication.

**Flow:**

1. Auth manager authenticates via URL-encoded credentials (base64 in
   query string)
2. Auth manager evaluates the login response using `success_indicator`:
   - Body **contains** indicator → body is the data page, no token
   - Body **does not contain** indicator → body is the session token
   - Empty body → fall back to `cookie_name`
3. Token stored in `auth_context.url_token`
4. Collector passes token to loader
5. Loader appends the token to each page URL:
   `{protocol}://{host}{path}?{token_prefix}{token}`

The token prefix (e.g., `ct_`) and cookie name are configured on the
auth strategy (`auth.token_prefix`, `auth.cookie_name`). The collector
prefers `auth_context.url_token` (body-derived) over cookie extraction.
The loader doesn't know how the token was obtained — it just appends
whatever the collector provides.

---

## Loader Selection

The transport declared in modem.yaml determines which loader is
instantiated:

| `transport` | Loader | Value type |
|-------------|--------|------------|
| `http` | HTTPLoader | Format-dependent: `BeautifulSoup` (HTML formats) or `dict` (structured formats) |
| `hnap` | HNAPLoader | `dict` (JSON) |
| `cbn` | CBNLoader | `defusedxml.ElementTree.Element` |

Selection happens once at startup (or after config change) and persists
for the integration's lifetime. The loader is instantiated by the
orchestrator's factory method.

---

## Error Signals

Loaders raise exceptions or return error indicators. They never retry,
back off, or decide what to do about failures — that's orchestrator
policy (see Signal and Policy Separation in `ARCHITECTURE.md`).

| Condition | Signal | Orchestrator response |
|-----------|--------|-----------------------|
| Connection refused | `ConnectionError` | Status `unreachable` |
| Request timeout | `Timeout` | Status `unreachable` |
| HTTP 401/403 | Status code in response | Stale session → retry auth |
| HTTP 5xx | Status code in response | Status `unreachable` |
| Login page on data URL | `LOAD_AUTH` | Clear session, retry once in the same poll; streak++ if the retry fails |
| Empty response body | Empty parsed result | Parser handles gracefully |
| Body will not decode as its format | `ResourceDecodeError` logged, path omitted from the resource dict | Parse layer reports the path unfulfilled → `LOAD_INTEGRITY` (PARSING_SPEC § Parser Diagnostics) |
| SSL handshake failure | `SSLError` | Check `legacy_ssl` flag |

On any 4xx/5xx response, the loader's exception message includes the
outgoing request shape (method, full URL with query string, and
headers actually sent). Header values whose names are declared by
the active auth strategy via `BaseAuthManager.headers()` are replaced
with `<set, len=N>` so logs confirm session-token presence without
leaking the value. Shared formatter:
`loaders.diagnostics.describe_request`. See ARCHITECTURE_DECISIONS.md
"Resource-load failure detail via request-shape log."

---

## HNAP Header Parsing Warning Suppression

Some modems send malformed HTTP headers that Python's parser flags.
ARRIS HNAP firmware prepends debug timing data plus leading whitespace
to the first header line (e.g. `4.400002  |Content-type: text/html`
preceded by spaces, which Python reads as an illegal header
continuation → `FirstHeaderLineIsContinuationDefect`, issue #98); the
SB6141 puts a space before the colon
(`MissingHeaderBodySeparatorDefect`). urllib3 emits a
`HeaderParsingError` warning with a full traceback for each such
response, producing noisy log entries on every poll cycle.

Core suppresses these warnings globally. The filter is harmless for
modems that don't trigger it — standard urllib3 header parsing warnings
are infrastructure noise for cable modem monitoring, not actionable
signals. urllib3 only emits this message after it has caught the
`HeaderParsingError` internally and returned the response, so the
suppressed warning never hides a real failure; genuine header
corruption that loses data surfaces in Core's own logging (zero
channels / parse error) instead. The malformed first header is
recoverable in practice: because `Content-Length` lands on a later,
well-formed line, the body is read in full and only the unused
Content-Type header is lost.

**Implementation:** `log_filters.SuppressHeaderParsingWarning`, a
`logging.Filter` that drops log records carrying a urllib3
`HeaderParsingError`. It keys on the **exception type** present on the
record (via `exc_info`, or as a positional arg), not on the rendered
message text. Every header-parse defect inherits from
`HeaderParsingError`, so one structural check covers all of them with
no per-defect string registry, and it cannot match an unrelated "parse
headers" message. It is attached to **both** the `urllib3.connection`
and `urllib3.connectionpool` loggers, because urllib3 moved the warning
between versions (1.26 emits from `connectionpool`, 2.x from
`connection`, and HA ships both across releases). Installed once at
package import via `install_filters()` (see `__init__.py`).

Scope note: this is a process-global filter, so it suppresses
`HeaderParsingError` warnings for every urllib3 consumer in the host
process, not only CMM polls. That is judged proportionate because the
warning is, by urllib3's own design, always the recovered-internally,
non-fatal case. If strict per-caller scoping is ever required, the
upgrade path is a thread-local set during CMM's own HTTP calls that the
filter consults.

**Why suppress rather than switch HTTP clients:** the lenient header
parsing under `requests`/`urllib3` (Python's `http.client`) is
load-bearing, not a liability. Cable modem firmware routinely emits
non-compliant HTTP, and `http.client` tolerates it: it records a defect
and still returns the body. Strict, spec-compliant clients reject the
same bytes outright. Feeding the #98 response to alternatives confirms
this: `httpx` (h11) raises `RemoteProtocolError` ("continuation line at
start of headers") and `aiohttp` (llhttp) raises a 400 "Invalid header
token". Either would turn a working modem into a failed poll with no
data. The `HeaderParsingError` warning is urllib3 being tolerant *and*
chatty; we keep the tolerance and silence the chatter rather than adopt
a client that is quiet only because it refuses to proceed.

---

## Login Page Detection

Some modems silently serve a login page at a data URL when the session
expires — HTTP 200, but the body is a login form instead of data.
Without detection, this reaches the parser and produces empty
extraction results that surface as a silent `no_signal` — wrong
root-cause classification (looks like a modem with zero channels,
actually an auth integrity failure) with no self-healing trigger.
The fall-through case is fully covered by UC-19a (see
[ORCHESTRATION_USE_CASES.md](ORCHESTRATION_USE_CASES.md)).

### Runtime behavior

The Resource Loader checks each HTTP 200 HTML response for login page
indicators before adding it to the resource dict. Detection is
automatic for any auth strategy that ARCHITECTURE.md's Auth Manager
table marks as not stateless on the `http` transport: a stateless
strategy holds no session that can expire, and the other transports
never reach this loader. That table is generated from the auth
models, and so is this rule — `_should_detect_login_pages` reads the
same `stateless` and `transport` ClassVars rather than restating
them.

**Detection invariant:** The data pages on the fetch list (status,
connection, channel info) do not carry a password field. Login pages
always do. A password field on a data URL means the modem served a
login page there.

**The test is a substring, not a DOM query.** `_is_login_page`
lowercases the undecoded body and looks for `type="password"` or
`type='password'`. This is deliberately looser than parsing for an
`<input type="password">` node: it runs before decode, so it costs
nothing and still reads a body that fails to decode, and it fires on
markup too broken to yield an `input` node and on login forms that
exist only inside a script template — the SPA shape the false-negative
row below describes. The tradeoff is that the string also matches
inside inline JS or a comment on a genuine data page, which is the
false-positive row.

When detected, the loader raises `LoginPageDetectedError`; the
collector maps it to `LOAD_AUTH` (the collection layer owns the
signal). The raise aborts the fetch, discarding every resource
already retrieved that poll — a session serving a login page at one
data URL is dead for the rest, and recovery refetches the whole list
anyway.

Recovery runs in the same poll, not the next one. The orchestrator
attempts logout (best-effort), clears the session, re-authenticates,
and refetches every target. If that succeeds the poll succeeds and
the auth streak resets. If it fails, the streak increments and the
poll reports `AUTH_FAILED`; at the threshold the auth circuit breaker
opens and blocks further polling. The breaker does not close on its
own — the HA layer starts a reauth flow, and completing it reloads
the config entry, which is what clears the state. See
[ORCHESTRATION_SPEC.md](ORCHESTRATION_SPEC.md) § Auth Circuit
Breaker and UC-19.

**Scope:** Only applies to HTTP transport, HTML format responses.
Structured formats (JSON, XML) and HNAP transport are not checked.

### Failure modes

| Failure | Impact | Likelihood | Mitigation |
|---------|--------|------------|------------|
| False positive (data page has `<input type="password">`) | Every poll aborts, retries, and fails; at the threshold the breaker opens and prompts reauth for credentials that are not wrong | Very low — the fetch list holds only status/data pages, not settings/admin pages | Detected during HAR regression; override via `session.login_page` (future, if needed) |
| False negative (response without `<input type="password">` is not real data) | Falls through to the parser and produces silent empty results — incorrectly surfaces as `no_signal`. Covers both JS-rendered SPA login forms and stub responses (issue #151). | Low — but observed in the field (CM1200, 2026-05-02) | Runtime: Parser Coordinator detects `0 of N expected anchors fulfilled` and raises `LOAD_INTEGRITY` (see UC-19a, `PARSING_SPEC § Parser Diagnostics`). Intake: HAR-time MCP onboarding flag (see below) |

If a false positive occurs in the field, the escape hatch is a
per-modem `session.login_page` override in modem.yaml with an
explicit indicator. This is not spec'd yet — it would be an additive
schema change if the need arises.

### MCP onboarding validation

During HAR analysis, the MCP pipeline should flag potential detection
issues:

1. **Login page without password input** — If the HAR shows a login
   page that has no `<input type="password">` in the initial HTML
   (e.g., JS-rendered SPA login), flag it: "Login page detection may
   not work for this modem — password field is dynamically rendered."

2. **Data page with password input** — If any data page response in
   the HAR contains `<input type="password">`, flag it: "Data page
   {path} contains a password field — login page detection will
   produce false positives."

Both are HARD STOP flags during onboarding — they require human
review before the modem can ship.

---

## Interaction with Other Components

```text
Auth Manager ──► Resource Loader ──► Parser
   │                  │                 │
   │ provides:        │ provides:       │ receives:
   │ - session        │ - resource dict │ - resource dict
   │ - url token      │                 │
   │ - cookies        │ fetches:        │ returns:
   │                  │ - fetch list    │ - ModemData
   │                  │                 │
```

- **Auth Manager → Loader:** The authenticated `requests.Session` (with
  cookies, auth headers) and any URL tokens. The loader uses the session
  as-is — it never modifies auth state.
- **Loader → Parser:** The resource dict. The parser receives pre-fetched
  content and extracts data. No session, no HTTP client, no auth
  awareness.
- **Orchestrator → Loader:** The orchestrator creates the loader, passes
  it the session and modem config, calls `fetch()`, and receives the
  resource dict. On loader failure, the orchestrator decides retry policy.

---

## Performance Characteristics

| Transport | HTTP requests per poll | Typical latency |
|-----------|----------------------|-----------------|
| HTTP | 1 per unique path in parser.yaml (typically 2-4) | 1-5s total (modem-dependent) |
| HNAP | 1 batched POST (all actions) | 1-2s |
| CBN | 1 per unique `fun` value in parser.yaml (typically 3-5) | 2-5s total (sequential, no batching) |

HNAP is the most efficient — one request regardless of action count.
HTTP scales with the number of unique data pages, but most modems
have 2-4 data pages. CBN is similar to HTTP in request count but
strictly sequential due to token rotation — no parallel fetching.

### Per-Resource Timing

The loader captures wall-clock time, response size, HTTP status code,
and Content-Type for each HTTP request, returned alongside the resource
dict as a list of `ResourceFetch` objects (see `ORCHESTRATION_SPEC.md`
§ Data Models):

```python
resource_fetches: list[ResourceFetch]
# e.g., [ResourceFetch("/status.html", 800.0, 12480, 200, "text/html"),
#         ResourceFetch("/info.html", 1200.0, 8192, 200, "text/html")]
# HNAP: [ResourceFetch("GetMultipleHNAPs", 1100.0, 24576, 200, "text/xml")]
```

Fields: `path`, `duration_ms` (milliseconds), `size_bytes`,
`status_code`, `content_type` (from response Content-Type header,
empty when absent). The orchestrator stores these on
`OrchestratorDiagnostics.resource_fetches` from the last successful
collection.

`status_code` and `content_type` enable remote diagnosis of format
mismatches (JSON served as `text/html`) and auth issues (unexpected
401/403 on data pages) without requiring a new HAR capture.

Each fetch is logged at DEBUG with its elapsed time:

```text
DEBUG "Resource loaded: /status.html (800ms, 12.2KB)"
DEBUG "Resource loaded: /info.html (1200ms, 8.0KB)"
```

This data is diagnostic — useful for identifying slow resources,
tracking latency trends, and troubleshooting format or auth issues.

Page deduplication keeps the request count at the number of unique paths,
not the number of semantic names. A modem with 5 semantic names pointing
to 2 unique paths makes 2 HTTP requests.
