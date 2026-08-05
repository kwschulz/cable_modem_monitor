# Modem Verification Status

## Modem Status Model

Verification state belongs to the modem config, not to the parser. It is
typed by the `ModemStatus` enum in
`models/modem_config/config.py`:

```python
class ModemStatus(StrEnum):
    CONFIRMED = "confirmed"                          # Confirmed working by real user
    AWAITING_VERIFICATION = "awaiting_verification"  # Released, needs user confirmation
    UNSUPPORTED = "unsupported"                      # Modem locked down, kept for documentation
```

`ModemConfig.status` is typed to it, so an unknown value fails validation
before it ships. The status also sets how complete an entry must be:
`validate_required_fields_by_status()` requires `auth`, `hardware`,
`attribution` and `isps` on a `confirmed` or `awaiting_verification` entry,
while `unsupported` needs only the identity fields.

### Status Definitions

| Status | Meaning | Next Steps |
|--------|---------|------------|
| **AWAITING_VERIFICATION** | Parser released but awaiting first user confirmation | Needs community testing |
| **CONFIRMED** | User with real modem confirmed parser works correctly | Stable for use |
| **UNSUPPORTED** | Modem locked down or no exposed status pages, kept for documentation | Awaiting user data |

### Using Status in Parsers

Status is declared under `status` in each modem's variant file (`modem.yaml`
or `modem-{variant}.yaml`). For multi-variant modems, each variant carries its
own status independently — one variant can be `confirmed` while another is
`awaiting_verification`. Promotion from `awaiting_verification` to `confirmed`
follows the ingest procedure in
[MODEM_DIRECTORY_SPEC.md](MODEM_DIRECTORY_SPEC.md#verification-artifact).

---

**Maintainer:** @kwschulz
