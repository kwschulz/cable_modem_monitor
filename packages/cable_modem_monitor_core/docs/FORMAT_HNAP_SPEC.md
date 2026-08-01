# HNAP Format Specification

> Parent spec: [PARSING_SPEC.md](PARSING_SPEC.md) --- common concepts, output contract, channel type detection

The HNAPParser extracts channel data from delimiter-separated values
embedded in HNAP JSON responses. Each HNAP action response contains a
single delimited string encoding all channels for a direction
(downstream or upstream). The parser splits records and fields by
configurable delimiters, applies type conversion and value mapping,
and produces the standard channel output contract.

## Contents

| Section | What it covers |
|---------|----------------|
| [HNAPParser](#hnapparser) | Format overview and scope |
| [HNAP record layout](#hnap-record-layout) | Field positions for downstream and upstream records |
| [parser.yaml example](#parseryaml-example) | Complete config validated against S33v2 HAR |
| [Config fields](#config-fields) | All supported parser.yaml fields and types |
| [Extraction algorithm](#extraction-algorithm) | Step-by-step parsing pipeline |
| [HNAP system_info](#hnap-system_info) | Flat key-value extraction for system metadata |

## HNAPParser

Extracts data from HNAP SOAP responses where channel data is encoded
as delimiter-separated strings within JSON values.

### HNAP record layout

Each channel is a `field_delimiter`-separated string. Records are
joined by `record_delimiter`. Every record ends with a trailing
delimiter, producing an empty final element after split (ignored by
the parser).

Downstream and upstream have **different field counts**. The fields
are positional --- no headers. Index 0 is the modem's row counter ---
parser.yaml maps it as `channel_number` (see
[CHANNEL_IDENTIFICATION_SPEC.md](CHANNEL_IDENTIFICATION_SPEC.md) §3
Finding 3). Unlike other formats, HNAP parsers do **not** auto-assign
`channel_number` --- it must be explicitly mapped from index 0.

**Downstream** (9 data fields per record --- validated from S33v2 HAR):

| Index | Field | Example (SC-QAM) | Example (OFDM) |
|-------|-------|-------------------|----------------|
| 0 | channel_number | `1` | `25` |
| 1 | lock_status | `Locked` | `Locked` |
| 2 | channel_type | `QAM256` | `OFDM PLC` |
| 3 | channel_id | `24` | `159` |
| 4 | frequency (Hz) | `567000000` | `663000000` |
| 5 | power (dBmV) | `3` | `3` |
| 6 | snr (dB) | `41` | `41` |
| 7 | corrected | `0` | `3684625135` |
| 8 | uncorrected | `0` | `0` |

**Upstream** (7 data fields --- no SNR, no error counts):

| Index | Field | Example (SC-QAM) | Example (OFDMA) |
|-------|-------|-------------------|-----------------|
| 0 | channel_number | `1` | `5` |
| 1 | lock_status | `Locked` | `Locked` |
| 2 | channel_type | `SC-QAM` | `OFDMA` |
| 3 | channel_id | `1` | `7` |
| 4 | symbol_rate (Hz) | `6400000` | `34000000` |
| 5 | frequency (Hz) | `38400000` | `41800000` |
| 6 | power (dBmV) | `47.0` | `40.0` |

**Channel type values vary by manufacturer.** The examples above are
from Arris S33/S33v2. Other HNAP modems may use different
strings for the same DOCSIS technology. The `channel_type.map` in
parser.yaml normalizes manufacturer-specific strings to canonical
types (`qam`, `ofdm`, `atdma`, `ofdma`).

### parser.yaml example

Validated against S33v2 HAR pipeline output (26 DS + 5 US channels).

```yaml
# parser.yaml --- HNAP delimited strings with mixed channel types
downstream:
  format: hnap
  response_key: "GetCustomerStatusDownstreamChannelInfoResponse"
  data_key: "CustomerConnDownstreamChannel"
  record_delimiter: "|+|"
  field_delimiter: "^"
  fields:
    - index: 0
      field: channel_number
      type: integer
    - index: 1
      field: lock_status
      type: string
    - index: 2
      field: channel_type
      type: string
      map:
        "QAM256": "qam"
        "OFDM PLC": "ofdm"
    - index: 3
      field: channel_id
      type: integer
    - index: 4
      field: frequency
      type: frequency
    - index: 5
      field: power
      type: float
    - index: 6
      field: snr
      type: float
    - index: 7
      field: corrected
      type: integer
    - index: 8
      field: uncorrected
      type: integer

upstream:
  format: hnap
  response_key: "GetCustomerStatusUpstreamChannelInfoResponse"
  data_key: "CustomerConnUpstreamChannel"
  record_delimiter: "|+|"
  field_delimiter: "^"
  fields:
    - index: 0
      field: channel_number
      type: integer
    - index: 1
      field: lock_status
      type: string
    - index: 2
      field: channel_type
      type: string
      map:
        "SC-QAM": "qam"
        "OFDMA": "ofdma"
    - index: 3
      field: channel_id
      type: integer
    - index: 4
      field: symbol_rate
      type: frequency
    - index: 5
      field: frequency
      type: frequency
    - index: 6
      field: power
      type: float

system_info:
  sources:
    - format: hnap
      response_key: "GetCustomerStatusConnectionInfoResponse"
      fields:
        - source: CustomerConnSystemUpTime
          field: system_uptime
          type: string
        - source: StatusSoftwareModelName
          field: model_name
          type: string
    - format: hnap
      response_key: "GetArrisDeviceStatusResponse"
      fields:
        - source: FirmwareVersion
          field: software_version
          type: string
```

**Filtering:** Some HNAP modems include placeholder channels with
`channel_id: 0`. Add `filter: { channel_id: { not: 0 } }` when
this is observed in the HAR. Not all HNAP modems need it --- the
S33v2 has no zero-ID placeholders.

### Config fields

| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `format` | string | yes | `hnap` --- selects `HNAPParser` |
| `response_key` | string | yes | HNAP action response key in `hnap_response` dict |
| `data_key` | string | yes | Field within the action response containing delimited data |
| `record_delimiter` | string | yes | Separator between channel records (typically `\|+\|`) |
| `field_delimiter` | string | yes | Separator between fields within a record (typically `^`) |
| `fields` | list | yes | Index-to-field mappings within each record |
| `fields[].index` | integer | yes | Position within the channel record (0-based) |
| `fields[].field` | string | yes | Canonical output field name |
| `fields[].type` | string | yes | Field type (see Common Concepts) |
| `fields[].unit` | string | no | Unit suffix to strip |
| `fields[].map` | dict | no | Value mapping (exact match, applied before type conversion) |
| `channel_type` | object | no | Channel type detection config (see [Channel Type Detection](PARSING_SPEC.md#channel-type-detection)) |
| `filter` | object | no | Row filter rules (see [Filter Rules](PARSING_SPEC.md#filter-rules)) |

### Extraction algorithm

1. Navigate `hnap_response[response_key][data_key]` to get the
   delimited string
2. Split by `record_delimiter` to get channel records
3. For each record, split by `field_delimiter` to get fields
4. Apply `channel_type` map if configured
5. Apply type conversion per `fields[].type`
6. Apply `filter` rules, drop non-matching records
7. Map fields by index to output dict

Action names vary by manufacturer. parser.yaml declares the exact key
names --- the strategy doesn't assume any naming convention.

### HNAP system_info

HNAP system_info sources use flat key-value responses (no delimiters).
Each source maps response keys to canonical field names. Multiple
sources can contribute fields --- last-write-wins on conflicts.

Each field mapping accepts `source`, `field`, `type`, and an optional
`map` (dict, exact-match value mapping applied before type conversion).

Common HNAP system_info fields:

| Response key pattern | Canonical field |
|---------------------|-----------------|
| `*SystemUpTime*` | `system_uptime` |
| `*ModelName*`, `*SoftwareModelName*` | `model_name` |
| `*FirmwareVersion*`, `*SoftwareVersion*` | `software_version` |
