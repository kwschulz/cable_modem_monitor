# System Info Specification

> Parent spec: [PARSING_SPEC.md](PARSING_SPEC.md) — common concepts, output contract, channel type detection

This specification covers how system_info fields (software version,
uptime, hardware version, DOCSIS status) are extracted from modem
responses using format-specific sources.

System info produces a flat dict instead of a channel list. Unlike
channel sections where data typically comes from one source, system info
is often spread across multiple pages or responses. The `sources` list
handles this — each source declares its own format, resource, and fields.
Results are merged into a single dict.

## Contents

| Section | What it covers |
|---------|----------------|
| [Multi-source examples](#multi-source-examples) | HNAP, HTML, JS, REST, and mixed-format source configs |
| [Source processing](#source-processing) | Merge order and last-write-wins semantics |
| [Canonical values](#canonical-values) | Normalizing modem-specific values to canonical form |
| [Formats for system info](#formats-for-system-info) | Format table and format-specific field schemas |
| [System Info Field Tiers](#system-info-field-tiers) | Tier 1 canonical, Tier 2 dedicated, Tier 3 pass-through |

---

## Multi-source examples

**HNAP — fields from multiple SOAP responses:**

```yaml
system_info:
  sources:
    - format: hnap
      response_key: "GetCustomerStatusConnectionInfoResponse"
      fields:
        - source: "CustomerConnNetworkAccess"
          field: docsis_status
          type: string

    - format: hnap
      response_key: "Get{Prefix}DeviceStatusResponse"
      fields:
        - source: "FirmwareVersion"
          field: software_version
          type: string

    - format: hnap
      response_key: "GetCustomerStatusStartupSequenceResponse"
      fields:
        - source: "CustomerConnBootStatus"
          field: boot_status
          type: string
        - source: "CustomerConnSecurityStatus"
          field: security_status
          type: string
```

**HTML — label-based fields from multiple pages:**

```yaml
system_info:
  sources:
    - format: html_fields
      resource: "/MotoHome.asp"
      fields:
        - label: "System Up Time"
          field: system_uptime
          type: string

    - format: html_fields
      resource: "/MotoSwInfo.asp"
      fields:
        - label: "Software Version"
          field: software_version
          type: string
        - label: "Hardware Version"
          field: hardware_version
          type: string

    - format: html_fields
      resource: "/MotoConnection.asp"
      fields:
        - label: "Cable Modem Status"
          field: docsis_status
          type: string
```

**HTML — id-based fields with regex extraction:**

```yaml
system_info:
  sources:
    - format: html_fields
      resource: "/DocsisStatus.asp"
      fields:
        - id: "SystemUpTime"
          pattern: "System Up Time:\\s*(.*)"
          field: system_uptime
          type: string
        - id: "CurrentSystemTime"
          pattern: "Current System Time:\\s*(.*)"
          field: current_time
          type: string
```

**Mixed formats — html_fields + JS variables:**

```yaml
system_info:
  sources:
    - format: html_fields
      resource: "/RouterStatus.htm"
      fields:
        - label: "Hardware Version"
          field: hardware_version
          type: string

    - format: javascript
      resource: "/RouterStatus.htm"
      functions:
        - name: "InitTagValue"
          delimiter: "|"
          fields:
            - offset: 1
              field: software_version
              type: string
            - offset: 33
              field: system_uptime
              type: string
```

**JS variable assignments — multiple pages:**

```yaml
system_info:
  sources:
    - format: javascript_vars
      resource: "/php/status_about_data.php"
      fields:
        - source: js_FWVersion
          field: software_version
          type: string
    - format: javascript_vars
      resource: "/php/status_status_data.php"
      fields:
        - source: js_HWTypeVersion
          field: hardware_version
          type: string
        - source: js_Duration
          field: system_uptime
          type: string
```

**REST — single JSON source:**

```yaml
system_info:
  sources:
    - format: json
      resource: "/rest/v1/cablemodem/state_"
      fields:
        - key: "upTime"
          field: system_uptime
          type: string
          path: "cablemodem"
        - key: "docsisVersion"
          field: docsis_version
          type: string
          path: "cablemodem"
        - key: "status"
          field: docsis_status
          type: string
          path: "cablemodem"
```

**Root-level JSON array — `array_path`:**

When the endpoint returns a root-level JSON array (`[{...}]`), the
resource loader wraps it as `{"_raw": [...]}`. Use `array_path` on the
source to navigate to the array and extract fields from its first
element — same concept as the channel parser's `array_path`:

```yaml
system_info:
  sources:
    - format: json
      resource: "/data/getSysInfo.asp"
      array_path: "_raw"
      fields:
        - key: hwVersion
          field: hardware_version
          type: string
        - key: swVersion
          field: software_version
          type: string
        - key: systemUptime
          field: system_uptime
          type: uptime
          format: "{hours}h:{minutes}m:{seconds}s"
```

---

## Source processing

The `ModemParserCoordinator` processes each source in order and merges
the resulting dicts. If two sources extract the same field name, the
later source wins (last-write-wins). In practice, fields should not
overlap — each source owns distinct fields.

---

## The `map` transform

The `map` property on a field definition normalizes modem-specific raw
values to a canonical or human-readable form. It applies to any field
type — system_info, channel fields, and status indicators.

**Unmapped values always pass through unchanged.** When a raw value has
no matching key in the `map` dict, it flows downstream as-is. This is
by design — `map` normalizes the values you know about (the "happy
path") while preserving unexpected values as raw diagnostic strings.
Modem firmware can return values not seen in HAR captures (error
states, degraded modes, localized text). Silently dropping them would
hide diagnostic information. Passing them through keeps them visible.

Without `map`, the raw value passes through as-is — suitable for
fields that are already human-readable (uptime strings, version
strings).

## Canonical values

Some system_info fields have a canonical value that downstream
consumers (the orchestrator, HA entities) depend on.  Use `map`
to normalize modem-specific raw values to the canonical form.

| Field | Canonical value | Raw examples | Purpose |
|-------|----------------|--------------|---------|
| `docsis_status` | `"Operational"` | `"Allowed"`, `"Connected"`, `"Good"`, `"success"`, `"online"` | Canonical success value. When the parser provides this field, the orchestrator uses it as-is. When absent, the orchestrator enriches `system_info` from channel `lock_status` (see RUNTIME_POLLING_SPEC § Status Derivation). |

**Diagnostic Pass-Through:** Values that represent in-progress or error
states (e.g., `"Ranging"`, `"Scanning"`, `"Locked"`, `"In Progress"`,
`"Access Denied"`) should **not** be mapped to `"Operational"`. They
must flow through as raw diagnostic strings. This allows the HA
Status sensor to show "Operational" when all is well, but preserve the
specific modem-reported state during outages or startup.

```yaml
# Example: normalize modem-specific docsis_status to canonical value
- field: docsis_status
  label: "Network Access"
  type: string
  map:
    "Allowed": "Operational"
```

---

## Formats for system info

| Format | Source type | How fields are found |
|--------|------------|---------------------|
| `html_fields` | HTML page | Three selector types (see below) |
| `javascript` | HTML page | JS function body → delimited string → offset |
| `javascript_vars` | HTML page | `var x = 'value'` assignment → named variable |
| `hnap` | HNAP response | Action response key → JSON key |
| `json` | REST response | Per-field `path` or source-level `array_path` → JSON key |
| `xml` | XML response | Root element → sub-element tag name |

### `javascript` system info field schema

Each JS function in a `javascript` system_info source maps positional
offsets to system_info field names. The field schema is:

| Property | Type | Required | Description |
|----------|------|:--------:|-------------|
| `offset` | integer | yes | Position in the delimited string (0-based) |
| `field` | string | yes | Output field name |
| `type` | string | yes | Field type (see Common Concepts) |
| `map` | dict | no | Value mapping (exact match, applied before type conversion) |

The `map` attribute is the same as on channel field mappings — it
transforms raw values to human-readable strings at extraction time.
Unmapped values pass through unchanged (see [The `map` transform](#the-map-transform) above).

```yaml
# Status indicators with value mapping
- format: javascript
  resource: "/status.htm"
  functions:
    - name: "InitStatusTagValue"
      delimiter: "|"
      fields:
        - offset: 1
          field: power_status
          type: string
          map:
            "0": "Good"
            "1": "Warning"
            "2": "Critical"
```

### `javascript_vars` system info

Extracts values from simple JS variable assignments in HTML
`<script>` tags. Unlike `javascript` (which parses `tagValueList`
delimited strings), this handles standalone named variables:

```javascript
var js_FWVersion = '01.05.063.13.EURO.PC20';
var js_HWTypeVersion = '7';
```

**parser.yaml schema:**

```yaml
system_info:
  sources:
    - format: javascript_vars
      resource: "/php/status_about_data.php"
      fields:
        - source: js_FWVersion
          field: software_version
          type: string
```

**Field schema:**

| Property | Type | Required | Description |
|----------|------|:--------:|-------------|
| `source` | string | yes | JS variable name to match |
| `field` | string | yes | Output system_info field name |
| `type` | string | yes | Field type (see Common Concepts) |
| `map` | dict | no | Value mapping (exact match, applied before type conversion) |

The parser matches both `var x = 'value'` and bare `x = 'value'`
assignments. Only single-quoted string values are supported (the
dominant pattern in Arris gateway firmware). If the extracted value
needs reformatting, use a parser.py PostProcessor to transform it
after extraction.

### `json` system info

JSON system_info sources extract fields from REST/JSON API responses.

**Source schema:**

| Property | Type | Required | Description |
|----------|------|:--------:|-------------|
| `format` | string | yes | `json` |
| `resource` | string | yes | URL path key in the resource dict |
| `encoding` | string | no | Response encoding (e.g., `base64`) |
| `array_path` | string | no | Dot-notation path to a JSON array. Navigates to the array and uses its first element as the source object for field lookups. Same concept as the channel parser's `array_path`. |
| `child_aggregates` | list | no | Aggregate values across repeated array items (see below) |

**Field schema:**

| Property | Type | Required | Description |
|----------|------|:--------:|-------------|
| `key` | string | yes | JSON key name in the source object |
| `field` | string | yes | Output system_info field name |
| `type` | string | yes | Field type (see Common Concepts) |
| `path` | string | no | Dot-notation path to navigate before key lookup (supports dict keys and numeric list indices, e.g., `"device"`, `"_raw.0"`) |
| `format` | string | no | Input format for types that require it (e.g., `"seconds"` for `uptime`) |
| `map` | dict | no | Value mapping (exact match) |

When a response is a root-level JSON array, the resource loader wraps
it as `{"_raw": [...]}`. Use source-level `array_path: "_raw"` to
unwrap it — this is cleaner than repeating `path: "_raw.0"` on every
field.

#### child_aggregates

Iterates the items of a JSON array, filters by key values, and
computes the `max` of a numeric key. Produces a single system_info
field per entry — the JSON counterpart of the
[`xml` source's child_aggregates](#xml-system-info). Both run the same
reducer, so filter rules, `map` normalization, and evaluation order are
identical; the formats differ only in how an item is located.

**Filter values compare as text.** Unlike a channel `filter`, which
compares after type conversion, a child_aggregate normalizes every
filter key to a string first — `type` describes the `max` field, not the
filter keys, so there is nothing to convert them against. Write
`{ not: "0" }`, not `{ not: 0 }`. A non-string rule value is rejected at
config load rather than silently matching nothing.

| Property | Type | Required | Description |
|----------|------|:--------:|-------------|
| `array_path` | string | yes | Dot-notation path to the array, resolved from the whole response |
| `item_path` | string | no | Dot-notation path within each item, for firmware that nests the object inside a wrapper key |
| `filter` | dict | yes | Key-value pairs that must all match, including `{ not: "value" }`. Values compare as text — see below |
| `map` | dict | no | Value mapping (exact match) applied to the `filter` values before comparison |
| `max` | string | yes | Key whose value is maximized |
| `field` | string | yes | Output system_info field name |
| `type` | string | yes | Field type for the aggregated value |
| `scale` | number | no | Multiplier applied after type conversion |

`array_path` resolves from the whole response, not from the
source-level `array_path` element that `fields` read. Evaluation order
matches the channel parser: extract, convert (which applies `map`),
filter, then aggregate — so filters see normalized values.

```yaml
# Extract max provisioned speed per direction from DOCSIS service flows
- format: json
  resource: "/rest/v1/cablemodem/serviceflows"
  fields: []
  child_aggregates:
    - array_path: serviceFlows
      item_path: serviceFlow
      filter: { direction: downstream }
      max: maxTrafficRate
      field: provisioned_speed_down
      type: integer
    - array_path: serviceFlows
      item_path: serviceFlow
      filter: { direction: upstream }
      max: maxTrafficRate
      field: provisioned_speed_up
      type: integer
```

`item_path` handles the wrapper-per-item shape
`{"serviceFlows": [{"serviceFlow": {...}}, ...]}`. Omit it when the
array holds the objects directly.

#### Why `max` is the only operation

A modem registers several service flows per direction, and only one of
them is the subscriber's tier. A capture of the Compal CH7465MT carries
three flows per direction: the tier itself, a secondary flow around
5 Mbit/s, and a small management flow at 128 kbit/s carrying a
`pMinReservedRate`. The provisioned tier is the largest of the set, so
`max` selects it.

Position cannot. Flow counts vary per modem — the Sagemcom F3896LG
publishes one flow per direction where the Compal publishes three — and
element order is not fixed, that same Compal capture emitting its
upstream flows first.

This is why the operation set here differs from the top-level
[`aggregate:` section](PARSING_SPEC.md#aggregate-fields-declared-in-parseryaml),
which supports only `sum`. That section totals a homogeneous set, the
per-channel error counters. This one selects among competing
declarations of the same quantity. Totalling service flows would report
a speed no one is provisioned for. The operations are not
interchangeable, and neither section's set implies the other's; a
proposal to add an operation to either still has to clear the
schema-boundary test in
[ARCHITECTURE_DECISIONS.md § Core's schema tracks fleet-observed
metrics](ARCHITECTURE_DECISIONS.md#cores-schema-tracks-fleet-observed-metrics-not-user-analytics).

#### Service flow direction

`provisioned_speed_down`/`_up` and `provisioned_burst_down`/`_up`
filter on the canonical direction words `downstream` and `upstream`,
in both `xml` and `json` sources. DOCS-IF3-MIB's `IfDirection` textual
convention defines both these words and the numeric codes
`downstream(1)`/`upstream(2)`; firmware that emits the numerics — or
any other spelling — normalizes them in the aggregate's `map`. Cross-file
validation rejects an aggregate that filters on the wrong direction,
because a swap produces plausible-looking but inverted speeds.

### `html_fields` selector types

| Selector | When to use | Algorithm |
|----------|------------|-----------|
| `label: "text"` | Value appears next to a text label | Find element containing label text, cascade through structural patterns (td→sibling td, th→paired td, span→sibling span, dt→dd) to locate the adjacent value element. Prefers leaf elements over ancestors — if a parent element contains the label text only because a child does, the child matches instead. |
| `id: "element_id"` | Value is in an element with a known HTML id | Direct element lookup via `id` attribute, extract text content |
| `css: "selector"` | Value is in an element targeted by CSS selector | CSS selector query via `select_one()`, extract text content |

Use `css` when the target element has no id and no adjacent text label
— for example, elements identified by `data-*` attributes, positional
selectors (`nth-child`), or attribute-value matches
(`th[data-i18n='key'] + td`).

### `html_fields` optional fields

All three selector types support these optional fields:

| Field | Type | Purpose |
|-------|------|---------|
| `attribute` | string | Extract an HTML attribute value instead of text content. When omitted, `get_text()` is used. |
| `pattern` | string | Regex applied to the extracted value (text or attribute). A single capture group returns `group(1)`; no capture group returns the full match. No match → field is skipped. |
| `map` | dict | Value mapping (exact match, applied before type conversion). E.g., `{"Allowed": "Operational"}`. |

**`attribute` and multi-valued HTML attributes:** BeautifulSoup returns
some HTML attributes (notably `class`) as lists. The parser joins
list-valued attributes with spaces to match the original HTML
representation. For example, `class="glyphicon glyphicon-ok"` extracts
as `"glyphicon glyphicon-ok"`, not a Python list. Use `pattern` to
isolate a specific value when the attribute contains multiple tokens.

**Use case — CSS-encoded status indicators:** Some modems encode status
as CSS classes rather than visible text (e.g., Bootstrap `class="success"`
or `class="danger"` with icon glyphicons). These elements have no text
content, so `get_text()` returns nothing useful. Use `css` + `attribute`
to extract the class value:

```yaml
# Extract provisioning status from a CSS-encoded indicator
- css: "td.provisioning-status"
  attribute: "class"
  pattern: "(success|danger)"
  field: provisioning_status
  type: string
```

The raw attribute value passes through as the field value. No value
mapping is applied — the parser extracts what the modem provides. If
the same pattern appears across multiple modems, the field can be
elevated from Tier 3 (modem-specific) to Tier 2 (registered) in the
field registry.

The `label` cascade is anti-fragile: firmware updates that change HTML
structure (e.g., `<th>/<td>` to `<td>/<td>`) don't break configs
because the parser tries multiple structural patterns. New patterns
added to the cascade benefit all modems without config changes.

System info fields are open-ended — modems expose different fields.
The parser extracts whatever is declared in parser.yaml. Fields not
declared are not extracted (implicit capabilities).

### `xml` system info

XML system_info sources extract fields from named sub-elements of a
root element. Used by CBN (Compal Broadband Networks) modems that
return XML responses keyed by `fun` parameter.

**Source schema:**

| Property | Type | Required | Description |
|----------|------|:--------:|-------------|
| `format` | string | yes | `xml` |
| `resource` | string | yes | Resource key (e.g., `"144"` for `fun=144`) |
| `root_element` | string | yes | XML element to navigate to before field extraction |
| `fields` | list | yes | Field mappings (see below) |
| `child_aggregates` | list | no | Aggregate values from repeated child elements (see below) |

**Field schema:**

| Property | Type | Required | Description |
|----------|------|:--------:|-------------|
| `source` | string | yes | XML sub-element tag name |
| `field` | string | yes | Output system_info field name |
| `type` | string | yes | Field type (see Common Concepts) |
| `format` | string | no | Input format for types that require it |
| `map` | dict | no | Value mapping (exact match) |
| `scale` | number | no | Multiplier applied after type conversion (numeric types only) |

#### child_aggregates

Iterates repeated child elements, filters by field values, and
computes the `max` of a numeric sub-element. Produces a single
system_info field per entry. Used for DOCSIS service flow extraction
(e.g., max provisioned speed per direction from `<serviceflow>`
elements).

| Property | Type | Required | Description |
|----------|------|:--------:|-------------|
| `child_element` | string | yes | Tag name of the repeated child element |
| `filter` | dict | yes | Key-value pairs that must all match (sub-element tag → text value), including `{ not: "value" }`. Values compare as text — see below |
| `map` | dict | no | Value mapping (exact match) applied to the `filter` values before comparison |
| `max` | string | yes | Sub-element tag name whose value is maximized |
| `field` | string | yes | Output system_info field name |
| `type` | string | yes | Field type for the aggregated value |
| `scale` | number | no | Multiplier applied after type conversion |

```yaml
# Extract max provisioned speed per direction from DOCSIS service flows
- format: xml
  resource: "144"
  root_element: cmstatus
  fields:
    - source: provisioning_st
      field: provisioning_status
      type: string
  child_aggregates:
    - child_element: serviceflow
      filter: { direction: downstream }
      map: { "1": downstream, "2": upstream }
      max: pMaxTrafficRate
      field: provisioned_speed_down
      type: integer
    - child_element: serviceflow
      filter: { direction: upstream }
      map: { "1": downstream, "2": upstream }
      max: pMaxTrafficRate
      field: provisioned_speed_up
      type: integer
```

This firmware emits the numeric direction codes, so `map` normalizes
them to the canonical words before the filter compares them. See
[Service flow direction](#service-flow-direction).

### Common field property: `scale`

All system_info field mapping models support an optional `scale`
property. When present, the numeric result of type conversion is
multiplied by `scale`. Whole-number float results are cast to int.

This is the same mechanism used by channel column mappings. Common
use: converting raw kHz to Hz (`scale: 1000`).

---

## System Info Field Tiers

system_info fields are organized into tiers that determine how they
surface as HA entities.

**Tier 1 — Canonical (4 fields):** `software_version`, `hardware_version`,
`system_uptime`, `docsis_status`. Defined in `SYSTEM_INFO_FIELDS`
(field_registry.py). Always expected. Drive core behavior (status
derivation, device info, uptime sensors).

**Tier 2 — Dedicated sensor classes:** Fields with specialized HA
entity behavior (device_class, state_class, unit conversion).
Currently: software_version, system_uptime, channel counts, error
totals, error rates. Elevation criteria:

- Natural HA device_class (temperature, data_size, percentage)
- Multiple modems expose it
- Benefits from unit parsing or state_class
- Users would expect it as a first-class entity

Currently Tier 2: software_version, system_uptime, channel counts,
error totals, error rates (`rate_corrected`, `rate_uncorrected`,
errors/min, MEASUREMENT), provisioned_speed_down/up (bit/s, displayed
as Mbit/s, DATA_RATE), provisioned_burst_down/up (B, DATA_SIZE).

**Source of value.** Most Tier 2 fields are populated by the parser
coordinator (native mapping, aggregate, or computed). Error rates are
the exception: they are written by the orchestrator from inter-poll
deltas, since rate is stateful and the parser layer is stateless. See
[ORCHESTRATION_SPEC.md § Derived Fields](ORCHESTRATION_SPEC.md#derived-fields).

**Tier 3 — Pass-through:** All remaining fields. Each gets a generic
`SystemInfoFieldSensor` (icon: `mdi:information-outline`, value as-is).
New system_info fields automatically appear as Tier 3 sensors —
no code change needed.

**Watchlist (Tier 3, candidates for future elevation):**

- `board_temperature` (1 modem) — strong Tier 2 candidate when a
  second modem surfaces it
- `connected_devices` (1 modem) — good metric with state_class=MEASUREMENT
- `memory_used_pct` (computed, 1 modem) — percentage with device_class

`serial_number` and `mac_address` are identity PII with no CMM consumer
and are **not collected**: no `parser.yaml` extracts them and the intake
mapping skips them, so they never reach `system_info` or become
pass-through sensors. Earlier versions surfaced them as Tier 3 sensors —
an unintended consequence of the default pass-through, not a deliberate
choice. If stable device identity is ever wanted, wire `serial_number`/MAC
into the HA device registry (`DeviceInfo` serial + `connections`)
deliberately, not as pass-through sensors.
