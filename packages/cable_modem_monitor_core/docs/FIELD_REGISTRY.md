# Field Registry

Field names follow a three-tier system. This registry is the naming
authority for all fields that cross the parser → Core → HA boundary.

Core defines **Tier 1 canonical fields** — the fields Core validates
and uses for entity identity, health checks, and status derivation.
See [PARSING_SPEC.md](PARSING_SPEC.md#field-guarantees)
for canonical field definitions and guarantees.

This document owns **Tier 2 registered fields** — standardized names
for pass-through fields that multiple modems expose. Core maintains
this registry because it defines what fields are valid across the
parser → Core → HA boundary; the Catalog populates them.

---

## Tier 1 — Canonical Fields (defined by Core)

Core validates these. Parsers must use exactly these names.

| Section | Fields |
|---------|--------|
| downstream | `channel_number`, `channel_id`, `frequency`, `power`, `snr`, `lock_status`, `modulation`, `channel_type`, `corrected`, `uncorrected` |
| upstream | `channel_number`, `channel_id`, `frequency`, `power`, `lock_status`, `modulation`, `channel_type`, `symbol_rate` |
| system_info | `software_version`, `hardware_version`, `system_uptime`, `docsis_status` |

### Tier 1 Promotion Rules

A field is promoted from Tier 2 to Tier 1 (Canonical) when Core logic
depends on it for more than pass-through:

1. **Identity**: Required to uniquely identify a channel or device.
2. **Health Logic**: Used to determine device health, signal availability,
   or connectivity state.
3. **Aggregates**: Required to calculate aggregate sensors (e.g., total
   uncorrected errors).

`channel_number` is always present (1-based, auto-assigned from row
position when not explicitly mapped). `source_channel_number` is
present only on JS-embedded modems when the per-function position
differs from the unified `channel_number`. See
[CHANNEL_IDENTIFICATION_SPEC.md](CHANNEL_IDENTIFICATION_SPEC.md) §10.

### `frequency` semantics

Hz, integer. The same field name carries different physical meaning
across channel types — but the catalog stores one canonical value
per channel, by convention:

| `channel_type` | What `frequency` represents |
|----------------|-----------------------------|
| `qam`, `atdma` | Carrier center frequency (the SC-QAM channel is a single 6/8 MHz carrier; center is unambiguous) |
| `ofdm`, `ofdma` | **Lower edge of the active subcarrier band** |

The OFDM/OFDMA convention is "lower edge" — not center, not FFT-block
boundary — for fleet consistency. Different firmwares report the band
in different shapes (a single MHz value, a `"low~high"` range string,
a discrete `firstFrequency` key, a `firstActiveSubcarrier` index, etc.);
each parser.yaml maps whichever firmware shape lands at the lower edge,
and downstream consumers can treat the value uniformly. `channel_width`
(Tier 2) carries the band span when the firmware exposes it.

How this convention was chosen: CM2050V's reported OFDM frequency
(690 MHz) overlaps SC-QAM channels at 642–651 MHz if interpreted as
center, which is physically impossible (no spectrum sharing). Lower
edge places the 96 MHz block at 690–786 MHz, clear of SC-QAM, matching
DOCSIS 3.1 OSSI MIB `docsIf31CmDsOfdmChanLowerBoundaryFrequency`.

### Canonical channel key order (JSON serialization)

Channel dicts are presented in a fixed key order when serialized to JSON
(diagnostics downloads, verified artifacts). Parser insertion order is an
artifact of each modem's native table layout and differs by parser type —
so serializers normalize via `canonicalize_channel_keys()` before output.

The order groups keys by role (identification → location → quality →
errors):

1. `lock_status`
2. `channel_type`
3. `channel_id`
4. `channel_number`
5. `source_channel_number`
6. `modulation`
7. `frequency`
8. `symbol_rate`
9. `power`
10. `snr`
11. `corrected`
12. `uncorrected`

Tier 2/3 pass-through fields preserve their original insertion order and
appear after the Tier 1 keys. Missing keys are skipped — channels remain
sparse.

Applies to serialization boundaries only. In-memory representations
(parser output, orchestrator snapshot, HA entity state) are schemaless
dicts and make no order guarantee.

---

## Tier 2 — Registered Fields

Standardized names for pass-through fields that multiple modems expose.
Core does not use these, but the test harness validates that parsers
use the registered name when the data is semantically equivalent. This
prevents the same concept from appearing under different names across
modems (e.g., `chan_width` vs `channel_width` vs `bandwidth`).

```yaml
# field_registry.yaml
registered_fields:
  downstream:
    channel_width:
      type: integer
      unit: "Hz"
      description: "OFDM channel bandwidth"
    active_subcarriers:
      type: integer
      description: "Number of active OFDM subcarriers"
    fft_type:
      type: string
      description: "FFT size/type for OFDM channel (e.g., '2K', '4K')"
    profile_id:
      type: string
      description: "OFDM downstream profile ID"
  upstream:
    channel_width:
      type: integer
      unit: "Hz"
      description: "OFDMA channel bandwidth"
    ranging_status:
      type: string
      description: "Upstream ranging status"
  system_info:
    docsis_version:
      type: string
      description: "DOCSIS specification version"
    boot_status:
      type: string
      description: "DOCSIS boot sequence status (Connectivity/Provisioning/Boot State)"
    dhcp_status:
      type: string
      description: "DHCP binding state"
    tftp_status:
      type: string
      description: "TFTP configuration file download state"
    model_name:
      type: string
      description: "Modem model identifier as reported by the device"
    temperature:
      type: float
      unit: "°C"
      description: "Modem internal temperature"
    provisioned_speed_down:
      type: integer
      unit: "bit/s"
      description: "ISP-provisioned downstream speed (max service flow rate)"
    provisioned_speed_up:
      type: integer
      unit: "bit/s"
      description: "ISP-provisioned upstream speed (max service flow rate)"
    provisioned_burst_down:
      type: integer
      unit: "B"
      description: "ISP-provisioned downstream max traffic burst"
    provisioned_burst_up:
      type: integer
      unit: "B"
      description: "ISP-provisioned upstream max traffic burst"
```

### Graduation Criteria

A field graduates from unregistered to registered when 3+ modems
expose semantically equivalent data. The pull request adding the
registration also updates all existing parser.yaml files to use the
standardized name.

---

## Tier 3 — Unregistered Fields

Modem-specific pass-throughs with no registry entry. These follow
the naming rules below and flow to downstream attributes unchanged.
No validation beyond format.

Examples: `t3_timeouts`, `t4_timeouts`, `security_type`.

---

## HA Entity Mapping

Tiers define naming authority — which fields Core validates and which
the registry standardizes. How each field becomes an HA entity is
determined by the data section it belongs to, not its tier:

- **Channel fields:** Tier 1 numeric fields (`power`, `snr`, etc.)
  become their own sensor entity. All other channel fields (any tier)
  become attributes on that channel's sensor.
- **System info fields:** Fields with a dedicated sensor class
  (currently Tier 1 canonical + aggregates) are consumed by that class.
  All remaining fields (any tier) become a dynamic
  `SystemInfoFieldSensor` entity.

See [ENTITY_MODEL_SPEC.md § Field Pass-Through](../../../custom_components/cable_modem_monitor/docs/ENTITY_MODEL_SPEC.md#field-pass-through)
for the full mapping rules.

---

## Naming Rules (all tiers)

- snake_case, lowercase, no abbreviations except industry-standard
  (`snr`, `fft`, `ofdm`)
- Units are always the base SI unit in the value — `frequency` is Hz,
  `power` is dBmV, `channel_width` is Hz — never scaled (no MHz, no kHz)
- The `unit` above is the stored unit. A consumer may display a scaled
  one (HA shows `provisioned_speed_*` in Mbit/s); that is a presentation
  choice and never changes what the field carries
- Boolean fields use `is_` prefix: `is_bonded`, `is_primary`
- Status fields use `_status` suffix: `lock_status`, `ranging_status`
