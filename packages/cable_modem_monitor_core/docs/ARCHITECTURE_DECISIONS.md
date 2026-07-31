# Architecture Decisions

Distilled decisions and their rationale — the "why" behind the
architecture. Grouped by theme. For the full design, see the spec
files; this document explains the choices that shaped them.

## Contents

| Section | What it covers |
|---------|----------------|
| [Package Boundaries](#package-boundaries) | Runtime package split, dependency direction, where each piece lives, specs citing the catalog rather than restating its coverage |
| [Core Schema Model](#core-schema-model) | What enters Core's schema vs what stays user-side; catalog stores source-faithful strings and maps only observed values, display normalizes; derived and dynamic fields, health as its own structure |
| [Transport and Constraint Model](#transport-and-constraint-model) | Transport as protocol identifier, generated constraint tables, implicit capabilities, shared protocol primitives, config as parameters |
| [Auth Architecture](#auth-architecture) | Strategy discreteness, session lifecycle, failure logging, credential reconfiguration as reconstruction |
| [Parsing Architecture](#parsing-architecture) | Three roles, per-section format selection, parser.py as escape hatch |
| [Session and Action Model](#session-and-action-model) | Signal/policy separation, session reuse, restart-only actions |
| [Recovery Architecture](#recovery-architecture) | Restart vs recovery, generic timing, reboot-signal vote, observer callback |
| [Testing Strategy](#testing-strategy) | HAR replay, conformance gates every modem, greenfield from specs, fresh-context capture |
| [Onboarding](#onboarding) | MCP for deterministic steps, catalog_tools owns the spec, inference vs assembly, no fallback |
| [Config Flow](#config-flow) | Cross-directory grouping, variant label design |
| [Extension Model](#extension-model) | How to add modems, formats, parsers, auth strategies, transports |
| [References](#references) | Pointers to authoritative specs |

---

## Package Boundaries

### Three runtime packages with strict dependency direction

**Decision:** Core (engine) ← Catalog (content) ← HA Integration
(adapter), enforced through real Python packaging. A fourth package —
`catalog_tools` — provides catalog authoring tools; it is off
this runtime chain. See "catalog_tools is a developer accelerator"
below.

**Rationale:** Import violations are missing module errors, not lint
warnings. Core is platform-agnostic and could power any consumer —
HA, a CLI tool, a Prometheus exporter. Catalog is content-only (YAML
configs, parser overrides, HAR captures) with no business logic. The
HA integration is a thin adapter that maps Core output to HA platforms.

**Constrains:** Core cannot import from Catalog or HA. Catalog cannot
import from HA. No circular dependencies. Adding modem-specific
knowledge to Core is a design violation.

### Core is synchronous; HA integrates via executor

**Decision:** Core is a synchronous library built on `requests`, and
this is permanent. The HA integration bridges it by running every
poll, health check, and action on Home Assistant's executor thread
pool (`async_add_executor_job`) — never on the event loop. The two
HA Quality Scale Platinum rules this fails by construction
(`async-dependency`, `inject-websession`) are declined by design,
not tracked as gaps.

**Rationale:** Three forces, each sufficient alone. (1) Core serves
non-HA consumers — the intake pipeline, the test harness, contributor
debugging scripts — and synchronous `requests` is the surface that
audience can read, run, and contribute against. (2) Modem firmware
quirks are handled by the mature `requests`/`urllib3` stack: the
legacy-SSL adapter that lowers OpenSSL's security level for old
cipher suites, duplicate-cookie tolerance, and lenient header parsing
that accepts responses stricter clients (including aiohttp) reject
outright. An async rewrite would re-litigate every one of those
battle-tested workarounds for zero functional gain. (3) Async buys
concurrency, and the workload has none: one modem, one sequential
login→fetch→logout conversation, a few requests per poll interval.
The practical cost of the executor pattern is a worker thread
occupied for a few seconds per poll — negligible.

The alternatives were weighed and rejected: an aiohttp rewrite
(high cost, regression risk in firmware-quirk handling, breaks the
sync contributor surface) and a dual sync/async stack (doubles the
maintenance surface and invites drift). A thin async facade over
sync internals would satisfy the rules' letter while changing
nothing real — quality-scale rules are checklists in service of
outcomes, and where a rule's mechanism assumes the dependency
exists for HA, the documented exception is the honest answer.

**Constrains:** No `aiohttp`/`httpx` in Core; no `homeassistant.*`
imports in Core (restated from the package-split decision). The HA
adapter must never call Core from the event loop — blocking calls
belong in executor jobs, and any blocking I/O found on the loop is
a bug, not a candidate for loosening this rule. Everything *around* the boundary is still held
to full strictness: executor discipline, session teardown, graceful
failure. HA-layer quality audits assess all other quality-scale
rules at face value and cite this entry for the two declined ones.

### Test harness lives in Core, not Catalog

**Decision:** Core owns the test harness (HAR replay framework, golden
file comparison, schema validators). Catalog holds per-modem data
files. Catalog's own suite is fleet-wide — it auto-discovers every
modem rather than naming any — so no test code is per-modem.

**Rationale:** Catalog contributors cannot accidentally modify test
assertions or loader logic. Catalog's CI installs Core and runs both
Core's harness against catalog files and its own fleet checks. Adding
a modem never requires writing test code.

**Constrains:** Test assertions cannot be customized per-modem, and a
catalog test may not name a specific modem. Strategy changes are
validated across all modems automatically.

### Test harness lives in Core, not catalog_tools

**Decision:** `core/test_harness/` (HARMockServer, auth simulators,
discovery, runner, golden-file comparison) stays in Core, despite the
fact that the standalone CLI it exposes (`python -m
…core.test_harness`) is contributor-onboarding tooling.

**Rationale:** Three packages consume the test harness — Core (for its
own auth and loader unit tests, where `HARMockServer` is the in-process
fixture), Catalog (for regression and golden-file pipelines), and
catalog_tools (for the contributor onboarding workflow). Of those,
only Core is upstream of the others. Moving the harness into
catalog_tools would force Core to depend on catalog_tools to run its
own tests, inverting the package dependency direction and breaking the
"catalog_tools is never a runtime dep" decision below (Core's tests
are part of what verifies that runtime).

**Note on prior confusion:** When `load_post_processor` (a runtime
extension-point loader, peer of `load_parser_config`) was discovered
imported by HA from `core.test_harness.runner`, it surfaced a real
misplacement — but only of that one function, not of test_harness as
a whole. The function was extracted to `core/post_processor.py`.
test_harness itself is correctly placed.

**Constrains:** The harness is part of Core's published surface, not a
separately-installable contributor tool. Test-harness coverage counts
toward Core's coverage gate. The standalone CLI (`__main__.py`)
remains in Core and is invoked via `python -m
solentlabs.cable_modem_monitor_core.test_harness`.

### catalog_tools is a developer accelerator, never a runtime dep

**Decision:** The `cable_modem_monitor_catalog_tools` package
contains the modem-onboarding pipeline — HAR analysis, YAML
generation, golden-file construction, verification ingest,
analyzer-correctness lints, fleet-pattern scanning, and trial
parsing. It is installed in development environments (maintainer
and contributor) and CI. It is never installed by HA or any other
runtime consumer.

**Rationale:** The system's minimum required surface is Core +
Catalog. Anyone could hand-author a valid `parser.yaml` and
`modem.yaml` and the integration would work end-to-end. Catalog tools
exist to reach a working configuration *faster and more consistently*,
not to make configuration possible. Every byte that ships to a user's
HA instance has install-size, dependency-closure, and security-
attack-surface cost; keeping intake tooling off that chain keeps those
costs tied only to runtime value.

**Operational test:** Deleting the `catalog_tools` package directory
must leave Core + Catalog + HA fully functional. If anything in
catalog_tools is structurally required by runtime code, that thing
is misplaced and must move into Core or Catalog.

**Constrains:** Nothing in Core, Catalog, or the HA integration may
import from `catalog_tools`. The HA integration's `manifest.json` does
not list `catalog_tools` as a requirement. `catalog_tools` depends on
Core and Catalog (one-way edge); neither ever depends on it.

### Catalog's runtime API surface is `CATALOG_PATH`

**Decision:** The only symbol the HA integration imports from
Catalog is `CATALOG_PATH` — a `Path` constant pointing at the modem
directory tree (`modems/{manufacturer}/{model}/`). Everything else
the integration needs, it gets from Core.

**Rationale:** Catalog is content-only. Its "API" is the structured
data at `CATALOG_PATH/{manufacturer}/{model}/*`, consumed by Core's
config loaders. A richer runtime API would pull per-modem logic into
Catalog, contradicting "Catalog has no business logic." Keeping the
runtime import surface at a single constant makes the rule
enforceable: if a runtime consumer needs to import anything else from
Catalog, the code belongs in Core instead.

**Constrains:** Helpers that operate on catalog YAML files at runtime
(config loading, cross-file validation) live in Core. Helpers that
operate on catalog YAML files at authoring time (fleet scanning,
trial parsing, normalization) live in `catalog_tools`. Catalog's
runtime package never grows a second top-level symbol without a
decision update.

### Pydantic is a Core runtime dep

**Decision:** `pydantic>=2.0` is declared in Core's
`[project] dependencies`, not gated behind an optional extra.

**Rationale:** Core's `models/` package defines the config schemas —
`ModemConfig` and `ParserConfig` — as Pydantic `BaseModel` subclasses.
They are imported by runtime consumers —
`core/validation/cross_file.py`, `core/orchestration/*`,
`core/test_harness/runner.py`. A clean non-HA install of Core cannot
function without pydantic. `ModemData` is a `TypedDict`, not a model:
it is parser output, validated by the conformance gate rather than at
construction.

Prior to the v3.14 carve-out, pydantic was mis-declared as
`[project.optional-dependencies] mcp = ["pydantic>=2.0"]`. The bug
was masked in practice because Home Assistant itself depends on
pydantic and supplies it transitively. The carve-out corrects the
declaration.

**Constrains:** Anyone installing `solentlabs-cable-modem-monitor-core`
receives pydantic as a transitive install. The `[mcp]` optional
extra is removed; intake-pipeline heavy deps (ruamel.yaml and any
future additions) live in `cable_modem_monitor_catalog_tools`.

---

### `solentlabs` namespace with a `cable_modem_monitor_` prefix

**Decision:** Packages publish under the `solentlabs` namespace, each
named `cable_modem_monitor_*`.

**Rationale:** PyPI uniqueness, consistent branding, and a name that
ties the libraries to the HA integration they power.

---

### Specs cite catalog artifacts; they never enumerate coverage

**Decision:** A modem model named in a Core spec links to the catalog
file that evidences the claim. Specs never carry a list of which
modems a strategy, transport, or format covers.

**Rationale:** The catalog is the single source of truth for coverage,
and it is the only copy anything enforces — the index and audit are
generated and CI fails when they drift, and `link-check` fails when a
citation stops resolving. A bare model name in prose has neither gate,
so it is wrong the moment the fleet moves and nothing says so.
ARCHITECTURE.md claimed the CBN strategy covered `CH7465MT, CH7466CE,
CH7465CE`: a list naming one model no source supports, omitting the
SB8200 CBN variant the catalog does carry. MODEM_YAML_SPEC.md repeated
the same model, with no source behind it, inside a paragraph headed
"Evidence:".

**Constrains:**

- A model name earns its place by linking to that modem's
  `modem.yaml`, `parser.yaml`, or a file under its `test_data/`.
  CHANNEL_IDENTIFICATION_SPEC.md is the reference example.
- Coverage questions are answered by the generated catalog index and
  CATALOG_AUDIT.md, never by prose in a spec.

---

## Core Schema Model

### Core's schema tracks fleet-observed metrics, not user analytics

**Decision:** Core ships data fields that are observed across the
fleet of cable modems, normalized into a vendor-neutral schema.
Derived fields are admitted when they re-present a fleet-observed
datum in a more directly useful form (e.g., cumulative FEC counters
exposed as per-minute rates) or fill in cardinality the fleet does
not always report natively (e.g., channel counts). User-side
analytics — spreads, deltas, composed health grades, threshold-based
classification — do not enter the Core schema. They belong in HA-side
blueprints distributed alongside the integration per
[BLUEPRINT_DISTRIBUTION_SPEC.md](../../../custom_components/cable_modem_monitor/docs/BLUEPRINT_DISTRIBUTION_SPEC.md).

**Rationale:** The catalog's authority comes from being a faithful
record of what cable modems actually report. That authority
underwrites two things: a defensible schema that contributors can
validate against real captures, and a descriptive document of fleet
behavior that could support standardization advocacy with the broader
cable-modem industry. The moment Core invents metrics no modem
exposes, the schema stops being documentation of the fleet and becomes
opinion — and loses both forms of authority.

This also resolves the recurring "where does derivation belong"
question without case-by-case judgment. The error-rate sensors
(`rate_corrected`, `rate_uncorrected`) are Core because they
re-present a fleet-observed datum (FEC codeword counters) over time;
the cumulative quantity exists on every DOCSIS modem. A signal-health
grade is HA-blueprint because no modem exposes a "health" metric and
the grade is composed from user-chosen thresholds. DS power spread,
max-of-N aggregates, weighted scores, and tier classification all
fall on the blueprint side for the same reason.

**Constrains:**

- A new Core field must be backed by evidence that the underlying
  metric is exposed across vendors. The catalog itself is the evidence
  base; `modem.verified.json` captures are the primary citations.
- Re-presenting a fleet datum (rate from cumulative count, canonical
  from non-canonical naming, count from list length) is admitted.
  Inventing a new aggregation no modem reports is not.
- Scoping decisions (e.g., SC-QAM-only error totals) must be
  defensible from how the fleet reports the underlying quantities,
  not from a downstream consumer's threshold preference. The FEC
  chain argument in PARSING_SPEC § Aggregate is an example of a
  fleet-observation justification.
- Interpretation, grading, and threshold-based classification live in
  HA blueprints, not in Core. PR proposals that require relaxing this
  rule (e.g., a signal-health sensor inside Core) are out of scope by
  this decision.

### Catalog data stays true to source; normalization happens at presentation

**Decision:** The catalog is the authoritative record of what each
modem reports about itself and how its manufacturer brands it.
`manufacturer:` in modem.yaml stores the manufacturer's actual
styling (`ARRIS` if that's how the modem self-reports, `Arris` if
that's what comes back from HNAP, `Compal`, `Hitron`, etc.). Data is
never pre-normalized to title case in the catalog.

**Why:** This project is a universal translator. Variation in
manufacturer string across the same vendor's products is real signal
— different firmware reports the manufacturer differently — not
drift to be ironed out. Normalizing at the source would destroy
evidence the fleet actually exhibits.

**Constrains:**

- Display layers (`build_model_display_name` in the integration's
  config flow, sensor titles, etc.) own case normalization for human
  readability. The catalog never does.
- Intake and review must not "fix" styling inconsistencies between
  entries; matching the hardware's own output is the requirement.

---

### Catalog maps only values its own capture shows

**Decision:** A `map:` block in parser.yaml enumerates values observed
in that modem's own HAR. Anticipatory entries for values the capture
does not contain are not added, and existing ones are removed when
found.

**Rationale:** An unobserved mapping is a guess about firmware nobody has
seen, and it is indistinguishable from evidence once committed. The
guess is also silently wrong-able: `TDMA` was mapped to `atdma` on
three modems and to `ofdma` on four, and the first group had no
capture supporting either target. With 40+ HARs on hand, "we have not
seen it" is a conclusion, not a gap to paper over.

**Constrains:**

- The exception is documented platform knowledge shared across a
  firmware family, where a sibling's capture supplies the evidence and
  a spec or journal entry records the reasoning. The Technicolor
  `.jst` three-way upstream map is the standing example.
- Where a value genuinely has not been seen, leave it unmapped and let
  it surface. A field-level `map:` passes unmapped values through, so
  an unknown string reaches output rather than being silently
  mislabeled, which is the signal that earns a real capture.

---

### Time-anchored derivations belong to the consumer, not Core

**Decision:** Core reports `system_uptime` as the modem gives it and
computes no boot timestamp. The HA adapter owns Last Boot Time,
deriving it from `system_uptime` where the modem reports one and from
orchestrator counter-reset evidence (`stats_last_reset`) where it does
not. See ENTITY_MODEL_SPEC § Boot-time source normalization.

**Rationale:** A timestamp needs a clock to anchor it and somewhere to
persist it. Core has neither: it is stateless per poll, and the only
clock available to it is the modem's, whose timezone and sync state
are unknowable. The consumer has both, so the derivation lives there.

Deriving per poll would be wrong in any case. `now - uptime` drifts a
few seconds each poll, writing a new recorder row for a value that
changes only on reboot. The adapter replaces the held value only on
reboot evidence: an uptime decrease, unambiguous because uptime is
monotonic within a boot, or a shift beyond a jitter tolerance.

**Constrains:** This is the rule for any field needing a clock or
history to compute, and it marks the limit of § Core's schema tracks
fleet-observed metrics. That entry admits derived fields that
re-present a fleet datum; re-presentation is stateless arithmetic on a
single poll, anchoring is not. If a modem ever reports a native boot
time, subtract it from that modem's own reported current time so its
clock cancels.

---

### `SystemInfo` carries dynamic fields

**Decision:** Modem-specific `system_info` fields pass through without
Core changes. Core only understands the structured fields it declares.

**Rationale:** A modem exposing something unusual does not require a
Core release to surface it. The cost is that unstructured fields get no
typing or validation, which is why fleet-observed metrics are promoted
to structured fields (see § Core's schema tracks fleet-observed
metrics).

---

### `HealthInfo` is separate from `ModemData`

**Decision:** Health lives in its own structure with its own lifecycle,
not as fields on `ModemData`.

**Rationale:** Different cadences. A ping is lightweight and can run
often; parsing is heavy and runs on the slower poll. Fusing them would
force the expensive path to run at the cheap path's frequency.

---

## Transport and Constraint Model

### Transport is a protocol identifier, not a constraint funnel

**Decision:** The `transport` field in modem.yaml identifies the wire
protocol: `http`, `hnap`, or `cbn`. For `http`, auth, session, and
format are configured independently (qualified — some auth/session
pairings are linked; see MODEM_YAML_SPEC.md auth-session-action
consistency rules). For `hnap` and `cbn`, the protocol constrains
everything.

**Rationale:** The majority of modems use HTTP requests regardless of
whether the response is HTML, JSON, or XML. The difference between an
HTML table scraper and a JSON API modem is response format, not transport.
Keeping transport separate from format means each does one thing:
transport selects the loader, format (in parser.yaml) selects the decode
step and extraction strategy. Auth strategies are orthogonal to both —
any auth can appear with any format over HTTP. HNAP and CBN are
genuinely different protocols (HNAP: SOAP POST + HMAC signing; CBN:
XML POST + AES-256-CBC), so each warrants its own transport value
where the protocol constrains auth, session, and format.

Evidence: across the HTTP modem population, no auth strategy is
structurally tied to a response format. Basic auth modems serve HTML
tables today, but nothing prevents a basic-auth modem from serving
JSON. The pairings observed are coincidental, not architectural.
See the HAR corpus analysis (local reference data) for the full
modem inventory and stress test results.

**Constrains:** Format strategies must be compatible with the value type
they receive. HTML formats expect `BeautifulSoup`, structured formats
expect `dict`. Misconfigured modem.yaml is rejected at load time.

### The published constraint tables are generated, not written

**Decision:** The auth, format, and action models are the single
authoritative source for the transport constraint. The tables in
ARCHITECTURE.md and MODEM_YAML_SPEC.md are rendered from those models
by `scripts/generate_constraint_tables.py` into marked regions;
`tests/models/test_constraint_tables.py` fails when a doc is stale.
Rules derived from the same source — login-page detection reads
`stateless` and `transport` off the strategy — do not restate the
table either.

**Rationale:** The constraint lived in four hand-maintained places and
three of them were wrong: both ARCHITECTURE tables omitted `bearer`
(shipped and in use on `sagemcom/f3896lg-vmb`), ARCHITECTURE listed
`xml` as an HTTP format when it is CBN-only, and both docs omitted
`javascript_vars`. Nothing failed, because nothing compared them.
Reading ARCHITECTURE alone therefore produced wrong conclusions about
which strategies exist. A doc that restates a machine-checkable fact
is a doc that will disagree with the code eventually; generation
removes the opportunity rather than adding a review step.

**Constrains:** Two columns carry no model behind them (the loader
class and the session mechanism) and live in `_TRANSPORT_PROSE` in
the generator, keyed by transport. A transport with no entry fails
generation instead of rendering a blank cell. The three-axis Mermaid
diagram in ARCHITECTURE.md still enumerates values by hand — a
generated region inside a diagram node would be unreadable — so its
contents are gated by test instead.

### Capabilities are implicit from parser output

**Decision:** No `capabilities` field in modem.yaml. A mapping in
parser.yaml or an override in parser.py IS the capability declaration.
No mapping = no entity.

**Rationale:** Eliminates a separate capabilities list that drifts from
actual parser output. The parser output is the single source of truth.
Absent capability = absent entity — no greyed-out buttons, no "not
supported" placeholders.

**Constrains:** For parsed data, the only way to declare a capability
is to implement the extraction. Two entity groups sit outside parser
output and are gated differently:

- `actions.restart` in modem.yaml declares restart capability — a
  modem command, not parsed data.
- The ICMP and HEAD latency sensors follow probe results held on the
  config entry, not a catalog declaration. `health.supports_icmp`
  seeds a default and `health.supports_head` sets a ceiling, but setup
  probes the network and the probe decides. ICMP reachability is a
  property of the network rather than the modem, so it cannot be
  declared in the catalog at all.

`actions.logout` is session lifecycle, not a capability; it creates no
entity.

---

### Protocol primitives live in a shared `protocol/` module

**Decision:** Cross-cutting protocol code sits in `protocol/`:
`protocol/hnap.py` for HMAC signing and constants, `protocol/cbn.py`
for the AES-256-CBC encryption `form_cbn` auth needs.

**Rationale:** HNAP signing is used by auth, loaders, and action
executors alike. A shared module removes the duplication while each
consumer still owns its transport-specific flow.

---

### Transport-scoped action executors with single dispatch

**Decision:** `http_action.py`, `hnap_action.py`, and `cbn_action.py`
implement their own protocols; one `execute_action()` dispatches to
them.

**Rationale:** The three protocols have nothing in common at the wire
level — form POST, SOAP, parameterized XML POST. Separate modules stop
them coupling, while the single entry point gives the collector
(logout) and orchestrator (restart) one interface to call.

---

### Config fields are parameters, not implementations

**Decision:** Config supplies *what* to find; Core owns *how*. For
example `endpoint_pattern` supplies a keyword and Core provides
form-action extraction as a built-in strategy.

**Rationale:** Keeps behaviour in Core where it is tested once, rather
than letting each modem encode its own procedure. Extensible via an
`extraction_mode` field if a modem ever needs non-form extraction.
See MODEM_YAML_SPEC § Principles.

---

## Auth Architecture

### Discrete strategies, not composable primitives

**Decision:** Auth is a discriminated union of self-contained strategy
types; `_AUTH_MODELS` in `models/modem_config/auth.py` is the
authoritative list. Each strategy has a per-strategy model and a single
audited implementation. The alternative — a toolkit of composable
primitives (encoding, CSRF, nonce generation) that you mix and match
per-modem — was rejected.

**Rationale:** The boundary between "separate strategy" and "config
flag" is structural behavior — how the auth protocol works. `form_nonce`
parses text prefixes, not redirects. `form_pbkdf2` requires multi-round
challenge-response with server salts. These are structurally different
protocols, not variations you can compose from shared building blocks.
Meanwhile, base64 encoding, CSRF tokens, dynamic endpoints, and
AJAX-style login are all config flags on `form` — same
POST-evaluate-redirect flow.

**Constrains:** Adding a new auth strategy requires a new dataclass
(with `display_name`, `transport`, and `stateless` ClassVars), a new
`BaseAuthManager` subclass with a `create_manager()` entry point,
and a new entry in the `AuthConfig` union. The factory dynamically
imports the manager module by strategy literal — no factory code
changes, no isinstance chains, no manual registry updates. Display
labels, transport validation sets, login-page detection, and the
constraint tables published in the specs all derive from the
ClassVars automatically. No per-modem auth hooks — all variation is
modem.yaml config.

### No per-modem auth hooks

**Decision:** Auth and session have no override points. All variation
is expressed through modem.yaml configuration.

**Rationale:** Auth touches credentials. One audited implementation per
strategy, not per-modem overrides that could mishandle passwords or
leak tokens. The variation across the modem fleet is wide but shallow —
different values (field names, encoding, cookie names), not different
behaviors. If a modem needs a genuinely new auth flow, that's a new
Core strategy.

**Constrains:** Cannot customize auth behavior per-modem. Any new auth
pattern requires a Core change — but this is intentional, as it
becomes available to all future modems using the same protocol.

### Session is lifecycle, auth owns the cookie

**Decision:** `cookie_name` and `token_prefix` live on the auth
strategy config, not the session section. Session config
(`headers`, `query_params`) is a separate top-level section that
owns post-login lifecycle — static request headers and logout timing.
`actions.logout` presence drives single-session semantics. Auth owns everything about the login flow
and its outputs, including which cookie the login response produces.

**Rationale:** The cookie is an output of the login flow — the modem's
`Set-Cookie` header in the login response establishes it. Auth managers
need the cookie name for protocol-level operations: `url_token`
clears stale cookies before re-login (matching the browser's
`eraseCookie()` call), and session validity checking asks "is this
auth cookie still present?" Placing `cookie_name` on session created
a cross-boundary dependency: the auth manager needed session config to
execute its own login flow, violating the separation it was designed
to enforce.

v3.12 and v3.13 stored `session_cookie_name` on the auth config
(`UrlTokenSessionConfig`). The v3.14 spec moved it to a separate
`session` section for cleaner YAML structure, but this broke
`url_token` pre-login cookie clearing and body token fallback — two
protocol behaviors that only surfaced during real-hardware testing
(SB8200, Issue #81). The move back restores the boundary that
v3.12/v3.13 got right: auth owns credentials and their artifacts,
session owns lifecycle.

Similarly, `token_prefix` is part of the `url_token` auth protocol —
the auth flow produces a body token, and `token_prefix` describes how
to inject it into subsequent requests. It belongs on `UrlTokenAuth`,
not session.

**Evidence:** Journal entries 2026-01-16 (resource-loader-architecture)
and 2026-01-27 (session-cookie-clearing-browser-behavior) document the
original discovery. The v3.14 gap analysis (2026-04-01) identified the
regression.

**Constrains:** No `logout_url` or `logout_required` convenience
fields. `actions.logout` presence drives single-session semantics;
`HttpAction.requires_session` controls whether the pre-retry logout
call is guarded by session validity. Auth strategies that don't use
cookies leave `cookie_name` empty (default).

### Session concurrency — SSOT via `actions.logout`

**Decision:** `actions.logout` presence is the single indicator that a modem
requires single-session discipline. `session.max_concurrent` (present in v3.13
and earlier) has been removed from `SessionConfig`.

**Rationale:** `max_concurrent: 1` and `actions.logout` always had to travel
together — one without the other was either dead config (logout without
`max_concurrent: 1` never fired) or a lockout footgun (`max_concurrent: 1`
without logout held the session open and blocked the user's web UI between
polls). Two fields encoding one constraint is redundant and a footgun source.
The XB10 onboarding gap made this concrete: the intake pipeline had configured
a logout block without `max_concurrent: 1`, silently disabling logout.

**New field:** `HttpAction.requires_session: bool = False` distinguishes
unauthenticated logout endpoints (`false` — can clear any active server-side
session without credentials; safe to call during pre-retry recovery) from
session-scoped endpoints (`true` — skip the pre-retry call when the session
is not valid, since it would fail anyway and the retry proceeds regardless).

**The guard tests session validity, not cookie presence.** It originally
read the cookie jar, which is equivalent for every cookie-based strategy but
wrong for header-authenticated ones: `bearer` holds a live session in
`session.headers["Authorization"]` with an empty jar, so a cookie test would
silently skip logout on exactly the modems that most need it. `session_is_valid`
already answers this per strategy — HNAP checks uid plus private key, cookie
strategies check `auth.cookie_name`, `url_token` checks the token — so the
guard delegates to it rather than reimplementing a narrower check.
CBN transport always embeds the session token by protocol; `requires_session`
is absent from `CbnAction` by type-system design.

**How intake picks the value:** a logout request captured in the HAR without
a session cookie is evidence for `false`. Absent that evidence — the YAML
declares logout but the capture never shows the call — the value is `true`,
which costs a skipped logout rather than a failed one. The catalog carries
the current settings; `rg requires_session` over `modems/` is the list.

**Constrains:** Any modem with `actions.logout` configured is treated as
single-session. There is no mechanism to configure logout without triggering
single-session semantics.

### Post-login 401 is read per auth strategy

**Decision:** `BaseAuthManager.auth_failure_mode()` returns how a 401/403
arriving *after* a successful `authenticate()` should be read. The base
returns `CREDENTIALS_SUSPECT`; a strategy overrides to `SESSION_REJECTED`
only when it rejects a bad password at login time, or to `NOT_CONFIGURED`
when it sends no credential at all and a 401 therefore means the catalog
entry is wrong rather than the password. `AuthFailureMode` in
`auth/base.py` is the authoritative set. Core's failure hint and the HA
config-flow error key both derive from it.

**Rationale:** `LOAD_AUTH` does not mean the login succeeded — it means the
strategy *believed* it did. `basic` never validates a credential at all
(`auth/basic.py` sets `session.auth` and returns success), and plain `form`
accepts any response under HTTP 400. For those, a later 401 is usually the
bad password surfacing late. Telling that user "this is not a username or
password problem" is a dead end; telling a user with good credentials to
check their password is merely unhelpful. The default is therefore
pessimistic, and overriding is a claim.

**Proven, not assumed.** A strategy may declare `SESSION_REJECTED` only
with a bad-password test showing `success=False`. Today that is
`form_pbkdf2` and `hnap` — the two the mock harness can reject. The others
likely do verify, but the harness cannot yet simulate a rejected login for
them, so the claim is unpaid and they keep the conservative default.
Adding harness rejection plus its test is the price of flipping one.

**Constrains:** The knowledge lives on the strategy, not in an `isinstance`
chain in the orchestration layer — this replaced one that had drifted into
asserting verification that never happened. `modem.yaml` gains no field;
catalog contributors are unaffected. `test_auth_failure_modes.py` fails
when a new strategy has no declared mode.

**Regression:** beta.17 mapped every `LOAD_AUTH` to a "your login worked"
message, so a wrong password on a `basic` or `form` modem reported that the
credentials were fine.

### Post-login endpoints are session lifecycle, not an auth hook

**Decision:** `session.post_login_endpoints` lists paths Core GETs on
every successful fresh login, from inside `authenticate()`. A non-2xx
response or a transport error logs a WARNING and collection continues.

Owned by `authenticate()` rather than by a step in `execute()`, because
the calls establish the session rather than precede the data fetch. That
placement is also what reaches the restart path, where `restart.py`
authenticates and dispatches its action without entering `execute()` —
firmware that needs the call to consider a session established needs it
before a restart POST too.

**Rationale:** This does not contradict "No per-modem auth hooks" above.
Nothing here touches credentials: no secret is sent beyond the session
cookie already on the wire, no token is read back, and the response is
discarded. It is a lifecycle call the firmware expects between login and
data, which is exactly what the session section owns.

Log-and-continue rather than fail-fast, because login *did* succeed:
`AUTH_FAILED` renders as "check username / password" and would send a
user to re-verify working credentials. If the call really was required,
the data fetch fails on its own and produces two log lines — this
warning naming the missed call, plus the data-page 401 with its body
(see § Auth-failure detail via single WARNING log). Making a discarded
response fatal would also let a transient hiccup, or firmware moving the
path, kill monitoring on a modem that was working.

**Evidence:** #120 Technicolor CGA6444VF — `/api/v1/session/menu` is the
first authenticated request in the contributor HAR and the only path in
it that ever returns 401. The independent headless client
totev/vodafone-station-cli (MIT) ends its Technicolor `login()` with the
same GET, which a CLI has no UI reason to make.

**Constrains:** Ordered, best-effort, fresh-login only. Carries
`session.query_params` like every other fetch. No response parsing, no
conditional or templated paths, no per-modem retry policy.

### Auth-failure detail via single WARNING log

**Decision:** When the collector's auth phase fails, it emits one
sanitized ``WARNING`` log carrying the modem's response — strategy
name, request line, response status + Content-Type, and a short
body snippet with the user's password scrubbed. A data page that
returns 401 or 403 *after* a successful login emits the same detail
as ``HttpStatusError``: it is the same diagnostic question one phase
later, and answering it only for the login request is what stalled
issue #120. There is no transport-layer adapter, no scoped capture,
no separate entry point, no structured ``AuthExchange`` type, and no
``har-capture`` dependency.

**Rationale:** The motivating issues (#86 Arris TG3442DE, #104
Netgear CM1100, #120 Technicolor CGA6444VF) are all stuck-setup
failures where a maintainer needs to see what the modem returned
to fix the catalog entry. The genuinely valuable signal for that
is the response status, Content-Type, and a body snippet — enough
to spot "this modem is HNAP, not form" or "the modem rejected our
field name". One log line covers it. An earlier v3.14 iteration
built a session-adapter capture mechanism with two entry points,
gating policy, structured exchange JSON in the diagnostics
download, and an upstream sanitization dependency — ~500 lines
plus a runtime dep, for marginal value over a single log line.
KISS prevailed.

The failure-detail log fires from the collector's existing auth
failure path, so initial setup, reauth, options-flow
re-validation, and steady-state polling all benefit equally
without per-flow plumbing. A circuit breaker bounds the volume
during steady-state failure.

**Constrains:** Auth managers must include the ``requests.Response``
on their failure ``AuthResult`` so the collector can render the
detail. The response body snippet is truncated to ~500 characters
and the user's literal password is replaced with ``[REDACTED]``;
URL query strings are stripped wholesale (some strategies put
credentials in the query — Arris ``url_token`` notably). Derived
credential forms (PBKDF2 hashes, SJCL/CBN encrypted blobs) are
left intact in the snippet — they are protocol-shaped, not the
user's secret, and they're often the diagnostic signal a
maintainer needs to confirm the strategy ran.

### LOAD_INTEGRITY failure detail via diagnostics download

**Decision:** When the collector's parse phase returns a
``LOAD_INTEGRITY`` signal (zero fulfilled anchors for an expected
resource), the stub response body is captured in
``OrchestratorDiagnostics.last_stub_body`` — a ``dict[str, str]``
keyed by resource path, stored whole. It persists across successful
polls until the next ``LOAD_INTEGRITY`` event.

**Rationale:** ``LOAD_INTEGRITY`` means the session expired and the
modem returned a JS redirect stub instead of channel data. This
failure is intermittent and self-healing — the recovery cycle clears
the session and re-auths, leaving no trace in steady-state. Bug
reports arrive as a diagnostics download (``diagnostics.json``
shared in a GitHub issue), not as live log snippets. A WARNING log
fires once and is gone by the time the user generates the download.
Placing the stub body in the structured download is the only path
that ensures the diagnostic artifact is present when the user shares
it.

This is intentionally different from the auth-failure detail
decision (which chose a WARNING log). Auth failures are observable
in real time by the user performing initial setup; they are
logged for that audience. ``LOAD_INTEGRITY`` failures occur in
steady-state polling and are recovered automatically — the user
who files a bug report may not know they happened. The diagnostics
download is the correct artifact for that audience.

**Constrains:** Body is stored in full — no truncation. Unlike the
auth-failure log (500-character limit, log-line budget), the
diagnostics download has no verbosity constraint and the full stub
body is the diagnostic signal. ``last_stub_body`` is
overwritten on each ``LOAD_INTEGRITY`` event and is never cleared
on successful polls — it must survive into the next diagnostics
download even after recovery. Only resources that returned zero
fulfilled anchors contribute entries; resources that were simply
absent from the resource dict do not.

### Resource-load failure detail via request-shape log

**Decision:** When a loader (HTTP, HNAP, CBN) raises or warns on a
4xx/5xx response, the failure message includes the actual outgoing
request shape — method, full URL with query string, and headers
sent. Header values whose lowercase name is declared by the active
auth strategy via ``BaseAuthManager.headers()`` are replaced with
``<set, len=N>``; everything else is verbatim. Implemented as a
shared ``loaders.diagnostics.describe_request`` helper consumed by
all three loader modules.

**Rationale:** Auth-phase failures already had detail (see prior
decision). Resource-phase failures didn't — the message was
``"HTTP 400 fetching /php/status_docsis_data.php"``. Issue #86
spent four alpha cycles on a TG3442DE 400 because each iteration
shipped a theory ("must be ``_n`` cache-buster", "must be
``ajaxSet_Session``"), the modem rejected the next attempt, and
nothing in the user's log told us *what we actually sent vs what
the browser sends*. The browser's HAR was ground truth; our side
had no symmetric artifact. Including the request shape in the
loader's exception message means the contributor's first failure
log paste IS the diff input.

**Constrains:** Auth strategies own which header names carry
session tokens — they declare them via ``BaseAuthManager.headers()``,
which defaults to ``frozenset({"cookie"})`` and is overridden by every
strategy that puts a credential somewhere else. That method is the
authoritative set; this entry does not restate it. Loaders treat
this set as opaque — a Core-layer ``headers`` parameter, no
"sensitive" qualifier in the loader API. The wire request is never
modified; redaction only applies when ``describe_request`` formats
the failure log line.

---

### Credential reconfiguration is reconstruction, not mutation

**Decision:** Credentials are constructor-bound. Core exposes no
credential setter and no auth-state reset method; a credential change
always means the consumer discards the orchestrator and builds a new
one (in HA: config-entry update + reload after the reauth or options
flow). A fresh instance starts with every auth-related field —
failure streak, circuit breaker, login backoff, session, error-rate
baseline — at its constructor default.

**Rationale:** `Orchestrator.reset_auth()` was specified (2026-03,
v3.14 Step 19) for an HA reauth flow that was planned to call it, but
the adapter shipped with entry reload instead (Step 21) and the method
never gained a production caller. Its contract was unfulfillable:
without a credential setter, the consumer must rebuild anyway, at
which point every field the reset would clear is already fresh.
Reconstruction is also strictly stronger than an in-place reset — a
hand-maintained clearing list must be extended for every new
auth-adjacent field (and was, across five commits, all only reachable
from tests), while a constructor can never miss one. Retired 2026-07;
UC-16 rewritten around rebuild, `AuthStateReset` event removed with
no replacement log line (the lockout WARNING, reload startup INFO,
and verbose first-poll INFO carry the recovery story).

**Constrains:** Consumers treat orchestrator instances as disposable
— no consumer-side caching that outlives a credential change, and
they call ``orchestrator.close()`` on discard. ``close()`` best-effort
logs out any live session (so single-session firmware isn't left
holding a lock — the same concern as § Session concurrency) and then
releases the HTTP session's sockets deterministically rather than at
GC. HA does this in ``async_unload_entry``, which runs before every
reload. Any future "clear auth state" need is served by rebuild, not
by re-adding a reset method.

---

### Base64 is an encoding on `form`, not its own strategy

**Decision:** Modems that base64-encode the password use
`encoding: base64` on the `form` strategy.

**Rationale:** The flow is identical to a plain form POST; only the
value differs. A separate strategy would duplicate the whole flow to
change one transformation. Contrast `form_pbkdf2`, which is a genuinely
different multi-round-trip exchange (see § Discrete strategies).

---

### JS-driven auth is a `form` variant

**Decision:** Login flows driven by page JavaScript are configured as
`form`, not as a distinct strategy.

**Rationale:** Not distinct enough to justify its own type — the
observable exchange is still a form POST. What the page script computes
is captured as hidden fields or extracted values.

---

### No auth discovery

**Decision:** Strategy *selection* is config-driven only. Core never
inspects a login page at runtime to determine which auth type a modem
uses.

**Rationale:** Too fragile — firmware pages vary far more than the
underlying exchange. Strategies do interact with login pages during
*execution* (extracting hidden fields, nonces, salts); the ban is on
using them to choose the strategy.

---

### `AuthResult.auth_context` is typed

**Decision:** Auth strategies store downstream state in an
`AuthContext` dataclass with named fields (`url_token`, `private_key`,
`token`, `user_id`), which the runner reads by attribute based on
`modem_config.transport`.

**Rationale:** Adding a transport means adding a field, not inventing a
magic string key. `cbn` needs no field at all — its session token lives
in cookies managed by `requests.Session`.

**Extended for action endpoints (F3896LG, #185).** `token` and `user_id`
were added when a firmware turned out to end its session with
`DELETE /rest/v1/user/{userId}/token/{token}` — both values live in the
login response and neither is a cookie. They are surfaced to action
endpoints as `{auth:token}` and `{auth:user_id}`, so the same named-field
rule now governs two consumers: the resource loader and the action
executor. The alternative, letting modem.yaml address the login response
by JSON path, was rejected in MODEM_YAML_SPEC.md § Architecture Decision:
a fixed key set, not a template language. A third value is a third field
here, not a new syntax there.

---

## Parsing Architecture

### Three roles: BaseParser, ModemParserCoordinator, parser.py

**Decision:** Parsing has three distinct roles instead of a single
class hierarchy. `BaseParser` (ABC) is the extraction interface, one
implementation per format. `ModemParserCoordinator` is the factory and
orchestrator. `parser.py` is an optional post-processor.

**Rationale:** The original design conflated extraction, orchestration,
and customization into one class — a god class risk. Separating them
makes each independently testable. The coordinator sequences extraction
and post-processing and owns nothing else; extraction complexity lives in
`BaseParser` implementations, and parser.py is a hook, not an inheritance
override.

**Constrains:** parser.py cannot subclass `BaseParser` or the
coordinator. It receives extraction output + raw resources and returns
final section data. Per-section hooks only (`parse_downstream`,
`parse_upstream`, `parse_system_info`) — no modem-level hook.

### Format selection is per-section, not per-modem

**Decision:** Each parser.yaml section (`downstream`, `upstream`,
`system_info`) declares its own `format` independently. A modem can
mix formats.

**Rationale:** Real modems mix formats — transposed tables for channels
plus JavaScript for system info, or html_fields from multiple pages.
Per-section format selection accommodates this without forcing a
single format choice.

**Constrains:** Every section must explicitly declare its format. No
inheritance, no defaults to guess.

### parser.yaml is primary, parser.py is the escape hatch

**Decision:** At least one of parser.yaml or parser.py is required.
parser.yaml is the primary expression mode. When both exist, parser.yaml
handles standard extraction and parser.py post-processes sections that
need code. When extraction is too complex for declarative config,
parser.py alone is valid.

**Rationale:** Declarative config (parser.yaml) is reviewable,
validatable, and consistent. Code (parser.py) handles genuine
structural variety that can't be expressed declaratively. When a
parser.py pattern recurs across 3+ modems, it graduates to a
parser.yaml config field.

**Constrains:** parser.py hooks cannot make network calls — only
pre-fetched resources are available. parser.py never contains auth
or metadata. modem.yaml never contains extraction logic.

### Promoting `_transpose_nodes` to a Core format

**Decision:** The indexed-pivot JSON shape (`name` + `indexN` rows,
each column a channel) was promoted from `dm1000/parser.py` into a
Core format (`json_transposed`) plus a public
`transpose_indexed_rows` helper. dm1000's `parser.py` keeps the
firmware-specific filter (`Power == "ON"` and `"OPERATE" in STATE`)
and the OFDMA channel build, but no longer owns the pivot.

**Rationale:** A valid HAR capture is enough to ground-truth a
parser format — the captured payload either matches the new format's
output for a given config or it doesn't. That's a different bar than
auth promotion, where end-to-end verification against a live modem
is required because failure modes (challenge replay, cookie
binding, anti-CSRF) only surface against running firmware. We
expect more sercomm-family modems to land with this same indexed-row
shape, and "leave the helper in dm1000 until the second consumer"
forces that contributor to refactor instead of just adding a
parser.yaml. The promotion cost is one model + one parser + registry
wiring — paid once, amortized across future modems.

**Constrains:** The format owns the pivot, type conversion,
`channel_type`, and `filter`. Firmware-specific quirks that aren't
expressible declaratively (substring filters, stateful gating) stay
in `parser.py` and import `transpose_indexed_rows` from the curated
`post_processor_helpers` module to share the pivot logic. New filter
operators added to handle such quirks should land as separate,
additive ADRs — not bundled with format promotions.

### Companion tables merge via config, not code

**Decision:** The `merge_by` field on `tables[]` entries tells the
coordinator to merge companion table fields into primary channels by
declared key fields, instead of appending them as separate channels.

**Rationale:** Some modems split channel data across primary and
companion tables. This was a parser.py use case that
graduated to config. `merge_by` is a list of field names forming the
lookup key — primary table wins on field conflicts.

**Constrains:** All tables in a section share the section-level
`resource`. Companion tables must have matching key fields for the
merge to work.

---

### `parser.*` parses; `modem.yaml` owns everything else

**Decision:** `parser.yaml` and `parser.py` are responsible for parsing
and nothing else. `modem.yaml` owns identity, auth, session, actions,
and metadata.

**Rationale:** A clean split means a contributor changing extraction
never touches auth, and vice versa. Where the declarative/code line
falls inside the parser is the implementer's call per modem (see
§ parser.yaml is primary).

---

### `modem-{variant}.yaml` per firmware variant

**Decision:** Each firmware variant gets its own file carrying its own
auth, session, and ISP config. Merging is implicit — variant files hold
no references to each other.

**Rationale:** Variants differ in ways that cut across the config, so
expressing them as diffs against a base would be harder to read than
stating each one whole. Independent files also give each variant
independent test data and independent confirmation status.

---

### Coordinator parser registry

**Decision:** Section type maps to parser function through a dict, not
an isinstance chain. The dict is derived from the format-model lists
rather than hand-maintained, so a format registered without a wrapper
fails at import. A section type with no entry raises
`NotImplementedError` naming the model class, at the lookup site.

**Rationale:** Adding a format is one wrapper entry plus the parser.
Deriving the dict from the model lists means the two cannot drift: the
comprehension indexes the wrapper table by `format_tag`, so a missing
wrapper is an import-time failure rather than something discovered on
the first modem that uses the format. Both failure paths name what is
missing instead of falling through to a generic error.

---

## Session and Action Model

### Signal/policy separation

**Decision:** Protocol layers (auth manager, resource loader, parser)
signal conditions. The orchestrator owns all policy (retry, backoff,
error reporting).

**Rationale:** When a layer both signals a condition and decides what
to do about it, callers inherit hidden policy they can't override.
Keeping signal and policy separate gives the orchestrator full
visibility and full control. Auth strategies raise
`LoginLockoutError` but don't track lockout state or decide the
response; `SignalPolicy` trips the circuit breaker (see § Session
reuse across polls for why lockout stops rather than backs off).

**Constrains:** Protocol layers never retry, back off, or decide what
to do about failures. All policy state (failure streaks, connectivity
backoff, session reuse decisions) lives on the orchestrator.

### Session reuse across polls

**Decision:** Sessions persist across polls. The auth manager reuses
valid sessions instead of re-authenticating every cycle.

**Rationale:** HNAP modems have firmware anti-brute-force that can lock
out or reboot the modem after repeated login attempts (HNAP modem
firmware `LOCKUP` and `REBOOT` states). Session reuse is the primary
defense. The safety net is the circuit breaker: a lockout signal stops
polling outright rather than backing off, because a modem that is
refusing logins to protect itself gains nothing from a slower retry.

**Constrains:** Session state must be maintained across polls. Stale
session detection requires a within-poll retry mechanism (zero channels
on reused session → clear and retry once). Reuse also disables itself:
after `stale_recovery_threshold` consecutive same-poll recoveries,
`SignalPolicy` stops attempting it for the rest of the runtime, so
firmware with a chronically short session TTL does not burn the first
request of every poll on a session it has already expired.

### Two modem-side actions only

**Decision:** Two actions: restart (user-triggered) and logout
(system-triggered). Both draw on the same `ActionConfig` union, which
carries one type discriminator per transport.

**Rationale:** The integration's purpose is read-only monitoring.
Restart is the sole state-changing action — a security and
terms-of-service boundary. Logout is session cleanup so users can
access the modem's web UI between polls. No other modem commands
are supported.

**Constrains:** Cannot add modem commands beyond restart without
expanding the action model. This is intentional.

---

### Health probes are independent layers, not an escalation chain

**Decision:** ICMP, HEAD, and TCP each measure a different layer. All
run when the modem supports them; none is a fallback for another.
Status comes from ICMP + TCP. HEAD measures latency only and never
changes status. A recent successful collection skips HEAD and TCP,
which would only re-prove what it already showed. For probe order and
per-configuration outcomes, see ORCHESTRATION_SPEC § Probe
Configurations.

**Rationale:** One HTTP-latency number was averaging two different
things: the modem's cached response and its handler execution. The same
modem read ~5ms or ~100ms depending on the network path, so the number
meant something different from one poll to the next (wire-traced,
2026-04-29). Separate probes keep them apart. They also make
misconfiguration readable — a reachable IP with a listening socket but
the wrong firmware now looks different from a dead modem. Escalation
would not do this: a passing ICMP says nothing about whether the web
server is listening.

**Constrains:** Never hammer a modem's web server for something a
cheaper probe answers. That rule lives in the skip gate and the
`supports_head` guard, not in probe ordering. Two things look like bugs
and are not: HEAD runs before TCP so a single-threaded modem gets an
uncontested connection, and a failed ICMP forces TCP past the skip gate
so stale evidence cannot outvote a live probe (UC-59a).

---

## Recovery Architecture

### Restart is a command, recovery is a window

**Decision:** `Orchestrator.restart()` is a one-shot command
(authenticate → execute action → clear session → trigger recovery →
return). It does not probe, does not wait for operational, does not
observe the reboot. Anything that happens after the command lands is
handled by a separate "recovery" module that owns polling cadence
for a bounded window.

**Rationale:** The earlier design conflated the *command* (send a
reboot instruction) with the *recovery observation* (watch until the
modem is operational again). These are separable concerns with
different contracts. The command is transactional — it either
dispatches cleanly or it doesn't. Recovery is a polling-cadence
concern — HOW OFTEN to poll for a while after a disruption. Merging
them produced a restart method that blocked for three minutes,
needed cancellation plumbing, owned its own probe loop, and
duplicated work that external-reboot detection also needed.

Splitting them lets each do one thing. Restart becomes ~5 lines.
Recovery becomes a single module that any trigger (command, observed
outage, heuristic) can ask to enter a window. The Status sensor
always reflects real snapshot state (Unreachable → ranging → Operational);
no synthetic "Restarting…" label. The restart button returns quickly;
the dashboard tells the user what's actually happening.

**Constrains:** Restart never waits, never times out, never
cancels. Its only failure mode is `command_failed` (auth or action
executor raised). Recovery cannot be triggered by caller request
other than the three defined paths (command, observed failure,
reboot-signal check). Consumers cannot observe recovery window
progress — they see the snapshot stream and react to that.

### One recovery concept, multiple triggers

**Decision:** Recovery is a unified module — `orchestration/recovery.py` —
with a single state (`active: bool`) and three entry triggers:
command dispatched, observed connectivity outage, and a reboot-
signal check on successful polls. All three enter the same window
with the same behavior.

**Rationale:** The modem's physical state — "not fully operational,
expected to return" — is the same regardless of what triggered it.
Modeling it as one thing with multiple triggers matches reality.
The earlier design had two implementations (inline wait for
commanded, flag+observer for detected) running in parallel; they
shared a deadline constant, differed in everything else, and
required the orchestrator to arbitrate between them. The unified
module removes the arbitration.

Triggers differ only in how they recognize "recovery is needed."
The recovery module's reaction is the same: open a window, let
polls run at a faster cadence, close the window when the window
duration expires. Exit is time-based, not snapshot-based — that
sidesteps the "should we exit on OPERATIONAL?" inference trap.

**Constrains:** New trigger paths go through the recovery module's
existing entry API (`begin(reason)`, `evaluate_snapshot`,
`evaluate_failure`). No other module owns polling loops, deadlines,
or cadence decisions. The orchestrator delegates; the collector has
no knowledge of recovery.

### Generic timing, not per-modem knobs

**Decision:** Recovery timing lives as class attributes on
`Recovery` in `orchestration/recovery.py` (e.g. `WINDOW_SECONDS`).
Modem YAML and action models carry no timing fields.

**Rationale:** Per-modem tuning was tried in v3.14 alpha and
removed. "How long a reboot takes" varies by firmware version, CMTS
load, and DOCSIS ranging — none of which are modem-class
characteristics. Bench-tuning values per modem doesn't scale:
firmware updates silently invalidate them, and a value too short or
too long produces misleading UX or wasted polls.

**Constrains:** New modems cannot introduce grace/timeout fields on
action models or in `modem.yaml`. Recovery timing is a global
concern. If future needs justify user-configurable cadence/window
settings, they live in HA's options flow, not per-modem config.

### Reboot-signal trigger is a simple threshold vote, bounded harm

**Decision:** The "did a reboot happen between polls?" trigger is a
2-of-3 vote over three observables: counter reset, uptime drop,
transitional docsis. Implemented as a private method
`_check_reboot_signals` on the `Recovery` class — no separate
module, no weights, no probabilities, no inference framing. Its
output is binary: "trigger a recovery window, or don't."

**Rationale:** An earlier sketch called this "recovery heuristics"
and lived in its own module with inference framing. That oversold
both the complexity and the uncertainty of the logic. It's boolean
logic over observables — a simple vote. The state it needs
(previous counter totals, previous uptime) belongs with
Recovery's other state; splitting it created a seam where there
wasn't a real boundary.

A false positive (two signals match without a real reboot) causes
polling to run faster for a bounded window and nothing else. No UX
misrepresentation (the snapshot still reports whatever the modem
reports), no misleading labels, no stalls. That blast-radius cap
is why the threshold can stay simple.

**Constrains:** The reboot-signal check cannot set status, cannot
publish UX state, cannot extend window duration. It returns a
reason string (e.g. `"reboot_signals:counter_reset+transitional_docsis"`)
or None. If the rule grows more complex — weighted scoring,
per-modem opt-outs, user-level disable — it graduates to its own
module then. Not now.

### Core→HA recovery coupling via observer callback

**Decision:** `Orchestrator.set_recovery_observer(callback)` lets
the HA adapter register a callable that fires when
`recovery_active` flips (False→True on window entry, True→False on
window exit). HA wires this to `dispatcher_send`, and its one
subscriber swaps the data coordinator between normal and recovery
cadence. Consumers that only need to read the flag do so directly;
the restart button is deliberately not among them — overlapping
presses are refused by the operation mutex, not by recovery state,
so a user whose modem is still flakey may retry.

**Rationale:** Core cannot import `homeassistant.*` (principle #3).
Polling `recovery_active` from HA would lag by a coordinator cycle
and miss rapid transitions. An observer callback keeps Core
platform-agnostic while giving HA immediate, thread-safe
notification when state changes on the Core poll thread. The
"poll faster for a while" loop lives in HA (via
`coordinator.update_interval`), not in Core — Core only signals
state.

**Constrains:** New Core→HA state signals follow the same observer
pattern, not shared mutable state or HA-side polling. Observer
callbacks must be safe to call from the Core poll thread. Any
"check until condition" loops triggered by Core state live in HA
using its native scheduling primitives.

---

### Recovery detection derives from `data_path_up`, not state enumeration

**Decision:** Every consumer that asks "did the modem's data path
recover?" derives the answer from `HealthStatus.data_path_up` — a
per-reading boolean (RESPONSIVE and ICMP_BLOCKED are up; DEGRADED,
UNRESPONSIVE, and UNKNOWN are down). Recovery is a down → up edge of
that boolean. No consumer enumerates health states or state
transitions for reachability decisions. Current consumers: Core's
connectivity-backoff clear (ORCHESTRATION_SPEC § Collection Flow) and
the HA adapter's health sync listeners (HA_ADAPTER_SPEC § Health Sync
Listeners).

**Rationale:** State enumeration produced three field bugs from the
same root: a startup UNKNOWN misread as recovery, DEGRADED missing
from the down-set (2026-06-29, issue #170 class), and a transitional
ICMP_BLOCKED reading laundering the down state so neither recovery
path fired (observed 2026-07-12 — a reachable modem stayed
Unreachable for a full scan interval). Lists of states model
positions; recovery is a property of a path, and intermediate
readings the list never anticipated each became a bug. ICMP_BLOCKED
counts as up because the ICMP contradiction override (UC-59a)
guarantees every such reading is confirmed by a live TCP handshake —
this supersedes the "conservative for v1" exclusion from 2026-04-01,
which predated that guarantee and was recorded only in a test-table
comment rather than here. Up → up transitions (RESPONSIVE ↔
ICMP_BLOCKED) are not edges, so ping-filtering networks get backoff
clearing without spurious forced polls.

**Constrains:** New reachability consumers use `data_path_up`; a new
HealthStatus member must define its `data_path_up` value at
introduction. Display concerns (the Status sensor's 10-level
cascade) stay granular and are exempt — this decision governs
reachability logic only. Provisional decisions get an entry in this
file at the moment they are made; a "v1"/"conservative" label in
code or tests is not a durable record.

---

## Testing Strategy

### HAR replay as integration tests

**Decision:** Each modem's `test_data/` directory contains HAR captures
(pipeline input) and expected output golden files (assertions). The
test harness replays each HAR through the `HARMockServer`, runs the full
pipeline, and compares output against the golden file.

**Rationale:** HAR captures are engine-independent HTTP recordings —
the most durable test fixture. Golden files capture reviewed output
against which all future runs are regression tests. If a `BaseParser`
implementation changes, all modems using that format are automatically
retested (regression firewall).

**Constrains:** Adding a modem requires a HAR capture and a reviewed
golden file, and no per-modem test code.

### Spec conformance gates every modem, not just confirmed ones

**Decision:** `test_modem_golden_spec_conformance` validates every
committed golden against PARSING_SPEC field contracts regardless of
`status:`. The earlier exemption for `awaiting_verification` and
`unsupported` entries is removed.

**Rationale:** The exemption made the check a promotion gate rather than a
regression gate: it fired once, at the moment a modem was declared
ready. Drift accumulated invisibly in onboarding entries and then
surfaced as a CI failure mid-confirmation, with a contributor waiting
— twice, on the XB7 (#107) and the XB6 (#111). Six entries had
accumulated non-canonical modulation output by the time the second one
hit. Catching a violation on the commit that introduces it costs the
author minutes; catching it at promotion costs a contributor days.

**Constrains:**

- A new catalog entry must be conformant on the commit that adds it.
  Landing drift and cleaning it up at confirmation time is no longer
  available, and downgrading `status:` no longer exempts an entry.
- Contributor PRs can now fail on a conformance rule during intake.
  The failure message names the field, the rule, and the value, and
  directs to the parser rather than the golden.

---

### Greenfield from specs, not migration from prior versions

**Decision:** No code reuse from prior versions. Build from the specs
using HAR files as the evidence base.

**Rationale:** Migration creates pressure to preserve old patterns.
"This test already exists, let's adapt it" leads to old assumptions
leaking in. A repeatable HAR-driven generation process is both cleaner
and more scalable — works identically for modem #1 and modem #33.

**Constrains:** Prior test intent (same modem, same input, same
expected output) is preserved through golden files, not through
migrated test code.

---

### HAR captures come from the `har-capture` utility

**Decision:** Fixtures are captured with `har-capture`, which launches a
fresh browser context.

**Rationale:** A fresh context carries no stale cookies or cached
session, so the capture contains the real auth exchange rather than a
session that was already open. A HAR captured against a live session
cannot prove how login works — see MODEM_INTAKE_WORKFLOW § Step 2.

---

## Onboarding

### Deterministic steps are code, Claude supplies judgment

**Decision:** Deterministic steps (HAR parsing, transport detection,
config generation, validation, test execution) are ordinary importable
modules in `catalog_tools`. Claude handles judgment calls (ambiguous
HTML formats, metadata web search, test failure diagnosis), driven by
the `modem-intake` skill. The user handles approval.

**Rationale:** Deterministic logic in code is repeatable and testable —
not dependent on LLM reasoning for correctness. The config constraints
(transport, auth, format) form the decision framework. Ambiguity is a
hard stop, not a guess. The split is what matters, not the delivery
mechanism: an earlier iteration exposed the same steps through an MCP
server, and carving `catalog_tools` out of Core replaced it with plain
modules a contributor can also call directly.

**Constrains:** HAR validation is a gate — post-auth-only HARs,
missing auth flows, and ambiguous transports halt analysis. No guessing.
Metadata not in the HAR is filled via web search with source
provenance.

### catalog_tools owns the onboarding spec

**Decision:** `ONBOARDING_SPEC.md` lives in catalog_tools' docs.
(Previously in Core's docs; moved in the v3.14 catalog_tools
carve-out.)

**Rationale:** The onboarding process generates Catalog content but
is executed by catalog_tools using Core's validation, test harness,
and schema definitions. Core remains the authority on what
constitutes a valid modem config; catalog_tools is the package that
*exercises* Core's schemas to produce content. The spec describing
the authoring pipeline belongs with the package that implements it.

**Constrains:** catalog_tools depends on Core's schema validators
and test harness. Core does not depend on catalog_tools; the
onboarding spec does not belong in Core's surface area.

---

### `enrich_metadata` separates inference from config assembly

**Decision:** `generate_config` assembles YAML from known facts.
Inferring facts from HAR analysis — `default_host` from request URLs,
DOCSIS version from channel types — belongs to `enrich_metadata`,
which reports inferred, missing, and conflicting fields.

**Rationale:** Two different concerns with different failure modes.
The split matters most for self-service contributors, who need to know
*what is missing* when validation fails rather than just that it did.

---

### `write_modem_package` owns file placement

**Decision:** A dedicated tool writes the catalog structure, rather
than the pipeline handing configs to the LLM to place.

**Rationale:** Guarantees the layout matches what the test harness
expects. File naming and directory structure are exactly the kind of
mechanical detail an LLM gets subtly wrong, and the failure surfaces
later as a confusing discovery miss.

---

### No fallback or auto-detection

**Decision:** If no `modem.yaml` matches, the integration cannot help.
There is no generic scraper fallback.

**Rationale:** A partial guess is worse than a clear failure — it
produces plausible-looking wrong data. The answer to an unsupported
modem is a HAR capture and a catalog entry, which is a path that ends
in real support rather than a permanent approximation.

---

## Extension Model

### How to add a modem

Add a directory under `modems/{manufacturer}/{model}/` with:

- `modem.yaml` — identity, auth, session, hardware, metadata
- `parser.yaml` — declarative extraction config
- `parser.py` — optional post-processor (only if needed)
- `test_data/modem.har` + `test_data/modem.expected.json` — HAR and
  golden file
- `test_data/modem.verified.json` — required to reach `confirmed`
  status; see MODEM_DIRECTORY_SPEC § Verification Status

No registration. No changes to Core or Catalog package code. Drop-in.

### How to add a format

Each format is described in **one place** — the section/source model
— via three ``ClassVar``s. Loaders, validators, and parser registries
all derive from the model lists; adding a format does not require
editing format-list frozensets in multiple files.

1. **`models/parser_config/{format}.py`** — define the model with:
   - `format_tag: ClassVar[str]` — value of ``format:`` in parser.yaml
   - `decode_kind: ClassVar[DecodeKind]` — `"html"`, `"json"`, `"xml"`,
     or `"hnap"` (drives the loader's body decoder and login-page
     detection)
   - `transports: ClassVar[frozenset[str]]` — which transports may
     select this format (drives cross-file transport/format validation)
   - The familiar Pydantic schema (`model_config`, `format: Literal["..."]`,
     fields)
2. **`models/parser_config/config.py`** (channel sections) **or**
   **`system_info.py`** (system_info sources) — append the model class
   to the list (`CHANNEL_SECTION_MODELS` or
   `SYSTEM_INFO_SOURCE_MODELS`). The discriminated union, the loader's
   decode dispatch, and the cross-file validator's transport map all
   derive from these lists.
3. **`parsers/formats/{format}.py`** — implement the `BaseParser`
   subclass.
4. **`parsers/registries.py`** — define the wrapper that adapts the
   parser to `tuple[list[dict], AnchorCount]` (channels) or
   `tuple[dict, AnchorCount, dict[str, str]]` (sysinfo), and add a
   `format_tag → wrapper` entry to `_CHANNEL_WRAPPERS_BY_TAG` (or
   `_SYSINFO_WRAPPERS_BY_TAG`). The model→callable dict is built by
   looking each model's `format_tag` up in that table, so ordering
   does not matter — a missing entry raises at import time.
5. **Regenerate the published tables** —
   `python scripts/generate_constraint_tables.py`. The valid-formats
   column of the constraint tables in ARCHITECTURE.md and
   MODEM_YAML_SPEC.md derives from `transports`;
   `tests/models/test_constraint_tables.py` fails if it is stale.

**Why ClassVars on the model.** Auth strategies already use this
pattern (`AuthStrategyBase` with `display_name`/`transport`/`stateless`
ClassVars + `_AUTH_MODELS` list). Format metadata is the same shape: a few
attributes that cross-cutting machinery needs to know about. Putting
them on the model keeps everything about a format colocated and lets
the loader, validator, and registry derive their views.

**Why wrappers stay in `registries.py`.** The wrappers contain
format-specific orchestration (channel_number assignment, multi-table
`merge_by`, unified-channel handling) that doesn't fit cleanly into
the BaseParser interface. Pulling them into format modules would
spread orchestration across N files; keeping them in `registries.py`
keeps that policy in one place. The `_CHANNEL_WRAPPERS_BY_TAG` dict
is the only per-format addition outside the model.

### Curated public-helper surface for parser.py

**Decision:** ``parser.py`` PostProcessors that need shared logic from
Core import it from a single curated module —
``solentlabs.cable_modem_monitor_core.post_processor_helpers`` — and
nothing else. The parser-sandbox validator allowlists this exact
fully-qualified module path; all other Core paths remain forbidden.

**Rationale:** The sandbox is what keeps PostProcessors honest (no
network I/O, no auth state access, no orchestrator peeking). Allowing
unrestricted Core imports defeats it; banning all Core imports forces
DRY violations when a primitive (e.g., `transpose_indexed_rows`) is
useful both in a Core format and in a firmware-quirk PostProcessor.
A single audited public surface threads the needle: helpers added
there are reviewed for parser.py safety, and the sandbox enforces
that no other Core path can be reached.

**Constrains:** Adding a helper means importing/defining it in
`post_processor_helpers.py` and listing it in `__all__`. Renaming or
removing one is a breaking change for catalog `parser.py` files —
keep churn low. Helpers that need network or stateful orchestrator
access do not belong here.

### How to add an auth strategy

1. **`models/modem_config/auth.py`** — three co-located additions:
   - Define the model class inheriting from `AuthStrategyBase` with
     `display_name`, `transport`, and `stateless` ClassVars.
     `stateless` is true only when the strategy establishes no
     server-side session: `none` sends no credential and `basic`
     re-sends it on every request, so neither holds anything that can
     expire mid-poll. Everything else obtains a session artefact at
     login and is stateful, which is what turns on login-page
     detection.
   - Add an `Annotated[NewAuth, Tag("strategy_name")]` member to the
     `AuthConfig` union.
   - Add the class to the `_AUTH_MODELS` registry list (immediately
     below the union).
2. **`auth/{strategy}.py`** — new manager module with a
   `create_manager(config)` entry point.
3. **`test_harness/auth/{strategy}.py`** — new handler module with a
   `create_handler(modem_config, har_entries)` entry point.
4. **Regenerate the published tables** —
   `python scripts/generate_constraint_tables.py`.

Three files plus a regeneration, all additive. No factory code changes
— the factory dynamically imports the manager module by strategy
literal. Display labels, transport validation sets, and login-page
detection derive from the model ClassVars. No existing strategy code
is touched.

**If you forget a step:** missing union member → modem.yaml fails to
parse at config load time. Missing `_AUTH_MODELS` entry → display
label and transport validation missing, caught by fleet test. Skipped
regeneration → `tests/models/test_constraint_tables.py` fails. No
failure is silent.

**Strategy selection is config-driven** — declared in modem.yaml,
never guessed at runtime. The dynamic import resolves a declared
strategy to its implementation; it does not discover which strategy
to use.

**If the strategy pre-fetches a login page, use the response.** Several
strategies GET a page as part of the auth handshake and extract
session-specific state from it — hidden fields, crypto parameters,
cookies, or tokens. The pre-fetch establishes session cookies as a side
effect, but its primary purpose is reading the data the handshake
needs. Discarding the response body is a bug.

### How to add a transport

Add a new loader (new value type), new `BaseParser` implementation(s)
that consume that type, a new action model, the transport literal on
`ModemConfig.transport`, and a `_TRANSPORT_PROSE` entry in
`scripts/generate_constraint_tables.py` for the two columns no model
carries (loader and session). Then regenerate. No existing code
changes.

---

## Config Flow

### Cross-directory grouping for same-model, different-transport entries

**Decision:** `list_modems()` groups catalog directories by
`(manufacturer.lower(), model.lower())`. The first directory encountered
in sorted path order becomes primary; additional matches are attached as
`sibling_dirs`. The config flow passes `sibling_dirs` to `list_variants()`,
which scans all directories and returns a flat combined list. Transport is
a variant dimension, not a dropdown dimension.

**Rationale:** The MODEM_YAML_SPEC rule "different transport = different
directory" is correct and must not change — directories are independently
testable. But users should not have to know the catalog directory structure.
Grouping at the `list_modems()` layer keeps each directory self-contained
while presenting a single logical entry in the UI.

**Constrains:** Variant dropdown values use composite keys
(`"{rel_dir}/{name|__default__}"`) to prevent collisions when multiple
directories each contribute a default variant. The selected directory is
stored as `_selected_modem_dir` on the config flow and used for all
downstream steps (auth strategy resolution, connection validation, config
entry storage).

### The variant name is the user-facing discriminator

**Decision:** The variant picker labels each entry with its auth
strategy and the variant's own name, taken from the filename stem.
`hardware.hw_version` is added only when two variants would otherwise
read identically. `hardware.firmware` is catalog metadata and is never
shown.

**Rationale:** Variants exist to express different auth contracts, not
different hardware, so labelling by hardware version asked users to
choose on an axis that decides nothing. The SB8200 config built from a
v6 capture ran clean end to end on v7 hardware: one config serves both
board revisions, and the version in the label was noise that led
contributors to pick the wrong variant (#124). Hardware version is
also weak as data. Of the three Arris S33 units captured, two report
no hardware version at all and the third reports a value the catalog
does not use.

**Constrains:** A variant's filename stem is user-facing text, so it
must name what really sets the variant apart. A misleading stem is
renamed rather than relabelled; there is deliberately no
label-override field, since
that would only dress up a bad name. `hw_version` stays recorded, and
earns a place in the label only where variants genuinely differ by
hardware alone (the S33 generations).

### Brand names as manufacturer-step choices

**Decision:** One catalog record per physical product. `manufacturer:`
stores the maker as the firmware reports it and determines the modem's
directory (`modems/{manufacturer}/{model}/`). `brands:` stores the
user-visible brand names from the product/box. The config flow's
manufacturer dropdown is built from the union of `manufacturer` values
and `brands` entries, so a rebranded modem appears under every name a
user might look for while remaining a single record.

**Rationale:** The G54 (#72) is made by CommScope (firmware
`manufacturer` field) but sold under the Arris brand (firmware
`customer` field, box branding). Filing it under CommScope only made it
undiscoverable for a user holding an Arris box; duplicating the entry
under Arris would fork one product's evidence trail (HAR, golden files,
verification status) across two records. The rule: disk and
`manufacturer:` reflect what the hardware reports; dropdowns reflect
what the user sees.

**Constrains:** Rebrands never get a second catalog entry —
MODEM_YAML_SPEC § Aliases vs Separate Entries governs. `brands` entries
must be sourced (box, marketing page, or firmware fields such as
`customer`). The model line's parenthetical shows alternate user-facing
names — `model_aliases` ∪ `brands`; `model_aliases` is not a dropdown
dimension, and firmware-internal identifiers (product codes, platform
strings) belong in neither field. Manufacturer display casing is
presentation-only and must preserve deliberate mixed case (CommScope,
not Commscope).

Labels are bucket-contextual: within a brand bucket the model line
leads with the brand name, and the parenthetical lists aliases and
other brands (`Xfinity XB6 (CGM4140COM)` under Xfinity), adding the
manufacturer-composed name only when no alias anchors the entry
(`Arris G54 (CommScope G54)` under Arris); within the manufacturer
bucket and the All view it leads with the manufacturer
(`CommScope G54 (Arris)`). The lead always matches the filter the user
chose.

---

## References

| Document | Purpose |
|----------|---------|
| `ARCHITECTURE.md` | System design, component contracts |
| `MODEM_YAML_SPEC.md` | modem.yaml schema |
| `MODEM_DIRECTORY_SPEC.md` | Catalog directory structure |
| `PARSING_SPEC.md` | Extraction formats, parser.yaml/parser.py |
| `RESOURCE_LOADING_SPEC.md` | Resource dict contract, loader behavior |
| `ORCHESTRATION_SPEC.md` | Orchestrator, collector, health monitor, restart monitor — interface contracts and data models |
| `ORCHESTRATION_USE_CASES.md` | Scenario-based use cases — normal ops, auth failures, connectivity, restart, health, lifecycle |
| `RUNTIME_POLLING_SPEC.md` | Poll cycle, session lifecycle, error recovery |
| `../../cable_modem_monitor_catalog_tools/docs/ONBOARDING_SPEC.md` | Catalog Tools modem onboarding |
| `FIELD_REGISTRY.md` | Field naming authority |
| `VERIFICATION_STATUS.md` | Parser status lifecycle |
| `../../../custom_components/cable_modem_monitor/docs/CONFIG_FLOW_SPEC.md` | Setup wizard |
| `../../../custom_components/cable_modem_monitor/docs/ENTITY_MODEL_SPEC.md` | Core output → HA entities |
| `../../../custom_components/cable_modem_monitor/docs/HA_ADAPTER_SPEC.md` | HA wiring — runtime data, coordinators, polling modes |
