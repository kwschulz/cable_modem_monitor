# JSON Format Specification

> Parent spec: [PARSING_SPEC.md](PARSING_SPEC.md) --- common concepts, output contract, channel type detection

The JSON parsers extract channel data from JSON API responses. Two
formats are available:

- `json` --- direct array-of-objects shape: each item in an array is
  one channel.
- `json_transposed` --- indexed-pivot shape: rows are metrics,
  ``indexN`` columns are channels. The parser pivots into per-channel
  dicts before applying field mapping.

Modems declare their structure in parser.yaml. Both formats reuse the
same key-to-field, type-conversion, channel-type, and filter machinery.

## Contents

| Section | What it covers |
|---------|----------------|
| [JSONParser](#jsonparser) | Flat form config, multi-array form, per-array resources, extraction algorithm |
| [JSONTransposedParser](#jsontransposedparser) | Indexed-pivot rows, name-field/index-prefix config, raw-row filters |

## JSONParser

Extracts data from JSON API responses using path navigation and direct
key access.

```yaml
# parser.yaml --- JSON API with dot-notation path to channel array
downstream:
  format: json
  resource: "/rest/v1/cablemodem/downstream"
  array_path: "downstream.channels"
  fields:
    - key: "channelId"
      field: channel_id
      type: integer
    - key: "lockStatus"
      field: lock_status
      type: boolean
      truthy: true
    - key: "modulation"
      field: modulation
      type: string
    - key: "frequency"
      field: frequency
      type: integer
    - key: "power"
      field: power
      type: float
    - key: "rxMer"
      field: snr
      type: float
      fallback_key: "snr"
    - key: "correctedErrors"
      field: corrected
      type: integer
    - key: "uncorrectedErrors"
      field: uncorrected
      type: integer
    - key: "channelType"
      field: channel_type
      type: string
      map:
        "sc-qam": "qam"
        "ofdm": "ofdm"

upstream:
  format: json
  resource: "/rest/v1/cablemodem/upstream"
  array_path: "upstream.channels"
  fields:
    - key: "channelId"
      field: channel_id
      type: integer
    - key: "frequency"
      field: frequency
      type: integer
    - key: "power"
      field: power
      type: float
    - key: "symbolRate"
      field: symbol_rate
      type: integer
```

**Config fields (flat form):**

| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `format` | string | yes | `json` --- selects `JSONParser` |
| `resource` | string | yes | URL path key in the resource dict |
| `array_path` | string | yes* | Dot-notation path to the channel array |
| `fields` | list | yes* | Key-to-field mappings within each JSON object |
| `fields[].key` | string | yes | JSON key name in the source object |
| `fields[].field` | string | yes | Canonical channel field name (FIELD_REGISTRY.md) |
| `fields[].type` | string | yes | Field type (one of `FIELD_TYPES`) |
| `fields[].fallback_key` | string | no | Alternative key if primary is missing |
| `fields[].format` | string | no | Input format for types that require it (e.g., `seconds` for `uptime`). |
| `fields[].scale` | number | no | Multiplier applied after type conversion. Whole-number results cast to int. |
| `fields[].unit` | string | no | Unit suffix stripped (case-insensitive) from the raw text before conversion (e.g. `"3.2 dBmV"` yields `3.2`). |
| `fields[].map` | map | no | Exact-match value translation on the stripped raw text before type conversion (e.g. `{"Locked": "locked"}`); unmapped values pass through unchanged. |
| `fields[].truthy` | any | no | Declares the raw value meaning true; the field becomes the boolean `raw == truthy`, bypassing type conversion. |
| `fields[].separator` | string | no | Splits a compound string value on a delimiter before conversion (e.g. `"0.3/60.3"`). |
| `fields[].separator_index` | integer | no | Which split segment to convert (default 0 = first; out-of-range falls back to the unsplit value). |
| `fields[].range` | string | no | `span` only: emits `last - first` from the separator-split parts (band-edge string to `channel_width`). Requires `separator`. Values not carrying the separator skip silently (mixed SC-QAM/OFDM arrays sharing a key). |
| `fields[].null_values` | list | no | Firmware no-value sentinels (e.g. `"---"` on inactive channel slots). Matching raw values (compared whitespace-trimmed) skip silently as sparse data; the cannot-coerce WARN stays reserved for undeclared shapes. |
| `channel_type` | object | no | `fixed:`, per-row `field:` + `map:`, or `derive:` form (same semantics as other formats). Applied before `fixed_fields`; rows that already extracted a `channel_type` keep it. |
| `fixed_fields` | map | no | Static field values for every channel. Applied after channel_type, before filter (same semantics as XML tables). |
| `filter` | object | no | Keep/drop rules on converted field values: a plain value keeps only matching channels; `{not: value}` drops matching channels. Applied after `fixed_fields`. |
| `arrays` | list | yes* | Multi-array form (alternative to array_path/fields) |

\* Mutually exclusive: use either flat form (`array_path` + `fields`)
or multi-array form (`arrays`).

**Multi-array form** --- for modems with multiple channel arrays in one
response (e.g., QAM + OFDM in separate JSON arrays):

```yaml
downstream:
  format: json
  resource: "/api/status"
  arrays:
    - array_path: "docsis.qam_channels"
      fields:
        - key: "channelID"
          field: channel_id
          type: integer
      channel_type:
        fixed: "qam"

    - array_path: "docsis.ofdm_channels"
      fields:
        - key: "ofdmID"
          field: channel_id
          type: integer
      channel_type:
        fixed: "ofdm"
```

Each array entry has its own `array_path`, `fields`, `channel_type`,
`fixed_fields`, and `filter`. Results from all arrays are concatenated.
In multi-array form these three must be set per array — a section-level
`channel_type`, `fixed_fields`, or `filter` alongside `arrays` is a
validation error (it would otherwise be silently ignored).

**Per-array resources** --- when channel data lives on separate API
endpoints (following the same pattern as XML tables), each array can
specify its own ``resource``:

```yaml
downstream:
  format: json
  encoding: base64
  arrays:
    - resource: "/api/qam"
      array_path: "nodes"
      channel_type:
        fixed: qam
      fields: [...]
    - resource: "/api/ofdm"
      array_path: "nodes"
      channel_type:
        fixed: ofdm
      fields: [...]
```

When a per-array ``resource`` is set, it overrides the section-level
resource for that array. Either provide a shared section-level
``resource`` or give every array its own --- partial coverage is a
validation error.

**Extraction algorithm:**

1. Get resource dict entry by path
2. Navigate to array(s) using dot-notation path
3. For each object in each array, map keys to canonical fields,
   apply `channel_type`, apply `fixed_fields` (static values
   override/fill fields on every channel), then apply `filter`
4. Concatenate results from all arrays
5. Auto-assign `channel_number` from 1-based array index when not
   already mapped by parser.yaml (see
   [CHANNEL_IDENTIFICATION_SPEC.md](CHANNEL_IDENTIFICATION_SPEC.md) §10)

## JSONTransposedParser

Extracts channel data from indexed-pivot JSON responses. Some firmware
returns channel arrays as a list of metric rows where each row carries
a name plus ``index1``/``index2``/... value columns --- one column per
channel:

```json
{
  "nodes": [
    {"name": "CH",     "index1": "0",  "index2": "1"},
    {"name": "Power",  "index1": "ON", "index2": "OFF"},
    {"name": "Freq",   "index1": "10", "index2": "20"}
  ]
}
```

Pivoting flips this into per-channel dicts (`{"CH": "0", "Power":
"ON", "Freq": "10"}` and so on) before applying the same mapping
machinery as `JSONParser`.

```yaml
upstream:
  format: json_transposed
  resource: "/api/upstream"
  channel_type:
    fixed: ofdma
  fields:
    - label: "CH"
      field: channel_id
      type: integer
    - label: "Freq"
      field: frequency
      type: frequency
      unit: MHz
    - label: "rep power"
      field: power
      type: float
      unit: dBmV
  filter:
    Power: "ON"
```

**Config fields:**

| Field | Type | Required | Default | Purpose |
|-------|------|----------|---------|---------|
| `format` | string | yes | --- | `json_transposed` --- selects `JSONTransposedParser` |
| `resource` | string | yes | --- | URL path key in the resource dict |
| `array_path` | string | no | `nodes` | Dot-notation path to the metric-rows array |
| `name_field` | string | no | `name` | Field within each row that carries the metric name |
| `index_prefix` | string | no | `index` | Prefix for indexed value columns (e.g. `index1`, `index2`) |
| `fields` | list | yes | --- | Row-name to field mappings (see below) |
| `fields[].label` | string | yes | --- | Exact value to match in the row's `name_field` |
| `fields[].field` | string | yes | --- | Canonical channel field name |
| `fields[].type` | string | yes | --- | Field type (one of `FIELD_TYPES`) |
| `fields[].unit` / `format` / `map` / `scale` | --- | no | --- | Same semantics as other formats |
| `channel_type` | object | no | --- | `fixed:` or cross-field `field:` + `map:` |
| `filter` | object | no | --- | Same equality / `not` rules as other formats. Keys may reference either a canonical channel field OR a raw row-name. Raw-row matching enables filtering on metrics that aren't mapped to a canonical field (e.g. firmware-state flags). |

**Extraction algorithm:**

1. Get resource dict entry by `resource`
2. Navigate to `array_path` to locate the metric rows
3. Detect indexed columns from the first row (sorted lex) by
   `index_prefix`
4. Pivot: for each indexed column, build one dict keyed by the
   `name_field` value of each row, with the column's value
5. Strip leading/trailing whitespace on string values
6. Apply field mappings (`label` → `field`, with type conversion)
7. Apply `channel_type` (fixed or map)
8. Apply `filter` against canonical fields and raw row-names
9. Auto-assign `channel_number` from 1-based column index when not
   already mapped by parser.yaml (see
   [CHANNEL_IDENTIFICATION_SPEC.md](CHANNEL_IDENTIFICATION_SPEC.md) §10)

**`transpose_indexed_rows` helper.** The pivot step (1–5 above
without the field mapping) is re-exported from
``solentlabs.cable_modem_monitor_core.post_processor_helpers`` — the
curated public surface that ``parser.py`` PostProcessors are allowed
to import (the parser-sandbox blocks all other Core paths). Use the
helper when a firmware-specific filter (e.g. substring match on a
transient state field) can't be expressed declaratively. Otherwise
prefer the declarative format.
