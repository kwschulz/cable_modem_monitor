# Code Review Criteria

This document defines the standards for code review in this project.

## Contents

| Section                  | What it covers                                              |
|--------------------------|-------------------------------------------------------------|
| Design Principles        | DRY, SoC, SOLID, no shortcuts, quality gates are not negotiable |
| Source File Standards    | Docstrings, type hints, async, forward refs, suppression discipline |
| Test File Standards      | Table-driven tests, fixtures vs inline, no data blobs, test overrides as code smell, gate scripts require tests |
| Error Handling           | Consistent patterns, meaningful messages                    |
| Naming Conventions       | Clear, descriptive, consistent                              |

---

## Design Principles

### DRY (Don't Repeat Yourself)

- **Extract shared logic** - If code appears 3+ times, extract to a function/class
- **Avoid copy-paste** - Duplicated code diverges over time and causes bugs
- **Use inheritance/composition** - For shared behavior across similar classes
- **Centralize constants** - Define once in `const.py`, import everywhere

```python
# BAD - duplicated validation logic
def validate_host(host):
    if not host or len(host) < 1:
        raise ValueError("Invalid host")

def validate_url(url):
    if not url or len(url) < 1:  # Same pattern!
        raise ValueError("Invalid url")

# GOOD - extracted to reusable function
def validate_non_empty(value: str, name: str) -> None:
    if not value or len(value) < 1:
        raise ValueError(f"Invalid {name}")
```

### Separation of Concerns (SoC)

- **Single responsibility** - Each module/class does one thing well
- **Clear module boundaries** - Don't mix I/O, parsing, and business logic
- **Layered architecture** - UI → Service → Data layers don't skip levels

```text
# Project layers (don't skip levels)
┌─────────────────────────────────────┐
│  Home Assistant Integration Layer   │  ← config_flow.py, sensor.py
├─────────────────────────────────────┤
│  Core Business Logic                │  ← discovery/, auth/, parsers/
├─────────────────────────────────────┤
│  Data/I/O Layer                     │  ← modem_config/, network.py
└─────────────────────────────────────┘
```

### SOLID Principles (where applicable)

- **S - Single Responsibility** - A class should have one reason to change
- **O - Open/Closed** - Open for extension, closed for modification (use ABCs)
- **L - Liskov Substitution** - Subtypes must be substitutable for base types
- **I - Interface Segregation** - Prefer small, focused interfaces
- **D - Dependency Inversion** - Depend on abstractions, not concretions

Most relevant to this project:

- **SRP**: Parsers only parse, auth strategies only authenticate
- **OCP**: New modems added via new parser files, not modifying existing code
- **DIP**: Core code depends on `ModemParser` ABC, not concrete parsers

### No Shortcuts, No Deferred Structure

If a better design is obvious — split by transport, shared types
module, DRY utility — use it now. Don't optimise for speed of first
draft. When a module grows past its natural boundary, restructure the
whole module rather than bolting on the new thing and leaving the rest.

Deferred structure becomes hidden tech debt; "we'll clean it up later"
usually means "we won't." Pay the structural cost in the change that
introduces the need.

### Patterns Yes, Model Rosters No

Core specs document *patterns*, never per-model state. A spec may name
the pattern a behaviour branches on — "the Arris `AT01.01.*` firmware
line uses SHA256, older lines use MD5, selected per entry by
`auth.hmac_algorithm`" — because that is why the code has a branch.
Firmware-line globs and protocol families don't rot.

What must not appear in a Core spec is a roster of models carrying
catalog state: verification status, channel counts, per-model config
values. `modem.yaml` is the authority for those, and the generated
catalog README already renders them. Copied into a spec they have no
sync path, so they drift silently and always in the same direction — a
modem gets confirmed and the spec never hears about it.

Naming a modem as *evidence* is different and stays allowed: "S33v3
HAR capture — SHA256 HMAC path (#98)" is provenance for a claim, dated
and historical by nature, not a live status assertion. Evidence Base
tables are fine; Status columns are not.

This is the same rule as *No Modem-Specific References in Tests* below,
with the same rationale, applied to prose: catalog churn must not
ripple into Core.

### Quality Gates Are Not Negotiable

If mypy, ruff, black, pytest, or any other quality gate fails, fix the
code. Don't exclude files, skip checks, or weaken thresholds. The only
valid exclusions are generated code and vendored dependencies.

This applies to all linters including markdownlint — fix the source
files, don't silence rules that flag real issues. Only configure away
rules that are genuinely inapplicable (e.g., line length for URLs,
duplicate headings in changelogs).

---

## Source File Standards

### Module Docstring (required)

Every Python file must have a module docstring at the top:

```python
"""Short one-line summary of what this module does.

Longer description if needed, explaining:
- Purpose and responsibility
- Key classes/functions provided
- Usage examples

Architecture:
    Optional ASCII diagram showing relationships

Example:
    >>> from module import function
    >>> function("input")
    "output"
"""
```

### Public API Docstrings

All public functions and classes must have docstrings:

```python
def process_data(raw: str, validate: bool = True) -> dict[str, Any]:
    """Process raw modem data into structured format.

    Args:
        raw: Raw HTML or JSON string from modem
        validate: Whether to validate the output schema

    Returns:
        Parsed data dictionary with keys: downstream, upstream, system_info

    Raises:
        ParseError: If raw data cannot be parsed
        ValidationError: If validate=True and output fails schema check
    """
```

### Type Hints (required)

All function signatures must have type hints:

```python
# BAD
def get_parser(name):
    ...

# GOOD
def get_parser(name: str) -> type[ModemParser] | None:
    ...
```

Preferred patterns for common situations:

| Situation | Use | Not |
|-----------|-----|-----|
| Param with fixed valid values | `Literal[...]` | `str` |
| Pydantic model from dict | `Model.model_validate(data)` | `Model(**data)` |
| JSON file caching | `@functools.lru_cache` | global `dict \| None` + manual checks |
| Pass-through `*args/**kwargs` | `Any` | `object` |
| JSON data | `dict[str, Any]` | `dict[str, object]` |

### No Blocking I/O in Async Context

When calling sync functions from async code (e.g., in `config_flow.py`,
`__init__.py`), check whether the function does I/O — file reads,
network calls, subprocess. If yes, wrap the call in an executor:

```python
# BAD - blocks event loop
adapter = get_auth_adapter_for_parser(parser_name)  # reads YAML files

# GOOD - runs in thread pool
adapter = await hass.async_add_executor_job(get_auth_adapter_for_parser, parser_name)
```

Home Assistant warns at runtime ("Detected blocking call…") but
catching this during development is better — Ruff can't detect it
when the blocking call is nested inside another function.

### Loading HAR Fixtures

Always use `load_har_json()` from
`solentlabs.cable_modem_monitor_core.har` for HAR file reads:

```python
from solentlabs.cable_modem_monitor_core.har import load_har_json

har_data = load_har_json(path)
```

Never use raw `json.loads(path.read_text())` for HAR files — they're
stored in Git LFS and the shared loader detects LFS pointers, attempts
`git lfs pull` automatically, and produces actionable guidance instead
of an opaque `JSONDecodeError` when LFS isn't set up. The only
exception is standalone scripts that intentionally avoid Core as a
dependency (e.g., `check_fixture_pii.py`), which inline the LFS-pointer
check themselves.

### No Forward References

Helper functions that reference a class must be defined **after** the
class, not before it. `from __future__ import annotations` makes
forward references parse, but the code reads wrong — a reader hitting
the helper first has to scroll past it to find the class.

```python
# BAD - helper references Foo before Foo is defined
def make_foo() -> Foo:
    return Foo(...)

class Foo: ...

# GOOD - helper after the class
class Foo: ...

def make_foo() -> Foo:
    return Foo(...)
```

### Suppression Discipline

When a quality gate flags an issue, the default reach is the code fix.
Suppression mechanisms (`# type: ignore`, `# pyright: ignore`, bare
`# noqa`, schema-validator scaffolds, validator bypass flags) are last
resorts.

Any suppression added in a change must carry a same-line justification
comment naming what's actually true and why suppression is the right
shape:

```python
# BAD - silent suppression
result = api.call()  # type: ignore

# GOOD - same-line justification
result = api.call()  # type: ignore[no-untyped-call]  # third-party SDK lacks stubs as of v2.10
```

`make suppression-check` (and the `Suppression Discipline` CI job)
enforces this on lines added in your changes; existing suppressions
are grandfathered. Never propose a suppression as the first answer to
a quality-gate failure.

### Corresponding Test File

Every source file should have a corresponding test file:

```text
# Core package
packages/cable_modem_monitor_core/.../auth/form.py
    → packages/cable_modem_monitor_core/tests/auth/test_form.py

# Catalog package
packages/cable_modem_monitor_catalog/.../registry.py
    → packages/cable_modem_monitor_catalog/tests/test_registry.py

# HA adapter
custom_components/cable_modem_monitor/services.py
    → tests/components/test_services.py
custom_components/cable_modem_monitor/dev_tools.py
    → tests/components/test_services.py

# Gate scripts (loaded by path — scripts/ is not an importable package)
scripts/check_changelog.py
    → tests/lib/test_check_changelog.py
packages/cable_modem_monitor_catalog/scripts/check_fixture_pii.py
    → packages/cable_modem_monitor_catalog/tests/test_check_fixture_pii_urls.py
```

---

## Test File Standards

### Module Docstring with TEST DATA TABLES

```python
"""Tests for auth discovery module.

TEST DATA TABLES
================
This module uses table-driven tests. Tables are defined at the top
of the file with ASCII box-drawing comments for readability.
"""
```

### Tables at TOP of File

Define all test data tables immediately after imports:

```python
# =============================================================================
# Test Data Tables
# =============================================================================

# ┌─────────────┬──────────────┬─────────────────────────────┐
# │ input       │ expected     │ description                 │
# ├─────────────┼──────────────┼─────────────────────────────┤
# │ "valid"     │ True         │ normal case                 │
# │ ""          │ False        │ empty string rejected       │
# └─────────────┴──────────────┴─────────────────────────────┘
#
# fmt: off
VALIDATION_CASES = [
    # (input,    expected, description)
    ("valid",    True,     "normal case"),
    ("",         False,    "empty string rejected"),
]
# fmt: on
```

### Use `# fmt: off/on` Guards

Preserve column alignment in tables:

```python
# fmt: off
CASES = [
    ("short",      1,    "x"),
    ("longer",     100,  "y"),
    ("very long",  1000, "z"),
]
# fmt: on
```

### Consume Tables with `@pytest.mark.parametrize`

```python
@pytest.mark.parametrize(
    "input,expected,desc",
    VALIDATION_CASES,
    ids=[c[2] for c in VALIDATION_CASES],  # Use description as test ID
)
def test_validation(input: str, expected: bool, desc: str):
    """Test validation via table-driven cases."""
    result = validate(input)
    assert result == expected, f"Failed: {desc}"
```

### Coverage Requirements

- **Core components**: Target 100% where sensible
- **Parsers**: Focus on parse logic, not every edge case
- **Integration tests**: Cover happy path + critical error paths
- **Gate scripts**: Required — see below

### Gate Scripts Require Tests

Anything wired into `.pre-commit-config.yaml` or a `make` check target
needs a test file, even though `scripts/` sits outside every `--cov`
target and no coverage floor will notice its absence.

These scripts are what enforce quality on everything else, which makes
them the one category where a silent failure is invisible by
construction: a broken gate does not report an error, it reports
success. `check_fixture_pii.py` announced "120 fixture files clean"
while a live credential sat in a HAR URL, because nothing was watching
the watcher.

A gate test must prove the gate **fires**, not merely that it stays
quiet:

- Every guard case (input that must NOT be flagged) is paired with a
  live counterpart asserting the same rule still fires outside the
  guard. Otherwise a rule that has stopped working passes both.
- Verify by mutation before trusting a new gate test: break the rule in
  the script, confirm tests fail, revert. A test suite that passes
  against a deliberately broken gate is testing nothing — see the
  0.12.2 case where guard inputs never matched the pattern under test
  and passed for the wrong reason.

Scripts are loaded by path with `importlib.util.spec_from_file_location`
(`scripts/` is not an importable package). Existing examples:
`tests/lib/test_check_changelog.py`,
`tests/lib/test_check_suppression_discipline.py`.

### Test Overrides Are a Code Smell

If reaching coverage requires heavy mocking, monkeypatching, or test
overrides, the code structure is wrong. Restructure the code (extract
dependency, make injectable) — don't paper over it with test
complexity.

Signs you're papering over structure: more than one or two
`monkeypatch.setattr` calls in a test, mocking internal helpers,
overriding private attributes, or replacing whole modules with stubs.
Any of these mean the seam belongs in the production code, not the
test.

### Schema Tests Use Fixtures, Behavioural Tests Stay Inline

Two distinct test shapes:

- **Schema tests** (valid/invalid configs, parse-or-reject inputs):
  data lives in JSON fixture files. Adding a test case = adding a
  file. Keeps the table of cases visible in the filesystem and avoids
  inline-data bloat.
- **Behavioural tests** (field defaults, access patterns, state
  mutations): assertions stay inline. Each behavior is one test
  function; the assertion shape *is* the test.

What decides the shape is where the meaning lives, not the test's
category. Data the assertion is *about* stays visible in the test;
data that is only setup — a parser config a diagnostics test needs in
order to have something to parse — belongs in a fixture, however
behavioural the assertion. A behavioural test loading a fixture for
its scaffolding is keeping its logic clean, not mixing shapes. A
schema test should still not assert on field defaults.

Fixture content uses placeholder values, never production formats:
no real URLs, hostnames, or vendor-specific payload shapes in
hand-written fixture JSON. Keep fixtures minimal — only the fields
the test exercises. (HAR-derived fixtures are the exception: they
are sanitized captures of real hardware and stay verbatim.)

### No Inline Data Blobs in Test Files

No inline JSON, HTML, or multi-line data in test methods. Data comes
from fixture files (preferred) or named module-level constants:

```python
# BAD - inline JSON blob
def test_parse():
    raw = """{"channels": [
        {"id": 1, "frequency": 100000000, ...},
        ...20 more lines...
    ]}"""

# GOOD - fixture file
def test_parse(load_fixture):
    raw = load_fixture("channels_24down_8up.json")

# ALSO GOOD - module-level constant (when data is short and used once)
SAMPLE_CHANNEL = {"id": 1, "frequency": 100_000_000, "locked": True}

def test_parse_single_channel():
    result = parse(SAMPLE_CHANNEL)
    assert result.frequency == 100_000_000
```

### No Modem-Specific References in Tests

Use generic paths and names (`Solent Labs`, `T100`, `/status.html`).
No cross-boundary imports — test data lives inside the package's own
`tests/`. Modem-specific fixtures belong in the catalog package, not
in core or HA-integration tests.

This keeps Core and HA tests independent of catalog churn — a renamed
modem doesn't ripple into Core's test suite.

---

## Error Handling

### Consistent Exception Types

Define exceptions alongside the code that raises them. Each module
owns its own error types:

```python
# In loaders/http.py
class ResourceLoadError(Exception): ...
class LoginPageDetectedError(ResourceLoadError): ...

# In orchestration/collector.py
class LoginLockoutError(Exception): ...
```

### Meaningful Error Messages

Include context in error messages:

```python
# BAD
raise ValueError("Invalid input")

# GOOD
raise ValueError(f"Invalid host '{host}': must be IP address or hostname")
```

### Modem-Specific Log Messages

Orchestration logging uses the typed event pattern — construct the
appropriate event dataclass and pass it to `log_event()`. The adapter
owns level routing, `[MODEL]` formatting, and message text.

See [`LOGGING_SPEC.md`](../packages/cable_modem_monitor_core/docs/LOGGING_SPEC.md)
for the event taxonomy, level policy, intentional exceptions, and the
`capture_events()` test pattern.

### Log Before Raising

Log errors at appropriate level before raising:

```python
_LOGGER.error("Authentication failed for %s: %s", host, error)
raise AuthenticationError(f"Failed to authenticate with {host}") from error
```

---

## Naming Conventions

### Files and Modules

- **snake_case** for all Python files: `auth_discovery.py`, `config_flow.py`
- **Descriptive names**: `parser_discovery.py` not `pd.py`

### Classes

- **PascalCase**: `ModemParser`, `AuthStrategy`, `DiscoveryPipeline`
- **Suffix with type**: `ArrisSB8200Parser`, `FormPlainAuthStrategy`

### Functions and Variables

- **snake_case**: `get_parser_by_name()`, `working_url`
- **Verb prefixes for functions**: `get_`, `create_`, `validate_`, `parse_`
- **Boolean prefixes**: `is_`, `has_`, `can_`, `should_`

### Constants

- **SCREAMING_SNAKE_CASE**: `DEFAULT_TIMEOUT`, `MAX_RETRIES`
- **Define in `const.py`** for shared constants

### Private Members

- **Single underscore prefix**: `_internal_method()`, `_CACHE`
- **Avoid double underscore** unless name mangling is specifically needed

---

## Quick Checklist

### Source File Review

- [ ] Module docstring present
- [ ] Public functions/classes have docstrings
- [ ] Type hints on all signatures
- [ ] No blocking I/O in async context
- [ ] DRY - no duplicated code blocks
- [ ] SoC - single responsibility
- [ ] Test file exists

### Test File Review

- [ ] Module docstring with TEST DATA TABLES section
- [ ] Tables at TOP with ASCII box-drawing
- [ ] `# fmt: off/on` guards around tables
- [ ] `@pytest.mark.parametrize` consumes tables
- [ ] Descriptive test IDs from table
