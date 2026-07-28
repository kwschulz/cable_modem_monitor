# Parsing Specification

Parsers extract DOCSIS signal data from modem web interface responses.
Every modem's web UI is different — different page structures, different
data formats, different field names — but the output is always the same:
channels with frequency, power, SNR, and error counts.

The parsing system absorbs this variety through three distinct roles:

- **`BaseParser` (ABC)** — the extraction interface, one implementation
  per format in `parsers/formats/`, each parameterized by parser.yaml
  section config. The authoritative format list is
  `ALL_FORMAT_MODELS` in `models/parser_config/config.py`.
- **`ModemParserCoordinator`** — factory and orchestrator. Reads
  parser.yaml, creates `BaseParser` instances per section, runs them,
  chains parser.py post-processing, assembles `ModemData`.
- **parser.py** — optional post-processor for modem-specific quirks.
  Receives extraction output + raw resources, can modify or replace.

`parser.yaml` declares the format and field mappings per data section.
`parser.py` handles modem-specific quirks that can't be expressed
declaratively.

**Design principles:**

- At least one of parser.yaml or parser.py is required — every modem package must have one
- parser.yaml is the primary expression mode — code is the escape hatch
- parser.yaml drives the fetch list; parser.py declares the resources its hooks read via `resources` — the orchestrator merges both at startup
- Format selection is per-section, not per-modem — a modem can mix formats
- `BaseParser` implementations are format experts, not modem experts
- Capabilities are implicit from mappings — no separate declarations
- Parsing is pure — no network calls, no auth, no session state

## Contents

| Section | What it covers |
|---------|----------------|
| [Two Layers: Transport and Format](#two-layers-transport-and-format) | Transport selects loader, format selects parser |
| [Resource Dict](#resource-dict) | What parsers receive — keyed by path, transport-specific values |
| [parser.yaml Schema](#parseryaml-schema) | Common concepts: field types, units, scale, uptime, filters |
| [parser.py — Post-Processing Hooks](#parserpy--post-processing-hooks) | When and how to use code-based post-processing |
| [Output Contract](#output-contract) | ModemData schema, field guarantees, entity identity |
| [Parser Diagnostics](#parser-diagnostics) | Per-resource expected vs. fulfilled anchor counts |
| [Channel Type Detection](#channel-type-detection) | How QAM vs OFDM is determined |
| [Aggregate](#aggregate-derived-system_info-fields) | Channel counts, error totals, scoped sums |
| [Computed](#computed-derived-system_info-fields) | Derived system_info from other system_info fields |
| [Performance Characteristics](#performance-characteristics) | Request counts and timing by transport |

### Format Specifications

Each extraction format has its own specification:

| Document | Formats | Extracts from |
|----------|---------|---------------|
| [FORMAT_TABLE_SPEC.md](FORMAT_TABLE_SPEC.md) | `table`, `table_transposed` | HTML `<table>` elements, companion table merging |
| [FORMAT_JAVASCRIPT_SPEC.md](FORMAT_JAVASCRIPT_SPEC.md) | `javascript`, `javascript_json` | Delimited strings and JSON arrays in JS |
| [FORMAT_HNAP_SPEC.md](FORMAT_HNAP_SPEC.md) | `hnap` | Delimiter-separated values in HNAP JSON |
| [FORMAT_JSON_SPEC.md](FORMAT_JSON_SPEC.md) | `json`, `json_transposed` | JSON API responses via field paths or indexed-pivot rows |
| [FORMAT_XML_SPEC.md](FORMAT_XML_SPEC.md) | `xml` | XML element children via tag names |

### Related Specifications

| Document | What it covers |
|----------|----------------|
| [SYSTEM_INFO_SPEC.md](SYSTEM_INFO_SPEC.md) | system_info extraction — multi-source, format schemas, field tiers |
| [RESOURCE_LOADING_SPEC.md](RESOURCE_LOADING_SPEC.md) | Resource dict contract, loader behavior per transport |
| [FIELD_REGISTRY.md](FIELD_REGISTRY.md) | Three-tier field naming authority |

---

## Two Layers: Transport and Format

**Transport** (modem.yaml) controls *how data is fetched* — the resource
loader. It identifies the transport protocol (`http`, `hnap`, or
`cbn`).

**Format** (parser.yaml, per-section) controls *how data is extracted* —
the extraction strategy. Each section (`downstream`, `upstream`,
`system_info`) declares its own format independently.

```text
modem.yaml transport → resource loader (how to fetch)
parser.yaml format   → decode step + extraction strategy (how to extract, per-section)
```

For the `http` transport, format is independent — any format can appear
with any auth strategy. A modem can mix formats across sections (e.g.,
`table` for downstream, `javascript` for system_info). For `hnap` and
`cbn`, the transport constrains the format (`hnap` and `xml`
respectively).

| Transport | Valid Formats | Why |
|-----------|--------------|-----|
| `hnap` | `hnap` | Protocol-defined: SOAP JSON with delimiters |
| `http` | `table`, `table_transposed`, `html_fields`, `javascript`, `javascript_json`, `json`, `json_transposed` | Format determines decode step; any format supports optional `encoding` property (e.g., `base64` — decoded before format-specific parsing). |
| `cbn` | `xml` | XML POST API: parameterized POST with XML responses |

See [MODEM_YAML_SPEC.md](MODEM_YAML_SPEC.md#validation-rules) for the full transport constraint
table including auth strategies.

### Extraction Formats

| Format | BaseParser Implementation | Extracts from | Scope |
|--------|--------------------------|---------------|-------|
| **HTML** | | | |
| `table` | `HTMLTableParser` | `<table>` elements, rows=channels, cols=metrics | any section |
| `table_transposed` | `HTMLTableTransposedParser` | `<table>` elements, rows=metrics, cols=channels | any section |
| `html_fields` | `HTMLFieldsParser` | Named fields in HTML via label text or element id | system_info only |
| **HTML (embedded JS)** | | | |
| `javascript` | `JSEmbeddedParser` | Delimited strings in JS variables/functions | any section |
| `javascript_json` | `JSJsonParser` | JSON arrays in JS variable assignments | any section |
| `javascript_vars` | `JSVarsSystemInfoParser` | Simple `var x = 'value'` assignments in `<script>` tags | system_info only |
| **JSON** | | | |
| `hnap` | `HNAPParser` | Delimiter-separated values in HNAP JSON responses | any section |
| `json` | `JSONParser` | JSON response structures via field paths | any section |
| `json_transposed` | `JSONTransposedParser` | JSON responses with `name`+`indexN` rows (rows=metrics, cols=channels) | downstream/upstream |
| **XML** | | | |
| `xml` | `XMLParser` | XML element children via tag name navigation | any section |

### Per-Section Format in Practice

Most modems use one format for all sections. But some don't:

```yaml
# Mixed formats — transposed tables for channels, JS for system info
downstream:
  format: table_transposed
  resource: "/cmSignalData.htm"
  # ...

upstream:
  format: table_transposed
  resource: "/cmSignalData.htm"
  # ...

system_info:
  format: javascript
  resource: "/cmSignalData.htm"
  functions:
    - name: "InitSystemInfoTagValue"
      # ...
```

```yaml
# Single format — all sections use the same strategy
downstream:
  format: hnap
  response_key: "GetCustomerStatusDownstreamChannelInfoResponse"
  # ...

upstream:
  format: hnap
  response_key: "GetCustomerStatusUpstreamChannelInfoResponse"
  # ...

system_info:
  format: hnap
  response_key: "GetCustomerStatusConnectionInfoResponse"
  # ...
```

When all sections use the same format, parser.yaml is still explicit —
each section declares its format. No inheritance, no defaults to guess.

**`BaseParser` implementations live in Core.** They consume parser.yaml
config. Adding a modem never requires changing a parser implementation
— if it does, the implementation is missing a config field.

---

## Resource Dict

The resource dict is the contract between the resource loader and the
parser (see [RESOURCE_LOADING_SPEC.md](RESOURCE_LOADING_SPEC.md) for
loader behavior). It contains pre-fetched response data keyed by resource
identifier. Parsers receive it as `resources: dict[str, Any]` and
extract data without making network calls.

### HTML and REST

Keys are the URL paths on the fetch list. One entry per unique path
(deduplicated by the loader).

```python
# HTML
{
    "/MotoConnection.asp": BeautifulSoup,
    "/MotoHome.asp": BeautifulSoup,
}

# REST
{
    "/rest/v1/cablemodem/downstream": dict,
    "/rest/v1/cablemodem/upstream": dict,
    "/rest/v1/cablemodem/state_": dict,
}
```

HTML values are `BeautifulSoup` objects. REST values are parsed JSON
dicts. Root-level JSON arrays are wrapped as `{"_raw": [...]}` to
maintain a consistent dict interface — channel parsers navigate into
the array via `array_path: "_raw"`.

### HNAP

HNAP uses a single batched SOAP request. The resource dict contains the
parsed response with action responses as top-level keys.

```python
{
    "hnap_response": {
        "GetCustomerStatusDownstreamChannelInfoResponse": {...},
        "GetCustomerStatusUpstreamChannelInfoResponse": {...},
        ...
    },
}
```

`hnap_response` is the `GetMultipleHNAPsResponse` dict containing all
action responses. Individual action responses are accessed by key name
from within this dict.

The resource dict contains only data. Auth infrastructure (sessions,
builders, tokens) flows through the orchestrator, not through the
resource dict.

### Fetch List Derivation

The orchestrator reads parser.yaml at startup to build the resource
fetch list. No separate `pages.data` declaration in modem.yaml — the
parser declares what it needs, and the orchestrator ensures it gets
fetched.

**HTML and REST:** The orchestrator collects all unique `resource`
values from parser.yaml sections (downstream, upstream, system_info
sources). These are URL paths. The loader fetches each unique path
once and builds the resource dict.

```text
parser.yaml                          fetch list
─────────────────────────            ──────────────
downstream:                          unique paths:
  resource: "/status.html"    ──►    - /status.html
upstream:                            - /info.html
  resource: "/status.html"
system_info:
  sources:
    - resource: "/info.html"
```

**parser.py:** A `PostProcessor` declares the resources its hooks read
in a `resources` class attribute — a dict of URL path → format (see
[parser.py — Post-Processing Hooks](#parserpy--post-processing-hooks)).
The orchestrator merges these paths into the fetch list, deduplicated
by path; when parser.yaml maps the same path, its declaration wins.
This replaces the former workaround of adding a fake field mapping
whose only purpose was forcing the fetch — declaration now lives with
the code that consumes the resource, and graduating an extraction from
parser.py to parser.yaml moves the resource declaration in the same
edit.

**HNAP:** Sections with `format: hnap` declare `response_key` instead
of `resource`. The orchestrator detects this and tells the HNAP loader
to batch all referenced action names into a single
`GetMultipleHNAPs` request. HNAP action names are derived from `response_key`
by stripping the `Response` suffix. The parser.py `resources` attribute
does not apply to HNAP — the batched request has no per-page fetch
list, and no HNAP modem has needed a hook-only action.

**Validation:** Nothing checks the fetch list at startup. A path that
fails at poll time raises `ResourceLoadError` and ends the cycle
(RESOURCE_LOADING_SPEC § Error Signals). A path that returns HTTP 200
but fails to decode is logged and omitted from the resource dict; the
absence surfaces as `0 of N anchors` → `LOAD_INTEGRITY` through
[Parser Diagnostics](#parser-diagnostics). HNAP declares no
per-resource paths, so neither applies to it.

### Path Navigation

The `_navigate_path()` helper navigates dot-notation paths within
nested structures. It supports both dict keys and numeric list indices:

```python
_navigate_path(data, "downstream.channels")   # → data["downstream"]["channels"]
_navigate_path(data, "_raw.0")                # → data["_raw"][0]
_navigate_path(data, "_raw.0.hwVersion")      # → data["_raw"][0]["hwVersion"]
```

Used by `JSONParser` (for `array_path`) and `JSONSystemInfoParser`
(for per-field `path` and source-level `array_path`).

---

## parser.yaml Schema

parser.yaml defines how each data section is extracted from resources.
Each section declares its `format` (which selects the extraction
strategy) and the format-specific field mappings. Sections within
the same parser.yaml can use different formats.

**parser.yaml never contains auth, session config, or metadata.**

### Common Concepts

#### Field Types

Every field mapping declares a type that controls parsing and conversion:

| Type | Input | Output | Notes |
|------|-------|--------|-------|
| `integer` | `"123"`, `123` | `int` | Strip whitespace |
| `float` | `"3.14 dBmV"` | `float` | Strip unit suffix |
| `string` | any | `str` | Strip whitespace |
| `frequency` | `"507 MHz"`, `507000000` | `int` (Hz) | Auto-detect Hz vs MHz by magnitude; strip unit suffix |
| `boolean` | `true`, `"Locked"` | `bool` | Configurable truthy value |
| `uptime` | `"1471890"`, `"D: 39 H: 06..."` | `str` | Requires `format`. Normalizes to `"N days HHh:MMm:SSs"` |

Only explicitly mapped fields are extracted. Source fields without a
mapping in parser.yaml are ignored — no implicit pass-through from
the source. To capture additional fields, add them to the mapping.

The `frequency` type handles the most common parser quirk: some modems
report in Hz, others in MHz, some include a unit suffix, some don't.
The strategy normalizes all frequencies to Hz.

#### Unit Stripping

Numeric fields often include unit suffixes in the source data (`"3.2 dBmV"`,
`"507 MHz"`, `"-15.3 dB"`). Each field mapping can declare a `unit` suffix
that the strategy strips before type conversion.

```yaml
- field: power
  type: float
  unit: "dBmV"
```

#### Scale Multiplication

Any numeric field mapping can declare an optional `scale` multiplier
applied after type conversion. Whole-number float results are cast to
int. Available across all channel parser formats (table, table_transposed,
json, xml, hnap, javascript).

```yaml
- field: symbol_rate
  type: float
  scale: 1000        # Msym/s → ksym/s
```

#### Uptime Normalization

The `uptime` type normalizes raw uptime values to a canonical format:
`"N days HHh:MMm:SSs"` (e.g., `"17 days 00h:51m:30s"`).

Requires a `format` field declaring the raw input format. Preset
formats:

- `seconds` — raw integer seconds (e.g., `"1471890"`)

Custom formats use `{days}`, `{hours}`, `{minutes}`, `{seconds}`
placeholders to parse modem-specific strings:

```yaml
# Raw seconds (Hub 5, CGA4236, G54)
- field: system_uptime
  type: uptime
  format: seconds

# Custom pattern (TG3442DE: "D: 39 H: 06 M: 24 S: 26")
- field: system_uptime
  type: uptime
  format: "D: {days} H: {hours} M: {minutes} S: {seconds}"

# Optional segment with brackets (Netgear CM2000/CM3000: firmware omits
# the "N days " prefix below 24h, e.g. "10 days 01:41:16" vs "05:07:57")
- field: system_uptime
  type: uptime
  format: "[{days} days ]{hours}:{minutes}:{seconds}"
```

Missing components default to 0. Whitespace in format strings is
matched flexibly. ``[...]`` brackets mark an optional segment — the
content inside is skipped if not present in the input. Brackets do not
nest. Compiled patterns are cached.

#### Filter Rules

Channel-level filtering removes invalid or placeholder records:

```yaml
filter:
  lock_status: "Locked"          # Keep only locked channels
  frequency: { not: 0 }          # Drop zero-frequency placeholders
  channel_id: { not: 0 }         # Drop HNAP placeholder slots
```

Filters apply after field extraction. A channel that fails any filter
condition is excluded from the output.

---

## parser.py — Post-Processing Hooks

parser.py handles modem-specific quirks that can't be expressed
declaratively in parser.yaml. It is an optional post-processor — not
a subclass of `BaseParser` or `ModemParserCoordinator`. The coordinator
invokes parser.py hooks after the `BaseParser` extraction step,
passing both the extraction output and the raw resources.

```text
ModemParserCoordinator
  ├── creates BaseParser instances from parser.yaml (factory)
  │     ├── HTMLTableParser
  │     ├── HNAPParser
  │     └── ...
  ├── runs them per section:
  │     ├── parse primary tables → concatenate → channel list
  │     ├── parse companion tables (merge_by) → merge into channel list
  │     └── section data complete
  ├── passes output + resources to parser.py hooks (if present)
  └── assembles ModemData
```

parser.py does not inherit from the coordinator or from `BaseParser`.
It is a plain class with optional hook methods that the coordinator
discovers and invokes per section.

### When to Use parser.py

- Complex uptime/boot time string parsing
- Restart window filtering (needs runtime state)
- Frequency range→center conversion
- Any extraction that requires conditional logic beyond simple filtering

### Post-Processing Contract

```python
class PostProcessor:
    """Post-process specific sections. Sections without a hook here
    use the BaseParser extraction output as-is.

    Class must be named ``PostProcessor`` — the runner imports this
    exact name from parser.py via ``getattr(module, "PostProcessor")``.
    """

    # Optional: the resources the hooks below read. Dict of URL
    # path → format; the orchestrator merges these into the fetch
    # list at startup (paths parser.yaml already maps are
    # deduplicated, with parser.yaml's format winning).
    resources = {
        "/network_setup.jst": "table",
    }

    def parse_downstream(
        self, channels: list[dict], resources: dict[str, Any]
    ) -> list[dict]:
        """Compute OFDM `frequency` from a non-standard firmware shape.

        OFDM ``frequency`` is the lower edge of the active subcarrier
        band — see FIELD_REGISTRY.md § frequency semantics. Most
        firmwares fit `range: span` (band string) or a discrete
        lower-edge key; reach for a PostProcessor only when the
        firmware shape is novel.
        """
        for ch in channels:
            if ch.get("channel_type") == "ofdm" and ch.get("freq_start"):
                ch["frequency"] = ch["freq_start"]
        return channels

    def parse_system_info(
        self, system_info: dict, resources: dict[str, Any]
    ) -> dict:
        """Replace system info with non-standard extraction."""
        soup = resources.get("/network_setup.jst")
        return self._extract_system_info(soup)

    # parse_upstream is NOT defined — BaseParser output used as-is
```

Each hook receives two arguments:

- The `BaseParser` extraction output for that section (channel list or
  system_info dict). May be empty if parser.yaml has no mapping for
  this section.
- The full resource dict (for accessing raw response data).

The hook's return value is final — it replaces the extraction output
for that section. Last-write-wins: parser.py can add fields, transform
values, filter channels, or fully replace the extraction output.

### Rules

1. **Hooks are per-section.** Define `parse_downstream`,
   `parse_upstream`, or `parse_system_info`. Sections without a hook
   use the `BaseParser` output directly. There is no modem-level hook
   — the coordinator owns the pipeline.

2. **No network calls.** parser.py receives the pre-fetched resource
   dict. No session, no HTTP client, no auth awareness. Declare the
   resources the hooks read in the `resources` class attribute (dict
   of URL path → format) — the orchestrator merges it into the fetch
   list. Never add a fake parser.yaml mapping just to force a fetch.

3. **No auth or session state.** parser.py is a pure data transformer.
   Infrastructure like HNAP builders, session tokens, and cookies flow
   through the orchestrator, not through the parser.

4. **Sandbox Rules (Enforcement):** parser.py must be a "Pure Parser." To
   ensure stability and security, it is subject to the following
   constraints:
   - **Allowed Imports:** Only `__future__`, `math`, `re`, `json`,
     `datetime`, `bs4`, `typing`, `collections`, `functools`, and
     `itertools` are permitted.
   - **No I/O:** No filesystem (`os`, `pathlib`), network (`requests`,
     `urllib`), or process (`subprocess`) calls allowed.
   - **No Side Effects:** Hooks must not modify the `resources` dict or
     global state.
   - **Validation:** The `validate_modem_package` tool and CI suite
     enforce these rules via static analysis of the `parser.py` AST.

5. **Graduate recurring patterns.** When the same hook appears in
   3+ modems, it becomes a parser.yaml config field and the hooks
   are deleted. parser.py is for genuine one-offs.

### What parser.py Returns

The same structure as `BaseParser` extraction output:

```python
# parse_downstream / parse_upstream → list of channel dicts
[
    {
        "channel_id": 1,
        "lock_status": "Locked",
        "modulation": "QAM256",
        "channel_type": "qam",
        "frequency": 507000000,
        "power": 3.2,
        "snr": 38.5,
        "corrected": 0,
        "uncorrected": 0,
    },
    ...
]

# parse_system_info → flat dict
{
    "system_uptime": "7 days 00:00:01",
    "hardware_version": "6.0",
    "software_version": "AB01.02.053",
}
```

### How parser.yaml and parser.py Combine

The `ModemParserCoordinator` processes each section independently:

```text
For each section (downstream, upstream, system_info):
  parser.yaml has mapping for this section?
    └─ Yes → BaseParser extracts per table:
              ├─ primary tables (no merge_by) → concatenate → channel list
              ├─ companion tables (merge_by) → merge fields into channel list
              └─ section data
    └─ No  → empty result ([] or {})
  parser.py has hook for this section?
    └─ Yes → hook(section_data, resources) → final section data
    └─ No  → section data used as-is
```

A modem can freely mix:

- **100% parser.yaml** — all sections declarative, no parser.py
- **parser.yaml + parser.py enrichment** — BaseParser extracts base
  data, parser.py adds/transforms fields using raw resources
- **parser.py replacement** — BaseParser output is empty or ignored,
  parser.py extracts everything from raw resources
- **Mixed per-section** — parser.yaml handles some sections, parser.py
  post-processes others

---

## Output Contract

All `BaseParser` implementations and parser.py hooks produce the same
`ModemData` shape. This is the contract between parsing and everything
downstream (entities, diagnostics, platform output).

Channel dicts and system_info are open — canonical fields are
guaranteed, but any additional field mapped in parser.yaml or extracted
by parser.py passes through without core changes. This lets modems
expose values like `channel_width`, `active_subcarriers`,
`temperature`, or `fft_type` without modifying Core or the
`BaseParser` implementations.

The orchestrator tracks the `system_info` field set between polls and
logs a WARNING (`system_info fields changed`) when fields appear or
disappear. This surfaces firmware-induced selector failures (e.g., a
CSS DOM change) that would otherwise be silent. Orchestrator-derived
fields (`rate_corrected`, `rate_uncorrected`) are excluded from the
comparison.

### ModemData Shape

```python
{
    "downstream": [channel_dict, ...],   # QAM and/or OFDM channels
    "upstream": [channel_dict, ...],     # ATDMA and/or OFDMA channels
    "system_info": {
        # Standard fields (present when the modem exposes them)
        "system_uptime": str,            # e.g., "7 days 00:00:01"
        "hardware_version": str,
        "software_version": str,
        "model_name": str,
        "docsis_status": str,            # "Operational" | "Not Synchronized"

        # Modem-specific fields pass through
        # e.g., "boot_status", "security_status", "docsis_version"
    },
}
```

### Channel Field Contracts by Type

QAM and OFDM are fundamentally different channel types with different
field semantics. QAM channels use a single modulation scheme per
channel — the `modulation` field is meaningful. OFDM channels use
per-subcarrier adaptive modulation across thousands of subcarriers —
there is no single modulation value. The field contracts reflect this.

#### Field publication is per-modem

Modems vary in what they expose. The catalog represents what each
modem actually publishes — it does not fabricate fields the modem
doesn't report.

- **Identity fields** (`channel_number`, `channel_id`, `channel_type`)
  are required on every *locked* channel. They drive entity identity
  and must be canonical there. Unlocked channels are the one exception:
  the nulling rule below removes `channel_type` along with everything
  else, so the requirement does not apply to them.
- **All other fields** in the per-type tables below are *if-published*:
  when the key is present and the value is non-null, it must conform to
  the contract; when the key is absent or the value is null, no
  violation. This covers modems that don't expose a column for the
  field (e.g., older DOCSIS 3.0 status pages without lock state) and
  the unlocked-channel nulling rule (unlocked channels keep only
  `channel_number` and `lock_status`; every other key is removed, not
  set to null).

Parser regressions that silently drop a field are caught by parser ↔
golden self-consistency, not by spec conformance.

#### Identity fields (all channel types)

Every locked channel dict contains these fields. They are used for
entity identity, position tracking, and lock status derivation. An
unlocked channel keeps only `channel_number` and `lock_status`.

| Field | Type | Notes |
| ----- | ---- | ----- |
| `channel_number` | int | 1-based position within direction |
| `channel_id` | int | DCID/UCID assigned by CMTS |
| `channel_type` | str | `"qam"`, `"ofdm"`, `"atdma"`, `"ofdma"` |
| `lock_status` | str | `"locked"` / `"not_locked"` |

#### Canonical modulation values

The `modulation` field carries a single value that names a real modulation
scheme. Canonical form is `QAM` followed by the constellation size, no
separator: `QAM8`, `QAM16`, `QAM32`, `QAM64`, `QAM128`, `QAM256`, `QAM512`,
`QAM1024`, `QAM2048`, `QAM4096`, `QAM8192`, `QAM16384`. `QPSK` is also
permitted. Anything else is a spec violation.

Regex: `^QAM(?:8|16|32|64|128|256|512|1024|2048|4096|8192|16384)$|^QPSK$`

**Provenance: this set is the DOCSIS standard, not a fleet observation.**
The enumeration is the modulation orders defined by the DOCSIS PHY
specifications: QPSK and 8/16/32/64/128-QAM (DOCSIS 3.0 SC-QAM upstream),
64/256-QAM (DOCSIS 3.0 SC-QAM downstream), and 16 through 4096-QAM for
DOCSIS 3.1 OFDM/OFDMA, with 8192/16384-QAM defined as optional downstream
orders. Grounding the set in the spec means omitting a non-canonical value
signals "not a valid DOCSIS modulation," never "an order the fleet has not
shown us yet." If a modem reports a genuinely valid order not listed here
(for example a future DOCSIS revision), the fix is to extend this set with
a citation to the defining spec, not to let the parser silently drop it.

Source: CableLabs DOCSIS Physical Layer Specifications,
`CM-SP-PHYv3.0` (§ Upstream/Downstream RF Channel) and `CM-SP-PHYv3.1`
(§ OFDM/OFDMA modulation), available from the CableLabs specification
portal at <https://www.cablelabs.com/specifications>.

Two cases to keep separate:

- **Incomplete or non-modulation value** (bare `QAM` with no order,
  channel-type restatements like `OFDM`/`OFDMA`, IUC lists): no valid
  DOCSIS modulation exists, so the parser omits the field.
- **Valid but unlisted order**: a gap in this enumeration, so extend it.

Modems report modulation in many surface forms (`256-QAM`, `256 QAM`,
`256qam`, `qam_256`, etc.). Normalization is the parser's job — apply
`map:` at extraction time, never push variants through to consumers.

Non-modulation strings sometimes appear in the source's modulation
column (channel-type restatements like `OFDM`, profile IDs, IUC lists,
bare `QAM`). The parser MUST `map:` them to a canonical value or omit
the field entirely. Passing them through is a spec violation.

#### QAM Downstream (`channel_type: "qam"`)

| Field | Type | Notes |
| ----- | ---- | ----- |
| `frequency` | int | Hz, always normalized |
| `power` | float | dBmV |
| `snr` | float | dB |
| `modulation` | str | Single scheme per channel. Canonical form: `"QAM256"`, `"QAM64"`, etc. See [Canonical modulation values](#canonical-modulation-values). |
| `corrected` | int | Correctable codeword errors |
| `uncorrected` | int | Uncorrectable codeword errors |

#### ATDMA Upstream (`channel_type: "atdma"`)

| Field | Type | Notes |
| ----- | ---- | ----- |
| `frequency` | int | Hz |
| `power` | float | dBmV |
| `modulation` | str | Canonical form: `"QAM64"`, `"QPSK"`, etc. See [Canonical modulation values](#canonical-modulation-values). |
| `symbol_rate` | int | Sym/s |

#### OFDM Downstream (`channel_type: "ofdm"`)

OFDM channels carry different metrics than QAM. `corrected` and
`uncorrected` are LDPC codeword counts (not Reed-Solomon) and are
reported by roughly half the fleet.

| Field | Type | Notes |
| ----- | ---- | ----- |
| `frequency` | int | Hz; lower edge of active subcarrier band (when available). See [FIELD_REGISTRY.md § `frequency` semantics](FIELD_REGISTRY.md#frequency-semantics). |
| `power` | float | dBmV (PLC power) |
| `snr` | float | Average RxMER in dB |
| `corrected` | int | LDPC codeword corrections (when available) |
| `uncorrected` | int | Uncorrectable LDPC codewords (when available) |
| `modulation` | str | Optional. PLC subcarrier modulation in canonical form (e.g., `"QAM4096"`). The parser MUST normalize via `map:` or omit the field if the source carries non-modulation strings. Common offenders: channel-type restatements (`"OFDM"`, `"OFDMA"`, `"OFDM PLC"`, `"Other"`), DOCSIS 3.1 profile IDs, and IUC lists (`"0,1,3,4"`, `"3, 4, 5, 6, 9..."`). See [Canonical modulation values](#canonical-modulation-values). |

#### OFDMA Upstream (`channel_type: "ofdma"`)

| Field | Type | Notes |
| ----- | ---- | ----- |
| `frequency` | int | Hz; lower edge of active subcarrier band (when available). See [FIELD_REGISTRY.md § `frequency` semantics](FIELD_REGISTRY.md#frequency-semantics). |
| `power` | float | dBmV |
| `modulation` | str | Optional. Same rules as OFDM downstream. |

#### Extended fields (optional, pass-through)

These fields are available on some modems and pass through to
downstream consumers without Core interpretation. When present, they
must use the canonical type and unit.

| Field | Type | Notes |
| ----- | ---- | ----- |
| `channel_width` | int | Hz — normalize from MHz or string |
| `fft_type` | str | `"2K"` / `"4K"` / `"8K"` |
| `active_subcarriers` | int | |

#### Fields stripped from OFDM/OFDMA output

These fields are not part of the OFDM contract. If present in parser
output, they are removed by coordinator post-processing.

| Field | Why |
| ----- | --- |
| `is_ofdm` | Redundant with `channel_type`. CM3500B-only artifact. |
| `symbol_rate` | Not applicable to OFDM/OFDMA. |

### Entity Identity Key

Core outputs channels as a list, with each channel carrying identity
fields for both keying strategies. The HA layer selects the entity
identity key via a user-configurable mode — see
[CHANNEL_IDENTIFICATION_SPEC.md](CHANNEL_IDENTIFICATION_SPEC.md) §5
for the mapping manager design.

**Core identity fields (always present on every channel):**

- `channel_number` — the modem's row position (1-based). Stable
  across reboots. Used as entity slot in position mode.
- `channel_id` — the CMTS-assigned DOCSIS Channel ID (DCID/UCID).
  Can change on reboot. Used with `channel_type` as the entity key
  in ID mode.
- `channel_type` — `qam`, `ofdm`, `atdma`, `ofdma`. Disambiguates
  DOCSIS 3.1 modems where SC-QAM and OFDM channels can share the
  same Channel ID.

**Optional identity field:**

- `source_channel_number` — present only on JS-embedded modems when
  the per-function position differs from the unified `channel_number`.
  Enables correlation with modem web UIs that show separate QAM and
  OFDM tables with independent numbering.

Core does not key or index by any of these fields — it emits them as
data. The HA mapping manager reads the user's chosen identity mode
and builds slot maps accordingly.

### Field Guarantees

**Identity fields** — Core understands these and uses them for entity
identity, status derivation, health checks, and DOCSIS lock detection:

- `channel_id` is always present and always an integer (not a string)
- `channel_type` uses canonical values: `qam`, `ofdm`, `atdma`, `ofdma`
- `lock_status` uses canonical values: `locked`, `not_locked`. Raw
  modem-specific values (e.g., `"Locked"`, `"Not Locked"`, `"Active"`,
  `"Inactive"`) are normalized via `map` in parser.yaml, same mechanism
  as `channel_type`. Modems that don't report lock status omit the
  field. The orchestrator uses normalized `lock_status` to derive
  `docsis_status` — see
  [RUNTIME_POLLING_SPEC.md](RUNTIME_POLLING_SPEC.md#status-derivation)
- `channel_number` is always present, 1-based. Most formats auto-assign
  from row/element position when not mapped; HNAP requires explicit
  mapping (all HNAP parser.yaml files map it from index 0). See
  [CHANNEL_IDENTIFICATION_SPEC.md §10](CHANNEL_IDENTIFICATION_SPEC.md#core-channel_number-pre-pass)
  for the per-format reconciliation

**Metric fields** — type and unit guarantees:

- `frequency` is always in Hz (`BaseParser` implementations normalize
  from MHz/GHz)
- `power` and `snr` are floats even when the source is integer
- `channel_width` is always in Hz when present
- Missing optional fields are omitted (not `null` or empty string)
- `system_info` keys are snake_case, values are strings

**Unlocked channels** — when `lock_status` is `"not_locked"`, all
metric fields are stripped. Only `channel_number` and `lock_status`
remain. This prevents firmware noise (`-Infinity`, `0`, `"Unknown"`)
from leaking into output. See CHANNEL_IDENTIFICATION_SPEC.md §6.

**Additional mapped fields** — Core does not validate or interpret
these. They flow from parser output to downstream attributes unchanged.
Only fields explicitly mapped in parser.yaml or returned by parser.py
are included — unmapped source fields are ignored. This is how modems
expose additional data without requiring Core changes.

### Field Name Registry

Field names follow a three-tier system. Core defines Tier 1 canonical
fields (above). The Catalog maintains the full registry — Tier 2
registered fields and Tier 3 naming conventions — because it grows
with the modem collection.

See [FIELD_REGISTRY.md](FIELD_REGISTRY.md)
for the complete registry, graduation criteria, and naming rules.

### Capabilities Are Implicit

The presence of data in the output IS the capability declaration:

- Downstream channels in output → `qam_downstream` capability
- OFDM channels in output → `ofdm_downstream` capability
- `system_uptime` in system_info → `system_uptime` capability

No separate capabilities list in modem.yaml. No registration. If the
parser extracts it, the entity exists.

---

## Parser Diagnostics

Alongside `ModemData`, the parser coordinator surfaces per-resource
diagnostics describing how completely the parse covered the
extraction targets declared in `parser.yaml`. These diagnostics are
the orchestrator's signal that a "successful" empty parse is
actually a stub-page false negative (see UC-19a).

### Contract

For each `resource` on the fetch list — parser.yaml sections, the
per-table and per-array paths inside them, and parser.py `resources`
declarations — the coordinator reports two counts:

| Field | Meaning |
|-------|---------|
| `expected_anchors` | Number of named extraction targets declared for this resource — sum of `functions[].name` across format-specific sources (`format: javascript`), `variable` declarations (`format: javascript_json`), `<table>` matchers (`format: table`), HNAP `data_key` references, etc. |
| `fulfilled_anchors` | Number of those targets the parser actually located in the response body |

Reported as a flat collection of `(resource_path, expected, fulfilled)`
records on the coordinator's parse output, available to the collector
without parsing internals.

### Semantics

- `fulfilled_anchors == expected_anchors` — extraction was complete; any zero-channel result is a real `no_signal` (UC-04 / UC-05).
- `0 < fulfilled_anchors < expected_anchors` — extraction was partial; current behavior preserved (warn and continue). Common on firmware variants where some sources legitimately do not exist.
- `fulfilled_anchors == 0` with `expected_anchors > 0` — stub-response detection. The collector raises this to `CollectorSignal.LOAD_INTEGRITY` (see UC-19a, ORCHESTRATION_SPEC § Signal Catalog).

### Implementation note

How the coordinator computes `fulfilled_anchors` is a format-specific
concern — JS-format parsers know which named functions located their
`tagValueList`; table parsers know which `<table>` matchers hit;
HNAP parsers know which `data_key` references resolved. This
specification defines **what is reported**, not the mechanism by
which each format counts.

**Presence accounting covers what per-anchor counting doesn't.** A
path no format parser counted is reported as one expected anchor,
fulfilled only if the resource reached the parse layer. This is what
covers XML `tables[]`, multi-array JSON `arrays[]`, and parser.py
`resources` — none of which hang off a section-level `resource`, so
no format parser attributes a count to them. A fetched resource that
never arrives (decode failure, loader skip) therefore cannot read as
a clean parse.

**XML sections remain exempt from per-anchor counting**: they report
trivially fulfilled anchors (`_parse_xml_channels` in
`parsers/registries.py`), and the CBN transport has not exhibited the
stub-page failure shape that drives UC-19a. Presence accounting still
covers their table paths. If a CBN modem ever serves a login/stub
page where channel XML is expected, XML sections opt in to real
anchor counting at that point. Parsers may surface the count via
tuple return, recorder injection, or coordinator-side inspection of
the extracted data shape against `parser_config` — whichever fits the
format cleanly.

The `BaseParser` output shape (`ModemData`) is unchanged. Diagnostics
are a sibling channel, not a replacement.

### Diagnostic dump exposure

Captured in HA's diagnostics download (per `RUNTIME_POLLING_SPEC §
Diagnostics for Remote Troubleshooting`) as a list of
`(resource_path, expected_anchors, fulfilled_anchors)` records.
Lets bug-report reviewers see at a glance which resources matched
and which slipped to stub.

### Field Outcomes (system_info)

Anchor counts operate at resource grain. One level down, a parse can
succeed — anchors fulfilled, channels extracted — while an individual
`system_info` field that parser.yaml explicitly maps produces nothing.
That state was previously invisible: an absent source key was skipped
without logging, and a failed type conversion logged only at DEBUG.
Field outcomes give it a home in diagnostics.

For each field mapped in `system_info.sources`, the parse pass
reports one of three outcomes:

| Outcome | Meaning |
|---------|---------|
| produced | Field present in merged system_info output (the normal case; not reported, inferred from output) |
| missing | No configured source produced a value for the field — the source key was absent or empty in every response |
| failed | A source contained a non-empty value, but type conversion rejected it. The raw value is captured (truncated to a fixed cap) |

Field names beginning with an underscore are internal hook
intermediates — values a yaml mapping extracts solely as input for a
parser.py hook — and are excluded from outcome accounting entirely.
They are not part of the modem's data contract and would otherwise
report as permanently missing.

Fields produced only by parser.py (the escape hatch for extractions
with no declarative path yet) are likewise outside the accounting:
with no yaml declaration there is no expectation to compare against,
so a broken hook extraction surfaces through field-set change
detection, not here. When a field graduates from parser.py to a
parser.yaml mapping (and its resource moves from the PostProcessor's
`resources` declaration into the mapping), it enters outcome
accounting automatically.

### Field outcome semantics

- Accounting is section-level, after the multi-source merge: a field
  mapped in two sources counts as produced if either source produced
  it. This matches user-visible impact.
- **No signal or policy coupling.** Unlike zero anchor fulfillment,
  a missing or failed field never raises a collector signal, never
  triggers a retry, and never increments a streak. Field outcomes
  are a diagnostics-only channel. Mapped fields legitimately go
  missing on firmware variants (same rationale as partial anchor
  fulfillment above); the catalog entry, not runtime policy, is the
  place to resolve the mismatch.
- **Distinct from field-set change detection.** The orchestrator's
  field-set change event (ORCHESTRATION_SPEC) fires when the produced
  field set *changes between polls* — a transition detector for
  firmware updates. Field outcomes compare produced fields against
  *configured* fields — a steady-state detector. A field that never
  parses from the first poll onward produces no transition and is
  visible only here.
- The captured raw value on `failed` is the repair datum: it lets a
  maintainer fix a `format` string or `map` entry directly from a
  user-shared diagnostics download, without a debug-log round-trip.
  Raw values are captured only for fields parser.yaml explicitly
  maps — data the catalog already intends to publish in parsed form
  (same trust decision as `last_stub_body`, see ORCHESTRATION_SPEC §
  OrchestratorDiagnostics). Values are truncated to a fixed cap as a
  guard against pathological responses; field values are small.

### Field outcome implementation note

As with anchor counts, this specification defines **what is
reported**, not the mechanism. `missing` derives centrally from the
configured-vs-produced set difference; `failed` reporting is
format-specific — each system_info parser knows when a located value
was rejected by conversion. The `BaseParser` output shape is
unchanged; field outcomes ride the same sibling diagnostics channel
as anchor counts.

**Background:** Issue #98. An Arris S33v3 entry mapped
`system_uptime` from a source-inferred (synthetic) HAR response. On
real hardware the field never appeared, and nothing in logs or
diagnostics could distinguish "modem doesn't send it" from "modem
sends it in an unhandled format" — resolving the question consumed a
maintainer session and the mapping had to be dropped on
absence-of-evidence. Field outcomes make the next such case
self-diagnosing from the first shared diagnostics download.

---

## Channel Type Detection

Modems report channel types differently. The strategy needs to classify
each channel as one of four canonical types: `qam`, `ofdm`, `atdma`,
`ofdma`.

parser.yaml supports four detection mechanisms:

### Fixed

All channels in the section (or table) are the same type. Used for
DOCSIS 3.0 modems (all QAM/ATDMA) and separate-table layouts where
each table contains one channel type:

```yaml
channel_type:
  fixed: "qam"
```

### Derive (universal direction-aware rule)

For modems with no dedicated channel_type column where the canonical
direction-driven rule applies — DS QAM*/QPSK → `qam`, US QAM*/QPSK →
`atdma`, OFDM → `ofdm`, OFDMA → `ofdma`. Replaces hand-coded ``map:``
blocks that enumerate every constellation per direction.

```yaml
channel_type:
  derive: from_modulation
```

The coordinator applies the derivation post-extraction (it knows
direction from the section name). Combine with ``type: modulation`` on
the modulation field so canonicalization runs before derivation.

Use ``map:`` (below) instead when the modem publishes a sentinel
value the universal rule doesn't recognize, or when the channel_type
column is a separate field with non-canonical labels.

### Map (cross-field derivation)

Derives channel type from another already-extracted field's value.
Use this when no dedicated channel type column exists and the type
must be inferred from a related field (e.g., modulation). Only the
``field`` accessor is supported — it references a field already in
the channel dict.

Every expected value from the modem firmware must have an explicit
entry. Unrecognized values produce a warning, allowing new channel
types to surface rather than be misclassified.

```yaml
# Derive channel_type from modulation column
channel_type:
  field: modulation
  map:
    "QAM256": "qam"
    "QAM64": "qam"
    "Other": "ofdm"
```

For same-field mapping (where the source data has a dedicated channel
type column/row/field), use the inline ``map:`` on the field mapping
instead. See [Explicit Field](#explicit-field) below.

### Explicit Field

The source data has a channel type field that is extracted directly
as a field mapping with a ``map`` attribute. The map normalizes the
raw value to a canonical type at extraction time. Use this when the
modem provides a dedicated channel type column/row/field.

All mapping types support ``map``: ``ColumnMapping`` (table),
``RowMapping`` (transposed), ``ChannelMapping`` (HNAP, JS),
``JsonChannelMapping`` (JSON), ``XMLColumnMapping`` (XML), and
``JSSystemInfoFieldMapping`` (system_info javascript).

**HTML table** (column mapping):

```yaml
columns:
  - index: 3
    field: channel_type
    type: string
    map:
      "SC-QAM": "qam"
      "OFDM": "ofdm"
```

**Transposed table** (row mapping):

```yaml
rows:
  - label: "Channel Type"
    field: channel_type
    type: string
    map:
      "TDMA_AND_ATDMA": "atdma"
      "ATDMA": "atdma"
      # DOCS-IF31-MIB defines no channel type field, so this firmware
      # renders the SC-QAM fallback "TDMA" for its OFDMA channels.
      "TDMA": "ofdma"
```

The `TDMA` row above is the Technicolor `.jst` platform's value set
(XB6/XB7/XB8/XB10), where `TDMA` means OFDMA. Map what the capture
shows; do not copy a mapping from another modem's page.

**HNAP** (channel mapping):

```yaml
channels:
  - index: 2
    field: channel_type
    type: string
    map:
      "SC-QAM": "qam"
      "OFDM PLC": "ofdm"
```

---

## Aggregate (Derived system_info Fields)

After extracting all sections (downstream, upstream, system_info) and
applying parser.py hooks, the coordinator enriches `system_info` with
fields derived from parsed channel data. Consumers read these from
`system_info` — the source (native modem value vs coordinator-computed)
is transparent.

### Channel Counts (always computed)

The coordinator always adds channel counts to `system_info`:

- `downstream_channel_count` — `len(downstream)`
- `upstream_channel_count` — `len(upstream)`

If parser.yaml maps native channel counts from the modem's web UI
(e.g., "Number of Channels Connected"), the native value takes
precedence (`setdefault` — coordinator does not overwrite).

### Aggregate Fields (declared in parser.yaml)

Optional section declaring scoped sums from channel data. Each entry
defines a field name, an aggregation operation, and the channel scope.
Error totals are context-dependent — QAM and OFDM use different error
correction mechanisms, so scoping by channel type matters.

```yaml
aggregate:
  total_corrected:
    sum: corrected
    channels: downstream.qam
  total_uncorrected:
    sum: uncorrected
    channels: downstream.qam
```

| Field | Type | Required | Description |
|-------|------| :--------: |-------------|
| `sum` | string | yes | Channel field to sum (e.g., `corrected`, `uncorrected`) |
| `channels` | string | yes | Scope: `downstream`, `upstream`, or type-qualified `downstream.qam`, `downstream.ofdm`, `upstream.atdma`, `upstream.ofdma` |

**Operations:** Only `sum` is supported. This section is purpose-built
for error totals, not a general aggregation engine. Proposals to add
new operations (e.g., `min`/`max`/`spread` for power-delta-style
aggregates) must clear the schema-boundary test in
[ARCHITECTURE_DECISIONS.md § Core's schema tracks fleet-observed
metrics](ARCHITECTURE_DECISIONS.md#cores-schema-tracks-fleet-observed-metrics-not-user-analytics):
the candidate metric must be fleet-observed (exposed by modems
across vendors), not a user-side analytic computed from existing
Core fields. User analytics belong in HA blueprints, not in this
section.

**Why parser.yaml, not modem.yaml?** The parser layer owns the data
context — it knows channel types, field names, and section structure.
Aggregate declarations depend on that context (e.g., scoping by
`downstream.qam` requires knowing that `channel_type` mapping exists).
modem.yaml stays focused on auth, session, and actions.

**Precedence rule:** If parser.yaml maps a `system_info` field with
the same name as an `aggregate` entry (e.g., both produce
`total_corrected`), the native mapping wins. The coordinator skips
the aggregate computation for that field. This handles the common
case: a modem natively reports totals → map them directly. A modem
that doesn't → declare the aggregate to compute them.

**Empty scope:** If the scoped channel set is empty (e.g.,
`downstream.qam` but the modem has only OFDM channels), the aggregate
field is omitted from `system_info`. It is not set to zero.

**DOCSIS version scoping.** DOCSIS 3.0 modems use `channels: downstream`
(all channels are SC-QAM). DOCSIS 3.1 modems use `channels: downstream.qam`
so that `total_corrected` and `total_uncorrected` carry the same semantic
meaning across the fleet: SC-QAM FEC codeword totals only. OFDM codeword
counters are not aggregated into these fields, and OFDM counters are not
comparable to SC-QAM counters. They are different entities at the spec
level, not a unit conversion away from each other.

Two facts in [DOCS-IF31-MIB](https://github.com/rlaager/docsis/blob/master/mibs/DOCS-IF31-MIB)
enforce this boundary:

- **Asynchronous per-profile counter discontinuities.**
  `docsIf31CmDsOfdmProfileStatsCtrDiscontinuityTime` (MIB lines 1710-1719)
  records the sysUpTime of the most recent counter discontinuity per
  profile row. These events fire independently of modem reboot and
  independently of SC-QAM counter resets. A cross-poll delta on OFDM
  counters can include a per-profile reset with no analogue in SC-QAM
  counter semantics.
- **Different forward-error-correction chains.** SC-QAM uses Reed-Solomon
  FEC. OFDM uses concatenated LDPC + BCH, per the MIB descriptions for
  `docsIf31CmDsOfdmProfileStatsCorrectedCodewords` (MIB lines 1581-1589:
  "failed pre-decoding LDPC syndrome check and passed BCH decoding") and
  `UncorrectableCodewords` (MIB lines 1591-1598: "failed BCH decoding").
  Direct numeric comparison across types is not meaningful.

SC-QAM error rates (`rate_corrected`, `rate_uncorrected`) are derived
from these totals by the orchestrator as a stateful inter-poll
computation and inherit this SC-QAM scope automatically. See
[ORCHESTRATION_SPEC.md § Derived Fields](ORCHESTRATION_SPEC.md#derived-fields).
OFDM error rates may be exposed in a separate future feature, with
per-profile entity exposure and per-profile discontinuity awareness;
they cannot be summed with SC-QAM rates. Modems whose only error
counters are OFDM (e.g., SB8200v3 XML API) omit the aggregate section
entirely, and therefore have no rate fields.

**Stale counters from channel reassignment:** DOCSIS 3.1 allows the
CMTS to reassign channel profiles dynamically. A channel slot that was
OFDM can be reassigned to SC-QAM (or vice versa). When this happens,
some modems retain the FEC codeword counters from the previous
assignment. An unlocked SC-QAM channel may carry billions of stale
LDPC codewords from its OFDM era, polluting QAM aggregates if not
filtered. The `lock_status` filter is the correct mitigation:
unlocked channels have unreliable historical data regardless of their
current type assignment.

**Execution:** The coordinator runs aggregate computation after all
sections are extracted and parser.py hooks have run. Results are
merged into `system_info` alongside channel counts, before the
coordinator returns `ModemData` to the collector.

## Computed (Derived system_info Fields)

Optional section declaring fields derived from other system_info fields.
Runs after aggregate computation. Like aggregates, uses `setdefault`
— native values win.

```yaml
computed:
  memory_used_pct:
    operation: percent_used
    inputs:
      total: memory_total
      free: memory_free
```

| Field | Type | Required | Description |
|-------|------| :--------: |-------------|
| `operation` | literal | yes | Named operation: `percent_used` or `combined_status`. |
| `inputs` | dict | yes | Maps operation parameter names to system_info field names |
| `precision` | integer | no | Decimal places for numeric results (default 1, `percent_used` only) |

### Operations

**`percent_used`** — Expects inputs `total` and `free`. Computes
`(total - free) / total * 100`. Skipped when either input is missing,
non-numeric, or total is zero. Values with trailing units (e.g.,
`"524288 kB"`) are parsed by stripping the suffix. Returns float.

**`combined_status`** — Synthesizes a single status from multiple
status fields. Expects N inputs, each mapping to a system_info field.
Returns `"Operational"` when all inputs are present and their values
equal `"Operational"` (case-insensitive). Returns `None` if any input
is missing. Returns the first non-positive value as-is when at least
one input does not match (preserves the raw value for diagnostics).

Modem-specific raw values (e.g. `"Complete"`, `"Allowed"`) are
normalized to `"Operational"` via `map` entries on the input field
definitions in parser.yaml — the coordinator only recognizes the
canonical value.

```yaml
computed:
  docsis_status:
    operation: combined_status
    inputs:
      ds: downstream_status
      us: upstream_status
```

New operations define their own expected input keys.

**Why not parser.py hooks?** Hooks work but don't scale — every modem
with memory fields would need its own parser.py. Declarative computed
fields keep the derivation visible in parser.yaml alongside the field
mappings that produce the inputs.

**Precedence rule:** Same as aggregates. If parser.yaml maps a native
`system_info` field with the same name, the native value wins.

---

## Performance Characteristics

| Phase | Cost | Scale factor |
|-------|------|--------------|
| parser.yaml load | Read + parse one YAML file | Once at startup |
| `BaseParser` instantiation | In-memory, no I/O | Once at startup |
| `parse_resources()` | CPU-bound HTML/JSON parsing | Per poll cycle |
| Field extraction | String splits, type casts | O(channels × fields) |

Parsing is CPU-bound and fast (< 100ms for the largest modems with 32+
channels). The bottleneck is always the HTTP fetch, not the parse.

`BaseParser` instances are created once at startup and reused
every poll cycle.
