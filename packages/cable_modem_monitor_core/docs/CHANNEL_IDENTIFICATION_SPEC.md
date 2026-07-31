# Channel Identification Strategy

**Status:** Draft
**Created:** 2026-04-13
**Purpose:** Define how per-channel entities are identified, named, and
managed across the integration lifecycle.

---

## Related Documentation

- **[MODEM_YAML_SPEC.md](./MODEM_YAML_SPEC.md)** — Modem configuration schema
- **[ARCHITECTURE.md](./ARCHITECTURE.md)** — Parser architecture
- **[PARSING_SPEC.md](./PARSING_SPEC.md)** — Parser output format
- **[ENTITY_MODEL_SPEC.md](../../../custom_components/cable_modem_monitor/docs/ENTITY_MODEL_SPEC.md)** — HA entity naming and identity

---

## 1. Why Channel ID Was Chosen

The integration currently names per-channel entities after the
CMTS-assigned Channel ID (DCID/UCID). [Discussion #97][d97] documents
the original rationale:

1. **ISP diagnostic context** — ISPs reference specific Channel IDs
   when troubleshooting
2. **Frequency correlation** — Channel ID maps to a specific frequency
3. **Error accumulation** — FEC errors accumulate per DCID
4. **DOCSIS 3.1 disambiguation** — SC-QAM and OFDM can share the same
   DCID; `(channel_type, channel_id)` creates separate entities

These reasons are technically valid, but two problems have emerged that
change the default choice.

---

## 2. What Users Told Us

### Entity names don't match the modem's web page

Tim ([SB8200, #42][i42]):

> "The bottom channel numbers correspond to the Channel ID numbers
> assigned, not the order of the Channels as listed on the page."

Jon ([TM1602A, #112][i112]) compared his modem's web page to HA
side-by-side:

```text
Modem Web UI:                    Home Assistant:
Downstream 1   DCID=1            DS QAM Ch 1 Power
Downstream 9   DCID=10           DS QAM Ch 10 Power
Downstream 14  DCID=22           DS QAM Ch 22 Power
```

The data is correct but the labels don't match. Every user who looks
at both screens hits this.

### Entities break after a reboot

When a modem reboots, the CMTS may assign different Channel IDs.
Existing entities show "Unknown," new channels have data but no
entities. Users must press "Reset Entities" or restart HA. [Issue #94
(TC4400)][i94] and [Discussion #97][d97] document this in detail.

---

## 3. What the Fleet Data Shows

HAR captures and parser source across the modem catalog revealed three
findings that change the design.

### Finding 1: All channels in a unified list

DOCSIS 3.1 modems report both QAM and OFDM downstream channels, but
firmware varies in how it presents them:

- **HNAP modems** deliver QAM and OFDM in a single
  response with unified position numbering (1-34). Positions are
  type-agnostic — the CMTS determines the QAM/OFDM mix.
- **JavaScript modems** report QAM and OFDM from
  separate JS functions, each with its own 1-based numbering. The
  modem's web UI renders them as separate tables ("Downstream Bonded
  Channels" and "Downstream OFDM Channels").

Core normalizes both into a single `downstream` list with unified
`channel_number`. For JS modems, QAM channels keep their function
numbering and OFDM channels are appended after QAM (e.g., 32 QAM
channels numbered 1-32, then 2 OFDM channels numbered 33-34). The
original per-function number is preserved as `source_channel_number`
when it differs from the unified `channel_number`.

[S34 parser.yaml][s34-parser] / HAR capture — 34 downstream positions
in one response:

- Positions 1-32: QAM256
- Position 33: Not Locked (empty — could become either type)
- Position 34: OFDM PLC

[MB8611][mb8611-expected] HAR capture — 33 downstream positions:

- Positions 1-32: QAM256
- Position 33: OFDM PLC

The [S33v2 verified output][s33v2-verified] confirms the parsed result
is a single `downstream_channels` array containing both QAM and OFDM
entries. Same for the [CM1200][cm1200-verified] and
[MB8600][mb8600-verified].

### Finding 2: Unlocked positions report fake data

S34 HAR capture, position 33:

```text
33^Not Locked^Unknown^0^0^ 0.0^ 0.0^0^0^
```

Channel ID = 0, frequency = 0, power = 0.0. These are the modem
saying "nothing here," not actual signal readings.

### Finding 3: Modems already number their own rows

HNAP parser.yaml field mappings start at `index: 1`
([S34][s34-parser], [MB8611][mb8611-parser]), not `index: 0`. In the
`^`-delimited record, position 0 is the modem's own row number — it
exists in every record but is intentionally unmapped. Every modem
provides this position number.

Across manufacturers, the modem's web UI column headers confirm the
distinction between row position and Channel ID:

| Manufacturer | Position column | DCID column | Source |
|---|---|---|---|
| Motorola | Channel | Channel ID | MB8611 Connection page JS (`DownstreamChanStatus[0]`) |
| Netgear | Channel | Channel ID | CM1200 HAR capture JS column layout |
| Technicolor | Channel Index | Channel ID | TC4400 HAR capture HTML (`<td class='hd'>`) |
| Arris (TM1602A) | Downstream 1, 2... | DCID | [#112 comment][i112-comment] |

The modem already gives us what users want. We've been throwing it
away.

---

## 4. HA Ecosystem Precedent

Every HA integration that has numbered hardware positions uses the
position as the entity identity:

- **Network switch ports** (UniFi, MikroTik) — Port 1 is always
  Port 1; the connected device is an attribute
- **NAS disk bays** (Synology, QNAP) — Bay 1 is always Bay 1; the
  physical disk serial is an attribute
- **UPS outlets** (APC, NUT) — Outlet 1 is always Outlet 1; the
  connected load is an attribute

The pattern: **hardware position is the entity identity, dynamic
assignment is an attribute.** None of these integrations offer a mode
selector — they use position because it's stable.

Cable modem channels are the same class of problem. The modem has N
downstream positions. The CMTS fills them with specific Channel IDs
that can change on reboot. The position is the stable identity.

---

## 5. Decision: Mapping Manager with User-Selected Identity

Core outputs channels as a list, with `channel_id` (DCID/UCID) as
the DOCSIS-native identifier on each record. The HA layer provides a
mapping manager that translates Core's channel list into keyed entity
slots using the user's chosen identity mode.

### Channel identity mode

The user selects a channel identity mode at config time, stored in
`entry.data["channel_identity"]`. This is a setup-time choice —
identical in UX to the existing entity prefix setting.

| Mode | Slot value | Entity example | Default |
|---|---|---|---|
| `number` | Modem's row position | `sensor.cable_modem_ds_ch_1_power` | Yes (new installs) |
| `id` | CMTS-assigned Channel ID | `sensor.cable_modem_ds_qam_ch_29_power` | No |

**No mode conversion.** Changing identity mode requires deleting and
re-adding the integration. This is the same constraint as changing
the entity prefix — the entity IDs change, so HA treats it as a new
setup.

### Why offer both modes

Position mode (`number`) addresses both user complaints from
[Section 2](#2-what-users-told-us): entity names match the modem's
web page, and entities survive reboots. It is the default for new
installs.

ID mode (`id`) preserves the current DCID-based naming for users who
prefer ISP-aligned entity names or who have existing automations
and dashboards built around Channel IDs. Forcing a migration on
every existing install would be disruptive for no user benefit.

The mapping manager handles data routing — translating Core's
channel_id keys into slot lookups and excluding unlocked channels in
ID mode. Sensors read the stored mode from `entry.data` to select
their entity ID format string. This is a single `if` in the sensor
base class — mode logic does not spread into parsers, coordinator,
or config flow.

### Entity format

The two modes produce different entity ID formats because they have
different disambiguation requirements.

**Position mode** — channel type is NOT in the entity ID. Positions
are unique by definition and type-agnostic — the CMTS can change the
type assigned to a position across reboots (Finding 1):

| Component | Format | Example |
|---|---|---|
| Entity ID | `sensor.{prefix}_{dir}_ch_{n}_{metric}` | `sensor.cable_modem_ds_ch_1_power` |
| Friendly name | `{DIR} Ch {n} {Metric}` | `DS Ch 1 Power` |
| Unique ID | `{entry_id}_cable_modem_{dir}_ch_{n}_{metric}` | `abc123_cable_modem_ds_ch_1_power` |

**ID mode** — channel type IS in the entity ID. DOCSIS 3.1 modems can
have two channels sharing the same DCID at different frequencies
([Section 8](#8-docsis-31)); `(channel_type, channel_id)` is the
disambiguation key. This matches the current V1 entity format:

| Component | Format | Example |
|---|---|---|
| Entity ID | `sensor.{prefix}_{dir}_{type}_ch_{id}_{metric}` | `sensor.cable_modem_ds_qam_ch_29_power` |
| Friendly name | `{DIR} {TYPE} Ch {id} {Metric}` | `DS QAM Ch 29 Power` |
| Unique ID | `{entry_id}_cable_modem_{dir}_{type}_ch_{id}_{metric}` | `abc123_cable_modem_ds_qam_ch_29_power` |

Where `{dir}` is `ds` or `us`, `{DIR}` is `DS` or `US`, and
`{type}`/`{TYPE}` is the channel type (`qam`, `ofdm`, `atdma`,
`ofdma`).

### Entity attributes

Both identifiers are always present as attributes regardless of mode:

**Bonded channel (HNAP / HTML table):**

```json
{
  "channel_number": 1,
  "channel_id": 29,
  "channel_type": "qam",
  "frequency": 759.0,
  "lock_status": "locked"
}
```

**Bonded OFDM channel (JS modem — unified position differs from
per-function number):**

```json
{
  "channel_number": 33,
  "source_channel_number": 1,
  "channel_id": 1,
  "channel_type": "ofdm",
  "frequency": 380.0,
  "lock_status": "locked"
}
```

`source_channel_number` is only present when it differs from
`channel_number`. This lets users correlate with the modem's web UI
when it shows separate QAM and OFDM tables with independent
numbering.

**Unlocked position (position mode only):**

```json
{
  "channel_number": 33,
  "channel_id": null,
  "channel_type": null,
  "frequency": null,
  "lock_status": "not_locked"
}
```

A user who needs to correlate with ISP diagnostics reads `channel_id`
from the attribute. A Lovelace card can display either identifier as
a column. Templates can filter by `channel_type`.

---

## 6. Fixed Entity Set (Position Mode)

In position mode, entities are created at setup from the modem's total
channel capacity, not from what happens to be bonded. A modem with 34
downstream and 4 upstream positions always has DS Ch 1 through DS Ch 34
and US Ch 1 through US Ch 4. Unbonded positions report no data with
`lock_status: "not_locked"`.

In ID mode, entities are created dynamically from whatever channels
the CMTS assigns on each poll — this is the current behavior.

### Unlocked channel handling (position mode)

In ID mode, the mapping manager excludes unlocked channels — no
entities are created for them. In position mode, when a channel's
lock status is not `"locked"`:

- Only `channel_number` and `lock_status` are preserved
- All other fields are stripped — `channel_id`, `channel_type`,
  `frequency`, `modulation`, all metric fields return `None`
  (HA renders as "Unknown")
- `lock_status` attribute reports the canonical value (`"not_locked"`),
  distinguishing an unlocked channel from a poll failure (entity
  unavailable)

**Where this happens.** Core's parser coordinator owns the nulling
pass — see PARSING_SPEC.md § Field Guarantees and
`parsers/coordinator.py`. By the time `modem_data` reaches the HA
adapter, unlocked channels have already been nulled. The mapping
manager is a translator (flat list → keyed slot map), not a filter;
it does not re-run the nulling rule. Two implementations of the same
rule would be a DRY violation, and one such divergence already caused
the v3.14.0-beta.1 missing-`lock_status` setup-crash regression.

**Modems that don't report `lock_status`.** Some modems
(`hitron/coda56`, `arris/cm820b`, `arris/tm1602a`, several others)
emit channels with no `lock_status` field at all. Per Core's
coordinator: *channels without `lock_status` are left alone — some
modems do not report lock status at all*. They flow through the
pipeline unchanged and must be treated as locked at every downstream
consumer, including the mapping manager.

Unlocked positions are expected — the entity set covers full hardware
capacity. These entities exist permanently with
`lock_status: "not_locked"`.

---

## 7. Rebonding

[Discussion #97][d97] identifies rebonding as the major pain point.

### Position mode: rebonding is solved

The entity set is fixed — all entities exist from day one. Channel numbers come from the modem's own
row positions. If the modem reboots and the CMTS assigns different
Channel IDs, Ch 1 is still Ch 1. The `channel_id` attribute updates,
but the entity and its graph history continue unbroken. No "Unknown"
entities, no Reset Entities, no user action required.

### ID mode: current behavior

In ID mode, rebonding produces the same symptoms documented in
[Section 2](#entities-break-after-a-reboot): old entities show
"Unknown," new channel IDs appear without entities. Users who choose
ID mode accept this trade-off — they want ISP-aligned naming and
understand the rebonding cost. Reset Entities resolves it, same as
today.

### Channel change events

When the integration detects a channel reassignment between
consecutive polls, it fires an HA event:

**Trigger conditions:**

1. **Modem reboot** — system uptime resets
2. **Channel ID change** — a position's DCID changes
3. **Channel type change** — a position's type changes
4. **Bonding change** — a position transitions locked / unlocked

**Event format:**

```yaml
event_type: cable_modem_channel_change
data:
  device_id: "abc123"
  trigger: "reboot"
  changes:
    - position: 5
      direction: "downstream"
      field: "channel_id"
      old_value: 29
      new_value: 30
    - position: 33
      direction: "downstream"
      field: "channel_type"
      old_value: "qam"
      new_value: "ofdm"
  timestamp: "2026-04-13T14:30:00Z"
```

**Use cases:** Notifications on reboot, dashboard refresh, diagnostics
logging, correlation of channel changes with connectivity issues.

**Core vs HA boundary:** Core detects the change by comparing
consecutive poll results and produces a structured change record. The
HA integration fires the event.

---

## 8. DOCSIS 3.1

DOCSIS 3.1 modems report both SC-QAM and OFDM in a single downstream
list ([Finding 1](#finding-1-all-channels-in-a-unified-list)). Two
channels can share the same DCID at different frequencies:

```text
Position 20: QAM   DCID=33  @ 600 MHz
Position 33: OFDM  DCID=33  @ 750 MHz
```

Position mode handles this naturally — each position number is unique
by definition, so no disambiguation is needed. ID mode handles it via
`(channel_type, channel_id)` in the entity ID — the same approach
used today.

---

## 9. Migration: Existing Installs

Existing installs use Channel ID-based entity naming (the V1 scheme).
Because the mapping manager supports ID mode natively, no entity
rewrites are needed.

### Migration step

`async_migrate_entry` runs once at HA startup when the config entry
version is V1:

1. Adds `channel_identity: "id"` to `entry.data`
2. Bumps config entry version from V1 to V2

This is a pure metadata update. No entity registry changes, no I/O,
no poll data required. Existing entities continue working as-is under
ID mode.

### Switching modes

There is no migration path between identity modes. Users who want to
switch from ID to position (or vice versa) delete and re-add the
integration. This is the same constraint as changing the entity
prefix — entity IDs change, so HA treats it as a new setup.

This eliminates the entire class of migration edge cases (partial
failure, rebond during upgrade, modem offline at upgrade time) that
a forced entity rewrite would introduce.

---

## 10. Implementation

### Data flow

1. **Core (Parser):** Parser output is a channel list containing
   `channel_id` (the DOCSIS-native identifier) and `channel_number`
   (the modem's row position) on every channel, including unlocked
   positions. `channel_number` is a data field at the Core level,
   not an identity key — Core does not key or index by it.

2. **Setup (Config Flow):** Reads the user's `channel_identity`
   choice and stores it in `entry.data`.

3. **Poll (Coordinator + Mapping Manager):** Coordinator receives
   Core output (all channels, including unlocked). The mapping manager
   reads `channel_identity` from `entry.data` and builds slot maps:
   - **Position mode:** `_downstream_by_slot` and `_upstream_by_slot`
     keyed by `channel_number` — all positions included, unlocked
     channels return `None` metrics
   - **ID mode:** `_downstream_by_slot` and `_upstream_by_slot`
     keyed by `(channel_type, channel_id)` — unlocked channels
     excluded (no valid key)

   Sensors read from `_*_by_slot` for data and from
   `entry.data["channel_identity"]` to select their entity ID format.

4. **Entity creation (Sensor):**
   - **Position mode:** Derives capacity from channel list lengths.
     Creates entities for slots 1..N. Unlocked positions return
     `None`.
   - **ID mode:** Creates entities dynamically from whatever channels
     appear in the slot map (current behavior).

### Core `channel_number` pre-pass

Core extracts the modem's row position as `channel_number` on every
channel record. This is needed regardless of which HA identity mode
the user selects — position mode uses it as the entity slot, and both
modes expose it as an entity attribute.

Different parser formats provide the channel number differently:

- **HNAP delimited** — explicit in `fields[0]` of the `^`-delimited
  record. Parser.yaml maps it directly.
- **HTML table** (`table`) — some modems provide an explicit row
  number column (MB7621 "Channel", TC4400 "Channel Index", TM1602A
  "Downstream N"). When present, parser.yaml maps it to
  `channel_number`. When the column contains mixed text (e.g.,
  CM3500B "1 QAM256"), the `pattern` field on the column mapping
  extracts the number via regex (`pattern: "(\\d+)"`). When absent
  (e.g., SB8200 where column 0 is Channel ID), the table parser
  framework auto-assigns `channel_number` from the 1-based row index.
  Multi-table modems that map `channel_number` from their own data
  also emit `source_channel_number` when tables concatenate and the
  original number differs from the unified position (e.g., CM1100,
  CM3500B).
- **Transposed HTML table** (`table_transposed`) — same auto-assign
  as standard tables. `channel_number` is assigned from the 1-based
  column index when not explicitly mapped.
- **JavaScript** (`javascript`) — QAM and OFDM are parsed from
  separate JS functions, each with its own 1-based numbering. The
  parser framework appends OFDM after QAM (function order in
  parser.yaml) and assigns unified `channel_number`. The original
  per-function number is preserved as `source_channel_number` when
  it differs from `channel_number`.
- **JavaScript JSON** (`javascript_json`) — auto-assigned from
  1-based row position when not mapped by parser.yaml.
- **JSON** (`json`) — auto-assigned from 1-based array index when
  not mapped by parser.yaml.
- **XML** (`xml`) — auto-assigned from 1-based element index when
  not mapped by parser.yaml.

The rule: map `channel_number` from modem-provided data when
available; auto-assign from position when not. Either way, every
channel in Core's output MUST have a `channel_number`.

The format-level auto-assign runs before parser.py hooks, so the
coordinator applies a final numbering pass after hooks: any channel
still lacking `channel_number` (hook-appended, or hook-built for a
section with no parser.yaml config) is assigned its 1-based position
in the final list. Mapped or format-assigned numbers are never
overwritten. This closes the gap where a hook-appended channel
shipped without the field and failed event payload validation on
every poll (#92, DM1000 OFDMA).

See PARSING_SPEC.md § Field Guarantees for the output guarantee.

### Modules affected

| Module | Change |
|---|---|
| `custom_components/.../mapping_manager.py` | New module: translates Core's channel list into keyed slot maps. Pure translation — Core owns nulling, this module trusts the contract. ID mode excludes unlocked channels by relying on Core's having nulled `channel_type`/`channel_id`. |
| `custom_components/.../sensor.py` | Read slot from mapping manager; position mode creates fixed set, ID mode creates dynamically |
| `custom_components/.../coordinator.py` | Build `_downstream_by_slot` / `_upstream_by_slot` via mapping manager |
| `custom_components/.../config_flow.py` | Add `channel_identity` selector |
| `custom_components/.../migrations/` | V1→V2: add `channel_identity: "id"` to entry.data (metadata only) |
| `custom_components/.../strings.json` + translations | Updated entity name format; config flow strings for identity selector |
| Parser framework (Core) | Emit `channel_number` and `lock_status` on every channel including unlocked |
| Golden files (all catalog modems) | Capacity view — all positions including unlocked, with `channel_number` on every channel |
| `ENTITY_MODEL_SPEC.md` | See detailed conflict list below |
| `PARSING_SPEC.md` | Document `channel_number` reconciliation per format |

### ENTITY_MODEL_SPEC conflicts

The following sections of
[ENTITY_MODEL_SPEC.md](../../../custom_components/cable_modem_monitor/docs/ENTITY_MODEL_SPEC.md)
must be updated to reflect the mapping manager architecture:

1. **§ Channel Identity** — Currently keys channels by
   `(channel_type, channel_id)` and builds `_downstream_by_id` /
   `_upstream_by_id`. Must change to mode-dependent slot maps
   (`_downstream_by_slot` / `_upstream_by_slot`) built by the
   mapping manager. ID mode still keys by `(channel_type, channel_id)`;
   position mode keys by `channel_number`.

2. **§ Per-Channel DS/US sensor suffixes** — Current format
   `_{ds|us}_{type}_ch_{id}_{metric}` is the ID mode format (unchanged).
   Position mode adds a new format: `_{ds|us}_ch_{n}_{metric}` — no
   channel type in the suffix. Both formats must be documented.

3. **§ Entity Naming — unique_id format** — ID mode retains the
   current format. Position mode uses
   `{entry_id}_cable_modem_{ds|us}_ch_{n}_{field}`.

4. **§ Entity Naming — channel sensor friendly names** — ID mode
   retains `"DS {TYPE} Ch {ID} {Metric}"`. Position mode uses
   `"{DIR} Ch {n} {Metric}"` — no channel type in the name.

ID mode preserves the current ENTITY_MODEL_SPEC format exactly. The
changes add position mode as an alternative, not a replacement. The
rest of ENTITY_MODEL_SPEC (system sensors, health sensors, LAN stats,
buttons, availability, device model) is unaffected.

---

## 11. Open Questions

1. ~~**`channel_number` for JavaScript parsers with separate
   functions**~~ — **Resolved.** JS modems render QAM and OFDM as
   separate tables with independent 1-based numbering. Core appends
   OFDM after QAM (function order in parser.yaml) to produce unified
   `channel_number`. The original per-function number is preserved as
   `source_channel_number` when it differs from `channel_number`.
   See [Section 3 Finding 1](#finding-1-all-channels-in-a-unified-list)
   and [Section 10 Core pre-pass](#core-channel_number-pre-pass).

2. **User-submitted verified file naming:** Users may submit
   diagnostics captures from either identity mode. The verified file
   naming convention should indicate the view (bonded or capacity)
   so both can be recorded as artifacts when available.

3. **Dynamic entity lifecycle (Discussion #97):** In position mode,
   the fixed entity set eliminates rebonding as a user-facing
   problem — the active/inactive lifecycle design from
   [Discussion #97][d97] is no longer needed. In ID mode, rebonding
   remains (current behavior). Channel change events (Section 7)
   provide automation hooks for both modes.

---

## Decision Summary

| Decision | Choice | Rationale |
|---|---|---|
| Core outputs channel list with channel_id | DOCSIS-native identifier per record | Core stays platform-agnostic; HA owns keying and presentation |
| HA mapping manager with user-selected mode | Position (default) or ID | Addresses user complaints while preserving existing naming for those who want it |
| Channel type in entity ID depends on mode | Position mode: not included (positions are unique). ID mode: included (DCID disambiguation for DOCSIS 3.1) | Position mode matches HA conventions; ID mode matches current V1 format |
| Channel number from modem, not computed | Core pre-pass extracts modem's row position; auto-assign from row index when modem doesn't provide it | Parsers already have the data ([Finding 3](#finding-3-modems-already-number-their-own-rows)) |
| JS modems: unified numbering with source preservation | Append OFDM after QAM; emit `source_channel_number` only when it differs | JS modems render separate tables; unified numbering enables position mode; source number preserves web UI correlation |
| Unlocked channels: None (position) / excluded (ID) | Zero is not a real measurement | ([Finding 2](#finding-2-unlocked-positions-report-fake-data)) |
| Fixed entity set (position mode) | Channel capacity from parsed output | Eliminates entity churn; ID mode uses dynamic creation |
| No mode conversion | Delete and re-add to switch | Same constraint as entity prefix; eliminates migration edge cases |
| Existing installs default to ID mode | Metadata-only migration | No entity rewrites; preserves current behavior |
| Channel change events on rebond | HA event with old/new values | Enables automations and diagnostics |
| Both identifiers as attributes | No information lost | Users/templates can display `channel_id` or `channel_number` regardless of mode |

---

## References

<!-- GitHub issues and discussions -->
[d97]: https://github.com/solentlabs/cable_modem_monitor/discussions/97
[i42]: https://github.com/solentlabs/cable_modem_monitor/issues/42
[i94]: https://github.com/solentlabs/cable_modem_monitor/issues/94
[i112]: https://github.com/solentlabs/cable_modem_monitor/issues/112
[i112-comment]: https://github.com/solentlabs/cable_modem_monitor/issues/112#issuecomment-2999587412

<!-- Catalog: parser.yaml files (source of field mappings) -->
[s34-parser]: ../../../packages/cable_modem_monitor_catalog/solentlabs/cable_modem_monitor_catalog/modems/arris/s34/parser.yaml
[mb8611-parser]: ../../../packages/cable_modem_monitor_catalog/solentlabs/cable_modem_monitor_catalog/modems/motorola/mb8611/parser.yaml

<!-- Catalog: verified/expected golden files (parsed output evidence) -->
[s33v2-verified]: ../../../packages/cable_modem_monitor_catalog/solentlabs/cable_modem_monitor_catalog/modems/arris/s33v2/test_data/modem.verified.json
[cm1200-verified]: ../../../packages/cable_modem_monitor_catalog/solentlabs/cable_modem_monitor_catalog/modems/netgear/cm1200/test_data/modem-basic.verified.json
[mb8600-verified]: ../../../packages/cable_modem_monitor_catalog/solentlabs/cable_modem_monitor_catalog/modems/motorola/mb8600/test_data/modem.verified.json
[mb8611-expected]: ../../../packages/cable_modem_monitor_catalog/solentlabs/cable_modem_monitor_catalog/modems/motorola/mb8611/test_data/modem.expected.json

<!-- Note: Raw HNAP/HTML fixtures are in HAR captures (Git LFS).
     The findings in Section 3 were confirmed from HAR data and
     golden files listed above. -->
