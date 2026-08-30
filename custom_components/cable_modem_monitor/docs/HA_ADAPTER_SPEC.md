# HA Adapter Specification

How the Home Assistant integration wires Core and Catalog into HA's
lifecycle. This spec defines the adapter layer — everything between
Core's synchronous API and HA's async event loop.

**Design principle:** The adapter is thin. Core owns all modem logic
(auth, parsing, polling policy, restart recovery). The adapter owns
scheduling, entity creation, and HA lifecycle. If logic could live in
Core, it should.

**Minimum HA version:** 2024.12 (for `entry.runtime_data` support).

**Related specs:**

- `ENTITY_MODEL_SPEC.md` — what entities to create
- `CONFIG_FLOW_SPEC.md` — setup wizard and options flow
- `ORCHESTRATION_SPEC.md` — Core's interface contracts
- `RUNTIME_POLLING_SPEC.md` — polling behavior and signal policy
- `ARCHITECTURE.md` — system design and package boundaries

---

## Contents

| Section | What it covers |
|---------|----------------|
| [Runtime Data](#runtime-data) | `CableModemRuntimeData` structure on `entry.runtime_data` |
| [Persistence Layers](#persistence-layers) | Where state lives: `runtime_data`, `entry.data`, `entry.options`, Store helper |
| [Startup](#startup) | `async_setup_entry` — component creation and wiring |
| [Unload](#unload) | `async_unload_entry` — cleanup and cancellation |
| [Removal](#removal) | `async_remove_entry` cleanup, recorder no-purge design choice, removal-docs rule |
| [Async Boundary](#async-boundary) | Which Core calls need executor wrapping |
| [Data Coordinator](#data-coordinator) | DataUpdateCoordinator wrapping `get_modem_data()` and deferred entity creation |
| [Health Coordinator](#health-coordinator) | Second coordinator wrapping `health_monitor.ping()` |
| [Polling Modes](#polling-modes) | Scheduled, disabled, manual trigger |
| [Restart Lifecycle](#restart-lifecycle) | Button → executor → one-shot command → return |
| [Recovery Adapter](#recovery-adapter) | Observer + cadence listener that reacts to Core's `recovery_active` flag |
| [Operation Mutex](#operation-mutex) | `active_operation` field — mutex between destructive buttons (restart, reset) |
| [Reset Entities Concurrency Guard](#reset-entities-concurrency-guard) | `active_operation` guard, null-safety |
| [Reauth Flow](#reauth-flow) | Circuit breaker → `async_step_reauth` |
| [Diagnostics Platform](#diagnostics-platform) | Core diagnostics + HA-side data |
| [Services](#services) | `generate_dashboard`, `request_refresh`, `request_health_check` |
| [Channel Bond Change Notifications](#channel-bond-change-notifications) | First-poll onboarding + totals-change detection with `generate_dashboard` hint |
| [Config Entry Migration](#config-entry-migration) | Version-keyed migration with auto-discovery |
| [Testing](#testing) | No modem-specific names, dynamic catalog discovery |
| [Distribution](#distribution) | HACS zip, PyPI packages, version pinning, release tiers |

---

## Runtime Data

All runtime state lives on `entry.runtime_data`. HA manages cleanup
automatically on unload.

```python
# ModemIdentity is defined in Core (see ORCHESTRATION_SPEC.md § Data Models).
# Populated from modem.yaml at config load time. Fields: manufacturer,
# model, docsis_version, release_date, status.


@dataclass
class CableModemRuntimeData:
    """All runtime state for one config entry."""

    data_coordinator: DataUpdateCoordinator
    health_coordinator: DataUpdateCoordinator | None
    orchestrator: Orchestrator
    health_monitor: HealthMonitor | None
    modem_identity: ModemIdentity
    active_operation: Literal["restart", "reset"] | None = None


type CableModemConfigEntry = ConfigEntry[CableModemRuntimeData]
```

**`active_operation`** is the mutex for destructive buttons —
Restart and Reset Entities. Set to `"restart"` or `"reset"` while
the corresponding button handler runs, cleared in a context
manager's `finally`. A second destructive press while one is
running is refused. See § Operation Mutex and § Reset Entities
Concurrency Guard.

It is adapter-layer state, separate from Core's `recovery_active`
flag. The two answer different questions:

- `active_operation` — is a destructive *button handler* currently
  running? (True for ~2–5 s during a restart button press, or for
  seconds-to-minutes during a reset.)
- `orchestrator.recovery_active` — is the *modem* currently in a
  recovery window? (True for the duration of
  `Recovery.WINDOW_SECONDS` after any recovery trigger.)

Only `active_operation` gates the buttons: a press while it is set
is refused (logged, no dispatch). `recovery_active` never gates
anything — it only drives polling cadence. During a button-press
restart both are True briefly; after `restart()` returns,
`active_operation` clears while `recovery_active` continues for
the rest of the window.

**Access pattern:**

```python
# In sensor.py, button.py, diagnostics.py
async def async_setup_entry(
    hass: HomeAssistant,
    entry: CableModemConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.data_coordinator
    orchestrator = entry.runtime_data.orchestrator
```

**Why not `hass.data[DOMAIN]`:** The old pattern requires manual dict
management (setdefault, pop) and has no type safety. `runtime_data` is
typed, auto-cleaned, and scoped to the entry.

---

## Persistence Layers

Per-entry state lives in one of four places, each with different
lifetime and write-trigger semantics. Pick deliberately — the wrong
layer can cause spurious integration reloads or lost state on restart.

| Layer | Lifetime | Write triggers | Use for |
|-------|----------|----------------|---------|
| `entry.runtime_data` | Process lifetime; cleared on unload/reload | None | Live Core objects (orchestrator, coordinators), channel map |
| `entry.data` | Persistent across restarts | Fires update listener → integration reload | User config (host, credentials), validation-derived fields, write-once markers |
| `entry.options` | Persistent across restarts | Same as `entry.data` | User-editable settings from the options flow (`scan_interval`, `health_check_interval`) |
| `Store` helper | Persistent across restarts | None — silent writes | Runtime state that mutates at poll cadence (e.g., channel-bond baseline) |

**Picking a layer:**

- Does it need to survive HA restart? *No* → `runtime_data`. *Yes* →
  one of the other three.
- Does it mutate frequently (per poll, per change)? *Yes* → `Store`.
  Writing to `entry.data` or `entry.options` would fire the update
  listener on every change, reloading the integration.
- Is it user-facing setup config? → `entry.data`.
- Is it user-editable via Settings → Configure? → `entry.options`.
- Is it a marker set once and never mutated afterwards (e.g.
  `CONF_CHANNEL_ONBOARDING_ELIGIBLE`)? → `entry.data` is safe because
  the single write happens before any listener is registered.

**Store helper.** HA's `homeassistant.helpers.storage.Store` persists
a typed payload under HA's config storage without firing config-entry
listeners. Each Store domain gets its own module with typed load /
save / remove helpers — `channel_bond_storage.py` is the first.
Storage keys are namespaced `cable_modem_monitor.{entry_id}.{domain}`
and cleaned up via `async_remove_entry` in `__init__.py` when the
user deletes the config entry.

Follow the same pattern for future runtime-state domains: one module,
one typed payload, load / save / remove helpers, cleanup hook.

---

## Startup

`async_setup_entry` loads configs and delegates component assembly to
the Core factory. Config loading stays in the adapter (catalog path
is HA-specific); assembly logic lives in Core.

```text
async_setup_entry(hass, entry)
 │
 ├─ 1. Load configs from catalog
 │     catalog_path / manufacturer / model / modem[-variant].yaml
 │     → modem_config, parser_config, post_processor
 │     (runs in executor — file I/O)
 │
 ├─ 1a. Inject credential encoding (Core concern)
 │      apply_credential_encoding(modem_config, ...)
 │
 ├─ 2. Resolve health probe defaults
 │     modem.yaml health config → defaults
 │     config entry data → overrides
 │
 ├─ 3. Create orchestration graph via Core factory
 │     create_orchestrator(modem_config, parser_config,
 │         post_processor, base_url, username, password, ...)
 │     → (orchestrator, health_monitor, modem_identity)
 │
 ├─ 4. Create data DataUpdateCoordinator
 │     update_method wraps orchestrator.get_modem_data()
 │     update_interval from config (or None if disabled)
 │
 ├─ 4a. Attach the recovery cadence listener (see § Recovery Adapter)
 │      attach_recovery_cadence_listener(hass, entry, orchestrator,
 │                                        data_coordinator)
 │      Registers the observer on Core, installs the cadence listener,
 │      and registers an unsubscribe callback for entry unload.
 │
 ├─ 5. Create health DataUpdateCoordinator (if health_monitor)
 │     update_method wraps health_monitor.ping()
 │     update_interval from config (or None if disabled)
 │
 ├─ 5a. Attach health sync listeners (if health_monitor)
 │      Health → data: on a down → up edge of HealthStatus.data_path_up
 │      (up = RESPONSIVE or ICMP_BLOCKED, both TCP-proven),
 │      triggers immediate data poll via coordinator.async_request_refresh()
 │      Data → health: on a successful poll while health still reads
 │      UNRESPONSIVE/DEGRADED, triggers immediate health refresh
 │      (see § Health Sync Listeners)
 │
 ├─ 6. Run first poll
 │     coordinator.async_config_entry_first_refresh()
 │     (always runs, even if polling is disabled)
 │
 ├─ 7. Store RuntimeData on entry
 │     entry.runtime_data = CableModemRuntimeData(...)
 │
 ├─ 8. Forward platform setup (sensor, button)
 │
 └─ 9. Update device registry
```

Services are **not** registered here. They are integration-global and
registered once in `async_setup` (see § Services) so they exist before
any entry is set up and survive an entry being removed and re-added.

**Steps 1-3 involve sync I/O** — all must run in executor via
`hass.async_add_executor_job()`. Step 3 delegates to the Core
factory which creates the collector, health monitor, orchestrator,
and identity internally.

**Step 6 always runs.** Even when polling is disabled, the first poll
runs during setup so entities have real data. "Disabled" means no
scheduled polls after setup, not "never poll."

**Two failure modes, handled differently.** Steps 1–5 (catalog config
load) and Step 6 (first poll) fail in different ways:

- **Catalog load fails** (Steps 1–5 — modem.yaml missing or corrupt, a
  bad catalog install). This is a permanent misconfiguration that HA's
  retry cannot fix, so `async_setup_entry` raises `ConfigEntryError`.
  HA surfaces a clear setup error rather than logging a bare "failed to
  set up" (Bronze `test-before-setup`).
- **Modem unreachable** (Step 6). The orchestrator never raises — the
  first poll returns `ModemSnapshot(UNREACHABLE, modem_data=None)` and
  setup proceeds. Always-available entities (Status, Info, Health)
  appear immediately; data-dependent entities are created later via the
  deferred-creation listener (see § Data Coordinator → Deferred Entity
  Creation). This is the proceed-when-unreachable design and is
  deliberately *not* a `ConfigEntryNotReady` — the integration is
  usable offline, showing the outage.

---

## Unload

`async_unload_entry` stops scheduled activity and cleans up.

```text
async_unload_entry(hass, entry)
 │
 ├─ 1. Unload platforms (sensor, button)
 │     hass.config_entries.async_unload_platforms(entry, PLATFORMS)
 │     (stops the data + health coordinators' scheduled polls)
 │
 ├─ 2. Close the orchestrator (session logout + socket pool release)
 │
 └─ 3. runtime_data auto-cleaned by HA
```

**Services are not unregistered on unload.** They are integration-
global (registered in `async_setup`, which runs once per HA session).
Unregistering them here would permanently remove them when the last
entry unloads — a subsequently re-added entry would find the services
gone, because `async_setup` does not run again.

**No restart cancellation primitive.** `orchestrator.restart()` is
one-shot and returns in a few seconds, so there's nothing long-
running to cancel. An in-flight `restart()` executor call (if any)
completes naturally — the worst case is a single ~5 s delay during
unload.

**No threads to join.** Core doesn't spawn threads — HA manages all
scheduling via coordinators and `async_add_executor_job`. Executor
tasks return to the pool when they complete.

**Recovery state survives unload.** `orchestrator.recovery_active`
is memory on the orchestrator instance; when the entry unloads, the
instance is garbage-collected and the state goes with it. Fresh
`async_setup_entry` always starts with `recovery_active == False`.

---

## Removal

Deleting a config entry runs `async_unload_entry` then
`async_remove_entry`. HA removes the device, entities, and encrypted
config-entry data itself; `async_remove_entry` removes the
integration's own `Store` payloads (the channel-bond baseline — see
§ Persistence Layers).

**Recorder history is not purged — our choice, not an HA rule.**
`async_remove_entry` cleans up only the integration's own `Store`
payloads. Long-term statistics and states history live in HA's
recorder, which governs its own retention (`purge_keep_days`);
removing the entry does not specially purge them. States age out on
the recorder's normal schedule, and statistics persist as orphaned
rows — which is why the `orphaned_statistics` service exists. We could
purge on removal (the machinery is right there), but doing so unbidden
would destroy history a user may want, so we leave it to them. HA has
no quality-scale rule in either direction; this is a deliberate design
choice, not a cited best practice.

**User-facing removal instructions** live in `.github/README.md`
§ Removing the Integration, satisfying HA's Bronze
[`docs-removal-instructions`](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/docs-removal-instructions/)
rule (which covers documenting removal only — it says nothing about
data retention).

---

## Async Boundary

Core's API is synchronous (`requests`-based I/O). Every Core call from
HA must go through `hass.async_add_executor_job()`.

| Call site | Core method | Typical duration |
|-----------|------------|-----------------|
| Data coordinator poll | `orchestrator.get_modem_data()` | 2-10s |
| Health coordinator poll | `health_monitor.ping()` | 1-5s |
| Restart button | `orchestrator.restart()` | 2-5s (one-shot) |
| Config flow validation | `list_modems()`, config loading, validation poll | <5s |
| Diagnostics | `orchestrator.diagnostics()` | <1ms (reads memory state) |

**All Core calls are bounded.** `restart()` is one-shot (auth +
POST + session clear); `get_modem_data()` is one poll; `ping()` is
one probe. None of them block the executor thread beyond their own
direct work. Recovery observation — the "keep polling until the
modem is back" behavior — lives on HA's side as a coordinator-
cadence switch (see § Recovery Adapter). Core never waits on
recovery.

---

## Data Coordinator

Wraps `orchestrator.get_modem_data()` in HA's `DataUpdateCoordinator`.

```python
async def _async_update_data() -> ModemSnapshot:
    return await hass.async_add_executor_job(
        orchestrator.get_modem_data
    )

data_coordinator = DataUpdateCoordinator(
    hass,
    logger,
    name=f"Cable Modem {coordinator_label}",
    update_method=_async_update_data,
    update_interval=timedelta(seconds=scan_interval),  # or None
    config_entry=entry,
)
```

Where `coordinator_label` is `"{model} ({host})"` when model is known,
else just `"{host}"`. This ensures HA's built-in coordinator logging
includes both identifiers for log correlation with Core's `[MODEL]`
convention.

**Return type:** `ModemSnapshot` — contains `connection_status`,
`docsis_status`, `modem_data`, `health_info`, `error`. Channel counts
and aggregate fields (e.g., `total_corrected`) are already in
`modem_data.system_info` — computed by the parser coordinator.
Sensors read directly from the snapshot.

**No exception wrapping.** The orchestrator never raises — all failures
are captured in `ModemSnapshot.connection_status` and
`ModemSnapshot.error`. The coordinator always succeeds, and sensors
derive availability from the snapshot content (see
ENTITY_MODEL_SPEC § Availability).

**Availability is logged per transition, not per poll.** Because the
coordinator never raises, HA's built-in once-per-transition unavailable
logging never engages, so `_async_update_data` tracks the edge itself: a
WARNING (`"Modem unavailable [MODEL] — status"`) the first time a poll
comes back with an error, and an INFO (`"Modem available again
[MODEL]"`) on the first poll that succeeds afterwards. A sustained
outage therefore writes one line, not one per poll, and the pair brackets
the outage in the log (Silver `log-when-unavailable`).

**First refresh:** `async_config_entry_first_refresh()` runs during
setup. Because the orchestrator never raises, this call always
succeeds — even when the modem is unreachable. A failed first poll
returns `ModemSnapshot(UNREACHABLE, modem_data=None)`. The sensor
platform handles this via deferred entity creation (see below).

### Deferred Entity Creation

When the first poll returns `modem_data=None` (modem unreachable at HA
startup), data-dependent entities (channels, system metrics, LAN stats)
cannot be created because they require channel IDs and field presence
from the poll data. The sensor platform handles this by:

1. Creating always-available entities immediately (Status, Info, Health)
2. Registering a one-shot coordinator listener on the data coordinator
3. On each coordinator update, the listener checks for `modem_data`
4. On the first update with `modem_data is not None`: creates
   data-dependent entities via `async_add_entities` and unsubscribes
5. `entry.async_on_unload(unsub)` ensures clean teardown if the entry
   is unloaded before the modem comes online
6. Schedules a delayed re-notification task (1 second) that fires
   `coordinator.async_update_listeners()`, ensuring deferred
   entities receive `_handle_coordinator_update()` after their
   coordinator listeners are registered. `async_update_listeners()`
   is the right primitive here — `async_set_updated_data()` would
   also unschedule and reschedule the refresh timer (resetting the
   regular poll cadence) and emit HA's "Manually updated" DEBUG log
   line, which misrepresents intent: we are re-fanning the current
   data, not updating it.

This guarantees that:

- Status and health sensors are always visible during outages
- Data sensors appear as soon as the modem becomes reachable
- Deferred entities populate state within 1 second — no Unknown window
  until next scheduled poll
- No duplicate entities — the listener is one-shot
- No leaked listeners — cleanup is automatic

See ORCHESTRATION_USE_CASES.md UC-84 for the full scenario.

---

## Health Coordinator

Second `DataUpdateCoordinator` wrapping `health_monitor.ping()`.
Independent cadence from the data coordinator.

```python
async def _async_update_health() -> HealthInfo:
    return await hass.async_add_executor_job(
        health_monitor.ping
    )

health_coordinator = DataUpdateCoordinator(
    hass,
    logger,
    name=f"Cable Modem {coordinator_label} Health",
    update_method=_async_update_health,
    update_interval=timedelta(seconds=health_check_interval),  # or None
    config_entry=entry,
)
```

**Conditional creation:** Only created if at least one probe works
(discovered during config flow Step 4). When no probes work,
`health_monitor` and `health_coordinator` are None on RuntimeData.

**Independence:** Health probes run on their own timer (default 30s).
The orchestrator reads `health_monitor.latest` during
`get_modem_data()` — no coupling between the two coordinators.
Health sensors update between data polls, giving faster outage
detection.

**Decoupled operation:** Health checks and data collection run
independently — neither suppresses the other. The health monitor
always runs its own probes on its own cadence.

---

## Polling Modes

Data and health intervals are independently configurable. Each can be
scheduled or disabled.

| Data polling | Health check | Coordinator setup |
|-------------|-------------|-------------------|
| Scheduled (default 600s) | Scheduled (default 30s) | Both coordinators with timers |
| Scheduled | Disabled | Data coordinator only, health_coordinator=None |
| Disabled | Scheduled | Data coordinator (no timer), health coordinator with timer |
| Disabled | Disabled | Both coordinators (no timers) |

**"Disabled" means `update_interval=None`** on the DataUpdateCoordinator.
The coordinator still exists (for manual refresh and first poll) but
does not schedule automatic updates.

**Manual trigger:** The "Update Modem Data" button calls
`data_coordinator.async_request_refresh()`. This works regardless of
polling mode — HA's built-in throttling prevents spam.

**Interval limits:**

| Setting | Min | Max | Default |
|---------|-----|-----|---------|
| Data poll interval | 30s | 86400s (24h) | 600s (10m) |
| Health check interval | 10s | 86400s (24h) | 30s |

Configurable via the options flow. Setting to 0 or "Disabled" sets
`update_interval=None`.

---

## Restart Lifecycle

The restart button runs `orchestrator.restart()` on an executor
thread. The Core call is one-shot — authenticate, dispatch the
command, clear the session, trigger the recovery module, return.
Typical duration: 2–5 seconds. Post-reboot polling is handled by
the recovery adapter's cadence switch (see § Recovery Adapter);
the button itself does not observe the reboot.

```text
User presses "Restart Modem"
 │
 ├─ 1. Acquire active_operation = "restart" via hold_active_operation
 │     (refuses if another destructive operation is already running).
 │     No gate on recovery_active — a user who sees a flakey modem
 │     after a restart is allowed to try again.
 │
 ├─ 2. Run in executor:
 │     orchestrator.restart()
 │     (returns in 2–5 s; triggers a recovery window internally)
 │
 ├─ 3. Context manager exits → active_operation cleared. A new press
 │     is accepted from here on; the user may retry if the dashboard
 │     shows a flakey state.
 │
 ├─ 4. Send persistent notification:
 │     success → "Restart Command Sent [MODEL]" / "<model> restart
 │               command dispatched in <N.N> seconds."
 │     failure → "Modem Restart Failed [MODEL]" / "<model> restart did
 │               not dispatch: <error>"
 │
 └─ 5. Trigger one immediate data refresh so the dashboard reacts to
       the post-dispatch state (typically UNREACHABLE while the modem
       reboots). On failure, raise HomeAssistantError after the
       refresh so the caller sees the action fail.
```

**No state writes during the press.** A `ButtonEntity`'s state is
its last-pressed timestamp, and every state write during the press
renders in the logbook as another "Pressed" entry attributed to the
pressing user — the former `_attr_available` grey-out made one press
log three times. The frontend already shows in-flight feedback while
the press service call is pending; overlap protection is the
`active_operation` mutex alone. This matches HA core convention: no
core button platform writes state inside `async_press`.

After step 5, scheduled data polls run at recovery cadence (driven
by § Recovery Adapter) and surface actual modem state — UNREACHABLE
while the modem is down, transitional docsis states while it ranges,
ONLINE once it returns. The dashboard reflects truth throughout; no
synthetic label.

### Sensor Behavior During a Recovery Window

A recovery window is open whenever `orchestrator.recovery_active`
is True, regardless of what triggered it (commanded restart,
observed outage, heuristic).

| Entity category | Behavior |
|-----------------|----------|
| Status sensor | Renders the snapshot's actual status — Operational / Unreachable / Denied / Not Locked / Auth Failed / etc. No synthetic "Restarting…" label. Always available. |
| Health sensors (ICMP, HTTP, health_status) | Independent coordinator. Continue updating on their own cadence — probes naturally report UNRESPONSIVE while the modem is down and recover when the modem does. |
| Data sensors (channel counts, SNR, power, uptime, system_info fields) | Available when the snapshot's `modem_data` is not None. Unavailable when `modem_data is None` (poll failed — typically UNREACHABLE during the reboot itself). This is the same rule as any non-recovery period; no recovery-specific special case. |
| Per-channel sensors | Same as data sensors. |
| Restart button | Always available (never greys out). A second press is refused only while `active_operation == "restart"` (during the ~2–5 s command dispatch). After that a press dispatches again — the user may choose to retry after observing the dashboard. |
| Update Modem Data button | Press is refused when `active_operation` is set. Normal polling at recovery cadence already refreshes the dashboard; extra manual refreshes would be wasted. |
| Reset Entities button | Press is refused when `active_operation` is set. |

Data sensors going Unavailable on poll failure is the honest
reading: the measurement didn't happen. A gap in time-series
history during a reboot is accurate — uptime and channel power
aren't valid while the modem is off. Holding the last reading
would publish false values.

**Unload during a window:** `async_unload_entry` stops the data
coordinator's scheduled polls. The recovery window state lives on
the orchestrator; it continues to tick but has no observable effect
until the next `async_setup_entry`. All state is memory-only — a
fresh setup starts with `recovery_active == False`.

**HA restart during a window:** executor threads die with the
process. The fresh orchestrator on next startup is in a clean state.

See ORCHESTRATION_USE_CASES.md UC-40 through UC-46, UC-49, UC-72,
UC-78, UC-88 for detailed scenarios.

---

## Recovery Adapter

All HA-side recovery wiring lives in `custom_components/cable_modem_monitor/recovery_adapter.py`.
The module owns the cadence constant, the per-entry dispatcher
signal name, and the single setup entry point that installs the
Core observer and the event-loop listener. Other HA modules don't
reference recovery state directly — they read
`orchestrator.recovery_active` or render snapshot truth.

`__init__.py` imports `attach_recovery_cadence_listener` directly
and calls it once during `async_setup_entry`. `coordinator.py`
stays a pure types module (`CableModemRuntimeData` +
`CableModemConfigEntry`); health-sync wiring is a small private
helper in `__init__.py` because it's local to startup and
conceptually separate from Core's recovery observer.

### Health Sync Listeners

`_attach_health_sync_listeners` installs one listener on each
coordinator so neither can outvote the other with stale state.

**Health → data (recovery direction).** Fires an immediate data poll
on a down → up edge of `HealthStatus.data_path_up` — the boolean
"does this reading prove TCP, the data path, is usable" (RESPONSIVE
and ICMP_BLOCKED are up; DEGRADED, UNRESPONSIVE, and UNKNOWN are
down). The edge is computed per reading rather than by enumerating
state transitions: enumeration missed DEGRADED (fixed 2026-06-29) and
then a transitional ICMP_BLOCKED that laundered the down state
(observed 2026-07-12 — UNRESPONSIVE → ICMP_BLOCKED → RESPONSIVE left
Status at Unreachable for a full scan interval). ICMP_BLOCKED counts
as up because since UC-59a every such reading is confirmed by a live
TCP pass; up → up steps (RESPONSIVE ↔ ICMP_BLOCKED) never fire, so
ping-blocking setups get no spurious polls. Consequence worth noting:
post-window recovery latency is bounded by the health-check interval,
so lengthening that interval slows reconnection proportionally.

**Data → health (stale-contradiction direction).** Fires an immediate
health refresh when a poll succeeds (`connection_status == ONLINE`)
while health still reads UNRESPONSIVE or DEGRADED. A completed
collection is live proof the data path is up; without the refresh,
the stale probe result holds the top of the Status cascade for up to
a full health interval, displaying "Unresponsive" over a modem that
is actively serving polls (observed live 2026-07-11: poll recovered
at 10:51:12, Status stale until the 10:53:29 scheduled probe). This
is UC-59a's principle — stale evidence must not outvote a live
signal — applied in the reverse direction. The refresh is cheap: the
fresh collection evidence engages the TCP/HEAD skip gate, so it is an
ICMP-only probe. ICMP_BLOCKED is not contradicted by a successful
poll (TCP was up); UNKNOWN means probes are not applicable, so a
refresh adds no information and would fire on every poll.

**Poll-proven suppression.** The data → health refresh flips health
down → RESPONSIVE, which would trip the recovery direction into
forcing a data poll seconds after the successful poll that started
the exchange — a redundant login on session-limited modems. The data
listener marks the recovery as poll-proven; the next probe result
consumes the mark, suppressing exactly that one forced poll. A
genuine later recovery (the mark was consumed by a still-down probe)
forces the poll as usual.

### Public surface

```python
# recovery_adapter.py

_RECOVERY_POLL_INTERVAL = timedelta(seconds=30)
# Data coordinator cadence while Core's recovery window is open.


def recovery_state_signal(entry_id: str) -> str:
    """Per-entry dispatcher signal name for recovery transitions."""


def attach_recovery_cadence_listener(
    hass: HomeAssistant,
    entry: CableModemConfigEntry,
    orchestrator: Orchestrator,
    data_coordinator: DataUpdateCoordinator[ModemSnapshot],
) -> None:
    """Install the recovery observer and cadence listener.

    Called once during ``async_setup_entry`` (Step 6a). Registers
    an unsubscribe callback on ``entry.async_on_unload``.
    """
```

### Behavior

- On `attach_recovery_cadence_listener()`:
  - Captures `data_coordinator.update_interval` as the "normal"
    cadence (closure local; NOT stored on RuntimeData).
  - Calls `orchestrator.set_recovery_observer(...)` with a
    thread-safe dispatcher send (hops to the event loop via
    `call_soon_threadsafe`).
  - Connects an event-loop listener on
    `recovery_state_signal(entry.entry_id)` that applies the
    cadence switch.
- On `recovery_active` True→False or False→True:
  - Core fires the observer from the poll thread.
  - The dispatcher send hops to the event loop.
  - The listener reads `orchestrator.recovery_active` and switches
    `data_coordinator.update_interval` between
    `_RECOVERY_POLL_INTERVAL` (True) and the captured normal
    cadence (False).
  - On True, also calls `async_request_refresh()` so the first
    fast-cadence poll happens immediately.
- When the captured normal cadence is `None` (user disabled
  polling): the listener is a no-op — we don't override the user's
  explicit opt-out.

### Why in HA and not Core

Core is synchronous and owns no timers. Pushing the "poll faster"
loop to HA's native scheduling keeps Core free of threads and
bounded-latency concerns and gives timer cancellation, reschedule-
on-interval-change, and event-loop safety for free.

### Consumers

Only `__init__.py` imports from `recovery_adapter.py` —
`async_setup_entry` calls `attach_recovery_cadence_listener()`
once during startup. Sensors and buttons do NOT reference the
recovery signal:

- `sensor.py` — reads snapshot state, which already updates via
  the coordinator on every poll (faster during a window, normal
  outside). No recovery signal subscription needed.
- `button.py` — gates only on `active_operation`. Does not read
  `recovery_active` or subscribe to any recovery signal.

The signal is used internally for the cadence listener. Tests
import `recovery_state_signal` directly for dispatcher-level
assertions; no other production module does.

---

## Operation Mutex

The adapter enforces mutual exclusion between destructive buttons
via the `active_operation` field on `RuntimeData`. The field
carries the name of the operation currently running, or `None`
when nothing is active.

Distinct from Core's `recovery_active`:

- `active_operation` gates button presses for the duration of a
  single handler (seconds). It's the only gate on the button.
- `recovery_active` is a cadence signal, not a gate. HA reads it
  to switch the data coordinator's polling interval. The restart
  button does NOT read it — a user who sees a flakey modem mid-
  recovery may legitimately want to retry.

### Concurrency matrix

| Running | Attempted | Behavior |
|---------|-----------|----------|
| — (no active_operation) | restart | Allowed; `active_operation = "restart"` for the handler's duration (~2–5 s). |
| — (no active_operation) | reset | Allowed; `active_operation = "reset"`. |
| — (no active_operation) | refresh (user) | Allowed — runs normally. |
| `active_operation` set | any button | Refused — the press logs a warning and returns without dispatching. |

`recovery_active` has no row because it doesn't participate in
gating. A button press during a recovery window is allowed; the
press goes through `active_operation` like any other.

### Context manager

Set/clear discipline lives in a single helper so both destructive
buttons share one code path.

```python
@contextmanager
def hold_active_operation(
    entry: CableModemConfigEntry,
    op: ActiveOperation,
) -> Iterator[None]:
    runtime = entry.runtime_data
    if runtime is None:
        raise OperationUnavailableError("runtime_data unavailable; entry is unloading")
    if runtime.active_operation is not None:
        raise OperationInProgressError(runtime.active_operation)
    runtime.active_operation = op
    try:
        yield
    finally:
        # Re-read; the entry may have unloaded or reloaded during the body.
        runtime = entry.runtime_data
        if runtime is not None:
            runtime.active_operation = None
```

Guarantees:

- The field is cleared on every exit path — success, exception,
  cancellation — because `finally` runs.
- Cleanup tolerates a concurrent entry unload that clears
  `runtime_data` to `None`.
- Uses `contextmanager` (not `asynccontextmanager`) because the
  set/clear itself is synchronous; the body is where awaits happen.
- No dispatcher signal fired from the mutex — `active_operation`
  is short-lived (seconds) and the buttons that read it don't need
  a signal (they only read the field when the user interacts with
  them). Core's `recovery_state_signal` handles the longer-lived
  window transitions.

### Diagnostics

`active_operation` is surfaced in the diagnostics download so a
stuck-state report is self-diagnosing. If the field is non-None
despite no handler actually running, the field's string value
identifies which code path left it set. `recovery_active` and the
recovery window's elapsed time are also exposed for the same
reason.

### Acceptance

The `active_operation` field and the `hold_active_operation` helper
are adapter-layer only — they gate destructive *buttons* for their
runtime (seconds). That is the ONLY button gate.

`orchestrator.recovery_active` is Core-scoped state set by the
recovery module when a window is open (from any trigger: command,
observed outage, reboot-signal match). Core's recovery observer fires the
dispatcher signal named by `recovery_state_signal(entry_id)` on
transitions. HA consumes it in one place only: the cadence listener
installed by `attach_recovery_cadence_listener` in `recovery_adapter.py`,
which drops the data coordinator's `update_interval` while a window
is open. Nothing else subscribes.

Core doesn't know *what* the observer does — it just invokes a
callable — which keeps the layering one-directional. No HA-side
component reads `recovery_active` for UX purposes: sensors render
snapshot truth, the button gates on the short-lived
`active_operation` mutex, and the user is trusted to decide when to
retry based on what the dashboard shows.

---

## Reset Entities Concurrency Guard

The Reset Entities button tears down and re-creates data-dependent
entities to pick up new channel IDs after a modem reboots to a
different channel set (UC-80). A second click while the first is
still running can fire `_handle_coordinator_update()` against
already-unloaded entities — observed symptom is an `AttributeError`
on `entry.runtime_data` after the entry is partially torn down.

The Reset button uses the shared `hold_active_operation` context
manager (see § Operation Mutex) for its mutex discipline. No state
writes inside the handler — same logbook rule as § Restart
Lifecycle:

```python
async def async_press(self) -> None:
    try:
        with hold_active_operation(self._entry, "reset"):
            # reset body — re-detect probes, remove entities,
            # update config entry, reload, notify
            ...
    except OperationInProgressError as err:
        _LOGGER.warning(...)
        return  # another destructive operation is running
```

Two defences, both required:

1. **`active_operation` check at entry** (via the context manager) —
   a second click while any destructive operation is running refuses
   immediately. The field lives on `RuntimeData`, not the button
   instance, so it survives the temporary teardown of data-dependent
   entities during reset.
2. **Null-safety** — the context manager re-reads `runtime_data` on
   exit because `async_unload_entry` can fire concurrently (and the
   reset body itself reloads the entry, replacing `runtime_data`).
   The reset flow must not assume the entry it started with is still
   loaded when it returns.

---

## Reauth Flow

When Core's auth circuit breaker opens, the adapter triggers HA's
native reauthentication flow. Two mechanisms open it: `AUTH_FAILED` and
`AUTH_LOCKOUT` trip it on the first occurrence, while `LOAD_AUTH` and
`LOAD_INTEGRITY` accumulate and trip it at 6 consecutive failures
(`ORCHESTRATION_SPEC.md` § Auth Circuit Breaker). The policy collapses
all four signals to `AUTH_FAILED`, so the adapter reads
`circuit_trip_signal` to tell the routes apart (UC-81a).

**`AUTH_LOCKOUT` is routed away from this flow.** Submitting the reauth
form calls `validate_connection`, a real login, and the config flow is
stateless by design (UC-86), so an open breaker does not stop it.
Prompting a locked-out user invites hand-fed attempts at a modem that
locked itself to stop them (#117). That route fires a persistent
notification instead — `_notify_auth_lockout`, once per open breaker,
with the wait-then-reload remedy and no mention of credentials. The
Status sensor remains the standing indicator.

The three remaining routes reach the flow below.

```text
Circuit breaker opens
 │
 ├─ 1. get_modem_data() returns AUTH_FAILED with circuit_breaker_open
 │
 ├─ 2. Adapter detects: snapshot.connection_status == AUTH_FAILED
 │     AND orchestrator.diagnostics().circuit_breaker_open == True
 │     AND circuit_trip_signal is not AUTH_LOCKOUT
 │
 ├─ 3. Trigger: entry.async_start_reauth(hass)
 │     HA shows "Reauthentication required" notification
 │
 ├─ 4. User enters new credentials via async_step_reauth
 │     (reuses Step 3 connection form — host + credentials)
 │
 ├─ 5. Validation runs in executor
 │     (connectivity + auth + parse — same pipeline used by setup
 │     and options flows; the collector emits a single WARNING with
 │     the modem's response on auth failure — see ORCHESTRATION_SPEC
 │     § Auth-Failure Detail Log)
 │
 ├─ 6. On success:
 │     ├─ Update config entry with new credentials
 │     ├─ Reload the entry — a fresh orchestrator is built with the
 │     │   new credentials (streak, circuit, backoff, session all
 │     │   start at constructor defaults; see UC-16)
 │     └─ Next poll attempts fresh login
 │
 └─ 7. On failure: show error, user retries
```

**No polling while circuit is open.** `get_modem_data()` returns
`AUTH_FAILED` immediately when the circuit breaker is open. The user
must fix credentials (or reload the integration) before polling
resumes. Manual refresh deliberately does not bypass the breaker —
see ORCHESTRATION_SPEC § Auth Circuit Breaker, "No manual bypass."

See ORCHESTRATION_USE_CASES.md UC-81 for the full scenario.

---

## Diagnostics Platform

The `diagnostics.py` module implements HA's diagnostics download.
Combines Core's `OrchestratorDiagnostics` with HA-side context.

**From Core (`orchestrator.diagnostics()`):**

Serialized via `OrchestratorDiagnostics.to_dict()` — all fields
included automatically when new diagnostics are added to the model.

- `poll_duration` — last poll wall-clock time in seconds
- `auth_failure_streak` — consecutive auth failures (0 = healthy)
- `circuit_breaker_open` — whether polling is stopped
- `circuit_trip_signal` — which signal opened the breaker (null while
  closed). The blocked polls that follow a trip report `ok`, so this
  is the only field in the download that says why polling stopped
- `session_is_valid` — auth manager session state
- `auth_strategy` — auth strategy name from modem config
- `connectivity_streak` — consecutive connectivity failures
- `connectivity_backoff_remaining` — polls to skip before retry
- `stale_session_recovery_streak` — consecutive recovered stale-session events
- `session_reuse_disabled` — whether cached-session reuse is off for this runtime
- `resource_fetches` — per-resource timing and size from last
  successful collection (path, duration_ms, size_bytes per resource)
- `last_poll_at` — ISO 8601 wall-clock timestamp (UTC) of last poll
- `last_stub_body` — response bodies from the last stub-page event,
  keyed by resource path (empty if none has occurred)
- `system_info_fields_missing` — mapped system_info fields the modem
  never sent on the most recent parse (PARSING_SPEC § Field Outcomes)
- `system_info_fields_failed` — mapped fields whose value type
  conversion rejected, with the raw value (truncated); retained for
  the runtime so intermittent failures stay visible

**Auth-failure detail surfaces in `recent_logs`.** When auth fails,
the collector emits a single sanitized ``WARNING`` log carrying
the modem's response (see ORCHESTRATION_SPEC.md § Auth-Failure
Detail Log). That line is included in the ``recent_logs`` block
of the diagnostics download alongside other Core/HA log records —
no separate auth-capture key, no on-demand round-trip to the modem
during diagnostics build.

**From HA (adapter-side):**

- PII review checklist
- Sanitized recent logs from both the HA adapter
  (`custom_components.cable_modem_monitor`) and Core package
  (`solentlabs.cable_modem_monitor_core`) loggers
- Runtime state summary (`modem_data` — connection + health only)
- Full `system_info` pass-through (all parser-extracted and computed fields)
- Full channel dump (`downstream_channels` + `upstream_channels`)
- Config entry details (host, protocol, supports_icmp, etc.)
- Coordinator state (last_update_success, update_interval)
- `active_operation` field — surfaces a stuck mutex in user reports
- Recovery window state — `recovery_active`, `recovery_reason`, and
  window elapsed seconds so a stuck-fast-poll report is
  self-diagnosing

**Sanitization:**

- File paths and RFC 1918 private IPs scrubbed in `recent_logs` and
  `last_error` blocks (preserves the modem gateway IP). Other
  blocks (`system_info`, channel dumps) pass through verbatim —
  the PII checklist is the primary safety net.
- PII checklist warns the user to verify before sharing

**No raw HTML capture.** Use `har-capture` for collecting raw modem
data for parser development.

### Diagnostics Top-Level Keys

The diagnostics output disassembles Core's nested `modem_data` dict
into separate top-level keys. The boundary between sections is
**source**: `modem_data` draws from snapshot and health evaluations
(orchestrator-derived state), while `system_info`, `downstream_channels`,
and `upstream_channels` are verbatim pass-throughs of Core's parser
output. The diagnostics builder never copies values from `system_info`
into `modem_data`.

| Key | Contents | Source |
|-----|----------|--------|
| `config_entry` | Host, protocol, model, credentials flag | HA config entry |
| `core_diagnostics` | Poll timing, auth state, circuit breaker | `orchestrator.diagnostics()` |
| `data_coordinator` | Last success, update interval | HA coordinator |
| `health_coordinator` | Last success, update interval | HA coordinator (conditional) |
| `modem_data` | Evaluated connection + health state | Snapshot + health probe |
| `system_info` | All parser-extracted and computed fields | `snapshot.modem_data["system_info"]` pass-through |
| `downstream_channels` | Per-channel data (sparse dicts) | `snapshot.modem_data["downstream"]` pass-through |
| `upstream_channels` | Per-channel data (sparse dicts) | `snapshot.modem_data["upstream"]` pass-through |

### `modem_data` — Evaluated State

The `modem_data` key contains **orchestrator-derived connection state
and health probe results**. Every field comes from `snapshot.*` or
`health_info.*` — evaluated assessments of modem reachability, not
raw parser output.

Modem identity (version, model), counters (error totals), and
measurements (channel counts, uptime) belong in `system_info` —
they are Core parser output, not HA-layer evaluations.

#### Connection State (from snapshot, enums converted to string values)

| Field | Type | Source |
|-------|------|--------|
| `connection_status` | string | `snapshot.connection_status` |
| `collector_signal` | string | `snapshot.collector_signal` |
| `error` | string | `snapshot.error` (empty on success) |

#### Health State (prefers health coordinator over snapshot)

| Field | Type | Source |
|-------|------|--------|
| `health_status` | string | Health probe result (`"none"` if unavailable) |
| `icmp_latency_ms` | float or null | ICMP round-trip (null if not supported) |
| `tcp_latency_ms` | float or null | TCP handshake to web port (null if HTTP probe disabled or skipped) |
| `http_latency_ms` | float or null | HTTP HEAD server response time (null if `supports_head=False`, HEAD failed, or skipped) |

### `system_info` — Parser Output

Verbatim pass-through of Core's `system_info` dict. Contains parser-
extracted fields, coordinator-computed counts, and aggregated totals.
This is the single source of truth for modem identity, counters, and
status.

Fields vary by modem (sparse dict). Common fields include:

| Field | Type | Origin |
|-------|------|--------|
| `downstream_channel_count` | int | Coordinator-computed (always present) |
| `upstream_channel_count` | int | Coordinator-computed (always present) |
| `total_corrected` | int | Aggregate or native (see PARSING_SPEC § Aggregate) |
| `total_uncorrected` | int | Aggregate or native (see PARSING_SPEC § Aggregate) |
| `docsis_status` | string | Parser-extracted or orchestrator-enriched (see below) |
| `software_version` | string | Parser-extracted |
| `system_uptime` | string | Parser-extracted |
| `model_name` | string | Parser-extracted (when available) |
| `hardware_version` | string | Parser-extracted (when available) |

#### `docsis_status` enrichment

`docsis_status` follows the same enrichment pattern as error totals:
the parser provides it when the modem exposes a native value, and the
orchestrator fills it in from channel `lock_status` when absent. If
neither the parser nor the orchestrator can determine the value (no
native field, no `lock_status` on channels), the field stays absent
in `system_info` — same sparse-dict rule as other fields. No sensor
is created.

1. **Parser provides it** — YAML `map` entries normalize vendor values
   to the canonical `"Operational"` (see SYSTEM_INFO_SPEC § Canonical
   Values). Non-mapped values pass through as raw diagnostic strings
   (e.g., `"Ranging"`).

2. **Parser does not provide it** — the orchestrator derives it from
   downstream channel `lock_status` fields and writes it into
   `system_info`. See RUNTIME_POLLING_SPEC § Status Derivation for
   the derivation rules (including when derivation is not possible).

One field, one location in the data layer. `snapshot.docsis_status`
reads from `system_info["docsis_status"]`, falling back to `"unknown"`
when the field is absent (used internally by the HA status cascade,
not exposed as a sensor).

---

## Event Bus

After every poll (success or failure), the data coordinator fires:

```text
cable_modem_monitor_data_updated
```

CMM fires unconditionally — it does not know or care whether any consumer is
listening. Data never leaves the user's HA instance unless a subscriber
(e.g., CMMT) explicitly transmits it.

### Listening in Developer Tools

Developer Tools → Events → enter `cable_modem_monitor_data_updated` →
Start Listening. The next poll will show the full payload.

The raw event from Developer Tools includes HA-injected wrapper fields
(`origin`, `time_fired`, `context`) that are not part of the CMM
payload. The CMM payload is the `data:` block only. `context.user_id`
and `context.parent_id` are always null — CMM fires unconditionally on
the poll cycle, not in response to a user action.

### Payload schema

Defined in `SnapshotEventPayload` in
`cable_modem_monitor_core.orchestration.event_payload`. Consumers validate
with:

```python
from solentlabs.cable_modem_monitor_core.orchestration import SnapshotEventPayload

payload = SnapshotEventPayload.model_validate(event.data)
```

### Payload shape

```yaml
schema_version: 1         # increment on breaking changes
connection_status: online # ConnectionStatus enum value
docsis_status: Operational  # present even when modem_data is null (failure case)
collector_signal: ok      # CollectorSignal enum value
error: ""                 # human-readable error, empty on success
stats_last_reset: null    # ISO 8601 datetime or null

health_info:              # null if no health monitor configured
  health_status: responsive
  icmp_latency_ms: 3.003
  tcp_latency_ms: 4.128
  http_latency_ms: null   # non-null only when supports_head=True and no recent collection

modem_data:               # null on collection failure
  downstream:
    - channel_number: 1
      lock_status: locked
      modulation: QAM256
      channel_id: 21
      frequency: 237000000
      power: -0.2
      snr: 41.1
      corrected: 0
      uncorrected: 0
      channel_type: qam
    # ... one entry per downstream channel
  upstream:
    - channel_number: 1
      lock_status: locked
      channel_type: atdma
      channel_id: 1
      frequency: 17600000
      power: 37.2
    # ... one entry per upstream channel
  system_info:
    system_uptime: 1 days 05h:37m:15s
    hardware_version: V1.0
    software_version: 7621-5.7.1.5
    downstream_channel_count: 24
    upstream_channel_count: 4
    total_corrected: 0
    total_uncorrected: 0
    rate_corrected: 0
    rate_uncorrected: 0
    # modem-specific fields may appear here; see FIELD_REGISTRY.md
```

Channel fields are sparse — not all modems report all fields. All channel
fields except `channel_number` are optional. `system_info` keys vary by
modem; see FIELD_REGISTRY.md § system_info for the baseline set.

### PII

CMM fires the full snapshot. Consumers are responsible for stripping PII
before any external transmission. CMM does not collect identity PII
(`mac_address`, `serial_number`) — no parser extracts them and the intake
mapping skips them (see SYSTEM_INFO_SPEC § Tiered Sensor Model). The global
denylist
(`packages/cable_modem_monitor_catalog/scripts/data/pii_fields_global.json`)
retains them as defensive defaults, and per-modem additions are declared in
`pii_fields` in each modem's `modem.yaml`, so the strip contract still holds
if a future field surfaces PII.

---

## Services

All services are registered once in `async_setup` (integration-global,
Bronze `action-setup`) — not per config entry. They exist before any
entry is configured and survive an entry being removed and re-added, so
they are never unregistered. Because a service call can arrive with no
loaded entry (or target a specific one), every handler resolves its
target config entry at call time via `_resolve_target_entries` /
`_resolve_config_entry_for_device`; none capture an entry at
registration.

**Failures raise; they are never returned.** A condition the user can
fix — no modem configured, no target entry, the modem offline when the
action needs live data — raises `ServiceValidationError`. An operation
that was attempted and failed raises `HomeAssistantError`; the restart
button also keeps its persistent notification as the durable record, and
raises only after refreshing the coordinator so the dashboard still
reflects current state. Returning an error dict, or smuggling an error
string back in a result field, is not a valid failure path (Silver
`action-exceptions`).

Distinguish this from legitimate empty results, which are returned
normally and are not errors: "no statistics to migrate", "no orphaned
statistics found", and the orphaned-statistics preview listing are all
successful outcomes.

### `generate_dashboard`

Generates Lovelace YAML for a complete modem dashboard based on
current channel data.

**Input options (UI form — `services.yaml`):**

- `device_id` (optional) — which modem to generate for. Defaults to
  first configured modem when omitted.
- Which graphs to include (DS power, DS SNR, DS frequency, US power,
  US frequency, errors, error rates, latency, status card)
- Graph timespan (hours)
- `short_titles` — a single global readability toggle for card titles
- `status_card_exclude` — pass-through fields to drop from the status card

**YAML-only options (accepted by the schema, off the UI form by
design):** `channel_label` (`auto` / `full` / `id_only` / `type_id`) and
`channel_grouping` (`by_direction` / `by_type`) rearrange and rename
lines *within* channel graphs. The form is for content selection (which
cards and graphs you get); these fine cosmetics are left to hand-editing
the emitted YAML. `short_titles` is the one formatting toggle kept on the
form, as a single global readability preference.

**How it works:**

1. Resolves target modem from `device_id` or falls back to first entry
2. Reads current channel data from `entry.runtime_data.data_coordinator`
3. Generates entity references for actual channels
4. Returns YAML string the user pastes into a manual dashboard card

### `request_refresh`

Triggers an immediate modem data poll, bypassing connectivity backoff.
Intended for automations that need on-demand polling (e.g., "ping
fails → trigger modem check").

**Fields:** Optional `device_id` (device selector filtered to
`cable_modem_monitor`). Falls back to all loaded entries when no
device is specified.

**Behavior:**

1. Resolve device_id to config entry
2. Short-circuit if `runtime.active_operation is not None` — a
   restart or reset is already running and will trigger its own
   post-operation refresh; no user action is needed
3. Call `orchestrator.reset_connectivity()` to clear backoff — this
   deliberately does not touch the auth circuit breaker; this
   service is an automation surface, and a breaker bypass would let
   a retry loop post known-bad credentials (see ORCHESTRATION_SPEC
   § Auth Circuit Breaker, "No manual bypass")
4. Refresh health coordinator (if health monitoring is enabled)
5. Refresh data coordinator

Same logic as the "Update Modem Data" button — both use the shared
`async_request_modem_refresh()` helper to stay DRY, including the
`active_operation` gate. Internal refreshes (post-restart step 6,
health sync listeners) call the coordinator directly rather than
going through the helper.

**Automation example:**

```yaml
automation:
  trigger:
    - platform: state
      entity_id: binary_sensor.ping_gateway
      to: "off"
      for: "00:01:00"
  action:
    - service: cable_modem_monitor.request_refresh
      data:
        device_id: <modem_device_id>
```

### `request_health_check`

Triggers an immediate health check (ICMP + TCP probes, plus HEAD if supported).

**Fields:** Optional `device_id` (device selector filtered to
`cable_modem_monitor`). Falls back to all loaded entries when no
device is specified.

**Behavior:**

1. Resolve device_id to config entry
2. If health monitoring is enabled, refresh health coordinator
3. If health monitoring is disabled, log a warning and return

**Automation example:**

```yaml
automation:
  trigger:
    - platform: state
      entity_id: binary_sensor.ping_gateway
      to: "off"
  action:
    - service: cable_modem_monitor.request_health_check
      data:
        device_id: <modem_device_id>
    - delay: "00:00:30"
    - service: cable_modem_monitor.request_refresh
      data:
        device_id: <modem_device_id>
```

---

## Notifications

A persistent notification is warranted only when the user has something to act
on, or to confirm an action they just took. Before adding one, name which of
the two it is. If it is neither, use an entity or the
`cable_modem_monitor_data_updated` event instead, which automations can consume
without interrupting anyone.

The integration raises five. The auth lockout asks you to fix your credentials.
Restart sent, restart failed and entity reset complete each confirm a button
press. The onboarding message confirms the modem came online after you added
it, and fires once per config entry. Channel bond totals changing is neither of
those, and the totals are already on the DS and US Channel Count sensors, which
is why it is being removed in 3.14.1 (#197).

### Channel bond onboarding

The data coordinator fires one persistent notification per config entry, the
first time a successful poll reports channel totals. It points the user at
`generate_dashboard` so they can build a Lovelace dashboard for the modem.

| Trigger | When it fires | Notification ID |
|---------|---------------|-----------------|
| Onboarding | First successful poll after setup | `cable_modem_monitor_onboarding_{entry_id}` |

Stable IDs per entry mean re-firing replaces rather than stacks.

**Persisted state:**

- `CONF_CHANNEL_ONBOARDING_ELIGIBLE` on **entry data** — set to
  `True` by the config flow on fresh setup; absent on upgraded
  entries. Written once at create time (before listeners exist) and
  never mutated afterwards, so it doesn't trigger the integration's
  update listener reload.
- Baseline totals live in a dedicated `homeassistant.helpers.storage.Store`
  (`channel_bond_storage.py`) keyed by entry ID. The Store payload is
  `{baseline_downstream: int, baseline_upstream: int}`. Presence of a
  Store payload means the entry has already been onboarded (fresh) or
  silently baselined (upgrade); absence means the next successful poll
  should run first-time logic. The totals themselves are no longer
  compared — presence is what carries the meaning.

**Why Store, not entry data.** The integration's update listener
reloads the integration on **any** entry-data mutation, so per-entry
state that changes at runtime cannot live there. The Store helper
persists it without tripping listeners.

**Suppression rules:**

- **Zero-totals guard (primary).** A `(0, 0)` reading means "no data
  yet" — a booting or `no_signal` page returns empty channel tables,
  not a real bond. The notifier returns `none` and the coordinator
  never persists it as a baseline. An operational modem always reports
  channels, so a zero total is never a legitimate reading. This is the
  primary guard because it holds regardless of outage length:
  `recovery_active` is time-boxed, and a real outage can outlive the
  window, at which point a transient `0` would otherwise be stored as
  the baseline.
- **Recovery guard (secondary).** Skipped while
  `orchestrator.recovery_active` is `True`.

**Upgrade path:** entries created before this feature lack
`CONF_CHANNEL_ONBOARDING_ELIGIBLE`. On first post-upgrade poll the
coordinator silently writes the current totals to the Store — no
retroactive onboarding notification.

**Cleanup.** `async_remove_entry` in `__init__.py` removes the Store
payload when the config entry is deleted.

**Removed in 3.14.1 — the change notification.** Bonded totals differing
from the stored baseline used to fire a second notification. It watched
the wrong value in both directions: a DOCSIS 3.1 OFDM carrier that
briefly drops and returns cost two notifications for a single poll of
missing data, while an ID-mode channel reassignment — the case it was
built for — can leave the totals unchanged and go undetected. Defaulting
identity mode to channel number was the real fix for the latter. See
#197.

**Pure logic lives in `channel_bond_notifier.py`** (no HA imports), so
the decision tree is unit-testable without mocks. The coordinator owns
the HA integration: reading `system_info`, reading
`orchestrator.recovery_active`, persisting the Store payload, and calling
`persistent_notification.create`.

## Config Entry Migration

Config entries evolve as the integration adds features and
restructures data.  Without migration, entries created by older
versions crash on startup because `async_setup_entry` expects keys
that don't exist.

**HA mechanism:** Config flows declare a `VERSION` class attribute.
When HA loads an entry whose stored version is lower than the current
`VERSION`, it calls `async_migrate_entry(hass, entry)` before
`async_setup_entry`.  The migration function transforms the entry
data and returns `True` (success) or `False` (failure — entry won't
load, user must reconfigure).

**Current version:** 2

**Design: auto-discovered migration registry.**

The `migrations/` directory uses convention-based discovery.  Drop a
file named `v{N}_to_v{M}.py` that exports `async_migrate(hass, entry)
-> bool`.  The registry discovers it automatically at import time —
no manual registration needed.  Migrations must be sequential
(M = N + 1).

`async_migrate_entry` walks the chain from the stored version to the
current version, applying each handler in sequence:

```text
stored v1 → v1_to_v2.async_migrate() → v2_to_v3.async_migrate() → current v3
```

Adding a future migration = one file.  No changes to dispatch logic.

**v1 → v2 key mapping:**

| v1 key | v2 key | Transform |
|--------|--------|-----------|
| `detected_manufacturer` | `manufacturer` | Rename |
| `detected_modem` | `model` | Strip manufacturer prefix |
| `detected_modem` | `user_selected_modem` | Copy as display name |
| `working_url` | `protocol` | Parse URL scheme; fallback: `legacy_ssl` → `"https"`, else `"http"` |
| `host` | `host` | Unchanged |
| `username` | `username` | Unchanged |
| `password` | `password` | Unchanged |
| `legacy_ssl` | `legacy_ssl` | Unchanged |
| `supports_icmp` | `supports_icmp` | Unchanged |
| `supports_head` | `supports_head` | Unchanged (default `false` if missing) |
| `entity_prefix` | `entity_prefix` | Unchanged (default `"none"` if missing) |
| `scan_interval` | `scan_interval` | Unchanged |
| — | `modem_dir` | Catalog lookup: manufacturer + model → relative directory path |
| — | `variant` | Default: `null` |
| — | `health_check_interval` | Default: 30 |

**`modem_dir` resolution:** The migration walks the catalog, reads
each `modem.yaml` for manufacturer and model names, and builds a
lookup table.  The v1 manufacturer and extracted model are matched
against this table (case-insensitive, including `model_aliases`).
The result is a relative path from the catalog root (e.g.,
`"arris/sb8200"`).  All config entry path construction uses
`modem_dir` — never manufacturer/model strings directly.

**Graceful failure:** If catalog lookup fails (modem removed or
manufacturer renamed beyond recognition), migration logs a warning
with the original values and returns `False`.  HA marks the entry as
failed — the user reconfigures through the setup wizard.

**v1 keys removed:** `parser_name`, `detected_manufacturer`,
`detected_modem`, `modem_choice`, `working_url`,
`parser_selected_at`, `docsis_version`, `actual_model`,
`auth_strategy`, `auth_form_config`, `auth_hnap_config`,
`auth_url_token_config`, `auth_discovery_status`,
`auth_discovery_failed`, `auth_discovery_error`, `auth_type`,
`auth_captured_response`.

---

## Testing

The adapter is modem-agnostic — its tests must be too.

**No modem-specific names.** Mock data uses generic names (`Solent
Labs`, `TPS-2000`, `TPS-3000`) — not real manufacturers or models. This
applies to all mock fixtures: entry data, catalog summaries, modem
identity, config flow selections, diagnostics titles, log messages.

**Mock at the Core/Catalog boundary.** Adapter tests mock the I/O
boundary (`load_modem_config`, `load_parser_config`,
`load_post_processor`, `list_modems`) and test wiring logic:
path dispatch, conditional construction, error propagation.
Do not parametrize over real catalog modems — that crosses the
layer boundary into catalog testing.

**Catalog tests own "every modem works."** The catalog test suite
(`test_modem_yaml_schema`, `test_modem_har_replay`) validates that
every modem config is valid and produces correct output through the
full orchestrator cycle. The adapter layer does not repeat this.

**Migration tests verify schema, not modem data.** Config entry
migration tests verify the key transform (v1 keys → v2 keys with
correct types and defaults). Use generic names for migration test
data. Catalog resolution algorithms (`resolve_modem_dir`) are tested
with synthetic `ModemSummary` data — not the real catalog.

---

## Module Inventory

The HA adapter layer consists of these modules:

| Module | Responsibility |
|--------|---------------|
| `__init__.py` | Component setup (`async_setup` service registration), entry startup/unload, migration dispatch, device registry, `async_remove_entry` cleanup |
| `coordinator.py` | `CableModemRuntimeData` dataclass + `CableModemConfigEntry` type alias |
| `recovery_adapter.py` | Recovery cadence listener — observer into Core + dispatcher signal that flips `update_interval` while a window is open |
| `mapping_manager.py` | Channel identity mapping (`ChannelMap`) — builds per-poll mapping between channel number/id and entity unique_id |
| `channel_bond_notifier.py` | Pure logic for channel-bond change detection — selects `NotifierAction` given totals, stored baseline, and recovery state |
| `channel_bond_storage.py` | Store-backed persistence for channel-bond baseline totals — per-entry load / save / remove |
| `sensor.py` | Entity classes for all sensor types |
| `button.py` | Restart, Update, Reset Entities buttons |
| `config_flow.py` | Setup wizard and options flow |
| `config_flow_helpers.py` | Validation pipeline, probe detection, encoding detection — async wrappers around Core I/O |
| `diagnostics.py` | Diagnostics download combining Core + HA-side data |
| `const.py` | Domain constants, config keys, defaults |
| `services.py` | Service registration wiring — constants, schemas, `async_request_modem_refresh`, `request_refresh` / `request_health_check` handler factories, `async_register_services` |
| `dev_tools.py` | Dev tool implementations — dashboard YAML generator (`_build_*` / `_format_*` / channel helpers / `create_generate_dashboard_handler`) and channel identity converter (`_plan_stat_renames_*` / `_migrate_statistics` / `create_convert_channel_identity_handler`) |
| `migrations/` | Version-keyed config entry migration handlers |
| `core/log_buffer.py` | Log capture for diagnostics (HA adapter + Core package loggers) |
| `lib/host_validation.py` | URL building, host input parsing |
| `lib/utils.py` | Utility functions (e.g., uptime parsing) |

All modem-specific logic lives in Core and Catalog. The adapter
imports from `solentlabs.cable_modem_monitor_core` and
`solentlabs.cable_modem_monitor_catalog` — never from modem config
files or parser code directly.

---

## Distribution

See [RELEASING.md](../../../docs/reference/RELEASING.md) for package
architecture, install flow, version pinning, release tiers, and
rollback safety.
