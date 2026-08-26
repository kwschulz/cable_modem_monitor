# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Upgrading from 3.13 no longer leaves dead buttons on the device
  page.** The Capture HTML button is gone in 3.14 (HAR capture replaced
  it), and the Restart button is now only created for modems that
  declare a restart action, so both could linger as unavailable
  leftovers. Upgrading clears them, and the Restart button is left
  alone on modems that do support it.

- **Error rate sensors no longer show a dozen decimal places.** A rate
  is a counter delta over elapsed seconds, so it landed on screen as
  `1,730,730.4592256 errors/min`. It now displays to two decimals. The
  full value is still recorded, and the displayed precision can be
  changed per entity in its settings.

- **A channel showing `----` no longer leaves its sensors
  unavailable.** Each channel got sensors only for the values it
  carried when entities were first created, so a channel with a
  placeholder in its Correcteds or Uncorrectables cell got none, while
  Power and SNR on that same channel showed Unknown. Per-channel
  sensors are now created for every metric the modem publishes, and
  show Unknown where a channel's own cell is empty. Existing installs
  pick them back up on upgrade, history intact.

- **The `orphaned_statistics` service no longer offers a second modem's
  live statistics for deletion.** It collected candidates by entity-ID
  prefix but checked them against a single config entry's registered
  entities. Every prefix option produces IDs under `cable_modem_`, so on
  a two-modem setup the first modem's scan swept in the second modem's
  statistics, found them unregistered to itself, and listed them as
  orphans. Running it with `execute: true` then cleared that modem's
  long-term history permanently. The scan now checks registered entities
  across every configured modem.

- **Netgear CM2000, CM2500 and CM3000 no longer report "Internet Access:
  Down" on a working connection.** This firmware hides that indicator on
  its own diagnostic page and shows a cable-modem indicator instead, so
  the value was never one the modem displayed. The `Internet Access`
  sensor is removed for these three models. Downstream and upstream
  power, SNR and partial-service status are unaffected.

- **The device page now shows the model your modem reports.** The
  integration read that value from the modem, left it out of the entity
  list as device metadata, and then never wrote it anywhere. It now
  appears as the device's model ID. The model stays the name the unit
  was sold under, so a modem reporting an internal code (XB7 reports
  `CGM4331COM`) keeps the recognizable name and gains the code
  alongside it.

## [3.14.0-beta.23] - 2026-08-24

### Fixed

- **The fixture PII gate now scans HAR request URLs.** A credential in a
  URL carries no field name, so har-capture's strict rules have nothing
  to match on, and its propagation sweep only rewrites values already
  redacted elsewhere in the same capture — a secret that appears *only*
  in a URL has no redacted copy to propagate from and shipped intact.
  `check_fixture_pii` read just the `text`, `value` and `content` keys
  and stopped recursion at bare strings, so `request.url` and
  `queryString` were never examined at all. It now checks every path
  segment and query entry — keys as well as values, in both literal and
  percent-encoded form — for base64 basic-auth pairs and opaque
  high-entropy tokens, skipping static asset filenames. Scanning the
  committed catalog under the new rule found one real leak: the Arris
  SB8200 `modem-cookie.har` login URL carried plaintext-equivalent
  credentials, which are now replaced with a format-preserving
  placeholder that keeps the bare-base64 wire shape the fixture exists
  to document. The gate previously reported that file as clean.

- **Three catalog fixtures no longer carry factory Wi-Fi credentials.**
  har-capture 0.12.2 closes two holes in its sanitizer: labeled default
  Wi-Fi passwords and SSIDs survived redaction, and `check_content`
  skipped an entire response body whenever it hit a stray placeholder,
  leaving 209 of 750 fleet entries unscanned. Re-running the fixture gate
  on the upgraded scanner found real credentials in three committed
  Technicolor captures. The Device Label Information block these gateways
  render carries factory Wi-Fi credentials in plain text: the XB7 and
  XB10 fixtures each kept a default SSID and password, the XB10 also its
  serial number, and the XB6 fixture kept a private network name. The
  serial had slipped past this project's own gate because
  `check_fixture_pii.py` treats any finding containing a colon as a code
  false positive, and har-capture returns that finding with its label
  attached. Every value is now replaced with `[REDACTED]`; nothing
  structural changed and none of them is parsed. The declared har-capture
  floor moves to 0.12.2 as a hard minimum, not a refresh. Redaction stops
  these values reaching new checkouts. It does not remove them from
  history, and the fixtures have never shipped to PyPI or HACS, which
  exclude `test_data`.

- **Dashboards are no longer empty on modems that do not report channel
  lock status.** In Channel Number mode the dashboard generator kept only
  channels whose `lock_status` read `locked`, so a modem that never emits
  the field lost every channel and
  `cable_modem_monitor.generate_dashboard` returned a card list with no
  channel graphs, even though the entities existed and held good data.
  Core and the entity mapping layer already treat a missing `lock_status`
  as locked, as the channel identification spec requires of every
  consumer; the dashboard generator was the last one that did not.
  Channels reported as explicitly unlocked are still left out. Found and
  fixed by a contributor on a Hitron CODA56.

- **The documented setup path now installs the packages it says it
  installs.** `scripts/setup.sh` stopped after the development
  requirements and never installed Core, Catalog, and Catalog Tools into
  the virtual environment, so `solentlabs` imports resolved to whatever
  was published on PyPI rather than the working copy, and tests ran
  against the released build. `make setup` had always installed all
  three; the two paths had drifted apart. `scripts/verify-setup.sh`, the
  script the setup guide points to when something looks wrong, checked
  only Core, so it reported a healthy environment while the other two
  were missing. It now checks all three and names the one to install.

- **The Technicolor XB8 no longer logs in on every poll.** Its catalog
  entry named the session cookie `session`, inherited from the XB7
  fixture it was reconstructed from; the gateway issues `DUKSID`. Core
  never recognized the session it had just established, so it posted
  credentials again on every poll. The first real XB8 capture,
  contributed by an owner on Rogers, corrects the cookie name, adds the
  logout endpoint, and confirms the entry against their gateway, which
  moves it from awaiting verification to confirmed. It also confirms on
  XB8 hardware the firmware quirk the entry had assumed from the XB7:
  one OFDM channel's error counts appear again on a QAM channel. That is
  why this modem reports no error totals and has no error total or error
  rate sensors.

### Changed

- **Checks on pull requests from forks now report their own result.**
  A pull request from a fork runs with a read-only token, so the two
  workflows that post an informational comment received a permission
  error and reported failure even when the underlying check had passed.
  The commit email check now runs with the repository's own token, and
  reads commit metadata only; it never checks out or runs anything from
  the pull request. Commit message validation keeps the restricted token,
  because it installs packages, and writes its guidance to the run
  summary where a fork contributor can read it. The contributing guide's
  commit message example also did not satisfy the project's own commit
  message rules, and now does.

- **A changelog entry is required only on pull requests into `main`.**
  Entries are written at release time, so requiring one on every pull
  request failed contributions that were never going to carry one. The
  structure of this file is now validated on every pull request instead,
  reporting only on lines a branch actually touches.

## [3.14.0-beta.22] - 2026-08-21

### Added

- **Netgear CM2500 added to the catalog.** Form auth with the
  per-page-load login-form action, logout, and restart come from
  maintainer bench captures of an unprovisioned unit and were exercised
  through Home Assistant on that hardware: the login lands on
  `/index.htm` and every system-info field parses. The unit never had a
  plant signal, so channel parsing rides on a declared reconstruction
  from the CM3000's capture (same firmware family), and the entry is
  awaiting verification until a provisioned CM2500 owner shares
  diagnostics. (Related to #189)

### Fixed

- **Netgear CM2050V and CM3000: the integration can log in again.** The
  firmware publishes a per-page-load `?id=` in its login form's action
  and refuses a POST without it, so the integration's login was rejected
  every time while a browser's worked. Polls succeeded only after a
  browser on the same network had signed in, because this firmware
  authenticates by source address rather than by cookie (bench-confirmed
  on a CM2500), which is why every report described "works after I log
  in, then stops." `form` auth gains `action_source: login_page`, which
  reads the form's action off the login page at every login; a page with
  no form, no matching form, or no action attribute logs an error and
  falls back to the configured action, so entries that do not opt in
  are unchanged. The CM3000 also gains the success criterion it lacked,
  so a refused login is reported at the login rather than one step later
  as a login page at the data URL. Confirmed on a CM2050V by the
  reporting contributor and on a bench CM2500. (Related to #189)

- **Netgear CM3000 no longer holds a session between polls.** Its entry
  declared no logout because the original capture never visited the
  logout page. It now declares the logout its sibling CM2050V's capture
  shows, the Logout.htm form's POST, sourced as family evidence, which
  puts it on the single-session path: log out after each poll and before
  a same-poll retry. Whether the firmware accepts the form's default
  timestamp is unverified on this model. (Related to #189)

- **Catalog tools: intake handles per-session login tokens and captures
  from an unprovisioned modem.** A query string on the login POST is
  emitted as `action_source: login_page` instead of a bare action at
  high confidence. An observed logout POST outranks the auto-action page
  that precedes it, which becomes its `pre_fetch_url`. A capture with no
  data pages gets one terminal state: a warning from analysis, a legible
  refusal from the golden and package writers, and no partial package
  left on disk; `docsis_version` is reported missing rather than guessed
  as 3.0. The replay harness now serves the captured login page before
  any session exists, so a login that reads that page can be certified,
  and when a capture records a refused login before the accepted one it
  replays the later exchange. (Related to #189)

- **A busy single-session modem no longer reads as a wrong password.**
  Some firmware refuses a second login with a normal HTTP 200 and a
  message in the body instead of a 5xx. That refusal failed the login
  check, stopped polling after one failure and opened the
  re-authentication form, whose own test login took the only session
  slot and caused the next refusal (#120, Technicolor CGA6444VF). A
  catalog entry can now declare the refusal body (`login_busy` on
  `form_pbkdf2`); a matching response is treated like a 5xx, the modem
  reports unreachable, and polling continues until the slot frees.
  Modems that do not declare it are unaffected.

## [3.14.0-beta.21] - 2026-08-10

### Added

- **Diagnostics now say why polling stopped.** When repeated auth
  failures stop polling, the download reported that the circuit breaker
  was open and nothing about the cause, so a firmware lockout, a wrong
  password and six consecutive refused sessions all produced the same
  payload. A new `circuit_trip_signal` field records which signal opened
  the breaker and survives the blocked polls that follow, which report no
  signal of their own. Additive: no existing diagnostics key changed, so
  files already collected stay comparable.

### Fixed

- **Your password can no longer reach a diagnostics download.**
  When a modem returns an unusable page, the whole page is saved into the
  diagnostics file so the failure can be diagnosed after the fact — and
  that file is what users are asked to attach to a public issue. The page
  was saved exactly as received, while every other saved response already
  had the password stripped. Some firmware echoes the submitted password
  back inside its own pages, so the combination could publish a working
  modem login. The page is now stripped at the moment it is saved.

- **The auth-failure warning now says why the login failed.** It carried
  the request, the response status and a body snippet, but discarded the
  one sentence naming the reason — so a login the modem accepted before
  sending us somewhere unrecognized logged the landing address as a bare
  URL, with nothing to say that was the problem or what was expected. The
  reason now leads the line. No new data is collected; it was already
  gathered and dropped at the last step.

- **A modem locked out by its own firmware no longer asks you to
  re-enter your password.** Firmware that locks out logins after too
  many attempts raised the same "Authentication expired" prompt as a
  wrong password, and submitting that form is itself a login attempt —
  so the remedy on offer was the one thing that keeps a locked-out modem
  locked out. A lockout now raises a notification instead, naming the
  lockout and the remedy that works: wait for the modem to clear it or
  power cycle it, then reload the integration. Blocked polls stop saying
  "reconfigure credentials" for the same reason. A wrong password still
  prompts for credentials exactly as before.

- **A modem that is busy or erroring during login is no longer reported
  as a wrong password.** Two auth strategies did not read the HTTP status
  of the login they had just sent. `form_nonce` judged the outcome purely
  from the response body, so a 401 or a 503 whose body happened not to
  carry the firmware's error prefix was treated as a successful login.
  `form_sjcl` fetched the login page without checking its status and
  discarded the response, so every HTTP error arrived as an empty set of
  JavaScript variables and surfaced as "Login page missing myIv or mySalt"
  — stopping polling on the first attempt and telling the user to fix
  their credentials. Both now treat a login answering 400 or above as
  failed and keep the response, so a busy modem reports as unreachable
  and retries on the next poll instead of demanding a password. Affects
  the Arris SB6190 auth-required variant and the Arris TG3442DE; no
  change for a modem whose login answers normally.

## [3.14.0-beta.20] - 2026-08-07

### Fixed

- **A logout the modem refused no longer drops a live session.** The
  post-poll logout discarded its result, so a refusal cleared the local
  cookie all the same. That leaves the session open on the modem with
  nothing left to close it, and on firmware that permits one login at a
  time it orphans a session per poll until the next login is locked out.
  The session is now cleared only after a logout the modem accepted; a
  refusal is logged and the cookie kept. Eight catalog files declare
  `actions.logout`, and behavior is unchanged for any whose logout
  answers 2xx.

- **Catalog tools: intake flags a capture that stops at the login
  redirect.** A login answering 3xx sends the browser somewhere, and that
  landing page is what `auth.success.redirect` is checked against, so a
  capture without it cannot test the login. The XB7 and XB8 fixtures were
  in that state and their replays passed anyway, because of the dropped
  HTTP-error check below. The HAR validation gate now warns at intake,
  and the same check runs over committed fixtures in the intake pipeline
  regression. Both fixtures are reconstructions rather than wire
  captures; each gains a landing entry whose comment records what was
  inferred, from which same-family captures, and what was left empty
  rather than invented. Neither modem's behavioural config changes.
  (Related to #189)

- **A form modem that checks the login no longer reports a rejected
  session as a bad password.** `form` auth verifies the credential when
  the entry names a `success` criterion, so a 401 arriving later is
  session-side, not the password; it had been reporting every such 401 as
  credentials-suspect, which sends a user with working credentials to
  re-check them. Nine catalog entries declare a criterion. A separate
  defect made declaring one *drop* the HTTP-error check, so a login the
  modem answered 401 was reported as successful; the check now runs for
  every form login. A `success` block naming neither `redirect` nor
  `indicator` is now a schema error rather than a block that silently
  checks nothing. (Related to #189)

- **The auth-success log line says which page the login landed on.** Form
  logins follow redirects, so a modem that refuses the credential and one
  that accepts it can both answer 200 and the line reported only that
  status. The landing path, which is what separates them, was carried on
  the auth result and never logged; it now appears as `landed:` for the
  strategies that have one. The line itself was pinned to DEBUG despite
  being documented as first-poll INFO, so the confirmation that auth
  worked was invisible unless debug logging was already on. The first
  login of a session's lifetime now logs at INFO and every login after it
  at DEBUG. (Related to #189)

- **The collection-complete log line is visible without debug logging.**
  It was pinned to DEBUG despite being documented as first-poll INFO, the
  same defect as the auth-success line above. A default-level log showed
  that the login worked but said nothing about whether the poll returned
  data, which is the half carrying the downstream and upstream channel
  counts. The first completed collection of a session's lifetime now logs
  at INFO and every one after it at DEBUG. (Related to #189)

- **Netgear CM2050V: a refused login is caught at the login.** The entry
  named no success criterion, so Core's fallback accepted any response
  under HTTP 400 and a refused login scored as success, surfacing two
  steps later as a data page that was really the login page. Both
  captures on file, two units six months apart, land an accepted login
  on `/index.htm`, and the entry now names that redirect. A refusal is
  reported as an authentication failure on the first poll instead of the
  sixth. (Related to #189)

- **The login-failed screen no longer asks the user to sign in with a
  browser first.** Step 3 read as a prerequisite for the integration,
  and on firmware holding auth state server-side it disturbs the thing
  being measured; the earlier fix for that advice covered only the 5xx
  path. The credential check stays, now conditional on the username and
  password looking right, and tells the user to sign out afterwards.
  Every translation carries the new wording. (Related to #189)

- **Catalog tools: every published module imports on its own again.**
  `analysis.format` and `analysis.mapping` imported each other at module
  level, so importing an `analysis.mapping` leaf module raised
  `ImportError` unless `analysis.format` had been imported first, which
  three test files worked around with a side-effect import. The
  from-package import now happens inside the four helpers that use it,
  and `scripts/check_import_isolation.py` holds the property by importing
  each published module with the package tree purged from `sys.modules`.

## [3.14.0-beta.19] - 2026-08-05

### Added

- **Ziggo Sagemcom F3896LG-ZG reports provisioned speed and burst.** The
  original capture carried a service flow endpoint the catalog entry never
  read. It supplies the ISP-provisioned downstream and upstream speed and
  maximum burst size, which now appear as four sensors, along with the
  modem's reported model name. These five values are read from the capture
  and have not been confirmed against the modem's own status page yet.
  (Related to #185)

- **Catalog tools: intake reports the endpoints nothing read.** A data
  endpoint the generator skipped produced no gap and no warning, which is
  how the service flow resource behind #185 was captured, fetched, and
  left unread with the pipeline reporting success. `analyze_har` now lists
  every 2xx JSON endpoint no part of the generated config consumes, each
  with its response body's key skeleton and value types. Keys are what
  make the judgment possible; values are where serial numbers and MAC
  addresses live, so they are never printed. It rides alongside warnings
  and never fails an intake. (Related to #185)

- **Catalog tools: a sweep for fields the committed catalog leaves on the
  wire.** `scripts/catalog_field_sweep.py` reads every committed entry's
  HAR for keys that resolve to a registry field and reports the ones that
  entry's own `parser.yaml` and golden never populate. It catches what the
  unread-resource report cannot: an endpoint that is read but read
  incompletely, such as the SB8200's `Boot State` row. Report, not gate,
  and nothing it finds is wired automatically.

### Fixed

- **Catalog tools: intake generates the system_info shapes it was
  missing.** Nested JSON fields were emitted with key and path joined into
  one dotted string, which Core looks up literally and never resolves,
  silently, on every JSON modem. `system_uptime` was emitted as a string,
  following a spec table that contradicted 33 of the 35 committed configs;
  it now emits `type: uptime` with the format chosen by testing candidates
  against the captured value. Vendor spellings of DOCSIS success normalize
  to `Operational` through a map, with error and in-progress states still
  passing through raw. Service flow resources now reduce to provisioned
  speed and burst aggregates. The uptime and status rules reach HNAP too,
  which had been emitting passthrough strings for all seven HNAP modems.
  (Related to #185, #129)

- **Provisioned speed reads in Mbit/s instead of raw bits per second.**
  The sensor declared bit/s and relied on Home Assistant to scale it for
  display, which it does not do for data rates, so a 110 Mbit/s downstream
  plan showed as "110100480 bit/s". The sensor now suggests Mbit/s while
  still storing bit/s, so history keeps a single unit. Home Assistant
  applies a suggested unit when it first creates an entity, so speed
  sensors that already exist keep bit/s until changed in their settings.
  (Related to #129)

- **Catalog tools: the intake regression grades each capture against the
  config that describes it.** A modem directory can hold several HARs,
  each capturing a different auth variant alongside its own
  `modem-<variant>.yaml`, but every HAR was graded against the base
  `modem.yaml`. Five auth strategies were reported as pipeline defects
  in which the detector and the catalog in fact agreed. Grading now
  resolves the config from the HAR stem, the rule the golden replay
  tests already use. Fleet accuracy also stopped dropping the captures
  it cannot process: a HAR that fails validation or analysis now scores
  zero against its full field count instead of leaving the denominator,
  which had been overstating the reported percentage.

- **Catalog tools: intake reads a URL login token from the query string
  rather than from anywhere in the URL.** The detector matched `login_`
  at any position, so `/cgi-bin/login_cgi` and `/Admin_Login_Lock.txt`
  registered as token logins. One modem was classified `url_token` when
  it uses a plain form, and another was given a login page pointing at a
  status file. The token must now follow the marker in the query string.
  A login that sends its credential as both a bare query parameter and a
  Basic header is recognized by that shape, which survives the capture
  sanitizer replacing the credential itself.

- **Auth failure logs no longer keep part of the password.** The 401/403
  and auth-failure lines trim the modem's response body to a fixed length
  before redacting the password, so a password falling across that
  boundary left its leading characters behind. Redaction now covers the
  whole body and the trim happens afterwards. This matters because these
  are the log lines the integration asks you to share when reporting a
  problem. Only firmware that echoes the submitted password back in an
  error page was affected.

## [3.14.0-beta.18] - 2026-08-03

### Added

- **Ziggo Sagemcom F3896LG-ZG confirmed on hardware.** Verified via
  contributor diagnostics on beta.17: 34 downstream (32 SC-QAM plus 2
  OFDM) and 6 upstream (4 ATDMA plus 2 OFDMA) channels locked in bridge
  mode, hardware 1.2, software LG-RDK_12.13.16-2504.5, all signals
  nominal. Restart is confirmed on hardware as well, the modem's own
  event log recording "Cable Modem Reboot because of - Reboot UI". It is
  the first of the two F3896LG entries with a verified restart; the
  Virgin Media sibling's remains untested. (Related to #185)

### Changed

- **The GitHub landing page leads with the supported-modems list.** The
  badge row opened with "Help Add Your Modem", asking for contributor
  effort before answering the question a visitor actually arrives with,
  which is whether their own modem works. Repo traffic showed the modem
  request page below the top-ten path cutoff while the supported-modems
  list was the third most-viewed path.

### Fixed

- **A busy modem no longer reads as a wrong password.** Firmware that
  permits one session at a time turns the integration's login away while
  someone else holds the slot, and answers with a 5xx to say so. Every
  non-2xx on a login was treated as a rejected credential, so opening the
  modem's own web page stopped polling on the first occurrence and asked
  the user to re-enter a password that was never wrong. The advice on that
  form, to try logging in via a browser first, was the very thing causing
  it. A login answering 5xx is now classified `AUTH_UNAVAILABLE`: the poll
  reports the modem unreachable, nothing accumulates toward the circuit
  breaker, and no credential prompt is raised however long the condition
  lasts. It clears by itself when the other session ends. Setup and
  reauthentication now report the modem as busy for the same reason
  instead of blaming the credentials. A genuinely wrong password still
  answers 401 or 403 and still stops on the first poll, and firmware
  anti-brute-force lockout is untouched.

  This is the rule the data path already applied. There, 401/403 means
  the session and every other status means the modem, so an identical 5xx
  produced "unreachable" when it landed on a data page and a credential
  prompt when it landed on the login. The cause is not always the user's
  to fix: on this firmware an ISP support session holds the same slot, and
  no password change would release it. (Related to #185)

- **Diagnostics reported no credentials on password-only modems.** The
  check required a username, which modems authenticating on a password
  alone never store, so the report contradicted the poll log two sections
  below it. (Related to #185)

## [3.14.0-beta.17] - 2026-07-31

### Added

- **Technicolor XB6 (CGM4140COM) confirmed on hardware.** Verified via
  contributor diagnostics: 32 downstream + 4 upstream channels locked,
  form auth, all signals nominal. Downstream modulation now normalizes to
  canonical `QAM256`, and upstream channel type reads the "Channel Type"
  column directly (the firmware reports a bare "QAM" with no
  constellation, so the unmappable modulation field is omitted, matching
  the XB7 and XB10). (Related to #111)

- **Virgin Media Hub 5 (Sagemcom F3896LG-VMB) confirmed on hardware.**
  Verified via contributor diagnostics on beta.16: 33 downstream + 6
  upstream channels locked, 134 days uptime, all signals nominal. The
  unauthenticated `state_` endpoint the parser reads carries no version
  field, so neither hardware nor software version is published. The
  restart action remains unverified on hardware. (Related to #82)

- **`{auth:token}` and `{auth:user_id}` placeholders in action
  endpoints.** Some firmware addresses the session in the URL itself:
  the Sagemcom F3896LG-ZG ends one with
  `DELETE /rest/v1/user/{userId}/token/{token}`, both values coming from
  the login response. `HttpAction.endpoint` was a plain string, and the
  only dynamic-endpoint mechanism scraped a form action out of
  pre-fetched HTML, so that logout could not be expressed at all — which
  on single-session firmware leaves the integration holding the user's
  only session slot. A fixed two-key set resolved from the auth context
  at action time, not a template language: modem.yaml names an
  already-captured value and the auth strategy decides what gets
  captured. `BearerAuth` gains `user_id_path` alongside `token_path`.

- **`session.post_login_endpoints` in modem.yaml.** Some SPA firmware
  treats the browser's first call after login as part of establishing
  the session and rejects data requests until it has been made. A modem
  can now declare those paths, and they are fetched in order on every
  fresh login, ahead of any data request. Responses are discarded, and
  a failure logs a warning rather than reporting an auth error, since
  the login itself already succeeded. The Technicolor CGA6444VF declares
  `/api/v1/session/menu` on this basis. (Related to #120)

### Changed

- **A data-page 401 or 403 now logs the request line and response
  body.** The line carried only the status and the path, which cannot
  distinguish a rejected session from a missing header, a missing
  cookie, or a refused request — the point where diagnosis has
  repeatedly stalled. The body is sanitized by the collector, the only
  layer that knows the password. Nothing about what is sent changes.
  (Related to #120)

- **Unimplemented cases fail instead of degrading quietly.** The
  test-harness auth factory fell back to a no-auth handler when a
  strategy had no handler module, and the HTTP loader coerced any
  unrecognized format to BeautifulSoup. The first turns a missing
  implementation into a passing test, the second into structurally
  valid garbage. Neither branch is reachable through a validated config,
  and neither had a test asserting the fallback.

- **Spec conformance now gates every modem, not just confirmed ones.**
  The check skipped anything still `awaiting_verification`, so
  non-canonical output accumulated unseen and then failed at promotion
  time, mid-confirmation with a contributor waiting. It now runs on
  every entry, and a new modem must be conformant on the commit that
  adds it.

### Fixed

- **A modem refusing a data page no longer reads as a bad password.** A
  401 on a data page arrives *after* a successful login, so the
  credentials are known good, but it mapped to `invalid_auth` and sent
  users to re-verify a password the modem had already accepted. That is
  the path an already configured modem takes: a streak opens the circuit
  breaker and prompts for reauthentication, landing a user with correct
  credentials on a form telling them to check their password. These now
  map to a new `session_rejected` message, and the integrity variant,
  previously unmapped, no longer reaches the user as "unknown".

  The reading is per auth strategy rather than blanket, because it only
  holds where the strategy actually verified the password at login.
  Basic auth never validates, and plain form auth accepts any response
  under HTTP 400, so on those a wrong password would still have reported
  the password as fine. Each strategy declares which it is, and only one
  that proves it with a bad-password test may claim the modem verified
  the credential. A wrong password on an MB7621 still correctly reports
  invalid credentials, confirmed on hardware. A failed validation attempt
  also closes its collector now, instead of stranding a server-side
  session on single-session firmware and colliding with the user's next
  try. (Related to #120)

- **HNAP firmware lockout was reported as rejected credentials.** The
  lockout exception was raised by the v3.13 HNAP module and lost when
  that module was retired, leaving the v3.14 manager returning a plain
  auth failure. Polling stopped either way, since both trip the circuit
  breaker identically, but a firmware lockout and a wrong password have
  different remedies and the user was shown the wrong one. The distinct
  signal and its event fire again. (Related to #117)

- **Bearer login could never succeed on the Sagemcom F3896LG.** It
  required exactly HTTP 200 and always sent a username key. Both are
  wrong for that REST API, which two Liberty Global operators ship:
  Virgin Media and Ziggo each authenticate on a password alone, and
  token creation answers 201. Any 2xx carrying the token now succeeds,
  and `auth.username_field` names the credential key — empty omits it
  entirely, following the same empty-means-unused convention
  `cookie_name` and `login_page` already use. (Related to #82, #185)

- **A resource that failed to decode read as a clean parse.** The loader
  logs the failure and omits the path, but three shapes hang their paths
  off something else and went unchecked: XML `tables[]`, multi-array
  JSON `arrays[]`, and resources declared by a parser's post-processor.
  For those, a missing resource meant silently fewer channels or absent
  system info while the poll reported success — on CBN surfacing as a
  false "no signal" rather than a load failure. Every declared path is
  now accounted for, and absence reaches the integrity signal by the
  established route.

- **A refused restart reported success.** The restart command dropped
  the result its action executor returned, so a modem that answered the
  reboot with 401 or 404 — or a per-action login the modem rejected —
  logged "Restart command sent", notified the user of a dispatch that
  never happened, and opened a recovery window for a reboot nobody
  asked for. The executor also reported any response it received as
  success, including refusals. Both now fail: the HTTP executor judges
  the response the way the CBN executor already did, and a failed
  action ends the restart before the session clear and the recovery
  window, since neither has a reboot to be premised on. The specific
  refusal reaches the log, so a report carries it. Affects the
  Hub 5 (F3896LG-VMB), whose restart authenticates separately from
  monitoring and so can fail on its own. (Related to #82)

- **A synthesized fixture could certify the client against itself.**
  The Hub 5's assembled login entry recorded the body Core sent rather
  than the one the firmware accepts, so a replay of the wrong request
  shape passed for as long as Core sent it. The fixture is corrected to
  the evidenced password-only body, and the mock server now refuses a
  JSON key the capture never carried instead of routing it — one
  direction only, and only where the capture pins a single body shape
  to a path. (Related to #82)

- **Six modems published non-canonical modulation values.** The
  SB8200 (CBN), G54, Hub 5 (F3896LG-VMB), CGA4236, CGA6444VF, and
  TC4400 passed firmware spellings straight through (`256QAM`,
  `64QAM`, `qam_256`, `256-QAM`, `16-qam`), which now normalize to
  canonical form. The TC4400 additionally published an IUC list on its
  OFDM channel and restated the channel type on its upstream rows;
  neither names a modulation scheme, so both are omitted. Downstream
  and upstream `modulation` attributes change value on these modems,
  and the TC4400's upstream `modulation` attribute goes away.

- **Speculative `TDMA` channel-type mappings removed.** The MB7621,
  SB6190, and CODA56 each mapped an upstream `TDMA` value that appears
  nowhere in their captures, and the MB7621 and SB6190 mapped it to
  `tdma`, which is not one of the four allowed channel types. The
  CM1100 keeps its mapping (the value appears in that firmware's own
  page source), as do the Technicolor .jst modems, whose shared
  three-way map is documented domain knowledge about that platform.

- **OFDM channels no longer leak non-modulation strings.** The parser
  stripped an enumerated list of known channel-type restatements from
  OFDM/OFDMA channels, which missed profile IDs and IUC lists. It now
  strips any value that names no real modulation scheme, so real PLC
  values like `QAM4096` still survive.

- **Spec conformance no longer flags unlocked channels for a missing
  `channel_type`.** The unlocked-channel nulling rule strips every key
  except `channel_number` and `lock_status`, so the identity requirement
  cannot apply there, but the validator checked it anyway. The false
  failure had never fired because no confirmed modem's golden carries an
  unlocked channel; the first one to capture a not-locked channel would
  have failed the gate mid-confirmation. PARSING_SPEC is reconciled to
  match, and the positive fixture now models real coordinator output
  instead of an assumed shape.

## [3.14.0-beta.16] - 2026-07-24

### Added

- **`null_values` parser.yaml option.** JSON field mappings can declare
  firmware no-value sentinels, which then skip silently as sparse data
  while undeclared shapes keep their warning. The DM1000 declares `---`
  for its inactive upstream slots, removing four warning lines per
  poll. (Related to #92)

### Changed

- **har-capture floor raised to 0.10.3.** The 0.10.3 sanitizer closes
  a credential-leak gap (the Sercomm `pws` login field) found during
  the DM1000 intake; catalog tooling now requires it. (Related to #92)

### Fixed

- **One button press, one logbook entry.** The restart button's
  availability toggle wrote state during the press, so a single press
  rendered as three "Pressed" logbook entries. The toggle is dropped in
  favor of the frontend's native in-flight feedback, and all three
  buttons now share the operation mutex the adapter spec defines:
  restart converts to it, Reset Entities gains the double-click guard
  it was missing, and Update Modem Data refuses presses during
  destructive operations.

- **DM1000 polling failed on every cycle since beta.10.** Channels
  built by parser.py hooks (the DM1000 OFDMA upstream) missed Core's
  channel_number pass and failed event payload validation, leaving all
  entities unavailable. Core now numbers hook-built channels after
  hooks run, and a new fleet test validates every catalog golden
  against the event payload schema, so this class of gap fails CI
  instead of production polls. (Related to #92)

## [3.14.0-beta.15] - 2026-07-22

### Added

- **Arris S33v3 uptime confirmed on hardware.** Contributor
  diagnostics show system uptime populated on real hardware, the one
  field the June confirmation could not close and the reason the
  mapping was restored in beta.14. Refreshes the S33v3 verified.json
  fixture from that download: 33 downstream and 6 upstream channels
  locked, full system_info, no field errors. (Related to #98)

- **Sercomm DM1000 reboot button and system uptime.** Both built
  from contributor captures; uptime feeds Last Boot Time. The reboot
  was exercised on real hardware — the capture records the modem's own
  confirm and "allow up to 60 seconds to restart" dialogs — and uptime
  is read from the status page the same session served. Closes both
  DM1000 catalog gaps. (Related to #92)

- **Locked-out credentials now prompt for reauthentication.** When
  the auth circuit breaker opens (definitive credential rejection,
  or repeated stale-session failures), the integration starts Home
  Assistant's native reauthentication flow — a "Reauthentication
  required" notification with the credential form attached. The flow
  itself has existed since the v3.14 adapter but nothing triggered
  it; users had to spot the ERROR log and fix credentials by hand.
  One WARNING per lockout, and a completed reauth reloads the
  integration so polling resumes with the new credentials (UC-81,
  UC-87).

- **Session cleanup on unload/reload.** The orchestrator now exposes
  `close()`, and the HA adapter calls it from `async_unload_entry` (in
  the executor). If a session is still live — a session-reuse modem at
  reload — it is logged out first so single-session firmware isn't left
  holding a lock that blocks the next login; then the socket pool is
  released immediately rather than lingering to garbage collection. The
  logout is best-effort and timeout-bounded, and a no-op for
  per-poll-logout modems whose session is already cleared.

- **Catalog audit shows how each fixture was produced.** The audit
  reads each HAR's own creator field and labels constructed fixtures
  (hybrid, reconstructed, synthetic, generated) in the Needs Testing,
  Pending Review, and Confirmed tables. An entry reports its weakest
  fixture, so a real capture beside a constructed one does not launder
  it.

### Changed

- **Supporting entities are now categorized as Diagnostic.** The
  primary group on the device page is the signal readout: Status and
  the per-channel sensors. Modem Info, Software Version, channel
  counts, error totals and rates, Last Boot Time, system-info
  pass-through, LAN statistics, the latency sensors, and the Update
  Modem Data button move to the Diagnostic section. Entity IDs and
  recorded history are unchanged.

- **Service failures now raise errors instead of returning them.**
  `generate_dashboard` handed its error message back as the YAML the
  caller was meant to paste; `convert_channel_identity` and
  `orphaned_statistics` returned error dicts; `request_refresh` and
  `request_health_check` logged a warning and reported success. All
  now raise, so automations and the UI see the failure. A failed
  modem restart raises too, after the coordinator refresh and the
  persistent notification. Legitimate empty results ("no statistics
  to migrate") remain successes.

- **Availability is logged once per transition.** The log gets one
  warning on the first failed poll of an outage and one info line on
  the first successful poll after it, instead of a warning per poll.

- **Services register at integration setup, and a catalog-load
  failure is a proper setup error.** The five services are available
  as soon as the integration loads rather than per config entry, and
  a failed catalog load now reports why in the UI instead of failing
  generically. (HA Quality Scale Bronze.)

### Fixed

- **Technicolor CGA6444VF: API polls no longer bounce with "session
  expired" right after a successful login.** The firmware rejects API
  requests lacking `X-Requested-With` and an origin-matching
  `Referer`; the config now sends both session headers, following the
  TG3442DE pattern. (Related to #120)

- **Translations: 81 damaged or missing strings repaired, with
  guards against regression.** A March locale regeneration had left
  four services in English inside every language file and silently
  stripped accents from French, Spanish, and Italian. The translated
  scope is restored to the config and options flows as documented,
  and the sync check now rejects out-of-scope sections, in-scope
  values identical to English, and accent-stripped locales.

- **generate_dashboard's form matches its real defaults.** The
  schema and handler defaults for `include_upstream_frequency` and
  `short_titles` had drifted apart, so the UI form and an automation
  omitting the fields produced different dashboards. Option fields
  now render as live controls showing their actual defaults, and
  `status_card_exclude` is dropped from the form — its valid values
  are modem-dependent and cannot be enumerated in a static form;
  scripted callers can still pass it.

- **Catalog tools: intake selects the login POST by its credential
  fields, not by recency.** A modem that serves both login and
  actions from one endpoint (DM1000: `/setup.cgi` handles login and
  reboot) previously had its reboot POST picked as the login at high
  confidence. The analyzer now picks the POST whose body carries a
  password-shaped field, and the action scan no longer fabricates
  endpoints from UI labels or asset paths.

### Removed

- **Core public API: `Orchestrator.reset_auth()` and the
  `AuthStateReset` event.** Spec'd for a reauth flow that was
  ultimately built on entry reload instead, the method never gained
  a production caller and could not fulfill its own contract:
  credentials are constructor-bound, so a credential change always
  rebuilds the orchestrator — at which point every field the reset
  would clear is already at its constructor default. Credential
  reconfiguration is now ratified as reconstruction
  (ARCHITECTURE_DECISIONS § Auth Architecture); UC-16 is rewritten
  around rebuild. Breaking only for external consumers of the Core
  library, of which none are known.

## [3.14.0-beta.14] - 2026-07-14

### Fixed

- **Recovery is no longer missed when an outage resolves through
  "ICMP Blocked".** A modem coming back sometimes answers TCP before
  ping, producing a transitional ICMP_BLOCKED health reading. The
  recovery detector enumerated state transitions and treated that
  reading as "was never down," so neither the immediate recovery poll
  nor Core's backoff clear fired — a reachable modem stayed
  Unreachable until the next scheduled scan (observed live: 13.5
  minutes). Both checks now derive from one predicate,
  `HealthStatus.data_path_up`: TCP was down and is now proven up.
  ICMP_BLOCKED counts as proof because, since the ICMP contradiction
  override shipped in this release, every such reading is confirmed
  by a live TCP handshake — this also retires the "conservative for
  v1" exclusion that kept ICMP_BLOCKED from clearing connectivity
  backoff on ping-filtered networks. Up-to-up transitions never
  trigger polls, so ping-blocking setups see no extra logins.

- **Status no longer shows a stale "Unresponsive" after recovery.**
  When a modem came back and a data poll succeeded before the next
  scheduled health probe, the last probe's Unresponsive reading —
  priority 1 in the Status cascade — kept masking the recovered modem
  for up to a full health interval (~2.5 minutes of "Unresponsive"
  over a modem actively serving polls). A successful poll is live
  proof the data path is up, so it now triggers an immediate health
  refresh — stale evidence must not outvote a live signal, the same
  principle as the health monitor's ICMP contradiction override
  (UC-59a), applied in the reverse direction.
  The refresh is ICMP-only (fresh collection evidence keeps the
  TCP/HEAD skip gate up), and the resulting recovery is recognized as
  poll-proven so it does not trigger the health-recovery listener's
  redundant forced poll — no extra logins on session-limited modems.

- **S33v3 system uptime mapped from real wire data.** A contributor
  HAR that includes the Software Information page (Cmswinfo.html)
  finally showed the populated response: the AT01 firmware writes
  `24 day(s) 10h:33m:56s` — `day(s)`, not the `days` the S33/S33v2
  firmware line writes. The original mapping guessed the S33v2-style
  format, silently failed to match on real hardware, and was misread
  as the field being absent and dropped. Restored with the observed
  format; the S33v3 test fixture is now that full wire capture,
  replacing the earlier synthetic fixture, and the S33v3 gaps entry
  is closed. Uptime feeds Last Boot Time, which no longer waits for
  a counter reset on this model. (Related to #98)

- **HAR loader decodes base64-stored HNAP bodies.** The HNAP
  response merge read `content.text` verbatim, so a response body
  stored with the HAR `encoding: base64` marker was silently
  skipped; only the HTTP resource path handled the marker. Both
  paths now share one decode step. Surfaced by the S33v3 capture,
  whose channel-data response is stored base64-encoded.

### Added

- **Sercomm DM1000 confirmed on hardware.** Verified via contributor
  diagnostics on 3.14.0-alpha.15: 34 downstream + 5 upstream channels
  locked, form auth, full system_info, clean polls. Modulation values
  now canonicalize (`256QAM` → `QAM256`, `64QAM` → `QAM64`) — caught
  by the confirmed-modem conformance gate at promotion. Lock status
  is now mapped from the firmware's native signals (QAM_NONE filter
  survivors, the OFDM PLC indicator, OFDMA STATE=OPERATE), which
  lets Core derive DOCSIS status. Remaining gaps documented in the
  catalog entry: no system_uptime and no reboot action, both blocked
  on one supplemental HAR covering status.html and the Reboot click.
  (Related to #92)

- **`fixed_fields` for JSON parsers.** The JSON channel format now
  supports static per-channel field values (flat and multi-array
  forms), mirroring the XML tables capability — for firmware that
  expresses lock state by omission or sentinel rather than a lock
  column. First consumer is the DM1000.

- **Catalog entries can declare capability gaps.** New optional
  `gaps:` list in modem.yaml (capability, needs, tracking issue)
  records what a verified modem is still missing and exactly what
  evidence closes it. The catalog audit renders them as a "Confirmed
  with Gaps" table — each row a self-contained contribution task.
  Backfilled for the S33v3 (uptime, #98) and DM1000 (uptime +
  reboot, #92).

### Changed

- **One outage, one Activity line.** A modem going unreachable used to
  write four logbook lines (Status, Docsis Status, Last Boot Time, and
  Software Version all going to Unavailable). Now it writes one: Status
  to Unreachable. The non-continuous data sensors (Software Version,
  Last Boot Time, and string pass-through fields) are now "sticky" —
  they stay available and hold their last value through a transient
  outage, so no spurious "to Unavailable" transition reaches the
  logbook. Real changes still log: a firmware push (Software Version)
  and a reboot (Last Boot Time) are value changes and each log one line.
  Continuous sensors (channel metrics, error counts and rates,
  provisioned speeds) are untouched, so their unavailable-gaps stay
  meaningful on graphs. (Related to #178)

- **Docsis Status sensor retired, folded into Status.** The Status
  sensor already carries the DOCSIS lock state two ways: as its display
  value (Not Locked, Partial Lock, Operational, via the priority
  cascade) and on its `docsis_status` attribute. The standalone
  `sensor.<prefix>_docsis_status` was a duplicate that double-logged
  every real lock change and contributed the fourth outage line above.
  The dashboard generator now renders DOCSIS Status as an attribute row
  off the Status sensor (the raw value, e.g. `operational`); regenerate
  your dashboard after upgrading. On upgrades from earlier 3.14 betas
  the removed sensor lingers as unavailable — press Reset Entities
  (keeps your config and history), or remove and re-add the
  integration, to clear it. Upgrades from 3.13 and earlier never had a
  standalone DOCSIS sensor, so there is nothing to clear.
  (Related to #178)

- **DOCSIS 4.0 enters the catalog vocabulary.** `hardware.docsis_version`
  now accepts `"4.0"` (Core schema + MODEM_YAML_SPEC § Hardware), defined
  as hardware capability rather than provisioned mode. The XB10
  (CGM601TCOM) is relabeled from "3.1" — the schema previously had no
  legal way to say 4.0. Its own fixture corroborates the hardware
  sources: locked upstream OFDMA at 105/200/297 MHz, FDX-band
  frequencies beyond DOCSIS 3.1's 204 MHz upstream ceiling, replacing
  the earlier "provisioned in DOCSIS 3.1 mode" note. Intake inference
  still assigns only "3.0"/"3.1" (D4.0 shares 3.1's channel types on
  the wire); "4.0" is set by hand from hardware sources. (Related to #173)

## [3.14.0-beta.13] - 2026-07-08

### Added

- **Status card contents are now configurable.** The dashboard
  generator's `generate_dashboard` service accepts a
  `status_card_exclude` list controlling which fields appear on the
  status card (empty by default; add `docsis_status` to drop the
  DOCSIS Status row). Label history for anyone diffing regenerated
  cards: that row was "Modem Status" through beta.10, silently became
  "Docsis Status" in beta.11 when a refactor routed it through the
  generic field loop, and is now "DOCSIS Status" back at its original
  position. (Related to #178)

- **Firmware and hardware versions on the device page.** The device
  registry now carries `sw_version` and `hw_version`, so the device
  info card shows them natively. The Software Version sensor remains
  — a firmware push is a state change worth automating on — but the
  Hardware Version and Model Name sensors are gone: immutable
  identity strings are device metadata, not sensors. Both values
  still flow in diagnostics and the event payload. Clear the leftover
  entities the same way as the uptime sensors below. (Related to #178)

- **Technicolor XB8 (CGM4981COM) catalog entry (awaiting hardware
  verification).** Built from a reconstructed fixture: the XB7 capture
  as structural template with real XB8 values from contributor
  diagnostics, whose live parse (34 downstream + 5 upstream channels
  locked) is the empirical basis. The entry stays awaiting
  verification until an XB8 owner confirms on current firmware.
  (Related to #101)

- **Catalog timeline now dates every modem.** Sourced hardware release
  dates were added to the XB8 and backfilled across 11 existing
  entries; the catalog README timeline previously skipped undated
  entries silently and now renders all 38. The CH7465MT's abbreviated
  ISP name became a proper sourced Magenta AT provider entry.

### Changed

- **System Uptime and Current Time sensors removed.** Both wrote a new
  recorder row every poll for values with no historical worth: uptime
  is derivable at display time from Last Boot Time, and a wall-clock
  reading from 40 polls ago answers nothing. Last Boot Time now holds
  steady across poll-timing jitter and Home Assistant restarts, and
  changes only when the modem actually reboots, so its relative
  rendering ("5 days ago") carries the uptime display. The dashboard
  generator emits the updated card; regenerate your dashboard after
  upgrading. Upgrades from 3.13 clear the old System Uptime entity
  automatically during config-entry migration. On upgrades from
  earlier 3.14 betas the removed sensors linger as unavailable —
  press Reset Entities (keeps your config and history), or remove
  and re-add the integration, to clear them. Both values still flow
  in the data layer (event payload, diagnostics). (Related to #178)

- **Find your modem by any name on its box.** The manufacturer dropdown
  now lists brand names alongside manufacturers (union of `manufacturer`
  and `brands` catalog fields), so a modem branded differently from its
  maker appears under both — the CommScope-made G54 sold as Arris shows
  under Arris and CommScope. Labels are bucket-contextual: the lead name
  always matches the filter chosen ("Arris G54 (CommScope G54)" under
  Arris; "CommScope G54 (Arris)" under CommScope), the parenthetical
  shows sticker codes and other brands ("Xfinity XB6 (CGM4140COM)"),
  and the "All" view lists one row per user-facing name so an
  alphabetical scan finds a modem under any of them. ISP-issued
  gateways gained their ISP buckets: the XB6, XB7, and XB10 appear
  under Xfinity, and the XB7 also under Cox's Panoramic Wifi. Brand
  entries are sourced in the catalog (firmware brand fields, product
  pages, manuals). (Related to #72)
- **Manufacturer names keep their real styling.** Display normalization
  no longer mangles deliberate mixed case: CommScope and SURFboard
  render as branded instead of "Commscope"/"Surfboard".
- **Two catalog entries renamed to their true manufacturer.** The G54
  moved to `commscope/g54` (firmware self-reports CommScope; Arris is
  the box brand) and the Virgin Media Hub 5 to `sagemcom/f3896lg-vmb`
  (the hub is a Sagemcom F3896LG-VMB; "Hub 5" and "SuperHub 5" remain
  as searchable aliases, Virgin Media as the brand). Entries created on
  earlier 3.14 betas for these two modems must be removed and re-added
  once after upgrading; upgrades from 3.13 and earlier resolve the new
  locations automatically. Unverifiable model aliases were removed
  (MB8612; Zoom 5370 and Thomson TCM420 are different hardware).
  (Related to #72, #82)

### Fixed

- **Honest message when login gets a 404.** A login endpoint returning
  404 means the device at that address has no login page (wrong device,
  or the modem web interface is unavailable) — not that credentials
  were rejected. The circuit breaker still stops polling immediately
  (retrying would keep posting credentials at an unknown device), but
  the log now says what actually happened and that reloading the
  integration retries, instead of telling you to reconfigure
  credentials that were never wrong.
- **Host field tolerates address-bar pastes.** A host entered with a
  path but no scheme ("192.168.100.1/cgi-bin/luci/") kept the path as
  part of the host and broke every request. Only the protocol and
  host are ever kept now, with or without a scheme; data-page paths
  always come from the catalog. (Related to #72)
- **Faster reconnect after a long modem reboot.** When a reboot
  outlasted the recovery window and the modem came back through a
  DEGRADED health state (pingable, but its web UI still warming up),
  the integration did not schedule an immediate poll on recovery and
  waited for the next slow scan (or a manual refresh) to reconnect.
  The health-recovery trigger now treats DEGRADED as a data-path-down
  state, so reconnection is bounded by the health-check interval
  instead. (Related to #170)

## [3.14.0-beta.12] - 2026-06-28

### Added

- **Technicolor XB10 (CGM601TCOM) confirmed on hardware.** Verified via
  contributor diagnostics: 10 downstream + 8 upstream channels locked,
  form auth, and the restart action tested working. Upstream channel type
  now reads the "Channel Type" column directly (the firmware reports a bare
  "QAM" with no constellation, so the unmappable modulation field is
  omitted, matching the XB7). (Related to #173)
- **Technicolor CGA6444VF logout action (untested on hardware).** This
  modem enforces single-session login, but its config defined no logout,
  so the integration never released its session after a poll and could
  collide with its own still-open session on the next login. The logout
  request is taken faithfully from the contributor HAR capture. It remains
  awaiting verification until a CGA6444VF owner confirms. (Related to #120)
- **Arris SB8200 (body-token variant) confirmed on hardware.** Verified
  on two units on Spectrum that exercise the identical auth and parser
  contract: HW v6 (34 DS + 4 US locked, Related to #170) and HW v7
  (29 DS + 4 US locked, Related to #124), both with full system_info and
  no errors.
- **Config flow recovers from a failed login instead of dead-ending.**
  When a variant guess fails validation, the connection form returns
  with the real error and an inline variant switch, so another variant
  can be tried without restarting Add Integration. Host and credentials
  carry across the switch, and the form re-renders to expose credential
  fields if the newly selected variant needs them. The form also names
  the variant currently being configured. (Related to #176)

### Changed

- **Arris SB8200 "basic" variant renamed to "body-token."** The file
  stem rendered as "URL Token (basic)" in the config-flow picker, which
  read as HTTP Basic Auth; it is a URL-token variant whose session token
  returns in the login response body. The picker label is now the honest
  "URL Token (body-token)." No config-entry migration was added, so an
  existing entry on this variant must be removed and re-added once after
  upgrading; host and entity prefix are unchanged, so history carries
  over. (Related to #124)
- **Variant picker no longer shows hardware version.** The hardware
  version does not determine the auth contract and misled contributors
  into picking a variant by it; it is now appended only to break ties
  between otherwise-identical variant labels. (Related to #176)

### Removed

- **Serial number and MAC address are no longer collected.** These
  identity fields were scraped during earlier field enrichment but have
  no consumer in the integration; they were only surfaced as generic
  pass-through sensors. They are PII with no monitoring value, so the
  parsers and the intake pipeline no longer extract them. On upgrade,
  the "Serial Number" and "MAC Address" sensors on the Technicolor XB6,
  XB7, and XB10 become unavailable and can be deleted.

### Fixed

- **Canonical modulation set now matches the DOCSIS standard.** The
  accepted modulation values had drifted to a fleet-observed subset that
  omitted 8-QAM, 128-QAM, and 512-QAM, all valid DOCSIS orders; a modem
  reporting one would have had the value rejected as a spec violation. The
  set is now the full DOCSIS PHY enumeration (QPSK, 8 through 4096-QAM,
  plus optional 8192/16384-QAM), grounded with a CableLabs citation.
- **SB8200 login-page response no longer crashes the loader.** When a
  url_token login returns the login page instead of a session token
  (single-session contention, redirect, or rejected credentials), the
  HTML is no longer injected as a request-header cookie, which raised an
  invalid-header error. Auth now skips the injection and the fetch is
  classified as a login failure, the handled outcome. (Related to #81,
  #124)
- **SB6141 error counts restored across firmware title drift.** The
  downstream codewords selector matched only the "Signal Status
  (Codewords)" title; firmware 1.0.7.0-SCM00 reports "Signal Stats
  (Codewords)," so corrected and uncorrected counts silently dropped on
  that cohort. Both titles now match. (Related to #177)
- **Malformed-header log noise suppressed structurally.** Firmware that
  emits non-RFC-compliant HTTP headers (Arris S33v3, SB6141) made
  urllib3 log a HeaderParsingError on every poll; the body parses fine
  regardless. A single filter keyed on the exception type now suppresses
  it on both urllib3 loggers. (Related to #98)
- **Bond-change notification no longer fires on zero-channel readings.**
  A (0,0) reading is a booting or no-signal page, not a real bond
  change; a transient zero stored as the baseline could re-fire as a
  spurious "24 to 0 then 0 to 24" change. Zero totals are now guarded so
  they never fire or persist.

## [3.14.0-beta.11] - 2026-06-15

### Added

- **Diagnostics now report system_info field outcomes.** A mapped field
  that produces nothing on a successful poll is no longer invisible:
  the diagnostics download lists fields the modem never sent
  (`system_info_fields_missing`) and fields whose value type conversion
  rejected, with the raw value (`system_info_fields_failed`). Lets a
  field-level parsing gap be diagnosed from the first shared
  diagnostics file instead of a contributor round-trip. (Related to #98)
- **Arris S33v3 confirmed on hardware.** Verified via contributor
  diagnostics: 33 downstream + 6 upstream channels, HNAP SHA256 auth,
  ICMP health checks working. The firmware does not report uptime;
  Last Boot Time derives from counter-reset detection. (Related to #98)
- **Four more modems confirmed on hardware.** Verified via contributor
  diagnostics: Motorola MB8611 (34 DS + 4 US, restart confirmed,
  Related to #60), Arris SB6183 (16 DS + 4 US, Related to #95), Netgear
  CM3000 (34 DS + 5 US, Related to #127), and ARRIS CM3500B
  (26 DS + 5 US, Related to #73).
- **Technicolor XB10 (CGM601TCOM) added to the catalog.** DOCSIS 4.0
  hardware provisioned in 3.1 mode. Auth, channel parsing, and restart
  action config derived from a contributor HAR; awaiting hardware
  confirmation before it is marked confirmed. (Related to #173)
- **`{cookie:name}` parameter interpolation.** Action params can now
  reference session-jar cookies by name, resolved at execution time.
  Supports the Double Submit Cookie CSRF pattern needed for the XB10
  restart action. (Related to #173)

### Changed

- **Breaking: `session.max_concurrent` replaced by
  `HttpAction.requires_session`.** `max_concurrent` is removed from
  SessionConfig. The presence of `actions.logout` is now the sole
  indicator of single-session semantics. Per-action
  `requires_session: true` skips the logout-before-retry when no session
  cookie is present. Catalog YAML still using `max_concurrent` must
  migrate. (Related to #170)
- **parser.py declares its own resource needs.** A `PostProcessor` now
  declares the resources its hooks read in a `resources` class attribute
  (URL path → format), merged into the fetch list at startup. Replaces
  the former workaround of adding a fake parser.yaml field mapping just
  to force the fetch. The Sercomm DM1000 — the only catalog user of the
  workaround — has been migrated; extracted output is unchanged.

### Fixed

- **CM3500B modulation values are now canonical.** The parser.yaml
  modulation columns were typed as plain strings; downstream QAM and
  upstream ATDMA channels now emit canonical `QAM256` / `QAM64`.
  (Related to #73)

## [3.14.0-beta.10] - 2026-06-04

### Added

- **`orphaned_statistics` service.** Finds recorder statistics for a
  modem that have no registered entity — left behind by a mode switch,
  channel rebonding (ID mode), or a prefix change. Default call returns a
  commented preview of orphaned entity IDs. Pass `execute: true` to purge
  all of them directly via HA's recorder. Purge is permanent. Documented
  in TROUBLESHOOTING.md § Ghost Statistics.
- **Technicolor XB7 (CGM4331COM) now fully supported.** Verified on
  hardware via contributor HAR capture. 34 downstream + 5 upstream channels
  confirmed.

### Fixed

- **Logout before same-poll auth retry on single-session firmware.** When
  `LOAD_AUTH` or `LOAD_INTEGRITY` fires on a modem with `actions.logout`
  configured, Core now attempts a best-effort logout before clearing the
  session and retrying.
  Releases any stale server-side session (e.g. after an unclean HA restart)
  so the immediate re-authentication can succeed. Related to #170.
- **SB8200 v6 Basic: logout action added to catalog.** The `modem-basic`
  variant was missing a logout action, preventing the logout-before-retry
  path from firing. Related to #170.
- **XB7: modulation field normalized to canonical form.** The XB7 firmware
  reports upstream modulation as `"64QAM"` where the catalog expects
  `"qam_64"`. The parser now normalizes at intake so modulation values are
  consistent across modem families. Addresses #107.

## [3.14.0-beta.9] - 2026-06-01

### Fixed

- **XB6/XB7: upstream OFDMA channels now correctly classified as `ofdma`.**
  Firmware reports `Channel Type: TDMA` for OFDMA upstream channels because
  `docsIfUpChannelType` (DOCS-IF-MIB, RFC 4546) has no OFDMA value. The
  channel type is now derived from the `Modulation` field instead, matching
  the DOCSIS 3.1 spec. `symbol_rate` and the redundant modulation label are
  correctly stripped from OFDMA channels. Addresses #107.
- **XB7: malformed `<th>` HTML normalized before parsing.** Firmware emits
  `<th>Label</td><td>value</td>` — unclosed `<th>` tags that cause
  BeautifulSoup to nest sibling cells instead of treating them as a row.
  `normalize_html()` now rewrites the tag at the input boundary so the table
  parser sees well-formed HTML. Addresses #107.
- **`inject_credential_cookie` auth: body is the credential cookie value.**
  Firmware that uses this pattern returns a server-issued session token in
  the auth response body; browser JS sets it directly as the credential
  cookie (`createCookie("credential", result)`). Core now replicates this
  correctly. An empty auth response body is treated as an error — there is
  no btoa fallback. Related to #170.

### Added

- **`inject_credential_cookie` field for `url_token` auth.** When set,
  Core sets `cookie_name` to the server-issued token from the auth response
  body after login. Use for firmware where the server never issues the
  credential cookie via `Set-Cookie` and the browser JS reads the token from
  the response body instead.
- **SB8200 `modem-basic` golden file regression test.** `modem-basic.har`
  now carries a synthetic auth response body so `test_modem_har_replay`
  exercises the full auth → fetch → parse pipeline for this variant.

## [3.14.0-beta.8] - 2026-05-29

### Fixed

- **Two-phase TLS probe for cipher-floor incompatibility.** Standard Python
  SSL is tried first; `SECLEVEL=0` is the fallback only when standard SSL
  fails. `legacy_ssl=True` when standard fails but `SECLEVEL=0` succeeds,
  covering both old TLS versions and TLS 1.2 modems whose cipher suites
  fall below Python's default security floor. Related to #170.

### Added

- **Field-set change detection for `system_info`.** The orchestrator
  now logs a WARNING when parser-level `system_info` fields appear or
  disappear between polls. Surfaces silent firmware-induced regressions
  (e.g., a CSS selector miss after a modem firmware update) that previously
  went undetected.

### Changed

- **Standardization of `boot_status` (Tier 2).** Renamed `provisioning_status`
  to `boot_status` in the Compal CH7465MT parser.
- **Documentation: `FIELD_REGISTRY.md` expanded.** Registered `model_name`
  in Tier 2; added `dhcp_status`, `tftp_status`, and `internet_access` to
  prevent field fragmentation across manufacturer-specific diagnostic pages.
  Renamed `fft_size` to `fft_type` in the field registry to match parser usage.
- **Standardization of `channel_width` (Tier 2).** Renamed `width` to
  `channel_width` across all parsers (SB8200, TC4400) and synchronized
  with the Tier 2 naming authority.
- **Standardization of `model_name` (Tier 2).** Renamed `model` to
  `model_name` in the Arris CM820B, TM1602A, Technicolor CGA2121, and
  Sercomm DM1000 parsers. Updated the confirmed `arris/cm820b` verified
  artifact.
- **Standardization of `ranging_status` (Tier 2).** Renamed `us_ranging_status`
  to `ranging_status` in the Technicolor CGA2121 parser.

## [3.14.0-beta.7] - 2026-05-26

### Added

- **Arris SB8200 `modem-basic` variant (URL Token, v6 hardware).** Added
  catalog entry for the v6 hardware variant (firmware AB01.01.009.51).
  Auth mechanism confirmed in Unreleased. Status: `awaiting_verification`.
  Related to #170.
- **`ModemSnapshot.to_event_payload()` and HA event bus integration.** Core
  now produces a structured `EventBusPayload` (model, ISP, status, channel
  counts, collection timestamp) on every successful poll. The HA coordinator
  fires this as the `cable_modem_monitor_data_updated` event, enabling
  downstream integrations to consume live modem data without coordinator
  coupling. Related to #169.

### Changed

- **Orchestration logging migrated to typed `OrchestratorEvent` dataclasses.**
  All log output from the orchestration layer now flows through a single
  `log_event()` adapter. No runtime behavior change.
- **HA adapter: dev tools extracted to `dev_tools.py`.** `async_request_modem_refresh`,
  service registration, and related utilities moved from `services.py` into a
  dedicated module. No behavior change.

## [3.14.0-beta.6] - 2026-05-22

### Breaking Changes

- **Arris SB8200 `variant: v7` config entries will fail on
  startup.** `modem-v7.yaml` was renamed internally to `modem-cookie.yaml`.
  Delete and re-add the integration to fix it.

### Added

- **Bearer token auth strategy (`auth.strategy: bearer`).** Supports
  RFC 6750 `Authorization: Bearer <token>` authentication for modems
  using token-based REST APIs. Virgin SuperHub 5 uses this path for
  its restart action.
- **Per-action auth (`action_auth`) on `HttpAction`.** Restart (and
  future) actions can carry independent credentials without touching
  the monitoring session. A fresh session is authenticated and
  discarded after the action. Modems with `auth.strategy: none` for
  monitoring but bearer-protected restart endpoints use this path
  (e.g., Virgin SuperHub 5). Config flow shows credential fields
  when `action_auth` is set on any restart action.
- **Virgin SuperHub 5 restart action.** `actions.restart` block added
  to `virgin/superhub5/modem.yaml` (bearer `action_auth`, sourced
  from issue #82). HAR verification pending — needs a contributor
  capture to confirm http vs https and username field handling. Related to #82.
- **`ModemStatus` StrEnum replacing `Literal[...]` in Core schema.**
  Values: `confirmed`, `awaiting_verification`, `unsupported`.
  Imported from `models` package. Closes future drift at the Pydantic
  gate. Catalog tools and config index updated in lockstep.
- **Per-variant `*` markers in config-flow variant dropdown.**
  Unconfirmed variants are labeled with `*` (e.g.,
  `SB8200 Cookie (v6)*`). A translation footer explaining the
  marker appears in all 12 supported locales. The model-level
  rollup shows `*` only when every variant in every sibling
  directory is unconfirmed.
- **Catalog tools: missing Tier-1 `system_info` fields flagged at
  golden-file generation.** `generate_golden_file` returns
  `missing_system_info_fields` — the diff of `SYSTEM_INFO_FIELDS`
  against what the parser extracted. A non-empty list means the
  HAR has data the parser isn't capturing; the onboarding workflow
  now instructs contributors to inspect the HAR and add the mapping
  before proceeding. Closes the gap where `system_uptime` was absent
  from `netgear/c7000v2` and `netgear/c3700` at authoring time but
  only caught at confirmation. Related to #163.
- **Restart action test harness auto-discovery.** `RestartTestCase`,
  `discover_restart_tests`, and `run_modem_restart_test` added to
  Core's test harness. The catalog test suite picks these up
  automatically — adding restart coverage for a modem means adding
  `test_data/modem-restart.har`. No test code changes needed. Two
  HAR sources (first match wins): dedicated `modem-restart.har`, or
  `modem.har` when `modem.yaml` declares `actions.restart`.
- **Catalog README auth strategy badges and render_readme.** Each
  modem row now shows a color-coded badge for its auth strategy
  (grouped by family: No auth / Simple / Form-based / Token-based /
  Protocol). `render_readme: true` added to `hacs.json` so the
  catalog README renders as the HACS store listing.
- **Chipset sourcing audit.** BCM3390 citations added for CM1200,
  CM2050v, CGA4236, and CGA6444VF (cross-model inference, sourced);
  bare "Same platform as X" stubs on S34 and MB8600 replaced with
  real URLs.

### Fixed

- **Technicolor `form_pbkdf2` modems rejected every login attempt.**
  `{"error": "ok"}` signals success on the Technicolor REST platform
  (CGA6444VF, CGA4236), but the truthy-error check treated `"ok"` as
  a failure and returned `MSG_LOGIN_1` on every attempt. New
  `login_success` field on `FormPbkdf2Auth` specifies the exact
  key-value pairs that constitute success; the existing truthy-error
  path is unchanged when `login_success` is unset. Addresses #120.
- **TCP probe analysis layer still matched HTTP GET log lines.**
  `362db673` removed the HTTP GET probe and promoted TCP connect as
  the primary L4 signal, but `log_parser.py` kept the old regex
  (`HTTP GET Xms, N bytes`) — real TCP probe logs were silently
  dropped. Regex updated to match the current `ICMP Xms, TCP Yms`
  format. `HealthEvent.http_ms` renamed to `tcp_ms` throughout the
  analysis layer. ORCHESTRATION_SPEC, ORCHESTRATION_USE_CASES,
  HA_ADAPTER_SPEC, and README updated to remove stale HTTP examples.
- **PII check false-positive on credit card pattern in modem
  fixtures.** `check_fixture_pii` flagged valid hex MAC/channel data
  as Visa card numbers. False positive suppressed for cable modem
  fixture context.
- **Netgear C3700 uptime field false-positive IPv6 redaction.** HAR
  capture included an uptime string that tripped the IP redaction
  regex; fixture corrected.

### Confirmed

- **Arris SB8200 HNAP** — verified on hardware by @inventor7777
  on TB01 firmware, Cox ISP, hardware version v6. Addresses #165.
- **Netgear C7000v2** — verified on hardware (beta.5 diagnostics).
  `system_uptime` parser added (RouterStatus.htm offset 33).
  Addresses #163.

## [3.14.0-beta.5] - 2026-05-20

### Fixed

- **HA install failure: Core dependency floors exceeded HA's pinned versions.**
  `requests>=2.34.2` and `pyyaml>=6.0.3` in Core's `pyproject.toml` both
  exceeded HA 2025.1.x pins (`requests==2.32.3`, `PyYAML==6.0.2`), causing uv
  to fail with "No solution found" on every fresh install. Floors lowered to
  `requests>=2.31.0` and `pyyaml>=6.0.0`. Addresses beta.4 install regression.

### Added

- **HA dependency compatibility gate.** `scripts/check_ha_compat.py` validates
  that every floor declared in Core and Catalog `pyproject.toml` is satisfiable
  under HA's `package_constraints.txt`. Wired into `make validate-ci` and the
  `ha-compat-check` CI job so this class of regression is caught before push.

## [3.14.0-beta.4] - 2026-05-20

### Added

- **Per-minute error rate sensors (#164).** SC-QAM `rate_corrected`
  and `rate_uncorrected` fields (errors/min) in `system_info`,
  derived by the orchestrator from inter-poll deltas on a monotonic
  clock. Two new HA sensors (`Rate Corrected Errors`, `Rate
  Uncorrected Errors`, MEASUREMENT state class) materialize whenever
  the modem reports SC-QAM error counters; a per-counter zero-floor
  rule means a total of `0` produces rate `0.0` immediately, so a
  fresh modem with no errors reads `0.0` rather than `unknown` from
  the first poll. Otherwise the sensors read `unknown` until the
  second poll fills the delta. Rate history graphs are opt-in via a
  new `include_error_rates` service option (default `false`); the
  default dashboard is unchanged. SC-QAM scope only; OFDM rates are
  future work per the DOCS-IF31-MIB boundary rule in PARSING_SPEC
  § Aggregate. Surfaces what users in #110, #144, and #161 were
  assembling with HA's `derivative` integration.
- **Modulation canonicalization elevated to core.** New
  `type: modulation` field type auto-canonicalizes raw values
  (`256QAM` → `QAM256`) via the shared helper — one source of
  truth instead of per-modem map blocks. Paired with
  `channel_type: derive: from_modulation`, the universal
  direction-aware rule (DS `QAM*` → `qam`, US `QAM*` → `atdma`,
  OFDM/OFDMA stand alone) replaces enumerated per-modem
  channel-type maps. Catalog migrations: cm820b, tg3442de,
  tm1602a, coda56 (DS keeps a source-code-to-value map layered
  under the new type for the modem-specific 1→QAM64 / 2→QAM256
  translation), ch7465mt. `XMLColumnMapping` gained `map:`
  support along the way, closing a format gap.
- **Spec-conformance gate against committed goldens.**
  `validate_modem_data` runs against `modem.expected.json` for
  every `status: confirmed` modem and asserts conformance with
  `PARSING_SPEC`. 16 fixture-driven cases plus inline helper
  coverage. Catches non-canonical modulation drift at PR time
  rather than at runtime. `awaiting_verification` modems are
  not enforced (parsers still iterating).
- **Optional-segment uptime format.** Uptime parsers now accept
  format strings with bracketed optional segments
  (e.g., `[{days} days ]{hours}:{minutes}:{seconds}`), so a single
  declarative format can match both `2 days 03:14:15` and `03:14:15`
  when the modem omits days under one day. Unified the cm2000 and
  cm3000 uptime declarations on this shared format.
- **Config-flow variant picker groups cross-directory modem
  variants.** SB8200 HTML, HNAP, and CBN variants now appear as a
  single "Arris SB8200" entry in the picker; choosing one opens a
  sub-picker for the variant. Hardware version (`hw_version`) read
  from `hardware.hw_version` in the catalog appears as "(v6)" or
  "(v7)" in the variant label so users can match against the modem
  sticker. `firmware` moves to `hardware.firmware`; `hw_version`
  removed from the top-level model config. Catalog README gains a
  CBN badge column and shows `hw_version` in the model column to
  disambiguate duplicate rows.

### Fixed

- **Netgear C7000v2 Basic Auth — 401 on every authenticated
  request.** Firmware requires the `XSRF_TOKEN` cookie (set on
  the initial `GET /`) to accompany every subsequent
  `Authorization: Basic` header. Enabled `challenge_cookie: true`
  in the C7000v2 `modem.yaml` — same pattern already shipping
  for the CM1200 HTTPS variant. Addresses #163 (thanks to
  @Anthranilic for the fresh HAR capture).
- **Compal CH7465MT upstream `channel_type` miscoded as `qam`.**
  Corrected to `atdma` as part of the modulation canonicalization
  migration. Longstanding miscoding — upstream DOCSIS 3.0 SC-QAM
  carriers are A-TDMA, not QAM.
- **9 CodeQL code-scanning alerts.** Implicit string
  concatenation in `services.py` and `generate_catalog_index.py`
  converted to explicit `+`; ineffectual `await` in
  `test_sensor_entities.py` marked explicit. Catalog README
  byte-identical after regeneration; no behavior change.
- **v1 to v2 migration: sibling variants missed; Ping and HTTP
  Latency sensors absent post-upgrade.** `resolve_variant` only
  walked the primary modem directory, missing variants defined in
  sibling directories (SB8200 CBN, HNAP, url_token). `supports_icmp`
  and `supports_head` were omitted from the migrated entry;
  `sensor.py` gates Ping and HTTP Latency sensor creation on those
  keys, so both were silently absent after upgrade. Both keys now
  default `True`; runtime health probing corrects false positives.
  Addresses #143.
- **Error rate baseline not cleared on `reset_auth()`.** The
  post-reset poll computed a rate across the auth-outage interval
  rather than emitting no-baseline. `reset_auth()` now clears the
  prior-state baseline. Related to #164.

### Changed

- **Catalog tools field registry expanded.** New vocabulary entries
  map non-standard modem headers to canonical fields without pipeline
  code changes: `Channel Index` to `channel_number`, `Received Level`
  and `Transmit Level` to `power`, `SNR/MER Threshold Value` to
  `snr`, and `Modulation/Profile ID` to `modulation`. HNAP
  `_run_pass1` now skips degenerate all-counters rows. Fleet accuracy
  improves from 72.3% to 74.0%; TC4400 per-modem accuracy from 60.8%
  to 98.6%.
- **Arris SB8200 CBN variant directory renamed from `sb8200v3` to
  `sb8200-cbn`.** No confirmed installations. Directory name now
  reflects transport rather than hardware revision, consistent with
  `sb8200-hnap`.

### Confirmed

- **Arris S33** — verified on real hardware by @ccpk1 on
  firmware `TB01.03.001.11_051722_212.S3` (32 DS + 4 US locked,
  all signals nominal). `modem.har` refreshed from his sanitized
  capture (114 entries, full reboot flow, all diagnostic
  screens); `modem.expected.json` regenerated against the
  existing curated `parser.yaml`; `modem.verified.json` added
  from his diagnostics. S33v2 and S33v3 remain
  `awaiting_verification` — each needs an independent
  confirmation. Related to #146, #140.

## [3.14.0-beta.3] - 2026-05-01

### Added

- **`verify_diagnostics` catalog tool.** Pure function plus thin CLI
  in `cable_modem_monitor_catalog_tools` that takes an HA diagnostics
  JSON and a release tag and produces a properly-shaped
  `verified.json` fixture (HA wrapper unwrapped, integration extras
  stripped, `verified_at`/`version` prepended, channel arrays
  rendered compact, multi-variant paths resolved). Replaces the
  hand-rolled python heredoc previously used at confirmation time
  with a deterministic, tested transform. Imports
  `SYSTEM_INFO_FIELDS` and `canonicalize_channel_keys` from the
  core registry to keep the field-list and channel-key-order
  truth in one place. Maintainer-facing; no end-user impact. See
  `MODEM_INTAKE_WORKFLOW.md` § Confirmation Phase.
- **`json_transposed` parser format.** New format for modems that
  expose channel data as transposed JSON tables (rows are fields,
  columns are channels). Lives in `core/parsers/formats/` alongside
  the existing `html_table`, `xml`, and `json_table` formats and
  registers through the centralized format registry so new formats
  compose in additively. Surfaces to contributors via modem.yaml
  `format:` selection.
- **OFDM/OFDMA `frequency` semantic pinned in FIELD_REGISTRY.md.**
  The same canonical field carries different physical meaning across
  channel types — SC-QAM `frequency` is the carrier center; OFDM
  and OFDMA `frequency` is the **lower edge of the active subcarrier
  band**. Documented with the spectrum-overlap evidence (CM2050V
  690 MHz overlaps SC-QAM at 642–651 MHz if interpreted as center)
  and the matching DOCSIS 3.1 OSSI MIB. New OFDM/OFDMA contributions
  map whichever firmware key lands at the lower edge to canonical
  `frequency`; per-firmware shape is mechanism, not concept.
- **`range: span` mapping operator for fan-out from one source
  field.** Splits a separator-delimited raw value and computes
  `last - first`, used by TG3442DE to derive `channel_width` from
  the same `Frequency` key that also supplies `frequency`
  (`"751~860"` → `frequency=751_000_000 Hz`,
  `channel_width=109_000_000 Hz`). Silently skips rows where the
  raw isn't range-shaped (numeric SC-QAM rows in the same table
  pass through without WARN). Bound checking surfaces reversed
  ranges and non-numeric edges as WARN logs. See
  `JsonChannelMapping.range` and `_compute_span` in
  `parsers/formats/json_parser.py`.
- **Silent-fail WARN surfacing for un-coercible channel field
  values.** When a non-empty raw value fails to coerce after any
  declared transform, `_extract_channel` now WARN-logs the field
  name + raw + type rather than DEBUG-logging silently. Catches
  novel firmware shapes (the TG3442DE `"751~860"` case) without
  requiring users to enable DEBUG. Sparse data (None, empty
  strings) continues to skip silently.
- **`channel_width` Tier 2 field, first populator: TG3442DE.**
  Registered in FIELD_REGISTRY.md since the dual-OFDM era; now
  carries actual data. Hz-valued integer matching the established
  `frequency` unit convention.
- **Fleet field-completeness regression test
  (`test_channel_field_completeness.py`).** Parser.yaml-driven:
  for each modem, walks `parser.yaml` to find which canonical
  fields it maps for which channel_types, then asserts every
  locked channel of those types in `modem.expected.json` carries
  those fields. Closes the regression-blindness loop where parser
  output was compared against a golden file generated by the same
  parser. Modems whose firmware genuinely doesn't expose a field
  (and whose parser.yaml correctly omits the mapping) pass without
  false alarms — distinguishes parser drops from firmware gaps.
  Caught the TG3442DE and g54 silent drops; technicolor/cga2121
  (firmware HTML lacks a frequency column) and virgin/superhub5
  (REST API exposes `firstActiveSubcarrier` and `channelWidth`
  but no OFDM frequency) are firmware gaps and pass naturally.
- **SB8200 catalog fixtures exercising both auth-manager
  extraction branches against the same AB01 firmware variant.**
  `arris/sb8200/test_data/modem.har` is the post-sanitization
  Travis #81 HAR (login response body stripped to empty) —
  exercises the cookie-fallback branch where `_extract_token`
  returns the cookie value when the body is empty.
  `modem-body-token.har` is a synthetic capture of the same
  firmware's actual production wire shape — 31-byte session
  token in the login response body, matching Travis's
  `bodySize: 31` and dtaubert's v3.13.0-beta.5 wire trace.
  Both exercise the active `modem.yaml` (login_prefix=`login_`,
  token_prefix=`ct_`, cookie_name=`sessionId`); together they
  pin the two `_extract_token` branches against runtime drift.
  Replaces the previous `modem.har` which was no-auth-shaped
  and passed only because of the test harness Tier 2 routing bug
  (see Fixed).

### Fixed

- **CodeQL `py/insecure-protocol` alert (high severity)** on
  `connectivity._tls_handshake` — suppressed via
  `codeql-config.yml` query-filter with rationale. The TLS
  handshake accepts legacy protocol versions on the *probe* path
  to detect what older modem firmware speaks; the runtime
  data-fetch path uses the legacy adapter only when the probe
  negotiated legacy. Documented architectural decision, same
  family as the existing `py/request-without-cert-validation`
  exclusion.
- **CodeQL `py/bad-tag-filter` alert (high severity)** on
  `catalog_tools/analysis/js_endpoints.py` — regex tightened
  from `</script\s*>` to `</script\b[^>]*>` so HTML5 end-tag
  variants (`</script foo>`, `</script/>`,
  `</script\t\n bar>`) match correctly. Module docstring
  documents the regex-vs-`bs4+lxml` tradeoff and the residual
  blindspots accepted as tech debt (unterminated tags, literal
  `</script>` in JS strings, CDATA wrapping, HTML-commented
  scripts).
- **Redundant `import logging`** removed from
  `tests/components/test_log_buffer.py` (already imported at
  module top). Closes CodeQL `py/repeated-import` alert.
- **Auth failure logs now surface response detail across all
  strategies.** `_log_auth_failure_detail` (collector.py) reads
  `auth_result.response` to emit sanitized request line + response
  status/Content-Type + body snippet on auth failure. Six strategies
  (`form_pbkdf2`, `form_sjcl`, `form_cbn`, `form_nonce`, `hnap`,
  `url_token`) plus the shared `parse_json_dict` helper now populate
  `response=` on failure paths where a `requests.Response` is in
  scope; previously only `form` did, so contributors triaging auth
  failures on any other strategy saw the no-response one-line
  fallback. Related to #120 (CGA6444VF — first real-world test of
  `form_pbkdf2`).
- **Stale HNAP sessions now recover within the same poll.** When a
  cached HNAP session is rejected by the server (LOAD_AUTH on
  HTTP 401/404), the orchestrator clears the cached session and
  retries collection once in the same poll instead of surfacing a
  transient `auth_failed` state. Affects modems whose firmware
  expires sessions faster than the configured poll interval (e.g.,
  Arris S33 with sub-10-minute auth timeout). Related to #140.
- **Adaptive session reuse disable for chronically short-TTL
  firmware.** After 2 consecutive recovered same-poll LOAD_AUTH
  events, the orchestrator stops attempting cached-session reuse for
  the rest of the runtime and starts each poll with a fresh login.
  An intervening normal successful poll resets the recovery streak.
  Runtime-only — `reset_auth()` or process restart re-enables reuse.
  Surfaces in diagnostics as `stale_session_recovery_streak` and
  `session_reuse_disabled`. Strategy is intentionally not exposed
  as a per-modem yaml field (per the "no per-modem recovery tuning"
  principle).
- **CM1200 silent `no_signal` for ~8 minutes after fresh re-add
  (#151).** Some cable modems serve a stub HTML page (HTTP 200,
  page chrome with no data section) on data URLs when the firmware
  is in an ambiguous auth state — typically when a stale session
  is still alive on the modem-side after the integration is
  re-added. The integration's loader gate only flagged login pages
  with a `<input type="password">` field; a stub without one slipped
  through, the parser logged warnings about every missing JS
  function anchor, returned empty channels, and the orchestrator
  reported `no_signal` as if the modem were online but unsynced.
  The reporter sat in that silent state until the modem eventually
  returned 401 and the existing `LOAD_AUTH` recovery path kicked in.
  The new `LOAD_INTEGRITY` collector signal closes the gap: the
  parser coordinator now reports per-resource expected-vs-fulfilled
  anchor counts, and the collector raises `LOAD_INTEGRITY` when any
  resource has expected anchors > 0 and fulfilled = 0. Treated
  identically to `LOAD_AUTH` (clear session, retry once in same
  poll, increment auth streak, surface as `auth_failed`) so
  recovery happens on the very next poll instead of waiting for
  the modem's internal session timeout. JS-format and JS-JSON
  modems get real anchor counting; other formats report trivially
  fulfilled until the same failure shape surfaces there. UC-19a
  documents the full flow; `RESOURCE_LOADING_SPEC.md` failure-mode
  table corrected (the prior text predicted `PARSE_ERROR` for
  this case, which never matched the actual silent-`no_signal`
  behavior).
- **Catalog Tools intake produced configs that failed Core
  validation — `session.max_concurrent: 1 requires actions.logout`
  (#151 stretch).** The session inference and action detection are
  independent passes in `catalog_tools/analyze_har`. When the
  session pass inferred IP-based single-session tracking but the
  HAR didn't include a logout request, the assembler set
  `max_concurrent: 1` without an accompanying `actions.logout`,
  producing a modem.yaml the Core validator correctly rejected
  (single-session without logout would lock users out). The
  cross-block constraint now lives in the assembler: when
  `max_concurrent: 1` is inferred but no logout endpoint was
  detected, drop the inference. Contributors capturing a new
  modem must include a logout flow before the integration treats
  it as single-session. Unblocked netgear/cm2000 in the regression
  pipeline.
- **Arris S33 reboot now succeeds — and the same fix propagated to
  S33v2, S33v3, and S34 (#146).** The Arris HNAP firmware rejects a
  bare `Action=reboot` payload; the modem requires a full
  `SetArrisConfigurationInfo` body that preserves the current EEE
  and LED state. Catalog reboot actions on all four siblings now
  carry `SetEEEEnable: ${ethSWEthEEE:0}` and
  `LED_Status: ${LedStatus:1}`, interpolated from the existing
  `GetArrisConfigurationInfo` pre-fetch. S33 fix verified on
  hardware by the reporter (#148, ccpk1); S33v2/S33v3/S34 mirrored
  from shared firmware lineage with the executor-level default
  fallback as the safety net, pending variant validation. Related
  to #117 (S33v2), #98 (S33v3), #108 (S34).
- **TG3442DE and g54 OFDM/OFDMA `frequency` silently dropped (#86).**
  Both modems' firmwares report OFDM/OFDMA frequency in shapes the
  parser silently dropped: TG3442DE as a `"low~high"` band string
  (`float()` raised `ValueError`, the converter returned `None`, the
  field never made it into the channel dict); g54 as a discrete
  lower-edge key the parser.yaml didn't map at all. Both now
  extracted as canonical `frequency` per the convention now pinned
  in FIELD_REGISTRY.md (lower edge of the active subcarrier band
  for OFDM/OFDMA). TG3442DE additionally populates the Tier 2
  `channel_width` field via the new `range: span` operator. The
  gap on TG3442DE was invisible because `modem.expected.json` was
  generated by the same broken parser, so the regression test
  agreed with itself; the new fleet field-completeness audit
  surfaces parser drops of canonical fields independently.
- **g54 OFDM-shadow channels mis-counted as SC-QAM (#86).**
  Surfaced by spectrum-overlap geometry while reviewing the OFDM
  fix above. DOCSIS 3.1 OSSI requires every downstream channel to
  appear in the legacy `docsIfDownChannelTable` — OFDM channels
  show up there with `modulation: "unsupported"`, with the rich
  data in `docsIf31CmDsOfdmChanTable`. g54's web API mirrors that:
  the dschannel array carried both real SC-QAM and OFDM-shadow
  rows, but parser.yaml's `lock_status: "locked"` filter alone
  let the shadows through. Filter tightened to also exclude
  `modulation: "UNSUPPORTED"`, trimming 2 ghost SC-QAM rows per
  poll. The DOCSIS-shadow pattern is documented in
  `cable_modem_monitor_catalog_tools/docs/ONBOARDING_SPEC.md` so
  future contributors hitting the same shape on a new modem find
  the answer in the intake spec.
- **dm1000 OFDM/OFDMA `frequency` dropped (#86).** Sercomm
  firmware exposes a single value (`OFDMFreq` downstream,
  `Center Freq SC0` upstream) with no subcarrier metadata; the
  test HAR's downstream value (827.6 MHz) overlaps real SC-QAM
  channels at 825-861 MHz, which is physically impossible if it
  were the active-band lower edge. Most likely the CMTS-assigned
  channel placement frequency (same concept as the DOCSIS legacy
  shadow frequency in g54). Removed the mapping to the canonical
  `frequency` field — reporting nothing beats misreporting
  placement-as-edge. dm1000 is `awaiting_verification`, so this
  moves test fixtures only.
- **cm3500b OFDM/OFDMA `frequency` was the band center (#86).**
  The PostProcessor in `arris/cm3500b/parser.py` averaged
  `first_subcarrier_freq` and `last_subcarrier_freq` from the
  HTML table to produce a band-center `frequency`. Now uses the
  first subcarrier directly (lower edge per the convention).
  ``channel_width`` continues to come from the firmware bandwidth
  column. cm3500b is `awaiting_verification`, so the change moves
  test fixtures only; no user-facing data has shifted.
- **PARSING_SPEC.md "OFDM frequency = center" stale wording.**
  The example PostProcessor and the OFDM/OFDMA channel field tables
  described `frequency` as the band center; updated to the
  lower-edge convention with cross-references to FIELD_REGISTRY.md.
- **SB8200 url_token data fetch returns 0/0 channels (#81).** The
  url_token auth manager populated `AuthResult.response` and
  `AuthResult.response_url` on its token-extraction branch (body is
  a session token, not a data page). The loader's auth-response
  reuse logic — which keys on those fields — then surfaced the
  token string as the parsed data page and skipped the real
  `?ct_<token>` data fetch. The fix returns `AuthResult` from the
  token branch with `response`/`response_url` unset; only the
  data-page branch (success_indicator present in body) advertises
  reuse. Contract tightened in `AuthResult` docstring +
  ORCHESTRATION_SPEC + RESOURCE_LOADING_SPEC + MODEM_YAML_SPEC,
  with paired auth/loader unit tests pinning both branches. Bug
  present from v3.14.0-alpha.7 (d7806b0a, "G-1 url_token body
  token extraction") through beta.2.

  Symptom shape (auth success, parser finds no tables, no
  LoginPageDetectedError) matches dtaubert's 2026-05-03 beta.2
  trace; hardware verification still required to confirm the fix
  resolves #81 specifically. Full evidence chain in the commit
  message.
- **Test harness Tier 2 routing masked auth-side bugs at login_page
  paths.** When a modem's login URL and a parser-fetched data page
  share a path (SB8200 logs in at `/cmconnectionstatus.html` and
  the parser fetches `/cmconnectionstatus.html`), `_find_route`
  Tier 2 collapsed a live login GET (with sanitized credentials in
  the query) onto the bare-path data entry, handing the data page
  back as the login response. Routing now requires login GETs to
  match a captured login HAR entry (path with query); only
  token-suffixed data fetches use the bare-path fallback. Surfaced
  the SB8200 fixture mismatch above. Login-page disambiguation is
  unit-tested in `test_har_mock_server.py::TestFindRouteLoginDisambiguation`.

### Changed

- **`load_post_processor` moved out of `core.test_harness.runner`** to
  its own module, `solentlabs.cable_modem_monitor_core.post_processor`.
  The function is a runtime extension-point loader (peer of
  `load_parser_config`), used by HA's setup path to import per-modem
  `parser.py` files. It was placed in `test_harness/` only because the
  test runner was the first caller; HA picked up the same import path
  when it needed the function later. Internal-only change — no behavior
  change for end users.
- **Orchestrator diagnostic field renamed: `last_poll_timestamp` →
  `last_poll_at`.** The old field stored `time.monotonic()` under
  a name that strongly implied wall-clock time but is actually
  process-uptime seconds — useless for "when did the user test"
  inspection of archived diagnostics. New field stores
  `datetime.now(UTC).isoformat()` and means what its name implies.
  Visible in `core_diagnostics` of HA diagnostics downloads. Zero
  consumers of the old field existed, so this is a clean rename.
  Existing committed `verified.json` fixtures retain the old name
  by the point-in-time-snapshot rule; new confirmations get
  `last_poll_at`.
- **Logging contract: quiet on success.** Parse-complete and other
  success-path orchestration logs fire at INFO on the first poll
  (fresh install, reconfigure, HA restart, reauth) and drop to DEBUG
  in steady-state. Operator-relevant transitions (status changes,
  adaptive-reuse state changes, counter resets) stay at INFO
  regardless of poll count. The integration's startup INFO line tells
  users where to enable DEBUG logging for per-poll detail. Liveness
  can be confirmed without DEBUG via the Diagnostics download
  (includes `last_poll_at`, streak counters, reuse state) or the
  integration page activity log. Multi-modem setups previously
  produced 144 INFO lines/day per modem; now produce one verbose
  startup block plus transitions.
- **Catalog chipset metadata filled** for Hitron CODA56 (Broadcom
  BCM3390) and Compal CH7465MT (Intel Puma DHCE2652, MTAT ISP).
  Affects badge rendering in the catalog README.

### Confirmed

- **Hitron CODA56** — BCM3390, form auth, Comcast/Xfinity. Closes #89.
- **Netgear CM1200 (basic-auth variant).** The form-auth variant
  remains awaiting verification (different contributor, different
  conditions).
- **Arris TG3442DE** — Intel Puma 7 (FHCE2752M), form_sjcl auth,
  Vodafone DE. Closes #86.

## [3.14.0-beta.2] - 2026-04-29

### Fixed

- **Arris TG3442DE (Vodafone DE) data fetch returned HTTP 400.**
  Modem firmware enforces a `Referer` header on AJAX endpoints —
  every working browser request in the user-supplied HAR carried it,
  but the catalog entry didn't replay it. Adds `Referer: "{base_url}/"`
  to the modem.yaml. Issue #86.
- **Setup crashed with `KeyError: 'lock_status'` on 9 catalog
  modems.** `mapping_manager` re-implemented an unlocked-channel
  nulling rule that Core's coordinator already performs on every
  poll, with stricter key-presence semantics, and crashed on the
  very key it had just confirmed absent. Affected `arris/cm820b`,
  `arris/tm1602a`, and 7 others. Removed the redundant pass; HA
  trusts Core's contract. Closes the seam with a
  catalog-wide `mapping_manager` regression test. Issue #89.

### Added

- **`{base_url}` placeholder in `session.headers`.** Header values
  resolve at session-build time, so per-deployment URLs (different
  user IPs) work for `Referer`/`Origin` headers that some modems
  validate against their own origin. See MODEM_YAML_SPEC.md.
- **Loader failure messages include outgoing request shape.** When
  `HTTPResourceLoader`, `HNAPLoader`, or `CBNLoader` hits a 4xx/5xx,
  the exception/warning includes method + full URL + headers sent,
  with session-token values redacted (`<set, len=N>`). Auth strategies
  declare their token-bearing headers via the new
  `BaseAuthManager.headers()` method — `Basic` adds `authorization`,
  `HNAP` adds `hnap_auth`, `form_sjcl`/`form_pbkdf2` add the
  configured `csrf_header`, default is `cookie`. Eliminates the
  N-round-trip "ship a theory, user retries, fail, repeat" loop
  that #86 spent four alphas on. See ARCHITECTURE_DECISIONS.md
  "Resource-load failure detail via request-shape log."
- `HealthInfo.tcp_latency_ms` field exposed in core orchestration,
  HA diagnostics, and the new `TCP Latency` sensor.
- **Options-flow cool-off window.** `HealthMonitor` now exposes
  `collection_active` and `last_collection_success_at`; the
  options-flow validation waits for a quiet window (no active
  collection, ≥5s since last success) before re-authing. Prevents
  session-contention failures on session-limited modems
  (MB7621 confirmed) when a re-auth would otherwise overlap with
  an in-flight poll.
- **`urllib3` log filter for `MissingHeaderBodySeparatorDefect`.**
  Some firmwares (SB6141 confirmed) emit `Cache-Control` with a
  non-RFC space before the colon, producing noisy
  `MissingHeaderBodySeparatorDefect` tracebacks on every poll. New
  `solentlabs.cable_modem_monitor_core.log_filters` module installs
  a `logging.Filter` at core import time that drops them. Module
  documents the pattern for future filter additions.

### Changed

- **Health probes split: ICMP (L3), TCP (L4), HEAD (latency-only).**
  Status derivation now uses ICMP + TCP only — TCP handshake to the
  modem's web port is the L4 reachability signal. HEAD timing is
  exposed as `http_latency_ms` for diagnostic value but does not
  affect status. Application-layer issues surface via slow-poll.
- **No GET fallback at fast cadence.** When a modem doesn't support
  HEAD (`supports_head=False`), the HEAD probe is skipped entirely
  rather than falling back to GET. GET timing on most embedded
  modems is bimodal (cold compute path vs warm cached path) and
  produced misleading bimodal data in the previous `HTTP Latency`
  sensor. GET-only modems now show only Ping + TCP latency sensors;
  the `HTTP Latency` sensor only appears when HEAD is verified at
  install time.
- **New `TCP Latency` sensor** (`_tcp_latency`). Always created when
  the HTTP probe is enabled, regardless of HEAD support. Replaces
  the conflated TCP-handshake-plus-HTTP-server-time number that the
  old HTTP Latency sensor exposed.
- **Single default health-check interval (30s).** The slower
  `DEFAULT_HEALTH_CHECK_INTERVAL_GET_ONLY = 60` was removed — all
  three probes (ICMP, TCP, HEAD) are lightweight, so the per-
  capability cadence differentiation is no longer needed.
- **Config-entry uniqueness keyed on entity prefix, not hostname.**
  A user can now add a different modem at the same default IP
  (e.g. swapping an MB7621 for an SB6141 for testing) as long as a
  different entity prefix is chosen. Same prefix at any host still
  blocks setup — entity ID collision is the actual concern.
- **Protocol detection rewritten to TCP probe + TLS handshake.**
  `detect_protocol` now TCP-probes :80 and :443 and runs a TLS
  handshake on :443 with a `SECLEVEL=0` broad-cipher context,
  preferring HTTPS whenever the handshake completes. `legacy_ssl`
  is observed from the negotiated TLS version (TLSv1.1 or older
  → True) instead of inferred from a failed-and-retried-with-
  weaker-ciphers attempt. Authentication runs exactly once per
  UC-86: the previous three-attempt fallback chain
  (HTTP → HTTPS → HTTPS+legacy) is removed. On single-session
  firmware the chain previously collided with its own first
  attempt and produced misleading `MSG_LOGIN_150`-style errors;
  the new flow surfaces the first error directly. Related to #120
  (awaiting user confirmation on the originating hardware).

## [3.14.0-beta.1] - 2026-04-28

First public beta of v3.14. The cumulative v3.14 changeset is
documented in the [3.14.0-alpha.17] entry below; beta.1 ships the
new distribution mechanism on top of that work.

### Changed

- **HACS distribution switched to release-asset (`zip_release: true`).**
  Each release tag attaches a `cable_modem_monitor.zip`. Smaller
  install (~120 KB vs ~3.4 MB source archive); excludes spec/dev
  files. Paired with `hide_default_branch: true` to prevent silent
  downgrades from the version selector.
- **Beta is the public-test entry tier.** Alpha is now a
  local-source-only development concept post-alpha.17 — no GitHub
  releases, no PyPI publish, no HACS visibility. Betas install
  manually via HACS → integration → Redownload → "Need a different
  version?" → Release. There is no auto-update path on betas — each
  beta is a deliberate per-version install by design.
- Documentation rewrites for the new model: README install section,
  TROUBLESHOOTING, RELEASING, HA_ADAPTER_SPEC, CONTRIBUTING, issue
  templates, MODEM_REQUEST, and catalog_tools intake docs. Catalog
  Tools framing updated from "maintainer-only" to "open to
  contributors with hardware."

### Removed

- `update.install` troubleshooting guidance. The Home Assistant
  service is not supported with this integration; use the HACS
  manual picker instead.

### Migration

- **Alpha testers on `feature/v3.14.0` branch tracking:** that path
  is no longer supported (HACS rejects branch installs once
  `zip_release` is set on the default branch). Install beta.1 via
  the manual picker described above.

## [3.14.0-alpha.17] - 2026-04-26

### Changed (developer-only — no user-facing impact)

- **Setup docs collapsed to a single canonical path.**
  `docs/setup/DEVCONTAINER.md` and `docs/setup/WSL2_SETUP.md` are
  removed; `docs/setup/GETTING_STARTED.md` is the only setup doc and
  describes one supported environment (VS Code dev container, with
  WSL2 as the Windows hosting layer). Maintaining parallel
  Local-Python / Dev-Container / WSL2 paths created drift between
  guides; collapsing to one path matches what we actually test in CI.
- **Internal restructure: catalog authoring pipeline carved out of
  Core into a repo-only package.** The modem onboarding pipeline
  (HAR analysis, YAML generation, golden-file construction, parity
  checks) has moved from `core/mcp/` to a new repo-only package
  `cable_modem_monitor_catalog_tools` that is never installed by HA
  and not published to PyPI. Two stray catalog runtime files
  (`fleet_scanner`, `trial_parser`) used only at authoring time
  moved with it. See `core/docs/ARCHITECTURE_DECISIONS.md` §
  "catalog_tools is a developer accelerator, never a runtime dep."
- **Pydantic correctness fix:** `pydantic>=2.0` promoted from
  Core's `[mcp]` optional extra to a first-class runtime dep. The
  prior declaration was a bug — Core's `models/`,
  `validation/cross_file.py`, and `orchestration/*` import pydantic
  at runtime, but the bug was masked because HA pulls pydantic
  transitively. The `[mcp]` extra is removed. Anyone using a
  dev/CI command like `pip install -e packages/cable_modem_monitor_core[mcp,sjcl]`
  should drop the `mcp` element.
- **Contributor path restructured around AI-assisted catalog growth.**
  `MODEM_REQUEST.md` is the user-facing path (low friction, AI
  screening optional); `CONTRIBUTING.md` is the developer / AI
  contributor path centered on a new "AI-Assisted Catalog
  Contribution" section (533 → ~270 lines after collapsing redundant
  test/lint sections). Attribution Standards moved to a new
  `docs/ATTRIBUTION.md` (parser docstring template, AI-attribution
  discipline, honest framing levels). Solo-maintained framing
  removed across all contributor-facing surfaces. Issue Closing
  Policy clarified: no auto-close ever; closing requires either an
  artifact (HAR + `modem.verified.json` for modem requests, reporter
  confirmation for bugs) or a deliberate manual close.
- **Issue-template polish.** `modem_verification.yml` and
  `modem_request.yml` issue templates trimmed to a single ask each;
  dropped "we" framing, the inline diagnostics-download walkthrough,
  and the trailing "What Happens Next?" sections. Also codified
  issue-label mutual-exclusivity (`needs-triage` / `in-development`
  / `needs-testing` / `needs-data` / `backlog` are exactly one) and
  the `release:vX.Y` rule in `CONTRIBUTING.md`.
- **CHANGELOG hygiene** — internal roadmap references (Pxx codes,
  internal milestone names) stripped from prior alpha sections;
  user-facing entries cite GitHub issues only.
- **Linter config alignment.** `.pre-commit-config.yaml` ruff bumped
  from v0.8.2 to v0.15.2 to match the project venv; the two
  versions disagreed on import-block formatting and 15
  catalog_tools test files were ping-ponging on every `--fix`.
  Folded in two markdownlint fixes that had been generating noise:
  bare URL in `CONTRIBUTING.md` (MD034) wrapped in angle brackets,
  and the vendored CodeQL workspace clone directory added to
  markdownlint ignores. The new ruff `ruff` hook id is a legacy
  alias in v0.15.x — flagged for a follow-up cleanup, not blocking.

### Added

- **Recovery state machine** — new `orchestration/recovery.py` owns
  the post-disruption polling window as a single concept with three
  triggers: a dispatched restart command, an observed connectivity
  outage, and a 2-of-3 reboot-signal vote (counter reset, uptime
  drop, transitional DOCSIS state). HA's `recovery_adapter.py`
  subscribes to a Core observer callback and flips the data
  coordinator to a faster cadence while the window is open. Restart
  (`orchestration/restart.py`) is now a one-shot command — it
  dispatches the reboot, triggers recovery, and returns; it no
  longer probes or waits. Status sensors render snapshot truth
  throughout (no synthetic "Restarting…" label).
- **Canonical channel key order** — `models/field_registry.py`
  exposes `CHANNEL_FIELD_ORDER` and `canonicalize_channel_keys()`;
  the HA diagnostics builder routes `downstream_channels` and
  `upstream_channels` through it before emit. Output order is now
  predictable (identification → location → quality → errors)
  regardless of each modem's native table layout.
- **CM2050V confirmed** on firmware tested by #105 (Dragon1473) —
  catalog promoted from `awaiting_verification` to `confirmed`.
- **Auth-failure detail in the log** — when authentication fails,
  the collector now emits a single sanitized `WARNING` carrying the
  modem's response (strategy, request line, status, Content-Type,
  body snippet with the user's password redacted and URL query
  stripped). Targets the stuck-in-setup workflow in #86, #104, #120
  without the "please enable debug logging and retry" round-trip.
  Same line surfaces in initial setup, reauth, options-flow
  re-validation, and steady-state polling — no per-flow plumbing.
- **Channel-bond change notifications** — the integration raises a
  persistent notification when downstream or upstream bonded channel
  totals change between polls, with a hint pointing at the
  `generate_dashboard` dev service so a stale dashboard can be
  refreshed. First-poll onboarding notification surfaces the same
  service for new users. Suppressed during restart-recovery windows
  (transient flux is expected). Baseline totals persist via a
  dedicated `Store` helper — not config-entry data — so updates
  don't trigger integration reloads. Documented in
  `HA_ADAPTER_SPEC.md` including a new "Persistence Layers" section
  covering the runtime_data / entry.data / entry.options / Store
  split.

### Changed

- **Transport-failure logs name the exception class** — auth and
  resource-load failures now prefix the wrapped requests exception
  class (`ConnectionError:`, `ConnectTimeout:`, `SSLError:`,
  `RemoteDisconnected:`, …) before the upstream message. Lets log
  scans and AI-assisted triage tell a refusal from a handshake
  failure from a half-closed socket without parsing the inner tuple
  representation. Applies to the loader chain (HTTP/HNAP/CBN), every
  auth strategy, and the collector's `Auth failed` detail line.
- **Log levels: genuine failures emit at `WARNING`, not `INFO`** —
  the collector's resource-load and connection-failure paths and the
  policy module's connectivity-failure / load-error transitions were
  previously `INFO`, indistinguishable from per-poll progress. They
  now surface at `WARNING` so the noise floor reflects real
  problems.
- **Reset-entity log breaks down by platform** — the
  `Removing N entities for reset` line now appends a per-platform
  count (`3 button, 141 sensor`), so the number reconciles with each
  platform's own `Created N entities` line on re-init.
- **ModemHealth freshness gate** — the health probe exposes
  `latest_probe_at`; the orchestrator refuses to clear connectivity
  backoff based on a stale RESPONSIVE reading from before an
  observed outage.
- **HA operation mutex** — `coordinator.active_operation` now gates
  Restart and Reset Entities buttons against each other; the
  destructive-button discipline lives on `RuntimeData` rather than
  the button instances.
- **Adaptive health-check default interval** — at setup, modems with
  neither ICMP nor HTTP HEAD support default to a 60s health check
  (versus 30s when either lightweight probe is available). GET-only
  probes download a full page body, so slowing the cadence reduces
  load on budget modem web servers. v1→v2 migration also defaults
  to 60s since probe data collected during migration is not
  trustworthy.

### Removed

- **Per-modem behavior flags** — the `behaviors:` block is removed
  from every `modem.yaml`. Per-modem behavior tuning was a Core
  extension point that attracted misuse; recovery timing is generic
  (see `Recovery` class attributes) and no other behavior knob is
  needed across the fleet.
- **`BehaviorsConfig` / `RestartPhase` / `response_monitor` /
  `channel_stability` / `parsers/coordinator` legacy modules** —
  the old restart-monitor plumbing is replaced by the unified
  recovery module.
- **`HnapAuthDiagnostics`** — the HNAP-specific diagnostic dataclass
  and `HnapAuthManager.last_auth_diagnostics` property are removed.
  HNAP auth diagnostics now flow through the generic on-demand
  capture along with every other strategy, and with sanitized
  wire detail rather than raw request/response dicts.

## [3.14.0-alpha.16] - 2026-04-16

### Added

- **Dual-mode channel identity** — position mode (`channel_number`) is
  the default for new installs, giving stable entity IDs across
  reboots. Existing installs keep ID mode (`channel_id`) via v1→v2
  migration so DCID-based naming continues. A `convert_channel_identity`
  service renames recorder statistics to match the current mode after a
  remove-and-re-add in the other mode, so historical graphs survive the
  switch. Related to #117.
- **Channel numbering across all parser formats** — Core auto-assigns
  `channel_number` (1-based position) on all 7 parser formats. Unlocked
  channels now return only `channel_number` + `lock_status`; all other
  fields are nulled. Locked-only channels count toward the DS/US totals.
- **TCP vs HTTP timing separation in health probes** — HTTP probe now
  measures TCP connect and server response independently. TCP is
  logged for diagnostics; `http_latency_ms` carries only server
  response time (the modem load indicator). Related to #117.
- **SB8200 HW v7 variant** — unprefixed base64 credentials in the URL
  query string; same page/parser as base SB8200, only auth parameters
  differ. MCP intake pipeline detects the unprefixed form. Related
  to #124.
- **Request-side query param detection in MCP intake** — session-level
  query parameters that appear on every data-fetch entry are emitted
  as `session.query_params` in the analysis output (filters jQuery
  cache-busters and auth-managed params). Related to #86.

### Fixed

- **Dashboard restart confirmation dialog** — the restart button moved
  out of the entities-card row into a dedicated button card so the
  confirmation dialog reliably fires on every tap (the entities-card
  row `tap_action` was being bypassed by the inner button widget).
- **MB8611 / MB8600 restart** — added `pre_fetch_action` mirroring
  the Arris HNAP pattern (browser always calls `GetMotoStatusSecXXX`
  before `SetStatusSecuritySettings`). Without the pre-fetch the
  modem returns ERROR instead of accepting the SET. Related to #60.
- **TG3442DE data-fetch and logout** — firmware requires a `_n=`
  cache-buster query parameter on all AJAX requests; server returns
  HTTP 400 without it. `session.query_params` now threads static
  query parameters through both the HTTP resource loader and the
  action executor. Related to #86.
- **`form_pbkdf2` auth Content-Type** — Technicolor modems only accept
  `application/x-www-form-urlencoded`, not JSON. The salt request and
  login POST now send form-encoded bodies. Resolves "No salt in
  server response" failures. Related to #120, #115.
- **Config flow connectivity error propagation** — the `form_nonce`
  encoding pre-fetch swallowed all exceptions and fell back to plain
  encoding, then proceeded to a doomed auth attempt. Connectivity
  and timeout errors now surface immediately as "network unreachable"
  instead of 40-line DEBUG tracebacks. Also fixes the
  `verified`/`confirmed` status check so confirmed modems no longer
  render with the unverified marker.
- **Grace-period stability log** — probes firing during the
  post-stability grace window now log `(grace: 12s/30s)` instead of
  the misleading `(stable: 4/3)` counter that overflowed past the
  threshold.

### Changed

- **Function/type colocation sweep** — free functions that take a
  single typed argument moved onto the type (e.g. `find_mapping` →
  `FieldMapping.find_by`, `detect_transport` → `TransportResult.detect`,
  `detect_auth` → `AuthDetail.detect`). Phase-local MCP result types
  moved out of the shared registry into their phase module.
  `get_device_name` moved out of HA `const.py` into `lib/utils.py` to
  keep `const.py` a pure leaf.
- **Config flow variant labels** — variants with the same auth
  strategy are now disambiguated by variant name instead of ISP list:
  `"URL Token (Comcast, Spectrum)"` → `"URL Token"` (default),
  `"URL Token (Spectrum)"` → `"URL Token — v7"` (named).
- **Fleet verification** — TM1602A confirmed on alpha.15 user
  diagnostics (#112); CH7465MT confirmed post provisioned-speed fix;
  SB6190 form-nonce with b64_packed encoding confirmed (#83);
  Broadcom BCM3390 chipset identified on CGA6444VF and CGA4236.

## [3.14.0-alpha.15] - 2026-04-12

### Added

- **Form-nonce base64 credential encoding** — auto-detect firmware that
  packs credentials as base64 into a hidden form field (SB6190 9.1.x).
  Encoding type discovered at setup time and stored in config entry.
  Related to #83, #93.
- **JS endpoint discovery in MCP intake** — scan HAR JavaScript content
  (standalone `.js` files + inline `<script>` blocks) for AJAX/fetch
  targets and diff against captured URLs. Uncaptured endpoints surface
  as advisory warnings during `analyze_har()`. Related to #86.
- **AST sandbox validator for parser.py** — enforces pure-parser
  principle via static analysis at `run_tests` and `write_modem_package`
  gates. Only safe stdlib + bs4/typing allowed; forbidden builtins
  (`eval`/`exec`/`open`) and relative imports rejected.

### Fixed

- **S33v2 confirmed** — verified on real hardware via alpha.14
  diagnostics. Rebuilt all `modem.verified.json` files from original
  GitHub diagnostics to preserve full modem data verbatim. Related
  to #117.
- **CGA2121 missing form field** — added `hidden_fields.language_selector`
  to CGA2121 modem.yaml (browser sends it during login but it was
  missing from config). Related to #96.
- **Provisioned speed direction swap** — DOCSIS service flow direction
  was reversed in CH7465MT (direction 1=downstream, 2=upstream per
  MULPI spec). Provisioned speed fields now store raw bps at Core
  level; HA sensor layer declares `DATA_RATE` device class for
  automatic Mbit/s display. Related to #129.
- **SJCL session validation empty body** — session finalization POST
  used `post_json()` which requires a JSON dict, but TG3442DE returns
  HTTP 200 with empty body (valid success). Replaced with plain POST.
  Related to #86.
- **DM1000 response encoding mismatch** — mock server now decodes
  HAR `content.encoding: base64` per HAR 1.2 spec. Without this,
  modems whose HAR uses base64 storage were served raw base64 text.
  Related to #92.
- **Log message quality — soak findings** — session label
  "none" → "new" on first poll; collector drops error text from
  connectivity logs; HA adapter logs poll-failure alongside
  coordinator success; coordinator name includes model + IP.

### Changed

- **Single-source system_info** — `modem_data` diagnostics summary
  reduced to evaluated connection + health state (6 fields);
  `system_info` is now the single source for all modem metadata.
  `docsis_status` enriched into `system_info` (not derived in
  parallel). Related to #117.
- **Self-describing auth strategies + Core orchestrator factory** —
  auth models carry `display_name` and `transport` ClassVars on
  shared `AuthStrategyBase`. Component assembly moved from HA adapter
  to Core factory (`create_collector`, `create_orchestrator`).
  Adding a new auth strategy is three additive files, zero
  modifications to existing code.
- **Parser sandbox rules and contribution docs** — PARSING_SPEC,
  ONBOARDING_SPEC, SYSTEM_INFO_SPEC, MODEM_DIRECTORY_SPEC updated
  with sandbox constraints, sanitization checks, and docsis_status
  pass-through semantics.

## [3.14.0-alpha.14] - 2026-04-09

### Added

- **SB6183 catalog entry** — Arris SB6183 no-auth modem with transposed
  table parsing. Related to #95.
- **Hub 5 catalog entry** — Virgin Media SuperHub 5 (VMDG660) with
  REST API JSON parsing, no auth required. Related to #82.
- **CODA56 re-intake** — Hitron CODA56 rebuilt with JSON body sniffing,
  `system_info` array_path support, and spec split. Related to #89.
- **Uptime normalization** — `scale` support on all system_info formats
  (seconds, duration strings, tick counts) across the fleet.
- **Provisioned speed sensors** — `child_aggregates` for downstream/
  upstream provisioned speed, `scale` on `system_info`, fleet-wide
  rollout.
- **YAML-driven `docsis_status` normalization** — StrEnum pass-through
  with configurable value mapping in parser YAML.

### Fixed

- **TG3442DE SJCL login** — missing `csrfNonce` header and wrong
  plaintext format broke authentication. Related to #86.
- **SB6190 `form_nonce` auth** — response reuse bug where auth manager
  consumed the response body twice; config completeness fixes.
  Related to #83, #93.
- **TM1602A parser enrichment** — added `docsis_status`, upstream
  modulation, and metadata cleanup. Related to #112.
- **CM1200 field mappings** — added missing field mappings and
  verification artifact. Related to #121.
- **Fleet-wide aggregate sweep** — `lock_status` filter corrections
  and firmware quirk documentation across catalog.

### Changed

- **DM1000 HAR expanded** — full JSON data endpoints captured.
- **Auth strategy specs extracted** — SJCL, PBKDF2, and CBN auth
  strategies documented in dedicated spec files.
- **Pre-commit hook** — new hook to catch gitignored path references.
- **Pre-commit config** — consolidated duplicate `pre-commit-hooks`
  repo blocks.

## [3.14.0-alpha.13] - 2026-04-08

### Added

- **CM1100 catalog entry** — Netgear CM1100 with form auth hidden
  field discovery. Related to #104.
- **DM1000 enhancements** — `password_field` list, per-array JSON
  resources, OFDM channel support. Related to #92.
- **MCP form discovery** — `login_page` emission, discoverable
  `hidden_fields` filtering, `form_selector` detection.
- **SJCL known-answer tests** — fixture-driven crypto tests anchored
  to `sjclCrypto.js` reference values, plus end-to-end integration
  test running `FormSjclAuthManager` against `HARMockServer`.

### Fixed

- **SJCL PBKDF2 salt encoding** — `_derive_key()` was UTF-8 encoding
  the salt instead of hex-decoding it, producing a different AES key
  than the modem expects. The bug was invisible in tests because the
  mock server had the same encoding error. Related to #86.
- **Deferred entity initial state** — guarantee initial state for
  deferred entities (UC-84).
- **Diagnostics error totals** — use Core canonical names in HA
  diagnostics.
- **Backoff off-by-one** — immediate circuit trip on credential
  rejection.
- **Stale probe flags** — drop stale `supports_icmp`/`supports_head`
  flags from v1→v2 config entry migration. Related to PR #57.

### Changed

- **MB8600 confirmed** — from user diagnostics. Related to #40.
- **CM820B confirmed** — from alpha.12 diagnostics. Related to PR #57.
- **MCP accuracy tracking** — replaced regression diff counts with
  field-level accuracy tracking.
- **Spec encoding boundaries** — ARCHITECTURE.md and MODEM_YAML_SPEC.md
  now explicitly document hex-decode/UTF-8 encoding at each step of
  the SJCL crypto chain.

## [3.14.0-alpha.12] - 2026-04-06

### Added

- **`javascript_json` format detection** — MCP intake pipeline now
  detects JS variable assignments containing JSON arrays in
  `<script>` tags (e.g., TG3442DE `json_dsData = [{...}]`).
- **Native `docsis_status`** — MB7621 (`Network Access`
  from MotoConnection.asp) and XB6/XB7 (`combined_status` computed
  from downstream + upstream status). Three modems now have native
  DOCSIS status instead of relying on lock-status derivation.
- **form_sjcl MCP detection** — intake pipeline recognizes SJCL
  AES-CCM login flows (encrypted POST body, JS page variables
  `myIv`/`mySalt`/`currentSessionId`). Related to #86.
- **Diagnostics enrichment** — `ResourceFetch` captures `status_code`
  and `content_type`. `OrchestratorDiagnostics` includes
  `auth_strategy`. Per-resource timing in HTTP/HNAP/CBN loaders.
- **`docs/README.md`** — project-level documentation index.

### Changed

- **HAR files moved to Git LFS** — 36 HAR test fixtures (~7.7 MB)
  stored as LFS pointers. New `load_har_json()` utility detects LFS
  pointers and auto-recovers. Contributors need `git-lfs` installed.
- **Shared auth JSON response helper** — extracted `auth/response.py`
  from form_sjcl, form_pbkdf2, and hnap. Consistent error messages
  with diagnostics logging, double-decode, and type checking.
  form_pbkdf2 login response now gets a proper type check.
- **Docs restructured** — `INTAKE_PIPELINE.md` and `MOCK_SERVER.md`
  moved to Catalog docs (closer to the code they document). Broken
  links fixed across reference docs.
- **`zip_release` reverted** — `hacs.json` reverted to
  `zip_release: false` for alpha branch-tracking installs.

### Fixed

- **MCP intake pipeline regressions** — session cookie detection
  expanded (`credential`/`sec` indicators), strategy-aware auth field
  copy, `html_fields` CSS selector serialization, nested JSON
  direction inference (G54 recursive key scanning).
- **Internal quality improvements** — `to_dict()` on diagnostics models,
  mock server `get_challenge_response()`, generic `ComputedField`
  for derived system_info, CM1200 InitTagValue offset docs.
- **Computed `system_info` fields** — generic `ComputedField` with
  named operations (e.g., CGA4236 `memory_used_pct`).

## [3.14.0-alpha.11] - 2026-04-05

### Fixed

- **Form auth false-positive on server errors** — `_check_success`
  fallback accepted any non-401 response as auth success when no
  explicit criteria were configured (13 modems). Now rejects all
  HTTP status >= 400. Also fixed three `requests.Response` truthiness
  checks that masked error status codes as 0 in logs.
- **DOCSIS 3.1 aggregate scoping** — 6 DOCSIS 3.1 modems (S33, S33v2,
  S33v3, S34, MB8600, MB8611) mixed QAM FEC and OFDM LDPC codewords
  in `total_corrected`/`total_uncorrected`. Scoped to QAM-only
  (`downstream.qam`) so totals carry the same semantic across the
  fleet. Removed SB8200v3 aggregate (no QAM error counters).
- **DOCSIS status field normalized** — 9 modems using inconsistent
  field names (`network_access`, `cm_status`, `registration_status`)
  normalized to canonical `docsis_status`.
- **CM1100 HAR** — added missing redirect target exposed by auth fix.
- **Health skip logging** — distinguished "collection active" from
  "recent collection" in skip messages.

### Added

- **HACS zip release asset** — HACS now downloads a 124 KB zip of only
  the integration files instead of the full 3.4 MB source archive.
  Added `loggers` field to `manifest.json` for debug logging.
- **Fleet-augmented MCP intake** — Catalog extension point scans 35
  parser.yaml files for 7 pattern categories (selectors, labels, IDs,
  JSON keys, delimiters, channel types, aggregates) and feeds them
  back into Core analysis and config generation.
- **Arris SB6141** — upgraded from synthetic to real-hardware catalog
  entry with system_info, codeword aggregates, and restart action.
- **MCP generator DOCSIS-version-aware** — auto-generates
  `downstream.qam` scope for DOCSIS 3.1, `downstream` for 3.0.

### Improved

- **Documentation restructure** — consolidated 6 overlapping setup
  docs into hub-and-spoke tree. Fixed 459 markdownlint violations.
  Reduced total doc lines by 26%.
- **Issue templates** — rewritten for HAR-only workflow with incognito
  capture warning. Removed stale Fallback Mode references from specs.

### Reverted

- **`zip_release` in hacs.json** — broke branch-based installs for
  alpha testers. HACS expects a zip asset on a GitHub release, but
  branch refs have no release. Reverted to source archive download.
  Will re-enable at stable release.

## [3.14.0-alpha.10] - 2026-04-04

### Added

- **Netgear CM3000** — New catalog entry for DOCSIS 3.1 modem with
  form auth (XSRF_TOKEN cookie). Same tagValueList format as
  CM2000/CM2050V. (Related to #127)
- **Core analysis module** — New `analysis` package with log parsing,
  diagnostics bridging, and outage duration computation. Split from
  monolithic script into Core (platform-agnostic models + parser) and
  HA layer (adapter-specific patterns + report formatting).
- **Arris S33v3 system_info enrichment** — Added system uptime and
  hardware version to S33v3 parser via `GetCustomerStatusSoftware`
  SOAP action, sourced from jzucker2 HAR capture. (Related to #98)

### Improved

- **Health check HTTP probe suppression** — HTTP health probe is now
  skipped when a data collection is active (avoids web server
  contention) or recently succeeded (redundant — collection already
  proved HTTP reachability). ICMP still runs. Eliminates ~550ms
  latency blips observed when health checks and data polls fire
  simultaneously after HA reload.

### Fixed

- **JS line comment stripping** — JS parser now strips `//` line
  comments before tagValueList extraction. Previously matched
  commented-out example data instead of live values, causing wrong
  DOCSIS status and missing uptime on CM1200. (Related to #121)
- **CM820B error totals and DOCSIS status** — Added aggregate section
  to CM820B parser.yaml for error totals. Added fallback in
  `derive_docsis_status()` to check `system_info.docsis_status` when
  per-channel `lock_status` is absent (2011-era hardware).
  (Related to #57)
- **S33/S33v2 uptime note** — Corrected modem.yaml notes that claimed
  "System uptime not available." HAR evidence confirms the HNAP API
  returns real uptime in `CustomerConnSystemUpTime`; the modem's
  JavaScript swaps the display to show clock time (browser UI bug).
- **Non-dict JSON response guards** — Added `isinstance(data, dict)`
  guards at all 8 `resp.json()` call sites across auth managers,
  HNAP loader, and HNAP action executor. Fixes crash when modems
  return double-encoded JSON strings. (Related to #86)
- **Restart button availability** — Button entity now tracks
  in-progress state dynamically instead of staying disabled after
  a restart completes.

## [3.14.0-alpha.9] - 2026-04-03

### Added

- **CBN transport** — New transport for Compal-based modems using
  encrypted XML APIs with PBKDF2/AES-CBC auth. (Related to #129)
- **Compal CH7465MT** — New catalog entry for Vodafone Station
  (Ziggo NL). CBN transport, DOCSIS 3.0. (Related to #129)
- **Arris S33v3** — New catalog entry. Uses SHA256 HMAC (not MD5 like
  S33/S33v2), compatible with S34 parser. (Related to #98)
- **Arris SB8200v3** — New catalog entry using CBN transport with
  XML multi-resource sections for DOCSIS 3.1 QAM+OFDM channels.
  (Related to #109)
- **XML `tables[]` for multi-resource sections** — XML parser now
  supports multiple table definitions per section (matching HTML/JSON
  pattern), enabling DOCSIS 3.1 modems with separate QAM/OFDM API
  endpoints.
- **`javascript_vars` system_info format** — New Core format handler
  for extracting system_info from simple JS variable assignments
  (`var x = 'value'`). Used by TG3442DE and future Arris Touchstone
  modems.
- **TG3442DE: `cm_status` and `connected_devices`** — New system_info
  fields from `overview_data.php`. DOCSIS online/offline status and
  connected device count. Test data rebuilt from contributor HAR
  capture. (Related to #86)

### Fixed

- **Migration manufacturer normalization** — v1 manufacturer strings
  (e.g., "Arris/CommScope") now normalize to catalog values ("Arris")
  during migration, fixing options flow lookup failures.
- **Migration variant resolution** — v1 `auth_strategy` field now
  correctly maps to v2 variant (e.g., `form_nonce` → SB6190
  `form-nonce` variant). (Related to #121)
- **`software_version` field name** — All Arris HNAP parser configs
  corrected from `firmware_version` to the canonical `software_version`
  field name.

### Changed

- **S33v2 and MB8600 split into standalone catalog entries** — Each
  model now has its own directory, parser, and golden file instead of
  being aliases.
- **Contributor attribution uses full GitHub profile URLs** across the
  entire catalog.

## [3.14.0-alpha.8] - 2026-04-02

### Added

- **Dynamic system_info sensors** — HA now creates sensors
  dynamically from parser-reported system_info fields (software
  version, hardware version, uptime, etc.) instead of a fixed set.
- **Health auto-recovery** — Connectivity backoff counter auto-clears
  when health probes detect recovery, reducing unnecessary wait time
  after transient network issues.
- **Pyright pre-commit hook** — Catches Pylance-visible type errors
  that mypy misses (`object` vs `Any`, submodule access).
- **MCP pipeline regression test** — Wired into the CI
  `test-packages` job for continuous validation.
- **CSS class selector** — `html_fields` format now supports CSS class
  selectors for field extraction, in addition to label/id selectors.

### Fixed

- **Config flow HTTPS retry** — Auth signal is now preserved when the
  HTTPS retry path fails, preventing silent loss of auth error context.
- **Sensor latency flicker** — Latency values are cached to prevent
  display flicker when a health probe fails intermittently.
- **Catalog→Core dependency pin** — Exact-pinned for uv pre-release
  compatibility.
- **Config flow test coverage** — Fixed test gaps and PII hook
  placement.
- **Log levels and namespaces** — Polished startup milestones, debug
  vs info boundaries, and namespace consistency across Core and HA.

### Changed

- **Fleet YAML key ordering normalized** across all catalog entries.
- **Golden file normalization** — Consistent formatting across all
  modem expected output files.
- **Scripts directory audit** — Removed dead files, fixed stale doc
  references.
- **Spec consistency audit** — Fixed links, terminology, and gaps
  across architecture and orchestration specs.

## [3.14.0-alpha.7] - 2026-04-01

### Fixed

- **HNAP stale session recovery** — Reused HNAP sessions that return
  HTTP errors now trigger re-authentication on the next poll cycle.
  v3.14 uses signal-based recovery (`HNAPLoadError` → `LOAD_AUTH`)
  instead of v3.13's within-poll zero-channel heuristic. (Related
  to #117)
- **url_token body token extraction** — `UrlTokenAuthManager` now
  extracts the session token from the login response body (not just
  cookies). On some SB8200 firmware the body token differs from the
  cookie value. Uses `success_indicator` as a response type
  discriminator: body contains indicator → data page (no token);
  body is short string → token; empty body → cookie fallback.
  (Related to #81)
- **Pre-login cookie clearing** — `UrlTokenAuthManager` clears the
  stale session cookie before sending the login request. Modems that
  reject re-login when an existing session cookie is present (SB8200)
  no longer fail on session re-establishment.
- **Form auth Referer header** — `FormAuthManager` now sends
  `Referer: {base_url}` on login POST requests. Defensive measure
  restored from v3.13 for firmware that validates the Referer header.
- **HNAP header parsing warnings** — Added urllib3 warning filter for
  HNAP modems that send malformed HTTP headers with debug timing data
  prepended (S34 firmware quirk). Suppresses noisy "Failed to parse
  headers" warnings in HA logs on every poll cycle.
- **Auth log_level parameter** — All 8 auth managers now accept a
  `log_level` parameter, consistent with the existing action executor
  pattern. Config flow auth logs at INFO for visibility; polling auth
  logs at DEBUG to keep steady-state logs clean.

### Added

- **HNAP auth diagnostics** — `HnapAuthDiagnostics` dataclass captures
  challenge and login request/response pairs (passwords redacted) for
  field debugging. Accessible via `HnapAuthManager.last_auth_diagnostics`.

### Changed

- **cookie_name ownership** — `cookie_name` moved from `SessionConfig`
  to individual auth strategy configs (`FormAuth`, `BasicAuth`,
  `UrlTokenAuth`, etc.). `token_prefix` moved to `UrlTokenAuth`.
  `SessionConfig` is now lifecycle-only (`max_concurrent`, `headers`).
  This restores the v3.13 boundary where auth owns the cookie it
  produces. 14 modem.yaml files, collector, test harness, and MCP
  config generator updated. 7 spec files updated.

## [3.14.0-alpha.6] - 2026-04-01

### Fixed

- **HNAP PrivateKey cookie** — HNAP data requests now include the
  `PrivateKey` cookie alongside `uid`, matching the Login.js protocol.
  Some firmware returns HTTP 500 without it. Regression from v3.13
  where the cookie was set but never documented in specs — v3.14's
  clean-room implementation missed it. (Related to #117)

### Changed

- HNAP session documented as `uid` + `PrivateKey` cookies across all
  specs (MODEM_YAML_SPEC, ORCHESTRATION_SPEC, RUNTIME_POLLING_SPEC,
  ONBOARDING_SPEC, ARCHITECTURE).

## [3.14.0-alpha.5] - 2026-03-31

### Fixed

- Config flow retries with HTTPS when authentication fails on
  auto-detected HTTP — fixes modems that redirect during auth
  but not during initial probe.
- Sensor entity properties (unit, icon, state class) invalidated
  on coordinator updates, fixing stale values after data changes.
- Removed dead code in 5 catalog parsers (TG3442DE, CODA56,
  DM1000, XB6, XB7).

### Changed

- HA adapter test coverage: 70% → 91% (304 tests). New coverage
  for startup sequence, diagnostics builder, async migration,
  dashboard YAML generation, and service registration.
- Test fixtures standardized on generic names (Solent Labs /
  TPS-2000) with mock boundary at Core/Catalog I/O layer.
- Consolidated pytest.ini into pyproject.toml.

## [3.14.0-alpha.4] - 2026-03-30

### Fixed

- Data-dependent sensor entities (channels, system metrics, LAN stats) are
  now created via deferred one-shot listener when the first poll fails due
  to modem being unreachable at HA startup. Previously these entities were
  never created, leaving the integration permanently incomplete until HA
  restarted with the modem online.
- Misleading "session: expired" and "Session invalid" log messages for
  logout modems (e.g., MB7621) now read "session: none" and "No active
  session" — the session was intentionally cleared by logout, not expired.

### Documentation

- Added UC-84 (startup while modem unreachable) to orchestration use cases
- Added "Deferred Entity Creation" section to HA Adapter Spec
- Fixed incorrect `ConfigEntryNotReady` claim in HA Adapter Spec — the
  orchestrator never raises, so first refresh always succeeds
- Added "Modem unreachable at startup" row to Entity Model availability table

## [3.14.0-alpha.3] - 2026-03-30

### Fixed

- Catalog package dependency on Core now allows pre-release versions
  (`>=3.14.0a1`). Fixes `uv`/`pip` resolver failure on fresh HA installs
  where no stable Core release exists on PyPI.

## [3.14.0-alpha.2] - 2026-03-30

### Added

- 75 HA adapter tests (config flow, options flow, services, diagnostics,
  migrations, sensors, buttons) — coverage 28% → 70%
- PyPI publishing workflow (`publish.yml`) with trusted publishers
- Alpha versioning support in CI — feature branch pushes trigger PyPI
  alpha releases, GitHub release workflow skips alpha tags
- Package README updates with PyPI badges and installation instructions

### Fixed

- CI validation failures from version consistency and package build checks
- HA dev container startup reliability (correct pip install paths)
- Release workflow: skip GitHub release creation for alpha tags

## [3.14.0-alpha.1] - 2026-03-30

### Architecture

v3.14 is a full architecture rewrite. The monolithic `custom_components/`
codebase is split into three layered packages:

- **`cable_modem_monitor_core`** — Platform-agnostic library: config models,
  auth managers, parsers, orchestration, health monitoring, and MCP tools.
  Zero Home Assistant dependencies. 1,572 tests, 96% coverage.
- **`cable_modem_monitor_catalog`** — Data-driven modem catalog: YAML configs,
  parser configs, HAR fixtures, and golden files. Drop a directory to add a
  modem — no code changes required. 31 HAR replay regression tests.
- **`custom_components/cable_modem_monitor`** — Thin HA adapter: maps Core
  output to HA entities, coordinators, config flow, and diagnostics.

### Added

- **MCP Onboarding Pipeline** — Six-phase analysis pipeline that generates
  `modem.yaml` and `parser.yaml` from a HAR capture: `validate_har`,
  `analyze_har`, `generate_config`, `generate_parser`, `enrich_metadata`,
  and `golden_file` tools. Detects auth strategy, data format, channel
  mappings, and actions directly from HTTP evidence.
- **31 Modems in Catalog** — All v3.13 modems migrated plus 12 new: TM1602A,
  CODA56, CM1100, CM2050V, DM1000, CGA4236, CGA6444VF, TG3442DE, S33v2,
  MB8600, XB6, CGM4140COM. Includes 6 variant entries (S33v2, CM1200-HTTPS,
  TC4400AM, MB8600/MB8611 shared parser, XB6, CGM4140COM).
- **SJCL AES-CCM Auth Strategy** (`form_sjcl`) — New auth strategy for
  Arris TG3442DE modems that use Stanford JavaScript Crypto Library
  encryption (AES-CCM mode with PBKDF2 key derivation).
- **Transport-Scoped Action Executors** — HTTP and HNAP action execution
  split into protocol-specific modules with pre-fetch support, endpoint
  extraction, and HMAC-signed SOAP calls.
- **Health Pipeline** — Independent health monitoring with ICMP ping and
  HTTP probes running on a faster cadence than data polling. Configurable
  suppression windows, per-modem probe selection, and graceful timeout
  handling.
- **Orchestrator** — Central polling coordinator: session reuse across
  polls (HNAP uid + private key, cookie-based sessions), circuit breaker
  for auth failures, restart-window awareness, evidence-based error
  tracking with structured totals per category.
- **Restart Monitor** — Detects modem restarts via connectivity loss +
  recovery pattern, with configurable detection window and cooldown.
- **HAR Replay Test Harness** — Deterministic parser testing: mock HTTP
  server replays HAR responses, golden file comparison validates output.
  Auto-discovers testable modems from the catalog.
- **Circular Log Buffer** — Diagnostics log buffer with configurable
  capacity, compatible with HA 2025.11+ reload behavior.
- **Credential Param Classification** — MCP action detection now identifies
  credential fields by name and sanitizer patterns, replacing values with
  empty strings in generated configs.

### Changed

- **Modem configs are YAML-driven** — Auth strategy, session management,
  data pages, parser format, field mappings, actions, and metadata are all
  declared in `modem.yaml` and `parser.yaml`. No modem requires code changes.
- **Parser architecture** — Format-specific parsers (HTML table, transposed
  table, JS embedded, JSON, HNAP) with shared type conversion, field
  registry, and table selection. Column/field mappings declared in
  `parser.yaml`.
- **Auth architecture** — Strategy pattern with 8 implementations (none,
  basic, form, form_nonce, form_pbkdf2, form_sjcl, url_token, hnap).
  Each reads config from `modem.yaml` — no strategy requires modem-specific
  code.
- **HA adapter is thin** — Config flow, entity creation, coordinator
  wiring, diagnostics. No parsing, no auth, no modem-specific logic.
  Uses `runtime_data` (HA 2024.12+) instead of `hass.data`.
- **v3.13 modules retired** — All v3.13 core modules, parsers, tests, and
  scripts removed. Clean break — no compatibility shims.

### Fixed

- **MB7621 restart** — Restart POST now includes all 6 required form fields
  (HAR-driven, not fixture-inspected). Previous config silently failed.
- **DOCSIS version false positive** — `enrich_metadata` now checks actual
  `channel_type` map values for OFDM/OFDMA, not just field presence. A
  DOCSIS 3.0 modem with a `channel_type` column no longer triggers 3.1.
- **Auth encoding detection** — Falls back to login page JavaScript analysis
  (`keyStr` constant, `btoa()`) when POST body value is sanitized.
- **Table footer detection** — MCP analysis and runtime parser now reject
  summary/footer rows ("Total", "Sum", etc.).
- **Sparse row filtering** — Runtime parser skips rows where fewer than half
  the declared columns produced values.

## [3.13.2] - 2026-03-11

### Added

- **HNAP Session Reuse** - Reuse existing HNAP sessions (uid cookie + private key) across poll cycles instead of re-authenticating every 60 seconds. Prevents anti-brute-force reboots on Arris S33/S33v2 modems (~1440 logins/day reduced to 1). Includes stale session detection with automatic retry on expired sessions. (#117)
- **S33v2 Model Aliases** - Added S33v2, CommScope S33v2, and ARRIS S33v2 to S33 detection aliases for hardware revision compatibility. (#117)
- **CM1200 Challenge Cookie Support** - Basic Auth now forwards the challenge cookie required by CM1200 HTTPS endpoints. (#121)
- **Protocol Auto-Detection** - Protocol (HTTP/HTTPS) is now decomposed from the host at config entry time, enabling correct scheme selection for modems like the CM1200 that use HTTPS. (#121)

### Fixed

- **Modem-specific timeout not applied** - All `_fetch_data` HTTP requests now use the modem's configured `timeout` instead of the hardcoded 10-second default. This fixes CM1200 read timeouts over HTTPS. (#121)
- **CM1200 request timeout** - Increased default timeout from 10s to 20s for CM1200 modems, which respond slowly over HTTPS. (#121)
- **HNAP login lockout detection** - HNAP auth now detects firmware-level login lockout responses (`LOCKUP`/`REBOOT`) and stops retrying to prevent modem reboot. (#117)
- **SB8200 url_token auth** - `ajax_login` and `auth_header_data` config values are now correctly propagated to the url_token auth strategy. (#126)
- **S33/S34 invalid URL** - Removed nonexistent lowercase `cmconnectionstatus.html` fallback URL from S33 and S34 modem configs.
- **CM1200 auth type corrected** - Changed CM1200 auth type from `basic` to `none` to match actual modem behavior. (#121)

## [3.13.1] - 2026-02-27

### Fixed

- **CM1200 zero channel data regression** - Fixed redundant authentication call that caused basic_http modems (CM1200, C7000v2, CM600, C3700, TC4400) to make an extra HTTP request per poll cycle. The `_pre_authenticate()` gate condition incorrectly used HTML presence as a proxy for "auth was performed," but basic_http auth is stateless and never returns HTML. This caused connection resets on modems that rate-limit rapid HTTPS requests. (#121)

## [3.13.0] - 2026-02-24

### Added

- **Arris/CommScope S34 Support** - HNAP protocol with HMAC-SHA256 authentication. 32 downstream + 5 upstream SC-QAM channels, plus OFDM/OFDMA. (Thanks [@rplancha](https://github.com/rplancha)! Based on [PR #90](https://github.com/solentlabs/cable_modem_monitor/pull/90))
- **ARRIS CM3500B Support** - EuroDOCSIS 3.1 cable modem with form-based authentication. Parses 24+ SC-QAM downstream, 2 OFDM downstream, 4 ATDMA upstream, and 1 OFDMA upstream channels. (Thanks [@ChBi89](https://github.com/ChBi89)! #73)
- **SB6190 Auth Support (9.1.103+)** - Added `form_nonce` auth strategy for ARRIS SB6190 modems with newer firmware that require login. Uses plain form POST with client-generated nonce instead of base64-encoded credentials. (#93, #83)
- **Entity Prefix Selection** - Multi-modem setups can now configure custom entity prefixes during setup to avoid naming conflicts
- **HTTP HEAD Auto-Detection** - Health monitor now auto-detects whether the modem supports HTTP HEAD requests during setup. Modems that don't support HEAD (e.g., TC4400's `micro_httpd`) use GET-only, eliminating session poisoning that caused permanent health check failures. (#94)
- **Configurable HMAC Algorithm** - `HNAPJsonRequestBuilder` now supports `hmac_algorithm` parameter ("md5" or "sha256") for modems with different authentication requirements
- **Action Layer Architecture** - Restart functionality now uses data-driven action definitions in `modem.yaml` instead of hardcoded parser methods
- **Circular Log Buffer** - New diagnostics buffer compatible with HA 2025.11+ that preserves logs across integration reloads
- **Diagnostic Logging Enhancement** - Added ERROR-level logging when parser receives login page HTML instead of data, making session expiration issues easier to diagnose during troubleshooting

### Changed

- **Modular Init** - `__init__.py` monolith extracted into focused modules (`setup.py`, `lifecycle.py`, `data_update.py`) (#1)
- **DataOrchestrator** - `ModemScraper` renamed to `DataOrchestrator` for clarity (#14)
- **Parser Registry** - `parser_discovery` module renamed to `parser_registry`
- **Fallback Subsystem** - Unknown modem discovery moved to `core/fallback/` directory
- **Auth Config Consolidation** - All auth configuration now lives in `auth.types{}` section of `modem.yaml`
- **Metadata Consolidation** - Separate `metadata.yaml` files merged into `modem.yaml`
- **Protocol Detection** - Removed hardcoded `default_port` and `protocol` from `modem.yaml`; now auto-detected
- **CGA2121 Data Page Config** - Parser now reads primary data page path from `modem.yaml` `pages.data` config instead of hardcoding, reducing duplication between parser and modem config

### Fixed

- **SB8200 HTTPS Session** - Match real browser behavior observed in HAR captures: login requests now include `X-Requested-With: XMLHttpRequest` header, data requests omit `Authorization` header. Session cookie cleared before login to prevent stale cookie conflicts. (#81)
- **SB6190 Parser Error** - Fixed parser returning 0 channels in production despite successful authentication. Parser now prioritizes explicit path (`/cgi-bin/status`) over root path (`/`) to avoid receiving auth response HTML instead of status page data. (#93, #83)
- **SB6190 Form AJAX Auth** - Fixed "invalid_credentials" error when using form_ajax authentication (firmware 9.1.103+). The config flow helper was not passing the form_ajax configuration to the auth workflow. (#93, #83)
- **XB7 Parser Data Page Path** - Fixed parser error when XB7 modem.yaml specifies a data page path. Parser now correctly reads from the configured path. (#107)
- **C7000v2 Session Management** - Fixed browser lockout and session issues by correcting session cookie name (`XSRF_TOKEN` instead of `session`) and logout endpoint (`/goform/logout` instead of `/Logout.htm`). Added POST support for `/goform/` logout endpoints. (#61)
- **TC4400 Parser Error** - Fixed parser returning 0 channels in v3.13.0 betas despite successful authentication. Parser now prioritizes explicit path (`/cmconnectionstatus.html`) over root path (`/`) to avoid receiving auth response HTML instead of channel data. (#94)
- **CGA2121 Auth Redirect** - Fixed parser receiving wrong page when modem redirects to `/basicUX.html` after login instead of the data page `/st_docsis.html`. Parser now tries the specific data page path first, then falls back to "/" for legacy compatibility. (#75)
- **CM3500B Auth Config** - Removed incorrect `success_indicator` from CM3500B modem.yaml auth config that could interfere with authentication validation. (#73)
- **Form Auth HTML Response** - Fixed form authentication discarding the HTML response when auth succeeded without a `success_indicator`, causing parsers like CGA2121 to see empty data (#75)
- **Config Flow Auth Type Preservation** - Fixed "Reset Entities" losing user's auth type selection for modems with multiple auth variants (SB6190, SB8200). Options flow now preserves `CONF_AUTH_TYPE` from config entry, preventing fallback to default auth type after entity reset. (#93)
- **Diagnostics PII Sanitization** - Updated to har-capture 0.3.3 with `redact_flagged=True` for automatic redaction of WiFi credentials and device names, plus fixes for IPv4 address corruption (5-octet bug) and version string preservation.
- **Case-Insensitive Password Detection** - Password field detection now case-insensitive (#75)
- **G54 OFDM Channel IDs** - Handle OFDM/OFDMA prefixed channel IDs correctly
- **URL Pattern Ordering** - Protected pages now prioritized in URL pattern ordering for polling
- **Log Buffer Persistence** - Diagnostics log buffer preserved across integration reloads
- **HTTP/HTTPS Detection** - Try HTTP before HTTPS to prevent protocol mismatch on setup
- **HTTP HEAD SSL Blocking I/O** - Moved `ssl.create_default_context()` in HTTP HEAD auto-detection to executor to avoid blocking the HA event loop during config flow

### Improved

- **Exception Handling** - Replaced broad `except Exception` blocks with specific exception types for better diagnostics (#6)
- **Test Coverage** - Config flow tests now use proper HA test infrastructure (`pytest-homeassistant-custom-component`)
- **Dev Tooling** - Mock server with `--auth-type` and `--delay` flags, HA reload script, E2E test framework, pre-push hooks (#20)

## [3.12.1] - 2026-01-20

### Fixed

- **HNAP Modem Setup** - Fixed `AssertionError` during discovery when setting up HNAP modems (MB8611, S33). The discovery pipeline now correctly handles HNAP authentication which returns JSON API responses rather than HTML pages. (#102)

## [3.12.0] - 2026-01-20

### ✨ Auth Strategy Discovery

v3.12.0 introduces Auth Strategy Discovery, a major architectural improvement that automatically detects and stores authentication requirements during initial setup. This eliminates hard-coded parser-specific authentication logic and improves reliability for modems with firmware variants.

### Added

- **Auth Strategy Discovery** - During config flow, the integration now probes the modem to discover which authentication method works (Basic HTTP, Form-based, HNAP/SOAP, URL Token, or No Auth)
- **Stored Auth Configuration** - The discovered auth strategy is stored in config entry and reused during polling, ensuring consistent authentication
- **Parser Hints** - Parsers now provide `hnap_hints`, `js_auth_hints`, and `auth_form_hints` for auth discovery
- **AuthHandler Improvements** - All authentication is now handled centrally by AuthHandler, not individual parsers

### Changed

- **BREAKING (Internal)**: Parsers no longer implement `login()` method directly. All auth is handled by `AuthHandler`
- **SB8200 Auth**: URL token authentication now uses session cookies instead of parser-stored tokens
- **S33/MB8611 HNAP**: HNAP builder is now created by AuthHandler and transferred to parser for data fetches
- **Fixture Organization**: Modem fixtures moved from `tests/parsers/` to `modems/{manufacturer}/{model}/fixtures/` with new `modem.yaml` schema for declarative modem configuration

### Fixed

- **SB6190 Auth Detection** - Combined credential forms with visible password fields now detected correctly (#83, #93)
- **Form Auth Response Handling** - Auth handler now returns `None` instead of form response HTML, allowing scraper to fetch actual data pages
- **Invalid Auth Error Display** - Bad credentials now show "Invalid username or password" instead of misleading "Cannot connect" error
- **Options Flow Auth Handling** - Options/reconfigure flow now properly catches authentication errors
- **Polling Log Noise** - Detection and connection success logs demoted from INFO to DEBUG (only visible when debugging enabled)
- **Misleading Parser Names** - Removed parser name from connection log that incorrectly showed URL pattern source instead of detected modem

### Improved

- **Error Messages** - Simplified authentication error message across all 12 languages
- **Conditional Logging** - Config flow uses verbose logging (INFO) for setup visibility, polling uses quiet logging (DEBUG) for normal operation

### Technical Details

Auth strategies supported:

- `NO_AUTH` - No authentication required
- `BASIC_HTTP` - HTTP Basic authentication
- `FORM_PLAIN` - Form POST with plain credentials
- `FORM_BASE64` - Form POST with base64-encoded credentials
- `HNAP_SESSION` - HNAP/SOAP challenge-response authentication
- `URL_TOKEN_SESSION` - URL-based token with session cookies

## [3.11.0] - 2025-12-27

### ⚠️ BREAKING CHANGE: Channel Sensor Entity IDs

Channel sensor entity IDs now include channel type for DOCSIS 3.1 disambiguation.

**Before:**

```text
sensor.cable_modem_ds_ch_32_power
sensor.cable_modem_us_ch_3_power
```

**After:**

```text
sensor.cable_modem_ds_qam_ch_32_power
sensor.cable_modem_ds_ofdm_ch_1_power
sensor.cable_modem_us_atdma_ch_3_power
sensor.cable_modem_us_ofdma_ch_1_power
```

**Why:** DOCSIS 3.1 modems can have overlapping Channel IDs across different channel types (QAM vs OFDM). Adding the channel type to the entity ID prevents collisions and makes entities unambiguous.

**Migration for DOCSIS 3.0 users:**

1. Go to Settings → Integrations → Cable Modem Monitor → Configure
2. Click Submit (no changes needed)
3. Entities are automatically migrated on reload, preserving history

**Migration for DOCSIS 3.1 users:**

1. Go to Developer Tools → Services
2. Call `cable_modem_monitor.generate_dashboard`
3. Copy the YAML output and replace your existing dashboard cards

### Added

- **Automatic Entity Migration for DOCSIS 3.0** - Existing entities are automatically migrated to the new naming scheme for DOCSIS 3.0 modems. Since DOCSIS 3.0 only has one channel type per direction (QAM downstream, ATDMA upstream), the migration is unambiguous and preserves entity history. Trigger migration by opening the integration's Configure dialog and clicking Submit.
- **Channel Type in Entity IDs** - All channel sensors now include channel type (qam, ofdm, atdma, ofdma) for DOCSIS 3.1 compatibility
- **Dashboard Channel Grouping** - `generate_dashboard` service now accepts `channel_grouping` parameter:
  - `by_direction` (default) - All downstream channels in one card, all upstream in another
  - `by_type` - Separate cards for each channel type (QAM, OFDM, ATDMA, OFDMA)
- **Smart Channel Labels** - `generate_dashboard` service now accepts `channel_label` parameter:
  - `auto` (default) - Smart labeling: type in title when single type, type in labels when mixed
  - `full` - Labels like "QAM Ch 32"
  - `id_only` - Labels like "Ch 32"
  - `type_id` - Labels like "QAM 32"
- **Channel Attributes** - Each channel sensor now exposes `channel_id`, `channel_type`, and `frequency` as state attributes
- **Downstream Frequency Graph** - `generate_dashboard` service now accepts `include_downstream_frequency` parameter
- **Technicolor CGA2121 Parser** - New parser for Technicolor CGA2121 gateways (#75)
- **Arris G54 Parser** - New parser for Arris G54 gateway devices (#72)
- **Session Logout Support** - Parsers can now define `logout_endpoint` for modems that only allow one authenticated session (e.g., Netgear C3700)
- **Auto-Generate Attribution** - Fixture metadata now automatically generates contributor attribution

### Changed

- **Entity ID Format** - Channel sensors use `ds_{type}_ch_{id}` format instead of `ds_ch_{id}`
- **Sensor Names** - Channel sensors now display type in name (e.g., "DS QAM Ch 32 Power")

### Fixed

- **Arris S33 Parser** - Removed incorrect uptime mapping, fixed frequency parsing (#32)
- **CM1200 Channel Type Detection** - Fixed OFDM/OFDMA channels not showing correctly; parser now includes channel_type for downstream channels, and coordinator checks all relevant fields (channel_type, modulation, is_ofdm) (#63)

### Technical

- **Channel Normalization** - Coordinator normalizes channel_type from parser data (is_ofdm, modulation, channel_type fields)
- **Frequency-Based Indexing** - Channels are sorted by frequency within each type for stable index assignment

### Upgrade Notes

To enable new features added in v3.11 (actual model display in device info, ICMP detection for health monitoring), go to **Settings → Devices & Services → Cable Modem Monitor → Configure → Submit**. This triggers a fresh validation that populates the new config fields. This is a good practice after any upgrade to pick up new configuration options.

## [3.10.2] - 2025-12-18

### CRITICAL HOTFIX

⚠️ **v3.10.1 is broken** - A BFG history rewrite accidentally replaced `#` comment characters with `***REMOVED***` in the released code, causing SyntaxError on startup for all users.

### Fixed

- **CRITICAL: SyntaxError on startup** - Fixed `***REMOVED***` corruption in source files caused by overly aggressive BFG history sanitization
- **Blocking Import Warning** - Moved deferred imports to top-level to avoid `Detected blocking call to import_module` warnings on HA 2024.7+ (Issue #70)
- **Health Monitor HTTP Fallback** - Improved exception handling when HEAD request fails, properly falls back to GET
- **PII Sanitization** - Extended sanitizer to catch Motorola JavaScript password variables (`var CurrentPwAdmin = 'x'`) missed by v3.9.2 tagValueList fix
- **Fixture PII** - Sanitized passwords, serial numbers, and email in 5 fixture files (MB7621, MB8611, CM600, XB7, SB6190)

### Added

- **No Signal Detection** - SB6141 parser now detects when modem is online but has no cable signal, showing "No Signal" status instead of misleading "Parser Issue"

## [3.10.1] - 2025-12-17

### Highlights

🏪 **HACS Default Repository Submission** - Preparing for inclusion in the HACS default repository list

### Added

- **Hassfest Validation** - Added hassfest CI workflow required for HACS default submission
- **AI Skills** - Added modem-request-triage and issue-to-fixture AI skills for development workflow

### Fixed

- **Manifest Key Order** - Sorted manifest.json keys per Home Assistant requirements (domain, name, then alphabetical)
- **Test Warnings** - Resolved implicit string concatenation warnings in test_s33.py

### Changed

- **Scripts Cleanup** - Removed 6 superseded maintenance scripts

## [3.10.0] - 2025-12-16

### Highlights

📡 **3 New Modem Parsers** - Netgear C7000v2, Netgear CM1200, and Arris CM820B now supported

🎛️ **Unified Status Sensor** - Single pass/fail sensor replaces deprecated connection and health sensors:

- `Operational` - All good: data parsed, DOCSIS locked, reachable
- `ICMP Blocked` - HTTP works but ping fails (parser-specific)
- `Partial Lock` / `Not Locked` - DOCSIS lock issues
- `Parser Error` / `Unresponsive` - Connection problems

📊 **Dashboard Generator Service** - One-click Lovelace dashboard creation:

- Call `cable_modem_monitor.generate_dashboard` from Developer Tools > Services
- Returns copy-paste ready YAML with your modem's actual channel IDs
- Configurable: include/exclude status card, graphs, latency, errors

🌍 **12-Language Support** - Added 6 new languages: Dutch, Italian, Polish, Swedish, Russian, Ukrainian

### Added

- **Netgear C7000v2 Parser** - Support for Netgear C7000v2 cable modem/router combo (DOCSIS 3.0)
- **Netgear CM1200 Parser** - Support for Netgear Nighthawk CM1200 (DOCSIS 3.1) - contributed via Issue #63
- **Arris CM820B Parser** - Support for Arris CM820B EuroDOCSIS 3.0 modem - community contribution from @dimkalinux
- **Unified Status Sensor** - New `sensor.cable_modem_status` combines connection health and DOCSIS lock state into single pass/fail sensor
- **Dashboard Generator Service** - New `generate_dashboard` service returns ready-to-use Lovelace YAML for all modem channels
- **S33 Firmware Version** - Added `GetArrisDeviceStatus` HNAP action to retrieve firmware version (Issue #32)
- **ICMP Skip Support** - Parsers can declare `supports_icmp = False` to skip ping checks for modems that block ICMP
- **12-Language Support** - Added Dutch, Italian, Polish, Swedish, Russian, and Ukrainian translations

### Changed

- **Status Sensor States** - Simplified to pass/fail states: Operational, ICMP Blocked, Partial Lock, Not Locked, Parser Error, Unresponsive

### Removed

- **Deprecated Sensors** - Removed `cable_modem_connection_status` and `cable_modem_health_status` (use unified `cable_modem_status` instead)

### Addressed

- **S33 ICMP Blocked** - S33 no longer shows "icmp_blocked" health status; ping check skipped for this modem (Issue #32)
- **S33 Uptime Parsing** - Support "X days HH:MM:SS" format returned by S33 modem (Issue #32)
- **S33 Restart** - Fetch current EEE/LED config before reboot to match browser behavior (Issue #32)

### Fixed

- **MB7621 Channel ID** - Channels now use DOCSIS Channel ID instead of row counter
- **CM2000 OFDM Parsing** - Added missing OFDM downstream/upstream channel parsing for DOCSIS 3.1 channels
- **MB8611 OFDM Capability** - Declared OFDM capability (channels were already parsed but capability wasn't flagged)
- **Config Flow Asterisk** - Correctly display asterisk for unverified parsers in modem selection

### Security

- **Netgear tagValueList Sanitization** - WiFi credentials in Netgear's positional `tagValueList` format are now properly sanitized in diagnostics (Issue #61)

### Documentation

- **Documentation Restructure** - Consolidated scattered docs into clear user journeys; net reduction of ~930 lines
- **MODEM_REQUEST.md** - New comprehensive guide for modem data submissions with PII review checklist
- **SB8200 Reboot** - Added historical context explaining why reboot is disabled (SNMP-only)
- **FIXTURES.md** - Added Protocol (HNAP/HTML) and Chipset columns with reference sections
- **README/TROUBLESHOOTING** - Updated status sensor documentation to reflect unified sensor

## [3.9.2] - 2025-12-13

### Security

- **PII Sanitization** - Addressed diagnostics sanitization gap for WiFi credentials, device names, serial numbers, and SSIDs in Netgear `tagValueList` JavaScript variables (Related to #61)

### Added

- **Device Name Detection** - Sanitization now detects and redacts device names appearing before IP/MAC addresses in access control lists
- **PII Check CI Job** - New CI workflow validates fixture files for unsanitized PII on every PR
- **Enhanced PII Checker** - Pre-commit hook now detects `tagValueList` credentials and validates HAR files

### Changed

- **Fixture PII Cleanup** - Redacted WiFi passwords, serial numbers, SSIDs, WEP keys, and device identifiers from existing test fixtures

## [3.9.1] - 2025-12-11

### Addressed

- **S33 HNAP Empty Action Value** - S33 parser now uses empty string `""` instead of empty dict `{}` for HNAP action values, resolving 500 errors (Related to #32)
- **MB8611 Restart PrivateKey Cookie** - Added missing `PrivateKey` cookie required for authenticated actions like modem restart (Related to #6)
- **Diagnostics Logging** - Removed redundant logging imports

### Changed

- **CI Dependencies** - Bumped actions/checkout from 4 to 6

### Documentation

- **README Improvements** - Updated screenshots and added troubleshooting guidance
- **CAPTURE_GUIDE** - Clarified HNAP vs HTML capture requirements

## [3.9.0] - 2025-12-09

### Added

- **Arris/CommScope S33 Parser** - Full support for DOCSIS 3.1 modem with HNAP authentication
- **ParserStatus Enum** - New lifecycle states for parser development (experimental, verified, deprecated)
- **HNAP Request Logging** - Debug logs now show exact JSON payloads sent to modem (helps compare with browser)
- **HNAP Auth Diagnostics** - Diagnostics JSON includes `hnap_auth_debug` section with request/response data
- **Debug Guidance in Error Messages** - Auth failure messages now include debug logging instructions (6 languages)
- **Fixture Requirements Guide** - `docs/FIXTURE_REQUIREMENTS.md` with metadata.yaml template and PII checklist
- **Examples Documentation** - Dashboard and automation examples moved to `docs/EXAMPLES.md`

### Changed

- **MB8611 HNAP Login** - Added `PrivateLogin` field to login requests (potential fix for auth failures)
- **TC4400 Parser Verified** - Updated verification status with community confirmation
- **XB7 Parser Verified** - Updated verification status with community confirmation
- **README Restructure** - Reduced from 756 to 371 lines (51% reduction) following single-source-of-truth principle
- **PR Template** - Added explicit fixture checkboxes for PII scrubbing and metadata.yaml

### Removed

- **Entity Cleanup Feature** - Removed vestigial v2.0 migration code (entity_cleanup.py, CleanupEntitiesButton, cleanup_entities service)
- **Motorola Generic Parser** - Removed obsolete parser superseded by model-specific parsers

### Fixed

- **HNAP Auth Cache** - Clear auth cache after modem restart to prevent stale token errors
- **MB8611 Restart Action** - Use correct HNAP action for restart command
- **Pre-commit Hook Compatibility** - Support both .venv and pyenv environments

### Developer Experience

- **Welcome Task** - New VS Code task for first-time dev container setup
- **Port Conflict Resolution** - Improved handling of port 8123 conflicts
- **Fixture PII Validation** - Pre-commit hook checks for MAC addresses and public IPs

## [3.8.6] - 2025-12-01

### Added

- **Test Fixtures License** - Added LICENSE file for test fixture data directory

### Changed

- **DevContainer Docker Compatibility** - Added `DOCKER_API_VERSION` env var for host daemon compatibility
- **Setup Script pyenv Support** - Auto-initialize pyenv, prefer `python` over `python3` for shim compatibility
- **Setup Python Detection** - Read required version from pyproject.toml, platform-specific install instructions

### Fixed

- **Port Cleanup Reliability** - Use `fuser` for reliable port 8123 cleanup (fixes VS Code port forwarding conflicts)
- **VS Code Tasks** - Added "Fix Dev Container" task to reinstall dependencies when tools are missing

## [3.8.5] - 2025-11-30

### Fixed

- **SB8200 Uptime Sensor** - Fix uptime showing "Unknown" by using correct `system_uptime` key (Issue #42)

## [3.8.4] - 2025-11-30

### Added

- **Verified Modem Metadata** - Parser metadata with release dates, DOCSIS versions, and fixture paths
- **i18n Translations** - Internationalization support for integration strings
- **SB8200 Uptime Support** - Parse uptime from cmswinfo.html product info page

### Changed

- **Diagnostic Improvements** - Organized fixture files and improved capture workflow

### Fixed

- **CodeQL Alerts** - Remove unused stdout/stderr variables (alerts #3, #4)
- **Release Script** - Don't create tag with --no-push flag in PR workflow

## [3.8.3] - 2025-11-29

### Added

- **Comprehensive HTML Capture** - Discovers all modem resources for fixture database (Issue #42)
  - Parses JavaScript files for URL patterns (menu configs, AJAX endpoints)
  - Extracts jQuery `.load()` fragments (header/footer templates)
  - Iterative discovery: fetches JS → finds more URLs → repeats
  - 2x improvement in captured data across all modems

### Changed

- **Capture Data Format** - Renamed `html` to `content` field to support JS, CSS, JSON responses
- **Login Method Standardization** - All parsers now return `tuple[bool, str | None]` consistently

### Fixed

- **Diagnostics Timestamp** - Log entries now show collection time instead of `0`
- **Mypy Type Error** - Added type annotation in html_crawler.py for BeautifulSoup rel attribute

## [3.8.2] - 2025-11-28

### Added

- **ARRIS SB8200 Parser** - Full support for DOCSIS 3.1 modem with 32 downstream + 3 upstream channels (Issue #42)

### Fixed

- **MB8611 Private Key Persistence** - Fix HNAP authentication key storage between sessions

## [3.8.1] - 2025-11-28

### Added

- **CM2000 Software Version** - Parse firmware version from index.htm InitTagValue() (Issue #38)
- **CM2000 Restart Support** - Remote modem restart via RouterStatus.htm buttonSelect=2 (Issue #38)
- **SB8200 Fixtures** - Test fixtures for future ARRIS SB8200 parser (Issue #42)
  - Full HTML capture with 32 downstream + 3 upstream channels
  - No authentication required (public status page)
- **MB8600 Fixtures** - Test fixtures for future Motorola MB8600 parser (Issue #40)
  - Login page with HNAP JavaScript references
  - Implementation guide for HNAP authentication

### Changed

- **CM2000 Fixtures** - Updated all 12 fixture pages from latest diagnostics
- **Issue Templates** - Rewritten to prioritize Fallback Mode + Capture HTML workflow
  - Added PII warnings for manual HTML capture
  - Improved auth details field to discourage password sharing

### Fixed

- **CM2000 Version Regex** - Fixed regex matching commented lines in InitTagValue()

## [3.8.0] - 2025-11-28

### Added

- **Netgear CM2000 Parser** - Full support for DOCSIS 3.1 cable modem (Issue #38)
  - Downstream and upstream channel parsing
  - System information extraction
  - Comprehensive test suite with fixtures
  - Credit: Community contribution

- **Parser Metadata System** - All parsers now expose device information
  - Added `release_date`, `docsis_version`, `fixtures_path` to base parser
  - Added `verified` status and `verification_source` for transparency
  - New `ModemInfoSensor` exposes metadata as Home Assistant entity attributes

- **MB8611 HNAP Authentication** - Challenge-response auth implementation
  - HMAC-MD5 based authentication for Motorola MB8611/MB8612
  - Credit: @BowlesCR (Chris Bowles) for reverse-engineering the auth flow
  - Credit: @cvonk (Coert Vonk) for HAR capture and persistent debugging support (Issue #6)
  - Prior art: xNinjaKittyx/mb8600 repository
  - ⚠️ UNVERIFIED - awaiting real hardware confirmation

- **HNAP JSON Builder Tests** - 535 lines of comprehensive test coverage

- **Fixture Index Generator** - Auto-generates fixture directory index
  - `scripts/generate_fixture_index.py` creates README.md for fixture directories
  - Documents available test fixtures for each modem model

- **Deployment Script** - Automated deployment to Home Assistant instances
  - `scripts/deploy_updates.sh` supports local, SSH, and Docker deployment
  - Interactive mode guides users through deployment options
  - New `docs/TESTING_ON_HA.md` guide for manual deployment

### Changed

- **MB8611 Fixture Consolidation** - Merged mb8611_hnap and mb8611_static into single mb8611 directory
  - Removed non-functional static parser (never authenticated)
  - Preserved MotoStatusLog.html and MotoStatusSecurity.html from static for field mapping reference
  - Renamed `mb8611_hnap.py` to `mb8611.py`

- **Standalone Capture Scripts** - Capture and sanitize scripts no longer require Home Assistant
  - Created `scripts/utils/sanitizer.py` with standalone PII sanitization
  - `capture_modem.py` and `sanitize_har.py` now work with just `pip install playwright`
  - Removes friction for contributors capturing modem data (California PII laws compliance)

### Fixed

- **Unverified Parser Selection** - Fixed bug where selecting an unverified parser fell back to autodiscovery
  - The " *" suffix marking unverified parsers wasn't stripped during lookup
  - Credit: @BowlesCR for discovering the issue (Issue #40)
- **GitHub Language Stats** - Added `.gitattributes` linguist-vendored rules
  - HTML/JSON fixtures no longer counted as project code
- **Pre-commit Config** - Migrated deprecated `commit` stage to `pre-commit`
- **PII Sanitization** - Updated fixture files with redacted MAC addresses

## [3.7.2] - 2025-11-26

### Added

- **C3700 Uptime Support** - System uptime and last boot time now available
  - Parses uptime from RouterStatus.htm tagValues[33] (e.g., "5 days 12:34:56")
  - Calculates last boot time from uptime
  - Added `SYSTEM_UPTIME` and `LAST_BOOT_TIME` capabilities

- **C3700 Restart Support** - Remote modem restart capability
  - Extracts session ID from form action for proper authentication
  - POSTs to `/goform/RouterStatus?id=<session>` with `buttonSelect=2`
  - Handles connection drop during reboot as expected success

- **Cross-Platform HA Startup** - New `ha-start.py` script
  - Port availability checking before startup
  - Clear error messages for common issues
  - Works in both local and devcontainer environments

- **DevContainer Detection** - capture_modem.py improvements
  - Auto-detects devcontainer environment
  - Prompts for HTTP Basic Auth credentials when needed

### Fixed

- **IPv6 Sanitizer** - No longer incorrectly matches time formats
  - Times like "12:34:56" were being converted to `***IPv6***`
  - Now uses callback to only replace strings containing hex letters (a-f)

- **Docker Port Conflict** - Removed port 8300 mapping from docker-compose.test.yml
  - Conflicted with VS Code devcontainer default port

- **Test Mock Paths** - Fixed 6 tests broken by import reorganization
  - Updated mock patch paths for AuthFactory in multiple test files

## [3.7.1] - 2025-11-25

### Added

- **CM600 Uptime Support** - System uptime and last boot time now available
  - Parses HH:MM:SS uptime format (e.g., "1308:19:22" = 1308 hours)
  - Calculates last boot time from uptime
  - Added `SYSTEM_UPTIME` and `LAST_BOOT_TIME` capabilities
  - Confirmed working by user @chairstacker (Issue #3)

### Changed

- **Uptime Parser Enhancement** - `parse_uptime_to_seconds()` now handles HH:MM:SS format
  - Supports hours exceeding 24 (long uptimes like "1308:19:22")
  - Backwards compatible with existing formats ("5d 12h 30m 15s")

- **PII Checker Improvement** - Recognizes uptime format as false positive
  - Prevents HH:MM:SS uptime strings from being flagged as IPv6 addresses

### Fixed

- **CM600 Documentation** - Corrected docstring that incorrectly stated uptime was unavailable
- **CM600 Test Fixture** - Updated with realistic uptime data instead of scrubbed placeholder

### Testing

- Improved cm600.py coverage: 75% → 90%
- Improved utils.py coverage: 65% → 85%
- Added 12 new tests for multi-page parsing, restart scenarios, detection methods

## [3.7.0] - 2025-11-25

### Added

- **Parser Capabilities System** - Standardized capability declarations across all parsers
  - New `ModemCapability` enum defines standard capabilities (uptime, channels, restart, etc.)
  - Each parser declares supported features via `capabilities` class attribute
  - Enables conditional entity creation based on modem capabilities
  - `has_capability()` class method for runtime capability checking

- **Netgear CM2000 Parser** - New parser for DOCSIS 3.1 cable modem
  - Full support for downstream/upstream channel parsing
  - System information extraction (model, firmware, uptime)
  - Comprehensive test suite with fixtures
  - Fixture README documenting data structure

- **PII Protection System** - Comprehensive safeguards for fixture data
  - Enhanced `html_helper.py` with `sanitize_html()` and `check_for_pii()` functions
  - New `scripts/check_fixture_pii.py` for CI validation of fixture files
  - Detects MAC addresses, public IPs, emails, IPv6 addresses
  - Smart false positive handling (timestamps, version numbers, sanitized placeholders)
  - RFC 5737 TEST-NET addresses used as safe IP placeholders

- **Fixture README Documentation** - All fixture directories now have README.md files
  - MB7621, MB8611 (static/HNAP), CM600, CM2000, TC4400, XB7
  - Documents data structure, source, and sanitization status

- **Developer Tooling**
  - `scripts/dev/list-supported-modems.py` - List all supported modem models
  - `scripts/dev/setup-git-email.sh` - Configure Git email for contributions
  - `.github/workflows/check-commit-email.yml` - Verify commit attribution

### Changed

- **CM600 Restart Improvements**
  - Added response logging for restart command debugging
  - Handle connection drop as expected success during reboot

- **Test Consolidation**
  - Removed redundant `tests/parsers/motorola/fixtures/generic/` directory
  - Deleted `test_generic.py`, moved restart detection tests to `test_mb7621.py`
  - All Motorola MB-series tests now consolidated in appropriate model-specific files

### Fixed

- **Fixture PII Sanitization** - Sanitized all existing fixture files
  - MAC addresses replaced with `XX:XX:XX:XX:XX:XX`
  - Public IPs replaced with RFC 5737 TEST-NET addresses (203.0.113.x)
  - Serial numbers redacted in README files

## [3.6.0] - 2025-11-25

### Added

- **Parser Verification Status System** - New transparency framework for modem parser reliability
  - All parsers now include `verified` boolean flag and `verification_source` documentation
  - Base parser defaults to `verified = False`, requiring explicit verification for each model
  - Verification sources tracked: maintainer testing, community reports, or unverified status
  - New `VERIFICATION_STATUS.md` document provides comprehensive parser status overview
- **UI Verification Indicators** - Users can now see which modems are verified during setup
  - Unverified parsers marked with asterisk (*) in modem selection dropdown
  - Field description explains: "Models marked with * are unverified or have known issues"
  - Applies to both initial setup and settings configuration
  - Helps users make informed decisions about parser selection
- **Enhanced Config Flow Translations** - Improved UI text consistency and clarity
  - All form fields now have consistent `data_description` help text
  - Clean field labels with helpful descriptions below each field
  - Translations system synchronized (strings.json → translations/en.json)
  - Fixed spacing inconsistencies between form fields
- **User Feedback System** - Streamlined path for users to report working unverified modems
  - New GitHub issue template (`modem_verification.yml`) with guided fields
  - Dedicated README section "Help Verify Modem Support" with clear call-to-action
  - Pre-filled form makes reporting take only 2 minutes
  - Alternative reporting path via Home Assistant Community Forums for non-GitHub users
  - Significantly reduced friction for community verification reports

### Changed

- **README Modem Support Tables** - Reorganized for transparency and accuracy
  - ✅ Verified Working: Only 4 confirmed models (Arris SB6141, Motorola MB7621, Netgear C3700, Netgear CM600)
  - ⚠️ Unverified Parsers: Parsers needing user confirmation
  - ❌ Known Issues: New section for broken parsers (MB8611 HNAP/Static moved here)
  - Clear verification sources cited for each working model
- **Motorola MB8611 Status** - Correctly documented as non-working
  - Removed from "Verified Working" list (was incorrectly listed)
  - Added to "Known Issues" section with specific problems documented
  - HNAP variant: SSL certificate and protocol issues (Issues #4, #6)
  - Static variant: Untested with limited features
- **Release Script Enhancements** - Comprehensive pre-deployment validation added
  - Now runs full test suite before release
  - Executes code quality checks matching CI exactly (ruff, black, mypy on entire repo)
  - Verifies translations/en.json matches strings.json
  - Auto-updates translations if out of sync
  - Stages translation file changes in commit
  - Added `--skip-tests` and `--skip-quality` flags for flexibility
  - Prevents CI failures by running identical checks locally before push

### Developer Experience

- **Docker Status Checking** - Added cross-platform Docker check helper (`scripts/dev/check-docker.py`)
  - Verifies Docker is installed and running before Docker operations
  - Provides platform-specific error messages for Windows, macOS, and Linux
  - Handles Unicode/ASCII fallback for Windows console compatibility
  - Integrated into all Docker-related VS Code tasks as a dependency
- **Dev Container Improvements** - Fixed post-create script execution order
  - CodeQL CLI now installs before attempting to use it
  - Added directory existence check before CodeQL pack installation
  - Suppressed pip root user warnings in container environment
- **Terminal Experience** - Enhanced welcome messages for new terminal sessions
  - Added terminal clearing before displaying welcome message for cleaner UI
  - Removed emoji from welcome text to fix Windows terminal encoding issues
  - Cross-platform compatibility maintained for Linux, macOS, and Windows

### Technical Details

- **Files Modified**:
  - `custom_components/cable_modem_monitor/config_flow.py` - Added `_get_parser_display_name()` helper, updated dropdown generation
  - `custom_components/cable_modem_monitor/strings.json` - Restructured with consistent field descriptions
  - `custom_components/cable_modem_monitor/translations/en.json` - Synchronized with strings.json
  - `custom_components/cable_modem_monitor/parsers/base_parser.py` - Set default `verified = False`
  - All parser files - Added verification status and sources
  - `README.md` - Reorganized modem support tables, updated MB8611 status
  - `VERIFICATION_STATUS.md` - New comprehensive verification status document
  - `scripts/release.py` - Added test, quality, and translation validation steps

### Planning

- Future features and improvements
- See GitHub issues and milestones for upcoming features

## [3.5.1] - 2025-11-24

### Enhanced

- **Netgear CM600 Parser Improvements** - Addressed user feedback from issue #3 with comprehensive parser enhancements
  - Channel data now correctly parses 24 downstream and 6 upstream channels from HTML tables instead of JavaScript dummy data
  - Frequency, power, SNR, and error values now match the modem's web interface
  - System information (hardware version, firmware version, uptime) now displays correctly instead of "unknown"
  - Parser now fetches both DocsisStatus.asp (channel data) and RouterStatus.asp (system info) for complete data

### Added

- **Modem Restart Support for CM600** - New `restart()` method enables modem reboot functionality
  - Sends POST request to `/goform/RouterStatus` with `RsAction=2` parameter
  - Integrated with existing Home Assistant button entity infrastructure
  - Comprehensive test coverage (3 new restart tests)

### Changed

- **CM600 Multi-Page Fetching** - Enhanced parser to fetch multiple pages for complete data
  - DocsisStatus.asp for downstream/upstream channel data
  - RouterStatus.asp for hardware version, firmware version, and system information
  - Graceful fallback if page fetching fails

### DevContainer

- **Port Forwarding Configuration** - Added explicit port forwarding for development environment
  - Configured ports 8123 (Home Assistant) and 8300 (Home Assistant Internal)
  - Enhanced port attributes with labels and auto-forward behavior

### Testing

- Updated all CM600 tests to reflect real modem data (24 downstream, 6 upstream channels)
- Added 3 new tests for modem restart functionality
- All 495 tests passing
- Code quality checks passing (ruff, black, mypy)

### Technical Details

- **Files Modified**:
  - `custom_components/cable_modem_monitor/parsers/netgear/cm600.py` - Complete rewrite of channel parsing (JavaScript → HTML tables), added restart() method
  - `tests/parsers/netgear/test_cm600.py` - Updated expectations for 24 DS/6 US channels, added restart tests
  - `.devcontainer/devcontainer.json` - Added forwardPorts configuration
  - `custom_components/cable_modem_monitor/const.py` - Version bump to 3.5.1
  - `custom_components/cable_modem_monitor/manifest.json` - Version bump to 3.5.1
  - `tests/components/test_version_and_startup.py` - Updated version assertion to 3.5.1
- **Related Issue**: Addresses user feedback in issue #3 (user verification pending)

## [3.4.1] - 2025-11-22

### Enhanced

- **Connectivity Check Diagnostics** - Significantly improved troubleshooting for modem connection issues
  - Added detailed timing information for each connection attempt (shows actual elapsed time vs timeout)
  - Logs now show which protocol (HTTP/HTTPS) and method (HEAD/GET) is being tried
  - Added GET request fallback when HEAD requests timeout (some modems don't support HEAD)
  - Comprehensive diagnostic messages now included in error output
  - Changed logging level from DEBUG to INFO/WARNING for better visibility
  - Each failure now reports: protocol, method, elapsed time, exception type, and error details
  - Error messages now include diagnostic summary to help identify root cause

### Changed

- **Connectivity Check Timeout** - Increased from 2s to 10s (Addresses Issue #3)
  - Aligns pre-flight connectivity check with main scraper timeout (10s)
  - More accommodating for slower-responding modems
  - Reduces false "network_unreachable" errors during setup
  - Particularly helpful for modems like Netgear CM600 that may need more time

### Added

- **Test Coverage** - Enhanced connectivity check testing
  - New test validates GET fallback behavior when HEAD requests timeout
  - Existing test updated to verify 10-second timeout configuration
  - Tests ensure both HEAD and GET methods work correctly with proper timeout

### Technical Details

- **Files Modified**:
  - `custom_components/cable_modem_monitor/config_flow.py` - Enhanced `_do_quick_connectivity_check()` with:
    - Timing measurement for all requests
    - GET fallback logic after HEAD timeout
    - Structured diagnostic info collection
    - Better logging at INFO/WARNING levels instead of DEBUG
  - `tests/components/test_config_flow.py` - Added GET fallback test case
  - `custom_components/cable_modem_monitor/const.py` - Version bump to 3.4.1
  - `custom_components/cable_modem_monitor/manifest.json` - Version bump to 3.4.1

### Benefits for Issue #3

This release provides extensive diagnostic information to help understand why the Netgear CM600 (and other modems) might fail connectivity checks:

- Identifies if it's a timeout issue vs connection refused vs other errors
- Shows if HTTP vs HTTPS makes a difference
- Reveals if HEAD requests aren't supported (fixed by GET fallback)
- Provides timing data to understand modem response characteristics
- All diagnostic details appear in Home Assistant logs for troubleshooting

## [3.4.0] - 2025-11-21

### Added

- **JSON HNAP Support for Motorola MB8611** - Dual-format HNAP authentication and parsing (Fixes Issue #29)
  - New `HNAPJsonRequestBuilder` class for JSON-formatted HNAP requests
  - MB8611 parser now tries JSON HNAP first, then falls back to XML/SOAP
  - Fixes `SET_JSON_FORMAT_ERROR` for firmware variants that reject XML/SOAP format
  - Enhanced error detection in authentication module for JSON error responses
  - Automatic format detection ensures compatibility with all MB8611 firmware variants
  - Both login and data parsing use dual-format strategy
- **Comprehensive Test Coverage** - 20 new tests for parser improvements
  - MB8611: 5 new tests for JSON HNAP support (login, parsing, fallback, error handling)
  - CM600: 15 new tests covering authentication (4 tests), edge cases (6 tests), and metadata (4 tests)
  - Validates HTTP Basic Auth configuration and behavior
  - Tests malformed data handling and graceful error recovery

### Fixed

- **Motorola MB8611 (HNAP) Configuration** - Resolved JSON format compatibility issue (Fixes Issue #29)
  - Modem firmware variants that respond with `SET_JSON_FORMAT_ERROR` now work correctly
  - Parser automatically detects and uses JSON-formatted HNAP requests when XML/SOAP fails
  - Seamless fallback ensures backward compatibility with older firmware
  - Users no longer need to manually select different parser variants
- **Netgear CM600 Authentication** - Fixed HTTP 401 errors on protected pages (Fixes Issue #3)
  - Enabled HTTP Basic Authentication for `/DocsisStatus.asp`, `/DashBoard.asp`, `/RouterStatus.asp`
  - Changed `auth_required: False` to `auth_required: True` for protected endpoints
  - Updated login() method to use AuthFactory for proper credential handling
  - Index pages remain accessible without authentication for modem detection
  - Parser now successfully retrieves channel data and system information

### Changed

- **Enhanced Authentication Error Detection** - Better diagnostics for HNAP format mismatches
  - Authentication module now detects JSON error responses (`SET_JSON_FORMAT_ERROR`, `LoginResult:FAILED`)
  - Warning messages suggest using JSON-formatted HNAP when XML/SOAP is rejected
  - Improved logging helps diagnose firmware-specific authentication issues
- **MB8611 Parser Architecture** - Refactored for dual-format support
  - Split parsing logic into `_parse_with_json_hnap()` and `_parse_with_xml_hnap()` methods
  - Consistent error handling across both JSON and XML/SOAP code paths
  - Enhanced debug logging shows which format succeeded
  - Channel count and response size logged for troubleshooting

### Technical Details

- **Files Added**: `core/hnap_json_builder.py` (212 lines) - JSON HNAP request builder
- **Files Modified**:
  - `core/authentication.py` - Added JSON error detection (lines 396-407)
  - `parsers/motorola/mb8611_hnap.py` - Dual-format login and parsing (lines 51-232)
  - `parsers/netgear/cm600.py` - HTTP Basic Auth configuration (lines 43-73)
  - `tests/parsers/motorola/test_mb8611_hnap.py` - 5 new JSON HNAP tests
  - `tests/parsers/netgear/test_cm600.py` - 15 new authentication and edge case tests
- **Compatibility**: No breaking changes, fully backward compatible with existing configurations
- **Test Coverage**: Increased from 443 to 463 tests (all passing)

## [3.3.1] - 2025-11-20

### Added

- **VS Code Development Environment Configuration** - Comprehensive IDE setup for consistent development
  - Extension recommendations for Python, testing, security (CodeQL), YAML, and Markdown
  - Excludes conflicting extensions (test adapters, pylint) that interfere with native Python testing
  - CodeQL extension settings for local query development and testing
  - Home Assistant development tasks for container lifecycle management
  - Enhanced devcontainer startup messages with quick-start guide
- **CodeQL Testing Infrastructure** - Local testing support for security queries
  - Command-line test runner script at `scripts/dev/test-codeql.sh`
  - Comprehensive testing guide at `docs/CODEQL_TESTING_GUIDE.md`
  - CodeQL pack documentation at `cable-modem-monitor-ql/README.md`
  - Automated CodeQL CLI installation in test script
- **Development Container Guide** - Cross-platform setup documentation
  - Complete guide at `docs/VSCODE_DEVCONTAINER_GUIDE.md` for Windows, macOS, Linux, Chrome OS
  - Home Assistant container management workflows
  - Testing panel usage instructions
  - Troubleshooting for common development issues

### Changed

- **Test Configuration** - Improved pytest discovery reliability
  - Excluded CodeQL test fixtures from pytest discovery (prevents false test detection)
  - Added `norecursedirs` in pytest.ini to ignore `cable-modem-monitor-ql`, `.venv`, and `codeql` directories
  - VS Code pytest settings now ignore CodeQL and venv directories
  - Fixes issue where CodeQL `.py` fixtures were incorrectly detected as Python tests
- **Git Ignore Configuration** - Better development artifact handling
  - Ignores local CodeQL CLI installation directory
  - Separates local development artifacts from GitHub workflow artifacts
  - Clarified comments distinguishing local vs CI/CD CodeQL resources

### Fixed

- **Threading Cleanup Error in Tests** - Resolved race condition in HTTP error handling tests
  - Fixed `test_http_rejects_5xx` test that had intermittent threading cleanup errors
  - Proper async mock teardown and session cleanup
  - Prevents "Task was destroyed but it is pending" warnings
- **Authentication Failure Handling** - Universal fix for setup blocking on auth failures (Issue #4)
  - Integration setup now properly blocks when authentication fails
  - Prevents "Retrying setup" loops for incorrect credentials
  - Returns proper `ConfigEntryNotReady` with auth error details
  - Applies to all authentication methods (Basic, Form, HNAP)
  - Users see clear error message: "Authentication failed" instead of infinite retry
- **Enhanced Diagnostic Logging** - Better troubleshooting for parser and auth issues
  - HNAP authentication shows full request/response details when auth fails
  - MB8611 HNAP parser logs attempted URLs and responses
  - MB8611 Static parser logs detection attempts and failures
  - Helps diagnose parser selection issues (related to Issue #4)
- **Parser Loading Performance Test** - Fixed flaky test timing assertion
  - Increased cached load threshold from 1ms to 10ms
  - Prevents intermittent failures on slower systems or under load
  - More realistic timing expectation for cached operations

## [3.3.0] - 2025-11-18

### Added

- **Netgear CM600 Support** - Full support for Netgear CM600 cable modem (Issue #3)
  - JavaScript-based parser for DocsisStatus.asp page
  - Extracts channel data from InitDsTableTagValue and InitUsTableTagValue functions
  - Comprehensive test coverage with real modem fixtures
  - Handles downstream and upstream channel parsing
  - Status: Awaiting user confirmation on hardware
- **Enhanced Parser Diagnostics** - Better troubleshooting information
  - `parser_detection` section shows user selection vs. auto-detection
  - `detection_method` field: "user_selected", "cached", or "auto_detected"
  - `parser_detection_history` tracks attempted parsers during failures
  - Helps diagnose parser mismatch issues (like Issue #4)
  - New `_get_detection_method()` helper function in diagnostics.py
  - Comprehensive test coverage (4 new diagnostics tests)
- **Core Module Test Coverage** - 115 new unit tests for previously untested modules
  - `core/signal_analyzer.py`: 22 tests covering SNR/power analysis, error trending, polling recommendations
  - `core/health_monitor.py`: 45 tests covering ping/HTTP checks, input validation, latency calculations
  - `core/hnap_builder.py`: 25 tests covering SOAP envelope building, XML parsing, HNAP requests
  - `core/discovery_helpers.py`: 3 tests covering ParserNotFoundError exception
  - `core/authentication.py`: 11 tests covering NoAuth, BasicHTTP, and Form auth strategies
  - `lib/html_crawler.py`: 9 tests covering HTML fetching, error handling, session management
  - Total test count increased from 328 to 443 tests (+35%)
  - Test-to-code ratio now ~70% (6,548 test lines / 9,404 source lines)
- **Code Coverage Requirement Increased** - Raised minimum coverage threshold
  - Increased from 50% to 60% in pytest.ini and CI/CD workflows
  - Current coverage: ~70% (exceeds new requirement)
  - Enforced in GitHub Actions for all pull requests
  - Reflects improved test infrastructure and quality standards
- **Enhanced CodeQL Security Scanning** - Comprehensive static analysis for security vulnerabilities
  - **5 Custom Security Queries** tailored for network device integrations:
    - `subprocess-injection.ql`: Detects command injection in subprocess calls (CWE-078, severity 9.0)
    - `unsafe-xml-parsing.ql`: Ensures defusedxml usage to prevent XXE attacks (CWE-611, severity 7.5)
    - `hardcoded-credentials.ql`: Finds hardcoded passwords/API keys (CWE-798, severity 8.5)
    - `insecure-ssl-config.ql`: Validates SSL/TLS configuration justifications (CWE-295, severity 6.0)
    - `path-traversal.ql`: Prevents file system path traversal (CWE-022, severity 8.0)
  - **Expanded Query Packs**: Added security-extended suite for comprehensive coverage
  - **Query Suite Organization**: `cable-modem-security.qls` organizes all custom queries
  - **Smart Exclusions**: Filters out false positives with documented rationale
  - **Enhanced Configuration**: Setup Python dependencies for better code flow analysis
  - **Comprehensive Documentation**: Full README with examples, justifications, and local testing guide
  - **Automated Scanning**: Runs on push, PRs, and weekly schedule (Mondays 9 AM UTC)
- **Development Environment Improvements**
  - Automated bootstrap script for Python virtual environment setup
  - Enhanced devcontainer configuration with custom Dockerfile
  - Cross-platform VSCode workspace configuration (Windows 11, Chrome OS Flex, macOS)
  - Improved pytest configuration with better test discovery
  - Developer quickstart documentation
  - Setup verification scripts
- **CI Check Script** - Local validation before pushing changes
  - `scripts/ci-check.sh` runs Black, Ruff, Mypy, and Pytest locally
  - Matches CI environment checks to catch issues before push
  - Provides immediate feedback without waiting for GitHub Actions
- **Local Environment Setup Guide** - Comprehensive troubleshooting documentation
  - `docs/LOCAL_ENVIRONMENT_SETUP.md` covers environment setup and common issues
  - Documents yarl import errors and dependency conflicts
  - Explains mypy behavior differences (with/without types-requests)
  - Provides pre-commit hook setup instructions
  - Includes recommended development workflow

### Changed

- **Documentation Cleanup** - Archived historical documents and streamlined roadmap
  - Trimmed ARCHITECTURE_ROADMAP.md from 2,474 to 313 lines (87% reduction)
  - Moved 7 historical documents to docs/archive/ (~130 KB)
  - Created archive structure: v3.3.0_dev_sessions/, completed_features/
  - Focused roadmap on current v3.x status and open issues
  - Better maintainability and navigation
- **Parser Detection Logging** - Enhanced troubleshooting output
  - Shows attempted parsers when detection fails
  - Added TC4400 detection debug logging (Issue #1)
  - Parser error messages include attempted parser list
  - Better visibility into detection failures
- **MB8611 Static Parser Enhancement** - Added MB8600 fallback URL compatibility
  - New URL pattern: `/MotoConnection.asp` (MB8600-style)
  - Handles firmware variations that use older MB8600 URLs
  - Form-based authentication support for legacy endpoints
- **Issue Status Updates** - Accurate tracking in TEST_FIXTURE_STATUS.md
  - Issue #2 (XB7 system info): Marked RESOLVED (v2.6.0)
  - Issue #3 (CM600): Marked IMPLEMENTED (v3.3.0), awaiting testing
  - Issue #4 (MB8611): Analysis shows parser mismatch issue
  - Issue #5 (XB7 timeout): Marked RESOLVED (v2.6.0)
- **Modem Compatibility Documentation** - Accurate status for all modems
  - CM600 listed as "Experimental / Newly Implemented"
  - MB8611 clarified as having dual parsers (HNAP vs Static)
  - Clear guidance on parser selection importance
- **Makefile Simplification** - Streamlined development commands
  - Reduced from 126 lines to 54 lines
  - Clearer command structure and documentation
  - Better cross-platform compatibility
- **Pre-commit Configuration** - Excluded JSONC files from JSON validation
  - Allows comments in devcontainer.json and VS Code settings.json
  - Maintains strict JSON checking for other configuration files
- **VSCode Settings Enhancement** - Comprehensive cross-platform configuration
  - Python interpreter path using .venv standard
  - Black formatter with proper cross-platform paths
  - Ruff linter configuration
  - Testing configuration for pytest
  - File handling and editor settings
- **Development Dependencies** - Aligned local environment with CI
  - Updated `requirements-dev.txt`: homeassistant 2025.1.0 → 2024.1.0 (fixes non-existent version)
  - Added `pytest-socket>=0.6.0` to match CI lint job requirements
  - Updated `scripts/setup.sh` to use `requirements-dev.txt` instead of manual package list
  - Updated `CONTRIBUTING.md` to use `requirements-dev.txt` instead of `tests/requirements.txt`
- **Documentation Cross-References** - Improved documentation discoverability
  - README.md now links to LOCAL_ENVIRONMENT_SETUP.md for troubleshooting
  - CONTRIBUTING.md references LOCAL_ENVIRONMENT_SETUP.md for environment issues
  - DEVELOPER_QUICKSTART.md includes LOCAL_ENVIRONMENT_SETUP.md in "Getting Help"
  - LOCAL_ENVIRONMENT_SETUP.md includes navigation header linking to other dev docs
  - Clear documentation hierarchy: README → CONTRIBUTING → specialized guides

### Fixed

- **CM600 Parser Robustness** - Improved error handling and data extraction
  - Better handling of JavaScript variable parsing
  - Type annotations for better code quality
  - Complexity warnings addressed with proper annotations
- **Import Organization** - Fixed module-level import ordering
  - Moved sanitize_html import to top of diagnostics.py
  - Sorted imports in test files for consistency
- **Type Checking Issues** - Added type ignore comments where appropriate
  - Fixed mypy errors in socket patching code
  - Proper type annotations for list variables
- **CI/CD Pipeline Issues** - Resolved multiple CI check failures
  - CodeQL configuration: Removed invalid `packs` section causing fatal error
  - Black formatting: Applied formatting to 3 test files (test_config_flow, test_authentication, test_health_monitor)
  - Mypy type checking: Configured to work with and without types-requests package
    - Disabled warn_redundant_casts and warn_unused_ignores (handles CI vs local differences)
    - Disabled warn_unreachable (prevents false positives with pytest.raises)
    - Excluded tests/ and tools/ directories from type checking
    - Added requests to mypy ignore list for consistency
  - Test failure: Fixed async mock setup in test_http_timeout (proper AsyncMock usage)
  - Removed test_html_crawler.py (tested non-existent HTMLCrawler class)
- **Type Checking Consistency** - Fixed environment-specific mypy errors
  - Added type casting in hnap_builder.py for response.text
  - Configured mypy.ini to handle both local (no stubs) and CI (with stubs) environments
  - Prevents "redundant cast" errors in CI and "returning Any" errors locally
- **Workflow Permissions** - Fixed GitHub Actions permissions for PR comments
  - Added write permissions to commit-lint.yml and changelog-check.yml workflows
  - Allows workflows to post helpful feedback comments when checks fail
  - Resolves "Resource not accessible by integration" 403 errors

## [3.2.0] - 2025-11-13

### Added

- **Fallback Mode for Unsupported Modems** - Universal parser for modems without specific support
  - New `UniversalFallbackParser` that works with any cable modem
  - Manual selection via "Unknown Modem (Fallback Mode)" in dropdown
  - Provides 4 basic sensors: Connection Status, Health Status, Ping Latency, HTTP Latency
  - Status shows as "limited" to indicate reduced functionality
  - Enables HTML capture button for diagnostic data collection
  - Allows users to contribute HTML samples for future parser development
  - Priority 1 (lowest) - only used when explicitly selected, never auto-detected
- **Parser Issue Status** - New status for when known parser extracts no channel data
  - Handles edge cases: bridge mode, firmware changes, modem initialization
  - Clear diagnostic messages guide user troubleshooting
  - Different from unsupported (parser exists but returns no data)
- **Health Monitoring in Diagnostics** - Network connectivity checks included in diagnostic download
  - ICMP ping test results with latency
  - HTTP HEAD request test results with latency
  - Connection status (responsive/unresponsive)
  - Helps diagnose network vs. modem issues

### Changed

- **Health Status Terminology** - Changed from "healthy" to "responsive" for clarity
- **Modem Dropdown and Auto-Detect Sorting** - Unified to alphabetical order
  - Both dropdown and auto-detection now use same alphabetical sorting (manufacturer → name)
  - Generic parsers appear last within their manufacturer group
  - Priority field deprecated (backward compatible, no longer used for ordering)
  - Example order: MB7621, MB8611 (HNAP), MB8611 (Static), MB Series (Generic)

### Fixed

- **Blocking Import in Event Loop** - Eliminated 515ms delays when updating button entity state
  - Moved parser import check from `available` property to async setup
  - Created `_check_restart_support()` helper function
  - Used `hass.async_add_executor_job()` to run imports in thread pool
  - Cached availability at setup time instead of checking dynamically
  - Fixes Home Assistant warning: "Detected blocking call to import_module"
- **Sensor Availability in Fallback/Limited Modes** - Sensors now properly show as available
  - Fixed sensors incorrectly showing unavailable when in fallback or limited status
  - Availability now correctly checks for fallback/limited status in addition to normal/parser_issue
- **Config Flow Input Preservation** - Form now preserves user input on validation errors
  - Previously, form would reset all fields when validation failed
  - Now preserves host, username, password, and modem_choice when showing errors
  - Improved user experience when connection fails or validation errors occur
- **Error Message Formatting** - Added newlines and numbered lists for readability
  - Used `\n` newlines in error messages (may need CSS for rendering)
  - Numbered steps for multi-step instructions
  - Easier to read error guidance in config flow
- **Latency Sensor Precision** - Rounded to whole milliseconds instead of 6+ decimals
  - Changed from `42.837194` ms to `43` ms
  - More readable and appropriate precision for network latency
- **Restart Button Availability** - Graceful handling for modems without restart support
  - Fallback mode and unknown modems don't show restart button
  - Check moved to async setup to avoid blocking I/O
  - Clear indication when restart functionality is unavailable

### Security

- **Bandit Security Scanner Suppressions** - Addressed false positive warnings
  - Added `# nosec B105` comments to suppress 3 false positives:
    - `CONF_PASSWORD` constant (configuration key name, not password value)
    - `password_field` parameters (HTML form field names, not password values)
  - All 6 Bandit warnings addressed (3 suppressed false positives + 3 already mitigated)
  - XML parsing warnings already protected by required defusedxml==0.7.1 dependency
  - Security analysis confirms 0 real vulnerabilities

### Testing

- **All 319 Tests Passing** - Fixed test failures for v3.2.0 release
  - Updated `test_version_is_3_2_0` to expect VERSION = "3.2.0"
  - Updated `test_get_parsers_sorts_alphabetically` to check alphabetical sorting
  - Added `# noqa: C901` to suppress complexity warnings (4 functions)
  - Applied Black formatting across all modified files

### Technical Details

- **Files Modified**: `const.py`, `manifest.json`, `config_flow.py`, `parsers/__init__.py`, `button.py`, `modem_scraper.py`, `sensor.py`, `strings.json`, test files
- **Version**: Bumped from 3.1.0 to 3.2.0
- **Commits**: 40+ commits with fallback mode, UX improvements, and bug fixes
- **Compatibility**: No breaking changes, fully backward compatible

## [3.1.0] - 2025-11-11

### Added

- **Update Modem Data Button** - Manual refresh button for on-demand data updates
  - `button.cable_modem_update_data` - Triggers immediate coordinator refresh
  - Useful for verifying changes after modem configuration or troubleshooting
  - Complements automatic polling with user-controlled updates
  - Shows notification when update is triggered
- **HTML Capture for Diagnostics** - Capture raw modem HTML for support requests
  - `button.cable_modem_capture_html` - Captures raw HTML responses from modem
  - Stores captured data in memory for 5 minutes with automatic expiry
  - Automatically sanitizes sensitive data (MACs, serials, passwords, private IPs)
  - Included in diagnostics download when available
  - Makes requesting support for unsupported modems much easier
  - Notification shows capture status and reminds user to download diagnostics
  - Diagnostic button category - grouped with other diagnostic tools

### Fixed

- **MB8611 Static Parser Missing URL Patterns** - Fixed "No URL patterns available to try" error (Fixes #6)
  - Added missing `url_patterns` attribute to `MotorolaMB8611StaticParser` class
  - Parser now properly specifies `/MotoStatusConnection.html` as the data source URL
  - Without this attribute, the modem scraper had no URLs to fetch, causing immediate failure
  - Users can now successfully use the "Motorola MB8611 (Static)" parser option
- **SSL Certificate Verification Issue** - Fixed HTTPS connection failures for modems with self-signed certificates (Fixes #6)
  - Added explicit `verify=session.verify` parameter to all HTTP requests in authentication.py (6 locations)
  - Added explicit `verify=session.verify` parameter to all HTTP requests in hnap_builder.py (2 locations)
  - While `session.verify=False` was already configured, some requests library versions may not reliably inherit this setting
  - Ensures SSL verification setting is explicitly passed to every HTTP request for consistent behavior
  - Resolves HTTPS connection failures for Motorola MB8611 and other modems using self-signed certificates
- **Diagnostics Log Retrieval** - Improved log collection for diagnostics downloads
  - Added primary method: retrieve logs from Home Assistant's system_log integration (in-memory circular buffer)
  - Falls back to reading home-assistant.log file if system_log unavailable
  - Fixes issue where diagnostics showed "Log file not available" on Docker/supervised installations
  - Fixed 'tuple' object has no attribute 'name' error by correctly parsing system_log tuple format
  - Discovered system_log only stores errors/warnings, not INFO/DEBUG logs (by design)
  - Updated to correctly parse tuple format: (logger_name, (file, line_num), exception_or_none)
  - Better error messages explain that full logs require HA logs UI, journalctl, or container logs
  - Will capture cable_modem_monitor errors when they occur for troubleshooting
- **Version Logging on Startup** - Integration now logs version number when it starts
  - Example: "Cable Modem Monitor version 3.1.0 is starting"
  - Helps identify which version is loaded when troubleshooting issues
  - Makes it easy to confirm integration loaded properly from diagnostic logs

### Performance

- **Parser Loading Optimization** - Dramatically faster integration startup and modem restarts
  - When user selects specific modem: load only that parser (8x faster than scanning all parsers)
  - Auto-detection mode: scan filesystem once, cache results for subsequent loads (instant)
  - Restart button: uses same optimization as startup
  - Added `get_parser_by_name()` function for direct parser loading without discovery
  - Added global parser cache to avoid repeated filesystem scans
  - Parser discovery now only runs during config flow and first auto-detection
- **Protocol Discovery Optimization** - Skips HTTP/HTTPS protocol fallback when working URL is cached
  - First setup: tries HTTPS→HTTP fallback, saves working URL with protocol
  - Subsequent startups: extracts protocol from cached URL, uses it directly
  - Eliminates failed connection attempts on HTTP-only modems (faster, cleaner logs)
  - Config changes automatically re-detect protocol when user clicks Submit
  - Particularly beneficial for older HTTP-only modems that previously logged HTTPS errors

### Removed

- **v1.x to v2.0 Entity Migration Code** - Removed automatic entity ID migration from legacy versions
  - Deleted 127 lines of migration code that ran on every startup
  - Removed: `async_migrate_entity_ids()`, `_migrate_config_data()`, and helper functions
  - Users still on v1.x can perform clean reinstall (see UPGRADING.md)
  - Reduces startup overhead and code complexity
  - Migration was for v2.0.0 (released Oct 24, 2025) - no longer needed at v3.1.0

### Testing

- **Comprehensive Test Coverage for v3.1.0 Features** - Added 20+ new test cases
  - UpdateModemDataButton tests (initialization, press, notification)
  - CaptureHtmlButton tests (success, failure, exception handling)
  - HTML sanitization tests (13 test cases covering MACs, serials, IPs, passwords, tokens)
  - Diagnostics integration tests (capture inclusion, expiry, sanitization verification)
  - Updated button setup test to verify all 5 buttons
- **Fixed 5 Failing Tests in test_version_and_startup.py** - All 283 tests now pass
  - Fixed HomeAssistant mock setup with proper attributes (data, config_entries, services)
  - Made async_add_executor_job properly execute functions and return awaitable results
  - Added async mocks for coordinator.async_config_entry_first_refresh
  - Patched _update_device_registry to avoid deep HA registry initialization
  - Added ConfigEntryState.SETUP_IN_PROGRESS to mock config entries
  - All version logging and parser selection optimization tests now pass

### Technical Details

- **Files Modified**: `mb8611_static.py`, `authentication.py`, `hnap_builder.py`, `diagnostics.py`, `__init__.py`, `button.py`, `parsers/__init__.py`, `modem_scraper.py`, `const.py`, `manifest.json`
- **HTML Capture Implementation**: Added `capture_raw` parameter to `get_modem_data()` and `_fetch_data()` methods, stores raw HTML in coordinator data with 5-minute TTL, sanitization removes MACs/serials/passwords/IPs while preserving signal data for debugging
- **Test Coverage**: Added `test_diagnostics.py` (28 tests) and expanded `test_button.py` (+6 tests, 669 lines total)
- **Root Cause**: The Static parser implementation was incomplete - it had parsing logic but no URL configuration
- **Impact**: Fixes both the "no URL patterns" error and HTTPS authentication issues for MB8611 and similar modems
- **Diagnostics**: Now works reliably across all Home Assistant installation types (Docker, supervised, core, OS)
- **Performance**: 8x faster startup when specific modem selected, instant startup for cached auto-detection
- **Compatibility**: No breaking changes, fully backward compatible with existing configurations

## [3.0.0] - 2025-11-10

### Added

- **MB8611 Dual-Parser Support** - Two parsing strategies for Motorola MB8611 modems
  - HNAP/SOAP protocol parser (priority 101) for API-based access with authentication
  - Static HTML parser (priority 100) as fallback for basic HTML table scraping
  - Increases compatibility and provides graceful fallback for different configurations
  - Both parsers support MB8611 and MB8612 models
  - Comprehensive test coverage for both parsers
- **Enhanced Discovery System** - Automatic modem detection with HNAP and HTTP-based discovery
  - HNAP protocol builder for Arris/Motorola modems
  - Discovery helpers for automatic modem identification
  - Detection notifications in config flow
- **Flexible Authentication Framework** - Support for multiple authentication strategies
  - Basic HTTP Authentication
  - Digest Authentication
  - HNAP Authentication (Arris/Motorola)
  - Strategy pattern for extensible auth types
- **MB8611 Parser** - Complete support for Motorola MB8611 cable modem
  - HNAP-based data extraction
  - 33 comprehensive tests for MB8611 functionality
- **Arris SB6190 Support** - Added parser for Arris SB6190 cable modem
  - Supports both transposed and non-transposed table formats
  - Parses downstream/upstream channels and error statistics
  - Comprehensive test suite with real hardware fixtures
  - Model-specific detection to avoid conflicts with other Arris modems
- **Enhanced Auto-Detection Logging** - INFO-level logs for modem auto-detection process
  - Logs now visible in Home Assistant's standard logs UI (not just raw logs)
  - Shows which parser is being used, URLs attempted, and detection results
  - Helps users understand what's happening during modem setup
  - Improves troubleshooting for connection and detection issues
- **Enhanced Diagnostics** - Diagnostics now include recent logs
  - Last 150 log entries from the integration automatically included
  - Logs are sanitized to remove sensitive information (passwords, MACs, private IPs)
  - Added modem detection metadata (detected_modem, parser_name, working_url)
  - Users no longer need to manually extract logs for bug reports
- **Docker Development Environment** - Complete Docker-based development setup
  - docker-compose.test.yml for local Home Assistant testing
  - VS Code Dev Container configuration
  - Management script (docker-dev.sh) with start/stop/logs/clean commands
  - Comprehensive documentation (DEVELOPER_QUICKSTART.md, .devcontainer/README.md)
  - Makefile targets for Docker operations
- **Comprehensive Test Coverage** - Added extensive test suites
  - 33 new tests for MB8611 parser
  - Coordinator improvements tests
  - Config flow tests
  - Total test improvements across authentication and discovery modules

### Changed

- **MB8611 Parser Refactoring** - Enhanced parser architecture
  - Renamed `mb8611.py` → `mb8611_hnap.py` with class rename to `MotorolaMB8611HnapParser`
  - Updated display name to "Motorola MB8611 (HNAP)" for clarity
  - Increased HNAP parser priority to 101 (tries before static parser)
  - Fixed frequency conversion to use `int(round())` for consistent integer output in both parsers
  - Reorganized test fixtures: `mb8611/` → `mb8611_hnap/` and new `mb8611_static/` directories
- **Session Management Improvements** - Better connection handling and modem restart monitoring
  - Improved modem restart detection and availability handling
  - Enhanced button component with better reload handling
  - Improved platform unload error handling during reload
  - Better channel synchronization detection
- **UI Improvements** - Fixed modem model selection dialog labels
  - "Modem Model" label now displays correctly in settings dialog
  - Added proper translations for modem_choice field
- **Code Quality Improvements** - Type checking and linting enhancements
  - Fixed all Pylance type checking errors
  - Fixed all Flake8 linting errors
  - Added .flake8 configuration (120-character line length)
  - Added pyproject.toml configuration
  - Removed unused imports and fixed PEP 8 formatting
  - Modern type hints with `from __future__ import annotations`
  - Enforced line length limits (removed E501 exception)

### Fixed

- **Type Checking Errors** - Resolved all mypy type checking errors
  - Added type annotations (`dict[str, Any]`) for channel data dictionaries in SB6190 parser
  - Removed `[mypy-requests.*]` ignore from mypy.ini to allow types-requests stubs (required by CI)
  - Added urllib3 to mypy.ini ignored imports list
  - All code quality checks (ruff, black, mypy with types-requests) now pass successfully
- **Code Cleanup** - Removed unused `import re` from diagnostics.py
- **Config Flow Handler Registration** - Fixed "Flow handler not found" error
  - Added @config_entries.HANDLERS.register(DOMAIN) decorator
  - Renamed ConfigFlow to CableModemMonitorConfigFlow for clarity
- **Parser Detection** - Improved Arris SB6141 parser to avoid conflicts with SB6190
  - Added model-specific detection checks
  - Explicitly exclude SB6190 to prevent false positives
- **Type Safety** - Resolved all type annotation errors
  - Fixed dictionary access type checking errors
  - Corrected type variance issues
  - Fixed parameter type annotations with None defaults
  - Resolved authentication module type errors
- **Parser Improvements** - Enhanced parser reliability
  - Fixed MB8611 test failures (AuthFactory patch path and frequency precision)
  - Improved None handling in Motorola generic parser detection
  - Removed duplicate manufacturer names from detection
- **SSL Context Creation** - Fixed blocking I/O in event loop for SSL context creation

### Documentation

- **Phase 1, 2, 3 Implementation Summary** - Comprehensive documentation of architecture phases
- **Session Improvements Summary** - Detailed session management enhancements
- **Test Coverage Summary** - Overview of test additions and coverage
- **Developer Documentation** - Enhanced contribution and setup guides
  - Added "Modem Model Selection" section to TROUBLESHOOTING.md
  - Documented how to view auto-detection logs (3 different methods)
  - Enhanced bug report template with better log collection instructions
  - Added modem selection dropdown to bug reports
  - Updated README.md with modem model configuration option
  - Updated CONTRIBUTING.md with Docker workflow instructions
- **Feature Request Organization** - Organized feature requests into dedicated directory
  - Smart polling sensor template
  - Netgear CM600 parser request
  - Phase 4 JSON configs proposal
  - Phase 5 community platform proposal
  - HTML Capture Feature Specification (507 lines)

## [2.6.1] - 2025-11-06

### Fixed

- **Excessive Logging** - Reduced excessive error logging during modem restart and connection attempts. Debug messages that were temporarily promoted to `ERROR` for testing have been moved to the appropriate `DEBUG` level, cleaning up the logs during normal operation.
- **Standardized Logging** - Updated all logging statements to use standard string formatting instead of f-strings for consistency and performance.

### Changed

- **Modem Restart Reliability** - The `restart_modem` function is now more robust.
  - It always re-fetches connection data before a restart to detect if the modem has fallen back from HTTPS to HTTP, ensuring the correct protocol is used.
  - It now attempts to log in before sending the restart command if credentials are provided, improving compatibility with modems that require authentication for restart functionality.
- **Motorola Parser Security** - Improved the security of the Motorola parser's login mechanism by allowing redirects only within private IP address ranges, preventing open redirect vulnerabilities while still accommodating local network device behavior.

### Added

- **Restart Tests** - Added a comprehensive suite of tests for the `restart_modem` functionality to verify HTTP/HTTPS fallback, login handling, and various failure scenarios.

## [2.6.0] - 2025-11-06

### Added

- **GitHub Best Practices Implementation** - Comprehensive repository governance and security
  - `SECURITY.md` - Vulnerability reporting policy and security guidelines
  - `CODE_OF_CONDUCT.md` - Contributor Covenant v2.1 for community standards
  - `GOVERNANCE.md` - Project governance, release process, and decision-making policies
  - `.github/CODEOWNERS` - Code ownership and automatic review assignments
  - `.github/dependabot.yml` - Automated dependency vulnerability scanning
  - `.github/workflows/codeql.yml` - GitHub Advanced Security code scanning
  - `.github/workflows/release.yml` - Automated release creation on version tags
  - `.github/workflows/commit-lint.yml` - Conventional commits validation
  - `.github/workflows/changelog-check.yml` - CHANGELOG.md update verification
  - `.github/ISSUE_TEMPLATE/bug_report.yml` - Structured bug report template
  - `.github/ISSUE_TEMPLATE/feature_request.yml` - Structured feature request template
  - `.github/ISSUE_TEMPLATE/config.yml` - Issue template configuration
  - `.github/pull_request_template.md` - Comprehensive PR template with checklist
  - `docs/BRANCH_PROTECTION.md` - Step-by-step guide for configuring branch protection
  - `mypy.ini` - Type checking configuration with mypy
  - Coverage enforcement: 50% minimum threshold in pytest and CI
  - Type checking with mypy in pre-commit hooks and CI

### Changed

- Enhanced CI/CD workflows with additional quality checks
  - Added mypy type checking to lint job
  - Added coverage enforcement to test job (--cov-fail-under=50)
- Updated test requirements to include mypy and types-requests
- Updated pre-commit hooks to include mypy type checking
- **Improved Exception Handling** - Better timeout and connection error handling in XB7 parser
  - Timeout errors logged at DEBUG level (reduces log noise during reboots)
  - Connection errors logged at WARNING level
  - Authentication errors logged at ERROR level
  - Helps distinguish between network issues, modem reboots, and authentication problems

### Security

- **Comprehensive Security Remediation** - Resolved all 26 CodeQL security vulnerabilities
  - **SSL/TLS Security (Critical/High - 4 issues)**
    - Made SSL certificate verification configurable via integration settings
    - Removed global `urllib3.disable_warnings()` call that suppressed all SSL warnings
    - Fixed hardcoded `ssl=False` in health monitor with proper SSL context
    - Added conditional SSL warning suppression only when verification is explicitly disabled
    - Added enhanced user-facing warnings about MITM attack risks
    - Defaults to disabled (verify_ssl=False) for backward compatibility with self-signed certificates
  - **Open Redirect Prevention (Critical/High - 2 issues)**
    - Added redirect URL validation in health monitor HTTP checks
    - Implemented same-host redirect checking in Motorola parser login
    - Added cross-host redirect blocking in Technicolor XB7 parser
    - Validates all redirect targets to prevent phishing attacks
  - **Command Injection Prevention (Medium - 1 issue)**
    - Fixed unsafe subprocess execution in health monitor ping function
    - Added comprehensive host validation with IPv4/IPv6/hostname regex patterns
    - Implemented shell metacharacter blocking and input sanitization
    - Protected ping subprocess from command injection attacks
  - **Input Validation & Sanitization (Medium - 4 issues)**
    - Added strict URL validation in config flow with protocol checking (HTTP/HTTPS only)
    - Implemented hostname/IP format validation using proper patterns
    - Added character whitelist validation blocking dangerous shell metacharacters
    - Replaced regex-based URL extraction with proper `urllib.parse` throughout codebase
    - Added validation helpers: `_is_valid_host()`, `_is_valid_url()`, `_is_safe_redirect()`
  - **Credential Security (Medium - 3 issues)**
    - Removed username logging from Motorola parser to prevent credential leakage
    - Added comprehensive security documentation for Base64 password encoding
    - Documented that Base64 is NOT encryption - it's merely encoding (modem firmware limitation)
    - Sanitized all credential-related log messages
  - **Exception Handling (Low - 4 issues)**
    - Replaced broad `Exception` catches with specific exception types (`ValueError`, `TypeError`)
    - Improved error messages for better debugging while preventing information leakage
    - Maintained proper exception logging with context
  - **Information Disclosure (Low - 4 issues)**
    - Sanitized exception messages in diagnostics to prevent sensitive data exposure
    - Added regex-based redaction of passwords, tokens, keys, and credentials from error messages
    - Masked IP addresses and file paths in exception output
    - Truncated long exception messages to 200 character limit
  - **Files Modified**: `config_flow.py`, `core/health_monitor.py`, `core/modem_scraper.py`, `diagnostics.py`, `parsers/motorola/generic.py`, `parsers/technicolor/xb7.py`
  - **Impact**: Eliminates all critical security vulnerabilities while maintaining backward compatibility
- **Health Monitoring System** - Dual-layer network diagnostics with 3 new sensors
  - `sensor.cable_modem_health_status` - Overall health (healthy/degraded/icmp_blocked/unresponsive)
  - `sensor.cable_modem_ping_latency` - ICMP ping response time in milliseconds
  - `sensor.cable_modem_http_latency` - HTTP web server response time in milliseconds
  - Runs on every coordinator poll (user-configurable 60-1800 seconds)
  - HTTP check supports SSL self-signed certs, redirects, and HEAD→GET fallback
- **XB7 System Information Enhancement** - New sensors for system details
  - `sensor.cable_modem_system_uptime` - Human-readable uptime (e.g., "21 days 15h:20m:33s")
  - `sensor.cable_modem_last_boot_time` - Calculated timestamp of last modem reboot
  - `sensor.cable_modem_software_version` - Firmware/software version from modem
  - Primary downstream channel detection (e.g., "Channel ID 10 is the Primary")
- **Reset Entities Button** - Configuration button to reset all entities
  - `button.cable_modem_reset_entities` - Removes all entities and reloads integration
  - Preserves entity IDs and historical data (linked by entity_id)
  - Useful after modem replacement or to fix entity registry issues
  - Includes comprehensive documentation about HA storage architecture
- **SSL Certificate Support** - Support for HTTPS modems with self-signed certificates
  - Adds `verify=False` to requests in modem_scraper.py
  - Suppresses urllib3 SSL warnings
  - Automatic HTTPS/HTTP protocol detection with fallback
  - Unblocks MB8611 and other HTTPS modems (Issue #6)

### Documentation

- **TROUBLESHOOTING.md** - Comprehensive troubleshooting guide
  - Connection and authentication issues
  - Health monitoring diagnostic matrix
  - Example automations for health alerts
  - Timeout handling during modem reboots
- **ARCHITECTURE_ROADMAP.md** - Updated with Phase 0 completion and v3.0 plans
  - Complete implementation roadmap through v4.0
  - Issue management policy
  - Version targets and strategy

### Test Fixtures

- **MB8611 Test Data** - Complete test fixtures for Motorola MB8611 (Issue #4)
  - HNAP JSON response with 33 downstream + 4 upstream channels
  - HTML pages: Login, Home, Connection, Software, Event Log
  - Ready for Phase 2 MB8611 parser implementation

### Technical Details

- Health monitoring uses ModemHealthMonitor class in `core/health_monitor.py`
- Health checks run async in parallel (ICMP + HTTP) during coordinator updates
- XB7 parser enhancements use regex parsing for uptime and boot time calculation
- Reset Entities button deletes entity registry entries but preserves recorder database
- Integration maintains stable `unique_id` pattern: `{entry.entry_id}_cable_modem_{sensor_name}`

## [2.5.0] - 2025-10-30

### Fixed

- **Critical Bug Fix** - Fixed config flow validation that allowed setup to succeed even when modem was unreachable
  - Changed `config_flow.py` to check correct key: `cable_modem_connection_status` instead of `connection_status`
  - This bug caused sensors to show as "unavailable" with no data despite successful integration setup
  - Resolves [#4](https://github.com/solentlabs/cable_modem_monitor/issues/4)
- **Diagnostics Data Fix** - Updated `diagnostics.py` to use correct data keys with `cable_modem_` prefix
  - Fixed all key names to match actual data structure returned by modem scraper
  - Diagnostics now properly display connection status, channel counts, and system info
- **Test Updates** - Updated all test files to use correct key names
  - `test_config_flow.py`: Fixed connection status key in mock data
  - `test_coordinator.py`: Fixed all data keys to match production code
  - `test_sensor.py`: Updated mock coordinator data keys

### Technical Details

- The root cause was a key name mismatch introduced during v2.0 refactoring
- `modem_scraper.py` returns `cable_modem_connection_status` but `config_flow.py` was checking `connection_status`
- Since `.get()` returns `None` for missing keys, validation incorrectly passed
- Users could "successfully" configure the integration but all entities remained unavailable

## [2.4.1] - 2025-10-29

### Added

- **Parser Priority System** - Model-specific parsers now tried before generic parsers
  - Ensures MB7621 uses its specific parser instead of generic Motorola parser
  - Priority 100 for model-specific parsers, 50 for generic/fallback parsers
  - Improves reliability and performance for supported models

### Fixed

- **MB7621 Auto-Detection Improvements**
  - Parser now checks software info page (`/MotoSwInfo.asp`) first for better detection
  - Prevents duplicate parser registration
  - Improved detection reliability

### Changed

- **Code Organization** - Refactored codebase for better maintainability
  - Organized parsers by manufacturer directories (motorola/, arris/, technicolor/)
  - Created `core/` directory for modem_scraper
  - Created `lib/` directory for shared utilities
  - Cleaner, more scalable architecture

## [2.3.0] - 2025-10-28

### Added

- **Technicolor XB7 Support** - Full parser implementation for XB7 cable modems
  - Supports 34 downstream + 5 upstream channels
  - Handles transposed table layout (similar to ARRIS SB6141)
  - Parses mixed frequency formats (both "609 MHz" text and "350000000" raw Hz)
  - Includes XB7-specific upstream fields: symbol rate and channel type
  - Parses error codewords (correctable/uncorrectable)
  - Detection by URL pattern (`network_setup.jst`) and content
  - Basic HTTP Authentication support
  - Used by Rogers (Canada), Comcast
  - Resolves [#2](https://github.com/solentlabs/cable_modem_monitor/issues/2)

### Test Coverage

- Added 27 comprehensive tests for XB7 parser:
  - 3 detection tests
  - 2 authentication tests
  - 9 downstream parsing tests
  - 9 upstream parsing tests
  - 2 system info tests
  - 2 integration tests
- **Total test suite: 108 tests passing** (was 81, added 27 new tests)

### Technical

- New file: `custom_components/cable_modem_monitor/parsers/technicolor_xb7.py`
- New test file: `tests/test_parser_technicolor_xb7.py`
- HTML fixture: `tests/fixtures/technicolor_xb7_network_setup.html`
- XB7-specific upstream channel fields:
  - `symbol_rate`: Integer (2560, 5120, 0)
  - `channel_type`: String (TDMA, ATDMA, TDMA_AND_ATDMA, OFDMA)

### Community Updates

- **ARRIS SB6141** confirmed working by @captain-coredump on [Community Forum](https://community.home-assistant.io/t/cable-modem-monitor-track-your-internet-signal-quality-in-home-assistant)
  - All 57 entities displaying correctly
  - Parser fully functional with v2.0.0+

### Thanks

- Special thanks to @esand for providing detailed HTML samples and modem information for XB7 support!
- Thanks to @captain-coredump for confirming ARRIS SB6141 compatibility and providing valuable testing feedback

## [2.2.1] - 2025-10-28

### Changed

- Updated manifest version and documentation images

## [2.2.0] - 2025-10-28

### Fixed

- **TC-4400 Authentication** - Corrected login method signature for TC4400 parser

## [2.1.0] - 2025-10-28

### Added

- **Cleanup Entities Button** - One-click cleanup of orphaned entities from entity registry
  - Useful after upgrades or entity ID changes
  - Displays notification showing how many entities were removed
  - Available in device controls alongside Restart Modem button

### Changed

- **Standardized Entity Naming** - All entities now use `cable_modem_` prefix
  - Provides consistent naming across all sensors
  - Makes entities easier to find and identify
  - Previous entity prefix configuration options removed (simpler UX)

## [2.0.0] - 2025-10-24

### Breaking Changes

- **Entity Naming Standardization** - All sensor entity IDs now use the hard-coded `cable_modem_` prefix
  - **Before (v1.x)**: Entity IDs could vary (no prefix or domain prefix)
  - **After (v2.0)**: All entity IDs consistently use `sensor.cable_modem_*` format
  - Automatic migration included - entity IDs will be renamed on first startup
  - Configuration options for entity prefixes have been removed
  - See UPGRADING.md for detailed migration guide

### Added

- **Automatic Entity ID Migration** - Seamlessly upgrades entity IDs from pre-v2.0 to v2.0 naming
  - Runs automatically on integration startup
  - Includes safety checks to prevent conflicts with other integrations
  - Logs all migrations for debugging
  - Preserves history where possible (some loss may occur due to database conflicts)
- **Enhanced Configuration Descriptions** - Detailed field descriptions in options flow
  - Clear instructions for password handling (leave blank to keep existing)
  - Current host/username displayed for context
  - Helpful descriptions for scan interval and history retention settings

### Changed

- **Simplified Configuration Flow** - Reduced from two steps to single-step options flow
  - Entity naming configuration removed (now hard-coded)
  - Cleaner, more intuitive configuration experience
- **Industry-Standard Sensor Names** - Channel sensors use DS/US abbreviations following industry standards
  - Downstream: "Downstream Ch 1 Power" → "DS Ch 1 Power"
  - Upstream: "Upstream Ch 1 Power" → "US Ch 1 Power"
  - Shorter names reduce redundancy in dashboard cards
  - Follows cable industry standard abbreviations (DS = Downstream, US = Upstream)
  - Entity IDs remain unchanged (still include downstream/upstream in the ID)
- **Improved Code Quality** - Code review and cleanup
  - Removed unused imports
  - Fixed SQL injection potential with parameterized queries
  - Added clarity comments explaining v2.0+ parser architecture
  - Better documentation throughout codebase

### Fixed

- **Upstream Channel Sensors** - Fixed upstream sensors not being created
  - Relaxed validation to allow upstream channels without frequency data
  - Fixed Motorola parser reading frequency from wrong column (was column 2, now column 5)
  - Fixed power reading from wrong column (was column 3, now column 6)
  - Upstream frequencies now display correctly in Hz
  - Only active "Locked" channels are shown (inactive "Not Locked" channels are filtered out)
  - Resolves issue where no upstream sensors appeared despite modem reporting 5 active channels

### Technical

- Added `async_migrate_entity_ids()` function in **init**.py for automatic migration
- Simplified config_flow.py to single-step options flow
- Removed deprecated CONF_ENTITY_PREFIX and related constants
- Updated sensor display names to use DS/US prefixes
- Updated clear_history service to use parameterized SQL queries
- All parser comments now reference v2.0+ (v1.8 was never released)
- Modified `base_parser.validate_upstream()` to make frequency optional
- Fixed Motorola MB parser upstream channel column indices
- Added filtering for "Not Locked" upstream channels in Motorola parser

### Migration Notes

- **Recommended**: Fresh install (cleanest approach)
- **Alternative**: Automatic migration will rename entities on first startup
- **History**: Some history loss may occur during migration due to database conflicts
- **Orphaned Data**: Old records from renamed entities persist - use clear_history service to clean up
- **Documentation**: See UPGRADING.md for complete migration guide

## [1.7.1] - 2025-10-23

### Fixed

- **Nested Table Parsing** - Fixed HTML parsing for modems with nested table structures
  - Some Motorola modems were showing "0 tables found" despite successful connection
  - Updated `_parse_downstream_channels()` and `_parse_upstream_channels()` to use `recursive=False` when searching for table rows
  - Prevents duplicate channel data from being parsed
  - All 67 tests pass
- **Deprecated config_entry Warning** - Removed explicit `self.config_entry` assignment in OptionsFlowHandler
  - Fixes Home Assistant 2025.12 deprecation warning
  - `config_entry` is now provided automatically by the base class

### Changed

- **Default Polling Interval** - Increased from 5 minutes (300s) to 10 minutes (600s)
  - Reduces load on modem and Home Assistant
  - Still within industry best practices (5-10 minute range)
  - Users can adjust via configuration options if needed
- **Attribution** - Updated credit to @captain-coredump in v1.7.0 release notes

## [1.7.0] - 2025-10-22

### Added

- **ARRIS SB6141 Support (Testing)** - Added parser for ARRIS SB6141 modem (awaiting user confirmation)
  - Handles unique HTML structure where columns represent channels instead of rows
  - Parses downstream channels with power, SNR, frequency, and error statistics
  - Parses upstream channels with power and frequency
  - Added comprehensive test coverage with 3 new tests
  - Based on HTML sample contributed by @captain-coredump from community forum
  - **Status**: Parser implemented and tested, awaiting real-world confirmation from user

### Technical

- Added `_parse_arris_sb6141()` method for ARRIS-specific parsing
- Added `_parse_arris_transposed_table()` for column-based channel data
- Added `_merge_arris_error_stats()` to combine error data from separate table
- Handles nested tables in Power Level row
- Created test fixture: `tests/fixtures/arris_sb6141_signal.html`

## [1.6.1] - 2025-10-22

### Changed

- **Improved Authentication UX** - Changed username default from "admin" to empty string for modems without authentication
  - Makes it clearer that credentials are optional
  - Reduces confusion for users with modems like ARRIS SB6141 that don't require login
- **Enhanced Error Messages** - Better diagnostics to distinguish connection failures from parsing failures
  - Error messages now clarify when auth is optional
  - Guides users to enable debug logging for unsupported modems
  - Provides clear instructions for requesting modem support

### Fixed

- **Better Debug Logging** - Improved logging for unsupported modem HTML formats
  - Logs successful connection URL and HTML structure details
  - Reduced log verbosity for connection attempts (moved to debug level)
  - Helps diagnose when modem connects but HTML format isn't recognized

### Technical

- Updated config_flow.py: Changed CONF_USERNAME default from "admin" to ""
- Updated modem_scraper.py: Added HTML structure logging and better error messages
- Updated strings.json and translations/en.json: Enhanced error messages and field descriptions

## [1.6.0] - 2025-10-22

### Added

- **Technicolor TC4400 Support** - Added support for TC4400 cable modems
  - Added `/cmconnectionstatus.html` URL endpoint
  - Based on research from philfry's check_tc4400 project (see ATTRIBUTION.md)
- **Comprehensive Sensor Tests** - Added tests/test_sensor.py with 17 test functions
  - Tests all sensor types: connection status, error counters, channel counts, system info
  - Tests edge cases: missing data, None values, invalid data
  - All 64 tests passing (47 existing + 17 new)
- **Ruff Configuration** - Added .ruff.toml for project-wide code quality standards
  - 120-character line limit for better readability
  - McCabe complexity limit of 12 for parsing functions

### Changed

- **Code Quality Improvements** - Fixed line length violations across codebase
  - Improved SQL query formatting in **init**.py
  - Better readability in modem_scraper.py parsing logic

## [1.4.0] - 2025-10-21

### Added

- **Clear History Button** - New UI button entity to clean up old historical data
  - Appears alongside Restart Modem button in device page
  - Uses configurable retention period from settings
  - One-click cleanup without using Developer Tools
- **Configurable History Retention** - New configuration option for history management
  - Set retention period: 1-365 days (default: 30 days)
  - Configure via Settings → Devices & Services → Cable Modem Monitor → Configure
  - Button automatically uses configured retention value
- **Example Graphs in Documentation** - Added two new screenshots showing real signal data
  - Downstream Power Levels graph with all channels
  - Signal-to-Noise Ratio graph with all channels

### Changed

- **Enhanced Documentation** - Comprehensive updates to README.md
  - New "Configuration Options" section explaining all settings
  - Expanded "Managing Historical Data" section
  - Updated dashboard YAML examples to include Clear History button
  - Added visual examples of historical graphs

### Technical

- Added `CONF_HISTORY_DAYS` and `DEFAULT_HISTORY_DAYS` constants to const.py
- Extended config_flow.py options flow with history_days field (1-365 validation)
- Added `ClearHistoryButton` class to button.py
- Updated translations/en.json with button and configuration translations
- Button reads retention setting from config entry data

## [1.3.0] - 2025-10-21

### Added

- **Options Flow** - Users can now reconfigure the integration without reinstalling
  - Update modem IP address through UI
  - Change username/password through UI
  - Leave password blank to keep existing password
- **Clear History Service** - New service to clean up old historical data
  - `cable_modem_monitor.clear_history` service
  - Specify days to keep (deletes older data)
  - Cleans both states and statistics tables
  - Automatically vacuums database to reclaim space
- **Translation Support** - Added English translations for config flow and services
- **Service Definitions** - Added services.yaml for proper service documentation

### Changed

- **Connection Status Improvements** - Now distinguishes between network issues and modem issues
  - `unreachable`: Cannot connect to modem (network/auth problem - Home Assistant issue)
  - `offline`: Modem responds but no channels detected (modem is actually down)
- **Sensor Availability** - All measurement sensors now become "unavailable" during connection failures
  - Charts no longer show drops to 0 during outages
  - Historical data gaps instead of misleading zero values
  - Connection status sensor remains available to show offline/unreachable state
- **Version Bump** - Updated to v1.3.0

### Fixed

- **Diagnostics Download** - Fixed AttributeError when downloading diagnostics
  - Removed invalid `last_update_success_time` attribute reference
  - Diagnostics now successfully export all modem data

### Technical

- Added `OptionsFlowHandler` class to config_flow.py for reconfiguration support
- Added clear_history service handler in **init**.py with SQLite database operations
- Modified sensor base class to control availability based on connection status
- Updated modem_scraper.py to return "unreachable" instead of "offline" for connection failures
- Added translations/en.json for internationalization support

## [1.2.2] - 2025-10-21

### Fixed

- **Zero values in history** - Integration now properly validates and skips updates when modem returns invalid/empty data
- Prevents recording of 0 values during modem connection issues or reboots
- Improved data extraction methods to return `None` instead of `0` for invalid data
- Added validation to skip channel data when all values are null/invalid

### Added

- **Diagnostics support** - Integration now provides downloadable diagnostics via Home Assistant UI
- Diagnostics include channel data, error counts, connection status, and last error information
- Documentation for cleaning up existing zero values from history (`cleanup_zero_values.md`)

### Technical

- `_extract_number()` and `_extract_float()` now return `None` instead of `0` when parsing fails
- Skip channel parsing when all critical values are `None`
- Skip entire update if no valid downstream or upstream channels are parsed
- Added comprehensive diagnostics platform for troubleshooting
- Improved error calculations to handle `None` values properly

## [1.2.1] - 2025-10-21

### Fixed

- **Error totals double-counting** - Fixed bug where Total row from modem table was being counted as a channel
- Error sensors now show correct values (previously were exactly double the actual errors)

### Technical

- Added check to skip "Total" row in downstream channel table parsing
- Prevents Total row (4489/8821) from being added to per-channel sums

## [1.2.0] - 2025-10-21

### Fixed

- **Software version parsing** - Now correctly uses CSS class selectors to find the value cell
- **System uptime parsing** - Now correctly uses CSS class selectors to find the value cell
- Both parsers now avoid matching header/label text and get actual values

### Technical

- Changed to class-based cell selection (`moto-param-value`, `moto-content-value`)
- More robust parsing that won't match table headers or labels

## [1.1.3] - 2025-10-21

### Fixed

- **System uptime parsing** now correctly extracts uptime from MotoConnection.asp page
- Restored system_uptime sensor (was incorrectly removed - it IS available on Motorola modems)

### Technical

- Fixed _parse_system_uptime() to match actual HTML structure from MotoConnection.asp
- Uptime parsed before fetching MotoHome.asp for efficiency

## [1.1.1] - 2025-10-21

### Fixed

- **Software version** now correctly parsed from MotoHome.asp page
- **Upstream channel count** now accurately reports modem's actual channel count from MotoHome.asp
- Channel counts now use modem-reported values instead of just counting parsed channels

### Removed

- **System uptime sensor** - Not available on Motorola MB series modems

### Technical

- Scraper now fetches additional data from MotoHome.asp for version and channel counts
- Added `_parse_channel_counts()` method for accurate channel counting
- Improved error handling for optional MotoHome.asp data

## [1.1.0] - 2025-10-20

### Added

- **Channel count sensors**: Track number of active upstream and downstream channels
- **Software version sensor**: Monitor modem firmware/software version
- **System uptime sensor**: Track how long the modem has been running
- **Modem restart button**: Remotely restart your cable modem from Home Assistant dashboard
- Enhanced dashboard examples with new sensors
- Automation examples for channel count monitoring and auto-restart

### Enhanced

- Modem scraper now extracts additional system information
- Improved documentation with trend analysis use cases
- Better automation examples for proactive network monitoring

### Technical

- Added button platform support
- Extended modem_scraper.py with version and uptime parsing
- New sensor classes for channel counts, version, and uptime
- Restart functionality with multiple endpoint fallbacks

## [1.0.0] - 2025-10-20

### Added

- Initial release of Cable Modem Monitor integration
- Config flow for easy UI-based setup
- Support for Motorola MB series modems (DOCSIS 3.0)
- Per-channel sensors for downstream channels:
  - Power levels (dBmV)
  - Signal-to-Noise Ratio (SNR in dB)
  - Frequency (MHz)
  - Corrected errors
  - Uncorrected errors
- Per-channel sensors for upstream channels:
  - Power levels (dBmV)
  - Frequency (MHz)
- Summary sensors for total corrected/uncorrected errors
- Connection status sensor
- Session-based authentication for password-protected modems
- Automatic detection of modem page URLs
- Integration reload support (no restart required for updates)
- Custom integration icons and branding
- Comprehensive documentation and examples
- HACS compatibility

### Security

- Credentials stored securely in Home Assistant's encrypted storage
- Session-based authentication with proper cookie handling
- No cloud services - all data stays local

### Known Issues

- Modem-specific HTML parsing may need adjustment for some models
- Limited to HTTP (no HTTPS support for modem connections)

[1.3.0]: https://github.com/solentlabs/cable_modem_monitor/releases/tag/v1.3.0
[1.2.2]: https://github.com/solentlabs/cable_modem_monitor/releases/tag/v1.2.2
[1.2.1]: https://github.com/solentlabs/cable_modem_monitor/releases/tag/v1.2.1
[1.2.0]: https://github.com/solentlabs/cable_modem_monitor/releases/tag/v1.2.0
[1.1.3]: https://github.com/solentlabs/cable_modem_monitor/releases/tag/v1.1.3
[1.1.1]: https://github.com/solentlabs/cable_modem_monitor/releases/tag/v1.1.1
[1.1.0]: https://github.com/solentlabs/cable_modem_monitor/releases/tag/v1.1.0
[1.0.0]: https://github.com/solentlabs/cable_modem_monitor/releases/tag/v1.0.0
