# Entity Model Specification

How the HA integration maps Core output to Home Assistant entities.

Core produces `ModemData` (channels + system_info) and health-check
results (latency + status). This spec defines how those map to HA
platforms, entities, attributes, and availability.

**Design principle:** The integration exposes a vendor-neutral
schema of metrics observed across the cable-modem fleet, in standard
HA format. Core admits derived fields when they re-present a
fleet-observed datum more directly (e.g., per-minute error rates
from cumulative FEC counters) or fill in cardinality the fleet does
not always report natively (e.g., channel counts). User-side
analytics — spreads, deltas, composed health grades, threshold-based
classification — are out of scope for Core and the HA adapter; they
belong in HA blueprints distributed alongside the integration. See
[ARCHITECTURE_DECISIONS.md § Core's schema tracks fleet-observed
metrics](../../../packages/cable_modem_monitor_core/docs/ARCHITECTURE_DECISIONS.md#cores-schema-tracks-fleet-observed-metrics-not-user-analytics)
for the full decision and
[BLUEPRINT_DISTRIBUTION_SPEC.md](BLUEPRINT_DISTRIBUTION_SPEC.md) for
the user-analytics distribution path.

---

## Contents

| Section | What it covers |
|---------|----------------|
| [Platforms](#platforms) | Which HA platforms are used and why |
| [Entity Catalog](#entity-catalog) | Every entity the integration creates |
| [Channel Identity](#channel-identity) | How channels are keyed and indexed |
| [Field Pass-Through](#field-pass-through) | How parser fields flow to HA |
| [Core vs HA Boundary](#core-vs-ha-boundary) | What lives where |
| [Device Model](#device-model) | HA device registration and naming |
| [Availability](#availability) | When entities are available/unavailable |
| [Entity Naming](#entity-naming) | unique_id and entity_id patterns |

---

## Platforms

| Platform | Purpose |
|----------|---------|
| `sensor` | All modem data — metrics, counters, text, timestamps |
| `button` | Actions (restart, update, reset) |

No `binary_sensor`, `select`, `switch`, `number`. Modems are
read-only data sources. Boolean fields (`lock_status`) and enum
fields (`network_access`) are exposed as attributes on sensor
entities. Users create template entities from attributes if needed.

---

## Entity Catalog

**Entity categories.** The device page keeps its primary group to the
signal readout — the Status sensor and every per-channel sensor
(power, SNR, frequency, corrected/uncorrected). Everything else is
supporting information and carries `EntityCategory.DIAGNOSTIC`: Modem
Info, Software Version, DS/US channel counts, error totals and rates,
Last Boot Time, system-info pass-through fields, LAN statistics, the
latency sensors, and the Update Modem Data button. Reset Entities is
`CONFIG`. Diagnostic classification is applied on the shared bases
(`_SystemInfoSensor`, `HealthSensorBase`) where the taxonomy already
groups them, not per entity.

### System Sensors

These are created when the corresponding data appears in parser
output (some gated by config).

| Entity | unique_id suffix | State | device_class | state_class | Unit | Attributes |
|--------|-----------------|-------|--------------|-------------|------|------------|
| Status | `_status` | display¹ | — | — | — | `connection_status`, `health_status`, `docsis_status`, `diagnosis` |
| Modem Info | `_info` | detected model | — | — | — | `manufacturer`, `model`, `release_date`, `docsis_version`, `status` (from Core's `ModemIdentity`) |
| Software Version | `_software_version` | string⁶ | — | — | — | — |
| Last Boot Time | `_last_boot_time` | datetime⁶ | TIMESTAMP | — | — | Held steady across poll jitter⁵ |
| DS Channel Count | `_downstream_channel_count` | int | — | MEASUREMENT | — | From `system_info` — native or coordinator-computed² |
| US Channel Count | `_upstream_channel_count` | int | — | MEASUREMENT | — | From `system_info` — native or coordinator-computed² |
| Total Corrected | `_total_corrected` | int | — | TOTAL_INCREASING | — | From `system_info` — native or coordinator-computed² |
| Total Uncorrected | `_total_uncorrected` | int | — | TOTAL_INCREASING | — | From `system_info` — native or coordinator-computed² |
| Rate Corrected | `_rate_corrected` | float | — | MEASUREMENT | errors/min | From `system_info` — orchestrator-computed inter-poll delta⁴ |
| Rate Uncorrected | `_rate_uncorrected` | float | — | MEASUREMENT | errors/min | From `system_info` — orchestrator-computed inter-poll delta⁴ |
| System Info Field³ | `_{field}` | pass-through⁶ | — | — | — | Dynamic per-field sensor for non-consumed system_info fields |

¹ See [Status Sensor](#status-sensor) for the priority cascade that
produces the display state.

² Channel counts and error totals appear in `modem_data.system_info`.

³ Dynamic sensor created for each `system_info` field not consumed by
a dedicated sensor class above. One `SystemInfoFieldSensor` class
handles all dynamic fields, parameterized by field name. Created only
when the field is present in parser output. See § Field Pass-Through.
Modems with native values have them mapped in parser.yaml. Modems
without native values declare derivation rules in parser.yaml's
`aggregate` section — the parser coordinator computes them from
channel data. Channel counts (`len(channels)`) are always computed
by the parser coordinator if not natively mapped. Consumers read from one
place regardless of source. See
[PARSING_SPEC.md](../../../packages/cable_modem_monitor_core/docs/PARSING_SPEC.md#aggregate-derived-system_info-fields).

⁴ Error-rate sensors are gated by SC-QAM **capability** (the
presence of `total_corrected` in `system_info`), not by immediate
rate-field presence. HA's data-dependent entity creation is one-shot
at first-data-available, and the orchestrator deliberately omits
`rate_corrected` / `rate_uncorrected` on the first poll (no prior
baseline), across counter resets, and when monotonic elapsed time
is non-positive. Gating on rate-field presence would leave the
sensors permanently absent. Instead, the rate sensors are created
alongside the totals; `native_value` returns `None` (HA renders
`unknown`) on polls where the orchestrator omits the field. See
[ORCHESTRATION_SPEC.md § Derived Fields](../../../packages/cable_modem_monitor_core/docs/ORCHESTRATION_SPEC.md#derived-fields).

⁵ Last Boot Time derives from `now − system_uptime` (or, for modems
without native uptime, the orchestrator's counter-reset detection).
Both sources jitter: poll timing shifts the computed timestamp a few
seconds every cycle, which would write a new recorder row per poll
and change the value on every HA restart (#178). The sensor therefore
holds its last emitted value and only replaces it on reboot
evidence: an uptime decrease since the previous poll (uptime is
monotonic within a boot, so a lower reading is a reboot regardless
of how close the timestamps land — catches double reboots inside
the tolerance), or a computation that moves by more than 5 minutes —
far above jitter and uptime-string granularity, far below the shift
of any real reboot (the entire prior uptime). The held value is
restored across HA restarts (`RestoreSensor`) and survives polls
where both sources are absent. Stability contract: **the state
changes only on a real reboot.**

⁶ **Sticky** (non-continuous data sensors). These fields carry no
`state_class` or `unit`, so Home Assistant treats them as
non-continuous and logs **every** state change to the logbook —
including a `→ Unavailable` transition when the modem goes
unreachable. On a transient outage the Status sensor is the sole
announcer (`→ Unreachable`); Software Version, Last Boot Time, and
non-continuous pass-through fields therefore stay available and hold
their last value through the data gap, so no second logbook line is
written (#178). This is the availability axis in action: **a sticky
sensor's gap carries no information, so it is suppressed; a continuous
sensor's gap is graph signal, so it is preserved.** Continuous
pass-through fields (those in `_SYSTEM_INFO_FIELD_UNITS`, which carry
a unit/`state_class`) opt out and keep the standard data-sensor
availability. Firmware- and reboot-change logging is unaffected: a
real change to Software Version or Last Boot Time is still a value
change and still logs one line.

#### Boot-time source normalization

Every boot-time source is
converted to a duration and anchored to HA's clock; the modem's wall
clock is never used as an absolute anchor (its timezone and sync
state are unknown). Source priority:

1. `system_uptime` — already a duration; the trusted source.
2. A native modem-reported boot time (none in the fleet today): if
   one appears, subtract it from the modem's own `current_time` on
   the same page so clock offset and timezone cancel, then anchor
   the resulting duration to HA's clock. The modem's clock is only
   ever compared against itself. Do not implement until a HAR shows
   a modem reporting this.
3. Counter-reset detection — an upper bound with poll-interval
   granularity, not a measurement; last resort.

**Implicit capabilities:** Capabilities are not declared — they are
implicit from parser.yaml mappings (see
[PARSING_SPEC.md](../../../packages/cable_modem_monitor_core/docs/PARSING_SPEC.md#capabilities-are-implicit)).
If parser.yaml maps `system_info.software_version`, the Software
Version sensor exists. If parser.yaml has a `downstream` section,
downstream channel sensors and channel counts exist. No data in
parser output → no entity created.

### Health Sensors

| Entity | unique_id suffix | State | device_class | state_class | Unit | Condition |
|--------|-----------------|-------|--------------|-------------|------|-----------|
| Ping Latency | `_ping_latency` | float | — | MEASUREMENT | ms | `supports_icmp = True` |
| TCP Latency | `_tcp_latency` | float | — | MEASUREMENT | ms | Health coordinator exists (HTTP probe enabled) |
| HTTP Latency | `_http_latency` | float | — | MEASUREMENT | ms | `supports_head = True` (HEAD-only — no GET fallback for bimodal-corrupted data) |

### Per-Channel Downstream Sensors

One entity per metric per channel. Entity creation and unique_id
format depend on the user's channel identity mode — see
[Channel Identity](#channel-identity).

**Position mode** (`channel_identity: "number"`):

| Entity | unique_id suffix | Unit | device_class | state_class | Condition |
|--------|-----------------|------|--------------|-------------|-----------|
| Power | `_ds_ch_{n}_power` | dBmV | — | MEASUREMENT | Always |
| SNR | `_ds_ch_{n}_snr` | dB | — | MEASUREMENT | Always |
| Frequency | `_ds_ch_{n}_frequency` | Hz | FREQUENCY | MEASUREMENT | If present |
| Corrected | `_ds_ch_{n}_corrected` | — | — | TOTAL_INCREASING | If present |
| Uncorrected | `_ds_ch_{n}_uncorrected` | — | — | TOTAL_INCREASING | If present |

**ID mode** (`channel_identity: "id"`):

| Entity | unique_id suffix | Unit | device_class | state_class | Condition |
|--------|-----------------|------|--------------|-------------|-----------|
| Power | `_ds_{type}_ch_{id}_power` | dBmV | — | MEASUREMENT | Always |
| SNR | `_ds_{type}_ch_{id}_snr` | dB | — | MEASUREMENT | Always |
| Frequency | `_ds_{type}_ch_{id}_frequency` | Hz | FREQUENCY | MEASUREMENT | If present |
| Corrected | `_ds_{type}_ch_{id}_corrected` | — | — | TOTAL_INCREASING | If present |
| Uncorrected | `_ds_{type}_ch_{id}_uncorrected` | — | — | TOTAL_INCREASING | If present |

**Attributes on every DS channel sensor:** `channel_number`,
`channel_id`, `channel_type`. Both identifiers are always present
regardless of mode. Additional Tier 2/3 fields from parser output
(lock_status, modulation, etc.) flow as attributes.

### Per-Channel Upstream Sensors

**Position mode** (`channel_identity: "number"`):

| Entity | unique_id suffix | Unit | device_class | state_class | Condition |
|--------|-----------------|------|--------------|-------------|-----------|
| Power | `_us_ch_{n}_power` | dBmV | — | MEASUREMENT | Always |
| Frequency | `_us_ch_{n}_frequency` | Hz | FREQUENCY | MEASUREMENT | If present |

**ID mode** (`channel_identity: "id"`):

| Entity | unique_id suffix | Unit | device_class | state_class | Condition |
|--------|-----------------|------|--------------|-------------|-----------|
| Power | `_us_{type}_ch_{id}_power` | dBmV | — | MEASUREMENT | Always |
| Frequency | `_us_{type}_ch_{id}_frequency` | Hz | FREQUENCY | MEASUREMENT | If present |

**Attributes on every US channel sensor:** `channel_number`,
`channel_id`, `channel_type`. Both identifiers are always present
regardless of mode. Additional fields (symbol_rate, lock_status,
modulation, ranging_status, etc.) flow as attributes.

### LAN Statistics Sensors

Created per network interface, 8 sensors each.

| Entity | unique_id suffix | Unit | device_class | state_class |
|--------|-----------------|------|--------------|-------------|
| Received Bytes | `_lan_{iface}_received_bytes` | B | DATA_SIZE | TOTAL_INCREASING |
| Received Packets | `_lan_{iface}_received_packets` | — | — | TOTAL_INCREASING |
| Received Errors | `_lan_{iface}_received_errors` | — | — | TOTAL_INCREASING |
| Received Drops | `_lan_{iface}_received_drops` | — | — | TOTAL_INCREASING |
| Transmitted Bytes | `_lan_{iface}_transmitted_bytes` | B | DATA_SIZE | TOTAL_INCREASING |
| Transmitted Packets | `_lan_{iface}_transmitted_packets` | — | — | TOTAL_INCREASING |
| Transmitted Errors | `_lan_{iface}_transmitted_errors` | — | — | TOTAL_INCREASING |
| Transmitted Drops | `_lan_{iface}_transmitted_drops` | — | — | TOTAL_INCREASING |

### Buttons

| Entity | unique_id suffix | entity_category | Availability |
|--------|-----------------|-----------------|--------------|
| Restart Modem | `_restart_button` | — | Only if `actions.restart` in modem.yaml |
| Update Modem Data | `_update_data_button` | DIAGNOSTIC | Always |
| Reset Entities | `_reset_entities_button` | CONFIG | Always |

Restart Modem is only created when modem.yaml declares
`actions.restart`. The other two buttons are always created.

The Update Modem Data button and the `request_refresh` service share
the same logic (`async_request_modem_refresh`): reset connectivity
backoff, refresh health probes, then trigger a data poll. Both
short-circuit when a destructive operation holds the
`active_operation` mutex — the adapter runs its own post-operation
refresh, so a user-initiated refresh during that window is
unnecessary (see HA_ADAPTER_SPEC § Operation Mutex).

The Restart Modem and Reset Entities buttons acquire the mutex for
the duration of their work and refuse if the other is already
running. The `request_health_check` service triggers only the
health probe. All services accept a device target for multi-modem
setups. See HA_ADAPTER_SPEC.md § Services for details and automation
examples.

### Status Sensor

The Status sensor composes three inputs from the latest snapshot
into a single display state via a priority cascade. The sensor
contains no business logic — it is a pure lookup over values
derived upstream. It does **not** read recovery state; the snapshot
already tells the truth about what the modem is reporting, and
recovery windows only change polling cadence, not the snapshot's
meaning.

**Inputs** (pass-through as attributes):

| Attribute | Source | Values |
|-----------|--------|--------|
| `connection_status` | Orchestrator (pipeline outcome) | `online`, `no_signal`, `parser_issue`, `auth_failed`, `unreachable` |
| `health_status` | Health pipeline (probe results) | `responsive`, `unresponsive`, `icmp_blocked`, `degraded` |
| `docsis_status` | Orchestrator (normalized `lock_status`) | `operational`, `partial_lock`, `not_locked`, `unknown` |
| `diagnosis` | HA integration (derived from `health_status`) | Free text |

**Priority cascade** (most to least concerning):

| Priority | Condition | Display State |
|----------|-----------|---------------|
| 1 | `health_status == unresponsive` | Unresponsive |
| 2 | `connection_status == unreachable` | Unreachable |
| 3 | `connection_status == auth_failed` | Auth Failed |
| 4 | `health_status == degraded` | Degraded |
| 5 | `connection_status == parser_issue` | Parser Error |
| 6 | `connection_status == no_signal` | No Signal |
| 7 | `docsis_status == not_locked` | Not Locked |
| 8 | `docsis_status == partial_lock` | Partial Lock |
| 9 | `health_status == icmp_blocked` | ICMP Blocked |
| 10 | default | Operational |

**Input freshness.** The cascade's inputs refresh on different
cadences (health every health-check interval, connection/DOCSIS every
poll), so one input can be stale when the other updates. The one
combination where staleness misleads — a stale `unresponsive` at
priority 1 masking a modem that just served a successful poll — is
closed by the adapter's health sync listeners: a successful poll
while health reads a data-path-down state triggers an immediate
health refresh (see HA_ADAPTER_SPEC § Health Sync Listeners), so the
contradiction lives for seconds, not a full health interval.

There is no synthetic "Restarting…" label. During a recovery
window — whether triggered by a commanded restart, an observed
outage, or a reboot-signal match — the Status sensor reflects
whatever the modem is actually reporting: Unreachable while the
modem is down, Not Locked / Partial Lock while it's ranging,
Operational once it returns. The recovery window's effect is on
*polling cadence* (HA polls faster during the window; see
HA_ADAPTER_SPEC § Recovery Adapter), not on the sensor's value.

**Re-rendering:** the Status sensor re-renders on every coordinator
update — faster during recovery cadence, normal cadence otherwise.
It doesn't need to subscribe to any recovery signal; the
coordinator already drives the updates.

See [RUNTIME_POLLING_SPEC.md](../../../packages/cable_modem_monitor_core/docs/RUNTIME_POLLING_SPEC.md#status-derivation)
for the derivation rules that produce the three input values.

---

## Channel Identity

The user selects a channel identity mode at config time, stored in
`entry.data["channel_identity"]`. This is a setup-time choice —
identical in UX to the entity prefix setting. No conversion between
modes; changing requires deleting and re-adding the integration.

See
[CHANNEL_IDENTIFICATION_SPEC.md](../../../packages/cable_modem_monitor_core/docs/CHANNEL_IDENTIFICATION_SPEC.md)
for the full rationale.

### Identity modes

| Mode | Slot key | Entity example | Default |
|------|----------|----------------|---------|
| `number` | `channel_number` (modem's row position) | `sensor.cable_modem_ds_ch_1_power` | Yes (new installs) |
| `id` | `(channel_type, channel_id)` (CMTS assignment) | `sensor.cable_modem_ds_qam_ch_29_power` | No |

**Channel types:**

- **Downstream:** `qam`, `ofdm`
- **Upstream:** `atdma`, `ofdma`

### Mapping manager

The mapping manager translates Core's channel list into keyed entity
slots. It reads `channel_identity` from `entry.data` and builds
`_downstream_by_slot` and `_upstream_by_slot` dicts for O(1) lookup.

- **Position mode:** slots keyed by `channel_number` — all positions
  included. Unlocked channels return `None` metrics. Entity set is
  fixed at setup from the channel list length.
- **ID mode:** slots keyed by `(channel_type, channel_id)` — unlocked
  channels excluded (no valid key). Entity set is dynamic — entities
  are created from whatever channels the modem currently reports.

Sensors read from `_*_by_slot` for data and from
`entry.data["channel_identity"]` to select their entity ID format.

### Rebonding behavior

- **Position mode:** Entities survive reboots. Channel numbers come
  from the modem's own row positions. If the CMTS reassigns Channel
  IDs, `channel_id` attributes update but entity identity is
  unchanged.
- **ID mode:** Current behavior. Old entities show "Unknown," new
  channel IDs appear without entities. Reset Entities resolves it.

### Migration (existing installs)

`async_migrate_entry` runs once at startup when the config entry
version is V1: adds `channel_identity: "id"` to `entry.data` and
bumps the version. Pure metadata update — no entity rewrites.

---

## Field Pass-Through

All fields from parser output flow to HA. Channels and system_info use
different patterns:

- **Channel fields** that are not a sensor metric become attributes on
  the channel sensor (e.g., `modulation`, `lock_status`).
- **System info fields** that lack a dedicated sensor class become
  their own sensor entity via `SystemInfoFieldSensor`.

The integration does not decide which fields are "important enough"
for their own entity. It exposes everything the parser provides. Users
create template sensors/binary_sensors from attributes for their own
automations.

[FIELD_REGISTRY.md](../../../packages/cable_modem_monitor_core/docs/FIELD_REGISTRY.md)
defines field naming authority across three tiers (canonical, registered,
unregistered). Tiers govern naming validation, not HA entity type — the
sections below define how each data section maps to HA entities.

### Channel Field Pass-Through

| FIELD_REGISTRY Tier | HA Mapping | Examples |
|---------------------|-----------|----------|
| Tier 1 canonical, numeric | Own sensor entity (one per channel) | `power`, `snr`, `frequency`, `corrected`, `uncorrected` |
| Tier 1 canonical, string/enum | Attribute on channel sensor | `lock_status`, `modulation`, `channel_type` |
| Tier 2 registered | Attribute on channel sensor | `channel_width`, `ranging_status` |
| Tier 3 unregistered | Attribute on channel sensor | `t3_timeouts`, `security_type` |

All non-metric fields from the channel dict are exposed as
`extra_state_attributes` on the channel's primary sensor.

### System Info Pass-Through

System info fields use a consumed/display-only/dynamic pattern:

| Pattern | HA Entity | Fields |
|---------|-----------|--------|
| Consumed (dedicated class or derivation) | One sensor class per field | `software_version`, `system_uptime`, `downstream_channel_count`, `upstream_channel_count`, `total_corrected`, `total_uncorrected`, `docsis_status` |
| Display-only (never minted) | None — display surfaces carry the value | `current_time`, `hardware_version`, `model_name` |
| Dynamic (pass-through) | Generic `SystemInfoFieldSensor(field=key)` | Everything else in `system_info` |

**Consumed fields** are those owned by a dedicated sensor class or
derivation in `sensor.py` (see § Entity Catalog — System Sensors).
They are listed in `CONSUMED_SYSTEM_INFO_FIELDS`:

- `software_version` — Tier 1 canonical, always from parser.yaml
  mappings
- `system_uptime` — Tier 1 canonical, consumed by the Last Boot Time
  derivation. It has **no sensor of its own**: an uptime state changes
  every poll by definition, writing a recorder row per cycle for a
  value that is `now − last_boot` at display time (#178). The
  dashboard generator renders Last Boot Time with `format: relative`
  to carry the uptime display.
- `downstream_channel_count`, `upstream_channel_count` — always
  computed by the parser coordinator (`len(channels)`); native value from
  parser.yaml takes precedence if mapped
- `total_corrected`, `total_uncorrected` — declared in parser.yaml's
  `aggregate` section (scoped sums from channel data); native mapping
  takes precedence if the modem reports totals directly
- `docsis_status` — consumed by the **Status sensor**, which already
  carries it two ways: `_compute_display_status` returns the DOCSIS
  lock state directly when nothing higher-priority is wrong (Not
  Locked / Partial Lock / Operational), and it is exposed raw on the
  Status sensor's `docsis_status` attribute. It has **no sensor of its
  own**: minting a standalone `SystemInfoFieldSensor` for it
  double-logs every real lock change (once on Status, once on the
  standalone) and adds a fourth `→ Unavailable` line on every outage
  (#178). The standalone entity was retired; the dashboard generator
  renders DOCSIS Status as an `attribute` row off the Status sensor.

**Display-only fields** (`DISPLAY_ONLY_SYSTEM_INFO_FIELDS`) are
captured for the data layer — snapshot, event payload, diagnostics —
but never minted as entities (#178):

- `current_time` — a per-poll wall-clock reading whose past states
  have no diagnostic value; persisting it writes a recorder row
  every poll.
- `hardware_version`, `model_name` — immutable identity strings. A
  sensor whose state cannot change is device metadata: the device
  registry carries them natively (`hw_version`, `model_id`), set in
  `_update_device_registry`. `model_id` is the modem's reported
  `model_name`; `model` stays the catalog entry's model. The two differ
  whenever the reported value is an OEM code rather than a retail name
  (XB7 reports `CGM4331COM`), so the reported one must never take over
  `model`. `software_version` is the deliberate
  contrast — it also appears on the device card (`sw_version`) but
  **keeps its sensor**, because a firmware push is a state change
  users automate on and the sensor is the only timeline of it.

**Discontinued entities:** sensors the integration used to create and
no longer does (`System Uptime`, `Current Time`, `Hardware Version`,
`Model Name`, `Docsis Status`) leave orphaned registry entries on
in-place upgrades. Two cleanup paths:

- **v1 (3.13) entries**: the v1→v2 config-entry migration removes the
  v1-era System Uptime row (`V1_DISCONTINUED_UNIQUE_ID_SUFFIXES` in
  `migrations/v1_to_v2.py`) — 3.13 upgraders never see the orphan.
  `Docsis Status` needs no migration entry: 3.13 exposed DOCSIS status
  only as a Status-sensor attribute, never as a standalone sensor, so
  a 3.13 upgrader has no `_docsis_status` row to orphan.
- **v2 (3.14 beta) entries**: no automatic cleanup — remove and
  re-add the integration (or press Reset Entities), which
  re-registers from the current sensor classes. This is the path for
  the retired `Docsis Status` sensor (minted as a dynamic pass-through
  on earlier 3.14 betas).

The grouping is by HA entity ownership, not by FIELD_REGISTRY tier.

**Dynamic fields** are everything else. `_create_data_dependent_entities`
loops over `system_info` keys, skips consumed fields, and creates a
`SystemInfoFieldSensor` for each remaining key. Field presence gates
creation — no data, no sensor. Straight pass-through — no type
conversion or boolean mapping.

**Unit metadata.** Fields listed in `_SYSTEM_INFO_FIELD_UNITS` get a
`device_class`, `state_class`, and unit, and count as continuous for
[§ Sensor Availability Logic](#sensor-availability-logic). The unit is
the one Core stores, always base SI
([FIELD_REGISTRY.md § Naming Rules](../../../packages/cable_modem_monitor_core/docs/FIELD_REGISTRY.md#naming-rules-all-tiers)).
Where that is not the unit users read, the field also declares a
suggested unit and HA scales the state for display: provisioned speed
is stored in bit/s and shown in Mbit/s. HA stamps a suggested unit into
the entity registry the first time it sees an entity and never revisits
it, so changing this table moves only newly created entities. Existing
ones follow the **Discontinued entities** paths above — the entity's
own settings, Reset Entities, or remove and re-add.

**Graduation:** When a dynamic field gets a dedicated sensor class, add
it to `_CONSUMED_SYSTEM_INFO_FIELDS`. The dynamic sensor for that field
stops being created. No entity migration — the dedicated class takes
over with its own unique_id.

---

## Core vs HA Boundary

| Core (platform-agnostic) | HA Integration |
|--------------------------|----------------|
| `ModemData` output (channels, system_info) | Sensor entities with HA metadata |
| `ModemIdentity` (manufacturer, model, docsis_version, release_date, status) | Modem Info sensor attributes (pass-through) |
| Field names, types, units | `device_class`, `state_class`, `unit_of_measurement` |
| Implicit capabilities (field present = capable) | Entity created if field present |
| Health check results (latency, reachability) | Health sensor entities |
| `ActionFactory` (restart support) | Button entities |
| Channel normalization (`channel_number`, `channel_type`, `channel_id`) | Mapping manager builds slot maps; entity unique_id incorporates mode-dependent key |

Core owns the data model. HA owns the presentation. The HA adapter
passes `ModemIdentity` fields through to the Modem Info sensor — no
derivation, no URL construction, no string comparison.

**What does not cross this boundary into Core:** user-side analytics
that compose, aggregate, or evaluate Core fields against
user-chosen thresholds. DS power spreads, max-of-N aggregates,
weighted health scores, Good/Fair/Poor tier classification, and any
"is this metric acceptable" judgment live in HA blueprints, not in
Core or the HA adapter. The principle is that Core's schema
documents what the fleet of modems reports; interpretation on top
of that schema is the user's choice and belongs in a distribution
surface designed for user customization. See
[BLUEPRINT_DISTRIBUTION_SPEC.md](BLUEPRINT_DISTRIBUTION_SPEC.md) for
how those user-analytics blueprints ship alongside the integration,
and
[ARCHITECTURE_DECISIONS.md § Core's schema tracks fleet-observed
metrics](../../../packages/cable_modem_monitor_core/docs/ARCHITECTURE_DECISIONS.md#cores-schema-tracks-fleet-observed-metrics-not-user-analytics)
for the underlying decision.

---

## Device Model

### One Device Per Config Entry

Each config entry creates one HA device:

```python
DeviceInfo(
    identifiers={(DOMAIN, entry.entry_id)},
    name=device_name,
    manufacturer=identity.manufacturer,
    model=identity.model,
    model_id=reported_model_name,
    configuration_url=f"{protocol}://{host}",
)
```

**Device name is set on every entity's DeviceInfo**, not just in
`_update_device_registry`. This ensures HA generates correct
entity_ids at registration time (before the device registry update
runs).

### Entity Prefix

The user selects a naming strategy at config time:

| Prefix Setting | Device Name | Purpose |
|---------------|-------------|---------|
| `none` (Default) | "Cable Modem" | Single modem setups |
| `model` | "Cable Modem {model}" | Multi-modem by model |
| `ip` | "Cable Modem {host}" | Multi-modem by IP |

All options produce entity_ids prefixed with `cable_modem_`
(e.g., `sensor.cable_modem_status`). The `model` and `ip` options
add an extra disambiguator for multi-modem setups.

### Multi-Modem

`unique_id` always includes `entry_id` (UUID), so entities are always
globally unique regardless of prefix setting. With `prefix=none` and
two modems, HA auto-suffixes entity_ids (`_2`, `_3`, etc.). Recommend
`model` or `ip` prefix for multi-modem setups.

---

## Availability

| Scenario | Continuous data¹ | Sticky data² | Status / Info | Health | Buttons |
|----------|------------------|--------------|---------------|--------|---------|
| Normal operation | available | available | available | available | available |
| Coordinator update failed | unavailable | unavailable | unavailable | unavailable | available |
| Modem unreachable | unavailable | available | available | available³ | available |
| Modem unreachable at startup | deferred⁴ | deferred⁴ | available | available³ | available |
| Parse error | unavailable | available | available | available³ | available |
| Field not parsed by this modem | never created | never created | n/a | n/a | n/a |

¹ **Continuous data sensors** carry a `state_class` or `unit`
(channel metrics, error totals/rates, channel counts, and continuous
pass-through fields). Their unavailable-gaps are meaningful on graphs,
so they go unavailable when a poll returns no `modem_data`
(`ModemSensorBase.available`).

² **Sticky data sensors** (Software Version, Last Boot Time,
non-continuous pass-through fields) carry no `state_class`/`unit` and
drive the logbook. They stay available and hold their last value
through a transient data gap so a modem outage logs no spurious
`→ Unavailable` line — the Status sensor is the sole announcer (#178).
See catalog footnote ⁶.

³ Health sensors remain available when the modem is unreachable or
produces parse errors — they report the failure state itself (latency
= None, status = Unresponsive/Parser Error).

⁴ When the modem is unreachable at HA startup, data-dependent entities
(channels, system metrics, LAN stats, sticky fields) are deferred
until the first successful poll — sticky holding needs a first value
to hold. Status, Info, and Health sensors are created immediately and
remain available during the outage. A delayed re-notification (1
second after creation) ensures deferred entities receive their initial
coordinator update, avoiding an "Unknown" state window until the next
scheduled poll. See HA_ADAPTER_SPEC § Deferred Entity Creation.

### Sensor Availability Logic

`ModemSensorBase.available` (continuous data sensors) returns `True`
when:

- `coordinator.last_update_success` is `True`, AND
- `modem_data` is present in the snapshot

Sticky data sensors drop the `modem_data` clause: available whenever
`coordinator.last_update_success` is `True`, holding the last
non-`None` value across the gap. Two small mixins compose this —
`_StickyAvailabilityMixin` (the availability override) and
`_HoldsLastValueMixin` (the value-holding helper). Software Version
uses both; Last Boot Time uses only the availability mixin (its value
already holds via `_stable_boot_time`); `SystemInfoFieldSensor` uses
the value-holding mixin and branches its own `available` (sticky for
non-continuous fields, standard `modem_data` rule for continuous
ones). The Status and Info sensors override `available` the same way —
always available while the coordinator is alive — because Status is
the single outage announcer.

Health sensors (Ping, HTTP) have independent availability based on
whether the health check ran (`ping_success is not None`,
`http_success is not None`).

---

## Entity Naming

### unique_id Format

All unique_ids are prefixed with `{entry_id}_cable_modem_`:

```text
{entry_id}_cable_modem_{suffix}
```

**System sensors:** suffix is the sensor name (e.g., `_status`,
`_software_version`, `_total_corrected`).

**Channel sensors** (format depends on identity mode):

Position mode (`number`):

```text
{entry_id}_cable_modem_{ds|us}_ch_{channel_number}_{field}
```

Example: `abc123_cable_modem_ds_ch_1_power`

ID mode (`id`):

```text
{entry_id}_cable_modem_{ds|us}_{channel_type}_ch_{channel_id}_{field}
```

Example: `abc123_cable_modem_ds_ofdm_ch_159_power`

**LAN sensors:**

```text
{entry_id}_cable_modem_lan_{interface}_{metric}
```

**Buttons:**

```text
{entry_id}_{button_name}
```

### Entity Names

All sensor classes set `_attr_has_entity_name = True`. HA generates
the full entity name from `{device_name} {sensor_name}`.

Channel sensor names depend on identity mode:

- **Position mode:** `"{DIR} Ch {n} {Metric}"` — no channel type in
  the name. Positions are unique by definition and type-agnostic.
  Example: "DS Ch 1 Power".
- **ID mode:** `"{DIR} {TYPE} Ch {ID} {Metric}"` — channel type
  included for DOCSIS 3.1 disambiguation.
  Example: "DS OFDM Ch 159 Power".
