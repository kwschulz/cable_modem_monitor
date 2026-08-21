# Platform Specifications

| Document | Governs |
|----------|---------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System design — packages, contracts, invariants, strategies |
| [ARCHITECTURE_DECISIONS.md](ARCHITECTURE_DECISIONS.md) | Design rationale — the "why" behind the architecture |
| [AUTH_SJCL_SPEC.md](AUTH_SJCL_SPEC.md) | `form_sjcl` — SJCL AES-CCM protocol, encoding rules, firmware assumptions |
| [AUTH_PBKDF2_SPEC.md](AUTH_PBKDF2_SPEC.md) | `form_pbkdf2` — PBKDF2 challenge-response protocol, salt handling |
| [AUTH_CBN_SPEC.md](AUTH_CBN_SPEC.md) | `form_cbn` — CBN AES-256-CBC protocol, session token rotation |
| [AUTH_HNAP_SPEC.md](AUTH_HNAP_SPEC.md) | `hnap` — HNAP HMAC challenge-response protocol, HNAP_AUTH header, lockout handling |
| [MODEM_YAML_SPEC.md](MODEM_YAML_SPEC.md) | modem.yaml schema — identity, auth, session, actions |
| [MODEM_DIRECTORY_SPEC.md](MODEM_DIRECTORY_SPEC.md) | Catalog directory structure, file roles, assembly rules |
| [PARSING_SPEC.md](PARSING_SPEC.md) | Parsing overview — common concepts, output contract, channel types, aggregates, post-processing |
| [FORMAT_TABLE_SPEC.md](FORMAT_TABLE_SPEC.md) | HTMLTableParser, HTMLTableTransposedParser, companion table merging |
| [FORMAT_JAVASCRIPT_SPEC.md](FORMAT_JAVASCRIPT_SPEC.md) | JSEmbeddedParser (delimited strings), JSJsonParser (JSON in JS) |
| [FORMAT_HNAP_SPEC.md](FORMAT_HNAP_SPEC.md) | HNAPParser — delimiter-separated values in HNAP JSON responses |
| [FORMAT_JSON_SPEC.md](FORMAT_JSON_SPEC.md) | JSONParser and JSONTransposedParser — JSON API responses, including indexed-pivot rows |
| [FORMAT_XML_SPEC.md](FORMAT_XML_SPEC.md) | XMLParser — XML element children via tag name navigation |
| [SYSTEM_INFO_SPEC.md](SYSTEM_INFO_SPEC.md) | system_info extraction — multi-source, format schemas, field tiers |
| [RESOURCE_LOADING_SPEC.md](RESOURCE_LOADING_SPEC.md) | Resource dict contract, loader behavior, HNAP batching |
| [ORCHESTRATION_SPEC.md](ORCHESTRATION_SPEC.md) | Orchestrator, collector, health monitor, restart monitor — interface contracts and data models |
| [LOGGING_SPEC.md](LOGGING_SPEC.md) | Structured log event union — EventLevel, adapter contract, full event inventory by phase, test pattern |
| [ORCHESTRATION_USE_CASES.md](ORCHESTRATION_USE_CASES.md) | Scenario-based use cases — normal ops, auth failures, connectivity, restart, health, lifecycle |
| [RUNTIME_POLLING_SPEC.md](RUNTIME_POLLING_SPEC.md) | Poll cycle, session lifecycle, health pipeline, restart recovery |
| [FIELD_REGISTRY.md](FIELD_REGISTRY.md) | Three-tier field naming authority |
| [VERIFICATION_STATUS.md](VERIFICATION_STATUS.md) | Modem status enum and verification lifecycle |
| [SNMP_RESEARCH.md](SNMP_RESEARCH.md) | SNMP on cable modems — research findings, deferred |

Intake / onboarding pipeline specs live in the
[Catalog Tools docs](../../cable_modem_monitor_catalog_tools/docs/).
Catalog Tools is the maintainer authoring package — HA never installs
it. See ARCHITECTURE_DECISIONS.md § "catalog_tools is a developer
accelerator, never a runtime dep".

## HA Integration Specs

| Document | Governs |
|----------|---------|
| [CONFIG_FLOW_SPEC.md](../../../custom_components/cable_modem_monitor/docs/CONFIG_FLOW_SPEC.md) | Setup wizard — steps, config entry, variant resolution |
| [ENTITY_MODEL_SPEC.md](../../../custom_components/cable_modem_monitor/docs/ENTITY_MODEL_SPEC.md) | Core output → HA entities, attributes, availability |
| [HA_ADAPTER_SPEC.md](../../../custom_components/cable_modem_monitor/docs/HA_ADAPTER_SPEC.md) | HA wiring — runtime data, coordinators, polling modes, restart, reauth |

## Conventions

Where content goes when adding or editing specs.

### Which document

- `ARCHITECTURE.md` — how the system fits together: packages,
  boundaries, invariants, extension points.
- `ARCHITECTURE_DECISIONS.md` — why it is that way: distilled
  decisions, their rationale, and what they constrain. Design detail
  links out to the spec that owns it.
- `*_SPEC.md` files — contracts the code must satisfy: signatures,
  schemas, rules. The test for spec text: it can be checked against
  the implementation.
- Runnable procedures a person executes step by step belong in
  workflow docs (e.g. the catalog tools intake workflow), which cite
  the governing spec rather than restating its rules.

### Own file vs a section

A topic earns its own file when it has its own conformance surface —
a protocol, format, or subsystem implemented or verified in isolation
(each auth strategy and parser format has one). Otherwise it is a
section in the doc that already governs the surrounding surface. A
new file always gets a row in the index above.

### Anti-bloat and cross-linking

Repo-wide rules — one home per rule, current contract only, what
earns an ADR entry, how to cite another doc — live in
[Project docs § Doc Authoring](../../../docs/README.md#doc-authoring).
